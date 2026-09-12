#!/usr/bin/env python3
"""Diagnose why a self-hosted backend URL is rejected by the app's setup probe.

The app classifies a candidate URL by scheme, then runs ONE bounded probe per
backend class (Esplora-HTTP, Electrum-SSL, Bitcoin Core RPC); every failure
collapses to a single value-free refusal line, so a user whose self-hosted
(Start9 etc.) backend "did not check out" can't see where it failed. This
read-only tool replays the three probe shapes against one URL and reports,
per backend, reachability, the TLS-error class (verify-failure vs
tls-handshake vs timeout vs connect), HTTP status, and the mainnet check.

It writes nothing and echoes NO secrets: the password is never printed, URL
userinfo is stripped, no addresses/xpubs are touched — safe to paste in chat.

Probes mirror src/localwallet/chain/{esplora,electrum,bitcoind}.py:
  esplora : GET {base}/blocks/tip then {base}/api/blocks/tip (the app
            auto-tries the /api API-root segment, TCK-BACKEND-003, latching
            whichever answers in shape); an empty-list tip falls back to the
            tip-first GET {root}/blocks page (TCK-BACKEND-004); then
            /blocks/0 (mainnet genesis proof: bare hash, or object with
            id + height == 0, list-wrapped tolerated)
  electrum: TLS socket + server.features genesis_hash == mainnet
  bitcoind: POST getblockchaininfo (chain == "main") — over http for
            http:// and bitcoind:// inputs, over https for https:// and
            bitcoind+tls:// inputs (the app's Core-first classification on
            the https rung, and its stored canonical form, is
            bitcoind+tls://<host>:<port>; --insecure mirrors the
            LOCALWALLET_TLS_VERIFY=0 rung on every TLS probe)

Latency (TCK-BACKEND-004): the three probes hit the SAME host, and on a
.local/mDNS host every fresh getaddrinfo costs seconds (AAAA-then-A
stalls). The two HTTP probes therefore share ONE pooled httpx.Client, and
process-lifetime DNS memoization below collapses all lookups (including
the electrum socket's) to one per host — results are cached per host with
the port re-attached per call, so the address families and flags the
caller asked for still decide the cache key.

Usage: python tools/probe_backend_diag.py URL [--user U --password P]
       [--insecure] [--json]
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
import urllib.parse

import httpx

MAINNET_GENESIS_HASH = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"
ELECTRUM_PORT, BITCOIND_PORT, TIMEOUT = 50002, 8332, 5.0
_VERIFY_NONE = ssl.create_default_context()
_VERIFY_NONE.check_hostname, _VERIFY_NONE.verify_mode = False, ssl.CERT_NONE


def _install_dns_memo():
    """Cache getaddrinfo per (host, family, type, proto, flags) for the
    process; resolve once WITHOUT the port and re-attach it per call. A
    diagnostic run probes up to three ports on one host; slow .local/AAAA
    resolution then costs one stall, not three. Process-lifetime is the
    cache TTL — fine in a tool that exits after the run."""
    real = socket.getaddrinfo
    cache = {}

    def memoized(host, port, family=0, type=0, proto=0, flags=0):
        key = (host, family, type, proto, flags)
        addrs = cache.get(key)
        if addrs is None:
            addrs = cache[key] = real(host, 0, family, type, proto, flags)
        return [(*addr[:4], (addr[4][0], port)) for addr in addrs]

    socket.getaddrinfo = memoized


def classify(exc):
    """Coarse TLS/transport class, walking the cause chain (httpx nests ssl
    errors several levels deep)."""
    original = exc
    for _ in range(8):
        if exc is None:
            break
        if isinstance(exc, ssl.SSLCertVerificationError):
            return "verify-failure"
        if isinstance(exc, ssl.SSLError):
            return "tls-handshake"
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return "timeout"
        if isinstance(exc, (ConnectionRefusedError, ConnectionResetError)):
            return "connect-refused"
        if isinstance(exc, socket.gaierror):
            return "dns"
        exc = exc.__cause__ or exc.__context__
    return type(original).__name__


def strip_userinfo(url):
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc.rsplit("@", 1)[-1], p.path, "", ""))


def _tip_height(resp):
    """The app's tip-height shape ladder (TCK-BACKEND-004 mirror) for a raw
    /blocks/tip response: returns the height, ``None`` for the empty-list
    ``[]`` shape (the caller must consult the tip-first /blocks page), or
    the string "unusable" when the answer is not Esplora shape at all."""
    if resp.status_code != 200:
        return "unusable"
    try:
        payload = resp.json()
    except ValueError:
        return "unusable"
    if isinstance(payload, int) and not isinstance(payload, bool):
        return payload
    if isinstance(payload, list) and not payload:
        return None
    if isinstance(payload, list) and payload and all(
        isinstance(b, dict) and isinstance(b.get("height"), int)
        and not isinstance(b.get("height"), bool) and b.get("height") >= 0
        for b in payload
    ):
        return max(b["height"] for b in payload)
    return "unusable"


def _genesis_ok(payload):
    """The app's genesis proof (check_backend mirror): list-wrapped or bare
    objects must prove id == mainnet genesis AND height == 0; bare hashes
    keep their lenient equality tolerance."""
    entries = payload if isinstance(payload, list) else [payload]
    for entry in entries:
        if isinstance(entry, str) and entry == MAINNET_GENESIS_HASH:
            return True
        if isinstance(entry, dict) and entry.get("id") == MAINNET_GENESIS_HASH:
            height = entry.get("height")
            if height == 0 and not isinstance(height, bool):
                return True
    return False


def probe_esplora(base_url, insecure, client):
    r = {"kind": "esplora", "reachable": False, "tls_error": None,
         "http_status": None, "mainnet": None, "error_class": None,
         "api_root": None}
    try:
        # Mirror the app's /api auto-try (TCK-BACKEND-003): bare {base}
        # first; only when it ANSWERS in a non-Esplora shape (non-2xx
        # or a non-JSON/unshape tip) move to {base}/api — a transport
        # failure is not retried under another path.
        for prefix in ("", "/api"):
            url = base_url.rstrip("/") + prefix
            try:
                resp = client.get(f"{url}/blocks/tip")
            except httpx.TimeoutException:
                r["tls_error"] = r["error_class"] = "timeout"
                r["http_status"] = None
                return r
            except httpx.ConnectError as e:
                r["tls_error"] = r["error_class"] = classify(e)
                return r
            r["http_status"] = resp.status_code
            height = _tip_height(resp)
            if height is None:
                # The [] shape: the app LATCHES this root (a valid JSON
                # answer) and falls back to the tip-first /blocks page AT
                # THE SAME ROOT — /api is no longer tried. Mirror it.
                height = _tip_first_blocks(client, url)
                if height == "unusable":
                    r["error_class"] = "not-esplora-shape (/blocks/tip=[] and /blocks)"
                    return r
            elif height == "unusable":
                r["error_class"] = (
                    "http-status" if resp.status_code != 200
                    else "not-esplora-shape")
                continue
            r["reachable"] = True
            r["api_root"] = prefix or "/"
            blocks = client.get(f"{url}/blocks/0").json()
            r["mainnet"] = _genesis_ok(blocks)
            return r
        return r
    except httpx.InvalidURL:
        r["error_class"] = "invalid-url"
    except Exception as e:  # noqa: BLE001
        r["tls_error"] = r["error_class"] = classify(e)
    return r


def _tip_first_blocks(client, url):
    """GET {url}/blocks — Esplora convention: recent blocks TIP-FIRST.
    Returns the height, "unusable", or None only never happens (the [] case
    already resolved) — non-list/empty/malformed answer is "unusable"."""
    try:
        payload = client.get(f"{url}/blocks").json()
    except (ValueError, httpx.HTTPError):
        return "unusable"
    if (isinstance(payload, list) and payload
            and isinstance(payload[0], dict)
            and isinstance(payload[0].get("height"), int)
            and not isinstance(payload[0].get("height"), bool)
            and payload[0]["height"] >= 0):
        return payload[0]["height"]
    return "unusable"


def probe_electrum(url, insecure):
    r = {"kind": "electrum", "reachable": False, "tls_error": None,
         "http_status": None, "mainnet": None, "error_class": None}
    p = urllib.parse.urlsplit(url)
    ctx = _VERIFY_NONE if insecure else ssl.create_default_context()
    raw = sock = None
    try:
        raw = socket.create_connection((p.hostname, p.port or ELECTRUM_PORT), timeout=TIMEOUT)
        sock = ctx.wrap_socket(raw, server_hostname=p.hostname)
        sock.sendall(b'{"jsonrpc":"2.0","id":1,"method":"server.features","params":[]}\n')
        buf = b""
        while b"\n" not in buf:
            buf += sock.recv(4096)
        gh = json.loads(buf.split(b"\n")[0]).get("result", {}).get("genesis_hash")
        r["reachable"] = True
        r["mainnet"] = bool(gh) and str(gh).startswith(MAINNET_GENESIS_HASH[:8])
    except ssl.SSLCertVerificationError as e:
        r["tls_error"] = r["error_class"] = classify(e)
    except ssl.SSLError as e:
        r["tls_error"] = r["error_class"] = classify(e)
    except (TimeoutError, OSError) as e:
        r["tls_error"] = r["error_class"] = classify(e)
    except Exception as e:  # noqa: BLE001
        r["error_class"] = classify(e)
    finally:
        if sock:
            sock.close()
        elif raw:
            raw.close()
    return r


def probe_bitcoind(url, user, password, insecure, client):
    r = {"kind": "bitcoind", "reachable": False, "tls_error": None,
         "http_status": None, "mainnet": None, "error_class": None,
         "rpc_error_code": None}
    p = urllib.parse.urlsplit(url)
    scheme = {"bitcoind": "http", "bitcoind+tls": "https",
              "https": "https", "http": "http"}.get(p.scheme, "http")
    auth = (user, password) if (user and password) else None
    body = json.dumps({"jsonrpc": "1.0", "id": 1, "method": "getblockchaininfo", "params": []})
    try:
        resp = client.post(f"{scheme}://{p.hostname}:{p.port or BITCOIND_PORT}/",
                           content=body, auth=auth)
        r["http_status"] = resp.status_code
        if resp.status_code == 401:
            r["error_class"] = "auth-refused"
            return r
        # The app's adapter reads the envelope on EVERY status: Core
        # signals its own method rejections as HTTP 500 WITH a JSON-RPC
        # error envelope, so the code is invisible if non-2xx short-
        # circuits to http-status first (TCK-DIAG-002 mirror). The CODE
        # is a protocol constant; the message TEXT is untrusted server
        # data and is deliberately not carried.
        try:
            envelope = resp.json()
        except ValueError:
            envelope = None
        error = envelope.get("error") if isinstance(envelope, dict) else None
        if error:
            r["error_class"] = "rpc-error"
            code = error.get("code") if isinstance(error, dict) else None
            r["rpc_error_code"] = code if isinstance(code, int) else None
        elif resp.status_code != 200:
            r["error_class"] = "http-status"
        else:
            r["reachable"] = True
            r["mainnet"] = (envelope or {}).get("result", {}).get("chain") == "main"
    except httpx.TimeoutException:
        r["tls_error"] = r["error_class"] = "timeout"
    except httpx.ConnectError as e:
        r["tls_error"] = r["error_class"] = classify(e)
    except Exception as e:  # noqa: BLE001
        r["error_class"] = classify(e)
    return r


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url")
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--insecure", action="store_true",
                    help="disable TLS verify (mirror LOCALWALLET_TLS_VERIFY=0)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    _install_dns_memo()
    # ONE pooled client for both HTTP probes (TCK-BACKEND-004): with the
    # DNS memo above the host is looked up ONCE for the whole run, however
    # many kinds/ports/paths it serves.
    with httpx.Client(timeout=TIMEOUT, verify=not a.insecure) as client:
        results = [probe_esplora(a.url, a.insecure, client),
                   probe_electrum(a.url, a.insecure),
                   probe_bitcoind(a.url, a.user, a.password, a.insecure, client)]
    if a.json:
        print(json.dumps({"url": strip_userinfo(a.url), "insecure": a.insecure,
                          "probes": results}, indent=2))
        return 0
    print(f"URL: {strip_userinfo(a.url)}  (--insecure: {a.insecure})")
    print("Password: [redacted]")
    for r in results:
        line = (f"\n[{r['kind']}]\n  reachable: {r['reachable']}\n  tls_error: "
                f"{r['tls_error']}\n  http     : {r['http_status']}\n  mainnet  : "
                f"{r['mainnet']}\n  error    : {r['error_class']}")
        if r.get("api_root"):
            line += f"\n  api_root : {r['api_root']}"
        if r.get("rpc_error_code") is not None:
            line += f"\n  rpc_code : {r['rpc_error_code']}"
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

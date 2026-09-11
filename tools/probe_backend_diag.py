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
            whichever answers in shape) then /blocks/0 (mainnet genesis)
  electrum: TLS socket + server.features genesis_hash == mainnet
  bitcoind: POST getblockchaininfo (chain == "main") — over http for
            http:// and bitcoind:// inputs, over https for https:// and
            bitcoind+tls:// inputs (the app's Core-first classification on
            the https rung, and its stored canonical form, is
            bitcoind+tls://<host>:<port>; --insecure mirrors the
            LOCALWALLET_TLS_VERIFY=0 rung on every TLS probe)

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


def probe_esplora(base_url, insecure):
    r = {"kind": "esplora", "reachable": False, "tls_error": None,
         "http_status": None, "mainnet": None, "error_class": None,
         "api_root": None}
    try:
        with httpx.Client(timeout=TIMEOUT, verify=not insecure) as c:
            # Mirror the app's /api auto-try (TCK-BACKEND-003): bare {base}
            # first; only when it ANSWERS in a non-Esplora shape (non-2xx
            # or a non-JSON tip) move to {base}/api — a transport failure
            # is not retried under another path.
            for prefix in ("", "/api"):
                try:
                    resp = c.get(f"{base_url.rstrip('/')}{prefix}/blocks/tip")
                except httpx.TimeoutException:
                    r["tls_error"] = r["error_class"] = "timeout"
                    r["http_status"] = None
                    return r
                except httpx.ConnectError as e:
                    r["tls_error"] = r["error_class"] = classify(e)
                    return r
                r["http_status"] = resp.status_code
                usable = resp.status_code == 200 and resp.text.strip().isdigit()
                if not usable and resp.status_code == 200:
                    try:
                        resp.json()
                    except ValueError:
                        r["error_class"] = "not-esplora-shape"  # 200 non-JSON
                        continue
                if not usable:
                    r["error_class"] = (
                        "http-status" if resp.status_code != 200
                        else "not-esplora-shape")
                    continue
                r["reachable"] = True
                r["api_root"] = prefix or "/"
                blocks = c.get(f"{base_url.rstrip('/')}{prefix}/blocks/0").json()
                ids = [b.get("id") if isinstance(b, dict) else b for b in blocks]
                r["mainnet"] = MAINNET_GENESIS_HASH in ids
                return r
            return r
    except httpx.InvalidURL:
        r["error_class"] = "invalid-url"
    except Exception as e:  # noqa: BLE001
        r["tls_error"] = r["error_class"] = classify(e)
    return r


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


def probe_bitcoind(url, user, password, insecure):
    r = {"kind": "bitcoind", "reachable": False, "tls_error": None,
         "http_status": None, "mainnet": None, "error_class": None}
    p = urllib.parse.urlsplit(url)
    scheme = {"bitcoind": "http", "bitcoind+tls": "https",
              "https": "https", "http": "http"}.get(p.scheme, "http")
    auth = (user, password) if (user and password) else None
    body = json.dumps({"jsonrpc": "1.0", "id": 1, "method": "getblockchaininfo", "params": []})
    try:
        with httpx.Client(timeout=TIMEOUT, verify=not insecure) as c:
            resp = c.post(f"{scheme}://{p.hostname}:{p.port or BITCOIND_PORT}/",
                          content=body, auth=auth)
            r["http_status"] = resp.status_code
            if resp.status_code == 401:
                r["error_class"] = "auth-refused"
            elif resp.status_code != 200:
                r["error_class"] = "http-status"
            else:
                r["reachable"] = True
                r["mainnet"] = resp.json().get("result", {}).get("chain") == "main"
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
    results = [probe_esplora(a.url, a.insecure),
               probe_electrum(a.url, a.insecure),
               probe_bitcoind(a.url, a.user, a.password, a.insecure)]
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
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

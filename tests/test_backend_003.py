"""TCK-BACKEND-003 — the two Start9 live repros, pinned as regressions.

Repro 1 (D2, /api): ``https://evil-star.local:56191`` — a private
mempool.space instance whose API lives under ``/api`` — was refused though
``/api/...`` paths worked, because the probe joined ``{base}/blocks/tip``
with no /api tolerance. Fixed in ONE seam (``EsploraClient._request_json``
auto-tries ``{base}/api{path}`` when the bare join answers in a non-Esplora
shape and latches the winner), so the onboarding probe AND the live client
(saved bare URL) both work.

Repro 2 (D1, https Core): ``https://192.168.0.25:65154`` — Bitcoin Core
RPC behind a TLS reverse proxy with basic auth — was refused because the
``https://`` rung routed ONLY to the Esplora probe and ``BitcoindClient``
accepted only the plain-http scheme. Fixed by the ``bitcoind+tls://``
sibling scheme (Core-first probe on the https rung; the CANONICAL stored
form keeps the transport bit — deviation from the ticket's literal
"rewrite to bitcoind://" because a plain scheme would rebuild a plain-http
client that cannot reach the node; ADR-0018 amendment).

Everything is loopback with a throwaway self-signed cert (imported from
tests/test_chain_tls — test-only key material for a fixture server, not a
secret in any sense PROJECT.md protects). No public network.
"""

from __future__ import annotations

import base64
import http.server
import json
import ssl
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest

from localwallet import app as app_module
from localwallet.app import BACKEND_PROBE_FAIL, BitcoindClient, _build_chain_client
from localwallet.chain import (
    MAINNET_GENESIS_HASH,
    ChainConfig,
    EsploraClient,
    check_backend,
)
from localwallet.chain.esplora import _ApiRootMismatch
from localwallet.config import Settings
from localwallet.store.db import Store, StoreError
from tests.test_chain_esplora import BROADCAST_TXID, TX_HEX
from tests.test_chain_tls import _SELF_SIGNED_CERT, _SELF_SIGNED_KEY

TIP = 800_000
GENESIS_ENTRY = [{"id": MAINNET_GENESIS_HASH, "height": 0}]


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No real HOME cookie rung, no real config file: every Settings ladder
    resolves inside tmp_path only (same discipline as test_chain_bitcoind)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LOCALWALLET_TLS_VERIFY", raising=False)
    monkeypatch.delenv("LOCALWALLET_RPC_COOKIE_PATH", raising=False)
    monkeypatch.setattr(
        "localwallet.config.CONFIG_FILE_PATH", tmp_path / "host-config-absent.json"
    )


class _Paths:
    """Per-fixture request-path recorder (shared by both TLS handlers)."""

    def __init__(self) -> None:
        self.seen: list[str] = []


def _start_tls_server(handler_class: type, tmp_path: Path) -> http.server.ThreadingHTTPServer:
    key = tmp_path / "key.pem"
    crt = tmp_path / "cert.pem"
    key.write_text(_SELF_SIGNED_KEY)
    crt.write_text(_SELF_SIGNED_CERT)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _url_for(server: http.server.ThreadingHTTPServer) -> str:
    return f"https://127.0.0.1:{server.server_address[1]}"


class _ApiOnlyEsploraHandler(http.server.BaseHTTPRequestHandler):
    """Repro 1: a mempool.space-style server — API ONLY under /api, the
    frontend root 404s every API path (the answer a bare saved host gets)."""

    paths: _Paths

    def do_GET(self) -> None:
        self.paths.seen.append(self.path)
        table = {
            "/api/blocks/tip": str(TIP).encode("ascii"),
            "/api/blocks/0": json.dumps(GENESIS_ENTRY).encode(),
            "/api/v1/prices": json.dumps({"USD": 60_000}).encode(),
            "/api/tx": BROADCAST_TXID.encode(),
        }
        body = table.get(self.path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.paths.seen.append(self.path)
        self.do_GET()

    def log_message(self, *_args: Any) -> None:
        return


class _TlsCoreHandler(http.server.BaseHTTPRequestHandler):
    """Repro 2: Core RPC over a TLS reverse proxy, basic auth demanded."""

    creds: tuple[str, str] | None = ("rpcu", "rpcp")
    chain: str = "main"
    authed: ClassVar[list[bool]] = []  # was an Authorization header present, per POST

    def do_POST(self) -> None:
        header = self.headers.get("Authorization")
        type(self).authed.append(header is not None)
        want = None if self.creds is None else f"{self.creds[0]}:{self.creds[1]}"
        given: str | None = None
        if header is not None and header.startswith("Basic "):
            try:
                given = base64.b64decode(header[6:], validate=True).decode("ascii")
            except Exception:  # noqa: BLE001 — a broken header is simply wrong
                given = None
        if want is not None and given != want:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            method = json.loads(self.rfile.read(length))["method"]
        except (ValueError, KeyError):
            self.send_response(400)
            self.end_headers()
            return
        if method == "getblockchaininfo":
            result = {"chain": self.chain, "blocks": TIP, "headers": TIP,
                      "bestblockhash": "ab" * 32}
        elif method == "getnetworkinfo":
            result = {"version": 220_000, "subversion": "/Satoshi:fixture/"}
        else:
            result = None
        body = json.dumps({"result": result, "error": None, "id": 1}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture()
def esplora_factory(tmp_path: Path) -> Any:
    """Started TLS Esplora servers, torn down after the test. ``make()``
    returns (bare https URL, paths-recorder)."""
    made: list[http.server.ThreadingHTTPServer] = []

    def make() -> tuple[str, _Paths]:
        paths = _Paths()
        bound = type("HB", (_ApiOnlyEsploraHandler,), {"paths": paths})
        server = _start_tls_server(bound, tmp_path)
        made.append(server)
        return _url_for(server), paths

    yield make
    for server in made:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def core_factory(tmp_path: Path) -> Any:
    """Started TLS Core-RPC servers, torn down after. ``make(**tuning)`` →
    (bare https URL, handler-subclass-with-authed-list)."""
    made: list[http.server.ThreadingHTTPServer] = []

    def make(
        *, chain: str = "main", creds: tuple[str, str] | None = ("rpcu", "rpcp")
    ) -> tuple[str, Any]:
        bound = type("CB", (_TlsCoreHandler,), {"creds": creds, "chain": chain, "authed": []})
        server = _start_tls_server(bound, tmp_path)
        made.append(server)
        return _url_for(server), bound

    yield make
    for server in made:
        server.shutdown()
        server.server_close()


_SETTINGS = Settings(request_timeout_s=2.0, max_retries=0)


# ---------------------------------------------------------------------------
# REPRO 1 — bare-host https Esplora: probe, save, and live client-join.
# ---------------------------------------------------------------------------


class TestRepro1ApiTolerance:
    def test_probe_and_save_bare_host_with_tls_off(
        self, esplora_factory: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """check_backend AND the settings-write probe reach the API through
        the auto-tried /api segment with LOCALWALLET_TLS_VERIFY=0, and the
        bare URL SAVES (stored as-is — no /api rewrite)."""
        url, _paths = esplora_factory()
        monkeypatch.setenv("LOCALWALLET_TLS_VERIFY", "0")
        assert check_backend(url, timeout_s=5.0, max_retries=0) is True
        assert app_module._probe_chain_backend(url, _SETTINGS) == url
        store = Store(tmp_path / "r1.db")
        try:
            store.set_chain_base_url(url)  # the save the probe authorizes
            assert store.get_chain_base_url() == url
        finally:
            store.close()

    def test_live_client_joins_the_saved_bare_url(
        self, esplora_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pin the plan demands: a SAVED bare-host URL works after save,
        not just at probe time. First request pays ONE root 404 then latches
        /api; every later request rides /api directly — no repeated
        double-hits."""
        url, paths = esplora_factory()
        monkeypatch.setenv("LOCALWALLET_TLS_VERIFY", "0")
        with EsploraClient(base_url=url, timeout_s=5.0, max_retries=0) as client:
            assert client.get_tip_height() == TIP  # 404 at root, then /api
            assert client.get_tip_height() == TIP  # latched
            assert client.get_json("/v1/prices", "prices") == {"USD": 60_000}
        assert paths.seen == [
            "/blocks/tip",  # first: bare join mismatches (404)
            "/api/blocks/tip",  # /api wins → latched
            "/api/blocks/tip",  # second tip call: single hit, root never retried
            "/api/v1/prices",
        ]

    def test_probe_refused_under_default_verify(self, esplora_factory: Any) -> None:
        """Fail-closed default (TLS verify ON): the self-signed backend is a
        verify-failure class → probe False, readiness None."""
        url, _paths = esplora_factory()
        assert check_backend(url, timeout_s=5.0, max_retries=0) is False
        assert app_module._probe_chain_backend(url, _SETTINGS) is None

    def test_api_suffixed_url_unchanged_single_join(
        self, esplora_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A base already ending /api (the public default's shape) is
        PRE-RESOLVED: never double-requested, never /api/api'd; the probe
        stores the URL unchanged."""
        url, paths = esplora_factory()
        monkeypatch.setenv("LOCALWALLET_TLS_VERIFY", "0")
        suffixed = f"{url}/api"
        assert check_backend(suffixed, timeout_s=5.0, max_retries=0) is True
        assert app_module._probe_chain_backend(suffixed, _SETTINGS) == suffixed
        assert "/api/api" not in "".join(paths.seen)  # no doubled segment
        assert "/blocks/tip" not in paths.seen  # bare join never attempted


class TestApiFallbackMechanics:
    """Offline (MockTransport) pins of the join rule itself."""

    def test_bare_base_retries_once_and_latches(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path == "/api/blocks/tip":
                return httpx.Response(200, text=str(TIP))
            if request.url.path == "/api/blocks/0":
                return httpx.Response(200, json=GENESIS_ENTRY)
            return httpx.Response(404)

        with EsploraClient(
            base_url="https://host.self", timeout_s=1.0, max_retries=0,
            transport=httpx.MockTransport(handler),
        ) as client:
            assert client.get_tip_height() == TIP
            assert client.get_tip_height() == TIP
        assert seen == ["/blocks/tip", "/api/blocks/tip", "/api/blocks/tip"]

    def test_200_non_json_body_also_counts_as_mismatch(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/blocks/tip":
                return httpx.Response(200, text=str(TIP))
            return httpx.Response(200, text="<html>frontend</html>")

        with EsploraClient(
            base_url="https://host.self", timeout_s=1.0, max_retries=0,
            transport=httpx.MockTransport(handler),
        ) as client:
            assert client.get_tip_height() == TIP

    def test_transport_failure_never_walks_the_prefix(self) -> None:
        """Unreachable/timeout is NOT the mismatch class (a path change fixes
        nothing): one attempt class, one collapse, snappy probe budget."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            raise httpx.ConnectError("nope")

        assert check_backend(
            "https://down.self", timeout_s=0.5, max_retries=0,
            transport=httpx.MockTransport(handler),
        ) is False
        assert seen == ["/blocks/tip"]  # no /api second try after a transport loss

    def test_persistent_mismatch_surfaces_the_original_error(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(404)

        with EsploraClient(
            base_url="https://host.self", timeout_s=1.0, max_retries=0,
            transport=httpx.MockTransport(handler),
        ) as client, pytest.raises(_ApiRootMismatch) as excinfo:
            client.get_tip_height()
        assert "status 404" in str(excinfo.value)
        assert seen == ["/blocks/tip", "/api/blocks/tip"]  # both tried, once each

    def test_broadcast_rides_the_latched_root(self) -> None:
        """A POST is NEVER speculatively sent twice, but once the reads have
        resolved /api the broadcast joins it too (single send, /api path)."""
        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path))
            if request.url.path == "/api/blocks/tip":
                return httpx.Response(200, text=str(TIP))
            if request.url.path == "/api/tx":
                return httpx.Response(200, text=BROADCAST_TXID)
            return httpx.Response(404)

        with EsploraClient(
            base_url="https://host.self", timeout_s=1.0, max_retries=0,
            transport=httpx.MockTransport(handler),
        ) as client:
            assert client.get_tip_height() == TIP  # resolves /api
            assert client.broadcast_tx(TX_HEX) == BROADCAST_TXID
        assert seen.count(("POST", "/api/tx")) == 1  # single send, latched root
        assert ("POST", "/tx") not in seen  # bare join never POSTed twice


# ---------------------------------------------------------------------------
# REPRO 2 — https Core RPC: classify, canonical rewrite, live rebuild.
# ---------------------------------------------------------------------------


class TestRepro2HttpsCore:
    def test_probe_classifies_core_and_rewrites_canonical(
        self, core_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The user's exact flow: bare https RPC URL + stored login (the
        auth-kwarg path) → classified Core, canonical form
        ``bitcoind+tls://host:port`` (the transport bit SURVIVES the save —
        see the ADR-0018 amendment deviation note)."""
        url, handler = core_factory()
        monkeypatch.setenv("LOCALWALLET_TLS_VERIFY", "0")
        auth = app_module._BackendAuth(user="rpcu", password="rpcp")
        assert app_module._probe_chain_backend(url, _SETTINGS, auth) == (
            "bitcoind+tls://" + url.partition("://")[2]
        )
        assert handler.authed and all(handler.authed)  # auth rode the POSTs

    def test_wrong_chain_refused(self, core_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        url, _handler = core_factory(chain="signet")
        monkeypatch.setenv("LOCALWALLET_TLS_VERIFY", "0")
        auth = app_module._BackendAuth(user="rpcu", password="rpcp")
        assert app_module._probe_chain_backend(url, _SETTINGS, auth) is None

    def test_default_verify_refuses_self_signed_core(self, core_factory: Any) -> None:
        """Fail-closed default: the self-signed RPC node is a verify-failure
        class → plain None refusal (no crash escaping the probe)."""
        url, _handler = core_factory()
        auth = app_module._BackendAuth(user="rpcu", password="rpcp")
        assert app_module._probe_chain_backend(url, _SETTINGS, auth) is None

    def test_bare_url_without_creds_refuses_with_creds_hint(
        self, core_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix-D (documented skip): the 401 collapses to the ONE value-free
        refusal, and the refusal line now NAMES the login case as a static
        hint (the probe's collapse-everything contract carries no failure
        CLASS; the hint is the honest channel)."""
        url, handler = core_factory()
        monkeypatch.setenv("LOCALWALLET_TLS_VERIFY", "0")
        assert app_module._probe_chain_backend(url, _SETTINGS) is None  # no creds
        assert "login" in BACKEND_PROBE_FAIL and "credentials" in BACKEND_PROBE_FAIL
        for secret in ("127.0.0.1", "rpcu", "rpcp", url):
            assert secret not in BACKEND_PROBE_FAIL  # value-free
        assert handler.authed[:1] == [False]  # the bare probe sent no header → 401

    def test_explicit_tls_scheme_stored_as_is(
        self, core_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Typing the canonical scheme directly is the explicit-choice rung:
        probed over TLS, stored UNCHANGED."""
        url, _handler = core_factory()
        canonical = "bitcoind+tls://" + url.partition("://")[2]
        monkeypatch.setenv("LOCALWALLET_TLS_VERIFY", "0")
        auth = app_module._BackendAuth(user="rpcu", password="rpcp")
        assert app_module._probe_chain_backend(canonical, _SETTINGS, auth) == canonical

    def test_stored_canonical_rebuilds_live_https_client(
        self, core_factory: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE deviation proof: the stored ``bitcoind+tls://`` URL builds (via
        the ONE ``_build_chain_client`` seam) a live client that actually
        speaks TLS — a plain ``bitcoind://`` rewrite could not."""
        url, _handler = core_factory()
        canonical = "bitcoind+tls://" + url.partition("://")[2]
        monkeypatch.setenv("LOCALWALLET_TLS_VERIFY", "0")
        settings = Settings(request_timeout_s=2.0, max_retries=0, chain_base_url=canonical)
        auth = app_module._BackendAuth(user="rpcu", password="rpcp")
        client = _build_chain_client(settings, auth)
        assert isinstance(client, BitcoindClient)
        try:
            assert client.get_tip_height() == TIP
        finally:
            client.close()

    def test_backend_kind_and_config_kind_are_bitcoind(self) -> None:
        """One scheme-dispatch seam: the TLS sibling badges and dispatches
        exactly like the plain scheme."""
        settings = Settings(chain_base_url="bitcoind+tls://node.local:8332")
        assert app_module._backend_kind(settings, resolved=True) == "bitcoind"
        assert ChainConfig("bitcoind+tls://h:1", 5.0, 0).kind == "bitcoind"
        assert ChainConfig("bitcoind+tls://h:1", 5.0, 0).tls_verify is True

    def test_open_https_core_no_auth_demanded(self, core_factory: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """A reverse proxy that does NOT demand auth: the bare https URL
        classifies Core with no credential overlay at all (Start9 default)."""
        url, _handler = core_factory(creds=None)
        monkeypatch.setenv("LOCALWALLET_TLS_VERIFY", "0")
        assert app_module._probe_chain_backend(url, _SETTINGS) == (
            "bitcoind+tls://" + url.partition("://")[2]
        )


# ---------------------------------------------------------------------------
# The stored rung + config shapes for the new scheme.
# ---------------------------------------------------------------------------


class TestStoredRung:
    def test_store_accepts_tls_scheme_shapes(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "shapes.db")
        try:
            store.set_chain_base_url("bitcoind+tls://node.local:65154")
            assert store.get_chain_base_url() == "bitcoind+tls://node.local:65154"
            store.set_chain_base_url("bitcoind+tls://h")  # default port applies
            for bad in (
                "bitcoind+tls://",
                "bitcoind+tls://h/bad/path",
                "bitcoind+tls://u:p@h",  # stored rung: never userinfo
                "bitcoind+tls://h:notaport",
            ):
                with pytest.raises(StoreError):
                    store.set_chain_base_url(bad)
        finally:
            store.close()

    def test_esplora_client_refuses_the_tls_scheme(self) -> None:
        """The fail-closed construction guard covers the whole family — no
        httpx client ever posts at a bitcoind+tls:// URL."""
        with pytest.raises(ValueError):
            EsploraClient(base_url="bitcoind+tls://h:1")


# ---------------------------------------------------------------------------
# Refusal copy (fix C) — value-free hints.
# ---------------------------------------------------------------------------


class TestRefusalHints:
    def test_hints_present_and_value_free(self) -> None:
        assert "LOCALWALLET_TLS_VERIFY=0" in BACKEND_PROBE_FAIL
        assert "/api" in BACKEND_PROBE_FAIL
        # Honest about the escape hatch (transport auth goes off).
        assert "authentication is then OFF" in BACKEND_PROBE_FAIL
        # The TCK-BACKEND-002 glosses survive the append:
        assert "Esplora (mempool.space-style) http(s)" in BACKEND_PROBE_FAIL
        assert "Bitcoin Core RPC (bitcoind://)" in BACKEND_PROBE_FAIL
        assert "nothing was saved and the current backend stays in service" in (
            BACKEND_PROBE_FAIL
        )

    def test_apply_failure_returns_the_hints(self, tmp_path: Path) -> None:
        """The settings-write surface actually DELIVERS the line (no host
        echo): a failed probe → refusal + hints, nothing stored."""
        flow, store = _tiny_flow(tmp_path)
        error, fields = flow.apply("https://evil-star.local:56191")
        assert error == BACKEND_PROBE_FAIL
        assert "evil-star" not in str(error)
        assert fields == {}
        assert store.get_chain_base_url() is None  # NOTHING saved
        store.close()


def _tiny_flow(tmp_path: Path) -> Any:
    """A ChainBackendFlow with a dead-probe (None) and no engine wiring:
    apply()'s pre-store refusal path needs nothing more."""
    from localwallet.app import ChainBackendFlow

    class _Wiring:
        pass

    wiring = _Wiring()
    wiring.store = Store(tmp_path / "flow.db")
    wiring.settings = Settings(request_timeout_s=1.0, max_retries=0)
    wiring.scan = None
    wiring.client = None
    return ChainBackendFlow(wiring, lambda _url: None), wiring.store


# ---------------------------------------------------------------------------
# The diagnostic script stays faithful (smoke against a live TLS fixture).
# ---------------------------------------------------------------------------


def test_probe_diag_script_mirrors_the_app(
    esplora_factory: Any, tmp_path: Path
) -> None:
    """tools/probe_backend_diag.py against the repro-1 fixture: the SAME
    /api auto-try the app now runs, reported as api_root; class-based
    verify-failure under default TLS."""
    url, _paths = esplora_factory()
    script = Path(__file__).resolve().parents[1] / "tools" / "probe_backend_diag.py"
    insecure = json.loads(
        subprocess.run(
            [sys.executable, str(script), url, "--insecure", "--json"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
    )
    probes = {p["kind"]: p for p in insecure["probes"]}
    assert probes["esplora"]["reachable"] is True
    assert probes["esplora"]["api_root"] == "/api"
    assert probes["esplora"]["mainnet"] is True
    strict = json.loads(
        subprocess.run(
            [sys.executable, str(script), url, "--json"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
    )
    strict_esplora = {p["kind"]: p for p in strict["probes"]}["esplora"]
    assert strict_esplora["reachable"] is False
    assert strict_esplora["tls_error"] == "verify-failure"

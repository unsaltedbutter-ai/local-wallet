"""TCK-BACKEND-004 (+TCK-DIAG-002 folded) — root-cause repros from the
MW-16 round-2 USER PAYLOADS, replayed OFFLINE (the implementer cannot reach
the VPN; the payloads are the evidence, pinned verbatim).

(1) Private mempool.space (Start9) at an https host —
    ``GET {api}/blocks/tip`` answers the EMPTY list ``[]`` and
    ``GET {api}/blocks/0`` a LIST-WRAPPED genesis OBJECT. The app probe
    must ACCEPT (tip via the tip-first ``/blocks`` fallback, genesis via
    the wrapped-object proof with hash + height==0 re-verified) and must
    never report a shape refusal as ``network-error`` again (the class-
    degradation the user saw: the empty-list ChainError carried no
    failure_class, so the report fell through classify_failure to
    network-error).

(2) bitcoind (Core 31, txindex+coinstatsindex, permissions proven fine
    by the user's direct ``scantxoutset status`` curl) — the scan failed
    as ``class=network-error exc=ChainError`` because the taxonomy had no
    ``rpc-error``. Two root causes are pinned here:
      * REQUEST SHAPE: the old scanobject ``desc(raw(<hex>))`` is Core's
        OUTPUT form; ``scantxoutset`` parses INPUT with the bare grammar
        (Core's own example is ``raw(<hex>)#checksum``), so a wrapped
        descriptor arrives back as a JSON-RPC error envelope — the
        "rejected by the server" the user saw. Repro: an error-envelope
        body classified ``rpc-error`` with its numeric code, the server's
        message TEXT never echoed.
      * TIMEOUT→RETRY LOOP: ``start`` answers only after a minutes-long
        walk, so the generic 10 s read timeout guaranteed a mid-scan
        loss, and the retry re-sent ``start`` against the still-held
        scan reserver ("Scan already in progress"). Repro: the scan call
        outlives a tiny per-request timeout (dedicated
        ``_SCAN_TIMEOUT_S`` budget) and is never retried (retries=0).

(3) The probe latency fix: the diag tool runs all three kinds against ONE
    host; a slow getaddrinfo (.local/mDNS, AAAA-then-A) was paid THREE
    times (separate clients + raw socket). Pinned: the memoized resolver
    resolves a host ONCE per process whatever ports the probes use.

App-surface pins: the scan-failure line carries ``code=`` value-free and
names the rpc-error suspects ONLY for a utxo-scan refusal.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet import app as app_module
from localwallet.app import Settings, _probe_chain_backend
from localwallet.chain import ChainError, EsploraClient, check_backend
from localwallet.chain.esplora import (
    MAINNET_GENESIS_HASH,
    NOT_ESPLORA_SHAPE,
    NOT_MAINNET,
    RPC_ERROR,
)

# Reuse the loopback Core fixture machinery from the M2 suite (the fixture
# factory itself gets its local re-declaration below, pytest-style).
from tests.test_chain_bitcoind import CLOSE, BitcoindFixture, _client, _scan_result
from tests.test_wallet_scan import ADDRS

_TOOLS = Path(__file__).resolve().parents[1] / "tools"


@pytest.fixture()
def bitcoind(tmp_path: Path) -> Any:
    """The M2 loopback Core stub, re-declared here exactly as its home
    module defines it (fixture factories must not be import-shadowed)."""
    made: list[BitcoindFixture] = []

    def make(**kwargs: Any) -> BitcoindFixture:
        server = BitcoindFixture(tmp_path, **kwargs)
        made.append(server)
        return server

    yield make
    for server in made:
        server.stop()


def _load_diag_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "probe_backend_diag", _TOOLS / "probe_backend_diag.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# The USER PAYLOAD, verbatim from the MW-16 round-2 curls (192.168.0.25:
# 56191 — values public Bitcoin data / LAN host shapes; no secret rides a
# genesis block).
# ---------------------------------------------------------------------------

_USER_GENESIS_OBJECT = {
    "id": MAINNET_GENESIS_HASH,
    "height": 0,
    "version": 1,
    "timestamp": 1231006505,
    "bits": 486604799,
    "nonce": 2083236893,
    "difficulty": 1,
    "merkle_root": "4a5e1e4baab89f3a32518a88c31bc87f618f76673e2cc77ab2127b7afdeda33b",
}

_TIP_PAGE = [
    {"id": "ff" * 32, "height": 900_000, "timestamp": 1_750_000_000},
    {"id": "ee" * 32, "height": 899_999, "timestamp": 1_749_999_000},
]


def _mempool_handler(
    blocks_page: Any = _TIP_PAGE,
    *,
    genesis_entry: Any = _USER_GENESIS_OBJECT,
    bare_root_ui: bool = True,
) -> Any:
    """The private mempool.space server shape: the frontend answers at /,
    the API under /api; ``/api/blocks/tip`` is ``[]``; ``/api/blocks/0``
    is the LIST-WRAPPED genesis object."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if bare_root_ui and not path.startswith("/api"):
            return httpx.Response(404, text="<html>mempool frontend</html>")
        api = path.removeprefix("/api")
        if api == "/blocks/tip":
            return httpx.Response(200, json=[])
        if api == "/blocks":
            return (
                blocks_page
                if isinstance(blocks_page, httpx.Response)
                else httpx.Response(200, json=blocks_page)
            )
        if api == "/blocks/0":
            return httpx.Response(200, json=[genesis_entry])
        return httpx.Response(404)

    return handler


def _client_for(handler: Any, base: str = "https://h.example") -> EsploraClient:
    return EsploraClient(
        base_url=base, timeout_s=5.0, max_retries=0,
        transport=httpx.MockTransport(handler),
    )


class TestTipEmptyListRepro:
    def test_check_backend_accepts_the_user_payload(self) -> None:
        """[] tip + tip-first /blocks page + list-wrapped genesis → ACCEPT.
        (check_backend runs on the BARE host; the /api auto-try latches,
        then the [] fallback rides the same latched root.)"""
        report: dict[str, str] = {}
        seen: list[str] = []

        def counting(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return _mempool_handler()(request)

        assert check_backend(
            "https://h.example", transport=httpx.MockTransport(counting), report=report
        )
        assert seen == [
            "/blocks/tip",        # bare root: the frontend 404s (mismatch)
            "/api/blocks/tip",    # [] — latches /api, triggers the fallback
            "/api/blocks",        # tip-first page → height
            "/api/blocks/0",      # list-wrapped genesis object
        ]
        assert report == {}

    def test_get_tip_height_takes_the_tip_first_entry(self) -> None:
        with _client_for(_mempool_handler()) as client:
            assert client.get_tip_height() == 900_000  # page[0], NOT the max

    def test_get_tip_block_falls_back_with_timestamp(self) -> None:
        with _client_for(_mempool_handler()) as client:
            block = client.get_tip_block()
        assert block.height == 900_000
        assert block.timestamp == 1_750_000_000

    def test_fallback_page_failure_names_both_endpoints(self) -> None:
        """[] tip AND a malformed /blocks page → honest refusal naming the
        endpoints tried, value-free, classed not-esplora-shape."""
        with _client_for(_mempool_handler(blocks_page={"junk": 1})) as client, pytest.raises(
            ChainError
        ) as excinfo:
            client.get_tip_height()
        message = str(excinfo.value)
        assert "/blocks/tip and /blocks" in message
        assert excinfo.value.failure_class == NOT_ESPLORA_SHAPE
        assert "900000" not in message and "junk" not in message  # value-free

    def test_empty_blocks_page_also_refused(self) -> None:
        with _client_for(_mempool_handler(blocks_page=[])) as client, pytest.raises(
            ChainError, match="/blocks/tip and /blocks"
        ):
            client.get_tip_height()

    def test_the_class_no_longer_degrades_to_network_error(self) -> None:
        """THE user symptom: with TLS verify off the app read
        ``class=network-error`` at the esplora-shape stage. A shape refusal
        (valid JSON, wrong shape, fallback ALSO wrong) now reports
        not-esplora-shape."""
        report: dict[str, str] = {}
        ok = check_backend(
            "https://h.example",
            transport=httpx.MockTransport(_mempool_handler(blocks_page="junk")),
            report=report,
        )
        assert not ok
        assert report["failure_class"] == NOT_ESPLORA_SHAPE

    def test_bare_integer_tip_shape_unchanged(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(200, json=870_000)

        with _client_for(handler, base="https://h.example/api") as client:
            assert client.get_tip_height() == 870_000
        assert calls == ["/api/blocks/tip"]  # no fallback request was made


class TestGenesisProof:
    @pytest.mark.parametrize(
        ("entry", "accepted"),
        [
            (_USER_GENESIS_OBJECT, True),   # list-wrapped object, height 0
            ({"id": MAINNET_GENESIS_HASH, "height": 12345}, False),  # wrong height
            ({"id": MAINNET_GENESIS_HASH}, False),  # no height proof → fail closed
            ({"id": MAINNET_GENESIS_HASH, "height": True}, False),  # bool height
            (MAINNET_GENESIS_HASH, True),   # tolerated bare hash
        ],
        ids=["object-h0", "object-hWrong", "object-noHeight", "object-boolH", "bare-hash"],
    )
    def test_genesis_entry_shapes(self, entry: Any, accepted: bool) -> None:
        report: dict[str, str] = {}
        ok = check_backend(
            "https://h.example/api",
            transport=httpx.MockTransport(_mempool_handler(genesis_entry=entry)),
            report=report,
        )
        assert ok is accepted
        if not accepted:
            assert report["failure_class"] == NOT_MAINNET

    def test_bare_object_payload_tolerated(self) -> None:
        """/blocks/0 answering a bare (UNWRAPPED) genesis object is still
        proven by hash + height==0."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/blocks/tip"):
                return httpx.Response(200, json=870_000)
            return httpx.Response(200, json=_USER_GENESIS_OBJECT)

        assert check_backend("https://h.example/api", transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# (2) bitcoind: rpc-error classification + the scan budget/retry pair.
# ---------------------------------------------------------------------------


class TestRpcErrorClassification:
    def test_500_with_envelope_classifies_rpc_error(self, bitcoind: Any) -> None:
        """The user's exact failure surface, now classifiable: Core's
        HTTP-500-with-error-envelope rejection. The numeric code rides the
        debug carry; the server's text is never echoed."""
        server = bitcoind(
            script={
                "scantxoutset": [
                    (
                        "error",
                        -8,
                        "Scan already in progress, use action \"abort\" or \"status\"",
                    )
                ]
            }
        )
        with _client(server) as client, pytest.raises(ChainError) as excinfo:
            client.get_address_utxos(ADDRS[0][0])
        exc = excinfo.value
        assert str(exc) == "utxo-scan request rejected by the server"
        assert exc.failure_class == RPC_ERROR
        assert exc.exc_name == "RPCError"
        assert exc.rpc_code == -8
        assert "already in progress" not in str(exc)

    def test_2xx_error_envelope_classifies_rpc_error(self, bitcoind: Any) -> None:
        """The request-shape root cause replayed: Core's descriptor-parse
        refusal (RPC_INVALID_ADDRESS_OR_KEY) on a wrapped scanobject."""
        body = json.dumps(
            {"result": None, "error": {"code": -5, "message": "Unrecognized output descriptor desc"}, "id": 1}
        )
        server = bitcoind(script={"scantxoutset": [("raw", body)]})
        with _client(server) as client, pytest.raises(ChainError) as excinfo:
            client.get_address_utxos(ADDRS[0][0])
        exc = excinfo.value
        assert exc.failure_class == RPC_ERROR
        assert exc.rpc_code == -5
        assert "Unrecognized" not in str(exc)

    def test_non_object_error_member_carries_no_code(self, bitcoind: Any) -> None:
        body = json.dumps({"result": None, "error": "nope", "id": 1})
        server = bitcoind(script={"getblockchaininfo": [("raw", body)]})
        with _client(server) as client, pytest.raises(ChainError) as excinfo:
            client.get_tip_height()
        exc = excinfo.value
        assert exc.failure_class == RPC_ERROR  # still an RPC-layer refusal
        assert exc.rpc_code is None  # nothing numeric was parseable


class TestScanBudget:
    def test_scan_outlives_the_generic_request_timeout(self, bitcoind: Any) -> None:
        """The fixture HANGS on scantxoutset for a full second — LONGER
        than the client's 0.25 s per-request timeout. The scan still
        succeeds because it runs on the dedicated bounded budget; a
        generic-timeout scan would have died exactly like the user's."""
        server = bitcoind(
            script={"scantxoutset": lambda p: _scan_result([])},
            hang=("scantxoutset",),
        )
        with _client(server, timeout_s=0.25) as client:
            assert client.get_address_utxos(ADDRS[0][0]) == []
        assert server.counts["scantxoutset"] == 1

    def test_normal_reads_keep_the_generic_budget(self, bitcoind: Any) -> None:
        """The dedicated budget is SCAN-ONLY: a hung getblockchaininfo
        still times out at the client's 0.25 s (class timeout)."""
        server = bitcoind(hang=("getblockchaininfo",))
        with _client(server, timeout_s=0.25) as client, pytest.raises(ChainError) as excinfo:
            client.get_address_utxos(ADDRS[0][0])
        assert excinfo.value.failure_class == "timeout"

    def test_scan_is_never_retried(self, bitcoind: Any) -> None:
        """A lost scan response is surfaced ONCE: a retry would re-send
        ``start`` against the still-held reserver — the self-inflicted
        'Scan already in progress' rejection of the user's log. Even with
        a client-wide retry budget, the scan gets exactly one send."""
        server = bitcoind(script={"scantxoutset": [CLOSE]})
        with _client(server, max_retries=2) as client, pytest.raises(ChainError):
            client.get_address_utxos(ADDRS[0][0])
        assert server.counts["scantxoutset"] == 1


# ---------------------------------------------------------------------------
# App surfaces: the code carry and the value-free suspect guidance.
# ---------------------------------------------------------------------------


def _rpc_refusal(message: str = "utxo-scan request rejected by the server") -> ChainError:
    return app_module.ChainError(
        message, failure_class=RPC_ERROR, exc_name="RPCError", rpc_code=-8
    )


class TestAppLines:
    def test_scan_line_carries_code_and_names_the_suspects(self) -> None:
        store = app_module.Store(None)
        wallet = store.create_wallet("default", "desc")
        worker = app_module.ChainWorker(None)
        flow = app_module.ScanFlow(store, wallet, worker, gap_limit=None)
        try:
            outputs: list[str] = []
            exc = _rpc_refusal()
            flow._warn(outputs.append, str(exc), exc)
        finally:
            worker.stop()
            store.close()
        line = outputs[0]
        assert "[class=rpc-error exc=RPCError code=-8]" in line
        assert "rejected by the server's RPC layer" in line
        assert "RPC role/permissions" in line
        assert "request shape" in line and "scan timeout" in line
        # value-free: no URL, host, address, or server text.
        assert "://" not in line and "already in progress" not in line

    def test_hint_is_scan_specific(self) -> None:
        suffix = app_module._scan_failure_suffix(_rpc_refusal("broadcast request rejected by the server"))
        assert "class=rpc-error exc=RPCError code=-8" in suffix
        assert "RPC layer" not in suffix  # not a UTXO-scan refusal → no suspects

    def test_non_rpc_failure_unchanged(self) -> None:
        exc = app_module.ChainError(
            "tip-height request failed: status 503",
            failure_class="http-status",
            exc_name="HTTPStatus",
        )
        suffix = app_module._scan_failure_suffix(exc)
        assert suffix == " [class=http-status exc=HTTPStatus]"  # no code, no hint

    def test_probe_refusal_line_carries_the_rpc_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Boom:
            def get_tip_height(self) -> None:
                raise _rpc_refusal("tip-height request rejected by the server")

            def close(self) -> None:
                pass

        outputs: list[str] = []

        class _Out:
            def warning(self, line: str) -> None:
                outputs.append(line)

        monkeypatch.setattr(app_module, "BitcoindClient", lambda **kw: _Boom())
        assert _probe_chain_backend("bitcoind://h.example:8332", Settings(), output=_Out()) is None
        (line,) = outputs
        assert "class=rpc-error exc=RPCError code=-8" in line
        assert "h.example" not in line


# ---------------------------------------------------------------------------
# (3) The diag tool: shared client, DNS memo, new shapes mirrored.
# ---------------------------------------------------------------------------


class TestDiagTool:
    @pytest.fixture()
    def diag(self) -> Any:
        return _load_diag_module()

    def test_esplora_probe_accepts_the_user_payload(self, diag: Any) -> None:
        calls: list[str] = []

        def counting(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return _mempool_handler()(request)

        with httpx.Client(transport=httpx.MockTransport(counting)) as client:
            r = diag.probe_esplora("https://h.example", False, client)
        assert r["reachable"] is True
        assert r["mainnet"] is True
        assert r["api_root"] == "/api"
        assert calls == ["/blocks/tip", "/api/blocks/tip", "/api/blocks", "/api/blocks/0"]

    def test_esplora_probe_names_the_fallback(self, diag: Any) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/blocks":
                return httpx.Response(200, json=[])
            return _mempool_handler()(request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            r = diag.probe_esplora("https://h.example", False, client)
        assert not r["reachable"]
        assert "/blocks/tip=[] and /blocks" in r["error_class"]

    def test_bitcoind_probe_surfaces_rpc_error_code(self, diag: Any) -> None:
        body = json.dumps(
            {"result": None, "error": {"code": -8, "message": "Scan already in progress"}, "id": 1}
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text=body)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            r = diag.probe_bitcoind("bitcoind+tls://h.example:8332", None, None, False, client)
        assert r["error_class"] == "rpc-error"
        assert r["rpc_error_code"] == -8
        assert "already in progress" not in json.dumps(r)  # text never carried

    def test_bitcoind_probe_still_reports_mainnet_ok(self, diag: Any) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"result": {"chain": "main", "blocks": 1}, "error": None, "id": 1}
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            r = diag.probe_bitcoind("bitcoind://h.example", None, None, False, client)
        assert r["reachable"] is True
        assert r["mainnet"] is True

    def test_dns_memo_resolves_each_host_once(self, diag: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[Any, Any]] = []
        real = socket.getaddrinfo

        def slow(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
            calls.append((host, port))
            return real(host, port, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", slow)
        diag._install_dns_memo()  # wraps `slow`; monkeypatch restores `real` after
        first = socket.getaddrinfo("localhost", 56191, type=socket.SOCK_STREAM)
        second = socket.getaddrinfo("localhost", 65154, type=socket.SOCK_STREAM)
        assert len(calls) == 1  # ONE resolution for two ports
        assert {entry[4][1] for entry in first} == {56191}  # ports re-attached
        assert {entry[4][1] for entry in second} == {65154}
        assert [entry[:4] for entry in first] == [entry[:4] for entry in second]

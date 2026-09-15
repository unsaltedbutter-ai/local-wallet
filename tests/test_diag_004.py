"""TCK-DIAG-004: min-relay-fee viewer in tools/probe_backend_diag.py.

The ``--minrelay`` flag adds ONE value-free floor read per backend class to
the existing diagnostic probe:
  bitcoind : getmempoolinfo.minrelaytxfee → BTC/kvB + exact sat/kvB/sat/vB
             + Core's shipped default (policy.h) + the engine's EFFECTIVE
             floor (MAX with the assumed 1 sat/vB, labelled with TCK-FEE-005)
  electrum : server.features relayfee printed RAW if advertised (unit
             caveat), else "not advertised"
  mempool  : /v1/fees/recommended minimumFee (sat/vB, congestion NOT relay)

Without the flag every existing behavior is byte-identical (no ``minrelay``
key, no extra RPC, same shape).
"""
from __future__ import annotations

import importlib.util
import json
import socket
import ssl
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

_TOOLS = Path(__file__).resolve().parent.parent / "tools"
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

GENESIS = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"


def _load_diag_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "probe_backend_diag", _TOOLS / "probe_backend_diag.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rpc_handler(methods: dict[str, dict]) -> Any:
    """MockTransport handler routing RPC POSTs by the ``method`` field."""
    def handler(request: httpx.Request) -> httpx.Response:
        envelope = json.loads(request.content)
        method = envelope["method"]
        result = methods.get(method)
        if result is None:
            return httpx.Response(200, json={"result": None, "error": {"code": -32601}, "id": 1})
        return httpx.Response(200, json={"result": result, "error": None, "id": 1})
    return handler


@pytest.fixture()
def diag() -> Any:
    return _load_diag_module()


# ---------------------------------------------------------------- bitcoind


def test_bitcoind_minrelay_happy_path_renders_all_units(diag: Any) -> None:
    handler = _rpc_handler({
        "getblockchaininfo": {"chain": "main", "blocks": 1},
        "getmempoolinfo": {"minrelaytxfee": 0.00000100},  # Core default
    })
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        r = diag.probe_bitcoind("bitcoind://h.example:8332", None, None, False, client, True)
    m = r["minrelay"]
    assert r["reachable"] is True
    assert m["source"] == "bitcoind getmempoolinfo.minrelaytxfee"
    assert m["minrelaytxfee_btc_per_kvb"] == "0.000001"
    assert m["sat_per_kvb"] == "100"
    assert m["sat_per_vb"] == "0.1"
    core = m["core_default"]
    assert core["btc_per_kvb"] == "0.00000100"
    assert core["sat_per_kvb"] == "100"
    assert core["sat_per_vb"] == "0.1"
    assert "policy.h" in core["cite"]
    eff = m["engine_effective_floor"]
    # TCK-FEE-005 applied: node 0.1 (10c) == assumed rail 0.1 (10c) -> MAX 10.
    assert eff["centisat_per_vb"] == 10
    assert eff["sat_per_vb"] == "0.1"
    assert "TCK-FEE-005" in eff["note"]
    assert "0.1 sat/vB" in eff["note"]


def test_bitcoind_minrelay_effective_floor_is_max_of_advertised(diag: Any) -> None:
    # Node advertises ABOVE the assumed floor: effective floor must ride it.
    handler = _rpc_handler({
        "getblockchaininfo": {"chain": "main", "blocks": 1},
        "getmempoolinfo": {"minrelaytxfee": 0.00010000},  # 10 sat/vB = 1000 c
    })
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        r = diag.probe_bitcoind("bitcoind://h.example:8332", None, None, False, client, True)
    m = r["minrelay"]
    assert m["sat_per_vb"] == "10"
    assert m["engine_effective_floor"]["centisat_per_vb"] == 1000
    assert m["engine_effective_floor"]["sat_per_vb"] == "10"


def test_bitcoind_minrelay_error_is_value_free(diag: Any) -> None:
    # Server text (untrusted) never rides the minrelay dict.
    def handler(request: httpx.Request) -> httpx.Response:
        envelope = json.loads(request.content)
        if envelope["method"] == "getmempoolinfo":
            return httpx.Response(200, json={
                "result": None, "error": {"code": -8, "message": "top secret server text"}, "id": 1})
        return httpx.Response(200, json={"result": {"chain": "main"}, "error": None, "id": 1})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        r = diag.probe_bitcoind("bitcoind://h.example:8332", None, None, False, client, True)
    m = r["minrelay"]
    assert m["error_class"] == "rpc-error"
    assert "top secret" not in json.dumps(m)


# ----------------------------------------------------------------- electrum


def test_electrum_minrelay_advertised_prints_raw(diag: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = (json.dumps({"result": {"genesis_hash": GENESIS, "relayfee": 1000}}) + "\n").encode()
    _stub_electrum(diag, monkeypatch, payload)
    r = diag.probe_electrum("ssl://h.example:50002", False, True)
    m = r["minrelay"]
    assert r["mainnet"] is True
    assert m["advertised"] is True
    assert m["relayfee_raw"] == "1000"
    assert "unit-ambiguous" in m["unit_note"] and "sats/kB" in m["unit_note"]


def test_electrum_minrelay_not_advertised(diag: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = (json.dumps({"result": {"genesis_hash": GENESIS}}) + "\n").encode()
    _stub_electrum(diag, monkeypatch, payload)
    r = diag.probe_electrum("ssl://h.example:50002", False, True)
    m = r["minrelay"]
    assert m["advertised"] is False
    assert m["relayfee_raw"] is None
    assert "not advertised" in m["note"]


def test_electrum_minrelay_non_numeric_relayfee_treated_unadvertised(
        diag: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # A bool/non-numeric server-controlled relayfee must not be str()'d raw
    # (mirrors the bitcoind shape discipline); advertised=False instead.
    payload = (json.dumps({"result": {"genesis_hash": GENESIS, "relayfee": True}}) + "\n").encode()
    _stub_electrum(diag, monkeypatch, payload)
    r = diag.probe_electrum("ssl://h.example:50002", False, True)
    m = r["minrelay"]
    assert m["advertised"] is False
    assert m["relayfee_raw"] is None


# ------------------------------------------------------------------ mempool


def test_mempool_minimum_fee_labeled_not_relay(diag: Any) -> None:
    calls: list[str] = []
    tip_page = [{"id": GENESIS, "height": 1}]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if not path.startswith("/api"):
            return httpx.Response(404, text="<html>frontend</html>")
        api = path.removeprefix("/api")
        if api == "/blocks/tip":
            return httpx.Response(200, json=[])
        if api == "/blocks":
            return httpx.Response(200, json=tip_page)
        if api == "/blocks/0":
            return httpx.Response(200, json=[{"id": GENESIS, "height": 0}])
        if api == "/v1/fees/recommended":
            return httpx.Response(200, json={"fastestFee": 8, "minimumFee": 1})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        r = diag.probe_esplora("https://mempool.space", False, client, True)
    m = r["minrelay"]
    assert r["api_root"] == "/api"
    assert m["minimum_fee_sat_per_vb"] == "1"
    assert "NOT a relay floor" in m["label"]
    assert "/api/v1/fees/recommended" in calls


def test_mempool_bare_root_url_has_no_double_slash(diag: Any) -> None:
    # Bare-root-served Esplora latches api_root "/"; the mempool fee URL must
    # not become {base}//v1/fees/recommended (TCK-DIAG-004 review finding 1).
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path == "/blocks/tip":
            return httpx.Response(200, json=1)
        if path == "/blocks/0":
            return httpx.Response(200, json=[{"id": GENESIS, "height": 0}])
        if path == "/v1/fees/recommended":
            return httpx.Response(200, json={"minimumFee": 1})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        r = diag.probe_esplora("https://mempool.space", False, client, True)
    m = r["minrelay"]
    assert r["api_root"] == "/"
    assert m["minimum_fee_sat_per_vb"] == "1"
    assert "/v1/fees/recommended" in calls
    assert "//v1/fees/recommended" not in calls


# ------------------------------------------------------------ no-flag regression


def test_no_flag_produces_no_minrelay_key(diag: Any) -> None:
    handler = _rpc_handler({"getblockchaininfo": {"chain": "main", "blocks": 1}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        r = diag.probe_bitcoind("bitcoind://h.example:8332", None, None, False, client)
    assert "minrelay" not in r
    assert r["reachable"] is True and r["mainnet"] is True


def test_electrum_no_flag_produces_no_minrelay_key(
        diag: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Without --minrelay even a relayfee-bearing features response must not
    # populate a minrelay key (regression pin, finding 4).
    payload = (json.dumps({"result": {"genesis_hash": GENESIS, "relayfee": 1000}}) + "\n").encode()
    _stub_electrum(diag, monkeypatch, payload)
    r = diag.probe_electrum("ssl://h.example:50002", False)
    assert "minrelay" not in r
    assert r["reachable"] is True and r["mainnet"] is True


def _stub_electrum(diag: Any, monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
    class _FakeSock:
        def __init__(self) -> None:
            self._buf = payload

        def sendall(self, data: bytes) -> None:
            pass

        def recv(self, n: int) -> bytes:
            out, self._buf = self._buf, b""
            return out

        def close(self) -> None:
            pass

    def fake_create(addr: Any, timeout: Any = None) -> object:
        return object()

    def fake_wrap(sock: Any, server_hostname: Any = None) -> _FakeSock:
        return _FakeSock()

    monkeypatch.setattr(socket, "create_connection", fake_create)
    monkeypatch.setattr(ssl, "create_default_context",
                        lambda: SimpleNamespace(wrap_socket=fake_wrap))

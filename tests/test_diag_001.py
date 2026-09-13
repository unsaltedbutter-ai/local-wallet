"""TCK-DIAG-001: value-free failure-class debug lines.

Covers the chain/ classification helper and the app-side surfacing at the
probe/scan-failure sites. These lines carry ONLY a failure class, a probe
stage, a URL-class (scheme), and an exception class name — never a host,
credential, address, or amount.
"""
from __future__ import annotations

import socket
import ssl
from typing import Any

import httpx
import pytest

from localwallet import app as app_module
from localwallet.app import _probe_chain_backend
from localwallet.chain import classify_failure
from localwallet.chain.esplora import (
    CONNECT_REFUSED,
    NETWORK_ERROR,
    NOT_MAINNET,
    TLS_VERIFY_FAILURE,
    check_backend,
)
from localwallet.config import Settings


class _FakeOutput:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, line: str) -> None:
        self.warnings.append(line)


def _mt(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ------------------------------------------------------------- chain mapping


def test_classify_failure_maps_transport_classes() -> None:
    assert classify_failure(ssl.SSLCertVerificationError("cert")) == TLS_VERIFY_FAILURE
    assert classify_failure(TimeoutError("t")) == "timeout"
    assert classify_failure(ConnectionRefusedError("r")) == CONNECT_REFUSED
    assert classify_failure(socket.gaierror(-2, "nodename")) == "dns-resolution"
    # httpx nests the real cause deep in a wrapper — the walk must find it.
    wrapped = httpx.ConnectError("wrapped", request=None)
    wrapped.__cause__ = ConnectionResetError("reset")
    assert classify_failure(wrapped) == CONNECT_REFUSED
    assert classify_failure(ValueError("unrelated")) == NETWORK_ERROR


# ------------------------------------------------------- check_backend report


def test_check_backend_report_not_mainnet() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/blocks/tip"):
            return httpx.Response(200, json=100)
        return httpx.Response(200, json=[{"id": "1" * 64, "height": 0}])

    report: dict[str, str] = {}
    assert not check_backend("https://n.example/api", transport=_mt(handler), report=report)
    assert report["failure_class"] == NOT_MAINNET


def test_check_backend_report_transport_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused") from ConnectionRefusedError("refused")

    report: dict[str, str] = {}
    assert not check_backend("https://n.example/api", transport=_mt(handler), report=report)
    assert report["failure_class"] == CONNECT_REFUSED
    assert report["exc_name"]  # non-empty


def test_check_backend_report_malformed_url() -> None:
    report: dict[str, str] = {}
    assert not check_backend("https://user:pass@n.example", report=report)
    assert report["failure_class"] == NETWORK_ERROR


# ------------------------------------------------- probe emission (no network)


def test_probe_rejects_foreign_scheme_with_debug_line() -> None:
    out = _FakeOutput()
    settings = Settings.from_env()
    assert _probe_chain_backend("ftp://n.example", settings, output=out) is None
    assert len(out.warnings) == 1
    line = out.warnings[0]
    assert "backend probe rejected" in line
    # ftp:// is NOT a known dispatch scheme → clamped to the literal "unknown"
    assert "url-class=unknown" in line
    # value-free discipline: the host never rides the line
    assert "n.example" not in line


def test_probe_url_class_clamps_scheme_less_input() -> None:
    """A pasted bc1… address / IP:port / user:pass@host reaching the refusal
    line must never leak its characters (TCK-DIAG-001 security review)."""
    out = _FakeOutput()
    settings = Settings.from_env()
    for bad in (
        "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
        "192.168.1.5:8332",
        "user:pass@192.168.1.5",
    ):
        out.warnings.clear()
        assert _probe_chain_backend(bad, settings, output=out) is None
        assert out.warnings, f"expected a scheme-rejected refusal for {bad!r}"
        line = out.warnings[0]
        assert "url-class=unknown" in line
        assert "192.168.1.5" not in line
        assert "user" not in line and "pass" not in line
        assert "bc1q" not in line


def test_probe_refuses_http_without_esplora_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TCK-DESCOPE-M3B: an http(s) candidate gets the ONE Core-shape
    attempt; when it does not answer in Core shape it is REFUSED with a
    value-free debug line — there is no Esplora-shape fallback probe
    anymore (the app module no longer even imports ``check_backend``),
    so a mempool.space-style URL cannot be persisted through this seam."""
    out = _FakeOutput()
    settings = Settings.from_env()
    assert not hasattr(app_module, "check_backend")  # seam closed, not idle

    def dead_core(base_url: str = "", **_kw: Any) -> Any:
        raise RuntimeError("not a Core RPC here")

    monkeypatch.setattr(app_module, "BitcoindClient", dead_core)
    assert _probe_chain_backend("http://h.example", settings, output=out) is None
    assert len(out.warnings) == 1
    line = out.warnings[0]
    assert "stage=bitcoind-core" in line
    assert "class=network-error" in line
    assert "h.example" not in line  # value-free: no host


# ------------------------------------------------- ScanFlow failure suffix


def test_scanflow_warn_appends_failure_suffix() -> None:
    store = app_module.Store(None)
    wallet = store.create_wallet("default", "desc")
    worker = app_module.ChainWorker(None)
    flow = app_module.ScanFlow(store, wallet, worker, gap_limit=None)
    try:
        outputs: list[str] = []
        exc = app_module.ChainError(
            "utxo-scan request rejected by the server",
            failure_class="http-status",
            exc_name="HTTPStatus",
        )
        flow._warn(outputs.append, str(exc), exc)
        expected = (
            "warning: startup scan failed: utxo-scan request rejected by the "
            "server — continuing with cached state. [class=http-status "
            "exc=HTTPStatus]"
        )
        assert outputs == [expected]
    finally:
        worker.stop()

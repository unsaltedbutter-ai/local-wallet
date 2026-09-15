"""TCK-DIAG-003: broadcast send-failure diagnosability.

A failed broadcast POST emits ONE value-free debug line to the console +
launch log (the ``_Output.warning`` channel, never the transcript/SSE
narration): the TCK-DIAG-001 failure CLASS + the exception class name (plus
the DIAG-002 numeric RPC code when carried). The friendly transcript line is
byte-identical to the pre-ticket wording, the flow STAYS SIGNED (kept-for-
retry), and the line never carries the raw tx hex, a txid, or server text.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest

from localwallet import app
from localwallet.chain import ChainError
from localwallet.protocol import validate_payload
from localwallet.tx.flow import TxFlowStatus
from tests.test_tx_flow import drive_to_signed, make_flow

TX_HEX = "deadbeef" * 4  # fake signed-tx hex the extraction seam returns
TXID_OK = "ab" * 32


class _FakeOutput:
    """The DIAG-001 test double: collects ONLY the warning channel."""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, line: str) -> None:
        self.warnings.append(line)

    def __call__(self, line: str) -> None:  # narration (must never see debug)
        raise AssertionError(f"debug line rode the narration channel: {line!r}")


def _env(params: dict[str, Any]):
    return validate_payload(json.dumps({"v": 0, "intent": "broadcast_tx", "params": params}))


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch):
    """A real TxFlow driven to SIGNED (test_tx_flow's canonical staging) +
    the broadcast handler over a client whose ``broadcast_tx`` behavior each
    test sets. The hex-extraction seam is faked (the DIAG-001 pattern): this
    ticket is about the ERROR CLASS surfaced, not PSBT finalization — the
    real extract/sign/revalidate pipeline keeps its own AC pins."""
    monkeypatch.setattr(app, "_extract_signed_tx_hex", lambda _b64: TX_HEX)
    flow, _ticks = make_flow()
    signed = drive_to_signed(flow)
    from tests.test_e2e_skeleton import _fixture_parsed

    store = app.Store(None)
    wallet = store.create_wallet("default", "desc")
    # Post-LABELS-UNIFY the broadcast handler re-derives the change ADDRESS
    # (next_index-1, the sign-time recovery) to write label inheritance onto
    # — seed the derivation the staging shape implies (this fixture's point
    # is the FAILURE CLASS, not the inheritance; see test_coin_labels for
    # the full e2e inheritance ride).
    store.update_derivation(wallet.id, 1, next_index=1)
    state: dict[str, Any] = {}

    def _broadcast(_hex: str) -> str:
        if "exc" in state:
            raise state["exc"]
        return TXID_OK

    def _call(exc: Exception | None = None, *, output: Any = None):
        """Dispatch broadcast_tx once; ``exc`` is what the POST raises."""
        if exc is not None:
            state["exc"] = exc
        handler = app._make_broadcast_tx_handler(
            flow,
            SimpleNamespace(broadcast_tx=_broadcast),
            store,
            wallet.id,
            _fixture_parsed(),
            output=output,
        )
        return handler(_env({"tx_ref": signed.tx_ref}))

    return flow, _call


# ------------------------------------------------------------- the debug line


def test_send_failure_emits_class_and_exc(world) -> None:
    """The MW-9 report shape: a structured ChainError (the DIAG-001
    taxonomy fields the adapters attach) surfaces as the DIAG-001 line
    shape on the warning channel — console/log only."""
    flow, call = world
    out = _FakeOutput()
    exc = ChainError(
        "broadcast request rejected by the server",
        failure_class="rpc-error",
        exc_name="RPCError",
        rpc_code=-27,
    )
    result = call(exc, output=out)
    assert out.warnings == [
        "broadcast: send failed [class=rpc-error exc=RPCError code=-27]"
    ]
    assert result == {
        "error": "broadcast_failed",
        "detail": "broadcast request rejected by the server",
    }
    # SIGNED retention unchanged (kept-for-retry, the existing AC pin's core).
    assert flow.state is TxFlowStatus.SIGNED


def test_unstructured_error_classified_from_taxonomy(world) -> None:
    """A ChainError without the structured fields (or a plain HTTP-error
    flavor) still classifies: the cause-chain walk names the class, the
    raised type names the exception."""
    _flow, call = world
    out = _FakeOutput()
    exc = ChainError("broadcast request rejected by the server")
    exc.__cause__ = TimeoutError("read timeout")
    call(exc, output=out)
    assert out.warnings == ["broadcast: send failed [class=timeout exc=ChainError]"]


def test_value_free_no_hex_no_txid_no_server_text(world) -> None:
    """The line carries class + exception name ONLY — never the raw tx hex,
    never the txid, never the detail/server text (the transcript keeps the
    chain layer's scrubbed message; the debug channel adds classes)."""
    _flow, call = world
    out = _FakeOutput()
    call(
        ChainError(
            f"broadcast failed: tx {TXID_OK} rejected: min relay fee not met",
            failure_class="http-status",
            exc_name="HTTPStatus",
        ),
        output=out,
    )
    line = out.warnings[0]
    assert TX_HEX not in line
    assert TXID_OK not in line
    assert "min relay fee" not in line
    assert "rejected" not in line


def test_no_output_seam_emits_nothing_and_still_fails_cleanly(world) -> None:
    """``output=None`` (the test-seam / direct-call path, and every wiring
    that never had a router threaded): no crash, same result dict."""
    _flow, call = world
    result = call(
        ChainError("broadcast request rejected by the server", failure_class="timeout")
    )
    assert result["error"] == "broadcast_failed"


# ------------------------------------------------- the transcript line, intact


def test_friendly_line_byte_identical_and_never_the_debug_line(world) -> None:
    """The user-visible line is the exact MW-9 sentence (byte-identical),
    and the debug line reaches it ONLY through the warning channel."""
    _flow, call = world
    result = call(
        ChainError("broadcast request rejected by the server"), output=_FakeOutput()
    )
    lines: list[str] = []
    app._print_broadcast_tx(result, lines.append)
    assert lines == [
        (
            "Broadcast failed (broadcast request rejected by the server) — "
            "the signed transaction is kept; say 'broadcast' to retry."
        )
    ]


def test_success_path_emits_nothing_new(world) -> None:
    """A successful broadcast adds NO warning line (the debug companion is
    a failure-path-only emission); the flow lands at BROADCAST as before."""
    flow, call = world
    out = _FakeOutput()
    result = call(output=out)
    assert result["status"] == "broadcast"
    assert result["txid"] == TXID_OK
    assert out.warnings == []
    assert flow.state is TxFlowStatus.BROADCAST

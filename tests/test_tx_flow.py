"""Tests for the dispatcher-owned send-flow state machine and confirm gate
(TCK-P2-003 — the destructive-flow safety core, ADR-0013; Phase 3 SIGNED/
BROADCAST extension TCK-P3-004).

The suite pins:

1. **Flow transitions** — the full table from the flow module docstring:
   happy path IDLE→CREATED→CONFIRMED, create-while-pending refusal, wrong
   ``tx_ref`` refusal, cancel/reset/expiry transitions, and re-entry from
   every terminal state; plus the Phase 3 extension CONFIRMED→SIGNED→
   BROADCAST (matching-``tx_ref`` only, no skip paths — broadcast from
   CONFIRMED is refused, re-sign after SIGNED is refused, re-broadcast
   after BROADCAST is refused) and the exhaustive state×transition table.
2. **The core invariant** — ``confirm`` is refused when the same-turn gate
   decision is not CONFIRM, even with a matching ``tx_ref`` and a fresh
   pending transaction: an LLM "yes" never counts as user confirmation
   (PROJECT.md §8.6). Tested prominently and per non-confirm decision.
   Signing deliberately has NO utterance gate: the device interaction IS
   the user action (§9 trust anchor) — the flow still demands the
   CONFIRMED state plus a matching ``tx_ref``.
3. **Deterministic gate classification** — every whitelist entry, multi-
   token utterances, mixed signals → AMBIGUOUS, chat sentences →
   NOT_A_DECISION, case/punctuation insensitivity, and property tests
   (unknown-token soup never decides; classification is stable under
   case/punctuation variation; positive-evidence requirement).
4. **Purity determinism** — injectable id factory and clock; expiry
   boundary exactly at PENDING_TTL_S; no I/O anywhere (the flow is driven
   purely from test data).
"""

import random
import string
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, _SRC.as_posix())

from localwallet.tx import TxEngineError
from localwallet.tx.flow import (
    PENDING_TTL_S,
    ConfirmGate,
    FlowError,
    GateDecision,
    PendingTx,
    SignedTx,
    TxFlow,
    TxFlowStatus,
)

# ---------------------------------------------------------------- helpers

T0 = 1_700_000_000.0


def counting_ids(prefix: str = "ref") -> object:
    """Deterministic id factory: ref-1, ref-2, ... (ticket: counter-based)."""
    counter = {"n": 0}

    def factory() -> str:
        counter["n"] += 1
        return f"{prefix}-{counter['n']}"

    return factory


def make_flow(*, now: float = T0) -> tuple[TxFlow, list[float]]:
    """A flow with deterministic ids and a settable fake clock."""
    ticks: list[float] = [now]
    flow = TxFlow(id_factory=counting_ids(), clock=lambda: ticks[-1])
    return flow, ticks


def stage(flow: TxFlow, **overrides) -> PendingTx:
    """Stage a canonical pending transaction (fields overridable)."""
    fields: dict = {
        "amount_sats": 50_000,
        "recipient": "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx",
        "fee_target": "fast",
        "fee_rate_sat_vb": 2,
        "fee_sats": 282,
        "change_sats": 49_718,
        "psbt_base64": "cHNidP8BAFICAAAAAane",
        "inputs_count": 1,
        "vsize": 141,
    }
    fields.update(overrides)
    return flow.create(**fields)


# ------------------------------------------------------------ transitions

def test_ttl_constant_is_ten_minutes():
    assert PENDING_TTL_S == 600


def test_new_flow_starts_idle_with_no_pending():
    flow, _ = make_flow()
    assert flow.state is TxFlowStatus.IDLE
    assert flow.pending is None


def test_full_happy_path_idle_created_confirmed():
    flow, _ticks = make_flow()
    pending = stage(flow)
    assert flow.state is TxFlowStatus.CREATED
    assert flow.pending is pending
    assert pending.tx_ref == "ref-1"
    assert pending.created_at == T0

    confirmed = flow.confirm(pending.tx_ref, gate_decision=GateDecision.CONFIRM, at=T0 + 5)
    assert flow.state is TxFlowStatus.CONFIRMED
    assert confirmed is pending, "confirm returns the exact PendingTx the user confirmed"
    assert flow.pending is None
    # terminal → reset → IDLE
    flow.reset()
    assert flow.state is TxFlowStatus.IDLE


def test_pending_stamps_identity_from_injected_factories():
    flow, ticks = make_flow(now=T0)
    first = stage(flow)
    assert first.tx_ref == "ref-1" and first.created_at == T0
    flow.cancel()
    ticks[-1] = T0 + 30
    second = stage(flow)
    assert second.tx_ref == "ref-2", "id factory called once per create"
    assert second.created_at == T0 + 30, "clock read at create time"


def test_pending_tx_is_frozen():
    flow, _ = make_flow()
    pending = stage(flow)
    with pytest.raises(AttributeError):
        pending.amount_sats = 1  # type: ignore[misc]


def test_create_while_pending_is_refused_value_free():
    flow, _ = make_flow()
    first = stage(flow)
    with pytest.raises(FlowError, match="already pending"):
        stage(flow)
    assert flow.state is TxFlowStatus.CREATED
    assert flow.pending is first, "the original pending is untouched"


def test_confirm_wrong_tx_ref_refused_state_stays_created():
    flow, _ = make_flow()
    pending = stage(flow)
    with pytest.raises(FlowError, match="does not match"):
        flow.confirm("ref-999", gate_decision=GateDecision.CONFIRM, at=T0 + 5)
    assert flow.state is TxFlowStatus.CREATED
    assert flow.pending is pending


def test_confirm_without_gate_is_refused_even_with_matching_ref():
    """THE core invariant: the model envelope alone never moves the flow.

    For every non-CONFIRM gate decision — including a user utterance the
    gate could not classify — confirm is refused and the flow stays
    CREATED with the pending intact. Only CONFIRM on the same turn (plus a
    matching tx_ref) proceeds.
    """
    for decision in (GateDecision.DENY, GateDecision.AMBIGUOUS, GateDecision.NOT_A_DECISION):
        flow, _ = make_flow()
        pending = stage(flow)
        with pytest.raises(FlowError, match="gate"):
            flow.confirm(pending.tx_ref, gate_decision=decision, at=T0 + 1)
        assert flow.state is TxFlowStatus.CREATED
        assert flow.pending is pending
        # the same turn with the gate satisfied DOES confirm
        flow.confirm(pending.tx_ref, gate_decision=GateDecision.CONFIRM, at=T0 + 1)
        assert flow.state is TxFlowStatus.CONFIRMED


def test_confirm_from_non_created_states_is_refused():
    flow, _ = make_flow()
    with pytest.raises(FlowError, match="no pending transaction"):
        flow.confirm("ref-1", gate_decision=GateDecision.CONFIRM, at=T0)
    assert flow.state is TxFlowStatus.IDLE


def test_expiry_transitions_to_expired_and_frees_the_flow():
    flow, _ticks = make_flow()
    stage(flow)
    with pytest.raises(FlowError, match="expired"):
        flow.confirm("ref-1", gate_decision=GateDecision.CONFIRM, at=T0 + PENDING_TTL_S + 0.001)
    assert flow.state is TxFlowStatus.EXPIRED
    assert flow.pending is None
    # EXPIRED is terminal but re-entry via create is allowed
    pending = stage(flow)
    assert flow.state is TxFlowStatus.CREATED
    assert pending.tx_ref == "ref-2"


def test_expiry_boundary_exactly_at_ttl_is_still_valid():
    flow, _ = make_flow()
    stage(flow)
    confirmed = flow.confirm("ref-1", gate_decision=GateDecision.CONFIRM, at=T0 + PENDING_TTL_S)
    assert flow.state is TxFlowStatus.CONFIRMED
    assert confirmed is not None


def test_expiry_check_precedes_tx_ref_match_wrong_ref_after_ttl_expires():
    """Expiry is evaluated BEFORE tx_ref matching (precondition order).

    A wrong-``tx_ref`` confirm attempt that arrives AFTER the TTL has lapsed
    does NOT get a "does not match" refusal with the pending left intact —
    expiry fires first and the flow transitions CREATED → EXPIRED, dropping
    the pending. This fail-closed destruction is intended: a stale pending
    must never linger to be matched by a later, wrong reference; the
    documented precondition order (state → expiry → gate → tx_ref, ADR-0013)
    guarantees an expired transaction is destroyed regardless of what the
    envelope names.
    """
    flow, _ticks = make_flow()
    stage(flow)
    # wrong ref, but past TTL: the expiry check (precondition 2) wins over
    # the tx_ref match (precondition 4)
    with pytest.raises(FlowError, match="expired"):
        flow.confirm("ref-999", gate_decision=GateDecision.CONFIRM, at=T0 + PENDING_TTL_S + 1)
    assert flow.state is TxFlowStatus.EXPIRED
    assert flow.pending is None


def test_expired_then_confirm_refused_with_no_pending_message():
    """Once EXPIRED, confirm is an ordinary wrong-state refusal."""
    flow, _ = make_flow()
    stage(flow)
    with pytest.raises(FlowError, match="expired"):
        flow.confirm("ref-1", gate_decision=GateDecision.CONFIRM, at=T0 + PENDING_TTL_S + 1)
    with pytest.raises(FlowError, match="no pending transaction"):
        flow.confirm("ref-1", gate_decision=GateDecision.CONFIRM, at=T0 + PENDING_TTL_S + 2)
    assert flow.state is TxFlowStatus.EXPIRED


def test_cancel_from_created_then_create_again():
    flow, _ = make_flow()
    staged = stage(flow)
    cancelled = flow.cancel()
    assert cancelled is staged
    assert flow.state is TxFlowStatus.CANCELLED
    assert flow.pending is None
    # CANCELLED → create allowed
    again = stage(flow)
    assert flow.state is TxFlowStatus.CREATED
    assert again.tx_ref == "ref-2"
    # and the second pending can be cancelled too
    flow.cancel()
    assert flow.state is TxFlowStatus.CANCELLED


def test_cancel_from_non_created_states_is_refused():
    flow, _ = make_flow()
    with pytest.raises(FlowError, match="no pending transaction"):
        flow.cancel()
    assert flow.state is TxFlowStatus.IDLE
    stage(flow)
    flow.confirm("ref-1", gate_decision=GateDecision.CONFIRM, at=T0 + 1)
    with pytest.raises(FlowError, match="no pending transaction"):
        flow.cancel()
    assert flow.state is TxFlowStatus.CONFIRMED


def test_reset_from_every_terminal_state_returns_to_idle():
    # CONFIRMED → reset
    flow, _ = make_flow()
    stage(flow)
    flow.confirm("ref-1", gate_decision=GateDecision.CONFIRM, at=T0 + 1)
    flow.reset()
    assert flow.state is TxFlowStatus.IDLE and flow.pending is None
    # CANCELLED → reset
    flow2, _ = make_flow()
    stage(flow2)
    flow2.cancel()
    flow2.reset()
    assert flow2.state is TxFlowStatus.IDLE
    # EXPIRED → reset
    flow3, _ = make_flow()
    stage(flow3)
    with pytest.raises(FlowError, match="expired"):
        flow3.confirm("ref-1", gate_decision=GateDecision.CONFIRM, at=T0 + PENDING_TTL_S + 1)
    flow3.reset()
    assert flow3.state is TxFlowStatus.IDLE


def test_reset_from_idle_is_a_noop_and_from_created_is_refused():
    flow, _ = make_flow()
    flow.reset()
    assert flow.state is TxFlowStatus.IDLE
    stage(flow)
    with pytest.raises(FlowError, match="already pending"):
        flow.reset()
    assert flow.state is TxFlowStatus.CREATED, "reset must not abandon a live pending"


# --------------------------------------------- Phase 3: SIGNED / BROADCAST

SIGNED_PSBT = "cHNidP8BAFICAAAAAane-signed-payload"


def drive_to_confirmed(flow: TxFlow) -> PendingTx:
    """Stage + dual-key confirm; returns the confirmed record."""
    pending = stage(flow)
    return flow.confirm(pending.tx_ref, gate_decision=GateDecision.CONFIRM, at=T0 + 5)


def drive_to_signed(flow: TxFlow) -> SignedTx:
    """Stage + confirm + mark_signed; returns the signed record."""
    drive_to_confirmed(flow)
    return flow.mark_signed("ref-1", SIGNED_PSBT)


def test_full_lifecycle_idle_created_confirmed_signed_broadcast():
    flow, _ = make_flow()
    pending = drive_to_confirmed(flow)
    assert flow.state is TxFlowStatus.CONFIRMED
    assert flow.confirmed is pending, "CONFIRMED holds the approved tx"

    signed = flow.mark_signed(pending.tx_ref, SIGNED_PSBT)
    assert flow.state is TxFlowStatus.SIGNED
    assert flow.signed is signed
    assert signed.tx_ref == pending.tx_ref
    assert signed.psbt_base64 == SIGNED_PSBT
    assert flow.confirmed is pending, "the approved record is retained for re-validation"

    txid = "a" * 64
    recorded = flow.broadcast(pending.tx_ref, txid)
    assert flow.state is TxFlowStatus.BROADCAST
    assert recorded == txid
    assert flow.txid == txid
    assert flow.confirmed is pending and flow.signed is signed
    # BROADCAST is terminal → reset → IDLE, records cleared, fresh flow works
    flow.reset()
    assert flow.state is TxFlowStatus.IDLE
    assert flow.confirmed is None and flow.signed is None and flow.txid is None
    assert stage(flow).tx_ref == "ref-2"


def test_mark_signed_from_created_is_refused_no_skip_path():
    """Signing requires CONFIRMED: the CREATED state cannot skip the gate."""
    flow, _ = make_flow()
    pending = stage(flow)
    with pytest.raises(FlowError, match="no confirmed transaction"):
        flow.mark_signed(pending.tx_ref, SIGNED_PSBT)
    assert flow.state is TxFlowStatus.CREATED
    assert flow.pending is pending
    assert flow.signed is None


def test_mark_signed_wrong_tx_ref_refused_state_unchanged():
    flow, _ = make_flow()
    drive_to_confirmed(flow)
    with pytest.raises(FlowError, match="does not match"):
        flow.mark_signed("ref-999", SIGNED_PSBT)
    assert flow.state is TxFlowStatus.CONFIRMED
    assert flow.signed is None


def test_mark_signed_value_hygiene():
    flow, _ = make_flow()
    drive_to_confirmed(flow)
    for bad_psbt in ("", None, 123):
        with pytest.raises(FlowError, match="non-empty string"):
            flow.mark_signed("ref-1", bad_psbt)  # type: ignore[arg-type]
    assert flow.state is TxFlowStatus.CONFIRMED


def test_re_sign_after_signed_is_refused():
    """A signed record is immutable state: re-signing is refused."""
    flow, _ = make_flow()
    drive_to_signed(flow)
    with pytest.raises(FlowError, match="no confirmed transaction"):
        flow.mark_signed("ref-1", "cHNidP8-resigned")
    assert flow.state is TxFlowStatus.SIGNED
    assert flow.signed.psbt_base64 == SIGNED_PSBT, "the original record is untouched"


def test_mark_signed_from_non_confirmed_states_is_refused():
    # IDLE
    flow, _ = make_flow()
    with pytest.raises(FlowError, match="no confirmed transaction"):
        flow.mark_signed("ref-1", SIGNED_PSBT)
    # CANCELLED
    flow2, _ = make_flow()
    stage(flow2)
    flow2.cancel()
    with pytest.raises(FlowError, match="no confirmed transaction"):
        flow2.mark_signed("ref-1", SIGNED_PSBT)
    # EXPIRED
    flow3, _ = make_flow()
    stage(flow3)
    with pytest.raises(FlowError, match="expired"):
        flow3.confirm("ref-1", gate_decision=GateDecision.CONFIRM, at=T0 + PENDING_TTL_S + 1)
    with pytest.raises(FlowError, match="no confirmed transaction"):
        flow3.mark_signed("ref-1", SIGNED_PSBT)


def test_broadcast_from_confirmed_is_refused_no_skip_path():
    """THE no-skip invariant: broadcast from CONFIRMED is refused — an
    un-signed (hence un-revalidated) transaction must never reach the
    network; there is no path past the SIGNED state."""
    flow, _ = make_flow()
    pending = drive_to_confirmed(flow)
    with pytest.raises(FlowError, match="no signed transaction"):
        flow.broadcast(pending.tx_ref, "a" * 64)
    assert flow.state is TxFlowStatus.CONFIRMED
    assert flow.txid is None


def test_broadcast_wrong_tx_ref_and_value_hygiene_refused():
    flow, _ = make_flow()
    drive_to_signed(flow)
    with pytest.raises(FlowError, match="does not match"):
        flow.broadcast("ref-999", "a" * 64)
    for bad_txid in ("", None, 7):
        with pytest.raises(FlowError, match="non-empty string"):
            flow.broadcast("ref-1", bad_txid)  # type: ignore[arg-type]
    assert flow.state is TxFlowStatus.SIGNED
    assert flow.txid is None


def test_re_broadcast_after_broadcast_is_refused():
    flow, _ = make_flow()
    drive_to_signed(flow)
    flow.broadcast("ref-1", "a" * 64)
    with pytest.raises(FlowError, match="no signed transaction"):
        flow.broadcast("ref-1", "b" * 64)
    assert flow.state is TxFlowStatus.BROADCAST
    assert flow.txid == "a" * 64, "the recorded txid is untouched"


def test_broadcast_from_non_signed_states_is_refused():
    # IDLE
    flow, _ = make_flow()
    with pytest.raises(FlowError, match="no signed transaction"):
        flow.broadcast("ref-1", "a" * 64)
    # CREATED
    flow2, _ = make_flow()
    stage(flow2)
    with pytest.raises(FlowError, match="no signed transaction"):
        flow2.broadcast("ref-1", "a" * 64)


def test_cancel_after_confirm_is_refused_signed_means_committed():
    """cancel stays CREATED-only: past CONFIRMED the user's decision is
    committed, and signed means committed to signing — no chat-level undo."""
    flow, _ = make_flow()
    drive_to_confirmed(flow)
    with pytest.raises(FlowError, match="no pending transaction"):
        flow.cancel()
    assert flow.state is TxFlowStatus.CONFIRMED
    flow.mark_signed("ref-1", SIGNED_PSBT)
    with pytest.raises(FlowError, match="no pending transaction"):
        flow.cancel()
    assert flow.state is TxFlowStatus.SIGNED


def test_reset_from_signed_and_broadcast_returns_to_idle():
    # SIGNED → reset
    flow, _ = make_flow()
    drive_to_signed(flow)
    flow.reset()
    assert flow.state is TxFlowStatus.IDLE
    assert flow.confirmed is None and flow.signed is None and flow.txid is None
    # BROADCAST → reset
    flow2, _ = make_flow()
    drive_to_signed(flow2)
    flow2.broadcast("ref-1", "a" * 64)
    flow2.reset()
    assert flow2.state is TxFlowStatus.IDLE


def test_create_from_live_record_states_is_refused():
    """A second flow must never silently abandon a live record: create is
    refused from CONFIRMED/SIGNED/BROADCAST (explicit reset is the path)."""
    for setup in (drive_to_confirmed, drive_to_signed):
        flow, _ = make_flow()
        setup(flow)
        with pytest.raises(FlowError, match="already in flight"):
            stage(flow)
    flow3, _ = make_flow()
    drive_to_signed(flow3)
    flow3.broadcast("ref-1", "a" * 64)
    with pytest.raises(FlowError, match="already in flight"):
        stage(flow3)
    # explicit reset frees the flow
    flow3.reset()
    stage(flow3)
    assert flow3.state is TxFlowStatus.CREATED


def test_signed_record_is_frozen():
    flow, _ = make_flow()
    signed = drive_to_signed(flow)
    with pytest.raises(AttributeError):
        signed.psbt_base64 = "tampered"  # type: ignore[misc]


def test_signed_broadcast_errors_never_echo_refs_or_values():
    """Phase 3 refusal messages are value-free: no tx_ref, no PSBT, no txid.

    The flow's txid check is value hygiene only (non-empty string) — the
    charset authority is the chain layer, which re-validates the broadcast
    response as 64 lowercase hex before the flow ever sees it.
    """
    flow, _ = make_flow()
    drive_to_confirmed(flow)
    secret_ref = "DEADBEEF-SENTINEL-REF"
    with pytest.raises(FlowError) as wrong:
        flow.mark_signed(secret_ref, SIGNED_PSBT)
    assert secret_ref not in str(wrong.value)
    assert SIGNED_PSBT not in str(wrong.value)
    flow.mark_signed("ref-1", SIGNED_PSBT)
    # wrong ref at broadcast: refused, ref never echoed
    with pytest.raises(FlowError) as bad_state:
        flow.broadcast(secret_ref, "a" * 64)
    assert secret_ref not in str(bad_state.value)
    # a hostile txid *value* cannot leak via a state refusal either
    with pytest.raises(FlowError) as bad_state2:
        flow.broadcast(secret_ref, "DEADBEEF-SENTINEL-TXID")
    assert "DEADBEEF-SENTINEL-TXID" not in str(bad_state2.value)
    # success records the chain-reported txid verbatim
    recorded = flow.broadcast("ref-1", "a" * 64)
    assert recorded == "a" * 64
    assert flow.txid == "a" * 64


def test_flow_state_table_exhaustive():
    """Every state × every Phase-3-relevant transition, pinned to the
    docstring table (allowed → new state; refused → FlowError, unchanged).

    States are reached deterministically; each cell asserts the allowed
    transition's exact result state or the refusal's state preservation.
    """
    TXID = "a" * 64

    def fresh(state: TxFlowStatus) -> TxFlow:
        flow, _ = make_flow()
        if state is TxFlowStatus.CREATED:
            stage(flow)
        elif state is TxFlowStatus.CONFIRMED:
            drive_to_confirmed(flow)
        elif state is TxFlowStatus.SIGNED:
            drive_to_signed(flow)
        elif state is TxFlowStatus.BROADCAST:
            drive_to_signed(flow)
            flow.broadcast("ref-1", TXID)
        elif state is TxFlowStatus.CANCELLED:
            stage(flow)
            flow.cancel()
        elif state is TxFlowStatus.EXPIRED:
            stage(flow)
            with pytest.raises(FlowError):
                flow.confirm("ref-1", gate_decision=GateDecision.CONFIRM, at=T0 + PENDING_TTL_S + 1)
        assert flow.state is state
        return flow

    allowed_create = {TxFlowStatus.IDLE, TxFlowStatus.CANCELLED, TxFlowStatus.EXPIRED}
    for state in TxFlowStatus:
        # create
        flow = fresh(state)
        if state in allowed_create:
            stage(flow)
            assert flow.state is TxFlowStatus.CREATED
        else:
            with pytest.raises(FlowError):
                stage(flow)
            assert flow.state is state
        # confirm (needs CREATED)
        flow = fresh(state)
        if state is TxFlowStatus.CREATED:
            flow.confirm("ref-1", gate_decision=GateDecision.CONFIRM, at=T0 + 1)
            assert flow.state is TxFlowStatus.CONFIRMED
        else:
            with pytest.raises(FlowError):
                flow.confirm("ref-1", gate_decision=GateDecision.CONFIRM, at=T0 + 1)
            assert flow.state is state
        # mark_signed (needs CONFIRMED)
        flow = fresh(state)
        if state is TxFlowStatus.CONFIRMED:
            flow.mark_signed("ref-1", SIGNED_PSBT)
            assert flow.state is TxFlowStatus.SIGNED
        else:
            with pytest.raises(FlowError):
                flow.mark_signed("ref-1", SIGNED_PSBT)
            assert flow.state is state
        # broadcast (needs SIGNED)
        flow = fresh(state)
        if state is TxFlowStatus.SIGNED:
            flow.broadcast("ref-1", TXID)
            assert flow.state is TxFlowStatus.BROADCAST
        else:
            with pytest.raises(FlowError):
                flow.broadcast("ref-1", TXID)
            assert flow.state is state
        # cancel (needs CREATED)
        flow = fresh(state)
        if state is TxFlowStatus.CREATED:
            flow.cancel()
            assert flow.state is TxFlowStatus.CANCELLED
        else:
            with pytest.raises(FlowError):
                flow.cancel()
            assert flow.state is state
        # reset (everywhere but CREATED)
        flow = fresh(state)
        if state is TxFlowStatus.CREATED:
            with pytest.raises(FlowError):
                flow.reset()
            assert flow.state is TxFlowStatus.CREATED
        else:
            flow.reset()
            assert flow.state is TxFlowStatus.IDLE


def test_flow_error_is_a_tx_engine_error():
    assert issubclass(FlowError, TxEngineError)


def test_flow_errors_never_echo_refs_or_content():
    """Refusal messages are value-free: no tx_ref, no utterance content."""
    flow, _ = make_flow()
    pending = stage(flow)
    secret_ish_ref = "DEADBEEF-SENTINEL-REF"
    with pytest.raises(FlowError) as wrong:
        flow.confirm(secret_ish_ref, gate_decision=GateDecision.CONFIRM, at=T0 + 1)
    assert secret_ish_ref not in str(wrong.value)
    with pytest.raises(FlowError) as gate:
        flow.confirm(pending.tx_ref, gate_decision=GateDecision.AMBIGUOUS, at=T0 + 1)
    assert "AMBIGUOUS" not in str(gate.value)


def test_create_from_every_allowed_state():
    for pre, expected_ref in (("fresh", "ref-1"), ("after-cancel", "ref-2"), ("after-expiry", "ref-2")):
        flow, _ = make_flow()
        if pre == "after-cancel":
            stage(flow)
            flow.cancel()
        elif pre == "after-expiry":
            stage(flow)
            with pytest.raises(FlowError):
                flow.confirm("ref-1", gate_decision=GateDecision.CONFIRM, at=T0 + PENDING_TTL_S + 1)
        pending = stage(flow)
        assert flow.state is TxFlowStatus.CREATED
        assert pending.tx_ref == expected_ref


# ------------------------------------------------------- the confirm gate

CONFIRM_WHITELIST = [
    "yes", "y", "yes please", "confirm", "confirmed", "confirm it",
    "send it", "send", "approve", "approved", "do it",
]
DENY_WHITELIST = [
    "no", "n", "no thanks", "cancel", "cancel it", "abort", "stop",
    "don't", "dont", "deny", "reject",
]
FILLER = ["please", "the", "it", "tx", "transaction"]


@pytest.mark.parametrize("utterance", CONFIRM_WHITELIST)
def test_gate_confirms_every_confirm_whitelist_entry(utterance: str):
    assert ConfirmGate.classify(utterance) is GateDecision.CONFIRM


@pytest.mark.parametrize("utterance", DENY_WHITELIST)
def test_gate_denies_every_deny_whitelist_entry(utterance: str):
    assert ConfirmGate.classify(utterance) is GateDecision.DENY


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("yes send it", GateDecision.CONFIRM),           # ticket example
        ("confirm the tx", GateDecision.CONFIRM),        # confirm + fillers
        ("send the transaction", GateDecision.CONFIRM),  # confirm + fillers
        ("yes please", GateDecision.CONFIRM),            # multi-word whitelist via fillers
        ("y please", GateDecision.CONFIRM),
        ("approve the tx please", GateDecision.CONFIRM),
        ("cancel the tx", GateDecision.DENY),            # deny + fillers
        ("cancel it please", GateDecision.DENY),
        ("no the transaction", GateDecision.DENY),
        ("abort the tx", GateDecision.DENY),
        ("please confirm", GateDecision.CONFIRM),        # filler-first order
        ("the yes", GateDecision.CONFIRM),
    ],
)
def test_gate_multi_token_union_classification(utterance: str, expected: GateDecision):
    assert ConfirmGate.classify(utterance) is expected


@pytest.mark.parametrize(
    "utterance",
    [
        "yes no",
        "no yes",
        "y n",
        "confirm cancel",
        "yes cancel it",
        "stop yes it please",
        "send cancel",
        "approved reject",
    ],
)
def test_gate_mixed_signals_are_ambiguous(utterance: str):
    assert ConfirmGate.classify(utterance) is GateDecision.AMBIGUOUS


@pytest.mark.parametrize(
    "utterance",
    [
        # plain chat while a tx pends: the flow stays CREATED
        "what is my balance?",
        "how fast will this confirm?",
        "why is the fee so high?",
        # the ticket's reserved near-misses: NOT fuzzy-matched
        "ok",
        "Ok",
        "ok send",
        "ok cancel",
        # content beyond the whitelists: not a decision
        "yes bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx",
        "yes send it to my brother",
        "probably yes",
        "yes, but wait",
        "send 20 to my brother",
        # filler alone carries no decision (positive evidence required)
        "the tx",
        "please the transaction",
        "it it it",
        # punctuation-only / empty / whitespace
        "",
        "   ",
        "!!!",
        "?",
        ",,,",
    ],
)
def test_gate_chat_and_near_misses_are_not_decisions(utterance: str):
    assert ConfirmGate.classify(utterance) is GateDecision.NOT_A_DECISION


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("YES!", "yes"),
        ("Yes.", "yes"),
        ("  yEs  ", "yes"),
        ("YES, PLEASE.", "yes please"),
        ("Confirm It!", "confirm it"),
        ("NO!!", "no"),
        ("No, thanks.", "no thanks"),
        ("CANCEL THE TX.", "cancel the tx"),
        ("Don't!", "don't"),
        ('"send it"', "send it"),
    ],
)
def test_gate_is_case_and_punctuation_insensitive(raw: str, canonical: str):
    expected = ConfirmGate.classify(canonical)
    assert expected is not GateDecision.NOT_A_DECISION
    assert ConfirmGate.classify(raw) is expected


def test_gate_internal_punctuation_preserved_for_phrases():
    """Edge punctuation is stripped; internal characters are kept (don't)."""
    assert ConfirmGate._tokenize("  Don't!  ") == ("don't",)
    assert ConfirmGate._tokenize("yes, please.") == ("yes", "please")


# ----------------------------------------------------------- gate properties

def _rand_case(word: str, rng: random.Random) -> str:
    return "".join(
        ch.upper() if rng.random() < 0.5 else ch.lower() for ch in word
    )


def _decorate(utterance: str, rng: random.Random) -> str:
    """Random case + leading/trailing punctuation + messy spacing."""
    punct = "!.,?;:\"'"
    decorated = " ".join(_rand_case(w, rng) for w in utterance.split())
    if rng.random() < 0.7:
        decorated = rng.choice(punct) * rng.randrange(1, 3) + decorated
    if rng.random() < 0.7:
        decorated = decorated + rng.choice(punct) * rng.randrange(1, 3)
    if rng.random() < 0.5:
        decorated = "  " + decorated.replace(" ", "  ", 1)
    return decorated


def test_gate_property_whitelist_classification_is_stable_under_noise():
    """Every whitelisted phrase keeps its decision under random case,
    edge punctuation, and whitespace noise (seeded, deterministic)."""
    rng = random.Random(2026)
    for phrase, expected in [
        *[(p, GateDecision.CONFIRM) for p in CONFIRM_WHITELIST],
        *[(p, GateDecision.DENY) for p in DENY_WHITELIST],
    ]:
        for _ in range(20):
            assert ConfirmGate.classify(_decorate(phrase, rng)) is expected


def test_gate_property_unknown_soup_never_decides():
    """Utterances built ONLY from unknown words (+punctuation/whitespace)
    are never CONFIRM/DENY/AMBIGUOUS — no not-a-decision string is ever
    promoted to a decision.

    This is the fail-closed direction of the whitelist: a decision requires
    positive evidence (whitelisted tokens); unknown content can never
    manufacture one.
    """
    rng = random.Random(42)
    known = set(ConfirmGate.CONFIRM_TOKENS) | set(ConfirmGate.DENY_TOKENS) | set(ConfirmGate.FILLER_TOKENS)
    vocab = [
        w
        for w in ("ok", "maybe", "probably", "sure", "go", "ahead", "wallet", "balance",
                  "pleaseeee", "ye", "yeah", "nope", "wait", "later", "bc1q", "sats",
                  "banana", "ship", "now", "then", "why", "how", "much")
        if w not in known
    ]
    punct = string.punctuation
    for _ in range(300):
        n = rng.randrange(1, 6)
        words = [rng.choice(vocab) for _ in range(n)]
        utterance = " ".join(
            w + (rng.choice(punct) if rng.random() < 0.4 else "") for w in words
        )
        if rng.random() < 0.1:
            utterance = ""
        assert ConfirmGate.classify(utterance) is GateDecision.NOT_A_DECISION, utterance


def test_gate_property_positive_evidence_requirement():
    """Whatever the input, a CONFIRM/DENY verdict implies every token came
    from that decision's union (plus filler) — or the utterance was a
    whitelist phrase. Nothing else can produce a decision."""
    rng = random.Random(7)
    all_tokens = sorted(
        set(ConfirmGate.CONFIRM_TOKENS)
        | set(ConfirmGate.DENY_TOKENS)
        | set(ConfirmGate.FILLER_TOKENS)
        | {"ok", "maybe", "yeah", "nope", "go", "wallet", "banana"}
    )
    for _ in range(500):
        tokens = [rng.choice(all_tokens) for _ in range(rng.randrange(1, 5))]
        utterance = " ".join(tokens)
        verdict = ConfirmGate.classify(utterance)
        token_set = set(tokens)
        if verdict is GateDecision.CONFIRM:
            assert (
                " ".join(tokens) in ConfirmGate.CONFIRM_PHRASES
                or token_set <= ConfirmGate.CONFIRM_TOKENS | ConfirmGate.FILLER_TOKENS
            ), utterance
            assert not token_set & ConfirmGate.DENY_TOKENS or " ".join(
                tokens
            ) in ConfirmGate.CONFIRM_PHRASES, f"deny token leaked into CONFIRM: {utterance}"
        elif verdict is GateDecision.DENY:
            assert (
                " ".join(tokens) in ConfirmGate.DENY_PHRASES
                or token_set <= ConfirmGate.DENY_TOKENS | ConfirmGate.FILLER_TOKENS
            ), utterance
        elif verdict is GateDecision.AMBIGUOUS:
            assert token_set & ConfirmGate.CONFIRM_TOKENS and token_set & ConfirmGate.DENY_TOKENS, utterance
        else:
            assert verdict is GateDecision.NOT_A_DECISION


def test_gate_property_mixed_interleavings_are_ambiguous():
    """Any interleaving of >=1 confirm token, >=1 deny token, and fillers
    is AMBIGUOUS — order and repetition do not matter."""
    rng = random.Random(99)
    for _ in range(200):
        tokens = (
            [rng.choice(sorted(ConfirmGate.CONFIRM_TOKENS))]
            + [rng.choice(sorted(ConfirmGate.DENY_TOKENS))]
            + [rng.choice(sorted(ConfirmGate.FILLER_TOKENS)) for _ in range(rng.randrange(0, 3))]
        )
        rng.shuffle(tokens)
        assert ConfirmGate.classify(" ".join(tokens)) is GateDecision.AMBIGUOUS


def test_gate_property_deterministic():
    rng = random.Random(5)
    corpus = CONFIRM_WHITELIST + DENY_WHITELIST + ["ok send", "yes no", "what?", ""]
    for _ in range(100):
        utterance = rng.choice(corpus)
        first = ConfirmGate.classify(utterance)
        assert ConfirmGate.classify(utterance) is first
        assert ConfirmGate.classify(utterance.upper()) is first


def test_gate_property_confirm_union_shuffle_is_confirm():
    """Any shuffle of >=1 confirm token + fillers stays CONFIRM (the
    ticket's 'yes send it' rule generalized)."""
    rng = random.Random(11)
    confirm_pool = sorted(ConfirmGate.CONFIRM_TOKENS)
    filler_pool = sorted(ConfirmGate.FILLER_TOKENS)
    for _ in range(200):
        tokens = [rng.choice(confirm_pool)]  # positive evidence guaranteed
        tokens += [rng.choice(confirm_pool + filler_pool) for _ in range(rng.randrange(0, 4))]
        rng.shuffle(tokens)
        assert ConfirmGate.classify(" ".join(tokens)) is GateDecision.CONFIRM, " ".join(tokens)


def test_gate_property_deny_union_shuffle_is_deny():
    rng = random.Random(13)
    deny_pool = sorted(ConfirmGate.DENY_TOKENS)
    filler_pool = sorted(ConfirmGate.FILLER_TOKENS)
    for _ in range(200):
        tokens = [rng.choice(deny_pool)]  # positive evidence guaranteed
        tokens += [rng.choice(deny_pool + filler_pool) for _ in range(rng.randrange(0, 4))]
        rng.shuffle(tokens)
        assert ConfirmGate.classify(" ".join(tokens)) is GateDecision.DENY, " ".join(tokens)


def test_gate_tokens_and_phrases_match_the_adr():
    """The whitelists are pinned: a drift here is an ADR-0013 change."""
    assert ConfirmGate.CONFIRM_PHRASES == frozenset(CONFIRM_WHITELIST)
    assert ConfirmGate.DENY_PHRASES == frozenset(DENY_WHITELIST)
    assert ConfirmGate.CONFIRM_TOKENS == frozenset({"yes", "y", "confirm", "confirmed", "send", "approve", "approved"})
    assert ConfirmGate.DENY_TOKENS == frozenset({"no", "n", "cancel", "abort", "stop", "don't", "dont", "deny", "reject"})
    assert ConfirmGate.FILLER_TOKENS == frozenset(FILLER)

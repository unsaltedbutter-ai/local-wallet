"""TCK-RBF-004 — the app bump-fee conversation (bump_fee handler wiring).

One test per beat of the ticket's done-when list, riding the REAL wiring
(the production dispatch table, mock chain, test-vector fake device
signer — reused from the e2e harness, no network, deterministic):

* target resolution through the PINNED RBF-005 resolver (single
  assume-and-name / multi indexed ask, never a guess / honest empty);
* the funding ask (change-first, smallest/mid/largest chooser over
  CONFIRMED coins ONLY, unconfirmed never offered nor used, the
  floor-unreachable refusal carrying the sanctioned floor number);
* the plan card (Replaces + Fee-delta rows verbatim from the builder);
* the FULL TxFlow ride (dual-key confirm → sign revalidation → single-
  POST broadcast) with lineage written ONLY on broadcast success via the
  sanctioned ``record_replacement`` (FLOW-REQUOTE commit-only-on-success);
* narration (supersede line; the mid-conversation confirmation recheck);
* the deterministic conversation machinery (label words never reach the
  model, any-next-utterance closes an open ask — never-trap, post-bump
  "faster"/"slower" reroute to a NEW bump, not create_tx);
* the TCK-CPFP-001 rider guard (cpfp mode refuses at step 0.5, value-free,
  zero fee-estimator calls) and the RBF-005 review riders' pins.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.protocol import IntentName, validate_payload
from localwallet.store import DIR_OUT, AddressRecord, TxRecord, UtxoRecord, superseded_states
from localwallet.tx.flow import GateDecision, TxFlowStatus
from localwallet.tx.replacement import rbf_min_fee_sats
from localwallet.tx.selection import estimate_tx_vsize
from tests.test_e2e_skeleton import (
    SEND_RECIPIENT,
    _fixture_parsed,
    derive_fixture_addresses,
)
from tests.test_tx_self_transfer import _hwi_table, _utxo

#: The canonical fixture send's recorded fee at the slow rung (1 sat/vB ×
#: the pinned 141 vB estimate) and at medium/fast — arithmetic the bump
#: card's delta must match field-for-field.
SLOW_FEE: Final[int] = 141
MEDIUM_FEE: Final[int] = 282
FAST_FEE: Final[int] = 423  # 3 sat/vB (SEND_FEES_PAYLOAD fastestFee)

HEX64_OTHER = "f" * 64  # a store-known in-flight row the flow cannot rebuild


# ------------------------------------------------------------------ helpers


def _env(intent: str, params: dict[str, Any]):
    return validate_payload(json.dumps({"v": 0, "intent": intent, "params": params}))


def _bump(params: dict[str, Any]):
    return _env("bump_fee", params)


def _ride(table: dict, session, flow, *, fee: str = "medium", amount: int = 60_000) -> str:
    """Drive the canonical create→confirm→sign→broadcast send and return
    the broadcast txid (the in-flight ORIGINAL a later bump replaces)."""
    res = table[IntentName.CREATE_TX](
        _env("create_tx", {"recipient": SEND_RECIPIENT, "amount_sats": amount, "fee_target": fee})
    )
    assert "tx_ref" in res, res
    ref = res["tx_ref"]
    session.gate_decision = GateDecision.CONFIRM
    assert table[IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": ref}))["status"] == "confirmed"
    assert table[IntentName.SIGN_TX](_env("sign_tx", {"tx_ref": ref}))["status"] == "signed"
    b = table[IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": ref}))
    assert b["status"] == "broadcast"
    assert flow.state is TxFlowStatus.BROADCAST
    return str(b["txid"])


def _add_coin(store, wallet_id: int, index: int, txid: str, value: int, *, confirmed: int = 1) -> None:
    """Plant one wallet coin (address + derivation + utxo rows) the way a
    post-broadcast RESCAN would — the candidate set the funding chooser
    reads. ``confirmed=0`` mimics the original's own pending change coin."""
    address = derive_fixture_addresses(index + 1)[index]
    store.upsert_batch(
        [
            AddressRecord(
                wallet_id=wallet_id,
                branch=0,
                index=index,
                address=address,
                script_type=_fixture_parsed().script_type,
                status="used",
            )
        ]
    )
    rows = store.get_utxos_for_wallet(wallet_id)
    rows.append(
        UtxoRecord(
            wallet_id=wallet_id,
            txid=txid,
            vout=0,
            address=address,
            value_sats=value,
            confirmed=confirmed,
            height=None,
        )
    )
    store.replace_utxos_for_wallet(wallet_id, rows)


class _FakeGen:
    """Records every prompt the model receives; answers with a harmless
    respond envelope (so any NON-intercepted turn is visible in prompts)."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, prompt: str, grammar_text: str | None) -> str:
        del grammar_text
        self.prompts.append(prompt)
        return json.dumps({"v": 0, "intent": "respond", "params": {"text": "ok."}})


@pytest.fixture()
def world():
    """HWI-signed canonical world: one confirmed 100_000-sat coin."""
    addrs = derive_fixture_addresses(8)
    state: dict[str, Any] = {}
    table, store, wallet, client, recorded, flow, session, signer = _hwi_table(
        {addrs[0]: [_utxo("d" * 64, 0, 100_000)]}, state=state
    )
    yield {
        "table": table, "store": store, "wallet": wallet, "client": client,
        "recorded": recorded, "flow": flow, "session": session, "signer": signer,
        "state": state,
    }
    store.close()
    client.close()


# =========================================================================
# 1. Target resolution — the pinned resolver consumed EXACTLY as shipped
# =========================================================================


def test_bump_fee_handler_wired(world) -> None:
    """The RBF-003 not-wired stub is GONE: dispatch answers (never raises)."""
    assert IntentName.BUMP_FEE in world["table"]
    assert not hasattr(app, "_bump_fee_not_wired")


def test_resolver_consumed_verbatim(world, monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler resolves through ``_resolve_in_flight_outgoing`` — the
    pinned RBF-005 API — and NOTHING re-derives the in-flight set."""
    calls: list[str] = []
    real = app._resolve_in_flight_outgoing

    def spy(records, *, now=None):
        calls.append("call")
        return real(records, now=now)

    monkeypatch.setattr(app, "_resolve_in_flight_outgoing", spy)
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    result = world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))
    assert result.get("error") is None
    assert calls  # the sanctioned resolver ran for this dispatch


def test_resolve_txid_resolves_directly(world) -> None:
    """Deliverable 1: "increase the fee on <txid>" — the hex target names
    the in-flight row and proceeds."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    result = world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))
    assert result.get("replaces") == txid
    assert result.get("bump_mode") == "change_trim"


def test_resolve_single_assume_and_name(world) -> None:
    """A non-txid reference with EXACTLY one in-flight transaction →
    assume-and-name-it (the resolver's single semantics): the result
    carries that txid verbatim."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    result = world["table"][IntentName.BUMP_FEE](_bump({"target": "the one I sent"}))
    assert result.get("replaces") == txid
    assert world["flow"].state is TxFlowStatus.CREATED


def test_resolve_none_is_honest_empty_zero_chain(world) -> None:
    """Nothing in flight (fresh wallet) → the honest empty answer, and it
    made ZERO network calls (no chain to consult for a bare refusal)."""
    result = world["table"][IntentName.BUMP_FEE](_bump({"target": "a" * 64}))
    assert result["error"] == "bump_nothing_in_flight"
    assert result["detail"] == app._BUMP_NOTHING_IN_FLIGHT
    assert world["recorded"] == []
    result2 = world["table"][IntentName.BUMP_FEE](_bump({"target": "bump it"}))
    assert result2["error"] == "bump_nothing_in_flight"


def test_resolve_unknown_txid_no_row_honest(world) -> None:
    """A well-formed txid the wallet never broadcast is not fabricated
    into anything: the honest empty refusal, value-free (never echoed)."""
    result = world["table"][IntentName.BUMP_FEE](_bump({"target": "c" * 64}))
    assert result["error"] == "bump_nothing_in_flight"
    assert "c" * 64 not in json.dumps(result)


def test_resolve_multi_indexed_ask_never_guesses(world) -> None:
    """≥2 in-flight + a reference that names neither → the indexed choice
    ask. NOTHING stages, nothing guesses; the entries ride the resolver's
    verbatim fields."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    world["store"].upsert_txs(
        [
            TxRecord(
                wallet_id=world["wallet"].id,
                txid=HEX64_OTHER,
                height=None,
                block_time=None,
                fee_sats=300,
                direction=DIR_OUT,
                raw_summary=None,
                amount_sats=5_000,
                fee_rate_centisat_vb=150,
                first_seen=1_700_000_000,
                replaced_by_txid=None,
            )
        ]
    )
    result = world["table"][IntentName.BUMP_FEE](_bump({"target": "bump my transaction"}))
    assert result.get("ask") == "target"
    assert [o["txid"] for o in result["options"]] == sorted([txid, HEX64_OTHER])  # store order
    assert world["flow"].state is TxFlowStatus.BROADCAST  # NOTHING staged
    assert world["session"].bump_ask is not None
    lines: list[str] = []
    app._print_bump_fee(result, lines.append)
    assert any("2 transactions in flight" in line for line in lines)


def test_target_ask_number_answer_routes_verbatim(world) -> None:
    """The ask closes through the deterministic intercept: "1" builds a
    CODE-owned envelope quoting the chosen txid verbatim from the ask."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    world["store"].upsert_txs(
        [
            TxRecord(
                wallet_id=world["wallet"].id, txid=HEX64_OTHER, height=None,
                block_time=None, fee_sats=300, direction=DIR_OUT, raw_summary=None,
                amount_sats=5_000, fee_rate_centisat_vb=150, first_seen=1_700_000_000,
                replaced_by_txid=None,
            )
        ]
    )
    fake = _FakeGen()
    loop = AgentLoop(fake, world["table"])
    outputs: list[str] = []
    ask_result = world["table"][IntentName.BUMP_FEE](_bump({"target": "bump it"}))
    assert [o["txid"] for o in ask_result["options"]] == sorted([txid, HEX64_OTHER])
    # Answer so that the choice lands on the flow-held row (the ONE that
    # can rebuild) — the indexed answer routes by POSITION, code quotes
    # the txid verbatim from the ask.
    answer = ask_result["options"].index(
        next(o for o in ask_result["options"] if o["txid"] == txid)
    ) + 1
    app._run_turn(
        loop, world["flow"], world["session"], str(answer), outputs.append,
        table=world["table"],
    )
    assert fake.prompts == []  # the answer never reached the model
    assert world["flow"].state is TxFlowStatus.CREATED  # the chosen one staged
    assert world["session"].bump_pending.old_txid == txid
    # A DIFFERENT in-flight row the flow cannot rebuild is refused
    # honestly (covered standalone in test_unrecorded_target_honest).
    other = 2 if answer == 1 else 1
    world["flow"].cancel()  # retire the staged bump, return to idle-ish
    world["session"].bump_pending = None
    world["session"].bump_ask = app._BumpAsk(
        kind="target", old_txid="", entries=ask_result["options"]
    )
    outputs2: list[str] = []
    app._run_turn(
        loop, world["flow"], world["session"], str(other), outputs2.append,
        table=world["table"],
    )
    assert any("rebuild" in line for line in outputs2)  # the honest line
    assert fake.prompts == []  # still never the model


# =========================================================================
# 2. Funding (BIP-125 rule 2) — change first, confirmed coins only
# =========================================================================


def test_change_first_hit_skips_the_ask(world) -> None:
    """Deliverable 2 + 3: the old tx's change reaches the floor → the plan
    is staged with NO funding ask (change_trim), card exact."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    result = world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))
    assert result.get("ask") is None and result.get("error") is None
    assert result["bump_mode"] == "change_trim"
    # Engine-computed delta, not a ladder rung alone: 141 → 423 paying 282.
    assert result["old_fee_sats"] == SLOW_FEE
    assert result["fee_sats"] == FAST_FEE
    assert result["fee_delta_sats"] == FAST_FEE - SLOW_FEE
    lines: list[str] = []
    app._print_bump_fee(result, lines.append, session=world["session"])
    assert (
        f"Replaces: {txid} — the original may still confirm; only one of "
        "these two ever will" in lines
    )
    assert "Fee: 141 sats → 423 sats (paying 282 sats extra)" in lines
    assert lines[0] == 'Pending — say "sign" to review it on your device, or "cancel" to discard.'


def test_funding_ask_offers_confirmed_only(world) -> None:
    """Change falls short (medium original bumped at fast) → the chooser
    over CONFIRMED coins only: the unconfirmed coin (the original's own
    change shape) is never offered; the original's spent input is excluded."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="medium")
    _add_coin(world["store"], world["wallet"].id, 4, "e" * 64, 10_000)
    _add_coin(world["store"], world["wallet"].id, 5, "7" * 64, 40_000, confirmed=0)
    result = world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))
    assert result.get("ask") == "funding"
    values = [option["value_sats"] for option in result["options"]]
    assert values == [10_000]  # the 40k unconfirmed coin is NOT offered
    # The original's own input coin (100k, still a stale snapshot row) is
    # excluded too — it is being re-spent by the original.
    assert 100_000 not in values
    assert world["session"].bump_ask.kind == "funding"
    assert world["session"].bump_ask.old_txid == txid


def test_funding_ask_framing_small_mid_large_with_labels(world) -> None:
    """Three+ confirmed coins → smallest / mid / largest framing; the
    user's stored labels print VERBATIM on the terminal card."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="medium")
    _add_coin(world["store"], world["wallet"].id, 4, "a" * 64, 10_000)
    _add_coin(world["store"], world["wallet"].id, 5, "b" * 64, 30_000)
    _add_coin(world["store"], world["wallet"].id, 6, "c" * 64, 50_000)
    world["store"].add_address_labels(derive_fixture_addresses(8)[6], ["exchange"])
    result = world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))
    assert [option["framing"] for option in result["options"]] == [
        "smallest", "mid", "largest",
    ]
    lines: list[str] = []
    app._print_bump_fee(result, lines.append)
    card = "\n".join(lines)
    assert "largest — 50,000 sats · labeled 'exchange'" in card
    assert "smallest — 10,000 sats" in card


def test_funding_ref_framing_word_first_call(world) -> None:
    """An explicit self-describing reference ("largest") skips the ask
    entirely — same call, staged plan."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="medium")
    _add_coin(world["store"], world["wallet"].id, 4, "a" * 64, 10_000)
    _add_coin(world["store"], world["wallet"].id, 5, "b" * 64, 50_000)
    result = world["table"][IntentName.BUMP_FEE](_bump({"target": txid, "funding_ref": "largest"}))
    assert result.get("error") is None
    assert result["bump_mode"] == "add_input"
    assert result["inputs_count"] == 2


def test_funding_ref_number_resolves_against_open_ask(world) -> None:
    """The intercept's code-built digit reference resolves against the
    OPEN ask (offered order), never against a re-guessed set."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="medium")
    _add_coin(world["store"], world["wallet"].id, 4, "a" * 64, 10_000)
    _add_coin(world["store"], world["wallet"].id, 5, "b" * 64, 50_000)
    ask = world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))
    assert ask.get("ask") == "funding"
    result = world["table"][IntentName.BUMP_FEE](_bump({"target": txid, "funding_ref": "2"}))
    assert result.get("error") is None
    assert result["bump_mode"] == "add_input"
    assert world["session"].bump_ask is None  # consumed


def test_funding_ask_persists_the_explicit_rate_knob(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review FINDING 1 (MAJOR): the user's fee knob survives an ask
    intercept. Turn 1 states 10 sat/vB; the change alone cannot reach the
    BIP-125 floor that fast, so the funding ask opens carrying the EXPLICIT
    rate. Answering "1" must re-stage at that rate — byte-checked against
    the builder input — NOT silently fall back to the FAST default (which
    here is a below-floor 3 sat/vB, the very rate the finding repro refused
    with 'at least 846 sats')."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="fast", amount=99_000)
    _add_coin(world["store"], world["wallet"].id, 4, "a" * 64, 30_000)
    ask = world["table"][IntentName.BUMP_FEE](_bump({"target": txid, "fee_rate_sat_vb": 10}))
    assert ask.get("ask") == "funding"  # change short of the floor at 10 sat/vB
    assert world["session"].bump_ask.fee_rate_sat_vb == 10  # the knob rode the ask
    assert world["session"].bump_ask.fee_target is None

    builder_rates: list[int] = []
    real_builder = app.build_replacement_plan

    def spy(original_tx, rate_c, funding_coin=None):  # type: ignore[no-untyped-def]
        builder_rates.append(rate_c)
        return real_builder(original_tx, rate_c, funding_coin=funding_coin)

    monkeypatch.setattr(app, "build_replacement_plan", spy)
    fake = _FakeGen()
    loop = AgentLoop(fake, world["table"])
    app._run_turn(loop, world["flow"], world["session"], "1", [].append, table=world["table"])
    assert fake.prompts == []  # the answer never reached the model
    assert world["flow"].state is TxFlowStatus.CREATED  # it staged (no floor refusal)
    # Byte-check against the builder input: EVERY replacement-plan call on
    # the answer turn ran at the user's explicit 10 sat/vB (1000 csat/vB),
    # never the FAST default (300 csat/vB).
    assert builder_rates and all(r == 1000 for r in builder_rates), builder_rates
    assert world["flow"].pending is not None
    assert world["flow"].pending.fee_rate_centisat_vb == 1000


def test_target_ask_persists_the_rung_knob(world) -> None:
    """Review FINDING 1 (the target-ask half): an explicit RUNGED ask
    answer re-quotes the rung, not the FAST default. Two in-flight
    transactions + "bump it slower" → indexed ask; a number answer
    re-dispatches at the stated rung (medium), byte-checked on the code-built
    envelope the intercept hands the handler."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    world["store"].upsert_txs(
        [
            TxRecord(
                wallet_id=world["wallet"].id, txid=HEX64_OTHER, height=None,
                block_time=None, fee_sats=300, direction=DIR_OUT, raw_summary=None,
                amount_sats=5_000, fee_rate_centisat_vb=150, first_seen=1_700_000_000,
                replaced_by_txid=None,
            )
        ]
    )
    ask = world["table"][IntentName.BUMP_FEE](_bump({"target": "bump it", "fee_target": "medium"}))
    assert ask.get("ask") == "target"
    assert world["session"].bump_ask.fee_target == "medium"
    assert world["session"].bump_ask.fee_rate_sat_vb is None
    # Answer so the choice lands on the flow-held row (the one that rebuilds).
    answer = ask["options"].index(next(o for o in ask["options"] if o["txid"] == txid)) + 1
    dispatched: list[dict] = []
    real = world["table"][IntentName.BUMP_FEE]
    world["table"][IntentName.BUMP_FEE] = lambda e: (dispatched.append(dict(e.params)), real(e))[1]
    try:
        fake = _FakeGen()
        loop = AgentLoop(fake, world["table"])
        app._run_turn(
            loop, world["flow"], world["session"], str(answer), [].append,
            table=world["table"],
        )
    finally:
        world["table"][IntentName.BUMP_FEE] = real
    assert fake.prompts == []
    # The intercept re-quoted the rung verbatim (no silent FAST fallback).
    assert dispatched == [{"target": txid, "funding_ref": None, "fee_target": "medium", "fee_rate_sat_vb": None}]
    assert world["flow"].state is TxFlowStatus.CREATED
    assert world["flow"].pending is not None and world["flow"].pending.fee_target == "medium"


def test_funding_ref_unmatched_refuses_honestly(world) -> None:
    ask = None
    txid = _ride(world["table"], world["session"], world["flow"], fee="medium")
    _add_coin(world["store"], world["wallet"].id, 4, "a" * 64, 10_000)
    ask = world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))
    assert ask.get("ask") == "funding"
    result = world["table"][IntentName.BUMP_FEE](_bump({"target": txid, "funding_ref": "9"}))
    assert result["error"] == "bump_funding_ref"
    assert world["flow"].state is TxFlowStatus.BROADCAST


def test_floor_unreachable_refusal_carries_floor_number(world) -> None:
    """No confirmed funding can reach the floor → refusal carrying the
    SANCTIONED floor number (the builder's own math, structured keys)."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="medium", amount=99_700)
    _add_coin(world["store"], world["wallet"].id, 4, "a" * 64, 100)
    result = world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))
    assert result["error"] == "bump_floor_unreachable"
    assert result["reason"] == "funding_below_floor"
    expected = rbf_min_fee_sats(300, estimate_tx_vsize(2, [_recip_script()], None))
    assert result["floor_sats"] == expected
    lines: list[str] = []
    app._print_bump_fee(result, lines.append)
    assert f"at least {expected:,} sats" in lines[0]


def test_rate_below_floor_refusal_carries_floor_number(world) -> None:
    """An explicit too-low rate refuses with the floor number (glm MUST 4:
    the user/chosen rate below the floor is refused with cause) — no ask,
    no staging, zero coin offers."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    result = world["table"][IntentName.BUMP_FEE](_bump({"target": txid, "fee_rate_sat_vb": 1}))
    assert result["error"] == "bump_floor_unreachable"
    assert result["reason"] == "rate_below_floor"
    assert result["floor_sats"] >= 2 * SLOW_FEE
    assert world["flow"].state is TxFlowStatus.BROADCAST


def test_explicit_rate_knob_stages_and_records_no_rung(world) -> None:
    """``fee_rate_sat_vb`` is taken VERBATIM (×100 at this edge): the
    record carries no rung and no fabricated ETA (create_tx precedent)."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    result = world["table"][IntentName.BUMP_FEE](_bump({"target": txid, "fee_rate_sat_vb": 6}))
    assert result.get("error") is None
    assert result["fee_rate_centisat_vb"] == 600
    assert result["fee_target"] is None
    assert "eta_wording" not in result
    assert world["flow"].pending.fee_rate_centisat_vb == 600


def _recip_script() -> bytes:
    from embit.script import address_to_scriptpubkey

    return bytes(address_to_scriptpubkey(SEND_RECIPIENT).data)


# =========================================================================
# 3. The full TxFlow ride + lineage commit-only-on-success
# =========================================================================


def test_full_ride_dual_key_unchanged(world) -> None:
    """The replacement rides create→confirm→sign→broadcast: an LLM-relayed
    confirm without the same-turn gate is REFUSED; the dual key passes;
    sign-time revalidation independently re-derives the replacement."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    bump = world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))
    ref = bump["tx_ref"]
    assert world["flow"].state is TxFlowStatus.CREATED
    world["session"].gate_decision = GateDecision.NOT_A_DECISION
    refused = world["table"][IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": ref}))
    assert refused["error"] == "confirm_refused"
    world["session"].gate_decision = GateDecision.CONFIRM
    assert world["table"][IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": ref}))["status"] == "confirmed"
    signed = world["table"][IntentName.SIGN_TX](_env("sign_tx", {"tx_ref": ref}))
    assert signed["status"] == "signed"  # revalidation passed on the replacement
    assert world["flow"].state is TxFlowStatus.SIGNED


def test_lineage_written_only_on_broadcast_success(world) -> None:
    """Deliverable 4: a FAILED broadcast touches no lineage (the pending
    marker survives); the retry that SUCCEEDS writes through the
    sanctioned ``record_replacement`` — commit-only-on-success."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    bump = world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))
    ref = bump["tx_ref"]
    world["session"].gate_decision = GateDecision.CONFIRM
    world["table"][IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": ref}))
    world["table"][IntentName.SIGN_TX](_env("sign_tx", {"tx_ref": ref}))
    original_row = next(
        r for r in world["store"].get_txs_for_wallet(world["wallet"].id) if r.txid == txid
    )
    assert original_row.replaced_by_txid is None
    world["state"]["broadcast_fail"] = True
    failed = world["table"][IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": ref}))
    assert failed["error"] == "broadcast_failed"
    assert world["session"].bump_pending is not None  # still the staged bump
    assert original_row.replaced_by_txid is None
    world["state"]["broadcast_fail"] = False
    b2 = world["table"][IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": ref}))
    assert b2["status"] == "broadcast"
    assert b2["replaces_txid"] == txid
    assert world["session"].bump_pending is None
    assert world["session"].bump_bcast_txid == b2["txid"]
    row = next(r for r in world["store"].get_txs_for_wallet(world["wallet"].id) if r.txid == txid)
    assert row.replaced_by_txid == b2["txid"]
    # Live race: ONE side will retire once a scan confirms either.
    states = superseded_states(world["store"].get_txs_for_wallet(world["wallet"].id))
    assert txid not in states and b2["txid"] not in states


def test_supersede_narration_line(world) -> None:
    """Deliverable 5: the broadcast of a replacement carries the supersede
    line; an ordinary broadcast never grows one."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    bump = world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))
    ref = bump["tx_ref"]
    world["session"].gate_decision = GateDecision.CONFIRM
    world["table"][IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": ref}))
    world["table"][IntentName.SIGN_TX](_env("sign_tx", {"tx_ref": ref}))
    b2 = world["table"][IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": ref}))
    lines: list[str] = []
    app._print_broadcast_tx(b2, lines.append, session=world["session"])
    assert f"Replaces: {txid} — the original may still confirm; only one of these two ever will" in lines


def test_replacement_confirmed_end_state_quoted(world) -> None:
    """Both end states are narratable from store truth (the RBF-005
    lineage answers): once the REPLACEMENT confirms, the original's status
    is the terminal "replaced by <new>" copy."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    bump = world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))
    ref = bump["tx_ref"]
    world["session"].gate_decision = GateDecision.CONFIRM
    world["table"][IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": ref}))
    world["table"][IntentName.SIGN_TX](_env("sign_tx", {"tx_ref": ref}))
    b2 = world["table"][IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": ref}))
    new_txid = b2["txid"]
    store, wid = world["store"], world["wallet"].id
    rows = {r.txid: r for r in store.get_txs_for_wallet(wid)}
    # A scan confirms the replacement (the "your replacement confirmed"
    # end state): the original's status answers terminal-replaced, from
    # store truth, with no chain call.
    winner = rows[new_txid]
    store.upsert_txs(
        [
            TxRecord(
                wallet_id=wid, txid=winner.txid, height=900_002,
                block_time=1_700_000_600, fee_sats=winner.fee_sats,
                direction=DIR_OUT, raw_summary=None,
                amount_sats=winner.amount_sats,
                fee_rate_centisat_vb=winner.fee_rate_centisat_vb,
                first_seen=winner.first_seen,
            )
        ]
    )
    calls_before = len(world["recorded"])
    status = world["table"][IntentName.TX_STATUS](_env("tx_status", {"txid": txid}))
    assert status["lineage"] == "replaced"
    assert status["replaced_by"] == new_txid
    assert status["replacement_height"] == 900_002
    assert len(world["recorded"]) == calls_before  # store truth — zero network
    lines: list[str] = []
    app._print_tx_status(status, lines.append)
    assert any("the replacement confirmed at height 900002" in line for line in lines)


def test_original_confirmed_end_state_evicts_bump(world) -> None:
    """The OTHER end state (deliverable 5): the ORIGINAL wins the race →
    the pending bump is terminal-EVICTED and its status honestly says the
    original confirmed instead (never a lost/hung bump)."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    bump = world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))
    ref = bump["tx_ref"]
    world["session"].gate_decision = GateDecision.CONFIRM
    world["table"][IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": ref}))
    world["table"][IntentName.SIGN_TX](_env("sign_tx", {"tx_ref": ref}))
    b2 = world["table"][IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": ref}))
    bump_txid = b2["txid"]
    store, wid = world["store"], world["wallet"].id
    row = next(r for r in store.get_txs_for_wallet(wid) if r.txid == txid)
    # A scan confirms the ORIGINAL (the bump can no longer land).
    store.upsert_txs(
        [
            TxRecord(
                wallet_id=wid, txid=row.txid, height=900_001, block_time=1_700_000_500,
                fee_sats=row.fee_sats, direction=DIR_OUT, raw_summary=None,
                amount_sats=row.amount_sats, fee_rate_centisat_vb=row.fee_rate_centisat_vb,
                first_seen=row.first_seen,
            )
        ]
    )
    calls_before = len(world["recorded"])
    status = world["table"][IntentName.TX_STATUS](_env("tx_status", {"txid": bump_txid}))
    assert status["lineage"] == "evicted"
    assert status["original_txid"] == txid
    assert len(world["recorded"]) == calls_before  # store truth, no chain
    # And the resolver drops BOTH the winner and the loser from "in flight"
    # (original confirmed; its evicted bump is terminal) — a later bump of
    # the confirmed original is refused honestly.
    assert app._resolve_in_flight_outgoing(store.get_txs_for_wallet(wid)) == []
    refused = world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))
    assert refused["error"] == "bump_already_confirmed"


def test_multi_bump_chain_links_transitively(world) -> None:
    """A bump of a bump (T1→T2→T3): each replacement links to the tx it
    directly replaced through the sanctioned writer; the intermediate T2
    carries the carried decomposition so the chain rebuilds without a
    chain fetch."""
    t1 = _ride(world["table"], world["session"], world["flow"], fee="slow")
    _add_coin(world["store"], world["wallet"].id, 4, "a" * 64, 50_000)

    def _bump_and_broadcast(target: str, rate: int) -> str:
        bump = world["table"][IntentName.BUMP_FEE](
            _bump({"target": target, "fee_rate_sat_vb": rate})
        )
        assert bump.get("error") is None, bump
        ref = bump["tx_ref"]
        world["session"].gate_decision = GateDecision.CONFIRM
        world["table"][IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": ref}))
        world["table"][IntentName.SIGN_TX](_env("sign_tx", {"tx_ref": ref}))
        b = world["table"][IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": ref}))
        assert b["status"] == "broadcast"
        return str(b["txid"])

    t2 = _bump_and_broadcast(t1, rate=4)  # first bump
    t3 = _bump_and_broadcast(t2, rate=8)  # bump of the bump (T2 is flow-held)
    rows = {r.txid: r for r in world["store"].get_txs_for_wallet(world["wallet"].id)}
    assert rows[t1].replaced_by_txid == t2
    assert rows[t2].replaced_by_txid == t3
    assert rows[t3].replaced_by_txid is None


def test_faster_while_replacement_pending_rebumps(world) -> None:
    """A speed word while a replacement is still PENDING re-bumps the same
    original at the new rung (the handler's re-bump branch), replacing the
    staged record — never coexisting with a second plan."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    # Stage the bump on the medium rung (change-only reaches the floor for
    # a slow original) so "faster" has a rung above it to move to.
    bump = world["table"][IntentName.BUMP_FEE](_bump({"target": txid, "fee_target": "medium"}))
    assert bump.get("error") is None and bump["fee_target"] == "medium"
    first_ref = bump["tx_ref"]
    _add_coin(world["store"], world["wallet"].id, 4, "a" * 64, 50_000)
    seen: list[dict] = []
    real = world["table"][IntentName.BUMP_FEE]
    world["table"][IntentName.BUMP_FEE] = lambda e: (seen.append(dict(e.params)), real(e))[1]
    try:
        fake = _FakeGen()
        loop = AgentLoop(fake, world["table"])
        app._run_turn(
            loop, world["flow"], world["session"], "faster", [].append, table=world["table"]
        )
    finally:
        world["table"][IntentName.BUMP_FEE] = real
    assert seen and seen[0]["target"] == txid and seen[0]["fee_target"] == "fast"
    assert world["flow"].state is TxFlowStatus.CREATED
    # The re-bump replaced the staged record (new tx_ref); old is inert.
    assert world["flow"].pending.tx_ref != first_ref
    assert world["session"].bump_pending.old_txid == txid


def test_mid_conversation_confirmation_recheck(world) -> None:
    """Deliverable 5 (recheck): the ORIGINAL confirms while the funding
    ask is open → answering the ask re-resolves against FRESH store truth
    and refuses honestly; a replacement of a confirmed transaction is
    never staged."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="medium")
    _add_coin(world["store"], world["wallet"].id, 4, "a" * 64, 10_000)
    ask = world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))
    assert ask.get("ask") == "funding"
    row = next(r for r in world["store"].get_txs_for_wallet(world["wallet"].id) if r.txid == txid)
    world["store"].upsert_txs(
        [
            TxRecord(
                wallet_id=row.wallet_id, txid=row.txid, height=900_001,
                block_time=1_700_000_500, fee_sats=row.fee_sats,
                direction=DIR_OUT, raw_summary=None, amount_sats=row.amount_sats,
                fee_rate_centisat_vb=row.fee_rate_centisat_vb, first_seen=row.first_seen,
            )
        ]
    )
    fake = _FakeGen()
    loop = AgentLoop(fake, world["table"])
    outputs: list[str] = []
    app._run_turn(
        loop, world["flow"], world["session"], "1", outputs.append, table=world["table"]
    )
    assert any("already confirmed" in line for line in outputs)
    assert world["flow"].state is TxFlowStatus.BROADCAST  # NOTHING staged
    assert world["session"].bump_ask is None
    assert fake.prompts == []  # the answer never reached the model


# =========================================================================
# 4. Conversation machinery — never-trap, labels, reroute
# =========================================================================


def test_labels_never_reach_the_model(world) -> None:
    """The pinned pump/FACTS property: coin label words (tags AND free
    notes) never enter any model prompt — offered coins are answered in
    code, and no bump machinery injects them."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="medium")
    _add_coin(world["store"], world["wallet"].id, 4, "a" * 64, 10_000)
    _add_coin(world["store"], world["wallet"].id, 5, "b" * 64, 50_000)
    world["store"].add_address_labels(
        derive_fixture_addresses(8)[4], ["exchange", "coffee money"]
    )
    ask = world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))
    assert ask["options"][0]["label"]  # display material EXISTS on the result
    fake = _FakeGen()
    loop = AgentLoop(fake, world["table"])
    outputs: list[str] = []
    app._run_turn(
        loop, world["flow"], world["session"], "the coffee money one", outputs.append,
        table=world["table"],
    )
    assert world["flow"].state is TxFlowStatus.CREATED  # the label answered it
    # A later ordinary turn: the prompt carries the pending FACTS card and
    # the transcript — the label words appear in NEITHER.
    app._run_turn(
        loop, world["flow"], world["session"], "what now?", outputs.append,
        table=world["table"],
    )
    assert fake.prompts
    for prompt in fake.prompts:
        assert "coffee" not in prompt and "exchange" not in prompt
    facts = app._flow_facts(world["flow"])
    assert not any("coffee" in str(v) or "exchange" in str(v) for v in facts.values())


def test_any_next_utterance_closes_the_ask(world) -> None:
    """Never-trap (UX-004/interrupt): an unrelated utterance while a
    funding ask stands opens the ordinary pipeline AND retires the ask."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="medium")
    _add_coin(world["store"], world["wallet"].id, 4, "a" * 64, 10_000)
    assert world["table"][IntentName.BUMP_FEE](_bump({"target": txid})).get("ask") == "funding"
    fake = _FakeGen()
    loop = AgentLoop(fake, world["table"])
    outputs: list[str] = []
    app._run_turn(
        loop, world["flow"], world["session"], "actually, what's my balance?",
        outputs.append, table=world["table"],
    )
    assert world["session"].bump_ask is None
    assert fake.prompts  # the utterance went where the USER sent it: the model


def test_deny_word_suppresses_ask_answer(world) -> None:
    """"don't take the largest" must never fund a replacement (slice-C
    deny-guard precedent): the ask closes, nothing dispatches."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="medium")
    _add_coin(world["store"], world["wallet"].id, 4, "a" * 64, 10_000)
    _add_coin(world["store"], world["wallet"].id, 5, "b" * 64, 50_000)
    world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))
    fake = _FakeGen()
    loop = AgentLoop(fake, world["table"])
    outputs: list[str] = []
    app._run_turn(
        loop, world["flow"], world["session"], "don't take the largest",
        outputs.append, table=world["table"],
    )
    assert world["session"].bump_ask is None
    assert world["flow"].state is TxFlowStatus.BROADCAST  # nothing staged


def test_faster_after_bump_broadcast_routes_to_bump_not_create(world) -> None:
    """Deliverable 7: after a bump broadcast, a bare "faster" dispatches a
    NEW bump_fee on the NEW txid (fee-target vocabulary) — the create_tx
    handler is never called."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    bump = world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))
    ref = bump["tx_ref"]
    world["session"].gate_decision = GateDecision.CONFIRM
    world["table"][IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": ref}))
    world["table"][IntentName.SIGN_TX](_env("sign_tx", {"tx_ref": ref}))
    b2 = world["table"][IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": ref}))
    dispatched: list[dict] = []
    real_bump = world["table"][IntentName.BUMP_FEE]
    real_create = world["table"][IntentName.CREATE_TX]

    def spy_bump(envelope):
        dispatched.append(dict(envelope.params))
        return real_bump(envelope)

    def spy_create(envelope):
        raise AssertionError("create_tx must never run for a post-bump speed word")

    world["table"][IntentName.BUMP_FEE] = spy_bump
    world["table"][IntentName.CREATE_TX] = spy_create
    try:
        fake = _FakeGen()
        loop = AgentLoop(fake, world["table"])
        outputs: list[str] = []
        app._run_turn(
            loop, world["flow"], world["session"], "faster", outputs.append,
            table=world["table"],
        )
    finally:
        world["table"][IntentName.BUMP_FEE] = real_bump
        world["table"][IntentName.CREATE_TX] = real_create
    assert dispatched == [{"target": b2["txid"], "fee_target": "fast", "funding_ref": None, "fee_rate_sat_vb": None}]
    assert fake.prompts == []  # never through the model


def test_slower_after_bump_broadcast_maps_one_rung_down(world) -> None:
    """The existing fee-target vocabulary: from a fast bump, "slower"
    routes to a medium-rung re-bump of the NEW tx."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    bump = world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))  # default fast
    ref = bump["tx_ref"]
    world["session"].gate_decision = GateDecision.CONFIRM
    world["table"][IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": ref}))
    world["table"][IntentName.SIGN_TX](_env("sign_tx", {"tx_ref": ref}))
    b2 = world["table"][IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": ref}))
    seen: list[dict] = []
    real = world["table"][IntentName.BUMP_FEE]
    world["table"][IntentName.BUMP_FEE] = lambda e: (seen.append(dict(e.params)), real(e))[1]
    try:
        fake = _FakeGen()
        loop = AgentLoop(fake, world["table"])
        app._run_turn(
            loop, world["flow"], world["session"], "slower", [].append, table=world["table"]
        )
    finally:
        world["table"][IntentName.BUMP_FEE] = real
    assert seen and seen[0]["fee_target"] == "medium"
    assert seen[0]["target"] == b2["txid"]


def test_faster_after_plain_broadcast_is_not_rerouted(world) -> None:
    """The reroute latch belongs to BUMP broadcasts only: after an
    ordinary send, "faster" stays on the existing (model) path."""
    _ride(world["table"], world["session"], world["flow"], fee="medium")
    assert world["session"].bump_bcast_txid is None
    fake = _FakeGen()
    loop = AgentLoop(fake, world["table"])
    app._run_turn(
        loop, world["flow"], world["session"], "faster", [].append, table=world["table"]
    )
    assert fake.prompts  # the model heard it, as it always has


def test_requote_of_staged_bump_is_refused(world) -> None:
    """A create_tx RE-QUOTE (same recipient+amount) against a staged
    replacement is refused — the create pipeline re-selects coins and
    would silently break the BIP-125 shape and the lineage."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    staged = world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))
    assert staged.get("error") is None
    before = world["flow"].pending.tx_ref
    refused = world["table"][IntentName.CREATE_TX](
        _env("create_tx", {"recipient": SEND_RECIPIENT, "amount_sats": 60_000, "fee_target": "fast"})
    )
    assert refused["error"] == "bump_requote"
    assert world["flow"].pending.tx_ref == before  # staged replacement intact
    lines: list[str] = []
    app._print_create_tx(refused, lines.append)
    assert any("cancel" in line and "bump" in line for line in lines)


# =========================================================================
# 5. Guards, refusals, riders
# =========================================================================


def test_flow_lifecycle_busy_refusals(world) -> None:
    """CONFIRMED/SIGNED mid-lifecycle → the honest busy refusal, value-
    free, zero chain calls (the estimator is never consulted)."""
    res = world["table"][IntentName.CREATE_TX](
        _env("create_tx", {"recipient": SEND_RECIPIENT, "amount_sats": 60_000, "fee_target": "slow"})
    )
    world["session"].gate_decision = GateDecision.CONFIRM
    world["table"][IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": res["tx_ref"]}))
    calls = len(world["recorded"])
    result = world["table"][IntentName.BUMP_FEE](_bump({"target": "bump whatever"}))
    assert result["error"] == "bump_flow_busy"
    assert result["detail"] == app._BUMP_FLOW_BUSY
    assert len(world["recorded"]) == calls


def test_created_without_bump_marker_shows_pending_card(world) -> None:
    """An ordinary staged send is NEVER bump-replaced: bump_fee while a
    plain pending exists re-shows the pending card."""
    world["table"][IntentName.CREATE_TX](
        _env("create_tx", {"recipient": SEND_RECIPIENT, "amount_sats": 60_000, "fee_target": "slow"})
    )
    result = world["table"][IntentName.BUMP_FEE](_bump({"target": "bump it"}))
    assert result["error"] == "tx_pending"
    assert world["session"].bump_pending is None


def test_wallet_loading_gate_refuses_first(world) -> None:
    """The ADR-0022 first-scan gate holds for bumps too — same line, same
    position as create_tx."""
    _t, store, wallet, client, _rec, flow, session, _s = _hwi_table({})
    try:
        handler = app._make_bump_fee_handler(
            store, wallet.id, _fixture_parsed(), flow,
            SimpleNamespace(estimate=lambda _t: (_ for _ in ()).throw(AssertionError("no chain call"))),
            lambda: None, session,
            scan_gate=SimpleNamespace(first_scan_incomplete=True),
        )
        result = handler(_bump({"target": "a" * 64}))
        assert result == {"error": "wallet_loading", "detail": app.WALLET_LOADING_REFUSAL}
    finally:
        store.close()
        client.close()


def test_unrecorded_target_honest_and_value_free(world) -> None:
    """An in-flight row the flow cannot rebuild (not the last broadcast —
    e.g. carried over from a previous session) is refused honestly; no
    raw transaction is ever fetched."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    assert world["flow"].txid == txid
    world["store"].upsert_txs(
        [
            TxRecord(
                wallet_id=world["wallet"].id, txid=HEX64_OTHER, height=None,
                block_time=None, fee_sats=300, direction=DIR_OUT, raw_summary=None,
                amount_sats=5_000, fee_rate_centisat_vb=150, first_seen=1_700_000_000,
                replaced_by_txid=None,
            )
        ]
    )
    result = world["table"][IntentName.BUMP_FEE](_bump({"target": HEX64_OTHER}))
    assert result["error"] == "bump_unrecorded"
    assert result["detail"] == app._BUMP_UNRECORDED
    assert HEX64_OTHER not in json.dumps(result)
    assert world["flow"].state is TxFlowStatus.BROADCAST  # nothing touched


def test_multi_output_plan_refused(world) -> None:
    """Bumping a self-transfer SPLIT (multi-output plan) is refused with
    the honest line (its bump needs the plan-aware revalidation path —
    adjacent work), never half-staged."""
    table, session, flow = world["table"], world["session"], world["flow"]
    res = table[IntentName.SELF_TRANSFER](_env("self_transfer", {"mode": "split", "parts": 2}))
    ref = res["tx_ref"]
    session.gate_decision = GateDecision.CONFIRM
    table[IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": ref}))
    table[IntentName.SIGN_TX](_env("sign_tx", {"tx_ref": ref}))
    b = table[IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": ref}))
    result = table[IntentName.BUMP_FEE](_bump({"target": b["txid"]}))
    assert result["error"] == "bump_multi_output"
    assert flow.state is TxFlowStatus.BROADCAST


def test_cpfp_guard_became_the_real_conversation(world) -> None:
    """TCK-CPFP-002 REPLACED the RBF-004 step-0.5 guard: mode ``cpfp`` now
    conversates — it still refuses cleanly BEFORE the split/consolidate
    branching (no crash on the missing ``below_size_sats``), still makes
    ZERO fee-estimator calls (the honest "nothing unconfirmed" answer is
    store truth), and the value-free ``cpfp_unavailable`` line survives
    ONLY for the session-less direct-wiring backstop (pinned in
    tests/test_cpfp002_conversation.py)."""
    def fees() -> list:
        return [r for r in world["recorded"] if "/v1/fees" in r.url.path]
    assert fees() == []
    result = world["table"][IntentName.SELF_TRANSFER](_env("self_transfer", {"mode": "cpfp"}))
    assert result == {
        "error": "cpfp_nothing_unconfirmed",
        "detail": app._CPFP_NOTHING_UNCONFIRMED,
    }
    assert fees() == []  # zero fee-estimator calls to answer
    merged = world["table"][IntentName.SELF_TRANSFER](
        _env("self_transfer", {"mode": "cpfp", "merge_coin": True})
    )
    assert merged["error"] == "cpfp_nothing_unconfirmed"
    lines: list[str] = []
    app._print_self_transfer(result, lines.append)
    assert lines == [app._CPFP_NOTHING_UNCONFIRMED]
    assert world["flow"].state is TxFlowStatus.IDLE  # nothing staged


def test_cpfp_while_a_send_pends_reshows_the_card(world) -> None:
    """TCK-CPFP-002: a cpfp envelope while an ORDINARY send pends re-shows
    the pending card (the flow-posture guard — a pending plan is never
    silently replaced; busy posture makes zero fee-estimator calls)."""
    world["table"][IntentName.CREATE_TX](
        _env("create_tx", {"recipient": SEND_RECIPIENT, "amount_sats": 60_000, "fee_target": "slow"})
    )
    def fees() -> list:
        return [r for r in world["recorded"] if "/v1/fees" in r.url.path]
    before = len(fees())
    result = world["table"][IntentName.SELF_TRANSFER](_env("self_transfer", {"mode": "cpfp"}))
    assert result["error"] == "tx_pending"
    assert len(fees()) == before
    assert world["flow"].state is TxFlowStatus.CREATED  # pending untouched


def test_cancel_retires_bump_state(world) -> None:
    """A DENY-cancel of the staged replacement retires the lineage marker
    (it belonged to THIS flow — never leaks onto the next one)."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    world["table"][IntentName.BUMP_FEE](_bump({"target": txid}))
    assert world["session"].bump_pending is not None
    fake = _FakeGen()
    loop = AgentLoop(fake, world["table"])
    outputs: list[str] = []
    app._run_turn(
        loop, world["flow"], world["session"], "no cancel it", outputs.append,
        table=world["table"],
    )
    assert world["session"].bump_pending is None
    assert world["flow"].state is TxFlowStatus.CANCELLED


def test_result_values_flow_card_rendering_only(world) -> None:
    """Discipline: refusal detail STRINGS are value-free (no txids/amounts/
    digits) — figures ride STRUCTURED keys only (ADR-0012 §7)."""
    txid = _ride(world["table"], world["session"], world["flow"], fee="slow")
    _add_coin(world["store"], world["wallet"].id, 4, "a" * 64, 10_000)
    _add_coin(world["store"], world["wallet"].id, 5, "b" * 64, 50_000)
    refusal = world["table"][IntentName.BUMP_FEE](
        _bump({"target": txid, "fee_rate_sat_vb": 1})
    )
    assert refusal["error"] == "bump_floor_unreachable"
    assert isinstance(refusal["floor_sats"], int)  # figures ride structured keys
    assert not any(ch.isdigit() for ch in str(refusal["detail"])), refusal["detail"]
    lines: list[str] = []
    app._print_bump_fee(refusal, lines.append)
    assert any(f"{refusal['floor_sats']:,} sats" in line for line in lines)  # rendered from the key
    for line in (app._BUMP_NOTHING_IN_FLIGHT, app._BUMP_UNRECORDED, app._BUMP_FLOW_BUSY):
        assert not any(ch.isdigit() for ch in line), line


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

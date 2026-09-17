"""TCK-FEE-007 — the consolidation rate ask (USER BUG 2026-09-15).

The repro that started it: consolidation → "slower" → the model's open-
ended "What speed would you like for the transaction?" → "0.75 sat/vbyte"
→ the SAME question AGAIN. The closed self_transfer envelope carries NO
rate key, so on the model route the explicit-rate answer could never be
consumed — an endless loop. UX-004's ceiling-ask consumption works for
create_tx precisely because that answer lands on create_tx's
``fee_rate_sat_vb``; the consolidation/self_transfer re-quote path had no
equivalent.

The fix (this file's contract), all deterministic and PRE-MODEL inside the
consolidation conversation (``app.py`` only — protocol/grammar/prompt are
byte-identical, so the model can never author a plan rate):

* a bare speed word while a CONSOLIDATION plan pends opens the ONE rate
  ask (the model's looping question is gone);
* a sat/vB-rate answer rebuilds the plan at exactly that rate through the
  LANDED explicit-rate seam (TCK-FEE-004/FEE-006: MAX with the RELAY rail
  only, a raise narrated, never a silent alteration, zero extra chain
  calls), riding the dispatcher-owned CREATED→CREATED replace
  (commit-only-on-success, the create_tx re-quote semantics);
* one ask → one consumption → never-trap: any non-answer (or a
  threshold-shaped answer) CLOSES the ask and releases the line.

The DISAMBIGUATION (pinned BOTH ways): a rate answer ("0.75 sat/vbyte")
is never consumed as a size THRESHOLD, and a threshold answer ("100000"
/ "100000 sats") is never consumed as a RATE. TCK-FEE-008 re-adjudicated
one leg — a BARE number to the open RATE ask IS a rate (the ask copy
says "say a rate"; the grammars are now disjoint by the ask-KIND gate
plus the sats-unit rule, not by the marker requirement alone).
"""

from __future__ import annotations

from typing import Any

import pytest

from localwallet import app
from localwallet.tx.flow import TxFlowStatus
from tests.test_cons001_conversation import (  # the production harness
    _coin,
    _labeled,
    _turn,
)
from tests.test_cons001_conversation import (
    world as _cons_world,  # noqa: F401 — the CONS-001 harness fixture
)


@pytest.fixture()
def world(_cons_world):  # noqa: F811 — fixture alias, not a shadowing arg
    yield _cons_world


OPENER = "consolidate utxos smaller than 50000 sats"


def _stage_two_coin_plan(world) -> tuple[str, int]:
    """Open a deterministic consolidation plan over the seed coin + one
    small coin (both unlabeled, one privacy pool) → the plan is pending
    (CREATED). Returns (tx_ref, original_fee_rate_centisat)."""
    _coin(world, "5" * 64, 7_000, index=5)  # two small unlabeled coins
    _coin(world, "6" * 64, 9_000, index=6)
    _outs, fake, _loop = _turn(world, OPENER)
    assert fake.prompts == []  # the explicit cut is fully deterministic
    flow = world["flow"]
    assert flow.state is TxFlowStatus.CREATED
    assert flow.pending is not None and flow.pending.inputs_count == 2
    return flow.pending.tx_ref, flow.pending.fee_rate_centisat_vb
# =========================================================================
# 1. THE USER REPRO — "slower" → "0.75 sat/vbyte" → plan rebuilt, ONE ask
# =========================================================================


def test_repro_slower_then_rate_rebuilds_plan_one_ask(world) -> None:
    ref0, rate0 = _stage_two_coin_plan(world)
    assert rate0 != 75  # the default bid is NOT already the asked rate

    # "slower" opens the ONE deterministic rate ask (NOT the model — the
    # looping question is replaced by code before the model sees a thing).
    outs1, fake1, loop1 = _turn(world, "slower")
    assert fake1.prompts == [] and loop1.history == ()  # never the model
    ask = world["session"].cons_ask
    assert ask is not None and ask.kind == "rate"
    assert app._CONS_RATE_ASK in outs1
    # the pending plan is UNTOUCHED by the ask (same ref, same bid):
    assert world["flow"].pending.tx_ref == ref0
    assert world["flow"].pending.fee_rate_centisat_vb == rate0

    # the answer rebuilds AT the stated rate — the explicit seam, verbatim.
    outs2, fake2, loop2 = _turn(world, "0.75 sat/vbyte")
    assert fake2.prompts == [] and loop2.history == ()  # consumed, no model
    pending = world["flow"].pending
    assert pending is not None
    assert pending.fee_rate_centisat_vb == 75  # exactly 0.75 sat/vB
    assert pending.fee_target is None  # an explicit rate records NO rung
    # dispatcher-owned replace: NEW ref, old ref inert, still one pending.
    assert pending.tx_ref != ref0
    assert world["flow"].state is TxFlowStatus.CREATED
    # the SAME coin set rebuilt (inputs unchanged — only the rate moved):
    assert pending.inputs_count == 2
    # the card quotes the new rate VERBATIM:
    assert "0.75 sat/vB" in "\n".join(outs2)
    # ONE ask total: the rebuilt card does NOT re-pitch the rate question.
    assert world["session"].cons_ask is None
    assert app._CONS_RATE_ASK not in outs2


def test_rebuild_rides_the_explicit_seam_zero_estimate_calls(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rate answer consults ONLY the relay-floor seam — NOT the
    estimator ladder (the FEE-002 "explicit rate ⇒ no estimate call"
    property, carried onto the consolidation path)."""
    _stage_two_coin_plan(world)
    _turn(world, "slower")
    real_estimate = app.FeeEstimator.estimate
    calls: list[Any] = []

    def spy_estimate(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(args)
        return real_estimate(self, *args, **kwargs)

    monkeypatch.setattr(app.FeeEstimator, "estimate", spy_estimate)
    _turn(world, "1.25 sat/vB")
    assert calls == []  # the rebuild made ZERO ladder-estimate calls
    assert world["flow"].pending.fee_rate_centisat_vb == 125


# =========================================================================
# 2. RATE-vs-THRESHOLD DISAMBIGUATION — pinned BOTH directions
# =========================================================================


def test_rate_answer_is_never_consumed_as_a_threshold(world) -> None:
    """The threshold ask (CONS-003) stands open; a sat/vB RATE answer is
    NOT a size → it CLOSES the ask (never-trap) and falls through, it is
    never silently taken as the sats cut."""
    _coin(world, "5" * 64, 7_000, index=5)
    _turn(world, "consolidate small utxos")  # opens the threshold ask
    ask = world["session"].cons_ask
    assert ask is not None and ask.kind == "threshold"
    _, fake, _ = _turn(world, "0.75 sat/vbyte")
    # the threshold ask is gone (a rate is not a size), nothing staged:
    assert world["session"].cons_ask is None
    assert world["flow"].state is not TxFlowStatus.CREATED
    # and it fell through to the ordinary pipeline (the never-trap close):
    assert len(fake.prompts) == 1


def test_whole_sat_rate_is_never_consumed_as_a_threshold(world) -> None:
    """Even a whole-sat rate ("2 sat/vB") beside a threshold ask is NOT a
    sats size — the vB unit is the discriminator; it closes the ask."""
    _coin(world, "5" * 64, 7_000, index=5)
    _turn(world, "consolidate small utxos")
    assert world["session"].cons_ask.kind == "threshold"
    _turn(world, "2 sat/vB")
    assert world["session"].cons_ask is None
    assert world["flow"].state is not TxFlowStatus.CREATED


@pytest.mark.parametrize("answer", ["100000", "100000 sats", "smaller than 100000"])
def test_threshold_answer_is_never_consumed_as_a_rate(world, answer: str) -> None:
    """A size answer to the RATE ask is not a rate → CLOSES the ask; the
    plan's bid is untouched. (TCK-FEE-008 re-adjudicated the FIRST case's
    reason, not its outcome: a bare number IS now the rate grammar, but
    "100000" sits beyond the envelope's 1..MAX_FEE_RATE_SAT_VB span, so
    it still releases — as do the sats-unit and multi-word shapes.)"""
    ref0, rate0 = _stage_two_coin_plan(world)
    _turn(world, "slower")
    assert world["session"].cons_ask.kind == "rate"
    _, fake, _ = _turn(world, answer)
    assert world["session"].cons_ask is None  # never-trap
    assert world["flow"].pending.tx_ref == ref0  # plan untouched
    assert world["flow"].pending.fee_rate_centisat_vb == rate0
    assert len(fake.prompts) == 1  # fell through to the model


# =========================================================================
# 3. BELOW-FLOOR RATE — the honest clamp per the landed seam semantics
# =========================================================================


def test_below_relay_floor_rate_is_raised_and_narrated(world) -> None:
    """A rate under the node's RELAY rail (the assumed 0.1 sat/vB = 10
    centisat here) is MAX'd UP to the rail and NARRATED once — never a
    silent sub-floor bid the network would refuse, never a silent
    alteration (FEE-004/FEE-006: the explicit seam floors at the RAIL
    only)."""
    _stage_two_coin_plan(world)
    _turn(world, "slower")
    outs, fake, _ = _turn(world, "0.05 sat/vB")  # 5 centisat < 10 rail
    assert fake.prompts == []  # still consumed deterministically
    pending = world["flow"].pending
    assert pending is not None
    assert pending.fee_rate_centisat_vb == 10  # clamped to the relay rail
    assert pending.fee_target is None
    # the honest floor line is printed once, quoting the rail verbatim
    # (the landed seam's narration, source = relay):
    note = app._CARD_FEE_FLOOR_NOTE_RELAY.format(rate="0.1")
    assert outs.count(note) == 1


# =========================================================================
# 4. NEVER-TRAP CLOSE on a non-answer
# =========================================================================


@pytest.mark.parametrize(
    "non_answer",
    ["what about the weather", "banana", "how do fees work"],
)
def test_non_answer_closes_the_rate_ask(world, non_answer: str) -> None:
    ref0, rate0 = _stage_two_coin_plan(world)
    _turn(world, "slower")
    assert world["session"].cons_ask.kind == "rate"
    _, fake, _ = _turn(world, non_answer)
    assert world["session"].cons_ask is None  # closed, never trapped
    assert world["flow"].pending.tx_ref == ref0  # the plan stands
    assert world["flow"].pending.fee_rate_centisat_vb == rate0
    assert len(fake.prompts) == 1  # the line reaches the model as chat


def test_deny_while_ask_open_cancels_the_flow(world) -> None:
    """An open rate ask never TRAPS the lifecycle: a deny word closes the
    ask (the shared deny-token suppression) and the SAME line reaches the
    gate — cancel owns it, the model never sees a cancel turn
    (CANCEL-001)."""
    _stage_two_coin_plan(world)
    _turn(world, "slower")
    assert world["session"].cons_ask.kind == "rate"
    _, fake, _ = _turn(world, "cancel")
    assert world["session"].cons_ask is None
    assert world["flow"].state is TxFlowStatus.CANCELLED
    assert world["session"].cons_pending is None  # the marker retires too
    assert len(fake.prompts) == 0  # CANCEL-001 short-circuit (no model)


# =========================================================================
# 5. The speed-word opener is keyed to a CONSOLIDATION pending ONLY
# =========================================================================


def test_speed_word_over_a_labeled_pool(world) -> None:
    """The canonical multi-coin consolidation (via _labeled + explicit
    cut) then a rate answer — the rebuild works over a pool selection
    too, carrying the SAME coins."""
    _labeled(world)  # kyc×2 (12k+30k), p2p×1 (45k), unlabeled 7k, seed 100k
    _turn(world, "consolidate utxos smaller than 50000 sats")
    flow = world["flow"]
    assert flow.state is TxFlowStatus.CREATED
    n_inputs = flow.pending.inputs_count  # type: ignore[union-attr]
    _turn(world, "slower")
    assert world["session"].cons_ask.kind == "rate"
    _, fake, _ = _turn(world, "0.9 sat/vB")
    assert fake.prompts == []
    assert flow.pending.fee_rate_centisat_vb == 90  # type: ignore[union-attr]
    assert flow.pending.inputs_count == n_inputs  # same coin set rebuilt


def test_fractional_two_decimal_rate_lands_verbatim(world) -> None:
    _stage_two_coin_plan(world)
    _turn(world, "slower")
    _turn(world, "1.23 sat/vB")
    assert world["flow"].pending.fee_rate_centisat_vb == 123


def test_three_decimal_rate_is_not_an_answer(world) -> None:
    """Sub-centisat precision (the engine's unit is centisat/vB = two
    decimals) is NOT this grammar — it CLOSES the ask rather than
    silently rounding a money figure."""
    _stage_two_coin_plan(world)
    _turn(world, "slower")
    _, fake, _ = _turn(world, "0.755 sat/vB")
    assert world["session"].cons_ask is None
    assert world["flow"].pending.fee_rate_centisat_vb != 75
    assert len(fake.prompts) == 1


def test_env_carryover_from_a_non_rate_pending_is_unaffected(world) -> None:
    """A bare speed word while NO consolidation is pending (fresh world,
    IDLE flow) never opens the rate ask — the opener is keyed to the
    staged-consolidation marker."""
    _, fake, _ = _turn(world, "slower")
    assert world["session"].cons_ask is None
    assert world["flow"].state is TxFlowStatus.IDLE
    assert len(fake.prompts) == 1  # falls through to the ordinary pipeline


# =========================================================================
# 6. Pure-parser unit pins for the rate grammar (closed-world acceptance)
# =========================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.75 sat/vB", 75),
        ("0.75 sat/vbyte", 75),
        ("2 sat/vb", 200),
        ("1,000 sat/vB", 100_000),
        ("0.75 sats per vbyte please", 75),
        ("the rate of 1.5 sat/vb", 150),
        # TCK-FEE-008 RE-ADJUDICATION: the original ("0.75", None) release
        # pin is retired by that ticket — a BARE number is a rate when the
        # rate ask is the open one (kind-gated; the ask copy always said
        # "say a rate", and rejecting "0.75" shipped it to the model):
        ("0.75", 75),
        # not a rate (threshold-shaped / ambiguous):
        ("100000 sats", None),  # sats-unit word, no vB marker (FEE-008 keeps it)
        (".75 sat/vb", None),  # leading-dot number could misread as 75 → reject
        ("5 sat/vb and call it a day", None),  # words outside the closed grammar
        ("0.755 sat/vB", None),  # > 2 decimals
        ("bananas sat/vB", None),  # no number at all
        ("2 sat/vB 3 sat/vB", None),  # two numbers
    ],
)
def test_rate_parser_grammar(text: str, expected: int | None) -> None:
    assert app._cons_rate_answer(text) == expected

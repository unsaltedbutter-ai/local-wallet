"""TCK-FEE-008 — the BARE number answers an open RATE ask (USER BUG 2026-09-16).

The debugger-verified repro: consolidation → "slower" → "What speed would
you like… Say a rate in sat/vB…" → "0.75" → REJECTED by the FEE-007
grammar (it demanded a vB-unit marker), the ask CLOSED (never-trap) and
the line fell to the LLM, which answered balance-nonsense. The ask copy
over-promised: a bare number IS a rate.

The fix (orchestrator-adjudicated option (a)), all inside
:func:`app._cons_rate_answer`: exactly ONE number and no other words (or
only neutral fillers — "0.75 please") is consumed as sat/vB — the same
centisat conversion, envelope bound and landed explicit-rail seam the
unit-suffixed forms ride. Unit-suffixed behavior is byte-for-byte
unchanged; multi-number, unitless-worded and non-numeric replies keep
today's release.

The FEE-007 rate-vs-threshold DISAMBIGUATION survives on two legs:
* the KIND gate — ``_cons_rate_answer`` runs ONLY under
  ``ask.kind == "rate"``, so a bare number to a THRESHOLD ask lands on
  the threshold grammar (pinned below: it stamps ``below_size_sats``,
  NEVER a rate), and the two asks are never open simultaneously;
* the sats-UNIT rule — a sats-unit word beside a marker-less number
  stays the threshold shape ("1000 sats" is not a rate).
"""

from __future__ import annotations

import pytest

from localwallet import app
from localwallet.tx.flow import TxFlowStatus
from tests.test_cons001_conversation import _coin, _spy, _turn
from tests.test_cons001_conversation import (
    world as _cons_world,  # noqa: F401 — the CONS-001 harness fixture
)
from tests.test_fee007_rate_ask import _stage_two_coin_plan


@pytest.fixture()
def world(_cons_world):  # noqa: F811 — fixture alias, not a shadowing arg
    yield _cons_world


# =========================================================================
# 1. THE REPRO — bare "0.75" after the rate ask rebuilds the plan, ONE ask
# =========================================================================


def test_bare_rate_rebuilds_plan_one_ask(world) -> None:
    """Mirror of the FEE-007 consumed-rate repro with the unit stripped:
    "0.75" is consumed pre-model, rebuilds at 75 centisat/vB over the
    SAME coins, and the consumed reply CLOSES the ask (one-ask
    invariant — the rebuilt card never re-pitches the question)."""
    ref0, rate0 = _stage_two_coin_plan(world)
    assert rate0 != 75
    _turn(world, "slower")
    assert world["session"].cons_ask.kind == "rate"

    outs, fake, loop = _turn(world, "0.75")
    assert fake.prompts == [] and loop.history == ()  # consumed, never the model
    pending = world["flow"].pending
    assert pending is not None
    assert pending.fee_rate_centisat_vb == 75  # exactly 0.75 sat/vB, verbatim
    assert pending.fee_target is None  # an explicit rate records NO rung
    assert pending.tx_ref != ref0 and pending.inputs_count == 2  # same coins
    assert world["flow"].state is TxFlowStatus.CREATED
    assert "0.75 sat/vB" in "\n".join(outs)  # the card quotes it verbatim
    assert world["session"].cons_ask is None
    assert app._CONS_RATE_ASK not in outs


def _stage_big_plan(world) -> None:
    """A plan fat enough to carry a 1,200 sat/vB fee (16k coins cannot —
    the seam's honest "insufficient funds" would stand the OLD plan,
    testing the builder instead of the grammar): seed 100k + two coins,
    cut 250000 → three inputs, 350k total."""
    _coin(world, "5" * 64, 120_000, index=5)
    _coin(world, "6" * 64, 130_000, index=6)
    _turn(world, "consolidate utxos smaller than 250000 sats")
    assert world["flow"].state is TxFlowStatus.CREATED


@pytest.mark.parametrize(
    ("answer", "centisat"), [("0.5", 50), ("75", 7_500), ("1,200", 120_000)]
)
def test_bare_decimal_whole_and_separated_forms(world, answer: str, centisat: int) -> None:
    """Decimal ("0.5"), whole ("75") and thousands-separated ("1,200")
    bare forms all land VERBATIM on the explicit seam (no ladder)."""
    _stage_big_plan(world)
    _turn(world, "slower")
    _, fake, _ = _turn(world, answer)
    assert fake.prompts == []  # consumed deterministically
    assert world["flow"].pending.fee_rate_centisat_vb == centisat
    assert world["session"].cons_ask is None


# =========================================================================
# 2. THE KIND GATE — a bare number to a THRESHOLD ask is never a rate
# =========================================================================


def test_bare_number_to_threshold_ask_stamps_size_not_rate(world) -> None:
    """The FEE-008 companion pin: the same bare-number shape answers a
    THRESHOLD ask through the THRESHOLD grammar (whole sats) — the
    envelope carries ``below_size_sats`` and NO rate key, and the
    rate-ask rebuild machinery is never touched."""
    _coin(world, "5" * 64, 7_000, index=5)
    _turn(world, "consolidate small utxos")
    ask = world["session"].cons_ask
    assert ask is not None and ask.kind == "threshold"
    seen = _spy(world)
    _, fake, _ = _turn(world, "50000")  # the bare form, threshold side
    assert fake.prompts == []  # consumed as the SIZE it always was
    assert seen[-1].params.model_dump() == {
        "mode": "consolidate",
        "below_size_sats": 50_000,
    }  # no fee_rate key exists on this envelope — the kind gate held
    assert world["session"].cons_ask is None
    assert world["flow"].state is TxFlowStatus.CREATED


def test_bare_decimal_to_threshold_ask_still_releases(world) -> None:
    """Today's behavior preserved: "0.75" is not a whole-sats SIZE either
    — to a THRESHOLD ask it is no answer, the ask CLOSES (never-trap)
    and the line falls through to the ordinary pipeline."""
    _coin(world, "5" * 64, 7_000, index=5)
    _turn(world, "consolidate small utxos")
    assert world["session"].cons_ask.kind == "threshold"
    _, fake, _ = _turn(world, "0.75")
    assert world["session"].cons_ask is None
    assert world["flow"].state is not TxFlowStatus.CREATED
    assert len(fake.prompts) == 1  # released, as ever


# =========================================================================
# 3. TODAY'S RELEASES KEEP RELEASED (guard rails)
# =========================================================================


@pytest.mark.parametrize(
    "line", ["0.75 0.5", "0.75 make it fast", "banana", "1000 sats", "0.75 sat"]
)
def test_non_bare_replies_keep_releasing(world, line: str) -> None:
    """Multi-number, unrelated-word, non-numeric and SATS-UNIT replies
    are not the bare form: close the ask (never-trap), leave the plan's
    bid untouched, release the line to the ordinary pipeline."""
    ref0, rate0 = _stage_two_coin_plan(world)
    _turn(world, "slower")
    assert world["session"].cons_ask.kind == "rate"
    _, fake, _ = _turn(world, line)
    assert world["session"].cons_ask is None
    assert world["flow"].pending.tx_ref == ref0
    assert world["flow"].pending.fee_rate_centisat_vb == rate0
    assert len(fake.prompts) == 1


# =========================================================================
# 4. Pure-parser unit pins for the bare acceptance (closed-world)
# =========================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # the bare form (TCK-FEE-008):
        ("0.75", 75),
        ("0.5", 50),
        ("75", 7_500),
        ("1,200", 120_000),
        ("0.75 please", 75),  # neutral fillers only
        ("the rate 1.5", 150),
        # still NOT a rate — threshold shape, bound, or off-grammar:
        ("1000 sats", None),  # sats unit, no vB marker: threshold shape
        ("0.75 sat", None),
        ("2 satoshi", None),
        ("0.755", None),  # sub-centisat precision: no silent rounding
        (".75", None),  # leading-dot could fold into "75" — 100× lesson
        ("100000", None),  # beyond the envelope's 1..MAX_FEE_RATE_SAT_VB span
        ("0", None),  # a 0 sat/vB rate is no rate
        ("0.75 and then some", None),  # words outside the closed vocabulary
        ("0.75 0.5", None),  # two numbers
        ("banana", None),
        ("sat/vb", None),  # marker without a number
    ],
)
def test_bare_rate_parser_grammar(text: str, expected: int | None) -> None:
    assert app._cons_rate_answer(text) == expected

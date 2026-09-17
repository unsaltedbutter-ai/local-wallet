"""TCK-CONS-004 — bare-number registry picks in consolidation lines.

Debugger root-cause (2026-09-16, verified live): "consolidate 24 and 17"
(UTXO registry #17 and #24) RELEASED to the model — only ``#``-marked or
address-worded digits were picks — and the model misread the numbers as
sat thresholds, answering the _SELF_NOTHING_BELOW refusal.

The rule (orchestrator-adjudicated): in a consolidation line a bare digit
can NEVER be a size cut (the deterministic comparator grammar requires a
BELOW-word), so a TWO-OR-MORE-number list that no comparator claims
resolves as registry picks at the shared choke point. The minimal read of
the single-bare-digit adjudication: a SINGLE address-wordless, #-less
digit keeps today's model-route release (the pinned "consolidate my 3
favorite coins" — re-pinned e2e at tests/test_chat002_consolidate.py:193;
consuming it would pick registry #3, a real change to a pinned release).

Pins here: the verbatim repro lines as picks; ``#N`` and "address N"
forms byte-identical; the size path untouched (a parsed comparator still
wins, list ∧ stated cut stays ambiguous); the single-digit release; and
end-to-end through the production harness: "consolidate 24 and 17" on a
seeded store PLANS those registry coins (model never sees the line, no
nothing-smaller refusal), while an unregistered number rides the existing
ADDRESS_REF_UNKNOWN clarify.
"""

from __future__ import annotations

import pytest

from localwallet import app
from localwallet.tx.flow import TxFlowStatus
from tests.test_cons001_conversation import (
    _labeled,
    _spy,
    _turn,
)
from tests.test_cons001_conversation import (
    world as _cons_world,  # noqa: F401 — the CONS-001 harness fixture
)
from tests.test_cons003_flexible_selection import _show_all
from tests.test_e2e_skeleton import derive_fixture_addresses
from tests.test_rbf004_bump import _add_coin


@pytest.fixture()
def world(_cons_world):  # noqa: F811 — fixture alias, not a shadowing arg
    yield _cons_world


# =========================================================================
# 1. Matcher pins (the debugger's repro matrix, verbatim)
# =========================================================================


def test_bare_number_lists_resolve_as_registry_picks() -> None:
    """Every RELEASED row of the verified repro matrix is now a 2-number
    pick; the conjunctive shape ("and", "&", comma) is irrelevant — two
    bare digits with no parseable below-comparator ARE the objects."""
    pick = (None, None, False, (17, 24), None, "")
    assert app._consolidation_intent("consolidate 24 and 17") == pick
    assert app._consolidation_intent("consolidate 3 and 9") == (
        None, None, False, (3, 9), None, ""
    )
    assert app._consolidation_intent("consolidate my coins 3 and 9") == (
        None, None, False, (3, 9), None, ""
    )
    assert app._consolidation_intent("consolidate coin 3 and 9") == (
        None, None, False, (3, 9), None, ""
    )
    assert app._consolidation_intent("consolidate utxos 3 and 9") == (
        None, None, False, (3, 9), None, ""
    )
    assert app._consolidation_intent("merge 5 and 7") == (
        None, None, False, (5, 7), None, ""
    )
    assert app._consolidation_intent("consolidate 3, 9") == (
        None, None, False, (3, 9), None, ""
    )


def test_hash_and_address_forms_are_unchanged() -> None:
    """The named forms keep their exact tuples — byte-identical behavior
    (and the fee-rung/deny gates they already rode are unchanged)."""
    assert app._consolidation_intent("consolidate #24 and #17") == (
        None, None, False, (17, 24), None, ""
    )
    assert app._consolidation_intent("consolidate address 3 & 9") == (
        None, None, False, (3, 9), None, ""
    )
    assert app._consolidation_intent("consolidate my 3 favorite coins") is None
    assert app._consolidation_intent("don't consolidate 3 and 9") is None


def test_size_path_wins_and_mixed_forms_stay_ambiguous() -> None:
    """A fully parsed comparator still takes the size path; a number LIST
    AND a stated cut in one line remains the ambiguous release; a single
    digit no comparator claims is still NOT a pick (the adjudication); and
    a pathological digit token neither picks nor crashes (CHAT-009)."""
    assert app._consolidation_intent("consolidate utxos smaller than 100001 sats") == (
        None, None, False, (), 100_001, ""
    )
    assert app._consolidation_intent("consolidate 3 and 9 under 100000 sats") is None
    assert app._consolidation_intent("consolidate #18 under 100000 sats") is None
    assert app._consolidation_intent("consolidate utxos larger than 100000 sats") is None
    assert app._consolidation_intent("consolidate " + "1" * 5000 + " and 3") is None


def test_bare_list_stays_bounded_like_the_hash_form() -> None:
    """Over the documented input ceiling the bare list is NOT intercepted,
    exactly as the #-list already behaves (never a silent truncation)."""
    many = " ".join(str(n) for n in range(1, app.MAX_SELF_TRANSFER_CONSOLIDATE_INPUTS + 2))
    assert app._consolidation_intent(f"consolidate {many}") is None


# =========================================================================
# 2. End-to-end through the production harness
# =========================================================================


def test_repro_line_reaches_the_consolidation_plan(world) -> None:
    """THE user report, verbatim: with registry numbers 17 and 24 holding
    coins, "consolidate 24 and 17" resolves as the picks, restates every
    FULL address, and plans — the model never sees the line and the
    nothing-smaller refusal NEVER renders."""
    addrs = derive_fixture_addresses(24)
    numbers = _show_all(world, 24)
    _add_coin(world["store"], world["wallet"].id, 16, "1" * 64, 11_000)
    _add_coin(world["store"], world["wallet"].id, 23, "2" * 64, 13_000)
    assert (numbers[addrs[16]], numbers[addrs[23]]) == (17, 24)
    seen = _spy(world)
    outs, fake, loop = _turn(world, "consolidate 24 and 17")
    assert fake.prompts == [] and loop.history == ()  # fully deterministic
    assert app._SELF_NOTHING_BELOW not in outs
    assert f"Coin #17 at {addrs[16]} — 11,000 sats." in outs
    assert f"Coin #24 at {addrs[23]} — 13,000 sats." in outs
    assert world["flow"].state is TxFlowStatus.CREATED
    assert world["flow"].pending is not None and world["flow"].pending.inputs_count == 2
    assert seen[-1].params.model_dump() == {
        "mode": "consolidate",
        "below_size_sats": 13_001,  # max(picked)+1 — never a user number
    }


def test_unknown_number_in_bare_list_honest_clarify(world) -> None:
    """A bare list naming a number the registry does not know rides the
    EXISTING value-free clarify — nothing staged, no nearest guess, no
    model turn (same path as the #-form miss)."""
    _labeled(world)
    outs, fake, _ = _turn(world, "consolidate 17 and 99")
    assert outs == [app.ADDRESS_REF_UNKNOWN]
    assert fake.prompts == []
    assert world["flow"].state is not TxFlowStatus.CREATED
    assert world["session"].cons_ask is None

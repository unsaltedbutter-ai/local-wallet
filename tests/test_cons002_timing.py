"""TCK-CONS-002 — the code-owned consolidate-TIMING answer (council qwen#9).

"is now a good time to consolidate?" / "should I consolidate now?" are
answered by the DISPATCHER, not the model: a FACTS snapshot of the engine's
own fee figures (the ONE cached estimator snapshot — zero extra chain
calls, integer centisat/vB) plus the PINNED decision rule:

    FOLD-ACT   slow bid <= the user's own consolidate_below_sat_vb ceiling
               (x100 unit conversion, the tx/selection.py step-5 mirror)
               AND >= 2 coins under the user's utxo_target_min_sats —
               waiting is pointless, the wallet folds them anyway;
    NO-DATA    no six-hour average (or no bids) — no comparison, no claim;
    WAIT       slow bid >  six_hour_low_average_centisat_vb (CHAT-002's
               elevated warning, mirrored);
    ACT        slow bid <= six_hour_low_average_centisat_vb.

Every line is hedged and VALUE-FREE: no digits, no fabricated probability.
The answer closes with the deterministic bypass INTO the existing
conversation (the canonical "consolidate my small utxos" utterance the
CHAT-002 intercept already consumes — no new gate vocabulary, no new
intent, no prompt route; the existing opener keeps priority over every
phrasing it already matched).

Rides the REAL CONS-001/CHAT-002 harness (production dispatch table, mock
chain, fake-device signer — no network, deterministic).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Final

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.chain.fees import FeeEstimator
from localwallet.protocol import IntentName
from localwallet.tx.flow import TxFlowStatus
from tests.test_chat002_consolidate import _blocks
from tests.test_cons001_conversation import _labeled, _turn
from tests.test_cons001_conversation import (
    world as _cons_world,  # noqa: F401 — the CONS-001 harness fixture, injected by name
)
from tests.test_e2e_skeleton import derive_fixture_addresses
from tests.test_rbf004_bump import _FakeGen
from tests.test_tx_self_transfer import _hwi_table, _utxo

FACTS_KEYS: Final[tuple[str, ...]] = (
    "slow_bid_centisat_vb",
    "medium_bid_centisat_vb",
    "fast_bid_centisat_vb",
    "six_hour_low_average_centisat_vb",
)


@pytest.fixture()
def world(_cons_world):  # noqa: F811 — fixture alias, not a shadowing arg
    yield _cons_world


def _ask(world, line: str) -> tuple[list[str], _FakeGen, AgentLoop]:
    """One REAL turn through the production intercept chain WITH the
    estimator wired (the pump's shape): the shared mock client backs a
    production FeeEstimator, so engineered ``state["mempool_blocks"]``
    snapshots drive the FACTS exactly like the live path."""
    est = world.setdefault("estimator", FeeEstimator(world["client"]))
    fake = _FakeGen()
    loop = AgentLoop(fake, world["table"])
    outputs: list[str] = []
    app._run_turn(
        loop, world["flow"], world["session"], line, outputs.append,
        table=world["table"], store=world["store"], fee_estimator=est,
    )
    return outputs, fake, loop


def _fee_gets(world) -> list[str]:
    return [
        r.url.path
        for r in world["recorded"]
        if r.url.path.endswith(("/v1/fees/recommended", "/v1/fees/mempool-blocks"))
    ]


# =========================================================================
# 1. The pinned branches — engineered fee snapshots (wait / act / no-data)
# =========================================================================


def test_wait_branch_elevated_slow_bid(world) -> None:
    """slow 8.0 (800c) > six-hour avg 5.33 (533c) → the WAIT recommendation,
    the bypass line, and NOTHING reaches the model (the consumed turn is
    transcript-free like every consolidation turn)."""
    _labeled(world)  # 4 coins under the default 100k target min
    world["state"]["mempool_blocks"] = _blocks([10.0, 8.0, 5.0, 4.0, 3.0, 2.0])
    outs, fake, loop = _ask(world, "is now a good time to consolidate?")
    assert outs == [app._CONS_TIMING_WAIT, app._CONS_TIMING_BYPASS]
    assert fake.prompts == [] and loop.history == ()
    assert world["session"].cons_ask is None  # an answer, not an opener
    assert world["flow"].state is TxFlowStatus.IDLE  # nothing staged


def test_act_branch_at_or_below_average(world) -> None:
    """TCK-FEE-006 RE-PIN: this payload (B₀ 8.0, then a cliff to 2.0) once
    acted on the strength of a slow bid BELOW the next block's floor
    (B₁ 2.0 = 200c <= avg 333c). Under the corrected policy floor a
    target-follower bid never undercuts the projected next block's OWN
    bottom: slow lifts to 800c, sits ABOVE the six-hour average, and the
    answer is WAIT. (The ACT branch stays pinned on projections that do
    not undercut themselves: test_act_boundary_equal_bids_is_not_wait.)
    The fold clause is switched OFF by the user's OWN target (no coins
    under 1000 sats), so the average comparison is what answers."""
    _labeled(world)
    world["store"].set_coin_setting("utxo_target_min_sats", "1000")
    world["state"]["mempool_blocks"] = _blocks([8.0, 2.0, 2.0, 2.0, 2.0, 2.0])
    outs, *_ = _ask(world, "should I consolidate now")
    assert outs == [app._CONS_TIMING_WAIT, app._CONS_TIMING_BYPASS]


def test_act_boundary_equal_bids_is_not_wait(world) -> None:
    """slow == avg (flat 2.0 bottoms → 200c == 200c) → ACT (the pinned
    comparison is ``slow > avg`` for WAIT — equality favors acting)."""
    _labeled(world)
    world["store"].set_coin_setting("utxo_target_min_sats", "1000")
    world["state"]["mempool_blocks"] = _blocks([2.0] * 6)
    outs, *_ = _ask(world, "is now a good time to consolidate?")
    assert outs[0] == app._CONS_TIMING_ACT


def test_no_data_branch_recommended_fallback(world) -> None:
    """No mempool-blocks route (404 → the recommended fallback): the
    six-hour average does not EXIST, so no comparison is drawn and no
    elevated/calm claim is made — the honest NO-DATA line, answer still
    useful (the bypass follows)."""
    _labeled(world)
    world["store"].set_coin_setting("utxo_target_min_sats", "1000")  # fold clause off
    outs, fake, _ = _ask(world, "is now a good time to consolidate?")
    assert outs == [app._CONS_TIMING_NO_DATA, app._CONS_TIMING_BYPASS]
    assert app._CONS_TIMING_WAIT not in outs
    assert app._CONS_TIMING_ACT not in outs
    assert fake.prompts == []


def test_no_data_branch_estimator_failure_degrades(world) -> None:
    """A broken fee source (recommended payload 500 → estimate raises,
    average seam fail-closes to None) degrades to the SAME honest NO-DATA
    line — never a fabricated comparison, never a crashed turn."""
    _labeled(world)
    world["store"].set_coin_setting("utxo_target_min_sats", "1000")
    world["state"]["fees_fail"] = True
    try:
        outs, fake, _ = _ask(world, "is now a good time to consolidate?")
        assert outs == [app._CONS_TIMING_NO_DATA, app._CONS_TIMING_BYPASS]
        assert fake.prompts == []
    finally:
        world["state"]["fees_fail"] = False


def test_fold_branch_wins_over_elevated(world) -> None:
    """The user's OWN fold policy outranks the elevated warning: slow 0.9
    clamps to the 1.0 relay floor (100c) which is ABOVE the 0.67 avg
    (WAIT-shaped) yet INSIDE the default 2 sat/vB ceiling with 4 coins
    under the 100k target — waiting is pointless, the engine folds them
    into ordinary sends anyway → the FOLD-ACT line."""
    _labeled(world)  # default policy: ceiling 2 sat/vB, target min 100_000
    world["state"]["mempool_blocks"] = _blocks([1.0, 0.9, 0.1])
    outs, *_ = _ask(world, "should I consolidate now?")
    assert outs[0] == app._CONS_TIMING_FOLD
    assert app._CONS_TIMING_WAIT not in outs


def test_fold_needs_the_users_own_small_coins(world) -> None:
    """Fold context is the wallet's TRUTH: with every coin above the
    user's own target min the fold clause cannot fire (same fee snapshot
    as the WAIT pin) — the comparison answers instead."""
    _labeled(world)
    world["store"].set_coin_setting("utxo_target_min_sats", "1000")
    world["state"]["mempool_blocks"] = _blocks([10.0, 8.0, 5.0, 4.0, 3.0, 2.0])
    outs, *_ = _ask(world, "is now a good time to consolidate?")
    assert outs[0] == app._CONS_TIMING_WAIT


def test_malformed_ladder_drops_decoration_keeps_answer(world, monkeypatch) -> None:
    """A corrupt coin-policy resolution kills the FOLD clause
    (decoration) in fail-quiet silence — the six-hour comparison still
    answers honestly. (Driven by patching the resolver: the store itself
    refuses malformed rung values, so a half-read policy can only arrive
    from elsewhere — the except-arm is what's pinned.)"""

    def _boom(*_a, **_k):
        raise ValueError("engineered malformed ladder")

    monkeypatch.setattr(app, "resolve_coin_selection_settings", _boom)
    world["state"]["mempool_blocks"] = _blocks([10.0, 8.0, 5.0, 4.0, 3.0, 2.0])
    outs, *_ = _ask(world, "is now a good time to consolidate?")
    assert outs[0] == app._CONS_TIMING_WAIT


# =========================================================================
# 2. The FACTS snapshot — shape and types (engine-owned figures only)
# =========================================================================


class _StubEst:
    """A FeeEstimator double: integer-centisat bids per rung + the six-hour
    average (or ``None`` / an injected exception — the degraded paths)."""

    def __init__(self, bids: dict[str, Any], avg: int | None, *, raise_exc: bool = False):
        self._bids = bids
        self._avg = avg
        self._raise = raise_exc

    def estimate(self, target):
        if self._raise:
            raise RuntimeError("engineered refresh failure")  # value-free
        return SimpleNamespace(rate_centisat_vb=self._bids[target.value], clamped=False)

    def six_hour_low_average_centisat_vb(self):
        return self._avg


def test_facts_shape_keys_and_integer_types() -> None:
    facts = app._consolidation_timing_facts(
        _StubEst({"slow": 100, "medium": 115, "fast": 230}, 67)
    )
    assert set(facts) == set(FACTS_KEYS)
    for key, value in facts.items():
        assert isinstance(value, int) and not isinstance(value, bool), key
    assert facts["slow_bid_centisat_vb"] == 100
    assert facts["six_hour_low_average_centisat_vb"] == 67


def test_facts_shape_degraded_paths() -> None:
    """No per-block data → the average is None and the bids ride; a broken
    source → every figure is None. Absence is always ``None``, never a
    zero, never a guess."""
    no_avg = app._consolidation_timing_facts(_StubEst({"slow": 200, "medium": 230, "fast": 460}, None))
    assert no_avg["six_hour_low_average_centisat_vb"] is None
    assert all(no_avg[k] is not None for k in tuple(FACTS_KEYS)[:3])
    broken = app._consolidation_timing_facts(
        _StubEst({"slow": 0, "medium": 0, "fast": 0}, None, raise_exc=True)
    )
    assert list(broken) == list(broken)  # keys intact
    assert all(v is None for v in broken.values())


# =========================================================================
# 3. The copy — value-free, hedged, no fabricated probability
# =========================================================================


def test_timing_copy_is_value_free_and_hedged() -> None:
    for line in (
        app._CONS_TIMING_WAIT,
        app._CONS_TIMING_ACT,
        app._CONS_TIMING_FOLD,
        app._CONS_TIMING_NO_DATA,
        app._CONS_TIMING_BYPASS,
    ):
        assert not any(ch.isdigit() for ch in line), line  # no figures, no clock
        assert "%" not in line and "probability" not in line.lower(), line
    for line in (
        app._CONS_TIMING_WAIT,
        app._CONS_TIMING_ACT,
        app._CONS_TIMING_FOLD,
        app._CONS_TIMING_NO_DATA,
    ):
        assert "promise" in line or "can't" in line, line  # every ANSWER hedged
    # the elevated recommendation mirrors CHAT-002's warning comparison
    assert "above" in app._CONS_TIMING_WAIT and "estimate, never " "a promise" in app._CONS_TIMING_WAIT


def test_timing_copy_quotes_no_threshold_constant() -> None:
    """The fold line names the user's OWN ceiling by its settings display
    word, never a number — and no line leaks a satoshis-per-vbyte value."""
    assert "ceiling" in app._CONS_TIMING_FOLD
    for line in (app._CONS_TIMING_FOLD, app._CONS_TIMING_WAIT, app._CONS_TIMING_ACT):
        assert "sat/vb" not in line and "centisat" not in line


# =========================================================================
# 4. Routing — conservative matcher, opener priority, bypass, gates
# =========================================================================


def test_matcher_shape_pinned() -> None:
    assert app._consolidation_timing_ask("is now a good time to consolidate?") is True
    assert app._consolidation_timing_ask("should I consolidate now") is True
    assert app._consolidation_timing_ask("when should I consolidate?") is True
    assert app._consolidation_timing_ask("should I wait to consolidate?") is True
    # imperatives keep the ACTION route (opener or model), never advice:
    assert app._consolidation_timing_ask("consolidate now") is False
    assert app._consolidation_timing_ask("don't consolidate now") is False
    # the noun is the CFG-004 settings word — its questions keep that route:
    assert app._consolidation_timing_ask("what is the consolidation fee ceiling?") is False
    # bare digits keep the model's threshold route (the opener rule mirrored):
    assert app._consolidation_timing_ask("is now a good time to consolidate under 100000 sats?") is False
    # unrelated chat is untouched:
    assert app._consolidation_timing_ask("did the exchange confirm?") is False
    assert app._consolidation_timing_ask("how do I merge my utxos?") is False


def test_opener_keeps_priority_over_timing(world) -> None:
    """A phrasing the existing intercept already opens still OPENS (no
    golden behavior changes): object-worded "should I consolidate my
    coins?" is a conversation opener, never the advice answer."""
    _labeled(world)
    outs, fake, _ = _ask(world, "should I consolidate my coins?")
    assert world["session"].cons_ask is not None and world["session"].cons_ask.kind == "rollup"
    assert outs[0] == app._CONS_ROLLUP_HEAD
    assert app._CONS_TIMING_BYPASS not in outs
    assert fake.prompts == []


def test_bypass_routing_into_the_conversation(world) -> None:
    """The answer's own words are the way in: saying the canonical
    utterance the BYPASS line names runs the EXISTING intercept —
    TCK-CONS-003 re-pins its endpoint as the ONE threshold ask (still
    fully intercepted, zero model prompts); the stated number then plans
    (the card, not the advice, carries every figure)."""
    _labeled(world)
    world["state"]["mempool_blocks"] = _blocks([10.0, 8.0, 5.0, 4.0, 3.0, 2.0])
    outs, *_ = _ask(world, "is now a good time to consolidate?")
    assert app._CONS_TIMING_BYPASS in outs and "consolidate my small utxos" in outs[-1]
    # the recommended path is already intercepted — no new vocabulary:
    assert app._consolidation_intent("consolidate my small utxos") == (
        None,
        None,
        True,
        (),
        None,
        "",
    )
    outs2, fake2, _ = _ask(world, "consolidate my small utxos")
    assert fake2.prompts == []  # consumed by the existing intercept
    assert world["session"].cons_ask is not None  # the ONE threshold ask
    assert app._CONS_THRESHOLD_ASK.format(scope="") in outs2
    outs3, fake3, _ = _ask(world, "100000")
    assert fake3.prompts == []  # the answer is intercepted too
    assert world["flow"].state is TxFlowStatus.CREATED
    assert any("Merge" in ln or "Fee" in ln for ln in outs3)


def test_gate_territory_and_no_estimator_stand_down(world) -> None:
    """While a plan PENDS the pending card owns the turn (the timing
    answer stands down exactly like the opener does), and with no
    estimator wired (the bare harness route) the question keeps today's
    model path — the status quo is pinned in both directions."""
    _labeled(world)
    _ask(world, "consolidate my kyc coins")
    _ask(world, "all")
    assert world["flow"].state is TxFlowStatus.CREATED
    _, fake, _ = _ask(world, "is now a good time to consolidate?")
    assert len(fake.prompts) == 1  # not intercepted while a card pends
    _ask(world, "cancel")
    # and WITHOUT an estimator (the CONS-001 _turn helper) it is ordinary chat:
    _ask(world, "cancel")
    _, fake2, _ = _turn(world, "is now a good time to consolidate?")
    assert len(fake2.prompts) == 1


def test_unscanned_wallet_falls_through() -> None:
    """No scan cursor → the answer (like the opener) stands down: the lazy
    scan's model route answers honestly instead."""
    addrs = derive_fixture_addresses(8)
    state: dict[str, Any] = {}
    table, store, wallet, client, _recorded, flow, session, _signer = _hwi_table(
        {addrs[0]: [_utxo("d" * 64, 0, 100_000)]}, state=state
    )
    try:
        assert store.get_sync_state(wallet.id, app.wallet_scan.CURSOR_KEY) is None
        fake = _FakeGen()
        loop = AgentLoop(fake, table)
        app._run_turn(
            loop, flow, session, "is now a good time to consolidate?", [].append,
            table=table, store=store, fee_estimator=FeeEstimator(client),
        )
        assert session.cons_ask is None
        assert len(fake.prompts) == 1
    finally:
        store.close()
        client.close()


# =========================================================================
# 5. Zero extra chain calls — the answer rides the ONE estimator snapshot
# =========================================================================


def test_timing_answer_fetches_nothing_extra(world) -> None:
    """One timing turn = ONE regular refresh of the SHARED estimator (the
    recommended + mempool-blocks pair — the same two calls every card
    path makes); a second turn on the same cached snapshot costs ZERO.
    No new endpoint, no new chain surface, no second estimator."""
    _labeled(world)
    world["state"]["mempool_blocks"] = _blocks([2.0] * 6)
    _ask(world, "get_balance-turn-warmup")  # not a timing line; the model answers
    base = len(_fee_gets(world))
    _ask(world, "is now a good time to consolidate?")
    after_first = _fee_gets(world)
    assert len(after_first) - base == 2  # exactly ONE snapshot refresh
    _ask(world, "should I consolidate now")
    assert len(_fee_gets(world)) == len(after_first)  # cache serves, zero fetches


def test_registry_stays_closed(world) -> None:
    """The closed-intent registry stays CLOSED through all of this: the
    timing answer added NO intent (the count holds at 15), no envelope can
    carry it — only the pre-model intercept produces it."""
    from localwallet.protocol import EnvelopeValidationError, validate_payload

    assert len(list(IntentName)) == 15
    with pytest.raises(EnvelopeValidationError):
        validate_payload(json.dumps({"v": 0, "intent": "consolidate_now", "params": {}}))
    assert IntentName.SELF_TRANSFER in world["table"]

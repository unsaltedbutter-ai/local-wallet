"""TCK-CHAT-002 — consolidate BY REGISTRY NUMBER / "my small utxos" + the
elevated-fee warning.

Rides the EXISTING TCK-CONS-001 conversation harness (production dispatch
table, mock chain, fake-device signer — no network, deterministic). Every
done-when criterion of the ticket gets its own beat:

* the registry-number opener ("consolidate address 3 & 9") resolves against
  the STABLE CHAT-001 registry, restates the FULL address at every
  resolution (glm #7), refuses an unknown number with the value-free
  clarify (never a nearest guess), and refuses a cross-KYC-pool pick
  (TCK-TX-SELF-001: one pool at a time) — the plan rides the same
  dispatcher-owned picked-set revalidation as the list ask;
* "consolidate my small utxos" filters by the user's OWN consolidation
  target (utxo_target_min_sats over the settings ladder — never a
  hardcoded size) and dispatches the threshold envelope the existing
  handler policy already owns (pool sides never mix, larger side wins);
* the plan preview lists every source BY NUMBER + FULL address (designer
  §3a rows), value verbatim from the handler's own store read;
* the §3 fee narration: the consolidation fee line ("…the cheapest rate
  that confirmed reliably over roughly the last six hours…", never a
  promise), the ELEVATED-FEE WARNING when the plan's final bid sits above
  the chain layer's six-hour average of per-block lowest fees (§3b, hedged —
  no fabricated probability), the calm line when it doesn't (§3c,
  softened) — and NO fee claim at all when the per-block data is absent
  (fail closed);
* COUNCIL CONSENSUS #1: 'later' is NOT taught anywhere (warning is
  narration-only) — the gate vocabulary stays confirm/cancel, a "cancel"
  after the warning closes the plan, "later" never acts as a decision.

The whole file is transcript-free on the intercept paths: the model never
sees an opener line, an answer, or a label.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from localwallet import app
from localwallet.protocol import IntentName, validate_payload
from localwallet.tx.flow import TxFlowStatus
from tests.test_cons001_conversation import (
    _coin,
    _labeled,
    _turn,
)
from tests.test_cons001_conversation import (
    world as _cons_world,  # noqa: F401 — the CONS-001 harness fixture, injected by name
)
from tests.test_e2e_skeleton import derive_fixture_addresses


@pytest.fixture()
def world(_cons_world):  # noqa: F811 — fixture alias, not a shadowing arg
    yield _cons_world

# =========================================================================
# helpers
# =========================================================================


def _env(intent: str, params: dict[str, Any]):
    return validate_payload(json.dumps({"v": 0, "intent": intent, "params": params}))


def _blocks(bottoms: list[float]) -> list[dict[str, Any]]:
    """A mempool-blocks payload with the given per-block feeRange bottoms
    (the endpoint's shape; the estimator already validated ordering)."""
    return [
        {
            "blockSize": 998_000,
            "medianFee": b * 2,
            "feeRange": [b, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0],
        }
        for b in bottoms
    ]


def _show(world, *addresses: str) -> dict[str, int]:
    """Register first-showings the way every showing surface does, and
    return address -> stable number."""
    return {
        a: world["store"].note_address_shown(world["wallet"].id, a).number
        for a in addresses
    }


# =========================================================================
# 1. Consolidate BY REGISTRY NUMBER (designer §3a; CHAT-001 invariants)
# =========================================================================


def test_number_opener_restates_full_addresses_and_plans(world) -> None:
    """The user's own numbers are the referents: EVERY resolution prints
    the FULL address (glm #7 — never number-only), and the plan preview
    lists the sources BY NUMBER + FULL ADDRESS, values verbatim."""
    _labeled(world)
    addrs = derive_fixture_addresses(8)
    numbers = _show(world, addrs[2], addrs[3])  # the two kyc coins' addresses
    n1, n2 = numbers[addrs[2]], numbers[addrs[3]]
    outs, fake, loop = _turn(world, f"consolidate address {n1} & {n2}")
    # transcript-free: the opener (and its numbers) never reach the model
    assert fake.prompts == [] and loop.history == ()
    # the resolution restatements: #N + FULL address + sats, one per coin
    assert f"Coin #{n1} at {addrs[2]} — 12,000 sats." in outs
    assert f"Coin #{n2} at {addrs[3]} — 30,000 sats." in outs
    assert world["flow"].state is TxFlowStatus.CREATED
    body = "\n".join(outs)
    # §3a rows: every source named by its STABLE number AND full address
    assert app._CONS_SOURCE_HEADER in outs
    assert f"  #{n1}. {addrs[2]} · 12,000 sats" in outs
    assert f"#{n2}. {addrs[3]} · 30,000 sats" in body
    # the numbers on the card are the registry's, not per-list positions
    assert world["store"].registry_number_for(world["wallet"].id, addrs[2]).number == n1


def test_number_with_no_coins_names_it_honestly(world) -> None:
    """A resolved registry number whose address holds nothing gets the
    value-free 'no coins there' notice — never folded into a generic
    empty; the plan proceeds on the addresses that DO hold coins."""
    _labeled(world)
    addrs = derive_fixture_addresses(8)
    numbers = _show(world, addrs[2], addrs[6])  # addrs[6] has no coins
    outs, *_ = _turn(
        world, f"consolidate address {numbers[addrs[6]]} and {numbers[addrs[2]]}"
    )
    assert app._CONS_NO_COINS_AT.format(number=numbers[addrs[6]]) in outs
    assert world["flow"].state is TxFlowStatus.CREATED  # the 12k coin plans on


def test_unknown_number_clarifies_never_plans(world) -> None:
    """A number outside the registry is the existing CHAT-001 value-free
    clarify (never a nearest match) and stages NOTHING."""
    _labeled(world)
    outs, fake, _ = _turn(world, "consolidate address 99")
    assert outs == [app.ADDRESS_REF_UNKNOWN]
    assert world["flow"].state is not TxFlowStatus.CREATED
    assert fake.prompts == [] and world["session"].cons_ask is None


def test_cross_pool_numbers_are_refused(world) -> None:
    """A pick spanning the KYC mark is refused value-free — consolidation
    merges ONE privacy pool at a time (TCK-TX-SELF-001 policy), and the
    opener never silently drops the other side."""
    _labeled(world)
    addrs = derive_fixture_addresses(8)
    numbers = _show(world, addrs[0], addrs[2])  # unlabeled 100k + kyc 12k
    outs, *_ = _turn(
        world, f"consolidate address {numbers[addrs[0]]} & {numbers[addrs[2]]}"
    )
    assert outs == [app._CONS_POOLS_APART]
    assert world["flow"].state is not TxFlowStatus.CREATED
    assert world["session"].cons_ask is None


def test_cross_pool_pick_with_empty_address_shows_only_the_refusal(world) -> None:
    """TCK-CHAT-002 FINDING 4: a cross-pool pick where one named address
    holds no coins shows ONLY the pool refusal — the no-coins notice is
    suppressed so the one real message is not muddied (both fail closed)."""
    _labeled(world)
    addrs = derive_fixture_addresses(8)
    numbers = _show(world, addrs[0], addrs[2], addrs[6])  # addrs[6] empty
    outs, *_ = _turn(
        world,
        f"consolidate address {numbers[addrs[0]]} & {numbers[addrs[2]]} "
        f"& {numbers[addrs[6]]}",
    )
    assert outs == [app._CONS_POOLS_APART]
    assert app._CONS_NO_COINS_AT.format(number=numbers[addrs[6]]) not in outs
    assert world["flow"].state is not TxFlowStatus.CREATED


def test_bare_digit_threshold_stays_on_the_model_route(world) -> None:
    """Address-wordless digits are a SIZE threshold, not a registry pick:
    the TX-SELF-001 golden phrasing keeps its existing model route."""
    _labeled(world)
    _, fake, _ = _turn(world, "consolidate all my coins under 100000 sats")
    assert len(fake.prompts) == 1


# =========================================================================
# 2. "consolidate my small utxos" — the settings-filtered path
# =========================================================================


def test_small_utxos_filters_by_the_target_setting(world) -> None:
    """SMALL = the user's OWN consolidation target (utxo_target_min_sats,
    stored rung wins over the shipped 100k default — proving the setting
    drives it); the pick rides the EXISTING threshold handler policy
    (pool sides never mix, larger side wins, honest other-side hint)."""
    _labeled(world)
    world["store"].set_coin_setting("utxo_target_min_sats", "20000")
    # below 20k: the 12k kyc coin + the 7k unlabeled coin (pools apart)
    outs, fake, loop = _turn(world, "consolidate my small utxos")
    assert fake.prompts == [] and loop.history == ()  # deterministic intercept
    assert world["flow"].state is TxFlowStatus.CREATED
    pending = world["flow"].pending
    assert pending is not None
    # the larger-total (kyc) side wins alone — 12k minus the fee, never a
    # silent cross-pool merge of the 7k coin
    assert pending.inputs_count == 1
    assert 0 < pending.amount_sats < 12_000
    assert any("small coin" in ln and "other marked coins" in ln for ln in outs)
    # §3a: even this threshold path restates its one source fully
    assert any(" · 12,000 sats" in ln for ln in outs)


def test_small_utxos_empty_wallet_answer_honest(world) -> None:
    """Nothing under the target is the EXISTING handler answer ('None of
    your coins are smaller than that…') — no plan, no crash, no model."""
    _coin(world, "a1" + "0" * 62, 500_000, index=3)  # one big coin only
    world["store"].set_coin_setting("utxo_target_min_sats", "20000")
    outs, fake, _ = _turn(world, "consolidate my small utxos")
    assert app._SELF_NOTHING_BELOW in outs
    assert world["flow"].state is not TxFlowStatus.CREATED
    assert fake.prompts == []


# =========================================================================
# 3. Fee narration — elevated / calm / fail-closed (designer §3a-c)
# =========================================================================


def test_elevated_fee_warning_fires_and_hedges(world) -> None:
    """The consolidation DEFAULT rung is SLOW (orchestrator ruling), so the
    elevated warning now fires on a STEEPLY dropping projection: the SLOW
    bid (B₁ 4.0 → 400) sits ABOVE the six-hour average of per-block lowest
    fees ((5+4+1+1+1+1)/6 → 217) → the §3a fee line plus the §3b hedged
    warning render. No figure, no probability, no new gate word."""
    _labeled(world)
    world["state"]["mempool_blocks"] = _blocks(
        [5.0, 4.0, 1.0, 1.0, 1.0, 1.0]
    )
    outs, *_ = _turn(world, "consolidate my small utxos")
    assert world["flow"].state is TxFlowStatus.CREATED
    assert app._CONS_FEE_LOW_LINE in outs
    assert app._CONS_FEE_ELEVATED_LINE in outs
    assert app._CONS_FEE_CALM_LINE not in outs
    hedged = app._CONS_FEE_ELEVATED_LINE.lower()
    assert "certain" in hedged or "might" in hedged  # hedge, never a promise
    assert not any(ch.isdigit() for ch in app._CONS_FEE_ELEVATED_LINE)


def test_calm_line_when_bid_at_or_below_average(world) -> None:
    """A flat projected floor (all bottoms 2.0) makes the six-hour average
    200 and the SLOW bid exactly 200 — at-or-below → the §3c softened calm
    line, never the warning."""
    _labeled(world)
    world["state"]["mempool_blocks"] = _blocks([2.0] * 6)
    outs, *_ = _turn(world, "consolidate my small utxos slowly")
    assert world["flow"].state is TxFlowStatus.CREATED
    assert app._CONS_FEE_LOW_LINE in outs
    assert app._CONS_FEE_CALM_LINE in outs
    assert app._CONS_FEE_ELEVATED_LINE not in outs
    assert "no need to wait on fees" in app._CONS_FEE_CALM_LINE  # §3c verbatim


def test_no_per_block_data_no_fee_claim(world) -> None:
    """mempool-blocks unreachable (the recommended fallback): the plan is
    byte-unchanged and NO fee claim of any kind renders — the six-hour
    bound does not exist to promise or to compare against. Fail closed."""
    _labeled(world)  # state has no "mempool_blocks" route -> 404 -> fallback
    outs, *_ = _turn(world, "consolidate my small utxos")
    assert world["flow"].state is TxFlowStatus.CREATED
    assert app._CONS_FEE_LOW_LINE not in outs
    assert app._CONS_FEE_ELEVATED_LINE not in outs
    assert app._CONS_FEE_CALM_LINE not in outs
    # the threshold path stages the re-show marker too (TCK-CHAT-002
    # FINDING 3) but its display carries NO fee claim — the six-hour bound
    # does not exist to compare against
    assert world["session"].cons_pending is not None
    assert "cons_fee_note" not in world["session"].cons_pending.display


def test_consolidation_defaults_slow_and_card_shows_slow_rate(world) -> None:
    """ORCHESTRATOR RULING: the consolidation flow's DEFAULT fee rung is
    SLOW (not MEDIUM) — so the §3a copy "bids the cheapest rate that
    confirmed reliably…" is TRUE of the actual bid. The card's Fee row
    shows the SLOW rate (2.0 sat/vB here, not MEDIUM's 2.3) and the slow
    target word."""
    _labeled(world)
    world["state"]["mempool_blocks"] = _blocks([2.0] * 6)
    outs, *_ = _turn(world, "consolidate my small utxos")
    pending = world["flow"].pending
    assert pending is not None and pending.fee_target == app.FeeTarget.SLOW.value
    fee_line = next(ln for ln in outs if ln.startswith("Fee:"))
    assert "· 2 sat/vB" in fee_line  # SLOW (200c), NOT MEDIUM (230c → "2.3")
    assert "· slow" in fee_line


def test_consolidation_explicit_faster_overrides_default(world) -> None:
    """An EXPLICIT user rung still wins over the SLOW default: "faster"
    names FAST, the plan stages FAST, and the card shows the fast rate."""
    _labeled(world)
    world["state"]["mempool_blocks"] = _blocks([2.0] * 6)
    outs, *_ = _turn(world, "consolidate my small utxos faster")
    pending = world["flow"].pending
    assert pending is not None and pending.fee_target == app.FeeTarget.FAST.value
    fee_line = next(ln for ln in outs if ln.startswith("Fee:"))
    assert "· fast" in fee_line


def test_small_utxos_pending_reshow_carries_sources(world) -> None:
    """TCK-CHAT-002 FINDING 3: the small-utxos (threshold) path stages the
    SAME re-show record the number/list paths do, so a pending re-show
    carries the §3a source rows + fee lines (the initial card always had
    them; the re-show must too)."""
    _labeled(world)
    world["store"].set_coin_setting("utxo_target_min_sats", "20000")
    world["state"]["mempool_blocks"] = _blocks([5.0, 4.0, 1.0, 1.0])
    first, *_ = _turn(world, "consolidate my small utxos")
    assert app._CONS_FEE_ELEVATED_LINE in first
    # a direct handler call (the create_tx-pending shape) re-shows the
    # staged threshold consolidation with its display fields
    result = world["table"][IntentName.SELF_TRANSFER](
        _env("self_transfer", {"mode": "consolidate", "below_size_sats": 20_000})
    )
    assert result["error"] == "tx_pending"
    assert result["cons_merge"] is True
    # the kyc 12k coin was the small-utxos pick; its §3a row re-shows
    assert app._CONS_SOURCE_HEADER in first
    assert any(" · 12,000 sats" in ln for ln in first)
    lines: list[str] = []
    app._print_self_plan(result, lines.append)
    assert app._CONS_SOURCE_HEADER in lines
    assert any(" · 12,000 sats" in ln for ln in lines)
    assert app._CONS_FEE_ELEVATED_LINE in lines


def test_fee_line_never_promises_confirmation(world) -> None:
    """glm #6 softening pins: the §3a fee line carries the estimate hedge
    and the calm line makes no timing claim either."""
    assert "never a promise of confirmation" in app._CONS_FEE_LOW_LINE
    assert not any(ch.isdigit() for ch in app._CONS_FEE_LOW_LINE)
    assert not any(ch.isdigit() for ch in app._CONS_FEE_CALM_LINE)


# =========================================================================
# 4. The gate is untouched (COUNCIL CONSENSUS #1 — no 'later')
# =========================================================================


def test_later_is_never_a_gate_word(world) -> None:
    """After the elevated-fee warning (whose designer affordance WAS a
    'Later' button): 'later' is NOT a decision — the plan stays pending
    (the warning is narration-only) and the gate vocabulary still belongs
    to confirm/cancel alone."""
    _labeled(world)
    world["state"]["mempool_blocks"] = _blocks([5.0, 4.0, 1.0, 1.0])
    outs, *_ = _turn(world, "consolidate my small utxos")
    assert app._CONS_FEE_ELEVATED_LINE in outs
    for word in ("later", "maybe", "not now"):
        _turn(world, word)
        assert world["flow"].state is TxFlowStatus.CREATED  # never confirmed
    # cancel still owns the way out (the plan just closes)
    _turn(world, "cancel")
    assert world["flow"].state is not TxFlowStatus.CREATED
    assert world["session"].cons_pending is None


def test_warning_copy_teaches_no_new_vocabulary() -> None:
    """Every §3 line is value-free and word-closed: no digits, no
    addresses, and 'later' appears nowhere in the consolidation copy."""
    for line in (
        app._CONS_FEE_LOW_LINE,
        app._CONS_FEE_ELEVATED_LINE,
        app._CONS_FEE_CALM_LINE,
        app._CONS_SOURCE_HEADER,
        app._CONS_POOLS_APART,
    ):
        assert not any(ch.isdigit() for ch in line), line
        assert "bc1" not in line.lower()
        assert "later" not in line.lower(), line
    # the gate ask line on the warning card is byte-identical to every
    # other card's (confirm/cancel/sign vocabulary UNCHANGED)
    assert app._CARD_ASK_LINE == (
        'Pending — say "sign" to review it on your device, or "cancel" to discard.'
    )


def test_pending_reshow_carries_sources_and_fee_lines(world) -> None:
    """A second consolidate attempt while the plan pends re-shows THE SAME
    card — §3a source rows and fee narration included (they ride the
    dispatcher-owned pending record; no re-registration, no chain call)."""
    _labeled(world)
    addrs = derive_fixture_addresses(8)
    numbers = _show(world, addrs[2], addrs[3])
    world["state"]["mempool_blocks"] = _blocks([5.0, 4.0, 1.0, 1.0])
    first, *_ = _turn(world, f"consolidate address {numbers[addrs[2]]} & {numbers[addrs[3]]}")
    assert app._CONS_FEE_ELEVATED_LINE in first
    # a direct handler call (the create_tx-pending shape): tx_pending
    # re-show carries the staged consolidation's display fields
    result = world["table"][IntentName.SELF_TRANSFER](
        _env("self_transfer", {"mode": "consolidate", "below_size_sats": 20_000})
    )
    assert result["error"] == "tx_pending"
    assert result["cons_merge"] is True
    n1, n2 = numbers[addrs[2]], numbers[addrs[3]]
    assert result["self_sources"] == [
        {"number": n1, "address": addrs[2], "value_sats": 12_000},
        {"number": n2, "address": addrs[3], "value_sats": 30_000},
    ]
    assert result["cons_fee_note"] is True and result["cons_fee_state"] == "elevated"
    lines: list[str] = []
    app._print_self_plan(result, lines.append)
    assert app._CONS_SOURCE_HEADER in lines
    assert f"  #{n1}. {addrs[2]} · 12,000 sats" in lines
    assert app._CONS_FEE_ELEVATED_LINE in lines


# =========================================================================
# 5. Cross-pool threshold honesty survives the new openers (regression)
# =========================================================================


def test_small_utxos_spanning_pools_takes_the_larger_side(world) -> None:
    """Default target 100k over the _labeled set: small coins sit on BOTH
    sides (kyc 42k vs other 52k) — the EXISTING policy consolidates the
    larger side and the card honestly says the other side waits. The
    small-utxos opener did not invent a second selection rule."""
    _labeled(world)
    world["store"].add_address_labels(  # keep p2p on the OTHER side (it is)
        "bc1qneverused" + "0" * 22, ["nothing"]
    )
    outs, *_ = _turn(world, "consolidate my small utxos")
    pending = world["flow"].pending
    assert pending is not None and pending.inputs_count == 2  # 45k + 7k side
    assert any("other marked coins" in ln for ln in outs)

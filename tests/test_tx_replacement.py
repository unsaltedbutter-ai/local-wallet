"""BIP-125 replacement-builder tests (TCK-RBF-002).

Binding list from the ledger row: floor math (old fee ≥/≤ the relay
increment), change-trim exactness (fee delta verbatim integers),
change-removal dust fold, add-input (chosen coin, determinism), floor
refusal when even the chosen coin can't reach the floor, rate-below-floor
refusal with the floor number, 0xfffffffd signaling on every input,
vsize/fee integer-exactness incl. fractional centisat rates, and
deterministic replay (same inputs -> field-identical plan).

vsize expectations are recomputed from independent component arithmetic
(mirroring the ADR-0012 §4 accounting) — never from the module under test.
"""

import math
from dataclasses import dataclass, replace

import pytest

from localwallet.tx.dust import dust_threshold, min_relay_fee_vbytes
from localwallet.tx.psbt import SEQUENCE_RBF_ENABLED
from localwallet.tx.replacement import (
    OriginalTx,
    RbfFloorError,
    RbfRefusalReason,
    RbfReplacementMode,
    ReplacementError,
    build_replacement_plan,
    rbf_min_fee_sats,
)

RECIPIENT = b"\x00\x14" + b"\x11" * 20  # P2WPKH recipient script
CHANGE = b"\x00\x14" + b"\x22" * 20  # P2WPKH change script
CHANGE_DUST = dust_threshold(CHANGE)  # computed, not the 294 literal


@dataclass(frozen=True)
class Coin:
    txid: str
    vout: int
    value_sats: int


def coin(n: int, value_sats: int, vout: int = 0) -> Coin:
    return Coin(txid=f"{n:064x}", vout=vout, value_sats=value_sats)


def vsize_of(n_inputs: int, with_change: bool, n_recipients: int = 1) -> int:
    """Independent max-witness P2WPKH accounting (ADR-0012 §4)."""
    weight = 4 * (4 + 1 + 1 + 4) + 2  # overhead, 1-byte varints (small counts)
    weight += n_inputs * (4 * 41 + (1 + 73 + 34))  # 272 WU per P2WPKH input
    weight += n_recipients * 4 * (8 + 1 + len(RECIPIENT))  # recipient outputs
    if with_change:
        weight += 4 * (8 + 1 + len(CHANGE))  # change output (31 vB)
    return math.ceil(weight / 4)


def fee(vsize: int, rate_c: int) -> int:
    """Independent ceil(vsize * centisat / 100)."""
    return math.ceil(vsize * rate_c / 100)


def recorded(coins, amount, old_fee, rate_c, *, with_change=True):
    """A conservation-true OriginalTx at (coins, amount, old_fee)."""
    total = sum(c.value_sats for c in coins)
    change = total - amount - old_fee
    assert change >= CHANGE_DUST or not with_change
    return OriginalTx(
        inputs=tuple(coins),
        recipients=((RECIPIENT, amount),),
        change_script=CHANGE if with_change else None,
        change_sats=change if with_change else None,
        fee_sats=old_fee,
        vsize=vsize_of(len(coins), with_change),
    )


class TestFloorMath:
    def test_old_fee_at_or_above_increment_doubles_the_fee(self):
        # Typical case: the original paid >= min relay, so old_fee (600)
        # dominates the vsize increment (141): floor = 2 * old_fee.
        vsize = vsize_of(1, True)
        assert rbf_min_fee_sats(600, vsize) == 1200

    def test_old_fee_below_increment_uses_relay_size(self):
        # Degenerate original parked under 1 sat/vB: old_fee (50) < the
        # vsize increment, so the increment branch binds.
        vsize = vsize_of(1, True)
        assert rbf_min_fee_sats(50, vsize) == 50 + vsize

    def test_increment_is_min_relay_of_new_size_not_a_constant(self):
        # The increment term is exactly the dust module's size-derived fee
        # at the replacement-only 1 sat/vB rate (CENTISAT unit post
        # TCK-FEE-005; the BIP-125 increment is DISTINCT from the 0.1
        # sat/vB initial-bid rail — conservative on purpose).
        for vsize in (110, 141, 178, 209, 555):
            assert rbf_min_fee_sats(1, vsize) == 1 + min_relay_fee_vbytes(
                vsize, min_relay_centisat_vb=100
            )

    def test_floor_refuses_bad_arguments(self):
        with pytest.raises(ReplacementError):
            rbf_min_fee_sats(0, 141)  # zero/negative old fee
        with pytest.raises(ReplacementError):
            rbf_min_fee_sats(1000, 100_001)  # over Core standardness bound
        with pytest.raises(ReplacementError):
            rbf_min_fee_sats(True, 141)  # bool is not an int here


class TestChangeTrim:
    def test_trim_fee_delta_is_verbatim_integer_math(self):
        # 1-in, 61_000 total... canonical shape: 100_000 in, 60_000 out.
        orig = recorded([coin(1, 100_000)], 60_000, 282, 200)
        plan = build_replacement_plan(orig, 500)  # 5 sat/vB
        vsize = vsize_of(1, True)
        assert plan.mode is RbfReplacementMode.CHANGE_TRIM
        assert plan.fee_sats == fee(vsize, 500) == 705
        assert plan.fee_sats - orig.fee_sats == 705 - 282 == 423
        assert plan.change_sats == 100_000 - 60_000 - 705 == 39_295
        assert plan.vsize == vsize
        assert plan.fee_rate_centisat_vb == 500
        assert plan.funding_coin is None
        # Recipients verbatim, change last.
        assert plan.outputs == ((RECIPIENT, 60_000), (CHANGE, 39_295))

    def test_fee_exactly_equal_to_floor_is_accepted(self):
        orig = recorded([coin(1, 100_000)], 60_000, 282, 200)
        floor = rbf_min_fee_sats(282, vsize_of(1, True))  # 564
        rate_c = floor * 100 // vsize_of(1, True)  # 400 -> 141*4 = 564 exact
        assert fee(vsize_of(1, True), rate_c) == floor
        plan = build_replacement_plan(orig, rate_c)
        assert plan.fee_sats == floor

    def test_fractional_centisat_rate_ceil_is_exact(self):
        orig = recorded([coin(1, 61_000)], 60_000, 150, 100)
        plan = build_replacement_plan(orig, 255)  # 2.55 sat/vB
        vsize = vsize_of(1, True)  # 141 — odd, so the ceil binds
        assert plan.vsize == vsize == 141
        assert plan.fee_sats == math.ceil(141 * 2.55) == 360
        assert plan.change_sats == 1000 - 360 == 640
        assert plan.mode is RbfReplacementMode.CHANGE_TRIM

    def test_trim_below_floor_rate_refuses_with_floor_number(self):
        # item 4: chosen rate implies 282 sats, floor is 564.
        orig = recorded([coin(1, 100_000)], 60_000, 282, 200)
        with pytest.raises(RbfFloorError) as excinfo:
            build_replacement_plan(orig, 200)
        err = excinfo.value
        assert err.reason is RbfRefusalReason.RATE_BELOW_FLOOR
        assert err.floor_sats == rbf_min_fee_sats(282, vsize_of(1, True)) == 564
        assert err.max_payable_sats == 40_000
        assert "564" in str(err)  # user-facing UI, ADR-0012 §7 precedent


class TestChangeFold:
    def test_dust_below_threshold_change_folds_into_fee(self):
        # Rate clears the floor but the trimmed change (244) is below the
        # computed dust of the change script -> the whole residue folds.
        orig = recorded([coin(1, 62_500)], 60_000, 1_000, 1_000)
        assert orig.change_sats == 1_500
        trimmed = 2_500 - fee(vsize_of(1, True), 1_600)
        assert trimmed < CHANGE_DUST  # the fold precondition itself
        plan = build_replacement_plan(orig, 1_600)
        assert plan.mode is RbfReplacementMode.CHANGE_FOLD
        assert plan.change_sats is None
        # Verbatim residue math: fee becomes old_fee + old_change.
        assert plan.fee_sats == 1_000 + 1_500 == 2_500
        assert plan.fee_sats - orig.fee_sats == 1_500
        assert plan.vsize == vsize_of(1, False)  # smaller shape: 31 vB gone
        assert plan.outputs == ((RECIPIENT, 60_000),)

    def test_fold_never_launderers_a_below_floor_rate(self):
        # Rate below floor AND residue above floor: folding to an
        # over-the-rate fee is a refusal (item 4), not a silent overpay.
        orig = recorded([coin(1, 61_000)], 60_000, 200, 100)  # payable 1000
        assert orig.change_sats == 800
        with pytest.raises(RbfFloorError) as excinfo:
            build_replacement_plan(orig, 150)  # fee 212 < floor 400
        assert excinfo.value.reason is RbfRefusalReason.RATE_BELOW_FLOOR


class TestAddInput:
    def test_change_only_shortfall_appends_the_chosen_coin(self):
        # Original: 61_000 in / 60_000 out -> residue 1000 < floor 1400.
        orig = recorded([coin(9, 61_000)], 60_000, 700, 200)
        assert orig.change_sats == 300
        chosen = coin(2, 5_000)  # smaller txid: must sort FIRST
        plan = build_replacement_plan(orig, 1_000, funding_coin=chosen)
        assert plan.mode is RbfReplacementMode.ADD_INPUT
        assert [c.txid for c in plan.inputs] == [chosen.txid, orig.inputs[0].txid]
        assert plan.funding_coin is chosen
        vsize2 = vsize_of(2, True)
        assert plan.fee_sats == fee(vsize2, 1_000) == 2_090
        assert plan.fee_sats >= rbf_min_fee_sats(700, vsize2) == 1_400
        assert plan.change_sats == 66_000 - 60_000 - 2_090 == 3_910
        assert plan.vsize == vsize2
        assert sum(c.value_sats for c in plan.inputs) == (
            sum(v for _s, v in plan.outputs) + plan.fee_sats
        )

    def test_add_input_fold_when_trimmed_change_goes_dust(self):
        # Coin appended, but the chosen rate eats change below dust:
        # ADD_INPUT with the change output removed.
        orig = recorded([coin(1, 61_000)], 60_000, 700, 200)
        plan = build_replacement_plan(orig, 2_300, funding_coin=coin(2, 4_000))
        assert plan.mode is RbfReplacementMode.ADD_INPUT
        assert plan.change_sats is None
        assert plan.fee_sats == 5_000  # whole residue, verbatim integer
        assert plan.fee_sats >= rbf_min_fee_sats(700, plan.vsize)
        assert plan.vsize == vsize_of(2, False)

    def test_changeless_original_bumps_by_coin_value(self):
        orig = OriginalTx(
            inputs=(coin(1, 61_000),),
            recipients=((RECIPIENT, 60_000),),
            change_script=None,
            change_sats=None,
            fee_sats=1_000,
            vsize=vsize_of(1, False),
        )
        plan = build_replacement_plan(orig, 200, funding_coin=coin(2, 2_000))
        assert plan.mode is RbfReplacementMode.ADD_INPUT
        assert plan.fee_sats == 3_000  # old fee + all of the coin
        assert plan.fee_sats >= rbf_min_fee_sats(1_000, vsize_of(2, False))
        assert plan.outputs == ((RECIPIENT, 60_000),)

    def test_even_the_chosen_coin_cannot_reach_the_floor_refuses(self):
        # item 2c: residue with the coin (1300) is under the floor (1400).
        orig = recorded([coin(9, 61_000)], 60_000, 700, 200)
        with pytest.raises(RbfFloorError) as excinfo:
            build_replacement_plan(orig, 1_000, funding_coin=coin(2, 300))
        err = excinfo.value
        assert err.reason is RbfRefusalReason.FUNDING_BELOW_FLOOR
        assert err.floor_sats == rbf_min_fee_sats(700, vsize_of(2, False)) == 1_400
        assert err.max_payable_sats == 1_300

    def test_rate_above_funding_refuses_without_underbidding(self):
        # The rate clears the floor but is unpayable from the residue:
        # never fold to a fee UNDER the chosen rate.
        orig = recorded([coin(1, 61_000)], 60_000, 200, 100)
        with pytest.raises(RbfFloorError) as excinfo:
            build_replacement_plan(orig, 100_000)  # 1000 sat/vB
        assert excinfo.value.reason is RbfRefusalReason.RATE_EXCEEDS_FUNDING
        assert excinfo.value.max_payable_sats == 1_000


class TestSignaling:
    def test_every_input_carries_the_rbf_sequence(self):
        orig = recorded([coin(1, 55_000), coin(2, 45_000)], 60_000, 423, 200)
        trimmed = build_replacement_plan(orig, 500)
        assert trimmed.input_sequences == (SEQUENCE_RBF_ENABLED,) * 2
        short = recorded([coin(9, 61_000)], 60_000, 700, 200)
        bumped = build_replacement_plan(short, 1_000, funding_coin=coin(2, 5_000))
        assert len(bumped.inputs) == 2
        assert bumped.input_sequences == (SEQUENCE_RBF_ENABLED,) * 2
        assert all(s == 0xFFFFFFFD for s in bumped.input_sequences)


class TestDeterminism:
    def _orig(self, coins):
        return OriginalTx(
            inputs=tuple(coins),
            recipients=((RECIPIENT, 60_000), (RECIPIENT, 1_200)),
            change_script=CHANGE,
            change_sats=sum(c.value_sats for c in coins) - 61_200 - 500,
            fee_sats=500,
            vsize=vsize_of(len(coins), True, n_recipients=2),
        )

    def test_same_inputs_replay_identically(self):
        coins = [coin(3, 40_000), coin(1, 30_000), coin(2, 50_000)]
        first = build_replacement_plan(self._orig(coins), 455)
        again = build_replacement_plan(self._orig(coins), 455)
        assert first == again  # frozen dataclasses: field-for-field

    def test_record_input_order_never_leaks_into_the_plan(self):
        coins = [coin(3, 40_000), coin(1, 30_000), coin(2, 50_000)]
        plan_a = build_replacement_plan(self._orig(coins), 455)
        plan_b = build_replacement_plan(self._orig(list(reversed(coins))), 455)
        assert plan_a == plan_b
        assert [c.txid for c in plan_a.inputs] == sorted(
            c.txid for c in plan_a.inputs
        )


class TestRecordValidation:
    def test_corrupt_records_are_refused(self):
        good = recorded([coin(1, 100_000)], 60_000, 282, 200)
        with pytest.raises(ReplacementError):
            build_replacement_plan(replace(good, vsize=good.vsize + 1), 500)
        with pytest.raises(ReplacementError):
            build_replacement_plan(replace(good, fee_sats=281), 500)  # conservation
        with pytest.raises(ReplacementError):
            build_replacement_plan(replace(good, change_sats=100), 500)  # sub-dust record

    def test_bad_requests_are_refused(self):
        orig = recorded([coin(1, 100_000)], 60_000, 282, 200)
        with pytest.raises(ReplacementError):
            build_replacement_plan(orig, 0)  # below the centisat rate floor
        with pytest.raises(ReplacementError):
            build_replacement_plan(orig, 1_000_001)  # above the rate ceiling
        with pytest.raises(ReplacementError):  # funding = existing outpoint
            build_replacement_plan(orig, 500, funding_coin=coin(1, 9_000))
        with pytest.raises(ReplacementError):  # duplicate outpoints in the record
            build_replacement_plan(
                replace(orig, inputs=(coin(1, 100_000), coin(1, 100_000))), 500
            )
        with pytest.raises(ReplacementError):  # half a change pair
            build_replacement_plan(replace(orig, change_sats=None), 500)

    def test_fee_floor_dominates_min_relay_alone(self):
        # Sanity: any accepted plan also clears the plain min-relay floor
        # (the BIP-125 floor is strictly above it by construction).
        orig = recorded([coin(1, 100_000)], 60_000, 282, 200)
        plan = build_replacement_plan(orig, 500)
        assert plan.fee_sats > min_relay_fee_vbytes(plan.vsize)

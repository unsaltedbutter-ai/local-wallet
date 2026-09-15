"""Fractional-rate engine tests (TCK-FEE-003 wave, docs/fee-fractional-plan.md).

The tx engine takes integer CENTISAT/VB (1 sat/vB = 100): fees are
``ceil(vsize × c / 100)`` sats — integer-exact (no float money), identical to
the old whole-sat product for multiples of 100, and never under-bidding the
rate for fractional bids (sub-1 included).
"""

import pytest

from localwallet.chain.fees import FeeEstimator, FeeTarget
from localwallet.tx.psbt import PsbtError, build_unsigned_psbt
from localwallet.tx.selection import (
    InsufficientFundsError,
    SelectionError,
    fee_sats_for,
    select_coins,
)
from tests.test_tx_psbt import (  # shared fixture material (pattern: test_tx_revalidate)
    ACCOUNT_PATH,
    account_key,
    change_address,
    fingerprint,
    recipient_script,
    source,
)

RECIPIENT = b"\x00\x14" + b"\x11" * 20
CHANGE_COST = 31


class _SubFloorNative:
    """Backend-native estimator double (no get_json, no floor capability):
    its ``estimate_fee`` answers whole sats/vB, so the floor clamp falls
    back to the assumed 0.1 sat/vB (TCK-FEE-005) — exactly Electrum's
    honest-absence case."""

    def __init__(self, rates: dict) -> None:
        self._rates = dict(rates)

    def estimate_fee(self, target) -> int:
        return self._rates[target]


class Utxo:
    def __init__(self, txid: str, vout: int, value_sats: int):
        self.txid = txid
        self.vout = vout
        self.value_sats = value_sats


def utxo(n: int, value_sats: int) -> Utxo:
    return Utxo(txid=f"{n:064x}", vout=0, value_sats=value_sats)


@pytest.mark.parametrize(
    ("vsize", "centisat", "sats"),
    [
        (141, 200, 282),  # whole 2 sat/vB — byte-identical to the old product
        (141, 100, 141),  # whole 1 sat/vB
        (141, 121, 171),  # the user's 1.21 target: 170.61 -> 171 (never under)
        (141, 242, 342),  # 2.42 faster rung: 341.22 -> 342
        (141, 55, 78),  # sub-1: 0.55 sat/vB -> 77.55 -> 78
        (68, 1, 1),  # floor unit: 0.01 sat/vB still pays >= 1 sat
        (1, 1, 1),
        (1000, 10, 100),  # exactly divisible: no ceil drift
    ],
)
def test_fee_sats_for_ceil_exact(vsize, centisat, sats):
    assert fee_sats_for(vsize, centisat) == sats


def test_selection_bids_fractional_rate_exactly():
    result = select_coins([utxo(1, 200_000)], 60_000, 121, CHANGE_COST, RECIPIENT)
    # 1-in/2-out P2WPKH vsize is 141 -> fee = ceil(141 x 1.21) = 171 sats.
    assert result.estimated_vsize == 141
    assert result.fee_sats == 171
    assert result.change_sats == 200_000 - 60_000 - 171


def test_selection_accepts_sub_one_rate():
    result = select_coins([utxo(1, 200_000)], 60_000, 55, CHANGE_COST, RECIPIENT)
    assert result.fee_sats == 78  # ceil(141 x 0.55)
    assert result.change_sats == 200_000 - 60_000 - 78


def test_whole_sat_rates_are_unchanged_by_the_unit():
    # The unit change is value-preserving at whole sat/vB: 2 sat/vB on the
    # same wallet finalizes identically to the pre-TCK-FEE-003 numbers
    # (cf. tests/test_tx_selection.py).
    result = select_coins([utxo(1, 200_000)], 60_000, 200, CHANGE_COST, RECIPIENT)
    assert result.fee_sats == 282
    assert result.change_sats == 139_718


@pytest.mark.parametrize("bad", [0, -1, 1_000_001, 2.5, "121", True])
def test_rate_bounds_and_types_fail_closed(bad):
    with pytest.raises(SelectionError):
        select_coins([utxo(1, 200_000)], 60_000, bad, CHANGE_COST, RECIPIENT)


def test_rate_floor_one_centisat_is_accepted():
    # 0.01 sat/vB is inside the engine contract (min-relay standardness is a
    # SEPARATE, later gate in the PSBT builder — ADR-0012 §3, unchanged).
    result = select_coins([utxo(1, 200_000)], 60_000, 1, CHANGE_COST, RECIPIENT)
    assert result.fee_sats == 2  # ceil(141 x 0.01) = 1.41 -> 2


def _build(result, amount_sats):
    return build_unsigned_psbt(
        result.selected,
        [(recipient_script(), amount_sats)],
        change_address() if result.change_sats is not None else None,
        result.change_sats,
        account_key=account_key(),
        account_fingerprint=fingerprint(),
        account_path=ACCOUNT_PATH,
        change_index=7,
    )


def test_sub_rail_bid_refuses_at_the_min_relay_gate_before_signing():
    # Defense-in-depth (TCK-FEE-003 pin, rail corrected by TCK-FEE-005):
    # the estimator clamps explicit bids to MAX(rung, floor) upstream (see
    # chain/fees.py and the e2e clamp tests), but ANY caller that hands the
    # ENGINE a rate under the size-derived relay floor is still refused at
    # BUILD, value-free, before any device signature. The rail is Core's
    # policy.h DEFAULT_MIN_RELAY_TX_FEE (100 sat/kvB = 0.1 sat/vB), NOT the
    # historical 1 sat/vB — which is precisely what FEE-005 fixed: a 0.55
    # sat/vB bid (fee 78 on this 141-vB shape) used to die here and now
    # builds, because a default node relays it.
    inputs = [source("ab" * 32, 0, 200_000, index=3)]
    result = select_coins(inputs, 60_000, 9, CHANGE_COST, recipient_script())
    assert result.fee_sats == 13  # ceil(141 × 0.09) < the ceil(141 × 0.1) = 15 rail
    with pytest.raises(PsbtError) as exc:
        _build(result, 60_000)
    assert "min-relay" in str(exc.value)
    assert "13" not in str(exc.value)  # value-free, as everywhere in tx/


def test_the_055_bid_the_old_1_sat_vB_rail_refused_now_builds():
    # TCK-FEE-005's user-visible meaning: a rate in the 0.1..1 sat/vB band
    # (what the corrected rail admits) clears BOTH layers with no clamp
    # and no refusal — estimator seam and gate share the 10-centisat rail.
    estimator = FeeEstimator(
        _SubFloorNative({t: 1 for t in FeeTarget}),
        ttl_s=30.0,
    )
    bid_c, raised = estimator.clamp_to_min_relay_floor(55)  # explicit 0.55
    assert (bid_c, raised) == (55, False)  # above the 10-centisat rail
    inputs = [source("ab" * 32, 0, 200_000, index=3)]
    result = select_coins(inputs, 60_000, bid_c, CHANGE_COST, recipient_script())
    assert result.fee_sats == 78  # ceil(141 × 0.55) >= the 15-sat rail
    _psbt, meta = _build(result, 60_000)  # builds clean — no psbt_failed
    assert meta.expected_fee_sats == 78


def test_estimator_floor_clamp_fixes_the_live_psbt_failed_send():
    # TCK-FEE-004 end to end at the engine edge (the user's corrected spec:
    # MAX(calculated, floor) — MIN would still fail), with TCK-FEE-005's
    # corrected rail: a bid UNDER the floor (5 centisat/vB = 0.05 sat/vB,
    # below Core's default 0.1) arrives at the builder ALREADY lifted to
    # the rail (5 -> 10), so the same 141-vB send that would die psbt_failed
    # now builds at the rail fee — ceil(141 × 0.1) = 15 sats.
    estimator = FeeEstimator(
        _SubFloorNative({t: 1 for t in FeeTarget}),
        ttl_s=30.0,
    )
    bid_c, raised = estimator.clamp_to_min_relay_floor(5)  # explicit sub-rail
    assert (bid_c, raised) == (10, True)  # MAX, never MIN, never a refusal
    inputs = [source("ab" * 32, 0, 200_000, index=3)]
    result = select_coins(inputs, 60_000, bid_c, CHANGE_COST, recipient_script())
    assert result.fee_sats == 15  # ceil(141 x 0.10) == the relay rail
    _psbt, meta = _build(result, 60_000)  # builds clean — no psbt_failed
    assert meta.expected_fee_sats == 15


def test_floor_so_high_the_send_is_unfundable_still_refuses():
    # The refusal that REMAINS (the ticket's carve-out): when the floored
    # fee cannot be paid out of the selected coins at all, the money layer
    # still fails closed with InsufficientFunds — clamping never conjures
    # value from nothing.
    inputs = [source("ab" * 32, 0, 60_000, index=3)]
    with pytest.raises(InsufficientFundsError):
        select_coins(inputs, 59_990, 100, CHANGE_COST, recipient_script())


def test_fractional_bid_above_the_floor_builds_normally():
    # Positive control: the user's 1.21 target pays ceil(141 x 1.21) = 171
    # sats >= the 15-sat (0.1 sat/vB) relay rail and builds clean.
    inputs = [source("ab" * 32, 0, 200_000, index=3)]
    result = select_coins(inputs, 60_000, 121, CHANGE_COST, recipient_script())
    assert result.fee_sats == 171
    _psbt, meta = _build(result, 60_000)
    assert meta.expected_fee_sats == 171

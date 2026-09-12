"""Fractional-rate engine tests (TCK-FEE-003 wave, docs/fee-fractional-plan.md).

The tx engine takes integer CENTISAT/VB (1 sat/vB = 100): fees are
``ceil(vsize × c / 100)`` sats — integer-exact (no float money), identical to
the old whole-sat product for multiples of 100, and never under-bidding the
rate for fractional bids (sub-1 included).
"""

import pytest

from localwallet.tx.selection import (
    SelectionError,
    fee_sats_for,
    select_coins,
)

RECIPIENT = b"\x00\x14" + b"\x11" * 20
CHANGE_COST = 31


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

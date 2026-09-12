"""Coin-selection tests (TCK-P2-002).

Covers the documented deterministic algorithm end to end:

- smallest-larger-first greedy accumulation with the canonical
  ``(value, txid, vout)`` order and deterministic tie-breaks;
- exact-fund no-change (fee = whole residue), change-below-dust folding
  (the residue becomes the fee — never added to the recipient), and
  viable-change selections;
- the single-coin improvement pass (fires only when never worse in fee);
- the no-shattering dust sweep (only when the wallet would keep
  sub-dust dust behind and a larger set yields viable change);
- insufficient funds as a clean, user-facing ``InsufficientFundsError``
  (amounts allowed per ADR-0012 — UI text, never logged);
- determinism (repeat + shuffled input order), duplicates, validation;
- the integer-weight vsize accounting verified against embit-built
  transactions (exact match under the max-witness convention).
"""

import math
import random

import pytest
from embit import script
from embit.transaction import Transaction, TransactionInput, TransactionOutput, Witness

from localwallet.tx.selection import (
    P2WPKH_INPUT_WEIGHT_WU,
    InsufficientFundsError,
    SelectionError,
    estimate_tx_vsize,
    select_coins,
)

RECIPIENT = b"\x00\x14" + b"\x11" * 20  # P2WPKH recipient script
CHANGE_P2WPKH = b"\x00\x14" + b"\x22" * 20
CHANGE_COST = 8 + 1 + len(CHANGE_P2WPKH)  # 31 vB, exact for any output script


class Utxo:
    """Minimal UtxoRecord-like duck type (plain data, no Store)."""

    def __init__(self, txid: str, vout: int, value_sats: int):
        self.txid = txid
        self.vout = vout
        self.value_sats = value_sats


def utxo(n: int, value_sats: int, vout: int = 0) -> Utxo:
    return Utxo(txid=f"{n:064x}", vout=vout, value_sats=value_sats)


def expected_vsize(n_inputs: int, n_outputs: int, with_change: bool) -> int:
    """Independent component arithmetic (mirrors the docstring, not code)."""
    in_varint = 1 if n_inputs < 253 else 3  # CompactSize growth
    out_varint = 1 if n_outputs < 253 else 3
    overhead = 4 * (4 + in_varint + out_varint + 4) + 2
    weight = overhead + n_inputs * P2WPKH_INPUT_WEIGHT_WU
    weight += 4 * (8 + 1 + len(RECIPIENT))
    if with_change:
        weight += 4 * CHANGE_COST
    return math.ceil(weight / 4)


class TestBasicSelection:
    def test_smallest_larger_first_accumulates_ascending(self):
        # No single coin can cover 60k + fee at rate 2, so both are taken,
        # smallest first.
        result = select_coins(
            [utxo(1, 50_000), utxo(2, 30_000)], 60_000, 200, CHANGE_COST, RECIPIENT
        )
        assert [u.value_sats for u in result.selected] == [30_000, 50_000]
        assert result.inputs_total == 80_000
        assert result.estimated_vsize == expected_vsize(2, 1, with_change=True)
        assert result.fee_sats == result.estimated_vsize * 2
        assert result.change_sats == 80_000 - 60_000 - result.fee_sats
        assert result.change_sats >= 294  # viable change output

    def test_tie_break_by_txid_then_vout(self):
        a = utxo(2, 40_000, vout=1)
        b = utxo(1, 40_000, vout=0)
        result = select_coins([a, b], 40_000, 100, CHANGE_COST, RECIPIENT)
        assert [u.txid for u in result.selected] == sorted(
            [u.txid for u in result.selected]
        )
        assert result.selected[0].txid == f"{1:064x}"

    def test_single_input_exact_change_viable(self):
        result = select_coins([utxo(9, 200_000)], 60_000, 200, CHANGE_COST, RECIPIENT)
        assert [u.value_sats for u in result.selected] == [200_000]
        assert result.estimated_vsize == expected_vsize(1, 1, with_change=True)
        assert result.fee_sats == result.estimated_vsize * 2
        assert result.change_sats == 200_000 - 60_000 - result.fee_sats


class TestChangePolicies:
    def test_exact_fund_produces_no_change_and_whole_residue_fee(self):
        # With-change fee for 1 input at rate 2:
        fee_with_change = expected_vsize(1, 1, with_change=True) * 2
        result = select_coins(
            [utxo(3, 60_000 + fee_with_change)], 60_000, 200, CHANGE_COST, RECIPIENT
        )
        # change would be 0 (< dust) -> dropped; the entire residue is the fee.
        assert result.change_sats is None
        assert result.fee_sats == 60_000 + fee_with_change - 60_000
        assert result.estimated_vsize == expected_vsize(1, 1, with_change=False)
        assert result.inputs_total == result.fee_sats + 60_000

    def test_change_below_dust_folds_into_fee(self):
        # Residue 250: below dust (294) as change, above the changeless
        # fee target (110 vB * 2) -> fold; fee becomes the whole residue.
        result = select_coins([utxo(4, 60_250)], 60_000, 200, CHANGE_COST, RECIPIENT)
        assert result.change_sats is None
        assert result.fee_sats == 250  # the full residue, not 110*2
        assert result.estimated_vsize == expected_vsize(1, 1, with_change=False)

    def test_residue_is_never_added_to_the_recipient(self):
        result = select_coins([utxo(4, 60_250)], 60_000, 200, CHANGE_COST, RECIPIENT)
        assert result.fee_sats + 60_000 == result.inputs_total

    def test_custom_change_script_changes_dust_threshold(self):
        # A 34-byte P2WSH change script has dust 330 (computed, not given).
        result = select_coins(
            [utxo(5, 60_000 + 400)],
            60_000,
            200,
            8 + 1 + 34,
            RECIPIENT,
            change_script=b"\x00\x20" + b"\x33" * 32,
        )
        assert result.change_sats is None  # 400 - fee < 330 -> fold
        assert result.fee_sats == 60_400 - 60_000


class TestSingleCoinImprovement:
    def test_prefers_one_coin_when_never_worse(self):
        # Greedy takes 30k + 50k (2-in fee), but the 200k coin alone is
        # cheaper (1-in vsize) -> the improvement pass must pick it.
        result = select_coins(
            [utxo(1, 50_000), utxo(2, 30_000), utxo(3, 200_000)],
            60_000,
            200,
            CHANGE_COST,
            RECIPIENT,
        )
        assert [u.value_sats for u in result.selected] == [200_000]
        assert result.fee_sats == expected_vsize(1, 1, with_change=True) * 2

    def test_exact_tie_single_coin_wins(self):
        # Improvement-pass `<=` semantics: the 60_418 coin alone folds a fee
        # of 418 (residue), EXACTLY equal to the greedy pair's 2-in fee of
        # 418 — the single coin must win on the tie (strictly fewer inputs,
        # never a worse fee).
        greedy_fee = expected_vsize(2, 1, with_change=True) * 2  # 418 @ rate 2
        assert greedy_fee == 418
        result = select_coins(
            [utxo(1, 50_000), utxo(2, 30_000), utxo(3, 60_000 + greedy_fee)],
            60_000,
            200,
            CHANGE_COST,
            RECIPIENT,
        )
        assert [u.value_sats for u in result.selected] == [60_000 + greedy_fee]
        assert result.fee_sats == greedy_fee  # tie, not a strictly better fee
        assert result.change_sats is None  # the tie fee is the folded residue

    def test_improvement_does_not_fire_when_strictly_worse(self):
        # 60_500 alone folds 500 sats into the fee (500 > greedy fee 418),
        # so the 2-input greedy result must stand.
        result = select_coins(
            [utxo(1, 50_000), utxo(2, 30_000), utxo(3, 60_500)],
            60_000,
            200,
            CHANGE_COST,
            RECIPIENT,
        )
        assert [u.value_sats for u in result.selected] == [30_000, 50_000]
        assert result.fee_sats == expected_vsize(2, 1, with_change=True) * 2

    def test_too_small_single_coin_never_hijacks(self):
        result = select_coins(
            [utxo(1, 50_000), utxo(2, 30_000), utxo(3, 55_000)],
            60_000,
            200,
            CHANGE_COST,
            RECIPIENT,
        )
        # 55k alone cannot fund 60k + fee; greedy pair stands.
        assert [u.value_sats for u in result.selected] == [30_000, 50_000]


class TestDustInputSkip:
    """Prefix-greedy false InsufficientFunds: sub-incremental-fee inputs."""

    def test_review_repro_dust_pool_cannot_poison_greedy_prefix(self):
        # 200 x 1-sat UTXOs + one 61_200-sat coin, amount 60_000, rate 10.
        # A 1-sat input costs 68 vB x 10 = 680 sats in incremental fee —
        # more than its value — so it can never help finalization and used
        # to poison every greedy prefix (InsufficientFunds). The skip rule
        # must select the big coin.
        utxos = [utxo(i + 1, 1) for i in range(200)] + [utxo(1000, 61_200)]
        result = select_coins(utxos, 60_000, 1_000, CHANGE_COST, RECIPIENT)
        assert [u.value_sats for u in result.selected] == [61_200]
        # The big coin funds it changelessly: the whole residue is the fee.
        assert result.change_sats is None
        assert result.fee_sats == 61_200 - 60_000
        assert result.estimated_vsize == expected_vsize(1, 1, with_change=False)

    def test_skip_threshold_is_68_vbytes_times_rate(self):
        # The documented threshold: P2WPKH input weight 272 WU = 68 vB
        # exactly, so the skip predicate is value < 68 * rate. At the
        # threshold itself an input is break-even (adds 68*rate value and
        # 68*rate fee), so `<` vs `<=` is outcome-equivalent by
        # construction — the improvement passes prune break-even inputs
        # anyway; the pin here is the constant and the strictly-below skip.
        assert P2WPKH_INPUT_WEIGHT_WU == 272
        assert P2WPKH_INPUT_WEIGHT_WU // 4 == 68
        # At rate 1 a 1-sat input (1 < 68) is skipped: a wallet of only
        # 1-sat coins can never finalize, even in the thousands.
        utxos = [utxo(i + 1, 1) for i in range(1000)]
        with pytest.raises(InsufficientFundsError):
            select_coins(utxos, 60_000, 100, CHANGE_COST, RECIPIENT)

    def test_all_dust_wallet_reports_insufficient_funds(self):
        # Every input below 68 x rate: nothing is selectable, so the honest
        # answer is InsufficientFunds, not a doomed selection.
        utxos = [utxo(i + 1, 50) for i in range(100)]  # 50 < 68 x 2
        with pytest.raises(InsufficientFundsError) as exc:
            select_coins(utxos, 60_000, 200, CHANGE_COST, RECIPIENT)
        assert exc.value.available == 100 * 50

    def test_skip_is_deterministic_under_shuffling(self):
        utxos = [utxo(i + 1, 1) for i in range(200)] + [utxo(1000, 61_200)]
        rng = random.Random(7)
        reference = select_coins(utxos, 60_000, 1_000, CHANGE_COST, RECIPIENT)
        for _ in range(10):
            shuffled = list(utxos)
            rng.shuffle(shuffled)
            assert select_coins(shuffled, 60_000, 1_000, CHANGE_COST, RECIPIENT) == reference


class TestConservationInvariant:
    """inputs_total == amount + fee + change (or 0), asserted at the boundary."""

    @staticmethod
    def _conserves(result, amount_sats: int) -> bool:
        change = result.change_sats if result.change_sats is not None else 0
        return result.inputs_total == amount_sats + result.fee_sats + change

    def test_viable_change_conserves(self):
        result = select_coins(
            [utxo(1, 50_000), utxo(2, 30_000)], 60_000, 200, CHANGE_COST, RECIPIENT
        )
        assert result.change_sats is not None
        assert self._conserves(result, 60_000)

    def test_folded_residue_conserves(self):
        result = select_coins([utxo(4, 60_250)], 60_000, 200, CHANGE_COST, RECIPIENT)
        assert result.change_sats is None
        assert self._conserves(result, 60_000)

    def test_single_coin_conserves(self):
        result = select_coins([utxo(9, 200_000)], 60_000, 200, CHANGE_COST, RECIPIENT)
        assert self._conserves(result, 60_000)

    def test_dust_skip_result_conserves(self):
        utxos = [utxo(i + 1, 1) for i in range(200)] + [utxo(1000, 61_200)]
        result = select_coins(utxos, 60_000, 1_000, CHANGE_COST, RECIPIENT)
        assert self._conserves(result, 60_000)


class TestNoShatteringDustSweep:
    def test_sweep_prefers_viable_change_over_dead_wallet_dust(self):
        # 300 sub-dust UTXOs (299 x 280 sats + 1 x 293 sats); amount tuned
        # so the greedy prefix of 299 folds (Case-A change would be 69 <
        # 294) and leaves the last 293-sat UTXO as dead wallet dust
        # (293 < 294). Adding it: change becomes exactly 294 (viable), so
        # the sweep must prefer the 300-input set with change over the
        # 299-input fold.
        rate = 1
        n_total = 300
        utxos = [utxo(i + 1, 280) for i in range(299)] + [utxo(300, 293)]
        selected_total = 299 * 280  # greedy prefix: all but the 293-sat UTXO
        changeless_vsize = expected_vsize(299, 1, with_change=False)
        amount = selected_total - changeless_vsize * rate - 100  # 63_244

        result = select_coins(utxos, amount, rate * 100, CHANGE_COST, RECIPIENT)

        assert len(result.selected) == n_total  # slightly larger input set
        assert result.change_sats is not None  # viable change output
        assert result.change_sats >= 294
        # And the sweep never runs without cause in normal wallets:
        normal = select_coins(
            [utxo(1, 50_000), utxo(2, 30_000)], 60_000, 200, CHANGE_COST, RECIPIENT
        )
        assert [u.value_sats for u in normal.selected] == [30_000, 50_000]

    def test_never_selects_all_utxos_when_a_subset_suffices(self):
        result = select_coins(
            [utxo(1, 50_000), utxo(2, 30_000), utxo(3, 900_000)],
            60_000,
            200,
            CHANGE_COST,
            RECIPIENT,
        )
        # Improvement pass picks ONE coin; never all three.
        assert len(result.selected) == 1
        assert len(result.selected) < 3


class TestInsufficientFunds:
    def test_residue_below_fee_target_is_insufficient(self):
        # 60_100 - 60_000 = 100 < changeless fee target 220 at rate 2.
        with pytest.raises(InsufficientFundsError) as exc:
            select_coins([utxo(1, 60_100)], 60_000, 200, CHANGE_COST, RECIPIENT)
        assert exc.value.needed > exc.value.available
        assert exc.value.available == 60_100

    def test_error_message_is_user_facing_with_amounts(self):
        # ADR-0012: needed/available amounts are deliberate UI text for the
        # chat surface; they must never be placed into logs by callers.
        with pytest.raises(InsufficientFundsError) as exc:
            select_coins([utxo(1, 10_000)], 60_000, 200, CHANGE_COST, RECIPIENT)
        message = str(exc.value)
        assert "10000" in message or "10 000" in message
        assert str(exc.value.needed) in message
        assert str(exc.value.available) in message

    def test_empty_wallet(self):
        with pytest.raises(InsufficientFundsError) as exc:
            select_coins([], 1_000, 200, CHANGE_COST, RECIPIENT)
        assert exc.value.available == 0

    def test_amount_above_total(self):
        with pytest.raises(InsufficientFundsError):
            select_coins([utxo(1, 50_000)], 500_000, 200, CHANGE_COST, RECIPIENT)


class TestDeterminism:
    def test_same_inputs_same_result(self):
        utxos = [utxo(1, 50_000), utxo(2, 30_000), utxo(3, 200_000)]
        first = select_coins(utxos, 60_000, 200, CHANGE_COST, RECIPIENT)
        second = select_coins(utxos, 60_000, 200, CHANGE_COST, RECIPIENT)
        assert first == second
        assert [id(u) for u in first.selected] == [
            id(u) for u in second.selected
        ]

    def test_shuffled_input_order_same_result(self):
        utxos = [utxo(i, 30_000 + 7_000 * i) for i in range(1, 8)]
        rng = random.Random(42)
        reference = select_coins(utxos, 60_000, 200, CHANGE_COST, RECIPIENT)
        for _ in range(20):
            shuffled = list(utxos)
            rng.shuffle(shuffled)
            assert select_coins(shuffled, 60_000, 200, CHANGE_COST, RECIPIENT) == reference

    def test_selected_returned_in_canonical_order(self):
        utxos = [utxo(7, 30_000), utxo(2, 20_000), utxo(5, 10_000)]
        result = select_coins(utxos, 55_000, 100, CHANGE_COST, RECIPIENT)
        values = [u.value_sats for u in result.selected]
        assert values == sorted(values)


class TestValidationFailClosed:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"amount_sats": -1},
            {"amount_sats": True},
            {"amount_sats": 1.5},
            {"fee_rate_centisat_vb": 0},
            {"fee_rate_centisat_vb": -2},
            {"fee_rate_centisat_vb": 1_000_001},
            {"change_cost_vbytes": 30},  # below 8 + 1 + 22
            {"change_cost_vbytes": -1},
            {"output_script": b""},
            {"output_script": "0014"},
        ],
    )
    def test_bad_arguments_refused(self, kwargs):
        params = {
            "utxos": [utxo(1, 200_000)],
            "amount_sats": 60_000,
            "fee_rate_centisat_vb": 200,
            "change_cost_vbytes": CHANGE_COST,
            "output_script": RECIPIENT,
        }
        params.update(kwargs)
        with pytest.raises(SelectionError):
            select_coins(**params)

    def test_recipient_below_dust_refused(self):
        with pytest.raises(SelectionError):
            select_coins(
                [utxo(1, 200_000)], 100, 200, CHANGE_COST, RECIPIENT
            )

    def test_op_return_output_script_refused(self):
        # Symmetry with the dust module's unspendable rule: OP_RETURN's dust
        # threshold is 0, which would otherwise admit any amount on an
        # unspendable output (B5).
        with pytest.raises(SelectionError) as exc:
            select_coins(
                [utxo(1, 200_000)], 60_000, 200, CHANGE_COST, b"\x6a\x04test"
            )
        assert "unspendable" in str(exc.value)
        # A 1-sat amount on an OP_RETURN output would have passed the dust
        # check (threshold 0) — the refusal must come first.
        with pytest.raises(SelectionError):
            select_coins([utxo(1, 200_000)], 1, 200, CHANGE_COST, b"\x6a\x04test")

    def test_duplicate_utxo_refused(self):
        with pytest.raises(SelectionError):
            select_coins(
                [utxo(1, 50_000), utxo(1, 50_000)],
                60_000,
                200,
                CHANGE_COST,
                RECIPIENT,
            )

    def test_bad_utxo_fields_refused(self):
        with pytest.raises(SelectionError):
            select_coins([utxo(1, 0)], 60_000, 200, CHANGE_COST, RECIPIENT)
        with pytest.raises(SelectionError):
            select_coins([Utxo("short", 0, 50_000)], 60_000, 200, CHANGE_COST, RECIPIENT)
        with pytest.raises(SelectionError):
            select_coins([Utxo(f"{1:064x}", -1, 50_000)], 60_000, 200, CHANGE_COST, RECIPIENT)


class TestVsizeAgainstEmbit:
    """The integer-weight estimate must equal embit's built-tx vsize."""

    @staticmethod
    def _embit_vsize(n_inputs: int, recipient: bytes, change: bytes | None) -> int:
        tx = Transaction(
            version=2,
            vin=[
                TransactionInput(
                    txid=bytes(reversed(bytes.fromhex(f"{i + 1:064x}"))),
                    vout=0,
                    sequence=0xFFFFFFFD,
                )
                for i in range(n_inputs)
            ],
            vout=[TransactionOutput(1000, script.Script(recipient))]
            + ([TransactionOutput(1000, script.Script(change))] if change else []),
            locktime=0,
        )
        stripped = len(tx.serialize())
        for vin in tx.vin:
            vin.witness = Witness([b"\x00" * 72, b"\x00" * 33])  # max P2WPKH witness
        full = len(tx.serialize())
        return math.ceil((3 * stripped + full) / 4)

    @pytest.mark.parametrize("n_inputs", [1, 2, 3])
    def test_estimate_matches_embit_exactly(self, n_inputs):
        with_change = self._embit_vsize(n_inputs, RECIPIENT, CHANGE_P2WPKH)
        without_change = self._embit_vsize(n_inputs, RECIPIENT, None)
        assert estimate_tx_vsize(n_inputs, [RECIPIENT], CHANGE_COST) == with_change
        assert estimate_tx_vsize(n_inputs, [RECIPIENT], None) == without_change

    def test_selection_vsize_matches_embit_built_tx(self):
        result = select_coins(
            [utxo(1, 50_000), utxo(2, 30_000)], 60_000, 200, CHANGE_COST, RECIPIENT
        )
        change_script = CHANGE_P2WPKH if result.change_sats is not None else None
        assert result.estimated_vsize == self._embit_vsize(
            len(result.selected), RECIPIENT, change_script
        )

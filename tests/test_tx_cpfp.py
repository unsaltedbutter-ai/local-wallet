"""Child-pays-for-parent builder tests (TCK-CPFP-001).

Binding list from the ledger row: child build (inbound only / inbound +
merge), unconfirmed-input handling (the inbound coin's parent MUST be the
recorded stuck parent — fail closed on a mismatched pair), fee-rate bounds
(all refusals VALUE-FREE), the honest parent-fee-unknown bound (no
fabricated package claim), dust computed from the destination script's
size, 0xfffffffd on every input, integer-exact centisat math incl.
fractional rates, and deterministic replay (same inputs -> field-identical
plan).

vsize/fee expectations are recomputed from independent component
arithmetic (mirroring the ADR-0012 §4 accounting, the test_tx_replacement
precedent) — never from the module under test.
"""

import dataclasses
import math
from dataclasses import dataclass

import pytest

from localwallet.tx.cpfp import (
    CpfpChildPlan,
    CpfpError,
    CpfpRefusalReason,
    StuckParent,
    build_cpfp_child_plan,
)
from localwallet.tx.dust import dust_threshold, min_relay_fee_vbytes
from localwallet.tx.psbt import SEQUENCE_RBF_ENABLED

DEST = b"\x00\x14" + b"\x33" * 20  # P2WPKH fresh own receive script
DEST_DUST = dust_threshold(DEST)  # computed, not a 294 literal

PARENT_TXID = "a" * 64


@dataclass(frozen=True)
class Coin:
    txid: str
    vout: int
    value_sats: int


def inbound(value: int, vout: int = 0, txid: str = PARENT_TXID) -> Coin:
    """A coin SPENDING THE STUCK PARENT's output (unconfirmed by nature)."""
    return Coin(txid=txid, vout=vout, value_sats=value)


def merge(value: int, n: int = 1) -> Coin:
    return Coin(txid=f"{n:064x}", vout=0, value_sats=value)


def vsize_of(n_inputs: int) -> int:
    """Independent max-witness P2WPKH accounting: n-in, 1-out, no change."""
    weight = 4 * (4 + 1 + 1 + 4) + 2  # overhead, 1-byte varints
    weight += n_inputs * (4 * 41 + (1 + 73 + 34))  # 272 WU per P2WPKH input
    weight += 4 * (8 + 1 + len(DEST))  # the single destination output
    return math.ceil(weight / 4)


def fee(vsize: int, rate_c: int) -> int:
    """Independent ceil(vsize * centisat / 100)."""
    return math.ceil(vsize * rate_c / 100)


VSIZE_1 = vsize_of(1)  # 110 vB
VSIZE_2 = vsize_of(2)  # 178 vB


def build(value: int = 100_000, rate: int = 500, *, parent=None, merge_coin=None) -> CpfpChildPlan:
    return build_cpfp_child_plan(
        parent or StuckParent(txid=PARENT_TXID, fee_sats=None, vsize=None),
        inbound(value),
        DEST,
        rate,
        merge_coin=merge_coin,
    )


class TestChildBuildInboundOnly:
    def test_single_input_single_output_shape(self) -> None:
        plan = build(100_000, 500)
        assert plan.vsize == VSIZE_1 == 110
        assert plan.fee_sats == fee(VSIZE_1, 500) == 550
        assert plan.output_sats == 100_000 - 550
        assert plan.outputs == ((DEST, 99_450),)  # ONE fresh-own output
        assert [c for c in plan.inputs] == [inbound(100_000)]  # verbatim coin
        assert plan.merged is False
        # conservation to the sat (inputs = outputs + fee)
        assert sum(c.value_sats for c in plan.inputs) == plan.output_sats + plan.fee_sats

    def test_rbf_policy_on_every_input(self) -> None:
        assert build().input_sequences == (SEQUENCE_RBF_ENABLED,)
        plan = build(200_000, 500, merge_coin=merge(50_000))
        assert plan.input_sequences == (SEQUENCE_RBF_ENABLED, SEQUENCE_RBF_ENABLED)

    def test_integer_exactness_fractional_centisat_rate(self) -> None:
        # 1.75 sat/vB = 175 centisat: ceil(110 * 175 / 100) = ceil(192.5) = 193
        plan = build(100_000, 175)
        assert plan.fee_sats == 193 == fee(VSIZE_1, 175)
        assert plan.output_sats == 100_000 - 193


class TestChildBuildInboundPlusMerge:
    def test_merge_coin_joins_the_child(self) -> None:
        plan = build(100_000, 500, merge_coin=merge(50_000))
        assert plan.merged is True
        assert plan.vsize == VSIZE_2 == 178
        assert plan.fee_sats == fee(VSIZE_2, 500) == 890
        assert plan.output_sats == 150_000 - 890
        assert plan.outputs == ((DEST, 150_000 - 890),)
        assert sum(c.value_sats for c in plan.inputs) == plan.output_sats + plan.fee_sats

    def test_inputs_canonical_txid_vout_order(self) -> None:
        # merge coin sorts BEFORE the inbound (its txid starts with 0x01...):
        # the plan's order is the PSBT builder's canonical (txid, vout) asc,
        # never the argument order.
        plan = build(100_000, 500, merge_coin=merge(50_000))
        keys = [(c.txid, c.vout) for c in plan.inputs]
        assert keys == sorted(keys)
        assert keys == [(f"{1:064x}", 0), (PARENT_TXID, 0)]


class TestUnconfirmedInputHandling:
    def test_mismatched_parent_is_refused_fail_closed(self) -> None:
        # The inbound coin must spend the RECORDED stuck parent — a coin
        # from another tx is a corrupt record, not a plan.
        with pytest.raises(CpfpError):
            build_cpfp_child_plan(
                StuckParent(txid=PARENT_TXID, fee_sats=None, vsize=None),
                Coin(txid="b" * 64, vout=0, value_sats=100_000),  # other parent
                DEST,
                500,
            )

    def test_duplicate_outpoints_refused(self) -> None:
        twin = inbound(100_000)
        with pytest.raises(CpfpError):
            build(100_000, 500, merge_coin=twin)

    def test_parent_txid_charset_strict(self) -> None:
        for bad in ("A" * 64, "a" * 63, "g" * 64, ""):
            with pytest.raises(CpfpError):
                build(parent=StuckParent(txid=bad, fee_sats=None, vsize=None))

    def test_coin_shape_validated_like_selection(self) -> None:
        with pytest.raises(CpfpError):
            build_cpfp_child_plan(
                StuckParent(txid=PARENT_TXID, fee_sats=None, vsize=None),
                Coin(txid=PARENT_TXID, vout=-1, value_sats=100_000),
                DEST,
                500,
            )
        with pytest.raises(CpfpError):
            build_cpfp_child_plan(
                StuckParent(txid=PARENT_TXID, fee_sats=None, vsize=None),
                Coin(txid=PARENT_TXID, vout=0, value_sats=0),
                DEST,
                500,
            )


class TestFeeRateBoundsRefusals:
    def test_rate_below_min_relay_refused(self) -> None:
        # 0.99 sat/vB: fee 109 < the size-derived relay floor of 110 sats.
        with pytest.raises(CpfpError) as exc:
            build(100_000, 99)
        assert exc.value.reason is CpfpRefusalReason.RATE_BELOW_MIN_RELAY
        # the exact 1 sat/vB edge passes (fee == min_relay for this size)
        assert min_relay_fee_vbytes(VSIZE_1) == VSIZE_1
        assert build(100_000, 100).fee_sats == VSIZE_1

    def test_fee_exceeds_funds_refused(self) -> None:
        # 1000 sat/vB on a 2000-sat coin: the bounded fee is unpayable.
        with pytest.raises(CpfpError) as exc:
            build(2_000, 100_000)
        assert exc.value.reason is CpfpRefusalReason.FEE_EXCEEDS_FUNDS

    def test_output_below_dust_refused_floor_from_script_size(self) -> None:
        fee_at_1 = fee(VSIZE_1, 100)
        # residue exactly at the computed dust floor passes; one sat below fails
        build(fee_at_1 + DEST_DUST, 100)
        with pytest.raises(CpfpError) as exc:
            build(fee_at_1 + DEST_DUST - 1, 100)
        assert exc.value.reason is CpfpRefusalReason.OUTPUT_BELOW_DUST

    def test_parent_shortfall_can_tip_into_fee_exceeds_funds(self) -> None:
        # Known-cheap parent at a high bid: the bound (package need) exceeds
        # the coin even though the child-only cost would fit.
        parent = StuckParent(txid=PARENT_TXID, fee_sats=1, vsize=100_000)
        with pytest.raises(CpfpError) as exc:
            build(100_000, 10_000, parent=parent)  # self cost fits; package doesn't
        assert exc.value.reason is CpfpRefusalReason.FEE_EXCEEDS_FUNDS

    @pytest.mark.parametrize(
        ("value", "rate", "expected"),
        [
            (100_000, 99, CpfpRefusalReason.RATE_BELOW_MIN_RELAY),
            (2_000, 100_000, CpfpRefusalReason.FEE_EXCEEDS_FUNDS),
        ],
    )
    def test_refusals_are_value_free(self, value, rate, expected) -> None:
        with pytest.raises(CpfpError) as exc:
            build(value, rate)
        assert exc.value.reason is expected
        # no numbers AT ALL in the message — not the caller's values, not a
        # derived floor (CPFP-002 renders UI from the plan record, never
        # from an error string; the reason enum is pure letters).
        msg = str(exc.value)
        assert not any(ch.isdigit() for ch in msg)
        assert "refused" in msg


class TestParentFeeBound:
    def test_unknown_parent_states_the_bound_honestly(self) -> None:
        plan = build(100_000, 500)  # StuckParent(None, None)
        assert plan.parent_fee_known is False
        assert plan.package_fee_rate_centisat_vb is None  # nothing fabricated
        assert plan.fee_sats == fee(VSIZE_1, 500)  # child-only self cost

    def test_known_underpaid_parent_shortfall_topups_the_child(self) -> None:
        # parent: 150 vB paying 100 sats at a 5 sat/vB bid -> the child must
        # cover ceil((110+150)*5) - 100 = 1200 (> self cost 550).
        parent = StuckParent(txid=PARENT_TXID, fee_sats=100, vsize=150)
        plan = build(100_000, 500, parent=parent)
        assert plan.fee_sats == 1200
        assert plan.parent_fee_known is True
        # honest effective package rate (integer-DOWNED floor):
        assert plan.package_fee_rate_centisat_vb == (100 + 1200) * 100 // (150 + 110)
        assert plan.package_fee_rate_centisat_vb >= 500  # the bid is a floor
        assert plan.output_sats == 100_000 - 1200

    def test_known_generous_parent_child_pays_only_self_cost(self) -> None:
        parent = StuckParent(txid=PARENT_TXID, fee_sats=5_000, vsize=150)
        plan = build(100_000, 500, parent=parent)
        assert plan.fee_sats == fee(VSIZE_1, 500)  # max() picks self cost
        assert plan.package_fee_rate_centisat_vb == (5_000 + 550) * 100 // (150 + 110)

    def test_half_a_parent_picture_refused(self) -> None:
        with pytest.raises(CpfpError):
            build(parent=StuckParent(txid=PARENT_TXID, fee_sats=100, vsize=None))
        with pytest.raises(CpfpError):
            build(parent=StuckParent(txid=PARENT_TXID, fee_sats=None, vsize=150))


class TestDeterminismAndHygiene:
    def test_replay_is_field_for_field_identical(self) -> None:
        parent = StuckParent(txid=PARENT_TXID, fee_sats=100, vsize=150)
        a = build(100_000, 500, parent=parent, merge_coin=merge(50_000))
        b = build(100_000, 500, parent=parent, merge_coin=merge(50_000))
        assert dataclasses.astuple(a) == dataclasses.astuple(b)

    def test_argument_order_does_not_change_the_plan(self) -> None:
        # The canonical sort makes (inbound first, merge second) and the
        # reversed outpoint naming produce the same input order.
        plan = build(100_000, 500, merge_coin=merge(50_000, n=1))
        flipped = build_cpfp_child_plan(
            StuckParent(txid=PARENT_TXID, fee_sats=None, vsize=None),
            inbound(100_000),
            DEST,
            500,
            merge_coin=Coin(txid=f"{1:064x}", vout=0, value_sats=50_000),
        )
        assert [(c.txid, c.vout) for c in plan.inputs] == [
            (c.txid, c.vout) for c in flipped.inputs
        ]
        assert plan.fee_sats == flipped.fee_sats

    def test_destination_structural_guards(self) -> None:
        for bad in (b"", b"\x6a" + b"\x00" * 4, "not-bytes"):  # type: ignore[arg-type]
            with pytest.raises((CpfpError, TypeError, ValueError)):
                build_cpfp_child_plan(
                    StuckParent(txid=PARENT_TXID, fee_sats=None, vsize=None),
                    inbound(100_000),
                    bad,
                    500,
                )

    def test_rate_bounds_fail_closed(self) -> None:
        for bad in (0, -1, True, "500", 1_000_001):
            with pytest.raises(CpfpError):
                build(100_000, bad)

    def test_plan_is_frozen(self) -> None:
        plan = build()
        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.fee_sats = 0  # type: ignore[misc]

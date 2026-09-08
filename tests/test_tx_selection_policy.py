"""TCK-UTXO-002: partitioned selection, consolidation, target bounds.

Covers the ADR-0012 amendment layers A/B/C as documented in the
``tx/selection.py`` module docstring (docstring-reproduction duty) and
docs/ux-utxo-notes-design.md §2:

- pool partition matrix: pure kyc-side wins, pure other-side wins,
  equal-fee tie breaks to other-side, mixed only when no pure pool funds
  (with the ``mixed`` flag set for the mandatory card warning), and a pure
  pool that costs MORE still wins (by design, §2.1);
- untagged wallet = pre-amendment behavior exactly;
- step 5 consolidation: trigger, <= 4 added inputs, the 2x-incremental-fee
  passage guard, the fee-rate threshold, conservation over the final set;
- step 3 never shatters a coin above utxo_target_max_sats;
- determinism (repeat + shuffle) and re-quote tag-mix stability (the
  ``mixed`` flag always describes the FINAL selection, §4.2);
- the ``coin_partition`` tag decision table (§1.4 + §1.3 mixed-lineage
  fail-safe).

Amounts use plain duck-typed UTXOs with the ``kyc_side`` boolean the caller
joins (§2) — tags/notes never enter this layer, so none are constructed here.
"""

import random

import pytest

from localwallet.tx.selection import SelectionError, coin_partition, select_coins

RECIPIENT = b"\x00\x14" + b"\x11" * 20  # P2WPKH recipient script
CHANGE_P2WPKH = b"\x00\x14" + b"\x22" * 20
CHANGE_COST = 8 + 1 + len(CHANGE_P2WPKH)  # 31 vB

AMOUNT = 100_000


class Coin:
    """UTXO duck type; kyc_side=None means the attribute is ABSENT
    (pre-join snapshot), distinct from an explicit False (untagged/tagged
    other-side)."""

    def __init__(self, n: int, value_sats: int, kyc_side: bool | None = None):
        self.txid = f"{n:064x}"
        self.vout = 0
        self.value_sats = value_sats
        if kyc_side is not None:
            self.kyc_side = kyc_side


def coin(n: int, value_sats: int, kyc: bool | None = None) -> Coin:
    return Coin(n, value_sats, kyc)


def run(coins, amount=AMOUNT, rate=1, **policy):
    return select_coins(
        coins, amount, rate, CHANGE_COST, RECIPIENT,
        change_script=CHANGE_P2WPKH, **policy,
    )


def values(result):
    return [u.value_sats for u in result.selected]


def conserved(result):
    return result.inputs_total == AMOUNT + result.fee_sats + (
        result.change_sats or 0
    )


# ------------------------------------------------------- partition matrix


class TestPartitionPreference:
    def test_pure_kyc_funds_when_other_pool_cannot(self):
        kyc = [coin(1, 70_000, True), coin(2, 60_000, True)]
        other = [coin(3, 1_000, False)]
        result = run(kyc + other)
        assert values(result) == [60_000, 70_000]
        assert result.mixed is False
        assert conserved(result)

    def test_pure_other_funds_when_kyc_pool_cannot(self):
        kyc = [coin(1, 1_000, True)]
        other = [coin(2, 70_000, False), coin(3, 60_000, False)]
        result = run(kyc + other)
        assert values(result) == [60_000, 70_000]
        assert result.mixed is False

    def test_cheapest_pure_pool_wins(self):
        # kyc side funds with ONE coin (cheaper); other side needs two.
        kyc = [coin(1, 105_000, True)]
        other = [coin(2, 60_000, False), coin(3, 50_000, False)]
        result = run(kyc + other)
        assert values(result) == [105_000]
        assert result.mixed is False

    def test_equal_fee_tie_breaks_to_other_side(self):
        # Fixed pool order (other-side, kyc-side) makes ties reproducible.
        kyc = [coin(1, 110_000, True)]
        other = [coin(2, 110_000, False)]
        result = run(kyc + other)
        assert [u.txid for u in result.selected] == [other[0].txid]

    def test_mixed_only_when_no_pure_pool_funds(self):
        kyc = [coin(1, 60_000, True)]
        other = [coin(2, 60_000, False)]
        result = run(kyc + other)  # neither 60k alone funds 100k+fee
        assert values(result) == [60_000, 60_000]
        assert result.mixed is True  # caller MUST render the mix warning
        assert conserved(result)

    def test_pure_pool_wins_even_when_mixed_would_cost_less(self):
        # §2.1's stated conservative edge, BY DESIGN: the pure kyc pool
        # finalizes by folding a 280-sat changeless residue (Case B), while
        # the same wallet untagged finalizes with 3 inputs at a fee of 277.
        # A pure pool that funds is taken anyway; the mixed run is never
        # even made.
        kyc = [coin(3, 50_000, True), coin(4, 50_280, True)]
        other = [coin(5, 49_000, False)]
        result = run(kyc + other)
        assert values(result) == [50_000, 50_280]
        assert result.mixed is False
        assert result.fee_sats == 280
        assert conserved(result)
        untagged = run([coin(3, 50_000), coin(4, 50_280), coin(5, 49_000)])
        assert untagged.fee_sats == 277  # mixing WAS cheaper...
        assert values(untagged) == [49_000, 50_000, 50_280]  # ...not taken

    def test_explicit_false_kyc_side_is_untagged(self):
        absent = [coin(1, 60_000), coin(2, 50_000)]
        explicit = [coin(1, 60_000, False), coin(2, 50_000, False)]
        assert values(run(absent)) == values(run(explicit))

    def test_untagged_wallet_is_pre_amendment_exactly(self):
        wallet = [coin(1, 12_000), coin(2, 60_000), coin(3, 50_000),
                  coin(4, 200_000)]
        baseline = run(wallet)  # no kyc_side attributes at all
        tagged_other = [coin(1, 12_000, False), coin(2, 60_000, False),
                        coin(3, 50_000, False), coin(4, 200_000, False)]
        again = run(tagged_other)
        assert values(baseline) == values(again)
        assert again.mixed is False and again.folded_count == 0


# ---------------------------------------------------- step 3 max-coin rule


class TestMaxCoinImprovement:
    def test_single_coin_improvement_never_shatters_above_max(self):
        wallet = [coin(1, 50_000), coin(2, 60_000), coin(3, 200_000)]
        unguarded = run(wallet)  # step 3 takes the 200k single coin
        assert values(unguarded) == [200_000]
        guarded = run(wallet, utxo_target_max_sats=150_000)
        assert values(guarded) == [50_000, 60_000]  # big coin preserved
        assert conserved(guarded)

    def test_improvement_still_fires_below_max(self):
        wallet = [coin(1, 50_000), coin(2, 60_000), coin(3, 111_000),
                  coin(4, 200_000)]
        guarded = run(wallet, utxo_target_max_sats=150_000)
        assert values(guarded) == [111_000]


# ----------------------------------------------------- step 5 consolidation


class TestConsolidation:
    WALLET = None

    def wallet(self):
        # one funding coin + five below-target-min coins (target min 50_000)
        return [coin(i + 1, 5_000) for i in range(5)] + [coin(9, 120_000)]

    def test_fires_at_or_below_threshold_and_caps_at_four_inputs(self):
        result = run(
            self.wallet(), rate=1,
            utxo_target_min_sats=50_000, consolidate_below_sat_vb=2,
        )
        assert result.folded_count == 4  # five candidates, bound is 4
        assert len(result.selected) == 5
        assert conserved(result)

    def test_threshold_is_inclusive_and_rate_gated(self):
        at = run(self.wallet(), rate=2, utxo_target_min_sats=50_000,
                 consolidate_below_sat_vb=2)
        above = run(self.wallet(), rate=3, utxo_target_min_sats=50_000,
                    consolidate_below_sat_vb=2)
        assert at.folded_count == 4
        assert above.folded_count == 0  # rate above ceiling: never fires
        assert conserved(above)

    def test_each_coin_must_earn_double_its_incremental_fee(self):
        # canonical order is value-ascending: the FIRST candidate below
        # 2 x 68 vB x rate stops the walk (doc §2.2 stop-at-violation).
        wallet = [coin(1, 135), coin(2, 5_000), coin(3, 5_000),
                  coin(4, 5_000), coin(9, 120_000)]
        result = run(wallet, rate=1, utxo_target_min_sats=50_000,
                     consolidate_below_sat_vb=2)
        assert result.folded_count == 0

    def test_needs_at_least_two_candidates_to_trigger(self):
        wallet = [coin(1, 5_000), coin(9, 120_000)]
        result = run(wallet, rate=1, utxo_target_min_sats=50_000,
                     consolidate_below_sat_vb=2)
        assert result.folded_count == 0
        assert len(result.selected) == 1

    def test_off_when_settings_absent(self):
        result = run(self.wallet(), rate=1)
        assert result.folded_count == 0
        assert len(result.selected) == 1

    def test_consolidation_never_touches_selected_or_large_coins(self):
        wallet = [coin(1, 5_000), coin(2, 5_000), coin(9, 120_000),
                  coin(10, 90_000)]  # 90_000 > target min: not a candidate
        result = run(wallet, rate=1, utxo_target_min_sats=50_000,
                     consolidate_below_sat_vb=2)
        assert 90_000 not in values(result)
        assert result.folded_count == 2

    def test_consolidation_runs_within_a_pure_pool(self):
        # layer A/C interaction: fold candidates come from the CHOSEN pool
        kyc = [coin(1, 5_000, True), coin(2, 5_000, True),
               coin(3, 120_000, True)]
        other = [coin(4, 5_000, False), coin(5, 5_000, False)]
        result = run(kyc + other, rate=1, utxo_target_min_sats=50_000,
                     consolidate_below_sat_vb=2)
        assert result.mixed is False
        assert result.folded_count == 2  # kyc-side folds only; other coins
        # never join a pure selection
        assert all(getattr(u, "kyc_side", False) for u in result.selected)


# ----------------------------------------------- determinism + re-quote pin


class TestDeterminismAndRequote:
    def test_repeat_and_shuffle_same_result(self):
        wallet = [coin(1, 60_000, True), coin(2, 5_000, True),
                  coin(3, 55_000, False), coin(4, 5_000, False),
                  coin(5, 50_500, True)]
        kwargs = {"rate": 2, "utxo_target_min_sats": 50_000,
                  "utxo_target_max_sats": 150_000,
                  "consolidate_below_sat_vb": 2}
        first = values(run(wallet, **kwargs))
        rng = random.Random(7)
        for _ in range(5):
            shuffled = wallet[:]
            rng.shuffle(shuffled)
            assert values(run(shuffled, **kwargs)) == first

    def test_requote_never_silently_changes_the_mix_flag(self):
        # §4.2: narration renders from the FINAL selection. Here a slow
        # rate funds purely (kyc side alone); a fast rate breaks the pure
        # pool's finalization and the fallback spans partitions — the flag
        # follows the set on every call, so the card cannot keep a stale
        # mix state across a re-quote.
        wallet = [coin(1, 1_000, False), coin(2, 50_500, True),
                  coin(3, 50_500, True)]
        slow = run(wallet, rate=1)
        assert slow.mixed is False
        assert values(slow) == [50_500, 50_500]
        fast = run(wallet, rate=6)
        assert fast.mixed is True  # only the full set funds at 6 sat/vB
        assert values(fast) == [1_000, 50_500, 50_500]
        for result in (slow, fast):
            flags = [getattr(u, "kyc_side", False) for u in result.selected]
            assert result.mixed == (any(flags) and not all(flags))


# ------------------------------------------------- coin_partition §1.4 table


class TestCoinPartition:
    def test_decision_table(self):
        cases = [
            ((), (False, False)),  # unlabeled -> other-side default
            (("consolidation",), (False, False)),  # neutral tag
            (("p2p",), (False, False)),
            (("purchase",), (False, False)),
            (("p2p", "purchase", "consolidation"), (False, False)),
            (("kyc",), (True, False)),
            (("exchange",), (True, False)),
            (("kyc", "exchange"), (True, False)),
            (("kyc", "consolidation"), (True, False)),
            (("exchange", "purchase"), (True, True)),  # mixed lineage
            (("kyc", "p2p"), (True, True)),
            (("kyc", "exchange", "p2p", "purchase", "consolidation"),
             (True, True)),
            (("nonsense",), (False, False)),  # unknown = neither side
            (("kyc", "nonsense"), (True, False)),
        ]
        for tags, expected in cases:
            assert coin_partition(tags) == expected, tags

    def test_mixed_is_the_kyc_side_fail_safe(self):
        # doc §1.3: a mixed coin "can un-mix nothing" — kyc-side.
        kyc_side, mixed = coin_partition(("kyc", "p2p"))
        assert kyc_side and mixed


# --------------------------------------------------------- argument guards


class TestPolicyArgumentGuards:
    def test_min_not_below_max_refused_value_free(self):
        with pytest.raises(SelectionError) as excinfo:
            run([coin(1, 120_000)], utxo_target_min_sats=90_000,
                utxo_target_max_sats=90_000)
        assert "90000" not in str(excinfo.value)

    @pytest.mark.parametrize("kwargs", [
        {"utxo_target_min_sats": 545},  # below the §2.3 bound
        {"utxo_target_min_sats": 100_000_001},
        {"utxo_target_max_sats": 21_000_000_000_001},
        {"consolidate_below_sat_vb": 0},
        {"consolidate_below_sat_vb": 101},
        {"consolidate_below_sat_vb": True},
    ])
    def test_out_of_bounds_refused(self, kwargs):
        with pytest.raises(SelectionError):
            run([coin(1, 120_000)], **kwargs)

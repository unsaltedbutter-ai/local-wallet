"""Tests for the deterministic confirmation ETA (TCK-P5-002, ADR-0020).

Component 1: a narration-only, deterministic ETA on the send flow. All
hermetic — pure function, no network, no model.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.chain import FeeTarget, estimate_eta

# The estimator's three recommended-fee rates, ordered fast >= medium >= slow
# (the AC sanity premise). Values are arbitrary but monotonic.
TARGET_RATES = {
    FeeTarget.FAST: 20,
    FeeTarget.MEDIUM: 10,
    FeeTarget.SLOW: 3,
}


def _minutes(target: FeeTarget, hint: int | None = None) -> int:
    return estimate_eta(target, seconds_since_last_block=hint).expected_minutes


class TestEtaBaseOrdering:
    def test_base_minutes_map_to_target_horizons(self) -> None:
        # fast ≈ 1 block, medium ≈ 6 (half-hour), slow ≈ 12 (hour).
        assert _minutes(FeeTarget.FAST) == 10
        assert _minutes(FeeTarget.MEDIUM) == 60
        assert _minutes(FeeTarget.SLOW) == 120

    def test_ac_fee_rate_ordering_inverts_eta(self) -> None:
        """AC sanity (simulated): fast >= medium >= slow rates ⇒ ETA inverted.

        The chosen target's base blocks (fast < medium < slow) guarantee the
        ordering regardless of the mempool hint — a higher fee rate never
        yields a longer ETA than a lower one.
        """
        assert TARGET_RATES[FeeTarget.FAST] >= TARGET_RATES[FeeTarget.MEDIUM] >= TARGET_RATES[FeeTarget.SLOW]
        assert _minutes(FeeTarget.FAST) <= _minutes(FeeTarget.MEDIUM) <= _minutes(FeeTarget.SLOW)

    def test_ordering_preserved_under_congestion(self) -> None:
        # A stretched mempool inflates every target by the SAME congestion
        # adder, so the ordering invariant still holds.
        fast = _minutes(FeeTarget.FAST, hint=1800)
        medium = _minutes(FeeTarget.MEDIUM, hint=1800)
        slow = _minutes(FeeTarget.SLOW, hint=1800)
        assert fast < medium < slow

    def test_eta_is_deterministic(self) -> None:
        a = estimate_eta(FeeTarget.MEDIUM, seconds_since_last_block=1200)
        b = estimate_eta(FeeTarget.MEDIUM, seconds_since_last_block=1200)
        assert a == b


class TestEtaCongestion:
    def test_no_hint_uses_base_only(self) -> None:
        eta = estimate_eta(FeeTarget.MEDIUM, seconds_since_last_block=None)
        assert eta.congestion_blocks == 0
        assert eta.expected_blocks == 6
        assert eta.expected_minutes == 60

    def test_below_threshold_no_congestion(self) -> None:
        eta = estimate_eta(FeeTarget.MEDIUM, seconds_since_last_block=599)
        assert eta.congestion_blocks == 0
        assert eta.expected_minutes == 60

    def test_stretched_mempool_adds_blocks_deterministically(self) -> None:
        # > 600s since last block ⇒ one extra block per additional 600s.
        eta = estimate_eta(FeeTarget.MEDIUM, seconds_since_last_block=1200)
        assert eta.congestion_blocks == 1
        assert eta.expected_blocks == 7
        assert eta.expected_minutes == 70

    def test_congestion_capped(self) -> None:
        # A huge stretch never blows the estimate up without bound.
        eta = estimate_eta(FeeTarget.FAST, seconds_since_last_block=100_000)
        assert eta.congestion_blocks == 3
        assert eta.expected_blocks == 4
        assert eta.expected_minutes == 40


class TestEtaWording:
    def test_wording_is_honest_and_value_free(self) -> None:
        eta = estimate_eta(FeeTarget.SLOW)
        assert "estimate only" in eta.wording
        assert "guarantee" in eta.wording
        # The wording carries no address, amount, or fee figure.
        assert "sat" not in eta.wording.lower()
        assert "tb1" not in eta.wording

    def test_wording_carries_minutes_range(self) -> None:
        eta = estimate_eta(FeeTarget.MEDIUM)
        assert str(eta.expected_minutes) in eta.wording
        assert str(eta.minutes_upper) in eta.wording
        assert eta.minutes_upper == eta.expected_minutes + 10  # one-block bound


def test_non_fee_target_raises() -> None:
    with pytest.raises(TypeError, match="FeeTarget"):
        estimate_eta("fast")  # type: ignore[arg-type]

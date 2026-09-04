"""Deterministic confirmation ETA for the send flow (TCK-P5-002).

This module computes a *narration-only* ETA estimate from the chosen fee
target and the mempool state hint (time since the last block). It is:

- **Deterministic.** Given a target and the mempool hint, the same ETA
  always results. There is no sampling, no randomness, no model inference,
  and no network I/O here (it never opens a transport — the mempool hint is
  passed in).
- **Narration-only.** The ETA is NEVER a gate input. It does not feed the
  confirm gate, coin selection, the fee, or any re-validation path; it only
  decorates the confirmation card and the FACTS block so the model can
  narrate honest expectations. Nothing destructive depends on it.

Mechanism (pinned in ADR-0020):

- Each :class:`~localwallet.chain.fees.FeeTarget` maps to a base expected
  number of blocks, matching the semantics of the estimator's three
  recommended-fee targets (ADR-0011 / ``chain/fees.py``): ``fast`` ≈ 1
  block, ``medium`` ≈ 6 (half-hour), ``slow`` ≈ 12 (hour).
- Expected minutes = expected blocks × :data:`_BLOCK_MINUTES` (10-minute
  average block target).
- A stretched mempool inflates the estimate deterministically: when the
  time since the last block exceeds :data:`_STRETCH_AFTER_S`, one extra
  block is added per elapsed :data:`_STRETCH_PER_BLOCK_S`, capped at
  :data:`_STRETCH_CAP_BLOCKS`. Because the same congestion adder applies
  regardless of target, the ordering invariant always holds:
  ``fast ≤ medium ≤ slow`` minutes (the AC sanity property — a higher fee
  rate must never yield a longer ETA than a lower one).
- The wording carries honest uncertainty ("estimate only, not a
  guarantee"); the estimate is never promised.

The three recommended-fee *rates* are not needed here: the chosen target
already encodes the confirmation horizon the estimator's payload names
(fastestFee / halfHourFee / hourFee). Callers that want to validate the
rate↔ETA ordering against live rates use the ordering test documented in
ADR-0020 (deferred-run cross-check procedure).
"""

from __future__ import annotations

from dataclasses import dataclass

from localwallet.chain.fees import FeeTarget

__all__ = ["EtaEstimate", "estimate_eta"]

#: Base expected blocks per confirmation-target preset, matching the
#: estimator's recommended-fee target semantics (fastestFee ≈ 1 block,
#: halfHourFee ≈ 6 blocks, hourFee ≈ 12 blocks).
_BASE_BLOCKS: dict[FeeTarget, int] = {
    FeeTarget.FAST: 1,
    FeeTarget.MEDIUM: 6,
    FeeTarget.SLOW: 12,
}

#: Average block target used to convert expected blocks to minutes.
_BLOCK_MINUTES: int = 10

#: Time since the last block (seconds) beyond which the mempool is treated
#: as "stretched" (a block took longer than the ~10-minute target).
_STRETCH_AFTER_S: int = 600

#: Extra expected block per additional full interval once stretched.
_STRETCH_PER_BLOCK_S: int = 600

#: Upper bound on congestion-inflated blocks (never let the mempool hint
#: blow the estimate up without bound).
_STRETCH_CAP_BLOCKS: int = 3


@dataclass(frozen=True, slots=True)
class EtaEstimate:
    """A deterministic ETA estimate for a fee target.

    Attributes:
        target: The fee target the estimate is for.
        expected_blocks: Total expected blocks (base + congestion).
        congestion_blocks: The deterministic mempool-congestion adder.
        expected_minutes: ``expected_blocks × 10`` — the point estimate.
        minutes_upper: A single-block upper bound for honest uncertainty.
        wording: Short, value-free, honest phrasing for the card / FACTS.
    """

    target: FeeTarget
    expected_blocks: int
    congestion_blocks: int
    expected_minutes: int
    minutes_upper: int
    wording: str


def estimate_eta(
    fee_target: FeeTarget,
    *,
    seconds_since_last_block: int | None = None,
) -> EtaEstimate:
    """Return a deterministic ETA for ``fee_target``.

    Args:
        fee_target: The chosen confirmation-target preset (``fast`` /
            ``medium`` / ``slow``).
        seconds_since_last_block: The mempool state hint — integer seconds
            since the last block (e.g. from
            :func:`~localwallet.chain.watch.time_since_last_block`), or
            ``None`` when unknown (clean unavailable ⇒ no congestion
            adjustment, the base estimate only).

    Returns:
        An :class:`EtaEstimate`; deterministic for a given input pair.

    Raises:
        TypeError: ``fee_target`` is not a :class:`FeeTarget`.
    """
    if not isinstance(fee_target, FeeTarget):
        raise TypeError("fee_target must be a FeeTarget")
    base = _BASE_BLOCKS[fee_target]
    congestion = 0
    if (
        seconds_since_last_block is not None
        and seconds_since_last_block > _STRETCH_AFTER_S
    ):
        congestion = min(
            (seconds_since_last_block - _STRETCH_AFTER_S) // _STRETCH_PER_BLOCK_S,
            _STRETCH_CAP_BLOCKS,
        )
    blocks = base + congestion
    minutes = blocks * _BLOCK_MINUTES
    minutes_upper = (blocks + 1) * _BLOCK_MINUTES
    wording = f"~{minutes}-{minutes_upper} min — estimate only, not a guarantee"
    return EtaEstimate(
        target=fee_target,
        expected_blocks=blocks,
        congestion_blocks=congestion,
        expected_minutes=minutes,
        minutes_upper=minutes_upper,
        wording=wording,
    )

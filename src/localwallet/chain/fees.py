"""Fee estimation: floor-follower bids over mempool data, recommended fees as fallback.

Two derivation layers, one cache, one combined refresh (TCK-FEE-001):

1. **Floor-follower (primary).** The bid for a confirmation target is the
   *minimum integer sat/vB at or above the observed floor* for that target —
   never more (no padding above the ceil). Floors come entirely from
   endpoint data, never from hardcoded fee constants:

   =================  ==========================================================
   ``FeeTarget``      floor = ``ceil(max(...))`` of
   =================  ==========================================================
   ``FAST``           ``minimumFee`` (recommended payload: lowest fee that
                      gets into the next block), ``mempool-blocks[0]``
                      ``feeRange`` bottom (projected next block's lowest
                      quantile), and the recent-blocks floor
   ``MEDIUM``         ``mempool-blocks[2]`` bottom (deeper projected block)
                      and the recent-blocks floor
   ``SLOW``           ``mempool-blocks[6]`` bottom and the recent-blocks floor
   =================  ==========================================================

   The **recent-blocks floor** is the lowest ``extras.feeRange`` bottom
    across the last five confirmed blocks (``/v1/blocks/{tip}``, tip from
    ``get_tip_height``); it stops a transiently-empty mempool from producing
    a floor-miss bid when blocks are actually full. The parser **enforces**
    projected-block bottoms to be non-increasing with depth (a payload that
    breaks the order is rejected, not trusted) and MEDIUM/SLOW share the
    recent-blocks floor, so the ordering invariant ``FAST >= MEDIUM >= SLOW``
    is a code invariant over accepted inputs — not a data-source assumption.
    When the projected list is shallower than an index the target clamps to
    the last (lowest) available block; a shallower-than-5 confirmed list, an
    empty projected list, depth-increasing projected bottoms, or any
    malformed shape aborts the floor derivation.

2. **Recommended fallback.** ``GET {base}/v1/fees/recommended`` (mempool.space
   shape) maps ``FAST``/``MEDIUM``/``SLOW`` onto ``fastestFee``/``halfHourFee``/
   ``hourFee`` — the pre-TCK-FEE-001 behavior. ANY failure of the floor
   endpoints (transport, HTTP, or strict shape validation) degrades to this
   mapping; the estimate's :class:`FeeSource` records which path produced it
   so narration can stay honest. The recommended payload itself still fails
   closed (a malformed/failed ``/v1/fees/recommended`` raises
   :class:`~localwallet.chain.esplora.ChainError` — there is no lower layer
   to fall back to).

Motivating case (user report, 2026-09-07 live run): ``fastestFee`` bid
2 sat/vB while the last 5 blocks confirmed down to ~0.34 sat/vB and the
projected next block bottomed at ~0.3 — the floor rule yields exactly
``ceil(max(1, 0.3, 0.34)) = 1`` sat/vB (ADR-0011 amendment).

The full recommended payload also carries ``economyFee`` and ``minimumFee``
(all in sats/vB). ``SLOW`` deliberately does NOT fall back to ``economyFee``
when ``hourFee`` is missing: we fail closed on any missing/malformed key
rather than silently estimating a fee the user might not expect (the fee
*value* itself is not secret, but the shape contract is strict, mirroring
the fail-closed style of :mod:`localwallet.chain.esplora`).

Payloads are cheap but rate-limited (R11), so estimates are cached with a
short, settings-driven TTL; one refresh (recommended + mempool-blocks + tip
+ recent blocks) populates the whole cache. All network I/O flows through
the injected :class:`~localwallet.chain.esplora.EsploraClient` — this module
never creates its own ``httpx`` client.

Only the estimator and its accessors live here. Fee *computation* for a
specific transaction (``floor_for_vsize`` etc.) is the tx-engine's job
(ticket P2-002) and is intentionally not implemented here.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from localwallet.chain.esplora import ChainError
from localwallet.config import Settings

if TYPE_CHECKING:
    from localwallet.chain.esplora import EsploraClient

__all__ = [
    "FeeEstimate",
    "FeeEstimator",
    "FeeSource",
    "FeeTarget",
]

# Endpoint kinds + paths used in error messages / requests (log-scrubbed).
_FEES_KIND = "fees-recommended"
_FEES_PATH = "/v1/fees/recommended"
_MEMPOOL_BLOCKS_KIND = "fees-mempool-blocks"
_MEMPOOL_BLOCKS_PATH = "/v1/fees/mempool-blocks"
_RECENT_BLOCKS_KIND = "blocks-recent"
_RECENT_BLOCKS_PATH = "/v1/blocks"

# mempool.space recommended-fees payload keys (strictly required, all ints).
_REQUIRED_KEYS = ("fastestFee", "halfHourFee", "hourFee", "economyFee", "minimumFee")

#: Confirmed blocks sampled for the recent-blocks floor (the user's "last 5
#: blocks" rule; the endpoint serves 15 — fewer than this means a fresh
#: backend with no trustworthy window, which fails closed to fallback).
_RECENT_BLOCK_WINDOW = 5


def _now() -> float:
    """Indirection over ``time.time`` so tests can control the clock."""
    return time.time()


class FeeTarget(StrEnum):
    """Confirmation-target preset (floor-follower depth / recommended field)."""

    FAST = "fast"
    MEDIUM = "medium"
    SLOW = "slow"


class FeeSource(StrEnum):
    """Which derivation produced a :class:`FeeEstimate` (honesty marker).

    Exported but unconsumed as of TCK-FEE-001: this is the provenance hook
    for the follow-up UI narration work (surfacing floor-follower vs
    recommended bids honestly), which is deliberately not wired here.
    """

    FLOOR_FOLLOWER = "floor-follower"
    RECOMMENDED = "recommended"


# Fallback mapping: user-facing targets onto recommended-payload keys.
_TARGET_KEYS = {
    FeeTarget.FAST: "fastestFee",
    FeeTarget.MEDIUM: "halfHourFee",
    FeeTarget.SLOW: "hourFee",
}

#: Depth of the projected-mempool block whose ``feeRange`` bottom feeds each
#: floor-follower target (0 = next block, 2 = ~30 min, 6 = ~70 min; clamped
#: to the last available block when the projection is shallower).
_PROJECTED_TARGET_INDEX: dict[FeeTarget, int] = {
    FeeTarget.FAST: 0,
    FeeTarget.MEDIUM: 2,
    FeeTarget.SLOW: 6,
}


@dataclass(frozen=True)
class FeeEstimate:
    """A fee bid for one target, in sats/vB.

    Attributes:
        target: The confirmation-target preset this estimate is for.
        sat_per_vb: Fee bid in satoshis per virtual byte (``int``).
        source_timestamp: Epoch seconds when the estimate was fetched from
            the provider (server-side data; used to show freshness to the
            user).
        source: The derivation path that produced the bid
            (:class:`FeeSource`): floor-follower over mempool projections,
            or the recommended-fees fallback.
    """

    target: FeeTarget
    sat_per_vb: int
    source_timestamp: float
    source: FeeSource


@dataclass(frozen=True)
class _Snapshot:
    """A parsed estimate set (one derivation layer) plus fetch metadata."""

    estimates: dict[FeeTarget, FeeEstimate]
    minimum_fee_sat_vb: int
    fetched_at: float


class FeeEstimator:
    """Cached fee estimator: floor-follower bids, recommended fallback.

    One combined refresh populates all three targets, so a single (fresh)
    call fetches once: the recommended payload always, then — while building
    the floor-follower set — the projected blocks, the tip height, and the
    recent confirmed blocks. :meth:`estimate` and
    :meth:`minimum_fee_sat_vb` serve a cached value while it is younger than
    ``ttl_s`` and refetch otherwise.

    Fail-closed policy: the recommended payload is validated strictly (a
    missing or malformed key raises :class:`~localwallet.chain.esplora.ChainError`
    and the cache is left untouched — a previously-good payload keeps
    serving until its TTL). The floor endpoints are ALSO validated strictly,
    but a failure there degrades to the recommended mapping (source marked
    :attr:`FeeSource.RECOMMENDED`) instead of raising. A recommended fee of
    ``0`` sat/vB is itself malformed — a zero estimate is a broken payload,
    not a cheap one; the tx engine floors fees via min-relay separately
    (ADR-0012 §3, pinned in ADR-0011).

    Args:
        client: The shared :class:`EsploraClient` to GET through (no second
            ``httpx`` client is created). Network access stays in ``chain/``.
        ttl_s: Cache lifetime in seconds; defaults to
            ``Settings.fee_cache_ttl_s`` (30s).

    Raises:
        ValueError: If ``ttl_s`` is not a positive, finite number. (A NaN or
            infinite TTL would silently disable cache expiry — a NaN
            comparison is always ``False`` so nothing would ever be stale,
            and an infinite TTL would never expire — hence fail closed.)
    """

    def __init__(self, client: EsploraClient, ttl_s: float | None = None) -> None:
        if ttl_s is None:
            ttl_s = Settings.from_env().fee_cache_ttl_s
        if (
            isinstance(ttl_s, bool)
            or not isinstance(ttl_s, (int, float))
            or (isinstance(ttl_s, float) and not math.isfinite(ttl_s))
            or ttl_s <= 0
        ):
            raise ValueError("ttl_s must be a positive, finite number of seconds")
        try:
            ttl_f = float(ttl_s)  # huge JSON-size ints overflow defensively
        except OverflowError as exc:
            raise ValueError("ttl_s must be a finite number of seconds") from exc
        self._client = client
        self._ttl_s = ttl_f
        self._cache: _Snapshot | None = None

    def _get_snapshot(self) -> _Snapshot:
        """Return the cache if fresh, otherwise fetch + parse + cache it."""
        now = _now()
        if self._cache is not None and now - self._cache.fetched_at < self._ttl_s:
            return self._cache
        payload = self._client.get_json(_FEES_PATH, _FEES_KIND)
        recommended = _parse_recommended(payload, _FEES_KIND, now)  # may raise (as before)
        snapshot = recommended
        try:
            snapshot = self._floor_follower(recommended.minimum_fee_sat_vb, now)
        except ChainError:
            snapshot = recommended  # fail-closed degrade; value-free, never logged
        self._cache = snapshot
        return snapshot

    def _floor_follower(self, minimum_fee_sat_vb: int, now: float) -> _Snapshot:
        """Derive floor-follower bids, or raise :class:`ChainError` (caught by the caller).

        The bid is ``ceil(max(floor terms))`` — the minimum integer sat/vB
        AT OR ABOVE the observed floor and never above it (no padding); the
        FAST term list additionally includes the payload's ``minimumFee``
        ("min fee to get into the next block"). All inputs are endpoint
        data — there is no hardcoded fee constant on this path.
        """
        projected = _parse_projected_bottoms(
            self._client.get_json(_MEMPOOL_BLOCKS_PATH, _MEMPOOL_BLOCKS_KIND),
            _MEMPOOL_BLOCKS_KIND,
        )
        # Tip first (validated non-negative int by the client), then the
        # confirmed blocks carrying that tip.
        tip = self._client.get_tip_height()
        recent_floor = _parse_recent_floor(
            self._client.get_json(
                f"{_RECENT_BLOCKS_PATH}/{tip}", _RECENT_BLOCKS_KIND
            ),
            _RECENT_BLOCKS_KIND,
        )
        estimates: dict[FeeTarget, FeeEstimate] = {}
        for target, index in _PROJECTED_TARGET_INDEX.items():
            bottom = projected[min(index, len(projected) - 1)]  # clamp to deepest
            floor = max(bottom, recent_floor)
            if target is FeeTarget.FAST:
                floor = max(floor, minimum_fee_sat_vb)
            estimates[target] = FeeEstimate(
                target=target,
                sat_per_vb=math.ceil(floor),
                source_timestamp=now,
                source=FeeSource.FLOOR_FOLLOWER,
            )
        return _Snapshot(
            estimates=estimates,
            minimum_fee_sat_vb=minimum_fee_sat_vb,
            fetched_at=now,
        )

    def estimate(self, target: FeeTarget) -> FeeEstimate:
        """Return the fee bid for ``target`` (sats/vB, ``int``), cached by TTL."""
        if not isinstance(target, FeeTarget):
            raise TypeError("target must be a FeeTarget")
        return self._get_snapshot().estimates[target]

    def minimum_fee_sat_vb(self) -> int:
        """Return the ``minimumFee`` field (sats/vB), cached by TTL."""
        return self._get_snapshot().minimum_fee_sat_vb

    def invalidate(self) -> None:
        """Drop the cached payload; the next call refetches."""
        self._cache = None


def _fee_range_bottom(container: dict[str, Any], kind: str, index: int) -> float:
    """Strictly extract a block entry's ``feeRange`` bottom (lowest quantile).

    The bottom must be a non-boolean, **positive**, finite ``int``/``float``
    (sats/vB are fractional on these payloads). Zero/negative/bool/string/
    NaN/Infinity are all malformed — :class:`ChainError`, value-free (never
    echoes the fee). Huge JSON ints overflow-convert defensively.
    """
    fee_range = container.get("feeRange")
    if not isinstance(fee_range, list) or not fee_range:
        raise ChainError(f"{kind} entry {index} has missing or empty 'feeRange'")
    bottom = fee_range[0]
    if isinstance(bottom, bool) or not isinstance(bottom, (int, float)) or bottom <= 0:
        raise ChainError(f"{kind} entry {index} has invalid 'feeRange' bottom")
    try:
        value = float(bottom)
    except OverflowError as exc:
        raise ChainError(f"{kind} entry {index} has non-finite 'feeRange' bottom") from exc
    if not math.isfinite(value):
        raise ChainError(f"{kind} entry {index} has non-finite 'feeRange' bottom")
    return value


def _parse_projected_bottoms(payload: Any, kind: str) -> list[float]:
    """Validate the ``/v1/fees/mempool-blocks`` payload; return per-block bottoms.

    Non-empty list of objects each carrying a ``feeRange`` (ascending fee
    quantiles; only the bottom is used), with bottoms **non-increasing with
    depth** — the ``FAST >= MEDIUM >= SLOW`` invariant depends on it, so a
    payload that breaks the order fails closed (to fallback) instead of
    being trusted. An empty list means nothing is projected — no
    trustworthy floor — and fails closed too.
    """
    if not isinstance(payload, list) or not payload:
        raise ChainError(f"{kind} response was not a non-empty list")
    bottoms: list[float] = []
    for index, entry in enumerate(payload):
        if not isinstance(entry, dict):
            raise ChainError(f"{kind} entry {index} is not an object")
        bottom = _fee_range_bottom(entry, kind, index)
        if bottoms and bottom > bottoms[-1]:
            raise ChainError(f"{kind} entry {index} breaks projected-fee ordering")
        bottoms.append(bottom)
    return bottoms


def _parse_recent_floor(payload: Any, kind: str) -> float:
    """Return the recent-blocks floor: the lowest ``extras.feeRange`` bottom
    across the last :data:`_RECENT_BLOCK_WINDOW` confirmed blocks.

    Entries (from ``/v1/blocks/{tip}``) carry their fee stats under
    ``extras``; a missing/non-object ``extras`` or malformed bottom fails
    closed. Fewer than :data:`_RECENT_BLOCK_WINDOW` blocks is no trustworthy
    window (fresh backend) — also fail closed.
    """
    if not isinstance(payload, list):
        raise ChainError(f"{kind} response was not a list")
    if len(payload) < _RECENT_BLOCK_WINDOW:
        raise ChainError(f"{kind} response has fewer than {_RECENT_BLOCK_WINDOW} blocks")
    bottoms: list[float] = []
    for index, entry in enumerate(payload[:_RECENT_BLOCK_WINDOW]):
        if not isinstance(entry, dict) or not isinstance(entry.get("extras"), dict):
            raise ChainError(f"{kind} entry {index} has missing or malformed 'extras'")
        bottoms.append(_fee_range_bottom(entry["extras"], kind, index))
    return min(bottoms)


def _parse_recommended(payload: Any, kind: str, fetched_at: float) -> _Snapshot:
    """Strictly validate a recommended-fees payload (fail closed).

    Every required key must be present and a non-boolean, **positive**
    ``int`` — zero is rejected, not just negative: a 0 sat/vB estimate is a
    broken payload, not a free one (the tx engine floors fees via min-relay
    separately; ADR-0012 §3, pinned in ADR-0011), and ``minimumFee = 0`` is
    equally nonsense. Any other shape raises :class:`ChainError` naming only
    the field and the problem kind — never a fee value. Bools are rejected
    because ``True/False`` are ``int`` subclasses in Python and would
    otherwise pass.
    """
    if not isinstance(payload, dict):
        raise ChainError(f"{kind} response was not an object")
    values: dict[str, int] = {}
    for key in _REQUIRED_KEYS:
        raw = payload.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise ChainError(f"{kind} response has missing or invalid '{key}'")
        values[key] = raw
    estimates = {
        target: FeeEstimate(
            target=target,
            sat_per_vb=values[field],
            source_timestamp=fetched_at,
            source=FeeSource.RECOMMENDED,
        )
        for target, field in _TARGET_KEYS.items()
    }
    return _Snapshot(
        estimates=estimates,
        minimum_fee_sat_vb=values["minimumFee"],
        fetched_at=fetched_at,
    )

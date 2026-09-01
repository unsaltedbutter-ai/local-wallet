"""Fee estimation against the mempool.space recommended-fees endpoint.

Wraps ``GET {base}/v1/fees/recommended`` (mempool.space shape) and maps the
three confirmation targets the UI cares about onto that payload's fields:

=====================  ====================
``FeeTarget``          payload field
=====================  ====================
``FAST`` (fast)        ``fastestFee``
``MEDIUM`` (medium)    ``halfHourFee``
``SLOW`` (slow)        ``hourFee``
=====================  ====================

The full payload also carries ``economyFee`` and ``minimumFee`` (all in
sats/vB). ``SLOW`` deliberately does NOT fall back to ``economyFee`` when
``hourFee`` is missing: we fail closed on any missing/malformed key rather
than silently estimating a fee the user might not expect (the fee *value*
itself is not secret, but the shape contract is strict, mirroring the
fail-closed style of :mod:`localwallet.chain.esplora`).

The recommended-fee payload is cheap but rate-limited (R11), so estimates
are cached with a short, settings-driven TTL. All network I/O flows through
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
    "FeeTarget",
]

# Endpoint kind + path used in error messages / requests (log-scrubbed).
_FEES_KIND = "fees-recommended"
_FEES_PATH = "/v1/fees/recommended"

# mempool.space recommended-fees payload keys (strictly required, all ints).
_REQUIRED_KEYS = ("fastestFee", "halfHourFee", "hourFee", "economyFee", "minimumFee")

def _now() -> float:
    """Indirection over ``time.time`` so tests can control the clock."""
    return time.time()


class FeeTarget(StrEnum):
    """Confirmation-target preset mapped to a recommended-fee field."""

    FAST = "fast"
    MEDIUM = "medium"
    SLOW = "slow"


# Maps the three user-facing confirmation targets to payload keys.
_TARGET_KEYS = {
    FeeTarget.FAST: "fastestFee",
    FeeTarget.MEDIUM: "halfHourFee",
    FeeTarget.SLOW: "hourFee",
}


@dataclass(frozen=True)
class FeeEstimate:
    """A recommended fee for one target, in sats/vB.

    Attributes:
        target: The confirmation-target preset this estimate is for.
        sat_per_vb: Recommended fee in satoshis per virtual byte (``int``).
        source_timestamp: Epoch seconds when the estimate was fetched from
            the provider (server-side data; used to show freshness to the
            user).
    """

    target: FeeTarget
    sat_per_vb: int
    source_timestamp: float


@dataclass(frozen=True)
class _Recommended:
    """The parsed recommended-fee payload plus the fetch timestamp."""

    estimates: dict[FeeTarget, FeeEstimate]
    minimum_fee_sat_vb: int
    fetched_at: float


class FeeEstimator:
    """Cached wrapper around the recommended-fees endpoint.

    One GET of ``/v1/fees/recommended`` populates all three targets, so a
    single fetch refreshes the whole cache. :meth:`estimate` and
    :meth:`minimum_fee_sat_vb` serve a cached value while it is younger than
    ``ttl_s`` and refetch otherwise. The response shape is validated strictly
    (fail closed): a missing or malformed key raises
    :class:`~localwallet.chain.esplora.ChainError` and the cache is left
    untouched (a previously-good payload keeps serving until its TTL). A
    recommended fee of ``0`` sat/vB is itself malformed — a zero estimate is
    a broken payload, not a cheap one; the tx engine floors fees via
    min-relay separately (ADR-0012 §3, pinned in ADR-0011).

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
        self._cache: _Recommended | None = None

    def _get_recommended(self) -> _Recommended:
        """Return the cache if fresh, otherwise fetch + parse + cache it."""
        now = _now()
        if self._cache is not None and now - self._cache.fetched_at < self._ttl_s:
            return self._cache
        payload = self._client.get_json(_FEES_PATH, _FEES_KIND)
        parsed = _parse_recommended(payload, _FEES_KIND, now)
        self._cache = parsed
        return parsed

    def estimate(self, target: FeeTarget) -> FeeEstimate:
        """Return the recommended fee for ``target`` (sats/vB), cached by TTL."""
        if not isinstance(target, FeeTarget):
            raise TypeError("target must be a FeeTarget")
        return self._get_recommended().estimates[target]

    def minimum_fee_sat_vb(self) -> int:
        """Return the ``minimumFee`` field (sats/vB), cached by TTL."""
        return self._get_recommended().minimum_fee_sat_vb

    def invalidate(self) -> None:
        """Drop the cached payload; the next call refetches."""
        self._cache = None


def _parse_recommended(payload: Any, kind: str, fetched_at: float) -> _Recommended:
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
        )
        for target, field in _TARGET_KEYS.items()
    }
    return _Recommended(
        estimates=estimates,
        minimum_fee_sat_vb=values["minimumFee"],
        fetched_at=fetched_at,
    )

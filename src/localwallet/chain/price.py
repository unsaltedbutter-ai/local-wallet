"""BTC price oracle against the mempool.space ``/v1/prices`` endpoint.

Wraps ``GET {base}/v1/prices`` (mempool.space shape:
``{"time": int(epoch), "USD": float, ...}``) and extracts the USD/BTC rate.
This is an external, incidental call (R10, R11): the rate is cached with a
TTL and a fetch timestamp so callers can show its age to the user, and the
degrade ladder below keeps the app usable offline.

OQ4 decisions encoded here and in ADR-0011:

- **Provider:** mempool.space ``/v1/prices`` — the same host as the chain
  data, so no additional third party is introduced (ADR-0003 already accepts
  the public operator for chain queries).
- **TTL:** 60s default, configurable via ``LOCALWALLET_PRICE_TTL_S``.
- **Default-on, opt-out:** enabled by default; ``LOCALWALLET_PRICE_ENABLED=0``
  disables it entirely (oracle is never called, :meth:`PriceOracle.fresh`
  raises :class:`ConfigDisabled`).
- **Offline degrade ladder:** fresh (age < TTL) → stale-with-age (served on
  any fetch-or-parse failure — transport errors, HTTP errors, *and*
  malformed 200 payloads — when a cached value exists and is younger than
  ``MAX_STALE_AGE_S``) → sats-only (raises :class:`PriceUnavailableError`
  when there is no cached value at all, or the only cached rate is older
  than the staleness cap).

All network I/O flows through the injected
:class:`~localwallet.chain.esplora.EsploraClient` — this module never creates
its own ``httpx`` client. Money math keeps sats as integers and USD as
``Decimal`` under the hood (see the rounding-policy notes on
:meth:`PriceOracle.sats_to_usd` / :meth:`PriceOracle.usd_to_sats`).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Any

from localwallet.chain.esplora import ChainError
from localwallet.config import Settings

if TYPE_CHECKING:
    from localwallet.chain.esplora import ChainClient

__all__ = [
    "MAX_STALE_AGE_S",
    "ConfigDisabled",
    "PriceOracle",
    "PriceUnavailableError",
    "Rate",
]

_PRICE_KIND = "price"
_PRICE_PATH = "/v1/prices"

_SATS_PER_BTC = 10**8
_CENTS_PER_USD = 100

#: Staleness cap (ADR-0011): a cached rate older than this is *never* served
#: by :meth:`PriceOracle.fresh` as an offline-degrade fallback — it raises
#: :class:`PriceUnavailableError` instead (callers fall back to sats-only).
#: :meth:`PriceOracle.stale_ok` still serves it with the fetch timestamp
#: attached: there the caller decides how old is too old. 24 h keeps the
#: displayed fiat figure honest for a market that moves daily while still
#: riding out a weekend-long backend outage.
MAX_STALE_AGE_S = 24 * 60 * 60

#: Plausible upper magnitude for a USD/BTC rate — a documented sanity bound,
#: not a price prediction. Real rates live around 1e4–1e6; this bound only
#: exists to fail closed on finite-but-absurd provider payloads (e.g. a
#: ``1e300`` JSON number) before they can distort every displayed fiat
#: figure. Three-plus orders of magnitude of headroom over any plausible
#: bull-case; revisited only via ADR-0011.
_MAX_USD_PER_BTC = 1_000_000_000.0


def _now() -> float:
    """Indirection over ``time.time`` so tests can control the clock."""
    return time.time()


class PriceUnavailableError(ChainError):
    """No price could be fetched and no cached rate exists.

    Value-free (no rate/amount in the message): callers (P2-004) fall back to
    sats-only display.
    """


class ConfigDisabled(Exception):
    """The price oracle is disabled by configuration (``price_enabled=False``).

    Distinct from :class:`PriceUnavailableError` because this is a deliberate
    opt-out state, not an outage. No rate/amount appears in the message.
    """


@dataclass(frozen=True)
class Rate:
    """A USD/BTC exchange rate with its fetch timestamp.

    Attributes:
        usd_per_btc: US dollars per bitcoin (a ``float``; see rounding-policy
            notes on :class:`PriceOracle` for how this enters money math).
        fetched_at: Epoch seconds when the rate was fetched.
        stale: ``True`` when this rate was served from cache past its TTL as
            an offline-degrade fallback (age warning for the UI). Fresh rates
            carry ``False``.
    """

    usd_per_btc: float
    fetched_at: float
    stale: bool = False

    def age_s(self) -> float:
        """Seconds since this rate was fetched (computed against the clock)."""
        return _now() - self.fetched_at


class PriceOracle:
    """Cached, TTL-bounded USD/BTC price oracle.

    Args:
        client: The shared :class:`ChainClient` to query through. Backends
            that carry no ``supports_price`` capability (Electrum/bitcoind
            adapters, TCK-ONB-004 plan OQ-2) are treated as a PERMANENT
            price outage: :meth:`fresh` / :meth:`stale_ok` raise
            :class:`PriceUnavailableError` without a fetch — USD-denominated
            ``create_tx`` refuses cleanly, sats-only keeps working.
        ttl_s: Cache lifetime in seconds; defaults to
            ``Settings.price_ttl_s`` (60s).
        enabled: Whether the oracle may query the network; defaults to
            ``Settings.price_enabled`` (``True``). When ``False`` the oracle
            is never called and :meth:`fresh` / :meth:`stale_ok` raise
            :class:`ConfigDisabled`.

    Raises:
        ValueError: If ``ttl_s`` is not a positive, finite number. (A NaN or
            infinite TTL would silently disable cache expiry — a NaN
            comparison is always ``False`` so nothing would ever be stale,
            and an infinite TTL would never expire — hence fail closed.)
    """

    def __init__(
        self,
        client: ChainClient,
        ttl_s: float | None = None,
        enabled: bool | None = None,
    ) -> None:
        settings = Settings.from_env()
        if ttl_s is None:
            ttl_s = settings.price_ttl_s
        if enabled is None:
            enabled = settings.price_enabled
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
        self._enabled = bool(enabled)
        self._cache: Rate | None = None

    # -- fetching / caching ------------------------------------------------

    def fresh(self) -> Rate:
        """Return a rate that is fresh (age < TTL) if at all possible.

        A backend without a price feed (``supports_price`` falsy — the
        capability seam of the TCK-ONB-004 plan) is the no-cache failure
        branch of the ladder directly: :class:`PriceUnavailableError`,
        nothing fetched, nothing fabricated.
    

        Degrade ladder (R10, OQ4; see ADR-0011):
        - cached value younger than the TTL → returned with ``stale=False``;
        - otherwise fetch **and parse**: on any failure — transport error,
          HTTP error, *or a malformed 200 payload* (shape validation is part
          of the failure path, so a 200-with-garbage degrades exactly like
          an outage) **with** a cached value younger than
          :data:`MAX_STALE_AGE_S` → the stale value is returned with
          ``stale=True`` (age warning, offline degrade);
        - on failure **without** any cache, or with a cache older than the
          staleness cap → :class:`PriceUnavailableError` (callers fall back
          to sats-only).

        A malformed payload is never cached: only a fully validated rate
        replaces the cached value.
        """
        self._check_enabled()
        self._check_supports_price()
        cached = self._cache
        if cached is not None and _now() - cached.fetched_at < self._ttl_s:
            return cached
        try:
            payload = self._client.get_json(_PRICE_PATH, _PRICE_KIND)
            rate = _parse_rate(payload, _PRICE_KIND)
        except ChainError as exc:
            if cached is not None and _now() - cached.fetched_at <= MAX_STALE_AGE_S:
                return _with_stale(cached)
            raise PriceUnavailableError("price unavailable") from exc
        self._cache = rate
        return rate

    def stale_ok(self) -> Rate:
        """Best-effort rate that tolerates staleness (explicit offline variant).

        Serves a cached value even past its TTL — and regardless of
        :data:`MAX_STALE_AGE_S`, which only caps what :meth:`fresh` will
        degrade to — rather than requiring a successful refetch; the rate
        carries its fetch timestamp (``age_s``), so the caller decides how
        old is too old. Attempts a refresh first when the cache is cold; a
        malformed 200 payload fails exactly like a transport failure. Only
        raises :class:`PriceUnavailableError` when there is no cache and the
        fetch/parse fails (nothing to degrade to), or
        :class:`ConfigDisabled` when disabled.
        """
        self._check_enabled()
        self._check_supports_price()
        if self._cache is not None:
            return self._cache
        try:
            payload = self._client.get_json(_PRICE_PATH, _PRICE_KIND)
            rate = _parse_rate(payload, _PRICE_KIND)
        except ChainError as exc:
            raise PriceUnavailableError("price unavailable") from exc
        self._cache = rate
        return rate

    def invalidate(self) -> None:
        """Drop the cached rate; the next call refetches."""
        self._cache = None

    # -- pure conversion helpers ------------------------------------------

    def sats_to_usd(self, sats: int, rate: Rate) -> int:
        """Convert satoshis to whole US cents.

        Rounding policy: **floors to the nearest whole cent** (1/100 USD),
        computed with ``Decimal`` to avoid float drift. Returns an ``int``
        count of cents — callers format for display. A sub-cent amount
        (e.g. 1 sat at a low rate) floors to ``0`` cents; the sat count is
        never altered.

        Raises:
            ValueError: If ``sats`` is not a non-negative ``int`` or ``rate``
                is not a positive :class:`Rate`.
        """
        _validate_sats(sats)
        _validate_rate(rate)
        cents = (Decimal(sats) * Decimal(repr(rate.usd_per_btc)) * _CENTS_PER_USD / _SATS_PER_BTC)
        return int(cents.to_integral_value(rounding=ROUND_FLOOR))

    def usd_to_sats(self, usd: float, rate: Rate) -> int:
        """Convert a USD amount to whole satoshis.

        Rounding policy: **floors to the nearest whole sat**, computed with
        ``Decimal`` to avoid float drift. A sub-sat fraction floors away, so
        tiny USD amounts can convert to ``0`` sats. ``usd`` is in dollars
        (e.g. ``1.25``).

        Raises:
            ValueError: If ``usd`` is not a non-negative number or ``rate``
                is not a positive :class:`Rate`.
        """
        _validate_usd(usd)
        _validate_rate(rate)
        sats = Decimal(repr(usd)) * _SATS_PER_BTC / Decimal(repr(rate.usd_per_btc))
        return int(sats.to_integral_value(rounding=ROUND_FLOOR))

    def _check_enabled(self) -> None:
        if not self._enabled:
            raise ConfigDisabled("price oracle is disabled by configuration")

    def _check_supports_price(self) -> None:
        # Capability gate (TCK-ONB-004 M1; plan OQ-2 default). Fail closed:
        # absence of the flag is treated as "no price feed", so a backend
        # can never be assumed to have one. Value-free message.
        if not getattr(self._client, "supports_price", False):
            raise PriceUnavailableError("price unavailable: backend has no price feed")


def _with_stale(rate: Rate) -> Rate:
    return Rate(usd_per_btc=rate.usd_per_btc, fetched_at=rate.fetched_at, stale=True)


def _parse_rate(payload: Any, kind: str) -> Rate:
    """Strictly validate a ``/v1/prices`` payload and extract USD/BTC.

    Fail closed: the payload must be an object carrying a non-boolean,
    positive, **finite** ``float``/``int`` ``USD`` field within a plausible
    magnitude (``0 < usd_per_btc <= _MAX_USD_PER_BTC``). Non-finite values
    are rejected explicitly because Python's ``json`` parses the bare
    ``NaN``/``Infinity`` tokens, and huge JSON integers are converted to
    ``float`` defensively so an ``OverflowError`` becomes a value-free
    :class:`ChainError`. Anything else raises :class:`ChainError` naming
    only the field/problem — never the rate value. ``time`` is accepted for
    shape parity but not required; ``fetched_at`` is the moment *we*
    fetched, not the provider's timestamp.
    """
    if not isinstance(payload, dict):
        raise ChainError(f"{kind} response was not an object")
    usd = payload.get("USD")
    if isinstance(usd, bool) or not isinstance(usd, (int, float)) or usd <= 0:
        raise ChainError(f"{kind} response has missing or invalid 'USD'")
    try:
        usd_f = float(usd)
    except OverflowError as exc:
        raise ChainError(f"{kind} response 'USD' is not representable") from exc
    if not math.isfinite(usd_f) or usd_f > _MAX_USD_PER_BTC:
        raise ChainError(f"{kind} response 'USD' is not a finite, plausible rate")
    return Rate(usd_per_btc=usd_f, fetched_at=_now())


def _validate_sats(sats: Any) -> None:
    if isinstance(sats, bool) or not isinstance(sats, int) or sats < 0:
        raise ValueError("sats must be a non-negative integer")


def _validate_usd(usd: Any) -> None:
    if isinstance(usd, bool) or not isinstance(usd, (int, float)) or usd < 0:
        raise ValueError("usd must be a non-negative number")


def _validate_rate(rate: Any) -> None:
    if not isinstance(rate, Rate):
        raise TypeError("rate must be a Rate")
    usd = rate.usd_per_btc
    if isinstance(usd, bool) or not isinstance(usd, (int, float)) or usd <= 0:
        raise ValueError("rate must have a positive usd_per_btc")
    try:
        usd_f = float(usd)  # huge ints overflow defensively
    except OverflowError as exc:
        raise ValueError("rate must have a finite usd_per_btc") from exc
    if not math.isfinite(usd_f) or usd_f > _MAX_USD_PER_BTC:
        raise ValueError("rate must have a finite, plausible usd_per_btc")

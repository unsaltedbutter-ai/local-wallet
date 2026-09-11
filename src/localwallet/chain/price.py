"""BTC price oracle against the mempool.space ``/v1/prices`` endpoint.

Wraps ``GET {base}/v1/prices`` (mempool.space shape:
``{"time": int(epoch), "USD": <per-BTC price>, "EUR": …, "GBP": …,
"CAD": …, "CHF": …, "AUD": …, "JPY": …}``) and extracts the per-BTC rate
in the *configured display currency* (TCK-FIAT-002; ADR-0011 amendment —
closed enum usd/eur/gbp/cad/chf/aud/jpy, default USD). **Unit semantics**
(followed from the endpoint exactly as TCK-P2-001/TCK-FIAT-001 already
consumed them — unchanged): each value is the price of one BTC in WHOLE
major units of that currency (e.g. dollars, not cents; yen, not sen —
JPY has no minor unit). This is an external, incidental call (R10, R11):
the rate is cached with a TTL and a fetch timestamp so callers can show
its age to the user, and the degrade ladder below keeps the app usable
offline.

OQ4 decisions encoded here and in ADR-0011:

- **Provider:** mempool.space ``/v1/prices`` — the same host as the chain
  data, so no additional third party is introduced (ADR-0003 already accepts
  the public operator for chain queries).
- **TTL:** 60s default, configurable via ``LOCALWALLET_PRICE_TTL_S``.
- **Default-on, opt-out:** enabled by default; ``LOCALWALLET_PRICE_ENABLED=0``
  disables it entirely (oracle is never called, :meth:`PriceOracle.fresh`
  raises :class:`ConfigDisabled`).
- **Currency:** ``LOCALWALLET_DISPLAY_CURRENCY`` / the ``display_currency``
  config-file key / the stored settings key (ladder resolved by the app and
  injected as :class:`PriceOracle` ``currency``; the shipped default is
  ``usd``). A cache entry is keyed by currency: switching display currency
  refetches, never re-tags a cached rate.
- **Offline degrade ladder:** fresh (age < TTL) → stale-with-age (served on
  any fetch-or-parse failure — transport errors, HTTP errors, *and*
  malformed 200 payloads — when a cached value exists and is younger than
  ``MAX_STALE_AGE_S``) → sats-only (raises :class:`PriceUnavailableError`
  when there is no cached value at all, or the only cached rate is older
  than the staleness cap).

All network I/O flows through the injected
:class:`~localwallet.chain.esplora.EsploraClient` — this module never creates
its own ``httpx`` client. Money math keeps sats as integers and the fiat side
as ``Decimal`` under the hood (see the rounding-policy notes on
:meth:`PriceOracle.sats_to_usd` / :meth:`PriceOracle.usd_to_sats` — names
kept for TCK-FIAT-001 wire compatibility; they convert in the *rate's*
currency).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Any, Final

from localwallet.chain.esplora import ChainError
from localwallet.config import (
    DEFAULT_DISPLAY_CURRENCY,
    Settings,
    normalize_display_currency,
)

if TYPE_CHECKING:
    from localwallet.chain.esplora import ChainClient

__all__ = [
    "MAX_STALE_AGE_S",
    "ConfigDisabled",
    "PriceOracle",
    "PriceUnavailableError",
    "Rate",
    "minor_per_unit",
]

_PRICE_KIND = "price"
_PRICE_PATH = "/v1/prices"

_SATS_PER_BTC = 10**8

#: Minor (smallest circulating) units per major unit, per closed display-
#: currency enum (TCK-FIAT-002; keys are the canonical lowercase codes).
#: JPY is the only zero-decimal currency on the endpoint — its per-BTC
#: figure is already in whole yen, so its "minor" unit is the yen itself.
_MINOR_PER_UNIT: Final[Mapping[str, int]] = {
    "usd": 100,
    "eur": 100,
    "gbp": 100,
    "cad": 100,
    "chf": 100,
    "aud": 100,
    "jpy": 1,
}


def minor_per_unit(currency: str) -> int:
    """Minor units per major unit for a canonical display-currency code
    (100 for the two-decimal codes, 1 for JPY). Raises :class:`ValueError`
    for any code outside the closed enum (fail closed, never a guess).
    """
    try:
        return _MINOR_PER_UNIT[currency]
    except KeyError:
        raise ValueError("unsupported display currency") from None


#: Staleness cap (ADR-0011): a cached rate older than this is *never* served
#: by :meth:`PriceOracle.fresh` as an offline-degrade fallback — it raises
#: :class:`PriceUnavailableError` instead (callers fall back to sats-only).
#: :meth:`PriceOracle.stale_ok` still serves it with the fetch timestamp
#: attached: there the caller decides how old is too old. 24 h keeps the
#: displayed fiat figure honest for a market that moves daily while still
#: riding out a weekend-long backend outage.
MAX_STALE_AGE_S = 24 * 60 * 60

#: Plausible upper magnitude for a per-BTC rate in ANY supported currency —
#: a documented sanity bound, not a price prediction. Real rates live around
#: 1e4–1e6 major units (JPY up to ~1e8); this bound only exists to fail
#: closed on finite-but-absurd provider payloads (e.g. a ``1e300`` JSON
#: number) before they can distort every displayed fiat figure. Several
#: orders of magnitude of headroom over any plausible bull-case in every
#: currency; revisited only via ADR-0011.
_MAX_FIAT_PER_BTC = 1_000_000_000.0


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
    """A per-BTC exchange rate in one display currency, with its fetch timestamp.

    Attributes:
        per_btc: whole major units of ``currency`` per bitcoin (a ``float``;
            see rounding-policy notes on :class:`PriceOracle` for how this
            enters money math).
        currency: canonical lowercase display-currency code (TCK-FIAT-002
            closed enum; ``"usd"`` default — every TCK-FIAT-001 consumer
            reads a USD rate exactly as before).
        fetched_at: Epoch seconds when the rate was fetched.
        stale: ``True`` when this rate was served from cache past its TTL as
            an offline-degrade fallback (age warning for the UI). Fresh rates
            carry ``False``.
    """

    per_btc: float
    fetched_at: float
    currency: str = DEFAULT_DISPLAY_CURRENCY
    stale: bool = False

    def age_s(self) -> float:
        """Seconds since this rate was fetched (computed against the clock)."""
        return _now() - self.fetched_at


class PriceOracle:
    """Cached, TTL-bounded per-BTC price oracle in the display currency.

    Args:
        client: The shared :class:`ChainClient` to query through. Backends
            that carry no ``supports_price`` capability (Electrum/bitcoind
            adapters, TCK-ONB-004 plan OQ-2) are treated as a PERMANENT
            price outage: :meth:`fresh` / :meth:`stale_ok` raise
            :class:`PriceUnavailableError` without a fetch — fiat-denominated
            ``create_tx`` refuses cleanly, sats-only keeps working.
        ttl_s: Cache lifetime in seconds; defaults to
            ``Settings.price_ttl_s`` (60s).
        enabled: Whether the oracle may query the network; defaults to
            ``Settings.price_enabled`` (``True``). When ``False`` the oracle
            is never called and :meth:`fresh` / :meth:`stale_ok` raise
            :class:`ConfigDisabled`.
        currency: The display currency — either a canonical/any-case code
            (``"EUR"`` == ``"eur"``) or a zero-argument callable returning
            one, re-read on EVERY fetch decision so a settings change takes
            effect on the next quote with no restart (TCK-FIAT-002; the app
            injects its env > config-file > stored > default ladder reader,
            :func:`localwallet.config.resolve_display_currency`, the same
            per-read ladder shape the coin-selection keys use). ``None``
            defaults to ``Settings.display_currency`` (env/file rung; empty
            = ``"usd"``). A cached rate is only ever served for the currency
            it was fetched in — a currency switch refetches; during an
            outage after a switch there is nothing to degrade to, and the
            ladder raises :class:`PriceUnavailableError` (never a
            wrong-currency number).

    Raises:
        ValueError: If ``ttl_s`` is not a positive, finite number, or
            ``currency`` is not a code from the closed display enum.
    """

    def __init__(
        self,
        client: ChainClient,
        ttl_s: float | None = None,
        enabled: bool | None = None,
        currency: str | Callable[[], str] | None = None,
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
        self._currency_fn: Callable[[], str] | None = None
        self._currency = DEFAULT_DISPLAY_CURRENCY
        if currency is not None:
            if callable(currency):
                self._currency_fn = currency
                normalize_display_currency(str(currency()), "display currency")  # eager pin
            else:
                self._currency = normalize_display_currency(
                    currency, "display currency"
                )
        elif settings.display_currency.strip():
            self._currency = normalize_display_currency(
                settings.display_currency, "LOCALWALLET_DISPLAY_CURRENCY"
            )
        self._client = client
        self._ttl_s = ttl_f
        self._enabled = bool(enabled)
        self._cache: Rate | None = None

    # -- fetching / caching ------------------------------------------------

    def _current_currency(self) -> str:
        """The display currency RIGHT NOW (static code, or the injected
        ladder reader re-read per call; normalized case-insensitively,
        fail-closed on an out-of-enum answer)."""
        if self._currency_fn is None:
            return self._currency
        return normalize_display_currency(str(self._currency_fn()), "display currency")

    def fresh(self) -> Rate:
        """Return a rate that is fresh (age < TTL) if at all possible.

        A backend without a price feed (``supports_price`` falsy — the
        capability seam of the TCK-ONB-004 plan) is the no-cache failure
        branch of the ladder directly: :class:`PriceUnavailableError`,
        nothing fetched, nothing fabricated.
    

        Degrade ladder (R10, OQ4; see ADR-0011):
        - cached value **in the current display currency** younger than the
          TTL → returned with ``stale=False``;
        - otherwise fetch **and parse**: on any failure — transport error,
          HTTP error, *or a malformed 200 payload* (shape validation is part
          of the failure path, so a 200-with-garbage degrades exactly like
          an outage) **with** a same-currency cached value younger than
          :data:`MAX_STALE_AGE_S` → the stale value is returned with
          ``stale=True`` (age warning, offline degrade);
        - on failure **without** any same-currency cache, or with a cache
          older than the staleness cap → :class:`PriceUnavailableError`
          (callers fall back to sats-only). A cache fetched in a NO LONGER
          current currency never degrades — a wrong-currency number is
          worse than none.

        A malformed payload is never cached: only a fully validated rate
        replaces the cached value.
        """
        self._check_enabled()
        self._check_supports_price()
        currency = self._current_currency()
        cached = self._cache
        same_currency = cached is not None and cached.currency == currency
        if same_currency and _now() - cached.fetched_at < self._ttl_s:  # type: ignore[union-attr]
            return cached
        try:
            payload = self._client.get_json(_PRICE_PATH, _PRICE_KIND)
            rate = _parse_rate(payload, _PRICE_KIND, currency)
        except ChainError as exc:
            if same_currency and _now() - cached.fetched_at <= MAX_STALE_AGE_S:  # type: ignore[union-attr]
                return _with_stale(cached)
            raise PriceUnavailableError("price unavailable") from exc
        self._cache = rate
        return rate

    def stale_ok(self) -> Rate:
        """Best-effort rate that tolerates staleness (explicit offline variant).

        Serves a same-display-currency cached value even past its TTL — and
        regardless of :data:`MAX_STALE_AGE_S`, which only caps what
        :meth:`fresh` will degrade to — rather than requiring a successful
        refetch; the rate carries its fetch timestamp (``age_s``), so the
        caller decides how old is too old. Attempts a refresh first when the
        cache is cold (or currency-switched); a malformed 200 payload fails
        exactly like a transport failure. Only raises
        :class:`PriceUnavailableError` when there is no same-currency cache
        and the fetch/parse fails (nothing to degrade to), or
        :class:`ConfigDisabled` when disabled.
        """
        self._check_enabled()
        self._check_supports_price()
        currency = self._current_currency()
        if self._cache is not None and self._cache.currency == currency:
            return self._cache
        try:
            payload = self._client.get_json(_PRICE_PATH, _PRICE_KIND)
            rate = _parse_rate(payload, _PRICE_KIND, currency)
        except ChainError as exc:
            raise PriceUnavailableError("price unavailable") from exc
        self._cache = rate
        return rate

    def invalidate(self) -> None:
        """Drop the cached rate; the next call refetches."""
        self._cache = None

    # -- pure conversion helpers ------------------------------------------

    def sats_to_usd(self, sats: int, rate: Rate) -> int:
        """Convert satoshis to whole minor units of the rate's currency.

        Name kept from TCK-FIAT-001 (injected-oracle test seams and the
        handlers route through it positionally): since TCK-FIAT-002 it
        converts in ``rate.currency`` — the USD default behaves byte-
        identically (whole US cents), a EUR rate yields euro cents, a JPY
        rate whole yen (zero-decimal currency, ``minor_per_unit``).

        Rounding policy: **floors to the nearest whole minor unit**,
        computed with ``Decimal`` to avoid float drift. Returns an ``int``
        count of minor units — callers format for display. A sub-unit
        amount (e.g. 1 sat at a low rate) floors to ``0``; the sat count
        is never altered.

        Raises:
            ValueError: If ``sats`` is not a non-negative ``int`` or ``rate``
                is not a positive :class:`Rate` in a supported currency.
        """
        _validate_sats(sats)
        _validate_rate(rate)
        minor = (
            Decimal(sats)
            * Decimal(repr(rate.per_btc))
            * minor_per_unit(rate.currency)
            / _SATS_PER_BTC
        )
        return int(minor.to_integral_value(rounding=ROUND_FLOOR))

    def usd_to_sats(self, usd: float, rate: Rate) -> int:
        """Convert an amount in the rate's currency to whole satoshis.

        Name kept for the same TCK-FIAT-001-compat reason: since
        TCK-FIAT-002 ``usd`` is an amount in MAJOR units of
        ``rate.currency`` (e.g. ``1.25`` dollars at a USD rate, ``1.25``
        euros at a EUR rate) — the display-currency conversion rides the
        setting, never a caller-chosen code.

        Rounding policy: **floors to the nearest whole sat**, computed with
        ``Decimal`` to avoid float drift. A sub-sat fraction floors away, so
        tiny amounts can convert to ``0`` sats.

        Raises:
            ValueError: If ``usd`` is not a non-negative number or ``rate``
                is not a positive :class:`Rate`.
        """
        _validate_usd(usd)
        _validate_rate(rate)
        sats = Decimal(repr(usd)) * _SATS_PER_BTC / Decimal(repr(rate.per_btc))
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
    return replace(rate, stale=True)


def _parse_rate(payload: Any, kind: str, currency: str = "usd") -> Rate:
    """Strictly validate a ``/v1/prices`` payload and extract the per-BTC
    rate in ``currency`` (canonical code; the endpoint keys are the
    uppercased codes — USD, EUR, GBP, CAD, CHF, AUD, JPY).

    Fail closed: the payload must be an object carrying a non-boolean,
    positive, **finite** ``float``/``int`` field for that currency within a
    plausible magnitude (``0 < per_btc <= _MAX_FIAT_PER_BTC``). Non-finite
    values are rejected explicitly because Python's ``json`` parses the bare
    ``NaN``/``Infinity`` tokens, and huge JSON integers are converted to
    ``float`` defensively so an ``OverflowError`` becomes a value-free
    :class:`ChainError`. Anything else raises :class:`ChainError` naming
    only the field/problem — never the rate value. ``time`` is accepted for
    shape parity but not required; ``fetched_at`` is the moment *we*
    fetched, not the provider's timestamp.
    """
    field = currency.upper()
    if not isinstance(payload, dict):
        raise ChainError(f"{kind} response was not an object")
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ChainError(f"{kind} response has missing or invalid '{field}'")
    try:
        per_btc = float(value)
    except OverflowError as exc:
        raise ChainError(f"{kind} response '{field}' is not representable") from exc
    if not math.isfinite(per_btc) or per_btc > _MAX_FIAT_PER_BTC:
        raise ChainError(f"{kind} response '{field}' is not a finite, plausible rate")
    return Rate(per_btc=per_btc, fetched_at=_now(), currency=currency)


def _validate_sats(sats: Any) -> None:
    if isinstance(sats, bool) or not isinstance(sats, int) or sats < 0:
        raise ValueError("sats must be a non-negative integer")


def _validate_usd(usd: Any) -> None:
    if isinstance(usd, bool) or not isinstance(usd, (int, float)) or usd < 0:
        raise ValueError("usd must be a non-negative number")


def _validate_rate(rate: Any) -> None:
    if not isinstance(rate, Rate):
        raise TypeError("rate must be a Rate")
    if rate.currency not in _MINOR_PER_UNIT:
        raise ValueError("rate currency is not a supported display currency")
    usd = rate.per_btc
    if isinstance(usd, bool) or not isinstance(usd, (int, float)) or usd <= 0:
        raise ValueError("rate must have a positive per_btc")
    try:
        usd_f = float(usd)  # huge ints overflow defensively
    except OverflowError as exc:
        raise ValueError("rate must have a finite per_btc") from exc
    if not math.isfinite(usd_f) or usd_f > _MAX_FIAT_PER_BTC:
        raise ValueError("rate must have a finite, plausible per_btc")

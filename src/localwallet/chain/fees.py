"""Fee estimation: target-follower bids over the projected next blocks,
recommended fees as fallback.

Two derivation layers, one cache, one combined refresh (TCK-FEE-003,
USER SPEC 2026-09-12, refining the TCK-FEE-001 floor-follower):

1. **Target-follower (primary).** Source: ``GET {base}/v1/fees/mempool-blocks``
   (projected next blocks; ``feeRange[0]`` — the lowest fee quantile — is
   that block's floor). ``B₀`` = first block's bottom (the next block),
   ``B₁`` = second block's bottom. Rates are **integer centisat/vB**
   (1 sat/vB = 100; ``121`` renders as ``1.21``):

   =================  ==========================================================
   ``FeeTarget``      rate
   =================  ==========================================================
   ``FAST`` (faster)  ``2 × MEDIUM`` (doubling of the ROUNDED target); every
                      rung is then MAX'd with the min-relay floor below
                      (section 3) — the old FAST-only ``minimumFee`` floor
                      (FEE-001) is generalized to all rungs
   ``MEDIUM`` (the   ``B₀ × 1.15`` rounded HALF-EVEN to 2 decimals, clamped up
    target)          so it is never below ``B₀`` itself (a floor never
                      undercuts its source)
   ``SLOW`` (slower)  ``B₁`` rounded UP (ceil) to 2 decimals — no markup; with
                      only ONE projected block: ``B₀`` itself (the floor,
                      ceil-2dp)
   =================  ==========================================================

   The parser **enforces** projected-block bottoms to be non-increasing with
   depth (a payload that breaks the order is rejected, not trusted — the
   FEE-001 security-review protection, kept because the code invariant
   ``FAST >= MEDIUM >= SLOW`` now depends on ``B₁ <= B₀``), so the ordering
   is a code invariant over accepted inputs — not a data-source assumption.
   An empty projected list or any malformed shape aborts the derivation.
   The FEE-001 recent-blocks floor (R₅, tip + ``/v1/blocks/{tip}``) is DROPPED:
   this policy has ONE source endpoint, and the ×1.15 markup covers R₅'s
   anti-underbid role (FEE-001's motivating case: B₀=0.3 with blocks
   confirming at 0.34 → target 0.345 ≥ 0.34).

   **Rounding (money-path rule, pinned):** all math is ``Decimal`` on the
   shortest-repr of the JSON float — target half-even at 2 dp
   (1.05567928730512 × 1.15 = 1.21403… → **1.21**, the user's own example;
   normal rounding, NOT ceil — ceil would say 1.22), floor terms (SLOW, the
   target clamp) ceil at 2 dp so no bid ever undercuts its floor. Rates
   leave this module ONLY as integer centisat/vB — no float crosses into
   money math.

2. **Recommended fallback.** ``GET {base}/v1/fees/recommended`` (mempool.space
   shape) maps ``FAST``/``MEDIUM``/``SLOW`` onto ``fastestFee``/``halfHourFee``/
   ``hourFee`` (whole sats/vB → exact centisat). ANY failure of the
   mempool-blocks endpoint (transport, HTTP, or strict shape validation)
   degrades to this mapping; the estimate's :class:`FeeSource` records which
   path produced it so narration can stay honest. The recommended payload
   itself still fails closed (a malformed/failed ``/v1/fees/recommended``
   raises :class:`~localwallet.chain.esplora.ChainError` — there is no lower
   layer to fall back to).

3. **Min-relay floor clamp (TCK-FEE-004, USER CORRECTED SPEC 2026-09-13,
   code-review fix 2026-09-14).** A policy-v2 rung may legitimately compute
   BELOW the min-relay floor that actually rejects transactions (the live
   failure: ``send 100000 sats to bc1q…`` died with ``psbt_failed (fee is
   below the min-relay floor for this transaction size)`` because
   MEDIUM = B₀ × 1.15 sat under it). Every bid this estimator finalizes is
   therefore ``MAX(rung, floor)``, with EACH rung clamped INDEPENDENTLY
   (MEDIUM→max(medium,floor), FASTER→max(faster,floor), SLOWER→
   max(slower,floor) — pinned semantics: simplest and honest; max is
   monotone, so the ``FAST >= MEDIUM >= SLOW`` invariant survives).

   The RELAY floor — the rate the network will actually accept — resolves
   by precedence, never from a congestion estimate:

   * **native node capability**: the wallet chain client's OPTIONAL
     ``ChainClient`` method :meth:`~localwallet.chain.bitcoind.
     BitcoindClient.min_relay_centisat_vb` (``getmempoolinfo``'s
     ``minrelaytxfee``; BTC/kvB, exact Decimal → centisat/vB). The
     estimator BIDS over the public fee source but takes its FLOOR from
     the user's own node: the production wiring injects the wallet client
     (and re-points it on a hot-swap) for exactly this.
   * **electrum**: honestly ABSENT (checked — ``server.features`` carries
     no relay fee and the legacy ``blockchain.relayfee`` is deprecated
     with unit-ambiguous answers; we never invent a dialect).
   * any query failure — ANY exception flavor: fail-closed, never a
     failed send.
   * **assumed floor** (the fallback and the irreducible rail): 0.1 sat/vB
     (= 100 sat/kvB, Core's ``DEFAULT_MIN_RELAY_TX_FEE`` in
     ``src/policy/policy.h``, master AND v31.0 — TCK-FEE-005 corrected
     this from the historical 1 sat/vB default that upstream LOWERED),
     ``_ASSUMED_MIN_RELAY_CENTISAT_VB``, the constant the tx engine's
     own build gates enforce — a node answering lower cannot license a
     bid our builder would refuse.

   Two consumers, two deliberately DIFFERENT floors (the code-review
   MAJOR: a congestion figure must never out-veto the node's relay floor
   on an EXPLICIT bid):

   * **policy rungs** (``estimate``): congestion-informed — every rung is
     MAX'd with ``max(minimumFee×100, relay floor)`` on the
     Esplora/publicinfo path (the next-block congestion bound is a
     legitimate POLICY bid floor, FEE-003-sanctioned) and with the relay
     floor alone on the backend-native path (no minimumFee exists there).
   * **the EXPLICIT-rate seam** (``clamp_to_min_relay_floor``): the RELAY
     floor ONLY — node capability, else the assumed constant; NEVER
     ``minimumFee``. An explicit 1 sat/vB bids 1 THROUGH congestion
     whenever the node's own floor is 1 sat/vB (pin). The seam answers
     from ONE TTL-cached floor query — never a snapshot refresh, zero
     chain calls when no floor-capable node is wired (the publicinfo
     wiring keeps FEE-002's "explicit rate ⇒ no chain calls" property).

   A clamped bid is flagged (``FeeEstimate.clamped``) so the card can
   narrate it once, honestly, quoting the floor verbatim. This floor is
   DISTINCT from ``tx/replacement.py``'s BIP-125 INCREMENTAL relay floor
   (a replacement must out-pay the original it evicts — a different
   question and a different constant): this clamp applies to a
   transaction's INITIAL bid only and never touches replacement math.
   An EXPLICIT user rate below the floor is likewise clamped UP and
   narrated (never silently refused, never silently altered —
   :meth:`FeeEstimator.clamp_to_min_relay_floor` is that seam).

Motivating cases (user live runs): 2026-09-07 — ``fastestFee`` bid 2 sat/vB
while blocks confirmed down to ~0.34 (FEE-001's floor-follower); 2026-09-12 —
the integer ceil still bid 2 sat/vB where the next block's floor allows
``1.0557 × 1.15 → 1.21`` (this policy; ADR-0011 amendments); 2026-09-13 —
MEDIUM computed under the node's min-relay floor and the send failed
outright (TCK-FEE-004: MAX(bid, floor)).

The full recommended payload also carries ``economyFee`` and ``minimumFee``
(all in sats/vB). ``SLOW`` deliberately does NOT fall back to ``economyFee``
when ``hourFee`` is missing: we fail closed on any missing/malformed key
rather than silently estimating a fee the user might not expect (the fee
*value* itself is not secret, but the shape contract is strict, mirroring
the fail-closed style of :mod:`localwallet.chain.esplora`).

**Backend-native path (TCK-ONB-004 M1).** A backend without the mempool.space
fee endpoints (no ``get_json`` — e.g. :class:`~localwallet.chain.electrum.
ElectrumClient`) gets its bids from the client's own ``estimate_fee(target)``
(a single ``estimatefee``/``estimatesmartfee``-style source, recorded with
:attr:`FeeSource.RECOMMENDED` provenance; whole sats/vB → exact centisat):
the target-follower simply does not exist there and is NEVER faked. Its
failure fails closed like a broken recommended payload — there is no lower
layer. ``minimum_fee_sat_vb()`` raises :class:`ChainError` on this path (the
backend exposes no such payload field and we never invent one). The
min-relay floor on this path rides a SEPARATE optional capability
(``min_relay_centisat_vb()``; present on bitcoind, honestly absent on
electrum) with the assumed 0.1 sat/vB as its fail-closed fallback — see the
floor section above.

Payloads are cheap but rate-limited (R11), so estimates are cached with a
short, settings-driven TTL; one refresh (recommended + mempool-blocks)
populates the whole cache. All network I/O flows through the injected
:class:`~localwallet.chain.esplora.ChainClient` — this module never creates
its own ``httpx`` client.

Only the estimator and its accessors live here. Fee *computation* for a
specific transaction is the tx-engine's job (integer centisat/vB →
``ceil(vsize × c / 100)`` sats, ADR-0012/0011 amendments).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from localwallet.chain.esplora import ChainError
from localwallet.config import Settings

if TYPE_CHECKING:
    from localwallet.chain.esplora import ChainClient

__all__ = [
    "FeeEstimate",
    "FeeEstimator",
    "FeeSource",
    "FeeTarget",
    "format_sat_vb",
]

# Endpoint kinds + paths used in error messages / requests (log-scrubbed).
_FEES_KIND = "fees-recommended"
_FEES_PATH = "/v1/fees/recommended"
_MEMPOOL_BLOCKS_KIND = "fees-mempool-blocks"
_MEMPOOL_BLOCKS_PATH = "/v1/fees/mempool-blocks"

# mempool.space recommended-fees payload keys (strictly required, all ints).
_REQUIRED_KEYS = ("fastestFee", "halfHourFee", "hourFee", "economyFee", "minimumFee")

#: The user's markup over the next block's floor (TCK-FEE-003 spec: 1.15).
_TARGET_MARKUP = Decimal("1.15")

#: One centisat/vB quantize step (rates are integer centisat/vB end to end).
_CENT = Decimal("0.01")

#: Magnitude ceiling for a projected/observed feeRange bottom (sats/vB),
#: mirroring price.py's plausible-rate bound (ADR-0011 §6): it equals the
#: tx engine's own maximum bid (10_000 sat/vB — dust.py's rate ceiling,
#: unchanged by TCK-FEE-005), so a bottom above it is a broken payload, not
#: a pricey one — and every
#: accepted value keeps the Decimal math inside the default 28-digit
#: context (a finite-but-absurd ``1e40`` would otherwise blow
#: ``quantize`` out of the documented ChainError fallback as an
#: InvalidOperation).
_MAX_BOTTOM_SAT_VB = 10_000

#: The ASSUMED min-relay floor in this module's unit (integer centisat/vB):
#: Bitcoin Core's ``DEFAULT_MIN_RELAY_TX_FEE`` (``src/policy/policy.h``,
#: master AND v31.0) of 100 sat/kvB = 0.1 sat/vB = 10 centisat/vB.
#: TCK-FEE-005 corrected this constant from 100 (= 1 sat/vB): that was the
#: HISTORICAL Core default (1000 sat/kvB) which upstream LOWERED, and the
#: FEE-004-era note claiming 1 sat/vB was justified by parity with the tx
#: engine's build gate (``tx/psbt.py``'s min-relay refusal) — parity that
#: held only because BOTH sides carried the same outdated figure. The gate
#: now rides this same 10-centisat/vB rail (``tx/dust.py``
#: ``_DEFAULT_MIN_RELAY_CENTISAT_VB``, pinned equal to this constant by
#: tests/test_tx_dust.py — one source of truth: gate rail == estimator
#: rail). TCK-FEE-004 uses it two ways: (a) the
#: fail-closed floor when no backend/source can answer the floor query,
#: and (b) the irreducible LOWER BOUND of a queried floor — a node
#: answering below it cannot license a bid our own builder would still
#: refuse (a node answering HIGHER still wins: MAX(advertised, assumed)).
#: This is a rate floor, never a size-derived fee (the dust/min-relay-
#: from-script-size invariant is untouched); it is DISTINCT from
#: tx/replacement.py's BIP-125 incremental-relay rate, which governs how
#: much MORE a replacement must pay and is deliberately not touched here.
_ASSUMED_MIN_RELAY_CENTISAT_VB = 10


def _apply_floor(snapshot: _Snapshot, floor_c: int) -> _Snapshot:
    """MAX every rung with the resolved min-relay floor, INDEPENDENTLY
    (TCK-FEE-004's pinned semantics: simplest and honest — each of
    MEDIUM/FASTER/SLOWER becomes ``max(rung, floor)``). ``max`` is monotone,
    so the accepted-input invariant ``FAST >= MEDIUM >= SLOW`` survives. A
    rung the floor actually raised carries ``clamped=True`` (narration says
    so once, quoting the floor verbatim). The floor never drops the bid, so
    an estimate that already met it is returned unchanged. The effective
    floor is ``max(queried, assumed)`` — our own build gate enforces the
    assumed 0.1 sat/vB regardless of what a node answers.
    """
    floor_c = max(floor_c, _ASSUMED_MIN_RELAY_CENTISAT_VB)
    estimates = {
        target: (
            replace(est, rate_centisat_vb=floor_c, clamped=True)
            if est.rate_centisat_vb < floor_c
            else est
        )
        for target, est in snapshot.estimates.items()
    }
    return replace(snapshot, estimates=estimates)


def _backend_min_relay_centisat_vb(client: Any) -> int:
    """Resolve the min-relay floor over the backend's OPTIONAL capability.

    TCK-FEE-004, riding the ONB-004 contract: a backend that can name its
    own floor exposes ``min_relay_centisat_vb() -> int`` (centisat/vB;
    today :class:`~localwallet.chain.bitcoind.BitcoindClient` via
    ``getmempoolinfo.minrelaytxfee``). Absence, an unusable answer or ANY
    exception from the query fail CLOSED to the assumed 0.1 sat/vB (the
    code-review LOW: no exception flavor of a floor query may fail a send)
    — never a fabricated node figure. Electrum honestly ABSENTS this
    capability (:class:`~localwallet.chain.electrum.ElectrumClient` defines
    no such method: ``server.features`` carries no relay fee and the legacy
    ``blockchain.relayfee`` is deprecated/unit-ambiguous — no dialect is
    invented), so its floor is the assumed constant.
    """
    getter = getattr(client, "min_relay_centisat_vb", None)
    if getter is None:
        return _ASSUMED_MIN_RELAY_CENTISAT_VB
    try:
        value = getter()
    except Exception:  # noqa: BLE001 — fail-closed floor fallback, value-free
        return _ASSUMED_MIN_RELAY_CENTISAT_VB
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return _ASSUMED_MIN_RELAY_CENTISAT_VB
    return max(value, _ASSUMED_MIN_RELAY_CENTISAT_VB)


def _now() -> float:
    """Indirection over ``time.time`` so tests can control the clock."""
    return time.time()


class FeeTarget(StrEnum):
    """Confirmation-target preset (faster / the target / slower).

    The protocol values are unchanged (``create_tx.fee_target``): FAST is
    the "faster" rung, MEDIUM the default "target" bid, SLOW the "slower"
    rung — see the module docstring for how each rate is derived.
    """

    FAST = "fast"
    MEDIUM = "medium"
    SLOW = "slow"


class FeeSource(StrEnum):
    """Which derivation produced a :class:`FeeEstimate` (honesty marker).

    Exported but unconsumed as of TCK-FEE-001: this is the provenance hook
    for the follow-up UI narration work (surfacing floor-follower vs
    recommended bids honestly), which is deliberately not wired here.
    ``FLOOR_FOLLOWER`` names the target-follower derivation (the value
    string predates TCK-FEE-003; the wire value is kept for honesty-marker
    stability).
    """

    FLOOR_FOLLOWER = "floor-follower"
    RECOMMENDED = "recommended"


# Fallback mapping: user-facing targets onto recommended-payload keys.
_TARGET_KEYS = {
    FeeTarget.FAST: "fastestFee",
    FeeTarget.MEDIUM: "halfHourFee",
    FeeTarget.SLOW: "hourFee",
}


def format_sat_vb(centisat_vb: int) -> str:
    """Render an integer centisat/vB rate as human sats/vB text (TCK-FEE-003).

    Pure formatting for the confirmation card / narration, which quote the
    rate verbatim from tool output: ``121 -> "1.21"``, ``242 -> "2.42"``,
    ``200 -> "2"`` (whole rates render without decimals, exactly like the
    pre-fractional display), ``55 -> "0.55"``. Trailing zeros are dropped,
    never leading fraction digits.

    Raises:
        TypeError: If the value is not an ``int`` (bools are ints in
            Python — rejected explicitly, the chain-wide discipline).
        ValueError: If the value is negative (no negative rate exists).
    """
    if isinstance(centisat_vb, bool) or not isinstance(centisat_vb, int):
        raise TypeError("centisat_vb must be an integer")
    if centisat_vb < 0:
        raise ValueError("centisat_vb must be non-negative")
    whole, frac = divmod(centisat_vb, 100)
    if frac == 0:
        return str(whole)
    if frac % 10 == 0:
        return f"{whole}.{frac // 10}"
    return f"{whole}.{frac:02d}"


@dataclass(frozen=True)
class FeeEstimate:
    """A fee bid for one target, in integer centisat/vB.

    Attributes:
        target: The confirmation-target preset this estimate is for.
        rate_centisat_vb: Fee bid in satoshis per virtual byte × 100
            (``int``; 121 = 1.21 sat/vB). Integer money math end to end
            (docs/fee-fractional-plan.md): the tx engine computes
            ``ceil(vsize × rate_centisat_vb / 100)`` sats. Format for
            display with :func:`format_sat_vb` — never re-derive a rate
            from a rounded whole-sat figure.
        source_timestamp: Epoch seconds when the estimate was fetched from
            the provider (server-side data; used to show freshness to the
            user).
        source: The derivation path that produced the bid
            (:class:`FeeSource`): target-follower over mempool projections,
            or the recommended-fees fallback.
        clamped: TCK-FEE-004 — True when this bid is the min-relay FLOOR
            (the MAX(bid, floor) clamp raised it above the policy-derived
            rung; the rate shown IS the floor then). False means the policy
            rung already met or cleared the floor and is untouched.
            Narration uses it to say so once, never silently.
    """

    target: FeeTarget
    rate_centisat_vb: int
    source_timestamp: float
    source: FeeSource
    clamped: bool = False


@dataclass(frozen=True)
class _Snapshot:
    """A parsed estimate set (one derivation layer) plus fetch metadata."""

    estimates: dict[FeeTarget, FeeEstimate]
    minimum_fee_sat_vb: int
    fetched_at: float
    #: True when the bids came from a backend-native ``estimate_fee`` (no
    #: Esplora endpoints existed): ``minimum_fee_sat_vb`` has no value to
    #: serve and refuses instead of fabricating one.
    native: bool = False


class FeeEstimator:
    """Cached fee estimator: target-follower bids, recommended fallback.

    One combined refresh populates all three targets, so a single (fresh)
    call fetches once: the recommended payload always, then — while building
    the target-follower set — the projected blocks. :meth:`estimate` and
    :meth:`minimum_fee_sat_vb` serve a cached value while it is younger than
    ``ttl_s`` and refetch otherwise.

    Fail-closed policy: the recommended payload is validated strictly (a
    missing or malformed key raises :class:`~localwallet.chain.esplora.ChainError`
    and the cache is left untouched — a previously-good payload keeps
    serving until its TTL). The mempool-blocks endpoint is ALSO validated
    strictly, but a failure there degrades to the recommended mapping
    (source marked :attr:`FeeSource.RECOMMENDED`) instead of raising. A
    recommended fee of ``0`` sat/vB is itself malformed — a zero estimate is
    a broken payload, not a cheap one; the tx engine floors fees via
    min-relay separately (ADR-0012 §3, pinned in ADR-0011).

    Args:
        client: The shared :class:`ChainClient` to query through (no
            second transport is created; Esplora backends additionally use
            the target-follower endpoints, non-Esplora backends their native
            ``estimate_fee``). Network access stays in ``chain/``.
        ttl_s: Cache lifetime in seconds; defaults to
            ``Settings.fee_cache_ttl_s`` (30s).
        relay_floor_client: TCK-FEE-004 (code-review fix): the WALLET chain
            client whose node defines the min-relay floor the production
            estimator must respect — duck-typed on the OPTIONAL
            ``min_relay_centisat_vb()`` capability, like every other seam
            here (bitcoind answers, electrum honestly doesn't, ``None`` or
            any failure → the assumed 0.1 sat/vB). The bids keep riding
            ``client`` (the public fee source); only the RELAY floor rides
            the node. Absent (or any client without the capability) the
            floor falls back to ``client`` itself — the backend-native
            path, where estimator and node are the same object.
            :meth:`set_relay_floor_client` re-points it on a hot-swap.

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
        relay_floor_client: ChainClient | None = None,
    ) -> None:
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
        self._relay_floor_client = relay_floor_client
        #: TTL-bounded (min_relay_centisat_vB, fetched_at) pair — the light
        #: floor-only answer the explicit-rate seam consumes (TCK-FEE-004).
        self._floor_cache: tuple[int, float] | None = None

    def set_relay_floor_client(self, client: ChainClient | None) -> None:
        """Re-point the min-relay FLOOR source at the (hot-swapped) wallet
        backend and drop the cached floor. The estimator instance itself
        stays ONE shared object across a swap (one cache, one TTL, the bids
        keep riding the public fee source) — only the node that DEFINES the
        relay floor moves with the backend (TCK-FEE-004 code-review fix)."""
        self._relay_floor_client = client
        self._floor_cache = None

    def _relay_floor_centisat_vb(self) -> int:
        """The RELAY floor alone, TTL-cached: node capability → the assumed
        0.1 sat/vB. NEVER the congestion ``minimumFee`` and NEVER a snapshot
        refresh — answering an explicit-rate clamp costs at most ONE floor
        query, zero chain calls when the floor source has no capability."""
        now = _now()
        if self._floor_cache is not None and now - self._floor_cache[1] < self._ttl_s:
            return self._floor_cache[0]
        client = self._relay_floor_client
        if client is None:
            client = self._client
        floor_c = _backend_min_relay_centisat_vb(client)
        self._floor_cache = (floor_c, now)
        return floor_c

    def _get_snapshot(self) -> _Snapshot:
        """Return the cache if fresh, otherwise fetch + parse + clamp + cache.

        Every snapshot leaves here with each rung MAX'd against the
        congestion-informed POLICY floor (TCK-FEE-004 + code-review fix):
        the native path uses the relay floor (:meth:`_relay_floor_centisat_
        vb`, no minimumFee exists there), the Esplora/publicinfo path
        ``max(minimumFee×100, relay floor)`` — the next-block congestion
        bound is a legitimate POLICY bid floor (FEE-003-sanctioned) and the
        node's floor participates (MAX in) when one is wired. A degraded
        target-follower attempt clamps the fallback snapshot the same way.
        """
        now = _now()
        if self._cache is not None and now - self._cache.fetched_at < self._ttl_s:
            return self._cache
        if not hasattr(self._client, "get_json"):
            # Backend-native path (Electrum/bitcoind adapters expose no
            # Esplora JSON): one estimate_fee per target, target-follower
            # skipped — never faked. Failures fail closed (ChainError).
            snapshot = _apply_floor(self._native_snapshot(now), self._relay_floor_centisat_vb())
            self._cache = snapshot
            return snapshot
        payload = self._client.get_json(_FEES_PATH, _FEES_KIND)
        recommended = _parse_recommended(payload, _FEES_KIND, now)  # may raise (as before)
        snapshot = recommended
        try:
            snapshot = self._target_follower(recommended.minimum_fee_sat_vb, now)
        except ChainError:
            snapshot = recommended  # fail-closed degrade; value-free, never logged
        snapshot = _apply_floor(
            snapshot,
            max(snapshot.minimum_fee_sat_vb * 100, self._relay_floor_centisat_vb()),
        )
        self._cache = snapshot
        return snapshot

    def _native_snapshot(self, now: float) -> _Snapshot:
        """Backend-native estimates: one ``estimate_fee(target)`` per target.

        The adapter's answer (whole sat/vB ``int``, already strictly validated
        by the client — e.g. Electrum ``estimatefee``) is scaled exactly to
        centisat/vB and used verbatim, with :attr:`FeeSource.RECOMMENDED`
        provenance (single-source recommended style, plan §0). A ChainError
        from any target aborts the refresh; the previously cached snapshot
        keeps serving until its TTL, exactly like a broken recommended
        payload.
        """
        estimates: dict[FeeTarget, FeeEstimate] = {}
        for target in FeeTarget:
            sat_per_vb = self._client.estimate_fee(target)
            if isinstance(sat_per_vb, bool) or not isinstance(sat_per_vb, int) or sat_per_vb <= 0:
                raise ChainError("fees-native estimate is missing or invalid")
            estimates[target] = FeeEstimate(
                target=target,
                rate_centisat_vb=sat_per_vb * 100,
                source_timestamp=now,
                source=FeeSource.RECOMMENDED,
            )
        return _Snapshot(
            estimates=estimates,
            minimum_fee_sat_vb=0,  # never served on the native path (see below)
            fetched_at=now,
            native=True,
        )

    def _target_follower(self, minimum_fee_sat_vb: int, now: float) -> _Snapshot:
        """Derive target-follower bids, or raise :class:`ChainError` (caught by the caller).

        TCK-FEE-003 user policy (all terms are endpoint data — no hardcoded
        fee constants; the 1.15 markup is the user's stated policy factor):

        - ``MEDIUM`` = ``B₀ × 1.15`` rounded half-even to 2 decimals, clamped
          up (ceil-2dp of ``B₀``) if that rounding ever lands below ``B₀``
          — a bid never undercuts its own floor;
        - ``FAST`` = ``2 × MEDIUM`` (the doubling acts on the ROUNDED target);
        - ``SLOW`` = ``B₁`` ceiled to 2 decimals (no markup; one projected
          block ⇒ ``B₀`` itself).

        Then :func:`_apply_floor` (applied by the caller) MAXes EACH rung
        independently with the source's min-relay floor — FEE-001's
        old "minimumFee bounds the FAST rung only" rule is generalized to
        every rung (TCK-FEE-004 pinned semantics: a sub-floor bid never
        leaves this module; the ladder can collapse onto the floor and
        that is honest, it cannot bid under it).

        With the parser's non-increasing-bottoms rule (``B₀ >= B₁``) this
        makes ``FAST >= MEDIUM >= SLOW`` a code invariant. All rounding is
        exact Decimal arithmetic; the outputs are integer centisat/vB.
        """
        bottoms = _parse_projected_bottoms(
            self._client.get_json(_MEMPOOL_BLOCKS_PATH, _MEMPOOL_BLOCKS_KIND),
            _MEMPOOL_BLOCKS_KIND,
        )
        target_c = _to_cents(bottoms[0] * _TARGET_MARKUP, ROUND_HALF_EVEN)
        if Decimal(target_c).scaleb(-2) < bottoms[0]:  # rounding dipped under the floor
            target_c = _to_cents(bottoms[0], ROUND_CEILING)
        slow_c = _to_cents(bottoms[1] if len(bottoms) > 1 else bottoms[0], ROUND_CEILING)
        fast_c = 2 * target_c
        estimates = {
            target: FeeEstimate(
                target=target,
                rate_centisat_vb=rate_c,
                source_timestamp=now,
                source=FeeSource.FLOOR_FOLLOWER,
            )
            for target, rate_c in (
                (FeeTarget.FAST, fast_c),
                (FeeTarget.MEDIUM, target_c),
                (FeeTarget.SLOW, slow_c),
            )
        }
        return _Snapshot(
            estimates=estimates,
            minimum_fee_sat_vb=minimum_fee_sat_vb,
            fetched_at=now,
        )

    def estimate(self, target: FeeTarget) -> FeeEstimate:
        """Return the fee bid for ``target`` (integer centisat/vB, 1 sat/vB
        = 100), cached by TTL. Format with :func:`format_sat_vb` for display.

        TCK-FEE-004: the returned bid is already floored — every rung leaves
        the snapshot layer as ``MAX(policy rung, min-relay floor)``; check
        :attr:`FeeEstimate.clamped` to narrate a floor-raised rung once,
        honestly (the displayed rate IS the floor then).
        """
        if not isinstance(target, FeeTarget):
            raise TypeError("target must be a FeeTarget")
        return self._get_snapshot().estimates[target]

    def clamp_to_min_relay_floor(self, rate_centisat_vb: int) -> tuple[int, bool]:
        """MAX an EXPLICIT user bid with the min-relay floor (TCK-FEE-004).

        The create_tx / bump_fee finalization seam for ``fee_rate_sat_vb``:
        a user-quoted rate below the node's RELAY floor is RAISED to it and
        the ``raised`` flag comes back True, so the card narrates the
        change — never a silent alteration of an explicit rate, never a
        silent sub-floor bid that the node would refuse (MAX, never MIN —
        the user's corrected spec verbatim). The floor is the RELAY floor
        ONLY (native node capability → the assumed 0.1 sat/vB): NEVER the
        congestion ``minimumFee`` — an explicit 1 sat/vB bids 1 THROUGH
        congestion whenever the node's own floor is 1 sat/vB (the
        code-review MAJOR: a congestion estimate must not out-veto the
        node). Answering costs at most ONE TTL-cached floor query — never
        a snapshot refresh, zero chain calls when the floor source has no
        capability (the publicinfo wiring keeps FEE-002's "explicit rate ⇒
        no chain calls" property). This clamps the INITIAL bid only; the
        BIP-125 INCREMENTAL floor a replacement must clear is
        ``tx/replacement.py``'s distinct rule and is not consulted here.

        Fail-closed like the floor itself: ANY failure of the floor query
        (not just ChainError — the code-review LOW) degrades to the
        assumed 0.1 sat/vB (the floor the tx engine's build gate already
        enforces), so an explicit send never fails on a floor query and
        the schema's 1 sat/vB explicit floor then passes through untouched.

        Raises:
            TypeError: non-int / bool input. ValueError: negative.
                Messages are value-free.
        """
        if isinstance(rate_centisat_vb, bool) or not isinstance(rate_centisat_vb, int):
            raise TypeError("rate_centisat_vb must be an integer")
        if rate_centisat_vb < 0:
            raise ValueError("rate_centisat_vb must be non-negative")
        try:
            floor_c = self._relay_floor_centisat_vb()
        except Exception:  # noqa: BLE001 — no floor-query failure may fail a send
            floor_c = _ASSUMED_MIN_RELAY_CENTISAT_VB
        if rate_centisat_vb < floor_c:
            return floor_c, True
        return rate_centisat_vb, False

    def minimum_fee_sat_vb(self) -> int:
        """Return the ``minimumFee`` field (sats/vB), cached by TTL.

        Raises:
            ChainError: On the backend-native path — a non-Esplora backend
                exposes no separate minimum-fee figure and we never invent
                one (fail closed; the tx engine's min-relay floor from
                script size is the authority on that path anyway).
        """
        snapshot = self._get_snapshot()
        if snapshot.native:
            raise ChainError("fees-native backend exposes no minimum-fee endpoint")
        return snapshot.minimum_fee_sat_vb

    def invalidate(self) -> None:
        """Drop the cached payload AND the cached relay floor; the next
        call refetches."""
        self._cache = None
        self._floor_cache = None


def _to_cents(value: Decimal, rounding: str) -> int:
    """Quantize a sats/vB Decimal to 2 dp (``rounding``) as integer centisat/vB.

    Safe by construction: every input derives from a parsed bottom bounded
    by :data:`_MAX_BOTTOM_SAT_VB` (times the exact 1.15 / 2 factors), so the
    default 28-digit Decimal context can never overflow into
    ``InvalidOperation`` here.
    """
    return int(value.quantize(_CENT, rounding=rounding).scaleb(2).to_integral_value())


def _fee_range_bottom(container: dict[str, Any], kind: str, index: int) -> Decimal:
    """Strictly extract a block entry's ``feeRange`` bottom (lowest quantile).

    The bottom must be a non-boolean, **positive**, finite ``int``/``float``
    (sats/vB are fractional on these payloads); the exact Decimal of the
    value's shortest repr is returned — all fee math is Decimal, never
    binary float. Zero/negative/bool/string/NaN/Infinity are all malformed
    — :class:`ChainError`, value-free (never echoes the fee). Huge JSON ints
    overflow-convert defensively, and a finite-but-absurd magnitude beyond
    :data:`_MAX_BOTTOM_SAT_VB` is refused the same way (price.py §6 pattern —
    it keeps every later ``quantize`` inside the default Decimal context).
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
    if value > _MAX_BOTTOM_SAT_VB:
        raise ChainError(f"{kind} entry {index} has implausible 'feeRange' bottom")
    return Decimal(str(bottom))


def _parse_projected_bottoms(payload: Any, kind: str) -> list[Decimal]:
    """Validate the ``/v1/fees/mempool-blocks`` payload; return per-block bottoms.

    Non-empty list of objects each carrying a ``feeRange`` (ascending fee
    quantiles; only the bottom is used), with bottoms **non-increasing with
    depth** — the ``MEDIUM >= SLOW`` invariant depends on it (TCK-FEE-003
    reads blocks [0] and [1]), so a payload that breaks the order fails
    closed (to fallback) instead of being trusted — the FEE-001
    security-review protection, kept. An empty list means nothing is
    projected — no trustworthy floor — and fails closed too.
    """
    if not isinstance(payload, list) or not payload:
        raise ChainError(f"{kind} response was not a non-empty list")
    bottoms: list[Decimal] = []
    for index, entry in enumerate(payload):
        if not isinstance(entry, dict):
            raise ChainError(f"{kind} entry {index} is not an object")
        bottom = _fee_range_bottom(entry, kind, index)
        if bottoms and bottom > bottoms[-1]:
            raise ChainError(f"{kind} entry {index} breaks projected-fee ordering")
        bottoms.append(bottom)
    return bottoms


def _parse_recommended(payload: Any, kind: str, fetched_at: float) -> _Snapshot:
    """Strictly validate a recommended-fees payload (fail closed).

    Every required key must be present and a non-boolean, **positive**
    ``int`` — zero is rejected, not just negative: a 0 sat/vB estimate is a
    broken payload, not a free one (the tx engine floors fees via min-relay
    separately; ADR-0012 §3, pinned in ADR-0011), and ``minimumFee = 0`` is
    equally nonsense. Whole sats/vB scale exactly to centisat/vB. Any other
    shape raises :class:`ChainError` naming only the field and the problem
    kind — never a fee value. Bools are rejected because ``True/False`` are
    ``int`` subclasses in Python and would otherwise pass.
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
            rate_centisat_vb=values[field] * 100,
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

# ADR-0011: Price source & policy (OQ4)

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decides:** PROJECT.md OQ4 ("Price source & policy: provider, TTL,
  default-on vs opt-in, fallback when offline. Needed by Phase 2.")
- **Scope:** `src/localwallet/chain/price.py` (ticket TCK-P2-001); relates to
  ADR-0003 (chain backend / privacy caveat), PROJECT.md §7.4, R10, R11, and
  §9 (what leaves the machine).

## Context

The UI wants to show fiat equivalents for balances and amounts. The price of
BTC in USD is external data that must be fetched at runtime — an incidental
third-party dependency on top of the already-accepted public chain backend
(ADR-0003). OQ4 asked four things: which provider, what TTL, default-on vs
opt-in, and how to behave offline.

## Decision

1. **Provider: mempool.space `GET /v1/prices`** (extracting the `USD` field),
   reached through the same `EsploraClient` as all chain data. This keeps the
   same host/operator as the chain queries (ADR-0003), so no *additional*
   third party is introduced — the coincidental-privacy leak (§9, R7) that a
   public operator can associate queried data with an IP is already
   documented and surfaced in the UI for chain traffic, and the price query
   adds no new party to that disclosure.
2. **TTL: 60 seconds** by default, configurable via
   `LOCALWALLET_PRICE_TTL_S`. Prices are cheap but rate limits (R11) argue
   for caching; 60s is short enough to feel current and long enough to
   dampen load. Configurable so a self-hosted or local backend (Phase 4) can
   tighten or relax it.
3. **Default-on with opt-out.** Enabled by default
   (`LOCALWALLET_PRICE_ENABLED`, default `1`); `0` disables the oracle
   entirely — it is never called, and `PriceOracle.fresh()`/`stale_ok()`
   raise `ConfigDisabled`. Opt-out rather than opt-in because the fiat figure
   is a core convenience users expect out of the box, and the single shared
   host keeps the marginal privacy cost minimal.
4. **Offline degrade ladder** (R10): `fresh()` serves a cached rate younger
   than the TTL; otherwise it fetches *and parses* — any failure (transport
   error, HTTP error, or a malformed 200 payload: shape validation is part
   of the failure path, so a 200-with-garbage degrades exactly like an
   outage) serves the cached rate flagged `stale=True` (the UI shows the
   age), but only while that cached rate is younger than `MAX_STALE_AGE_S`
   (24 h; module constant in `price.py`). Beyond that staleness cap
   `fresh()` raises `PriceUnavailableError` instead of serving an ancient
   rate; with no cache at all it raises `PriceUnavailableError`
   (value-free) and callers fall back to sats-only display. An explicit
   `stale_ok()` variant serves whatever cache exists without requiring
   freshness and regardless of the cap — the rate carries its fetch
   timestamp, so the caller decides how old is too old. A malformed payload
   is never cached: only a fully validated rate replaces the cached value.
5. **Rounding policy:** sats → USD floors to whole US cents, USD → sats
   floors to whole sats, both computed with `Decimal` (no float drift in sat
   counts). Sats are always integers. Documented on the methods; P2-004
   formats for display.
6. **Payload hardening (security review, TCK-P2-001):** provider payloads
   are validated fail-closed beyond shape. The USD rate must be finite
   (Python's `json` parses the bare `NaN`/`Infinity` tokens — both
   rejected) and within a documented plausible magnitude
   (`0 < usd_per_btc <= 1e9`); huge JSON integers convert to float
   defensively so `OverflowError` becomes a value-free `ChainError`. The
   `ttl_s` of both cache constructors (price, fees) must be positive *and
   finite* — a NaN/inf TTL would silently disable cache expiry. Relatedly,
   the fee estimator rejects a recommended fee of `0` sat/vB for any target
   and a `minimumFee` of `0`: a zero estimate is a broken payload, not a
   cheap one — the tx engine floors fees via min-relay separately
   (ADR-0012 §3).

## Alternatives considered

- **Third-party price API (CoinGecko etc.) — rejected:** adds a second
  operator who also learns the querying IP, widening the §9 privacy surface;
  the shared-host option is strictly smaller and sufficient for an MVP fiat
  figure.
- **Opt-in only — rejected:** fiat display is a headline UX convenience; an
  opt-in default would bury it. The shared-host privacy posture makes
  default-on acceptable, and the `0` switch satisfies privacy-maximalists.
- **Serving stale forever — rejected:** unbounded staleness is misleading
  for a price; the `MAX_STALE_AGE_S` (24 h) staleness cap on what `fresh()`
  will degrade to, plus the sats-only fallback, keeps the number honest.

## Consequences

- A public operator sees price-query traffic from the app's IP; this is the
  same operator already handling chain queries and is covered by the §9 / R7
  disclosure — an honest note, not a new leak.
- Availability depends on the shared backend's uptime; the cache + degrade
  ladder is the mitigation (R10, R11).
- Phase 4 (self-hosted backend) can keep the same `PriceOracle` interface
  and adjust source/TTL via config; `tools/lint_network.py` still restricts
  all network I/O to `chain/`.
- **Revisit triggers:** a second price source becomes worthwhile if (a) the
  shared host is unavailable for extended periods and the stale window runs
  out, (b) a Phase 4 backend does not expose `/v1/prices`, or (c) product
  wants multi-currency/multi-source aggregation. Any change re-runs the OQ4
  decision rather than silently extending this one.

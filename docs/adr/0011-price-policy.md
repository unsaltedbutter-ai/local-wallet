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

## Amendment (2026-09-08, TCK-FEE-001): floor-follower fee bidding

User feature request (2026-09-07 live run): a `fast` send bid 2 sat/vB
while the last 5 blocks confirmed down to ~0.34 sat/vB and the projected
next block bottomed at ~0.3–0.57 sat/vB — a systematic overpay from
`fastestFee` (Core's block-1 estimate). The observed case must bid
**no more than 1 sat/vB**.

**Decision — the fee estimator (`chain/fees.py`) is now two-layer:**

1. **Floor-follower (primary).** The bid is the minimum integer sat/vB at
   or above the observed floor for the target — `ceil(max(terms))`, never
   more (no padding above the ceil; that ceil IS the sanity bound). All
   terms come from endpoint data; no fee constants are hardcoded:

   - `FAST = ceil(max(minimumFee, B₀, R₅))`
   - `MEDIUM = ceil(max(B₂, R₅))`
   - `SLOW   = ceil(max(B₆, R₅))`

   where `Bᵢ` is the `feeRange[0]` bottom (lowest fee quantile) of
   `mempool-blocks[i]` from `GET /v1/fees/mempool-blocks` (the projected
   next blocks; shallower projections clamp to the deepest available
   block), and `R₅` is the **recent-blocks floor** — the lowest
   `extras.feeRange[0]` bottom across the last 5 confirmed blocks from
   `GET /v1/blocks/{tip}` (tip via the existing tip endpoint). `R₅` is why
   a transiently-empty mempool can't produce a bid that misses when blocks
   are actually full (the user's live case: 0.34 > 0.3, so the blocks —
   not the projection — set the floor). `minimumFee` ("min fee to get into
   the next block") floors `FAST` only; `MEDIUM`/`SLOW` target later
   blocks and may bid below it. The parser **enforces** projected bottoms
   to be non-increasing with depth (a payload that breaks the order fails
   closed to the fallback) and all three terms share `R₅`, so
   `FAST ≥ MEDIUM ≥ SLOW` is a code invariant over accepted payloads — not
   a data-source assumption (security-review follow-up, 2026-09-08). The
   rule is a floor-follower, not a cap: a congested
   next block (high `B₀`) lifts `FAST` with it. The user's observed case
   yields exactly `ceil(max(1, 0.3, 0.34)) = 1` sat/vB.
2. **Recommended fallback.** Any failure on the new surfaces — transport,
   HTTP, or strict shape validation (non-list, empty `feeRange`, missing
   `extras`, fewer than 5 confirmed blocks or no projected blocks,
   depth-increasing projected bottoms, bool/string/negative/**zero**/
   NaN/Infinity bottoms, per the §6 payload-hardening style) — degrades to
   the original §1 mapping (`fastestFee`/`halfHourFee`/`hourFee`). The
   source is recorded on every
   `FeeEstimate` (`FeeSource.FLOOR_FOLLOWER` vs `FeeSource.RECOMMENDED`)
   so the narration can stay honest; the fee line keeps its verbatim rate
   quote and "estimate only, not a guarantee" hedge (the ETA wording,
   ADR-0020, unchanged). A malformed/failed `/v1/fees/recommended` itself
   still raises `ChainError` (fail closed — there is no lower layer), so
   `create_tx` still surfaces `chain_unavailable` exactly as before.

**Rate limits (R11):** the whole set is one combined refresh under the
existing `fee_cache_ttl_s` (4 small GETs: recommended, mempool-blocks,
tip, recent blocks); a degraded refresh is cached too (no endpoint
hammering). Fees stay integer sats/vB end to end; the tx engine's
min-relay floor (ADR-0012 §3) still applies underneath. Protocol values
(`create_tx.fee_target`) are unchanged. Endpoint shapes pinned against
live mempool.space on 2026-09-08: `mempool-blocks` carries 7-quantile
ascending `feeRange` per projected block; `/v1/blocks` (no height)
carries NO fee stats — the block fee data lives under `extras.feeRange`
on `/v1/blocks/{height}`.

## Amendment (2026-09-11, TCK-FIAT-002): multi-currency display

User use-case wave (2026-09-10): balances in currencies beyond USD. The
same `/v1/prices` payload already carries `USD, EUR, GBP, CAD, CHF, AUD,
JPY` (confirmed live 2026-09-10), so the multi-currency display introduces
no new endpoint, host, or privacy surface — §1 is unchanged apart from
which field of the ONE response is extracted.

**Decision:**

1. **Display currency is a setting, never a model choice.** A new
   `display_currency` setting resolves through the one documented ladder
   (env `LOCALWALLET_DISPLAY_CURRENCY` > config file > stored settings key
   > shipped default `usd`) against a CLOSED enum — exactly the seven
   codes the endpoint serves — parsed case-insensitively, canonical
   lowercase. An unknown code on any rung is a fail-closed, value-free
   **startup refusal** (ADR-0009: a corrupt setting never silently flips
   policy); the settings-surface write path validates the same enum at
   write. USD remains the default and every USD wire shape (the
   TCK-FIAT-001 `usd_total_cents`/`btc_usd`/`usd_cents` result keys and
   the `$x,xxx.xx` narration) is byte-compatible when unset.
2. **The model never authors a currency code.** The intent protocol is
   unchanged (no new intent, no new params): fiat-phrased balance asks in
   ANY currency wording ("balance in euros / GBP / pounds …") still emit
   `get_balance`, and the conversion rides the user's setting. Narration
   quotes the currency tag verbatim from the tool result
   (`fiat_currency`), exactly like addresses and amounts. For the same
   reason `create_tx.amount_usd` (historical field name, unchanged
   grammar) is interpreted in the DISPLAY currency: the app's setting is
   the only currency selector, and the confirmation card labels the fiat
   figure with the actual code, so a quote never silently reads as
   dollars.
3. **Oracle/caching:** `PriceOracle` fetches the configured currency's
   field of the same payload; a cache entry is tagged with its currency
   and is only ever served for that currency — switching display currency
   refetches, and during an outage after a switch the ladder degrades to
   sats-only rather than serving a wrong-currency number. The stored rung
   is re-read per fetch decision, so a settings change is live on the
   next quote with no restart (`requires_restart: false`, honestly).
4. **Units:** unchanged §5-style money math — the endpoint value is whole
   MAJOR units per BTC (dollars, euros, yen — not minor units); minor-unit
   integers are computed on conversion, with the currency's minor scale
   (100 for the two-decimal codes, 1 for JPY — zero decimals come from the
   code's minor scale, never a hardcoded cents constant). Plausibility
   bound §6 now applies per-currency (`0 < per_btc <= 1e9` in major
   units — headroom for JPY's ~1e8 magnitude).

**Revisit triggers:** other currencies (e.g. SEK, INR, BTC-as-display) are
only worth adding if the endpoint starts serving them AND users ask —
each new code widens the enum, the eval fixtures, and the formatting
matrix; an arbitrary ISO-4217 passthrough is rejected (a fetch of a field
the provider may not carry, and a label the model could hallucinate).

## Amendment (2026-09-12, TCK-DESCOPE-M3A): price/fee source decoupled from the wallet backend

The wallet no longer rides an Esplora API (ADR-0003/0018 amendments): chain
data comes only from Electrum or Bitcoin Core. §1's rationale — "same host
as the chain queries, so no *additional* third party" — **no longer holds
and the ADR now says so honestly**: fees and prices ALWAYS come from
mempool.space as an INDEPENDENT third-party public source (the standalone
`chain/publicinfo.py` fetcher pinned to the repurposed `esplora_base_url`),
regardless of which wallet backend is configured.

- **Privacy posture restated:** the fee/price payloads are public
  aggregations and carry **no wallet addresses** — the disclosure to this
  third party is the app's IP and request timing only. That is a smaller
  leak than any chain query and needs no consent gate (a price/fee fetch
  cannot name a wallet); `LOCALWALLET_PRICE_ENABLED`'s opt-out stays the
  user's switch over it.
- **Behavior unchanged:** the floor-follower (2026-09-08 amendment) and
  the §4 degrade ladder run byte-equivalently whenever the public source
  is reachable, and their existing fail-closed shapes (recommended
  fallback / stale-with-age / sats-only) apply when it is not — including
  on an Electrum/bitcoind backend, where the old per-backend
  `estimate_fee`/`supports_price=False` capability seam simply no longer
  decides anything (it stays on the adapters, unused by the estimator).
- §1's "extracting the USD field of `/v1/prices`" and the 2026-09-11
  multi-currency amendment are untouched: same endpoint, same closed
  currency enum, same TTLs — only the injection point moved from the
  wallet client to the public-info fetcher.

## Amendment (2026-09-12, TCK-FEE-003): target-follower fee policy v2 + fractional rates

User live run (2026-09-12): the FEE-001 floor-follower still bid **2 sat/vB**
(next block's floor 1.0557, `ceil` to whole sats) — "still too high". The
user's binding spec refines the mapping to a fractional ladder sourced from
ONE endpoint: `GET /v1/fees/mempool-blocks` (`feeRange[0]` = that projected
block's floor; `B₀` first block, `B₁` second).

**Decision — fee policy v2 (replaces the FEE-001 FAST/MEDIUM/SLOW mapping):**

- `MEDIUM` (the target) = `B₀ × 1.15` rounded HALF-EVEN to 2 decimals,
  clamped UP (ceil-2dp of `B₀`) whenever the rounding would land below `B₀`
  — a bid never undercuts its own floor. User pin:
  `1.05567928730512 × 1.15 = 1.21403… → 1.21` (normal 2-dp rounding — the
  user rejected ceil-to-1.22 AND the old ceil-to-2).
- `FAST` (faster) = `2 × MEDIUM` (the doubling acts on the ROUNDED target:
  2 × 1.21 = 2.42 exactly), still additionally floored by the recommended
  payload's `minimumFee` ("min fee to get into the next block" bounds the
  next-block rung — FEE-001's protection, kept).
- `SLOW` (slower) = `B₁` ceil-rounded to 2 dp, no markup; with only ONE
  projected block, `B₀` itself (the floor).
- Fail-closed unchanged: ANY mempool-blocks transport/HTTP/shape failure
  degrades to the §amendment-1 recommended mapping; the parser's
  non-increasing-bottoms rule (FEE-001's security-review MEDIUM) is KEPT —
  it now underwrites the code invariant `FAST ≥ MEDIUM ≥ SLOW` (`B₁ ≤ B₀`),
  and both original counterexamples stay pinned.
- Dropped: the recent-blocks floor `R₅` and its tip + `/v1/blocks/{tip}`
  GETs (the spec names one source; the ×1.15 markup covers R₅'s
  anti-underbid role — FEE-001's motivating case bids 0.34 ≥ the 0.34 the
  blocks confirmed at). Combined refresh is now two small GETs.
- FEE-001's binding observed case survives: FAST = `max(2 × 0.34, 1)` =
  **1 sat/vB**, still "no more than 1".

**Decision — fractional rates end to end (USER DIRECTION 2026-09-12: "we
need to support less than 1 sat/vB and fractional values like 1.5"):**

Rates travel as **integer centisat/vB** (`121` = 1.21 sat/vB) —
`FeeEstimate.rate_centisat_vb`, `select_coins(fee_rate_centisat_vb,
1..1_000_000)`, `PendingTx.fee_rate_centisat_vb`. Transaction fees become
`fee_sats_for(vsize, c) = ceil(vsize × c / 100)` — integer-exact (the §
"integers only for sats/fees/vsize" invariant HOLDS; no float or Decimal
ever enters `tx/`), byte-identical to the old `vsize × rate` at whole
sat/vB (every pre-existing fee value is unchanged), and the CEIL keeps a
fractional rate from being under-paid by its own rounding. The card quotes
a `fee_rate_display` string formatted from the integer by the chain-owned
`format_sat_vb` (verbatim-tool-output rule preserved; whole sats render as
`"2"`, fractional as `"1.21"`, `"0.55"`). Estimator math is `Decimal`
inside `chain/fees.py` ONLY (shortest-repr of the JSON floats, half-even
target, ceil floors) and leaves as integer centisat. The protocol's
explicit `create_tx.fee_rate_sat_vb` stays whole-sat int 1..10_000 (GBNF
unchanged; fractional explicit entry = follow-up FEE-004) and is scaled
×100 at the handler edge. The PSBT builder's min-relay STANDARDNESS gate
(Core default 1 sat/vB, computed from vsize, ADR-0012 §3) is UNCHANGED —
sub-1 BIDS are legal data through estimation/selection/display, and a tx
whose folded fee would dip under the relay floor still fails closed at
build time before any signature; relaxing that floor is a relay-policy
decision for the operator backend, not a fee-math side effect.

Pins: both user example payloads from the 2026-09-12 spec (1.0557…→1.21,
1.0→1.0; 2×→2.42) run byte-exact in `tests/test_chain_fees.py`; the
half-even tie (0.3 × 1.15 = 0.345 → 0.34), the sub-floor clamp (tiny
bottoms), the single-block slow case, `minimumFee` fast-only flooring, the
2-GET refresh, and an end-to-end card line quoting "1.21 sat/vB" with
`ceil(141 × 1.21) = 171` sats in `tests/test_e2e_skeleton.py`; engine ceil
exactness + sub-1 selection in `tests/test_tx_fractional_fees.py`.
ETA narration, the estimate-only hedge, the explicit-rate path and the
top-rung ceiling-ask copy are unchanged (numbers only).

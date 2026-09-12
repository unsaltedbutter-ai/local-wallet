# Fractional fee plan (TCK-FEE-003 wave, 2026-09-12)

USER DIRECTION (2026-09-12): the app must support fee rates **below 1 sat/vB**
and **fractional rates like 1.5 sat/vB** — end to end: estimation,
transaction generation, and summarization (the card). The current engine
takes an integer sat/vB and rejects any float, so the estimator's v2 bids
(1.21 / 2.42 / 1.0 from the user's live payloads) could not be spent without
this plumbing.

## The contract (one decision, everything else follows)

Rates travel as **integer centisat/vB** (`121` = 1.21 sat/vB). Sats, fees,
and vsizes stay integer everywhere (PROJECT.md money-math invariant holds —
no float and no Decimal ever enters the tx engine):

- `fee_sats(vsize, c) = ceil(vsize × c / 100)` — integer-exact
  (`-(-vsize * c // 100)`); byte-identical to today's `vsize × rate` for
  every whole-sat rate (every current test value).
- Display is a pure format of the integer: `format_sat_vb(121) == "1.21"`,
  `format_sat_vb(200) == "2"`, `format_sat_vb(55) == "0.55"`. The card
  quotes the formatted rate verbatim from the tool result (new
  `fee_rate_display` key); the machine key is `fee_rate_centisat_vb`.
- Engine bounds: `1..1_000_000` centisat/vB (0.01..10_000 sat/vB) — the old
  `1..10_000 sat/vB` ceiling preserved in the new unit; the min-relay
  absolute-fee floor (ADR-0012 §3, computed from script size) still applies
  underneath, unchanged.

## Fee policy v2 (chain/fees.py — the user spec, refines the FEE-001 floor-follower)

Source: `GET /v1/fees/mempool-blocks` (projected blocks; `feeRange[0]` = the
lowest quantile = that block's floor). `B₀` = first block, `B₁` = second.

- **TARGET (MEDIUM)** = round-to-2-decimals( HALF-EVEN ) of `B₀ × 1.15`,
  never below `B₀` itself (if the rounding result `< B₀`, use ceil-2dp of
  `B₀`). User pin: `1.05567928730512 × 1.15 = 1.21403… → 1.21` (normal
  rounding, NOT ceil — ceil would give 1.22 and the user said 1.21).
- **FASTER (FAST)** = `2 × TARGET` exactly (2 × 1.21 = 2.42 — the doubling
  is of the ROUNDED target), additionally floored by the recommended
  payload's `minimumFee` (FEE-001's protection kept where it still applies:
  "min fee to get into the next block" bounds the next-block rung).
- **SLOWER (SLOW)** = `B₁` ceil-rounded to 2 decimals (no markup; the ceil
  keeps it from under-bidding its own floor). With only ONE projected block:
  `B₀` itself (ceil-2dp).

**Rounding rule (pinned):** target = Decimal half-even to 2dp, clamped up to
never sit below `feeRange[0]`; the FLOOR terms (slow, target-clamp)
round UP (ceil-2dp) so no displayed or bid rate ever undercuts its source
floor; `×1.15` and `×2` are exact Decimal operations on the shortest-repr of
the JSON float. 1.21403 → 1.21 ✓; 1.0 → 1.0 ✓; 2.42 ✓.

## FEE-001 protections — kept / dropped

- **Non-monotonic parser protection: KEPT.** Projected bottoms must be
  non-increasing with depth or the payload fails closed to the fallback —
  now it protects the code invariant `FAST ≥ TARGET ≥ SLOW` (which depends
  on `B₁ ≤ B₀`) exactly as it protected the old ladder. Both FEE-001
  security-review counterexamples stay pinned.
- **minimumFee floor: KEPT**, on the FAST rung only (FEE-001 semantics).
- **Recent-blocks floor (R₅) + tip/recent fetches: DROPPED.** The user spec
  names ONE source (mempool-blocks); R₅'s anti-underbid role is taken over
  by the ×1.15 markup (the FEE-001 motivating case: B₀=0.3, blocks
  confirmed at 0.34 → target 0.345 ≥ 0.34 ✓). Dropping it removes two of
  the four refresh GETs (cache TTL still bounds load).
- **Fail-closed fallback: KEPT** — any mempool-blocks transport/HTTP/shape
  failure degrades to the recommended mapping (now ×100 centisat; values
  identical), source honest (`FeeSource`), zero-fee payloads still rejected.
- Explicit `fee_rate_sat_vb` path (UX-004/FEE-002), ETA narration and the
  estimate-only hedge: UNCHANGED (the int sats/vB envelope value is
  multiplied by 100 at the handler edge).

## Ticket breakdown (this wave = FEE-003; the unit contract forces one commit)

1. **TCK-FEE-003 (chain, this ticket):** `fees.py` policy v2 +
   `FeeEstimate.rate_centisat_vb` + `format_sat_vb`; ADR-0011 amendment;
   user-payload pins in `tests/test_chain_fees.py`.
2. **TCK-TX-FRAC-001 (tx, lands with it — the suite cannot be green
   between 1 and 3 without dead shims):** `select_coins` / `_Selector` /
   `_Policy` / `SelectionResult` take `fee_rate_centisat_vb`
   (1..1_000_000), `fee_sats = ceil(vsize×c/100)`, `min_useful_value` and
   the fold-passage use the same helper, step-5 compare is `c ≤
   consolidate_below_sat_vb×100`; `PendingTx.fee_rate_centisat_vb`.
3. **TCK-APP-FRAC-001 (app wiring + summarization):** both handlers feed
   centisat (explicit int ×100 at the edge), self-transfer `ceil(vsize×c/100)`
   math, result dicts carry `fee_rate_centisat_vb` + `fee_rate_display`,
   card lines quote the display verbatim, requote direction compares
   centisat. Narration COPY strings unchanged except the number.
4. Tests across the touched surface (mechanical ×100 in existing pins, plus
   new fractional pins: 1.21-card, sub-1 slow, ceil-fee exactness).

Deferred (own ticket if requested): **FEE-004** — fractional EXPLICIT rates
in chat ("actually make it 1.5 sat/vB" / sub-1 entry): GBNF + pydantic +
evals change; the protocol currently takes integer sats/vB 1..10000 and the
ceiling-ask copy is value-free, so nothing blocks; this wave leaves that
knob int-only.

## Security note

Money path: policy rounding is Decimal-exact in `chain/`, integer-exact in
`tx/` (no float crosses into sat math); all provider payloads stay
fail-closed parsed; no fee/amount values in logs (unchanged). Security
review follows per ticket discipline.

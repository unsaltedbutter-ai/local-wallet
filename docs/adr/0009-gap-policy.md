# ADR-0009: Gap policy — default 20, per-branch termination, rescan is always full

- **Status:** Accepted
- **Date:** 2026-08-31
- **Decides:** PROJECT.md OQ7 ("Gap-limit policy defaults + rescan UX
  (auto-widen? warn?)") and the R3 mitigation ("detect used address
  beyond derived window cases").
- **Scope:** `src/localwallet/wallet/scan.py` scan/rescan semantics,
  `settings` key `gap_limit`, `sync_state` warning payload, Phase 1-004
  UI surfacing.

## Context

A gap-limited scanner stops after N consecutive unused addresses; usage
beyond that window (addresses imported into Electrum/Sparrow, or a
recipient reusing change) is invisible to a normal scan. The wallet must
be honest about that limitation, make the window configurable, and
provide a repair path that never silently guesses.

## Decision

1. **Default gap: 20** (BIP44 convention), one setting for both
   branches: `settings.gap_limit` (decimal string, validated to
   1..1000; malformed or out-of-range values fail closed with a
   value-free error — a corrupt setting never silently changes scan
   depth). A `gap_limit` argument overrides the setting per call.
2. **Per-branch, consecutive-unused termination.** Branches (0 = receive,
   1 = change) are walked independently in ascending index order from 0;
   the walk stops after `gap_limit` consecutive unused addresses, so the
   window is `[0, last_used_index + gap_limit]` (usage at index 3 → stop
   at 23 with the default). Each window address costs exactly one txs
   call and one utxo call; everything is strictly sequential (branch 0
   before branch 1) — no concurrency in v1.
3. **Normal scan trusts the cache; rescan never does.**
   `scan_wallet` reuses cached address mappings and never lowers the
   cached derivation cursor. `rescan_wallet` is the repair path
   (Phase 1 AC "rescan fixes a simulated stale cache"): it re-derives
   every window mapping from the key, recomputes
   `max_used_index`/`next_index` from chain truth
   (`next_index = max_used_index + 1`), and replaces the UTXO snapshot
   wholesale. There are **no partial-widening heuristics**: if the
   window must grow, the user widens `gap_limit` (or the default) and
   rescans — full window, deterministic result.
4. **Beyond-window usage is detected and persisted, never guessed.**
   When a scan/rescan finds `max_used_index` beyond the previously
   recorded scan window, `sync_state["out_of_window_detected"]` is
   written as JSON
   `{"detected_at": <iso>, "branches": {"<branch>": {"max_used_index":
   n, "previous_window_end": m}}}` for the P1-004 UI to surface. The key
   is rewritten on every completed scan: an empty payload explicitly
   clears a stale warning once the window covers observed usage. A
   first scan never warns (there is no previous window to exceed).
5. **Allocation flags survive rescans.** Address rows marked
   `allocated` are preserved by address string through a rescan (status
   precedence: used > allocated > unused), so the repair path never
   silently re-issues an address the user already received.

## Rationale

- The BIP44 default of 20 matches Electrum/mempool.space cross-check
  expectations (Phase 1 AC) and user mental models from other wallets.
- Consecutive-unused termination per branch matches how BIP44 wallets
  actually allocate (receive and change gaps are independent).
- Rescan-as-rebuild is the only semantics that is obviously correct
  under cache corruption; incremental widening heuristics trade
  correctness for a few saved requests on a path that is already
  rate-limit-friendly (bounded by `used + gap` per branch, sequential).
- Persisting the warning (vs. raising or narrating once) makes the R3
  condition durable and visible; clearing it on the next clean scan
  keeps it truthful.

## Consequences

- Usage deeper than the configured gap is invisible to normal scans by
  design; the UI must present the `out_of_window_detected` warning and
  the rescan affordance (P1-004).
- `rescan_wallet` rewrites in-window rows; rows beyond the current
  window are left as cached history. They cannot hold live UTXOs (any
  fundable address reappears in the walk — its funding transaction
  marks it used and extends the window), and the wallet-wide UTXO
  snapshot is replaced only from the freshly scanned window.
- A failed scan persists nothing. The chain phase runs to completion
  before any store mutation (a transport or chain-payload failure leaves
  the store untouched), and the persist phase is a single atomic store
  transaction (`Store.persist_scan_result`): address statuses, derivation
  cursors, the UTXO snapshot, history, sync cursors and the
  `out_of_window_detected` write all commit together or roll back
  entirely. A crash or store failure mid-persist therefore can never
  desync address statuses from the derivation cursor or sync state —
  the prior state stays exactly intact and the scan can be retried
  cleanly.

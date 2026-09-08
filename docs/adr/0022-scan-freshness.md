# ADR-0022: Non-blocking startup scan — dedicated chain worker, engine-only persistence, and cache freshness

- **Status:** Accepted (revised per the web-architecture consult 2026-09-07;
  drafted TOGETHER with ADR-0024 so the threading models agree; implementation
  ticket TCK-SCAN-002/003).
- **Date:** 2026-09-07
- **Decides:** How the startup scan runs **non-blocking** in BOTH the CLI and
  the web worlds, and how cache-served answers during the first scan are honestly
  freshness-flagged. The original cooperative-chunked design is rejected in
  favor of a **dedicated chain worker** with no store access that returns
  immutable record sets; **only the engine thread persists** via
  `Store.persist_scan_result` (the single atomic transaction). This model also
  covers the watch polls, retiring ADR-0019/P5-001's "full scan per poll" cost
  note. Relates to ADR-0019 (watch threading), ADR-0024 (web threading, drafted
  alongside), ADR-0018 (backend), ADR-0023 (onboarding's load narration depends
  on this), ADR-0010 (single wallet), ADR-0021 (mainnet-only).
- **Scope:** `src/localwallet/app.py` (engine wiring, `create_tx` pre-first-scan
  gate), `src/localwallet/wallet/scan.py` (derive+fetch vs persist split),
  `src/localwallet/store/` (`persist_scan_result` — already the single atomic
  transaction), `tests/`, and evals (stale-flag narration fixtures). The web half
  lands in TCK-WEB-005.

## Context

The startup scan probes every window address sequentially against the chain
backend. Against a slow backend or a large history it can take minutes (TCK-UX-001
added a progress notice and dotted line precisely because the prompt is not live
until the scan finishes). The Phase 5 watch poll added to the cost: ADR-0019's
probe calls `scan_wallet(store, client, wallet)` — a **full scan per poll** — so a
60-second watch against a slow backend blocks the UI for the whole poll
(referenced in the P5-001 cost note).

`run()` in `app.py` calls `_startup_scan(...)` before the REPL starts (and before
"Type a message" prints, line 2453) — the scan is blocking by construction today.
`scan_wallet`/`rescan_wallet` in `wallet/scan.py` are **store-fused**: they take
`store`, do the chain fetch, and call `store.persist_scan_result(...)` (scan.py:428)
inside `_run_scan`. The store holds **one** connection per `Store` instance
(`sqlite3.connect` in `store/db.py:162`, `isolation_level=None`, engine-thread
owned, `check_same_thread=True` by default), so no other thread may write to it.

Two designs were weighed. **Cooperative chunking** (resume the scan across REPL
ticks, preserving ADR-0019's no-threads shape) was the original plan. The consult
(F1) rejected it for the web world on a concrete, measured failure: with a real
engine-thread UI, a 60-second watch poll — or a 41-call scan (the TCK-SCAN-001
zero-history address count, down from 81) — freezes the engine thread for the
whole poll because the chunk still runs on the engine thread. Cooperative
chunking only hides the freeze between *user turns*; it cannot hide a poll that
must complete, and it couples scan resume state into the REPL loop. The dedicated
worker removes chain I/O from the engine thread entirely.

## Decision

1. **The startup scan is non-blocking in BOTH the CLI and web worlds.** The scan
   runs concurrently with the app being usable; the CLI prompt becomes live before
   the scan finishes (in the CLI, the between-turns drain can still observe scan
   progress), and in the web world the UI stays responsive throughout.

2. **A dedicated chain worker does all scan/watch chain I/O and has NO store
   access.** A single worker thread owns the chain `EsploraClient` for
   scans/polls. It **derives + fetches** and returns **immutable record sets**
   (address rows, derivation states, UTXO snapshot, tx rows, sync-state updates)
   as data — it never touches sqlite. This is required because the store
   connection is engine-thread-owned (`check_same_thread=True`); giving the
   worker its own connection was explicitly rejected (ADR-0019 deferred it, and
   this ADR does not resurrect it — see Alternatives).

3. **ONLY the engine thread persists.** The engine consumes the worker's
   immutable record sets and calls `Store.persist_scan_result(...)` — the single
   atomic transaction in `store/db.py:597–633` that commits addresses,
   derivation, the UTXO snapshot (full replace), transactions, and sync state
   together or not at all. Persistence is therefore single-threaded by
   construction, not by discipline: cross-thread sqlite writes are impossible.

4. **The same worker covers the startup scan AND the watch polls.** Both are
   chain I/O; both post immutable record sets to the engine, which persists.
   This retires the P5-001 "full scan per poll" cost note: the poll no longer
   blocks the engine thread (the worker does it), and the engine thread only
   does the cheap persist of whatever the poll produced. In the CLI world, where
   no background thread is needed, the ADR-0019 between-turns tick may remain for
   polls (ADR-0024 decision 9 decides the exact split); the startup scan is
   non-blocking in both worlds.

5. **Freshness flag on cache-served answers during the first scan.** While the
   first scan has not completed, an answer served from the (possibly empty or
   partial) cache carries a **deterministic, tool-owned freshness flag**
   (`stale=true` in the handler result / FACTS). The flag is:

   - **tool-owned** — produced by code from the scan's completion state, never by
     the model;
   - **narration-only** — it annotates an answer; it never changes the answer's
     values (balances are still quoted verbatim from the cache);
   - **model never authors freshness claims** — the model narrates freshness only
     from the tool's flag; it never invents "up to date" or "stale". (This ties to
     ADR-0023's step-3 copy "the most up to date information I have" — honest only
     because the flag is tool-owned.)

   The flag clears when the first scan completes (the engine records completion
   in the store's sync state).

6. **Per-intent policy on partial data before the first scan completes:**

   - `get_balance`, `get_history`, `get_utxos` — **MAY answer**, cache-served and
     stale-flagged (decision 5).
   - `new_address` — **MAY answer**: allocation is pure store bookkeeping +
     deterministic derivation (app.py `_make_new_address_handler`) and is
     chain-free, so it is safe before the first scan.
   - `create_tx` — **MUST refuse until the first scan completes.** Sending against
     a partial/empty cache could select coins or present a balance that the scan
     would later revise. It returns a friendly refusal (value-free) explaining
     the wallet is still loading, mirroring the ADR-0013 pending-guard style —
     not a guess, not a silent partial send. This is the load-bearing AC: chat is
     unblocked, but **value movement stays gated** until the scan finishes
     (ADR-0023 step 3 explicitly forbids implying sending is possible mid-load).

   The `confirm_tx`/`sign_tx`/`broadcast_tx` lifecycle is unaffected: it is
   gated by the ADR-0013 state machine regardless.

## Why cooperative-chunking lost (consult F1)

The original design resumed the scan across REPL ticks to preserve ADR-0019's
no-threads shape. It was rejected on the measured failure the consult named: any
chain I/O still running on the engine thread freezes the UI — a 60-second watch
poll or a 41-call scan blocks the engine thread for its whole duration. Chunking
only hides the freeze between *user turns* and cannot hide a poll that must
complete; it also threads scan-resume state through the REPL loop (a new
coupling and a new failure mode). The dedicated worker removes chain I/O from the
engine thread entirely and, because it returns immutable sets that only the
engine persists, does not resurrect the rejected "worker with its own store
connection" either. This is the model ADR-0024 builds on.

## TCK-SCAN-003 file-plan draft

The implementation splits by surface. **Files:** `src/localwallet/app.py` (engine
wiring: worker start/teardown, engine-thread persist, freshness state,
`create_tx` pre-first-scan gate), `src/localwallet/wallet/scan.py` (split
`scan_wallet`/`rescan_wallet` into a derive+fetch phase returning immutable record
sets and a persist phase the engine calls; the CLI `--rescan` path rides the same
split), `src/localwallet/store/` (no schema change; `persist_scan_result` reused
as-is), `tests/` (worker no-store-access pin, engine-only-persist pin, freshness
flag matrix, `create_tx`-refuses-pre-first-scan, CLI prompt-live-before-scan,
dots-between-turns), and evals (stale-flag narration fixtures: the model must not
author a freshness claim).

**Halves:** the **CLI half** ships with TCK-SCAN-003 (app.py + scan.py split +
tests + evals; REPL prompt live <1 s; stale-flagged answers; `create_tx` blocked
pre-first-scan with the friendly line; dots between turns). The **web half**
ships with **TCK-WEB-005**, which absorbs SCAN-003's web-side work: the same
worker model surfaced in the web UI with freshness flags and the same
`create_tx` refusal, reusing the CLI half's engine plumbing (per ADR-0024
sequencing, SCAN-002/003 is absorbed into WEB-005's scan half).

## Alternatives considered

- **Cooperative chunked resume on the engine thread** (the original plan).
  Rejected per consult F1 (above): the engine-thread freeze on a 60s poll /
  41-call scan is a real, measured failure; chunking hides it only between turns
  and couples scan state into the REPL loop.
- **Chain worker with its own store connection.** Rejected: it reintroduces
  cross-thread sqlite writes and the ADR-0019 deferral. The worker is
  store-free; the engine persists (decision 2/3), keeping persistence
  single-threaded by construction.
- **Silently serve partial data with no freshness signal.** Rejected: a balance
  or history served during the first scan that looks final would mislead, and
  would let the model over-claim freshness. The tool-owned flag is the honest
  minimum (decision 5).
- **Let `create_tx` run pre-first-scan with a warning.** Rejected (decision 6):
  value movement against a partial/empty cache is exactly the mistake the
  closed-intent, careful-confirm design exists to prevent; it refuses until the
  scan completes, and chat remains unblocked in the meantime.

## Consequences

- **Real refactor cost:** `scan_wallet`/`rescan_wallet` are today store-fused
  (`_run_scan` calls `store.persist_scan_result`, scan.py:428). Splitting them
  into a derive+fetch phase (immutable record sets, no store) and a persist phase
  (engine calls `persist_scan_result`) is a real change to `wallet/scan.py` and
  its call sites in `app.py`, including the `--rescan` path and the watch probe.
  `Store.check_same_thread` and the single-connection invariant are preserved.
- **Engine owns persistence** via the one atomic `persist_scan_result`
  (db.py:597–633); the worker holds no `Store` (test-pinned no-store-access).
- **Freshness flag** is deterministic and tool-owned; model never authors
  freshness; balances still quoted verbatim. Stale-flag narration fixtures land
  in evals.
- **`create_tx` refuses pre-first-scan** with a friendly, value-free line; chat
  and `get_balance`/`get_history`/`get_utxos`/`new_address` are unblocked
  (stale-flagged where applicable).
- **CLI prompt live <1 s** after launch (scan runs concurrently; dots continue
  between turns via the parameterized progress callback — the stdout-dots
  parameterization ADR-0024 decision 12 records). **Web half** lands in
  TCK-WEB-005.
- **Watch polls covered:** the P5-001 "full scan per poll" cost note is retired;
  polls run on the worker and only the engine persists what they produce.
- **ADR-0023 dependency met:** its step-3 non-blocking copy becomes honest once
  this lands; until then the interim blocking variant stays mandatory.
- **Value-free / no-secrets rules** are restated for the scan/freshness surface:
  no addresses, amounts, or counts in the refusal/freshness narration beyond the
  tool-owned flag; nothing logged.

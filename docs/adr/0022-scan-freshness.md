# ADR-0022: Non-blocking startup scan — dedicated chain worker, engine-only persistence, and cache freshness

- **Status:** Accepted (revised per the web-architecture consult 2026-09-07;
  reconciliation recorded 2026-09-08; drafted TOGETHER with ADR-0024 so the
  threading models agree; implementation ticket TCK-SCAN-002/003; amended
  2026-09-09 by ADR-0023 amendment 2 / TCK-ONB-006 — the first-run scan
  exception in §Amendment 1; amended 2026-09-10 by TCK-UX-011 — the
  get_balance engine-thread stand-down in §Amendment 2).
- **Date:** 2026-09-07 (reconciled 2026-09-08)
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

1. **The startup scan is non-blocking in BOTH the CLI and web worlds, superseding
   the blocking startup-scan behavior of ADR-0019's startup/load path.**
   (Amendment 1 adds the first-run exception: the scan stays non-blocking
   once it may run, but on an unresolved backend it may not run at all.) The scan
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
   partial) cache carries a **deterministic, tool-owned freshness flag** — the
   handler result / FACTS key `freshness`, with the closed values `'fresh'` (the
   first scan has completed) or `'stale'` (the first scan is still running). The
   flag is:

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

7. **ADR-0019 §2 no-poller-thread test pin retired here as PLANNED work.** The
   `tests/test_watch_incoming.py` pin that asserted a single-threaded design
   (`threading.enumerate() == 1` — "tick is synchronous, no sleep / no poller
   thread") was already retired for the threaded world by ADR-0019's 2026-09-07
   amendment (landed from ADR-0024 decision 9). This ADR records the retirement
   as **PLANNED work executed by TCK-SCAN-003** and reconciles, not contradicts,
   that amendment: the pin is re-scoped to the thread **delta** around `tick()`
   (the watcher itself must still spawn nothing — it only reads `poll_due`/`tick`
   from whatever thread drives it), engine-thread mode is covered by the pump
   harness in `tests/test_engine_pump.py`, and the dedicated chain worker holds
   no store access (it is not ADR-0019's deferred "thread with its own store
   connection"). CLI-world polls keep the between-turns tick where no background
   thread is needed (ADR-0024 decision 9 / decision 4 above decide the split).

## Amendment 1 (2026-09-09, TCK-ONB-006): the first-run scan exception

A user report dated 2026-09-09 fixed the privacy hole this ADR's eagerness
opened: pasting a zpub on a fresh install began the startup scan
*immediately*, against the public mempool.space default — the wallet's
addresses went to a third-party operator **before the user had any say
about the backend** (user-confirmed rationale: addresses must not leak to
the public default before an explicit choice). Non-blocking-ness is kept;
what changes is *what it is non-blocking against*.

**The exception (decided here, implemented in `app.py`):** when NO backend
choice exists on any rung — env > config file > stored URL (the
`resolve_chain_base_url` ladder) **nor the explicit public opt-in record**
(ADR-0023 amendment 2's `chain_backend_choice="public"` settings row,
written only by the warned onboarding conversation; a stored-but-empty rung
means "never chose", NOT "chose public") — the startup-scan gate is armed
in a new pre-state `awaiting_backend`: the `ScanFlow` holds its plan (and
on an `AUTO_SCAN=0` launch, where no plan exists to hold, the gate exists
precisely to stand the lazy paths down — see Scope below) and the worker
fetches NOTHING until the backend branch resolves. Every run that
has a choice on any rung behaves exactly as decided above (or opted out
of, `AUTO_SCAN=0`) — unchanged.

- The gate's closed enum grows by one additive member
  (`disabled/awaiting_backend/pending/running/done/skipped`); the `state/1`
  snapshot tag is NOT bumped (TCK-WEB-005's additive rule), and the shipped
  client renders unknown states as "no chip".
- While `awaiting_backend`: `in_progress` and `first_scan_incomplete` are
  TRUE, so decisions 5/6 apply verbatim — cache reads are `stale`-flagged,
  `create_tx` refuses (sends were never possible pre-first-scan anyway),
  the lazy in-handler scan and the watch drain stand down. No chain call of
  any kind leaves the process: address leak and tx-timing leak both wait.
- **Release paths.** An explicit public consent (the warned pick, recorded
  then released in-session) starts the held fetch on the public client —
  which is exactly what the user accepted. An OWN-SERVER choice does NOT
  fire the scan: the live client was built at bootstrap from the old
  resolution (ADR-0018 config-only), and fetching through the public
  default after the user named their own server would be precisely
  decision 4's forbidden silent fallback — the load waits for the next
  launch, and the copy says so. The plan is re-built from the store at
  release time (engine thread, network-free), so addresses allocated
  during the wait are included.
- **Scope of the exception.** It applies to launches where the choice CAN
  be made or pointed at: the interactive CLI (mandatory pre-scan ask,
  ADR-0023 amendment 2) and the web UI (scan held; the launch hint names
  the wait — ADR-0023 keeps the browser consent-free, requirement 5). A
  headless scripted launch is not blocked and not deferred either way —
  the command line itself is the operator's decision (ADR-0023's
  never-block-the-script rule predates this amendment and survives it);
  the operator who wants the deferred posture sets a rung or runs the CLI
  conversation once. `AUTO_SCAN=0` is NOT an escape hatch (security-review
  finding 1): opting out of the AUTOMATIC scan is not consent to an
  unchosen server, so on interactive/web launches the hold arms whenever
  the backend is unresolved regardless of AUTO_SCAN — there the gate exists
  to stand the lazy in-handler scan and the watch drain down (nothing was
  planned to defer), and a release is a user-initiated load, not an auto
  scan. `AUTO_SCAN=0` keeps its lazy-handler semantics only where no hold
  applies: resolved launches (any transport) and headless scripted
  launches (the carve-out above).
- The freshness machinery is untouched: `awaiting_backend` reuses the
  same tool-owned `stale` flag and the same completion cursor semantics.

## Amendment 2 (2026-09-10, TCK-UX-011): the lazy get_balance scan is non-blocking in the web world, blocking in the CLI

A user report dated 2026-09-10 (UX-008 verdict ii) found the last
blocking path decision 1 was meant to retire: a `get_balance` (the
`/balance` quick action, or any chat balance ask) on a store with **no
sync cursor and no scan in flight** — a startup scan that failed/was
skipped, or an `AUTO_SCAN=0` launch with no gate at all — ran its lazy
first scan **inline, on the engine thread**, minutes-class turn block
after the client echo. Decision 1's non-blocking rule is honoured in the
WEB world by the dedicated worker (decision 2); it never said *how* the
lazy in-handler scan behaves once the worker is not already busy.

**The split (decided here, keyed on TRANSPORT, not on gate-absence):**
`build_dispatch_table` gains an explicit `defer_scans: bool`. In the
web/engine world (`defer_scans=True`) the lazy scan **STANDS DOWN**
exactly like the SCAN-003 in-flight path — answer immediately from the
cache (honest empty/verbatim values), tool-owned `freshness: stale`, an
additive `scan_pending: true` result key, and the value-free narration
line "first scan running in the background…" while the existing dots
keep flowing. It then **KICKS** the background `ScanFlow` (a new
`kick_scan_fn` seam closed over the handler → `ScanFlow.kick_scan`) so
the load actually starts — **no new thread** (the kick reuses the one
chain worker decision 2 created) and **no store access off the engine
thread** (planning + `begin` run on the engine thread; only the pump
persists, decision 3). The kick is idempotent: a no-op while any scan is
in flight, so a double `/balance` never double-kicks.

**The CLI inline scan is INTENTIONAL — the documented CLI exception.**
When `defer_scans=False` (the CLI world, including the `AUTO_SCAN=0` dev
opt-out that never arms a startup scan) the lazy in-handler scan stays
**blocking/inline on the engine thread**. The CLI has no dedicated chain
worker running in this scenario — `AUTO_SCAN=0` launches no background
fetch, and a failed startup scan's worker is idle — so the terminal's
own thread is the only one available to make the chain call, and the
synchronous scan is the shape every CLI test and the Phase 0 AC
("What's my balance?" returns a correct LIVE balance) are built on. This
is the one place the blocking startup-scan behavior ADR-0019 had before
decision 1 supersedes it survives deliberately: in the CLI, blocking the
terminal on the scan a balance question requires is the expected UX; in
the web world it is the bug this amendment fixes. The distinction is
carried by `defer_scans` alone — the same `scan_gate`/worker objects ride
both paths.

`create_tx`'s pre-first-scan refusal (decision 6) and every freshness
narration pin are UNCHANGED by this amendment: the stand-down only makes
a `get_balance` answer cache-served and honest about it while starting
the load.

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

**Pins that change (decision 7):** the `threading.enumerate() == 1` no-poller-thread
pin in `tests/test_watch_incoming.py` is re-scoped to the thread delta around
`tick()` (the watcher spawns nothing) and engine-thread mode is covered by
`tests/test_engine_pump.py`; the new worker no-store-access and engine-only-persist
pins replace any "worker with its own connection" assumption. The CLI startup
prompt-liveness and the `create_tx` refusal are new pins; the stale-flag narration
fixture is an eval gate.

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

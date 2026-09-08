# ADR-0019: `watch_incoming` — tick-driven polling, surfacing, and privacy

- **Status:** Accepted; §2 amended by ADR-0024 (web-world threading) — see
  the Amendment section at the end.
- **Date:** 2026-09-03 (amended 2026-09-07)
- **Decides:** The Phase 5 (TCK-P5-001) `watch_incoming` first slice: the
  polling loop + thread model, how incoming txs are surfaced (and whether a
  new model intent is introduced), the mempool time-since-block helper, and
  the privacy decision for background watching against a public explorer.
  Relates to ADR-0018 (single chain-backend selection point) and ADR-0002
  (intent protocol lockstep).
- **Scope:** `src/localwallet/config.py` (watch settings),
  `src/localwallet/chain/` (`esplora.get_tip_block`, `chain/watch.py`), the
  wiring in `src/localwallet/app.py`, the CLI REPL drain, tests.

## Context

Phase 5 AC (PROJECT.md §12): "incoming mempool tx surfaced within one poll
cycle". `watch_incoming` is the feature name for periodic re-scanning so the
app can notice a transaction arriving at a watched address. PROJECT.md §7.2
lists `watch_incoming` under "known-shape intents (illustrative, not final)".

Two questions drive this ADR:

1. **Is `watch_incoming` a new *user-facing model intent*?** Phase 5's AC is
   about *surfacing* — the app noticing and telling the user — not about a
   model-addressable capability. Surfacing is proactive and deterministic:
   the LLM is explicitly not in the polling loop (AGENTS.md). No user
   utterance triggers it, and the narration is built from dispatcher-owned
   facts (P2-004: amounts/addresses quoted verbatim from tool output), not
   from model text.
2. **How does the poller run without violating the single-threaded SQLite
   invariant?** The store keeps one connection per `Store` instance and is
   not shareable across threads; a poller thread would need its own
   connection and a thread-safe event queue.

## Decision

### 1. NO new intent (surfacing rides existing mechanisms)

Phase 5 does **not** require a new user-facing intent for this slice. We
introduce **no new intent** (registry stays at 12; `v` stays `0`; no
grammar/prompt/protocol change) — the smallest closed extension. The
notification text is dispatcher-owned narration printed by the CLI directly
from the poller's structured events; the model is never involved in the
polling loop or the narration. (If a later ticket needs a model-addressable
"watch status" phrasing, it would follow full lockstep then — this is a
deliberate deferral, not a commitment.)

### 2. Single-threaded / tick-driven poller (no background thread)

`IncomingWatcher` (in `chain/watch.py`) exposes:

- `tick()` — runs exactly ONE poll cycle synchronously against the injected
  **probe** (a test double in tests; the production scan+store reader in the
  app) and returns the list of `IncomingEvent`s to surface. No sleeps.
- `poll_due(now)` — whether a configured interval has elapsed since the last
  poll (seeded at construction so a fast session never scans and an idle gap
  of ≥ one interval triggers a poll).

The CLI REPL calls `poll_due`/`tick` **between user turns** and prints the
returned events. This implements the `watch_interval_s` semantics in one
thread, shares no sqlite object across threads, and needs no background
thread or event queue — the returned list IS the "events list the CLI drains
and prints". The single-threaded design is pinned by a test (no poller
thread exists; `tick` is synchronous and deterministic). A true background
thread with its own store connection is explicitly deferred; revisit only if
real-time (no-user-interaction) surfacing is required. *(Superseded for the
threaded world by the ADR-0024 amendment at the end of this document; the CLI
keeps this design.)*

### 3. Polling reuses the SINGLE config-selected EsploraClient

The production probe wraps `scan_wallet(store, client, wallet)` (the same
`client` construction site as everywhere else, which resolves through
`ChainConfig.from_settings`, ADR-0018). When self-hosted
(`LOCALWALLET_CHAIN_BASE_URL` set) polling hits the user's node, never the
public API. The startup watch line states — in lockstep with the privacy
banner — whether watching runs against the user's own node or the public API.

### 4. Surfacing = dispatcher-owned facts; deterministic dedup + confirmed transition

Detection is plain code. The probe reads the wallet's transactions and UTXOs
from the store (existing accessors only — **no store schema change**) and
shapes *incoming* transactions (scan directions `in`/`self`) into
`WatchedTx` observations carrying a verbatim address and received amount.
The poller keeps an in-process map `txid -> last-confirmed-flag` (process-
scoped, like the send flow's session state) and emits:

- a `received` event the first time a tx appears (with its current
  confirmation state), and
- a `confirmed` event exactly once when a surfaced tx later confirms (with
  its block height).

The same tx is never re-surfaced; the narration quotes the event's values
verbatim. These values are deliberately shown to the **user** in the UI (the
required exception to the no-addresses/amounts rule, PROJECT.md §7.8) and are
never logged or passed to the model.

### 5. mempool time-since-block helper

`time_since_last_block(client, now)` returns integer seconds since the last
block's timestamp, or `None` (clean unavailable) when the backend exposes no
timestamp or the lookup fails. The tip timestamp comes from the configured
backend via the new `EsploraClient.get_tip_block`, which parses `/blocks/tip`
with the same tolerant spirit as the `get_tip_height` divergence fix (HANDOFF
§5): a block-list yields the max-height entry's `timestamp`; a bare-integer
shape yields none (→ `None`, never a fabricated value). Negative (future
timestamp / clock skew) clamps to 0.

### 6. Privacy decision (public-API polling)

Background polling against a public explorer periodically re-queries the
user's addresses — a larger privacy cost than on-demand queries. Decision:

- **Conservative default interval (60 s).** `LOCALWALLET_WATCH_INTERVAL_S`
  defaults to `60.0`; `0` turns watching off entirely (off-via-zero).
- **Honest indicator.** The startup watch line states whether background
  watching runs against the public API or the user's own node, reading the
  SAME single selection point (`Settings.chain_base_url`, ADR-0018) as the
  privacy banner — they can never disagree.
- **Opt-out.** `LOCALWALLET_WATCH_INTERVAL_S=0` disables watching. Users on
  the public default who care are pointed at `LOCALWALLET_CHAIN_BASE_URL` for
  their own node, where polling is private and cheap.
- Malformed interval env fails closed (`ValueError` at settings load).

## Alternatives considered

- **Add a `watch_incoming`/`watch_status` intent (registry 12 → 13).**
  Rejected for this slice: Phase 5's AC is deterministic surfacing, not a
  model capability; adding an intent would require full lockstep
  (grammar/schema/prompt/drift pins/evals) for no functional gain, and the
  notification path never needs model text. Deferred to a future ticket that
  actually needs model-addressable watch state.
- **Background poller thread with its own store connection.** Deferred: the
  single-threaded tick-driven design satisfies the AC, is fully testable, and
  avoids cross-thread sqlite handling and a thread-safe queue. Revisit if
  real-time (idle) surfacing is required.
- **Store poll-state table for dedup.** Rejected: in-process poller state is
  process-scoped (like the send flow) and needs no schema change; a persisted
  "already surfaced" marker can be added behind a schema-versioned migration
  if cross-run dedup is ever needed.

## Consequences

- `config.py` gains `watch_interval_s` (`LOCALWALLET_WATCH_INTERVAL_S`,
  default 60.0, `0` = off, malformed fails closed).
- `chain/esplora.py` gains `TipBlock` and `get_tip_block` (tolerant
  `/blocks/tip` parsing).
- New `chain/watch.py` owns `IncomingWatcher`, `IncomingEvent`, `WatchedTx`,
  and `time_since_last_block` — no network imports (polling uses the injected
  client/probe).
- `app.py` wires the production probe over the single client, prints the
  watch privacy line, and the REPL drains `poll_due`/`tick` between turns.
- No intent/protocol/grammar/prompt change; `v` stays `0`; existing lockstep
  tests unchanged.
- New hermetic tests (`tests/test_watch_incoming.py`): poll-cycle AC, dedup,
  confirmed-transition, interval/off config matrix, malformed env fails
  closed, time-since-block integer math + tolerant parsing, tick-driven
  pin, app probe + narration.

## Amendment (2026-09-07, ADR-0024 decision 9): web-world threading

The single-threaded tick-driven poller of ADR-0019 §2 remains the CLI-world
design, but is superseded for the threaded world by ADR-0024's threading
model: a **single state-owning engine thread** owns all state (store, flow,
watcher dedup, agent loop); **stateless transport threads** (HTTP/SSE)
marshal bytes only; and chain I/O runs on a **dedicated worker with no store
access**, posting immutable result sets to the engine, which is the **only**
persister (`Store.persist_scan_result`, the single atomic transaction). The
watch poll — like the startup scan — is chain I/O and therefore runs on that
worker in the web world (ADR-0022), not on the engine thread and not on a
transport thread.

ADR-0019 §2's "A true background thread with its own store connection is
explicitly deferred" and its single-threaded no-poller-thread pin are
**retired as planned work** for the threaded world: the dedicated chain
worker has **no store access** (so "its own store connection" is not what it
does — the engine persists), and the no-poller-thread test pin in
**`tests/test_watch_incoming.py`** (the "single-threaded design (ADR-0019):
tick is synchronous, no sleep / no poller thread" test, which asserted
`threading.enumerate() == 1`) is superseded by the worker model and retired
as of TCK-WEB-001/ADR-0024: the pin is re-scoped to the thread DELTA around
`tick()` (the watcher itself must still spawn nothing), and engine-thread
mode is covered by the pump harness in `tests/test_engine_pump.py`. The CLI
retains the between-turns tick where no background thread is needed
(ADR-0022 decides the exact split); the privacy decision (§6), the dedup
map, and the surfacing (dispatcher-owned facts, quoted verbatim, never
logged or passed to the model) are unchanged.

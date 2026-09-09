# ADR-0024: Localhost web UI — opt-in browser front-end over the existing engine

- **Status:** Accepted (decisions fixed by orchestrator + web-architecture consult
  2026-09-07; implementation ticket TCK-WEB-000).
- **Date:** 2026-09-07
- **Decides:** The web-UI architecture: how a browser front-end talks to the
  existing single-threaded engine without violating the closed-intent, dual-key,
  watch-only, and one-network-module invariants. Records the threading model
  (one state-owning engine thread + stateless transport threads + a dedicated
  chain-I/O worker), the SSE transport decision, the security surface (token,
  Host allowlist, no CORS, no cookies, no session), the XSS render contract,
  the button-to-turn wiring, and the ADR-0019 amendment drafted below.
- **Scope:** `src/localwallet/app.py` (queue-driven pump replacing `_repl`'s
  `input_fn`; `output_fn` → event emitter; scan-dot stdout parameterization),
  a new `src/localwallet/ui/web/` package, `tools/lint_network.py` + its test
  pins (exception list only), tests, and docs. Relates to ADR-0019 (watch
  threading, amended below), ADR-0013 (dual-key confirm gate), ADR-0010 (single
  wallet), ADR-0016 (loopback lint-exception structure this mirrors),
  ADR-0021 (mainnet-only), ADR-0022 (scan freshness / chain worker — drafted
  alongside so the threading model agrees), and ADR-0007 (the one-file
  network-exception precedent).

## Context

Today the app is a CLI REPL: `run()` in `src/localwallet/app.py` calls
`_repl()` which reads lines via `input_fn` and writes via `output_fn`, and every
user turn flows through `_run_turn()` — confirm-gate classification first, then
`loop.run()` (3-layer validation → allowlist dispatch). The web UI must reuse
this engine **unmodified in substance**: the same `_run_turn` path, the same
dual-key gate, the same `AgentLoop`/dispatch table, the same store and chain
client. It must not fork a second wallet logic path, because the security
posture (closed intents, dispatcher-owned confirmation, value-free narration,
watch-only) is load-bearing and cannot be re-implemented per-UI.

The core architectural problem is threading. The engine's state — the SQLite
`Store` (`sqlite3.connect(...)` in `store/db.py` opens **one** connection in
`__init__`, autocommit/isolation_level=None, used only by the construction
thread), the `TxFlow`/`SendSession` send state, the `IncomingWatcher` dedup map,
the `AgentLoop` — is single-threaded by design and is not shareable across
threads. A browser talks over an HTTP/SSE server, which is inherently threaded
(`ThreadingHTTPServer`). The question is where the boundary sits.

The consult (2026-09-07) supplied the evidence this ADR records: the local LLM
runtime (llama.cpp via `ctypes.CDLL`) **releases the GIL during decode**, so a
model generation is effectively non-blocking for other threads — but the *engine
turn* still owns the store and the send flow, and those must not be touched from
another thread. stdlib `http.server` defaults to **HTTP/1.0** (connection-per-
request; a plain keep-alive-less server), and `ThreadingHTTPServer.block_on_close`
is a shutdown trap (a close that waits for every handler thread to drain can hang
on a stalled connection). `EventSource` **cannot send custom request headers**, so
it cannot carry the per-launch token — hence `fetch` + `ReadableStream`.

## Decision

### 1. Localhost-only, opt-in web UI; the CLI stays the default

Launching the web UI is explicit: `--web` or `LOCALWALLET_UI=web`. Without it,
behavior is byte-identical to today (the CLI REPL). The server binds **only to
`127.0.0.1`** (never `0.0.0.0`) on an **ephemeral OS-assigned port**, and prints a
canonical `http://127.0.0.1:<port>/?token=<token>` URL for the user to open. There
is **no remote access**: the server is unreachable from the LAN or the network by
construction (loopback bind). This is an inbound-loopback surface, not an
outbound one — the distinction that motivates decision 10.

### 2. Stdlib `ThreadingHTTPServer`, HTTP/1.0, stay-and-document

The HTTP server is `http.server.ThreadingHTTPServer` (stdlib) with **no new
runtime dependency** (the repo's chosen stack forbids gratuitous additions; a
framework adds nothing the stdlib server + SSE can't do here). HTTP/1.0 is the
stdlib default and is **accepted, documented, and pinned** — not "fixed": each
browser request gets its own connection, which is exactly the request/response +
SSE shape we need and is the simplest thing that works. No dependency, no
HTTP/2, no keep-alive tuning beyond what the transport needs. The one deviation
from stdlib defaults that matters is `TCP_NODELAY` on the SSE sockets (one frame
per write, decision 5) and `block_on_close=False` (shutdown, decision 5).

### 3. ONE state-owning engine thread; stateless transport threads

All state lives on a single **engine thread**:

- The `Store` is constructed **on the engine thread** (`sqlite3` default
  `check_same_thread=True` guards this — the connection must never be touched by
  another thread; ADR-0022's chain worker therefore has no store access).
- The `TxFlow`/`SendSession`, `IncomingWatcher`, `AgentLoop`, and dispatch table
  are owned by the engine thread.
- **Turns are never cancelled.** A turn that has started runs to completion; the
  front-end observes progress via the SSE event stream and can re-sync state from
  a ring-buffer replay (decision 5). Cancelling a mid-flight model generation
  would tear down engine invariants for no benefit.
- **All reads and writes of engine state route through the engine thread's
  command queue.** The HTTP/SSE transport threads only marshal bytes; they never
  call a handler, the store, or the flow directly. A transport thread that needs
  a fact enqueues a request and receives the result (a future) from the engine.

Concretely this is a **queue-driven pump** replacing `_repl`'s blocking
`input_fn("you> ")` loop (WEB-001): instead of blocking on `input`, the pump
blocks on a command queue, runs each dequeued turn through the *same* `_run_turn`
path, and routes `output_fn` output into the SSE event emitter. The existing
~96 REPL e2e tests run **unedited** against this queue-driven harness — that is
the seam-proof: if the engine path were changed, those tests would break.

### 4. Chain I/O runs on a dedicated worker with NO store access; ONLY the engine persists

Scan/watch network I/O (ADR-0022) runs on a **dedicated chain worker thread** —
**not** the engine thread and **not** the transport threads. The worker:

- holds **no `Store` reference** (ADR-0022: it derives + fetches and returns
  **immutable record sets**, never touching sqlite; this is required because the
  store connection is engine-thread-owned, `check_same_thread=True`);
- posts its immutable result sets to the engine queue;
- the **engine thread** is the *only* persister: it calls
  `Store.persist_scan_result` (the single atomic transaction in `store/db.py`
  lines 597–633 — addresses, derivation, UTXO snapshot, tx rows, sync state
  commit together or not at all), so cross-thread sqlite use is impossible by
  construction, not by discipline.

This covers the startup scan **and** the watch polls (ADR-0022): both are chain
I/O and both post to the engine, which persists. The ADR-0019 single-threaded
watch tick gives way to this worker for the web world; the CLI may keep the
between-turns tick where a background thread adds nothing (ADR-0022 decides).

### 5. SSE transport: `fetch` + `ReadableStream`, not `EventSource`

Server-Sent Events carry turn progress, watch events, and state. Decisions:

- **`fetch` + `ReadableStream` on the client — NOT `EventSource`.** `EventSource`
  cannot set custom request headers, so it cannot present the per-launch token
  (decision 6). `fetch` can. The event stream is a normal POST/GET with the token
  header.
- **Event-id ring buffer + `Last-Event-ID` replay.** The server keeps a bounded
  ring of recent events; on reconnect a client sends `Last-Event-ID` and the
  server replays from there, so a dropped connection or a slow transport never
  loses a state transition.
- **Bounded per-connection queues with overflow → close → replay.** Each
  connection's outbound queue is bounded; on overflow the connection is closed
  and the client reconnects with `Last-Event-ID` (replay is the recovery path —
  never an unbounded buffer).
- **Heartbeat.** A periodic keepalive comment-frame (`: ping`) keeps the
  connection alive past idle and lets the client detect a dead stream.
- **Broken-pipe swallow.** A client that closed its socket mid-write is not a
  server error; the write exception is swallowed and the connection torn down.
- **Sentinel shutdown with `block_on_close=False`.** The server does **not** wait
  for every handler thread to drain on close (a stalled connection would hang
  shutdown — the `block_on_close` trap). Instead a sentinel event signals the
  engine to finish and the server closes without blocking on connections.
- **One frame per write + `TCP_NODELAY`.** Each event is a single `write`
  (its own frame), and `TCP_NODELAY` disables Nagle batching so events flush
  immediately — no 40ms frame-merge latency on the SSE sockets.

### 6. Security surface: loopback + token, Host allowlist, no CORS, no cookies

- **`127.0.0.1` bind + ephemeral port + canonical URL.** The server is reachable
  only on loopback; the port is OS-assigned (never a fixed, predictable port);
  the printed URL is the canonical entry point.
- **Random per-launch token via request header.** Each launch generates a random
  token; every request (including the SSE stream) must present it in a header
  (`X-Auth-Token`). The token is the only credential and it is **per-launch** —
  it dies with the process.
  - *Why the token stays (security review 2026-09-08 — KEEP, re-asked by the
  user):* loopback bind ≠ user-only access — every local user/process reaches
  the port. A hostile WEBPAGE is already blocked tokenlessly (its cross-origin
  POSTs carry `Origin: https://evil.com` → refused; no CORS ⇒ it cannot read
  anything; Host allowlist covers rebinding). A NON-BROWSER local client sends
  no `Origin`, so without the token it could drive the FULL send flow
  (create → confirm → sign → broadcast): the dual-key gate does not defend when
  one client controls both POSTs, and the file-signer rung completes with zero
  user interaction. The token is therefore the SOLE credential on that axis.
  UX cost is zero (island auto-injection). A tokenless read-only `GET /state`
  was evaluated and rejected: no gain (pages can't read cross-origin anyway),
  just a second weaker auth policy.
- **Host allowlist (DNS-rebinding primary defense).** The server validates the
  `Host` header against a small allowlist (`127.0.0.1`, `localhost`, and the
  literal loopback IP on which the server is bound). This is the primary defense
  against DNS-rebinding attacks (a malicious page that resolves a public name to
  `127.0.0.1` and drives the loopback server); the token header is the
  secondary, independent layer.
- **NO CORS headers.** The server sends no `Access-Control-Allow-*` headers, so
  a cross-origin page cannot read responses or stream events. Because the server
  is loopback-only and token-gated, CORS would only widen the surface; its
  absence is intentional and test-pinned (WEB-003).
- **NO cookies, ever.** There are no `Set-Cookie` headers and no session
  mechanism. CSRF is therefore moot (a browser cannot be coerced into attaching
  a token it doesn't have). This is **documented so nobody adds sessions later**:
  the token-header scheme is the whole auth surface and must stay that way.
- **Token never in URLs or logs.** The token rides a header, never the query
  string (no referer/path leakage), and is never written to any log. **Access
  logs are suppressed entirely** (WEB-002 pins "token not in logs"); the watch
  and app layers already value-free-scrub, and the HTTP layer suppresses its
  request logging so nothing crosses the wire into disk.
- **Local-process trust statement.** The security model is explicitly **local**:
  anyone who can already execute code as the same user on the machine (or read
  the process's memory / environment) can bypass any of this. The web surface
  protects against *other* local processes, browsers, and remote pages — it is
  **not** a boundary against a hostile same-user process. This is stated in the
  README/doctor line and the code, so the guarantee is never oversold.

### 7. XSS render contract: `textContent`-only + CSP + red-team render fixtures

- **Every value rendered by the web UI is written with `textContent`** (or
  equivalent DOM APIs), never `innerHTML`/`outerHTML`/template-string markup.
  This is a hard contract because the values include model output.
- **`sanitize_tool_output` is NOT an HTML escaper.** It is the agent layer's
  value-free scrubber for the CLI; it must not be relied on to make output safe
  for HTML injection. The render layer's `textContent` contract is the real
  defense and is enforced separately.
- **CSP.** A Content-Security-Policy restricts script sources (no inline, no
  `eval`, same-origin module scripts only), further shrinking the blast radius if
  a value ever did reach markup.
- **Red-team render fixtures in `evals/`** pin the contract: model-output-shaped
  strings (markup, `<script>`, attribute-injection, event-handler payloads) are
  rendered and asserted to appear as inert text. This is a test-pinned invariant,
  not a guideline.

### 8. Buttons are canonical utterances through FULL `_run_turn`; no direct handler bypass

The web UI's action buttons (confirm / cancel / sign / retry / fee-speed) do
**not** call handlers or endpoints directly. Each button injects the **canonical
whitelisted utterance** (the exact phrase the CLI user would type — e.g. the
confirm/cancel/sign wording) into the **full `_run_turn` path**: confirm-gate
classification → `loop.run` → allowlist dispatch. Consequences:

- **The dual-key rule (ADR-0013) is preserved.** The button's utterance passes
  through `ConfirmGate.classify` like any turn, so a button click is the user's
  second key alongside the handler's `confirm_tx` envelope — exactly as a typed
  "confirm" is. A click is never a first-class approval the handler trusts
  directly.
- **Endpoint → `flow.confirm()` bypass is explicitly forbidden.** No HTTP
  endpoint may call `TxFlow.confirm` (or any flow mutator) directly. All
  mutation funnels through the engine's `_run_turn`; the flow state machine and
  the gate remain the sole authorities (a test pins that no route touches the
  flow object).
- The free-text box goes through the same full 3-layer pipeline (GBNF → pydantic
  → business rules → allowlist dispatch) as every other utterance.

### 9. ADR-0019 amendment (drafted TEXT — to be landed separately)

> **The following amends ADR-0019. It is drafted here as TEXT and is NOT yet
> landed; the orchestrator lands it into `docs/adr/0019-watch-incoming.md`
> separately (as its own amendment section), together with or after the WEB-001
> engine work. Do not treat it as in force until landed.**
>
> **ADR-0019 §2 amendment (web-world threading):**
>
> The single-threaded tick-driven poller of ADR-0019 §2 remains the CLI-world
> design, but is superseded for the threaded world by ADR-0024's threading model:
> a **single state-owning engine thread** owns all state (store, flow, watcher
> dedup, agent loop); **stateless transport threads** (HTTP/SSE) marshal bytes
> only; and chain I/O runs on a **dedicated worker with no store access**, posting
> immutable result sets to the engine, which is the **only** persister
> (`Store.persist_scan_result`, the single atomic transaction). The watch poll —
> like the startup scan — is chain I/O and therefore runs on that worker in the
> web world (ADR-0022), not on the engine thread and not on a transport thread.
>
> ADR-0019 §2's "A true background thread with its own store connection is
> explicitly deferred" and its single-threaded no-poller-thread pin are **retired
> as planned work** for the threaded world: the dedicated chain worker has **no
> store access** (so "its own store connection" is not what it does — the engine
> persists), and the no-poller-thread test pin in
> **`tests/test_watch_incoming.py`** (the "single-threaded design (ADR-0019): tick
> is synchronous, no sleep / no poller thread" test, which asserts
> `threading.enumerate() == 1`) is superseded by the worker model and is retired
> as part of WEB-001/ADR-0022. The CLI retains the between-turns tick where no
> background thread is needed (ADR-0022 decides the exact split); the privacy
> decision (§6), the dedup map, and the surfacing (dispatcher-owned facts, quoted
> verbatim, never logged or passed to the model) are unchanged.

### 10. Lint exception: inbound-loopback server files under `src/localwallet/ui/web/**`

The web server performs **inbound** network I/O (listening on loopback), which
the `tools/lint_network.py` ban on outbound network imports must distinguish
from the *outbound* network access that only `chain/` may have. Following the
ADR-0016 structure (a directory-scoped `NODE_NETWORK_DIRS` exception, pinned by
tests so it cannot silently grow), the exception is:

- **Scope:** `WEB_SERVER_DIRS = ("ui/web",)` (directories relative to the lint
  root), added to `tools/lint_network.py` alongside `NODE_NETWORK_DIRS`, with the
  error message naming it.
- **Pinned list + test requirement (mirroring ADR-0016):** `tests/test_lint_network.py`
  pins `WEB_SERVER_DIRS == ("ui/web",)` and that the exception is a *directory*,
  not a name prefix (a sibling file named `web.py` is still flagged). The
  exception cannot grow without a test + ADR change.
- **Inbound ≠ outbound:** the exception lets `ui/web/**` use the stdlib `http`
  server to *listen on loopback*; it does **not** authorize outbound calls. Any
  outbound network access stays in `chain/` (e.g. a future "open a link" feature
  is not this exception's business). Loopback-only is enforced by the bind
  decision (decision 1) plus tests, as in ADR-0016's operational enforcement.
- The web package may import `http.server` / `socketserver` (the HTTP surface);
  every other package remains banned from them.

### 11. Launch surface

`--web` / `LOCALWALLET_UI=web` selects the web server instead of the REPL after
the same startup wiring (wallet resolve, store open, model pick, chain client).
Everything before the REPL/web split is shared; the split is only at the input/
output seam.

### 12. Packaging prerequisites (recorded for WEB-006)

Two pre-existing CLI details must be parameterized so a frozen console app can
run the web UI: the scan-progress **dots** (currently `sys.stdout.write(".")`
directly in `_scan_progress_tick`/`_end_scan_progress_line`) must flow through the
engine's `output_fn`-equivalent so the web world can suppress/route them, and the
static assets must load via **`importlib.resources`** (not filesystem-relative
paths) so PyInstaller `--add-data` works. These are recorded here as prerequisites
for WEB-001 (dots parameterization, TCK-UX-001 surface) and WEB-006 (resources
loading), not implemented by this ADR.

## Sequencing

- **WEB-001 before WEB-002:** the engine pump + queue harness (and the lint
  exception pins) land before any server, because the server is thin over the
  pump; the seam-proof (existing ~96 REPL e2e tests green unedited on the
  queue-driven harness) gates WEB-001.
- **TCK-UX-002 is a GATE-MERGE before WEB-004:** WEB-004's sign/confirm buttons
  depend on TCK-UX-002 ("sign" in the whitelist, chained turn) landing first —
  the buttons route through `_run_turn`, whose confirm/sign handling UX-002
  finalizes. WEB-004 must not land before UX-002.
- **ADR-0022 / SCAN-002/003 absorbed into WEB-005's scan half:** the web UI's
  non-blocking startup scan and freshness flags (WEB-005) implement the
  decisions drafted in ADR-0022; SCAN-003's work splits between WEB-005 (web
  half) and the CLI half (see ADR-0022's file-plan draft).

## Non-goals (explicitly out of scope)

- **No remote access** — the server is loopback-only by construction (decision 1).
- **No auth beyond the launch token** — no accounts, no password, no session,
  no cookies; the token is the whole credential (decision 6).
- **No multi-wallet** — ADR-0010 single-active-wallet profile; the web UI
  operates on the same active wallet, no switcher.
- **No build toolchain** — the front-end is vanilla ES modules + CSS served as
  static files; no bundler, no framework, no npm dependency.

## Alternatives considered

- **`EventSource` for the stream.** Rejected: it cannot set request headers, so it
  cannot carry the token. `fetch` + `ReadableStream` is the only stdlib-browser
  way to send an authenticated SSE-like stream.
- **A second wallet-logic path / endpoints that call handlers or the flow
  directly.** Rejected: it would fork the load-bearing security posture (closed
  intents, dual-key, value-free narration). Buttons go through the full
  `_run_turn` (decision 8) or not at all.
- **A framework (FastAPI/Flask) or an ASGI server.** Rejected as a new runtime
  dependency for what the stdlib `ThreadingHTTPServer` + SSE already covers; the
  stack forbids gratuitous additions, and HTTP/1.0 is documented (decision 2).
- **CORS headers / cookie sessions.** Rejected: the loopback+token model makes
  them both widen the surface (decision 6); CSRF is moot without cookies, and the
  decision is recorded so no one adds sessions later.
- **`block_on_close=True` shutdown.** Rejected as the hang trap (decision 5):
  a stalled connection would deadlock shutdown; the sentinel + non-blocking close
  is the robust path.
- **Cancellable turns.** Rejected (decision 3): mid-generation cancellation tears
  down engine invariants for no benefit; completion + replay is simpler and safe.

## Consequences

- **New package `src/localwallet/ui/web/**`** — HTTP server, static shell,
  chat-turn API, SSE stream (ring buffer, replay, bounded queues, heartbeat,
  shutdown-join). Lint-exempt inbound-loopback (decision 10, test-pinned).
- **`app.py` gains a queue-driven pump** replacing `_repl`'s `input_fn` block;
  `output_fn` becomes an event emitter; the existing ~96 REPL e2e tests must pass
  **unedited** against it (seam-proof for WEB-001).
- **Engine-thread ownership moves** (WEB-001): Store, flow, watcher, loop all
  constructed/owned on the engine thread; `check_same_thread` is the guard;
  transport threads marshal bytes only.
- **Chain worker (ADR-0022)** with no store access; the engine is the only
  persister via `persist_scan_result` (db.py:597–633).
- **Security pins (WEB-002/003):** access-log suppression + token-out-of-logs;
  no-CORS / no-Set-Cookie; Host allowlist; XSS `textContent`-only + CSP; red-team
  render fixtures in `evals/`. Value-free / no-secrets rules are restated and
  enforced for the HTTP surface (nothing echoed to logs; token header-only).
- **ADR-0019 amendment** (decision 9) lands separately by the orchestrator; the
  no-poller-thread test pin in `tests/test_watch_incoming.py` is retired as
  planned work with WEB-001/ADR-0022.
- **Launch `--web` / `LOCALWALLET_UI=web`**; CLI default unchanged.
- **Packaging prerequisites recorded** (dots parameterization WEB-001, static
  assets via `importlib.resources` WEB-006).
- **Real code cost noted:** the store-fused `scan_wallet`/`rescan_wallet` (which
  today take `store` and persist inside `_run_scan`, scan.py:428) must be split
  into a derive+fetch phase returning immutable record sets and a persist phase
  called only by the engine — a real refactor (ADR-0022 details it). The web
  surface must also restate the no-address/amount value-free rules, since an HTTP
  response is a new disclosure surface (mitigated by suppressing logs and
  token-gating).

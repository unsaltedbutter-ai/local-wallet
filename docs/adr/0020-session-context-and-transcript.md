# ADR-0020: Session Context, Confirmation ETA, and Transcript Management

- **Status:** Accepted
- **Date:** 2026-09-03
- **Decides:** The Phase 5 final slice (TCK-P5-002): (1) the deterministic
  confirmation ETA vs fee rate / mempool state; (2) R13 session
  memory/summarization (PROJECT.md §13 R13, §7.1); (3) OQ14 transcript
  management (PROJECT.md §14 OQ14). Also records the deferred-run live-ETA
  cross-check procedure (Phase 5 AC "ETA sane vs live fees").
- **Scope:** `src/localwallet/chain/eta.py` (ETA), `src/localwallet/agent/session.py`
  (summary + transcript), `src/localwallet/agent/loop.py` (R13 integration),
  `src/localwallet/app.py` (card/FACTS wiring + `/export`/`/scrub` CLI
  commands), tests. Relates to ADR-0002 (intent protocol), ADR-0006 (context
  budget), ADR-0011 (fee targets), ADR-0019 (watch/time-since-block).

## Context

Three Phase 5 items land together as the final slice:

1. **Confirmation ETA.** The send-flow confirmation card (TCK-P2-004) shows
   amount/recipient/fee/size/change but no expectation of how long
   confirmation might take. PROJECT.md G7 / §10 wants the assistant to
   surface fee-vs-urgency honestly, and Phase 5 AC wants the ETA "sane vs
   live fees".
2. **Session memory (R13).** "Small-model context discipline — long sessions
   degrade." The Phase 0 loop dropped the oldest turns past a hard cap
   (`MAX_HISTORY_TURNS = 20`, ADR-0006 ≤8K budget); Phase 5 replaces that
   with deterministic summarization.
3. **Transcript management (OQ14).** "Chat-log retention: on-disk transcripts
   retention/scrub/export UX." PROJECT.md §7.8 lists "session transcripts
   (with scrub/export)" as a store concern.

## Decision

### 1. Confirmation ETA: deterministic, narration-only, never a gate

A new pure module `chain/eta.py` computes an ETA estimate from the chosen
fee target and the mempool state hint. Deterministic and **narration-only**:
the ETA never feeds the confirm gate, coin selection, the fee, or any
re-validation path — it only decorates the confirmation card and the FACTS
block so the model narrates honest expectations.

- **Mapping (target → expected blocks → minutes):** `fast` ≈ 1 block,
  `medium` ≈ 6 (half-hour), `slow` ≈ 12 (hour), at `10` minutes per block —
  matching the semantics of the estimator's three recommended-fee targets
  (ADR-0011 / `chain/fees.py`: fastestFee / halfHourFee / hourFee).
- **Mempool state:** `time_since_last_block` (ADR-0019) is the hint. When it
  exceeds 10 minutes, one extra expected block is added per additional
  10-minute interval, capped at 3 (a stretched mempool never blows the
  estimate up without bound). Because the same congestion adder applies to
  every target, the ordering invariant always holds: `fast ≤ medium ≤ slow`
  minutes (the AC sanity property — a higher fee rate never yields a longer
  ETA).
- **Honest wording:** "~60-70 min — estimate only, not a guarantee". The ETA
  is never promised; uncertainty is explicit (a one-block upper bound).
- **Injection (dispatcher-owned facts only):** the create_tx handler computes
  the ETA and returns `eta_blocks`/`eta_minutes`/`eta_wording` on the card
  result; the CREATED-state FACTS carry `pending_tx_eta_minutes` /
  `pending_tx_eta_wording`. The model never computes an ETA — it quotes these
  verbatim. The mempool hint is injected via a `seconds_since_last_block_fn`
  (production wires `time_since_last_block(client)`; tests inject fakes);
  any failure degrades to no congestion adjustment, never a crash.

### 2. Session memory / summarization (R13): deterministic, value-free, budgeted

The agent loop keeps the **most recent** turns verbatim and folds everything
older into a compact, **structured, deterministic summary** built only from
dispatcher-owned state — NOT model-generated (no extra inference, no
nondeterminism) and **not persisted** (in-memory, process-scoped; a
schema-versioned store table is deferred unless a future ticket needs
cross-run persistence).

The summary records, per older turn, only its *shape*:

- **topics / intents covered** — intent names parsed from the canonical
  envelope JSON (a closed enum; never user/model free text),
- **flow state transitions** — a count of destructive send-flow intents
  (`create_tx`/`confirm_tx`/`sign_tx`/`broadcast_tx`),
- **watch events count** — a value-free integer the REPL records from the
  watch drain (`record_event("watch_events", n)`).

PRIVACY: the summary and the rolling context are **value-free** — no
addresses, no amounts, no xpubs. Money values appear only in the per-turn
FACTS block for the current turn, never in the retained summary. (The recent
window is verbatim by design, so it may carry values the user typed; the
*summary block* and the *exported file* are the value-free surfaces.)

**Budget numbers** (pinned): `MAX_RECENT_TURNS = 20` verbatim turns and
`MAX_SUMMARY_CHARS = 400` for the summary block. Together these bound the
injected context for a session of any length (a 30-minute multi-topic session
stays inside the ADR-0006 ≤8K budget). The budget test drives a 300-turn
multi-topic session and asserts the assembled prompt stays under a pinned
character bound while the summary + recent window remain correct.

### 3. Transcript management (OQ14): CLI commands, not model intents

Export and scrub are implemented as **deterministic CLI commands** in the
REPL (`/export <path>`, `/scrub`, `/help`) — NOT model intents. Therefore
there is **no protocol change**: no new intent, no grammar/prompt/drift/registry
change, and `v` stays `0` (the ADR-0002 lockstep rule is untouched by design).
The model never gains access to a new capability or to any new secret material.

- **Export** (`/export <path>`): writes the session transcript (summary
  block + recent turns) as plain text, every line passed through
  :func:`redact_transcript`. The written file is **value-free**.
- **Redaction set** (documented, exported as `REDACTION`): addresses
  (bech32/legacy, matched case-insensitively — BIP-173 permits all-uppercase
  bech32), amounts (`<n> sats` / `<n> satoshis` / `$<n>` USD / `<n> BTC`),
  xpub/keys (extended key
  strings), and cookie paths (`*.cookie`), replaced with stable value-free
  tokens (`<addr>`, `<amount>`, `<key>`, `<cookie-path>`). This is the
  documented set from the ticket.
- **Redaction set — expanded by TCK-SEC-001 after independent review**
  (dated 2026-09-06): the set above now additionally covers txids
  (64-lowercase-hex, `→ <txid>`, matched before the legacy-address pattern
  to avoid a false base58 match), JSON-keyed amounts (`"amount_sats"` /
  `"amount_usd"` / `"fee_sats"` numeric values, `→ <amount>`, which the
  canonical envelope JSON serializes verbatim), and BIP39-shaped seeds
  (exactly 12 or 24 lowercase letter-only 3-8-char words, `→ <seed>` —
  over-redaction of an ordinary 12-word lowercase sentence is accepted in
  the fail-safe direction). Textual amount patterns and the export format
  are unchanged.
- **Scrub** (`/scrub`): clears the in-memory transcript and summary entirely
  — after a scrub the model's next prompt carries no history.
- **Store involvement:** none in this slice (in-memory-only is acceptable
  per the ticket). A schema-versioned `transcripts` table is deferred; export
  is a user-triggered file write, not automatic on-disk retention.

## Deferred-run: live-ETA cross-check procedure (Phase 5 AC)

The Phase 5 AC "ETA sane vs mempool.space on live fees" cannot run in this
hermetic sandbox (no live network in tests). It is a **deferred-run manual
procedure**, to be executed on a funded testnet wallet with live mempool.space:

1. Start the CLI with a testnet watch key and the public backend
   (`LOCALWALLET_CHAIN_BASE_URL` unset, or a self-hosted Esplora).
2. Issue a send (`send <amount> sats to <tb1...>`) and record the printed
   ETA line (e.g. "ETA: ~60-70 min — estimate only, not a guarantee") and the
   `fee_target` shown on the card.
3. Query `GET https://mempool.space/testnet4/api/v1/fees/recommended` and
   `/blocks/tip` at the same time. Confirm:
   - the chosen `fee_target` corresponds to the expected recommended-fee field
     (`fastestFee`/`halfHourFee`/`hourFee`), and the ETA's base blocks match
     the target's confirmation horizon (1/6/12);
   - the ETA is *monotonic* with the live rates (fast ≤ medium ≤ slow
     minutes), and increases deterministically when `time_since_last_block`
     is stretched;
   - the ETA is presented as an estimate, never a guarantee.
4. Optionally broadcast and observe whether confirmation time falls within
   the stated range (informational only — the range is honest uncertainty,
   not a promise).
5. Record results back into this ADR's Results section below.

## Alternatives considered

- **Model-generated summarization — rejected:** extra inference per turn is
  nondeterministic, costs latency (ADR-0006), and would put money values or
  mis-summarized facts into context — the exact failure R13 guards against.
- **Persist transcripts in the store — deferred:** no schema change this
  slice; export is user-triggered and scrub is in-memory, satisfying OQ14
  without a migration.
- **ETA from a fixed lookup table keyed only on the chosen rate — rejected:**
  keying on the target enum (whose names encode the confirmation horizons)
  is simpler, fully deterministic, and keeps the ordering invariant by
  construction; the three target *rates* remain the semantic basis.
- **Export/scrub as model intents — rejected:** would force full ADR-0002
  lockstep (schema+grammar+prompt+drift pins+registry pins+eval fixtures) for
  two deterministic UI features the model never needs; CLI commands need no
  protocol change.

## Consequences

- `chain/eta.py` adds `estimate_eta`/`EtaEstimate` (pure; no network import;
  exported from `chain/__init__.py`). `chain/` already hosts pure helpers
  (fees parsing, time-since-block), so this stays inside the one networked
  module's lint surface without adding I/O there.
- The create_tx card result gains `eta_blocks`/`eta_minutes`/`eta_wording`;
  the CREATED FACTS gain `pending_tx_eta_*`. The confirm gate and the
  re-validation/broadcast paths are untouched — ETA is narration-only.
- `agent/session.py` owns the summary (`SessionSummary`), the redaction set,
  and the export writer. `agent/loop.py` folds overflow into the summary
  instead of dropping it, exposes `scrub`/`export_transcript`/`record_event`/
  `context_prompt`/`session_summary`, and renders summary + recent window in
  the prompt. `MAX_HISTORY_TURNS`/`MAX_RECENT_TURNS = 20`, `MAX_SUMMARY_CHARS
  = 400`.
- `app.py` wires the ETA mempool hint (`time_since_last_block(client)`), the
  card ETA line, the watch-event counter, and the `/export` `/scrub` `/help`
  commands in the REPL.
- No protocol/grammar/prompt change; `v` stays `0`; eval fixtures unchanged;
  existing lockstep tests unchanged.
- New hermetic tests: `tests/test_chain_eta.py` (ordering/sanity, congestion,
  wording), `tests/test_session_transcript.py` (budget, value-free summary,
  export round-trip + redactions, scrub), plus app-level ETA-as-FACTS and
  CLI-command tests in `tests/test_e2e_skeleton.py`.

## Results (deferred-run)

| Date | Backend | ETA shown | live fastest/half/hour | /blocks/tip Δ | Sanity |
|---|---|---|---|---|---|
| (pending) | mempool.space/testnet4 | — | — | — | — |

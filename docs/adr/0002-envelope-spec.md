# ADR-0002 — Envelope Spec v0: Wire Format of the Closed Intent Protocol

- **Status:** Accepted (Phase 0)
- **Date:** 2026-08-31
- **Answers:** PROJECT.md §14 OQ2 — "Envelope spec details: streaming, error
  envelopes, versioning, parallel intents, whether `params` references user
  entities by ID vs inline."
- **Scope:** `src/localwallet/protocol/` (ticket TCK-P0-002); shared with the
  GBNF grammar `src/localwallet/agent/grammar/envelope.gbnf` (TCK-P0-004) —
  the two MUST change together.

## Context

PROJECT.md §8 fixes the invariants of the intent protocol (closed world,
three validation layers, allowlist dispatch, fail-closed) but deliberately
defers the wire-format details to a Phase 0 spec. This ADR is that spec, in
force from the walking skeleton onward. Constraints it must respect: the
model is small and weak at agentic tool use (R1), so the format must be as
mechanical as possible; model output is untrusted input (§5.2), so every
field is validated and nothing is trusted by construction; and the format
must be expressible as a single strict GBNF grammar so malformed output is
syntactically impossible before validation (§5.4).

## Decision

### 1. Canonical envelope contract v0 (model-emitted)

```
{"v": 0, "intent": <enum>, "params": {...}}
  - "v": integer, must be exactly 0.
  - "intent": one of "respond" | "clarify" | "get_balance" (closed enum).
    (extended to six intents in Phase 1 — see "v0 extensions" below)
  - "params": REQUIRED object.
    - respond → {"text": string, 1..4000 chars}
    - clarify → {"question": string, 1..1000 chars}
    - get_balance → {} (empty object; reserved for future opts)
- No extra top-level keys; no extra params keys (closed world);
  unknown intent or wrong version → invalid envelope.
```

One envelope = one intent. Envelope-level rules:

- **Strict key order in the grammar** (`v`, `intent`, `params`) with the
  intent branch coupled to its params shape at decode time, so a mismatched
  pairing (e.g. `intent: "respond"` with `params: {}`) is syntactically
  impossible. The pydantic schema re-checks the pairing anyway (defense in
  depth, and so non-grammar producers — tests, evals, fuzzers — get the
  same rejections).
- **JSON object only** — arrays, scalars, and `null` are invalid envelopes.
- Lengths are character counts; bounds are enforced at the schema layer
  (pydantic) and meaning-level rules (non-blank after strip) at the
  business-rule layer.

### 2. Versioning: integer `v` field, v0 only

- The envelope carries an integer `v`; booleans/strings/floats are not
  integers for this purpose. v0 is the only version; anything else is an
  invalid envelope.
- **Bump policy:** a new version is a *gate*, not a field edit. Shipping
  `v1` requires: (a) a new grammar + schema accepted side by side with v0
  (dual-accept window — both versions validate during the transition);
  (b) eval runs showing the model reliably emits the new version; (c) an
  explicit cutover where v0 rejection flips on; (d) an ADR. The dispatcher
  is version-aware at the parse boundary only; handlers always see
  version-normalized envelopes.

### 3. Error envelope (system→UI only, never model-emitted)

```
{"v": 0, "error": {"code": "invalid_envelope"|"dispatch_error"|"chain_error",
                   "detail": string}}
```

- Produced by the system to report failures to the UI/agent loop. The
  model's only output is the intent envelope; any other shape it emits is
  rejected as `invalid_envelope`.
- `detail` is a human-readable, value-free string: failure messages never
  echo raw payload content (untrusted, possibly huge) and never contain
  xpubs, addresses, or amounts (§7.8 logging policy).
- `chain_error` is defined here for protocol completeness; it is produced
  by chain-facing paths, and `protocol/` imports nothing from `chain/`.

### 4. One intent per envelope (no parallel intents in v0)

Each envelope expresses exactly one intent; there is no batching, no
intent sequences, no "and then" semantics. Rationale: the destructive
flows are dispatcher-owned state machines (`create_tx → confirm_tx →
sign_tx → broadcast_tx`, §8.6) — parallel intents would blur which state
machine a payload belongs to and make confirm-gate reasoning ambiguous;
and a 2B-class model emits one decision at a time far more reliably than a
batch (R1). Revisit only with Phase 6 eval data if a concrete multi-intent
turn pattern emerges.

### 5. Params reference user entities INLINE (no ID indirection in v0)

Params carry values verbatim (recipient address string, amount, option
flags), not references into wallet state. There is no persisted wallet
state for IDs to point at until `store/` lands (Phase 1), and the
quote-verbatim rule (§8.4, R9) already requires the model to copy
addresses/amounts from injected tool-output blocks — inline values are
exactly what the confirm gate and the hardware screen must be compared
against. When persisted entities exist (labels, saved recipients), IDs may
be introduced per-intent behind the same validation discipline, with the
resolved entity injected into context for verbatim quoting.

### 6. Streaming deferred to Phase 5

Envelopes are small (≤ ~4 KB), single JSON objects; streaming partial
envelopes buys nothing in Phase 0 and adds failure modes (partial-grammar
validation, UI flicker, half-validated dispatch). Chat *text* streaming of
`respond` output is a Phase 5 UX concern and does not change the wire
format decided here.

### 7. Validation pipeline binding

The contract binds to code as follows (one re-prompt on any validation
failure, then escalate to `clarify` — `MAX_VALIDATION_RETRIES == 1`):

| Layer | Where |
|---|---|
| 1. GBNF grammar (decode time) | `agent/grammar/envelope.gbnf` |
| 2. Schema (types/enums/ranges/coupling) | `protocol/envelope.py` (`validate_payload`) |
| 3. Business rules (meaning) | `protocol/intents.py` (`BUSINESS_RULES`) |
| Allowlist dispatch | `protocol/dispatcher.py` (`DispatchTable`, `dispatch`, `handle_raw`) |

Module layout note: the closed world (`IntentName`, params models,
`INTENT_REGISTRY`) is defined in `envelope.py` as part of the wire schema
and re-exported by `intents.py`; `intents.py` owns the layer-3 rules. This
keeps imports one-directional (`dispatcher → intents → envelope →
errors`).

## Alternatives considered

- **Free-form tool calling (name + arbitrary kwargs, open set) —
  rejected:** an open world contradicts §8.1; unbounded kwargs evade
  schema audit; and E2B-class models measurably degrade on open tool
  vocabularies (R1). A closed enum with typed params gives the same
  capability with a greppable security surface.
- **Parallel/batched intents in one envelope — rejected for v0:** see §4;
  state-machine ownership and confirm-gate clarity win. Cost is a few
  extra model turns, which evals in Phase 6 will price precisely.
- **ID indirection for user entities — rejected until wallet state
  exists:** nothing to index yet, and it would interpose a resolution step
  between the model's quote and the value shown to the user/device —
  exactly where spoofing bugs live (R9).
- **Streaming envelopes now — deferred:** no Phase 0 consumer; grammar
  constraint of a complete object is strictly simpler and safer.
- **No version field (implicit single format) — rejected:** a format bump
  then requires either a flag day or heuristic format sniffing; an integer
  `v` makes the gate explicit and testable for the cost of two bytes.

## Consequences

- The grammar and the pydantic schema are twins: adding an intent means a
  new `IntentName` member, a params model, an `INTENT_REGISTRY` entry, a
  business rule, a grammar branch, a handler, and eval fixtures — in that
  spirit, all together, with an eval run per the working agreements.
- Every validation failure surfaces as a structured error envelope
  (`invalid_envelope` / `dispatch_error`); handler crashes are contained
  and surfaced the same way, never swallowed, never fatal to the chat loop.
- `get_balance` params are `{}` today; future opts (depth, confirmation
  target) extend that object behind the same closed-world rules.

## v0 extensions (2026-08, Phase 1 — ticket TCK-P1-003)

Phase 1 extended the closed intent enum with three wallet-read /
wallet-state intents: `get_history`, `get_utxos`, `new_address`. This is a
**backward-compatible extension of v0, not a version bump**:

- Old envelopes remain valid unchanged and `v` stays `0`. Per the bump
  policy in §2, a version bump is reserved for breaking changes (dual-accept
  window, eval-gated cutover); adding enum members and optional params keys
  only widens the accepted set — no previously-valid envelope is invalidated.
- Final params contract:
  - `get_history` → `{}` or `{"limit": int, 1..100}`; omitted `limit` means
    the handler applies its default of 20. Grammar-side syntactic bound is
    1..999 (1–3 digits, no leading zero); the schema layer is the authority
    for 1..100.
  - `get_utxos` → `{}` exactly (same reserved-for-future-opts shape as
    `get_balance`).
  - `new_address` → `{}` or `{"branch": 0|1}`; 0 = receive chain (the
    default the handler applies when omitted), 1 = change chain (rarely
    user-requested, but allowed and documented).
- Grammar, schema, and system prompt moved in lockstep per the
  cross-reference rule: new grammar branches in
  `agent/grammar/envelope.gbnf` (optional keys via whole-object
  alternation, strict key order preserved), params models +
  `INTENT_REGISTRY` in `protocol/envelope.py`, layer-3 rules in
  `protocol/intents.py`, and the prompt intent list + one new few-shot in
  `agent/prompt.py`.
- Handlers and golden eval fixtures land in TCK-P1-004 / TCK-P1-005; until
  then a valid new-intent envelope surfaces `dispatch_error` ("no handler
  registered"), which is the intended fail-closed behavior.

## v0 extensions (2026-09, Phase 2 — tickets TCK-P2-003 / TCK-P2-004)

Phase 2 extended the closed intent enum with the first two steps of the
dispatcher-owned destructive send flow (ADR-0013): `create_tx` and
`confirm_tx`. This is a **backward-compatible extension of v0, not a version
bump**:

- Old envelopes remain valid unchanged and `v` stays `0` — same bump-policy
  reasoning as the Phase 1 extension (§2 reserves a bump for breaking
  changes; adding enum members and required-key params shapes only widens
  the accepted set, never invalidating a previously-valid envelope).
- Final params contract:
  - `create_tx` → `{"recipient": str, 14..100}`, plus EXACTLY ONE of
    `{"amount_sats": int, 546..21_000_000_000_000_000}` |
    `{"amount_usd": number, 0.01..1_000_000}`, plus optional
    `{"fee_target": "fast"|"medium"|"slow"}`. Grammar-side, the amount-pair
    alternation makes emitting both syntactically impossible; the recipient
    meaning-check (testnet witness-v0 P2WPKH per ADR-0008) is layer 3.
  - `confirm_tx` → `{"tx_ref": str, 1..64}` — a reference to the pending
    transaction, quoted verbatim from the confirmation card the flow
    produced; content matching against flow state is the flow's job, never
    the rules'.
- Grammar, schema, system prompt, and the flow moved in lockstep per the
  cross-reference rule: new grammar branches in
  `agent/grammar/envelope.gbnf`, params models + `INTENT_REGISTRY` in
  `protocol/envelope.py`, layer-3 rules in `protocol/intents.py`, the prompt
  intent list + few-shots in `agent/prompt.py`, and the dispatcher-owned
  state machine + confirm gate in `tx/flow.py` (ADR-0013). The two intents
  are one lockstep change because the flow needs both.
- Handler wiring and golden eval fixtures land in TCK-P2-004 / TCK-P2-005;
  until then a valid new-intent envelope surfaces `dispatch_error` ("no
  handler registered"), which is the intended fail-closed behavior.

## v0 extensions (2026-09, Phase 3 — ticket TCK-P3-004)

Phase 3 extended the closed intent enum with the remaining send-flow steps
plus the status lookup (ADR-0013 flow extension): `sign_tx`,
`broadcast_tx`, `tx_status` — registry of ELEVEN. This is a
**backward-compatible extension of v0, not a version bump** (same bump-policy
reasoning as the Phase 1/2 extensions: adding enum members and
required/optional-key params shapes only widens the accepted set, `v` stays
`0`, no previously-valid envelope is invalidated):

- Final params contract:
  - `sign_tx` → `{"tx_ref": str, 1..64}` plus optional
    `{"signer": "file"|"hwi"}` (closed enum; omitted ⇒ the handler applies
    its default signer policy — the signer choice beyond this enum is a
    handler/app decision, never model-chosen; explicit `null` is rejected).
    The device interaction IS the user action: the hardware-wallet screen
    is the trust anchor (PROJECT.md §9), so no chat-utterance gate exists
    at the flow level for signing.
  - `broadcast_tx` → `{"tx_ref": str, 1..64}` — references the SIGNED flow
    record; the flow refuses broadcast unless the state is `SIGNED` with a
    matching reference, and the handler must have completed signed-PSBT
    re-validation (`tx/revalidate.py`) before the chain call — a mismatch
    is a hard stop.
  - `tx_status` → `{"txid": str}` — schema layer admits any string; the
    layer-3 business rule enforces EXACTLY 64 LOWERCASE hex characters
    (strict charset, fail closed, lowercase-only by decision: quoted txids
    stay verbatim-comparable and URL-safe without normalization). This
    user/model-supplied value is interpolated into a request URL path, so
    the charset check IS the injection guard; the GBNF grammar pins the
    identical shape at decode time (`hex_txid ::= [0-9a-f]{64}`).
- Grammar, schema, system prompt, and the flow moved in lockstep per the
  cross-reference rule: new grammar branches in
  `agent/grammar/envelope.gbnf` (signer enum tail optional via whole-tail
  alternation, strict key order preserved; `hex_txid` bounded-repetition
  character class), params models + `INTENT_REGISTRY` in
  `protocol/envelope.py`, layer-3 rules in `protocol/intents.py`, the
  prompt intent list in `agent/prompt.py`, and the `SIGNED`/`BROADCAST`
  flow states in `tx/flow.py` (ADR-0013 amendment: dispatcher-owned
  transitions, matching `tx_ref` from the immediately preceding state, no
  skip paths — broadcast only from `SIGNED`).
- Handler wiring and the full lifecycle land in TCK-P3-005; until then a
  valid new-intent envelope surfaces `dispatch_error` ("no handler
  registered"), which is the intended fail-closed behavior. Golden/redteam
  eval fixtures for the new intents land with TCK-P3-006 (the eval-ship
  obligation is recorded, not discharged, by this ticket).

## v0 extensions (2026-09, Phase 4 / TCK-TX-SELF-001 / TCK-RBF-003)

The ledger continued past Phase 3's eleven: Phase 4 added `node_status`
(twelve), TCK-TX-SELF-001 added `self_transfer` (thirteen), and TCK-RBF-003
added `bump_fee` — **registry of FOURTEEN**. Each is a **backward-compatible
extension of v0, not a version bump** (same bump-policy reasoning as the
Phase 1/2/3 extensions: adding enum members and optional-key params shapes
only widens the accepted set, `v` stays `0`, no previously-valid envelope is
invalidated).

- `node_status` (Phase 4, TCK-P4-003) → `{"params": {}}` exactly — the
  model emits nothing; the dispatcher owns all data (advise-only node
  doctor).
- `self_transfer` (TCK-TX-SELF-001) → `{"mode": "split"|"consolidate"}` plus
  the mode's ONE required key (`parts` for split / `below_size_sats` for
  consolidate) and an optional `fee_target` tail. No address/outpoint/amount
  key is representable — the money plan is engine-derived.
- `bump_fee` (TCK-RBF-003) → `{"target": str, 1..64}` (a 64-hex txid OR the
  app's pending-ref token, carried as a shape-validated string; resolving it
  against store lineage / flow state is the RBF-004/005 handler's job),
  optional `{"funding_ref": str, 1..100}` (a coin reference; semantics are
  the handler's), and AT MOST ONE of optional `fee_target` / `fee_rate_sat_vb`
  (mutually exclusive, mirroring `create_tx`). The model never computes the
  new fee — it only carries a rung or a user-quoted whole-sat rate. Grammar,
  schema, system prompt, drift pins, and eval fixtures moved in lockstep per
  the cross-reference rule; handler wiring lands in RBF-004 (until then a
  `bump_fee` envelope surfaces `dispatch_error` via the not-wired stub).

## v0 amendment (2026-09, TCK-CPFP-001): `self_transfer` gains a `cpfp` mode

CPFP (child-pays-for-parent) — unsticking a stuck **inbound** payment by
spending its unconfirmed output in a fresh high-fee child — is an ADDITIVE
`mode` value on the existing `self_transfer` intent, NOT a new intent: the
**registry stays FOURTEEN**. This is a pure backward-compatible widening
(the same bump-policy reasoning as every extension above): a new enum value
in an existing closed field plus one new optional key. No previously-valid
`self_transfer` envelope changes shape or meaning, so `v` stays `0`.

- `self_transfer.mode` widened `"split"|"consolidate"` → `"split"|
  "consolidate"|"cpfp"`. `cpfp` requires NO number key (`parts` and
  `below_size_sats` are both forbidden for it — the engine resolves which
  unconfirmed inbound coin is stuck, so the model names no coin).
- `cpfp` solely owns one new OPTIONAL key `{"merge_coin": true|false}` — a
  strict JSON boolean (lax bool coercions closed at the schema layer)
  carrying the user's stated wish to merge a SECOND own coin into the child
  (the third reshuffle flavour the council asked for). It steers the child's
  SHAPE only; WHICH coin is deterministic handler policy — no outpoint or
  address is representable, so the params still cannot smuggle an invented
  money value past the confirm gate. `merge_coin` is rejected for `split`
  and `consolidate` (mode↔key pairing, layer 2 + re-checked layer 3).
- `cpfp` shares the `fee_target` tail (no `fee_rate_sat_vb`: an internal
  reshuffle never rides the explicit-rate override — the estimator ladder
  bids the child; the builder's rate parameter is fed handler-side in
  centisat/vB, TCK-FEE-003). The child builder and its honest
  parent-fee-unknown bound live in `tx/cpfp.py` (ADR-0012 amendment).

Grammar (`self-transfer` now alternates `split-params | consolidate-params |
cpfp-params`), schema, system prompt (cpfp routed on INBOUND phrasings and
explicitly distinguished from `bump_fee`, which is for the user's OWN
in-flight transaction), drift pins, and eval fixtures (golden-053..055 +
`selftransfer-bypass-001` re-pinned to try the new mode for a bypass) moved
IN LOCKSTEP. The `self_transfer` handler's cpfp refusal-guard (value-free
"not yet available", mirroring the `_bump_fee_not_wired` stub) + full wiring
ride TCK-RBF-004 (that ticket owns `app.py` in flight). Until they land an
unwired `cpfp` envelope is FAIL-CLOSED — it trips the handler's
consolidate-branch invariant (`assert below_size_sats is not None`) and the
dispatcher's blanket `except Exception` containment surfaces a structured
`dispatch_error`; nothing is ever built, staged, or broadcast — but the
clean early refusal (before the fee-estimator call) is RBF-004's job.


## v0 amendment (2026-09, TCK-CHAT-001): `get_addresses` + `address_number` (referential addresses)

Every address the app SHOWS the user now carries a STABLE wallet-lifetime
number (registry, store schema v4) and a first-shown timestamp, and the
chat can refer to addresses BY NUMBER. This ships one NEW intent and two
ADDITIVE optional params keys — **registry FIFTEEN**, `v` stays `0` (each
is a backward-compatible widening: no previously-valid envelope changes
shape or meaning).

- `get_addresses` → `{}` (the numbered LIST of shown addresses — "what
  addresses have I used?") or `{"address_number": int, 1..9_999_999}`
  (SHOW that one address — "show address 3"). Decision recorded here: the
  ticket's stated PREFERENCE was additive params on existing intents, and
  the referents DO ride existing intents (below); a LIST needs a home of
  its own — no existing intent means "enumerate my addresses", and
  inventing one (e.g. `get_utxos {"list_addresses": true}`) would couple
  two unrelated answers to one handler contract. One small intent is the
  honest shape.
- `get_balance` / `get_utxos` widened `{}` → `{}` OR
  `{"address_number": N}` ("balance of #3", "which coins are on address
  2?"). All THREE intents share one identical params shape
  (`_AddressScopedParams`); the envelope's registry-binding validator keeps
  each intent pinned to its own class. SCOPING DECISION (the ticket asked
  for it documented): the existing handlers CAN answer one-address scope
  honestly — `get_balance` already sums the cached UTXO snapshot and
  `get_utxos` already returns it, so a filter on `address ==` the resolved
  address IS store UTXO truth (no widened network path, no new lookup);
  the handlers were scoped rather than bypassed.
- The `address_number` VALUE is a NUMBER CARRIER ONLY. Grammar admits
  `{"address_number": <1-7-digit int, no leading zero>}`; the schema
  bounds it 1..9_999_999 (transport bound; the grammar's syntactic cap
  MATCHES it here, unlike the limit/fee-rate looser/tighter split, since
  there is no business meaning past "positive"); layer 3 re-checks it.
  NO address is representable in any of the three params shapes — the
  model cannot target, invent, correct, or renumber an address; it quotes
  a FACTS-injected number and the ENGINE re-resolves every one against
  the store registry (schema v4), which is the only number→address
  authority. An out-of-range or unknown number is a value-free clarify
  (`address_ref_unknown`), never a nearest-match and never a whole-wallet
  fallback (a dropped scope silently retargets the user's mental model).
- FACTS: every turn with a non-empty registry injects
  `address_registry: #<n>:<address>:<used|not-used-yet> …` +
  `address_registry_count` (+ an honest `address_registry_note` when the
  bounded injection omits rows — the injection is ROUTING HELP, the
  handler-side lookup is the authority). Registry numbers/labels are
  never fabricated: a number exists only because a surface SHOWED the
  address (new-address line, /receive preview, UTXO listing, watch
  surfacing, or the list itself for activity-seen-but-never-shown rows),
  and the first-shown DATE is store truth the narration never quotes per
  row (one FAQ-style line is its only visible copy).

Grammar (new `get-addresses` branch; `params-balance`/`params-utxos` whole-
object alternations, llama.cpp 0.3.35 dialect), schema, system prompt
(list/referent phrasings route, the number-verbatim rule, three few-shots),
drift pins (grammar conformance ACCEPT/REJECT, registry-completeness 14→15,
prompt intent-membership), and eval fixtures (golden-056..060: list, show-N,
balance-of-N, utxos-of-N) moved IN LOCKSTEP. Prompt compaction ceilings
re-pinned (prompt-context 9500→11500, session-transcript 12700→14500 —
session-length-independent bounds, moved only by the fixed-prompt growth).
The store-migration half (schema v4 `address_registry`) follows the
RBF-001 versioned fail-closed pattern; no previously-valid DB is
reinterpreted, and nothing is back-filled (pre-CHAT-001 addresses get
their number at their NEXT showing — the registry never back-dates a
first-shown event it did not witness).

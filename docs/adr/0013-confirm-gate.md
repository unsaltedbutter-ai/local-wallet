# ADR-0013: Confirm gate — dedicated confirm_tx intent + deterministic utterance gate

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decides:** PROJECT.md §14 OQ3 — "Confirm gate mechanics: dedicated
  `confirm_tx` intent vs yes/no utterance parsing; how to prevent the model
  from confirming on the user's behalf."
- **Scope:** `src/localwallet/tx/flow.py` (flow + gate, TCK-P2-003);
  `protocol/envelope.py` / `protocol/intents.py` / `agent/grammar/envelope.gbnf`
  / `agent/prompt.py` moved in lockstep per ADR-0002 (create_tx + confirm_tx
  are the first two states of the send flow). Consumers: the app wiring
  (TCK-P2-004) and the Phase 3 signer/broadcast states. Relates to ADR-0002
  (envelope v0), ADR-0004 (testnet-only), ADR-0008 (P2WPKH-only send),
  ADR-0012 (tx engine the flow protects).

## Context

PROJECT.md §8.6 fixes the invariant — "Stateful flows are state machines,
not improvisation ... Confirmation requires an explicit user utterance
parsed by the confirm gate — an LLM 'yes' on the user's behalf is invalid" —
but leaves the mechanics open. The candidates: a dedicated `confirm_tx`
intent, free-running yes/no utterance parsing, or letting the model itself
decide that the user has confirmed. The model is small and weak at agentic
judgment (R1), model output is untrusted input (§5.2), and the destructive
surface (build → confirm → sign → broadcast) is exactly where a spoofed
"yes" costs money.

## Decision

### 1. Dedicated `confirm_tx` intent (closed enum, 8 members)

`confirm_tx` joins the intent enum with params `{"tx_ref": str, 1..64}` —
a reference to the pending transaction, quoted verbatim from the
confirmation card the flow produced. Adding two enum members and one
required-key params shape is a backward-compatible v0 extension per the
ADR-0002 bump policy: no previously-valid envelope is invalidated, `v`
stays 0. `create_tx` joins in the same extension (params: `recipient`
string 14..100; exactly one of `amount_sats` int 546..21e15 |
`amount_usd` number 0.01..1e6; optional `fee_target` fast|medium|slow) —
the two intents are one lockstep change because the flow needs both.

### 2. Dual-key rule: envelope AND same-turn utterance gate

Moving the flow CREATED → CONFIRMED requires BOTH of:

1. a valid `confirm_tx` envelope whose `tx_ref` matches the pending
   transaction (necessary — the model proposes), AND
2. a `CONFIRM` classification of the user's utterance from the **same
   turn** by the deterministic gate (co-sufficient — the user decides).

The app layer passes the gate decision into `TxFlow.confirm(...)`; the flow
refuses when the decision is not `CONFIRM` even with a matching `tx_ref`.
The gate is deterministic app code (`tx/flow.py`, `ConfirmGate`), never a
model judgment: the model never parses user intent for destructive steps,
it only relays structure.

### 3. Dispatcher-owned states; no parallel pending transactions

States: `IDLE → CREATED → CONFIRMED`, with `CANCELLED` and `EXPIRED`
terminal; `reset()` returns terminal states to `IDLE`. The flow holds at
most one pending transaction: `create` from `CREATED` is refused
(value-free "already pending — confirm or cancel it first"), so two
destructive flows can never interleave and a `tx_ref` always names exactly
one staged transaction. A stale pending is not silently reaped — `create`
stays refused and `confirm` reports expiry (CREATED → EXPIRED); recovery
is explicit confirm/cancel, never an implicit reset. `reset` from CREATED
is likewise refused: leaving a live pending without an explicit decision
would be the silent step the machine exists to prevent.

### 4. TTL expiry — 10 minutes

A `CREATED` transaction expires when the confirm attempt arrives more than
`PENDING_TTL_S = 600` seconds after `created_at` (module constant; the
boundary second itself is still valid). Rationale: fee/UTXO state drifts
(the confirmation card's numbers go stale), users abandon flows, and an
unbounded pending window turns "confirm" into a hazard. Expiry is
evaluated at confirm time against an injected clock — deterministic and
testable; production supplies wall time. Pending state is also
session-scoped: the flow lives only in memory for the life of the process,
so a pending transaction — and its confirmation — is lost when the app
exits; after a restart the user simply re-creates the send.

### 5. Whitelist-exact utterance classification (no fuzzy matching)

The gate classifies, deterministically:

- **Normalize:** lowercase; split on whitespace; strip ASCII punctuation
  from token edges; drop empty tokens. No tokens ⇒ `NOT_A_DECISION`.
- **Phrase match:** the joined tokens equal a whitelist phrase → CONFIRM
  (`yes`, `y`, `yes please`, `confirm`, `confirmed`, `confirm it`,
  `send it`, `send`, `approve`, `approved`, `do it`) or DENY (`no`, `n`,
  `no thanks`, `cancel`, `cancel it`, `abort`, `stop`, `don't`, `dont`,
  `deny`, `reject`).
- **Token-set match:** with at least one decision token required, every
  token in CONFIRM-single ∪ FILLER (`please`, `the`, `it`, `tx`,
  `transaction`) → CONFIRM; every token in DENY-single ∪ FILLER → DENY; a
  mix of CONFIRM and DENY tokens → `AMBIGUOUS`; any unknown token
  (including `ok`, `ok send`, `probably`) or a filler-only set ("the tx")
  → `NOT_A_DECISION`. Filler alone is never positive evidence:
  confirmation requires at least one whitelisted decision word.

`AMBIGUOUS` is reserved for mixed signals — the only case where both a yes
and a no were literally said — and is treated as "not confirmed"
(fail closed). There is **no** fuzzy/substring/edit-distance matching. The
asymmetry justifies it: a near-match that slips through is a silent
destructive step; a match we miss costs one re-ask. `ok` alone is
deliberately not whitelisted (culturally ambiguous), and any utterance
containing content beyond the whitelists (an address, a question, "yes
bc1q…") is not a decision — the app re-asks.

### 6. The gate lives in app code, not the model

The user-utterance classifier is pure Python in `tx/flow.py`. The model's
prompt guidance (emit `confirm_tx` only on an explicit same-turn
confirmation, quote `tx_ref` verbatim from the card) is advisory; the
enforced discipline is the dispatcher running the gate on the raw utterance
and the flow refusing every other path. Model compliance is an eval
concern; gate enforcement is structural.

### 7. Rejected alternatives

- **Yes/no parsing by the LLM** ("did the user confirm? answer
  yes/no"): puts a destructive decision in the hands of a 2B-class model
  (R1); untestable against adversarial utterances; and the parse result is
  still model output — untrusted input deciding about money.
- **Model-confirmed flows** (no user utterance check; the flow advances
  whenever a valid `confirm_tx` envelope arrives): the model could confirm
  on the user's behalf — the exact failure §8.6 forbids; prompt injection
  via chain data (R8) becomes a broadcast primitive.
- **Inline decision params in `confirm_tx`** (e.g.
  `{"tx_ref": "...", "decision": "yes"}`): the decision would be model
  -emitted text — indistinguishable from the model confirming by itself;
  it adds a second untrusted channel where the deterministic utterance
  gate already exists.
- **Fuzzy classification** (edit distance, embeddings, "ok send" as
  confirm): silently widens the destructive surface; rejected for v0 (see
  §5).

## Consequences

- Phase 2 ships `create_tx`/`confirm_tx` end-to-end up to the confirmed
  state; handler wiring and the live dispatch table are TCK-P2-004. Until
  then, a valid `create_tx`/`confirm_tx` envelope dispatches to
  `dispatch_error` ("no handler registered") — the intended fail-closed
  behavior.
- The grammar, schema, business rules, and prompt moved together
  (ADR-0002 lockstep): grammar branches for both intents (amount-pair
  alternation makes both-amounts syntactically impossible; `recipient`
  precedes the amount; `fee_target` tail optional), strict-int/strict-float
  param validators, layer-3 testnet witness-v0 P2WPKH recipient check with
  value-free failures, and prompt intent-list/few-shot updates.
- Phase 3 will extend the state machine with `SIGNED`/`BROADCAST` behind
  the same discipline: dispatcher-owned transitions, verbatim `tx_ref`
  matching, and — for broadcast — a fresh same-turn gate decision.
- `confirm_tx` business rules validate `tx_ref` shape only (non-empty,
  printable); content matching against flow state is the flow's job, so
  there is exactly one authority for "does this reference name the pending
  transaction".
- **Eval obligation (AGENTS.md working agreement):** golden fixtures for
  `create_tx`/`confirm_tx` and re-adjudication of golden-018 (send-request
  phrasing) land in TCK-P2-005 immediately after handler wiring (TCK-P2-004);
  the P2-005 eval run is the merge gate for this protocol+prompt change.

## Amendment (2026-09, Phase 3 — ticket TCK-P3-004): SIGNED / BROADCAST

Phase 3 extended the flow past `CONFIRMED` with `SIGNED` and terminal
`BROADCAST` (TCK-P3-004), behind the same dispatcher-owned discipline:
`sign`/`broadcast` require the flow in the immediately preceding state with
a matching `tx_ref` quoted from the model's `sign_tx`/`broadcast_tx`
envelope — no skip paths, broadcast only from `SIGNED`. No additional
utterance gate exists at the flow level for signing, deliberately: the
DEVICE interaction IS the user action (the hardware-wallet screen is the
trust anchor, PROJECT.md §9 — the user physically approves the exact
transaction on the device), so an LLM-relayed "the user approved" carries
no decision weight there; what signing structurally requires is the
`CONFIRMED` state, the matching `tx_ref`, and — before broadcast —
completed deterministic signed-PSBT re-validation at handler level
(`tx/revalidate.py`, TCK-P3-005 wiring): a mismatch is a hard stop that
never reaches the broadcast transition. `cancel` remains CREATED-only:
signed means committed to signing; recovery past signing is a fresh flow,
never a silent rewind. Eval fixtures for the three new intents
(`sign_tx`/`broadcast_tx`/`tx_status`) land with TCK-P3-006 per the
AGENTS.md eval-ship obligation.

## Amendment (2026-09-07, UX — ticket TCK-UX-002): re-quote replacement + the approve→sign merge

Grounded in the finalized card-redesign spec
`docs/ux-tx-card-feedback.md` (§2 re-quote, §3 merge). Two changes to the
flow, both **tightening** the one-pending invariant, neither weakening the
dual key. No envelope/schema/grammar change (`fee_target` already exists;
"sign" is gate-vocabulary only — no new intent).

### 1. FLOW-REQUOTE — `create` from `CREATED` is now a dispatcher-owned replacement

Previously (§3) `create` from `CREATED` was refused (`tx_pending`) so two
destructive flows could never interleave. The card's speed offer
("faster"/"slower") needs a same-destination re-quote at a different ladder
rung, which is a `create_tx` arriving while one pends. The refusal is
replaced by a **replacement**, which preserves the invariant more strongly
than a refusal did:

- Still **exactly one** pending transaction — the old record is swapped for
  the new, never two.
- **New `tx_ref`, fresh TTL** (`created_at` re-read): the old reference goes
  inert the instant the record is replaced, so a `confirm_tx` quoting it
  fails the verbatim-match check (§2) — fail closed, the user cannot confirm
  a card that no longer reflects the live numbers (the FINAL-fee invariant:
  a confirmation can only name the fee on the card currently in front of
  them).
- **Commit-only-on-success ordering**: the caller (the `create_tx` handler)
  runs the entire new build — selection, fee math, PSBT — and only replaces
  the staged record if it fully succeeds. A re-quote that pushes the wallet
  short at a higher rung surfaces `insufficient_funds` and the ORIGINAL
  pending survives intact (a refused replacement never discards the live
  one). `CONFIRMED`/`SIGNED`/`BROADCAST` still refuse `create` outright (the
  approved/signed record is committed — abandoning it silently is the step
  the machine exists to prevent).
- The refusal branch is retained for a **different destination** (recipient
  or amount changed) — that stays a `tx_pending` refusal, not a silent
  destination swap.
- A same-rung rebuild (the user answers the offer with "medium"/the current
  target) is a legal no-op-ish replacement: identical numbers, fresh
  `tx_ref`/TTL, offer retired. Ceiling (faster-than-fastest) and floor
  (slower-than-cheapest) answers refuse without replacing; the staged card
  stays live.

### 2. GATE-MERGE — "sign" joins the CONFIRM whitelist; confirm chains the handoff in one turn

The card's ask verb becomes "sign" ("say 'sign' to review it on your
device"), replacing the old two-step "approve, then sign" that read as an
unnecessary extra step. `"sign"` joins `CONFIRM_PHRASES`/`CONFIRM_TOKENS`
(§5); `"confirm"`/`"approve"`/`"send"`/`"yes"` all **remain** valid —
muscle memory preserved, only the card's prompt word changed. On a confirm
that succeeds, the dispatcher runs the sign handoff **in the same turn**
(app-code chaining of two dispatcher-owned states — the model proposed
`confirm_tx`, the chain is deterministic code). What is **not** weakened:

- **The dual key (§2) is untouched.** Chaining runs only after a
  `CONFIRMED` transition that already required the same-turn `CONFIRM`
  utterance AND a matching-`tx_ref` `confirm_tx` envelope. An LLM "yes"
  still confirms nothing; the sign step is the ordinary `sign_tx` handler
  reached by a code-built envelope quoting the dispatcher-owned confirmed
  `tx_ref` (never model- or user-supplied).
- **`CONFIRMED` and `SIGNED` remain distinct states.** Only the user
  prompts merged from two to one; the approved-but-not-yet-exported record
  is still a real, auditable moment (an export/device failure leaves it
  `CONFIRMED`, recoverable by the existing CONFIRMED-"retry" interception).
- **The device screen stays the trust anchor** (§9 / Phase-3 amendment). The
  merged flow does not add a signing-utterance gate — the §3 rationale
  ("the DEVICE interaction IS the user action; an LLM-relayed approval
  carries no weight") is unchanged and is exactly why merging the chat
  prompts is safe: the second chat step gated nothing the device didn't
  already gate. The cooling-off pause it removed is dominated by the device
  review and the separately gated broadcast.
- **Broadcast is still separately gated** — the chain stops at the handoff;
  `sign`→`broadcast` is not chained (a signed transaction reaching the chain
  still needs its own fresh same-turn gate decision, Phase-3 amendment).
  `cancel` stays CREATED-only; the merged turn can still reject on the
  device, and nothing broadcasts without the gated "broadcast".
- **The speed words never confirm.** "faster"/"slower"/"important"/"save"
  and the rest of the offer vocabulary join NO whitelist — not CONFIRM, not
  DENY, not FILLER — so a speed answer classifies `NOT_A_DECISION` and is
  structurally incapable of advancing the flow (pinned in
  `tests/test_tx_flow.py`; the offer line is a wh-question, never a polar
  yes/no one, so a "yes"/"no" cannot answer it with the wrong meaning). The
  only whitelist change in this amendment is admitting "sign" to CONFIRM.

`/details` (the deep card view) rides the ADR-0020 transcript-command
channel — a deterministic UI command, not an intent, and the spoken word
"details" is deliberately NOT whitelisted (unknown token ⇒ never a
decision). Eval fixtures for the merged flow (`golden-031` "sign" while a
tx pends ⇒ `confirm_tx`; `golden-032`/`033` the importance→`fee_target`
mapping; `golden-034` "sign" with nothing pending ⇒ never a fabricated
`confirm_tx`; `confirm-bypass-007` "faster" ⇒ not a confirm) ship with this
change per the AGENTS.md eval-ship obligation.

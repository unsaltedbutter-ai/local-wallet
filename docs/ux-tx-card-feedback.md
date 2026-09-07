# UX proposal: confirmation-card redesign (create_tx / confirm_tx)

- **Status:** PROPOSAL ONLY — no implementation during MW-4. `app.py` is
  hands-off until the MW-4 live run lifts; every change below is a code
  ticket filed against post-MW-4.
- **Source:** mid-MW-4 live-run user feedback: "too verbose in one way and
  not enough options in another — 1. briefer summary, 2. speed up / slow
  down options, 3. 'approve' then 'sign' feels like an unnecessary step."
- **Grounded in:** PROJECT.md §9/§10, ADR-0013 (+ Phase 3 amendment),
  `app.py` card renderer / narration strings / gate wiring (read-only
  survey), `chain/fees.py` (fast/medium/slow ladder), `chain/eta.py`
  (hedge wording).

## 0. Diagnosis (what the user actually saw)

The code-rendered card (`_print_confirmation_card`) is already fairly tight.
The wall of prose in the user's screenshot ("Tell me if you want to…
Or if you can… Or…") is the **model's own `respond` narration** on top of
the card. Two consequences:

1. **Verbosity is a narration problem, not a renderer problem.** The card
   body is code-owned; the options menu the user complained about is
   model-spilled. The fix is: the card itself carries a short, code-owned
   options line, and the prompt tells the model to add at most one lead-in
   sentence after `create_tx`.
2. **The model offered options the flow cannot honor.** "Increase fee" while
   `CREATED` dead-ends today: `create_tx` is refused (`tx_pending`), there
   is no cancel intent (cancel is the gate's DENY path), and "show me more
   details" is model-invented with no backing affordance. Free options that
   can't execute are worse than no options — they train the user to say
   words the machine refuses.

## 1. Brief default card

### Default view (code-rendered, replaces today's ten-line card)

```
Pending — say "sign" to review it on your device, or "cancel" to discard.
To:    bc1qunjgxclqzzx6qrw7qgsjrxa2lz0qmzntn34lu8
Pay:   50,000 sats (~$39.66 @ $79,325/BTC · rate 12s old)
Fee:   141 sats · 1 sat/vB · slow — ETA ~60-70 min (estimate only, not a guarantee)
Want it faster or slower? Say "faster" or "slower" · full breakdown: /details
```

**Stays in default** (§10 non-negotiables): recipient **full address**
(never truncated), amount with **dual units + rate age/staleness markers**
(existing `usd_cents`/`rate_age_s`/`rate_stale` mechanism — keep verbatim),
fee in sats (absolute cost) **and** sat/vB with the speed word, and the ETA
with the **existing hedge string verbatim** from `chain/eta.py`
("estimate only, not a guarantee"). One obvious confirm affordance, cancel
always named. The ask line is the ONLY place the card tells the user what
to do.

**Moves behind "details"** (reprinted on demand, still code-rendered from
the same handler result — nothing recomputed, nothing model-generated):
Size (vB), Inputs count, Change, Expires countdown, full `tx_ref`, and the
target label duplicated in the fee line. These matter for verification,
not for the decision — §10 lists them as card contents; this proposal
**demotes rather than deletes** them. If the orchestrator wants strict §10
conformance instead, keep them on the default card and cut the model
narration only (that alone removes ~60% of what the user saw).

Implementation note: `/details` rides the existing ADR-0020 transcript-command
channel (like `/export`, `/scrub`) — a deterministic UI command, NOT a model
intent, and no protocol surface. The REPL caches the last full card render
while `CREATED` and reprints it. "details" as a spoken word is deliberately
NOT whitelisted by the gate (unknown token ⇒ `NOT_A_DECISION` ⇒ can never
confirm).

Thousands-separators on sats are display formatting only (same class the
renderer docstring already allows: integer formatting of result values);
values themselves stay verbatim from tool output.

### Prompt guidance change (lockstep)

After a `create_tx` success the model emits at most one lead-in line (or an
empty `respond`); the card carries all options. This kills the model-spilled
menu and any future dead-end offers.

## 2. Fee-speed options: "faster" / "slower" as first-class

### What each does

`create_tx` already carries `fee_target: fast|medium|slow` in the closed
grammar — **no envelope schema change is needed.** A speed request is a
**re-quote**: the dispatcher replaces the pending transaction with a fresh
`create_tx` at the adjacent ladder rung (slow→medium→fast /
fast→medium→slow), same recipient and amount quoted from the pending
record (never re-derived from user text), fresh fee/size/ETA/`tx_ref`/TTL,
and the flow returns to `CREATED` with a **newly rendered card**.

This requires one deliberate flow-policy change (today: `create` from
`CREATED` is refused, `tx_pending`). The refusal exists to keep
"at most one pending, no interleaved destructive flows" — a **replacement**
preserves that invariant even harder: there is still exactly one pending,
the old `tx_ref` is invalidated, and any `confirm_tx` quoting the stale ref
fails the verbatim-match check (fail closed). Ticket below.

### The MUST NOT change list (ADR-0013 preserved)

- A speed utterance is never a confirmation. "faster"/"slower"/"increase
  fee" contain no whitelisted decision token ⇒ gate `NOT_A_DECISION` on
  that turn ⇒ the flow cannot advance past `CREATED` that turn.
- Confirmation is only valid **against the card showing the final fee**:
  the re-quote re-renders the card and resets the TTL; the user's next
  CONFIRM-classified utterance is classified from the *same turn* as the
  confirm envelope, per the dual-key rule.
- The prior turn's gate decision cannot leak: `SendSession.gate_decision`
  is recomputed at the top of every turn.
- New `tx_ref` per re-quote; old refs are inert.

### Copy

- Re-quote accepted (card reprinted, one lead line):
  `Re-quoted at the faster rate — review the new fee below:`
- Already fastest: `That's already the fastest recommended rate (next-block
  target). Say "sign" to proceed or "cancel" to discard.`
- Already cheapest: `That's already the cheapest recommended rate — we never
  quote below the network minimum. Say "sign" to proceed or "cancel" to
  discard.`
- If a re-quote pushes the wallet short of funds at the higher fee, the
  existing `insufficient_funds` line surfaces (value-bearing user-facing
  amounts per ADR-0012 — unchanged) and the ORIGINAL pending stays intact
  (replacement commits only on success — this ordering must be in the
  ticket: validate the new build BEFORE discarding the old pending).

Keep the three-rung ladder; do not expose arbitrary sat/vB ("set the fee to
3") — that WOULD need a grammar/schema change and hands the user a number
we'd have to re-derive dust/min-relay math against. The ladder's rung
numbers are real (estimator-sourced), which is what §10's "present speed
options with real numbers" asks for.

## 3. approve → sign: verdict = MERGE (one utterance)

### Why the two-step exists, and what it actually protects

Today: `approve`/`confirm` (gate CONFIRM + `confirm_tx` envelope, dual-key)
→ `CREATED→CONFIRMED`; then `_CONFIRMED_LINE` asks for "sign" →
`sign_tx` → `CONFIRMED→SIGNED`, device handoff. The ADR-0013 Phase 3
amendment is explicit that **signing has no utterance gate by design**: the
device interaction IS the user action (hardware screen is the trust anchor,
§9), and broadcast still demands its own fresh same-turn gate decision.

So the second chat step ("sign") gates nothing the device doesn't already
gate. Its only safety value is a cooling-off pause between "I approve the
numbers" and "put this in front of my device" — but the user can reject on
the device, nothing reaches the chain without a separately gated
"broadcast", and the signed PSBT re-validation is a hard stop regardless.
The cooling-off is real but tiny; the friction (and the confusing
"approve, then sign, but also 'sign' was on the card?" incoherence the user
hit) is large. **Merge.**

### The merged shape

- The card asks once: **`say "sign" to review it on your device, or
  "cancel" to discard.`** "sign" is the lead verb because it names what
  actually happens next (handoff to device), not a rubber stamp.
- Gate change: `"sign"` joins the CONFIRM whitelist (deterministic,
  phrase + token-set rules unchanged). `"confirm"`, `"approve"`, `"send"`,
  `"yes"` etc. **remain valid** — same decision, muscle memory preserved;
  only the card's prompt word changes.
- On confirm success the dispatcher runs the sign handoff **in the same
  turn** (app-code chaining of two dispatcher-owned states — the model
  proposes `confirm_tx`; the chain is deterministic code, not an LLM
  "yes"). The user sees the existing §10 device-handoff narration directly
  ("Check your device — compare the address and amount on its screen, then
  approve there."), skipping `_CONFIRMED_LINE` entirely.
- `CONFIRMED` and `SIGNED` remain distinct states in `TxFlow` — the state
  machine keeps its shape (an approved-but-not-yet-exported record is a
  real, auditable moment); only the *user prompts* merge from two to one.
- Nothing weakens: dual-key confirm intact; an LLM `sign_tx` envelope while
  `CREATED` still refuses (state gate); device review unchanged; broadcast
  still separately gated; re-validation hard stop unchanged.

### Copy for the merged flow

| Moment | String |
|---|---|
| Card ask line | `Pending — say "sign" to review it on your device, or "cancel" to discard.` |
| Device handoff (hwi) | `Sending to your device — compare the address and amount on its screen, then approve there. Come back and say "broadcast" when it's done, or "cancel" is no longer available after signing — reject on the device to abandon it.` *(final clause: ticket should decide; the CREATED-only-cancel boundary must stay discoverable)* |
| Still-pending guidance | `Still pending — say "sign" to send it to your device, or "cancel" to discard it.` |
| Ambiguous guidance | `That was ambiguous — say "sign" to proceed with the pending transaction, or "cancel" to discard it.` |

Caveat to the eval ticket: when the user says "sign" while `CREATED`, the
model must emit `confirm_tx` (not `sign_tx`) — prompt guidance plus a
golden fixture; a wrong envelope fails closed at the flow.

## 4. Protocol impact — what the 12-intent registry absorbs

**Absorbed with zero envelope/grammar change:**

- "faster"/"slower" → existing `create_tx` + `fee_target` param (flow
  policy + prompt only).
- "sign"-as-confirm → gate whitelist (app code, `tx/flow.py`) + prompt.
- "details" → `/details` transcript command (ADR-0020 channel), no intent.
- Merged confirm+sign → dispatcher chaining, no new intents.

**Needs a separate envelope-schema ticket (NOT recommended now):**
arbitrary fee-rate input (`{"fee_rate_sat_vb": n}` on `create_tx`) —
grammar, pydantic, business rules, prompt, and evals in lockstep. The
three-rung ladder covers the feedback; skip until a real request needs it.

### Code tickets to file (post-MW-4)

1. **CARD-BRIEF** — trim `_print_confirmation_card` to the default view;
   cache full render; `/details` command; prompt rule "≤1 lead-in line
   after create_tx". Strings only + small REPL change.
2. **FLOW-REQUOTE** — allow dispatcher-owned replacement `create_tx` from
   `CREATED` (new `tx_ref`, TTL reset, commit-only-on-success ordering);
   prompt guidance + eval fixtures for faster/slower; floor/ceiling copy.
   Ships with an **ADR-0013 amendment note** (replacement policy) — the ADR
   itself is orchestrator-owned; this doc does not amend it.
3. **GATE-MERGE** — add "sign" to the CONFIRM whitelist; same-turn
   confirm→sign chaining in the app wiring; updated guidance strings;
   "sign while CREATED ⇒ confirm_tx" eval fixture. Also carries an
   ADR-0013 amendment note (whitelist membership + merged user step).

## Appendix: string block (structured for i18n)

```
card.ask                = Pending — say "sign" to review it on your device, or "cancel" to discard.
card.speed_hint         = Want it faster or slower? Say "faster" or "slower" · full breakdown: /details
card.requote_lead       = Re-quoted at the {faster|slower} rate — review the new fee below:
card.rate_ceiling       = That's already the fastest recommended rate (next-block target). Say "sign" to proceed or "cancel" to discard.
card.rate_floor         = That's already the cheapest recommended rate — we never quote below the network minimum. Say "sign" to proceed or "cancel" to discard.
card.line_amount        = Pay: {sats} sats (~${usd} @ ${btc_rate}/BTC · rate {age}s old{stale? " · stale"})
card.line_fee           = Fee: {fee_sats} sats · {rate} sat/vB · {target_word} — ETA {eta_wording}
details.line_extra      = Size: {vsize} vB · Inputs: {n} · Change: {change} · Expires: ~{min} min · Ref: {tx_ref}
guide.still_pending     = Still pending — say "sign" to send it to your device, or "cancel" to discard it.
guide.ambiguous         = That was ambiguous — say "sign" to proceed with the pending transaction, or "cancel" to discard it.
```

`{eta_wording}` is the verbatim `chain/eta.py` output including the hedge —
never reworded by the model. `{target_word}` is fast/medium/slow prose
(next-block / ~30-min / ~1-hour), also code-supplied. Absent values render
`unavailable` (existing fail-closed rule — never a fabricated "0").

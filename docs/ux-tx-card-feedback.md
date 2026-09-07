# UX proposal: confirmation-card redesign (create_tx / confirm_tx)

- **Status:** PROPOSAL ONLY — no implementation during MW-4. `app.py` is
  hands-off until the MW-4 live run lifts; every change below is a code
  ticket filed against post-MW-4.
- **Source:** mid-MW-4 live-run user feedback: "too verbose in one way and
  not enough options in another — 1. briefer summary, 2. speed up / slow
  down options, 3. 'approve' then 'sign' feels like an unnecessary step."
  Item 2 has since been escalated by the user into the primary requirement
  of §2.0, with an explicit trigger condition (quoted verbatim there).
- **Grounded in:** PROJECT.md §9/§10, ADR-0013 (+ Phase 3 amendment),
  `app.py` card renderer / create_tx handler / gate wiring (read-only
  survey), `protocol/envelope.py` (`fee_target` optionality) and
  `tx/flow.py` (`ConfirmGate` whitelists), `chain/fees.py` (fast/medium/slow
  ladder), `chain/eta.py` (hedge wording).

## 0. What the user actually saw (provenance, third time right)

**Third-pass correction (2026-09-07), direct from the user.** The two
earlier passes — each fed the same wrong provenance through the
orchestrator — analyzed a "menu" text ("…increase fee… decrease fee… show
me more details… when you approve I'll send to your hardware wallet…") as
if it had appeared around the card, and diagnosed a model-narration spill
needing a prompt cap. **That diagnosis is wrong and every trace of it is
removed: there is no spill problem.** At the `create_tx` turn the app
renders exactly one thing — the nine-line card quoted below — and the
"menu" text was the user's own proposal sketch of the UX they *want*.

Renderer output (`_CARD_HEADER_LINE` + `_print_confirmation_card`,
`app.py` — the actual card, verbatim):

```
Pending transaction — review it carefully, then say 'confirm' or 'cancel':
Amount: 50000 sats ($39.66 · rate age 0s)
To: bc1qunjgxclqzzx6qrw7qgsjrxa2lz0qmzntn34lu8
Fee: 141 sats (1 sat/vB, medium target)
Size: 141 vB
Inputs: 1
Change: 262359 sats
Expires: ~10 min
ETA: ~60-70 min — estimate only, not a guarantee
Ref: 7eabb444f7c045d49779199af2cf325f
```

What the corrected facts change — the diagnosis, not the design:

1. **The sketch is a requirements list, not a bug report.** Every option it
   names maps onto an affordance this doc proposes: "increase fee /
   decrease fee" → §2 (proactive offer + re-quote mechanics, FLOW-REQUOTE);
   "show me more details" → `/details`; "when you approve I'll send to your
   hardware wallet" → the approve→sign seam the §3 merge closes. §1's
   brief card stands on the "briefer summary" half of the feedback and no
   longer needs any anti-spill rationale.
2. **Dead-end rule (now a design rule, not an observed bug).** Anything the
   card offers must be executable in the flow's current state: "faster /
   slower" is card-worthy only together with FLOW-REQUOTE (today
   `create_tx` from `CREATED` refuses `tx_pending`); cancel works today
   (it is the gate's DENY path); arbitrary fee-rate entry does not exist
   and is deliberately not offered (§2.3).
3. **The two second-pass field corrections stand:** (a) the `Fee:` line
   already carries sats + sat/vB + the speed word, and `ETA:` already
   carries the `chain/eta.py` hedge verbatim — the §1 merge is one renderer
   concatenation of existing values, not new copy; (b) the first-pass
   example card's `@ $79,325/BTC` rate anchor does **not** exist in the
   handler result (it carries `usd_cents`, `rate_age_s`, `rate_stale`,
   `rate_fetched_at`, and no rate value) — the explicit anchor stays
   deferred (§1).
4. **New primary requirement (user verbatim):** *"We want to let the user
   tell us this transaction is important or not important — to spend more
   money to speed it up or slow it down to save money. We should offer this
   option if the user hasn't given us an indication that the transaction
   can be slow or should be fast."* Designed as the proactive speed offer,
   §2.0.

## 1. Brief default card

### Default view (code-rendered, replaces today's header + 9-line card)

The card's last line is **conditional — one slot, two variants, never both
at once** (offer mechanics: §2.0):

**Variant A** — fresh card, user expressed no speed preference (the offer
fires):

```
Pending — say "sign" to review it on your device, or "cancel" to discard.
To:    bc1qunjgxclqzzx6qrw7qgsjrxa2lz0qmzntn34lu8
Pay:   50,000 sats ($39.66 · rate age 0s)
Fee:   141 sats · 1 sat/vB × 141 vB · medium — ETA ~60-70 min — estimate only, not a guarantee
From:  your wallet (1 source) · 262,359 sats come back as change
How important is this one? Say "faster" to confirm sooner (a slightly higher fee) or "slower" to save money (it may take longer) — or say "sign" to keep this rate · full breakdown: /details
```

**Variant B** — a preference is already known (the user's own request
carried one, or the user answered the offer): the card stops asking:

```
Pending — say "sign" to review it on your device, or "cancel" to discard.
To:    bc1qunjgxclqzzx6qrw7qgsjrxa2lz0qmzntn34lu8
Pay:   50,000 sats ($39.66 · rate age 0s)
Fee:   268 sats · 2 sat/vB × 134 vB · fast — ETA ~10-20 min — estimate only, not a guarantee
From:  your wallet (1 source) · 262,232 sats come back as change
full breakdown: /details
```

(Variant B's numbers are illustrative post-"faster" re-quote values —
§2.1: coin selection re-runs, so fee/vsize/change/ETA all refresh together.)

Four data lines + ask line + one conditional tail, rendered from the same
handler result — nothing recomputed, nothing model-generated. The ask line
is where the card names the primary action (it replaces
`_CARD_HEADER_LINE`; the re-show path at the `tx_pending` refusal prints
the same card under the still-pending guide line). The offer tail presents
fee-tune alternatives that loop back to the **same** ask line after a
re-quote — §10's one obvious confirm affordance stays singular, and the
card never grows two competing menus: the offer *replaces* the earlier
draft's unconditional "Want it faster or slower?" hint, which was wrong in
both directions (it pitched a choice to users who had already made one,
and stayed silent when the app should proactively ask).

### Field-by-field mapping against the REAL nine lines

| Real card line | Verdict | What remains (merge/move, not re-propose) |
|---|---|---|
| `Amount:` 50000 sats ($39.66 · rate age 0s) | **STAYS** | sats + USD + `rate age Ns` + ` · stale` marker all kept verbatim from the existing `usd_cents`/`rate_age_s`/`rate_stale` mechanism. Only formatting-class changes (thousands separators). See the rate-age note below for why "rate age 0s" is NOT noise. |
| `To:` full address | **STAYS** | §10 verbatim: never truncated mid-hash, copyable. |
| `Fee:` 141 sats (1 sat/vB, medium target) | **STAYS — absorbs Size + ETA** | It already carries absolute sats, sat/vB, and the speed word; the remaining work is a renderer concatenation of values already in the same result dict (`vsize`, `eta_wording`), not new copy. |
| `Size:` 141 vB | **STAYS, folded into Fee** | §10's card list explicitly names **size** — the first pass demoted it on a wrong reading of §10 (corrected below). It rides the fee line as `1 sat/vB × 141 vB`: the fee/size relationship is exactly why §10 wants size on the card. |
| `Inputs:` 1 | **STAYS, rephrased on a "From:" line** | §10 requires a "source of funds summary" — `Inputs: 1` is that item, today rendered as unexplained jargon (a §10 tone violation in its own right). The brief line reads `From: your wallet (1 source) · …`; the raw `Inputs:` count survives verbatim in /details. |
| `Change:` 262359 sats | **STAYS, merged onto the "From:" line** | First pass said demote; the real §10 wording (source-of-funds summary) flips that verdict. The honest UX reason: one 312,500-sat coin is consumed and a 262,359-sat change coin comes back (the balance itself only drops by amount + fee) — without the change line, "where did my big coin go?" has no answer on the card. "262,359 sats come back as change" is reassurance, not detail. |
| `Expires:` ~10 min | **DEMOTES** | Not in §10's list. On a fresh card it is the constant TTL (600 s → always "~10 min") — zero per-card information. It only carries information on a re-show, where `_tx_pending_result` recomputes true remaining TTL — and that is the still-pending guide + /details moment, exactly where it belongs. |
| `ETA:` ~60-70 min — estimate only, not a guarantee | **STAYS, merged into Fee** | Hedge wording already verbatim from `chain/eta.py`; nothing to write, only to concatenate onto the fee line. |
| `Ref:` 7eabb444… | **DEMOTES — honest call** | Not in §10's list, and not security-relevant on the brief: the dual-key check matches the envelope's `tx_ref` against the dispatcher's record, and the model quotes the ref from FACTS, never from the card (HANDOFF §3.7 — the card is terminal-only; the model cannot see it). The user never reads or types it, and with at-most-one-pending no confirm can point at the wrong card. The moments a HUMAN needs it — matching the ADR-0014 `unsigned-<ref>…` transfer filenames, quoting an expired ref in an error — are confirm/sign/broadcast time, where the narration re-prints the ref then anyway (audit symmetry already in those results; CARD-BRIEF ticket must pin that the sign-time re-print survives). A 32-hex line on a decision card is pure noise. |

**Rate-age note (`"rate age 0s"` reads like noise?):** keep it on the
brief line. That clause IS §10's "rate timestamp" duty for the dual-units
rule — the freshness promise matters most at the moment money is
committed, and demoting it to save twelve characters breaks the rule
literally. On a fresh create the oracle is cache-fresh so it reads `0s`;
that is the honest answer, not noise. If the orchestrator wants a softer
read, the change is formatting-only copy (`· rate just fetched` under a
60 s threshold) with the number always shown at or above it — a
ticket-level wording decision; the default proposed here keeps the
verbatim age.

**Fee-line honesty:** the example keeps `eta_wording` verbatim, so the
merged line carries two em-dashes ("… — ETA ~60-70 min — estimate only,
not a guarantee"). If the orchestrator dislikes the double dash, that is
a `chain/eta.py` wording ticket — the renderer must not re-punctuate the
hedge (verbatim rule), and today's separate ETA line carries the same
wording for exactly that reason.

**§10 correction to the first pass:** the earlier draft claimed "§10 lists
size/inputs/change/expiry/ref as card contents." It does not. §10's
confirmation-card sentence verbatim: *"amount, recipient (full address,
copyable — never truncated mid-hash), fee, size, ETA, source of funds
summary. One obvious confirm affordance; cancel is always available."*
Ref and Expires are absent from that list (their demotion is on firmer
ground than the first pass claimed); size and a source-of-funds summary
are present (which is what flips Size/Inputs/Change from demote to
merge-as-visible). The demote-rather-than-delete principle is unchanged:
**/details reprints the full original nine-line card verbatim** (the
cached render), so strict §10-conformance and this brief view are the
same data at two depths.

**Re-show behavior (existing, unchanged here):** the `tx_pending`
re-render sends `usd_cents=None`, so the USD/rate-age parenthetical drops
out entirely — the accepted sats-only degrade. With the USD segment gone
there is no rate timestamp to show, which is consistent, not new. The
re-show also renders the offer slot as variant B (§2.0 — deliberate: the
offer is one-shot; the cached /details reprint is of the variant-B card
plus ref/expires lines).

Implementation note: `/details` rides the existing ADR-0020 transcript-command
channel (like `/export`, `/scrub`) — a deterministic UI command, NOT a model
intent, and no protocol surface. The REPL caches the last full card render
while `CREATED` and reprints it. "details" as a spoken word is deliberately
NOT whitelisted by the gate (unknown token ⇒ `NOT_A_DECISION` ⇒ can never
confirm).

Thousands-separators on sats are display formatting only (same class the
renderer docstring already allows: integer formatting of result values);
values themselves stay verbatim from tool output.

### Narration note (retracts the earlier passes' prompt cap)

The earlier passes' rule "cap the model at one lead-in sentence after
`create_tx`" existed to stop a narration spill that never happened —
**withdrawn**. Model narration after a create result stays as ordinary
chat text; the design hedge is entirely code-side: every decision
affordance (sign, cancel, faster, slower, /details) is printed on the
card, so the user is never dependent on narration to know their options.
The only prompt change this doc needs is the `fee_target` guidance in
§2.0 — an instruction about an explicit parameter, not a length cap.

## 2. Fee speed: the proactive offer and the faster/slower re-quote

### 2.0 The proactive offer (primary requirement)

**Trigger rule, as implementable logic:**

```
show offer  ⇔  the create_tx envelope that staged the pending tx
               carried NO params.fee_target  (the user's request named no
               speed/importance preference)
```

If the user's send request said "ASAP", "no hurry", "make it fast",
"cheap" — anything expressible as `fee_target` — the envelope carries it
and the card renders variant B: **never ask someone who already
answered**. Once the user answers the offer, the re-quote envelope carries
an explicit `fee_target`, so the offer retires for the rest of that
transaction. One-shot by construction, no new state.

**Code reality check (read-only survey, today).** The omitted-vs-explicit
distinction does not survive to render time: `app.py:1257` collapses an
absent param into the default
(`target = FeeTarget(params.fee_target) if params.fee_target else FeeTarget.MEDIUM`)
and `TxFlow.create` stores only the resolved string — by the time the card
renders, `fee_target` reads `"medium"` whether the user chose it or the
code defaulted it. (`PendingTx.fee_target` is typed `str | None` and
`_eta_for` has a `None` branch, but the create handler never passes
`None` — that branch is defensive only.) The fix is one display-only
handler-result key — `fee_target_defaulted: params.fee_target is None` —
same class as the deferred rate-anchor addition: a result-dict value,
**not** an envelope change. `create_tx.fee_target` already exists
(optional `fast|medium|slow`, GBNF + pydantic both closed over it), so
schema, grammar, and contract v0 stay untouched.

**Re-shows.** `_tx_pending_result` builds the card from the flow record,
which cannot know the default came from absence — so re-shows render
variant B. Deliberate: the offer is a proactive one-shot on the fresh
card; unrecognized chatter should not re-pitch it. If product later wants
the offer to survive re-shows, the upgrade is one frozen bool on
`PendingTx` (+ a `create()` kwarg) — not worth it now.

**Prompt additions (no schema change).** `agent/prompt.py` today lists
`fee_target` as an optional param but gives the model no guidance on when
to set it — the trigger would silently misfire on model guessing. Add:
(1) set `fee_target` explicitly ONLY when the user states a
speed/importance preference (`fast` for "ASAP / important / hurry it",
`slow` for "no hurry / save money / can wait"); otherwise omit it — never
guess; (2) a speed change on a pending transaction is a fresh `create_tx`
with `fee_target` explicit. Both ship with eval fixtures under the
FLOW-REQUOTE ticket. This is intent extraction only — the ladder rates
still come from the estimator; the model never touches money logic.

**Shape: an offer line on the card, not a pre-card question.** Three
reasons:

1. **Numbers first.** The offer only makes sense beside the real fee and
   ETA the create just produced ("1 sat/vB · medium — ETA ~60-70 min");
   the user trades concrete sats for concrete minutes. A pre-card
   question has no numbers to show — §10's "present speed options with
   real numbers" is impossible before the transaction exists.
2. **Zero forced friction.** The ask line is valid immediately: a user
   with no preference just says "sign" — the displayed medium IS the
   "no preference" answer. A pre-card question inserts a mandatory turn
   into every send (the requirement says *offer*, not *interrogate*) and
   delays reviewing the recipient/amount — the review that actually
   protects the user.
3. **One decision surface.** A pre-card question would live in chat
   narration — skippable, deniable text; the card is code-owned, printed,
   and guaranteed present while `CREATED`. The answer path is the same
   envelope slot either way (`create_tx` + `fee_target`), so the question
   buys nothing the card line doesn't already do.

**Framing decision: speed words, importance prose.** The spoken affordance
words are `faster` / `slower` — short, one-to-one with `fee_target`,
describing exactly what the machine does (moves a confirmation target on
the ladder), and structurally adjacent to nothing in the gate vocabulary
(§2.2). "Important" is the user's *question*, not the machine's *action*,
so it frames the offer line (the user's own framing, §10's
knowledgeable-friend tone) and is handled by prompt mapping — but is not
demanded as a magic word ("important", "not important", "this is urgent"
all map; the user can't get the mapping wrong by forgetting the exact
noun). "No preference" gets **no spoken word of its own**: "sign" (or
"confirm"/"yes" — muscle memory preserved) already IS the keep-this-rate
answer, and inventing a third keyword for an action that exists is one
more thing to forget. §10 plain-UI duty applied: each option is named
with its consequence — sooner costs more, cheaper waits longer — never
with the parameter name; "fee target", "sat/vB ladder", and "estimate"
mechanics stay off the offer line (the fee line above already carries the
real numbers, and the ETA line already carries the hedge).

**Offer copy:**

| Moment | String |
|---|---|
| Card tail, variant A (offer) | `How important is this one? Say "faster" to confirm sooner (a slightly higher fee) or "slower" to save money (it may take longer) — or say "sign" to keep this rate · full breakdown: /details` |
| Card tail, variant B (settled) | `full breakdown: /details` |

Build-order note: variant A's keep-word is "sign" because the proposal
assumes the §3 GATE-MERGE; if CARD-BRIEF ships first, the keep-word is
"confirm" (whitelisted today, and the current header's word) — a
one-token swap in `card.offer`.

### 2.1 What each choice does — FLOW-REQUOTE mechanics

`create_tx` already carries `fee_target: fast|medium|slow` in the closed
grammar — **no envelope schema change is needed.** A speed answer is a
**re-quote**: the dispatcher replaces the pending transaction with a fresh
`create_tx` at the ladder target (from the card's own current target:
medium→fast on "faster", medium→slow on "slower"), same recipient and
amount quoted from the pending record (never re-derived from user text),
fresh fee/size/ETA/`tx_ref`/TTL, and the flow returns to `CREATED` with a
**newly rendered card** — now variant B, because the re-quote envelope
carries `fee_target` explicitly (that is what retires the offer).

This requires one deliberate flow-policy change (today: `create` from
`CREATED` is refused, `tx_pending`). The refusal exists to keep
"at most one pending, no interleaved destructive flows" — a **replacement**
preserves that invariant even harder: there is still exactly one pending,
the old `tx_ref` is invalidated, and any `confirm_tx` quoting the stale ref
fails the verbatim-match check (fail closed). Ticket below.

**Cross-check against the REAL card's fields.** A re-quote replaces every
line on the card except Amount and To: coin selection re-runs at the new
fee rate, so `fee_sats`, `fee_rate_sat_vb`, `vsize` (the fee line's
`× 141 vB` segment), `inputs_count` and `change_sats` (the "From:" line),
the `eta_*` trio, the new `tx_ref` and the reset `expires_in_s` all come
fresh from the new create_tx result — the merged brief lines draw from
the same dict, so no extra plumbing. The ladder move reads the card's own
`fee_target` (on the quoted card: `medium` — "faster"→fast, "slower"→slow;
the variant-A offer applies to exactly this defaulted-medium case).
Amount and To stay pinned to the pending record, quoted never re-derived.
A higher rung pushing the wallet short: existing `insufficient_funds`
line, original pending intact (commit-only-on-success ordering,
unchanged).

### 2.2 Gate safety under the offer (ADR-0013 dual key, preserved)

The MUST NOT change list:

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

**Collision audit — checked word-by-word against the real `ConfirmGate`
whitelists (`tx/flow.py`).**

1. **No offer word collides.** Every candidate answer word — fast, faster,
   slow, slower, speed, hurry, rush, important, save, money, cheap, asap,
   medium — is absent from `CONFIRM_PHRASES`/`CONFIRM_TOKENS`,
   `DENY_PHRASES`/`DENY_TOKENS`, and `FILLER_TOKENS`, so each classifies
   `NOT_A_DECISION`: structurally incapable of moving the flow. "fast" and
   "slow" do live in the `fee_target` enum vocabulary (envelope +
   `chain/fees.py`), but the gate never inspects envelope params and the
   model never inspects gate token sets — the two closed sets are disjoint
   **by layer**, not by luck. No collision.
2. **Mixed answers fail closed.** "no hurry" touches the DENY token "no"
   but hits the early `NOT_A_DECISION` return on the unknown token
   "hurry"; "yes faster" likewise (CONFIRM hit + unknown "faster"). The
   app re-asks. This is ADR-0013's fail-closed asymmetry working, not a
   bug — but it means users must say a clean option word for anything to
   happen, which is exactly what the offer line spells out.
3. **The one live collision is inbound through the copy.** "yes" / "y"
   are gate CONFIRM and "no" / "n" / "no thanks" are gate DENY. A
   yes/no-shaped offer would get "answered" by a gate word with the
   precisely wrong meaning: "Is this important?" → "yes" (meaning *hurry
   it*) would **confirm at the default fee**; "Do you want it faster?" →
   "no" (meaning *let it wait*) would **cancel the transaction**. Hence a
   hard copy rule: **the offer line must never be a polar (yes/no)
   question.** The shipped line is a wh-question that cannot be answered
   "yes"/"no" plus three named words, and each named path lands correctly:
   "faster"/"slower" → re-quote (never a confirm, by 1); "sign" (or the
   keep-word) → confirm at the fee displayed on the card the user is
   reading — the FINAL-fee invariant, satisfied by construction because a
   re-quote invalidates the old `tx_ref` before the new card prints;
   "cancel" → DENY path, unchanged. The old draft's
   `card.speed_hint` word set ("faster"/"slower") is reused unchanged for
   the same reason — gate-tested vocabulary, already in the FLOW-REQUOTE
   fixture plan.
4. The offer **adds no tokens to any whitelist** — not CONFIRM, not DENY,
   not FILLER. The only whitelist change in this doc stays GATE-MERGE's
   admission of "sign" (§3), which the offer's keep-word relies on.

### 2.3 Outcome map and copy

| User says (while `CREATED`) | Gate | Model → dispatcher | Card after |
|---|---|---|---|
| "faster" | `NOT_A_DECISION` | `create_tx` fee_target=fast | re-quote lead line + variant-B card |
| "slower" | `NOT_A_DECISION` | `create_tx` fee_target=slow | same |
| "important" / "hurry it" / "ASAP" | `NOT_A_DECISION` | fee_target=fast (prompt-mapped) | same |
| "not important" / "no hurry" / "save money" | `NOT_A_DECISION` | fee_target=slow (prompt-mapped) | same |
| "medium" / "standard" | `NOT_A_DECISION` | same-rung re-quote (explicit target) | identical numbers, fresh ref/TTL, offer retired — a legal no-op-ish rebuild, no new copy needed |
| "sign" (post-GATE-MERGE) / "confirm" / "yes" | `CONFIRM` | `confirm_tx` | confirms at the displayed fee (final-fee invariant) |
| "cancel" | `DENY` | gate DENY path | cancelled — path unchanged |
| "faster" when already fast | `NOT_A_DECISION` | ceiling refusal | `card.rate_ceiling` |
| "slower" when already slow | `NOT_A_DECISION` | floor refusal | `card.rate_floor` |
| anything unclassifiable | `NOT_A_DECISION` | respond / clarify | variant-B re-show |

Copy (re-quote accepted, card reprinted, lead line):

- `Re-quoted at the faster rate — review the new fee below:`
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

**Real-card cross-check (verdict unchanged).** The renderer's own header
(`_CARD_HEADER_LINE`) asked `say 'confirm' or 'cancel'`, then
`_CONFIRMED_LINE` asks for "sign" — the incoherence the user hit lives
wholly in that seam: two consecutive renderer lines naming different verbs
for one decision. (The user's sketch line "when you approve I'll send to
your hardware wallet" describes precisely the flow they want in place of
that seam.) Nothing in the real nine fields changes the analysis above;
replacing the header with the single ask line and admitting "sign" to the
CONFIRM whitelist fixes both ends of the seam at once.

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

- The proactive offer → conditional card tail driven by a new display-only
  create_tx result key (`fee_target_defaulted`) + prompt guidance. No
  schema/grammar/v0 change; no new intents.
- "faster"/"slower" (and importance utterances mapped to them) → existing
  `create_tx` + `fee_target` param (flow policy + prompt only).
- "sign"-as-confirm → gate whitelist (app code, `tx/flow.py`) + prompt.
- "details" → `/details` transcript command (ADR-0020 channel), no intent.
- Merged confirm+sign → dispatcher chaining, no new intents.

**Needs a separate envelope-schema ticket (NOT recommended now):**
arbitrary fee-rate input (`{"fee_rate_sat_vb": n}` on `create_tx`) —
grammar, pydantic, business rules, prompt, and evals in lockstep. The
three-rung ladder covers the feedback; skip until a real request needs it.

### Code tickets to file (post-MW-4)

1. **CARD-BRIEF** — replace `_CARD_HEADER_LINE` with the ask line; trim
   `_print_confirmation_card` to the merged default view with the
   conditional tail (variant A offer / variant B tail, selected by
   `fee_target_defaulted` — one new display-only key on the create_tx
   handler result; Fee absorbs vsize + ETA concatenation from the same
   result dict; new "From:" line; Expires/Ref off the brief); cache the
   full nine-line render (variant-B base); `/details` command; verify the
   sign/broadcast narration re-prints `tx_ref` (the Ref demotion leans on
   it). Strings + small renderer/REPL/handler-dict change; no flow, gate,
   schema, or grammar change.
2. **FLOW-REQUOTE + OFFER** — allow dispatcher-owned replacement `create_tx`
   from `CREATED` (new `tx_ref`, TTL reset, commit-only-on-success
   ordering); prompt guidance: (a) set `fee_target` ONLY on a stated user
   preference, never guess, (b) importance-utterance mapping
   (ASAP/important/hurry → fast; no hurry/save money/can wait → slow),
   (c) speed changes always carry an explicit `fee_target`; eval fixtures
   for faster/slower re-quotes and the importance mapping; gate fixtures
   asserting every offer-answer classifies `NOT_A_DECISION`;
   floor/ceiling/same-rung copy. Ships with an **ADR-0013 amendment note**
   (replacement policy) — the ADR itself is orchestrator-owned; this doc
   does not amend it.
3. **GATE-MERGE** — add "sign" to the CONFIRM whitelist; same-turn
   confirm→sign chaining in the app wiring; updated guidance strings;
   "sign while CREATED ⇒ confirm_tx" eval fixture. Also carries an
   ADR-0013 amendment note (whitelist membership + merged user step).
   Build-order dependency: CARD-BRIEF's variant-A keep-word is "sign" if
   this ships first, "confirm" otherwise.

### Reverted from earlier passes

- The "prompt cap: ≤1 lead-in line after create_tx" rule — WITHDRAWN
  (§0/§1): it fixed a narration spill that does not exist.
- The "model-spilled menu" diagnosis and the free-options-are-dead-ends
  bug framing — REPLACED by §0: the menu was the user's requirements
  sketch; the dead-end constraint survives as a design rule (§0.2).

## Appendix: string block (structured for i18n)

```
card.ask                = Pending — say "sign" to review it on your device, or "cancel" to discard.   # replaces _CARD_HEADER_LINE verbatim
card.offer              = How important is this one? Say "faster" to confirm sooner (a slightly higher fee) or "slower" to save money (it may take longer) — or say "sign" to keep this rate · {card.details_tail}   # variant A tail; ONLY a wh-framing — a polar (yes/no) question is forbidden (§2.2: "yes"→CONFIRM, "no"→DENY); keep-word "confirm" pre-GATE-MERGE
card.details_tail       = full breakdown: /details   # variant B tail (preference already known — offer retired, never re-asked)
card.requote_lead       = Re-quoted at the {faster|slower} rate — review the new fee below:
card.rate_ceiling       = That's already the fastest recommended rate (next-block target). Say "sign" to proceed or "cancel" to discard.
card.rate_floor         = That's already the cheapest recommended rate — we never quote below the network minimum. Say "sign" to proceed or "cancel" to discard.
card.line_to            = To: {recipient}
card.line_amount        = Pay: {amount_sats} sats{usd? " (${usd} · rate age {rate_age_s}s{rate_stale? " · stale"})"}
card.line_fee           = Fee: {fee_sats} sats{rate? " · {fee_rate_sat_vb} sat/vB"}{vsize? " × {vsize} vB"}{target? " · {target_word}"}{eta? " — ETA {eta_wording}"}
card.line_from          = From: your wallet ({inputs_count} source{s? "s"}){change? " · {change_sats} sats come back as change"}
guide.still_pending     = Still pending — say "sign" to send it to your device, or "cancel" to discard it.
guide.ambiguous         = That was ambiguous — say "sign" to proceed with the pending transaction, or "cancel" to discard it.
```

(`card.speed_hint` from the earlier draft is deleted — its unconditional
faster/slower pitch is replaced by `card.offer` when no preference exists
and by `card.details_tail` when one does; the offer slot is never rendered
twice.)

`{eta_wording}` is the verbatim `chain/eta.py` output including the hedge —
never reworded by the model. `{target_word}` is the card's existing
fast/medium/slow word (rendered today as "medium target"; prose form
"next block / ~30 min / ~1 hour" if the orchestrator wants it spelled out —
same data, ticket-level wording decision). All values verbatim from the
handler result; absent values keep the existing fail-closed `unavailable`
marker rule (never a fabricated "0") — the merged lines above drop an
optional segment when the renderer would print `unavailable` for it, and
the raw field survives in the /details reprint.

`/details` needs no new string block: it reprints the cached full nine-line
card verbatim. Deferred string (NOT in this proposal's default view): an
explicit `@ $X/BTC` rate anchor — the create_tx result carries no rate
value today (only `usd_cents`/`rate_age_s`/`rate_stale`/`rate_fetched_at`),
so it is one new handler key when a user asks "what rate did you use?";
USD + age answer the decision-relevant "is this dollar figure fresh?"
without it.

## 5. `/details` deep view: "where this money comes from" (per-source provenance)

- **Source:** post-MW-4 user requirement (intent quoted): when we show a
  proposed transaction and the user asks for more information, explain
  where the UTXOs we are going to spend came from — (a) when they arrived,
  (b) other transactions they're associated with if known, (c) any KYC or
  non-KYC tag.
- **Grounded in (read-only survey):** `store/models.py` (`UtxoRecord`,
  `TxRecord`, `AddressRecord`), `store/db.py` accessors
  (`get_utxos_for_wallet`, `get_txs_for_wallet`, `get_by_address`),
  `tx/flow.py` (`PendingTx` — carries `psbt_base64` + `inputs_count`, NOT
  the per-input list), `tx/psbt.py` / `tx/revalidate.py` (the deterministic
  prevout/witness-utxo parse pattern to reuse), PROJECT.md §9/§10,
  ADR-0020 (transcript-command channel).

### 5.0 The honest-data rule (what this view may and may not say)

Three ceilings, stated before any copy:

1. **Store-only, zero new network.** Every claim below renders from rows
   this wallet already wrote during its own scans. The view fetches
   nothing — so it adds **nothing** to the §9 "what leaves the machine"
   table, and the copy must never imply chain-wide graph analysis. A
   question the local rows can't answer gets the honest fallback
   ("beyond what this wallet has seen" — §5.4), not a lookup.
2. **No KYC data source exists, and we will not fabricate one.** The
   requirement's item (c) as literally phrased ("KYC or non-KYC tag") is
   **not satisfiable in v1**: we have no exchange-attribute feed, no
   oracle, nothing — and inventing one would be exactly the over-claim §9
   forbids. The only honest v1 shape is **user-assigned notes** the user
   typed themselves, surfaced verbatim. "This came from Coinbase" is ever
   only the user's own note echoed back — never our inference, and the
   copy says so (`src.bounds`). The tagging *feature* (schema + a way to
   set notes) is a separate ticket (**LABELS-USER**, §5.5) — this section
   designs only the display shape, including the `(unlabeled)` fallback so
   shipping labels later doesn't reshape the view.
3. **Terminal-only, never model context.** `/details` output is
   code-rendered to the terminal (same channel and precedent as the card —
   the model cannot see it, HANDOFF §3.7). That is deliberate and it also
   closes a §9 threat vector: user notes are user input, and the §7.10
   red-team list already names "prompt injection via labels" — verbatim
   display to a human is safe; injecting the same strings into model
   context is not, so they never go there. In chat, the model may only say
   the view exists (FACTS carry balance-level facts, not provenance).

Jargon: the word "UTXO" never appears in user-facing strings. The plain
rendering is **source** with a one-time gloss on the view's head line
("the separate payments your wallet holds — Bitcoin spends them like
cash"), §10's zero-jargon-without-explanation rule.

### 5.1 What the store actually has, per input (data audit)

The pending record stores only `inputs_count` + `psbt_base64`, so the
renderer deterministically parses prevouts (`txid:vout`, value) out of the
cached PSBT — the exact parse `revalidate.py` already performs — then
joins against store rows:

| Need | Source today | Verdict |
|---|---|---|
| which coins are inputs (txid, vout, value, our address) | `PendingTx.psbt_base64` parse (code, verbatim) | **exists** |
| (a) arrival, confirmed coins | `TxRecord.block_time` (unix seconds of the block) + `height`, joined on the funding `txid`; `UtxoRecord.height`/`confirmed` corroborate | **exists** |
| (a) arrival, *unconfirmed* coins | `height=None`, `confirmed=0` is detectable, but **no first-seen/observed timestamp exists anywhere** — "when did it arrive" has no honest answer beyond "not in a block yet" | **needs store work** (one column — `STORE-FIRSTSEEN`, §5.5; no migration designed here) |
| (b) same-address history this wallet watched | join `UtxoRecord.address` → other rows sharing that address across `utxos` + `transactions` (direction in/out/self counts). No dedicated accessor; load-and-join in the renderer is fine at v1 wallet sizes (ADR-0010) — `ponytail:` add store accessors if scan history ever outgrows the list scan | **exists** (own-scan bounds only — the copy must carry that) |
| (c) any tag/label | nothing — `AddressRecord` has no label column; nothing classifies anything | **future ticket** (LABELS-USER); v1 renders `(unlabeled)` |
| — | `TxRecord.raw_summary` is `None` at every write site; `UtxoRecord.confirmed` is a flag, not a time | not usable; not promised |

### 5.2 Placement: inside `/details`, card unchanged

The brief card stays exactly as §1 defines it — this view is a depth
*below* `/details`, appended under the cached nine-line reprint:
`/details` → full card verbatim (existing §1 plan) → blank line →
provenance block (`src.*`). One affordance, one place.

**Decision: v1 is reachable only while a transaction is pending.**
Justification (brief): the requirement's trigger is precisely "when we
show a proposed transaction and the user asks for more information" — the
moment provenance matters is the destructive-flow review. Answering
provenance questions about arbitrary coins outside a pending tx needs a
coin-picking UX (which coin? addresses repeat; ambiguity handling) —
real surface, zero current demand. The code path is identical either
way, so a standalone `/coins` view later is a reuse, not a rewrite; until
a user actually asks, YAGNI. Chat-side, "where do my coins come from?"
with nothing pending is a `respond` turn that offers the view only when a
tx pends.

### 5.3 Per-source line format

One block per input, ordered exactly as `select_coins` returns them
(deterministic sort — the PSBT's input order, never re-ordered for
display). Example with two sources (mainnet figures are illustrative,
all values in real output are verbatim from the parse/store):

```
Where this comes from — 2 sources, 312,500 sats in total
  Sources are the separate payments your wallet holds; Bitcoin spends
  them like cash, so one payment can draw on several.

  1.  250,000 sats · received 2026-08-31 (~7 days ago) · block 921,433
      address bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh · (unlabeled)
      other activity we've seen on that address: 2 received, 1 sent

  2.  62,500 sats · received recently — not in a block yet
      address bc1q9hm8cqq9vnv3zkfhq3fh3ddcmb88mypex0fydf · (unlabeled)
      no other activity on that address in this wallet's history

  This list covers only what this wallet has seen on its own. Nothing
  was fetched to build it, and nothing here is a claim about who sent
  anything — labels, where present, are your own notes, shown as you
  wrote them.
```

The label line is the only part that changes when LABELS-USER ships:
`(unlabeled)` → `your note: "marketplace payout"` (user's words verbatim,
quotes are the verbatim delimiter, no rewriting/case-fixing).

### 5.4 Copy rules inside the block

- **Values verbatim, always** (§10 / HANDOFF §3.7): sats, full addresses
  (never truncated mid-hash, copyable), txids when quoted, block heights —
  all from the PSBT parse / store rows. Date + "(~N days ago)" is display
  formatting derived from `block_time`, same class as the sats separators
  §1 allows.
- **Missing-data honesty, never a fabricated zero or guessed date:**
  `block_time` null but height present → `confirmed in block {height} —
  exact time not recorded`; unconfirmed → the `arrival_pending` line
  (makes no claim about *when* — that's the STORE-FIRSTSEEN gap §5.1, and
  the copy carries its ceiling, not hides it); funding txid absent from
  `transactions` (older history than the current cache) →
  `arrival_unknown` + association line `beyond_ours` ("we haven't watched
  the whole chain — ask me to resync" style next-step, §10 error pattern).
- **Association counts** are stated as what *we saw* (`"we've seen"`),
  direction words are plain: received / sent / sent back to yourself
  (`DIR_SELF`); `self` never renders as a suspicious duplicate.
- **Dual units (§10) — scoped deviation, flagged for the orchestrator:**
  the per-source lines are **sats-only**. The create_tx result carries
  `usd_cents`/`rate_age_s` for the transaction total, not a raw rate, so a
  per-line USD figure would need a new rate key (same deferred anchor as
  §1's note) and would spam the block. The §10 dual-units duty is written
  for confirmation *cards*; the card and the block's total line carry it.
  If the orchestrator reads §10 strictly for every amount on screen, the
  fix is one handler key (`rate_sat_usd`), not a copy change.

### 5.5 Tickets this section files (post-MW-4; joins §4's list)

1. **TX-PROVENANCE** — the §5.2–5.4 view: PSBT prevout parse (reuse the
   `revalidate.py` pattern), in-renderer store joins, `src.*` strings,
   `/details` append under the cached card. No envelope/grammar/intent
   change, no network, no flow/gate change. Tests: golden render from
   fixture store rows; honest-fallback branches (null block_time,
   unconfirmed, unknown txid).
2. **STORE-FIRSTSEEN** — one column so unconfirmed arrivals get an honest
   "received ~N min ago". Not designed here (schema-migration owner is
   the orchestrator); until it lands, `arrival_pending` is the ceiling.
3. **LABELS-USER** (feature ticket, NOT copy) — user-assigned notes on
   addresses: storage, a set/clear command, and the verbatim
   `src.label_yours` render. Ships with an eval asserting label text
   never enters model context (§9 injection vector, §5.0.3) and a copy
   review confirming nothing auto-classifies.

### Appendix additions (string block, structured for i18n)

```
src.head        = Where this comes from — {n} source{s? "s"} {total_sats} sats in total
src.gloss       = Sources are the separate payments your wallet holds; Bitcoin spends them like cash, so one payment can draw on several.
src.line_value  = {index}. {value_sats} sats · {arrival}
src.arrival_block   = received {date} (~{age}) · block {height}          # from TxRecord.block_time
src.arrival_height  = confirmed in block {height} — exact time not recorded
src.arrival_pending = received recently — not in a block yet
src.arrival_unknown = arrival outside what this wallet has recorded — try a resync, then /details again
src.line_address = address {address} · {label}                          # address verbatim, never truncated
src.label_none   = (unlabeled)                                          # v1 default; never implies we checked anything
src.label_yours  = your note: "{user_label}"                            # LABELS-USER; user's words verbatim, our copy never endorses the claim
src.assoc_counts = other activity we've seen on that address: {n_in} received, {n_out} sent{n_self? ", {n_self} back to yourself"}
src.assoc_none   = no other activity on that address in this wallet's history
src.assoc_beyond = we've watched {n} transaction{s? "s"} on that address; anything earlier or elsewhere is beyond what this wallet has seen
src.bounds       = This list covers only what this wallet has seen on its own. Nothing was fetched to build it, and nothing here is a claim about who sent anything — labels, where present, are your own notes, shown as you wrote them.
src.error        = I couldn't read the coin history for this wallet — check it's still loaded, then say "retry". Your pending transaction is untouched.   # value-free (§7.8): names no address/txid/amount
```

(`{arrival}`/`{label}` select one variant each, above; the renderer
chooses by data presence, so no line ever renders an empty segment.
`src.error` follows the §10 cause-plus-next-step pattern and the
fail-closed rule: if provenance can't be read, `/details` shows the card
reprint plus `src.error`, and the pending flow is untouched — provenance
is informational, never a gate.)

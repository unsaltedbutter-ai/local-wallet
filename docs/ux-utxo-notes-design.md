# UX proposal: coin notes + tag-aware selection policy

- **Status:** PROPOSAL ONLY — copy and policy design. `app.py` changes are
  filed against post-MW-4 (same hands-off rule as
  `docs/ux-tx-card-feedback.md`); `store/` and `tx/` work is dispatchable
  earlier (section 5 flags each ticket).
- **Source:** user requirements 2026-09-07 (intent quoted in §0), extending
  the user-requested provenance view (`ux-tx-card-feedback.md` §5, whose
  LABELS-USER placeholder this design absorbs and supersedes).
- **Grounded in (all read-only):** `tx/selection.py` + ADR-0012 (current
  deterministic policy, incl. its TCK-HW-003 amendment), `tx/dust.py`,
  ADR-0009 (gap_limit setting precedent: fail-closed parse, value-free
  errors), `store/db.py` (settings key/value; `chain_base_url` typed-pair
  precedent; env > stored > default ladder via `config.resolve_chain_base_url`),
  `store/models.py` (settings table survives the UTXO snapshot's
  DELETE+re-INSERT — labels must live in a **separate table**, §1.3),
  `ux-tx-card-feedback.md` §1 (card one-conditional-slot rule), §2
  (FLOW-REQUOTE re-runs selection; the offer owns the tail slot), §5
  (provenance view: user-assigned labels only, label text never enters
  model context, "source" as the §10 gloss for UTXO).

## 0. Requirements, verbatim intent

(a) Capture user notes about transactions — date automatic; things like "is
this a KYC transaction", "going to an exchange", "peer to peer", "a
purchase", "a consolidation" — to inform **which coins we spend in future
transactions**.

(b) Selection "cannot just be the smallest one that fits":
1. don't mix KYC with non-KYC coins;
2. sometimes consolidate two UTXOs when fees are low — **and tell the user**;
3. user-configurable target UTXO minimum and maximum (advanced setting,
   gap-limit-style), with best-guess defaults for the average user.

## 1. Notes capture

### 1.1 The cardinal rule (read this first)

Tags and notes are **user-authored facts about the user's own coins**. They
are consumed exclusively by **deterministic code**: the selection layer reads
them as plain data (like fee rate and UTXOs — §10 of ADR-0012's
"arrives as data" discipline); the card renderer prints them verbatim. They
**never enter model context, in either direction**:

- The model never sees tag/note text (not in FACTS, not in the transcript
  injection). This is the §7.10 "prompt injection via labels" vector, closed
  the same way `ux-tx-card-feedback.md` §5.0.3 closes it for `/details`:
  verbatim display to a human is safe; the same string near the sampler is
  not.
- The model never **authors** them. `/label` is a deterministic
  transcript-channel command (ADR-0020, like `/details`, `/export`,
  `/scrub`) — no new intent, no envelope, no grammar change, no prompt
  guidance. There is no chat path by which a sentence like "mark that as
  KYC" mutates a label; the command line does it. (A future natural-language
  front door could emit a *structured* `label_coin` intent whose params the
  label-code re-validates against the closed tag set — deliberately out of
  scope now; see skipped list at the end.)
- Label text is **never** echoed into exception/log context (store policy:
  value-free errors; the text is user data, same class as an address).

Honest-bounds rule (§9): a tag is the user's own claim, and the copy says so.
We never verify anything: no exchange's KYC policy, no counterparty identity.
Display frame is always "you marked" / "your note" — never "this is a KYC
transaction."

### 1.2 When the ask happens: on demand, plus one post-broadcast hint

**Decision: no prompt during the confirm flow, ever.** Three reasons:

1. The confirmation card already has one job and one conditional slot (the
   speed offer, `ux-tx-card-feedback.md` §1/§2.0). A second question —
   "what kind of payment is this?" — would either evict the offer or break
   the one-slot rule, and mid-flow questions are exactly the friction the
   TCK-UX-002 work is removing.
2. The dual-key gate discipline (§2.2 there): any question shown while
   `CREATED` risks a gate word answering it ("yes" → CONFIRM). A note
   capture must never be answerable with a gate word; the only bulletproof
   placement is **outside** the pending flow entirely.
3. The facts are freshest right after broadcast — and by then the flow is
   in `BROADCAST`, no gate armed, so even the *hint* cannot be
   gate-mistaken.

So capture is:

- **Primary, on demand:** `/label [last | <txid>] [tag words] [| free note]`
  — deterministic command, terminal output. `last` resolves to the most
  recent tx this wallet broadcast (app session state; with nothing to label
  it says so, value-free). Tags from a closed set (§1.4); text after `|` is
  a free note. Date is never typed — the transaction already carries
  `block_time`/`height` (the §5.1 arrival join).
- **Secondary, one hint:** after a successful broadcast the renderer prints
  one line (code-owned, static string — not narration):
  `Want to remember what this was? Type /label last [tag] ["note"]` —
  skippable forever, never repeated within the session for the same tx.

### 1.3 Where notes live: coin-level, in a new table, with lineage

**Decision: per-UTXO (outpoint) is the primary key; tx-level is derived.**
Justification against both consumers: SELECTION needs coin-level tags (it
partitions candidate UTXOs — §2.1); HISTORY wants tx-level (the `/details`
provenance line — §5.3 there). Storing per-coin and rendering per-tx works
in both directions; the reverse doesn't (a tx tag can't tell selection which
of the wallet's *other* coins share its character). One table:

```
coin_labels (
    wallet_id  INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
    txid       TEXT NOT NULL,
    vout       INTEGER NOT NULL,
    tags       TEXT NOT NULL,   -- closed-set ids, comma-joined, canonical order
    note       TEXT,            -- user's free text, verbatim, ≤ 500 chars
    PRIMARY KEY (wallet_id, txid, vout)
)
```

It must be a **separate table**, not a column on `utxos`: the snapshot is
DELETE+re-INSERTed by every scan (`store/db.py::_replace_utxo_rows`), so
anything stored on the UTXO row dies on the next sync. Outpoint-keyed rows
survive rescans unchanged (schema v2 migration; the settings table's
key/value mechanism is *not* used — labels are structured data, not scalars).

Writes:

- `/label <txid> …` labels the **wallet-owned outputs** of that transaction
  (we know which outputs are ours — derived addresses). This matches the
  user's mental model: "this payment was a purchase" ⇒ *the coins it created*
  are purchase-coins. The spent inputs of that tx are already gone.
- **Lineage at broadcast confirmation** (deterministic, store-side): when
  our own tx S change output C, C inherits the union of S's input tag sets.
  Conservative taint semantics: a kyc coin's change is still kyc-side; a
  coin created by mixing is "mixed" (both sides) and thereafter counts on
  **both** sides of the partition check — it can un-mix nothing, and being
  treated as kyc-side is the fail-safe direction (§2.1).
- `/label` with no tags and no note clears; a bare re-label replaces.
- Unlabeled coin = no row = `(unlabeled)` in the §5 view. That fallback
  already exists, so this feature drops into the provenance renderer with
  zero reshape.

Privacy note (honesty, §9): labels live in the local DB only; the DB holds
user-claimed attributes, so it is already sensitive — covered by the same
export/scrub story as the rest of the store. Nothing about labels leaves
the machine: selection is local, and labeling never causes a network call.

### 1.4 Tag vocabulary: small closed set + free-text note

**Recommendation: fixed suggested tags (closed set, selection-relevant) plus
one free-text note per coin — not free text alone, and not free text
parsed.** Rationale: the user's own examples map cleanly onto a handful of
words; selection can only act on a closed vocabulary (a deterministic
partition test cannot grep prose); the free note carries everything else
("Alice's refund", amounts in fiat, anything) as display-only history,
never read by selection logic.

Suggested set (labels the user applies to their payments; display strings
include the honest frame):

| id | user sees | meaning for selection (§2.1) |
|---|---|---|
| `kyc` | KYC | KYC-side |
| `exchange` | exchange | KYC-side (funds become exchange-traceable) |
| `p2p` | peer to peer | other-side |
| `purchase` | purchase | other-side |
| `consolidation` | consolidation | neutral (describes the payment, not the coins' character — display-only) |
| (none) | (unlabeled) | other-side by default |

Two partition classes exist because selection only ever needs **one binary
question**: is this coin associated with KYC'd venues or not? `p2p` vs
`purchase` vs unlabeled doesn't change any spend decision; the distinction
is history for the human (surfaced verbatim in `/details`). The partition
rule is thus: **kyc-side = {kyc, exchange, mixed-by-lineage};
other-side = everything else; a transaction whose inputs span both classes
is a "mix."**

Unknown tag word in `/label` → plain-cause + next-step error:
`I don't know that label. Known ones: kyc, exchange, p2p, purchase,
consolidation — or type your own words after "|" for a note.` (value-free —
quotes no txid.)

## 2. Selection policy (ADR-0012 amendment draft in §2.4)

All of this stays inside the existing contract: deterministic pure function
of plain data (§7.5/ADR-0012), no I/O in `tx/`, exact integer math. The
handler joins labels onto the UTXO snapshot before calling `select_coins`;
each duck-typed UTXO gains one plain attribute, `kyc_side: bool` (mixed
coins: `True` — fail-safe; a `mixed` flag rides too for narration). Fee
rate and settings arrive as data like everything else.

### 2.1 Tag-compatible selection (the mixing rule)

Define precisely: a **mix** is a selected input set containing at least one
kyc-side coin and at least one other-side coin.

Algorithm amendment, layered *around* the existing four steps (which are
unchanged within any given pool):

1. Run the existing algorithm (steps 1–4) on three pools: kyc-side coins
   only, other-side coins only, and the full set.
2. If one or more pure pools finalize, choose between them by
   `(fee_sats, pool-order)` — lowest fee wins, deterministic tie-break
   (pool-order: other-side, kyc-side; fixed so equal-fee runs are
   reproducible). The mixing run is discarded.
3. If **no pure pool funds the amount** and the full set does → the tx is a
   mix: it proceeds, but the card MUST carry the mix warning line (§4.3) —
   "avoid unless the user explicitly asks or no alternative exists, and if
   unavoidable, narrate it." The explicit-ask path (a per-send
   "use my exchange coins" preference on `create_tx`) is deliberately NOT
   designed here — it needs an envelope change; the fallback ordering
   already covers the realistic cases.

Conservative edge, stated so a reviewer doesn't "fix" it: a pure pool can
lose a *single-coin improvement* to the mixed pool's cheaper fee — we still
take the pure pool. Unmixed costs more by design; that's the user's rule (b1)
made structural, and §2.3's max-coin rule already establishes "save a coin,
not sats" as policy precedent.

### 2.2 Low-fee consolidation (rule b2) — with mandatory narration

New improvement **step 5**, after the existing step 4 and before the
conservation assert (which still holds — the assert is over the final set):

- **Trigger:** the tx's fee rate ≤ `consolidate_below_sat_vb` (setting,
  §2.3) **and** the chosen pool contains ≥ 2 unselected coins with
  `value_sats < utxo_target_min`.
- **Action:** add such coins to the input set, canonical order, one at a
  time, while ALL of: added inputs ≤ `_MAX_CONSOLIDATE = 4`; each added
  coin's value ≥ 2 × its incremental input fee (`2 × 68 vB × rate` — a
  folding coin must earn its passage at double the greedy skip bound); the
  set still finalizes. Stop at the first violation. Deterministic by
  construction (pure function of snapshot + rate + settings, same as steps
  1–4). Change/fee recompute through the existing `finalize()`.
- **Narration before confirm, always:** the consolidation never changes
  *what* the user pays, but it changes *which coins* — the same §10
  source-of-funds duty as the input count. It rides the card's **From:
  line** as one optional clause (not the conditional tail — that slot is
  the speed offer's, §4.3):
  `From: your wallet (3 sources · folding in 2 small ones now to save fees later)`
  Plain-words check: "sources" already glossed by the §5 view; "folding in"
  needs no gloss; no "consolidation/UTXO" jargon on the card.

**Default threshold proposal: 2 sat/vB.** Who sets it: shipped default,
user-changeable in advanced settings like everything here. Why 2: the
estimator ladder's slow/medium rungs live around 1–2 sat/vB, so
consolidation fires on ordinary cheap-fee sends (the realistic "fees are
low" moments) and never on a priority spike, where folding extra inputs
would inflate the very fee the user just chose to pay more of. The `slow`
fee target doubles as the spoken affordance for "consolidate for me when
it's cheap" — no new vocabulary word needed; the clause narrates when it
happens.

### 2.3 Target min/max (rule b3) — names, defaults, bounds

Mirror the gap_limit precedent exactly (ADR-0009 + TCK-CFG-001 +
`chain_base_url`): decimal-string values in the `settings` table, typed
store accessor pairs as the only sanctioned writers (fail-closed
validation at write, value-free errors), pure resolution function in
`config.py`, precedence **env > stored > default**, startup refuses to run
on a malformed value rather than silently reverting.

| setting key | env | default | bounds | semantics |
|---|---|---|---|---|
| `utxo_target_min_sats` | `LOCALWALLET_UTXO_TARGET_MIN_SATS` | **100,000 sats** (~$79 at ~$79k/BTC) | 546 .. 100,000,000 | coins below this are consolidation candidates (§2.2) |
| `utxo_target_max_sats` | `LOCALWALLET_UTXO_TARGET_MAX_SATS` | **10,000,000 sats** (0.1 BTC) | must exceed min; ≤ 21,000,000,000,000 | step 3 (single-coin improvement) never shatters a coin above max to save fees |
| `consolidate_below_sat_vb` | `LOCALWALLET_CONSOLIDATE_BELOW_SAT_VB` | **2** | 1 .. 100 | fee-rate ceiling for step 5 (§2.2) |

Validation cross-check: `min ≥ max` is malformed → fail closed (a corrupt
setting never silently flips policy — the ADR-0009 sentence applies verbatim).

Why these defaults for "the average user," and the tradeoff, stated for the
settings screen (plain words):

- **Many small coins** cost more to spend later (each source adds ~68 vB ≈
  a few hundred sats at low rates) and widen the privacy surface (more
  links on-chain, more address history).
- **Few big coins** pay consolidation fees *now* (the folding tx itself)
  and concentrate each future spend — plus, with tags, larger KYC/non-KYC
  blocks make future unmixed spends easier.
- 100,000 sats sits where a future spend starts costing a noticeable
  percent of the coin's own value even at 10 sat/vB; 0.1 BTC is "big enough
  to protect" for typical holders without reserving so much that ordinary
  sends run short — ascending greedy already prefers small coins, so max
  only protects the *reserve* from fee-penny-shaving by the improvement
  pass. Both numbers are opinionated defaults, exactly gap-limit's status.

### 2.4 ADR-0012 amendment draft (text to be applied by the orchestrator — this doc does not amend the ADR)

> ## Amendment (TCK-UTXO-002, 2026-09-07): partitioned selection,
> target min/max, low-fee consolidation
>
> Decisions 1–4 are unchanged *within a candidate pool*; three policy
> layers wrap them. Coin tags and target settings arrive as plain data on
> the UTXO snapshot and the settings argument, exactly like the fee rate —
> `tx/` performs no store access and reads no user free text (the note
> field never reaches this module; only the `kyc_side`/`mixed` booleans
> and integer settings do).
>
> 1. **Partition preference.** Run decision 1 (steps 1–4) over the kyc-side
>    pool, the other-side pool, and the full set. A pure pool wins over the
>    mixed result whenever it finalizes (choice: lowest `fee_sats`, then
>    fixed pool order other-side → kyc-side); mixed inputs occur only when
>    no pure pool funds the amount. "Purely by design, mixed pools may pay
>    more than a mixed selection; the caller must surface the mix on the
>    confirmation card." Coins created by a mixed spend inherit both
>    classes (caller-side lineage; `tx/` sees only the resulting boolean,
>    where mixed means kyc-side).
> 2. **Step 3 bound (max target).** The single-coin improvement pass never
>    substitutes a coin above `utxo_target_max_sats`; preserving one large
>    coin outweighs a cheaper fee.
> 3. **Step 5, low-fee consolidation.** After step 4: when the fee rate ≤
>    `consolidate_below_sat_vb`, fold unselected pool coins with
>    `value_sats < utxo_target_min_sats` in canonical order, bounded by 4
>    added inputs and per-coin value ≥ 2 × incremental input fee, keeping
>    finalization. Pure function of inputs like steps 1–4; the conservation
>    assert covers the final set.
> 4. **Settings ladder.** `utxo_target_min_sats` (default 100,000),
>    `utxo_target_max_sats` (default 10,000,000), `consolidate_below_sat_vb`
>    (default 2): `settings` keys with typed fail-closed store writers,
>    pure env > stored > default resolution in `config`, malformed value =
>    startup refusal, value-free errors — the ADR-0009 / ADR-0023 pattern.

## 3. Settings surface (`/settings`)

The CLI has no settings page; the REPL grows a deterministic transcript
command (ADR-0020 channel again — model never sees it, no intent):

- `/settings` — prints current value, default, and a one-line plain-words
  description for each managed key: `gap_limit`, `utxo_target_min_sats`,
  `utxo_target_max_sats`, `consolidate_below_sat_vb`, `chain_base_url`
  (read out as host only? no — full value; it's the user's own setting,
  terminal-safe, same class as /details printing addresses).
- `/settings <key> <value>` — validates through the SAME typed store writer
  (never a second parser), echoes the new value plainly, and states the
  consequence ("Future sends will fold small coins when fees are at or
  under 2 sat/vB"). Bad value → the writer's fail-closed refusal surfaced
  as cause + fix + example line; nothing stored.
- `/settings <key> ""` clears the stored rung (back to default), matching
  the `set_chain_base_url("")` convention.

Data-model rule for the stated future wish (web UI with a settings button):
every value lives in the `settings` key/value table under the documented
key, validated at write by the store's typed pair, with the precedence
ladder in `config` — a web page is then a pure reader/writer of the same
keys and never needs app.py. No UI concept (card, command, button) appears
in the storage layer.

## 4. Interaction with existing flows

### 4.1 create_tx wiring (dispatcher-owned, model-free)

The `create_tx` handler already resolves UTXOs + fee rate and calls
`select_coins` (ADR-0012 decoupling note). The change is a handler-side
join: `coin_labels` rows → `kyc_side`/`mixed` attributes on the snapshot,
settings values → new keyword args. The model's role is zero: it emits the
same `create_tx` envelope it emits today; tags influence the deterministic
selection underneath it. (Chain-layer note for the implementer: selection
input objects are already duck-typed; adding attributes keeps `tx/`
I/O-free — no lint exposure.)

### 4.2 FLOW-REQUOTE pin (the §2 interaction)

A faster/slower re-quote re-runs selection at a new rate — which can
legitimately change the mix (a pool that funded at 1 sat/vB may not fund at
4; step 5 switches off when the rate rises above the threshold). Rule, to
be pinned by an eval/gate fixture in TCK-UTXO-004:

1. **The card always describes the final selection.** Mix/consolidation
   clauses render from the just-computed input set, so a re-quote can never
   *silently* change tag-mixing properties — the new card either shows the
   mix line or it doesn't, and confirmation is only valid against the card
   the user is reading (the existing final-fee invariant generalizes to
   final-inputs).
2. Handler additionally refuses nothing and warns nothing extra on re-quote
   — the clause machinery is rate-agnostic; no new state on `PendingTx`
   beyond the already-needed `inputs` metadata the /details parse uses.

### 4.3 Card slot allocation (one conditional slot rule — where things go)

`ux-tx-card-feedback.md` §1: the card's last line is one conditional slot,
two variants, owned by the speed offer. That rule governs lines that
**present a choice**. The new material never competes for that slot:

| piece | placement | why |
|---|---|---|
| consolidation clause | appended to the **From: data line**, optional segment | it's a source-of-funds fact (§10's card list), same class as the change segment already on that line; names no verb, presents no choice |
| mix warning | **dedicated conditional line above the ask line**, printed only when the final selection mixes partitions | an unavoidable-mix is a §10-worthy "review carefully" moment — it must be visible at a glance, cannot hide in /details; it names no actionable verb besides the existing "cancel" (the ask line's), so no gate vocabulary is added |
| note hint | terminal line after **broadcast**, flow no longer pending (§1.2) | zero card impact |
| labels themselves | **/details only** (the §5.3 `src.label_yours` line goes live — this design IS LABELS-USER) | the brief card stays four data lines; provenance depth is what /details is for |

Copy strings (structured for i18n; values verbatim from tool output):

```
card.line_from          = From: your wallet ({inputs_count} source{s?} · {n} sats come back as change){consolidated? " · folding in {folded_count} small ones now to save fees later"}
card.mix_warning        = Heads up: this mixes coins you marked KYC with coins you didn't — say "cancel" if that's not what you want.
card.broadcast_hint     = Want to remember what this was? Type /label last [tag] ["note"]
label.set               = Noted on transaction {txid} — your coin tags: {tags}{note? " · your note: \"{note}\""}   # txid verbatim full; tags/note echoed as stored
label.cleared           = Cleared your note for that transaction's coins.
label.unknown_tag       = I don't know that label. Known ones: kyc, exchange, p2p, purchase, consolidation — or type your own words after "|" for a note.
label.nothing_last      = Nothing to label yet — "last" is the most recent payment you've sent.
label.error_store       = I couldn't save that note — the database is busy; say "retry".   # cause + next step, value-free
settings.head           = Current settings (change one: /settings <name> <value>):
settings.row            = {key} = {value}{default? " (default {default})"} — {plain_description}
settings.set_ok         = Set {key} to {value}. {consequence_line}
settings.set_bad        = That's not a valid {key}. {range_words} Try, for example: /settings {key} {example}
settings.cleared        = Cleared {key} — back to the default ({default}).
```

Honesty checks baked into the strings: mix warning says "coins you
**marked**" (user's claim, our echo); the §5 view's existing
`src.label_yours = your note: "{user_label}"` frame is the display shape —
no string anywhere asserts that we know anything about an exchange's
policies or a counterparty's identity. "KYC" itself is the user's word; the
one-time gloss on first mix warning / settings row: "KYC = an exchange or
service that knows who you are."

### 4.4 Gate audit (ADR-0013 unchanged)

- `/label` and `/settings` are transcript commands — they never reach the
  model, the gate, or the dispatcher. Zero whitelist changes, zero new
  intents, zero envelope/grammar changes.
- No new spoken words appear as *questions* on the card: the mix warning
  names only "cancel" (already DENY, correctly placed); the consolidation
  clause asks nothing; the broadcast hint appears when no gate is armed.
  The note-capture prompt can therefore be unanswerable by a gate word, by
  placement and by channel.

## 5. Proposed tickets

Format matches TASKS.md rows; "post-MW-4" flags the app.py touch.

| ID | area | files | depends | done-when |
|---|---|---|---|---|
| TCK-UTXO-001 | store+capture | `store/{db,models}.py` (schema v2: `coin_labels` + typed fail-closed accessors + lineage-on-broadcast helper), `app.py` (`/label` command, post-broadcast hint, `last` tracking), tests | — | label/erase/list round-trips survive a full rescan; lineage union rule pinned by store tests; hint prints once post-broadcast; note text never enters any agent context (negative test); **store half dispatchable now; app.py half post-MW-4** |
| TCK-UTXO-002 | tx+config | `tx/selection.py` (+ docstring reproduction), `config.py` (three env rungs), tests, `docs/adr/0012…` (amendment §2.4 — orchestrator applies) | TCK-UTXO-001 (tests need label data shape, not the command) | partition preference / step-5 consolidation / max-coin improvement all deterministic + docstring-reproducible; conservation assert green over final sets; malformed settings refuse startup value-free; **dispatchable now (no app.py)** |
| TCK-UTXO-003 | ui | `app.py` (`/settings` command over the typed writers), strings §3/§4.3, tests | TCK-UTXO-002 (keys exist), TCK-CFG-002 (config-file rung — reconcile ladders) | `/settings` lists/changes/clears all three new keys through the same store validators as the env ladder; **post-MW-4** |
| TCK-UTXO-004 | narration+evals | `app.py` card renderer (From clause, mix line), `evals/golden` + gate fixtures | TCK-UX-002 (same renderer files, ordered), TCK-UTXO-001/002 | consolidation clause renders iff inputs grew beyond the funding minimum; mix warning renders iff final selection spans partitions; **FLOW-REQUOTE fixture: faster→slower re-quote that changes the mix re-renders the card with the line present/absent accordingly**; every offer/hint string audited for gate-word collision; **post-MW-4 (app.py) + evals dispatchable early** |

Skipped by design (add when demanded): a `label_coin` model intent for
spoken labeling (needs schema lockstep; `/label` covers it), a per-send
"pay from my exchange coins" preference (envelope param — §2.1's fallback
ordering covers the realistic cases), per-line USD on labels (no amounts in
labels to convert).

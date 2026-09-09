# UX spec — first-run web flow: watch key + node choice on one card (TBC ticket pending)

Owner: UX writing. Scope: the web page's initial state, the first-run card,
the settings consolidation, and every new/changed string. No code here;
§6 is the implementer brief and the TBC list. Branch: `dev/plan-run-1`.
Companion docs: `docs/ux-web-copy.md` (voice rules 1–5 — this spec follows
them), ADR-0023 + amendment 2 (the consent model), ADR-0024 §7 (render
contract), `src/localwallet/ui/onboarding.py` (the §9-signed CLI copy the
web lines echo).

User direction honored (2026-09-09): first launch asks for BOTH the watch
key and the node info, on one card; a checkbox labeled in the user's own
words picks the public node and disables the URL field; zpub, gap limit and
chain_base_url live in Settings; Settings starts open when unset and closed
once set; after a successful key entry the form card goes away (the green
"Connected." remains as the hand-off beat).

---

## 1. First-run card layout

One card (evolves the existing `#watchkey-panel`), sections in the user's
order. Shown while the `/state` snapshot says the wallet or the backend is
unset (§4 booleans). Chat stays disabled while any part of the card is up
(current behavior, kept — an unprovisioned engine cannot answer).

```
┌──────────────────────────────────────────────────────────┐
│  Connect your wallet                                     │
│                                                          │
│  [existing lede + hardware-wallet-only warning — REUSED  │
│   VERBATIM from index.html; do not paraphrase again]     │
│                                                          │
│  ( ) Public account key                                  │
│  [ zpub…                                        ]        │
│                                                          │
│  Where should the app check your wallet?                 │
│                                                          │
│  Your server's web address                               │
│  [ https://mempool.your-node.example/api        ]        │
│  The web address of your node's mempool.space app — its  │
│  API address is usually the same, with /api at the end.  │
│  Nothing is saved, and no address of your wallet goes    │
│  to it, until the app has checked that it answers        │
│  correctly.                                              │
│                                                          │
│  [ ] Use a public node even though it damages my privacy │
│                                                          │
│  [ Connect ]                                             │
│  (status line — see §2)                                  │
└──────────────────────────────────────────────────────────┘
```

### The checkbox

- **Label (verbatim user wording, kept):** `Use a public node even though
  it damages my privacy`. Form label, no period (ux-web-copy rule 2).
  "node" vs "server": the user's word wins on the label; body copy keeps
  "server" (the CLI's word) and the label is the one deliberate exception —
  noted so the implementer doesn't "fix" it.
- **Checked:** the URL field is disabled (`disabled`, greyed via CSS but
  text stays readable — `aria-disabled` + a class, never `hidden`; a typed
  URL stays visible while greyed, untick restores it). The URL hint line is
  replaced by the leak note `nodePublicNote` (§5) — the checkbox is only
  honest where the leak is named on the same screen, which is exactly the
  ADR-0023 amendment-2 principle ("consent happens where the leak is
  named") satisfied on the web surface.
- **Default on first view: UNCHECKED.** Justification: privacy is the
  visible default path (the card must not pre-select the leak — that would
  re-enact the implicit-default posture amendment 2 retired); the checked
  state is an affirmative, warned choice, and an unticked box is what makes
  the leak note's absence legible.

### Empty-field interplay (explicit, no silent defaults)

Unchecked + empty URL is **not** a submittable state and **not** a hidden
"it'll default to public" state. Clicking Connect with a key + empty URL +
unticked box shows the inline validation line `nodeRequired` (§5) under the
status area and does not POST. The user resolves it one way or the other —
type an address, or tick the box. There is no third path.

### Card variants (same card, less of it)

- **Key unset + backend unresolved** (true first run): full card as above.
- **Key set + backend unresolved** (returning user who never chose; the
  web's replacement for pointing at the terminal via `WEB_SETUP_HINT`):
  key field replaced by a one-line status `keyConnectedLine` (§5); the node
  section + Connect stand alone. Needs the additive `/state` boolean in
  §6-TBC-3.
- **Key unset + backend resolved** (flag/env or Settings supplied a URL):
  key section only — today's card exactly, no node section.

## 2. State transitions

Submit is one POST (§6-TBC-1 decides the payload shape). The client shows
the server's value-free reason lines verbatim — it never adds diagnosis the
engine didn't report (mirrors the watchkey relay contract in `app.js`).

| # | Input | Engine outcome | Screen |
|---|-------|----------------|--------|
| A | key + URL, both valid | key provisioned; URL probed via the existing `check_backend` path (decision-5 validation; **probed before saved**) and saved; `chain_backend_choice` NOT written (a stored URL is itself a resolved rung, ADR-0023 amendment 2 req. 1) | fields freeze, status shows `connectedOwnLine` (green), then the card **dismisses** on the next `/state` (§dismissal below). Wallet is NOT loaded this session — the honest restart line is part of `connectedOwnLine`; the CLI's `DEFERRED_RESTART` logic transfers verbatim because the reason (live client ≠ chosen client) is identical. |
| B | key + checkbox | key provisioned; public consent recorded (`chain_backend_choice="public"`, TCK-ONB-006 semantics) and the held scan released through the same hook the CLI uses | fields freeze, status shows `connectedPublicLine` (green, names the leak one final time in the ack), card **dismisses**; the scan chip (`scanLoading`) takes over — the wallet actually loads this time, so saying so is honest. |
| C | no URL + no checkbox | nothing sent | inline `nodeRequired`; card unchanged, nothing lost. |
| D | bad key (seed-shaped, testnet, private, unparseable) | engine's existing value-free refusal (`watchkeyRejectedPrefix + <reason>`, pins in `app.js`) | error in status line; key field keeps its content (the user fixes a typo, doesn't retype 111 chars); node section untouched. |
| E | unreachable / not-mainnet / not-Esplora URL | probe fails, **nothing saved, nothing leaked** | status shows `nodeFailed` (adapted `VALIDATION_FAIL`: same facts — wasn't reachable / didn't answer as a mainnet mempool.space API / nothing saved / no address sent — next step adapted to a form: "fix the address and try again, or tick the public box"; the CLI's "say retry" and doctor lines don't belong on a button). Fields keep their contents. Card stays. |
| F | `ssl://`-style Electrum URL | refused by name, never probed (v1 Esplora-over-http(s) limit, decision 7) | status shows `nodeUnsupportedScheme` (adapted `NON_ESPLORA_URL`, same body, web next step). Card stays. |
| G | engine busy / unreachable | 503 / transport error | existing `watchkeyBusy` / `unreachable` lines, unchanged. |

Failure branches never partially apply: key and node ride one request
(§6-TBC-1), so a failed probe cannot leave a provisioned key with a half-
saved backend. One field failing keeps everything the user typed.

### Dismissal

On success (A or B): the submit disables the fields, the green
`Connected.`-family status line is shown, and the **card dismisses
entirely** when the engine's next `/state` snapshot stops reporting
`needs_watch_key` (own-node path: it dismisses on provisioning, since the
backend is saved with the key in the same request). Dismissal is driven
ONLY by the typed snapshot — the client never infers success from its own
echo (existing rule, kept verbatim). Chat re-enables as the card goes; the
status line's final words are repeated as nothing new — the engine's own
banner/scan surfaces carry the story from there. **No client-authored
post-dismissal narration.** Rationale: the transcript is engine voice (§10
consistency); a client-invented "all set!" line would be the web parsing/
implying state it must not own.

## 3. Settings consolidation

Rows in the existing panel (built from `GET /settings` server-owned
entries, `textContent`-only). New friendly labels come from a client
`KEY_LABELS` map (the raw key stays as the row's data identity, so an
unknown future key still renders — with its raw key as label and the
generic text input, matching the panel's existing tolerance rule).

Row order: **Your wallet's public key · Address gap limit · Your node's web
address**.

### zpub row

- **Display: the FULL key, never truncated, in a read-only selectable
  field, with a Copy button.** Decision against the "last-4 only" option:
  it's a watch-only xpub (not a secret — §9's leak table doesn't even list
  it), and the row's job is cross-checking against the hardware device's
  screen (ADR-0015's fingerprint-trust logic) and re-setting up the device.
  Truncating mid-hash destroys both jobs and violates the §10 full-
  address/copyable rule; a reveal-toggle for a public key is theater.
  It is never logged and never persists client-side beyond the DOM (panel
  rule, unchanged).
- **Edit flow = re-provision, destructive-ish.** Changing the key switches
  what the app watches; the cached balance/history on screen belongs to
  the OLD key. Follows §10's destructive pattern — one obvious confirm
  affordance, always-available cancel, never a silent apply:
  1. User edits the field; Apply becomes `Switch wallet` (the danger
     verb — "Apply" undersells this), and the row status shows
     `zpubSwitchWarn` + a `Keep current` button.
  2. `Switch wallet` → the new key goes through the SAME engine parse+gate
     path as first run (seed/private/testnet refusals value-free, kept).
     Success → status `zpubSwitched`, which says restart (a running engine
     already derived addresses from the old key; the ADR-0018 config-only
     honesty applies a fortiori). Failure → engine's value-free line, old
     key untouched.
  3. `Keep current` → row reverts to the stored value, nothing sent.
  - Engine support for replacing a provisioned key is **TBC** (§6-TBC-4 —
    today `POST /watchkey` 409s when a wallet exists). The UI copy is final
    and shippable the moment the endpoint decision lands; if the
    orchestrator declines the endpoint, the row renders read-only (display
    + Copy) with no edit affordance, and this copy stays unused in the
    table.
- **Privacy note (implementer):** showing the full xpub in DOM is fine
  (public, user's own config, token-gated page); echoing it into any
  status line or transcript is not (value-free rule). Copy never repeats
  the key.

### gap_limit row

Existing behavior verbatim: numeric input, inline range check
(`settingsRange`), takes effect on the next scan, `env_override` flag
line. Only change: the friendly label.

### chain_base_url row

Existing behavior (text input, `requires_restart` flag, env-override
line) plus ONE honestly-wrong-string fix, same class as ux-web-copy §3:

- **Replace** `Empty = public default — its operator can link your queries
  to your IP.` **with** `Empty = no server chosen — until a server is set
  or the public box is ticked, the app checks no addresses and balances
  stay empty.` Under ADR-0023 amendment 2 an empty stored rung means
  "never chose", not "public" — the current hint is now false, and it
  hides that the wallet simply won't load. The public path is chosen on
  the warned card (§1), not by clearing this field.
- Saving an address from Settings still needs the first-run-grade probe
  (§6-TBC-2) — the same decision-5 rule, else Settings is a bypass around
  the validation the card does.

## 4. Open-when-unset rule

The settings panel auto-opens on launch when the wallet is not fully
configured, and auto-closes once it is. Trigger set, evaluated on every
typed `/state` snapshot (never inferred from anything else):

**OPEN when** `needs_watch_key === true` OR `backend_unresolved === true`
(new additive boolean, §6-TBC-3 — mirrors the single source of truth
`app._backend_resolved`: no URL on any ladder rung AND no
`chain_backend_choice="public"` marker; the env/file rungs count, so an
operator-exported backend reads as configured and does NOT force-open).

**CLOSE when** the trigger condition stops being true. Exact moments:

1. **Launch** — snapshot arrives: open if triggering, closed otherwise.
   (First-run users see the card AND the open panel behind it: the panel
   shows the same settings at rest; the card is the asking surface. The
   toggle keeps working — a manually-closed panel stays closed for this
   session; auto-open is one-shot per launch, so it never fights the
   user.)
2. **Post-provision (card submit success, path A or B)** — the same
   snapshot that dismisses the card also satisfies the condition → panel
   closes. This is the user's "once set, the settings should close."
3. **Post-clear** — clearing the URL in Settings back to empty makes the
   next snapshot triggering again; the panel re-opens (the wallet is now
   in a hold state the user should see). One re-open per transition, same
   one-shot rule.

Returning user who wiped config (§4's real motivation): condition holds at
launch → panel is open, card is up, nothing to hunt for.

A manually OPENED panel with a triggering condition gets no special
treatment — dismissal only happens via the transition in 2/3.

## 5. Copy table

Voice: ux-web-copy.md rules 1–5 (sentence case; periods on sentences, not
labels; cause + next step; no exclamation marks; terminology locked to the
CLI: "server", "first scan", "mempool.space app"). Leak wording quotes the
USER-APPROVED ADR-0023 amendment-2 language; lines marked (echo) adapt a
signed `onboarding.py` constant so the two transports can never disagree
about what was promised. Proposed `LABELS` keys given for i18n structure.

| # | LABELS key | String | Rationale |
|---|-----------|--------|-----------|
| 1 | `nodeAskTitle` | `Where should the app check your wallet?` | Card section title; the decision framed as the user's (NODE_ASK's opening move), zero jargon, no scare. |
| 2 | `nodeUrlLabel` | `Your server's web address` | Field label; "web address" not "URL"/"endpoint" (§10 no jargon); echoes URL_PROMPT's vocabulary. |
| 3 | `nodeUrlPlaceholder` | `https://mempool.your-node.example/api` | Shape-teaching placeholder (the /api suffix is the actual gotcha); example host is obviously-example, never a real service. |
| 4 | `nodeUrlHint` | `The web address of your node's mempool.space app — its API address is usually the same, with /api at the end. Nothing is saved, and no address of your wallet goes to it, until the app has checked that it answers correctly.` | (echo) URL_PROMPT verbatim minus the "Type" lead (it's a field now); carries the save-later promise that makes probing a remote URL non-leaky. |
| 5 | `nodePublicCheck` | `Use a public node even though it damages my privacy` | USER-APPROVED verbatim label. Subordinate clause IS the warning — visible without opening the page source of the consent; no period (label rule). |
| 6 | `nodePublicNote` | `The app will use the public mempool.space server. Whoever runs it sees every address you check, can link those addresses together and to your IP, and watches when your transactions move. You can change this any time in Settings.` | Replaces hint when ticked. Sentence 2 quotes the amendment-2 user-approved leak wording exactly — consent is only valid where the price is stated, and it's stated while the tick is hot. Last sentence: not a verdict (PUBLIC_CHOSEN_ACK's posture), and on web the switch IS in Settings. |
| 7 | `nodeRequired` | `Enter your server's web address, or tick the public box above.` | The empty-field-vs-checkbox resolution (§1): names both exits, imperative, no blame. Nothing is sent until one exists. |
| 8 | `nodeFailed` | `That address didn't check out: it wasn't reachable, or it didn't answer as a mainnet mempool.space API. Nothing was saved, and no address of your wallet was ever sent to it. Fix the address and try again, or tick the public box.` | (echo) VALIDATION_FAIL; first three sentences verbatim (they're signed); next-step adapted to a form (the CLI's "say retry"/doctor lines have no web equivalent). No silent fallback is implied — the two exits are the only doors. |
| 9 | `nodeUnsupportedScheme` | `That's not an address this app can use yet: it connects only to Esplora-protocol servers over http(s) — the web address of a mempool.space app. Electrum servers (ssl:// and the like) are planned for a later version. Nothing was probed and nothing was saved. Enter an http(s) address, or tick the public box.` | (echo) NON_ESPLORA_URL, same adaptation. Names the limit honestly, never reads as a broken feature. |
| 10 | `connectedPublicLine` | `Connected. The public mempool.space server it is, chosen with eyes open: whoever runs it sees the addresses we check and when your transactions move. You can switch to your own server any time in Settings.` | (echo) PUBLIC_CHOSEN_ACK with the CLI's "/setup" → "Settings"; "Loading your wallet now" is NOT appended — the scan chip (`scanLoading`) already says it, one surface one claim. Green status; card then dismisses (§2). |
| 11 | `connectedOwnLine` | `Connected. Your server is saved. Your wallet isn't loaded yet — it will load from your server the next time you start the app. Until then, no addresses are checked.` | (echo) DEFERRED_RESTART's facts in card-length form: the no-in-session-load honesty is mandatory (the live client isn't the chosen one; loading would be the forbidden silent fallback). "Quit and start again" trimmed — the user just got here; next-launch phrasing matches EFFECTS_NEXT_LAUNCH's posture. |
| 12 | `keyConnectedLine` | `Your wallet key is connected.` | Node-only card variant (§1): states what's done, adds nothing, value-free (never echoes key material or last-4s — mode framing only, per the /setup copy convention). |
| 13 | `settingsZpubLabel` | `Your wallet's public key` | Settings row label; "public" pre-empts the seed-words fear at the one place the full key is on screen; mirrors the card's "public account key". |
| 14 | `settingsCopy` | `Copy` | Button label, standard verb, no period. |
| 15 | `settingsCopied` | `Copied.` | One-word status line takes a period (rule 2); past tense = done, promises nothing about where it went. |
| 16 | `zpubSwitchWarn` | `This replaces the wallet the app watches. The balance and history you see belong to your current key — a new key starts a fresh watch, after you restart.` | The destructive-confirm copy: states the blast radius (old cache = old wallet's) without jargon or scare, and the restart fact in the same breath so the post-apply state can't surprise. |
| 17 | `zpubSwitchConfirm` | `Switch wallet` | The danger verb replaces Apply. Never "Delete" or "Reset" — nothing is deleted (rule 4: honest about what actually happens). |
| 18 | `zpubSwitchKeep` | `Keep current` | The always-available cancel, phrased as what it preserves, not what it refuses. |
| 19 | `zpubSwitched` | `Switched. Quit and start the app again to load the new wallet.` | Post-apply honesty (restart-gated, §3); period per rule 2; no wallet data echoed. |
| 20 | `settingsGapLabel` | `Address gap limit` | Friendly label; the term kept (power-user row, env-var audience) but "address" leads so the noun explains itself. |
| 21 | `settingsNodeLabel` | `Your node's web address` | Friendly label for `chain_base_url`; consistent with the card's field wording (#2). |
| 22 | `settingsEmptyIsDefault` (REPLACES) | `Empty = no server chosen — until a server is set or the public box is ticked, the app checks no addresses and balances stay empty.` | Honestly-wrong fix (§3): amendment 2 made empty ≠ default-public; old line would advertise a fallback that no longer exists. New placeholder short form (#23) keeps the field legible. |
| 23 | `settingsEmptyPlaceholder` (REPLACES) | `Empty = no server chosen` | Short form inside the field; same fix, truncation-safe split (the two-constant structure ux-web-copy §4(a) predicted). |
| 24 | `nodeUrlLabelVisuallyHidden` — none | (reuse existing card lede/warning/`Connect`/`watchkeySaving`/`watchkeyConnected`/refusals verbatim) | Explicit non-change: the signed hardware-wallet-only guidance and value-free refusal pins are reused untouched; duplicating or softening them here would fork load-bearing copy. |

**String count: 23 new/changed** (21 new + 2 replaced), 0 of the existing
watchkey strings altered.

## 6. Implementer constraints + endpoint map (no invented endpoints)

Contracts (all pre-existing, restated because this flow adds inputs):
- `textContent`-only rendering for every value incl. URL echoes-back — none
  (server never echoes values; the client re-reads from the fresh
  snapshot/settings entry, as today).
- No inline handlers; delegated listeners like `settingsListEl`/`actionsEl`.
- Every new string is a `LABELS` key (single source, i18n-ready); static
  card markup lives in `index.html` like the existing lede.
- Error display value-free: relay the engine's refusal line verbatim after
  the `watchkeyRejectedPrefix`-style label; invent nothing.
- CSP: the checkbox/URL additions need no new form mechanics (native
  `<input type="checkbox">` + `disabled` toggle — no JS beyond a listener
  that swaps hint text and disables the field).
- State truth stays typed-snapshot-only: card visibility, dismissal, and
  panel open/close all read `/state`; the client never infers from local
  form state.

Interaction → endpoint map:

| Interaction | Maps to |
|---|---|
| Key field + validation errors | `POST /watchkey` (exists; engine parse+gate, value-free refusals) |
| Public checkbox success path | same POST, consent recorded `chain_backend_choice="public"` → **TBC-1** |
| Own-URL success path | same POST, URL probe (`check_backend`) + store write on the engine thread → **TBC-1/TBC-2** |
| zpub display/Copy | `GET /settings` gains the key as a read-only entry → **TBC-5** |
| zpub switch | re-provision path → **TBC-4** |
| gap_limit / chain_base_url rows | `GET/POST /settings` (exist; allowlist `gap_limit`, `chain_base_url`) |
| Panel open-when-unset | `/state` booleans → **TBC-3** |

**TBC list (orchestrator decisions; nothing below is invented here):**

1. **Public-consent transport.** Recommended: extend the `POST /watchkey`
   payload with a required-by-card `backend` envelope —
   `{"key": …, "backend": {"mode": "public"} | {"mode": "url", "url": …}}`
   — processed on the engine thread inside the same provision step (one
   atomic apply, §2's no-partial-state property, and consent is written by
   the warned code path, honoring amendment 2's "only the warned
   conversation writes the marker" principle now that the warned card IS
   the web conversation). Alternative rejected here: putting
   `chain_backend_choice` in the `/settings` allowlist — a generic scalar
   write with no leak-warning beside it is precisely the silent-consent
   amendment 2 forbids. Either way the ADR-0023 note "deliberately NOT in
   the web /settings allowlist / no consent surface in the browser" is
   superseded by user direction and needs an ADR line by the orchestrator.
2. **Probe on the web path.** First-run (§2) and Settings (§3) must run
   the same decision-5 `check_backend` validation before saving any URL.
   Today `POST /settings` `chain_base_url` is shape-validated only (store
   writer), and no web path reaches `check_backend`. Needs: engine-side
   probe on the settings write path + first-run save (fail-closed,
   value-free errors; never saved-then-probed).
3. **`/state` boolean `backend_unresolved`** (additive under `state/1`,
   mirrors `app._backend_resolved`) — drives the node-only card variant
   and the panel open-when-unset rule.
4. **Re-provision endpoint.** Replacing a provisioned key today 409s on
   `POST /watchkey`. Needs an engine decision (a confirm-gated re-key
   command vs. restart-required file/flag path). Copy #16–19 is ready;
   ship the row read-only until this lands.
5. **Watch key in the `GET /settings` snapshot.** A new allowlisted
   display entry (read-only, full value, `requires_restart: true`-style
   flag optional). It is watch-only public data, so it does not violate
   the settings surface's value-free rule (that rule exists for wallet
   data — addresses/amounts — not for the user's own public key), but the
   "never log xpubs" rule must be restated for the response-body path.

---

*Decisions this doc owes the orchestrator (summary):* checkbox default
UNCHECKED with no silent fallback (empty+unticked = inline exit-list,
never submitted); dismissal = freeze fields, green Connected-line, remove
card on the next snapshot, no client-authored transcript narration;
full-key display with Copy in Settings (no truncation), edit = confirm-
gated re-provision behind TBC-4; consent rides the watchkey payload
(recommended, TBC-1).

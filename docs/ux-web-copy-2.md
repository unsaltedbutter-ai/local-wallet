# UX copy pass 2 — web UI (post-TCK-WEB-009 / LAUNCH-001..003)

Owner: UX writing. Branch: `dev/plan-run-1`. Companion to
`docs/ux-web-copy.md` (voice rules 1–5 — this pass follows them) and
`docs/ux-first-run-web.md` (whose §3/§5 recommendations are partially
OBSOLETED by what actually shipped; see §4 flag 1 and §6). No code changed
here; new keys are named for i18n structure and flagged as implementer work.

Inventory scope (all read at HEAD): the full `LABELS` map + inline status
strings in `src/localwallet/ui/web/static/app.js`, the markup strings in
`src/localwallet/ui/web/static/index.html`, the watch-key replace rungs,
the backend-switch copy in `src/localwallet/ui/onboarding.py`
(`SWITCHING_NOW` / `SWITCH_AFTER_SCAN` / `WEB_SETUP_HINT` / `BACKEND_PROBE_FAIL`),
and the model-card constants in `src/localwallet/app.py`
(`MODEL_CARD_*`, `MODEL_DL_*`, `MODEL_DECLINED_LINES`, `MODEL_PRELOAD_NOTICE`,
`MODEL_INTEGRITY_WARNING`, `NO_MODEL_DEMO_BANNER`). Leak wording quotes the
user-approved ADR-0023 amendment-2 language.

---

## 1. The replace-affordance DECISION (watch key, SET state)

**Decision: adopt the chain-base Edit⇄Apply pattern on the zpub row.**
The SET state today is a collapsed read-only display with no action at all,
yet the client's 409 → confirm → apply rungs (`replaceStage`) and the
engine's `replace:true + confirm:true` double opt-in are fully wired. The
result: a provisioned user on the web **cannot switch wallets** — the
replace path is unreachable because there is no way to submit a new key.
That is a dead end, not a design. One Edit button reopens the existing
machinery; nothing new is built, no endpoint is added, and the destructive
shape stays engine-gated (an LLM "yes" never counts; the confirm rung is the
user's own click).

Flow (all rungs except the Edit click already exist in `app.js`):

1. **SET row**: the engine's truncated display (unchanged — Show/Copy/Replace
   stay removed per user direction; a replace never needs the old value) +
   an `Edit` button (reuses `settingsEdit`, "Edit" — same verb as the
   chain-base row; the confirm rung, not the entry verb, carries the weight).
2. **Edit click** → the row flips to the form (no request, mirrors the
   chain-base Edit rung), with replace-mode copy:
   - `watchkeyReplaceLede` (NEW):
     `Paste the public account key (xpub, ypub, or zpub) of the wallet you
     want to watch instead.`
   - `watchkeyWarning` — reused verbatim (load-bearing hardware-wallet-only
     copy; never fork it).
   - Submit button `watchKeyReplaceSubmit` (NEW, replaces "Connect" in this
     mode): `Replace wallet` — honest about what the click aims at; it is
     still not the destructive confirmation (the engine's 409 raises that).
   - `Cancel` (reuses `watchKeyReplaceCancel`): collapses the row back to
     the display; nothing sent. Always available.
3. **Submit** → plain `POST /watchkey`; the engine answers `already` (409)
   → the EXISTING confirm rung shows `watchKeyReplaceConfirm` + danger
   `Replace` + `Cancel`. Confirmed → existing apply rung
   (`replace:true, confirm:true`) → collapse to display, `Connected.` echo,
   fresh `/settings` + `/state` reads — all already implemented.

Reword of the confirm rung (the flow stays; "watch key" in the closing
question is internal vocabulary — the pane teaches "public account key"):

> The cached balance and history belong to the current wallet; replacing
> discards any pending transaction and re-scans for the new one (the old
> wallet's data stays on this machine, unused). Replace the wallet's key?

("stays in the store" → "stays on this machine": "store" is the SQLite
module's name, not the user's.)

## 2. Inventory — current → proposed (or keep) → rationale

### 2a. Header / connection / transcript (unchanged since pass 1 unless noted)

| # | Current (key) | Proposed | Rationale |
|---|---|---|---|
| 1 | `Local Wallet` (brand/title) | keep | Brand. |
| 2 | `Settings` (toggle + panel aria) | keep | Pass-1 rule. |
| 3 | `Connecting…` / `Connected` / `Reconnecting to <origin> …` | keep | Pass 1 signed; ellipsis rule holds. |
| 4 | 401 line `This page's session no longer matches the wallet — reload this page.` | keep | Pass-1 fix, shipped as drafted. |
| 5 | no-token line `This page wasn't opened from your wallet — …` | keep | Pass-1 fix, shipped as drafted. |
| 6 | `Wallet loading — balances may be incomplete until the first scan finishes.` (scanLoading) | keep | Pass-1 fix; matches CLI. |
| 7 | `First scan failed — balances may be incomplete.` (scanSkipped) | keep | Pass-1 priority fix, shipped. |
| 8 | `Messages you send and replies from your wallet appear here.` (hint) | keep | Pass-1 fix. |
| 9 | `Ask your wallet…` / `Send` / `Working…` / `Wallet` / `You` | keep | Pass 1. |
| 10 | `queued` (queuedTag) | keep | Lowercase tag rule. |
| 11 | `Reconnected — some earlier messages may be missing.` (resyncGap) | keep | Pass-1 fix. |
| 12 | `Could not reach the wallet server — is it still running? Check the terminal…` (unreachable) | keep | Pass-1 fix. |
| 13 | aria `Wallet conversation` / `Flow actions` / `Settings` (panel) | keep | Screen-reader names, accurate. |

### 2b. Header quick bar (NEW surface — flagged)

| # | Current | Proposed | Rationale |
|---|---|---|---|
| 14 | `BTC Balance` (sends `/balance`) + `USD Balance` (sends the SAME `/balance`) | **one button `Balance`**, title `Sends "/balance"` | Two buttons, one identical deterministic turn — the USD button over-claims a distinct USD action while the engine answers both with the same dual-unit card (which itself carries BTC/sats + USD + rate timestamp — the §10 rule the card already satisfies). If the orchestrator keeps two buttons for discoverability, the minimum honest fix: `USD Balance` → retitle `title='Sends "/balance" — the reply shows USD too'`. Recommend one button. |
| 15 | nav aria `Quick balance actions` | `Balance actions` (after #14) | Drops "quick" (internal ticket vocabulary) and plural once collapsed. |

### 2c. Settings panel — generic rows

| # | Current (key) | Proposed | Rationale |
|---|---|---|---|
| 16–22 | `Loading…` / `Could not load settings — the wallet is busy or unreachable.` / `Apply` / `Saving…` / `Applied.` / `Rejected — no reason given.` / `Rejected:` | keep | Pass-1 set, shipped as drafted. |
| 23 | `The wallet is busy — try again.` (settingsBusy) | keep | Pass-1. |
| 24 | `Could not save — try again.` (settingsFailed) | keep | Pass-1. |
| 25 | `Takes effect after restart.` (settingsRestart) | keep | Pass-1. |
| 26 | `Empty = public default` (settingsEmptyPlaceholder, in-field) | keep | Short form; see flag 1 — the shipped pair is the CORRECT one post-TCK-BACKEND-002. |
| 27 | `Empty = public default — its operator can link your queries to your IP.` (settingsEmptyIsDefault, hint under field) | keep (+ new apply-time note, #52) | The prior spec's replacement ("Empty = no server chosen — …") is now FALSE on the web path: an empty write hot-swaps the live client to the public default in-session (no restart). The leak half-sentence carries the ADR-0023 amendment-2 duty while the hint is visible; what's missing is the beat AFTER an empty apply (#52). |
| 28 | `Set via environment variable — edit there or remove it.` (settingsEnvOverride) | keep | Technical audience, precise. |
| 29 | `Enter a whole number between {min} and {max}.` (settingsRange) | keep | Pass-1 fix. |
| 30 | section heads `Wallet` / `Network & scanning` | keep | Plain, sentence case, matches row contents. |
| 31 | raw key labels in rows (`watch_key`, `chain_base_url`, `gap_limit`) | keep | Dev-visible keys, but they ARE the settings identity and the friendly copy sits in each row's lede/hint; `settingsZpubLabel`-style renames were superseded by the user's collapsed-display direction. Revisit only if a non-technical support report ever points at "gap_limit". |

### 2d. zpub section (TCK-WEB-009 a/b)

| # | Current (key) | Proposed | Rationale |
|---|---|---|---|
| 32 | watchkeyLede (`Enter the wallet's public account key — an xpub, ypub, or zpub — to begin. …`) | keep | Signed first-run guidance, device names included; the pass-1 rule "reuse verbatim" holds. |
| 33 | watchkeyWarning (`This is not your seed words. … Mainnet keys only — testnet keys are refused.`) | keep | Load-bearing hardware-wallet-only copy; never fork. |
| 34 | `Public account key` (watchKeyInputLabel, aria) | keep | Teaches the safe term. |
| 35 | `Connect` (watchKeyConnect) | keep (ENTRY) — NEW `watchKeyReplaceSubmit` in replace mode (§1) | First-run verb stays; replace mode must not read like first setup. |
| 36 | `Connecting…` / `Connected.` / `Not accepted:` / `The wallet is busy — try again.` / `Could not connect — try again.` (watchkeySaving/Connected/RejectedPrefix/Busy/Failed) | keep | Pass-1 pins + error-pattern compliant. |
| 37 | `Status unknown — reconnecting…` (watchKeyRowUnknown) | `Wallet status unknown — waiting for the server to reconnect…` | "Status" alone is unanchored (whose status?); name the subject. Marginal but this is a first-glance state on flaky starts. |
| 38 | `Connected.` (watchKeyRowConnected, display fallback) | keep | Matches the transcript echo, one beat one phrase. |
| 39 | watchKeyEnvOverride (`The key was set via environment variable — a replacement takes effect once that override is removed.`) | keep | Cause + condition + exit, technical audience. |
| 40 | watchKeyReplaceConfirm | reword — see §1 | "watch key" internal vocab; "store" → "on this machine". |
| 41 | `Replace` / `Cancel` (watchKeyReplaceYes/Cancel) | keep | Danger verb + always-available exit, exactly the destructive pattern. |
| 42 | NEW: `Edit` on the SET row, `watchkeyReplaceLede`, `watchKeyReplaceSubmit` | §1 copy | Reopens the already-wired replace rungs; zero new endpoints. |

### 2e. Backend badges (TCK-WEB-009 e)

| # | Current | Proposed | Rationale |
|---|---|---|---|
| 43 | badge words `mempool` / `electrum` / `bitcoind` | keep | The engine's family names, lowercase like `queued`; inventing "your node"/"public explorer" per badge would fork terminology. |
| 44 | dim vs lit = **nothing textual** (CSS only; container aria-label is the flat string `mempool electrum bitcoind`) | NEW legend line `badgeLegend`: `The highlighted name is the kind of server the app asks about your addresses.` + per-badge dynamic `title`/`aria-label`: lit → `badgeInUse`: `In use — the app checks your addresses against this kind of server.`; dim → `badgeIdle`: `Not in use.`; dim bitcoind → `badgeReserved`: `Not available yet — the app cannot connect to a plain Bitcoin Core address.` | PRIORITY: a state-carrying indicator with no text alternative is an accessibility failure AND a comprehension failure — "dim vs lit" currently tells a first-time reader nothing about which server is live. The legend names the mapping; the per-badge title/aria states each one's role; `bitcoind` needs its own line because a permanently-dim badge otherwise reads as a bug ("is my Core node not being detected?") when the real fact is ADR-0023 decision 7: the app cannot speak Core's RPC. `paintBackendBadges()` recomposes the labels (they are already the only function touching the badges). |

### 2f. Resync now + applied-write notes (TCK-WEB-009 f/g)

| # | Current (key) | Proposed | Rationale |
|---|---|---|---|
| 45 | `Resync now` (resyncNow) | keep | Verb-first; "resync" is glossed by the results. |
| 46 | `Starting…` (resyncSaving) | keep | Present-continuous ellipsis rule. |
| 47 | `Re-scan started — the scan chip above shows progress.` (resyncStarted) | `Re-scan started — watch the status line at the top of the page for progress.` | "scan chip" is the developer's component name; the next step must be followable by someone who has never read a ticket. |
| 48 | `Already scanning — try again once the current scan finishes.` (resyncBusy) | keep | Cause + next step, textbook. |
| 49 | `Re-scan is not available right now.` (resyncUnavailable) | `Re-scan is not available right now — nothing was changed. Try again once the wallet has finished loading.` | Bare dead-end: the §10 error pattern demands a next step, and "nothing was changed" forecloses the "did it half-start?" worry. |
| 50 | `Saved — re-scanning with the new value; the scan chip above follows.` (resyncNoteStarted) | `Saved — re-scanning with the new value; the status line at the top follows.` | Same "chip" fix as #47. |
| 51 | `Saved — a scan is already running; it will use the new value.` (resyncNoteBusy) | keep | Honest (the regular scan does pick the new value). |
| 52 | NEW apply-time note for an applied EMPTY chain-base write (`settingsEmptyApplied`): `Switched to the public mempool.space server — whoever runs it sees every address you check, can link those to your IP, and watches when your transactions move. Enter your own server's address to switch back.` | client-side: the chain-base Apply rung knows the value it sent was empty (the submitted value is already in the DOM — relaying it would break the value-free rule, but an EMPTY-vs-NOT-EMPTY test adds no value); the engine's reply is otherwise `resync: "started"` with no leak beat | UNDER-WARN PRIORITY: clearing the field and pressing Apply silently routes every future address query to the public server — the exact leak ADR-0023 amendment 2 says consent must ride. The hint line (#27) sits above it, but the confirm-of-action beat must name the consequence AS IT LANDS. The last sentence gives the exit (never a verdict posture). |
| 53 | `Saved — the re-scan is queued behind the current scan.` (resyncNoteDeferred) | keep | Plain, mechanical, true. |
| 54 | `Saved — your environment configuration outranks this one; it applies at next restart.` (resyncNoteSkipped) | keep | Names the shadowing rung and the honest delay; matches `settingsRestart` posture. |
| 55 | `Already up to date — no re-scan needed.` (resyncNoteUnchanged) | keep | Says why no scan followed. |
| 56 | `Saved — no re-scan could start right now.` (resyncNoteUnavailable) | keep | Value saved (true), re-scan honestly declined; unlike #49 the "Saved" lead already frames it and a plain-Apply row can carry no more without noise. |

### 2g. Model card + quick actions (TCK-LAUNCH-002) — web side

| # | Current | Proposed | Rationale |
|---|---|---|---|
| 57 | `Yes, download the model` | keep | Answers the card's question in the card's words. |
| 58 | `No, show model-free actions` | `No — show what works without the model` | "model-free" is ticket vocabulary ("model-free quick actions" appears nowhere the user has been told); the button should promise the outcome in plain words, and "without the model" is the phrase the engine's own decline lines teach. |
| 59 | `Show balance` (/balance) | keep | Verb-first, unambiguous. |
| 60 | `Show receiving address` (/receive) | keep | The common ask, next-unused. |
| 61 | `New address` (/address) | `Create new address` + title `Sends "/address" — allocates a fresh address instead of reusing the next unused one` | #60/#61 are the one confusable pair on screen (both "an address"); the verb split (Show vs Create) plus the disambiguating title is the minimum honest fix. Add matching title `Sends "/receive"` to #60 (pass-1 quote-the-utterance pattern). |
| 62 | `Open settings` (qa-settings, client-only) | keep | It opens the panel; no utterance, no title needed. |
| 63 | `Downloading the model` + ` {pct}% — {x} of {y}` (modelDownloading + renderModelProgress composition) | keep | Percent + bytes only, path-free, value-free by construction; `<progress>` + textContent is honest. (Binary KiB/MiB units stay — precision over cuteness, and the manifest numbers are the same units.) |

### 2h. Engine constants the web renders (proposals = flags for app.py/onboarding.py owners; NOT silent changes)

| # | Current | Proposed | Rationale |
|---|---|---|---|
| 64 | `Model hasn't been downloaded. Want to download now?` (MODEL_CARD_QUESTION) | `The AI model hasn't been downloaded yet. Want to download it now?` | "Model" bare is the first thing a user reads and the least-explained noun in the app; "AI model" anchors it (this is the voice: knowledgeable friend). Sentence otherwise fine. |
| 65 | MODEL_CARD_HINT (Answer 'yes' … /download.) | keep | Names both exits and the re-arm path; matches button copy. |
| 66 | `Downloading the model now — verified against its pinned hash before install; progress appears here.` (MODEL_DL_STARTED) | `Downloading the model now — it will be checked against its official fingerprint before it is installed. Progress shows here.` | "pinned hash" is developer vocabulary; "official fingerprint" carries the same security fact to a human. Split the two facts into two sentences. |
| 67 | `The model download is already in progress.` (MODEL_DL_RUNNING) | keep | Status, true, nothing more. |
| 68 | `Model downloaded and verified. It activates the NEXT time you start local-wallet (this session keeps running without it).` (MODEL_DL_DONE) | `Downloaded and verified. The model takes over the next time you start the app — this session keeps running without it.` | ALL-CAPS emphasis breaks pass-1 rule 1; "start local-wallet" forks the brand usage (everywhere else: "the app", "start local-wallet and open the address" — the parenthetical already carries the honesty beat, kept verbatim in sense). |
| 69 | MODEL_DL_FAILED (…an unfinished partial file is kept for the next attempt to resume…) | keep | Best line in the set: cause, what wasn't installed, the resume fact, both exits. |
| 70 | MODEL_DECLINED_LINES (5 lines) | keep | Code-owned, matches the buttons after #58. |
| 71 | `Loading the model in the background — your first question may wait for it.` (MODEL_PRELOAD_NOTICE) | keep | Honest hedge ("may") on the shared-build-lock wait. |
| 72 | `model file failed its integrity check — re-download recommended` (MODEL_INTEGRITY_WARNING) | `The model file failed its integrity check — it may be corrupted. Run /download to fetch a fresh copy.` | Starts lowercase like a log line, not a sentence (rule 1); passive "recommended" has no actor and no next step (rule 3); names the fixable cause ("corrupted") and the exact command. |
| 73 | `that backend did not check out: it is unreachable, or it does not serve mainnet as an Esplora (http(s)) / Electrum (ssl://) server — nothing was saved and the current backend stays in service` (BACKEND_PROBE_FAIL, shown after the web's `Rejected:` prefix) | add the gloss the CLI already gives: `…as an Esplora (mempool.space-style) http(s) or Electrum (ssl://) server…` | "Esplora" bare is unexplained jargon (§10); CLI `VALIDATION_FAIL` already glosses it "(mempool.space)" — the two transports must teach the same word the same way. Otherwise keep verbatim: it's value-free, says nothing was saved, and names the standing backend. |
| 74 | `Switched — the app asks your new server from here on, and I'm reloading your wallet from it now (your coin tags stay put).` (SWITCHING_NOW) | keep | ADR-0023 §9 voice; "coin tags" is taught vocabulary at this point in the journey; the parenthetical is the data-safety beat. |
| 75 | `Saved — the app switches to your new server and reloads your wallet the moment the scan in progress finishes (your coin tags stay put).` (SWITCH_AFTER_SCAN) | keep | Same; the concurrency rule stated in plain words. |
| 76 | WEB_SETUP_HINT (long launch-time unresolved line) | keep | Long, but it is the entire leak contract + both fixes + the hot-swap honesty in one honest block, CLI-only surface. |
| 77 | NO_MODEL_DEMO_BANNER (no-manifest fallback) | keep | Only reachable by source installs without the tracked manifest — an operator string, env-var named on purpose. |
| 78 | model card Yes/No while `model_state === "loading"` (preload) → no card, no quick actions, notice #71 narrates | keep (correct by construction) | `MODEL_CARD_STATES`/`MODEL_QUICK_STATES` both exclude `loading`; the notice is the only line, and it promises nothing beyond "may wait". Verified, nothing to change. |

## 3. Consistency rules — delta vs `docs/ux-web-copy.md`

1. **No component names.** A next step must name what the user can see
   ("the status line at the top"), never what the ticket calls it
   ("scan chip"). Extends rule 3's "concrete next step".
2. **Indicators need words.** Any state carried only by styling (dim/lit,
   color, opacity) gets a text alternative — a legend line, a `title`, and a
   recomposed `aria-label`. Styling is not copy.
3. **`model-free` is banned user-facing** (internal shorthand); say "without
   the model", the phrase the engine's decline lines already teach. Rule 5's
   lock extends: web buttons may only reuse words the engine's rendered
   output uses.
4. **One utterance, one button.** A button whose click is identical to
   another's must be collapsed or honestly titled about the shared result
   (the BTC/USD pair). "Sends \"…\"" titles extend to every utterance button,
   new surfaces included.
5. **Empty-write honesty.** Where a settings write of "nothing" routes
   traffic to the warned public server, the apply-time beat repeats the leak
   — the placeholder hint is not consent theater (ADR-0023 amendment 2's
   "consent where the leak is named" generalized to mutations).

## 4. Honestly-wrong flags (priority order)

1. **UNDER-WARN — empty chain-base Apply silently switches to the public
   server** (#52). Post-TCK-BACKEND-002, clearing the field and applying
   installs the public-default client and re-scans immediately; the only
   leak mention sits above as a placeholder hint the user may never have
   read. Ship `settingsEmptyApplied`. NOTE FOR ORCHESTRATOR: the reverse
   also matters — `docs/ux-first-run-web.md` §3/#22 proposes replacing
   `settingsEmptyIsDefault` with "Empty = no server chosen — … balances stay
   empty"; that claim is now FALSE on the web write path. **Do not apply the
   older replacement**; the shipped string + the new apply-note is the
   correct pair.
2. **DEAD END — the replace flow is unreachable in the SET state** (§1).
   Engine + client rungs exist, no affordance opens them; a user who wants
   to watch a different wallet currently must wipe state or use the CLI.
3. **OVER-CLAIM — `USD Balance` is the same turn as `BTC Balance`** (#14).
   Two buttons, one identical reply; the labels promise distinct actions
   that don't exist. Collapse to one `Balance` button (the card already
   carries both units + the rate timestamp).
4. **ACCESSIBILITY FAIL — dim/lit badges are styling-only** and the
   container's aria-label (`"mempool electrum bitcoind"`) claims nothing
   (#43–44); a screen-reader user learns nothing about which backend is
   live, and the permanently-dim `bitcoind` badge reads as a detection bug
   instead of "not supported yet".
5. **NO NEXT STEP — `Re-scan is not available right now.` (#49) and
   `model file failed its integrity check — re-download recommended` (#72)**
   both stop at the verdict; §10 requires cause + next step.
6. **JARGON-AT-FIRST-GLANCE — "Model hasn't been downloaded" (#64),
   "pinned hash" (#66), "model-free" (#58), "Esplora" un-glossed on web
   (#73), "watch key" mid-confirm-sentence (#40).** Individually small;
   together they are the difference between a card a user reads and one
   they trust.

## 5. Restraint — explicit non-changes

- Kept every pass-1 fix that shipped as drafted (13 strings, rows 4–12 etc.)
  rather than re-polishing; churn costs eval fixtures and memory.
- Badge family words stay (`mempool`/`electrum`/`bitcoind`) — inventing
  friendly per-badge names would fork the engine's family mapping for zero
  added truth; the legend does the explaining.
- The raw settings keys stay as row labels (row 31) — the collapsed,
  never-editable zpub display, the Edit⇄Apply chain row, and the bounded gap
  row each carry friendly copy where it matters.
- No Show/Copy returns for the watch key (user direction stands — and the
  replace flow §1 genuinely does not need the old value).
- `settingsSectionWatchKey` stays `Wallet` (not "Your wallet's public key"
  from the prior spec) — section head + row + lede already say it once each.
- No new states, no new endpoints, no re-architected cards. The two new
  COPY surfaces (#42, #52) reuse existing rungs and an existing client-side
  fact (empty-vs-not).

## 6. Tally

- **Inventoried:** 78 distinct strings/rows (52 `LABELS` keys, 5 inline
  status strings, ~16 markup strings, ~10 engine constants; aria-labels
  counted where they carry meaning).
- **Changed (web-side proposals):** #37, #40, #47, #49, #50, #58, #61 +
  quick-bar collapse #14 (2 buttons → 1) + tooltips #60/#61 = **9**.
- **New strings proposed:** `watchkeyReplaceLede`, `watchKeyReplaceSubmit`,
  SET-row `Edit` (reuses existing key), `badgeLegend`, `badgeInUse`,
  `badgeIdle`, `badgeReserved`, `settingsEmptyApplied` = **7 new keys**
  (one Edit affordance reuses `settingsEdit`).
- **Engine-constant rewords flagged (not applied here):** #64, #66, #67,
  #72, #73 = **5** (app.py/onboarding.py owners decide; every proposal is
  same-fact-same-shape, so eval fixtures touch wording pins only).
- **Kept:** the remaining **~61**, including all pass-1 fixes, the whole
  signed onboarding block (SWITCHING_NOW / SWITCH_AFTER_SCAN /
  WEB_SETUP_HINT / GREETING family), gate buttons and titles, and the model
  card's Yes button, progress renderer, decline lines, failure line, and
  notice.

*Notes for the orchestrator:* (a) implementer items §1 (Edit affordance +
replace-mode form — the rungs exist), #52 (client already knows the applied
value was empty; no server field needed), #44 (recompose badges'
title/aria in `paintBackendBadges` — already the sole painter), #14 (markup
only). (b) The prior spec's replacement of `settingsEmptyIsDefault` must be
struck from its §5 or someone will re-apply it — flag 1 explains why it is
now false. (c) All new strings are `LABELS` keys or named constants;
i18n-ready; no prose parsed, no values echoed anywhere (the empty-write
note tests emptiness, never content). (d) #37 (`watchKeyRowUnknown`) renders only in the pre-first-snapshot
window; the reword is marginal — drop it if the fixture diff isn't worth it.

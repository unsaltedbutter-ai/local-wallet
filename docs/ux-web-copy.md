# UX copy pass — web UI (TCK-WEB pass)

Scope: every user-visible string in `src/localwallet/ui/web/static/index.html` and
the `LABELS` map / injected labels in `app.js`. Voice reference: the CLI narration
constants in `app.py` (`WALLET_LOADING_REFUSAL`, `FRESHNESS_NOTE`, the card lines —
`Pending — say "sign" … or "cancel" to discard.`, `_CARD_RATE_CEILING`, etc.) and
PROJECT.md §10. `docs/ux-tx-card-feedback.md` has no §9/§10; its appendix string
block was the voice check. No code changed; no new strings beyond rewordings.

## 1. Current → proposed

### Header / connection status

| Current | Proposed | Rationale |
|---|---|---|
| `Local Wallet` (title + brand) | keep | Brand name; nothing to fix. |
| `Settings` (toggle button + panel aria-label) | keep | Plain, universal. |
| `Wallet conversation` (transcript aria-label) | keep | Screen-reader-only; accurate. |
| `Flow actions` (actions aria-label) | keep | Screen-reader-only; accurate. |
| `Connecting…` | keep | Status word, no blame, matches CLI's calm present-continuous style. |
| `Connected` | keep | Status words carry no period (rule 1). |
| `Reconnecting…` | keep | Honest — promises an attempt, not success. |
| `Not authorized — reload this page.` | `This page's session no longer matches the wallet — reload this page.` | "Not authorized" is legalese and implies an account/security problem; the real cause is a stale per-launch token. Next step already correct; reload genuinely fixes it (the server injects a fresh token per response). |
| `No session token — open the page served by local-wallet.` | `This page wasn't opened from your wallet — start local-wallet and open the address it prints.` | "session token" is internal jargon; the sentence named the cause but gave no path a non-developer can follow. |

### Transcript and compose

| Current | Proposed | Rationale |
|---|---|---|
| `Turns you send and lines from your wallet appear here.` | `Messages you send and replies from your wallet appear here.` | "turns" is engine jargon (the protocol's internal word); §10: zero jargon without explanation. |
| `Wallet` / `You` (turn role tags) | keep | Second-person, matches the CLI's "your wallet" address. |
| `queued` (tag on a held submission) | keep | Lowercase tag, honest FIFO disclosure, disappears on promotion. |
| `Message` (visually-hidden input label) | keep | Accessible name; fine. |
| `Ask your wallet…` (input placeholder) | keep | Exactly the voice: second person, calm, inviting. |
| `Send` | keep | Standard verb; no ambiguity. |
| `Working…` | keep | True (a turn is in flight); no over-claim of what stage. |

### Action buttons (labels live in index.html)

| Current | Proposed | Rationale |
|---|---|---|
| `Confirm` | keep | The gate word, verbatim. |
| `Sign` | keep | Matches the CLI's `say "sign"` gate-merge phrase. |
| `Cancel` | keep | Escape hatch; must stay short and always visible — a destructive *exit* needs no extra warning. |
| `Retry signing` | keep | More explicit than the CLI's bare "retry"; good here. |
| `Faster fee` / `Slower fee` | keep | Mirror the CLI offer's words "faster" / "slower"; "fee" disambiguates what gets faster. |
| `Sends: confirm` / `Sends: sign` / `Sends: cancel` / `Sends: retry` / `Sends: faster` / `Sends: slower` (tooltips, 6) | `Sends "confirm"` / `Sends "sign"` / `Sends "cancel"` / `Sends "retry"` / `Sends "faster"` / `Sends "slower"` | Keep quoting the literal utterance, but in quotes: the colon form left it ambiguous whether the quoted word is what's sent or a label. Quotes = "this exact word" is the CLI convention (`say "sign"`). |

### Event stream / errors (LABELS)

| Current | Proposed | Rationale |
|---|---|---|
| `Reconnected — some earlier events may be missing.` | `Reconnected — some earlier messages may be missing.` | "events" is wire-protocol vocabulary; what the user lost, if anything, are transcript lines. Honesty level unchanged — still "may". |
| `Could not reach the wallet server. Is it still running?` | `Could not reach the wallet server — is it still running? Check the terminal where you started it.` | The question alone is a diagnosis without a next step (§10 error pattern); the terminal is where the answer lives. |
| `Wallet loading — balances may be stale until the first scan completes.` | `Wallet loading — balances may be incomplete until the first scan finishes.` | "stale" implies old-but-present; the truth (per `FRESHNESS_NOTE`: "cache may be incomplete") is missing data. Aligns the chip with the CLI's exact wording. |
| `Scanning skipped.` | `First scan failed — balances may be incomplete.` | PRIORITY FIX, see §3. "Skipped" implies a deliberate choice by someone; in the code (`mark_skipped`) it means the scan attempt *errored out*. Named cause + consequence. |

### Settings panel (LABELS)

| Current | Proposed | Rationale |
|---|---|---|
| `Loading…` | keep | Standard status word. |
| `Could not load settings — the wallet is busy or unreachable.` | keep | Honest about the uncertainty ("or"); retry is implicit by reopening the panel. |
| `Apply` | keep | Verb-first, matches "Send". |
| `Saving…` | keep | Accurate present-continuous. |
| `Applied.` | keep | Period fine for a one-word status *line* (rule 1). |
| `Rejected.` (bare, no reason from server) | `Rejected — no reason given.` | A one-word past participle reads as a verdict without explanation; saying the reason is missing is honest and tells the user the silence is on the wallet, not them. |
| `Rejected:` (prefix + server's reason) | keep | The composed line (`Rejected: <server reason>`) already follows cause+detail. |
| `The wallet is busy — try again.` | keep | Cause + next step, textbook. |
| `Could not save — try again.` | keep | Cause + next step. |
| `Takes effect after restart.` | keep | Terse but unambiguous in a settings row. |
| `Empty = public default.` (placeholder + hint, one constant) | `Empty = public default — its operator can link your queries to your IP.` | PRIORITY FIX, see §3. Under-warns on the one genuinely privacy-relevant fact in the UI. Note for the orchestrator: this constant fills BOTH the input placeholder and the hint line; if placeholder truncation matters, split into two constants (`…public default` short form in the field, full line under it) — same strings, one structural tweak. |
| `Set via environment variable — edit there or remove it.` | keep | The audience for an env-locked setting is technical; precise and actionable as-is. |
| `Must be a whole number between {min} and {max}.` (settingsRange) | `Enter a whole number between {min} and {max}.` | Imperative gives the correction as a next step instead of a scolding; content identical. |

## 2. Consistency rules applied

1. **Capitalization:** sentence case for every string; no ALL-CAPS, no Title Case labels. Lowercase is reserved for the inline `queued` tag (chip, not sentence).
2. **Periods:** complete sentences take a period; button labels and one/two-word status chips (`Connected`, `Send`, `queued`) never do. Ellipsis = something is happening right now.
3. **Error vs status:** errors/rejections = plain cause + concrete next step, second person, no blame; statuses = what is true right now, nothing more. No exclamation marks anywhere; no legalese ("authorized" gone).
4. **Destructive wording:** buttons are the always-available exits — `Cancel` stays blunt and un-scary (it discards an unsigned draft, not money); "discard" (CLI's word) stays the verb for what cancel does, never "delete".
5. **Terminology locked to the CLI:** "wallet loading", "first scan", "balances may be incomplete", "rate" (never "fee rate" in chips), "sat/vB", quoted literal utterances (`say "sign"` / `Sends "sign"`). The web layer never invents a synonym for a word the engine already taught the user.

## 3. Honestly-wrong strings (priority fixes)

1. **`Scanning skipped.`** — actively misleading framing. The state is set only when the scan *errored* (chain/storage failure); "skipped" suggests someone chose to skip, and it hides that balances are likely incomplete. Under-warns where money decisions follow.
2. **`Empty = public default.`** — under-warns. The public default means a third-party explorer sees the wallet's addresses queried from the user's IP; §10 and the privacy-copy rule require saying so, not hinting with the word "public".
3. **`Not authorized — reload this page.`** — over-claims a security posture ("authorized" implies credentials/permissions) and under-explains the benign cause (the page's per-launch token is stale). Confused users at this state are the ones who paste the URL to friends or restart blindly.

## 4. Tally

- Inventoried: 40 distinct strings (tooltips counted per utterance where they differ; 6 tooltip instances = 1 pattern).
- **Changed: 14** (empty hint, 6 tooltips, 401 line, no-token line, `resyncGap`, `unreachable`, `scanLoading`, `scanSkipped`, `settingsRejected`, `settingsEmptyIsDefault`, `settingsRange`).
- **Kept: 26** — including all six action-button labels, the placeholder, and most of the settings panel. The existing base was honest; the pass tightened wording, not voice.

Notes for the orchestrator: (a) `settingsEmptyIsDefault` doubling as placeholder *and* hint is the one case where a reword wants a structural (two-constant) split — flagged, not assumed. (b) Tooltip change is mechanical across the 6 buttons. (c) All strings remain single-source in `LABELS` / markup, ready for i18n extraction; no new keys added.

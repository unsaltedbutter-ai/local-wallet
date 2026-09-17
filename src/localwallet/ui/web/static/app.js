// Local Wallet web client (TCK-WEB-002 + 004 + 005 + 008, LAUNCH-001/002,
// WEB-009). Vanilla ES module — no framework, no build step. XSS contract:
// every dynamic value (all model output) is rendered via textContent ONLY.
// HTML-string sinks are banned here (ADR-0024 §7). Buttons inject canonical
// utterances through POST /action into the FULL engine turn pipeline
// (ADR-0024 §8) — the client can never skip a gate because it never touches
// a handler or the flow. Settings mutations ride POST /settings and
// POST /watchkey (engine-validated); the resync button rides POST /resync
// (TCK-BACKEND-002). TCK-WEB-009: the settings pane is the ONLY first-run
// surface (zpub entry moved into it; the separate card is gone), the set
// watch key collapses to a read-only truncated display, the chain-base row
// is an Edit→Apply read-only cycle with a Resync-now action, and the header
// carries the model-free balance quick actions. TCK-DESCOPE-M3B (user
// direction 2026-09-11): the backend KIND badges are eliminated — the trust
// badge (privacy_mode) is the pane's only badge. TCK-WEB-023 AMENDMENT
// (user MW-17 direction 2026-09-13): the electrum & bitcoind kind pills COME
// BACK near the server field, tinted by the SAME engine-side closed
// privacy_mode classification (GREEN iff own_node_local/own_node_private,
// YELLOW iff public/own_node_remote; none/absent/awaiting → no pill). The
// mempool badge stays gone. Tint NEVER comes from client URL sniffing.
// TCK-WEB-013: the chain row always shows the effective backend URL with a
// privacy_mode-driven trust badge, the empty field is directly typeable
// (Edit/Cancel only once a value exists), the env rung gets one honest
// note instead of the write path, first run stays in the pane under a
// one-time public-default leak beat, and a 401 on any pane POST says
// "reload" rather than "try again".
// TCK-WEB-015: the in-flight indicator is a TRANSIENT pending bubble IN the
// transcript (shown by submit / remote user_text echoes, shared by queued
// turns, removed at a queue-draining turn_end) — the old below-input
// busy element is gone.
// TCK-WEB-028 (static robustness batch): (1) a stuck pending bubble is
// reconciled away by a typed idle state/1 snapshot with an empty local queue
// (mid-turn server death replays no turn_end); (2) /turn + /action failures
// split transport-down from server-rejection copy; (3) the reconnecting
// status is announced on the TRANSITION only, not per backoff attempt;
// (4) the QR dialog (aria-modal) Tab-wraps focus inside itself; (5) action
// buttons disable on click until the next /state repaint restores them.
// TCK-HW-005 static half: the engine's additive ``own_address`` event marks
// which just-shown bubble address is OURS (never client prose sniffing), and
// ONLY that marking arms a small calculator-icon button that injects the
// canonical ``/verifyaddress <branch> <index>`` utterance through the same
// POST /action full-turn path. Hidden unless the typed /state signer_kind
// is "hwi" (file signer / absent = no device to show on).

const island = window.__LOCALWALLET__;
const token = island && typeof island.token === "string" ? island.token : "";

const transcriptEl = document.getElementById("transcript");
const hintEl = document.getElementById("hint");
const statusEl = document.getElementById("conn-status");
const formEl = document.getElementById("turn-form");
const inputEl = document.getElementById("turn-text");
// The NORMAL chat placeholder, read from the markup once (index.html owns
// the copy; TCK-ONB-007 static half flips it only while the key is needed).
const chatPlaceholder = inputEl.placeholder;
const sendBtn = document.getElementById("turn-send");
const scrollerEl = document.getElementById("scroller");
const actionsEl = document.getElementById("actions");
const scanChipEl = document.getElementById("scan-chip");
const scanErrorEl = document.getElementById("scan-error");
const privacyChipEl = document.getElementById("privacy-chip");
const privacySublineEl = document.getElementById("privacy-subline");
// TCK-WEB-027 + TCK-WEB-031 (a): the header TITLE — a click-to-copy chip
// button inside the h1 (index.html markup; hidden until a typed /state
// snapshot carries the closed field).
const walletFpEl = document.getElementById("wallet-fp");
const settingsToggleEl = document.getElementById("settings-toggle");
const settingsPanelEl = document.getElementById("settings-panel");
const settingsHeadingEl = document.getElementById("settings-heading");
const settingsCloseEl = document.getElementById("settings-close");
const settingsRetryEl = document.getElementById("settings-retry");
const settingsStatusEl = document.getElementById("settings-status");
const settingsListEl = document.getElementById("settings-list");
// TCK-WEB-026: the ONE shared click-to-copy live region (role=status,
// aria-live=polite, visually hidden). flashCopyResult writes its value-free
// state sentence here so both copy controls give SR + touch feedback.
const copyStatusEl = document.getElementById("copy-status");
// TCK-WEB-019: the ONE polite live region for SETTLED turn content
// (announceTurn below; separate from the WEB-026 copy region above).
const turnStatusEl = document.getElementById("turn-status");
// TCK-QR-001: the receive-address QR viewer (see the QR section below).
const qrViewerEl = document.getElementById("qr-viewer");
const qrImgEl = document.getElementById("qr-img");
const qrCaptionEl = document.getElementById("qr-caption");
const qrCloseEl = document.getElementById("qr-close");

// The leak sentence, shared verbatim by the pane's public-consent subline
// and the first-run beat (one string, so the two disclosures can never
// drift).
const PUBLIC_LEAK_SENTENCE =
  "whoever runs it sees every address you check, can link those to your IP, " +
  "and watches when your transactions move.";

// One map for every user-facing string this file injects (designer pass —
// button labels live in index.html markup, likewise for rewording).
const LABELS = {
  resyncGap: "Reconnected — some earlier messages may be missing.",
  queuedTag: "queued",
  // TCK-WEB-015: the transient pending bubble's accessible name — the same
  // "Working…" word UX-008 used below the input (relocated, not new copy);
  // the visible bubble is dots-only.
  turnWorking: "Working…",
  // TCK-ONB-007 static half (user correction 2026-09-11): chat is the
  // first-run entry point — while the wallet still needs its key the input
  // stays ENABLED and asks for the key; the normal placeholder (index.html
  // markup) returns from the engine snapshot the moment a key is wired.
  chatNeedsKeyPlaceholder: "Paste your xpub or zpub to get started…",
  // TCK-LAUNCH-004: the compose-area loading state (see paintChatPlaceholder
  // — the ONLY renderer of it). A web launch emits no preload line in the
  // transcript; this placeholder swap IS the loading surface, typed off
  // model_state='loading'. Ellipsis per the ticket's exact-copy pin.
  chatLoadingPlaceholder: "Loading local llm…",
  unreachable:
    "Could not reach the wallet server — is it still running? Check the terminal where you started it.",
  // TCK-WEB-028 (2): a non-ok /turn or /action reply that ISN'T a transport
  // failure means the server was REACHED and refused — the unreachable
  // sentence would send the user hunting a live server that is answering.
  // The ticket's line; the server's own value-free ``error`` string (static
  // strings only, same data.error/textContent contract as the settings and
  // watchkey rejection paths) replaces the bare sentence when present.
  turnRejected: "The wallet couldn't run that — try again.",
  turnRejectedPrefix: "The wallet couldn't run that: ",
  // scan chip (TCK-WEB-005) — honest states straight from /state's scan_state
  scanLoading:
    "Wallet loading — balances may be incomplete until the first scan finishes.",
  scanSkipped: "First scan failed — balances may be incomplete.",
  // TCK-UX-010 + WEB-012 (f): privacy chip subline (VISIBLE text under the
  // chip word) per privacy_mode NAME — the closed enum the server adds to /state. Unknown names hide
  // the chip; these strings are the only per-mode prose.
  privacyPublic:
    "Public explorer — the operator can associate queried addresses with your IP.",
  privacyOwnLocal: "Your node on this machine — lookups stay here.",
  privacyOwnRemote:
    "Your node on another machine — private only if you trust it.",
  // TCK-WEB-023 (council fold 2026-09-13): the two host-named modes gain the
  // engine-supplied bare backend_host (/state, creds already stripped
  // server-side) — {host} is substituted ONLY through the isBareHost gate
  // below, and a refused/absent host falls back to the generic sentence
  // above / privacyOwnPrivate below (omit-never-empty discipline).
  privacyOwnRemoteAt:
    "Your node at {host} — private only if you trust it.",
  // own_node_private (private-range literal IP, TCK-WEB-023): the badge goes
  // GREEN but the binding glm council hedge stays TRUE — a private-IP server
  // you do not run still sees every query, so only the COLOR claims private
  // and the words keep the hedge ("run this server yourself").
  privacyOwnPrivate:
    "Your node — only private if you run this server yourself.",
  privacyOwnPrivateAt:
    "Your node at {host} — only private if you run this server yourself.",
  privacyAwaiting: "No backend chosen yet.",
  // TCK-WEB-027: the header wallet-fingerprint chip + the wallet-section
  // hint. The chip's visible text is "Wallet <fp>" — the WHOLE 8-hex value
  // verbatim, never truncated mid-hash (the WEB-026 naming rule). The hint
  // ships the HW-002 honesty adjustment: the ticket's device-parity claim
  // was FALSE (the chip carries the descriptor-origin ACCOUNT fp; the
  // device shows its MASTER — they DIFFER watch-only), so the copy says
  // exactly that. {fp} is substituted ONLY with the regex-gated value
  // (the privacySublineText {host} discipline). TCK-WEB-032 (verdict b:
  // parity NOT achievable on a bare-zpub provisioning path — the device's
  // master fp rides no input we accept and cannot be computed from an
  // account key without fabricating a value): ONE clarifying sentence
  // names which number the device screen — and device-sourced imports like
  // Sparrow's — actually show.
  walletFpWord: "Wallet",
  walletFpCopyName: "Copy wallet fingerprint",
  walletFpHint:
    "First characters: {fp} — your wallet's fingerprint. Your hardware " +
    "wallet shows its own, different number (the device fingerprint) — " +
    "they won't match, and that's expected. The number on your device's " +
    "screen — and in wallet apps that imported directly from the device " +
    "(like Sparrow) — is its MASTER fingerprint: a public account key " +
    "can never reveal it.",
  // TCK-WEB-031 (b/f): the LIVE connection chip names WHAT is connected —
  // the ticket's exact strings "<Kind>: <host>" for the two real backend
  // kinds, generic "Connected" for none/awaiting/no-host (pin f). The kind
  // word rides the typed /state backend_kind NAME and the host the typed
  // backend_host (engine truth, isBareHost-gated — never client-parsed).
  connConnected: "Connected",
  connElectrum: "Electrum",
  connBitcoind: "Bitcoind",
  // settings panel (TCK-WEB-005)
  settingsLoading: "Loading…",
  settingsUnavailable: "Could not load settings — the wallet is busy or unreachable.",
  settingsApply: "Apply",
  settingsEdit: "Edit",
  settingsSaving: "Saving…",
  // TCK-WEB-021 (6): the chain row's wait word names what is actually
  // happening — the engine PROBE (seconds-class) runs inside the Apply.
  settingsChecking: "Checking the server…",
  settingsApplied: "Applied.",
  settingsRejected: "Rejected — no reason given",
  settingsRejectedPrefix: "Rejected:",
  // TCK-WEB-021 (10): the static value-free next-step suffix on every
  // rejection line (one shared sentence — the suffix itself never varies).
  settingsRejectNext: " — check the value and apply again.",
  watchkeyRejectNext: " — check the key and try again.",
  // TCK-WEB-021 (3): the server card's collapsed rest zone (explanatory
  // prose: legend-style hints, creds notes, env/restart flags).
  chainRestNotes: "Server notes",
  settingsBusy: "The wallet is busy — try again.",
  settingsFailed: "Could not save — try again.",
  settingsRestart: "Takes effect after restart.",
  // split per docs/ux-web-copy.md §3, RE-SCOPED by TCK-DESCOPE-M3B: an
  // empty rung is UNRESOLVED (TCK-DESCOPE-M3A killed the silent public
  // default), so the strings say that — no leak claim for a server that
  // is never consulted.
  settingsEmptyPlaceholder: "Empty = no server chosen",
  settingsEmptyIsDefault:
    "Empty = no server chosen — the app looks nothing up until you set " +
    "one or record consent to the public Electrum server.",
  settingsEnvOverride: "Set via environment variable — edit there or remove it.",
  settingsRange: (min, max) => `Enter a whole number between ${min} and ${max}.`,
  settingsSectionWatchKey: "Wallet",
  settingsSectionNetwork: "Network & scanning",
  // TCK-LAUNCH-002 model-download card + inline progress.
  modelDownloading: "Downloading the model",
  // zpub section of the settings pane (TCK-WEB-009 a/b): first-run ENTRY
  // state (the moved card, compacted) and the collapsed SET state. The full
  // key is never needed in this pane anymore — the display is the engine's
  // own truncated value (Show/Copy/Replace affordances removed by user
  // direction; the replace/confirm POST rung stays wired for an
  // already-provisioned engine answering a fresh submit).
  watchkeyLede:
    "Enter the wallet's public account key — an xpub, ypub, or zpub — to " +
    "begin. You'll find it in your hardware wallet's settings under “export " +
    "public key” or “account descriptor” (for example a Jade or Coldcard).",
  watchkeyWarning:
    "This is not your seed words. This app is hardware-wallet-only. It " +
    "never accepts a seed phrase or a private key, and it could not use " +
    "one. Mainnet keys only — testnet keys are refused.",
  // TCK-WEB-021 (7): the entry form is title + input + Connect + ONE
  // reassurance line; the full lecture (lede + warning) collapses behind a
  // native <details> whose summary asks the form's one real question.
  watchkeyReassure: "A public key only — never your seed words or a private key.",
  watchkeyFindSummary: "Where do I find this?",
  watchKeyInputLabel: "Public account key",
  watchKeyConnect: "Connect",
  watchkeySaving: "Connecting…",
  watchkeyConnected: "Connected.",
  watchkeyRejectedPrefix: "Not accepted:",
  watchkeyBusy: "The wallet is busy — try again.",
  watchkeyFailed: "Could not connect — try again.",
  watchKeyRowUnknown:
    "Wallet status unknown — waiting for the server to reconnect…",
  watchKeyRowConnected: "Connected.",
  watchKeyEnvOverride:
    "The key was set via environment variable — a replacement takes " +
    "effect once that override is removed.",
  // TCK-WEB-027: the replace copy must NAME the fingerprint change — the
  // header chip's number moves with the new key (the sentence is the
  // ticket's exact wording; "Wallet …" quotes the chip's word pattern).
  watchKeyReplaceConfirm:
    "The cached balance and history belong to the current wallet; " +
    "replacing discards any pending transaction and re-scans for the new " +
    "one (the old wallet's data stays on this machine, unused). The " +
    "header's Wallet … number changes with the new key. Replace " +
    "the wallet's key?",
  watchKeyReplaceYes: "Replace",
  watchKeyReplaceCancel: "Cancel",
  // copy pass 2 §1: the SET row's Edit→form replace mode (the rungs below
  // this one are the existing 409-confirm/apply stages, unchanged).
  watchkeyReplaceLede:
    "Paste the public account key (xpub, ypub, or zpub) of the wallet you " +
    "want to watch instead.",
  watchKeyReplaceSubmit: "Replace wallet",
  // TCK-WEB-010: bubble copy control (aria-label + transient title states).
  copyMessage: "Copy message",
  // TCK-WEB-014: the in-bubble token affordance is a copy control now (no
  // navigation, so the old mempool.space disclosure is gone with D8).
  clickToCopy: "Click to copy",
  copyDone: "Copied",
  // TCK-WEB-026: the shared live-region sentence (value-free — the copied
  // token never enters it) + the value-bearing name parts for token buttons
  // ("Copy address bc1q…"/"Copy transaction id <full hash>" — the token is
  // quoted whole, never truncated mid-hash).
  copyOk: "Copied.",
  copyFail: "Copy failed — select it and copy manually.",
  copyAddress: "Copy address",
  copyTxid: "Copy transaction id",
  // TCK-QR-001: receive-address QR. The title/aria string is the ticket's
  // exact wording; the alt repeats it plus the address verbatim (a
  // screen-reader user can read/copy what the QR encodes); the fail line is
  // value-free (the server's 400 never echoes, and neither does this).
  qr: "QR",
  qrTitle: "Receive address QR — scan with a wallet to send to this address",
  qrFailed: "Could not show the QR code.",
  // TCK-HW-005 static half: the per-own-address verify-on-device button.
  // Both strings are the ticket's pinned wording (tooltip + accessible
  // name); the WEB-017 referent rule binds the INJECTED utterance, which
  // carries its own coordinates (see noteOwnAddress below).
  hwVerifyTip: "Show on hardware wallet",
  hwVerifyAria: "Show this address on your hardware wallet",
  // copy pass 2 #44 dim/lit badge words (TCK-WEB-009 e): RETIRED by
  // TCK-DESCOPE-M3B; the TCK-WEB-023 AMENDMENT brings the electrum/bitcoind
  // kind pills BACK as neutral NAME pills (no dim/lit trust tier of their
  // own — one color per kind, tinted by the shared privacy_mode
  // classification). The mempool word stays gone (user direction).
  kindElectrum: "Electrum",
  kindBitcoind: "Bitcoin Core",
  // Backend credentials (TCK-ONB-004 M3): the chain-base row's login block,
  // shown while EDITING an http:// (ambiguous — could be Core RPC) or
  // bitcoind:// address. The password never comes back from the server (the
  // engine's entry says only SET/UNSET), so the fields start empty every
  // render; typing is local-only, and a submitted value rides one POST and
  // is never echoed by any reply.
  credsHeading: "Server login — only if it asks for one",
  credsHint:
    "Kept in this app's local database on this machine, sent only to that " +
    "server, never logged and never shown back here.",
  credsUserLabel: "User",
  credsPassLabel: "Password",
  credsNoneLabel: "No credentials needed",
  credsSavedNote: "A login record is saved for your server (the value is not shown).",
  credsNoneSaved:
    "Your server is saved as needing no login — the app sends none.",
  credsClear: "Clear login",
  credsCleared: "Login cleared — press Apply again to reconnect without it.",
  credsNeedBoth: "Fill in both user and password, or neither.",
  // Resync now (TCK-WEB-009 f) — POST /resync's closed statuses, value-free.
  resyncNow: "Resync now",
  resyncSaving: "Starting…",
  resyncStarted:
    "Re-scan started — watch the status line at the top of the page for progress.",
  resyncBusy: "Already scanning — try again once the current scan finishes.",
  resyncUnavailable:
    "Re-scan is not available right now — nothing was changed. Try again " +
    "once the wallet has finished loading.",
  // applied-write notes from the response's resync field (TCK-WEB-009 g):
  // gap-limit and chain-base writes report whether a resync followed.
  resyncNoteStarted:
    "Saved — re-scanning with the new value; the status line at the top follows.",
  // TCK-DESCOPE-M3B: the old settingsEmptyApplied note ("Switched to the
  // public mempool.space server — …") is GONE with the family it named:
  // since TCK-DESCOPE-M3A an empty chain-base apply cannot land on a wired
  // engine (the write is refused — there is no silent public default), and
  // on an unwired pump it merely clears the choice, which reads as the
  // plain "Applied." line.
  // TCK-WEB-013 (1): the effective-backend line (the ADDITIVE
  // effective_chain_base_url from the /settings replies — the public
  // default becomes VISIBLE when the stored rung is empty; absent = the
  // line is omitted, never fabricated). (4)'s pane beat is GONE (TCK-ONB-007
  // static half, user correction 2026-09-11): the backend ask rides the
  // engine's chat bubble; the pane keeps the chain field and the 001B
  // consent button.
  settingsNowUsing: "Now using:",
  // (5) the env rung's honest note (value-free: names the mechanism and
  // the file, never the env VALUE beyond the URL already shown).
  chainEnvOverride:
    "Set by environment variable — change it there or in the " +
    "local-wallet folder\u2019s config.json.",
  settingsCancel: "Cancel",
  // (2) the chain row's trust badge — rides the /state privacy_mode
  // closed enum ONLY (never derived from the URL string client-side).
  trustLocal: "on this computer",
  // TCK-WEB-023: the private-range-IP mode reads as the user's own machine
  // ON THEIR OWN NETWORK — the green tint claims "private network", the
  // chip subline keeps the "run it yourself" hedge.
  trustPrivate: "your own machine (private network)",
  trustRemote: "your own machine (remote)",
  trustPublic: "public server — see privacy notice",
  trustAwaiting: "not set up yet",
  // (6) the stale-token sentence: shared with the stream path — a pane
  // POST that 401s can never be fixed by retrying.
  sessionStale:
    "This page\u2019s session no longer matches the wallet — reload this page.",
  resyncNoteBusy: "Saved — a scan is already running; it will use the new value.",
  resyncNoteSkipped:
    "Saved — your environment configuration outranks this one; it applies at next restart.",
  resyncNoteUnchanged: "Already up to date — no re-scan needed.",
  resyncNoteUnavailable: "Saved — no re-scan could start right now.",
  // TCK-GAP-001 (code-review MINOR): a NARROWED gap_limit needs no auto-rescan
  // (ADR-0009 amendment — nothing is missed by not scanning); this line says
  // so and names the manual apply path. The engine's value-free tradeoff note
  // (data.note) follows on the same line — no clause here repeats it.
  resyncNoteNoRescan:
    "Applied — no re-scan needed; the change takes effect on your next scan. " +
    "Use Resync now to apply it.",
  // TCK-PRIVACY-001B: the explicit public-backend consent button in the
  // pane's chain section. The subline REUSES the pane's own leak sentence
  // (PUBLIC_LEAK_SENTENCE) — one disclosure, never a second voice.
  consentPublic: "Use public server",
  consentSubline:
    "The public Electrum server electrum.blockstream.info — " +
    PUBLIC_LEAK_SENTENCE,
  consentSaving: "Recording…",
  consentLoading: "Consent recorded — the wallet is loading now.",
  consentRecorded: "Consent recorded.",
  consentFailed: "Could not record consent — try again.",
  // TCK-WEB-022: the suggested-public-server chip group. The group name is
  // the aria-label ONLY (never painted text); the warn line REUSES the pane's
  // one disclosure sentence (PUBLIC_LEAK_SENTENCE, the same dash pattern as
  // consentSubline above — one voice, value-free).
  chainChipsGroup: "Suggested public Electrum servers",
  chainChipsWarn: "A public server — " + PUBLIC_LEAK_SENTENCE,
  // TCK-UTXO-005: the confirmed/pending icon's ACCESSIBLE NAMES (pinned
  // copy — the CSS glyph is the non-color cue; these words carry the meaning).
  utxoConfirmed: "confirmed",
  utxoPending: "pending in mempool",
  // TCK-UTXO-006: the copy-only txid chip's whole painted label (the value
  // never reaches text; the sibling copyTokenButton's value-bearing aria
  // name carries the full hash).
  utxoTxidChip: "[tx]",
};

// Which buttons the typed /state snapshot shows, per flow position. The
// canonical utterances (data-utterance in markup) are quoted from the engine:
//   confirm/sign/cancel — ConfirmGate whitelists (tx/flow.py); "sign" is the
//     TCK-UX-002 gate-merge phrase (confirm + same-turn device handoff).
//   retry — the CONFIRMED-state re-sign interception in app.py _run_turn.
//   faster/slower — NOT gate utterances: the fee-target offer words the card
//     names (app.py); they ride POST /action as ordinary chat-classified
//     turns, exactly as if typed.
const VISIBILITY = {
  created: ["confirm", "sign", "cancel", "faster", "slower"],
  confirmed: ["retry"],
};
const FLOW_STATES = new Set([
  "idle", "created", "confirmed", "signed", "broadcast", "cancelled", "expired",
]);

// TCK-DESCOPE-M3B (user direction 2026-09-11): the backend-KIND badges were
// ELIMINATED from the settings pane. TCK-WEB-023 AMENDMENT (user MW-17
// direction 2026-09-13): the electrum/bitcoind kind pills COME BACK near the
// server field (mempool stays gone), riding the engine's additive
// ``backend_kind`` NAME (closed enum none/electrum/bitcoind) for the WORD and
// the SAME privacy_mode closed enum for the TINT — one classification, never
// client URL sniffing. The old dim/lit machinery (BADGE_FAMILIES, per-kind
// trust tiers) is NOT resurrected: kindPillPaint below is the whole painter.
// Keys the pane renders specially (own rows / own block) — everything else
// on the allowlist falls through to the generic text row. The credential
// trio (TCK-ONB-004 M3) belongs to the chain-base row's login block.
const SPECIAL_SETTING_KEYS = new Set([
  "watch_key", "chain_base_url", "gap_limit",
  "backend_auth_user", "backend_auth_pass", "backend_auth_none",
]);

// TCK-WEB-021 (1): the CLOSED key→word map — snake_case engine keys never
// headline a row again. Unknown/future allowlisted keys fall back to the
// raw key (honest, never a wrong guess); the copy below is designer-
// reviewable in one place.
const SETTING_LABELS = {
  watch_key: "Public account key",
  chain_base_url: "Server address",
  gap_limit: "Gap limit",
  display_currency: "Display currency",
  watch_interval_s: "Watch interval (seconds)",
  utxo_target_min_sats: "Coin target minimum (sats)",
  utxo_target_max_sats: "Coin target maximum (sats)",
  consolidate_below_sat_vb: "Consolidation threshold (sat/vB)",
};

function settingLabel(key) {
  return Object.prototype.hasOwnProperty.call(SETTING_LABELS, key)
    ? SETTING_LABELS[key]
    : key;
}

// The closed resync statuses the settings/resync replies carry (app.py
// RESYNC_STATUSES) → honest inline note. An unknown value renders the plain
// "Applied." line — never a guess.
const RESYNC_NOTES = new Map([
  ["started", LABELS.resyncNoteStarted],
  ["busy", LABELS.resyncNoteBusy],
  ["skipped", LABELS.resyncNoteSkipped],
  ["unchanged", LABELS.resyncNoteUnchanged],
  ["unavailable", LABELS.resyncNoteUnavailable],
  ["no_rescan", LABELS.resyncNoteNoRescan],
]);

const state = {
  lastEventId: 0,       // SSE cursor; sent as Last-Event-ID on reconnect
  openTurn: null,       // <li> currently receiving engine output
  progressLine: null,   // text node receiving raw progress chars (dots)
  busy: false,
  queue: [],            // local <li>s submitted while a turn was in flight
  everConnected: false, // first /state comes from boot; later ones from reconnect
  stopped: false,       // true on 401/no-token: stop reconnecting
  backoffMs: 500,
  // TCK-WEB-028 (3): the reconnecting TRANSITION tracker — the live-region
  // text is written once on entry into (and once out of) reconnecting, never
  // per backoff attempt (every textContent mutation re-announces to AT).
  reconnecting: false,
  // TCK-WEB-008: /state freshness (a slow first snapshot must never re-show
  // an entry state the engine has already outgrown), the terminal dismiss on
  // accept, and the key the USER supplied in THIS page session (memory only
  // — never persisted, never logged; a key supplied at launch never reaches
  // the client at all, so the collapsed display then falls back to the
  // engine's own truncated settings entry).
  // TCK-WEB-011: this tab's locally-echoed submits awaiting their engine
  // ``user_text`` echo (see renderUserText). Memory only; order = submit
  // order; the engine's bus order matches it.
  pendingEchos: [],
  // TCK-WEB-015: the transient pending bubble (the busy face IN the
  // transcript). ONE shared tail node for a whole busy period (queue
  // included); never persisted, never replayed content — null when idle.
  pendingBubble: null,
  // TCK-WEB-012 (f): the last KNOWN privacy_mode NAME from a typed
  // snapshot — state/0 (engine busy) must not blank the chip mid-turn.
  privacyMode: "",
  // TCK-WEB-020: the LAST RENDERED scan_error line (typed state/1 only).
  // The tracker doubles as the transition gate: the live region is touched
  // only when the line appears, changes, or disappears — never per
  // snapshot (every textContent mutation re-announces, the WEB-028
  // reconnecting-tracker discipline). "" = clean. Memory only, like
  // everything else here.
  scanError: "",
  // TCK-WEB-021 (5): the pinned reload trigger. The last TYPED snapshot's
  // backend_kind NAME (state/0 keeps the last known value, the privacyMode
  // discipline; half of the trust signature, and — since the TCK-WEB-023
  // AMENDMENT — the WORD source of the kind pill) and the signature it last
  // produced. null = no typed snapshot yet = no reload baseline (opening
  // fetches anyway).
  backendName: "",
  trustSig: null,
  // TCK-HW-005 static half (D7): the last TYPED snapshot's additive
  // signer_kind NAME ("file" | "hwi"; omitted pre-provision). "" (absent /
  // never typed / unknown) and "file" both HIDE the verify-on-device button
  // — a file-signer user has no device to show on, and the client never
  // guesses a signer from prose. state/0 keeps the last known value (the
  // backendName discipline). Memory only, never logged.
  signerKind: "",
  // TCK-WEB-023 (council fold): the last TYPED snapshot's additive
  // backend_host — the BARE hostname of the user's own configured server,
  // engine-extracted (scheme/port/path/creds already stripped), present ONLY
  // in the host-named modes (own_node_remote / own_node_private). Cleared by
  // every typed snapshot that omits it (the engine omits = nothing to name;
  // omit-never-empty, never a stale host across a typed flip); state/0 keeps
  // the last known value like privacyMode. Rendered only through isBareHost.
  // Memory only, never logged (it is a host — the value-free rule).
  backendHost: "",
  // TCK-WEB-027: the last TYPED snapshot's wallet_fingerprint (the engine-
  // validated closed 8-hex account-key fingerprint; gated again here — see
  // WALLET_FP_RE). "" = no chip, no settings hint: field ABSENT on a typed
  // snapshot clears it (the chip never keeps a dead wallet's number),
  // state/0 (busy engine) keeps the last value. Never fabricated, never
  // logged, never derived client-side from the zpub.
  walletFingerprint: "",
  // TCK-WEB-022: the last TYPED snapshot's vetted public-Electrum chip list
  // (engine-validated {url, label} entries; gated again here — see
  // readSuggestedServers). [] = no group (missing key on a typed snapshot
  // clears it; state/0 keeps the last known group — the backend_host /
  // walletFingerprint discipline). The sig is the change gate: the DOM is
  // touched ONLY when the array actually differs (render-once today — the
  // engine list is a code-owned constant). Memory only, never logged.
  suggestedServers: [],
  suggestedServersSig: null,
  stateSeq: 0,
  watchKeyDismissed: false,
  watchKeyPresent: null, // null = unknown | true | false (typed state/1 only)
  watchKeyNeeded: false, // the pane renders the zpub ENTRY form iff true
  sessionWatchKey: "",
  // TCK-WEB-013 (1): the effective chain base URL from the last settings
  // read (creds-stripped server-side; null = field absent = omit the
  // "Now using" line, never fabricate). Memory only, like every value.
  effectiveChainUrl: null,
  // TCK-LAUNCH-002: the model card + inline download progress. The card /
  // quick-action buttons are shown ONLY from the typed snapshot's additive
  // model_state NAME (never prose); the progress line is an inline element
  // fed by the int-only model_progress events (percent + bytes, value-free).
  downloadLine: null, // <p> currently receiving the inline download bar
  // TCK-LAUNCH-004: the compose-area loading indicator, driven ONLY by the
  // typed snapshot's additive model_state NAME ('loading' → true; unknown
  // values never guess; state/0 keeps the last known value — the same
  // discipline as every other typed field). The bare turn_end the pump emits
  // at the preload's terminal marker re-reads /state, so this flips to false
  // on ready AND failed (no stuck indicator).
  modelLoading: false,
  // TCK-WEB-009: the pane's data. The last successfully read settings
  // entries (in memory only — never persisted, never logged) so the zpub
  // row can flip form↔display on a /state transition without a refetch
  // storm (the engine is routinely busy — refetching right after a
  // provisioning accept would 503). (The backend_kind NAME this also
  // carried for the kind badges was retired with them, TCK-DESCOPE-M3B.)
  settings: null, // null | Map<key, entry>
  // copy pass 2 §1: the SET row's Edit⇄form toggle (client-side only — no
  // request happens until the form's Replace-wallet submit).
  watchKeyReplaceOpen: false,
};

// model_state (a closed enum name from /state) → which of the two model
// sections is on screen. absent/failed = the Yes/No card (failed re-offers
// it so the user can retry); declined/running = the model-free quick
// actions (the inline progress bar shows in the transcript while
// running); ready/absent-field/unknown = neither.
const MODEL_CARD_STATES = new Set(["absent", "failed"]);
const MODEL_QUICK_STATES = new Set(["declined", "running"]);

// ---------------------------------------------------------------- utilities

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setStatus(kind, label) {
  statusEl.dataset.state = kind;
  statusEl.textContent = label;
}

function setBusy(busy) {
  // Send stays enabled: the server queues turns (queued rendering below).
  // TCK-WEB-015: the busy face is the IN-TRANSCRIPT pending bubble (see
  // showPendingBubble), toggled where this flag is set/cleared; the old
  // below-input busy indicator is gone.
  state.busy = busy;
}

function scrollToEnd() {
  // keep the tail in view, but don't fight a user who scrolled up
  const gap = scrollerEl.scrollHeight - scrollerEl.scrollTop - scrollerEl.clientHeight;
  if (gap < 160) scrollerEl.scrollTop = scrollerEl.scrollHeight;
}

function authHeaders(extra) {
  const headers = { "X-Auth-Token": token };
  return Object.assign(headers, extra);
}

// ---------------------------------------------------------------- rendering

// TCK-LINK-001 + TCK-WEB-014: chat-bubble token affordances. ONLY these two
// closed token shapes are treated specially, and ONLY inside engine/user
// bubbles (progress lines, the model-download bar, and every settings-pane
// display keep plain text nodes — they never route through appendBubbleText).
// WEB-014 (user direction, reverses LINK-001's navigation): a qualifying
// token renders as a COPY BUTTON, not a link — no href, no external origin
// (the ONE bubble navigation is the TCK-CHAT-006 engine link line below,
// whose URL is full-line-validated before it becomes an <a>). The
// visible label is the token VERBATIM (addresses/txids are quoted from tool
// output; the affordance never alters displayed characters), and the string
// handed to the clipboard is that same verbatim token. A <button> (not a
// href-less <a>) because copy is an action and a button is focusable and
// keyboard-operable by construction; styles.css resets it to the exact
// inline text layout of the old anchor (overflow-wrap:anywhere keeps the
// same mid-token break at phone width — verified no layout shift).
// mainnet bech32: "bc1" + lowercase bech32 charset, total length 14..90.
const ADDRESS_RE = /^bc1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{11,87}$/;
const TXID_RE = /^[0-9a-f]{64}$/;
// TCK-UTXO-006: the engine's CLOSED arrival shape — a UTC "YYYY-MM-DD" or the
// literal "pending" — already a display string, so the client renders it
// VERBATIM (zero date math, zero reformatting); anything else fails the gate.
const UTXO_ARRIVAL_RE = /^(\d{4}-\d{2}-\d{2}|pending)$/;
// Standalone-token scan: the \b guards (plus greedy-length + boundary
// backtracking) reject a shape-valid token embedded in a longer word.
const LINK_SCAN_RE =
  /\b(bc1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{11,87}|[0-9a-f]{64})\b/g;

// TCK-CHAT-006: engine explorer LINK lines. The engine emits plain text —
// a head line "Explorer links (mempool.space) — click to open:" then one
// line per link, "<Label>: <url>", URL last, no trailing punctuation, and
// EVERY url was built engine-side as the literal https://mempool.space root
// plus a closed path shape. ONLY a line that matches this anchored regex IN
// FULL renders its URL as a click-to-open anchor; the href is the captured,
// shape-validated token — arbitrary bubble text never reaches an href
// (LINK-001 precedent). The label set is the closed engine enum; paths are
// /tx/<64-hex> | /address/<mainnet-bech32-shape> | bare root. Anything else
// — a URL-shaped string inside a narration sentence, malformed path,
// trailing punctuation, uppercase hex, foreign origin — fails the match and
// keeps the existing token copy-affordance treatment (WEB-014 unchanged).
const EXPLORER_LINK_LINE_RE =
  /^(Transaction|Address|Mempool): (https:\/\/mempool\.space(?:\/tx\/[0-9a-f]{64}|\/address\/bc1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{11,87})?)$/;

// Append `text` to a bubble line as text nodes with qualifying tokens turned
// into copy buttons. Render-once idempotence: every .turn-text line is built
// ONCE at its event — the handleEvent event-id duplicate guard drops replayed
// SSE events before appendText/appendUser ever run — and this transform never
// re-reads an existing line, so a replay cannot double-wrap or nest controls.
function appendBubbleText(line, text) {
  // TCK-CHAT-006: the ONLY navigation in a bubble — an engine link LINE
  // (full-line strict match) paints its validated URL as a real <a>; the
  // user's click is the gesture (no window.open, no auto-navigation).
  const link = EXPLORER_LINK_LINE_RE.exec(text);
  if (link) {
    line.appendChild(document.createTextNode(link[1] + ": "));
    line.appendChild(explorerAnchor(link[2]));
    return;
  }
  let last = 0;
  let m;
  LINK_SCAN_RE.lastIndex = 0;
  while ((m = LINK_SCAN_RE.exec(text)) !== null) {
    if (m.index > last) {
      line.appendChild(document.createTextNode(text.slice(last, m.index)));
    }
    line.appendChild(copyTokenButton(m[0]));
    if (ADDRESS_RE.test(m[0])) {
      // TCK-QR-001 (D9: preserved): the per-address QR affordance rides
      // right after the token (txids get none). Its "QR" caption is excluded
      // from bubbleText (lineText) so copying a message never gains button
      // words; the token button's text IS the verbatim token, so it stays.
      line.appendChild(qrButton(m[0]));
    }
    last = m.index + m[0].length;
  }
  if (last === 0 || last < text.length) {
    line.appendChild(document.createTextNode(text.slice(last)));
  }
}

// localhost is a secure context so navigator.clipboard normally exists; a
// missing API or a rejected write reports false and lands in the visible
// fail state (class + title only — never an alert, never an inline style).
async function clipboardWrite(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

// The WEB-010 copy feedback pattern, shared by both copy controls, upgraded
// once by TCK-WEB-026 so both inherit: class state (color PLUS a soft
// background and a floating CSS ::after word — non-color cues that survive
// forced-colors), swapped title AND aria-label, and the ONE shared
// value-free live region announcing "Copied." / the fail sentence. A fail
// HOLDS (its class and live sentence survive to the next click — a stale
// clipboard on money is not a 1.6s-and-forget situation) but its title and
// aria-label revert IMMEDIATELY, so the accessible name keeps carrying WHICH
// token failed; an ok swaps them for the window, then reverts.
function flashCopyResult(ctrl, ok, baseTitle, baseAria) {
  ctrl.classList.remove("copy-ok", "copy-fail");
  ctrl.classList.add(ok ? "copy-ok" : "copy-fail");
  copyStatusEl.textContent = ok ? LABELS.copyOk : LABELS.copyFail;
  clearTimeout(ctrl._copyReset);
  if (ok) {
    ctrl.title = LABELS.copyDone;
    ctrl.setAttribute("aria-label", LABELS.copyDone);
    ctrl._copyReset = setTimeout(() => {
      ctrl.classList.remove("copy-ok", "copy-fail");
      ctrl.title = baseTitle;
      ctrl.setAttribute("aria-label", baseAria);
    }, 1600);
  } else {
    // fail: the held CLASS and the live sentence carry the state; the NAME
    // (and desktop-hover title) return to the value-bearing base at once.
    ctrl.title = baseTitle;
    ctrl.setAttribute("aria-label", baseAria);
  }
}

// TCK-WEB-014: the in-bubble underlined token, now a copy affordance. Its
// only content is the verbatim token (no label text nodes), so lineText
// keeps copying message text exactly (D9). TCK-WEB-026: the accessible NAME
// carries the value — "Copy address bc1q…" / "Copy transaction id <full
// hash>" (ADDRESS_RE discriminates; the old "Click to copy" name erased the
// token from the SR queue) — while the hover-only title keeps the plain
// garnish. The whole token is quoted, never truncated.
function copyTokenButton(token) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "explorer-link";
  btn.textContent = token; // verbatim label
  const name =
    (ADDRESS_RE.test(token) ? LABELS.copyAddress : LABELS.copyTxid) + " " + token;
  btn.title = LABELS.clickToCopy;
  btn.setAttribute("aria-label", name);
  btn.addEventListener("click", async () => {
    flashCopyResult(btn, await clipboardWrite(token), LABELS.clickToCopy, name);
  });
  return btn;
}

// TCK-CHAT-006: the ONE anchor builder in the client. Its href argument is
// ONLY ever a capture of EXPLORER_LINK_LINE_RE — https://mempool.space plus
// a shape-validated path, never arbitrary text. The visible text is the URL
// verbatim via textContent (XSS contract). target=_blank + rel=noopener
// noreferrer; the click IS the user gesture — no event listener, no
// scripting, no window.open.
function explorerAnchor(url) {
  const a = document.createElement("a");
  a.className = "explorer-open-link";
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.textContent = url;
  return a;
}

// TCK-WEB-010: per-bubble copy control. The copyable text of a turn is its
// message lines only (progress dots and the model-download bar are transient
// telemetry, not message text); a system bubble holds its text on the li
// itself. textContent read, textContent copy — the XSS contract never
// serializes markup here. TCK-QR-001: the per-address "QR" button labels are
// affordance words, not message text — lineText skips .qr-btn children.
function lineText(line) {
  let text = "";
  for (const node of line.childNodes) {
    if (node.nodeType === Node.ELEMENT_NODE && node.classList.contains("qr-btn")) continue;
    // TCK-UTXO-005: the amount button's VISIBLE text is per-row click state;
    // the copied/announced text is CANONICAL (always the row's sats view —
    // the dataset carries the engine's own figure, nothing is re-derived).
    if (node.nodeType === Node.ELEMENT_NODE && node.classList.contains("utxo-amount")) {
      text += node.dataset.satsText;
      continue;
    }
    // TCK-UTXO-006: the copy-only txid chip PAINTS "[tx]" (selection chrome,
    // same rule as the .qr-btn exclusion); the copied/announced text carries
    // the FULL verbatim txid off the dataset instead — the row copy never
    // changes shape with the gate.
    if (node.nodeType === Node.ELEMENT_NODE && node.classList.contains("utxo-txid-btn")) {
      text += node.dataset.txid;
      continue;
    }
    text += node.textContent;
  }
  return text;
}

function bubbleText(turn) {
  const lines = turn.querySelectorAll(".turn-text:not(.turn-progress):not(.turn-model)");
  if (lines.length > 0) return Array.from(lines, lineText).join("\n").trim();
  return (turn.textContent || "").trim();
}

// TCK-WEB-019: ONE polite live region speaks each SETTLED engine turn's text
// exactly once — noteTurnEnd (the turn_end handler) calls this before the
// turn closes. The line seam is bubbleText's: .turn-text minus
// .turn-progress/.turn-model, so the per-tick dot mutation can never reach
// the region. The user's echo and the pending bubble are separate transcript
// nodes, never children of the engine turn — excluded by construction.
// PINNED: lines join with "\n" (same shape as the copy text; SRs read a
// newline as a pause between paragraphs). An empty or line-less turn
// announces nothing (a stray/replayed turn_end is silent). replaceChildren
// with a FRESH text node per announcement keeps an identical repeat a
// subtree change (same-text textContent writes can be skipped); the
// once-per-turn guarantee is handleEvent's replay id-guard, which drops a
// duplicate turn_end before noteTurnEnd ever runs.
function announceTurn(turn) {
  if (!turn) return;
  const lines = turn.querySelectorAll(".turn-text:not(.turn-progress):not(.turn-model)");
  if (lines.length === 0) return;
  const text = Array.from(lines, lineText).join("\n").trim();
  if (!text) return;
  turnStatusEl.replaceChildren(document.createTextNode(text));
}

// The overlapping-squares icon, built with createElementNS (CSP-safe: no
// markup strings, no external assets; the shapes carry no text nodes, so an
// appended button never changes bubbleText).
function copyIcon() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const front = document.createElementNS(ns, "rect");
  front.setAttribute("x", "9");
  front.setAttribute("y", "9");
  front.setAttribute("width", "13");
  front.setAttribute("height", "13");
  front.setAttribute("rx", "2");
  const back = document.createElementNS(ns, "path");
  back.setAttribute("d", "M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1");
  svg.append(back, front);
  return svg;
}

// One button per bubble, added when the bubble first carries copyable text
// (empty / progress-only bubbles get none). The ok/fail feedback is the
// shared WEB-010 pattern (flashCopyResult).
function addCopyButton(turn) {
  if (turn.querySelector(".copy-btn") || !bubbleText(turn)) return;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "copy-btn";
  btn.setAttribute("aria-label", LABELS.copyMessage);
  btn.title = LABELS.copyMessage;
  btn.appendChild(copyIcon());
  btn.addEventListener("click", async () => {
    const text = bubbleText(turn);
    if (!text) return;
    flashCopyResult(
      btn,
      await clipboardWrite(text),
      LABELS.copyMessage,
      LABELS.copyMessage,
    );
  });
  turn.appendChild(btn);
}

// ------------------------------------------------------- receive QR (TCK-QR-001)
// The "QR" button beside every linked mainnet address opens a modal viewer
// showing that address as a scannable QR. GET /qr is token-gated and an
// <img> request cannot carry X-Auth-Token — so the SVG is FETCHED with the
// header and handed to <img> through a blob: object URL (the only shapes
// CSP img-src admits: 'self' + blob:). The server re-validates the value
// (mainnet segwit only) and echoes nothing but the QR; every string here
// goes in via textContent/setAttribute — the XSS contract stands.
let qrOpenFor = ""; // the address currently on screen ("" = closed)
let qrOpener = null; // the button to refocus on close
let qrSeq = 0; // stale-response guard: only the newest open may paint
let qrUrl = null; // live object URL (revoked on close/replace)

function closeQr() {
  qrSeq += 1; // abandon any in-flight fetch
  qrOpenFor = "";
  if (qrUrl !== null) {
    URL.revokeObjectURL(qrUrl);
    qrUrl = null;
  }
  qrImgEl.removeAttribute("src");
  qrImgEl.setAttribute("alt", "");
  qrViewerEl.hidden = true;
  const opener = qrOpener;
  qrOpener = null;
  if (opener && document.contains(opener)) opener.focus();
}

async function openQr(address, opener) {
  closeQr();
  qrOpenFor = address;
  qrOpener = opener;
  qrViewerEl.hidden = false;
  qrCaptionEl.textContent = address; // VERBATIM from the bubble's tool output
  qrCloseEl.focus();
  const seq = qrSeq;
  try {
    const response = await fetch(
      "/qr?value=" + encodeURIComponent(address),
      { headers: authHeaders(), cache: "no-store" }
    );
    if (!response.ok) throw new Error("qr refused");
    const blob = await response.blob();
    if (seq !== qrSeq) return; // closed or replaced while loading
    qrUrl = URL.createObjectURL(blob);
    qrImgEl.setAttribute("alt", LABELS.qrTitle + ": " + address);
    qrImgEl.src = qrUrl;
  } catch {
    if (seq !== qrSeq) return;
    qrCaptionEl.textContent = LABELS.qrFailed;
  }
}

function qrButton(address) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "qr-btn";
  btn.textContent = LABELS.qr;
  btn.title = LABELS.qrTitle;
  btn.setAttribute("aria-label", LABELS.qrTitle);
  btn.addEventListener("click", () => {
    // A click is a toggle: re-clicking the open address dismisses the viewer.
    if (!qrViewerEl.hidden && qrOpenFor === address) closeQr();
    else openQr(address, btn);
  });
  return btn;
}

qrCloseEl.addEventListener("click", closeQr);
qrViewerEl.addEventListener("click", (event) => {
  if (event.target === qrViewerEl) closeQr(); // backdrop click
});
// TCK-WEB-021 (6): the QR dialog is the TOPMOST layer — while it is open its
// Escape handler CONSUMES the key (stopImmediatePropagation) so the separate
// settings-pane document keydown listener never runs on the same press (one
// Escape closes the QR, not the QR AND the pane). The settings handler also
// guards on !qrViewerEl.hidden, so the ordering is safe either way.
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !qrViewerEl.hidden) {
    event.stopImmediatePropagation();
    closeQr();
  }
});
// TCK-WEB-028 (4): the QR dialog claims aria-modal="true" — while it is open,
// Tab must cycle WITHIN it, not walk into the background transcript. Focus
// starts on the Close button (openQr) and the dialog's only real control is
// that button, but the wrap is written generically over the dialog's
// focusables (first/last boundary + a strayed focus pulled back in). The
// selector stays off anchors by construction (the WEB-014 no-navigation pin
// bans the very literal here); Escape tiering from WEB-021 is untouched.
document.addEventListener("keydown", (event) => {
  if (event.key !== "Tab" || qrViewerEl.hidden) return;
  const focusables = qrViewerEl.querySelectorAll(
    'button, [tabindex]:not([tabindex="-1"])',
  );
  if (focusables.length === 0) return;
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  const at = document.activeElement;
  const inside = qrViewerEl.contains(at);
  if (event.shiftKey && (at === first || !inside)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (at === last || !inside)) {
    event.preventDefault();
    first.focus();
  }
});

// ------------------------------------------- verify-on-device (TCK-HW-005 static)
// The engine's OWN ownership declaration: an ``own_address`` SSE event
// ({address, branch, index} JSON) follows the narration text event of a
// receive/new-address turn. ONLY that event — never a client-side sniff of
// the prose — marks an address as OURS, so a RECIPIENT address in any
// bubble can never gain the button. The event's payload address is matched
// against the bubble's rendered token buttons VERBATIM (the same tool-output
// string), and the button injects its utterance from the EVENT's own
// coordinates — the utterance names its referent (WEB-017), never deictic.
// A malformed/unknown-shaped payload arms nothing (ignored, never fatal);
// an event whose turn already closed arms nothing either (fail-closed — a
// missing affordance is honest, a misplaced one would claim ownership).

// The calculator glyph (the device's screen + keys), built with
// createElementNS exactly like copyIcon: CSP-safe, no markup strings, and
// the shapes carry NO text nodes, so lineText/bubbleText/announceTurn are
// untouched and the existing QR/copy affordances never gain button words.
function hardwareIcon() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const body = document.createElementNS(ns, "rect");
  body.setAttribute("x", "4");
  body.setAttribute("y", "2");
  body.setAttribute("width", "16");
  body.setAttribute("height", "20");
  body.setAttribute("rx", "2");
  const screen = document.createElementNS(ns, "path");
  screen.setAttribute("d", "M8 6h8");
  const keys = document.createElementNS(ns, "path");
  keys.setAttribute(
    "d",
    "M8 10h.01M12 10h.01M16 10h.01M8 14h.01M12 14h.01M16 14v4M8 18h.01M12 18h.01",
  );
  svg.append(body, screen, keys);
  return svg;
}

// The click rides the SAME /action channel as every other button (ADR-0024
// §8 — full _run_turn; nothing here touches a handler or the flow).
// WEB-028 (5) disable-on-click: the control goes inert the moment it is
// pressed and is restored by the next /state repaint (applyState). The
// engine's own reply narrates the device handoff; a file signer or a
// missing device gets the engine's guidance line — never an error here.
function hwVerifyButton(utterance) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "hw-verify-btn";
  btn.dataset.utterance = utterance;
  btn.title = LABELS.hwVerifyTip;
  btn.setAttribute("aria-label", LABELS.hwVerifyAria);
  btn.hidden = state.signerKind !== "hwi"; // typed truth only; repaint follows
  btn.appendChild(hardwareIcon());
  btn.addEventListener("click", () => {
    if (btn.disabled || state.stopped) return;
    btn.disabled = true;
    submit("/action", "utterance", utterance);
  });
  return btn;
}

function noteOwnAddress(raw) {
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    return; // non-JSON payload: ignore, never render raw
  }
  if (!data || typeof data !== "object") return;
  const address = data.address;
  const branch = data.branch;
  const index = data.index;
  // Re-gate the wire shape client-side (the engine already shape-gates;
  // untrusted server data is still validated before it arms an action).
  if (typeof address !== "string" || !address) return;
  if (!Number.isInteger(branch) || !Number.isInteger(index)) return;
  if (branch < 0 || branch > 1 || index < 0) return;
  const turn = state.openTurn;
  if (!turn) return; // contract: the event follows its narration, pre-turn_end
  const utterance = "/verifyaddress " + branch + " " + index;
  // Duplicate/replayed marking of the SAME coordinates arms a second button
  // nowhere (the id guard already drops true replays; this covers a repeat
  // emission inside one turn).
  if (turn.querySelector('[data-utterance="' + utterance + '"]')) return;
  for (const token of turn.querySelectorAll(".explorer-link")) {
    // ENGINE-TRUTH match: the button lands ONLY beside a rendered token
    // whose whole text is the event's address — never on prose, never on a
    // recipient address (no such event exists for those).
    if (token.textContent !== address) continue;
    const qr = token.nextElementSibling;
    const anchor = qr && qr.classList && qr.classList.contains("qr-btn") ? qr : token;
    anchor.after(hwVerifyButton(utterance));
    break; // one button per (address,coords) per turn; see the dedupe above
  }
}

// Called from applyState on EVERY /state repaint (after the typed
// signer_kind read above): restores the WEB-028 (5) disable-on-click, and
// visibility comes ONLY from the typed signer_kind NAME — file / absent /
// unknown = hidden (no device to show on; the engine's own no-device line
// is the fallback narration, this control never becomes an error state).
function paintHwVerifyButtons() {
  const show = state.signerKind === "hwi";
  for (const btn of transcriptEl.querySelectorAll(".hw-verify-btn")) {
    btn.disabled = false;
    btn.hidden = !show;
  }
}

// ------------------------------------------------- UTXO render rows (TCK-UTXO-005)
// A NON-EMPTY get_utxos result carries the additive typed ``utxo_rows`` list
// (one row per listed coin, narration order; the field contract lives with the
// engine's _utxo_render_rows docstring). The pump stamps it onto the bus as a
// ``utxo_rows`` event AFTER the coin narration lines it upgrades — the exact
// TCK-HW-005 own_address additive-stamp precedent (the CLI sink ignores the
// kind, so every non-web surface keeps its byte-identical text).
// CLIENT RULE (pinned): PREFER the rows when they arrive and match; anything
// else — no event (empty listing, error/clarify shapes, legacy results), a
// malformed payload, a partial match, no open turn — leaves the turn's text
// render EXACTLY as today. The builder is a pure function of the rows array,
// so an SSE replay renders identically (handleEvent's id guard already drops
// a replayed turn before any of it runs).
// The row COMPUTES NOTHING: value_btc swaps VERBATIM (engine math); the only
// client arithmetic is thousands grouping of the engine's own integer — the
// same `:,` shape the narration line prints.

function formatSats(valueSats) {
  // Digit grouping only (the Python :, format for the contract's ints).
  return String(valueSats).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function utxoRowIsWellFormed(row) {
  if (!row || typeof row !== "object" || Array.isArray(row)) return false;
  if (!Number.isInteger(row.value_sats) || row.value_sats < 0) return false;
  if (typeof row.value_btc !== "string" || !/^[0-9]+\.[0-9]{8}$/.test(row.value_btc)) return false;
  if (typeof row.confirmed !== "boolean") return false;
  if (typeof row.txid !== "string" || !TXID_RE.test(row.txid)) return false;
  // ``number`` and ``address`` are present TOGETHER or absent together (the
  // degenerate address-less row carries neither — never a fabricated one).
  const hasNumber = row.number !== undefined;
  const hasAddress = row.address !== undefined;
  if (hasNumber !== hasAddress) return false;
  if (hasNumber && (!Number.isInteger(row.number) || row.number < 0)) return false;
  if (hasAddress && (typeof row.address !== "string" || !row.address)) return false;
  // label: optional passthrough; present means non-empty string.
  if (row.label !== undefined && (typeof row.label !== "string" || !row.label)) return false;
  // TCK-UTXO-006 gate keys: BOTH present (a 006 row) or BOTH absent (a
  // pre-006 payload — keeps today's text rendering). A 006 row's marker is
  // the literal true and its arrival the engine's closed display string;
  // anything else fails the whole payload (all-or-nothing, UTXO-005 pattern).
  const hasArrival = row.arrival !== undefined;
  const hasCopyOnly = row.txid_copy_only !== undefined;
  if (hasArrival !== hasCopyOnly) return false;
  if (hasArrival) {
    if (typeof row.arrival !== "string" || !UTXO_ARRIVAL_RE.test(row.arrival)) return false;
    if (row.txid_copy_only !== true) return false;
  }
  return true;
}

function utxoAmountButton(row) {
  const satsText = formatSats(row.value_sats) + " sats";
  const btcText = row.value_btc + " BTC"; // verbatim engine string + unit word
  const btn = el("button", "utxo-amount", satsText);
  btn.type = "button";
  // The CANONICAL copy view (lineText reads this, never the toggled text —
  // copied message text can never depend on click state).
  btn.dataset.satsText = satsText;
  let showBtc = false; // PER-ROW state; no page-level toggle state exists
  btn.addEventListener("click", () => {
    showBtc = !showBtc;
    btn.textContent = showBtc ? btcText : satsText;
  });
  return btn;
}

// Confirmed/pending: the glyph is a CSS ::before SHAPE (filled vs open — the
// non-color cue, WCAG 1.4.1, the WEB-026 precedent; the word lives in the
// stylesheet only, so the span carries NO DOM text and copy/announce text is
// untouched). The accessible name carries the whole meaning.
function utxoStateIcon(confirmed) {
  const cls = confirmed ? "utxo-state utxo-confirmed" : "utxo-state utxo-pending";
  const span = el("span", cls);
  span.setAttribute("role", "img");
  span.setAttribute("aria-label", confirmed ? LABELS.utxoConfirmed : LABELS.utxoPending);
  return span;
}

// TCK-UTXO-006: on GATED rows the txid is clipboard-only — this compact chip
// (qr-btn's register, "[tx]" label) replaces the verbatim-token button. The
// full hash NEVER reaches painted text; it rides the dataset (lineText reads
// it so the row's copy text keeps the verbatim txid) and the value-bearing
// WEB-026 name ("Copy transaction id <full hash>", the sibling copyTokenButton
// convention). The click is the SAME shared copy machinery as every other copy
// control — clipboardWrite(txid) + flashCopyResult, no second copy path.
function utxoTxidChip(txid) {
  const btn = el("button", "utxo-txid-btn", LABELS.utxoTxidChip);
  btn.type = "button";
  btn.dataset.txid = txid;
  btn.title = LABELS.clickToCopy;
  const name = LABELS.copyTxid + " " + txid;
  btn.setAttribute("aria-label", name);
  btn.addEventListener("click", async () => {
    flashCopyResult(btn, await clipboardWrite(txid), LABELS.clickToCopy, name);
  });
  return btn;
}

// The user-spec row: <#number> <amount toggle> [<arrival>] <confirmed/pending
// icon> <copy-address> <copy-txid> (label last, ONLY when the row carries one).
// The copy buttons ARE the WEB-014/026 machinery (verbatim token content,
// value-bearing aria name, flashCopyResult + the shared #copy-status live
// region) — no second copy path exists. The single-space text nodes between
// parts survive in textContent (the copy text) even where flex collapses
// them visually; the TEXTLESS icon span takes no seam of its own, so the
// row copy is "#14 10,000,000 sats <addr> <txid>" — never a double space
// around the glyph. TCK-UTXO-006: a GATED row (both keys — well-formedness
// already refused one-without-the-other, so the marker alone is the branch)
// reads its arrival date right after the amount (verbatim; no client date
// math) and swaps the verbatim txid button for the copy-only chip — the
// copied line still carries the FULL txid (the chip's dataset), never "[tx]".
// Pre-006 rows (neither key) keep today's render byte-identical.
function utxoRowElement(row) {
  const line = el("p", "turn-text utxo-row");
  const gated = row.txid_copy_only === true;
  if (row.number !== undefined) {
    line.appendChild(el("span", "utxo-number", "#" + row.number));
    line.appendChild(document.createTextNode(" "));
  }
  line.appendChild(utxoAmountButton(row));
  if (gated) {
    line.appendChild(document.createTextNode(" "));
    line.appendChild(el("span", "utxo-arrival", row.arrival)); // VERBATIM
  }
  line.appendChild(utxoStateIcon(row.confirmed));
  line.appendChild(document.createTextNode(" "));
  if (row.address !== undefined) {
    line.appendChild(copyTokenButton(row.address));
    line.appendChild(document.createTextNode(" "));
  }
  line.appendChild(gated ? utxoTxidChip(row.txid) : copyTokenButton(row.txid));
  if (row.label !== undefined) {
    line.appendChild(document.createTextNode(" "));
    line.appendChild(el("span", "utxo-label", row.label));
  }
  return line;
}

function noteUtxoRows(raw) {
  let rows;
  try {
    rows = JSON.parse(raw);
  } catch {
    return; // non-JSON payload: ignore, never render raw
  }
  if (!Array.isArray(rows) || rows.length === 0) return;
  if (!rows.every(utxoRowIsWellFormed)) return; // untrusted wire: ALL-or-nothing
  const turn = state.openTurn;
  if (!turn) return; // contract: the event rides its narration, pre-turn_end
  // Pair each row (narration order) with the coin line it restates: row and
  // line are two renderings of ONE store truth, so the line is the one
  // printing this row's "#N", separated sats figure, and FULL txid. First
  // unconsumed match per row keeps same-txid/same-value coins paired in order
  // (interchangeable rows produce an identical render — deterministic).
  const lines = Array.from(
    turn.querySelectorAll(
      ".turn-text:not(.turn-progress):not(.turn-model):not(.utxo-row)",
    ),
  );
  const coins = [];
  for (const row of rows) {
    const sats = formatSats(row.value_sats) + " sats";
    const txNeedle = "tx " + row.txid;
    const at = lines.findIndex((line) => {
      const text = lineText(line);
      return (
        (row.number === undefined || text.startsWith("#" + row.number + " ")) &&
        text.includes(sats) && text.includes(txNeedle)
      );
    });
    if (at === -1) return; // partial coverage: keep the whole text fallback
    coins.push(lines.splice(at, 1)[0]);
  }
  // Upgrade IN PLACE: the rows land where the first coin line was (the
  // listing's head line stays above, the tool's note lines stay below), then
  // the narrated coin lines they replace come out.
  for (const row of rows) coins[0].before(utxoRowElement(row));
  for (const line of coins) line.remove();
  addCopyButton(turn); // WEB-010: (re-)arm the bubble copy over the row lines
  scrollToEnd();
}

function ensureTurn() {
  if (!state.openTurn) {
    state.openTurn = el("li", "turn turn-engine");
    state.openTurn.appendChild(el("span", "turn-role", "Wallet"));
    state.progressLine = null;
    transcriptEl.appendChild(state.openTurn);
    tailPendingBubble(); // TCK-WEB-015: the pending bubble rides the TAIL
  }
  hintEl.hidden = true;
  return state.openTurn;
}

function appendText(text) {
  const turn = ensureTurn();
  const line = el("p", "turn-text");
  appendBubbleText(line, text); // TCK-WEB-014: click-to-copy token buttons
  turn.appendChild(line);
  addCopyButton(turn); // first copyable line of this engine turn
  state.progressLine = null;
  state.downloadLine = null; // the next tick starts a fresh inline bar
  scrollToEnd();
}

function appendProgress(chars) {
  // The CLI uses a bare "\n" to settle its in-place model-download percent
  // line; in the web transcript that newline is decoration the DOM bar does
  // not need — skip a whitespace-only tick when no dot line is open.
  if (!state.progressLine && !chars.trim()) return;
  const turn = ensureTurn();
  if (!state.progressLine) {
    const line = el("p", "turn-text turn-progress");
    state.progressLine = document.createTextNode("");
    line.appendChild(state.progressLine);
    turn.appendChild(line);
  }
  state.progressLine.appendData(chars);
  scrollToEnd();
}

// One inline model-download bar (TCK-LAUNCH-002). The payload is the
// engine's INT-ONLY JSON ({"downloaded","total","pct"}); a non-JSON or
// non-object payload is ignored, never rendered raw. Percent + byte counts
// only — no path, no filename, no wallet data. A <progress> element plus
// textContent labels (the XSS contract: no HTML-string sinks).
function renderModelProgress(raw) {
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    return;
  }
  if (!data || typeof data !== "object") return;
  const turn = ensureTurn();
  if (!state.downloadLine) {
    const line = el("p", "turn-text turn-model");
    const bar = el("progress", "model-bar");
    bar.max = "100";
    const label = el("span", "model-progress-label");
    line.append(bar, label);
    turn.appendChild(line);
    state.downloadLine = { line, bar, label };
  }
  const pct = Number.isInteger(data.pct) ? data.pct : null;
  if (pct === null) state.downloadLine.bar.removeAttribute("value");
  else state.downloadLine.bar.setAttribute("value", String(pct));
  const downloaded = Number.isInteger(data.downloaded) ? data.downloaded : 0;
  let text = LABELS.modelDownloading;
  if (pct !== null) text += ` ${pct}%`;
  text += ` — ${humanBytes(downloaded)}`;
  if (Number.isInteger(data.total) && data.total > 0) {
    text += ` of ${humanBytes(data.total)}`;
  }
  state.downloadLine.label.textContent = text;
  scrollToEnd();
}

// Display formatting of a byte count (the tool's integers; the client
// computes no wallet value — this is the txid-shortening class of display).
function humanBytes(bytes) {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const shown = unit === 0 ? String(Math.round(value)) : value.toFixed(1);
  return `${shown} ${units[unit]}`;
}

// TCK-WEB-024: ANY non-engine bubble closes an open engine turn, so a reply
// can never amend an earlier bubble regardless of server ordering. Shared
// with noteTurnEnd (turn_end closes the engine's own turn).
function closeOpenTurn() {
  state.openTurn = null;
  state.progressLine = null;
}

function appendSystem(text) {
  closeOpenTurn();
  const turn = el("li", "turn turn-system", text);
  addCopyButton(turn);
  transcriptEl.appendChild(turn);
  tailPendingBubble(); // TCK-WEB-015: stays the last line in the transcript
  hintEl.hidden = true;
  scrollToEnd();
}

function appendUser(text, queued) {
  closeOpenTurn();
  const turn = el("li", "turn turn-user" + (queued ? " turn-queued" : ""));
  turn.appendChild(el("span", "turn-role", "You"));
  if (queued) turn.appendChild(el("span", "turn-queued-tag", LABELS.queuedTag));
  const line = el("p", "turn-text");
  appendBubbleText(line, text); // TCK-WEB-014: click-to-copy token buttons
  turn.appendChild(line);
  addCopyButton(turn);
  transcriptEl.appendChild(turn);
  tailPendingBubble(); // TCK-WEB-015: queued echoes land ABOVE the bubble
  hintEl.hidden = true;
  scrollToEnd();
  return turn;
}

// TCK-WEB-015 (user direction): the in-flight indicator is a TRANSIENT chat
// bubble IN the transcript, not a widget below the input. Lifecycle, pinned
// by the TCK-WEB-015 decisions:
//  * shown on every submit (/turn AND /action — the shared submit()) right
//    after the user's own echo, and on any REMOTE user_text echo (other tab
//    or CLI) — so a reload/reconnect mid-turn re-shows it from the replayed
//    echo, honestly, at the transcript tail;
//  * ONE node per busy period (showPendingBubble is idempotent — replayed or
//    repeated events can never duplicate it), and it rides the TAIL: every
//    transcript append re-moves it after the new content, so engine output
//    streams above it while the turn runs;
//  * REPLACED (removed) at turn_end — the pinned D11 decision, not the first
//    reply — but ONLY when the local queue has drained AND nothing was
//    promoted (a promoted queued turn is still in flight and its echo
//    dedupes, so the bubble rides on for it); a failed submit clears it
//    under its own drained-queue (!busy) guard;
//  * never persisted, never replayed as content (it holds no text nodes —
//    only the aria-label word "Working…" relocated from UX-008's markup);
//    the copy selectors ignore it (no .turn-text line, no copy button).
// The dots are TCK-UX-008's CSS animation verbatim (styles.css keyframes +
// reduced-motion opt-out, now inside the bubble). This bubble is the
// APP-COMPUTING face ONLY — device-wait/transport-down rungs are
// TCK-WEB-018's scope, not this one.
function showPendingBubble() {
  if (!state.pendingBubble) {
    state.pendingBubble = el("li", "turn turn-pending");
    state.pendingBubble.setAttribute("role", "status");
    state.pendingBubble.setAttribute("aria-label", LABELS.turnWorking);
    const dots = el("span", "busy-dots"); // the UX-008 animation carrier
    dots.setAttribute("aria-hidden", "true");
    dots.append(el("span"), el("span"), el("span"));
    state.pendingBubble.appendChild(dots);
  }
  tailPendingBubble();
  scrollToEnd();
}

function clearPendingBubble() {
  if (!state.pendingBubble) return;
  state.pendingBubble.remove();
  state.pendingBubble = null;
}

// appendChild MOVES an existing node, so this both (re)tails the bubble and
// guarantees it can never exist twice in the transcript.
function tailPendingBubble() {
  if (state.pendingBubble) transcriptEl.appendChild(state.pendingBubble);
}

// TCK-WEB-011: the engine echoes EVERY accepted line (typed text, button
// utterance, slash command, CLI) on the shared bus as ``user_text`` before
// running it, so every tab renders the user's message. Dedupe: this tab's
// own submits are already echoed locally — the event suppresses the ONE
// oldest still-unconfirmed pending echo with the exact same text (the bus
// delivers in engine-pickup order, which matches local submit order), and
// anything unmatched (another tab's line, or the CLI) renders as a normal
// user bubble. Replay composes with this: the event-id duplicate guard in
// handleEvent drops a replayed echo of an already-consumed pending, and a
// tab that connects LATE has no pending echoes at all, so replay renders
// every echo exactly once.
// ponytail ceiling: EXACT text match, not turn ids — a pending entry whose
// event is lost (only possible across a too_far_behind gap, which clears
// the list) could later suppress another tab's identical-text echo; the
// bubble count and content stay correct either way.
function renderUserText(text) {
  const at = state.pendingEchos.indexOf(text);
  if (at !== -1) {
    state.pendingEchos.splice(at, 1);
    return;
  }
  appendUser(text, false);
  // TCK-WEB-015: an UNMATCHED echo is another surface's submit (tab/CLI) —
  // a turn is now in flight here too, so this tab shows the shared pending
  // bubble (idempotent: replayed echoes can never duplicate it).
  showPendingBubble();
}

// One turn_end closed the engine's current turn. Promote the oldest locally
// queued submit (it is the next line the FIFO pump will pick up) and re-sync
// button visibility — flow state only changes during turns. No turn_start
// event exists (kinds are text/progress/turn_end), so turn_end is the
// reconcile point.
function noteTurnEnd() {
  announceTurn(state.openTurn); // TCK-WEB-019: settle → speak, once
  closeOpenTurn();
  const next = state.queue.shift();
  if (next) {
    next.classList.remove("turn-queued");
    const tag = next.querySelector(".turn-queued-tag");
    if (tag) tag.remove();
  }
  setBusy(state.queue.length > 0);
  // TCK-WEB-015 (review MINOR): replace-on-turn_end, but a PROMOTED queued
  // turn is still in flight and its echo dedupes (renderUserText suppresses
  // it), so this seam is the only thing that could keep its bubble — clear
  // ONLY with nothing promoted (queue then empty by construction, busy
  // false). Promoted → keep the single shared bubble for the next turn.
  if (!next) clearPendingBubble();
  refreshState();
}

// ---------------------------------------------------------------- app state
// Button visibility comes ONLY from the typed value-free /state snapshot
// (state/1). Anything else — state/0 (engine busy/dead), a malformed or
// unknown payload — hides all actions and keeps chat working. No prose is
// ever parsed to infer structure.

function visibleActions(snap) {
  if (!snap || snap.schema !== "state/1") return [];
  if (typeof snap.flow_state !== "string" || !FLOW_STATES.has(snap.flow_state)) return [];
  if (snap.pending_present === true && snap.flow_state === "created") {
    return VISIBILITY.created;
  }
  if (snap.flow_state === "confirmed") return VISIBILITY.confirmed;
  return [];
}

function applyState(snap) {
  const typed = !!snap && snap.schema === "state/1";
  // TCK-WEB-028 (1): STUCK-PENDING-BUBBLE reconcile. A TYPED state/1 is
  // answered by the engine thread only BETWEEN turns (busy/dead → state/0),
  // so typed flow_state "idle" + an EMPTY local queue means nothing can
  // still be in flight here: a mid-turn stream death whose server restarted
  // without a replayed turn_end in reach would otherwise leave the
  // transient "Working…" bubble up forever. If a turn really starts, the
  // unmatched user_text echo re-shows the bubble (renderUserText).
  if (typed && snap.flow_state === "idle" && state.queue.length === 0) {
    clearPendingBubble();
  }
  // TCK-WEB-021 (5): read the trust signature's OTHER half from typed truth
  // only (state/0 keeps the last known value — same discipline as
  // privacyMode). Since the TCK-WEB-023 AMENDMENT the NAME also feeds the
  // kind pill's word (KIND_PILL_WORDS); a backend flip still pins the
  // settings reload below.
  if (typed && typeof snap.backend_kind === "string") {
    state.backendName = snap.backend_kind;
  }
  // TCK-HW-005 static half: the signer KIND NAME rides typed truth only
  // (state/0 keeps the last known value — the backendName discipline).
  if (typed && typeof snap.signer_kind === "string") {
    state.signerKind = snap.signer_kind;
  }
  const visible = new Set(visibleActions(snap));
  for (const btn of actionsEl.querySelectorAll("button")) {
    // TCK-WEB-028 (5): every repaint is snapshot truth — RESTORE the
    // in-flight disable a click applied (and, with it, never leave a
    // permanently disabled control: the server's own reply re-renders the
    // row whether the POST landed, failed, or the engine moved on).
    btn.disabled = false;
    // Skip the model/quick buttons — they are driven by model_state below.
    if (btn.classList.contains("model-only") || btn.classList.contains("quick-only")) {
      continue;
    }
    btn.hidden = !visible.has(btn.dataset.action);
  }
  paintHwVerifyButtons(); // TCK-HW-005: own-address chips (restore + gating)
  applyScanChip(snap);
  applyPrivacyChip(snap);
  applyWalletFpChip(snap); // TCK-WEB-027: the header fingerprint title chip
  applySuggestedServers(snap); // TCK-WEB-022: the click-to-FILL chip group
  applyWatchKeyGate(snap);
  applyModelPrompt(snap);
  paintConnLiveChip(); // TCK-WEB-031 (b): kind/host chip repaint (LAST —
  // both backendName (above) and backendHost (applyPrivacyChip) carry THIS
  // snapshot's truth by now; the noteTrustFlip reload trigger keeps its
  // established final position).
  // TCK-WEB-021 (8): the settings-door dot (wallet/backend unset — drive
  // the eye to setup). (5): the pinned reload trigger, LAST, so both
  // signature halves (backendName above, privacyMode in applyPrivacyChip)
  // carry THIS snapshot's truth before it is compared.
  paintSettingsDot();
  noteTrustFlip(typed);
}

// TCK-WEB-021 (5): the STALE-NOW-LINE fix, pinned: the pane reloads its
// settings exactly once per /state SNAPSHOT FLIP of backend_kind or
// privacy_mode (a chat-entered URL used to leave a confidently-wrong
// "Now using" line + trust badge up). The signature is the pair of LAST
// KNOWN typed NAMES; a flip while the pane is CLOSED only re-baselines
// (opening always fetches fresh). state/0 replies carry no new signature.
function noteTrustFlip(typed) {
  if (!typed) return;
  const sig = state.backendName + "|" + state.privacyMode;
  if (state.trustSig !== null && sig !== state.trustSig && !settingsPanelEl.hidden) {
    loadSettings();
  }
  state.trustSig = sig;
}

// TCK-WEB-021 (8): the state dot on the header Settings gear — lit while
// the setup is unfinished on typed truth ONLY: the wallet still needs its
// key (needs_watch_key, tracked by applyWatchKeyGate) or the backend choice
// is unresolved (privacy_mode awaiting_backend). Unknown/absent mode (no
// typed snapshot yet) never lights it — no guess, no nag. The dot itself
// is styles.css on the data-attribute (never an inline style); the word
// "dot" never reaches the AT (the pane is the affordance, the gear keeps
// its label).
function paintSettingsDot() {
  const unfinished =
    state.watchKeyNeeded === true || state.privacyMode === "awaiting_backend";
  settingsToggleEl.dataset.needsSetup = unfinished ? "1" : "";
}

// TCK-LAUNCH-002: the Yes/No card buttons and the model-free quick-action
// buttons render ONLY from the typed snapshot's additive model_state NAME
// (state/1; the shipped doctrine: never infer structure from prose, never
// guess on an unknown value). absent/failed = the card; declined/running =
// quick actions; ready / a real model (no model_state field) / an unknown
// name = neither. While the wallet still needs its key (the pane's zpub
// entry state) the model card stands down (provisioning is the single
// first-run ask).
function applyModelPrompt(snap) {
  const typed = snap && snap.schema === "state/1";
  const modelState = typed && typeof snap.model_state === "string" ? snap.model_state : "";
  // TCK-LAUNCH-004: the compose-area loading indicator rides this SAME typed
  // read — 'loading' only (ready/failed/absent/unknown never hold it); an
  // untyped state/0 reply keeps the last known value (no flicker while the
  // engine is briefly busy, the privacyMode discipline).
  if (typed) state.modelLoading = modelState === "loading";
  paintChatPlaceholder();
  const gated = state.watchKeyNeeded; // no wallet yet — the entry state owns the page
  const showCard = !gated && MODEL_CARD_STATES.has(modelState);
  const showQuick = !gated && MODEL_QUICK_STATES.has(modelState);
  for (const btn of actionsEl.querySelectorAll(".model-only")) {
    btn.hidden = !showCard;
  }
  for (const btn of actionsEl.querySelectorAll(".quick-only")) {
    btn.hidden = !showQuick;
  }
}

// TCK-LAUNCH-004: the ONE writer of the compose placeholder (the old inline
// ternary in applyWatchKeyGate folded here — both callers run on every /state
// snapshot; idempotent). The placeholder swap is the whole loading surface:
// same input, text only, ZERO layout shift, nothing announced (an AT reads a
// placeholder on focus; a chip would need a live region and shove the row).
// Precedence: the watch-key entry ask outranks loading (the entry state owns
// the page), loading outranks the normal placeholder. Both gate callers run
// per snapshot, so precedence is always re-applied from state truth — the
// failed outcome's honest surface is the existing model-absent card (shown by
// applyModelPrompt), never a stuck indicator.
function paintChatPlaceholder() {
  if (state.watchKeyNeeded) inputEl.placeholder = LABELS.chatNeedsKeyPlaceholder;
  else if (state.modelLoading) inputEl.placeholder = LABELS.chatLoadingPlaceholder;
  else inputEl.placeholder = chatPlaceholder;
}

// TCK-LAUNCH-001 first-run, chat-first per TCK-ONB-007's STATIC half (user
// correction 2026-09-11): needs_watch_key NEVER opens the settings pane and
// NEVER disables chat — the engine's greeting/backend beats run the
// first-run conversation IN CHAT; the pane opens only from its explicit
// controls and stays fully functional for later editing. The placeholder
// rides the TYPED snapshot (with the WEB-008 exception kept: a 200
// ``accepted`` is the ENGINE'S OWN confirmation the key parsed, gated and
// persisted, so the entry state DISMISSES on the spot — a terminal dismiss
// is never re-shown by a stale snapshot). (TCK-WEB-031 (c): the old
// WEB-009 (h) header balance quickbar rode this gate — it is removed; the
// wallet-fingerprint header TITLE follows its own typed gate below.)
function applyWatchKeyGate(snap) {
  const typed = !!snap && snap.schema === "state/1";
  let needs = typed && snap.needs_watch_key === true;
  if (state.watchKeyDismissed) needs = false;
  if (typed) state.watchKeyPresent = !needs;
  const wasNeeded = state.watchKeyNeeded;
  state.watchKeyNeeded = needs;
  paintChatPlaceholder();
  if (needs !== wasNeeded) renderSettings(); // flip the pane's zpub row
  if (needs && !wasNeeded && settingsPanelEl.hidden === false) focusWatchInput();
}

// The terminal success path of a watch-key submit (TCK-WEB-008 fix 1,
// TCK-WEB-009 (b)): collapse the pane's zpub row to the read-only truncated
// display; the dismiss makes the NEXT gated pass (any /state reply — the
// terminal engine truth WEB-008 keeps) restore the normal placeholder. The
// pane stays exactly as the user left it — TCK-ONB-007 killed the
// auto-open episode this used to hand off to.
function dismissWatchKeyForm(key) {
  state.sessionWatchKey = key; // memory only: the display fallback pre-/settings-read
  state.watchKeyDismissed = true;
  state.watchKeyPresent = true;
  state.watchKeyNeeded = false;
  state.watchKeyReplaceOpen = false; // applied → the row returns collapsed (§1)
  renderSettings();
}

// The scan chip reflects ONLY the typed snapshot's scan_state (additive under
// state/1). Unknown/absent values (state/0 fallback, future states) clear it —
// never a guess, never a hard-fail.
//
// TCK-WEB-020: the persistent failure line rides the SAME apply path. The
// typed snapshot's additive scan_error key (the value-free DIAG-001 class
// line, engine-owned copy) is rendered VERBATIM via textContent — present
// key shows it, absent key clears it (the engine drops it the moment a
// subsequent scan starts or lands, so staleness is server-authoritative;
// never empty-string framing, never client inference). state/0 keeps the
// last render untouched (the privacyMode discipline — a busy engine must
// not flicker the line off and re-announce it on the next typed snapshot).
// DOM writes happen ONLY on transition, one textContent assignment per
// change — the polite live region never spams per-snapshot.
function applyScanChip(snap) {
  scanChipEl.hidden = true;
  if (!snap || snap.schema !== "state/1") return;
  const rawError = snap.scan_error; // read ONCE; absent/non-string = clean
  const scanError = typeof rawError === "string" ? rawError : "";
  if (scanError !== state.scanError) {
    state.scanError = scanError;
    scanErrorEl.textContent = scanError; // verbatim — never parsed, never reworded
    scanErrorEl.hidden = scanError === "";
  }
  if (snap.scan_state === "pending" || snap.scan_state === "running") {
    scanChipEl.textContent = LABELS.scanLoading;
    scanChipEl.hidden = false;
  } else if (snap.scan_state === "skipped") {
    scanChipEl.textContent = LABELS.scanSkipped;
    scanChipEl.hidden = false;
  }
}

// TCK-UX-010: the persistent privacy label — the closed privacy_mode enum
// NAMES (the additive /state field) map to the subline copy; the raw enum
// never reaches the user as text (the word shown is the static "Privacy
// notice" node in index.html).
const PRIVACY_SUBLINE = {
  public: LABELS.privacyPublic,
  own_node_local: LABELS.privacyOwnLocal,
  // TCK-WEB-023: the FIFTH name — the private-range literal-IP mode (widened
  // by TCK-WEB-030 to literal-OR-resolved). The map key gates the NAME
  // (unknown names still clear the mode); TCK-WEB-030 supersedes the old
  // GREEN-chip behavior: the TOP badge is HIDDEN for this mode, while the
  // name keeps feeding the pane badge / kind-pill tint below.
  own_node_private: LABELS.privacyOwnPrivate,
  own_node_remote: LABELS.privacyOwnRemote,
  awaiting_backend: LABELS.privacyAwaiting,
};

// The two host-named modes (TCK-WEB-023 council fold): the {host} template
// substitutes ONLY when a VALIDATED engine host is present; absent/refused
// → the generic PRIVACY_SUBLINE sentence (omit-never-empty). public /
// own_node_local / awaiting_backend keep their unchanged copy.
const PRIVACY_HOST_TEMPLATES = {
  own_node_remote: LABELS.privacyOwnRemoteAt,
  own_node_private: LABELS.privacyOwnPrivateAt,
};

// Defensive host gate (ADR-0024 §7 spirit — the wire value is untrusted
// model-free server data, still validated before display): a BARE hostname /
// IP literal only. ANY refusal (@, ://, path/slash, whitespace, junk) falls
// back to the generic subline — a credential-bearing "host" is never painted
// even if the engine's parser ever regressed. Fails closed, value-free.
const BARE_HOST_RE = /^[A-Za-z0-9._\-:[\]]{1,253}$/;
function isBareHost(host) {
  return typeof host === "string" && BARE_HOST_RE.test(host);
}

// PURE (node-pinned): mode NAME + last typed host → the chip's subline copy.
// Never reads the effective chain URL — the host rides the typed /state
// backend_host field ONLY.
function privacySublineText(mode, host) {
  const tpl = PRIVACY_HOST_TEMPLATES[mode];
  if (tpl !== undefined && isBareHost(host)) {
    const at = tpl.indexOf("{host}");
    return tpl.slice(0, at) + host + tpl.slice(at + "{host}".length);
  }
  return PRIVACY_SUBLINE[mode] || "";
}

// TCK-WEB-031 (b) PURE (node-pinned): the LIVE connection chip's label from
// the last known typed pair (backend_kind NAME + backend_host). Only the
// two real kinds WITH a valid host gain "<Kind>: <host>"; kind
// none/awaiting/unknown/absent or a missing/refused host keeps today's
// generic word (pin f — fail closed, never a half-name like "Electrum: ").
function connLiveText(kind, host) {
  const word =
    kind === "electrum" ? LABELS.connElectrum
      : kind === "bitcoind" ? LABELS.connBitcoind
        : "";
  return word && isBareHost(host) ? word + ": " + host : LABELS.connConnected;
}

// TCK-WEB-031 (b): repaint the live chip when a /state snapshot moves the
// kind/host underneath it. Rides the existing state-diff discipline, so it
// never flickers across SSE reconnects: it writes ONLY while the stream is
// ALREADY live (connecting/reconnecting/unauthorized text belongs to
// listen() alone) and ONLY on an actual label CHANGE (a state/0 or an
// identical snapshot = zero DOM writes = zero live-region announcements).
function paintConnLiveChip() {
  if (statusEl.dataset.state !== "live") return;
  const label = connLiveText(state.backendName, state.backendHost);
  if (statusEl.textContent !== label) setStatus("live", label);
}

// TCK-WEB-013 (2): the chain row's trust badge — the SAME closed enum the
// header chip rides (privacy_mode from /state; never derived from the URL
// string client-side). Unknown/absent enum → no badge (existing discipline).
const TRUST_BADGE_WORDS = {
  own_node_local: LABELS.trustLocal,
  own_node_private: LABELS.trustPrivate,
  own_node_remote: LABELS.trustRemote,
  public: LABELS.trustPublic,
  awaiting_backend: LABELS.trustAwaiting,
};

// TCK-WEB-023 AMENDMENT: the kind pills (electrum/bitcoind; mempool stays
// gone — no key here means an unpinnable word, and the engine closed set is
// none/electrum/bitcoind). The WORD rides backend_kind, the TINT rides the
// SAME single privacy_mode classification the trust badge uses — GREEN iff
// the mode is one of the two own-node green names, YELLOW otherwise; no
// pill at kind none/unknown or an absent/awaiting/unknown mode. The tint is
// a CLASS name (styles.css owns the colors); nothing here sniffs a URL.
const KIND_PILL_WORDS = {
  electrum: LABELS.kindElectrum,
  bitcoind: LABELS.kindBitcoind,
};
const KIND_PILL_TINTS = {
  own_node_local: "kind-pill-private",
  own_node_private: "kind-pill-private",
  public: "kind-pill-public",
  own_node_remote: "kind-pill-public",
};

// PURE (node-pinned): the pill's paint plan for (kind, mode) — visible only
// when BOTH the word and the tint are known-closed values.
function kindPillPaint(kind, mode) {
  const words = KIND_PILL_WORDS[kind];
  const tint = KIND_PILL_TINTS[mode];
  if (words === undefined || tint === undefined) {
    return { visible: false, text: "", tint: "" };
  }
  return { visible: true, text: words, tint };
}

// The pill element for the chain row's status zone, or null (no pill today).
function kindPill() {
  const paint = kindPillPaint(state.backendName, state.privacyMode);
  if (!paint.visible) return null;
  return el("span", "kind-pill " + paint.tint, paint.text);
}

// The badge element for the "Now using" line, or null (unknown mode).
// Color rides data-privacy — the same attribute selector + token pair as
// the header chip (styles.css), so pane and header can never disagree.
function trustBadge() {
  const words = TRUST_BADGE_WORDS[state.privacyMode];
  if (!words) return null;
  const badge = el("span", "trust-badge", words);
  badge.dataset.privacy = state.privacyMode;
  return badge;
}

// Re-paint the pane's existing badges in place when /state moves the mode
// (the pane is not rebuilt by snapshots — renderSettings owns that).
function paintTrustBadges() {
  const words = TRUST_BADGE_WORDS[state.privacyMode] || "";
  for (const badge of settingsListEl.querySelectorAll(".trust-badge")) {
    badge.hidden = !words;
    if (words) {
      badge.dataset.privacy = state.privacyMode;
      badge.textContent = words;
    }
  }
  // TCK-WEB-023: the kind pills re-tint in the SAME pass (one snapshot of
  // truth paints the whole badge family — a mid-flip pane can never show a
  // green word under a yellow badge or vice versa).
  const pill = kindPillPaint(state.backendName, state.privacyMode);
  for (const node of settingsListEl.querySelectorAll(".kind-pill")) {
    node.hidden = !pill.visible;
    if (pill.visible) {
      node.className = "kind-pill " + pill.tint;
      node.textContent = pill.text;
    }
  }
}

// TCK-PRIVACY-001B: the consent button's visibility rides ONLY the persisted
// privacy_mode NAME from typed /state truth (awaiting_backend = unresolved
// first-run hold = show; a resolved choice = hide — the "Now using" line and
// the trust badge then carry the truth). Re-painted in place with the badges
// so a background /state flip retires the button without a pane rebuild.
function paintConsentRow() {
  const show = state.privacyMode === "awaiting_backend";
  for (const box of settingsListEl.querySelectorAll(".chain-consent")) {
    box.hidden = !show;
  }
}

// TCK-UX-010 + TCK-WEB-012 (f): two upgrades over the original chip. (1) The chip PERSISTS the last known mode
// across state/0 (engine-busy) snapshots — it must not blink out mid-turn;
// only a contradicting TYPED snapshot (a different valid name, or an
// unknown/absent one, which is never fabricated) replaces or clears it.
// (2) The trust subline is VISIBLE text (a small line under the chip word),
// not a hover-only title — readable without a mouse; the raw enum name
// still never reaches the user as text.
function applyPrivacyChip(snap) {
  if (snap && snap.schema === "state/1") {
    const mode = snap.privacy_mode;
    state.privacyMode =
      typeof mode === "string" &&
      Object.prototype.hasOwnProperty.call(PRIVACY_SUBLINE, mode)
        ? mode
        : "";
    // TCK-WEB-023: the additive typed backend_host NAME (a bare own-config
    // host or an ABSENT field = nothing to name — cleared, never kept stale;
    // state/0 keeps the last known value, the privacyMode discipline).
    const host = state.privacyMode === "" ? undefined : snap.backend_host;
    state.backendHost = typeof host === "string" ? host : "";
  }
  const mode = state.privacyMode;
  paintTrustBadges(); // TCK-WEB-013 (2): the pane badge rides the same truth
  paintConsentRow(); // TCK-PRIVACY-001B: likewise the public-consent button
  // TCK-WEB-030 (user direction 2026-09-15): own_node_private HIDES the top
  // badge entirely — a private-LAN server (the engine's widened
  // literal-OR-resolved classification) shows no chip. TOP BADGE ONLY: the
  // mode NAME still lands in state.privacyMode, so the pane trust badge, the
  // kind-pill tint and the settings hedge copy all keep their WEB-023
  // behavior; state/0 persists the name (chip stays hidden mid-busy) and a
  // later typed mode change re-shows the chip through the paint below.
  if (!mode || mode === "own_node_private") {
    privacyChipEl.hidden = true;
    privacyChipEl.removeAttribute("data-privacy");
    if (privacySublineEl.textContent !== "") privacySublineEl.textContent = "";
    return;
  }
  privacyChipEl.dataset.privacy = mode;
  // TCK-WEB-023: host-bearing for the two host-named modes when the engine
  // sent one (validated in privacySublineText/isBareHost), generic otherwise.
  // The chip is a live region: the subline is touched ONLY on a change
  // (the WEB-020 transition gate — a state/0 or an identical snapshot must
  // not re-write it and re-announce).
  const subline = privacySublineText(mode, state.backendHost);
  if (privacySublineEl.textContent !== subline) privacySublineEl.textContent = subline;
  privacyChipEl.hidden = false;
}

// TCK-DESCOPE-M3B deleted the old kind-badge machinery (dim/lit family
// pills). TCK-WEB-023 AMENDMENT: the additive backend_kind NAME rides
// /state again (closed enum none/electrum/bitcoind) and is consumed by the
// kindPill/kindPillPaint painter ONLY — never the resurrected dim/lit shape.

async function refreshState() {
  // Newest-wins (TCK-WEB-008): a snapshot that started before a provisioning
  // submit can land AFTER it (a busy engine serializes reads); applying that
  // stale answer would re-show the dismissed form, so only the newest reply
  // may touch the DOM.
  const seq = ++state.stateSeq;
  try {
    const response = await fetch("/state", { headers: authHeaders(), cache: "no-store" });
    if (!response.ok) return;
    const snap = await response.json();
    if (seq === state.stateSeq) applyState(snap);
  } catch {
    // server unreachable: leave visibility as-is; the stream status shows it
  }
}

// ------------------------------------------------- wallet fingerprint chip (TCK-WEB-027)
// The CLOSED shape again gates the wire here (the engine validates upstream,
// but every wire value is untrusted — same discipline as isBareHost): any
// non-matching value counts as ABSENT, never painted, never guessed.
const WALLET_FP_RE = /^[0-9a-f]{8}$/;

// PURE (node-pinned): the chip's visible text, its value-bearing accessible
// name, and the settings hint line. Only regex-valid fps ever reach them.
function walletFpChipText(fp) {
  return LABELS.walletFpWord + " " + fp;
}

function walletFpCopyName(fp) {
  return LABELS.walletFpCopyName + " " + fp;
}

function walletFpHintText(fp) {
  const tpl = LABELS.walletFpHint;
  const at = tpl.indexOf("{fp}");
  return tpl.slice(0, at) + fp + tpl.slice(at + "{fp}".length);
}

// Typed-state-only lifecycle (the WEB-020/023 doctrine): DOM writes ONLY on
// transition — one write per change, the polite copy region never spams.
// state/0 touches nothing (a busy engine must not flicker the chip); a typed
// snapshot that OMITS the field clears and hides it (unprovisioned, or a
// watch-key replace between snapshots). When the settings pane is open the
// wallet-section hint follows the same typed truth (renderSettings rebuilds
// from the cached entries — the noteTrustFlip/loadSettings precedent).
function applyWalletFpChip(snap) {
  if (!snap || snap.schema !== "state/1") return;
  const raw = snap.wallet_fingerprint;
  const fp = typeof raw === "string" && WALLET_FP_RE.test(raw) ? raw : "";
  if (fp === state.walletFingerprint) return;
  state.walletFingerprint = fp;
  walletFpEl.textContent = fp ? walletFpChipText(fp) : "";
  walletFpEl.hidden = fp === "";
  if (fp) {
    // WEB-026 naming rule: the NAME carries the whole value ("Copy wallet
    // fingerprint f1a2b3c4"), the hover title keeps the plain garnish.
    walletFpEl.setAttribute("aria-label", walletFpCopyName(fp));
    walletFpEl.title = LABELS.clickToCopy;
  } else {
    walletFpEl.removeAttribute("aria-label");
    walletFpEl.removeAttribute("title");
  }
  if (!settingsPanelEl.hidden) renderSettings();
}

// Click-to-copy reuses the SHARED WEB-026 machinery as-is — flashCopyResult
// already takes the value-bearing base name explicitly (it is not address/
// txid-coupled; clipboardWrite takes the raw fp). No new copy plumbing.
// The value is read AT CLICK time: a chip re-rendered by a replace can
// never hand out the OLD wallet's number.
walletFpEl.addEventListener("click", async () => {
  const fp = state.walletFingerprint;
  if (!fp) return;
  const name = walletFpCopyName(fp);
  flashCopyResult(walletFpEl, await clipboardWrite(fp), LABELS.clickToCopy, name);
});


// ------------------------------------------------------------- event stream

function handleEvent(id, kind, data) {
  if (id > 0) {
    if (id <= state.lastEventId) return; // replay duplicate guard
    state.lastEventId = id;
  }
  // Unknown future kinds are ignored, never fatal (server may outgrow us).
  if (kind === "text") appendText(data);
  else if (kind === "progress") appendProgress(data);
  else if (kind === "model_progress") renderModelProgress(data);
  else if (kind === "user_text") renderUserText(data);
  else if (kind === "own_address") noteOwnAddress(data); // TCK-HW-005 static
  else if (kind === "utxo_rows") noteUtxoRows(data); // TCK-UTXO-005 static
  else if (kind === "turn_end") noteTurnEnd();
  else if (kind === "resync") {
    // too_far_behind: the cursor predates the server ring, so part of the
    // transcript is unrecoverable — say so (never silently gap-fill), then
    // re-sync state. The retained backlog follows this frame on the same
    // stream; the duplicate guard above drops any of it we already saw.
    // TCK-WEB-011: echoes lost in the gap can never arrive, so drop the
    // pending list rather than let a stale entry swallow a future message.
    state.pendingEchos = [];
    appendSystem(LABELS.resyncGap);
    refreshState();
  }
}

function parseFrame(frame) {
  let id = 0;
  let kind = "";
  const dataLines = [];
  for (const line of frame.split("\n")) {
    if (line === "" || line.startsWith(":")) continue; // ": ping" comments
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "id") id = Number(value) || 0;
    else if (field === "event") kind = value;
    else if (field === "data") dataLines.push(value);
  }
  if (kind) handleEvent(id, kind, dataLines.join("\n"));
}

async function consumeStream(body) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) return; // server closed (sentinel/drop) -> caller replays
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) !== -1) {
      parseFrame(buffer.slice(0, cut));
      buffer = buffer.slice(cut + 2);
    }
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// EventSource cannot send headers, so the stream is fetch + ReadableStream
// with the per-launch token (ADR-0024 §5). Reconnect sends Last-Event-ID;
// the server replays its ring gap-free/duplicate-free.
async function listen() {
  while (!state.stopped) {
    try {
      // TCK-WEB-028 (3): a RETRY stays in the reconnecting state — the
      // "Connecting…" word is written on the first pass only, so the polite
      // live region never re-announces on the connecting↔reconnecting flip
      // every backoff step used to make.
      if (!state.reconnecting) setStatus("connecting", "Connecting…");
      const headers = authHeaders();
      if (state.lastEventId > 0) headers["Last-Event-ID"] = String(state.lastEventId);
      const response = await fetch("/events", { headers, cache: "no-store" });
      if (response.status === 401) {
        state.stopped = true;
        setStatus("unauthorized", LABELS.sessionStale);
        return;
      }
      if (!response.ok || !response.body) throw new Error(String(response.status));
      state.backoffMs = 500; // a live stream resets the backoff ladder
      // TCK-WEB-028 (3): the OUT-of-reconnecting transition is the one
      // announcement — this text write is it.
      state.reconnecting = false;
      setStatus("live", connLiveText(state.backendName, state.backendHost));
      if (state.everConnected) refreshState(); // reconnect: buttons may have moved
      state.everConnected = true;
      await consumeStream(response.body);
    } catch {
      // transport failure: treat like a closed stream and retry
    }
    if (state.stopped) return;
    // TCK-WEB-028 (3): announce only the TRANSITION into reconnecting; the
    // text is untouched while the state persists (no mutation = no
    // announcement), and the data-state dot keeps its steady style.
    if (!state.reconnecting) {
      state.reconnecting = true;
      setStatus("reconnecting", `Reconnecting to ${location.origin} …`);
    }
    await sleep(state.backoffMs + Math.floor(Math.random() * 250));
    state.backoffMs = Math.min(state.backoffMs * 2, 15000);
  }
}

// ------------------------------------------------------------------ sending

// One send path for typed lines AND button clicks: render the user echo
// (dimmed "queued" if a turn is already in flight — the server queue makes
// the FIFO order honest), POST it, then let the SSE stream carry the reply.
// 202 = queued only; busy clears via turn_end. A failed POST un-renders the
// echo: the engine never saw that line.
async function submit(path, field, value) {
  const queued = state.busy;
  const echo = appendUser(value, queued);
  if (queued) state.queue.push(echo);
  // TCK-WEB-011: the engine re-echoes every accepted line on the bus as a
  // ``user_text`` event (all tabs see it); this tab registers the pending
  // local echo so renderUserText can suppress its own copy.
  state.pendingEchos.push(value);
  setBusy(true);
  showPendingBubble(); // TCK-WEB-015: the busy face, right below the echo
  let status = 0;
  let reason = "";
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ [field]: value }),
    });
    status = response.status;
    if (!response.ok) {
      // TCK-WEB-028 (2): a rejection body is value-free by the server's own
      // contract (static strings only — "engine busy", "expected JSON
      // object…"); we quote it, never echo the submitted line. Unreadable
      // body → the plain sentence below.
      const data = await response.json().catch(() => null);
      if (data && typeof data.error === "string") reason = data.error;
      throw new Error(String(status));
    }
  } catch {
    echo.remove();
    const at = state.queue.indexOf(echo);
    if (at !== -1) state.queue.splice(at, 1);
    const pe = state.pendingEchos.indexOf(value);
    if (pe !== -1) state.pendingEchos.splice(pe, 1); // the engine never saw the line
    setBusy(state.queue.length > 0);
    if (!state.busy) clearPendingBubble(); // same replace rule: no busy, no bubble
    // TCK-WEB-016: a 401 is the per-launch token of a PREVIOUS wallet run
    // (stale tab) — "unreachable" would send the user hunting a live server.
    // Same honest sentence as the stream/resync/consent paths; only a
    // reload/the new URL fixes it.
    // TCK-WEB-028 (2): status 0 = the fetch itself threw (nothing was
    // reached) → the transport sentence. Any other non-ok code PROVES the
    // server answered → the refusal sentence (its own reason when present).
    appendSystem(
      status === 401
        ? LABELS.sessionStale
        : status === 0
          ? LABELS.unreachable
          : reason
            ? LABELS.turnRejectedPrefix + reason
            : LABELS.turnRejected,
    );
    // TCK-WEB-028 (5): the POST is dead — no turn_end will ever come for
    // this line, so re-read snapshot truth (restores a button disabled at
    // click; harmless no-op for typed submits).
    refreshState();
  }
}

formEl.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = inputEl.value.trim();
  if (!text) return;
  if (state.stopped) {
    // TCK-WEB-016: a stopped tab (stale token/401) must not swallow the
    // press silently with the text still in the box — show the same line.
    appendSystem(LABELS.sessionStale);
    return;
  }
  inputEl.value = "";
  submit("/turn", "text", text);
});

// Delegated listener (ADR-0024 §8): a click POSTs the button's canonical
// utterance to /action — the engine's FULL _run_turn path, the same gate
// classification a typed phrase meets. Nothing here calls a handler.
// TCK-LAUNCH-002: the model-card / quick-action buttons ride the SAME
// channel with the pump's canonical SLASH COMMANDS (/download, /later,
// /balance, /receive, /address) — deterministic engine-side intercepts,
// never model-classified. "Open settings" (no utterance) is client-only:
// the panel is already local. (TCK-WEB-031 (c/e): the header balance
// quickbar rode a twin of this listener and is removed; the model-free
// qa-balance "Show balance" button in THIS action bar keeps the same
// /balance channel — the card it renders carries the USD line, there is no
// separate USD utterance and none is invented.)
actionsEl.addEventListener("click", (event) => {
  const btn = event.target.closest("button");
  if (!btn || state.stopped) return;
  if (btn.dataset.action === "qa-settings") {
    openSettings();
    return;
  }
  if (btn.dataset.utterance) {
    // TCK-WEB-028 (5): disable the clicked control through the whole POST +
    // turn; the next /state repaint (applyState) restores it from snapshot
    // truth. A disabled button swallows the browser's own click, so a fast
    // double-click on Confirm queues "confirm" exactly once.
    btn.disabled = true;
    submit("/action", "utterance", btn.dataset.utterance);
  }
});

// ------------------------------------------------------------------ settings
// TCK-WEB-005 + TCK-WEB-009. GET /settings renders an allowlisted snapshot
// the SERVER owns; unknown keys/fields render generically (a text input) and
// never crash the panel. The pane's section order is fixed by the user
// direction: 1) wallet zpub, 2) chain base url (+ badges + Resync now),
// 3) gap limit. POSTs reuse the same auth header path as every other
// request. Values live only in panel DOM + in-memory render state — never
// logged, never stored client-side.

let inputSeq = 0; // unique label/id pairing inside the rebuilt panel

// A generic editable row (gap_limit, display_currency, and any future
// allowlisted key the client has no special row for): human label (closed
// map + raw-key fallback, TCK-WEB-021 (1)) + text/number input + Apply →
// POST /settings.
function settingRow(entry, index) {
  const li = el("li", "setting");
  li.dataset.key = entry.key; // refocusRowControl's stable row handle
  const keyId = "setting-input-" + index;

  const label = el("label", "setting-key", settingLabel(entry.key));
  label.htmlFor = keyId;
  li.appendChild(label);

  const line = el("div", "setting-line");
  const input = document.createElement("input");
  input.className = "setting-input";
  input.id = keyId;
  input.value = typeof entry.value === "string" ? entry.value : "";
  const bounded =
    entry.type === "int" && Number.isInteger(entry.min) && Number.isInteger(entry.max);
  input.type = bounded ? "number" : "text";
  if (bounded) {
    input.min = String(entry.min);
    input.max = String(entry.max);
  }
  if (entry.type === "url") input.placeholder = LABELS.settingsEmptyPlaceholder;
  const btn = el("button", "btn btn-secondary btn-small setting-apply", LABELS.settingsApply);
  btn.type = "button";
  btn.dataset.settingKey = entry.key;
  line.append(input, btn);
  li.appendChild(line);

  if (entry.requires_restart === true) {
    li.appendChild(el("p", "setting-flag", LABELS.settingsRestart));
  }
  if (entry.env_override === true) {
    li.appendChild(el("p", "setting-flag", LABELS.settingsEnvOverride));
  }
  const status = el("p", "setting-status");
  status.setAttribute("role", "status");
  li.appendChild(status);
  return li;
}

// Display-only shortening (TCK-WEB-008): head…tail, never mid-hash. The
// engine's settings entry is already truncated by the SAME rule server-side;
// this fallback only covers the pre-read window of a key typed in THIS page.
function truncateKey(key) {
  return key.length <= 24 ? key : key.slice(0, 12) + "…" + key.slice(-8);
}

// Live refs of the zpub ENTRY form (rebuilt whenever the row flips state).
let watchForm = null; // { input, submit }

function focusWatchInput() {
  if (watchForm && watchForm.input.isConnected) watchForm.input.focus();
}

// TCK-WEB-021 (6): an Escape/Cancel row rebuild must not drop keyboard
// focus to <body>. Rows carry a stable data-key handle; the rebuilt row's
// primary control gets focus, and the pane heading (the WEB-012 open-focus
// target) is the fallback whenever the row or control is gone.
function refocusRowControl(key) {
  const row = settingsListEl.querySelector('.setting[data-key="' + key + '"]');
  const target =
    row &&
    row.querySelector(".setting-apply, .watchkey-line button, .watchkey-display-line button");
  (target || settingsHeadingEl).focus();
}

// The settings pane's zpub section (TCK-WEB-009 a/b; copy pass 2 §1; entry
// form reworked by TCK-WEB-021 (6)/(7)). States, all rendered from typed
// /state truth plus the server's entry:
//  * ENTRY (wallet needs a key): title + input + Connect + ONE reassurance
//    line; the full lecture (where to find the key, the seed/private-key
//    warning) collapses behind a native <details>. Submit is the ONE
//    POST /watchkey channel (the same path the replaced card used,
//    replace/confirm rung kept); ALL key validation is the engine's
//    parse+gate, refusals relayed value-free.
//  * SET: a collapsed display (the engine's truncated descriptor,
//    "wpkh([e7f511…" style) + an Edit button. No Show/Copy/Replace
//    affordances (user direction): the full value is never needed here.
//    Edit (same verb as the chain-base row) flips the row to the
//    replace-mode form — no request; submit rides the same POST /watchkey,
//    and the engine's 409 raises the existing confirm/apply rungs.
function watchKeyRow(serverEntry) {
  const li = el("li", "setting setting-watchkey");
  li.dataset.key = "watch_key"; // refocusRowControl's stable row handle
  const status = el("p", "setting-status");
  status.setAttribute("role", "status");
  watchForm = null;

  if (state.watchKeyPresent === null && !serverEntry) {
    li.appendChild(el("p", "setting-key", settingLabel("watch_key")));
    li.appendChild(el("p", "watchkey-value", LABELS.watchKeyRowUnknown));
    li.appendChild(status);
    return li;
  }
  const needsEntry =
    state.watchKeyNeeded ||
    (serverEntry ? serverEntry.configured !== true : state.watchKeyPresent !== true);

  if (needsEntry || state.watchKeyReplaceOpen) {
    // ENTRY, or the §1 Edit rung of the replace cycle (flip to the form,
    // replace-mode copy, no request — the submit is the SAME
    // submitWatchKey path; the engine answers ``already`` (409) and raises
    // the existing confirm rung below it; a silent overwrite is engine-
    // impossible). TCK-WEB-021 (6): a VISIBLE <label> names the input
    // (htmlFor/id pairing, no aria-label duplication).
    const replacing = !needsEntry;
    if (!replacing) state.watchKeyReplaceOpen = false; // the ENTRY form owns the row
    inputSeq += 1;
    const inputId = "watchkey-input-" + inputSeq;
    const label = el("label", "setting-key", LABELS.watchKeyInputLabel);
    label.htmlFor = inputId;
    const more = el("details", "setting-details");
    more.appendChild(el("summary", "setting-details-summary", LABELS.watchkeyFindSummary));
    more.appendChild(
      el("p", "setting-hint", replacing ? LABELS.watchkeyReplaceLede : LABELS.watchkeyLede),
    );
    more.appendChild(el("p", "setting-flag", LABELS.watchkeyWarning));
    const box = el("div", "watchkey-replace"); // confirm / apply rungs of a replace
    const line = el("div", "watchkey-line");
    const input = watchKeyInput();
    input.id = inputId;
    const btn = el(
      "button",
      "btn btn-primary btn-small",
      replacing ? LABELS.watchKeyReplaceSubmit : LABELS.watchKeyConnect,
    );
    btn.type = "button";
    line.append(input, btn);
    // ENTRY gets the ONE reassurance line (the lecture collapsed above it);
    // the replace form keeps its own lede inside the details.
    li.append(label, line);
    if (!replacing) li.appendChild(el("p", "setting-hint", LABELS.watchkeyReassure));
    li.append(more, box, status);
    btn.addEventListener("click", () => submitWatchKey(input, btn, status, box));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        submitWatchKey(input, btn, status, box);
      }
    });
    if (replacing) {
      // Cancel collapses back to the display; nothing sent. (TCK-WEB-021
      // (6): focus lands on the rebuilt row's Edit button, not <body>.)
      const no = el("button", "btn btn-secondary btn-small", LABELS.watchKeyReplaceCancel);
      no.type = "button";
      no.addEventListener("click", () => {
        state.watchKeyReplaceOpen = false;
        renderSettings();
        refocusRowControl("watch_key");
      });
      line.appendChild(no);
    }
    watchForm = { input, submit: btn };
    return li;
  }

  // SET — collapsed display + the §1 Edit affordance. TCK-WEB-021 (2): the
  // configured wallet is the pane's heaviest fact (.watchkey-value in
  // styles.css); the label carries the human word, never "watch_key".
  const display =
    serverEntry && typeof serverEntry.value === "string"
      ? serverEntry.value
      : state.sessionWatchKey
        ? truncateKey(state.sessionWatchKey)
        : LABELS.watchKeyRowConnected;
  li.appendChild(el("p", "setting-key", settingLabel("watch_key")));
  const line = el("div", "watchkey-line watchkey-display-line");
  line.appendChild(el("p", "watchkey-value", display));
  const editBtn = el("button", "btn btn-secondary btn-small", LABELS.settingsEdit);
  editBtn.type = "button";
  editBtn.addEventListener("click", () => {
    state.watchKeyReplaceOpen = true;
    renderSettings();
    focusWatchInput(); // mirrors the chain-base Edit rung: focus where typing continues
  });
  line.appendChild(editBtn);
  li.appendChild(line);
  // TCK-WEB-027: the wallet section's fingerprint hint line — the same
  // typed truth the header chip rides, plus the HW-002 honesty note (the
  // device shows a DIFFERENT number; mismatch is expected). Typed-only:
  // no known fingerprint, no line — never a guess at a value.
  if (state.walletFingerprint) {
    li.appendChild(
      el("p", "setting-hint", walletFpHintText(state.walletFingerprint)),
    );
  }
  if (serverEntry && serverEntry.env_override === true) {
    li.appendChild(el("p", "setting-flag", LABELS.watchKeyEnvOverride));
  }
  li.appendChild(status);
  return li;
}

// The watch-key entry input (shared by the ENTRY form and the §1
// replace-mode form). TCK-WEB-021 (6): the visible <label> (htmlFor/id at
// the call sites) is the input's name now — no duplicate aria-label.
function watchKeyInput() {
  const input = document.createElement("input");
  input.type = "text";
  input.spellcheck = false;
  input.autocomplete = "off";
  input.maxLength = 200;
  input.placeholder = "zpub…";
  return input;
}

// The chain-base row (TCK-WEB-009 d/e/f, TCK-WEB-013; zoned by TCK-WEB-021
// (3)). THREE zones inside the server card:
//  * STATUS (.chain-status): the effective backend — "Now using: <url>"
//    with real visual weight ((2): the most privacy-relevant fact is no
//    longer the smallest text) + the trust badge riding the /state
//    privacy_mode closed enum ONLY + (TCK-WEB-023 AMENDMENT) the
//    electrum/bitcoind kind PILLs beside it, tinted by the SAME closed
//    classification (styles.css color-vocabulary contract; none/absent/
//    awaiting → no pill — kindPill returns null).
//  * ACT (.chain-act): field + Apply/Edit/Cancel + (TCK-WEB-022) the
//    .chain-chips slot holding the click-to-FILL suggested-server chip
//    group (warn sentence first, then one <button> per vetted server — see
//    chainChipsGroup; absent engine list = no group at all).
//  * REST (.chain-rest): the explanatory prose (empty-field legend, creds
//    notes, env/restart flags) collapsed behind a native <details>.
//    Resync-now and the gap_limit row stay VISIBLE outside it — the
//    recovery path must not hide.
// A stored value renders read-only + Edit→Apply (the existing POST
// /settings write; the engine probes before saving and hot-swaps
// server-side — ADR-0018 amendment); an EMPTY field is directly typeable
// (Apply straight away), and the env rung suppresses the field's whole
// write path for one honest note. Edit/typing gain Cancel + Escape, which
// rebuild the row from engine truth with no request and REFOCUS the row's
// control (TCK-WEB-021 (6)). While a write (or the engine's probe inside
// it) is in flight the button is disabled: no double-submit, and the wait
// word is "Checking the server…" (seconds-class probe, not a save).
function chainBaseRow(entry) {
  inputSeq += 1;
  const li = el("li", "setting setting-chain");
  li.dataset.key = entry.key; // refocusRowControl's stable row handle
  const keyId = "setting-input-" + inputSeq;
  const label = el("label", "setting-key", settingLabel(entry.key));
  label.htmlFor = keyId;
  li.appendChild(label);

  // TCK-WEB-013 (5): an env-rung entry is not this field's to write — no Edit rung.
  const envRung = entry.env_override === true;
  const status = el("p", "setting-status");
  status.setAttribute("role", "status");

  // --- status zone -------------------------------------------------------
  // TCK-WEB-013 (1): the effective backend is ALWAYS shown when the server
  // carries it (an empty stored field answers EMPTY since TCK-DESCOPE-M3A —
  // unresolved, no silent public default; a recorded public consent lands
  // the named public Electrum server on this line), with the trust badge
  // riding the privacy_mode enum beside it. Field absent (bare pump) → the
  // zone stays empty (never fabricated).
  const statusZone = el("div", "chain-status");
  if (state.effectiveChainUrl) {
    const nowLine = el("div", "chain-now-line");
    nowLine.appendChild(
      el("p", "chain-now", LABELS.settingsNowUsing + " " + state.effectiveChainUrl)
    );
    const badge = trustBadge();
    if (badge) nowLine.appendChild(badge);
    // TCK-WEB-023: the kind pill joins the trust badge HERE (same status
    // zone, same wrap line); null = kind none/absent or mode not yet known.
    const pill = kindPill();
    if (pill) nowLine.appendChild(pill);
    statusZone.appendChild(nowLine);
  }
  li.appendChild(statusZone);

  // TCK-PRIVACY-001B: the ONLY web trigger of public-backend consent. It
  // shows while the typed /state privacy_mode enum says the backend choice
  // is unresolved (awaiting_backend) and hides the moment a choice exists
  // (engine truth repainted by paintConsentRow — never a client guess).
  // Closing the pane, asking a balance, or any other action never rides
  // this path. The leak disclosure sits right above the button.
  const consent = el("div", "chain-consent");
  consent.appendChild(el("p", "setting-flag", LABELS.consentSubline));
  const consentLine = el("div", "setting-line");
  const consentBtn = el("button", "btn btn-secondary btn-small", LABELS.consentPublic);
  consentBtn.type = "button";
  consentBtn.addEventListener("click", () => requestPublicConsent(consentBtn, status));
  consentLine.appendChild(consentBtn);
  consent.appendChild(consentLine);
  consent.hidden = state.privacyMode !== "awaiting_backend";
  li.appendChild(consent);

  // --- act zone ----------------------------------------------------------
  const actZone = el("div", "chain-act");
  const line = el("div", "setting-line");
  const input = document.createElement("input");
  input.className = "setting-input";
  input.id = keyId;
  input.type = "text";
  input.spellcheck = false;
  input.autocomplete = "off";
  input.value = typeof entry.value === "string" ? entry.value : "";
  // TCK-WEB-013 (3): an EMPTY stored field is directly typeable — Edit
  // earns its keep only when a value exists (and never on the env rung,
  // where a stored write is shadowed anyway).
  const directlyTypeable = !envRung && input.value.trim() === "";
  input.readOnly = !directlyTypeable;
  if (!envRung) input.placeholder = LABELS.settingsEmptyPlaceholder;
  // Escape-cancel must tell a touched directly-typeable field from an
  // untouched one (the field never was "opened" by an Edit click).
  input.dataset.dirty = "0";
  input.addEventListener("input", () => {
    input.dataset.dirty = "1";
    updateCredsVisibility(li, input);
  });
  const btn = el(
    "button",
    "btn btn-secondary btn-small setting-apply",
    directlyTypeable ? LABELS.settingsApply : LABELS.settingsEdit,
  );
  btn.type = "button";
  btn.dataset.settingKey = entry.key;
  // TCK-WEB-013 (3): Cancel for the edit mode — restores read-only from
  // ENGINE truth (renderSettings rebuilds from the entry, the
  // replace-cancel shape), no request. Visible exactly while the field is
  // editable. TCK-WEB-021 (6): after the rebuild, focus returns to the
  // row's Apply/Edit control — never <body>.
  const cancel = el("button", "btn btn-secondary btn-small setting-cancel", LABELS.settingsCancel);
  cancel.type = "button";
  cancel.hidden = !directlyTypeable;
  cancel.addEventListener("click", () => {
    renderSettings();
    refocusRowControl("chain_base_url");
  });
  if (!envRung) line.append(input, btn, cancel);
  else line.appendChild(input);
  actZone.appendChild(line);
  // TCK-WEB-022: the suggested-server chip slot rides the act zone, below
  // the URL field / beside the Apply path it feeds. No chip list, no group.
  actZone.appendChild(el("div", "chain-chips"));
  li.appendChild(actZone);
  renderChainChips(li);

  // TCK-DESCOPE-M3B: the old kind-badge STRIP (legend + mempool chips) stays
  // deleted here — no legend, no mempool, no idle/dim state. The TCK-WEB-023
  // AMENDMENT pill lives in the STATUS zone beside the trust badge, and the
  // trust badge still carries the whole privacy truth (the pill only says
  // WHICH software, tinted by the same classification).

  // Resync-now (TCK-WEB-009 f): VISIBLE, OUTSIDE the rest zone — the
  // recovery path must not hide behind a <details>.
  const resyncLine = el("div", "setting-line setting-resync-line");
  const resyncBtn = el("button", "btn btn-ghost btn-small", LABELS.resyncNow);
  resyncBtn.type = "button";
  resyncBtn.addEventListener("click", () => requestResync(resyncBtn, status));
  resyncLine.appendChild(resyncBtn);
  li.appendChild(resyncLine);

  // --- rest zone ---------------------------------------------------------
  // TCK-WEB-021 (3): the explanatory prose collapses behind a native
  // <details> (no JS, no aria invention — the browser owns the toggle).
  const restZone = el("details", "setting-details chain-rest");
  restZone.appendChild(el("summary", "setting-details-summary", LABELS.chainRestNotes));
  // TCK-WEB-013 (5): the "Empty = no server chosen" legend lies under an
  // env rung (the effective URL line above carries the truth) — suppressed there.
  if (!envRung) {
    restZone.appendChild(el("p", "setting-hint", LABELS.settingsEmptyIsDefault));
  }
  if (entry.requires_restart === true) {
    restZone.appendChild(el("p", "setting-flag", LABELS.settingsRestart));
  }
  if (envRung) {
    restZone.appendChild(el("p", "setting-flag", LABELS.chainEnvOverride));
  }
  // TCK-ONB-004 M3: the credential NOTES (set/unset sentences) and their
  // rare clear affordance ride inside the rest zone. The engine's secret
  // entries report only SET/UNSET (never the value), so the fields start
  // empty on every render; the checkbox mirrors the stored none-flag.
  const credFlags = backendCredFlags();
  if (credFlags.none) {
    restZone.appendChild(el("p", "setting-flag creds-note", LABELS.credsNoneSaved));
  } else if (credFlags.user || credFlags.pass) {
    restZone.appendChild(el("p", "setting-flag creds-note", LABELS.credsSavedNote));
  }
  if (credFlags.none || credFlags.user || credFlags.pass) {
    const clearLine = el("div", "setting-line setting-creds-clear-line");
    const clearBtn = el("button", "btn btn-secondary btn-small", LABELS.credsClear);
    clearBtn.type = "button";
    clearBtn.addEventListener("click", () => {
      clearBackendCreds(clearBtn, status, li);
    });
    clearLine.appendChild(clearBtn);
    restZone.appendChild(clearLine);
  }
  li.appendChild(restZone);

  // The login FIELDS stay outside the collapsed zone: they surface only
  // while EDITING an http:// (ambiguous — may be Core RPC) or bitcoind://
  // address (https is a Core-RPC input alias — creds ride the dedicated
  // keys, fields never appear for it — and ssl:// Electrum has no standard
  // auth); a mid-edit user must never have to hunt a closed <details>.
  const creds = el("div", "setting-creds");
  creds.hidden = true;
  const noneLine = el("label", "setting-creds-none");
  const noneBox = document.createElement("input");
  noneBox.type = "checkbox";
  noneBox.className = "creds-none";
  noneBox.checked = credFlags.none;
  noneLine.append(noneBox, document.createTextNode(" " + LABELS.credsNoneLabel));
  const userIn = document.createElement("input");
  userIn.type = "text";
  userIn.className = "setting-input creds-user";
  userIn.spellcheck = false;
  userIn.autocomplete = "off";
  userIn.maxLength = 256;
  userIn.placeholder = LABELS.credsUserLabel;
  userIn.setAttribute("aria-label", LABELS.credsUserLabel);
  const passIn = document.createElement("input");
  passIn.type = "password";
  passIn.className = "setting-input creds-pass";
  passIn.autocomplete = "new-password";
  passIn.maxLength = 256;
  passIn.placeholder = LABELS.credsPassLabel;
  passIn.setAttribute("aria-label", LABELS.credsPassLabel);
  const credsLine = el("div", "setting-line");
  credsLine.append(userIn, passIn);
  creds.append(noneLine, credsLine, el("p", "setting-hint", LABELS.credsHint));
  li.appendChild(creds);
  li.dataset.credsNoneInitial = credFlags.none ? "1" : "0";

  li.appendChild(status);
  return li;
}

// ------------------------------------------------ suggested public servers (TCK-WEB-022)
// The vetted public-Electrum chip list is ENGINE truth: the additive typed
// /state ``suggested_servers`` array of {url, label} objects (code-owned,
// probe-verified literals — never user data, never a client echo). Chips are
// click-to-FILL, NEVER click-to-apply: a click lands the URL verbatim in the
// row's field and moves focus there (qwen#5), so a public→public switch still
// passes through the row's ONE Apply path — the delegated [data-setting-key]
// handler, where the leak disclosure and the engine probe live. The group
// (role="group", real <button>s, warn sentence BEFORE the chips — DOM order =
// SR order) renders only when the array is non-empty; typed missing = no
// group, state/0 keeps the last known group (the backend_host/walletFingerprint
// discipline), and a re-render fires ONLY when the array actually changes
// (static today — render-once + presence-gated).
//
// Wire gate (ADR-0024 §7 spirit — every wire value is untrusted, re-validated
// before display): an entry survives only as a {url, label} of two non-empty
// strings; anything else is dropped, never painted, never guessed. The values
// reach the DOM through el()/textContent ONLY (label as button text; the url
// as the field's value — inert text, never markup, never a link href).
function readSuggestedServers(raw) {
  if (!Array.isArray(raw)) return [];
  const out = [];
  for (const entry of raw) {
    if (entry === null || typeof entry !== "object") continue;
    const url = entry.url;
    const label = entry.label;
    if (typeof url !== "string" || url === "") continue;
    if (typeof label !== "string" || label === "") continue;
    out.push({ url: url, label: label });
  }
  return out;
}

// PURE (node-pinned): the chip group for one row, or null = render nothing
// (never an empty group). `fill` is the click behavior, injected so the
// builder itself touches no fetch and no flow object.
function chainChipsGroup(servers, fill) {
  if (!Array.isArray(servers) || servers.length === 0) return null;
  const group = el("div", "chain-chip-group");
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", LABELS.chainChipsGroup);
  group.appendChild(el("p", "chain-chips-warn", LABELS.chainChipsWarn));
  for (const server of servers) {
    const chip = el("button", "chain-chip", server.label);
    chip.type = "button";
    chip.addEventListener("click", () => fill(server.url));
    group.appendChild(chip);
  }
  return group;
}

// PURE (node-pinned): the ONE thing a chip click does — fill the row's field
// with the vetted URL verbatim and focus it. If the field is read-only (a
// stored value), the row's own Edit rung opens first: the delegated Apply
// handler's isChain && input.readOnly branch does exactly that with NO
// request, and reusing the real button (never a duplicated unlock) keeps the
// one write path honest. No POST, no data-setting-key, no trim, no rewrite.
function fillChainServer(input, applyBtn, url) {
  if (input.readOnly) applyBtn.click();
  input.value = url;
  input.dataset.dirty = "1";
  input.focus();
}

// Populate (or clear) one chain card's chip slot. A row WITHOUT a live
// Apply/Edit control — the env rung, whose field has no write path — gets no
// chips: filling a field nothing can apply is a dead affordance.
function renderChainChips(li) {
  const slot = li.querySelector(".chain-chips");
  if (!slot) return;
  const input = li.querySelector(".setting-input");
  const applyBtn = li.querySelector(".setting-apply");
  const group =
    input && applyBtn
      ? chainChipsGroup(state.suggestedServers, (url) =>
          fillChainServer(input, applyBtn, url),
        )
      : null;
  slot.textContent = "";
  if (group) slot.appendChild(group);
}

function paintChainChips() {
  for (const li of settingsListEl.querySelectorAll(".setting-chain")) {
    renderChainChips(li);
  }
}

// Typed-state-only lifecycle (the WEB-020/023/027 doctrine): state/0 touches
// nothing (a busy engine never flickers the group away); a typed snapshot
// without the key clears it (absent = nothing to offer); the DOM is written
// ONLY when the gated array actually differs.
function applySuggestedServers(snap) {
  if (!snap || snap.schema !== "state/1") return;
  const servers = readSuggestedServers(snap.suggested_servers);
  const sig = JSON.stringify(servers);
  if (sig === state.suggestedServersSig) return;
  state.suggestedServers = servers;
  state.suggestedServersSig = sig;
  paintChainChips();
}

// What the engine's secret entries said at the last full read (the ONLY
// credential facts the client may know: set/unset flags, never values).
function backendCredFlags() {
  const get = (key) => {
    const entry = state.settings && state.settings.get(key);
    return !!(entry && entry.configured === true);
  };
  return {
    user: get("backend_auth_user"),
    pass: get("backend_auth_pass"),
    none: get("backend_auth_none"),
  };
}

// The login block shows only while editing an auth-capable scheme.
function updateCredsVisibility(row, input) {
  const creds = row.querySelector(".setting-creds");
  if (!creds) return;
  const value = input.value.trim().toLowerCase();
  creds.hidden = !(
    input.readOnly === false &&
    (
      value.startsWith("http://") ||
      value.startsWith("bitcoind://") ||
      value.startsWith("bitcoind+tls://")
    )
  );
}

// One POST /settings write: {code, data} — the engine's closed ``status``
// is the truth the caller judges, and the HTTP code rides along because a
// 401 has its own honest sentence (TCK-WEB-013 item 6) that a parse-only
// return would lose.
async function postSetting(key, value) {
  const response = await fetch("/settings", {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ key, value }),
  });
  return { code: response.status, data: await response.json().catch(() => null) };
}

// The credential overlay the chain-base Apply submits AS PART OF the URL
// POST (TCK-ONB-004 M3 security-review LOW 2): the engine lands it, probes
// with it — "creds ride the probe and the built client" — and rewinds the
// prior record if the address is refused, so a rejected Apply never strands
// new creds against the old URL. The checkbox wins outright when checked:
// explicit no-auth, pair wiped.
function credWrites(row) {
  const box = row.querySelector(".creds-none");
  if (!box) return [];
  const user = row.querySelector(".creds-user");
  const pass = row.querySelector(".creds-pass");
  const wasNone = row.dataset.credsNoneInitial === "1";
  if (box.checked) {
    const writes = wasNone ? [] : [["backend_auth_none", "1"]];
    if (wasNone || user.value || pass.value) {
      writes.push(["backend_auth_user", ""], ["backend_auth_pass", ""]);
    }
    return writes;
  }
  const writes = wasNone ? [["backend_auth_none", ""]] : [];
  if (user.value || pass.value) {
    writes.push(["backend_auth_user", user.value], ["backend_auth_pass", pass.value]);
  }
  return writes;
}

// Local courtesy gate (the engine re-validates fail-closed regardless): a
// pair is written TOGETHER or not at all — a half-typed login is refused
// on the spot instead of silently falling back to the cookie ladder.
function localCredProblem(row) {
  const box = row.querySelector(".creds-none");
  if (!box || box.checked) return "";
  const user = row.querySelector(".creds-user");
  const pass = row.querySelector(".creds-pass");
  if ((user.value && !pass.value) || (pass.value && !user.value)) {
    return LABELS.credsNeedBoth;
  }
  return "";
}

// Clear login: three "" writes (the typed-writer clear convention), then a
// local flag flip — the note lines are the only visible state and the next
// full read renders engine truth anyway.
async function clearBackendCreds(btn, status, row) {
  if (btn.disabled || state.stopped) return;
  btn.disabled = true;
  status.dataset.kind = "";
  status.textContent = LABELS.settingsSaving;
  try {
    for (const key of ["backend_auth_user", "backend_auth_pass", "backend_auth_none"]) {
      const { code, data } = await postSetting(key, "");
      if (code === 401) {
        // TCK-WEB-013 (6): a retry can never succeed on a stale token.
        status.dataset.kind = "error";
        status.textContent = LABELS.sessionStale;
        return;
      }
      if (!data || data.status !== "applied") {
        status.dataset.kind = "error";
        status.textContent =
          data && typeof data.error === "string"
            ? LABELS.settingsRejectedPrefix + " " + data.error + LABELS.settingsRejectNext
            : LABELS.settingsFailed;
        return;
      }
    }
    status.dataset.kind = "ok";
    status.textContent = LABELS.credsCleared;
    row.dataset.credsNoneInitial = "0";
    row.querySelectorAll(".creds-note").forEach((note) => note.remove());
    const clearLine = row.querySelector(".setting-creds-clear-line");
    if (clearLine) clearLine.remove();
  } catch {
    status.dataset.kind = "error";
    status.textContent = LABELS.unreachable;
  } finally {
    btn.disabled = false;
  }
}

// (e): the badge painter (paintBackendBadges) is DELETED by TCK-DESCOPE-M3B
// along with the kind badges — see the note in the settings section.

// (f): POST /resync carries no data; the transport maps the closed engine
// status (started→202, busy→409, anything else→503). 202: the scan chip
// reappears from the next /state truth (the engine also emits turn_end).
// Disabled while in flight — no double-submit.
async function requestResync(btn, status) {
  if (btn.disabled || state.stopped) return;
  btn.disabled = true;
  status.dataset.kind = "";
  status.textContent = LABELS.resyncSaving;
  let note = LABELS.resyncUnavailable;
  let kind = "error";
  try {
    const response = await fetch("/resync", { method: "POST", headers: authHeaders() });
    const data = await response.json().catch(() => null);
    const closed =
      data && data.schema === "resync/1" && typeof data.status === "string"
        ? data.status
        : null;
    if ((response.status === 202 && closed === "started") || (response.status === 202 && closed === null)) {
      note = LABELS.resyncStarted;
      kind = "ok";
    } else if (response.status === 409 || closed === "busy") {
      note = LABELS.resyncBusy;
      kind = "";
    } else if (response.status === 401) {
      // TCK-WEB-013 (6): a stale per-launch token — retrying can never
      // succeed; say what actually fixes it (same sentence as the stream).
      note = LABELS.sessionStale;
    }
  } catch {
    note = LABELS.unreachable;
    kind = "error";
  } finally {
    status.dataset.kind = kind;
    status.textContent = note;
    btn.disabled = false;
    refreshState(); // the scan chip is the truth, never this echo
  }
}

// TCK-PRIVACY-001B: ONE fetch path, reached ONLY by the consent button above.
// POST /consent carries no data; the ENGINE pump performs the record+release
// and answers the closed status (loading = the held first-run scan started —
// the F2 contract; recorded = choice stands with nothing held). The chips,
// the beat and the button's own visibility then follow from the next /state
// snapshot — never from this echo (same discipline as requestResync).
async function requestPublicConsent(btn, status) {
  if (btn.disabled || state.stopped) return;
  btn.disabled = true;
  status.dataset.kind = "";
  status.textContent = LABELS.consentSaving;
  let note = LABELS.consentFailed;
  let kind = "error";
  try {
    const response = await fetch("/consent", { method: "POST", headers: authHeaders() });
    const data = await response.json().catch(() => null);
    const closed =
      data && data.schema === "consent/1" && typeof data.status === "string"
        ? data.status
        : null;
    if (response.status === 200 && closed !== null && closed !== "unavailable") {
      note = closed === "loading" ? LABELS.consentLoading : LABELS.consentRecorded;
      kind = "warn"; // a leak disclosure earns the beat's warn tone, not "ok"
    } else if (response.status === 401) {
      note = LABELS.sessionStale; // a retry can never fix a stale token
    }
  } catch {
    note = LABELS.unreachable;
  } finally {
    status.dataset.kind = kind;
    status.textContent = note;
    btn.disabled = false;
    refreshState(); // engine truth repaints the chips and retires the button
  }
}

// Rebuild the pane from in-memory render state (last settings read + typed
// /state). Section order is fixed (user direction a): Wallet (zpub), then
// Network & scanning (chain base row, gap row, then any future generic
// keys). Rows are rebuilt only on open/fetch or a watch-state flip — an
// in-flight edit is never clobbered by a background /state refresh.
function renderSettings() {
  const entries = state.settings;
  const watch = watchKeyRow(entries ? entries.get("watch_key") : null);
  const walletCol = settingsCol(LABELS.settingsSectionWatchKey, [watch]);
  const networkRows = [];
  if (entries) {
    const chain = entries.get("chain_base_url");
    if (chain) networkRows.push(chainBaseRow(chain));
    const gap = entries.get("gap_limit");
    if (gap) {
      inputSeq += 1;
      networkRows.push(settingRow(gap, inputSeq));
    }
    for (const entry of entries.values()) {
      // watch_key/chain_base_url/gap_limit have their own rows; the three
      // secret credential entries (TCK-ONB-004 M3) render INSIDE the chain-
      // base row's login block — a generic text row would beg for a value
      // the server will never return.
      if (!SPECIAL_SETTING_KEYS.has(entry.key)) {
        inputSeq += 1;
        networkRows.push(settingRow(entry, inputSeq));
      }
    }
  }
  if (networkRows.length > 0) {
    settingsListEl.replaceChildren(
      walletCol,
      settingsCol(LABELS.settingsSectionNetwork, networkRows),
    );
  } else {
    settingsListEl.replaceChildren(walletCol);
  }
}

function settingsCol(title, rows) {
  const col = el("section", "settings-col");
  col.appendChild(el("h3", "setting-heading", title));
  const ul = el("ul", "settings-col-list");
  ul.append(...rows);
  col.appendChild(ul);
  return col;
}

async function loadSettings() {
  // The zpub section renders from the typed /state truth alone at first —
  // honest even when GET /settings is unavailable (a first-run engine has no
  // settings store yet, or is mid-provision). Once the server list lands,
  // the rows render from its entries: the watch_key entry is NEVER an
  // editable row (read-only surface; changing the key is the engine-gated
  // POST /watchkey path) and chain_base_url gets its Edit/Apply row.
  settingsRetryEl.hidden = true; // a new attempt speaks for itself
  settingsStatusEl.textContent = LABELS.settingsLoading;
  renderSettings();
  try {
    const response = await fetch("/settings", { headers: authHeaders(), cache: "no-store" });
    const data = response.ok ? await response.json().catch(() => null) : null;
    if (!data || !Array.isArray(data.settings)) {
      showSettingsUnavailable();
      return;
    }
    settingsStatusEl.textContent = "";
    const entries = new Map();
    for (const entry of data.settings) {
      // an entry without a string key is unrenderable: skip it, keep the rest
      if (!entry || typeof entry.key !== "string") continue;
      entries.set(entry.key, entry);
    }
    state.settings = entries;
    // TCK-WEB-013 (1): the additive effective-URL field — stamped when the
    // engine has chain wiring, omitted (null) on bare pumps. Reset first:
    // a reply without the field must not keep a stale line up.
    state.effectiveChainUrl =
      typeof data.effective_chain_base_url === "string"
        ? data.effective_chain_base_url
        : null;
    // TCK-WEB-023 AMENDMENT: the additive backend_kind NAME is consumed
    // again — the FRESHEST word for the kind pill (a chat-entered swap while
    // the pane sat closed can outrun the last /state tick; the pill's TINT
    // still rides privacy_mode only, never this string's content).
    if (typeof data.backend_kind === "string") {
      state.backendName = data.backend_kind;
    }
    renderSettings();
  } catch {
    showSettingsUnavailable();
  }
}

// TCK-WEB-012 (g): the load-failure line gets a visible Retry beside it —
// the pane's ONLY unavailability path — rerunning the same GET /settings.
function showSettingsUnavailable() {
  settingsStatusEl.textContent = LABELS.settingsUnavailable;
  settingsRetryEl.hidden = false;
  settingsRetryEl.disabled = false;
}

settingsRetryEl.addEventListener("click", () => {
  settingsRetryEl.disabled = true; // one attempt in flight; loadSettings re-arms
  loadSettings();
});

// Inline pre-POST check for bounded int keys; the engine re-validates
// fail-closed anyway. Returns "" when the value may be sent.
function localSettingProblem(input) {
  if (input.type !== "number") return "";
  const text = input.value.trim();
  const min = Number(input.min);
  const max = Number(input.max);
  if (/^\d+$/.test(text)) {
    const n = Number(text);
    if (n >= min && n <= max) return "";
  }
  return LABELS.settingsRange(min, max);
}

// Delegated (same pattern as /action): the chain-base button runs the
// Edit↔Apply state machine ((d): read-only → Edit click → editable + Apply
// → write → read-only); every [data-setting-key] Apply click POSTs one
// {key, value} to /settings and renders the server's honest status.
// Rejection bodies are value-free; we show the server's own reason line,
// inventing no detail. The reply's additive resync/swapped fields (TCK-BACK-
// END-002) pick the applied note ((g): gap-limit "resyncing…" vs "already up
// to date", straight from the response — never inferred).
settingsListEl.addEventListener("click", async (event) => {
  const btn = event.target.closest("button[data-setting-key]");
  if (!btn || state.stopped) return;
  const row = btn.closest(".setting");
  const input = row.querySelector("input");
  const status = row.querySelector(".setting-status");
  const isChain = btn.dataset.settingKey === "chain_base_url";
  if (isChain && input.readOnly) {
    // Edit rung: no request, no state change — just open the field (and,
    // TCK-ONB-004 M3, the login block if the scheme can carry one).
    input.readOnly = false;
    input.focus();
    btn.textContent = LABELS.settingsApply;
    const cancel = row.querySelector(".setting-cancel");
    if (cancel) cancel.hidden = false; // WEB-013 (3): Cancel earns its keep now
    status.dataset.kind = "";
    status.textContent = "";
    updateCredsVisibility(row, input);
    return;
  }
  const problem = localSettingProblem(input) || (isChain ? localCredProblem(row) : "");
  status.dataset.kind = "error";
  if (problem) {
    status.textContent = problem;
    return;
  }
  btn.disabled = true; // in-flight (engine probe included): no double-submit
  // TCK-WEB-021 (6): the chain row's wait word names the seconds-class
  // PROBE the engine runs inside the Apply — "Checking the server…".
  status.textContent = isChain ? LABELS.settingsChecking : LABELS.settingsSaving;
  const submitted = input.value.trim(); // emptiness test only — never re-echoed
  try {
    // TCK-ONB-004 M3 (security-review LOW 2): the login no longer rides
    // separate POSTs BEFORE the address — it rides the SAME Apply. The
    // engine probes with the new creds first and commits creds+URL together
    // only on success; a refused address rewinds the pair server-side.
    let auth = null;
    if (isChain) {
      const writes = credWrites(row);
      if (writes.length > 0) auth = Object.fromEntries(writes);
    }
    const response = await fetch("/settings", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(
        auth
          ? { key: btn.dataset.settingKey, value: submitted, auth }
          : { key: btn.dataset.settingKey, value: submitted }
      ),
    });
    const data = await response.json().catch(() => null);
    if (response.status === 200 && data && data.status === "applied") {
      // TCK-DESCOPE-M3B: the empty-chain-base special note is GONE. Since
      // TCK-DESCOPE-M3A a wired engine REFUSES an empty apply (no silent
      // public default to switch to) and an unwired pump merely clears the
      // choice — both read honestly through the plain/RESYNC notes below.
      status.dataset.kind = "ok";
      const note =
        typeof data.resync === "string" && RESYNC_NOTES.has(data.resync)
          ? RESYNC_NOTES.get(data.resync)
          : LABELS.settingsApplied;
      // TCK-GAP-001 follow-up: when the apply carries an engine tradeoff
      // note (data.note — today the value-free GAP_NARROW_NOTE on a gap
      // DECREASE), render it too, on the same status line (the established
      // prefix + engine-string pattern, e.g. settingsRejectedPrefix above).
      // Non-string/empty notes render nothing; still textContent-only.
      status.textContent =
        typeof data.note === "string" && data.note
          ? note + " " + data.note
          : note;
      // confirm from the server's freshly re-read entry, never our own echo
      const fresh = Array.isArray(data.settings) ? data.settings[0] : null;
      if (fresh && Object.prototype.hasOwnProperty.call(fresh, "value")) {
        input.value = typeof fresh.value === "string" ? fresh.value : "";
      }
      if (isChain) {
        // (d) + TCK-WEB-013 (3): applied → read-only with Edit again — but
        // an applied EMPTY value lands directly-typeable (with the leak
        // note above already naming what just happened).
        input.readOnly = input.value.trim() !== "";
        btn.textContent = input.readOnly ? LABELS.settingsEdit : LABELS.settingsApply;
        const cancel = row.querySelector(".setting-cancel");
        if (cancel) cancel.hidden = input.readOnly;
        input.dataset.dirty = "0";
        // (1): this reply is a sealed one too — follow the effective URL,
        // with the GET's reset-if-absent rule (TCK-WEB-013 (1)): an applied
        // reply without the field means bare pump now — null it and drop the
        // line, never leave the previous URL painted.
        state.effectiveChainUrl =
          typeof data.effective_chain_base_url === "string"
            ? data.effective_chain_base_url
            : null;
        const now = row.querySelector(".chain-now");
        if (state.effectiveChainUrl) {
          if (now) {
            now.textContent = LABELS.settingsNowUsing + " " + state.effectiveChainUrl;
          }
        } else if (now) {
          now.remove();
        }
        // (M3): the login fields close with the row — the pair lives only
        // in the engine's store now; refresh the local set/unset facts from
        // what this Apply actually wrote (the next full read confirms
        // engine truth; the values themselves never come back).
        const creds = row.querySelector(".setting-creds");
        if (creds) {
          creds.hidden = true;
          const user = row.querySelector(".creds-user");
          const pass = row.querySelector(".creds-pass");
          const box = row.querySelector(".creds-none");
          if (!box.checked && user.value && pass.value) {
            row.dataset.credsNoneInitial = "0";
            if (!row.querySelector(".creds-note")) {
              // TCK-WEB-021 (3): the note is prose — it joins the collapsed
              // rest zone (rendered notes live there too), not the row tail.
              const rest = row.querySelector(".chain-rest");
              const note = el("p", "setting-flag creds-note", LABELS.credsSavedNote);
              if (rest) rest.appendChild(note);
              else row.insertBefore(note, creds);
            }
          }
          if (box.checked) {
            row.dataset.credsNoneInitial = "1";
          }
          user.value = "";
          pass.value = "";
        }
      }
      if (state.settings && fresh && typeof fresh.key === "string") {
        state.settings.set(fresh.key, fresh);
      }
      // (a swap may have moved the backend_kind NAME — TCK-DESCOPE-M3B:
      // the client no longer reads it, the kind badges are gone).
      if (data.resync === "started") {
        refreshState(); // the scan chip follows, from engine truth
      }
    } else if (response.status === 400 && data && data.status === "rejected") {
      status.dataset.kind = "error";
      // TCK-WEB-021 (10): every rejection line ends in the SAME static
      // value-free next-step suffix (the suffix never varies, never
      // echoes — the server's own reason is the only dynamic part).
      status.textContent =
        typeof data.error === "string"
          ? LABELS.settingsRejectedPrefix + " " + data.error + LABELS.settingsRejectNext
          : LABELS.settingsRejected + LABELS.settingsRejectNext;
      // a refused swap/probe: the field stays editable for a correction
    } else if (response.status === 401) {
      // TCK-WEB-013 (6): a stale per-launch token — retrying can never
      // succeed; say what actually fixes it (the stream's own sentence).
      status.dataset.kind = "error";
      status.textContent = LABELS.sessionStale;
    } else {
      // 503 (engine busy) and anything unexpected: one honest retry line
      status.dataset.kind = "error";
      status.textContent = response.status === 503 ? LABELS.settingsBusy : LABELS.settingsFailed;
    }
  } catch {
    status.dataset.kind = "error";
    status.textContent = LABELS.unreachable;
  } finally {
    btn.disabled = false;
  }
});

// TCK-WEB-012 (g): Enter inside a setting row's main field (the chain-base
// input, the gap row, any future generic row) triggers that row's primary
// action — the same Edit→Apply verb as a click, matching the watch-key
// forms' Enter handlers. The login block's fields are deliberately out:
// their verb is the row's Apply fired from the URL field, never a partial
// credential submit.
settingsListEl.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  const input = event.target;
  if (!(input instanceof HTMLInputElement) || input.type === "checkbox") return;
  if (input.closest(".setting-creds")) return;
  const btn = input
    .closest(".setting")
    .querySelector("button[data-setting-key]");
  if (!btn || btn.disabled) return;
  event.preventDefault();
  btn.click();
});

function openSettings() {
  if (settingsPanelEl.hidden) {
    settingsPanelEl.hidden = false;
    settingsToggleEl.setAttribute("aria-expanded", "true");
    loadSettings(); // fetch on demand, fresh every time the panel opens
    // TCK-WEB-012 (d): opening moves focus INTO the pane — the zpub entry
    // input when the first-run form owns the pane, else the pane heading
    // (a tabindex="-1" programmatic landmark, announced but not tab-stopped).
    if (state.watchKeyNeeded) focusWatchInput();
    else settingsHeadingEl.focus();
  }
}

// TCK-WEB-012 (c/d): closing returns focus to the header Settings toggle (so
// the keyboard never drops to <body>). TCK-ONB-007 static half: no
// first-run/beat handoff lives here anymore — the pane is purely explicit-
// control driven, and the WEB-013 (4) beat retired with it (closing is just
// closing; never an implied consent, never a re-open trigger).
function closeSettings() {
  if (!settingsPanelEl.hidden) {
    settingsPanelEl.hidden = true;
    settingsToggleEl.setAttribute("aria-expanded", "false");
    settingsToggleEl.focus();
  }
}

settingsToggleEl.addEventListener("click", () => {
  if (settingsPanelEl.hidden) openSettings();
  else closeSettings();
});

settingsCloseEl.addEventListener("click", () => closeSettings());

// TCK-WEB-012 (c): Escape while the pane is open. An OPEN EDIT ROW cancels
// FIRST (watch-key replace form → collapsed display; editing chain-base /
// generic row → renderSettings() rebuilds from engine truth, discarding
// the unsaved input); the NEXT Escape closes the pane. With nothing open,
// one Escape closes. TCK-WEB-021 (6): the QR dialog is the TOPMOST layer —
// while it is open its own listener consumes Escape and this handler
// returns early (a single press must never close both). A cancel rebuilds
// the row and REFOCUSES its control (focus never drops to <body>).
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || settingsPanelEl.hidden) return;
  if (!qrViewerEl.hidden) return;
  if (state.watchKeyReplaceOpen) {
    event.preventDefault();
    state.watchKeyReplaceOpen = false;
    renderSettings();
    refocusRowControl("watch_key");
    return;
  }
  // The URL field only — the login block's fields are never readonly and
  // live hidden when not editing, so a class-scoped match is required.
  // TCK-WEB-013 (3): cancel the row only when an edit is actually OPEN —
  // a value present (Edit rung) or a touched directly-typeable field; an
  // untouched empty field is not an open edit, so Escape closes the pane.
  // TCK-WEB-021 (3): the field lives in the act zone — a descendant match
  // through .chain-act still excludes the login block by class.
  const editing = settingsPanelEl.querySelector(
    ".setting-chain .setting-line > input.setting-input:not(.creds-user):not(.creds-pass):not([readonly])",
  );
  if (editing && (editing.value.trim() !== "" || editing.dataset.dirty === "1")) {
    event.preventDefault();
    renderSettings(); // row back to read-only + Edit; nothing was sent
    refocusRowControl("chain_base_url");
    return;
  }
  event.preventDefault();
  closeSettings();
});

// ------------------------------------------------- watch key entry (001/009)
// The key string rides ONLY to POST /watchkey (token/Host/Origin-gated, like
// every mutation); ALL validation is the engine's existing parse+gate path.
// We relay the server's value-free status line verbatim — the submitted key
// is never re-rendered, echoed into the transcript, or logged. ONE fetch path
// (TCK-WEB-008): the pane's entry form and the replace rung are the same
// POST /watchkey — no second endpoint, no client-side verdict. The replace
// rung (TCK-LAUNCH-002) adds the explicit DOUBLE opt-in the engine demands:
// replace:true AND confirm:true, raised only when the engine itself answers
// ``already`` (409) to a fresh submit.
async function postWatchKey(key, replace = false) {
  try {
    const response = await fetch("/watchkey", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(replace ? { key, replace: true, confirm: true } : { key }),
    });
    const data = await response.json().catch(() => null);
    return { code: response.status, data };
  } catch {
    return { code: 0, data: null }; // transport failure: caller shows unreachable
  }
}

async function submitWatchKey(input, btn, status, box) {
  const key = input.value.trim();
  if (!key || btn.disabled) return;
  btn.disabled = true;
  status.dataset.kind = "";
  status.textContent = LABELS.watchkeySaving;
  const { code, data } = await postWatchKey(key);
  if (code === 200 && data && (data.status === "accepted" || data.status === "replaced")) {
    // TCK-WEB-008 fix 1: the engine just confirmed the key — collapse the
    // row NOW (no waiting on the next /state round-trip, which can stall
    // behind the post-provision banner), and keep "Connected." where it
    // survives: the transcript + the connection status.
    status.textContent = "";
    dismissWatchKeyForm(key);
    appendSystem(LABELS.watchkeyConnected);
  } else if (code === 409 && data && data.status === "already") {
    // The engine holds a key already: the replace/confirm rung (never a
    // silent overwrite — the double opt-in rides on confirm).
    replaceStage("confirm", input, btn, status, box, key);
  } else if (
    (code === 400 || code === 409) &&
    data &&
    typeof data.error === "string"
  ) {
    // Rejection reasons are value-free by the engine's contract — safe to
    // quote; we add nothing but the static next-step suffix (TCK-WEB-021
    // (10)), and nothing here can echo the submitted key.
    status.dataset.kind = "error";
    status.textContent =
      LABELS.watchkeyRejectedPrefix + " " + data.error + LABELS.watchkeyRejectNext;
  } else if (code === 0) {
    status.dataset.kind = "error";
    status.textContent = LABELS.unreachable;
  } else if (code === 401) {
    // TCK-WEB-013 (6): stale token — the reload sentence, never "try again".
    status.dataset.kind = "error";
    status.textContent = LABELS.sessionStale;
  } else if (code === 503) {
    status.dataset.kind = "error";
    status.textContent = LABELS.watchkeyBusy;
  } else {
    status.dataset.kind = "error";
    status.textContent = LABELS.watchkeyFailed;
  }
  btn.disabled = false;
  // Re-read the engine's truth either way: the snapshot re-syncs button/scan
  // state, and (post-dismiss, TCK-WEB-008) can no longer resurrect the form.
  refreshState();
}

// The replace confirm/apply stages, inline in the pane's zpub section
// ((a): the entry logic — including the engine's replace+confirm semantics
// — moved here with the form). The engine re-runs the SAME parse+gate path
// on the ENGINE thread; its closed status is the whole verdict, relayed
// value-free.
function replaceStage(stage, input, btn, status, box, key) {
  status.textContent = "";
  status.dataset.kind = "";
  box.replaceChildren();
  box.hidden = false;
  if (stage === "confirm") {
    box.appendChild(el("p", "setting-flag", LABELS.watchKeyReplaceConfirm));
    const yes = el("button", "btn btn-danger btn-small", LABELS.watchKeyReplaceYes);
    yes.type = "button";
    yes.addEventListener("click", () => replaceStage("apply", input, btn, status, box, key));
    const no = el("button", "btn btn-secondary btn-small", LABELS.watchKeyReplaceCancel);
    no.type = "button";
    no.addEventListener("click", () => {
      box.hidden = true;
      box.replaceChildren();
    });
    box.append(yes, no);
    return;
  }
  btn.disabled = true;
  status.dataset.kind = "";
  status.textContent = LABELS.watchkeySaving;
  postWatchKey(key, true).then(({ code, data }) => {
    btn.disabled = false;
    if (code === 200 && data && (data.status === "replaced" || data.status === "accepted")) {
      // The engine re-wired onto the new key: collapse the row and re-read
      // tool truth (fresh /settings + /state), never from this echo.
      box.hidden = true;
      box.replaceChildren();
      dismissWatchKeyForm(key);
      loadSettings();
      refreshState();
    } else {
      // Rejection reasons are value-free by the engine's contract.
      const reason =
        code === 0
          ? LABELS.unreachable
          : code === 401
            ? LABELS.sessionStale // TCK-WEB-013 (6)
            : data && typeof data.error === "string"
              ? LABELS.watchkeyRejectedPrefix + " " + data.error + LABELS.watchkeyRejectNext
              : LABELS.watchkeyFailed;
      status.dataset.kind = "error";
      status.textContent = reason;
    }
  });
}

// -------------------------------------------------------------------- start

refreshState(); // (a) on load

if (!token) {
  // The island is server-injected; absence means this file was not served
  // by the wallet server. Nothing here can work without it.
  state.stopped = true;
  setStatus(
    "down",
    "This page wasn't opened from your wallet — start local-wallet and open the address it prints.",
  );
  inputEl.disabled = true;
  sendBtn.disabled = true;
} else {
  listen();
}

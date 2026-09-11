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
// is an Edit→Apply read-only cycle with backend badges + a Resync-now
// action, and the header carries the model-free balance quick actions.
// TCK-WEB-013: the chain row always shows the effective backend URL with a
// privacy_mode-driven trust badge, the empty field is directly typeable
// (Edit/Cancel only once a value exists), the env rung gets one honest
// note instead of the write path, first run stays in the pane under a
// one-time public-default leak beat, and a 401 on any pane POST says
// "reload" rather than "try again".

const island = window.__LOCALWALLET__;
const token = island && typeof island.token === "string" ? island.token : "";

const transcriptEl = document.getElementById("transcript");
const hintEl = document.getElementById("hint");
const statusEl = document.getElementById("conn-status");
const formEl = document.getElementById("turn-form");
const inputEl = document.getElementById("turn-text");
const sendBtn = document.getElementById("turn-send");
const busyEl = document.getElementById("turn-busy");
const scrollerEl = document.getElementById("scroller");
const actionsEl = document.getElementById("actions");
const quickbarEl = document.getElementById("quickbar");
const scanChipEl = document.getElementById("scan-chip");
const privacyChipEl = document.getElementById("privacy-chip");
const privacySublineEl = document.getElementById("privacy-subline");
const settingsToggleEl = document.getElementById("settings-toggle");
const settingsPanelEl = document.getElementById("settings-panel");
const settingsHeadingEl = document.getElementById("settings-heading");
const settingsCloseEl = document.getElementById("settings-close");
const settingsRetryEl = document.getElementById("settings-retry");
const settingsStatusEl = document.getElementById("settings-status");
const settingsListEl = document.getElementById("settings-list");

// The leak sentence, shared verbatim by the empty-apply note and the
// first-run beat (TCK-WEB-013 item 4 reuses the copy pass 2 #52 wording —
// one string, so the two disclosures can never drift).
const PUBLIC_LEAK_SENTENCE =
  "whoever runs it sees every address you check, can link those to your IP, " +
  "and watches when your transactions move.";

// One map for every user-facing string this file injects (designer pass —
// button labels live in index.html markup, likewise for rewording).
const LABELS = {
  resyncGap: "Reconnected — some earlier messages may be missing.",
  queuedTag: "queued",
  unreachable:
    "Could not reach the wallet server — is it still running? Check the terminal where you started it.",
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
  privacyAwaiting: "No backend chosen yet.",
  // settings panel (TCK-WEB-005)
  settingsLoading: "Loading…",
  settingsUnavailable: "Could not load settings — the wallet is busy or unreachable.",
  settingsApply: "Apply",
  settingsEdit: "Edit",
  settingsSaving: "Saving…",
  settingsApplied: "Applied.",
  settingsRejected: "Rejected — no reason given.",
  settingsRejectedPrefix: "Rejected:",
  settingsBusy: "The wallet is busy — try again.",
  settingsFailed: "Could not save — try again.",
  settingsRestart: "Takes effect after restart.",
  // split per docs/ux-web-copy.md §3: short form in the field, full
  // privacy-disclosure line under it
  settingsEmptyPlaceholder: "Empty = public default",
  settingsEmptyIsDefault:
    "Empty = public default — its operator can link your queries to your IP.",
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
  watchKeyReplaceConfirm:
    "The cached balance and history belong to the current wallet; " +
    "replacing discards any pending transaction and re-scans for the new " +
    "one (the old wallet's data stays on this machine, unused). Replace " +
    "the wallet's key?",
  watchKeyReplaceYes: "Replace",
  watchKeyReplaceCancel: "Cancel",
  // copy pass 2 §1: the SET row's Edit→form replace mode (the rungs below
  // this one are the existing 409-confirm/apply stages, unchanged).
  watchkeyReplaceLede:
    "Paste the public account key (xpub, ypub, or zpub) of the wallet you " +
    "want to watch instead.",
  watchKeyReplaceSubmit: "Replace wallet",
  // backend badges (TCK-WEB-009 e) — the words are the closed enum family
  // names the engine's backend_kind maps onto (TCK-ONB-004 M3: every
  // family in the closed enum is live and emittable now).
  badgeMempool: "mempool",
  badgeElectrum: "electrum",
  badgeBitcoind: "bitcoind",
  // TCK-WEB-010: bubble copy control (aria-label + transient title states).
  copyMessage: "Copy message",
  copyDone: "Copied",
  copyFailed: "Copy failed",
  // copy pass 2 #44: dim/lit is a state-carrying indicator, so it gets
  // words — a legend line plus a dynamic title/aria-label per badge
  // (recomposed by paintBackendBadges, the sole badge painter).
  badgeLegend:
    "The highlighted name is the kind of server the app asks about your " +
    "addresses.",
  badgeInUse:
    "In use — the app checks your addresses against this kind of server.",
  badgeIdle: "Not in use.",
  // TCK-ONB-004 M3: the bitcoind badge is a LIVE family now (Core RPC is
  // selectable and auto-detected) — the old "not available yet" line was
  // retired with the M2/M3 flip; every badge now shares in-use/idle words.
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
  // copy pass 2 #52 (UNDER-WARN fix): an EMPTY chain-base apply that lands
  // (the engine hot-swaps to the public default) must name the leak AS IT
  // LANDS — the placeholder hint above the field is not the consent beat.
  // Tested for emptiness only; the value itself is never echoed (value-free).
  settingsEmptyApplied:
    "Switched to the public mempool.space server — " +
    PUBLIC_LEAK_SENTENCE +
    " Enter your own server's address to switch back.",
  // TCK-WEB-013: (1) the effective-backend line (the ADDITIVE
  // effective_chain_base_url from the /settings replies — the public
  // default becomes VISIBLE when the stored rung is empty; absent = the
  // line is omitted, never fabricated). (4) the one-time first-run beat,
  // the same leak named up front instead of only after an empty Apply.
  settingsNowUsing: "Now using:",
  firstRunBeat:
    "You are on the public mempool.space server — " +
    PUBLIC_LEAK_SENTENCE +
    " Add your own server's address below — or close Settings to continue " +
    "for now.",
  // (5) the env rung's honest note (value-free: names the mechanism and
  // the file, never the env VALUE beyond the URL already shown).
  chainEnvOverride:
    "Set by environment variable — change it there or in " +
    "~/.localwallet/config.json.",
  settingsCancel: "Cancel",
  // (2) the chain row's trust badge — rides the /state privacy_mode
  // closed enum ONLY (never derived from the URL string client-side).
  trustLocal: "on this computer",
  trustRemote: "your own machine (remote)",
  trustPublic: "public server — see privacy notice",
  trustAwaiting: "not set up yet",
  // (6) the stale-token sentence: shared with the stream path — a pane
  // POST that 401s can never be fixed by retrying.
  sessionStale:
    "This page\u2019s session no longer matches the wallet — reload this page.",
  resyncNoteBusy: "Saved — a scan is already running; it will use the new value.",
  resyncNoteDeferred: "Saved — the re-scan is queued behind the current scan.",
  resyncNoteSkipped:
    "Saved — your environment configuration outranks this one; it applies at next restart.",
  resyncNoteUnchanged: "Already up to date — no re-scan needed.",
  resyncNoteUnavailable: "Saved — no re-scan could start right now.",
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

// TCK-WEB-009 (e): the three dimmed badges next to the chain-base field and
// the closed backend_kind → badge mapping (the engine's enum, app.py
// BACKEND_KINDS). mempool/electrum/bitcoind are the badge families: every
// http(s) Esplora-shaped backend (the public mempool.space default, a
// self-hosted mempool /api install, a root-served electrs/esplora) is the
// "mempool" family; ssl:// is electrum; bitcoind is the M2/M3 Core-RPC
// adapter (emitted since the M3 stored rung — including the http://
// auto-detect rewrite). "none"/unknown: all dim.
// ponytail: one badge per family, not per enum name — a fourth "esplora"
// badge only earns its pixels when the two ever behave differently here.
const BADGE_FAMILIES = {
  mempool: new Set(["mempool", "public", "esplora"]),
  electrum: new Set(["electrum"]),
  bitcoind: new Set(["bitcoind"]),
};
const BACKEND_KINDS = new Set([
  "none", "electrum", "public", "mempool", "esplora", "bitcoind",
]);
// Keys the pane renders specially (own rows / own block) — everything else
// on the allowlist falls through to the generic text row. The credential
// trio (TCK-ONB-004 M3) belongs to the chain-base row's login block.
const SPECIAL_SETTING_KEYS = new Set([
  "watch_key", "chain_base_url", "gap_limit",
  "backend_auth_user", "backend_auth_pass", "backend_auth_none",
]);

// The closed resync statuses the settings/resync replies carry (app.py
// RESYNC_STATUSES) → honest inline note. An unknown value renders the plain
// "Applied." line — never a guess.
const RESYNC_NOTES = new Map([
  ["started", LABELS.resyncNoteStarted],
  ["busy", LABELS.resyncNoteBusy],
  ["deferred", LABELS.resyncNoteDeferred],
  ["skipped", LABELS.resyncNoteSkipped],
  ["unchanged", LABELS.resyncNoteUnchanged],
  ["unavailable", LABELS.resyncNoteUnavailable],
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
  // TCK-WEB-012 (f): the last KNOWN privacy_mode NAME from a typed
  // snapshot — state/0 (engine busy) must not blank the chip mid-turn.
  privacyMode: "",
  stateSeq: 0,
  watchKeyDismissed: false,
  watchKeyPresent: null, // null = unknown | true | false (typed state/1 only)
  watchKeyNeeded: false, // the pane renders the zpub ENTRY form iff true
  sessionWatchKey: "",
  settingsAutoShown: false, // first-run auto-open episode armed/active
  // TCK-WEB-013 (4): the one-time first-run backend beat. Set on a
  // successful Connect DURING the auto-open episode (the pane then stays
  // open under it); cleared when the user closes the pane themselves —
  // that close is the explicit "not now" (no re-open loop, no re-show).
  firstRunBeat: false,
  // TCK-WEB-013 (1): the effective chain base URL from the last settings
  // read (creds-stripped server-side; null = field absent = omit the
  // "Now using" line, never fabricate). Memory only, like every value.
  effectiveChainUrl: null,
  // TCK-LAUNCH-002: the model card + inline download progress. The card /
  // quick-action buttons are shown ONLY from the typed snapshot's additive
  // model_state NAME (never prose); the progress line is an inline element
  // fed by the int-only model_progress events (percent + bytes, value-free).
  downloadLine: null, // <p> currently receiving the inline download bar
  // TCK-WEB-009: the pane's data. The last successfully read settings
  // entries (in memory only — never persisted, never logged) plus the live
  // backend_kind NAME, so the zpub row can flip form↔display on a /state
  // transition without a refetch storm (the engine is routinely busy —
  // refetching right after a provisioning accept would 503).
  settings: null, // null | Map<key, entry>
  backendKind: "",
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
  state.busy = busy;
  busyEl.hidden = !busy;
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

// TCK-WEB-010: per-bubble copy control. The copyable text of a turn is its
// message lines only (progress dots and the model-download bar are transient
// telemetry, not message text); a system bubble holds its text on the li
// itself. textContent read, textContent copy — the XSS contract never
// serializes markup here.
function bubbleText(turn) {
  const lines = turn.querySelectorAll(".turn-text:not(.turn-progress):not(.turn-model)");
  if (lines.length > 0) return Array.from(lines, (line) => line.textContent).join("\n").trim();
  return (turn.textContent || "").trim();
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
// (empty / progress-only bubbles get none). localhost is a secure context so
// navigator.clipboard normally exists; a missing API or a rejected write
// lands in the visible fail state (class + title only — never an alert,
// never an inline style).
function addCopyButton(turn) {
  if (turn.querySelector(".copy-btn") || !bubbleText(turn)) return;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "copy-btn";
  btn.setAttribute("aria-label", LABELS.copyMessage);
  btn.title = LABELS.copyMessage;
  btn.appendChild(copyIcon());
  let resetTimer = 0;
  btn.addEventListener("click", async () => {
    const text = bubbleText(turn);
    if (!text) return;
    let ok = false;
    try {
      await navigator.clipboard.writeText(text);
      ok = true;
    } catch {
      ok = false;
    }
    btn.classList.remove("copy-ok", "copy-fail");
    btn.classList.add(ok ? "copy-ok" : "copy-fail");
    btn.title = ok ? LABELS.copyDone : LABELS.copyFailed;
    clearTimeout(resetTimer);
    resetTimer = setTimeout(() => {
      btn.classList.remove("copy-ok", "copy-fail");
      btn.title = LABELS.copyMessage;
    }, 1600);
  });
  turn.appendChild(btn);
}

function ensureTurn() {
  if (!state.openTurn) {
    state.openTurn = el("li", "turn turn-engine");
    state.openTurn.appendChild(el("span", "turn-role", "Wallet"));
    state.progressLine = null;
    transcriptEl.appendChild(state.openTurn);
  }
  hintEl.hidden = true;
  return state.openTurn;
}

function appendText(text) {
  const turn = ensureTurn();
  const line = el("p", "turn-text");
  line.appendChild(document.createTextNode(text));
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

function appendSystem(text) {
  const turn = el("li", "turn turn-system", text);
  addCopyButton(turn);
  transcriptEl.appendChild(turn);
  hintEl.hidden = true;
  scrollToEnd();
}

function appendUser(text, queued) {
  const turn = el("li", "turn turn-user" + (queued ? " turn-queued" : ""));
  turn.appendChild(el("span", "turn-role", "You"));
  if (queued) turn.appendChild(el("span", "turn-queued-tag", LABELS.queuedTag));
  const line = el("p", "turn-text");
  line.appendChild(document.createTextNode(text));
  turn.appendChild(line);
  addCopyButton(turn);
  transcriptEl.appendChild(turn);
  hintEl.hidden = true;
  scrollToEnd();
  return turn;
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
}

// One turn_end closed the engine's current turn. Promote the oldest locally
// queued submit (it is the next line the FIFO pump will pick up) and re-sync
// button visibility — flow state only changes during turns. No turn_start
// event exists (kinds are text/progress/turn_end), so turn_end is the
// reconcile point.
function noteTurnEnd() {
  state.openTurn = null;
  state.progressLine = null;
  const next = state.queue.shift();
  if (next) {
    next.classList.remove("turn-queued");
    const tag = next.querySelector(".turn-queued-tag");
    if (tag) tag.remove();
  }
  setBusy(state.queue.length > 0);
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
  const visible = new Set(visibleActions(snap));
  for (const btn of actionsEl.querySelectorAll("button")) {
    // Skip the model/quick buttons — they are driven by model_state below.
    if (btn.classList.contains("model-only") || btn.classList.contains("quick-only")) {
      continue;
    }
    btn.hidden = !visible.has(btn.dataset.action);
  }
  applyScanChip(snap);
  applyPrivacyChip(snap);
  applyWatchKeyGate(snap);
  applyModelPrompt(snap);
  applyBackendKind(snap);
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

// TCK-LAUNCH-001 first-run (TCK-WEB-009 (a): now a SETTINGS-PANE state, the
// standalone card is gone): the zpub entry form shows iff the typed
// snapshot's additive ``needs_watch_key`` is true. While it is up, chat is
// disabled — there is no wallet to talk to yet — and the header balance
// buttons stay hidden. The pane's own submit (POST /watchkey) clears the
// state; we NEVER infer provisioning from local state, only from the
// engine's next snapshot — with the TCK-WEB-008 exception kept: a 200
// ``accepted`` is the ENGINE'S OWN confirmation that the key parsed, gated
// and persisted, so the entry state DISMISSES on the spot (no lingering
// form while the pump is busy with the post-provision banner). A terminal
// dismiss is never re-shown by a stale snapshot.
function applyWatchKeyGate(snap) {
  const typed = !!snap && snap.schema === "state/1";
  let needs = typed && snap.needs_watch_key === true;
  if (state.watchKeyDismissed) needs = false;
  if (typed) state.watchKeyPresent = !needs;
  const wasNeeded = state.watchKeyNeeded;
  state.watchKeyNeeded = needs;
  inputEl.disabled = needs;
  sendBtn.disabled = needs;
  // TCK-WEB-009 (h): the header quick buttons appear once a wallet is
  // provisioned and never before (typed truth only — unknown = hidden).
  quickbarEl.hidden = state.watchKeyPresent !== true;
  if (needs !== wasNeeded) renderSettings(); // flip the pane's zpub row
  if (needs && !wasNeeded && settingsPanelEl.hidden === false) focusWatchInput();
  // TCK-WEB-008 fix 3: an unset wallet opens the settings panel on its own
  // and the panel closes itself once the key lands. A configured launch
  // never opens; a state/0 (unknown) never fires either direction.
  if (needs && !state.settingsAutoShown) {
    state.settingsAutoShown = true;
    openSettings();
  } else if (typed && !needs && state.settingsAutoShown) {
    state.settingsAutoShown = false;
    closeSettings(inputEl); // same episode end via snapshot: focus chat
  }
}

// The terminal success path of a watch-key submit (TCK-WEB-008 fix 1,
// TCK-WEB-009 (b)): collapse the pane's zpub row to the read-only truncated
// display. TCK-WEB-013 (4): during a first-run auto-open episode the pane
// now STAYS OPEN under the backend beat instead of auto-closing — the
// WEB-012 dismiss→chat-focus handoff moved to closeSettings (it fires when
// the user actually closes the pane, which is the explicit "not now").
function dismissWatchKeyForm(key) {
  state.sessionWatchKey = key; // memory only: the display fallback pre-/settings-read
  state.watchKeyDismissed = true;
  state.watchKeyPresent = true;
  state.watchKeyNeeded = false;
  state.watchKeyReplaceOpen = false; // applied → the row returns collapsed (§1)
  inputEl.disabled = false;
  sendBtn.disabled = false;
  quickbarEl.hidden = false;
  if (state.settingsAutoShown) {
    state.settingsAutoShown = false; // the auto-open episode hands off to the beat
    state.firstRunBeat = true;
    renderSettings();
    // The chain row (the beat's host) is engine truth: read it fresh, then
    // scroll/focus. A failed read shows the pane's honest retry line.
    loadSettings().then(revealFirstRunBeat);
    return;
  }
  renderSettings();
}

// (4): reveal the beat — put the cursor where the user acts (the empty,
// directly-typeable chain field; focus() scrolls it into view), or on the
// pane heading when the settings read could not land a chain row yet.
function revealFirstRunBeat() {
  if (!state.firstRunBeat || settingsPanelEl.hidden) return;
  const field = settingsListEl.querySelector(
    ".setting-chain .setting-line > input.setting-input",
  );
  const target = field || settingsHeadingEl;
  if (target.isConnected) target.focus();
}

// The scan chip reflects ONLY the typed snapshot's scan_state (additive under
// state/1). Unknown/absent values (state/0 fallback, future states) clear it —
// never a guess, never a hard-fail.
function applyScanChip(snap) {
  scanChipEl.hidden = true;
  if (!snap || snap.schema !== "state/1") return;
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
  own_node_remote: LABELS.privacyOwnRemote,
  awaiting_backend: LABELS.privacyAwaiting,
};

// TCK-WEB-013 (2): the chain row's trust badge — the SAME closed enum the
// header chip rides (privacy_mode from /state; never derived from the URL
// string client-side). Unknown/absent enum → no badge (existing discipline).
const TRUST_BADGE_WORDS = {
  own_node_local: LABELS.trustLocal,
  own_node_remote: LABELS.trustRemote,
  public: LABELS.trustPublic,
  awaiting_backend: LABELS.trustAwaiting,
};

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
  }
  const mode = state.privacyMode;
  paintTrustBadges(); // TCK-WEB-013 (2): the pane badge rides the same truth
  if (!mode) {
    privacyChipEl.hidden = true;
    privacyChipEl.removeAttribute("data-privacy");
    privacySublineEl.textContent = "";
    return;
  }
  privacyChipEl.dataset.privacy = mode;
  privacySublineEl.textContent = PRIVACY_SUBLINE[mode];
  privacyChipEl.hidden = false;
}

// TCK-WEB-009 (e): the badge strip follows the additive backend_kind NAME
// from /state (and from every /settings reply — both carry it). An absent
// or unknown value keeps the last known kind (additive-field rule: the
// engine may legitimately answer state/0 while busy; that is not "no
// backend").
function applyBackendKind(snap) {
  if (!snap || snap.schema !== "state/1") return;
  if (typeof snap.backend_kind === "string" && BACKEND_KINDS.has(snap.backend_kind)) {
    if (snap.backend_kind !== state.backendKind) {
      state.backendKind = snap.backend_kind;
      paintBackendBadges();
    }
  }
}

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
      setStatus("connecting", "Connecting…");
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
      setStatus("live", "Connected");
      if (state.everConnected) refreshState(); // reconnect: buttons may have moved
      state.everConnected = true;
      await consumeStream(response.body);
    } catch {
      // transport failure: treat like a closed stream and retry
    }
    if (state.stopped) return;
    setStatus("reconnecting", `Reconnecting to ${location.origin} …`);
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
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ [field]: value }),
    });
    if (!response.ok) throw new Error(String(response.status));
  } catch {
    echo.remove();
    const at = state.queue.indexOf(echo);
    if (at !== -1) state.queue.splice(at, 1);
    const pe = state.pendingEchos.indexOf(value);
    if (pe !== -1) state.pendingEchos.splice(pe, 1); // the engine never saw the line
    setBusy(state.queue.length > 0);
    appendSystem(LABELS.unreachable);
  }
}

formEl.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = inputEl.value.trim();
  if (!text || state.stopped) return;
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
// the panel is already local. TCK-WEB-009 (h): the header balance buttons
// ride the same /action channel with /balance (the card it renders carries
// the USD line — there is no separate USD utterance and none is invented).
actionsEl.addEventListener("click", (event) => {
  const btn = event.target.closest("button");
  if (!btn || state.stopped) return;
  if (btn.dataset.action === "qa-settings") {
    openSettings();
    return;
  }
  if (btn.dataset.utterance) {
    submit("/action", "utterance", btn.dataset.utterance);
  }
});

quickbarEl.addEventListener("click", (event) => {
  const btn = event.target.closest("button");
  if (!btn || state.stopped || !btn.dataset.utterance) return;
  submit("/action", "utterance", btn.dataset.utterance);
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

// A generic editable row (gap_limit and any future allowlisted key the
// client has no special row for): text/number input + Apply → POST /settings.
function settingRow(entry, index) {
  const li = el("li", "setting");
  const keyId = "setting-input-" + index;

  const label = el("label", "setting-key", entry.key);
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

// The watch-key entry input (shared by the ENTRY form and the §1 replace-mode
// form — the same safe term in the aria-label, one builder, no fork).
function watchKeyInput() {
  const input = document.createElement("input");
  input.type = "text";
  input.spellcheck = false;
  input.autocomplete = "off";
  input.maxLength = 200;
  input.placeholder = "zpub…";
  input.setAttribute("aria-label", LABELS.watchKeyInputLabel);
  return input;
}

// The settings pane's zpub section (TCK-WEB-009 a/b; copy pass 2 §1). States,
// all rendered from typed /state truth plus the server's entry:
//  * ENTRY (wallet needs a key): the moved card, compacted — lede, warning,
//    one input + Connect. Submit is the ONE POST /watchkey channel (the same
//    path the replaced card used, replace/confirm rung kept); ALL key
//    validation is the engine's parse+gate, refusals relayed value-free.
//  * SET: a collapsed display (the engine's truncated descriptor,
//    "wpkh([e7f511…" style) + an Edit button. No Show/Copy/Replace
//    affordances (user direction): the full value is never needed here.
//    Edit (same verb as the chain-base row) flips the row to the
//    replace-mode form — no request; submit rides the same POST /watchkey,
//    and the engine's 409 raises the existing confirm/apply rungs.
function watchKeyRow(serverEntry) {
  const li = el("li", "setting setting-watchkey");
  li.appendChild(el("p", "setting-key", "watch_key"));
  const status = el("p", "setting-status");
  status.setAttribute("role", "status");
  watchForm = null;

  if (state.watchKeyPresent === null && !serverEntry) {
    li.appendChild(el("p", "watchkey-value", LABELS.watchKeyRowUnknown));
    li.appendChild(status);
    return li;
  }
  const needsEntry =
    state.watchKeyNeeded ||
    (serverEntry ? serverEntry.configured !== true : state.watchKeyPresent !== true);
  if (needsEntry) {
    state.watchKeyReplaceOpen = false; // the ENTRY form owns the row
    li.appendChild(el("p", "setting-hint", LABELS.watchkeyLede));
    li.appendChild(el("p", "setting-flag", LABELS.watchkeyWarning));
    const box = el("div", "watchkey-replace"); // confirm / apply rungs of a replace
    const line = el("div", "watchkey-line");
    const input = watchKeyInput();
    const btn = el("button", "btn btn-primary btn-small", LABELS.watchKeyConnect);
    btn.type = "button";
    line.append(input, btn);
    li.append(line, box, status);
    btn.addEventListener("click", () => submitWatchKey(input, btn, status, box));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        submitWatchKey(input, btn, status, box);
      }
    });
    watchForm = { input, submit: btn };
    if (state.settingsAutoShown) focusWatchInput(); // first-run episode: once, on reveal
    return li;
  }

  if (state.watchKeyReplaceOpen) {
    // §1 Edit rung of the replace cycle: flip to the form, replace-mode
    // copy, no request. The submit is the SAME submitWatchKey path — the
    // engine answers ``already`` (409) and raises the existing confirm rung
    // (below it); a silent overwrite is engine-impossible. Cancel collapses
    // back to the display; nothing sent. Always available.
    li.appendChild(el("p", "setting-hint", LABELS.watchkeyReplaceLede));
    li.appendChild(el("p", "setting-flag", LABELS.watchkeyWarning));
    const box = el("div", "watchkey-replace"); // confirm / apply rungs
    const line = el("div", "watchkey-line");
    const input = watchKeyInput();
    const submitBtn = el("button", "btn btn-primary btn-small", LABELS.watchKeyReplaceSubmit);
    submitBtn.type = "button";
    const no = el("button", "btn btn-secondary btn-small", LABELS.watchKeyReplaceCancel);
    no.type = "button";
    no.addEventListener("click", () => {
      state.watchKeyReplaceOpen = false;
      renderSettings();
    });
    line.append(input, submitBtn, no);
    li.append(line, box, status);
    submitBtn.addEventListener("click", () => submitWatchKey(input, submitBtn, status, box));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        submitWatchKey(input, submitBtn, status, box);
      }
    });
    watchForm = { input, submit: submitBtn };
    return li;
  }

  // SET — collapsed display + the §1 Edit affordance.
  const display =
    serverEntry && typeof serverEntry.value === "string"
      ? serverEntry.value
      : state.sessionWatchKey
        ? truncateKey(state.sessionWatchKey)
        : LABELS.watchKeyRowConnected;
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
  if (serverEntry && serverEntry.env_override === true) {
    li.appendChild(el("p", "setting-flag", LABELS.watchKeyEnvOverride));
  }
  li.appendChild(status);
  return li;
}

// The chain-base row (TCK-WEB-009 d/e/f, reworked by TCK-WEB-013): the
// effective backend is ALWAYS shown ("Now using: <url>" from the additive
// /settings field, omitted when absent — never fabricated) with a trust
// badge riding the /state privacy_mode enum ONLY. A stored value renders
// read-only + Edit→Apply (the existing POST /settings write; the engine
// probes before saving and hot-swaps server-side — ADR-0018 amendment); an
// EMPTY field is directly typeable (Apply straight away), and the env rung
// suppresses the field's whole write path for one honest note. Edit/typing
// gain Cancel + Escape, which rebuild the row from engine truth with no
// request. While a write (or the engine's probe inside it) is in flight the
// button is disabled: no double-submit. Under the field: the three dimmed
// backend badges and the Resync-now action.
function chainBaseRow(entry) {
  inputSeq += 1;
  const li = el("li", "setting setting-chain");
  const keyId = "setting-input-" + inputSeq;
  const label = el("label", "setting-key", entry.key);
  label.htmlFor = keyId;
  li.appendChild(label);

  // (5): an env-rung entry is not this field's to write — no Edit rung.
  const envRung = entry.env_override === true;

  // TCK-WEB-013 (1/2): the effective backend is ALWAYS shown when the
  // server carries it (this is how the public default becomes visible
  // under an empty stored field), with the trust badge riding the
  // privacy_mode enum beside it. Field absent (bare pump) → line omitted.
  if (state.effectiveChainUrl) {
    const nowLine = el("div", "chain-now-line");
    nowLine.appendChild(
      el("p", "chain-now", LABELS.settingsNowUsing + " " + state.effectiveChainUrl)
    );
    const badge = trustBadge();
    if (badge) nowLine.appendChild(badge);
    li.appendChild(nowLine);
  }

  // (4): the one-time first-run beat, above the field the "add your own
  // node below" sentence points at.
  if (state.firstRunBeat) {
    li.appendChild(el("p", "setting-flag chain-beat", LABELS.firstRunBeat));
  }

  const line = el("div", "setting-line");
  const input = document.createElement("input");
  input.className = "setting-input";
  input.id = keyId;
  input.type = "text";
  input.spellcheck = false;
  input.autocomplete = "off";
  input.value = typeof entry.value === "string" ? entry.value : "";
  // (3): an EMPTY stored field is directly typeable — Edit earns its keep
  // only when a value exists (and never on the env rung, where a stored
  // write is shadowed anyway).
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
  // (3): Cancel for the edit mode — restores read-only from ENGINE truth
  // (renderSettings rebuilds from the entry, the replace-cancel shape),
  // no request. Visible exactly while the field is editable.
  const cancel = el("button", "btn btn-secondary btn-small setting-cancel", LABELS.settingsCancel);
  cancel.type = "button";
  cancel.hidden = !directlyTypeable;
  cancel.addEventListener("click", () => renderSettings());
  if (!envRung) line.append(input, btn, cancel);
  else line.appendChild(input);
  li.appendChild(line);

  // copy pass 2 #44: the legend is the visible text alternative to dim/lit,
  // and each badge gets a dynamic title/aria in paintBackendBadges (the
  // badges stay a plain group — a container role="img" would hide the
  // per-badge names from screen readers).
  li.appendChild(el("p", "setting-hint", LABELS.badgeLegend));
  const badges = el("div", "backend-badges");
  for (const [family, labelText] of [
    ["mempool", LABELS.badgeMempool],
    ["electrum", LABELS.badgeElectrum],
    ["bitcoind", LABELS.badgeBitcoind],
  ]) {
    const badge = el("span", "backend-badge", labelText);
    badge.dataset.badge = family;
    badge.setAttribute("role", "img"); // makes the aria-label authoritative
    badges.appendChild(badge);
  }
  li.appendChild(badges);

  const resyncLine = el("div", "setting-line setting-resync-line");
  const resyncBtn = el("button", "btn btn-ghost btn-small", LABELS.resyncNow);
  resyncBtn.type = "button";
  resyncBtn.addEventListener("click", () => requestResync(resyncBtn, status));
  resyncLine.appendChild(resyncBtn);
  li.appendChild(resyncLine);

  // (5): the "Empty = public default" hint lies under an env rung (the
  // effective URL line above carries the truth instead) — suppressed there.
  if (!envRung) {
    li.appendChild(el("p", "setting-hint", LABELS.settingsEmptyIsDefault));
  }
  if (entry.requires_restart === true) {
    li.appendChild(el("p", "setting-flag", LABELS.settingsRestart));
  }
  if (envRung) {
    li.appendChild(el("p", "setting-flag", LABELS.chainEnvOverride));
  }

  // TCK-ONB-004 M3: the credentials block. The engine's secret entries
  // report only SET/UNSET (never the value), so the fields start empty on
  // every render; the checkbox mirrors the stored none-flag. Visibility:
  // while EDITING an http:// (ambiguous — may be Core RPC) or bitcoind://
  // address only; https answers as Esplora (creds inert) and ssl://
  // Electrum has no standard auth — the fields never appear for those.
  const credFlags = backendCredFlags();
  if (credFlags.none) {
    li.appendChild(el("p", "setting-flag creds-note", LABELS.credsNoneSaved));
  } else if (credFlags.user || credFlags.pass) {
    li.appendChild(el("p", "setting-flag creds-note", LABELS.credsSavedNote));
  }
  if (credFlags.none || credFlags.user || credFlags.pass) {
    const clearLine = el("div", "setting-line setting-creds-clear-line");
    const clearBtn = el("button", "btn btn-secondary btn-small", LABELS.credsClear);
    clearBtn.type = "button";
    clearBtn.addEventListener("click", () => {
      const note = li.querySelector(".setting-status");
      clearBackendCreds(clearBtn, note, li);
    });
    clearLine.appendChild(clearBtn);
    li.appendChild(clearLine);
  }
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

  const status = el("p", "setting-status");
  status.setAttribute("role", "status");
  li.appendChild(status);
  return li;
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
    (value.startsWith("http://") || value.startsWith("bitcoind://"))
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
            ? LABELS.settingsRejectedPrefix + " " + data.error
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

// (e): dimmed by default (low-opacity token styling), highlighted only for
// the family the current backend_kind belongs to. Copy pass 2 #44: the
// dim/lit state is styling, so this sole painter also recomposes each
// badge's title + aria-label (lit → in-use; dim → idle). The accessible
// name keeps the family word + the sentence.
function paintBackendBadges() {
  for (const badge of settingsListEl.querySelectorAll(".backend-badge")) {
    const family = BADGE_FAMILIES[badge.dataset.badge];
    const active = !!(family && state.backendKind && family.has(state.backendKind));
    if (active) badge.dataset.active = "true";
    else badge.removeAttribute("data-active");
    const words = active ? LABELS.badgeInUse : LABELS.badgeIdle;
    badge.title = words;
    badge.setAttribute("aria-label", badge.textContent + " — " + words);
  }
}

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
  paintBackendBadges();
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
    if (typeof data.backend_kind === "string" && BACKEND_KINDS.has(data.backend_kind)) {
      state.backendKind = data.backend_kind;
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
  status.textContent = LABELS.settingsSaving;
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
      // Copy pass 2 #52: an EMPTY chain-base write that lands hot-swaps to
      // the public server — the applied note must name the leak as it lands
      // (ADR-0023 amendment 2 consent duty). Not when the engine says the
      // value changed nothing ("unchanged") or the env override outranks it
      // ("skipped"): there the switch is not happening now.
      const switched =
        data.resync === undefined ||
        data.resync === "started" ||
        data.resync === "busy" ||
        data.resync === "deferred";
      const emptyApplied = isChain && submitted === "" && switched;
      status.dataset.kind = emptyApplied ? "warn" : "ok";
      const note = emptyApplied
        ? LABELS.settingsEmptyApplied
        : typeof data.resync === "string" && RESYNC_NOTES.has(data.resync)
          ? RESYNC_NOTES.get(data.resync)
          : LABELS.settingsApplied;
      status.textContent = note;
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
        // (4): an Apply answers the first-run beat's ask — it retires here
        // too (the pane-close path clears it in closeSettings).
        state.firstRunBeat = false;
        const beat = row.querySelector(".chain-beat");
        if (beat) beat.remove();
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
              row.insertBefore(
                el("p", "setting-flag creds-note", LABELS.credsSavedNote),
                creds,
              );
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
      if (typeof data.backend_kind === "string" && BACKEND_KINDS.has(data.backend_kind)) {
        state.backendKind = data.backend_kind; // a swap may have moved the kind
        paintBackendBadges();
      }
      if (data.resync === "started" || data.resync === "deferred") {
        refreshState(); // the scan chip follows, from engine truth
      }
    } else if (response.status === 400 && data && data.status === "rejected") {
      status.dataset.kind = "error";
      status.textContent =
        typeof data.error === "string"
          ? LABELS.settingsRejectedPrefix + " " + data.error
          : LABELS.settingsRejected;
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

// TCK-WEB-012 (c/d): closing returns focus to a chosen element — by default
// the header Settings toggle (so the keyboard never drops to <body>), or
// the chat compose input for the first-run auto-close after Connect (the
// promise dismissWatchKeyForm's comment makes: the user is done with the
// pane and ready to type).
function closeSettings(returnFocusTo) {
  if (!settingsPanelEl.hidden) {
    settingsPanelEl.hidden = true;
    settingsToggleEl.setAttribute("aria-expanded", "false");
    if (state.firstRunBeat) {
      // TCK-WEB-013 (4): this close (X / Escape / header toggle) IS the
      // explicit "not now" — the beat retires one-time (never re-shown,
      // no re-open loop; the engine's awaiting_backend gate defers the
      // first-run scan on its own) and the WEB-012 chat handoff fires here.
      state.firstRunBeat = false;
      if (!returnFocusTo) returnFocusTo = inputEl;
    }
    const target = returnFocusTo || settingsToggleEl;
    if (!target.disabled) target.focus();
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
// one Escape closes.
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || settingsPanelEl.hidden) return;
  if (state.watchKeyReplaceOpen) {
    event.preventDefault();
    state.watchKeyReplaceOpen = false;
    renderSettings();
    return;
  }
  // The URL field only — the login block's fields are never readonly and
  // live hidden when not editing, so a class-scoped match is required.
  // TCK-WEB-013 (3): cancel the row only when an edit is actually OPEN —
  // a value present (Edit rung) or a touched directly-typeable field; an
  // untouched empty field is not an open edit, so Escape closes the pane.
  const editing = settingsPanelEl.querySelector(
    ".setting-chain .setting-line > input.setting-input:not(.creds-user):not(.creds-pass):not([readonly])",
  );
  if (editing && (editing.value.trim() !== "" || editing.dataset.dirty === "1")) {
    event.preventDefault();
    renderSettings(); // row back to read-only + Edit; nothing was sent
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
    // quote; we add nothing, and nothing here can echo the submitted key.
    status.dataset.kind = "error";
    status.textContent = LABELS.watchkeyRejectedPrefix + " " + data.error;
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
              ? LABELS.watchkeyRejectedPrefix + " " + data.error
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

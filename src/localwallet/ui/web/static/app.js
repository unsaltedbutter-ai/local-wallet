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
const settingsToggleEl = document.getElementById("settings-toggle");
const settingsPanelEl = document.getElementById("settings-panel");
const settingsStatusEl = document.getElementById("settings-status");
const settingsListEl = document.getElementById("settings-list");

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
  watchKeyRowUnknown: "Status unknown — reconnecting…",
  watchKeyRowConnected: "Connected.",
  watchKeyEnvOverride:
    "The key was set via environment variable — a replacement takes " +
    "effect once that override is removed.",
  watchKeyReplaceConfirm:
    "The cached data belongs to the current wallet; replacing discards " +
    "any pending transaction and re-scans for the new wallet (the previous " +
    "wallet's cache stays in the store, unused). Replace the watch key?",
  watchKeyReplaceYes: "Replace",
  watchKeyReplaceCancel: "Cancel",
  // backend badges (TCK-WEB-009 e) — the words are the closed enum family
  // names the engine's backend_kind maps onto (bitcoind is reserved).
  badgeMempool: "mempool",
  badgeElectrum: "electrum",
  badgeBitcoind: "bitcoind",
  // Resync now (TCK-WEB-009 f) — POST /resync's closed statuses, value-free.
  resyncNow: "Resync now",
  resyncSaving: "Starting…",
  resyncStarted: "Re-scan started — the scan chip above shows progress.",
  resyncBusy: "Already scanning — try again once the current scan finishes.",
  resyncUnavailable: "Re-scan is not available right now.",
  // applied-write notes from the response's resync field (TCK-WEB-009 g):
  // gap-limit and chain-base writes report whether a resync followed.
  resyncNoteStarted: "Saved — re-scanning with the new value; the scan chip above follows.",
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
// "mempool" family; ssl:// is electrum; bitcoind is reserved (never emitted
// today — its badge stays dimmed by construction). "none"/unknown: all dim.
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
  stateSeq: 0,
  watchKeyDismissed: false,
  watchKeyPresent: null, // null = unknown | true | false (typed state/1 only)
  watchKeyNeeded: false, // the pane renders the zpub ENTRY form iff true
  sessionWatchKey: "",
  settingsAutoShown: false, // first-run auto-open episode armed/active
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
  transcriptEl.appendChild(el("li", "turn turn-system", text));
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
  transcriptEl.appendChild(turn);
  hintEl.hidden = true;
  scrollToEnd();
  return turn;
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
    closeSettings();
  }
}

// The terminal success path of a watch-key submit (TCK-WEB-008 fix 1,
// TCK-WEB-009 (b)): collapse the pane's zpub row to the read-only truncated
// display, hand focus back to chat, keep the transcript + status.
function dismissWatchKeyForm(key) {
  state.sessionWatchKey = key; // memory only: the display fallback pre-/settings-read
  state.watchKeyDismissed = true;
  state.watchKeyPresent = true;
  state.watchKeyNeeded = false;
  inputEl.disabled = false;
  sendBtn.disabled = false;
  quickbarEl.hidden = false;
  renderSettings();
  if (state.settingsAutoShown) {
    state.settingsAutoShown = false; // the auto-open episode is over
    closeSettings();
  }
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
  else if (kind === "turn_end") noteTurnEnd();
  else if (kind === "resync") {
    // too_far_behind: the cursor predates the server ring, so part of the
    // transcript is unrecoverable — say so (never silently gap-fill), then
    // re-sync state. The retained backlog follows this frame on the same
    // stream; the duplicate guard above drops any of it we already saw.
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
        setStatus(
          "unauthorized",
          "This page's session no longer matches the wallet — reload this page.",
        );
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

// The settings pane's zpub section (TCK-WEB-009 a/b). Two states, both
// rendered from typed /state truth plus the server's entry:
//  * ENTRY (wallet needs a key): the moved card, compacted — lede, warning,
//    one input + Connect. Submit is the ONE POST /watchkey channel (the same
//    path the replaced card used, replace/confirm rung kept); ALL key
//    validation is the engine's parse+gate, refusals relayed value-free.
//  * SET: a collapsed read-only display — the engine's truncated descriptor
//    ("wpkh([e7f511…" style). No Show/Copy/Replace affordances (user
//    direction): the full value is never needed in this pane anymore.
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
    li.appendChild(el("p", "setting-hint", LABELS.watchkeyLede));
    li.appendChild(el("p", "setting-flag", LABELS.watchkeyWarning));
    const box = el("div", "watchkey-replace"); // confirm / apply rungs of a replace
    const line = el("div", "watchkey-line");
    const input = document.createElement("input");
    input.type = "text";
    input.spellcheck = false;
    input.autocomplete = "off";
    input.maxLength = 200;
    input.placeholder = "zpub…";
    input.setAttribute("aria-label", LABELS.watchKeyInputLabel);
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

  // SET — collapsed display only.
  const display =
    serverEntry && typeof serverEntry.value === "string"
      ? serverEntry.value
      : state.sessionWatchKey
        ? truncateKey(state.sessionWatchKey)
        : LABELS.watchKeyRowConnected;
  li.appendChild(el("p", "watchkey-value", display));
  if (serverEntry && serverEntry.env_override === true) {
    li.appendChild(el("p", "setting-flag", LABELS.watchKeyEnvOverride));
  }
  li.appendChild(status);
  return li;
}

// The chain-base row (TCK-WEB-009 d/e/f): a read-only field + Edit; Edit
// makes it editable and the button becomes Apply; Apply is the existing
// POST /settings write (the engine probes before saving and hot-swaps
// server-side — ADR-0018 amendment), and success returns the row to
// read-only. A refused probe shows the engine's honest value-free line and
// keeps the field editable for a correction. While a write (or the engine's
// probe inside it) is in flight the button is disabled: no double-submit.
// Under the field: the three dimmed backend badges and the Resync-now
// action.
function chainBaseRow(entry) {
  inputSeq += 1;
  const li = el("li", "setting setting-chain");
  const keyId = "setting-input-" + inputSeq;
  const label = el("label", "setting-key", entry.key);
  label.htmlFor = keyId;
  li.appendChild(label);

  const line = el("div", "setting-line");
  const input = document.createElement("input");
  input.className = "setting-input";
  input.id = keyId;
  input.type = "text";
  input.spellcheck = false;
  input.autocomplete = "off";
  input.readOnly = true; // (d): starts read-only
  input.value = typeof entry.value === "string" ? entry.value : "";
  input.placeholder = LABELS.settingsEmptyPlaceholder;
  const btn = el("button", "btn btn-secondary btn-small setting-apply", LABELS.settingsEdit);
  btn.type = "button";
  btn.dataset.settingKey = entry.key;
  line.append(input, btn);
  li.appendChild(line);

  const badges = el("div", "backend-badges");
  badges.setAttribute("role", "img");
  badges.setAttribute("aria-label", LABELS.badgeMempool + " " + LABELS.badgeElectrum + " " + LABELS.badgeBitcoind);
  for (const [family, labelText] of [
    ["mempool", LABELS.badgeMempool],
    ["electrum", LABELS.badgeElectrum],
    ["bitcoind", LABELS.badgeBitcoind],
  ]) {
    const badge = el("span", "backend-badge", labelText);
    badge.dataset.badge = family;
    badges.appendChild(badge);
  }
  li.appendChild(badges);

  const resyncLine = el("div", "setting-line setting-resync-line");
  const resyncBtn = el("button", "btn btn-ghost btn-small", LABELS.resyncNow);
  resyncBtn.type = "button";
  resyncBtn.addEventListener("click", () => requestResync(resyncBtn, status));
  resyncLine.appendChild(resyncBtn);
  li.appendChild(resyncLine);

  li.appendChild(el("p", "setting-hint", LABELS.settingsEmptyIsDefault));
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

// (e): dimmed by default (low-opacity token styling), highlighted only for
// the family the current backend_kind belongs to. bitcoind stays dimmed —
// reserved, the engine never emits that kind today.
function paintBackendBadges() {
  for (const badge of settingsListEl.querySelectorAll(".backend-badge")) {
    const family = BADGE_FAMILIES[badge.dataset.badge];
    if (family && state.backendKind && family.has(state.backendKind)) {
      badge.dataset.active = "true";
    } else {
      badge.removeAttribute("data-active");
    }
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
      if (entry.key !== "watch_key" && entry.key !== "chain_base_url" && entry.key !== "gap_limit") {
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
  settingsStatusEl.textContent = LABELS.settingsLoading;
  renderSettings();
  try {
    const response = await fetch("/settings", { headers: authHeaders(), cache: "no-store" });
    const data = response.ok ? await response.json().catch(() => null) : null;
    if (!data || !Array.isArray(data.settings)) {
      settingsStatusEl.textContent = LABELS.settingsUnavailable;
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
    if (typeof data.backend_kind === "string" && BACKEND_KINDS.has(data.backend_kind)) {
      state.backendKind = data.backend_kind;
    }
    renderSettings();
  } catch {
    settingsStatusEl.textContent = LABELS.settingsUnavailable;
  }
}

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
    // Edit rung: no request, no state change — just open the field.
    input.readOnly = false;
    input.focus();
    btn.textContent = LABELS.settingsApply;
    status.dataset.kind = "";
    status.textContent = "";
    return;
  }
  const problem = localSettingProblem(input);
  status.dataset.kind = "error";
  if (problem) {
    status.textContent = problem;
    return;
  }
  btn.disabled = true; // in-flight (engine probe included): no double-submit
  status.textContent = LABELS.settingsSaving;
  try {
    const response = await fetch("/settings", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ key: btn.dataset.settingKey, value: input.value.trim() }),
    });
    const data = await response.json().catch(() => null);
    if (response.status === 200 && data && data.status === "applied") {
      status.dataset.kind = "ok";
      const note =
        typeof data.resync === "string" && RESYNC_NOTES.has(data.resync)
          ? RESYNC_NOTES.get(data.resync)
          : LABELS.settingsApplied;
      status.textContent = note;
      // confirm from the server's freshly re-read entry, never our own echo
      const fresh = Array.isArray(data.settings) ? data.settings[0] : null;
      if (fresh && Object.prototype.hasOwnProperty.call(fresh, "value")) {
        input.value = typeof fresh.value === "string" ? fresh.value : "";
      }
      if (isChain) {
        input.readOnly = true; // (d): applied → back to the read-only state
        btn.textContent = LABELS.settingsEdit;
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

function openSettings() {
  if (settingsPanelEl.hidden) {
    settingsPanelEl.hidden = false;
    settingsToggleEl.setAttribute("aria-expanded", "true");
    loadSettings(); // fetch on demand, fresh every time the panel opens
    if (state.watchKeyNeeded) focusWatchInput();
  }
}

function closeSettings() {
  if (!settingsPanelEl.hidden) {
    settingsPanelEl.hidden = true;
    settingsToggleEl.setAttribute("aria-expanded", "false");
  }
}

settingsToggleEl.addEventListener("click", () => {
  if (settingsPanelEl.hidden) openSettings();
  else closeSettings();
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

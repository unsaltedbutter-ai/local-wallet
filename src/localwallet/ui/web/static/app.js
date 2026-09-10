// Local Wallet web client (TCK-WEB-002 + TCK-WEB-004). Vanilla ES module —
// no framework, no build step. XSS contract: every dynamic value (all model
// output) is rendered via textContent ONLY. HTML-string sinks are banned
// here (ADR-0024 §7). Buttons inject canonical utterances through POST
// /action into the FULL engine turn pipeline (ADR-0024 §8) — the client can
// never skip a gate because it never touches a handler or the flow.

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
const scanChipEl = document.getElementById("scan-chip");
const settingsToggleEl = document.getElementById("settings-toggle");
const settingsPanelEl = document.getElementById("settings-panel");
const settingsStatusEl = document.getElementById("settings-status");
const settingsListEl = document.getElementById("settings-list");
const watchkeyPanelEl = document.getElementById("watchkey-panel");
const watchkeyInputEl = document.getElementById("watchkey-input");
const watchkeySubmitEl = document.getElementById("watchkey-submit");
const watchkeyStatusEl = document.getElementById("watchkey-status");

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
  // TCK-LAUNCH-002 model-download card + inline progress.
  modelDownloading: "Downloading the model",
  // watch key in settings (TCK-WEB-008 follow-up (a) / TCK-LAUNCH-002): the
  // FULL public key arrives ONLY on the explicit single-key read (Show/Copy).
  watchKeyEnvOverride:
    "The key was set via environment variable — a replacement takes " +
    "effect once that override is removed.",
  watchKeyReplaced: "Watch key replaced.",
  watchKeyReplacedNote:
    "The new wallet loads with its own empty cache; the previous wallet's " +
    "data stays in the store and no longer applies.",
  // first-run watch-key form (TCK-LAUNCH-001) — the engine owns all key
  // validation; we only relay its value-free status lines verbatim.
  watchkeySaving: "Connecting…",
  watchkeyConnected: "Connected.",
  watchkeyRejectedPrefix: "Not accepted:",
  watchkeyRejected: "The key was not accepted — check it and try again.",
  watchkeyBusy: "The wallet is busy — try again.",
  watchkeyFailed: "Could not connect — try again.",
  // settings watch-key row (TCK-WEB-008) — placeholder copy for the
  // designer pass (docs/ux-first-run-web.md); strings live ONLY here.
  settingsSectionWatchKey: "Wallet",
  settingsSectionNetwork: "Network & scanning",
  watchKeyRowUnknown: "Status unknown — reconnecting…",
  watchKeyRowAbsent: "No watch key connected yet — enter it in the form above.",
  watchKeyRowConnected: "Connected.",
  watchKeyReveal: "Show",
  watchKeyHide: "Hide",
  watchKeyCopy: "Copy full key",
  watchKeyCopied: "Full key copied.",
  watchKeyCopyFailed: "Copy failed — the full key is shown; select and copy it.",
  watchKeyReplace: "Replace watch key",
  watchKeyReplaceCancel: "Cancel",
  watchKeyReplaceYes: "Replace",
  watchKeyReplaceConfirm:
    "The cached data belongs to the current wallet; replacing discards " +
    "any pending transaction and re-scans for the new wallet (the previous " +
    "wallet's cache stays in the store, unused). Replace the watch key?",
  watchKeyReplaceLabel: "New public account key",
  watchKeyReplaceApply: "Apply new key",
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
  // a form the engine has already outgrown), the terminal dismiss on accept,
  // the derived presence for the settings row, and the key the USER supplied
  // in THIS page session (memory only — never persisted, never logged; a
  // key supplied at launch never reaches the client at all, so it cannot be
  // shown: the row then says so).
  stateSeq: 0,
  watchKeyDismissed: false,
  watchKeyPresent: null, // null = unknown | true | false (typed state/1 only)
  sessionWatchKey: "",
  settingsAutoShown: false, // first-run auto-open episode armed/active
  // TCK-LAUNCH-002: the model card + inline download progress. The card /
  // quick-action buttons are shown ONLY from the typed snapshot's additive
  // model_state NAME (never prose); the progress line is an inline element
  // fed by the int-only model_progress events (percent + bytes, value-free).
  downloadLine: null, // <p> currently receiving the inline progress bar
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
}

// TCK-LAUNCH-002: the Yes/No card buttons and the model-free quick-action
// buttons render ONLY from the typed snapshot's additive model_state NAME
// (state/1; the shipped doctrine: never infer structure from prose, never
// guess on an unknown value). absent/failed = the card; declined/running =
// quick actions; ready / a real model (no model_state field) / an unknown
// name = neither. While the watch-key form still gates the page the model
// card stands down (the form is the single first-run ask).
function applyModelPrompt(snap) {
  const typed = snap && snap.schema === "state/1";
  const modelState = typed && typeof snap.model_state === "string" ? snap.model_state : "";
  const gated = !watchkeyPanelEl.hidden; // no wallet yet — form owns the page
  const showCard = !gated && MODEL_CARD_STATES.has(modelState);
  const showQuick = !gated && MODEL_QUICK_STATES.has(modelState);
  for (const btn of actionsEl.querySelectorAll(".model-only")) {
    btn.hidden = !showCard;
  }
  for (const btn of actionsEl.querySelectorAll(".quick-only")) {
    btn.hidden = !showQuick;
  }
}

// TCK-LAUNCH-001 first-run: the watch-key form is shown ONLY on the typed
// snapshot's additive ``needs_watch_key`` boolean (state/1 stays valid;
// the shipped client keys off known fields and ignores the rest). While it
// is up, chat is disabled — there is no wallet to talk to yet. The panel's
// own submit (POST /watchkey) clears it; we NEVER infer provisioning from
// local state, only from the engine's next snapshot — with one TCK-WEB-008
// exception: a 200 ``accepted`` is the ENGINE'S OWN confirmation that the
// key parsed, gated and persisted, so the form DISMISSES on the spot (the
// user fix: no lingering card while the pump is busy with the post-provision
// banner). A terminal dismiss is never re-shown by a stale snapshot, and the
// ongoing refreshes below still re-sync everything else.
function applyWatchKeyGate(snap) {
  const typed = !!snap && snap.schema === "state/1";
  let needs = typed && snap.needs_watch_key === true;
  if (state.watchKeyDismissed) needs = false;
  if (typed) state.watchKeyPresent = !needs;
  const wasNeeded = !watchkeyPanelEl.hidden;
  watchkeyPanelEl.hidden = !needs;
  const mute = needs;
  inputEl.disabled = mute;
  sendBtn.disabled = mute;
  if (needs && !wasNeeded) watchkeyInputEl.focus(); // once, on reveal — not every refresh
  // TCK-WEB-008 fix 3: an unset wallet opens the settings panel on its own
  // (the row inside explains what is missing) and the panel closes itself
  // once the key lands. A configured launch never opens; a state/0 (unknown)
  // never fires either direction.
  if (needs && !state.settingsAutoShown) {
    state.settingsAutoShown = true;
    openSettings();
  } else if (typed && !needs && state.settingsAutoShown) {
    state.settingsAutoShown = false;
    closeSettings();
  }
}

// The terminal success path of a watch-key submit (TCK-WEB-008 fix 1): hide
// the whole form card, hand focus back to chat, keep the transcript + status.
function dismissWatchKeyForm(key) {
  state.sessionWatchKey = key; // memory only: powers the settings-row reveal/copy
  state.watchKeyDismissed = true;
  state.watchKeyPresent = true;
  watchkeyInputEl.value = ""; // the key lives nowhere else after this line
  watchkeyPanelEl.hidden = true;
  inputEl.disabled = false;
  sendBtn.disabled = false;
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
// the panel is already local.
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

// ------------------------------------------------------------------ settings
// TCK-WEB-005. GET /settings renders an allowlisted snapshot the SERVER owns;
// unknown keys/fields render generically (a text input) and never crash the
// panel. POSTs reuse the same auth header path as every other request. Values
// live only in panel DOM state — never logged, never stored client-side.

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
  const btn = el("button", "btn btn-secondary setting-apply", LABELS.settingsApply);
  btn.type = "button";
  btn.dataset.settingKey = entry.key;
  line.append(input, btn);
  li.appendChild(line);

  if (entry.type === "url") {
    li.appendChild(el("p", "setting-hint", LABELS.settingsEmptyIsDefault));
  }
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

function headingRow(text) {
  return el("li", "setting-heading", text);
}

// Display-only shortening (TCK-WEB-008 fix 2): the COPY and the reveal show
// the FULL key — a display truncation is head…tail, never mid-hash-where-it-
// matters, and never the copied value.
function truncateKey(key) {
  return key.length <= 24 ? key : key.slice(0, 12) + "…" + key.slice(-8);
}

async function copyText(text) {
  // Loopback http://127.0.0.1 IS a secure context (browser rule), so the
  // clipboard API is normally there; any failure falls back to on-screen
  // full reveal — the user fix demands a copyable value, however it lands.
  await navigator.clipboard.writeText(text);
}

// The settings watch-key row (TCK-WEB-008 + TCK-LAUNCH-002 follow-up (a)).
// PRESENCE comes from the typed /state truth (same source as the form
// gate); the DISPLAY value comes from the server's settings list entry
// (display-TRUNCATED — the engine owns the shortening) and falls back to a
// this-session key ONLY when the server entry is not yet loaded. The FULL
// value is fetched on demand via the explicit single-key read
// GET /settings?key=watch_key (a public account key — never a secret, yet
// still never logged; the endpoint is token-gated). The server never sends
// the key on any UNauthenticated surface (ADR-0024, pinned by server tests).
function watchKeyRow(serverEntry) {
  const li = el("li", "setting setting-watchkey");
  li.appendChild(el("p", "setting-key", "watch_key"));
  const value = el("p", "watchkey-value");
  li.appendChild(value);
  const actions = el("div", "watchkey-actions");
  li.appendChild(actions);
  const status = el("p", "setting-status");
  status.setAttribute("role", "status");
  li.appendChild(status);
  const box = el("div", "watchkey-replace"); // replace-confirm / replace-apply stages
  box.hidden = true;
  li.appendChild(box);

  if (state.watchKeyPresent === null && !serverEntry) {
    value.textContent = LABELS.watchKeyRowUnknown;
    return li;
  }
  const configured = serverEntry
    ? serverEntry.configured === true
    : state.watchKeyPresent === true;
  if (!configured) {
    value.textContent = LABELS.watchKeyRowAbsent;
    return li;
  }

  // Display value: the server's truncated entry verbatim, or the value this
  // page supplied (memory only) before the settings list arrived.
  let display =
    serverEntry && typeof serverEntry.value === "string"
      ? serverEntry.value
      : state.sessionWatchKey
        ? truncateKey(state.sessionWatchKey)
        : LABELS.watchKeyRowConnected;
  let revealed = false;
  let full = ""; // the revealed value (memory only; never logged/persisted)
  value.textContent = display;

  const renderValue = () => {
    value.textContent = revealed && full ? full : display;
  };

  async function ensureFull() {
    if (full) return full;
    if (state.sessionWatchKey) {
      full = state.sessionWatchKey; // this page's own submit — reuse it
      return full;
    }
    // Explicit single-key read (the ONLY channel that carries the full key).
    try {
      const response = await fetch("/settings?key=watch_key", {
        headers: authHeaders(),
        cache: "no-store",
      });
      const data = response.ok ? await response.json().catch(() => null) : null;
      const entry = data && Array.isArray(data.settings) ? data.settings[0] : null;
      if (entry && typeof entry.value === "string") full = entry.value;
    } catch {
      /* full stays empty: reveal shows the truncated value, never a guess */
    }
    return full;
  }

  const reveal = el("button", "btn btn-secondary btn-small", LABELS.watchKeyReveal);
  reveal.type = "button";
  reveal.addEventListener("click", async () => {
    if (revealed) {
      revealed = false;
    } else {
      full = await ensureFull();
      revealed = !!full;
    }
    renderValue();
    reveal.textContent = revealed ? LABELS.watchKeyHide : LABELS.watchKeyReveal;
  });
  const copy = el("button", "btn btn-secondary btn-small", LABELS.watchKeyCopy);
  copy.type = "button";
  copy.addEventListener("click", async () => {
    full = await ensureFull();
    if (!full) {
      status.dataset.kind = "error";
      status.textContent = LABELS.watchKeyCopyFailed;
      return;
    }
    try {
      await copyText(full);
      status.dataset.kind = "ok";
      status.textContent = LABELS.watchKeyCopied;
    } catch {
      status.dataset.kind = "error";
      status.textContent = LABELS.watchKeyCopyFailed;
    }
    if (!revealed) {
      revealed = true; // the full value is on screen either way
      renderValue();
      reveal.textContent = LABELS.watchKeyHide;
    }
  });
  actions.append(reveal, copy);

  // EDIT flow (TCK-WEB-008 fix 2 + TCK-LAUNCH-002 replace decision): button
  // → inline confirm (the cached data belongs to the CURRENT wallet; the
  // engine warns it stays behind) → new-key input → POST /watchkey with the
  // explicit replace+confirm opt-in. The engine re-runs the SAME parse+gate
  // path on the ENGINE thread; its closed status is the whole verdict,
  // relayed value-free. A running session can now replace in place.
  const replace = el("button", "btn btn-secondary btn-small", LABELS.watchKeyReplace);
  replace.type = "button";
  replace.addEventListener("click", () => replaceStage("confirm"));
  actions.appendChild(replace);
  if (serverEntry && serverEntry.env_override === true) {
    li.appendChild(el("p", "setting-flag", LABELS.watchKeyEnvOverride));
  }

  function replaceStage(stage) {
    status.textContent = "";
    status.dataset.kind = "";
    box.replaceChildren();
    box.hidden = false;
    if (stage === "confirm") {
      box.appendChild(el("p", "watchkey-warning", LABELS.watchKeyReplaceConfirm));
      const yes = el("button", "btn btn-danger btn-small", LABELS.watchKeyReplaceYes);
      yes.type = "button";
      yes.addEventListener("click", () => replaceStage("apply"));
      const no = el("button", "btn btn-secondary btn-small", LABELS.watchKeyReplaceCancel);
      no.type = "button";
      no.addEventListener("click", () => {
        box.hidden = true;
        box.replaceChildren();
      });
      box.append(yes, no);
      return;
    }
    const line = el("div", "setting-line");
    const input = document.createElement("input");
    input.type = "text";
    input.className = "setting-input";
    input.spellcheck = false;
    input.autocomplete = "off";
    input.maxLength = 200;
    input.setAttribute("aria-label", LABELS.watchKeyReplaceLabel);
    const apply = el("button", "btn btn-primary btn-small", LABELS.watchKeyReplaceApply);
    apply.type = "button";
    apply.addEventListener("click", async () => {
      const key = input.value.trim();
      if (!key || apply.disabled) return;
      apply.disabled = true;
      status.dataset.kind = "";
      status.textContent = LABELS.watchkeySaving;
      const { code, data } = await postWatchKey(key, true);
      apply.disabled = false;
      if (code === 200 && data && (data.status === "replaced" || data.status === "accepted")) {
        // The engine re-wired onto the new key: re-read the panel rows from
        // tool truth (fresh /settings list + /state), never from this echo.
        state.sessionWatchKey = key;
        state.watchKeyPresent = true;
        full = ""; // force a fresh server reveal of the NEW key
        revealed = false;
        status.dataset.kind = "ok";
        status.textContent = LABELS.watchKeyReplaced;
        box.hidden = true;
        box.replaceChildren();
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
    line.append(input, apply);
    box.appendChild(line);
  }
  return li;
}

async function loadSettings() {
  // The watch-key section renders from the typed /state truth alone at
  // first — honest even when GET /settings is unavailable (a first-run
  // engine has no settings store yet). Once the server list lands, the row
  // re-renders with its display-TRUNCATED entry (TCK-LAUNCH-002); the
  // watch_key entry is NEVER rendered as a generic editable row (it is
  // read-only through this surface — changing the key is the gated
  // POST /watchkey replace path). The server-owned rows follow under their
  // own heading (TCK-WEB-008 fix 4: coherent grouping/labels).
  const render = (watchEntry, serverRows) => {
    const rows = [];
    for (const entry of serverRows) {
      rows.push(settingRow(entry, rows.length));
    }
    const list = [headingRow(LABELS.settingsSectionWatchKey), watchKeyRow(watchEntry)];
    if (rows.length > 0) list.push(headingRow(LABELS.settingsSectionNetwork), ...rows);
    settingsListEl.replaceChildren(...list);
    return rows.length;
  };
  settingsStatusEl.textContent = LABELS.settingsLoading;
  render(null, []);
  try {
    const response = await fetch("/settings", { headers: authHeaders(), cache: "no-store" });
    const data = response.ok ? await response.json().catch(() => null) : null;
    if (!data || !Array.isArray(data.settings)) {
      settingsStatusEl.textContent = LABELS.settingsUnavailable;
      return;
    }
    settingsStatusEl.textContent = "";
    let watchEntry = null;
    const serverRows = [];
    for (const entry of data.settings) {
      // an entry without a string key is unrenderable: skip it, keep the rest
      if (!entry || typeof entry.key !== "string") continue;
      if (entry.key === "watch_key") watchEntry = entry;
      else serverRows.push(entry);
    }
    const rendered = render(watchEntry, serverRows);
    if (rendered === 0 && !watchEntry) {
      settingsStatusEl.textContent = LABELS.settingsUnavailable;
    }
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

// Delegated (same pattern as /action): apply clicks POST one {key, value} to
// /settings and render the server's honest status. Rejection bodies are
// value-free; we show the server's own reason line, inventing no detail.
settingsListEl.addEventListener("click", async (event) => {
  const btn = event.target.closest("button[data-setting-key]");
  if (!btn || state.stopped) return;
  const row = btn.closest(".setting");
  const input = row.querySelector("input");
  const status = row.querySelector(".setting-status");
  const problem = localSettingProblem(input);
  status.dataset.kind = "error";
  if (problem) {
    status.textContent = problem;
    return;
  }
  btn.disabled = true;
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
      status.textContent = LABELS.settingsApplied;
      // confirm from the server's freshly re-read entry, never our own echo
      const fresh = Array.isArray(data.settings) ? data.settings[0] : null;
      if (fresh && Object.prototype.hasOwnProperty.call(fresh, "value")) {
        input.value = typeof fresh.value === "string" ? fresh.value : "";
      }
    } else if (response.status === 400 && data && data.status === "rejected") {
      status.dataset.kind = "error";
      status.textContent =
        typeof data.error === "string"
          ? LABELS.settingsRejectedPrefix + " " + data.error
          : LABELS.settingsRejected;
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

// ------------------------------------------------- first-run watch key (001)
// The key string rides ONLY to POST /watchkey (token/Host/Origin-gated, like
// every mutation); ALL validation is the engine's existing parse+gate path.
// We relay the server's value-free status line verbatim — the submitted key
// is never re-rendered, cleared into the transcript, or logged. ONE fetch
// path (TCK-WEB-008): the first-run form and the settings replace flow are
// the same POST /watchkey — no second endpoint, no client-side verdict.
// The replace rung (TCK-LAUNCH-002 / ADR-0024 amendment) adds the explicit
// DOUBLE opt-in the engine demands: replace:true AND confirm:true.
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

async function submitWatchKey() {
  const key = watchkeyInputEl.value.trim();
  if (!key || watchkeySubmitEl.disabled) return;
  watchkeySubmitEl.disabled = true;
  watchkeyStatusEl.dataset.kind = "";
  watchkeyStatusEl.textContent = LABELS.watchkeySaving;
  const { code, data } = await postWatchKey(key);
  if (code === 200 && data && data.status === "accepted") {
    // TCK-WEB-008 fix 1: the engine just confirmed the key — dismiss the
    // form card NOW (no waiting on the next /state round-trip, which can
    // stall behind the post-provision banner), and keep "Connected." where
    // it survives: the transcript + the connection status.
    watchkeyStatusEl.textContent = "";
    dismissWatchKeyForm(key);
    appendSystem(LABELS.watchkeyConnected);
  } else if (
    (code === 400 || code === 409) &&
    data &&
    typeof data.error === "string"
  ) {
    // Rejection reasons are value-free by the engine's contract — safe to
    // quote; we add nothing, and nothing here can echo the submitted key.
    watchkeyStatusEl.dataset.kind = "error";
    watchkeyStatusEl.textContent = LABELS.watchkeyRejectedPrefix + " " + data.error;
  } else if (code === 0) {
    watchkeyStatusEl.dataset.kind = "error";
    watchkeyStatusEl.textContent = LABELS.unreachable;
  } else if (code === 503) {
    watchkeyStatusEl.dataset.kind = "error";
    watchkeyStatusEl.textContent = LABELS.watchkeyBusy;
  } else {
    watchkeyStatusEl.dataset.kind = "error";
    watchkeyStatusEl.textContent = LABELS.watchkeyFailed;
  }
  watchkeySubmitEl.disabled = false;
  // Re-read the engine's truth either way: the snapshot re-syncs button/scan
  // state, and (post-dismiss, TCK-WEB-008) can no longer resurrect the form.
  refreshState();
}

watchkeySubmitEl.addEventListener("click", submitWatchKey);
watchkeyInputEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    submitWatchKey();
  }
});

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

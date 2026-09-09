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
  // first-run watch-key form (TCK-LAUNCH-001) — the engine owns all key
  // validation; we only relay its value-free status lines verbatim.
  watchkeySaving: "Connecting…",
  watchkeyConnected: "Connected.",
  watchkeyRejectedPrefix: "Not accepted:",
  watchkeyRejected: "The key was not accepted — check it and try again.",
  watchkeyBusy: "The wallet is busy — try again.",
  watchkeyFailed: "Could not connect — try again.",
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
};

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
  scrollToEnd();
}

function appendProgress(chars) {
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
    btn.hidden = !visible.has(btn.dataset.action);
  }
  applyScanChip(snap);
  applyWatchKeyGate(snap);
}

// TCK-LAUNCH-001 first-run: the watch-key form is shown ONLY on the typed
// snapshot's additive ``needs_watch_key`` boolean (state/1 stays valid;
// the shipped client keys off known fields and ignores the rest). While it
// is up, chat is disabled — there is no wallet to talk to yet. The panel's
// own submit (POST /watchkey) clears it; we NEVER infer provisioning from
// local state, only from the engine's next snapshot.
function applyWatchKeyGate(snap) {
  const needs = !!snap && snap.schema === "state/1" && snap.needs_watch_key === true;
  const wasNeeded = !watchkeyPanelEl.hidden;
  watchkeyPanelEl.hidden = !needs;
  const mute = needs;
  inputEl.disabled = mute;
  sendBtn.disabled = mute;
  if (needs && !wasNeeded) watchkeyInputEl.focus(); // once, on reveal — not every refresh
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
  try {
    const response = await fetch("/state", { headers: authHeaders(), cache: "no-store" });
    if (!response.ok) return;
    applyState(await response.json());
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
actionsEl.addEventListener("click", (event) => {
  const btn = event.target.closest("button[data-utterance]");
  if (!btn || state.stopped) return;
  submit("/action", "utterance", btn.dataset.utterance);
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

async function loadSettings() {
  settingsStatusEl.textContent = LABELS.settingsLoading;
  settingsListEl.replaceChildren();
  try {
    const response = await fetch("/settings", { headers: authHeaders(), cache: "no-store" });
    const data = response.ok ? await response.json().catch(() => null) : null;
    if (!data || !Array.isArray(data.settings)) {
      settingsStatusEl.textContent = LABELS.settingsUnavailable;
      return;
    }
    settingsStatusEl.textContent = "";
    const rows = [];
    for (const entry of data.settings) {
      // an entry without a string key is unrenderable: skip it, keep the rest
      if (entry && typeof entry.key === "string") rows.push(settingRow(entry, rows.length));
    }
    settingsListEl.replaceChildren(...rows);
    if (rows.length === 0) settingsStatusEl.textContent = LABELS.settingsUnavailable;
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

settingsToggleEl.addEventListener("click", () => {
  const open = settingsPanelEl.hidden;
  settingsPanelEl.hidden = !open;
  settingsToggleEl.setAttribute("aria-expanded", String(open));
  if (open) loadSettings(); // fetch on demand, fresh every time the panel opens
});

// ------------------------------------------------- first-run watch key (001)
// The key string rides ONLY to POST /watchkey (token/Host/Origin-gated, like
// every mutation); ALL validation is the engine's existing parse+gate path.
// We relay the server's value-free status line verbatim — the submitted key
// is never re-rendered, cleared into the transcript, or logged.

async function submitWatchKey() {
  const key = watchkeyInputEl.value.trim();
  if (!key || watchkeySubmitEl.disabled) return;
  watchkeySubmitEl.disabled = true;
  watchkeyStatusEl.dataset.kind = "";
  watchkeyStatusEl.textContent = LABELS.watchkeySaving;
  try {
    const response = await fetch("/watchkey", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ key }),
    });
    const data = await response.json().catch(() => null);
    if (response.status === 200 && data && data.status === "accepted") {
      watchkeyInputEl.value = ""; // the key lives nowhere after this line
      watchkeyStatusEl.dataset.kind = "ok";
      watchkeyStatusEl.textContent = LABELS.watchkeyConnected;
    } else if (
      (response.status === 400 || response.status === 409) &&
      data &&
      typeof data.error === "string"
    ) {
      // Rejection reasons are value-free by the engine's contract — safe to
      // quote; we add nothing, and nothing here can echo the submitted key.
      watchkeyStatusEl.dataset.kind = "error";
      watchkeyStatusEl.textContent = LABELS.watchkeyRejectedPrefix + " " + data.error;
    } else if (response.status === 503) {
      watchkeyStatusEl.dataset.kind = "error";
      watchkeyStatusEl.textContent = LABELS.watchkeyBusy;
    } else {
      watchkeyStatusEl.dataset.kind = "error";
      watchkeyStatusEl.textContent = LABELS.watchkeyFailed;
    }
  } catch {
    watchkeyStatusEl.dataset.kind = "error";
    watchkeyStatusEl.textContent = LABELS.unreachable;
  } finally {
    watchkeySubmitEl.disabled = false;
    // Re-read the engine's truth either way: on success the form disappears
    // when the next snapshot stops saying needs_watch_key (never on our own
    // echo); on failure chat stays exactly as gated.
    refreshState();
  }
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

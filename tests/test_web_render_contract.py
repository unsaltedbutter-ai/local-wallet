"""TCK-WEB-003 XSS render contract (ADR-0024 §7): textContent-only + red-team.

Two enforcement layers, one ticket:

* **Static client audit** — the REAL web-builder client (``app.js`` /
  ``index.html``) is scanned for every HTML-string sink and inline-handler /
  ``javascript:`` surface the contract bans. This is the durable guard: if a
  future client edit reaches for ``innerHTML``, the XSS fixtures' whole premise
  (values are inert text) silently dies, and ONLY a source pin catches it in CI
  with no browser. ``sanitize_tool_output`` is NOT an HTML escaper (the ADR is
  explicit) so the render layer is the real defense — this test IS that defense.
* **Render-fixture inertness** — every ``evals/redteam/render/*.json`` hostile
  payload is run through the deterministic ``textContent`` model the eval runner
  ships (``run_evals.render_text_content``) and asserted to survive verbatim as
  one element-free text node — markup never becomes a node / handler / URL.
* **Live CSP + island nonce** — a real served index carries a CSP that allows NO
  inline script except the island (per-response nonce) and NO eval, proving the
  shrink-the-blast-radius layer end to end (the client source audit alone
  cannot see the header).
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_EVALS_PATH = REPO_ROOT / "evals" / "run_evals.py"
_RENDER_DIR = REPO_ROOT / "evals" / "redteam" / "render"
_STATIC = REPO_ROOT / "src" / "localwallet" / "ui" / "web" / "static"


def _load_run_evals():
    spec = importlib.util.spec_from_file_location("render_run_evals", RUN_EVALS_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUN_EVALS = _load_run_evals()

# The banned sinks/handlers (ADR-0024 §7 + web-builder "HARD" contract). Each is
# matched as a *code* usage, so the contract's own explanatory comments (which
# name them in prose) must not trip the scan.
_BANNED_SINKS = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")


def _strip_js_comments(source: str) -> str:
    """Remove // line and /* block */ comments so the sink scan sees only code
    (the client comments the sinks in prose; those are not violations)."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return "\n".join(
        re.sub(r"//.*$", "", line) for line in source.splitlines()
    )


def test_real_client_uses_no_html_string_sinks() -> None:
    app_js = (_STATIC / "app.js").read_text(encoding="utf-8")
    code = _strip_js_comments(app_js)
    for sink in _BANNED_SINKS:
        assert sink not in code, f"app.js writes through the banned sink {sink!r}"
    # Dynamic model output goes in via text nodes ONLY.
    assert "textContent" in code or "createTextNode" in code
    assert "createTextNode" in code or ".textContent =" in code


def test_real_client_has_no_inline_handlers_or_eval_or_javascript_urls() -> None:
    app_js = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    index_html = (_STATIC / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"\bon[a-z]+\s*[:=]", app_js), "inline handler assignment"
    assert not re.search(r"\son[a-z]+=", index_html), "inline on*= handler in markup"
    assert "eval(" not in app_js and "new Function" not in app_js
    assert "javascript:" not in app_js.lower()
    # Buttons are wired with addEventListener (canonical utterances, §8), and the
    # shipped index declares the module script with NO inline body.
    assert "addEventListener" in app_js
    assert "<script" in index_html  # the module tag
    assert 'src="/static/app.js"' in index_html


# TCK-WEB-008 mechanical pins on the shipped client (static, browser-free):
# the watch-key form dismisses on the engine's own accept (never waits on a
# /state round-trip), a stale snapshot cannot re-show it, and the settings
# replace flow rides the ONE existing POST /watchkey channel — no second,
# handler-shaped endpoint.
def test_client_dismisses_the_form_on_accept_and_reuses_watchkey_endpoint() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    assert "dismissWatchKeyForm" in code
    assert "watchKeyDismissed" in code  # terminal dismiss: stale /state can't undo it
    # exactly ONE fetch to /watchkey (postWatchKey), shared by form + replace:
    assert code.count('fetch("/watchkey"') == 1
    assert "replaceStage" in code and "watchKeyRow" in code


# TCK-ONB-007 STATIC half (user correction 2026-09-11) first-run pins: the
# settings pane NEVER self-opens and chat is NEVER disabled while the wallet
# still needs its key — the engine's chat beats own first-run. The pane
# opens ONLY from its explicit controls (header Settings toggle / quick
# action), and the whole WEB-008/009-era auto-open episode machinery is
# gone from the shipped client. The needs-key placeholder rides the TYPED
# snapshot (restore keyed on it too — no local flag), and the grouped
# greeting bubble renders its "\n" line breaks because .turn-text is
# pre-wrap (the SSE/parser round-trip already joins data: lines with \n).
def test_first_run_never_opens_the_pane_and_chat_stays_enabled() -> None:
    raw = (_STATIC / "app.js").read_text(encoding="utf-8")
    code = _strip_js_comments(raw)
    # the retired episode machinery is gone ENTIRELY (not just re-gated):
    for dead in ("settingsAutoShown", "firstRunBeat", "revealFirstRunBeat"):
        assert dead not in raw, f"app.js still carries {dead!r}"
    # the watch-key gate neither disables chat nor touches the pane:
    gate = code[code.index("function applyWatchKeyGate"):
                code.index("function dismissWatchKeyForm")]
    assert "openSettings" not in gate and "closeSettings" not in gate
    assert "inputEl.disabled" not in gate and "sendBtn.disabled" not in gate
    # placeholder flips ON the typed needs (restore rides the same flag):
    assert (
        "inputEl.placeholder = needs ? LABELS.chatNeedsKeyPlaceholder : chatPlaceholder;"
        in gate
    )
    # openSettings has exactly three occurrences: definition + the two
    # EXPLICIT controls (the settings quick action + the header toggle).
    assert code.count("openSettings()") == 3
    # the needs-key ask copy (engine beats own the rest of the prose):
    assert 'chatNeedsKeyPlaceholder: "Paste your xpub or zpub to get started…"' in code
    # the normal placeholder lives in the markup (app.js reads it once):
    index_html = (_STATIC / "index.html").read_text(encoding="utf-8")
    assert 'placeholder="Ask your wallet…"' in index_html
    assert "const chatPlaceholder = inputEl.placeholder;" in code
    # multi-line bubbles: the transcript paints \n as breaks (pre-wrap).
    styles = (_STATIC / "styles.css").read_text(encoding="utf-8")
    turn_text = styles[styles.index(".turn-text {"):]
    turn_text = turn_text[: turn_text.index("}")]
    assert "white-space: pre-wrap" in turn_text


# TCK-ONB-007 static half, behavioral (node if present): the SHIPPED
# applyWatchKeyGate runs against DOM stubs — a needs_watch_key snapshot
# leaves chat ENABLED with the key placeholder, opens NOTHING, and the next
# provisioned snapshot restores the normal placeholder (typed truth, no
# local flag).
def test_watch_key_gate_keeps_chat_open_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    gate = re.search(
        r"function applyWatchKeyGate\(snap\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    script = """
      const LABELS = { chatNeedsKeyPlaceholder: "Paste your xpub or zpub to get started…" };
      const chatPlaceholder = "Ask your wallet…";
      const state = { watchKeyDismissed: false, watchKeyPresent: null, watchKeyNeeded: false };
      const inputEl = { disabled: false, placeholder: chatPlaceholder };
      const sendBtn = { disabled: false };
      const quickbarEl = { hidden: true };
      const settingsPanelEl = { hidden: true }; // stays closed: NOTHING may open it
      let openCalls = 0, closeCalls = 0, renders = 0;
      const openSettings = () => { settingsPanelEl.hidden = false; openCalls++; };
      const closeSettings = () => { closeCalls++; };
      const renderSettings = () => { renders++; };
      const focusWatchInput = () => {};
      __GATE__
      applyWatchKeyGate({ schema: "state/1", needs_watch_key: true });
      if (inputEl.disabled || sendBtn.disabled) throw new Error("chat-disabled");
      if (inputEl.placeholder !== LABELS.chatNeedsKeyPlaceholder) throw new Error("placeholder");
      if (openCalls || closeCalls || !settingsPanelEl.hidden) throw new Error("pane-touched");
      if (state.watchKeyNeeded !== true) throw new Error("needed-not-tracked");
      applyWatchKeyGate({ schema: "state/1", needs_watch_key: false });
      if (inputEl.placeholder !== chatPlaceholder) throw new Error("placeholder-restore");
      if (openCalls || closeCalls) throw new Error("pane-touched-2");
      console.log("ok");
    """.replace("__GATE__", gate)
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


# TCK-UX-010 static pin: the persistent privacy chip renders ONLY the closed
# privacy_mode enum NAMES (unknown/absent → hidden), styles it via the
# data-privacy attribute (CSP-clean: classes/attrs, never inline styles), and
# never writes the raw enum as visible text (the word shown is the static
# "Privacy notice" node in index.html).
def test_privacy_chip_is_enum_gated_and_never_paints_raw_enum() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    assert "applyPrivacyChip" in code and "privacy_mode" in code
    assert "PRIVACY_SUBLINE" in code
    assert 'dataset.privacy = mode' in code  # color rides the attribute, not inline style
    assert "privacyChipEl.textContent" not in code  # visible text stays static markup
    index_html = (_STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="privacy-chip"' in index_html
    assert "Privacy notice" in index_html
    styles = (_STATIC / "styles.css").read_text(encoding="utf-8")
    for name in ("public", "own_node_local", "own_node_remote", "awaiting_backend"):
        assert f'data-privacy="{name}"' in styles


# TCK-WEB-010 static pins: per-bubble copy control. The copyable text of a
# turn excludes transient telemetry (progress dots + model-download bar); an
# empty / progress-only bubble gets no button (addCopyButton early-returns on
# the .copy-btn idempotence check OR an empty bubbleText); exactly ONE button
# class assignment exists, shared by every call site.
def test_copy_button_selector_guard_and_single_class_assignment() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    # copy-text selector excludes progress + model-download telemetry verbatim.
    assert ".turn-text:not(.turn-progress):not(.turn-model)" in code
    # empty-bubble guard sits alongside the wiring, and the .copy-btn early
    # return makes addCopyButton idempotent (no double buttons on re-render).
    assert "!bubbleText(turn)" in code
    assert 'turn.querySelector(".copy-btn")' in code
    # exactly ONE button class assignment, shared by every call site.
    assert code.count('btn.className = "copy-btn"') == 1
    # real call sites today: appendText, appendSystem, appendUser (3).
    assert code.count("addCopyButton(turn);") == 3


# TCK-WEB-011 static pin: the engine's user_text echo is deduped against this
# tab's locally-echoed pending submits (renderUserText), and that branch rides
# the SAME handleEvent AFTER the event-id replay-duplicate guard — so a
# replayed echo of an already-consumed pending is dropped before renderUserText
# ever runs (ordering pinned by source index).
def test_render_user_text_dedupes_after_event_id_guard() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    assert "renderUserText" in code
    assert "state.pendingEchos" in code
    guard = code.index("id <= state.lastEventId")  # replay duplicate guard
    user_text = code.index('kind === "user_text"')  # the dedupe branch
    assert guard < user_text


# TCK-WEB-012/013 static pins: the privacy subline is VISIBLE text painted onto
# privacySublineEl (per privacy_mode NAME) while the raw enum name is never the
# chip's visible text; the chain-row trust badge derives ONLY from the
# state.privacyMode closed enum — never from the effective chain URL content.
def test_privacy_subline_visible_and_trust_badge_keys_off_privacy_mode() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    # visible subline: the closed enum NAME maps to prose on privacySublineEl.
    assert "privacySublineEl.textContent" in code
    # the raw privacy enum is never painted as the chip's visible text.
    assert "privacyChipEl.textContent" not in code
    # the chain-row trust badge derives ONLY from the privacyMode state.
    assert "TRUST_BADGE_WORDS[state.privacyMode]" in code
    # ...and no trust decision reads the effective chain URL content.
    trust_block = code[code.index("const TRUST_BADGE_WORDS"):code.index("function paintTrustBadges")]
    assert "effectiveChainUrl" not in trust_block


# TCK-LINK-001 (revised by TCK-WEB-014, critique D8) static pins: the
# qualifying-token scan is unchanged, but the affordance is a COPY BUTTON —
# the visible label is the verbatim token, no navigation machinery survives
# (no EXPLORER_ORIGIN, no explorerHref, no href/target/rel, no mempool
# disclosure string), and the feedback reuses the shared WEB-010 ok/fail
# pattern. Classes/attributes only (no inline style on the generated control).
def test_linkify_client_shape_pins() -> None:
    raw = (_STATIC / "app.js").read_text(encoding="utf-8")
    code = _strip_js_comments(raw)
    # regexes unchanged: only the navigation died, not the scanner
    assert "const ADDRESS_RE = " in raw
    assert "const TXID_RE = " in raw
    assert "const LINK_SCAN_RE =" in raw  # (its literal wraps to the next line)
    # D8: the dead explorer machinery and its privacy disclosure are gone
    assert "EXPLORER_ORIGIN" not in raw
    assert "explorerHref" not in raw
    assert "explorerLink" not in raw
    assert "Opens mempool.space" not in raw
    # no navigation of any kind: an <a> is never built, href/target/rel absent
    assert 'createElement("a")' not in code
    assert "href" not in code
    assert "_blank" not in code and "noopener" not in code
    # copy affordance: button with the verbatim token as its only content
    assert 'btn.className = "explorer-link"' in code
    assert "btn.textContent = token;" in code
    assert "clipboardWrite(token)" in code
    assert "navigator.clipboard.writeText" in code
    assert '"Click to copy"' in raw
    # WEB-010 ok/fail feedback pattern shared: one helper, both copy controls
    assert code.count("flashCopyResult(") == 3
    assert code.count("1600") == 1  # the revert window lives in the helper only
    assert ".style" not in code and "setAttribute(\"style\"" not in code
    # only the two bubble painters call the token pass (appendText,
    # appendUser); progress + model lines keep plain createTextNode telemetry.
    assert code.count("appendBubbleText(line, text);") == 2
    assert 'el("p", "turn-text turn-progress")' in code  # progress line intact


# TCK-PRIVACY-001B static pins: the ONE web trigger of public-backend consent
# is the chain row's "Use public server" button — exactly one fetch path to
# /consent, reached only by that button; the pane-close path fires NO request
# (closing ≠ consent, the client half); the button's visibility rides ONLY the
# typed /state privacy_mode closed enum (no client-side inference), repainted
# by the same chip pass as the trust badges; and the disclosure reuses the
# pane's own leak sentence verbatim (one voice, no invented copy).
def test_consent_button_is_the_only_consent_path_and_state_gated() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    assert code.count('fetch("/consent"') == 1  # one handler, no bypass routes
    assert code.count("requestPublicConsent(") == 2  # definition + button wiring
    close = code[code.index("function closeSettings"):
                 code.index("settingsToggleEl.addEventListener")]
    assert "fetch(" not in close  # pane close sends NOTHING: never an implied consent
    # visibility: built gated on the enum, retired by the chip's own pass.
    assert 'consent.hidden = state.privacyMode !== "awaiting_backend";' in code
    painter = code[code.index("function paintConsentRow"):
                    code.index("function applyPrivacyChip")]
    assert 'state.privacyMode === "awaiting_backend"' in painter
    chip = code[code.index("function applyPrivacyChip"):
                code.index("function applyBackendKind")]
    assert "paintConsentRow();" in chip
    # honest copy: the button subline IS the pane's leak sentence.
    assert 'consentSubline: "The public mempool.space server — " + PUBLIC_LEAK_SENTENCE' in code


# TCK-LINK-001 regexes + TCK-WEB-014 behavior (runs under node if present):
# the SHIPPED scanner regexes are extracted and fed the same accept/reject
# vectors (unchanged), and the SHIPPED copyTokenButton/clipboardWrite/
# flashCopyResult run against DOM stubs: the control is a <button> with the
# verbatim token as its only content and NO navigation attributes, a click
# writes the verbatim token to the clipboard and lands in the WEB-010 ok
# state (reverting after the 1.6s window), and a rejected write lands in the
# visible fail state.
def test_linkify_regexes_and_click_to_copy_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    address_re = re.search(r"const ADDRESS_RE = (/[^;]+);", code).group(1)
    txid_re = re.search(r"const TXID_RE = (/[^;]+);", code).group(1)
    scan_re = re.search(r"const LINK_SCAN_RE =\s*\n?\s*([^;]+);", code).group(1)
    copy_fn = re.search(
        r"function copyTokenButton\(token\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    clip_fn = re.search(
        r"async function clipboardWrite\(text\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    flash_fn = re.search(
        r"function flashCopyResult\(ctrl, ok, baseTitle\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    script = """
      const ADDRESS_RE = __ADDR_RE__;
      const TXID_RE = __TX_RE__;
      const LINK_SCAN_RE = __SCAN_RE__;
      function linkify(text) {
        const out = [];
        LINK_SCAN_RE.lastIndex = 0;
        let m;
        while ((m = LINK_SCAN_RE.exec(text)) !== null) out.push(m[0]);
        return out;
      }
      const addr = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq";
      const tx = "f".repeat(64);
      // accepted: standalone tokens only — the scan yields the token verbatim
      let a = linkify("send to " + addr + " now");
      if (a.length !== 1 || a[0] !== addr) throw new Error("addr");
      a = linkify("tx " + tx + " confirmed");
      if (a.length !== 1 || a[0] !== tx) throw new Error("tx");
      // rejected: uppercase, longer-word embedding, oversized, wrong charset
      if (linkify("BC1QAR0SRRR7XFKVY5L643LYDNW9RE59GTZZWF5MDQ").length) throw new Error("upper");
      if (linkify("x" + addr).length) throw new Error("embedded-left");
      if (linkify("pre" + tx).length) throw new Error("tx-embedded");
      if (linkify(tx + "f").length) throw new Error("tx-65");
      if (linkify(addr.slice(0, 3) + "!" + addr.slice(4)).length) throw new Error("charset");
      if (linkify("/address/x bc1q").length) throw new Error("fragment");
      if (linkify("x".repeat(100)).length) throw new Error("noise");
      if (linkify("b".repeat(100)).length) throw new Error("oversize");
      // --- the copy control itself, shipped functions on DOM stubs ---
      const LABELS = { clickToCopy: "Click to copy", copyDone: "OK", copyFailed: "FAIL" };
      let resetFn = null;
      globalThis.setTimeout = (fn) => { resetFn = fn; return 1; };
      globalThis.clearTimeout = () => { resetFn = null; };
      globalThis.document = { createElement: (tag) => {
        if (tag !== "button") throw new Error("not-a-button: " + tag);
        const classes = new Set();
        return {
          type: "", className: "", textContent: "", title: "",
          setAttribute(name, value) { this[name] = value; },
          addEventListener(_name, fn) { this._click = fn; },
          classList: {
            add: (c) => classes.add(c),
            remove: (c) => classes.delete(c),
            contains: (c) => classes.has(c),
          },
        };
      }};
      let copied = null;
      let refuse = false;
      // navigator is a getter-only global on modern node — define, don't assign
      Object.defineProperty(globalThis, "navigator", {
        configurable: true,
        value: { clipboard: { writeText: async (t) => {
          if (refuse) throw new Error("denied");
          copied = t;
        } } },
      });
      __COPY_FN__
      __CLIP_FN__
      __FLASH_FN__
      const main = async () => {
        const btn = copyTokenButton(addr);
        if (btn.type !== "button") throw new Error("no-type");
        if (btn.textContent !== addr) throw new Error("label-not-verbatim");
        // no navigation machinery on the control (it is not a link at all)
        if ("href" in btn || "target" in btn || "rel" in btn) throw new Error("navigation");
        if (btn.title !== LABELS.clickToCopy) throw new Error("title");
        if (btn["aria-label"] !== LABELS.clickToCopy) throw new Error("aria");
        await btn._click(); // clipboard receives the VERBATIM token
        if (copied !== addr) throw new Error("clipboard-value");
        if (!btn.classList.contains("copy-ok") || btn.classList.contains("copy-fail"))
          throw new Error("ok-state");
        if (btn.title !== LABELS.copyDone) throw new Error("ok-title");
        resetFn(); // the 1.6s window reverts class + base title
        if (btn.classList.contains("copy-ok") || btn.title !== LABELS.clickToCopy)
          throw new Error("revert");
        refuse = true; // a rejected write = visible fail state, no silent swallow
        await btn._click();
        if (!btn.classList.contains("copy-fail") || btn.title !== LABELS.copyFailed)
          throw new Error("fail-state");
        console.log("ok");
      };
      main().catch((e) => { console.error(e); process.exit(1); });
    """
    script = (
        script.replace("__ADDR_RE__", address_re)
        .replace("__TX_RE__", txid_re)
        .replace("__SCAN_RE__", scan_re)
        .replace("__COPY_FN__", copy_fn)
        .replace("__CLIP_FN__", clip_fn)
        .replace("__FLASH_FN__", flash_fn)
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


# TCK-WEB-015 static pins (user direction): the in-flight indicator is a
# TRANSIENT pending bubble IN the transcript — the old below-input busy
# element is gone from markup, client and stylesheet (no id, no ref, no
# selector; the connection-status area survives). The bubble's lifecycle
# hooks are pinned at every seam: both submit paths (shared submit()) and
# the remote user_text echo show it, noteTurnEnd and a failed submit clear
# it under the SAME !busy guard (replace-on-turn_end, queue-shared), and
# every transcript append re-tails the single node (so replay/echo storms
# can never duplicate it — show is guarded and appendChild MOVES). The
# UX-008 animation (keyframes + reduced-motion opt-out) still exists and
# now rides inside the bubble; the bubble itself carries no text nodes
# (transient — never copyable, never persisted).
def test_pending_bubble_lives_in_the_transcript_and_the_old_indicator_is_gone() -> None:
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    index_html = (_STATIC / "index.html").read_text(encoding="utf-8")
    styles = (_STATIC / "styles.css").read_text(encoding="utf-8")
    # the removed below-input widget leaves NOTHING behind (comments too):
    assert "turn-busy" not in code + index_html + styles
    assert "busyEl" not in code
    # connection status ≠ turn status: the header conn-status machinery stays.
    assert 'id="conn-status"' in index_html and "function setStatus" in code
    # lifecycle seams: show on submit + remote echo; a failed submit clears
    # behind the !busy (drained-queue) guard; noteTurnEnd clears ONLY with
    # nothing promoted — a promoted queued turn is still in flight and its
    # echo dedupes, so the single bubble must ride on for it (MINOR fix).
    submit_fn = re.search(
        r"async function submit\(path, field, value\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    note_end = re.search(r"function noteTurnEnd\(\) \{.*?\n\}", code, re.DOTALL).group(0)
    render_user = re.search(
        r"function renderUserText\(text\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    assert "showPendingBubble();" in submit_fn
    assert re.search(r"if \(!state\.busy\) clearPendingBubble\(\);", submit_fn)
    assert "showPendingBubble();" in render_user
    assert re.search(r"if \(!next\) clearPendingBubble\(\);", note_end)
    assert "if (!state.busy) clearPendingBubble();" not in note_end
    # single tail node: guarded show + one shared retailer used by every
    # transcript appender (ensureTurn, appendSystem, appendUser, show).
    assert code.count("if (!state.pendingBubble) {") == 1  # the guarded show
    assert code.count("tailPendingBubble();") == 4
    # the bubble is the dots, not content: no .turn-text line (the copy
    # selectors never see it), aria name is the relocated UX-008 word.
    assert 'el("li", "turn turn-pending")' in code
    assert '"busy-dots"' in code and "LABELS.turnWorking" in code
    assert "turnWorking: " in code and "Working…" in code  # existing word, not new copy
    # transient: the client persists nothing, ever (the bubble included).
    assert "localStorage" not in code and "sessionStorage" not in code
    # the animation survived the move: keyframes + reduced-motion opt-out,
    # plus the bubble's own style rule.
    assert "@keyframes busy-dot" in styles
    assert "prefers-reduced-motion: reduce" in styles
    assert ".turn-pending" in styles


# TCK-WEB-015 behavioral check (node if present): the SHIPPED bubble trio +
# renderUserText + noteTurnEnd run against a moving-child DOM stub — a
# remote user_text echo shows the bubble at the tail, repeated echoes and
# own-echo dedupe never duplicate it, a content append re-tails the single
# node, turn_end with a non-empty queue KEEPS it (the queue shares the one
# bubble), the turn_end PROMOTING the last queued turn keeps it too (the
# promoted turn is in flight — review MINOR), and only the turn_end with
# nothing left to promote removes it.
def test_pending_bubble_lifecycle_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    fns = [
        re.search(rf"function {name}\([^)]*\) \{{.*?\n\}}", code, re.DOTALL).group(0)
        for name in (
            "showPendingBubble", "clearPendingBubble",
            "tailPendingBubble", "renderUserText", "noteTurnEnd", "closeOpenTurn",
        )
    ]
    script = """
      const mkNode = (tag) => ({
        tag, className: "", textContent: "", attrs: {}, children: [], parent: null,
        setAttribute(k, v) { this.attrs[k] = v; },
        appendChild(n) {
          if (n.parent) {
            const i = n.parent.children.indexOf(n);
            if (i !== -1) n.parent.children.splice(i, 1);
          }
          n.parent = this; this.children.push(n); return n;
        },
        append(...ns) { for (const n of ns) this.appendChild(n); },
        remove() {
          if (!this.parent) return;
          const i = this.parent.children.indexOf(this);
          if (i !== -1) this.parent.children.splice(i, 1);
          this.parent = null;
        },
        querySelector() { return null; },
        get classList() {
          const self = this;
          const set = () => new Set(self.className.split(/\\s+/).filter(Boolean));
          return {
            add(c) { const s = set(); s.add(c); self.className = [...s].join(" "); },
            remove(c) { const s = set(); s.delete(c); self.className = [...s].join(" "); },
            contains(c) { return set().has(c); },
          };
        },
      });
      globalThis.document = { createElement: (t) => mkNode(t) };
      const el = (tag, className, text) => {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
      };
      const LABELS = { turnWorking: "Working…" };
      const state = {
        busy: true, openTurn: null, progressLine: null, queue: [],
        pendingEchos: [], pendingBubble: null,
      };
      const transcriptEl = mkNode("ol");
      const appendUser = (text) => { state._echoes = (state._echoes || 0) + 1; return mkNode("li"); };
      const setBusy = (b) => { state.busy = b; };
      const scrollToEnd = () => {};
      const refreshState = () => {};
      const bubbles = () =>
        transcriptEl.children.filter((n) => n.className.includes("turn-pending"));
      __SHOW__
      __CLEAR__
      __TAIL__
      __RENDER__
      __CLOSE__
      __END__
      // 1. remote echo (other tab/CLI): the bubble appears at the tail
      renderUserText("hello from the CLI");
      if (bubbles().length !== 1) throw new Error("show-remote");
      // 2. replay storm: repeated echoes never duplicate (guarded show)
      renderUserText("hello from the CLI");
      if (bubbles().length !== 1) throw new Error("no-duplicate");
      // 3. own-echo dedupe: suppressed event adds no bubble, keeps the one
      state.pendingEchos.push("mine");
      renderUserText("mine");
      if (bubbles().length !== 1 || state._echoes !== 2) throw new Error("own-echo");
      // 4. content appended mid-stream re-tails the SINGLE node
      const content = mkNode("li");
      transcriptEl.appendChild(content);
      tailPendingBubble();
      if (bubbles().length !== 1) throw new Error("retail-dup");
      if (transcriptEl.children[transcriptEl.children.length - 1] !== bubbles()[0])
        throw new Error("retail-tail");
      // 5. no text nodes inside (transient, copy-invisible): just the dots
      const b = bubbles()[0];
      if (b.children.some((n) => n.textContent !== "")) throw new Error("bubble-text");
      if (b.attrs["aria-label"] !== LABELS.turnWorking) throw new Error("bubble-name");
      // 6. turn_end with a still-full queue: the shared tail bubble KEEPS
      state.busy = true;
      state.queue.push(mkNode("li"), mkNode("li"));
      state.queue[0].classList.add("turn-queued");
      noteTurnEnd();
      if (bubbles().length !== 1 || !state.busy) throw new Error("queue-keeps-bubble");
      // 7. the LAST queued turn's PROMOTION at turn_end keeps the bubble
      //    too (the review MINOR): busy flips false but the promoted turn is
      //    in flight and its echo dedupes — nothing else would re-show it.
      noteTurnEnd();
      if (bubbles().length !== 1 || state.busy) throw new Error("promoted-keeps-bubble");
      // 8. turn_end with nothing promoted and the queue empty: removed
      //    (replace-on-turn_end, pinned)
      noteTurnEnd();
      if (bubbles().length !== 0 || state.pendingBubble !== null)
        throw new Error("drain-clears");
      // 9. a later submit re-shows a FRESH node (old ref gone)
      showPendingBubble();
      if (bubbles().length !== 1 || bubbles()[0] === b) throw new Error("reshow");
      console.log("ok");
    """
    script = (
        script.replace("__SHOW__", fns[0])
        .replace("__CLEAR__", fns[1])
        .replace("__TAIL__", fns[2])
        .replace("__RENDER__", fns[3])
        .replace("__CLOSE__", fns[5])
        .replace("__END__", fns[4])
    )
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


# TCK-WEB-024 behavioral check (node if present): background narration can
# never steal a later reply's turn anchor. The SHIPPED client functions
# (ensureTurn/appendUser/appendSystem/appendText/appendProgress/renderUserText/
# noteTurnEnd/closeOpenTurn) run against a moving-child DOM stub across the
# four real server orderings — a non-engine bubble (user/system) closes any
# open engine turn (FIX 2), so the reply always lands in its OWN engine turn
# AFTER the user's bubble, never inside the narration bubble. Plus the
# client-only pin: even an UNCLOSED background turn (the pre-fix engine
# stream) cannot re-anchor the reply.
def test_reply_never_lands_in_the_narration_bubble_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    fns = [
        re.search(rf"function {name}\([^)]*\) \{{.*?\n\}}", code, re.DOTALL).group(0)
        for name in (
            "closeOpenTurn", "ensureTurn", "appendText", "appendProgress",
            "appendUser", "appendSystem", "noteTurnEnd", "renderUserText",
            "showPendingBubble", "clearPendingBubble", "tailPendingBubble",
            "handleEvent",
        )
    ]
    script = """
      const mkNode = (tag) => ({
        tag, className: "", textContent: "", attrs: {}, children: [], parent: null,
        setAttribute(k, v) { this.attrs[k] = v; },
        appendChild(n) {
          if (n.parent) {
            const i = n.parent.children.indexOf(n);
            if (i !== -1) n.parent.children.splice(i, 1);
          }
          n.parent = this; this.children.push(n); return n;
        },
        append(...ns) { for (const n of ns) this.appendChild(n); },
        remove() {
          if (!this.parent) return;
          const i = this.parent.children.indexOf(this);
          if (i !== -1) this.parent.children.splice(i, 1);
          this.parent = null;
        },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        get classList() {
          const self = this;
          const set = () => new Set(self.className.split(/\\s+/).filter(Boolean));
          return {
            add(c) { const s = set(); s.add(c); self.className = [...s].join(" "); },
            remove(c) { const s = set(); s.delete(c); self.className = [...s].join(" "); },
            contains(c) { return set().has(c); },
          };
        },
      });
      globalThis.document = {
        createElement: (t) => mkNode(t),
        createTextNode: (d) => {
          const n = mkNode("#text");
          n.textContent = d;
          n.appendData = (s) => { n.textContent += s; };
          return n;
        },
      };
      const el = (tag, className, text) => {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
      };
      const LABELS = { turnWorking: "Working…" };
      const hintEl = { hidden: true };
      const scrollToEnd = () => {};
      const refreshState = () => {};
      const addCopyButton = () => {}; // leaf copy control: no ordering effect
      const appendBubbleText = (line, text) => { line.textContent = text; };
      const state = { busy: false, openTurn: null, progressLine: null,
                      downloadLine: null, queue: [], pendingEchos: [],
                      pendingBubble: null, lastEventId: 0 };
      const setBusy = (b) => { state.busy = b; };
      const transcriptEl = mkNode("ol");
      __FNS__
      let id = 0;
      function drive(events) {
        // fresh transcript + pristine state per interleaving
        transcriptEl.children.length = 0;
        state.openTurn = null; state.progressLine = null; state.downloadLine = null;
        state.queue = []; state.pendingEchos = []; state.pendingBubble = null;
        state.lastEventId = 0; state.busy = false;
        for (const [kind, data] of events) handleEvent(++id, kind, data);
      }
      const userIdx = () => transcriptEl.children.findIndex(
        (n) => n.className.includes("turn-user"));
      const hasText = (n, s) => (n.textContent || "").includes(s) ||
        n.children.some((c) => hasText(c, s));
      const replyTurn = () => transcriptEl.children.find(
        (n) => n.className.includes("turn-engine") && hasText(n, "REPLY"));
      const narrationTurn = (t) => transcriptEl.children.find(
        (n) => n.className.includes("turn-engine") && hasText(n, t));
      function check(label) {
        const u = userIdx(), r = replyTurn();
        if (u === -1) throw new Error(label + ": no user bubble");
        if (!r) throw new Error(label + ": no reply turn");
        if (transcriptEl.children.indexOf(r) <= u)
          throw new Error(label + ": reply before user bubble");
        // the reply must NOT share the narration bubble's engine turn
        const nTurn = narrationTurn("SCAN") || narrationTurn("WATCH");
        if (nTurn && r === nTurn) throw new Error(label + ": reply inside narration");
      }
      // A: [scan text][turn_end][user][reply][turn_end]
      drive([["text","SCAN"],["turn_end",""],["user_text","u"],
             ["text","REPLY"],["turn_end",""]]);
      check("A");
      // B: [user][scan text][turn_end][reply][turn_end]
      drive([["user_text","u"],["text","SCAN"],["turn_end",""],
             ["text","REPLY"],["turn_end",""]]);
      check("B");
      // C: the REAL rescan stream — dots then summary then user then reply.
      //    (FAILS pre-fix: the summary opened a turn the reply then joined.)
      drive([["progress","."],["progress","."],["progress","."],
             ["progress","\\n"],["text","SCAN"],["user_text","u"],
             ["text","REPLY"],["turn_end",""]]);
      check("C");
      // D: [watch text][turn_end][user][reply][turn_end]
      drive([["text","WATCH"],["turn_end",""],["user_text","u"],
             ["text","REPLY"],["turn_end",""]]);
      check("D");
      // client-only pin: an UNCLOSED background turn (no turn_end marker)
      // still renders the reply below the user bubble, not inside it.
      drive([["text","SCAN"],["user_text","u"],["text","REPLY"]]);
      check("unclosed");
      console.log("ok");
    """
    script = script.replace("__FNS__", "\n".join(fns))
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


# TCK-WEB-016 behavioral check (node if present): the SHIPPED submit() and
# the form listener are extracted from app.js and run against a stubbed
# fetch — a 401 (stale-tab token from a previous launch) renders the
# session-stale/reload line, NOT "server unreachable"; other failures keep
# the unreachable line; a stopped tab shows the stale line instead of
# swallowing the press with the text still in the box.
def test_submit_401_and_stopped_tab_label_the_stale_session_under_node() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")
    code = _strip_js_comments((_STATIC / "app.js").read_text(encoding="utf-8"))
    submit_fn = re.search(
        r"async function submit\(path, field, value\) \{.*?\n\}", code, re.DOTALL
    ).group(0)
    form_listener = re.search(
        r'formEl\.addEventListener\("submit", \(event\) => \{.*?\n\}\);',
        code,
        re.DOTALL,
    ).group(0)
    script = """
      const LABELS = { unreachable: "UNREACHABLE-LINE", sessionStale: "STALE-LINE" };
      const state = { busy: false, stopped: false, queue: [], pendingEchos: [] };
      let systemLines = [];
      const appendSystem = (s) => systemLines.push(s);
      const setBusy = (b) => { state.busy = b; };
      // TCK-WEB-015: submit shows/clears the transient transcript bubble.
      let bubbles = 0;
      const showPendingBubble = () => { bubbles++; };
      const clearPendingBubble = () => { bubbles--; };
      const authHeaders = (h) => h;
      let lastEcho = null;
      const appendUser = (value) => {
        lastEcho = { removed: false, remove() { this.removed = true; } };
        return lastEcho;
      };
      let fetchImpl = null;
      const fetch = (path, opts) => fetchImpl(path, opts);
      __SUBMIT__
      let formHandler = null;
      const formEl = { addEventListener: (_n, fn) => { formHandler = fn; } };
      const inputEl = { value: "" };
      __FORM__
      const main = async () => {
        // accepted: echo stays, no system line
        fetchImpl = async () => ({ status: 202, ok: true });
        await submit("/turn", "text", "hello");
        if (lastEcho.removed || systemLines.length) throw new Error("202-path");
        // 401: the stale-session line (never "unreachable"), echo un-rendered
        fetchImpl = async () => ({ status: 401, ok: false });
        systemLines = [];
        await submit("/turn", "text", "hello");
        if (!lastEcho.removed) throw new Error("401-kept-echo");
        if (systemLines.length !== 1 || systemLines[0] !== LABELS.sessionStale)
          throw new Error("401-label");
        // 503 / transport failure: the unreachable line stands
        fetchImpl = async () => ({ status: 503, ok: false });
        systemLines = [];
        await submit("/turn", "text", "hello");
        if (systemLines[0] !== LABELS.unreachable) throw new Error("503-label");
        fetchImpl = async () => { throw new TypeError("network"); };
        systemLines = [];
        await submit("/turn", "text", "hello");
        if (systemLines[0] !== LABELS.unreachable) throw new Error("throw-label");
        // stopped tab: the press is NOT swallowed — stale line, text kept
        systemLines = [];
        inputEl.value = "typed into a stopped tab";
        state.stopped = true;
        formHandler({ preventDefault: () => {} });
        if (systemLines[0] !== LABELS.sessionStale) throw new Error("stopped-line");
        if (inputEl.value === "") throw new Error("stopped-consumed");
        // TCK-WEB-015: every failed submit cleared its own bubble; only the
        // one accepted turn's shared bubble remains (never one per submit).
        if (bubbles !== 1) throw new Error("bubble-count");
        console.log("ok");
      };
      main().catch((e) => { console.error(e); process.exit(1); });
    """.replace("__SUBMIT__", submit_fn).replace("__FORM__", form_listener)
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


@pytest.mark.parametrize(
    "path", sorted(_RENDER_DIR.glob("*.json")), ids=lambda p: p.stem
)
def test_render_fixture_payload_stays_inert(path: Path) -> None:
    """Every hostile payload survives as ONE element-free text node (the
    render contract), and the fixture declares the correct contract shape."""
    case = json.loads(path.read_text(encoding="utf-8"))
    RUN_EVALS._validate_render_expectation(case["expectation"])
    ok, reason = RUN_EVALS._render_case_is_inert(case)
    assert ok, reason
    payload = case["payload"]
    node = RUN_EVALS.render_text_content(payload)
    # The browser would show the string verbatim — no parsing, no node growth.
    assert node == {"node_type": "#text", "data": payload, "children": []}
    # Control-char / oversized payloads stay byte-exact (never "corrected").
    assert node["data"] == payload


def test_render_fixture_corpus_covers_the_hostile_vectors() -> None:
    """The set is a genuine red-team corpus: the vectors span event handlers,
    script elements, javascript: URLs, attribute breakouts, control chars and an
    oversized line — not one payload copy-pasted."""
    vectors = set()
    for path in _RENDER_DIR.glob("*.json"):
        case = json.loads(path.read_text(encoding="utf-8"))
        vectors.add(case["vector"])
        assert case["category"] == "render"
    assert {
        "event_handler_attribute",
        "inline_script_element",
        "javascript_url_href",
        "attribute_breakout",
        "control_characters",
        "oversized_single_line",
    } <= vectors


# ------------------------------------------------ live server: CSP + nonce pins


def _bootstrap() -> Any:
    """A model-free stub engine (mirrors tests/test_web_server._bootstrap): the
    render-contract tests serve the REAL static client, so the engine only has
    to answer turns, not run a wallet."""
    from localwallet import app as _app
    from localwallet.agent.loop import AgentLoop
    from localwallet.protocol import IntentName
    from localwallet.tx.flow import TxFlow

    table = {
        IntentName.RESPOND: _app._respond_handler,
        IntentName.CLARIFY: _app._clarify_handler,
    }
    return _app.EngineContext(
        loop=AgentLoop(_app.stub_generate, table),
        flow=TxFlow(),
        session=_app.SendSession(),
        table=table,
    )


def _serve_real_static() -> Any:
    from localwallet.ui.web.server import serve_web

    server = serve_web(_bootstrap, static_dir=_STATIC)
    server.serve()
    return server


def test_served_index_csp_allows_only_the_nonced_island() -> None:
    import http.client

    server = _serve_real_static()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.httpd.server_address[1])
        conn.request("GET", "/", headers={"X-Auth-Token": server.token})
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        csp = response.getheader("Content-Security-Policy")
        conn.close()
    finally:
        server.stop()

    assert csp is not None
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "script-src 'self' 'nonce-" in csp
    # Exactly ONE inline <script> — the token island — and it carries a nonce.
    inline = re.findall(r"<script(?![^>]*\ssrc=)[^>]*>", body)
    assert len(inline) == 1
    nonce = re.search(r'<script nonce="([^"]+)"', inline[0])
    assert nonce is not None, "the island inline script must carry the CSP nonce"
    assert nonce.group(1) in csp
    # The external module script (src=) needs no nonce (covered by 'self').
    assert re.search(r'<script[^>]*\ssrc="/static/app\.js"', body)


def test_static_asset_served_with_no_inline_nonce_csp() -> None:
    import http.client

    server = _serve_real_static()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.httpd.server_address[1])
        conn.request("GET", "/static/app.js", headers={"X-Auth-Token": server.token})
        response = conn.getresponse()
        ctype = response.getheader("Content-Type")
        csp = response.getheader("Content-Security-Policy")
        response.read()
        conn.close()
    finally:
        server.stop()
    assert ctype.startswith(("application/javascript", "text/javascript"))
    assert "script-src 'self';" in csp  # strict no-inline policy, no island nonce

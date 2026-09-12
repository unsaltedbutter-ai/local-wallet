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

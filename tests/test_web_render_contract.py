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

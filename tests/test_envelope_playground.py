"""Tests for the envelope playground (TCK-P0-009, tools/envelope_playground.py).

Covers the local HTTP debugging tool end-to-end with real HTTP over an
ephemeral port (``httpx`` client-side; a test-dep usage), the single-shot
validation path with injected fake runtimes (no live model), the HTML
escaping of untrusted output, the 400/405/500 handling, the loopback-only
binding, the no-``chain``-import invariant, and the ``--golden`` report.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAYGROUND_PATH = REPO_ROOT / "tools" / "envelope_playground.py"

#: Valid envelope JSON emitted by several fakes.
_GET_BALANCE = '{"v": 0, "intent": "get_balance", "params": {}}'


def _load_playground():
    spec = importlib.util.spec_from_file_location("envelope_playground", PLAYGROUND_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PG = _load_playground()


def _serve(state) -> tuple[object, threading.Thread, int]:
    """Start a playground server on an ephemeral port; return (server, thread, port)."""
    server = PG.create_server(state, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, server.server_address[1]


class _Stopper:
    """Stops the server+thread on fixture teardown."""

    def __init__(self, server, thread) -> None:
        self.server = server
        self.thread = thread

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic env: no remote endpoint, no model path by default."""
    monkeypatch.delenv("LOCALWALLET_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCALWALLET_LLM_MODEL", raising=False)
    monkeypatch.delenv("LOCALWALLET_MODEL_PATH", raising=False)
    monkeypatch.delenv("LOCALWALLET_PLAYGROUND_PORT", raising=False)


# --------------------------------------------------------------- page + API


def test_page_serves_200_with_form_and_local_notice(clean_env) -> None:
    state = PG.PlaygroundState()  # no env → local/stub runtime
    server, thread, port = _serve(state)
    stopper = _Stopper(server, thread)
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert 'id="text"' in resp.text
        assert 'type="submit"' in resp.text
        assert "local/stub runtime" in resp.text
    finally:
        stopper.close()


def test_api_happy_path_get_balance(clean_env) -> None:
    fake = lambda prompt, grammar: _GET_BALANCE
    state = PG.PlaygroundState(generate_fn=fake)
    server, thread, port = _serve(state)
    stopper = _Stopper(server, thread)
    try:
        resp = httpx.post(
            f"http://127.0.0.1:{port}/api/envelope", json={"text": "What's my balance?"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["raw"] == _GET_BALANCE
        assert body["outcome"]["status"] == "ok"
        assert body["outcome"]["intent"] == "get_balance"
        assert body["outcome"]["params"] == {}
        assert body["outcome"]["failures"] == []
        assert "local/stub runtime" in body["notice"]
    finally:
        stopper.close()


def test_api_garbage_raw_verbatim_and_failures(clean_env) -> None:
    garbage = "this is not an envelope at all"
    fake = lambda prompt, grammar: garbage
    state = PG.PlaygroundState(generate_fn=fake)
    server, thread, port = _serve(state)
    stopper = _Stopper(server, thread)
    try:
        resp = httpx.post(
            f"http://127.0.0.1:{port}/api/envelope", json={"text": "hello"}
        )
        assert resp.status_code == 200
        body = resp.json()
        # Raw output preserved verbatim, no server-side transformation.
        assert body["raw"] == garbage
        assert body["outcome"]["status"] in ("rejected", "needs_retry")
        assert body["outcome"]["failures"], "expected validation failures"
        assert body["outcome"]["intent"] is None
        assert body["outcome"]["params"] is None
    finally:
        stopper.close()


def test_api_new_intent_get_history_returns_stub_result(clean_env) -> None:
    """An intent beyond the original 3-handler stub (get_history) now has a
    stub echo handler, so the probe reports ``ok`` instead of a reject."""
    get_history = '{"v": 0, "intent": "get_history", "params": {}}'
    state = PG.PlaygroundState(generate_fn=lambda p, g: get_history)
    server, thread, port = _serve(state)
    stopper = _Stopper(server, thread)
    try:
        resp = httpx.post(
            f"http://127.0.0.1:{port}/api/envelope", json={"text": "my history"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["outcome"]["status"] == "ok"
        assert body["outcome"]["intent"] == "get_history"
        assert body["outcome"]["params"] == {}
        assert body["outcome"]["failures"] == []
    finally:
        stopper.close()


def test_api_preserves_raw_html_control_chars(clean_env) -> None:
    raw = '{"v": 0, "intent": "respond", "params": {"text": "<b>& \\"quotes\\"\\nESC"}}'
    fake = lambda prompt, grammar: raw
    state = PG.PlaygroundState(generate_fn=fake)
    server, thread, port = _serve(state)
    stopper = _Stopper(server, thread)
    try:
        resp = httpx.post(
            f"http://127.0.0.1:{port}/api/envelope", json={"text": "hi"}
        )
        assert resp.status_code == 200
        body = resp.json()
        # JSON transport round-trips the raw string exactly (only the remote
        # runtime strips; an injected fake bypasses that).
        assert body["raw"] == raw
    finally:
        stopper.close()


def test_escape_helper_escapes_untrusted_output() -> None:
    assert PG.escape_html('<script>"&') == "&lt;script&gt;&quot;&amp;"
    assert PG.escape_html("'") == "&#x27;"
    assert PG.escape_html("plain text") == "plain text"


def test_page_escapes_hostile_notice(clean_env) -> None:
    state = PG.PlaygroundState()
    # Force a "remote" state with a hostile notice to prove the page escapes it.
    state.notice = '<img src=x onerror=alert(1)> & "quotes"'
    state.remote = True
    page = PG._page_html(state)
    assert "<img" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page


# ---------------------------------------------------------- status handling


def test_api_400_missing_text(clean_env) -> None:
    state = PG.PlaygroundState(generate_fn=lambda p, g: _GET_BALANCE)
    server, thread, port = _serve(state)
    stopper = _Stopper(server, thread)
    try:
        resp = httpx.post(f"http://127.0.0.1:{port}/api/envelope", json={})
        assert resp.status_code == 400
        assert "text" in resp.json()["error"]
        resp2 = httpx.post(f"http://127.0.0.1:{port}/api/envelope", json={"text": 123})
        assert resp2.status_code == 400
    finally:
        stopper.close()


def test_api_400_oversized_text(clean_env) -> None:
    state = PG.PlaygroundState(generate_fn=lambda p, g: _GET_BALANCE)
    server, thread, port = _serve(state)
    stopper = _Stopper(server, thread)
    try:
        resp = httpx.post(
            f"http://127.0.0.1:{port}/api/envelope", json={"text": "x" * (PG.MAX_TEXT_CHARS + 1)}
        )
        assert resp.status_code == 400
    finally:
        stopper.close()


def test_api_405_on_get(clean_env) -> None:
    state = PG.PlaygroundState(generate_fn=lambda p, g: _GET_BALANCE)
    server, thread, port = _serve(state)
    stopper = _Stopper(server, thread)
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/api/envelope")
        assert resp.status_code == 405
        assert "POST" in resp.headers.get("allow", "")
    finally:
        stopper.close()


def test_api_500_no_traceback(clean_env) -> None:
    def boom(prompt, grammar):
        raise RuntimeError("secret internal detail")

    state = PG.PlaygroundState(generate_fn=boom)
    server, thread, port = _serve(state)
    stopper = _Stopper(server, thread)
    try:
        resp = httpx.post(f"http://127.0.0.1:{port}/api/envelope", json={"text": "hi"})
        assert resp.status_code == 500
        body = resp.json()
        assert body["error"] == "internal_error"
        # No traceback / no raw detail leak.
        assert "Traceback" not in resp.text
        assert "secret internal detail" not in resp.text
    finally:
        stopper.close()


# ------------------------------------------------------------ invariants


def test_server_binds_loopback_only(clean_env) -> None:
    state = PG.PlaygroundState(generate_fn=lambda p, g: _GET_BALANCE)
    server, thread, _port = _serve(state)
    stopper = _Stopper(server, thread)
    try:
        assert server.server_address[0] == "127.0.0.1"
        assert PG.PLAYGROUND_HOST == "127.0.0.1"
    finally:
        stopper.close()


def test_stub_table_covers_all_six_intents() -> None:
    """Every closed-protocol intent maps to a stub echo handler."""
    assert set(PG.STUB_TABLE) == {
        PG.IntentName.RESPOND,
        PG.IntentName.CLARIFY,
        PG.IntentName.GET_BALANCE,
        PG.IntentName.GET_HISTORY,
        PG.IntentName.GET_UTXOS,
        PG.IntentName.NEW_ADDRESS,
    }


def test_no_chain_import_in_playground_source() -> None:
    """The stub table and file never import the chain module / EsploraClient."""
    import ast

    source = PLAYGROUND_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(PLAYGROUND_PATH))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    # No import statement may touch the chain module.
    assert not any(m.startswith("localwallet.chain") for m in imported)
    # No EsploraClient identifier anywhere (import or prose).
    assert "EsploraClient" not in source
    # The only network-capable module referenced is the ADR-0007 bridge, via
    # import of its names only — never a network import in this file.
    assert "import httpx" not in source
    assert "import socket" not in source


# ------------------------------------------------------------------ golden


def test_golden_report_with_fake_runtime(
    clean_env, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Always-get_balance fake: PASS the get_balance fixtures, FAIL the rest.

    Runs against a controlled golden set (the single-intent fixtures
    ``golden-001..011``) rather than the full suite: the playground's matcher
    (``tools/envelope_playground.py``) understands only single-intent
    expectations, while the Phase 1 suite gained ``intent_in`` fixtures
    (golden-014, golden-019) that belong to ``evals/run_evals.py``'s extended
    matcher. Restricting to the single-intent set keeps this test faithful
    and ``tools/`` untouched.
    """
    import json as _json

    golden_dir = tmp_path / "golden"
    golden_dir.mkdir()
    src_dir = REPO_ROOT / "evals" / "golden"
    for name in [f"golden-{i:03d}.json" for i in range(1, 12)]:
        (golden_dir / name).write_text(
            _json.dumps(_json.loads((src_dir / name).read_text(encoding="utf-8"))),
            encoding="utf-8",
        )

    fake = lambda prompt, grammar: _GET_BALANCE
    state = PG.PlaygroundState(generate_fn=fake, golden_dir=golden_dir)
    score = state.run_golden_report()
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip().startswith("golden-")]
    assert len(lines) == 11  # one row per fixture
    passing = {ln.split()[0] for ln in lines if ln.strip().endswith("PASS")}
    assert passing == {"golden-001", "golden-002", "golden-003", "golden-008", "golden-011"}
    assert score == pytest.approx(5 / 11)
    assert "SUMMARY: 5/11 passed (45.5%)" in out

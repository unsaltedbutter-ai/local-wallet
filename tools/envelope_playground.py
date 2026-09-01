#!/usr/bin/env python3
"""Envelope playground — local debugging/QA web tool (TCK-P0-009).

Type a hypothetical user statement, submit it, and the tool prints the raw
JSON the LLM generated plus the validation verdict — a way to "get a feel"
for what the model can do and to probe intents that don't exist yet
(future-spec input).

Design constraints (all non-negotiable):

- **Display-only.** One statement → ONE generation → ``handle_raw`` against
  a pure *stub* dispatch table — ``respond``/``clarify`` passthrough plus
  ``get_balance``/``get_history``/``get_utxos``/``new_address`` echo
  handlers that return ``{"echo": True}`` canned results. There is NO real
  dispatch, NO chain import, and NO persistence anywhere in this file. It
  probes what the model emits; it never executes anything.
- **Local only.** The server binds ``127.0.0.1`` (hardcoded + asserted);
  default port 8086, overridable via ``--port`` and
  ``LOCALWALLET_PLAYGROUND_PORT``.
- **No new dependencies.** stdlib ``http.server`` only (``httpx`` is used
  only inside the ADR-0007 excepted ``remote_runtime`` module, which we
  merely import — never in this file itself).
- **Untrusted model output is escaped.** Every dynamic string rendered into
  the HTML page passes through :func:`escape_html`; the raw model output is
  only ever shown via ``textContent`` in the page's JS (no ``innerHTML``).
- **Privacy.** The page and every API response carry a notice; when the
  ADR-0007 remote bridge is active the page header shows it prominently
  (chat text leaves this machine → host:port).

Runtime selection mirrors ``app.py`` precedence and reuses
``evals/run_evals.py:select_runtime`` (the canonical implementation): remote
bridge via ``LOCALWALLET_LLM_BASE_URL``, else local GGUF via
``LOCALWALLET_MODEL_PATH``, else ``app.stub_generate``. The env-overridable
:class:`PlaygroundState` also accepts an explicit ``generate_fn`` test
override (``None`` = environment selection).

Usage::

    python tools/envelope_playground.py [--port 8086] [--golden]
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final

# --- sys.path shim (mirrors evals/run_evals.py) ----------------------------
# Let both entrypoints (script and ``python -m``) resolve ``localwallet``
# from the repo's src/ regardless of the working directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
# ``evals`` is not a package (no __init__.py), so add the directory itself
# and import ``run_evals`` — its canonical ``select_runtime`` drives our
# env-based runtime selection exactly as the eval runner does.
_EVALS_DIR = _REPO_ROOT / "evals"
if str(_EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(_EVALS_DIR))

from localwallet.agent.runtime import load_grammar_text
from localwallet.protocol import (
    IntentName,
    Outcome,
    OutcomeStatus,
    handle_raw,
)

#: Loopback only — hardcoded and asserted; binding is non-negotiable.
PLAYGROUND_HOST: Final[str] = "127.0.0.1"
#: Default TCP port (overridden by ``--port``, then ``LOCALWALLET_PLAYGROUND_PORT``).
DEFAULT_PORT: Final[int] = 8086
#: Environment variable overriding the default port.
PORT_ENV_VAR: Final[str] = "LOCALWALLET_PLAYGROUND_PORT"
#: Maximum accepted ``text`` length in a POST body, in characters.
MAX_TEXT_CHARS: Final[int] = 10_000

#: Notice shown whenever the local/stub runtime is active (nothing remote).
LOCAL_NOTICE: Final[str] = "local/stub runtime — nothing leaves this machine"

#: Pure stub dispatch table: NO real handlers, NO chain. Each handler just
#: echoes a sentinel so ``handle_raw`` has an ``ok`` path; the playground
#: reports the *envelope* the model emitted, never handler side effects.
def _stub_handler(envelope) -> dict[str, object]:
    del envelope  # stub ignores the envelope; report comes from Outcome
    return {"echo": True}


STUB_TABLE: Final[dict[IntentName, object]] = {
    IntentName.RESPOND: _stub_handler,
    IntentName.CLARIFY: _stub_handler,
    IntentName.GET_BALANCE: _stub_handler,
    IntentName.GET_HISTORY: _stub_handler,
    IntentName.GET_UTXOS: _stub_handler,
    IntentName.NEW_ADDRESS: _stub_handler,
}


def escape_html(value: object) -> str:
    """Escape ``value`` for safe injection into the inline HTML page.

    Model output and runtime notices are untrusted; this is correct practice
    even on localhost. ``quote=True`` also escapes ``'``/``"`` so values are
    safe inside quoted attributes.
    """
    return html.escape(str(value), quote=True)


def compose_prompt(user_text: str) -> str:
    """Assemble the single-turn prompt EXACTLY as ``AgentLoop`` does.

    keep-in-sync with ``src/localwallet/agent/loop.py::AgentLoop._build_prompt``
    evaluated with ``facts={}`` and empty history: the system prompt, the
    sanitized ``user:`` line, then the ``envelope:`` cue, joined by blank
    lines. (``render_facts({})`` yields an empty string, so no FACTS block
    is injected — this matches the loop exactly.)
    """
    from localwallet.agent.context import sanitize_tool_output
    from localwallet.agent.prompt import build_system_prompt

    parts: list[str] = [build_system_prompt()]
    parts.append(f"user: {sanitize_tool_output(user_text)}")
    parts.append("envelope:")
    return "\n\n".join(parts)


def _params_ok(expectation: dict[str, object], params: dict[str, object]) -> bool:
    """Structural check of validated envelope params vs a golden expectation.

    keep-in-sync with ``evals/run_evals.py::_params_ok``.
    """
    exact = expectation.get("params")
    if exact is not None:
        return params == exact
    if expectation.get("text_nonempty") is True:
        return bool(str(params.get("text", "")).strip())
    if expectation.get("question_nonempty") is True:
        return bool(str(params.get("question", "")).strip())
    return False


def _matches_expectation(outcome: Outcome, expectation: dict[str, object]) -> bool:
    """Whether a single-shot outcome satisfies a golden expectation.

    keep-in-sync with ``evals/run_evals.py::_matches_expectation`` (adapted
    from an ``AgentTurnResult`` to an ``Outcome``: the playground runs one
    generation, no retry loop, so there is no loop-level turn status).
    """
    if outcome.status is not OutcomeStatus.OK or outcome.envelope is None:
        return False
    if outcome.envelope.intent.value != expectation["intent"]:
        return False
    return _params_ok(expectation, outcome.envelope.params.model_dump())


class PlaygroundState:
    """Holds the selected generate callable + privacy notice + golden config.

    Args:
        generate_fn: Optional test override ``(prompt, grammar_text) -> str``.
            When ``None``, the runtime is selected from the environment,
            mirroring ``app.py`` precedence (see :meth:`_select_runtime`).
        golden_dir: Directory of golden fixtures for ``--golden`` (defaults
            to ``evals/golden``).
    """

    def __init__(
        self,
        generate_fn: object | None = None,
        *,
        golden_dir: Path | None = None,
    ) -> None:
        self._generate_fn = generate_fn
        self.runtime: object | None = None
        self.notice: str = LOCAL_NOTICE
        self.remote: bool = False
        self.golden_dir = golden_dir or (_REPO_ROOT / "evals" / "golden")
        self._select_runtime()

    # --------------------------------------------------- runtime selection

    def _select_runtime(self) -> None:
        """Pick the generate callable, mirroring ``app.py`` precedence.

        canonical selection lives in evals/run_evals.py — keep in sync.
        An explicit ``generate_fn`` override (test seam) wins outright.
        """
        if self._generate_fn is not None:
            self.runtime = self._generate_fn
            self.notice = LOCAL_NOTICE
            self.remote = False
            return
        # Reuse run_evals' canonical env selection (remote → model → None).
        import run_evals  # local import: evals/ is on sys.path (shim above)

        runtime, notice = run_evals.select_runtime(None)
        if runtime is not None:
            self.runtime = runtime
            self.remote = notice is not None
            self.notice = notice if notice is not None else LOCAL_NOTICE
            return
        # Fallback: the deterministic stub model (app.stub_generate). Imported
        # lazily so this file never pulls the chain module transitively at
        # import time.
        from localwallet.app import stub_generate

        self.runtime = stub_generate
        self.notice = LOCAL_NOTICE
        self.remote = False

    def _call_generate(self, prompt: str, grammar_text: str) -> str:
        """Invoke the selected runtime uniformly (``generate`` seam or callable)."""
        runtime = self.runtime
        if hasattr(runtime, "generate"):
            return runtime.generate(prompt, grammar_text)  # type: ignore[union-attr]
        return runtime(prompt, grammar_text)  # type: ignore[operator]

    # --------------------------------------------------------- single shot

    def single_shot(self, user_text: str) -> tuple[str, Outcome]:
        """Run ONE generation for ``user_text`` and validate it.

        No retry loop, no conversation history. Returns the raw model output
        verbatim plus the ``handle_raw`` :class:`Outcome` against
        :data:`STUB_TABLE`.

        Raises:
            Exception: any runtime failure propagates to the caller (the
                HTTP layer converts it to a 500 JSON body; the ``--golden``
                path lets it surface to stdout).
        """
        prompt = compose_prompt(user_text)
        raw = self._call_generate(prompt, load_grammar_text())
        outcome = handle_raw(raw, STUB_TABLE)
        return raw, outcome

    def serialize_outcome(self, raw: str, outcome: Outcome) -> dict[str, object]:
        """Package the API/display JSON for one raw output + outcome."""
        envelope = outcome.envelope
        params = envelope.params.model_dump() if envelope is not None else None
        intent = envelope.intent.value if envelope is not None else None
        error_detail = None
        if outcome.error is not None:
            error_detail = outcome.error.error.detail
        return {
            "raw": raw,
            "notice": self.notice,
            "outcome": {
                "status": outcome.status.value,
                "intent": intent,
                "params": params,
                "failures": list(outcome.failures),
                "error_detail": error_detail,
            },
        }

    # --------------------------------------------------------------- golden

    def run_golden_report(self) -> float:
        """Run every golden fixture once; print a PASS/FAIL table + score.

        Purely informational startup print (does not block serving). Returns
        the score (0.0–1.0).
        """
        cases: list[dict[str, object]] = []
        for path in sorted(self.golden_dir.glob("*.json")):
            with path.open(encoding="utf-8") as fh:
                cases.append(json.load(fh))

        passed = 0
        total = len(cases)
        print(f"{'case':<12} {'status':<10} {'intent':<14} verdict")
        print("-" * 56)
        for case in cases:
            case_id = case.get("id", "<no-id>")
            expectation = case["expectation"]
            _raw, outcome = self.single_shot(case["prompt"])
            matched = _matches_expectation(outcome, expectation)
            if matched:
                passed += 1
            intent = outcome.envelope.intent.value if outcome.envelope is not None else "-"
            print(
                f"{case_id!s:<12} {outcome.status.value:<10} "
                f"{intent:<14} {'PASS' if matched else 'FAIL'}"
            )
        score = passed / total if total else 0.0
        print()
        print(f"SUMMARY: {passed}/{total} passed ({score * 100:.1f}%)")
        return score


# ---------------------------------------------------------------- HTTP layer


def _page_html(state: PlaygroundState) -> str:
    """Render the single inline HTML page (no external assets, vanilla JS)."""
    header_notice = ""
    if state.remote:
        # Prominent page header when the ADR-0007 bridge is active.
        header_notice = (
            f'<div class="bridge" role="alert">&#9888; '
            f"{escape_html(state.notice)}"
            f"</div>"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>local-wallet envelope playground</title>
<style>
  body {{ font-family: ui-monospace, Menlo, Consolas, monospace; max-width: 860px;
          margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }}
  h1 {{ font-size: 1.3rem; }}
  .bridge {{ background: #fff3cd; border: 1px solid #e0b400; color: #5c4400;
             padding: .7rem; border-radius: 4px; margin: 1rem 0; }}
  .notice {{ color: #555; font-size: .9rem; margin: .5rem 0 1rem; }}
  input[type=text] {{ width: 100%; padding: .6rem; font-size: 1rem;
                     box-sizing: border-box; margin-bottom: .5rem; }}
  button {{ padding: .6rem 1.2rem; font-size: 1rem; cursor: pointer; }}
  pre {{ background: #f4f4f4; border: 1px solid #ddd; padding: 1rem;
         overflow-x: auto; white-space: pre-wrap; word-break: break-word; }}
  #err {{ color: #b00020; }}
</style>
</head>
<body>
{header_notice}
<h1>local-wallet — envelope playground</h1>
<p>Type a hypothetical user statement; the page shows the raw JSON the LLM
generated and the validation verdict. No real dispatch happens — this is a
display-only probe.</p>
<p class="notice">Privacy: {escape_html(state.notice)}</p>
<form id="f">
  <label for="text">Statement</label>
  <input type="text" id="text" name="text" placeholder="What's my balance?"
         autocomplete="off" maxlength="{MAX_TEXT_CHARS}">
  <button type="submit">Generate envelope</button>
</form>
<pre id="result">(nothing yet)</pre>
<p id="err"></p>
<script>
  const form = document.getElementById('f');
  const text = document.getElementById('text');
  const result = document.getElementById('result');
  const err = document.getElementById('err');
  form.addEventListener('submit', async (e) => {{
    e.preventDefault();
    err.textContent = '';
    const value = text.value;
    if (value.length > {MAX_TEXT_CHARS}) {{
      err.textContent = 'text too long (max {MAX_TEXT_CHARS} chars)';
      return;
    }}
    try {{
      const resp = await fetch('/api/envelope', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ text: value }}),
      }});
      const data = await resp.json();
      // Model output is untrusted: render via textContent, never innerHTML.
      result.textContent = JSON.stringify(data, null, 2);
    }} catch (ex) {{
      err.textContent = 'request failed: ' + String(ex);
    }}
  }});
</script>
</body>
</html>
"""


class _RequestHandler(BaseHTTPRequestHandler):
    """Serves the page and the ``/api/envelope`` single-shot endpoint."""

    server_version = "local-wallet-playground/0.1"

    # ------------------------------------------------------------- helpers

    @property
    def state(self) -> PlaygroundState:
        return self.server.state  # type: ignore[attr-defined]

    def _send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: str, status: int = 200) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _read_body(self) -> str:
        length = self.headers.get("Content-Length")
        if length is None:
            return ""
        try:
            n = int(length)
        except ValueError:
            return ""
        return self.rfile.read(max(n, 0)).decode("utf-8", errors="replace")

    def log_message(self, fmt: str, *args: object) -> None:
        """Suppress default stderr request logging (no logging by contract)."""
        del fmt, args

    # ---------------------------------------------------------------- verbs

    def do_GET(self) -> None:
        if self.path == "/":
            self._send_html(_page_html(self.state))
            return
        if self.path == "/api/envelope":
            self._route_405()
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        if self.path != "/api/envelope":
            self._send_json({"error": "not found"}, status=404)
            return
        try:
            data = json.loads(self._read_body() or "{}")
        except json.JSONDecodeError:
            self._send_json({"error": "request body must be JSON"}, status=400)
            return
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str):
            self._send_json({"error": "missing 'text' (a string) in request body"}, status=400)
            return
        if len(text) > MAX_TEXT_CHARS:
            self._send_json(
                {"error": f"text too long: >{MAX_TEXT_CHARS} characters"}, status=400
            )
            return
        try:
            raw, outcome = self.state.single_shot(text)
        except Exception as exc:  # noqa: BLE001 - no traceback leak; 500 JSON body
            self._send_json(
                {"error": "internal_error", "detail": type(exc).__name__}, status=500
            )
            return
        self._send_json(self.state.serialize_outcome(raw, outcome), status=200)

    # 405 for unsupported verbs on existing routes (e.g. GET /api/envelope).
    def _route_405(self) -> None:
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.send_header("Content-Length", "0")
        self.end_headers()


class PlaygroundHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server holding the shared :class:`PlaygroundState`."""

    daemon_threads = True
    state: PlaygroundState


def create_server(state: PlaygroundState, port: int = 0) -> PlaygroundHTTPServer:
    """Create (and bind) a playground server on loopback.

    ``port=0`` asks the OS for an ephemeral port (test seam); the caller can
    read the actual port from ``server.server_address``. Binding is
    hardcoded to :data:`PLAYGROUND_HOST` — loopback only, non-negotiable.
    """
    assert PLAYGROUND_HOST == "127.0.0.1", "playground must bind loopback only"
    server = PlaygroundHTTPServer((PLAYGROUND_HOST, port), _RequestHandler)
    server.state = state
    return server


def _resolve_port(cli_port: int | None) -> int:
    """Port precedence: ``--port`` → ``LOCALWALLET_PLAYGROUND_PORT`` → default."""
    if cli_port is not None:
        return cli_port
    env = os.environ.get(PORT_ENV_VAR, "").strip()
    if env:
        return int(env)
    return DEFAULT_PORT


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="envelope_playground",
        description=(
            "Local-only envelope playground: type a hypothetical statement "
            "and see the raw LLM output + validation verdict."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"TCP port (default {DEFAULT_PORT}; overrides {PORT_ENV_VAR})",
    )
    parser.add_argument(
        "--golden",
        action="store_true",
        help=(
            "before serving, run every evals/golden fixture once and print "
            "a PASS/FAIL table + score (informational; does not block serving)"
        ),
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: list[str] | None = None) -> int:
    """Entry point: run ``--golden`` (if requested) then serve until Ctrl-C."""
    args = _parse_args(argv)
    state = PlaygroundState()
    port = _resolve_port(args.port)

    if args.golden:
        print("GOLDEN RUN (single-shot, informational)")
        state.run_golden_report()
        print()

    server = create_server(state, port)
    bound_port = server.server_address[1]
    print(f"Playground on http://{PLAYGROUND_HOST}:{bound_port} (Ctrl-C to quit)")
    print(state.notice)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass  # clean shutdown on Ctrl-C
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

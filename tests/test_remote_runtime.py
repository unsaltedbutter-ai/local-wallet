"""Tests for the TEMPORARY remote OpenAI-compatible runtime (ADR-0007).

All tests inject the ``transport`` seam — no live network in the default
run. The single live smoke test at the bottom is gated behind
``LOCALWALLET_LLM_LIVE=1`` (mirrors the ``LOCALWALLET_E2E_LIVE`` pattern in
``test_e2e_skeleton.py``) and is a plumbing smoke only: per ADR-0007, no
eval conclusions may be drawn from this endpoint.

Covered:

- happy path: content returned (stripped), exact method/URL/payload fields;
- grammar: sent as top-level ``grammar`` when provided; any 400 (or a 4xx
  whose body mentions "grammar") triggers exactly ONE downgrade retry and
  the downgrade is remembered on the instance for subsequent calls;
- API key header present only when a non-empty key resolves;
- 5xx / timeout / connection error → ``ModelRuntimeError`` (no retries);
- scrubbing: no raised message ever echoes the prompt, API key, or grammar;
- missing/empty choices and malformed response shapes fail closed;
- env-default resolution at call time (and explicit args winning over env).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Final

import httpx
import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.agent.loop import AgentLoop, AgentTurnStatus
from localwallet.agent.prompt import build_system_prompt
from localwallet.agent.remote_runtime import (
    DEFAULT_TIMEOUT_S,
    LLM_API_KEY_ENV_VAR,
    LLM_BASE_URL_ENV_VAR,
    LLM_MODEL_ENV_VAR,
    LLM_TIMEOUT_ENV_VAR,
    RemoteOpenAIRuntime,
    debug_notice,
    host_only,
)
from localwallet.agent.runtime import MODEL_PATH_ENV_VAR, ModelRuntimeError
from localwallet.protocol import DispatchTable, IntentName

# ---------------------------------------------------------------- constants

BASE_URL = "http://notible.local:8083/v1"
MODEL_ID = "mlx-community/gemma-4-26b-a4b-it-mxfp8"
EXPECTED_URL = f"{BASE_URL}/chat/completions"
PROMPT = "SECRET-PROMPT-FRAGMENT What's my balance?"
GRAMMAR = "root ::= envelope // SECRET-GRAMMAR-FRAGMENT"
API_KEY = "SUPERSECRET-KEY-123"
GET_BALANCE_JSON = '{"v": 0, "intent": "get_balance", "params": {}}'

_LLM_ENV_VARS = (LLM_BASE_URL_ENV_VAR, LLM_MODEL_ENV_VAR, LLM_API_KEY_ENV_VAR, LLM_TIMEOUT_ENV_VAR)


# ---------------------------------------------------------------- helpers


def chat_response(content: str) -> dict[str, object]:
    """Minimal OpenAI-shaped success payload."""
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class FakeTransport:
    """Transport seam stub: scripted ``(status, payload)`` answers.

    Entries may be exceptions (raised instead of returned) to simulate
    network-level failures. Records every call for assertions.
    """

    def __init__(self, responses: list[tuple[int, object | None] | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str], dict[str, object]]] = []

    def __call__(
        self, method: str, url: str, headers: dict[str, str], json_body: dict[str, object]
    ) -> tuple[int, object | None]:
        self.calls.append((method, url, dict(headers), json_body))
        item = self.responses.pop(0) if self.responses else (500, None)
        if isinstance(item, Exception):
            raise item
        return item


def make_runtime(
    responses: list[tuple[int, object | None] | Exception],
    *,
    api_key: str | None = None,
) -> tuple[RemoteOpenAIRuntime, FakeTransport]:
    """Runtime wired to a scripted fake transport (no live network)."""
    transport = FakeTransport(responses)
    runtime = RemoteOpenAIRuntime(
        base_url=BASE_URL,
        model=MODEL_ID,
        api_key=api_key,
        transport=transport,
    )
    return runtime, transport


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all LOCALWALLET_LLM_* env vars so tests are hermetic."""
    for name in _LLM_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# ------------------------------------------------------------- happy path


def test_happy_path_returns_stripped_content_and_correct_request() -> None:
    runtime, transport = make_runtime(
        [(200, chat_response(f'  {GET_BALANCE_JSON} \n'))]
    )
    out = runtime.generate(PROMPT, GRAMMAR)
    assert out == GET_BALANCE_JSON  # only transformation is .strip()

    assert len(transport.calls) == 1
    method, url, headers, body = transport.calls[0]
    assert method == "POST"
    assert url == EXPECTED_URL
    assert headers["Content-Type"] == "application/json"
    assert "Authorization" not in headers  # no key configured
    assert body["model"] == MODEL_ID
    assert body["temperature"] == 1.0
    assert body["top_p"] == 0.95
    assert body["messages"] == [{"role": "user", "content": PROMPT}]
    assert body["grammar"] == GRAMMAR  # attempted when provided


def test_no_grammar_field_when_grammar_text_is_none() -> None:
    runtime, transport = make_runtime([(200, chat_response(GET_BALANCE_JSON))])
    runtime.generate(PROMPT)
    _method, _url, _headers, body = transport.calls[0]
    assert "grammar" not in body


def test_agent_loop_seam_integration_uses_remote_runtime() -> None:
    """RemoteOpenAIRuntime slots into AgentLoop exactly like a GenerateFn.

    AgentLoop wraps it (it is not a ModelRuntime) and calls it positionally
    with the real envelope grammar — pinning the seam contract.
    """
    runtime, transport = make_runtime([(200, chat_response(GET_BALANCE_JSON))])
    table: DispatchTable = {
        IntentName.RESPOND: lambda envelope: {"ok": True},
        IntentName.CLARIFY: lambda envelope: {"ok": True},
        IntentName.GET_BALANCE: lambda envelope: {"ok": True},
    }
    loop = AgentLoop(runtime, table)
    turn = loop.run("What's my balance?", {})
    assert turn.status is AgentTurnStatus.OK
    assert turn.result == {"ok": True}
    method, url, _headers, body = transport.calls[0]
    assert (method, url) == ("POST", EXPECTED_URL)
    assert isinstance(body["grammar"], str) and body["grammar"]  # real grammar text


# ------------------------------------------------- grammar downgrade logic


def test_400_with_grammar_downgrades_once_then_succeeds() -> None:
    runtime, transport = make_runtime(
        [(400, {"error": "unknown field"}), (200, chat_response(GET_BALANCE_JSON))]
    )
    out = runtime.generate(PROMPT, GRAMMAR)
    assert out == GET_BALANCE_JSON
    assert len(transport.calls) == 2
    assert "grammar" in transport.calls[0][3]
    assert "grammar" not in transport.calls[1][3]


def test_downgrade_is_remembered_for_subsequent_calls() -> None:
    runtime, transport = make_runtime(
        [
            (400, {"error": "unknown field"}),
            (200, chat_response(GET_BALANCE_JSON)),
            (200, chat_response(GET_BALANCE_JSON)),  # next generate()
        ]
    )
    assert runtime.generate(PROMPT, GRAMMAR) == GET_BALANCE_JSON
    # Subsequent call: grammar is skipped entirely (instance flag), so the
    # request succeeds on the FIRST transport call.
    assert runtime.generate(PROMPT, GRAMMAR) == GET_BALANCE_JSON
    assert len(transport.calls) == 3
    assert "grammar" not in transport.calls[2][3]


def test_non_400_4xx_with_grammar_hint_downgrades() -> None:
    runtime, transport = make_runtime(
        [(422, {"error": "grammar is not a supported field"}), (200, chat_response(GET_BALANCE_JSON))]
    )
    assert runtime.generate(PROMPT, GRAMMAR) == GET_BALANCE_JSON
    assert len(transport.calls) == 2
    assert "grammar" not in transport.calls[1][3]


def test_4xx_without_grammar_hint_fails_without_retry() -> None:
    runtime, transport = make_runtime([(401, {"error": "bad credentials"})])
    with pytest.raises(ModelRuntimeError) as excinfo:
        runtime.generate(PROMPT, GRAMMAR)
    assert len(transport.calls) == 1  # no downgrade retry
    assert "401" in str(excinfo.value)


def test_400_without_grammar_attached_does_not_retry() -> None:
    """A 400 on a grammar-less request is a hard error (nothing to downgrade)."""
    runtime, transport = make_runtime([(400, {"error": "bad model"})])
    with pytest.raises(ModelRuntimeError):
        runtime.generate(PROMPT)
    assert len(transport.calls) == 1


# ------------------------------------------------------------- api key


def test_api_key_sent_as_bearer_header_when_set() -> None:
    runtime, transport = make_runtime(
        [(200, chat_response(GET_BALANCE_JSON))], api_key=f"  {API_KEY}  "
    )
    runtime.generate(PROMPT)
    headers = transport.calls[0][2]
    assert headers["Authorization"] == f"Bearer {API_KEY}"


def test_api_key_env_only_sent_when_non_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LLM_API_KEY_ENV_VAR, API_KEY)
    runtime, transport = make_runtime([(200, chat_response(GET_BALANCE_JSON))])
    runtime.generate(PROMPT)
    assert transport.calls[0][2]["Authorization"] == f"Bearer {API_KEY}"


def test_no_authorization_header_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LLM_API_KEY_ENV_VAR, "")  # empty env → no key
    runtime, transport = make_runtime([(200, chat_response(GET_BALANCE_JSON))])
    runtime.generate(PROMPT)
    assert "Authorization" not in transport.calls[0][2]


# ------------------------------------------------------- failure surfaces


def test_500_fails_closed_without_retry() -> None:
    runtime, transport = make_runtime([(500, {"error": "boom"})])
    with pytest.raises(ModelRuntimeError) as excinfo:
        runtime.generate(PROMPT, GRAMMAR)
    assert len(transport.calls) == 1  # no transient retries
    assert "status 500" in str(excinfo.value)
    assert host_only(EXPECTED_URL) in str(excinfo.value)
    assert "boom" not in str(excinfo.value)  # response-body token scrubbed


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("timed out while reading"),
        httpx.ConnectError("connection refused details here"),
    ],
    ids=["timeout", "connect-error"],
)
def test_transport_errors_become_model_runtime_errors(exc: Exception) -> None:
    runtime, transport = make_runtime([exc])
    with pytest.raises(ModelRuntimeError) as excinfo:
        runtime.generate(PROMPT)
    assert len(transport.calls) == 1
    message = str(excinfo.value)
    assert type(exc).__name__ in message  # class name allowed
    assert host_only(EXPECTED_URL) in message  # host allowed
    assert "timed out while reading" not in message  # exception text scrubbed
    assert "connection refused details here" not in message


def test_malformed_response_shapes_fail_closed() -> None:
    cases: list[tuple[int, object | None]] = [
        (200, None),  # unparseable body
        (200, ["not", "an", "object"]),
        (200, {"choices": []}),  # empty choices
        (200, {"nope": True}),  # missing choices
        (200, {"choices": ["not-an-object"]}),
        (200, {"choices": [{"message": None}]}),
        (200, {"choices": [{"message": {"content": None}}]}),
        (200, {"choices": [{"message": {"content": 42}}]}),
    ]
    for status, payload in cases:
        runtime, _transport = make_runtime([(status, payload)])
        with pytest.raises(ModelRuntimeError):
            runtime.generate(PROMPT)


# ------------------------------------------------------------- scrubbing


@pytest.mark.parametrize(
    "responses",
    [
        [(500, {"error": "boom"})],
        [httpx.ConnectTimeout("conn")],
        [(400, {"error": "unknown field"}), (400, {"error": "still bad"})],
        [(200, {"choices": []})],
    ],
    ids=["http-500", "timeout", "grammar-downgrade-then-400", "empty-choices"],
)
def test_error_messages_never_echo_prompt_key_or_grammar(responses) -> None:
    runtime, _transport = make_runtime(responses, api_key=API_KEY)
    with pytest.raises(ModelRuntimeError) as excinfo:
        runtime.generate(PROMPT, GRAMMAR)
    message = str(excinfo.value)
    assert PROMPT not in message
    assert "SECRET-PROMPT-FRAGMENT" not in message
    assert API_KEY not in message
    assert "SECRET-GRAMMAR-FRAGMENT" not in message


def test_debug_notice_discloses_host_and_model_but_not_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LLM_API_KEY_ENV_VAR, API_KEY)
    notice = debug_notice(BASE_URL, MODEL_ID)
    assert notice == (
        f"DEBUG: using remote LLM notible.local:8083 ({MODEL_ID}) — "
        "chat text leaves this machine."
    )
    assert API_KEY not in notice


# ---------------------------------------------------- env-default resolution


def test_env_defaults_resolved_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LLM_BASE_URL_ENV_VAR, BASE_URL)
    monkeypatch.setenv(LLM_MODEL_ENV_VAR, MODEL_ID)
    monkeypatch.setenv(LLM_API_KEY_ENV_VAR, API_KEY)
    monkeypatch.setenv(LLM_TIMEOUT_ENV_VAR, "7.5")
    transport = FakeTransport([(200, chat_response(GET_BALANCE_JSON))])
    runtime = RemoteOpenAIRuntime(transport=transport)  # nothing explicit — all from env
    assert runtime.generate(PROMPT) == GET_BALANCE_JSON
    assert transport.calls[0][1] == EXPECTED_URL
    assert transport.calls[0][3]["model"] == MODEL_ID
    assert transport.calls[0][2]["Authorization"] == f"Bearer {API_KEY}"
    assert runtime.resolve_timeout_s() == 7.5


def test_explicit_args_win_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LLM_BASE_URL_ENV_VAR, "http://env-host:1/v1")
    monkeypatch.setenv(LLM_MODEL_ENV_VAR, "env-model")
    monkeypatch.setenv(LLM_TIMEOUT_ENV_VAR, "3")
    runtime = RemoteOpenAIRuntime(
        base_url=BASE_URL, model=MODEL_ID, timeout_s=42.0
    )
    assert runtime.resolve_base_url() == BASE_URL
    assert runtime.resolve_model() == MODEL_ID
    assert runtime.resolve_timeout_s() == 42.0


def test_default_timeout_without_env_or_arg() -> None:
    assert RemoteOpenAIRuntime().resolve_timeout_s() == DEFAULT_TIMEOUT_S == 120.0


def test_missing_base_url_and_model_fail_with_config_messages() -> None:
    runtime = RemoteOpenAIRuntime(transport=FakeTransport([]))
    with pytest.raises(ModelRuntimeError) as excinfo:
        runtime.generate(PROMPT)
    assert LLM_BASE_URL_ENV_VAR in str(excinfo.value)

    runtime2 = RemoteOpenAIRuntime(base_url=BASE_URL, transport=FakeTransport([]))
    with pytest.raises(ModelRuntimeError) as excinfo2:
        runtime2.generate(PROMPT)
    assert LLM_MODEL_ENV_VAR in str(excinfo2.value)


def test_invalid_timeout_env_var_named_but_value_not_echoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LLM_TIMEOUT_ENV_VAR, "garbage-timeout-value")
    with pytest.raises(ModelRuntimeError) as excinfo:
        RemoteOpenAIRuntime().resolve_timeout_s()
    assert LLM_TIMEOUT_ENV_VAR in str(excinfo.value)
    assert "garbage-timeout-value" not in str(excinfo.value)


# ------------------------------------------------- app.py runtime selection


# Canonical mainnet fixture zpub — the SAME constant as
# tests/test_e2e_skeleton.py::ZPUB / tests/test_wallet_descriptor.py::ZPUB
# (one fixed seed across the suite; public key only — not a secret).
FIXTURE_ZPUB: Final[str] = (
    "zpub6qh6bF4roUgQtg2fm5SUhRsQFEidwUPPLhS82BDHjtNh2UxmgNfCS8NF4jQoBqNCeEW"
    "BaKyTxcmyBkq3iuZS5Seyz5dWMcwYxaMgpZn4cWQ"
)


def _run_app(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> list[str]:
    """Run app.run() to a clean 'exit' and return the captured output lines.

    Phase 1 app wiring: the app now opens a SQLite store and (by default)
    scans at startup — point the store at a temp file and opt out of the
    scan so these runtime-selection tests stay hermetic (no disk side
    effects in the repo, no chain I/O). Assertions are unchanged.
    """
    import tempfile

    from localwallet import app

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(Path(tmp) / "store.db"))
        monkeypatch.setenv("LOCALWALLET_AUTO_SCAN", "0")
        outputs: list[str] = []
        code = app.run(
            argv,
            input_fn=lambda _prompt: "exit",
            output_fn=outputs.append,
        )
    assert code == 0
    return outputs


def test_app_selects_remote_runtime_and_prints_disclosure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LLM_BASE_URL_ENV_VAR, BASE_URL)
    monkeypatch.setenv(LLM_MODEL_ENV_VAR, MODEL_ID)
    monkeypatch.setenv(LLM_API_KEY_ENV_VAR, API_KEY)
    outputs = _run_app(monkeypatch, ["--zpub", FIXTURE_ZPUB])
    notice_lines = [line for line in outputs if line.startswith("DEBUG: using remote LLM")]
    assert len(notice_lines) == 1
    assert "notible.local:8083" in notice_lines[0]
    assert MODEL_ID in notice_lines[0]
    assert "chat text leaves this machine" in notice_lines[0]
    assert API_KEY not in "\n".join(outputs)  # never the key


def test_app_remote_env_wins_over_model_path_and_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LLM_BASE_URL_ENV_VAR, BASE_URL)
    monkeypatch.setenv(LLM_MODEL_ENV_VAR, MODEL_ID)
    monkeypatch.setenv(MODEL_PATH_ENV_VAR, "/some/model.gguf")
    outputs = _run_app(monkeypatch, ["--stub-llm", "--zpub", FIXTURE_ZPUB])
    assert any(line.startswith("DEBUG: using remote LLM") for line in outputs)


def test_app_never_selects_remote_runtime_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = _run_app(monkeypatch, ["--stub-llm", "--zpub", FIXTURE_ZPUB])
    assert not any("DEBUG: using remote LLM" in line for line in outputs)


# --------------------------------------------- pre-flight: remote env w/o model


def test_app_preflight_remote_env_without_model_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """LOCALWALLET_LLM_BASE_URL set but no model → config error on stderr, exit 2."""
    from localwallet import app

    monkeypatch.setenv(LLM_BASE_URL_ENV_VAR, BASE_URL)
    # LLM_MODEL deliberately left unset.
    code = app.run(
        ["--zpub", FIXTURE_ZPUB],
        input_fn=lambda _prompt: "exit",
        output_fn=lambda _s: None,
    )
    assert code == 2
    err = capsys.readouterr().err
    assert LLM_MODEL_ENV_VAR in err
    assert LLM_BASE_URL_ENV_VAR in err


def test_app_preflight_remote_env_with_model_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOCALWALLET_LLM_BASE_URL + LOCALWALLET_LLM_MODEL → runtime selected."""
    monkeypatch.setenv(LLM_BASE_URL_ENV_VAR, BASE_URL)
    monkeypatch.setenv(LLM_MODEL_ENV_VAR, MODEL_ID)
    outputs = _run_app(monkeypatch, ["--zpub", FIXTURE_ZPUB])
    assert any(line.startswith("DEBUG: using remote LLM") for line in outputs)


# ------------------------------------------------------------- live smoke


LIVE_BASE_URL = "http://notible.local:8083/v1"
LIVE_MODEL = "mlx-community/gemma-4-26b-a4b-it-mxfp8"


@pytest.mark.network
@pytest.mark.skipif(
    os.environ.get("LOCALWALLET_LLM_LIVE") != "1",
    reason="live remote-LLM smoke: set LOCALWALLET_LLM_LIVE=1 to include",
)
def test_live_smoke_remote_llm_plumbing() -> None:
    """Live smoke against the ADR-0007 debug bridge (plumbing check ONLY).

    Per ADR-0007 this proves reachability + response shape; it is NOT an
    eval signal (the debug model is a different capability class than the
    pinned E2B). Grammar field is omitted (plain OpenAI request).
    """
    runtime = RemoteOpenAIRuntime(base_url=LIVE_BASE_URL, model=LIVE_MODEL)
    prompt = build_system_prompt() + "What's my balance?"
    out = runtime.generate(prompt)
    assert isinstance(out, str)
    assert out.strip()

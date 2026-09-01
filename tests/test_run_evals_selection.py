"""Tests for ``evals/run_evals.py`` --model runtime selection (TCK-P0-008).

Pins that model-mode selection mirrors ``app.py``: remote debug bridge when
``LOCALWALLET_LLM_BASE_URL`` is set (ADR-0007), local GGUF via model path
otherwise, exit 2 when neither is configured. The remote case uses a fake
transport — no live network.
"""

from __future__ import annotations

import collections
import importlib.util
import sys
from pathlib import Path

import pytest

from localwallet.agent.loop import AgentTurnResult, AgentTurnStatus
from localwallet.agent.remote_runtime import (
    LLM_BASE_URL_ENV_VAR,
    LLM_MODEL_ENV_VAR,
    RemoteOpenAIRuntime,
)
from localwallet.agent.runtime import MODEL_PATH_ENV_VAR, ModelRuntime
from localwallet.protocol import validate_payload

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_EVALS_PATH = REPO_ROOT / "evals" / "run_evals.py"


def _load_run_evals():
    spec = importlib.util.spec_from_file_location("run_evals_module", RUN_EVALS_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUN_EVALS = _load_run_evals()

BASE_URL = "http://notible.local:8083/v1"
MODEL_ID = "mlx-community/gemma-4-26b-a4b-it-mxfp8"


class _FakeTransport:
    """Records the single request; returns a canned OpenAI-shaped answer."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str], dict[str, object]]] = []

    def __call__(
        self, method: str, url: str, headers: dict[str, str], json_body: dict[str, object]
    ) -> tuple[int, object | None]:
        self.calls.append((method, url, dict(headers), json_body))
        return 200, {"choices": [{"message": {"content": "{}"}}]}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic env: no remote endpoint, no model path by default."""
    monkeypatch.delenv(LLM_BASE_URL_ENV_VAR, raising=False)
    monkeypatch.delenv(LLM_MODEL_ENV_VAR, raising=False)
    monkeypatch.delenv(MODEL_PATH_ENV_VAR, raising=False)


def test_remote_env_selects_remote_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LLM_BASE_URL_ENV_VAR, BASE_URL)
    monkeypatch.setenv(LLM_MODEL_ENV_VAR, MODEL_ID)
    monkeypatch.setenv("LOCALWALLET_LLM_API_KEY", "SECRET-KEY-XYZ")
    transport = _FakeTransport()

    runtime, notice = RUN_EVALS.select_runtime(None, transport=transport)
    assert isinstance(runtime, RemoteOpenAIRuntime)
    # One-line disclosure on stderr: host + model only, never the API key.
    assert notice is not None
    assert "DEBUG: using remote LLM notible.local:8083" in notice
    assert MODEL_ID in notice
    assert "chat text leaves this machine" in notice
    assert "SECRET-KEY-XYZ" not in notice

    # The selected runtime is live: one generate through the fake transport.
    assert runtime.generate("prompt", None) == "{}"
    assert transport.calls[0][1] == f"{BASE_URL}/chat/completions"


def test_remote_env_takes_precedence_over_model_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LLM_BASE_URL_ENV_VAR, BASE_URL)
    monkeypatch.setenv(MODEL_PATH_ENV_VAR, "/some/model.gguf")
    runtime, _notice = RUN_EVALS.select_runtime("/cli/model.gguf")
    assert isinstance(runtime, RemoteOpenAIRuntime)  # remote wins, mirrors app.py


def test_model_path_selects_model_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, notice = RUN_EVALS.select_runtime("/tmp/fake.gguf")
    assert isinstance(runtime, ModelRuntime)
    assert notice is None
    assert runtime.resolve_model_path() == "/tmp/fake.gguf"


def test_model_path_env_selects_model_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODEL_PATH_ENV_VAR, "/env/model.gguf")
    runtime, _notice = RUN_EVALS.select_runtime(None)
    assert isinstance(runtime, ModelRuntime)
    assert runtime.resolve_model_path() == "/env/model.gguf"


def test_neither_configured_selects_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, notice = RUN_EVALS.select_runtime(None)
    assert runtime is None
    assert notice is None


def test_main_model_mode_without_config_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = RUN_EVALS.main(["--model"])
    assert code == 2
    err = capsys.readouterr().err
    assert "--model requires a model path" in err
    assert MODEL_PATH_ENV_VAR in err


def test_fixture_mode_default_exit_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """Fixture mode is unchanged by the selection rework (exit 0)."""
    code = RUN_EVALS.main([])
    out = capsys.readouterr().out
    assert code == 0
    assert "FIXTURE MODE" in out
    assert "RESULT: all fixtures valid" in out


def test_version_guard_rejects_old_python(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """On Python < 3.12 the guard exits 2 with a clear message, not a SyntaxError."""
    # In production the guard runs at import time; here we call the factored
    # function with sys.version_info faked to an old interpreter.
    # ``sys.version_info`` cannot be instantiated directly, so fake a
    # compatible namedtuple exposing major/minor/micro and tuple ordering.
    _OldVersion = collections.namedtuple("_OldVersion", "major minor micro")
    monkeypatch.setattr(RUN_EVALS.sys, "version_info", _OldVersion(3, 11, 0))
    with pytest.raises(SystemExit) as excinfo:
        RUN_EVALS.ensure_python_version()
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "requires Python 3.12+" in err
    assert "3.11.0" in err
    assert "Hint: python3 -m venv .venv" in err


# ------------------------------------------- matcher: intent_in + params_if_*


def _result_for(envelope_json: str) -> AgentTurnResult:
    """A dispatchable agent turn wrapping a validated envelope."""
    env = validate_payload(envelope_json)
    return AgentTurnResult(
        status=AgentTurnStatus.OK,
        envelope=env,
        result={"ok": True},
        user_message=None,
        turns_used=1,
    )


class TestIntentInMatcher:
    """Pins the extended matcher: ``intent_in`` sets + ``params_if_<intent>``.

    These drive the golden-014 (out-of-range limit) and golden-019
    (parallel-intent boundary) fixtures. A matched intent passes when its
    ``params_if_<intent>`` object (if any) equals the emitted params exactly;
    an intent listed with no ``params_if_`` is free on params.
    """

    def test_exact_intent_still_matches(self) -> None:
        res = _result_for('{"v":0,"intent":"get_history","params":{}}')
        assert RUN_EVALS._matches_expectation(
            res, {"intent": "get_history", "params": {}}
        )
        assert not RUN_EVALS._matches_expectation(
            res, {"intent": "get_utxos", "params": {}}
        )

    def test_intent_in_get_history_hit(self) -> None:
        res = _result_for('{"v":0,"intent":"get_history","params":{}}')
        expectation = {"intent_in": ["clarify", "get_history"], "params_if_get_history": {}}
        assert RUN_EVALS._matches_expectation(res, expectation)

    def test_intent_in_clarify_hit_without_params_if(self) -> None:
        res = _result_for('{"v":0,"intent":"clarify","params":{"question":"how many?"}}')
        expectation = {"intent_in": ["clarify", "get_history"], "params_if_get_history": {}}
        assert RUN_EVALS._matches_expectation(res, expectation)

    def test_intent_in_params_if_exact_hit(self) -> None:
        res = _result_for('{"v":0,"intent":"get_history","params":{"limit":5}}')
        expectation = {
            "intent_in": ["clarify", "get_history"],
            "params_if_get_history": {"limit": 5},
        }
        assert RUN_EVALS._matches_expectation(res, expectation)

    def test_intent_in_params_if_mismatch_is_miss(self) -> None:
        # Emitting limit=5 violates the params_if_get_history={} expectation:
        # the contract allows only {} for get_history here (default 20) or a
        # clarify, not a non-default limit.
        res = _result_for('{"v":0,"intent":"get_history","params":{"limit":5}}')
        expectation = {"intent_in": ["clarify", "get_history"], "params_if_get_history": {}}
        assert not RUN_EVALS._matches_expectation(res, expectation)

    def test_intent_in_miss_when_intent_not_listed(self) -> None:
        res = _result_for('{"v":0,"intent":"get_balance","params":{}}')
        expectation = {"intent_in": ["new_address", "clarify"]}
        assert not RUN_EVALS._matches_expectation(res, expectation)

    def test_intent_in_matches_any_listed_intent(self) -> None:
        expectation = {"intent_in": ["new_address", "clarify"]}
        new = _result_for('{"v":0,"intent":"new_address","params":{}}')
        clarify = _result_for('{"v":0,"intent":"clarify","params":{"question":"which?"}}')
        assert RUN_EVALS._matches_expectation(new, expectation)
        assert RUN_EVALS._matches_expectation(clarify, expectation)

    def test_escalated_turn_is_never_a_match(self) -> None:
        # A clarified/escalated turn has no envelope: it cannot satisfy an
        # intent_in expectation even when the intent name is in the list.
        expectation = {"intent_in": ["clarify", "get_history"]}
        result = AgentTurnResult(
            status=AgentTurnStatus.CLARIFIED,
            envelope=None,
            result=None,
            user_message="rephrase",
            turns_used=2,
        )
        assert not RUN_EVALS._matches_expectation(result, expectation)


class TestParamsIfSchema:
    """Fixture-mode schema validation of ``intent_in`` + ``params_if_*``."""

    def test_expectation_variants_yield_valid_envelopes(self) -> None:
        expectation = {"intent_in": ["clarify", "get_history"], "params_if_get_history": {}}
        variants = list(RUN_EVALS._expectation_variants(expectation))
        assert [intent for intent, _ in variants] == ["clarify", "get_history"]
        # Each variant builds a schema-valid envelope (clarify needs a
        # question; get_history takes the params_if {} object).
        for intent, params in variants:
            env = validate_payload({"v": 0, "intent": intent, "params": params})
            assert env.intent.value == intent

    def test_intent_in_without_params_if_uses_representative_params(self) -> None:
        expectation = {"intent_in": ["new_address", "clarify"]}
        variants = dict(RUN_EVALS._expectation_variants(expectation))
        assert variants["new_address"] == {}
        assert "question" in variants["clarify"]
        for intent, params in variants.items():
            validate_payload({"v": 0, "intent": intent, "params": params})

    def test_malformed_intent_in_rejected(self) -> None:
        with pytest.raises(ValueError):
            list(RUN_EVALS._expectation_variants({"intent_in": []}))
        with pytest.raises(ValueError):
            list(RUN_EVALS._expectation_variants({"intent_in": ["not_an_intent"]}))
        with pytest.raises(ValueError):
            list(
                RUN_EVALS._expectation_variants(
                    {"intent_in": ["get_history"], "params_if_get_history": [1]}
                )
            )

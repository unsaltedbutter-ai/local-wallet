"""Tests for ``evals/run_evals.py`` --model runtime selection (TCK-P0-008).

Pins that model-mode selection mirrors ``app.py``: remote debug bridge when
``LOCALWALLET_LLM_BASE_URL`` is set (ADR-0007), local GGUF via model path
otherwise, exit 2 when neither is configured. The remote case uses a fake
transport — no live network.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from localwallet.agent.remote_runtime import (
    LLM_BASE_URL_ENV_VAR,
    LLM_MODEL_ENV_VAR,
    RemoteOpenAIRuntime,
)
from localwallet.agent.runtime import MODEL_PATH_ENV_VAR, ModelRuntime

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

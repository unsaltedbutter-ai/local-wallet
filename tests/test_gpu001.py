"""TCK-GPU-001 — GPU offload by default + the honest one-line launch verdict.

Root cause pinned here at the source: llama-cpp-python's ``Llama`` defaults
``n_gpu_layers=0`` (CPU) even in Metal/CUDA builds (verified against the
pinned 0.3.35 wheel), so the runtime must pass the value explicitly. Every
test is hermetic: the decision function and the config rung are pure; the
load path runs against a fake ``llama_cpp`` module injected into
``sys.modules`` (the TCK-LAUNCH-003 pattern). Nothing here loads the real
3GB GGUF or touches the GPU — the real-load smoke is a manual, out-of-suite
run recorded in the ticket report.
"""

from __future__ import annotations

import sys
import threading
import types
from pathlib import Path
from typing import Any

import pytest

from localwallet import app
from localwallet.agent import runtime as runtime_mod
from localwallet.agent.runtime import (
    CPU_NO_BACKEND_LINE,
    MODEL_PATH_ENV_VAR,
    ModelRuntime,
    ModelRuntimeError,
    gpu_fallback_line,
    gpu_offload_decision,
    llama_backends,
)
from localwallet.config import (
    N_GPU_LAYERS_ENV_VAR,
    Settings,
    n_gpu_layers_setting,
    resolve_n_gpu_layers,
)
from tests.test_e2e_skeleton import ZPUB

GPU_METAL_LINE: str = "inference: GPU (Metal) — all layers offloaded"
GPU_CUDA_LINE: str = "inference: GPU (CUDA) — all layers offloaded"
CPU_DISABLED_LINE: str = (
    f"inference: CPU — reason: disabled via {N_GPU_LAYERS_ENV_VAR}=0"
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No repo-root config.json, no model/GPU/remote env, throwaway store —
    every rung starts unset and the launch never touches the real model."""
    monkeypatch.setenv(
        "LOCALWALLET_CONFIG_PATH", str(tmp_path / "absent-config.json")
    )
    for var in (
        N_GPU_LAYERS_ENV_VAR,
        MODEL_PATH_ENV_VAR,
        app.ZPUB_ENV_VAR,
        app.UI_ENV_VAR,
        "LOCALWALLET_LLM_BASE_URL",
        "LOCALWALLET_LLM_MODEL",
        "LOCALWALLET_WATCH_INTERVAL_S",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(tmp_path / "gpu.db"))
    monkeypatch.setenv(app.AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")


# ------------------------------------------------------- decision function


def test_metal_backend_decides_full_offload() -> None:
    assert gpu_offload_decision(None, (True, False)) == (-1, GPU_METAL_LINE)


def test_cuda_backend_decides_full_offload() -> None:
    assert gpu_offload_decision(None, (False, True)) == (-1, GPU_CUDA_LINE)


def test_both_backends_name_metal() -> None:
    # Theoretical layout (a dylib set per backend); llama.cpp prefers Metal
    # on macOS — the line must match the platform that would actually run.
    assert gpu_offload_decision(None, (True, True)) == (-1, GPU_METAL_LINE)


def test_no_backend_decides_cpu_with_the_honest_reason() -> None:
    assert gpu_offload_decision(None, (False, False)) == (0, CPU_NO_BACKEND_LINE)


def test_missing_wheel_decides_cpu() -> None:
    assert gpu_offload_decision(None, None) == (0, CPU_NO_BACKEND_LINE)


def test_env_zero_forces_cpu_even_with_metal() -> None:
    assert gpu_offload_decision(0, (True, False)) == (0, CPU_DISABLED_LINE)


def test_env_partial_offload_line_carries_the_count() -> None:
    assert gpu_offload_decision(16, (True, False)) == (
        16,
        "inference: GPU (Metal) — partial: 16 layers (env)",
    )


def test_env_minus_one_is_explicit_full_offload() -> None:
    assert gpu_offload_decision(-1, (False, True)) == (-1, GPU_CUDA_LINE)


def test_env_rung_without_backend_is_silent_cpu() -> None:
    # A partial/all-offload request means nothing without a backend — the
    # line says WHY (no GPU backend), never a fake GPU claim.
    assert gpu_offload_decision(16, (False, False)) == (0, CPU_NO_BACKEND_LINE)


def test_fallback_line_is_error_class_only() -> None:
    class _Boom(Exception):
        """docstring"""

    line = gpu_fallback_line(_Boom("secret /Users/homer/model.gguf details"))
    assert line == "inference: CPU — reason: GPU load failed: _Boom"


# --------------------------------------------------------- config rung


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("", None), ("  ", None), ("-1", -1), ("0", 0),
     (" 16 ", 16), ("999", 999), ("-0", 0)],
)
def test_resolve_valid_rungs(raw: str | None, expected: int | None) -> None:
    assert resolve_n_gpu_layers(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "1000", "-2", "+3", "1_0", "١٦", "-", "8.5"])
def test_resolve_malformed_refuses_value_free(raw: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        resolve_n_gpu_layers(raw)
    # The refusal is ONE fixed string (names the env var + bounds) — no
    # malformed input can ride it out of the process. ("-" can't be
    # substring-checked: it is part of the fixed "-1" bound text.)
    assert str(excinfo.value) == (
        f"{N_GPU_LAYERS_ENV_VAR} must be -1 or an integer between 0 and 999"
    )


def test_from_env_startup_refusal_is_value_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(N_GPU_LAYERS_ENV_VAR, "banana")
    with pytest.raises(ValueError) as excinfo:
        Settings.from_env()
    assert "banana" not in str(excinfo.value)


def test_from_env_accepts_the_rung(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(N_GPU_LAYERS_ENV_VAR, "-1")
    assert Settings.from_env().n_gpu_layers == "-1"


def test_call_time_ladder_env_over_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text('{"n_gpu_layers": "16"}', encoding="utf-8")
    monkeypatch.setenv("LOCALWALLET_CONFIG_PATH", str(cfg))
    assert n_gpu_layers_setting() == 16  # file rung honored
    monkeypatch.setenv(N_GPU_LAYERS_ENV_VAR, "0")
    assert n_gpu_layers_setting() == 0  # env wins over file


def test_call_time_rung_malformed_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(N_GPU_LAYERS_ENV_VAR, "1000")
    with pytest.raises(ValueError):
        n_gpu_layers_setting()


# ------------------------------------------------------- runtime load path


def _fake_llama_module(ctor: type) -> types.ModuleType:
    mod = types.ModuleType("llama_cpp")

    class FakeGrammar:
        @classmethod
        def from_string(cls, _text: str) -> FakeGrammar:
            return cls()

    mod.Llama = ctor  # type: ignore[attr-defined]
    mod.LlamaGrammar = FakeGrammar  # type: ignore[attr-defined]
    return mod


@pytest.fixture()
def gguf(tmp_path: Path) -> str:
    path = tmp_path / "m.gguf"
    path.write_bytes(b"GGUF")
    return str(path)


def test_metal_load_passes_minus_one_and_emits_gpu_line(
    gguf: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[dict[str, Any]] = []

    class FakeLlama:
        def __init__(self, **kw: Any) -> None:
            built.append(kw)

    monkeypatch.setitem(sys.modules, "llama_cpp", _fake_llama_module(FakeLlama))
    monkeypatch.setattr(runtime_mod, "llama_backends", lambda: (True, False))
    lines: list[str] = []
    rt = ModelRuntime(model_path=gguf, decision_fn=lines.append)
    rt.load()
    assert built[0]["n_gpu_layers"] == -1  # the root-cause fix, pinned
    assert lines == [GPU_METAL_LINE]
    assert rt.inference_line == GPU_METAL_LINE


def test_env_zero_load_passes_cpu_and_disabled_line(
    gguf: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[dict[str, Any]] = []

    class FakeLlama:
        def __init__(self, **kw: Any) -> None:
            built.append(kw)

    monkeypatch.setitem(sys.modules, "llama_cpp", _fake_llama_module(FakeLlama))
    monkeypatch.setattr(runtime_mod, "llama_backends", lambda: (True, False))
    monkeypatch.setenv(N_GPU_LAYERS_ENV_VAR, "0")
    lines: list[str] = []
    ModelRuntime(model_path=gguf, decision_fn=lines.append).load()
    assert built[0]["n_gpu_layers"] == 0
    assert lines == [CPU_DISABLED_LINE]


def test_partial_env_passes_the_count(
    gguf: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[dict[str, Any]] = []

    class FakeLlama:
        def __init__(self, **kw: Any) -> None:
            built.append(kw)

    monkeypatch.setitem(sys.modules, "llama_cpp", _fake_llama_module(FakeLlama))
    monkeypatch.setattr(runtime_mod, "llama_backends", lambda: (False, True))
    monkeypatch.setenv(N_GPU_LAYERS_ENV_VAR, "16")
    lines: list[str] = []
    ModelRuntime(model_path=gguf, decision_fn=lines.append).load()
    assert built[0]["n_gpu_layers"] == 16
    assert lines == ["inference: GPU (CUDA) — partial: 16 layers (env)"]


def test_no_backend_load_passes_cpu(
    gguf: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[dict[str, Any]] = []

    class FakeLlama:
        def __init__(self, **kw: Any) -> None:
            built.append(kw)

    monkeypatch.setitem(sys.modules, "llama_cpp", _fake_llama_module(FakeLlama))
    monkeypatch.setattr(runtime_mod, "llama_backends", lambda: (False, False))
    lines: list[str] = []
    ModelRuntime(model_path=gguf, decision_fn=lines.append).load()
    assert built[0]["n_gpu_layers"] == 0
    assert lines == [CPU_NO_BACKEND_LINE]


def test_gpu_load_failure_retries_once_at_cpu_and_tells_the_truth(
    gguf: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[int] = []

    class FakeLlama:
        def __init__(self, **kw: Any) -> None:
            built.append(kw["n_gpu_layers"])
            if kw["n_gpu_layers"] != 0:
                # A driver/Metal edge — the message carries junk; only the
                # CLASS NAME may ever reach a line.
                raise RuntimeError("boom /Users/homer/private.gguf")

    monkeypatch.setitem(sys.modules, "llama_cpp", _fake_llama_module(FakeLlama))
    monkeypatch.setattr(runtime_mod, "llama_backends", lambda: (True, False))
    lines: list[str] = []
    rt = ModelRuntime(model_path=gguf, decision_fn=lines.append)
    rt.load()
    assert built == [-1, 0]  # ONE bounded retry, at CPU
    assert lines == ["inference: CPU — reason: GPU load failed: RuntimeError"]
    assert "boom" not in lines[0] and "homer" not in lines[0]  # value-free


def test_cpu_load_failure_is_not_retried_or_silenced(
    gguf: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[int] = []

    class FakeLlama:
        def __init__(self, **kw: Any) -> None:
            built.append(kw["n_gpu_layers"])
            raise OSError("no such file")

    monkeypatch.setitem(sys.modules, "llama_cpp", _fake_llama_module(FakeLlama))
    monkeypatch.setattr(runtime_mod, "llama_backends", lambda: (True, False))
    lines: list[str] = []
    with pytest.raises(OSError):
        ModelRuntime(model_path=gguf, decision_fn=lines.append).load()
    assert built == [-1, 0]  # the single retry was attempted…
    assert lines == []  # …and its failure is a hard stop, never a fake line


def test_stub_runtime_never_emits_a_line(monkeypatch: pytest.MonkeyPatch) -> None:
    lines: list[str] = []
    rt = ModelRuntime(
        generate_fn=lambda _p, _g: "x", decision_fn=lines.append
    )
    rt.load()
    rt.generate("prompt")
    assert lines == []
    assert rt.inference_line is None


def test_malformed_env_refuses_the_load_value_free(
    gguf: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeLlama:
        def __init__(self, **_kw: Any) -> None:
            raise AssertionError("must not construct with a malformed rung")

    monkeypatch.setitem(sys.modules, "llama_cpp", _fake_llama_module(FakeLlama))
    monkeypatch.setenv(N_GPU_LAYERS_ENV_VAR, "banana")
    with pytest.raises(ModelRuntimeError) as excinfo:
        ModelRuntime(model_path=gguf).load()
    assert "banana" not in str(excinfo.value)


# ------------------------------------------------------ shared detection


def test_version_report_shares_the_runtime_detection_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One detection source, no drift: app.py IMPORTS the runtime's
    llama_backends (shared symbol identity) and /version's helper DELEGATES
    to that binding (patching it moves the version answer), so the backends
    line and the GPU decision can never disagree."""
    assert app.llama_backends is runtime_mod.llama_backends is llama_backends
    monkeypatch.setattr(app, "llama_backends", lambda: (False, True))
    assert app._llama_backends() == (False, True)


# --------------------------------------------- launch line: stdout + log


class _DecidingFakeRuntime:
    """Duck-typed ModelRuntime: ``load()`` delivers the decision line
    through the ctor's ``decision_fn`` (the real emit site), then reports
    done."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.decision_fn = kwargs.get("decision_fn")
        self.loaded = threading.Event()

    def load(self) -> None:
        if callable(self.decision_fn):
            self.decision_fn(GPU_METAL_LINE)
        self.loaded.set()

    def generate(self, *_a: object, **_k: object) -> str:
        raise AssertionError("no turn must run in these tests")


def test_cli_launch_prints_the_decision_line_to_stdout_and_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"GGUF")
    built: list[_DecidingFakeRuntime] = []
    monkeypatch.setattr(
        app, "ModelRuntime",
        lambda *a, **k: built.append(_DecidingFakeRuntime(*a, **k)) or built[-1],
    )
    monkeypatch.setenv(MODEL_PATH_ENV_VAR, str(gguf))
    outputs: list[str] = []

    def input_fn(_prompt: str) -> str:
        # Gate the session end on the emit having happened (loader thread):
        # no log-close race, no flake.
        assert built and built[0].loaded.wait(15)
        return "exit"

    code = app.run(
        ["--zpub", ZPUB], input_fn=input_fn, output_fn=outputs.append,
        interactive=False,
    )
    assert code == 0
    assert GPU_METAL_LINE in outputs  # CLI stdout
    (log_path,) = (tmp_path / "logs").glob("launch-*.log")
    assert f"WARN {GPU_METAL_LINE}" in log_path.read_text(encoding="utf-8")


def test_real_runtime_through_run_emits_once_to_stdout_and_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end (still hermetic): the REAL ModelRuntime + a fake wheel
    module — the line reaches the terminal and the launch log exactly
    once, and the fake Llama was built with n_gpu_layers=-1."""
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"GGUF")
    built: list[dict[str, Any]] = []
    constructed = threading.Event()

    class FakeLlama:
        def __init__(self, **kw: Any) -> None:
            built.append(kw)
            constructed.set()

        def __call__(self, **_kw: Any) -> dict[str, Any]:
            return {"choices": [{"text": '{"v": 0}'}]}

    monkeypatch.setitem(
        sys.modules, "llama_cpp", _fake_llama_module(FakeLlama)
    )
    monkeypatch.setattr(runtime_mod, "llama_backends", lambda: (True, False))
    monkeypatch.setenv(MODEL_PATH_ENV_VAR, str(gguf))
    outputs: list[str] = []

    def input_fn(_prompt: str) -> str:
        assert constructed.wait(15)
        return "exit"

    code = app.run(
        ["--zpub", ZPUB], input_fn=input_fn, output_fn=outputs.append,
        interactive=False,
    )
    assert code == 0
    assert built[0]["n_gpu_layers"] == -1
    assert outputs.count(GPU_METAL_LINE) == 1
    (log_path,) = (tmp_path / "logs").glob("launch-*.log")
    text = log_path.read_text(encoding="utf-8")
    assert text.count(f"WARN {GPU_METAL_LINE}") == 1


@pytest.mark.parametrize(
    "mode",
    ["stub", "remote"],
)
def test_stub_and_remote_launches_have_no_decision_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Honest absence: neither the demo stub nor the ADR-0007 bridge builds
    a llama runtime, so NO inference line is printed or logged."""
    outputs: list[str] = []
    argv = ["--zpub", ZPUB]
    if mode == "stub":
        argv.append("--stub-llm")
    else:
        monkeypatch.setenv("LOCALWALLET_LLM_BASE_URL", "http://127.0.0.1:9/v1")
        monkeypatch.setenv("LOCALWALLET_LLM_MODEL", "some-model")
    code = app.run(
        argv, input_fn=lambda _p: "exit", output_fn=outputs.append,
        interactive=False,
    )
    assert code == 0
    joined = "\n".join(outputs)
    assert "inference:" not in joined
    (log_path,) = (tmp_path / "logs").glob("launch-*.log")
    assert "inference:" not in log_path.read_text(encoding="utf-8")

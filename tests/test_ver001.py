"""TCK-VER-001 — the deterministic version report (user problem 2026-09-20:
"which build/prompt/model am I RUNNING?").

Pins the three access paths to ONE identical, value-free block:

* the ``/version`` transcript command (deterministic UI, ADR-0020 —
  the ``prompts == []`` pin below proves the LLM is never consulted);
* the per-launch log lines (APP-LOG-001 discipline: narration goes to the
  transcript, errors + VERSION go to the launch log — never the terminal,
  never the web transcript);
* ``tools/version_report.py`` (the paste-able block, smoke-pinned below).

And the report SHAPE: the prompt fingerprint equals a recomputed sha256 of
``build_system_prompt()`` (it CHANGES with every prompt edit — that is the
point: paste it, compare against the hash at any HEAD); the backend-flag
booleans are present; model paths appear as BASENAME only and HOME never
appears anywhere (LAUNCH-002 scrub discipline, belt-pinned); the commit is
the install-time stamp or an honest "unknown (editable/source run)" — never
fabricated.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.agent.prompt import build_system_prompt
from localwallet.agent.runtime import MODEL_PATH_ENV_VAR
from localwallet.protocol import INTENT_REGISTRY
from localwallet.store import SCHEMA_VERSION, Store
from tests.test_e2e_skeleton import ZPUB

REPO = Path(__file__).resolve().parents[1]


class _SpyGen:
    """Records every prompt it is asked to generate (the never-called pin)."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, prompt: str, **kwargs: Any) -> str:
        self.prompts.append(prompt)
        return '{"v": 0, "intent": "respond", "params": {"text": "nope"}}'


def _run_version(
    *,
    store: Store | None = None,
    preload_flow: app.ModelPreloadFlow | None = None,
    model_flow: app.ModelDownloadFlow | None = None,
) -> tuple[list[str], _SpyGen]:
    gen = _SpyGen()
    loop = AgentLoop(gen, {})  # never invoked by the deterministic handler
    lines: list[str] = []
    app._handle_transcript_command(
        "/version",
        loop,
        lines.append,
        store=store,
        preload_flow=preload_flow,
        model_flow=model_flow,
    )
    return lines, gen


# ------------------------------------------------------ /version determinism


def test_version_is_deterministic_never_the_model() -> None:
    """The done-when pin: ``/version`` runs the block with ZERO model
    involvement (prompts == []) — a deterministic transcript command like
    /label and /details (ADR-0020), never an intent."""
    lines, gen = _run_version()
    assert gen.prompts == []
    joined = "\n".join(lines)
    assert joined.startswith("app: local-wallet ")
    assert "prompt: sha256 " in joined


def test_version_reports_live_store_schema(tmp_path: Path) -> None:
    """The store pin rides LIVE store truth (PRAGMA user_version via the
    typed accessor), and the intent-registry count is the closed-protocol
    sanity pin."""
    store = Store(tmp_path / "store.db")
    try:
        lines, _ = _run_version(store=store)
    finally:
        store.close()
    joined = "\n".join(lines)
    assert f"store: schema={SCHEMA_VERSION}, intents={len(INTENT_REGISTRY)}" in joined


def test_store_schema_version_property_reads_the_pragma(tmp_path: Path) -> None:
    """The store accessor itself: a fresh DB stamps exactly SCHEMA_VERSION
    (the same value _migrate gated on)."""
    with Store(tmp_path / "s.db") as store:
        assert store.schema_version == SCHEMA_VERSION


# ------------------------------------------------------------- report shape


def test_prompt_hash_matches_a_recomputed_sha256() -> None:
    """The fingerprint is NOT a stored constant — it hashes the actual
    prompt bytes of THIS build (a prompt edit moves it; the char count
    doubles as the 15,000-budget check)."""
    text = build_system_prompt()
    expect = f"prompt: sha256 {hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}, {len(text)} chars"
    lines = app.version_report_lines(model=(None, None, None))
    assert expect in lines


def test_backend_flag_booleans_present() -> None:
    """'Is inference on GPU?' is answered with plain booleans (the installed
    wheel's dylib inventory — a find_spec glob, no GPU init needed)."""
    joined = "\n".join(app.version_report_lines(model=(None, None, None)))
    assert re.search(r"backends: metal=(True|False), cuda=(True|False)", joined)


def test_llama_flags_definitively_on_user_report_not_at_launch() -> None:
    """load_llama=True forces the wheel import for a DEFINITIVE answer; the
    launch-log line (load_llama=False) defers to the honest marker so startup
    never pays the GPU init."""
    deferred = "\n".join(app.version_report_lines(model=(None, None, None)))
    assert "not loaded (see /version)" in deferred
    forced = "\n".join(
        app.version_report_lines(model=(None, None, None), load_llama=True)
    )
    assert "not loaded" not in forced
    # The forced report names a backend family (MTL/CUDA/CPU) or honestly
    # says the wheel is unavailable — never a blank.
    assert re.search(r"flags: (\w+ )", forced)


def test_report_is_value_free_home_scrubbed(tmp_path: Path) -> None:
    """Value-free by construction AND by belt: no HOME anywhere, model paths
    reduced to basenames, no key/address shapes."""
    gguf = tmp_path / "modelcache" / "some-model.gguf"
    gguf.parent.mkdir()
    gguf.write_bytes(b"x")
    lines = app.version_report_lines(
        model=(str(gguf), "ab" * 32, None),
        load_llama=False,
    )
    joined = "\n".join(lines)
    assert str(Path.home()) not in joined
    assert gguf.parent.name not in joined  # basename only, never a directory
    assert "some-model.gguf" in joined
    assert "sha256 abababababab (manifest pin)" in joined


def test_scrub_home_helper() -> None:
    home = str(Path.home()).rstrip("/")
    assert app._scrub_home(f"{home}/x") == "~/x"


def test_silenced_native_fds_partial_failure_no_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ITEM-3 pin: when ``os.dup`` fails after ``os.open`` succeeded (a
    closed/pinned std-fd harness), the with-block survives unsilenced AND
    the devnull fd already taken is closed — no leak."""
    real_open = os.open
    real_close = os.close
    opened: list[int] = []
    closed: list[int] = []

    def _fake_open(*a: Any, **k: Any) -> int:
        fd = real_open(*a, **k)
        opened.append(fd)
        return fd

    def _raising_dup(_fd: int) -> int:
        raise OSError("dup failed")

    def _fake_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(os, "open", _fake_open)
    monkeypatch.setattr(os, "dup", _raising_dup)
    monkeypatch.setattr(os, "close", _fake_close)

    with app._silenced_native_fds():
        pass  # survives the partial failure and yields unsilenced

    assert opened  # devnull was opened before dup failed
    assert closed == opened  # every opened fd was closed — no leak


# ---------------------------------------------------------------- commit line


def test_commit_from_install_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The embedded stamp (written by install.sh) is reported verbatim —
    including the honest -dirty suffix."""
    monkeypatch.setitem(
        sys.modules, "localwallet._build_commit", SimpleNamespace(BUILD_COMMIT="0123456789ab-dirty")
    )
    joined = "\n".join(app.version_report_lines(model=(None, None, None)))
    assert "@ 0123456789ab-dirty" in joined


def test_commit_absent_is_honest_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No stamp (the source checkout) → 'unknown (editable/source run)'.
    Never fabricated, never a runtime git call."""
    monkeypatch.setitem(sys.modules, "localwallet._build_commit", None)
    joined = "\n".join(app.version_report_lines(model=(None, None, None)))
    assert "unknown (editable/source run)" in joined


def test_editable_install_detected_via_direct_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editable detection rides the dist-info ``direct_url.json``
    ``dir_info.editable`` flag (PEP 660) — the reliable signal, not a bare
    .pth (which a non-editable source install can also carry)."""
    class _FakeDist:
        def __init__(self, text: str | None) -> None:
            self._text = text

        def read_text(self, name: str) -> str | None:
            return self._text if name == "direct_url.json" else None

    monkeypatch.setattr(
        app.importlib.metadata, "distribution", lambda _n: _FakeDist(
            '{"url": "file:///x", "dir_info": {"editable": true}}'
        )
    )
    assert app._is_editable_install() is True
    monkeypatch.setattr(
        app.importlib.metadata, "distribution", lambda _n: _FakeDist('{"url": "file:///x"}')
    )
    assert app._is_editable_install() is False
    monkeypatch.setattr(
        app.importlib.metadata, "distribution", lambda _n: _FakeDist(None)
    )
    assert app._is_editable_install() is False


def test_app_line_editable_caveat_appended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editable install → the app line carries the honest caveat (the stamp
    was written once at install; the running source may be newer)."""
    monkeypatch.setitem(
        sys.modules, "localwallet._build_commit", SimpleNamespace(BUILD_COMMIT="0123456789ab")
    )
    monkeypatch.setattr(app, "_is_editable_install", lambda: True)
    joined = "\n".join(app.version_report_lines(model=(None, None, None)))
    assert (
        "@ 0123456789ab (editable install — stamp may be older than running source)"
        in joined
    )


def test_app_line_non_editable_has_no_caveat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real (non-editable) install shows the bare stamp — no caveat."""
    monkeypatch.setitem(
        sys.modules, "localwallet._build_commit", SimpleNamespace(BUILD_COMMIT="0123456789ab")
    )
    monkeypatch.setattr(app, "_is_editable_install", lambda: False)
    joined = "\n".join(app.version_report_lines(model=(None, None, None)))
    assert "@ 0123456789ab\n" in joined
    assert "editable install" not in joined


# ------------------------------------------------------------- model truths


def test_session_truth_preload_is_the_launch_003_path(tmp_path: Path) -> None:
    """A live preload flow carries BOTH the path and the manifest pin — the
    /version report shows them (the checksum the launch actually verifies)."""
    gguf = tmp_path / "pinned.gguf"
    gguf.write_bytes(b"x")
    flow = app.ModelPreloadFlow(
        SimpleNamespace(),  # runtime never touched by the report
        model_path=str(gguf),
        sha256="cd" * 32,
    )
    truth = app._session_model_truth(flow, None)
    joined = "\n".join(app.version_report_lines(model=truth))
    assert "model: pinned.gguf, sha256 cdcdcdcdcdcd (manifest pin)" in joined
    assert str(tmp_path) not in joined  # basename only


def test_session_truth_env_rung_unpinned_is_honest(tmp_path: Path) -> None:
    """An arbitrary env-rung file has NO pin → 'no checksum (unpinned path)'
    (LAUNCH-003 never invents a verdict; neither does the report)."""
    gguf = tmp_path / "custom.gguf"
    gguf.write_bytes(b"x")
    flow = app.ModelPreloadFlow(SimpleNamespace(), model_path=str(gguf))
    lines = app.version_report_lines(model=app._session_model_truth(flow, None))
    assert "model: custom.gguf, no checksum (unpinned path)" in lines[2]


def test_session_truth_download_card_and_stub() -> None:
    """Not-downloaded (card armed) and no-model (stub/remote) report
    honestly — no path, no fabricated hash."""
    card = app.ModelDownloadFlow(model_name="gemma-default")
    line = app.version_report_lines(model=app._session_model_truth(None, card))[2]
    assert line == "model: gemma-default (pinned default not downloaded — demo stub runs)"
    none = app.version_report_lines(model=app._session_model_truth(None, None))[2]
    assert none == "model: none (demo stub or remote-bridge run)"


def test_ladder_truth_env_rung(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The no-session resolver (launch line + tool) mirrors _run's ladder:
    env rung wins, arbitrary path → unpinned."""
    gguf = tmp_path / "env-model.gguf"
    gguf.write_bytes(b"x")
    monkeypatch.setenv(MODEL_PATH_ENV_VAR, str(gguf))
    assert app._ladder_model_truth() == (str(gguf), None, None)


def test_ladder_truth_default_rung(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No env → the manifest's pinned default (present file carries its
    pin); monkeypatched module constants prove both branches without a
    real models/ layout."""
    monkeypatch.delenv(MODEL_PATH_ENV_VAR, raising=False)
    gguf = tmp_path / "bin" / "def.gguf"
    gguf.parent.mkdir()
    gguf.write_bytes(b"x")
    monkeypatch.setattr(app, "_resolve_default_model", lambda: ("def", gguf))
    monkeypatch.setattr(app, "_manifest_pin_for", lambda p: "ef" * 32)
    assert app._ladder_model_truth() == (str(gguf), "ef" * 32, None)
    gguf.unlink()  # absent file → the NAME branch (card state)
    assert app._ladder_model_truth() == (None, None, "def")


def test_ladder_truth_env_set_but_missing_is_unpinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MINOR-4 pin (tool path): env path set but the file is absent → the
    ladder carries the PATH unpinned and the report says so honestly — never
    the misleading "none (demo stub or remote-bridge run)"."""
    missing = tmp_path / "env-missing.gguf"
    monkeypatch.setenv(MODEL_PATH_ENV_VAR, str(missing))
    assert app._ladder_model_truth() == (str(missing), None, None)
    line = app.version_report_lines(model=app._ladder_model_truth())[2]
    assert line == "model: env-missing.gguf (file missing), no checksum (unpinned path)"


def test_ladder_truth_env_whitespace_padded_matches_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ITEM-2 pin: a whitespace-padded env path is carried RAW (mirroring
    run() verbatim) — never stripped to a path the session never ran."""
    gguf = tmp_path / "pad.gguf"
    gguf.write_bytes(b"x")
    padded = f"  {gguf}  "
    monkeypatch.setenv(MODEL_PATH_ENV_VAR, padded)
    assert app._ladder_model_truth() == (padded, None, None)


def test_session_truth_env_path_branch(tmp_path: Path) -> None:
    """MINOR-4 pin (session path): the env rung selected-but-missing passes
    its path through ``env_path`` so the launch-log truth resolves exactly
    like the tool's ladder (same line), never to "none"."""
    missing = tmp_path / "env-missing.gguf"
    truth = app._session_model_truth(None, None, env_path=str(missing))
    assert truth == (str(missing), None, None)
    line = app.version_report_lines(model=truth)[2]
    assert line == "model: env-missing.gguf (file missing), no checksum (unpinned path)"
    # Without the env path (stub/remote session) the honest "none" stands:
    assert app.version_report_lines(model=app._session_model_truth(None, None))[2] == (
        "model: none (demo stub or remote-bridge run)"
    )


# ------------------------------------------------------------ launch log line


def test_launch_writes_the_version_to_the_log_not_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """APP-LOG-001 discipline for the version half: the block rides the
    per-launch log (mode-independent, CLI and web share the one site), and
    it is NEVER narrated to the terminal/transcript."""
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv(app.AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")
    outputs: list[str] = []
    code = app.run(
        ["--stub-llm", "--zpub", ZPUB],
        output_fn=outputs.append,
        input_fn=lambda _p: "exit",
        interactive=False,
    )
    assert code == 0
    assert not [ln for ln in outputs if ln.startswith("app: local-wallet")]
    (log_path,) = (tmp_path / "logs").glob("launch-*.log")
    text = log_path.read_text(encoding="utf-8")
    assert "INFO version app: local-wallet " in text
    assert "INFO version prompt: sha256 " in text
    assert "INFO version model: none (demo stub or remote-bridge run)" in text
    assert "INFO version backends: " in text
    # The stub launch reports the truth triple of ITS selection — the
    # launch-log line must not pay the wheel's GPU init:
    assert "not loaded (see /version)" in text


def test_launch_log_env_missing_resolves_like_ladder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MINOR-4 pin (launch-log path, end-to-end): env path set but the file
    is absent → the launch-log model line reports the unpinned missing path,
    matching the tool's ladder — not the misleading "none (demo stub or
    remote-bridge run)"."""
    missing = tmp_path / "env-missing.gguf"
    monkeypatch.setenv(MODEL_PATH_ENV_VAR, str(missing))
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv(app.AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")
    outputs: list[str] = []
    code = app.run(
        ["--stub-llm", "--zpub", ZPUB],  # env rung still wins over --stub-llm
        output_fn=outputs.append,
        input_fn=lambda _p: "exit",
        interactive=False,
    )
    assert code == 0
    (log_path,) = (tmp_path / "logs").glob("launch-*.log")
    text = log_path.read_text(encoding="utf-8")
    assert (
        f"INFO version model: {missing.name} (file missing), no checksum (unpinned path)"
        in text
    )
    assert "INFO version model: none (demo stub or remote-bridge run)" not in text


def test_session_version_env_missing_matches_ladder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ITEM-1 pin (end-to-end): env path set but the file is absent → the
    /version command IN THE SAME SESSION reports the unpinned missing path,
    matching the launch log and the tool's ladder — never the misleading
    "none (demo stub or remote-bridge run)" (the env rung wins over
    --stub-llm at run(), so the session reports run()'s RAW value)."""
    missing = tmp_path / "env-missing.gguf"
    monkeypatch.setenv(MODEL_PATH_ENV_VAR, str(missing))
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv(app.AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")
    fed = {"n": 0}

    def _input(_p: str) -> str:
        fed["n"] += 1
        return "/version" if fed["n"] == 1 else "exit"

    outputs: list[str] = []
    code = app.run(
        ["--stub-llm", "--zpub", ZPUB],
        output_fn=outputs.append,
        input_fn=_input,
        interactive=False,
    )
    assert code == 0
    joined = "\n".join(outputs)
    assert (
        f"model: {missing.name} (file missing), no checksum (unpinned path)" in joined
    )
    assert "model: none (demo stub or remote-bridge run)" not in joined


def test_tools_version_report_smoke() -> None:
    """``.venv/bin/python tools/version_report.py`` exits 0 with the same
    block shape (the paste-able third path)."""
    res = subprocess.run(
        [sys.executable, str(REPO / "tools" / "version_report.py")],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith("app: local-wallet ")
    assert "prompt: sha256 " in res.stdout
    assert re.search(r"backends: metal=(True|False), cuda=(True|False)", res.stdout)
    assert str(Path.home()) not in res.stdout


# ------------------------------------------------------------------- /help


def test_help_lists_version() -> None:
    """/help (the command index user sees) mentions /version."""
    out: list[str] = []
    loop = AgentLoop(_SpyGen(), {})
    app._handle_transcript_command("/help", loop, out.append)
    assert "/version" in out[0]

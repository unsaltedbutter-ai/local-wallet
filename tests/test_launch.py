"""TCK-LAUNCH-001 — web-first default launch (ADR-0024 amendment).

Pins the launch matrix, browser auto-open, no-model fallback, watch-key
precedence, and the fixed-port knob. Nothing here opens a real browser or
touches the network: the browser is a monkeypatched seam and every web
session runs against a throwaway tmp store with the watch-interval off.

Launch matrix (entry = ``main`` / ``python -m localwallet.ui.cli``):

| invocation                         | transport |
|------------------------------------|-----------|
| bare entry                         | WEB       |
| LOCALWALLET_UI=cli entry           | CLI       |
| --cli entry                        | CLI       |
| --web entry                        | WEB       |
| LOCALWALLET_UI=web entry           | WEB       |
| direct programmatic run() (tests)  | CLI (unchanged default) |
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import socket
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

from localwallet import app
from localwallet.agent.runtime import ModelRuntime
from localwallet.config import Settings
from localwallet.store import Store
from localwallet.wallet import WalletDescriptor
from tests.test_e2e_skeleton import XPRV, ZPUB


def _seed_store(store_path: Path, key: str = ZPUB) -> None:
    """Persist a wallet row the way a real accepted launch would (the
    canonical descriptor is THE stored watch key)."""
    store = Store(str(store_path))
    try:
        wallet = store.create_wallet("default", WalletDescriptor.from_key(key).descriptor)
        store.set_active_wallet(wallet.id)
    finally:
        store.close()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every launch runs against an empty tmp store with the background
    watch off and no model env — never the repo-root localwallet.db."""
    for var in (
        app.ZPUB_ENV_VAR,
        app.UI_ENV_VAR,
        "LOCALWALLET_MODEL_PATH",
        "LOCALWALLET_LLM_BASE_URL",
        "LOCALWALLET_LLM_MODEL",
        "LOCALWALLET_CHAIN_BASE_URL",
        "LOCALWALLET_WEB_PORT",
        "LOCALWALLET_GAP_LIMIT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(tmp_path / "launch.db"))
    monkeypatch.setenv(app.AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")


# ----------------------------------------------------------- default UI flip


def test_bare_entry_launches_web(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: ``python -m localwallet.ui.cli`` (no args) serves
    the web UI, not the REPL. Driven through a background thread parked on
    the server, released via the on_web_server seam the entry forwards."""
    opened: list[str] = []
    monkeypatch.setattr(app.webbrowser, "open", lambda url: opened.append(url) or True)
    capture: dict[str, Any] = {}
    gate = threading.Event()
    outputs: list[str] = []
    thread = threading.Thread(
        target=lambda: capture.update(
            code=app.main(
                ["--zpub", ZPUB],
                output_fn=outputs.append,
                on_web_server=lambda s: (capture.update(server=s), gate.set()),
            )
        ),
        daemon=True,
    )
    thread.start()
    assert gate.wait(30), "bare entry never started a web server"
    server = capture["server"]
    try:
        assert any(line.startswith("Web UI:") for line in outputs)
        # Browser auto-open at the canonical URL, and the calm best-effort
        # line (never the token — the island delivers it from the URL).
        assert opened == [server.url]
        assert "Opening your browser…" in outputs
        # Minimal web banner: the REPL 'type a message' hint is NOT printed.
        assert not any(line.startswith("Type a message") for line in outputs)
        # TCK-APP-LOG-001: user-facing narration routes to the SSE emitter,
        # NOT the terminal — the privacy notice is absent from stdout here
        # (its presence in the event stream is pinned in test_web_server.py).
        assert not any(line.startswith("Privacy notice:") for line in outputs)
    finally:
        server.stop()
        thread.join(15)


def test_env_cli_keeps_the_repl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LOCALWALLET_UI=cli opts back out of the web default; the browser is
    never touched."""
    opened: list[str] = []
    monkeypatch.setattr(app.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setenv(app.UI_ENV_VAR, "cli")
    outputs: list[str] = []
    code = app.main(
        ["--stub-llm", "--zpub", ZPUB],
        input_fn=lambda _p: "exit",
        output_fn=outputs.append,
    )
    assert code == 0
    assert not any(line.startswith("Web UI:") for line in outputs)
    assert opened == []


def test_cli_flag_overrides_web_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.webbrowser, "open", lambda _u: True)
    monkeypatch.setenv(app.UI_ENV_VAR, "web")
    outputs: list[str] = []
    code = app.main(
        ["--stub-llm", "--zpub", ZPUB, "--cli"],
        input_fn=lambda _p: "exit",
        output_fn=outputs.append,
    )
    assert code == 0
    assert not any(line.startswith("Web UI:") for line in outputs)


def test_web_flag_overrides_cli_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--web wins over LOCALWALLET_UI=cli (the flag > env rule)."""
    monkeypatch.setattr(app.webbrowser, "open", lambda _u: True)
    monkeypatch.setenv(app.UI_ENV_VAR, "cli")
    capture: dict[str, Any] = {}
    gate = threading.Event()
    outputs: list[str] = []
    thread = threading.Thread(
        target=lambda: capture.update(
            code=app.main(
                ["--stub-llm", "--zpub", ZPUB, "--web"],
                output_fn=outputs.append,
                on_web_server=lambda s: (capture.update(server=s), gate.set()),
            )
        ),
        daemon=True,
    )
    thread.start()
    assert gate.wait(30)
    capture["server"].stop()
    thread.join(15)
    assert any(line.startswith("Web UI:") for line in outputs)


def test_web_and_cli_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as exc:
        app._parse_args(["--web", "--cli"])
    assert exc.value.code == 2


# --------------------------------------------------- browser auto-open safety


def test_browser_open_failure_is_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headless/SSH: webbrowser.open raising must NEVER crash the launch —
    one calm manual-open line stands in its place."""

    def boom(_url: str) -> bool:
        raise RuntimeError("no browser here")

    monkeypatch.setattr(app.webbrowser, "open", boom)
    capture: dict[str, Any] = {}
    gate = threading.Event()
    outputs: list[str] = []
    thread = threading.Thread(
        target=lambda: capture.update(
            code=app.main(
                ["--stub-llm", "--zpub", ZPUB],
                output_fn=outputs.append,
                on_web_server=lambda s: (capture.update(server=s), gate.set()),
            )
        ),
        daemon=True,
    )
    thread.start()
    assert gate.wait(30), "a browser-less launch must still serve the web UI"
    server = capture["server"]
    url_line = next(line for line in outputs if line.startswith("Web UI: "))
    url = url_line[len("Web UI: ") :]
    server.stop()
    thread.join(15)
    joined = "\n".join(outputs)
    assert "Could not open a browser" in joined
    assert url in joined  # names the exact URL to open by hand
    assert "Traceback" not in joined


def test_run_web_does_not_open_a_browser_without_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the ENTRY auto-opens; a programmatic run(--web) (all the other
    web tests) never hijacks a real browser."""
    opened: list[str] = []
    monkeypatch.setattr(app.webbrowser, "open", lambda url: opened.append(url) or True)
    capture: dict[str, Any] = {}
    gate = threading.Event()
    thread = threading.Thread(
        target=lambda: capture.update(
            code=app.run(
                ["--stub-llm", "--zpub", ZPUB, "--web"],
                output_fn=lambda _s: None,
                on_web_server=lambda s: (capture.update(server=s), gate.set()),
            )
        ),
        daemon=True,
    )
    thread.start()
    assert gate.wait(30)
    capture["server"].stop()
    thread.join(15)
    assert opened == []


# ------------------------------------------------------ watch-key precedence


def test_stored_key_is_reused_by_a_bare_headless_cli_launch(
    tmp_path: Path,
) -> None:
    """No flag, no env, but a wallet row exists → the launch continues on
    the stored key instead of the old exit-2 refusal (single-user: "if they
    have given us a zpub we use that one")."""
    store_path = Path(os.environ["LOCALWALLET_STORE_PATH"])
    _seed_store(store_path)
    outputs: list[str] = []
    code = app.run(
        ["--stub-llm"],
        input_fn=lambda _p: "exit",
        output_fn=outputs.append,
        interactive=False,
    )
    assert code == 0
    assert not any("No watch key configured" in line for line in outputs)


def test_env_and_flag_override_the_stored_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--zpub > LOCALWALLET_ZPUB > stored. With a valid key STORED, an
    invalid key on the env (or flag) rung still wins — and is refused
    value-free (exit 2), proving the higher rung was read, not the store."""
    _seed_store(Path(os.environ["LOCALWALLET_STORE_PATH"]))
    monkeypatch.setenv(app.ZPUB_ENV_VAR, XPRV)  # private key — always refused
    outputs: list[str] = []
    code = app.run(
        ["--stub-llm"],
        input_fn=lambda _p: "exit",
        output_fn=outputs.append,
        interactive=False,
    )
    assert code == 2
    joined = "\n".join(outputs)
    assert "Watch key rejected" in joined
    assert XPRV not in joined  # value-free


def test_flag_wins_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_store(Path(os.environ["LOCALWALLET_STORE_PATH"]))
    monkeypatch.setenv(app.ZPUB_ENV_VAR, XPRV)
    outputs: list[str] = []
    # --zpub supplies the valid stored key again → the invalid env rung is
    # shadowed by the flag (flag > env), so the launch runs.
    code = app.run(
        ["--stub-llm", "--zpub", ZPUB],
        input_fn=lambda _p: "exit",
        output_fn=outputs.append,
        interactive=False,
    )
    assert code == 0


def test_never_configured_headless_cli_still_refuses(
    tmp_path: Path,
) -> None:
    outputs: list[str] = []
    code = app.run(
        ["--stub-llm"],
        input_fn=lambda _p: "exit",
        output_fn=outputs.append,
        interactive=False,
    )
    assert code == 2
    assert any("No watch key configured" in line for line in outputs)


# --------------------------------------------------------- fixed-port option


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_fixed_port_is_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    port = _free_port()
    monkeypatch.setenv("LOCALWALLET_WEB_PORT", str(port))
    monkeypatch.setattr(app.webbrowser, "open", lambda _u: True)
    capture: dict[str, Any] = {}
    gate = threading.Event()
    thread = threading.Thread(
        target=lambda: capture.update(
            code=app.main(
                ["--stub-llm", "--zpub", ZPUB],
                output_fn=lambda _s: None,
                on_web_server=lambda s: (capture.update(server=s), gate.set()),
            )
        ),
        daemon=True,
    )
    thread.start()
    assert gate.wait(30)
    server = capture["server"]
    try:
        assert server.httpd.server_address[1] == port
    finally:
        server.stop()
        thread.join(15)


def test_busy_fixed_port_exits_2_naming_the_fix(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    port = _free_port()
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)
    monkeypatch.setenv("LOCALWALLET_WEB_PORT", str(port))
    try:
        code = app.run(
            ["--stub-llm", "--zpub", ZPUB, "--web"], output_fn=lambda _s: None
        )
        assert code == 2
        # Web errors go to stderr + the log file (TCK-APP-LOG-001).
        joined = capsys.readouterr().err
        assert "already in use" in joined
        assert "LOCALWALLET_WEB_PORT" in joined  # names the fix
        assert str(port) not in joined  # value-free
    finally:
        blocker.close()


def test_out_of_range_port_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LOCALWALLET_WEB_PORT", "70000")
    code = app.run(
        ["--stub-llm", "--zpub", ZPUB, "--web"], output_fn=lambda _s: None
    )
    assert code == 2
    # Web errors go to stderr + the log file (TCK-APP-LOG-001).
    assert any(line.startswith("Configuration error:") for line in capsys.readouterr().err.splitlines())


def test_web_port_ladder_env_over_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Settings ladder for the new rung: env > config file > default."""
    cfg = tmp_path / "config.json"
    cfg.write_text('{"web_port": 8123}')
    monkeypatch.delenv("LOCALWALLET_WEB_PORT", raising=False)
    assert Settings.from_env(cfg).web_port == 8123
    monkeypatch.setenv("LOCALWALLET_WEB_PORT", "9000")
    assert Settings.from_env(cfg).web_port == 9000
    assert Settings().web_port == 0  # shipped default = ephemeral


# ----------------------------------------- reconnect backoff bounded (deliverable 7)


def test_events_reconnect_backoff_is_bounded() -> None:
    """The browser-inherent /events reconnect storm against a dead server
    must stay BOUNDED (verified, not changed — ADR-0024 §5). The client
    doubles the delay up to a 15s ceiling and resets on a live stream; this
    pins both so a future edit cannot turn it into a tight retry loop."""
    app_js = (
        Path(app.__file__).resolve().parent / "ui" / "web" / "static" / "app.js"
    ).read_text(encoding="utf-8")
    assert "Math.min(state.backoffMs * 2, 15000)" in app_js  # 15s ceiling
    assert "state.backoffMs = 500; // a live stream resets the backoff ladder" in app_js


# ===================== TCK-LAUNCH-002: default model + download card =========
#
# Resolution matrix (no --stub-llm / no generate_fn):
#
# | rung                                   | outcome                        |
# |----------------------------------------|--------------------------------|
# | LOCALWALLET_MODEL_PATH set             | ModelRuntime (env), no card    |
# | manifest default, FILE PRESENT         | ModelRuntime(default), no card |
# | manifest default, FILE ABSENT          | stub + Yes/No card + flow      |
# | no resolvable default (manifest)       | stub + old demo banner         |
# | --stub-llm                             | stub, NO banner, NO card       |
#
# Download lifecycle is driven with FAKE fast-downloader subprocesses (the
# command seam) — the real models/download_model.py is never run here (its
# own seam is pinned separately, and network stays untouched).

# Fake downloader scripts (argument-list seam — no network, no paths):
_FAKE_OK = (
    "import json\n"
    "for n in (1_048_576, 2_097_152, 3_145_728):\n"
    "    print(json.dumps({'downloaded': n, 'total': 3_145_728}), flush=True)\n"
)
_FAKE_FAIL = (
    "import sys\n"
    "print('boom /Users/homer/.local-wallet/models/x', file=sys.stderr)\n"
    "sys.exit(1)\n"
)
_FAKE_HANG = (
    "import json, time\n"
    "print(json.dumps({'downloaded': 1, 'total': 100}), flush=True)\n"
    "time.sleep(60)\n"
)


class _FakeRuntime:
    """Stand-in for the lazily-loaded ModelRuntime (never generated on).

    TCK-LAUNCH-003: records preload ``load()`` calls (the background
    preload thread fires at engine start) without touching llama.cpp.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.ctor = (args, kwargs)
        self.load_calls = 0
        self.loaded = threading.Event()

    def load(self) -> None:
        self.load_calls += 1
        self.loaded.set()

    def generate(self, prompt: str, *, grammar_text: str | None = None) -> str:
        raise AssertionError("no turn must run in these tests")


# ------------------------------------------------------- resolution matrix


def test_env_model_path_selects_runtime_without_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALWALLET_MODEL_PATH", "/some/where/model.gguf")
    built: list[_FakeRuntime] = []
    monkeypatch.setattr(app, "ModelRuntime", lambda *a, **k: built.append(
        _FakeRuntime(*a, **k)) or built[-1])
    outputs: list[str] = []
    code = app.run(
        ["--zpub", ZPUB], input_fn=lambda _p: "exit", output_fn=outputs.append
    )
    assert code == 0
    assert len(built) == 1  # the real runtime, not the stub
    joined = "\n".join(outputs)
    assert app.MODEL_CARD_QUESTION not in joined
    assert app.NO_MODEL_DEMO_BANNER not in joined


def test_default_file_present_selects_real_model_silently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gguf = tmp_path / "models" / "bin" / "m.gguf"
    gguf.parent.mkdir(parents=True)
    gguf.write_bytes(b"GGUF-fake")
    monkeypatch.setattr(app, "_resolve_default_model", lambda: ("m", gguf))
    built: list[_FakeRuntime] = []
    monkeypatch.setattr(app, "ModelRuntime", lambda *a, **k: built.append(
        _FakeRuntime(*a, **k)) or built[-1])
    outputs: list[str] = []
    code = app.run(
        ["--zpub", ZPUB], input_fn=lambda _p: "exit", output_fn=outputs.append
    )
    assert code == 0
    assert built and built[0].ctor[1].get("model_path") == str(gguf)
    joined = "\n".join(outputs)
    assert app.MODEL_CARD_QUESTION not in joined  # the NORMAL launch: no card
    assert app.NO_MODEL_DEMO_BANNER not in joined  # no demo either
    # TCK-LAUNCH-003: and the real rung PRELOADS at engine start — the
    # background loader called runtime.load() (seam counter, never llama).
    assert built[0].loaded.wait(10)
    assert app.MODEL_PRELOAD_NOTICE in joined


def test_default_file_absent_arms_the_download_card(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "models" / "bin" / "m.gguf"
    monkeypatch.setattr(app, "_resolve_default_model", lambda: ("m", missing))
    outputs: list[str] = []
    code = app.run(
        ["--zpub", ZPUB], input_fn=lambda _p: "exit", output_fn=outputs.append
    )
    assert code == 0
    joined = "\n".join(outputs)
    assert app.MODEL_CARD_QUESTION in joined  # no silent demo mode
    assert app.MODEL_CARD_HINT in joined
    assert app.NO_MODEL_DEMO_BANNER not in joined  # the card REPLACES it
    # TCK-LAUNCH-003: the card path is the PRELOAD path's exclusive
    # opposite — nothing to load, so no preload arms (no regression).
    assert app.MODEL_PRELOAD_NOTICE not in joined


def test_unresolvable_default_keeps_the_plain_demo_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "_resolve_default_model", lambda: None)
    outputs: list[str] = []
    code = app.run(
        ["--zpub", ZPUB], input_fn=lambda _p: "exit", output_fn=outputs.append
    )
    assert code == 0
    joined = "\n".join(outputs)
    assert app.NO_MODEL_DEMO_BANNER in joined
    assert app.MODEL_CARD_QUESTION not in joined  # nothing exists to download


def test_stub_llm_flag_prints_no_banner_and_no_card(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--stub-llm stays the explicit dev choice (LAUNCH-001 behavior):
    neither the banner nor a download offer — no flow is ever built."""
    monkeypatch.setattr(
        app, "_resolve_default_model",
        lambda: ("m", tmp_path / "nope.gguf"),
    )
    outputs: list[str] = []
    code = app.run(
        ["--stub-llm", "--zpub", ZPUB],
        input_fn=lambda _p: "exit",
        output_fn=outputs.append,
    )
    assert code == 0
    joined = "\n".join(outputs)
    assert app.NO_MODEL_DEMO_BANNER not in joined
    assert app.MODEL_CARD_QUESTION not in joined


def test_shipped_manifest_pins_exactly_one_default_model() -> None:
    """The manifest's default marker IS the pinned E2B build with a real
    sha256 (the card only ever offers what the child can hash-verify)."""
    entries = json.loads((app.MODELS_DIR / "manifest.json").read_text())
    defaults = [e for e in entries if e.get("default") is True]
    assert len(defaults) == 1
    assert defaults[0]["name"] == "gemma-4-E2B-it-Q4_K_M"
    assert isinstance(defaults[0]["sha256"], str) and len(defaults[0]["sha256"]) == 64
    resolved = app._resolve_default_model()
    assert resolved is not None
    name, path = resolved
    assert name == "gemma-4-E2B-it-Q4_K_M"
    assert path == app.MODELS_DIR / "bin" / f"{name}.gguf"


# ----------------------------------------------------- verdict classifier


def test_card_verdicts_never_outrun_the_confirm_gate() -> None:
    """Bare yes/no classify only while the card is shown AND nothing
    pends; slash forms are canonical and always classify while a flow
    exists; once the model is ready, bare words return to chat."""
    m = app.ModelDownloadFlow(model_name="x")
    assert app._model_card_verdict(m, "yes", tx_pending=False) == "yes"
    assert app._model_card_verdict(m, "n", tx_pending=False) == "no"
    assert app._model_card_verdict(m, "yes", tx_pending=True) is None  # gate's
    assert app._model_card_verdict(m, "no", tx_pending=True) is None
    assert app._model_card_verdict(m, "/download", tx_pending=True) == "yes"
    m.state = "declined"
    assert app._model_card_verdict(m, "yes", tx_pending=False) is None
    assert app._model_card_verdict(m, "/download", tx_pending=False) == "yes"
    m.state = "ready"
    assert app._model_card_verdict(m, "no", tx_pending=False) is None
    # …and while the ADR-0023 onboarding ask listens (its yes/no gates):
    assert app._model_card_verdict(
        app.ModelDownloadFlow(model_name="x"), "yes", False, onboarding_listening=True
    ) is None
    assert app._model_card_verdict(
        app.ModelDownloadFlow(model_name="x"), "/download", False,
        onboarding_listening=True,
    ) == "yes"  # slash forms never collide with any gate


# --------------------------------------------------- download lifecycle


def _download_session(
    command: list[str], answers: list[str], *, until: str
) -> tuple[app.ModelDownloadFlow, list[app.EngineEvent]]:
    """Run ONE real pump on a thread with a fake-downloader flow; feed the
    answers; wait for the flow to reach `until`; QUIT; join bounded."""
    events: list[app.EngineEvent] = []
    emitter = app.EventEmitter(events.append)
    flow = app.ModelDownloadFlow(model_name="fake", command=command)
    commands: queue.Queue[Any] = queue.Queue()
    loop = app.AgentLoop(app.stub_generate, {app.IntentName.RESPOND: app._respond_handler})
    thread = threading.Thread(
        target=lambda: app._pump(
            loop,
            emitter.text,
            commands,
            flow=app.TxFlow(),
            session=app.SendSession(),
            table={},
            emitter=emitter,
            model=flow,
        ),
        daemon=True,
    )
    thread.start()
    for answer in answers:
        commands.put(answer)
    deadline = time.monotonic() + 15
    while flow.state != until and time.monotonic() < deadline:
        time.sleep(0.02)
    commands.put(app.QUIT)
    thread.join(15)
    assert not thread.is_alive()  # bounded exit, always
    return flow, events


def _texts(events: list[app.EngineEvent]) -> str:
    return "\n".join(e.payload for e in events if e.kind == "text")


def test_yes_downloads_with_inline_progress_then_ready() -> None:
    flow, events = _download_session(
        [sys.executable, "-c", _FAKE_OK], ["/download"], until="ready"
    )
    texts = _texts(events)
    assert app.MODEL_DL_STARTED in texts
    assert app.MODEL_DL_DONE in texts
    ticks = [e for e in events if e.kind == app.EVENT_MODEL_PROGRESS]
    assert len(ticks) == 3
    percents = []
    for tick in ticks:
        payload = json.loads(tick.payload)  # INT-ONLY JSON by contract
        assert set(payload) == {"downloaded", "total", "pct"}
        assert all(v is None or isinstance(v, int) for v in payload.values())
        percents.append(payload["pct"])
    assert percents == [33, 66, 100]
    assert flow.state == "ready"
    # value-free progress: no path/name string can ride an int-only payload,
    # and the child's stdout text never enters the stream either:
    assert "/" not in "".join(t.payload for t in ticks)


def test_download_failure_narrates_value_free_and_offers_retry() -> None:
    flow, events = _download_session(
        [sys.executable, "-c", _FAKE_FAIL], ["/download"], until="failed"
    )
    texts = _texts(events)
    assert app.MODEL_DL_FAILED in texts
    assert app.MODEL_DL_DONE not in texts
    joined = texts + "".join(
        e.payload for e in events if e.kind == app.EVENT_MODEL_PROGRESS
    )
    assert "homer" not in joined and ".local-wallet" not in joined  # stderr dead-dropped
    # the card re-arms from failed (the No-path buttons offered again):
    assert app._model_card_verdict(flow, "yes", tx_pending=False) == "yes"


def test_one_download_at_a_time_concurrency_guard() -> None:
    _flow, events = _download_session(
        [sys.executable, "-c", _FAKE_HANG],
        ["/download", "/download"],
        until="running",
    )
    assert app.MODEL_DL_STARTED in _texts(events)
    assert app.MODEL_DL_RUNNING in _texts(events)  # the second ask refused
    assert sum(1 for e in events if e.kind == "text" and app.MODEL_DL_STARTED in e.payload) == 1


def test_quit_terminates_the_child_bounded_no_orphans() -> None:
    started = time.monotonic()
    flow, _events = _download_session(
        [sys.executable, "-c", _FAKE_HANG], ["/download"], until="running"
    )
    elapsed = time.monotonic() - started
    assert elapsed < 20  # _download_session joined the pump (5s term bounds)
    proc = flow._proc
    assert proc is not None and proc.poll() is not None  # child dead
    assert not any(
        t.name == "model-download" and t.is_alive() for t in threading.enumerate()
    )


def test_flow_start_guard_is_the_single_mutation_point() -> None:
    """Unit-level: start() only proceeds from card states, never twice."""
    flow = app.ModelDownloadFlow(model_name="x", command=[sys.executable, "-c", _FAKE_HANG])
    assert flow.start() is False  # not attached yet (no pump queue)
    commands: queue.Queue[Any] = queue.Queue()
    flow.attach(commands)
    assert flow.start() is True
    assert flow.start() is False  # running: guard
    assert flow.state == "running"
    flow.cancel()
    assert not any(t.name == "model-download" and t.is_alive() for t in threading.enumerate())


def test_declined_shows_model_free_actions_and_quick_actions_run_without_llm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """NO → the quick-action list renders; /receive, /address and /settings
    perform through the pump's DETERMINISTIC intercepts (the stub is never
    asked — no canned '(stub model, dev mode)' narration may appear)."""
    monkeypatch.setattr(
        app, "_resolve_default_model",
        lambda: ("m", tmp_path / "nope.gguf"),
    )
    lines = iter(["no", "/receive", "/address", "/settings", "exit"])
    outputs: list[str] = []
    code = app.run(
        ["--zpub", ZPUB], input_fn=lambda _p: next(lines), output_fn=outputs.append
    )
    assert code == 0
    joined = "\n".join(outputs)
    for offer in app.MODEL_DECLINED_LINES:
        assert offer in joined
    assert "Next receive address (index 0" in joined
    assert "Fresh receive address (index 0)" in joined
    assert "watch_key: " in joined  # the settings readout, truncated form
    assert "…" in joined
    assert ZPUB not in joined  # the settings surface never reveals in full
    assert "(stub model, dev mode)" not in joined  # the LLM was bypassed


# -------------------------------------------- watch-key settings entry


def _keyed_store(tmp_path: Path) -> tuple[Path, str]:
    """Seed a store the way an accepted launch would; return (path, desc)."""
    from localwallet.wallet import WalletDescriptor

    store_path = tmp_path / "watchkey.db"
    store = Store(str(store_path))
    try:
        descriptor = WalletDescriptor.from_key(ZPUB).descriptor
        wallet = store.create_wallet("default", descriptor)
        store.set_active_wallet(wallet.id)
    finally:
        store.close()
    return store_path, descriptor


def test_settings_watch_key_entry_truncated_list_full_on_explicit_read(
    tmp_path: Path,
) -> None:
    _, descriptor = _keyed_store(tmp_path)
    store = Store(str(tmp_path / "watchkey.db"))
    try:
        listing = app.handle_settings_request(store, None, None)
        entry = next(
            e for e in listing["settings"] if e["key"] == app.WATCH_KEY_SETTING
        )
        assert entry["configured"] is True
        assert entry["type"] == "watch_key"
        assert entry["value"] == app._display_truncate(descriptor)
        assert entry["value"] != descriptor  # the list NEVER carries the full key
        # explicit single-key read → the full public value, flagged:
        reveal = app.handle_settings_request(store, app.WATCH_KEY_SETTING, None)
        assert reveal["status"] == "ok"
        assert reveal["settings"][0]["value"] == descriptor
        assert reveal["settings"][0]["revealed"] is True
        # NOT on the write allowlist — a settings-shaped key write is refused
        # and the submitted value is not echoed:
        attempt = app.handle_settings_request(store, app.WATCH_KEY_SETTING, "x" * 30)
        assert attempt["status"] == "rejected"
        assert "x" * 30 not in str(attempt)
    finally:
        store.close()


def test_display_truncation_is_head_dot_dot_tail() -> None:
    assert app._display_truncate("short") == "short"
    long = "z" * 100
    cut = app._display_truncate(long)
    assert cut.startswith(long[:12]) and cut.endswith(long[-8:]) and "…" in cut


# ============ TCK-LAUNCH-003: background model preload + launch checksum ====
#
# The repo machine HAS the real 3GB GGUF: nothing here may load it. Every
# build is a fake — a duck-typed runtime with a gated ``load()`` for the
# flow/pump level, and a fake ``llama_cpp`` module injected into
# ``sys.modules`` for the ModelRuntime lock pin. Checksums run over tiny
# tmp files with a supplied fake pin.

_PRELOAD_THREAD_NAMES = ("model-preload", "model-integrity")


class _LoadBoom(Exception):
    """Error type raised by a fake load() that fails (the flow catches
    Exception broadly; the name only documents intent)."""


class _GateRuntime:
    """Duck-typed ModelRuntime: ``load()`` waits on a gate Event (the
    stand-in for the multi-GB build) and records calls."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail = fail

    def load(self) -> None:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(15), "test gate never opened"
        if self.fail:
            raise _LoadBoom("fake load failure")


class _DoneRuntime:
    """Duck-typed ModelRuntime whose ``load()`` returns at once."""

    def __init__(self) -> None:
        self.calls = 0
        self.loaded = threading.Event()

    def load(self) -> None:
        self.calls += 1
        self.loaded.set()


def _preload_pump(
    flow: app.ModelPreloadFlow,
) -> tuple[list[app.EngineEvent], queue.Queue[Any], threading.Thread]:
    """One REAL pump on a thread with the flow attached; returns the event
    list, the command queue and the pump thread (caller QUITs/joins)."""
    events: list[app.EngineEvent] = []
    emitter = app.EventEmitter(events.append)
    commands: queue.Queue[Any] = queue.Queue()
    thread = threading.Thread(
        target=lambda: app._pump(
            app.AgentLoop(app.stub_generate, {app.IntentName.RESPOND: app._respond_handler}),
            emitter.text,
            commands,
            flow=app.TxFlow(),
            session=app.SendSession(),
            table={},
            emitter=emitter,
            preload=flow,
        ),
        daemon=True,
    )
    thread.start()
    return events, commands, thread


def _request_snapshot(commands: queue.Queue[Any]) -> dict[str, object]:
    """One typed ``/state`` read through the SAME queue. Queue FIFO is the
    drain proof: every marker enqueued BEFORE this request has been
    engine-handled by the time the reply lands."""
    reply: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
    commands.put(app.StateSnapshotRequest(app.STATE_SNAPSHOT_COMMAND, reply))
    return reply.get(15)


def _await_settled(flow: app.ModelPreloadFlow, deadline_s: float = 15.0) -> None:
    """Block until the load marker has been CONSUMED (state flipped off
    ``loading``) AND every spawned worker thread is done — so a checksum
    marker is provably ENQUEUED by the time the caller does a FIFO
    ``/state`` drain. Sound after the flip: both workers spawn inside the
    one PRELOAD_START handling that strictly precedes any marker, so a
    worker seen not-alive then is a finished one (not a not-yet-started
    one)."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline and flow.state == "loading":
        time.sleep(0.02)
    assert flow.state != "loading", "load marker never consumed"
    while time.monotonic() < deadline:
        if not any(
            t.name in _PRELOAD_THREAD_NAMES and t.is_alive()
            for t in threading.enumerate()
        ):
            return
        time.sleep(0.02)
    raise AssertionError("preload workers never settled")


def _quit_and_join(commands: queue.Queue[Any], thread: threading.Thread) -> None:
    commands.put(app.QUIT)
    thread.join(15)
    assert not thread.is_alive()  # bounded exit, always


def test_model_state_flips_loading_to_ready_over_the_pump(tmp_path: Path) -> None:
    """The deliverable-1 pin: with the load GATED, the pump keeps answering
    ``/state`` — additive ``model_state='loading'`` — and the terminal
    marker flips it to ``ready`` (turn_end rides so the browser re-reads)."""
    runtime = _GateRuntime()
    flow = app.ModelPreloadFlow(runtime, model_path=str(tmp_path / "m.gguf"))
    events, commands, thread = _preload_pump(flow)
    try:
        commands.put(app.PRELOAD_START)
        assert runtime.entered.wait(10)  # the loader called runtime.load()
        snap = _request_snapshot(commands)
        assert snap["model_state"] == "loading"  # pump responsive DURING load
        assert app.MODEL_PRELOAD_NOTICE in _texts(events)
        before = len(events)
        runtime.release.set()
        _await_settled(flow)
        snap = _request_snapshot(commands)  # FIFO: the marker was consumed
        assert snap["model_state"] == "ready"
        assert runtime.calls == 1  # exactly ONE build
        assert any(
            e.kind == app.EVENT_TURN_END and e.id > before for e in events
        )  # the flip closes a turn for the re-read
    finally:
        runtime.release.set()
        _quit_and_join(commands, thread)


def test_preload_failure_flips_failed_and_logs_value_free(tmp_path: Path) -> None:
    """A raising load = state ``failed`` + one log line; the session keeps
    running (the next generate re-raises through the existing per-turn
    path — runtime-side, pinned separately)."""
    logs: list[str] = []
    runtime = _GateRuntime(fail=True)
    flow = app.ModelPreloadFlow(
        runtime, model_path=str(tmp_path / "m.gguf"), log_fn=logs.append
    )
    events, commands, thread = _preload_pump(flow)
    try:
        commands.put(app.PRELOAD_START)
        assert runtime.entered.wait(10)
        runtime.release.set()
        _await_settled(flow)
        snap = _request_snapshot(commands)
        assert snap["model_state"] == "failed"
        assert logs == [app._MODEL_PRELOAD_FAILED_LOG]  # log-only, value-free
        assert not any(  # no extra user-facing panic line
            app.MODEL_INTEGRITY_WARNING in e.payload for e in events
        )
    finally:
        _quit_and_join(commands, thread)


def test_checksum_mismatch_warns_and_keeps_serving(tmp_path: Path) -> None:
    """The deliverable-2 decision pin: mismatch = the value-free warning
    LINE (transcript) + LOG entry, state stays honest about the LOAD
    (``ready``) — serving continues."""
    gguf = tmp_path / "m.gguf"
    payload = b"GGUF-ish bytes"
    gguf.write_bytes(payload)
    logs: list[str] = []
    flow = app.ModelPreloadFlow(
        _DoneRuntime(),
        model_path=str(gguf),
        sha256="0" * 64,  # fake pin that can never match
        log_fn=logs.append,
    )
    events, commands, thread = _preload_pump(flow)
    try:
        commands.put(app.PRELOAD_START)
        _await_settled(flow)
        snap = _request_snapshot(commands)  # FIFO drain proof
        assert snap["model_state"] == "ready"  # KEEPS SERVING
        texts = _texts(events)
        assert texts.count(app.MODEL_INTEGRITY_WARNING) == 1  # one line
        assert logs == [app.MODEL_INTEGRITY_WARNING]  # + the log entry
        # value-free: no path, no digests, no filename anywhere
        joined = texts + "".join(logs)
        assert str(gguf) not in joined and "m.gguf" not in joined
        assert hashlib.sha256(payload).hexdigest() not in joined
    finally:
        _quit_and_join(commands, thread)


def test_checksum_pass_is_silent(tmp_path: Path) -> None:
    """The matching pin = the NORMAL case: the check RUNS (thread, FIFO
    drain) and says NOTHING."""
    payload = b"GGUF-fake-pinned-bytes"
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(payload)
    logs: list[str] = []
    flow = app.ModelPreloadFlow(
        _DoneRuntime(),
        model_path=str(gguf),
        sha256=hashlib.sha256(payload).hexdigest(),
        log_fn=logs.append,
    )
    events, commands, thread = _preload_pump(flow)
    try:
        commands.put(app.PRELOAD_START)
        _await_settled(flow)
        snap = _request_snapshot(commands)
        assert snap["model_state"] == "ready"
        assert app.MODEL_INTEGRITY_WARNING not in _texts(events)
        assert logs == []
    finally:
        _quit_and_join(commands, thread)


def test_env_rung_preloads_at_engine_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point (user direction 11): a resolved REAL model rung
    starts the load at ENGINE START — not at the first question. Driven
    end-to-end through run() on the CLI pump with a fake runtime."""
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"GGUF")
    built: list[_FakeRuntime] = []
    monkeypatch.setattr(
        app, "ModelRuntime",
        lambda *a, **k: built.append(_FakeRuntime(*a, **k)) or built[-1],
    )
    monkeypatch.setenv("LOCALWALLET_MODEL_PATH", str(gguf))
    outputs: list[str] = []
    code = app.run(
        ["--zpub", ZPUB], input_fn=lambda _p: "exit", output_fn=outputs.append
    )
    assert code == 0
    assert built and built[0].loaded.wait(10)  # load called at start
    assert app.MODEL_PRELOAD_NOTICE in "\n".join(outputs)


def test_first_query_waits_for_the_in_flight_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The runtime-side serialization pin (deliverable 1's "waits cleanly"):
    a generate() landing mid-build BLOCKS on the construction lock, then
    rides the finished runtime — never a double build, never an error,
    never a silent drop. Fake llama_cpp module (slow ctor), real lock code."""
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"GGUF")
    entered = threading.Event()
    release = threading.Event()
    built: list[str] = []

    class FakeLlama:
        def __init__(self, *, model_path: str, **_kw: object) -> None:
            built.append(model_path)
            entered.set()
            assert release.wait(15), "test gate never opened"

        def __call__(self, **_kw: object) -> dict[str, Any]:
            return {"choices": [{"text": '{"v": 0}'}]}

    class FakeGrammar:
        @classmethod
        def from_string(cls, _text: str) -> FakeGrammar:
            return cls()

    fake_mod = types.ModuleType("llama_cpp")
    fake_mod.Llama = FakeLlama  # type: ignore[attr-defined]
    fake_mod.LlamaGrammar = FakeGrammar  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llama_cpp", fake_mod)

    rt = ModelRuntime(model_path=str(gguf))
    loader = threading.Thread(target=rt.load, daemon=True)
    loader.start()
    assert entered.wait(10)  # the build is IN FLIGHT, lock held

    outcomes: list[str] = []
    query = threading.Thread(
        target=lambda: outcomes.append(rt.generate("prompt")), daemon=True
    )
    query.start()
    query.join(0.3)
    assert query.is_alive()  # the first query WAITS (clean block, no drop)
    assert built == [str(gguf)]  # …and did NOT start a second build
    release.set()
    loader.join(15)
    query.join(15)
    assert built == [str(gguf)]  # exactly one construction, ever
    assert outcomes == ['{"v": 0}']  # the waited-on turn ANSWERS


def test_manifest_pin_lookup_is_exact_and_fail_closed(tmp_path: Path) -> None:
    """The launch checksum verifies ONLY manifest-pinned files; an arbitrary
    env-rung path gets no verdict against no pin (``None`` = skip)."""
    entries = json.loads((app.MODELS_DIR / "manifest.json").read_text())
    pinned = next(e for e in entries if e.get("default") is True)
    assert app._manifest_pin_for(app.MODELS_DIR / "bin" / f"{pinned['name']}.gguf") == (
        pinned["sha256"]
    )
    assert app._manifest_pin_for(tmp_path / "whatever.gguf") is None
    assert app._manifest_pin_for(app.MODELS_DIR / "bin" / "unpinned.gguf") is None

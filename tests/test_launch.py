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

import os
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from localwallet import app
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
        assert any(line.startswith("Privacy notice:") for line in outputs)
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _free_port()
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)
    monkeypatch.setenv("LOCALWALLET_WEB_PORT", str(port))
    try:
        outputs: list[str] = []
        code = app.run(
            ["--stub-llm", "--zpub", ZPUB, "--web"], output_fn=outputs.append
        )
        assert code == 2
        joined = "\n".join(outputs)
        assert "already in use" in joined
        assert "LOCALWALLET_WEB_PORT" in joined  # names the fix
        assert str(port) not in joined  # value-free
    finally:
        blocker.close()


def test_out_of_range_port_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALWALLET_WEB_PORT", "70000")
    outputs: list[str] = []
    code = app.run(
        ["--stub-llm", "--zpub", ZPUB, "--web"], output_fn=outputs.append
    )
    assert code == 2
    assert any(line.startswith("Configuration error:") for line in outputs)


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

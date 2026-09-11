"""TCK-APP-LOG-001 — per-launch error logging + web/CLI output routing.

Pins the log file contract (location, name, value-free content), the
one-code-path error routing to it in BOTH modes, and the web-mode routing
that keeps user-facing narration OUT of the terminal and IN the SSE emitter.

Everything the user should see goes to the browser (web) or the terminal
(CLI); errors/warnings go to the console AND the ``logs/`` file beside the
store DB. Narration is never mirrored to the log, and key material never
reaches it (value-free invariant).
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import pytest

from localwallet import app
from tests.test_e2e_skeleton import ZPUB


def _run_cli(argv: list[str], *, output_fn: Any, input_fn: Any) -> int:
    return app.run(argv, input_fn=input_fn, output_fn=output_fn, interactive=False)


# ------------------------------------------------------------------ log file


def test_log_filename_pattern_and_launch_metadata(tmp_path: Path) -> None:
    """The per-launch log lives in ``logs/`` beside the store DB, named
    ``launch-YYYYMMDD-HHMMSS.log`` (local launch date+time), starting with a
    metadata line that names the mode and never any key material."""
    log = app._Log(str(tmp_path / "store.db"), "web")
    try:
        log.error("boom")
    finally:
        log.close()
    logs = list((tmp_path / "logs").glob("launch-*.log"))
    assert len(logs) == 1
    m = re.fullmatch(r"launch-(\d{8})-(\d{6})\.log", logs[0].name)
    assert m is not None
    text = logs[0].read_text(encoding="utf-8")
    assert "launch mode=web app=local-wallet/0.1.0" in text
    assert "ERROR boom" in text


def test_log_lines_are_bounded_single_line(tmp_path: Path) -> None:
    """Per-line hygiene: newlines are collapsed and oversized lines are
    truncated, so no log line can smuggle a multi-line payload or unbounded
    text."""
    log = app._Log(str(tmp_path / "store.db"), "cli")
    try:
        log.error("line one\nline two")
        log.warning("x" * 5000)
    finally:
        log.close()
    (log_path,) = (tmp_path / "logs").glob("launch-*.log")
    lines = log_path.read_text(encoding="utf-8").splitlines()
    data = [ln for ln in lines if "ERROR" in ln or "WARN" in ln]
    assert len(data) == 2
    assert all("\n" not in ln for ln in data)
    assert all(len(ln) <= 1100 for ln in data)  # timestamp+level+bounded payload


def test_log_dir_failure_degrades_to_console_only_never_fatal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Directory-creation failure (store_path's parent is a file) must not be
    fatal: a one-line console note, writes no-op, nothing crashes."""
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    log = app._Log(str(blocker / "db"), "cli")  # parent is a FILE
    assert log._fh is None
    log.error("boom")  # no crash, no traceback
    log.close()
    assert "could not open the error log" in capsys.readouterr().err
    assert list(tmp_path.glob("**/launch-*.log")) == []


# ------------------------------------------------------------------ CLI mode


def test_cli_narration_goes_to_terminal_and_error_to_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI mode is unchanged: narration prints to the terminal, and a config
    error ALSO lands in the log file (one code path) — value-free (the
    private key never reaches the log)."""
    from tests.test_e2e_skeleton import XPRV

    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv(app.AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.setenv(app.ZPUB_ENV_VAR, XPRV)  # private key — always refused
    outputs: list[str] = []
    code = _run_cli(["--stub-llm"], output_fn=outputs.append, input_fn=lambda _p: "exit")
    assert code == 2
    joined = "\n".join(outputs)
    assert "Watch key rejected" in joined  # CLI narration → terminal
    assert XPRV not in joined  # value-free on the terminal
    (log_path,) = (tmp_path / "logs").glob("launch-*.log")
    text = log_path.read_text(encoding="utf-8")
    assert "ERROR Watch key rejected" in text  # the SAME error is logged
    assert XPRV not in text  # key material never reaches the log


def test_cli_narration_is_never_mirrored_to_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI narration (the privacy banner) prints to the terminal but is NOT
    mirrored into the log — the log holds errors/warnings only."""
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv(app.AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")
    outputs: list[str] = []
    code = _run_cli(
        ["--stub-llm", "--zpub", ZPUB], output_fn=outputs.append,
        input_fn=lambda _p: "exit",
    )
    assert code == 0
    assert any(line.startswith("Privacy notice:") for line in outputs)  # terminal
    (log_path,) = (tmp_path / "logs").glob("launch-*.log")
    text = log_path.read_text(encoding="utf-8")
    assert "Privacy notice:" not in text  # narration never mirrored
    assert ZPUB not in text


# ------------------------------------------------------------------- web mode


def _launch_web(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> tuple[threading.Thread, list[str], dict[str, Any]]:
    from localwallet.ui.web.server import serve_web  # noqa: F401 (start seam)

    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(tmp_path / "web.db"))
    monkeypatch.setenv(app.AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")
    monkeypatch.delenv(app.UI_ENV_VAR, raising=False)
    outputs: list[str] = []
    capture: dict[str, Any] = {}
    gate = threading.Event()

    def on_server(server: Any) -> None:
        capture["server"] = server
        gate.set()

    thread = threading.Thread(
        target=lambda: capture.update(
            code=app.run(argv, output_fn=outputs.append, on_web_server=on_server)
        ),
        daemon=True,
    )
    thread.start()
    assert gate.wait(30), "web server never started"
    return thread, outputs, capture


class _Stream:
    def __init__(self, server: Any) -> None:
        import socket

        self.sock = socket.create_connection(
            ("127.0.0.1", server.httpd.server_address[1]), timeout=10
        )
        self.sock.sendall(
            (
                "GET /events HTTP/1.0\r\nHost: 127.0.0.1\r\n"
                f"X-Auth-Token: {server.token}\r\n\r\n"
            ).encode()
        )
        self.buf = b""

    def read_until(self, needle: bytes, timeout: float = 15.0) -> bytes:
        import time

        deadline = time.monotonic() + timeout
        while needle not in self.buf:
            self.sock.settimeout(max(0.1, deadline - time.monotonic()))
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError(f"stream closed before {needle!r}")
            self.buf += chunk
        return self.buf

    def close(self) -> None:
        self.sock.close()


def test_web_narration_is_in_the_emitter_not_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deliverable 5 pin: a web turn's narration line appears in the SSE
    emitter events but is ABSENT from the terminal (stdout) output."""
    thread, outputs, capture = _launch_web(
        tmp_path, monkeypatch, ["--stub-llm", "--zpub", ZPUB, "--web"]
    )
    server = capture["server"]
    try:
        import http.client

        conn = http.client.HTTPConnection(
            "127.0.0.1", server.httpd.server_address[1], timeout=10
        )
        conn.request(
            "POST", "/turn", b'{"text":"hello there"}',
            {"Content-Type": "application/json", "X-Auth-Token": server.token},
        )
        response = conn.getresponse()
        response.read()
        conn.close()
        assert response.status == 202

        stream = _Stream(server)
        frame = stream.read_until(b"event: turn_end")
        stream.close()
        # A turn's narration reached the SSE emitter:
        assert b"event: text" in frame
    finally:
        server.stop()
        thread.join(15)
    assert capture.get("code") == 0
    # And the terminal carried NONE of the turn narration (only the launch
    # block: URL + token + shutdown):
    assert not any("hello there" in line or "(stub model" in line for line in outputs)
    assert any(line.startswith("Web UI:") for line in outputs)


# ---------------------------------------- TCK-UX-012(a): web startup lines = separate bubbles


def test_web_output_closes_each_startup_line_as_its_own_turn(
    tmp_path: Path,
) -> None:
    """In WEB mode every narration line (the whole buffered banner, then any
    post-bind startup line) is followed by a ``turn_end`` marker: the browser
    renders one bubble per turn, so WITHOUT the closer the banner lines — and
    the first reply after them — all glom into a single merged bubble. The
    engine already emitted one event per line (verified); this pins the
    web-side delimiter that actually separates them."""
    events: list[Any] = []
    emitter = app.EventEmitter(events.append)
    log = app._Log(str(tmp_path / "store.db"), "web")
    terminal_lines: list[str] = []
    out = app._Output(web=True, terminal=terminal_lines.append, log=log)
    try:
        # Pre-bind lines buffer, then flush at bind_emitter.
        out("Privacy notice: x")
        out("Background watch: off. Change it in settings.")
        out.bind_emitter(emitter)
        # Post-bind line routes directly.
        out("Type a message — 'exit' or Ctrl-D quits.")
    finally:
        log.close()
    assert [(e.kind, e.payload) for e in events] == [
        (app.EVENT_TEXT, "Privacy notice: x"),
        (app.EVENT_TURN_END, ""),
        (app.EVENT_TEXT, "Background watch: off. Change it in settings."),
        (app.EVENT_TURN_END, ""),
        (app.EVENT_TEXT, "Type a message — 'exit' or Ctrl-D quits."),
        (app.EVENT_TURN_END, ""),
    ]
    assert terminal_lines == []  # web narration never reaches the terminal


def test_cli_output_prints_lines_with_no_markers(tmp_path: Path) -> None:
    """CLI mode is byte-identical: each line goes to the terminal, no emitter
    / turn_end machinery is involved at all."""
    log = app._Log(str(tmp_path / "store.db"), "cli")
    lines: list[str] = []
    out = app._Output(web=False, terminal=lines.append, log=log)
    try:
        out("Privacy notice: x")
        out("Background watch: off. Change it in settings.")
    finally:
        log.close()
    assert lines == [
        "Privacy notice: x",
        "Background watch: off. Change it in settings.",
    ]

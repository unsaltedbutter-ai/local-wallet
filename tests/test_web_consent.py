"""TCK-PRIVACY-001B (WEB half): the "Use public server" consent button.

The web UI's ONE trigger of a public-backend choice rides
``POST /consent`` → a typed :class:`app.ConsentRequest` THROUGH the engine
pump → the EXISTING :func:`app.set_public_backend_consent` seam, executed on
the ENGINE thread (security-review constraint: the HTTP transport thread
never touches the seam, the store, or the scan). Pins here:

* the route is middleware-identical to every other POST (Host → Origin →
  token; value-free bodies), and the closed status maps honestly
  (``loading``/``recorded``→200, unwired→503 ``unavailable`` — never a lie);
* the press records the ONB-006 marker and releases the held first-run scan
  end to end over a REAL unprovisioned web session (the
  tests/test_privacy_consent.py launch harness);
* NOTHING else is consent: asking a balance leaves the gate held (zero
  calls) and the marker unset, and the client's pane-close path contains no
  request at all (test_web_render_contract.py source-pins that half).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from localwallet import app
from localwallet.store import Store
from localwallet.ui.onboarding import BACKEND_CHOICE_PUBLIC, BACKEND_CHOICE_SETTING
from tests.test_e2e_skeleton import ZPUB
from tests.test_privacy_consent import _fake_generate, _launch_first_run
from tests.test_web_server import _request, _settings_server, _Stream


def _read_marker(path: Path) -> str | None:
    """The consent record as the ENGINE wrote it (opened only after the
    engine thread is done — the tests never race the pump for the store)."""
    store = Store(path)
    try:
        return store.get_setting(BACKEND_CHOICE_SETTING)
    finally:
        store.close()


# ------------------------------------------------- transport-contract pins


def test_consent_route_middleware_status_and_record(tmp_path: Path) -> None:
    """Token/Host/Origin apply like every other POST; the reply is the
    closed value-free status. This harness engine has a store but NO held
    scan: the honest answer is ``recorded`` (choice stands, nothing to
    release — never ``loading``, the F2 contract) — and the marker really
    lands in the engine's store."""
    server = _settings_server(tmp_path)
    status, _h, _d, _r = _request(server, "POST", "/consent")
    assert status == 401  # token gate before anything else
    status, _h, _d, _r = _request(
        server, "POST", "/consent", token=server.token,
        headers={"Host": "example.com"},
    )
    assert status == 400  # Host allowlist first, like every request
    status, _h, _d, _r = _request(
        server, "POST", "/consent", token=server.token,
        headers={"Origin": "http://evil.example"},
    )
    assert status == 403  # same-origin belt braces
    status, _h, data, _r = _request(
        server, "POST", "/consent", token=server.token
    )
    assert status == 200
    body = json.loads(data)
    assert body["schema"] == "consent/1"
    assert body["status"] == "recorded"
    server.stop()
    assert _read_marker(tmp_path / "settings.db") == BACKEND_CHOICE_PUBLIC


def test_consent_maps_loading_only_from_the_seam(tmp_path: Path) -> None:
    """``loading`` (the scan-started report) is marshalled, never judged,
    by the transport; anything outside the closed pair answers 503 — the
    no-store ``unavailable`` included (nothing was recorded, no record
    written)."""
    server = _settings_server(tmp_path)
    try:
        for closed, code in (("loading", 200), ("recorded", 200), ("unavailable", 503)):
            server.handle.request_consent = (
                lambda _t, _s=closed: {"schema": "consent/1", "status": _s}
            )
            status, _h, data, _r = _request(
                server, "POST", "/consent", token=server.token
            )
            assert status == code
            assert json.loads(data)["status"] == closed
    finally:
        server.stop()


def test_dead_engine_consent_is_a_value_free_503(tmp_path: Path) -> None:
    server = _settings_server(tmp_path)
    try:
        server.handle.error = RuntimeError("bootstrap died")
        status, _h, data, _r = _request(
            server, "POST", "/consent", token=server.token
        )
        assert status == 503 and b"engine busy" in data
    finally:
        server.stop()


def test_consent_runs_on_the_engine_thread(tmp_path: Path) -> None:
    """Security-review constraint, proven structurally: the seam executes
    where the PUMP runs, never on the HTTP handler thread — the thread name
    is captured INSIDE the seam call."""
    server = _settings_server(tmp_path)
    seen: dict[str, str] = {}
    real = app.set_public_backend_consent

    def spy(store: Any, scan: Any = None) -> bool:
        seen["thread"] = threading.current_thread().name
        return real(store, scan)

    try:
        app.set_public_backend_consent = spy  # type: ignore[assignment]
        _request(server, "POST", "/consent", token=server.token)
        assert seen["thread"] == server.handle.thread.name
    finally:
        app.set_public_backend_consent = real  # type: ignore[assignment]
        server.stop()


# ------------------------------------------------------ the real end-to-end


def test_consent_press_records_releases_and_nothing_else_consents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole seam over a REAL first-run web session: zpub in → the gate
    HOLDS (zero calls); a balance question changes NOTHING (asking ≠
    consent); the /consent PRESS records the marker, releases the held scan,
    and the EXISTING /state machinery carries the flip with zero new client
    inference (scan_state leaves awaiting_backend, privacy_mode resolves to
    public). After the session ends, the durable record says public."""
    thread, _outputs, capture = _launch_first_run(
        tmp_path, monkeypatch, _fake_generate()
    )
    server = capture["server"]
    stream = _Stream(server)
    try:
        stream.read_head()
        status, _h, _d, _r = _request(
            server, "POST", "/watchkey", {"key": ZPUB}, token=server.token
        )
        assert status == 200
        snap = json.loads(_request(server, "GET", "/state", token=server.token)[2])
        assert snap["scan_state"] == "awaiting_backend"
        assert snap["privacy_mode"] == "awaiting_backend"
        assert capture["calls"] == []

        # A balance question is NOT consent: answer served, gate still held.
        status, _h, _d, _r = _request(
            server, "POST", "/turn", {"text": "/balance"}, token=server.token
        )
        assert status == 202
        stream.read_until(b"turn_end", timeout=15)
        assert capture["calls"] == []

        # The press: the answer is the seam's own truth — the held scan
        # starts loading (F2: only ever reported on the engine's True).
        status, _h, data, _r = _request(
            server, "POST", "/consent", token=server.token
        )
        assert status == 200
        assert json.loads(data) == {"schema": "consent/1", "status": "loading"}
        # The pump closed a turn so the client re-reads /state (SSE wiring).
        stream.read_until(b"turn_end", timeout=15)
        # The released scan proceeds: the typed snapshot moves off the hold
        # on the fields the chips ALREADY ride (no new /state field).
        for _ in range(50):
            snap = json.loads(
                _request(server, "GET", "/state", token=server.token)[2]
            )
            if snap["scan_state"] != "awaiting_backend":
                break
            time.sleep(0.1)
        assert snap["scan_state"] != "awaiting_backend"
        assert snap["privacy_mode"] == "public"
    finally:
        stream.close()
        server.stop()
        thread.join(15)
    assert capture.get("code") == 0
    store_path = capture["store_path"]
    assert _read_marker(store_path) == BACKEND_CHOICE_PUBLIC

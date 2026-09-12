"""TCK-WEB-002 server half (ADR-0024): loopback HTTP/SSE over the engine pump.

Everything runs against a REAL server on 127.0.0.1:0 and a REAL engine
(``start_engine`` with a stub bootstrap, reusing the pump-test harness
patterns — ``_run_turn`` replaced by an echo so turns are deterministic and
model-free). Pins, per the ticket's done-when list:

* token gate (401 without ``X-Auth-Token`` on every DATA-BEARING endpoint)
  and the HTTP/1.0 transport (stay-and-document, §2);
* TCK-WEB-007 bootstrap: the shell (``GET /``, ``/index.html``) and
  ``GET /static/*`` serve WITHOUT the token (the island in the page IS the
  token delivery), Host allowlist + CSP still enforced on them first;
* POST /turn and /action route through the FULL pump pipeline as typed
  lines (no handler/flow bypass — structural source pin + behavior pin);
* event fan-out to all subscribers with monotonic ids;
* ring-buffer replay via ``Last-Event-ID`` after reconnect;
* security-review pins: a stale/absent cursor after MORE retained events
  than the per-connection queue cap still makes progress (replay streams
  straight to the socket — never dead-on-arrival, no gap/duplicate at the
  replay/live seam, dropped subs reaped on the SAME publish); negative
  Content-Length answered as a bounded 4xx (never a read-until-EOF thread
  park); web bind failure exits 2 with the clean value-free line;
* bounded per-connection queue: overflow CLOSES that connection, the engine
  never blocks and other subscribers survive;
* ``: ping`` heartbeat frame;
* sentinel shutdown: ``stop()`` returns with an SSE thread parked on an open
  stream (no ``block_on_close`` hang) and joins the engine thread;
* no CORS headers anywhere, no ``Set-Cookie``, no tracebacks on stderr, the
  token in NO log line / error body / URL / ``/state`` response;
* missing static dir = graceful 404; token JSON-island injection in the
  served index.html;
* ``run()`` web wiring: ``--web`` / ``LOCALWALLET_UI=web``, launch URL and
  token printed on SEPARATE lines (URL token-free), full wiring through
  ``start_engine``.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.app import EngineContext, EngineEvent
from localwallet.chain import PriceUnavailableError
from localwallet.config import Settings
from localwallet.protocol import IntentName
from localwallet.store import Store
from localwallet.tx.flow import TxFlow, TxFlowStatus
from localwallet.ui.web import server as webserver
from localwallet.ui.web.server import serve_web, sse_frame
from localwallet.wallet import WalletDescriptor
from tests.test_e2e_skeleton import SEND_RECIPIENT, ZPUB

# ------------------------------------------------------------------ harness


def _bootstrap() -> EngineContext:
    table = {
        IntentName.RESPOND: app._respond_handler,
        IntentName.CLARIFY: app._clarify_handler,
    }
    return EngineContext(
        loop=AgentLoop(app.stub_generate, table),
        flow=TxFlow(),
        session=app.SendSession(),
        table=table,
    )


@pytest.fixture
def echo_turns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Turn path → echo recorder (same seam as tests/test_engine_pump.py):
    proves submit → pump → _run_turn → emitter → bus → SSE end to end."""
    seen: list[str] = []

    def fake_turn(*args: Any, **kwargs: Any) -> None:
        line: str = args[3]
        output_fn: Callable[[str], None] = args[4]
        seen.append(line)
        output_fn(f"echo:{line}")

    monkeypatch.setattr(app, "_run_turn", fake_turn)
    return seen


@pytest.fixture
def serve(tmp_path: Path) -> Any:
    """Serve-factory fixture; every created server is stopped on teardown."""
    servers: list[Any] = []

    def _serve(**options: Any) -> Any:
        options.setdefault("static_dir", tmp_path / "static")
        server = serve_web(_bootstrap, **options)
        servers.append(server)
        return server

    yield _serve
    for server in servers:
        server.stop()


def _conn(port: int) -> http.client.HTTPConnection:
    return http.client.HTTPConnection("127.0.0.1", port, timeout=10)


def _request(
    server: Any,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    token: str | None = None,
    headers: dict[str, str] | None = None,
    raw_body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes, http.client.HTTPResponse]:
    conn = _conn(server.httpd.server_address[1])
    send_headers = dict(headers or {})
    if token is not None:
        send_headers["X-Auth-Token"] = token
    payload = raw_body if raw_body is not None else (
        json.dumps(body).encode() if body is not None else None
    )
    if payload is not None:
        send_headers.setdefault("Content-Type", "application/json")
    conn.request(method, path, payload, send_headers)
    response = conn.getresponse()
    data = response.read()
    got_headers = {k.lower(): v for k, v in response.getheaders()}
    conn.close()
    return response.status, got_headers, data, response


class _Stream:
    """Raw socket SSE client: full control over WHEN (whether) to read."""

    def __init__(
        self,
        server: Any,
        *,
        token: str | None = "auto",
        last_event_id: int | None = None,
    ) -> None:
        self.sock = socket.create_connection(
            ("127.0.0.1", server.httpd.server_address[1]), timeout=10
        )
        headers = ["GET /events HTTP/1.0", "Host: 127.0.0.1"]
        if token == "auto":
            token = server.token
        if token is not None:
            headers.append(f"X-Auth-Token: {token}")
        if last_event_id is not None:
            headers.append(f"Last-Event-ID: {last_event_id}")
        self.sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
        self.buf = b""

    def read_head(self, timeout: float = 10.0) -> str:
        deadline = time.monotonic() + timeout
        while b"\r\n\r\n" not in self.buf:
            self.sock.settimeout(max(0.1, deadline - time.monotonic()))
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError(self.buf)
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        return head.decode("latin-1")

    def read_until(self, needle: bytes, timeout: float = 10.0) -> bytes:
        deadline = time.monotonic() + timeout
        while needle not in self.buf:
            self.sock.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError as exc:
                raise AssertionError(
                    f"no {needle!r} within {timeout}s: {self.buf[:400]!r}"
                ) from exc
            if not chunk:
                raise EOFError(f"stream closed before {needle!r}")
            self.buf += chunk
        return self.buf

    def read_to_eof(self, timeout: float = 15.0) -> int:
        """Drain until the server closed; returns total bytes."""
        total = 0
        deadline = time.monotonic() + timeout
        while True:
            self.sock.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError as exc:  # pragma: no cover - flake guard
                raise AssertionError("stream never reached EOF") from exc
            if not chunk:
                return total + len(chunk)
            total += len(chunk)

    def close(self) -> None:
        self.sock.close()


# ------------------------------------------------------------- token gate §6


def test_every_endpoint_requires_token_and_replies_http_1_0(serve: Any) -> None:
    server = serve()
    port = server.httpd.server_address[1]
    assert port != 0  # ephemeral port bound (never fixed/predictable)
    # DATA-BEARING endpoints only — the shell/static are the token-island
    # bootstrap (TCK-WEB-007) and are pinned open below.
    cases = [
        ("GET", "/state", None),
        ("GET", "/settings", None),
        ("POST", "/turn", {"text": "hi"}),
        ("POST", "/action", {"utterance": "confirm"}),
        ("POST", "/settings", {"key": "gap_limit", "value": "5"}),
        # TCK-LAUNCH-001: the first-run watch-key entry is data-bearing too.
        ("POST", "/watchkey", {"key": "zpub-some-key"}),
        # TCK-PRIVACY-001B: the public-consent press is a mutation too.
        ("POST", "/consent", None),
        # TCK-QR-001: the QR encoder is data-bearing too (token-gated GET).
        ("GET", "/qr?value=bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq", None),
    ]
    for method, path, body in cases:
        status, headers, data, response = _request(server, method, path, body)
        assert status == 401, path
        assert response.version == 10  # HTTP/1.0 stay-and-document (ADR-0024 §2)
        assert server.token.encode() not in data  # error body NEVER carries it
        assert "www-authenticate" not in headers  # no browser auth prompt
    stream = _Stream(server, token=None)
    assert "401" in stream.read_head().splitlines()[0]
    stream.close()
    # The correct header gets through:
    status, _headers, data, _r = _request(
        server, "GET", "/state", token=server.token
    )
    assert status == 200
    assert json.loads(data)["last_event_id"] >= 0


def test_token_never_travels_in_a_url_or_appears_in_logs(
    serve: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    server = serve()
    _request(server, "GET", "/state")  # 401
    _request(server, "GET", "/")  # 200 — island page carries the token IN BODY only
    _request(server, "GET", "/nope", token="wrong-token")  # 404
    _request(server, "POST", "/turn", {"text": "hello"}, token=server.token)
    stream = _Stream(server)
    stream.read_head()
    stream.close()  # abrupt mid-stream close: the classic traceback bait
    captured = capsys.readouterr()
    assert server.token not in captured.out
    assert server.token not in captured.err
    assert "Traceback" not in captured.err  # broken pipes are swallowed (§5)


# ------------------------------------------------------ turn/action pump §8


def test_turn_routes_through_full_pump_and_fans_out_to_every_subscriber(
    serve: Any, echo_turns: list[str]
) -> None:
    server = serve(heartbeat_s=30.0)
    a, b = _Stream(server), _Stream(server)
    a.read_head(), b.read_head()
    status, _h, _d, _r = _request(
        server, "POST", "/turn", {"text": "hello web"}, token=server.token
    )
    assert status == 202
    for stream in (a, b):
        frame = stream.read_until(b"echo:hello web")
        assert b"event: text\ndata: echo:hello web\n\n" in frame
        # completion marker per turn (ids strictly monotonic across streams)
        frame = stream.read_until(b"event: turn_end")
        ids = [int(i) for i in re.findall(rb"id: (\d+)", frame)]
        assert ids == sorted(ids)
    assert echo_turns == ["hello web"]  # FULL _run_turn path, never a bypass
    a.close(), b.close()


def test_action_utterance_is_a_typed_line_through_the_same_pipeline(
    serve: Any, echo_turns: list[str]
) -> None:
    """ADR-0024 §8: a button's canonical utterance is submitted as a typed
    line; the gate sees the exact phrase — no endpoint calls a handler."""
    server = serve(heartbeat_s=30.0)
    stream = _Stream(server)
    stream.read_head()
    status, _h, _d, _r = _request(
        server, "POST", "/action", {"utterance": "confirm"}, token=server.token
    )
    assert status == 202
    assert b"data: echo:confirm\n\n" in stream.read_until(b"echo:confirm")
    assert echo_turns == ["confirm"]  # the raw utterance reached the pump
    stream.close()
    source = Path(webserver.__file__).read_text(encoding="utf-8")
    # Structural bypass pin: the transport module never even names a mutator.
    for forbidden in ("TxFlow", "ConfirmGate", ".confirm(", "_run_turn("):
        assert forbidden not in source, forbidden


def test_action_requires_utterance_field(serve: Any) -> None:
    server = serve()
    status, _h, data, _r = _request(
        server, "POST", "/action", {"text": "confirm"}, token=server.token
    )
    assert status == 400
    assert b"utterance" in data


def test_dead_engine_turn_and_action_are_a_value_free_503(serve: Any) -> None:
    """TCK-WEB-016 (a): POST /turn//action check ``engine.error`` like
    /resync and /consent already do — a dead engine gets the honest,
    value-free 503 BEFORE submit(), never a 202 into a queue nobody
    drains (the "first message lost" stranding). Nothing reaches the
    command queue and no internals ride the body."""
    server = serve()
    try:
        server.handle.error = RuntimeError("bootstrap died")
        for path, body in (
            ("/turn", {"text": "hello"}),
            ("/action", {"utterance": "confirm"}),
        ):
            status, _h, data, _r = _request(
                server, "POST", path, body, token=server.token
            )
            assert status == 503 and b"engine busy" in data
            assert b"bootstrap" not in data and b"died" not in data
        assert server.handle.commands.qsize() == 0  # refused BEFORE submit()
    finally:
        server.stop()


def test_pump_death_sets_handle_error_and_closes_the_turn(
    serve: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TCK-WEB-016 (b): a turn raising OUTSIDE the contained path kills the
    pump — the ``start_engine`` guard must set ``handle.error`` AND emit
    ``turn_end`` so no client is stranded mid-turn. Ordering (error first,
    marker after) is pinned through the real bus: by the time the turn_end
    frame is observable on the SSE socket, the handle is already flagged
    (same-thread happens-before), so the client's /state re-read and any
    retry hit the 503 fast-fails, never a doomed 202."""

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("turn exploded")

    monkeypatch.setattr(app, "_run_turn", explode)
    server = serve(heartbeat_s=30.0)
    stream = _Stream(server)
    stream.read_head()
    status, _h, _d, _r = _request(
        server, "POST", "/turn", {"text": "kill the pump"}, token=server.token
    )
    assert status == 202  # the engine was alive when the line was queued
    stream.read_until(b"event: turn_end")  # raises on stranding — the pin
    assert server.handle.error is not None
    assert isinstance(server.handle.error, RuntimeError)
    thread = server.handle.thread
    assert thread is not None
    thread.join(10)
    assert not thread.is_alive()  # dead, loudly: the joiner sees the error
    stream.close()


def test_user_text_echo_fans_out_to_every_tab_and_replays(
    serve: Any, echo_turns: list[str]
) -> None:
    """TCK-WEB-011: the submitter's OWN utterance rides the same SSE fan-out
    as server messages — /turn free text and /action canonical utterances
    both arrive typed as ``user_text`` (verbatim payload, the frame carries
    its event id so the submitting tab can dedupe its local echo), each echo
    precedes its reply, and the ring buffer replays echoes to a late tab via
    Last-Event-ID exactly like any other event. The transport needs no kind
    registration: the frame writer is generic pass-through (pinned here)."""
    server = serve(heartbeat_s=30.0)
    a, b = _Stream(server), _Stream(server)
    a.read_head(), b.read_head()
    _request(server, "POST", "/turn", {"text": "hello web"}, token=server.token)
    _request(server, "POST", "/action", {"utterance": "confirm"}, token=server.token)
    for stream in (a, b):
        frame = stream.read_until(b"data: echo:confirm")
        assert b"event: user_text\ndata: hello web\n\n" in frame
        assert b"event: user_text\ndata: confirm\n\n" in frame
        # The echo rides BEFORE the turn it triggered.
        assert frame.index(b"event: user_text\ndata: hello web") < frame.index(
            b"data: echo:hello web"
        )
        ids = re.findall(rb"id: (\d+)\nevent: user_text", frame)
        assert len(ids) == 2  # every echo carries its monotonic id (client dedupe)
    # A tab that connected AFTER both turns replays the echoes from the ring:
    late = _Stream(server, last_event_id=0)
    buf = late.read_until(b"data: echo:confirm")
    assert b"event: user_text\ndata: hello web\n\n" in buf
    assert b"event: user_text\ndata: confirm\n\n" in buf
    a.close(), b.close(), late.close()


# ----------------------------------------------------------------- SSE §5


def test_ring_buffer_replays_after_reconnect_via_last_event_id(
    serve: Any, echo_turns: list[str]
) -> None:
    server = serve(heartbeat_s=60.0)  # no heartbeats muddying frame ids
    stream = _Stream(server)
    stream.read_head()
    _request(server, "POST", "/turn", {"text": "one"}, token=server.token)
    first_buf = stream.read_until(b"turn_end")
    # Cursor at the END of turn one (the max id of its user_text/text/
    # turn_end frames) — a cursor set at the start of the turn (its
    # user_text echo, once the min) would legitimately replay echo:one.
    first_id = max(int(i) for i in re.findall(rb"id: (\d+)", first_buf))
    _request(server, "POST", "/turn", {"text": "two"}, token=server.token)
    second_buf = stream.read_until(b"echo:two")
    seen_id = max(int(i) for i in re.findall(rb"id: (\d+)", first_buf + second_buf))
    stream.close()
    # One turn while nobody is listening — the ring must bridge the gap:
    _request(server, "POST", "/turn", {"text": "three"}, token=server.token)
    reconnect = _Stream(server, last_event_id=seen_id)
    buf = reconnect.read_until(b"echo:three")
    ids = [int(i) for i in re.findall(rb"id: (\d+)", buf)]
    assert ids and min(ids) > seen_id  # nothing already delivered is repeated
    assert b"echo:three" in buf  # the missed turn is replayed
    assert b"echo:one" not in buf
    # And a cursor from long ago replays from the retained window:
    old = _Stream(server, last_event_id=first_id)
    buf = old.read_until(b"echo:three")
    assert b"echo:two" in buf and b"echo:three" in buf
    assert b"echo:one" not in buf  # strictly AFTER Last-Event-ID
    reconnect.close(), old.close()


def test_heartbeat_ping_frame_on_idle(serve: Any) -> None:
    server = serve(heartbeat_s=0.05)
    stream = _Stream(server)
    stream.read_head()
    assert b": ping\n\n" in stream.read_until(b": ping\n\n", timeout=5.0)
    stream.close()


def test_bounded_queue_overflow_closes_the_slow_connection_not_the_engine(
    serve: Any, echo_turns: list[str]
) -> None:
    """A browser that stops reading is dropped when its bounded queue
    fills; the engine never blocks and a fresh connection still works (§5)."""
    server = serve(
        queue_maxsize=1, send_timeout_s=2.0, heartbeat_s=60.0
    )  # tiny queue + fast send-timeout
    big = "x" * 900_000  # ~900 KB frames: ~20 MB total > any loopback buffer
    slow = _Stream(server)
    slow.read_head()  # headers only — then NEVER read again (the stuck tab)
    for i in range(22):
        _request(server, "POST", "/turn", {"text": f"{i} {big}"}, token=server.token)
    # The overflow is bookkeeping-visible: the dead subscriber was removed.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        status, _h, data, _r = _request(
            server, "GET", "/state", token=server.token
        )
        assert status == 200  # the engine is alive: /state answers THROUGH it
        if json.loads(data)["subscribers"] == 0:
            break
        time.sleep(0.05)
    else:  # pragma: no cover - flake guard
        raise AssertionError("stuck subscriber was never dropped")
    assert echo_turns[0].startswith("0 xxx")  # turns kept flowing meanwhile
    # The dropped client reached EOF (its writer died on write/timeout):
    assert slow.read_to_eof(timeout=15.0) > 0
    slow.close()


# ------------------------------------------- security-review: replay livelock


def test_bus_stale_cursor_subscriber_is_never_dead_on_arrival() -> None:
    """The MEDIUM fix (server.py:138-152): ``subscribe`` snapshots the ring
    and returns it for DIRECT-to-socket replay, never through the bounded
    queue. More retained events than the queue cap CANNOT overflow a fresh
    subscriber into dead-on-arrival (the old reconnect-with-same-cursor
    livelock)."""
    bus = webserver._Bus(ring_size=2048, maxsize=512)
    for i in range(1, 1300):  # more than double the per-connection queue cap
        bus.publish(EngineEvent(id=i, kind="text", payload=f"e{i}"))
    # A brand-new subscriber with a stale (absent -> 0) cursor:
    sub, replay = bus.subscribe(0)
    assert sub.dead is False  # NOT dead-on-arrival
    assert [e.id for e in replay] == list(range(1, 1300))  # full retained window
    assert sub.events.empty()  # replay never touched the bounded queue
    # Gap-free/duplicate-free seam: everything published from here on flows
    # through the live queue (the ring snapshot is frozen).
    bus.publish(EngineEvent(id=1300, kind="text", payload="live"))
    assert sub.events.get_nowait().id == 1300
    # A mid-window cursor replays strictly AFTER it, still not dead:
    sub2, replay2 = bus.subscribe(1299)
    assert sub2.dead is False
    assert [e.id for e in replay2] == [1300]


def test_dropped_subscriber_reaped_on_the_same_publish() -> None:
    """The ordering nit (server.py:149-151): the old ``subscribe`` appended a
    sub to ``_subs`` AFTER dropping it, so an overflow-dropped subscriber
    lingered until the NEXT publish. Now the overflow happens in
    ``publish`` under the lock and removes it immediately."""
    bus = webserver._Bus(ring_size=8, maxsize=1)
    sub, _ = bus.subscribe(0)
    bus.publish(EngineEvent(id=1, kind="text", payload="a"))
    bus.publish(EngineEvent(id=2, kind="text", payload="b"))  # overflow -> drop
    assert sub.dead is True
    assert bus.snapshot()["subscribers"] == 0  # reaped THIS publish, not next


def test_stale_cursor_sse_client_receives_full_replay_end_to_end(serve: Any) -> None:
    """Integration pin for the MEDIUM fix: over a REAL server, a fresh SSE
    connection with no ``Last-Event-ID`` after 600 retained events (>512
    queue cap) receives every frame in order — proof it is not
    dead-on-arrival — and the live seam is contiguous (no gap/duplicate)."""
    server = serve(heartbeat_s=60.0)
    n = 600
    for i in range(1, n + 1):
        server.bus.publish(EngineEvent(id=i, kind="text", payload=f"e{i}"))
    assert server.bus.snapshot()["buffered_events"] == n
    stream = _Stream(server)  # absent cursor -> replay_after=0
    stream.read_head()
    buf = stream.read_until(b"data: e600\n\n")
    ids = [int(x) for x in re.findall(rb"id: (\d+)", buf)]
    assert ids == list(range(1, n + 1))  # full replay, gap-free, no dupes
    # A live event published right after the replay flows through the seam:
    server.bus.publish(EngineEvent(id=n + 1, kind="text", payload="live"))
    buf = stream.read_until(b"data: live\n\n")
    ids = [int(x) for x in re.findall(rb"id: (\d+)", buf)]
    assert ids == list(range(1, n + 2))  # replay + live strictly contiguous
    stream.close()


# ------------------------------------------- security-review: negative CL


def test_negative_content_length_is_bounded_4xx_and_frees_the_thread(
    serve: Any,
) -> None:
    """The LOW fix (server.py:325-338): ``int("-5")`` used to pass the
    ``> MAX_BODY_BYTES`` gate and reach ``rfile.read(-5)`` — a
    read-UNTIL-EOF that parks the handler thread. Clamped to >=0 now, so
    the (empty) body fails JSON parsing and the client gets a clean 400
    WITHOUT ever sending a body or the server hanging on read."""
    server = serve()
    conn = _conn(server.httpd.server_address[1])
    conn.putrequest("POST", "/turn")
    conn.putheader("X-Auth-Token", server.token)
    conn.putheader("Content-Length", "-5")
    conn.endheaders()  # NO body: read(-5) would block here until timeout
    response = conn.getresponse()
    assert response.status == 400  # bounded 4xx, not a parked thread
    response.read()
    conn.close()
    # The handler thread was NOT stuck on the parked read: the server still
    # serves other requests immediately.
    status, _h, _d, _r = _request(server, "GET", "/state", token=server.token)
    assert status == 200


# ------------------------------------------- security-review: bind-failure UX


def test_bind_failure_exits_2_with_clean_value_free_message(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The INFO fix (app.py:3053): ``serve_web`` ran OUTSIDE the try, so a
    bind failure raised a traceback instead of the clean exit-2 contract.
    Now wrapped: exit 2 + a VALUE-FREE line (the socket error carries the
    address, which must never leak). Web errors go to stderr + the log file
    (TCK-APP-LOG-001)."""

    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError(98, "address already in use: 127.0.0.1:55555")

    monkeypatch.setattr(webserver, "serve_web", boom)
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(tmp_path / "web.db"))
    monkeypatch.setenv(app.AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.delenv(app.UI_ENV_VAR, raising=False)
    code = app.run(
        ["--stub-llm", "--zpub", ZPUB, "--web"], output_fn=lambda _s: None
    )
    assert code == 2
    joined = capsys.readouterr().err
    assert "Could not start the web server." in joined
    assert "address already in use" not in joined  # value-free (finding 4)
    assert "55555" not in joined and "Traceback" not in joined


def test_sse_frame_shape_is_fetch_compatible() -> None:
    event = EngineEvent(id=7, kind="text", payload="line one\nline two")
    assert sse_frame(event) == (
        b"id: 7\nevent: text\ndata: line one\ndata: line two\n\n"
    )
    assert sse_frame(EngineEvent(id=1, kind="progress", payload=".")) == (
        b"id: 1\nevent: progress\ndata: .\n\n"
    )
    # One-frame-per-write plumbing pins: unbuffered writes + TCP_NODELAY.
    assert webserver._Handler.wbufsize == 0
    assert webserver._Handler.disable_nagle_algorithm is True
    assert webserver._Server.block_on_close is False
    assert webserver._Server.daemon_threads is True


# ---------------------------------------------------- shutdown join (F4.4)


def test_stop_unblocks_parked_sse_thread_and_joins_the_engine(
    serve: Any,
) -> None:
    server = serve(heartbeat_s=300.0)  # the writer is truly parked (no ping)
    stream = _Stream(server)
    stream.read_head()
    started = time.monotonic()
    server.stop()  # must NOT hang on the parked connection (F4.4)
    assert time.monotonic() - started < 10.0
    assert stream.read_to_eof(timeout=10.0) >= 0  # sentinel tore the stream down
    thread = server.handle.thread
    assert thread is not None
    thread.join(10)
    assert thread.is_alive() is False  # QUIT honored between turns, thread joined
    stream.close()
    server.stop()  # idempotent


# ----------------------------------------------------------- static + island


def test_missing_static_dir_is_graceful_404(serve: Any) -> None:
    server = serve(static_dir=Path("/") / "definitely-not-here")
    status, _h, _d, _r = _request(server, "GET", "/", token=server.token)
    assert status == 404
    status, _h, _d, _r = _request(server, "GET", "/static/app.js", token=server.token)
    assert status == 404
    # /state keeps answering WITHOUT any static files — and never hands out
    # the token (the index.html island is its only delivery path, §6):
    status, _h, data, _r = _request(server, "GET", "/state", token=server.token)
    assert status == 200
    assert server.token.encode() not in data


def test_token_island_is_injected_into_served_index(tmp_path: Path, serve: Any) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text(
        "<html><head><title>w</title></head><body>hi</body></html>", "utf-8"
    )
    server = serve(static_dir=static)
    status, headers, data, _r = _request(server, "GET", "/", token=server.token)
    assert status == 200
    assert headers["content-type"].startswith("text/html")
    body = data.decode()
    island = body.index("window.__LOCALWALLET__")
    assert server.token in body  # the island carries the token…
    assert island < body.lower().index("</head>")  # …before </head>
    assert json.loads(
        body[body.index("{", island) : body.index("}", island) + 1]
    ) == {"token": server.token}
    # Static assets serve verbatim; traversal is refused; oversized 413:
    (static / "app.js").write_text("console.log(1)", "utf-8")
    status, headers, data, _r = _request(
        server, "GET", "/static/app.js", token=server.token
    )
    assert status == 200 and data == b"console.log(1)"
    conn = _conn(server.httpd.server_address[1])
    conn.putrequest("GET", "/static/%2e%2e%2f%2e%2e%2fetc%2fpasswd")
    conn.putheader("X-Auth-Token", server.token)
    conn.endheaders()
    assert conn.getresponse().status == 404  # %2e stays literal — no decode
    conn.close()
    conn = _conn(server.httpd.server_address[1])
    conn.putrequest("GET", "/static/../../etc/passwd")  # raw dots (curl-style)
    conn.putheader("X-Auth-Token", server.token)
    conn.endheaders()
    assert conn.getresponse().status == 404
    conn.close()
    big = json.dumps({"text": "y" * (webserver.MAX_BODY_BYTES + 10)})
    status, _h, _d, _r = _request(
        server, "POST", "/turn", raw_body=big.encode(), token=server.token
    )
    assert status == 413


def test_browser_bootstrap_serves_shell_and_static_without_token(
    tmp_path: Path, serve: Any
) -> None:
    """TCK-WEB-007: the token is DELIVERED by the island inside index.html, so
    a real browser's FIRST navigation (no token — it cannot have one yet) must
    get 200 + island, never a 401 deadlock. Static assets are public too (no
    user data in the shell); the gated endpoints are unmoved."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text(
        "<html><head><title>w</title></head><body>hi</body></html>", "utf-8"
    )
    (static / "app.js").write_text("console.log(1)", "utf-8")
    server = serve(static_dir=static)
    for path in ("/", "/index.html"):
        status, headers, data, _r = _request(server, "GET", path)  # NO token
        assert status == 200, path
        body = data.decode()
        assert "window.__LOCALWALLET__" in body  # island is the delivery
        assert server.token in body  # token rides the island, the only channel
        assert "content-security-policy" in headers  # CSP still on the shell
    # Static asset without token; traversal refusal survives the exemption:
    status, _h, data, _r = _request(server, "GET", "/static/app.js")
    assert status == 200 and data == b"console.log(1)"
    conn = _conn(server.httpd.server_address[1])
    conn.putrequest("GET", "/static/../../etc/passwd")
    conn.endheaders()
    assert conn.getresponse().status == 404
    conn.close()
    # Host allowlist still runs FIRST on the public paths (400, not 200/401):
    for path in ("/", "/static/app.js"):
        status, data = _host(server, "GET", path, "evil.example")  # no token
        assert status == 400, path
        assert b"evil.example" not in data and server.token.encode() not in data
    # Gated endpoints keep the exact old behavior (401, value-free) — and the
    # token the page just delivered drives them (the real browser flow):
    status, _h, data, _r = _request(server, "GET", "/state")
    assert status == 401 and server.token.encode() not in data
    status, _h, _d, _r = _request(server, "GET", "/state", token=server.token)
    assert status == 200


def test_island_injection_fallbacks() -> None:
    inj = webserver.inject_token_island
    token = "tk"
    assert inj("<html><body>b</body></html>", token).index("window") < inj(
        "<html><body>b</body></html>", token
    ).index("</body>")
    assert inj("<p>x</p>", token).startswith("<script>")
    assert json.dumps({"token": token}) in inj("<html></html>", token)


# ----------------------------------------------------------- headers §6


def test_no_cors_no_cookies_on_any_response(serve: Any) -> None:
    server = serve()
    responses: list[dict[str, str]] = []
    _status, headers, _d, _r = _request(server, "GET", "/state")  # 401
    responses.append(headers)
    _status, headers, _d, _r = _request(
        server, "GET", "/state", token=server.token
    )  # 200
    responses.append(headers)
    _status, headers, _d, _r = _request(
        server, "POST", "/turn", {"text": "hi"}, token=server.token
    )  # 202
    responses.append(headers)
    _status, headers, _d, _r = _request(  # unknown path (404 handler)
        server, "GET", "/nope", token=server.token
    )
    responses.append(headers)
    stream = _Stream(server)
    head = stream.read_head()
    stream.close()
    responses.append(
        {
            k.strip().lower(): v.strip()
            for line in head.splitlines()[1:]
            if ":" in (k := line.split(":", 1)[0])
            for v in [line.split(":", 1)[1]]
        }
    )
    for headers in responses:
        for name in headers:
            assert not name.startswith("access-control-"), name  # no CORS §6
            assert name != "set-cookie", name  # no session, ever §6


# -------------------------------------------- WEB-003 security (§6/§7): Host/
# Origin/CSP/nonce + drive-by matrix


def _host(server: Any, method: str, path: str, host: str, **kw: Any) -> tuple[int, bytes]:
    """A request with an EXPLICIT Host header (http.client would otherwise
    always send 127.0.0.1:<port>) — drives the DNS-rebinding path."""
    conn = _conn(server.httpd.server_address[1])
    headers = dict(kw.pop("headers", {}))
    if (token := kw.pop("token", None)) is not None:
        headers["X-Auth-Token"] = token
    body = kw.pop("body", None)
    payload = json.dumps(body).encode() if body is not None else None
    if payload is not None:
        headers.setdefault("Content-Type", "application/json")
    conn.putrequest(method, path, skip_host=True)
    conn.putheader("Host", host)
    for k, v in headers.items():
        conn.putheader(k, v)
    conn.endheaders(payload)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def test_dns_rebinding_host_is_refused_before_the_token(serve: Any) -> None:
    """ADR-0024 §6: a public name that rebinds to 127.0.0.1 presents its OWN
    Host header; the allowlist refuses it independently of (and BEFORE) the
    token layer, with a value-free body (the offending host is never echoed)."""
    server = serve()
    for path, method, body in (("/state", "GET", None), ("/", "GET", None),
                               ("/static/app.js", "GET", None),  # TCK-WEB-007: static too
                               ("/settings", "GET", None),
                               ("/turn", "POST", {"text": "hi"}),
                               ("/settings", "POST", {"key": "gap_limit", "value": "5"}),
                               ("/events", "GET", None)):
        status, data = _host(
            server, method, path, "evil.example", token=server.token, body=body
        )
        assert status == 400, path
        assert b"host not allowed" in data
        assert b"evil.example" not in data  # value-free (never echoes the host)
        assert server.token.encode() not in data
    # A Host with the ephemeral port is fine (name comparison, port stripped):
    port = server.httpd.server_address[1]
    for host in (f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"):
        status, _data = _host(server, "GET", "/state", host, token=server.token)
        assert status == 200, host


def test_dns_rebinding_cross_product_matrix_blocks_every_drive_by(
    serve: Any, echo_turns: list[str]
) -> None:
    """Consult F10 drive-by matrix: a malicious page whose name rebinds to
    127.0.0.1 and knows nothing about the per-launch token. Each defense layer
    is INDEPENDENT — remove any one and the others still refuse the drive-by.

    * A public Host is refused BEFORE the token (Host allowlist, §6).
    * A cross-origin POST is refused even WITH a valid token (origin check).
    * A wrong token is refused even with a good Host/Origin (token gate, §6).
    * No response ever carries CORS grant headers or a cookie (§6).
    * A legit same-origin turn still works (the matrix is not a blanket Denial)."""
    server = serve()
    port = server.httpd.server_address[1]

    # (1) Public (rebound) Host → 400, regardless of a VALID token.
    for path, method, body in (("/", "GET", None), ("/state", "GET", None),
                               ("/static/app.js", "GET", None),  # TCK-WEB-007: static too
                               ("/settings", "GET", None),
                               ("/turn", "POST", {"text": "x"}),
                               ("/settings", "POST", {"key": "gap_limit", "value": "5"})):
        status, data = _host(
            server, method, path, f"rebound-to-loopback.example:{port}",
            token=server.token, body=body,
        )
        assert status == 400, path
        assert b"rebound-to-loopback" not in data and server.token.encode() not in data
    # (2) Loopback Host but a cross-origin POST Origin → 403 even with token.
    status, headers, data, _r = _request(
        server, "POST", "/turn", {"text": "x"}, token=server.token,
        headers={"Origin": "http://attacker.example"},
    )
    assert status == 403
    assert echo_turns == []  # never reached the pump
    # (3) Good Host+Origin but a WRONG token → 401 (token layer stands alone).
    status, headers, _d, _r = _request(
        server, "POST", "/turn", {"text": "x"}, token="nope",
        headers={"Origin": f"http://127.0.0.1:{port}"},
    )
    assert status == 401
    assert echo_turns == []
    # (4) A legit same-origin turn DOES work.
    status, _h, _d, _r = _request(
        server, "POST", "/turn", {"text": "hi"}, token=server.token,
        headers={"Origin": f"http://localhost:{port}"},
    )
    assert status == 202
    # (5) None of the refused responses granted CORS or set a cookie.
    assert not any(k.startswith("access-control-") for k in headers)
    assert "set-cookie" not in headers


def test_cross_origin_post_is_refused_same_origin_and_ignored_when_absent(
    serve: Any, echo_turns: list[str]
) -> None:
    """Defense-in-depth (no cookies ⇒ CSRF is structurally moot): a POST that
    DOES carry Origin must be same-loopback; a hostile origin is refused with
    the token still valid (the check is independent), while a browser-omitting
    Origin (curl / same-origin nav) still works through the full pump."""
    server = serve(heartbeat_s=30.0)
    status, _h, data, _r = _request(
        server, "POST", "/turn", {"text": "x"}, token=server.token,
        headers={"Origin": "http://evil.example"},
    )
    assert status == 403 and b"cross-origin" in data
    assert echo_turns == []  # the hostile utterance never reached the pump
    # Same-origin (loopback) origin passes:
    status, _h, _d, _r = _request(
        server, "POST", "/turn", {"text": "ok"}, token=server.token,
        headers={"Origin": "http://127.0.0.1"},
    )
    assert status == 202
    # Absent Origin (the http.client default) passes too:
    status, _h, _d, _r = _request(
        server, "POST", "/turn", {"text": "again"}, token=server.token
    )
    assert status == 202
    # 202 means QUEUED, not executed (never-cancel) — wait for the pump to
    # have run both turns before reading the list (under full-suite load the
    # engine thread can legitimately still be mid-drain when we get here).
    deadline = time.monotonic() + 10.0
    while echo_turns != ["ok", "again"] and time.monotonic() < deadline:
        time.sleep(0.02)
    assert echo_turns == ["ok", "again"]


def test_csp_header_and_island_nonce_on_html_static_and_json(
    tmp_path: Path, serve: Any
) -> None:
    """ADR-0024 §7: a CSP with NO unsafe-inline/unsafe-eval on HTML/asset/JSON
    responses; the injected token island carries a per-response nonce that the
    document's CSP whitelists; a plain asset gets the no-inline policy."""
    static = tmp_path / "csp-static"
    static.mkdir(parents=True, exist_ok=True)
    (static / "index.html").write_text(
        "<html><head></head><body>hi</body></html>", "utf-8"
    )
    (static / "app.js").write_text("console.log(1)", "utf-8")
    server = serve(static_dir=static)

    status, headers, body, _r = _request(server, "GET", "/", token=server.token)
    assert status == 200
    csp = headers["content-security-policy"]
    assert "script-src 'self' 'nonce-" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "connect-src 'self'" in csp
    # The island nonce MATCHES the one in the CSP, and is not the token:
    text = body.decode()
    import re as _re
    island_nonce = _re.search(r'<script nonce="([^"]+)"', text)
    assert island_nonce is not None
    nonce = island_nonce.group(1)
    assert nonce in csp and nonce != server.token
    assert "window.__LOCALWALLET__" in text  # island still delivers the token

    # A second fetch uses a DIFFERENT nonce (per-response, not per-launch):
    _s2, h2, b2, _r2 = _request(server, "GET", "/", token=server.token)
    other = _re.search(r'<script nonce="([^"]+)"', b2.decode()).group(1)
    assert other != nonce and other in h2["content-security-policy"]

    # Static asset: strict no-inline script policy, no nonce needed.
    _s3, h3, _d3, _r3 = _request(server, "GET", "/static/app.js", token=server.token)
    assert h3["content-security-policy"].startswith("default-src 'none';")
    assert "nonce" not in h3["content-security-policy"]

    # JSON error path also carries CSP (defense-in-depth).
    _s4, h4, _d4, _r4 = _request(server, "GET", "/nope", token=server.token)
    assert "content-security-policy" in h4


# ---------------------------------------------------------------- SSE resync


def test_subscriber_is_flagged_only_when_cursor_predates_the_ring() -> None:
    """subscribe() distinguishes a GAP (a real cursor older than the retained
    window) from an honest full-backlog replay (fresh connection) and from a
    cursor still inside the window — only the first carries ``resync_from``."""
    bus = webserver._Bus(ring_size=4, maxsize=64)
    for i in range(1, 9):  # ids 1..8 published; ring keeps 5,6,7,8 (oldest=5)
        bus.publish(EngineEvent(id=i, kind="text", payload=f"e{i}"))
    # Cursor 2 predates oldest 5 (events 3,4 are gone) → gap:
    sub, replay = bus.subscribe(2)
    assert sub.resync_from == 5
    assert [e.id for e in replay] == [5, 6, 7, 8]  # honest partial, no dupes
    # Cursor 4 is exactly oldest-1 → the immediate next IS retained → NO gap:
    sub2, _ = bus.subscribe(4)
    assert sub2.resync_from is None
    # Cursor 6 is inside the window → no gap:
    sub3, _ = bus.subscribe(6)
    assert sub3.resync_from is None
    # A fresh connection (cursor 0, no Last-Event-ID) is NOT a loss → no flag:
    sub4, replay4 = bus.subscribe(0)
    assert sub4.resync_from is None
    assert [e.id for e in replay4] == [5, 6, 7, 8]


def test_resync_frame_shape() -> None:
    """The signal is a named ``resync`` event (the client's fetch reader can
    detect a named event; it DROPS comment lines during parse) and carries NO
    ``id:`` line so it never advances the client's cursor past the gap."""
    frame = webserver.resync_frame(9).decode()
    assert frame.startswith("event: resync\ndata: ")
    assert frame.endswith("\n\n")
    assert "\nid: " not in frame and not frame.startswith("id:")
    body = json.loads(frame.split("data: ", 1)[1].split("\n", 1)[0])
    assert body == {"reason": "too_far_behind", "oldest_event_id": 9}


def test_stale_cursor_sse_gets_one_resync_frame_before_partial_replay(
    serve: Any,
) -> None:
    """End-to-end: a reconnect whose Last-Event-ID predates the (wrapped) ring
    receives ONE resync frame, THEN the retained backlog — gap is never silent
    and the client still gets everything still held (§5 replay = recovery)."""
    server = serve(heartbeat_s=60.0, ring_size=4)
    for i in range(1, 9):  # only 5..8 retained after the wrap
        server.bus.publish(EngineEvent(id=i, kind="text", payload=f"e{i}"))
    stale = _Stream(server, last_event_id=2)
    stale.read_head()
    buf = stale.read_until(b"data: e8\n\n")
    assert buf.startswith(b"event: resync\n")  # the explicit signal comes FIRST
    assert b'"oldest_event_id": 5' in buf
    # The retained backlog follows; the lost 3,4 are NOT silently pretended:
    ids = [int(x) for x in re.findall(rb"\nid: (\d+)\n", buf)]
    assert ids == [5, 6, 7, 8]
    stale.close()
    # A fresh connection gets the backlog with NO resync (not a loss):
    fresh = _Stream(server)
    fresh.read_head()
    fresh_buf = fresh.read_until(b"data: e8\n\n")
    assert b"event: resync" not in fresh_buf
    fresh.close()


def test_malformed_requests_are_clean_json_errors(serve: Any) -> None:
    server = serve()
    status, _h, data, _r = _request(
        server, "POST", "/turn", raw_body=b"{not json", token=server.token
    )
    assert status == 400 and b"json" in data.lower()
    status, _h, data, _r = _request(
        server, "POST", "/turn", {"text": 12}, token=server.token
    )
    assert status == 400
    status, _h, _d, _r = _request(
        server, "POST", "/state", {"text": "x"}, token=server.token
    )
    assert status == 404  # GET-only path


# ------------------------------------------------------- /state via engine


def test_state_snapshot_serializes_through_the_engine_queue(
    serve: Any, echo_turns: list[str]
) -> None:
    """GET /state answers with the TYPED, value-free snapshot (TCK-WEB-003)
    AND keeps the transport fields it always had — the client must tolerate
    both the pre-WEB-003 transport-only shape (``schema`` ``state/0``) and the
    rich ``state/1`` one, so BOTH key sets stay present in the rich shape."""
    server = serve(heartbeat_s=60.0, state_timeout_s=10.0)
    stream = _Stream(server)
    stream.read_head()
    _request(server, "POST", "/turn", {"text": "before"}, token=server.token)
    stream.read_until(b"echo:before")
    status, headers, data, response = _request(
        server, "GET", "/state", token=server.token
    )
    assert status == 200
    assert response.version == 10  # HTTP/1.0 (the pinned transport, §2)
    assert headers["content-type"] == "application/json"
    snapshot = json.loads(data)
    # Transport fields unchanged (both shapes carry them — the tolerance key):
    assert snapshot["last_event_id"] > 0  # engine-published cursor (F5)
    assert {"last_event_id", "buffered_events", "subscribers"} <= set(snapshot)
    # Typed, value-free engine snapshot now rides the same response:
    assert snapshot["schema"] == "state/1"
    assert snapshot["flow_state"] == "idle"
    assert snapshot["pending_present"] is False
    assert snapshot["gate_decision"] == "not_a_decision"
    assert snapshot["watch"] == {"configured": False, "enabled": False}
    # TCK-WEB-005: the scan state rides the SAME state/1 tag (additive keys;
    # the client reads by name and ignores the rest) — closed enum name + bool
    # only, no progress value that could leak wallet size. This harness has no
    # ScanFlow wired: "disabled".
    assert snapshot["scan_state"] == "disabled"
    assert snapshot["first_scan_complete"] is False
    # Value-free: no address/amount/xpub/txid-shaped value anywhere in the body.
    assert server.token.encode() not in data  # never via /state
    # The snapshot request is NOT a user turn — the pump answers it on the
    # engine thread through the typed StateSnapshotRequest branch (never
    # _run_turn, never the model), so the echoed turn list is untouched:
    assert echo_turns == ["before"]
    stream.close()


def test_state_reports_awaiting_backend_for_a_held_scan(tmp_path: Path) -> None:
    """TCK-ONB-006 (ADR-0022 amendment 1) through the real HTTP door: a
    HELD first-run scan surfaces the additive ``awaiting_backend`` value of
    the closed ``scan_state`` enum — an enum NAME and a bool, no data
    (the first-run wallet is unscanned; nothing may leak through the
    status door either). The shipped client treats the unknown name as
    'no chip' (app.js ignores values outside its map) — additive by the
    TCK-WEB-005 tag rule, so ``state/1`` is unchanged."""

    def bootstrap() -> EngineContext:
        store = Store(str(tmp_path / "held.db"))
        wallet = store.create_wallet(
            "default", WalletDescriptor.from_key(ZPUB).descriptor
        )
        worker = app.ChainWorker(None)  # client unused: a held scan fetches nothing
        held["worker"] = worker
        flow = app.ScanFlow(store, wallet, worker, gap_limit=None)
        flow.set_startup_deferred()
        table = {
            IntentName.RESPOND: app._respond_handler,
            IntentName.CLARIFY: app._clarify_handler,
        }
        return EngineContext(
            loop=AgentLoop(app.stub_generate, table),
            flow=TxFlow(),
            session=app.SendSession(),
            table=table,
            scan=flow,
            store=store,
        )

    held: dict[str, app.ChainWorker] = {}
    server = serve_web(bootstrap, static_dir=tmp_path / "static")
    try:
        status, _h, data, _r = _request(
            server, "GET", "/state", token=server.token
        )
        assert status == 200
        snapshot = json.loads(data)
        assert snapshot["schema"] == "state/1"
        assert snapshot["scan_state"] == "awaiting_backend"
        assert snapshot["first_scan_complete"] is False
        # TCK-UX-010 (critic finding 4): the held first-run gate rides the
        # additive ``privacy_mode`` too — the ONB-006 hold overrides the
        # settings-derived mode (this harness carries no settings at all,
        # which the hold still outranks: an enum NAME, never a URL).
        assert snapshot["privacy_mode"] == "awaiting_backend"
        assert ZPUB not in data.decode()  # value-free like every other state
    finally:
        server.stop()
        held["worker"].stop()


def test_state_falls_back_to_transport_shape_when_engine_busy(
    serve: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy engine (the typed read times out) is not a stall: GET /state
    still answers 200 with the transport-only ``state/0`` shape (the pre-
    WEB-003 contract the client already handles). No value can leak because
    none was computed."""
    server = serve(state_timeout_s=10.0)
    monkeypatch.setattr(server.handle, "request_state", lambda _t: None)
    status, _h, data, _r = _request(server, "GET", "/state", token=server.token)
    assert status == 200
    snapshot = json.loads(data)
    assert snapshot["schema"] == "state/0"
    assert {"last_event_id", "buffered_events", "subscribers"} <= set(snapshot)
    assert "flow_state" not in snapshot  # minimal shape carries no engine facts
    assert server.token.encode() not in data


def test_state_answers_immediately_when_the_engine_died(
    serve: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the OLD STATE_PING wait broke early on ``engine.error`` so a
    dead bootstrap never stalled GET /state. The typed path preserves it — it
    must NOT block the full ``state_timeout_s`` waiting on a queue that will
    never drain; it answers at once with the transport-only shape."""
    server = serve(state_timeout_s=999.0)  # absurd timeout: proves no wait
    monkeypatch.setattr(server.handle, "error", RuntimeError("bootstrap died"))
    started = time.monotonic()
    status, _h, data, _r = _request(server, "GET", "/state", token=server.token)
    assert status == 200 and time.monotonic() - started < 5.0
    assert json.loads(data)["schema"] == "state/0"
    assert str(server.token) not in data.decode()


def test_state_snapshot_never_carries_values(
    serve: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The typed snapshot is value-free BY CONSTRUCTION even with a live
    pending transaction: only enum names + booleans are exposed — the pending
    record's recipient/amount/tx_ref/psbt never reach the wire. The handler
    partial binds THIS handle object, so patching its method is what the
    transport actually calls."""
    server = serve(state_timeout_s=10.0)

    class _Pending:  # duck-typed flow.pending, packed with sensitive fields
        recipient = "bc1qattrrust"  # would-be leak
        amount_sats = 100_000
        tx_ref = "SECRETREF"
        psbt_base64 = "cHNidP8SECRET"

    class _BusyFlow:
        state = TxFlowStatus.CREATED
        pending = _Pending()

    snapshot = app.build_state_snapshot(
        _BusyFlow(),
        app.SendSession(),
        None,
        # TCK-UX-010: the new field rides the SAME value-free door — its
        # value is a closed PRIVACY_MODES NAME, so no host/scheme/port/
        # userinfo CAN appear anywhere in the body.
        privacy_mode=app._backend_mode(
            Settings(chain_base_url="ssl://us3r:secret@node.invalid:50002")
        ),
    )
    monkeypatch.setattr(server.handle, "request_state", lambda _t: snapshot)

    status, _h, data, _r = _request(server, "GET", "/state", token=server.token)
    assert status == 200
    assert snapshot["flow_state"] == "created"
    assert snapshot["pending_present"] is True  # a BOOL, never the pending data
    assert snapshot["privacy_mode"] == "own_node_remote"  # a NAME, never the URL
    assert snapshot["privacy_mode"] in app.PRIVACY_MODES
    text = data.decode("latin-1")
    for secret in ("bc1qattrrust", "100000", "SECRETREF", "cHNidP8"):
        assert secret not in text
    for leak in ("node.invalid", "50002", "us3r", "secret", "://", "ssl:"):
        assert leak not in text
    assert str(server.token) not in text



# ------------------------------------------- /settings read+write (TCK-WEB-005)


def _settings_server(
    tmp_path: Path, name: str = "settings.db", *, monkeypatch: Any = None
) -> Any:
    """A real server over a REAL engine-owned store (constructed ON the
    engine thread — the check_same_thread contract the pump relies on)."""
    if monkeypatch is not None:  # hermetic env_override flags
        monkeypatch.delenv(app.GAP_LIMIT_ENV_VAR, raising=False)
        monkeypatch.delenv(app.CHAIN_BASE_URL_ENV_VAR, raising=False)
        for coin_key in app.COIN_SETTING_KEYS:
            monkeypatch.delenv(f"LOCALWALLET_{coin_key.upper()}", raising=False)

    def bootstrap() -> EngineContext:
        return EngineContext(
            loop=AgentLoop(
                app.stub_generate,
                {
                    IntentName.RESPOND: app._respond_handler,
                    IntentName.CLARIFY: app._clarify_handler,
                },
            ),
            flow=TxFlow(),
            session=app.SendSession(),
            table={},
            store=Store(tmp_path / name),
        )

    return serve_web(bootstrap, static_dir=tmp_path / "static")


def test_settings_get_lists_the_allowlist_shape_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /settings = the UI-agnostic data model (TCK-UTXO-003 alignment):
    every allowlisted key with its stored value, type, bounds, and the HONEST
    effect flags. Only keys whose DB rung is read by live code today are
    listed — invented keys would be writes with no reader."""
    server = _settings_server(tmp_path, monkeypatch=monkeypatch)
    try:
        status, headers, data, _r = _request(
            server, "GET", "/settings", token=server.token
        )
        assert status == 200
        assert headers["content-type"] == "application/json"
        snapshot = json.loads(data)
        assert snapshot["schema"] == "settings/1"
        assert snapshot["status"] == "ok"
        # TCK-WEB-013: this engine has a store but NO chain wiring, so the
        # additive effective-URL field is honestly absent end-to-end (the
        # transport forwards the engine's dict verbatim — no registration).
        assert app.SETTINGS_EFFECTIVE_CHAIN_URL_KEY not in snapshot
        entries = {entry["key"]: entry for entry in snapshot["settings"]}
        assert set(entries) == {
            "gap_limit",
            "chain_base_url",
            # TCK-UX-009: the background-watch interval (the watcher-build
            # ladder is its live reader; the watcher builds at launch →
            # requires_restart True).
            "watch_interval_s",
            "watch_key",
            # TCK-ONB-004 M3: the credential keys join the read surface as
            # SECRET entries — set/unset facts only, never the value.
            "backend_auth_user",
            "backend_auth_pass",
            "backend_auth_none",
            # TCK-UTXO-003: the coin-selection policy keys join the surface
            # (bounds + defaults single-sourced from config; resolved per
            # selection, so requires_restart False on all three).
            "utxo_target_min_sats",
            "utxo_target_max_sats",
            "consolidate_below_sat_vb",
            # TCK-FIAT-002: the display currency (bounded enum entry, live
            # per-fetch reader -> requires_restart False).
            "display_currency",
        }
        # The M3 never-echo pin: even the PASSWORD key's entry carries no
        # value anywhere in the reply bytes (the raw response is what the
        # browser gets — the check runs on `data` below).
        for secret_key in ("backend_auth_user", "backend_auth_pass", "backend_auth_none"):
            secret = entries[secret_key]
            assert secret["type"] == "secret"
            assert secret["value"] is None
            assert secret["configured"] is False
        gap = entries["gap_limit"]
        assert gap["type"] == "int" and gap["value"] is None
        assert gap["default"] == str(app.wallet_scan.DEFAULT_GAP_LIMIT)
        assert gap["min"] == 1 and gap["max"] == 1000
        # Honest flags: gap_limit re-resolves per scan; chain_base_url is
        # CONFIG-only (ADR-0018) — the client is built at bootstrap.
        assert gap["requires_restart"] is False
        assert gap["env_override"] is False
        # TCK-UTXO-003: a coin-policy entry — int type, config-sourced
        # bounds/default, honest per-selection flags.
        coin = entries["utxo_target_min_sats"]
        assert coin["type"] == "int" and coin["value"] is None
        assert coin["default"] == str(app.COIN_SETTING_DEFAULTS["utxo_target_min_sats"])
        assert (coin["min"], coin["max"]) == app.COIN_SETTING_BOUNDS["utxo_target_min_sats"]
        assert coin["requires_restart"] is False and coin["env_override"] is False
        chain = entries["chain_base_url"]
        assert chain["type"] == "url" and chain["value"] is None
        assert chain["requires_restart"] is True
        # TCK-LAUNCH-002: the READ-ONLY watch key entry (no wallet in this
        # bare store → configured False, null value; it is absent from the
        # WRITE allowlist — pinned by the write-matrix tests).
        watch = entries["watch_key"]
        assert watch["type"] == "watch_key" and watch["configured"] is False
        assert server.token.encode() not in data
    finally:
        server.stop()


def test_settings_post_writes_apply_and_refusals_are_value_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /settings = one allowlisted key per write, validated fail-closed
    ON THE ENGINE THREAD. Applied changes re-read from the store (tool truth,
    never the client's echo); refusals carry a value-free error (the submitted
    value is never echoed) and leave EVERY unrelated key untouched."""
    server = _settings_server(tmp_path, monkeypatch=monkeypatch)
    try:
        # Valid gap_limit write → applied, re-read entry confirms it.
        status, _h, data, _r = _request(
            server, "POST", "/settings",
            {"key": "gap_limit", "value": "5"}, token=server.token,
        )
        assert status == 200
        applied = json.loads(data)
        assert applied["status"] == "applied"
        assert applied["settings"] == [
            {
                "key": "gap_limit", "type": "int", "value": "5", "default": "20",
                "min": 1, "max": 1000, "requires_restart": False,
                "env_override": False,
            }
        ]
        # Valid chain_base_url write goes through the store's typed writer.
        status, _h, data, _r = _request(
            server, "POST", "/settings",
            {"key": "chain_base_url", "value": "http://127.0.0.1:3006/api"},
            token=server.token,
        )
        assert status == 200
        assert json.loads(data)["status"] == "applied"

        # Invalid writes → 400, value-free, nothing echoed, nothing stored.
        for bad, echo in (
            ({"key": "gap_limit", "value": "abc"}, b"abc"),
            ({"key": "gap_limit", "value": "0"}, b"'0'"),
            ({"key": "gap_limit", "value": "1001"}, b"1001"),
            # off-allowlist: refused WITHOUT naming the key back (fail-closed)
            ({"key": "active_wallet_id", "value": "9"}, b"active_wallet_id"),
            # the store typed writer refuses credentials — the password too
            ({"key": "chain_base_url", "value": "http://user:hunter2@x"}, b"hunter2"),
            ({"key": "chain_base_url", "value": "ftp://x"}, b"ftp://x"),
            ({"key": "gap_limit", "value": "x" * 5000}, None),
        ):
            status, _h, data, _r = _request(
                server, "POST", "/settings", bad, token=server.token
            )
            assert status == 400, bad
            rejected = json.loads(data)
            assert rejected.get("status") == "rejected", (bad, rejected)
            if echo is not None:
                assert echo not in data
            if bad["key"] == "gap_limit" and len(bad["value"]) < 200:
                assert rejected["key"] == "gap_limit"
            if bad["key"] == "chain_base_url" and len(bad["value"]) < 60:
                assert rejected["key"] == "chain_base_url"

        # Malformed shapes → clean 400s (never a crash, never the engine).
        status, _h, _d, _r = _request(
            server, "POST", "/settings", {"key": 5, "value": "7"},
            token=server.token,
        )
        assert status == 400
        status, _h, _d, _r = _request(
            server, "POST", "/settings", raw_body=b"{not json", token=server.token
        )
        assert status == 400

        # Body cap applies here too (shared _read_body path).
        big = json.dumps({"key": "gap_limit", "value": "y" * (webserver.MAX_BODY_BYTES + 10)})
        status, _h, _d, _r = _request(
            server, "POST", "/settings", raw_body=big.encode(), token=server.token
        )
        assert status == 413

        # Zero effect on unrelated keys: both writes above still stand exactly
        # as set (the invalid attempts changed nothing).
        status, _h, data, _r = _request(
            server, "GET", "/settings", token=server.token
        )
        assert status == 200
        entries = {e["key"]: e for e in json.loads(data)["settings"]}
        assert entries["gap_limit"]["value"] == "5"
        assert entries["chain_base_url"]["value"] == "http://127.0.0.1:3006/api"
    finally:
        server.stop()


def test_settings_post_auth_overlay_rides_the_url_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TCK-ONB-004 M3 security-review LOW 2 (transport half): the web Apply
    sends the login as ONE combined POST (key chain_base_url + value +
    ``auth`` overlay) — no separate cred writes to strand. The engine
    commits creds+URL together on success and rewinds the pair when the URL
    write fails (the plain harness server here has no hot-swap controller,
    which is the same ordered path); the transport shape-checks the overlay
    and never interprets it."""
    server = _settings_server(tmp_path, monkeypatch=monkeypatch)
    try:
        status, _h, data, _r = _request(
            server, "POST", "/settings",
            {
                "key": "chain_base_url",
                "value": "http://127.0.0.1:3006/api",
                "auth": {"backend_auth_user": "rpc-user", "backend_auth_pass": "rpc-pass"},
            },
            token=server.token,
        )
        assert status == 200 and json.loads(data)["status"] == "applied"
        status, _h, data, _r = _request(server, "GET", "/settings", token=server.token)
        entries = {e["key"]: e for e in json.loads(data)["settings"]}
        assert entries["chain_base_url"]["value"] == "http://127.0.0.1:3006/api"
        assert entries["backend_auth_user"]["configured"] is True
        assert entries["backend_auth_pass"]["configured"] is True
        # Never-echo stands on every reply byte (the read is set/unset only).
        assert b"rpc-user" not in data and b"rpc-pass" not in data

        # Refused URL + new login: the engine rewinds the overlay — the old
        # pair stays configured, the old URL stands, nothing echoes.
        status, _h, _d, _r = _request(
            server, "POST", "/settings",
            {"key": "chain_base_url", "value": "ftp://x", "auth": {"backend_auth_user": "other-user"}},
            token=server.token,
        )
        assert status == 400
        status, _h, data, _r = _request(server, "GET", "/settings", token=server.token)
        entries = {e["key"]: e for e in json.loads(data)["settings"]}
        assert entries["chain_base_url"]["value"] == "http://127.0.0.1:3006/api"
        assert entries["backend_auth_user"]["configured"] is True  # rewound, not blanked
        assert b"other-user" not in data

        # Malformed auth shapes: clean 400s at the transport, before the
        # engine ever sees them.
        for bad in (
            {"auth": "not-an-object"},
            {"auth": {"backend_auth_user": 5}},
            {"auth": ["backend_auth_user"]},
        ):
            status, _h, _d, _r = _request(
                server, "POST", "/settings",
                {"key": "chain_base_url", "value": "http://127.0.0.1:3006/api", **bad},
                token=server.token,
            )
            assert status == 400, bad
    finally:
        server.stop()


def test_settings_endpoints_answer_503_when_the_engine_is_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No fallback shape for settings (a settings page showing guessed values
    is worse than an honest failure): a dead engine → 503, value-free, on
    BOTH endpoints — GET and the write path (which must never half-apply)."""
    server = _settings_server(tmp_path)
    try:
        monkeypatch.setattr(server.handle, "error", RuntimeError("bootstrap died"))
        status, _h, data, _r = _request(
            server, "GET", "/settings", token=server.token
        )
        assert status == 503 and b"engine busy" in data
        status, _h, _d, _r = _request(
            server, "POST", "/settings", {"key": "gap_limit", "value": "5"},
            token=server.token,
        )
        assert status == 503
        monkeypatch.setattr(server.handle, "error", None)
        # The refused write indeed never happened: the fresh read is pristine.
        status, _h, data, _r = _request(
            server, "GET", "/settings", token=server.token
        )
        entries = {e["key"]: e for e in json.loads(data)["settings"]}
        assert entries["gap_limit"]["value"] is None
    finally:
        server.stop()


# ---------------------------------------------- POST /resync (TCK-BACKEND-002)


def test_resync_endpoint_is_token_gated_and_status_mapped(tmp_path: Path) -> None:
    """``POST /resync`` (user direction 6, the ``Resync now`` button): carries
    NO data; the transport marshals a typed ResyncRequest through the pump
    and maps the closed engine status (started→202, busy→409, other→503).
    Token-gated like every mutating endpoint (401 without it). The bare
    harness engine has no chain wiring → the honest ``unavailable``; the
    started/busy shapes are pinned by patching the handle seam (the engine-
    side semantics live in tests/test_backend_hotswap.py)."""
    server = _settings_server(tmp_path)
    try:
        status, _h, _d, _r = _request(server, "POST", "/resync")
        assert status == 401  # token gate before anything else
        status, _h, data, _r = _request(
            server, "POST", "/resync", token=server.token
        )
        assert status == 503  # no chain wiring: unavailable, never a lie
        body = json.loads(data)
        assert body["schema"] == "resync/1"
        assert body["status"] == "unavailable"
        for status_name, code in (("started", 202), ("busy", 409)):
            server.handle.request_resync = (
                lambda _t, _s=status_name: {"schema": "resync/1", "status": _s}
            )
            status, _h, data, _r = _request(
                server, "POST", "/resync", token=server.token
            )
            assert status == code
            assert json.loads(data)["status"] == status_name
    finally:
        server.stop()


# --------------------------- pre-first-scan refusal over the web turn (WEB-005)


def test_web_send_pre_first_scan_gets_the_loading_refusal_as_text(
    tmp_path: Path,
) -> None:
    """Deliverable 2 pin: the engine-side create_tx refusal (ADR-0022
    decision 6) needs NO new server code — a web turn with the scan gate
    still pending routes through the same full pipeline and the friendly,
    dispatcher-owned line arrives as an SSE ``text`` event, verbatim."""

    def bootstrap() -> EngineContext:
        store = Store(tmp_path / "loading.db")
        wd = WalletDescriptor.from_key(ZPUB)
        wallet = store.create_wallet("default", wd.descriptor)
        table = app.build_dispatch_table(
            store,
            wallet,
            wd.parsed,
            None,  # client: the refusal fires before ANY chain/store work
            lambda: pytest.fail("refusal must precede any scan"),
            fee_estimator=SimpleNamespace(
                estimate=lambda target: SimpleNamespace(sat_per_vb=2)
            ),
            price_oracle=SimpleNamespace(
                fresh=lambda: (_ for _ in ()).throw(PriceUnavailableError("stub")),
                sats_to_usd=lambda sats, rate: None,
            ),
            scan_gate=app.StartupScan(enabled=True),  # pending = pre-first-scan
        )
        return EngineContext(
            loop=AgentLoop(app.stub_generate, table),
            flow=TxFlow(),
            session=app.SendSession(),
            table=table,
            store=store,
        )

    server = serve_web(bootstrap, static_dir=tmp_path / "static")
    try:
        stream = _Stream(server)
        stream.read_head()
        status, _h, _d, _r = _request(
            server, "POST", "/turn",
            {"text": f"send 10000 sats to {SEND_RECIPIENT}"},
            token=server.token,
        )
        assert status == 202
        buf = stream.read_until(app.WALLET_LOADING_REFUSAL.encode(), timeout=30)
        # The refusal rides a TEXT frame verbatim (no re-encoding, no model
        # wording), and it is the friendly line, not an error dump.
        assert f"data: {app.WALLET_LOADING_REFUSAL}\n\n".encode() in buf
        # No transaction ever pended (the refusal is pre-flow, structural):
        status, _h, data, _r = _request(
            server, "GET", "/state", token=server.token
        )
        snapshot = json.loads(data)
        assert snapshot["flow_state"] == "idle"
        assert snapshot["pending_present"] is False
        stream.close()
    finally:
        server.stop()


# --------------------------------------------------------- run() wiring §5


def _run_web_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    capture: dict[str, Any],
    *,
    web_env: bool,
) -> tuple[threading.Thread, list[str]]:
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(tmp_path / "web.db"))
    monkeypatch.setenv(app.AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")
    monkeypatch.delenv(app.UI_ENV_VAR, raising=False)
    if web_env:
        monkeypatch.setenv(app.UI_ENV_VAR, "web")
    outputs: list[str] = []
    gate = threading.Event()

    def on_server(server: Any) -> None:
        capture["server"] = server
        gate.set()

    thread = threading.Thread(
        target=lambda: capture.update(
            code=app.run(
                args,
                output_fn=outputs.append,
                on_web_server=on_server,
            )
        ),
        daemon=True,
    )
    thread.start()
    assert gate.wait(30), "web server never started"
    return thread, outputs


@pytest.mark.parametrize("flag_like", [True, False], ids=["--web", "LOCALWALLET_UI"])
def test_run_web_launch_lines_are_token_separate_and_turn_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag_like: bool,
) -> None:
    capture: dict[str, Any] = {}
    args = ["--stub-llm", "--zpub", ZPUB] + (["--web"] if flag_like else [])
    thread, outputs = _run_web_session(
        tmp_path, monkeypatch, args, capture, web_env=not flag_like
    )
    server = capture["server"]
    try:
        stream = _Stream(server)
        assert "text/event-stream" in stream.read_head()
        status, _h, _d, _r = _request(
            server, "POST", "/turn", {"text": "hello there"}, token=server.token
        )
        assert status == 202
        # Real full-wiring turn (stub LLM → respond intent) fans out:
        frame = stream.read_until(b"event: turn_end", timeout=30)
        assert b"event: text\ndata:" in frame  # narrated, then completed
        stream.close()
    finally:
        server.stop()
    thread.join(15)
    assert capture.get("code") == 0
    url_line = next(line for line in outputs if line.startswith("Web UI: http://127.0.0.1:"))
    assert f":{server.httpd.server_address[1]}" in url_line  # ephemeral port
    token_line = next(line for line in outputs if server.token in line)
    assert token_line != url_line  # SEPARATE lines: token never rides the URL
    assert server.token not in "".join(outputs[: outputs.index(url_line)])
    # Teardown (server.stop() above) must announce the instance is dead so a
    # stale tab from a prior launch is diagnosable. Value-free: no port/token.
    assert any("no longer reachable" in line for line in outputs)


def test_run_cli_default_unchanged_no_server(tmp_path: Path, monkeypatch) -> None:
    """No --web, no LOCALWALLET_UI: the REPL path (no engine thread, no
    bound socket) — the CLI world is untouched outside run()'s wiring."""
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv(app.AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.delenv(app.UI_ENV_VAR, raising=False)
    outputs: list[str] = []
    lines = iter(["exit"])
    code = app.run(
        ["--stub-llm", "--zpub", ZPUB],
        input_fn=lambda _p: next(lines),
        output_fn=outputs.append,
    )
    assert code == 0
    assert [t for t in threading.enumerate() if t.name == "engine"] == []
    assert not any(line.startswith("Web UI:") for line in outputs)


# ================================= TCK-LAUNCH-001 first-run watch-key entry
#
# The real launch door: a WEB session started with NO key (flag/env/stored
# all empty) serves the page's first-run form; POST /watchkey marshals the
# key THROUGH the pump, the EXISTING parse+gate path runs on the engine
# thread, and acceptance persists the wallet + continues the normal
# post-xpub sequence (TCK-ONB-006: scan HELD at awaiting_backend — zero
# chain calls until a backend is chosen). Every refusal is value-free.

_SEED_PHRASE = " ".join(["bacon"] * 12)  # BIP39-shaped (redactor test fixture)


def _launch_first_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    args: list[str] | None = None,
    arm_model_card: bool = False,
) -> tuple[threading.Thread, list[str], dict[str, Any]]:
    """Start a REAL web session with no key anywhere; returns
    ``(thread, outputs, capture)`` with ``capture["server"]`` live. The
    caller owns ``server.stop()`` + join. Chain I/O is impossible by
    construction here (unprovisioned) — the fresh tmp store also proves
    nothing was configured by an earlier run.

    ``arm_model_card=True`` (TCK-LAUNCH-002) instead resolves the default
    to a NOT-DOWNLOADED file, arming the download card; the fake model name
    keeps every seam deterministic (no real manifest/models on either
    branch, no subprocess ever spawned by these tests)."""
    import httpx  # local: the ONLY network-ish lib, mock transport only

    store_path = tmp_path / "first-run.db"
    for var in (
        app.ZPUB_ENV_VAR,
        app.UI_ENV_VAR,
        "LOCALWALLET_MODEL_PATH",
        "LOCALWALLET_LLM_BASE_URL",
        "LOCALWALLET_LLM_MODEL",
        "LOCALWALLET_CHAIN_BASE_URL",
        "LOCALWALLET_WEB_PORT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(store_path))
    monkeypatch.setenv(app.AUTO_SCAN_ENV_VAR, "1")
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")
    # TCK-LAUNCH-002 machine-independence: a real models/bin download would
    # make the DEFAULT resolve to an existing file and select the GGUF
    # runtime here; an absent file would ARM THE DOWNLOAD CARD (extra SSE
    # frames). These first-run tests predate (and are orthogonal to) the
    # card — pin the "no resolvable default" branch so the stream shape is
    # identical on every machine. arm_model_card=True opts INTO the card
    # (a fake not-downloaded default) for the dedicated card tests below.
    if arm_model_card:
        monkeypatch.setattr(
            app, "_resolve_default_model",
            lambda: ("fake-model", tmp_path / "no-such-model.gguf"),
        )
    else:
        monkeypatch.setattr(app, "_resolve_default_model", lambda: None)
    calls: list[str] = []
    capture: dict[str, Any] = {"calls": calls, "store_path": store_path}

    from localwallet.chain import EsploraClient as _Esplora

    def handler(request: Any) -> Any:
        calls.append(request.url.path)
        return httpx.Response(599, json={})  # never legitimately reached

    def counting_client(*_args: Any, **_kwargs: Any) -> Any:
        # TCK-DESCOPE-M3A: the app builds the wallet client and the public-
        # info fetcher through two seams; both are pinned to a request-
        # counting MockTransport client here (base_url etc. are what the
        # real seams would pass; the mock transport guarantees nothing can
        # leave the process — the FIRST-RUN path should not construct a
        # wallet client at all, and any chain request is a leak: 599).
        return _Esplora(
            base_url="https://mempool.space/api",
            timeout_s=2.0,
            max_retries=0,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(app, "_build_chain_client", counting_client)
    monkeypatch.setattr(app, "_public_info_client", counting_client)
    outputs: list[str] = []
    gate = threading.Event()

    def on_server(server: Any) -> None:
        capture["server"] = server
        gate.set()

    thread = threading.Thread(
        target=lambda: capture.update(
            code=app.run(
                args if args is not None else ["--web"],
                output_fn=outputs.append,
                on_web_server=on_server,
            )
        ),
        daemon=True,
    )
    thread.start()
    assert gate.wait(30), "unprovisioned web server never started"
    return thread, outputs, capture


def _session_state(server: Any) -> dict[str, Any]:
    status, _h, data, _r = _request(server, "GET", "/state", token=server.token)
    assert status == 200
    return json.loads(data)


def test_first_run_state_shows_the_form_and_chat_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread, _outputs, capture = _launch_first_run(tmp_path, monkeypatch)
    server = capture["server"]
    try:
        snap = _session_state(server)
        assert snap["schema"] == "state/1"
        assert snap["needs_watch_key"] is True  # additive flag → the form shows
        # A typed line BEFORE the key cannot reach any handler/loop: the pump
        # refuses it value-free (there is no wallet to talk to yet).
        stream = _Stream(server)
        stream.read_head()
        status, _h, _d, _r = _request(
            server, "POST", "/turn", {"text": "what's my balance?"},
            token=server.token,
        )
        assert status == 202  # queued like every turn — the refusal rides the stream
        # TCK-UX-012(a): the demo banner now closes its OWN turn (a ``turn_end``
        # BEFORE the refusal), so ``read_until(turn_end)`` could legitimately
        # return just the banner bubble. Read for the refusal itself (blocks
        # until it reaches the stream); the bubble SPLIT is pinned where it is
        # cleanly observable (test_engine_pump card test + test_app_logging
        # _Output test), here we pin the refusal still rides a real turn.
        frame = stream.read_until(app.WATCHKEY_REQUIRED_NOTICE.encode())
        assert app.WATCHKEY_REQUIRED_NOTICE.encode() in frame
        assert ZPUB.encode() not in frame  # and NOTHING key-shaped
        # The refusal's own completion marker follows it in the stream.
        assert b"event: turn_end" in stream.read_until(b"event: turn_end")
        stream.close()
        assert capture["calls"] == []  # refusal meant zero chain access
    finally:
        server.stop()
        thread.join(15)


def test_watchkey_accepts_persists_and_defers_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread, outputs, capture = _launch_first_run(tmp_path, monkeypatch)
    server = capture["server"]
    try:
        status, _h, data, _r = _request(
            server, "POST", "/watchkey", {"key": ZPUB}, token=server.token
        )
        assert status == 200
        assert json.loads(data)["status"] == "accepted"
        # The key was NEVER echoed back (value-free reply).
        assert ZPUB.encode() not in data
        # Persisted through the EXISTING store path: one wallet row, the
        # canonical descriptor, active.
        store = Store(str(capture["store_path"]))
        try:
            wallets = store.list_wallets()
            active = store.get_active_wallet()
        finally:
            store.close()
        assert len(wallets) == 1
        assert active is not None and active.descriptor == (
            WalletDescriptor.from_key(ZPUB).descriptor
        )
        # The normal post-xpub flow: TCK-ONB-006 holds the first scan at
        # awaiting_backend (no backend was ever chosen) — the /state door
        # says so and NOTHING left the process.
        snap = _session_state(server)
        assert "needs_watch_key" not in snap  # provisioned: form gone
        assert snap["scan_state"] == "awaiting_backend"
        assert snap["first_scan_complete"] is False
        assert capture["calls"] == []
        # The banner for this launch ran through the normal web sequence
        # (privacy notice + the honest unresolved-backend hint) — routed to
        # the SSE emitter, NOT the terminal (TCK-APP-LOG-001).
        joined = "\n".join(outputs)
        assert "Privacy notice:" not in joined
        stream = _Stream(server)
        stream.read_head()
        frame = stream.read_until(b"No server choice has been made yet", timeout=15)
        assert b"Privacy notice:" in frame
        stream.close()
        # A second submit cannot replace the wallet (ADR-0010 single-wallet).
        status, _h, data, _r = _request(
            server, "POST", "/watchkey", {"key": ZPUB}, token=server.token
        )
        assert status == 409
        assert json.loads(data)["status"] == "already"
        assert len(Store(str(capture["store_path"])).list_wallets()) == 1
    finally:
        server.stop()
        thread.join(15)


@pytest.mark.parametrize(
    ("bad_key", "needle"),
    [
        ("not-a-key-at-all", "not a valid extended key"),
        pytest.param(
            "vpub5ZJ3cDEGGk61yWWUHFHgmG3M4je4yFD3ebC6jWHsqV8Cxh2K5zz8c6X5Hk7FkUAB"
            "FTjRkQBz3g84MYeRhjAdnq1QmrmyTRTrzs8rFVCJUyh",
            "mainnet-only",
            id="testnet-vpub",
        ),
        pytest.param(
            "xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKm"
            "PGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi",
            "watch-only",
            id="private-xprv",
        ),
        pytest.param(_SEED_PHRASE, "hardware-wallet-only", id="seed-phrase"),
    ],
)
def test_watchkey_refusals_are_value_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_key: str,
    needle: str,
) -> None:
    thread, _outputs, capture = _launch_first_run(tmp_path, monkeypatch)
    server = capture["server"]
    try:
        status, _h, data, _r = _request(
            server, "POST", "/watchkey", {"key": bad_key}, token=server.token
        )
        assert status == 400
        body = json.loads(data)
        assert body["status"] == "rejected"
        assert needle in body["error"]  # the layer's own reason, relayed
        assert bad_key not in data.decode()  # NEVER echoed
        # Nothing was persisted and the form stays armed (retriable):
        store = Store(str(capture["store_path"]))
        try:
            assert store.list_wallets() == []
        finally:
            store.close()
        assert _session_state(server)["needs_watch_key"] is True
        assert capture["calls"] == []
    finally:
        server.stop()
        thread.join(15)


def test_watchkey_relaunch_uses_the_stored_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User direction: "If they have given us a zpub we use that one."
    Provision once, stop, relaunch bare (same store) — the second launch is
    a NORMAL keyed web launch: no form, wallet row reused, never duplicated."""
    thread, _outputs, capture = _launch_first_run(tmp_path, monkeypatch)
    server = capture["server"]
    status, _h, _d, _r = _request(
        server, "POST", "/watchkey", {"key": ZPUB}, token=server.token
    )
    assert status == 200
    server.stop()
    thread.join(15)

    thread2, outputs2, capture2 = _launch_first_run(tmp_path, monkeypatch)
    server2 = capture2["server"]
    try:
        snap = _session_state(server2)
        assert "needs_watch_key" not in snap  # provisioned at bootstrap
        assert snap["scan_state"] == "awaiting_backend"  # ONB-006 still holds
        joined = "\n".join(outputs2)
        # Real wiring ran at launch — its narration reached the SSE emitter,
        # not the terminal (TCK-APP-LOG-001).
        assert "Privacy notice:" not in joined
        stream = _Stream(server2)
        stream.read_head()
        stream.read_until(b"Privacy notice:", timeout=15)
        stream.close()
        store = Store(str(capture["store_path"]))
        try:
            assert len(store.list_wallets()) == 1  # reused, not duplicated
        finally:
            store.close()
    finally:
        server2.stop()
        thread2.join(15)


def test_favicon_is_served_without_the_token() -> None:
    """TCK-LAUNCH-001: browsers request /favicon.ico WITHOUT the auth
    header (the console-404 stub). Served through the static handler,
    token-exempt like the shell, Host/CSP gates still applied."""
    server = serve_web(_bootstrap)  # default = the package's real static dir
    try:
        status, headers, data, _r = _request(server, "GET", "/favicon.ico")
        assert status == 200
        assert headers["content-type"].startswith("image/")
        assert data[:4] == b"\x00\x00\x01\x00"  # a valid ICO header
        assert "content-security-policy" in headers
        status, _h, data, _r = _request(
            server, "GET", "/favicon.ico", headers={"Host": "evil.example"}
        )
        assert status == 400  # the DNS-rebinding gate runs on static too
        assert b"evil" not in data  # value-free refusal
    finally:
        server.stop()


def test_watchkey_on_a_keyed_engine_is_refused(serve: Any) -> None:
    """Fail-closed at the door that only exists for the first-run pump: a
    provisioned/stub engine has NO WatchKeyProvision, so the pump refuses
    the submit (never a store write, never a 200)."""
    server = serve()
    status, _h, data, _r = _request(
        server, "POST", "/watchkey", {"key": ZPUB}, token=server.token
    )
    assert status == 503
    assert json.loads(data)["status"] == "unavailable"
    assert ZPUB.encode() not in data


# =================== TCK-LAUNCH-002: model card + watch-key surfacing =======
#
# Transport-contract pins only — the download LIFECYCLE (subprocess, cancel,
# concurrency) is pinned against a real pump at the app level in
# tests/test_launch.py; here we assert only what crosses the HTTP/SSE
# boundary. No test in this section POSTs /download (the web harness carries
# a fake not-downloaded default but is never consented — no child spawns).

from tests.test_wallet_descriptor import YPUB


def _model_serve(tmp_path: Path, *, succeed: bool = True) -> Any:
    """A web server whose engine carries an ARMED model-download flow (fake
    command) + the RESPOND/CLARIFY table (no wallet), mirroring a real
    default-file-absent launch. The caller owns server.stop()."""
    table = {
        IntentName.RESPOND: app._respond_handler,
        IntentName.CLARIFY: app._clarify_handler,
    }
    command = [
        "import json",
        "for n in (1048576, 2097152, 3145728):",
        "    print(json.dumps({'downloaded': n, 'total': 3145728}), flush=True)",
    ] if succeed else ["import sys; sys.exit(1)"]

    def bootstrap() -> EngineContext:
        return EngineContext(
            loop=AgentLoop(app.stub_generate, table),
            flow=TxFlow(),
            session=app.SendSession(),
            table=table,
            model=app.ModelDownloadFlow(
                model_name="fake", command=[sys.executable, "-c", "\n".join(command)]
            ),
        )

    return serve_web(bootstrap, static_dir=tmp_path / "static")


def test_model_state_rides_the_typed_state_snapshot(tmp_path: Path) -> None:
    """state/1 gains an ADDITIVE model_state NAME (enum only, never data)
    while a download flow is armed — the sole source the client's Yes/No
    buttons key off. No amount/address/token can appear in it."""
    server = _model_serve(tmp_path)
    try:
        status, _h, data, _r = _request(server, "GET", "/state", token=server.token)
        snap = json.loads(data)
        assert status == 200 and snap["schema"] == "state/1"
        assert snap["model_state"] == "absent"  # armed, card shown
        assert server.token.encode() not in data
    finally:
        server.stop()


def test_download_progress_is_int_only_value_free_sse(tmp_path: Path) -> None:
    """A consented /download streams model_progress frames (int-only JSON:
    percent + bytes, no path/user/wallet data) and the card flips to
    `ready` — all across the wire, driven by the fake downloader."""
    server = _model_serve(tmp_path)
    stream = _Stream(server)
    try:
        stream.read_head()
        # (a fresh subscription replays the ring — the startup card lines
        # are already retained; no pre-turn to wait for.)
        status, _h, _d, _r = _request(
            server, "POST", "/action", {"utterance": "/download"},
            token=server.token,
        )
        assert status == 202
        stream.read_until(b"event: model_progress")
        # isolate the model_progress frames' payloads:
        import re as _re

        ticks = _re.findall(
            rb"event: model_progress\ndata: (\{[^}]*\})", stream.buf
        )
        assert ticks
        for raw in ticks:
            payload = json.loads(raw)
            assert set(payload) == {"downloaded", "total", "pct"}
            assert all(
                v is None or isinstance(v, int) for v in payload.values()
            )
            assert b"/" not in raw  # no path-shaped string in any tick
        stream.read_until(b"Downloaded and verified")  # the ready line (copy pass 2 #68)
        snap = _session_state(server)
        assert snap["model_state"] == "ready"
    finally:
        stream.close()
        server.stop()


def test_quick_action_bypasses_the_model_turn_pipeline(
    serve: Any, echo_turns: list[str]
) -> None:
    """ADR-0024 §8 hold + LAUNCH-002: a canonical quick-action slash
    (/balance) is intercepted by CODE ahead of _run_turn — the model is
    never asked (echo_turns stays empty), so it answers with no model. A
    NON-slash chat line still routes through the turn pipeline normally."""
    server = serve(heartbeat_s=30.0)
    stream = _Stream(server)
    try:
        stream.read_head()
        _request(server, "POST", "/action", {"utterance": "/balance"},
                 token=server.token)
        frame = stream.read_until(b"event: turn_end")
        assert echo_turns == []  # never reached _run_turn
        assert b"not available" in frame  # (this harness table has no handler)
        # A plain chat line still runs the full turn path:
        _request(server, "POST", "/turn", {"text": "hello model"},
                 token=server.token)
        stream.read_until(b"echo:hello model")
        assert echo_turns == ["hello model"]
    finally:
        stream.close()


# ------------------------------------------- watch-key settings surfacing


def _watch_store_server(tmp_path: Path, monkeypatch: Any) -> Any:
    """A server over a REAL engine-owned store with an accepted wallet (the
    canonical descriptor IS the persisted key) — the settings surface the
    watch-key entry reads from."""
    store_path = tmp_path / "watchkey.db"
    seed = Store(str(store_path))
    try:
        wallet = seed.create_wallet(
            "default", WalletDescriptor.from_key(ZPUB).descriptor
        )
        seed.set_active_wallet(wallet.id)
    finally:
        seed.close()
    monkeypatch.delenv(app.GAP_LIMIT_ENV_VAR, raising=False)
    monkeypatch.delenv(app.CHAIN_BASE_URL_ENV_VAR, raising=False)
    monkeypatch.delenv(app.ZPUB_ENV_VAR, raising=False)

    def bootstrap() -> EngineContext:
        table = {
            IntentName.RESPOND: app._respond_handler,
            IntentName.CLARIFY: app._clarify_handler,
        }
        return EngineContext(
            loop=AgentLoop(app.stub_generate, table),
            flow=TxFlow(),
            session=app.SendSession(),
            table=table,
            store=Store(str(store_path)),
        )

    server = serve_web(bootstrap, static_dir=tmp_path / "static")
    server._watch_store_path = store_path  # type: ignore[attr-defined]
    return server


def test_settings_watch_key_truncated_in_list_full_only_on_explicit_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _watch_store_server(tmp_path, monkeypatch)
    descriptor = WalletDescriptor.from_key(ZPUB).descriptor
    try:
        status, _h, data, _r = _request(server, "GET", "/settings", token=server.token)
        assert status == 200
        entries = {e["key"]: e for e in json.loads(data)["settings"]}
        watch = entries["watch_key"]
        assert watch["configured"] is True and watch["type"] == "watch_key"
        # DISPLAY-TRUNCATED in the general list — never the full descriptor:
        assert watch["value"] == app._display_truncate(descriptor)
        assert watch["value"] != descriptor
        assert descriptor.encode() not in data
        # The explicit single-key read (the Show/Copy click) carries FULL:
        status, _h, rdata, _r = _request(
            server, "GET", "/settings?key=watch_key", token=server.token
        )
        assert status == 200
        revealed = json.loads(rdata)["settings"][0]
        assert revealed["value"] == descriptor and revealed["revealed"] is True
        # …and it is STILL token-gated: no token → 401, key never leaks.
        status, _h, edata, _r = _request(server, "GET", "/settings?key=watch_key")
        assert status == 401
        assert descriptor.encode() not in edata
    finally:
        server.stop()


def test_settings_watch_key_is_read_only_over_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watch key is deliberately OFF the settings WRITE allowlist — a
    POST /settings {key:watch_key} is refused (its name is not even echoed);
    changing the key is the gated /watchkey path ONLY."""
    server = _watch_store_server(tmp_path, monkeypatch)
    try:
        status, _h, data, _r = _request(
            server, "POST", "/settings",
            {"key": "watch_key", "value": YPUB}, token=server.token,
        )
        assert status == 400
        assert json.loads(data)["status"] == "rejected"
        assert YPUB.encode() not in data  # the submitted value is never echoed
    finally:
        server.stop()


# --------------------------------------------- in-place REPLACE (002/ADR)


def _keyed_web(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A REAL keyed web session (stub, tmp store) whose engine carries a
    preset provision — the replace surface. Caller owns stop()+join()."""
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(tmp_path / "replace.db"))
    monkeypatch.setenv(app.AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")
    monkeypatch.delenv(app.UI_ENV_VAR, raising=False)
    monkeypatch.delenv(app.ZPUB_ENV_VAR, raising=False)
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
    return thread, capture["server"]


def test_watchkey_replace_requires_the_double_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread, server = _keyed_web(tmp_path, monkeypatch)
    new_descriptor = WalletDescriptor.from_key(YPUB).descriptor
    try:
        # (a) bare submit against a configured wallet → 409 already (contract).
        status, _h, data, _r = _request(
            server, "POST", "/watchkey", {"key": YPUB}, token=server.token
        )
        assert status == 409 and json.loads(data)["status"] == "already"
        assert YPUB.encode() not in data
        # (b) replace WITHOUT confirm → still 409 (single flag is not consent).
        status, _h, data, _r = _request(
            server, "POST", "/watchkey", {"key": YPUB, "replace": True},
            token=server.token,
        )
        assert status == 409
        # (c) replace+confirm with the SAME key → 409, named as the current one.
        status, _h, data, _r = _request(
            server, "POST", "/watchkey",
            {"key": ZPUB, "replace": True, "confirm": True}, token=server.token,
        )
        assert status == 409 and "already connected" in json.loads(data)["error"]
        # (d) replace+confirm NEW key → 200 replaced; the new wallet is active.
        status, _h, data, _r = _request(
            server, "POST", "/watchkey",
            {"key": YPUB, "replace": True, "confirm": True}, token=server.token,
        )
        assert status == 200 and json.loads(data)["status"] == "replaced"
        assert YPUB.encode() not in data  # the key never rides back
        store = Store(str(tmp_path / "replace.db"))
        try:
            active = store.get_active_wallet()
            # ADR-0010 single-WALLET is per descriptor; the swap reactivated
            # the store onto the NEW key while the session continues in place.
            assert active is not None and active.descriptor == new_descriptor
        finally:
            store.close()
        snap = _session_state(server)
        assert "needs_watch_key" not in snap  # still provisioned
    finally:
        server.stop()
        thread.join(15)


def test_watchkey_replace_refusals_are_value_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replace submit runs the EXACT same gated parse path — a testnet /
    private key is refused (400) value-free, and NOTHING was swapped."""
    thread, server = _keyed_web(tmp_path, monkeypatch)
    try:
        for bad in (
            (
                "vpub5ZJ3cDEGGk61yWWUHFHgmG3M4je4yFD3ebC6jWHsqV8Cxh2K5zz8c6X5Hk7FkUAB"
                "FTjRkQBz3g84MYeRhjAdnq1QmrmyTRTrzs8rFVCJUyh"
            ),
            (
                "xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKm"
                "PGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi"
            ),
        ):
            status, _h, data, _r = _request(
                server, "POST", "/watchkey",
                {"key": bad, "replace": True, "confirm": True},
                token=server.token,
            )
            assert status == 400
            assert bad.encode() not in data  # value-free refusal
        snap = _session_state(server)
        assert snap["scan_state"] in {"disabled", "awaiting_backend", "done"}
    finally:
        server.stop()
        thread.join(15)

"""TCK-WEB-002 server half (ADR-0024): loopback HTTP/SSE over the engine pump.

Everything runs against a REAL server on 127.0.0.1:0 and a REAL engine
(``start_engine`` with a stub bootstrap, reusing the pump-test harness
patterns — ``_run_turn`` replaced by an echo so turns are deterministic and
model-free). Pins, per the ticket's done-when list:

* token gate (401 without ``X-Auth-Token`` on EVERY endpoint) and the
  HTTP/1.0 transport (stay-and-document, §2);
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
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.app import EngineContext, EngineEvent
from localwallet.protocol import IntentName
from localwallet.tx.flow import TxFlow
from localwallet.ui.web import server as webserver
from localwallet.ui.web.server import serve_web, sse_frame
from tests.test_e2e_skeleton import ZPUB

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
    cases = [
        ("GET", "/state", None),
        ("GET", "/", None),
        ("POST", "/turn", {"text": "hi"}),
        ("POST", "/action", {"utterance": "confirm"}),
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


# ----------------------------------------------------------------- SSE §5


def test_ring_buffer_replays_after_reconnect_via_last_event_id(
    serve: Any, echo_turns: list[str]
) -> None:
    server = serve(heartbeat_s=60.0)  # no heartbeats muddying frame ids
    stream = _Stream(server)
    stream.read_head()
    _request(server, "POST", "/turn", {"text": "one"}, token=server.token)
    first_buf = stream.read_until(b"turn_end")
    first_id = min(int(i) for i in re.findall(rb"id: (\d+)", first_buf))
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
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The INFO fix (app.py:3053): ``serve_web`` ran OUTSIDE the try, so a
    bind failure raised a traceback instead of the clean exit-2 contract.
    Now wrapped: exit 2 + a VALUE-FREE line (the socket error carries the
    address, which must never leak)."""

    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError(98, "address already in use: 127.0.0.1:55555")

    monkeypatch.setattr(webserver, "serve_web", boom)
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(tmp_path / "web.db"))
    monkeypatch.setenv(app.AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.delenv(app.UI_ENV_VAR, raising=False)
    outputs: list[str] = []
    code = app.run(
        ["--stub-llm", "--zpub", ZPUB, "--web"], output_fn=outputs.append
    )
    assert code == 2
    assert "Could not start the web server." in outputs
    joined = " ".join(outputs)
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
    assert snapshot["last_event_id"] > 0  # engine-published cursor (F5)
    assert set(snapshot) == {"last_event_id", "buffered_events", "subscribers"}
    assert server.token.encode() not in data  # never via /state
    # The snapshot request itself went through the pump as a typed line —
    # the queued STATE_PING_COMMAND round-trips the engine (busy turns are
    # honored first; the marker is what releases the wait):
    assert echo_turns == ["before"]  # the "/state" line is a transcript
    # command, never an LLM turn — _run_turn (echoed above) never saw it.
    stream.close()


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

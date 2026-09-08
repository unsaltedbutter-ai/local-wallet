"""The localhost web server (TCK-WEB-002, ADR-0024).

Thin stdlib HTTP/SSE transport over the engine pump (:func:`localwallet.app`
``start_engine`` / ``EngineHandle``). Invariants this module upholds:

* **Threading (ADR-0024 §3):** ONE engine thread owns all state. Transport
  threads marshal bytes only — every mutation funnels through
  ``engine.submit()`` into the pump's full ``_run_turn`` path (a button
  utterance is a TYPED LINE, never a handler/flow bypass, §8), and reads
  serialize through the engine queue too (GET /state, §3 consult F5).
* **SSE (§5):** fetch-compatible ``id:``/``event:``/``data:`` frames; a
  server-side event-id ring buffer replayed from ``Last-Event-ID`` — the
  replay backlog is streamed straight to the socket under the same lock
  hold that registers the live queue, so a stale or absent cursor ALWAYS
  makes progress (everything retained, in order, no gap, no duplicate —
  never a dead-on-arrival connection); BOUNDED per-connection LIVE queues —
  backlog overflow CLOSES that connection (client replays), the engine
  NEVER blocks on a browser; a ``: ping`` heartbeat comment frame on idle;
  broken-pipe/reset swallowed (no tracebacks); one frame per write with
  ``TCP_NODELAY``; every connection socket bounded by a read/write timeout
  (``_Handler.handle``); sentinel shutdown with ``block_on_close=False`` so
  parked SSE threads never hang close (F4.4).
* **Security (§6):** binds 127.0.0.1 only, ephemeral port; a random
  per-launch token gates EVERY endpoint via the ``X-Auth-Token`` header (the
  401 path deliberately does NOT send ``WWW-Authenticate`` — a browser would
  pop a native credential prompt); the token never appears in URLs, logs,
  error bodies, or /state — it reaches the client only through the JSON
  island injected into the served index.html. No CORS headers, no
  ``Set-Cookie``, access logging suppressed entirely. The full
  Host/Origin/CSP hardening matrix is TCK-WEB-003.
* **HTTP/1.0 (§2):** the stdlib default, ACCEPTED and PINNED, not "fixed" —
  each browser request gets its own connection, which is exactly the
  request/response + one-long-lived-SSE shape we need. No chunked encoding
  is ever hand-rolled here; SSE needs none (frames flush per write).

Static files are served from this package's ``static/`` dir (owned by the
web-builder agent; a missing dir or file is a graceful 404). Packaging
will move the lookup to ``importlib.resources`` in TCK-WEB-006 (ADR-0024
§12).
"""

from __future__ import annotations

import collections
import hmac
import json
import mimetypes
import queue
import secrets
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final

from localwallet.app import EngineEvent, EngineHandle, start_engine

#: Loopback bind (ADR-0024 §1): never 0.0.0.0, and not configurable — the
#: loopback-only invariant is structural, not a default.
HOST: Final[str] = "127.0.0.1"
#: Auth header carrying the per-launch token (the ONLY credential, §6).
AUTH_HEADER: Final[str] = "X-Auth-Token"
#: Cap on POST bodies (chat lines are short; this bounds per-request memory).
MAX_BODY_BYTES: Final[int] = 1 << 20
#: GET /state waits this long for its queued request's turn_end marker; a
#: busy engine (turn in flight) legitimately delays the snapshot.
STATE_TIMEOUT_S: Final[float] = 120.0
#: Idle SSE connections get a ``: ping`` comment frame this often (§5).
HEARTBEAT_S: Final[float] = 15.0
#: Per-connection socket timeout, reads AND writes: a socket stalled this
#: long on a client that stopped reading/writing is treated as gone (send
#: timeouts must not stretch shutdown toward forever, F4.4).
SEND_TIMEOUT_S: Final[float] = 30.0
#: Ring buffer capacity (§5 replay window) and the per-connection queue cap.
RING_SIZE: Final[int] = 2048
QUEUE_MAXSIZE: Final[int] = 512

#: The transcript command GET /state queues as its serialization ping. It is
#: an UNKNOWN slash-command, so the pump routes it to
#: ``app._handle_transcript_command``'s deterministic help fallback — no
#: model, no side effect; its completion marker (like any processed command)
#: proves the engine drained the queue up to our request. The visible echo
#: of the help line is the honest cost of F5 serialization until the pump
#: grows typed snapshot commands (WEB-005).
STATE_PING_COMMAND: Final[str] = "/state"


class _ClientGone(Exception):
    """A client write failed (reset/dead/stalled peer) — not a server error."""


# --------------------------------------------------------------- event fan-out


@dataclass
class _Subscriber:
    """One SSE connection's bounded inbound slice of the event stream."""

    events: queue.Queue[EngineEvent | None]
    dead: bool = False


class _Bus:
    """Ring buffer + bounded per-connection fan-out, fed by the engine thread.

    ``publish`` runs ON the engine thread (it is the emitter's sink) and must
    NEVER block: a slow or stuck browser is dropped (marked dead, removed)
    the moment its bounded LIVE queue fills — the client recovers by
    reconnecting with ``Last-Event-ID`` and replaying from the ring (§5).
    ``subscribe`` snapshots the retained ring and registers the live queue
    under ONE lock hold, so every event lands in exactly one of the two
    sets: gap-free, duplicate-free. The snapshot is returned for the
    connection owner to stream straight to the socket — NOT through the
    bounded queue — so even a full-ring backlog with a stale cursor cannot
    overflow a fresh subscriber into dead-on-arrival (the old livelock).
    A cursor predating the ring still gets everything retained, in order
    (an honest partial replay beats a spurious gap; the explicit
    too-far-behind signal stays WEB-003's).
    """

    def __init__(self, ring_size: int = RING_SIZE, maxsize: int = QUEUE_MAXSIZE) -> None:
        self._lock = threading.Lock()
        self._ring: collections.deque[EngineEvent] = collections.deque(maxlen=ring_size)
        self._maxsize = maxsize
        self._subs: list[_Subscriber] = []
        self.last_id = 0
        self.closed = False

    def publish(self, event: EngineEvent) -> None:
        """Engine-thread sink. Takes the same lock /state uses — brief,
        uncontended; the only blocking risk would be an unbounded operation,
        and there is none (put_nowait)."""
        with self._lock:
            if self.closed:
                return
            self._ring.append(event)
            self.last_id = event.id
            for sub in list(self._subs):
                try:
                    sub.events.put_nowait(event)
                except queue.Full:
                    # overflow -> close THIS connection only (bounded queues, §5)
                    self._drop(sub)

    def subscribe(self, replay_after: int) -> tuple[_Subscriber, list[EngineEvent]]:
        """Register a live subscriber and snapshot the replay window under
        ONE lock hold. Returns ``(sub, replay)``: ``replay`` is the retained
        ring past ``replay_after`` (streamed straight to the socket by the
        caller, never through ``sub.events``), and everything published from
        here on lands in ``sub.events``. Serialized against ``publish`` by
        ``_lock``, so the two sets are contiguous and disjoint — gap-free,
        duplicate-free — and a full-ring backlog can never overflow the
        bounded live queue during replay (no dead-on-arrival livelock)."""
        sub = _Subscriber(events=queue.Queue(maxsize=self._maxsize))
        with self._lock:
            if self.closed:
                sub.dead = True
                return sub, []
            replay = [e for e in self._ring if e.id > replay_after]
            self._subs.append(sub)
            return sub, replay

    def unsubscribe(self, sub: _Subscriber) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "last_event_id": self.last_id,
                "buffered_events": len(self._ring),
                "subscribers": len(self._subs),
            }

    def shutdown(self) -> None:
        """Mark closed and push the ``None`` sentinel to every parked writer
        thread so no SSE connection can hold shutdown hostage (F4.4; pairs
        with ``block_on_close=False``)."""
        with self._lock:
            self.closed = True
            subs, self._subs = self._subs, []
        for sub in subs:
            sub.dead = True
            try:
                sub.events.put_nowait(None)
            except queue.Full:
                pass  # the dead flag already dooms it; the writer will exit

    def _drop(self, sub: _Subscriber) -> None:
        """Caller holds the lock."""
        sub.dead = True
        if sub in self._subs:
            self._subs.remove(sub)


def sse_frame(event: EngineEvent) -> bytes:
    """One SSE frame (§5: one frame per write): ``id:``/``event:``/``data:``
    lines, payload carried verbatim (never re-encoded, never "corrected"),
    multi-line payloads as consecutive ``data:`` lines per the spec."""
    lines = [f"id: {event.id}", f"event: {event.kind}"]
    lines += [f"data: {chunk}" for chunk in event.payload.split("\n")]
    return ("\n".join(lines) + "\n\n").encode("utf-8")


# ----------------------------------------------------------------- HTTP surface


class _Handler(BaseHTTPRequestHandler):
    """One connection. HTTP/1.0 (the stdlib default) — kept, documented,
    and test-pinned per ADR-0024 §2."""

    server_version = "localwallet"
    sys_version = ""  # never advertise the Python version
    wbufsize = 0  # unbuffered writes: one frame per TCP write (§5)
    disable_nagle_algorithm = True  # TCP_NODELAY on every handler socket (§5)

    def __init__(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
        server: Any,
        **injected: Any,
    ) -> None:
        # Server-owned state (bus/token/handle/ knobs) arrives via the
        # functools.partial factory; instance attrs before super().__init__
        # (which dispatches do_GET straight away).
        for name, value in injected.items():
            setattr(self, name, value)
        super().__init__(request, client_address, server)

    # Injected attributes (declared for reading; set from the partial above).
    bus: _Bus
    token: str
    engine: EngineHandle
    static_dir: Path
    heartbeat_s: float
    state_timeout_s: float
    send_timeout_s: float

    # -- logging: suppressed entirely (token never reaches stderr, §6) ------
    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    def log_error(self, format: str, *args: Any) -> None:
        del format, args

    # -- plumbing -----------------------------------------------------------
    def handle(self) -> None:
        # One socket timeout for the WHOLE connection: it bounds non-SSE
        # request reads (a client that opens a socket and stalls can no
        # longer park a handler thread forever) AND bounds SSE writes (a
        # browser that stopped reading is dropped on send-timeout, §5/F4.4).
        self.connection.settimeout(self.send_timeout_s)
        # Broken pipe / reset mid-request is a normal Tuesday for a browser
        # tab that closed: swallow quietly, no traceback (§5).
        try:
            super().handle()
        except OSError:
            return

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        self._respond(status, "application/json", json.dumps(payload).encode())

    def _respond(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authorized(self) -> bool:
        got = self.headers.get(AUTH_HEADER, "")
        return hmac.compare_digest(got.encode("utf-8", "replace"), self.token.encode())

    def _require_token(self) -> bool:
        if self._authorized():
            return True
        # Deliberate: no WWW-Authenticate header. It is the standard 401
        # companion, but on a top-level document load it pops a native
        # browser credential dialog — terrible UX and an auth-surface hint
        # to any origin that can make the user type. Status + body only.
        self._send_json(401, {"error": "unauthorized"})
        return False

    def _path(self) -> str:
        return urllib.parse.urlparse(self.path).path  # query NEVER carries the token

    # -- GET ----------------------------------------------------------------
    def do_GET(self) -> None:
        if not self._require_token():
            return
        path = self._path()
        if path == "/events":
            self._events()
        elif path == "/state":
            self._state()
        elif path in ("/", "/index.html"):
            self._static("index.html", inject_token=True)
        elif path.startswith("/static/"):
            self._static(path[len("/static/") :])
        else:
            self._send_json(404, {"error": "not found"})

    # -- POST ---------------------------------------------------------------
    def do_POST(self) -> None:
        if not self._require_token():
            self._drain_body()
            return
        path = self._path()
        if path not in ("/turn", "/action"):
            self._drain_body()
            self._send_json(404, {"error": "not found"})
            return
        field = "text" if path == "/turn" else "utterance"
        body = self._read_body()
        if body is None:
            return  # 413 already sent
        try:
            payload = json.loads(body)
            value = payload[field] if isinstance(payload, dict) else None
        except (ValueError, KeyError, TypeError):
            self._send_json(400, {"error": f"expected JSON object with a '{field}' key"})
            return
        if not isinstance(value, str):
            self._send_json(400, {"error": f"'{field}' must be a string"})
            return
        # The ONLY engine call any route makes: bytes onto the command queue.
        # /action is the button path (ADR-0024 §8): its canonical utterance
        # runs the FULL pump pipeline (confirm gate -> loop -> allowlist) as
        # a typed line — this module never imports, touches, or could reorder
        # a flow/handler call (a test pins the surface).
        self.engine.submit(value)
        self._send_json(202, {"status": "queued"})

    def _read_body(self) -> bytes | None:
        try:
            # CLAMP: rfile.read(-N) means read-until-EOF — a negative
            # Content-Length would park this thread until the client
            # closes. A bogus size gets a plain empty body → clean 400.
            length = max(0, int(self.headers.get("Content-Length") or 0))
        except ValueError:
            self._send_json(400, {"error": "bad Content-Length"})
            return None
        if length > MAX_BODY_BYTES:
            # Drain (bounded) BEFORE answering so the client reliably sees
            # the 413 instead of a connection reset mid-send.
            self._drain_body()
            self.close_connection = True
            self._send_json(413, {"error": "body too large"})
            return None
        return self.rfile.read(length)

    def _drain_body(self) -> None:
        """Read (and discard) a rejected POST body so the response we just
        wrote is the last word on this HTTP/1.0 connection — an unread
        request body + immediate close makes some clients report a
        connection error instead of the 401/404 they were actually given."""
        try:
            length = max(0, int(self.headers.get("Content-Length") or 0))
        except ValueError:
            return
        remaining = min(length, MAX_BODY_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                return
            remaining -= len(chunk)

    # -- SSE ----------------------------------------------------------------
    def _events(self) -> None:
        try:
            replay_after = int(self.headers.get("Last-Event-ID") or 0)
        except ValueError:
            replay_after = 0
        sub, replay = self.bus.subscribe(replay_after)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        # Writes are bounded by the connection socket timeout (set in
        # handle()): a parked kernel buffer must not stretch shutdown
        # toward forever (F4.4). The replay backlog streams to the socket
        # FIRST — bounded by client backpressure, NOT by the live queue's
        # cap — then live events flow via queue.get() below.
        try:
            for event in replay:
                self.wfile.write(sse_frame(event))
            self._pump_sse(sub)
        except (_ClientGone, OSError):
            pass  # browser went away mid-frame: tear down, no traceback (§5)
        finally:
            self.bus.unsubscribe(sub)

    def _pump_sse(self, sub: _Subscriber) -> None:
        while True:
            if sub.dead:  # live-queue overflow or shutdown: close, client replays
                raise _ClientGone
            try:
                item = sub.events.get(timeout=self.heartbeat_s)
            except queue.Empty:
                self.wfile.write(b": ping\n\n")  # heartbeat comment frame (§5)
                continue
            if item is None:
                raise _ClientGone  # shutdown sentinel
            self.wfile.write(sse_frame(item))

    # -- state --------------------------------------------------------------
    def _state(self) -> None:
        # Reads serialize through the engine queue (consult F5): a snapshot
        # request is QUEUED as a typed line (STATE_PING_COMMAND — a
        # deterministic transcript no-op, never the model) and we wait for
        # any engine event past our queue point before answering. The
        # snapshot itself is pure transport state published BY the engine
        # thread (the bus cursor) — the store/flow are never touched from a
        # transport thread, and the token is never in it.
        marker_seen = self.bus.last_id
        self.engine.submit(STATE_PING_COMMAND)
        deadline = time.monotonic() + self.state_timeout_s
        while time.monotonic() < deadline:
            if self.bus.last_id > marker_seen or self.engine.error is not None:
                break
            time.sleep(0.01)
        self._send_json(200, self.bus.snapshot())

    # -- static -------------------------------------------------------------
    def _static(self, rel: str, inject_token: bool = False) -> None:
        target = (self.static_dir / rel).resolve()
        root = self.static_dir.resolve()
        if not target.is_relative_to(root) or not target.is_file():
            self._send_json(404, {"error": "not found"})
            return
        body = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if inject_token and ctype == "text/html":
            body = inject_token_island(body.decode("utf-8"), self.token).encode("utf-8")
        self._respond(200, ctype, body)


def inject_token_island(html: str, token: str) -> str:
    """Template-inject the per-launch JSON island before ``</head>`` (or
    ``</body>``, or the front as last resort) — the ONLY channel by which the
    token reaches the client (never a URL, log, error body, or /state).
    WEB-003's CSP lands a script nonce for this inline island."""
    island = "<script>window.__LOCALWALLET__ = " + json.dumps({"token": token}) + ";</script>"
    lowered = html.lower()
    for marker in ("</head>", "</body>"):
        idx = lowered.rfind(marker)
        if idx != -1:
            return html[:idx] + island + "\n" + html[idx:]
    return island + html


class _Server(ThreadingHTTPServer):
    """Daemon transport threads + non-blocking close (ADR-0024 §5).

    ``block_on_close=True`` (stdlib default on ThreadingHTTPServer) is the
    documented hang trap (F4.4): shutdown would wait for every handler
    thread, including a browser parked on an SSE stream. The bus sentinel
    unblocks those writers; the server never waits for them.
    """

    daemon_threads = True
    block_on_close = False

    # ponytail: thread-per-connection — every browser tab holds a thread
    # (SSE parks in queue.get). Ceiling: a few dozen concurrent connections
    # before thread churn matters; the local-process trust model (ADR-0024
    # §6: loopback bind + per-launch token, so the only clients are this
    # machine's own browsers) makes that ceiling unreachable in practice.
    # Upgrade path if it ever matters: a single selector/poll reader loop,
    # not a framework. Reads/writes are already socket-timeout-bounded
    # (_Handler.handle) so a stalled peer cannot pin a thread forever.
    def handle_error(self, request: Any, client_address: tuple[str, int]) -> None:
        # A connection dying mid-teardown is a normal browser event; the
        # stdlib's stderr traceback is noise here (no secrets — the access
        # log path is separately suppressed, §6). Engine-side failures travel
        # through handle.error instead.
        del request, client_address


class WebServer:
    """The server half of the web UI: owns ONE engine instance plus the
    loopback HTTP/SSE front. Lifecycle: :meth:`serve` (background thread) →
    :meth:`stop` (sentinel shutdown; joins the engine, never the browsers —
    ``block_on_close=False``, F4.4)."""

    def __init__(
        self,
        bootstrap: Callable[[], Any],
        *,
        static_dir: Path | None = None,
        port: int = 0,
        heartbeat_s: float = HEARTBEAT_S,
        state_timeout_s: float = STATE_TIMEOUT_S,
        send_timeout_s: float = SEND_TIMEOUT_S,
        queue_maxsize: int = QUEUE_MAXSIZE,
    ) -> None:
        self.token = secrets.token_urlsafe(32)
        self.bus = _Bus(maxsize=queue_maxsize)
        self.handle = start_engine(bootstrap, self.bus.publish)
        handler = partial(
            _Handler,
            bus=self.bus,
            token=self.token,
            engine=self.handle,
            static_dir=static_dir if static_dir is not None else _STATIC_DIR,
            heartbeat_s=heartbeat_s,
            state_timeout_s=state_timeout_s,
            send_timeout_s=send_timeout_s,
        )
        self.httpd = _Server((HOST, port), handler)
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()

    @property
    def url(self) -> str:
        """Launch URL — deliberately token-FREE (the token never rides a
        URL, §6; run() prints the token on its own line)."""
        return f"http://{HOST}:{self.httpd.server_address[1]}"

    def serve(self) -> None:
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, name="web-server", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Idempotent sentinel shutdown: close the engine (QUIT is honored
        between turns — never-cancel stands, §3), push the SSE sentinel,
        stop accepting, tear down the socket."""
        if self._stopped.is_set():
            return
        self._stopped.set()
        self.bus.shutdown()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.handle.shutdown()
        if self._thread is not None:
            self._thread.join(5)

    def wait(self, timeout: float | None = None) -> bool:
        return self._stopped.wait(timeout)


#: Default static dir (owned by the web-builder agent; may not exist yet —
#: serving 404s gracefully is pinned). WEB-006 moves this to
#: importlib.resources for the frozen build (ADR-0024 §12).
_STATIC_DIR: Final[Path] = Path(__file__).resolve().parent / "static"


def serve_web(
    bootstrap: Callable[[], Any],
    *,
    static_dir: Path | None = None,
    **options: Any,
) -> WebServer:
    """Start the loopback server (background thread) over a freshly started
    engine. Returns the :class:`WebServer` (caller owns ``stop()``)."""
    server = WebServer(bootstrap, static_dir=static_dir, **options)
    server.serve()
    return server

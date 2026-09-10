"""The localhost web server (TCK-WEB-002, ADR-0024).

Thin stdlib HTTP/SSE transport over the engine pump (:func:`localwallet.app`
``start_engine`` / ``EngineHandle``). Invariants this module upholds:

* **Threading (ADR-0024 §3):** ONE engine thread owns all state. Transport
  threads marshal bytes only — every mutation funnels through
  ``engine.submit()`` into the pump's full ``_run_turn`` path (a button
  utterance is a TYPED LINE, never a handler/flow bypass, §8), and reads
  serialize through the engine queue too (GET /state, §3 consult F5;
  GET/POST /settings ride the same queue via ``SettingsRequest`` — the
  transport never touches the store, TCK-WEB-005).
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
* **Security (§6):** binds 127.0.0.1 only, ephemeral port (fixed-port
  opt-in via ``LOCALWALLET_WEB_PORT``, ADR-0024 §6 amendment); a random
  per-launch token gates every DATA-BEARING endpoint (``/events``,
  ``/state``, ``/turn``, ``/action``, ``/settings``, ``/watchkey``) via the ``X-Auth-Token`` header (the
  401 path deliberately does NOT send ``WWW-Authenticate`` — a browser would
  pop a native credential prompt). The shell (``GET /``, ``/index.html``) and
  ``GET /static/*`` are the deliberate token EXEMPTION (TCK-WEB-007): the
  token reaches the client ONLY through the island injected into that page, so
  gating the delivery mechanism is a bootstrap deadlock — and the exemption is
  safe because the static shell carries no user data. The security surface
  stays: the Host allowlist runs FIRST on every request INCLUDING static
  (DNS-rebinding defense unweakened), CSP on every response, no CORS, no
  cookies, and every data-bearing endpoint still token-gated; a ``Host`` allowlist (``_require_host``) is
  the DNS-rebinding primary defense, with a proportionate same-origin check
  on POSTs (``_require_same_origin``) as belt-braces (no cookies ⇒ CSRF is
  structurally moot). The token never appears in URLs, logs, error bodies, or
  /state — it reaches the client only through the JSON island injected into
  the served index.html. A Content-Security-Policy header (ADR-0024 §7,
  ``csp_header``) allows NO inline script except that island (a per-response
  nonce), no eval, same-origin styles/connect only. No CORS headers, no
  ``Set-Cookie``, access logging suppressed entirely.
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
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final

from localwallet.app import (
    STATE_SCHEMA_TRANSPORT_ONLY,
    EngineEvent,
    EngineHandle,
    start_engine,
)

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

#: Hosts the server will answer for (ADR-0024 §6, DNS-rebinding primary
#: defense). The port is ephemeral/variable so the comparison is on the
#: HOSTNAME ONLY (port stripped); the bound interface is 127.0.0.1 and the
#: canonical launch URL uses it, but ``localhost`` is a first-class alias a
#: user may type. A public name that rebinds to 127.0.0.1 presents its OWN
#: host header and is refused here — independent of the token layer.
ALLOWED_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost"})

#: Content-Security-Policy for the shell + island (ADR-0024 §7). ``{nonce}`` is
#: substituted per-response: the injected token island is the ONE inline script
#: and carries a fresh nonce. NO ``unsafe-inline``, NO ``unsafe-eval``; same-
#: origin module scripts + stylesheet only. ``default-src 'none'`` closes every
#: other fetch; ``connect-src 'self'`` is the fetch+SSE stream;
#: ``base-uri``/``form-action``/``frame-ancestors`` are the standard belt braces.
CSP_TEMPLATE: Final[str] = (
    "default-src 'none'; "
    "script-src 'self'{nonce}; "
    "style-src 'self'; "
    "connect-src 'self'; "
    "img-src 'self'; "
    "font-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)


def csp_header(nonce: str | None = None) -> str:
    """The CSP for one response. With a ``nonce`` the inline token island is
    allowed via ``'nonce-…'``; without one (static assets, JSON) the strict
    no-inline policy applies — ``'self'`` scripts only."""
    return CSP_TEMPLATE.format(nonce=f" 'nonce-{nonce}'" if nonce else "")


class _ClientGone(Exception):
    """A client write failed (reset/dead/stalled peer) — not a server error."""


# --------------------------------------------------------------- event fan-out


@dataclass
class _Subscriber:
    """One SSE connection's bounded inbound slice of the event stream."""

    events: queue.Queue[EngineEvent | None]
    dead: bool = False
    #: The oldest retained event id, set when this connection's ``Last-Event-ID``
    #: predates the ring (a gap the backlog cannot bridge) → the connection owner
    #: emits one explicit ``resync`` frame before replay (TCK-WEB-003). ``None``
    #: for a fresh (no-cursor) connection or a cursor inside the retained window.
    resync_from: int | None = None


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
    (an honest partial replay), AND now also an explicit ``resync`` signal
    stashed on the subscriber so the client can hard-resync — the gap is
    never silent (TCK-WEB-003).
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
        bounded live queue during replay (no dead-on-arrival livelock).

        If ``replay_after`` is a real cursor (``>= 1``) that predates the
        retained window (events were evicted before it), ``sub.resync_from``
        is set to the oldest retained id so the connection owner emits ONE
        explicit ``resync`` frame before the (partial) replay — the client
        hard-resyncs from ``/state`` instead of silently trusting a gap
        (TCK-WEB-003). A fresh (no-cursor) connection is NOT flagged: it is
        not resuming, so an honest full-backlog replay is correct, not a loss."""
        sub = _Subscriber(events=queue.Queue(maxsize=self._maxsize))
        with self._lock:
            if self.closed:
                sub.dead = True
                return sub, []
            replay = [e for e in self._ring if e.id > replay_after]
            if replay_after >= 1 and self._ring:
                oldest = self._ring[0].id
                if oldest > replay_after + 1:
                    sub.resync_from = oldest
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


def resync_frame(oldest_event_id: int) -> bytes:
    """The too-far-behind signal (TCK-WEB-003): a named ``resync`` event sent
    when a reconnect's ``Last-Event-ID`` predates the retained ring, so the
    client hard-resyncs (refetch ``/state`` / reload the transcript) rather than
    silently trusting a gap. Deliberately carries NO ``id:`` line — the client's
    cursor is not advanced by a transport signal; the replayed events that
    follow carry their own ids. A comment frame is NOT used: the client drops
    comment lines during parse, so a named event is the only shape its
    ``fetch`` reader can detect. Value-free (an event id is transport metadata,
    already exposed in /state)."""
    body = json.dumps({"reason": "too_far_behind", "oldest_event_id": oldest_event_id})
    return f"event: resync\ndata: {body}\n\n".encode()


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
        # WebServer handler factory (a closure over the live server);
        # instance attrs before super().__init__ (which dispatches do_GET
        # straight away).
        for name, value in injected.items():
            setattr(self, name, value)
        super().__init__(request, client_address, server)

    # Injected attributes (declared for reading; set by the factory).
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

    def _send_json(
        self, status: int, payload: Mapping[str, Any], *, csp: str | None = None
    ) -> None:
        self._respond(
            status,
            "application/json",
            json.dumps(payload).encode(),
            csp=csp if csp is not None else csp_header(),
        )

    def _respond(
        self, status: int, ctype: str, body: bytes, *, csp: str | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if csp is not None:
            # ADR-0024 §7: no inline script except the nonce-carrying island,
            # no eval. Sent on every HTML/asset/JSON response.
            self.send_header("Content-Security-Policy", csp)
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

    @staticmethod
    def _host_of(value: str) -> str:
        """Lowercased hostname with any port stripped (IPv6 de-bracketed).

        The port is ephemeral/variable (ADR-0024 §1) so the Host allowlist is
        a NAME comparison; brackets on an IPv6 literal are removed to match the
        bare ``::1``-style forms. Never raises; an empty/garbage value yields
        ``""`` and is refused by the caller.
        """
        host = value.strip().lower()
        if host.startswith("["):  # IPv6 literal [::1]:port
            end = host.find("]")
            return host[1:end] if end != -1 else host[1:]
        return host.rsplit(":", 1)[0] if ":" in host else host

    def _require_host(self) -> bool:
        """Host allowlist — the DNS-rebinding PRIMARY defense (ADR-0024 §6).

        A page that resolves a public name to 127.0.0.1 still presents that
        name in ``Host``; refusing any host outside the loopback allowlist
        kills the drive-by before the token even matters (an independent layer
        from the token header). Missing/hostile Host → 400, value-free (the
        offending host is never echoed back)."""
        if self._host_of(self.headers.get("Host", "")) in ALLOWED_HOSTS:
            return True
        self._send_json(400, {"error": "host not allowed"})
        return False

    def _require_same_origin(self) -> bool:
        """Same-origin check on state-changing requests (defense-in-depth).

        No cookies exist (ADR-0024 §6) so a cross-site request cannot ride
        ambient credentials and CSRF is structurally moot — a hostile page
        cannot present the per-launch token header. This check is therefore
        proportionate belt-braces, not the auth boundary: when a browser
        DOES send ``Origin`` (it does on POST), it must be a loopback origin
        matching the allowlist. Absent ``Origin`` (non-browser clients) passes.
        """
        origin = self.headers.get("Origin")
        if origin is None or self._host_of(urllib.parse.urlparse(origin).netloc) in (
            ALLOWED_HOSTS
        ):
            return True
        self._send_json(403, {"error": "cross-origin"})
        return False

    def _path(self) -> str:
        return urllib.parse.urlparse(self.path).path  # query NEVER carries the token

    # -- GET ----------------------------------------------------------------
    def do_GET(self) -> None:
        if not self._require_host():
            return
        path = self._path()
        # TCK-WEB-007 bootstrap exemption: the shell and its assets are served
        # BEFORE the token check — the token is DELIVERED by the island inside
        # index.html, so gating that page is a deadlock (a first browser
        # navigation cannot present a token it has not yet received). Safe
        # because the static shell carries no user data; the security surface
        # is the Host allowlist (checked FIRST, above, for static too —
        # DNS-rebinding defense unweakened), CSP on every response, no CORS,
        # no cookies, and the token-gated data-bearing endpoints below.
        if path in ("/", "/index.html"):
            self._static("index.html", inject_token=True)
            return
        if path.startswith("/static/"):
            self._static(path[len("/static/") :])
            return
        if path == "/favicon.ico":
            # TCK-LAUNCH-001: browsers request this by convention WITHOUT
            # the auth header (a root-level path, not /static/*), so the
            # 404 was an unavoidable console error. Serve the tracked stub
            # icon through the same static handler as every other asset
            # (Host allowlist + CSP already applied above; carries no data).
            self._static("favicon.ico")
            return
        if not self._require_token():
            return
        if path == "/events":
            self._events()
        elif path == "/state":
            self._state()
        elif path == "/settings":
            self._settings_get()
        else:
            self._send_json(404, {"error": "not found"})

    # -- POST ---------------------------------------------------------------
    def do_POST(self) -> None:
        if not self._require_host():
            self._drain_body()
            return
        if not self._require_same_origin():
            self._drain_body()
            return
        if not self._require_token():
            self._drain_body()
            return
        path = self._path()
        if path not in ("/turn", "/action", "/settings", "/watchkey"):
            self._drain_body()
            self._send_json(404, {"error": "not found"})
            return
        body = self._read_body()
        if body is None:
            return  # 413 already sent
        if path == "/settings":
            # The ONLY general write endpoint: a single allowlisted key,
            # validated fail-closed ON THE ENGINE THREAD (TCK-WEB-005). The
            # transport never touches the store — it marshals the request
            # through the pump queue like every other state access.
            self._settings_post(body)
            return
        if path == "/watchkey":
            # TCK-LAUNCH-001 first-run watch-key entry (the ONE other
            # mutating endpoint): the transport marshals the key string
            # THROUGH the pump (WatchKeyRequest); the EXISTING parse+gate
            # path runs on the engine thread. No key material is ever
            # parsed, stored, echoed, or logged here.
            self._watchkey_post(body)
            return
        field = "text" if path == "/turn" else "utterance"
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
            if sub.resync_from is not None:
                # Cursor predates the retained ring: one explicit resync
                # signal, THEN the honest partial backlog (TCK-WEB-003).
                self.wfile.write(resync_frame(sub.resync_from))
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
        # Reads serialize through the engine queue (consult F5). The transport
        # asks the engine for a TYPED, value-free snapshot (flow position +
        # watch status, TCK-WEB-003) via StateSnapshotRequest — it never touches
        # the flow/store itself. The engine answers between turns; a busy engine
        # past the timeout is not a stall: we fall back to the transport-only
        # shape (schema state/0), which the client already tolerates (it keys off
        # the transport fields, present in BOTH shapes). The token is never here.
        base = self.bus.snapshot()
        # A dead/errored engine will never drain the queue — answer at once with
        # the transport-only shape (the old STATE_PING fast-fail, preserved).
        typed = None if self.engine.error is not None else self.engine.request_state(
            self.state_timeout_s
        )
        if typed is None:
            self._send_json(200, {**base, "schema": STATE_SCHEMA_TRANSPORT_ONLY})
            return
        self._send_json(200, {**base, **typed})

    # -- settings (TCK-WEB-005) ----------------------------------------------
    def _settings_get(self) -> None:
        # Reads serialize through the engine queue exactly like /state: the
        # transport asks for the ALLOWLISTED settings snapshot and never touches
        # the store. No transport-only fallback exists for settings (a settings
        # page that shows stale/absent values is worse than an honest 503): a
        # busy/dead engine answers 503 and the client retries. Value-free —
        # only user-authored scalars (gap count, backend URL) and the PUBLIC
        # watch key appear (truncated in the list; the FULL key only on the
        # explicit ``?key=watch_key`` single-key read — the settings panel's
        # Show/Copy click, TCK-WEB-008 follow-up (a); never in any log, and
        # this endpoint is token-gated). The token is never here.
        key = self._query_key()
        settings = (
            None
            if self.engine.error is not None
            # key=None → the general allowlisted list; key set → the
            # ENGINE-decided single-key read (only watch_key reveals).
            else self.engine.request_settings(self.state_timeout_s, key)
        )
        if settings is None:
            self._send_json(503, {"error": "engine busy"})
            return
        self._send_json(200, settings)

    def _query_key(self) -> str | None:
        """The single ``key`` query parameter of GET /settings (the explicit
        single-key READ rung; TCK-WEB-008 follow-up (a)). Absent/blank/multi-
        valued → ``None`` (the general list read). The value is untrusted and
        never echoed — the ENGINE allowlist decides what a key read answers."""
        try:
            values = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query
            ).get("key") or []
        except ValueError:
            return None
        return values[0] if len(values) == 1 and values[0] else None

    def _settings_post(self, body: bytes) -> None:
        # ONE key per write (the engine applies + re-reads it, or refuses with
        # a value-free error). HTTP maps the engine's closed status:
        # applied→200, rejected→400, anything else (unavailable)→503.
        try:
            payload = json.loads(body)
            key = payload["key"] if isinstance(payload, dict) else None
            value = payload["value"] if isinstance(payload, dict) else None
        except (ValueError, KeyError, TypeError):
            self._send_json(
                400, {"error": "expected JSON object with 'key' and 'value'"}
            )
            return
        if not isinstance(key, str) or not isinstance(value, str):
            self._send_json(400, {"error": "'key' and 'value' must be strings"})
            return
        # (No value checks here: ALL validation — allowlist, type, bounds,
        # size cap — is engine-owned and fail-closed; the request body is
        # already size-bounded by _read_body. The transport never duplicates
        # a rule it cannot see the truth of.)
        result = (
            None
            if self.engine.error is not None
            else self.engine.request_settings(self.state_timeout_s, key, value)
        )
        if result is None:
            # Dead engine: no apply ever happened. A TIMEOUT is the never-
            # cancel semantics POST /turn already has: the queued write may
            # still land when the engine drains — the client re-reads via GET
            # rather than assuming failure (never a silent double-submit).
            self._send_json(503, {"error": "engine busy"})
            return
        status = result.get("status")
        code = {"applied": 200, "rejected": 400}.get(status, 503)
        self._send_json(code, result)

    def _watchkey_post(self, body: bytes) -> None:
        # TCK-LAUNCH-001 first-run watch-key submit (and the TCK-LAUNCH-002
        # in-place REPLACE rung, ADR-0024 amendment). The transport ONLY
        # shape-checks the JSON (a string ``key``; STRICT boolean
        # ``replace``+``confirm`` flags) and marshals it through the engine
        # queue (``WatchKeyRequest``). ALL key validation — parse,
        # mainnet-only gate, watch-only refusals, seed-phrase detection —
        # is the EXISTING engine path (app.py), fail-closed, with
        # value-free refusals; this module never imports a key parser.
        # REPLACE requires BOTH flags (the double opt-in the settings
        # panel's confirm step raises) and reruns the SAME gated path —
        # the transport's verdict is structurally impossible here. HTTP
        # maps the closed engine status: accepted/replaced→200,
        # rejected→400, already→409, store_error/unavailable/busy→503.
        # The key never rides back.
        try:
            payload = json.loads(body)
            key = payload["key"] if isinstance(payload, dict) else None
            replace = payload.get("replace") if isinstance(payload, dict) else None
            confirm = payload.get("confirm") if isinstance(payload, dict) else None
        except (ValueError, KeyError, TypeError):
            self._send_json(400, {"error": "expected JSON object with a 'key' string"})
            return
        if not isinstance(key, str):
            self._send_json(400, {"error": "'key' must be a string"})
            return
        allow_replace = replace is True and confirm is True
        result = (
            None
            if self.engine.error is not None
            else self.engine.request_watchkey(
                self.state_timeout_s, key, allow_replace=allow_replace
            )
        )
        if result is None:
            # Dead/timeout engine: the never-cancel POST contract stands —
            # the submit may still land; the client re-reads /state rather
            # than assuming failure (never a silent double-submit).
            self._send_json(503, {"error": "engine busy"})
            return
        status = result.get("status")
        code = {
            "accepted": 200,
            "replaced": 200,
            "rejected": 400,
            "already": 409,
        }.get(status, 503)
        self._send_json(code, result)

    # -- static -------------------------------------------------------------
    def _static(self, rel: str, inject_token: bool = False) -> None:
        target = (self.static_dir / rel).resolve()
        root = self.static_dir.resolve()
        if not target.is_relative_to(root) or not target.is_file():
            self._send_json(404, {"error": "not found"})
            return
        body = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        nonce: str | None = None
        if inject_token and ctype == "text/html":
            # Fresh per-response nonce (ADR-0024 §7): ONLY the token island
            # script carries it; the CSP that follows allows exactly it.
            nonce = secrets.token_urlsafe(16)
            body = inject_token_island(
                body.decode("utf-8"), self.token, nonce=nonce
            ).encode("utf-8")
        self._respond(200, ctype, body, csp=csp_header(nonce))


def inject_token_island(html: str, token: str, *, nonce: str = "") -> str:
    """Template-inject the per-launch JSON island before ``</head>`` (or
    ``</body>``, or the front as last resort) — the ONLY channel by which the
    token reaches the client (never a URL, log, error body, or /state). The
    inline island script carries the per-response ``nonce`` that
    :func:`csp_header` whitelists (ADR-0024 §7); a blank nonce (the default,
    used by the isolated unit tests / a no-CSP context) emits a bare script."""
    attr = f' nonce="{nonce}"' if nonce else ""
    island = (
        f"<script{attr}>window.__LOCALWALLET__ = "
        + json.dumps({"token": token})
        + ";</script>"
    )
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
        ring_size: int = RING_SIZE,
    ) -> None:
        self.token = secrets.token_urlsafe(32)
        self.bus = _Bus(ring_size=ring_size, maxsize=queue_maxsize)

        def handler(
            request: socket.socket, client_address: tuple[str, int], server: Any
        ) -> _Handler:
            # Lazy read of self.handle: the engine starts AFTER the bind
            # below, and a request can only arrive after serve() — by then
            # the handle exists. Binding FIRST means a failed bind (busy
            # fixed port, TCK-LAUNCH-001) raises with NO engine thread
            # left behind, instead of orphaning one that would keep wiring
            # and pumping forever (daemon only at interpreter exit).
            return _Handler(
                request,
                client_address,
                server,
                bus=self.bus,
                token=self.token,
                engine=self.handle,
                static_dir=static_dir if static_dir is not None else _STATIC_DIR,
                heartbeat_s=heartbeat_s,
                state_timeout_s=state_timeout_s,
                send_timeout_s=send_timeout_s,
            )

        self.httpd = _Server((HOST, port), handler)
        self.handle = start_engine(bootstrap, self.bus.publish)
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

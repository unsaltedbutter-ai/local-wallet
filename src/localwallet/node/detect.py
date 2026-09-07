"""Local Bitcoin Core / mempool / electrs detection (Phase 4, TCK-P4-001).

Detects a user's OWN node/daemon instances bound to the loopback interface.
This is the Phase 4 privacy upgrade (moving chain I/O onto the user's machine,
per PROJECT.md §12 / ADR-0003 "Phase 4 replaces the backend with a self-hosted
instance") — it is NOT a data leak: every request targets ``127.0.0.1`` /
``localhost`` and nothing leaves the machine. Network access is permitted here
via the scoped exception added to ``tools/lint_network.py`` (ADR-0016,
:data:`NODE_NETWORK_DIRS`); ``chain/`` remains the only module that may touch
remote hosts.

Design decisions (see docs/adr/0016-localhost-node-io.md):

- **localhost-only probing.** We never resolve or contact a public host. All
  ports are probed on ``127.0.0.1`` with tight timeouts so a hung daemon can
  never stall the app.
- **Detection is a state machine, never a crash.** Each probe resolves to a
  ``NodeStatus``: :attr:`NodeStatus.REACHABLE`, :attr:`NodeStatus.OFFLINE`
  (the clean "no node here" state), :attr:`NodeStatus.AUTH_FAILED`
  (reachable but the RPC cookie is wrong/missing), or
  :attr:`NodeStatus.MALFORMED` (reachable but the RPC/HTTP response is not the
  shape we expect). Unreachable instances land in ``OFFLINE`` — detection
  never raises for a missing daemon.
- **Value-free diagnostics.** Probe results carry ports/kinds as plain data,
  but no error string ever contains an address, amount, or the RPC cookie
  CONTENT (a secret, PROJECT.md §7.8). The cookie is read only to build the
  Basic-auth header; it is never logged or returned.
- **Advise-only.** This module only *detects and reports*; it never executes
  commands or runs privileged operations. The agent (later, TCK-P4-003) merely
  narrates the guidance content in ``doctor.py``.
- **Cookie auth is canonical.** Bitcoin Core RPC is authenticated with the
  cookie file (``-rpccookiefile``), never ``-rpcuser``/``-rpcpassword`` in
  configuration or logs.

Port/cookie conventions are cited inline against Bitcoin Core v28.0
(``src/chainparamsbase.cpp`` / ``src/kernel/chainparams.cpp``) and the
mempool.space / electrs docs. All are configurable via :class:`Settings`
(``LOCALWALLET_RPC_PORT``, ``LOCALWALLET_RPC_COOKIE_PATH``,
``LOCALWALLET_LOCAL_MEMPOOL_URL``, ``LOCALWALLET_NODE_DETECTION_ENABLED``).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final, Self

import httpx

from localwallet.config import Settings

__all__ = [
    "BitcoinCoreDetector",
    "CoreHealth",
    "CoreRpcProbe",
    "LocalNodeReport",
    "NodeKind",
    "NodeStatus",
    "detect_local_nodes",
]

#: Bitcoin Core JSON-RPC default ports. Source: Bitcoin Core v28.0
#: ``src/chainparamsbase.cpp``, ``CreateBaseChainParams`` — mainnet 8332,
#: testnet3 18332, signet 38332, regtest 18443. The project is mainnet-only
#: (ADR-0021), so mainnet 8332 is probed first; the legacy/dev networks are
#: kept so a user's existing node is still found. Each port maps to a
#: per-network cookie datadir (see ``_COOKIE_BY_PORT``).
CORE_RPC_PORTS: Final[tuple[int, ...]] = (8332, 18332, 18443, 38332)

#: Well-known self-hosted mempool.space API port (backend), and the electrs
#: Esplora HTTP port. The mempool URL is configurable via Settings; the
#: electrs port is a documented default (full base_url configurability lands
#: with the backend wiring in TCK-P4-002).
MEMPOOL_API_PORT: Final[int] = 3006
ELECTRS_ESPLORA_HTTP_PORT: Final[int] = 3002

#: Per-network RPC port → relative cookie path (within the data dir). In
#: Bitcoin Core, each network's cookie lives in that network's net-specific
#: data dir; mainnet (8332) uses the data dir root (``~/.bitcoin/.cookie``).
#: ``.cookie`` is the conventional cookie filename.
_COOKIE_BY_PORT: Final[dict[int, str]] = {
    8332: ".cookie",
    18332: "testnet3/.cookie",
    18443: "regtest/.cookie",
    38332: "signet/.cookie",
}

_LOCALHOST = "127.0.0.1"

#: The only hosts node detection is ever allowed to probe (ADR-0016 localhost
#: contract). A configured URL whose host is not in this set is never probed.
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})

_CORE_RPC_METHOD = "getblockchaininfo"
_RPC_ID = 1


class NodeKind(str, Enum):
    """The kind of node/daemon an instance is."""

    BITCOIN_CORE = "bitcoin_core"
    MEMPOOL = "mempool"
    ELECTRS = "electrs"


class NodeStatus(str, Enum):
    """State-machine outcome of a probe (see module docstring)."""

    REACHABLE = "reachable"
    OFFLINE = "offline"
    AUTH_FAILED = "auth_failed"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class CoreHealth:
    """Sanitized summary of Bitcoin Core ``getblockchaininfo``.

    Carries only sync metadata — no addresses, amounts, or any wallet data
    (watch-only app; PROJECT.md §7.8). ``verification_progress`` is Core's
    0.0–1.0 IBD progress toward the assumevalid target.

    Attributes:
        chain: The network name Core reports (e.g. ``main``; ``main`` is the
            expected value for this mainnet-only app, ADR-0021).
        blocks: Local best-block height.
        headers: Headers received (upper bound on sync target).
        verification_progress: Core's IBD progress fraction (0.0–1.0).
        initial_block_download: Whether Core is still in initial block
            download (the "headers not caught up / syncing" state).
    """

    chain: str
    blocks: int
    headers: int
    verification_progress: float
    initial_block_download: bool

    @property
    def sync_percent(self) -> float:
        """IBD progress as a percentage (Core's ``verification_progress``)."""
        return self.verification_progress * 100.0

    @property
    def headers_progress_percent(self) -> float:
        """Headers received as a percentage of the local tip, or 0 if unknown."""
        if self.headers <= 0:
            return 0.0
        return max(0.0, min(100.0, (self.blocks / self.headers) * 100.0))

    @property
    def is_synced(self) -> bool:
        """True when headers are caught up and Core is out of initial download."""
        return self.headers > 0 and self.blocks >= self.headers and not self.initial_block_download


@dataclass(frozen=True)
class CoreRpcProbe:
    """Outcome of probing one Bitcoin Core RPC port on localhost.

    ``status`` is the state-machine result; ``health`` is populated only when
    the probe reached ``REACHABLE``. ``cookie_present`` records whether a
    cookie file was found to attempt authentication with.
    """

    port: int
    status: NodeStatus
    cookie_present: bool
    health: CoreHealth | None = None


@dataclass(frozen=True)
class LocalNodeReport:
    """The complete local-node detection result.

    ``core`` is one :class:`CoreRpcProbe` per probed RPC port;
    ``mempool``/``electrs`` are the loopback HTTP probe outcomes. Detection is
    purely advisory — nothing here implies any action was (or will be) taken.
    """

    core: tuple[CoreRpcProbe, ...]
    mempool: NodeStatus
    electrs: NodeStatus

    @property
    def reachable_kinds(self) -> tuple[NodeKind, ...]:
        """Kinds with at least one reachable instance, in a stable order."""
        kinds: list[NodeKind] = []
        if any(p.status is NodeStatus.REACHABLE for p in self.core):
            kinds.append(NodeKind.BITCOIN_CORE)
        if self.mempool is NodeStatus.REACHABLE:
            kinds.append(NodeKind.MEMPOOL)
        if self.electrs is NodeStatus.REACHABLE:
            kinds.append(NodeKind.ELECTRS)
        return tuple(kinds)

    @property
    def any_reachable(self) -> bool:
        return bool(self.reachable_kinds)


def _read_cookie(path: Path) -> str | None:
    """Return the raw cookie line (``user:pass``) or ``None`` if unreadable.

    The cookie CONTENT is a secret and is only ever used to build the Basic
    auth header — it is never logged, returned, or echoed in errors.
    """
    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text if text else None


def _basic_auth_header(cookie: str) -> dict[str, str]:
    """Build an ``Authorization`` header from a Bitcoin Core cookie line."""
    token = base64.b64encode(cookie.encode("ascii")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _parse_core_health(payload: object) -> CoreHealth:
    """Strictly validate a ``getblockchaininfo`` ``result`` (fail closed).

    Returns a sanitized :class:`CoreHealth`; any unexpected shape raises
    :class:`TypeError`, which the caller maps to
    :attr:`NodeStatus.MALFORMED`. Errors are value-free.
    """
    if not isinstance(payload, dict):
        raise TypeError("getblockchaininfo result was not an object")
    chain = payload.get("chain")
    if not isinstance(chain, str) or not chain:
        raise TypeError("getblockchaininfo missing 'chain'")
    blocks = payload.get("blocks")
    headers = payload.get("headers")
    progress = payload.get("verificationprogress")
    ibd = payload.get("initialblockdownload")
    if (
        isinstance(blocks, bool)
        or not isinstance(blocks, int)
        or isinstance(headers, bool)
        or not isinstance(headers, int)
    ):
        raise TypeError("getblockchaininfo missing/invalid block heights")
    if isinstance(progress, bool) or not isinstance(progress, (int, float)):
        raise TypeError("getblockchaininfo missing/invalid verification progress")
    if not isinstance(ibd, bool):
        raise TypeError("getblockchaininfo missing/invalid initialblockdownload")
    return CoreHealth(
        chain=chain,
        blocks=blocks,
        headers=headers,
        verification_progress=float(progress),
        initial_block_download=ibd,
    )


class BitcoinCoreDetector:
    """Probe localhost Bitcoin Core JSON-RPC endpoints (cookie auth).

    One ``httpx.Client`` (with an injectable ``transport`` test seam) is
    created per instance. Probing is bounded by a tight per-request timeout so
    a hung daemon never blocks the app.

    Args:
        settings: Runtime settings (rpc_port, rpc_cookie_path override).
        data_dir: Bitcoin Core data dir to look for per-network cookies
            (defaults to ``~/.bitcoin``). Test seam.
        ports: RPC ports to probe (defaults to :data:`CORE_RPC_PORTS`).
        timeout_s: Per-request timeout in seconds (tight — localhost).
        transport: Optional ``httpx.BaseTransport`` injection point (test
            seam; production callers leave it as ``None``).
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        data_dir: Path | None = None,
        ports: tuple[int, ...] = CORE_RPC_PORTS,
        timeout_s: float = 2.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        settings = settings or Settings.from_env()
        self._settings = settings
        self._data_dir = data_dir if data_dir is not None else Path.home() / ".bitcoin"
        self._ports = ports
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _cookie_for_port(self, port: int) -> str | None:
        """Return the cookie line to use for ``port``, or ``None`` if absent."""
        # An explicit env/configured cookie path overrides the per-network
        # default for the configured port.
        if self._settings.rpc_cookie_path and port == self._settings.rpc_port:
            return _read_cookie(Path(self._settings.rpc_cookie_path))
        rel = _COOKIE_BY_PORT.get(port)
        if rel is None:
            return None
        return _read_cookie(self._data_dir / rel)

    def _probe_port(self, port: int) -> CoreRpcProbe:
        """Probe one RPC port and resolve its state-machine status."""
        cookie = self._cookie_for_port(port)
        url = f"http://{_LOCALHOST}:{port}"
        body = {
            "jsonrpc": "1.0",
            "id": _RPC_ID,
            "method": _CORE_RPC_METHOD,
            "params": [],
        }
        try:
            response = self._client.post(
                url,
                json=body,
                headers=_basic_auth_header(cookie) if cookie else {},
            )
        except httpx.TransportError:
            # Connection refused / timeout / DNS — the clean "no node here" state.
            return CoreRpcProbe(port=port, status=NodeStatus.OFFLINE, cookie_present=cookie is not None)

        if response.status_code in (401, 403):
            return CoreRpcProbe(port=port, status=NodeStatus.AUTH_FAILED, cookie_present=cookie is not None)
        if response.status_code != 200:
            # Reachable but not a Core RPC endpoint (or a bad request).
            return CoreRpcProbe(port=port, status=NodeStatus.MALFORMED, cookie_present=cookie is not None)

        try:
            envelope = response.json()
        except ValueError:
            return CoreRpcProbe(port=port, status=NodeStatus.MALFORMED, cookie_present=cookie is not None)

        if not isinstance(envelope, dict):
            return CoreRpcProbe(port=port, status=NodeStatus.MALFORMED, cookie_present=cookie is not None)
        rpc_error = envelope.get("error")
        if rpc_error is not None:
            # A JSON-RPC error body (e.g. method denied) — present but unusable.
            return CoreRpcProbe(port=port, status=NodeStatus.MALFORMED, cookie_present=cookie is not None)
        try:
            health = _parse_core_health(envelope.get("result"))
        except (TypeError, ValueError):
            return CoreRpcProbe(port=port, status=NodeStatus.MALFORMED, cookie_present=cookie is not None)
        return CoreRpcProbe(
            port=port, status=NodeStatus.REACHABLE, cookie_present=cookie is not None, health=health
        )

    def probe(self) -> tuple[CoreRpcProbe, ...]:
        """Probe all configured RPC ports and return their outcomes."""
        return tuple(self._probe_port(port) for port in self._ports)


def _probe_http_status(client: httpx.Client, url: str) -> NodeStatus:
    """Probe a loopback HTTP endpoint and return a status (never raises)."""
    try:
        response = client.get(url)
    except (httpx.HTTPError, httpx.InvalidURL):
        # Any httpx failure — connection refused/timeout/DNS *or* a malformed
        # URL (``httpx.InvalidURL`` is not an ``HTTPError`` in httpx 0.28) —
        # resolves to the clean "no node here" state. Detection never raises.
        return NodeStatus.OFFLINE
    if 200 <= response.status_code < 300:
        return NodeStatus.REACHABLE
    # Present but not serving what we expect (non-2xx).
    return NodeStatus.MALFORMED


def _loopback_host(url: str) -> str | None:
    """Return the host if ``url`` targets a loopback host, else ``None``.

    Non-loopback hosts and unparseable (malformed) URLs both yield ``None`` —
    a clean "do not probe" signal. Never raises. Value-free.
    """
    try:
        host = httpx.URL(url).host
    except httpx.InvalidURL:
        return None
    if host in _LOOPBACK_HOSTS:
        return host
    return None


def _probe_configured_mempool(client: httpx.Client, url: str) -> NodeStatus:
    """Probe the configured mempool URL only if it targets a loopback host.

    The node/ network exception is loopback-only by contract (ADR-0016):
    a configured URL pointing at a public or LAN host is never contacted, and a
    malformed URL is never probed either — both resolve to the clean
    :attr:`NodeStatus.OFFLINE` state. Remote/self-hosted reach is the
    TCK-P4-002 backend switch, not this module's concern.
    """
    if _loopback_host(url) is None:
        return NodeStatus.OFFLINE
    return _probe_http_status(client, url)


def detect_local_nodes(
    settings: Settings | None = None,
    *,
    data_dir: Path | None = None,
    timeout_s: float = 2.0,
    transport: httpx.BaseTransport | None = None,
) -> LocalNodeReport:
    """Run the full local-node detection pass and return a report.

    Detects, in order: Bitcoin Core RPC (cookie auth, per-port state machine),
    a self-hosted mempool.space API, and an electrs Esplora HTTP endpoint — all
    on localhost only. When ``settings.node_detection_enabled`` is false, the
    pass is skipped and an empty report is returned (nothing is probed).

    Never raises for a missing daemon: every unreachable instance resolves to
    :attr:`NodeStatus.OFFLINE`. ``transport`` is a test seam; production
    callers leave it as ``None``.
    """
    settings = settings or Settings.from_env()
    empty: tuple[CoreRpcProbe, ...] = ()
    if not settings.node_detection_enabled:
        return LocalNodeReport(core=empty, mempool=NodeStatus.OFFLINE, electrs=NodeStatus.OFFLINE)

    with httpx.Client(timeout=timeout_s, transport=transport) as client:
        core_detector = BitcoinCoreDetector(
            settings, data_dir=data_dir, timeout_s=timeout_s, transport=transport
        )
        with core_detector:
            core = core_detector.probe()

        mempool = _probe_configured_mempool(client, settings.local_mempool_url)
        electrs = _probe_http_status(client, f"http://{_LOCALHOST}:{ELECTRS_ESPLORA_HTTP_PORT}")

    return LocalNodeReport(core=core, mempool=mempool, electrs=electrs)

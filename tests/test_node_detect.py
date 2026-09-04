"""Tests for the local node detector (src/localwallet/node/detect.py).

These exercise the detection state machine with mocked transports — no real
socket/network I/O ever happens. Cases: reachable / unreachable / bad-cookie /
malformed-RPC, cookie handling, the node_detection_enabled gate, and the
advise-only/value-free guarantees.
"""

from pathlib import Path

import httpx

from localwallet.config import Settings
from localwallet.node.detect import (
    BitcoinCoreDetector,
    CoreHealth,
    LocalNodeReport,
    NodeStatus,
    detect_local_nodes,
)


def _core_result(chain: str = "testnet4", blocks: int = 100, headers: int = 100,
                 progress: float = 1.0, ibd: bool = False) -> dict:
    return {
        "result": {
            "chain": chain,
            "blocks": blocks,
            "headers": headers,
            "verificationprogress": progress,
            "initialblockdownload": ibd,
        },
        "error": None,
        "id": 1,
    }


def _cookie_dir(tmp_path: Path, *rel_parts: str) -> Path:
    """Write a cookie file and return the data dir containing it."""
    data_dir = tmp_path / "datadir"
    (data_dir / Path(*rel_parts)).parent.mkdir(parents=True, exist_ok=True)
    (data_dir / Path(*rel_parts)).write_text("__cookie__:deadbeefcafe\n", encoding="ascii")
    return data_dir


def _transport_for(core_by_port: dict[int, object] | None = None,
                   fail_ports: set[int] | None = None,
                   auth_fail_ports: set[int] | None = None) -> httpx.MockTransport:
    """Build a mock transport routing RPC posts by URL port.

    ``core_by_port`` maps port → JSON body (200). ``fail_ports`` raise a
    transport error (unreachable). ``auth_fail_ports`` return 401 (bad cookie).
    Any non-listed port raises a transport error.
    """
    core_by_port = core_by_port or {}
    fail_ports = fail_ports or set()
    auth_fail_ports = auth_fail_ports or set()

    def handler(request: httpx.Request) -> httpx.Response:
        port = request.url.port
        if port in fail_ports:
            raise httpx.ConnectError("connection refused", request=request)
        if port in auth_fail_ports:
            return httpx.Response(401, request=request)
        body = core_by_port.get(port)
        if body is None:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json=body, request=request)

    return httpx.MockTransport(handler)


# ------------------------------------------------------------- Core: states


def test_core_reachable_gives_health(tmp_path):
    data_dir = _cookie_dir(tmp_path, "testnet4", ".cookie")
    transport = _transport_for(core_by_port={48332: _core_result(ibd=False)})
    det = BitcoinCoreDetector(data_dir=data_dir, ports=(48332,), transport=transport)
    with det:
        probes = det.probe()
    assert len(probes) == 1
    probe = probes[0]
    assert probe.status is NodeStatus.REACHABLE
    assert probe.cookie_present is True
    assert probe.health is not None
    assert probe.health.chain == "testnet4"
    assert probe.health.is_synced is True
    assert probe.health.sync_percent == 100.0


def test_core_unreachable_is_clean_offline_no_crash(tmp_path):
    data_dir = _cookie_dir(tmp_path, "testnet4", ".cookie")
    transport = _transport_for(fail_ports={48332})
    det = BitcoinCoreDetector(data_dir=data_dir, ports=(48332,), transport=transport)
    with det:
        probes = det.probe()
    assert len(probes) == 1
    assert probes[0].status is NodeStatus.OFFLINE
    assert probes[0].health is None


def test_core_bad_cookie_is_auth_failed(tmp_path):
    data_dir = _cookie_dir(tmp_path, "testnet4", ".cookie")
    transport = _transport_for(auth_fail_ports={48332})
    det = BitcoinCoreDetector(data_dir=data_dir, ports=(48332,), transport=transport)
    with det:
        probes = det.probe()
    assert probes[0].status is NodeStatus.AUTH_FAILED


def test_core_malformed_json_is_malformed(tmp_path):
    data_dir = _cookie_dir(tmp_path, "testnet4", ".cookie")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json", request=request)

    det = BitcoinCoreDetector(data_dir=data_dir, ports=(48332,), transport=httpx.MockTransport(handler))
    with det:
        probes = det.probe()
    assert probes[0].status is NodeStatus.MALFORMED


def test_core_malformed_shape_is_malformed(tmp_path):
    data_dir = _cookie_dir(tmp_path, "testnet4", ".cookie")
    transport = _transport_for(core_by_port={48332: {"result": {"chain": 5}, "error": None}})
    det = BitcoinCoreDetector(data_dir=data_dir, ports=(48332,), transport=transport)
    with det:
        probes = det.probe()
    assert probes[0].status is NodeStatus.MALFORMED


def test_core_rpc_error_body_is_malformed(tmp_path):
    data_dir = _cookie_dir(tmp_path, "testnet4", ".cookie")
    transport = _transport_for(core_by_port={48332: {"result": None, "error": {"code": -1, "message": "x"}}})
    det = BitcoinCoreDetector(data_dir=data_dir, ports=(48332,), transport=transport)
    with det:
        probes = det.probe()
    assert probes[0].status is NodeStatus.MALFORMED


def test_core_syncing_reports_progress(tmp_path):
    data_dir = _cookie_dir(tmp_path, "testnet4", ".cookie")
    transport = _transport_for(core_by_port={48332: _core_result(blocks=50, headers=100, progress=0.5, ibd=True)})
    det = BitcoinCoreDetector(data_dir=data_dir, ports=(48332,), transport=transport)
    with det:
        probes = det.probe()
    probe = probes[0]
    assert probe.status is NodeStatus.REACHABLE
    assert probe.health is not None
    assert probe.health.is_synced is False
    assert probe.health.sync_percent == 50.0
    assert probe.health.headers_progress_percent == 50.0


# ---------------------------------------------------------------- Cookie


def test_missing_cookie_sets_cookie_present_false(tmp_path):
    data_dir = tmp_path / "empty-datadir"  # no cookie file
    data_dir.mkdir()
    transport = _transport_for(core_by_port={48332: _core_result()})
    det = BitcoinCoreDetector(data_dir=data_dir, ports=(48332,), transport=transport)
    with det:
        probes = det.probe()
    assert probes[0].cookie_present is False


def test_explicit_cookie_path_overrides_per_network(tmp_path):
    custom = tmp_path / "custom-cookie"
    custom.write_text("__cookie__:abc\n", encoding="ascii")
    settings = Settings(rpc_port=48332, rpc_cookie_path=str(custom))
    transport = _transport_for(core_by_port={48332: _core_result()})
    det = BitcoinCoreDetector(settings, data_dir=tmp_path, ports=(48332,), transport=transport)
    with det:
        probes = det.probe()
    assert probes[0].status is NodeStatus.REACHABLE


def test_cookie_secret_never_in_report(tmp_path):
    data_dir = _cookie_dir(tmp_path, "testnet4", ".cookie")
    transport = _transport_for(core_by_port={48332: _core_result()})
    det = BitcoinCoreDetector(data_dir=data_dir, ports=(48332,), transport=transport)
    with det:
        probes = det.probe()
    # The cookie CONTENT must never appear anywhere in probe data.
    assert "__cookie__:deadbeefcafe" not in repr(probes)


# ------------------------------------------------------------- Full report


def test_detect_local_nodes_report_and_reachable_kinds(tmp_path):
    data_dir = _cookie_dir(tmp_path, "testnet4", ".cookie")
    settings = Settings(local_mempool_url="http://127.0.0.1:3006")

    # Use one transport covering both RPC and HTTP probes by URL port.
    def handler(request: httpx.Request) -> httpx.Response:
        port = request.url.port
        if port == 48332:
            return httpx.Response(200, json=_core_result(), request=request)
        if port in (3006, 3002):
            return httpx.Response(200, request=request)
        raise httpx.ConnectError("refused", request=request)

    report = detect_local_nodes(settings, data_dir=data_dir, transport=httpx.MockTransport(handler))
    assert isinstance(report, LocalNodeReport)
    assert report.any_reachable is True
    assert NodeStatus.REACHABLE in {p.status for p in report.core}
    assert report.mempool is NodeStatus.REACHABLE
    assert report.electrs is NodeStatus.REACHABLE
    kinds = report.reachable_kinds
    assert "bitcoin_core" in kinds
    assert "mempool" in kinds
    assert "electrs" in kinds


def test_detect_local_nodes_offline_when_everything_unreachable(tmp_path):
    data_dir = _cookie_dir(tmp_path, "testnet4", ".cookie")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    report = detect_local_nodes(data_dir=data_dir, transport=httpx.MockTransport(handler))
    assert report.any_reachable is False
    assert report.mempool is NodeStatus.OFFLINE
    assert report.electrs is NodeStatus.OFFLINE
    assert all(p.status is NodeStatus.OFFLINE for p in report.core)


def test_detect_local_nodes_skipped_when_disabled(tmp_path):
    data_dir = _cookie_dir(tmp_path, "testnet4", ".cookie")
    settings = Settings(node_detection_enabled=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("detection must not probe when disabled")

    report = detect_local_nodes(settings, data_dir=data_dir, transport=httpx.MockTransport(handler))
    assert report.core == ()
    assert report.mempool is NodeStatus.OFFLINE
    assert report.any_reachable is False


def test_detect_local_nodes_never_raises_for_missing_daemon(tmp_path):
    # Any collection of failures must resolve to a report, not an exception.
    data_dir = _cookie_dir(tmp_path, "testnet4", ".cookie")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    report = detect_local_nodes(data_dir=data_dir, transport=httpx.MockTransport(handler))
    assert isinstance(report, LocalNodeReport)


def test_malformed_configured_mempool_url_is_clean_state_not_crash(tmp_path):
    """FIX-1: a malformed LOCALWALLET_LOCAL_MEMPOOL_URL must never crash.

    ``httpx.InvalidURL`` is not a ``TransportError`` (nor an ``HTTPError`` in
    httpx 0.28), so it previously escaped the probe's except clause. It must now
    resolve to a clean state, and nothing may be probed for a bad URL.
    """
    data_dir = _cookie_dir(tmp_path, "testnet4", ".cookie")
    settings = Settings(local_mempool_url="not-a-url")

    def handler(request: httpx.Request) -> httpx.Response:
        # Core/electrs loopback probes are expected; the malformed mempool URL
        # must never reach the wire.
        if request.url.host != "127.0.0.1":
            raise AssertionError("malformed URL must not be probed")
        port = request.url.port
        if port == 48332:
            return httpx.Response(200, json=_core_result(), request=request)
        if port == 3002:
            return httpx.Response(200, request=request)
        raise httpx.ConnectError("refused", request=request)

    report = detect_local_nodes(settings, data_dir=data_dir, transport=httpx.MockTransport(handler))
    # Clean report, never an exception; malformed URL lands in OFFLINE.
    assert isinstance(report, LocalNodeReport)
    assert report.mempool is NodeStatus.OFFLINE


# ------------------------------------------------------------ Value-free


def test_core_health_never_carries_secrets(tmp_path):
    data_dir = _cookie_dir(tmp_path, "testnet4", ".cookie")
    transport = _transport_for(core_by_port={48332: _core_result()})
    det = BitcoinCoreDetector(data_dir=data_dir, ports=(48332,), transport=transport)
    with det:
        probes = det.probe()
    health = probes[0].health
    assert isinstance(health, CoreHealth)
    # Health carries only sync metadata — no wallet data.
    assert health.chain == "testnet4"
    assert isinstance(health.blocks, int)


def test_detect_uses_only_loopback_hosts(tmp_path):
    """Detection never targets a non-loopback host (privacy invariant)."""
    data_dir = _cookie_dir(tmp_path, "testnet4", ".cookie")
    seen_hosts: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.add(request.url.host)
        port = request.url.port
        if port == 48332:
            return httpx.Response(200, json=_core_result(), request=request)
        if port in (3006, 3002):
            return httpx.Response(200, request=request)
        raise httpx.ConnectError("refused", request=request)

    detect_local_nodes(data_dir=data_dir, transport=httpx.MockTransport(handler))
    assert seen_hosts, "expected at least one probe"
    assert seen_hosts <= {"127.0.0.1", "localhost"}


def test_detect_never_probes_public_mempool_url(tmp_path):
    """NOTE-3: node/ is loopback-only; a public/LAN mempool URL is not probed.

    A user-set LOCALWALLET_LOCAL_MEMPOOL_URL pointing at a public host must
    never be contacted by this lint-exempt module. It resolves to the clean
    OFFLINE state instead; the transport proves zero requests to that host.
    """
    data_dir = _cookie_dir(tmp_path, "testnet4", ".cookie")
    settings = Settings(local_mempool_url="http://example.com:3006")

    def handler(request: httpx.Request) -> httpx.Response:
        # Any probe to a non-loopback host is a contract violation.
        if request.url.host != "127.0.0.1":
            raise AssertionError(f"must not probe non-loopback host: {request.url.host}")
        port = request.url.port
        if port == 48332:
            return httpx.Response(200, json=_core_result(), request=request)
        if port == 3002:
            return httpx.Response(200, request=request)
        raise httpx.ConnectError("refused", request=request)

    report = detect_local_nodes(settings, data_dir=data_dir, transport=httpx.MockTransport(handler))
    assert report.mempool is NodeStatus.OFFLINE
    assert report.electrs is NodeStatus.REACHABLE
    assert any(p.status is NodeStatus.REACHABLE for p in report.core)

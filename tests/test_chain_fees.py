"""Tests for the fee estimator (floor-follower over mempool data, ADR-0011
amendment; recommended-fees fallback).

All HTTP traffic is served by ``httpx.MockTransport`` — no real network.

``ScriptedServer`` answers EVERY path with its scripted entry, so over the
floor-follower endpoints a recommended-shaped payload is malformed (not a
list) and the refresh fails closed to the recommended mapping — the values
these tests assert are the fallback values, fetched as
``recommended + mempool-blocks`` (the floor attempt aborts at the first
bad shape; the floor path itself is exercised by ``RoutedServer`` below).
"""

import sys
from pathlib import Path

import httpx
import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.chain import ChainError, EsploraClient, FeeEstimator, FeeSource, FeeTarget
from localwallet.chain import esplora as esplora_module
from localwallet.chain import fees as fees_module

BASE_URL = "https://mempool.space/testnet4/api"
KIND = "fees-recommended"

RECOMMENDED = {
    "fastestFee": 30,
    "halfHourFee": 25,
    "hourFee": 18,
    "economyFee": 10,
    "minimumFee": 1,
}


class ScriptedServer:
    def __init__(self, *entries: httpx.Response | Exception) -> None:
        self._entries = list(entries)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self._entries) - 1)
        entry = self._entries[index]
        if isinstance(entry, Exception):
            raise entry
        return entry

    def client(self, *, max_retries: int = 2) -> EsploraClient:
        return EsploraClient(
            base_url=BASE_URL,
            timeout_s=5.0,
            max_retries=max_retries,
            transport=httpx.MockTransport(self.handler),
        )


def _record_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(esplora_module, "_sleep_for", sleeps.append)
    return sleeps


def _estimator(server: ScriptedServer, *, ttl_s: float = 30.0, max_retries: int = 2):
    return FeeEstimator(server.client(max_retries=max_retries), ttl_s=ttl_s)


def _freeze_clock(monkeypatch: pytest.MonkeyPatch, start: float = 1_000_000.0):
    """Return a setter controlling ``fees._now`` (module clock)."""
    clock = {"now": start}

    def set_time(t: float) -> None:
        clock["now"] = t

    monkeypatch.setattr(fees_module, "_now", lambda: clock["now"])
    return set_time


# -- happy path ------------------------------------------------------------


def test_estimate_maps_targets_to_fields():
    server = ScriptedServer(httpx.Response(200, json=RECOMMENDED))
    with server.client() as client:
        estimator = FeeEstimator(client, ttl_s=30.0)
        fast = estimator.estimate(FeeTarget.FAST)
        medium = estimator.estimate(FeeTarget.MEDIUM)
        slow = estimator.estimate(FeeTarget.SLOW)
    assert fast.sat_per_vb == 30
    assert medium.sat_per_vb == 25
    assert slow.sat_per_vb == 18
    # Floor endpoints malformed (dict, not list) -> fail-closed fallback.
    assert (fast.source, medium.source, slow.source) == (FeeSource.RECOMMENDED,) * 3
    assert (fast.target, medium.target, slow.target) == (
        FeeTarget.FAST,
        FeeTarget.MEDIUM,
        FeeTarget.SLOW,
    )
    assert len(server.requests) == 2  # recommended ok, floor attempt aborts at first shape
    request = server.requests[0]
    assert request.method == "GET"
    assert request.url.path == "/testnet4/api/v1/fees/recommended"


def test_minimum_fee_sat_vb():
    server = ScriptedServer(httpx.Response(200, json=RECOMMENDED))
    with server.client() as client:
        estimator = FeeEstimator(client, ttl_s=30.0)
        assert estimator.minimum_fee_sat_vb() == 1
    assert len(server.requests) == 2


def test_estimate_source_timestamp_reflects_fetch_time(monkeypatch: pytest.MonkeyPatch):
    set_time = _freeze_clock(monkeypatch, start=5_000.0)
    server = ScriptedServer(httpx.Response(200, json=RECOMMENDED))
    with server.client() as client:
        estimator = FeeEstimator(client, ttl_s=30.0)
        est = estimator.estimate(FeeTarget.FAST)
        assert est.source_timestamp == 5_000.0
        set_time(5_000.0 + 5.0)
        # still within TTL -> served from cache, source timestamp unchanged
        est2 = estimator.estimate(FeeTarget.FAST)
        assert est2.source_timestamp == 5_000.0
    assert len(server.requests) == 2


# -- caching / TTL ---------------------------------------------------------


def test_cache_serves_within_ttl_without_refetch(monkeypatch: pytest.MonkeyPatch):
    set_time = _freeze_clock(monkeypatch)
    server = ScriptedServer(httpx.Response(200, json=RECOMMENDED))
    with server.client() as client:
        estimator = FeeEstimator(client, ttl_s=30.0)
        estimator.estimate(FeeTarget.FAST)
        set_time(1_000_000.0 + 20.0)  # < 30s TTL
        estimator.estimate(FeeTarget.MEDIUM)
    assert len(server.requests) == 2  # one combined refresh; no refetch while fresh


def test_refetches_after_ttl(monkeypatch: pytest.MonkeyPatch):
    set_time = _freeze_clock(monkeypatch)
    server = ScriptedServer(httpx.Response(200, json=RECOMMENDED))
    with server.client() as client:
        estimator = FeeEstimator(client, ttl_s=30.0)
        estimator.estimate(FeeTarget.FAST)
        set_time(1_000_000.0 + 31.0)  # past TTL
        estimator.estimate(FeeTarget.FAST)
    assert len(server.requests) == 4  # two combined refreshes


def test_invalidate_forces_refetch(monkeypatch: pytest.MonkeyPatch):
    _freeze_clock(monkeypatch)
    server = ScriptedServer(httpx.Response(200, json=RECOMMENDED))
    with server.client() as client:
        estimator = FeeEstimator(client, ttl_s=30.0)
        estimator.estimate(FeeTarget.FAST)
        estimator.invalidate()
        estimator.estimate(FeeTarget.FAST)
    assert len(server.requests) == 4


# -- fail-closed shape validation -----------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [RECOMMENDED],  # list, not object
        "junk",
        42,
        {k: v for k, v in RECOMMENDED.items() if k != "fastestFee"},  # missing key
        {**RECOMMENDED, "fastestFee": "30"},  # string
        {**RECOMMENDED, "fastestFee": True},  # bool (int subclass)
        {**RECOMMENDED, "fastestFee": -1},  # negative
        {**RECOMMENDED, "fastestFee": 30.5},  # float
    ],
)
def test_malformed_recommended_raises_chain_error(payload):
    server = ScriptedServer(httpx.Response(200, json=payload))
    with server.client() as client, pytest.raises(ChainError):
        FeeEstimator(client, ttl_s=30.0).estimate(FeeTarget.FAST)
    assert len(server.requests) == 1  # shape errors are not retried


def test_missing_minimum_fee_fails_closed():
    payload = {**RECOMMENDED, "minimumFee": None}
    server = ScriptedServer(httpx.Response(200, json=payload))
    with server.client() as client, pytest.raises(ChainError):
        FeeEstimator(client, ttl_s=30.0).minimum_fee_sat_vb()
    assert len(server.requests) == 1


@pytest.mark.parametrize("zero_key", ["fastestFee", "halfHourFee", "hourFee", "economyFee", "minimumFee"])
def test_zero_fee_payload_rejected(zero_key):
    # Zero-fee policy (ADR-0011): a 0 sat/vB estimate is a broken payload,
    # not a cheap one — the tx engine floors via min-relay separately.
    payload = {**RECOMMENDED, zero_key: 0}
    server = ScriptedServer(httpx.Response(200, json=payload))
    with server.client() as client, pytest.raises(ChainError):
        FeeEstimator(client, ttl_s=30.0).estimate(FeeTarget.FAST)
    assert len(server.requests) == 1  # shape errors are not retried


def test_zero_minimum_fee_rejected_on_direct_access():
    payload = {**RECOMMENDED, "minimumFee": 0}
    server = ScriptedServer(httpx.Response(200, json=payload))
    with server.client() as client, pytest.raises(ChainError):
        FeeEstimator(client, ttl_s=30.0).minimum_fee_sat_vb()
    assert len(server.requests) == 1


def test_zero_fee_error_message_is_value_free():
    payload = {**RECOMMENDED, "fastestFee": 0}
    server = ScriptedServer(httpx.Response(200, json=payload))
    with server.client() as client, pytest.raises(ChainError) as excinfo:
        FeeEstimator(client, ttl_s=30.0).estimate(FeeTarget.FAST)
    message = str(excinfo.value)
    assert "fastestFee" in message  # structural field name allowed
    assert " 0" not in message  # no fee value leaked


def test_slow_does_not_fall_back_to_economy_fee():
    # SLOW must map to hourFee strictly; a missing hourFee fails closed even
    # though economyFee is present.
    payload = {**RECOMMENDED, "hourFee": None}
    server = ScriptedServer(httpx.Response(200, json=payload))
    with server.client() as client, pytest.raises(ChainError):
        FeeEstimator(client, ttl_s=30.0).estimate(FeeTarget.SLOW)
    assert len(server.requests) == 1


def test_value_not_echoed_in_error_message():
    server = ScriptedServer(httpx.Response(200, json={**RECOMMENDED, "fastestFee": None}))
    with server.client() as client, pytest.raises(ChainError) as excinfo:
        FeeEstimator(client, ttl_s=30.0).estimate(FeeTarget.FAST)
    message = str(excinfo.value)
    assert "fastestFee" in message  # field name is structural, allowed
    assert "30" not in message  # no fee value leaked
    assert KIND in message


# -- retry policy reuse ----------------------------------------------------


def test_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch):
    sleeps = _record_sleeps(monkeypatch)
    server = ScriptedServer(httpx.Response(429), httpx.Response(200, json=RECOMMENDED))
    with server.client(max_retries=2) as client:
        estimator = FeeEstimator(client, ttl_s=30.0)
        assert estimator.estimate(FeeTarget.FAST).sat_per_vb == 30
    assert len(server.requests) == 3  # 429, recommended ok, floor attempt aborts
    assert len(sleeps) == 1


def test_retry_exhaustion_on_429_raises_chain_error(monkeypatch: pytest.MonkeyPatch):
    sleeps = _record_sleeps(monkeypatch)
    server = ScriptedServer(httpx.Response(429))
    with server.client(max_retries=2) as client, pytest.raises(ChainError) as excinfo:
        FeeEstimator(client, ttl_s=30.0).estimate(FeeTarget.FAST)
    message = str(excinfo.value)
    assert "429" in message
    assert "after 2 retries" in message
    assert len(server.requests) == 3
    assert len(sleeps) == 2


# -- constructor validation ------------------------------------------------


def test_invalid_ttl_raises_value_error():
    server = ScriptedServer(httpx.Response(200, json=RECOMMENDED))
    for bad in (0, -1, True, "x"):
        with pytest.raises(ValueError):
            FeeEstimator(server.client(), ttl_s=bad)  # type: ignore[arg-type]
    assert server.requests == []  # no requests fired


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_ttl_raises_value_error(bad):
    # A NaN TTL would disable expiry entirely (comparisons are always
    # False) and an inf TTL would never expire — both fail closed (A4).
    server = ScriptedServer(httpx.Response(200, json=RECOMMENDED))
    with pytest.raises(ValueError):
        FeeEstimator(server.client(), ttl_s=bad)
    assert server.requests == []


def test_estimate_rejects_non_fee_target():
    server = ScriptedServer(httpx.Response(200, json=RECOMMENDED))
    with server.client() as client, pytest.raises(TypeError):
        FeeEstimator(client, ttl_s=30.0).estimate("fast")  # type: ignore[arg-type]
    assert server.requests == []


# -- env parsing -----------------------------------------------------------


def test_fee_cache_ttl_from_env(monkeypatch: pytest.MonkeyPatch):
    from localwallet.config import Settings

    monkeypatch.setenv("LOCALWALLET_FEE_CACHE_TTL_S", "45.5")
    assert Settings.from_env().fee_cache_ttl_s == 45.5


def test_fee_cache_ttl_default():
    from localwallet.config import Settings

    assert Settings.from_env().fee_cache_ttl_s == 30.0


# -- floor-follower (TCK-FEE-001, ADR-0011 amendment) -----------------------

TIP = 900_000

# User's 2026-09-07 live case: last-5 bottoms 0.5/0.34/0.37/0.36/0.4, next
# projection bottom 0.3, and (observed) a next block whose projection
# OVERSHADOWS the deep tail. feeRange bottoms are ascending quantiles; [0]
# is the lowest.
LIVE_RECENT = [0.5, 0.34, 0.37, 0.36, 0.4, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0]
LIVE_PROJECTED = [0.3, 0.28, 0.26, 0.24, 0.22, 0.2, 0.18, 0.16]


def _projected_payload(bottoms: list) -> list[dict]:
    return [
        {"blockSize": 998_000, "medianFee": b * 2, "feeRange": [b, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]}
        for b in bottoms
    ]


def _recent_payload(bottoms: list) -> list[dict]:
    return [
        {
            "height": TIP - i,
            "timestamp": 1_700_000_000 - i * 600,
            "extras": {"medianFee": 1.0, "feeRange": [b, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]},
        }
        for i, b in enumerate(bottoms)
    ]


def _floor_routes(
    *,
    recommended: dict | None = None,
    projected: list | None = None,
    recent: list | None = None,
) -> dict:
    """Complete route set for one healthy combined refresh (overridable).

    ``projected``/``recent`` are whole RESPONSE PAYLOADS (already-shaped,
    including the malformed-shape fixtures); the defaults are the healthy
    live-case payloads.
    """
    return {
        "/v1/fees/recommended": httpx.Response(
            200, json=RECOMMENDED if recommended is None else recommended
        ),
        "/v1/fees/mempool-blocks": httpx.Response(
            200, json=_projected_payload(LIVE_PROJECTED) if projected is None else projected
        ),
        "/blocks/tip": httpx.Response(200, json=TIP),
        "/v1/blocks/": httpx.Response(
            200, json=_recent_payload(LIVE_RECENT) if recent is None else recent
        ),
    }


class RoutedServer:
    """Per-path routing fake (a combined refresh touches four endpoints).

    Route keys are path MARKERS (matched by substring, so the mid-path
    ``/v1/blocks/`` prefix of ``/v1/blocks/{tip}`` works). Unmatched paths
    answer 404 — the client fails immediately (4xx is not retried) so the
    estimator's fail-closed fallback exercises naturally.
    """

    def __init__(self, routes: dict[str, httpx.Response | Exception]) -> None:
        self._routes = routes
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for marker, entry in self._routes.items():
            if marker in request.url.path:
                if isinstance(entry, Exception):
                    raise entry
                return entry
        return httpx.Response(404, json=None)

    def client(self, *, max_retries: int = 0) -> EsploraClient:
        return EsploraClient(
            base_url=BASE_URL,
            timeout_s=5.0,
            max_retries=max_retries,
            transport=httpx.MockTransport(self.handler),
        )


def _estimates(server: RoutedServer) -> dict[FeeTarget, int]:
    with server.client() as client:
        estimator = FeeEstimator(client, ttl_s=30.0)
        return {
            target: estimator.estimate(target).sat_per_vb for target in FeeTarget
        }


def test_user_observed_case_bids_exactly_1_sat_vb():
    # The motivating overpay: fastestFee said 2 sat/vB while the last 5
    # blocks confirmed down to 0.34 and the next-block projection bottomed
    # at 0.3 — the bid must NOT exceed 1 sat/vB.
    server = RoutedServer(_floor_routes(recommended={**RECOMMENDED, "fastestFee": 2}))
    with server.client() as client:
        est = FeeEstimator(client, ttl_s=30.0).estimate(FeeTarget.FAST)
    assert est.sat_per_vb == 1
    assert est.source is FeeSource.FLOOR_FOLLOWER
    assert isinstance(est.sat_per_vb, int) and not isinstance(est.sat_per_vb, bool)


def test_congested_next_block_bottom_lifts_fast():
    # Floor-follower, not a cap: an 8 sat/vB next-block bottom bids 8.
    for bottom, expected in ((8.0, 8), (8.4, 9), (12.5, 13)):
        server = RoutedServer(
            _floor_routes(projected=_projected_payload([bottom] + [1.0] * 7))
        )
        with server.client() as client:
            est = FeeEstimator(client, ttl_s=30.0).estimate(FeeTarget.FAST)
        assert est.sat_per_vb == expected
        assert est.source is FeeSource.FLOOR_FOLLOWER


def test_medium_slow_use_deeper_projected_blocks():
    # FAST=blocks[0], MEDIUM=blocks[2], SLOW=blocks[6] bottoms (ceil), all
    # floored by the recent-blocks minimum (0.34 here — under every term).
    bottoms = [10.0, 9.0, 6.5, 5.0, 4.0, 3.0, 2.2, 1.0]
    server = RoutedServer(
        _floor_routes(
            projected=_projected_payload(bottoms),
            recent=_recent_payload([0.3] * 15),
        )
    )
    rates = _estimates(server)
    assert rates[FeeTarget.FAST] == 10
    assert rates[FeeTarget.MEDIUM] == 7  # ceil(6.5)
    assert rates[FeeTarget.SLOW] == 3  # ceil(2.2)
    assert rates[FeeTarget.FAST] >= rates[FeeTarget.MEDIUM] >= rates[FeeTarget.SLOW]


def test_recent_blocks_floor_lifts_quiet_projection():
    # Transiently-empty projection (0.2 bottoms) but the last 5 blocks
    # confirmed at >= 3.0 — the observed-blocks floor must stop the 1 sat
    # underbid that would miss a full block.
    server = RoutedServer(
        _floor_routes(
            projected=_projected_payload([0.2] * 8),
            recent=_recent_payload([3.0, 4.0, 5.0, 6.0, 7.0] + [8.0] * 10),
        )
    )
    rates = _estimates(server)
    assert rates[FeeTarget.FAST] == 3
    assert rates[FeeTarget.MEDIUM] == 3  # ceil(max(0.2, 3.0))
    assert rates[FeeTarget.SLOW] == 3


def test_minimum_fee_lifts_fast_only():
    # minimumFee means "min to get into the NEXT block": it floors FAST but
    # must not drag MEDIUM/SLOW up with it.
    server = RoutedServer(
        _floor_routes(
            recommended={**RECOMMENDED, "minimumFee": 5},
            projected=_projected_payload([1.0, 0.9, 0.5, 0.45, 0.42, 0.41, 0.4, 0.3]),
            recent=_recent_payload([0.3] * 15),
        )
    )
    with server.client() as client:
        estimator = FeeEstimator(client, ttl_s=30.0)
        rates = {target: estimator.estimate(target).sat_per_vb for target in FeeTarget}
        assert estimator.minimum_fee_sat_vb() == 5  # accessor still serves the payload
    assert rates[FeeTarget.FAST] == 5
    assert rates[FeeTarget.MEDIUM] == 1  # ceil(max(0.5, 0.3)) — not floored by minimumFee
    assert rates[FeeTarget.SLOW] == 1


def test_shallow_projection_clamps_to_deepest_block():
    # Fewer projected blocks than the SLOW index: clamp, never IndexError.
    server = RoutedServer(
        _floor_routes(
            projected=_projected_payload([4.5, 3.2, 2.7]),
            recent=_recent_payload([0.1] * 15),
        )
    )
    rates = _estimates(server)
    assert rates[FeeTarget.FAST] == 5  # ceil(4.5)
    assert rates[FeeTarget.MEDIUM] == 3  # ceil(2.7), blocks[2]
    assert rates[FeeTarget.SLOW] == 3  # blocks[6] clamped to blocks[2]


def _raw_json(text: str) -> httpx.Response:
    """A 200 response with a raw JSON body (stdlib json parses the bare
    NaN/Infinity tokens that strict encoders — and httpx's json= — refuse)."""
    return httpx.Response(
        200, content=text.encode("ascii"), headers={"content-type": "application/json"}
    )


@pytest.mark.parametrize(
    ("suffix", "entry"),
    [
        # projected-block payloads (fail closed to the recommended mapping)
        ("/v1/fees/mempool-blocks", httpx.Response(200, json={"junk": 1})),
        ("/v1/fees/mempool-blocks", httpx.Response(200, json=[])),
        ("/v1/fees/mempool-blocks", httpx.Response(200, json=["not-an-object"])),
        ("/v1/fees/mempool-blocks", httpx.Response(200, json=[{"blockSize": 1}])),
        ("/v1/fees/mempool-blocks", httpx.Response(200, json=[{"feeRange": []}])),
        ("/v1/fees/mempool-blocks", httpx.Response(200, json=[{"feeRange": [True, 2.0]}])),
        ("/v1/fees/mempool-blocks", httpx.Response(200, json=[{"feeRange": [0, 2.0]}])),
        ("/v1/fees/mempool-blocks", httpx.Response(200, json=[{"feeRange": [-777.5, 2.0]}])),
        ("/v1/fees/mempool-blocks", httpx.Response(200, json=[{"feeRange": ["777", 2.0]}])),
        ("/v1/fees/mempool-blocks", _raw_json('[{"feeRange": [NaN, 2.0]}]')),
        ("/v1/fees/mempool-blocks", _raw_json('[{"feeRange": [Infinity, 2.0]}]')),
        ("/v1/fees/mempool-blocks", httpx.Response(200, json=[{"feeRange": [10**400, 2.0]}])),
        # security-review MEDIUM: parser-accepted non-monotonic bottoms that
        # would break FAST >= MEDIUM >= SLOW must fail closed (both counter-
        # examples from the review: FAST<MEDIUM via [0.2, 5.0, 5.0] with
        # minimumFee, and MEDIUM<SLOW via the 8.0 tail breaking out of the
        # SLOW clamp).
        ("/v1/fees/mempool-blocks", httpx.Response(200, json=_projected_payload([0.2, 5.0, 5.0]))),
        (
            "/v1/fees/mempool-blocks",
            httpx.Response(200, json=_projected_payload([3.0, 3.0, 1.0, 1.0, 1.0, 1.0, 8.0])),
        ),
        # confirmed-block payloads
        ("/v1/blocks/", httpx.Response(200, json={"junk": 1})),
        ("/v1/blocks/", httpx.Response(200, json=_recent_payload([1.0] * 4))),  # < 5-block window
        ("/v1/blocks/", httpx.Response(200, json=[{"height": TIP}] * 5)),  # missing 'extras'
        ("/v1/blocks/", httpx.Response(200, json=[{"extras": "nope"}] * 5)),
        ("/v1/blocks/", httpx.Response(200, json=[{"extras": {}}] * 5)),  # empty extras
        ("/v1/blocks/", httpx.Response(200, json=[{"extras": {"feeRange": [0, 2.0]}}] * 5)),
        ("/v1/blocks/", httpx.Response(200, json=[{"extras": {"feeRange": [True, 2.0]}}] * 5)),
        ("/v1/blocks/", httpx.Response(200, json=[{"extras": {"feeRange": [-777.5]}}] * 5)),
        (
            "/v1/blocks/",
            _raw_json(
                '[{"extras": {"feeRange": [1.0]}}, {"extras": {"feeRange": [1.0]}},'
                ' {"extras": {"feeRange": [1.0]}}, {"extras": {"feeRange": [1.0]}},'
                ' {"extras": {"feeRange": [NaN]}}]'
            ),
        ),
    ],
)
def test_malformed_floor_payload_falls_back_to_recommended(suffix, entry):
    routes = _floor_routes()
    routes[suffix] = entry
    server = RoutedServer(routes)
    with server.client() as client:
        estimator = FeeEstimator(client, ttl_s=30.0)
        rates = {target: estimator.estimate(target) for target in FeeTarget}
    # The old mapping verbatim — and honest about which path produced it.
    assert {t: e.sat_per_vb for t, e in rates.items()} == {
        FeeTarget.FAST: 30,
        FeeTarget.MEDIUM: 25,
        FeeTarget.SLOW: 18,
    }
    assert all(e.source is FeeSource.RECOMMENDED for e in rates.values())


@pytest.mark.parametrize(
    "suffix", ["/v1/fees/mempool-blocks", "/blocks/tip", "/v1/blocks/"]
)
def test_floor_endpoint_failure_falls_back_to_recommended(suffix):
    # Transport failure on ANY floor endpoint (the tip is the recent-blocks
    # prerequisite) degrades to the recommended mapping; no exception.
    routes = _floor_routes()
    routes[suffix] = httpx.ConnectError("boom")
    server = RoutedServer(routes)
    with server.client() as client:
        est = FeeEstimator(client, ttl_s=30.0).estimate(FeeTarget.FAST)
    assert est.sat_per_vb == 30
    assert est.source is FeeSource.RECOMMENDED


def test_floor_endpoint_404_falls_back(monkeypatch: pytest.MonkeyPatch):
    # Missing endpoint (self-hosted backends without the mempool.space
    # extensions) fails closed the same way.
    _freeze_clock(monkeypatch)
    routes = _floor_routes()
    del routes["/v1/fees/mempool-blocks"]  # unrouted -> 404
    server = RoutedServer(routes)
    with server.client() as client:
        est = FeeEstimator(client, ttl_s=30.0).estimate(FeeTarget.FAST)
    assert est.sat_per_vb == 30
    assert est.source is FeeSource.RECOMMENDED


def test_combined_refresh_fetches_each_endpoint_once(monkeypatch: pytest.MonkeyPatch):
    set_time = _freeze_clock(monkeypatch)
    server = RoutedServer(_floor_routes())
    with server.client() as client:
        estimator = FeeEstimator(client, ttl_s=30.0)
        estimator.estimate(FeeTarget.FAST)
        estimator.estimate(FeeTarget.MEDIUM)
        estimator.minimum_fee_sat_vb()
        paths = [r.url.path for r in server.requests]
        assert len(paths) == 4  # one GET per endpoint, regardless of target count
        assert [p.rsplit("/api", 1)[-1] for p in paths] == [
            "/v1/fees/recommended",
            "/v1/fees/mempool-blocks",
            "/blocks/tip",
            f"/v1/blocks/{TIP}",
        ]
        set_time(1_000_000.0 + 31.0)  # past TTL
        estimator.estimate(FeeTarget.SLOW)
    assert len(server.requests) == 8  # one combined re-refresh


def test_fallback_result_is_cached_like_any_estimate(monkeypatch: pytest.MonkeyPatch):
    # A degraded refresh is still a refresh: TTL pressure stays bounded.
    set_time = _freeze_clock(monkeypatch)
    routes = _floor_routes()
    routes["/v1/fees/mempool-blocks"] = httpx.Response(200, json=[])
    server = RoutedServer(routes)
    with server.client() as client:
        estimator = FeeEstimator(client, ttl_s=30.0)
        estimator.estimate(FeeTarget.FAST)
        estimator.estimate(FeeTarget.SLOW)
        assert len(server.requests) == 2  # no endpoint hammering
        set_time(1_000_000.0 + 31.0)
        estimator.estimate(FeeTarget.FAST)
    assert len(server.requests) == 4  # the full refresh retried after TTL


def test_non_monotonic_projected_bottoms_rejected_by_parser():
    # Security-review MEDIUM: FAST >= MEDIUM >= SLOW must be a parser
    # invariant, not a data-source assumption — non-increasing-with-depth is
    # enforced here; the end-to-end fallback for both review counterexamples
    # is pinned in test_malformed_floor_payload_falls_back_to_recommended.
    with pytest.raises(ChainError) as excinfo:
        fees_module._parse_projected_bottoms(_projected_payload([0.2, 5.0, 5.0]), "k")
    message = str(excinfo.value)
    assert "entry 1" in message  # structural index allowed
    assert "0.2" not in message and "5.0" not in message  # no fee value leaked
    with pytest.raises(ChainError):
        fees_module._parse_projected_bottoms(
            _projected_payload([3.0, 3.0, 1.0, 1.0, 1.0, 1.0, 8.0]), "k"
        )
    # Ties are non-increasing (accepted), and a plain descending list passes.
    assert fees_module._parse_projected_bottoms(
        _projected_payload([2.0, 2.0, 1.0]), "k"
    ) == [2.0, 2.0, 1.0]


def test_floor_error_messages_are_value_free():
    # The fallback swallows these errors, but the messages stay scrubbed
    # (defense in depth: never echo a fee value).
    with pytest.raises(ChainError) as excinfo:
        fees_module._parse_projected_bottoms([{"feeRange": [-777.5]}], "k")
    assert "777" not in str(excinfo.value)
    with pytest.raises(ChainError) as excinfo:
        fees_module._parse_recent_floor([{"extras": {"feeRange": [-777.5]}}] * 5, "k")
    assert "777" not in str(excinfo.value)
    with pytest.raises(ChainError) as excinfo:
        fees_module._parse_recent_floor([], "k")
    assert "fewer than 5" in str(excinfo.value)

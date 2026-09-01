"""Tests for the fee estimator wrapper (mempool.space recommended-fees).

All HTTP traffic is served by ``httpx.MockTransport`` — no real network.
"""

import sys
from pathlib import Path

import httpx
import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.chain import ChainError, EsploraClient, FeeEstimator, FeeTarget
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
    assert (fast.target, medium.target, slow.target) == (
        FeeTarget.FAST,
        FeeTarget.MEDIUM,
        FeeTarget.SLOW,
    )
    assert len(server.requests) == 1  # one GET populates all targets
    request = server.requests[0]
    assert request.method == "GET"
    assert request.url.path == "/testnet4/api/v1/fees/recommended"


def test_minimum_fee_sat_vb():
    server = ScriptedServer(httpx.Response(200, json=RECOMMENDED))
    with server.client() as client:
        estimator = FeeEstimator(client, ttl_s=30.0)
        assert estimator.minimum_fee_sat_vb() == 1
    assert len(server.requests) == 1


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
    assert len(server.requests) == 1


# -- caching / TTL ---------------------------------------------------------


def test_cache_serves_within_ttl_without_refetch(monkeypatch: pytest.MonkeyPatch):
    set_time = _freeze_clock(monkeypatch)
    server = ScriptedServer(httpx.Response(200, json=RECOMMENDED))
    with server.client() as client:
        estimator = FeeEstimator(client, ttl_s=30.0)
        estimator.estimate(FeeTarget.FAST)
        set_time(1_000_000.0 + 20.0)  # < 30s TTL
        estimator.estimate(FeeTarget.MEDIUM)
    assert len(server.requests) == 1  # no refetch while fresh


def test_refetches_after_ttl(monkeypatch: pytest.MonkeyPatch):
    set_time = _freeze_clock(monkeypatch)
    server = ScriptedServer(httpx.Response(200, json=RECOMMENDED))
    with server.client() as client:
        estimator = FeeEstimator(client, ttl_s=30.0)
        estimator.estimate(FeeTarget.FAST)
        set_time(1_000_000.0 + 31.0)  # past TTL
        estimator.estimate(FeeTarget.FAST)
    assert len(server.requests) == 2


def test_invalidate_forces_refetch(monkeypatch: pytest.MonkeyPatch):
    _freeze_clock(monkeypatch)
    server = ScriptedServer(httpx.Response(200, json=RECOMMENDED))
    with server.client() as client:
        estimator = FeeEstimator(client, ttl_s=30.0)
        estimator.estimate(FeeTarget.FAST)
        estimator.invalidate()
        estimator.estimate(FeeTarget.FAST)
    assert len(server.requests) == 2


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
    assert len(server.requests) == 2
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

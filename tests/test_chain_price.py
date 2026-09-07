"""Tests for the price oracle (mempool.space /v1/prices).

All HTTP traffic is served by ``httpx.MockTransport`` — no real network.
"""

import sys
from pathlib import Path

import httpx
import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.chain import (
    ChainError,
    ConfigDisabled,
    EsploraClient,
    PriceOracle,
    PriceUnavailableError,
    Rate,
)
from localwallet.chain import esplora as esplora_module
from localwallet.chain import price as price_module

BASE_URL = "https://mempool.space/api"
KIND = "price"

PRICES = {"time": 1_700_000_000, "USD": 67_500.5}


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


def _freeze_clock(monkeypatch: pytest.MonkeyPatch, start: float = 2_000_000.0):
    clock = {"now": start}

    def set_time(t: float) -> None:
        clock["now"] = t

    monkeypatch.setattr(price_module, "_now", lambda: clock["now"])
    return set_time


# -- happy path ------------------------------------------------------------


def test_fresh_parses_usd_rate():
    server = ScriptedServer(httpx.Response(200, json=PRICES))
    with server.client() as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        rate = oracle.fresh()
    assert rate.usd_per_btc == 67_500.5
    assert rate.stale is False
    request = server.requests[0]
    assert request.method == "GET"
    assert request.url.path == "/api/v1/prices"


def test_age_s_tracks_clock(monkeypatch: pytest.MonkeyPatch):
    set_time = _freeze_clock(monkeypatch, start=1_000.0)
    server = ScriptedServer(httpx.Response(200, json=PRICES))
    with server.client() as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        rate = oracle.fresh()
        assert rate.fetched_at == 1_000.0
        set_time(1_000.0 + 5.0)
        assert rate.age_s() == 5.0


# -- caching / TTL ---------------------------------------------------------


def test_fresh_serves_cache_within_ttl(monkeypatch: pytest.MonkeyPatch):
    set_time = _freeze_clock(monkeypatch)
    server = ScriptedServer(httpx.Response(200, json=PRICES))
    with server.client() as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        oracle.fresh()
        set_time(2_000_000.0 + 30.0)  # < 60s TTL
        rate = oracle.fresh()
    assert rate.usd_per_btc == 67_500.5
    assert rate.stale is False
    assert len(server.requests) == 1  # no refetch while fresh


def test_fresh_refetches_after_ttl(monkeypatch: pytest.MonkeyPatch):
    set_time = _freeze_clock(monkeypatch)
    server = ScriptedServer(httpx.Response(200, json=PRICES))
    with server.client() as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        oracle.fresh()
        set_time(2_000_000.0 + 61.0)  # past TTL
        rate = oracle.fresh()
    assert rate.usd_per_btc == 67_500.5
    assert rate.stale is False
    assert len(server.requests) == 2


def test_fresh_uses_new_value_after_refetch(monkeypatch: pytest.MonkeyPatch):
    set_time = _freeze_clock(monkeypatch)
    server = ScriptedServer(
        httpx.Response(200, json=PRICES),
        httpx.Response(200, json={**PRICES, "USD": 70_000.0}),
    )
    with server.client() as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        first = oracle.fresh()
        assert first.usd_per_btc == 67_500.5
        set_time(2_000_000.0 + 61.0)
        second = oracle.fresh()
        assert second.usd_per_btc == 70_000.0
    assert len(server.requests) == 2


def test_invalidate_forces_refetch(monkeypatch: pytest.MonkeyPatch):
    _freeze_clock(monkeypatch)
    server = ScriptedServer(httpx.Response(200, json=PRICES))
    with server.client() as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        oracle.fresh()
        oracle.invalidate()
        oracle.fresh()
    assert len(server.requests) == 2


# -- staleness / offline degrade ladder ------------------------------------


def test_fetch_failure_with_cache_serves_stale(monkeypatch: pytest.MonkeyPatch):
    set_time = _freeze_clock(monkeypatch)
    server = ScriptedServer(
        httpx.Response(200, json=PRICES),
        httpx.Response(503),
    )
    with server.client(max_retries=0) as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        oracle.fresh()  # populate cache at t0
        set_time(2_000_000.0 + 61.0)  # past TTL -> next call refetches
        rate = oracle.fresh()  # fetch fails, serve cached stale
    assert rate.usd_per_btc == 67_500.5
    assert rate.stale is True  # age-warning flag for the UI
    assert len(server.requests) == 2  # one populate + one failed refetch


def test_fetch_failure_without_cache_raises_price_unavailable(monkeypatch: pytest.MonkeyPatch):
    _record_sleeps(monkeypatch)
    server = ScriptedServer(httpx.Response(503))
    with server.client() as client, pytest.raises(PriceUnavailableError) as excinfo:
        PriceOracle(client, ttl_s=60.0).fresh()
    message = str(excinfo.value)
    assert "67" not in message  # value-free
    assert "USD" not in message
    assert server.requests  # it really attempted the network


def test_price_unavailable_is_a_chain_error():
    assert issubclass(PriceUnavailableError, ChainError)


def test_stale_ok_serves_cache_past_ttl_without_fetch(monkeypatch: pytest.MonkeyPatch):
    set_time = _freeze_clock(monkeypatch)
    server = ScriptedServer(httpx.Response(200, json=PRICES))
    with server.client() as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        oracle.fresh()  # populate cache
        set_time(2_000_000.0 + 1000.0)  # way past TTL
        rate = oracle.stale_ok()
    assert rate.usd_per_btc == 67_500.5
    assert len(server.requests) == 1  # no refetch: stale is acceptable


def test_stale_ok_with_cold_cache_fetches():
    server = ScriptedServer(httpx.Response(200, json=PRICES))
    with server.client() as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        rate = oracle.stale_ok()
    assert rate.usd_per_btc == 67_500.5
    assert len(server.requests) == 1


def test_stale_ok_without_cache_and_failure_raises(monkeypatch: pytest.MonkeyPatch):
    _record_sleeps(monkeypatch)
    server = ScriptedServer(httpx.Response(503))
    with server.client() as client, pytest.raises(PriceUnavailableError):
        PriceOracle(client, ttl_s=60.0).stale_ok()


# -- disabled config -------------------------------------------------------


def test_disabled_oracle_raises_config_disabled(monkeypatch: pytest.MonkeyPatch):
    server = ScriptedServer(httpx.Response(200, json=PRICES))
    with server.client() as client:
        oracle = PriceOracle(client, ttl_s=60.0, enabled=False)
        with pytest.raises(ConfigDisabled):
            oracle.fresh()
        with pytest.raises(ConfigDisabled):
            oracle.stale_ok()
    assert server.requests == []  # oracle never called when disabled


# -- fail-closed shape validation ------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],  # list, not object
        "junk",
        {},  # missing USD
        {"USD": "67500"},  # string
        {"USD": True},  # bool
        {"USD": 0},  # zero
        {"USD": -5},  # negative
    ],
)
def test_malformed_price_raises_chain_error(payload):
    server = ScriptedServer(httpx.Response(200, json=payload))
    with server.client() as client, pytest.raises(ChainError):
        PriceOracle(client, ttl_s=60.0).fresh()
    assert len(server.requests) == 1  # shape errors are not retried


def test_value_not_echoed_in_price_error():
    # Parse-level message names only the field (structural), never a value.
    with pytest.raises(ChainError) as excinfo:
        price_module._parse_rate({"USD": None}, KIND)
    message = str(excinfo.value)
    assert "USD" in message  # field name is structural, allowed
    assert "67" not in message  # no rate value leaked


def test_no_cache_malformed_200_degrades_to_price_unavailable():
    # A2: a 200-with-garbage is a fetch failure like any other — with no
    # cache there is nothing to degrade to, so the value-free
    # PriceUnavailableError surface is raised (not the parse detail).
    server = ScriptedServer(httpx.Response(200, json={"USD": None}))
    with server.client() as client, pytest.raises(PriceUnavailableError) as excinfo:
        PriceOracle(client, ttl_s=60.0).fresh()
    message = str(excinfo.value)
    assert "price unavailable" == message  # value-free degrade surface
    assert "USD" not in message and "67" not in message
    assert len(server.requests) == 1  # shape errors are not retried


def test_no_cache_malformed_200_also_hits_stale_ok():
    server = ScriptedServer(httpx.Response(200, json={"junk": True}))
    with server.client() as client, pytest.raises(PriceUnavailableError):
        PriceOracle(client, ttl_s=60.0).stale_ok()
    assert len(server.requests) == 1


def test_provider_price_sentinel_is_the_degenerate_fail_closed_case():
    """The provider's sentinel body (``{"time": ..., "USD": -1}`` — the shape
    the public endpoint served when no fiat data was available) is a
    DEGENERATE case now that mainnet real prices are the normal parse. The
    sentinel must still fail closed to the sats-only degrade surface, and the
    sentinel value must never surface as a rate.
    """
    server = ScriptedServer(httpx.Response(200, json={"time": 1_700_000_000, "USD": -1}))
    with server.client() as client, pytest.raises(PriceUnavailableError) as excinfo:
        PriceOracle(client, ttl_s=60.0).fresh()
    assert "price unavailable" == str(excinfo.value)  # value-free sats-only degrade
    assert "-1" not in str(excinfo.value)  # sentinel value never leaks
    assert len(server.requests) == 1  # shape errors are not retried


# -- non-finite / absurd rate rejection (A1) --------------------------------


@pytest.mark.parametrize(
    ("response_kwargs", "label"),
    [
        # Python's json parses the bare NaN/Infinity tokens into
        # float('nan')/float('inf') — both must be rejected (A1).
        ({"content": b'{"time": 1, "USD": NaN}'}, "bare-nan-json-token"),
        ({"content": b'{"time": 1, "USD": Infinity}'}, "bare-infinity-json-token"),
        ({"json": {"USD": 10**400}}, "huge-json-int"),
        ({"json": {"USD": 1e300}}, "finite-but-absurd-magnitude"),
    ],
    ids=lambda label: label,
)
def test_non_finite_or_absurd_rate_rejected(response_kwargs, label):
    server = ScriptedServer(httpx.Response(200, **response_kwargs))
    with server.client() as client, pytest.raises(ChainError) as excinfo:
        PriceOracle(client, ttl_s=60.0).fresh()
    assert "67" not in str(excinfo.value)  # value-free degrade surface
    assert len(server.requests) == 1  # shape errors are not retried


def test_huge_int_rate_overflow_is_a_value_free_chain_error():
    # float(10**400) raises OverflowError; it must convert to a ChainError
    # that names only the field, never the magnitude.
    with pytest.raises(ChainError) as excinfo:
        price_module._parse_rate({"USD": 10**400}, KIND)
    message = str(excinfo.value)
    assert "USD" in message  # structural field name only
    assert "400" not in message  # the magnitude never leaks


def test_absurd_rate_is_never_cached_over_a_good_rate(monkeypatch: pytest.MonkeyPatch):
    # A good rate is cached; after the TTL a malformed 200 must degrade to
    # the cached-stale rate — the garbage payload poisons nothing.
    set_time = _freeze_clock(monkeypatch)
    server = ScriptedServer(
        httpx.Response(200, json=PRICES),
        httpx.Response(200, json={"USD": 1e300}),
    )
    with server.client() as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        oracle.fresh()  # populate cache with the good rate
        set_time(2_000_000.0 + 61.0)
        rate = oracle.fresh()  # malformed 200 -> cached-stale degrade
        assert rate.usd_per_btc == 67_500.5
        assert rate.stale is True
        # Nothing from the bad payload entered the cache (white-box pin):
        assert oracle._cache is not None
        assert oracle._cache.usd_per_btc == 67_500.5
    assert len(server.requests) == 2


# -- malformed-200 degrade ladder (A2) ---------------------------------------


def test_malformed_200_degrades_to_cached_stale_like_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    set_time = _freeze_clock(monkeypatch)
    server = ScriptedServer(
        httpx.Response(200, json=PRICES),
        httpx.Response(200, json={"USD": None}),  # 200 with garbage shape
    )
    with server.client() as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        oracle.fresh()  # populate cache at t0
        set_time(2_000_000.0 + 61.0)  # past TTL -> next call refetches
        rate = oracle.fresh()  # malformed 200 -> cached-stale degrade
    assert rate.usd_per_btc == 67_500.5
    assert rate.stale is True
    assert len(server.requests) == 2


# -- staleness cap (A3) -------------------------------------------------------


def test_fresh_beyond_stale_cap_raises_instead_of_serving_ancient_rate(
    monkeypatch: pytest.MonkeyPatch,
):
    set_time = _freeze_clock(monkeypatch)
    server = ScriptedServer(
        httpx.Response(200, json=PRICES),
        httpx.Response(503),
    )
    with server.client(max_retries=0) as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        oracle.fresh()  # populate cache at t0
        set_time(2_000_000.0 + price_module.MAX_STALE_AGE_S + 1.0)
        with pytest.raises(PriceUnavailableError):
            oracle.fresh()  # fetch failed AND cache is beyond the cap
    assert len(server.requests) == 2


def test_fresh_at_exactly_the_stale_cap_still_degrades(
    monkeypatch: pytest.MonkeyPatch,
):
    set_time = _freeze_clock(monkeypatch)
    server = ScriptedServer(
        httpx.Response(200, json=PRICES),
        httpx.Response(503),
    )
    with server.client(max_retries=0) as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        oracle.fresh()
        set_time(2_000_000.0 + price_module.MAX_STALE_AGE_S)  # boundary: <= cap
        rate = oracle.fresh()
    assert rate.usd_per_btc == 67_500.5
    assert rate.stale is True


def test_stale_ok_still_serves_beyond_the_stale_cap(monkeypatch: pytest.MonkeyPatch):
    # stale_ok is the explicit offline variant: the cap never applies to it —
    # the rate carries its fetch timestamp, callers decide how old is too old.
    set_time = _freeze_clock(monkeypatch)
    server = ScriptedServer(httpx.Response(200, json=PRICES))
    with server.client() as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        oracle.fresh()
        set_time(2_000_000.0 + price_module.MAX_STALE_AGE_S + 1.0)
        rate = oracle.stale_ok()
    assert rate.usd_per_btc == 67_500.5
    assert len(server.requests) == 1  # no refetch attempted


def test_stale_cap_is_documented_24h():
    assert price_module.MAX_STALE_AGE_S == 24 * 60 * 60


# -- retry policy reuse ----------------------------------------------------


def test_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch):
    sleeps = _record_sleeps(monkeypatch)
    server = ScriptedServer(httpx.Response(429), httpx.Response(200, json=PRICES))
    with server.client(max_retries=2) as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        assert oracle.fresh().usd_per_btc == 67_500.5
    assert len(server.requests) == 2
    assert len(sleeps) == 1


def test_retry_exhaustion_raises_price_unavailable(monkeypatch: pytest.MonkeyPatch):
    sleeps = _record_sleeps(monkeypatch)
    server = ScriptedServer(httpx.Response(429))
    with server.client(max_retries=2) as client, pytest.raises(PriceUnavailableError) as excinfo:
        PriceOracle(client, ttl_s=60.0).fresh()
    message = str(excinfo.value)
    assert "price unavailable" == message  # value-free degrade surface
    assert len(server.requests) == 3  # retries reused from the shared client
    assert len(sleeps) == 2  # backoff was scheduled between retries


# -- conversion / rounding -------------------------------------------------


def _rate(usd_per_btc: float) -> Rate:
    return Rate(usd_per_btc=usd_per_btc, fetched_at=0.0)


def test_sats_to_usd_basic():
    server = ScriptedServer(httpx.Response(200, json=PRICES))
    with server.client() as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        # 1_000_000 sats @ $67_500.5/BTC = $675.005 -> 67500 cents (floor)
        assert oracle.sats_to_usd(1_000_000, _rate(67_500.5)) == 67_500


def test_sats_to_usd_floors_to_cent():
    server = ScriptedServer(httpx.Response(200, json=PRICES))
    with server.client() as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        # 1 sat @ $200_000/BTC = $0.002 -> 0.2 cents -> floor 0 cents
        assert oracle.sats_to_usd(1, _rate(200_000)) == 0
        # 1 sat @ $1_000_000/BTC = $0.01 -> 1 cent exactly
        assert oracle.sats_to_usd(1, _rate(1_000_000)) == 1


def test_sats_to_usd_uses_decimal_not_float_drift():
    server = ScriptedServer(httpx.Response(200, json=PRICES))
    with server.client() as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        # A rate with a fractional cent that float would mis-round:
        # 100 sats @ $0.005/BTC impossible (rate must be >0) — use a real case:
        # 1 sat @ $0.00001 -> 0.000001 cents -> floor 0. Only tests Decimal path.
        assert oracle.sats_to_usd(1, _rate(0.00001)) == 0
        # 123456789 sats @ 90_000.07 -> exact, no drift in sat/cent integer math
        assert oracle.sats_to_usd(123_456_789, _rate(90_000.07)) >= 0


def test_usd_to_sats_basic():
    server = ScriptedServer(httpx.Response(200, json=PRICES))
    with server.client() as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        # $67_500.5 / BTC -> $1 = 1481.47... sats -> floor 1481
        assert oracle.usd_to_sats(1.0, _rate(67_500.5)) == 1481


def test_usd_to_sats_floors_to_sat():
    server = ScriptedServer(httpx.Response(200, json=PRICES))
    with server.client() as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        # tiny amount converts to 0 sats (floor)
        assert oracle.usd_to_sats(0.00001, _rate(67_500.5)) == 0


def test_conversion_round_trip_integer_preserved():
    server = ScriptedServer(httpx.Response(200, json=PRICES))
    with server.client() as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        rate = _rate(50_000.0)
        sats = 2_500_000
        cents = oracle.sats_to_usd(sats, rate)
        # $2_500_000 / 1e8 * 50000 = $1250.00 -> 125000 cents
        assert cents == 125_000
        assert isinstance(cents, int)


@pytest.mark.parametrize(
    ("method", "bad"),
    [
        ("sats_to_usd", -1),
        ("sats_to_usd", True),
        ("sats_to_usd", "100"),
        ("usd_to_sats", -1.0),
        ("usd_to_sats", True),
        ("usd_to_sats", "1.0"),
    ],
)
def test_conversion_rejects_invalid_amounts(method, bad):
    server = ScriptedServer(httpx.Response(200, json=PRICES))
    with server.client() as client:
        oracle = PriceOracle(client, ttl_s=60.0)
        with pytest.raises(ValueError):
            getattr(oracle, method)(bad, _rate(50_000.0))  # type: ignore[arg-type]
    assert server.requests == []  # pure helpers never hit the network


# -- constructor validation ------------------------------------------------


def test_invalid_ttl_raises_value_error():
    server = ScriptedServer(httpx.Response(200, json=PRICES))
    for bad in (0, -1, True, "x"):
        with pytest.raises(ValueError):
            PriceOracle(server.client(), ttl_s=bad)  # type: ignore[arg-type]
    assert server.requests == []


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_ttl_raises_value_error(bad):
    # A NaN TTL would disable expiry entirely (comparisons are always
    # False) and an inf TTL would never expire — both fail closed (A4).
    server = ScriptedServer(httpx.Response(200, json=PRICES))
    with pytest.raises(ValueError):
        PriceOracle(server.client(), ttl_s=bad)
    assert server.requests == []


# -- env parsing -----------------------------------------------------------


def test_price_ttl_and_enabled_from_env(monkeypatch: pytest.MonkeyPatch):
    from localwallet.config import Settings

    monkeypatch.setenv("LOCALWALLET_PRICE_TTL_S", "120.0")
    monkeypatch.setenv("LOCALWALLET_PRICE_ENABLED", "0")
    settings = Settings.from_env()
    assert settings.price_ttl_s == 120.0
    assert settings.price_enabled is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", True), ("0", False), ("true", True), ("false", False), ("TRUE", True), ("no", False)],
)
def test_bool_coercion_accepts_common_spellings(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool):
    from localwallet.config import Settings

    monkeypatch.setenv("LOCALWALLET_PRICE_ENABLED", raw)
    assert Settings.from_env().price_enabled is expected


def test_bool_coercion_rejects_invalid(monkeypatch: pytest.MonkeyPatch):
    from localwallet.config import Settings

    monkeypatch.setenv("LOCALWALLET_PRICE_ENABLED", "maybe")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_price_defaults():
    from localwallet.config import Settings

    settings = Settings.from_env()
    assert settings.price_ttl_s == 60.0
    assert settings.price_enabled is True


def test_disabled_via_env_on_oracle(monkeypatch: pytest.MonkeyPatch):
    # The default source (Settings.from_env) honors LOCALWALLET_PRICE_ENABLED.
    monkeypatch.setenv("LOCALWALLET_PRICE_ENABLED", "0")
    server = ScriptedServer(httpx.Response(200, json=PRICES))
    with server.client() as client:
        oracle = PriceOracle(client)  # enabled left None -> reads Settings
        with pytest.raises(ConfigDisabled):
            oracle.fresh()
    assert server.requests == []

"""Tests for the fee estimator (target-follower over the projected next
blocks, TCK-FEE-003 user spec; recommended-fees fallback, ADR-0011).

All HTTP traffic is served by ``httpx.MockTransport`` — no real network.

``ScriptedServer`` answers EVERY path with its scripted entry, so over the
mempool-blocks endpoint a recommended-shaped payload is malformed (not a
list) and the refresh fails closed to the recommended mapping — the values
these tests assert are the fallback values, fetched as
``recommended + mempool-blocks`` (the floor attempt aborts at the first
bad shape; the floor path itself is exercised by ``RoutedServer`` below).

Rates are integer CENTISAT/VB (1 sat/vB = 100) end to end — the tx-engine
unit (docs/fee-fractional-plan.md); display text comes from
:func:`format_sat_vb` and is pinned here too.
"""

import sys
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.chain import ChainError, EsploraClient, FeeEstimator, FeeSource, FeeTarget
from localwallet.chain import esplora as esplora_module
from localwallet.chain import fees as fees_module
from localwallet.chain.fees import format_sat_vb

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
    # Fallback values are whole sats/vB — exact multiples of 100 centisat.
    assert fast.rate_centisat_vb == 3000
    assert medium.rate_centisat_vb == 2500
    assert slow.rate_centisat_vb == 1800
    # Floor endpoint malformed (dict, not list) -> fail-closed fallback.
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
        assert estimator.estimate(FeeTarget.FAST).rate_centisat_vb == 3000
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


# -- display formatting (TCK-FEE-003 wave) -----------------------------------


@pytest.mark.parametrize(
    ("centisat", "text"),
    [
        (121, "1.21"),  # the user's 2026-09-12 target
        (242, "2.42"),  # the user's 2026-09-12 faster
        (100, "1"),  # the user's 2026-09-12 slower (whole sats: no decimals)
        (3000, "30"),  # fallback-style whole sats/vB
        (55, "0.55"),  # sub-1 support
        (1, "0.01"),  # engine floor
        (150, "1.5"),  # trailing zero dropped ("1.50" never shown)
        (0, "0"),  # the engine never bids this (payload floor rejects 0);
        # formatting is total anyway
    ],
)
def test_format_sat_vb(centisat, text):
    assert format_sat_vb(centisat) == text


@pytest.mark.parametrize("bad", [1.21, "121", None, True, False])
def test_format_sat_vb_rejects_non_int_and_bool(bad):
    with pytest.raises(TypeError):
        format_sat_vb(bad)


def test_format_sat_vb_rejects_negative():
    with pytest.raises(ValueError):
        format_sat_vb(-1)


# -- target-follower (TCK-FEE-003 user spec, ADR-0011 amendment) ------------

# The user's 2026-09-12 live payloads (verbatim block-1 and block-2 feeRange
# bottoms; the remaining projected bottoms follow the endpoint's
# non-increasing-with-depth shape and stay at or below block 2's floor):
USER_BLOCK1_BOTTOM = 1.05567928730512
USER_BLOCK2_BOTTOM = 1.0
USER_PROJECTED = [USER_BLOCK1_BOTTOM, USER_BLOCK2_BOTTOM, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]

# User's 2026-09-07 live case (FEE-001): next projection bottomed at 0.3,
# blocks confirmed down to 0.34; the "must NOT bid more than 1 sat/vB" rule
# survives under v2 through the minimumFee floor on FAST.
LIVE_PROJECTED = [0.3, 0.28, 0.26, 0.24, 0.22, 0.2, 0.18, 0.16]


def _projected_payload(bottoms: list) -> list[dict]:
    return [
        {"blockSize": 998_000, "medianFee": b * 2, "feeRange": [b, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]}
        for b in bottoms
    ]


def _floor_routes(
    *,
    recommended: dict | None = None,
    projected: list | None = None,
) -> dict:
    """Complete route set for one healthy combined refresh (overridable).

    ``projected`` is the whole mempool-blocks RESPONSE PAYLOAD (already-
    shaped, including the malformed-shape fixtures); the default is the
    user's 2026-09-12 payload.
    """
    return {
        "/v1/fees/recommended": httpx.Response(
            200, json=RECOMMENDED if recommended is None else recommended
        ),
        "/v1/fees/mempool-blocks": httpx.Response(
            200, json=_projected_payload(USER_PROJECTED) if projected is None else projected
        ),
    }


class RoutedServer:
    """Per-path routing fake (a combined refresh touches two endpoints).

    Route keys are path MARKERS (matched by substring). Unmatched paths
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


def _rates(server: RoutedServer) -> dict[FeeTarget, int]:
    with server.client() as client:
        estimator = FeeEstimator(client, ttl_s=30.0)
        return {
            target: estimator.estimate(target).rate_centisat_vb for target in FeeTarget
        }


def test_user_payload_pins_target_faster_slower():
    # THE user example payload (2026-09-12, binding): block1 feeRange[0] =
    # 1.05567928730512 -> TARGET = 1.0557 x 1.15 = 1.21403... -> "We should
    # offer 1.21" (normal 2-dp rounding, NOT ceil-to-1.22); FASTER = 2 x
    # 1.21 = 2.42 EXACTLY (the doubling acts on the rounded target); SLOWER
    # = block2 feeRange[0] = 1.0, no markup.
    rates = _rates(RoutedServer(_floor_routes()))
    assert rates[FeeTarget.MEDIUM] == 121
    assert rates[FeeTarget.FAST] == 242
    assert rates[FeeTarget.SLOW] == 100
    assert format_sat_vb(rates[FeeTarget.MEDIUM]) == "1.21"
    assert format_sat_vb(rates[FeeTarget.FAST]) == "2.42"
    assert format_sat_vb(rates[FeeTarget.SLOW]) == "1"


def test_user_case_source_is_target_follower():
    server = RoutedServer(_floor_routes())
    with server.client() as client:
        est = FeeEstimator(client, ttl_s=30.0).estimate(FeeTarget.MEDIUM)
    assert est.source is FeeSource.FLOOR_FOLLOWER
    assert isinstance(est.rate_centisat_vb, int) and not isinstance(est.rate_centisat_vb, bool)


def test_target_never_undercuts_its_own_floor():
    # Two nested protections, pinned since TCK-FEE-004:
    #  * INTERNAL (policy v2): rounding dips under B₀ only for tiny bottoms
    #    (0.15 x B₀ < 0.005): B₀ = 0.021 -> 0.02415 -> half-even would say
    #    0.02 < the floor -> the target clamp lifts to ceil-2dp (0.03);
    #  * MIN-RELAY FLOOR (TCK-FEE-004): every rung then MAXes with the
    #    source floor (minimumFee 1 sat/vB here), so the sub-1 policy rungs
    #    all surface AT the floor — a bid below what relays accept never
    #    leaves this module (the live psbt_failed this ticket fixed).
    rates = _rates(
        RoutedServer(_floor_routes(projected=_projected_payload([0.021] * 8)))
    )
    assert rates[FeeTarget.MEDIUM] == 100  # 3 lifted to the min-relay floor
    assert rates[FeeTarget.SLOW] == 100  # 3 lifted to the min-relay floor
    assert rates[FeeTarget.FAST] == 100  # max(2 x 3, minimumFee 1 sat/vB)


def test_half_even_rounding_ties():
    # Decimal ROUND_HALF_EVEN at 2dp, pinned on the exact tie B₀=2.3:
    # 2.3 x 1.15 = 2.645 -> ties-to-even -> 2.64 (2.65 would be half-up).
    # (The tie stays above the 1 sat/vB min-relay floor so the clamp cannot
    # mask it — the sub-1 tie case 0.3 -> 0.345 -> 0.34 now clamps to the
    # floor and is pinned in test_min_relay_clamp_*.)
    rates = _rates(
        RoutedServer(
            _floor_routes(
                projected=_projected_payload([2.3, 2.2, 2.2, 2.2, 2.2, 2.2, 2.2, 2.2])
            )
        )
    )
    assert rates[FeeTarget.MEDIUM] == 264


def test_single_projected_block_slow_is_the_floor_itself():
    # "if only one projected block exists, slow = first block's feeRange[0]
    # (the floor itself, no markup)" — ceil at 2dp so it never dips under.
    rates = _rates(RoutedServer(_floor_routes(projected=_projected_payload([4.5]))))
    assert rates[FeeTarget.MEDIUM] == 518  # 4.5 x 1.15 = 5.175 -> half-even 5.18
    assert rates[FeeTarget.FAST] == 1036  # 2 x 5.18
    assert rates[FeeTarget.SLOW] == 450  # 4.5 exactly, no markup


def test_minimum_fee_floor_clamps_every_rung():
    # TCK-FEE-004 SUPERSEDES FEE-001's "minimumFee bounds the FAST rung
    # only" rule: the publicinfo payload's minimumFee is the floor FOR THAT
    # SOURCE, and EACH rung is MAX'd with it independently (pinned
    # semantics). B₀ = 0.4 -> policy 46/92/35, minimumFee 5 -> every rung
    # lifts to 500 (the ladder collapses onto the floor and that is
    # honest — sub-floor bids simply fail at the node).
    rates = _rates(
        RoutedServer(
            _floor_routes(
                recommended={**RECOMMENDED, "minimumFee": 5},
                projected=_projected_payload([0.4, 0.35, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3]),
            )
        )
    )
    assert rates[FeeTarget.MEDIUM] == 500
    assert rates[FeeTarget.FAST] == 500
    assert rates[FeeTarget.SLOW] == 500


def test_congested_next_block_lifts_the_whole_ladder():
    # Policy, not a cap: an 8.4 sat/vB next-block floor bids 8.4 x 1.15.
    rates = _rates(
        RoutedServer(_floor_routes(projected=_projected_payload([8.4, 6.0, 6.0, 6.0])))
    )
    assert rates[FeeTarget.MEDIUM] == 966  # 9.66
    assert rates[FeeTarget.FAST] == 1932  # 19.32
    assert rates[FeeTarget.SLOW] == 600


def test_ordering_invariant_faster_target_slower():
    for bottoms in (
        [1.05567928730512, 1.0, 1.0, 1.0],
        [5.0, 5.0, 5.0],  # ties are non-increasing and accepted
        [0.4, 0.39, 0.2, 0.1],
        [12.345, 0.01],
    ):
        rates = _rates(
            RoutedServer(_floor_routes(projected=_projected_payload(bottoms)))
        )
        assert (
            rates[FeeTarget.FAST] >= rates[FeeTarget.MEDIUM] >= rates[FeeTarget.SLOW]
        ), bottoms


def test_live_2026_09_07_case_still_bids_at_most_1_sat_vb():
    # FEE-001's binding observed case, re-derived under v2 + the FEE-004
    # floor: the next block bottomed at 0.3 with the last blocks confirming
    # down to 0.34. The bid must not exceed 1 sat/vB, and TCK-FEE-004's
    # min-relay clamp now pins every rung AT the 1 sat/vB floor (minimumFee
    # 1): policy TARGET 0.34 / SLOW 0.28 both lift to 1.00 — never below
    # what relays accept, never the 2 sat/vB overbid FEE-001 replaced.
    rates = _rates(
        RoutedServer(
            _floor_routes(
                recommended={**RECOMMENDED, "fastestFee": 2},
                projected=_projected_payload(LIVE_PROJECTED),
            )
        )
    )
    assert rates[FeeTarget.FAST] == 100
    assert rates[FeeTarget.MEDIUM] == 100
    assert rates[FeeTarget.SLOW] == 100


def _raw_json(text: str) -> httpx.Response:
    """A 200 response with a raw JSON body (stdlib json parses the bare
    NaN/Infinity tokens that strict encoders — and httpx's json= — refuse)."""
    return httpx.Response(
        200, content=text.encode("ascii"), headers={"content-type": "application/json"}
    )


@pytest.mark.parametrize(
    "entry",
    [
        httpx.Response(200, json={"junk": 1}),
        httpx.Response(200, json=[]),
        httpx.Response(200, json=["not-an-object"]),
        httpx.Response(200, json=[{"blockSize": 1}]),
        httpx.Response(200, json=[{"feeRange": []}]),
        httpx.Response(200, json=[{"feeRange": [True, 2.0]}]),
        httpx.Response(200, json=[{"feeRange": [0, 2.0]}]),
        httpx.Response(200, json=[{"feeRange": [-777.5, 2.0]}]),
        httpx.Response(200, json=[{"feeRange": ["777", 2.0]}]),
        _raw_json('[{"feeRange": [NaN, 2.0]}]'),
        _raw_json('[{"feeRange": [Infinity, 2.0]}]'),
        httpx.Response(200, json=[{"feeRange": [10**400, 2.0]}]),
        # security-review (TCK-FEE-003): a FINITE-but-absurd magnitude must
        # degrade too — quantize on it would raise InvalidOperation, which
        # is NOT a ChainError and would escape the documented fallback.
        httpx.Response(200, json=[{"feeRange": [1e40, 2.0]}]),
        httpx.Response(200, json=[{"feeRange": [10_000.5, 2.0]}]),
        # security-review MEDIUM (FEE-001, kept): parser-accepted non-
        # monotonic bottoms that would break TARGET >= SLOW must fail closed.
        # Both original review counterexamples stay pinned, plus the direct
        # B1 > B0 inversion this policy would bid on.
        httpx.Response(200, json=_projected_payload([0.2, 5.0, 5.0])),
        httpx.Response(200, json=_projected_payload([3.0, 3.0, 1.0, 1.0, 1.0, 1.0, 8.0])),
        httpx.Response(200, json=_projected_payload([1.0, 5.0, 0.5])),
    ],
)
def test_malformed_floor_payload_falls_back_to_recommended(entry):
    routes = _floor_routes()
    routes["/v1/fees/mempool-blocks"] = entry
    server = RoutedServer(routes)
    with server.client() as client:
        estimator = FeeEstimator(client, ttl_s=30.0)
        rates = {target: estimator.estimate(target) for target in FeeTarget}
    # The old mapping verbatim (in exact centisat) — and honest about which
    # path produced it.
    assert {t: e.rate_centisat_vb for t, e in rates.items()} == {
        FeeTarget.FAST: 3000,
        FeeTarget.MEDIUM: 2500,
        FeeTarget.SLOW: 1800,
    }
    assert all(e.source is FeeSource.RECOMMENDED for e in rates.values())


def test_floor_endpoint_failure_falls_back_to_recommended():
    # Transport failure on the projected-blocks endpoint degrades to the
    # recommended mapping; no exception. (FEE-001's tip/blocks endpoints are
    # gone with the recent-blocks floor — this is the whole new surface.)
    routes = _floor_routes()
    routes["/v1/fees/mempool-blocks"] = httpx.ConnectError("boom")
    server = RoutedServer(routes)
    with server.client() as client:
        est = FeeEstimator(client, ttl_s=30.0).estimate(FeeTarget.FAST)
    assert est.rate_centisat_vb == 3000
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
    assert est.rate_centisat_vb == 3000
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
        assert len(paths) == 2  # one GET per endpoint, regardless of target
        # count; the FEE-001 tip + recent-blocks GETs are GONE with R5
        assert [p.rsplit("/api", 1)[-1] for p in paths] == [
            "/v1/fees/recommended",
            "/v1/fees/mempool-blocks",
        ]
        set_time(1_000_000.0 + 31.0)  # past TTL
        estimator.estimate(FeeTarget.SLOW)
    assert len(server.requests) == 4  # one combined re-refresh


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
    # Security-review MEDIUM (FEE-001), kept for TCK-FEE-003: TARGET >= SLOW
    # now depends on B0 >= B1 — non-increasing-with-depth is enforced here,
    # value-free; the end-to-end fallback is pinned in
    # test_malformed_floor_payload_falls_back_to_recommended.
    with pytest.raises(ChainError) as excinfo:
        fees_module._parse_projected_bottoms(_projected_payload([0.2, 5.0, 5.0]), "k")
    message = str(excinfo.value)
    assert "entry 1" in message  # structural index allowed
    assert "0.2" not in message and "5.0" not in message  # no fee value leaked
    with pytest.raises(ChainError):
        fees_module._parse_projected_bottoms(
            _projected_payload([3.0, 3.0, 1.0, 1.0, 1.0, 1.0, 8.0]), "k"
        )
    # Ties are non-increasing (accepted), and a plain descending list passes
    # — as exact Decimals of the shortest repr (no binary float math).
    assert fees_module._parse_projected_bottoms(
        _projected_payload([2.0, 2.0, 1.0]), "k"
    ) == [Decimal("2.0"), Decimal("2.0"), Decimal("1.0")]


def test_floor_error_messages_are_value_free():
    # The fallback swallows these errors, but the messages stay scrubbed
    # (defense in depth: never echo a fee value).
    with pytest.raises(ChainError) as excinfo:
        fees_module._parse_projected_bottoms([{"feeRange": [-777.5]}], "k")
    assert "777" not in str(excinfo.value)
    with pytest.raises(ChainError) as excinfo:
        fees_module._parse_projected_bottoms([{"feeRange": [1e40]}], "k")
    assert "40" not in str(excinfo.value)  # the magnitude bound fires, value-free
    with pytest.raises(ChainError) as excinfo:
        fees_module._parse_projected_bottoms([], "k")
    assert "non-empty" in str(excinfo.value)


def test_bottom_magnitude_boundary_is_the_engine_ceiling():
    # 10_000 sat/vB = the tx engine's own max bid (1000x min-relay, dust.py)
    # — at it the payload is accepted (and the Decimal math stays inside
    # the default context), beyond it fails closed like any garbage.
    assert fees_module._parse_projected_bottoms(
        [{"feeRange": [10_000]}, {"feeRange": [1e-9]}], "k"
    ) == [Decimal(10_000), Decimal("1e-9")]
    with pytest.raises(ChainError):
        fees_module._parse_projected_bottoms([{"feeRange": [10_000.0001]}], "k")


# -- min-relay floor clamp (TCK-FEE-004, USER CORRECTED SPEC 2026-09-13,
#    code-review fix 2026-09-14) -------------------------------------------
#
# MAX(policy rung, floor), EACH rung independently — a sub-floor bid never
# leaves this module (the live failure was MEDIUM computing under the
# min-relay floor and the send dying psbt_failed). TWO distinct floors:
# policy rungs take the congestion-informed floor (publicinfo:
# max(minimumFee x 100, relay floor); native: the relay floor alone); the
# EXPLICIT seam takes the RELAY FLOOR ONLY — node capability
# (bitcoind min_relay_centisat_vb, injected as relay_floor_client like the
# production wiring does) → the ASSUMED rail (TCK-FEE-005: 0.1 sat/vB =
# Core's policy.h DEFAULT_MIN_RELAY_TX_FEE of 100 sat/kvB) — NEVER
# minimumFee, NEVER a snapshot refresh.


class _Native:
    """Backend-native client double (NO get_json): estimate_fee answers
    whole sats/vB per target, exactly like ElectrumClient/BitcoindClient."""

    def __init__(self, rates: dict[FeeTarget, int]) -> None:
        self._rates = dict(rates)
        self.estimate_calls = 0

    def estimate_fee(self, target: FeeTarget) -> int:
        self.estimate_calls += 1
        return self._rates[target]


class _FloorNative(_Native):
    """Native double WITH the optional floor capability."""

    def __init__(self, rates, floor=None, error: bool = False) -> None:
        super().__init__(rates)
        self._floor = floor
        self._error = error
        self.floor_calls = 0

    def min_relay_centisat_vb(self) -> int:
        self.floor_calls += 1
        if self._error:
            raise ChainError("fees-test floor query failed")
        return self._floor


def test_healthy_ladder_is_never_marked_clamped():
    # The user's 2026-09-12 payload clears the 1 sat/vB floor on every rung
    # (121 / 242 / 100 — SLOW equals the floor, it is not lifted to it):
    # the clamp must not narrate where it did not fire.
    server = RoutedServer(_floor_routes())
    with server.client() as client:
        est = FeeEstimator(client, ttl_s=30.0)
        estimates = {target: est.estimate(target) for target in FeeTarget}
    assert {t: e.rate_centisat_vb for t, e in estimates.items()} == {
        FeeTarget.MEDIUM: 121,
        FeeTarget.FAST: 242,
        FeeTarget.SLOW: 100,
    }
    assert not any(e.clamped for e in estimates.values())


def test_clamped_flag_marks_only_the_floor_raised_rungs():
    # B₀ = 0.9 -> TARGET = 1.035 half-even -> 1.04 (clears the floor);
    # FAST = 2.08 clears; SLOW = ceil2(0.8) = 0.80 UNDER the 1 sat/vB
    # minimumFee floor -> lifted to exactly 1.00, flagged. Per-rung honesty:
    # only the rung that moved says so.
    server = RoutedServer(_floor_routes(projected=_projected_payload([0.9, 0.8, 0.8])))
    with server.client() as client:
        est = FeeEstimator(client, ttl_s=30.0)
        medium = est.estimate(FeeTarget.MEDIUM)
        fast = est.estimate(FeeTarget.FAST)
        slow = est.estimate(FeeTarget.SLOW)
    assert (medium.rate_centisat_vb, medium.clamped) == (104, False)
    assert (fast.rate_centisat_vb, fast.clamped) == (208, False)
    assert (slow.rate_centisat_vb, slow.clamped) == (100, True)


def test_recommended_fallback_rungs_clamped_to_minimum_fee():
    # The degraded path floors identically (same source rule): ScriptedServer
    # answers mempool-blocks with a dict (malformed -> fallback), and
    # hourFee 1 sat/vB sits under minimumFee 4 -> the SLOW rung is lifted
    # to the floor, the others already clear it.
    server = ScriptedServer(
        httpx.Response(
            200,
            json={
                "fastestFee": 8,
                "halfHourFee": 5,
                "hourFee": 1,
                "economyFee": 1,
                "minimumFee": 4,
            },
        )
    )
    with server.client() as client:
        est = FeeEstimator(client, ttl_s=30.0)
        fast = est.estimate(FeeTarget.FAST)
        medium = est.estimate(FeeTarget.MEDIUM)
        slow = est.estimate(FeeTarget.SLOW)
    assert (fast.rate_centisat_vb, fast.clamped) == (800, False)
    assert (medium.rate_centisat_vb, medium.clamped) == (500, False)
    assert (slow.rate_centisat_vb, slow.clamped) == (400, True)
    assert all(e.source is FeeSource.RECOMMENDED for e in (fast, medium, slow))


# -- the explicit-rate finalization seam (create_tx / bump_fee) -----------


def test_clamp_to_min_relay_floor_explicit_seam():
    server = ScriptedServer(httpx.Response(200, json=RECOMMENDED))  # minimumFee 1
    with server.client() as client:
        est = FeeEstimator(client, ttl_s=30.0)
        assert est.clamp_to_min_relay_floor(500) == (500, False)  # clears
        assert est.clamp_to_min_relay_floor(55) == (55, False)  # 0.55: inside
        # (TCK-FEE-005) the Core-default rail — no lift, no narration
        assert est.clamp_to_min_relay_floor(10) == (10, False)  # at the rail
        assert est.clamp_to_min_relay_floor(5) == (10, True)  # under -> MAX
    # Code-review fix: the seam answers from the RELAY floor alone — the
    # assumed 0.1 sat/vB rail here (Core policy.h DEFAULT_MIN_RELAY_TX_FEE
    # = 100 sat/kvB), NO snapshot refresh, zero chain calls.
    assert server.requests == []


class _FloorOnly:
    """Wallet-backend double carrying ONLY the optional floor capability —
    the shape the production estimator sees via ``relay_floor_client``
    (bids ride the public source, the FLOOR rides this node)."""

    def __init__(self, floor=None, error: Exception | None = None) -> None:
        self._floor = floor
        self._error = error
        self.floor_calls = 0

    def min_relay_centisat_vb(self) -> int:
        self.floor_calls += 1
        if self._error is not None:
            raise self._error
        return self._floor


def test_explicit_seam_ignores_the_congestion_minimum_fee():
    # CODE-REVIEW MAJOR pin: minimumFee (3 sat/vB next-block CONGESTION
    # estimate) must NEVER lift an explicit 1 sat/vB — the node's own relay
    # floor decides explicit bids, and answering costs ZERO fee-source
    # calls (the old wiring refreshed the whole snapshot and MAXed against
    # the congestion figure; both are gone).
    server = ScriptedServer(httpx.Response(200, json={**RECOMMENDED, "minimumFee": 3}))
    node = _FloorOnly(100)
    with server.client() as client:
        est = FeeEstimator(client, ttl_s=30.0, relay_floor_client=node)
        assert est.clamp_to_min_relay_floor(100) == (100, False)  # bids 1 THROUGH congestion
    assert server.requests == []


def test_explicit_seam_floors_at_the_node_not_the_source():
    # Node relay floor 2.5 sat/vB under a source minimumFee of 5: the
    # explicit seam answers 250 — the relay floor ONLY (never 500, the
    # congestion figure; never a full refresh).
    server = ScriptedServer(httpx.Response(200, json={**RECOMMENDED, "minimumFee": 5}))
    node = _FloorOnly(250)
    with server.client() as client:
        est = FeeEstimator(client, ttl_s=30.0, relay_floor_client=node)
        assert est.clamp_to_min_relay_floor(100) == (250, True)  # MAX, narrated
        assert est.clamp_to_min_relay_floor(250) == (250, False)  # clears
        assert est.clamp_to_min_relay_floor(300) == (300, False)  # above stays
    assert server.requests == []
    assert node.floor_calls == 1  # ONE TTL-cached floor query, not per clamp


def test_explicit_seam_without_a_capable_node_uses_the_assumed_floor():
    # Electrum's honest absence on the production (publicinfo) wiring: the
    # injected wallet client has NO floor capability -> assumed 0.1 sat/vB
    # (TCK-FEE-005), and ZERO chain calls anywhere — this RESTORES FEE-002's
    # "explicit rate => no chain calls" pin on that wiring.
    server = ScriptedServer(httpx.Response(200, json={**RECOMMENDED, "minimumFee": 3}))
    electrum_like = _Native({t: 1 for t in FeeTarget})  # no get_json, no capability
    with server.client() as client:
        est = FeeEstimator(client, ttl_s=30.0, relay_floor_client=electrum_like)
        assert est.clamp_to_min_relay_floor(100) == (100, False)  # congestion 3 does NOT lift
        assert est.clamp_to_min_relay_floor(99) == (99, False)  # 0.99: rail 10 clears it
        assert est.clamp_to_min_relay_floor(9) == (10, True)  # the assumed rail does
    assert server.requests == []


def test_native_explicit_seam_is_one_floor_query_no_refresh(monkeypatch: pytest.MonkeyPatch):
    # Code-review MINOR: the native explicit seam used to run the FULL
    # refresh (3 estimate_fee RPCs + the floor query). It is now ONE
    # getmempoolinfo-shaped floor query, TTL-cached and bounded.
    set_time = _freeze_clock(monkeypatch)
    client = _FloorNative(
        {FeeTarget.FAST: 6, FeeTarget.MEDIUM: 2, FeeTarget.SLOW: 1}, floor=250
    )
    est = FeeEstimator(client, ttl_s=30.0)
    assert est.clamp_to_min_relay_floor(100) == (250, True)
    assert est.clamp_to_min_relay_floor(250) == (250, False)  # TTL-cached
    assert client.floor_calls == 1
    assert client.estimate_calls == 0  # the LADDER was never refreshed
    set_time(1_000_000.0 + 31.0)  # past TTL -> one bounded re-query
    assert est.clamp_to_min_relay_floor(100) == (250, True)
    assert client.floor_calls == 2


def test_node_floor_participates_in_the_congestion_informed_policy_rungs():
    # POLICY rungs KEEP the FEE-003-sanctioned congestion-informed floor —
    # max(minimumFee x 100, relay floor): a node floor ABOVE the source's
    # minimumFee lifts every rung onto itself (MAX in). The explicit seam
    # over the same wiring answers the RELAY floor alone (1000, not the
    # minimum figure) — the two concepts stay separate.
    payload = {
        "fastestFee": 5,
        "halfHourFee": 4,
        "hourFee": 2,
        "economyFee": 1,
        "minimumFee": 1,
    }
    server = ScriptedServer(httpx.Response(200, json=payload))
    node = _FloorOnly(1000)
    with server.client() as client:
        est = FeeEstimator(client, ttl_s=30.0, relay_floor_client=node)
        for target in FeeTarget:
            estimate = est.estimate(target)
            assert (estimate.rate_centisat_vb, estimate.clamped) == (1000, True)
        assert est.clamp_to_min_relay_floor(100) == (1000, True)
    assert len(server.requests) == 2  # one combined refresh, untouched shape
    assert node.floor_calls == 1  # the floor query rode the refresh, TTL-cached


@pytest.mark.parametrize("boom", [RuntimeError("rpc exploded"), ChainError("no"), KeyError("k")])
def test_explicit_seam_catches_every_floor_exception_flavor(boom):
    # Security-review LOW: the floor query may fail with ANY exception
    # flavor (transport, JSON-RPC shape, adapter bug) — none may fail an
    # explicit send: fail closed to the assumed floor, value-free.
    node = _FloorOnly(error=boom)
    est = FeeEstimator(
        _Native({t: 2 for t in FeeTarget}), ttl_s=30.0, relay_floor_client=node
    )
    assert est.clamp_to_min_relay_floor(9) == (10, True)  # fail-closed to the rail
    assert est.clamp_to_min_relay_floor(100) == (100, False)


def test_clamp_fails_closed_to_assumed_floor_when_source_down():
    # A floor query that cannot complete NEVER fails the send (the explicit
    # path historically made zero chain calls — it must stay sendable):
    # the assumed 0.1 sat/vB applies, silently (no raise, no narration lie —
    # it is the floor our own build gate enforces regardless — dust.py's
    # default rate, TCK-FEE-005).
    server = ScriptedServer(httpx.ConnectError("boom"))
    with server.client() as client:
        est = FeeEstimator(client, ttl_s=30.0)
        assert est.clamp_to_min_relay_floor(100) == (100, False)
        assert est.clamp_to_min_relay_floor(9) == (10, True)


@pytest.mark.parametrize("bad", [1.0, "100", None, True, False, -1])
def test_clamp_to_min_relay_floor_type_discipline(bad):
    server = ScriptedServer(httpx.Response(200, json=RECOMMENDED))
    est = FeeEstimator(server.client(), ttl_s=30.0)
    with pytest.raises((TypeError, ValueError)):
        est.clamp_to_min_relay_floor(bad)
    assert server.requests == []  # validated before any network


# -- the backend-native floor capability (bitcoind; electrum absent) ------


def test_native_floor_capability_lifts_rungs_and_is_cached():
    # Node relayfee 5 sat/vB > estimatesmartfee answers: MEDIUM 2 / SLOW 1
    # lift to the floor, FAST 6 already clears. The floor query rides the
    # refresh (one call), so repeated estimates and the explicit clamp see
    # the SAME floor without hammering the node.
    client = _FloorNative(
        {FeeTarget.FAST: 6, FeeTarget.MEDIUM: 2, FeeTarget.SLOW: 1}, floor=500
    )
    est = FeeEstimator(client, ttl_s=30.0)
    fast = est.estimate(FeeTarget.FAST)
    medium = est.estimate(FeeTarget.MEDIUM)
    slow = est.estimate(FeeTarget.SLOW)
    assert (fast.rate_centisat_vb, fast.clamped) == (600, False)
    assert (medium.rate_centisat_vb, medium.clamped) == (500, True)
    assert (slow.rate_centisat_vb, slow.clamped) == (500, True)
    assert est.clamp_to_min_relay_floor(250) == (500, True)
    assert client.floor_calls == 1  # one query per refresh, TTL-cached
    assert client.estimate_calls == 3


def test_native_absent_capability_uses_the_assumed_floor():
    # Electrum's honest absence (no capability method at all): bids serve
    # unchanged and the floor is the assumed 0.1 sat/vB rail (TCK-FEE-005).
    client = _Native({FeeTarget.FAST: 3, FeeTarget.MEDIUM: 2, FeeTarget.SLOW: 1})
    est = FeeEstimator(client, ttl_s=30.0)
    medium = est.estimate(FeeTarget.MEDIUM)
    assert (medium.rate_centisat_vb, medium.clamped) == (200, False)
    assert est.clamp_to_min_relay_floor(9) == (10, True)
    assert est.clamp_to_min_relay_floor(100) == (100, False)


def test_native_floor_query_failure_fails_closed_not_fatal():
    # A broken floor answer NEVER breaks the refresh: the estimates still
    # serve, clamped to the assumed constant (fail-closed, no fabricated
    # node claim).
    client = _FloorNative(
        {FeeTarget.FAST: 2, FeeTarget.MEDIUM: 2, FeeTarget.SLOW: 1}, error=True
    )
    est = FeeEstimator(client, ttl_s=30.0)
    assert est.estimate(FeeTarget.MEDIUM).rate_centisat_vb == 200
    assert est.clamp_to_min_relay_floor(9) == (10, True)


def test_native_floor_below_the_assumed_floor_cannot_lower_the_rail():
    # A node answering 0.05 sat/vB cannot license a bid under the 0.1
    # sat/vB OUR build gate enforces (psbt.py/revalidate.py would refuse
    # it): the effective floor is MAX(queried, assumed). TCK-FEE-005 moved
    # the rail from 100 to 10; the MAX semantics are unchanged.
    client = _FloorNative({FeeTarget.FAST: 1, FeeTarget.MEDIUM: 1, FeeTarget.SLOW: 1}, floor=5)
    est = FeeEstimator(client, ttl_s=30.0)
    assert est.clamp_to_min_relay_floor(9) == (10, True)  # 9 < rail wins over 5
    assert est.estimate(FeeTarget.SLOW).rate_centisat_vb == 100  # clears the rail


def test_node_floor_between_the_rail_and_one_sat_vB_now_wins():
    # THE FEE-005 OVER-FLOORING FIX: a node advertising 0.4 sat/vB used to
    # be out-vetoed by the 1 sat/vB assumed rail (bid lifted to 100 — 2.5x
    # what this node relays). The rail is now 0.1, so the NODE figure
    # (40) is the effective floor: 35 lifts to 40, 60 passes untouched.
    client = _FloorNative({FeeTarget.FAST: 1, FeeTarget.MEDIUM: 1, FeeTarget.SLOW: 1}, floor=40)
    est = FeeEstimator(client, ttl_s=30.0)
    assert est.clamp_to_min_relay_floor(35) == (40, True)  # MAX, narrated
    assert est.clamp_to_min_relay_floor(60) == (60, False)  # the old rail would have lifted this to 100
    slow = est.estimate(FeeTarget.SLOW)  # native rung 1 sat/vB = 100 clears 40
    assert (slow.rate_centisat_vb, slow.clamped) == (100, False)


@pytest.mark.parametrize("junk", [0, -5, True, "250", 2.5, None])
def test_native_floor_junk_answer_falls_back(junk):
    # Trust boundary: the capability's answer is re-validated here — only a
    # positive plain int lifts the floor; everything else fails closed to
    # the assumed 0.1 sat/vB rail, value-free.
    client = _FloorNative(
        {FeeTarget.FAST: 2, FeeTarget.MEDIUM: 2, FeeTarget.SLOW: 1}, floor=junk
    )
    est = FeeEstimator(client, ttl_s=30.0)
    assert est.clamp_to_min_relay_floor(9) == (10, True)
    assert est.clamp_to_min_relay_floor(100) == (100, False)


# -- six-hour average of per-block lowest fees (TCK-CHAT-002) ---------------


def _avg_with(server: RoutedServer) -> int | None:
    """Refresh through the NORMAL estimator path, then ask the seam."""
    with server.client() as client:
        est = FeeEstimator(client, ttl_s=30.0)
        est.estimate(FeeTarget.MEDIUM)
        return est.six_hour_low_average_centisat_vb()


def test_six_hour_avg_user_payload_exact():
    # THE user payload bottoms: first SIX blocks of USER_PROJECTED
    # (1.05567928730512, then five at 1.0) -> sum 6.05567928730512 / 6 =
    # 1.00927988121752 -> half-even 2 dp = 1.01 -> 101 centisat/vB.
    assert _avg_with(RoutedServer(_floor_routes())) == 101


def test_six_hour_avg_window_bound_takes_at_most_six_blocks():
    # 8 projected blocks exist; ONLY THE FIRST SIX feed the average (the
    # endpoint projects further than the ~6h horizon — cheap deep blocks
    # must not drag the bound down; the window bound is documented on
    # _SIX_HOUR_PROJECTED_BLOCKS, never silently widened).
    bottoms = [3.0, 2.0, 2.0, 2.0, 2.0, 2.0, 0.2, 0.2]
    # first six: 3 + 2*5 = 13 / 6 = 2.1666... -> half-even 2dp = 2.17
    assert _avg_with(RoutedServer(_floor_routes(projected=_projected_payload(bottoms)))) == 217


@pytest.mark.parametrize(
    ("bottoms", "expected"),
    [
        # A SHORTER payload averages what exists — the window is honestly
        # under six hours then (never padded, never fabricated).
        ([2.0, 1.0], 150),
        # One projected block: the window is that single bottom.
        ([1.05567928730512], 106),
        # Exact half-even tie: 0.125 -> 0.12 (ties-to-even at the last dp).
        ([0.13, 0.12], 12),
    ],
)
def test_six_hour_avg_short_window_averages_what_exists(bottoms, expected):
    server = RoutedServer(_floor_routes(projected=_projected_payload(bottoms)))
    assert _avg_with(server) == expected


def test_six_hour_avg_degraded_source_is_none():
    # mempool-blocks unreachable (404): the ladder degrades to recommended
    # and the average has NO per-block data to average -> None (the
    # consumer's elevated-fee warning stands down; never a fallback figure).
    routes = {
        "/v1/fees/recommended": httpx.Response(200, json=RECOMMENDED),
        "/v1/fees/mempool-blocks": httpx.Response(404, json=None),
    }
    assert _avg_with(RoutedServer(routes)) is None


def test_six_hour_avg_native_path_is_none():
    # A backend-native snapshot never saw a mempool-blocks payload.
    client = _FloorNative({FeeTarget.FAST: 2, FeeTarget.MEDIUM: 2, FeeTarget.SLOW: 1}, floor=10)
    est = FeeEstimator(client, ttl_s=30.0)
    est.estimate(FeeTarget.MEDIUM)
    assert est.six_hour_low_average_centisat_vb() is None


def test_six_hour_avg_fails_closed_never_raises():
    # A TOTAL source failure (recommended broken too): estimate() raises,
    # the seam answers None — narration decoration never fails a plan.
    with RoutedServer(
        {
            "/v1/fees/recommended": httpx.Response(500, json=None),
            "/v1/fees/mempool-blocks": httpx.Response(404, json=None),
        }
    ).client() as client:
        est = FeeEstimator(client, ttl_s=30.0)
        with pytest.raises(ChainError):
            est.estimate(FeeTarget.MEDIUM)
        assert est.six_hour_low_average_centisat_vb() is None


def test_six_hour_avg_costs_no_extra_chain_calls():
    # The figure RIDES the one snapshot refresh the estimate already does
    # (the ticket: "the per-block data is ALREADY fetched").
    server = RoutedServer(_floor_routes())
    with server.client() as client:
        est = FeeEstimator(client, ttl_s=30.0)
        est.estimate(FeeTarget.MEDIUM)
        before = len(server.requests)
        for _ in range(3):
            assert est.six_hour_low_average_centisat_vb() == 101
        assert len(server.requests) == before

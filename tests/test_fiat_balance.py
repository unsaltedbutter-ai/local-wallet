"""TCK-FIAT-001: the ``get_balance`` answer carries best-effort USD.

No new intent — the handler closes over the SAME price-oracle wiring as
``create_tx``'s USD path (ADR-0011 ladder) and gains ``usd_total_cents``
+ ``btc_usd`` (+ the ``rate_stale``/``rate_age_s`` markers when the rate
is stale-but-served). ANY price failure — unavailable feed,
capability-absent backend, disabled oracle, even an unexpected bug —
leaves the keys ABSENT: a sats-only answer, never an error.
``_print_balance`` renders one extra line iff the keys are present,
figures verbatim from the handler result (send-card formatting via
``_card_rate``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from localwallet.app import StartupScan, _print_balance, build_dispatch_table
from localwallet.chain import EsploraClient, PriceOracle
from localwallet.protocol import DispatchTable, IntentName, validate_payload
from localwallet.store import Store
from localwallet.wallet import GAP_LIMIT_SETTING, scan_wallet
from localwallet.wallet.descriptor import WalletDescriptor
from tests.test_e2e_skeleton import (
    EXPECTED_TOTAL,
    GET_BALANCE_JSON,
    TEST_GAP,
    UTXOS_ADDR0,
    UTXOS_ADDR1,
    ZPUB,
    _mock_client,
    _scan_handler,
    derive_fixture_addresses,
)

#: USD/BTC rate served by the mock ``/v1/prices`` endpoint. The fixture
#: balance (69,345 sats) @ this rate = 6,726.465 cents → floors to
#: 6,726 (the oracle's documented ROUND_FLOOR policy).
PRICE_USD = 97_000.0
EXPECTED_USD_CENTS = 6_726

_BALANCE_ENVELOPE = validate_payload(GET_BALANCE_JSON)


def _balance_table(
    *,
    usd: float = PRICE_USD,
    prices_state: dict[str, Any] | None = None,
    make_oracle: Callable[[EsploraClient], Any] | None = None,
    funded: bool = True,
    scan_gate: Any | None = None,
) -> tuple[DispatchTable, Store, EsploraClient, list[httpx.Request]]:
    """Store-backed dispatch table over a mock chain that ALSO serves
    ``/v1/prices`` (scan/tip shapes come from the e2e fixtures).

    ``prices_state`` is a mutable injection point (``{"fail": True}``
    flips the endpoint to 500 mid-test); ``make_oracle`` builds the
    wired oracle over the same client (disabled / custom-TTL seams);
    ``funded=False`` leaves the wallet empty; ``scan_gate`` passes a
    startup-scan gate (the backend-hold seam).
    """
    prices_state = prices_state if prices_state is not None else {}
    recorded: list[httpx.Request] = []
    addr0, addr1 = derive_fixture_addresses(2)
    utxos = {addr0: UTXOS_ADDR0, addr1: UTXOS_ADDR1} if funded else {}
    scan = _scan_handler(recorded, utxos_by_addr=utxos)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/prices"):
            recorded.append(request)
            if prices_state.get("fail"):
                return httpx.Response(500, json=None)
            return httpx.Response(200, json={"time": 1_700_000_000, "USD": usd})
        return scan(request)

    client = _mock_client(handler)
    store = Store.memory()
    wd = WalletDescriptor.from_key(ZPUB)
    wallet = store.create_wallet("default", wd.descriptor)
    store.set_active_wallet(wallet.id)
    store.set_setting(GAP_LIMIT_SETTING, str(TEST_GAP))
    table = build_dispatch_table(
        store,
        wallet,
        wd.parsed,
        client,
        lambda: scan_wallet(store, client, wallet),
        price_oracle=None if make_oracle is None else make_oracle(client),
        scan_gate=scan_gate,
    )
    return table, store, client, recorded


def _balance(table: DispatchTable) -> dict[str, object]:
    return table[IntentName.GET_BALANCE](_BALANCE_ENVELOPE)


def _price_requests(recorded: list[httpx.Request]) -> int:
    return sum(1 for r in recorded if r.url.path.endswith("/v1/prices"))


# ------------------------------------------------------------- the handler


def test_balance_gets_usd_keys_when_price_available() -> None:
    table, store, client, recorded = _balance_table()
    try:
        result = _balance(table)
    finally:
        client.close()
        store.close()
    assert "error" not in result
    assert result["total_sats"] == EXPECTED_TOTAL  # sats keys unchanged
    assert result["usd_total_cents"] == EXPECTED_USD_CENTS  # floor-to-cent
    assert result["btc_usd"] == PRICE_USD
    assert "rate_stale" not in result  # fresh rate: no stale marker
    assert _price_requests(recorded) == 1


def test_balance_zero_sats_convert_to_honest_zero_not_absent() -> None:
    """An empty wallet with a live rate renders $0.00 — a real value,
    not a price failure."""
    table, store, client, _recorded = _balance_table(funded=False)
    try:
        result = _balance(table)
    finally:
        client.close()
        store.close()
    assert result["total_sats"] == 0
    assert result["usd_total_cents"] == 0


def test_balance_price_outage_is_sats_only_never_an_error() -> None:
    """500 on ``/v1/prices`` with no cache to degrade to: the USD keys
    are simply ABSENT — the answer itself stays whole."""
    table, store, client, _recorded = _balance_table(prices_state={"fail": True})
    try:
        result = _balance(table)
    finally:
        client.close()
        store.close()
    assert "error" not in result
    assert result["total_sats"] == EXPECTED_TOTAL
    assert "usd_total_cents" not in result
    assert "btc_usd" not in result


def test_balance_disabled_oracle_is_sats_and_never_fetches() -> None:
    """LOCALWALLET_PRICE_ENABLED=0 (ConfigDisabled): sats-only, no error,
    and the oracle is never called at all."""
    table, store, client, recorded = _balance_table(
        make_oracle=lambda client_: PriceOracle(client_, enabled=False)
    )
    try:
        result = _balance(table)
    finally:
        client.close()
        store.close()
    assert "error" not in result
    assert result["total_sats"] == EXPECTED_TOTAL
    assert "usd_total_cents" not in result
    assert _price_requests(recorded) == 0


def test_balance_backend_hold_stands_down_the_price_fetch() -> None:
    """ADR-0022 amendment 1 (security review F1): while the backend
    choice is HELD (``awaiting_backend``) the balance answer is
    cache-served with ZERO chain calls — the best-effort price fetch
    stands down with the lazy scan, it must not probe a server the user
    never picked."""
    table, store, client, recorded = _balance_table(
        scan_gate=StartupScan(enabled=True, deferred=True)
    )
    try:
        result = _balance(table)
    finally:
        client.close()
        store.close()
    assert "error" not in result
    assert result["freshness"] == "stale"  # cache-served, honestly flagged
    assert "usd_total_cents" not in result
    assert recorded == []  # THE leak pin: nothing reached the chain


def test_balance_capability_absent_backend_keys_absent_without_fetch() -> None:
    """A backend without the price capability is a PERMANENT outage:
    PriceUnavailableError, nothing fetched, nothing fabricated."""

    class _NoPriceFeed:
        supports_price = False

        def __init__(self) -> None:
            self.gets = 0

        def get_json(self, *_a: object, **_k: object) -> object:
            self.gets += 1  # must never happen — asserted below
            return {}

    feed = _NoPriceFeed()
    table, store, client, _recorded = _balance_table(
        make_oracle=lambda _client_: PriceOracle(feed)  # type: ignore[arg-type]
    )
    try:
        result = _balance(table)
    finally:
        client.close()
        store.close()
    assert "error" not in result
    assert "usd_total_cents" not in result
    assert "btc_usd" not in result
    assert feed.gets == 0


def test_balance_exploding_oracle_still_answers_sats_only() -> None:
    """Containment pin: even a BUG in the price path (neither expected
    failure) may not fail or pollute the balance answer."""

    class _ExplodingOracle:
        def fresh(self) -> Any:
            raise RuntimeError("unexpected price-path bug")

        def sats_to_usd(self, _sats: int, _rate: Any) -> int:  # pragma: no cover
            raise AssertionError("unreachable")

    table, store, client, _recorded = _balance_table(
        make_oracle=lambda _client_: _ExplodingOracle()
    )
    try:
        result = _balance(table)
    finally:
        client.close()
        store.close()
    assert "error" not in result
    assert result["total_sats"] == EXPECTED_TOTAL
    assert "usd_total_cents" not in result


def test_balance_stale_rate_served_with_age_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0011 ladder on the balance path: warm cache past TTL + failing
    endpoint → stale-but-served, still marked ``rate_stale`` + ``rate_age_s``."""
    from localwallet.chain import price as price_module

    clock = {"now": 1_000.0}
    monkeypatch.setattr(price_module, "_now", lambda: clock["now"])
    state: dict[str, Any] = {}
    table, store, client, _recorded = _balance_table(
        prices_state=state,
        make_oracle=lambda client_: PriceOracle(client_, ttl_s=0.000001),
    )
    try:
        first = _balance(table)
        assert first["usd_total_cents"] == EXPECTED_USD_CENTS
        assert "rate_stale" not in first  # fresh: no marker

        state["fail"] = True
        clock["now"] = 2_000.0
        second = _balance(table)
        assert "error" not in second
        assert second["usd_total_cents"] == EXPECTED_USD_CENTS  # cached rate
        assert second["rate_stale"] is True
        assert second["rate_age_s"] == 1_000  # age from the injected clock
    finally:
        client.close()
        store.close()


# ------------------------------------------------------------- the render


def _render(result: dict[str, object]) -> list[str]:
    lines: list[str] = []
    _print_balance(result, lines.append)
    return lines


_BASE_RESULT: dict[str, object] = {
    "confirmed_sats": 123_450_000,
    "unconfirmed_sats": 0,
    "total_sats": 123_450_000,
    "addresses_scanned": 2,
    "tip_height": 900_000,
    "freshness": "fresh",
}


def test_print_balance_renders_usd_line_only_when_keys_present() -> None:
    without = _render(dict(_BASE_RESULT))
    assert not any("≈" in line for line in without)  # sats-only: no fiat line

    with_usd = _render({**_BASE_RESULT, "usd_total_cents": 1_234_567, "btc_usd": 97_000.0})
    assert len(with_usd) == len(without) + 1
    # Cents verbatim from the result ($12,345.67), rate thousands-separated
    # exactly like the send card (``_card_rate``).
    assert "≈ $12,345.67 · @ $97,000/BTC" in with_usd


def test_print_balance_usd_line_marks_stale_rate_with_age() -> None:
    lines = _render(
        {
            **_BASE_RESULT,
            "usd_total_cents": 1_234_567,
            "btc_usd": 97_000.0,
            "rate_stale": True,
            "rate_age_s": 1_000,
        }
    )
    # The send card's stale wording; the now-untrusted rate figure is not shown.
    assert "≈ $12,345.67 · rate age 1000s · stale" in lines


def test_print_balance_error_result_unchanged() -> None:
    lines = _render({"error": "chain_unavailable", "detail": "backend down"})
    assert len(lines) == 1
    assert "chain unavailable" in lines[0]

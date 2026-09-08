"""Tests for the Phase 4 chain-backend switch (TCK-P4-002, ADR-0018).

``Settings.chain_base_url`` (``LOCALWALLET_CHAIN_BASE_URL``) is the single
config selection point: when set, EVERY EsploraClient-mediated call (address
txs/utxos, tip, fees, price, broadcast) hits it, and — critically — zero
requests reach the public default when self-hosted. When unset, the legacy
``Settings.esplora_base_url`` public default (ADR-0003) is preserved.

All HTTP traffic is served by ``httpx.MockTransport`` — no real network.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.app import build_dispatch_table
from localwallet.chain.config import ChainConfig
from localwallet.chain.esplora import EsploraClient
from localwallet.config import Settings
from localwallet.protocol import IntentName, validate_payload
from localwallet.store import Store
from localwallet.wallet import GAP_LIMIT_SETTING, WalletDescriptor, scan_wallet
from localwallet.wallet.derivation import derive_addresses

PUBLIC_HOST = "mempool.space"
PUBLIC_BASE = "https://mempool.space/api"
SELF_HOSTED = "http://127.0.0.1:3006"
ADDRESS = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"

# The same fixed fixture mainnet zpub used across the suite (a public key,
# not a secret — derived from the fixed seed in test_wallet_descriptor.py,
# same procedure as the Phase 0 e2e fixtures). Mainnet-only (ADR-0021): the
# wallet layer refuses testnet keys, so the former testnet vpub fixture is
# replaced by the canonical mainnet zpub.
ZPUB: str = (
    "zpub6qh6bF4roUgQtg2fm5SUhRsQFEidwUPPLhS82BDHjtNh2UxmgNfCS8NF4jQoBqNCeEW"
    "BaKyTxcmyBkq3iuZS5Seyz5dWMcwYxaMgpZn4cWQ"
)

RECOMMENDED = {
    "fastestFee": 30,
    "halfHourFee": 25,
    "hourFee": 18,
    "economyFee": 10,
    "minimumFee": 1,
}
PRICES = {"time": 1_700_000_000, "USD": 20_000.0}


# --------------------------------------------------------------------------
# Config selection matrix (pure, no I/O)
# --------------------------------------------------------------------------


def test_unset_uses_public_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCALWALLET_CHAIN_BASE_URL", raising=False)
    monkeypatch.delenv("LOCALWALLET_ESPLORA_BASE_URL", raising=False)
    cfg = ChainConfig.from_settings(Settings.from_env())
    # ADR-0003 behavior preserved when the selector is unset.
    assert cfg.base_url == PUBLIC_BASE


def test_set_uses_self_hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", SELF_HOSTED)
    cfg = ChainConfig.from_settings(Settings.from_env())
    assert cfg.base_url == SELF_HOSTED


def test_chain_base_url_wins_over_esplora_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # The selector is authoritative even when the legacy var is also set.
    monkeypatch.setenv("LOCALWALLET_ESPLORA_BASE_URL", "https://example.com/legacy/api")
    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", SELF_HOSTED)
    cfg = ChainConfig.from_settings(Settings.from_env())
    assert cfg.base_url == SELF_HOSTED


def test_chain_base_url_surrounding_whitespace_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", f"  {SELF_HOSTED}  ")
    cfg = ChainConfig.from_settings(Settings.from_env())
    assert cfg.base_url == SELF_HOSTED


def test_empty_chain_base_url_falls_back_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", "")
    monkeypatch.setenv("LOCALWALLET_ESPLORA_BASE_URL", "https://example.com/legacy/api")
    cfg = ChainConfig.from_settings(Settings.from_env())
    assert cfg.base_url == "https://example.com/legacy/api"


def test_esplora_base_url_still_works_standalone(monkeypatch: pytest.MonkeyPatch) -> None:
    # Backward compatibility with the existing env var.
    monkeypatch.delenv("LOCALWALLET_CHAIN_BASE_URL", raising=False)
    monkeypatch.setenv("LOCALWALLET_ESPLORA_BASE_URL", "https://example.com/api")
    cfg = ChainConfig.from_settings(Settings.from_env())
    assert cfg.base_url == "https://example.com/api"


@pytest.mark.parametrize(
    "bad",
    ["not-a-url", "ftp://x", "127.0.0.1:3006", "//x", "https://user:pass@host/api"],
)
def test_malformed_chain_base_url_fails_closed_value_free(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    # Fail closed at construction with a value-free error — the malformed
    # URL is never echoed and never causes a mid-request crash.
    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", bad)
    with pytest.raises(ValueError) as excinfo:
        ChainConfig.from_settings(Settings.from_env())
    assert bad not in str(excinfo.value)
    assert "http" in str(excinfo.value)


def test_userinfo_base_url_rejected_at_construction_value_free() -> None:
    # https://user:pass@host would send embedded credentials on every request
    # (violating the "no API keys are used or sent" guarantee); reject it
    # value-free at construction, independent of the settings layer.
    with pytest.raises(ValueError) as excinfo:
        ChainConfig(base_url="https://user:pass@host/api", timeout_s=10.0, max_retries=3)
    # Value-free: the credential/URL is never echoed (the word "userinfo" in
    # the message is fine; the embedded credentials are not).
    assert "user:pass" not in str(excinfo.value)
    assert "https://user" not in str(excinfo.value)
    assert "http" in str(excinfo.value)


def test_userinfo_base_url_rejected_at_from_settings_value_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", "https://user:pass@host/api")
    with pytest.raises(ValueError) as excinfo:
        ChainConfig.from_settings(Settings.from_env())
    assert "user:pass" not in str(excinfo.value)
    assert "https://user" not in str(excinfo.value)
    assert "http" in str(excinfo.value)


def test_whitespace_only_chain_base_url_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # A whitespace-only selection is deliberate intent that strips to nothing;
    # it must NOT silently fall back to the public default — fail closed.
    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", "   ")
    with pytest.raises(ValueError) as excinfo:
        ChainConfig.from_settings(Settings.from_env())
    # Value-free: no URL/whitespace echoed.
    assert "   " not in str(excinfo.value)


def test_malformed_chain_base_url_fails_at_client_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", "not-a-url")
    # EsploraClient() with no explicit base_url flows through the selection;
    # a malformed selection fails at construction, never mid-request.
    with pytest.raises(ValueError):
        EsploraClient()


# --------------------------------------------------------------------------
# Integration-style test double: config-selected base_url routes queries to IT
# --------------------------------------------------------------------------


class ScriptedServer:
    """MockTransport backend recording requests and serving by path."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/blocks/tip"):
            return httpx.Response(200, json=870_000)
        if path.endswith("/utxo"):
            return httpx.Response(200, json=[])
        if path.endswith("/txs"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json=None)


def test_config_selected_base_url_routes_all_queries_to_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Address txs / utxo / tip all hit the configured instance, not the
    public default, when the selection is active."""
    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", SELF_HOSTED)
    server = ScriptedServer()
    client = EsploraClient(
        timeout_s=5.0, max_retries=0, transport=httpx.MockTransport(server.handler)
    )
    try:
        # The client was constructed with NO explicit base_url — it must have
        # resolved through the single selection point.
        assert client._base_url == SELF_HOSTED
        client.get_address_txs(ADDRESS)
        client.get_address_utxos(ADDRESS)
        client.get_tip_height()
    finally:
        client.close()

    assert len(server.requests) == 3
    for req in server.requests:
        assert req.url.host == "127.0.0.1", req.url
        assert req.url.port == 3006
        assert "mempool.space" not in req.url.host
    assert server.requests[0].url.path == f"/address/{ADDRESS}/txs"
    assert server.requests[1].url.path == f"/address/{ADDRESS}/utxo"
    assert server.requests[2].url.path == "/blocks/tip"


# --------------------------------------------------------------------------
# Zero public calls when self-hosted: the full handler path
# --------------------------------------------------------------------------


class AssertionTransport:
    """Transport that RAISES if any request reaches the public default host.

    Same pattern as the P4-001 loopback tests in test_node_detect.py: the
    transport proves zero requests to the forbidden host by failing loudly.
    """

    def __init__(self) -> None:
        self.seen_hosts: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen_hosts.append(request.url.host)
        if request.url.host == PUBLIC_HOST:
            raise AssertionError(f"request reached public default while self-hosted: {request.url}")
        path = request.url.path
        if path.endswith("/blocks/tip"):
            return httpx.Response(200, json=870_000)
        if path.endswith("/v1/fees/recommended"):
            return httpx.Response(200, json=RECOMMENDED)
        if path.endswith("/v1/prices"):
            return httpx.Response(200, json=PRICES)
        parts = path.rstrip("/").split("/")
        kind = parts[-1] if parts else ""
        addr0 = derive_addresses(WalletDescriptor.from_key(ZPUB).parsed, 0, 0, 1)[0].address
        if kind == "utxo":
            # Fund the first receive address with one 100_000-sat UTXO so the
            # create_tx path can select coins (proving fees+price fire).
            return httpx.Response(200, json=[{"txid": "d" * 64, "vout": 0, "value": 100_000,
                                              "status": {"confirmed": True}}]
                                  if request.url.path.endswith(f"/{addr0}/utxo") else [])
        if kind == "txs":
            # Chain-truthful mirror (TCK-SCAN-001): the scan only fetches
            # /utxo for addresses with history, so the funded address must
            # serve its funding transaction here.
            if request.url.path.endswith(f"/{addr0}/txs"):
                return httpx.Response(200, json=[{
                    "txid": "d" * 64,
                    "vout": [{"scriptpubkey_address": addr0, "value": 100_000}],
                    "status": {"confirmed": True},
                }])
            return httpx.Response(200, json=[])
        return httpx.Response(404, json=None)


def _self_hosted_table(monkeypatch: pytest.MonkeyPatch) -> tuple[
    dict[IntentName, Any], Store, Any, EsploraClient, AssertionTransport
]:
    """Build the wired dispatch table over a self-hosted, assertion client."""
    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", SELF_HOSTED)
    transport = AssertionTransport()
    client = EsploraClient(
        timeout_s=5.0, max_retries=0, transport=httpx.MockTransport(transport.handler)
    )
    assert client._base_url == SELF_HOSTED  # selection active
    store = Store.memory()
    wd = WalletDescriptor.from_key(ZPUB)
    wallet = store.create_wallet("default", wd.descriptor)
    store.set_active_wallet(wallet.id)
    store.set_setting(GAP_LIMIT_SETTING, "2")
    table = build_dispatch_table(
        store, wallet, wd.parsed, client, lambda: scan_wallet(store, client, wallet)
    )
    return table, store, wallet, client, transport


def test_zero_public_calls_when_self_hosted_balance_fees_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full wired handler path (balance/address queries + fees + price)
    runs with zero requests to the public default when self-hosted."""
    table, store, _wallet, client, transport = _self_hosted_table(monkeypatch)
    try:
        # balance → lazy scan → address txs/utxo + tip (all to self-hosted).
        bal = table[IntentName.GET_BALANCE](
            validate_payload('{"v": 0, "intent": "get_balance", "params": {}}')
        )
        assert "error" not in bal
        assert bal["confirmed_sats"] == 100_000  # the funded UTXO
        assert "tip_height" in bal

        # create_tx → fee estimator + price oracle + change/recipient selection
        # (fees and price are wired over the SAME config-selected client).
        recipient = derive_addresses(WalletDescriptor.from_key(ZPUB).parsed, 0, 9, 1)[0].address
        create = table[IntentName.CREATE_TX](
            validate_payload(
                {
                    "v": 0,
                    "intent": "create_tx",
                    "params": {"recipient": recipient, "amount_sats": 60_000},
                }
            )
        )
        assert "error" not in create, create
        assert create["amount_sats"] == 60_000
    finally:
        client.close()
        store.close()

    assert transport.seen_hosts, "expected at least one request"
    # The self-hosted host was used; the public default was never reached.
    assert PUBLIC_HOST not in transport.seen_hosts
    assert set(transport.seen_hosts) == {"127.0.0.1"}

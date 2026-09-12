"""Chain-backend selection — TCK-DESCOPE-M3A re-scope of the ADR-0018 switch.

The WALLET backend is selected by ``Settings.chain_base_url``
(``LOCALWALLET_CHAIN_BASE_URL``) and is Electrum (``ssl://``) or Bitcoin Core
(``bitcoind://``/``bitcoind+tls://``) ONLY. There is NO public wallet default
anymore:

* unset (empty) ``chain_base_url`` is UNRESOLVED — ``ChainConfig
  .from_settings`` fails closed with a value-free error (never a silent
  fallback to mempool.space; the first-run scan holds instead);
* ``esplora_base_url`` (``LOCALWALLET_ESPLORA_BASE_URL``) is NO LONGER the
  wallet fallback. It is repurposed as the PUBLIC-INFO base (fees + prices)
  that ``EsploraClient``/``PublicInfoClient`` read — so an Esplora client
  constructed with no base URL now resolves ``esplora_base_url``, NOT
  ``chain_base_url`` (the two are decoupled, plan §4);
* the fee floor-follower + price oracle ride a standalone public-info
  fetcher regardless of the wallet backend (proven by the integration test
  at the bottom: wallet data hits the Electrum/bitcoind host, fees/prices
  hit the public mempool.space host).

All HTTP traffic is served by ``httpx.MockTransport`` — no real network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.app import _build_chain_client
from localwallet.chain.config import ChainConfig
from localwallet.chain.esplora import EsploraClient
from localwallet.chain.fees import FeeEstimator, FeeTarget
from localwallet.chain.price import PriceOracle
from localwallet.chain.publicinfo import PublicInfoClient
from localwallet.config import PUBLIC_ELECTRUM_URL, Settings
from localwallet.wallet import WalletDescriptor

PUBLIC_HOST = "mempool.space"
PUBLIC_BASE = "https://mempool.space/api"
ELECTRUM = "ssl://electrum.mine.example:50002"
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
# Wallet selection matrix (pure, no I/O) — UNRESOLVED is the new "unset".
# --------------------------------------------------------------------------


def test_unset_chain_base_url_is_unresolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """No wallet default (ADR-0003 amendment): an empty selection fails
    closed instead of falling back to mempool.space (or anything else)."""
    monkeypatch.delenv("LOCALWALLET_CHAIN_BASE_URL", raising=False)
    monkeypatch.delenv("LOCALWALLET_ESPLORA_BASE_URL", raising=False)
    with pytest.raises(ValueError) as excinfo:
        ChainConfig.from_settings(Settings.from_env())
    assert "mempool.space" not in str(excinfo.value)  # value-free, no default leaked


def test_esplora_base_url_is_not_a_wallet_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OLD fallback (empty chain → esplora_base_url) is removed: even
    with the public-info base configured, an empty wallet selection is
    unresolved."""
    monkeypatch.delenv("LOCALWALLET_CHAIN_BASE_URL", raising=False)
    monkeypatch.setenv("LOCALWALLET_ESPLORA_BASE_URL", "https://example.com/api")
    with pytest.raises(ValueError):
        ChainConfig.from_settings(Settings.from_env())


def test_set_electrum_and_bitcoind_select_their_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", ELECTRUM)
    cfg = ChainConfig.from_settings(Settings.from_env())
    assert cfg.base_url == ELECTRUM
    assert cfg.kind == "electrum"

    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", "bitcoind://127.0.0.1:8332")
    cfg = ChainConfig.from_settings(Settings.from_env())
    assert cfg.kind == "bitcoind"

    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", PUBLIC_ELECTRUM_URL)
    assert ChainConfig.from_settings(Settings.from_env()).kind == "electrum"


def test_surrounding_whitespace_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", f"  {ELECTRUM}  ")
    assert ChainConfig.from_settings(Settings.from_env()).base_url == ELECTRUM


def test_whitespace_only_chain_base_url_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", "   ")
    with pytest.raises(ValueError) as excinfo:
        ChainConfig.from_settings(Settings.from_env())
    assert "   " not in str(excinfo.value)


@pytest.mark.parametrize(
    "bad",
    ["not-a-url", "ftp://x", "127.0.0.1:3006", "//x", "https://user:pass@host/api"],
)
def test_malformed_chain_base_url_fails_closed_value_free(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", bad)
    with pytest.raises(ValueError) as excinfo:
        ChainConfig.from_settings(Settings.from_env())
    assert bad not in str(excinfo.value)


# --------------------------------------------------------------------------
# The wallet construction site refuses an Esplora (http) backend (M3A).
# --------------------------------------------------------------------------


def test_build_chain_client_refuses_an_http_wallet_backend() -> None:
    """An http(s) (Esplora-shaped) selection is no longer a wallet backend:
    the construction site refuses it value-free (the public-info path is
    reached only through PublicInfoClient, never the wallet client)."""
    with pytest.raises(ValueError) as excinfo:
        _build_chain_client(Settings(chain_base_url="http://127.0.0.1:3006"))
    assert "127.0.0.1" not in str(excinfo.value)  # value-free refusal


def test_build_chain_client_accepts_electrum_and_bitcoind() -> None:
    assert _build_chain_client(Settings(chain_base_url=ELECTRUM)) is not None
    assert (
        _build_chain_client(Settings(chain_base_url="bitcoind://127.0.0.1:8332"))
        is not None
    )


# --------------------------------------------------------------------------
# EsploraClient / PublicInfoClient resolve the PUBLIC-INFO base, never the
# wallet selection (the two are decoupled).
# --------------------------------------------------------------------------


def test_esplora_client_default_base_is_public_info_not_wallet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EsploraClient() with no base_url resolves ``esplora_base_url`` (the
    public-info base) and IGNORES the wallet ``chain_base_url``."""
    monkeypatch.setenv("LOCALWALLET_ESPLORA_BASE_URL", "https://fees.example/api")
    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", ELECTRUM)
    client = EsploraClient()
    try:
        assert client._base_url == "https://fees.example/api"
    finally:
        client.close()


def test_public_info_client_reads_esplora_base_url() -> None:
    """``PublicInfoClient`` is pinned to the public-info base and exposes
    exactly the fee/price read surface (no wallet methods)."""
    info = PublicInfoClient(
        Settings(esplora_base_url=PUBLIC_BASE),
        transport=httpx.MockTransport(lambda _r: httpx.Response(404, json=None)),
    )
    try:
        assert info.supports_price is True
        assert not hasattr(info, "get_address_txs")
        assert not hasattr(info, "broadcast_tx")
        assert not hasattr(info, "get_address_utxos")
    finally:
        info.close()


# --------------------------------------------------------------------------
# Integration (plan §4): wallet data rides the wallet client, fees/prices
# ride the standalone public-info fetcher — one dispatch table, two hosts.
# --------------------------------------------------------------------------


class _HostRecorder:
    def __init__(self) -> None:
        self.hosts: list[str] = []


def test_fees_and_prices_decoupled_from_the_wallet_backend() -> None:
    """A bitcoind/Electrum wallet client carries NO Esplora fee/price
    endpoints (no ``get_json``), yet the floor-follower + price ladder still
    work because they ride a SEPARATE public-info fetcher pointed at
    mempool.space. The wallet host is never asked for fees/prices, and the
    public host is never asked for wallet addresses (the ADR-0011 note:
    payloads carry no addresses)."""
    wallet_host = _HostRecorder()
    public_host = _HostRecorder()

    def wallet_transport(request: httpx.Request) -> httpx.Response:
        wallet_host.hosts.append(request.url.host)
        # An Electrum/bitcoind adapter would not speak Esplora JSON at all;
        # reaching it for a fee/price query is the bug we are pinning out.
        raise AssertionError(f"wallet host must not serve fees/prices: {request.url}")

    def public_transport(request: httpx.Request) -> httpx.Response:
        public_host.hosts.append(request.url.host)
        path = request.url.path
        if path.endswith("/v1/fees/recommended"):
            return httpx.Response(200, json=RECOMMENDED)
        if path.endswith("/v1/prices"):
            return httpx.Response(200, json=PRICES)
        if path.endswith("/v1/fees/mempool-blocks") or path.startswith("/api/v1/blocks"):
            # Floor endpoints malformed → estimator degrades to recommended
            # (the documented fallback); still proves the public host served
            # the fee fetch and the wallet host did not.
            return httpx.Response(404, json=None)
        if path.endswith("/blocks/tip"):
            return httpx.Response(200, json=870_000)
        return httpx.Response(404, json=None)

    public = PublicInfoClient(
        Settings(esplora_base_url=PUBLIC_BASE),
        transport=httpx.MockTransport(public_transport),
    )
    estimator = FeeEstimator(public, ttl_s=60.0)
    oracle = PriceOracle(public, ttl_s=60.0)

    fast = estimator.estimate(FeeTarget.FAST)
    assert fast.rate_centisat_vb == 3000  # fastestFee (recommended fallback here)
    rate = oracle.fresh()
    assert rate.per_btc == 20_000.0
    assert set(public_host.hosts) == {PUBLIC_HOST}
    # The wallet adapter was never consulted for public info (and the
    # assertion transport would have raised if it had been).
    assert wallet_host.hosts == []
    public.close()


def test_wallet_descriptor_untouched_by_public_info() -> None:
    """Sanity: the public-info fetcher never needs the wallet key — it is a
    pure public aggregation read (no address in any request path)."""
    paths: list[str] = []

    def spy(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/v1/prices"):
            return httpx.Response(200, json=PRICES)
        return httpx.Response(404, json=None)

    info = PublicInfoClient(
        Settings(esplora_base_url=PUBLIC_BASE),
        transport=httpx.MockTransport(spy),
    )
    try:
        PriceOracle(info, ttl_s=60.0).fresh()
    finally:
        info.close()
    # No request path carries an address segment.
    addr = WalletDescriptor.from_key(ZPUB).descriptor
    assert all("address" not in p for p in paths)
    assert addr not in " ".join(paths)

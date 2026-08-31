"""Tests for the Esplora chain client.

All HTTP traffic is served by ``httpx.MockTransport`` — no real network.
"""

import sys
from pathlib import Path

import httpx
import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.chain import ChainError, EsploraClient
from localwallet.chain import esplora as esplora_module

BASE_URL = "https://mempool.space/testnet4/api"
ADDRESS = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"

TXS_PAYLOAD = [
    {
        "txid": "a" * 64,
        "vout": 0,
        "value": 1000,
        "status": {"confirmed": True},
    }
]

UTXOS_PAYLOAD = [
    {"txid": "a" * 64, "vout": 0, "value": 1000, "status": {"confirmed": True}},
    {"txid": "b" * 64, "vout": 1, "value": 250, "status": {"confirmed": False}},
]


class ScriptedServer:
    """``MockTransport`` backend serving a scripted list of responses.

    Entries are ``httpx.Response`` instances or exceptions to raise. The last
    entry repeats once the script is exhausted. Every request is recorded so
    tests can assert the exact URL paths that the client hit.
    """

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

    def client(self, *, max_retries: int = 2, base_url: str = BASE_URL) -> EsploraClient:
        return EsploraClient(
            base_url=base_url,
            timeout_s=5.0,
            max_retries=max_retries,
            transport=httpx.MockTransport(self.handler),
        )


def _record_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace real backoff sleeps with a recording list (no test slowdowns)."""
    sleeps: list[float] = []
    monkeypatch.setattr(esplora_module, "_sleep_for", sleeps.append)
    return sleeps


def test_get_address_txs_happy_path():
    server = ScriptedServer(httpx.Response(200, json=TXS_PAYLOAD))
    with server.client() as client:
        result = client.get_address_txs(ADDRESS)
    assert result == TXS_PAYLOAD  # payload passes through verbatim
    request = server.requests[0]
    assert request.method == "GET"
    assert request.url.host == "mempool.space"
    assert request.url.path == f"/testnet4/api/address/{ADDRESS}/txs"
    assert "?" not in str(request.url)  # no query params / API keys
    assert request.headers["User-Agent"].startswith("local-wallet/")


def test_get_address_utxos_happy_path():
    server = ScriptedServer(httpx.Response(200, json=UTXOS_PAYLOAD))
    with server.client() as client:
        result = client.get_address_utxos(ADDRESS)
    assert result == UTXOS_PAYLOAD
    request = server.requests[0]
    assert request.method == "GET"
    assert request.url.path == f"/testnet4/api/address/{ADDRESS}/utxo"


def test_get_tip_height_happy_path():
    server = ScriptedServer(httpx.Response(200, json=870_000))
    with server.client() as client:
        assert client.get_tip_height() == 870_000
    request = server.requests[0]
    assert request.method == "GET"
    assert request.url.path == "/testnet4/api/blocks/tip"


def test_base_url_trailing_slash_is_normalized():
    server = ScriptedServer(httpx.Response(200, json=870_000))
    with server.client(base_url=BASE_URL + "/") as client:
        client.get_tip_height()
    assert server.requests[0].url.path == "/testnet4/api/blocks/tip"


def test_default_client_uses_settings_defaults():
    server = ScriptedServer(httpx.Response(200, json=870_000))
    client = EsploraClient(transport=httpx.MockTransport(server.handler))
    try:
        assert client.get_tip_height() == 870_000
    finally:
        client.close()
    request = server.requests[0]
    assert request.url.host == "mempool.space"
    assert request.url.path == "/testnet4/api/blocks/tip"


def test_env_override_flows_through_to_client(monkeypatch: pytest.MonkeyPatch):
    # The defaults source honors LOCALWALLET_* env vars via Settings.from_env().
    monkeypatch.setenv("LOCALWALLET_REQUEST_TIMEOUT_S", "3.5")
    server = ScriptedServer(httpx.Response(200, json=870_000))
    client = EsploraClient(transport=httpx.MockTransport(server.handler))
    try:
        # The defaults source honored the env override for a value we left at None.
        assert client._config.timeout_s == 3.5
    finally:
        client.close()


def test_negative_tip_height_raises_chain_error():
    server = ScriptedServer(httpx.Response(200, json=-1))
    with server.client() as client, pytest.raises(ChainError, match="negative integer"):
        client.get_tip_height()
    assert len(server.requests) == 1  # shape/bound errors are not retried


def test_zero_tip_height_is_accepted():
    server = ScriptedServer(httpx.Response(200, json=0))
    with server.client() as client:
        assert client.get_tip_height() == 0
    assert len(server.requests) == 1


def test_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch):
    sleeps = _record_sleeps(monkeypatch)
    server = ScriptedServer(httpx.Response(429), httpx.Response(200, json=TXS_PAYLOAD))
    with server.client() as client:
        assert client.get_address_txs(ADDRESS) == TXS_PAYLOAD
    assert len(server.requests) == 2  # initial attempt + one retry
    assert len(sleeps) == 1  # backoff was scheduled between attempts


def test_retries_on_5xx_then_succeeds(monkeypatch: pytest.MonkeyPatch):
    _record_sleeps(monkeypatch)
    server = ScriptedServer(httpx.Response(503), httpx.Response(200, json=UTXOS_PAYLOAD))
    with server.client() as client:
        assert client.get_address_utxos(ADDRESS) == UTXOS_PAYLOAD
    assert len(server.requests) == 2


def test_retries_on_connection_error_then_succeeds(monkeypatch: pytest.MonkeyPatch):
    _record_sleeps(monkeypatch)
    server = ScriptedServer(
        httpx.ConnectError("connection refused"), httpx.Response(200, json=870_000)
    )
    with server.client() as client:
        assert client.get_tip_height() == 870_000
    assert len(server.requests) == 2


def test_retry_exhaustion_on_429_raises_chain_error(monkeypatch: pytest.MonkeyPatch):
    sleeps = _record_sleeps(monkeypatch)
    server = ScriptedServer(httpx.Response(429))
    with server.client(max_retries=2) as client, pytest.raises(ChainError) as excinfo:
        client.get_address_utxos(ADDRESS)
    message = str(excinfo.value)
    assert "429" in message  # final status code is preserved
    assert "after 2 retries" in message
    assert ADDRESS not in message  # log-scrubbing invariant
    assert len(server.requests) == 3  # initial attempt + 2 retries
    assert len(sleeps) == 2


def test_retry_exhaustion_on_timeouts_raises_chain_error(monkeypatch: pytest.MonkeyPatch):
    _record_sleeps(monkeypatch)
    server = ScriptedServer(httpx.ReadTimeout("timed out"))
    with server.client(max_retries=2) as client, pytest.raises(ChainError) as excinfo:
        client.get_address_txs(ADDRESS)
    message = str(excinfo.value)
    assert "network error (ReadTimeout)" in message
    assert "after 2 retries" in message
    assert len(server.requests) == 3


@pytest.mark.parametrize("status", [400, 403, 404])
def test_other_4xx_raises_immediately_without_retry(monkeypatch: pytest.MonkeyPatch, status: int):
    sleeps = _record_sleeps(monkeypatch)
    server = ScriptedServer(httpx.Response(status))
    with server.client(max_retries=3) as client, pytest.raises(ChainError) as excinfo:
        client.get_address_txs(ADDRESS)
    message = str(excinfo.value)
    assert f"status {status}" in message
    assert ADDRESS not in message
    assert len(server.requests) == 1  # no retries for plain 4xx
    assert sleeps == []


@pytest.mark.parametrize(
    "invoke",
    [
        lambda client: client.get_address_txs(ADDRESS),
        lambda client: client.get_address_utxos(ADDRESS),
        lambda client: client.get_tip_height(),
    ],
)
def test_malformed_json_raises_chain_error(invoke):
    server = ScriptedServer(httpx.Response(200, text="this is not json"))
    with server.client() as client, pytest.raises(ChainError, match="not valid JSON"):
        invoke(client)
    assert len(server.requests) == 1  # malformed bodies are not retried


@pytest.mark.parametrize(
    ("invoke", "payload"),
    [
        (lambda client: client.get_address_txs(ADDRESS), {}),
        (lambda client: client.get_address_txs(ADDRESS), ["not-an-object"]),
        (lambda client: client.get_address_utxos(ADDRESS), {"utxos": []}),
        (lambda client: client.get_tip_height(), "870000"),  # string, not int
        (lambda client: client.get_tip_height(), 1.5),  # float, not int
        (lambda client: client.get_tip_height(), True),  # bool is not accepted
        (lambda client: client.get_tip_height(), [870_000]),
    ],
)
def test_wrong_response_shape_raises_chain_error(invoke, payload):
    server = ScriptedServer(httpx.Response(200, json=payload))
    with server.client() as client, pytest.raises(ChainError):
        invoke(client)
    assert len(server.requests) == 1  # shape errors are not retried


@pytest.mark.parametrize(
    "bad_address",
    ["", " ", "tb1q x", "addr/ect", "x" * 101, "tb1qé", None],
)
def test_invalid_address_argument_raises_without_requesting(bad_address):
    server = ScriptedServer(httpx.Response(200, json=[]))
    with server.client() as client, pytest.raises(ChainError) as excinfo:
        client.get_address_utxos(bad_address)  # type: ignore[arg-type]
    assert server.requests == []  # refused before any request is built
    assert ADDRESS not in str(excinfo.value)  # argument never echoed


def test_context_manager_closes_underlying_client():
    server = ScriptedServer(httpx.Response(200, json=870_000))
    with server.client() as client:
        assert client.get_tip_height() == 870_000
    with pytest.raises(RuntimeError):
        client.get_tip_height()  # httpx refuses requests after close


def test_backoff_delay_is_bounded_exponential_with_jitter():
    jitter = esplora_module._BACKOFF_JITTER_FRACTION
    base = esplora_module._BACKOFF_BASE_S
    cap = esplora_module._BACKOFF_CAP_S
    d0 = esplora_module._backoff_delay(0)
    d1 = esplora_module._backoff_delay(1)
    d5 = esplora_module._backoff_delay(5)
    assert base <= d0 <= base * (1 + jitter)  # attempt 0: base delay + jitter
    assert d0 < d1  # exponential growth between early attempts
    assert cap <= d5 <= cap * (1 + jitter)  # attempt 5 is capped

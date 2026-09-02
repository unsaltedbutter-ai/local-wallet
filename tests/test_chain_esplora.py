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


def test_tip_height_list_shape_returns_max_height():
    # mempool.space has been observed (2026-08) to return a block list here.
    payload = [{"height": 100}, {"height": 105}, {"height": 102, "extra": "x"}]
    server = ScriptedServer(httpx.Response(200, json=payload))
    with server.client() as client:
        assert client.get_tip_height() == 105  # tip = highest known block
    assert len(server.requests) == 1


def test_tip_height_single_element_list():
    server = ScriptedServer(httpx.Response(200, json=[{"height": 300_000}]))
    with server.client() as client:
        assert client.get_tip_height() == 300_000


@pytest.mark.parametrize(
    "payload",
    [
        [],  # empty list
        [{}],  # missing height
        [{"height": "100"}],  # non-int height
        [{"height": -1}],  # negative height
        [{"height": True}],  # bool height
        [123],  # non-dict item
        [{"height": 1}, "junk"],  # non-dict item among valid entries
    ],
)
def test_tip_height_malformed_list_raises_chain_error(payload):
    server = ScriptedServer(httpx.Response(200, json=payload))
    with server.client() as client, pytest.raises(ChainError):
        client.get_tip_height()
    assert len(server.requests) == 1  # shape errors are not retried


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


# --------------------------------------------------- broadcast (TCK-P3-004)

TX_HEX = "0200" + "ab" * 31  # 64 hex chars, even length — guard-valid body
BROADCAST_TXID = "c" * 64


def test_broadcast_tx_happy_path():
    server = ScriptedServer(httpx.Response(200, text=BROADCAST_TXID))
    with server.client() as client:
        assert client.broadcast_tx(TX_HEX) == BROADCAST_TXID
    assert len(server.requests) == 1  # single attempt: POST never retries
    request = server.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/testnet4/api/tx"
    assert request.headers["Content-Type"] == "text/plain"
    assert request.content.decode("ascii") == TX_HEX  # body verbatim


def test_broadcast_tx_strips_response_whitespace():
    server = ScriptedServer(httpx.Response(200, text=BROADCAST_TXID + "\n"))
    with server.client() as client:
        assert client.broadcast_tx(TX_HEX) == BROADCAST_TXID


@pytest.mark.parametrize("status", [400, 404, 500, 503, 429])
def test_broadcast_tx_never_retries_any_failure(monkeypatch: pytest.MonkeyPatch, status: int):
    """The no-retry decision: a POST is not idempotent — a 5xx (or 429)
    after a possibly-successful broadcast must NEVER be retried (a retry
    could double-broadcast). Every failure is a single attempt."""
    sleeps = _record_sleeps(monkeypatch)
    server = ScriptedServer(httpx.Response(status))
    with server.client(max_retries=3) as client, pytest.raises(ChainError) as excinfo:
        client.broadcast_tx(TX_HEX)
    message = str(excinfo.value)
    assert f"status {status}" in message
    assert "broadcast" in message
    assert TX_HEX not in message  # log-scrubbing invariant
    assert len(server.requests) == 1  # exactly one attempt
    assert sleeps == []  # no backoff was ever scheduled


def test_broadcast_tx_connection_error_single_attempt(monkeypatch: pytest.MonkeyPatch):
    """Transport failures are also single-attempt: httpx cannot reliably
    distinguish 'request never sent' from 'response lost after the server
    accepted the transaction' — never gamble the money path on a retry."""
    _record_sleeps(monkeypatch)
    server = ScriptedServer(httpx.ConnectError("connection refused"))
    with server.client(max_retries=3) as client, pytest.raises(ChainError) as excinfo:
        client.broadcast_tx(TX_HEX)
    message = str(excinfo.value)
    assert "network error (ConnectError)" in message
    assert "broadcast" in message
    assert len(server.requests) == 1
    assert TX_HEX not in message


@pytest.mark.parametrize(
    "bad_hex",
    [
        "",  # empty
        "0200",  # below the 64-char floor
        "ab" * 31,  # 62 chars, even but short
        "abc" * 21,  # 63 chars: short AND odd
        "a" * 65,  # odd length (65 chars)
        "0x" + "ab" * 31,  # 0x prefix is not hex
        "ab" * 15 + "gg" + "ab" * 16,  # non-hex characters
        "ab" * 15 + "AB" + "ab" * 16 + "!",  # trailing non-hex junk
        "a b" + "ab" * 31,  # whitespace inside
        "ab" * 50_001,  # over the ~100 KB cap
        None,  # not a string
        123,  # not a string
    ],
)
def test_broadcast_tx_charset_guard_refuses_before_requesting(bad_hex):
    """Charset/parity/length guards fire BEFORE anything is sent, and the
    tx hex is never echoed (value-free)."""
    server = ScriptedServer(httpx.Response(200, text=BROADCAST_TXID))
    with server.client() as client, pytest.raises(ChainError) as excinfo:
        client.broadcast_tx(bad_hex)  # type: ignore[arg-type]
    assert server.requests == []  # refused before any request is built
    message = str(excinfo.value)
    assert "invalid transaction hex" in message
    if isinstance(bad_hex, str) and bad_hex:
        assert bad_hex not in message


def test_broadcast_tx_response_txid_is_revalidated():
    """The response body must re-validate as 64 LOWERCASE hex — a caller
    can never record a bogus id as broadcast (it must be usable verbatim
    with get_tx_status)."""
    cases = {
        "not-a-txid": httpx.Response(200, text="not-a-txid"),
        "uppercase": httpx.Response(200, text="C" * 64),  # lowercase-only contract
        "short": httpx.Response(200, text="c" * 63),
        "long": httpx.Response(200, text="c" * 65),
        "html-error-page-with-200": httpx.Response(200, text="<html>oops</html>"),
        "empty": httpx.Response(200, text=""),
        "json-object": httpx.Response(200, json={"txid": "c" * 64}),
    }
    for name, response in cases.items():
        server = ScriptedServer(response)
        with server.client() as client, pytest.raises(ChainError) as excinfo:
            client.broadcast_tx(TX_HEX)
        message = str(excinfo.value)
        assert "not a valid transaction id" in message, name
        assert len(server.requests) == 1, name
        # value-free: neither the raw body nor the sent hex is echoed
        assert TX_HEX not in message
        for fragment in ("<html>", '{"txid"'):
            assert fragment not in message


# ------------------------------------------------ tx status (TCK-P3-004)

STATUS_TXID = "d" * 64


def test_get_tx_status_confirmed_happy_path():
    payload = {
        "confirmed": True,
        "block_height": 870_000,
        "block_hash": "0" * 64,
        "block_time": 1_700_000_000,
    }
    server = ScriptedServer(httpx.Response(200, json=payload))
    with server.client() as client:
        status = client.get_tx_status(STATUS_TXID)
    assert status == esplora_module.TxStatus(
        txid=STATUS_TXID, confirmed=True, block_height=870_000, block_time=1_700_000_000
    )
    request = server.requests[0]
    assert request.method == "GET"
    assert request.url.path == f"/testnet4/api/tx/{STATUS_TXID}/status"
    assert "?" not in str(request.url)  # no query params / API keys


def test_get_tx_status_unconfirmed_null_fields():
    payload = {"confirmed": False, "block_height": None, "block_hash": None, "block_time": None}
    server = ScriptedServer(httpx.Response(200, json=payload))
    with server.client() as client:
        status = client.get_tx_status(STATUS_TXID)
    assert status == esplora_module.TxStatus(
        txid=STATUS_TXID, confirmed=False, block_height=None, block_time=None
    )


@pytest.mark.parametrize(
    "payload",
    [
        [],  # not an object
        "confirmed",  # not an object
        {},  # missing 'confirmed'
        {"confirmed": "true"},  # non-boolean confirmed
        {"confirmed": 1},  # non-boolean confirmed
        {"confirmed": None},  # non-boolean confirmed
        {"confirmed": True, "block_height": "870000"},  # non-int height
        {"confirmed": True, "block_height": True},  # bool height
        {"confirmed": True, "block_height": -1},  # negative height
        {"confirmed": True, "block_height": 1.5},  # float height
        {"confirmed": True, "block_time": "1700000000"},  # non-int time
        {"confirmed": True, "block_time": -5},  # negative time
    ],
)
def test_get_tx_status_malformed_shape_raises_single_request(payload):
    server = ScriptedServer(httpx.Response(200, json=payload))
    with server.client() as client, pytest.raises(ChainError):
        client.get_tx_status(STATUS_TXID)
    assert len(server.requests) == 1  # shape errors are not retried


@pytest.mark.parametrize(
    "bad_txid",
    ["", "d" * 63, "d" * 65, "D" * 64, "../" + "d" * 61, "d" * 32 + " " + "d" * 31,
     "á" * 64, "d" * 20 + ";" * 44, None, 123],
)
def test_get_tx_status_txid_guard_refuses_before_requesting(bad_txid):
    """The strict lowercase-hex charset is the URL-path injection guard:
    refused BEFORE the URL is constructed; the txid is never echoed."""
    server = ScriptedServer(httpx.Response(200, json={"confirmed": False}))
    with server.client() as client, pytest.raises(ChainError) as excinfo:
        client.get_tx_status(bad_txid)  # type: ignore[arg-type]
    assert server.requests == []  # no URL was ever built
    message = str(excinfo.value)
    assert "invalid txid argument" in message
    if isinstance(bad_txid, str) and bad_txid:
        assert bad_txid not in message  # value-free


def test_get_tx_status_unknown_txid_404_is_value_free_single_request():
    server = ScriptedServer(httpx.Response(404))
    with server.client() as client, pytest.raises(ChainError) as excinfo:
        client.get_tx_status(STATUS_TXID)
    message = str(excinfo.value)
    assert "status 404" in message
    assert STATUS_TXID not in message  # log-scrubbing invariant
    assert len(server.requests) == 1  # plain 4xx: no retry


def test_tx_status_dataclass_is_frozen():
    status = esplora_module.TxStatus(txid=STATUS_TXID, confirmed=False, block_height=None, block_time=None)
    with pytest.raises(AttributeError):
        status.confirmed = True  # type: ignore[misc]

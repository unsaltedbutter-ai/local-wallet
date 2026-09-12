"""TCK-ONB-004 M2 — the Bitcoin Core RPC client against a LOCAL fixture.

Hermetic by construction: a real ``http.server`` on 127.0.0.1 speaks the
minimal Bitcoin Core JSON-RPC protocol (HTTP Basic) with deterministic
scripted responses (plan §2 test section) — no public network in CI, no
live node. Same fixture discipline as M1's ssl stub.

Coverage (ticket gates):

* AUTH MATRIX: constructor user/pass, URL userinfo (percent-decoded), the
  cookie file (explicit path + the documented HOME default + rotation),
  precedence, "no credentials" = the header is OMITTED, a bad 401 refused
  value-free and never retried, the cookie CONTENT never sent when the
  file is oversized/unreadable;
* the mainnet gate (``getblockchaininfo.chain`` must be "main", ADR-0021;
  refused deterministically, not retried) and the capability floor
  (``getnetworkinfo.version`` >= 220000);
* ``scantxoutset`` → the EXACT Esplora ``/utxo`` shape ``scan.py`` parses,
  descriptors built from our OWN scripts only (bare ``raw(<hex>)`` since
  TCK-BACKEND-004 — never keys, never the node wallet), the per-tip
  snapshot cache
  (reuse / descriptor-union extension / tip invalidation / incomplete
  walk fails closed), Decimal-exact satoshi conversion;
* SCAN EQUIVALENCE vs the Esplora mock on one shared scenario — the M1
  methodology, narrowed honestly where the data allows (see the docstring
  of ``test_fetch_scan_bitcoind_matches_where_the_data_allows``:
  unspent-only history is the plan §2 tradeoff, and it is asserted AS
  precisely as documented);
* status confirmations, tip/tip-block, fee mapping (targets + CONSERVATIVE
  + no-feerate warmup answers fail closed; the FeeEstimator native branch
  and the price-oracle capability refusal ride the M1 seam unchanged);
* broadcast: embit txid binding, single-attempt under EVERY failure class,
  server text never echoed;
* transport retry policy consistent with Esplora/Electrum (class names,
  shared backoff, "network error (Class)" surface); value-free errors
  everywhere; ``ChainConfig``/scheme selection (``bitcoind://`` → the
  BitcoindClient, https stays Esplora), the app's probe dispatch, and the
  structural ``ChainClient`` protocol conformance;
* the debugger handoff (2026-09-12): verbose-tx height read from ``height``
  (modern Core) OR the legacy ``blockheight``, scan rows without a height
  read as unconfirmed, verbosity-2 funding-tx requests (prevout mapping),
  and shape refusals carrying ``not-core-shape`` (never the
  network-error collapse). Fixtures serve the shape a REAL Core answers
  for the verbosity actually requested.
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from embit.script import address_to_scriptpubkey
from embit.transaction import Transaction

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet import app as app_module
from localwallet.chain import (
    BitcoindClient,
    ChainClient,
    ChainConfig,
    ChainError,
    ElectrumClient,
    EsploraClient,
    FeeEstimator,
    FeeSource,
    FeeTarget,
    PriceOracle,
    PriceUnavailableError,
    TipBlock,
    TxStatus,
)
from localwallet.chain import bitcoind as bitcoind_module
from localwallet.chain.esplora import NOT_CORE_SHAPE
from localwallet.chain.watch import time_since_last_block
from localwallet.config import Settings
from localwallet.wallet import scan as wallet_scan
from tests.test_chain_electrum import _esplora_scenario, _fresh_plan, _spk_hex
from tests.test_chain_esplora import BROADCAST_TXID, TX_HEX
from tests.test_wallet_scan import _EXTERNAL, ADDRS, TIP, FakeChain, utxo_entry

BH: str = "ab" * 32  # a deterministic 64-hex "bestblockhash"
TIP_TIME: int = 1_700_000_000


class _Close:
    """Scripted action: kill the connection without answering."""


CLOSE = _Close()


def _btc(sats: int) -> float:
    """Sats → the BTC number a Core response would carry (float on the
    wire; the client's parse_float=Decimal pipeline reads the SHORTEST
    REPR token back exactly — which is what makes tiny hostile amounts
    testable: 5e-09 BTC really is half a satoshi)."""
    return sats / 100_000_000


def _addr_script_hex(address: str) -> str:
    return address_to_scriptpubkey(address).data.hex()


_ALL_ADDRS = ADDRS[0] + ADDRS[1]
_SCRIPT_OF = {a: _addr_script_hex(a) for a in _ALL_ADDRS}


class BitcoindFixture:
    """Threaded loopback JSON-RPC HTTP stub serving scripted Core answers.

    ``script`` maps an RPC method to either a callable ``params -> entry``
    or a list of entries consumed per call (the last entry repeats — the
    ScriptedServer convention). An entry may be a result value,
    ``("error", code, message)`` (HTTP 500 + JSON-RPC error envelope),
    ``("status", n)`` (bare HTTP n, non-JSON body — a proxy answer),
    ``("raw", text)`` (HTTP 200 with that exact body) or :data:`CLOSE`.
    ``getblockchaininfo`` / ``getnetworkinfo`` / ``getblockheader`` default
    to a compliant mainnet v22 handshake; ``chain``/``blocks``/``version``
    tune those defaults. ``expect_credentials`` is ``("user", "pass")``,
    ``None`` (require the header to be ABSENT), or a predicate over the
    decoded credential string. Request records carry method/params and a
    HAS-header bool — never any credential value.
    """

    def __init__(
        self,
        tmp_path: Path,
        *,
        script: dict[str, Any] | None = None,
        chain: str = "main",
        blocks: int = TIP,
        version: int = 220_000,
        expect_credentials: Any = None,
        hang: tuple[str, ...] = (),
    ) -> None:
        self.script = dict(script or {})
        self.chain = chain
        self.blocks = blocks
        self.version = version
        self.hang = hang
        self.requests: list[tuple[str, Any]] = []
        self.has_auth: list[bool] = []
        self.counts: Counter[str] = Counter()
        self.unexpected: list[str] = []
        self.connects = 0
        self._index: Counter[str] = Counter()
        if callable(expect_credentials):
            self._check_creds = expect_credentials
        elif expect_credentials is None:
            self._check_creds = lambda creds: creds is None
        else:
            pair = f"{expect_credentials[0]}:{expect_credentials[1]}"
            self._check_creds = lambda creds: creds == pair
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence stderr
                pass

            def do_POST(self) -> None:
                fixture.connects += 1
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                try:
                    message = json.loads(body)
                    method = message["method"]
                    params = message.get("params", [])
                except (ValueError, KeyError):
                    _respond(self, 400, b"bad request body")
                    return
                authed = fixture._authorized(self.headers.get("Authorization"))
                fixture.requests.append((method, params))
                fixture.counts[method] += 1
                if not authed:
                    _respond(self, 401, b"unauthorized")
                    return
                if method in fixture.hang:
                    time.sleep(1.0)  # outlasts any test timeout
                fixture._send(self, fixture._answer(method, params))

        self._http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._http.daemon_threads = True
        self._thread = threading.Thread(target=self._http.serve_forever, daemon=True)
        self._thread.start()

    def _authorized(self, header: str | None) -> bool:
        creds: str | None = None
        if header is not None:
            if header.startswith("Basic "):
                try:
                    creds = base64.b64decode(header[6:], validate=True).decode("ascii")
                except Exception:  # noqa: BLE001 — a broken header is simply "wrong"
                    creds = "\x00broken"  # cannot equal any real pair
            else:
                creds = "\x00nonbasic"
        self.has_auth.append(creds is not None)
        return bool(self._check_creds(creds))

    def _answer(self, method: str, params: list[Any]) -> Any:
        scripted = self.script.get(method)
        if scripted is None:
            if method == "getblockchaininfo":
                return {
                    "chain": self.chain,
                    "blocks": self.blocks,
                    "headers": self.blocks,
                    "bestblockhash": BH,
                    "verificationprogress": 1.0,
                    "initialblockdownload": False,
                }
            if method == "getnetworkinfo":
                return {"version": self.version, "subversion": "/Satoshi:fixture/"}
            if method == "getblockheader":
                return {"height": self.blocks, "time": TIP_TIME}
            self.unexpected.append(method)
            return ("error", -32601, f"Method not found {method}")
        if callable(scripted):
            return scripted(params)
        entry = scripted[min(self._index[method], len(scripted) - 1)]
        self._index[method] += 1
        return entry

    def _send(self, handler: BaseHTTPRequestHandler, entry: Any) -> None:
        if entry is CLOSE:
            try:
                handler.connection.close()  # no answer at all: the client sees EOF
            except OSError:
                pass
            return
        if isinstance(entry, tuple) and entry and entry[0] == "error":
            body = json.dumps(
                {"result": None, "error": {"code": entry[1], "message": entry[2]}, "id": 1}
            ).encode()
            _respond(handler, 500, body)
            return
        if isinstance(entry, tuple) and entry and entry[0] == "status":
            _respond(handler, entry[1], b"proxy exploded, not json")
            return
        if isinstance(entry, tuple) and entry and entry[0] == "raw":
            _respond(handler, 200, entry[1].encode())
            return
        try:
            body = json.dumps({"result": entry, "error": None, "id": 1}, allow_nan=False).encode()
        except ValueError:
            _respond(handler, 500, b'{"result": null, "error": {"code": 1, "message": "unencodable"}, "id": 1}')
            return
        _respond(handler, 200, body)

    @property
    def url(self) -> str:
        return f"bitcoind://127.0.0.1:{self._http.server_address[1]}"

    def url_with(self, userinfo: str) -> str:
        return f"bitcoind://{userinfo}@127.0.0.1:{self._http.server_address[1]}"

    def methods(self) -> list[str]:
        return [m for m, _ in self.requests]

    def stop(self) -> None:
        self._http.shutdown()
        self._http.server_close()


def _respond(handler: BaseHTTPRequestHandler, status: int, body: bytes) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except OSError:
        pass


@pytest.fixture()
def bitcoind(tmp_path: Path) -> Any:
    """Factory for a started fixture server; everything is torn down after."""
    made: list[BitcoindFixture] = []

    def make(**kwargs: Any) -> BitcoindFixture:
        server = BitcoindFixture(tmp_path, **kwargs)
        made.append(server)
        return server

    yield make
    for server in made:
        server.stop()


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No real HOME, no stray env rungs, no real config file: the default
    ``~/.bitcoin/.cookie`` rung and every Settings ladder resolve inside
    tmp_path only."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("LOCALWALLET_CHAIN_BASE_URL", raising=False)
    monkeypatch.delenv("LOCALWALLET_ESPLORA_BASE_URL", raising=False)
    monkeypatch.delenv("LOCALWALLET_RPC_COOKIE_PATH", raising=False)
    monkeypatch.delenv("LOCALWALLET_TLS_VERIFY", raising=False)
    monkeypatch.setattr("localwallet.config.CONFIG_FILE_PATH", tmp_path / "absent.json")
    yield


@pytest.fixture()
def record_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Backoff sleeps are recorded, not slept (same pattern as esplora)."""
    sleeps: list[float] = []
    monkeypatch.setattr(bitcoind_module, "_sleep_for", sleeps.append)
    return sleeps


def _client(server: BitcoindFixture, **kwargs: Any) -> BitcoindClient:
    kwargs.setdefault("timeout_s", 2.0)
    kwargs.setdefault("max_retries", 0)
    return BitcoindClient(base_url=server.url, **kwargs)


# ------------------------------------------------------------------ auth


class TestAuthMatrix:
    def _tip(self, server: BitcoindFixture, **kwargs: Any) -> int:
        with _client(server, **kwargs) as client:
            return client.get_tip_height()

    def test_constructor_user_pass(self, bitcoind: Any) -> None:
        server = bitcoind(expect_credentials=("rpcu", "rpcp"))
        assert self._tip(server, rpc_user="rpcu", rpc_password="rpcp") == TIP
        assert all(server.has_auth)

    def test_url_userpass_percent_decoded(self, bitcoind: Any) -> None:
        server = bitcoind(expect_credentials=("user@name", "p@ss word-ok"))
        # percent-encoding round trip: the URL carried "user%40name" and
        # "p%40ss%20word-ok"; the fixture sees the DECODED pair.
        with BitcoindClient(
            base_url=server.url_with("user%40name:p%40ss%20word-ok"),
            timeout_s=2.0,
            max_retries=0,
        ) as client:
            assert client.get_tip_height() == TIP

    def test_cookie_file_explicit_path(self, bitcoind: Any, tmp_path: Path) -> None:
        cookie = tmp_path / ".cookie"
        cookie.write_text("__cookie__:s3cr3t\n")
        server = bitcoind(expect_credentials=("__cookie__", "s3cr3t"))
        assert self._tip(server, rpc_cookie_path=cookie) == TIP

    def test_default_home_cookie_rung(self, bitcoind: Any, tmp_path: Path) -> None:
        # Settings semantics: an EMPTY rpc_cookie_path means the
        # per-network default under (HOME)/.bitcoin — pinned via the
        # hermetic HOME.
        (tmp_path / ".bitcoin").mkdir()
        (tmp_path / ".bitcoin" / ".cookie").write_text("__cookie__:defaulted")
        server = bitcoind(expect_credentials=("__cookie__", "defaulted"))
        assert self._tip(server, rpc_cookie_path="") == TIP

    def test_settings_env_rung(self, bitcoind: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cookie = tmp_path / "env.cookie"
        cookie.write_text("__cookie__:fromenv")
        monkeypatch.setenv("LOCALWALLET_RPC_COOKIE_PATH", str(cookie))
        server = bitcoind(expect_credentials=("__cookie__", "fromenv"))
        with BitcoindClient(base_url=server.url, timeout_s=2.0, max_retries=0) as client:
            assert client.get_tip_height() == TIP

    def test_url_userpass_wins_over_cookie(self, bitcoind: Any, tmp_path: Path) -> None:
        cookie = tmp_path / ".cookie"
        cookie.write_text("__cookie__:loser")
        server = bitcoind(expect_credentials=("urluser", "urlpass"))
        with BitcoindClient(
            base_url=server.url_with("urluser:urlpass"),
            timeout_s=2.0,
            max_retries=0,
            rpc_cookie_path=cookie,
        ) as client:
            assert client.get_tip_height() == TIP

    def test_no_credentials_omits_the_header(self, bitcoind: Any) -> None:
        # No user/pass, no cookie anywhere (hermetic HOME): the plan's
        # "no credentials needed" = the Authorization header is ABSENT.
        server = bitcoind(expect_credentials=None)
        with _client(server) as client:
            assert client.get_tip_height() == TIP
        assert server.has_auth and not any(server.has_auth)

    def test_unusable_cookie_files_degrade_to_omitted_header(
        self, bitcoind: Any, tmp_path: Path
    ) -> None:
        missing = tmp_path / "gone"
        huge = tmp_path / "huge"
        huge.write_bytes(b"x" * (bitcoind_module._MAX_COOKIE_BYTES + 1))
        no_colon = tmp_path / "nocolon"
        no_colon.write_text("just-a-name-no-colon")
        server = bitcoind(expect_credentials=None)
        for path in (missing, huge, no_colon):
            with _client(server, rpc_cookie_path=path) as client:
                assert client.get_tip_height() == TIP
        assert not any(server.has_auth)

    def test_cookie_rotation_is_honored(self, bitcoind: Any, tmp_path: Path) -> None:
        cookie = tmp_path / ".cookie"
        cookie.write_text("__cookie__:first")
        server = bitcoind(expect_credentials=lambda creds: creds in ("__cookie__:first", "__cookie__:second"))
        with _client(server, rpc_cookie_path=cookie) as client:
            assert client.get_tip_height() == TIP
            cookie.write_text("__cookie__:second")  # Core rewrote it on restart
            assert client.get_tip_height() == TIP

    def test_bad_credentials_refused_value_free(self, bitcoind: Any) -> None:
        server = bitcoind(expect_credentials=("u", "right"))
        with pytest.raises(ChainError) as excinfo:
            self._tip(server, rpc_user="u", rpc_password="wrong")
        message = str(excinfo.value)
        assert "authentication failed" in message
        assert "wrong" not in message and "right" not in message
        assert base64.b64encode(b"u:wrong").decode() not in message
        assert "127.0.0.1" not in message

    def test_auth_failure_is_never_retried(self, bitcoind: Any) -> None:
        server = bitcoind(expect_credentials=("u", "right"))
        with pytest.raises(ChainError):
            self._tip(server, max_retries=3, rpc_user="u", rpc_password="wrong")
        assert server.counts["getblockchaininfo"] == 1

    def test_credentials_never_enter_client_state_errors(
        self, bitcoind: Any, tmp_path: Path
    ) -> None:
        # A non-mainnet node refuses value-free while URL userinfo + cookie
        # are configured: neither secret may ride any error surface.
        cookie = tmp_path / ".cookie"
        cookie.write_text("__cookie__:secretpass")
        server = bitcoind(chain="test")
        url = server.url_with("rpcuser:rpcpassword")
        with BitcoindClient(
            base_url=url, timeout_s=2.0, max_retries=0, rpc_cookie_path=cookie
        ) as client, pytest.raises(ChainError) as excinfo:
            client.get_tip_height()
        message = str(excinfo.value)
        for secret in ("rpcpassword", "secretpass", "rpcuser", "__cookie__"):
            assert secret not in message
        assert base64.b64encode(b"rpcuser:rpcpassword").decode() not in message


# ------------------------------------------------------------- gates


class TestMainnetAndCapabilityGates:
    @pytest.mark.parametrize("chain", ["test", "regtest", "signet", "", 42, None])
    def test_non_mainnet_refused_value_free_not_retried(
        self, bitcoind: Any, chain: Any
    ) -> None:
        server = bitcoind(chain=chain)
        with _client(server, max_retries=2) as client, pytest.raises(ChainError) as excinfo:
            client.get_tip_height()
        assert "does not serve mainnet" in str(excinfo.value)
        assert "127.0.0.1" not in str(excinfo.value)
        assert server.counts["getblockchaininfo"] == 1  # deterministic: no retry
        assert "getnetworkinfo" not in server.counts  # refused AT the chain check

    def test_mainnet_passes_and_gate_runs_once_per_client(self, bitcoind: Any) -> None:
        server = bitcoind()
        with _client(server) as client:
            client.get_tip_height()  # gate (getblockchaininfo) + tip call
            assert server.counts["getblockchaininfo"] == 2
            assert server.counts["getnetworkinfo"] == 1
            client.get_tip_height()  # gate done: one call only
        assert server.counts["getblockchaininfo"] == 3
        assert server.counts["getnetworkinfo"] == 1

    @pytest.mark.parametrize("version", [210_000, 0, True, None, "twenty-two"])
    def test_capability_floor_refuses_old_or_broken_nodes(self, bitcoind: Any, version: Any) -> None:
        server = bitcoind(version=version)
        with _client(server) as client, pytest.raises(ChainError, match="too old") as excinfo:
            client.get_tip_height()
        assert "handshake-capabilities" in str(excinfo.value)
        assert str(version) not in str(excinfo.value) if version is not None else True

    def test_version_at_the_floor_passes(self, bitcoind: Any) -> None:
        server = bitcoind(version=220_000)
        with _client(server) as client:
            assert client.get_tip_height() == TIP

    def test_junk_handshake_objects_fail_closed(self, bitcoind: Any) -> None:
        server = bitcoind(script={"getblockchaininfo": ["junk", {"no": "chain"}]})
        with _client(server) as client, pytest.raises(ChainError):
            client.get_tip_height()


# ------------------------------------------------- scan snapshot + shapes


#: Sentinel for a scan row that OMITS the 'height' field entirely (real
#: nodes have been seen doing exactly that — debugger handoff 2026-09-12).
_NO_HEIGHT: Any = object()


def _unspent_row(
    txid: str, vout: int, address: str, sats: int, height: Any = TIP
) -> dict[str, Any]:
    """One ``scantxoutset`` unspent row. ``height`` rides verbatim: pass an
    int, a digit-string, ``None`` (explicit null) or :data:`_NO_HEIGHT`
    (field absent) to pin the lenient mapping."""
    row: dict[str, Any] = {
        "txid": txid,
        "vout": vout,
        "scriptPubKey": _SCRIPT_OF[address],
        "descriptor": f"desc(raw({_SCRIPT_OF[address]}))",
        "amount": _btc(sats),
    }
    if height is not _NO_HEIGHT:
        row["height"] = height
    return row


def _scan_result(rows: list[dict[str, Any]], height: int = TIP) -> dict[str, Any]:
    return {
        "success": True,
        "complete": True,
        "height": height,
        "bestblockhash": BH,
        "total_amount": 1.0,  # carried but never consumed by this adapter
        "unspents": rows,
    }


class TestScanTranslation:
    def test_descriptors_are_built_from_our_own_scripts(self, bitcoind: Any) -> None:
        address = ADDRS[0][0]
        server = bitcoind(script={"scantxoutset": lambda p: _scan_result([])})
        with _client(server) as client:
            assert client.get_address_utxos(address) == []
        (_method, params), = [(m, x) for m, x in server.requests if m == "scantxoutset"]
        assert params[0] == "start"
        # TCK-BACKEND-004: BARE ``raw(<hex>)`` — the shape Core's own
        # scantxoutset example documents. ``desc(raw(<hex>))`` is Core's
        # OUTPUT form; the input grammar (EvalDescriptorStringOrObject →
        # descriptor::Parse) has no ``desc`` function and REFUSES the whole
        # start request — this pin is the bug's memorial.
        assert params[1] == [f"raw({_spk_hex(address)})"]
        # The watch-only contract: only our script hex rode the request —
        # no key material of any kind exists in it.
        assert all(d.startswith("raw(") and not d.startswith("desc(") for d in params[1])

    def test_utxo_entries_indistinguishable_from_esplora_shape(self, bitcoind: Any) -> None:
        address = ADDRS[0][0]
        rows = [
            _unspent_row("a" * 64, 0, address, 9_000, 800_010),
            _unspent_row("b" * 64, 1, address, 4_000, 800_011),
        ]
        server = bitcoind(script={"scantxoutset": lambda p: _scan_result(rows)})
        with _client(server) as client:
            translated = client.get_address_utxos(address)
        expected = [
            utxo_entry("a" * 64, 0, 9_000, height=800_010),
            utxo_entry("b" * 64, 1, 4_000, height=800_011),
        ]
        assert translated == expected
        assert [wallet_scan._parse_utxo_entry(e, i) for i, e in enumerate(translated)] == [
            wallet_scan._parse_utxo_entry(e, i) for i, e in enumerate(expected)
        ]

    def test_amounts_are_decimal_exact_satoshi(self, bitcoind: Any) -> None:
        address = ADDRS[0][0]
        row = _unspent_row("a" * 64, 0, address, 123_456_789, 800_010)
        server = bitcoind(script={"scantxoutset": lambda p: _scan_result([row])})
        with _client(server) as client:
            (entry,) = client.get_address_utxos(address)
        assert entry["value"] == 123_456_789

    def test_fractional_satoshi_amount_fails_closed(self, bitcoind: Any) -> None:
        # 5e-09 BTC = half a satoshi: no float pipeline may round a
        # non-existent whole satoshi into a balance.
        raw = json.dumps(
            {
                "result": {
                    "success": True,
                    "height": TIP,
                    "unspents": [
                        {
                            "txid": "a" * 64,
                            "vout": 0,
                            "scriptPubKey": _SCRIPT_OF[ADDRS[0][0]],
                            "amount": 0.000000005,
                            "height": TIP,
                        }
                    ],
                },
                "error": None,
                "id": 1,
            }
        )
        server = bitcoind(script={"scantxoutset": [("raw", raw)]})
        with _client(server) as client, pytest.raises(ChainError) as excinfo:
            client.get_address_utxos(ADDRS[0][0])
        assert "whole number of satoshis" in str(excinfo.value)

    def test_unrequested_scripts_are_never_bucketed(self, bitcoind: Any) -> None:
        # A row for a script we never asked about (another tool's scan
        # overlapping the shared node) cannot leak into our snapshot.
        row = {
            "txid": "a" * 64,
            "vout": 0,
            "scriptPubKey": "0014" + "cd" * 20,
            "amount": 1.0,
            "height": TIP,
        }
        server = bitcoind(script={"scantxoutset": lambda p: _scan_result([row])})
        with _client(server) as client:
            assert client.get_address_utxos(ADDRS[0][0]) == []

    @pytest.mark.parametrize(
        "entry",
        [
            {"not": "an object"},
            "junk",
            42,
        ],
    )
    def test_scan_result_shape_junk_fails_closed(self, bitcoind: Any, entry: Any) -> None:
        server = bitcoind(script={"scantxoutset": lambda p: entry})
        with _client(server) as client, pytest.raises(ChainError) as excinfo:
            client.get_address_utxos(ADDRS[0][0])
        # Fix 4: a Core answer in the wrong shape carries the SHAPE class.
        assert excinfo.value.failure_class == NOT_CORE_SHAPE

    @pytest.mark.parametrize("flag", [{"success": False}, {"complete": False}])
    def test_incomplete_walks_fail_closed(self, bitcoind: Any, flag: dict) -> None:
        base = _scan_result([])
        base.update(flag)
        server = bitcoind(script={"scantxoutset": lambda p: base})
        with _client(server) as client, pytest.raises(ChainError, match="did not complete"):
            client.get_address_utxos(ADDRS[0][0])

    def test_malformed_unspent_rows_fail_closed(self, bitcoind: Any) -> None:
        bad_rows = [
            {"txid": "zz" * 32, "vout": 0, "scriptPubKey": "ff", "amount": 1.0, "height": 1},
            {"txid": "a" * 64, "vout": True, "scriptPubKey": "ff", "amount": 1.0, "height": 1},
            {"txid": "a" * 64, "vout": 0, "scriptPubKey": "", "amount": 1.0, "height": 1},
            {"txid": "a" * 64, "vout": 0, "scriptPubKey": "ff", "amount": "1.0", "height": 1},
            {"txid": "a" * 64, "vout": 0, "scriptPubKey": "ff", "amount": -1.0, "height": 1},
        ]
        for row in bad_rows:
            server = bitcoind(script={"scantxoutset": lambda p, row=row: _scan_result([row])})
            with _client(server) as client, pytest.raises(ChainError) as excinfo:
                client.get_address_utxos(ADDRS[0][0])
            assert excinfo.value.failure_class == NOT_CORE_SHAPE  # fix 4
            server.stop()

    @pytest.mark.parametrize("height", [_NO_HEIGHT, None, 0])
    def test_unspent_row_without_a_height_reads_unconfirmed_never_raises(
        self, bitcoind: Any, height: Any
    ) -> None:
        # Fix 2 (debugger handoff): the minutes-class walk SUCCEEDED — an
        # absent/null/zero row height is an honest UNCONFIRMED entry, not
        # a raise that throws the whole scan away.
        address = ADDRS[0][0]
        row = _unspent_row("a" * 64, 0, address, 9_000, height=height)
        server = bitcoind(script={"scantxoutset": lambda p: _scan_result([row])})
        with _client(server) as client:
            (entry,) = client.get_address_utxos(address)
        assert entry == {"txid": "a" * 64, "vout": 0, "value": 9_000, "status": {"confirmed": False}}

    def test_unspent_row_with_string_height_reads_confirmed(self, bitcoind: Any) -> None:
        # A digit-string height is read as the int it means.
        address = ADDRS[0][0]
        row = _unspent_row("a" * 64, 0, address, 9_000, height="800010")
        server = bitcoind(script={"scantxoutset": lambda p: _scan_result([row])})
        with _client(server) as client:
            (entry,) = client.get_address_utxos(address)
        assert entry["status"] == {"confirmed": True, "block_height": 800_010}


class TestSnapshotCache:
    def test_same_address_same_tip_serves_from_cache(self, bitcoind: Any) -> None:
        server = bitcoind(script={"scantxoutset": lambda p: _scan_result([])})
        with _client(server) as client:
            client.get_address_utxos(ADDRS[0][0])
            client.get_address_txs(ADDRS[0][0])  # txs probe rides the same walk
            client.get_address_utxos(ADDRS[0][0])
        assert server.counts["scantxoutset"] == 1

    def test_new_address_extends_the_descriptor_union(self, bitcoind: Any) -> None:
        server = bitcoind(script={"scantxoutset": lambda p: _scan_result([])})
        with _client(server) as client:
            client.get_address_utxos(ADDRS[0][0])
            client.get_address_utxos(ADDRS[0][1])
        walks = [x for m, x in server.requests if m == "scantxoutset"]
        assert len(walks) == 2
        assert walks[1][1] == [
            f"raw({_spk_hex(ADDRS[0][0])})",
            f"raw({_spk_hex(ADDRS[0][1])})",
        ]

    def test_tip_movement_invalidates(self, bitcoind: Any) -> None:
        state = {"h": TIP}
        server = bitcoind(
            script={
                "getblockchaininfo": lambda p: {
                    "chain": "main",
                    "blocks": state["h"],
                    "headers": state["h"],
                    "bestblockhash": BH,
                },
                "scantxoutset": lambda p: _scan_result([], height=state["h"]),
            }
        )
        with _client(server) as client:
            client.get_address_utxos(ADDRS[0][0])
            client.get_address_utxos(ADDRS[0][0])
            assert server.counts["scantxoutset"] == 1
            state["h"] += 1
            client.get_address_utxos(ADDRS[0][0])
        assert server.counts["scantxoutset"] == 2

    def test_height_shrinking_still_invalidates(self, bitcoind: Any) -> None:
        # A lying/mid-reorg scan height never becomes a cache key ABOVE the
        # tip we resolved (min() clamp) — the next probe at the real tip
        # re-walks.
        state = {"tip": TIP}
        server = bitcoind(
            script={
                "getblockchaininfo": lambda p: {
                    "chain": "main",
                    "blocks": state["tip"],
                    "headers": state["tip"],
                    "bestblockhash": BH,
                },
                "scantxoutset": lambda p: _scan_result([], height=TIP + 10),
            }
        )
        with _client(server) as client:
            client.get_address_utxos(ADDRS[0][0])
            client.get_address_utxos(ADDRS[0][0])
        assert server.counts["scantxoutset"] == 2


# ------------------------------------------------- history (tx verbose)


def _verbosity_requested(param: Any) -> int:
    """The verbosity a scripted request actually asked for (``True`` is
    Core's legacy alias for 1; the history path asks for 2)."""
    return 2 if param == 2 else 1


def _core_verbose(
    txid: str,
    *,
    vins: tuple[str, ...] = (_EXTERNAL,),
    vouts: tuple[str, ...] = (),
    confirmed: bool = True,
    height: int = 800_000,
    block_time: int | None = TIP_TIME,
    fee_sats: int | None = 1_500,
    verbosity: int = 2,
) -> dict[str, Any]:
    """A verbose ``getrawtransaction`` as a REAL Bitcoin Core 31 node
    answers the verbosity actually requested.

    Real Core names the confirmation height field ``height`` (the old
    fixture served ``blockheight`` — the legacy outlier — and that union
    shape no node emits is exactly what hid the live scan failure,
    debugger handoff 2026-09-12). The input ``prevout`` objects ride
    ONLY at verbosity 2 (the capability floor guarantees 22+). Addresses
    not encodable at all (the fixture's garbage sender string) fall back
    to ``address``-only — both mapping paths are exercised.
    """

    def spk(address: str) -> dict[str, Any]:
        try:
            return {"hex": _spk_hex(address), "address": address, "type": "witness_v0_keyhash"}
        except Exception:  # noqa: BLE001 — garbage fixture address, name-only form
            return {"address": address, "type": "nonstandard"}

    tx: dict[str, Any] = {
        "txid": txid,
        "vin": [
            {
                "txid": "e" * 64,
                "vout": 0,
                **(
                    {"prevout": {"scriptPubKey": spk(a), "value": 1.0}}
                    if verbosity >= 2
                    else {}
                ),
            }
            for a in vins
        ],
        "vout": [
            {"n": i, "value": 0.0001, "scriptPubKey": spk(a)} for i, a in enumerate(vouts)
        ],
        "confirmations": TIP - height + 1 if confirmed else 0,
    }
    if confirmed:
        tx["blockhash"] = "be" * 32
        tx["height"] = height
        if block_time is not None:
            tx["blocktime"] = block_time
    if fee_sats is not None:
        tx["fee"] = _btc(fee_sats)
    return tx


class TestHistoryTranslation:
    def _serve(self, bitcoind: Any, rows: list[dict[str, Any]], verbose: dict[str, Any]) -> BitcoindFixture:
        return bitcoind(
            script={
                "scantxoutset": lambda p: _scan_result(rows),
                "getrawtransaction": lambda p: verbose,
            }
        )

    def test_tx_entry_indistinguishable_from_esplora_shape(self, bitcoind: Any) -> None:
        address = ADDRS[0][0]
        rows = [_unspent_row("a" * 64, 0, address, 9_000, 800_000)]
        verbose = _core_verbose("a" * 64, vouts=(address,), height=800_000, fee_sats=1_500)
        server = self._serve(bitcoind, rows, verbose)
        with _client(server) as client:
            (translated,) = client.get_address_txs(address)
        esplora = {
            "txid": "a" * 64,
            "vin": [{"prevout": {"scriptpubkey_address": _EXTERNAL}}],
            "vout": [{"scriptpubkey_address": address}],
            "status": {"confirmed": True, "block_height": 800_000, "block_time": TIP_TIME},
            "fee": 1_500,
        }
        # The validated _RawTx records — what the scan ACTUALLY consumes —
        # are equal; the Esplora entry on the right is the truth per ticket.
        assert wallet_scan._parse_tx_entry(translated) == wallet_scan._parse_tx_entry(esplora)

    def test_funding_tx_fetched_once_per_distinct_txid(self, bitcoind: Any) -> None:
        address = ADDRS[0][0]
        rows = [
            _unspent_row("a" * 64, 0, address, 9_000, 800_000),
            _unspent_row("a" * 64, 1, address, 4_000, 800_000),
        ]
        server = self._serve(bitcoind, rows, _core_verbose("a" * 64, vouts=(address,)))
        with _client(server) as client:
            (entry,) = client.get_address_txs(address)
        assert entry["txid"] == "a" * 64
        assert server.counts["getrawtransaction"] == 1

    def test_fee_absent_from_node_is_absent_never_fabricated(self, bitcoind: Any) -> None:
        address = ADDRS[0][0]
        rows = [_unspent_row("a" * 64, 0, address, 9_000, 800_000)]
        verbose = _core_verbose("a" * 64, vouts=(address,), fee_sats=None)
        server = self._serve(bitcoind, rows, verbose)
        with _client(server) as client:
            (entry,) = client.get_address_txs(address)
        assert "fee" not in entry

    def test_input_without_prevout_contributes_nothing(self, bitcoind: Any) -> None:
        # A pruned node omits 'prevout' for coins whose block is gone — the
        # input contributes no address (the coinbase tolerance every
        # adapter shares), no crash, no fabrication.
        address = ADDRS[0][0]
        rows = [_unspent_row("a" * 64, 0, address, 9_000, 800_000)]
        verbose = _core_verbose("a" * 64, vouts=(address,))
        for item in verbose["vin"]:
            del item["prevout"]
        server = self._serve(bitcoind, rows, verbose)
        with _client(server) as client:
            (entry,) = client.get_address_txs(address)
        assert entry["vin"] == [{}]

    def test_mislabeled_verbose_tx_bound_and_refused(self, bitcoind: Any) -> None:
        address = ADDRS[0][0]
        rows = [_unspent_row("a" * 64, 0, address, 9_000, 800_000)]
        server = self._serve(bitcoind, rows, _core_verbose("f" * 64))
        with _client(server) as client, pytest.raises(ChainError) as excinfo:
            client.get_address_txs(address)
        assert "does not match" in str(excinfo.value)
        assert excinfo.value.failure_class == NOT_CORE_SHAPE

    def test_funding_txs_are_requested_at_verbosity_2(self, bitcoind: Any) -> None:
        # Fix 3 (debugger handoff): ``prevout`` is a verbosity-2-ONLY
        # field; the legacy [txid, True] request silently stripped every
        # input's sender address. The request shape AND the mapping it
        # exists for are pinned together.
        address = ADDRS[0][0]
        rows = [_unspent_row("a" * 64, 0, address, 9_000, 800_000)]
        server = self._serve(bitcoind, rows, _core_verbose("a" * 64, vouts=(address,)))
        with _client(server) as client:
            (entry,) = client.get_address_txs(address)
        assert [x for m, x in server.requests if m == "getrawtransaction"] == [["a" * 64, 2]]
        assert entry["vin"] == [{"prevout": {"scriptpubkey_address": _EXTERNAL}}]

    @pytest.mark.parametrize(
        "field",
        [("height", 800_000), ("blockheight", 800_000), ("height", "800000")],
    )
    def test_confirmed_history_height_read_from_height_or_legacy_name(
        self, bitcoind: Any, field: tuple[str, Any]
    ) -> None:
        # Fix 1: modern Core names the verbose-tx height field ``height``;
        # the legacy ``blockheight`` outlier and digit-strings are still
        # accepted (this exact matrix — blockheight-only fixture — is what
        # hid the live "invalid 'blockheight'" scan failure).
        name, value = field
        address = ADDRS[0][0]
        rows = [_unspent_row("a" * 64, 0, address, 9_000, 800_000)]
        verbose = _core_verbose("a" * 64, vouts=(address,))
        del verbose["height"]
        verbose[name] = value
        server = self._serve(bitcoind, rows, verbose)
        with _client(server) as client:
            (entry,) = client.get_address_txs(address)
        assert entry["status"]["block_height"] == 800_000

    @pytest.mark.parametrize(
        "junk", [{}, {"height": None}, {"height": "x"}, {"height": -1}, {"height": True}]
    )
    def test_confirmed_history_without_readable_height_is_shape_refused(
        self, bitcoind: Any, junk: dict[str, Any]
    ) -> None:
        # Fixes 1+4: confirmations > 0 and NOTHING readable in
        # blockheight/height is a broken payload — refused carrying the
        # SHAPE class, never degrading to the network-error collapse.
        address = ADDRS[0][0]
        rows = [_unspent_row("a" * 64, 0, address, 9_000, 800_000)]
        verbose = _core_verbose("a" * 64, vouts=(address,))
        del verbose["height"]
        verbose.update(junk)
        server = self._serve(bitcoind, rows, verbose)
        with _client(server) as client, pytest.raises(ChainError) as excinfo:
            client.get_address_txs(address)
        assert excinfo.value.failure_class == NOT_CORE_SHAPE

    def test_scan_error_payload_never_echoes_server_text(self, bitcoind: Any) -> None:
        # The node's rejection text can carry the queried txid — kind only.
        server = bitcoind(
            script={"scantxoutset": [("error", -34, "scanning transaction outset already started")]}
        )
        with _client(server) as client, pytest.raises(ChainError) as excinfo:
            client.get_address_utxos(ADDRS[0][0])
        assert str(excinfo.value) == "utxo-scan request rejected by the server"
        assert "started" not in str(excinfo.value)


# ---------------------------------------- scan equivalence (the M2 seam)


def _bitcoind_script_from_scenario(txs: dict, utxos: dict) -> dict[str, Any]:
    """The shared scenario, as a REAL Core node could answer it: the UTXO
    set only (confirmed coins; mempool coins do not exist there), funding
    txs by verbose getrawtransaction, everything else invisible."""
    unspent: dict[str, list[dict[str, Any]]] = {}
    verbose: dict[str, dict[str, Any]] = {}
    for address, entries in txs.items():
        for entry in entries:
            for coin in utxos.get(address, []):
                if coin["txid"] != entry["txid"] or not coin["status"]["confirmed"]:
                    continue
                height = entry["status"].get("block_height", TIP)
                rows = unspent.setdefault(_SCRIPT_OF[address], [])
                if not any(r["txid"] == entry["txid"] and r["vout"] == coin["vout"] for r in rows):
                    rows.append(
                        _unspent_row(entry["txid"], coin["vout"], address, coin["value"], height)
                    )
                if entry["txid"] not in verbose:
                    verbose[entry["txid"]] = _core_verbose(
                        entry["txid"],
                        vins=tuple(i["prevout"]["scriptpubkey_address"] for i in entry["vin"]),
                        vouts=tuple(o["scriptpubkey_address"] for o in entry["vout"]),
                        confirmed=True,
                        height=height,
                        block_time=entry["status"].get("block_time", TIP_TIME),
                        fee_sats=entry.get("fee"),
                    )

    def scantx(params: list[Any]) -> dict[str, Any]:
        scripts = [d[len("raw(") : -1] for d in params[1]]
        rows = [r for s in scripts for r in unspent.get(s, [])]
        return _scan_result(rows)

    def gettx(params: list[Any]) -> Any:
        found = verbose.get(params[0])
        if found is None:
            return ("error", -5, f"no information available about transaction {params[0]}")
        return found

    return {
        "scantxoutset": scantx,
        "getrawtransaction": gettx,
    }


def test_fetch_scan_bitcoind_matches_where_the_data_allows(
    bitcoind: Any,
) -> None:
    """THE M2 contract, honestly narrowed: scan.py cannot tell the backends
    apart ON THE DATA BITCOIND CARRIES — and the documented divergence is
    asserted as precisely as the plan defines it.

    Scenario (the M1 test's own): tx_a funds a0 (LATER FULLY SPENT by
    self-tx tx_b), tx_c funds a1 UNCONFIRMED (mempool), tx_d funds a2 and
    its coin lives. Bitcoin Core without a wallet/address-index sees only
    UNSPENT outputs of the confirmed UTXO set (plan §2, OQ-1 default), so:

    * EQUAL where the data allows: the a2/d coin — its UtxoRecord and its
      tx_a... sorry, its tx_d TxRecord (txid, height, block_time, fee,
      direction) is field-by-field IDENTICAL to the Esplora mock's, the
      branch-0 window walk stops at the same place (a2 is the max used
      index on both sides), and the scan seam itself is UNEDITED (same
      ``fetch_scan``, same parsers — zero changes to scan tests or scan.py).
    * DOCUMENTED DIVERGENCE (never silent, the price of option (a)):
      - a0 (fully spent) surfaces NO history: its rows come back unused —
        tx_a and tx_b are absent from the tx_rows;
      - a1's mempool coin is honest absence (not in the UTXO set): tx_c
        absent, the snapshot carries no unconfirmed coin;
      - branch 1's ch0 output was spent off-scenario (the shared utxo map
        lists no ch0 coin): invisible here too, so branch 1 walks a
        shorter window (cursor 20 vs 21) and branch 1's derivation cursor
        differs. Branch 0's cursor is EQUAL (walk length is set by a2).

    Rescan widens descriptor ranges, never a fabricated used/history claim.
    """
    txs, utxos = _esplora_scenario()
    a2 = ADDRS[0][2]

    fake = FakeChain(txs=txs, utxos=utxos, tip=TIP)
    store_e, plan_e = _fresh_plan()
    try:
        with fake.client() as esplora_client:
            over_esplora = wallet_scan.fetch_scan(plan_e, esplora_client)
    finally:
        store_e.close()

    server = bitcoind(script=_bitcoind_script_from_scenario(txs, utxos))
    store_b, plan_b = _fresh_plan()
    try:
        with _client(server) as bitcoind_client:
            over_bitcoind = wallet_scan.fetch_scan(plan_b, bitcoind_client)
    finally:
        store_b.close()

    # ---- EQUAL WHERE THE DATA ALLOWS: the surviving confirmed coin.
    esplora_d_row = next(r for r in over_esplora.tx_rows if r.txid == "d" * 64)
    esplora_d_coin = next(r for r in over_esplora.utxo_snapshot if r.txid == "d" * 64)
    assert [r.txid for r in over_bitcoind.tx_rows] == ["d" * 64]
    assert over_bitcoind.tx_rows[0] == esplora_d_row
    assert over_bitcoind.utxo_snapshot == (esplora_d_coin,)
    assert over_bitcoind.utxo_snapshot[0].address == a2
    summary = over_bitcoind.summary
    assert summary.tip_height == over_esplora.summary.tip_height == TIP
    assert summary.utxo_count == 1 and summary.utxo_value_sats == 9_000
    assert summary.truncated is False
    # Branch 0's window: a2 is used at index 2 on BOTH sides, so the walk
    # stops at the same last index and the derivation cursor agrees.
    assert over_esplora.summary.branches[0].window_last_index == 22
    assert summary.branches[0].window_last_index == 22
    assert summary.branches[0].next_index == over_esplora.summary.branches[0].next_index == 3

    # ---- DOCUMENTED DIVERGENCE, asserted exactly (never silent).
    # Spent/mempool history is invisible: a0 and a1 read as UNUSED (their
    # Esplora rows read as USED), branch 1 saw nothing.
    assert summary.branches[0].used_indices == (2,)  # esplora: (0, 1, 2)
    assert over_esplora.summary.branches[0].used_indices == (0, 1, 2)
    assert summary.branches[1].used_indices == ()  # esplora: (0,)
    assert summary.branches[1].window_last_index == 19  # no coin to extend it
    statuses_b = {row.index: row.status for row in over_bitcoind.address_rows if row.branch == 0}
    statuses_e = {row.index: row.status for row in over_esplora.address_rows if row.branch == 0}
    assert statuses_b[0] == "unused" and statuses_e[0] == "used"  # spent: invisible
    assert statuses_b[1] == "unused" and statuses_e[1] == "used"  # mempool: invisible
    assert statuses_b[2] == statuses_e[2] == "used"  # the coin both can see
    # The mempool coin is honest ABSENCE, not a wrong shape: the snapshot
    # simply does not carry it, and no unconfirmed row exists anywhere.
    assert all(row.confirmed for row in over_bitcoind.utxo_snapshot)
    txids_b = {row.txid for row in over_bitcoind.tx_rows}
    assert txids_b == {"d" * 64}  # tx_a/tx_b (spent) and tx_c (mempool) absent

    # Sync cursors ride the same seam (windows differ, mechanisms do not).
    sync_e = dict(over_esplora.sync_state_updates)
    sync_b = dict(over_bitcoind.sync_state_updates)
    assert sync_b[wallet_scan.TIP_KEY] == sync_e[wallet_scan.TIP_KEY]
    assert sync_b[wallet_scan.CURSOR_KEY] == json.dumps({"0": 23, "1": 20}, sort_keys=True)


# -------------------------------------------------------------- broadcast


class TestBroadcast:
    def test_happy_path_txid_bound(self, bitcoind: Any) -> None:
        server = bitcoind(script={"sendrawtransaction": [BROADCAST_TXID]})
        with _client(server) as client:
            assert client.broadcast_tx(TX_HEX) == BROADCAST_TXID
        assert ("sendrawtransaction", [TX_HEX]) in server.requests

    def test_wrong_txid_refused(self, bitcoind: Any) -> None:
        server = bitcoind(script={"sendrawtransaction": ["f" * 64]})
        with _client(server) as client, pytest.raises(ChainError, match="does not match"):
            client.broadcast_tx(TX_HEX)

    def test_junk_txid_response_refused(self, bitcoind: Any) -> None:
        server = bitcoind(script={"sendrawtransaction": ["ok: 123"]})
        with _client(server) as client, pytest.raises(ChainError, match="malformed 'txid'"):
            client.broadcast_tx(TX_HEX)

    def test_single_attempt_even_on_transport_loss(self, bitcoind: Any) -> None:
        """No-retry parity: a POST-equivalent is never re-sent, whatever
        the failure — even with a retry budget."""
        server = bitcoind(script={"sendrawtransaction": [CLOSE, BROADCAST_TXID]})
        with _client(server, max_retries=3) as client, pytest.raises(ChainError) as excinfo:
            client.broadcast_tx(TX_HEX)
        assert server.counts["sendrawtransaction"] == 1
        assert str(excinfo.value) == "broadcast failed: network error (RemoteDisconnected)"

    def test_node_rejection_single_attempt_never_echoes_text(self, bitcoind: Any) -> None:
        secret = f"18: bad-txns-inputs-missingorspent, txid {BROADCAST_TXID}"
        server = bitcoind(script={"sendrawtransaction": [("error", -27, secret)]})
        with _client(server, max_retries=3) as client, pytest.raises(ChainError) as excinfo:
            client.broadcast_tx(TX_HEX)
        assert server.counts["sendrawtransaction"] == 1
        message = str(excinfo.value)
        assert message == "broadcast request rejected by the server"
        assert BROADCAST_TXID not in message

    def test_bare_proxy_500_is_single_attempt_too(self, bitcoind: Any) -> None:
        server = bitcoind(script={"sendrawtransaction": [("status", 500)]})
        with _client(server, max_retries=2) as client, pytest.raises(ChainError) as excinfo:
            client.broadcast_tx(TX_HEX)
        assert server.counts["sendrawtransaction"] == 1
        assert "status 500" in str(excinfo.value)

    def test_unparseable_hex_never_sent(self, bitcoind: Any) -> None:
        server = bitcoind()
        with _client(server) as client, pytest.raises(ChainError, match="invalid transaction hex"):
            client.broadcast_tx("ff" * 40)  # hex-valid, not a parseable transaction
        assert "sendrawtransaction" not in server.counts

    def test_expected_txid_is_consensus_txid(self) -> None:
        # Cross-check the fixture: what broadcast_tx binds the answer to.
        assert Transaction.parse(bytes.fromhex(TX_HEX)).txid().hex() == BROADCAST_TXID


# ------------------------------------------------------------------ fees


class TestFees:
    def test_target_and_conservative_mode_mapping(self, bitcoind: Any) -> None:
        rates = {1: 0.00004, 2: 0.00002, 6: 0.00001}

        def answer(params: list[Any]) -> dict[str, Any]:
            assert params[1] == "CONSERVATIVE"
            return {"feerate": rates[params[0]], "blocks": params[0]}

        server = bitcoind(script={"estimatesmartfee": answer})
        with _client(server) as client:
            assert client.estimate_fee(FeeTarget.FAST) == 4
            assert client.estimate_fee(FeeTarget.MEDIUM) == 2
            assert client.estimate_fee(FeeTarget.SLOW) == 1
        sends = [p for m, p in server.requests if m == "estimatesmartfee"]
        assert sends == [[1, "CONSERVATIVE"], [2, "CONSERVATIVE"], [6, "CONSERVATIVE"]]

    def test_warmup_answer_without_feerate_fails_closed(self, bitcoind: Any) -> None:
        server = bitcoind(
            script={"estimatesmartfee": [{"blocks": 6, "errors": ["Estimating fees..."]}]}
        )
        with _client(server) as client, pytest.raises(ChainError, match="not a usable rate"):
            client.estimate_fee(FeeTarget.FAST)

    @pytest.mark.parametrize(
        "junk", [0, -1, "0.0001", True, None, {"feerate": "x"}, 100, 5e-07, float("nan")]
    )
    def test_junk_rates_fail_closed(self, bitcoind: Any, junk: Any) -> None:
        server = bitcoind(script={"estimatesmartfee": [{"feerate": junk, "blocks": 2}]})
        with _client(server) as client, pytest.raises(ChainError):
            client.estimate_fee(FeeTarget.MEDIUM)

    def test_estimator_uses_native_source_and_caches(self, bitcoind: Any) -> None:
        server = bitcoind(
            script={"estimatesmartfee": lambda p: {"feerate": 0.00001 * (7 - p[0]), "blocks": p[0]}}
        )
        with _client(server) as client:
            estimator = FeeEstimator(client, ttl_s=30.0)
            fast = estimator.estimate(FeeTarget.FAST)
            slow = estimator.estimate(FeeTarget.SLOW)
            assert (fast.rate_centisat_vb, slow.rate_centisat_vb) == (600, 100)
            assert fast.source is FeeSource.RECOMMENDED  # single-source honesty
            estimator.estimate(FeeTarget.MEDIUM)  # cache hit: no new fetch
        assert server.counts["estimatesmartfee"] == 3  # one refresh total

    def test_estimator_native_failure_fails_closed(self, bitcoind: Any) -> None:
        server = bitcoind(
            script={
                "estimatesmartfee": [
                    {"feerate": 0.00002, "blocks": 1},
                    {"feerate": 0.00002, "blocks": 2},
                    {"feerate": 0.00002, "blocks": 6},
                    {"blocks": 1, "errors": ["warmup"]},
                ]
            }
        )
        with _client(server) as client:
            estimator = FeeEstimator(client, ttl_s=30.0)
            assert estimator.estimate(FeeTarget.FAST).rate_centisat_vb == 200
            estimator.invalidate()
            with pytest.raises(ChainError):
                estimator.estimate(FeeTarget.FAST)  # no lower layer, never fabricated


# ------------------------------------------------------- tip and status


class TestTipAndStatus:
    def test_tip_height(self, bitcoind: Any) -> None:
        server = bitcoind()
        with _client(server) as client:
            assert client.get_tip_height() == TIP

    @pytest.mark.parametrize("blocks", [None, True, "x", -1, {"h": 1}])
    def test_malformed_tip_fails_closed(self, bitcoind: Any, blocks: Any) -> None:
        server = bitcoind(
            script={
                "getblockchaininfo": [{
                    "chain": "main",
                    "blocks": blocks,
                    "bestblockhash": BH,
                }]
            }
        )
        with _client(server) as client, pytest.raises(ChainError):
            client.get_tip_height()

    def test_tip_block_carries_header_time(self, bitcoind: Any) -> None:
        server = bitcoind()
        with _client(server) as client:
            assert client.get_tip_block() == TipBlock(height=TIP, timestamp=TIP_TIME)
        headers = [p for m, p in server.requests if m == "getblockheader"]
        assert headers == [[BH, True]]

    @pytest.mark.parametrize("junk", [{}, {"height": TIP}, {"height": 999, "time": TIP_TIME}])
    def test_tip_block_missing_time_or_height_is_clean_or_refused(
        self, bitcoind: Any, junk: Any
    ) -> None:
        server = bitcoind(
            script={
                "getblockheader": [junk],
                "getblockchaininfo": [{"chain": "main", "blocks": TIP, "bestblockhash": BH}],
            }
        )
        with _client(server) as client:
            if junk.get("height") == TIP:
                # time unavailable → the clean None (never fabricated);
                # height mismatch → fail closed.
                assert client.get_tip_block() == TipBlock(height=TIP, timestamp=None)
            else:
                with pytest.raises(ChainError):
                    client.get_tip_block()

    def test_tx_status_confirmed(self, bitcoind: Any) -> None:
        server = bitcoind(
            script={
                "getrawtransaction": lambda p: _core_verbose(
                    p[0], height=800_000, verbosity=_verbosity_requested(p[1])
                )
            }
        )
        with _client(server) as client:
            assert client.get_tx_status("a" * 64) == TxStatus(
                txid="a" * 64, confirmed=True, block_height=800_000, block_time=TIP_TIME
            )

    @pytest.mark.parametrize(
        "field",
        [("height", 800_000), ("blockheight", 800_000), ("height", "800000")],
    )
    def test_tx_status_height_read_from_height_or_legacy_name(
        self, bitcoind: Any, field: tuple[str, Any]
    ) -> None:
        # Fix 1 rides get_tx_status too (confirm AND watch die together).
        name, value = field
        verbose = _core_verbose("a" * 64, height=800_000, verbosity=1)
        del verbose["height"]
        verbose[name] = value
        server = bitcoind(script={"getrawtransaction": lambda p: verbose})
        with _client(server) as client:
            status = client.get_tx_status("a" * 64)
        assert status.confirmed is True
        assert status.block_height == 800_000

    @pytest.mark.parametrize(
        "junk", [{}, {"height": None}, {"height": "x"}, {"height": -1}, {"height": True}]
    )
    def test_tx_status_confirmed_without_readable_height_is_shape_refused(
        self, bitcoind: Any, junk: dict[str, Any]
    ) -> None:
        # Fix 4: the app's debug line reads class=not-core-shape, never
        # the network-error collapse.
        verbose = _core_verbose("a" * 64, verbosity=1)
        del verbose["height"]
        verbose.update(junk)
        server = bitcoind(script={"getrawtransaction": lambda p: verbose})
        with _client(server) as client, pytest.raises(ChainError) as excinfo:
            client.get_tx_status("a" * 64)
        assert excinfo.value.failure_class == NOT_CORE_SHAPE

    def test_tx_status_mempool_tx_never_reports_first_seen_as_block_time(
        self, bitcoind: Any
    ) -> None:
        verbose = _core_verbose("a" * 64, confirmed=False, verbosity=1)
        verbose["time"] = 1_755_000_000  # Core's first-seen time for a mempool tx
        server = bitcoind(script={"getrawtransaction": lambda p: verbose})
        with _client(server) as client:
            assert client.get_tx_status("a" * 64) == TxStatus(
                txid="a" * 64, confirmed=False, block_height=None, block_time=None
            )

    @pytest.mark.parametrize("junk", [None, True, "0", -1, 1.5, {}, [], {"confirmations": "x"}])
    def test_tx_status_malformed_fails_closed(self, bitcoind: Any, junk: Any) -> None:
        server = bitcoind(script={"getrawtransaction": lambda p: junk})
        with _client(server) as client, pytest.raises(ChainError):
            client.get_tx_status("a" * 64)

    def test_unknown_txid_surfaces_as_chain_error(self, bitcoind: Any) -> None:
        server = bitcoind(
            script={"getrawtransaction": [("error", -5, f"no such transaction {'a' * 64}")]}
        )
        with _client(server) as client, pytest.raises(ChainError) as excinfo:
            client.get_tx_status("a" * 64)
        assert str(excinfo.value) == "tx-status request rejected by the server"

    def test_bad_txid_argument_never_sent(self, bitcoind: Any) -> None:
        server = bitcoind()
        with _client(server) as client, pytest.raises(ChainError, match="invalid txid"):
            client.get_tx_status("A" * 64)  # uppercase violates the contract
        assert server.requests == []  # not even the handshake ran

    def test_bad_address_argument_never_connects(self, bitcoind: Any) -> None:
        server = bitcoind()
        with _client(server) as client, pytest.raises(ChainError, match="invalid address"):
            client.get_address_utxos("bc1q not-an-address")  # Esplora's charset rule
        assert server.requests == []


# ----------------------------------------------------------- retry/timeout


class TestRetryPolicy:
    def test_timeout_retries_with_backoff_then_fails(
        self, bitcoind: Any, record_sleeps: list[float]
    ) -> None:
        server = bitcoind(hang=("getblockchaininfo",))
        with _client(server, timeout_s=0.05, max_retries=2) as client, pytest.raises(
            ChainError
        ) as excinfo:
            client.get_tip_height()
        assert len(record_sleeps) == 2
        assert "network error (TimeoutError)" in str(excinfo.value)

    def test_connection_refused_retries_then_chain_error(
        self, bitcoind: Any, record_sleeps: list[float]
    ) -> None:
        server = bitcoind()
        url = server.url
        server.stop()
        with BitcoindClient(base_url=url, timeout_s=1.0, max_retries=1) as client, pytest.raises(
            ChainError, match=r"network error \(ConnectionRefusedError\)"
        ):
            client.get_tip_height()
        assert len(record_sleeps) == 1

    def test_bare_5xx_retries_then_recovers(self, bitcoind: Any, record_sleeps: list[float]) -> None:
        server = bitcoind(script={"getnetworkinfo": [("status", 503)] * 2 + [{"version": 220_000}]})
        with _client(server, max_retries=3) as client:
            assert client.get_tip_height() == TIP  # gate recovered mid-retry
        assert len(record_sleeps) == 2

    def test_error_envelope_is_not_retried(self, bitcoind: Any, record_sleeps: list[float]) -> None:
        server = bitcoind(script={"estimatesmartfee": [("error", -4, "Estimating fees, wait")], "scantxoutset": lambda p: _scan_result([])})
        with _client(server, max_retries=3) as client:
            with pytest.raises(ChainError, match="fee-estimate request rejected"):
                client.estimate_fee(FeeTarget.FAST)
            assert server.counts["estimatesmartfee"] == 1
            assert not record_sleeps
            # Same client, still usable after a deterministic failure:
            assert client.get_address_utxos(ADDRS[0][0]) == []

    def test_malformed_json_fails_closed(self, bitcoind: Any) -> None:
        server = bitcoind(script={"getnetworkinfo": [("raw", "corrupt{{{")]})
        with _client(server) as client, pytest.raises(ChainError, match="not valid JSON"):
            client.get_tip_height()

    def test_non_rpc_envelope_fails_closed(self, bitcoind: Any) -> None:
        server = bitcoind(script={"getnetworkinfo": [("raw", json.dumps({"nope": 1}))]})
        with _client(server) as client, pytest.raises(ChainError, match="JSON-RPC envelope"):
            client.get_tip_height()

    def test_errors_never_leak_host_or_query(self, bitcoind: Any) -> None:
        address = ADDRS[0][3]
        server = bitcoind(script={"scantxoutset": ["junk"]})
        with _client(server) as client, pytest.raises(ChainError) as excinfo:
            client.get_address_utxos(address)
        message = str(excinfo.value)
        assert address not in message and _spk_hex(address) not in message
        assert "127.0.0.1" not in message


# --------------------------------------------- selection + protocol + probe


class TestSelectionAndProtocol:
    def test_chain_config_kind_matrix(self) -> None:
        for url, kind in [
            ("bitcoind://h", "bitcoind"),
            ("bitcoind://h:8332", "bitcoind"),
            ("bitcoind://127.0.0.1:8332", "bitcoind"),
            ("bitcoind://u:p@h:1", "bitcoind"),
            ("ssl://h", "electrum"),
            ("https://mempool.space/api", "esplora"),
            ("http://127.0.0.1:3006", "esplora"),
        ]:
            assert ChainConfig(base_url=url, timeout_s=5, max_retries=0).kind == kind, url

    @pytest.mark.parametrize(
        "bad",
        [
            "bitcoind://",
            "bitcoind://h:port",
            "bitcoind://h:99999",
            "bitcoind://h/p",
            "bitcoind://h?q=1",
            "bitcoind://u@h",  # half a credential pair is a typo, not a hint
            "bitcoind://:pw@h",
            "bitcoind://u s:p@h",  # whitespace in userinfo
            "bitcoind://u:p@h",  # ← valid; kept OUT of the bad list below
        ][:-1],
    )
    def test_malformed_bitcoind_url_fails_closed_value_free(self, bad: str) -> None:
        with pytest.raises(ValueError) as excinfo:
            ChainConfig(base_url=bad, timeout_s=5, max_retries=0)
        suffix = bad.split("://", 1)[1]
        assert not suffix or suffix not in str(excinfo.value)

    def test_credential_pair_in_url_is_accepted(self) -> None:
        config = ChainConfig(base_url="bitcoind://u:p@h:8332", timeout_s=5, max_retries=0)
        assert config.kind == "bitcoind"

    def test_from_settings_routes_bitcoind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", "bitcoind://127.0.0.1:8332")
        config = ChainConfig.from_settings(Settings.from_env())
        assert config.kind == "bitcoind"
        assert config.base_url == "bitcoind://127.0.0.1:8332"

    def test_esplora_client_refuses_bitcoind_urls(self) -> None:
        with pytest.raises(ValueError, match="BitcoindClient"):
            EsploraClient(base_url="bitcoind://h:1")

    def test_bitcoind_client_refuses_foreign_schemes(self) -> None:
        with pytest.raises(ValueError, match="bitcoind://"):
            BitcoindClient(base_url="https://mempool.space/api", timeout_s=5.0, max_retries=0)
        with pytest.raises(ValueError, match="bitcoind://"):
            BitcoindClient(base_url="ssl://h:50002", timeout_s=5.0, max_retries=0)

    def test_build_chain_client_picks_by_scheme(self) -> None:
        client_b = app_module._build_chain_client(
            Settings(chain_base_url="bitcoind://127.0.0.1:59999", request_timeout_s=5.0, max_retries=0)
        )
        client_x = app_module._build_chain_client(
            Settings(chain_base_url="ssl://127.0.0.1:59999", request_timeout_s=5.0, max_retries=0)
        )
        # TCK-DESCOPE-M3A: http(s) is no longer a wallet backend — the
        # construction site refuses it value-free (public info only).
        try:
            assert isinstance(client_b, BitcoindClient)
            assert isinstance(client_x, ElectrumClient)
            assert (client_b._host, client_b._port) == ("127.0.0.1", 59999)
            with pytest.raises(ValueError):
                app_module._build_chain_client(
                    Settings(chain_base_url="https://mempool.space/api", request_timeout_s=5.0, max_retries=0)
                )
        finally:
            client_b.close()
            client_x.close()

    def test_build_chain_client_default_port_is_mainnet_rpc(self) -> None:
        client = app_module._build_chain_client(
            Settings(chain_base_url="bitcoind://h", request_timeout_s=5.0, max_retries=0)
        )
        try:
            assert client._port == 8332  # ADR-0021: mainnet-only, one default
        finally:
            client.close()

    def test_both_new_clients_structurally_satisfy_chain_client(self, bitcoind: Any) -> None:
        server = bitcoind()
        with (
            BitcoindClient(base_url=server.url, timeout_s=5.0, max_retries=0) as client_b,
            EsploraClient(
                base_url="https://mempool.space/api",
                timeout_s=5.0,
                max_retries=0,
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
            ) as client_e,
        ):
            assert isinstance(client_b, ChainClient)
            assert isinstance(client_e, ChainClient)

    def test_get_json_is_esplora_only(self, bitcoind: Any) -> None:
        server = bitcoind()
        with _client(server) as client:
            assert not hasattr(client, "get_json")  # the plan's capability split

    def test_price_oracle_refuses_bitcoind_without_fetching(self, bitcoind: Any) -> None:
        server = bitcoind()
        with _client(server) as client:
            assert client.supports_price is False
            oracle = PriceOracle(client, ttl_s=60.0, enabled=True)
            with pytest.raises(PriceUnavailableError, match="no price feed"):
                oracle.fresh()
        assert server.requests == []  # refused before ANY network contact

    def test_watch_tip_helper_rides_the_adapter(self, bitcoind: Any) -> None:
        server = bitcoind()
        with _client(server) as client:
            assert time_since_last_block(client, now=TIP_TIME + 300.0) == 300


class TestProbeDispatch:
    """``/setup`` + the settings write gate: ``_probe_chain_backend`` now
    classifies ``bitcoind://`` by the adapter's OWN handshake gate (the
    deliverable-7 small addition)."""

    def test_probe_true_on_a_mainnet_node(self, bitcoind: Any) -> None:
        server = bitcoind()
        settings = Settings(request_timeout_s=2.0, max_retries=0)
        # M3 contract: the canonical URL back (bitcoind:// stays itself).
        assert app_module._probe_chain_backend(server.url, settings) == server.url

    def test_probe_false_wrong_chain(self, bitcoind: Any) -> None:
        server = bitcoind(chain="signet")
        settings = Settings(request_timeout_s=2.0, max_retries=0)
        assert app_module._probe_chain_backend(server.url, settings) is None

    def test_probe_false_auth_refused(self, bitcoind: Any) -> None:
        server = bitcoind(expect_credentials=("u", "right"))
        settings = Settings(request_timeout_s=2.0, max_retries=0)
        assert app_module._probe_chain_backend(server.url, settings) is None  # no creds → 401

    def test_probe_true_through_cookie_ladder(self, bitcoind: Any, tmp_path: Path) -> None:
        cookie = tmp_path / ".cookie"
        cookie.write_text("__cookie__:pw")
        server = bitcoind(expect_credentials=("__cookie__", "pw"))
        settings = Settings(
            request_timeout_s=2.0, max_retries=0, rpc_cookie_path=str(cookie)
        )
        assert app_module._probe_chain_backend(server.url, settings) == server.url

    def test_probe_false_dead_endpoint(self) -> None:
        settings = Settings(request_timeout_s=0.2, max_retries=0)
        assert app_module._probe_chain_backend("bitcoind://127.0.0.1:1", settings) is None

    def test_probe_false_malformed_url(self) -> None:
        settings = Settings(request_timeout_s=2.0, max_retries=0)
        assert app_module._probe_chain_backend("bitcoind://", settings) is None

    def test_backend_kind_bitcoind_scheme(self) -> None:
        kind = app_module._backend_kind(
            Settings(chain_base_url="bitcoind://127.0.0.1:8332"), resolved=True
        )
        assert kind == app_module.BACKEND_KIND_BITCOIND
        # userinfo-carrying selection URLs badge the SAME (scheme-first check)
        assert (
            app_module._backend_kind(
                Settings(chain_base_url="bitcoind://u:p@127.0.0.1:8332"), resolved=True
            )
            == app_module.BACKEND_KIND_BITCOIND
        )
        # https stays Esplora-primary even after M3's autodetect: the
        # Core shape is tried on http:// only (https RPC is inexpressible,
        # M2 scope — the shape probe never pretends otherwise)
        assert (
            app_module._backend_kind(Settings(chain_base_url="https://x.example/api"), resolved=True)
            == app_module.BACKEND_KIND_MEMPOOL
        )
        # bitcoin:// (a typo scheme) is NOT the Core badge
        assert (
            app_module._backend_kind(Settings(chain_base_url="bitcoin://z"), resolved=True)
            != app_module.BACKEND_KIND_BITCOIND
        )


class TestHttpAutodetect:
    """TCK-ONB-004 M3: the AMBIGUOUS ``http://`` rung. The probe answers in
    Core RPC shape FIRST; a win returns the ``bitcoind://`` REWRITE as the
    canonical URL to store — so ``backend_kind``, the client dispatch and
    every badge ride the ONE unchanged scheme seam (the detection adds no
    second kind-plumbing). Ports are never trusted for classification
    (the fixture answers on an ephemeral port, which no heuristic maps to
    Core); the shapes decide. Nothing here touches a real network: the
    fixture is loopback."""

    def test_http_answering_in_core_shape_stores_the_rewrite(self, bitcoind: Any) -> None:
        server = bitcoind()  # open auth (no credentials demanded)
        http_url = "http://" + server.url.partition("://")[2]
        settings = Settings(request_timeout_s=2.0, max_retries=0)
        detected = app_module._probe_chain_backend(http_url, settings)
        assert detected == server.url  # bitcoind://host:port canonical

    def test_http_wrong_chain_refuses_after_both_shapes(self, bitcoind: Any) -> None:
        server = bitcoind(chain="signet")
        http_url = "http://" + server.url.partition("://")[2]
        settings = Settings(request_timeout_s=2.0, max_retries=0)
        # The Core handshake gate refuses non-mainnet AT ENTRY; the Esplora
        # fallback cannot read genesis off the RPC root either → the single
        # value-free None refusal (what was TRIED is named by the caller's
        # line, never by this answer).
        assert app_module._probe_chain_backend(http_url, settings) is None

    def test_http_core_probe_carries_the_credential_overlay(
        self, bitcoind: Any
    ) -> None:
        server = bitcoind(expect_credentials=("rpcu", "rpcp"))
        http_url = "http://" + server.url.partition("://")[2]
        settings = Settings(request_timeout_s=2.0, max_retries=0)
        # No stored login, no cookie here → the Core shape 401s → None
        # (the Esplora fallback answers the same verdict).
        assert app_module._probe_chain_backend(http_url, settings) is None
        auth = app_module._BackendAuth(user="rpcu", password="rpcp")
        assert app_module._probe_chain_backend(http_url, settings, auth) == server.url

    def test_http_no_credentials_overlay_omits_the_header(self, bitcoind: Any) -> None:
        # A server that demands auth CANNOT be satisfied by the omit-flag
        # overlay (that is the honest 401 story); an OPEN server accepts it.
        open_server = bitcoind()
        http_url = "http://" + open_server.url.partition("://")[2]
        settings = Settings(request_timeout_s=2.0, max_retries=0)
        omit = app_module._BackendAuth(omit=True)
        assert (
            app_module._probe_chain_backend(http_url, settings, omit)
            == open_server.url
        )

    def test_http_userinfo_is_never_probed_as_core(self, bitcoind: Any) -> None:
        server = bitcoind(expect_credentials=("rpcu", "rpcp"))
        hostport = server.url.partition("://")[2]
        settings = Settings(request_timeout_s=2.0, max_retries=0)
        # URL-embedded credentials do not reach the Core branch on the
        # stored rung at all (they are refused there — logins ride the
        # dedicated keys), and the Esplora branch fails closed on userinfo
        # at construction WITHOUT a socket: the fixture saw nothing.
        assert (
            app_module._probe_chain_backend(f"http://rpcu:rpcp@{hostport}", settings)
            is None
        )
        assert server.requests == []

"""TCK-ONB-004 M1 — the Electrum-protocol client against a LOCAL fixture.

Hermetic by construction: a real ``ssl``-wrapped socket server on
127.0.0.1 speaks the minimal Electrum 1.4 JSON-lines protocol with
deterministic scripted responses (plan §1 test section) — no public
network in CI. The self-signed cert is the throwaway loopback material
already pinned in ``tests/test_chain_tls.py`` (it authenticates nothing
real and is not a secret in any PROJECT.md sense).

Coverage (ticket gates):
* handshake: ``server.version`` (2 params, list-or-string result) +
  ``server.features`` mainnet proof (testnet genesis refused value-free);
* balance/history/utxo TRANSLATION to the EXACT shapes ``scan.py``
  consumes — pinned hardest by running ``fetch_scan`` against BOTH the
  scan tests' Esplora mock and this adapter over one scenario and
  comparing the ``ScanRecords`` field-by-field (indistinguishable);
* broadcast: txid binding, single-attempt (never retried), arg guards;
* fee mapping (estimatefee → sat/vB, -1 fails closed) + the FeeEstimator
  native branch, and the PriceOracle capability refusal;
* tls_verify semantics on ssl sockets (self-signed refused by default,
  reachable via CA trust, and via the env escape hatch);
* retry/timeout policy consistent with Esplora (kind names, backoff
  sleeps, "network error (Class)" surface); value-free errors everywhere;
* ``ChainConfig`` scheme selection (ssl:// → electrum kind) and the
  structural ``ChainClient`` protocol conformance of both clients.
"""

from __future__ import annotations

import hashlib
import json
import socket
import ssl
import sys
import threading
import time
from collections import Counter
from dataclasses import replace
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
    MAINNET_GENESIS_HASH,
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
    check_backend,
)
from localwallet.chain import electrum as electrum_module
from localwallet.chain.watch import time_since_last_block
from localwallet.config import Settings
from localwallet.store import Store
from localwallet.wallet import scan as wallet_scan
from tests.test_chain_esplora import BROADCAST_TXID, TX_HEX
from tests.test_chain_tls import _SELF_SIGNED_CERT, _SELF_SIGNED_KEY
from tests.test_wallet_scan import _EXTERNAL, ADDRS, TIP, WD, FakeChain, utxo_entry

# ----------------------------------------------------------------- fixture

_TESTNET_GENESIS = "097152f0da5a3bb8196178ff54fb039c1a6c4f742b665e402432269462886e4e"

_SPK_CACHE: dict[str, str] = {}


def _spk_hex(address: str) -> str:
    if address not in _SPK_CACHE:
        _SPK_CACHE[address] = address_to_scriptpubkey(address).data.hex()
    return _SPK_CACHE[address]


def _sh(address: str) -> str:
    """The electrum scripthash convention, computed independently here."""
    script = address_to_scriptpubkey(address).data
    return hashlib.sha256(script).digest()[::-1].hex()


def _tip_header(timestamp: int = 1_700_000_000) -> str:
    """80 bytes of plausible block header: LE Unix time at bytes 68–72."""
    header = bytearray(80)
    header[68:72] = timestamp.to_bytes(4, "little")
    return header.hex()


class _Close:
    """Scripted action: kill the connection without answering."""


CLOSE = _Close()


class ElectrumFixture:
    """Threaded TLS JSON-lines Electrum stub serving scripted answers.

    ``script`` maps a method to either a callable ``params -> result`` or
    a list of entries consumed per call (the last entry repeats — the same
    convention as the Esplora tests' ``ScriptedServer``). An entry may be
    a result value, ``("error", message)``, ``("raw", line)`` (send that
    raw line instead of the answer), or :data:`CLOSE`. ``server.version``
    and ``server.features`` default to a compliant mainnet handshake
    (``features_error=True`` makes features answer with an error).
    """

    def __init__(
        self,
        tmp_path: Path,
        *,
        script: dict[str, Any] | None = None,
        genesis: str = MAINNET_GENESIS_HASH,
        version_result: Any = ("fixture-electrum", "1.4"),
        features_error: bool = False,
        greeting: bytes | None = None,
        hang: tuple[str, ...] = (),
        notify: bool = False,
    ) -> None:
        self.script = dict(script or {})
        self.genesis = genesis
        self.version_result = version_result
        self.features_error = features_error
        self.greeting = greeting
        self.hang = hang
        self.notify = notify
        self.requests: list[tuple[str, list[Any]]] = []
        self.counts: Counter[str] = Counter()
        self.connects = 0
        self.unexpected: list[str] = []
        self._index: Counter[str] = Counter()
        key = tmp_path / "e-key.pem"
        crt = tmp_path / "e-cert.pem"
        key.write_text(_SELF_SIGNED_KEY)
        crt.write_text(_SELF_SIGNED_CERT)
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(8)
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(str(crt), str(key))
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"ssl://127.0.0.1:{self._server.getsockname()[1]}"

    def stop(self) -> None:
        self._stopping.set()
        try:
            self._server.close()
        except OSError:
            pass

    # -- internals ----------------------------------------------------------

    def _accept(self) -> None:
        while not self._stopping.is_set():
            try:
                raw, _addr = self._server.accept()
            except OSError:
                return
            self.connects += 1
            threading.Thread(target=self._serve, args=(raw,), daemon=True).start()

    def _serve(self, raw: socket.socket) -> None:
        try:
            conn = self._ctx.wrap_socket(raw, server_side=True)
        except OSError:
            try:
                raw.close()
            except OSError:
                pass
            return
        try:
            if self.greeting is not None:
                conn.sendall(self.greeting)
            for line in conn.makefile("rb"):
                if not self._handle(conn, json.loads(line)):
                    return
        except (OSError, ValueError):
            pass  # client vanished / junk frame — tests assert client-side
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _answer(self, method: str, params: list[Any]) -> tuple[Any, Any]:
        """Return ``(result, error)`` for one request per the script."""
        if method == "server.version":
            return self.version_result, None
        if method == "server.features":
            if self.features_error:
                return None, {"code": 1, "message": "no features here"}
            return {"genesis_hash": self.genesis, "server_version": "fixture"}, None
        scripted = self.script.get(method)
        if scripted is None:
            self.unexpected.append(method)
            return None, {"code": 1, "message": f"unhandled {method}"}
        if callable(scripted):
            entry = scripted(params)
        else:
            entry = scripted[min(self._index[method], len(scripted) - 1)]
            self._index[method] += 1
        if entry is CLOSE:
            return CLOSE, None
        if isinstance(entry, tuple) and entry and entry[0] == "error":
            return None, {"code": 1, "message": entry[1]}
        return entry, None

    def _handle(self, conn: ssl.SSLSocket, message: dict[str, Any]) -> bool:
        method = message.get("method", "")
        params = message.get("params", [])
        self.requests.append((method, params))
        self.counts[method] += 1
        if method in self.hang:
            time.sleep(1.0)  # outlasts any test timeout; the close unblocks it
            return True
        result, error = self._answer(method, params)
        if result is CLOSE:
            return False
        if self.notify:
            # An id-less new-block notification ahead of the real answer —
            # the client must skip it (plan: no push consumption in v1).
            conn.sendall(
                json.dumps(
                    {"method": "blockchain.headers.updated", "params": [{"height": TIP}]}
                ).encode()
                + b"\n"
            )
        if isinstance(result, tuple) and result and result[0] == "raw":
            conn.sendall((result[1] + "\n").encode())
            return True
        reply = {"id": message.get("id"), "result": result, "error": error}
        conn.sendall((json.dumps(reply) + "\n").encode())
        return True


@pytest.fixture()
def electrum(tmp_path: Path) -> Any:
    """Factory for a started fixture server; everything is torn down after."""
    made: list[ElectrumFixture] = []

    def make(**kwargs: Any) -> ElectrumFixture:
        server = ElectrumFixture(tmp_path, **kwargs)
        made.append(server)
        return server

    yield make
    for server in made:
        server.stop()


@pytest.fixture(autouse=True)
def _tls_off_and_no_host_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Hermetic defaults: the fixture's self-signed cert is UNtrusted, so
    the protocol tests run on the documented escape-hatch rung; a real
    ~/.localwallet/config.json on the test host can never flip knobs here.
    TLS trust itself is pinned explicitly in TestTls below."""
    monkeypatch.setenv("LOCALWALLET_TLS_VERIFY", "0")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr("localwallet.config.CONFIG_FILE_PATH", tmp_path / "absent.json")
    yield


@pytest.fixture()
def record_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Backoff sleeps are recorded, not slept (same pattern as esplora)."""
    sleeps: list[float] = []
    monkeypatch.setattr(electrum_module, "_sleep_for", sleeps.append)
    return sleeps


def _client(server: ElectrumFixture, **kwargs: Any) -> ElectrumClient:
    kwargs.setdefault("timeout_s", 2.0)
    kwargs.setdefault("max_retries", 0)
    return ElectrumClient(base_url=server.url, **kwargs)


# ------------------------------------------------------------- handshake


class TestHandshake:
    def test_version_and_features_sequence(self, electrum: Any) -> None:
        server = electrum(
            script={"blockchain.headers.subscribe": [{"height": TIP, "hex": _tip_header()}]}
        )
        with _client(server) as client:
            assert client.get_tip_height() == TIP
        names = [m for m, _ in server.requests]
        assert names[:2] == ["server.version", "server.features"]
        name, proto = server.requests[0][1]
        assert name.startswith("local-wallet/")
        assert proto == "1.4"

    def test_string_version_result_accepted(self, electrum: Any) -> None:
        server = electrum(
            version_result="ElectrumX 0.14",
            script={"blockchain.headers.subscribe": [{"height": 7}]},
        )
        with _client(server) as client:
            assert client.get_tip_height() == 7

    def test_junk_version_result_refused(self, electrum: Any) -> None:
        server = electrum(version_result={"weird": True})
        with _client(server) as client, pytest.raises(ChainError, match="handshake"):
            client.get_tip_height()

    def test_non_mainnet_genesis_refused_value_free(self, electrum: Any) -> None:
        # ADR-0021: the adapter IS the mainnet gate for env-configured
        # ssl:// backends (no setup probe until M3) — enforced per connect.
        server = electrum(genesis=_TESTNET_GENESIS)
        with _client(server, max_retries=2) as client, pytest.raises(ChainError) as excinfo:
            client.get_tip_height()
        assert "does not serve mainnet" in str(excinfo.value)
        assert _TESTNET_GENESIS not in str(excinfo.value)
        assert "127.0.0.1" not in str(excinfo.value)
        assert server.connects == 1  # deterministic refusal: NOT retried

    def test_features_error_refused(self, electrum: Any) -> None:
        server = electrum(features_error=True)
        with _client(server) as client, pytest.raises(ChainError, match="handshake-features"):
            client.get_tip_height()


class TestEntryProbe:
    """TCK-ONB-004 M3: ``ssl://`` now reaches the /setup + settings entry
    points, whose gate is ``app._probe_chain_backend``. M1's deviation 3
    ("mainnet enforcement lives IN the adapter handshake because M1 exposes
    no setup probe; M3's probe re-adds the check AT ENTRY") is closed here:
    the probe forces the very same handshake, so a non-mainnet server is
    refused AT ENTRY — value-free (the answer is a bare None: no host, no
    hash, no server text)."""

    def test_probe_accepts_mainnet_server_returns_canonical_url(
        self, electrum: Any
    ) -> None:
        server = electrum(
            script={"blockchain.headers.subscribe": [{"height": TIP}]}
        )
        settings = Settings(request_timeout_s=2.0, max_retries=0)
        assert app_module._probe_chain_backend(server.url, settings) == server.url

    def test_probe_refuses_testnet_genesis_at_entry(self, electrum: Any) -> None:
        server = electrum(genesis=_TESTNET_GENESIS)
        settings = Settings(request_timeout_s=2.0, max_retries=0)
        assert app_module._probe_chain_backend(server.url, settings) is None
        assert server.connects >= 1  # the refusal came FROM the handshake


# ---------------------------------------------------- shape translation


def tx_verbose(
    txid: str,
    *,
    vins: tuple[str, ...] = (_EXTERNAL,),
    vouts: tuple[str, ...] = (),
    height: int | None = None,
    block_time: int | None = None,
) -> dict[str, Any]:
    """Electrum 1.4 verbose transaction for the fixture.

    Inputs carry the server-supplied ``addresses`` form (pass-through) and
    outputs the DERIVABLE ``hex`` form (client-side embit translation);
    addresses that are not encodable at all (the fixture's garbage sender
    string) fall back to ``addresses`` — both mapping paths are exercised
    on every tx.
    """
    tx: dict[str, Any] = {
        "txid": txid,
        "vin": [
            {
                "txid": "e" * 64,
                "vout": 0,
                "prevout": {"scriptPubKey": {"addresses": [a]}, "value": 1.0},
            }
            for a in vins
        ],
        "vout": [],
    }
    for i, a in enumerate(vouts):
        try:
            spk: dict[str, Any] = {"hex": _spk_hex(a)}
        except Exception:  # noqa: BLE001 — garbage fixture address, use pass-through form
            spk = {"addresses": [a]}
        tx["vout"].append({"scriptPubKey": spk, "value": 0.0001, "n": i})
    if height is None:
        # mempool: ElectrumX/electrs OMIT the confirmations field entirely
        pass
    else:
        tx["confirmations"] = TIP - height + 1
        tx["blockheight"] = height
        if block_time is not None:
            tx["time"] = block_time
    return tx


class TestTranslation:
    def test_scripthash_convention(self, electrum: Any) -> None:
        address = ADDRS[0][0]
        server = electrum(script={"blockchain.scripthash.get_history": lambda p: []})
        with _client(server) as client:
            client.get_address_txs(address)
        sent = [tuple(p) for m, p in server.requests if m == "blockchain.scripthash.get_history"]
        assert sent == [(_sh(address),)]

    def test_utxo_entries_indistinguishable_from_esplora_shape(self, electrum: Any) -> None:
        address = ADDRS[0][0]
        rows = [
            {"tx_hash": "a" * 64, "tx_pos": 0, "value": 9_000, "height": 800_010},
            {"tx_hash": "c" * 64, "tx_pos": 1, "value": 4_000, "height": 0},  # mempool
        ]
        server = electrum(script={"blockchain.scripthash.listunspent": lambda p: rows})
        with _client(server) as client:
            translated = client.get_address_utxos(address)
        expected = [
            utxo_entry("a" * 64, 0, 9_000, height=800_010),
            utxo_entry("c" * 64, 1, 4_000, confirmed=False),
        ]
        assert translated == expected
        # And the scan parser reads both identically:
        assert [wallet_scan._parse_utxo_entry(e, i) for i, e in enumerate(translated)] == [
            wallet_scan._parse_utxo_entry(e, i) for i, e in enumerate(expected)
        ]

    def test_tx_entry_indistinguishable_from_esplora_shape(self, electrum: Any) -> None:
        address = ADDRS[0][0]
        verbose = tx_verbose("a" * 64, vouts=(address,), height=800_000, block_time=1_700_000_000)
        server = electrum(
            script={
                "blockchain.scripthash.get_history": lambda p: [
                    {"tx_hash": "a" * 64, "height": 800_000, "fee": 1_500}
                ],
                "blockchain.transaction.get": lambda p: verbose,
            }
        )
        with _client(server) as client:
            (translated,) = client.get_address_txs(address)
        esplora = {
            "txid": "a" * 64,
            "vin": [{"prevout": {"scriptpubkey_address": _EXTERNAL}}],
            "vout": [{"scriptpubkey_address": address}],
            "status": {"confirmed": True, "block_height": 800_000, "block_time": 1_700_000_000},
            "fee": 1_500,
        }
        # The validated _RawTx records — what the scan ACTUALLY consumes —
        # are equal; the Esplora mock on the left is the truth per ticket.
        assert wallet_scan._parse_tx_entry(translated) == wallet_scan._parse_tx_entry(esplora)

    def test_unconfirmed_history_entry_maps_like_esplora(self, electrum: Any) -> None:
        address = ADDRS[0][1]
        server = electrum(
            script={
                "blockchain.scripthash.get_history": lambda p: [
                    {"tx_hash": "c" * 64, "height": 0}  # no fee: mempool-only
                ],
                "blockchain.transaction.get": lambda p: tx_verbose("c" * 64, vouts=(address,)),
            }
        )
        with _client(server) as client:
            (translated,) = client.get_address_txs(address)
        assert translated["status"] == {"confirmed": False}
        assert "fee" not in translated  # absent, never fabricated

    def test_malformed_optional_fee_dropped_not_fatal(self, electrum: Any) -> None:
        server = electrum(
            script={
                "blockchain.scripthash.get_history": lambda p: [
                    {"tx_hash": "a" * 64, "height": 800_000, "fee": "junk"}
                ],
                "blockchain.transaction.get": lambda p: tx_verbose("a" * 64, height=800_000),
            }
        )
        with _client(server) as client:
            (entry,) = client.get_address_txs(ADDRS[0][0])
        assert "fee" not in entry

    @pytest.mark.parametrize(
        ("method", "result"),
        [
            ("blockchain.scripthash.get_history", {"not": "a list"}),
            ("blockchain.scripthash.get_history", [{"tx_hash": "zz" * 32, "height": 1}]),
            ("blockchain.scripthash.get_history", [{"tx_hash": "a" * 64, "height": "x"}]),
            ("blockchain.scripthash.listunspent", "junk"),
            (
                "blockchain.scripthash.listunspent",
                [{"tx_hash": "a" * 64, "tx_pos": -1, "value": 1, "height": 5}],
            ),
            (
                "blockchain.scripthash.listunspent",
                [{"tx_hash": "a" * 64, "tx_pos": 0, "value": True, "height": 5}],
            ),
            ("blockchain.transaction.get", [42]),
        ],
    )
    def test_malformed_payloads_fail_closed_value_free(
        self, electrum: Any, method: str, result: Any
    ) -> None:
        address = ADDRS[0][0]
        server = electrum(
            script={
                "blockchain.scripthash.get_history": lambda p: (
                    [{"tx_hash": "a" * 64, "height": 800_000}]
                    if method == "blockchain.transaction.get"
                    else result
                ),
                "blockchain.scripthash.listunspent": lambda p: result,
                "blockchain.transaction.get": lambda p: result,
            }
        )
        with _client(server, max_retries=3) as client, pytest.raises(ChainError) as excinfo:
            if method == "blockchain.scripthash.listunspent":
                client.get_address_utxos(address)
            else:
                client.get_address_txs(address)
        message = str(excinfo.value)
        # Shape failures are NOT retried and leak nothing: no address, no
        # scripthash, no host.
        assert server.counts[method] == 1
        assert address not in message and _sh(address) not in message
        assert "127.0.0.1" not in message

    def test_bad_address_argument_never_connects(self, electrum: Any) -> None:
        server = electrum()
        with _client(server) as client, pytest.raises(ChainError, match="invalid address"):
            client.get_address_txs("bc1q not-an-address")  # Esplora's charset rule
        assert server.requests == []  # validation precedes even the handshake

    def test_server_error_never_echoes_server_text(self, electrum: Any) -> None:
        secret = f"no history for {_sh(ADDRS[0][0])}"
        server = electrum(script={"blockchain.scripthash.get_history": [("error", secret)]})
        with _client(server) as client, pytest.raises(ChainError) as excinfo:
            client.get_address_txs(ADDRS[0][0])
        assert str(excinfo.value) == "address-txs request rejected by the server"
        assert secret not in str(excinfo.value)


# ------------------------------------------- scan-level indistinguishability


def _esplora_scenario() -> tuple[dict, dict]:
    """One four-transaction wallet story (in / self / mempool-in / hold).

    Returns the ESPLORA-shaped txs and utxos maps keyed by address; the
    electrum fixture is scripted FROM the same story.
    """
    a0, a1, a2 = ADDRS[0][0], ADDRS[0][1], ADDRS[0][2]
    ch0 = ADDRS[1][0]

    def entry_for(txid: str, vouts: tuple[str, ...], *, confirmed: bool, fee: int) -> dict[str, Any]:
        status: dict[str, Any] = {"confirmed": confirmed}
        if confirmed:
            status["block_height"] = 800_000 if txid == "a" * 64 else 800_005
            status["block_time"] = 1_700_000_000
        return {
            "txid": txid,
            "version": 2,
            "locktime": 0,
            "vin": [{"prevout": {"scriptpubkey_address": _EXTERNAL, "value": 1.0}}],
            "vout": [{"scriptpubkey_address": a, "value": 0.0001} for a in vouts],
            "size": 200,
            "weight": 500,
            "status": status,
            "fee": fee,
        }

    tx_a = entry_for("a" * 64, (a0,), confirmed=True, fee=1_500)
    tx_b = entry_for("b" * 64, (ch0, _EXTERNAL), confirmed=True, fee=900)
    tx_b["vin"] = [{"prevout": {"scriptpubkey_address": a0, "value": 1.0}}]  # self
    tx_c = entry_for("c" * 64, (a1,), confirmed=False, fee=500)
    tx_d = entry_for("d" * 64, (a2,), confirmed=True, fee=1_200)
    txs = {a0: [tx_b, tx_a], a1: [tx_c], a2: [tx_d], ch0: [tx_b]}  # newest-first
    utxos = {
        a2: [utxo_entry("d" * 64, 0, 9_000, height=800_005)],
        a1: [utxo_entry("c" * 64, 0, 4_000, confirmed=False)],
    }
    return txs, utxos


def _electrum_script(txs: dict, utxos: dict) -> dict[str, Any]:
    """The same story in native Electrum payloads (oldest-first history)."""
    by_txid: dict[str, dict[str, Any]] = {}
    history: dict[str, list[dict[str, Any]]] = {}
    for address, entries in txs.items():
        rows = []
        for entry in reversed(entries):  # electrum answers oldest-first
            verbose = tx_verbose(
                entry["txid"],
                vins=tuple(i["prevout"]["scriptpubkey_address"] for i in entry["vin"]),
                vouts=tuple(o["scriptpubkey_address"] for o in entry["vout"]),
                height=entry["status"].get("block_height"),
                block_time=entry["status"].get("block_time"),
            )
            by_txid.setdefault(entry["txid"], verbose)
            row: dict[str, Any] = {"tx_hash": entry["txid"], "height": entry["status"].get("block_height", 0)}
            if "fee" in entry:
                row["fee"] = entry["fee"]
            rows.append(row)
        history[_sh(address)] = rows
    unspent = {
        _sh(address): [
            {
                "tx_hash": e["txid"],
                "tx_pos": e["vout"],
                "value": e["value"],
                "height": e["status"].get("block_height", 0),
            }
            for e in entries
        ]
        for address, entries in utxos.items()
    }

    def _history(params: list[Any]) -> list[Any]:
        return history.get(params[0], [])

    def _utxos(params: list[Any]) -> list[Any]:
        return unspent.get(params[0], [])

    def _get(params: list[Any]) -> dict[str, Any]:
        return by_txid[params[0]]

    return {
        "blockchain.headers.subscribe": [{"height": TIP, "hex": _tip_header()}],
        "blockchain.scripthash.get_history": _history,
        "blockchain.scripthash.listunspent": _utxos,
        "blockchain.transaction.get": _get,
    }


def _fresh_plan() -> tuple[Store, wallet_scan.ScanPlan]:
    store = Store.memory()
    wallet = store.create_wallet("main", WD.descriptor)
    store.set_active_wallet(wallet.id)
    return store, wallet_scan.plan_scan(store, WD)


def test_fetch_scan_is_identical_over_both_backends(electrum: Any) -> None:
    """THE M1 contract: scan.py cannot tell the two backends apart.

    Same scenario → field-by-field equal ScanRecords (wall-clock stamp
    excluded), proving the translated shapes ride the existing injected-
    client seam with ZERO edits to the scan tests or scan.py.
    """
    txs, utxos = _esplora_scenario()

    fake = FakeChain(txs=txs, utxos=utxos, tip=TIP)
    store_e, plan_e = _fresh_plan()
    try:
        with fake.client() as esplora_client:
            over_esplora = wallet_scan.fetch_scan(plan_e, esplora_client)
    finally:
        store_e.close()

    server = electrum(script=_electrum_script(txs, utxos))
    store_x, plan_x = _fresh_plan()
    try:
        with _client(server) as electrum_client:
            over_electrum = wallet_scan.fetch_scan(plan_x, electrum_client)
    finally:
        store_x.close()

    assert replace(over_electrum.summary, scanned_at="") == replace(
        over_esplora.summary, scanned_at=""
    )
    assert over_electrum.address_rows == over_esplora.address_rows
    assert over_electrum.derivation_states == over_esplora.derivation_states
    assert over_electrum.utxo_snapshot == over_esplora.utxo_snapshot
    assert over_electrum.tx_rows == over_esplora.tx_rows
    sync_x = dict(over_electrum.sync_state_updates)
    sync_e = dict(over_esplora.sync_state_updates)
    sync_x.pop(wallet_scan.SCAN_AT_KEY)
    sync_e.pop(wallet_scan.SCAN_AT_KEY)
    assert sync_x == sync_e

    # Non-trivial scenario sanity (asserted on the ELECTRUN run directly):
    # usage 0/1/2 on branch 0 and the change branch used on branch 1, a
    # snapshot holding both the confirmed and the mempool coin, and the
    # in/self directions computed correctly.
    summary = over_electrum.summary
    assert summary.branches[0].used_indices == (0, 1, 2)
    assert summary.branches[1].used_indices == (0,)
    assert summary.utxo_count == 2
    assert summary.utxo_value_sats == 13_000
    assert summary.truncated is False
    directions = {row.txid: row.direction for row in over_electrum.tx_rows}
    assert directions["a" * 64] == "in"
    assert directions["b" * 64] == "self"
    assert directions["c" * 64] == "in"
    assert directions["d" * 64] == "in"


# -------------------------------------------------------------- broadcast


class TestBroadcast:
    def test_happy_path_txid_bound(self, electrum: Any) -> None:
        server = electrum(script={"blockchain.transaction.broadcast": [BROADCAST_TXID]})
        with _client(server) as client:
            assert client.broadcast_tx(TX_HEX) == BROADCAST_TXID
        assert server.requests[-1] == ("blockchain.transaction.broadcast", [TX_HEX])

    def test_wrong_txid_refused(self, electrum: Any) -> None:
        server = electrum(script={"blockchain.transaction.broadcast": ["f" * 64]})
        with _client(server) as client, pytest.raises(ChainError, match="does not match"):
            client.broadcast_tx(TX_HEX)

    def test_junk_txid_response_refused(self, electrum: Any) -> None:
        server = electrum(script={"blockchain.transaction.broadcast": ["ok: 123"]})
        with _client(server) as client, pytest.raises(ChainError, match="malformed 'txid'"):
            client.broadcast_tx(TX_HEX)

    def test_single_attempt_even_on_transport_loss(self, electrum: Any) -> None:
        """No-retry parity with Esplora: a POST-equivalent is never
        re-sent, whatever the failure — even with a retry budget."""
        server = electrum(script={"blockchain.transaction.broadcast": [CLOSE, BROADCAST_TXID]})
        with _client(server, max_retries=3) as client, pytest.raises(ChainError) as excinfo:
            client.broadcast_tx(TX_HEX)
        assert server.counts["blockchain.transaction.broadcast"] == 1
        assert str(excinfo.value) == "broadcast failed: network error (ConnectionResetError)"

    def test_error_response_single_attempt(self, electrum: Any) -> None:
        server = electrum(
            script={"blockchain.transaction.broadcast": ("error", "18: tx already known")}
        )
        with _client(server, max_retries=3) as client, pytest.raises(ChainError) as excinfo:
            client.broadcast_tx(TX_HEX)
        assert server.counts["blockchain.transaction.broadcast"] == 1
        assert "already known" not in str(excinfo.value)

    def test_unparseable_hex_never_sent(self, electrum: Any) -> None:
        server = electrum()
        with _client(server) as client, pytest.raises(ChainError, match="invalid transaction hex"):
            client.broadcast_tx("ff" * 40)  # hex-valid, not a parseable transaction
        assert "blockchain.transaction.broadcast" not in server.counts

    def test_expected_txid_is_consensus_txid(self) -> None:
        # Cross-check the fixture: what broadcast_tx binds the answer to.
        assert Transaction.parse(bytes.fromhex(TX_HEX)).txid().hex() == BROADCAST_TXID


# ------------------------------------------------------------------ fees


class TestFees:
    def test_estimatefee_mapping_and_targets(self, electrum: Any) -> None:
        rates = {"fast": 0.00004, "medium": 0.00002, "slow": 0.00001}

        def answer(params: list[Any]) -> float:
            return rates[{1: "fast", 2: "medium", 6: "slow"}[params[0]]]

        server = electrum(script={"blockchain.estimatefee": answer})
        with _client(server) as client:
            assert client.estimate_fee(FeeTarget.FAST) == 4
            assert client.estimate_fee(FeeTarget.MEDIUM) == 2
            assert client.estimate_fee(FeeTarget.SLOW) == 1
        sends = [p for m, p in server.requests if m == "blockchain.estimatefee"]
        assert sends == [[1], [2], [6]]

    def test_estimatefee_minus_one_fails_closed(self, electrum: Any) -> None:
        server = electrum(script={"blockchain.estimatefee": [-1]})
        with _client(server) as client, pytest.raises(ChainError, match="not a usable rate"):
            client.estimate_fee(FeeTarget.FAST)

    @pytest.mark.parametrize("junk", ["0.0001", True, None, {"rate": 1}, 0, -0.5, 100, float("nan")])
    def test_estimatefee_junk_fails_closed(self, electrum: Any, junk: Any) -> None:
        server = electrum(script={"blockchain.estimatefee": [junk]})
        with _client(server) as client, pytest.raises(ChainError):
            client.estimate_fee(FeeTarget.MEDIUM)

    def test_estimator_uses_native_source_and_caches(self, electrum: Any) -> None:
        server = electrum(script={"blockchain.estimatefee": lambda p: 0.00001 * (7 - p[0])})
        with _client(server) as client:
            estimator = FeeEstimator(client, ttl_s=30.0)
            fast = estimator.estimate(FeeTarget.FAST)
            slow = estimator.estimate(FeeTarget.SLOW)
            assert (fast.sat_per_vb, slow.sat_per_vb) == (6, 1)
            assert fast.source is FeeSource.RECOMMENDED  # single-source honesty
            estimator.estimate(FeeTarget.MEDIUM)  # cache hit: no new fetch
        assert server.counts["blockchain.estimatefee"] == 3  # one refresh total

    def test_estimator_minimum_fee_refuses_on_electrum(self, electrum: Any) -> None:
        server = electrum(script={"blockchain.estimatefee": [0.00002]})
        with _client(server) as client:
            estimator = FeeEstimator(client, ttl_s=30.0)
            estimator.estimate(FeeTarget.FAST)  # warm the native cache
            with pytest.raises(ChainError, match="no minimum-fee"):
                estimator.minimum_fee_sat_vb()

    def test_estimator_native_failure_fails_closed(self, electrum: Any) -> None:
        server = electrum(
            script={"blockchain.estimatefee": [0.00002, 0.00002, 0.00002, -1]}
        )
        with _client(server) as client:
            estimator = FeeEstimator(client, ttl_s=30.0)
            assert estimator.estimate(FeeTarget.FAST).sat_per_vb == 2
            estimator.invalidate()
            with pytest.raises(ChainError):
                estimator.estimate(FeeTarget.FAST)  # no lower layer, never fabricated

    def test_esplora_estimate_fee_protocol_member(self) -> None:
        payload = {"fastestFee": 30, "halfHourFee": 25, "hourFee": 18, "economyFee": 10, "minimumFee": 1}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v1/fees/recommended"
            return httpx.Response(200, json=payload)

        with EsploraClient(
            base_url="https://mempool.space/api",
            timeout_s=5.0,
            max_retries=0,
            transport=httpx.MockTransport(handler),
        ) as client:
            assert client.estimate_fee(FeeTarget.SLOW) == 18


# ------------------------------------------------------- tip and status


class TestTipAndStatus:
    def test_tip_height_and_block_from_one_subscribe(self, electrum: Any) -> None:
        server = electrum(
            script={
                "blockchain.headers.subscribe": [
                    {"height": 870_000, "hex": _tip_header(1_755_000_000)}
                ]
            }
        )
        with _client(server) as client:
            assert client.get_tip_height() == 870_000
            assert client.get_tip_block() == TipBlock(height=870_000, timestamp=1_755_000_000)

    def test_missing_header_is_clean_unavailable_timestamp(self, electrum: Any) -> None:
        server = electrum(script={"blockchain.headers.subscribe": [{"height": 5}]})
        with _client(server) as client:
            assert client.get_tip_block() == TipBlock(height=5, timestamp=None)

    def test_garbage_header_hex_is_clean_unavailable_timestamp(self, electrum: Any) -> None:
        server = electrum(script={"blockchain.headers.subscribe": [{"height": 5, "hex": "abcd"}]})
        with _client(server) as client:
            assert client.get_tip_block().timestamp is None

    @pytest.mark.parametrize("junk", [{"height": -1}, {"height": True}, ["x"], 42, {}])
    def test_malformed_tip_fails_closed(self, electrum: Any, junk: Any) -> None:
        server = electrum(script={"blockchain.headers.subscribe": [junk]})
        with _client(server) as client, pytest.raises(ChainError):
            client.get_tip_height()

    def test_tx_status_confirmed_and_unconfirmed(self, electrum: Any) -> None:
        txid = "a" * 64
        server = electrum(
            script={
                "blockchain.transaction.get": [
                    tx_verbose(txid, height=800_000, block_time=1_700_000_000),
                    tx_verbose(txid),  # mempool
                ]
            }
        )
        with _client(server) as client:
            assert client.get_tx_status(txid) == TxStatus(
                txid=txid, confirmed=True, block_height=800_000, block_time=1_700_000_000
            )
            assert client.get_tx_status(txid) == TxStatus(
                txid=txid, confirmed=False, block_height=None, block_time=None
            )

    def test_unknown_txid_surfaces_as_chain_error(self, electrum: Any) -> None:
        server = electrum(script={"blockchain.transaction.get": [("error", "missing tx")]})
        with _client(server) as client, pytest.raises(ChainError, match="tx-status"):
            client.get_tx_status("a" * 64)

    @pytest.mark.parametrize("junk", [None, True, "0", -1, 1.5, [], {}])
    def test_tx_status_malformed_confirmations_fails_closed(self, electrum: Any, junk: Any) -> None:
        server = electrum(
            script={"blockchain.transaction.get": [dict(tx_verbose("a" * 64), confirmations=junk)]}
        )
        with _client(server) as client, pytest.raises(ChainError, match="confirmations"):
            client.get_tx_status("a" * 64)

    def test_bad_txid_argument_never_sent(self, electrum: Any) -> None:
        server = electrum()
        with _client(server) as client, pytest.raises(ChainError, match="invalid txid"):
            client.get_tx_status("A" * 64)  # uppercase violates the contract
        assert "blockchain.transaction.get" not in server.counts


# ----------------------------------------------------------- tls / trust


class TestTls:
    def test_default_verify_refuses_self_signed(self, electrum: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        # Fail-closed default (no env, no config file): the same honest
        # "network error (SSLCertVerificationError)" surface https Esplora
        # produces for a self-signed backend.
        monkeypatch.delenv("LOCALWALLET_TLS_VERIFY", raising=False)
        server = electrum()
        with _client(server) as client, pytest.raises(ChainError) as excinfo:
            client.get_tip_height()
        assert "network error (SSLCertVerificationError)" in str(excinfo.value)

    def test_trusted_ca_verifies_and_connects(
        self, electrum: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Proof verification is REAL, not silently off: with the fixture
        # cert trusted via the standard SSL_CERT_FILE rung (no app knob),
        # the default (verify ON) reaches the very same server.
        monkeypatch.delenv("LOCALWALLET_TLS_VERIFY", raising=False)
        crt = tmp_path / "e-cert.pem"
        crt.write_text(_SELF_SIGNED_CERT)
        monkeypatch.setenv("SSL_CERT_FILE", str(crt))
        server = electrum(script={"blockchain.headers.subscribe": [{"height": 9}]})
        with _client(server) as client:
            assert client.get_tip_height() == 9

    def test_escape_hatch_env_reaches_self_signed(self, electrum: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCALWALLET_TLS_VERIFY", "0")
        server = electrum(script={"blockchain.headers.subscribe": [{"height": 9}]})
        with _client(server) as client:
            assert client.get_tip_height() == 9


# ------------------------------------------------------- retry / timeout


class TestRetryPolicy:
    def test_timeout_retries_with_backoff_then_fails(
        self, electrum: Any, record_sleeps: list[float]
    ) -> None:
        server = electrum(hang=("blockchain.headers.subscribe",))
        with _client(server, timeout_s=0.05, max_retries=2) as client, pytest.raises(ChainError) as excinfo:
            client.get_tip_height()
        assert server.counts["blockchain.headers.subscribe"] == 3
        assert server.connects == 3  # every retry reconnects + re-handshakes
        assert len(record_sleeps) == 2  # esplora-shaped exponential backoff
        assert all(s > 0 for s in record_sleeps)
        assert "tip-height request failed after 2 retries: network error (TimeoutError)" in str(excinfo.value)

    def test_connection_refused_retries_then_chain_error(
        self, electrum: Any, record_sleeps: list[float]
    ) -> None:
        server = electrum()
        url = server.url
        server.stop()
        with ElectrumClient(base_url=url, timeout_s=1.0, max_retries=1) as client, pytest.raises(
            ChainError, match=r"network error \(ConnectionRefusedError\)"
        ):
            client.get_tip_height()
        assert len(record_sleeps) == 1

    def test_error_response_is_not_retried_and_connection_survives(
        self, electrum: Any, record_sleeps: list[float]
    ) -> None:
        server = electrum(
            script={
                "blockchain.scripthash.get_history": [("error", "boom")],
                "blockchain.headers.subscribe": [{"height": 4}],
            }
        )
        with _client(server, max_retries=3) as client:
            with pytest.raises(ChainError, match="address-txs request rejected"):
                client.get_address_txs(ADDRS[0][0])
            assert server.counts["blockchain.scripthash.get_history"] == 1
            assert not record_sleeps
            # Same connection, reused for the next call (no reconnect storm):
            assert client.get_tip_height() == 4
        assert server.connects == 1

    def test_dropped_connection_reconnects_transparently(self, electrum: Any) -> None:
        server = electrum(
            script={"blockchain.headers.subscribe": [CLOSE, {"height": 12}, {"height": 13}]}
        )
        with _client(server) as client:
            with pytest.raises(ChainError):
                client.get_tip_height()
            assert client.get_tip_height() == 12  # reconnected + re-handshook
        assert server.connects == 2
        assert server.requests[0][0] == "server.version"

    def test_greeting_and_notification_lines_skipped(self, electrum: Any) -> None:
        server = electrum(
            greeting=b"FixtureElectrum says hello\r\n",
            notify=True,
            script={"blockchain.headers.subscribe": [{"height": 77}]},
        )
        with _client(server) as client:
            assert client.get_tip_height() == 77
            assert client.get_tip_height() == 77  # second answer, same connection
        assert server.connects == 1

    def test_garbage_after_first_answer_fails_closed_and_drops(self, electrum: Any) -> None:
        server = electrum(
            script={
                "blockchain.headers.subscribe": [
                    {"height": 1},
                    ("raw", "corrupt{{{"),
                    {"height": 2},
                ]
            }
        )
        with _client(server) as client:
            assert client.get_tip_height() == 1
            with pytest.raises(ChainError, match="not valid JSON"):
                client.get_tip_height()
            # Stream integrity was lost: the next call rebuilds from a
            # fresh handshake (the unanswered garbage'd request is gone).
            assert client.get_tip_height() == 2
        assert server.connects == 2


# ------------------------------------------------- selection + protocol


class TestSelectionAndProtocol:
    def test_chain_config_kind_matrix(self) -> None:
        for url, kind in [
            ("ssl://h", "electrum"),
            ("ssl://h:50002", "electrum"),
            ("ssl://127.0.0.1:50001", "electrum"),
            ("https://mempool.space/api", "esplora"),
            ("http://127.0.0.1:3006", "esplora"),
        ]:
            assert ChainConfig(base_url=url, timeout_s=5, max_retries=0).kind == kind, url

    @pytest.mark.parametrize("bad", ["ssl://", "ssl://h:port", "ssl://h:99999", "ssl://h/p", "ssl://u:p@h"])
    def test_malformed_ssl_url_fails_closed_value_free(self, bad: str) -> None:
        with pytest.raises(ValueError) as excinfo:
            ChainConfig(base_url=bad, timeout_s=5, max_retries=0)
        suffix = bad.split("://", 1)[1]
        assert not suffix or suffix not in str(excinfo.value)

    def test_from_settings_routes_ssl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", "ssl://electrum.example:50002")
        config = ChainConfig.from_settings(Settings.from_env())
        assert config.kind == "electrum"
        assert config.base_url == "ssl://electrum.example:50002"

    def test_esplora_client_refuses_ssl_urls(self) -> None:
        with pytest.raises(ValueError, match="ElectrumClient"):
            EsploraClient(base_url="ssl://h:1")

    def test_check_backend_stays_esplora_shaped(self, electrum: Any) -> None:
        # The M1 /setup probe is unchanged: an ssl:// endpoint simply does
        # not pass the Esplora-shape check (M3 replaces it with a
        # scheme-aware probe).
        server = electrum()
        assert check_backend(server.url, timeout_s=0.5, max_retries=0) is False

    def test_build_chain_client_picks_by_scheme(self) -> None:
        client_x = app_module._build_chain_client(
            Settings(chain_base_url="ssl://127.0.0.1:59999", request_timeout_s=5.0, max_retries=0)
        )
        client_e = app_module._build_chain_client(
            Settings(chain_base_url="https://mempool.space/api", request_timeout_s=5.0, max_retries=0)
        )
        try:
            assert isinstance(client_x, ElectrumClient)
            assert isinstance(client_e, EsploraClient)
            assert (client_x._host, client_x._port) == ("127.0.0.1", 59999)
        finally:
            client_x.close()
            client_e.close()

    def test_build_chain_client_default_port(self) -> None:
        client = app_module._build_chain_client(
            Settings(chain_base_url="ssl://h", request_timeout_s=5.0, max_retries=0)
        )
        try:
            assert client._port == 50002
        finally:
            client.close()

    def test_both_clients_structurally_satisfy_chain_client(self, electrum: Any) -> None:
        """scan/watch/fees/price code against the EsploraClient-typed seam;
        this protocol test is the contract pin for the swapped client."""
        server = electrum()
        with (
            ElectrumClient(base_url=server.url, timeout_s=5.0, max_retries=0) as client_x,
            EsploraClient(
                base_url="https://mempool.space/api",
                timeout_s=5.0,
                max_retries=0,
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
            ) as client_e,
        ):
            assert isinstance(client_x, ChainClient)
            assert isinstance(client_e, ChainClient)

    def test_get_json_is_esplora_only(self, electrum: Any) -> None:
        server = electrum()
        with _client(server) as client:
            assert not hasattr(client, "get_json")  # the plan's capability split

    def test_price_oracle_refuses_electrum_without_fetching(self, electrum: Any) -> None:
        server = electrum()
        with _client(server) as client:
            oracle = PriceOracle(client, ttl_s=60.0, enabled=True)
            with pytest.raises(PriceUnavailableError, match="no price feed"):
                oracle.fresh()
            with pytest.raises(PriceUnavailableError, match="no price feed"):
                oracle.stale_ok()
        assert server.requests == []  # refused before ANY network contact

    def test_errors_never_leak_host_or_query(self, electrum: Any) -> None:
        server = electrum(script={"blockchain.scripthash.get_history": "junk"})
        address = ADDRS[0][3]
        with _client(server) as client, pytest.raises(ChainError) as excinfo:
            client.get_address_txs(address)
        message = str(excinfo.value)
        assert address not in message and _sh(address) not in message
        assert "127.0.0.1" not in message
        assert str(server.url.rsplit(":", 1)[1]) not in message


# ------------------------------ watch seam: same client, existing polling


def test_watch_tip_helper_rides_the_adapter(electrum: Any) -> None:
    server = electrum(
        script={
            "blockchain.headers.subscribe": [{"height": TIP, "hex": _tip_header(1_700_000_000)}]
        }
    )
    with _client(server) as client:
        # Fixed "now" five minutes after the fixture's tip timestamp.
        assert time_since_last_block(client, now=1_700_000_300.0) == 300

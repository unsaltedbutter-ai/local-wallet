"""Electrum-protocol chain client (TCK-ONB-004 M1; ADR-0018 amendment).

The second :class:`~localwallet.chain.esplora.ChainClient` implementation:
a synchronous Electrum 1.4 (stratum JSON-lines) client over a stdlib
``ssl`` socket — **no new dependency** (docs/onb-004-backend-adapters-plan.md
§1: stdlib is sufficient for the fail-closed/retry story). Selected by the
``ssl://host[:port]`` URL scheme at the single construction point
(``ChainConfig.from_settings`` → ``app._build_chain_client``); http(s) URLs
keep using :class:`~localwallet.chain.esplora.EsploraClient`.

The adapter's outputs are INDISTINGUISHABLE from the Esplora shapes the
app's strict parsers consume (``wallet.scan._parse_tx_entry`` /
``_parse_utxo_entry``): the address-txs/utxos methods TRANSLATE the
Electrum responses into those exact shapes (pinned by
``tests/test_chain_electrum.py`` against the scan tests' mock payloads).

Method mapping (plan §1 table):

* handshake per connection: ``server.version`` (2 params; result accepted
  as a list or a bare string) + ``server.features``, whose ``genesis_hash``
  MUST equal :data:`~localwallet.chain.esplora.MAINNET_GENESIS_HASH` —
  mainnet-only is enforced HERE because M1 exposes no setup-time probe
  for ``ssl://`` (ADR-0021; a testnet server is refused value-free on
  every connection).
* ``blockchain.scripthash.get_history`` + one
  ``blockchain.transaction.get(tx, verbose)`` per tx → ``get_address_txs``
  (the N+1 cost is the plan's accepted OQ-3 default; the gap window bounds
  it), history ``fee`` passes through when present (never fabricated).
* ``blockchain.scripthash.listunspent`` → ``get_address_utxos``
  (``confirmed`` = ``height > 0``, the electrs/ElectrumX mempool
  convention).
* ``blockchain.headers.subscribe`` → tip height AND the tip block timestamp
  (deviation from the plan's ``blockchain.headers.tip``/``block.header``
  mapping: ``subscribe`` is the only universally implemented tip call and
  its result already carries the raw 80-byte header; timestamp = little-
  endian bytes 68–72. The new-block notifications it arms are id-less
  lines this client skips — the watch poll still rides the ChainWorker's
  existing re-scan path, unchanged).
* ``blockchain.transaction.get(tx, verbose)`` → ``get_tx_status``.
* ``blockchain.transaction.broadcast`` → ``broadcast_tx` with the SAME
  single-attempt semantics as Esplora (never retried; the expected txid is
  computed from the hex BEFORE sending and the answer is re-bound to it,
  TCK-SEC-004 change 1).
* ``blockchain.estimatefee(blocks)`` → ``estimate_fee`` (target param is
  ADVISORY — servers may answer every target with one rate, documented
  plan §1 tolerance; a ``-1`` "cannot estimate" answer fails closed, never
  fabricated).

Transport notes: one connection, one request in flight (sequential, never
batched — plan: no batching in v1), guarded by a lock because the engine
thread (broadcast/status/fees) and the ChainWorker thread (scan/watch)
share the client, exactly as they share the ``httpx``-thread-safe
``EsploraClient``. A lost line-reader position always forces a reconnect
+ re-handshake before the next retry, so a partial read can never bleed
into a later response.

Tolerances (plan §1): ONE non-JSON greeting line is skipped before a
connection's first parsed answer (server banners); id-less / other-id
lines (notifications) are skipped; a server-side JSON-RPC error is a
ChainError naming only the endpoint kind — the server's own error text is
never echoed (untrusted input, and it can embed the queried scripthash).

Failure discipline mirrors Esplora exactly: connection failures and
timeouts (the transport class) retry with the SHARED backoff policy for
read-only calls; malformed JSON, unexpected shapes, and server errors
fail immediately with value-free :class:`ChainError` messages (endpoint
*kinds* only — no host, address, scripthash, txid, or amount ever leaves
this module in an error string). No logging, no printing.
"""

from __future__ import annotations

import hashlib
import json
import math
import socket
import ssl
import threading
from types import TracebackType
from typing import Any, Final, Self
from urllib.parse import urlsplit

from embit.script import Script, address_to_scriptpubkey
from embit.transaction import Transaction

from localwallet.chain.config import ChainConfig
from localwallet.chain.esplora import (
    _TXID_CHARSET,
    _TXID_LENGTH_CHARS,
    MAINNET_GENESIS_HASH,
    NOT_MAINNET,
    ChainError,
    TipBlock,
    TxStatus,
    _backoff_delay,
    _sleep_for,
    _validate_address,
    _validate_tx_hex,
    _validate_txid,
    classify_failure,
)
from localwallet.chain.fees import FeeTarget
from localwallet.config import Settings

__all__ = ["ElectrumClient"]

# Keep in sync with the version in pyproject.toml (same string as esplora's).
_CLIENT_NAME: Final[str] = "local-wallet/0.1.0 (watch-only Bitcoin wallet)"
_PROTOCOL_MIN: Final[str] = "1.4"

#: Standard Electrum SSL port, used when the URL omits one.
_DEFAULT_SSL_PORT: Final[int] = 50002

# Endpoint kinds used in error messages INSTEAD of anything the server or
# the request carried (log-scrubbing invariant, PROJECT.md §7.8). Reuses
# the exact kind names the Esplora client emits so caller-visible error
# shapes do not depend on the backend.
_KIND_HANDSHAKE = "handshake"
_KIND_FEATURES = "handshake-features"
_KIND_ADDRESS_TXS = "address-txs"
_KIND_ADDRESS_UTXOS = "address-utxos"
_KIND_TIP_HEIGHT = "tip-height"
_KIND_TIP_BLOCK = "tip-block"
_KIND_BROADCAST = "broadcast"
_KIND_TX_STATUS = "tx-status"
_KIND_FEE_ESTIMATE = "fee-estimate"

#: Our confirmation targets → Electrum ``estimatefee`` block targets
#: (plan §1; advisory, servers vary).
_ESTIMATEFEE_TARGETS: Final[dict[FeeTarget, int]] = {
    FeeTarget.FAST: 1,
    FeeTarget.MEDIUM: 2,
    FeeTarget.SLOW: 6,
}

# BTC/kB → sat/vB: 1 BTC/kB = 1e8 sats / 1e3 bytes (1e3 vB) = 1e5 sat/vB.
_SAT_VB_PER_BTC_KB: Final[float] = 100_000.0

#: Sanity ceiling for a server's estimatefee answer (BTC/kB). Not a fee
#: policy — a broken/evil payload guard in the spirit of price.py's
#: _MAX_USD_PER_BTC; anything above 0.1 BTC/kB (10 000 sat/vB) is nonsense.
_MAX_FEE_BTC_KB: Final[float] = 0.1

#: Block header field: 4-byte little-endian Unix time at byte offset 68.
_HEADER_TIME_OFFSET: Final[int] = 68
_HEADER_BYTES: Final[int] = 80


class _TransportFailure(Exception):
    """Internal marker: the CONNECTION failed (connect/TLS/timeout/closed).

    This is the retryable class — exactly the httpx ``TransportError``
    equivalent from the Esplora client's policy. Every raise site carries a
    bare exception CLASS NAME (never a value), which :meth:`ElectrumClient._rpc`
    surfaces inside the ChainError after the retry budget is exhausted;
    the marker itself never escapes to callers. ``failure_class`` is the
    TCK-DIAG-001 value-free class attached at the raw-exception raise site.
    """

    def __init__(self, exc_name: str, *, failure_class: str | None = None) -> None:
        super().__init__(exc_name)
        self.failure_class = failure_class


def _require_txid_hex(value: Any, kind: str) -> str:
    """Validate a server-supplied txid as 64 lowercase hex (fail closed)."""
    if (
        not isinstance(value, str)
        or len(value) != _TXID_LENGTH_CHARS
        or not set(value) <= _TXID_CHARSET
    ):
        raise ChainError(f"{kind} response has a missing or malformed 'txid'")
    return value


def _require_plain_int(value: Any) -> int | None:
    """Return value as a strict non-bool int, or None when it is not one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _address_from_script_pubkey(spk: Any) -> str | None:
    """Best-effort ``scriptpubkey_address`` for one tx io entry.

    Prefers the server's ``addresses`` list (ElectrumX/electrs both attach
    it on outputs) and falls back to DERIVING the address from the raw
    ``hex`` script with embit. Anything unmappable (OP_RETURN, nonstandard,
    malformed) is simply absent — ``scan._addresses_from_io`` treats a
    missing address as "contributes nothing", the same as Esplora does for
    the same scripts. Never raises, value-free.
    """
    if not isinstance(spk, dict):
        return None
    addresses = spk.get("addresses")
    if isinstance(addresses, list) and addresses and isinstance(addresses[0], str):
        return addresses[0]
    script_hex = spk.get("hex")
    if isinstance(script_hex, str):
        try:
            return Script(bytes.fromhex(script_hex)).address()
        except Exception:  # noqa: BLE001 — containment: any unmappable script is "no address"
            return None
    return None


class ElectrumClient:
    """Synchronous Electrum-protocol (stratum, JSON-lines over TLS) client.

    Satisfies :class:`~localwallet.chain.esplora.ChainClient`. Construction
    is network-free (connect + handshake happen lazily on the first call);
    the URL, per-call timeout, retry budget and TLS trust resolve through
    the SAME single selection point as the Esplora client —
    :meth:`ChainConfig.from_settings` (``Settings.chain_base_url`` /
    ``LOCALWALLET_CHAIN_BASE_URL`` with an ``ssl://`` scheme; ``tls_verify``
    env > config file > fail-closed ``True``, ADR-0018 amendment):
    electrum servers on Start9/Umbrel-style boxes ship self-signed certs
    too, so the ``tls_verify=False`` escape hatch (plus the app's honest
    startup warning) applies identically here.

    Retry policy (consistent with Esplora): connect/TLS/timeout/closed
    failures retry up to ``max_retries`` with the shared exponential
    backoff + jitter (read-only calls); every shape/JSON/server-error
    failure raises immediately. ``broadcast_tx`` is the deliberate
    single-attempt exception (a re-send is never automatic — callers
    recover via ``get_tx_status``, exactly the Esplora contract).

    Raises:
        ValueError: at construction if the resolved URL is not a well-formed
            ``ssl://host[:port]`` endpoint (fail closed, value-free).
    """

    #: No price feed on the Electrum protocol (plan OQ-2): the price oracle
    #: refuses fail-closed to the sats-only rung on this backend.
    supports_price: bool = False

    def __init__(
        self,
        base_url: str | None = None,
        timeout_s: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        defaults = ChainConfig.from_settings(Settings.from_env())
        self._config = ChainConfig(
            base_url=defaults.base_url if base_url is None else base_url,
            timeout_s=defaults.timeout_s if timeout_s is None else timeout_s,
            max_retries=defaults.max_retries if max_retries is None else max_retries,
            # Same resolution as Esplora (env > config file > fail-closed
            # True). Electrum TLS certs are frequently self-signed too.
            tls_verify=defaults.tls_verify,
        )
        parsed = urlsplit(self._config.base_url)
        if parsed.scheme != "ssl":
            raise ValueError("ElectrumClient requires an ssl:// URL")
        self._host: str = parsed.hostname  # validated by ChainConfig
        self._port: int = parsed.port or _DEFAULT_SSL_PORT
        self._lock = threading.Lock()
        # ponytail: one global socket + lock; the Esplora backend shares one
        # httpx.Client the same way. Per-thread connections only if the
        # watch poll and interactive turns ever measurably contend.
        self._sock: ssl.SSLSocket | None = None
        self._buf = b""
        self._next_id = 0
        self._answered = False  # greeting tolerance, per connection

    # ------------------------------------------------------------- lifecycle

    def close(self) -> None:
        """Close the socket (idempotent; safe to call from any thread)."""
        with self._lock:
            self._drop_connection()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # --------------------------------------------------------- contract API

    def get_address_txs(self, address: str) -> list[dict[str, Any]]:
        """Transaction history for ``address``, in the Esplora ``/txs`` shape.

        ``blockchain.scripthash.get_history`` lists the txs; each is
        expanded with one ``blockchain.transaction.get(tx, verbose=true)``
        (the plan's accepted N+1, OQ-3). Outputs carry ``txid``,
        ``status{confirmed[,block_height][,block_time]}``, optional ``fee``
        (from the history entry — sats, passed through only when the server
        sent one, never fabricated) and ``vin``/``vout`` with
        ``scriptpubkey_address`` where mappable — exactly what
        ``scan._parse_tx_entry`` consumes. Ordering is oldest-first here vs
        newest-first on Esplora; every consumer is order-independent (the
        scan merges by txid), documented as tolerated.
        """
        history = self._rpc(
            "blockchain.scripthash.get_history",
            [self._scripthash(address)],
            _KIND_ADDRESS_TXS,
        )
        if not isinstance(history, list) or any(
            not isinstance(item, dict) for item in history
        ):
            raise ChainError(f"{_KIND_ADDRESS_TXS} history was not a list of objects")
        entries: list[dict[str, Any]] = []
        for item in history:
            txid = _require_txid_hex(item.get("tx_hash"), _KIND_ADDRESS_TXS)
            height = _require_plain_int(item.get("height"))
            if height is None:
                raise ChainError(f"{_KIND_ADDRESS_TXS} history entry has a malformed 'height'")
            raw_fee = item.get("fee")
            if raw_fee is not None and (
                isinstance(raw_fee, bool) or not isinstance(raw_fee, int) or raw_fee < 0
            ):
                # A malformed optional fee is dropped, not fatal: scan
                # stores fee=None by design when absent (never fabricated).
                raw_fee = None
            verbose = self._rpc(
                "blockchain.transaction.get",
                [txid, True],
                _KIND_ADDRESS_TXS,
            )
            entries.append(self._tx_entry(verbose, height, raw_fee))
        return entries

    def get_address_utxos(self, address: str) -> list[dict[str, Any]]:
        """Unspent outputs for ``address``, in the Esplora ``/utxo`` shape.

        ``blockchain.scripthash.listunspent`` entries ``{tx_hash, tx_pos,
        value, height}`` map to ``{txid, vout, value, status:{confirmed
        [,block_height]}}`` with ``confirmed = height > 0`` (0/mempool is
        unconfirmed), exactly what ``scan._parse_utxo_entry`` and
        :func:`~localwallet.chain.esplora.balance_from_utxos` consume.
        """
        rows = self._rpc(
            "blockchain.scripthash.listunspent",
            [self._scripthash(address)],
            _KIND_ADDRESS_UTXOS,
        )
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise ChainError(f"{_KIND_ADDRESS_UTXOS} response was not a list of objects")
        utxos: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            txid = _require_txid_hex(row.get("tx_hash"), _KIND_ADDRESS_UTXOS)
            vout = _require_plain_int(row.get("tx_pos"))
            if vout is None or vout < 0:
                raise ChainError(f"utxo entry {index} has a missing or invalid 'vout'")
            value = _require_plain_int(row.get("value"))
            if value is None or value < 0:
                raise ChainError(f"utxo entry {index} has a missing or invalid 'value'")
            height = _require_plain_int(row.get("height"))
            if height is None:
                raise ChainError(f"utxo entry {index} has a missing or invalid 'height'")
            confirmed = height > 0
            status: dict[str, Any] = {"confirmed": confirmed}
            if confirmed:
                status["block_height"] = height
            utxos.append({"txid": txid, "vout": vout, "value": value, "status": status})
        return utxos

    def get_tip_height(self) -> int:
        """Tip height via ``blockchain.headers.subscribe`` (``{height}``)."""
        return self._tip_result()["height"]

    def get_tip_block(self) -> TipBlock:
        """Tip block info from one ``blockchain.headers.subscribe``.

        The result carries the raw 80-byte tip header hex; the timestamp is
        its little-endian bytes 68–72. An absent/unparseable ``hex`` is the
        clean ``timestamp=None`` unavailable state (the same contract as
        Esplora's bare-integer tip shape) — never a fabricated time; a
        malformed ``height`` still fails closed.
        """
        result = self._tip_result()
        timestamp: int | None = None
        header_hex = result.get("hex")
        if isinstance(header_hex, str):
            try:
                header = bytes.fromhex(header_hex)
            except ValueError:
                header = b""
            if len(header) == _HEADER_BYTES:
                stamp = int.from_bytes(header[_HEADER_TIME_OFFSET : _HEADER_TIME_OFFSET + 4], "little")
                if stamp > 0:
                    timestamp = stamp
        return TipBlock(height=result["height"], timestamp=timestamp)

    def broadcast_tx(self, tx_hex: str) -> str:
        """Broadcast a signed transaction — SINGLE attempt, no retries.

        ``blockchain.transaction.broadcast``. The same money-path discipline
        as :meth:`EsploraClient.broadcast_tx <localwallet.chain.esplora.EsploraClient.broadcast_tx>`:
        the tx hex is validated before anything is sent, the EXPECTED txid
        is computed from the serialization with embit beforehand, the
        answer is re-validated as 64 lowercase hex and BOUND to the
        expected txid (a well-formed but different txid from a misbehaving
        server is a value-free :class:`ChainError`, TCK-SEC-004 change 1).
        A POST-equivalent is not idempotent, so every failure — transport
        or otherwise — surfaces after exactly ONE attempt; callers recover
        through :meth:`get_tx_status`, never by re-broadcasting.
        """
        _validate_tx_hex(tx_hex)
        try:
            expected_txid = Transaction.parse(bytes.fromhex(tx_hex)).txid().hex()
        except Exception as exc:  # containment: embit parse errors vary
            raise ChainError(
                f"{_KIND_BROADCAST} invalid transaction hex: not a parseable transaction"
            ) from exc
        result = self._rpc(
            "blockchain.transaction.broadcast", [tx_hex], _KIND_BROADCAST, retries=0
        )
        reported = _require_txid_hex(result, _KIND_BROADCAST)
        if reported != expected_txid:
            raise ChainError(
                f"{_KIND_BROADCAST} response txid does not match the broadcast transaction"
            )
        return reported

    def get_tx_status(self, txid: str) -> TxStatus:
        """One transaction's confirmation status via verbose
        ``blockchain.transaction.get``.

        ``confirmed`` = ``confirmations > 0``; ``block_height``/``block_time``
        come from ``blockheight``/``time`` when confirmed and are ``None``
        otherwise (a mempool tx's first-seen ``time`` is deliberately NOT
        reported as a block time). An unknown txid surfaces as the server's
        error → :class:`ChainError` — the Esplora 404 parity; callers decide
        what "unknown" means at the handler layer.
        """
        _validate_txid(txid)
        verbose = self._rpc("blockchain.transaction.get", [txid, True], _KIND_TX_STATUS)
        if not isinstance(verbose, dict):
            raise ChainError(f"{_KIND_TX_STATUS} response was not an object")
        # ElectrumX/electrs OMIT 'confirmations' for mempool txs — absent
        # means unconfirmed. A present-but-malformed value still fails closed.
        if "confirmations" not in verbose:
            confirmations = 0
        else:
            confirmations = _require_plain_int(verbose["confirmations"])
            if confirmations is None or confirmations < 0:
                raise ChainError(
                    f"{_KIND_TX_STATUS} response has a missing or invalid 'confirmations'"
                )
        confirmed = confirmations > 0
        block_height: int | None = None
        block_time: int | None = None
        if confirmed:
            block_height = _require_plain_int(verbose.get("blockheight"))
            if block_height is None or block_height < 0:
                raise ChainError(f"{_KIND_TX_STATUS} response has invalid 'blockheight'")
            stamp = _require_plain_int(verbose.get("time"))
            block_time = stamp if stamp is not None and stamp >= 0 else None
        return TxStatus(
            txid=txid, confirmed=confirmed, block_height=block_height, block_time=block_time
        )

    def estimate_fee(self, target: FeeTarget) -> int:
        """Backend-native fee bid via ``blockchain.estimatefee`` → sat/vB.

        The number parameter (block target: FAST=1, MEDIUM=2, SLOW=6) is
        ADVISORY — some servers answer every target with one rate (plan §1
        tolerance, accepted; the tx engine's min-relay floor keeps bids
        sane). The answer is BTC/kB (possibly fractional): ``-1`` (cannot
        estimate) and any non-positive/absurd/non-numeric value fail closed
        as :class:`ChainError` — never a fabricated bid. Conversion:
        ``sat/vB = round(BTC/kB × 100000)``; a positive rate that rounds to
        0 is rejected for the same reason a 0 recommended fee is (a 0 sat/vB
        bid is broken, not free).
        """
        if not isinstance(target, FeeTarget):
            raise TypeError("target must be a FeeTarget")
        raw = self._rpc(
            "blockchain.estimatefee", [_ESTIMATEFEE_TARGETS[target]], _KIND_FEE_ESTIMATE
        )
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ChainError(f"{_KIND_FEE_ESTIMATE} response was not a number")
        try:
            btc_per_kb = float(raw)
        except OverflowError as exc:
            raise ChainError(f"{_KIND_FEE_ESTIMATE} response is not representable") from exc
        if not math.isfinite(btc_per_kb) or btc_per_kb < 0 or btc_per_kb > _MAX_FEE_BTC_KB:
            # -1 is the documented "cannot estimate" sentinel; negatives and
            # absurd rates are broken payloads. Fail closed, name nothing.
            raise ChainError(f"{_KIND_FEE_ESTIMATE} response is not a usable rate")
        if btc_per_kb == 0:
            raise ChainError(f"{_KIND_FEE_ESTIMATE} response was a zero rate")
        sat_vb = round(btc_per_kb * _SAT_VB_PER_BTC_KB)
        if sat_vb <= 0:
            raise ChainError(f"{_KIND_FEE_ESTIMATE} response is below 1 sat/vB granularity")
        return sat_vb

    # ------------------------------------------------------------- internals

    @staticmethod
    def _scripthash(address: str) -> str:
        """Electrum scripthash: ``sha256(scriptPubKey)`` reversed, hex.

        The address's output script comes from embit; the strict
        alphanumeric/length pre-check is Esplora's (fail closed before any
        request is even framed). Every address the app holds is mainnet
        (ADR-0021); an unconvertible string is a value-free ChainError.
        """
        _validate_address(address)
        try:
            script = address_to_scriptpubkey(address).data
        except Exception:  # noqa: BLE001 — containment: embit errors vary and must not echo the address
            raise ChainError("invalid address argument") from None
        return hashlib.sha256(script).digest()[::-1].hex()

    def _tip_result(self) -> dict[str, Any]:
        """Validated ``blockchain.headers.subscribe`` result object."""
        result = self._rpc("blockchain.headers.subscribe", [], _KIND_TIP_HEIGHT)
        if not isinstance(result, dict):
            raise ChainError(f"{_KIND_TIP_HEIGHT} response was not an object")
        height = _require_plain_int(result.get("height"))
        if height is None or height < 0:
            raise ChainError(f"{_KIND_TIP_HEIGHT} response has a missing or invalid 'height'")
        return {"height": height, "hex": result.get("hex")}

    @staticmethod
    def _tx_entry(verbose: Any, height: int, fee: int | None) -> dict[str, Any]:
        """Translate one Electrum verbose tx into the Esplora ``/txs`` shape."""
        kind = _KIND_ADDRESS_TXS
        if not isinstance(verbose, dict):
            raise ChainError(f"{kind} transaction detail was not an object")
        entry: dict[str, Any] = {"txid": _require_txid_hex(verbose.get("txid"), kind)}
        status: dict[str, Any] = {"confirmed": height > 0}
        if height > 0:
            status["block_height"] = height
            stamp = _require_plain_int(verbose.get("time"))
            if stamp is not None and stamp >= 0:
                status["block_time"] = stamp
        entry["status"] = status
        if fee is not None:
            entry["fee"] = fee
        vin = verbose.get("vin")
        vout = verbose.get("vout")
        if not isinstance(vin, list) or not isinstance(vout, list) or any(
            not isinstance(item, dict) for item in [*vin, *vout]
        ):
            raise ChainError(f"{kind} transaction detail has malformed 'vin'/'vout'")
        inputs: list[dict[str, Any]] = []
        for item in vin:
            prevout = item.get("prevout")
            if isinstance(prevout, dict):
                address = _address_from_script_pubkey(prevout.get("scriptPubKey"))
                inputs.append(
                    {"prevout": {"scriptpubkey_address": address} if address else {}}
                )
            else:
                inputs.append({})  # coinbase / no prevout — contributes nothing
        outputs: list[dict[str, Any]] = []
        for item in vout:
            address = _address_from_script_pubkey(item.get("scriptPubKey"))
            outputs.append({"scriptpubkey_address": address} if address else {})
        entry["vin"] = inputs
        entry["vout"] = outputs
        return entry

    def _rpc(self, method: str, params: list[Any], kind: str, *, retries: int | None = None) -> Any:
        """One JSON-RPC call under the Esplora-consistent retry policy.

        Transport failures (connect/TLS/timeout/closed socket) reconnect —
        including the mainnet-enforcing handshake — and retry up to
        ``retries`` (default: the configured budget; broadcast passes 0).
        Everything else (server error answers, malformed JSON, bad shapes)
        raises immediately. Error messages carry only the endpoint kind and
        exception CLASS names.
        """
        budget = self._config.max_retries if retries is None else retries
        last_failure = "no attempt completed"
        last_transport: _TransportFailure | None = None
        for attempt in range(budget + 1):
            try:
                with self._lock:
                    if self._sock is None:
                        self._connect()
                    return self._raw_request(method, params, kind)
            except _TransportFailure as exc:
                with self._lock:
                    self._drop_connection()
                # Class-name-only surface, mirroring esplora's
                # "network error (ConnectError)" style; str(exc) carries no
                # values by construction of every raise site below.
                last_failure = f"network error ({exc.args[0] if exc.args else type(exc).__name__})"
                last_transport = exc
            if attempt < budget:
                _sleep_for(_backoff_delay(attempt))
        if budget == 0:
            raise ChainError(
                f"{kind} failed: {last_failure}",
                failure_class=last_transport.failure_class if last_transport else None,
                exc_name=last_transport.args[0] if last_transport and last_transport.args else None,
            )
        raise ChainError(
            f"{kind} request failed after {budget} retries: {last_failure}",
            failure_class=last_transport.failure_class if last_transport else None,
            exc_name=last_transport.args[0] if last_transport and last_transport.args else None,
        )

    def _connect(self) -> None:
        """Open the TLS socket and run the fail-closed handshake.

        Caller holds the lock. ``SSLError``/``OSError`` (incl. timeouts and
        DNS/refusal) become :class:`_TransportFailure` (retryable class);
        the handshake's mainnet/shape failures are plain ChainErrors and
        abort without retry.
        """
        context = ssl.create_default_context()
        if not self._config.tls_verify:
            # Deliberate operator escape hatch (LOCALWALLET_TLS_VERIFY=0),
            # the same downgrade the app warns about at startup for https
            # Esplora backends. The app prints the same warning for this
            # backend too.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        try:
            raw = socket.create_connection((self._host, self._port), timeout=self._config.timeout_s)
        except OSError as exc:
            raise _TransportFailure(
                type(exc).__name__, failure_class=classify_failure(exc)
            ) from None
        try:
            self._sock = context.wrap_socket(raw, server_hostname=self._host)
        except (OSError, ValueError) as exc:
            # ValueError covers wrap-time misuse (empty hostname); an
            # empty host cannot reach here (ChainConfig rejects it) but a
            # raw never-None socket is the point: close, then fail class-
            # only, never an ssl message that can echo the endpoint.
            try:
                raw.close()
            except OSError:
                pass
            raise _TransportFailure(
                type(exc).__name__, failure_class=classify_failure(exc)
            ) from None
        self._buf = b""
        self._answered = False
        try:
            self._handshake()
        except ChainError:
            # A refused handshake (non-mainnet, junk version) must not leave
            # a live-but-unusable connection behind.
            self._drop_connection()
            raise

    def _drop_connection(self) -> None:
        """Close and forget the socket (idempotent). Caller holds the lock."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._buf = b""

    def _handshake(self) -> None:
        """``server.version`` + mainnet proof via ``server.features``.

        The version result is a list ``[name, min_version]`` or a bare
        string depending on the server (plan §1) — both accepted, content
        not interpreted. ``server.features.genesis_hash`` MUST equal the
        mainnet constant: this adapter is the only mainnet gate for an
        env-configured ``ssl://`` backend until the M3 setup probe exists
        (ADR-0021; testnet/regtest servers are refused value-free).
        """
        version = self._raw_request("server.version", [_CLIENT_NAME, _PROTOCOL_MIN], _KIND_HANDSHAKE)
        if not isinstance(version, (str, list)):
            raise ChainError(f"{_KIND_HANDSHAKE} response was not a string or list")
        features = self._raw_request("server.features", [], _KIND_FEATURES)
        if not isinstance(features, dict) or features.get("genesis_hash") != MAINNET_GENESIS_HASH:
            raise ChainError(
                f"{_KIND_FEATURES} backend does not serve mainnet",
                failure_class=NOT_MAINNET,
            )

    def _raw_request(self, method: str, params: list[Any], kind: str) -> Any:
        """Send one request line, read lines until the matching answer.

        Caller holds the lock and guarantees a live connection. Transport
        losses raise :class:`_TransportFailure`; malformed payloads and
        server errors raise :class:`ChainError` immediately (no retry —
        every attempt would see the same). Notification lines (no ``id``)
        and any other id's lines are skipped; the FIRST unparsable line on
        a connection is the tolerated server greeting, a SECOND one is a
        malformed-stream failure (fail closed).
        """
        if self._sock is None:  # contract: _rpc connects under the lock first
            raise ChainError(f"{kind} request on a closed connection")
        sock = self._sock
        self._next_id += 1
        request_id = self._next_id
        frame = json.dumps({"id": request_id, "method": method, "params": params}) + "\n"
        try:
            sock.sendall(frame.encode("utf-8"))
        except OSError as exc:
            raise _TransportFailure(
                type(exc).__name__, failure_class=classify_failure(exc)
            ) from None
        while True:
            try:
                line = self._read_line()
            except OSError as exc:
                raise _TransportFailure(
                    type(exc).__name__, failure_class=classify_failure(exc)
                ) from None
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                if self._answered:
                    # Framing integrity is lost on a live connection: drop
                    # it (under the caller's lock) so a later request can
                    # never read this one's remains.
                    self._drop_connection()
                    raise ChainError(f"{kind} response stream was not valid JSON") from None
                continue  # the pre-answer greeting line (plan §1 tolerance)
            if not isinstance(message, dict):
                continue
            if "id" in message and message["id"] == request_id:
                self._answered = True
                error = message.get("error")
                if error:
                    # Server text NEVER echoed (untrusted; can carry the
                    # queried scripthash). Kind only.
                    raise ChainError(f"{kind} request rejected by the server")
                return message.get("result")
            # id-less notifications (headers/scripthash updates) and stale
            # ids: skipped per plan (no batching, no push consumption v1).

    def _read_line(self) -> bytes:
        """Read one newline-terminated line; clean EOF is a lost stream.

        Socket timeouts raise ``TimeoutError`` (an ``OSError``) from
        ``recv`` and bubble up as the caller's transport failure; the
        server closing the stream mid-wait is surfaced with the
        ``ConnectionResetError`` class name (the httpx-ConnectError
        analogue in the user-visible "network error (…)" surface).
        """
        sock = self._sock
        if sock is None:  # unreachable under the _rpc contract; belt-and-braces
            raise ChainError("read on a closed connection")
        while b"\n" not in self._buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise _TransportFailure("ConnectionResetError", failure_class=classify_failure(ConnectionResetError()))
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line

"""Esplora HTTP client and the UTXO-balance helper.

This module is the one legitimate user of network I/O in local-wallet
(``tools/lint_network.py`` forbids network imports anywhere else). It speaks
the Esplora API shape served by mempool.space and by self-hosted mempool /
electrs instances — the Phase 4 backend swap targets this same interface
(ADR-0003).

Backend selection (Phase 4, TCK-P4-002; ADR-0018): the base URL is resolved
from :class:`localwallet.config.Settings` in
:meth:`ChainConfig.from_settings` — the single, unambiguous selection point.
``Settings.chain_base_url`` (``LOCALWALLET_CHAIN_BASE_URL``) is authoritative
when set, so flipping the wallet onto the user's own instance is a
config-only operation and EVERY EsploraClient-mediated call (address
txs/utxos, tip, fees, price, broadcast) hits the configured instance with
zero requests to the public default. When it is unset, the legacy
``Settings.esplora_base_url`` (``LOCALWALLET_ESPLORA_BASE_URL``) is used,
preserving the ADR-0003 public default. A self-hosted URL must serve
testnet4 (ADR-0004); the client's path shapes are identical regardless of
host. A malformed selected URL fails closed with a value-free
:class:`ValueError` at construction — never a mid-request crash.

Design notes:

- Synchronous ``httpx`` client: one ``httpx.Client`` per ``EsploraClient``
  instance, context-manager closeable, identifying ``User-Agent``, no API
  keys; GET endpoints only — plus the single POST ``/tx`` broadcast
  primitive (TCK-P3-004), which is the only write this client performs.
- Bounded retries with exponential backoff + jitter, applied ONLY to
  connection errors, timeouts, HTTP 429 and 5xx — GET endpoints only.
  ``broadcast_tx`` is deliberately exempt: a POST is not idempotent, so it
  is a SINGLE attempt with zero automatic retries (see its docstring).
  Every other non-2xx status fails immediately. Exhaustion of the retry
  budget, malformed JSON, and unexpected response shapes raise
  :class:`ChainError`.
- Log-scrubbing invariant (PROJECT.md §7.8): no error string produced here
  ever contains a full address, txid, tx hex, or amount. Endpoint *kinds*
  such as ``address-utxos`` are used in messages instead of request URLs,
  and HTTP error details are limited to status codes / exception class names.
- No logging and no printing in library code.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, ClassVar, Final, Protocol, Self, runtime_checkable

import httpx
from embit.transaction import Transaction

from localwallet.chain.config import BITCOIND_SCHEME, ELECTRUM_SCHEME, ChainConfig
from localwallet.config import Settings

if TYPE_CHECKING:
    from localwallet.chain.fees import FeeTarget

__all__ = [
    "MAINNET_GENESIS_HASH",
    "Balance",
    "ChainClient",
    "ChainError",
    "EsploraClient",
    "TipBlock",
    "TxStatus",
    "balance_from_utxos",
    "check_backend",
]

# Keep in sync with the version in pyproject.toml.
_USER_AGENT = "local-wallet/0.1.0 (watch-only Bitcoin wallet)"

# Exponential backoff: base * 2**attempt, capped, plus uniform jitter.
_BACKOFF_BASE_S = 0.25
_BACKOFF_CAP_S = 8.0
_BACKOFF_JITTER_FRACTION = 0.25

# Bech32 addresses are at most 90 chars, base58 at most ~35; generous cap.
_MAX_ADDRESS_LEN = 100

# Broadcast body guard: a serialized Bitcoin transaction is at least ~60 hex
# chars (bare coinbase-shaped minimum is far below anything we broadcast,
# but the floor keeps degenerate 1-2 byte bodies out); Core's standardness
# ceiling is 100 KB of *transaction weight*, so a 100 KB hex-string cap is a
# generous transport bound (~50 KB of raw bytes — anything larger would not
# be standard-relayed anyway).
_MIN_TX_HEX_CHARS = 64
_MAX_TX_HEX_CHARS = 100_000

# Endpoint kinds used in error messages instead of URLs (which embed the
# queried address — never leak it into errors or logs).
_KIND_ADDRESS_TXS = "address-txs"
_KIND_ADDRESS_UTXOS = "address-utxos"
_KIND_TIP_HEIGHT = "tip-height"
_KIND_TIP_BLOCK = "tip-block"
_KIND_BROADCAST = "broadcast"
_KIND_TX_STATUS = "tx-status"
_KIND_BLOCKS_AT_HEIGHT = "blocks-at-height"

#: Mainnet genesis block hash — Bitcoin's protocol constant, the canonical
#: proof that a backend serves MAINNET (ADR-0021/0023 decision 5; the
#: Esplora API exposes no network-name endpoint). Public data, not user
#: data; regtest shares this genesis and is refused by the app's loopback
#: node probe at the setup layer (ui/onboarding.py), not here.
MAINNET_GENESIS_HASH: Final[str] = (
    "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"
)


class ChainError(Exception):
    """A chain-data or chain-transport failure that callers must handle.

    Raised for network errors after the retry budget is exhausted, immediate
    non-retryable HTTP failures, malformed JSON, unexpected response shapes,
    and malformed UTXO payloads (see :func:`balance_from_utxos`).

    Message contract: safe for logs and chat narration — never contains full
    addresses, txids, or amounts (log-scrubbing invariant, PROJECT.md §7.8).
    """


@dataclass(frozen=True)
class Balance:
    """Satoshi totals for a set of UTXOs, split by confirmation status.

    ``total_sats`` is derived as ``confirmed_sats + unconfirmed_sats`` and
    cannot be set independently (any caller-supplied value is overwritten in
    ``__post_init__``). Values are plain non-negative ``int`` sats.

    Raises:
        ValueError: If a component is negative or not an ``int`` (bools are
            rejected; config/value errors are programmer errors).
    """

    confirmed_sats: int
    unconfirmed_sats: int
    total_sats: int = 0  # derived; always overwritten in __post_init__

    def __post_init__(self) -> None:
        for name in ("confirmed_sats", "unconfirmed_sats"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        object.__setattr__(self, "total_sats", self.confirmed_sats + self.unconfirmed_sats)


def balance_from_utxos(utxos: list[dict[str, Any]]) -> Balance:
    """Sum an Esplora ``/address/{addr}/utxo`` payload into a :class:`Balance`.

    Entries are split on ``entry["status"]["confirmed"]`` (a strict boolean).
    This helper fails closed: any missing or malformed field raises
    :class:`ChainError` rather than silently undercounting a balance. Error
    messages name the entry index and field, never the value (amounts are
    never logged).
    """
    if not isinstance(utxos, list):
        raise ChainError("utxos payload must be a list")
    confirmed = 0
    unconfirmed = 0
    for index, entry in enumerate(utxos):
        if not isinstance(entry, dict):
            raise ChainError(f"utxo entry {index} is not an object")
        status = entry.get("status")
        if not isinstance(status, dict):
            raise ChainError(f"utxo entry {index} has missing or malformed 'status'")
        is_confirmed = status.get("confirmed")
        if not isinstance(is_confirmed, bool):
            raise ChainError(f"utxo entry {index} has missing or non-boolean 'status.confirmed'")
        value = entry.get("value")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ChainError(f"utxo entry {index} has missing or invalid 'value'")
        if is_confirmed:
            confirmed += value
        else:
            unconfirmed += value
    return Balance(confirmed_sats=confirmed, unconfirmed_sats=unconfirmed)


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter for 0-based retry ``attempt``."""
    base = min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * (2**attempt))
    return base + random.uniform(0.0, base * _BACKOFF_JITTER_FRACTION)


def _sleep_for(seconds: float) -> None:
    """Indirection over ``time.sleep`` so tests can suppress real delays."""
    time.sleep(seconds)


def _validate_address(address: str) -> None:
    """Reject address arguments that could alter the request path.

    Standard Bitcoin address encodings (base58, bech32/bech32m) are pure
    ASCII alphanumerics, so anything else is treated as an upstream bug and
    refused before a request is built (fail closed). The error message
    deliberately does not echo the address.
    """
    if (
        not isinstance(address, str)
        or not address
        or not address.isascii()
        or not address.isalnum()
        or len(address) > _MAX_ADDRESS_LEN
    ):
        raise ChainError("invalid address argument")


#: A txid is EXACTLY 64 lowercase hex characters — the same strict charset
#: the protocol layer's ``tx_status`` business rule enforces. Lowercase-only
#: is the documented contract (ADR-0002 Phase 3 extension): quoted txids
#: stay verbatim-comparable and URL-safe without normalization.
_TXID_LENGTH_CHARS = 64
_TXID_CHARSET = frozenset("0123456789abcdef")

#: Hex charset accepted for a serialized transaction body (either case is
#: valid hex for a request BODY; the injection-critical surface is the URL
#: path, which only ever carries the strictly-validated txid).
_TX_HEX_CHARSET = frozenset("0123456789abcdefABCDEF")


def _validate_txid(txid: str) -> None:
    """Reject txid arguments that could alter the request path (fail closed).

    The txid is user/model-supplied data interpolated into a GET URL path,
    so the strict charset check IS the injection guard: EXACTLY 64
    lowercase hex characters, no whitespace, no traversal fragments, no
    unicode. Refused BEFORE any URL is constructed. The error message
    deliberately does not echo the txid.
    """
    if (
        not isinstance(txid, str)
        or len(txid) != _TXID_LENGTH_CHARS
        or not set(txid) <= _TXID_CHARSET
    ):
        raise ChainError("invalid txid argument")


def _validate_tx_hex(tx_hex: str) -> None:
    """Reject tx-hex arguments that could corrupt the broadcast body.

    Guards applied BEFORE anything is sent: a non-empty string of hex
    digits (either case — this is a request body, not a URL path), even
    length (each byte is two chars), at least :data:`_MIN_TX_HEX_CHARS`
    and at most :data:`_MAX_TX_HEX_CHARS` characters. The tx hex itself is
    never echoed in errors (log-scrubbing invariant).
    """
    if (
        not isinstance(tx_hex, str)
        or not tx_hex
        or len(tx_hex) % 2 != 0
        or not _MIN_TX_HEX_CHARS <= len(tx_hex) <= _MAX_TX_HEX_CHARS
        or not set(tx_hex) <= _TX_HEX_CHARSET
    ):
        raise ChainError("invalid transaction hex argument")


@dataclass(frozen=True, slots=True)
class TxStatus:
    """Confirmation status of one transaction, quoted from the explorer.

    ``txid`` — the queried transaction id (verbatim; tool output, not an
    error string). ``confirmed`` — strict boolean from the response.
    ``block_height`` / ``block_time`` — the confirming block's height and
    timestamp, or ``None`` while unconfirmed. All fields are shape-validated
    in :meth:`EsploraClient.get_tx_status` before this record exists.
    """

    txid: str
    confirmed: bool
    block_height: int | None
    block_time: int | None


@dataclass(frozen=True, slots=True)
class TipBlock:
    """Chain-tip block info quoted from the explorer (watch narration).

    ``height`` — the tip block's height. ``timestamp`` — the tip block's
    Unix timestamp in seconds, or ``None`` when the backend did not expose
    one (e.g. the documented Esplora ``/blocks/tip`` bare-integer shape has
    no timestamp; the mempool.space block-list shape carries one). A
    ``None`` timestamp is the *clean unavailable* state — the
    time-since-block helper reports nothing rather than fabricating a value.
    Both fields are shape-validated in :meth:`EsploraClient.get_tip_block`
    before this record exists.
    """

    height: int
    timestamp: int | None


def _parse_json(response: httpx.Response, kind: str) -> Any:
    """Parse a 2xx response body as JSON; malformed bodies raise ChainError."""
    try:
        return response.json()
    except ValueError as exc:  # json.JSONDecodeError and encoding errors
        raise ChainError(f"{kind} response was not valid JSON") from exc


def _require_object_list(payload: Any, kind: str) -> list[dict[str, Any]]:
    """Enforce the expected list-of-objects response shape (fail closed)."""
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise ChainError(f"{kind} response was not a list of objects")
    return payload


def _max_block_list_height(payload: list[Any], kind: str) -> int:
    """Return the maximum ``height`` across a list of block objects (fail closed).

    Accepts only a non-empty list of dicts each carrying an integer
    ``height`` >= 0 (bools rejected). Any other entry, malformed ``height``,
    or an empty list raises :class:`ChainError`. The tip is the highest
    known block.
    """
    if not payload:
        raise ChainError(f"{kind} response was an empty block list")
    max_height = -1
    for index, entry in enumerate(payload):
        if not isinstance(entry, dict):
            raise ChainError(f"{kind} response block {index} is not an object")
        height = entry.get("height")
        if isinstance(height, bool) or not isinstance(height, int) or height < 0:
            raise ChainError(f"{kind} response block {index} has invalid 'height'")
        max_height = max(max_height, height)
    return max_height


@runtime_checkable
class ChainClient(Protocol):
    """The backend-agnostic contract every chain adapter satisfies.

    Derived from the ACTUAL call sites (docs/onb-004-backend-adapters-plan.md
    §0): ``wallet.scan``, ``chain.watch`` and the app's broadcast/recovery and
    fee paths code against exactly these members. ``EsploraClient`` and
    ``ElectrumClient`` both structurally satisfy it (pinned by
    ``tests/test_chain_electrum.py``); the TCK-ONB-004 plan's M2 ``bitcoind``
    adapter must too.

    Deliberately NOT on the protocol: ``get_json`` — raw Esplora JSON is
    Esplora-only, and every consumer that needs it (the fee floor-follower,
    the price oracle) gates on the capability seam instead (``estimate_fee``
    is the backend-native fee source; ``supports_price`` declares whether a
    price feed exists at all). Presence of ``get_json`` identifies an Esplora
    backend; absence means the honest non-Esplora degrade paths apply.
    """

    #: Whether this backend serves a USD price feed (ADR-0011 ladder input;
    #: TCK-ONB-004 plan OQ-2). ``PriceOracle`` refuses fail-closed to the
    #: sats-only rung when this is falsy.
    supports_price: bool

    def get_address_txs(self, address: str) -> list[dict[str, Any]]: ...

    def get_address_utxos(self, address: str) -> list[dict[str, Any]]: ...

    def get_tip_height(self) -> int: ...

    def get_tip_block(self) -> TipBlock: ...

    def broadcast_tx(self, tx_hex: str) -> str: ...

    def get_tx_status(self, txid: str) -> TxStatus: ...

    def estimate_fee(self, target: FeeTarget) -> int: ...

    def close(self) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class EsploraClient:
    """Synchronous client for a public or self-hosted Esplora API.

    Defaults come from :class:`localwallet.config.Settings` loaded via
    ``Settings.from_env()``, so ``LOCALWALLET_*`` environment overrides are
    honored. One ``httpx.Client`` is created per instance and closed on
    :meth:`close` or context-manager exit.

    Retry policy: connection errors, timeouts, HTTP 429 and 5xx are retried
    up to ``max_retries`` times with exponential backoff + jitter — GET
    endpoints only. Every other non-2xx status (e.g. 404) raises
    :class:`ChainError` immediately. ``broadcast_tx`` is the deliberate
    exception: a POST is not idempotent, so it runs as a SINGLE attempt
    with zero automatic retries (see its docstring). Retry exhaustion,
    malformed JSON, and unexpected response shapes also raise
    :class:`ChainError`.

    Error strings never include full addresses or txids (log-scrubbing
    invariant). No API keys are used or sent.

    TLS trust: certificate verification of https backends rides the same
    ``Settings`` resolution as ``base_url`` — ``Settings.tls_verify``
    (``LOCALWALLET_TLS_VERIFY`` / config-file key; env > file > fail-closed
    ``True``; ADR-0018 amendment, TCK-BACKEND-001). ``False`` builds the
    httpx client with verification OFF for a self-hosted backend with a
    private-CA / self-signed cert; the app prints one honest startup warning
    when that setting is active.

    Args:
        base_url: Esplora API root; when ``None`` it resolves through the
            single selection point ``ChainConfig.from_settings`` —
            ``Settings.chain_base_url`` when set (self-hosted), else the
            legacy ``Settings.esplora_base_url`` public default (ADR-0018).
        timeout_s: Per-request timeout in seconds; defaults to Settings.
        max_retries: Retries after the initial attempt; defaults to Settings.
        transport: Optional ``httpx.BaseTransport`` injection point (test
            seam; production callers leave it as ``None``).
    """

    #: This backend serves the mempool.space ``/v1/prices`` feed, so the
    #: price oracle may query it (capability seam, TCK-ONB-004 plan §0).
    supports_price: bool = True

    #: Recommended-fee payload key per confirmation target (the single
    #: native fee source for this backend; kept here as PLAIN STRINGS
    #: because ``fees.FeeTarget`` would import-cycle — the lookup uses
    #: ``target.value``).
    _RECOMMENDED_FEE_KEYS: ClassVar[dict[str, str]] = {
        "fast": "fastestFee",
        "medium": "halfHourFee",
        "slow": "hourFee",
    }

    def __init__(
        self,
        base_url: str | None = None,
        timeout_s: float | None = None,
        max_retries: int | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        defaults = ChainConfig.from_settings(Settings.from_env())
        self._config = ChainConfig(
            base_url=defaults.base_url if base_url is None else base_url,
            timeout_s=defaults.timeout_s if timeout_s is None else timeout_s,
            max_retries=defaults.max_retries if max_retries is None else max_retries,
            # TLS trust rides the SAME resolution as base_url (ADR-0018
            # amendment, TCK-BACKEND-001): no explicit argument — env >
            # config-file > fail-closed default, so a self-hosted config
            # gets URL and trust knob from one place. check_backend's probe
            # inherits the same value through this construction path.
            tls_verify=defaults.tls_verify,
        )
        if self._config.base_url.startswith(BITCOIND_SCHEME):
            # bitcoind:// is the Core-RPC adapter's scheme (TCK-ONB-004 M2;
            # ADR-0018 amendment): fail closed at construction, never a
            # nonsense httpx request (an UnsupportedProtocol crash class
            # that escapes this module's ChainError contract).
            raise ValueError("bitcoind:// URLs require the BitcoindClient adapter")
        if self._config.base_url.startswith(ELECTRUM_SCHEME):
            # ssl:// is the Electrum adapter's scheme (TCK-ONB-004 M1;
            # ADR-0018 amendment): construction fails closed here rather
            # than sending a nonsense httpx request at it later.
            raise ValueError("ssl:// URLs require the ElectrumClient adapter")
        # Trailing slash is normalized so the path joining below is exact.
        self._base_url = self._config.base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=self._config.timeout_s,
            headers={"User-Agent": _USER_AGENT},
            transport=transport,
            # httpx uses this for TLS verification of https backends; a
            # self-hosted instance with a private/self-signed cert is reached
            # ONLY when the user explicitly set this False (fail-closed
            # default True). Ignored when a ``transport`` is injected (test
            # seam carries its own SSL policy).
            verify=self._config.tls_verify,
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def get_address_txs(self, address: str) -> list[dict[str, Any]]:
        """Fetch the full transaction history for ``address``.

        ``GET {base}/address/{address}/txs``. Raises :class:`ChainError` per
        the class retry/error policy; the address is never echoed in errors.
        """
        _validate_address(address)
        payload = self._request_json(_KIND_ADDRESS_TXS, f"/address/{address}/txs")
        return _require_object_list(payload, _KIND_ADDRESS_TXS)

    def get_address_utxos(self, address: str) -> list[dict[str, Any]]:
        """Fetch the unspent outputs for ``address``.

        ``GET {base}/address/{address}/utxo``. Entries carry ``txid``,
        ``vout``, ``value``, and ``status.confirmed``; deeper per-entry
        validation happens in :func:`balance_from_utxos`.
        """
        _validate_address(address)
        payload = self._request_json(_KIND_ADDRESS_UTXOS, f"/address/{address}/utxo")
        return _require_object_list(payload, _KIND_ADDRESS_UTXOS)

    def get_tip_height(self) -> int:
        """Fetch the current chain tip height (``GET {base}/blocks/tip``).

        Mempool.space has been observed (2026-08) to serve this endpoint as
        a JSON *list* of recent block objects (each carrying an integer
        ``height``) rather than the documented bare integer, on both
        testnet4 and mainnet. We therefore tolerate both shapes: a bare
        non-negative integer (the documented Esplora shape), or a non-empty
        list of block objects whose maximum ``height`` is returned as the
        tip (highest known block). Anything else fails closed as
        :class:`ChainError` — we never fabricate or guess a height.
        """
        payload = self._request_json(_KIND_TIP_HEIGHT, "/blocks/tip")
        if isinstance(payload, bool):
            raise ChainError(f"{_KIND_TIP_HEIGHT} response was not an integer or block list")
        if isinstance(payload, int):
            parsed = payload
        elif isinstance(payload, list):
            parsed = _max_block_list_height(payload, _KIND_TIP_HEIGHT)
        else:
            raise ChainError(f"{_KIND_TIP_HEIGHT} response was not an integer or block list")
        if parsed < 0:
            raise ChainError(f"{_KIND_TIP_HEIGHT} response was a negative integer")
        return parsed

    def get_tip_block(self) -> TipBlock:
        """Fetch the chain-tip block info (``GET {base}/blocks/tip``).

        The same tolerant parsing spirit as :meth:`get_tip_height` (the
        mempool.space ``/blocks/tip`` divergence — HANDOFF §5): a bare
        non-negative integer (the documented Esplora shape) yields a
        :class:`TipBlock` with that height and ``timestamp=None``; a
        non-empty list of block objects yields the entry with the maximum
        ``height``, carrying its ``timestamp`` when present (a malformed or
        absent ``timestamp`` is the clean ``None`` unavailable state, never
        a fabricated value). Anything else fails closed as
        :class:`ChainError` — we never guess a tip or a timestamp.
        """
        payload = self._request_json(_KIND_TIP_BLOCK, "/blocks/tip")
        if isinstance(payload, bool):
            raise ChainError(f"{_KIND_TIP_BLOCK} response was not an integer or block list")
        if isinstance(payload, int):
            if payload < 0:
                raise ChainError(f"{_KIND_TIP_BLOCK} response was a negative integer")
            return TipBlock(height=payload, timestamp=None)
        if isinstance(payload, list):
            if not payload:
                raise ChainError(f"{_KIND_TIP_BLOCK} response was an empty block list")
            best_index = 0
            best_height = -1
            for index, entry in enumerate(payload):
                if not isinstance(entry, dict):
                    raise ChainError(f"{_KIND_TIP_BLOCK} response block {index} is not an object")
                height = entry.get("height")
                if isinstance(height, bool) or not isinstance(height, int) or height < 0:
                    raise ChainError(f"{_KIND_TIP_BLOCK} response block {index} has invalid 'height'")
                if height > best_height:
                    best_height = height
                    best_index = index
            entry = payload[best_index]
            timestamp = entry.get("timestamp")
            if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
                timestamp = None
            return TipBlock(height=best_height, timestamp=timestamp)
        raise ChainError(f"{_KIND_TIP_BLOCK} response was not an integer or block list")

    def broadcast_tx(self, tx_hex: str) -> str:
        """Broadcast a signed transaction (``POST {base}/tx``, single attempt).

        The ONLY write this client performs: the raw transaction hex goes
        as a ``text/plain`` body to ``{base}/tx`` (Esplora broadcast
        convention); the 2xx response body is the txid as plain text. The
        txid is re-validated against the strict 64-lowercase-hex contract
        before being returned — anything else (uppercase, wrong length,
        whitespace-padded junk, an HTML error page) is a :class:`ChainError`,
        so a caller can never record a bogus id as broadcast.

        TXID BINDING (TCK-SEC-004 change 1): the EXPECTED txid is computed
        from ``tx_hex`` itself BEFORE the request is sent, by parsing the
        serialization with embit and calling ``Transaction.txid()`` — which
        implements the consensus txid definition (witness data stripped; a
        naive ``sha256d`` of the witness-INCLUSIVE serialization would yield
        the wtxid, not the txid). The response txid is then BOUND to the
        transaction we actually sent: a well-formed but different txid (a
        misbehaving backend) is refused with a value-free :class:`ChainError`
        so a caller can never record/narrate a txid that is not this
        transaction's.

        NO-RETRY DECISION (deliberate, TCK-P3-004): a POST is not
        idempotent. If a broadcast actually reached the server but the
        response was lost, an automatic retry would broadcast a second
        time; Esplora's re-POST behavior for an already-known transaction
        is implementation-defined (txid echo or error), and the conservative
        stance is to never gamble the money path on it. This also covers
        connection errors and timeouts: httpx cannot reliably distinguish
        "request never sent" from "response lost after the server accepted
        the transaction", so EVERY failure of the single attempt — network
        error, 429, 5xx, anything — surfaces immediately as
        :class:`ChainError` carrying only the endpoint kind, the status
        code, or the exception class name. Callers recover by checking
        ``get_tx_status`` for the intended transaction instead of
        re-broadcasting blindly (rate-limit tradeoff noted against R11:
        the caller-visible recovery path is a cheap GET, not a POST).

        Args:
            tx_hex: The signed transaction, serialized as hex (charset-,
                parity- and length-guarded by :func:`_validate_tx_hex`
                BEFORE anything is sent; never echoed in errors).

        Returns:
            The txid of the broadcast transaction, verbatim from the
            response (64 lowercase hex, whitespace-stripped).

        Raises:
            ChainError: malformed argument, a ``tx_hex`` that does not parse
                as a transaction, any non-2xx status (single attempt, no
                retries), transport failure, a response body that does not
                re-validate as a txid, or a well-formed response txid that
                does not match the broadcast transaction.
        """
        _validate_tx_hex(tx_hex)
        # Compute the expected txid from the serialization BEFORE anything
        # is sent (fail fast — an unparseable transaction never reaches the
        # network). embit's ``txid()`` strips witness data per the consensus
        # txid definition; the hex form is lowercase by construction.
        try:
            expected_txid = Transaction.parse(bytes.fromhex(tx_hex)).txid().hex()
        except Exception as exc:  # containment: embit parse errors vary
            raise ChainError(
                f"{_KIND_BROADCAST} invalid transaction hex: not a parseable transaction"
            ) from exc
        url = f"{self._base_url}/tx"
        try:
            response = self._client.post(
                url, content=tx_hex.encode("ascii"), headers={"Content-Type": "text/plain"}
            )
        except httpx.TransportError as exc:
            raise ChainError(f"{_KIND_BROADCAST} failed: network error ({type(exc).__name__})") from exc
        status = response.status_code
        if not 200 <= status < 300:
            # Single attempt for EVERY non-2xx — including 429/5xx (no-retry decision above).
            raise ChainError(f"{_KIND_BROADCAST} failed: status {status}")
        reported = response.text.strip()
        if len(reported) != _TXID_LENGTH_CHARS or not set(reported) <= _TXID_CHARSET:
            raise ChainError(f"{_KIND_BROADCAST} response was not a valid transaction id")
        # Bind the reported txid to the transaction we actually sent
        # (TCK-SEC-004 change 1). Both sides are lowercase hex here (the
        # charset contract above; ``txid().hex()`` by construction), so a
        # direct comparison is the case-insensitive-safe check. Value-free
        # detail: neither txid is echoed.
        if reported != expected_txid:
            raise ChainError(
                f"{_KIND_BROADCAST} response txid does not match the broadcast transaction"
            )
        return reported

    def get_tx_status(self, txid: str) -> TxStatus:
        """Fetch a transaction's confirmation status (``GET {base}/tx/{txid}/status``).

        The response is shape-validated fail-closed: ``confirmed`` must be
        a strict boolean, ``block_height``/``block_time`` non-negative
        integers or ``null`` (bools rejected). An unknown txid surfaces as
        the client's ordinary non-2xx handling (404 → :class:`ChainError`,
        value-free) — callers that want "unconfirmed" semantics for unknown
        ids decide that at the handler layer, never here.

        Args:
            txid: The transaction id, validated by :func:`_validate_txid`
                (EXACTLY 64 lowercase hex) BEFORE the URL is constructed —
                the charset check is the injection guard for this
                user/model-supplied path component.

        Raises:
            ChainError: malformed argument, non-2xx status per the GET
                retry policy, malformed JSON, or an unexpected shape.
        """
        _validate_txid(txid)
        payload = self._request_json(_KIND_TX_STATUS, f"/tx/{txid}/status")
        if not isinstance(payload, dict):
            raise ChainError(f"{_KIND_TX_STATUS} response was not an object")
        confirmed = payload.get("confirmed")
        if not isinstance(confirmed, bool):
            raise ChainError(f"{_KIND_TX_STATUS} response has missing or non-boolean 'confirmed'")
        parsed: list[int | None] = []
        for name in ("block_height", "block_time"):
            value = payload.get(name)
            if value is None:
                parsed.append(None)
            elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ChainError(f"{_KIND_TX_STATUS} response has invalid '{name}'")
            else:
                parsed.append(value)
        return TxStatus(
            txid=txid,
            confirmed=confirmed,
            block_height=parsed[0],
            block_time=parsed[1],
        )

    def estimate_fee(self, target: FeeTarget) -> int:
        """Backend-native single fee bid in sat/vB (``ChainClient`` contract).

        Source: ``GET {base}/v1/fees/recommended`` (mempool.space shape), the
        key matching ``target``. Strictly validated (positive ``int``, bools
        rejected — a 0 sat/vB bid is a broken payload, never a free one;
        same fail-closed rule as :func:`localwallet.chain.fees._parse_recommended`).
        Note: the app's fee path for THIS backend goes through
        :class:`~localwallet.chain.fees.FeeEstimator`, which prefers its
        richer floor-follower over this single-source endpoint; this method
        exists so the Esplora client satisfies the same protocol as every
        other adapter.
        """
        key = self._RECOMMENDED_FEE_KEYS[target.value]
        payload = self._request_json("fees-recommended", "/v1/fees/recommended")
        if not isinstance(payload, dict):
            raise ChainError("fees-recommended response was not an object")
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ChainError(f"fees-recommended response has missing or invalid '{key}'")
        return value

    def get_json(self, path: str, kind: str) -> Any:
        """Public GET + retry + parse for any path on this client.

        Generalized entry point so sibling adapters (fees, price) reuse the
        same transport, retry policy, and fail-closed parse without creating
        a second ``httpx.Client``. ``path`` must be a ``/``-rooted URL path
        (e.g. ``"/v1/fees/recommended"``); ``kind`` is the log-scrubbed
        endpoint name used in error messages instead of the URL.

        Retry/error behaviour matches :meth:`_request_json`: connection
        errors, timeouts, 429 and 5xx are retried with bounded backoff; all
        other non-2xx statuses, retry exhaustion, and malformed JSON raise
        :class:`ChainError` carrying only the kind/status (no URLs).

        Raises:
            ChainError: If ``path`` is malformed or the request/parse fails.
        """
        if not isinstance(path, str) or not path.startswith("/"):
            raise ChainError("invalid request path")
        return self._request_json(kind, path)

    def _request_json(self, kind: str, path: str) -> Any:
        """GET ``{base}{path}`` under the retry policy; return parsed JSON.

        Retries only connection errors/timeouts (``httpx.TransportError``),
        HTTP 429, and 5xx — with exponential backoff + jitter. Other
        non-2xx statuses raise immediately. All failure surfaces end as
        :class:`ChainError` carrying only the endpoint kind, status code,
        or exception class name (no addresses/txids).
        """
        url = f"{self._base_url}{path}"
        last_failure = "no attempt completed"
        for attempt in range(self._config.max_retries + 1):
            try:
                response = self._client.get(url)
            except httpx.InvalidURL:
                # Request-time URL breakage that ChainConfig's shape check
                # cannot see (e.g. a non-numeric port: ``http://h:port``).
                # Deterministic — every attempt fails identically, so no
                # retry — and it ends as this method's contract-promised
                # value-free ChainError: the raw httpx exception (whose own
                # message can carry a URL fragment) never escapes to the
                # caller's thread (TCK-ONB-003 review, finding 1).
                raise ChainError(f"{kind} request failed: invalid base URL") from None
            except httpx.TransportError as exc:
                # Connection errors and timeouts are the retryable class.
                last_failure = f"network error ({type(exc).__name__})"
            else:
                status = response.status_code
                if 200 <= status < 300:
                    return _parse_json(response, kind)
                if status == 429 or status >= 500:
                    last_failure = f"status {status}"
                else:
                    # 4xx (rate limiting aside) and anything else: fail now.
                    raise ChainError(f"{kind} request failed: status {status}")
            if attempt < self._config.max_retries:
                _sleep_for(_backoff_delay(attempt))
        raise ChainError(
            f"{kind} request failed after {self._config.max_retries} retries: {last_failure}"
        )


def check_backend(
    base_url: str,
    *,
    timeout_s: float = 10.0,
    max_retries: int = 0,
    transport: httpx.BaseTransport | None = None,
) -> bool:
    """Probe a candidate self-hosted backend (ADR-0023 decision 5).

    ``True`` only when the URL (a) constructs through the fail-closed
    :class:`ChainConfig` shape check (well-formed http(s), no userinfo),
    (b) answers in Esplora shape (the strict tip-height parse proves
    reachability *and* the API family), and (c) serves **mainnet** — its
    block-height-0 list must contain a block whose hash is
    :data:`MAINNET_GENESIS_HASH` (canonical Esplora entries are block
    objects carrying ``"id"``; a bare hash string is tolerated). A
    testnet/other-network instance fails (c) and is refused (ADR-0021).

    Every failure collapses to ``False`` — construction, transport, HTTP,
    parse and shape alike, including any httpx request error outside the
    :class:`ChainError` surface (an escaping exception would kill the
    engine pump this runs on; TCK-ONB-003 review, finding 1). The caller
    owns the one honest user-facing message, so no URL, host, status, or
    exception detail ever leaves this function (value-free by
    construction). Retries are pointless for a setup probe of a server the
    user just pointed at — default ``max_retries=0`` keeps the prompt
    snappy; the caller may raise it.

    ``transport`` is the standard test seam; production passes ``None``.
    TLS trust is NOT a parameter here: the probe shares the client's ladder
    (env > config file > fail-closed default), so a self-hosted backend with
    a self-signed cert is only reachable through an explicit
    ``LOCALWALLET_TLS_VERIFY=0`` that also drives the real client — the check
    and the wallet can never disagree on transport policy.
    """
    try:
        client = EsploraClient(
            base_url=base_url,
            timeout_s=timeout_s,
            max_retries=max_retries,
            transport=transport,
        )
    except ValueError:
        return False  # malformed URL: ChainConfig failed closed at construction
    try:
        client.get_tip_height()
        blocks = client.get_json("/blocks/0", _KIND_BLOCKS_AT_HEIGHT)
    except (ChainError, httpx.InvalidURL):
        # The whole request surface collapses to False — ChainError covers
        # transport/HTTP/JSON/shape failures, and httpx.InvalidURL is
        # belt-and-braces for request-time URL breakage (non-numeric port)
        # that _request_json also converts; the contract here is that
        # NOTHING escapes to the caller (finding 1).
        return False
    finally:
        client.close()
    if not isinstance(blocks, list):
        return False
    # Esplora's /blocks/<height> serves block OBJECTS whose "id" is the
    # block hash (the same canonical shape :meth:`EsploraClient.get_tip_block`
    # parses); a bare-hash list is tolerated leniently. Matching the raw
    # entries rejected every genuine object-shaped mainnet backend
    # (TCK-ONB-003 review, finding 2).
    ids = [b.get("id") if isinstance(b, dict) else b for b in blocks]
    return MAINNET_GENESIS_HASH in ids

"""Esplora HTTP client and the UTXO-balance helper.

This module is the one legitimate user of network I/O in local-wallet
(``tools/lint_network.py`` forbids network imports anywhere else). It speaks
the Esplora API shape served by mempool.space and by self-hosted mempool /
electrs instances — the Phase 4 backend swap targets this same interface
(ADR-0003).

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
from typing import Any, Self

import httpx

from localwallet.chain.config import ChainConfig
from localwallet.config import Settings

__all__ = ["Balance", "ChainError", "EsploraClient", "TxStatus", "balance_from_utxos"]

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
_KIND_BROADCAST = "broadcast"
_KIND_TX_STATUS = "tx-status"


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

    Args:
        base_url: Esplora API root; defaults to the Settings default
            (``https://mempool.space/testnet4/api``).
        timeout_s: Per-request timeout in seconds; defaults to Settings.
        max_retries: Retries after the initial attempt; defaults to Settings.
        transport: Optional ``httpx.BaseTransport`` injection point (test
            seam; production callers leave it as ``None``).
    """

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
        )
        # Trailing slash is normalized so the path joining below is exact.
        self._base_url = self._config.base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=self._config.timeout_s,
            headers={"User-Agent": _USER_AGENT},
            transport=transport,
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

    def broadcast_tx(self, tx_hex: str) -> str:
        """Broadcast a signed transaction (``POST {base}/tx``, single attempt).

        The ONLY write this client performs: the raw transaction hex goes
        as a ``text/plain`` body to ``{base}/tx`` (Esplora broadcast
        convention); the 2xx response body is the txid as plain text. The
        txid is re-validated against the strict 64-lowercase-hex contract
        before being returned — anything else (uppercase, wrong length,
        whitespace-padded junk, an HTML error page) is a :class:`ChainError`,
        so a caller can never record a bogus id as broadcast.

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
            ChainError: malformed argument, any non-2xx status (single
                attempt, no retries), transport failure, or a response
                body that does not re-validate as a txid.
        """
        _validate_tx_hex(tx_hex)
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

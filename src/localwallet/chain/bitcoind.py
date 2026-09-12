"""Bitcoin Core RPC chain client (TCK-ONB-004 M2; ADR-0018 amendment).

The third :class:`~localwallet.chain.esplora.ChainClient` implementation: a
synchronous Bitcoin Core JSON-RPC client over stdlib ``http.client`` —
plain http on the ``bitcoind://`` scheme (Core RPC's loopback form), and
since TCK-BACKEND-003 (ADR-0018 amendment), https on the ``bitcoind+tls://``
sibling (Core's ``-rpcssl`` or, the common shape, a TLS reverse proxy —
Start9 etc.). The transport bit is part of the scheme so a stored URL
rebuilds the identical client at every later launch; TLS trust on the TLS
sibling rides the SAME ``tls_verify`` ladder as the httpx adapters (fail-
closed default True; ``LOCALWALLET_TLS_VERIFY=0`` reaches a private-CA /
self-signed node, and the app's honest value-free startup warning fires
there exactly as it does for Esplora). Selected by the URL scheme at the
single construction point (``ChainConfig.from_settings`` →
``app._build_chain_client``); http(s) keeps Esplora, ``ssl://`` keeps
Electrum (docs/onb-004-backend-adapters-plan.md §2, M2).

Ticket deviation from plan §2's "use httpx" line: the M2 brief mandates the
stdlib transport ("stdlib ``http.client`` within chain/ rules"), and plan
§2 itself allows urllib/socket inside ``chain/`` — http.client is the stdlib
form of exactly that allowance. One RPC per connection (Core closes its HTTP
connections freely anyway) keeps framing trivially safe: no stream position
can bleed between requests; the electrum adapter's reconnect discipline
reduces to "the next attempt opens a fresh socket".

Auth (HTTP Basic, resolved per request, in the plan's order):

1. user/pass — the ``bitcoind://user:pass@host:port`` (or
   ``bitcoind+tls://user:pass@host:port``) URL userinfo (the
   env/config-file rungs only: the store's write validation refuses
   embedded credentials, and M3 adds the dedicated settings keys), or the
   explicit ``rpc_user``/``rpc_password`` constructor pair (library seam);
2. the cookie file — ``Settings.rpc_cookie_path``
   (``LOCALWALLET_RPC_COOKIE_PATH`` / config-file key; empty means the
   data-dir default ``~/.bitcoin/.cookie``, the SAME ladder semantics the
   node doctor documents; the file is local, so this source only works for
   a same-machine node — remote nodes use user/pass). Re-read on every
   request (bounded size cap) so a Core restart rotating the cookie
   self-heals without a reconfigure;
3. no credentials — the ``Authorization`` header is OMITTED entirely (the
   plan's "no credentials needed" semantics, for an open local RPC).

The cookie CONTENT and any password are secrets: never logged, never
returned, never echoed in an error (value-free discipline; the header is
built from the raw bytes without them ever passing through a message).

Mainnet gate (ADR-0021): the FIRST call on a client instance runs the
handshake — ``getblockchaininfo`` whose ``chain`` MUST be ``"main"`` (a
testnet/signet/regtest node is refused value-free) plus ``getnetworkinfo``
whose ``version`` must clear 220000 (capability honesty: verbose
``getrawtransaction`` only carries the ``prevout`` objects the input-address
mapping below needs from Core 22 on — an old node would silently
mis-attribute an outgoing spend as incoming, so it is refused instead).

The scan problem — the plan's option (a), watch-only exact:

* ``scantxoutset("start", ["raw(<script hex>)", ...])`` takes ONLY bare
  output-script descriptors — built here from the addresses ``scan.py``
  hands us, our own receive/change scripts, and NEVER the ``desc(...)``
  wrapper (that is Core's OUTPUT form; the input grammar refuses it,
  TCK-BACKEND-004; see :meth:`BitcoindClient._scan_snapshot`);
  ``importprivkey``/``importdescriptors`` are NEVER called: no keys ever
  touch the node wallet. The returned UTXO snapshot answers
  :meth:`get_address_utxos` directly (``amount`` BTC → exact-satoshi
  ``value`` via Decimal; ``height > 0`` → confirmed).
* :meth:`get_address_txs` assembles what history the data allows: the
  funding transactions of the address's UNSPENT outputs (one verbose
  ``getrawtransaction`` per distinct txid). **Documented limitation (plan
  §2; OQ-1 default):** Core without a wallet/address-index cannot
  enumerate spent history — ``scantxoutset`` sees ONLY UNSPENT outputs, so
  fully-spent addresses surface NO history, and mempool outputs never
  enter the UTXO set either (unconfirmed coins are honest absence:
  ``getrawtransaction``'s mempool flag decodes known txids but discovers
  nothing). A rescan widens the descriptor set the way the gap window
  does elsewhere; nothing is ever fabricated. Users needing full spent
  history point the backend at Esplora/electrum.
* One ``scantxoutset`` walks the WHOLE UTXO set, so the answer is cached
  as one snapshot per tip height: every script asked about so far (all
  ours) accumulates into the descriptor set of the next walk, and a cached
  hit is validated by one cheap ``getblockchaininfo`` per probe. The walk
  is SYNCHRONOUS server-side (Core answers only after it completes), so
  the start call runs with the dedicated :data:`_SCAN_TIMEOUT_S` budget and
  ZERO retries — see the constant for the timeout→retry→"Scan already in
  progress" trap that pairing fixes. Ceiling
  (``ponytail:`` comment at the cache): a same-height reorg between probes
  keeps the stale snapshot until the next block lands — watch polls make
  that window minutes on a live node.

Contract mapping (plan §2 table): ``getblockchaininfo`` → tip height
(``.blocks``; ``blocks < headers`` = still syncing is NOT a tip error — the
height is honest chain-truth-so-far; sync-progress narration stays the
app's watch surface, ADR-0023 decision 5); ``bestblockhash`` → verbose
``getblockheader`` → :class:`TipBlock` (``time``; absent/malformed →
``None`` clean-unavailable, never fabricated); verbose
``getrawtransaction`` → :class:`TxStatus` (``confirmations > 0``;
``blockheight``/``blocktime`` only when confirmed — a mempool tx's
first-seen ``time`` is deliberately NOT reported as a block time, the
electrum parity rule); ``sendrawtransaction`` → :meth:`broadcast_tx` with
the SAME single-attempt + embit txid-binding semantics as every other
adapter (TCK-SEC-004 change 1); ``estimatesmartfee`` →
:meth:`estimate_fee` (targets FAST=1, MEDIUM=2, SLOW=6 blocks in
``CONSERVATIVE`` mode — the wallet bids its own money, so it takes the
over-bid-safe answer; the tx engine's min-relay floor stays the binding
lower rail; a warmup answer without ``feerate`` fails closed, never
fabricated). ``supports_price = False``: Core serves no price oracle — the
existing gate refuses USD-denominated ``create_tx`` fail-closed to the
sats-only rung (plan OQ-2).

Failure discipline mirrors Esplora/Electrum exactly: transport losses
(OSError, ``http.client`` protocol errors, HTTP 429/5xx WITHOUT a JSON-RPC
error envelope) retry read-only calls with the SHARED backoff policy; auth
refusal (401/403), other non-2xx, malformed JSON, error envelopes and
malformed shapes raise immediately with value-free :class:`ChainError`
messages (endpoint *kinds* only — the server's own error text is untrusted
input that can embed txids/amounts and is NEVER echoed; a JSON-RPC error
envelope instead classifies as ``rpc-error`` and carries its NUMERIC code,
a protocol constant — TCK-DIAG-002). No host, address, txid, credential or
amount ever leaves this module in an error string. No
logging, no printing.
"""

from __future__ import annotations

import base64
import http.client
import json
import ssl
import threading
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self
from urllib.parse import unquote, urlsplit

from embit.script import Script, address_to_scriptpubkey
from embit.transaction import Transaction

from localwallet.chain.config import ChainConfig
from localwallet.chain.esplora import (
    _TXID_CHARSET,
    _TXID_LENGTH_CHARS,
    AUTH_REQUIRED,
    HTTP_STATUS,
    NOT_MAINNET,
    RPC_ERROR,
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

__all__ = ["BitcoindClient"]

#: Standard mainnet RPC port, used when the URL omits one (ADR-0021:
#: mainnet-only, so this is the ONLY default — testnet 18332 / regtest
#: 18443 / signet 38332 never apply here).
_DEFAULT_RPC_PORT: Final[int] = 8332

#: The ONE network name Core reports for mainnet in ``getblockchaininfo``.
_MAINNET_CHAIN: Final[str] = "main"

#: Capability floor (see module docstring): verbose ``getrawtransaction``
#: carries input ``prevout`` scriptPubKeys from Bitcoin Core 22.0 on
#: (encoded version 220000); without them an outgoing spend would
#: silently mis-attribute as incoming, so old nodes are refused instead.
_MIN_CORE_VERSION: Final[int] = 220_000

#: Cookie files are one short ``user:pass`` line; anything bigger is not a
#: cookie (bounded read, fail closed — a huge file or wrong path is
#: refused without ever reading or echoing its content).
_MAX_COOKIE_BYTES: Final[int] = 512

#: Response body ceiling. Core's own RPC limit is 128 MiB in; for OUR reads
#: the big ones are ``scantxoutset`` snapshots (~150 JSON bytes per coin) —
#: 64 MiB covers ~400k coins against the 2000-address scan-window ceiling
#: (TCK-SEC-002) with orders of magnitude to spare. A larger body is a
#: broken/evil server, not our wallet.
_MAX_RESPONSE_BYTES: Final[int] = 64 * 1024 * 1024

#: Timeout budget for the ``scantxoutset("start", ...)`` walk, INDEPENDENT
#: of the generic per-request timeout (TCK-BACKEND-004 fix d, pinned):
#: Core's start action answers only AFTER the scan completes (its own RPC
#: contract) — a full-UTXO-set walk is minutes-class on real mainnet
#: hardware (it walks the coins-DB cursor; the coinstatsindex does NOT
#: speed scantxoutset up). The generic
#: ``request_timeout_s`` (default 10 s) guaranteed a mid-scan read timeout,
#: and the retry loop then re-sent ``start`` while the first scan still
#: held the server-side reserver — Core refused the duplicate with the
#: documented "Scan already in progress" RPC error. That client-created
#: timeout→retry→rejection loop WAS the user's "request rejected by the
#: server" (MW-16 round 2; permissions proven fine by their direct
#: ``status`` call). 30 minutes is a BOUNDED ceiling (a wedged server
#: cannot hang the worker forever) on the minutes-class realistic worst
#: case for a small descriptor set; the scan call runs with retries=0, so
#: a genuine loss surfaces honestly ONCE as a timeout-class failure rather
#: than as a self-inflicted rpc-error.
_SCAN_TIMEOUT_S: Final[float] = 1800.0

#: Our confirmation targets → ``estimatesmartfee`` block targets (plan §2;
#: the mapping is documented there; Core clamps targets beyond its
#: estimate window server-side, and 1/2/6 are always inside it).
_ESTIMATESMARTFEE_TARGETS: Final[dict[FeeTarget, int]] = {
    FeeTarget.FAST: 1,
    FeeTarget.MEDIUM: 2,
    FeeTarget.SLOW: 6,
}

#: BTC/kB → sat/vB (1e8 sats / 1e3 bytes) and BTC → sats, as exact Decimals.
_SAT_VB_PER_BTC_KB: Final[Decimal] = Decimal(100_000)
_SATS_PER_BTC: Final[Decimal] = Decimal(100_000_000)

#: Sanity ceiling for a server's feerate answer (BTC/kB) — the electrum
#: adapter's ``_MAX_FEE_BTC_KB`` twin: a broken/evil payload guard, not a
#: fee policy (10 000 sat/vB is nonsense).
_MAX_FEE_BTC_KB: Final[Decimal] = Decimal("0.1")

# Endpoint kinds used in error messages INSTEAD of anything the server or
# the request carried (log-scrubbing invariant, PROJECT.md §7.8). The
# shared kinds reuse the exact names the Esplora/Electrum clients emit so
# caller-visible error shapes do not depend on the backend.
_KIND_GATE = "handshake"
_KIND_CAPABILITY = "handshake-capabilities"
_KIND_ADDRESS_TXS = "address-txs"
_KIND_ADDRESS_UTXOS = "address-utxos"
_KIND_UTXO_SCAN = "utxo-scan"
_KIND_TIP_HEIGHT = "tip-height"
_KIND_TIP_BLOCK = "tip-block"
_KIND_BROADCAST = "broadcast"
_KIND_TX_STATUS = "tx-status"
_KIND_FEE_ESTIMATE = "fee-estimate"

#: HTTP statuses that mean "your credentials did not pass" (value-free
#: refusal regardless of which source supplied the header).
_AUTH_STATUSES: Final[frozenset[int]] = frozenset({401, 403})

# Keep in sync with the version in pyproject.toml (the SAME client identity
# string the Esplora/Electrum clients send).
_USER_AGENT: Final[str] = "local-wallet/0.1.0 (watch-only Bitcoin wallet)"

#: Hex check for block hashes riding params (same charset contract as
#: txids — a malformed hash from the server is never sent back to it).
_HEX64: Final[frozenset[str]] = frozenset("0123456789abcdef")


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


def _btc_to_sats(value: Any, kind: str, field: str) -> int:
    """Convert a JSON BTC amount (Decimal/int, never float) to sats.

    Responses are parsed with ``parse_float=Decimal``, so a wire amount
    like ``0.00009000`` becomes an EXACT Decimal; the satoshi product must
    be integral or the payload is malformed (fail closed — a truncated
    float-derived value would silently mis-state a balance). Errors name
    only the kind and the field, never the value (amounts are never
    logged).
    """
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise ChainError(f"{kind} response has a missing or malformed '{field}'")
    sats = Decimal(value) * _SATS_PER_BTC
    if sats != sats.to_integral_value():
        raise ChainError(f"{kind} response '{field}' is not a whole number of satoshis")
    total = int(sats)
    if total < 0:
        raise ChainError(f"{kind} response '{field}' is negative")
    return total


def _script_address(spk: Any) -> str | None:
    """Best-effort address for one Core scriptPubKey object.

    Prefers the server's ``address``/``addresses`` fields (present for any
    standard script) and falls back to DERIVING the address from the raw
    ``hex`` with embit. Anything unmappable (OP_RETURN, nonstandard,
    malformed) is simply absent — ``scan._addresses_from_io`` treats a
    missing address as "contributes nothing", the same tolerance the
    electrum adapter applies. Never raises, value-free.
    """
    if not isinstance(spk, dict):
        return None
    address = spk.get("address")
    if isinstance(address, str) and address:
        return address
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


def _rpc_error_code(envelope: Any) -> int | None:
    """The NUMERIC code of a JSON-RPC error member, or ``None`` when absent
    or malformed. RPC codes are protocol constants (``-8``
    INVALID_PARAMETER, ``-34`` already-in-progress…) — TCK-DIAG-002 deems
    them safe to carry on the value-free debug line; the error's TEXT is
    untrusted server data and never leaves this module."""
    error = envelope.get("error") if isinstance(envelope, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return code


def _rpc_rejected(kind: str, envelope: Any) -> ChainError:
    """The shared refusal for a JSON-RPC error envelope: the value-free
    ``rpc-error`` class (TCK-DIAG-002 — the taxonomy slot these rejections
    used to collapse into network-error), ``RPCError`` as the debug
    exception name, and the numeric code carried for the app's line."""
    return ChainError(
        f"{kind} request rejected by the server",
        failure_class=RPC_ERROR,
        exc_name="RPCError",
        rpc_code=_rpc_error_code(envelope),
    )


def _constant_rejected(constant: str) -> Decimal:
    """Reject NaN/Infinity in a JSON payload (fail closed).

    ``json.loads(parse_constant=...)`` routes the non-standard JS literals
    here; returning anything would smuggle non-finite numbers into money
    math, so this raises and the whole body collapses as malformed JSON.
    """
    raise ValueError(f"unsupported JSON constant: {constant}")


class _TransportRetry(Exception):
    """Internal marker: this failure is the RETRYABLE class — exactly the
    httpx ``TransportError``/429/5xx set of the Esplora policy. Every raise
    site carries a value-free surface string (exception CLASS name or HTTP
    status code); the marker itself never escapes to callers. ``failure``
    is the TCK-DIAG-001 value-free failure-class + exception-name pair
    surfaced by the retry handler."""

    def __init__(self, surface: str, *, failure_class: str | None = None, exc_name: str | None = None) -> None:
        super().__init__(surface)
        self.failure_class = failure_class
        self.exc_name = exc_name


class BitcoindClient:
    """Synchronous Bitcoin Core JSON-RPC client (HTTP Basic; plain http on
    ``bitcoind://``, https on the ``bitcoind+tls://`` sibling — TCK-BACKEND-003).

    Satisfies :class:`~localwallet.chain.esplora.ChainClient`. Construction
    is network-free (the fail-closed handshake runs on the first call); the
    app's ONE construction site (``app._build_chain_client``, fed by
    ``ChainConfig.from_settings``) passes the selected URL + scalars
    explicitly; a direct construction without them falls back to the env/
    config-file ``chain_base_url`` rung with the same merged ``Settings``
    timeout/retry/TLS scalars, and ``Settings.rpc_cookie_path`` (env >
    config-file > data-dir default, ladder unchanged, ADR-0018 amendment).
    ``tls_verify`` rides the
    construction ONLY for the TLS sibling: the plain-http transport has no
    TLS layer to trust or downgrade, the https transport verifies by
    default and is downgraded only by the explicit env/file rung (the app
    prints its honest startup warning there, backend-agnostic as ever).

    Retry policy (consistent with Esplora/Electrum): transport failures and
    bare 429/5xx retry up to ``max_retries`` with the shared exponential
    backoff + jitter (read-only calls); auth refusal, other statuses,
    malformed payloads and node rejections raise immediately.
    ``broadcast_tx`` is the deliberate single-attempt exception (a
    re-send is never automatic — callers recover via ``get_tx_status``,
    exactly the Esplora contract).

    Raises:
        ValueError: at construction if the resolved URL is not a well-formed
            ``bitcoind://`` or ``bitcoind+tls://`` endpoint (fail closed,
            value-free).
    """

    #: No price feed on a Bitcoin Core node (plan OQ-2): the price oracle
    #: refuses fail-closed to the sats-only rung on this backend.
    supports_price: bool = False

    def __init__(
        self,
        base_url: str | None = None,
        timeout_s: float | None = None,
        max_retries: int | None = None,
        *,
        rpc_user: str | None = None,
        rpc_password: str | None = None,
        rpc_cookie_path: str | Path | None = None,
        no_credentials: bool = False,
    ) -> None:
        settings = Settings.from_env()
        # TCK-DESCOPE-M3A decoupling (mirrors ElectrumClient/EsploraClient):
        # the SCALAR defaults come straight from the merged env/file
        # Settings — NOT ``ChainConfig.from_settings`` (which now refuses an
        # empty ``chain_base_url`` as UNRESOLVED and would spuriously raise
        # when the caller supplies an explicit base_url the fresh env read
        # cannot see, e.g. a stored-rung probe). An omitted ``base_url`` with
        # an empty ``chain_base_url`` still fails closed in
        # ``ChainConfig.__post_init__``.
        self._config = ChainConfig(
            base_url=settings.chain_base_url if base_url is None else base_url,
            timeout_s=settings.request_timeout_s if timeout_s is None else timeout_s,
            max_retries=settings.max_retries if max_retries is None else max_retries,
            # TCK-BACKEND-003: the ladder rides through, exactly as the
            # Esplora/Electrum constructions do it. inert on the plain-http
            # scheme (no TLS layer); decides certificate trust on the
            # bitcoind+tls:// sibling. There is no per-call override seam:
            # probe and live client can never disagree on transport policy.
            tls_verify=settings.tls_verify,
        )
        parsed = urlsplit(self._config.base_url)
        if parsed.scheme not in ("bitcoind", "bitcoind+tls"):
            raise ValueError("BitcoindClient requires a bitcoind:// (or bitcoind+tls://) URL")
        self._tls = parsed.scheme == "bitcoind+tls"
        # One server TLS context per client (immutable after construction —
        # the ladder cannot change mid-session). Verified by default; the
        # explicit LOCALWALLET_TLS_VERIFY=0 / config-file rung disables
        # verification for private-CA / self-signed nodes, mirroring the
        # httpx adapters' behaviour (and the app's honest startup warning
        # fires alongside it, value-free).
        self._ssl_context: ssl.SSLContext | None = None
        if self._tls:
            context = ssl.create_default_context()
            if not self._config.tls_verify:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            self._ssl_context = context
        if not isinstance(no_credentials, bool):
            raise ValueError("no_credentials must be a boolean")  # noqa: TRY004
        if no_credentials and (
            parsed.username is not None
            or rpc_user is not None
            or rpc_password is not None
        ):
            # Contradictory construction: an explicit login AND "omit the
            # Authorization header entirely". The app's resolver never does
            # this (its interplay rule: the checkbox wins over empty/absent
            # fields, and a filled pair UNCHECKS the box); a caller that
            # hits it is a programmer error — fail closed, value-free.
            raise ValueError("no_credentials contradicts explicit credentials")
        self._host: str = parsed.hostname  # host presence: ChainConfig
        self._port: int = parsed.port or _DEFAULT_RPC_PORT
        # Auth source 1: user/pass — URL userinfo (the env/config-file rung
        # of the single selection point; percent-encoded parts are decoded
        # back to what Core's rpcuser/rpcpassword actually are), else the
        # explicit constructor pair (the library seam; TCK-ONB-004 M3's
        # ``backend_auth_user``/``backend_auth_pass`` settings keys thread
        # through it — never logged, never echoed). Static for the client's
        # life.
        self._basic: str | None = None
        if parsed.username is not None:
            self._basic = self._encode_basic(
                f"{unquote(parsed.username)}:{unquote(parsed.password or '')}"
            )
        elif rpc_user is not None and rpc_password is not None:
            self._basic = self._encode_basic(f"{rpc_user}:{rpc_password}")
        # Auth source 2: the cookie file, resolved through the SAME ladder
        # the node doctor documents (arg > LOCALWALLET_RPC_COOKIE_PATH /
        # config-file key > the empty-string default "~/.bitcoin/.cookie",
        # whose documented meaning is the mainnet data-dir root; ADR-0021
        # knows no other network). Read per request; a missing/unusable
        # line degrades to source 3 (no Authorization header at all — a
        # node that demands auth answers that honestly with a 401).
        cookie_file: Path | None
        if no_credentials:
            # The settings checkbox (TCK-ONB-004 M3): explicit
            # "no credentials needed" — auth source 3, the header is omitted
            # for every request and the cookie file is NEVER consulted.
            cookie_file = None
        else:
            cookie = (
                str(rpc_cookie_path)
                if rpc_cookie_path is not None
                else settings.rpc_cookie_path
            )
            cookie_file = Path(cookie) if cookie.strip() else (
                Path.home() / ".bitcoin" / ".cookie"
            )
        self._cookie_file = cookie_file
        # Re-entrant: contract methods take it around a helper chain that
        # re-enters (_scan_snapshot → get_tip_height → gate). The lock
        # serializes every RPC exactly the way the electrum adapter
        # serializes its one socket (engine thread + ChainWorker share the
        # client; ponytail: global lock, per-request connections if a real
        # node ever measurably contends).
        self._lock = threading.RLock()
        self._gated = False
        # UTXO-scan snapshot cache: {script hex -> Esplora-shaped entries}
        # taken at _snapshot_height, plus the script SET the cached walk
        # actually covered (a hit requires the queried script to be IN that
        # set — an address first seen after the walk must trigger a fresh
        # walk, never a silent "no coins"). Every script is one the app
        # asked about: our own wallet's receive/change addresses.
        self._scripts: set[str] = set()
        self._snapshot: dict[str, list[dict[str, Any]]] | None = None
        self._snapshot_scripts: frozenset[str] = frozenset()
        self._snapshot_height: int | None = None

    # ------------------------------------------------------------- lifecycle

    def close(self) -> None:
        """No persistent connection to release (idempotent)."""

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

    def get_address_utxos(self, address: str) -> list[dict[str, Any]]:
        """Unspent outputs for ``address``, in the Esplora ``/utxo`` shape.

        Answered from the ``scantxoutset`` UTXO snapshot (the plan's option
        (a)); entries map ``{txid, vout, value, status:{confirmed
        [,block_height]}}`` exactly like the electrum adapter's
        ``listunspent`` translation, so ``scan._parse_utxo_entry`` and
        :func:`~localwallet.chain.esplora.balance_from_utxos` cannot tell
        the backends apart. **Unconfirmed outputs are not in the UTXO set —
        they surface as honest absence, never as fabricated entries** (the
        documented M2 history tradeoff; rescan widens, spent history needs
        Esplora/electrum).
        """
        script_hex = self._script_hex(address)
        with self._lock:
            self._scripts.add(script_hex)
            return list(self._scan_snapshot(script_hex).get(script_hex, []))

    def get_address_txs(self, address: str) -> list[dict[str, Any]]:
        """History for ``address`` as far as the data allows, Esplora shape.

        The funding transactions of the address's UNSPENT outputs (one
        verbose ``getrawtransaction`` per distinct txid). Plan §2's
        documented limit: Core without a wallet/address-index cannot
        enumerate spent history, so a fully-spent address — and one funded
        only in the mempool, which never enters the UTXO set — answers
        ``[]``, exactly the shape a fresh unused address gets, and the gap
        walk treats it as unused (nothing fabricated). Entries carry
        ``txid``, ``status{confirmed[,block_height][,block_time]}``,
        optional ``fee`` (only when the node reports it — Core omits it
        when inputs are unknown, e.g. some pruned-node reads) and
        ``vin``/``vout`` with ``scriptpubkey_address`` where mappable.
        """
        script_hex = self._script_hex(address)
        with self._lock:
            self._scripts.add(script_hex)
            unspents = self._scan_snapshot(script_hex).get(script_hex, [])
            txids = list(dict.fromkeys(entry["txid"] for entry in unspents))
            entries: list[dict[str, Any]] = []
            for txid in txids:
                verbose = self._rpc("getrawtransaction", [txid, True], _KIND_ADDRESS_TXS)
                entries.append(self._tx_entry(verbose, txid))
            return entries

    def get_tip_height(self) -> int:
        """Tip height via ``getblockchaininfo`` ``.blocks``.

        A still-syncing node (``blocks < headers``) reports its honest
        height-so-far; sync-progress narration is the app's watch surface,
        not an error here (plan §2, ADR-0023 decision 5).
        """
        info = self._rpc("getblockchaininfo", [], _KIND_TIP_HEIGHT)
        if not isinstance(info, dict):
            raise ChainError(f"{_KIND_TIP_HEIGHT} response was not an object")
        blocks = _require_plain_int(info.get("blocks"))
        if blocks is None or blocks < 0:
            raise ChainError(f"{_KIND_TIP_HEIGHT} response has a missing or invalid 'blocks'")
        return blocks

    def get_tip_block(self) -> TipBlock:
        """Tip block info: ``bestblockhash`` → verbose ``getblockheader``.

        ``timestamp`` is the header's block time; an absent/malformed value
        is the clean ``None`` unavailable state (the Esplora bare-integer
        contract), never a fabricated time. A missing/malformed tip height
        or hash fails closed, as does a header whose ``height`` disagrees
        with the tip we promised (a broken snapshot, not a rounding).
        """
        info = self._rpc("getblockchaininfo", [], _KIND_TIP_BLOCK)
        if not isinstance(info, dict):
            raise ChainError(f"{_KIND_TIP_BLOCK} response was not an object")
        height = _require_plain_int(info.get("blocks"))
        if height is None or height < 0:
            raise ChainError(f"{_KIND_TIP_BLOCK} response has a missing or invalid 'blocks'")
        best_hash = info.get("bestblockhash")
        if (
            not isinstance(best_hash, str)
            or len(best_hash) != _TXID_LENGTH_CHARS
            or not set(best_hash) <= _HEX64
        ):
            raise ChainError(f"{_KIND_TIP_BLOCK} response has a missing or malformed 'bestblockhash'")
        header = self._rpc("getblockheader", [best_hash, True], _KIND_TIP_BLOCK)
        if not isinstance(header, dict):
            raise ChainError(f"{_KIND_TIP_BLOCK} response was not an object")
        header_height = _require_plain_int(header.get("height"))
        if header_height is None or header_height != height:
            raise ChainError(f"{_KIND_TIP_BLOCK} header height does not match the tip")
        timestamp = _require_plain_int(header.get("time"))
        if timestamp is None or timestamp < 0:
            timestamp = None
        return TipBlock(height=height, timestamp=timestamp)

    def broadcast_tx(self, tx_hex: str) -> str:
        """Broadcast a signed transaction — SINGLE attempt, no retries.

        ``sendrawtransaction``. The same money-path discipline as every
        other adapter: the hex is validated before anything is sent, the
        EXPECTED txid is computed from the serialization with embit
        beforehand, and the answer is re-validated as 64-lowercase-hex and
        BOUND to the expected txid (a well-formed but different txid from a
        misbehaving node is a value-free :class:`ChainError`, TCK-SEC-004
        change 1). A POST-equivalent is not idempotent, so EVERY failure —
        transport, auth, or the node's own rejection (an already-known or
        non-standard transaction arrives as an error envelope) — surfaces
        after exactly ONE attempt; callers recover via
        :meth:`get_tx_status`, never by re-broadcasting. The node's error
        TEXT (which echoes the txid) is never surfaced — kind only.
        """
        _validate_tx_hex(tx_hex)
        try:
            expected_txid = Transaction.parse(bytes.fromhex(tx_hex)).txid().hex()
        except Exception as exc:  # containment: embit parse errors vary
            raise ChainError(
                f"{_KIND_BROADCAST} invalid transaction hex: not a parseable transaction"
            ) from exc
        with self._lock:
            self._ensure_gate()
            result = self._request("sendrawtransaction", [tx_hex], _KIND_BROADCAST, retries=0)
        reported = _require_txid_hex(result, _KIND_BROADCAST)
        if reported != expected_txid:
            raise ChainError(
                f"{_KIND_BROADCAST} response txid does not match the broadcast transaction"
            )
        return reported

    def get_tx_status(self, txid: str) -> TxStatus:
        """One transaction's confirmation status via verbose
        ``getrawtransaction``.

        ``confirmed`` = ``confirmations > 0``; ``block_height``/``block_time``
        come from ``blockheight``/``blocktime`` when confirmed and are
        ``None`` otherwise (a mempool tx's first-seen ``time`` is
        deliberately NOT reported as a block time — the electrum parity
        rule; Core, unlike ElectrumX, ALWAYS sends ``confirmations`` — 0
        for mempool). An unknown txid surfaces as the node's error →
        :class:`ChainError` (the Esplora 404 parity); callers decide what
        "unknown" means at the handler layer.
        """
        _validate_txid(txid)
        verbose = self._rpc("getrawtransaction", [txid, True], _KIND_TX_STATUS)
        if not isinstance(verbose, dict):
            raise ChainError(f"{_KIND_TX_STATUS} response was not an object")
        confirmations = _require_plain_int(verbose.get("confirmations"))
        if confirmations is None or confirmations < 0:
            raise ChainError(
                f"{_KIND_TX_STATUS} response has a missing or invalid 'confirmations'"
            )
        confirmed = confirmations > 0
        block_height: int | None = None
        block_time: int | None = None
        if confirmed:
            # 'blockheight' exists from Core 21 (older nodes are refused at
            # the capability gate); a confirmed tx WITHOUT it is a broken
            # payload.
            block_height = _require_plain_int(verbose.get("blockheight"))
            if block_height is None or block_height < 0:
                raise ChainError(f"{_KIND_TX_STATUS} response has invalid 'blockheight'")
            stamp = _require_plain_int(verbose.get("blocktime"))
            block_time = stamp if stamp is not None and stamp >= 0 else None
        return TxStatus(
            txid=txid, confirmed=confirmed, block_height=block_height, block_time=block_time
        )

    def estimate_fee(self, target: FeeTarget) -> int:
        """Backend-native fee bid via ``estimatesmartfee`` → sat/vB.

        Confirmation-target mapping (plan §2, documented): FAST→1,
        MEDIUM→2, SLOW→6 blocks, always in ``CONSERVATIVE`` mode (the
        wallet bids its own money: over-bid-safe; the tx engine's
        min-relay floor keeps bids from going below what relays accept).
        The answer is BTC/kB (a Decimal-exact wire number):
        ``sat/vB = round(BTC/kB × 100000)``. A warmup/syncing node answers
        WITHOUT ``feerate`` (its ``errors`` array is untrusted server text,
        never echoed) and any non-positive/absurd/non-numeric rate fails
        closed as :class:`ChainError` — never a fabricated bid; a positive
        rate that rounds to 0 is rejected like a 0 sat/vB recommendation
        (a 0 sat/vB bid is broken, not free).
        """
        if not isinstance(target, FeeTarget):
            raise TypeError("target must be a FeeTarget")
        result = self._rpc(
            "estimatesmartfee",
            [_ESTIMATESMARTFEE_TARGETS[target], "CONSERVATIVE"],
            _KIND_FEE_ESTIMATE,
        )
        if not isinstance(result, dict):
            raise ChainError(f"{_KIND_FEE_ESTIMATE} response was not an object")
        if "feerate" not in result:
            raise ChainError(f"{_KIND_FEE_ESTIMATE} response is not a usable rate")
        raw = result["feerate"]
        if isinstance(raw, bool) or not isinstance(raw, (Decimal, int)):
            raise ChainError(f"{_KIND_FEE_ESTIMATE} response was not a number")
        btc_per_kb = Decimal(raw)
        if btc_per_kb <= 0 or btc_per_kb > _MAX_FEE_BTC_KB:
            raise ChainError(f"{_KIND_FEE_ESTIMATE} response is not a usable rate")
        sat_vb = round(btc_per_kb * _SAT_VB_PER_BTC_KB)  # Decimal → int, half-even
        if sat_vb <= 0:
            raise ChainError(f"{_KIND_FEE_ESTIMATE} response is below 1 sat/vB granularity")
        return sat_vb

    # ------------------------------------------------------------- internals

    @staticmethod
    def _script_hex(address: str) -> str:
        """The output-script hex for one address (strict pre-check first).

        Every address this adapter is handed is a mainnet script derived
        from the wallet's own xpub (ADR-0021); the script hex is what
        enters the ``raw(...)`` scan descriptor — scripts only, no key
        ever touches the node. An unconvertible string is a value-free
        ChainError (the Esplora charset rule runs first, so a malformed
        argument never even frames a request).
        """
        _validate_address(address)
        try:
            return address_to_scriptpubkey(address).data.hex()
        except Exception:  # noqa: BLE001 — containment: embit errors vary and must not echo the address
            raise ChainError("invalid address argument") from None

    def _scan_snapshot(self, queried: str) -> dict[str, list[dict[str, Any]]]:
        """``{script hex: Esplora-shaped utxo entries}`` at the current tip.

        Caller holds the lock and has ALREADY added the queried script to
        ``self._scripts``. A cached walk serves only while the tip height
        is unchanged AND the CACHED WALK COVERED the queried script (an
        uncovered script has no honest answer in the cache — not even
        "empty"); otherwise a fresh ``scantxoutset`` covers the UNION of
        every script asked about so far (all of them ours). A walk with a
        growing descriptor set (each first-time address re-walks once,
        taking every earlier script with it) is the accepted M2 cost;
        steady-state windows hit the cache and only re-walk when the tip
        moves. An interrupted/failed walk (``success`` not true, or
        Core's ``complete: false`` — e.g. a concurrent scanner on a
        shared node) fails closed: an unreliable UTXO snapshot never
        becomes a balance.
        """
        height = self.get_tip_height()
        if (
            self._snapshot is not None
            and self._snapshot_height == height
            and queried in self._snapshot_scripts
        ):
            return self._snapshot
        # BARE ``raw(<hex>)`` descriptors — the shape scantxoutset's own
        # documented example uses. NOT ``desc(raw(...))``: the ``desc(...)``
        # wrapper is Core's canonical OUTPUT form (LISTDESC and scan results
        # print it); the input grammar (rpc/util.cpp
        # EvalDescriptorStringOrObject → descriptor::Parse) has no ``desc``
        # function, so a wrapped string parses as an unknown descriptor and
        # the node REFUSES the whole start request (TCK-BACKEND-004's
        # rejection: an RPC error envelope our old code collapsed into
        # network-error). The optional checksum is validated only when
        # present; ours is computed at read time, so we send the bare form.
        descriptors = [f"raw({script})" for script in sorted(self._scripts)]
        # Own timeout budget and NO retries (see _SCAN_TIMEOUT_S): a
        # ``start`` walk answers only when it completes; retrying a request
        # whose response we timed out would meet the still-running scan's
        # reserver and be refused ("Scan already in progress") — the same
        # self-inflicted rejection class, from the other side. The gate has
        # already run (get_tip_height above), so _request is the right
        # layer; retries=0 makes ANY transport loss surface once, honestly.
        result = self._request(
            "scantxoutset",
            ["start", descriptors],
            _KIND_UTXO_SCAN,
            retries=0,
            timeout_s=_SCAN_TIMEOUT_S,
        )
        if not isinstance(result, dict):
            raise ChainError(f"{_KIND_UTXO_SCAN} response was not an object")
        if result.get("success") is not True:
            raise ChainError(f"{_KIND_UTXO_SCAN} scan did not complete")
        if "complete" in result and result["complete"] is not True:
            raise ChainError(f"{_KIND_UTXO_SCAN} scan did not complete")
        scan_height = _require_plain_int(result.get("height"))
        if scan_height is None or scan_height < 0:
            raise ChainError(f"{_KIND_UTXO_SCAN} response has a missing or invalid 'height'")
        unspents = result.get("unspents")
        if not isinstance(unspents, list) or any(
            not isinstance(entry, dict) for entry in unspents
        ):
            raise ChainError(f"{_KIND_UTXO_SCAN} response 'unspents' is not a list of objects")
        snapshot: dict[str, list[dict[str, Any]]] = {}
        for index, entry in enumerate(unspents):
            txid = _require_txid_hex(entry.get("txid"), _KIND_UTXO_SCAN)
            vout = _require_plain_int(entry.get("vout"))
            if vout is None or vout < 0:
                raise ChainError(f"utxo entry {index} has a missing or invalid 'vout'")
            script = entry.get("scriptPubKey")
            if (
                not isinstance(script, str)
                or len(script) != len(script.strip())
                or not script
            ):
                raise ChainError(
                    f"utxo entry {index} has a missing or malformed 'scriptPubKey'"
                )
            value = _btc_to_sats(entry.get("amount"), _KIND_UTXO_SCAN, "amount")
            block_height = _require_plain_int(entry.get("height"))
            if block_height is None:
                raise ChainError(f"utxo entry {index} has a missing or invalid 'height'")
            # UTXO-set entries are confirmed by construction (height > 0);
            # a non-positive height is a broken payload mapped honestly as
            # unconfirmed rather than dropped (never silently undercount).
            confirmed = block_height > 0
            status: dict[str, Any] = {"confirmed": confirmed}
            if confirmed:
                status["block_height"] = block_height
            # Bucket ONLY under scripts we ourselves sent — an answer
            # carrying an unrequested script belongs to nobody's wallet but
            # some other tool's scan overlapping this node's, never ours.
            if script in self._scripts:
                snapshot.setdefault(script, []).append(
                    {"txid": txid, "vout": vout, "value": value, "status": status}
                )
        self._snapshot = snapshot
        self._snapshot_scripts = frozenset(self._scripts)
        # The walk's OWN height is the cache key: any disagreement with the
        # tip (moved mid-probe, or a server answering heights it cannot
        # prove) re-walks on the next probe rather than trusting a key the
        # snapshot never covered.
        self._snapshot_height = scan_height
        return snapshot

    def _tx_entry(self, verbose: Any, requested_txid: str) -> dict[str, Any]:
        """Translate one verbose ``getrawtransaction`` into the Esplora
        ``/txs`` shape (the electrum adapter's twin mapping)."""
        kind = _KIND_ADDRESS_TXS
        if not isinstance(verbose, dict):
            raise ChainError(f"{kind} transaction detail was not an object")
        txid = _require_txid_hex(verbose.get("txid"), kind)
        if txid != requested_txid:
            # Bind the answer to the tx we asked for — a mislabeled
            # verbose tx must never be attributed to the queried address.
            raise ChainError(f"{kind} transaction detail does not match the requested txid")
        confirmations = _require_plain_int(verbose.get("confirmations"))
        if confirmations is None or confirmations < 0:
            raise ChainError(f"{kind} transaction detail has a missing or invalid 'confirmations'")
        status: dict[str, Any] = {"confirmed": confirmations > 0}
        if confirmations > 0:
            block_height = _require_plain_int(verbose.get("blockheight"))
            if block_height is None or block_height < 0:
                raise ChainError(f"{kind} transaction detail has invalid 'blockheight'")
            status["block_height"] = block_height
            stamp = _require_plain_int(verbose.get("blocktime"))
            if stamp is not None and stamp >= 0:
                status["block_time"] = stamp
        entry: dict[str, Any] = {"txid": txid, "status": status}
        if "fee" in verbose:
            entry["fee"] = _btc_to_sats(verbose["fee"], kind, "fee")
        vin = verbose.get("vin")
        vout = verbose.get("vout")
        if not isinstance(vin, list) or not isinstance(vout, list) or any(
            not isinstance(item, dict) for item in [*vin, *vout]
        ):
            raise ChainError(f"{kind} transaction detail has malformed 'vin'/'vout'")
        inputs: list[dict[str, Any]] = []
        for item in vin:
            # Core 22+ attaches the input's prevout (scriptPubKey and
            # value); a PRUNED node may omit it for coins whose funding
            # block is gone — the input then contributes no address,
            # exactly like a coinbase on the other adapters.
            prevout = item.get("prevout")
            address = (
                _script_address(prevout.get("scriptPubKey"))
                if isinstance(prevout, dict)
                else None
            )
            inputs.append({"prevout": {"scriptpubkey_address": address}} if address else {})
        outputs: list[dict[str, Any]] = []
        for item in vout:
            address = _script_address(item.get("scriptPubKey"))
            outputs.append({"scriptpubkey_address": address} if address else {})
        entry["vin"] = inputs
        entry["vout"] = outputs
        return entry

    def _ensure_gate(self) -> None:
        """The once-per-client fail-closed handshake (caller holds the lock).

        ``getblockchaininfo.chain`` must be ``"main"`` (ADR-0021 — the
        refusal is deterministic: it raises OUTSIDE the transport retry
        loop, so a testnet node is contacted exactly once) and
        ``getnetworkinfo.version`` must clear :data:`_MIN_CORE_VERSION`.
        The flag is set only after BOTH pass, so a transport loss during
        the gate simply retries the gate on the next attempt.
        """
        if self._gated:
            return
        info = self._request("getblockchaininfo", [], _KIND_GATE)
        if not isinstance(info, dict):
            raise ChainError(f"{_KIND_GATE} response was not an object")
        if info.get("chain") != _MAINNET_CHAIN:
            raise ChainError(f"{_KIND_GATE} backend does not serve mainnet", failure_class=NOT_MAINNET)
        node_info = self._request("getnetworkinfo", [], _KIND_CAPABILITY)
        if not isinstance(node_info, dict):
            raise ChainError(f"{_KIND_CAPABILITY} response was not an object")
        version = _require_plain_int(node_info.get("version"))
        if version is None or version < _MIN_CORE_VERSION:
            raise ChainError(f"{_KIND_CAPABILITY} backend is too old to describe transactions fully")
        self._gated = True

    def _rpc(self, method: str, params: list[Any], kind: str) -> Any:
        """One gate-checked read under the Esplora-consistent retry policy."""
        with self._lock:
            self._ensure_gate()
            return self._request(method, params, kind)

    def _request(
        self,
        method: str,
        params: list[Any],
        kind: str,
        *,
        retries: int | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        """One JSON-RPC call with Esplora-consistent failure discipline.

        Caller holds the lock. Retryable class (up to ``retries``, default
        the configured budget; broadcast passes 0): transport failures and
        bare HTTP 429/5xx WITHOUT a JSON-RPC error envelope (a proxy's 500;
        the node's OWN method rejections arrive as a 500 WITH an error
        envelope and are deterministic — retrying resends identical bytes).
        Auth refusal, other non-2xx, malformed JSON, error envelopes and
        bad shapes raise immediately. Messages carry only the endpoint
        kind, the HTTP status CODE, or the exception CLASS name — never a
        credential, host, address, txid, amount, or server text.

        ``timeout_s`` overrides the per-request budget for ONE call (the
        ``scantxoutset`` walk uses :data:`_SCAN_TIMEOUT_S`, not the generic
        read timeout); ``None`` means the configured value.
        """
        budget = self._config.max_retries if retries is None else retries
        last_failure = "no attempt completed"
        last_transport: _TransportRetry | None = None
        for attempt in range(budget + 1):
            try:
                return self._attempt(method, params, kind, timeout_s=timeout_s)
            except _TransportRetry as exc:
                last_failure = str(exc.args[0]) if exc.args else "network error"
                last_transport = exc
            if attempt < budget:
                _sleep_for(_backoff_delay(attempt))
        if budget == 0:
            raise ChainError(
                f"{kind} failed: {last_failure}",
                failure_class=last_transport.failure_class if last_transport else None,
                exc_name=last_transport.exc_name if last_transport else None,
            )
        raise ChainError(
            f"{kind} request failed after {budget} retries: {last_failure}",
            failure_class=last_transport.failure_class if last_transport else None,
            exc_name=last_transport.exc_name if last_transport else None,
        )

    def _attempt(
        self, method: str, params: list[Any], kind: str, *, timeout_s: float | None = None
    ) -> Any:
        """Exactly one HTTP POST of one RPC; validated envelope in, result out."""
        request_timeout = self._config.timeout_s if timeout_s is None else timeout_s
        body = json.dumps(
            {"jsonrpc": "1.0", "id": 1, "method": method, "params": params}
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }
        basic = self._credential_header()
        if basic is not None:
            headers["Authorization"] = basic
        conn: http.client.HTTPConnection
        if self._tls:
            # The https transport (TCK-BACKEND-003): the per-client context
            # above decides certificate trust. A failed verification is the
            # ssl.SSLCertVerificationError (an OSError) the retry handler
            # below already classifies value-free as a network error — the
            # same collapse httpx's ConnectError gets on the Esplora side,
            # and the refusal copy names the TLS escape hatch.
            conn = http.client.HTTPSConnection(
                self._host,
                self._port,
                timeout=request_timeout,
                context=self._ssl_context,
            )
        else:
            conn = http.client.HTTPConnection(
                self._host, self._port, timeout=request_timeout
            )
        try:
            try:
                conn.request("POST", "/", body=body, headers=headers)
                response = conn.getresponse()
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
                status = response.status
            except (OSError, http.client.HTTPException) as exc:
                raise _TransportRetry(
                    f"network error ({type(exc).__name__})",
                    failure_class=classify_failure(exc),
                    exc_name=type(exc).__name__,
                ) from None
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise ChainError(f"{kind} response exceeded the size bound")
            if status in _AUTH_STATUSES:
                raise ChainError(
                    f"{kind} request refused: authentication failed",
                    failure_class=AUTH_REQUIRED,
                )
            if not 200 <= status < 300:
                envelope = self._try_envelope(raw)
                if isinstance(envelope, dict) and envelope.get("error"):
                    # Core signals its OWN method rejections as HTTP 500
                    # WITH an error envelope — class + code, never text.
                    raise _rpc_rejected(kind, envelope)
                if status == 429 or status >= 500:
                    raise _TransportRetry(
                        f"status {status}",
                        failure_class=HTTP_STATUS,
                        exc_name="HTTPStatus",
                    )
                raise ChainError(
                    f"{kind} request failed: status {status}",
                    failure_class=HTTP_STATUS,
                    exc_name="HTTPStatus",
                )
        finally:
            conn.close()
        try:
            envelope = json.loads(
                raw.decode("utf-8"),
                parse_float=Decimal,
                parse_constant=_constant_rejected,
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise ChainError(f"{kind} response was not valid JSON") from exc
        if not isinstance(envelope, dict) or "result" not in envelope:
            raise ChainError(f"{kind} response was not a JSON-RPC envelope")
        if envelope.get("error") is not None:
            # Same rule on a 2xx: the server's error text is untrusted,
            # but the numeric code is a protocol constant and rides the
            # rpc-error debug class (TCK-DIAG-002).
            raise _rpc_rejected(kind, envelope)
        return envelope["result"]

    def _credential_header(self) -> str | None:
        """The ``Authorization`` value for THIS request, or ``None``.

        Resolution order (plan §2): explicit user/pass, then the cookie
        file (re-read per request — rotation-safe — bounded by
        :data:`_MAX_COOKIE_BYTES`; a missing/unreadable/oversized/
        colon-less/non-ascii cookie degrades to source 3 rather than
        half-authenticating: a node that demands credentials answers that
        case honestly with a 401 refusal). The cookie CONTENT never
        escapes this method except inside the base64 header value; nothing
        is ever logged.
        """
        if self._basic is not None:
            return self._basic
        cookie_file = self._cookie_file
        if cookie_file is not None:
            try:
                # BOUNDED AT READ TIME (TCK-ONB-004 M3, the M2 review LOW):
                # read() with a size cap, not read-then-check — a gigabyte
                # at the cookie path can never be slurbed into memory just
                # to learn it is too big. cap+1 bytes distinguishes "exactly
                # at the cap" from "over the cap".
                with cookie_file.open("rb") as handle:
                    data = handle.read(_MAX_COOKIE_BYTES + 1)
            except OSError:
                data = b""
            if 0 < len(data) <= _MAX_COOKIE_BYTES:
                text = data.decode("ascii", errors="strict") if data.isascii() else ""
                text = text.strip()
                if text and ":" in text:
                    return self._encode_basic(text)
        return None

    @staticmethod
    def _encode_basic(userpass: str) -> str:
        """Build one ``Basic`` header value (the ONLY place the joined
        ``user:pass`` bytes exist beyond their source; never logged)."""
        token = base64.b64encode(userpass.encode("utf-8")).decode("ascii")
        return f"Basic {token}"

    @staticmethod
    def _try_envelope(raw: bytes) -> Any:
        """Lenient JSON parse used ONLY to classify a non-2xx body: Core
        signals method rejections as HTTP 500 WITH a JSON-RPC error
        envelope (deterministic — never retried) while a proxy or the HTTP
        layer itself answers 5xx WITHOUT one (retryable). The content
        never escapes; we only test "is there an error member".
        """
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

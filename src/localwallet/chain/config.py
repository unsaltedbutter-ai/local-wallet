"""Chain-scoped configuration, derived from the root Settings.

This module only adapts ``localwallet.config.Settings`` into the values the
chain adapter needs. It never duplicates or overrides the root settings, and
it performs no I/O of any kind.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from localwallet.config import Settings

__all__ = [
    "BITCOIND_SCHEME",
    "BITCOIND_TLS_SCHEME",
    "ELECTRUM_SCHEME",
    "ChainConfig",
]


#: URL scheme that selects the Electrum-protocol adapter (TCK-ONB-004 M1;
#: ADR-0018 amendment).
ELECTRUM_SCHEME: str = "ssl://"

#: URL scheme that selects the Bitcoin Core RPC adapter over the PLAIN-HTTP
#: JSON-RPC surface (TCK-ONB-004 M2; ADR-0018 amendment). Scheme-based
#: autodetection of Core on http(s) URLs is M3's probe job; a plain-http
#: endpoint answering in Core shape is stored under THIS scheme (the
#: canonical rewrite).
BITCOIND_SCHEME: str = "bitcoind://"

#: The TLS sibling of :data:`BITCOIND_SCHEME` (TCK-BACKEND-003; ADR-0018
#: amendment): ``bitcoind+tls://host[:port]`` selects the SAME Core RPC
#: adapter over an HTTPS transport (a node behind ``-rpcssl`` or a TLS
#: reverse proxy — the Start9 shape). The transport bit must survive the
#: save (a stored URL rebuilds the live client verbatim at every later
#: launch), so the https-RPC INPUT alias gets its CANONICAL form here —
#: one added scheme in the same family, NOT a second dispatch seam:
#: :attr:`ChainConfig.kind` answers ``"bitcoind"`` for both, and every
#: badge/kind/build site rides that unchanged. TLS trust rides the same
#: ``tls_verify`` ladder as the httpx adapters (fail-closed default True;
#: ``False`` only via LOCALWALLET_TLS_VERIFY / config-file key, with the
#: app's honest startup warning). Same shape rules as the plain scheme
#: (userinfo permitted on the env/config-file rungs only).
BITCOIND_TLS_SCHEME: str = "bitcoind+tls://"


@dataclass(frozen=True)
class ChainConfig:
    """Connection parameters for the chain adapter.

    Attributes:
        base_url: backend URL — a user-selected wallet backend via
            ``Settings.chain_base_url`` (ADR-0018 as amended by
            TCK-DESCOPE-M3A: Electrum ``ssl://host[:port]`` or Bitcoin Core
            RPC ``bitcoind://[user:pass@]host[:port]`` /
            ``bitcoind+tls://…``), OR an Esplora API root such as
            ``https://mempool.space/api`` constructed DIRECTLY by the
            public-info fetcher (fees/prices only — no wallet default)
            (TCK-ONB-004 M2; ADR-0018 amendment — see :attr:`kind`). The
            single selection lives in :meth:`from_settings`. Core userinfo
            is permitted ONLY on the env/config-file rungs (the store's
            ``set_chain_base_url`` write validation refuses embedded
            credentials, and M3's settings pane carries dedicated
            never-echoed keys); value-free everywhere.
        timeout_s: Per-request timeout in seconds (applied to connect/read).
        max_retries: Number of retries after the initial attempt (0 disables
            retries entirely).
        tls_verify: Verify the backend's TLS certificate (TCK-BACKEND-001;
            ADR-0018 amendment). ``True`` (default, fail-closed); ``False``
            builds the httpx client with verification OFF for self-hosted
            https backends with a private-CA / self-signed cert — the app
            then prints one honest warning line at startup (transport auth
            is off: a network-path observer can see or alter requests).
            The plain-http ``bitcoind://`` transport has no TLS layer and
            does not consult it; the ``bitcoind+tls://`` sibling (https
            Core RPC, TCK-BACKEND-003) rides this knob exactly like the
            httpx adapters do.

    Raises:
        ValueError: If any value is out of range or malformed (fail closed at
            construction time; config errors are programmer errors, distinct
            from runtime :class:`~localwallet.chain.esplora.ChainError`).
    """

    base_url: str
    timeout_s: float
    max_retries: int
    # Fail-closed default: a caller that never threads the setting (test
    # fixtures, direct construction) keeps verification ON. Only an explicit
    # ``False`` disables it, and that path is reserved for the app's startup
    # warning (see :attr:`Settings.tls_verify`).
    tls_verify: bool = True

    @property
    def kind(self) -> str:
        """Adapter selected by the URL scheme: ``"electrum"`` for ``ssl://``
        (TCK-ONB-004 M1), ``"bitcoind"`` for the Core RPC family —
        ``bitcoind://`` and its https sibling ``bitcoind+tls://``
        (TCK-ONB-004 M2; TCK-BACKEND-003) — ``"esplora"`` for http(s).
        The construction site (``app._build_chain_client``) dispatches on
        exactly this value; the transport difference between the two Core
        schemes lives INSIDE the adapter, not in a second seam."""
        if self.base_url.startswith(ELECTRUM_SCHEME):
            return "electrum"
        if self.base_url.startswith(
            (BITCOIND_SCHEME, BITCOIND_TLS_SCHEME)
        ):
            return "bitcoind"
        return "esplora"

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str):
            raise ValueError("base_url must be an http(s), ssl:// or bitcoind:// URL")  # noqa: TRY004
        if self.base_url.startswith(ELECTRUM_SCHEME):
            self._validate_electrum_url()
        elif self.base_url.startswith((BITCOIND_SCHEME, BITCOIND_TLS_SCHEME)):
            self._validate_bitcoind_url()
        elif not (self.base_url.startswith("http://") or self.base_url.startswith("https://")):
            raise ValueError("base_url must be an http(s) URL")
        else:
            # Reject embedded userinfo (https://user:pass@host): httpx would
            # send those credentials on every request, contradicting the
            # "no API keys are used or sent" guarantee. Value-free, fail
            # closed.
            if urlsplit(self.base_url).username is not None:
                raise ValueError("base_url must be an http(s) URL without userinfo")
        if (
            isinstance(self.timeout_s, bool)
            or not isinstance(self.timeout_s, (int, float))
            or self.timeout_s <= 0
        ):
            raise ValueError("timeout_s must be a positive number of seconds")
        if (
            isinstance(self.max_retries, bool)
            or not isinstance(self.max_retries, int)
            or self.max_retries < 0
        ):
            raise ValueError("max_retries must be a non-negative integer")
        # Strict type-check (TCK-BACKEND-001): the ladder value must be a real
        # bool by the time it reaches construction. ``from_env`` already
        # coerces/refuses the env+file rungs, so a non-bool here is a
        # programmer error — fail closed, never silently truthy-test a string.
        if not isinstance(self.tls_verify, bool):
            # ValueError (not TypeError), matching every other guard in this
            # file and the root config._load_config_file (see its TRY004):
            # a mis-typed config knob is treated as a config error, not a
            # programmer type error, so the whole ladder surfaces one class.
            raise ValueError("tls_verify must be a boolean")  # noqa: TRY004

    def _validate_bitcoind_url(self) -> None:
        """Fail closed on a malformed ``bitcoind://[user:pass@]host[:port]``
        or ``bitcoind+tls://[user:pass@]host[:port]`` Core RPC endpoint
        (TCK-ONB-004 M2; ADR-0018 amendment; the TLS sibling added by
        TCK-BACKEND-003 — identical shape rules, transport differs inside
        the adapter).

        Shape rules (the ``ssl://`` validator's discipline, with one
        documented difference): a parseable host, an optional NUMERIC
        in-range port, no path/query/fragment (the RPC surface is a single
        POST root — a stray path is a typo, not a hint), and — because
        Bitcoin Core RPC is Basic-auth by design — userinfo IS permitted
        here. The credential rules that come with it are value-free and
        strict: user and password must appear TOGETHER (``user@host`` with
        no ``:pass`` is refused: Core's rpcuser/rpcpassword are a pair, a
        half-pair is a typo, never a fallback), and neither may contain
        whitespace or non-ASCII (percent-encode or use the constructor
        arguments; a raw secret fragment must never need quoting rules to
        survive). Nothing here echoes any part of the URL. The default
        port (8332, mainnet RPC — ADR-0021 has no other network) is applied
        by the adapter, not stored here.
        """
        parsed = urlsplit(self.base_url)
        bad = ValueError("base_url must be a bitcoind://host[:port] Core RPC URL")
        if not parsed.hostname:
            raise bad
        try:
            _ = parsed.port  # raises ValueError on non-numeric/out-of-range
        except ValueError:
            raise bad from None
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise bad
        if parsed.username is not None or parsed.password is not None:
            if parsed.username is None or parsed.password is None:
                raise bad  # half a credential pair is a typo, not a hint
            for part in (parsed.username, parsed.password):
                if not part or any(c.isspace() for c in part) or not part.isascii():
                    raise bad

    def _validate_electrum_url(self) -> None:
        """Fail closed on a malformed ``ssl://host[:port]`` endpoint.

        Shape rules (mirroring the http(s) branch's discipline): a parseable
        host, no embedded userinfo, no path/query/fragment (the Electrum
        protocol has no URL namespace — a stray path is a typo, not a hint),
        and a numeric port inside the TCP range when present. All errors are
        value-free (the URL may embed nothing secret, but the invariant
        covers every path). The default port (50002, the standard Electrum
        SSL port) is applied by the adapter, not stored here.
        """
        parsed = urlsplit(self.base_url)
        bad = ValueError("base_url must be an ssl://host[:port] Electrum URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must be an ssl:// URL without userinfo")
        if not parsed.hostname:
            raise bad
        try:
            _ = parsed.port  # raises ValueError on non-numeric/out-of-range
        except ValueError:
            raise bad from None
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise bad

    @classmethod
    def from_settings(cls, settings: Settings) -> ChainConfig:
        """Build a ChainConfig from the root Settings (no env reads here).

        The WALLET chain backend is selected here — the single, unambiguous
        selection point (Phase 4, TCK-P4-002; ADR-0018 as amended by
        TCK-DESCOPE-M3A):

        - ``settings.chain_base_url`` set (non-empty) is the authoritative
          backend base for the WHOLE wallet (every client-mediated call:
          address txs/utxos, tip, watch, broadcast, tx_status) — flipping
          the backend to a user's own instance is a config-only operation;
        - ``settings.chain_base_url`` EMPTY means UNRESOLVED: there is no
          wallet default anymore (the old ``esplora_base_url`` public
          fallback is removed — mempool.space is a PUBLIC-INFO source for
          fees/prices only, ADR-0003/0011/0023 amendments). Constructing a
          wallet client while unresolved fails closed with a value-free
          :class:`ValueError`; the app holds the first-run scan at
          ``awaiting_backend`` instead (never a silent public scan).

        The URL SCHEME selects the adapter kind (TCK-ONB-004 M1/M2; ADR-0018
        amendment): ``ssl://host[:port]`` → the Electrum-protocol client
        (:attr:`kind` == ``"electrum"``), ``bitcoind[+tls]://`` → the Core
        RPC client; http(s) stays a well-formed ChainConfig shape (the
        public-info Esplora path and the M4-retired probe construct it
        directly) but the WALLET construction site refuses that kind.

        A malformed selected URL (a foreign scheme, or an ``ssl://`` URL
        without a well-formed host[:port]) fails closed here with a
        value-free :class:`ValueError` at construction time — never a
        mid-request crash.
        """
        selected = settings.chain_base_url.strip() if settings.chain_base_url else ""
        # A non-empty chain_base_url that strips to nothing (whitespace-only)
        # is malformed — the user set it deliberately, so silently treating
        # it as unset would undo their intent. Fail closed.
        if settings.chain_base_url and not selected:
            raise ValueError("chain_base_url must not be blank when set")
        if not selected:
            # TCK-DESCOPE-M3A: empty is UNRESOLVED, never a public default
            # (value-free — there is nothing to echo).
            raise ValueError(
                "no wallet chain backend is configured (chain_base_url is empty)"
            )
        base_url = selected
        return cls(
            base_url=base_url,
            timeout_s=settings.request_timeout_s,
            max_retries=settings.max_retries,
            tls_verify=settings.tls_verify,
        )

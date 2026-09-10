"""Chain-scoped configuration, derived from the root Settings.

This module only adapts ``localwallet.config.Settings`` into the values the
chain adapter needs. It never duplicates or overrides the root settings, and
it performs no I/O of any kind.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from localwallet.config import Settings

__all__ = ["BITCOIND_SCHEME", "ELECTRUM_SCHEME", "ChainConfig"]


#: URL scheme that selects the Electrum-protocol adapter (TCK-ONB-004 M1;
#: ADR-0018 amendment).
ELECTRUM_SCHEME: str = "ssl://"

#: URL scheme that selects the Bitcoin Core RPC adapter (TCK-ONB-004 M2;
#: ADR-0018 amendment). ``bitcoind://host[:port]`` is an explicit-choice
#: transport (the plain-http JSON-RPC surface; https RPC is out of M2
#: scope and NOT expressible here) — scheme-based autodetection of Core
#: on http(s) URLs stays M3's probe job. Everything else this class
#: accepts is Esplora http(s) or Electrum ssl://.
BITCOIND_SCHEME: str = "bitcoind://"


@dataclass(frozen=True)
class ChainConfig:
    """Connection parameters for the chain adapter.

    Attributes:
        base_url: backend URL — an Esplora API root such as
            ``https://mempool.space/api`` (public default, ADR-0003) or a
            user's self-hosted instance selected via
            ``Settings.chain_base_url`` (ADR-0018), an Electrum-protocol
            endpoint ``ssl://host[:port]`` which selects the Electrum
            adapter, OR a Bitcoin Core RPC endpoint
            ``bitcoind://[user:pass@]host[:port]`` which selects the
            Core-RPC adapter (TCK-ONB-004 M2; ADR-0018 amendment — see
            :attr:`kind`). The single selection lives in
            :meth:`from_settings`. Core userinfo is permitted ONLY on the
            env/config-file rungs (the store's ``set_chain_base_url`` write
            validation refuses embedded credentials, and M3's settings pane
            carries dedicated never-echoed keys); value-free everywhere.
        timeout_s: Per-request timeout in seconds (applied to connect/read).
        max_retries: Number of retries after the initial attempt (0 disables
            retries entirely).
        tls_verify: Verify the backend's TLS certificate (TCK-BACKEND-001;
            ADR-0018 amendment). ``True`` (default, fail-closed); ``False``
            builds the httpx client with verification OFF for self-hosted
            https backends with a private-CA / self-signed cert — the app
            then prints one honest warning line at startup (transport auth
            is off: a network-path observer can see or alter requests).
            The ``bitcoind://`` adapter is plain-http (Core RPC on
            loopback) and does not consult this knob: https RPC is
            unexpressible for the scheme (M2 scope, ADR-0018 amendment).

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
        (TCK-ONB-004 M1), ``"bitcoind"`` for ``bitcoind://`` (TCK-ONB-004
        M2), ``"esplora"`` for http(s). The construction site
        (``app._build_chain_client``) dispatches on exactly this value."""
        if self.base_url.startswith(ELECTRUM_SCHEME):
            return "electrum"
        if self.base_url.startswith(BITCOIND_SCHEME):
            return "bitcoind"
        return "esplora"

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str):
            raise ValueError("base_url must be an http(s), ssl:// or bitcoind:// URL")  # noqa: TRY004
        if self.base_url.startswith(ELECTRUM_SCHEME):
            self._validate_electrum_url()
        elif self.base_url.startswith(BITCOIND_SCHEME):
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
        endpoint (TCK-ONB-004 M2; ADR-0018 amendment).

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

        The chain backend is selected here — the single, unambiguous
        selection point (Phase 4, TCK-P4-002; ADR-0018):

        - if ``settings.chain_base_url`` is set (non-empty), it is the
          authoritative backend base for the WHOLE wallet (every
          client-mediated call: address txs/utxos, tip, fees, price,
          broadcast) — flipping the backend to a user's own instance is a
          config-only operation;
        - otherwise ``settings.esplora_base_url`` is used, preserving the
          ADR-0003 public default and full backward compatibility with
          ``LOCALWALLET_ESPLORA_BASE_URL``.

        The URL SCHEME selects the adapter kind (TCK-ONB-004 M1; ADR-0018
        amendment): ``ssl://host[:port]`` → the Electrum-protocol client
        (:attr:`kind` == ``"electrum"``, consumed at the single construction
        site ``app._build_chain_client``); http(s) → Esplora as before.

        A malformed selected URL (non-http(s), or an ``ssl://`` URL without
        a well-formed host[:port]) fails closed here with a value-free
        :class:`ValueError` at construction time — never a mid-request crash.
        """
        selected = settings.chain_base_url.strip() if settings.chain_base_url else ""
        # A non-empty chain_base_url that strips to nothing (whitespace-only)
        # is malformed — the user set it deliberately, so silently falling back
        # to the public default would undo their intent. Fail closed. Only a
        # truly absent/empty value falls back to the legacy default.
        if settings.chain_base_url and not selected:
            raise ValueError("chain_base_url must not be blank when set")
        base_url = selected or settings.esplora_base_url
        return cls(
            base_url=base_url,
            timeout_s=settings.request_timeout_s,
            max_retries=settings.max_retries,
            tls_verify=settings.tls_verify,
        )

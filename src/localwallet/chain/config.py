"""Chain-scoped configuration, derived from the root Settings.

This module only adapts ``localwallet.config.Settings`` into the values the
chain adapter needs. It never duplicates or overrides the root settings, and
it performs no I/O of any kind.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from localwallet.config import Settings

__all__ = ["ChainConfig"]


@dataclass(frozen=True)
class ChainConfig:
    """Connection parameters for the chain adapter.

    Attributes:
        base_url: Esplora API root, e.g. ``https://mempool.space/testnet4/api``
            (public default, ADR-0003) or a user's self-hosted instance
            selected via ``Settings.chain_base_url`` (ADR-0018). The single
            selection lives in :meth:`from_settings`.
        timeout_s: Per-request timeout in seconds (applied to connect/read).
        max_retries: Number of retries after the initial attempt (0 disables
            retries entirely).

    Raises:
        ValueError: If any value is out of range or malformed (fail closed at
            construction time; config errors are programmer errors, distinct
            from runtime :class:`~localwallet.chain.esplora.ChainError`).
    """

    base_url: str
    timeout_s: float
    max_retries: int

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str) or not (
            self.base_url.startswith("http://") or self.base_url.startswith("https://")
        ):
            raise ValueError("base_url must be an http(s) URL")
        # Reject embedded userinfo (https://user:pass@host): httpx would send
        # those credentials on every request, contradicting the "no API keys
        # are used or sent" guarantee. Value-free, fail closed.
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

    @classmethod
    def from_settings(cls, settings: Settings) -> ChainConfig:
        """Build a ChainConfig from the root Settings (no env reads here).

        The chain backend is selected here — the single, unambiguous
        selection point (Phase 4, TCK-P4-002; ADR-0018):

        - if ``settings.chain_base_url`` is set (non-empty), it is the
          authoritative Esplora base for the WHOLE wallet (every
          EsploraClient-mediated call: address txs/utxos, tip, fees, price,
          broadcast) — flipping the backend to a user's own instance is a
          config-only operation;
        - otherwise ``settings.esplora_base_url`` is used, preserving the
          ADR-0003 public default and full backward compatibility with
          ``LOCALWALLET_ESPLORA_BASE_URL``.

        A malformed (non-http(s)) selected URL fails closed here with a
        value-free :class:`ValueError` at construction time — never a
        mid-request crash.
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
        )

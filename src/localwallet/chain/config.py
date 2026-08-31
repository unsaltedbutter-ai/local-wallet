"""Chain-scoped configuration, derived from the root Settings.

This module only adapts ``localwallet.config.Settings`` into the values the
chain adapter needs. It never duplicates or overrides the root settings, and
it performs no I/O of any kind.
"""

from __future__ import annotations

from dataclasses import dataclass

from localwallet.config import Settings

__all__ = ["ChainConfig"]


@dataclass(frozen=True)
class ChainConfig:
    """Connection parameters for the chain adapter.

    Attributes:
        base_url: Esplora API root, e.g. ``https://mempool.space/testnet4/api``.
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
        """Build a ChainConfig from the root Settings (no env reads here)."""
        return cls(
            base_url=settings.esplora_base_url,
            timeout_s=settings.request_timeout_s,
            max_retries=settings.max_retries,
        )

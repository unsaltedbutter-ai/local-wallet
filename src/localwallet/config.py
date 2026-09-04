"""Settings holder for local-wallet.

Loaded from env vars prefixed ``LOCALWALLET_`` via ``from_env()``. Stdlib only.
No secrets are stored or logged here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields


@dataclass
class Settings:
    """Small, explicitly-settable runtime settings."""

    esplora_base_url: str = "https://mempool.space/testnet4/api"
    # THE single chain-backend selection point (Phase 4, TCK-P4-002; ADR-0018).
    # When set (non-empty), this URL is the authoritative Esplora base for the
    # WHOLE wallet: every EsploraClient-mediated call (address txs/utxos, tip,
    # fees, price, broadcast) hits it. When empty (the default), the legacy
    # ``esplora_base_url`` is used instead — preserving the ADR-0003 public
    # default and full backward compatibility with LOCALWALLET_ESPLORA_BASE_URL.
    # The instance must serve testnet4 (ADR-0004 invariant); the client's path
    # shapes are identical regardless of host. Validation is fail-closed at
    # client construction (ChainConfig), never mid-request.
    chain_base_url: str = ""
    request_timeout_s: float = 10.0
    max_retries: int = 3
    network: str = "testnet"
    store_path: str = "localwallet.db"
    price_ttl_s: float = 60.0
    price_enabled: bool = True
    fee_cache_ttl_s: float = 30.0
    # --- Node detection (Phase 4, node/ module; TCK-P4-001) ---
    # Path to a Bitcoin Core RPC cookie file. Empty string means "use the
    # per-network default under ~/.bitcoin" (e.g. ~/.bitcoin/testnet4/.cookie).
    # The cookie CONTENT is a secret and is never logged or echoed.
    rpc_cookie_path: str = ""
    # Bitcoin Core JSON-RPC port to probe. Defaults to testnet4 (48332) per
    # ADR-0004 / Bitcoin Core chainparamsbase.cpp.
    rpc_port: int = 48332
    # Self-hosted mempool.space API root on localhost (well-known default
    # backend port 3006). Detected by the node doctor; full backend wiring
    # lands in TCK-P4-002.
    local_mempool_url: str = "http://127.0.0.1:3006"
    # Master switch for the node doctor's detection pass. "1" (enabled) by
    # default; set to "0" to skip probing entirely (privacy/perf escape hatch).
    node_detection_enabled: bool = True
    # --- Background watch (Phase 5, TCK-P5-001) ---
    # Seconds between ``watch_incoming`` poll cycles. ``0`` disables
    # background watching entirely (the off-via-zero escape hatch, ADR-0019).
    # A conservative default (60 s) limits how often the user's addresses are
    # re-queried against a PUBLIC explorer — the honest-privacy knob
    # documented in ADR-0019; on the user's own node (``chain_base_url`` set,
    # ADR-0018) polling is cheap and private either way.
    watch_interval_s: float = 60.0

    @classmethod
    def from_env(cls) -> Settings:
        """Build a Settings instance, overriding defaults from LOCALWALLET_* env vars.

        Recognized variables: ``LOCALWALLET_ESPLORA_BASE_URL``,
        ``LOCALWALLET_CHAIN_BASE_URL``,
        ``LOCALWALLET_REQUEST_TIMEOUT_S``, ``LOCALWALLET_MAX_RETRIES``,
        ``LOCALWALLET_NETWORK``, ``LOCALWALLET_STORE_PATH``,
        ``LOCALWALLET_PRICE_TTL_S``, ``LOCALWALLET_PRICE_ENABLED``,
        ``LOCALWALLET_FEE_CACHE_TTL_S``, ``LOCALWALLET_RPC_COOKIE_PATH``,
        ``LOCALWALLET_RPC_PORT``, ``LOCALWALLET_LOCAL_MEMPOOL_URL``,
        ``LOCALWALLET_NODE_DETECTION_ENABLED``,
        ``LOCALWALLET_WATCH_INTERVAL_S``. Unknown variables are ignored.

        Boolean fields accept ``0``/``1`` or ``true``/``false``/``yes``/``no``
        (any case); anything else raises :class:`ValueError` (fail closed —
        config errors are programmer errors).
        """
        def _coerce(name: str, value: str):
            field = next(f for f in fields(cls) if f.name == name)
            if isinstance(field.default, bool):
                lowered = value.strip().lower()
                if lowered in ("1", "true", "yes"):
                    return True
                if lowered in ("0", "false", "no"):
                    return False
                raise ValueError(
                    f"invalid boolean for {name}: expected 0/1 or true/false"
                )
            if isinstance(field.default, int):
                return int(value)
            if isinstance(field.default, float):
                return float(value)
            return value

        values = {}
        for field in fields(cls):
            env_name = f"LOCALWALLET_{field.name.upper()}"
            raw = os.environ.get(env_name)
            if raw is not None:
                values[field.name] = _coerce(field.name, raw)
        return cls(**values)

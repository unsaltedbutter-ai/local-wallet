"""Settings holder for local-wallet.

Loaded from env vars prefixed ``LOCALWALLET_`` via ``from_env()``. Stdlib only.
No secrets are stored or logged here.

**Stdlib-only constraint (lint-enforced discipline for this module):** this
module must never import :mod:`localwallet.store` (or anything doing I/O).
The persisted chain-backend choice (ADR-0023, TCK-ONB-002) therefore enters
through :func:`resolve_chain_base_url` as a plain argument: the startup wiring
(TCK-ONB-003) reads the stored value from the store and injects it here.
``Settings.chain_base_url`` remains the single backend selection point
(ADR-0018); this module only decides *where its value may come from*.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Final

# ---------------------------------------------------------------------------
# Coin-selection policy settings (TCK-UTXO-002, docs/ux-utxo-notes-design.md
# §2.3 + ADR-0012 amendment). THE single source of truth for the keys, shipped
# defaults and bounds — the store's typed writer (``Store.set_coin_setting``)
# and the tx engine's argument guards (``tx/selection.py``) both read this
# table, so a bound can never drift between the rungs of the ladder.
#
# Bounds from the doc's table: the 546-sat floor on the target minimum is the
# P2PKH dust figure at Core's default relay rate — an upper bound on what a
# dust output can be, used here ONLY as a coarse sanity rail for the setting
# (the ADR-0012 choice, doc §2.3); the real dust/min-relay rules stay
# computed from script size in ``tx/dust.py``.
# ---------------------------------------------------------------------------

UTXO_TARGET_MIN_SETTING: Final[str] = "utxo_target_min_sats"
UTXO_TARGET_MAX_SETTING: Final[str] = "utxo_target_max_sats"
CONSOLIDATE_BELOW_SAT_VB_SETTING: Final[str] = "consolidate_below_sat_vb"

#: Settings keys in ladder order (documented iteration order for readers).
COIN_SETTING_KEYS: Final[tuple[str, ...]] = (
    UTXO_TARGET_MIN_SETTING,
    UTXO_TARGET_MAX_SETTING,
    CONSOLIDATE_BELOW_SAT_VB_SETTING,
)

#: Inclusive per-key bounds (doc §2.3 table; the max-over-min cross-check is
#: separate — see :func:`resolve_coin_selection_settings`).
COIN_SETTING_BOUNDS: Final[Mapping[str, tuple[int, int]]] = {
    UTXO_TARGET_MIN_SETTING: (546, 100_000_000),
    UTXO_TARGET_MAX_SETTING: (546, 21_000_000_000_000),
    CONSOLIDATE_BELOW_SAT_VB_SETTING: (1, 100),
}

#: Shipped defaults (doc §2.3: "opinionated defaults, exactly gap-limit's
#: status": 100k sats target floor, 0.1 BTC protection ceiling, 2 sat/vB
#: consolidation ceiling — the slow/medium ladder rungs live around there).
COIN_SETTING_DEFAULTS: Final[Mapping[str, int]] = {
    UTXO_TARGET_MIN_SETTING: 100_000,
    UTXO_TARGET_MAX_SETTING: 10_000_000,
    CONSOLIDATE_BELOW_SAT_VB_SETTING: 2,
}


@dataclass
class Settings:
    """Small, explicitly-settable runtime settings."""

    esplora_base_url: str = "https://mempool.space/api"
    # THE single chain-backend selection point (Phase 4, TCK-P4-002; ADR-0018).
    # When set (non-empty), this URL is the authoritative Esplora base for the
    # WHOLE wallet: every EsploraClient-mediated call (address txs/utxos, tip,
    # fees, price, broadcast) hits it. When empty (the default), the legacy
    # ``esplora_base_url`` is used instead — preserving the ADR-0003 public
    # default and full backward compatibility with LOCALWALLET_ESPLORA_BASE_URL.
    # The instance must serve mainnet (mainnet-only invariant, ADR-0021); the
    # client's path shapes are identical regardless of host. Validation is
    # fail-closed at client construction (ChainConfig), never mid-request.
    chain_base_url: str = ""
    request_timeout_s: float = 10.0
    max_retries: int = 3
    network: str = "main"
    store_path: str = "localwallet.db"
    price_ttl_s: float = 60.0
    price_enabled: bool = True
    fee_cache_ttl_s: float = 30.0
    # --- Node detection (Phase 4, node/ module; TCK-P4-001) ---
    # Path to a Bitcoin Core RPC cookie file. Empty string means "use the
    # per-network default under ~/.bitcoin" (e.g. ~/.bitcoin/.cookie for
    # mainnet). The cookie CONTENT is a secret and is never logged or echoed.
    rpc_cookie_path: str = ""
    # Bitcoin Core JSON-RPC port to probe. Defaults to mainnet (8332) per
    # Bitcoin Core chainparamsbase.cpp (mainnet-only invariant, ADR-0021).
    rpc_port: int = 8332
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
    #: Dev knob (TCK-CFG-001): the per-scan address gap limit override
    #: (``LOCALWALLET_GAP_LIMIT``), as a DECIMAL STRING. Empty string = unset
    #: (use the DB ``gap_limit`` setting, else :data:`~localwallet.wallet.scan.DEFAULT_GAP_LIMIT`
    #: of 20 — ADR-0009). Kept as a string so ``from_env`` needs no special
    #: coercion: the app validates + bounds it (fail-closed, value-free) at
    #: startup, then threads the resolved int into every scan as the per-call
    #: ``gap_limit`` argument (so it wins over the DB setting). A small gap
    #: speeds up scans but can MISS allocated-but-unused addresses; widen +
    #: rescan per ADR-0009.
    gap_limit: str = ""
    # --- Coin-selection policy (TCK-UTXO-002, docs/ux-utxo-notes-design.md
    # §2.3): the same gap-limit shape — DECIMAL STRINGS, empty = unset, so
    # ``from_env`` needs no coercion; the startup wiring resolves
    # env > stored > default fail-closed via
    # :func:`resolve_coin_selection_settings` and threads the ints into
    # ``select_coins`` as plain data (tx/ reads no env, no store). Env names:
    # LOCALWALLET_UTXO_TARGET_MIN_SATS / LOCALWALLET_UTXO_TARGET_MAX_SATS /
    # LOCALWALLET_CONSOLIDATE_BELOW_SAT_VB.
    utxo_target_min_sats: str = ""
    utxo_target_max_sats: str = ""
    consolidate_below_sat_vb: str = ""

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
        ``LOCALWALLET_WATCH_INTERVAL_S``, ``LOCALWALLET_GAP_LIMIT``,
        ``LOCALWALLET_UTXO_TARGET_MIN_SATS``,
        ``LOCALWALLET_UTXO_TARGET_MAX_SATS``,
        ``LOCALWALLET_CONSOLIDATE_BELOW_SAT_VB``.
        Unknown variables are ignored.

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


def resolve_chain_base_url(
    env_value: str | None, stored_value: str | None
) -> str | None:
    """Resolve the chain backend selection (ADR-0023 precedence; TCK-ONB-002).

    Pure function — no env reads, no I/O, no store import. Precedence::

        env LOCALWALLET_CHAIN_BASE_URL  >  stored choice  >  None

    Each rung treats ``None``, the empty string, and whitespace-only as
    *unset* (an exported empty var is indistinguishable from an absent one —
    both mean "no override"). The winning value is returned stripped; a
    whitespace-only setting never resolves to a usable URL, so it falls
    through to the next rung instead. ``None`` means *no rung is set*: the
    caller keeps ``Settings.chain_base_url`` empty and the existing public
    default applies unchanged (zero change when unset, ADR-0018 decision 2 /
    ADR-0023 decision 3). Validation of a stored value happened at write time
    (``Store.set_chain_base_url``); the env rung keeps failing closed in
    ``ChainConfig.from_settings`` as before.
    """
    for value in (env_value, stored_value):
        if value is not None and value.strip():
            return value.strip()
    return None


@dataclass(frozen=True, slots=True)
class CoinSelectionSettings:
    """Resolved coin-selection policy integers (plain data for ``tx/``)."""

    target_min_sats: int
    target_max_sats: int
    consolidate_below_sat_vb: int


def _coin_setting_source(key: str, is_env: bool) -> str:
    """Name the failing rung in an error message: env var or settings key.

    Never the value (ADR-0009 value-free discipline) — the key/env names are
    fixed public identifiers, not user data.
    """
    return f"LOCALWALLET_{key.upper()}" if is_env else f"setting {key!r}"


def resolve_coin_selection_settings(
    env_values: Mapping[str, str | None],
    stored_values: Mapping[str, str | None],
) -> CoinSelectionSettings:
    """Resolve the three coin-selection settings (ADR-0012 amendment, doc §2.3).

    Pure function — no env reads, no I/O, no store import (same shape as
    :func:`resolve_chain_base_url`; the ladder logic mirrors the gap_limit
    resolution in ``wallet.scan._resolve_gap_limit``). Precedence per key::

        env LOCALWALLET_<KEY>  >  stored settings key  >  shipped default

    Fail-closed: every non-empty rung must be a plain ASCII decimal integer
    within the key's :data:`COIN_SETTING_BOUNDS`, and the resolved pair must
    satisfy ``min < max`` — a malformed value (or a corrupt min/max pair from
    any mix of rungs) raises :class:`ValueError` so startup refuses to run
    rather than silently reverting a policy (ADR-0009: "a corrupt setting
    never silently flips policy"). Errors are value-free. ``None``, empty and
    whitespace-only rungs mean *unset* (the chain_base_url convention).
    """
    resolved: dict[str, int] = {}
    for key in COIN_SETTING_KEYS:
        lo, hi = COIN_SETTING_BOUNDS[key]
        source = None
        for is_env, values in ((True, env_values), (False, stored_values)):
            raw = values.get(key)
            if raw is not None and raw.strip():
                source = (raw.strip(), _coin_setting_source(key, is_env))
                break
        if source is None:
            resolved[key] = COIN_SETTING_DEFAULTS[key]
            continue
        text, rung = source
        if not text.isascii() or not text.isdigit():
            raise ValueError(f"{rung} must be a plain decimal integer")
        value = int(text)
        if not lo <= value <= hi:
            raise ValueError(f"{rung} must be between {lo} and {hi}")
        resolved[key] = value
    min_sats = resolved[UTXO_TARGET_MIN_SETTING]
    max_sats = resolved[UTXO_TARGET_MAX_SETTING]
    if min_sats >= max_sats:
        raise ValueError(
            "coin target settings are malformed: utxo_target_min_sats must "
            "be below utxo_target_max_sats"
        )
    return CoinSelectionSettings(
        target_min_sats=min_sats,
        target_max_sats=max_sats,
        consolidate_below_sat_vb=resolved[CONSOLIDATE_BELOW_SAT_VB_SETTING],
    )

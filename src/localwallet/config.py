"""Settings holder for local-wallet.

Loaded via ``from_env()``: env vars prefixed ``LOCALWALLET_`` override the
shipped defaults, and a user-editable JSON config file
(:data:`CONFIG_FILE_PATH`) fills the rung between env and the store-backed
settings. Stdlib only. No secrets are stored or logged here.

Every keyed setting resolves through ONE documented ladder (TCK-CFG-002)::

    env (LOCALWALLET_*)  >  config file (<repo/install root>/config.json)  >  stored (DB)  >  shipped default

The config-file rung is merged inside ``from_env()`` (env > file; a file key
is ignored only when the same key's env var is set). The stored rung is NOT
read here — it enters through the ``resolve_*`` functions as a plain argument
(see below), so this module stays stdlib-only and store-free.

**Stdlib-only constraint (lint-enforced discipline for this module):** this
module must never import :mod:`localwallet.store` (or anything doing I/O).
The persisted chain-backend choice (ADR-0023, TCK-ONB-002) therefore enters
through :func:`resolve_chain_base_url` as a plain argument: the startup wiring
(TCK-ONB-003) reads the stored value from the store and injects it here.
``Settings.chain_base_url`` remains the single backend selection point
(ADR-0018); this module only decides *where its value may come from*.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
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

DISPLAY_CURRENCY_SETTING: Final[str] = "display_currency"

#: THE closed display-currency enum (TCK-FIAT-002, ADR-0011 amendment):
#: exactly the codes the mempool.space ``/v1/prices`` endpoint serves
#: (confirmed live 2026-09-10), canonical lowercase. Parsing is
#: case-insensitive; every consumer canonicalizes through
#: :func:`normalize_display_currency` before use. The LLM never authors a
#: currency code — this setting is the only currency selector (the model
#: emits ``get_balance``; the app converts).
DISPLAY_CURRENCIES: Final[tuple[str, ...]] = (
    "usd",
    "eur",
    "gbp",
    "cad",
    "chf",
    "aud",
    "jpy",
)

#: Shipped default: USD (unchanged TCK-FIAT-001 behavior when no rung is set).
DEFAULT_DISPLAY_CURRENCY: Final[str] = "usd"


#: Path of the user-editable config file (TCK-CFG-003): ``config.json`` at the
#: INSTALL/REPO ROOT, next to the code — NOT ``~/.localwallet/config.json``.
#: Anchor rule: resolved from the package location, not the launch CWD, so it
#: works for source checkouts AND ``install.sh`` layouts alike. The package
#: lives at ``<root>/src/localwallet/config.py`` (source checkout) or the
#: editable-install's clone at ``$INSTALL_ROOT/src/localwallet/config.py``;
#: ``Path(__file__).resolve().parents[2]`` is that shared ``<root>`` — the
#: directory that CONTAINS ``src/``. ``os.getcwd()`` is never consulted, so
#: the launch directory cannot move the file. The file holds the same scalar
#: fields as the ``LOCALWALLET_*`` env vars (lowercase field names), slotting
#: between env and the store-backed settings in the ONE ladder. Absent file =
#: no change. Malformed content is a value-free startup refusal (fail closed).
#: An existing ``~/.localwallet/config.json`` is simply no longer read by
#: default (no silent migration); set ``LOCALWALLET_CONFIG_PATH`` to keep
#: reading any file you choose.
CONFIG_FILE_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "config.json"


def _field_kind(field) -> type:
    """Expected JSON type for a Settings field (bool/int/float, else str)."""
    if isinstance(field.default, bool):
        return bool
    if isinstance(field.default, int):
        return int
    if isinstance(field.default, float):
        return float
    return str


def _load_config_file(path: Path, known: tuple) -> dict[str, object]:
    """Read and strictly validate the config file, or ``{}`` when absent.

    Fail-closed and value-free (TCK-CFG-002): malformed JSON, a non-object
    root, an unknown key, or a value of the wrong JSON type for its field all
    raise :class:`ValueError` naming only the key + problem kind — never the
    offending value. The whole file is validated even if env overrides a key,
    so a corrupt file always refuses startup rather than silently dropping a
    rung (ADR-0009 spirit).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError("config file could not be read") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("config file is not valid JSON") from exc
    if not isinstance(data, dict):
        # ValueError, not TypeError: a wrong-typed config file is a user
        # config error (fail-closed, value-free), not a programmer misuse.
        raise ValueError("config file must be a JSON object")  # noqa: TRY004
    by_name = {f.name: f for f in known}
    out: dict[str, object] = {}
    for name, value in data.items():
        field = by_name.get(name)
        if field is None:
            raise ValueError(f"unknown config key: {name}")
        kind = _field_kind(field)
        if kind is bool:
            if not isinstance(value, bool):
                raise ValueError(f"config key {name} must be a boolean")
        elif kind is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"config key {name} must be an integer")
        elif kind is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"config key {name} must be a number")
            value = float(value)
        elif not isinstance(value, str):
            raise ValueError(f"config key {name} must be a string")
        out[name] = value
    return out


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
    # TLS trust for the chain backend's https transport (TCK-BACKEND-001;
    # ADR-0018 amendment). ``True`` (fail-closed shipped default) verifies the
    # backend's certificate. ``False`` builds the httpx client with
    # verification OFF — the deliberate escape hatch for self-hosted https
    # backends serving a private-CA or self-signed cert (Start9 etc.), whose
    # connections httpx otherwise refuses as ``ConnectError``. Ladder is
    # env > config file > default ONLY — no stored rung (unlike
    # ``chain_base_url``): downgrading transport security is a host/operator
    # decision, never a UI-toggled setting, and every other boolean scalar
    # here (price_enabled, node_detection_enabled) is env/file-only too. When
    # this resolves False the app prints one honest value-free warning line at
    # startup (app.TLS_UNVERIFIED_WARNING). Same construction path as
    # ``chain_base_url`` (ChainConfig.from_settings → EsploraClient), so a
    # self-hosted config gets both knobs from one place.
    tls_verify: bool = True
    request_timeout_s: float = 10.0
    max_retries: int = 3
    network: str = "main"
    store_path: str = "localwallet.db"
    price_ttl_s: float = 60.0
    price_enabled: bool = True
    #: Display currency (TCK-FIAT-002, ADR-0011 amendment): a DECIMAL-STRING
    #: -style closed code, same gap-limit shape — empty string = UNSET (fall
    #: through to the stored settings key, else :data:`
    #: DEFAULT_DISPLAY_CURRENCY` of "usd"). Env name
    #: ``LOCALWALLET_DISPLAY_CURRENCY``; config-file key ``display_currency``.
    #: Case-insensitive on every rung; :func:`resolve_display_currency`
    #: validates against :data:`DISPLAY_CURRENCIES` and refuses an unknown
    #: code fail-closed and value-free (startup refusal, ADR-0009 spirit).
    display_currency: str = ""
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
    #: (``LOCALWALLET_GAP_LIMIT`` or the ``gap_limit`` config-file key), as a
    #: DECIMAL STRING. Empty string = unset
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
    # --- Web UI launch (TCK-LAUNCH-001; ADR-0024 §6 amendment) ---
    # Fixed loopback port for the web UI. Ladder is env > config file >
    # shipped default ONLY (no stored rung — like tls_verify, the bind
    # surface is an operator/host decision). Default ``0`` = the shipped
    # ephemeral OS-assigned port. A predictable port makes the per-launch
    # token MORE valuable, not less (ADR-0024 §6: the token is the sole
    # credential; nothing about it weakens with a known port). The value
    # never enters logs/errors (the app's port-failure line is value-free).
    web_port: int = 0

    @classmethod
    def from_env(
        cls, config_path: str | Path | None = None
    ) -> Settings:
        """Build a Settings instance from env vars and the optional config file.

        Recognized variables: ``LOCALWALLET_ESPLORA_BASE_URL``,
        ``LOCALWALLET_CHAIN_BASE_URL``,
        ``LOCALWALLET_REQUEST_TIMEOUT_S``, ``LOCALWALLET_MAX_RETRIES``,
        ``LOCALWALLET_NETWORK``, ``LOCALWALLET_STORE_PATH``,
        ``LOCALWALLET_PRICE_TTL_S``, ``LOCALWALLET_PRICE_ENABLED``,
        ``LOCALWALLET_FEE_CACHE_TTL_S``, ``LOCALWALLET_RPC_COOKIE_PATH``,
        ``LOCALWALLET_RPC_PORT``, ``LOCALWALLET_LOCAL_MEMPOOL_URL``,
        ``LOCALWALLET_NODE_DETECTION_ENABLED``,
        ``LOCALWALLET_TLS_VERIFY``,
        ``LOCALWALLET_WATCH_INTERVAL_S``, ``LOCALWALLET_GAP_LIMIT``,
        ``LOCALWALLET_UTXO_TARGET_MIN_SATS``,
        ``LOCALWALLET_UTXO_TARGET_MAX_SATS``,
        ``LOCALWALLET_CONSOLIDATE_BELOW_SAT_VB``,
        ``LOCALWALLET_DISPLAY_CURRENCY``,
        ``LOCALWALLET_WEB_PORT``.
        Unknown variables are ignored.

        Boolean fields accept ``0``/``1`` or ``true``/``false``/``yes``/``no``
        (any case); anything else raises :class:`ValueError` (fail closed —
        config errors are programmer errors).

        Precedence per keyed setting (the ONE ladder, TCK-CFG-002)::

            env LOCALWALLET_*  >  config file  >  stored (DB, via resolve_*)  >  shipped default

        ``config_path`` defaults to :data:`CONFIG_FILE_PATH`. Set the
        ``LOCALWALLET_CONFIG_PATH`` env var to read any other file instead
        (the escape hatch if the repo-root ``config.json`` isn't where you
        want it). The config file
        uses the lowercase field names (``gap_limit``, ``chain_base_url``,
        …) with per-field JSON types (bool/int/float/string); a key already
        set by env is left to env. A malformed file raises :class:`ValueError`
        (value-free) — fail closed. An absent file is a no-op.
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
        # Config-file rung: fills in any key env did not set (env > file).
        # Strict fail-closed parse of the WHOLE file regardless of env.
        path = Path(config_path) if config_path is not None else Path(
            os.environ.get("LOCALWALLET_CONFIG_PATH") or CONFIG_FILE_PATH
        )
        for name, value in _load_config_file(path, fields(cls)).items():
            env_name = f"LOCALWALLET_{name.upper()}"
            if os.environ.get(env_name) is None:
                values[name] = value
        return cls(**values)


def resolve_chain_base_url(
    env_value: str | None, stored_value: str | None
) -> str | None:
    """Resolve the chain backend selection (ADR-0023 precedence; TCK-ONB-002).

    Pure function — no env reads, no I/O, no store import. The first argument
    is the value already merged by ``Settings.from_env`` — env and config-file
    (TCK-CFG-002) collapsed into one rung (env wins over file inside the
    merge). Precedence::

        env  >  config file  >  stored choice  >  None

    i.e. the caller injects ``Settings.chain_base_url`` (env-or-file) as
    ``env_value`` and the stored value as ``stored_value``. Each rung treats
    ``None``, the empty string, and whitespace-only as
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


def normalize_display_currency(raw: str, source: str) -> str:
    """Case-insensitively parse one currency code to its canonical lowercase
    form (TCK-FIAT-002), or raise :class:`ValueError` naming only the
    ``source`` rung and the closed enum — never the offending value
    (ADR-0009 value-free discipline: a corrupt setting never silently
    reverts policy; the caller refuses startup).
    """
    code = raw.strip().lower()
    if code not in DISPLAY_CURRENCIES:
        raise ValueError(
            f"{source} must be one of {', '.join(DISPLAY_CURRENCIES)}"
        )
    return code


def resolve_display_currency(
    env_value: str | None, stored_value: str | None
) -> str:
    """Resolve the display currency (TCK-FIAT-002; ADR-0011 amendment).

    Pure function — no env reads, no I/O, no store import (same shape as
    :func:`resolve_chain_base_url`). The first argument is the value already
    merged by ``Settings.from_env`` — env and config-file collapsed into one
    rung (env wins over file inside the merge). Precedence::

        env  >  config file  >  stored settings key  >  "usd"

    Each rung treats ``None``, the empty string, and whitespace-only as
    *unset* (the chain_base_url convention); a present rung is parsed
    case-insensitively against :data:`DISPLAY_CURRENCIES`. An unknown code on
    ANY rung raises :class:`ValueError` (fail closed, value-free) — the app
    turns that into a startup refusal; the oracle never fetches a currency
    it cannot name.
    """
    for raw, source in (
        (env_value, "LOCALWALLET_DISPLAY_CURRENCY"),
        (stored_value, f"setting {DISPLAY_CURRENCY_SETTING!r}"),
    ):
        if raw is not None and raw.strip():
            return normalize_display_currency(raw, source)
    return DEFAULT_DISPLAY_CURRENCY


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
    resolution in ``wallet.scan._resolve_gap_limit``). The first mapping is
    the values already merged by ``Settings.from_env`` — env and config-file
    (TCK-CFG-002) collapsed into one rung (env wins over file inside the
    merge). Precedence per key::

        env  >  config file  >  stored settings key  >  shipped default

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

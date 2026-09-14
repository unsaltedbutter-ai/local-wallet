"""Application wiring: agent loop → allowlist dispatch → wallet handlers.

Phase 1 wiring (TCK-P1-004) extended with the Phase 2 send flow
(TCK-P2-004). This module only glues existing pieces together — it
implements no protocol, wallet, chain, or tx-engine logic itself:

- :func:`build_dispatch_table` — the allowlist dispatch table (closed
  intent enum → handlers). Every handler reads the SQLite store
  (populated by :func:`localwallet.wallet.scan.scan_wallet`):
  ``get_balance`` sums the cached UTXO snapshot, ``get_history`` /
  ``get_utxos`` project cached rows, ``new_address`` allocates the next
  derivation index (store bookkeeping + pure derivation — never
  network), ``respond``/``clarify`` pass the model's text through
  unchanged, and the send flow runs the dispatcher-owned state machine
  through its full Phase 3 lifecycle (TCK-P3-005): ``create_tx``
  resolves the amount (sats, or USD via the price oracle), estimates
  the fee, selects coins and builds the unsigned PSBT via the pure tx
  engine, then stages a :class:`~localwallet.tx.flow.PendingTx`;
  ``confirm_tx`` moves the flow CREATED → CONFIRMED only under the
  dual-key rule (ADR-0013): a matching ``tx_ref`` AND a CONFIRM
  classification of the SAME turn's user utterance by the deterministic
  :class:`~localwallet.tx.flow.ConfirmGate` — an LLM "yes" never counts;
  ``sign_tx`` hands the approved record to the configured signer (file
  airgap per ADR-0014, or HWI-USB per ADR-0015) and passes the signed
  PSBT through deterministic re-validation
  (:func:`localwallet.tx.revalidate.revalidate_signed_psbt`) before
  recording it — a mismatch is a hard stop, the flow stays CONFIRMED;
  ``broadcast_tx`` extracts the raw hex from the re-validated signed
  PSBT, POSTs it once (the chain layer's single-attempt policy), and
  records the BROADCAST state plus a history row; ``tx_status`` quotes
  the explorer's confirmation status for a verbatim txid, answered from
  the store's lineage truth when a bumped transaction's fate is already
  settled there (TCK-RBF-005).
- :func:`run` / :func:`main` — CLI wiring: read the watch-only key from
  ``--zpub``, ``LOCALWALLET_ZPUB``, or the STORED wallet row (the wallets
  table's canonical descriptor carries the key — TCK-LAUNCH-001:
  ``--zpub`` > env > stored), parse + gate it (mainnet-only, value-free
  errors → config-error exit 2); on an interactive first
  launch with no key supplied, the ADR-0023 onboarding greeting asks for
  it instead (:func:`localwallet.ui.onboarding.ask_watch_key`); on a WEB
  launch with no key at all the first-run watch-key form serves it
  (:class:`WatchKeyProvision` — the same parse+gate path, engine-pump
  owned). The entry point (:func:`main`) launches the web UI by default
  (TCK-LAUNCH-001, ADR-0024 amendment; ``--cli``/``LOCALWALLET_UI=cli``
  keeps the REPL) and best-effort opens the browser; no configured model
  falls back to the dev stub with a visible banner instead of exiting.
  Then open
  the store (:class:`~localwallet.config.Settings` ``store_path``),
  inject the stored backend rung (ADR-0023 decision 3:
  ``env > config file > stored > public default``, resolved through
  :func:`localwallet.config.resolve_chain_base_url` — the ONE place the
  stored choice enters), reuse or create the single wallet profile
   (descriptor-match guard, ADR-0010), pick the model runtime (remote
   debug bridge → local GGUF → ``--stub-llm``; a resolved local GGUF is
   PRELOADED on a background thread at engine start, with a launch checksum
   against the manifest's pinned sha256, so the first user query never pays
   the multi-GB build — TCK-LAUNCH-003), start the NON-BLOCKING
  startup scan (or ``--rescan``; env opt-out via ``LOCALWALLET_AUTO_SCAN=0``)
  on the dedicated chain worker (:class:`ScanFlow`, TCK-SCAN-003 /
  ADR-0022 — the REPL prompt is live while the scan runs, dots flow
  between turns, and the engine thread persists the result), run the
  first-run onboarding conversation when fresh + interactive + no backend
  on any rung (:class:`~localwallet.ui.onboarding.OnboardingFlow`,
  TCK-ONB-003 — deterministic, code-owned, never model context; the
  step-2 ask stays open across ordinary chat turns), the same backend
  branch re-armed mid-session by the ``/setup`` transcript command on
  every other interactive CLI launch (TCK-ONB-005), and run the chat
  REPL. The REPL owns the :class:`TxFlow` / :class:`SendSession` pair and
  classifies every user utterance against the confirm gate at the top of
  each turn. The REPL is the CLI transport over the queue-driven engine
  pump (:func:`_pump`, ADR-0024 §3); the threaded engine entry point for
  the web UI (WEB-002) is :func:`start_engine`.

Invariants honored here:

- Model output is untrusted input handled exclusively by
  ``AgentLoop`` → ``handle_raw`` (3-layer validation → allowlist
  dispatch). Nothing in this module parses or executes model text.
- Network I/O happens only inside ``localwallet.chain``; this module
  imports that local module, never a network library (lint-enforced).
  Handlers reach the chain only through the scan callable (lazy first
  scan) — ``new_address``/``get_history``/``get_utxos`` never do I/O.
  Scan/watch I/O runs on the dedicated :class:`ChainWorker` thread
  (ADR-0022), which holds NO Store reference: the worker returns
  immutable record sets and the ENGINE thread is the only persister
  (:meth:`ScanFlow.scan_now` and the pump's scan-event handling).
- Cache answers during the first scan carry the deterministic,
  tool-owned ``freshness`` flag (ADR-0022 decision 5); ``create_tx``
  refuses until the first scan completes (decision 6) — both computed
  by code, never by the model.
- The UI prints values verbatim from handler result dicts — it computes
  nothing, and the model narrates no numbers. Addresses appear only in
  result/narration output (``get_utxos``/``new_address``), never in
  errors, warnings, or logs.
- Nothing (banner, REPL, errors, exit paths) ever echoes the watch key.
  Watch-key error strings are value-free by contract; chain/store/scan
  error strings are scrubbed by their layers (no addresses/txids/amounts).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from pathlib import Path
from string import punctuation
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Final, TextIO

from embit import finalizer
from embit.psbt import PSBT
from embit.script import address_to_scriptpubkey

from localwallet.agent.context import sanitize_tool_output
from localwallet.agent.loop import AgentLoop, AgentTurnResult, AgentTurnStatus
from localwallet.agent.remote_runtime import (
    LLM_BASE_URL_ENV_VAR,
    LLM_MODEL_ENV_VAR,
    RemoteOpenAIRuntime,
    debug_notice,
)
from localwallet.agent.runtime import MODEL_PATH_ENV_VAR, GenerateFn, ModelRuntime
from localwallet.chain import (
    BitcoindClient,
    ChainClient,
    ChainConfig,
    ChainError,
    ConfigDisabled,
    ElectrumClient,
    FeeEstimator,
    FeeTarget,
    IncomingEvent,
    IncomingWatcher,
    PriceOracle,
    PriceUnavailableError,
    PublicInfoClient,
    WatchedTx,
    classify_failure,
    estimate_eta,
    format_sat_vb,
    minor_per_unit,
    time_since_last_block,
)
from localwallet.chain.config import (
    BITCOIND_SCHEME,
    BITCOIND_TLS_SCHEME,
    ELECTRUM_SCHEME,
)
from localwallet.chain.esplora import NETWORK_ERROR, RPC_ERROR
from localwallet.config import (
    COIN_SETTING_BOUNDS,
    COIN_SETTING_DEFAULTS,
    COIN_SETTING_KEYS,
    DEFAULT_DISPLAY_CURRENCY,
    DISPLAY_CURRENCIES,
    DISPLAY_CURRENCY_SETTING,
    PUBLIC_ELECTRUM_URL,
    Settings,
    resolve_chain_base_url,
    resolve_coin_selection_settings,
    resolve_display_currency,
)
from localwallet.node import LocalNodeReport, NodeStatus, detect_local_nodes
from localwallet.node.doctor import NodeDoctor
from localwallet.protocol import (
    BroadcastTxParams,
    BumpFeeParams,
    ClarifyParams,
    ConfirmTxParams,
    CreateTxParams,
    DispatchTable,
    Envelope,
    GetAddressesParams,
    GetBalanceParams,
    GetHistoryParams,
    GetUtxosParams,
    Handler,
    IntentName,
    NewAddressParams,
    NodeStatusParams,
    RespondParams,
    SelfTransferParams,
    SignTxParams,
    TxStatusParams,
)
from localwallet.signer.base import Signer, SignerError
from localwallet.signer.file import FilePsbtSigner
from localwallet.signer.hwi import DeviceError, HwiUsbSigner
from localwallet.store import (
    ADDRESS_ALLOCATED,
    ADDRESS_LABEL_MAX_CHARS,
    ADDRESS_USED,
    BRANCH_CHANGE,
    BRANCH_RECEIVE,
    COIN_NOTE_MAX_CHARS,
    COIN_TAGS,
    DIR_IN,
    DIR_OUT,
    DIR_SELF,
    SUPERSEDED_EVICTED,
    SUPERSEDED_REPLACED,
    AddressRecord,
    AddressRegistryRecord,
    Store,
    StoreError,
    TxRecord,
    UtxoRecord,
    WalletRecord,
    superseded_states,
)
from localwallet.tx.cpfp import (
    CpfpError,
    StuckParent,
    build_cpfp_child_plan,
)
from localwallet.tx.dust import dust_threshold
from localwallet.tx.flow import (
    PENDING_TTL_S,
    ConfirmGate,
    FlowError,
    GateDecision,
    PendingTx,
    TxFlow,
    TxFlowStatus,
)
from localwallet.tx.psbt import (
    SEQUENCE_RBF_ENABLED,
    PsbtError,
    PsbtInputSource,
    build_unsigned_psbt,
    psbt_to_base64,
)
from localwallet.tx.replacement import (
    OriginalTx,
    RbfFloorError,
    ReplacementError,
    ReplacementPlan,
    build_replacement_plan,
)
from localwallet.tx.revalidate import (
    IntendedTx,
    TamperedPsbtError,
    revalidate_signed_psbt,
)
from localwallet.tx.selection import (
    InsufficientFundsError,
    SelectionError,
    coin_partition,
    estimate_tx_vsize,
    fee_sats_for,
    select_coins,
)
from localwallet.ui.onboarding import (
    BACKEND_CHOICE_PUBLIC,
    BACKEND_CHOICE_SETTING,
    CONFIRMED,
    PUBLIC_CHOSEN_ACK,
    PUBLIC_LOADING_NOW,
    SWITCH_AFTER_SCAN,
    SWITCHING_NOW,
    WEB_SETUP_HINT,
    OnboardingFlow,
    _looks_like_seed,
    ask_watch_key,
)
from localwallet.wallet import scan as wallet_scan
from localwallet.wallet.derivation import BranchDeriver, derive_addresses
from localwallet.wallet.descriptor import (
    MAINNET_COIN_TYPE,
    SCRIPT_PURPOSES,
    ParsedKey,
    WalletDescriptor,
    WatchKeyError,
)

if TYPE_CHECKING:  # circular at runtime: ui.web.server imports this module
    from localwallet.ui.web.server import WebServer

__all__ = [
    "AUTO_SCAN_ENV_VAR",
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_SIGNER_DIR",
    "DISPLAY_CURRENCY_ENV_VAR",
    "DISPLAY_CURRENCY_SETTING",
    "EVENT_MODEL_PROGRESS",
    "GAP_LIMIT_ENV_VAR",
    "MODEL_CARD_QUESTION",
    "MODEL_DECLINED_LINES",
    "MODEL_DOWNLOAD_COMMAND",
    "MODEL_INTEGRITY_WARNING",
    "MODEL_LATER_COMMAND",
    "MODEL_PRELOADED_NOTICE",
    "MODEL_PRELOAD_NOTICE",
    "NODE_STATUS_DETECTION_DISABLED",
    "NO_MODEL_DEMO_BANNER",
    "OUT_OF_WINDOW_NOTICE",
    "PRELOAD_START",
    "PRIVACY_INDICATOR",
    "PRIVACY_INDICATOR_OWN_NODE_LOCAL",
    "PRIVACY_INDICATOR_OWN_NODE_REMOTE",
    "SIGNER_DIR_ENV_VAR",
    "SIGNER_ENV_VAR",
    "UI_ENV_VAR",
    "WATCHKEY_COMMAND",
    "WATCH_INTERVAL_ENV_VAR",
    "WATCH_INTERVAL_SETTING",
    "WATCH_KEY_SETTING",
    "WEB_PORT_ENV_VAR",
    "ZPUB_ENV_VAR",
    "ModelDownloadFlow",
    "ModelPreloadFlow",
    "SendSession",
    "SignerSelection",
    "build_dispatch_table",
    "main",
    "privacy_indicator",
    "run",
    "stub_generate",
]

#: Environment variable supplying the watch-only account key
#: (``--zpub`` overrides it).
ZPUB_ENV_VAR: Final[str] = "LOCALWALLET_ZPUB"

#: Environment variable (and config-file key ``web_port``) fixing the web
#: UI's loopback port (TCK-LAUNCH-001, ADR-0024 §6 amendment). Ladder:
#: env > config file > default 0 = ephemeral OS-assigned (the shipped
#: behavior). A fixed port is the user's opt-in convenience; per the
#: amendment it makes the per-launch token MORE valuable, not less (the
#: port was never the secret — the token is).
WEB_PORT_ENV_VAR: Final[str] = "LOCALWALLET_WEB_PORT"

#: TCP port bounds for the fixed-port knob (0 keeps the ephemeral default).
WEB_PORT_MIN: Final[int] = 0
WEB_PORT_MAX: Final[int] = 65535

#: TCK-LAUNCH-001 no-model fallback: instead of the old exit-2 refusal, a
#: launch with no ``LOCALWALLET_MODEL_PATH`` / remote bridge / ``--stub-llm``
#: runs the deterministic dev stub so the tool ALWAYS launches. The banner
#: is VISIBLE (printed through the transport's own output channel) and says
#: exactly what is canned and how to get the real model. Value-free.
#: TCK-LAUNCH-002: this bare banner remains ONLY for the case where no
#: default model can be resolved at all (no manifest / no pinned default —
#: nothing the app could offer to download). The normal "not downloaded
#: yet" case replaces it with the deterministic Yes/No card below.
NO_MODEL_DEMO_BANNER: Final[str] = (
    "No model configured — running in demo mode (canned data); set "
    "LOCALWALLET_MODEL_PATH for the real model."
)

# ------------------------------------------------- default-model resolution
# (TCK-LAUNCH-002, ADR-0001 amendment): no LOCALWALLET_MODEL_PATH means the
# DEFAULT pinned model, not the stub. The manifest carries the pinned model
# list; the entry flagged ``"default": true`` with a recorded sha256 is the
# model every launch assumes. File exists → real runtime, no banner, no
# card. File absent → the stub keeps the session alive BUT the launch is
# never silently demo: the engine emits the card below and offers to run
# the pinned downloader (models/download_model.py — hash verification
# untouched) as an engine-owned subprocess with inline progress.

#: Repo root when running from the source tree (``src/localwallet/app.py``
#: → two parents up). The wheel/frozen packaging story is ADR-0024 §12
#: (TCK-WEB-006); until then an install without this layout simply finds no
#: manifest and degrades to the plain demo banner.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
MODELS_DIR: Final[Path] = _REPO_ROOT / "models"
_MODEL_MANIFEST_PATH: Final[Path] = MODELS_DIR / "manifest.json"
_MODEL_BIN_DIR: Final[Path] = MODELS_DIR / "bin"
_MODEL_DOWNLOAD_SCRIPT: Final[Path] = MODELS_DIR / "download_model.py"


def _resolve_default_model() -> tuple[str, Path] | None:
    """The manifest's pinned default model as ``(name, gguf_path)``.

    Reads :data:`_MODEL_MANIFEST_PATH` (a repo-tracked build file, not user
    data) and selects the entry flagged ``"default": true`` — the pinned
    E2B build per ADR-0001. Fail-closed ``None`` (→ demo banner, no card):
    unreadable/absent manifest, no defaulted entry, or an entry whose
    ``sha256`` is still null (an UNPINNED model is never auto-downloaded —
    the hash-pinned verification contract is what makes the download safe).
    Never echoes any path or value; the returned path's existence is the
    CALLER's question (this function only resolves what WOULD be used).
    """
    try:
        entries = json.loads(_MODEL_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("default") is not True:
            continue
        name = entry.get("name")
        sha256 = entry.get("sha256")
        if not isinstance(name, str) or not name or not isinstance(sha256, str):
            return None
        return name, _MODEL_BIN_DIR / f"{name}.gguf"
    return None


def _manifest_pin_for(path: Path) -> str | None:
    """The manifest's pinned ``sha256`` for ``path`` when that path IS a
    pinned model entry's file (``models/bin/<name>.gguf``); ``None`` when
    nothing pins it (TCK-LAUNCH-003 launch checksum). An arbitrary
    ``LOCALWALLET_MODEL_PATH`` file has no recorded hash — the app NEVER
    invents a verdict against no pin; it simply skips the checksum.
    Fail-closed ``None`` on an unreadable manifest; value-free (the hash
    is build metadata, but it still goes nowhere near a line or a log).
    """
    try:
        entries = json.loads(_MODEL_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        sha256 = entry.get("sha256")
        if (
            isinstance(name, str)
            and name
            and isinstance(sha256, str)
            and _MODEL_BIN_DIR / f"{name}.gguf" == path
        ):
            return sha256
    return None


#: The deterministic Yes/No card (TCK-LAUNCH-002, user direction
#: 2026-09-09). Emitted by the ENGINE pump at session start when the pinned
#: default model is simply not downloaded yet — code-owned text, identical
#: on every transport (web: transcript lines + buttons driven by the
#: additive ``model_state`` field of the typed ``/state`` snapshot; CLI:
#: the same lines + the 'yes'/'no' prompt intercept below). Value-free.
MODEL_CARD_QUESTION: Final[str] = (
    "The AI model hasn't been downloaded yet. Want to download it now?"
)
MODEL_CARD_HINT: Final[str] = (
    "Answer 'yes' (or tap the button) to download it now, or 'no' to see "
    "what I can do without the model — you can start the download anytime "
    "with /download."
)
MODEL_DL_STARTED: Final[str] = (
    "Downloading the model now — it will be checked against its official "
    "fingerprint before it is installed. Progress shows here."
)
MODEL_DL_RUNNING: Final[str] = "The model download is already in progress."
MODEL_DL_DONE: Final[str] = (
    "Downloaded and verified. The model takes over the next time you start "
    "the app — this session keeps running without it."
)
MODEL_DL_FAILED: Final[str] = (
    "Model download failed — nothing was installed (an unfinished partial "
    "file is kept for the next attempt to resume). Answer 'yes' or tap the "
    "button to try again, or 'no' for the model-free actions."
)
MODEL_DECLINED_LINES: Final[tuple[str, ...]] = (
    "No problem — these work right now without the model:",
    "  /balance — show your balance",
    "  /receive — show your next receive address",
    "  /address — allocate a fresh address",
    "  /settings — show the settings the app reads",
    "  /download — start the model download later",
)
#: Bare-word CLI intercepts for the card (TCK-LAUNCH-002). Deliberately
#: refused whenever a transaction pends (the confirm gate owns 'yes'/'no'
#: then — the model-card intercept never runs ahead of ADR-0013) or once the
#: card is no longer awaiting an answer. The slash forms (/download, /later)
#: are unambiguous code-owned commands and always intercepted.
_MODEL_YES_WORDS: Final[frozenset[str]] = frozenset({"yes", "y"})
_MODEL_NO_WORDS: Final[frozenset[str]] = frozenset({"no", "n"})
MODEL_DOWNLOAD_COMMAND: Final[str] = "/download"
MODEL_LATER_COMMAND: Final[str] = "/later"
#: Model states at which a BARE yes/no is read as a card answer (the card is
#: on screen only in these). The slash forms (/download, /later) always
#: classify while a flow object exists (an explicit re-arm after declining).
_CARD_SHOWN_STATES: Final[frozenset[str]] = frozenset({"absent", "failed"})
#: Model-free quick actions — canonical slash utterances the web buttons
#: POST to /action (→ engine.submit → pump intercept). They dispatch
#: EXISTING allowlist handlers directly with CODE-built envelopes (the same
#: shape a model envelope takes), never the LLM (there may be no model).
#: Each maps a command to an intent; params are the closed empty/default.
_QUICK_ACTION_INTENTS: Final[dict[str, IntentName]] = {
    "/balance": IntentName.GET_BALANCE,
    "/address": IntentName.NEW_ADDRESS,
}
#: ``/receive`` (next receive address) and ``/settings`` (settings read) are
#: pure store reads, not model intents — handled directly (None marker).
_QUICK_STORE_COMMANDS: Final[frozenset[str]] = frozenset({"/receive", "/settings"})

# ------------------------------------------- model preload (TCK-LAUNCH-003)
#
# User direction 2026-09-09 (11): the first query paid the whole
# multi-GB llama.cpp build because ModelRuntime loaded lazily — the model
# is now PRELOADED on a bounded background thread at ENGINE start, so a
# session's first question only waits for whatever load remains (never
# an error, never a drop: the runtime's build lock serializes the wait).
# (4 part 2): a model file already present is checksummed against the
# manifest's pinned sha256 at launch — CONCURRENTLY with the load (both
# are read-only and the checksum gates nothing, so sequencing it before
# the load would only delay readiness). A mismatch is the value-free
# warning below + a line in the per-launch log, and the session KEEPS
# SERVING (documented decision: model_state stays honest about the LOAD
# — ``loading`` → ``ready`` | ``failed`` — the checksum is advisory; a
# genuinely corrupt GGUF fails inside llama.cpp's own build anyway, and
# a verdict that bricks a working wallet is the worse failure).

#: Narrated by the pump when the background preload begins (web: the
#: transcript, CLI: the terminal). Value-free. (TCK-UX-009 user copy.)
MODEL_PRELOAD_NOTICE: Final[str] = "Loading local llm."
#: Narrated ONCE when the preload reaches ``ready`` (TCK-UX-009). The
#: failed/declined paths never print it, and a launch with no preload
#: flow at all (stub / download card / remote bridge) never prints it —
#: it rides the :class:`ModelPreloadFlow` ready transition only.
MODEL_PRELOADED_NOTICE: Final[str] = "Local llm fully loaded."
#: Browser/transcript status line when the launch checksum fails.
#: Value-free: no path, no hash, no filename (the manifest is public but
#: the line stays scrubbed like every other warning).
MODEL_INTEGRITY_WARNING: Final[str] = (
    "The model file failed its integrity check — it may be corrupted. "
    "Run /download to fetch a fresh copy."
)
#: Per-launch-log line when the background LOAD itself failed (the state
#: flips to ``failed`` and the next generate re-raises through the
#: existing per-turn error path — this only records the startup fact).
_MODEL_PRELOAD_FAILED_LOG: Final[str] = (
    "model failed to preload at startup; the next request will retry the load"
)

#: TCK-WEB-008 follow-up (a): the watch key surfaced in GET /settings — a
#: display-TRUNCATED entry by default, the full value on an explicit
#: single-key read (``GET /settings?key=watch_key``). It is a PUBLIC
#: account key (watch-only app, ADR-0010/0021): not a secret, and yet it
#: still never reaches logs (access logging is suppressed wholesale) or
#: any unauthenticated surface (the endpoint is token-gated).
WATCH_KEY_SETTING: Final[str] = "watch_key"

#: The in-place REPLACE allowance decision (TCK-WEB-008 follow-up (b),
#: ADR-0024 amendment): a POST /watchkey carrying BOTH ``replace: true``
#: and ``confirm: true`` re-runs the EXISTING parse+gate path and rebinds
#: the engine; a bare submit against a configured wallet stays 409.
#: The one code-owned narration line after a successful replace — the
#: explicit old-wallet-cache warning (the store keeps the previous wallet's
#: rows; they simply stop being the active wallet's). Value-free.
_WATCHKEY_REPLACED_NOTE: Final[str] = (
    "Watch key replaced — the new wallet loads now with its own empty "
    "cache and any pending transaction was discarded. The previous "
    "wallet's cached data stays in the store; it no longer applies to "
    "this wallet."
)
_WATCHKEY_SAME: Final[str] = "that key is the wallet already connected"

#: Environment variable opting out of the startup scan (``"0"`` disables;
#: any other value — including unset — keeps the default on).
AUTO_SCAN_ENV_VAR: Final[str] = "LOCALWALLET_AUTO_SCAN"

#: Environment variable selecting the UI transport (TCK-WEB-002, ADR-0024
#: §1/§11, amended by TCK-LAUNCH-001): the ENTRY point (:func:`main`, i.e.
#: ``python -m localwallet.ui.cli``) launches the localhost web UI by
#: default; ``LOCALWALLET_UI=cli`` opts back into the REPL, ``--cli``
#: overrides the env, and ``--web`` forces web. Only the exact value
#: ``"cli"`` selects the terminal — any other value (or unset) keeps the
#: launch default. Direct programmatic :func:`run` callers (tests,
#: harnesses) keep the pre-flip CLI default unless they pass
#: ``default_web=True``.
UI_ENV_VAR: Final[str] = "LOCALWALLET_UI"

#: Dev knob (TCK-CFG-001): overrides the per-scan address gap limit
#: (``LOCALWALLET_GAP_LIMIT``). An integer 1..1000; validated fail-closed at
#: startup (exit 2, value-free) and threaded into every scan as the per-call
#: ``gap_limit`` — so precedence is: explicit per-call argument >
#: LOCALWALLET_GAP_LIMIT > DB ``gap_limit`` setting > default 20 (ADR-0009).
GAP_LIMIT_ENV_VAR: Final[str] = "LOCALWALLET_GAP_LIMIT"

#: Bounds for the env gap limit (mirror ``wallet.scan._MIN_GAP/_MAX_GAP``).
GAP_LIMIT_MIN: Final[int] = 1
GAP_LIMIT_MAX: Final[int] = 1000

#: TCK-GAP-001: the ONE value-free line narrated when a ``gap_limit`` apply
#: NARROWS the window (new value < old). A smaller window can only HIDE
#: addresses beyond it, so no auto-rescan follows (ADR-0009 amendment); the
#: line names the tradeoff — addresses beyond the window may drop out of
#: visibility, existing derivation state is preserved, and raising the value
#: again (then re-syncing) brings them back. No addresses, amounts, or
#: digits: value-free by construction.
GAP_NARROW_NOTE: Final[str] = (
    "A smaller window may hide addresses beyond it; your existing derivation "
    "state is kept, and raising the value again (then re-syncing) will show "
    "them again."
)

#: Environment rung of the background-watch interval (TCK-UX-009 ladder:
#: env > stored ``watch_interval_s`` setting > default 60; ``0`` = off —
#: the ADR-0019 escape hatch, now also settable via the settings surface).
WATCH_INTERVAL_ENV_VAR: Final[str] = "LOCALWALLET_WATCH_INTERVAL_S"

#: The store key of the persisted watch interval — allowlisted for the
#: settings surface ONLY because :func:`_resolve_watch_interval` (the
#: watcher build site in :func:`_wire`) reads it on every launch.
WATCH_INTERVAL_SETTING: Final[str] = "watch_interval_s"

#: Bounds of the whole watch-interval ladder: 0 (off) .. one day (seconds).
WATCH_INTERVAL_MIN: Final[int] = 0
WATCH_INTERVAL_MAX: Final[int] = 86400

#: The shipped default — derived from the :class:`Settings` field default,
#: never a second hardcoded literal (same single-source rule as
#: ``_PUBLIC_ELECTRUM_HOST``).
WATCH_INTERVAL_DEFAULT_S: Final[float] = float(
    Settings.__dataclass_fields__["watch_interval_s"].default
)

#: ONE value-free startup line when the STORED watch interval fails its
#: read-time validation (non-numeric or out of bounds): the default applies
#: and the watcher still builds — a corrupt setting never silently kills
#: the watch (the WRITE path is what refuses, fail-closed).
WATCH_INTERVAL_STALE_WARNING: Final[str] = (
    "The stored background-watch interval is invalid — using the default."
)

#: Environment rung of the display-currency ladder (TCK-FIAT-002, ADR-0011
#: amendment): env > config file > stored ``display_currency`` setting >
#: default ``usd``. The closed enum and the fail-closed value-free resolver
#: live in :mod:`localwallet.config` (single source, like the coin keys).
DISPLAY_CURRENCY_ENV_VAR: Final[str] = "LOCALWALLET_DISPLAY_CURRENCY"

#: Environment variable selecting the signing backend (``--signer``
#: overrides it): ``"file"`` (airgap transfer folder, ADR-0014 — the
#: default) or ``"hwi"`` (USB hardware wallet via HWI-as-a-library,
#: ADR-0015). Validated at startup; invalid values are a config error.
SIGNER_ENV_VAR: Final[str] = "LOCALWALLET_SIGNER"

#: Environment variable pointing the file signer at its transfer folder;
#: falls back to :data:`DEFAULT_SIGNER_DIR`.
SIGNER_DIR_ENV_VAR: Final[str] = "LOCALWALLET_SIGNER_DIR"

#: Default transfer folder for the file signer (ADR-0014); created on
#: demand at the first export.
DEFAULT_SIGNER_DIR: Final[str] = "./psbt-transfer"

#: The closed signer-kind set (mirrors the protocol's ``signer`` enum).
SIGNER_KIND_FILE: Final[str] = "file"
SIGNER_KIND_HWI: Final[str] = "hwi"
_SIGNER_KINDS: Final[frozenset[str]] = frozenset({SIGNER_KIND_FILE, SIGNER_KIND_HWI})

#: History entries returned when the model omits ``params.limit``
#: (protocol contract, ADR-0002 v0 extensions).
DEFAULT_HISTORY_LIMIT: Final[int] = 20

#: Sort sentinel so unconfirmed transactions (``height=None``) sort as the
#: newest entries — above any real block height / epoch timestamp.
_NEVER_CONFIRMED: Final[int] = 2**63 - 1

#: The §9 honest privacy indicator, shown verbatim at startup (PROJECT.md
#: §9 / R7 — never over-claim privacy while querying a public server).
#: TCK-DESCOPE-M3A: the PUBLIC tier is no longer mempool.space-by-default —
#: it is the explicitly consented public Electrum server
#: (:data:`localwallet.config.PUBLIC_ELECTRUM_URL`), whose operator sees
#: every queried address plus the IP (3-state banner, TCK-SEC-004 change 5):
#: when the chain backend is the user's own server the banner instead shows
#: :data:`PRIVACY_INDICATOR_OWN_NODE_LOCAL` (loopback host) or
#: :data:`PRIVACY_INDICATOR_OWN_NODE_REMOTE` (any other configured host),
#: selected by :func:`privacy_indicator` off the same single selection
#: point the chain client uses.
PRIVACY_INDICATOR: Final[str] = (
    "Querying the public Electrum server — the operator can associate "
    "queried addresses with your IP."
)

#: TCK-DESCOPE-M3A: the §9 indicator while the wallet backend is UNRESOLVED
#: (no rung, no consent): the silent public default is gone, so the honest
#: banner says NOTHING has been queried — never a stale "public" claim for
#: a server the user never chose.
PRIVACY_INDICATOR_UNCHOSEN: Final[str] = (
    "No server chosen yet — no wallet address has been queried anywhere; "
    "balances and history stay empty until you pick a backend."
)

#: The §9 privacy indicator for a self-hosted backend on THIS machine
#: (ADR-0018: ``Settings.chain_base_url`` set to a loopback host ⇒ all
#: chain lookups go to the user's own node, none to the public default).
PRIVACY_INDICATOR_OWN_NODE_LOCAL: Final[str] = (
    "Querying your own node on this machine — addresses and lookups stay here."
)

#: The §9 privacy indicator for a self-hosted backend on ANOTHER machine
#: (LAN/VPS instance). TCK-UX-009 user copy: the configured HOST is
#: interpolated (scheme/port/credentials stripped by
#: :func:`_configured_url_host`) — a display of the user's own config,
#: not a new leak surface — and the sentence itself carries the trust
#: hedge ("only private if you trust this machine"), which also carries
#: the follow-up-register L5 over-claim (the banner keys on
#: ``chain_base_url`` PRESENCE, not ownership). This is a format
#: template: render it with the host, or fall back to the generic
#: :data:`PRIVACY_INDICATOR_OWN_NODE_REMOTE_GENERIC` when no host is
#: parseable (malformed URL — better vague than broken).
PRIVACY_INDICATOR_OWN_NODE_REMOTE: Final[str] = (
    "Querying {} for transaction information. This is only private if you "
    "trust this machine."
)

#: The host-less fallback of the REMOTE banner (no host extractable from
#: the configured URL): the pre-TCK-UX-009 generic wording.
PRIVACY_INDICATOR_OWN_NODE_REMOTE_GENERIC: Final[str] = (
    "Querying your own node on another machine — nothing goes to a public API."
)

#: The 3-way chain-backend privacy mode (TCK-SEC-004 change 5), returned by
#: :func:`_backend_mode` and carried verbatim in the ``node_status`` FACTS.
BACKEND_MODE_PUBLIC: Final[str] = "public"
BACKEND_MODE_OWN_NODE_LOCAL: Final[str] = "own_node_local"
BACKEND_MODE_OWN_NODE_REMOTE: Final[str] = "own_node_remote"

#: TCK-UX-010: the FOURTH ``/state`` ``privacy_mode`` name — the ONB-006
#: first-run hold (``scan.gate.state == "awaiting_backend"``, the same
#: literal as the ``scan_state`` gate name). It OVERRIDES the 3-way mode
#: while held: the backend is not chosen yet, so no mode claim is honest.
PRIVACY_MODE_AWAITING_BACKEND: Final[str] = "awaiting_backend"

#: The CLOSED enum of ``/state`` ``privacy_mode`` values (TCK-UX-010): the
#: three :func:`_backend_mode` names plus the :data:`PRIVACY_MODE_AWAITING_BACKEND`
#: hold. Names only — never a URL/host, never a bool (the same badge rule as
#: :data:`BACKEND_KINDS`).
PRIVACY_MODES: Final[frozenset[str]] = frozenset(
    {
        BACKEND_MODE_PUBLIC,
        BACKEND_MODE_OWN_NODE_LOCAL,
        BACKEND_MODE_OWN_NODE_REMOTE,
        PRIVACY_MODE_AWAITING_BACKEND,
    }
)

#: Hosts that count as "the user's own node on this machine" — mirrors the
#: intent of ``node/detect.py`` ``_LOOPBACK_HOSTS`` (that helper cannot be
#: imported with its httpx dependency into this network-import-free module,
#: so the set is mirrored here; keep the two in sync).
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})

#: The ``node_status`` narration predicates (TCK-SEC-004 change 5, approved
#: copy) — "You are " + one of these, mirroring the banner's split. The
#: PUBLIC predicate names the consented public Electrum tier (its
#: operator/leak shape is unchanged from the old public default,
#: TCK-DESCOPE-M3A). The REMOTE predicate carries the SAME host insertion
#: as the banner (TCK-UX-009 lockstep), falling back to the generic line
#: when no host is parseable. The UNCHOSEN predicate (TCK-DESCOPE-M3A) is
#: the honest UNRESOLVED answer — with no silent public default there is
#: no server to point at, and the line says so.
_NODE_STATUS_UNCHOSEN: Final[str] = (
    "No server has been chosen yet — the app has not queried any address "
    "of your wallet. Pick one (your own Electrum or Bitcoin Core server, "
    "or explicitly the public Electrum server with its leak warning) to "
    "load the wallet."
)
_NODE_STATUS_PUBLIC: Final[str] = (
    "You are querying the public Electrum server — the operator can "
    "associate queried addresses with your IP."
)
_NODE_STATUS_OWN_NODE_LOCAL: Final[str] = (
    "You are querying your own node on this machine — addresses and lookups "
    "stay here."
)
_NODE_STATUS_OWN_NODE_REMOTE: Final[str] = (
    "You are querying {} for transaction information. This is only private "
    "if you trust this machine."
)
_NODE_STATUS_OWN_NODE_REMOTE_GENERIC: Final[str] = (
    "You are querying your own node on another machine — nothing goes to a "
    "public API."
)


def _configured_url_host(url: str) -> str | None:
    """Host of a ``scheme://host[:port]/…`` URL, parsed at string level.

    This module must not import network libraries (urllib/httpx are
    lint-banned outside ``chain/`` and the node doctor), so the host is
    extracted with plain string surgery: scheme split, path/query/fragment
    and userinfo dropped, IPv6 literal de-bracketed, port dropped. Returns
    ``None`` when no host is parseable. Never raises; value-free.
    """
    rest = url.split("://", 1)[1] if "://" in url else url
    rest = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in rest:
        rest = rest.rsplit("@", 1)[1]
    if rest.startswith("["):
        end = rest.find("]")
        host = rest[1:end] if end != -1 else rest[1:]
        return host or None
    host = rest.split(":", 1)[0]
    return host or None


def _url_without_credentials(url: str) -> str:
    """``url`` with any USERINFO removed from its authority — plain string
    surgery (urllib is lint-banned here, same style as
    :func:`_configured_url_host`): ``scheme://user:pass@host:port/path`` →
    ``scheme://host:port/path``. The effective-chain-backend display field
    (TCK-WEB-013) rides this so the settings pane can show the user's own
    configured server WITHOUT the login it embeds (bitcoind:// RPC URLs
    legitimately carry one). A URL without userinfo is returned unchanged;
    never raises.
    """
    scheme, sep, rest = url.partition("://")
    authority, slash, tail = rest.partition("/")
    if "@" in authority:
        authority = authority.rsplit("@", 1)[1]
    return f"{scheme}{sep}{authority}{'/' + tail if slash else ''}"


def _effective_chain_url(settings: Settings) -> str:
    """THE live wallet chain-backend selection, verbatim (userinfo included):
    the single selection point (ADR-0018 as amended by TCK-DESCOPE-M3A) —
    ``chain_base_url`` (boot-resolved env/config/stored fold, updated in
    place by every hot-swap and by an explicit public consent). EMPTY means
    UNRESOLVED: there is no public fallback anymore (the old
    ``esplora_base_url`` default is gone — mempool.space serves public
    fee/price info only). ``_backend_kind`` maps from it; the settings
    pane DISPLAYS it (TCK-WEB-013) through :func:`_url_without_credentials`
    — one resolution expression, so the kind and the shown URL can never
    disagree."""
    return settings.chain_base_url.strip()


#: The HOST of :data:`PUBLIC_ELECTRUM_URL` (derived, never a second
#: literal — the single-source rule the old ``_PUBLIC_DEFAULT_HOST``
#: followed). The explicit public electrum choice banners as PUBLIC: its
#: operator is a third party by definition.
_PUBLIC_ELECTRUM_HOST: Final[str] = (
    _configured_url_host(PUBLIC_ELECTRUM_URL) or ""
)

#: TCK-DESCOPE-M3A (ADR-0023 amendment 2 / its own amendment): the honest,
#: value-free startup line for a HEADLESS (non-interactive, non-web) launch
#: whose backend is UNRESOLVED. The old carve-out let such a launch scan the
#: silent public default (ADR-0023's "the command line is the operator's
#: decision"); with no public default there is nothing to scan — a headless
#: launch with no server on any rung and no recorded public consent has no
#: one to ask, so the startup scan is REFUSED (the gate holds at
#: ``awaiting_backend``; every wallet path stays leak-free).
HEADLESS_BACKEND_REFUSAL: Final[str] = (
    "No chain backend is configured (set LOCALWALLET_CHAIN_BASE_URL / the "
    "config-file key to an Electrum (ssl://) or Bitcoin Core "
    "(bitcoind://) server, or run the app interactively once to choose) — "
    "startup scan REFUSED: nothing was looked up and no address left this "
    "machine."
)


def _loopback_host_of(url: str) -> str | None:
    """The host when ``url`` targets a loopback host, else ``None``.

    The app-level mirror of the node/ detector's loopback gate (ADR-0016):
    passed into :class:`~localwallet.ui.onboarding.OnboardingFlow` as the
    "may the doctor probe this URL?" predicate, so a REMOTE candidate's
    validation is a pure ``chain/`` concern. Reuses the same string-
    surgery host parser as the banner split — one helper, one truth.
    """
    host = _configured_url_host(url)
    if host is not None and host.lower() in _LOOPBACK_HOSTS:
        return host
    return None


def _env_gap_limit(settings: Settings) -> int | None:
    """Resolve the :data:`GAP_LIMIT_ENV_VAR` override to an int, or ``None``.

    Fail-closed startup preflight (the same spirit as the zpub config-error
    path): a non-empty but non-integer — or out-of-range (1..1000) — value is
    a config error, raised with a VALUE-FREE message (the env value is never
    echoed). ``None`` means unset → scans use the DB ``gap_limit`` setting
    (else the default 20, ADR-0009).
    """
    raw = settings.gap_limit.strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{GAP_LIMIT_ENV_VAR} must be an integer between "
            f"{GAP_LIMIT_MIN} and {GAP_LIMIT_MAX}"
        ) from exc
    if not GAP_LIMIT_MIN <= value <= GAP_LIMIT_MAX:
        raise ValueError(
            f"{GAP_LIMIT_ENV_VAR} must be an integer between "
            f"{GAP_LIMIT_MIN} and {GAP_LIMIT_MAX}"
        )
    return value


def _resolve_watch_interval(settings: Settings, store: Store) -> tuple[float, str | None]:
    """The background-watch interval ladder, read at the watcher build site
    (TCK-UX-009): env ``LOCALWALLET_WATCH_INTERVAL_S`` (or the config-file
    rung — both already merged into ``settings.watch_interval_s`` by
    ``from_env``) > the stored ``watch_interval_s`` settings key > the
    shipped default 60. Bounded 0..86400 (0 = off), fail-closed:

    * an env/config-file rung outside the bounds is a startup refusal with
      a VALUE-FREE :class:`_WiringError` (the value is never echoed) — the
      same config-error spirit as :func:`_env_gap_limit`;
    * a stored rung that fails read-time validation (not a whole number in
      bounds) degrades to the default with ONE value-free warning —
      returned as the second tuple element; the caller narrates it. A
      broken setting never takes the watcher (or the launch) down.

    Known simplification (ponytail: the from_env merge erased the
    distinction): an env/config rung EXACTLY equal to the default is
    indistinguishable from "no rung" when only the config file set it (the
    env var's presence is checked directly) — the ladder then falls to
    stored/default. The file rung is the dev surface; the settings UI
    claim this ladder backs is env > stored > default.
    """
    if _env_overridden(WATCH_INTERVAL_ENV_VAR) or (
        settings.watch_interval_s != WATCH_INTERVAL_DEFAULT_S
    ):
        rung = settings.watch_interval_s
        if not WATCH_INTERVAL_MIN <= rung <= WATCH_INTERVAL_MAX:
            raise _WiringError(
                f"Configuration error: {WATCH_INTERVAL_ENV_VAR} must be "
                f"between {WATCH_INTERVAL_MIN} and {WATCH_INTERVAL_MAX}"
            )
        return rung, None
    try:
        raw = store.get_setting(WATCH_INTERVAL_SETTING)
    except (StoreError, sqlite3.Error):
        raw = None  # unreadable store → the shipped default; never a stall
    if raw is None:
        return WATCH_INTERVAL_DEFAULT_S, None
    try:
        stored = int(raw.strip())
    except ValueError:
        stored = -1
    if not WATCH_INTERVAL_MIN <= stored <= WATCH_INTERVAL_MAX:
        return WATCH_INTERVAL_DEFAULT_S, WATCH_INTERVAL_STALE_WARNING
    return float(stored), None


def _stored_display_currency(store: Store) -> str | None:
    """The stored ``display_currency`` rung, read fail-quiet (an unreadable
    store = rung unset, never a stall — the watch-interval precedent)."""
    try:
        return store.get_setting(DISPLAY_CURRENCY_SETTING)
    except (StoreError, sqlite3.Error):
        return None


def _display_currency_reader(
    settings: Settings, store: Store, session: SendSession | None = None
) -> Callable[[], str]:
    """The price oracle's LIVE display-currency reader (TCK-FIAT-002,
    ADR-0011 amendment). The ladder — env > config-file > stored > default
    — is re-resolved on EVERY oracle fetch decision, so a settings-panel
    change lands on the next quote with no restart (the honest
    ``requires_restart`` False; same per-read shape as the coin-policy keys
    in the create_tx handler). The env/config-file rung is the immutable
    boot snapshot (``Settings.from_env`` already merged them; only the
    startup-validated values ever ride it).

    TCK-FIAT-003 (the MW-17 EUR bug): when ``session`` is wired, a per-ask
    currency ONE-SHOT (``session.fiat_ask_currency``, stamped by
    :func:`_detect_fiat_ask_currency` on the user's own utterance at the
    head of the turn) takes precedence over the ladder for exactly that
    turn's handler dispatch — the currency the user ASKED in answers that
    reply; the ``display_currency`` SETTING is untouched (every later turn
    rides the ladder again). The one-shot code is always a member of the
    closed enum (the word table maps onto it), so it never needs
    validation.

    The returned callable NEVER raises: :func:`_wire` refuses startup on an
    invalid rung and the settings surface validates every write fail-closed,
    so a ValueError after boot can only mean a hand-tampered DB row — the
    ladder then degrades to the boot-validated env/file/default answer
    (never a silent currency change, never a crash inside a price fetch).
    """
    boot = resolve_display_currency(settings.display_currency, None)

    def read() -> str:
        if session is not None and session.fiat_ask_currency is not None:
            return session.fiat_ask_currency
        try:
            return resolve_display_currency(
                settings.display_currency, _stored_display_currency(store)
            )
        except ValueError:
            return boot

    return read


#: The CLOSED per-ask currency word table (TCK-FIAT-003): explicit currency
#: words/names on the user's OWN utterance, mapped onto the closed display
#: enum (``config.DISPLAY_CURRENCIES``). Whole whitespace-delimited tokens
#: only (edge punctuation stripped, lowercased) — ``usd`` INSIDE an address
#: or ``euro`` inside ``eurozone`` never matches. Ambiguous (two different
#: currencies named) or absent = no override, the display ladder answers as
#: before. This is CODE reading the user's words — the model never authors a
#: currency code and the envelope schema never carries one.
_FIAT_ASK_WORDS: Final[Mapping[str, str]] = {
    "eur": "eur",
    "euro": "eur",
    "euros": "eur",
    "usd": "usd",
    "dollar": "usd",
    "dollars": "usd",
    "gbp": "gbp",
    "pound": "gbp",
    "pounds": "gbp",
    "cad": "cad",
    "chf": "chf",
    "aud": "aud",
    "jpy": "jpy",
    "yen": "jpy",
}


def _detect_fiat_ask_currency(line: str) -> str | None:
    """Deterministic per-ask currency intercept (TCK-FIAT-003, MW-17:
    "what is my balance in Euros?" must answer in EUR even with the display
    setting unset). Returns the single currency the utterance's tokens name,
    or ``None`` (nothing named / more than one named = ambiguity, ride the
    display ladder). Same whole-token matching as the other deterministic
    utterance intercepts (the consolidation/cpfp word tables)."""
    found = {
        _FIAT_ASK_WORDS[token]
        for token in (t.strip(punctuation) for t in line.lower().split())
        if token in _FIAT_ASK_WORDS
    }
    if len(found) != 1:
        return None
    return found.pop()


def _open_browser(url: str) -> bool:
    """Best-effort browser auto-open at web launch (TCK-LAUNCH-001,
    stdlib :mod:`webbrowser` — no dependency, no subprocess of our own).

    Headless/SSH boxes and browser-less environments are the NORMAL case,
    not an error: every failure path (no registered browser —
    ``webbrowser.get()`` raising — or a controller that crashes / returns
    False) is contained here and reported to the caller as ``False``.
    Never fatal, never raises. The URL is the token-free canonical launch
    URL (ADR-0024 §6: the token never rides a URL).
    """
    try:
        return webbrowser.open(url)
    except Exception:  # noqa: BLE001 — best-effort BY CONTRACT (see docstring)
        return False


def _stored_watch_descriptor(store_path: str | None) -> WalletDescriptor | None:
    """The watch key persisted in the store, if any (TCK-LAUNCH-001:
    "if they have given us a zpub we use that one").

    The EXISTING wallets-table row IS the persistence — the canonical
    descriptor string embeds the account key — so no new schema/setting
    lands here; :meth:`WalletDescriptor.from_descriptor_string` rebuilds
    the gated descriptor (and its parsed key) from the stored row.
    Single-wallet tool (ADR-0010): the active wallet row is the key.

    Fail-closed quiet: no DB file (NEVER created just to look), unreadable
    store, no active row, or a descriptor that no longer parses (corrupt)
    all read as "never configured" — the caller falls through to the
    first-run flow (web form / interactive ask / headless refusal), and a
    genuinely broken store still surfaces through the real wiring's own
    exit-2 line later. Value-free by contract: the key is never echoed.
    """
    if not store_path or not Path(store_path).is_file():
        return None
    store: Store | None = None
    try:
        store = Store(store_path)
        wallet = store.get_active_wallet()
        if wallet is None:
            return None
        return WalletDescriptor.from_descriptor_string(wallet.descriptor)
    except (StoreError, sqlite3.Error, OSError, WatchKeyError):
        return None
    finally:
        if store is not None:
            store.close()


def privacy_indicator(settings: Settings) -> str:
    """Return the §9 privacy banner for the given backend selection.

    The wording is gated on the SAME single selection point the chain
    client uses (:func:`_effective_chain_url`) and classifies the
    configured backend (TCK-DESCOPE-M3A): UNRESOLVED (no rung — nothing
    queried, the honest empty), the consented PUBLIC ELECTRUM server, the
    user's own server on this machine (loopback host), or the user's own
    server on another machine. The split keeps the banner honest for a
    remote LAN/VPS instance — "addresses and lookups stay on this machine"
    would over-claim there (R7). Reading the knob here — rather than
    re-hardcoding a public default — keeps the banner and the node_status
    narration from ever diverging from what the client actually uses.
    """
    mode = _backend_mode(settings)
    if mode == PRIVACY_MODE_AWAITING_BACKEND:
        return PRIVACY_INDICATOR_UNCHOSEN
    if mode == BACKEND_MODE_OWN_NODE_LOCAL:
        return PRIVACY_INDICATOR_OWN_NODE_LOCAL
    if mode == BACKEND_MODE_OWN_NODE_REMOTE:
        # TCK-UX-009 user copy: name the host (scheme/port/creds stripped —
        # user-owned config display, not a leak surface); a URL with no
        # parseable host keeps the generic wording rather than printing
        # something broken.
        host = _configured_url_host(settings.chain_base_url.strip())
        if host is not None:
            return PRIVACY_INDICATOR_OWN_NODE_REMOTE.format(host)
        return PRIVACY_INDICATOR_OWN_NODE_REMOTE_GENERIC
    return PRIVACY_INDICATOR

#: TCK-BACKEND-001 (ADR-0018 amendment): the honest, value-free warning
#: printed ONCE at startup when ``Settings.tls_verify`` resolves to
#: ``False`` (self-hosted https backend with a private-CA / self-signed
#: cert). Disabling TLS verification weakens transport AUTHENTICATION, so
#: this line is unskippable in narration — it states the concrete risk (a
#: network-path observer can see the queried addresses and tamper with
#: responses) and the safer alternative, and never echoes any URL/host.
#: Printed regardless of which backend is configured: the same downgrade
#: risk applies to the public default.
TLS_UNVERIFIED_WARNING: Final[str] = (
    "Warning: TLS certificate verification for the chain backend is "
    "DISABLED. Whoever controls the network path can observe your queried "
    "addresses and tamper with the responses. Prefer a certificate your "
    "system already trusts (add the CA to the OS trust store) over "
    "LOCALWALLET_TLS_VERIFY=0 / tls_verify=false."
)

#: ADR-0009 UI surfacing for ``sync_state["out_of_window_detected"]``:
#: printed at startup when the store carries a non-empty warning payload.
#: Generic scrubbed wording (indexes only in the payload; no addresses).
OUT_OF_WINDOW_NOTICE: Final[str] = (
    "note: usage was found beyond your usual address window — a rescan is "
    "recommended; say 'rescan' is not available yet, restart with --rescan"
)

#: TCK-UX-001 pre-scan notice: printed via ``output_fn`` BEFORE the
#: startup/``--rescan`` scan runs so the user knows why the prompt is not
#: live yet (the scan probes every window address sequentially and can
#: take a while against a slow backend). Value-free by construction — no
#: addresses, amounts, or counts.
SCAN_PROGRESS_NOTICE: Final[str] = (
    "Checking for new transactions (may take a moment)…"
)

#: TCK-SEC-002 truncation surfacing: appended to the scan/rescan narration
#: ONLY when ``summary.truncated`` (any branch hit the absolute window
#: ceiling). Value-free — no addresses, amounts, or indices; the only
#: quantitative reference is the documented ``window cap`` constant. Calm
#: tone + a rescan/config nudge, mirroring :data:`OUT_OF_WINDOW_NOTICE`.
TRUNCATION_NOTICE: Final[str] = (
    "note: the address window cap was reached — usage may exist beyond it "
    "and newer transactions may not be included; consider rescanning or "
    "raising the window cap"
)

#: TCK-SCAN-003 (ADR-0022 decision 5): the closed values of the
#: tool-owned ``freshness`` result key attached to cache-served
#: wallet-read answers. ``fresh`` = the store carries a completed scan
#: (sync cursor) and no first scan is running; ``stale`` = the first
#: scan is still running (or has never completed) and the answer comes
#: from a possibly-empty/partial cache. Code computes it from the scan's
#: completion state — the model never authors freshness claims; it only
#: narrates the flag (values fixed here; nothing user- or model-derived).
FRESHNESS_FRESH: Final[str] = "fresh"
FRESHNESS_STALE: Final[str] = "stale"

#: CLI narration suffix on a stale-flagged read (ADR-0022 decision 5's
#: honesty rule for the terminal path). Value-free: it annotates that
#: the figures above may not be final; it carries no address/amount.
FRESHNESS_NOTE: Final[str] = (
    "note: wallet cache may be incomplete — the first scan has not finished"
)

#: TCK-UX-011 (ADR-0022 amendment 2): the ONE honest balance line when the
#: engine-thread read stood the lazy scan down and KICKED the background
#: flow instead (web/engine world). Static copy — value-free by
#: construction (no address/amount/digit); it subsumes the plain
#: :data:`FRESHNESS_NOTE` on a `scan_pending` answer (one note line, not
#: two). The tool-owned `scan_pending` result key drives it, never the
#: model.
SCAN_PENDING_NOTE: Final[str] = (
    "note: first scan running in the background — these figures may update when it completes"
)

#: TCK-PENDING-001: the tool-owned confirm-likelihood line on a pending
#: summary, when NO fee/ETA data backs an estimate. Static copy — value-free
#: by construction (no digits, no probability, no minutes). The documented
#: bound: the store records no fee TARGET and no first-seen timestamp for
#: cached transactions, so the eta.py ladder has nothing honest to compute
#: from here (the ladder still runs where its inputs exist — the confirmation
#: card / CREATED pending FACTS, TCK-P5-002, unchanged). A future ticket that
#: records broadcast fee-targets may replace this note's VALUE with the
#: ladder's wording; the key shape stays.
PENDING_NO_ETA_NOTE: Final[str] = (
    "No confirmation estimate right now — pending transactions have no recorded fee target to estimate from"
)

# ---------------------------------------------------------------- TCK-CHAT-001
#
# The referential-address surface: every address shown to the user carries
# a STABLE wallet-lifetime number from the store's registry (schema v4), and
# a numbered referent ("show address 3", "balance of #3") resolves
# engine-side against that registry with a bound-check. The copy below is
# drafted in the docs/ux-web-copy-2.md voice (designer §2(a)/(b)/§5 strings
# were never committed as a doc) — FLAGGED for designer review. Static,
# value-free by construction (no address/amount digits in the framing; the
# per-row values are verbatim TOOL output rendered separately).

#: One-time teaching line, shown the FIRST time a numbered address list is
#: printed to the user (persisted per wallet; never repeated).
ADDRESS_REF_HINT: Final[str] = (
    'Tip: you can refer to any of these by number — "show address 2", '
    '"balance of #2".'
)

#: The FAQ-style line (council: the visible copy for the first-shown date
#: is ONE line; the date itself is stored, never narrated per row).
ADDRESS_TRACKING_FAQ_LINE: Final[str] = (
    "I note the date I first show you each address, and every address "
    "keeps its number for good."
)

#: The out-of-range/unknown-referent answer. Value-free: it names no
#: address and echoes no fabricated state; it offers the one honest next
#: step (list the numbers that DO exist). Deliberately NOT a guess.
ADDRESS_REF_UNKNOWN: Final[str] = (
    "I don't have an address with that number — I only answer for numbers "
    "I've shown you. Say \"what addresses have I used\" for the list."
)

#: The empty-registry answer (wallet has shown no addresses yet).
ADDRESSES_NONE_SHOWN: Final[str] = (
    "I haven't shown you any addresses yet — ask me for a new one when "
    "you're ready to receive."
)

#: Settings key marking the one-time numbered-referent hint as SHOWN
#: (wallet-lifetime: survives sessions by design — the hint teaches the
#: stable-registry contract, which is exactly what persists).
_ADDRESS_REF_HINT_SETTING: Final[str] = "address_ref_hint_shown"

#: Hard caps on the FACTS-injected registry (context budget, ADR-0006):
#: at most this many entries serialize, and the joined value stays well
#: under :data:`localwallet.agent.context.MAX_FACTS_VALUE_CHARS` (2000).
#: A larger registry injects the FIRST ``_REGISTRY_FACTS_MAX`` entries and
#: the count fact, and the prompt tells the model the list can be
#: incomplete — the handler-side bound-check stays the only authority.
_REGISTRY_FACTS_MAX: Final[int] = 20

#: The per-entry registry FACTS budget (chars); entries that would push the
#: joined line past it are dropped from the injection (never truncated
#: mid-value: a cut-down address is worse than an omitted entry).
_REGISTRY_FACTS_CHARS: Final[int] = 1600

#: TCK-SCAN-003 (ADR-0022 decision 6): the friendly, value-free
#: ``create_tx`` refusal while the FIRST scan has not completed —
#: dispatcher-owned code gates it (never model judgment), mirroring the
#: ADR-0013 pending-guard style. Value movement stays blocked until the
#: scan finishes; chat (and stale-flagged reads) remain unblocked.
WALLET_LOADING_REFUSAL: Final[str] = (
    "Your wallet is still loading — the first scan has not finished, so I "
    "can't build a transaction yet. In the meantime I can share your "
    "balance, history, or a new address (any cached figures will be "
    "marked as still loading)."
)

#: TCK-PRIVACY-001 (user direction 2026-09-11): the value-free ``tx_status``
#: refusal while the first-run backend choice is UNRESOLVED (the
#: ``awaiting_backend`` hold). The ONB-006 promise is that NO query reaches a
#: server the user never picked — the honest answer is "nothing was looked
#: up", never a silent wrong answer and never a leak. The model narrates it
#: verbatim (dispatcher-owned copy, same discipline as
#: :data:`WALLET_LOADING_REFUSAL`).
NO_BACKEND_REFUSAL: Final[str] = (
    "No server has been chosen yet, so there is nothing for me to ask — "
    "that check did not run, and no query left this machine. Pick a server "
    "first (your own, or explicitly the public one), then ask me again."
)


#: Fallback UI strings (mirror the agent loop's generic containment
#: messages; the loop normally supplies these).
_GENERIC_FAILURE: Final[str] = (
    "Something went wrong while processing that request. Please try again."
)

_STUB_RESPOND_TEXT: Final[str] = (
    "(stub model, dev mode) I am a canned stand-in for the local LLM. "
    'Ask "What\'s my balance?" or "give me a new address" to exercise '
    "the wallet intents."
)
_STUB_RESPOND_ENVELOPE: Final[str] = json.dumps(
    {"v": 0, "intent": "respond", "params": {"text": _STUB_RESPOND_TEXT}}
)
_STUB_BALANCE_ENVELOPE: Final[str] = json.dumps(
    {"v": 0, "intent": "get_balance", "params": {}}
)
_STUB_HISTORY_ENVELOPE: Final[str] = json.dumps(
    {"v": 0, "intent": "get_history", "params": {}}
)
_STUB_UTXOS_ENVELOPE: Final[str] = json.dumps(
    {"v": 0, "intent": "get_utxos", "params": {}}
)
_STUB_NEW_ADDRESS_ENVELOPE: Final[str] = json.dumps(
    {"v": 0, "intent": "new_address", "params": {}}
)

#: Canned ``create_tx`` recipient for the dev stub (P0 fixture address: the
#: first receive address of the canonical mainnet fixture zpub used
#: throughout the tests).
#: Deterministic canned data for ``--stub-llm`` only — never a real payee.
_STUB_RECIPIENT: Final[str] = "bc1qypwwwujhndm5fv2wu4ly07gl20wvq0tcvpnp6u"

#: Fallback canned amount (sats) when the stub cannot parse one from the
#: user text. Deliberately above the dust bound and below typical fixtures.
_STUB_FALLBACK_AMOUNT_SATS: Final[int] = 10_000

#: Canned ``confirm_tx`` for the dev stub. The placeholder ``tx_ref``
#: deliberately does NOT match any real pending reference (the stub cannot
#: see the flow's id factory): dispatching it demonstrates the flow's
#: refusal path in dev mode. Deterministic tests inject generate closures
#: that quote the real pending ``tx_ref`` instead (see tests/test_e2e_skeleton.py).
_STUB_CONFIRM_TX_ENVELOPE: Final[str] = json.dumps(
    {"v": 0, "intent": "confirm_tx", "params": {"tx_ref": "dev-stub-pending-tx"}}
)

#: Canned Phase 3 lifecycle envelopes for the dev stub (TCK-P3-005): the
#: ``tx_ref`` placeholders deliberately cannot match a real flow record —
#: dispatching them demonstrates the sign/broadcast refusal paths. The
#: ``tx_status`` placeholder IS a shape-valid txid (64 lowercase hex) that
#: exercises the chain lookup path; deterministic tests inject closures
#: quoting the flow's real references instead.
_STUB_SIGN_TX_ENVELOPE: Final[str] = json.dumps(
    {"v": 0, "intent": "sign_tx", "params": {"tx_ref": "dev-stub-pending-tx"}}
)
_STUB_BROADCAST_TX_ENVELOPE: Final[str] = json.dumps(
    {"v": 0, "intent": "broadcast_tx", "params": {"tx_ref": "dev-stub-pending-tx"}}
)
_STUB_TX_STATUS_TXID: Final[str] = "ab" * 32
_STUB_TX_STATUS_ENVELOPE: Final[str] = json.dumps(
    {"v": 0, "intent": "tx_status", "params": {"txid": _STUB_TX_STATUS_TXID}}
)
_STUB_NODE_STATUS_ENVELOPE: Final[str] = json.dumps(
    {"v": 0, "intent": "node_status", "params": {}}
)

#: ``node_status`` detection-state literal when node detection is disabled
#: (``Settings.node_detection_enabled`` false ⇒ clean "detection disabled"
#: state, no probing — LOCALWALLET_NODE_DETECTION_ENABLED=0). The narration
#: says so and offers no fabricated findings.
NODE_STATUS_DETECTION_DISABLED: Final[str] = "disabled"

#: User-facing narration lines for the send flow (TCK-P2-004; card
#: redesign TCK-UX-002 per docs/ux-tx-card-feedback.md — every string here
#: is the designer-approved copy quoted VERBATIM from that doc's appendix
#: block; every VALUE they carry comes verbatim from the handler result
#: dict — the UI computes nothing (integer formatting of result values
#: only, the same display-truncation class as txid shortening)).
_CARD_ASK_LINE: Final[str] = (
    'Pending — say "sign" to review it on your device, or "cancel" to discard.'
)
#: Variant-A tail (card.offer, doc §2.0/§2.2): the proactive speed offer.
#: Fires ONLY when the create_tx envelope carried no fee_target
#: (display-only key ``fee_target_defaulted``). Hard copy rule (§2.2.3):
#: the offer is a wh-question — never a polar (yes/no) one, because
#: "yes"→CONFIRM and "no"→DENY would answer a yes/no offer with the
#: precisely wrong meaning. The keep-word is "sign" (GATE-MERGE shipped).
_CARD_OFFER_TAIL: Final[str] = (
    'How important is this one? Say "faster" to confirm sooner (a slightly '
    'higher fee) or "slower" to save money (it may take longer) — or say '
    '"sign" to keep this rate · '
)
#: Variant-B tail (card.details_tail): a speed preference is already known
#: (the request carried one, or the offer was answered) — never re-asked;
#: the offer is one-shot by construction.
_CARD_DETAILS_TAIL: Final[str] = "full breakdown: /details"
#: FLOW-REQUOTE lead line (card.requote_lead, doc §2.3); same-rung
#: re-quotes ("medium" answered to medium) carry no direction word.
_CARD_REQUOTE_LEAD: Final[str] = (
    "Re-quoted at the {direction} rate — review the new fee below:"
)
_CARD_REQUOTE_LEAD_SAME_RUNG: Final[str] = "Re-quoted — review the new fee below:"
_CARD_RATE_CEILING: Final[str] = (
    "That's already the fastest recommended rate (next-block target). "
    "If you'd like confirmation sooner, tell me a rate in sat/vB and I'll "
    'rebuild the transaction at that rate — or say "sign" to proceed or '
    '"cancel" to discard.'
)
_CARD_RATE_FLOOR: Final[str] = (
    "That's already the cheapest recommended rate — we never quote below "
    'the network minimum. Say "sign" to proceed or "cancel" to discard.'
)
#: TCK-FEE-004 min-relay clamp line: printed ONCE under the card's Fee line
#: whenever the bid this card shows IS the min-relay floor (a policy rung
#: the clamp raised, or an explicit user rate clamped UP toward it — never
#: a silent alteration of an explicit rate). Deliberately source-neutral
#: wording: the floor came from the node (bitcoind relayfee), from the fee
#: source (recommended minimumFee) or from the ASSUMED 1 sat/vB every
#: build gate here enforces — "the floor this wallet enforces" is honest
#: for all three (no narration lie on the fail-closed rung); we never
#: claim the node said something it didn't. The quoted rate is verbatim
#: from the result's own fee_rate_display; no amounts.
_CARD_FEE_FLOOR_NOTE: Final[str] = (
    "Note: the min-relay floor is {rate} sat/vB — the lowest rate this "
    "wallet builds at, so the fee uses it."
)
#: Mix warning (TCK-UTXO-004, docs/ux-utxo-notes-design.md §4.3): a dedicated
#: conditional line printed ONLY when the FINAL selection spans the KYC /
#: not-KYC partitions — which happens only when no pure pool funds the amount
#: (§2.1). Copy is verbatim from the doc's §4 block, code-owned, never
#: model-authored. Honesty frame: "coins you marked" is the user's own claim
#: (§1.1); we verify nothing. Gate audit (§4.4): the only actionable verb it
#: names is the existing "cancel" — no new gate word.
_CARD_MIX_WARNING: Final[str] = (
    'Heads up: this mixes coins you marked KYC with coins you didn\'t — '
    'say "cancel" if that\'s not what you want.'
)
_CANCELLED_LINE: Final[str] = "Transaction cancelled."
_GUIDANCE_STILL_PENDING: Final[str] = (
    'Still pending — say "sign" to send it to your device, or "cancel" '
    "to discard it."
)
_GUIDANCE_AMBIGUOUS: Final[str] = (
    'That was ambiguous — say "sign" to proceed with the pending '
    "transaction, or \"cancel\" to discard it."
)

# ---------------------------------------------------------- self-transfer
# (TCK-TX-SELF-001: explicit on-demand reshuffles — SPLIT one coin into N
# equal parts / CONSOLIDATE coins below a size into one. Every value below
# is dispatcher-owned code text; the plan's numbers reach the terminal ONLY
# through the card renderer, verbatim from the handler result dict.)

#: Input ceiling for ONE explicit consolidate (TCK-TX-SELF-001). The
#: ADR-0012 step-5 guard (≤4 added inputs) is a *spend-time* fold policy —
#: an explicit on-demand merge legitimately sweeps more, bounded here at a
#: documented constant: 256 P2WPKH inputs ≈ 17.5k vB, comfortably inside
#: Core standardness (100k vB) even with the fastest-rung fee bumps.
#: ponytail: hard cap — a wallet holding more sub-threshold coins runs a
#: second consolidate after the first confirms; chunked auto-repeat when
#: someone actually has >256 dust coins at once.
MAX_SELF_TRANSFER_CONSOLIDATE_INPUTS: Final[int] = 256

#: Honest refusals (value-free; the user-facing InsufficientFunds line is
#: the existing ADR-0012 exception, rendered from structured keys).
_SELF_NOTHING_BELOW: Final[str] = (
    "None of your coins are smaller than that — nothing to consolidate."
)
_SELF_SPLIT_BELOW_DUST: Final[str] = (
    "That coin is too small to split into that many pieces — each piece "
    "would fall below the network's minimum output size. Try fewer pieces."
)
_SELF_TOO_MANY_SMALL: Final[str] = (
    f"Too many small coins to merge in one transaction (over "
    f"{MAX_SELF_TRANSFER_CONSOLIDATE_INPUTS}) — say a smaller size to "
    "merge a share of them."
)

#: TCK-CPFP-002: the RBF-004 step-0.5 guard became the real conversation.
#: This line survives for the ONE genuinely unsupported shape left: a
#: self_transfer handler wired WITHOUT a session (a legacy direct-call
#: wiring that cannot carry the conversation's dispatcher-owned state).
#: Production wirings always pass the session — the value-free refusal
#: stands as the fail-closed backstop.
_CPFP_NOT_READY: Final[str] = (
    "Child-pays-for-parent fee bumping isn't available yet — nothing was "
    "staged or sent."
)

# --- cpfp conversation copy (TCK-CPFP-002) ----------------------------------
#
# Same discipline as the bump block: every line is dispatcher-owned text;
# value-bearing figures reach the card ONLY as structured result keys
# rendered verbatim from the pure builder's plan / the store's rows.

#: The stuck-INBOUND resolver's indexed ask (deliverable 1: multiple
#: unconfirmed inbound coins — never a guess). Copy mirrors the bump
#: target-ask (the never-trap tail included).
_CPFP_TARGET_HEAD: Final[str] = (
    "You have {count} unconfirmed payments coming in — which one should I "
    "hurry? Say a number; any other words set this aside."
)
#: The THREE-option menu head (deliverable 2): the hurry options ride ONLY
#: over CONFIRMED own coins eligible as merge inputs; with none eligible
#: the plain single-input plan is staged directly (no fake menu).
_CPFP_OPTIONS_HEAD: Final[str] = (
    "Your stuck payment can carry a hurry fee of its own — or one of your "
    "confirmed coins can pay it too. Pick one:"
)
_CPFP_OPTIONS_TAIL: Final[str] = (
    "Say a number, or 'smallest' / 'largest' — or name the coin's label. "
    "Any other words set this aside."
)
#: The card's framing row (deliverable 3): the child-pays-for-parent
#: explanation, and the COUNCIL MUST hedge (glm#3) in its verbatim shape —
#: the reorg/undo hedge IS that clause; the renderer prints both lines.
_CPFP_CARD_HEAD: Final[str] = (
    "Child pays for parent — this new transaction spends your stuck "
    "incoming payment and pays the fee that hurries it"
)
_CPFP_HEDGE_LINE: Final[str] = (
    "Heads up: this spends a payment that hasn't confirmed yet — if that "
    "payment is undone, this won't send"
)
_CPFP_PARENT_LINE: Final[str] = "Hurries: {parent_txid} — this child pays its way too"
#: Honest package rows: with the parent's recorded fee known, the plan's
#: integer-DOWNED package-rate FLOOR (verbatim from the builder); with it
#: unknown, NO rate figure is ever quoted (tx/cpfp.py's bound).
_CPFP_PACKAGE_LINE: Final[str] = (
    "Package: at least {package_rate} sat/vB counting the payment it hurries"
)
_CPFP_PACKAGE_UNKNOWN: Final[str] = (
    "The stuck payment's own fee is unknown — no package rate can be "
    "quoted; it may confirm slower than this child's rate"
)
_CPFP_NOTHING_UNCONFIRMED: Final[str] = (
    "Nothing unconfirmed is coming in right now — there is no stuck "
    "payment to hurry."
)
_CPFP_ALREADY_CONFIRMED: Final[str] = (
    "That payment already confirmed — there is nothing left to hurry."
)
#: Mid-conversation recheck (the RBF-004 pattern): the chosen inbound left
#: the unconfirmed set while an ask stood open — replaced/undone.
_CPFP_INBOUND_GONE: Final[str] = (
    "That payment is no longer coming in — it was undone or replaced; "
    "nothing was staged or sent."
)
_CPFP_COIN_GONE: Final[str] = (
    "That coin is no longer yours to spend — nothing was staged or sent."
)
#: The pure builder's fee-math refusals (CpfpRefusalReason) — value-free
#: per ADR-0012's CPFP amendment (the builder never quotes sats, and CPFP
#: card figures come from the PLAN, never an error path).
_CPFP_CANNOT_FUND: Final[str] = (
    "That payment can't fund a hurry at this rate — nothing was staged or "
    "sent. Try again when fees are lower, or ask for a slower child."
)
_CPFP_PLAN_FAILED: Final[str] = (
    "The stuck payment does not add up — nothing was staged."
)
_CPFP_FLOW_BUSY: Final[str] = (
    "Finish the transaction already in flight first (sign it or cancel "
    "it), then hurry a stuck payment."
)
#: Broadcast-failure copy for a DEAD parent (deliverable 5): input-
#: unspendable, classified from fresh store/chain truth by the broadcast
#: handler. Never pitches a retry (there is nothing left to hurry) — the
#: value-free ``broadcast_failed`` line keeps that wording for transient
#: failures only.
_CPFP_PARENT_GONE: Final[str] = (
    "Not broadcast — the payment this child spends is gone (it was undone "
    "or replaced), so this child can never send; nothing to hurry anymore."
)

# --- consolidation conversation copy (TCK-CONS-001) -------------------------
#
# The roll-up and the asks are CODE-RENDERED, deterministic views of the
# store's rows (per-tag count + sats; ascending coin lists). Label/tag words
# are matched HERE in code — the user's consolidation utterances are
# intercepted BEFORE the model sees them, so a tag/label word never routes
# through the model (the RBF-004 lesson generalized from ask ANSWERS to the
# conversation's OPENING; §7.10). Amounts/labels print verbatim, store-side;
# nothing here enters FACTS or the transcript.

#: The label roll-up ask (deliverable 1): one row per non-empty tag group
#: (canonical COIN_TAGS order, the unlabeled group last), count + sats
#: computed by code from the store's rows.
_CONS_ROLLUP_HEAD: Final[str] = (
    "Here is how your coins are labeled — which group should I consolidate? "
    "Say a label or its number; any other words set this aside."
)
_CONS_ROLLUP_ROW: Final[str] = "  {index}. {tag} — {coins} coin{s} · {sats:,} sats"
_CONS_ROLLUP_UNLABELED: Final[str] = "(unlabeled)"

#: The count ask (deliverable 2): "1 coin or N?" over the chosen label's
#: coins. The tag's total is code-computed; both figures ride verbatim.
_CONS_COUNT_HEAD: Final[str] = (
    "You marked {coins} coin{s} {tag} — {sats:,} sats together. "
)
_CONS_COUNT_ASK: Final[str] = (
    "Consolidate all {coins} into one new coin, or just 1 of them? Say "
    "'all', '{coins}' or '1'; any other words set this aside."
)

#: The no-label path (deliverable 3): ascending UTXO list — amount, label,
#: confirm-state — selected by the CHAT-001 registry number (stable, never
#: positional; coins sharing an address share its number, and a number pick
#: takes every coin listed under it).
_CONS_LIST_HEAD: Final[str] = (
    "Your coins, smallest first — say the NUMBER to pick (any other words "
    "set this aside):"
)
_CONS_LIST_ROW: Final[str] = "  #{number}. {sats:,} sats · {label} · {state}"
_CONS_LIST_NO_LABEL: Final[str] = "no label"

#: The resolution restatement (the CHAT-001 glm#7 invariant): every number
#: resolution prints the FULL address before anything is planned.
_CONS_RESOLVED: Final[str] = "Coin #{number} at {address} — {sats:,} sats."
_CONS_EMPTY: Final[str] = "You have no coins to consolidate yet."
_CONS_COIN_GONE: Final[str] = (
    "A coin you picked is no longer yours to spend — nothing was staged "
    "or sent."
)

#: The consolidation plan echo (deliverable 4): the conversation's card line,
#: built by the renderer from the staged result's OWN ``inputs_count`` /
#: ``amount_sats`` (engine-computed totals, quote-verbatim) — never a
#: promise of what the plan will be. ``Merge``/``UTXO(s)`` pluralities are
#: the renderer's; the figures are the record's.
_CONS_PLAN_LINE: Final[str] = (
    "Merge {sources} UTXO{s} to create one new UTXO of {amount:,} sats"
)

#: Closed-set tag words the deterministic opener/intercept match (the
#: §1.4 vocabulary + honest display synonyms; user data stays in code).
_CONS_TAG_TERMS: Final[dict[str, tuple[str, ...]]] = {
    "kyc": ("kyc",),
    "exchange": ("exchange",),
    "p2p": ("p2p", "peer"),
    "purchase": ("purchase",),
    "consolidation": ("consolidation",),
}
_CONS_UNLABELED_TERMS: Final[tuple[str, ...]] = ("unlabeled", "unlabelled")

#: The opener's conservative phrase shape: a consolidation VERB plus a
#: consolidation OBJECT word, and NO bare digit (an explicit
#: "…under 100000 sats" threshold stays on the existing TX-SELF-001 model
#: route — the golden phrasings are untouched).
_CONS_VERBS: Final[frozenset[str]] = frozenset(
    {
        "consolidate",
        "consolidates",
        "consolidating",
        "merge",
        "merges",
        "merging",
        "sweep",
        "sweeps",
        "sweeping",
    }
)
_CONS_OBJECT_TERMS: Final[frozenset[str]] = frozenset(
    {
        "coin",
        "coins",
        "utxo",
        "utxos",
        "dust",
        "ones",
        "wallet",
        "funds",
        "everything",
        "all",
        "small",
        "labeled",
        "unlabeled",
        "unlabelled",
    }
)

#: The count ask's answer vocabulary ("all 4"/"4" merge the group; "1"
#: opens the list to pick a single coin). A hit on BOTH sets is ambiguous.
_CONS_ALL_TERMS: Final[frozenset[str]] = frozenset(
    {"all", "everything", "both", "them", "lot", "total", "entire", "yes", "merge", "merging"}
)
_CONS_ONE_TERMS: Final[frozenset[str]] = frozenset({"1", "one", "single", "lone"})

#: Fee-rung phrases the opener parses IN CODE (the RBF-004 MAJOR lesson:
#: the stated knob must survive every ask intercept — it rides the ask
#: record and is re-quoted onto the code-built envelope). "no hurry"/
#: "not urgent" carry deny-shaped words ("no"/"not") that must NOT
#: suppress the opener — the phrase is checked first.
_CONS_SLOW_PHRASES: Final[tuple[str, ...]] = ("no hurry", "not urgent", "slowly", "slow", "cheap")
_CONS_FAST_PHRASES: Final[tuple[str, ...]] = ("asap", "urgent", "hurry", "faster", "fast", "quick")


def _consolidation_intent(line: str) -> tuple[str | None, str | None] | None:
    """Deterministic consolidation-opener match (TCK-CONS-001, BEFORE the
    model sees the line): ``None`` = not a consolidation opener (ordinary
    pipeline), else ``(tag, fee_target)`` where ``tag`` is a closed-set id,
    ``""`` for the unlabeled path, or ``None`` for the label roll-up, and
    ``fee_target`` is the rung the user's words named (``None`` = the
    engine's MEDIUM default, unchanged). Any deny token suppresses the
    intercept (the HW-005 slice-C review-MEDIUM rule) — except inside a
    matched slow-rung phrase ("no hurry" is an urgency, not a refusal).
    A line carrying a bare digit is NOT intercepted: an explicit size
    threshold stays on the existing model route (golden phrasings
    preserved). ponytail: word-set matching — "merge my notes about
    coins" style collisions ride the never-trap; the card remains the
    authority on what any plan actually spends."""
    words = [w for w in (t.strip(punctuation) for t in line.lower().split()) if w]
    if not words or not any(w in _CONS_VERBS for w in words):
        return None
    if not any(w in _CONS_OBJECT_TERMS for w in words):
        return None
    if any(w.isdigit() for w in words):
        return None
    joined = " ".join(words)
    fee: str | None = None
    # SLOW phrases are checked FIRST and win: "no hurry" contains the fast
    # trigger "hurry" — the negated phrase is the urgency, not its word.
    if any(p in joined for p in _CONS_SLOW_PHRASES):
        fee = "slow"
    elif any(p in joined for p in _CONS_FAST_PHRASES):
        fee = "fast"
    # A deny token suppresses the intercept (HW-005 slice-C rule) — except
    # the deny-shaped word INSIDE a matched slow phrase ("no hurry"/"not
    # urgent" are urgencies, not refusals).
    if fee != "slow" and any(w in _BUMP_DENY_TOKENS for w in words):
        return None
    if any(w in _CONS_UNLABELED_TERMS for w in words):
        return "", fee
    for tag, terms in _CONS_TAG_TERMS.items():
        if any(w in terms for w in words):
            return tag, fee
    return None, fee


# --- bump conversation copy (TCK-RBF-004) -----------------------------------
#
# Every line is dispatcher-owned text (the model never authors it; the
# renderer prints it verbatim through the sanitizer). Value-bearing figures
# reach the card ONLY as structured result keys rendered from the pure
# builder's output / the store's rows (quote-verbatim rule), never as
# hand-written constants here.

#: The plan card's BIP-125 hedge row — the same honest substring the
#: TCK-RBF-005 tx_status replaced-copy carries, in card shape.
_BUMP_REPLACES_LINE: Final[str] = (
    "Replaces: {old_txid} — the original may still confirm; only one of "
    "these two ever will"
)
#: The plan card's fee-delta row: values verbatim from the builder's plan
#: (old fee from the recorded original, new fee the plan's, delta their
#: integer difference — ENGINE-computed, never a ladder rung alone).
_BUMP_FEE_LINE: Final[str] = (
    "Fee: {old_fee} sats → {new_fee} sats (paying {delta} sats extra)"
)
_BUMP_NOTHING_IN_FLIGHT: Final[str] = (
    "Nothing of yours is in flight right now — there is no fee to bump."
)
_BUMP_ALREADY_CONFIRMED: Final[str] = (
    "That transaction already confirmed — there is nothing left to bump."
)
_BUMP_ALREADY_SETTLED: Final[str] = (
    "That transaction's race is already settled — check its status; there "
    "is nothing further to bump."
)
_BUMP_UNRECORDED: Final[str] = (
    "I can't rebuild that transaction's funding plan from my records — the "
    "fee bump covers the transaction this app last broadcast."
)
_BUMP_MULTI_OUTPUT: Final[str] = (
    "A fee bump doesn't cover a multi-output coin reshuffle yet — nothing "
    "was staged or sent."
)
_BUMP_FLOW_BUSY: Final[str] = (
    "Finish the transaction already in flight first (sign it or cancel "
    "it), then bump the fee."
)
_BUMP_REQUOTE_GUIDANCE: Final[str] = (
    "That pending transaction is a fee bump — to change its speed, cancel "
    "it and ask for the bump again (say 'faster' or 'slower')."
)
_BUMP_PLAN_FAILED: Final[str] = (
    "The recorded transaction does not add up — nothing was staged."
)
_BUMP_FUNDING_HEAD: Final[str] = (
    "The change alone can't reach the replacement minimum — the bump needs "
    "one of your CONFIRMED coins. Pick one:"
)
_BUMP_FUNDING_TAIL: Final[str] = (
    "Say a number, or 'smallest', 'mid' or 'largest' — or name the coin's "
    "label. Any other words set this aside."
)
_BUMP_TARGET_HEAD: Final[str] = (
    "You have {count} transactions in flight — which one should I bump? "
    "Say a number; any other words set this aside."
)
_BUMP_FLOOR_RATE: Final[str] = (
    "That rate can't replace it — the new fee must be at least "
    "{floor_sats} sats (BIP 125). Say 'faster' or name a higher rate."
)
_BUMP_FLOOR_FUNDING: Final[str] = (
    "Even the best confirmed coin can't fund this bump — the replacement "
    "fee must be at least {floor_sats} sats."
)
_BUMP_FLOOR_EXCEEDS: Final[str] = (
    "That rate costs more than the funding can pay — at most "
    "{max_payable_sats} sats can go to the fee, and the replacement needs "
    "at least {floor_sats} sats."
)
_BUMP_ASK_REF_UNRESOLVED: Final[str] = (
    "I couldn't match that coin reference — pick one of the confirmed "
    "coins listed."
)
_BUMP_SUPERSEDE_LINE: Final[str] = (
    "Replaces: {old_txid} — the original may still confirm; only one of "
    "these two ever will"
)

#: 64-hex txid shape (lowercase — store txids are canonical lowercase).
_BUMP_HEX64_RE: Final = re.compile(r"[0-9a-f]{64}")


def _bump_framed_options(
    coins: list[_BumpFundingOption],
) -> list[_BumpFundingOption]:
    """The funding ask's up-to-three numbered framing (TCK-RBF-004,
    smallest/largest/mid per the user rule): 1 coin → just it; 2 → smallest
    + largest; 3+ → smallest, median, largest. Deterministic over the
    canonical ``(value_sats, txid, vout)`` ascending order the caller's
    list already carries; the FULL confirmed set stays addressable by the
    framing words themselves (a mid choice on 4 coins lands the true
    median), only the presentation trims."""
    n = len(coins)
    if n == 1:
        picks = [(0, "coin")]
    elif n == 2:
        picks = [(0, "smallest"), (1, "largest")]
    else:
        picks = [(0, "smallest"), (n // 2, "mid"), (n - 1, "largest")]
    return [replace(coins[i], framing=word) for i, word in picks]


#: Everyday synonyms for the funding ask's framing words (deterministic
#: answer vocab only — the OFFERED card always says the canonical word;
#: a single offered coin is answerable by its number or its label).
_BUMP_FRAMING_SYNONYMS: Final[dict[str, tuple[str, ...]]] = {
    "smallest": ("smallest", "small", "smaller", "lowest", "tiny"),
    "mid": ("mid", "middle"),
    "largest": ("largest", "large", "big", "biggest", "highest"),
}

#: Any deny token from the confirm gate's own vocabulary suppresses a
#: deterministic ask answer ENTIRELY (TCK-HW-005 slice C review-MEDIUM
#: precedent): "don't take the largest" must never fund a replacement.
_BUMP_DENY_TOKENS: Final[frozenset[str]] = ConfirmGate.DENY_TOKENS | frozenset(
    {"not", "nevermind", "never", "mind"}
)


def _bump_funding_answer(line: str, ask: _BumpAsk) -> int | None:
    """Deterministic answer for an OPEN bump ask (TCK-RBF-004, the
    UX-004/interrupt never-trap discipline): 1-based choice index, or
    ``None`` (the caller CLEARS the ask and the utterance falls through to
    the ordinary pipeline — the ask never traps the conversation).

    The answer vocabulary is a number (``2``), a framing word
    (``smallest``/``mid``/``largest``, plus the everyday synonyms), or a
    coin-LABEL word — matched against the user's own stored tags/notes for
    the offered coins, HERE in code, so the label words never reach the
    model (the funding ask's whole deterministic-intercept point). A term
    matching several offered coins is ambiguous → no answer. Any deny
    token suppresses the match."""
    words = [w for w in (t.strip(punctuation) for t in line.lower().split()) if w]
    if not words or any(w in _BUMP_DENY_TOKENS for w in words):
        return None
    if ask.kind == "funding":
        term_to_choice: dict[str, int] = {}
        ambiguous: set[str] = set()
        for i, option in enumerate(ask.options):
            choice = i + 1
            terms = set(_BUMP_FRAMING_SYNONYMS.get(option.framing, ())) | set(
                option.match_terms
            )
            for term in terms:
                if term_to_choice.get(term) not in (None, choice):
                    ambiguous.add(term)
                term_to_choice[term] = choice
        first = words[0]
        if first.isdigit() and 1 <= int(first) <= len(ask.options):
            return int(first)
        hits = {
            term_to_choice[w]
            for w in words
            if w in term_to_choice and w not in ambiguous
        }
        if len(hits) == 1:
            return hits.pop()
        return None
    if ask.kind == "target":
        first = words[0]
        if first.isdigit() and 1 <= int(first) <= len(ask.entries):
            return int(first)
    return None


#: Bare speed-utterance phrases for the post-broadcast reroute (display
#: vocab, TCK-RBF-004; whole-line exact match after punctuation strip —
#: the ``_file_export_choice`` tightened-intercept precedent).
_BUMP_FASTER_PHRASES: Final[frozenset[str]] = frozenset(
    {"faster", "go faster", "make it faster", "make the fee faster"}
)
_BUMP_SLOWER_PHRASES: Final[frozenset[str]] = frozenset(
    {"slower", "go slower", "make it slower", "make the fee slower"}
)


def _bump_speed_choice(line: str) -> str | None:
    """``"faster"``/``"slower"`` for a bare speed utterance, else ``None``
    (TCK-RBF-004 deliverable 7): after a BUMP broadcast these route to a
    fresh bump of the NEW transaction (existing fee-target vocabulary) —
    never to ``create_tx`` (which would silently start a duplicate
    payment; its pending guard cannot see a BROADCAST flow)."""
    words = [w.strip(punctuation) for w in line.lower().split()]
    joined = " ".join(w for w in words if w)
    if joined in _BUMP_FASTER_PHRASES:
        return "faster"
    if joined in _BUMP_SLOWER_PHRASES:
        return "slower"
    return None


def _bump_next_rung(current: str | None, direction: str) -> str | None:
    """One ladder step from ``current`` (TCK-RBF-004): the SAME
    three-target order the re-quote map uses (:data:`_FEE_LADDER_ORDER`) —
    at the extreme the rung holds (the bump then answers honestly: a
    same-rung re-bump fails the BIP-125 floor with the sanctioned floor
    number — the ceiling-ask shape). ``None`` when the staged record
    carries no rung (an explicit-rate bump: no ladder to step — the
    utterance falls through to the ordinary pipeline)."""
    if current is None or current not in _FEE_LADDER_ORDER:
        return None
    pos = _FEE_LADDER_ORDER[current]
    nxt = pos - 1 if direction == "faster" else pos + 1
    nxt = min(max(nxt, 0), len(_FEE_LADDER_ORDER) - 1)
    return {0: "fast", 1: "medium", 2: "slow"}[nxt]


@dataclass(frozen=True, slots=True)
class _BumpOriginal:
    """Dispatcher-owned decomposition of the in-flight transaction a bump
    replaces (TCK-RBF-004): the flow record's scalars PLUS the PSBT-derived
    input sources (the :class:`~localwallet.tx.psbt.PsbtInputSource` coins
    the pure :class:`~localwallet.tx.replacement.OriginalTx` consumes, in
    that record's order). Carried on the session so a bump re-ask while its
    replacement still pends rebuilds from the SAME decomposition — never a
    re-read of user or model text."""

    recipients: tuple[tuple[bytes, int], ...]
    change_script: bytes | None
    change_sats: int | None
    inputs: tuple[PsbtInputSource, ...]
    fee_sats: int
    vsize: int

    def as_original_tx(self) -> OriginalTx:
        """The pure-builder view — the carried decomposition verbatim
        (change script + value are both the ORIGINAL's, both-or-neither,
        re-verified downstream)."""
        return OriginalTx(
            inputs=self.inputs,
            recipients=self.recipients,
            change_script=self.change_script,
            change_sats=self.change_sats,
            fee_sats=self.fee_sats,
            vsize=self.vsize,
        )


@dataclass(frozen=True, slots=True)
class _BumpFundingOption:
    """One funding-ask choice (TCK-RBF-004): the candidate coin plus its
    DISPLAY label text. The label strings are user data — terminal material
    only (the renderer prints them verbatim); they never enter the model
    transcript (handler results are not injected into prompts) and
    ``match_terms`` is what the deterministic intercept matches answers
    against, so the label words never route through the model either."""

    framing: str  # "smallest" | "mid" | "largest" | "coin"
    value_sats: int
    coin: PsbtInputSource
    address: str | None
    label_display: str | None
    match_terms: tuple[str, ...]


@dataclass
class _BumpAsk:
    """An OPEN bump-conversation ask (TCK-RBF-004). ``kind`` is
    ``"target"`` (several in-flight transactions — index the choice) or
    ``"funding"`` (change fell short — index a confirmed-coin choice).
    ``old_txid`` is the verbatim txid the ask belongs to. The REPL's
    deterministic intercept answers the ask by index; ANY other utterance
    clears it (never-trap, the UX-004/interrupt discipline).

    ``fee_target`` / ``fee_rate_sat_vb`` carry the fee knob the ask-opening
    envelope resolved (at most one is set — ``BumpFeeParams`` makes them
    mutually exclusive), so the answer intercept RE-QUOTES the user's
    stated urgency rather than silently dropping it to the FAST default
    (which could stage a bid the user never asked for, or spuriously
    refuse a floor the explicit rate could fund). The handler re-validates
    the whole plan on the answer turn, so re-quoting adds no trust surface."""

    kind: str
    old_txid: str
    options: tuple[_BumpFundingOption, ...] = ()
    entries: tuple[dict[str, object], ...] = ()
    fee_target: str | None = None
    fee_rate_sat_vb: int | None = None


def _bump_ask_fee_kwargs(ask: _BumpAsk) -> dict[str, object]:
    """The ``BumpFeeParams`` fee-knob kwargs an ask answer must re-quote
    (TCK-RBF-004): whichever knob the ask opened with, or ``{}`` for a
    knobless (FAST-default) ask — never an explicit ``None``, which the
    envelope's present-when-not-omitted validators reject."""
    if ask.fee_rate_sat_vb is not None:
        return {"fee_rate_sat_vb": ask.fee_rate_sat_vb}
    if ask.fee_target is not None:
        return {"fee_target": ask.fee_target}
    return {}


@dataclass(frozen=True, slots=True)
class _BumpPending:
    """A staged replacement awaiting the user's lifecycle decisions
    (TCK-RBF-004): the pending ``tx_ref`` it was staged under, the verbatim
    ``old_txid`` it replaces (the broadcast handler's commit-only-on-success
    lineage write keys on this), and the carried decomposition (re-bumps
    while it pends rebuild from it)."""

    tx_ref: str
    old_txid: str
    original: _BumpOriginal


# ------------------------------------------------------------- cpfp state
# (TCK-CPFP-002: the dispatcher-owned conversation state. Structural twin
# of the bump block above — the child is a NEW transaction spending the
# unconfirmed INBOUND coin, so NOTHING here touches the RBF lineage: the
# store's lineage link is RBF-only (schema v3 semantics) and the parent
# transaction is never replaced, only hurried.)


@dataclass(frozen=True, slots=True)
class _CpfpOption:
    """One numbered option of the cpfp menu (TCK-CPFP-002 deliverable 2).
    ``framing`` is ``"smallest"``/``"largest"``/``"coin"`` for a merge
    option (the eligible confirmed coin it would merge — display label and
    intercept match terms ride verbatim from the store, print-only, never
    model-routed) or ``"plain"`` for the no-merge child (the stuck payment
    alone, consolidated into one fresh coin — answerable by number only).
    ``coin`` is ``None`` for the plain option."""

    framing: str
    value_sats: int | None
    coin: PsbtInputSource | None
    label_display: str | None
    match_terms: tuple[str, ...]


@dataclass
class _CpfpAsk:
    """An OPEN cpfp-conversation ask (TCK-CPFP-002). ``kind`` is
    ``"coin"`` (several unconfirmed inbound payments — index the choice)
    or ``"options"`` (the merge menu over eligible confirmed coins).
    ``inbound`` is the resolved stuck coin an ``"options"`` ask belongs to
    (dispatcher-owned, revalidated against a FRESH store read on the
    answer turn — the mid-conversation recheck). ``fee_target`` carries
    the rung the ask-opening envelope resolved so the answer re-quotes the
    user's stated urgency instead of silently defaulting to FAST (the
    RBF-004 fee-knob-persistence lesson). ``choice`` is the ONE field the
    ``_run_turn`` deterministic intercept stamps when it matches an
    answer — the cpfp envelope structurally cannot carry a coin reference
    (no outpoint or number key exists in its params), so the CHOICE is
    session state, code-stamped, never model-settable. Any other
    utterance clears the ask (never-trap)."""

    kind: str
    options: tuple[_CpfpOption, ...] = ()
    entries: tuple[dict[str, object], ...] = ()
    inbound: PsbtInputSource | None = None
    fee_target: str | None = None
    choice: int | None = None


@dataclass(frozen=True, slots=True)
class _CpfpPending:
    """A staged cpfp child awaiting its lifecycle (TCK-CPFP-002): the
    pending ``tx_ref`` it rides under, the stuck payment's ``parent_txid``
    and outpoint it spends (the broadcast-failure classification rechecks
    THIS coin's liveness — deliverable 5), and the plan-card display facts
    the flow record cannot carry (``/details``-style re-shows and the
    cpfp ``tx_pending`` re-print render from this record, verbatim)."""

    tx_ref: str
    parent_txid: str
    inbound_txid: str
    inbound_vout: int
    display: Mapping[str, object]


def _cpfp_answer(line: str, ask: _CpfpAsk) -> int | None:
    """Deterministic answer for an OPEN cpfp ask (TCK-CPFP-002): the
    funding-branch matcher of :func:`_bump_funding_answer` is THE shared
    vocabulary (number / framing word / coin LABEL word, deny tokens
    suppressed, ambiguous terms no-answered) — normalized into a
    throwaway ``_BumpAsk`` so one matcher implementation serves both
    conversations. The cpfp ``"coin"`` ask (index the stuck payment)
    matches by number only, like the bump target ask. ``None`` = no
    answer (the caller CLEARS the ask and the utterance falls through to
    the ordinary pipeline — never-trap)."""
    return _bump_funding_answer(
        line,
        _BumpAsk(
            kind="funding" if ask.kind == "options" else "target",
            old_txid="",
            options=ask.options,  # type: ignore[arg-type] — duck-typed: _CpfpOption carries .framing/.match_terms
            entries=ask.entries,
        ),
    )


# ------------------------------------------------- consolidation state
# (TCK-CONS-001: the dispatcher-owned consolidation conversation. The
# structural twin of the cpfp block: the plan rides the EXISTING
# ``self_transfer`` consolidate mode — no new intent, no envelope key ever
# carries a coin reference; the picked coins live ONLY on this session
# record, code-stamped, and the handler revalidates them against a fresh
# store read at staging time.)


@dataclass(frozen=True, slots=True)
class _ConsRow:
    """One label group of the roll-up ask: the closed-set ``tag`` (``""`` =
    the unlabeled group), its display word + intercept match terms, and the
    group's coin dicts (``value_sats``/``txid``/``vout``/``address``/
    ``confirmed``/``label`` — all store-truth fields; label text is
    print-only user data, the §7.10 discipline)."""

    tag: str
    display: str
    match_terms: tuple[str, ...]
    value_sats: int
    coins: tuple[dict[str, object], ...]


@dataclass
class _ConsAsk:
    """An OPEN consolidation-conversation ask. ``kind`` is ``"rollup"``
    (pick a label group), ``"count"`` ("1 coin or N?" over the group), or
    ``"list"`` (the ascending coin list, answered by CHAT-001 registry
    NUMBER). ``fee_target`` carries the rung the opener's words resolved so
    every later intercept re-quotes it instead of silently defaulting (the
    RBF-004 MAJOR lesson, applied to this conversation's multi-step asks:
    the knob AND the label group persist across every intercept). ``choice``
    /``picked`` are the ONE fields the deterministic intercept stamps when
    the final answer dispatches — consolidate params carry a
    threshold-only number the ENGINE computed, never a coin reference."""

    kind: str
    rows: tuple[_ConsRow, ...] = ()
    entries: tuple[dict[str, object], ...] = ()
    label_display: str | None = None
    fee_target: str | None = None
    choice: int | None = None
    picked: tuple[dict[str, object], ...] = ()
    wallet_id: int | None = None


@dataclass(frozen=True, slots=True)
class _ConsPending:
    """A staged consolidation plan awaiting its lifecycle (TCK-CONS-001):
    the pending ``tx_ref`` it rides under. On a SUCCESSFUL broadcast the
    handler annotates the plan's own outputs (the §1.3 union inheritance
    already runs for every self-transfer; this record adds the
    ``consolidation`` tag + the "consolidated from N outputs" note on top —
    N counted from the broadcast's own inputs, never from user text)."""

    tx_ref: str


def _cons_answer(line: str, ask: _ConsAsk) -> int | None:
    """Deterministic answer for an OPEN consolidation ask (the never-trap
    discipline shared with :func:`_bump_funding_answer`: deny tokens
    suppress, an ambiguous or absent match returns ``None`` and the caller
    CLEARS the ask). Kind-shaped result:
    ``"rollup"`` → the 1-based ROW (number or unique tag word);
    ``"count"`` → ``1`` (pick a single coin) or ``2`` (merge them all —
    "all"/the literal count digit);
    ``"list"`` → the chosen CHAT-001 registry NUMBER itself (never a row
    position — the council invariant), answering only when every digit in
    the line names one known number."""
    words = [w for w in (t.strip(punctuation) for t in line.lower().split()) if w]
    if not words or any(w in _BUMP_DENY_TOKENS for w in words):
        return None
    if ask.kind == "rollup":
        first = words[0]
        if first.isdigit():
            return int(first) if 1 <= int(first) <= len(ask.rows) else None
        term_to_row: dict[str, int] = {}
        ambiguous: set[str] = set()
        for i, row in enumerate(ask.rows):
            for term in row.match_terms:
                if term_to_row.get(term) not in (None, i + 1):
                    ambiguous.add(term)
                term_to_row[term] = i + 1
        hits = {
            term_to_row[w] for w in words if w in term_to_row and w not in ambiguous
        }
        return hits.pop() if len(hits) == 1 else None
    if ask.kind == "count":
        ws = set(words)
        one = bool(ws & _CONS_ONE_TERMS)
        allt = bool(ws & _CONS_ALL_TERMS) or (
            words[0].isdigit() and int(words[0]) == len(ask.entries)
        )
        if one and not allt:
            return 1
        if allt and not one:
            return 2
        return None
    # kind == "list": the registry number is the referent (coins at one
    # address share its number and are all picked with it).
    digits = {int(w) for w in words if w.isdigit()}
    known = {int(str(e["number"])) for e in ask.entries}
    if len(digits) == 1 and digits <= known:
        return digits.pop()
    return None


def _print_cons_ask(ask: _ConsAsk, output_fn: Callable[[str], None]) -> None:
    """Render an open consolidation ask (code-rendered, deterministic;
    every figure verbatim from the store rows the ask carries — label text
    is print-only, handler-free, never model-bound)."""
    if ask.kind == "rollup":
        output_fn(sanitize_tool_output(_CONS_ROLLUP_HEAD))
        for i, row in enumerate(ask.rows):
            output_fn(
                sanitize_tool_output(
                    _CONS_ROLLUP_ROW.format(
                        index=i + 1,
                        tag=row.display,
                        coins=len(row.coins),
                        s="s" if len(row.coins) != 1 else "",
                        sats=row.value_sats,
                    )
                )
            )
        return
    if ask.kind == "count":
        coins = len(ask.entries)
        total = sum(int(str(e["value_sats"])) for e in ask.entries)
        output_fn(
            sanitize_tool_output(
                _CONS_COUNT_HEAD.format(
                    coins=coins,
                    s="s" if coins != 1 else "",
                    tag=f"'{ask.label_display or _CONS_ROLLUP_UNLABELED}'",
                    sats=total,
                )
                + _CONS_COUNT_ASK.format(coins=coins)
            )
        )
        return
    output_fn(sanitize_tool_output(_CONS_LIST_HEAD))
    for entry in ask.entries:
        confirmed = entry.get("confirmed")
        label = entry.get("label")
        output_fn(
            sanitize_tool_output(
                _CONS_LIST_ROW.format(
                    number=entry.get("number"),
                    sats=int(str(entry["value_sats"])),
                    label=(
                        # the stored display text already quotes its bits
                        # (the CPFP `_display` shape) — printed VERBATIM.
                        label
                        if isinstance(label, str) and label
                        else _CONS_LIST_NO_LABEL
                    ),
                    state="confirmed" if confirmed == 1 else "unconfirmed",
                )
            )
        )


def _consolidation_coin_rows(
    store: Store, wallet_id: int
) -> tuple[dict[str, object], ...]:
    """The wallet's coins as consolidation-list dicts, canonical ASCENDING
    order ``(value_sats, txid, vout)`` — the one deterministic order every
    chooser in this app uses. Label display text joins the stored tags +
    note verbatim (print-only); ``tags`` rides the raw closed-set ids for
    the roll-up grouping (code data, never a narration leak — nothing here
    reaches a prompt: the ask renderer prints, the model never sees)."""
    utxos = store.get_utxos_for_wallet(wallet_id)
    labels = {
        (row.txid.lower(), row.vout): row for row in store.get_coin_labels(wallet_id)
    }
    coins: list[dict[str, object]] = []
    for row in sorted(utxos, key=lambda u: (u.value_sats, u.txid.lower(), u.vout)):
        label = labels.get((row.txid.lower(), row.vout))
        tags = tuple(label.tags) if label is not None else ()
        bits = [*tags, *([label.note] if label is not None and label.note else [])]
        coins.append(
            {
                "txid": row.txid,
                "vout": row.vout,
                "value_sats": row.value_sats,
                "address": row.address,
                "confirmed": row.confirmed,
                "tags": tags,
                "label": ", ".join(f"'{bit}'" for bit in bits) if bits else None,
            }
        )
    return tuple(coins)


def _cons_rollup_rows(
    coins: tuple[dict[str, object], ...]
) -> tuple[_ConsRow, ...]:
    """The per-tag count + sats roll-up (deliverable 1, code-computed):
    COIN_TAGS canonical order, a coin counts in EVERY tag group it carries
    (union-tagged coins are honestly in both groups — display-only), and
    the unlabeled group last (no row / no tags). Empty groups are omitted."""
    rows: list[_ConsRow] = []
    for tag in COIN_TAGS:
        group = tuple(c for c in coins if tag in (c.get("tags") or ()))
        if group:
            rows.append(
                _ConsRow(
                    tag=tag,
                    display=tag,
                    match_terms=_CONS_TAG_TERMS[tag],
                    value_sats=sum(int(str(c["value_sats"])) for c in group),
                    coins=group,
                )
            )
    unlabeled = tuple(c for c in coins if not (c.get("tags") or ()))
    if unlabeled:
        rows.append(
            _ConsRow(
                tag="",
                display=_CONS_ROLLUP_UNLABELED,
                match_terms=_CONS_UNLABELED_TERMS,
                value_sats=sum(int(str(c["value_sats"])) for c in unlabeled),
                coins=unlabeled,
            )
        )
    return tuple(rows)


def _run_consolidation_turn(
    session: SendSession,
    store: Store,
    flow: TxFlow,
    line: str,
    output_fn: Callable[[str], None],
    *,
    table: DispatchTable,
) -> bool:
    """One consolidation-conversation turn (TCK-CONS-001), run BEFORE the
    gate and the model exactly like the hardware/bump/cpfp intercepts:
    ``True`` = consumed (the turn ends here; the line is NEVER added to the
    model transcript — tag/label words are intercepted before the model
    sees them, the RBF-004 lesson applied to the conversation's opening).

    State machine (dispatcher-owned, mirrors ``_CpfpAsk``):
    ``rollup`` (pick a label) → ``count`` ("1 coin or N?") → optional
    ``list`` (ascending UTXO list, CHAT-001 registry number pick) →
    dispatch. The final answer stamps ``choice``/``picked`` on the ask and
    CODE builds the ``self_transfer`` consolidate envelope (params carry
    ONLY the engine-computed threshold + the persisted fee rung — never a
    coin reference); the handler consumes the ask and revalidates every
    picked coin against a FRESH store read (the mid-conversation recheck).
    A group of exactly one coin answers the count ask by construction and
    goes straight to the plan (no fake question — the CPFP-002 collapse
    discipline). Any unmatched utterance CLEARS the ask (never-trap) and
    the line falls through to the ordinary pipeline."""
    ask = session.cons_ask
    if ask is not None:
        choice = _cons_answer(line, ask)
        if choice is None:
            session.cons_ask = None  # never-trap
            return False
        if ask.kind == "rollup":
            row = ask.rows[choice - 1]
            if row.tag == "":
                return _cons_open_list(
                    session, store, ask.wallet_id, row.coins, ask.fee_target, output_fn
                )
            if len(row.coins) == 1:
                return _cons_finalize(
                    session, row.coins, ask.fee_target, line, output_fn, table=table
                )
            session.cons_ask = _ConsAsk(
                kind="count",
                entries=row.coins,
                label_display=row.display,
                fee_target=ask.fee_target,
                wallet_id=ask.wallet_id,
            )
            _print_cons_ask(session.cons_ask, output_fn)
            return True
        if ask.kind == "count":
            if choice == 1:
                return _cons_open_list(
                    session, store, ask.wallet_id, ask.entries, ask.fee_target, output_fn
                )
            return _cons_finalize(
                session, ask.entries, ask.fee_target, line, output_fn, table=table
            )
        picked = tuple(
            e for e in ask.entries if int(str(e["number"])) == choice
        )
        # The CHAT-001 invariant: every resolution RESTATES the FULL
        # address(es) before anything is planned (never number-only).
        for entry in picked:
            output_fn(
                sanitize_tool_output(
                    _CONS_RESOLVED.format(
                        number=choice,
                        address=entry.get("address"),
                        sats=int(str(entry["value_sats"])),
                    )
                )
            )
        return _cons_finalize(session, picked, ask.fee_target, line, output_fn, table=table)

    # --- the opener (BEFORE the model; conservative phrase match) ---
    if flow.state in (
        TxFlowStatus.CREATED,
        TxFlowStatus.CONFIRMED,
        TxFlowStatus.SIGNED,
    ):
        return False  # gate territory (the pending card owns this turn)
    intent = _consolidation_intent(line)
    if intent is None:
        return False
    tag, fee_target = intent
    try:
        wallet = store.get_active_wallet()
        if wallet is None:
            return False
        if store.get_sync_state(wallet.id, wallet_scan.CURSOR_KEY) is None:
            return False  # never scanned: the model route's lazy scan answers honestly
        coins = _consolidation_coin_rows(store, wallet.id)
    except (StoreError, sqlite3.Error):
        return False  # sugar never crashes a turn; the ordinary pipeline continues
    if not coins:
        output_fn(sanitize_tool_output(_CONS_EMPTY))
        return True
    if tag == "":
        unlabeled = tuple(c for c in coins if not (c.get("tags") or ()))
        if not unlabeled:
            output_fn(sanitize_tool_output(_CONS_EMPTY))
            return True
        return _cons_open_list(
            session, store, wallet.id, unlabeled, fee_target, output_fn
        )
    if tag is not None:
        group = tuple(c for c in coins if tag in (c.get("tags") or ()))
        if not group:
            output_fn(sanitize_tool_output(_CONS_EMPTY))
            return True
        if len(group) == 1:
            return _cons_finalize(session, group, fee_target, line, output_fn, table=table)
        session.cons_ask = _ConsAsk(
            kind="count",
            entries=group,
            label_display=tag,
            fee_target=fee_target,
            wallet_id=wallet.id,
        )
        _print_cons_ask(session.cons_ask, output_fn)
        return True
    rows = _cons_rollup_rows(coins)
    session.cons_ask = _ConsAsk(
        kind="rollup", rows=rows, fee_target=fee_target, wallet_id=wallet.id
    )
    _print_cons_ask(session.cons_ask, output_fn)
    return True


def _cons_open_list(
    session: SendSession,
    store: Store,
    wallet_id: int | None,
    coins: tuple[dict[str, object], ...],
    fee_target: str | None,
    output_fn: Callable[[str], None],
) -> bool:
    """Install the ascending-list ask, numbering every shown address
    through the sanctioned registry writer (TCK-CHAT-001: showing IS the
    registration act — ``note_address_shown`` is idempotent MAX+1, so a
    first-ever showing gets its stable wallet-lifetime number and every
    later showing keeps it; no row is ever unnumbered, no dead end). A
    registry write failure clears the ask and releases the line to the
    ordinary pipeline (never-trap; the model never sees label data here —
    a list ANSWER is a bare number, an opener line is closed-set words)."""
    if wallet_id is None:
        session.cons_ask = None
        return False
    try:
        entries: list[dict[str, object]] = []
        for coin in coins:
            address = coin.get("address")
            if not isinstance(address, str) or not address:
                # A wallet coin without an address row is a store-shape
                # surprise, not a list candidate: stand the conversation
                # aside (fail closed, never a half-numbered ask).
                session.cons_ask = None
                return False
            record = store.note_address_shown(wallet_id, address)
            entries.append({**coin, "number": record.number})
    except (StoreError, sqlite3.Error):
        session.cons_ask = None
        return False
    session.cons_ask = _ConsAsk(
        kind="list", entries=tuple(entries), fee_target=fee_target, wallet_id=wallet_id
    )
    _print_cons_ask(session.cons_ask, output_fn)
    return True


def _cons_finalize(
    session: SendSession,
    coins: tuple[dict[str, object], ...],
    fee_target: str | None,
    line: str,
    output_fn: Callable[[str], None],
    *,
    table: DispatchTable,
) -> bool:
    """The answer that plans: stamp the dispatcher-owned coins on the ask
    (the envelope itself carries NO coin reference — only the engine's own
    threshold ``max(values)+1`` and the persisted rung) and dispatch
    straight to the ``self_transfer`` handler, whose fresh-store revalidation
    and flow guards remain the authority. Transcript-free like every
    consolidation turn."""
    values = [int(str(c["value_sats"])) for c in coins]
    params_kwargs: dict[str, object] = {
        "mode": "consolidate",
        "below_size_sats": max(values) + 1,
    }
    if fee_target is not None:
        params_kwargs["fee_target"] = fee_target
    session.cons_ask = _ConsAsk(
        kind="final", entries=coins, picked=coins, choice=1, fee_target=fee_target
    )
    try:
        _dispatch_code_self_turn(
            session, line, SelfTransferParams(**params_kwargs), output_fn, table=table
        )
    finally:
        # The handler consumes the answered ask; this covers every path
        # that returned before consumption (the scan gate) — a stale
        # answered ask must never re-dispatch on a later utterance.
        session.cons_ask = None
    return True


@dataclass
class SendSession:
    """Per-turn send-flow context shared by the REPL and the handlers.

    The REPL classifies the user's utterance against the deterministic
    confirm gate (:class:`~localwallet.tx.flow.ConfirmGate`) at the top of
    every turn — BEFORE the model runs — and stores the decision here; the
    ``confirm_tx`` handler passes it into
    :meth:`~localwallet.tx.flow.TxFlow.confirm`. That ordering is the
    dual-key wiring (ADR-0013): the gate decision always describes the
    SAME turn as the ``confirm_tx`` envelope, and an LLM "yes" can never
    substitute for it.

    ``card_render`` is the TCK-UX-002 ``/details`` cache: the full
    nine-line card render (the classic, value-verbatim format) belonging
    to the LAST rendered pending card. The on-screen default is the brief
    merged view; ``/details`` reprints this cached full render verbatim
    while a transaction pends (ADR-0020 transcript-command channel). The
    lines are the card's own display material (address/amounts — printed
    to the terminal anyway, never logged); the model cannot see them.

    ``last_broadcast_txid`` / ``label_hint_txid`` (TCK-UTXO-001, design doc
    §1.2/§1.3) are the ``/label`` session state: the most recent tx THIS
    wallet broadcast (what ``last`` resolves to) and the tx the one-line
    post-broadcast capture hint was already shown for (never repeated for
    the same tx within the session). Terminal-channel display data —
    labels themselves never enter model context.

    ``hw_sign_wanted`` / ``file_sign_export_once`` (TCK-HW-005 slice C)
    are the CODE-STAMPED sign-routing choices the user's own words set —
    the deterministic utterance intercepts write them, the ``sign_tx``
    handler reads and consumes them, and the MODEL can never set or clear
    either (gate_decision precedent, ADR-0013). ``hw_sign_wanted`` latches
    "sign this flow on my device" until a device sign succeeds, an explicit
    file export runs, or the flow is cancelled — so a bare "retry" re-probes
    the device instead of silently exporting. ``file_sign_export_once`` is
    the one-shot explicit file fallback ("file"/"export" after the device
    ask).

    ``bump_ask`` / ``bump_pending`` / ``bump_bcast_txid`` (TCK-RBF-004) are
    the bump conversation's dispatcher-owned state: the OPEN
    target/funding ask the deterministic intercept answers (any other
    utterance clears it — never-trap), the staged replacement's lineage
    record (the broadcast handler writes ``record_replacement`` ONLY on a
    successful broadcast, keyed by the pending ``tx_ref``), and the last
    successfully broadcast replacement's txid (what arms the post-broadcast
    "faster"/"slower" rerouting). Code-owned end to end: the model can
    neither set, read, nor clear any of them.

    ``cpfp_ask`` / ``cpfp_pending`` (TCK-CPFP-002) are the SAME machinery
    for the child-pays-for-parent conversation: the OPEN coin/options ask
    (with the fee rung and — stamped by the intercept — the chosen option,
    since a cpfp envelope cannot carry a coin reference), and the staged
    child's record (its spent outpoint drives the broadcast-failure
    parent-liveness classification; its display facts re-show the cpfp
    card while the child pends). NO lineage: the hurried parent is a
    different, untouched transaction — the store's lineage link stays
    RBF-only. Code-owned end to end like the bump state.

    ``cons_ask`` / ``cons_pending`` (TCK-CONS-001) are the consolidation
    conversation's twin: the OPEN rollup/count/list ask (the label group,
    the fee rung and — stamped by the intercept — the picked coins all
    ride the dispatcher-owned record; consolidate params carry only the
    engine-computed threshold), and the staged plan's ``tx_ref`` marker
    (a successful broadcast adds the ``consolidation`` tag + the
    "consolidated from N outputs" note to the plan's own outputs ON TOP of
    the existing §1.3 union inheritance, then retires). Code-owned end to
    end: the model can neither set, read, nor clear either.

    ``fiat_ask_currency`` (TCK-FIAT-003, the MW-17 EUR bug) is the ONE-SHOT
    per-ask currency the deterministic word-table intercept
    (:func:`_detect_fiat_ask_currency`) stamps from the user's OWN words
    before the turn's handlers run, and :func:`_run_turn` clears in a
    ``finally`` when they are done: an explicit currency word ("in Euros?")
    answers THAT reply in that currency while the ``display_currency``
    SETTING stays untouched (the file_export one-shot precedent,
    HW-005 slice C). The model can neither set, read, nor clear it — the
    envelope carries no currency.
    """

    gate_decision: GateDecision = GateDecision.NOT_A_DECISION
    card_render: list[str] | None = None
    last_broadcast_txid: str | None = None
    label_hint_txid: str | None = None
    hw_sign_wanted: bool = False
    file_sign_export_once: bool = False
    bump_ask: _BumpAsk | None = None
    bump_pending: _BumpPending | None = None
    bump_bcast_txid: str | None = None
    cpfp_ask: _CpfpAsk | None = None
    cpfp_pending: _CpfpPending | None = None
    cons_ask: _ConsAsk | None = None
    cons_pending: _ConsPending | None = None
    fiat_ask_currency: str | None = None


@dataclass(frozen=True, slots=True)
class SignerSelection:
    """Resolved signing-backend configuration (TCK-P3-005).

    Built once at startup from ``--signer`` / :data:`SIGNER_ENV_VAR` /
    :data:`SIGNER_DIR_ENV_VAR`:

    - ``kind`` — ``"file"`` (airgap transfer folder, ADR-0014) or
      ``"hwi"`` (USB hardware wallet, ADR-0015);
    - ``dir_path`` — the file signer's transfer folder (created on demand
      at the first export);
    - ``fingerprint_hex`` — the wallet's expected ACCOUNT-key fingerprint
      from the parsed wallet key (the descriptor origin fingerprint; the
      device's master fingerprint is unknowable to a watch-only
      account-level wallet — ADR-0015 amendment #2); the HWI signer's
      post-open account-key bind is constructed from it together with the
      descriptor's account path — lazily, ONLY when the hwi kind is
      selected, and per sign attempt (the signer objects are stateless).
    """

    kind: str
    dir_path: Path
    fingerprint_hex: str


def stub_generate(prompt: str, grammar_text: str | None) -> str:
    """Deterministic stub model for ``--stub-llm`` — dev/test mode ONLY.

    This is **not** the acceptance path: the AC path is the real local
    GGUF runtime (:data:`~localwallet.agent.runtime.MODEL_PATH_ENV_VAR`).
    The stub exists so the full wiring (agent loop → validation →
    allowlist dispatch → store-backed handlers) can be exercised
    deterministically without a model file.

    Behavior: matches the current user turn (the last ``user: `` segment
    of the assembled prompt, see ``AgentLoop._build_prompt``) against a
    fixed phrase table — "balance" → ``get_balance``; "history" or
    "transaction" → ``get_history`` (except a no-txid confirm-ask like
    "when will my transaction confirm?" → ``get_utxos``, TCK-PENDING-001);
    "utxo", "pending" or "incoming" → ``get_utxos`` (TCK-PENDING-001: the
    answer's pending block narrates from the store either way); "new
    address" / "address" → ``new_address``; "node" or "privacy" →
    ``node_status``; "status" → ``tx_status`` (the
    first 64-hex token in the utterance is extracted verbatim, with the
    canned placeholder as fallback); "sign" → ``sign_tx``; "broadcast" →
    ``broadcast_tx``; a send request ("send … to bc1…") → ``create_tx``
    (the ``bc1…`` token and the ``<n> sats`` / ``$<n>`` figure are
    extracted verbatim from the user turn, with the canned fixture
    recipient / a canned 10000-sat amount as fallbacks); a confirmation
    utterance ("confirm", "yes", …) → ``confirm_tx``; anything else → a
    canned ``respond``. ``grammar_text`` is accepted for
    :data:`~localwallet.agent.runtime.GenerateFn` compatibility and
    ignored.

    Dev-mode caveats (by design, documented): the canned
    ``confirm_tx``/``sign_tx``/``broadcast_tx`` carry placeholder
    ``tx_ref`` values that cannot match a real flow reference (the stub
    cannot see the flow's id factory) — the flow refuses them, which
    demonstrates the refusal paths in dev mode. Deterministic tests do
    NOT rely on the stub for the happy path; they inject generate
    closures that quote the flow's real references. Everything the stub
    extracts from user text is untrusted input like any model output: it
    flows through the full 3-layer validation before any handler runs.

    Args:
        prompt: The fully assembled agent prompt.
        grammar_text: The envelope GBNF grammar (ignored by the stub).

    Returns:
        A canned envelope JSON document (untrusted-input contract still
        applies: it flows through ``handle_raw`` like any model output).
    """
    del grammar_text
    user_turn = prompt.rsplit("user: ", 1)[-1].lower()
    # The assembled prompt terminates with "\n\nenvelope:"; the utterance
    # is the first line of the last user segment (REPL input is one line).
    utterance = user_turn.split("\n")[0].strip()
    if "balance" in user_turn:
        return _STUB_BALANCE_ENVELOPE
    # TCK-PENDING-001 fix (scoped): only a no-txid CONFIRM-ASK ("when
    # will my transaction confirm?") routes to get_utxos per the
    # docstring / prompt map, NOT get_history (which "transaction" would
    # otherwise match first). The confirm-ask shape asks about
    # confirmation timing: it contains "confirm"/"confirmation" AND a
    # transaction/tx/it token AND has no 64-hex txid. Plain "transaction"
    # phrasings (history/recent/show) keep routing to get_history below;
    # a phrase WITH an explicit 64-hex txid/hash falls through as before.
    _confirm_ask = (
        "confirm" in user_turn
        and re.search(r"\b(transaction|tx|it)\b", user_turn)
        and not re.search(r"\b[0-9a-f]{64}\b", user_turn)
    )
    if _confirm_ask:
        return _STUB_UTXOS_ENVELOPE
    if "history" in user_turn or "transaction" in user_turn:
        return _STUB_HISTORY_ENVELOPE
    if "utxo" in user_turn or "pending" in user_turn or "incoming" in user_turn:
        return _STUB_UTXOS_ENVELOPE
    if "address" in user_turn:
        return _STUB_NEW_ADDRESS_ENVELOPE
    if "node" in user_turn or "privacy" in user_turn:
        return _STUB_NODE_STATUS_ENVELOPE
    if "status" in user_turn:
        hex_match = re.search(r"\b[0-9a-f]{64}\b", utterance)
        if hex_match is None:
            return _STUB_TX_STATUS_ENVELOPE
        return json.dumps(
            {"v": 0, "intent": "tx_status", "params": {"txid": hex_match.group(0)}}
        )
    if "sign" in user_turn:
        return _STUB_SIGN_TX_ENVELOPE
    if "broadcast" in user_turn:
        return _STUB_BROADCAST_TX_ENVELOPE
    if "send" in user_turn and "bc1" in user_turn:
        return _stub_create_tx_envelope(utterance)
    if (
        "confirm" in utterance
        or "approve" in utterance
        or utterance in ("yes", "y", "yes please", "send it", "do it")
    ):
        return _STUB_CONFIRM_TX_ENVELOPE
    return _STUB_RESPOND_ENVELOPE


def _stub_create_tx_envelope(user_turn: str) -> str:
    """Build the stub's canned ``create_tx`` envelope from the user turn.

    Deterministic extraction, dev mode only: the recipient is the first
    whitespace token starting with ``bc1`` (edge punctuation stripped),
    falling back to the canned fixture address; the amount is the first
    ``<n> sats`` figure, else the first ``$<n>`` figure, else the canned
    sats fallback. The output is an ordinary model-output document — it
    must pass the same validation as the real model's envelope.
    """
    recipient = _STUB_RECIPIENT
    for token in user_turn.split():
        candidate = token.strip(punctuation)
        if candidate.startswith("bc1") and len(candidate) > 3:
            recipient = candidate
            break

    params: dict[str, object]
    sats_match = re.search(r"send\s+(\d+)\s*sats", user_turn)
    usd_match = re.search(r"\$\s*(\d+(?:\.\d+)?)", user_turn)
    if sats_match is not None:
        params = {"recipient": recipient, "amount_sats": int(sats_match.group(1))}
    elif usd_match is not None:
        params = {"recipient": recipient, "amount_usd": float(usd_match.group(1))}
    else:
        params = {
            "recipient": recipient,
            "amount_sats": _STUB_FALLBACK_AMOUNT_SATS,
        }
    return json.dumps({"v": 0, "intent": "create_tx", "params": params})


def build_dispatch_table(
    store: Store,
    wallet: WalletRecord,
    parsed: ParsedKey,
    client: ChainClient | None,
    scan_fn: Callable[[], object],
    *,
    flow: TxFlow | None = None,
    session: SendSession | None = None,
    fee_estimator: FeeEstimator | None = None,
    price_oracle: PriceOracle | None = None,
    signer_selection: SignerSelection | None = None,
    signer: Signer | FilePsbtSigner | None = None,
    settings: Settings | None = None,
    node_detect_fn: Callable[[], LocalNodeReport] | None = None,
    seconds_since_last_block_fn: Callable[[], int | None] | None = None,
    scan_gate: StartupScan | None = None,
    kick_scan_fn: Callable[[], bool] | None = None,
    defer_scans: bool = False,
    output: _Output | None = None,
) -> DispatchTable:
    """Build the allowlist dispatch table for the running app.

    Args:
        store: The persistence layer every handler reads.
        wallet: The active wallet row (ADR-0010: exactly one profile).
        parsed: The wallet's parsed account key (for ``new_address``
            derivation, the send flow's PSBT account fields, and the HWI
            signer's expected fingerprint; public key only).
        client: The chain client — used only by ``scan_fn`` (the lazy
            first scan inside ``get_balance``/``create_tx``), by the
            fee/price wrappers below, and by the ``broadcast_tx`` /
            ``tx_status`` handlers; the read handlers never touch it.
        scan_fn: Zero-argument callable performing one wallet scan
            (the app's plan→worker-fetch→persist composition of
            :mod:`localwallet.wallet.scan` in production). Used lazily
            when the store has no sync cursor.
        scan_gate: The engine's startup-scan state (TCK-SCAN-003,
            ADR-0022 decisions 5/6): while its first scan is in progress
            the read handlers answer cache-served and ``stale``-flagged,
            the lazy in-handler scan stands down (the worker owns the
            chain), and ``create_tx`` refuses with the friendly
            :data:`WALLET_LOADING_REFUSAL` line. ``None`` (tests, the
            AUTO_SCAN=0 wiring) = no scan in flight — the pre-split
            behavior.
        kick_scan_fn: The engine-thread seam that STARTS the background
            :class:`ScanFlow` (TCK-UX-011, ADR-0022 amendment 2; wired to
            :meth:`ScanFlow.kick_scan`): called by a ``defer_scans``
            ``get_balance`` that found no sync cursor and no scan in
            flight, so the stale answer actually kicks off the load it
            promises. Idempotent by construction (a no-op while any scan
            is in flight) — the double-``/balance`` never double-kicks.
            Its BOOL result drives the answer's ``scan_pending`` key
            (TCK-UX-012(d): kick reality, not kick intent).
        defer_scans: Keyed on TRANSPORT, not gate-None (TCK-UX-011):
            ``True`` in the web/engine world, where a lazy first scan is
            NEVER run inline (a minutes-class chain walk must not block
            the turn) — the read answers from cache, stale-flagged, with
            the additive ``scan_pending: true`` key, and kicks the flow.
            ``False`` (default) keeps the pre-split inline scan for the
            CLI world (incl. the AUTO_SCAN=0 dev opt-out) — the
            documented CLI exception in ADR-0022 amendment 2.
        flow: The dispatcher-owned send-flow state machine (TCK-P2-004).
            Defaults to a fresh :class:`TxFlow` with the real clock and
            uuid id factory; the REPL and tests share ONE instance.
        session: The per-turn gate-decision carrier (dual-key rule,
            ADR-0013). Defaults to a fresh :class:`SendSession`.
        fee_estimator: Fee-rate source for ``create_tx``; defaults to a
            :class:`FeeEstimator` over ``client``.
        price_oracle: per-BTC rate source in the display currency for
            ``create_tx`` and ``get_balance`` (best-effort fiat display,
            TCK-FIAT-001 / TCK-FIAT-002); defaults to a
            :class:`PriceOracle` over ``client``.
        signer_selection: Signing-backend configuration for
            ``sign_tx`` (TCK-P3-005). Defaults to the file signer over
            :data:`SIGNER_DIR_ENV_VAR` / :data:`DEFAULT_SIGNER_DIR` with
            the ``parsed`` key's fingerprint — the same policy
            :func:`run` applies explicitly.
        signer: Signer-object override (test seam): used as-is by the
            ``sign_tx`` handler for the hwi kind, and for the file kind
            when it IS a :class:`FilePsbtSigner`. Production passes
            ``None`` and lets the handler construct from
            ``signer_selection`` per attempt.
        settings: Runtime settings, consumed by the ``node_status``
            handler's detection and backend-mode selection. Defaults to
            :meth:`Settings.from_env`. The handler passes these SAME
            settings to :func:`localwallet.node.detect_local_nodes`, so
            detection honors ``LOCALWALLET_NODE_DETECTION_ENABLED`` and the
            banner/narration backend mode derives from the same source the
            chain client uses (ADR-0018).
        node_detect_fn: Zero-argument callable running the local-node
            detection pass (test seam). Defaults to
            :func:`detect_local_nodes` over ``settings`` — advise-only,
            honors ``node_detection_enabled``, bounded latency (P4-001).
        output: The mode-aware :class:`_Output` router (TCK-DIAG-003): the
            ``broadcast_tx`` handler emits its value-free send-failure debug
            line through ``output.warning`` (console + launch log, NEVER the
            transcript/SSE narration). ``None`` (test seam) emits nothing.

    Returns:
        A :class:`~localwallet.protocol.DispatchTable` covering the whole
        closed intent enum.
    """
    wallet_id = wallet.id
    # One shared flow/session pair: the create and confirm handlers must
    # see the SAME dispatcher-owned state machine (never two instances).
    tx_flow = flow if flow is not None else TxFlow()
    send_session = session if session is not None else SendSession()
    app_settings = settings if settings is not None else Settings.from_env()
    if signer_selection is None:
        env_kind = os.environ.get(SIGNER_ENV_VAR, "").strip().lower()
        signer_selection = SignerSelection(
            kind=env_kind if env_kind in _SIGNER_KINDS else SIGNER_KIND_FILE,
            dir_path=Path(
                os.environ.get(SIGNER_DIR_ENV_VAR, "").strip() or DEFAULT_SIGNER_DIR
            ),
            fingerprint_hex=parsed.hd_key.my_fingerprint.hex(),
        )
    # ONE oracle shared by create_tx (USD amount resolution) and
    # get_balance (best-effort fiat display, TCK-FIAT-001): the caches —
    # fresh → stale → sats-only per the ADR-0011 ladder — ride together.
    app_price_oracle = price_oracle if price_oracle is not None else PriceOracle(client)
    return {
        IntentName.RESPOND: _respond_handler,
        IntentName.CLARIFY: _clarify_handler,
        IntentName.GET_BALANCE: _make_get_balance_handler(
            store, wallet_id, scan_fn, scan_gate, price_oracle=app_price_oracle,
            kick_scan_fn=kick_scan_fn, defer_scans=defer_scans,
        ),
        IntentName.GET_HISTORY: _make_get_history_handler(store, wallet_id, scan_gate),
        IntentName.GET_UTXOS: _make_get_utxos_handler(store, wallet_id, scan_gate),
        IntentName.GET_ADDRESSES: _make_get_addresses_handler(
            store, wallet_id, scan_gate
        ),
        IntentName.NEW_ADDRESS: _make_new_address_handler(store, wallet_id, parsed),
        IntentName.CREATE_TX: _make_create_tx_handler(
            store,
            wallet_id,
            parsed,
            tx_flow,
            fee_estimator if fee_estimator is not None else FeeEstimator(client),
            app_price_oracle,
            scan_fn,
            seconds_since_last_block_fn=seconds_since_last_block_fn,
            scan_gate=scan_gate,
            settings=app_settings,
            session=send_session,
        ),
        IntentName.CONFIRM_TX: _make_confirm_tx_handler(tx_flow, send_session),
        IntentName.SIGN_TX: _make_sign_tx_handler(
            tx_flow, signer_selection, signer, store, wallet_id, parsed,
            session=send_session,
        ),
        IntentName.BROADCAST_TX: _make_broadcast_tx_handler(
            tx_flow, client, store, wallet_id, session=send_session, output=output
        ),
        IntentName.TX_STATUS: _make_tx_status_handler(
            client, tx_flow, scan_gate, store=store, wallet_id=wallet_id
        ),
        IntentName.NODE_STATUS: _make_node_status_handler(
            app_settings,
            node_detect_fn=node_detect_fn,
        ),
        IntentName.SELF_TRANSFER: _make_self_transfer_handler(
            store,
            wallet_id,
            parsed,
            tx_flow,
            fee_estimator if fee_estimator is not None else FeeEstimator(client),
            scan_fn,
            # TCK-CPFP-002: the cpfp conversation state rides the ONE
            # session (ask supersede / staging marker / re-show record).
            session=send_session,
            seconds_since_last_block_fn=seconds_since_last_block_fn,
            scan_gate=scan_gate,
        ),
        IntentName.BUMP_FEE: _make_bump_fee_handler(
            store,
            wallet_id,
            parsed,
            tx_flow,
            fee_estimator if fee_estimator is not None else FeeEstimator(client),
            scan_fn,
            send_session,
            seconds_since_last_block_fn=seconds_since_last_block_fn,
            scan_gate=scan_gate,
        ),
    }


def _respond_handler(envelope: Envelope) -> dict[str, object]:
    """``respond``: identity passthrough of the model's chat text."""
    params = envelope.params
    if not isinstance(params, RespondParams):
        # Unreachable via validated envelopes; fail closed anyway.
        return {"error": "internal", "detail": "respond params shape mismatch"}
    return {"text": params.text}


def _clarify_handler(envelope: Envelope) -> dict[str, object]:
    """``clarify``: identity passthrough of the model's question."""
    params = envelope.params
    if not isinstance(params, ClarifyParams):
        return {"error": "internal", "detail": "clarify params shape mismatch"}
    return {"question": params.question}


def _store_error(exc: Exception) -> dict[str, object]:
    """Standard ``store_error`` result (detail is value-free by contract)."""
    return {"error": "store_error", "detail": str(exc)}


def _make_get_balance_handler(
    store: Store,
    wallet_id: int,
    scan_fn: Callable[[], object],
    scan_gate: StartupScan | None = None,
    price_oracle: PriceOracle | None = None,
    kick_scan_fn: Callable[[], bool] | None = None,
    defer_scans: bool = False,
) -> Handler:
    """Create the ``get_balance`` handler closed over the store.

    Sums the cached UTXO snapshot (confirmed/unconfirmed split) and
    reports the count of addresses holding UTXOs plus the tip height
    recorded by the last scan. If the wallet has never scanned (no sync
    cursor) AND no startup scan is in flight, ``scan_fn`` runs once
    lazily first — this keeps the Phase 0 AC ("What's my balance?"
    returns a correct live balance) working when the startup scan is
    opted out via :data:`AUTO_SCAN_ENV_VAR`. That inline blocking scan
    is the CLI transport only (``defer_scans=False``): in the web/engine
    world (``defer_scans=True``, TCK-UX-011 / ADR-0022 amendment 2) the
    same case ANSWERS immediately from the cache with ``freshness:
    stale`` + the additive ``scan_pending: true`` key and calls
    ``kick_scan_fn`` to start the background flow instead (idempotent;
    never a minutes-class turn block). While the non-blocking
    startup scan runs (ADR-0022 decision 6) the lazy scan stands down —
    the chain worker already owns the chain — and the cache answers as
    served. Every answer carries the deterministic, tool-owned
    ``freshness`` key (decision 5): ``stale`` while the first scan has
    not completed, ``fresh`` after; narration-only — the balance values
    stay verbatim from the cache. At most one scan attempt happens per
    call; a failed lazy scan surfaces as
    ``{"error": "chain_unavailable", "detail": <scrubbed>}`` (chain and
    scan error strings are value-free by contract), store failures as
    ``{"error": "store_error", ...}``. A failed/absent tip height omits
    the ``tip_height`` key entirely — never a fabricated value.

    Best-effort fiat (TCK-FIAT-001, no new intent; TCK-FIAT-002 multi-
    currency): when ``price_oracle`` is wired, the answer gains the USD
    pair ``usd_total_cents`` + ``btc_usd`` under the default display
    currency (byte-identical FIAT-001 wire shape) — or, for a non-USD
    display currency, the currency-tagged trio ``fiat_total_minor`` +
    ``fiat_currency`` + ``fiat_per_btc`` — plus the ``rate_stale``/
    ``rate_age_s`` markers on the ADR-0011 degrade ladder, all computed
    from the sats total. ANY price outcome short of a rate — unavailable
    feed, capability-absent backend, disabled oracle, or an unexpected
    failure — leaves those keys ABSENT: a sats-only answer, never an
    error, never a fabricated number.

    TCK-CHAT-001 (``address_number``): the additive param scopes the SAME
    cached-snapshot sum to the UTXOs of ONE registry address — resolution
    is engine-side against the store registry (the handler never widens:
    no new lookup, no network, one filter over the rows it already
    loaded), a miss answers the value-free ``address_ref_unknown`` clarify
    (never a whole-wallet answer for a bad referent — silently dropping
    the scope would retarget the user's mental model), and the answer
    RESTATES the resolved ``address`` alongside ``address_number`` for the
    full-address narration invariant.
    """

    def handler(envelope: Envelope) -> dict[str, object]:
        params = envelope.params
        if not isinstance(params, GetBalanceParams):
            return {"error": "internal", "detail": "get_balance params shape mismatch"}
        scan_pending = False
        scoped_address: str | None = None
        if params.address_number is not None:
            # TCK-CHAT-001 referent: resolve the stable number against the
            # registry FIRST — before any lazy scan (a bad referent must
            # not pay for a minutes-class scan to discover it is unknown).
            # Engine-verified bound-check; a miss is the value-free
            # clarify, never a whole-wallet fallback.
            try:
                scoped_address = _resolve_address_ref(
                    store, wallet_id, params.address_number
                )
            except (StoreError, sqlite3.Error) as exc:
                return _store_error(exc)
            if scoped_address is None:
                return {"error": _ADDRESS_REF_UNKNOWN}
        try:
            in_flight = scan_gate is not None and scan_gate.in_progress
            if (
                store.get_sync_state(wallet_id, wallet_scan.CURSOR_KEY) is None
                and not in_flight
            ):
                if defer_scans:
                    # TCK-UX-011 (ADR-0022 amendment 2): the web/engine
                    # world never blocks a turn on a minutes-class inline
                    # scan. Stand down like the SCAN-003 in-flight path —
                    # cache answers verbatim, stale-flagged — and KICK the
                    # background flow so the honest "first scan running in
                    # the background" is literally true (engine thread, no
                    # new threads, no store access off it). A later turn
                    # while the kicked scan runs sees ``in_flight`` and
                    # takes the unchanged SCAN-003 path: no second kick.
                    # TCK-UX-012(d) review MINOR: ``scan_pending`` reflects
                    # the KICK REALITY (kick_scan's bool), not mere intent —
                    # no kick fired (no seam, or the kick's plan failed)
                    # means no key, and the answer falls back to the plain
                    # stale note rather than claiming a load that never
                    # started.
                    scan_pending = bool(kick_scan_fn()) if kick_scan_fn is not None else False
                else:
                    try:
                        scan_fn()
                    except (ChainError, wallet_scan.ScanError, WatchKeyError) as exc:
                        # detail is scrubbed by the chain/scan layers
                        # (value-free of addresses/txids/amounts) — safe
                        # to surface verbatim.
                        return {"error": "chain_unavailable", "detail": str(exc)}
            utxos = store.get_utxos_for_wallet(wallet_id)
            tip_raw = store.get_sync_state(wallet_id, wallet_scan.TIP_KEY)
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)
        if scoped_address is not None:
            utxos = [u for u in utxos if u.address == scoped_address]
        confirmed = sum(u.value_sats for u in utxos if u.confirmed == 1)
        unconfirmed = sum(u.value_sats for u in utxos if u.confirmed != 1)
        result: dict[str, object] = {
            "confirmed_sats": confirmed,
            "unconfirmed_sats": unconfirmed,
            "total_sats": confirmed + unconfirmed,
            "addresses_scanned": len({u.address for u in utxos if u.address}),
            # Tool-owned freshness (ADR-0022 decision 5): the model
            # narrates it, never authors it; the values above are cache
            # verbatim either way.
            "freshness": _freshness(store, wallet_id, scan_gate),
        }
        if params.address_number is not None:
            # Full-address restatement (CHAT-001 invariant: a scoped
            # answer ALWAYS names the address; number-only is the bug).
            result["address_number"] = params.address_number
            result["address"] = scoped_address
        if scan_pending:
            # TCK-UX-011 (additive key, ADR-0022 amendment 2): the engine
            # stood the lazy scan down and kicked the background flow —
            # the narration line is driven by THIS flag, never by the
            # model. Absent on every other answer (unchanged shapes).
            result["scan_pending"] = True
        if tip_raw is not None:
            try:
                tip = int(tip_raw)
            except ValueError:
                tip = -1  # malformed cursor: omit rather than fabricate
            if tip >= 0:
                result["tip_height"] = tip
        # Fiat sugar (TCK-FIAT-001): best-effort USD total over the SAME
        # ADR-0011 ladder as create_tx's sats path. The whole block is
        # containment: price trouble must NEVER fail a balance answer —
        # any miss leaves the usd keys absent (sats-only, no error, no
        # value in any message; the oracle's own errors are value-free).
        # ADR-0022 amendment 1: while the backend choice is unresolved
        # (``awaiting_backend`` hold) the app makes ZERO chain calls —
        # the best-effort price fetch stands down with the lazy scan.
        # TCK-UX-011 (amendment 2): the same discipline on a
        # ``scan_pending`` stand-down — an answer that just deferred a
        # minutes-class scan to the background must not then BLOCK the
        # turn on a networked price fetch it only shows as display
        # sugar. The kicked scan's own completion refreshes the figures;
        # this answer is stale-flagged anyway (sats-only is honest).
        held = scan_gate is not None and scan_gate.state == "awaiting_backend"
        if price_oracle is not None and not held and not scan_pending:
            currency = None
            try:
                rate = price_oracle.fresh()
                fiat_minor = price_oracle.sats_to_usd(result["total_sats"], rate)
                currency = rate.currency
            except (PriceUnavailableError, ConfigDisabled):
                pass  # outage / capability-absent / opt-out → sats-only
            except Exception:  # noqa: BLE001, S110 — containment: never fail a balance answer over display sugar
                pass
            else:
                # Currency-aware fiat keys (TCK-FIAT-002, ADR-0011 amendment):
                # USD answers keep the byte-identical TCK-FIAT-001 wire shape
                # (``usd_total_cents`` + ``btc_usd`` — the web client and the
                # FIAT-001 pins consume them unchanged); a non-USD display
                # currency instead carries the explicit trio
                # ``fiat_total_minor`` (whole minor units: cents for the
                # two-decimal codes, yen for JPY) + ``fiat_currency``
                # (canonical lowercase code from the closed enum) +
                # ``fiat_per_btc`` (whole currency units per BTC — the
                # endpoint's unit semantics). The USD keys stay ABSENT for
                # non-USD: a EUR figure never rides a USD-shaped key.
                if currency == DEFAULT_DISPLAY_CURRENCY:
                    result["usd_total_cents"] = fiat_minor
                    result["btc_usd"] = rate.per_btc
                else:
                    result["fiat_total_minor"] = fiat_minor
                    result["fiat_currency"] = currency
                    result["fiat_per_btc"] = rate.per_btc
                if rate.stale:
                    # Stale-but-served (offline degrade): the narration
                    # marks the age exactly like the send card does.
                    result["rate_stale"] = True
                    result["rate_age_s"] = int(rate.age_s())
        return result

    return handler


def _make_get_history_handler(
    store: Store,
    wallet_id: int,
    scan_gate: StartupScan | None = None,
) -> Handler:
    """Create the ``get_history`` handler closed over the store.

    Projects cached transactions ordered by height DESC then block_time
    DESC (unconfirmed — ``height=None`` — sort as the newest entries),
    capped at ``params.limit`` or :data:`DEFAULT_HISTORY_LIMIT`. Items
    carry ``txid``/``height``/``direction``/``fee_sats``/``block_time``
    exactly as cached (nullable fields stay ``None``); the result is
    deliberately address-free — addresses appear only in
    ``get_utxos``/``new_address`` output. The result carries the
    tool-owned ``freshness`` key (ADR-0022 decision 6: this read MAY
    answer cache-served and stale-flagged before the first scan
    completes). No network I/O.
    """

    def handler(envelope: Envelope) -> dict[str, object]:
        params = envelope.params
        if not isinstance(params, GetHistoryParams):
            return {"error": "internal", "detail": "get_history params shape mismatch"}
        limit = params.limit if params.limit is not None else DEFAULT_HISTORY_LIMIT
        try:
            txs = store.get_txs_for_wallet(wallet_id)
            freshness = _freshness(store, wallet_id, scan_gate)
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)
        ordered = sorted(
            txs,
            key=lambda t: (
                t.height if t.height is not None else _NEVER_CONFIRMED,
                t.block_time if t.block_time is not None else _NEVER_CONFIRMED,
            ),
            reverse=True,  # stable: equal keys keep the store's txid order
        )
        shown = ordered[:limit]
        return {
            "transactions": [
                {
                    "txid": t.txid,
                    "height": t.height,
                    "direction": t.direction,
                    "fee_sats": t.fee_sats,
                    "block_time": t.block_time,
                }
                for t in shown
            ],
            "shown": len(shown),
            "freshness": freshness,
        }

    return handler


def _pending_summary(
    utxo_records: Sequence[UtxoRecord],
    tx_records: Sequence[TxRecord],
) -> dict[str, object]:
    """The compact pending block for a ``get_utxos`` answer (TCK-PENDING-001).

    Pure and store-only — no network, no clock, no new tracking:

    - **Incoming pending** = unconfirmed UTXO rows (the same
      ``confirmed != 1`` data the balance answer already carries). A
      coin CREATED by one of the wallet's own still-unconfirmed
      transactions is our own change, not an incoming payment — excluded
      by exact txid join (both rows already in the cache; no heuristics).
    - **Outgoing pending** = transaction rows with ``height is None`` and
      our spend directions (``out``/``self``) — broadcast-but-unconfirmed
      (the history row the broadcast handler writes, plus whatever a
      scan has since confirmed about direction) — MINUS the superseded
      rows: once one side of a lineage pair (``replaced_by_txid``,
      TCK-RBF-001) has confirmed, the sibling is terminal
      (:func:`~localwallet.store.superseded_states`: ``replaced``/
      ``evicted``) and exits the count. A pending count that can never
      go down is a lie; BIP-125 says exactly one of the pair ever will
      confirm, so the loser retires on the winner's height. The
      join for incoming exclusion still covers superseded spends (a
      replaced transaction's own change coin can never come to be — it
      must not read as an incoming payment).

    Documented bounds (the store's fidelity, narrated honestly rather
    than invented): schema v3 records amount/fee-rate/first-seen only for
    transactions broadcast by THIS build (legacy rows keep NULL), and the
    pending block itself carries only counts and verbatim store sums —
    the confirm-likelihood line is the static
    :data:`PENDING_NO_ETA_NOTE` degrade, never a fabricated probability
    or minute figure. Empty dict when nothing is pending (a clean
    wallet's answer is byte-identical to before).
    """
    retired = superseded_states(tx_records)
    outgoing = [
        t
        for t in tx_records
        if t.height is None and t.direction in (DIR_OUT, DIR_SELF)
    ]
    pending_spend_txids = {t.txid for t in outgoing}
    visible_outgoing = [t for t in outgoing if t.txid not in retired]
    incoming = [
        u
        for u in utxo_records
        if u.confirmed != 1 and u.txid not in pending_spend_txids
    ]
    if not incoming and not visible_outgoing:
        return {}
    return {
        "pending_incoming_count": len(incoming),
        "pending_incoming_sats": sum(u.value_sats for u in incoming),
        "pending_outgoing_count": len(visible_outgoing),
        # Tool-owned wording; the narration prints it verbatim (the model
        # never authors the estimate — there is none to author).
        "pending_eta_note": PENDING_NO_ETA_NOTE,
    }


def _resolve_in_flight_outgoing(
    tx_records: Sequence[TxRecord],
    *,
    now: int | None = None,
) -> list[dict[str, object]]:
    """TCK-RBF-005 shared resolver: the wallet's in-FLIGHT outgoing spends.

    PINNED CONTRACT (TCK-RBF-004 consumes exactly this API — no
    re-derivation): name ``_resolve_in_flight_outgoing``, positional
    ``tx_records``, keyword-only ``now``, returning ``list[dict]``.

    Args:
        tx_records: ONE wallet's verbatim transaction rows — pass
            :meth:`localwallet.store.Store.get_txs_for_wallet` output
            straight through (store order, txid ascending; entry
            ``index`` follows that order, so it is deterministic).
        now: unix seconds for the ``age_s`` computation; ``None`` reads
            :func:`time.time` (tests inject a fixed clock).

    Returns:
        One entry per in-flight outgoing transaction — rows with
        ``height is None`` and direction ``out``/``self``, MINUS the
        lineage-retired losers (:func:`localwallet.store.superseded_states`:
        a ``replaced`` original or an ``evicted`` bump is terminal, never
        in flight):

        - ``[]`` — nothing in flight. The honest empty answer; callers
          must never fabricate a transaction from it.
        - exactly one entry — THE in-flight transaction: assume-and-name-it
          semantics (the caller narrates its ``txid`` verbatim).
        - two or more — the disambiguation list; render it indexed.

        Entry keys: ``index`` (1-based position in the returned list),
        ``txid``, ``amount_sats``, ``fee_rate_centisat_vb``, ``age_s``
        (seconds since ``first_seen``, floored at 0). The last three are
        verbatim store values — a legacy (pre-v3) row or one broadcast
        before the capture shipped carries ``None``, meaning "not
        recorded", never invented.

    Pure: no store handle, no network, no clock beyond ``now``. The
    values are tool output (narratable verbatim); this function raises
    nothing and errors carry nothing.
    """
    retired = superseded_states(tx_records)
    clock = int(time.time()) if now is None else now
    entries: list[dict[str, object]] = []
    for row in tx_records:
        if row.height is not None or row.direction not in (DIR_OUT, DIR_SELF):
            continue
        if row.txid in retired:
            continue
        entries.append(
            {
                "index": len(entries) + 1,
                "txid": row.txid,
                "amount_sats": row.amount_sats,
                "fee_rate_centisat_vb": row.fee_rate_centisat_vb,
                "age_s": (
                    None if row.first_seen is None else max(0, clock - row.first_seen)
                ),
            }
        )
    return entries


def _make_get_utxos_handler(
    store: Store,
    wallet_id: int,
    scan_gate: StartupScan | None = None,
) -> Handler:
    """Create the ``get_utxos`` handler closed over the store.

    Returns the cached UTXO snapshot verbatim (``txid``/``vout``/
    ``address``/``value_sats``/``confirmed``) plus a count. Addresses
    come from the store — i.e. from tool output via the scan — so the
    quote-verbatim rule is satisfied end to end. The result carries the
    tool-owned ``freshness`` key (ADR-0022 decision 6: cache-served
    answers before the first scan completes are stale-flagged, never
    withheld). When anything is pending (unconfirmed receives or
    broadcast-unconfirmed spends), the answer gains the additive
    ``pending_*`` block from :func:`_pending_summary` — counts and
    verbatim store sums plus the static no-ETA honesty line; on a clean
    wallet these keys are ABSENT and the result shape is unchanged. The
    pending figures ride the cache, so during the first scan they are
    partial-but-verbatim — the stale flag already says so. No network
    I/O.

    TCK-CHAT-001: this listing is a SHOWING surface — every own address
    it prints gets its stable registry number assigned (idempotent
    first-showing write) and the row carries that ``number`` verbatim for
    the narration. The additive ``address_number`` param scopes the
    listing to one registry address (engine-resolved, bound-checked; a
    miss is the value-free ``address_ref_unknown`` clarify and the answer
    RESTATES the full address).
    """

    def handler(envelope: Envelope) -> dict[str, object]:
        params = envelope.params
        if not isinstance(params, GetUtxosParams):
            return {"error": "internal", "detail": "get_utxos params shape mismatch"}
        try:
            records = store.get_utxos_for_wallet(wallet_id)
            txs = store.get_txs_for_wallet(wallet_id)
            freshness = _freshness(store, wallet_id, scan_gate)
            scoped_address: str | None = None
            if params.address_number is not None:
                scoped_address = _resolve_address_ref(
                    store, wallet_id, params.address_number
                )
                if scoped_address is None:
                    return {"error": _ADDRESS_REF_UNKNOWN}
                records = [r for r in records if r.address == scoped_address]
            # Number every address this answer will PRINT (showing = the
            # registry's only writer; idempotent, so repeats never move a
            # number or re-stamp the date).
            numbers: dict[str, int] = {
                r.address: r.number for r in store.list_address_registry(wallet_id)
            }
            for address in sorted({r.address for r in records if r.address}):
                numbers[address] = store.note_address_shown(wallet_id, address).number
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)
        utxos = [
            {
                "txid": r.txid,
                "vout": r.vout,
                "address": r.address,
                "value_sats": r.value_sats,
                "confirmed": bool(r.confirmed),
                "number": numbers.get(r.address) if r.address else None,
            }
            for r in records
        ]
        result: dict[str, object] = {
            "utxos": utxos,
            "count": len(utxos),
            "freshness": freshness,
        }
        if params.address_number is not None:
            result["address_number"] = params.address_number
            result["address"] = scoped_address
        result.update(_pending_summary(records, txs))
        return result

    return handler


def _make_new_address_handler(
    store: Store,
    wallet_id: int,
    parsed: ParsedKey,
) -> Handler:
    """Create the ``new_address`` handler: allocate the next index.

    Allocation is pure store bookkeeping + deterministic derivation —
    it never requires the network: the address is derived from the
    parsed key at the branch's ``next_index`` (derivation is a pure
    function — re-deriving the same index always yields the same
    address), the row is upserted with status ``allocated``,
    :meth:`~localwallet.store.Store.allocate` records the allocation,
    and :meth:`~localwallet.store.Store.bump_next_index` consumes the
    index so the next call yields the next index. Rescans preserve
    ``allocated`` rows by address string (ADR-0009), so an issued
    address is never silently re-issued.

    Single-process assumption / crash window: allocation is read → derive
    → upsert → allocate → bump across separate store transactions. This
    CLI is single-user (no concurrent writers), so under normal operation
    the index advances exactly once per call. A crash between the upsert
    and the bump can however re-issue the SAME address string on the next
    call — a same-string re-issue (no fund loss, the address is already
    this wallet's), never a skipped or duplicated derivation index.

    TCK-CHAT-001 (numbering at FIRST SHOWING): the address this handler
    returns is always printed to the user by ``_print_new_address``, so
    the handler registers it (idempotent ``note_address_shown`` — an
    address previewed earlier by ``/receive`` KEEPS its original number)
    and the result carries the stable ``address_number`` for the narration
    to print verbatim. Registry failure never un-allocates the address:
    a store error here is surfaced by the (unchanged) value-free
    ``store_error`` path, and the allocation itself already committed in
    its own transactions — nothing half-happens.
    """

    def handler(envelope: Envelope) -> dict[str, object]:
        params = envelope.params
        if not isinstance(params, NewAddressParams):
            return {"error": "internal", "detail": "new_address params shape mismatch"}
        branch = params.branch if params.branch is not None else 0
        try:
            index = store.get_derivation(wallet_id, branch).next_index
            address = BranchDeriver(parsed, branch).address(index)
            store.upsert_batch(
                [
                    AddressRecord(
                        wallet_id=wallet_id,
                        branch=branch,
                        index=index,
                        address=address,
                        script_type=parsed.script_type,
                        status=ADDRESS_ALLOCATED,
                    )
                ]
            )
            store.allocate(wallet_id, branch, index)
            store.bump_next_index(wallet_id, branch)
            # First showing (this handler's answer is always narrated):
            # assign/return the stable registry number BEFORE any error
            # path can skip it — same atomicity discipline as the rest of
            # the allocation bookkeeping (single writer).
            registry = store.note_address_shown(wallet_id, address)
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)
        return {
            "address": address,
            "branch": branch,
            "index": index,
            "address_number": registry.number,
        }

    return handler


# ------------------------------------------------- referential addresses
#
# TCK-CHAT-001: the stable per-wallet NUMBER is the user's handle for an
# address ("address 3", "#3"). THREE invariants shape everything below:
# (a) resolution is ENGINE-side against the store registry — a miss is the
# value-free :data:`ADDRESS_REF_UNKNOWN` clarify, never a guess and never a
# nearest-match; (b) EVERY resolution restates the FULL address (a number-
# only answer is the bug the council named: silent retargeting); (c) the
# registry numbers/labels are never fabricated — numbers exist only because
# a surface SHOWN the address, and the label slot renders honestly empty
# until address-keyed labels exist (TCK-CHAT-003).

#: Handler error CODE (not text) for a referent the registry does not
#: hold — the narration maps it to :data:`ADDRESS_REF_UNKNOWN`, the
#: value-free clarify. The code itself carries no number.
_ADDRESS_REF_UNKNOWN: Final[str] = "address_ref_unknown"


def _resolve_address_ref(store: Store, wallet_id: int, number: int) -> str | None:
    """The full address behind ``number``, or ``None`` (out of range).

    The engine-verified bound-check: ``None`` is not an error to raise but
    the caller's cue for the value-free clarify — the store never guesses
    a nearest number and this helper never echoes the number it could not
    resolve.
    """
    record = store.get_address_by_number(wallet_id, number)
    return record.address if record is not None else None


def _used_state(store: Store, wallet_id: int) -> dict[str, dict[str, object]]:
    """Per-address activity join (pure store read — the "used" truth).

    Keyed by address; ``used`` is what the list header honestly defines
    ("we've seen activity"), computed as: the scan marked the address
    ``used``, OR the wallet currently holds a coin on it (a UTXO row IS
    seen activity). ``sats_total`` sums the stored values verbatim; it is
    ``None`` (never a fabricated 0) for an address with no UTXO row —
    spent-through and never-funded addresses both print without a value,
    and the freshness bound of the whole join belongs to the last scan,
    which the caller carries separately (``freshness`` / header copy).
    """
    state: dict[str, dict[str, object]] = {}
    for branch in (BRANCH_RECEIVE, BRANCH_CHANGE):
        for row in store.get_addresses(wallet_id, branch):
            state.setdefault(row.address, {})["used"] = row.status == ADDRESS_USED
    for utxo in store.get_utxos_for_wallet(wallet_id):
        if not utxo.address:
            continue
        entry = state.setdefault(utxo.address, {})
        entry["used"] = True
        entry["sats_total"] = int(entry.get("sats_total", 0)) + utxo.value_sats
    for entry in state.values():
        entry.setdefault("sats_total", None)
    return state


def _make_get_addresses_handler(
    store: Store,
    wallet_id: int,
    scan_gate: StartupScan | None = None,
) -> Handler:
    """Create the ``get_addresses`` handler (TCK-CHAT-001 registry query).

    ``params {}`` lists EVERY address the wallet has shown the user, in
    stable-number order; ``{"address_number": N}`` resolves N engine-side
    against the registry (a miss answers the value-free clarify) and
    answers with that ONE entry — its FULL address restated.

    Numbering at first showing, honestly: an address with activity the
    scan has seen but the user has never been shown is FIRST SHOWN by this
    very list, so the handler registers it (in receive-then-change, index
    order — deterministic) before building the rows. A never-shown, never-
    used address (the rest of the derivation window) is NOT here and gets
    no number: the registry tracks what the user has seen, not what the
    gap limit derived.

    The label slot is every row's ``"label": None`` — address-keyed labels
    do not exist yet (TCK-CHAT-003 lands them onto this exact field); the
    narration renders the slot honestly ``unlabeled``, and NOTHING here
    fabricates a label. The one-time referent hint (``hint_new``) is
    stamped by the LIST answer only (a single restatement teaches nothing
    new) and flips a persisted settings flag exactly once per wallet
    lifetime. No network I/O.
    """

    def handler(envelope: Envelope) -> dict[str, object]:
        params = envelope.params
        if not isinstance(params, GetAddressesParams):
            return {"error": "internal", "detail": "get_addresses params shape mismatch"}
        try:
            used = _used_state(store, wallet_id)
            if params.address_number is not None:
                # A single restatement SHOWS only that one address — it
                # must not number the other activity rows (a number is
                # minted at a showing, never as a side effect of lookup).
                record = store.get_address_by_number(wallet_id, params.address_number)
                if record is None:
                    # Out-of-range: the value-free clarify, never a guess.
                    return {"error": _ADDRESS_REF_UNKNOWN}
                return {
                    "addresses": [_registry_entry(record, used)],
                    "requested_number": params.address_number,
                    "count": 1,
                    "freshness": _freshness(store, wallet_id, scan_gate),
                    "last_scan_at": store.get_sync_state(wallet_id, wallet_scan.SCAN_AT_KEY),
                }
            # Register activity-seen-but-never-shown addresses NOW (the
            # list is their first showing) — deterministic (branch, index)
            # order so numbers follow the wallet's own address order.
            numbered = {row.address for row in store.list_address_registry(wallet_id)}
            to_show: list[tuple[int, int, str]] = []
            for branch in (BRANCH_RECEIVE, BRANCH_CHANGE):
                for row in store.get_addresses(wallet_id, branch):
                    if row.address not in numbered and (
                        row.status == ADDRESS_USED
                        or (used.get(row.address) or {}).get("used") is True
                    ):
                        to_show.append((branch, row.index, row.address))
            for _branch, _index, address in sorted(to_show):
                store.note_address_shown(wallet_id, address)
            rows = store.list_address_registry(wallet_id)
            freshness = _freshness(store, wallet_id, scan_gate)
            last_scan_at = store.get_sync_state(wallet_id, wallet_scan.SCAN_AT_KEY)
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)

        # The hint teaches the NUMBERED LIST; an empty registry prints the
        # honest empty line and consumes nothing (the flag only flips the
        # first time rows actually print).
        hint_new = bool(rows) and store.get_setting(_ADDRESS_REF_HINT_SETTING) != "1"
        if hint_new:
            store.set_setting(_ADDRESS_REF_HINT_SETTING, "1")
        result: dict[str, object] = {
            "addresses": [_registry_entry(r, used) for r in rows],
            "count": len(rows),
            "freshness": freshness,
            "last_scan_at": last_scan_at,
        }
        if hint_new:
            result["hint_new"] = True
        return result

    return handler


def _registry_entry(record: AddressRegistryRecord, used: Mapping[str, dict[str, object]]) -> dict[str, object]:
    """One list row: verbatim address, stable number, honest activity/
    value join, and the label slot that is nothing until CHAT-003 fills it."""
    truth = used.get(record.address, {})
    return {
        "number": record.number,
        "address": record.address,
        "used": truth.get("used") is True,
        "sats_total": truth.get("sats_total"),
        # Honest empty slot until TCK-CHAT-003's address-keyed labels
        # exist (never fabricated; coin tags are OUTPOINT-scoped and do
        # not generalize to an address).
        "label": None,
    }


def _pending_remaining_s(flow: TxFlow) -> int:
    """Seconds until the pending transaction expires, floored at 0.

    The age is measured with the flow's OWN injected clock — the same
    clock that stamped ``created_at`` and that :meth:`TxFlow.confirm`
    compares against :data:`PENDING_TTL_S` — so a re-shown card or an
    injected FACTS value can never claim more remaining lifetime than
    the confirm gate will actually grant (TCK-P2-004 SR fix: expiry
    honesty). At or past the TTL this reports ``0``; the next confirm
    attempt then reports the expiry (CREATED → EXPIRED).
    """
    pending = flow.pending
    if pending is None:
        return 0
    remaining = PENDING_TTL_S - (flow._clock() - pending.created_at)
    return max(0, int(remaining))


def _eta_for(
    fee_target: str | None,
    *,
    seconds_since_last_block_fn: Callable[[], int | None] | None,
) -> dict[str, object] | None:
    """Compute the confirmation-ETA fields for a fee target (narration-only).

    Deterministic and value-free (ADR-0020): the ETA is a decoration for the
    confirmation card / FACTS, never a gate input. ``None`` when the target
    is unknown or not a :class:`FeeTarget` (defensive — fail closed, no
    fabricated ETA). The mempool hint (``seconds_since_last_block_fn``) is
    consulted best-effort; any failure yields no congestion adjustment, never
    a crash.
    """
    if fee_target is None:
        return None
    try:
        target = FeeTarget(fee_target)
    except ValueError:
        return None
    hint: int | None = None
    if seconds_since_last_block_fn is not None:
        try:
            hint = seconds_since_last_block_fn()
        except Exception:  # noqa: BLE001 — fail-closed: no hint ⇒ base estimate
            hint = None
    eta = estimate_eta(target, seconds_since_last_block=hint)
    return {
        "eta_blocks": eta.expected_blocks,
        "eta_minutes": eta.expected_minutes,
        "eta_wording": eta.wording,
    }


#: Speed-rung positions on the estimator's three-target ladder (the ORDER
#: only — rates themselves always come from the estimator, never here).
_FEE_LADDER_ORDER: Final[dict[str, int]] = {"fast": 0, "medium": 1, "slow": 2}


def _requote_notice(current_target: str | None, target: FeeTarget) -> str | None:
    """Ceiling/floor guard for a re-quote request (§2.3 outcome map).

    Asking faster than the fastest rung (fast→fast) or slower than the
    cheapest rung already AT the network minimum (slow→slow) is refused —
    the staged record untouched. A same-rung ``medium`` is the legal
    no-op-ish rebuild (identical numbers, fresh ``tx_ref``/TTL, offer
    retired), and every other rung move re-quotes. ``None`` ⇒ no refusal.
    """
    if current_target is None:
        return None
    if current_target == FeeTarget.FAST.value and target is FeeTarget.FAST:
        return "ceiling"
    if current_target == FeeTarget.SLOW.value and target is FeeTarget.SLOW:
        return "floor"
    return None


def _requote_direction(current_target: str | None, target: FeeTarget) -> str | None:
    """The re-quote lead line's direction word (display-only): ``"faster"``
    / ``"slower"`` when the rung moved, ``None`` for a same-rung rebuild.
    """
    if current_target is None or current_target == target.value:
        return None
    if _FEE_LADDER_ORDER.get(target.value, 1) < _FEE_LADDER_ORDER.get(current_target, 1):
        return "faster"
    return "slower"


def _pending_tx_facts(
    flow: TxFlow,
    *,
    seconds_since_last_block_fn: Callable[[], int | None] | None = None,
) -> dict[str, object]:
    """The pending-transaction FACTS the model must quote verbatim.

    Injected on every turn that STARTS with the flow in ``CREATED``
    (TCK-P2-004 SR fix): the system prompt instructs the model to quote
    ``tx_ref`` VERBATIM from the confirmation card, but the card is only
    printed to the terminal — the model never sees terminal output.
    These facts are the machine-readable counterpart of the card, built
    exclusively from the dispatcher-owned pending record (never from
    user or model text) so a production ``confirm_tx`` envelope can name
    the right transaction. The values reach the prompt through
    :func:`~localwallet.agent.context.render_facts` inside the loop,
    which sanitizes each value (R8); the keys here are code-controlled
    by construction.

    The confirmation ETA (TCK-P5-002) is included as a dispatcher-owned,
    narration-only fact (``pending_tx_eta_*``) so the model can narrate
    honest expectations — never generated by the model.
    """
    pending = flow.pending
    if pending is None:  # defensive: CREATED always carries a pending tx
        return {}
    eta = _eta_for(pending.fee_target, seconds_since_last_block_fn=seconds_since_last_block_fn)
    if pending.self_payment_indices is not None:
        # TCK-TX-SELF-001: a self-transfer plan carries no user-stated
        # recipient to re-quote (confirm_tx needs only the tx_ref) — the
        # engine-derived destination addresses stay OUT of the model
        # transcript; the plan is described by the code-owned summary
        # (:func:`_self_plan_words`), never by anything the model could
        # "correct" into a new destination.
        facts: dict[str, object] = {
            "pending_tx_ref": pending.tx_ref,
            "pending_tx_plan": _self_plan_words(pending),
            "pending_tx_expires_in_s": _pending_remaining_s(flow),
        }
        if eta is not None:
            facts["pending_tx_eta_minutes"] = eta["eta_minutes"]
            facts["pending_tx_eta_wording"] = eta["eta_wording"]
        return facts
    facts_all: dict[str, object] = {
        "pending_tx_ref": pending.tx_ref,
        "pending_tx_amount_sats": pending.amount_sats,
        "pending_tx_recipient": pending.recipient,
        "pending_tx_expires_in_s": _pending_remaining_s(flow),
    }
    if eta is not None:
        facts_all["pending_tx_eta_minutes"] = eta["eta_minutes"]
        facts_all["pending_tx_eta_wording"] = eta["eta_wording"]
    return facts_all


def _flow_facts(
    flow: TxFlow,
    *,
    seconds_since_last_block_fn: Callable[[], int | None] | None = None,
) -> dict[str, object]:
    """The dispatcher-owned FACTS for the flow's CURRENT state.

    Injected at the top of every REPL turn (before the model runs) so the
    model can quote the values the NEXT destructive envelope must carry —
    the machine-readable counterpart of the printed narration, built
    exclusively from dispatcher-owned records (never from user or model
    text; the P2-004 lesson, extended to the full lifecycle):

    - ``CREATED`` → the pending card facts (:func:`_pending_tx_facts`) for
      ``confirm_tx``;
    - ``CONFIRMED`` → the approved record's ``confirmed_tx_ref`` (plus
      amount/recipient context) for ``sign_tx``;
    - ``SIGNED`` → ``signed_tx_ref`` for ``broadcast_tx``;
    - ``BROADCAST`` → ``broadcast_txid`` (plus ``broadcast_tx_ref``) so a
      ``tx_status`` envelope can quote the chain-reported id verbatim —
      REQUIRED for the production path: the model never sees terminal
      output, so without these facts it could only invent a txid.

    ``seconds_since_last_block_fn`` feeds the narration-only ETA fact
    (:func:`_eta_for`); ``None`` ⇒ the ETA uses no congestion adjustment.

    Values reach the prompt through
    :func:`~localwallet.agent.context.render_facts`, which sanitizes each
    value (R8); keys are code-controlled by construction.
    """
    if flow.state is TxFlowStatus.CREATED:
        return _pending_tx_facts(
            flow, seconds_since_last_block_fn=seconds_since_last_block_fn
        )
    confirmed = flow.confirmed
    if flow.state is TxFlowStatus.CONFIRMED and confirmed is not None:
        if confirmed.self_payment_indices is not None:
            # TCK-TX-SELF-001: same transcript discipline as CREATED — a
            # self-transfer plan never injects its engine-derived
            # addresses; sign_tx quotes only the confirmed ref.
            return {
                "confirmed_tx_ref": confirmed.tx_ref,
                "confirmed_tx_plan": _self_plan_words(confirmed),
            }
        return {
            "confirmed_tx_ref": confirmed.tx_ref,
            "confirmed_tx_amount_sats": confirmed.amount_sats,
            "confirmed_tx_recipient": confirmed.recipient,
        }
    signed = flow.signed
    if flow.state is TxFlowStatus.SIGNED and signed is not None:
        return {"signed_tx_ref": signed.tx_ref}
    if flow.state is TxFlowStatus.BROADCAST and flow.txid is not None:
        facts: dict[str, object] = {"broadcast_txid": flow.txid}
        if signed is not None:
            facts["broadcast_tx_ref"] = signed.tx_ref
        return facts
    return {}


def _address_registry_facts(store: Store) -> dict[str, object]:
    """The TCK-CHAT-001 registry FACTS for one turn (routing help, never
    authority).

    Shape (only when the active wallet has shown addresses)::

        address_registry: #1:bc1q…:used #2:bc1q…:not-used-yet …
        address_registry_count: 27
        address_registry_note: showing first 20 of 27 — quote the NUMBER \
only; the app resolves and restates the address

    Keys are code-controlled (the render_facts contract); values pass
    through its sanitizer. Entry order is the stable number order. The
    injection is BOUNDED (``_REGISTRY_FACTS_MAX`` entries within
    ``_REGISTRY_FACTS_CHARS`` characters, never cut mid-entry) and the
    note says honestly how many exist, so the model can never conclude a
    number "does not exist" from an omitted row — every ``address_number``
    param is re-resolved engine-side against the FULL registry regardless
    of what this line showed. No wallet / empty registry → no facts at all
    (a fresh wallet teaches the model nothing it can misuse). Used-state
    comes from the same store join as the list narration — never a model
    inference, never a fabrication.
    """
    wallet = store.get_active_wallet()
    if wallet is None:
        return {}
    rows = store.list_address_registry(wallet.id)
    if not rows:
        return {}
    # ponytail: the used-state join is O(window rows + UTXO rows) per turn
    # (cheap at realistic sizes; upgrade path = a cached store count join).
    used = _used_state(store, wallet.id)
    parts: list[str] = []
    budget = _REGISTRY_FACTS_CHARS
    for row in rows:
        truth = used.get(row.address, {})
        item = (
            f"#{row.number}:{row.address}:"
            f"{'used' if truth.get('used') is True else 'not-used-yet'}"
        )
        if len(parts) >= _REGISTRY_FACTS_MAX or len(item) + 1 > budget:
            break
        budget -= len(item) + 1
        parts.append(item)
    facts: dict[str, object] = {
        "address_registry": " ".join(parts),
        "address_registry_count": len(rows),
    }
    if len(parts) < len(rows):
        facts["address_registry_note"] = (
            f"showing first {len(parts)} of {len(rows)} — quote the NUMBER "
            "only; the app resolves and restates the address"
        )
    return facts


def _tx_pending_result(
    flow: TxFlow,
    *,
    seconds_since_last_block_fn: Callable[[], int | None] | None = None,
) -> dict[str, object]:
    """The ``tx_pending`` refusal result, carrying the pending card fields.

    Surfaced when ``create_tx`` arrives while a transaction is already
    pending for a DIFFERENT destination (a same-destination re-quote
    replaces instead — §2.1). A stale pending is recovered explicitly
    (ADR-0013), never reaped. The pending card is re-shown from the
    flow's own record so the user can act on it; rate fields are unknown
    on re-show (``usd_cents=None``) and ``expires_in_s`` is the REMAINING
    ttl (flow's clock, floored at 0) — a re-shown card never claims more
    lifetime than the confirm gate will grant (a pending expired by the
    clock is refused at confirm anyway). The card view is variant B
    (``fee_target_defaulted=False``): the flow record cannot know whether
    the staged target came from an omitted param or a stated preference,
    and the offer is deliberately a one-shot on the fresh card (§2.0) —
    unrecognized chatter never re-pitches it.
    """
    result: dict[str, object] = {
        "error": "tx_pending",
        "fee_target_defaulted": False,
        "fee_requote": False,
    }
    pending = flow.pending
    if pending is not None:
        eta = _eta_for(
            pending.fee_target, seconds_since_last_block_fn=seconds_since_last_block_fn
        )
        result.update(
            {
                "tx_ref": pending.tx_ref,
                "amount_sats": pending.amount_sats,
                "recipient": pending.recipient,
                "fee_sats": pending.fee_sats,
                "fee_rate_centisat_vb": pending.fee_rate_centisat_vb,
                "fee_rate_display": format_sat_vb(pending.fee_rate_centisat_vb),
                "vsize": pending.vsize,
                "change_sats": pending.change_sats,
                "inputs_count": pending.inputs_count,
                "usd_cents": None,
                "rate_stale": False,
                "rate_age_s": None,
                "rate_fetched_at": None,
                "fee_target": pending.fee_target,
                "expires_in_s": _pending_remaining_s(flow),
            }
        )
        if pending.self_payment_indices is not None:
            # TCK-TX-SELF-001: the pending is a self-transfer PLAN — the
            # re-show must render the plan (N × each, sources, fee), never
            # a single-recipient card that would show one output of the
            # reshuffle as if it were the whole send. The uniform per-part
            # value is amount_sats // parts BY CONSTRUCTION (the handler
            # stages equal payment outputs; consolidate has parts == 1).
            parts = len(pending.self_payment_indices)
            result.update(
                {
                    "self_transfer": True,
                    "self_mode": "split" if parts > 1 else "consolidate",
                    "self_parts": parts,
                    "self_each_sats": pending.amount_sats // parts,
                    "self_inputs_total_sats": pending.amount_sats + pending.fee_sats,
                    "self_new_addresses": parts,
                }
            )
        if eta is not None:
            result["eta_blocks"] = eta["eta_blocks"]
            result["eta_minutes"] = eta["eta_minutes"]
            result["eta_wording"] = eta["eta_wording"]
    return result


def _make_create_tx_handler(
    store: Store,
    wallet_id: int,
    parsed: ParsedKey,
    flow: TxFlow,
    fee_estimator: FeeEstimator,
    price_oracle: PriceOracle,
    scan_fn: Callable[[], object],
    *,
    seconds_since_last_block_fn: Callable[[], int | None] | None = None,
    scan_gate: StartupScan | None = None,
    settings: Settings | None = None,
    session: SendSession | None = None,
) -> Handler:
    """Create the ``create_tx`` handler: stage an unsigned pending transaction.

    Pipeline (every step fail-closed; nothing stages unless ALL succeed):

    0. First-scan gate (TCK-SCAN-003, ADR-0022 decision 6 — the
       load-bearing AC): while the enabled startup scan's first scan has
       NOT completed, the handler refuses with the friendly, value-free
       :data:`WALLET_LOADING_REFUSAL` line (``{"error": "wallet_loading",
       ...}``) BEFORE any network or store work. Sending against a
       partial/empty cache could select coins or present a balance the
       scan would later revise — this is dispatcher-owned code gating
       value movement, never model judgment, and not a silent partial
       send. Once the first scan completed (or was skipped by a startup
       failure), the gate lifts and the pre-split path applies: a store
       with no completed scan still lazy-scans (step 4) exactly as
       before, so ``AUTO_SCAN=0`` sessions and failed-startup sessions
       behave as they always did.
       ``confirm_tx``/``sign_tx``/``broadcast_tx`` are untouched: a
       pending can only exist post-gate, and the ADR-0013 state machine
       remains their only authority.

    1. Pending guard: with a transaction already pending, an *identical*
       recipient + ``amount_sats`` envelope is a dispatcher-owned
       **re-quote** (FLOW-REQUOTE, TCK-UX-002 / ADR-0013 amendment): the
       pipeline runs again at the requested ``fee_target`` and
       :meth:`TxFlow.create` REPLACES the staged record on success — new
       ``tx_ref``, fresh TTL, still exactly one pending, the old reference
       inert. Anything that would change the money destination (different
       recipient, or a USD amount that cannot be compared to the staged
       sats) still refuses with ``{"error": "tx_pending", ...}`` plus the
       pending card fields — before any network or store work. A re-quote
       whose rung is already the pending's rung refuses with
       ``rate_notice`` ceiling/floor on top of the pending fields (the
       ladder has rungs, not arbitrary rates — §2.3); a same-rung
       ``medium`` is a legal no-op-ish rebuild (§2.3, fresh ref/TTL).
       A re-quote carrying an explicit ``fee_rate_sat_vb`` (TCK-FEE-002,
       the answer to the ceiling ask) is NOT bound by the rung guard —
       it replaces through the same commit-only-on-success path at the
       user-quoted literal rate.
     2. Amount resolution: ``amount_sats`` is taken direct;
        ``amount_usd`` requires the price oracle (:meth:`PriceOracle.fresh`)
        and is an amount in the display currency (TCK-FIAT-002: the
        conversion rides the setting — the field name is historical, the
        model never authors a currency).
        A price failure on the USD path refuses the whole request with
        ``{"error": "price_unavailable", ...}`` and NO flow entry (the
        user retries, or gives sats). On the sats path the oracle is
        consulted best-effort for the card's fiat display only — a failure
        there degrades to ``usd_cents=None`` and never blocks the send.
       A stale-but-served rate (ADR-0011 ladder) is marked ``rate_stale``
       with its age; the rate's fetch timestamp is included either way.
    3. Fee rate: the two fee knobs are mutually exclusive at the schema
       layer (ADR-0012 amendment, TCK-FEE-002). An explicit
       ``fee_rate_sat_vb`` is used VERBATIM as the bid (scaled exactly to
       the engine's centisat/vB unit) — no estimator call,
       no rung is recorded (``fee_target=None``, no fabricated ETA), and the
       card retires the speed offer like any stated preference. Otherwise
       ``fee_target`` maps onto :class:`FeeTarget`; when the model omits it
       the handler applies **MEDIUM** by default
       (documented decision, TCK-P2-004: a send with no stated urgency
       gets the half-hour target, never the cheapest/slowest). The
       omitted-vs-explicit distinction is display-only state — the result
       carries ``fee_target_defaulted`` so the card can offer the speed
       choice exactly once (§2.0); the flow semantics are unchanged. A
       failed fee lookup surfaces as ``chain_unavailable``.
    4. UTXO snapshot: read from the store; when the wallet has never
       scanned (no sync cursor) the scan runs once lazily first (same
       path as ``get_balance``), then the snapshot is re-read.
    5. Change address: the branch-1 ``next_index`` is DERIVED but NOT
       allocated while selection/build runs (derive-check-without-
       allocate). Store allocation (upsert ``allocated`` row →
       :meth:`Store.allocate` → :meth:`Store.bump_next_index`) happens
       ONLY after the PSBT build succeeds — so a failed selection/build
       needs no rollback (nothing was written), and a failed bookkeeping
       write leaves no pending transaction (the handler returns
       ``store_error``; a retry re-derives the SAME index because
       derivation is pure and the bump is the last write). Allocated
       rows are never removed (ADR-0009); the only split state —
       allocated row without bump — self-heals on that retry.
    6. Selection + PSBT: the pure tx engine (:func:`select_coins`,
       :func:`build_unsigned_psbt`) does all money math; the handler
       only maps store rows into engine inputs. The change cost passed
       to selection is computed from the change script's serialized
       size (``9 + len(script)`` vB), never hardcoded.
    7. Staging: :meth:`TxFlow.create` stamps the flow-owned ``tx_ref``;
       the handler returns the confirmation-card dict verbatim from the
       pending record plus the rate/USD display fields.

    Value discipline: ``InsufficientFundsError`` carries needed/available
    sats — those are user-facing UI figures (ADR-0012) returned as
    structured result keys for the narration, never placed in a detail
    string that could reach a log. All other engine/chain/store error
    strings are value-free by their layers' contracts.
    """

    def handler(envelope: Envelope) -> dict[str, object]:
        params = envelope.params
        if not isinstance(params, CreateTxParams):
            return {"error": "internal", "detail": "create_tx params shape mismatch"}

        # 0. First-scan gate (ADR-0022 decision 6): value movement waits
        #    for the first scan, before ANY network/store work and before
        #    even the pending guard. Fail closed on refusal; the copy is
        #    the dispatcher-owned, value-free line (the model narrates it
        #    verbatim; it never generates or "corrects" it).
        if scan_gate is not None and scan_gate.first_scan_incomplete:
            return {"error": "wallet_loading", "detail": WALLET_LOADING_REFUSAL}

        # 1. Pending guard (before any network/store work). A create_tx
        #    quoting the SAME recipient and sats amount as the staged
        #    record is a dispatcher-owned re-quote (FLOW-REQUOTE, §2.1):
        #    the destination money does not move, only the rung. Anything
        #    else while a tx pends still refuses with the re-shown card
        #    (at-most-one-pending, no interleaved destructive flows).
        staged = flow.pending if flow.state is TxFlowStatus.CREATED else None
        requote = (
            staged is not None
            # TCK-TX-SELF-001: a staged SELF-TRANSFER plan is never a
            # re-quote target — its first output address (what a matching
            # create_tx would quote) is engine-derived, not a user-stated
            # destination; replacing it with a single send would silently
            # change the plan's shape. Refuse with the pending card.
            and staged.self_payment_indices is None
            and params.recipient == staged.recipient
            and params.amount_sats is not None
            and params.amount_sats == staged.amount_sats
        )
        if staged is not None and not requote:
            return _tx_pending_result(
                flow, seconds_since_last_block_fn=seconds_since_last_block_fn
            )

        # 1.5 TCK-RBF-004: a re-quote whose staged record is a fee-bump
        #     replacement is refused — the create_tx pipeline RE-SELECTS
        #     coins, which is not the BIP-125 shape (the replacement must
        #     keep ALL the original's inputs + its own added funding).
        #     Re-bumping the SAME transaction (faster/slower) is the
        #     bump_fee handler's deterministic intercept; a create_tx
        #     cannot stand in for it without losing the lineage.
        if (
            requote
            and staged is not None
            and session is not None
            and session.bump_pending is not None
            and session.bump_pending.tx_ref == staged.tx_ref
        ):
            return {
                "error": "bump_requote",
                "detail": _BUMP_REQUOTE_GUIDANCE,
            }

        # 3 (resolved early — pure, no I/O): fee target for the
        # ceiling/floor guard below and the ladder estimate below. An
        # explicit user-quoted rate (fee_rate_sat_vb, schema-exclusive with
        # fee_target) resolves to NO rung: the literal rate is used and the
        # rung guard is skipped (that IS the ceiling-ask answer, UX-004 →
        # TCK-FEE-002).
        target = (
            None
            if params.fee_rate_sat_vb is not None
            else FeeTarget(params.fee_target) if params.fee_target
            else FeeTarget.MEDIUM
        )
        if requote and staged is not None and target is not None:
            notice = _requote_notice(staged.fee_target, target)
            if notice is not None:
                # Refused BEFORE any network/store work; the staged
                # record is untouched (same ref, same remaining TTL).
                return {
                    **_tx_pending_result(
                        flow, seconds_since_last_block_fn=seconds_since_last_block_fn
                    ),
                    "rate_notice": notice,
                }

        # 2. Amount resolution (sats direct; the fiat amount via the price
        #    oracle). TCK-FIAT-002: ``amount_usd`` is an amount in the
        #    USER'S DISPLAY CURRENCY — "the conversion rides the setting".
        #    The closed protocol field keeps its historical name (no
        #    grammar churn); the model never authors a currency code, and
        #    the card labels the figure with the actual currency, so a
        #    non-USD quote never silently reads as dollars.
        rate = None
        if params.amount_sats is not None:
            amount_sats = params.amount_sats
            try:
                rate = price_oracle.fresh()
            except (PriceUnavailableError, ConfigDisabled):
                rate = None  # display-only sugar on the sats path
        else:
            try:
                rate = price_oracle.fresh()
            except (PriceUnavailableError, ConfigDisabled) as exc:
                # Both messages are value-free (no rate/amount echo).
                return {"error": "price_unavailable", "detail": str(exc)}
            amount_sats = price_oracle.usd_to_sats(params.amount_usd, rate)

        currency = rate.currency if rate is not None else DEFAULT_DISPLAY_CURRENCY
        fiat_minor = (
            price_oracle.sats_to_usd(amount_sats, rate) if rate is not None else None
        )
        # Wire-compat (TCK-FIAT-002, same key design as the balance
        # answer): the USD default keeps the exact FIAT-001 card fields;
        # a non-USD display leaves the USD-shaped keys honestly None and
        # adds the currency-tagged trio below.
        usd_cents = fiat_minor if currency == DEFAULT_DISPLAY_CURRENCY else None
        btc_usd = (
            rate.per_btc
            if rate is not None and currency == DEFAULT_DISPLAY_CURRENCY
            else None
        )
        rate_stale = rate.stale if rate is not None else False
        rate_age_s = int(rate.age_s()) if rate is not None else None
        rate_fetched_at = rate.fetched_at if rate is not None else None

        # 3 (cont.). Fee rate: the literal user-quoted sat/vB rate when
        # present (the user's number is quoted verbatim and is the whole
        # point of the ceiling-ask answer; it arrives in whole sats/vB and
        # scales exactly to centisat/vB here), else the ladder (MEDIUM
        # default when neither knob is given). From here on ``fee_rate`` is
        # ALWAYS integer centisat/vB (1 sat/vB = 100) — the engine unit
        # (docs/fee-fractional-plan.md, TCK-FEE-003 wave).
        # TCK-FEE-004 min-relay clamp: the rung path is already floored
        # INSIDE the estimator (every rung MAX'd with the source/node floor;
        # ``estimate.clamped`` says whether the floor raised it); the
        # EXPLICIT path consults ONLY the floor here — the user's number
        # still wins whenever it clears the floor, and a floor raise is
        # narrated (never a silent alteration of an explicit rate; never a
        # silent sub-floor bid the node would refuse — MAX, never MIN).
        # This floor is DISTINCT from tx/replacement.py's BIP-125
        # INCREMENTAL relay floor (a bump must out-pay its original — a
        # different constant, untouched here; the clamp is INITIAL-bid only).
        floor_raised = False
        if params.fee_rate_sat_vb is not None:
            fee_rate, floor_raised = fee_estimator.clamp_to_min_relay_floor(
                params.fee_rate_sat_vb * 100
            )
        else:
            try:
                estimate = fee_estimator.estimate(target)
            except ChainError as exc:
                return {"error": "chain_unavailable", "detail": str(exc)}
            fee_rate = estimate.rate_centisat_vb
            floor_raised = estimate.clamped

        # 4. UTXO snapshot with the lazy first scan.
        try:
            if store.get_sync_state(wallet_id, wallet_scan.CURSOR_KEY) is None:
                try:
                    scan_fn()
                except (ChainError, wallet_scan.ScanError, WatchKeyError) as exc:
                    # detail is scrubbed by the chain/scan layers — safe verbatim.
                    return {"error": "chain_unavailable", "detail": str(exc)}
            utxos = store.get_utxos_for_wallet(wallet_id)
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)

        # Recipient output script (layer 3 already proved the address is a
        # mainnet witness-v0 P2WPKH bech32 string; containment anyway).
        try:
            recipient_script = bytes(address_to_scriptpubkey(params.recipient).data)
        except Exception:  # noqa: BLE001 — containment: embit raises varied errors for bad addresses; re-raising would leak the untrusted recipient into error strings
            return {"error": "internal", "detail": "recipient could not be encoded as a script"}

        # 5. Change candidate: derive, do NOT allocate yet (see docstring).
        try:
            change_index = store.get_derivation(wallet_id, BRANCH_CHANGE).next_index
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)
        change_address = BranchDeriver(parsed, BRANCH_CHANGE).address(change_index)
        change_script = bytes(address_to_scriptpubkey(change_address).data)

        # 6. Selection + PSBT via the pure tx engine.
        #
        # 6a. Tag-aware join (TCK-UTXO-004, docs/ux-utxo-notes-design.md
        # §4.1 — dispatcher-owned, model-free): coin_labels rows become the
        # ONE plain boolean the selection layer reads (``kyc_side``; a
        # mixed-lineage coin is kyc-side — the §1.3 fail-safe). Tag and note
        # TEXT stops here: never model context, never logs, never tx/ (labels
        # arrive as plain data, the same discipline as rate and settings —
        # ADR-0012 amendment). Unlabeled coins stay untagged (other side), so
        # a wallet without labels selects exactly as before the amendment.
        try:
            label_rows = store.get_coin_labels(wallet_id)
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)
        selection_inputs: Sequence[Any] = utxos
        kyc_outpoints = {
            (row.txid, row.vout)
            for row in label_rows
            if coin_partition(row.tags)[0]
        }
        if kyc_outpoints:
            # Only the kyc-side coins are re-wrapped: attribute-absent means
            # other-side under the engine's duck-type contract.
            selection_inputs = [
                SimpleNamespace(**vars(utxo), kyc_side=True)
                if (utxo.txid, utxo.vout) in kyc_outpoints
                else utxo
                for utxo in utxos
            ]
        # 6b. Coin-selection settings (doc §2.3 ladder) resolved PER
        # SELECTION: the stored rung is read fresh here, so a settings-panel
        # change lands on the next quote with no restart (the honest
        # requires_restart False on those entries); the env/config-file rung
        # is the startup Settings snapshot. A malformed value or a min>=max
        # rung-cross refuses fail-closed — resolve's errors name keys and
        # rungs only, never values (ADR-0009), so the detail is log-safe.
        env_settings = settings if settings is not None else Settings()
        try:
            coin_policy = resolve_coin_selection_settings(
                {key: getattr(env_settings, key, "") for key in COIN_SETTING_KEYS},
                {key: store.get_coin_setting(key) for key in COIN_SETTING_KEYS},
            )
        except ValueError as exc:
            return {"error": "selection_failed", "detail": str(exc)}
        try:
            selection = select_coins(
                selection_inputs,
                amount_sats,
                fee_rate,
                9 + len(change_script),  # serialized change-output cost in vB
                recipient_script,
                change_script=change_script,
                utxo_target_min_sats=coin_policy.target_min_sats,
                utxo_target_max_sats=coin_policy.target_max_sats,
                consolidate_below_sat_vb=coin_policy.consolidate_below_sat_vb,
            )
        except InsufficientFundsError as exc:
            # needed/available are user-facing UI figures (ADR-0012):
            # structured keys for narration, never a log-bound detail.
            return {
                "error": "insufficient_funds",
                "needed_sats": exc.needed,
                "available_sats": exc.available,
            }
        except SelectionError as exc:
            return {"error": "selection_failed", "detail": str(exc)}

        inputs: list[PsbtInputSource] = []
        try:
            for utxo in selection.selected:
                if not utxo.address:
                    return {
                        "error": "internal",
                        "detail": "cached utxo has no address record",
                    }
                record = store.get_by_address(utxo.address)
                if (
                    record is None
                    or record.wallet_id != wallet_id
                    or record.branch not in (0, 1)
                ):
                    return {
                        "error": "internal",
                        "detail": "cached utxo has no usable derivation record",
                    }
                inputs.append(
                    PsbtInputSource(
                        txid=utxo.txid,
                        vout=utxo.vout,
                        value_sats=utxo.value_sats,
                        script_pubkey=bytes(address_to_scriptpubkey(utxo.address).data),
                        branch=record.branch,
                        index=record.index,
                    )
                )
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)

        purpose = SCRIPT_PURPOSES[parsed.script_type]
        try:
            psbt, _meta = build_unsigned_psbt(
                inputs,
                [(recipient_script, amount_sats)],
                change_address if selection.change_sats is not None else None,
                selection.change_sats,
                account_key=parsed.hd_key,
                account_fingerprint=parsed.hd_key.my_fingerprint,
                account_path=(purpose + 2**31, MAINNET_COIN_TYPE + 2**31, 2**31),
                # change_index is in scope from step 5; the builder emits the
                # change output's bip32 derivation with it (TCK-HW-003).
                change_index=change_index,
            )
            psbt_base64 = psbt_to_base64(psbt)
        except PsbtError as exc:
            return {"error": "psbt_failed", "detail": str(exc)}

        # 5 (cont.). Allocation bookkeeping — strictly AFTER the successful
        # build, strictly BEFORE staging (see docstring for the failure
        # windows; no rollback path is needed under this ordering).
        try:
            store.upsert_batch(
                [
                    AddressRecord(
                        wallet_id=wallet_id,
                        branch=BRANCH_CHANGE,
                        index=change_index,
                        address=change_address,
                        script_type=parsed.script_type,
                        status=ADDRESS_ALLOCATED,
                    )
                ]
            )
            store.allocate(wallet_id, BRANCH_CHANGE, change_index)
            store.bump_next_index(wallet_id, BRANCH_CHANGE)
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)

        # 7. Stage the pending transaction (flow owns tx_ref identity).
        try:
            pending = flow.create(
                amount_sats=amount_sats,
                recipient=params.recipient,
                fee_rate_centisat_vb=fee_rate,
                fee_sats=selection.fee_sats,
                psbt_base64=psbt_base64,
                inputs_count=len(inputs),
                vsize=selection.estimated_vsize,
                fee_target=target.value if target is not None else None,
                change_sats=selection.change_sats,
            )
        except FlowError:
            # Unreachable single-threaded after the pending guard; fail
            # closed with the pending card rather than double-staging.
            return _tx_pending_result(
                flow, seconds_since_last_block_fn=seconds_since_last_block_fn
            )

        # Explicit-rate records carry no rung ⇒ no rung-derived ETA (the ETA
        # estimator is target-based; failing closed beats fabricating one).
        eta = _eta_for(pending.fee_target, seconds_since_last_block_fn=seconds_since_last_block_fn)

        result: dict[str, object] = {
            "tx_ref": pending.tx_ref,
            "amount_sats": pending.amount_sats,
            "recipient": pending.recipient,
            "fee_sats": pending.fee_sats,
            "fee_rate_centisat_vb": pending.fee_rate_centisat_vb,
            "fee_rate_display": format_sat_vb(pending.fee_rate_centisat_vb),
            "vsize": pending.vsize,
            "change_sats": pending.change_sats,
            "inputs_count": pending.inputs_count,
            # TCK-UTXO-004 (doc §4): display-only narration facts about the
            # FINAL selection — the card's mix warning and consolidation
            # clause render from these, so a re-quote (which re-runs
            # selection and re-renders the card) can never silently change
            # the tag-mix (§4.2: confirmation is only valid against the card
            # the user is reading). Terminal/renderer material only: these
            # keys never enter a FACTS block or the model transcript.
            "mixed": selection.mixed,
            "folded_count": selection.folded_count,
            "usd_cents": usd_cents,
            "rate_stale": rate_stale,
            "rate_age_s": rate_age_s,
            "rate_fetched_at": rate_fetched_at,
            "btc_usd": btc_usd,            "fee_target": pending.fee_target,
            # Display-only card-view selectors (TCK-UX-002; NOT flow
            # state): variant A of the card tail when the envelope carried
            # neither fee knob (the one-shot speed offer — a user-quoted
            # explicit rate IS a stated speed preference, TCK-FEE-002),
            # and the re-quote lead-line marker/direction when this result
            # replaced a pending record (§2.0/§2.3).
            "fee_target_defaulted": params.fee_target is None and params.fee_rate_sat_vb is None,
            "fee_requote": requote,
            "expires_in_s": PENDING_TTL_S,
            **({} if eta is None else eta),
        }
        if floor_raised:
            # TCK-FEE-004: the min-relay clamp raised THIS bid to the floor
            # (a policy rung under it, or an explicit user rate under a
            # higher source/node floor). Display-only narration marker —
            # one honest line under the Fee data line; ABSENT unless the
            # clamp fired (conditional-key pattern, TCK-FIAT-003).
            result["fee_floor_note"] = True
        if rate is not None and currency != DEFAULT_DISPLAY_CURRENCY:
            # TCK-FIAT-002 currency-tagged card fields (same design as the
            # balance answer): ``fiat_total_minor`` = the send amount in the
            # display currency's minor units, ``fiat_currency`` = canonical
            # code, ``fiat_per_btc`` = whole units per BTC. The USD-shaped
            # keys above stay None — never a mislabeled figure.
            result["fiat_total_minor"] = fiat_minor
            result["fiat_currency"] = currency
            result["fiat_per_btc"] = rate.per_btc
        if rate is not None:
            # TCK-FIAT-003 (MW-17 user note): the card's Fee line gains a
            # fiat conversion too — the SAME ADR-0011 rate as the Pay
            # segment, in the turn's EFFECTIVE display currency (the
            # oracle's reader already carries the per-ask one-shot). No
            # rate = no key = the sats-only Fee line exactly as today;
            # never a fabricated figure, never an error.
            result["fee_fiat_minor"] = price_oracle.sats_to_usd(pending.fee_sats, rate)
        if requote and staged is not None:
            if params.fee_rate_sat_vb is not None:
                # Explicit-rate re-quote: no rungs to compare — direction is
                # the literal rate vs the staged record's (display-only).
                if fee_rate > staged.fee_rate_centisat_vb:
                    result["requote_direction"] = "faster"
                elif fee_rate < staged.fee_rate_centisat_vb:
                    result["requote_direction"] = "slower"
            else:
                direction = _requote_direction(staged.fee_target, target)
                if direction is not None:
                    result["requote_direction"] = direction
        return result

    return handler


def _self_plan_words(pending: PendingTx) -> str:
    """The code-owned plan summary for FACTS (TCK-TX-SELF-001).

    Describes a staged self-transfer WITHOUT naming any address (the
    model never quotes one — the flow derives all destinations
    engine-side); counts and the uniform per-output sats value come from
    the dispatcher-owned record. Rendered through
    :func:`~localwallet.agent.context.render_facts` like every fact.
    """
    n = len(pending.self_payment_indices or ())
    if n > 1:
        return (
            f"self-transfer split into {n} equal parts of "
            f"{pending.amount_sats // n} sats (own new addresses)"
        )
    return (
        f"self-transfer consolidate {pending.inputs_count} coins into 1 "
        f"part of {pending.amount_sats} sats (own new address)"
    )


def _make_self_transfer_handler(
    store: Store,
    wallet_id: int,
    parsed: ParsedKey,
    flow: TxFlow,
    fee_estimator: FeeEstimator,
    scan_fn: Callable[[], object],
    *,
    session: SendSession | None = None,
    seconds_since_last_block_fn: Callable[[], int | None] | None = None,
    scan_gate: StartupScan | None = None,
) -> Handler:
    """Create the ``self_transfer`` handler: stage a plan-owned reshuffle.

    Explicit on-demand self-transfer (TCK-TX-SELF-001): SPLIT one coin into
    N equal parts, or CONSOLIDATE coins below a stated size into one. The
    envelope carries NO address, NO outpoint, NO recipient — every money
    value below is DERIVED here (dispatcher-owned, deterministic); the
    model only relays the two numbers the user stated. The staged flow is
    the SAME dispatcher-owned state machine as ``create_tx``
    (``TxFlow.create`` → CREATED → dual-key confirm → sign → broadcast —
    ADR-0013 untouched; the confirm/sign/broadcast whitelists and handlers
    never learn that this record is special beyond
    ``PendingTx.self_payment_indices``).

    Deterministic plan (documented engine policy, pinned by tests):

    - **SPLIT** (``parts``): the input is the wallet's LARGEST single UTXO
      — canonical order ``(value_sats, txid, vout)`` descending, so ties
      break deterministically; the largest coin is the one the user asks
      to shard ("split my big utxo into N"). The explicit request
      overrides ADR-0012's shatter-preservation default by design. N
      outputs go to N FRESH receive-branch (branch 0) addresses — the
      branch documented choice: these ARE user-facing coins (what
      ``new_address`` hands out), not fee-sweep change; consecutive
      indices from branch-0 ``next_index``. The per-part value is uniform:
      ``each = (V − fee) // parts`` with the sub-part remainder FOLDED INTO
      THE FEE (outputs stay exactly equal — the card's ``N × each`` is
      literally true; fold is bounded by ``parts − 1`` sats). No change
      output.
    - **CONSOLIDATE** (``below_size_sats``): the candidate set is every
      UTXO with ``value_sats`` STRICTLY below the threshold, in canonical
      order, capped at :data:`MAX_SELF_TRANSFER_CONSOLIDATE_INPUTS`.
      Privacy pools (TCK-UTXO-002) are RESPECTED: coins are partitioned
      kyc-side vs other-side exactly like ``create_tx``'s tag join; a
      consolidate NEVER mixes pool sides. If the below-threshold set spans
      both pools, this run consolidates the pool with the larger total
      value (ties → other-side first, the fixed pool order) and the result
      carries ``self_other_side_count`` for an honest "more small coins on
      the other side" card line — the documented pick (never refuse a
      mergeable wallet, never silently mix). One output to ONE fresh
      receive address, value ``total − fee``; no change.
    - The fee bid rides the SAME estimator ladder as ``create_tx``
      (``fee_target`` rung; MEDIUM default when neither knob is given — no
      ``fee_rate_sat_vb`` exists on this intent; an internal reshuffle
      never rides the explicit-rate override).
    - **CPFP** (TCK-CPFP-002, the ``cpfp`` mode — a separate pipeline,
      documented at :func:`_cpfp`): unstick a stuck INBOUND payment by
      spending its unconfirmed coin in a fresh high-fee child paying ONE
      own fresh receive address. Money math lives in
      :func:`~localwallet.tx.cpfp.build_cpfp_child_plan` (the ONLY fee
      computation; the child-pays-for-parent bound + the honest
      parent-fee-unknown shape come from its plan verbatim). Which coin is
      stuck is engine-resolved from a FRESH store read at every dispatch
      (the RBF-004 mid-conversation-recheck pattern — a payment that
      confirmed or vanished while an ask stood open is answered honestly,
      never staged against); the merge menu offers ONLY CONFIRMED own
      coins (BIP-125-eligible inputs), labels printed verbatim for the
      terminal and never routed through the model. The default rung is
      FAST (asking to hurry IS a stated urgency — the RBF-004 documented
      decision; no explicit-rate knob exists on this intent, so the fee
      knob that must persist across an ask is the rung). The child rides
      the SAME create→confirm→sign→broadcast state machine (dual-key +
      device handoff untouched; ``self_payment_indices`` marks the record,
      so the sign-time re-derivation and the label-lineage capture behave
      exactly as for any self-transfer plan). The hurried PARENT is never
      modified and NO lineage is written (store lineage is RBF-only; a
      child-parent link would be new schema — not this ticket).
    - **CONSOLIDATION CONVERSATION** (TCK-CONS-001, the dispatcher-owned
      twin of the cpfp asks — the conversation opens and advances in
      :func:`_run_consolidation_turn`, BEFORE the gate and the model, so
      label/tag words never route through the model). When a consolidate
      envelope answers an OPEN ask (the intercept stamps ``choice`` +
      ``picked`` on the session record — params carry only the engine's
      own max+1 threshold, never a coin reference), step 1.5 consumes the
      ask and the picked outpoints are revalidated against THIS fresh
      store read: a coin that vanished while the ask stood open is the
      honest ``cons_coin_gone`` answer, never a silently swapped set.
      Without an answered ask the threshold/pool policy above is
      byte-unchanged (the golden model-routed phrasings keep their
      direct-plan behavior). A staged conversation plan carries
      ``cons_merge`` (plan-echo card line) and arms the broadcast-time
      annotation: the §1.3 union inheritance PLUS the closed-set
      ``consolidation`` tag and the "consolidated from N outputs" note,
      N counted from the broadcast's own inputs.

    Bounds / fail-closed (every refusal BEFORE any allocation or staging;
    a staged plan only ever reflects a fully successful build):

    - First-scan gate (ADR-0022 decision 6): refusal identical to
      ``create_tx`` while the first scan is incomplete (interaction
      unchanged).
    - Anything already pending (flow ``CREATED``) → the ``tx_pending``
      refusal with the PENDING plan re-shown — self-transfer offers no
      same-plan re-quote (a re-quote would re-derive fresh destination
      indices; changing speed is cancel + re-ask, stated up front).
    - Empty wallet / nothing below the threshold / the split coin cannot
      fund ``fee + parts × dust`` → honest value-free refusals (the
      below-dust split rides the friendly InsufficientFunds-style
      :data:`_SELF_SPLIT_BELOW_DUST` line; an empty consolidation set is
      :data:`_SELF_NOTHING_BELOW`; over-cap is :data:`_SELF_TOO_MANY_SMALL`;
      a consolidation that cannot clear fee+dust reports the existing
      structured ``insufficient_funds`` pair — user-facing UI, ADR-0012).
    - Per-output dust is double-checked: pre-build here (fail closed with
      the friendly line) AND again inside :func:`build_unsigned_psbt`
      (computed from the script size, never a constant).

    Store discipline mirrors ``create_tx`` step 5: destination addresses
    are DERIVED while planning, allocated only AFTER the successful build
    and BEFORE staging (a failed build writes nothing; a mid-bookkeeping
    failure leaves no pending and self-heals on retry per ADR-0009).
    """

    def _inputs_for(utxo_rows: list[UtxoRecord]) -> tuple[list[PsbtInputSource], dict[str, object] | None]:
        """Map store rows to PSBT input sources (same containment as
        ``create_tx`` step 6): every row must resolve to THIS wallet's
        derivation record or the plan refuses value-free."""
        inputs: list[PsbtInputSource] = []
        try:
            for utxo in utxo_rows:
                if not utxo.address:
                    return [], {"error": "internal", "detail": "cached utxo has no address record"}
                record = store.get_by_address(utxo.address)
                if (
                    record is None
                    or record.wallet_id != wallet_id
                    or record.branch not in (0, 1)
                ):
                    return [], {
                        "error": "internal",
                        "detail": "cached utxo has no usable derivation record",
                    }
                inputs.append(
                    PsbtInputSource(
                        txid=utxo.txid,
                        vout=utxo.vout,
                        value_sats=utxo.value_sats,
                        script_pubkey=bytes(address_to_scriptpubkey(utxo.address).data),
                        branch=record.branch,
                        index=record.index,
                    )
                )
        except (StoreError, sqlite3.Error) as exc:
            return [], _store_error(exc)
        return inputs, None

    def _own_sources(
        rows: list[UtxoRecord],
    ) -> tuple[list[tuple[PsbtInputSource, UtxoRecord]], dict[str, object] | None]:
        """Store rows → the wallet's OWN PSBT input sources paired with
        their rows (TCK-CPFP-002): rows without an address, a usable
        derivation record, or a mappable script are DROPPED — a coin that
        cannot be built into the child is never offered (the RBF-004
        funding chooser's documented skip policy; every figure a card
        prints stays verbatim store truth)."""
        out: list[tuple[PsbtInputSource, UtxoRecord]] = []
        try:
            for row in rows:
                if not row.address:
                    continue
                record = store.get_by_address(row.address)
                if (
                    record is None
                    or record.wallet_id != wallet_id
                    or record.branch not in (0, 1)
                ):
                    continue
                try:
                    script = bytes(address_to_scriptpubkey(row.address).data)
                except Exception:  # noqa: BLE001,S112 — containment: a junk address row simply leaves the offerable set (value-free)
                    continue
                out.append(
                    (
                        PsbtInputSource(
                            txid=row.txid,
                            vout=row.vout,
                            value_sats=row.value_sats,
                            script_pubkey=script,
                            branch=record.branch,
                            index=record.index,
                        ),
                        row,
                    )
                )
        except (StoreError, sqlite3.Error) as exc:
            return [], _store_error(exc)
        return out, None

    def _cpfp(params: SelfTransferParams) -> dict[str, object]:
        """The child-pays-for-parent conversation (TCK-CPFP-002). The
        handler docstring's CPFP bullet is the contract; the pipeline
        mirrors the RBF-004 bump conversation (flow-posture guards first,
        FRESH store truth at every dispatch = the mid-conversation
        recheck, the fee estimator touched ONLY after resolution so every
        early refusal makes zero chain calls, asks carry the fee rung for
        re-quoting, staging is commit-only-on-success: the flow record is
        touched only after the full build + allocation bookkeeping)."""
        assert session is not None  # the caller refuses session-less wirings

        def _coin_ask(kind: str, **state: object) -> dict[str, object]:
            """Install an open ask carrying the resolved fee knob (the
            RBF-004 MAJOR lesson: the user's stated urgency survives the
            ask — the intercept re-quotes it onto the answer envelope)."""
            session.cpfp_ask = _CpfpAsk(kind=kind, fee_target=params.fee_target, **state)
            if kind == "coin":
                payload: list[dict[str, object]] = [dict(e) for e in state["entries"]]  # type: ignore[union-attr]
            else:
                payload = [
                    {
                        "index": i + 1,
                        "framing": option.framing,
                        "value_sats": option.value_sats,
                        "label": option.label_display,
                    }
                    for i, option in enumerate(state["options"])  # type: ignore[arg-type]
                ]
            return {"cpfp": True, "ask": kind, "options": payload}

        def _classify_gone(
            rows: list[UtxoRecord], txid: str, vout: int
        ) -> dict[str, object]:
            """The chosen payment left the unconfirmed set mid-conversation:
            CONFIRMED (a row still there, confirmed) is the honest
            already-confirmed answer; anything else is the undone/replaced
            answer. Neither ever stages a child."""
            row = next(
                (r for r in rows if r.txid.lower() == txid and r.vout == vout), None
            )
            if row is not None and row.confirmed == 1:
                return {"error": "cpfp_already_confirmed", "detail": _CPFP_ALREADY_CONFIRMED}
            return {"error": "cpfp_inbound_gone", "detail": _CPFP_INBOUND_GONE}

        # 1. Flow posture (value-free, zero chain calls): past-the-gate
        #    lifecycle states are busy (never abandon a committed plan);
        #    any other pending is the plan-card re-show; MY staged child
        #    re-shows AS the cpfp plan (the RBF-004 pending-guard shape).
        if flow.state in (TxFlowStatus.CONFIRMED, TxFlowStatus.SIGNED):
            return {"error": "cpfp_flow_busy", "detail": _CPFP_FLOW_BUSY}
        if flow.state is TxFlowStatus.CREATED:
            result = _tx_pending_result(
                flow, seconds_since_last_block_fn=seconds_since_last_block_fn
            )
            if (
                session.cpfp_pending is not None
                and flow.pending is not None
                and flow.pending.tx_ref == session.cpfp_pending.tx_ref
            ):
                result.update(session.cpfp_pending.display)
            return result

        # 2. Answer consumption (the deterministic intercept stamps
        #    ``choice`` on the open ask and re-quotes the fee knob onto
        #    this envelope). A FRESH cpfp envelope SUPERSEDES an open
        #    unanswered ask (every refusal below leaves nothing open; a
        #    new ask is installed only by this call's own branches).
        prior = session.cpfp_ask
        session.cpfp_ask = None
        answered = prior if prior is not None and prior.choice is not None else None

        # 3. Store snapshot, FRESH at every dispatch (the lazy first scan
        #    rides the same posture as split/consolidate step 3).
        try:
            if store.get_sync_state(wallet_id, wallet_scan.CURSOR_KEY) is None:
                try:
                    scan_fn()
                except (ChainError, wallet_scan.ScanError, WatchKeyError) as exc:
                    # detail is scrubbed by the chain/scan layers — safe verbatim.
                    return {"error": "chain_unavailable", "detail": str(exc)}
            utxo_rows = store.get_utxos_for_wallet(wallet_id)
            tx_rows = store.get_txs_for_wallet(wallet_id)
            label_rows = store.get_coin_labels(wallet_id)
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)
        all_coins, err = _own_sources(utxo_rows)
        if err is not None:
            return err
        clock = int(time.time())
        labels = {
            (row.txid.lower(), row.vout): row for row in label_rows
        }
        first_seen = {row.txid: row.first_seen for row in tx_rows}

        def _display(key: tuple[str, int]) -> tuple[str | None, tuple[str, ...]]:
            """The coin's stored label (display text + intercept match
            terms) — user data, terminal material ONLY: handler results
            are not prompt context, and ask answers route through the
            deterministic intercept, so label words never reach the
            model (the RBF-004 discipline, reused verbatim)."""
            label = labels.get(key)
            if label is None:
                return None, ()
            bits = [*label.tags] + ([label.note] if label.note else [])
            if not bits:
                return None, ()
            return (
                ", ".join(f"'{bit}'" for bit in bits),
                tuple(
                    word for bit in bits for word in bit.lower().split() if word
                ),
            )

        # 4. Deliverable 1: the unconfirmed INBOUND set (mempool coins
        #    credited to own addresses), canonical ascending order
        #    (value_sats, txid, vout) — the same determinism every other
        #    chooser here applies.
        inbound = sorted(
            (pair for pair in all_coins if pair[1].confirmed == 0),
            key=lambda p: (p[0].value_sats, p[0].txid.lower(), p[0].vout),
        )
        chosen: PsbtInputSource | None = None
        if answered is not None and prior is not None and prior.kind == "options":
            # The MENU answered: the coin the menu was opened for rides the
            # ask (dispatcher-owned) and is revalidated against THIS fresh
            # read — if it confirmed or was undone while the menu stood
            # open, the honest answer replaces the plan (mid-conversation
            # recheck; a child is never staged against a dead parent).
            assert prior.inbound is not None
            want = (prior.inbound.txid.lower(), prior.inbound.vout)
            match = next(
                (src for src, _ in inbound if (src.txid.lower(), src.vout) == want),
                None,
            )
            if match is None:
                return _classify_gone(utxo_rows, want[0], want[1])
            chosen = match
        elif answered is not None and prior is not None and prior.kind == "coin":
            # The coin ask answered: the CHOSEN outpoint (stamped by the
            # intercept from the ask's own entries — never a re-guess of
            # position, the set may have shifted while the ask stood open)
            # must still be an unconfirmed candidate; the mid-conversation
            # recheck is this fresh read.
            entry = prior.entries[prior.choice - 1]
            want = (str(entry["txid"]), int(str(entry["vout"])))
            match = next(
                (src for src, _ in inbound if (src.txid.lower(), src.vout) == want),
                None,
            )
            if match is None:
                return _classify_gone(utxo_rows, want[0], want[1])
            chosen = match
        elif not inbound:
            # Honest empty (deliverable 1): nothing unconfirmed coming in.
            return {
                "error": "cpfp_nothing_unconfirmed",
                "detail": _CPFP_NOTHING_UNCONFIRMED,
            }
        elif len(inbound) == 1:
            # Exactly one → proceed, naming it (the resolver's
            # assume-and-name semantics; the txid reaches the card only
            # through the staged result, verbatim).
            chosen = inbound[0][0]
        else:
            # Several and nothing chosen yet: the INDEXED choice ask —
            # amount + age + destination label, verbatim from the store
            # (an unrecorded age says "not recorded", never invented).
            entries = [
                {
                    "index": i + 1,
                    "txid": src.txid,
                    "vout": src.vout,
                    "value_sats": src.value_sats,
                    "age_s": (
                        None
                        if first_seen.get(src.txid) is None
                        else max(0, clock - int(first_seen[src.txid]))
                    ),
                    "label": _display((src.txid.lower(), src.vout))[0],
                }
                for i, (src, _) in enumerate(inbound)
            ]
            return _coin_ask("coin", entries=tuple(entries))
        assert chosen is not None  # every branch above resolves or returns

        # 5. Deliverable 2: the merge menu — offered ONLY over CONFIRMED
        #    own coins eligible as merge inputs (BIP-125-eligible
        #    inputs; the inbound coin itself is unconfirmed and never in
        #    this set). None eligible → the plain single-input plan is
        #    built directly (no fake menu). Framing words smallest/largest
        #    over the canonical ascending order; ONE eligible coin collapses
        #    the merge pair to a single "coin" option — the menu never
        #    offers the same coin twice.
        eligible = sorted(
            (pair for pair in all_coins if pair[1].confirmed == 1),
            key=lambda p: (p[0].value_sats, p[0].txid.lower(), p[0].vout),
        )
        plain_option = _CpfpOption(
            framing="plain",
            value_sats=None,
            coin=None,
            label_display=None,
            match_terms=(),
        )
        option: _CpfpOption
        if answered is not None and prior is not None and prior.kind == "options":
            # The menu answered (the stuck payment was revalidated in step
            # 4): the CHOSEN option rides the ask (code-stamped index); a
            # merge option's coin must still be an eligible CONFIRMED
            # input in this fresh read (a coin spent elsewhere mid-ask is
            # the honest gone answer, never a silently swapped one).
            option = prior.options[prior.choice - 1]
            if option.framing != "plain":
                assert option.coin is not None
                still = next(
                    (
                        src
                        for src, _ in eligible
                        if (src.txid.lower(), src.vout)
                        == (option.coin.txid.lower(), option.coin.vout)
                    ),
                    None,
                )
                if still is None:
                    return {"error": "cpfp_coin_gone", "detail": _CPFP_COIN_GONE}
                option = replace(option, coin=still)
        elif not eligible:
            option = plain_option
        else:
            options: list[_CpfpOption] = []
            if len(eligible) == 1:
                src, _row = eligible[0]
                display, terms = _display((src.txid.lower(), src.vout))
                options.append(
                    _CpfpOption(
                        framing="coin",
                        value_sats=src.value_sats,
                        coin=src,
                        label_display=display,
                        match_terms=terms,
                    )
                )
            else:
                for framing, (src, _) in (("smallest", eligible[0]), ("largest", eligible[-1])):
                    display, terms = _display((src.txid.lower(), src.vout))
                    options.append(
                        _CpfpOption(
                            framing=framing,
                            value_sats=src.value_sats,
                            coin=src,
                            label_display=display,
                            match_terms=terms,
                        )
                    )
            options.append(plain_option)
            return _coin_ask("options", options=tuple(options), inbound=chosen)

        # 6. Fee bid — the ONLY chain call this flow makes, and only
        #    AFTER resolution (every refusal and ask above made zero).
        #    FAST default (a hurry request IS a stated urgency, the
        #    documented RBF-004 decision inherited); a stated rung rides
        #    the shared estimator ladder. No explicit-rate knob exists on
        #    this intent (ADR-0002: an internal reshuffle never rides the
        #    rate override).
        rung = FeeTarget(params.fee_target) if params.fee_target else FeeTarget.FAST
        try:
            estimate = fee_estimator.estimate(rung)
        except ChainError as exc:
            return {"error": "chain_unavailable", "detail": str(exc)}
        fee_rate = estimate.rate_centisat_vb  # TCK-FEE-004: floored in the estimator

        # 7. The parent picture — honest both-or-neither (tx/cpfp.py).
        #    A stuck inbound is normally FOREIGN (watch-only sees its
        #    output, never its inputs) and the store keeps no vsize for
        #    ANY row, so the ONE honest "known" shape is the transaction
        #    this app itself last broadcast whose id IS the funding tx
        #    (its fee and recorded ESTIMATE-vsize ride the flow's retained
        #    record — the RBF-002/RBF-004 estimate contract, never a
        #    measured or back-solved number).
        parent = StuckParent(chosen.txid.lower(), None, None)
        if (
            flow.state is TxFlowStatus.BROADCAST
            and flow.txid == chosen.txid.lower()
            and flow.confirmed is not None
        ):
            parent = StuckParent(
                chosen.txid.lower(),
                flow.confirmed.fee_sats,
                flow.confirmed.vsize,
            )

        # 8. Destination: ONE FRESH receive address (branch-0
        #    ``next_index``) — DERIVED here, the model cannot author it
        #    (the envelope carries no address, no outpoint, no number);
        #    allocated only AFTER the build (create_tx step 5's
        #    discipline, ADR-0009 self-heal).
        try:
            start_index = store.get_derivation(wallet_id, BRANCH_RECEIVE).next_index
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)
        try:
            destination = derive_addresses(parsed, BRANCH_RECEIVE, start_index, 1)[0]
            dest_script = bytes(address_to_scriptpubkey(destination.address).data)
        except Exception:  # noqa: BLE001 — containment: deriver/embit errors vary; re-raising could leak key material, and every path is value-free
            return {"error": "internal", "detail": "fresh receive derivation failed"}

        # 9. The plan — the pure CPFP-001 builder is the ONLY money math.
        merge_coin = option.coin
        try:
            plan = build_cpfp_child_plan(
                parent,
                chosen,
                dest_script,
                fee_rate,
                merge_coin=merge_coin,
            )
        except CpfpError as exc:
            if exc.reason is None:
                return {"error": "cpfp_plan_failed", "detail": _CPFP_PLAN_FAILED}
            # Fee-math refusals are VALUE-FREE per ADR-0012's CPFP
            # amendment (the builder never quotes sats; the reason rides a
            # machine key, never a detail string).
            return {
                "error": "cpfp_cannot_fund",
                "reason": str(exc.reason.value),
                "detail": _CPFP_CANNOT_FUND,
            }

        # 10. Build → allocate → stage (fail-closed; the flow record is
        #     touched ONLY after the full PSBT build and bookkeeping
        #     succeeded). ``payment_derivations`` labels the fresh output
        #     internal (TCK-HW-003 discipline), and ``self_payment_indices``
        #     on the record makes the sign-time independent re-derivation
        #     and the broadcast label-lineage hold for the child exactly
        #     as for any self-transfer plan.
        purpose = SCRIPT_PURPOSES[parsed.script_type]
        try:
            psbt, meta = build_unsigned_psbt(
                list(plan.inputs),
                [(dest_script, plan.output_sats)],
                None,
                None,
                account_key=parsed.hd_key,
                account_fingerprint=parsed.hd_key.my_fingerprint,
                account_path=(purpose + 2**31, MAINNET_COIN_TYPE + 2**31, 2**31),
                payment_derivations=[(BRANCH_RECEIVE, destination.index)],
            )
            psbt_base64 = psbt_to_base64(psbt)
        except PsbtError as exc:
            return {"error": "psbt_failed", "detail": str(exc)}
        try:
            store.upsert_batch(
                [
                    AddressRecord(
                        wallet_id=wallet_id,
                        branch=BRANCH_RECEIVE,
                        index=destination.index,
                        address=destination.address,
                        script_type=parsed.script_type,
                        status=ADDRESS_ALLOCATED,
                    )
                ]
            )
            store.allocate(wallet_id, BRANCH_RECEIVE, destination.index)
            store.bump_next_index(wallet_id, BRANCH_RECEIVE)
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)
        try:
            if flow.state is TxFlowStatus.BROADCAST:
                # The last broadcast's lifecycle is terminal (the parent
                # picture, if it was THAT transaction, is already baked
                # into the plan); the child starts its own full ride
                # through the SAME state machine (the RBF-004 _stage
                # precedent — ADR-0013 untouched).
                flow.reset()
            pending = flow.create(
                amount_sats=plan.output_sats,
                recipient=destination.address,
                fee_rate_centisat_vb=fee_rate,
                fee_sats=plan.fee_sats,
                psbt_base64=psbt_base64,
                inputs_count=len(plan.inputs),
                vsize=meta.vsize,
                fee_target=rung.value,
                change_sats=None,
                self_payment_indices=(destination.index,),
            )
        except FlowError:
            # Unreachable single-threaded after the posture guards; fail
            # closed with the pending card rather than double-staging.
            return _tx_pending_result(
                flow, seconds_since_last_block_fn=seconds_since_last_block_fn
            )

        merge_value = merge_coin.value_sats if merge_coin is not None else None
        display = {
            "cpfp": True,
            "self_mode": "cpfp",
            "cpfp_parent_txid": parent.txid,
            "cpfp_parent_fee_known": plan.parent_fee_known,
            "cpfp_package_fee_rate_centisat_vb": plan.package_fee_rate_centisat_vb,
            "cpfp_merged": plan.merged,
            "cpfp_inbound_sats": chosen.value_sats,
            "cpfp_merge_framing": None if merge_coin is None else option.framing,
            "cpfp_merge_value_sats": merge_value,
            # Destinations verbatim (renderer /details material — rides the
            # cpfp ``tx_pending`` re-show too, so the re-printed card is
            # the SAME card, never a bare send shape).
            "self_destinations": [
                {"address": destination.address, "amount_sats": plan.output_sats}
            ],
        }
        session.cpfp_pending = _CpfpPending(
            pending.tx_ref, parent.txid, chosen.txid.lower(), chosen.vout, display
        )
        eta = _eta_for(pending.fee_target, seconds_since_last_block_fn=seconds_since_last_block_fn)
        return {
            "tx_ref": pending.tx_ref,
            "amount_sats": pending.amount_sats,
            "recipient": pending.recipient,
            "fee_sats": pending.fee_sats,
            "fee_rate_centisat_vb": pending.fee_rate_centisat_vb,
            "fee_rate_display": format_sat_vb(pending.fee_rate_centisat_vb),
            # TCK-FEE-004: display-only min-relay narration, only when the
            # clamp actually raised the child's (initial) bid.
            **({"fee_floor_note": True} if estimate.clamped else {}),
            "vsize": pending.vsize,
            "change_sats": None,
            "inputs_count": pending.inputs_count,
            "fee_target": pending.fee_target,
            "expires_in_s": PENDING_TTL_S,
            "self_transfer": True,
            **display,
            # Display material ONLY (terminal/renderer — never a FACTS
            # block, never model transcript): every figure verbatim from
            # the builder's plan / the store's row.
            "self_parts": 1,
            "self_each_sats": plan.output_sats,
            "self_inputs_total_sats": chosen.value_sats + (merge_value or 0),
            "self_new_addresses": 1,
            # The recorded parent fee when this app broadcast the hurried
            # payment itself (the only honest-known shape, step 7); None
            # (foreign parent) renders the unknown-package-rate line.
            "cpfp_parent_fee_sats": parent.fee_sats,
            # A cpfp card never pitches the send speed-offer tail (the
            # urgency was stated by asking to hurry; re-bumping the child
            # is the existing bump_fee conversation's job).
            "fee_target_defaulted": False,
            "fee_requote": False,
            **({} if eta is None else eta),
        }

    def handler(envelope: Envelope) -> dict[str, object]:
        params = envelope.params
        if not isinstance(params, SelfTransferParams):
            return {"error": "internal", "detail": "self_transfer params shape mismatch"}

        # 0. First-scan gate (ADR-0022 decision 6) — same line, same
        #    position as create_tx: refusal BEFORE any network/store work.
        if scan_gate is not None and scan_gate.first_scan_incomplete:
            return {"error": "wallet_loading", "detail": WALLET_LOADING_REFUSAL}

        # 0.5 TCK-CPFP-002: the ``cpfp`` MODE branches to its own
        # conversation HERE — step 0.5, exactly where the RBF-004 guard
        # stood: BEFORE the split/consolidate branching (a cpfp envelope
        # would trip the consolidate ``below_size_sats`` assert), and the
        # conversation's own refusals make ZERO fee-estimator calls. The
        # value-free refusal survives for ONE genuinely unsupported shape:
        # a session-less direct wiring (legacy call sites) cannot carry
        # the conversation's dispatcher-owned state.
        if params.mode == "cpfp":
            if session is None:
                return {"error": "cpfp_unavailable", "detail": _CPFP_NOT_READY}
            return _cpfp(params)

        # 1. Pending guard: a staged plan is never silently replaced by
        #    another destructive plan (no self-transfer re-quote; see the
        #    docstring). Past-the-gate states stay refused (flow.create's
        #    own FlowError backstops, same shape as create_tx). A staged
        #    consolidation (TCK-CONS-001) re-shows WITH its plan-echo
        #    marker (the same card it was confirmed on, never a bare
        #    reshape the user did not read).
        if flow.state is TxFlowStatus.CREATED:
            result = _tx_pending_result(
                flow, seconds_since_last_block_fn=seconds_since_last_block_fn
            )
            if (
                session is not None
                and session.cons_pending is not None
                and flow.pending is not None
                and flow.pending.tx_ref == session.cons_pending.tx_ref
            ):
                result["cons_merge"] = True
            return result

        # 1.5 TCK-CONS-001: consume the consolidation conversation's ask
        #     BEFORE any work (the CPFP-002 step-2 discipline — every
        #     path after this point leaves nothing open). Only an ANSWERED
        #     ask (choice + picked, both code-stamped by the deterministic
        #     intercept — the model can neither set nor read them) steers
        #     the inputs; a FRESH envelope supersedes an unanswered ask.
        cons_picked: tuple[dict[str, object], ...] | None = None
        if session is not None and session.cons_ask is not None:
            prior_cons = session.cons_ask
            session.cons_ask = None
            if (
                params.mode != "split"
                and prior_cons.choice is not None
                and prior_cons.picked
            ):
                cons_picked = prior_cons.picked

        # 2. Fee bid: the estimator ladder (the ONLY chain call this flow
        #    makes — exactly like create_tx; no new chain surface). The bid
        #    is min-relay floored inside the estimator (TCK-FEE-004).
        target = FeeTarget(params.fee_target) if params.fee_target else FeeTarget.MEDIUM
        try:
            estimate = fee_estimator.estimate(target)
        except ChainError as exc:
            return {"error": "chain_unavailable", "detail": str(exc)}
        fee_rate = estimate.rate_centisat_vb

        # 3. UTXO snapshot with the lazy first scan (same path as
        #    create_tx step 4).
        try:
            if store.get_sync_state(wallet_id, wallet_scan.CURSOR_KEY) is None:
                try:
                    scan_fn()
                except (ChainError, wallet_scan.ScanError, WatchKeyError) as exc:
                    # detail is scrubbed by the chain/scan layers — safe verbatim.
                    return {"error": "chain_unavailable", "detail": str(exc)}
            utxos = store.get_utxos_for_wallet(wallet_id)
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)

        # 4. Fresh destination indices: branch-0 next_index read ONCE;
        #    DERIVE (pure) now, ALLOCATE only after the build succeeds.
        try:
            start_index = store.get_derivation(wallet_id, BRANCH_RECEIVE).next_index
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)

        split = params.mode == "split"
        parts = params.parts if split else 1
        assert parts is not None  # schema+layer-3 guarantee parts for split
        other_side_count: int | None = None  # consolidate cross-pool hint

        try:
            destinations = derive_addresses(parsed, BRANCH_RECEIVE, start_index, parts)
            dest_scripts = [bytes(address_to_scriptpubkey(d.address).data) for d in destinations]
        except Exception:  # noqa: BLE001 — containment: deriver/embit address encoding raises varied errors; re-raising could leak key material, and every path is value-free
            return {"error": "internal", "detail": "fresh receive derivation failed"}

        own_dust = dust_threshold(dest_scripts[0])

        if split:
            if not utxos:
                # Empty wallet: needed = fee for the changeless 1-in/N-out
                # shape + the N dust floors (user-facing UI, ADR-0012).
                needed = fee_sats_for(estimate_tx_vsize(1, dest_scripts, None), fee_rate) + parts * own_dust
                return {
                    "error": "insufficient_funds",
                    "needed_sats": needed,
                    "available_sats": 0,
                }
            # Engine policy (documented): the LARGEST single coin, canonical
            # (value_sats, txid, vout) descending — ties break the same way
            # every other selection here does.
            coin = max(
                utxos, key=lambda u: (u.value_sats, u.txid.lower(), u.vout)
            )
            input_rows = [coin]
            inputs_total = coin.value_sats
            vsize = estimate_tx_vsize(1, dest_scripts, None)
            fee_floor = fee_sats_for(vsize, fee_rate)
            each = (inputs_total - fee_floor) // parts
            if each < own_dust:
                # pre-build dust refusal, value-free (the friendly line):
                # N × each would each sit below the network's minimum.
                return {"error": "self_split_below_dust"}
            payment_values = [each] * parts
        else:
            if cons_picked is not None:
                # TCK-CONS-001: the conversation answered — EXACTLY the
                # picked coins (code-stamped on the dispatcher-owned ask,
                # never a model- or text-supplied reference), revalidated
                # against THIS fresh store read (the RBF-004/CPFP-002
                # mid-conversation recheck: a picked coin spent elsewhere
                # while an ask stood open is the honest gone answer, never
                # a silently swapped set). The envelope's threshold is the
                # engine's own max+1 — authority for the plan stays here.
                keys = {
                    (str(c["txid"]).lower(), int(str(c["vout"]))) for c in cons_picked
                }
                chosen = [
                    u for u in utxos if (u.txid.lower(), u.vout) in keys
                ]
                if len(chosen) != len(keys):
                    return {"error": "cons_coin_gone", "detail": _CONS_COIN_GONE}
            else:
                threshold = params.below_size_sats
                assert threshold is not None  # layer 2+3 guarantee for consolidate
                below = [
                    u for u in utxos if u.value_sats < threshold
                ]
                if not below:
                    return {"error": "self_nothing_below"}
                # Privacy pools (TCK-UTXO-002): partition by stored coin tags,
                # NEVER merge across sides. The larger-total pool wins; ties go
                # other-side first (the fixed pool order).
                try:
                    label_rows = store.get_coin_labels(wallet_id)
                except (StoreError, sqlite3.Error) as exc:
                    return _store_error(exc)
                kyc_outpoints = {
                    (row.txid, row.vout)
                    for row in label_rows
                    if coin_partition(row.tags)[0]
                }
                kyc_pool = [u for u in below if (u.txid, u.vout) in kyc_outpoints]
                other_pool = [u for u in below if (u.txid, u.vout) not in kyc_outpoints]
                other_total = sum(u.value_sats for u in other_pool)
                kyc_total = sum(u.value_sats for u in kyc_pool)
                if kyc_pool and other_pool:
                    chosen = other_pool if other_total >= kyc_total else kyc_pool
                    skipped = kyc_pool if other_total >= kyc_total else other_pool
                    other_side_count = len(skipped)
                else:
                    chosen = kyc_pool or other_pool
                    other_side_count = None
            if len(chosen) > MAX_SELF_TRANSFER_CONSOLIDATE_INPUTS:
                return {"error": "self_too_many_small"}
            chosen.sort(key=lambda u: (u.value_sats, u.txid.lower(), u.vout))
            input_rows = chosen
            inputs_total = sum(u.value_sats for u in chosen)
            vsize = estimate_tx_vsize(len(chosen), dest_scripts[:1], None)
            fee_sats = fee_sats_for(vsize, fee_rate)
            out_value = inputs_total - fee_sats
            if out_value < own_dust:
                # User-facing UI figures (ADR-0012), never a log-bound detail.
                return {
                    "error": "insufficient_funds",
                    "needed_sats": fee_sats + own_dust,
                    "available_sats": inputs_total,
                }
            payment_values = [out_value]

        # 5. Inputs → PSBT sources (contained value-free on any mapping gap).
        inputs, err = _inputs_for(input_rows)
        if err is not None:
            return err

        # 6. Build via the SAME tx engine as create_tx. payment_derivations
        #    labels the fresh own outputs with their receive coordinates so
        #    the signing device renders them as internal, not external
        #    destinations (TCK-HW-003 discipline generalized; the builder
        #    fail-closes if any claimed derivation does not match its
        #    script). No change output: the plan's residue folds into fee.
        purpose = SCRIPT_PURPOSES[parsed.script_type]
        payment_derivations = [(BRANCH_RECEIVE, d.index) for d in destinations]
        try:
            psbt, meta = build_unsigned_psbt(
                inputs,
                list(zip(dest_scripts, payment_values, strict=True)),
                None,
                None,
                account_key=parsed.hd_key,
                account_fingerprint=parsed.hd_key.my_fingerprint,
                account_path=(purpose + 2**31, MAINNET_COIN_TYPE + 2**31, 2**31),
                payment_derivations=payment_derivations,
            )
            psbt_base64 = psbt_to_base64(psbt)
        except PsbtError as exc:
            return {"error": "psbt_failed", "detail": str(exc)}

        # 7. Allocation bookkeeping — strictly AFTER the build, strictly
        #    BEFORE staging (create_tx's discipline; ADR-0009 self-heal).
        try:
            store.upsert_batch(
                [
                    AddressRecord(
                        wallet_id=wallet_id,
                        branch=BRANCH_RECEIVE,
                        index=d.index,
                        address=d.address,
                        script_type=parsed.script_type,
                        status=ADDRESS_ALLOCATED,
                    )
                    for d in destinations
                ]
            )
            for d in destinations:
                store.allocate(wallet_id, BRANCH_RECEIVE, d.index)
                store.bump_next_index(wallet_id, BRANCH_RECEIVE)
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)

        # 8. Stage (flow owns tx_ref identity) — self_payment_indices marks
        #    the record; the sign-time intent builder re-derives every
        #    payment script from these indices (independent re-proof).
        amount_total = sum(payment_values)
        try:
            pending = flow.create(
                amount_sats=amount_total,
                recipient=destinations[0].address,
                fee_rate_centisat_vb=fee_rate,
                fee_sats=meta.expected_fee_sats,
                psbt_base64=psbt_base64,
                inputs_count=len(inputs),
                vsize=meta.vsize,
                fee_target=target.value,
                change_sats=None,
                self_payment_indices=tuple(d.index for d in destinations),
            )
        except FlowError:
            # Unreachable single-threaded after the pending guard; fail
            # closed with the pending card rather than double-staging.
            return _tx_pending_result(
                flow, seconds_since_last_block_fn=seconds_since_last_block_fn
            )

        eta = _eta_for(pending.fee_target, seconds_since_last_block_fn=seconds_since_last_block_fn)
        result: dict[str, object] = {
            "tx_ref": pending.tx_ref,
            "amount_sats": pending.amount_sats,
            "recipient": pending.recipient,
            "fee_sats": pending.fee_sats,
            "fee_rate_centisat_vb": pending.fee_rate_centisat_vb,
            "fee_rate_display": format_sat_vb(pending.fee_rate_centisat_vb),
            # TCK-FEE-004: display-only min-relay narration (see create_tx);
            # only when the clamp actually raised a plan rung.
            **({"fee_floor_note": True} if estimate.clamped else {}),
            "vsize": pending.vsize,
            "change_sats": None,
            "inputs_count": pending.inputs_count,
            "fee_target": pending.fee_target,
            "expires_in_s": PENDING_TTL_S,
            # Self-transfer plan display facts (terminal/renderer material
            # ONLY — never a FACTS block, never the model transcript):
            # the card names the SHAPE of the plan; every value verbatim
            # from the handler's own computation.
            "self_transfer": True,
            "self_mode": "split" if split else "consolidate",
            "self_parts": len(payment_values),
            "self_each_sats": payment_values[0],
            "self_inputs_total_sats": inputs_total,
            "self_new_addresses": len(destinations),
            # Destinations verbatim (renderer /details material, TCK-UX-013):
            # each fresh receive address paired with its output amount.
            "self_destinations": [
                {"address": d.address, "amount_sats": amt}
                for d, amt in zip(destinations, payment_values, strict=True)
            ],
            # fee_target_defaulted is DELIBERATELY absent: a self-transfer
            # card never pitches the one-shot speed offer (no re-quote path
            # exists for a plan; a speed preference must be stated up front
            # or reached by cancel + re-ask).
            **({} if eta is None else eta),
        }
        if other_side_count:
            result["self_other_side_count"] = other_side_count
        if cons_picked is not None:
            # TCK-CONS-001: the plan-echo marker (the card renderer prints
            # the pinned "Merge N UTXOs to create one new UTXO of X sats"
            # line from this result's OWN inputs_count/amount_sats — engine
            # totals, verbatim) and the broadcast annotation record (a
            # successful broadcast tags the plan's outputs
            # ``consolidation`` + notes "consolidated from N outputs";
            # §1.3 union inheritance already ran for every self-transfer).
            result["cons_merge"] = True
            if session is not None:
                session.cons_pending = _ConsPending(pending.tx_ref)
        return result

    return handler


def _make_bump_fee_handler(
    store: Store,
    wallet_id: int,
    parsed: ParsedKey,
    flow: TxFlow,
    fee_estimator: FeeEstimator,
    scan_fn: Callable[[], object],
    session: SendSession,
    *,
    seconds_since_last_block_fn: Callable[[], int | None] | None = None,
    scan_gate: StartupScan | None = None,
) -> Handler:
    """Create the ``bump_fee`` handler: the BIP-125 replacement conversation.

    TCK-RBF-004 — consumes the landed pieces, rebuilds nothing:
    :func:`_resolve_in_flight_outgoing` (the pinned RBF-005 resolver — the
    ONLY target resolution), :func:`~localwallet.tx.replacement.
    build_replacement_plan` (the pure RBF-002 builder — the ONLY money
    math; ``estimate_tx_vsize`` semantics carried through the recorded
    original, never re-measured), and the RBF-001 sanctioned
    :meth:`~localwallet.store.Store.record_replacement` writer (called by
    the BROADCAST handler, commit-only-on-success — this handler never
    touches lineage). The staged replacement rides the SAME
    create→confirm→sign→broadcast state machine: the dual-key gate and the
    device handoff are untouched (the record is a plain
    :class:`~localwallet.tx.flow.PendingTx`; ``session.bump_pending`` is
    the code-side lineage marker the confirm/sign/broadcast handlers never
    read).

    Pipeline (every refusal BEFORE any staging; nothing is built unless
    the whole plan succeeds):

    0. First-scan gate, identical posture to ``create_tx`` (value
       movement waits for the wallet load). Flow guards: ``CONFIRMED``/
       ``SIGNED`` → honest busy refusal; ``CREATED`` → refused UNLESS this
       is a re-bump of the currently staged replacement (the pending bump
       is replaced through the same commit-only-on-success swap the
       re-quote uses — never silently coexisting with a second plan).
    1. Target resolution on a FRESH store read (this is ALSO the
       mid-conversation recheck: if the original confirmed while an ask
       stood open, it has left the in-flight set HERE and the honest
       already-confirmed answer replaces the plan — a replacement of a
       confirmed transaction is never staged):
       - 64-hex txid → resolved directly when in flight;
       - a non-txid reference with exactly ONE in-flight transaction →
         assume-and-name-it (the resolver's single semantics, the txid
         quoted verbatim);
       - several in flight and nothing naming one → the INDEXED CHOICE ASK
         (never guess);
       - nothing in flight → the honest empty refusal.
    2. Fee bid: the envelope's knobs exactly like ``create_tx`` — an
       explicit ``fee_rate_sat_vb`` scales ×100 (the ONE edge that sees
       centisat); a ``fee_target`` rung rides the shared estimator; NO
       stated knob defaults to FAST (documented RBF-004 decision: a bump
       IS a stated urgency — the MEDIUM default belongs to ordinary
       sends). The estimator is called only after the target resolved,
       so every early refusal makes ZERO chain calls.
    3. Decomposition of the recorded original (the app-layer job RBF-002
       documented): from the flow's retained record (terminal BROADCAST —
       the transaction this app last broadcast) or, for a re-bump, from
       the carried :class:`_BumpOriginal`. A txid the app cannot rebuild
       is refused honestly — the store keeps no inputs/outputs and no raw
       transaction is fetched (this module never touches the chain).
       Multi-output plans (a self-transfer SPLIT) are refused (their
       bump needs the plan-aware revalidation path — adjacent work,
       ledgered); a single-output reshuffle rides the ordinary shape.
    4. Funding (BIP-125 rule 2 — the replacement may only ADD CONFIRMED
       coins): CHANGE FIRST — the plan builder is tried with no added
       coin; its change paths (trim/fold) are the change paying. When the
       change alone cannot reach the floor, a chooser over the wallet's
       CONFIRMED coins ONLY opens (unconfirmed coins — including the
       original's own pending change — are never offered and never
       silently used; the original's inputs are excluded). Framing:
       smallest / mid / largest, the user's coin labels printed verbatim
       for the terminal — labels never reach the model (handler results
       are not prompt context, and answers route through the
       deterministic intercept). The ask is probed with the LARGEST
       confirmed coin FIRST: when even it cannot reach the floor, the
       honest refusal carries the sanctioned floor number instead of
       asking a question nothing can answer. An explicit ``funding_ref``
       (framing word, offered number, or coin address) skips the ask.
       The floor refusal's sats figures ride STRUCTURED keys (the
       ``insufficient_funds`` / ADR-0012 §7 precedent), never a log-bound
       detail string.
    5. Staging: a FRESH change index (derive → build → allocate → stage —
       ``create_tx`` step 5's exact discipline, so the sign-time
       independent re-derivation re-check holds for the replacement too),
       the SAME pure PSBT builder, then the flow swap (a terminal BROADCAST
       is reset to IDLE ONLY when the replacement is already fully built;
       a CREATED re-bump replaces directly). ``session.bump_pending``
       carries the lineage to the broadcast handler.

    Narration contract: the result carries the plan card's verbatim fields
    (``replaces``/``old_fee_sats``/``fee_delta_sats`` from the builder's
    output); the renderer prints the fixed hedge + fee-delta rows.
    """

    def _floor_refusal(exc: RbfFloorError) -> dict[str, object]:
        """The sanctioned floor-number refusal: the builder's own
        ``floor_sats``/``max_payable_sats``, structured (never a detail
        string carrying values)."""
        return {
            "error": "bump_floor_unreachable",
            "reason": str(exc.reason.value),
            "floor_sats": exc.floor_sats,
            "max_payable_sats": exc.max_payable_sats,
            "detail": "the replacement cannot be funded at this rate",
        }

    def _decompose(rec: PendingTx) -> _BumpOriginal | None:
        """The app-layer decomposition RBF-002 documented: the flow's own
        staged PSBT (dispatcher-owned bytes, built by this app at create
        time) parsed into the pure builder's :class:`OriginalTx` inputs —
        prevout coins with their wallet derivation coordinates, the
        payment outputs verbatim in order, and the recorded change pair.
        Any gap (no witness UTXO, no derivation, a change scalar that
        disagrees with the PSBT) is a fail-closed ``None`` — a corrupt
        record never feeds money math. The vsize is the RECORDED ESTIMATE
        carried through (the RBF-002 contract trap: never treated as
        measured, never re-measured). Output-script SHAPE is not inspected
        here — the pure builder's conservation check plus its vsize
        re-derivation (``tx.replacement``, ``estimate_tx_vsize``) is the
        shape authority, so a non-P2WPKH reshuffle fails closed there,
        not in this decomposition."""
        try:
            psbt = PSBT.parse(base64.b64decode(rec.psbt_base64))
        except Exception:  # noqa: BLE001 — containment: embit parse errors vary; the record is ours, a failure here is value-free by construction
            return None
        if len(psbt.tx.vin) != len(psbt.inputs):
            return None
        coins: list[PsbtInputSource] = []
        for vin, scope in zip(psbt.tx.vin, psbt.inputs):
            if scope.witness_utxo is None or not scope.bip32_derivations:
                return None
            path = next(iter(scope.bip32_derivations.values())).derivation
            if len(path) < 2:
                return None
            branch, index = int(path[-2]), int(path[-1])
            if branch not in (0, 1) or index < 0:
                return None
            coins.append(
                PsbtInputSource(
                    txid=bytes(reversed(vin.txid)).hex(),
                    vout=vin.vout,
                    value_sats=scope.witness_utxo.value,
                    script_pubkey=bytes(scope.witness_utxo.script_pubkey.data),
                    branch=branch,
                    index=index,
                )
            )
        outs = [(bytes(o.script_pubkey.data), o.value) for o in psbt.tx.vout]
        if rec.change_sats is not None:
            if len(outs) < 2 or outs[-1][1] != rec.change_sats:
                return None
            change_script, change_sats = outs[-1]
            recipients = tuple(outs[:-1])
        else:
            change_script, change_sats = None, None
            recipients = tuple(outs)
        if not coins or not recipients:
            return None
        return _BumpOriginal(
            recipients=recipients,
            change_script=change_script,
            change_sats=change_sats,
            inputs=tuple(coins),
            fee_sats=rec.fee_sats,
            vsize=rec.vsize,
        )

    def _confirmed_candidates(
        original: _BumpOriginal,
    ) -> tuple[list[_BumpFundingOption], dict[str, object] | None]:
        """The funding pool: CONFIRMED coins only (BIP-125 rule 2 — an
        added input must be confirmed; the original's own pending change
        is unconfirmed and therefore never in this set), minus every
        outpoint the original already spends. Unusable rows (no address,
        no derivation record, foreign wallet) are DROPPED — a coin that
        cannot be built into the replacement is never offered (the
        chooser only offers what it can fund)."""
        try:
            if store.get_sync_state(wallet_id, wallet_scan.CURSOR_KEY) is None:
                try:
                    scan_fn()
                except (ChainError, wallet_scan.ScanError, WatchKeyError) as exc:
                    # detail is scrubbed by the chain/scan layers — safe verbatim.
                    return [], {"error": "chain_unavailable", "detail": str(exc)}
            utxo_rows = store.get_utxos_for_wallet(wallet_id)
            label_rows = store.get_coin_labels(wallet_id)
            by_address = {
                r.address: r
                for r in (
                    store.get_by_address(u.address)
                    for u in utxo_rows
                    if u.address and u.confirmed == 1
                )
                if r is not None
            }
        except (StoreError, sqlite3.Error) as exc:
            return [], _store_error(exc)
        spent = {(c.txid.lower(), c.vout) for c in original.inputs}
        labels = {
            (row.txid.lower(), row.vout): row for row in label_rows
        }
        coins: list[_BumpFundingOption] = []
        for u in utxo_rows:
            if u.confirmed != 1 or not u.address:
                continue
            key = (u.txid.lower(), u.vout)
            if key in spent:
                continue
            record = by_address.get(u.address)
            if record is None or record.wallet_id != wallet_id or record.branch not in (0, 1):
                continue
            try:
                script = bytes(address_to_scriptpubkey(u.address).data)
            except Exception:  # noqa: BLE001,S112 — containment: a junk address row simply leaves the offerable set (value-free); the chooser only offers what it can fund, silently SKIPPING a broken row is the documented policy
                continue
            label = labels.get(key)
            display: str | None = None
            terms: tuple[str, ...] = ()
            if label is not None:
                bits = [*label.tags] + ([label.note] if label.note else [])
                if bits:
                    display = ", ".join(f"'{bit}'" for bit in bits)
                    terms = tuple(
                        word
                        for bit in bits
                        for word in bit.lower().split()
                        if word
                    )
            coins.append(
                _BumpFundingOption(
                    framing="",
                    value_sats=u.value_sats,
                    coin=PsbtInputSource(
                        txid=u.txid,
                        vout=u.vout,
                        value_sats=u.value_sats,
                        script_pubkey=script,
                        branch=record.branch,
                        index=record.index,
                    ),
                    address=u.address,
                    label_display=display,
                    match_terms=terms,
                )
            )
        coins.sort(key=lambda o: (o.value_sats, o.coin.txid.lower(), o.coin.vout))
        return coins, None

    def _resolve_funding_ref(
        ref: str, coins: list[_BumpFundingOption], ask: _BumpAsk | None
    ) -> _BumpFundingOption | None:
        """Model-quoted ``funding_ref`` → a candidate: the offered number
        from the ask that opened (the deterministic intercept quotes it),
        a framing word (self-describing over the full confirmed set), or
        an exact coin address. Anything else is ``None`` (the caller
        re-asks — never a guessed coin)."""
        text = ref.strip()
        lowered = text.lower()
        if ask is not None and ask.kind == "funding" and text.isdigit():
            index = int(text)
            if 1 <= index <= len(ask.options):
                return ask.options[index - 1]
            return None
        # Framing word: THE shared vocab (:data:`_BUMP_FRAMING_SYNONYMS`,
        # also consulted by the deterministic answer intercept), reduced to
        # a position over the full confirmed set — the laziest single
        # source of truth for the chooser's synonyms.
        for framing, synonyms in _BUMP_FRAMING_SYNONYMS.items():
            if lowered in synonyms:
                if framing == "mid":
                    return coins[len(coins) // 2]
                if framing == "largest":
                    return coins[-1]
                return coins[0]  # "smallest"
        for option in coins:
            if option.address == text:
                return option
        return None

    def _stage(
        original: _BumpOriginal,
        rec: PendingTx,
        plan: ReplacementPlan,
        old_txid: str,
        rung: FeeTarget | None,
        floor_raised: bool,
    ) -> dict[str, object]:
        """Build + stage the replacement (fail-closed; the flow record is
        touched ONLY after the full PSBT build and address bookkeeping
        succeeded — commit-only-on-success through and through). The
        plan's coins are already :class:`PsbtInputSource` objects (the
        decomposition's or a candidate's), so the builder consumes them
        unchanged; the change rides a FRESH branch-1 index like every
        ordinary send's (the sign-time independent re-derivation then
        holds for the replacement exactly as for a create)."""
        rate_c = plan.fee_rate_centisat_vb
        change_address: str | None = None
        change_index: int | None = None
        try:
            if plan.change_sats is not None:
                change_index = store.get_derivation(wallet_id, BRANCH_CHANGE).next_index
                change_address = BranchDeriver(parsed, BRANCH_CHANGE).address(change_index)
            build_recipients: list[tuple[bytes, int]] = list(plan.outputs)
            if plan.change_sats is not None:
                build_recipients = build_recipients[:-1]
            purpose = SCRIPT_PURPOSES[parsed.script_type]
            psbt, meta = build_unsigned_psbt(
                list(plan.inputs),
                build_recipients,
                change_address,
                plan.change_sats,
                account_key=parsed.hd_key,
                account_fingerprint=parsed.hd_key.my_fingerprint,
                account_path=(purpose + 2**31, MAINNET_COIN_TYPE + 2**31, 2**31),
                change_index=change_index,
            )
            psbt_base64 = psbt_to_base64(psbt)
        except PsbtError as exc:
            return {"error": "psbt_failed", "detail": str(exc)}
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)
        except Exception:  # noqa: BLE001 — containment: embit/derivation errors vary; re-raising could leak record material
            return {"error": "internal", "detail": "replacement psbt could not be built"}

        try:
            if change_address is not None and change_index is not None:
                store.upsert_batch(
                    [
                        AddressRecord(
                            wallet_id=wallet_id,
                            branch=BRANCH_CHANGE,
                            index=change_index,
                            address=change_address,
                            script_type=parsed.script_type,
                            status=ADDRESS_ALLOCATED,
                        )
                    ]
                )
                store.allocate(wallet_id, BRANCH_CHANGE, change_index)
                store.bump_next_index(wallet_id, BRANCH_CHANGE)
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)

        try:
            if flow.state is TxFlowStatus.BROADCAST:
                # The ORIGINAL's lifecycle is complete (terminal); the
                # replacement starts its own full ride through the SAME
                # state machine (ADR-0013 untouched — the reset only
                # clears the finished flow's records, which this handler
                # has already decomposed).
                flow.reset()
            pending = flow.create(
                amount_sats=rec.amount_sats,
                recipient=rec.recipient,
                fee_rate_centisat_vb=rate_c,
                fee_sats=plan.fee_sats,
                psbt_base64=psbt_base64,
                inputs_count=len(plan.inputs),
                vsize=meta.vsize,
                fee_target=rung.value if rung is not None else None,
                change_sats=plan.change_sats,
            )
        except FlowError:
            # Lost-the-race backstop (single-threaded unreachable after the
            # guards): fail closed, the staged/recorded flow untouched.
            return {"error": "bump_flow_busy", "detail": _BUMP_FLOW_BUSY}

        session.bump_pending = _BumpPending(pending.tx_ref, old_txid, original)
        session.bump_ask = None
        eta = _eta_for(pending.fee_target, seconds_since_last_block_fn=seconds_since_last_block_fn)
        return {
            "bump": True,
            "replaces": old_txid,
            "bump_mode": plan.mode.value,
            "tx_ref": pending.tx_ref,
            "amount_sats": pending.amount_sats,
            "recipient": pending.recipient,
            "fee_sats": pending.fee_sats,
            # Plan-card delta fields, verbatim builder output (the renderer
            # prints them; the model never sees results at all).
            "old_fee_sats": original.fee_sats,
            "fee_delta_sats": plan.fee_sats - original.fee_sats,
            "fee_rate_centisat_vb": pending.fee_rate_centisat_vb,
            "fee_rate_display": format_sat_vb(pending.fee_rate_centisat_vb),
            # TCK-FEE-004: display-only min-relay narration for the bump's
            # INITIAL bid, only when the clamp raised it (the BIP-125
            # incremental floor the builder applies below is a DISTINCT
            # rule, never narrated as this one).
            **({"fee_floor_note": True} if floor_raised else {}),
            "vsize": pending.vsize,
            "change_sats": pending.change_sats,
            "inputs_count": pending.inputs_count,
            "usd_cents": None,
            "rate_stale": False,
            "rate_age_s": None,
            "rate_fetched_at": None,
            "fee_target": pending.fee_target,
            # A bump card never pitches the send speed-offer tail (the
            # urgency was stated by asking for the bump); the re-bump
            # route is the deterministic faster/slower intercept.
            "fee_target_defaulted": False,
            "fee_requote": False,
            "expires_in_s": PENDING_TTL_S,
            **({} if eta is None else eta),
        }

    def handler(envelope: Envelope) -> dict[str, object]:
        params = envelope.params
        if not isinstance(params, BumpFeeParams):
            return {"error": "internal", "detail": "bump_fee params shape mismatch"}

        # A fresh bump_fee envelope SUPERSEDES any open ask: the prior ask
        # is captured for THIS call's number resolution and cleared here
        # (every refusal path below then honestly leaves nothing open; a
        # new ask is installed only by this call's own ask branch). The
        # intercept dispatch always arrives with the ask open — digits
        # resolve against ``prior_ask``.
        prior_ask = session.bump_ask
        session.bump_ask = None

        # 0. First-scan gate (ADR-0022 decision 6) — same line, same
        #    position as create_tx: refusal BEFORE any network/store work.
        if scan_gate is not None and scan_gate.first_scan_incomplete:
            return {"error": "wallet_loading", "detail": WALLET_LOADING_REFUSAL}
        # 0.5 Flow posture: past-the-gate lifecycle states are busy (never
        #     abandon a committed plan); a pending plan is refused UNLESS
        #     this is a re-bump of the staged replacement itself (whose
        #     pending this handler may replace, commit-only-on-success).
        if flow.state in (TxFlowStatus.CONFIRMED, TxFlowStatus.SIGNED):
            return {"error": "bump_flow_busy", "detail": _BUMP_FLOW_BUSY}
        rebump = (
            flow.state is TxFlowStatus.CREATED
            and session.bump_pending is not None
            and flow.pending is not None
            and flow.pending.tx_ref == session.bump_pending.tx_ref
        )
        if flow.state is TxFlowStatus.CREATED and not rebump:
            return _tx_pending_result(
                flow, seconds_since_last_block_fn=seconds_since_last_block_fn
            )

        # 1. Target resolution — the pinned RBF-005 resolver on a FRESH
        #    store read (the mid-conversation confirmation recheck IS this
        #    step: a confirmed/retired original has left the in-flight set
        #    and can never be staged against).
        try:
            rows = store.get_txs_for_wallet(wallet_id)
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)
        entries = _resolve_in_flight_outgoing(rows)
        by_txid = {str(entry["txid"]): entry for entry in entries}
        target = params.target.strip()
        old_txid: str | None = None
        if _BUMP_HEX64_RE.fullmatch(target):
            if target in by_txid:
                old_txid = target
            else:
                row = next((r for r in rows if r.txid == target), None)
                if row is not None and row.height is not None:
                    return {"error": "bump_already_confirmed", "detail": _BUMP_ALREADY_CONFIRMED}
                if row is not None and target in superseded_states(rows):
                    # Lineage already settled (replaced: its bump confirmed
                    # — or this targeted the bump and the original won).
                    # Either way there is nothing further to bump; the
                    # lineage-aware tx_status copy narrates the detail.
                    return {"error": "bump_already_confirmed", "detail": _BUMP_ALREADY_SETTLED}
                return {"error": "bump_nothing_in_flight", "detail": _BUMP_NOTHING_IN_FLIGHT}
        elif len(entries) == 1:
            # The resolver's assume-and-name-it semantics: the reference
            # cannot be checked against a txid, but there is exactly one
            # in-flight transaction — proceed with IT, quoted verbatim.
            old_txid = str(entries[0]["txid"])
        elif len(entries) >= 2:
            # Never guess: the indexed choice ask (entries verbatim from
            # the resolver — amounts may be honest NULLs, never invented).
            session.bump_ask = _BumpAsk(
                kind="target",
                old_txid="",
                entries=tuple(entries),
                fee_target=params.fee_target,
                fee_rate_sat_vb=params.fee_rate_sat_vb,
            )
            return {
                "bump": True,
                "ask": "target",
                "options": [dict(entry) for entry in entries],
            }
        else:
            return {"error": "bump_nothing_in_flight", "detail": _BUMP_NOTHING_IN_FLIGHT}
        assert old_txid is not None  # every branch above resolves or returns

        # 1.5 A pending plan is replaced by a bump ONLY when that bump is
        #     the SAME lineage (the staged replacement re-bumping its own
        #     original). Any other target while a plan pends is the busy
        #     refusal — a different bump never silently replaces it.
        if (
            flow.state is TxFlowStatus.CREATED
            and session.bump_pending is not None
            and old_txid != session.bump_pending.old_txid
        ):
            return _tx_pending_result(
                flow, seconds_since_last_block_fn=seconds_since_last_block_fn
            )

        # 2. Fee bid (only AFTER the target resolved — refusals above made
        #    zero chain calls). An explicit user-quoted rate is taken
        #    verbatim (×100 at this edge, the FEE-003 precedent, no rung
        #    recorded) but MIN-RELAY-FLOORED (TCK-FEE-004: MAX(rate, floor)
        #    with honest narration on a raise); a stated rung rides the
        #    shared estimator (already floored there); nothing stated
        #    defaults to FAST (asking for a bump IS a stated urgency — see
        #    the docstring). This is the bump's INITIAL bid only — the
        #    DISTINCT BIP-125 incremental-relay floor (a replacement must
        #    out-pay its original by an increment of its own size) remains
        #    tx/replacement.py's alone, applied inside build_replacement_plan
        #    below and untouched here.
        if params.fee_rate_sat_vb is not None:
            rate_c, floor_raised = fee_estimator.clamp_to_min_relay_floor(
                params.fee_rate_sat_vb * 100
            )
            rung: FeeTarget | None = None
        else:
            rung = FeeTarget(params.fee_target) if params.fee_target else FeeTarget.FAST
            try:
                estimate = fee_estimator.estimate(rung)
            except ChainError as exc:
                return {"error": "chain_unavailable", "detail": str(exc)}
            rate_c = estimate.rate_centisat_vb
            floor_raised = estimate.clamped

        # 3. Decomposition of the recorded original (from the carried
        #    re-bump record, else the flow's retained broadcast record —
        #    the ONLY source this app has: the store keeps no inputs and
        #    no raw transaction is fetched).
        rec = flow.pending if rebump and flow.pending is not None else flow.confirmed
        if rec is None:
            return {"error": "bump_unrecorded", "detail": _BUMP_UNRECORDED}
        if rebump:
            assert session.bump_pending is not None
            original = session.bump_pending.original
        else:
            if flow.state is not TxFlowStatus.BROADCAST or flow.txid != old_txid:
                return {"error": "bump_unrecorded", "detail": _BUMP_UNRECORDED}
            decomposed = _decompose(rec)
            if decomposed is None:
                return {"error": "bump_unrecorded", "detail": _BUMP_UNRECORDED}
            original = decomposed
        if len(original.recipients) > 1:
            return {"error": "bump_multi_output", "detail": _BUMP_MULTI_OUTPUT}

        oo = original.as_original_tx()

        # 4. Funding — CHANGE FIRST (the user rule): the change-only plan
        #    (trim/fold shapes) is tried before any coin is considered.
        try:
            plan = build_replacement_plan(oo, rate_c)
        except RbfFloorError as change_exc:
            coins, err = _confirmed_candidates(original)
            if err is not None:
                return err
            if not coins:
                return _floor_refusal(change_exc)
            # Probe the BEST candidate first: when even the largest
            # confirmed coin cannot reach the floor, refuse with the
            # sanctioned number instead of asking a question nothing can
            # answer (never trap the user into a doomed chooser).
            try:
                build_replacement_plan(oo, rate_c, funding_coin=coins[-1].coin)
            except RbfFloorError as best_exc:
                return _floor_refusal(best_exc)
            except ReplacementError:
                return {"error": "bump_plan_failed", "detail": _BUMP_PLAN_FAILED}
            chosen: _BumpFundingOption | None = None
            if params.funding_ref is not None:
                chosen = _resolve_funding_ref(params.funding_ref, coins, prior_ask)
                if chosen is None:
                    return {
                        "error": "bump_funding_ref",
                        "detail": _BUMP_ASK_REF_UNRESOLVED,
                    }
            if chosen is None:
                options = tuple(_bump_framed_options(coins))
                session.bump_ask = _BumpAsk(
                    kind="funding",
                    old_txid=old_txid,
                    options=options,
                    fee_target=params.fee_target,
                    fee_rate_sat_vb=params.fee_rate_sat_vb,
                )
                return {
                    "bump": True,
                    "ask": "funding",
                    "replaces": old_txid,
                    "options": [
                        {
                            "index": i + 1,
                            "framing": option.framing,
                            "value_sats": option.value_sats,
                            "label": option.label_display,
                        }
                        for i, option in enumerate(options)
                    ],
                }
            try:
                plan = build_replacement_plan(oo, rate_c, funding_coin=chosen.coin)
            except RbfFloorError as coin_exc:
                return _floor_refusal(coin_exc)
            except ReplacementError:
                return {"error": "bump_plan_failed", "detail": _BUMP_PLAN_FAILED}
        except ReplacementError:
            # The recorded original does not add up (fail-closed inside
            # the pure builder); the value-free line says what it means —
            # the builder's own messages never reach the UI (they could
            # carry record scalars on paths the layer never expects).
            return {"error": "bump_plan_failed", "detail": _BUMP_PLAN_FAILED}

        # 5. Build + stage (the full TxFlow ride continues from CREATED —
        #    confirm/sign/broadcast handlers unchanged, dual-key intact).
        return _stage(original, rec, plan, old_txid, rung, floor_raised)

    return handler


def _make_confirm_tx_handler(flow: TxFlow, session: SendSession) -> Handler:
    """Create the ``confirm_tx`` handler: CREATED → CONFIRMED under the dual key.
    The flow transition requires BOTH keys (ADR-0013): the model's
    ``confirm_tx`` envelope with a ``tx_ref`` matching the pending
    transaction (this handler), AND the CONFIRM classification of the
    current user turn's utterance — read from :class:`SendSession`,
    which the REPL fills from the deterministic
    :class:`~localwallet.tx.flow.ConfirmGate` BEFORE the model runs.
    Any :class:`~localwallet.tx.flow.FlowError` (no pending tx, expiry,
    gate not satisfied, ``tx_ref`` mismatch) is refused with
    ``{"error": "confirm_refused", "detail": <value-free flow message>}``
    — the refusal message IS the user-facing UX. On success the handler
    returns the confirmed record incl. the unsigned PSBT (the Phase 3
    signing handoff); the narration prints no PSBT payload.
    """

    def handler(envelope: Envelope) -> dict[str, object]:
        params = envelope.params
        if not isinstance(params, ConfirmTxParams):
            return {"error": "internal", "detail": "confirm_tx params shape mismatch"}
        try:
            pending = flow.confirm(params.tx_ref, gate_decision=session.gate_decision)
        except FlowError as exc:
            return {"error": "confirm_refused", "detail": str(exc)}
        return {
            "status": "confirmed",
            "tx_ref": pending.tx_ref,
            "psbt_base64": pending.psbt_base64,
            "message": "ready for signing (Phase 3)",
        }

    return handler


def _intended_from_confirmed(
    confirmed: PendingTx, *, parsed: ParsedKey, change_index: int | None
) -> IntendedTx:
    """Build the :class:`IntendedTx` from the flow's APPROVED record.

    ``parsed`` is the wallet's account key (threaded from the sign handler)
    and ``change_index`` the branch-1 child index the change output used
    (recovered from the store at sign time; ``None`` when the record has no
    change) — together they let R3 re-derive the change script
    independently instead of trusting the staged PSBT alone.

    The intent must describe EXACTLY the transaction the user confirmed
    (ADR-0013 amendment / ``tx/revalidate.py`` contract). Every field
    comes from the dispatcher-owned ``PendingTx`` the flow retained
    through ``CONFIRMED`` — never from model or user text:

    - ``expected_recipient_outputs`` — the recipient ``(script, value)``
      pair: the script re-derived from the approved record's recipient
      address (layer 3 proved it a mainnet P2WPKH bech32 string at
      create time), the value from ``amount_sats``;
    - ``expected_change`` — ``(script, value)`` LAST when the record
      carries change: the script read back from the approved record's own
      staged PSBT output list (the change ADDRESS is not stored on the
      record — the built PSBT is the authority; the value must equal
      ``change_sats``), ``None`` otherwise; R3 additionally re-derives the
      branch-1 change address from ``parsed`` at ``change_index`` and
      asserts it equals the PSBT's change script (independent
      re-derivation — a drift between what we staged and what the deriver
      would produce fails closed before anything signs);
    - ``expected_inputs_count`` / ``expected_fee_sats`` — verbatim from
      the record (the engine computed them at build time);
    - ``expected_sequence`` — :data:`SEQUENCE_RBF_ENABLED`, the policy
      value :func:`~localwallet.tx.psbt.build_unsigned_psbt` enforces on
      every input (ADR-0012);
    - ``expected_vsize_max`` — the record's build-time vsize estimate
      PLUS ONE (the signed tx may be a vB shorter than the max-witness
      estimate, never meaningfully longer — revalidate check 10);
    - ``tx_ref`` — the record's reference (audit symmetry).

    The staged PSBT is dispatcher-owned state (built by the tx engine at
    create time and held by the flow since) — parsing it here is reading
    our own record, not trusting external input; any inconsistency with
    the record's scalar fields raises, which the caller turns into a
    value-free internal error (fail closed, nothing signed).

    SELF-TRANSFER RECORDS (TCK-TX-SELF-001): when the record carries
    ``self_payment_indices``, every payment output is the wallet's OWN
    receive address and there is NO change output. The intent is proven the
    same independent way the change output is: each output's script is
    RE-DERIVED from ``parsed`` at its recorded receive index and must match
    the staged PSBT position-for-position (a drift between what we staged
    and what the deriver produces fails closed before anything signs), and
    ``amount_sats`` (the plan's TOTAL payment value, uniform per output by
    the handler's construction) must equal the sum of the output values.
    ``expected_recipient_outputs`` is then the WHOLE output list — the same
    re-validation gate ``tx/revalidate.py`` applies to external sends, just
    with every own address as an expected positional output.
    """
    psbt = PSBT.parse(base64.b64decode(confirmed.psbt_base64))
    outputs = [(bytes(out.script_pubkey.data), out.value) for out in psbt.tx.vout]

    if confirmed.self_payment_indices is not None:
        # Self-transfer: N own receive outputs, never any change.
        indices = confirmed.self_payment_indices
        if confirmed.change_sats is not None:
            raise ValueError("self-transfer record must carry no change")
        if len(outputs) != len(indices):
            raise ValueError("confirmed record output count mismatch")
        if not indices or confirmed.amount_sats % len(indices) != 0:
            raise ValueError("self-transfer record payment total mismatch")
        each = confirmed.amount_sats // len(indices)
        if sum(value for _script, value in outputs) != confirmed.amount_sats:
            raise ValueError("self-transfer record payment total mismatch")
        receive_deriver = BranchDeriver(parsed, BRANCH_RECEIVE)
        expected_recipients: list[tuple[bytes, int]] = []
        for output, index in zip(outputs, indices, strict=True):
            expected_script = bytes(address_to_scriptpubkey(receive_deriver.address(index)).data)
            if output != (expected_script, each):
                raise ValueError("confirmed record self-output mismatch")
            expected_recipients.append(output)
        return IntendedTx(
            expected_recipient_outputs=tuple(expected_recipients),
            expected_change=None,
            expected_inputs_count=confirmed.inputs_count,
            expected_fee_sats=confirmed.fee_sats,
            expected_sequence=SEQUENCE_RBF_ENABLED,
            expected_vsize_max=confirmed.vsize + 1,
            tx_ref=confirmed.tx_ref,
        )

    expected_len = 2 if confirmed.change_sats is not None else 1
    if len(outputs) != expected_len:
        raise ValueError("confirmed record output count mismatch")
    recipient_script = bytes(address_to_scriptpubkey(confirmed.recipient).data)
    if outputs[0] != (recipient_script, confirmed.amount_sats):
        raise ValueError("confirmed record recipient output mismatch")
    change: tuple[bytes, int] | None = None
    if confirmed.change_sats is not None:
        if outputs[1][1] != confirmed.change_sats:
            raise ValueError("confirmed record change output mismatch")
        # R3 independent re-derivation: the change script staged in the PSBT
        # must be EXACTLY the branch-1 deriver's address at the recorded
        # change index — never a silently different script (fail closed,
        # value-free; the caller turns this into an internal error).
        if change_index is None:
            raise ValueError("confirmed record missing change index")
        change_address = BranchDeriver(parsed, BRANCH_CHANGE).address(change_index)
        rederived_script = bytes(address_to_scriptpubkey(change_address).data)
        if outputs[1][0] != rederived_script:
            raise ValueError("confirmed record change output script mismatch")
        change = outputs[1]
    return IntendedTx(
        expected_recipient_outputs=(outputs[0],),
        expected_change=change,
        expected_inputs_count=confirmed.inputs_count,
        expected_fee_sats=confirmed.fee_sats,
        expected_sequence=SEQUENCE_RBF_ENABLED,
        expected_vsize_max=confirmed.vsize + 1,
        tx_ref=confirmed.tx_ref,
    )


def _extract_signed_tx_hex(psbt_base64: str) -> str:
    """Finalize the SIGNED PSBT and return the raw transaction hex.

    The same embit extraction the re-validation gate uses
    (``embit.finalizer.finalize_psbt`` building each input's final
    witness from the verified partial signatures) — NO re-signing, no
    mutation: the bytes that passed re-validation are the bytes that get
    broadcast. The signed record already passed the gate at sign time
    (``mark_signed`` is reachable only after a revalidation pass), so a
    failure here is contained as an internal error, never an error string
    carrying tx material.
    """
    psbt = PSBT.parse(base64.b64decode(psbt_base64))
    tx = finalizer.finalize_psbt(psbt)
    if tx is None:
        raise ValueError("signed transaction could not be finalized")
    return tx.serialize().hex()


def _descriptor_account_path(parsed: ParsedKey) -> str:
    """The BIP 32 account path this wallet's descriptor origin carries.

    Same canonical construction the descriptor string itself is built
    from (``wallet.descriptor._build_descriptor_string``: purpose and
    mainnet coin type from the key's script type) — derived from the
    parsed key, never a hardcoded ``m/84'/0'/0'`` literal (ADR-0015
    amendment #2: the HWI signer asks the device for its account key AT
    this path and binds the answer to the descriptor origin fingerprint).
    """
    purpose = SCRIPT_PURPOSES[parsed.script_type]
    return f"m/{purpose}'/{MAINNET_COIN_TYPE}'/0'"


#: TCK-HW-005 SLICE C (user live finding 2026-09-12): the sign path found
#: itself PSBT file when the user wanted their Jade. The pre-sign device
#: check answers the absent case with a CHOICE, never a silent export. The
#: line keeps the signer family's ``"No device found — plug in"`` prefix
#: verbatim (the PHASE3-AC-3 pin, and the family's single source of
#: wording) and adds the explicit file fallback: "file"/"export" then runs
#: the export, "retry" re-probes. Value-free; code-owned; never model text.
_HW_SIGN_ASK: Final[str] = (
    "No device found — plug in and unlock your device, then say 'retry', "
    "or say 'file' and I can export the transaction file for your SD card "
    "instead."
)

#: The value-free note on a device sign the USER'S OWN WORDS routed ahead
#: of a file-configured signer (TCK-HW-005 slice C; same conflict-guidance
#: pattern as HW-004's "Using your configured signer (file)." — names what
#: ran, value-free, never a refusal).
_HW_DEVICE_PREFER_NOTE: Final[str] = (
    "Signed on your connected device (your configured signer is the file "
    "transfer — say 'file' while a transaction awaits signing to export instead)."
)


def _make_sign_tx_handler(
    flow: TxFlow,
    selection: SignerSelection,
    signer_override: Signer | FilePsbtSigner | None,
    store: Store,
    wallet_id: int,
    parsed: ParsedKey,
    session: SendSession | None = None,
) -> Handler:
    """Create the ``sign_tx`` handler: CONFIRMED record → signer → revalidate.

    Pipeline (every step fail-closed; the flow moves only after ALL
    succeed — TCK-P3-005 / ADR-0013 amendment):

    1. Flow gate: the flow must be ``CONFIRMED`` with a ``tx_ref``
       matching the approved record. Refusals surface as
       ``{"error": "sign_refused", "detail": <value-free>}``; the state
       is never touched by a refused attempt.
    2. Intent construction: :func:`_intended_from_confirmed` — the frozen
       description of the approved transaction the signed result must
       match exactly.
    3. Signer dispatch: the app-configured backend (``selection.kind``,
       from ``--signer`` / :data:`SIGNER_ENV_VAR`) is AUTHORITATIVE
       (TCK-HW-004). The model's optional closed-enum ``signer`` param is
       advisory ONLY and never overrides the user's configured backend — a
       mismatch surfaces a value-free guidance note naming the configured
       kind (never the model's), never a re-route.
       - **file** (ADR-0014): the EXPECTED signed file is the
         deterministic ADR-0014 name derived from the confirmed
         ``tx_ref`` (``FilePsbtSigner.signed_import_path``); the model's
         ``sign_tx`` params carry only ``tx_ref``+``signer``, so the
         filename is handler-derived, never model-supplied. When that
         file is present it is imported directly (the unsigned export
         already happened on a previous attempt — the signer refuses a
         re-export that would collide with a placed signed file);
         otherwise the unsigned PSBT is exported to the transfer folder
         (idempotent on retry) and the handler returns
         ``{"error": "signed_file_missing", ...}`` with the export path
         and expected filename for narration.
        - **hwi** (ADR-0015 + amendment #2): :class:`HwiUsbSigner` is
          constructed lazily ONLY when this kind runs (expected account-key
          fingerprint and the descriptor's account path, both from the
          parsed wallet key), and the candidate gate + post-open
          account-key bind run inside it. A :class:`DeviceError` maps to
          ``{"error": "device_error", "guidance": <§10 guidance>}`` — the
          guidance string is code-owned text from the error hierarchy and
          is narrated verbatim.
           PRE-SIGN DEVICE CHECK (TCK-HW-005 slice C, user finding
           2026-09-12): every device-path attempt first runs ONE bounded
           enumerate (:meth:`HwiUsbSigner.sign_probe`, no unlock attempt).
           No device → the value-free file-offering ASK; locked → the
           existing locked-guidance family; NOTHING exports silently and
           the flow stays CONFIRMED. The user's own words may also route
           the FILE kind onto this device path (probe-gated, session-
           latched — the model cannot set it) or, via an explicit
           "file"/"export" answer, run the export under the hwi config.
    4. REVALIDATION GATE (non-negotiable):
       :func:`~localwallet.tx.revalidate.revalidate_signed_psbt` checks
       the signed PSBT against the intent — outputs in exact order,
       fee, inputs, sequence, vsize, per-input EC verification. ANY
       mismatch is a HARD STOP:
       ``{"error": "revalidation_failed", "detail": <value-free>}`` with
       the flow untouched (still ``CONFIRMED``) — the user retries the
       signing step or re-creates the transaction. Nothing signed, and
       broadcast is unreachable from this state.
    5. Recording: :meth:`TxFlow.mark_signed` (CONFIRMED → SIGNED) with
       the signed PSBT exactly as the signer returned it. Success result:
       ``{"status": "signed", "tx_ref", "txid" (from RevalidatedTx),
       "signer_name", "checksum_verified"}`` — broadcasting is a separate
       model intent, never automatic.

    No network I/O: the signer boundary is device/file I/O; the chain is
    not touched on this path.
    """

    def handler(envelope: Envelope) -> dict[str, object]:
        params = envelope.params
        if not isinstance(params, SignTxParams):
            return {"error": "internal", "detail": "sign_tx params shape mismatch"}

        # 1. Flow gate (pre-check; the transition happens only on success).
        confirmed = flow.confirmed
        if flow.state is not TxFlowStatus.CONFIRMED or confirmed is None:
            return {"error": "sign_refused", "detail": "no confirmed transaction to sign"}
        if params.tx_ref != confirmed.tx_ref:
            return {
                "error": "sign_refused",
                "detail": "tx_ref does not match the confirmed transaction",
            }

        # 2. Intent from the approved record (contained: a record/PSBT
        # inconsistency is a caller bug — fail closed, value-free).
        try:
            # The change index used at create is the branch-1 next_index
            # advanced by exactly one at staging (ADR-0009 allocation);
            # recover it for R3's independent re-derivation of the change
            # script (single-pending-tx invariant keeps it stable here).
            change_index = None
            if confirmed.change_sats is not None:
                change_index = (
                    store.get_derivation(wallet_id, BRANCH_CHANGE).next_index - 1
                )
            intended = _intended_from_confirmed(
                confirmed, parsed=parsed, change_index=change_index
            )
        except Exception:  # noqa: BLE001 — containment: embit parse/consistency errors vary; re-raising could leak record material
            return {
                "error": "internal",
                "detail": "confirmed transaction record is inconsistent with its staged psbt",
            }

        # 3. Signer dispatch (TCK-HW-004): the app-configured backend is
        # AUTHORITATIVE. The model's optional closed-enum ``signer`` param
        # never reroutes the user's airgap-vs-device security choice — it is
        # advisory only. If the model named a DIFFERENT kind than the
        # configured one, surface a value-free guidance line naming the
        # configured kind (never the model's text); the configured kind
        # still runs.
        kind = selection.kind
        if params.signer is not None and params.signer != selection.kind:
            conflict_note = f"Using your configured signer ({selection.kind})."
        else:
            conflict_note = None

        def _out(result: dict[str, object]) -> dict[str, object]:
            if conflict_note is not None and "guidance" not in result:
                result["guidance"] = conflict_note
            return result

        # TCK-HW-005 SLICE C (user live finding 2026-09-12): the user's OWN
        # WORDS can steer the handoff — deterministic utterance intercepts
        # stamp these session flags; the model can never set or clear them
        # (gate_decision precedent, ADR-0013). ``hw_sign_wanted`` latches
        # "device for THIS flow" so every sign attempt (incl. a bare
        # "retry") re-probes the device instead of silently exporting; an
        # explicit "file"/"export" answer consumes
        # ``file_sign_export_once`` and runs the airgap export even under
        # the hwi config. Configured file signer + neither flag = the
        # pre-slice file behavior, byte-identical (HW-004's matrix intact).
        hw_wanted = session is not None and session.hw_sign_wanted
        file_once = session is not None and session.file_sign_export_once
        if session is not None:
            session.file_sign_export_once = False
        run_device = (kind != SIGNER_KIND_FILE or hw_wanted) and not file_once

        if not run_device:
            file_signer = (
                signer_override
                if isinstance(signer_override, FilePsbtSigner)
                else FilePsbtSigner(selection.dir_path)
            )
            signed_path = file_signer.signed_import_path(confirmed.tx_ref)
            if signed_path.exists():
                # The user placed the signed file: import it (the unsigned
                # export happened on a previous attempt).
                try:
                    signed_result = file_signer.import_signed(
                        signed_path, expected_tx_ref=confirmed.tx_ref
                    )
                except SignerError as exc:
                    return _out({"error": "import_failed", "detail": str(exc)})
            else:
                try:
                    exported = file_signer.export_unsigned(
                        confirmed.psbt_base64, confirmed.tx_ref
                    )
                except SignerError as exc:
                    return _out({"error": "export_failed", "detail": str(exc)})
                return _out({
                    "error": "signed_file_missing",
                    "unsigned_path": str(exported.unsigned_path),
                    "signed_filename": signed_path.name,
                    "signer_name": file_signer.name,
                })
        else:
            # The device path. Under the FILE kind with the user's
            # hardware-utterance latch, any override is the FILE signer's —
            # build the device signer the way the hwi kind does (module
            # attribute: the test seam the repl harness monkeypatches).
            device_signer = (
                signer_override
                if signer_override is not None and kind != SIGNER_KIND_FILE
                else HwiUsbSigner(
                    selection.fingerprint_hex, _descriptor_account_path(parsed)
                )
            )
            # PRE-SIGN DEVICE CHECK (slice C): ONE bounded enumerate, no
            # unlock attempt, no client opened, no file written. Absent →
            # the value-free file-offering ASK (never a silent export,
            # never a silent dead end); locked → the EXISTING locked-guidance
            # family (the unlock rides slice A's chat command). Either way
            # the flow stays CONFIRMED. DUCK-TYPED on the probe surface (not
            # isinstance): the real HwiUsbSigner is the only signer with
            # ``sign_probe``; test-seam fakes without one sign directly (they
            # own their error paths), and the class is monkeypatchable.
            sign_probe = getattr(device_signer, "sign_probe", None)
            if sign_probe is not None:
                state, probe_lines = sign_probe()
                if state == "absent":
                    return _out({"error": "device_error", "guidance": _HW_SIGN_ASK})
                if state == "locked":
                    return _out({
                        "error": "device_error",
                        "guidance": "\n".join(probe_lines),
                    })
            try:
                signed_result = device_signer.sign_unsigned(confirmed.psbt_base64)
            except DeviceError as exc:
                # guidance is code-owned §10 text from the error hierarchy.
                return _out({"error": "device_error", "guidance": str(exc)})
            except SignerError as exc:
                return _out({"error": "signer_error", "detail": str(exc)})

        # 4. Revalidation gate — mismatch is a HARD STOP, flow untouched.
        try:
            revalidated = revalidate_signed_psbt(signed_result.psbt_base64, intended)
        except TamperedPsbtError as exc:
            return _out({"error": "revalidation_failed", "detail": str(exc)})

        # 5. Record the signed PSBT (CONFIRMED → SIGNED).
        try:
            signed = flow.mark_signed(params.tx_ref, signed_result.psbt_base64)
        except FlowError as exc:  # unreachable single-threaded after the gate
            return _out({"error": "sign_refused", "detail": str(exc)})

        if session is not None:
            # The flow is signed — the user's device wish is fulfilled
            # (slice C latch discipline: the preference never outlives it).
            session.hw_sign_wanted = False
        result: dict[str, object] = {
            "status": "signed",
            "tx_ref": signed.tx_ref,
            "txid": revalidated.txid,
            "signer_name": signed_result.signer_name,
            "checksum_verified": signed_result.checksum_verified,
        }
        if run_device and kind == SIGNER_KIND_FILE:
            # The user's WORDS routed this sign onto the device ahead of the
            # configured file transfer — name it, value-free (HW-004's
            # conflict-guidance pattern: names what ran, never a refusal;
            # the config still owns every turn the user did not speak for).
            result["guidance"] = _HW_DEVICE_PREFER_NOTE
        return _out(result)

    return handler


def _cpfp_parent_gone(
    store: Store, wallet_id: int, client: ChainClient, pending: _CpfpPending
) -> bool:
    """Is the hurried parent of a failed-to-broadcast cpfp child PROVEN
    gone? (TCK-CPFP-002 deliverable 5 — the input-unspendable classifier.)

    Evidence order (RBF-005's doctrine: store truth first, the chain only
    to confirm condemnation): the coin the child spends has left our
    unspent set on a fresh read → the scan already saw the payment undone
    or replaced — gone. The row is still there but CONFIRMED → the
    payment landed, the child's input is spendable, the failure was
    something else — not gone. Still unconfirmed in the cache → ask the
    backend (the documented recovery GET; ONE call, only ever on an
    already-failed cpfp-child broadcast): the parent no longer exists
    anywhere it could ride → gone; known (mempool or chain) or any other
    answer → NOT proven gone → honest transient. A failed store read or
    any failed/unrecognized chain answer never condemns (absence of
    evidence is never evidence of death — the user keeps the retryable
    ``broadcast_failed`` answer)."""
    try:
        rows = store.get_utxos_for_wallet(wallet_id)
    except (StoreError, sqlite3.Error):
        return False
    row = next(
        (
            r
            for r in rows
            if r.txid.lower() == pending.inbound_txid and r.vout == pending.inbound_vout
        ),
        None,
    )
    if row is None:
        return True
    if row.confirmed == 1:
        return False
    try:
        client.get_tx_status(pending.parent_txid)
    except ChainError as exc:
        # The RBF-005 pinned dialect: the Esplora/Bitcoind not-found
        # surfaces as ``status 404``; other chain errors (transport, 5xx,
        # an Electrum-dialect rejection) condemn nothing.
        return "status 404" in str(exc)
    except Exception:  # noqa: BLE001 — containment: an adapter surprise never condemns a parent
        return False
    return False


def _make_broadcast_tx_handler(
    flow: TxFlow,
    client: ChainClient,
    store: Store,
    wallet_id: int,
    *,
    session: SendSession | None = None,
    output: _Output | None = None,
) -> Handler:
    """Create the ``broadcast_tx`` handler: SIGNED record → chain backend → BROADCAST.

    TCK-RBF-004 (commit-only-on-success lineage): when the broadcast that
    just SUCCEEDED is the staged replacement a ``bump_fee`` conversation
    carried (``session.bump_pending`` names it by ``tx_ref``), the handler
    writes the lineage through the sanctioned
    :meth:`~localwallet.store.Store.record_replacement` writer and marks
    the broadcast as a bump (result ``replaces_txid`` + the session's
    ``bump_bcast_txid`` that arms the faster/slower reroute). A FAILED
    broadcast touches nothing (the signed record is kept for retry, the
    pending is unchanged, no lineage is written); a lineage-WRITE failure
    after a successful broadcast rides ``store_warning`` (bookkeeping never
    undoes the money path) with the pending slot cleared (the link is
    recoverable via ``/label``-style manual state, never a re-broadcast).

    Pipeline (TCK-P3-005 / ADR-0013 amendment — broadcast ONLY from
    ``SIGNED``, no skip path past the signing state):

    1. Flow gate: the flow must be ``SIGNED`` with a ``tx_ref`` matching
       the signed record. Refusals surface as
       ``{"error": "broadcast_refused", "detail": <value-free>}``. The
       gate presupposes the handler-level revalidation completed at sign
       time (``mark_signed`` is reachable only through it) — the immutable
       :class:`~localwallet.tx.flow.SignedTx` record is byte-for-byte what
       was re-validated.
    2. Hex extraction: the SIGNED PSBT is finalized and extracted to raw
       transaction hex with the same embit finalization the revalidation
       gate uses (:func:`_extract_signed_tx_hex`) — no re-signing, no
       mutation.
    3. Broadcast: ``client.broadcast_tx(tx_hex)`` — a SINGLE-attempt POST
       (the chain layer's documented no-retry policy: a POST is not
       idempotent). A :class:`ChainError` (network, 429, 5xx, malformed
       response) surfaces as
       ``{"error": "broadcast_failed", "detail": <scrubbed by the chain
       layer>}`` and the flow STAYS ``SIGNED`` — the signed transaction is
       kept and the user retries; recovery is a cheap GET (``tx_status``),
       never a blind re-POST by us. Every send failure ALSO emits the
       TCK-DIAG-003 value-free debug line (failure class + exception name
       via the DIAG-001 taxonomy) to console + launch log through
       ``output.warning`` — never the transcript/SSE channel, never the
       tx hex or txid. ``output=None`` (test seam, direct-call harnesses)
       emits nothing, exactly like the probe/scan debug sites.
     4. Recording: :meth:`TxFlow.broadcast` (SIGNED → BROADCAST, terminal)
        with the chain-reported txid, then the outbound transaction is
        upserted into the store's history (``height=None``,
        ``direction="out"``, the approved record's fee AND — TCK-RBF-001
        schema v3 capture — the approved record's amount, fee rate, and the
        broadcast first-seen stamp) so ``get_history`` shows it immediately.
        A store failure becomes a value-free
       ``store_warning`` result key — the BROADCAST state is preserved
       (the money path succeeded; bookkeeping must not undo it).

    Success result: ``{"status": "broadcast", "txid", "message":
    "tracking confirmations — ask 'status'"}``.
    """

    def handler(envelope: Envelope) -> dict[str, object]:
        params = envelope.params
        if not isinstance(params, BroadcastTxParams):
            return {"error": "internal", "detail": "broadcast_tx params shape mismatch"}

        # 1. Flow gate.
        signed = flow.signed
        if flow.state is not TxFlowStatus.SIGNED or signed is None:
            return {"error": "broadcast_refused", "detail": "no signed transaction to broadcast"}
        if params.tx_ref != signed.tx_ref:
            return {
                "error": "broadcast_refused",
                "detail": "tx_ref does not match the signed transaction",
            }

        # 2. Extract the broadcast hex from the re-validated signed PSBT.
        try:
            tx_hex = _extract_signed_tx_hex(signed.psbt_base64)
        except Exception:  # noqa: BLE001 — containment: unreachable post-revalidate; never leak tx material into the error
            return {
                "error": "internal",
                "detail": "signed transaction could not be extracted for broadcast",
            }

        # 3. Single-attempt POST (chain layer owns the no-retry policy).
        try:
            txid = client.broadcast_tx(tx_hex)
        except ChainError as exc:
            # TCK-DIAG-003: the console/log debug companion for the friendly
            # line below — value-free by construction (the DIAG-001 failure
            # CLASS + exception class name from the structured ChainError;
            # the rpc-error rung also carries its numeric CODE, a protocol
            # constant per DIAG-002). NEVER the tx hex, never the txid, no
            # response text, no detail string: the chain layer's canned
            # message already rides the transcript, and a raw server body is
            # untrusted text that never echoes anywhere. Rides
            # ``_Output.warning`` (console + launch log; web mode never
            # touches SSE) — the exact DIAG-001 channel and shape.
            if output is not None:
                fc, name, extra = _failure_parts(exc)
                output.warning(
                    f"broadcast: send failed [class={fc} exc={name}{extra}]"
                )
            # TCK-CPFP-002 (deliverable 5): a failed broadcast of a STAGED
            # CPFP CHILD distinguishes an input that can never spend (the
            # hurried parent is gone — recheck fresh store truth first, the
            # chain's own memory only as the confirming second read) from
            # a transient failure. The gone answer NEVER pitches a retry
            # (the signed child can never land); the ordinary
            # ``broadcast_failed`` kept-for-retry wording is reserved for
            # everything the recheck cannot condemn. The recheck itself is
            # the documented recovery GET (chain's single-POST policy:
            # callers recover via tx_status, never via a blind re-POST);
            # it fires ONLY on an already-failed cpfp-child broadcast, so
            # no other flow grows a network call, and its 404 trigger is
            # the Esplora/Bitcoind error dialect — on a backend that
            # speaks otherwise the answer degrades honestly to transient
            # (never a "gone" claim without evidence).
            detail = str(exc)  # scrubbed by the chain layer (no txids/tx hex)
            if (
                session is not None
                and session.cpfp_pending is not None
                and session.cpfp_pending.tx_ref == params.tx_ref
                and _cpfp_parent_gone(store, wallet_id, client, session.cpfp_pending)
            ):
                return {"error": "cpfp_parent_gone", "detail": _CPFP_PARENT_GONE}
            return {"error": "broadcast_failed", "detail": detail}

        # 4. Record the transition, then the history row.
        try:
            flow.broadcast(params.tx_ref, txid)
        except FlowError as exc:  # unreachable single-threaded after the gate
            return {"error": "broadcast_refused", "detail": str(exc)}

        result: dict[str, object] = {
            "status": "broadcast",
            "txid": txid,
            "message": "tracking confirmations — ask 'status'",
        }
        confirmed = flow.confirmed
        try:
            store.upsert_txs(
                [
                    TxRecord(
                        wallet_id=wallet_id,
                        txid=txid,
                        height=None,
                        block_time=None,
                        fee_sats=confirmed.fee_sats if confirmed is not None else None,
                        direction=DIR_OUT,
                        raw_summary=None,
                        # TCK-RBF-001 broadcast-time capture (schema v3): the
                        # fields the flow's confirmed record already carries.
                        # THE one store write that unblocks disambiguation
                        # lists and the BIP-125 delta; the shared upsert's
                        # COALESCE preserves them across every later scan.
                        amount_sats=(
                            confirmed.amount_sats if confirmed is not None else None
                        ),
                        fee_rate_centisat_vb=(
                            confirmed.fee_rate_centisat_vb
                            if confirmed is not None
                            else None
                        ),
                        first_seen=int(time.time()),
                    )
                ]
            )
        except (StoreError, sqlite3.Error) as exc:
            # Bookkeeping must not undo the broadcast: warn, stay BROADCAST.
            result["store_warning"] = f"could not record the transaction in history ({exc})"

        # 4b. RBF lineage write (TCK-RBF-004, commit-only-on-success): this
        #     broadcast SUCCEEDED and the record it carried is the staged
        #     fee-bump replacement (one ``tx_ref`` threads pending→confirmed
        #     →signed→broadcast). Link old→new through the sanctioned
        #     writer ONLY. A failure is a ``store_warning`` (the money path
        #     is done) and clears the pending slot (no re-broadcast to retry
        #     the write; the live tx is tracked regardless).
        if (
            session is not None
            and session.bump_pending is not None
            and session.bump_pending.tx_ref == params.tx_ref
        ):
            bump = session.bump_pending
            session.bump_pending = None
            try:
                store.record_replacement(wallet_id, bump.old_txid, txid)
            except (StoreError, sqlite3.Error) as exc:
                result["store_warning"] = (
                    f"could not record the replacement lineage ({exc})"
                )
            else:
                result["replaces_txid"] = bump.old_txid
                session.bump_bcast_txid = txid

        # 4c. CPFP child marker retirement (TCK-CPFP-002): this broadcast
        #     SUCCEEDED for the staged child, so its conversation record is
        #     done. NO lineage is written — the store's lineage link is
        #     RBF-only (schema v3 semantics): the hurried parent is a
        #     DIFFERENT, untouched transaction, and a child→parent link
        #     would be new schema (not this ticket). The child's own outputs
        #     inherit the coin labels below (``self_payment_indices`` — the
        #     TCK-TX-SELF-001 lineage path, unchanged).
        if (
            session is not None
            and session.cpfp_pending is not None
            and session.cpfp_pending.tx_ref == params.tx_ref
        ):
            session.cpfp_pending = None

        # 5. Coin-label lineage (TCK-UTXO-001, design doc §1.3): our outputs
        #    inherit the UNION of the wallet's spent inputs' tag sets — a
        #    mixed-lineage coin carries both classes (the fail-safe side for
        #    the deterministic partition check). Which outputs are ours is the
        #    revalidated positional contract: change rides LAST when present
        #    (tx/psbt.py + the sign-time revalidation), and this SIGNED record
        #    is byte-frozen, so vout = count-1 is ours exactly when
        #    change_sats is set. A self-transfer (TCK-TX-SELF-001) owns EVERY
        #    output (fresh receive plan, no external destination) — ALL vouts
        #    inherit, which is what keeps a consolidated/split coin on its
        #    pool side after the reshuffle. A send whose recipient is our own
        #    receive address inherits nothing there (ponytail: honest-bounds
        #    edge — the coin appears unlabeled on the next scan and /label
        #    covers it; full script-ownership matching is the provenance
        #    view's problem, not this capture path's). Purely local
        #    bookkeeping — labeling never causes network I/O — and it must
        #    never undo a completed broadcast, so EVERY failure is contained
        #    value-free. TCK-CONS-001: when this broadcast is a staged
        #    consolidation plan (the conversation's marker matches the flow
        #    record), the inherited union gets the closed-set
        #    ``consolidation`` tag ADDED (a second tag describing what this
        #    payment was, display-only per §1.4 — never a partition word)
        #    and the free-note RECORD "consolidated from N outputs" — N
        #    counted from the broadcast's own inputs, never from user text.
        owned_vouts: tuple[int, ...] = ()
        if confirmed is not None:
            try:
                signed_psbt = PSBT.parse(base64.b64decode(signed.psbt_base64))
                n_vouts = len(signed_psbt.tx.vout)
                if confirmed.self_payment_indices is not None:
                    owned_vouts = tuple(range(n_vouts))
                elif confirmed.change_sats is not None:
                    owned_vouts = (n_vouts - 1,)
                if owned_vouts:
                    spent_inputs = tuple(
                        (bytes(reversed(vin.txid)).hex(), vin.vout)
                        for vin in signed_psbt.tx.vin
                    )
                    store.propagate_coin_lineage(
                        wallet_id, txid, owned_vouts, spent_inputs
                    )
                    if (
                        session is not None
                        and session.cons_pending is not None
                        and session.cons_pending.tx_ref == params.tx_ref
                    ):
                        n_from = len(spent_inputs)
                        record = f"consolidated from {n_from} output{'s' if n_from != 1 else ''}"
                        for out_vout in owned_vouts:
                            existing = store.get_coin_label(wallet_id, txid, out_vout)
                            merged = tuple(
                                dict.fromkeys(
                                    [*(existing.tags if existing else ()), "consolidation"]
                                )
                            )
                            store.set_coin_label(
                                wallet_id, txid, out_vout, merged, record
                            )
            except Exception:  # noqa: BLE001 — containment: embit/store errors vary; missed tag-inheritance is annotation loss, never a money or broadcast failure
                result.setdefault(
                    "store_warning",
                    "coin tag inheritance did not record — use /label after the next scan",
                )
        # Consolidation marker retirement (TCK-CONS-001, the CPFP-002 4c
        # shape): this broadcast SUCCEEDED, the conversation record is done
        # whether or not the annotation write made it (a failed write is
        # the contained store_warning above, never a retry of the money).
        if (
            session is not None
            and session.cons_pending is not None
            and session.cons_pending.tx_ref == params.tx_ref
        ):
            session.cons_pending = None
        return result

    return handler


def _lineage_tx_status(
    tx_records: Sequence[TxRecord], txid: str
) -> dict[str, object] | None:
    """TCK-RBF-005: the recorded-lineage answer for one queried txid.

    Pure store read — returns the tool-result dict when the wallet's own
    rows (``get_txs_for_wallet`` output, passed verbatim) already carry
    the truth about ``txid``, else ``None`` (no lineage data: the caller
    asks the chain):

    - ``{"txid", "confirmed": False, "lineage": "replaced", "replaced_by",
      "replacement_height"}`` — a pending original. Its replacement has
      CONFIRMED (terminal: ``replacement_height`` set from the row) or the
      recorded bump is unresolved (live race: ``replacement_height`` is
      ``None`` — the caller may use that shape only to resolve a chain
      not-found, never to pre-empt the chain, because the original may
      still confirm).
    - ``{"txid", "confirmed": False, "lineage": "evicted", "original_txid",
      "original_height"}`` — a pending bump whose ORIGINAL confirmed: the
      bump can never take effect (terminal).

    Every terminal claim derives from :func:`localwallet.store.
    superseded_states` (the one retirement rule — reorg-honest: a cleared
    height un-retires the sibling, and a link to an unconfirmed or
    unrecorded partner claims nothing). All quoted values come verbatim
    from the rows; nothing is invented, nothing is fetched.
    """
    by_txid = {r.txid: r for r in tx_records}
    row = by_txid.get(txid)
    if row is None:
        return None
    state = superseded_states(tx_records).get(txid)
    if state == SUPERSEDED_REPLACED:
        partner = by_txid[row.replaced_by_txid or ""]
        return {
            "txid": txid,
            "confirmed": False,
            "lineage": "replaced",
            "replaced_by": row.replaced_by_txid,
            "replacement_height": partner.height,
        }
    if state == SUPERSEDED_EVICTED:
        winner = next(
            r
            for r in tx_records
            if r.replaced_by_txid == txid and r.height is not None
        )
        return {
            "txid": txid,
            "confirmed": False,
            "lineage": "evicted",
            "original_txid": winner.txid,
            "original_height": winner.height,
        }
    if row.height is None and row.replaced_by_txid is not None:
        # Live race: the bump is a recorded fact (we broadcast it), its
        # outcome is not yet. Hedged shape — height None.
        return {
            "txid": txid,
            "confirmed": False,
            "lineage": "replaced",
            "replaced_by": row.replaced_by_txid,
            "replacement_height": None,
        }
    return None


def _make_tx_status_handler(
    client: ChainClient,
    flow: TxFlow,
    scan_gate: StartupScan | None = None,
    *,
    store: Store | None = None,
    wallet_id: int | None = None,
) -> Handler:
    """Create the ``tx_status`` handler: quoted txid → chain backend status.

    The ``txid`` param (layer 3 enforced it to EXACTLY 64 lowercase hex —
    the injection guard for the URL path) is looked up via
    ``client.get_tx_status``; the result quotes the response verbatim:
    ``{"txid", "confirmed", "block_height", "block_time"}``.

    TCK-RBF-005 (lineage-aware answers from store truth) when a ``store``
    is wired (every production table wires one; ``store=None`` keeps the
    pure chain behavior the legacy headless call sites pin):

    - a txid whose lineage pair has RESOLVED is answered BEFORE the chain
      call — the scan already proved which side confirmed and the chain
      can neither improve nor contradict that: terminal ``replaced`` →
      the "replaced by <new txid>" copy (with the replacement's recorded
      height), terminal ``evicted`` → the honest eviction copy.
    - a txid the backend does not know (``status 404`` — the query that
      for a superseded original used to mean an endless "try again") with
      a recorded bump resolves to the hedged replaced copy instead
      (:func:`_lineage_tx_status`). Never an endless retry loop.
    - everything else is byte-identical: unlinked lookups, the
      eventual-consistency line for the flow's own just-broadcast txid,
      and the ``awaiting_backend`` refusal — which still runs FIRST, so
      the no-consent hold stands down every lookup, lineage included (a
      terminal claim without consent could never exist anyway: heights
      arrive only via consented scans).

    TCK-PRIVACY-001 (the audit's one un-gated pre-consent chain call): while
    the first-run backend is UNRESOLVED (``awaiting_backend``), the handler
    refuses with :data:`NO_BACKEND_REFUSAL` BEFORE the lookup — a status
    check must never be the query that reaches a server the user never
    picked. The hold state is the ONLY stand-down: once a backend is chosen
    (or during the consented first scan) the lookup behaves unchanged.

    Eventual consistency (documented): a JUST-broadcast transaction is
    often not indexed by the backend yet — the lookup answers not-found
    until it sees the transaction. When the queried txid IS the flow's recorded
    broadcast txid and the lookup fails with the not-found status, the
    handler surfaces ``{"error": "unknown_tx", "detail": <value-free>}``
    instead of a generic chain failure, so the narration can say "not
    indexed yet — try again shortly". The 404 detection matches the chain
    layer's documented error-message contract ("status 404"); other
    chain failures surface as ``{"error": "chain_unavailable",
    "detail": <scrubbed>}``.

    Backend-dependence note (TCK-RBF-004 review rider): the ``"status 404"``
    hedge trigger is the Esplora/Bitcoind error dialect. Electrum reports a
    not-found through a DIFFERENT dialect (:mod:`localwallet.chain.electrum`
    raises ``request rejected by the server``, not ``status 404``), so on an
    Electrum backend the 404→hedged-replaced and 404→eventual-consistency
    branches do NOT fire — the lookup surfaces as an ordinary
    ``chain_unavailable`` instead. Normalizing the trigger is deferred (the
    honest smaller-diff is naming the dependence here); the terminal
    lineage answers above are backend-independent (store-truth, no chain).

    Store-read stand-down (TCK-RBF-004 review rider): when a store IS wired
    and the lineage read itself fails, this handler returns ``store_error``
    and stands the lookup down EVEN for a txid with no lineage row (which a
    pure-chain wiring would have answered). This is the RBF-005 shape, kept
    (lineage truth and the chain answer share the one read; failing the read
    means the lineage answer cannot be trusted either — fail closed).

    The model obtains the txid to query from the FACTS block
    (``broadcast_txid``, :func:`_flow_facts`) — quoted verbatim, never
    invented.
    """

    def handler(envelope: Envelope) -> dict[str, object]:
        params = envelope.params
        if not isinstance(params, TxStatusParams):
            return {"error": "internal", "detail": "tx_status params shape mismatch"}
        if scan_gate is not None and scan_gate.state == "awaiting_backend":
            return {"error": "backend_unchosen", "detail": NO_BACKEND_REFUSAL}
        lineage: dict[str, object] | None = None
        if store is not None and wallet_id is not None:
            try:
                rows = store.get_txs_for_wallet(wallet_id)
            except (StoreError, sqlite3.Error) as exc:
                return _store_error(exc)
            lineage = _lineage_tx_status(rows, params.txid)
            if lineage is not None and (
                lineage["lineage"] == "evicted"
                or lineage.get("replacement_height") is not None
            ):
                return lineage  # terminal store truth — the chain adds nothing
        try:
            status = client.get_tx_status(params.txid)
        except ChainError as exc:
            detail = str(exc)  # scrubbed by the chain layer (no txids)
            if "status 404" in detail and lineage is not None:
                # The backend never saw (or no longer relays) this txid, but
                # the wallet's own lineage rows know: the recorded bump's
                # hedged replaced copy — never an endless "try again" for a
                # superseded original. (Live-race shape only: terminal ones
                # were answered above, without a chain call.)
                # ponytail: multi-bump chains (T1←T2←T3, each replaced_by
                # pointing at its direct bump) — this hedged copy names ONE
                # direct replacement per row, so a query on T1 says "replaced
                # by T2" even after T2 itself was replaced by T3. The pair it
                # names (T1, T2) can then both be dead (only T3 ever confirms)
                # — a hedge-overstatement, never a false "confirmed".
                # Transitive-closure resolution is future work (one store
                # walk; unneeded for the single bump the conversation ships).
                return lineage
            if (
                flow.txid is not None
                and params.txid == flow.txid
                and "status 404" in detail
            ):
                return {
                    "error": "unknown_tx",
                    "detail": (
                        "the broadcast transaction is not indexed yet — eventual "
                        "consistency; try again shortly"
                    ),
                }
            return {"error": "chain_unavailable", "detail": detail}
        return {
            "txid": status.txid,
            "confirmed": status.confirmed,
            "block_height": status.block_height,
            "block_time": status.block_time,
        }

    return handler


def _backend_mode(settings: Settings) -> str:
    """The chain-backend privacy mode (3-way classification, TCK-SEC-004
    change 5; the PUBLIC meaning re-targeted and the UNRESOLVED answer
    added by TCK-DESCOPE-M3A).

    Derived from the SAME single selection point the chain client uses
    (:func:`_effective_chain_url`, ADR-0018 as amended):

    - :data:`PRIVACY_MODE_AWAITING_BACKEND` — no ``chain_base_url``: the
      wallet backend is UNRESOLVED (no public default anymore); nothing is
      being consulted and no mode claim is honest.
    - :data:`BACKEND_MODE_PUBLIC` — the consented PUBLIC ELECTRUM server
      (:data:`PUBLIC_ELECTRUM_URL`'s host): a third-party operator sees
      every queried address plus the IP (the red leak warning).
    - :data:`BACKEND_MODE_OWN_NODE_LOCAL` — a configured URL whose host is
      loopback: the user's own node on this machine.
    - :data:`BACKEND_MODE_OWN_NODE_REMOTE` — any other configured host
      (LAN/VPS instance): still the user's own server, but NOT on this
      machine, so copy must not claim lookups "stay on this machine".

    The node_status narration mirrors this function (and the privacy banner
    renders its REMOTE host through it), so neither can disagree with the
    banner about which backend is actually in use. The ``/state``
    ``privacy_mode`` field rides this classification too (TCK-UX-010); the
    ONB-006 ``awaiting_backend`` gate override agrees with the empty-rung
    answer by construction.
    """
    configured = settings.chain_base_url.strip()
    if not configured:
        return PRIVACY_MODE_AWAITING_BACKEND
    host = _configured_url_host(configured)
    # Code-review fix 3 (TCK-DESCOPE-M3A): DNS hosts are case-insensitive —
    # a hand-typed ``ssl://Electrum.Blockstream.info`` is the SAME consented
    # public server and must banner PUBLIC, not own-node (lowercase both
    # sides, the loopback branch's discipline).
    if host is not None and host.lower() == _PUBLIC_ELECTRUM_HOST:
        return BACKEND_MODE_PUBLIC
    if host is not None and host.lower() in _LOOPBACK_HOSTS:
        return BACKEND_MODE_OWN_NODE_LOCAL
    return BACKEND_MODE_OWN_NODE_REMOTE


def _make_node_status_handler(
    settings: Settings,
    *,
    node_detect_fn: Callable[[], LocalNodeReport] | None = None,
) -> Handler:
    """Create the ``node_status`` handler: advise-only node doctor FACTS.

    Runs the local-node detection pass (:func:`detect_local_nodes` — P4-001)
    and the doctor's recommendation selector (:class:`NodeDoctor`), then
    returns a dispatcher-owned FACTS dict the narration quotes verbatim.
    Detection is ADVISE-ONLY: it never executes commands, and the narration
    never includes cookie contents or any credential material — guidance
    comes only from the doctor's structured content.

    Two clean states never probe:

    - **Detection disabled** (``settings.node_detection_enabled`` false,
      LOCALWALLET_NODE_DETECTION_ENABLED=0): the result carries
      ``detection_state="disabled"`` and no findings — no probing, the
      agent says so.
    - **Detection unavailable** (defensive; detection is designed never to
      raise): ``detection_state="unavailable"``, no fabricated findings.

    The result always carries ``backend_mode`` (the 3-way classification
    from the chain-backend selection, :func:`_backend_mode`:
    ``public``/``own_node_local``/``own_node_remote``) so the narration can
    state "querying the public API" vs "your own node on this machine" vs
    the REMOTE host-named line in lockstep with the privacy banner — in
    REMOTE mode the result also carries ``backend_host`` (the configured
    URL's host, scheme/port/credentials stripped, TCK-UX-009: the user's
    OWN config echoed back to them, same as the banner), and the narration
    falls back to the generic wording when there is no host to name.
    """

    def handler(envelope: Envelope) -> dict[str, object]:
        params = envelope.params
        if not isinstance(params, NodeStatusParams):
            # Unreachable via validated envelopes; fail closed anyway.
            return {"error": "internal", "detail": "node_status params shape mismatch"}
        mode = _backend_mode(settings)
        facts: dict[str, object] = {
            "backend_mode": mode,
            "node_detection_enabled": bool(settings.node_detection_enabled),
        }
        if mode == BACKEND_MODE_OWN_NODE_REMOTE:
            host = _configured_url_host(settings.chain_base_url.strip())
            if host is not None:
                facts["backend_host"] = host
        if not settings.node_detection_enabled:
            facts["detection_state"] = NODE_STATUS_DETECTION_DISABLED
            return facts
        try:
            report = (
                node_detect_fn()
                if node_detect_fn is not None
                else detect_local_nodes(settings)
            )
        except Exception:  # noqa: BLE001 — containment: detection is designed never to raise; a failure must never leak internals into narration
            facts["detection_state"] = "unavailable"
            return facts

        core = report.core
        core_reachable = any(p.status is NodeStatus.REACHABLE for p in core)
        core_auth_issue = any(p.status is NodeStatus.AUTH_FAILED for p in core)
        core_synced = any(
            p.status is NodeStatus.REACHABLE
            and p.health is not None
            and p.health.is_synced
            for p in core
        )
        mempool_reachable = report.mempool is NodeStatus.REACHABLE
        electrs_reachable = report.electrs is NodeStatus.REACHABLE
        advice = NodeDoctor().recommend(
            any_core_reachable=core_reachable,
            core_synced=core_synced,
            core_auth_issue=core_auth_issue,
            indexer_reachable=mempool_reachable or electrs_reachable,
        )
        facts.update(
            {
                "detection_state": "ran",
                "core_reachable": core_reachable,
                "core_synced": core_synced,
                "core_auth_issue": core_auth_issue,
                "mempool_reachable": mempool_reachable,
                "electrs_reachable": electrs_reachable,
                "doctor_state": advice.state.value,
                "doctor_headline": advice.headline,
                "doctor_detail": advice.detail,
                "doctor_next_step": advice.next_step,
            }
        )
        return facts

    return handler


def _last_block_suffix(client: ChainClient) -> str | None:
    """The "last block ~N min ago" narration suffix for ONE drain.

    Computed ONCE per drain (never per event) from the single injected
    chain client via :func:`time_since_last_block`. Value-free: only the
    integer minutes are narrated, never the raw timestamp or height. Any
    failure — a failed tip lookup, a backend with no timestamp, or any
    unexpected exception — swallows to ``None`` (no suffix, no crash, no
    extra request re-attempted here); fail-closed.
    """
    try:
        seconds = time_since_last_block(client)
    except Exception:  # noqa: BLE001 — fail-closed: any failure -> no suffix
        return None
    if seconds is None:
        return None
    minutes = max(1, round(seconds / 60))
    return f"last block ~{minutes} min ago"


def _narrate_incoming_event(
    event: IncomingEvent, suffix: str | None = None, number: int | None = None
) -> str:
    """Narrate one ``watch_incoming`` surfacing event (ADR-0019).

    Dispatcher-owned narration from dispatcher-owned facts (P2-004): every
    value (amount, address, height) is quoted verbatim from the event, which
    the poller built from tool output — the model is never in this loop, and
    nothing is generated or "corrected". The short txid is display truncation
    of tool output (the same class as the history narration). This text is
    deliberately shown to the USER in the UI — the required exception to the
    no-addresses/amounts rule; it is never logged.

    ``suffix`` — the value-free "last block ~N min ago" note computed ONCE
    per drain (:func:`_last_block_suffix`); appended verbatim when present.

    ``number`` (TCK-CHAT-001): the event's own address is being PRINTED to
    the user, so the drain registers it and passes its stable registry
    number here — rendered ``at #N <address>``. ``None`` (no store seam /
    registration failure / address-less event) renders the pre-CHAT-001
    line UNCHANGED: the printer never fabricates a number.
    """
    short = f"{event.txid[:12]}…"
    address_part = f"#{number} {event.address}" if number is not None else event.address
    if event.kind == "received":
        state = "confirmed" if event.confirmed else "in mempool"
        line = (
            f"Incoming: received {event.amount_sats} sats at {address_part} "
            f"({state}, tx {short})."
        )
    else:
        height = event.height
        height_part = f" (height {height})" if height is not None else ""
        line = (
            f"Confirmed: {event.amount_sats} sats at {address_part} "
            f"now confirmed{height_part} (tx {short})."
        )
    if suffix:
        line = f"{line} · {suffix}"
    return line


def _make_watch_probe(
    store: Store,
    wallet_id: int,
    scan_fn: Callable[[], object],
) -> Callable[[], list[WatchedTx]]:
    """Build the production ``watch_incoming`` probe for the current wallet.

    The probe refreshes the chain state through ``scan_fn`` (which in the app
    is :meth:`ScanFlow.scan_now` — the ADR-0022 split run as ONE blocking
    scan: plan on the engine, the derive+fetch on the dedicated chain worker
    over the SINGLE config-selected wallet backend (Electrum or bitcoind —
    TCK-DESCOPE-M3A), persist by the engine;
    ADR-0018 — a self-hosted poll hits the user's node, never the public
    API), then reads the wallet's transactions
    and UTXOs back from the store and shapes them into
    :class:`WatchedTx` observations for the poller.

    Only *incoming* transactions (scan direction ``in`` or ``self`` — i.e.
    a watched address received funds) are surfaced. The received address is
    the watched output with the largest value (verbatim) and the amount is
    the total received to the wallet's addresses (verbatim tool output);
    ``confirmed`` is the transaction's height presence.

    Truncation (TCK-SEC-002): the probe's ``scan_fn`` return value — a full
    :class:`ScanSummary` that may carry ``truncated`` — is deliberately
    DISCARDED here. Background watch narration surfaces incoming events
    only; it never renders a scan summary, so the truncation notice belongs
    to explicit scan/rescan narration and cannot spam every poll tick. This
    is the simplest no-spam option: the flag is simply never consumed on
    the watch path (a persistent condition is already surfaced by the
    explicit-scan narration the user can trigger).

    Network only via ``scan_fn``/the chain client — this function itself
    performs no I/O.
    """

    def probe() -> list[WatchedTx]:
        scan_fn()
        txs = store.get_txs_for_wallet(wallet_id)
        utxos = store.get_utxos_for_wallet(wallet_id)
        utxo_by_tx: dict[str, list[UtxoRecord]] = {}
        for utxo in utxos:
            utxo_by_tx.setdefault(utxo.txid, []).append(utxo)
        result: list[WatchedTx] = []
        for tx in txs:
            if tx.direction not in (DIR_IN, DIR_SELF):
                continue
            ours = utxo_by_tx.get(tx.txid, [])
            if not ours:
                continue
            primary = max(ours, key=lambda u: (u.value_sats, u.vout))
            result.append(
                WatchedTx(
                    txid=tx.txid,
                    incoming=True,
                    confirmed=tx.height is not None,
                    height=tx.height,
                    block_time=tx.block_time,
                    address=primary.address,
                    amount_sats=sum(u.value_sats for u in ours),
                )
            )
        return result

    return probe


def _drain_watch(
    watcher: IncomingWatcher | None,
    output_fn: Callable[[str], None],
    *,
    client: ChainClient | None = None,
    store: Store | None = None,
) -> int:
    """Run one due watch cycle (if any) and narrate its events to the user.

    Single-threaded / tick-driven (ADR-0019): the REPL calls this between
    turns; ``poll_due`` gates the run on the configured interval so a full
    poll does not happen on every keystroke. A transient chain/store/scan
    failure fails open — no events, no crash, no logged value — and the next
    turn retries.

    Persistent-failure visibility (NOTE-1, TCK-UX-012(c) copy): when a due
    poll raises, a short value-free line naming the deterministic retry delay
    (``watch: check failed — retrying in ~60s`` — the interval comes from the
    watcher's own resolved value, never a re-read of the ladder) is surfaced
    THROTTLED — once per failure streak, tracked on the watcher — so a
    persistently broken poll stays visible without spamming every turn. When
    a streak ENDS (the first success after at least one failure), ONE
    symmetric ``watch: recovered.`` line prints. Both are never logged.

    Time-since-block narration (NOTE-2): when ``client`` is provided and the
    drain produced events, the "last block ~N min ago" suffix is computed
    ONCE per drain via :func:`_last_block_suffix` and appended to each event's
    narration. Any failure yields no suffix (fail-closed).

    Returns:
        The number of events surfaced this drain (0 when none / disabled).
        The REPL records this value-free count into the session summary
        (R13) so long-session context notes how many watch events occurred.
    """
    if watcher is None:
        return 0
    try:
        if not watcher.poll_due():
            return 0
        events = watcher.tick()
        recovered = watcher.mark_poll_succeeded()
        if recovered:
            # TCK-UX-012(c): the failure streak ENDED (mark_poll_succeeded
            # answers True only after at least one prior failure) — ONE
            # value-free recovery line, symmetric to the once-per-streak
            # failure line below. A healthy poll prints nothing.
            output_fn("watch: recovered.")
        suffix = (
            _last_block_suffix(client)
            if (client is not None and events)
            else None
        )
        # TCK-CHAT-001: a watch event PRINTS an own address to the user,
        # so each event address gets its stable registry number here
        # (idempotent first-showing write). A registry failure degrades
        # the NUMBER only — the surfacing itself never rides on it.
        numbers: dict[str, int] = {}
        if store is not None:
            wallet = store.get_active_wallet()
            if wallet is not None:
                for event in events:
                    if not event.address or event.address in numbers:
                        continue
                    try:
                        numbers[event.address] = store.note_address_shown(
                            wallet.id, event.address
                        ).number
                    except (StoreError, sqlite3.Error):
                        continue
        for event in events:
            output_fn(
                sanitize_tool_output(
                    _narrate_incoming_event(
                        event, suffix, number=numbers.get(event.address or "")
                    )
                )
            )
        return len(events)
    except (ChainError, wallet_scan.ScanError, StoreError, sqlite3.Error, WatchKeyError):
        # Fail open: a background-poll failure must never interrupt the chat.
        # NOTE-1 (TCK-UX-012(c)): surface a throttled, value-free line ONCE
        # per failure streak (the retry delay is the watcher's own resolved
        # interval — never a hardcoded number). Never logged.
        if watcher.mark_poll_failed():
            output_fn(f"watch: check failed — retrying in ~{watcher.interval_s:g}s")
        return 0


# ------------------------------------------------- engine pump (TCK-WEB-001, ADR-0024 §3)

#: Event kinds. The CLI sink renders ``text``/``progress`` exactly as the
#: pre-web REPL wrote them (output_fn line / raw stdout char); the web
#: transport (WEB-002) maps them onto SSE frames.
EVENT_TEXT: Final[str] = "text"
EVENT_PROGRESS: Final[str] = "progress"
EVENT_TURN_END: Final[str] = "turn_end"
#: TCK-LAUNCH-002: one model-download progress tick. Payload is a JSON
#: document of INTS ONLY ({"downloaded": int, "total": int|None, "pct":
#: int|None}) — never a path, never a filename, never wallet data, so no
#: scrubbing is needed (nothing string-shaped can enter it). The web client
#: renders an inline progress bar; the CLI sink re-renders one percent line
#: in place (carriage-return, like the scan dots).
EVENT_MODEL_PROGRESS: Final[str] = "model_progress"

#: TCK-WEB-011 (MW-10 #2): the user's OWN utterance echoed onto the shared
#: event bus, so EVERY other tab renders the user's message (the transcript
#: is a shared session — ADR-0010; server messages already fan out to all
#: tabs, the submitter's local echo did not). Emitted at the pump's single
#: string-command choke point, so every submit path rides it (free text,
#: canonical action utterances, quick actions, slash commands). The payload
#: is the utterance verbatim (sanitized exactly like the transcript echo
#: path, :func:`sanitize_tool_output`) and NOTHING else — no token, no
#: internal state. It carries the event's own monotonic id, so the
#: SUBMITTING tab suppresses the echo against its pending local echo
#: (client-side dedupe; the engine cannot see tabs). The CLI sink ignores
#: the kind (the terminal already shows the typed line — byte-identical
#: output, the WEB-001 seam); the ring buffer replays it like any event.
EVENT_USER_TEXT: Final[str] = "user_text"

#: Command token the web transport stamps on a typed ``/state`` snapshot
#: request (TCK-WEB-003). Recognized ONLY as the ``command`` label of a
#: :class:`StateSnapshotRequest` (below) — it is never a model turn and never
#: a chat line, so nothing sensitive can ride it.
STATE_SNAPSHOT_COMMAND: Final[str] = "/state"

#: ``/state`` snapshot schema tags (value-free; a fixed literal, never data).
#: ``state/1`` = typed snapshot (flow + watch + scan); ``state/0`` = the
#: minimal transport-only shape served while the engine is busy (the client
#: tolerates BOTH — it is written to key off the transport fields, which are
#: unchanged).
#: TCK-WEB-005 DECISION: the scan fields (``scan_state``,
#: ``first_scan_complete``) are ADDITIVE under ``state/1`` — the tag is NOT
#: bumped. The client (and every future one) reads a state/1 snapshot by
#: NAMED key and ignores unknown keys, and the pinned static client refuses
#: any tag other than ``state/0``/``state/1``; a ``state/2`` bump would
#: blind-side the shipped UI for zero new information. Rule for the next
#: change: additive = keep the tag; any rename/removal = bump and update the
#: client in the same ticket.
STATE_SCHEMA: Final[str] = "state/1"
STATE_SCHEMA_TRANSPORT_ONLY: Final[str] = "state/0"


@dataclass(frozen=True)
class EngineEvent:
    """One engine output event with a strictly monotonic id (never reused,
    never reordered). The id is the ring-buffer/replay key WEB-002 consumes.
    """

    id: int
    kind: str
    payload: str


class EventEmitter:
    """Monotonic-id event sink — the transport-agnostic output end of the
    pump. :meth:`text` is the ``output_fn``-shaped closure every engine
    layer keeps calling unchanged; ``emit`` covers progress/turn markers.
    Sinks must be fast and thread-safe-for-one-writer (the engine thread is
    the only emitter); buffering/replay lives above this seam (WEB-002).
    """

    def __init__(self, sink: Callable[[EngineEvent], None]) -> None:
        self._sink = sink
        self._next_id = 0

    def emit(self, kind: str, payload: str = "") -> EngineEvent:
        event = EngineEvent(id=self._next_id + 1, kind=kind, payload=payload)
        self._next_id += 1
        self._sink(event)
        return event

    def text(self, payload: str) -> None:
        """``output_fn``-shaped adapter: every engine line becomes an event."""
        self.emit(EVENT_TEXT, payload)


def _cli_model_progress_line(payload: str) -> str:
    """Render one model-download tick as an in-place carriage-return line.

    Input is the int-only JSON document built by :class:`ModelDownloadFlow`
    (never parsed text from the child); a malformed payload renders a bare
    "downloading" nudge, never raw bytes. Display formatting of tool
    integers only — the UI computes nothing.
    """
    try:
        data = json.loads(payload)
        pct = data["pct"] if isinstance(data["pct"], int) else None
        downloaded = data["downloaded"] if isinstance(data["downloaded"], int) else None
        total = data["total"] if isinstance(data.get("total"), int) else None
    except (ValueError, KeyError, TypeError):
        pct, downloaded, total = None, None, None
    if downloaded is None:
        return "\r  downloading the model..."
    shown = f"\r  downloading the model: {downloaded // (1 << 20)} MiB"
    if total:
        shown += f" of {total // (1 << 20)} MiB"
    if pct is not None:
        shown += f" ({pct}%)"
    return shown + " "


def cli_sink(output_fn: Callable[[str], None]) -> Callable[[EngineEvent], None]:
    """The CLI rendering of the event stream — byte-identical to the old
    REPL for text/progress (output_fn line / raw stdout char; scan dots,
    the closing newline), plus the TCK-LAUNCH-002 in-place model-download
    percent line. Markers stay invisible, and so does the TCK-WEB-011
    ``user_text`` echo (the terminal already shows what the user typed —
    the kind exists for the multi-tab web transcript).
    """

    def sink(event: EngineEvent) -> None:
        if event.kind == EVENT_TEXT:
            output_fn(event.payload)
        elif event.kind == EVENT_PROGRESS:
            sys.stdout.write(event.payload)
            sys.stdout.flush()
        elif event.kind == EVENT_MODEL_PROGRESS:
            sys.stdout.write(_cli_model_progress_line(event.payload))
            sys.stdout.flush()

    return sink


def cli_emitter(output_fn: Callable[[str], None]) -> EventEmitter:
    """An :class:`EventEmitter` that reproduces the pre-web CLI exactly."""
    return EventEmitter(cli_sink(output_fn))


# ------------------------------------------------------- app logging (TCK-APP-LOG-001)
#
# Everything the user should see routes through ``_Output``: narration to the
# terminal (CLI, unchanged) or the SSE emitter (web); errors/warnings to the
# console + the per-launch log file in BOTH modes (one code path). Log files
# live in a ``logs/`` dir beside the store DB (derived at runtime from the
# existing ``Settings.store_path`` — config.py untouched) and carry launch
# metadata + errors/warnings only — never narration, never key material.

#: Version-ish string for the launch-metadata log line (keep in sync with
#: pyproject.toml / the chain client's ``_CLIENT_NAME``). Never key material.
_APP_VERSION: Final[str] = "0.1.0"


class _Log:
    """Append-only per-launch error log (``logs/launch-YYYYMMDD-HHMMSS.log``
    beside the store DB). Value-free by construction: only launch metadata and
    error/warning lines, never narration or key material. Per-line size is
    bounded; a directory-creation failure degrades to console-only with a
    one-line note and is never fatal (``write`` no-ops without a handle)."""

    _LINE_MAX: Final[int] = 1000

    def __init__(self, store_path: str, mode: str) -> None:
        self._fh: TextIO | None = None
        try:
            log_dir = Path(store_path).parent / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            self._fh = open(  # noqa: SIM115 — long-lived per-launch handle
                log_dir / f"launch-{time.strftime('%Y%m%d-%H%M%S')}.log",
                "a",
                encoding="utf-8",
            )
        except OSError:
            self._fh = None
            sys.stderr.write(
                "note: could not open the error log (logs/) — errors print to "
                "the console only.\n"
            )
            sys.stderr.flush()
            return
        self.write(
            "INFO",
            f"launch mode={mode} app=local-wallet/{_APP_VERSION}",
        )

    def write(self, level: str, line: str) -> None:
        fh = self._fh
        if fh is None:
            return
        try:
            line = line.replace("\n", " ").strip()
            if len(line) > self._LINE_MAX:
                line = line[: self._LINE_MAX] + "…"
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {level} {line}\n")
            fh.flush()
        except OSError:
            self._fh = None  # a broken log degrades to console-only, never fatal

    def error(self, line: str) -> None:
        self.write("ERROR", line)

    def warning(self, line: str) -> None:
        self.write("WARN", line)

    def close(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass


class _Output:
    """Mode-aware output router (TCK-APP-LOG-001). ``__call__`` is the
    narration channel (the ``output_fn``-shaped surface every engine layer
    keeps calling): terminal in CLI mode, the SSE emitter in web mode.
    ``error``/``warning`` write to the log file AND the console (stderr in
    web, the terminal in CLI — unchanged); ``console`` is launch-critical
    terminal output (URL/token/shutdown) in web mode. In web mode narration
    is buffered until the engine emitter is bound (:meth:`bind_emitter`) so
    the startup banner reaches the browser, then routes directly — all on the
    engine thread (one emitter writer).

    TCK-UX-012(a): in WEB mode every narration line also closes its turn with
    a ``turn_end`` marker. The browser renders one bubble per turn (it groups
    ``text`` events until the marker), and everything this channel carries in
    web mode is a standalone startup line (the bootstrap banner, buffered or
    post-bind, and the provisioning path's re-run of it) — without the
    delimiter the whole banner, and the first reply after it, arrived as ONE
    merged bubble. The emission was already one ``output_fn`` call per line
    (verified); this closes the web-side merge at its actual source. The CLI
    terminal never sees a marker (its sink ignores the kind — byte-identical
    output); mid-turn narration in web flows through the pump's own emitter
    text channel, untouched here.
    """

    def __init__(
        self,
        *,
        web: bool,
        terminal: Callable[[str], None],
        log: _Log,
    ) -> None:
        self._web = web
        self._terminal = terminal
        self._log = log
        self._emitter: EventEmitter | None = None
        self._buffered: list[str] = []

    def bind_emitter(self, emitter: EventEmitter) -> None:
        """Attach the engine emitter (once available) and flush any buffered
        startup narration to it — each line as its own closed turn (see the
        class docstring, TCK-UX-012(a)). Engine-thread only."""
        self._emitter = emitter
        for line in self._buffered:
            emitter.text(line)
            emitter.emit(EVENT_TURN_END)
        self._buffered.clear()

    def __call__(self, line: str) -> None:  # narration
        if not self._web:
            self._terminal(line)
        elif self._emitter is not None:
            self._emitter.text(line)
            # Buffered lines get the same closer at flush time (above): every
            # web narration line is one closed bubble (TCK-UX-012(a)).
            self._emitter.emit(EVENT_TURN_END)
        else:
            self._buffered.append(line)

    def error(self, line: str) -> None:
        self._log.error(line)
        self._console(line)

    def warning(self, line: str) -> None:
        self._log.warning(line)
        self._console(line)

    def log_error(self, line: str) -> None:
        """Log an error WITHOUT a console echo (for a line already written to
        stderr by its own call site — preserves the CLI's original channel)."""
        self._log.error(line)

    def close(self) -> None:
        """Close the per-launch log file (idempotent; safe after any exit)."""
        self._log.close()

    def console(self, line: str) -> None:
        """Launch-critical terminal output: the terminal in BOTH modes, never
        buffered, never logged (URL/token/shutdown lines)."""
        self._terminal(line)

    def _console(self, line: str) -> None:
        if self._web:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
        else:
            self._terminal(line)


# ------------------------------------------- chain worker (TCK-SCAN-003, ADR-0022)


def _has_completed_scan(store: Store, wallet_id: int) -> bool:
    """Whether the store carries a completed-scan cursor (the durable
    first-scan-completion record ADR-0022 decision 5 names). A read failure
    is treated as "not completed" (fail closed toward ``stale``)."""
    try:
        return store.get_sync_state(wallet_id, wallet_scan.CURSOR_KEY) is not None
    except (StoreError, sqlite3.Error):
        return False


def _public_consent_recorded(store: Store) -> bool:
    """Whether the EXPLICIT public-backend consent record exists
    (:data:`BACKEND_CHOICE_SETTING` == ``"public"``, written ONLY by a
    warned choice — the onboarding conversation, the chat public answer, or
    :func:`set_public_backend_consent`). Fail closed: an unreadable record
    counts as "never chose" (defer + ask, never leak-by-accident). Since
    TCK-DESCOPE-M3A the record names a concrete server —
    :data:`~localwallet.config.PUBLIC_ELECTRUM_URL`, folded onto the
    selection ladder by :func:`_wire` and the consent seam — never a silent
    mempool.space default."""
    try:
        return store.get_setting(BACKEND_CHOICE_SETTING) == BACKEND_CHOICE_PUBLIC
    except (StoreError, sqlite3.Error):
        return False


def _backend_resolved(effective_backend: str | None, store: Store) -> bool:
    """THE single source of truth for "a chain backend has been chosen"
    (TCK-ONB-006; ADR-0022 amendment 1 + ADR-0023 amendment 2, as amended
    by TCK-DESCOPE-M3A). Resolved = a URL on any rung of the resolution
    ladder (env > config file > stored — exactly what
    :func:`resolve_chain_base_url` returns), OR an explicit public opt-in
    record (:data:`_public_consent_recorded`) — which resolves the wallet
    onto the NAMED public Electrum server, never a silent default. An UNSET
    stored rung means "never chose", NOT "chose public" — that's why the
    marker exists. While unresolved, a first-run startup scan holds at
    ``awaiting_backend`` and the onboarding ask (re-)arms; a headless
    launch REFUSES the scan (:data:`HEADLESS_BACKEND_REFUSAL`)."""
    if effective_backend is not None:
        return True
    return _public_consent_recorded(store)


def set_public_backend_consent(store: Store, swap: ChainBackendFlow | None) -> bool:
    """The ENGINE-SIDE way to record an EXPLICIT public-backend consent —
    the seam the web consent button (TCK-PRIVACY-001B) and the chat public
    answer ride. Since TCK-DESCOPE-M3A "public" is a NAMED server: two
    effects, identical to the warned CLI conversation's public branch
    (:meth:`OnboardingFlow._accept_public`): write the ONB-006 marker
    (:data:`BACKEND_CHOICE_SETTING` = ``"public"``, so every FUTURE launch
    resolves onto the public Electrum server through the same fold), then
    INSTALL :data:`~localwallet.config.PUBLIC_ELECTRUM_URL` as the live
    wallet client through the hot-swap seam
    (:meth:`ChainBackendFlow.install_public`) and release a HELD first-run
    startup scan ON IT. There is no public-default client to release onto
    anymore — but consent only ever INSTALLS into a HELD (unresolved) scan:
    a POST while a RESOLVED backend serves records the choice and moves
    NOTHING (code-review fix 2; the sanctioned resolved→public switch is
    /setup's revert, never a stray consent press).

    Callers must be consent itself, never a proxy for it: closing the
    onboarding pane, asking a balance, skipping the ask, or any other user
    action implies NOTHING here (an unset rung means "never chose").
    ENGINE-THREAD ONLY (the pump's thread owns the store, the gate and the
    scan — like every typed request). The record write is best-effort with
    the same fail-closed direction as the CLI path: a failed write means
    the ask re-appears next launch (toward asking, never toward leaking);
    THIS session's consent and release stand either way.

    Returns whether the held first-run scan actually started loading on
    the public Electrum server (security review F2 contract: report
    "loading now" only on ``True``; ``False`` when no scan is held, no
    engine swap controller exists, or planning failed and it stood down)."""
    try:
        store.set_setting(BACKEND_CHOICE_SETTING, BACKEND_CHOICE_PUBLIC)
    except (StoreError, sqlite3.Error):
        pass
    return swap.install_public() if swap is not None else False


def _freshness(store: Store, wallet_id: int, gate: StartupScan | None) -> str:
    """The deterministic, TOOL-owned ``freshness`` flag (ADR-0022 decision 5).

    ``stale`` while the (enabled) startup scan has not completed, or when the
    cache was never populated by a completed scan; ``fresh`` once a scan has
    completed and the store carries its cursor. Computed purely from the
    scan's completion state — the model never authors it, and it never
    changes the answer's values (balances/addresses stay verbatim from the
    cache); it is narration-only.
    """
    if gate is not None and gate.first_scan_incomplete:
        return FRESHNESS_STALE
    return FRESHNESS_FRESH if _has_completed_scan(store, wallet_id) else FRESHNESS_STALE


class StartupScan:
    """The startup-scan state gate (ADR-0022 decisions 5/6), shared with the
    handlers as ``scan_gate``.

    A tiny closed state machine owned by the ENGINE thread: only the pump
    (engine) flips it via :meth:`mark_running`/:meth:`mark_done`/
    :meth:`mark_skipped` (and :meth:`ScanFlow.set_startup_deferred`/
    :meth:`ScanFlow.release_backend` for the TCK-ONB-006 pair); the chain
    worker never touches it (it delivers its result through the command
    queue). Handlers only read it. ``disabled`` is the "no startup scan
    configured" state (``AUTO_SCAN=0`` or a table built without a gate) —
    the pre-split lazy behavior applies and no ``create_tx`` block is
    imposed. ``awaiting_backend`` (TCK-ONB-006, ADR-0022 amendment 1) is
    the first-run EXCEPTION: a startup scan IS configured but HELD until
    the backend choice resolves — no chain call may happen before the
    user picked (or accepted) a server.

    ``first_scan_incomplete`` (awaiting/pending/running) is the
    load-bearing gate: ``create_tx`` refuses while it is set (value
    movement waits for the first scan), the lazy in-handler scan stands
    down (the worker owns the chain — and while awaiting, NOTHING does),
    and cache reads are ``stale``-flagged. Once the scan completes
    (``done``) or is skipped after a failure (``skipped``), it clears.
    """

    __slots__ = ("_state",)

    def __init__(self, *, enabled: bool, deferred: bool = False) -> None:
        if not enabled:
            self._state = "disabled"
        else:
            self._state = "awaiting_backend" if deferred else "pending"

    @property
    def enabled(self) -> bool:
        return self._state != "disabled"

    @property
    def state(self) -> str:
        """The gate's state as a CLOSED enum name (TCK-WEB-005; the
        ``awaiting_backend`` member added by TCK-ONB-006 is additive under
        the unchanged ``state/1`` snapshot tag): exactly one of
        ``disabled``/``awaiting_backend``/``pending``/``running``/``done``/
        ``skipped``. The web ``/state`` snapshot exposes this string and
        nothing else about the scan — it is a name, never data (no
        progress/counts: a percentage would leak wallet size through the
        door of a progress bar)."""
        return self._state

    @property
    def in_progress(self) -> bool:
        return self._state in ("awaiting_backend", "pending", "running")

    @property
    def complete(self) -> bool:
        return self._state == "done"

    @property
    def first_scan_incomplete(self) -> bool:
        """Stale-flag predicate: a startup scan is configured but its first
        scan has not completed (awaiting a backend choice, pending, or
        running)."""
        return self._state in ("awaiting_backend", "pending", "running")

    def mark_running(self) -> None:
        self._state = "running"

    def mark_done(self) -> None:
        self._state = "done"

    def mark_skipped(self) -> None:
        self._state = "skipped"

    def _rearm(self, state: str) -> None:
        """Rebind the gate's state IN PLACE (TCK-BACKEND-002 stale-gate fix,
        LOW): a backend release/re-arm mutates the SAME object instead of
        replacing it, so the handler table's ``scan_gate`` (captured at
        wiring time) keeps the one object the pump later flips to
        ``done``/``skipped`` — a stale pre-release gate can never outlive a
        release and strand every handler on ``awaiting_backend`` forever."""
        self._state = state


class _ScanTick:
    """A value-free progress marker the worker enqueues (one per probed
    address) so the pump renders a bare ``.`` between turns. Carries no
    address/index/amount data (TCK-UX-001 progress contract, re-hosted on
    the engine queue for the non-blocking scan)."""

    __slots__ = ()


@dataclass(frozen=True)
class _ScanDone:
    """The worker's terminal delivery: ``value`` is the immutable
    :class:`~localwallet.wallet.scan.ScanRecords` (``ok=True``) or the
    exception the chain/scan phase raised (``ok=False``). Enqueued onto the
    command queue so the ENGINE thread persists it (ADR-0022 decision 3)."""

    ok: bool
    value: object


#: Worker-queue shutdown sentinel.
_WORKER_STOP: Final[object] = object()


@dataclass
class _WorkerJob:
    """One submitted fetch job: a completion signal + result slot the engine
    reads only after :attr:`done` is set (``ok``/``value`` mirror the
    :class:`_ScanDone` fields)."""

    done: threading.Event = dataclass_field(default_factory=threading.Event)
    ok: bool = False
    value: object = None


class ChainWorker:
    """The dedicated chain-I/O thread (ADR-0022 decision 2, ADR-0024 §4).

    Runs scan/watch :func:`~localwallet.wallet.scan.fetch_scan` jobs off the
    engine thread against a :class:`ScanPlan` snapshot + the chain client and
    hands back immutable :class:`ScanRecords`. It holds NO ``Store`` reference
    — the whole point of the split — so cross-thread sqlite is impossible by
    construction; only the engine persists (decision 3). One worker thread
    processes jobs serially, so at most one chain fetch is ever in flight.

    The startup scan uses :meth:`submit` (async; the caller pumps the command
    queue and persists on completion). The lazy in-turn scan and the watch
    poll use :meth:`scan` (submit + block until the worker's own job finishes;
    the engine waits between/within a turn, exactly as the pre-split
    synchronous scan did, but with no chain I/O on the engine thread).
    """

    def __init__(self, client: ChainClient | None) -> None:
        # TCK-DESCOPE-M3A: ``None`` = the wallet backend is UNRESOLVED. No
        # job is ever submitted while the startup gate holds, so the worker
        # thread never dereferences it; a consent/save installs the real
        # client through :meth:`set_client` before anything can fetch.
        self._client = client
        self._jobs: queue.Queue[Any] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="chain-worker", daemon=True)
        self._thread.start()

    def submit(
        self,
        plan: wallet_scan.ScanPlan,
        *,
        on_progress: Callable[[], None] | None = None,
        on_result: Callable[[bool, object], None] | None = None,
    ) -> _WorkerJob:
        """Queue a fetch job. ``on_progress`` (worker thread) fires per probed
        address; ``on_result`` (worker thread) fires once at completion. Both
        callbacks run on the worker thread and must be thread-safe queue
        puts only — the app pushes :class:`_ScanTick`/:class:`_ScanDone`
        markers onto the command queue so the engine does the persisting."""
        job = _WorkerJob()
        self._jobs.put((job, plan, on_progress, on_result))
        return job

    def scan(self, plan: wallet_scan.ScanPlan) -> wallet_scan.ScanRecords:
        """Run one blocking fetch on the worker and return its record set
        (raising the job's exception on THIS thread so callers surface it
        through their own error mapping). The chain I/O never touches the
        calling thread; the caller persists the returned records. Only call
        when no async job is in flight (the gate guarantees this) — the
        single worker processes jobs serially."""
        job = self.submit(plan)
        job.done.wait()
        if not job.ok:
            raise job.value  # type: ignore[misc]
        return job.value  # type: ignore[return-value]

    def set_client(self, client: ChainClient) -> None:
        """Rebind the fetch client IN PLACE (TCK-BACKEND-002 hot-swap;
        ADR-0018 amendment). ENGINE-THREAD ONLY, and only while NO job is in
        flight — the same precondition the blocking :meth:`scan` documents
        (the swap controller checks the scan gate before installing): the
        worker thread reads ``self._client`` once at the START of each job
        and never touches it mid-fetch, so a swap between jobs is atomic by
        construction. No worker/thread rebuild: the queue, the thread, and
        every :class:`ScanFlow` bound method ride through unchanged."""
        self._client = client

    def _run(self) -> None:
        while True:
            item = self._jobs.get()
            if item is _WORKER_STOP:
                return
            job, plan, on_progress, on_result = item  # type: ignore[misc]
            ok = True
            value: object = None
            try:
                value = wallet_scan.fetch_scan(plan, self._client, progress_fn=on_progress)
            except BaseException as exc:  # noqa: BLE001 — carried to the engine
                ok, value = False, exc
            job.ok, job.value = ok, value
            job.done.set()
            if on_result is not None:
                on_result(ok, value)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop after the current job drains (an in-flight fetch is never
        cancelled; it is bounded by the chain client's own timeouts). The
        thread is joined up to ``timeout`` and otherwise abandoned (daemon) —
        an abandoned job delivers to nobody, and the store stays consistent
        because only the engine ever persists."""
        self._jobs.put(_WORKER_STOP)
        self._thread.join(timeout)


#: The chain/ endpoint kind a bitcoind ``scantxoutset`` refusal carries
#: (the error message prefix; the ONLY part of the failure detail the scan
#: guidance below pattern-matches on — value-free by the chain contract).
_KIND_UTXO_SCAN_REFUSAL: Final[str] = "utxo-scan"

#: The value-free suspect list for an rpc-error UTXO-scan refusal
#: (TCK-BACKEND-004): the node answered at the RPC layer and refused the
#: request — name the suspects, invent nothing. The numeric RPC code rides
#: the class tag; the server's message text never surfaces anywhere.
_SCAN_RPC_REFUSAL_HINT: Final[str] = (
    " The request was rejected by the server's RPC layer — check the RPC"
    " role/permissions for this user, the request shape, or the scan timeout."
)


def _scan_failure_suffix(exc: BaseException) -> str:
    """The ``[class=… exc=…]`` debug suffix (TCK-DIAG-001, plus the
    value-free RPC code of an ``rpc-error`` refusal — TCK-DIAG-002) and the
    scantxoutset suspect-list guidance, shared by EVERY scan-failure line
    (the engine-thread :meth:`ScanFlow._warn` and the launch-time sync
    rescan path format the same sentence)."""
    fc, name, extra = _failure_parts(exc)
    suffix = f" [class={fc} exc={name}{extra}]"
    if fc == RPC_ERROR and _KIND_UTXO_SCAN_REFUSAL in str(exc):
        suffix += _SCAN_RPC_REFUSAL_HINT
    return suffix


class ScanFlow:
    """The engine-side startup-scan controller + the shared blocking scan
    (TCK-SCAN-003, ADR-0022 decisions 1/3/4).

    Owns the :class:`ChainWorker`, the :class:`StartupScan` gate, and the
    store/wallet needed to persist. Only the pump (engine) thread mutates it:
    :meth:`begin` submits the non-blocking startup fetch, and
    :meth:`handle_command` persists the delivered record set + narrates the
    completion (or the scrubbed failure warning), flipping the gate.
    :meth:`scan_now` is the synchronous composition (plan → worker fetch →
    engine persist) that the lazy in-handler scan and the watch poll ride, so
    every scan/watch chain fetch runs on the worker and every persist runs on
    the engine (the P5-001 "full scan per poll on the engine thread" cost is
    retired). The web half (surfacing this in the browser) is TCK-WEB-005.
    """

    def __init__(
        self,
        store: Store,
        wallet: WalletRecord,
        worker: ChainWorker,
        *,
        gap_limit: int | None,
        startup_plan: wallet_scan.ScanPlan | None = None,
        rescan: bool = False,
        output: _Output | None = None,
    ) -> None:
        self._store = store
        self._wallet = wallet
        self._wallet_id = wallet.id
        self._worker = worker
        self._gap_limit = gap_limit
        self._startup_plan = startup_plan
        self._rescan = rescan
        self._output = output
        self._commands: queue.Queue[Any] | None = None
        self.gate = StartupScan(enabled=startup_plan is not None)
        self._started = False
        #: One-shot first-run narration hook (TCK-ONB-003 step 4): called
        #: with the pump's output function ONCE, when the FIRST startup
        #: scan persists successfully (never on failure — the load did not
        #: complete). ``_wire`` arms it only for an onboarding session.
        self.on_first_scan_done: Callable[[Callable[[str], None]], None] | None = None

    def set_startup(self, plan: wallet_scan.ScanPlan, *, rescan: bool = False) -> None:
        """Arm the non-blocking startup scan (engine-thread wiring call).
        The gate enables IMMEDIATELY — the stale flags and the ``create_tx``
        block apply from the moment wiring returns, before the pump even
        starts the fetch on the worker (ADR-0022 decision 6: value movement
        waits for the first scan; there is no unlocked window)."""
        self._startup_plan = plan
        self._rescan = rescan
        self.gate._rearm("pending")

    def set_startup_deferred(self, *, rescan: bool = False) -> None:
        """TCK-ONB-006 (ADR-0022 amendment 1, the first-run exception): arm
        the startup scan in HELD state — the gate reads ``awaiting_backend``
        and NO chain call ever leaves the process until the backend choice
        resolves via :meth:`release_backend`. Staleness semantics are
        identical to ``pending``: cache reads are stale-flagged,
        ``create_tx`` refuses, the lazy in-handler scan and the watch drain
        stand down (nothing may probe a server the user never picked).
        Engine-thread wiring call. Armed REGARDLESS of AUTO_SCAN
        (security review F1): on an AUTO_SCAN=0 launch nothing was ever
        planned — the hold exists to keep the lazy handlers and the watch
        drain stood down until the choice resolves."""
        self._rescan = rescan
        self.gate._rearm("awaiting_backend")

    def release_backend(self) -> bool:
        """TCK-ONB-006: the backend choice resolved. Two callers, both
        loading from the user's CHOSEN server, never a refused one:
        an explicit PUBLIC consent (the flow's own ``public_chosen`` hook,
        releasing the still-public-default client the user just accepted),
        or a TCK-BACKEND-002 own-server HOT-SWAP (the swap has already moved
        the worker onto the chosen client before calling this). Plan NOW on
        the engine thread (fresh store reads) and start the held startup
        scan. No-op unless the gate is ``awaiting_backend``.

        Returns whether the load actually started (security review F2): the
        consent ack may only claim "loading now" when this says ``True`` —
        a no-op or a failed plan (scan stood down) says ``False``."""
        if self.gate.state != "awaiting_backend":
            return False
        # The shared plan→arm→begin tail (TCK-UX-012(d) dedup): a plan
        # failure here can only be a broken store (planning is
        # store-reads-only and network-free) — stand the startup scan down
        # exactly like the wiring-time planning failure did, and let the
        # handlers' lazy path (now unlocked) retry per turn. The tail can
        # only answer False on that failure (a successful plan always arms
        # the gate ``pending`` before the submit), so ``not started`` IS
        # the planning failure.
        if not self._plan_arm_begin(rebuild=self._rescan):
            self.gate.mark_skipped()
            return False
        return True

    # ------------------------------------------------------ non-blocking startup

    def attach(self, commands: queue.Queue[Any]) -> None:
        """Bind the pump's command queue (the worker delivers here)."""
        self._commands = commands

    def begin(self) -> None:
        """Submit the non-blocking startup scan once (the pump calls this at
        the top of its loop, AFTER the prompt is already live — ADR-0022
        decision 1). No-op when no startup scan is configured."""
        if self._started or self._startup_plan is None or self._commands is None:
            return
        self._started = True
        self.gate.mark_running()
        commands = self._commands
        self._worker.submit(
            self._startup_plan,
            on_progress=lambda: commands.put(_ScanTick()),
            on_result=lambda ok, value: commands.put(_ScanDone(ok, value)),
        )

    def _plan_arm_begin(self, *, rebuild: bool) -> bool:
        """The ONE plan→arm→begin tail shared by :meth:`release_backend`,
        :meth:`resync_now` and :meth:`kick_scan` (TCK-UX-012(d) review
        MINOR dedup; behavior identical): plan on engine-thread store reads
        (network-free), arm the gate via :meth:`set_startup`, and let
        :meth:`begin` submit the fetch exactly once (``_started`` reset first
        so a finished earlier scan cannot block the fresh plan). ``rebuild``
        is the ONLY per-caller difference — ``resync_now`` forces ``True``
        (the ``--rescan`` repair semantics); the release/kick paths carry the
        launch's own ``self._rescan``. ``False`` when planning failed (a
        broken store — the callers decide their stand-down); after a
        successful plan the gate is always armed ``pending``/``running``, so
        the answer says whether the load is on."""
        try:
            plan = wallet_scan.plan_scan(
                self._store,
                self._wallet,
                gap_limit=self._gap_limit,
                rebuild=rebuild,
            )
        except (
            ChainError,
            wallet_scan.ScanError,
            WatchKeyError,
            StoreError,
            sqlite3.Error,
        ):
            return False
        self.set_startup(plan, rescan=rebuild)
        self._started = False  # let begin() submit the fresh plan once
        self.begin()
        return self.gate.state in ("pending", "running")

    def resync_now(self) -> bool:
        """The full REPAIR rescan as a triggerable action (TCK-BACKEND-002
        deliverable 5 — the ``Resync now`` button, the chain-swap resync, and
        the gap-limit-change resync all ride here): the SCAN-003 ``--rescan``
        rebuild semantics (re-derive the whole gap window from chain truth,
        "as though the zpub had been entered for the first time") re-run on
        the SAME flow, gate object, worker and command queue — the pump's
        existing ``_ScanTick``/``_ScanDone`` handling persists + narrates it
        exactly like the startup scan (the rescan summary line, since the
        rescan flag flips here).

        Tags survive by construction: ``persist_scan``'s write-set is the
        utxo/address/tx/sync-state tables only — ``coin_labels`` is a
        separate table the scan never touches (pinned in
        tests/test_backend_hotswap.py; store/db.py's own contract note).

        Concurrency guard (single-threaded truth, engine thread): ``False``
        when a scan already owns the worker (pending/running) or the startup
        scan is still HELD awaiting a backend choice (nothing to resync yet),
        or when the pump queue is not attached. While it runs, the gate is
        ``running`` again — stale-flagged reads, the ``create_tx`` block, and
        the watch stand-down behave exactly as during a startup rescan.
        """
        if self.gate.in_progress or self._commands is None:
            return False
        return self._plan_arm_begin(rebuild=True)

    def kick_scan(self) -> bool:
        """TCK-UX-011 (ADR-0022 amendment 2): start the background scan for
        a turn that STOOD the lazy inline scan down — a web/engine
        ``get_balance`` answers cache-served + stale + ``scan_pending`` and
        kicks HERE, so the honest "first scan running in the background"
        is literally true. ENGINE-thread only (the handler dispatch runs
        there): planning is store-reads-only and the fetch rides the ONE
        existing worker — no new threads, no store access off the engine.

        Idempotent no-op (``False``) when any scan already owns the worker
        (gate awaiting/pending/running — the double ``/balance`` never
        double-kicks), before the pump attaches its command queue, or when
        planning fails; otherwise arms the gate and starts the fetch
        exactly once (covers a disabled AUTO_SCAN=0 launch and a
        failed/abandoned startup scan — gate ``skipped`` — alike: the kick
        is a user-initiated load, not an auto scan, ADR-0022 amendment 1's
        distinction). Returns whether the load started."""
        if self.gate.in_progress or self._commands is None:
            return False
        return self._plan_arm_begin(rebuild=self._rescan)

    def handle_command(
        self,
        command: object,
        output_fn: Callable[[str], None],
        emitter: EventEmitter | None,
    ) -> bool:
        """Consume one scan event ON THE ENGINE THREAD; ``True`` when handled.
        :class:`_ScanTick` renders a bare dot; :class:`_ScanDone` closes the
        dot line, persists the record set (the ONLY store write of the scan),
        flips the gate, and narrates the completion/failure exactly as the
        pre-split blocking scan did."""
        if isinstance(command, _ScanTick):
            if emitter is not None:
                emitter.emit(EVENT_PROGRESS, ".")
            return True
        if isinstance(command, _ScanDone):
            self._finish(command, output_fn, emitter)
            return True
        return False

    def _finish(
        self,
        done: _ScanDone,
        output_fn: Callable[[str], None],
        emitter: EventEmitter | None,
    ) -> None:
        if emitter is not None:
            emitter.emit(EVENT_PROGRESS, "\n")  # close the progress-dot line
        if not done.ok:
            exc = done.value
            self.gate.mark_skipped()
            if isinstance(
                exc,
                (
                    ChainError,
                    wallet_scan.ScanError,
                    WatchKeyError,
                    StoreError,
                    sqlite3.Error,
                ),
            ):
                self._warn(output_fn, str(exc), exc)
                self._out_of_window(output_fn)
                return
            raise exc  # a genuine worker bug must not be swallowed
        try:
            summary = wallet_scan.persist_scan(self._store, done.value)  # engine thread
        except (StoreError, sqlite3.Error) as exc:
            self.gate.mark_skipped()
            self._warn(output_fn, str(exc), exc)
            self._out_of_window(output_fn)
            return
        self.gate.mark_done()
        output_fn(
            _rescan_summary_line(summary) if self._rescan else _scan_summary_line(summary)
        )
        self._out_of_window(output_fn)
        if self.on_first_scan_done is not None:
            # ADR-0023 step 4 (one-shot): the onboarding conversation's
            # load-complete line rides the same engine-thread narration as
            # the scan summary it follows.
            hook = self.on_first_scan_done
            self.on_first_scan_done = None
            hook(output_fn)

    def _out_of_window(self, output_fn: Callable[[str], None]) -> None:
        """The ADR-0009 warning as re-assessed by the scan that just landed
        (printed after the completion/failure narration, where the scan
        result's out-of-window write lives)."""
        out_of_window = _out_of_window_line(self._store, self._wallet_id)
        if out_of_window is not None:
            output_fn(out_of_window)

    def _warn(
        self,
        output_fn: Callable[[str], None],
        detail: str,
        exc: BaseException | None = None,
    ) -> None:
        """The scrubbed startup-failure line (scan/chain/store/key errors are
        value-free by their layers' contracts). The REPL still runs; handlers
        surface store-empty/chain-down states per turn. With a ``_Output``
        router present (web/CLI launch), the line is a TCK-DIAG-001 warning
        (log + console, never the SSE stream); otherwise it falls back to the
        caller's narration channel (test harnesses)."""
        label = "rescan" if self._rescan else "startup scan"
        line = f"warning: {label} failed: {detail} — continuing with cached state."
        if exc is not None:
            line += _scan_failure_suffix(exc)
        out = self._output
        if out is not None:
            out.warning(line)
        else:
            output_fn(line)

    # ------------------------------------------------------------- blocking scan

    def scan_now(self) -> wallet_scan.ScanSummary:
        """One synchronous scan composed across the split: ``plan_scan``
        (engine) → worker ``fetch_scan`` (engine waits) → ``persist_scan``
        (engine). The lazy in-handler first scan and the watch poll both ride
        this, so all their chain I/O runs on the worker and all persistence on
        the engine. Behavior (ordering, failure semantics) matches the
        pre-split ``scan_wallet(store, client, wallet)``.

        Raises:
            ChainError / ScanError / WatchKeyError / StoreError: as the fused
                scan did (the worker re-raises its exception here).
        """
        plan = wallet_scan.plan_scan(
            self._store, self._wallet, gap_limit=self._gap_limit, rebuild=False
        )
        return wallet_scan.persist_scan(self._store, self._worker.scan(plan))

    @property
    def in_progress(self) -> bool:
        return self.gate.in_progress

    @property
    def first_scan_recorded(self) -> bool:
        """The DURABLE first-scan-completion fact (ADR-0022 decision 5's
        completed-scan cursor), read on the ENGINE thread. Unlike the gate
        (this session's startup-scan state) it survives restarts and covers
        the AUTO_SCAN=0 lazy path — the honest source for the web snapshot's
        ``first_scan_complete`` flag (TCK-WEB-005)."""
        return _has_completed_scan(self._store, self._wallet_id)

    @property
    def pending(self) -> bool:
        """True while a submitted startup scan result has not yet been
        handled (persisted + narrated) by the engine thread."""
        return self._started and self.gate.in_progress

    def drain_until_complete(
        self,
        output_fn: Callable[[str], None],
        emitter: EventEmitter | None,
        *,
        timeout: float = 8.0,
    ) -> None:
        """Session-end drain: collect any still-pending startup-scan events
        (blocking on the command queue) so the result is persisted + narrated
        exactly once before :func:`run` returns — the scan never silently
        vanishes. Non-scan commands queued behind the exit are dropped (the
        session is ending). Bounded by ``timeout`` (the fetch itself is
        bounded by the chain client's own timeouts); a scan that has already
        finished is a no-op."""
        if not self.pending or self._commands is None:
            return
        deadline = time.monotonic() + timeout
        while self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                command = self._commands.get(timeout=remaining)
            except queue.Empty:
                break
            self.handle_command(command, output_fn, emitter)


# ------------------------------------- model download (TCK-LAUNCH-002, ADR-0001)


@dataclass(frozen=True)
class _ModelProgress:
    """One reader-thread delivery: bytes so far + the expected total (either
    may be ``None`` when the server withheld Content-Length). Ints only,
    never a path or name — the pump re-emits them as the int-only JSON
    payload of :data:`EVENT_MODEL_PROGRESS`."""

    downloaded: int
    total: int | None


@dataclass(frozen=True)
class _ModelDone:
    """The reader thread's terminal delivery: ``ok`` = the pinned downloader
    exited 0 (its own contract: hash-verified before install, resumable
    ``.part`` kept on failure)."""

    ok: bool


class ModelDownloadFlow:
    """Engine-owned orchestrator for the pinned default-model download
    (TCK-LAUNCH-002, ADR-0001 amendment).

    The SAME lifecycle discipline as :class:`ScanFlow`: the state machine is
    mutated ONLY on the ENGINE (pump) thread; the worker here (one reader
    thread) does nothing but run the child and enqueue immutable int-only
    markers on the pump's command queue. The child process is
    ``models/download_model.py`` — the tracked, hash-pinned build-time
    downloader — invoked as an argument LIST (no shell, ever); the script's
    own verification contract (streaming SHA-256 against the manifest pin,
    refuse-to-install on mismatch, resumable ``.part``) is untouched.

    A closed state machine: ``absent`` (card armed) → ``running`` → ``ready``
    | ``failed``; ``declined`` is the user's "no" (quick-action list shown;
    ``/download`` re-arms the offer from any answered state). The
    concurrency guard is single-threaded truth: :meth:`start` only proceeds
    from ``absent``/``failed``, so ONE download at a time is structural.

    ``command`` is the test seam: production always runs the pinned script
    with ``--model <default> --json-progress``; tests inject a fast fake.
    The child's stdout is parsed for INT-ONLY JSON progress lines and
    otherwise IGNORED — no child text (paths, names, errors) ever reaches
    an event, a log, or the terminal; failures surface only as the canned
    value-free :data:`MODEL_DL_FAILED` line.
    """

    def __init__(
        self,
        *,
        model_name: str,
        command: Sequence[str] | None = None,
        join_timeout_s: float = 5.0,
    ) -> None:
        self.model_name = model_name
        self.state: str = "absent"
        self._command = list(
            command
            if command is not None
            else [
                sys.executable,
                str(_MODEL_DOWNLOAD_SCRIPT),
                "--model",
                model_name,
                "--json-progress",
            ]
        )
        self._join_timeout_s = join_timeout_s
        self._commands: queue.Queue[Any] | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        # Guards the spawn-vs-cancel race: QUIT may arrive BEFORE the reader
        # thread has created the child (it would then orphan a live
        # downloader). ``cancel`` flips the flag under the lock; the reader
        # registers the proc under the same lock and terminates it itself if
        # the session was already torn down.
        self._lock = threading.Lock()
        self._cancelled = False

    def attach(self, commands: queue.Queue[Any]) -> None:
        """Bind the pump's command queue (the reader delivers onto it)."""
        self._commands = commands

    def start(self) -> bool:
        """Spawn the downloader (engine thread). ``False`` = the guard says
        no (already running, already installed, or no pump queue yet) — ONE
        download at a time is enforced HERE, at the single mutation point.
        ``declined`` re-arms (the user changed their mind via /download)."""
        if self.state not in ("absent", "failed", "declined") or (
            self._commands is None
        ):
            return False
        self.state = "running"
        self._cancelled = False
        self._reader = threading.Thread(target=self._run, name="model-download")
        self._reader.start()
        return True

    def _run(self) -> None:  # reader thread — queue puts ONLY
        commands = self._commands
        assert commands is not None  # set by start() on the engine thread
        try:
            # Deliberate stderr=DEVNULL: the downloader's messages may name
            # paths; the exit code is the whole story we surface. The human-
            # readable --check/--write-hash workflow stays a terminal tool.
            proc = subprocess.Popen(
                self._command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            commands.put(_ModelDone(ok=False))
            return
        with self._lock:
            self._proc = proc
            doomed = self._cancelled
        if doomed:  # QUIT landed between start() and the spawn
            proc.terminate()
        try:
            assert proc.stdout is not None  # PIPE above
            for raw in proc.stdout:
                line = raw.strip()
                if not line.startswith("{"):
                    continue  # the downloader's human prints (never echoed)
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                downloaded = data.get("downloaded")
                total = data.get("total")
                if not isinstance(downloaded, int) or isinstance(downloaded, bool):
                    continue
                if total is not None and (
                    not isinstance(total, int) or isinstance(total, bool)
                ):
                    total = None
                commands.put(_ModelProgress(downloaded=downloaded, total=total))
        except (OSError, ValueError):  # a broken pipe is a dead child
            pass
        finally:
            try:
                ok = proc.wait() == 0
            except OSError:
                ok = False
            commands.put(_ModelDone(ok=ok))

    def handle_command(
        self,
        command: object,
        output_fn: Callable[[str], None],
        emitter: EventEmitter | None,
    ) -> bool:
        """Consume one reader delivery ON THE ENGINE THREAD; ``True`` when
        handled. Progress relays as the int-only JSON
        :data:`EVENT_MODEL_PROGRESS`; the terminal marker flips the state,
        closes the CLI's in-place line, and narrates the canned outcome."""
        if isinstance(command, _ModelProgress):
            if emitter is not None:
                emitter.emit(
                    EVENT_MODEL_PROGRESS,
                    json.dumps(_model_progress_fields(command)),
                )
            return True
        if isinstance(command, _ModelDone):
            if emitter is not None:
                emitter.emit(EVENT_PROGRESS, "\n")  # close the in-place line
            self._proc = None
            self._reader = None
            self.state = "ready" if command.ok else "failed"
            output_fn(MODEL_DL_DONE if command.ok else MODEL_DL_FAILED)
            return True
        return False

    def decline(self) -> None:
        """The user's 'no': the offer is answered with the model-free
        actions. A running download is NOT cancelled by 'no' (it was
        already consented); declining only applies to the card states."""
        if self.state in ("absent", "failed"):
            self.state = "declined"

    def cancel(self) -> None:
        """Session end (pump exit / QUIT): terminate the child BOUNDED and
        join the reader — no orphaned downloader processes. The partial
        file is deliberately left in place: the pinned script RESUMES it on
        the next consented attempt (its documented contract). The
        ``_cancelled`` flag (set under the lock) also dooms a child that
        finishes spawning only AFTER this returns (the spawn-vs-QUIT race)."""
        with self._lock:
            self._cancelled = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=self._join_timeout_s)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=self._join_timeout_s)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    pass
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(self._join_timeout_s)


def _model_progress_fields(tick: _ModelProgress) -> dict[str, int | None]:
    """The int-only event payload (percent computed from the tool's byte
    counts — display arithmetic on tool output, the same class as txid
    shortening; clamped, never fabricated: ``pct`` is ``None`` without a
    known total)."""
    total = tick.total
    downloaded = max(tick.downloaded, 0)
    pct: int | None = None
    if total is not None and total > 0:
        pct = min(100, downloaded * 100 // total)
    return {"downloaded": downloaded, "total": total, "pct": pct}


# ------------------------------- model preload + launch checksum (TCK-LAUNCH-003)


@dataclass(frozen=True, slots=True)
class _PreloadStart:
    """Pump command: arm the background model preload + launch checksum.
    The CLI transport queues it at pump entry; the WEB launcher queues it
    only AFTER the URL/token launch lines have printed — the pinned
    wheel's model-build noise silencer dup2's /dev/null onto process fds
    1/2 for the build's duration (process-wide), and the printed token is
    the ONE line that must never fall into that window."""

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "PRELOAD_START"


#: Push this on the command queue to arm the preload (see
#: :class:`_PreloadStart`).
PRELOAD_START: Final = _PreloadStart()


@dataclass(frozen=True)
class _PreloadDone:
    """The loader thread's terminal delivery: ``ok`` = the llama runtime
    finished building. VALUE-FREE by construction (a flag only — the
    failure's message may name the model path; the error resurfaces on
    the next generate through the existing per-turn path, which is the
    whole reason the marker carries nothing else)."""

    ok: bool


@dataclass(frozen=True)
class _IntegrityDone:
    """The checksum thread's delivery: ``ok`` = the file hashed to the
    manifest's pinned sha256 (never the digests themselves)."""

    ok: bool


class ModelPreloadFlow:
    """Background preload + launch checksum for a resolved REAL local
    model (TCK-LAUNCH-003; the absent-file sibling is
    :class:`ModelDownloadFlow` — the two are mutually exclusive by
    construction: no file → card, file → preload).

    The SAME lifecycle discipline as every other engine flow
    (:class:`ScanFlow`, :class:`ModelDownloadFlow`): the closed state
    machine (``loading`` → ``ready`` | ``failed``) is mutated ONLY on the
    ENGINE (pump) thread in :meth:`handle_command`; the two daemon worker
    threads do nothing but read-only work and queue immutable markers.

    Why a BACKGROUND load thread rather than the ticket's engine-thread
    arm alternative: while the pump itself builds the model it cannot
    service the command queue — the required ``model_state='loading'``
    badge could never be SERVED, ``/state`` would time out for the whole
    build, and QUIT would stall. Off-thread the pump stays live: snapshots
    answer ``loading`` throughout, the terminal marker flips the state and
    emits ``turn_end`` so the browser re-reads /state, and only a model
    turn waits — INSIDE the runtime's build lock
    (:meth:`localwallet.agent.runtime.ModelRuntime.load`/``generate``),
    the ticket's "first query waits cleanly" serialization point: a
    mid-build ``generate`` blocks on the same lock the loader holds and
    then finds the finished runtime (never a double build of a multi-GB
    model, never an error, never a drop). The wait is bounded
    (:data:`~localwallet.agent.runtime.LOAD_WAIT_TIMEOUT_S`) and honest:
    it only expires on a genuinely wedged read, where the pre-change
    inline load would have hung the session exactly the same.

    Thread-safety evidence for OFF-THREAD construction (pinned wheel,
    llama-cpp-python 0.3.35): ``Llama.__init__`` is pure construction —
    ``internals.LlamaModel``/``LlamaContext`` are ctypes handles with no
    thread-local state, no signal handlers and no Python-level caches;
    ctypes foreign calls RELEASE THE GIL, so the engine loop keeps
    ticking while the native load runs, and the finished object is handed
    over under the build lock and thereafter used by exactly one thread
    (the engine). The one process-wide side effect is the wheel's
    ``suppress_stdout_stderr`` around the model read (fd dup2 + sys.stdout
    swap for the build's few seconds): launch lines are printed BEFORE the
    arm (the web launcher queues :data:`PRELOAD_START` after the token
    line), and CLI scan dots that land inside the window are the accepted,
    self-healing, DOCUMENTED loss (next turn's prompt returns; the log
    file and SSE stream have their own fds and never see it).

    The checksum thread runs CONCURRENTLY with the load (documented
    ordering decision: both are pure reads, the checksum gates nothing,
    and hashing first would only postpone readiness). Mismatch → value-free
    warning line + log entry, session KEEPS SERVING: ``model_state``
    reflects the LOAD's honest verdict only (closed enum: card states
    ``absent``/``running``/``ready``/``failed``/``declined`` + preload
    member ``loading``; ``ready``/``failed`` are shared names with the
    card's meanings).
    """

    def __init__(
        self,
        runtime: ModelRuntime,
        *,
        model_path: str,
        sha256: str | None = None,
        log_fn: Callable[[str], None] | None = None,
        hash_chunk_bytes: int = 1 << 23,
    ) -> None:
        self.state: str = "loading"
        self._runtime = runtime
        self._model_path = model_path
        self._sha256 = sha256 if isinstance(sha256, str) and sha256 else None
        self._log_fn = log_fn
        self._chunk = hash_chunk_bytes
        self._commands: queue.Queue[Any] | None = None
        self._started = False

    def attach(self, commands: queue.Queue[Any]) -> None:
        """Bind the pump's command queue (the workers deliver onto it)."""
        self._commands = commands

    def handle_command(
        self, command: object, output_fn: Callable[[str], None]
    ) -> bool:
        """Consume one queue item ON THE ENGINE THREAD; ``True`` when
        handled. The start marker spawns the workers (never before the
        launch lines have printed — see :class:`_PreloadStart`); the
        preload marker flips the state machine; the checksum marker only
        narrates (see the class docstring's ordering decision)."""
        if isinstance(command, _PreloadStart):
            output_fn(MODEL_PRELOAD_NOTICE)  # prints BEFORE the fd window opens
            commands = self._commands
            if commands is not None and not self._started:
                self._started = True
                threading.Thread(
                    target=self._load,
                    args=(commands,),
                    name="model-preload",
                    daemon=True,
                ).start()
                if self._sha256 is not None:
                    threading.Thread(
                        target=self._integrity,
                        args=(commands,),
                        name="model-integrity",
                        daemon=True,
                    ).start()
            return True
        if isinstance(command, _PreloadDone):
            self.state = "ready" if command.ok else "failed"
            if command.ok:
                # TCK-UX-009: exactly once — this marker is delivered by
                # the single loader thread (the _started guard spawns it
                # once), and the failed path never prints it.
                output_fn(MODEL_PRELOADED_NOTICE)
            elif self._log_fn is not None:
                self._log_fn(_MODEL_PRELOAD_FAILED_LOG)
            return True
        if isinstance(command, _IntegrityDone):
            if not command.ok:
                if self._log_fn is not None:
                    self._log_fn(MODEL_INTEGRITY_WARNING)
                output_fn(MODEL_INTEGRITY_WARNING)
            return True
        return False

    def _load(self, commands: queue.Queue[Any]) -> None:  # loader thread
        try:
            self._runtime.load()
            ok = True
        except Exception:  # noqa: BLE001 — the flag is the whole story;
            # the error itself re-raises on the next generate (the runtime
            # caches SUCCESS only), never from a worker thread.
            ok = False
        commands.put(_PreloadDone(ok=ok))

    def _integrity(self, commands: queue.Queue[Any]) -> None:  # checksum thread
        digest = hashlib.sha256()
        try:
            with open(self._model_path, "rb") as fh:
                for chunk in iter(lambda: fh.read(self._chunk), b""):
                    digest.update(chunk)
        except OSError:
            return  # cannot hash → claims NOTHING (the loader surfaces it)
        commands.put(_IntegrityDone(ok=digest.hexdigest() == self._sha256.lower()))


class _QuitSentinel:
    """Terminal command: stops the pump BETWEEN turns (never-cancel)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "QUIT"


#: Push this on the command queue to end a session (ADR-0024 §3: turns are
#: never cancelled — QUIT is honored only once the in-flight turn completed).
QUIT: Final[_QuitSentinel] = _QuitSentinel()


@dataclass
class _PumpError:
    """A feeder-side exception carried on the queue to be re-raised ON the
    engine thread, exactly where the old inline ``input_fn`` call raised it
    (preserves the CLI's exception semantics: EOFError ends the session,
    anything else — StopIteration, injected KeyboardInterrupt — propagates).
    """

    exc: BaseException


#: Command token the web transport stamps on a first-run watch-key submit
#: (TCK-LAUNCH-001): recognized ONLY as the ``command`` label of a
#: :class:`WatchKeyRequest`, it is never a model turn and never a chat line,
#: so no key material can ride it into the dispatcher.
WATCHKEY_COMMAND: Final[str] = "/watchkey"

#: ``/watchkey`` reply schema tag (additive-tag rule as for ``state/1``).
WATCHKEY_SCHEMA: Final[str] = "watchkey/1"

#: Value-free reply lines for the closed watch-key outcomes. The submitted
#: key NEVER appears in any of them (watch-only: keys never echo into
#: errors/logs); parse refusals come from ``WatchKeyError`` verbatim, which
#: is value-free by the descriptor layer's contract.
_WATCHKEY_ALREADY: Final[str] = "a watch key is already configured"
_WATCHKEY_SEED: Final[str] = (
    "that looks like a seed phrase — this app is hardware-wallet-only and "
    "never accepts seed words or private keys; provide the wallet's public "
    "account key (zpub/xpub/ypub)"
)
_WATCHKEY_UNAVAILABLE: Final[str] = "watch key setup is not available"

#: Shown (value-free) on a first-run web session for ANY user line until the
#: watch key lands: the placeholder wiring has no handlers, no store, and no
#: model, so a chat turn can only be refused, never run.
WATCHKEY_REQUIRED_NOTICE: Final[str] = (
    "No watch key configured yet — enter your wallet's public account key "
    "(zpub/xpub/ypub) using the form on this page to begin."
)

# ---------------------------------------------------------------------------
# TCK-ONB-007 (chat-first onboarding): deterministic engine beats and the
# PRE-MODEL chat intercepts. USER COPY VERBATIM — the beat strings below are
# spec, not editable prose. Zero model contact on every path here; all lines
# value-free (no user key, URL, address, or amount can enter any of them).
# ---------------------------------------------------------------------------

#: Startup beats on a FRESH needs_watch_key launch ONLY (a configured
#: launch never emits them): emitted by :func:`_pump` as ONE output_fn line
#: — the three sentences joined with ``\n`` closing a single turn, so the
#: web client renders ONE bubble with line breaks (user direction,
#: 2026-09-11; the SSE frame carries the payload verbatim and .turn-text is
#: pre-wrap). ORDER vs the model-absent surfaces is PINNED (critique Q6):
#: these beats come FIRST, the model card after them; the model-absent
#: BANNER rides the buffered startup narration, which flushes before the
#: pump runs at all.
CHAT_ONB_GREETING: Final[str] = "Hi, I'd like to be your new Bitcoin wallet."
CHAT_ONB_KEY_ASK: Final[str] = "Enter your xpub or zpub to get started."
CHAT_ONB_KEY_HELP_OFFER: Final[str] = (
    "If you don't know where to get that, ask me how and I'll get you some help."
)
_CHAT_ONB_OPENING: Final[tuple[str, ...]] = (
    CHAT_ONB_GREETING,
    CHAT_ONB_KEY_ASK,
    CHAT_ONB_KEY_HELP_OFFER,
)

#: Key landed through chat: the ack (its OWN bubble), then the backend beat
#: as ONE grouped bubble (ask + the two option lines joined with ``\n`` —
#: the mirror of the greeting group, pinned; the static-half user
#: correction 2026-09-11), shown only while the backend choice is genuinely
#: unresolved — an operator rung that already resolves it asks nothing, per
#: ONB-006.
CHAT_ONB_KEY_SAVED: Final[str] = "Great. I saved that."
CHAT_ONB_BACKEND_ASK: Final[str] = (
    "Now, where should I go to get blockchain information?"
)
# TCK-DESCOPE-M3A (USER REDIRECTION 2026-09-11): a WALLET backend is an
# Electrum server or a Bitcoin Core node — mempool.space is public fee/price
# info only and is never offered here as a wallet choice. The public tier is
# the NAMED public Electrum server, and its leak is named with it (the red
# warning, ONB-006/001B discipline — an explicit choice, never a default).
CHAT_ONB_BACKEND_OWN: Final[str] = (
    "If you run your own Bitcoin node or an Electrum server — Start9, "
    "Umbrel and MyNode all do — that would be better for privacy."
)
CHAT_ONB_BACKEND_PUBLIC: Final[str] = (
    "But if you don't have one of those, you can use the public Electrum "
    "server electrum.blockstream.info — chosen with eyes open: whoever "
    "runs it sees every address you check and can link it to your IP."
)
_CHAT_ONB_BACKEND_BEATS: Final[tuple[str, ...]] = (
    CHAT_ONB_BACKEND_ASK,
    CHAT_ONB_BACKEND_OWN,
    CHAT_ONB_BACKEND_PUBLIC,
)

# TCK-ONB-008 (chat backend creds UX, council-decided 2026-09-13): creds are
# NEVER collected in chat — a password typed as a chat message is echoed to
# every tab (user_text), announced by the a11y live region, and replayed from
# the SSE ring forever. When the backend beat receives a URL of an AUTH-
# CAPABLE scheme (the Bitcoin Core RPC family — the engine's own scheme
# constants, no new classifier), it emits ONE deterministic hand-off bubble
# naming the EXISTING settings-pane login block (masked fields, never-
# echoed storage — TCK-ONB-004 M3 / TCK-WEB-012) and proceeds past the URL
# as before; an embedded-login URL is refused value-free, the typed string
# never echoed, parsed-for-storage, or logged.
CHAT_ONB_BACKEND_CREDS: Final[str] = (
    "That server asks for a username and password. I don't take those in "
    "chat — open Settings (top right). Under the server address you'll "
    "find the login fields; fill them in and press Apply."
)
CHAT_ONB_LOGIN_REMOVED: Final[str] = (
    "Remove the login from the address — I'll ask for it in Settings."
)

#: The deterministic "ask me how" answer (NO model — the AI may not even be
#: running yet). Implementation-time copy (designer mini-pass, critique Q6):
#: generic device guidance only — Jade's export menu, Sparrow's wallet
#: settings — no user values, and the seed-phrase refusal with hardware-
#: only guidance rides the line itself (watch-only invariant, AGENTS.md).
CHAT_ONB_KEY_HOWTO: Final[str] = (
    "Your xpub or zpub comes from the wallet device or app itself — never "
    "your seed words. On a Jade: open the menu and choose the public-key "
    "export (the account xpub or zpub) and copy what it shows. In Sparrow "
    "Wallet: open your wallet, go to Settings, and the account's public "
    "key is shown there for copying. It is one long string starting with "
    "xpub, ypub, or zpub — paste it here when you have it. This app is "
    "hardware-wallet-only and watch-only: seed phrases and private keys "
    "are always refused, never needed."
)

#: SLIP-132 token prefixes (public + private siblings, testnet rungs
#: included — the testnet/private shapes exist HERE only so the paste
#: routes to the EXISTING gated parser that refuses them; the refusal
#: machinery is never duplicated here).
_WATCHKEY_TOKEN_PREFIXES: Final[tuple[str, ...]] = (
    "xpub", "ypub", "zpub", "tpub", "vpub", "upub",
    "xprv", "yprv", "zprv", "tprv", "vprv", "uprv",
)


def _chat_key_material(line: str) -> bool:
    """Whether a chat line while unprovisioned should ride the EXISTING
    parse+provision path (which owns the mainnet-only, watch-only and
    seed refusals). Two shapes only: a WHITESPACE-FREE token carrying a
    known key prefix (a pasted key, truncated or not — the gated parser
    judges it), or BIP39-SHAPED input (the seed refusal surfaces). Ordinary
    prose is never treated as key material."""
    text = line.strip()
    if not text:
        return False
    if _looks_like_seed(text):
        return True
    if any(ch.isspace() for ch in text):
        return False
    return text.lower().startswith(_WATCHKEY_TOKEN_PREFIXES)


def _chat_key_help_ask(line: str) -> bool:
    """The PINNED help matcher (critique Q6, asked while needs_watch_key):
    the word ``how`` together with one key topic word, whole words only
    (edge punctuation stripped). ``where is my xpub`` (no "how") and ``how
    are you`` (no topic) stay the ordinary refusal."""
    words = [w.strip(punctuation) for w in line.lower().split()]
    return "how" in words and any(
        w in ("xpub", "zpub", "key", "help") for w in words
    )


#: Words that make a "public" line a QUESTION or a REJECTION about the
#: public server rather than a choice OF it — presence anywhere vetoes the
#: match (fail toward asking, never toward consenting: the consent marker
#: is DURABLE, one ambiguous utterance must never pin the public posture).
#: Contractions arrive APOSTROPHE-STRIPPED (see :func:`_chat_public_choice`)
#: — the full ``n't`` family is listed in its stripped form.
_CHAT_PUBLIC_VETO_WORDS: Final[frozenset[str]] = frozenset(
    {
        "what", "why", "how", "who", "when", "where", "is", "am", "are",
        "was", "were", "do", "does", "did", "have", "has", "had", "can",
        "could", "cannot", "cant", "should", "tell", "explain", "define",
        "mean", "means", "meaning", "difference", "versus", "vs", "about",
        "think", "not", "no", "never", "without", "instead", "rather",
        "skip", "avoid",
        "dont", "doesnt", "didnt", "isnt", "arent", "wasnt", "werent",
        "wont", "shouldnt", "wouldnt", "couldnt", "hasnt", "havent",
        "mightnt", "mustnt", "neednt", "aint",
    }
)
#: Answer-shaped length ceiling: a sentence discussing public is ordinary
#: chat (the model may explain), never consent.
_CHAT_PUBLIC_MAX_WORDS: Final[int] = 6


def _chat_public_choice(line: str) -> bool:
    """The PINNED public matcher: a short ANSWER containing the whole word
    ``public``, with no question/negation veto word and no trailing ``?``.
    "use public" / "public" / "I'll use the public server" match; "what
    does public mean", "I don't want public" and "is public safer?" do
    not. Consent itself still rides the 001B seam — a match can never be
    an un-flagged accept (the leak disclosure is the ack that follows)."""
    raw = line.strip().lower()
    if not raw or raw.endswith("?"):
        return False
    words = [
        w.strip(punctuation).replace("'", "").replace("\u2019", "")
        for w in raw.split()
    ]
    if "public" not in words or len(words) > _CHAT_PUBLIC_MAX_WORDS:
        return False
    return _CHAT_PUBLIC_VETO_WORDS.isdisjoint(words)


def _chat_backend_url_candidate(line: str) -> str | None:
    """A chat line that IS a backend URL — one whitespace-free token with an
    accepted scheme prefix (the SAME :data:`_KNOWN_PROBE_SCHEMES` the entry
    probe dispatches on; free prose merely mentioning a URL stays ordinary
    chat). Classification/refusal judgment belongs to the probe, not here."""
    text = line.strip()
    if not text or any(ch.isspace() for ch in text):
        return None
    return text if text.lower().startswith(_KNOWN_PROBE_SCHEMES) else None


#: The AUTH-CAPABLE schemes (TCK-ONB-008): the Bitcoin Core RPC family —
#: the explicit ``bitcoind[+tls]://`` pair plus the bare http(s) aliases the
#: probe auto-detects into them (every member is a component of
#: :data:`_KNOWN_PROBE_SCHEMES`; the Electrum ``ssl://`` entry is NOT here —
#: an Electrum server asks no login through this seam). A URL whose scheme
#: is in this set gets the deterministic creds hand-off bubble.
_CHAT_AUTH_CAPABLE_SCHEMES: Final[tuple[str, ...]] = (
    BITCOIND_SCHEME,
    BITCOIND_TLS_SCHEME,
    "http://",
    "https://",
)


def _chat_url_auth_capable(url: str) -> bool:
    """Scheme-prefix test against the engine's own scheme constants (pure
    string read, like :func:`_chat_backend_url_candidate` — no new
    classifier, no parse, nothing that can echo a value)."""
    return url.lower().startswith(_CHAT_AUTH_CAPABLE_SCHEMES)


def _chat_url_has_login(url: str) -> bool:
    """Whether the URL embeds userinfo (``user:pass@`` / ``user@``) —
    reusing :func:`_url_without_credentials`: a URL whose credential-
    stripped form differs from it carries a login. Never raises; the
    answer is a bool, the value never escapes."""
    return _url_without_credentials(url) != url


@dataclass(frozen=True)
class WatchKeyRequest:
    """A typed first-run watch-key submit queued THROUGH the engine pump
    (TCK-LAUNCH-001).

    Sibling of :class:`SettingsRequest` / :class:`StateSnapshotRequest`: the
    transport never parses, stores, or gates the key (ADR-0024 §3) — it
    marshals the raw string onto the command queue and the ENGINE thread
    runs the EXISTING parse+gate path (mainnet-only, testnet/private-key/
    seed refusals intact) and persists the wallet via the existing store
    path, then answers with a value-free closed status.

    TCK-WEB-008 follow-up (b) / TCK-LAUNCH-002: ``allow_replace`` is set
    ONLY when the transport saw BOTH ``replace: true`` AND ``confirm: true``
    in the POST body (the ADR-0024 amendment's explicit double opt-in);
    the engine's parse+gate path is IDENTICAL either way — replace changes
    WHICH wiring the gate may produce, never HOW the key is gated.
    """

    command: str
    key: str
    reply: queue.Queue[dict[str, object]]
    allow_replace: bool = False


@dataclass
class WatchKeyProvision:
    """Watch-key provisioning (and, per the ADR-0024 amendment, explicit
    in-place REPLACEMENT) for a web launch — the engine thread's owner of
    that one step (TCK-LAUNCH-001, TCK-LAUNCH-002).

    Holds every argument :func:`_wire` needs (all resolved and
    config-validated at startup, BEFORE the transport split). ``provision``
    runs the SAME gated path the CLI ``--zpub``/ask flow uses — parse +
    gate the key, persist/reuse the wallet profile (the wallets-table
    descriptor IS the persistence; no new schema) — by calling
    :func:`_wire` itself, so the whole normal post-xpub sequence (banner,
    ONB-006 ``awaiting_backend`` deferral, startup-scan planning) is
    exactly what a keyed launch gets, because it IS :func:`_wire`.

    A keyed launch hands this object the EXISTING wiring (``wiring``
    preset), which is what makes a later explicit replace possible; a
    first-run launch starts it empty.

    The submitted key is a single-use argument: never stored on this
    object, never logged, never echoed. A submit against a configured
    wallet WITHOUT the double opt-in is refused value-free with
    :data:`_WATCHKEY_ALREADY` (the 409 contract stands — replace is
    opt-in, never an accident); with it, the new key is gated, wired,
    and the OLD engine pieces are torn down on this (engine) thread
    before the swap.
    """

    settings: Settings
    signer_selection: SignerSelection
    env_gap: int | None
    rescan: bool
    flow: TxFlow | None
    generate: ModelRuntime | GenerateFn | RemoteOpenAIRuntime
    node_detect_fn: Callable[[], LocalNodeReport] | None
    output_fn: Callable[[str], None]
    wiring: _Wiring | None = None
    #: TCK-DIAG-003: the output router threaded to :func:`_wire` so a
    #: provisioned/replaced wiring keeps the broadcast debug companion.
    output: _Output | None = None

    def provision(self, key: str, *, allow_replace: bool = False) -> dict[str, object]:
        """Parse+gate ``key`` and wire the engine ON THE ENGINE THREAD.

        Returns the value-free reply dict (``status`` is a closed enum:
        ``accepted``/``replaced``/``rejected``/``already``/``store_error``;
        the pump adds ``unavailable`` when no provision object exists).
        Every parse or gate refusal (testnet key, private key, seed-shaped
        input, malformed key) surfaces as ``rejected`` with the layer's own
        value-free reason — the key NEVER rides back. ``replaced`` fires
        only on the explicit double opt-in against a configured wallet.
        """
        was_configured = self.wiring is not None
        if was_configured and not allow_replace:
            return {
                "schema": WATCHKEY_SCHEMA,
                "status": "already",
                "error": _WATCHKEY_ALREADY,
            }
        candidate = key.strip()
        if _looks_like_seed(candidate):
            # Seed-shaped BEFORE parse (the SAME sanctioned scrubber check
            # the interactive key ask uses, onboarding._looks_like_seed): a
            # seed phrase is refused with the hardware-wallet-only guidance
            # and never handed to the key parser.
            return {
                "schema": WATCHKEY_SCHEMA,
                "status": "rejected",
                "error": _WATCHKEY_SEED,
            }
        try:
            # The EXISTING gated parse+descriptor path (mainnet-only,
            # watch-only, value-free WatchKeyErrors, ADR-0021) — no new
            # key-handling code.
            descriptor = WalletDescriptor.from_key(candidate)
        except WatchKeyError as exc:
            return {
                "schema": WATCHKEY_SCHEMA,
                "status": "rejected",
                "error": str(exc),
            }
        if was_configured and self._is_current_descriptor(descriptor):
            # Replace with the SAME key is a no-op worth naming (still
            # value-free — the descriptor is never echoed).
            return {
                "schema": WATCHKEY_SCHEMA,
                "status": "already",
                "error": _WATCHKEY_SAME,
            }
        try:
            wiring = _wire(
                parsed=descriptor.parsed,
                descriptor=descriptor,
                signer_selection=replace(
                    self.signer_selection,
                    fingerprint_hex=descriptor.parsed.hd_key.my_fingerprint.hex(),
                ),
                settings=self.settings,
                env_gap=self.env_gap,
                rescan=self.rescan,
                flow=self.flow,
                generate=self.generate,
                node_detect_fn=self.node_detect_fn,
                output_fn=self.output_fn,
                web_mode=True,
                replace=was_configured,
                output=self.output,
            )
        except _WiringError as exc:
            # Store-layer failure (the ONE line is the same value-free
            # string the keyed launch prints before its exit 2). The key
            # was valid but NOTHING was persisted — the form stays up,
            # retriable, exactly like the settings path's refusals. The
            # existing wiring (if any) is untouched and still authoritative.
            return {"schema": WATCHKEY_SCHEMA, "status": "store_error",
                    "error": str(exc)}
        if was_configured:
            # The swap is atomic on the engine thread: the NEW wiring is
            # fully built (wallet row persisted, active id set) before the
            # OLD pieces are released. Everything on the old store was
            # already durable (WAL autocommit); closing the chain client is
            # thread-free, the worker join is bounded, and the pump rebinds
            # onto the new pieces from the reply status.
            old = self.wiring
            assert old is not None
            old.worker.stop()
            _close_quietly(old.client)
            old.store.close()
        self.wiring = wiring
        return {
            "schema": WATCHKEY_SCHEMA,
            "status": "replaced" if was_configured else "accepted",
        }

    def _is_current_descriptor(self, descriptor: WalletDescriptor) -> bool:
        """Whether the active wallet row already carries this descriptor
        (fail-closed ``False`` on any read error — a broken store surfaces
        through the real wiring path anyway)."""
        try:
            assert self.wiring is not None
            wallet = self.wiring.store.get_active_wallet()
        except (StoreError, sqlite3.Error):
            return False
        return wallet is not None and wallet.descriptor == descriptor.descriptor


@dataclass
class EngineContext:
    """Everything the pump runs on, constructed ON the engine thread."""

    loop: AgentLoop
    flow: TxFlow
    session: SendSession
    table: DispatchTable
    watcher: IncomingWatcher | None = None
    client: ChainClient | None = None
    #: The non-blocking startup-scan controller (TCK-SCAN-003, ADR-0022);
    #: the pump attaches it to the command queue and drives its events.
    scan: ScanFlow | None = None
    #: The engine-owned store, present when the pump must answer settings
    #: reads/writes (TCK-WEB-005). Only the pump thread may touch it — the
    #: web bootstrap constructs it ON the engine thread.
    store: Store | None = None
    #: TCK-LAUNCH-001 first-run web provisioning: set when the launch had
    #: NO watch key (flag/env/stored all empty) — the pump then holds every
    #: user line and accepts only a :class:`WatchKeyRequest`, whose
    #: successful handling rebinds the pump onto the real wiring.
    provision: WatchKeyProvision | None = None
    #: TCK-LAUNCH-002: the engine-owned model-download flow, present when
    #: the launch fell back to the demo stub because the pinned default
    #: model file is not downloaded yet. The pump attaches it to the command
    #: queue and surfaces its state/progress; ``None`` = nothing to offer
    #: (a real model, remote bridge, or the explicit --stub-llm choice).
    model: ModelDownloadFlow | None = None
    #: TCK-LAUNCH-003: the engine-owned background PRELOAD + launch checksum
    #: of the resolved REAL local model (mutually exclusive with ``model``:
    #: file present → preload, absent → card). The pump attaches it; the
    #: transport arms it with :data:`PRELOAD_START` (CLI at pump entry, web
    #: after the URL/token launch lines — see :class:`_PreloadStart`).
    preload: ModelPreloadFlow | None = None
    #: TCK-APP-LOG-001: the mode-aware output router (web mode only). When
    #: present, :func:`start_engine` binds the engine emitter to it right
    #: after bootstrap so startup narration reaches the SSE stream (and
    #: provisioning narration routes there directly).
    output: _Output | None = None
    #: TCK-BACKEND-002: the engine's chain-backend hot-swap controller
    #: (built by :func:`_wire`, engine-thread-owned). The pump answers
    #: chain_base_url settings writes through it (probe-before-save + swap),
    #: serves ``backend_kind`` to /state, and runs the resync-now command.
    #: ``None`` on the first-run placeholder (no wiring to swap).
    backend: ChainBackendFlow | None = None
    #: TCK-UX-010: the boot-resolved runtime settings — the ONE
    #: object the hot-swap mutates (:attr:`_Wiring.settings`). The pump
    #: derives the ``/state`` ``privacy_mode`` NAME from it (via
    #: :func:`_backend_mode`) at the snapshot call site; ``None`` on the
    #: degenerate first-run placeholder (no backend chosen yet → the field
    #: is OMITTED, never fabricated — same rule as ``backend_kind``).
    settings: Settings | None = None
    #: TCK-HW-005 slice A: the stateless HWI signer the chat probe/unlock
    #: interception runs on (constructed by :func:`_wire` for THIS wallet's
    #: key; ``None`` on the first-run placeholder — there is no wallet to
    #: probe against yet, and every ordinary line is refused anyway).
    hwi: HwiUsbSigner | None = None


@dataclass(frozen=True)
class StateSnapshotRequest:
    """A typed ``/state`` read queued THROUGH the engine pump (TCK-WEB-003).

    The transport never touches the flow/store/watcher directly (ADR-0024 §3:
    all state reads route through the engine thread); it drops this request on
    the command queue and the ENGINE thread answers it between turns
    (never-cancel), so a busy engine simply delays the reply rather than
    racing a transport thread over shared state. The reply is a value-free,
    validated snapshot — never a model turn, never a chat line.
    """

    command: str
    reply: queue.Queue[dict[str, object]]


def build_state_snapshot(
    flow: TxFlow,
    session: SendSession,
    watcher: IncomingWatcher | None,
    scan: ScanFlow | None = None,
    model: ModelDownloadFlow | None = None,
    backend_kind: str | None = None,
    preload: ModelPreloadFlow | None = None,
    privacy_mode: str | None = None,
) -> dict[str, object]:
    """The value-free ``/state`` snapshot, built ON the engine thread.

    Deliberately minimal and validated-by-construction: the only facts are the
    dispatcher-owned flow position (a closed :class:`TxFlowStatus` enum name),
    whether a transaction pends (a boolean), the last turn's gate classification
    (a closed :class:`GateDecision` enum name), whether a watcher is
    configured/enabled (booleans), — TCK-WEB-005 — the startup-scan state
    (a closed :class:`StartupScan` state name) plus the durable
    first-scan-completed boolean, — TCK-LAUNCH-002 — the model-download
    state (a closed :class:`ModelDownloadFlow` state name), —
    TCK-LAUNCH-003 — the model PRELOAD state (a closed
    :class:`ModelPreloadFlow` state name; the two are mutually exclusive:
    file present → preload, absent → card),     — TCK-BACKEND-002 — the
    live backend kind (a closed :data:`BACKEND_KINDS` enum NAME: never a
    URL/host), and — TCK-UX-010 — the privacy mode (a
    precomputed closed :data:`PRIVACY_MODES` enum NAME, computed by the
    pump at the call site; the builder sees no settings). No address,
    amount, txid, ``tx_ref``, key
    material OR progress byte-count CAN appear — every value is an enum NAME
    or a boolean, never data. No progress percentage here (a wallet-size
    oracle); download progress rides its own event kind.
    """
    snapshot: dict[str, object] = {
        "schema": STATE_SCHEMA,
        "flow_state": flow.state.value,
        "pending_present": flow.pending is not None,
        "gate_decision": session.gate_decision.value,
        "watch": {
            "configured": watcher is not None,
            "enabled": bool(watcher is not None and watcher.enabled),
        },
        "scan_state": scan.gate.state if scan is not None else "disabled",
        "first_scan_complete": bool(scan is not None and scan.first_scan_recorded),
    }
    if model is not None:
        # Additive under state/1 (the shipped client reads named keys and
        # ignores this whole field when absent): a closed state NAME only.
        snapshot["model_state"] = model.state
    elif preload is not None:
        # TCK-LAUNCH-003: the SAME additive ``model_state`` field now also
        # carries the preload machine (``loading`` while the background load
        # runs, then ``ready``/``failed``). A checksum mismatch does NOT ride
        # here (advisory; the session keeps serving) — see the class docs.
        snapshot["model_state"] = preload.state
    if backend_kind is not None:
        # Additive under state/1 (same rule): the CLOSED enum name of the
        # live chain backend kind — value-free by construction (the settings
        # pane stopped badging it in TCK-DESCOPE-M3B; the field stays).
        snapshot["backend_kind"] = backend_kind
    if privacy_mode is not None:
        # TCK-UX-010: additive under state/1 (same rule): the CLOSED
        # privacy-mode NAME (:data:`PRIVACY_MODES`) the pump precomputed —
        # an enum name, never a URL/host. Absent (None) = no settings
        # context; omitted, never guessed (the ``backend_kind`` pattern).
        snapshot["privacy_mode"] = privacy_mode
    return snapshot


# ------------------------------------------------- settings surface (TCK-WEB-005)

#: Command token the web transport stamps on a typed ``/settings`` read/write
#: (the sibling of :data:`STATE_SNAPSHOT_COMMAND`): recognized ONLY as the
#: ``command`` label of a :class:`SettingsRequest`, it is never a model turn
#: and never a chat line, so nothing sensitive can ride it.
SETTINGS_COMMAND: Final[str] = "/settings"

#: ``/settings`` snapshot schema tag — value-free literal; same additive-tag
#: rule as :data:`STATE_SCHEMA` (add fields under ``settings/1``; bump only on
#: a rename/removal, with the client in the same ticket).
SETTINGS_SCHEMA: Final[str] = "settings/1"

#: TCK-WEB-013: the additive ``/settings`` reply field carrying the chain
#: base URL ACTUALLY in service (env/config/stored fold, or the public
#: default when the stored rung is unset) — a read-only DISPLAY of the
#: user's own config for the settings pane, USERINFO STRIPPED by
#: :func:`_url_without_credentials` (never a credential surface). Additive
#: under the unchanged ``settings/1`` tag; stamped with ``backend_kind``
#: at the one seal point, omitted (never fabricated) when no engine chain
#: wiring exists. The trust badge is NOT this field: it rides the EXISTING
#: ``/state`` ``privacy_mode`` closed enum (TCK-UX-010).
SETTINGS_EFFECTIVE_CHAIN_URL_KEY: Final[str] = "effective_chain_base_url"

#: Env rung of the ADR-0023 ladder for the chain backend (displayed as the
#: honest ``env_override`` flag only — resolution stays in
#: :func:`localwallet.config.resolve_chain_base_url`; the value is never read).
CHAIN_BASE_URL_ENV_VAR: Final[str] = "LOCALWALLET_CHAIN_BASE_URL"

#: Cap on a POSTed setting value (the body-size gate bounds the request; this
#: keeps junk out of the store). A URL the user's own wiring already accepts
#: fits with orders of magnitude to spare.
MAX_SETTING_VALUE_CHARS: Final[int] = 2048

#: The store key of the persisted chain-backend choice — readable/writable
#: ONLY through the store's typed pair (``get_chain_base_url`` /
#: ``set_chain_base_url``), which owns the write validation (TCK-ONB-002).
_CHAIN_BASE_URL_KEY: Final[str] = "chain_base_url"

#: The backend credential keys (TCK-ONB-004 M3; plan §3 storage section) —
#: readable/writable ONLY through the store's typed pairs. The web
#: settings SURFACE carries them as SECRET entries: GET answers only
#: whether each key is SET (``configured``), never the value, and a write
#: is never echoed back — the password rides one POST body to the engine
#: thread and exists at rest only inside the local single-user DB file
#: (the documented threat model: same trust surface as the wallet
#: descriptor itself; OS-keyring is future work, plan OQ-4). Clearing is
#: the ``""``-writes-delete convention shared with every typed writer.
_BACKEND_AUTH_USER_KEY: Final[str] = "backend_auth_user"
_BACKEND_AUTH_PASS_KEY: Final[str] = "backend_auth_pass"
_BACKEND_AUTH_NONE_KEY: Final[str] = "backend_auth_none"

#: The closed set of credential keys an Apply may carry (``SettingsRequest.
#: creds``) — anything else in the overlay is refused before ANY write.
_BACKEND_AUTH_KEYS: Final[frozenset[str]] = frozenset(
    {_BACKEND_AUTH_USER_KEY, _BACKEND_AUTH_PASS_KEY, _BACKEND_AUTH_NONE_KEY}
)

# --------------------------------- backend kind + hot-swap surfaces (TCK-BACKEND-002)

#: The CLOSED enum of ``backend_kind`` values the settings/``/state``
#: surfaces expose (TCK-BACKEND-002 user direction 10; ADR-0018 amendment;
#: RE-SCOPED by TCK-DESCOPE-M3B to the two wallet families only — the
#: public-consent choice installs the NAMED public Electrum server, an
#: ``ssl://`` URL, so it reports kind ``electrum``; the trust dimension
#: rides the separate ``privacy_mode`` enum, never the kind). Value-free by
#: construction — an enum NAME, never a URL/host. Mapping (scheme only; no
#: probe rides the read):
#:
#: * ``none``      — no backend is being consulted (no client, the
#:                   first-run choice is still unresolved/held, or a legacy
#:                   http(s) Esplora-shape rung that no longer builds a
#:                   wallet client at all);
#: * ``electrum``  — the live URL's scheme is ``ssl://`` (M1 adapter;
#:                   includes the consented public Electrum server);
#: * ``bitcoind``  — the live URL's scheme is ``bitcoind://`` or its
#:                   ``bitcoind+tls://`` TLS sibling (M2/M3 adapters — the
#:                   http(s) auto-detect STORES the rewrite, so the live
#:                   URL is always one of the canonical schemes).
BACKEND_KIND_NONE: Final[str] = "none"
BACKEND_KIND_ELECTRUM: Final[str] = "electrum"
BACKEND_KIND_BITCOIND: Final[str] = "bitcoind"
BACKEND_KINDS: Final[frozenset[str]] = frozenset(
    {BACKEND_KIND_NONE, BACKEND_KIND_ELECTRUM, BACKEND_KIND_BITCOIND}
)

#: The ONE honest refusal line for a failed validation probe (TCK-BACKEND-002
#: deliverable 2): the write is refused value-free — neither URL nor host nor
#: server text ever rides it. It names the CLOSED probe categories the probe
#: collapses to (both collapse identically, so the refusal cannot probe-worsen
#: privacy by distinguishing "unreachable" from "wrong chain" over the wire).
#: TCK-BACKEND-003 (fix C) appends three value-free HINTS after the line —
#: the TLS escape hatch (honest: the unverified-transport warning applies),
#: the /api API-root segment (now auto-tried, so the bare host works), and
#: the demanded-login case. The hints are static (not per-failure) because
#: the probe's contract is the collapse-everything ``str | None`` answer
#: (TCK-ONB-003 review finding 1: NOTHING escapes to the caller — plumbing
#: a "reached-but-401" distinction through it would either widen that
#: escape surface or rewrite the return type every conversation and test
#: pins, for a message one hint already carries; fix D's conditional line
#: SKIPPED by design, documented).
BACKEND_PROBE_FAIL: Final[str] = (
    "that backend did not check out: it is unreachable, or it does not "
    "serve mainnet as an Electrum (ssl://) or Bitcoin Core RPC server "
    "(bitcoind://, bitcoind+tls://, or the plain http(s) address of an RPC "
    "port — auto-detected) — those are the only wallet backends this app "
    "speaks, nothing was saved and the current backend stays in service. "
    "hints: a self-signed server certificate is refused unless TLS "
    "verification is turned off (LOCALWALLET_TLS_VERIFY=0 / "
    "tls_verify=false — transport authentication is then OFF, see the "
    "startup warning); if the server answers but demands a login, set the "
    "backend credentials and retry (a bare URL without them cannot pass)"
)

#: The closed ``resync`` reply values on the settings/resync surfaces
#: (TCK-BACKEND-002): ``started`` (the full rebuild scan is running),
#: ``busy`` (another scan owns the chain worker right now — the regular
#: scan will use the new value anyway), ``deferred`` (only via a
#: chain_base_url swap queued behind an in-flight scan), ``skipped``
#: (nothing to resync / the write is shadowed by an env/config-file rung),
#: ``unchanged`` (a gap_limit apply whose value did not change),
#: ``no_rescan`` (TCK-GAP-001: a gap_limit apply that NARROWED the window —
#: deliberately applied WITHOUT an auto-rescan, since a smaller window can
#: only hide addresses; the reply carries the value-free
#: :data:`GAP_NARROW_NOTE` tradeoff line instead), ``unavailable`` (no
#: engine chain wiring).
RESYNC_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "started",
        "busy",
        "deferred",
        "skipped",
        "unchanged",
        "no_rescan",
        "unavailable",
    }
)

#: THE allowlist (fail-closed, TCK-WEB-005): only settings keys that EXIST in
#: the store's key/value table and are READ by live code today. Anything
#: else — invented ``fee_cache_ttl_s``/``utxo_*``/env-only scalars — would be
#: a write with no reader, so it is refused. Unknown future keys 404 here
#: until their ladder ships.
_SETTINGS_KEYS: Final[frozenset[str]] = frozenset(
    {
        wallet_scan.GAP_LIMIT_SETTING,
        _CHAIN_BASE_URL_KEY,
        # TCK-UX-009: the background-watch interval. Allowlisted ONLY
        # because the watcher build site in _wire reads it back through
        # _resolve_watch_interval (env > stored > default) — the setting
        # the "Change it in settings" line claims.
        WATCH_INTERVAL_SETTING,
        # TCK-FIAT-002: the display currency (closed enum, single-sourced
        # from localwallet.config). Allowlisted because the price oracle
        # RE-READS the ladder on every fetch decision
        # (:func:`_display_currency_reader`) — the next quote already
        # converts in the new currency, no restart.
        DISPLAY_CURRENCY_SETTING,
        # TCK-ONB-004 M3: the backend credential keys (SECRET entries —
        # readable as SET/UNSET only, never value). They have live readers
        # (the engine's credential resolver feeding probe + client build).
        _BACKEND_AUTH_USER_KEY,
        _BACKEND_AUTH_PASS_KEY,
        _BACKEND_AUTH_NONE_KEY,
        # TCK-UTXO-003: the three coin-selection policy keys (doc §2.3/§3).
        # The create_tx handler resolves env > stored > default on EVERY
        # selection (live reader since TCK-UTXO-004's wiring), and the
        # store's typed accessor pair (``get/set_coin_setting``) is the
        # sanctioned writer behind ``_apply_setting_change``.
        *COIN_SETTING_KEYS,
    }
)


@dataclass(frozen=True)
class SettingsRequest:
    """A typed ``/settings`` read (``key is None``) or single-key write
    (``key`` + ``value``) queued THROUGH the engine pump.

    Like :class:`StateSnapshotRequest`, the transport never touches the store
    (ADR-0024 §3): the request rides the command queue and the ENGINE thread
    validates, persists, and answers between turns. One key per write keeps
    the contract honest — a rejected change cannot half-apply, and unrelated
    keys are structurally untouched.
    """

    command: str
    key: str | None
    value: str | None
    reply: queue.Queue[dict[str, object]]
    #: Optional credential overlay that RIDES a ``chain_base_url`` write
    #: (TCK-ONB-004 M3 security-review LOW 2): the ``backend_auth_*`` keys
    #: the web Apply submitted together with the address, as one map. The
    #: engine writes them BEFORE the URL probe (so the probe tests the
    #: login the user just typed) and REWINDS the prior record if the URL
    #: write then fails — new creds are never committed against the old
    #: URL. Every other request keeps this ``None``.
    creds: Mapping[str, str] | None = None


#: Command token the web transport stamps on the ``Resync now`` action
#: (TCK-BACKEND-002 user direction 6; the sibling of :data:`SETTINGS_COMMAND`):
#: recognized ONLY as the ``command`` label of a :class:`ResyncRequest`.
RESYNC_COMMAND: Final[str] = "/resync"

#: ``/resync`` reply schema tag (additive-tag rule as for ``settings/1``).
RESYNC_SCHEMA: Final[str] = "resync/1"


@dataclass(frozen=True)
class ResyncRequest:
    """A typed ``resync_now`` trigger queued THROUGH the engine pump
    (TCK-BACKEND-002 deliverable 5). The browser's button carries NO data;
    the ENGINE thread runs the existing SCAN-003 rebuild path (the ``--rescan``
    semantics: full rescan as-if the key was entered for the first time) —
    tags survive by construction (``coin_labels`` is a separate table, never
    in the scan write-set; pinned by tests/test_backend_hotswap.py). The
    concurrency guard is single-threaded truth: one answer, one scan at a
    time (:meth:`ScanFlow.resync_now` refuses while a scan owns the worker).
    """

    command: str
    reply: queue.Queue[dict[str, object]]


#: Command label the web transport stamps on a public-backend consent press
#: (TCK-PRIVACY-001B; the sibling of :data:`RESYNC_COMMAND`): recognized ONLY
#: as the ``command`` field of a :class:`ConsentRequest`, it is never a model
#: turn and never a chat line.
CONSENT_COMMAND: Final[str] = "/consent"

#: ``/consent`` reply schema tag (additive-tag rule as for ``resync/1``).
CONSENT_SCHEMA: Final[str] = "consent/1"


@dataclass(frozen=True)
class ConsentRequest:
    """A typed public-backend-consent trigger queued THROUGH the engine pump
    (TCK-PRIVACY-001B — the seam :func:`set_public_backend_consent`'s web
    rider). The browser's button carries NO data; the ENGINE thread (which
    owns the store, the gate and the scan) runs the existing seam — the HTTP
    transport thread never touches either. The reply is the closed value-free
    status: ``loading`` ONLY when the held first-run scan actually started
    (the F2 contract), ``recorded`` when the choice stands with nothing held
    to release, ``unavailable`` when no store is wired. A press is consent
    itself; NO other web action rides this command (engine-side, nothing
    else ever records the choice).
    """

    command: str
    reply: queue.Queue[dict[str, object]]


def _env_overridden(env_var: str) -> bool:
    """Whether an env var is set to a non-blank value (the honest
    ``env_override`` flag: the stored rung is shadowed until restart with the
    env unset; the VALUE is never read or echoed)."""
    return bool(os.environ.get(env_var, "").strip())


def _display_truncate(text: str) -> str:
    """The display-only head…tail shortening of a public key (same visual
    rule the client applies; TCK-WEB-008) — a display truncation of tool
    output, never the revealed/copied value."""
    if len(text) <= 24:
        return text
    return f"{text[:12]}…{text[-8:]}"


def _watch_key_entry(store: Store, *, reveal: bool = False) -> dict[str, object]:
    """The settings-surface watch-key entry (TCK-WEB-008 follow-up (a),
    TCK-LAUNCH-002): the active wallet's canonical descriptor — a PUBLIC
    account key (watch-only, ADR-0010/0021) — listed DISPLAY-TRUNCATED by
    default and in full ONLY on an explicit single-key read. The entry
    never appears in any log (web access logging is suppressed wholesale);
    every route that can carry it is token-gated (ADR-0024 §6). A read
    failure or no active wallet renders ``configured: False`` — fail quiet,
    never a guess."""
    descriptor: str | None = None
    try:
        wallet = store.get_active_wallet()
        if wallet is not None:
            descriptor = wallet.descriptor
    except (StoreError, sqlite3.Error):
        descriptor = None
    if descriptor is None:
        return {
            "key": WATCH_KEY_SETTING,
            "type": "watch_key",
            "value": None,
            "default": None,
            "min": None,
            "max": None,
            "requires_restart": False,
            "env_override": False,
            "configured": False,
            "revealed": False,
        }
    return {
        "key": WATCH_KEY_SETTING,
        "type": "watch_key",
        "value": descriptor if reveal else _display_truncate(descriptor),
        "default": None,
        "min": None,
        "max": None,
        # The in-session REPLACE is engine-allowed now (ADR-0024 amendment:
        # POST /watchkey with replace+confirm re-wires on the engine thread),
        # so the honest restart flag is False.
        "requires_restart": False,
        # A key on the env/flag rung shadows the stored row after restart
        # (LAUNCH-001 precedence) — the honest flag only, never the value.
        "env_override": _env_overridden(ZPUB_ENV_VAR),
        "configured": True,
        "revealed": reveal,
    }


def _settings_entries(
    store: Store, backend: ChainBackendFlow | None = None
) -> list[dict[str, object]]:
    """The current stored value of every allowlisted key, with its type,
    allowed range, and honest effect flags. Values here are user-authored
    scalars (a gap count, a backend URL) or the PUBLIC watch key in its
    display-truncated form — never private material, never wallet history
    data (no address, amount or balance exists in the settings table).

    ``backend`` (TCK-BACKEND-002): the engine's hot-swap controller. With
    one wired (every production pump), a stored ``chain_base_url`` write
    takes effect IN-SESSION — the honest ``requires_restart`` flag is then
    ``False``, UNLESS an env/config-file rung shadows the stored one (that
    ladder rule is unchanged: ADR-0023). With ``None`` (a bare harness pump
    with no chain wiring) the flag stays ``True`` — the write would be a
    plain store row, effective next launch, and the flag must say so.
    """
    shadowed = backend is not None and backend.shadowed
    entries: list[dict[str, object]] = [
        {
            "key": wallet_scan.GAP_LIMIT_SETTING,
            "type": "int",
            "value": store.get_setting(wallet_scan.GAP_LIMIT_SETTING),
            "default": str(wallet_scan.DEFAULT_GAP_LIMIT),
            "min": wallet_scan._MIN_GAP,
            "max": wallet_scan._MAX_GAP,
            # Every scan plan re-reads the setting (wallet.scan._resolve_gap_limit):
            # takes effect on the NEXT scan, no restart. TCK-BACKEND-002: an
            # APPLIED change fires a resync itself (user direction 8) — except
            # a NARROWING change (TCK-GAP-001), which is applied without one
            # (a smaller window can only hide addresses) and narrates the
            # tradeoff instead.
            "requires_restart": False,
            "env_override": _env_overridden(GAP_LIMIT_ENV_VAR),
        },
        {
            "key": _CHAIN_BASE_URL_KEY,
            "type": "url",
            "value": store.get_chain_base_url(),
            # Unset stored rung → the built-in public default (ADR-0003);
            # null value below means exactly that.
            "default": None,
            "min": None,
            "max": None,
            # TCK-BACKEND-002 (ADR-0018 amendment): the engine hot-swaps the
            # chain client when the write lands — NO restart — unless the
            # stored rung is shadowed by an env/config-file rung (then the
            # next-launch honesty stands).
            "requires_restart": backend is None or shadowed,
            "env_override": _env_overridden(CHAIN_BASE_URL_ENV_VAR),
        },
        # TCK-UX-009: the background-watch interval, same gap-limit shape.
        # The ladder at the watcher build site (env > stored > default 60)
        # is its live reader; the watcher is built ONCE at launch, so the
        # honest effect flag is RESTART.
        {
            "key": WATCH_INTERVAL_SETTING,
            "type": "int",
            "value": store.get_setting(WATCH_INTERVAL_SETTING),
            "default": f"{WATCH_INTERVAL_DEFAULT_S:g}",
            "min": WATCH_INTERVAL_MIN,
            "max": WATCH_INTERVAL_MAX,
            "requires_restart": True,
            "env_override": _env_overridden(WATCH_INTERVAL_ENV_VAR),
        },
        # TCK-FIAT-002: the display currency — a CLOSED-ENUM entry (the
        # config module owns the codes and the ladder; "options" is that
        # fixed list, never user data). The price oracle re-reads the
        # ladder per fetch, so a stored change is live on the NEXT quote:
        # the honest requires_restart is False (the coin-policy shape, not
        # the watcher's). An env/config-file rung shadows the stored one
        # (flag only — its value is never read or echoed here).
        {
            "key": DISPLAY_CURRENCY_SETTING,
            "type": "enum",
            "value": store.get_setting(DISPLAY_CURRENCY_SETTING),
            "default": DEFAULT_DISPLAY_CURRENCY,
            "options": list(DISPLAY_CURRENCIES),
            "min": None,
            "max": None,
            "requires_restart": False,
            "env_override": _env_overridden(DISPLAY_CURRENCY_ENV_VAR),
        },
        # TCK-WEB-008 follow-up (a), TCK-LAUNCH-002: the watch key rides the
        # SAME read surface, display-TRUNCATED (a public account key, never a
        # secret; never in logs). It is READ-ONLY here — the write allowlist
        # (:data:`_SETTINGS_KEYS`) deliberately excludes it, so a POST can
        # never invent a settings-shaped key write; changing the key goes
        # through the gated :class:`WatchKeyRequest` path (replace opt-in).
        _watch_key_entry(store),
    ]
    # TCK-ONB-004 M3: the backend credential keys ride the same surface as
    # SECRET entries — ``configured`` is the whole story a read may tell.
    entries.extend(_backend_auth_entries(store))
    # TCK-UTXO-003: the three coin-selection policy keys (doc §2.3) ride the
    # SAME gap-limit-style surface — stored rung value, shipped default,
    # bounds and honest flags. They resolve per selection (TCK-UTXO-004
    # wired the reader into the create_tx handler), so the honest
    # ``requires_restart`` is False; the env rung's EXISTENCE flips
    # ``env_override`` — its VALUE is never read or echoed here.
    entries.extend(_coin_setting_entries(store))
    return entries


def _coin_setting_entries(store: Store) -> list[dict[str, object]]:
    """The coin-selection policy entries: stored rung (``None`` = unset →
    default applies), shipped default and per-key bounds single-sourced from
    :mod:`localwallet.config`, decimal-string values exactly like the
    store's typed writers. Errors never echo a submitted value (the writers'
    contract, unchanged here)."""
    return [
        {
            "key": key,
            "type": "int",
            "value": store.get_coin_setting(key),
            "default": str(COIN_SETTING_DEFAULTS[key]),
            "min": COIN_SETTING_BOUNDS[key][0],
            "max": COIN_SETTING_BOUNDS[key][1],
            # Resolved fresh on every selection (create_tx handler): NO
            # restart — the next quote already uses the new numbers.
            "requires_restart": False,
            "env_override": _env_overridden(f"LOCALWALLET_{key.upper()}"),
        }
        for key in COIN_SETTING_KEYS
    ]


def _backend_auth_entries(store: Store) -> list[dict[str, object]]:
    """The three credential entries for the settings read surface (TCK-ONB-
    004 M3): ``type: "secret"``, ``value`` ALWAYS ``None`` — GET answers only
    whether each key is SET (the client renders "a login is saved" + the
    clear action, never the login or password). Unreadable store → all
    ``False`` (fail quiet toward "nothing stored", the watch-key entry's
    discipline; a surprise never echoes a partial value)."""
    try:
        configured = (
            store.get_backend_auth_user() is not None,
            store.get_backend_auth_pass() is not None,
            store.get_backend_auth_none(),
        )
    except (StoreError, sqlite3.Error):
        configured = (False, False, False)
    return [
        {
            "key": key,
            "type": "secret",
            "value": None,  # NEVER the stored value — set/unset only.
            "configured": flag,
            "default": None,
            "min": None,
            "max": None,
            # Honest effect: a credential change needs no restart — it takes
            # effect when the server address is (re)Applied in-session (the
            # probe + the rebuilt client resolve credentials at that moment),
            # or at the next launch either way.
            "requires_restart": False,
            # Credentials have NO env/config-file rung (deliberate,
            # ADR-0018 M3 amendment: the stored DB pair is their only rung) —
            # the shadow flag is structurally False.
            "env_override": False,
        }
        for key, flag in zip(
            (
                _BACKEND_AUTH_USER_KEY,
                _BACKEND_AUTH_PASS_KEY,
                _BACKEND_AUTH_NONE_KEY,
            ),
            configured,
            strict=True,
        )
    ]


def _snapshot_backend_auth(
    store: Store,
) -> tuple[str | None, str | None, bool] | None:
    """The credential record BEFORE a creds-carrying Apply (TCK-ONB-004 M3
    security-review LOW 2) — the rewind target when the URL write fails, so
    a refused Apply can never strand the NEW pair against the OLD URL (the
    same snapshot/restore discipline the /setup credentials step keeps).
    Unreadable store → ``None`` (nothing to rewind toward; the refusal line
    is already the answer)."""
    try:
        return (
            store.get_backend_auth_user(),
            store.get_backend_auth_pass(),
            store.get_backend_auth_none(),
        )
    except (StoreError, sqlite3.Error):
        return None


def _restore_backend_auth(
    store: Store, snapshot: tuple[str | None, str | None, bool] | None
) -> None:
    """Best-effort rewind of the credential record to a prior snapshot.
    Values that passed the typed writers once pass again; a store that
    cannot even roll back is the same sqlite-loss failure the write path
    already answers — every line on this surface stays value-free."""
    if snapshot is None:
        return
    user, password, none_flag = snapshot
    try:
        store.set_backend_auth_none(none_flag)
        store.set_backend_auth_user(user or "")
        store.set_backend_auth_pass(password or "")
    except (StoreError, sqlite3.Error):
        pass


def _apply_setting_change(store: Store, key: str, value: str) -> str | None:
    """Validate + persist ONE allowlisted change on the ENGINE thread.

    ``None`` on success, else a value-free refusal line (it names the key and
    the rule — the submitted value is never echoed). ``chain_base_url``
    delegates to the store's typed writer, the ONLY sanctioned writer of that
    key (validation is never duplicated there).

    ponytail: ``gap_limit`` has no typed store writer yet (read side:
    ``wallet.scan._resolve_gap_limit``), so the canonical-form + bounds check
    lives here, with the bounds imported from the scan layer (single source);
    when TCK-UTXO-003's typed accessor pair lands, this becomes a call to it.
    """
    if key == wallet_scan.GAP_LIMIT_SETTING:
        text = value.strip()
        try:
            gap = int(text)
        except ValueError:
            gap = -1
        if str(gap) != text or not wallet_scan._MIN_GAP <= gap <= wallet_scan._MAX_GAP:
            return (
                f"{key} must be a whole number between "
                f"{wallet_scan._MIN_GAP} and {wallet_scan._MAX_GAP}"
            )
        try:
            store.set_setting(key, str(gap))  # canonical decimal string
        except (StoreError, sqlite3.Error):
            return f"could not save {key}"
        return None
    if key == WATCH_INTERVAL_SETTING:
        # TCK-UX-009: the same gap-limit shape (no typed writer yet) —
        # canonical-form + bounds 0..86400 here, fail-closed, VALUE-FREE
        # (the submitted value is never echoed); the ladder at the watcher
        # build site is the reader.
        text = value.strip()
        try:
            interval = int(text)
        except ValueError:
            interval = -1
        if (
            str(interval) != text
            or not WATCH_INTERVAL_MIN <= interval <= WATCH_INTERVAL_MAX
        ):
            return (
                f"{key} must be a whole number between "
                f"{WATCH_INTERVAL_MIN} and {WATCH_INTERVAL_MAX}"
            )
        try:
            store.set_setting(key, str(interval))  # canonical decimal string
        except (StoreError, sqlite3.Error):
            return f"could not save {key}"
        return None
    if key == DISPLAY_CURRENCY_SETTING:
        # TCK-FIAT-002: the closed-enum write — parsed CASE-INSENSITIVELY
        # and stored CANONICAL lowercase ("" clears the stored rung back to
        # env/file/default on the ladder). Anything outside the enum is
        # refused WITHOUT echoing the submitted value; the oracle's per-
        # fetch ladder read makes an applied change live on the next quote.
        text = value.strip().lower()
        if text and text not in DISPLAY_CURRENCIES:
            return f"{key} must be one of {', '.join(DISPLAY_CURRENCIES)}"
        try:
            store.set_setting(key, text)  # canonical lowercase (or clear)
        except (StoreError, sqlite3.Error):
            return f"could not save {key}"
        return None
    if key in COIN_SETTING_KEYS:
        # TCK-UTXO-003: the store's typed accessor pair is the ONLY
        # sanctioned writer (bounds + min<max cross-check fail-closed AT
        # WRITE, value-free — the gap_limit ponytail note is resolved;
        # there is no second parser here). ``""`` clears the stored rung
        # back to the shipped default; surrounding whitespace canonicalizes
        # like every other scalar rung on this surface.
        try:
            store.set_coin_setting(key, value.strip())
        except (StoreError, sqlite3.Error) as exc:
            return str(exc)
        return None
    if key == _BACKEND_AUTH_NONE_KEY:
        # The checkbox as a closed write: "1" sets explicit no-credentials,
        # "" clears the record (back to the documented default ladder).
        # Anything else is refused WITHOUT echoing the submitted value.
        text = value.strip()
        if text not in ("", "1"):
            return f"{key} must be 1 or empty"
        try:
            store.set_backend_auth_none(text == "1")
        except (StoreError, sqlite3.Error) as exc:
            return str(exc)
        return None
    if key in (_BACKEND_AUTH_USER_KEY, _BACKEND_AUTH_PASS_KEY):
        # Shape validation (length/ASCII/no-control-chars, value-free) is
        # the store's typed writer's — the ONLY sanctioned writer, never
        # duplicated here. ``""`` clears; the reply's re-read entry says
        # SET/UNSET, never the value.
        try:
            if key == _BACKEND_AUTH_USER_KEY:
                store.set_backend_auth_user(value)
            else:
                store.set_backend_auth_pass(value)
        except (StoreError, sqlite3.Error) as exc:
            return str(exc)
        return None
    try:
        # ``""`` clears the stored rung (the set_chain_base_url convention);
        # every other validation (scheme, host, no credentials, no
        # whitespace) is the store writer's, fail-closed, value-free by
        # the store layer's contract — safe to surface.
        store.set_chain_base_url(value)
    except (StoreError, sqlite3.Error) as exc:
        return str(exc)
    return None


def handle_settings_request(
    store: Store | None,
    key: str | None,
    value: str | None,
    backend: ChainBackendFlow | None = None,
    creds: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Answer a :class:`SettingsRequest` ON THE ENGINE THREAD — the only
    thread that ever reads/writes the settings table for the web transport.

    Read (``key is None``) → the allowlisted entries (watch key DISPLAY-
    TRUNCATED). Explicit single-key read (``key`` set, ``value is None``)
    → that ONE entry, with the FULL public watch key — the deliberate
    second step the settings UI takes only on the user's Show/Copy click
    (TCK-WEB-008 follow-up (a); a public account key, still never logged).
    Write → validate fail-closed, persist via the store's settings API, and
    reply with the freshly re-read entry (the client confirms from tool
    truth, never from its own echo). Refusals carry a value-free ``error``;
    an off-allowlist key is refused WITHOUT even naming the request (the
    name itself is untrusted input).

    ``backend`` (TCK-BACKEND-002, the ADR-0018 hot-swap amendment): with the
    engine's chain-backend controller wired, a ``chain_base_url`` write runs
    the probe-before-save path (deliverable 2 — an unreachable/foreign-chain
    URL is refused value-free, NOTHING stored, the old client untouched) and,
    once stored, hot-swaps the live client + fires a full resync (or defers
    both behind the in-flight scan); the applied reply carries the honest
    ``swapped``/``resync`` fields. A ``gap_limit`` write whose value ACTUALLY
    CHANGED fires the same resync (user direction 8) — UNLESS it NARROWED the
    window (TCK-GAP-001): a SMALLER value is applied WITHOUT an auto-rescan
    (``resync: "no_rescan"``) plus the value-free :data:`GAP_NARROW_NOTE`
    tradeoff line, because a smaller window can only hide addresses beyond
    it — the user can still re-sync manually (Resync now). An unchanged
    apply says so (``resync: "unchanged"``, no scan). With ``backend=None``
    (a bare harness pump — no chain wiring to swap) both keys keep the
    plain store write and the entry flags carry the next-launch honesty.
    Every wired
    reply (reads included) carries the additive ``backend_kind`` NAME
    (deliverable 10, re-scoped by TCK-DESCOPE-M3B: an enum name, value-free;
    the settings pane no longer badged it — the field still rides).

    ``creds`` (TCK-ONB-004 M3 security-review LOW 2): a credential overlay
    riding a ``chain_base_url`` write — one atomic Apply, ordered the honest
    way. The pair lands on the store FIRST (the probe then tests exactly the
    login the user typed), the URL write runs, and ANY failure after that
    rewinds the prior credential record: an Apply that does not save the
    address never leaves new creds against the old URL. On success both the
    pair and the URL stand committed together. ``creds`` on any other key —
    or carrying any key outside the closed credential trio — is refused
    before a single write, value-free (the request is untrusted input).
    """
    unknown = {"schema": SETTINGS_SCHEMA, "status": "rejected", "error": "unknown setting"}
    if store is None:
        # Only reachable if the transport talks to an engine without the
        # settings wiring (a bare test pump); fail closed, never guess.
        return {
            "schema": SETTINGS_SCHEMA,
            "status": "unavailable",
            "error": "settings not available",
        }
    if key is None:
        return _settings_reply({"status": "ok", "settings": _settings_entries(store, backend)}, backend)
    if value is None:
        # Explicit single-key READ (never a write, never the general list).
        # The watch key reads back FULL here — this shape (key set, value
        # None) is the deliberate second step the settings UI takes ONLY on
        # the user's Show/Copy click; the general list above stays
        # truncated. An unknown read key is refused without naming it back.
        if key == WATCH_KEY_SETTING:
            return _settings_reply(
                {"status": "ok", "settings": [_watch_key_entry(store, reveal=True)]}, backend
            )
        entry = next(
            (e for e in _settings_entries(store, backend) if e["key"] == key), None
        )
        if entry is None:
            return unknown
        return _settings_reply({"status": "ok", "settings": [entry]}, backend)
    if key not in _SETTINGS_KEYS or not isinstance(value, str):
        return unknown
    if creds is not None and (
        key != _CHAIN_BASE_URL_KEY
        or not isinstance(creds, dict)
        or not all(
            k in _BACKEND_AUTH_KEYS and isinstance(v, str)
            for k, v in creds.items()
        )
    ):
        # A creds overlay is ONLY ever honest riding its own URL Apply;
        # refused as unknown-shape input before a single write.
        return unknown
    if len(value) > MAX_SETTING_VALUE_CHARS:
        return _settings_reply(
            {
                "status": "rejected",
                "key": key,
                "error": "value too long",
            },
            backend,
        )
    extra: dict[str, object] = {}
    auth_prior: tuple[str | None, str | None, bool] | None = None
    if creds:
        # The atomic Apply, part 1 (security-review LOW 2): land the pair
        # through the SAME typed writers first — the URL probe resolves the
        # store, so it tests the login the user just typed. A rejected cred
        # SHAPE rewinds whatever the partial writes changed and never probes.
        auth_prior = _snapshot_backend_auth(store)
        for cred_key in (
            _BACKEND_AUTH_NONE_KEY,
            _BACKEND_AUTH_USER_KEY,
            _BACKEND_AUTH_PASS_KEY,
        ):
            if cred_key not in creds:
                continue
            error = _apply_setting_change(store, cred_key, creds[cred_key])
            if error is not None:
                _restore_backend_auth(store, auth_prior)
                return _settings_reply(
                    {"status": "rejected", "key": key, "error": error}, backend
                )
    if key == _CHAIN_BASE_URL_KEY and backend is not None:
        # The hot-swap path OWNS this write (probe → build → typed store
        # write → install) — the store's typed writer stays the only writer.
        error, extra = backend.apply(value)
        if error is not None:
            # Part 2 of the atomic Apply: the address did NOT save, so the
            # just-written pair is rewound (no new creds against the old URL).
            _restore_backend_auth(store, auth_prior)
            return _settings_reply(
                {"status": "rejected", "key": key, "error": error}, backend
            )
    else:
        before = (
            store.get_setting(key) if key == wallet_scan.GAP_LIMIT_SETTING else None
        )
        if (error := _apply_setting_change(store, key, value)) is not None:
            _restore_backend_auth(store, auth_prior)
            return _settings_reply(
                {"status": "rejected", "key": key, "error": error}, backend
            )
        if key == wallet_scan.GAP_LIMIT_SETTING:
            after = next(
                (
                    e["value"]
                    for e in _settings_entries(store, backend)
                    if e["key"] == key
                ),
                None,
            )
            if str(after) == str(before):
                extra["resync"] = "unchanged"  # direction 8: say so, no scan
            elif int(after) < (
                int(before) if before is not None else wallet_scan.DEFAULT_GAP_LIMIT
            ):
                # TCK-GAP-001: a NARROWER window never auto-rescans — it can
                # only hide addresses beyond it (ADR-0009 amendment). Apply
                # the stored value WITHOUT a resync and narrate the tradeoff
                # (value-free); the user can re-sync manually via Resync now.
                extra["resync"] = "no_rescan"
                extra["note"] = GAP_NARROW_NOTE
            elif backend is not None:
                extra["resync"] = backend.resync()
            else:
                extra["resync"] = "unavailable"
    entry = next(e for e in _settings_entries(store, backend) if e["key"] == key)
    return _settings_reply(
        {"status": "applied", "settings": [entry], **extra}, backend
    )


def _settings_reply(
    fields: dict[str, object], backend: ChainBackendFlow | None
) -> dict[str, object]:
    """Seal one ``/settings`` reply with its schema tag — and, when an
    engine chain wiring exists, the additive ``backend_kind`` NAME
    (TCK-BACKEND-002 deliverable 10: a closed enum member, value-free;
    absent when no backend is wired — the additive-``settings/1`` rule)
    and the additive :data:`SETTINGS_EFFECTIVE_CHAIN_URL_KEY` display
    string (TCK-WEB-013: the chain base URL ACTUALLY in service — env/
    config/stored fold, or the public default when no rung is set —
    USERINFO STRIPPED, never a credential surface; user-owned config
    already displayed in the settings pane). Both fields stamp at THIS
    one seal point so every wired reply shape carries them together;
    both are omitted, never fabricated, when nothing is wired. The
    trust badge is NOT here: it rides the EXISTING /state
    ``privacy_mode`` closed enum (TCK-UX-010, already shipped)."""
    reply: dict[str, object] = {"schema": SETTINGS_SCHEMA, **fields}
    if backend is not None:
        reply["backend_kind"] = backend.kind
        reply[SETTINGS_EFFECTIVE_CHAIN_URL_KEY] = backend.effective_base_url
    return reply


@dataclass
class EngineHandle:
    """The transport's view of the engine thread (WEB-002 consumes this):
    submit strings, read events from the sink, ``shutdown`` ends the session
    after the in-flight turn. ``error`` carries a bootstrap failure.
    """

    commands: queue.Queue[Any]
    emitter: EventEmitter
    thread: threading.Thread | None = None
    error: BaseException | None = None

    def submit(self, line: str) -> None:
        """Enqueue one user line / transcript command (bytes only — the
        transport never touches the flow, the store, or a handler)."""
        self.commands.put(line)

    def request_state(self, timeout: float) -> dict[str, object] | None:
        """Read a typed, value-free ``/state`` snapshot THROUGH the pump.

        A transport thread cannot touch the flow/watcher (ADR-0024 §3), so it
        queues a :class:`StateSnapshotRequest` and blocks on its reply; the
        engine thread answers between turns. Returns ``None`` if the engine is
        busy past ``timeout`` or has errored — the caller falls back to the
        transport-only shape (never a stall, never a lie).
        """
        reply: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        self.commands.put(StateSnapshotRequest(STATE_SNAPSHOT_COMMAND, reply))
        try:
            return reply.get(timeout=timeout)
        except queue.Empty:
            return None

    def request_settings(
        self,
        timeout: float,
        key: str | None = None,
        value: str | None = None,
        creds: Mapping[str, str] | None = None,
    ) -> dict[str, object] | None:
        """Read the allowlisted settings (``key is None``), read ONE key
        explicitly (``key`` set, ``value is None`` — the full public watch
        key), or apply ONE validated change THROUGH the pump (TCK-WEB-005,
        TCK-WEB-008 follow-up (a)). ``creds`` is the closed credential
        overlay riding a ``chain_base_url`` write (TCK-ONB-004 M3
        security-review LOW 2 — one atomic Apply, ordered probe-first);
        the transport merely marshals it, ALL validation stays engine-side.

        Same discipline as :meth:`request_state`: the transport thread never
        touches the store; the ENGINE thread validates fail-closed, persists,
        and answers between turns. ``None`` on timeout = engine busy past the
        deadline (the transport answers 503; never-cancel stands — the queued
        consult may still be answered after the caller gave up, so the client
        RE-READS via GET rather than assuming the write failed)."""
        reply: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        self.commands.put(
            SettingsRequest(SETTINGS_COMMAND, key, value, reply, creds)
        )
        try:
            return reply.get(timeout=timeout)
        except queue.Empty:
            return None

    def request_watchkey(
        self, timeout: float, key: str, *, allow_replace: bool = False
    ) -> dict[str, object] | None:
        """Submit a watch key THROUGH the pump (TCK-LAUNCH-001; in-place
        REPLACE per the TCK-LAUNCH-002 / ADR-0024 amendment).

        Same discipline as :meth:`request_settings`: the transport thread
        never parses, gates, stores, or logs key material — it marshals the
        raw string (plus the explicit ``allow_replace`` opt-in the transport
        sets ONLY when the POST body carried BOTH ``replace`` and
        ``confirm``) onto the command queue and the ENGINE thread runs the
        existing parse+gate path and answers between turns. ``None`` on
        timeout = never-cancel stands (the submit may still land; the client
        RE-READS /state rather than assuming failure).
        """
        reply: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        self.commands.put(
            WatchKeyRequest(WATCHKEY_COMMAND, key, reply, allow_replace)
        )
        try:
            return reply.get(timeout=timeout)
        except queue.Empty:
            return None

    def request_resync(self, timeout: float) -> dict[str, object] | None:
        """Trigger a full wallet resync THROUGH the pump (TCK-BACKEND-002
        deliverable 5 — the browser's ``Resync now`` button). Same discipline
        as :meth:`request_settings`: the transport carries no data, the
        ENGINE thread runs the existing SCAN-003 rebuild path and answers
        with the closed value-free status. ``None`` on timeout = never-cancel
        stands (the queued resync may still land; the client re-reads
        /state — scan_state is the truth)."""
        reply: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        self.commands.put(ResyncRequest(RESYNC_COMMAND, reply))
        try:
            return reply.get(timeout=timeout)
        except queue.Empty:
            return None

    def request_consent(self, timeout: float) -> dict[str, object] | None:
        """Record an explicit public-backend consent THROUGH the pump
        (TCK-PRIVACY-001B — the web consent button). Same discipline as
        :meth:`request_resync`: the transport carries no data and never
        touches the seam — the ENGINE thread runs
        :func:`set_public_backend_consent` (record + release) and answers
        with the closed value-free status. ``None`` on timeout = never-cancel
        stands (the queued press may still land; the client re-reads /state —
        scan_state/privacy_mode are the truth)."""
        reply: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        self.commands.put(ConsentRequest(CONSENT_COMMAND, reply))
        try:
            return reply.get(timeout=timeout)
        except queue.Empty:
            return None

    def shutdown(self) -> None:
        """End the session AFTER the current turn completes (never-cancel)."""
        self.commands.put(QUIT)


def start_engine(
    bootstrap: Callable[[], EngineContext],
    sink: Callable[[EngineEvent], None],
) -> EngineHandle:
    """Start the dedicated engine thread (ADR-0024 §3, threaded mode).

    ``bootstrap`` runs ON the engine thread — the Store (sqlite3 default
    ``check_same_thread=True``) MUST be created inside it — and the pump
    then serves ``handle.commands`` there. (The lazy Llama runtime USED to
    share this pinning; TCK-LAUNCH-003 made its construction explicitly
    thread-safe, so a resolved local model now preloads on its own
    background thread and the engine only ever uses it.)
    The CLI path (:func:`_repl`) runs the same pump with the main thread as the
    engine; WEB-002's server supplies ``bootstrap`` over the real wiring.
    """
    handle = EngineHandle(commands=queue.Queue(), emitter=EventEmitter(sink))

    def body() -> None:
        try:
            ctx = bootstrap()
        # Deliberate: the bootstrap's failure travels VERBATIM to the thread
        # joiner via handle.error (narrowing would hide e.g. a SystemExit
        # raised during state construction).
        except BaseException as exc:  # noqa: BLE001 — engine-thread bootstrap
            handle.error = exc
            return
        if ctx.output is not None:
            # Bind the engine emitter to the router so buffered startup
            # narration flushes to the SSE stream and provisioning narration
            # routes there directly (single emitter writer: engine thread).
            ctx.output.bind_emitter(handle.emitter)
        # TCK-WEB-016: a pump death OUTSIDE the contained turn path (the
        # re-raised _PumpError, a store fault, any bug) must never strand a
        # client mid-turn: flag the handle FIRST (every transport's
        # engine.error check and /state fast-fail then precede any client
        # reaction to the marker), then close the turn so open bubbles
        # render and re-read /state instead of spinning on the echo.
        try:
            _pump(
                ctx.loop,
                handle.emitter.text,
                handle.commands,
                flow=ctx.flow,
                session=ctx.session,
                table=ctx.table,
                watcher=ctx.watcher,
                client=ctx.client,
                emitter=handle.emitter,
                scan=ctx.scan,
                store=ctx.store,
                provision=ctx.provision,
                model=ctx.model,
                preload=ctx.preload,
                backend=ctx.backend,
                settings=ctx.settings,
                hwi=ctx.hwi,
            )
        except BaseException as exc:  # noqa: BLE001 — engine-thread pump death
            handle.error = exc
            handle.emitter.emit(EVENT_TURN_END)

    handle.thread = threading.Thread(target=body, name="engine", daemon=True)
    handle.thread.start()
    return handle


def _stdin_feeder(
    input_fn: Callable[[str], str],
    commands: queue.Queue[Any],
    ready: threading.Event,
    stop: threading.Event,
) -> None:
    """CLI feeder: turns ``input_fn("you> ")`` lines into queue commands.

    The ``ready``/``stop`` handshake paces reads so the prompt is printed
    only after the previous turn's output AND the between-turns watch drain
    completed — the exact order of the old single-threaded loop (the
    feeder thread prints nothing itself; ``input_fn`` does). EOF becomes
    QUIT; any other exception travels as :class:`_PumpError` and is
    re-raised on the engine thread.
    """
    while not stop.is_set():
        ready.wait()
        ready.clear()
        if stop.is_set():
            return
        try:
            line = input_fn("you> ")
        except EOFError:
            commands.put(QUIT)
            return
        # Deliberate: EVERY feeder exception (StopIteration, injected
        # KeyboardInterrupt included) travels on the queue to be re-raised
        # on the engine thread, preserving the pre-pump CLI exception
        # semantics exactly (ADR-0024 §3).
        except BaseException as exc:  # noqa: BLE001 — forward to engine thread
            commands.put(_PumpError(exc))
            return
        commands.put(line)


def _model_card_verdict(
    model: ModelDownloadFlow,
    utterance: str,
    tx_pending: bool,
    onboarding_listening: bool = False,
) -> str | None:
    """Deterministically classify one model-card answer (TCK-LAUNCH-002).

    ``"yes"`` / ``"no"`` / ``None`` (not a card answer — ordinary pipeline).
    The slash forms are canonical button utterances and always classify
    while a flow object exists. A BARE 'yes'/'no' classifies ONLY while the
    card is genuinely showing, no transaction pends (the confirm gate owns
    those words then — ADR-0013), AND no onboarding ask is listening (the
    ADR-0023 backend ask answers by yes/no too — it is the higher-priority
    deterministic channel).
    """
    if utterance == MODEL_DOWNLOAD_COMMAND:
        return "yes"
    if utterance == MODEL_LATER_COMMAND:
        return "no"
    if tx_pending or onboarding_listening or model.state not in _CARD_SHOWN_STATES:
        return None
    if utterance in _MODEL_YES_WORDS:
        return "yes"
    if utterance in _MODEL_NO_WORDS:
        return "no"
    return None


def _answer_model_card(
    model: ModelDownloadFlow, verdict: str, output_fn: Callable[[str], None]
) -> None:
    """Apply one card answer — code-owned narration, engine-thread-only
    (the single mutation point for the flow's state machine)."""
    if verdict == "yes":
        if model.start():
            output_fn(MODEL_DL_STARTED)
        elif model.state == "running":
            output_fn(MODEL_DL_RUNNING)
        else:  # ready — the download already completed this session
            output_fn(MODEL_DL_DONE)
        return
    model.decline()
    for line in MODEL_DECLINED_LINES:
        output_fn(line)


def _run_quick_action(
    line: str,
    loop: AgentLoop,
    table: DispatchTable,
    session: SendSession,
    store: Store | None,
    output_fn: Callable[[str], None],
    backend: ChainBackendFlow | None = None,
) -> bool:
    """Execute one model-free quick action ON THE ENGINE THREAD; ``True``
    when handled (TCK-LAUNCH-002 deliverable 4).

    DOCUMENTED LLM BYPASS: these are NOT model turns and emit NO model
    output. The handler-bound commands dispatch the EXISTING allowlist
    handlers directly with CODE-built empty-params envelopes (the same
    handler + same ``_print_turn`` narration the retry interception rides,
    TCK-HW-002) — the model is only ever the envelope SOURCE, so a session
    running on the demo stub (or with no model at all) answers them
    identically. ``/receive`` and ``/settings`` are pure store READS (same
    deterministic channel as ``/label``, ADR-0020). Values print verbatim
    from tool output; the UI computes nothing.
    """
    command = line.split(maxsplit=1)[0].lower()
    intent = _QUICK_ACTION_INTENTS.get(command)
    if intent is not None:
        handler = table.get(intent)
        if handler is None:  # pragma: no cover — bare test tables
            output_fn("That action is not available right now.")
            return True
        params: NewAddressParams | GetBalanceParams
        if intent is IntentName.NEW_ADDRESS:
            params = NewAddressParams()
        else:
            params = GetBalanceParams()
        envelope = Envelope(v=0, intent=intent, params=params)
        result = handler(envelope)
        loop.add_turn(command, envelope.model_dump_json())
        _print_turn(
            AgentTurnResult(
                status=AgentTurnStatus.OK,
                envelope=envelope,
                result=result,
                user_message=None,
                turns_used=0,
            ),
            output_fn,
            session=session,
        )
        return True
    if command == "/receive":
        _print_next_receive_address(store, output_fn)
        return True
    if command == "/settings":
        _print_settings_readout(store, output_fn, backend)
        return True
    return False


def _print_next_receive_address(
    store: Store | None, output_fn: Callable[[str], None]
) -> None:
    """``/receive``: the NEXT receive address — pure derivation at the
    branch's live ``next_index``, NO allocation and NO network (an
    allocation-free preview; ``/address`` is the allocating command).
    Address display is verbatim tool output (terminal/transcript channel
    only, never a log). TCK-CHAT-001: printing an own address is a
    showing, so it gets its stable registry number HERE (the registry
    write is idempotent — the same string keeps the same number when
    ``/address`` later allocates it); a registry failure degrades to the
    number-free line, never a crash on a read path."""
    if store is None:
        output_fn(_LABEL_STORE_UNAVAILABLE)
        return
    try:
        wallet = store.get_active_wallet()
        if wallet is None:
            output_fn(_LABEL_NO_WALLET)
            return
        descriptor = WalletDescriptor.from_descriptor_string(wallet.descriptor)
        index = store.get_derivation(wallet.id, 0).next_index
        address = BranchDeriver(descriptor.parsed, 0).address(index)
    except (StoreError, sqlite3.Error, WatchKeyError):
        output_fn(_LABEL_ERROR_STORE)
        return
    try:
        number = store.note_address_shown(wallet.id, address).number
    except (StoreError, sqlite3.Error):
        number = None
    number_part = f" #{number}" if number is not None else ""
    output_fn(
        sanitize_tool_output(
            f"Next receive address{number_part} (index {index}, not yet issued — "
            f'"/address" reserves a fresh one): {address}'
        )
    )


def _print_settings_readout(
    store: Store | None,
    output_fn: Callable[[str], None],
    backend: ChainBackendFlow | None = None,
) -> None:
    """``/settings`` CLI readout of the same allowlisted entries
    GET /settings serves (the watch key arrives in its display-TRUNCATED
    form by construction; user-authored scalars verbatim)."""
    reply = handle_settings_request(store, None, None, backend)
    if reply.get("status") != "ok":
        output_fn("Settings are not available right now.")
        return
    entries = reply.get("settings")
    assert isinstance(entries, list)
    for entry in entries:
        assert isinstance(entry, dict)
        value = entry.get("value")
        shown = "(not set)" if value is None else str(value)
        output_fn(sanitize_tool_output(f"{entry.get('key')}: {shown}"))


def _pump(
    loop: AgentLoop,
    output_fn: Callable[[str], None],
    commands: queue.Queue[Any],
    *,
    flow: TxFlow,
    session: SendSession,
    table: DispatchTable,
    watcher: IncomingWatcher | None = None,
    client: ChainClient | None = None,
    emitter: EventEmitter | None = None,
    ready: threading.Event | None = None,
    scan: ScanFlow | None = None,
    store: Store | None = None,
    onboarding: OnboardingFlow | None = None,
    provision: WatchKeyProvision | None = None,
    model: ModelDownloadFlow | None = None,
    preload: ModelPreloadFlow | None = None,
    backend: ChainBackendFlow | None = None,
    settings: Settings | None = None,
    hwi: HwiUsbSigner | None = None,
) -> None:
    """The transport-agnostic turn pump (ADR-0024 §3): blocking
    ``queue.get()`` → the UNCHANGED :func:`_run_turn` path.

    The only input is ``commands`` and the only output is ``output_fn`` (an
    :meth:`EventEmitter.text` closure). Turns are never cancelled: every
    dequeued command runs to completion and ``QUIT`` is honored between
    turns; each processed turn ends with a ``turn_end`` marker (ids carry
    the ordering WEB-002's ring buffer replays). The between-turns watch
    drain (ADR-0019) is preserved. ``ready`` is the CLI feeder's pacing
    hook (None for queue-native transports).

    Non-blocking startup scan (TCK-SCAN-003, ADR-0022): when a ``scan`` flow
    is given, the pump attaches it to the command queue and starts the
    worker fetch, then treats the worker's ``_ScanTick``/``_ScanDone``
    deliveries as first-class queue items — so the prompt goes live
    immediately, progress dots and the completion narration surface between
    turns as they arrive on the same queue, and the engine (never the
    worker) persists. While the first scan runs the watch drain stands down
    (the single worker is occupied). On exit the pending scan is drained to
    completion so its result is persisted + narrated exactly once.

    Typed transport consults (TCK-WEB-003/005): ``StateSnapshotRequest`` and
    ``SettingsRequest`` are answered BETWEEN commands on this thread — the
    settings pair needs ``store`` (the engine-owned store); without it (CLI
    pumps, bare test pumps) the request is refused fail-closed.

    First-run watch-key provisioning (TCK-LAUNCH-001): when ``provision`` is
    given the engine started with NO wallet. A ``WatchKeyRequest`` runs the
    existing parse+gate path on this thread and — on success — the pump
    REBINDS onto the real wiring (the placeholder loop/table never runs a
    turn); ordinary lines before that are refused value-free. A submit
    against an ALREADY-configured engine is refused 409 UNLESS it carries
    the explicit replace+confirm opt-in (``allow_replace``, TCK-LAUNCH-002 /
    ADR-0024 amendment) — then the pump rebinds onto the fresh wiring and
    narrates the old-wallet-cache warning (never the key).

    Model download (TCK-LAUNCH-002): when ``model`` is given the launch fell
    back to the demo stub because the pinned default GGUF is not downloaded.
    The pump attaches it to the command queue and arms the deterministic
    Yes/No card once; ``_ModelProgress``/``_ModelDone`` reader deliveries
    are consumed as first-class queue items (int-only progress events +
    terminal narration), and ``/download``|``/later`` (plus bare yes/no when
    nothing pends) answer the card on this thread. Model-free quick actions
    (``/balance``, ``/address``) dispatch the allowlist handlers directly —
    a code-owned bypass of the LLM, never model output. On exit any live
    download child is terminated BOUNDED (no orphans).

    Model preload (TCK-LAUNCH-003): when ``preload`` is given the launch
    resolved a REAL local model; the pump attaches it to the command queue
    but does NOT arm it — the transport queues :data:`PRELOAD_START` when
    safe for its channel (CLI: at pump entry; web: after the URL/token
    launch lines printed, because the wheel's build-time stdout silencer is
    process-wide). The pump then consumes the ``_PreloadDone``/
    ``_IntegrityDone`` worker deliveries as first-class queue items: the
    state machine (``loading`` → ``ready``/``failed``) flips HERE (the
    terminal marker also emits ``turn_end`` so the browser re-reads
    ``/state``), a checksum mismatch narrates one value-free warning and
    serving CONTINUES, and a model turn that arrives mid-load waits inside
    the runtime's build lock — clean serialization, never an error.

    Chain-backend hot-swap (TCK-BACKEND-002, ADR-0018 amendment): when
    ``backend`` is given, ``chain_base_url`` settings writes run the probe→
    store→SWAP path on this thread (the engine thread owns the client's
    whole lifecycle), and ``ResyncRequest`` triggers the full rebuild scan.
    The ONE concurrency rule (pinned): a swap never crosses an in-flight
    scan — while the scan gate is in progress the (already-validated,
    already-stored) swap is DEFERRED and installed the moment the worker's
    ``_ScanDone`` has been persisted; the OLD client keeps serving untouched
    until then (fail-closed, never clientless). ``client`` (this pump's
    local: watch-drain + turn facts) rebinds on every swap, immediate or
    deferred. A deferred swap whose session ends first is dropped — the
    stored value simply takes effect at next launch, exactly the
    pre-amendment behavior.

    Chat-first onboarding (TCK-ONB-007, user copy VERBATIM): a FRESH
    needs_watch_key launch opens with the deterministic greeting GROUP —
    the three lines joined with ``\\n`` into ONE output_fn event closing
    ONE turn (the web renders one bubble with line breaks; user direction
    2026-09-11; a configured launch never emits it). While unprovisioned,
    key-shaped chat lines ride the EXISTING parse+provision path PRE-MODEL
    (the gated refusals are that path's own value-free lines) and, on
    success, the ack bubble + the grouped backend beat follow; the pinned
    "ask me how" matcher answers with the deterministic export guidance;
    everything else keeps the watch-key refusal notice. While PROVISIONED
    with the
    backend still UNRESOLVED, a chat message that is a backend URL rides
    the settings-POST probe→store→swap discipline (``ChainBackend``.
    ``apply`` — url_class clamp + DIAG-001 companion included) and a short
    public-answer routes through the 001B consent seam with the leak
    disclosure; a resolved launch swallows NOTHING (post-setup URLs are
    ordinary chat). TCK-ONB-008: an auth-capable (Core RPC family) URL
    prepends the deterministic Settings-creds hand-off bubble before the
    same probe→store→swap ride, and an embedded-login URL is refused value-
    free without ever reaching the probe. Zero model contact on all of it.
    """

    def _onb_line(line: str) -> None:
        # One onboarding BUBBLE = one output_fn line closing one turn
        # (TCK-UX-012 pump pattern). A bubble may carry several display
        # lines joined with "\n" — the web renders them inside ONE bubble
        # (.turn-text is pre-wrap); the CLI sink ignores the marker,
        # byte-identical terminal output.
        output_fn(line)
        if emitter is not None:
            emitter.emit(EVENT_TURN_END)

    def _adopt_wiring() -> None:
        """Rebind the pump onto the provisioned wiring — the ONE rebind
        path shared by the typed ``/watchkey`` submit (TCK-LAUNCH-001) and
        the chat-provision intercept (TCK-ONB-007), so the two entries can
        never drift. Engine thread only (like every pump mutation)."""
        nonlocal loop, flow, session, table, watcher, client, store, scan
        nonlocal backend, settings, hwi
        assert provision is not None and provision.wiring is not None
        wiring = provision.wiring
        loop = wiring.loop
        flow = wiring.flow
        session = wiring.session
        table = wiring.table
        watcher = wiring.watcher
        client = wiring.client
        store = wiring.store
        scan = wiring.scan
        # TCK-BACKEND-002: the fresh wiring owns its own swap controller
        # (built by _wire) — the pump follows.
        backend = wiring.swap
        # TCK-UX-010: the privacy_mode source moves with the wiring — the
        # SAME settings object the swap mutates in place (and the
        # provisioned first-run path's boot-resolved settings before
        # any wiring existed: the rebind simply follows the live one).
        settings = wiring.settings
        # TCK-HW-005 slice A: the probe signer belongs to the WALLET (its
        # account fingerprint comes from the key) — the rebind follows it.
        hwi = wiring.hwi
        if scan is not None:
            scan.attach(commands)
            scan.begin()

    def _chat_backend_intent(line: str) -> bool:
        """The chat surface of the backend choice, WHILE IT IS UNRESOLVED
        ONLY (TCK-ONB-007; critique Q4's state gate): a pasted backend URL
        rides the EXISTING probe→store→SWAP path (the /setup outcome, the
        settings-POST security discipline inherited — the probe is the ONE
        closure that carries the output router, so _emit_probe_failure
        surfaces the value-free DIAG-001 companion and the url_class clamp
        stays intact); a short public-answer rides the 001B consent seam
        (:func:`set_public_backend_consent` — record + release, never an
        un-flagged accept: the ack re-names the leak). A RESOLVED launch
        consumes NOTHING here — post-setup URLs are ordinary chat, never
        swallowed (pinned)."""
        nonlocal client
        if store is None or backend is None or settings is None:
            return False
        if _backend_resolved(settings.chain_base_url.strip() or None, store):
            return False
        url = _chat_backend_url_candidate(line)
        if url is not None:
            if _chat_url_has_login(url):
                # TCK-ONB-008: an embedded login is refused BEFORE anything
                # else — the typed string (it contains creds) is never
                # echoed, probed, parsed-for-storage, or logged. Value-free
                # constant line; the turn is consumed (no model contact).
                _onb_line(CHAT_ONB_LOGIN_REMOVED)
                return True
            if _chat_url_auth_capable(url):
                # TCK-ONB-008: ONE deterministic hand-off bubble naming the
                # existing Settings login block, then the beat proceeds past
                # the URL exactly as today (the URL is not a secret).
                _onb_line(CHAT_ONB_BACKEND_CREDS)
            error, fields = backend.apply(url)
            client = backend.client  # rebind on an immediate swap (no-op else)
            if error is not None:
                # value-free refusal (BACKEND_PROBE_FAIL et al)
                _onb_line(error)
            else:
                # Reviewer MINOR fold: every ack line closes its OWN turn
                # (the beat pattern) — no two-texts-one-bubble grouping.
                _onb_line(CONFIRMED)
                if fields.get("swapped") is True:
                    _onb_line(SWITCHING_NOW)
                elif fields.get("resync") in ("deferred", "busy"):
                    _onb_line(SWITCH_AFTER_SCAN)
            return True
        if _chat_public_choice(line):
            started = set_public_backend_consent(store, backend)
            # Code-review fix 1 (TCK-DESCOPE-M3A): a consent install IS a
            # client swap — the pump's local follows the live client exactly
            # like the sibling URL/settings/deferred branches rebind, or the
            # watch drain and the ETA facts keep narrating off the stale
            # (None/retired) client for the whole session.
            client = backend.client
            _onb_line(PUBLIC_CHOSEN_ACK)
            if started:
                # F2 honesty (identical to the CLI flow's public branch):
                # "loading now" only when the release REPORTED a start.
                _onb_line(PUBLIC_LOADING_NOW)
            return True
        return False

    if scan is not None:
        scan.attach(commands)
        scan.begin()
    if provision is not None and provision.wiring is None:
        # TCK-ONB-007 fresh needs_watch_key launch ONLY: the chat-first
        # greeting GROUP — the three lines joined with \n, ONE bubble and
        # ONE turn (static-half user correction 2026-09-11), ORDER-PINNED
        # before the model-absent card below (the model-absent banner
        # flushes ahead of the pump — see the docstring). An ordinary
        # output_fn line: the CLI transport renders it identically
        # (requirement 6).
        _onb_line("\n".join(_CHAT_ONB_OPENING))
    if model is not None:
        model.attach(commands)
        if model.state == "absent":
            # The deterministic card (code-owned text; the web buttons and
            # CLI yes/no are the two answer channels). Replaces the silent
            # demo-mode banner — the user always learns the model is absent.
            # TCK-UX-012(a): each card line is a startup line — closed as its
            # own web turn (the CLI sink ignores the marker; byte-identical).
            output_fn(MODEL_CARD_QUESTION)
            if emitter is not None:
                emitter.emit(EVENT_TURN_END)
            output_fn(MODEL_CARD_HINT)
            if emitter is not None:
                emitter.emit(EVENT_TURN_END)
    if preload is not None:
        # TCK-LAUNCH-003: bind the queue only — the TRANSPORT arms the
        # load with PRELOAD_START when its channel is print-safe (the
        # wheel's build-time /dev/null dup2 window is process-wide).
        preload.attach(commands)

    def _narrate_line(line: str) -> None:
        # TCK-WEB-011 fold (UX-012 review MINOR): narration lines (watch
        # failure / recovered / incoming-tx, scan progress/summary/hooks)
        # close their own turn in web mode, like the startup lines _Output
        # already does — without the closer they merge into the NEXT reply
        # bubble and steal its turn anchor (TCK-WEB-024). Mid-turn narration
        # is untouched (it flows through the pump's text channel). The CLI
        # sink ignores the marker: byte-identical terminal output.
        output_fn(line)
        if emitter is not None:
            emitter.emit(EVENT_TURN_END)

    while True:
        if not (scan is not None and scan.in_progress):
            watch_count = _drain_watch(watcher, _narrate_line, client=client, store=store)
            if watch_count:
                loop.record_event("watch_events", watch_count)
        if ready is not None:
            ready.set()
        command = commands.get()
        if scan is not None and scan.handle_command(command, _narrate_line, emitter):
            # A swap DEFERRED behind this scan installs now that the worker
            # has delivered (and the engine has persisted) its result — then
            # the swap's own resync occupies the worker again (the pump's
            # watch drain stands down on its own in-progress check).
            if backend is not None and backend.take_deferred():
                client = backend.client
            continue
        if model is not None and model.handle_command(command, output_fn, emitter):
            # A terminal download marker closes an (implicit) turn so the
            # web client re-reads /state: the model_state flips and the
            # card/buttons resolve.
            if isinstance(command, _ModelDone) and emitter is not None:
                emitter.emit(EVENT_TURN_END)
            continue
        if preload is not None and preload.handle_command(command, output_fn):
            # TCK-LAUNCH-003: PRELOAD_START spawns the workers; _PreloadDone
            # flips the closed state machine and closes an implicit turn so
            # the client re-reads /state (model_state loading → ready/failed);
            # _IntegrityDone narrates the value-free warning inline (no state
            # change — the session keeps serving a mismatched file).
            if isinstance(command, (_PreloadStart, _PreloadDone)) and emitter is not None:
                emitter.emit(EVENT_TURN_END)
            continue
        if isinstance(command, _PumpError):
            raise command.exc
        if command is QUIT:
            break
        if isinstance(command, WatchKeyRequest):
            # TCK-LAUNCH-001 first-run watch-key entry: the ENGINE thread
            # runs the EXISTING parse+gate path (:class:`WatchKeyProvision`
            # → _wire), never a model turn, and answers with a value-free
            # status. On success (accepted/replaced) the pump REBINDS onto
            # the real wiring and starts the freshly armed startup scan —
            # the normal post-xpub sequence continues from here exactly as
            # a keyed launch. A bare submit against an already-configured
            # engine is refused ``already`` (409) UNLESS the request carried
            # the explicit replace+confirm opt-in (``allow_replace``,
            # TCK-LAUNCH-002): then the SAME gated path rewires onto the
            # new key and the pump emits the old-wallet-cache warning. The
            # key NEVER rides back.
            reply = (
                {"schema": WATCHKEY_SCHEMA, "status": "unavailable",
                 "error": _WATCHKEY_UNAVAILABLE}
                if provision is None
                else provision.provision(command.key, allow_replace=command.allow_replace)
            )
            command.reply.put(reply)
            if reply.get("status") in ("accepted", "replaced"):
                _adopt_wiring()
                if reply.get("status") == "replaced":
                    _narrate_line(_WATCHKEY_REPLACED_NOTE)
            continue
        if isinstance(command, StateSnapshotRequest):
            # Typed value-free /state read (TCK-WEB-003), answered ON the engine
            # thread — no model, no output event, no chat line; the transport
            # blocks on this reply (or falls back to transport-only on timeout).
            # TCK-WEB-005: the scan gate's closed state name + the durable
            # first-scan boolean ride the same snapshot (still enum/bool only).
            # TCK-LAUNCH-001: an additive ``needs_watch_key`` flag (a boolean,
            # value-free) tells the first-run page to show its form; the
            # shipped client ignores unknown fields, so ``state/1`` is intact.
            # TCK-LAUNCH-002: an additive ``model_state`` NAME drives the
            # Yes/No card + quick-action buttons (enum name, never data).
            # TCK-BACKEND-002: an additive ``backend_kind`` NAME (closed
            # enum, never a URL/host; TCK-DESCOPE-M3B trimmed it to
            # none/electrum/bitcoind and dropped the client's kind badge).
            # TCK-UX-010: an additive ``privacy_mode`` NAME (closed
            # :data:`PRIVACY_MODES`, never a URL/host), computed HERE —
            # the ONB-006 hold overrides the mode while a first-run backend
            # choice is outstanding; no settings context = field omitted,
            # never fabricated (the builder stays settings-free and value-free).
            privacy_mode = (
                PRIVACY_MODE_AWAITING_BACKEND
                if scan is not None and scan.gate.state == PRIVACY_MODE_AWAITING_BACKEND
                else (_backend_mode(settings) if settings is not None else None)
            )
            snapshot = build_state_snapshot(
                flow,
                session,
                watcher,
                scan,
                model,
                backend.kind if backend is not None else None,
                preload,
                privacy_mode,
            )
            if provision is not None and provision.wiring is None:
                snapshot["needs_watch_key"] = True
            command.reply.put(snapshot)
            continue
        if isinstance(command, SettingsRequest):
            # Typed /settings read/single-key write (TCK-WEB-005), answered ON
            # the engine thread — the ONLY thread that reads/writes the
            # settings table for the transport. Fail-closed validation,
            # value-free refusals, unrelated keys structurally untouched.
            # TCK-BACKEND-002: with the engine's ``backend`` controller wired,
            # a chain_base_url write additionally probes BEFORE the save and
            # hot-swaps the live client after it (or defers the install
            # behind the in-flight scan) — the pump's ``client`` local (watch
            # drain + turn facts) rebinds on an immediate swap.
            reply = handle_settings_request(
                store, command.key, command.value, backend, command.creds
            )
            command.reply.put(reply)
            if backend is not None and reply.get("swapped") is True:
                client = backend.client
            continue
        if isinstance(command, ResyncRequest):
            # Typed resync_now trigger (TCK-BACKEND-002 deliverable 5): the
            # ENGINE thread runs the existing SCAN-003 rebuild path (tags
            # survive — coin_labels is outside the scan write-set by
            # construction); the closed value-free status answers the button
            # and the scan's own progress/completion narration rides the
            # regular scan events. Emit turn_end so the web client re-reads
            # /state (scan_state flipped).
            status = backend.resync() if backend is not None else "unavailable"
            command.reply.put({"schema": RESYNC_SCHEMA, "status": status})
            if emitter is not None:
                emitter.emit(EVENT_TURN_END)
            continue
        if isinstance(command, ConsentRequest):
            # TCK-PRIVACY-001B: the web consent button — the ONE web trigger
            # of a public-backend choice, executed HERE on the engine thread
            # via the EXISTING seam (record the ONB-006 marker + release the
            # held first-run scan; no store wired = refuse). turn_end so the
            # client re-reads /state: the released scan's chip and the
            # resolved privacy_mode ride the existing machinery (no new
            # client-side inference). The closed status is value-free.
            started = store is not None and set_public_backend_consent(store, backend)
            # Code-review fix 1 (TCK-DESCOPE-M3A): a consent install moves
            # the live client — rebind the pump's local to it (no-op when
            # nothing installed), exactly like the settings-swap branch, so
            # the watch drain + ETA facts serve from the client that is
            # ACTUALLY running this session.
            if backend is not None:
                client = backend.client
            command.reply.put(
                {
                    "schema": CONSENT_SCHEMA,
                    "status": (
                        "loading" if started
                        else "recorded" if store is not None
                        else "unavailable"
                    ),
                }
            )
            if emitter is not None:
                emitter.emit(EVENT_TURN_END)
            continue
        if isinstance(command, str):
            line = command.strip()
            if line and emitter is not None:
                # TCK-WEB-011 SINGLE CHOKE POINT: every submitted line —
                # /turn free text, /action canonical utterances, quickbar
                # quick actions, slash commands — echoes onto the shared
                # bus as ``user_text`` BEFORE any branch runs, so no path
                # can be missed and every tab sees the user's message.
                emitter.emit(EVENT_USER_TEXT, sanitize_tool_output(line))
            utterance = line.lower()
            if model is not None:
                verdict = _model_card_verdict(
                    model,
                    utterance,
                    flow.state is TxFlowStatus.CREATED,
                    onboarding_listening=(
                        onboarding is not None and onboarding.is_listening
                    ),
                )
                if verdict is not None:
                    _answer_model_card(model, verdict, output_fn)
                    if emitter is not None:
                        emitter.emit(EVENT_TURN_END)
                    continue
            if (
                utterance in _QUICK_ACTION_INTENTS
                or utterance in _QUICK_STORE_COMMANDS
            ) and (provision is None or provision.wiring is not None):
                # Model-free quick action (TCK-LAUNCH-002): dispatch the
                # EXISTING handler directly (or a store read) — a code-owned
                # deterministic bypass of the LLM, never model output. Falls
                # through to the provision guard while unprovisioned.
                _run_quick_action(
                    utterance, loop, table, session, store, output_fn, backend
                )
                if emitter is not None:
                    emitter.emit(EVENT_TURN_END)
                continue
        if provision is not None and provision.wiring is None:
            # TCK-LAUNCH-001 first-run: NO wallet exists yet, so the
            # placeholder loop/table MUST never run a turn — every ordinary
            # user line is refused value-free (there is nothing to ask about
            # until the key lands). The transport's typed requests are
            # handled above, untouched. TCK-ONB-007 chat-first exceptions,
            # both PRE-MODEL and gated on needs_watch_key: the pinned "ask
            # me how" matcher answers with the deterministic export
            # guidance (no model); key-shaped chat (single key-prefixed
            # token, or BIP39-shaped) rides the EXISTING parse+provision
            # path — the mainnet-only, watch-only and seed refusals are
            # THAT path's own value-free lines, reused, never duplicated —
            # and on success the pump rebinds exactly like the typed
            # submit, then the ack + the grouped backend beat follow (the
            # beat fires only while the choice is genuinely unresolved; an
            # operator rung that already resolves it asks nothing).
            # ORDER (security/code review MINOR 2): HELP BEFORE key-
            # material — a long-lowercase help question is BIP39-SHAPE-
            # matching (the scrubber regex is shape-only), while "how" is
            # NO BIP39 word, so a real seed phrase can never hit the help
            # matcher; the converse order swallowed the help ask.
            text = command.strip() if isinstance(command, str) else ""
            if text and _chat_key_help_ask(text):
                _onb_line(CHAT_ONB_KEY_HOWTO)
                continue
            if text and _chat_key_material(text):
                reply = provision.provision(text, allow_replace=False)
                if reply.get("status") == "accepted":
                    _adopt_wiring()
                    _onb_line(CHAT_ONB_KEY_SAVED)
                    assert settings is not None and store is not None
                    if not _backend_resolved(
                        settings.chain_base_url.strip() or None, store
                    ):
                        # ONE grouped bubble (the greeting group's mirror
                        # structure — pinned; static-half user
                        # correction 2026-09-11).
                        _onb_line("\n".join(_CHAT_ONB_BACKEND_BEATS))
                else:
                    error = reply.get("error")
                    _onb_line(
                        error
                        if isinstance(error, str) and error
                        else _WATCHKEY_UNAVAILABLE
                    )
                continue
            if text and _chat_key_help_ask(text):
                _onb_line(CHAT_ONB_KEY_HOWTO)
                continue
            _onb_line(WATCHKEY_REQUIRED_NOTICE)
            continue
        line = command.strip()
        if not line:
            continue
        if line.lower() in ("exit", "quit"):
            break
        if line.startswith("/"):
            _handle_transcript_command(
                line,
                loop,
                output_fn,
                flow=flow,
                session=session,
                store=store,
                onboarding=onboarding,
            )
        elif onboarding is not None and onboarding.handle_line(line, output_fn):
            # TCK-ONB-003/005: consumed on the deterministic onboarding
            # channel (the node ask's own vocabulary; armed only by the
            # first-run startup ask or a /setup command) — never model
            # context, never the dispatcher. A dormant flow consumes
            # nothing. The CLI ask owns its vocabulary FIRST, so this
            # branch always precedes the chat-backend intercept below.
            pass
        elif _chat_backend_intent(line):
            # TCK-ONB-007: chat URL paste / public answer while the backend
            # is UNRESOLVED (web chat; a CLI launch's armed flow consumed
            # its own vocabulary above). Every ack/refusal line already
            # closed its OWN turn (the _onb_line pattern, code-review
            # MINOR fold) — skip the shared closer so no empty marker
            # follows. Ordinary lines, and EVERYTHING on a resolved launch,
            # fall through to the turn below — post-setup URLs are never
            # swallowed (state gate pinned).
            continue
        else:
            _run_turn(
                loop, flow, session, line, output_fn, client=client, table=table,
                scan_gate=scan.gate if scan is not None else None, hwi=hwi,
                store=store,
            )
        if emitter is not None:
            emitter.emit(EVENT_TURN_END)
    if scan is not None:
        scan.drain_until_complete(_narrate_line, emitter)
    if model is not None:
        # Session end (QUIT / process exit): terminate a live download
        # child BOUNDED and join its reader — no orphaned downloader
        # processes. The resumable partial file is deliberately kept.
        model.cancel()


def main(argv: Sequence[str] | None = None, **run_kwargs: Any) -> int:
    """Console entry point (TCK-LAUNCH-001: the WEB-first launch).

    The bare command (``python -m localwallet.ui.cli``) serves the
    localhost web UI and best-effort opens the browser; ``--cli`` /
    ``LOCALWALLET_UI=cli`` keeps the terminal REPL. Delegates to
    :func:`run` — the programmatic default there is unchanged (CLI) so
    existing harnesses/tests are untouched by the flip. ``run_kwargs``
    forwards the test seams (``output_fn``/``on_web_server``/…) that
    parking on the server would otherwise make unreachable from a test.
    """
    return run(argv, default_web=True, open_browser=True, **run_kwargs)


def run(
    argv: Sequence[str] | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    flow: TxFlow | None = None,
    generate_fn: GenerateFn | None = None,
    node_detect_fn: Callable[[], LocalNodeReport] | None = None,
    on_web_server: Callable[[WebServer], None] | None = None,
    interactive: bool | None = None,
    backend_check_fn: Callable[[str], str | None] | None = None,
    default_web: bool = False,
    open_browser: bool = False,
) -> int:
    """Wire the application from ``argv``/environment and run the REPL
    (programmatic default) or the web UI (the entry-point default,
    TCK-LAUNCH-001 — see ``default_web``).

    Configuration precedence: ``--zpub`` overrides ``LOCALWALLET_ZPUB``,
    which overrides the STORED watch key (the wallets-table descriptor
    from an earlier launch — TCK-LAUNCH-001); a web launch with no key on
    any rung serves the first-run watch-key form instead of refusing;
    ``LOCALWALLET_GAP_LIMIT`` (validated fail-closed at startup, exit 2 on a
    malformed value) overrides the DB ``gap_limit`` setting for every scan,
    which in turn falls back to the default 20 (ADR-0009); ``--signer``
    overrides ``LOCALWALLET_SIGNER`` for the signing backend
    (default ``file``; the HWI signer is constructed lazily only when
    selected, with the expected fingerprint from the parsed wallet key;
    the file signer's transfer folder comes from
    ``LOCALWALLET_SIGNER_DIR`` or ``./psbt-transfer``, created on
    demand); the model runtime is picked as remote debug bridge
    (:data:`~localwallet.agent.remote_runtime.LLM_BASE_URL_ENV_VAR`, ADR-0007,
    with a one-line disclosure), then the real local model
    (:data:`MODEL_PATH_ENV_VAR`), then ``--stub-llm`` dev mode. The
    wallet profile is reused when the store already holds the same
    descriptor, else created (``name="default"``; ADR-0010
    single-wallet). The startup scan (``--rescan`` for the full repair
    scan) runs NON-BLOCKING on the dedicated chain worker (TCK-SCAN-003,
    ADR-0022): the REPL prompt goes live immediately, dots flow between
    turns, and the engine persists + narrates the result when it lands —
    while ``create_tx`` stays gated until that first scan completes.
    ``LOCALWALLET_AUTO_SCAN=0`` skips
    it — the first balance/created-tx lookup then scans lazily. Chain/store
    failures at startup print a scrubbed warning and the REPL still
    starts; handlers surface store-empty / chain-down states per turn.

    The REPL owns the send-flow state machine: ``flow`` defaults to a
    :class:`TxFlow` with the real clock and uuid id factory; tests inject
    a deterministic one (controllable clock for expiry, capturable
    reference for confirm envelopes) through this seam. ``generate_fn``
    likewise injects a bare model callable ahead of the env/flag
    selection (send-flow e2e tests quote the flow's real pending
    ``tx_ref``, which the canned stub cannot know). Pending
    confirmations are session-scoped (ADR-0013): the flow lives in
    memory for this process only, and a pending transaction — including
    its confirmation state — is lost when the app exits.

    Args:
        argv: CLI arguments (defaults to ``sys.argv[1:]``).
        input_fn: REPL line reader (``input``-compatible; test seam).
        output_fn: REPL/banner writer (``print``-compatible; test seam).
        flow: The :class:`TxFlow` for the send flow (test seam; a fresh
            real-clock instance by default).
        generate_fn: A bare ``generate(prompt, grammar) -> str`` model
            callable used as-is when provided (test seam).
        node_detect_fn: A bare zero-argument callable running the local-node
            detection pass (test seam), forwarded to the ``node_status``
            handler. Defaults to the real :func:`detect_local_nodes` over
            the resolved settings (advise-only; honors
            LOCALWALLET_NODE_DETECTION_ENABLED).
        on_web_server: Web mode only (test seam, TCK-WEB-002): called with
            the started :class:`~localwallet.ui.web.server.WebServer` once
            the engine bootstraps and the launch lines are printed, before
            ``run`` parks on the server.
        interactive: ADR-0023 onboarding gate (TCK-ONB-003; test seam).
            ``None`` (production) means "stdin's tty state" — a headless
            or scripted launch NEVER enters the first-run conversation and
            is never blocked by it; ``True`` drives the conversation with
            the injected ``input_fn``.
        backend_check_fn: The step-5 URL-validation probe AND M3 kind
            classifier (test seam, TCK-ONB-003/TCK-ONB-004 M3):
            ``base_url -> canonical backend URL to store, or None to
            refuse``. Defaults to the bounded app-side probe (chain/ is the
            only networked module) on the resolved timeout settings.

    Returns:
        Process exit code: ``0`` on normal exit (including ``exit``,
        Ctrl-D, Ctrl-C), ``2`` on configuration errors (refused key,
        store failure, malformed config, busy/unusable fixed port). No
        configured model is NO LONGER an error — it falls back to the
        stub with a visible banner (TCK-LAUNCH-001). A missing key is
        exit 2 only for a headless CLI launch; web launches serve the
        first-run form instead. Configuration errors never echo the key.
    """
    args = _parse_args(argv)

    # TCK-ONB-003 launch gates. ``interactive`` is the test/automation seam
    # (production: ``None`` → stdin's tty state; a closed or redirected
    # stdin counts as headless — the ADR-0023 rule that scripted launches
    # are NEVER blocked by a conversation). The web transport never gets the
    # onboarding conversation (requirement 5: terminal-only).
    #
    # TCK-LAUNCH-001 (ADR-0024 §11 amendment): the ENTRY point (main)
    # launches the WEB UI by default. Flag wins over env over the default:
    # ``--cli`` forces the REPL, ``--web`` forces the browser front,
    # ``LOCALWALLET_UI=cli`` opts out of the web default, and only the
    # EXACT value "cli" does. Direct programmatic ``run`` callers keep the
    # pre-flip CLI default unless they pass ``default_web=True``.
    env_ui = os.environ.get(UI_ENV_VAR, "").strip().lower()
    if args.cli:
        web_mode = False
    elif args.web:
        web_mode = True
    else:
        web_mode = env_ui == "web" or (default_web and env_ui != "cli")
    if interactive is None:
        try:
            is_interactive = sys.stdin.isatty()
        except (OSError, ValueError):  # closed stdin: headless
            is_interactive = False
    else:
        is_interactive = bool(interactive)

    # TCK-CFG-002: load settings from env + the config file. A malformed
    # config file (bad JSON / wrong type / unknown key) raises ValueError
    # here — refuse startup with a value-free line (exit 2) BEFORE any store
    # side effects, mirroring the gap_limit/zpub config-error paths below.
    # (Moved ahead of the key resolution by TCK-LAUNCH-001: the STORED
    # watch-key rung needs ``settings.store_path``.)
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        # Pre-log: settings (hence the store path) are unreadable, so the log
        # location is unknowable — console-only (stderr in web, terminal in CLI).
        line = f"Configuration error: {exc}"
        if web_mode:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
        else:
            output_fn(line)
        return 2

    # TCK-APP-LOG-001: per-launch error log beside the store DB + the
    # mode-aware output router. ``log``/``output`` are the SINGLE output
    # channel from here down (narration → terminal/emitter; errors/warnings →
    # console + log) so CLI stays unchanged and web narration reaches the
    # browser instead of the terminal.
    log = _Log(settings.store_path, "web" if web_mode else "cli")
    output = _Output(web=web_mode, terminal=output_fn, log=log)

    # Watch key precedence (TCK-LAUNCH-001, documented deviation from the
    # ticket's "stored > env > flag" sketch — the flag/env rungs must keep
    # overriding for backward compatibility): ``--zpub`` >
    # ``LOCALWALLET_ZPUB`` > the STORED wallet row (the wallets-table
    # descriptor carries the accepted key from whichever rung supplied it
    # the first time any launch persisted a wallet — "if they have given
    # us a zpub we use that one"). No key anywhere: a WEB launch continues
    # UNPROVISIONED (the page's first-run form supplies it through the
    # same parse+gate path); headless CLI keeps the exit-2 refusal; an
    # interactive CLI first launch is GREETED and asked (ADR-0023 step 1,
    # :func:`ask_watch_key` — validates through the same gated parser;
    # None = the user exited).
    zpub = (args.zpub or os.environ.get(ZPUB_ENV_VAR, "")).strip()
    descriptor: WalletDescriptor | None = (
        None if zpub else _stored_watch_descriptor(settings.store_path)
    )
    if not zpub and descriptor is None:
        if web_mode:
            pass  # first-run web flow: provisioning owns the key from here
        elif not is_interactive:
            output.error(f"No watch key configured: pass --zpub or set {ZPUB_ENV_VAR}.")
            return 2
        else:
            asked = ask_watch_key(input_fn, output_fn)
            if asked is None:
                return 0
            zpub = asked
    if zpub:
        try:
            # Gated parse (mainnet-only gate enforced at parse time — flip
            # per ADR-0021) plus the canonical wallet descriptor; the
            # stored rung already came back through the same gate in
            # _stored_watch_descriptor. Value-free WatchKeyErrors.
            descriptor = WalletDescriptor.from_key(zpub)
        except WatchKeyError as exc:
            output.error(f"Watch key rejected: {exc}")
            return 2

    # Signer selection (TCK-P3-005): --signer overrides LOCALWALLET_SIGNER;
    # default "file". The HwiUsbSigner object itself is constructed lazily
    # (per sign attempt, inside the sign_tx handler) ONLY when the hwi kind
    # is selected — the expected fingerprint comes from the parsed wallet
    # key. The file signer's transfer folder is created on demand at the
    # first export. Invalid selections are a startup config error (exit 2).
    signer_kind = (
        (args.signer or os.environ.get(SIGNER_ENV_VAR, "")).strip().lower()
        or SIGNER_KIND_FILE
    )
    if signer_kind not in _SIGNER_KINDS:
        output.error(
            f"Invalid signer selection: use --signer file|hwi or set "
            f"{SIGNER_ENV_VAR}=file|hwi."
        )
        return 2
    signer_selection = SignerSelection(
        kind=signer_kind,
        dir_path=Path(
            os.environ.get(SIGNER_DIR_ENV_VAR, "").strip() or DEFAULT_SIGNER_DIR
        ),
        # The account-key fingerprint from the parsed wallet key; the
        # first-run WEB flow has no key yet, and WatchKeyProvision
        # re-builds this with the real fingerprint at provisioning time —
        # the placeholder never reaches a signer or the store.
        fingerprint_hex=(
            descriptor.parsed.hd_key.my_fingerprint.hex()
            if descriptor is not None
            else ""
        ),
    )

    # Pre-flight (SR minor): if the remote debug bridge is opted into but no
    # model id resolves, fail at startup (exit 2, mirroring the zpub config
    # error) instead of selecting a runtime that would fail every turn. The
    # message never echoes the env value.
    remote_base_url = os.environ.get(LLM_BASE_URL_ENV_VAR, "").strip()
    remote_model = os.environ.get(LLM_MODEL_ENV_VAR, "").strip()
    if remote_base_url and not remote_model:
        # Original channel preserved (stderr) + the same value-free line goes
        # to the per-launch error log (TCK-APP-LOG-001 one code path).
        line = (
            f"No model configured for the remote bridge: set {LLM_MODEL_ENV_VAR} "
            f"alongside {LLM_BASE_URL_ENV_VAR}."
        )
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
        output.log_error(line)
        return 2

    generate: ModelRuntime | GenerateFn | RemoteOpenAIRuntime
    model_flow: ModelDownloadFlow | None = None
    preload_flow: ModelPreloadFlow | None = None
    if generate_fn is not None:
        # Injected bare model callable (test seam) — used ahead of the
        # env/flag selection; flows through handle_raw like any runtime.
        generate = generate_fn
    elif remote_base_url:
        # ADR-0007 (TEMPORARY debug bridge): selected only via explicit env,
        # with a one-line disclosure that chat text leaves this machine.
        generate = RemoteOpenAIRuntime()
        output_fn(debug_notice(remote_base_url, remote_model))
    elif os.environ.get(MODEL_PATH_ENV_VAR):
        generate = ModelRuntime()
        # TCK-LAUNCH-003 (user direction 11): a REAL local model at the env
        # rung preloads on a background thread at engine start so the FIRST
        # query stops paying the multi-GB build. Only when the file is
        # actually there (else the pre-existing per-turn error stands);
        # no launch checksum for an arbitrary path — nothing pins it, and
        # the app never invents a verdict against no pin. The hasattr guard
        # is the test seam that swaps in a faked runtime with no load hook.
        env_model = os.environ.get(MODEL_PATH_ENV_VAR, "")
        if env_model and Path(env_model).is_file() and hasattr(generate, "load"):
            preload_flow = ModelPreloadFlow(
                generate,  # type: ignore[arg-type]  # env rung: always ModelRuntime
                model_path=env_model,
                sha256=_manifest_pin_for(Path(env_model)),
                log_fn=log.warning,
            )
    elif args.stub_llm:
        # Explicit dev choice (unchanged from TCK-LAUNCH-001): the stub
        # without a banner and without a download card — the operator
        # already knows exactly what they are running.
        generate = stub_generate
    else:
        # TCK-LAUNCH-002 (ADR-0001 amendment): NO explicit model means the
        # DEFAULT pinned model — not the stub. Resolution:
        #   default file present  → the real GGUF runtime, silently (that
        #                           is the normal, expected launch);
        #   default file absent   → the demo stub KEEPS the session alive,
        #                           but the engine arms the deterministic
        #                           Yes/No download card (no more silent
        #                           demo mode when the user simply has not
        #                           downloaded yet);
        #   no resolvable pinned  → the old plain banner (nothing the app
        #                           default (manifest unreadable)   could
        #                           offer to download).
        default = _resolve_default_model()
        if default is not None and default[1].is_file():
            generate = ModelRuntime(model_path=str(default[1]))
            # TCK-LAUNCH-003: the normal, expected launch. The GGUF loads
            # on a background thread NOW (user direction 11) and its bytes
            # are checksummed CONCURRENTLY against the manifest pin (user
            # direction 4 part 2) — mismatch warns value-free, serving
            # continues (documented in ModelPreloadFlow).
            if hasattr(generate, "load"):
                preload_flow = ModelPreloadFlow(
                    generate,
                    model_path=str(default[1]),
                    sha256=_manifest_pin_for(default[1]),
                    log_fn=log.warning,
                )
        elif default is not None:
            generate = stub_generate
            model_flow = ModelDownloadFlow(model_name=default[0])
        else:
            generate = stub_generate
            output(NO_MODEL_DEMO_BANNER)

    # TCK-CFG-001 preflight: resolve + validate LOCALWALLET_GAP_LIMIT
    # (fail-closed, value-free — the same spirit as the zpub config-error
    # path above). A malformed value refuses startup with exit 2 BEFORE any
    # store side effects; a valid value is threaded into every scan below as
    # the per-call gap_limit so it overrides the DB setting (ADR-0009).
    try:
        env_gap = _env_gap_limit(settings)
    except ValueError as exc:
        output.error(f"Configuration error: {exc}")
        return 2

    # TCK-WEB-002 (ADR-0024 §1/§11, default since TCK-LAUNCH-001): the web
    # UI shares every config decision above; the split is only at the
    # input/output seam — _run_web runs the SAME wiring inside
    # start_engine's engine-thread bootstrap (closing TCK-WEB-001's
    # deferred deviation) and serves the loopback HTTP/SSE front instead
    # of the REPL. ``descriptor is None`` = first-run launch (no key on
    # any rung): the server starts unprovisioned and the page's
    # watch-key form completes the wiring through the pump.
    if web_mode:
        return _run_web(
            descriptor=descriptor,
            signer_selection=signer_selection,
            settings=settings,
            env_gap=env_gap,
            rescan=args.rescan,
            flow=flow,
            generate=generate,
            node_detect_fn=node_detect_fn,
            output_fn=output_fn,
            output=output,
            on_web_server=on_web_server,
            open_browser=open_browser,
            model=model_flow,
            preload=preload_flow,
        )

    assert descriptor is not None  # CLI reaches here only with a key
    try:
        wiring = _wire(
            parsed=descriptor.parsed,
            descriptor=descriptor,
            signer_selection=signer_selection,
            settings=settings,
            env_gap=env_gap,
            rescan=args.rescan,
            flow=flow,
            generate=generate,
            node_detect_fn=node_detect_fn,
            output_fn=output,
            cli_interactive=is_interactive,
            backend_check_fn=backend_check_fn,
            output=output,
        )
    except _WiringError as exc:
        output.error(str(exc))
        return 2

    try:
        _repl(
            wiring.loop,
            output,
            input_fn,
            flow=wiring.flow,
            session=wiring.session,
            watcher=wiring.watcher,
            client=wiring.client,
            table=wiring.table,
            scan=wiring.scan,
            store=wiring.store,
            onboarding=wiring.onboarding,
            model=model_flow,
            preload=preload_flow,
            backend=wiring.swap,
            hwi=wiring.hwi,
        )
    except KeyboardInterrupt:
        pass  # clean exit on Ctrl-C
    finally:
        wiring.worker.stop()  # join the chain worker before closing its client
        if wiring.client is not None:
            wiring.client.close()
        wiring.store.close()
        # The remote debug bridge also owns a client (httpx) — close it
        # alongside the chain client when it exposes close().
        close = getattr(generate, "close", None)
        if callable(close):
            close()
        log.close()
    return 0


class _WiringError(Exception):
    """A startup wiring failure that pre-web ``run`` exited 2 on; carries
    the exact user-facing (value-free) line. Raised from :func:`_wire` so
    both transports report it identically (CLI inline, web via the engine
    bootstrap)."""


@dataclass
class _Wiring:
    """The stateful engine pieces :func:`_wire` builds (ADR-0024 §3: in web
    mode these are all constructed ON the engine thread — the Store's
    ``check_same_thread`` is the guard)."""

    store: Store
    #: The WALLET chain client (Electrum or bitcoind, ADR-0018 as amended),
    #: or ``None`` while the backend is UNRESOLVED (TCK-DESCOPE-M3A: there
    #: is no silent public client anymore — unresolved means nothing to
    #: query, and every chain-riding surface is gate-held until a consent
    #: or a save installs the real client through the hot-swap seam).
    client: ChainClient | None
    loop: AgentLoop
    flow: TxFlow
    session: SendSession
    table: DispatchTable
    watcher: IncomingWatcher | None
    worker: ChainWorker
    scan: ScanFlow
    #: The runtime settings whose ``chain_base_url`` field carries the
    #: boot-resolved effective backend (env > config file > stored, folded
    #: by :func:`_wire`). The hot-swap mutates this ONE field and rebuilds
    #: from it (TCK-BACKEND-002), so the client, the banner classification
    #: and the node_status narration keep resolving the SAME value.
    settings: Settings = dataclass_field(repr=False)
    #: The parsed account key + active wallet row the swap needs to REBUILD
    #: the three chain-riding dispatch-table handlers (create_tx /
    #: broadcast_tx / tx_status) over a fresh client — everything else in
    #: the table is store/flow-only and survives a swap untouched.
    parsed: ParsedKey = dataclass_field(repr=False)
    wallet: WalletRecord
    #: TCK-ONB-003/005 (ADR-0023): the backend conversation for THIS session
    #: — armed at startup on a first-run CLI launch, DORMANT on every other
    #: interactive CLI launch (the /setup command arms it), and always
    #: ``None`` for web (the browser never gets an onboarding surface, only
    #: :data:`WEB_SETUP_HINT`).
    onboarding: OnboardingFlow | None = None
    #: The env/config-file rung of the backend ladder AS PRESENTED to
    #: :func:`_wire` (before the stored fold): non-empty means the STORED
    #: rung is shadowed (ADR-0023 precedence) — a stored write then takes
    #: effect at NEXT launch only, and the hot-swap honestly declines
    #: (TCK-BACKEND-002: the swap follows the same precedence the client
    #: construction does; the ladder itself stays single-sourced in config).
    boot_backend: str = ""
    #: TCK-UX-011 (ADR-0022 amendment 2): the TRANSPORT the wiring serves
    #: (True in web/engine mode). The get_balance handler is rebuilt on
    #: every backend hot-swap and must keep the same stand-down posture,
    #: so it rides the wiring, not just the initial table build.
    defer_scans: bool = False
    #: The engine-thread hot-swap controller (TCK-BACKEND-002; built by
    #: :func:`_wire` right after the wiring itself — late-bound because it
    #: owns a reference to the wiring it can mutate).
    swap: ChainBackendFlow | None = None
    #: TCK-DESCOPE-M3A (plan §4): the ONE standalone PUBLIC-INFO fetcher
    #: (mempool.space fees/prices) the fee estimator and price oracle ride
    #: REGARDLESS of the wallet backend — constructed once, never rebuilt
    #: on a hot-swap, never carries wallet data.
    public_info: PublicInfoClient | None = None
    #: The one shared :class:`FeeEstimator` (bids over ``public_info``, min-
    #: relay floor over the wallet client's optional capability) — kept on
    #: the wiring so a hot-swap rebind reuses it (one source, one cache,
    #: one TTL); the swap only re-points its FLOOR at the new backend
    #: (TCK-FEE-004 code-review fix).
    fee_estimator: FeeEstimator | None = None
    #: The one shared :class:`PriceOracle` (over ``public_info`` and the
    #: live display-currency ladder), same single-instance rule as the
    #: estimator (TCK-FIAT-001's shared-cache invariant, backend-free
    #: since M3A).
    price_oracle: PriceOracle | None = None
    #: TCK-HW-005 slice A: the stateless HWI signer for the chat
    #: probe/unlock interception (built by :func:`_wire` from THIS wallet's
    #: account key — construction is cheap, hwilib imports lazily inside
    #: it, and the object signs nothing on this path).
    hwi: HwiUsbSigner | None = None
    #: The mode-aware output router (TCK-DIAG-003): kept on the wiring so
    #: the hot-swap rebind of the ``broadcast_tx`` handler re-attaches the
    #: console/log debug companion too (None = test seam, emits nothing).
    output: _Output | None = dataclass_field(default=None, repr=False)


@dataclass(frozen=True)
class _BackendAuth:
    """A resolved backend-credential overlay (TCK-ONB-004 M3). Consumed
    ONLY by the Bitcoin Core RPC surface (probe + client build): the
    Electrum protocol has no standard auth and the Esplora API surface
    takes none, so a stored pair is inert for those kinds (documented —
    plan OQ-5's "rarely needed" answer applied: not sent, not lost).

    * ``user``/``password``: the stored basic-auth pair (BOTH present or
      the resolver returns ``None`` instead — a half pair is the documented
      cookie-default path, never a silent partial login);
    * ``omit``: the explicit "no credentials needed" checkbox — the
      ``Authorization`` header is OMITTED entirely and the cookie file is
      never consulted (the plan §3 semantics).
    """

    user: str | None = None
    password: str | None = None
    omit: bool = False


def _backend_auth(store: Store) -> _BackendAuth | None:
    """Resolve the stored credential rung (env/config-file URL userinfo and
    the cookie default live INSIDE the adapters, under this overlay — the
    ladder never double-applies). Interplay (ticket's documented rule):
    checkbox SET → omit auth; box UNSET + filled pair → basic auth; box
    UNSET + empty/half → ``None`` = the client's own default ladder (URL
    userinfo if any, else the cookie file, else no auth — M2's behavior,
    unchanged when nothing is stored). An unreadable store resolves to
    ``None`` (fail quiet toward the documented default, fail CLOSED toward
    never sending a half-guessed credential)."""
    try:
        if store.get_backend_auth_none():
            return _BackendAuth(omit=True)
        user = store.get_backend_auth_user()
        password = store.get_backend_auth_pass()
    except (StoreError, sqlite3.Error):
        return None
    if user and password:
        return _BackendAuth(user=user, password=password)
    return None


def _bitcoind_auth_kwargs(auth: _BackendAuth | None) -> dict[str, object]:
    """The BitcoindClient constructor kwargs for one resolved overlay
    (``None`` = nothing stored: M2's default ladder rides untouched)."""
    if auth is None:
        return {}
    if auth.omit:
        return {"no_credentials": True}
    if auth.user and auth.password:
        return {"rpc_user": auth.user, "rpc_password": auth.password}
    return {}


def _build_chain_client(
    settings: Settings, auth: _BackendAuth | None = None
) -> ChainClient:
    """Construct the config-selected WALLET chain backend (TCK-ONB-004
    M1/M2; the M3 ``auth`` overlay threads the stored credential keys;
    TCK-DESCOPE-M3A closes the family).

    The URL SCHEME picks the adapter through the single selection point
    (:meth:`ChainConfig.from_settings`, ADR-0018 as amended): an
    ``ssl://host[:port]`` base rides the Electrum-protocol client, a
    ``bitcoind://``/``bitcoind+tls://`` base the Bitcoin Core RPC client.
    An http(s) (Esplora-shaped) base REFUSES construction here: since the
    2026-09-11 redirection, wallet information comes only from Electrum or
    bitcoind — mempool.space serves public fees/prices through
    :class:`~localwallet.chain.publicinfo.PublicInfoClient`, never wallet
    data. Construction is network-free in both kinds (clients connect
    lazily; the Electrum and Core handshakes — including the mainnet-only
    proof, ADR-0021 — run on the first call), and timeout/retry/TLS-trust
    values come from the SAME resolved settings, so the privacy banner,
    the watch-mode line and the transport can never disagree. A missing
    selection (empty ``chain_base_url`` = UNRESOLVED) or a malformed one
    fails closed here with the value-free :class:`ValueError`
    ``ChainConfig`` has always raised at construction.
    """
    config = ChainConfig.from_settings(settings)
    if config.kind == "bitcoind":
        # Auth rides the SAME resolved settings: URL userinfo (the
        # env/config-file rung) for user/pass, ``settings.rpc_cookie_path``
        # for the cookie file ("" → the documented ~/.bitcoin/.cookie
        # default); M3's stored rung (``backend_auth_user``/``_pass``/
        # ``_none``, resolved by :func:`_backend_auth`) overlays through
        # the constructor pair the M2 seam was left open for. URL userinfo
        # outranks the stored pair inside the client — matching the ladder
        # precedence the env rung already has.
        return BitcoindClient(
            base_url=config.base_url,
            timeout_s=config.timeout_s,
            max_retries=config.max_retries,
            rpc_cookie_path=settings.rpc_cookie_path,
            **_bitcoind_auth_kwargs(auth),
        )
    if config.kind == "electrum":
        return ElectrumClient(
            base_url=config.base_url,
            timeout_s=config.timeout_s,
            max_retries=config.max_retries,
        )
    # TCK-DESCOPE-M3A: no other kind is a WALLET backend (the Esplora
    # shape is public-info-only; see the docstring). Value-free.
    raise ValueError("wallet chain backend must be an Electrum or bitcoind URL")


def _public_info_client(settings: Settings) -> PublicInfoClient:
    """The ONE public fee/price fetcher (TCK-DESCOPE-M3A, plan §4): built
    ONCE per wiring, independent of the wallet backend and never rebuilt
    on a hot-swap (public aggregations — payloads carry no addresses)."""
    return PublicInfoClient(settings)


def _probe_tip(client_factory: Callable[[], Any]) -> tuple[bool, BaseException | None]:
    """Run ONE candidate client through its own handshake (one tip call)
    and bound the wreckage: construction, connect, shape, wrong chain, a
    401 — ANYTHING collapses to ``False``, and the probe client is always
    closed. The caller owns the one honest refusal line; nothing this
    swallows ever escapes (value-free by construction). Returns
    ``(accepted, exc)`` where ``exc`` is the swallowed failure (or ``None``)
    so the caller can emit a TCK-DIAG-001 debug companion."""
    client: Any = None
    try:
        client = client_factory()
        client.get_tip_height()  # connect + handshake + mainnet gate
        return True, None
    except Exception as exc:  # noqa: BLE001 — the collapse-everything contract
        return False, exc
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001, S110 — a dead probe cannot fail
                pass


#: The CLOSED scheme set :func:`_probe_chain_backend` actually dispatches on.
#: :func:`_probe_url_class` clamps a candidate to exactly these, so the
#: ``url-class=`` debug field can never carry a host, address, or credential.
_KNOWN_PROBE_SCHEMES: Final[tuple[str, ...]] = (
    ELECTRUM_SCHEME,  # ssl://
    BITCOIND_SCHEME,  # bitcoind://
    BITCOIND_TLS_SCHEME,  # bitcoind+tls://
    "http://",
    "https://",
)


def _probe_url_class(text: str) -> str:
    """The value-free URL-class (scheme) for a probe-refusal debug line.

    Clamps to the known dispatch schemes, else the literal ``"unknown"``.
    The raw ``partition(":")[0]`` of arbitrary user text would echo a host
    (``192.168.1.5:8332``), an entire address (``bc1…``), or a credential
    (``user:pass@host``) into the console/log — never allowed (TCK-DIAG-001
    security review)."""
    for scheme in _KNOWN_PROBE_SCHEMES:
        if text.startswith(scheme):
            return scheme.rstrip(":/")
    return "unknown"


def _failure_parts(exc: BaseException | None) -> tuple[str, str, str]:
    """Value-free ``(failure_class, exception_class_name, debug-extra)`` for
    a debug line (TCK-DIAG-001). Prefers the structured class the chain/
    adapters attach to :class:`ChainError`; otherwise derives it from the
    exception type. The third element is a pre-formatted, value-free suffix
    — currently only the NUMERIC JSON-RPC error code an ``rpc-error``
    refusal carried (TCK-DIAG-002: protocol constants, not user data; the
    server's message text never rides)."""
    fc = getattr(exc, "failure_class", None) or classify_failure(exc)
    name = getattr(exc, "exc_name", None) or type(exc).__name__
    code = getattr(exc, "rpc_code", None)
    extra = f" code={code}" if isinstance(code, int) and not isinstance(code, bool) else ""
    return fc, name, extra


def _emit_probe_failure(
    output: _Output | None,
    *,
    stage: str,
    url_class: str,
    fc: str,
    name: str,
    extra: str = "",
) -> None:
    """The console/log debug companion for a rejected probe URL (TCK-DIAG-001).
    Value-free: only the failure class, the probe stage, the CLAMPED
    URL-class (a known scheme or the literal ``unknown`` — never a host,
    credential, address, or amount), the exception class name, and the
    optional value-free debug extra (an RPC error CODE). A no-op when no
    ``output`` router is present (the test seam / direct-call path)."""
    if output is None:
        return
    output.warning(
        f"backend probe rejected: stage={stage} url-class={url_class} "
        f"class={fc} exc={name}{extra}"
    )


def _probe_chain_backend(
    url: str,
    settings: Settings,
    auth: _BackendAuth | None = None,
    output: _Output | None = None,
) -> str | None:
    """The ONE bounded, value-free readiness probe AND kind classifier for a
    CANDIDATE backend URL (TCK-BACKEND-002 deliverable 2; the auto-detect
    entry of TCK-ONB-004 M3 — the user never names the kind, the app "does
    its best" per plan §3). Returns the CANONICAL URL to store — the input
    unchanged unless the M3 classifier rewrote it — or ``None`` for a
    refusal (every failure collapses identically to the caller's one honest
    line; no host, status or detail ever rides the answer).

    Classification (scheme first, probe decides only what the scheme cannot):

    * ``ssl://`` → Electrum (the scheme is unambiguous): one
      :class:`ElectrumClient` tip call forces M1's fail-closed HANDSHAKE
      (``server.version`` + ``server.features`` whose ``genesis_hash`` must
      equal the mainnet constant — the entry-time GENESIS gate is the
      adapter's own, reused verbatim), then a bounded close. Stored as-is.
    * ``bitcoind://`` / ``bitcoind+tls://`` → explicit Core RPC choice (the
      M2 scheme and its https TLS sibling, TCK-BACKEND-003): one
      :class:`BitcoindClient` tip call through the SAME handshake the live
      client runs (``getblockchaininfo.chain == "main"`` + the Core-22
      capability floor + the auth matrix — a 401 collapses to refusal). The
      stored rung carries it WITHOUT userinfo (the store's writer);
      credentials ride the dedicated ``backend_auth_*`` keys via ``auth``.
      Stored as-is (the transport bit is the scheme, so it survives the
      save and rebuilds the identical live client every later launch).
    * ``http://`` / ``https://`` → AMBIGUOUS INPUT ALIASES FOR BITCOIN CORE
      RPC ONLY (TCK-DESCOPE-M3B re-scope: an Esplora-shaped http(s) server
      is NOT a wallet backend anymore — mempool.space serves public
      fees/prices only, ADR-0003/0011 as amended). ONE attempt: the Core
      JSON-RPC SHAPE against the canonical rewrite (a
      :class:`BitcoindClient` over the ``bitcoind://``/``bitcoind+tls://``
      form of the URL, POST ``getblockchaininfo`` with the resolved
      credentials — stored pair, else the documented cookie ladder — over
      ``auth``). A Core win STORES THE REWRITE (``http://h:8332`` →
      ``bitcoind://h:8332``; https → the ``bitcoind+tls://`` TLS sibling,
      TCK-BACKEND-003 D1: the https RPC URL is an INPUT alias, the canonical
      STORED form carries the transport through the ONE scheme-dispatch
      seam) so ``backend_kind``, the client builder and the settings pane
      all ride one unchanged scheme read. NO Core win → ``None`` REFUSED —
      there is deliberately no Esplora-shape fallback probe anymore, so a
      mempool.space-style URL can never be persisted through this seam
      (the M3A interim gap closed). URLs carrying userinfo are refused
      WITHOUT a probe (embedded credentials are refused on the stored rung
      — logins ride the dedicated keys).

    PORTS ARE NOT TRUSTED FOR CLASSIFICATION (M3 decision, plan §3's
    "never bypass the probe" hardened): no 8332/3006 heuristic steers the
    decision — scheme + shape probe is everything, because a port is a
    convention any real deployment (proxies, Docker mappings, Start9 app
    ports) breaks. The ambiguous rungs cost at most ONE bounded attempt.

    Snappy budget (same rule as ever): ONE attempt past the initial, the
    shared per-request timeout; TLS trust rides the ladder inside the
    clients, so a probe can never disagree with the transport policy the
    real client would get.
    """
    text = url.strip()
    if not text:
        return None
    timeout = settings.request_timeout_s
    retries = min(settings.max_retries, 1)
    # The URL-class for the debug line is the CLAMPED scheme — only schemes
    # this probe actually dispatches on, else the literal "unknown". Never a
    # host, address, or credential: scheme-less user text (a pasted bc1…
    # address, an IP:port, "user:pass@host") can reach the refusal line, and
    # the raw partition-on-":" would leak it verbatim (TCK-DIAG-001 security
    # review). The host echo allowance of UX-009 is NOT used here.
    url_class = _probe_url_class(text)

    def _refuse(stage: str, fc: str, name: str, extra: str = "") -> None:
        _emit_probe_failure(
            output, stage=stage, url_class=url_class, fc=fc, name=name, extra=extra
        )

    def _core_shape(core_url: str) -> tuple[bool, BaseException | None]:
        """One bounded Core-RPC handshake against the canonical rewrite; a
        malformed rewrite (e.g. a path riding through) fails the CONSTRUCTION
        guard inside the probe and collapses to False — which since
        TCK-DESCOPE-M3B is the whole answer for an http(s) candidate."""
        return _probe_tip(
            lambda: BitcoindClient(
                base_url=core_url,
                timeout_s=timeout,
                max_retries=retries,
                rpc_cookie_path=settings.rpc_cookie_path,
                **_bitcoind_auth_kwargs(auth),
            )
        )

    if text.startswith(ELECTRUM_SCHEME):
        ok, exc = _probe_tip(
            lambda: ElectrumClient(
                base_url=text, timeout_s=timeout, max_retries=retries
            )
        )
        if not ok:
            _refuse("electrum", *_failure_parts(exc))
            return None
        return text
    if text.startswith((BITCOIND_SCHEME, BITCOIND_TLS_SCHEME)):
        ok, exc = _core_shape(text)
        if not ok:
            _refuse("bitcoind-core", *_failure_parts(exc))
            return None
        return text
    for scheme, core_scheme in (("http://", BITCOIND_SCHEME), ("https://", BITCOIND_TLS_SCHEME)):
        if not text.startswith(scheme):
            continue
        # The ambiguous rungs (http:// M3, https:// TCK-BACKEND-003): INPUT
        # aliases for Bitcoin Core RPC ONLY — the ONE Core-shape attempt
        # against the canonical rewrite feeds the single scheme-dispatch
        # seam. No Core win means REFUSAL (TCK-DESCOPE-M3B: there is no
        # Esplora-shape fallback anymore, so an Esplora/mempool.space-style
        # URL can never be persisted through this seam). A userinfo-
        # carrying candidate is refused without a probe (embedded
        # credentials are refused on the stored rung — logins ride the
        # dedicated keys).
        rest = text[len(scheme) :]
        if "@" in rest.partition("/")[0]:
            _refuse("bitcoind-core", NETWORK_ERROR, "ValueError")
            return None
        ok, core_exc = _core_shape(core_scheme + rest)
        if ok:
            return core_scheme + rest
        _refuse("bitcoind-core", *_failure_parts(core_exc))
        return None
    _refuse("scheme-rejected", NETWORK_ERROR, "ValueError")
    return None

def _backend_kind(settings: Settings, *, resolved: bool) -> str:
    """The CLOSED ``backend_kind`` enum NAME for the live backend
    (TCK-BACKEND-002 deliverable 10, RE-SCOPED by TCK-DESCOPE-M3B to the
    two wallet families; the full mapping table lives at
    :data:`BACKEND_KINDS`). Derived from the SAME single selection point the
    client construction uses (``Settings.chain_base_url``) — scheme only,
    NEVER a network probe, and VALUE-FREE: a name, not a URL.
    ``resolved=False`` (the first-run choice still unmade) answers
    ``none``: nothing is being consulted and the field must not claim a
    server the user never picked. A legacy http(s) URL on a resolved rung
    also answers ``none`` — since the de-scope it builds NO wallet client
    (the construction seam refuses it), so nothing of that shape is ever
    in service."""
    if not resolved:
        return BACKEND_KIND_NONE
    url = _effective_chain_url(settings)
    if url.startswith(ELECTRUM_SCHEME):
        return BACKEND_KIND_ELECTRUM
    if url.startswith((BITCOIND_SCHEME, BITCOIND_TLS_SCHEME)):
        # The M2 Core-RPC adapter (TCK-ONB-004) including the https TLS
        # sibling scheme (TCK-BACKEND-003); the http(s) auto-detect STORES
        # the rewrite, so a live Core backend always reads one of these.
        return BACKEND_KIND_BITCOIND
    return BACKEND_KIND_NONE


class ChainBackendFlow:
    """The engine-thread chain-backend hot-swap controller (TCK-BACKEND-002;
    ADR-0018 amendment: a stored ``chain_base_url`` write takes effect
    IN-SESSION — the requires-restart semantics this ADR carried are
    superseded for the stored rung).

    ONE owner of the swap lifecycle, mutated exclusively on the ENGINE
    (pump) thread between turns; the chain client's entire lifecycle is
    engine-thread-owned by construction. State:

        idle ─apply(url)→ [probe → build → store-write] →
            install-now → swapped + full resync → idle
                      ↘ (scan in flight) DEFERRED(url, client) ──┐
        idle ─install_saved(url)→ (probe+store already done by    │ the
                                           /setup conversation) ──┤ pump:
        idle ─resync()→ full rebuild scan ────────────────────────┘
        take_deferred() after the in-flight scan's _ScanDone → install → resync

    Fail-closed at every seam: a probe/build/store failure REFUSES the write
    (value-free) and the OLD client stays installed and serving — the engine
    is never left clientless. The install (worker rebind + handler rebuild +
    BOUNDED close of the old client) runs only while NO scan fetch is in
    flight: the worker reads its client once per job, so between-jobs is the
    only moment an in-flight fetch cannot be holding the old reference —
    hence the defer-instead-of-stand-down rule (pinned; the alternative —
    blocking the engine on a job drain — would stall the pump for the whole
    scan). Tags survive the resync by construction (``coin_labels`` is not
    in the scan write-set).
    """

    def __init__(
        self,
        wiring: _Wiring,
        probe: Callable[[str], str | None],
    ) -> None:
        self._w = wiring
        self._probe = probe
        #: A validated, stored-but-not-yet-installed swap: (url, client).
        self._deferred: tuple[str, ChainClient] | None = None

    # ------------------------------------------------------------- surfaces

    @property
    def client(self) -> ChainClient | None:
        """The CURRENTLY SERVING chain client (the pump rebinds its local
        after any swap via this property), or ``None`` while the wallet
        backend is still unresolved (TCK-DESCOPE-M3A: unresolved has no
        client — the hold, not a silent public stand-in)."""
        return self._w.client

    @property
    def shadowed(self) -> bool:
        """Whether the env/config-file rung shadows the stored one — the
        honest ``requires_restart`` answer (a stored write waits for the
        next launch, exactly the resolution precedence, unchanged)."""
        return bool(self._w.boot_backend.strip())

    @property
    def kind(self) -> str:
        """The live backend's CLOSED enum NAME (:data:`BACKEND_KINDS`) for
        the settings/``/state`` kind fields — computed from the same
        selection point the serving client was built from, value-free."""
        effective = self._w.settings.chain_base_url.strip()
        return _backend_kind(
            self._w.settings,
            resolved=_backend_resolved(effective or None, self._w.store),
        )

    @property
    def effective_base_url(self) -> str:
        """The chain base URL ACTUALLY in service (TCK-WEB-013): the SAME
        single selection point ``kind``/``_backend_mode`` read — the boot
        fold (env > config file > stored) updated in place by every hot-
        swap and by an explicit public consent; EMPTY when unresolved
        (TCK-DESCOPE-M3A: no silent public default) — rendered through
        :func:`_url_without_credentials` (the pane may show the user's own
        server; it may never show a login)."""
        return _url_without_credentials(_effective_chain_url(self._w.settings))

    def _worker_occupied(self) -> bool:
        """Whether a scan FETCH owns the worker right now (pending/running).
        ``awaiting_backend`` deliberately does NOT count: a HELD first-run
        scan has submitted nothing — the worker is idle, and installing over
        it (then releasing on the new client) is exactly the point."""
        scan = self._w.scan
        return scan is not None and scan.gate.state in ("pending", "running")

    def apply(self, url: str) -> tuple[str | None, dict[str, object]]:
        """The settings-write path: PROBE-and-classify before the save
        (deliverable 2 + TCK-ONB-004 M3 auto-detect — the probe returns the
        CANONICAL URL: an ``http://`` endpoint answering in Core RPC shape
        is stored as ``bitcoind://``, the detected kind therefore feeds
        ``backend_kind``/dispatch through the ONE unchanged scheme seam),
        build before the save (a construction failure refuses the write —
        never store a value this process could not serve) WITH the resolved
        stored credentials riding the same overlay the probe used, then the
        store's typed writer (the ONLY sanctioned writer of the key) and the
        install. Returns ``(value-free refusal line | None, reply fields
        swapped/resync)``. A failure at any pre-store step leaves the store
        AND the live client untouched (credential rows included — a plain
        chain_base_url write never touches them)."""
        text = url.strip()
        canonical = text
        if text:
            detected = self._probe(text)
            if detected is None:
                return BACKEND_PROBE_FAIL, {}
            canonical = detected
        auth = _backend_auth(self._w.store)
        if self.shadowed:
            # Env/config-file rung set: the write is STORED (probed) but the
            # live ladder already outranks it — no swap, next-launch honesty
            # (the response's requires_restart flag says so, unchanged).
            error = self._store_write(canonical)
            if error is not None:
                return error, {}
            return None, {"swapped": False, "resync": "skipped"}
        try:
            new_client = _build_chain_client(
                replace(self._w.settings, chain_base_url=canonical), auth
            )
        except ValueError:
            # Malformed despite the probe (a shape the probe tolerated that
            # ChainConfig refuses): refuse the WRITE value-free, old client
            # untouched — never store a value we cannot serve.
            return BACKEND_PROBE_FAIL, {}
        error = self._store_write(canonical)
        if error is not None:
            _close_quietly(new_client)
            return error, {}
        if self._worker_occupied():
            # A fetch owns the worker: DEFER the install (the validated
            # client idles — connected lazily, nothing is in flight on it).
            self._deferred = (canonical, new_client)
            return None, {"swapped": False, "resync": "deferred"}
        return None, {
            "swapped": True,
            "resync": self._install(canonical, new_client),
        }

    def install_saved(self, url: str) -> str:
        """The ``/setup`` hook (deliverable 1, CLI path): the conversation
        already ran its warned probe + doctor gate + typed store write —
        this only performs the SAME install ``apply`` would. Returns the
        closed ONBOARDING outcome (not the resync status): ``swapped`` (the
        live client moved to the saved URL and the resync was started),
        ``deferred`` (validated + stored, install queued behind the
        in-flight scan), ``skipped`` (an env/config-file rung shadows the
        stored one — the honest next-launch copy). An EMPTY url is the
        /setup REVERT-to-public (TCK-DESCOPE-M3A): with the public marker
        just recorded by the conversation there is no "clear the rung and
        ride a default" meaning anymore — a revert INSTALLS the named
        public Electrum server, same as a fresh consent would."""
        if not url.strip():
            if not _public_consent_recorded(self._w.store):
                return "skipped"  # cleared with no consent = unresolved
            outcome, _started = self._install_public()
            return outcome
        text = url.strip()
        if self.shadowed:
            return "skipped"
        try:
            # The /setup conversation stored the probe's CANONICAL URL (and
            # any credentials it collected) through the typed writers — the
            # install resolves the same credential overlay a settings-apply
            # would, so both entries build the identical client.
            new_client = _build_chain_client(
                replace(self._w.settings, chain_base_url=text),
                _backend_auth(self._w.store),
            )
        except ValueError:
            return "skipped"  # stored, honest next-launch line
        if self._worker_occupied():
            self._deferred = (text, new_client)
            return "deferred"
        self._install(text, new_client)
        return "swapped"

    def install_public(self) -> bool:
        """TCK-PRIVACY-001 as re-targeted by TCK-DESCOPE-M3A: the consent
        seam's INSTALL half — build the named public Electrum client
        (:data:`~localwallet.config.PUBLIC_ELECTRUM_URL`) through the SAME
        scheme-dispatch construction + hot-swap path every other backend
        choice rides, install it (rebinding worker/handlers, closing the
        old client BOUNDED), and release a held first-run scan ON IT.
        There is no public-default client to release onto anymore.

        Code-review fix 2 (TCK-DESCOPE-M3A): consent INSTALLS only into the
        HELD first-run scan (gate ``awaiting_backend`` — the unresolved
        state consent actually answers). A stale/duplicate consent POST
        while a RESOLVED backend already serves the wallet must NEVER move
        queries to the public Electrum server or fire a resync: the record
        write stands (it changes nothing while a rung wins the ladder), the
        install is a NO-OP. The sanctioned resolved→public switch is /setup
        (``install_saved("")``, marker-first) or an explicit ``apply()`` —
        both deliberately route past this guard.
        Fail-closed: a build failure (or a shadowing env/config-file rung)
        installs nothing and leaves the wiring untouched. Returns whether
        the held scan ACTUALLY started loading (F2 contract)."""
        scan = self._w.scan
        if scan is None or scan.gate.state != "awaiting_backend":
            return False
        outcome, started = self._install_public()
        return outcome == "swapped" and started

    def _install_public(self) -> tuple[str, bool]:
        """The public-Electrum install shared by :meth:`install_public`
        (chat/web consent) and the ``/setup`` revert (``install_saved("")``
        with the marker recorded). Returns ``(swapped|deferred|skipped,
        held-scan-started)`` — the started flag is honest only when the
        release/report says the load began (security review F2)."""
        if self.shadowed:
            # An operator rung outranks the stored consent; the next
            # launch resolves through the ladder as always.
            return "skipped", False
        try:
            new_client = _build_chain_client(
                replace(self._w.settings, chain_base_url=PUBLIC_ELECTRUM_URL),
                _backend_auth(self._w.store),
            )
        except ValueError:
            return "skipped", False  # never strand the live client
        if self._worker_occupied():
            self._deferred = (PUBLIC_ELECTRUM_URL, new_client)
            return "deferred", False
        status = self._install(PUBLIC_ELECTRUM_URL, new_client)
        return "swapped", status == "started"

    def resync(self) -> str:
        """The ``Resync now`` action (deliverable 5, user direction 6): the
        full SCAN-003 rebuild scan on the CURRENT client (tags survive).
        ``busy`` while a scan owns the worker or the first-run backend is
        still unchosen (the held scan will run the full load anyway)."""
        scan = self._w.scan
        if scan is None:
            return "unavailable"
        return "started" if scan.resync_now() else "busy"

    def take_deferred(self) -> bool:
        """Called by the pump after every handled scan event: install the
        deferred swap once no scan is in flight (the scan that was in
        flight has just been persisted — or failed and stood down). Returns
        whether a swap landed (the pump rebinds its ``client`` local). A
        swap deferred across a session END is dropped: the stored value
        simply applies at next launch (the pre-amendment behavior)."""
        if self._deferred is None:
            return False
        if self._worker_occupied():
            return False  # a newer job grabbed the worker again; wait it out
        url, new_client = self._deferred
        self._deferred = None
        self._install(url, new_client)
        return True

    # -------------------------------------------------------------- internals

    def _store_write(self, text: str) -> str | None:
        """The sanctioned typed write (``""`` clears, per the store's
        convention); the value-free StoreError line is the refusal."""
        try:
            self._w.store.set_chain_base_url(text)
        except (StoreError, sqlite3.Error) as exc:
            return str(exc)
        return None

    def _install(self, url: str, new_client: ChainClient) -> str:
        """Swap in the validated client, rebind every chain-riding surface,
        BOUNDED-close the old one, then fire the full resync (user
        directions 5/6: a new URL is always followed by a fresh sync).

        Precondition (checked by every caller): NO scan fetch is in flight
        (gate not pending/running) — the worker reads its client once per
        job, and the engine-thread blocking scans cannot interleave with
        this (single-threaded between turns)."""
        w = self._w
        # The single selection point moves as ONE value: everything that
        # resolves the backend (banner, watch mode, node_status, the next
        # plan/fetch) rides this same settings object.
        w.settings.chain_base_url = url
        old = w.client
        w.worker.set_client(new_client)
        w.client = new_client  # type: ignore[assignment]
        self._rebind_handlers(new_client)
        _close_quietly(old)
        scan = w.scan
        if scan is None:
            return "unavailable"
        if scan.gate.state == "awaiting_backend":
            # The first-run HELD scan: releasing now plans + starts it ON
            # THE NEW CLIENT — the server the user just chose, never the
            # public default they refused (the ONB-006 promise, now kept
            # in-session instead of at next launch).
            return "started" if scan.release_backend() else "busy"
        return "started" if scan.resync_now() else "busy"

    def _rebind_handlers(self, client: ChainClient) -> None:
        """Rebuild the client-riding dispatch-table entries over the
        new client IN PLACE — the table is the same dict object the pump,
        the AgentLoop and every pending consult share, so no consumer can
        hold the dead closure set after the swap returns. The other
        handlers read only the store/flow and are structurally
        unaffected.

        TCK-DESCOPE-M3A (plan §4): the fee/price wrappers do NOT ride the
        swap anymore — the estimator and the oracle live on the ONE
        standalone public-info fetcher (:attr:`_Wiring.public_info`),
        backend-independent by construction, so the swap re-attaches the
        SAME shared instances (one source, one cache, one TTL — and no
        rebind even needs to touch them beyond passing the shared oracle
        into the rebuilt handlers). ONE exception (TCK-FEE-004 code-review
        fix): the shared estimator's min-relay FLOOR source IS backend-
        owned — the swap re-points it at the new client (bids and cache
        keep riding the public source unchanged)."""
        w = self._w
        scan = w.scan
        # TCK-FIAT-001 security-review LOW (folded into TCK-UX-009): ONE
        # oracle shared by get_balance (fiat display) and create_tx (USD
        # resolution) — the single-cache invariant the initial wiring
        # establishes (build_dispatch_table), restored here. TCK-FIAT-002:
        # the shared oracle keeps the SAME live display-currency ladder.
        price_oracle = w.price_oracle
        if price_oracle is None:  # pragma: no cover — _wire always sets it
            price_oracle = PriceOracle(
                w.public_info
                if w.public_info is not None
                else PublicInfoClient(w.settings),
                currency=_display_currency_reader(w.settings, w.store, w.session),
            )
            w.price_oracle = price_oracle
        fee_estimator = w.fee_estimator
        if fee_estimator is None:  # pragma: no cover — _wire always sets it
            fee_estimator = FeeEstimator(
                w.public_info
                if w.public_info is not None
                else PublicInfoClient(w.settings),
                relay_floor_client=client,
            )
            w.fee_estimator = fee_estimator
        else:
            # TCK-FEE-004 (code-review fix): ONE shared estimator across
            # the swap (same bids, same cache, same TTL) — but the
            # min-relay FLOOR is the wallet backend's own: re-point it.
            fee_estimator.set_relay_floor_client(client)
        w.table[IntentName.GET_BALANCE] = _make_get_balance_handler(
            w.store,
            w.wallet.id,
            scan.scan_now if scan is not None else (lambda: None),
            scan.gate if scan is not None else None,
            price_oracle=price_oracle,
            # TCK-UX-011: the rebuilt handler keeps the SAME transport
            # posture — web/engine still defers + kicks, CLI still scans
            # inline — over the one flow/gate/worker object.
            kick_scan_fn=scan.kick_scan if scan is not None else None,
            defer_scans=w.defer_scans,
        )
        w.table[IntentName.CREATE_TX] = _make_create_tx_handler(
            w.store,
            w.wallet.id,
            w.parsed,
            w.flow,
            fee_estimator,
            price_oracle,
            scan.scan_now if scan is not None else (lambda: None),
            seconds_since_last_block_fn=lambda: _safe_time_since_last_block(client),
            scan_gate=scan.gate if scan is not None else None,
            settings=w.settings,
            session=w.session,
        )
        w.table[IntentName.BROADCAST_TX] = _make_broadcast_tx_handler(
            w.flow, client, w.store, w.wallet.id, session=w.session, output=w.output
        )
        w.table[IntentName.TX_STATUS] = _make_tx_status_handler(
            client,
            w.flow,
            scan.gate if scan is not None else None,
            store=w.store,
            wallet_id=w.wallet.id,
        )
        # TCK-TX-SELF-001: self_transfer rides the fee estimator (now the ONE
        # backend-independent shared instance); same scan_fn/gate threading
        # as create_tx.
        w.table[IntentName.SELF_TRANSFER] = _make_self_transfer_handler(
            w.store,
            w.wallet.id,
            w.parsed,
            w.flow,
            fee_estimator,
            scan.scan_now if scan is not None else (lambda: None),
            # TCK-CPFP-002: the SAME session object across the swap — the
            # cpfp conversation state survives a backend hot-swap.
            session=w.session,
            seconds_since_last_block_fn=lambda: _safe_time_since_last_block(client),
            scan_gate=scan.gate if scan is not None else None,
        )
        # TCK-RBF-004: bump_fee rides the same shared fee_estimator + the
        # lazy scan (for the funding candidate set) + the ONE session that
        # carries the lineage/bump state; rebuilt exactly like self_transfer.
        w.table[IntentName.BUMP_FEE] = _make_bump_fee_handler(
            w.store,
            w.wallet.id,
            w.parsed,
            w.flow,
            fee_estimator,
            scan.scan_now if scan is not None else (lambda: None),
            w.session,
            seconds_since_last_block_fn=lambda: _safe_time_since_last_block(client),
            scan_gate=scan.gate if scan is not None else None,
        )


def _close_quietly(client: ChainClient | None) -> None:
    """Bounded, best-effort close of a retired client: BOTH adapters'
    close() are synchronous and local (httpx pool discard / socket close),
    and a close failure can never be allowed to unwind an APPLIED settings
    write — the value is stored and serving regardless. ``None`` (the
    TCK-DESCOPE-M3A unresolved state — there was no client to retire) is a
    no-op."""
    if client is None:
        return
    try:
        client.close()
    except Exception:  # noqa: BLE001, S110 — containment: retirement never raises
        pass


def _safe_time_since_last_block(client: ChainClient) -> int | None:
    """The rebuilt ETA hint's fail-closed wrapper (chain.watch is already
    fail-quiet; this keeps a handler-internal surprise from escaping — the
    narration-only fact degrades to ``None``, never a crash mid-turn)."""
    try:
        return time_since_last_block(client)
    except Exception:  # noqa: BLE001 — narration-only; degrade to no hint
        return None


def _wire(
    *,
    parsed: ParsedKey,
    descriptor: WalletDescriptor,
    signer_selection: SignerSelection,
    settings: Settings,
    env_gap: int | None,
    rescan: bool,
    flow: TxFlow | None,
    generate: ModelRuntime | GenerateFn | RemoteOpenAIRuntime,
    node_detect_fn: Callable[[], LocalNodeReport] | None,
    output_fn: Callable[[str], None],
    cli_interactive: bool = False,
    web_mode: bool = False,
    backend_check_fn: Callable[[str], str | None] | None = None,
    replace: bool = False,
    output: _Output | None = None,
) -> _Wiring:
    """Build store → wallet profile → chain client → watch → startup-scan
    plan → dispatch table → agent loop (moved verbatim from the pre-web
    ``run``, plus the ADR-0022 non-blocking scan wiring).

    The CLI calls this on the MAIN thread (behavior byte-identical to the
    old inline block); the web UI calls it INSIDE the
    :func:`start_engine` bootstrap — the engine thread (ADR-0024 §3).
    Config-fatal failures raise :class:`_WiringError` with the exact line
    the pre-web REPL printed before its ``return 2``.

    The startup scan is NOT run here (TCK-SCAN-003, ADR-0022 decision 1):
    the store-snapshot plan (:func:`localwallet.wallet.scan.plan_scan`,
    engine-thread reads only, network-free) is prepared and armed on the
    :class:`ScanFlow`, which the pump starts once the REPL loop exists —
    the prompt goes live while the chain worker fetches, and the engine
    persists + narrates the result between turns.
    """
    try:
        store = Store(settings.store_path)
        try:
            wallet_row, created_here = _resolve_or_create_wallet(
                store, descriptor, replace=replace
            )
            store.set_active_wallet(wallet_row.id)
        except (StoreError, sqlite3.Error) as exc:
            store.close()
            raise _WiringError(f"Could not prepare the wallet store: {exc}") from exc
    except (StoreError, sqlite3.Error, OSError) as exc:
        raise _WiringError(f"Could not open the wallet store: {exc}") from exc

    # TCK-ONB-003: the STORED backend rung enters the resolution here — the
    # one sanctioned injection point (ADR-0023 decision 3; config.py stays
    # store-free, so the stored value rides in as a plain argument). The
    # effective selection (env > config file > stored) is written back onto
    # ``settings.chain_base_url``, the single selection point (ADR-0018):
    # the chain client, the privacy banner, the watch-mode line, and the
    # node_status narration all read that one field and can therefore never
    # disagree (decision 6). TCK-DESCOPE-M3A: with nothing on any rung the
    # backend is UNRESOLVED — no client is constructed and no wallet call
    # can happen (the old silent public-esplora default is removed; the
    # scan holds at ``awaiting_backend`` interactively/web, and a headless
    # launch REFUSES the scan). An explicit public consent record resolves
    # the wallet onto the NAMED public Electrum server (folded below, the
    # one place consent becomes a URL).
    # TCK-BACKEND-002: the PRE-fold value is the env/config-file rung, kept
    # on the wiring as ``boot_backend`` — the hot-swap's honest
    # shadowed/requires_restart answer follows the SAME precedence without
    # re-reading the world (a rung set at launch cannot be unset in-session).
    boot_backend = (settings.chain_base_url or "").strip()
    effective_backend = resolve_chain_base_url(
        settings.chain_base_url, store.get_chain_base_url()
    )
    if effective_backend is None and _public_consent_recorded(store):
        # Consent from an earlier warned conversation IS a choice — of this
        # named server (never a silent default; ADR-0003 amendment).
        effective_backend = PUBLIC_ELECTRUM_URL
    if effective_backend is not None:
        settings.chain_base_url = effective_backend
        try:
            client: ChainClient | None = _build_chain_client(
                settings, _backend_auth(store)
            )
        except ValueError as exc:
            # An operator rung (env/config file) naming a non-wallet
            # backend (an http(s) Esplora URL — no longer a wallet family,
            # or a malformed scheme): fail closed with the value-free
            # startup refusal, never a silent fallback (ADR-0023 decision
            # 4: a chosen backend that cannot be served is named, not
            # papered over). The value-free line names the accepted
            # families only.
            raise _WiringError(
                "Configuration error: LOCALWALLET_CHAIN_BASE_URL (or the "
                "config-file key) is not a supported wallet backend — name "
                "an Electrum (ssl://) or Bitcoin Core (bitcoind://) server"
            ) from exc
    else:
        # UNRESOLVED: the wallet has NO chain client. Every chain-riding
        # surface is structurally held (awaiting_backend gate / refused
        # headless scan); a consent or a save installs the real client
        # through the hot-swap seam.
        client = None
    # TCK-FIAT-002 (ADR-0011 amendment): the display-currency ladder is
    # VALIDATED AT STARTUP — an unknown code on any rung (env, config file,
    # or stored) is a fail-closed, VALUE-FREE startup refusal, the same
    # exit-2 config-error path as the watch interval. A corrupt currency
    # setting never silently changes what fiat figures mean. The resolved
    # value is not kept here — the oracle re-reads the whole ladder live
    # (see :func:`_display_currency_reader`).
    try:
        resolve_display_currency(settings.display_currency, _stored_display_currency(store))
    except ValueError as exc:
        raise _WiringError(f"Configuration error: {exc}") from exc
    # TCK-DESCOPE-M3A (plan §4): fees and prices ride the ONE standalone
    # PUBLIC-INFO fetcher (mempool.space, payloads carry no wallet
    # addresses) REGARDLESS of which wallet backend resolves — constructed
    # once here, never rebuilt on a hot-swap. The floor-follower and the
    # ADR-0011 price ladder behave exactly as they did on an Esplora
    # wallet backend; a public-source failure degrades per the existing
    # fail-closed shapes (recommended fallback / stale → sats-only).
    public_info = _public_info_client(settings)
    # TCK-FEE-004 (code-review fix): the estimator BIDS over the public fee
    # source but the min-relay FLOOR is what OUR NODE accepts — the wallet
    # client's optional capability rides in (bitcoind answers; electrum
    # honestly absents → the assumed 1 sat/vB). An UNRESOLVED boot posture
    # (client None) starts on the assumed floor and the hot-swap re-points
    # it via _rebind_handlers when the user picks a backend in-session.
    fee_estimator = FeeEstimator(public_info, relay_floor_client=client)
    # TCK-FIAT-003: the session is built BEFORE the oracle so the injected
    # ladder reader can consult the per-ask currency one-shot (a no-op for
    # every other consumer — the REPL and the handlers share THIS object).
    session = SendSession()
    price_oracle = PriceOracle(
        public_info,
        currency=_display_currency_reader(settings, store, session),
    )
    tx_flow = flow if flow is not None else TxFlow()

    # The dedicated chain worker (ADR-0022 decision 2): ALL scan/watch chain
    # I/O runs on this thread; it receives store snapshots (ScanPlans) and
    # returns immutable ScanRecords — it never holds the Store. The engine
    # thread (this wiring's owner) persists everything via ScanFlow.
    worker = ChainWorker(client)

    output_fn(f"Privacy notice: {privacy_indicator(settings)}")

    # TCK-BACKEND-001 (ADR-0018 amendment): unskippable honesty line, once
    # per launch, iff the operator deliberately turned transport
    # verification off (env > config file > fail-closed default — no stored
    # rung, so this reads EXACTLY the value the chain client resolved from
    # its own Settings.from_env() pass; the two can never disagree).
    if not settings.tls_verify:
        output_fn.warning(TLS_UNVERIFIED_WARNING)

    # Background watch (Phase 5, TCK-P5-001; ADR-0019, ADR-0022 decision 4).
    # Tick-driven in the CLI: the watcher holds no thread and shares no
    # sqlite object across threads; the REPL runs a due poll cycle between
    # turns, and the poll's chain fetch rides the SAME worker (the P5-001
    # "full scan per poll on the engine thread" cost note is retired — the
    # engine only persists what the worker fetched). TCK-UX-009: the
    # interval resolves on its ladder (env > stored setting > default 60)
    # HERE — the stored rung's reader, which is what makes the plain
    # "Change it in settings" claim true (the watcher is built at launch →
    # the settings entry carries requires_restart). TCK-UX-012(b): "on" is
    # the NORMAL state — an on launch prints NOTHING (UX-009's on-line is
    # retired); the off line keeps the settings pointer.
    scan = ScanFlow(store, wallet_row, worker, gap_limit=env_gap, output=output_fn)
    watch_interval, watch_interval_warning = _resolve_watch_interval(settings, store)
    if watch_interval_warning is not None:
        output_fn(watch_interval_warning)
    watcher: IncomingWatcher | None = None
    if watch_interval > 0:
        watcher = IncomingWatcher(
            _make_watch_probe(store, wallet_row.id, scan.scan_now),
            interval_s=watch_interval,
        )
    else:
        output_fn("Background watch: off. Change it in settings.")

    # Startup scan plan (non-blocking, ADR-0022 decision 1) — or the
    # opted-out / failed-to-plan fallback. Planning is store-reads-only and
    # network-free, so it stays on the engine thread; the fetch runs on the
    # worker once the pump starts the flow.
    #
    # TCK-ONB-006 (ADR-0022 amendment 1, the FIRST-RUN EXCEPTION): when no
    # backend choice exists on ANY rung (env > config file > stored >
    # explicit-public record), an interactive or web launch HOLDS the gate
    # at ``awaiting_backend`` until the backend branch resolves —
    # user-confirmed 2026-09-09: wallet addresses must never reach a server
    # before an explicit choice. The hold is INDEPENDENT of AUTO_SCAN
    # (security review F1, the blocker): turning off the AUTOMATIC scan is
    # not consent to an unchosen server — the held gate stands the lazy
    # in-handler scan and the watch drain down too, so an AUTO_SCAN=0
    # launch stays leak-free while unresolved and the mandatory ask re-arms
    # on EVERY unresolved interactive launch (a consent-released load is
    # user-initiated, not an auto scan). Every run with a resolved choice
    # scans immediately (or stays lazy-opted-out), unchanged.
    # TCK-DESCOPE-M3A AMENDS THE HEADLESS CARVE-OUT (ADR-0023 amendment 3):
    # a non-interactive scripted launch cannot answer the ask, and the
    # silent public default it used to fall through to is GONE — scanning
    # an unchosen server is the leak the whole consent discipline exists to
    # prevent, so an unresolved headless launch REFUSES the startup scan
    # with the value-free :data:`HEADLESS_BACKEND_REFUSAL` line and holds
    # the gate at ``awaiting_backend`` forever (no wallet call can happen;
    # the handlers refuse with :data:`NO_BACKEND_REFUSAL`). "The command
    # line is the operator's decision" now means AN EXPLICIT server on the
    # command line (env/config-file rung), not the absence of one.
    auto_scan = os.environ.get(AUTO_SCAN_ENV_VAR, "").strip() != "0"
    backend_choice_resolved = _backend_resolved(effective_backend, store)
    defer_startup = False
    if not backend_choice_resolved:
        scan.set_startup_deferred(rescan=rescan)
        defer_startup = True
        if not (cli_interactive or web_mode):
            output_fn.warning(HEADLESS_BACKEND_REFUSAL)
    elif rescan or auto_scan:
        output_fn(SCAN_PROGRESS_NOTICE)
        try:
            scan.set_startup(
                wallet_scan.plan_scan(
                    store, wallet_row, gap_limit=env_gap, rebuild=rescan
                ),
                rescan=rescan,
            )
        except (
            ChainError,
            wallet_scan.ScanError,
            WatchKeyError,
            StoreError,
            sqlite3.Error,
        ) as exc:
            label = "rescan" if rescan else "startup scan"
            output_fn.warning(
                f"warning: {label} failed: {exc} — continuing with cached state."
                + _scan_failure_suffix(exc)
            )

    if not scan.gate.enabled:
        # No startup scan will run (opted out, or planning failed): the
        # persisted out-of-window warning is current — show it now, and the
        # handlers lazy-scan via the worker as they have always done.
        out_of_window = _out_of_window_line(store, wallet_row.id)
        if out_of_window is not None:
            output_fn(out_of_window)

    # TCK-SCAN-003 (supersedes the TCK-UX-001 hint ordering): the "type a
    # message" hint prints IMMEDIATELY — the startup scan runs on the chain
    # worker concurrently, the prompt is live while the dots still flow
    # between turns, and the completion narration lands when the engine
    # persists the result (the scan can take minutes; the REPL may not).
    # TCK-LAUNCH-001 (ADR-0024 amendment): the hint names REPL affordances
    # (typing 'exit', Ctrl-D) that DO NOT EXIST in the browser — the web
    # launch banner stays minimal (privacy notice + watch line + URL); the
    # page's own chrome is the user's prompt.
    if not web_mode:
        output_fn("Type a message — 'exit' or Ctrl-D quits.")

    # TCK-BACKEND-002 + TCK-ONB-004 M3: the ONE scheme-aware probe-AND-
    # classifier every entry point shares (settings write-before-save, the
    # onboarding step-5 validation): it returns the CANONICAL backend URL to
    # store (M3 auto-detect: an http:// Core-RPC endpoint is rewritten to
    # bitcoind://) or None to refuse. The test seam ``backend_check_fn``
    # overrides it for BOTH (the onboarding probe and the swap's before-save
    # probe), so a scripted test drives the whole hot-swap path with one
    # injected callable. Default is the bounded chain/ probe (G5: the only
    # networked module); credentials are resolved FROM THE STORE at call
    # time, so a credential saved moments before an Apply rides the same
    # probe and the same client build.
    probe_fn: Callable[[str], str | None] = (
        backend_check_fn
        if backend_check_fn is not None
        else (
            lambda url: _probe_chain_backend(url, settings, _backend_auth(store), output_fn)
        )
    )
    # Late-bound: the onboarding flow's swap hook and the wiring's controller
    # resolve to the same object once _wire's tail builds it.
    swap: ChainBackendFlow | None = None

    # TCK-ONB-003 (ADR-0023) + TCK-ONB-005 + TCK-ONB-006: the backend
    # conversation — CLI transport ONLY, built for EVERY interactive CLI
    # launch. It arms at startup whenever the backend is UNRESOLVED and the
    # ask is load-bearing for this launch: the wallet profile was created
    # THIS run (the first-run ask) or the startup scan is being held for it
    # (an unresolved returning wallet that never answered — the mandatory
    # pre-scan ask re-arms until it resolves; ADR-0023 amendment 2). A
    # preset ladder rung (env/config file) is an operator decision the
    # conversation must not overwrite or re-ask, and a resolved returning
    # user never sees the startup ask. Otherwise it stays DORMANT
    # (handle_line consumes nothing, ordinary chat reaches the model
    # untouched) until the /setup transcript command arms the same branch
    # via begin_setup. The web transport never receives the flow at all
    # (requirement 5: no onboarding surface in the browser; /setup there
    # prints the one-line pointer). Construction and narration are pure
    # store/console I/O — the model is never involved.
    onboarding: OnboardingFlow | None = None
    if web_mode:
        if not backend_choice_resolved:
            output_fn(WEB_SETUP_HINT)
    elif cli_interactive:
        ask_at_startup = (
            not backend_choice_resolved and (created_here or defer_startup)
        )
        onboarding = OnboardingFlow(
            store=store,
            check_backend=probe_fn,
            node_report=(
                None
                if not settings.node_detection_enabled
                else (node_detect_fn or (lambda: detect_local_nodes(settings)))
            ),
            loopback_host=_loopback_host_of,
            armed=ask_at_startup,
            deferred=defer_startup,
            # Explicit public consent (recorded by the flow itself) now
            # INSTALLS the named public Electrum server through the
            # hot-swap seam and releases the held scan ON IT
            # (TCK-DESCOPE-M3A — there is no public-default client to
            # release onto). No-op unless the gate is actually awaiting, and
            # it REPORTS whether the load started (the flow gates its
            # "loading now" line on that answer, F2). Late-bound like
            # ``backend_saved``: only the pump runs it, after the tail
            # builds the controller.
            public_chosen=(
                lambda: swap.install_public() if swap is not None else False
            ),
            # TCK-BACKEND-002: an OWN-server save now hot-swaps the live
            # client and resyncs in-session (the ADR-0018 amendment) — the
            # conversation reports swapped/deferred/skipped and adjusts its
            # honesty line accordingly. ``None`` until the tail builds the
            # controller (never called before then: only the pump runs it).
            backend_saved=(
                lambda url: swap.install_saved(url) if swap is not None else "skipped"
            ),
        )
        if ask_at_startup:
            for line in onboarding.opening_lines(
                load_started=scan.gate.enabled, deferred=defer_startup
            ):
                output_fn(line)
            # Step 4 fires when the FIRST startup scan persists successfully
            # (one-shot; never on scan failure — the load did not complete)
            # — including a DEFERRED scan released by a public consent.
            scan.on_first_scan_done = onboarding.emit_load_complete

    # TCK-FIAT-003: `session` was built with the price oracle above (the
    # reader holds THIS object) — the REPL and the handlers below share it.
    # TCK-HW-005 slice A: the chat probe/unlock signer, built ONCE per
    # wiring from THIS wallet's account key (fingerprint + descriptor
    # account path — mirroring the lazy sign build in the sign_tx handler).
    # CONFIG-INDEPENDENT by design: a file-signer launch can still be
    # asked "can you see my Jade?". Construction never imports hwilib
    # (lazy, inside the signer); the probe/report path cannot sign.
    hwi_probe = HwiUsbSigner(
        signer_selection.fingerprint_hex, _descriptor_account_path(parsed)
    )
    # The confirmation-ETA mempool hint (TCK-P5-002): consulted per create_tx
    # and per CREATED turn (lazily, fail-closed to no congestion adjustment);
    # narration-only — never a gate input. TCK-DESCOPE-M3A: an UNRESOLVED
    # backend has no client to ask — the hint degrades to None (the wallet
    # is gated off long before this narration could run anyway).
    seconds_since_last_block_fn = (
        lambda: time_since_last_block(client) if client is not None else None
    )
    table = build_dispatch_table(
        store,
        wallet_row,
        parsed,
        client,
        scan.scan_now,
        flow=tx_flow,
        session=session,
        fee_estimator=fee_estimator,
        price_oracle=price_oracle,
        signer_selection=signer_selection,
        settings=settings,
        node_detect_fn=node_detect_fn,
        seconds_since_last_block_fn=seconds_since_last_block_fn,
        scan_gate=scan.gate,
        # TCK-UX-011 (ADR-0022 amendment 2): keyed on TRANSPORT — the
        # web/engine thread must never inline-scan; the kick seam is the
        # real flow. CLI (including the AUTO_SCAN=0 dev opt-out) keeps the
        # inline lazy scan (defer_scans=False, the documented exception).
        kick_scan_fn=scan.kick_scan,
        defer_scans=web_mode,
        output=output,
    )
    loop = AgentLoop(generate, table)
    wiring = _Wiring(
        store=store,
        client=client,
        loop=loop,
        flow=tx_flow,
        session=session,
        table=table,
        watcher=watcher,
        worker=worker,
        scan=scan,
        onboarding=onboarding,
        settings=settings,
        parsed=parsed,
        wallet=wallet_row,
        boot_backend=boot_backend,
        defer_scans=web_mode,
        public_info=public_info,
        fee_estimator=fee_estimator,
        price_oracle=price_oracle,
        hwi=hwi_probe,
        output=output,
    )
    # Build last so the controller sees the finished wiring it mutates (the
    # late-bound ``swap`` name above now points here for the onboarding hook).
    swap = ChainBackendFlow(wiring, probe_fn)
    wiring.swap = swap
    return wiring


def _run_web(
    *,
    descriptor: WalletDescriptor | None,
    signer_selection: SignerSelection,
    settings: Settings,
    env_gap: int | None,
    rescan: bool,
    flow: TxFlow | None,
    generate: ModelRuntime | GenerateFn | RemoteOpenAIRuntime,
    node_detect_fn: Callable[[], LocalNodeReport] | None,
    output_fn: Callable[[str], None],
    output: _Output,
    on_web_server: Callable[[WebServer], None] | None = None,
    open_browser: bool = False,
    model: ModelDownloadFlow | None = None,
    preload: ModelPreloadFlow | None = None,
) -> int:
    """The web launch (TCK-WEB-002, ADR-0024 §1/§3/§11; default UI and
    browser auto-open per the TCK-LAUNCH-001 amendment).

    The full startup wiring flows through :func:`start_engine` (closing
    TCK-WEB-001's deferred deviation): ``bootstrap`` runs ``_wire`` ON the
    engine thread — the Store's ``check_same_thread`` pins construction
    there — and the server owns the one engine instance behind the loopback
    HTTP/SSE front. Prints the launch URL and the per-launch token on
    SEPARATE lines (the token is copyable but NEVER embedded in a URL —
    ADR-0024 §6); then best-effort opens the browser at the canonical URL
    (``open_browser``: the entry-point launch; the printed lines stand on
    their own when no browser exists) and parks until Ctrl-C.

    ``descriptor is None`` is the TCK-LAUNCH-001 FIRST-RUN launch (no key
    on flag/env/stored): the engine starts UNPROVISIONED (placeholder loop/
    table the pump never lets a user line reach), the page shows the
    watch-key form, and a successful ``POST /watchkey`` runs the existing
    parse+gate path + :func:`_wire` ON the engine thread
    (:class:`WatchKeyProvision`), after which the pump rebinds and the
    normal post-xpub sequence (banner to this same terminal ``output_fn``,
    ONB-006 ``awaiting_backend`` deferral, startup scan) is exactly the
    keyed launch's. A KEYED launch carries the same provision object with
    its wiring preset (TCK-LAUNCH-002): bare re-submits still 409, the
    explicit replace+confirm opt-in rewires in place (ADR-0024 amendment).

    ``model`` (TCK-LAUNCH-002) is the armed download flow (``None`` when a
    real model, the remote bridge, or the explicit --stub-llm path was
    selected); it rides the engine context so the pump emits the Yes/No
    card and owns the download lifecycle on the ENGINE thread.
    ``preload`` (TCK-LAUNCH-003) is the mutually-exclusive sibling: the
    background preload + launch checksum of the resolved REAL local model,
    armed only AFTER the URL/token launch lines above have printed (the
    wheel's build-time stdout silencer is process-wide — see
    :class:`ModelPreloadFlow`).

    Exit codes mirror the CLI: ``0`` normal, ``2`` wiring/config failure
    (surfaced from the bootstrap; a busy fixed port names the fix).
    """
    # Late import: ui.web.server imports this module (no cycle at runtime).
    from localwallet.ui.web.server import serve_web

    port = settings.web_port
    if not WEB_PORT_MIN <= port <= WEB_PORT_MAX:
        # Fail closed BEFORE binding (same spirit as the gap-limit
        # preflight): value-free, names the knob, never echoes the number.
        output.error(
            f"Configuration error: {WEB_PORT_ENV_VAR} must be a port "
            f"between {WEB_PORT_MIN} and {WEB_PORT_MAX} ({WEB_PORT_MIN} "
            "= an automatic free port)."
        )
        return 2

    booted = threading.Event()
    startup: dict[str, BaseException | None] = {"exc": None}
    wired: dict[str, _Wiring] = {}
    # The first-run holder (the provisioned wiring never lands in `wired`;
    # teardown reads it from here).
    held: dict[str, WatchKeyProvision] = {}

    def bootstrap() -> EngineContext:
        if descriptor is None:
            # First-run web launch (see the docstring): placeholder
            # engine — nothing here can touch a wallet that does not
            # exist yet (the pump gates every user line).
            provision = WatchKeyProvision(
                settings=settings,
                signer_selection=signer_selection,
                env_gap=env_gap,
                rescan=rescan,
                flow=flow,
                generate=generate,
                node_detect_fn=node_detect_fn,
                output_fn=output,
                output=output,
            )
            held["provision"] = provision
            booted.set()
            return EngineContext(
                loop=AgentLoop(stub_generate, {}),
                flow=flow if flow is not None else TxFlow(),
                session=SendSession(),
                table={},
                provision=provision,
                model=model,
                preload=preload,
                output=output,
            )
        try:
            wiring = _wire(
                parsed=descriptor.parsed,
                descriptor=descriptor,
                signer_selection=signer_selection,
                settings=settings,
                env_gap=env_gap,
                rescan=rescan,
                flow=flow,
                generate=generate,
                node_detect_fn=node_detect_fn,
                output_fn=output,
                web_mode=True,
                output=output,
            )
        except BaseException as exc:
            startup["exc"] = exc
            booted.set()
            raise
        wired["wiring"] = wiring
        # TCK-LAUNCH-002 (ADR-0024 amendment): a keyed launch carries the
        # SAME provisioning surface with its wiring preset — a bare
        # POST /watchkey still 409s (the already-contracted answer), while
        # the explicit replace+confirm opt-in reruns the gated path and the
        # pump rebinds in place.
        provision = WatchKeyProvision(
            settings=settings,
            signer_selection=signer_selection,
            env_gap=env_gap,
            rescan=rescan,
            flow=flow,
            generate=generate,
            node_detect_fn=node_detect_fn,
            output_fn=output,
            wiring=wiring,
            output=output,
        )
        held["provision"] = provision
        booted.set()
        return EngineContext(
            loop=wiring.loop,
            flow=wiring.flow,
            session=wiring.session,
            table=wiring.table,
            watcher=wiring.watcher,
            client=wiring.client,
            scan=wiring.scan,
            store=wiring.store,
            provision=provision,
            model=model,
            preload=preload,
            output=output,
            backend=wiring.swap,
            settings=wiring.settings,
            hwi=wiring.hwi,
        )

    try:
        server = serve_web(bootstrap, port=port)
    except OSError:
        # A bind failure (address/port unavailable) is the only startup
        # failure serve_web can raise. Exit 2 with the clean, VALUE-FREE
        # message — never echo the socket error (it carries the address),
        # exactly how every other config-fatal path reports. With a FIXED
        # port the fix is nameable (TCK-LAUNCH-001). The engine thread
        # bootstrap may have spawned is a daemon: it dies with this
        # exiting process, so there is nothing to tear down here.
        if port:
            output.error(
                "Could not start the web server — that port is already "
                f"in use. Set {WEB_PORT_ENV_VAR} to a free port (or "
                "leave it unset for an automatic one) and start again."
            )
        else:
            output.error("Could not start the web server.")
        return 2
    try:
        # The engine bootstraps (banner + watch line + prompt state) before
        # the URL prints; the startup scan now runs NON-BLOCKING on the
        # chain worker inside the pump (TCK-SCAN-003, ADR-0022) — turns
        # queued meanwhile are served in order, and the web UI's
        # freshness/progress surfacing of the same flow is TCK-WEB-005.
        # On a first-run launch the bootstrap is a placeholder: it settles
        # immediately and the real wiring happens at provisioning.
        booted.wait()
        exc = startup["exc"]
        if exc is not None:
            output.error(
                str(exc)
                if isinstance(exc, _WiringError)
                else "Could not start the engine."
            )
            return 2
        output_fn(f"Web UI: {server.url}")
        output_fn(f"Token: {server.token}")
        if open_browser:
            # TCK-LAUNCH-001: best-effort auto-open at the canonical
            # (token-free) URL. Headless/SSH/browser-less launches are the
            # NORMAL case: ONE calm manual-open line replaces the browser,
            # never a traceback, never a fatal (the token island reaches the
            # page from the URL alone).
            output_fn("Opening your browser…")
            if not _open_browser(server.url):
                output_fn(
                    f"Could not open a browser — open {server.url} manually."
                )
        if preload is not None:
            # TCK-LAUNCH-003: arm the background preload + launch checksum
            # ONLY now — every launch-critical line above (URL, TOKEN) has
            # printed. The pinned wheel's model-build noise silencer dup2's
            # /dev/null onto the process's stdout/stderr for the build's
            # duration; a swallowed token would lock the user out, so the
            # web transport arms AFTER its prints (the CLI arms at pump
            # entry, where nothing unrecoverable is pending).
            server.handle.commands.put(PRELOAD_START)
        if on_web_server is not None:
            on_web_server(server)
        server.wait()
    except KeyboardInterrupt:
        pass  # clean exit on Ctrl-C (mirrors the CLI)
    finally:
        server.stop()
        # The launch port and token are EPHEMERAL per launch (ADR-0024 §6):
        # this instance is dead, so any tab left over from a previous launch
        # can never reconnect. Say so (value-free) so the user knows to use
        # the freshly printed URL instead of staring at a stale "Reconnecting".
        output_fn("Web UI stopped — the URL printed above is no longer reachable.")
        wiring = wired.get("wiring")
        provision = held.get("provision")
        if provision is not None and provision.wiring is not None:
            # Track the CURRENT wiring: a first-run provisioning lands
            # here, and a TCK-LAUNCH-002 in-place REPLACE swapped
            # provision.wiring (the old pieces were already torn down on
            # the engine thread inside provision()).
            wiring = provision.wiring
        if wiring is not None:
            # server.stop() pushed QUIT and joined the engine thread (whose
            # pump drained the startup scan to completion before returning);
            # stopping the chain worker joins its idle loop. The httpx-backed
            # client has no thread affinity, closing after the worker is
            # fine. The Store is deliberately NOT closed from this thread
            # (check_same_thread pins it to the engine): the store is
            # autocommit (isolation_level=None), everything written was
            # already durable, and the connection dies with the process.
            wiring.worker.stop()
            if wiring.client is not None:
                wiring.client.close()
        close = getattr(generate, "close", None)
        if callable(close):
            close()
        output.close()
    return 0


def _resolve_or_create_wallet(
    store: Store,
    descriptor: WalletDescriptor,
    *,
    replace: bool = False,
) -> tuple[WalletRecord, bool]:
    """Reuse the wallet row carrying this descriptor, else create one.

    ``replace`` (TCK-LAUNCH-002 in-place wallet swap) changes the CREATE
    path only: the previous wallet's row keeps its name and every cached
    row it owns (it simply stops being the ACTIVE wallet), and the new
    profile takes the next free deterministic name — the ADR-0010
    name-collision guard for ordinary launches is untouched.

    Returns ``(row, created_this_run)`` — the fresh-wallet flag gates the
    ADR-0023 first-run conversation (returning users resume silently; the
    skipped node ask is re-offered by the chat-time triggers, not by every
    startup).

    Duplicate-descriptor guard (ADR-0010 single-wallet profile): the
    store is searched for a row whose descriptor matches the supplied
    key's canonical descriptor; a match is reused as-is (no duplicate
    row, derivation state and cache intact). Only when no row matches
    is a ``name="default"`` profile created. A name collision with a
    *different* descriptor surfaces as a value-free store error to the
    caller (exit 2) rather than a silent second profile.
    """
    rows = store.list_wallets()
    for row in rows:
        if row.descriptor == descriptor.descriptor:
            return row, False
    if not replace:
        # Normal launch: the name collision with a DIFFERENT descriptor is
        # the ADR-0010 guard (a conflicting --zpub exits 2 — never a silent
        # second profile).
        return store.create_wallet(name="default", descriptor=descriptor.descriptor), True
    # IN-PLACE REPLACE (TCK-LAUNCH-002 / ADR-0024 amendment): the previous
    # wallet's row KEEPS its name and its cached rows (they stop being the
    # active wallet's); the new profile gets the next free deterministic
    # name ("default-2", "default-3", … — bounded by the row count).
    names = {row.name for row in rows}
    name = "default"
    suffix = 1
    while name in names:
        suffix += 1
        name = f"default-{suffix}"
    return store.create_wallet(name=name, descriptor=descriptor.descriptor), True


def _scan_summary_line(summary: wallet_scan.ScanSummary) -> str:
    """The startup-scan completion line (counts + tip height only — never
    addresses or amounts; byte-identical to the TCK-UX-001-era narration,
    now emitted by :class:`ScanFlow` when the engine lands the scan)."""
    return (
        f"Startup scan complete: {summary.utxo_count} UTXOs · "
        f"tip height {summary.tip_height}."
        f"{_truncation_notice(summary)}"
    )


def _truncation_notice(summary: wallet_scan.ScanSummary) -> str:
    """One-line truncation notice, or ``""`` when the scan was not truncated.

    Consumes ``summary.truncated`` (TCK-SEC-002) so an attacker-driven
    scan/rescan that hit the absolute window ceiling is NEVER silent to the
    user. Value-free and calm; returns ``""`` when ``truncated`` is False so
    the non-truncated narration stays byte-identical to the pre-change text.
    """
    if not summary.truncated:
        return ""
    return " " + TRUNCATION_NOTICE


def _rescan_summary_line(summary: wallet_scan.ScanSummary) -> str:
    """Counts-only ``--rescan`` summary (per-branch indexes + UTXO count)."""
    branches = " · ".join(
        f"branch {b.branch}: scanned {b.scanned}, "
        f"max used {b.max_used_index}, next index {b.next_index}"
        for _, b in sorted(summary.branches.items())
    )
    return (
        f"Rescan complete: {branches} · {summary.utxo_count} UTXOs · "
        f"tip height {summary.tip_height}"
        f"{_truncation_notice(summary)}"
    )


def _out_of_window_line(store: Store, wallet_id: int) -> str | None:
    """The ADR-0009 startup warning when the store carries the flag.

    Reads ``sync_state["out_of_window_detected"]`` (written by every
    completed scan; empty payload = no warning). Malformed payloads are
    ignored silently — the warning is advisory and must never break
    startup.
    """
    try:
        raw = store.get_sync_state(wallet_id, wallet_scan.OUT_OF_WINDOW_KEY)
    except (StoreError, sqlite3.Error):
        return None
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    branches = payload.get("branches") if isinstance(payload, dict) else None
    if isinstance(branches, dict) and branches:
        return OUT_OF_WINDOW_NOTICE
    return None


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse CLI arguments (see :func:`run` for the flags)."""
    parser = argparse.ArgumentParser(
        prog="local-wallet",
        description=(
            "Watch-only mainnet Bitcoin wallet driven by a local LLM "
            "(Phase 1: wallet engine)."
        ),
    )
    parser.add_argument(
        "--zpub",
        help=(
            "account-level watch-only extended public key "
            "(xpub/ypub/zpub for mainnet); overrides LOCALWALLET_ZPUB"
        ),
    )
    parser.add_argument(
        "--rescan",
        action="store_true",
        help=(
            "run a full cache-repair rescan at startup (re-derive every "
            "window address from the key, rebuild derivation state and "
            "the UTXO snapshot) instead of the normal cache-trusting scan"
        ),
    )
    parser.add_argument(
        "--stub-llm",
        action="store_true",
        help=(
            "dev/test mode: deterministic canned model instead of the local "
            "GGUF runtime (NOT the acceptance path)"
        ),
    )
    parser.add_argument(
        "--signer",
        choices=sorted(_SIGNER_KINDS),
        default=None,
        help=(
            "signing backend for the send flow: 'file' (airgap transfer "
            "folder, default) or 'hwi' (USB hardware wallet); overrides "
            "LOCALWALLET_SIGNER"
        ),
    )
    ui_group = parser.add_mutually_exclusive_group()
    ui_group.add_argument(
        "--web",
        action="store_true",
        help=(
            "serve the localhost web UI (ADR-0024) — the launch default "
            "since TCK-LAUNCH-001; overrides LOCALWALLET_UI=cli"
        ),
    )
    ui_group.add_argument(
        "--cli",
        action="store_true",
        help=(
            "run the terminal REPL instead of the default web UI; "
            "overrides LOCALWALLET_UI (equivalently: LOCALWALLET_UI=cli)"
        ),
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def _repl(
    loop: AgentLoop,
    output_fn: Callable[[str], None],
    input_fn: Callable[[str], str],
    *,
    flow: TxFlow,
    session: SendSession,
    watcher: IncomingWatcher | None = None,
    client: ChainClient | None = None,
    table: DispatchTable,
    emitter: EventEmitter | None = None,
    scan: ScanFlow | None = None,
    store: Store | None = None,
    onboarding: OnboardingFlow | None = None,
    model: ModelDownloadFlow | None = None,
    preload: ModelPreloadFlow | None = None,
    backend: ChainBackendFlow | None = None,
    hwi: HwiUsbSigner | None = None,
) -> None:
    """The CLI transport over the engine pump (TCK-WEB-001, ADR-0024 §3).

    Reads user lines until EOF/exit and prints each turn's outcome — with
    the SAME queue-driven :func:`_pump` the threaded engine runs, this main
    thread being the engine (CLI behavior unchanged, including the
    between-turns :func:`_drain_watch` tick and the exact prompt/output
    order; :func:`_stdin_feeder` paces the reads via the ready/stop
    handshake). ``emitter`` overrides the CLI event routing (harness/web
    seam); by default output flows through :func:`cli_emitter`.

    ``table`` is the allowlist dispatch table the loop runs on — forwarded
    to :func:`_run_turn` for the deterministic CONFIRMED-retry re-sign
    (TCK-HW-002), which calls the ``sign_tx`` handler directly (never the
    model). ``scan`` (TCK-SCAN-003, ADR-0022) is the non-blocking startup-scan
    flow the pump drives: the REPL prompt goes live immediately and the scan
    completes across turns via the command queue.

    The flow/session pair is owned by this loop's caller (:func:`run`);
    every turn runs through :func:`_run_turn` so the confirm gate sees
    the raw utterance before the model does. Transcript commands (OQ14,
    ADR-0020) are handled by the pump (deterministic UI, never model
    intents); ``store`` rides along for the ``/label`` coin-label command
    (TCK-UTXO-001 — engine-thread-only access, same pin as the WEB-005
    settings pair). There is no protocol change.
    """
    commands: queue.Queue[Any] = queue.Queue()
    ready = threading.Event()
    stop = threading.Event()
    if preload is not None:
        # TCK-LAUNCH-003: the CLI arms the preload at pump entry — every
        # launch-critical line (banner, privacy notice) already printed
        # through _wire; the first prompt/dots racing the wheel's
        # build-time stdout window are the documented self-healing loss.
        commands.put(PRELOAD_START)
    if emitter is None:
        emitter = cli_emitter(output_fn)
    threading.Thread(
        target=_stdin_feeder,
        args=(input_fn, commands, ready, stop),
        name="repl-stdin",
        daemon=True,
    ).start()
    try:
        _pump(
            loop,
            emitter.text,
            commands,
            flow=flow,
            session=session,
            table=table,
            watcher=watcher,
            client=client,
            emitter=emitter,
            ready=ready,
            scan=scan,
            store=store,
            onboarding=onboarding,
            model=model,
            preload=preload,
            backend=backend,
            hwi=hwi,
        )
    finally:
        stop.set()
        ready.set()


#: Fallback wording for an unparseable ``/`` command (value-free). The
#: TCK-LAUNCH-002 model-free quick actions (/balance, /receive, /address,
#: /settings) and the card commands (/download, /later) join the list —
#: all deterministic transcript/channel intercepts, never model intents.
_TRANSCRIPT_HELP: Final[str] = (
    "Commands: /details — reprint the pending transaction's full card; "
    "/label — list or set your own coin tags and notes; "
    "/setup — choose which server answers the app about your addresses "
    "(your own Electrum server or Bitcoin Core node, or the consented "
    "public Electrum server); "
    "/export <path> — write a redacted session transcript; "
    "/scrub — clear the in-memory transcript; "
    "/balance, /receive, /address, /settings — model-free reads (work "
    "without the local LLM); /download, /later — answer the model card; "
    "/help — show this."
)
#: ``/details`` with no cached card (nothing has pended this session —
#: value-free).
_DETAILS_NONE: Final[str] = "No pending transaction to show a full breakdown for."
#: ``/setup`` where NO flow is armed (TCK-ONB-005): the backend branch
#: needs the terminal command loop, so web-chat/headless submissions of the
#: command get the pointer instead (the choice itself is CLI- or
#: web-settings-owned; structurally CLI-only by construction).
_SETUP_CLI_ONLY: Final[str] = (
    "Backend setup runs in the terminal app: start local-wallet at a "
    "terminal and type /setup. (In the web UI, the Settings panel edits "
    "the same choice.)"
)

# ------------------------------------------------------------- /label (UTXO-001)
#
# Coin tags and notes are USER-AUTHORED facts about the user's own coins, on
# the deterministic transcript channel (ADR-0020, like /details): they never
# reach the model, the gate, or the dispatcher (design doc §1.1/§4.4). The
# copy below is code-owned and static except for the values echoed verbatim
# from the store's typed writer (txid, stored tags, stored note — terminal
# display, same class as /details printing addresses). Display always frames
# them as the user's claim ("your note" / "you marked") — we verify nothing (§9).

#: §1.4 closed tag set → the strings the user sees (display-only mapping;
#: ids are what the deterministic selection layer consumes, TCK-UTXO-002).
_LABEL_TAG_DISPLAY: Final[dict[str, str]] = {
    "kyc": "KYC",
    "exchange": "exchange",
    "p2p": "peer to peer",
    "purchase": "purchase",
    "consolidation": "consolidation",
}
#: §1.2 command shape (terminal UI; never a model-facing string).
_LABEL_USAGE: Final[str] = (
    'Usage: /label [last | <txid>] [tag words] ["your own words" after a |]'
    " — bare /label lists your coins."
)
#: §4.3 label.unknown_tag — cause + the closed set + the free-note way out.
_LABEL_UNKNOWN_TAG: Final[str] = (
    "I don't know that label. Known ones: "
    + ", ".join(COIN_TAGS)
    + ' — or type your own words after "|" for a note.'
)
#: §4.3 label.nothing_last (value-free).
_LABEL_NOTHING_LAST: Final[str] = (
    'Nothing to label yet — "last" is the most recent payment you\'ve sent.'
)
#: §4.3 label.error_store — cause + next step, value-free.
_LABEL_ERROR_STORE: Final[str] = (
    'I couldn\'t save that note — the database is busy; say "retry".'
)
#: §4.3 label.cleared.
_LABEL_CLEARED: Final[str] = "Cleared your note for that transaction's coins."
#: Fail-closed target shapes (value-free; the command is terminal-only so a
#: malformed txid is named plainly, never quoted back).
_LABEL_BAD_TXID: Final[str] = (
    'That doesn\'t look like a transaction id — 64 hex characters, or use "last".'
)
_LABEL_NO_TARGET: Final[str] = (
    "Nothing to label for that transaction yet — its coins show up after a scan."
)
_LABEL_NO_WALLET: Final[str] = "No wallet is open yet — nothing to label."
_LABEL_NOTE_TOO_LONG: Final[str] = (
    f"That note is too long — {COIN_NOTE_MAX_CHARS} characters maximum. "
    'Shorten the text after the "|".'
)
_LABEL_STORE_UNAVAILABLE: Final[str] = "Labels are unavailable — no wallet store is open."
_LABEL_NO_COINS: Final[str] = "No coins to show yet — the wallet has no unspent outputs."
#: §4.3 label.list_head — the honesty frame (§9) on every listing.
_LABEL_LIST_HEAD: Final[str] = (
    "Your unspent coins — tags are what YOU marked (we never verify anything):"
)
#: §4.3 card.broadcast_hint — the ONE post-broadcast capture hint (§1.2):
#: printed when no gate is armed (flow is terminal BROADCAST), static
#: code-owned string, never repeated within the session for the same tx.
_LABEL_BROADCAST_HINT: Final[str] = (
    'Want to remember what this was? Type /label last [tag] ["note"]'
)


def _handle_transcript_command(
    command: str,
    loop: AgentLoop,
    output_fn: Callable[[str], None],
    *,
    flow: TxFlow | None = None,
    session: SendSession | None = None,
    store: Store | None = None,
    onboarding: OnboardingFlow | None = None,
) -> None:
    """Handle an OQ14 transcript CLI command (``/details``, ``/label``,
    ``/setup``, ``/export``, ``/scrub``, ``/help``).

    Deterministic UI features, NOT model intents (ADR-0020): no protocol,
    grammar, or prompt change. Output is short and plain. ``/details``
    (TCK-UX-002) reprints the cached FULL nine-line confirmation card
    verbatim, reachable ONLY while a transaction is pending (CREATED) —
    the flow's live-pending gate is the single check, so a spent/abandoned
    card can never be re-shown as if live. It is a deterministic UI
    command, never a spoken decision: the word "details" is deliberately
    NOT in the gate's whitelists, so it can never confirm anything.

    ``/label`` (TCK-UTXO-001) lists/sets/clears the user's OWN coin tags
    and notes on the same channel — never model context, never a gate
    answer, never an envelope; the store's typed accessors are the only
    writers and own the fail-closed validation.

    ``/setup`` (TCK-ONB-005, ADR-0023 step 5 against an EXISTING wallet)
    arms the onboarding flow's backend branch for the command loop: the
    lines that FOLLOW the command ride the same deterministic channel the
    first-run ask already uses (never the model). CLI-only by
    construction: the pump hands it the session's flow, and only
    interactive CLI wiring builds one — anywhere else it prints the
    honest pointer (:data:`_SETUP_CLI_ONLY`).
    """
    parts = command.split(maxsplit=1)
    cmd = parts[0].lower()
    if cmd == "/help":
        output_fn(_TRANSCRIPT_HELP)
        return
    if cmd == "/setup":
        if onboarding is None:
            output_fn(_SETUP_CLI_ONLY)
            return
        onboarding.begin_setup(output_fn)
        return
    if cmd == "/details":
        pending = flow is not None and flow.state is TxFlowStatus.CREATED
        if not pending or session is None or not session.card_render:
            output_fn(_DETAILS_NONE)
            return
        for line in session.card_render:
            output_fn(line)
        return
    if cmd == "/label":
        _handle_label_command(parts[1] if len(parts) > 1 else "", store, session, output_fn)
        return
    if cmd == "/scrub":
        loop.scrub()
        output_fn("Transcript cleared.")
        return
    if cmd == "/export":
        if len(parts) < 2 or not parts[1].strip():
            output_fn("Usage: /export <path>")
            return
        target = Path(parts[1].strip())
        if target.exists():
            output_fn("export: file already exists, choose another path")
            return
        try:
            lines = loop.export_transcript(target)
        except OSError:
            output_fn("Could not write the transcript export.")
            return
        output_fn(f"Transcript exported ({lines} lines, redacted).")
        return
    output_fn(_TRANSCRIPT_HELP)


def _active_wallet_id(store: Store | None, output_fn: Callable[[str], None]) -> int | None:
    """Resolve the active wallet id for a transcript command, or ``None``
    after printing the value-free reason (no store / no wallet open)."""
    if store is None:
        output_fn(_LABEL_STORE_UNAVAILABLE)
        return None
    try:
        wallet = store.get_active_wallet()
    except (StoreError, sqlite3.Error):
        output_fn(_LABEL_ERROR_STORE)
        return None
    if wallet is None:
        output_fn(_LABEL_NO_WALLET)
        return None
    return wallet.id


def _label_tag_line(tags: Sequence[str]) -> str:
    """Render a stored tag id list as the user-facing display strings (§1.4)."""
    return ", ".join(_LABEL_TAG_DISPLAY.get(tag, tag) for tag in tags)


def _label_echo(txid: str, tags: Sequence[str], note: str | None) -> str:
    """One §4.3 label.set line — txid verbatim, tags/note echoed AS STORED
    (canonical order from the typed writer). Terminal display only."""
    shown = f"Noted on transaction {txid}"
    if tags:
        shown += f" — your coin tags: {_label_tag_line(tags)}"
    if note:
        shown = f'{shown}{" ·" if tags else " —"} your note: "{note}"'
    return shown


def _list_coin_labels(store: Store, wallet_id: int, output_fn: Callable[[str], None]) -> None:
    """Bare ``/label``: list tags/notes for the wallet's UNSPENT coins (§1.3).

    Terminal-only, value-VERBATIM display is fine here (same class as the
    /details card — addresses/amounts go to the human, never the model).
    Unlabeled coins fall back to ``(unlabeled)`` — no row, matching the
    §5 provenance view's existing fallback shape.
    """
    try:
        utxos = store.get_utxos_for_wallet(wallet_id)
        labels = {(r.txid, r.vout): r for r in store.get_coin_labels(wallet_id)}
    except (StoreError, sqlite3.Error):
        output_fn(_LABEL_ERROR_STORE)
        return
    if not utxos:
        output_fn(_LABEL_NO_COINS)
        return
    output_fn(_LABEL_LIST_HEAD)
    for u in utxos:
        rec = labels.get((u.txid, u.vout))
        tags = rec.tags if rec is not None else ()
        note = rec.note if rec is not None else None
        tail = "(unlabeled)" if not tags and not note else _label_echo(u.txid, tags, note)
        output_fn(sanitize_tool_output(f"{u.txid}:{u.vout}  {u.value_sats} sats — {tail}"))


def _handle_label_command(
    rest: str,
    store: Store | None,
    session: SendSession | None,
    output_fn: Callable[[str], None],
) -> None:
    """Implement ``/label`` (design doc §1.2, TCK-UTXO-001) — deterministic,
    transcript-channel, NEVER model-facing.

    Grammar::

        /label                                → list tags/notes for unspent coins
        /label [last | <txid>] [tag words] [| free note]

    ``last`` resolves to the most recent tx this wallet BROADCAST this session
    (``session.last_broadcast_txid``); ``<txid>`` is a full 64-hex id. Both
    label the wallet-owned outputs the store knows about for that txid (its
    unspent coins from that tx under ``utxos`` PLUS any recorded
    ``coin_labels`` outpoints). A bare ``/label <target>`` with no tags and
    no note clears (§1.3). All validation is fail-closed: unknown tag → the
    §1.4 refusal listing the closed set; over-long note → a length-cap
    refusal; malformed txid → refused. The typed store accessor is the sole
    writer; the model is not consulted and label text never enters a
    prompt/envelope/FACTS (§1.1/§7.10).

    The store is the sole source of truth for which outpoints are ours: a
    target txid resolves to the wallet's unspent outputs from that tx PLUS any
    coin_labels rows for it (so a just-broadcast change coin is labelable
    before the next rescan, and a retained spent-coin label can be edited).
    A txid with no known coin says so value-free (§1.2).
    """
    tokens = rest.split("|", 1)
    head = tokens[0].strip()
    note = tokens[1].strip() if len(tokens) == 2 else None
    if note == "":
        note = None

    # Bare /label → list (no target, no writes).
    if head == "":
        if store is None:
            output_fn(_LABEL_STORE_UNAVAILABLE)
            return
        wallet_id = _active_wallet_id(store, output_fn)
        if wallet_id is not None:
            _list_coin_labels(store, wallet_id, output_fn)
        return

    words = head.split()
    target = words[0] if words else ""
    tag_words = words[1:] if words else []

    if target.lower() == "last":
        txid = session.last_broadcast_txid if session is not None else None
        if txid is None:
            output_fn(_LABEL_NOTHING_LAST)
            return
    else:
        txid = target
        if len(txid) != 64 or any(c not in "0123456789abcdef" for c in txid.lower()):
            # A tag word with no target ("forgot the last/<txid>") is the most
            # common slip → usage; anything else is a malformed id.
            output_fn(_LABEL_USAGE if txid.lower() in COIN_TAGS else _LABEL_BAD_TXID)
            return
        txid = txid.lower()

    if store is None:
        output_fn(_LABEL_STORE_UNAVAILABLE)
        return
    wallet_id = _active_wallet_id(store, output_fn)
    if wallet_id is None:
        return

    # Fail-closed tag validation against the closed set (case-insensitive in;
    # the canonical ids out). Unknown word → the §1.4 refusal, nothing stored.
    tag_ids: list[str] = []
    for word in tag_words:
        cid = word.lower()
        if cid not in COIN_TAGS:
            output_fn(_LABEL_UNKNOWN_TAG)
            return
        tag_ids.append(cid)

    if note is not None and len(note) > COIN_NOTE_MAX_CHARS:
        output_fn(_LABEL_NOTE_TOO_LONG)
        return

    # Which of THIS transaction's outputs are ours? Both wallet-owned unspent
    # rows (coins this tx CREATED for us — the utxos snapshot holds only our
    # own coins) and recorded label rows (the change coin of a just-broadcast
    # send, written by the lineage helper before any rescan). A txid with no
    # recorded coin is not labelable yet — §1.2 "says so".
    try:
        utxos = store.get_utxos_for_wallet(wallet_id)
        label_rows = store.get_coin_labels(wallet_id)
    except (StoreError, sqlite3.Error):
        output_fn(_LABEL_ERROR_STORE)
        return
    our_vouts = sorted(
        {u.vout for u in utxos if u.txid == txid}
        | {r.vout for r in label_rows if r.txid == txid}
    )
    if not our_vouts:
        output_fn(_LABEL_NO_TARGET)
        return

    cleared = not tag_ids and note is None
    try:
        for vout in our_vouts:
            # A bare re-label replaces; no tags + no note clears (§1.3).
            rec = store.set_coin_label(wallet_id, txid, vout, tag_ids, note)
        if cleared:
            output_fn(_LABEL_CLEARED)
        elif rec is not None:
            output_fn(sanitize_tool_output(_label_echo(txid, rec.tags, rec.note)))
    except StoreError:
        # The store's own fail-closed validation (should be pre-caught above);
        # a residual StoreError is surfaced value-free, nothing is asserted
        # saved. sqlite is guarded by Store's transaction handling.
        output_fn(_LABEL_ERROR_STORE)
        return


# ---------------------------------------------------------------------------
# TCK-HW-005 SLICE A: hardware-wallet probe/unlock chat intercept (deterministic,
# PRE-MODEL). USER LIVE FINDINGS 2026-09-12: "can you see my hardware wallet?"
# → the model's "what do you mean" is WRONG (probe + report); "unlock my
# hardware wallet" → "I cannot access your hardware wallet" is WRONG (the app
# CAN, via hwi); "I connected my hardware wallet" → report AND run the unlock
# for device classes that have a host-driven one. The matcher is code-owned
# and TIGHT (v1): a device TOPIC word (a class name, "hardware", or "device")
# plus a verb from exactly one family, whole words only (the
# _chat_public_choice word shape). Show-address / pre-sign check / static
# button are later slices — this one only probes, reports, and unlocks.
# ---------------------------------------------------------------------------

_HW_CLASS_WORDS: Final[frozenset[str]] = frozenset(
    {"jade", "bitbox", "bitbox02", "trezor", "ledger", "coldcard", "keepkey"}
)
_HW_TOPIC_WORDS: Final[frozenset[str]] = _HW_CLASS_WORDS | frozenset(
    {"hardware", "device", "devices"}
)
_HW_UNLOCK_WORDS: Final[frozenset[str]] = frozenset(
    {"unlock", "unlocks", "unlocking"}
)
_HW_CONNECT_WORDS: Final[frozenset[str]] = frozenset(
    {
        "connect", "connects", "connected", "connection", "plug", "plugged",
        "locked",
    }
)
_HW_SEE_WORDS: Final[frozenset[str]] = frozenset(
    {"see", "sees", "seen", "detect", "detects", "detected"}
)
#: TCK-HW-005 SLICE C (user live finding 2026-09-12): "let's sign with my
#: hardware wallet" must NEVER fall through to the LLM ("I cannot directly
#: interact…" is a WRONG answer — the app CAN, via hwi). A device topic
#: word (same set as above) plus a sign verb.
_HW_SIGN_WORDS: Final[frozenset[str]] = frozenset({"sign", "signs", "signing"})


def _hardware_chat_verb(line: str) -> str | None:
    """``'unlock'`` | ``'connect'`` | ``'see'`` | ``'sign'`` | ``None``.

    Priority order is UNLOCK > CONNECT > SEE > SIGN (slice-A families
    keep their exact precedence; the slice-C ``'sign'`` verb is the new
    bottom rung, so "can you see my hardware wallet" stays a probe/report
    and "unlock my jade and sign" starts with the unlock).

    ``'see'`` answers with probe+report ONLY; ``'unlock'`` and
    ``'connect'`` (the "I connected my hardware wallet" report) also drive
    the host-unlockable classes. ``'sign'`` ROUTES the pending send flow
    onto the device sign path (slice C — probe-gated inside the handler)
    and, with no flow pending, answers probe+report WITHOUT driving an
    unlock (nothing to sign; the unlock stays slice A's chat command).
    Bare "unlock" (no topic), "unlock my wallet" (no HARDWARE/device/class
    word), "sign the transaction" (no topic), "can you see my balance",
    and "connect to my node" match nothing here and stay ordinary chat.
    """
    words = {
        w.strip(punctuation).replace("'", "").replace("\u2019", "")
        for w in line.lower().split()
    }
    if words.isdisjoint(_HW_TOPIC_WORDS):
        return None
    if words & _HW_UNLOCK_WORDS:
        return "unlock"
    if words & _HW_CONNECT_WORDS:
        return "connect"
    if words & _HW_SEE_WORDS:
        return "see"
    if words & _HW_SIGN_WORDS:
        return "sign"
    return None


#: TCK-HW-005 SLICE C: the pinned ANSWER to the device-absent ask ("…or say
#: 'file' and I can export the transaction file for your SD card instead").
#: Whole-utterance phrases only (the onboarding matcher shape) — consumed
#: ONLY while a transaction sits CONFIRMED awaiting its handoff, so a stray
#: "file" anywhere else stays ordinary chat.
_FILE_EXPORT_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "file",
        "export",
        "the file",
        "export the file",
        "file export",
        "export it",
        "export please",
        "file please",
        "export instead",
        "file instead",
        "use the file",
        "sd card",
        "export the psbt",
    }
)


def _file_export_choice(line: str) -> bool:
    """The bare "file"/"export" fallback answer (slice C, pinned set)."""
    words = [
        w.strip(punctuation).replace("'", "").replace("\u2019", "")
        for w in line.lower().split()
    ]
    return " ".join(w for w in words if w) in _FILE_EXPORT_PHRASES


#: TCK-HW-005 slice C SECURITY FIX (review MEDIUM): the sign-verb match is
#: DELIBERATELY BROADER than :class:`ConfirmGate` ("sign with my hardware
#: wallet" classifies NOT_A_DECISION at the gate — "with"/"my"/"hardware"
#: are not FILLER tokens), so the routing must never stamp a CONFIRM the
#: gate itself would refuse. Guard: ANY deny token from the gate's own
#: DENY vocabulary ("no", "cancel", "stop", "abort", "don't"/"dont",
#: "deny", "reject", …) plus the standalone negation "not" (covering
#: "do not sign …") suppresses the deterministic sign routing ENTIRELY —
#: the utterance falls through to the ordinary gate/model pipeline, which
#: handles cancel/NOT_A_DECISION honestly and keeps the CREATED-only chat
#: cancel (the user's undo) intact.
_HW_DENY_TOKENS: Final = ConfirmGate.DENY_TOKENS | frozenset({"not"})


def _hardware_sign_denied(line: str) -> bool:
    """True when the utterance carries deny/negation vocabulary."""
    words = {
        w.strip(punctuation).replace("'", "").replace("\u2019", "")
        for w in line.lower().split()
    }
    return bool(words & _HW_DENY_TOKENS)


def _route_hardware_sign(
    loop: AgentLoop,
    flow: TxFlow,
    session: SendSession,
    line: str,
    output_fn: Callable[[str], None],
    *,
    table: DispatchTable,
) -> bool:
    """Route "sign with my hardware wallet" (slice C) through the REAL
    sign path — never the LLM, no skip, no reorder. Deny/negation
    utterances are excluded by the caller's :func:`_hardware_sign_denied`
    guard BEFORE this function runs.

    CONFIRMED: one code-built ``sign_tx`` envelope (dispatcher-owned ref,
    the retry-intercept precedent) with ``session.hw_sign_wanted`` latched;
    the handler's pre-sign device check runs the ONE enumerate and decides
    (device present → sign; absent → the file-offering ask, never a silent
    export; locked → the existing guidance family).

    CREATED — the CONFIRM stamp, stated honestly: this is a DELIBERATE,
    NARROW EXTENSION of the gate's "sign" ask word, NOT classifier-
    equivalent (ConfirmGate.classify on this phrasing is NOT_A_DECISION —
    the extra words are not filler). It is justified by: (1) the user's OWN
    literal utterance names the sign intent (device topic word ∧ sign verb,
    whole words, code-matched) — no LLM is consulted anywhere on this path,
    so an LLM "yes" can never produce the stamp; (2) same-turnness: the
    decision describes the very utterance being dispatched (the ADR-0013
    dual-key invariant); (3) the ref is dispatcher-owned (flow state, never
    user text); (4) bounded consequences — nothing signs without on-device
    approval, the revalidation hard stop is unchanged, and broadcast stays
    a separately gated turn; (5) the deny/negation guard above refuses to
    advance the flow against any cancel reading. The confirm handler runs
    FIRST; the sign only chains if the flow actually reached CONFIRMED.
    History records every dispatched turn like any model turn would.
    """
    if IntentName.SIGN_TX not in table:
        return False
    if flow.state is TxFlowStatus.CREATED:
        if flow.pending is None or IntentName.CONFIRM_TX not in table:
            return False
        # Code-stamped same-turn CONFIRM — the deliberate narrow extension
        # described in the docstring (user's own literal words, no LLM,
        # deny-guarded upstream; NOT a ConfirmGate.classify match).
        confirm_envelope = Envelope(
            v=0,
            intent=IntentName.CONFIRM_TX,
            params=ConfirmTxParams(tx_ref=flow.pending.tx_ref),
        )
        session.gate_decision = GateDecision.CONFIRM
        confirm_result = table[IntentName.CONFIRM_TX](confirm_envelope)
        loop.add_turn(line, confirm_envelope.model_dump_json())
        _print_turn(
            AgentTurnResult(
                status=AgentTurnStatus.OK,
                envelope=confirm_envelope,
                result=confirm_result,
                user_message=None,
                turns_used=0,
            ),
            output_fn,
            session=session,
        )
    confirmed = flow.confirmed if flow.state is TxFlowStatus.CONFIRMED else None
    if confirmed is None:
        # The confirm above refused (value-free refusal already narrated) —
        # the deterministic turn is complete; the ordinary pipeline must
        # not re-ask the model about the same utterance.
        return True
    session.hw_sign_wanted = True
    sign_envelope = Envelope(
        v=0,
        intent=IntentName.SIGN_TX,
        params=SignTxParams(tx_ref=confirmed.tx_ref),
    )
    result = table[IntentName.SIGN_TX](sign_envelope)
    loop.add_turn(line, sign_envelope.model_dump_json())
    _print_turn(
        AgentTurnResult(
            status=AgentTurnStatus.OK,
            envelope=sign_envelope,
            result=result,
            user_message=None,
            turns_used=0,
        ),
        output_fn,
        session=session,
    )
    return True


def _run_hardware_chat(
    loop: AgentLoop,
    flow: TxFlow,
    session: SendSession,
    line: str,
    output_fn: Callable[[str], None],
    *,
    table: DispatchTable,
    hwi: HwiUsbSigner | None,
) -> bool:
    """The deterministic hardware-utterance intercept (slices A + C).

    ``'sign'`` with a flow PENDING (CREATED/CONFIRMED) routes through the
    sign handler's device path (slice C — probe-gated, never the LLM)
    UNLESS the utterance carries deny/negation vocabulary — a denied
    sign match falls through to the ORDINARY gate/model path (no
    CONFIRM stamp, no sign dispatch; the CREATED-only chat cancel and
    the model's honest handling stay intact; review MEDIUM fix).
    ``'sign'`` with NO pending flow gets slice A's probe+report (report
    only — nothing to sign, so no unlock is driven). The unlock/connect/
    see families answer straight from the probe signer; without one
    (placeholder wiring) they stay ordinary chat, exactly as before."""
    verb = _hardware_chat_verb(line)
    if verb is None:
        return False
    if verb == "sign" and flow.state in (
        TxFlowStatus.CREATED,
        TxFlowStatus.CONFIRMED,
    ):
        if _hardware_sign_denied(line):
            return False
        return _route_hardware_sign(
            loop, flow, session, line, output_fn, table=table
        )
    if hwi is None:
        return False
    if verb == "sign":
        # No flow pending: probe+report WITHOUT driving an unlock (the
        # sign handoff itself never waits on one; slice A owns unlocks).
        # TCK-HW-006: the probe carries an additive wallet_match verdict;
        # a proven mismatch already IS one of the lines (the honest MW-17
        # copy, model-named by the signer) — the narration below is
        # otherwise exactly the pre-ticket bytes.
        for text in hwi.probe_and_report(attempt_unlock=False).lines:
            output_fn(sanitize_tool_output(text))
        return True
    for text in hwi.probe_and_report(attempt_unlock=verb != "see").lines:
        output_fn(sanitize_tool_output(text))
    return True


def _dispatch_code_sign_turn(
    loop: AgentLoop,
    session: SendSession,
    line: str,
    tx_ref: str,
    output_fn: Callable[[str], None],
    *,
    table: DispatchTable,
) -> dict[str, object]:
    """Shared body of the deterministic sign intercepts (TCK-HW-002 retry /
    TCK-HW-005 slice C hardware-word + file-fallback): CODE builds the
    ``sign_tx`` envelope from the dispatcher-owned confirmed ``tx_ref``
    (never a model- or user-supplied reference) and dispatches straight to
    the handler — whose flow gate, pre-sign device check, revalidation
    hard stop, and latch consumption are the unchanged pipeline. History
    records the turn like any dispatched turn would. Returns the handler
    result (the file-fallback intercept inspects it for latch upkeep)."""
    envelope = Envelope(
        v=0,
        intent=IntentName.SIGN_TX,
        params=SignTxParams(tx_ref=tx_ref),
    )
    result = table[IntentName.SIGN_TX](envelope)
    loop.add_turn(line, envelope.model_dump_json())
    _print_turn(
        AgentTurnResult(
            status=AgentTurnStatus.OK,
            envelope=envelope,
            result=result,
            user_message=None,
            turns_used=0,
        ),
        output_fn,
        session=session,
    )
    return result


def _dispatch_code_bump_turn(
    session: SendSession,
    line: str,
    params: BumpFeeParams,
    output_fn: Callable[[str], None],
    *,
    table: DispatchTable,
) -> dict[str, object]:
    """Shared body of the deterministic bump-conversation intercepts
    (TCK-RBF-004, the ``_dispatch_code_sign_turn`` pattern): CODE builds
    the ``bump_fee`` envelope from the dispatcher-owned ask/lineage state
    (the verbatim txid and choice index as stamped when the ask opened —
    never a model- or user-supplied reference) and dispatches straight to
    the handler, whose own guards remain the authority.

    UNLIKE the sign intercepts, the turn is NOT appended to the model
    transcript: an ask answer may be a coin LABEL word (user data — the
    §7.10 rule keeps labels out of model context through any channel, and
    history re-injects user text verbatim on later turns), so the bump
    conversation rides the transcript-free command/onboarding channel
    instead. Conversation state flows onward through the dispatcher-owned
    FACTS (the pending card follows a staged replacement like any other),
    never through history."""
    envelope = Envelope(v=0, intent=IntentName.BUMP_FEE, params=params)
    result = table[IntentName.BUMP_FEE](envelope)
    _print_turn(
        AgentTurnResult(
            status=AgentTurnStatus.OK,
            envelope=envelope,
            result=result,
            user_message=None,
            turns_used=0,
        ),
        output_fn,
        session=session,
    )
    return result


def _dispatch_code_self_turn(
    session: SendSession,
    line: str,
    params: SelfTransferParams,
    output_fn: Callable[[str], None],
    *,
    table: DispatchTable,
) -> dict[str, object]:
    """Shared body of the deterministic self_transfer-conversation answer
    intercepts (TCK-CPFP-002's cpfp dispatch, reused verbatim by the
    TCK-CONS-001 consolidation conversation; the
    :func:`_dispatch_code_bump_turn` pattern): CODE builds the
    ``self_transfer`` envelope from the dispatcher-owned ask state (the fee
    rung re-quoted from the ask; the CHOICE itself is stamped on the ask
    record, because the closed params cannot carry a coin reference — the
    model therefore can neither set, read, nor clear it) and dispatches
    straight to the self_transfer handler, whose own guards remain the
    authority. Transcript-free like the bump dispatch: an answer may be
    a coin LABEL word (user data — the §7.10 rule keeps labels out of
    model context through any channel)."""
    del line  # recorded nowhere the model sees (see docstring)
    envelope = Envelope(v=0, intent=IntentName.SELF_TRANSFER, params=params)
    result = table[IntentName.SELF_TRANSFER](envelope)
    _print_turn(
        AgentTurnResult(
            status=AgentTurnStatus.OK,
            envelope=envelope,
            result=result,
            user_message=None,
            turns_used=0,
        ),
        output_fn,
        session=session,
    )
    return result


# ---------------------------------------------------------------------------
# TCK-LABEL-001: chat label-by-address intercept (deterministic, PRE-MODEL).
# USER BUG (live, 2026-09-12): "label bc1q… as 'KYC'" → the narration claimed
# the label was set but the store held NOTHING — the utterance had no route
# at all: ``coin_labels`` is OUTPOINT-keyed (address ≠ txid:vout), so an
# address label had nowhere to land, and the model's success-shaped ``respond``
# was pure fabrication (an honest-response violation). The fix is the pair the
# bug lacked: the schema v5 ``address_labels`` table (store) + THIS intercept,
# which code-parses the address-literal phrasings BEFORE the model, writes
# through the typed accessor, and narrates only committed store truth (a
# success line exists iff the row exists). Precedents: the RBF-004 / CPFP-002
# / CONS-001 code-self-turn dispatches and the FIAT-003 pre-model word-table
# intercept. Labels are user-authored facts consumed by deterministic code —
# a consumed turn never reaches the transcript or the model (§1.1/§7.10).
# NON-address phrasings ("label my strike address as KYC") are deliberately
# NOT this intercept's grammar — the address must be a literal mainnet bech32
# token; everything else falls through to the unchanged model path (an honest
# route there needs a prompt line, and prompt changes force the eval gate:
# reported to the orchestrator, not touched here).
# ---------------------------------------------------------------------------

_ADDRESS_LABEL_VERB: Final[str] = "label"
_ADDRESS_LABEL_CONNECTORS: Final[frozenset[str]] = frozenset({"as", "is"})
#: Value-free refusals (plain cause + next step; the address/label are never
#: quoted on a failure — nothing was stored, so there is no value to echo).
_ADDRESS_LABEL_NO_VALUE: Final[str] = (
    'No label to store — try: label <address> as "your label"'
)
_ADDRESS_LABEL_TOO_LONG: Final[str] = (
    f"Labels are capped at {ADDRESS_LABEL_MAX_CHARS} characters — shorten "
    "the label and try again; nothing was stored."
)
_ADDRESS_LABEL_GARBAGE: Final[str] = (
    'I couldn\'t read that label — try: label <address> as "your label" '
    "(quoted text must close, and nothing may follow the closing quote); "
    "nothing was stored."
)
_ADDRESS_LABEL_STORE_ERROR: Final[str] = (
    "I couldn't save that label — the store refused the write; nothing was "
    "stored."
)


def _is_mainnet_bech32_address(token: str) -> bool:
    """Strict SHAPE gate for the intercept's address token (mainnet-only,
    ADR-0021): a ``bc1`` string that survives a real bech32/bech32m decode.

    embit's varied decode errors are CONTAINED (the psbt.py precedent): a
    bad token is a shape MISS, never a crash, and the token is never echoed.
    base58/other shapes decode fine as scripts but fail the ``bc1`` gate —
    this grammar speaks segwit address literals.
    """
    if not token.startswith("bc1"):
        return False
    try:
        address_to_scriptpubkey(token)
    except Exception:  # noqa: BLE001 — containment: embit raises varied errors
        return False
    return True


def _address_label_request(line: str) -> tuple[str, str] | str | None:
    """Code-parse a label-by-address utterance BEFORE the model.

    Grammar (the ticket's two phrasings, nothing looser)::

        label <address> as <label>
        label <address> <label>

    ``<address>`` must be a literal mainnet bech32 token (all-lower, or
    all-upper normalized to lower on write — bech32 case rules, never a
    mixed-case paste). ``<label>`` is a quoted string (``"…"``/``'…'``,
    balanced, nothing outside the quotes) or bounded trailing text:
    non-empty, ≤ :data:`ADDRESS_LABEL_MAX_CHARS`, printable. Returns:

    * ``None`` — NOT this grammar (including every non-address-literal
      phrasing: the line falls through to the unchanged pipeline);
    * a code-owned refusal string — verb+address matched but the VALUE is
      missing/overflowed/garbage. The turn is still CONSUMED with a
      value-free "nothing was stored" line: a half-parsed label utterance
      handed to the model is exactly the fabrication path this intercept
      closes;
    * ``(address, label)`` — validated, ready for the typed store write.
    """
    words = line.strip().split(maxsplit=2)
    if not words or words[0].lower() != _ADDRESS_LABEL_VERB or len(words) < 2:
        return None
    token = words[1]
    if not token.islower():
        if not token.isupper():
            return None  # mixed case is invalid bech32 — not an address literal
        token = token.lower()
    if not _is_mainnet_bech32_address(token):
        return None
    rest = words[2] if len(words) == 3 else ""
    head, sep, tail = rest.partition(" ")
    if sep and head.lower() in _ADDRESS_LABEL_CONNECTORS:
        rest = tail  # drop the connector; EVERYTHING after it is the label
    elif not sep and rest.lower() in _ADDRESS_LABEL_CONNECTORS:
        rest = ""  # connector with no value ("label <addr> as")
    rest = rest.strip()
    if rest[:1] in ("\"", "'"):
        if len(rest) < 2 or rest[-1] != rest[0]:
            return _ADDRESS_LABEL_GARBAGE  # unbalanced / trailing after close
        rest = rest[1:-1].strip()
    if not rest:
        return _ADDRESS_LABEL_NO_VALUE
    if len(rest) > ADDRESS_LABEL_MAX_CHARS:
        return _ADDRESS_LABEL_TOO_LONG
    if not rest.isprintable():
        return _ADDRESS_LABEL_GARBAGE
    return token, rest


def _run_address_label_turn(
    store: Store, line: str, output_fn: Callable[[str], None]
) -> bool:
    """Consume a label-by-address chat turn; True when the turn was consumed.

    STORE-TRUTH narration (the bug's other half): the success line prints
    ONLY from the record the typed writer read back after its commit — a row
    exists whenever this line is narrated, and a failed/absent write narrates
    a refusal instead, never a claim. The ack echoes the stored label
    verbatim and names the surface ADDRESS-LEVEL; when the address's coins
    are known from scan data the line still says only that the label applies
    to the ADDRESS and lists nothing else (no outpoints, no amounts). The
    coin-level ``/label`` command ships unchanged — the two surfaces coexist,
    distinctly named. The model is never consulted for a consumed turn, so
    label text never enters a prompt, the transcript, or FACTS (§7.10).
    """
    request = _address_label_request(line)
    if request is None:
        return False
    if isinstance(request, str):
        output_fn(sanitize_tool_output(request))
        return True
    address, label = request
    try:
        record = store.set_address_label(address, label)
    except (StoreError, sqlite3.Error):
        # Fail closed, value-free: the write did not commit → no row exists →
        # nothing may be claimed stored.
        output_fn(sanitize_tool_output(_ADDRESS_LABEL_STORE_ERROR))
        return True
    output_fn(
        sanitize_tool_output(
            f'Address {record.address} is now labeled "{record.label}" — '
            "an address-level label for the whole address; coin tags are a "
            "separate surface (/label lists and sets those, per coin)."
        )
    )
    return True


def _run_turn(
    loop: AgentLoop,
    flow: TxFlow,
    session: SendSession,
    line: str,
    output_fn: Callable[[str], None],
    *,
    client: ChainClient | None = None,
    table: DispatchTable,
    scan_gate: StartupScan | None = None,
    hwi: HwiUsbSigner | None = None,
    store: Store | None = None,
) -> None:
    """Run ONE REPL turn: gate classification → agent → flow narration.

    Dual-key wiring (ADR-0013): at the top of the turn — BEFORE the
    model runs — a live pending transaction puts the user's utterance
    through :meth:`ConfirmGate.classify` and stores the decision on the
    session, so the ``confirm_tx`` handler (which runs inside
    ``loop.run``) consumes a gate decision from the SAME turn.

    ``scan_gate`` (TCK-SCAN-003): while the first startup scan has not
    completed — running, or skipped after a failed startup scan (the
    cache was never populated by this run's scan; the handler results
    carry the matching ``freshness: stale``, ADR-0022 decision 5
    security-review fix) — a tool-owned ``freshness=stale`` fact is
    added to the turn's FACTS so the model narrates the loading state
    honestly (it never authors the claim — the gate computes it from
    completion state). ``None``/disabled/complete adds nothing.

    - DENY while pending: the gate decision is authoritative — the flow
      is cancelled proactively (no model cancel intent is waited for)
      and the cancellation is narrated after the turn's own output.
    - AMBIGUOUS while pending: the turn proceeds normally and a guidance
      line asks the user to confirm or cancel explicitly.
    - CONFIRM while the flow is still CREATED after the turn (the model
      did not emit ``confirm_tx``): a guidance line — the flow is
      untouched.
    - NOT_A_DECISION: normal chat; a pending card simply stays pending.
    - CONFIRMED + bare "retry" (TCK-HW-002, MW-4): intercepted BEFORE the
      model — the ``sign_tx`` handler is re-invoked directly with the
      dispatcher-owned confirmed ``tx_ref`` (the SAME code path the model
      envelope dispatches to; the model can never reach here without the
      user typing "retry", and an LLM "retry" is never consulted). Every
      other state and every other utterance takes the unchanged pipeline.
    - Hardware probe/unlock utterances (TCK-HW-005 slices A + C): the
      deterministic :func:`_hardware_chat_verb` matcher intercepts the
      "can you see / I connected / unlock my hardware wallet" family
      BEFORE the gate and the model — the live signer answers with the
      value-free probe report (and drives the Jade/BitBox02 host unlock
      on the unlock/connect families). No intent, no envelope, no sign
      path; the model never deflects with "I cannot access your hardware
      wallet" (a WRONG answer — the app CAN, via hwi). Slice C extends
      the matcher with the SIGN family: "sign with my hardware wallet"
      while a flow pends routes through the real confirm/sign dispatch
      (device-checked in the handler, model never consulted); any
      deny/negation token in the utterance ("don't sign with my hardware
      wallet", "cancel and sign…") SUPPRESSES the routing — the line
      falls through to the ordinary gate/model path (no CONFIRM stamp,
      chat cancel intact; review MEDIUM fix). With no flow pending it
      degrades to the slice-A report; the bare "file"/"export" answer to
      the device-absent ask (CONFIRMED only) runs the explicit export
      (the device latch survives a FAILED export; review LOW fix).
    - GATE-MERGE (TCK-UX-002, ADR-0013 amendment): when the turn's
      ``confirm_tx`` succeeds, the device handoff (``sign_tx`` handler,
      code-built envelope from the dispatcher-owned confirmed ref) runs in
      the SAME turn — the card asks once ("sign"), the state machine keeps
      CONFIRMED/SIGNED internally, and broadcast stays a separately gated
      turn. An LLM "yes" never counts: the chain only follows a dual-key
      confirm that already passed.
    - Per-ask currency one-shot (TCK-FIAT-003, MW-17): before the model
      runs, :func:`_detect_fiat_ask_currency` matches a closed currency
      WORD table against the user's own utterance and stamps the one-shot
      on ``session.fiat_ask_currency`` for exactly the turn's handlers
      (the oracle's display-currency reader consults it); cleared when the
      turn's dispatch ends — the ``display_currency`` setting is never
      touched, and an ambiguous/absent ask rides the ladder as before.
    - FACTS (TCK-P2-004 SR fix, extended to the full lifecycle in
      TCK-P3-005): the flow's current state is injected as the turn's
      FACTS block (:func:`_flow_facts`) — the pending card while
      ``CREATED``, the approved record's reference while ``CONFIRMED``,
      the signed reference while ``SIGNED``, and the broadcast txid while
      ``BROADCAST`` — so every destructive/status envelope can quote the
      dispatcher-owned value verbatim (the model never sees the printed
      narration). After a DENY-cancel no pending exists and the facts
      stay empty; the gate decision above remains the only confirmation
      authority either way.
    """
    # TCK-HW-002 (MW-4): deterministic re-sign interception — device-error
    # guidance tells the user to say 'retry'; routing that bare utterance
    # through the model loses the intent. The envelope is built by CODE
    # from dispatcher-owned flow state (the confirmed record's tx_ref —
    # never a model- or user-supplied reference) and dispatched straight to
    # the sign_tx handler; the handler's own flow gate remains the
    # authority. History records the turn like any dispatched turn would.
    # TCK-HW-005 slice C: while the user's words latched "device for this
    # flow", this same dispatch RE-PROBES the device (the handler reads the
    # latch) — "retry" after the file-offering ask means "device again".
    confirmed = flow.confirmed if flow.state is TxFlowStatus.CONFIRMED else None
    if confirmed is not None and line.strip().lower() == "retry":
        _dispatch_code_sign_turn(
            loop, session, line, confirmed.tx_ref, output_fn, table=table
        )
        return
    # TCK-HW-005 slice C: the explicit file fallback ANSWER to the
    # device-absent ask — CONFIRMED-scoped (pin), so a stray "file"/
    # "export" anywhere else stays ordinary chat. One-shot session stamp;
    # the handler consumes it and runs the UNCHANGED airgap export (even
    # under the hwi config — the user's own word, never the model's).
    if (
        confirmed is not None
        and IntentName.SIGN_TX in table
        and _file_export_choice(line)
    ):
        session.file_sign_export_once = True
        try:
            result = _dispatch_code_sign_turn(
                loop, session, line, confirmed.tx_ref, output_fn, table=table
            )
        finally:
            # The handler consumes this flag at its dispatch head; this
            # finally also covers the refusal paths that return BEFORE it
            # (a stale one-shot must never arm a later turn's sign).
            session.file_sign_export_once = False
        # LOW review fix: retire the device latch only when the file leg
        # actually RAN (export handed off, or a placed file imported and
        # signed). A failed export/import keeps the user's device
        # preference, so the next "retry" still re-probes the device.
        if (
            result.get("status") == "signed"
            or result.get("error") == "signed_file_missing"
        ):
            session.hw_sign_wanted = False
        return
    # TCK-HW-005 slices A + C: deterministic hardware utterance intercept —
    # BEFORE the gate and the model. The envelope-free narration comes
    # straight from the signer's value-free report or the EXISTING guidance
    # family; "sign with my hardware wallet" while a flow pends ROUTES the
    # unchanged sign path (device-checked in the handler, never the LLM).
    if _run_hardware_chat(
        loop, flow, session, line, output_fn, table=table, hwi=hwi
    ):
        return
    # TCK-RBF-004: the bump conversation's two deterministic intercepts,
    # BOTH BEFORE the gate and the model (the never-trap / retry precedent).
    #   (a) An OPEN funding/target ASK: the answer is a number, a framing
    #       word, or a coin LABEL word (matched HERE against the stored
    #       label — the label never reaches the model), stamped onto a
    #       CODE-built ``bump_fee`` envelope and dispatched straight to the
    #       handler. Any OTHER utterance closes the ask (never-trap) and
    #       falls through to the ordinary pipeline unchanged.
    #   (b) After a bump BROADCASTS, a bare "faster"/"slower" routes to a
    #       NEW bump of the NEW transaction (the existing fee-target
    #       vocabulary), never to ``create_tx``.
    # TCK-CPFP-002 adds the cpfp conversation's identical intercept for an
    # OPEN coin/options ask (checked right after the bump block — when
    # both conversations have an open ask, an answer-shaped utterance
    # resolves to the bump one; any other utterance closes both).
    ask = session.bump_ask
    if ask is not None and IntentName.BUMP_FEE in table:
        choice = _bump_funding_answer(line, ask)
        if choice is not None:
            if ask.kind == "target":
                target = str(ask.entries[choice - 1]["txid"])
                params = BumpFeeParams(target=target, **_bump_ask_fee_kwargs(ask))
            else:
                params = BumpFeeParams(
                    target=ask.old_txid,
                    funding_ref=str(choice),
                    **_bump_ask_fee_kwargs(ask),
                )
            _dispatch_code_bump_turn(session, line, params, output_fn, table=table)
            return
        # Any other utterance closes a live ask (never-trap) before the
        # ordinary pipeline sees it.
        session.bump_ask = None
    # TCK-CPFP-002: the cpfp conversation's deterministic intercept — the
    # SAME machinery BEFORE the gate and the model (the bump block above
    # it keeps priority: at most one conversation's answer is matchable
    # per utterance, and any non-match closes BOTH asks). An OPEN coin/
    # options ask is answered by a number / framing word / coin LABEL
    # word (label matched in code — the label words never reach the
    # model); the choice is stamped on the dispatcher-owned ask record
    # (cpfp params cannot carry a reference), the fee rung is re-quoted
    # onto the CODE-built envelope, and dispatch goes straight to the
    # self_transfer handler. Any OTHER utterance closes the ask
    # (never-trap) and falls through to the ordinary pipeline unchanged.
    cpfp_ask = session.cpfp_ask
    if cpfp_ask is not None and IntentName.SELF_TRANSFER in table:
        choice = _cpfp_answer(line, cpfp_ask)
        if choice is not None:
            cpfp_ask.choice = choice
            cpfp_params: dict[str, object] = {"mode": "cpfp"}
            if cpfp_ask.kind == "options":
                cpfp_params["merge_coin"] = (
                    cpfp_ask.options[choice - 1].framing != "plain"
                )
            if cpfp_ask.fee_target is not None:
                cpfp_params["fee_target"] = cpfp_ask.fee_target
            _dispatch_code_self_turn(
                session, line, SelfTransferParams(**cpfp_params), output_fn, table=table
            )
            return
        session.cpfp_ask = None
    # TCK-CONS-001: the consolidation conversation — the SAME machinery,
    # checked right after the cpfp block (when several conversations hold an
    # open ask, an answer-shaped utterance resolves to the earliest one —
    # the documented double-open corner; any other utterance closes ALL).
    # An OPEN rollup/count/list ask is answered by a number / label word /
    # registry number (matched HERE in code — label words never reach the
    # model), and a consolidation-shaped utterance OPENS the conversation
    # before the model ever sees it (the RBF-004 lesson generalized to the
    # opening). Consumed turns never touch the transcript
    # (:func:`_dispatch_code_self_turn`); any unmatched utterance closes the
    # ask (never-trap) and falls through to the ordinary pipeline.
    if (
        store is not None
        and IntentName.SELF_TRANSFER in table
        and _run_consolidation_turn(session, store, flow, line, output_fn, table=table)
    ):
        return
    # TCK-LABEL-001: chat label-by-address ("label bc1… as 'KYC'") — the
    # deterministic pre-model intercept for the bug that had NO route: the
    # code-parsed value writes the schema v5 address_labels row and the ack
    # narrates only committed store truth (row exists ⟺ success line). The
    # consumed turn never reaches the transcript or the model (§7.10);
    # value-free refusals consume too — a half-parsed label utterance must
    # not reach the model either. Checked after the conversation intercepts
    # (their open asks still close on a non-matching utterance — never-trap).
    if store is not None and _run_address_label_turn(store, line, output_fn):
        return
    speed = _bump_speed_choice(line)
    if speed is not None and IntentName.BUMP_FEE in table:
        if (
            flow.state is TxFlowStatus.BROADCAST
            and session.bump_bcast_txid is not None
            and flow.txid == session.bump_bcast_txid
        ):
            # Deliverable 7: a bare speed word right after a bump
            # broadcast → a NEW bump of the NEW tx (fee-target vocab),
            # never a create_tx.
            rung = _bump_next_rung(
                flow.confirmed.fee_target if flow.confirmed else None, speed
            )
            if rung is not None:
                _dispatch_code_bump_turn(
                    session,
                    line,
                    BumpFeeParams(target=session.bump_bcast_txid, fee_target=rung),
                    output_fn,
                    table=table,
                )
                return
        elif (
            flow.state is TxFlowStatus.CREATED
            and session.bump_pending is not None
            and flow.pending is not None
            and flow.pending.tx_ref == session.bump_pending.tx_ref
        ):
            # A speed word while a replacement still PENDING re-bumps the
            # SAME original at the new rung (the handler's re-bump branch,
            # commit-only-on-success swap of the staged replacement).
            rung = _bump_next_rung(flow.pending.fee_target, speed)
            if rung is not None:
                _dispatch_code_bump_turn(
                    session,
                    line,
                    BumpFeeParams(target=session.bump_pending.old_txid, fee_target=rung),
                    output_fn,
                    table=table,
                )
                return
    session.gate_decision = (
        ConfirmGate.classify(line)
        if flow.state is TxFlowStatus.CREATED
        else GateDecision.NOT_A_DECISION
    )
    cancelled = False
    if session.gate_decision is GateDecision.DENY and flow.state is TxFlowStatus.CREATED:
        flow.cancel()
        cancelled = True
        # TCK-HW-005 slice C: the device wish belonged to THIS flow — a
        # cancelled flow retires the latch (never leaks onto the next one).
        session.hw_sign_wanted = False
        # TCK-RBF-004: a cancelled flow also retires any bump conversation
        # state that belonged to THIS staged replacement (never leaks onto
        # the next flow). A carried re-bump decomposition is only valid
        # while its pending lives.
        session.bump_pending = None
        session.bump_ask = None
        # TCK-CPFP-002: same retirement for the cpfp conversation — the
        # staged child's marker and any open ask belonged to THIS flow.
        session.cpfp_pending = None
        session.cpfp_ask = None
        # TCK-CONS-001: and for the consolidation conversation (a DENY
        # retires the ask AND the staged plan marker — neither leaks onto
        # the next flow).
        session.cons_ask = None
        session.cons_pending = None
    # Narration-only ETA fact (TCK-P5-002): the mempool hint is computed
    # lazily ONLY when the flow is CREATED (the ETA fact is needed); any
    # failure degrades to no congestion adjustment, never a crash.
    seconds_since_last_block_fn = (
        (lambda: time_since_last_block(client)) if client is not None else None
    )
    facts = _flow_facts(
        flow, seconds_since_last_block_fn=seconds_since_last_block_fn
    )
    # TCK-CHAT-001: the stable address registry rides every turn's FACTS
    # (numbers + addresses + used-state) so the model can MAP numbered
    # phrasings to the right intent with the number as a param. It never
    # authors numbers — the handler re-resolves every one against the
    # store (the facts are routing help, not authority). A registry read
    # failure injects nothing (an unnumbered turn is safe: referent asks
    # then resolve to the handler's own store truth or the clarify) and
    # never crashes the turn over sugar.
    if store is not None:
        try:
            facts.update(_address_registry_facts(store))
        except (StoreError, sqlite3.Error):
            pass
    if scan_gate is not None and scan_gate.enabled and not scan_gate.complete:
        # ADR-0022 decision 5 (SR fix): the deterministic, tool-owned
        # freshness fact while the first scan has NOT completed — pending,
        # running, or skipped after a failed startup scan, matching the
        # handlers' stale flag. ``first_scan_incomplete`` stays the
        # narrower create_tx gate (decision 6): a skip unblocks sends.
        # The model narrates from it; it never authors a freshness claim.
        facts["freshness"] = FRESHNESS_STALE
    # TCK-FIAT-003 (MW-17): the per-ask currency one-shot — CODE reads the
    # user's OWN utterance (closed word table, whole tokens) BEFORE the
    # model sees the turn; the price oracle's injected reader
    # (:func:`_display_currency_reader`) consults it for exactly this
    # turn's handler dispatch, so "in Euros?" answers in EUR while the
    # display_currency SETTING stays untouched. The model still only routes
    # the phrasing to get_balance/create_tx — it never authors a currency
    # and the envelope never carries one. Cleared in a finally: a stale
    # one-shot must never arm a later turn (the file_sign_export_once
    # precedent).
    session.fiat_ask_currency = _detect_fiat_ask_currency(line)
    try:
        turn = loop.run(line, facts)
    finally:
        session.fiat_ask_currency = None
    _print_turn(turn, output_fn, session=session)
    # GATE-MERGE (TCK-UX-002, ADR-0013 amendment): a successful confirm
    # chains straight into the device handoff IN THE SAME TURN — the
    # card's ask word "sign" completes "review + hand to device" in one
    # user step (the CONFIRMED/SIGNED states stay distinct; only the
    # prompts merged). The envelope is CODE-built from the dispatcher-
    # owned confirmed tx_ref (never model- or user-supplied) and dispatched
    # straight to the sign_tx handler; the handler's own flow gate and the
    # re-validation hard stop are unchanged, and nothing broadcasts here —
    # broadcast keeps its own separately-gated turn.
    envelope = turn.envelope
    confirmed = flow.confirmed
    if (
        envelope is not None
        and envelope.intent is IntentName.CONFIRM_TX
        and flow.state is TxFlowStatus.CONFIRMED
        and confirmed is not None
    ):
        sign_envelope = Envelope(
            v=0,
            intent=IntentName.SIGN_TX,
            params=SignTxParams(tx_ref=confirmed.tx_ref),
        )
        sign_result = table[IntentName.SIGN_TX](sign_envelope)
        _print_turn(
            AgentTurnResult(
                status=AgentTurnStatus.OK,
                envelope=sign_envelope,
                result=sign_result,
                user_message=None,
                turns_used=0,
            ),
            output_fn,
            session=session,
        )
    if cancelled:
        output_fn(sanitize_tool_output(_CANCELLED_LINE))
        return
    if flow.state is TxFlowStatus.CREATED:
        guidance = (
            _GUIDANCE_AMBIGUOUS
            if session.gate_decision is GateDecision.AMBIGUOUS
            else _GUIDANCE_STILL_PENDING
            if session.gate_decision is GateDecision.CONFIRM
            else None
        )
        if guidance is not None:
            output_fn(sanitize_tool_output(guidance))


def _print_turn(
    turn: AgentTurnResult,
    output_fn: Callable[[str], None],
    *,
    session: SendSession | None = None,
) -> None:
    """Print one agent turn according to its status and intent.

    Every printed value comes verbatim from the handler result dict —
    the UI computes nothing (txid shortening is display truncation of
    tool output, per the narration contract). All strings pass through
    :func:`~localwallet.agent.context.sanitize_tool_output` immediately
    before printing (SR-006: the envelope grammar permits ``\\uXXXX`` so
    ESC/bidi control characters must never reach the terminal).

    ``session`` (TCK-UX-002) carries the ``/details`` full-card render
    cache: a confirmation card printed through this turn is cached (full
    classic render) for the ``/details`` reprint while the flow stays in
    ``CREATED``. ``None`` (direct calls/tests) renders without caching.
    """
    if turn.status is AgentTurnStatus.CLARIFIED:
        output_fn(sanitize_tool_output(turn.user_message or _GENERIC_FAILURE))
        return
    if turn.status is AgentTurnStatus.FAILED:
        output_fn(sanitize_tool_output(turn.user_message or _GENERIC_FAILURE))
        return

    envelope = turn.envelope
    if envelope is None:  # pragma: no cover — OK turns always carry one
        output_fn(sanitize_tool_output(_GENERIC_FAILURE))
        return

    if envelope.intent is IntentName.RESPOND and isinstance(envelope.params, RespondParams):
        output_fn(sanitize_tool_output(envelope.params.text))
    elif envelope.intent is IntentName.CLARIFY and isinstance(envelope.params, ClarifyParams):
        output_fn(sanitize_tool_output(envelope.params.question))
    elif envelope.intent is IntentName.GET_BALANCE:
        _print_balance(turn.result or {}, output_fn)
    elif envelope.intent is IntentName.GET_HISTORY:
        _print_history(turn.result or {}, output_fn)
    elif envelope.intent is IntentName.GET_UTXOS:
        _print_utxos(turn.result or {}, output_fn)
    elif envelope.intent is IntentName.GET_ADDRESSES:
        _print_addresses(turn.result or {}, output_fn)
    elif envelope.intent is IntentName.NEW_ADDRESS:
        _print_new_address(turn.result or {}, output_fn)
    elif envelope.intent is IntentName.CREATE_TX:
        _print_create_tx(turn.result or {}, output_fn, session=session)
    elif envelope.intent is IntentName.SELF_TRANSFER:
        _print_self_transfer(turn.result or {}, output_fn, session=session)
    elif envelope.intent is IntentName.BUMP_FEE:
        _print_bump_fee(turn.result or {}, output_fn, session=session)
    elif envelope.intent is IntentName.CONFIRM_TX:
        _print_confirm_tx(turn.result or {}, output_fn)
    elif envelope.intent is IntentName.SIGN_TX:
        _print_sign_tx(turn.result or {}, output_fn)
    elif envelope.intent is IntentName.BROADCAST_TX:
        _print_broadcast_tx(turn.result or {}, output_fn, session=session)
    elif envelope.intent is IntentName.TX_STATUS:
        _print_tx_status(turn.result or {}, output_fn)
    elif envelope.intent is IntentName.NODE_STATUS:
        _print_node_status(turn.result or {}, output_fn)
    else:  # pragma: no cover — closed intent enum
        output_fn(sanitize_tool_output(_GENERIC_FAILURE))


def _error_line(result: Mapping[str, object], label: str) -> str:
    """Render a handler ``{"error": ..., "detail": ...}`` result for the UI.

    ``chain_unavailable`` keeps its human wording; ``price_unavailable``
    likewise (send-flow narration); other codes print as-is. Details are
    scrubbed by their layers (value-free of addresses/txids/amounts) and
    are safe to surface verbatim.
    """
    error = str(result.get("error", "error"))
    human = {
        "chain_unavailable": "chain unavailable",
        "price_unavailable": "price data unavailable",
    }.get(error, error)
    detail = str(result.get("detail", "")).strip()
    suffix = f" ({detail})" if detail else ""
    return f"{label} — {human}{suffix}"


def _print_balance(result: Mapping[str, object], output_fn: Callable[[str], None]) -> None:
    """Print the balance verbatim from the handler's result dict.

    A ``stale`` freshness flag (TCK-SCAN-003, ADR-0022 decision 5) adds one
    value-free note line — honest display of the tool-owned flag; the
    figures themselves print verbatim from the cache either way. Handler-
    supplied fiat (TCK-FIAT-001's USD keys or TCK-FIAT-002's currency-
    tagged trio) adds one fiat line, verbatim-formatted; absent keys
    render nothing. A
    ``scan_pending`` answer (TCK-UX-011, ADR-0022 amendment 2) prints the
    one static "first scan running in the background" line INSTEAD of the
    plain stale note (one honest line, never two) — static copy, no
    figures the handler did not already print verbatim.
    """
    if result.get("error") is not None:
        if result.get("error") == _ADDRESS_REF_UNKNOWN:
            output_fn(sanitize_tool_output(ADDRESS_REF_UNKNOWN))
            return
        output_fn(sanitize_tool_output(_error_line(result, "Balance lookup failed")))
        return
    if result.get("address_number") is not None:
        # TCK-CHAT-001 scoped answer: LEAD with the full-address
        # restatement — a number-only balance is the retargeting bug the
        # council named. Value verbatim from the handler's resolution.
        output_fn(
            sanitize_tool_output(
                f"Balance for address #{result['address_number']} — "
                f"{result.get('address', '')}:"
            )
        )
    confirmed = result.get("confirmed_sats", 0)
    unconfirmed = result.get("unconfirmed_sats", 0)
    total = result.get("total_sats", 0)
    scanned = result.get("addresses_scanned", 0)
    tip = result.get("tip_height", 0)
    # SR-006 minor 2: an absent tip_height (handler deliberately omitted it
    # after a tip-lookup failure) must print "tip unavailable", never a
    # fabricated "tip height 0".
    tip_label = f"tip height {tip}" if "tip_height" in result else "tip unavailable"
    output_fn(
        f"Balance (mainnet): {confirmed} sats (confirmed) + {unconfirmed} sats (unconfirmed)"
    )
    output_fn(
        f"Total {total} sats · {scanned} addresses with UTXOs · {tip_label}"
    )
    # Fiat line (TCK-FIAT-001; TCK-FIAT-002 multi-currency): rendered iff
    # the handler supplied fiat keys — figures verbatim from the result
    # dict, formatted like the send card's USD segment (``_card_rate``
    # thousands separation, the same stale/age marker wording), the
    # currency LABEL riding the tagged keys (``1,234.56 EUR`` /
    # ``8,900,000 JPY``); absent keys print nothing (a sats-only answer
    # never grows a dishonest fiat line).
    fiat = _fiat_pair(result)
    if fiat is not None:
        fiat_line = f"≈ {_fiat_text(fiat[0], fiat[1], grouped=True)}"
        if result.get("rate_stale"):
            rate_age = result.get("rate_age_s")
            if rate_age is not None:
                fiat_line += f" · rate age {rate_age}s"
            fiat_line += " · stale"
        else:
            segment = _card_rate_segment(result)
            if segment is not None:
                fiat_line += f" · {segment}"
        output_fn(sanitize_tool_output(fiat_line))
    if result.get("scan_pending") is True:
        # TCK-UX-011 (ADR-0022 amendment 2): the tool kicked the background
        # load for this very answer — ONE static, value-free line,
        # subsuming the plain stale note (never two notes on one answer).
        output_fn(sanitize_tool_output(SCAN_PENDING_NOTE))
    else:
        _print_freshness_note(result, output_fn)


def _print_freshness_note(
    result: Mapping[str, object], output_fn: Callable[[str], None]
) -> None:
    """One value-free note line when the tool flagged the answer
    ``stale`` (first scan not complete, ADR-0022); nothing when fresh."""
    if result.get("freshness") == FRESHNESS_STALE:
        output_fn(sanitize_tool_output(FRESHNESS_NOTE))


def _print_history(result: Mapping[str, object], output_fn: Callable[[str], None]) -> None:
    """Print history lines: ``tx <short-txid>… <direction> <height|unconfirmed>``.

    Address-free by contract (P1 narration): only txid/direction/height
    are shown; values are verbatim from the handler result dict. A
    stale-flagged answer (ADR-0022) leads with the value-free loading note
    — an empty cache during the first scan must never read as a final
    "No transactions found."
    """
    if result.get("error") is not None:
        output_fn(sanitize_tool_output(_error_line(result, "History lookup failed")))
        return
    _print_freshness_note(result, output_fn)
    transactions = result.get("transactions")
    if not isinstance(transactions, list) or not transactions:
        output_fn(sanitize_tool_output("No transactions found."))
        return
    for tx in transactions:
        if not isinstance(tx, dict):  # pragma: no cover — handler-shaped data
            continue
        txid = str(tx.get("txid", ""))
        short = f"{txid[:12]}…" if txid else "tx <unknown>"
        height = tx.get("height")
        height_label = str(height) if height is not None else "unconfirmed"
        direction = str(tx.get("direction", "?"))
        output_fn(sanitize_tool_output(f"tx {short} {direction} {height_label}"))


def _print_utxos(result: Mapping[str, object], output_fn: Callable[[str], None]) -> None:
    """Print one line per UTXO with the address verbatim from the result.

    A stale-flagged answer (ADR-0022) leads with the value-free loading
    note — an empty/partial cache during the first scan must never read
    as a final "No unspent outputs." When the handler flagged pending
    items (TCK-PENDING-001), one compact summary line follows — counts
    and the incoming sat sum verbatim from the tool-owned ``pending_*``
    keys (the UI computes nothing), the unrecorded-outgoing-amount bound
    stated in copy, never an address — plus the tool's static
    confirm-likelihood note line (verbatim; on the cache-served pending
    path it is the honest no-estimate degrade, never a minute figure).
    """
    if result.get("error") is not None:
        if result.get("error") == _ADDRESS_REF_UNKNOWN:
            output_fn(sanitize_tool_output(ADDRESS_REF_UNKNOWN))
            return
        output_fn(sanitize_tool_output(_error_line(result, "UTXO lookup failed")))
        return
    _print_freshness_note(result, output_fn)
    _print_pending_block(result, output_fn)
    if result.get("address_number") is not None:
        # TCK-CHAT-001 scoped listing: the full-address restatement leads.
        output_fn(
            sanitize_tool_output(
                f"Coins on address #{result['address_number']} — "
                f"{result.get('address', '')}:"
            )
        )
    utxos = result.get("utxos")
    if not isinstance(utxos, list) or not utxos:
        output_fn(sanitize_tool_output("No unspent outputs."))
        return
    for utxo in utxos:
        if not isinstance(utxo, dict):  # pragma: no cover — handler-shaped data
            continue
        address = utxo.get("address")
        number = utxo.get("number")
        # Stable registry number prefix on every printed own address
        # (TCK-CHAT-001); an address this answer did not register simply
        # carries no prefix — never a fabricated one.
        number_part = f"#{number} " if isinstance(number, int) else ""
        address_part = f"{number_part}{address} · " if address else ""
        confirmed_label = "confirmed" if utxo.get("confirmed") else "unconfirmed"
        txid = str(utxo.get("txid", ""))
        short = f"{txid[:12]}…" if txid else "tx <unknown>"
        output_fn(
            sanitize_tool_output(
                f"{address_part}{utxo.get('value_sats', 0)} sats · "
                f"{confirmed_label} · tx {short} vout {utxo.get('vout', 0)}"
            )
        )


def _print_pending_block(
    result: Mapping[str, object], output_fn: Callable[[str], None]
) -> None:
    """The one-line pending summary + the tool's confirm-likelihood line.

    Absent keys (nothing pending) print nothing — a clean wallet's
    answer is byte-identical to before TCK-PENDING-001. Figures are
    verbatim from the handler result; the wording around them is static
    copy. The outgoing segment states the recorded-data bound
    ("amount not recorded") instead of hiding the missing figure.
    """
    incoming = result.get("pending_incoming_count", 0)
    outgoing = result.get("pending_outgoing_count", 0)
    if not isinstance(incoming, int) or not isinstance(outgoing, int):
        return  # malformed shape: print nothing rather than guess (fail quiet)
    if incoming <= 0 and outgoing <= 0:
        return
    segments: list[str] = []
    if incoming > 0:
        sats = result.get("pending_incoming_sats", 0)
        segments.append(f"{incoming} incoming for {sats} sats")
    if outgoing > 0:
        segments.append(f"{outgoing} outgoing (amount not recorded)")
    output_fn(sanitize_tool_output("Pending: " + " · ".join(segments)))
    note = result.get("pending_eta_note")
    if isinstance(note, str) and note:
        output_fn(sanitize_tool_output(note))


def _print_new_address(result: Mapping[str, object], output_fn: Callable[[str], None]) -> None:
    """Print the fresh address verbatim from the handler's result dict.

    TCK-CHAT-001: the fresh address is a FIRST SHOWING — the handler
    registered it, so the line names its stable number too (a result
    without the number key renders the pre-CHAT-001 line unchanged: the
    printer never fabricates one)."""
    if result.get("error") is not None:
        output_fn(
            sanitize_tool_output(_error_line(result, "Could not allocate a new address"))
        )
        return
    branch = result.get("branch", 0)
    kind = "change" if branch == 1 else "receive"
    number = result.get("address_number")
    if isinstance(number, int) and not isinstance(number, bool):
        output_fn(
            sanitize_tool_output(
                f"Fresh {kind} address (index {result.get('index', 0)}, "
                f"address #{number}): {result.get('address', '')}"
            )
        )
        return
    output_fn(
        sanitize_tool_output(
            f"Fresh {kind} address (index {result.get('index', 0)}): "
            f"{result.get('address', '')}"
        )
    )


def _print_addresses(result: Mapping[str, object], output_fn: Callable[[str], None]) -> None:
    """Narrate the TCK-CHAT-001 registry answer (list or one restatement).

    Every row prints the FULL address verbatim from the handler result
    (number-only answers are the bug); framing copy is static and
    value-free. The header honestly defines "used" and states the
    freshness bound of the last scan; the first-shown DATE itself is never
    narrated (one FAQ-style line is its only visible copy); the
    numbered-referent hint rides the ``hint_new`` flag exactly once per
    wallet lifetime. The label slot renders ``unlabeled`` while
    address-keyed labels do not exist — honest empty, never invented.
    """
    error = result.get("error")
    if error is not None:
        if error == _ADDRESS_REF_UNKNOWN:
            output_fn(sanitize_tool_output(ADDRESS_REF_UNKNOWN))
            return
        output_fn(sanitize_tool_output(_error_line(result, "Address lookup failed")))
        return
    _print_freshness_note(result, output_fn)
    entries = result.get("addresses")
    if not isinstance(entries, list):  # pragma: no cover — handler-shaped
        entries = []
    if not entries:
        output_fn(sanitize_tool_output(ADDRESSES_NONE_SHOWN))
        return
    scoped = result.get("requested_number") is not None
    if not scoped:
        output_fn(
            sanitize_tool_output(
                "Your addresses, with the number each one keeps for good:"
            )
        )
        bound = _scan_bound_phrase(result.get("last_scan_at"), result.get("freshness"))
        output_fn(sanitize_tool_output(f'"Used" means we\'ve seen activity — {bound}.'))
        output_fn(sanitize_tool_output(ADDRESS_TRACKING_FAQ_LINE))
    for entry in entries:
        if not isinstance(entry, dict):  # pragma: no cover — handler-shaped
            continue
        parts = [
            f"#{entry.get('number')}",
            str(entry.get("address", "")),
            "used" if entry.get("used") is True else "not used yet",
            # Honest empty slot until TCK-CHAT-003's address labels exist.
            str(entry.get("label")) if entry.get("label") else "unlabeled",
        ]
        sats = entry.get("sats_total")
        if isinstance(sats, int) and not isinstance(sats, bool):
            parts.append(f"{sats} sats")
        output_fn(sanitize_tool_output(" · ".join(parts)))
    if result.get("hint_new") is True:
        output_fn(sanitize_tool_output(ADDRESS_REF_HINT))


def _scan_bound_phrase(last_scan_at: object, freshness: object) -> str:
    """The freshness BOUND of the 'used' definition (header honesty).

    Renders only what the tool recorded: the last scan's date (a scan
    stamp, display-only, never a narration of any per-address shown
    date), or the honest no-scan state. Never a claim of currency the
    data does not support (ADR-0022 freshness discipline)."""
    if freshness == FRESHNESS_STALE:
        return "as of the last completed scan — none yet, this wallet is still loading"
    if isinstance(last_scan_at, str) and last_scan_at[:4].isdigit():
        return f"as of your last scan ({last_scan_at[:10]})"
    return "as of the last completed scan"


def _print_self_plan(
    result: Mapping[str, object],
    output_fn: Callable[[str], None],
    session: SendSession | None = None,
) -> None:
    """Render a staged self-transfer PLAN card (TCK-TX-SELF-001).

    Same brief-card discipline as the send flow: every value comes verbatim
    from the handler result dict (an absent/non-int numeric key renders an
    explicit ``unavailable`` marker — never a fabricated 0, TCK-SEC-004
    class); no address is ever named (the plan's destinations are internal
    engine state, not user-review material — the DEVICE shows them, and
    their count is all the card claims). Shared by the fresh
    ``self_transfer`` card and the ``tx_pending`` re-show (so a plan that
    pends while the user tries something else is re-shown AS THE PLAN it
    is, never as a misleading one-recipient send). The full
    ``/details`` render (adds Expires + Ref) is cached on ``session``
    exactly like the send card's.
    """
    parts = _card_sats(result, "self_parts")
    each = _card_sats(result, "self_each_sats")
    in_total = _card_sats(result, "self_inputs_total_sats")
    sources = _card_sats(result, "inputs_count")
    mode = result.get("self_mode")
    if isinstance(mode, str) and mode == "split" and parts is not None:
        plan = f"Plan: split 1 coin into {parts} × {each or 'unavailable'} sats"
        addr_count = _card_sats(result, "self_new_addresses")
        plan += (
            f" → {addr_count} fresh addresses"
            if addr_count is not None
            else " → fresh addresses"
        )
    else:
        plan = ""
        if result.get("cons_merge") is True:
            # TCK-CONS-001 plan echo: the conversation's consolidation,
            # stated as what the ENGINE planned — input count and the new
            # UTXO's value quoted from this result's own record fields
            # (the same fail-closed rule: an absent/non-int figure falls
            # back to the generic plan line, never a fabricated number).
            n_in = result.get("inputs_count")
            amt = result.get("amount_sats")
            if (
                isinstance(n_in, int)
                and not isinstance(n_in, bool)
                and isinstance(amt, int)
                and not isinstance(amt, bool)
            ):
                plan = _CONS_PLAN_LINE.format(
                    sources=n_in, s="s" if n_in != 1 else "", amount=amt
                )
        if not plan:
            merged = (
                f"{sources} small coin{'s' if sources != '1' else ''}"
                if sources is not None
                else "small coins"
            )
            plan = (
                f"Plan: merge {merged} into 1 × {each or 'unavailable'} sats "
                "(1 fresh address)"
            )
    in_line = (
        f"In: {in_total} sats" if in_total is not None else "In: unavailable"
    )
    if sources is not None:
        in_line += f" from {sources} {'source' if sources == '1' else 'sources'}"
    fee_sats = _card_sats(result, "fee_sats")
    fee = "Fee: unavailable" if fee_sats is None else f"Fee: {fee_sats} sats"
    if fee_sats is not None:
        rate = _card_fee_rate_text(result)
        if rate is not None:
            fee += f" · {rate} sat/vB"
        vsize = _card_sats(result, "vsize")
        if vsize is not None:
            fee += f" × {vsize} vB"
        target_word = result.get("fee_target")
        if isinstance(target_word, str) and target_word:
            fee += f" · {target_word}"
        eta_wording = result.get("eta_wording")
        if isinstance(eta_wording, str) and eta_wording:
            # Verbatim chain/eta.py hedge appended — never re-punctuated.
            fee += f" — ETA {eta_wording}"
    lines: list[str] = [_CARD_ASK_LINE, plan, in_line, fee]
    floor_note = _fee_floor_note_line(result)  # TCK-FEE-004 (see brief card)
    if floor_note is not None:
        lines.append(floor_note)
    other = _card_sats(result, "self_other_side_count")
    if other is not None:
        # Honest cross-pool note (the privacy pools are never mixed — this
        # run consolidated one side; the other side's small coins remain
        # for a second consolidate). Count only, no amounts.
        lines.append(
            f"Also {other} small coin{'s' if other != '1' else ''} sit among "
            "your other marked coins — consolidate again after this to "
            "merge those too."
        )
    lines.append(_CARD_DETAILS_TAIL)
    if session is not None:
        # The /details full render: plan lines minus the tail, plus
        # Destinations/Expires/Ref (the brief card demotes them, same as the
        # send card). Destinations are the plan's fresh receive addresses,
        # verbatim from the handler result, one per line with its amount.
        full = list(lines[:-1])
        dests = result.get("self_destinations")
        if isinstance(dests, list):
            for entry in dests:
                if not isinstance(entry, dict):
                    continue
                addr = entry.get("address")
                amt = entry.get("amount_sats")
                if not isinstance(addr, str) or not addr:
                    continue
                if isinstance(amt, int) and not isinstance(amt, bool):
                    full.append(f"To: {addr} ({amt} sats)")
                else:
                    full.append(f"To: {addr}")
        expires = result.get("expires_in_s")
        if isinstance(expires, int) and not isinstance(expires, bool):
            full.append(f"Expires: ~{expires // 60} min")
        ref = result.get("tx_ref")
        if isinstance(ref, str) and ref:
            # Value-free app-generated handle with its purpose (TCK-UX-014).
            full.append(
                f"Ref: {ref} — names this pending transaction if you ask to "
                "cancel or reprint it before it expires."
            )
        session.card_render = full
    for line in lines:
        output_fn(sanitize_tool_output(line))


def _print_bump_fee(
    result: Mapping[str, object],
    output_fn: Callable[[str], None],
    *,
    session: SendSession | None = None,
) -> None:
    """Narrate a ``bump_fee`` outcome (TCK-RBF-004).

    Success → the replacement PLAN CARD: the gate ask line (byte-identical
    to the send card's — the dual-key wording is nobody's to change), the
    two pinned rows (``Replaces: <old txid> — …only one of these two ever
    will`` and ``Fee: <old> → <new> (paying <delta> extra)``, every figure
    verbatim from the handler result — the builder's numbers, the FEE-003
    rate text), To/Pay/From context, and the ``/details`` demotion tail
    (the full render — Expires/Ref/ETA — cached on ``session`` exactly
    like the send card's).

    An open ASK (target disambiguation / funding chooser) prints its
    numbered options with the resolver's/chooser's verbatim fields — the
    funding card additionally prints each coin's stored LABEL for the
    terminal (user data, print-only: handler results never enter model
    context, and answers come back through the deterministic intercept,
    so a label word never routes through the model).

    Refusals print their dispatcher-owned friendly lines; the floor
    refusal renders its sanctioned floor number from the STRUCTURED keys
    (the ``insufficient_funds`` precedent — amounts are card material,
    never detail-string material).
    """
    error = result.get("error")
    ask = result.get("ask")
    if error is None and ask == "target":
        options = result.get("options")
        count = len(options) if isinstance(options, list) else 0
        output_fn(sanitize_tool_output(_BUMP_TARGET_HEAD.format(count=count)))
        if isinstance(options, list):
            for entry in options:
                if not isinstance(entry, dict):  # pragma: no cover — handler-shaped
                    continue
                txid = str(entry.get("txid", ""))
                short = f"{txid[:12]}…" if txid else "tx <unknown>"
                amount = entry.get("amount_sats")
                amount_part = (
                    f"{amount:,} sats"
                    if isinstance(amount, int) and not isinstance(amount, bool)
                    else "amount not recorded"
                )
                rate_c = entry.get("fee_rate_centisat_vb")
                rate_part = (
                    f"{format_sat_vb(rate_c)} sat/vB"
                    if isinstance(rate_c, int) and not isinstance(rate_c, bool)
                    else "rate not recorded"
                )
                age = entry.get("age_s")
                age_part = (
                    f"~{max(1, int(age) // 60)} min old"
                    if isinstance(age, int) and not isinstance(age, bool)
                    else "age not recorded"
                )
                output_fn(
                    sanitize_tool_output(
                        f"  {entry.get('index')}. tx {short} · {amount_part} · "
                        f"{rate_part} · {age_part}"
                    )
                )
        return
    if error is None and ask == "funding":
        output_fn(sanitize_tool_output(_BUMP_FUNDING_HEAD))
        options = result.get("options")
        if isinstance(options, list):
            for entry in options:
                if not isinstance(entry, dict):  # pragma: no cover — handler-shaped
                    continue
                framing = str(entry.get("framing", "")) or "coin"
                value = entry.get("value_sats")
                value_part = (
                    f"{value:,} sats"
                    if isinstance(value, int) and not isinstance(value, bool)
                    else "amount not recorded"
                )
                line = f"  {entry.get('index')}. {framing} — {value_part}"
                label = entry.get("label")
                if isinstance(label, str) and label:
                    line += f" · labeled {label}"
                output_fn(sanitize_tool_output(line))
        output_fn(sanitize_tool_output(_BUMP_FUNDING_TAIL))
        return
    if error == "bump_floor_unreachable":
        floor = result.get("floor_sats")
        payable = result.get("max_payable_sats")
        floor_part = f"{floor:,}" if isinstance(floor, int) and not isinstance(floor, bool) else ""
        payable_part = (
            f"{payable:,}" if isinstance(payable, int) and not isinstance(payable, bool) else ""
        )
        reason = result.get("reason")
        if reason == "rate_below_floor":
            output_fn(sanitize_tool_output(_BUMP_FLOOR_RATE.format(floor_sats=floor_part)))
        elif reason == "rate_exceeds_funding":
            output_fn(
                sanitize_tool_output(
                    _BUMP_FLOOR_EXCEEDS.format(
                        max_payable_sats=payable_part, floor_sats=floor_part
                    )
                )
            )
        else:
            output_fn(sanitize_tool_output(_BUMP_FLOOR_FUNDING.format(floor_sats=floor_part)))
        return
    if error == "wallet_loading":
        output_fn(sanitize_tool_output(str(result.get("detail", "")) or WALLET_LOADING_REFUSAL))
        return
    if error is not None:
        friendly = {
            "bump_nothing_in_flight",
            "bump_already_confirmed",
            "bump_unrecorded",
            "bump_multi_output",
            "bump_flow_busy",
            "bump_funding_ref",
            "bump_plan_failed",
        }
        if error in friendly:
            # The refusal IS the UX: the handler's code-owned friendly line.
            output_fn(sanitize_tool_output(str(result.get("detail", "")).strip()))
            return
        output_fn(sanitize_tool_output(_error_line(result, "Could not prepare the fee bump")))
        return
    # Success: the replacement plan card.
    old_txid = str(result.get("replaces", ""))
    old_fee = _card_sats(result, "old_fee_sats")
    new_fee = _card_sats(result, "fee_sats")
    delta = _card_sats(result, "fee_delta_sats")
    lines: list[str] = [_CARD_ASK_LINE]
    if old_txid:
        lines.append(_BUMP_REPLACES_LINE.format(old_txid=old_txid))
    amount = _card_sats(result, "amount_sats")
    lines.append(f"To: {result.get('recipient', '')}")
    lines.append(f"Pay: {amount} sats" if amount is not None else "Pay: unavailable")
    if old_fee is not None and new_fee is not None and delta is not None:
        lines.append(
            _BUMP_FEE_LINE.format(old_fee=old_fee, new_fee=new_fee, delta=delta)
        )
    rate = _card_fee_rate_text(result)
    if rate is not None:
        lines.append(f"Rate: {rate} sat/vB · {result.get('vsize', '')} vB")
    floor_note = _fee_floor_note_line(result)  # TCK-FEE-004 (see brief card)
    if floor_note is not None:
        lines.append(floor_note)
    eta_wording = result.get("eta_wording")
    if isinstance(eta_wording, str) and eta_wording:
        lines.append(f"ETA: {eta_wording}")
    sources = result.get("inputs_count")
    if isinstance(sources, int) and not isinstance(sources, bool):
        lines.append(f"From: your wallet ({sources:,} source{'s' if sources != 1 else ''})")
    else:
        lines.append("From: your wallet (sources unavailable)")
    lines.append(_CARD_DETAILS_TAIL)
    for line in lines:
        output_fn(sanitize_tool_output(line))
    if session is not None:
        full = lines[:-1]
        expires = result.get("expires_in_s")
        if isinstance(expires, int) and not isinstance(expires, bool):
            full.append(f"Expires: ~{expires // 60} min")
        ref = result.get("tx_ref")
        if isinstance(ref, str) and ref:
            full.append(
                f"Ref: {ref} — names this pending transaction if you ask to "
                "cancel or reprint it before it expires."
            )
        session.card_render = full


def _print_cpfp_coin_ask(
    result: Mapping[str, object], output_fn: Callable[[str], None]
) -> None:
    """The indexed stuck-payment choice ask (TCK-CPFP-002 deliverable 1):
    amount + age + destination label, every field verbatim from the
    store's rows (an unrecorded age says so, never invented — the bump
    target-ask's shape and honesty). The never-trap wording rides the
    head line itself."""
    options = result.get("options")
    count = len(options) if isinstance(options, list) else 0
    output_fn(sanitize_tool_output(_CPFP_TARGET_HEAD.format(count=count)))
    if not isinstance(options, list):  # pragma: no cover — handler-shaped
        return
    for entry in options:
        if not isinstance(entry, dict):  # pragma: no cover — handler-shaped
            continue
        txid = str(entry.get("txid", ""))
        short = f"{txid[:12]}…" if txid else "tx <unknown>"
        amount = entry.get("value_sats")
        amount_part = (
            f"{amount:,} sats"
            if isinstance(amount, int) and not isinstance(amount, bool)
            else "amount not recorded"
        )
        age = entry.get("age_s")
        age_part = (
            f"~{max(1, int(age) // 60)} min old"
            if isinstance(age, int) and not isinstance(age, bool)
            else "age not recorded"
        )
        line = (
            f"  {entry.get('index')}. {amount_part} · {age_part} · "
            f"tx {short} vout {entry.get('vout', 0)}"
        )
        label = entry.get("label")
        if isinstance(label, str) and label:
            line += f" · labeled {label}"
        output_fn(sanitize_tool_output(line))


def _print_cpfp_options(
    result: Mapping[str, object], output_fn: Callable[[str], None]
) -> None:
    """The THREE-option merge menu (TCK-CPFP-002 deliverable 2): the
    smallest/largest merge options carry their coin's value and stored
    label VERBATIM (print-only user data — answers route through the
    deterministic intercept, never the model), then the no-merge option
    (the plain child, consolidated into one fresh coin). Any other
    words set the menu aside (never-trap, the tail line)."""
    output_fn(sanitize_tool_output(_CPFP_OPTIONS_HEAD))
    options = result.get("options")
    if isinstance(options, list):
        for entry in options:
            if not isinstance(entry, dict):  # pragma: no cover — handler-shaped
                continue
            if entry.get("framing") == "plain":
                output_fn(
                    sanitize_tool_output(
                        f"  {entry.get('index')}. no merge — just the stuck "
                        "payment, consolidated into one fresh coin"
                    )
                )
                continue
            framing = str(entry.get("framing", "")) or "coin"
            value = entry.get("value_sats")
            value_part = (
                f"{value:,} sats"
                if isinstance(value, int) and not isinstance(value, bool)
                else "amount not recorded"
            )
            line = f"  {entry.get('index')}. merge your {framing} coin — {value_part}"
            label = entry.get("label")
            if isinstance(label, str) and label:
                line += f" · labeled {label}"
            output_fn(sanitize_tool_output(line))
    output_fn(sanitize_tool_output(_CPFP_OPTIONS_TAIL))


def _print_cpfp_plan(
    result: Mapping[str, object],
    output_fn: Callable[[str], None],
    *,
    session: SendSession | None = None,
) -> None:
    """Render a staged cpfp CHILD plan card (TCK-CPFP-002 deliverable 3).

    Same brief-card discipline as the bump/self-plan: the gate ask line
    byte-identical (the dual-key wording is nobody's to change), every
    figure verbatim from the handler result (the pure builder's plan),
    an absent/non-int numeric key rendering the fail-closed
    ``unavailable`` marker (TCK-SEC-004 class, never a fabricated 0).
    The framing row (``Child pays for parent``) and the COUNCIL MUST
    hedge ("spends a payment that hasn't confirmed yet — if that payment
    is undone, this won't send", the reorg/undo hedge) are pinned lines.
    The package row is honest in BOTH shapes: with the parent's recorded
    fee known, the builder's integer-DOWNED package-rate floor; with it
    unknown, the stated bound — never a fabricated package rate. The
    full ``/details`` render (To/Expires/Ref) caches on ``session`` like
    every other card."""
    lines: list[str] = [_CARD_ASK_LINE, _CPFP_CARD_HEAD, _CPFP_HEDGE_LINE]
    parent_txid = result.get("cpfp_parent_txid")
    if isinstance(parent_txid, str) and parent_txid:
        lines.append(_CPFP_PARENT_LINE.format(parent_txid=parent_txid))
    amount = _card_sats(result, "amount_sats")
    lines.append(f"Pay: {amount} sats — into 1 fresh coin of yours")
    fee_sats = _card_sats(result, "fee_sats")
    fee = "Fee: unavailable" if fee_sats is None else f"Fee: {fee_sats} sats"
    if fee_sats is not None:
        rate = _card_fee_rate_text(result)
        if rate is not None:
            fee += f" · {rate} sat/vB"
        vsize = _card_sats(result, "vsize")
        if vsize is not None:
            fee += f" × {vsize} vB"
        target_word = result.get("fee_target")
        if isinstance(target_word, str) and target_word:
            fee += f" · {target_word}"
        eta_wording = result.get("eta_wording")
        if isinstance(eta_wording, str) and eta_wording:
            # Verbatim chain/eta.py hedge appended — never re-punctuated.
            fee += f" — ETA {eta_wording}"
    lines.append(fee)
    package_rate = result.get("cpfp_package_fee_rate_centisat_vb")
    if result.get("cpfp_parent_fee_known") is True and isinstance(
        package_rate, int
    ) and not isinstance(package_rate, bool):
        # The builder's integer-DOWNED FLOOR of the combined effective
        # rate — "at least", verbatim from the plan (never a midpoint
        # claim, never a number this renderer computes).
        lines.append(
            _CPFP_PACKAGE_LINE.format(package_rate=format_sat_vb(package_rate))
        )
    else:
        lines.append(_CPFP_PACKAGE_UNKNOWN)
    if result.get("cpfp_merged") is True:
        framing = result.get("cpfp_merge_framing")
        merge_value = _card_sats(result, "cpfp_merge_value_sats")
        lines.append(
            f"+ merged with your {framing if isinstance(framing, str) and framing else 'coin'}"
            + (f" coin ({merge_value} sats)" if merge_value is not None else " coin")
        )
    sources = result.get("inputs_count")
    if isinstance(sources, int) and not isinstance(sources, bool):
        lines.append(f"From: your wallet ({sources:,} source{'s' if sources != 1 else ''})")
    else:
        lines.append("From: your wallet (sources unavailable)")
    lines.append(_CARD_DETAILS_TAIL)
    for line in lines:
        output_fn(sanitize_tool_output(line))
    if session is not None:
        # The /details full render (the brief card demotes To/Expires/Ref
        # — the destinations are the plan's fresh receive address,
        # verbatim, printed only here; the model never sees any of it).
        full = lines[:-1]
        dests = result.get("self_destinations")
        if isinstance(dests, list):
            for entry in dests:
                if not isinstance(entry, dict):
                    continue
                addr = entry.get("address")
                amt = entry.get("amount_sats")
                if not isinstance(addr, str) or not addr:
                    continue
                if isinstance(amt, int) and not isinstance(amt, bool):
                    full.append(f"To: {addr} ({amt} sats)")
                else:
                    full.append(f"To: {addr}")
        expires = result.get("expires_in_s")
        if isinstance(expires, int) and not isinstance(expires, bool):
            full.append(f"Expires: ~{expires // 60} min")
        ref = result.get("tx_ref")
        if isinstance(ref, str) and ref:
            # Value-free app-generated handle with its purpose (TCK-UX-014).
            full.append(
                f"Ref: {ref} — names this pending transaction if you ask to "
                "cancel or reprint it before it expires."
            )
        session.card_render = full


def _print_self_transfer(
    result: Mapping[str, object],
    output_fn: Callable[[str], None],
    *,
    session: SendSession | None = None,
) -> None:
    """Narrate a ``self_transfer`` outcome (TCK-TX-SELF-001; the ``cpfp``
    mode's conversation branch is TCK-CPFP-002).

    Success → the plan card (:func:`_print_self_plan`) — or, for a cpfp
    child, the child-pays-for-parent card (:func:`_print_cpfp_plan`); an
    open cpfp ASK (coin choice / merge menu) prints its numbered options
    with the store's verbatim fields (labels print-only, never model-
    routed — the bump ask's discipline). The honest
    value-free refusals print their dispatcher-owned lines;
    ``insufficient_funds`` reuses the friendly needed/have line (user-facing
    amounts, ADR-0012 — never a log-bound detail); ``tx_pending`` re-shows
    whatever plan/send currently pends (through the same shared renderers
    as ``create_tx`` — a staged cpfp child re-shows AS its cpfp card).
    Everything else goes through :func:`_error_line`.
    """
    error = result.get("error")
    if error is None and result.get("cpfp") is True:
        # TCK-CPFP-002: ask (coin choice / merge menu) or the staged
        # child's plan card — the cpfp conversation's own render family.
        ask = result.get("ask")
        if ask == "coin":
            _print_cpfp_coin_ask(result, output_fn)
        elif ask == "options":
            _print_cpfp_options(result, output_fn)
        else:
            _print_cpfp_plan(result, output_fn, session=session)
        return
    if error is None:
        _print_self_plan(result, output_fn, session)
        return
    if error == "insufficient_funds":
        output_fn(
            sanitize_tool_output(
                f"Insufficient funds: need {result.get('needed_sats', 0)} sats, "
                f"have {result.get('available_sats', 0)} sats."
            )
        )
        return
    if error == "self_nothing_below":
        output_fn(sanitize_tool_output(_SELF_NOTHING_BELOW))
        return
    if error == "self_split_below_dust":
        output_fn(sanitize_tool_output(_SELF_SPLIT_BELOW_DUST))
        return
    if error == "cpfp_unavailable":
        # TCK-CPFP-002: the session-less direct-wiring backstop's clean
        # value-free line, printed verbatim (never an error dump).
        output_fn(sanitize_tool_output(_CPFP_NOT_READY))
        return
    if error in (
        "cpfp_nothing_unconfirmed",
        "cpfp_already_confirmed",
        "cpfp_inbound_gone",
        "cpfp_coin_gone",
        "cpfp_cannot_fund",
        "cpfp_plan_failed",
        "cpfp_flow_busy",
        "cons_coin_gone",
    ):
        # The cpfp conversation's honest answers ARE the UX (the bump
        # refusals' precedent), plus the consolidation conversation's
        # mid-conversation recheck (TCK-CONS-001): code-owned friendly
        # lines, printed verbatim; fee-math refusals carry the
        # machine-readable reason as a STRUCTURED key only (their detail
        # strings never quote values — ADR-0012 §7).
        output_fn(sanitize_tool_output(str(result.get("detail", "")).strip()))
        return
    if error == "self_too_many_small":
        output_fn(sanitize_tool_output(_SELF_TOO_MANY_SMALL))
        return
    if error == "tx_pending":
        if result.get("cpfp") is True:
            # A staged cpfp child re-shown (TCK-CPFP-002): AS its cpfp
            # card (the carried display fields + the flow record's own
            # numbers) — never the misleading generic reshape.
            output_fn(sanitize_tool_output(_GUIDANCE_STILL_PENDING))
            _print_cpfp_plan(result, output_fn, session=session)
            return
        if result.get("self_transfer") is True:
            output_fn(sanitize_tool_output(_GUIDANCE_STILL_PENDING))
            _print_self_plan(result, output_fn, session)
            return
        notice = result.get("rate_notice")
        if notice == "ceiling":
            output_fn(sanitize_tool_output(_CARD_RATE_CEILING))
            return
        if notice == "floor":
            output_fn(sanitize_tool_output(_CARD_RATE_FLOOR))
            return
        output_fn(sanitize_tool_output(_GUIDANCE_STILL_PENDING))
        _print_brief_card(result, output_fn, session)
        return
    if error == "wallet_loading":
        output_fn(sanitize_tool_output(str(result.get("detail", "")) or WALLET_LOADING_REFUSAL))
        return
    output_fn(sanitize_tool_output(_error_line(result, "Could not prepare the coin reshuffle")))


def _print_create_tx(
    result: Mapping[str, object],
    output_fn: Callable[[str], None],
    *,
    session: SendSession | None = None,
) -> None:
    """Narrate a ``create_tx`` outcome (TCK-P2-004 card UX; TCK-UX-002
    brief redesign per docs/ux-tx-card-feedback.md §1/§2).

    Success → the BRIEF card (ask line + To/Pay/Fee/From + one conditional
    tail), with the full nine-line render cached on ``session`` for
    ``/details``. A re-quote (``fee_requote``) leads with
    ``card.requote_lead`` (§2.3) and renders variant B — the re-quote
    envelope carried an explicit ``fee_target``, so the offer is retired.
    ``tx_pending`` (a create at a DIFFERENT destination while one pends) →
    the still-pending guide line + the same brief card re-rendered from
    the result's own fields (variant B: the flow record cannot know the
    target was defaulted — the offer is a fresh-card one-shot, §2.0); a
    ``rate_notice`` on top (ceiling/floor, §2.3) prints just that line —
    the staged card stays valid on screen. ``insufficient_funds`` → the
    friendly line built from the structured ``needed_sats``/
    ``available_sats`` keys (user-facing amounts, per ADR-0012 — never a
    log-bound detail string); a re-quote that pushes the wallet short at
    the higher rung lands here and the ORIGINAL pending is intact
    (commit-only-on-success, §2.1). Everything else goes through
    :func:`_error_line` (value-free details).
    """
    error = result.get("error")
    if error == "insufficient_funds":
        output_fn(
            sanitize_tool_output(
                f"Insufficient funds: need {result.get('needed_sats', 0)} sats, "
                f"have {result.get('available_sats', 0)} sats."
            )
        )
        return
    if error == "tx_pending":
        notice = result.get("rate_notice")
        if notice == "ceiling":
            output_fn(sanitize_tool_output(_CARD_RATE_CEILING))
            return
        if notice == "floor":
            output_fn(sanitize_tool_output(_CARD_RATE_FLOOR))
            return
        output_fn(sanitize_tool_output(_GUIDANCE_STILL_PENDING))
        _print_brief_card(result, output_fn, session)
        return
    if error == "wallet_loading":
        # ADR-0022 decision 6: the pre-first-scan refusal is a friendly
        # dispatcher-owned line (value-free), not an error dump.
        output_fn(sanitize_tool_output(str(result.get("detail", "")) or WALLET_LOADING_REFUSAL))
        return
    if error is not None:
        output_fn(sanitize_tool_output(_error_line(result, "Could not create the transaction")))
        return
    if result.get("fee_requote"):
        direction = result.get("requote_direction")
        lead = (
            _CARD_REQUOTE_LEAD.replace("{direction}", str(direction))
            if isinstance(direction, str) and direction
            else _CARD_REQUOTE_LEAD_SAME_RUNG
        )
        output_fn(sanitize_tool_output(lead))
    _print_brief_card(result, output_fn, session)


def _card_sats(result: Mapping[str, object], key: str) -> str | None:
    """Thousands-separated sats value, or ``None`` when absent/not-an-int
    (the fail-closed rule: never a fabricated ``0`` — TCK-SEC-004 change 4
    class; the merged brief lines DROP an optional segment whose value is
    unavailable; the raw field survives in the /details full render)."""
    value = result.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return f"{value:,}"


def _card_fee_rate_text(result: Mapping[str, object]) -> str | None:
    """The fee-rate card segment (formatted sats/vB text like ``"1.21"``),
    or ``None`` when absent/not-a-string — the same fail-closed rule as
    :func:`_card_sats`: never a fabricated figure, drop the segment. The
    text is the chain-owned ``format_sat_vb`` of the tool result's integer
    centisat/vB rate, quoted verbatim (TCK-FEE-003 wave)."""
    value = result.get("fee_rate_display")
    return value if isinstance(value, str) and value else None


def _fee_floor_note_line(result: Mapping[str, object]) -> str | None:
    """The TCK-FEE-004 min-relay-floor note line, or ``None``. Present only
    when the handler marked THIS bid as raised to the floor
    (``fee_floor_note=True``, display-only) AND the card can quote the rate
    verbatim from its own ``fee_rate_display`` (the clamped bid IS the
    floor). Fail-closed like every card segment: no figure, no line — the
    floor is never narrated as a number the result does not carry.
    """
    if result.get("fee_floor_note") is not True:
        return None
    rate = _card_fee_rate_text(result)
    return None if rate is None else _CARD_FEE_FLOOR_NOTE.format(rate=rate)


def _card_rate(result: Mapping[str, object]) -> str | None:
    """Thousands-separated per-BTC rate, whole units when the source gave
    whole units (the price provider does — ADR-0011 §4), else 2 decimals.
    Reads the legacy USD key first, the currency-tagged ``fiat_per_btc``
    otherwise (TCK-FIAT-002). ``None`` when absent/not numeric
    (fail-closed: never a fabricated rate)."""
    value = result.get("btc_usd")
    if value is None:
        value = result.get("fiat_per_btc")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return f"{int(value):,}" if float(value).is_integer() else f"{value:,.2f}"


def _card_rate_segment(result: Mapping[str, object]) -> str | None:
    """The fresh-rate segment the fiat lines append: ``@ $97,000/BTC`` for
    the USD default (byte-identical to the TCK-FIAT-001 wording) or
    ``@ 8,900,000 JPY/BTC`` for a currency-tagged result (TCK-FIAT-002) —
    ``None`` when no rate figure is present (fail-closed)."""
    rate = _card_rate(result)
    if rate is None:
        return None
    currency = result.get("fiat_currency")
    if (
        isinstance(currency, str)
        and currency
        and currency != DEFAULT_DISPLAY_CURRENCY
    ):
        return f"@ {rate} {currency.upper()}/BTC"
    return f"@ ${rate}/BTC"


def _fiat_pair(result: Mapping[str, object]) -> tuple[int, str] | None:
    """The fiat display pair (minor-unit amount, currency code) from a
    handler result — values verbatim from tool output, fail-closed. The
    TCK-FIAT-002 key design: the currency-tagged ``fiat_total_minor`` +
    ``fiat_currency`` pair (a non-USD answer) is read first; the legacy
    USD keys (``usd_total_cents`` on the balance answer, ``usd_cents`` on
    the send card — the FIAT-001 shapes) resolve to ``("…", "usd")``.
    ``None`` (absent / not-an-int / null) renders no fiat line."""
    minor = result.get("fiat_total_minor")
    currency = result.get("fiat_currency")
    if (
        isinstance(minor, int)
        and not isinstance(minor, bool)
        and isinstance(currency, str)
        and currency
    ):
        return minor, currency
    for key in ("usd_total_cents", "usd_cents"):
        value = result.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value, DEFAULT_DISPLAY_CURRENCY
    return None


def _fee_fiat_pair(result: Mapping[str, object]) -> tuple[int, str] | None:
    """The Fee-line fiat pair (TCK-FIAT-003): the handler-supplied
    ``fee_fiat_minor`` in the card result's currency — the tagged
    ``fiat_currency`` when the answer carries one (FIAT-002 shape), the
    USD default otherwise (the same key design as :func:`_fiat_pair`).
    ``None`` when the key is absent (no rate → the sats-only Fee line
    exactly as today): fail-closed, never a fabricated conversion."""
    minor = result.get("fee_fiat_minor")
    if isinstance(minor, bool) or not isinstance(minor, int):
        return None
    currency = result.get("fiat_currency")
    if isinstance(currency, str) and currency:
        return minor, currency
    return minor, DEFAULT_DISPLAY_CURRENCY


def _fiat_text(minor: int, currency: str, *, grouped: bool) -> str:
    """Format a minor-unit fiat amount for display (the same display-only
    formatting class as ``_card_rate``'s thousands separation — the figure
    is the tool's, never recomputed). The USD default keeps its exact
    legacy shape (``$1,234.57`` with ``grouped``, ``$1234.56`` without —
    the balance line groups, the card segments historically do not); any
    other code LABELS itself (``1,234.56 EUR``, ``8,900,000 JPY`` — the
    zero-decimal shape comes from ``minor_per_unit``, not a guess). An
    out-of-enum code (only hand-forged tool data can produce one) keeps
    two decimals and still never claims a ``$``.
    """
    try:
        per = minor_per_unit(currency)
    except ValueError:
        per = 100
    whole, frac = divmod(minor, per)
    num = f"{whole:,}" if grouped else f"{whole}"
    if currency == DEFAULT_DISPLAY_CURRENCY:
        return f"${num}.{frac:02d}"
    label = currency.upper()
    if per == 100:
        return f"{num}.{frac:02d} {label}"
    return f"{num} {label}"


def _print_brief_card(
    result: Mapping[str, object],
    output_fn: Callable[[str], None],
    session: SendSession | None = None,
) -> None:
    """Render the brief default card (doc §1): ask line, then To / Pay /
    Fee / From, then ONE conditional tail — variant A (``card.offer``,
    when the envelope omitted ``fee_target``) or variant B
    (``card.details_tail``), never both. Four data lines + ask + tail, all
    drawn from the SAME handler result dict — nothing recomputed, nothing
    model-generated; the Fee line absorbs size + the verbatim ``chain/
    eta.py`` hedge, and Expires/Ref demote to the full ``/details`` view
    (the sign/broadcast-time re-prints carry the ref onward — §1 Ref row).
    The full classic nine-line render of the same result is cached on
    ``session`` for ``/details``.
    """
    # TCK-TX-SELF-001: a re-shown pending that is a self-transfer PLAN (the
    # user tried create_tx while a reshuffle pends) renders as its plan —
    # never as a single-recipient card that would misrepresent N outputs
    # as one destination. Checked BEFORE the full-render cache so the
    # misleading classic card is never what /details would reprint.
    if result.get("self_transfer") is True:
        _print_self_plan(result, output_fn, session)
        return
    if session is not None:
        full: list[str] = []
        _print_confirmation_card(result, full.append)
        session.card_render = full
    # The mix warning rides ABOVE the ask line (doc §4.3 slot table): an
    # unavoidable mix is a review-carefully moment, visible at a glance, and
    # the pending re-show (`tx_pending`, whose flow record carries no mix
    # flag) never contradicts the card the user is confirming against — the
    # line describes the FINAL selection of the run that rendered it.
    if result.get("mixed") is True:
        output_fn(sanitize_tool_output(_CARD_MIX_WARNING))
    output_fn(sanitize_tool_output(_CARD_ASK_LINE))
    output_fn(sanitize_tool_output(f"To: {result.get('recipient', '')}"))
    pay = "Pay: unavailable"
    amount = _card_sats(result, "amount_sats")
    if amount is not None:
        pay = f"Pay: {amount} sats"
        fiat = _fiat_pair(result)
        if fiat is not None:
            # TCK-FIAT-002: the Pay parenthetical follows the display
            # currency (figures verbatim from the result; USD renders the
            # exact legacy shape, a tagged currency labels itself).
            pay += f" ({_fiat_text(fiat[0], fiat[1], grouped=False)}"
            if result.get("rate_stale"):
                # Stale per the ADR-0011 ladder: surface WHY the number may
                # be off (age) instead of the (now-untrusted) rate figure.
                rate_age = result.get("rate_age_s")
                if rate_age is not None:
                    pay += f" · rate age {rate_age}s"
                pay += " · stale"
            else:
                segment = _card_rate_segment(result)
                if segment is not None:
                    pay += f" · {segment}"
            pay += ")"
    output_fn(sanitize_tool_output(pay))
    fee_sats = _card_sats(result, "fee_sats")
    fee = "Fee: unavailable" if fee_sats is None else f"Fee: {fee_sats} sats"
    if fee_sats is not None:
        rate = _card_fee_rate_text(result)
        if rate is not None:
            fee += f" · {rate} sat/vB"
        vsize = _card_sats(result, "vsize")
        if vsize is not None:
            fee += f" × {vsize} vB"
        fee_fiat = _fee_fiat_pair(result)
        if fee_fiat is not None:
            # TCK-FIAT-003: the fiat parenthetical on the send card's Fee
            # line (figures verbatim from the handler result, the same
            # rate the Pay segment carries; the stale marker mirrors the
            # Pay line's honesty). Absent key = sats-only as today.
            approx = _fiat_text(fee_fiat[0], fee_fiat[1], grouped=False)
            if result.get("rate_stale"):
                approx += " · stale"
            fee += f" (≈ {approx})"
        target_word = result.get("fee_target")
        if isinstance(target_word, str) and target_word:
            fee += f" · {target_word}"
        eta_wording = result.get("eta_wording")
        if isinstance(eta_wording, str) and eta_wording:
            # Verbatim chain/eta.py hedge appended — never re-punctuated.
            fee += f" — ETA {eta_wording}"
    output_fn(sanitize_tool_output(fee))
    floor_note = _fee_floor_note_line(result)
    if floor_note is not None:
        # TCK-FEE-004: one honest min-relay-floor line, under the Fee data
        # line (the card that raised its bid says so once). Absent unless
        # the handler marked this bid floor-raised.
        output_fn(sanitize_tool_output(floor_note))
    sources = result.get("inputs_count")
    if isinstance(sources, int) and not isinstance(sources, bool):
        from_line = f"From: your wallet ({sources:,} {'source' if sources == 1 else 'sources'})"
    else:
        from_line = "From: your wallet (sources unavailable)"
    change = _card_sats(result, "change_sats")
    if change is not None:
        from_line += f" · {change} sats come back as change"
    # Consolidation clause (TCK-UTXO-004, doc §2.2/§4.3): a step-5 fold
    # appends to the From data line — a source-of-funds FACT, names no verb,
    # presents no choice (the tail slot stays the speed offer's). Count
    # only; the clause renders iff folded_count > 0 (absent key or 0 = the
    # step did not fire, and a re-show that cannot know says nothing rather
    # than lying). Plain words: no "consolidation"/"UTXO" jargon.
    folded = result.get("folded_count")
    if isinstance(folded, int) and not isinstance(folded, bool) and folded > 0:
        from_line += f" · folding in {folded:,} small ones now to save fees later"
    output_fn(sanitize_tool_output(from_line))
    if result.get("fee_target_defaulted"):
        output_fn(sanitize_tool_output(_CARD_OFFER_TAIL + _CARD_DETAILS_TAIL))
    else:
        output_fn(sanitize_tool_output(_CARD_DETAILS_TAIL))


def _print_confirmation_card(
    result: Mapping[str, object], output_fn: Callable[[str], None]
) -> None:
    """Render the pending-transaction confirmation card.

    Every value is verbatim from the handler result dict — the renderer
    only formats (USD cents → dollars, TTL seconds → minutes, the same
    display-only class as txid truncation). The recipient is quoted ONLY
    from ``result["recipient"]`` (tool-output verbatim rule); the fiat
    segment appears only when the handler supplied fiat keys (legacy
    ``usd_cents`` or the TCK-FIAT-002 tagged trio — :func:`_fiat_pair`),
    with the rate age and the stale marker when present.

    Absent numeric keys render an explicit ``unavailable`` marker — never a
    fabricated value (SR-006 class, TCK-SEC-004 change 4; the same failure
    class removed for ``tip_height`` in :func:`_print_balance`): a missing
    ``amount_sats``/``fee_sats``/``vsize``/``inputs_count``/``expires_in_s``
    must not print as "0".
    """
    if "amount_sats" in result:
        amount_line = f"Amount: {result['amount_sats']} sats"
    else:
        amount_line = "Amount: unavailable"
    fiat = _fiat_pair(result)
    if fiat is not None:
        amount_line += f" ({_fiat_text(fiat[0], fiat[1], grouped=False)}"
        if result.get("rate_stale"):
            # Stale per the ADR-0011 ladder: surface WHY the number may be
            # off (age) instead of the (now-untrusted) rate figure.
            rate_age = result.get("rate_age_s")
            if rate_age is not None:
                amount_line += f" · rate age {rate_age}s"
            amount_line += " · stale"
        else:
            segment = _card_rate_segment(result)
            if segment is not None:
                amount_line += f" · {segment}"
        amount_line += ")"
    output_fn(sanitize_tool_output(amount_line))
    output_fn(sanitize_tool_output(f"To: {result.get('recipient', '')}"))
    if "fee_sats" in result:
        fee_line = f"Fee: {result['fee_sats']} sats"
        fee_parts: list[str] = []
        fee_rate_text = _card_fee_rate_text(result)
        if fee_rate_text is not None:
            fee_parts.append(f"{fee_rate_text} sat/vB")
        # None/absent ⇒ no segment: an explicit-rate record carries no rung
        # (TCK-FEE-002) — never render "None target".
        target_word = result.get("fee_target")
        if isinstance(target_word, str) and target_word:
            fee_parts.append(f"{target_word} target")
        if fee_parts:
            fee_line += f" ({', '.join(fee_parts)})"
    else:
        fee_line = "Fee: unavailable"
    output_fn(sanitize_tool_output(fee_line))
    floor_note = _fee_floor_note_line(result)  # TCK-FEE-004 (see brief card)
    if floor_note is not None:
        output_fn(sanitize_tool_output(floor_note))
    if "vsize" in result:
        output_fn(sanitize_tool_output(f"Size: {result['vsize']} vB"))
    else:
        output_fn(sanitize_tool_output("Size: unavailable"))
    if "inputs_count" in result:
        output_fn(sanitize_tool_output(f"Inputs: {result['inputs_count']}"))
    else:
        output_fn(sanitize_tool_output("Inputs: unavailable"))
    change = result.get("change_sats")
    change_label = f"Change: {change} sats" if change is not None else "Change: none"
    output_fn(sanitize_tool_output(change_label))
    if "expires_in_s" in result:
        output_fn(sanitize_tool_output(f"Expires: ~{int(result['expires_in_s']) // 60} min"))
    else:
        output_fn(sanitize_tool_output("Expires: unavailable"))
    eta_wording = result.get("eta_wording")
    if isinstance(eta_wording, str) and eta_wording:
        output_fn(sanitize_tool_output(f"ETA: {eta_wording}"))
    ref = result.get("tx_ref", "")
    # The ref is an app-generated handle for the pending transaction (cancel
    # / requote / reprint before expiry) — value-free, presented with its
    # purpose (TCK-UX-014).
    ref_line = f"Ref: {ref}" if ref else "Ref: unavailable"
    output_fn(
        sanitize_tool_output(
            ref_line
            + " — names this pending transaction if you ask to cancel or "
            "reprint it before it expires."
        )
    )


def _print_confirm_tx(result: Mapping[str, object], output_fn: Callable[[str], None]) -> None:
    """Narrate a ``confirm_tx`` outcome.

    A refusal is the UX: the flow's own value-free message is surfaced
    verbatim ("pending transaction expired", "confirmation gate not
    satisfied…", "tx_ref does not match…"). SUCCESS prints nothing: the
    GATE-MERGE (TCK-UX-002, ADR-0013 amendment) chains the device handoff
    in the same turn, and its §10 narration (export line / guidance /
    signed-verified line — the sign-time re-print of the ref-derived
    filename, on which the card's Ref demotion leans) is what the user
    reads; the old "Approved. Next step: sign" seam line is retired
    (doc §3). The PSBT payload from the result is never printed.
    """
    error = result.get("error")
    if error == "confirm_refused":
        detail = str(result.get("detail", "")).strip()
        message = f"Not confirmed — {detail}." if detail else "Not confirmed."
        output_fn(sanitize_tool_output(message))
        return
    if error is not None:
        output_fn(sanitize_tool_output(_error_line(result, "Could not confirm the transaction")))
        return
    if result.get("status") == "confirmed":
        # The chained sign handoff narrates this turn (_run_turn).
        return
    output_fn(sanitize_tool_output(_GENERIC_FAILURE))  # pragma: no cover — handler-shaped


def _print_sign_tx(result: Mapping[str, object], output_fn: Callable[[str], None]) -> None:
    """Narrate a ``sign_tx`` outcome (TCK-P3-005 device-handoff UX, §10).

    - ``signed_file_missing`` → the file-path handoff line: the export
      path and the EXPECTED signed filename, verbatim from the handler
      result (tool output — the path comes from ``FilePsbtSigner``).
    - ``device_error`` → the §10 guidance string verbatim (code-owned
      text from the signer error hierarchy — never model-generated).
    - ``revalidation_failed`` → the hard-stop line with the value-free
      detail from the re-validation gate; the tx hex never appears.
    - ``sign_refused`` / other errors → value-free refusal/failure lines.
    - success → "Signed and verified ✓ txid <txid>. Ready to broadcast —
      say 'broadcast'." with the txid verbatim from the re-validated
      result; the PSBT payload is never printed. For a file-signer import
      without a checksum sidecar, the ADR-0014 integrity note is added.
    """
    error = result.get("error")
    if error == "signed_file_missing":
        output_fn(
            sanitize_tool_output(
                f"Exported to {result.get('unsigned_path', '')}. Move it to your "
                f"SD card, sign on your device, then save the signed file back "
                f"and tell me the path (say: signed {result.get('signed_filename', '')})."
            )
        )
        return
    if error == "device_error":
        output_fn(sanitize_tool_output(str(result.get("guidance", "")).strip()))
        return
    if error == "revalidation_failed":
        detail = str(result.get("detail", "")).strip()
        message = "The signed transaction failed verification"
        if detail:
            message += f" ({detail})"
        message += " — nothing was signed or sent; try signing again."
        output_fn(sanitize_tool_output(message))
        return
    if error == "sign_refused":
        detail = str(result.get("detail", "")).strip()
        message = f"Not signed — {detail}." if detail else "Not signed."
        output_fn(sanitize_tool_output(message))
        return
    if error is not None:
        output_fn(
            sanitize_tool_output(_error_line(result, "Could not sign the transaction"))
        )
        return
    if result.get("status") == "signed":
        output_fn(
            sanitize_tool_output(
                f"Signed and verified ✓ txid {result.get('txid', '')}. "
                f"Ready to broadcast — say 'broadcast'."
            )
        )
        if result.get("signer_name") == "file" and not result.get("checksum_verified"):
            output_fn(
                sanitize_tool_output(
                    "Note: the signed file had no checksum sidecar — integrity not verified."
                )
            )
        return
    output_fn(sanitize_tool_output(_GENERIC_FAILURE))  # pragma: no cover — handler-shaped


def _print_broadcast_tx(
    result: Mapping[str, object],
    output_fn: Callable[[str], None],
    *,
    session: SendSession | None = None,
) -> None:
    """Narrate a ``broadcast_tx`` outcome (TCK-P3-005).

    Success → "Sent! txid <txid> — tracking…" with the txid verbatim from
    the chain response. ``broadcast_failed`` keeps the signed transaction
    and says so (single-attempt POST policy: retrying is explicit).
    Refusals and other errors surface value-free; a ``store_warning`` is
    printed after the success line (bookkeeping failed, broadcast didn't).
    The tx hex never appears in any line.

    A success also carries the TCK-UTXO-001 capture state (design doc §1.2):
    the txid becomes ``session.last_broadcast_txid`` (what ``/label last``
    resolves to), and ONE static, code-owned hint line
    (``card.broadcast_hint``) prints — never narration, never a gate input
    (the flow is terminal BROADCAST here, no gate armed, so the hint is
    unanswerable by a gate word), never repeated for the same tx within the
    session. ``session=None`` (direct-call tests) renders without the hint.
    """
    error = result.get("error")
    if error == "broadcast_refused":
        detail = str(result.get("detail", "")).strip()
        message = f"Not broadcast — {detail}." if detail else "Not broadcast."
        output_fn(sanitize_tool_output(message))
        return
    if error == "cpfp_parent_gone":
        # TCK-CPFP-002 deliverable 5: the hurried parent is PROVEN gone —
        # the child can never spend. The honest line replaces the kept-
        # for-retry wording ENTIRELY (no useless retry pitch against a
        # dead parent); nothing left is sent.
        output_fn(sanitize_tool_output(_CPFP_PARENT_GONE))
        return
    if error == "broadcast_failed":
        detail = str(result.get("detail", "")).strip()
        message = "Broadcast failed"
        if detail:
            message += f" ({detail})"
        message += " — the signed transaction is kept; say 'broadcast' to retry."
        output_fn(sanitize_tool_output(message))
        return
    if error is not None:
        output_fn(
            sanitize_tool_output(
                _error_line(result, "Could not broadcast the transaction")
            )
        )
        return
    if result.get("status") == "broadcast":
        txid = str(result.get("txid", ""))
        output_fn(sanitize_tool_output(f"Sent! txid {txid} — tracking…"))
        # TCK-RBF-004 supersede narration (ONLY on a broadcast that linked
        # the lineage — commit-only-on-success): the same BIP-125 hedge
        # wording the plan card and the status answer carry.
        replaces = result.get("replaces_txid")
        if isinstance(replaces, str) and replaces:
            output_fn(
                sanitize_tool_output(_BUMP_SUPERSEDE_LINE.format(old_txid=replaces))
            )
        warning = result.get("store_warning")
        if isinstance(warning, str) and warning.strip():
            output_fn(sanitize_tool_output(f"warning: {warning}"))
        if session is not None and txid:
            session.last_broadcast_txid = txid
            if session.label_hint_txid != txid:
                session.label_hint_txid = txid
                output_fn(_LABEL_BROADCAST_HINT)
        return
    output_fn(sanitize_tool_output(_GENERIC_FAILURE))  # pragma: no cover — handler-shaped


def _print_tx_status(result: Mapping[str, object], output_fn: Callable[[str], None]) -> None:
    """Narrate a ``tx_status`` outcome (TCK-P3-005).

    Confirmed → "Confirmed at height N." (N verbatim from the chain
    response); unconfirmed → "In mempool (unconfirmed).";
    ``unknown_tx`` → the eventual-consistency note; ``backend_unchosen`` →
    :data:`NO_BACKEND_REFUSAL` verbatim (TCK-PRIVACY-001); other errors
    surface value-free via :func:`_error_line`.

    TCK-RBF-005 lineage shapes (handler results, not errors) print the
    store's recorded values verbatim: ``replaced`` → "replaced by <new
    txid>" with the replacement's height when the scan has proven it, the
    BIP-125 hedge otherwise; ``evicted`` → the honest bump-lost line with
    the confirmed original's txid and height. The UI computes nothing.
    """
    error = result.get("error")
    if error == "unknown_tx":
        output_fn(
            sanitize_tool_output(
                "Transaction not found on the chain yet — it may not be indexed; "
                "try again in a moment."
            )
        )
        return
    lineage = result.get("lineage")
    if lineage == "replaced":
        replaced_by = result.get("replaced_by")
        height = result.get("replacement_height")
        if height is not None:
            output_fn(
                sanitize_tool_output(
                    f"It was replaced by {replaced_by} — the replacement confirmed "
                    f"at height {height}."
                )
            )
        else:
            output_fn(
                sanitize_tool_output(
                    f"It was replaced by {replaced_by} — the original may still "
                    "confirm; only one of these two ever will."
                )
            )
        return
    if lineage == "evicted":
        output_fn(
            sanitize_tool_output(
                f"It never confirmed — it was the fee bump, and the original it "
                f"replaced went through instead ({result.get('original_txid')} "
                f"at height {result.get('original_height')})."
            )
        )
        return
    if error == "backend_unchosen":
        # TCK-PRIVACY-001: the dispatcher-owned refusal prints verbatim
        # (the same style as the create_tx wallet_loading line) — never
        # the raw error code, never a chain-failure wording.
        output_fn(
            sanitize_tool_output(str(result.get("detail", "")) or NO_BACKEND_REFUSAL)
        )
        return
    if error is not None:
        output_fn(
            sanitize_tool_output(_error_line(result, "Status lookup failed"))
        )
        return
    if result.get("confirmed"):
        height = result.get("block_height")
        message = (
            f"Confirmed at height {height}."
            if height is not None
            else "Confirmed."
        )
        output_fn(sanitize_tool_output(message))
        return
    output_fn(sanitize_tool_output("In mempool (unconfirmed)."))


def _print_node_status(result: Mapping[str, object], output_fn: Callable[[str], None]) -> None:
    """Narrate a ``node_status`` outcome from the dispatcher-owned FACTS.

    Every printed value comes verbatim from the handler result dict (the
    advise-only detection + doctor content) — the UI computes nothing and
    the narration never includes cookie contents or credential material.
    """
    if result.get("error") is not None:
        output_fn(sanitize_tool_output(_error_line(result, "Node status lookup failed")))
        return
    backend = result.get("backend_mode")
    if backend == PRIVACY_MODE_AWAITING_BACKEND:
        # TCK-DESCOPE-M3A: no silent default means no server to name — the
        # honest unchosen answer, never the public-leak line for a server
        # the user never picked.
        output_fn(sanitize_tool_output(_NODE_STATUS_UNCHOSEN))
    elif backend == BACKEND_MODE_OWN_NODE_LOCAL:
        output_fn(sanitize_tool_output(_NODE_STATUS_OWN_NODE_LOCAL))
    elif backend == BACKEND_MODE_OWN_NODE_REMOTE:
        # Same host insertion as the banner (TCK-UX-009 lockstep); generic
        # wording when the handler found no host to name.
        host = result.get("backend_host")
        if isinstance(host, str) and host:
            output_fn(sanitize_tool_output(_NODE_STATUS_OWN_NODE_REMOTE.format(host)))
        else:
            output_fn(sanitize_tool_output(_NODE_STATUS_OWN_NODE_REMOTE_GENERIC))
    else:
        output_fn(sanitize_tool_output(_NODE_STATUS_PUBLIC))

    detection_state = result.get("detection_state")
    if detection_state == NODE_STATUS_DETECTION_DISABLED:
        output_fn(sanitize_tool_output("Local node detection is disabled (LOCALWALLET_NODE_DETECTION_ENABLED=0) — nothing was probed."))
        return
    if detection_state == "unavailable":
        output_fn(sanitize_tool_output("Local node detection could not run this time."))
        return

    core_reachable = bool(result.get("core_reachable"))
    if core_reachable:
        if result.get("core_synced"):
            output_fn(sanitize_tool_output("A Bitcoin Core node is reachable and synced."))
        else:
            output_fn(sanitize_tool_output("A Bitcoin Core node is reachable but still syncing."))
    else:
        output_fn(sanitize_tool_output("No Bitcoin Core node detected."))
    indexers = []
    if result.get("mempool_reachable"):
        indexers.append("mempool")
    if result.get("electrs_reachable"):
        indexers.append("electrs")
    if indexers:
        output_fn(sanitize_tool_output(f"Indexer reachable: {', '.join(indexers)}."))
    else:
        output_fn(sanitize_tool_output("No local indexer (mempool/electrs) detected."))

    headline = str(result.get("doctor_headline", "")).strip()
    if headline:
        output_fn(sanitize_tool_output(f"Doctor: {headline}"))
    next_step = str(result.get("doctor_next_step", "")).strip()
    if next_step:
        output_fn(sanitize_tool_output(f"Guidance: {next_step}"))

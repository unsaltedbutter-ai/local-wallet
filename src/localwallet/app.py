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
  the explorer's confirmation status for a verbatim txid.
- :func:`run` / :func:`main` — CLI wiring: read the watch-only key from
  ``--zpub`` or ``LOCALWALLET_ZPUB``, parse + gate it (mainnet-only,
  value-free errors → config-error exit 2); on an interactive first
  launch with no key supplied, the ADR-0023 onboarding greeting asks for
  it instead (:func:`localwallet.ui.onboarding.ask_watch_key`). Then open
  the store (:class:`~localwallet.config.Settings` ``store_path``),
  inject the stored backend rung (ADR-0023 decision 3:
  ``env > config file > stored > public default``, resolved through
  :func:`localwallet.config.resolve_chain_base_url` — the ONE place the
  stored choice enters), reuse or create the single wallet profile
  (descriptor-match guard, ADR-0010), pick the model runtime (remote
  debug bridge → local GGUF → ``--stub-llm``), start the NON-BLOCKING
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
import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from string import punctuation
from typing import TYPE_CHECKING, Any, Final

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
    ChainError,
    ConfigDisabled,
    EsploraClient,
    FeeEstimator,
    FeeTarget,
    IncomingEvent,
    IncomingWatcher,
    PriceOracle,
    PriceUnavailableError,
    WatchedTx,
    check_backend,
    estimate_eta,
    time_since_last_block,
)
from localwallet.config import Settings, resolve_chain_base_url
from localwallet.node import LocalNodeReport, NodeStatus, detect_local_nodes
from localwallet.node.doctor import NodeDoctor
from localwallet.protocol import (
    BroadcastTxParams,
    ClarifyParams,
    ConfirmTxParams,
    CreateTxParams,
    DispatchTable,
    Envelope,
    GetHistoryParams,
    Handler,
    IntentName,
    NewAddressParams,
    NodeStatusParams,
    RespondParams,
    SignTxParams,
    TxStatusParams,
)
from localwallet.signer.base import Signer, SignerError
from localwallet.signer.file import FilePsbtSigner
from localwallet.signer.hwi import DeviceError, HwiUsbSigner
from localwallet.store import (
    ADDRESS_ALLOCATED,
    BRANCH_CHANGE,
    COIN_NOTE_MAX_CHARS,
    COIN_TAGS,
    DIR_IN,
    DIR_OUT,
    DIR_SELF,
    AddressRecord,
    Store,
    StoreError,
    TxRecord,
    UtxoRecord,
    WalletRecord,
)
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
from localwallet.tx.revalidate import (
    IntendedTx,
    TamperedPsbtError,
    revalidate_signed_psbt,
)
from localwallet.tx.selection import InsufficientFundsError, SelectionError, select_coins
from localwallet.ui.onboarding import (
    BACKEND_CHOICE_PUBLIC,
    BACKEND_CHOICE_SETTING,
    WEB_SETUP_HINT,
    OnboardingFlow,
    ask_watch_key,
)
from localwallet.wallet import scan as wallet_scan
from localwallet.wallet.derivation import BranchDeriver
from localwallet.wallet.descriptor import (
    MAINNET_COIN_TYPE,
    SCRIPT_PURPOSES,
    ParsedKey,
    WalletDescriptor,
    WatchKeyError,
    parse_wallet_key,
)

if TYPE_CHECKING:  # circular at runtime: ui.web.server imports this module
    from localwallet.ui.web.server import WebServer

__all__ = [
    "AUTO_SCAN_ENV_VAR",
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_SIGNER_DIR",
    "GAP_LIMIT_ENV_VAR",
    "NODE_STATUS_DETECTION_DISABLED",
    "OUT_OF_WINDOW_NOTICE",
    "PRIVACY_INDICATOR",
    "PRIVACY_INDICATOR_OWN_NODE_LOCAL",
    "PRIVACY_INDICATOR_OWN_NODE_REMOTE",
    "SIGNER_DIR_ENV_VAR",
    "SIGNER_ENV_VAR",
    "UI_ENV_VAR",
    "ZPUB_ENV_VAR",
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

#: Environment variable opting out of the startup scan (``"0"`` disables;
#: any other value — including unset — keeps the default on).
AUTO_SCAN_ENV_VAR: Final[str] = "LOCALWALLET_AUTO_SCAN"

#: Environment variable selecting the UI transport (TCK-WEB-002, ADR-0024
#: §1/§11): ``"web"`` serves the opt-in localhost web UI instead of the
#: REPL. The CLI default is unchanged (unset/any other value = REPL); the
#: ``--web`` flag overrides this.
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
#: §9 / R7 — never over-claim privacy while querying a public explorer).
#: This is the PUBLIC-API wording (3-state banner, TCK-SEC-004 change 5):
#: when the chain backend is the user's own node the banner instead shows
#: :data:`PRIVACY_INDICATOR_OWN_NODE_LOCAL` (loopback host) or
#: :data:`PRIVACY_INDICATOR_OWN_NODE_REMOTE` (any other configured host),
#: selected by :func:`privacy_indicator` off the same single selection
#: point the chain client uses.
PRIVACY_INDICATOR: Final[str] = (
    "Querying public mempool.space — the operator can associate queried "
    "addresses with your IP."
)

#: The §9 privacy indicator for a self-hosted backend on THIS machine
#: (ADR-0018: ``Settings.chain_base_url`` set to a loopback host ⇒ all
#: chain lookups go to the user's own node, none to the public default).
PRIVACY_INDICATOR_OWN_NODE_LOCAL: Final[str] = (
    "Querying your own node on this machine — addresses and lookups stay here."
)

#: The §9 privacy indicator for a self-hosted backend on ANOTHER machine
#: (LAN/VPS instance): still the user's own node — but the R7 no-over-claim
#: rule forbids saying lookups "stay on this machine".
PRIVACY_INDICATOR_OWN_NODE_REMOTE: Final[str] = (
    "Querying your own node on another machine — nothing goes to a public API."
)

#: The 3-way chain-backend privacy mode (TCK-SEC-004 change 5), returned by
#: :func:`_backend_mode` and carried verbatim in the ``node_status`` FACTS.
BACKEND_MODE_PUBLIC: Final[str] = "public"
BACKEND_MODE_OWN_NODE_LOCAL: Final[str] = "own_node_local"
BACKEND_MODE_OWN_NODE_REMOTE: Final[str] = "own_node_remote"

#: Hosts that count as "the user's own node on this machine" — mirrors the
#: intent of ``node/detect.py`` ``_LOOPBACK_HOSTS`` (that helper cannot be
#: imported with its httpx dependency into this network-import-free module,
#: so the set is mirrored here; keep the two in sync).
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})

#: The ``node_status`` narration predicates (TCK-SEC-004 change 5, approved
#: copy) — "You are " + one of these, mirroring the banner's 3-way split.
#: The public wording is unchanged from the pre-change narration.
_NODE_STATUS_PUBLIC: Final[str] = (
    "You are querying the public API — the operator can associate queried "
    "addresses with your IP."
)
_NODE_STATUS_OWN_NODE_LOCAL: Final[str] = (
    "You are querying your own node on this machine — addresses and lookups "
    "stay here."
)
_NODE_STATUS_OWN_NODE_REMOTE: Final[str] = (
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


def privacy_indicator(settings: Settings) -> str:
    """Return the §9 privacy banner for the given backend selection.

    The wording is gated on the SAME single selection point the chain
    client uses (ADR-0018 ``ChainConfig.from_settings``) and classifies the
    configured backend three ways (TCK-SEC-004 change 5): the public
    default, the user's own node on this machine (loopback host), or the
    user's own node on another machine. The 3-way split keeps the banner
    honest for a remote LAN/VPS instance — "addresses and lookups stay on
    this machine" would over-claim there (R7). Reading the knob here —
    rather than re-hardcoding a public default — keeps the banner and the
    node_status narration from ever diverging from what the client actually
    uses.
    """
    mode = _backend_mode(settings)
    if mode == BACKEND_MODE_OWN_NODE_LOCAL:
        return PRIVACY_INDICATOR_OWN_NODE_LOCAL
    if mode == BACKEND_MODE_OWN_NODE_REMOTE:
        return PRIVACY_INDICATOR_OWN_NODE_REMOTE
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
_CANCELLED_LINE: Final[str] = "Transaction cancelled."
_GUIDANCE_STILL_PENDING: Final[str] = (
    'Still pending — say "sign" to send it to your device, or "cancel" '
    "to discard it."
)
_GUIDANCE_AMBIGUOUS: Final[str] = (
    'That was ambiguous — say "sign" to proceed with the pending '
    "transaction, or \"cancel\" to discard it."
)


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
    """

    gate_decision: GateDecision = GateDecision.NOT_A_DECISION
    card_render: list[str] | None = None
    last_broadcast_txid: str | None = None
    label_hint_txid: str | None = None


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
    "transaction" → ``get_history``; "utxo" → ``get_utxos``; "new
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
    if "history" in user_turn or "transaction" in user_turn:
        return _STUB_HISTORY_ENVELOPE
    if "utxo" in user_turn:
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
    client: EsploraClient,
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
        flow: The dispatcher-owned send-flow state machine (TCK-P2-004).
            Defaults to a fresh :class:`TxFlow` with the real clock and
            uuid id factory; the REPL and tests share ONE instance.
        session: The per-turn gate-decision carrier (dual-key rule,
            ADR-0013). Defaults to a fresh :class:`SendSession`.
        fee_estimator: Fee-rate source for ``create_tx``; defaults to a
            :class:`FeeEstimator` over ``client``.
        price_oracle: USD/BTC rate source for ``create_tx``; defaults to
            a :class:`PriceOracle` over ``client``.
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
    return {
        IntentName.RESPOND: _respond_handler,
        IntentName.CLARIFY: _clarify_handler,
        IntentName.GET_BALANCE: _make_get_balance_handler(
            store, wallet_id, scan_fn, scan_gate
        ),
        IntentName.GET_HISTORY: _make_get_history_handler(store, wallet_id, scan_gate),
        IntentName.GET_UTXOS: _make_get_utxos_handler(store, wallet_id, scan_gate),
        IntentName.NEW_ADDRESS: _make_new_address_handler(store, wallet_id, parsed),
        IntentName.CREATE_TX: _make_create_tx_handler(
            store,
            wallet_id,
            parsed,
            tx_flow,
            fee_estimator if fee_estimator is not None else FeeEstimator(client),
            price_oracle if price_oracle is not None else PriceOracle(client),
            scan_fn,
            seconds_since_last_block_fn=seconds_since_last_block_fn,
            scan_gate=scan_gate,
        ),
        IntentName.CONFIRM_TX: _make_confirm_tx_handler(tx_flow, send_session),
        IntentName.SIGN_TX: _make_sign_tx_handler(
            tx_flow, signer_selection, signer, store, wallet_id, parsed
        ),
        IntentName.BROADCAST_TX: _make_broadcast_tx_handler(
            tx_flow, client, store, wallet_id
        ),
        IntentName.TX_STATUS: _make_tx_status_handler(client, tx_flow),
        IntentName.NODE_STATUS: _make_node_status_handler(
            app_settings,
            node_detect_fn=node_detect_fn,
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
) -> Handler:
    """Create the ``get_balance`` handler closed over the store.

    Sums the cached UTXO snapshot (confirmed/unconfirmed split) and
    reports the count of addresses holding UTXOs plus the tip height
    recorded by the last scan. If the wallet has never scanned (no sync
    cursor) AND no startup scan is in flight, ``scan_fn`` runs once
    lazily first — this keeps the Phase 0 AC ("What's my balance?"
    returns a correct live balance) working when the startup scan is
    opted out via :data:`AUTO_SCAN_ENV_VAR`. While the non-blocking
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
    """

    def handler(envelope: Envelope) -> dict[str, object]:
        del envelope  # get_balance params are empty by schema
        try:
            in_flight = scan_gate is not None and scan_gate.in_progress
            if (
                store.get_sync_state(wallet_id, wallet_scan.CURSOR_KEY) is None
                and not in_flight
            ):
                try:
                    scan_fn()
                except (ChainError, wallet_scan.ScanError, WatchKeyError) as exc:
                    # detail is scrubbed by the chain/scan layers (value-free
                    # of addresses/txids/amounts) — safe to surface verbatim.
                    return {"error": "chain_unavailable", "detail": str(exc)}
            utxos = store.get_utxos_for_wallet(wallet_id)
            tip_raw = store.get_sync_state(wallet_id, wallet_scan.TIP_KEY)
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)
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
        if tip_raw is not None:
            try:
                tip = int(tip_raw)
            except ValueError:
                tip = -1  # malformed cursor: omit rather than fabricate
            if tip >= 0:
                result["tip_height"] = tip
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
    withheld). No network I/O.
    """

    def handler(envelope: Envelope) -> dict[str, object]:
        del envelope  # get_utxos params are empty by schema
        try:
            records = store.get_utxos_for_wallet(wallet_id)
            freshness = _freshness(store, wallet_id, scan_gate)
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)
        utxos = [
            {
                "txid": r.txid,
                "vout": r.vout,
                "address": r.address,
                "value_sats": r.value_sats,
                "confirmed": bool(r.confirmed),
            }
            for r in records
        ]
        return {"utxos": utxos, "count": len(utxos), "freshness": freshness}

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
        except (StoreError, sqlite3.Error) as exc:
            return _store_error(exc)
        return {"address": address, "branch": branch, "index": index}

    return handler


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
    facts: dict[str, object] = {
        "pending_tx_ref": pending.tx_ref,
        "pending_tx_amount_sats": pending.amount_sats,
        "pending_tx_recipient": pending.recipient,
        "pending_tx_expires_in_s": _pending_remaining_s(flow),
    }
    eta = _eta_for(pending.fee_target, seconds_since_last_block_fn=seconds_since_last_block_fn)
    if eta is not None:
        facts["pending_tx_eta_minutes"] = eta["eta_minutes"]
        facts["pending_tx_eta_wording"] = eta["eta_wording"]
    return facts


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
                "fee_rate_sat_vb": pending.fee_rate_sat_vb,
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
       ``amount_usd`` requires the price oracle (:meth:`PriceOracle.fresh`).
       A price failure on the USD path refuses the whole request with
       ``{"error": "price_unavailable", ...}`` and NO flow entry (the
       user retries, or gives sats). On the sats path the oracle is
       consulted best-effort for the card's USD display only — a failure
       there degrades to ``usd_cents=None`` and never blocks the send.
       A stale-but-served rate (ADR-0011 ladder) is marked ``rate_stale``
       with its age; the rate's fetch timestamp is included either way.
    3. Fee rate: the two fee knobs are mutually exclusive at the schema
       layer (ADR-0012 amendment, TCK-FEE-002). An explicit
       ``fee_rate_sat_vb`` is used VERBATIM as the bid — no estimator call,
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
            and params.recipient == staged.recipient
            and params.amount_sats is not None
            and params.amount_sats == staged.amount_sats
        )
        if staged is not None and not requote:
            return _tx_pending_result(
                flow, seconds_since_last_block_fn=seconds_since_last_block_fn
            )

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

        # 2. Amount resolution (sats direct; USD via the price oracle).
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

        usd_cents = price_oracle.sats_to_usd(amount_sats, rate) if rate is not None else None
        rate_stale = rate.stale if rate is not None else False
        rate_age_s = int(rate.age_s()) if rate is not None else None
        rate_fetched_at = rate.fetched_at if rate is not None else None
        btc_usd = rate.usd_per_btc if rate is not None else None

        # 3 (cont.). Fee rate: the literal user-quoted sat/vB rate when
        # present (no estimator call — the user's number is quoted verbatim
        # and is the whole point of the ceiling-ask answer), else the ladder
        # (MEDIUM default when neither knob is given).
        if params.fee_rate_sat_vb is not None:
            fee_rate = params.fee_rate_sat_vb
        else:
            try:
                fee_rate = fee_estimator.estimate(target).sat_per_vb
            except ChainError as exc:
                return {"error": "chain_unavailable", "detail": str(exc)}

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
        try:
            selection = select_coins(
                utxos,
                amount_sats,
                fee_rate,
                9 + len(change_script),  # serialized change-output cost in vB
                recipient_script,
                change_script=change_script,
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
                fee_rate_sat_vb=fee_rate,
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
            "fee_rate_sat_vb": pending.fee_rate_sat_vb,
            "vsize": pending.vsize,
            "change_sats": pending.change_sats,
            "inputs_count": pending.inputs_count,
            "usd_cents": usd_cents,
            "rate_stale": rate_stale,
            "rate_age_s": rate_age_s,
            "rate_fetched_at": rate_fetched_at,
            "btc_usd": btc_usd,
            "fee_target": pending.fee_target,
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
        if requote and staged is not None:
            if params.fee_rate_sat_vb is not None:
                # Explicit-rate re-quote: no rungs to compare — direction is
                # the literal rate vs the staged record's (display-only).
                if fee_rate > staged.fee_rate_sat_vb:
                    result["requote_direction"] = "faster"
                elif fee_rate < staged.fee_rate_sat_vb:
                    result["requote_direction"] = "slower"
            else:
                direction = _requote_direction(staged.fee_target, target)
                if direction is not None:
                    result["requote_direction"] = direction
        return result

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
    """
    psbt = PSBT.parse(base64.b64decode(confirmed.psbt_base64))
    outputs = [(bytes(out.script_pubkey.data), out.value) for out in psbt.tx.vout]
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


def _make_sign_tx_handler(
    flow: TxFlow,
    selection: SignerSelection,
    signer_override: Signer | FilePsbtSigner | None,
    store: Store,
    wallet_id: int,
    parsed: ParsedKey,
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

        if kind == SIGNER_KIND_FILE:
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
            device_signer = (
                signer_override
                if signer_override is not None
                else HwiUsbSigner(
                    selection.fingerprint_hex, _descriptor_account_path(parsed)
                )
            )
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

        return _out({
            "status": "signed",
            "tx_ref": signed.tx_ref,
            "txid": revalidated.txid,
            "signer_name": signed_result.signer_name,
            "checksum_verified": signed_result.checksum_verified,
        })

    return handler


def _make_broadcast_tx_handler(
    flow: TxFlow,
    client: EsploraClient,
    store: Store,
    wallet_id: int,
) -> Handler:
    """Create the ``broadcast_tx`` handler: SIGNED record → Esplora → BROADCAST.

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
       never a blind re-POST by us.
    4. Recording: :meth:`TxFlow.broadcast` (SIGNED → BROADCAST, terminal)
       with the chain-reported txid, then the outbound transaction is
       upserted into the store's history (``height=None``,
       ``direction="out"``, the approved record's fee) so ``get_history``
       shows it immediately. A store failure becomes a value-free
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
            # detail is scrubbed by the chain layer (no txids/tx hex).
            return {"error": "broadcast_failed", "detail": str(exc)}

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
                    )
                ]
            )
        except (StoreError, sqlite3.Error) as exc:
            # Bookkeeping must not undo the broadcast: warn, stay BROADCAST.
            result["store_warning"] = f"could not record the transaction in history ({exc})"

        # 5. Coin-label lineage (TCK-UTXO-001, design doc §1.3): our outputs
        #    inherit the UNION of the wallet's spent inputs' tag sets — a
        #    mixed-lineage coin carries both classes (the fail-safe side for
        #    the deterministic partition check). Which outputs are ours is the
        #    revalidated positional contract: change rides LAST when present
        #    (tx/psbt.py + the sign-time revalidation), and this SIGNED record
        #    is byte-frozen, so vout = count-1 is ours exactly when
        #    change_sats is set. A send whose recipient is our own receive
        #    address inherits nothing there (ponytail: honest-bounds edge —
        #    the coin appears unlabeled on the next rescan and /label covers
        #    it; full script-ownership matching is the provenance view's
        #    problem, not this capture path's). Purely local bookkeeping —
        #    labeling never causes network I/O — and it must never undo a
        #    completed broadcast, so EVERY failure is contained value-free.
        if confirmed is not None and confirmed.change_sats is not None:
            try:
                signed_psbt = PSBT.parse(base64.b64decode(signed.psbt_base64))
                spent_inputs = tuple(
                    (bytes(reversed(vin.txid)).hex(), vin.vout)
                    for vin in signed_psbt.tx.vin
                )
                store.propagate_coin_lineage(
                    wallet_id, txid, (len(signed_psbt.tx.vout) - 1,), spent_inputs
                )
            except Exception:  # noqa: BLE001 — containment: embit/store errors vary; missed tag-inheritance is annotation loss, never a money or broadcast failure
                result.setdefault(
                    "store_warning",
                    "coin tag inheritance did not record — use /label after the next scan",
                )
        return result

    return handler


def _make_tx_status_handler(client: EsploraClient, flow: TxFlow) -> Handler:
    """Create the ``tx_status`` handler: quoted txid → Esplora status.

    The ``txid`` param (layer 3 enforced it to EXACTLY 64 lowercase hex —
    the injection guard for the URL path) is looked up via
    ``client.get_tx_status``; the result quotes the response verbatim:
    ``{"txid", "confirmed", "block_height", "block_time"}``.

    Eventual consistency (documented): a JUST-broadcast transaction is
    often not indexed by the explorer yet — Esplora answers 404 until it
    sees the transaction. When the queried txid IS the flow's recorded
    broadcast txid and the lookup fails with the not-found status, the
    handler surfaces ``{"error": "unknown_tx", "detail": <value-free>}``
    instead of a generic chain failure, so the narration can say "not
    indexed yet — try again shortly". The 404 detection matches the chain
    layer's documented error-message contract ("status 404"); other
    chain failures surface as ``{"error": "chain_unavailable",
    "detail": <scrubbed>}``.

    The model obtains the txid to query from the FACTS block
    (``broadcast_txid``, :func:`_flow_facts`) — quoted verbatim, never
    invented.
    """

    def handler(envelope: Envelope) -> dict[str, object]:
        params = envelope.params
        if not isinstance(params, TxStatusParams):
            return {"error": "internal", "detail": "tx_status params shape mismatch"}
        try:
            status = client.get_tx_status(params.txid)
        except ChainError as exc:
            detail = str(exc)  # scrubbed by the chain layer (no txids)
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
    """The chain-backend privacy mode — 3-way (TCK-SEC-004 change 5).

    Derived from the SAME single selection point the chain client uses
    (ADR-0018 ``ChainConfig.from_settings``):

    - :data:`BACKEND_MODE_PUBLIC` — no ``Settings.chain_base_url``: the
      public explorer serves the lookups (R7 honesty: its operator can
      associate queried addresses with the user's IP).
    - :data:`BACKEND_MODE_OWN_NODE_LOCAL` — a configured URL whose host is
      loopback: the user's own node on this machine.
    - :data:`BACKEND_MODE_OWN_NODE_REMOTE` — any other configured host
      (LAN/VPS instance): still the user's own node, but NOT on this
      machine, so copy must not claim lookups "stay on this machine".

    The node_status narration and the watch-mode line mirror this function,
    so neither can disagree with the privacy banner about which backend is
    actually in use.
    """
    configured = settings.chain_base_url.strip()
    if not configured:
        return BACKEND_MODE_PUBLIC
    host = _configured_url_host(configured)
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
    "your own node on another machine" in lockstep with the privacy banner.
    """

    def handler(envelope: Envelope) -> dict[str, object]:
        params = envelope.params
        if not isinstance(params, NodeStatusParams):
            # Unreachable via validated envelopes; fail closed anyway.
            return {"error": "internal", "detail": "node_status params shape mismatch"}
        facts: dict[str, object] = {
            "backend_mode": _backend_mode(settings),
            "node_detection_enabled": bool(settings.node_detection_enabled),
        }
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


def _last_block_suffix(client: EsploraClient) -> str | None:
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
    event: IncomingEvent, suffix: str | None = None
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
    """
    short = f"{event.txid[:12]}…"
    if event.kind == "received":
        state = "confirmed" if event.confirmed else "in mempool"
        line = (
            f"Incoming: received {event.amount_sats} sats at {event.address} "
            f"({state}, tx {short})."
        )
    else:
        height = event.height
        height_part = f" (height {height})" if height is not None else ""
        line = (
            f"Confirmed: {event.amount_sats} sats at {event.address} "
            f"now confirmed{height_part} (tx {short})."
        )
    if suffix:
        line = f"{line} · {suffix}"
    return line


def _watch_mode_fragment(mode: str) -> str:
    """The watch-startup line's backend fragment, mirroring the banner.

    Approved copy (TCK-SEC-004 change 5): ``the public API`` /
    ``your own node on this machine`` / ``your own node on another
    machine`` — the same three-way honesty split as
    :func:`privacy_indicator` and the node_status narration.
    """
    return {
        BACKEND_MODE_PUBLIC: "the public API",
        BACKEND_MODE_OWN_NODE_LOCAL: "your own node on this machine",
        BACKEND_MODE_OWN_NODE_REMOTE: "your own node on another machine",
    }[mode]


def _make_watch_probe(
    store: Store,
    wallet_id: int,
    scan_fn: Callable[[], object],
) -> Callable[[], list[WatchedTx]]:
    """Build the production ``watch_incoming`` probe for the current wallet.

    The probe refreshes the chain state through ``scan_fn`` (which in the app
    is :meth:`ScanFlow.scan_now` — the ADR-0022 split run as ONE blocking
    scan: plan on the engine, the derive+fetch on the dedicated chain worker
    over the SINGLE config-selected EsploraClient, persist by the engine;
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
    client: EsploraClient | None = None,
) -> int:
    """Run one due watch cycle (if any) and narrate its events to the user.

    Single-threaded / tick-driven (ADR-0019): the REPL calls this between
    turns; ``poll_due`` gates the run on the configured interval so a full
    poll does not happen on every keystroke. A transient chain/store/scan
    failure fails open — no events, no crash, no logged value — and the next
    turn retries.

    Persistent-failure visibility (NOTE-1): when a due poll raises, a short
    value-free line (``watch: check failed, will retry next cycle``) is
    surfaced THROTTLED — once per failure streak, tracked on the watcher and
    reset on the next successful poll — so a persistently broken poll stays
    visible without spamming every turn. It is never logged.

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
        watcher.mark_poll_succeeded()
        suffix = (
            _last_block_suffix(client)
            if (client is not None and events)
            else None
        )
        for event in events:
            output_fn(sanitize_tool_output(_narrate_incoming_event(event, suffix)))
        return len(events)
    except (ChainError, wallet_scan.ScanError, StoreError, sqlite3.Error, WatchKeyError):
        # Fail open: a background-poll failure must never interrupt the chat.
        # NOTE-1: surface a throttled, value-free line ONCE per failure streak
        # (reset on the next successful poll). Never logged.
        if watcher.mark_poll_failed():
            output_fn("watch: check failed, will retry next cycle")
        return 0


# ------------------------------------------------- engine pump (TCK-WEB-001, ADR-0024 §3)

#: Event kinds. The CLI sink renders ``text``/``progress`` exactly as the
#: pre-web REPL wrote them (output_fn line / raw stdout char); the web
#: transport (WEB-002) maps them onto SSE frames.
EVENT_TEXT: Final[str] = "text"
EVENT_PROGRESS: Final[str] = "progress"
EVENT_TURN_END: Final[str] = "turn_end"

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


def cli_sink(output_fn: Callable[[str], None]) -> Callable[[EngineEvent], None]:
    """The CLI rendering of the event stream — byte-identical to the old
    REPL: text lines go to ``output_fn``, progress chars (scan dots, the
    closing newline) straight to ``sys.stdout`` flushed, markers invisible.
    """

    def sink(event: EngineEvent) -> None:
        if event.kind == EVENT_TEXT:
            output_fn(event.payload)
        elif event.kind == EVENT_PROGRESS:
            sys.stdout.write(event.payload)
            sys.stdout.flush()

    return sink


def cli_emitter(output_fn: Callable[[str], None]) -> EventEmitter:
    """An :class:`EventEmitter` that reproduces the pre-web CLI exactly."""
    return EventEmitter(cli_sink(output_fn))


# ------------------------------------------- chain worker (TCK-SCAN-003, ADR-0022)


def _has_completed_scan(store: Store, wallet_id: int) -> bool:
    """Whether the store carries a completed-scan cursor (the durable
    first-scan-completion record ADR-0022 decision 5 names). A read failure
    is treated as "not completed" (fail closed toward ``stale``)."""
    try:
        return store.get_sync_state(wallet_id, wallet_scan.CURSOR_KEY) is not None
    except (StoreError, sqlite3.Error):
        return False


def _backend_resolved(effective_backend: str | None, store: Store) -> bool:
    """THE single source of truth for "a chain backend has been chosen"
    (TCK-ONB-006; ADR-0022 amendment 1 + ADR-0023 amendment 2). Resolved =
    a URL on any rung of the resolution ladder (env > config file > stored
    — exactly what :func:`resolve_chain_base_url` returns), OR an explicit
    public opt-in record (:data:`BACKEND_CHOICE_SETTING`, written only by
    the warned onboarding conversation). An UNSET stored rung means "never
    chose", NOT "chose public" — that's why the marker exists. Fail closed:
    an unreadable record counts as unresolved (defer + ask, never
    leak-by-accident). While unresolved, a first-run startup scan holds at
    ``awaiting_backend`` and the onboarding ask (re-)arms."""
    if effective_backend is not None:
        return True
    try:
        return store.get_setting(BACKEND_CHOICE_SETTING) == BACKEND_CHOICE_PUBLIC
    except (StoreError, sqlite3.Error):
        return False


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

    def __init__(self, client: EsploraClient) -> None:
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
    ) -> None:
        self._store = store
        self._wallet = wallet
        self._wallet_id = wallet.id
        self._worker = worker
        self._gap_limit = gap_limit
        self._startup_plan = startup_plan
        self._rescan = rescan
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
        self.gate = StartupScan(enabled=True)

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
        self.gate = StartupScan(enabled=True, deferred=True)

    def release_backend(self) -> bool:
        """TCK-ONB-006: the backend choice resolved (an explicit public
        consent was recorded — the ONLY in-session release; an own-server
        choice takes effect next launch per ADR-0018, so it never fires a
        fetch through the old public client). Plan NOW on the engine thread
        (fresh store reads) and start the held startup scan. No-op unless
        the gate is ``awaiting_backend``.

        Returns whether the load actually started (security review F2): the
        consent ack may only claim "loading now" when this says ``True`` —
        a no-op or a failed plan (scan stood down) says ``False``."""
        if self.gate.state != "awaiting_backend":
            return False
        try:
            plan = wallet_scan.plan_scan(
                self._store,
                self._wallet,
                gap_limit=self._gap_limit,
                rebuild=self._rescan,
            )
        except (
            ChainError,
            wallet_scan.ScanError,
            WatchKeyError,
            StoreError,
            sqlite3.Error,
        ):
            # Planning is store-reads-only and network-free; a failure here
            # can only be a broken store — stand the startup scan down
            # exactly like the wiring-time planning failure did, and let the
            # handlers' lazy path (now unlocked) retry per turn.
            self.gate.mark_skipped()
            return False
        self.set_startup(plan, rescan=self._rescan)
        self.begin()
        return self.gate.state in ("pending", "running")

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
                self._warn(output_fn, str(exc))
                self._out_of_window(output_fn)
                return
            raise exc  # a genuine worker bug must not be swallowed
        try:
            summary = wallet_scan.persist_scan(self._store, done.value)  # engine thread
        except (StoreError, sqlite3.Error) as exc:
            self.gate.mark_skipped()
            self._warn(output_fn, str(exc))
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

    def _warn(self, output_fn: Callable[[str], None], detail: str) -> None:
        """The scrubbed startup-failure line (scan/chain/store/key errors are
        value-free by their layers' contracts). The REPL still runs; handlers
        surface store-empty/chain-down states per turn."""
        label = "rescan" if self._rescan else "startup scan"
        output_fn(f"warning: {label} failed: {detail} — continuing with cached state.")

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


@dataclass
class EngineContext:
    """Everything the pump runs on, constructed ON the engine thread."""

    loop: AgentLoop
    flow: TxFlow
    session: SendSession
    table: DispatchTable
    watcher: IncomingWatcher | None = None
    client: EsploraClient | None = None
    #: The non-blocking startup-scan controller (TCK-SCAN-003, ADR-0022);
    #: the pump attaches it to the command queue and drives its events.
    scan: ScanFlow | None = None
    #: The engine-owned store, present when the pump must answer settings
    #: reads/writes (TCK-WEB-005). Only the pump thread may touch it — the
    #: web bootstrap constructs it ON the engine thread.
    store: Store | None = None


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
) -> dict[str, object]:
    """The value-free ``/state`` snapshot, built ON the engine thread.

    Deliberately minimal and validated-by-construction: the only facts are the
    dispatcher-owned flow position (a closed :class:`TxFlowStatus` enum name),
    whether a transaction pends (a boolean), the last turn's gate classification
    (a closed :class:`GateDecision` enum name), whether a watcher is
    configured/enabled (booleans), and — TCK-WEB-005 — the startup-scan state
    (a closed :class:`StartupScan` state name) plus the durable
    first-scan-completed boolean. No address, amount, txid, ``tx_ref``, key
    material OR scan progress CAN appear — every value is an enum NAME or a
    boolean, never data. No progress percentage: a percent is a ratio against
    the wallet's address count and leaks wallet size through the back door.
    """
    return {
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

#: THE allowlist (fail-closed, TCK-WEB-005): only settings keys that EXIST in
#: the store's key/value table and are READ by live code today. Anything
#: else — invented ``fee_cache_ttl_s``/``utxo_*``/env-only scalars — would be
#: a write with no reader, so it is refused. Unknown future keys 404 here
#: until their ladder ships.
_SETTINGS_KEYS: Final[frozenset[str]] = frozenset(
    {wallet_scan.GAP_LIMIT_SETTING, _CHAIN_BASE_URL_KEY}
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


def _env_overridden(env_var: str) -> bool:
    """Whether an env var is set to a non-blank value (the honest
    ``env_override`` flag: the stored rung is shadowed until restart with the
    env unset; the VALUE is never read or echoed)."""
    return bool(os.environ.get(env_var, "").strip())


def _settings_entries(store: Store) -> list[dict[str, object]]:
    """The current stored value of every allowlisted key, with its type,
    allowed range, and honest effect flags. Values here are user-authored
    scalars (a gap count, a backend URL) — never wallet data (no address,
    amount or key material exists in the settings table)."""
    return [
        {
            "key": wallet_scan.GAP_LIMIT_SETTING,
            "type": "int",
            "value": store.get_setting(wallet_scan.GAP_LIMIT_SETTING),
            "default": str(wallet_scan.DEFAULT_GAP_LIMIT),
            "min": wallet_scan._MIN_GAP,
            "max": wallet_scan._MAX_GAP,
            # Every scan plan re-reads the setting (wallet.scan._resolve_gap_limit):
            # takes effect on the NEXT scan, no restart.
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
            # ADR-0018: the switch is CONFIG-only — the chain client (and the
            # fee/price wrappers riding it) are constructed once at bootstrap;
            # a stored change takes effect on the next launch, never hot.
            "requires_restart": True,
            "env_override": _env_overridden(CHAIN_BASE_URL_ENV_VAR),
        },
    ]


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
    store: Store | None, key: str | None, value: str | None
) -> dict[str, object]:
    """Answer a :class:`SettingsRequest` ON THE ENGINE THREAD — the only
    thread that ever reads/writes the settings table for the web transport.

    Read → the allowlisted entries. Write → validate fail-closed, persist via
    the store's settings API, and reply with the freshly re-read entry (the
    client confirms from tool truth, never from its own echo). Refusals carry
    a value-free ``error``; an off-allowlist key is refused WITHOUT even
    naming the request (the name itself is untrusted input)."""
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
        return {"schema": SETTINGS_SCHEMA, "status": "ok", "settings": _settings_entries(store)}
    if key not in _SETTINGS_KEYS or not isinstance(value, str):
        return unknown
    if len(value) > MAX_SETTING_VALUE_CHARS:
        return {
            "schema": SETTINGS_SCHEMA,
            "status": "rejected",
            "key": key,
            "error": "value too long",
        }
    if (error := _apply_setting_change(store, key, value)) is not None:
        return {"schema": SETTINGS_SCHEMA, "status": "rejected", "key": key, "error": error}
    entry = next(e for e in _settings_entries(store) if e["key"] == key)
    return {"schema": SETTINGS_SCHEMA, "status": "applied", "settings": [entry]}


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
        self, timeout: float, key: str | None = None, value: str | None = None
    ) -> dict[str, object] | None:
        """Read the allowlisted settings (``key is None``) or apply ONE
        validated change THROUGH the pump (TCK-WEB-005).

        Same discipline as :meth:`request_state`: the transport thread never
        touches the store; the ENGINE thread validates fail-closed, persists,
        and answers between turns. ``None`` on timeout = engine busy past the
        deadline (the transport answers 503; never-cancel stands — the queued
        consult may still be answered after the caller gave up, so the client
        RE-READS via GET rather than assuming the write failed)."""
        reply: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        self.commands.put(SettingsRequest(SETTINGS_COMMAND, key, value, reply))
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
    ``check_same_thread=True``) and the lazy Llama runtime MUST be created
    inside it — and the pump then serves ``handle.commands`` there. The
    CLI path (:func:`_repl`) runs the same pump with the main thread as the
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
        )

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


def _pump(
    loop: AgentLoop,
    output_fn: Callable[[str], None],
    commands: queue.Queue[Any],
    *,
    flow: TxFlow,
    session: SendSession,
    table: DispatchTable,
    watcher: IncomingWatcher | None = None,
    client: EsploraClient | None = None,
    emitter: EventEmitter | None = None,
    ready: threading.Event | None = None,
    scan: ScanFlow | None = None,
    store: Store | None = None,
    onboarding: OnboardingFlow | None = None,
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
    """
    if scan is not None:
        scan.attach(commands)
        scan.begin()
    while True:
        if not (scan is not None and scan.in_progress):
            watch_count = _drain_watch(watcher, output_fn, client=client)
            if watch_count:
                loop.record_event("watch_events", watch_count)
        if ready is not None:
            ready.set()
        command = commands.get()
        if scan is not None and scan.handle_command(command, output_fn, emitter):
            continue
        if isinstance(command, _PumpError):
            raise command.exc
        if command is QUIT:
            break
        if isinstance(command, StateSnapshotRequest):
            # Typed value-free /state read (TCK-WEB-003), answered ON the engine
            # thread — no model, no output event, no chat line; the transport
            # blocks on this reply (or falls back to transport-only on timeout).
            # TCK-WEB-005: the scan gate's closed state name + the durable
            # first-scan boolean ride the same snapshot (still enum/bool only).
            command.reply.put(build_state_snapshot(flow, session, watcher, scan))
            continue
        if isinstance(command, SettingsRequest):
            # Typed /settings read/single-key write (TCK-WEB-005), answered ON
            # the engine thread — the ONLY thread that reads/writes the
            # settings table for the transport. Fail-closed validation,
            # value-free refusals, unrelated keys structurally untouched.
            command.reply.put(
                handle_settings_request(store, command.key, command.value)
            )
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
            # nothing. Everything else below stays an ordinary turn.
            pass
        else:
            _run_turn(
                loop, flow, session, line, output_fn, client=client, table=table,
                scan_gate=scan.gate if scan is not None else None,
            )
        if emitter is not None:
            emitter.emit(EVENT_TURN_END)
    if scan is not None:
        scan.drain_until_complete(output_fn, emitter)


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point; delegates to :func:`run`."""
    return run(argv)


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
    backend_check_fn: Callable[[str], bool] | None = None,
) -> int:
    """Wire the application from ``argv``/environment and run the REPL.

    Configuration precedence: ``--zpub`` overrides ``LOCALWALLET_ZPUB``;
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
        backend_check_fn: The step-5 URL-validation probe (test seam,
            TCK-ONB-003): ``base_url -> serves mainnet in Esplora shape``.
            Defaults to :func:`localwallet.chain.check_backend` (the only
            networked module) on the resolved timeout settings.

    Returns:
        Process exit code: ``0`` on normal exit (including ``exit``,
        Ctrl-D, Ctrl-C), ``2`` on configuration errors (missing key,
        refused key, store failure, no model). Configuration errors
        never echo the key.
    """
    args = _parse_args(argv)

    # TCK-ONB-003 launch gates. ``interactive`` is the test/automation seam
    # (production: ``None`` → stdin's tty state; a closed or redirected
    # stdin counts as headless — the ADR-0023 rule that scripted launches
    # are NEVER blocked by a conversation). The web transport never gets the
    # onboarding conversation (requirement 5: terminal-only).
    web_mode = bool(
        args.web or os.environ.get(UI_ENV_VAR, "").strip().lower() == "web"
    )
    if interactive is None:
        try:
            is_interactive = sys.stdin.isatty()
        except (OSError, ValueError):  # closed stdin: headless
            is_interactive = False
    else:
        is_interactive = bool(interactive)

    zpub = (args.zpub or os.environ.get(ZPUB_ENV_VAR, "")).strip()
    if not zpub:
        if web_mode or not is_interactive:
            output_fn(f"No watch key configured: pass --zpub or set {ZPUB_ENV_VAR}.")
            return 2
        # ADR-0023 step 1: an interactive first launch is GREETED and asked
        # for the key instead of refused (headless keeps the exit-2 line
        # above — a conversation never gates a scripted launch). The ask
        # validates through the same gated parser as the startup path, so
        # seed-shaped lines are refused with guidance and private/testnet
        # keys fail value-free; None = the user exited.
        asked = ask_watch_key(input_fn, output_fn)
        if asked is None:
            return 0
        zpub = asked

    try:
        # Gated parse (mainnet-only gate enforced at parse time — flip per
        # ADR-0021) plus the canonical wallet descriptor. Both raise
        # value-free WatchKeyErrors.
        parsed = parse_wallet_key(zpub)
        descriptor = WalletDescriptor.from_key(zpub)
    except WatchKeyError as exc:
        output_fn(f"Watch key rejected: {exc}")
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
        output_fn(
            f"Invalid signer selection: use --signer file|hwi or set "
            f"{SIGNER_ENV_VAR}=file|hwi."
        )
        return 2
    signer_selection = SignerSelection(
        kind=signer_kind,
        dir_path=Path(
            os.environ.get(SIGNER_DIR_ENV_VAR, "").strip() or DEFAULT_SIGNER_DIR
        ),
        fingerprint_hex=parsed.hd_key.my_fingerprint.hex(),
    )

    # Pre-flight (SR minor): if the remote debug bridge is opted into but no
    # model id resolves, fail at startup (exit 2, mirroring the zpub config
    # error) instead of selecting a runtime that would fail every turn. The
    # message never echoes the env value.
    remote_base_url = os.environ.get(LLM_BASE_URL_ENV_VAR, "").strip()
    remote_model = os.environ.get(LLM_MODEL_ENV_VAR, "").strip()
    if remote_base_url and not remote_model:
        print(
            f"No model configured for the remote bridge: set {LLM_MODEL_ENV_VAR} "
            f"alongside {LLM_BASE_URL_ENV_VAR}.",
            file=sys.stderr,
        )
        return 2

    generate: ModelRuntime | GenerateFn | RemoteOpenAIRuntime
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
    elif args.stub_llm:
        generate = stub_generate
    else:
        output_fn(f"No model configured: set {MODEL_PATH_ENV_VAR} or pass --stub-llm.")
        return 2

    # TCK-CFG-002: load settings from env + the config file. A malformed
    # config file (bad JSON / wrong type / unknown key) raises ValueError
    # here — refuse startup with a value-free line (exit 2) BEFORE any store
    # side effects, mirroring the gap_limit/zpub config-error paths below.
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        output_fn(f"Configuration error: {exc}")
        return 2

    # TCK-CFG-001 preflight: resolve + validate LOCALWALLET_GAP_LIMIT
    # (fail-closed, value-free — the same spirit as the zpub config-error
    # path above). A malformed value refuses startup with exit 2 BEFORE any
    # store side effects; a valid value is threaded into every scan below as
    # the per-call gap_limit so it overrides the DB setting (ADR-0009).
    try:
        env_gap = _env_gap_limit(settings)
    except ValueError as exc:
        output_fn(f"Configuration error: {exc}")
        return 2

    # TCK-WEB-002 (ADR-0024 §1/§11): the web UI is opt-in and shares every
    # config decision above; the split is only at the input/output seam —
    # _run_web runs the SAME wiring inside start_engine's engine-thread
    # bootstrap (closing TCK-WEB-001's deferred deviation) and serves the
    # loopback HTTP/SSE front instead of the REPL.
    if web_mode:
        return _run_web(
            parsed=parsed,
            descriptor=descriptor,
            signer_selection=signer_selection,
            settings=settings,
            env_gap=env_gap,
            rescan=args.rescan,
            flow=flow,
            generate=generate,
            node_detect_fn=node_detect_fn,
            output_fn=output_fn,
            on_web_server=on_web_server,
        )

    try:
        wiring = _wire(
            parsed=parsed,
            descriptor=descriptor,
            signer_selection=signer_selection,
            settings=settings,
            env_gap=env_gap,
            rescan=args.rescan,
            flow=flow,
            generate=generate,
            node_detect_fn=node_detect_fn,
            output_fn=output_fn,
            cli_interactive=is_interactive,
            backend_check_fn=backend_check_fn,
        )
    except _WiringError as exc:
        output_fn(str(exc))
        return 2

    try:
        _repl(
            wiring.loop,
            output_fn,
            input_fn,
            flow=wiring.flow,
            session=wiring.session,
            watcher=wiring.watcher,
            client=wiring.client,
            table=wiring.table,
            scan=wiring.scan,
            store=wiring.store,
            onboarding=wiring.onboarding,
        )
    except KeyboardInterrupt:
        pass  # clean exit on Ctrl-C
    finally:
        wiring.worker.stop()  # join the chain worker before closing its client
        wiring.client.close()
        wiring.store.close()
        # The remote debug bridge also owns a client (httpx) — close it
        # alongside the Esplora client when it exposes close().
        close = getattr(generate, "close", None)
        if callable(close):
            close()
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
    client: EsploraClient
    loop: AgentLoop
    flow: TxFlow
    session: SendSession
    table: DispatchTable
    watcher: IncomingWatcher | None
    worker: ChainWorker
    scan: ScanFlow
    #: TCK-ONB-003/005 (ADR-0023): the backend conversation for THIS session
    #: — armed at startup on a first-run CLI launch, DORMANT on every other
    #: interactive CLI launch (the /setup command arms it), and always
    #: ``None`` for web (the browser never gets an onboarding surface, only
    #: :data:`WEB_SETUP_HINT`).
    onboarding: OnboardingFlow | None = None


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
    backend_check_fn: Callable[[str], bool] | None = None,
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
            wallet_row, created_here = _resolve_or_create_wallet(store, descriptor)
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
    # the chain client, the 3-state privacy banner, the watch-mode line, and
    # the node_status narration all read that one field and can therefore
    # never disagree (decision 6). With nothing on any rung, the value stays
    # empty and behavior is bit-identical to the public default.
    effective_backend = resolve_chain_base_url(
        settings.chain_base_url, store.get_chain_base_url()
    )
    if effective_backend is not None:
        settings.chain_base_url = effective_backend

    client = EsploraClient(
        # ``None`` keeps today's client-side ``ChainConfig.from_settings``
        # resolution; the only difference is that the stored rung has now
        # been folded into ``settings.chain_base_url`` above, so the client
        # and every banner/mode surface resolve the SAME value.
        base_url=settings.chain_base_url or None,
        timeout_s=settings.request_timeout_s,
        max_retries=settings.max_retries,
    )
    # base_url is deliberately left as the client default: it resolves through
    # the single selection point (ChainConfig.from_settings — Settings.chain_base_url
    # when set, else the legacy esplora_base_url). This construction site must NOT
    # hardcode a public default that would bypass the Phase 4 backend switch (ADR-0018).
    # Fee/price wrappers share the ONE chain client (no second transport);
    # construction is network-free — they fetch lazily, per their TTLs.
    fee_estimator = FeeEstimator(client)
    price_oracle = PriceOracle(client)
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
        output_fn(TLS_UNVERIFIED_WARNING)

    # Background watch (Phase 5, TCK-P5-001; ADR-0019, ADR-0022 decision 4).
    # Tick-driven in the CLI: the watcher holds no thread and shares no
    # sqlite object across threads; the REPL runs a due poll cycle between
    # turns, and the poll's chain fetch rides the SAME worker (the P5-001
    # "full scan per poll on the engine thread" cost note is retired — the
    # engine only persists what the worker fetched). The startup line
    # states — in lockstep with the privacy banner — whether background
    # watching runs against the user's own node or the public API.
    scan = ScanFlow(store, wallet_row, worker, gap_limit=env_gap)
    watcher: IncomingWatcher | None = None
    if settings.watch_interval_s > 0:
        watcher = IncomingWatcher(
            _make_watch_probe(store, wallet_row.id, scan.scan_now),
            interval_s=settings.watch_interval_s,
        )
        watch_mode = _watch_mode_fragment(_backend_mode(settings))
        output_fn(
            f"Background watch: on — checks up to every "
            f"{settings.watch_interval_s:g}s against {watch_mode} "
            f"(LOCALWALLET_WATCH_INTERVAL_S=0 turns it off)."
        )
    else:
        output_fn("Background watch: off.")

    # Startup scan plan (non-blocking, ADR-0022 decision 1) — or the
    # opted-out / failed-to-plan fallback. Planning is store-reads-only and
    # network-free, so it stays on the engine thread; the fetch runs on the
    # worker once the pump starts the flow.
    #
    # TCK-ONB-006 (ADR-0022 amendment 1, the FIRST-RUN EXCEPTION): when no
    # backend choice exists on ANY rung (env > config file > stored >
    # explicit-public record), an interactive or web launch HOLDS the gate
    # at ``awaiting_backend`` until the backend branch resolves —
    # user-confirmed 2026-09-09: wallet addresses must never reach the
    # public default before an explicit choice. The hold is INDEPENDENT of
    # AUTO_SCAN (security review F1, the blocker): turning off the
    # AUTOMATIC scan is not consent to an unchosen server — the held gate
    # stands the lazy in-handler scan and the watch drain down too, so an
    # AUTO_SCAN=0 launch stays leak-free while unresolved and the
    # mandatory ask re-arms on EVERY unresolved interactive launch (a
    # consent-released load is user-initiated, not an auto scan). Every
    # run with a resolved choice scans immediately (or stays lazy-
    # opted-out), unchanged. A headless scripted launch keeps ADR-0023's
    # never-blocked contract: no ask can appear there, so it behaves
    # exactly as before the amendment — the command line is the operator's
    # explicit decision (the documented carve-out; ADR-0022 amendment 1).
    auto_scan = os.environ.get(AUTO_SCAN_ENV_VAR, "").strip() != "0"
    backend_choice_resolved = _backend_resolved(effective_backend, store)
    defer_startup = (
        not backend_choice_resolved and (cli_interactive or web_mode)
    )
    if defer_startup:
        scan.set_startup_deferred(rescan=rescan)
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
            output_fn(f"warning: {label} failed: {exc} — continuing with cached state.")

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
    output_fn("Type a message — 'exit' or Ctrl-D quits.")

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
            check_backend=backend_check_fn
            or (
                # Snappy setup probe: one attempt (min'ing the retry budget
                # down, never up), the shared per-request timeout. The probe
                # itself is chain/ code — the ONLY networked module (G5).
                lambda url: check_backend(
                    url,
                    timeout_s=settings.request_timeout_s,
                    max_retries=min(settings.max_retries, 1),
                )
            ),
            node_report=(
                None
                if not settings.node_detection_enabled
                else (node_detect_fn or (lambda: detect_local_nodes(settings)))
            ),
            loopback_host=_loopback_host_of,
            armed=ask_at_startup,
            deferred=defer_startup,
            # Explicit public consent (recorded by the flow itself) is the
            # ONLY in-session release of the held scan — an own-server save
            # waits for the next launch (ADR-0018 config-only; the live
            # client is the old one and must never fetch on a refused
            # backend). No-op unless the gate is actually awaiting, and it
            # REPORTS whether the load started (the flow gates its
            # "loading now" line on that answer, security review F2).
            public_chosen=scan.release_backend,
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

    session = SendSession()
    # The confirmation-ETA mempool hint (TCK-P5-002): consulted per create_tx
    # and per CREATED turn (lazily, fail-closed to no congestion adjustment);
    # narration-only — never a gate input.
    seconds_since_last_block_fn = lambda: time_since_last_block(client)
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
    )
    loop = AgentLoop(generate, table)
    return _Wiring(
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
    )


def _run_web(
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
    on_web_server: Callable[[WebServer], None] | None = None,
) -> int:
    """The web launch (TCK-WEB-002, ADR-0024 §1/§3/§11).

    The full startup wiring flows through :func:`start_engine` (closing
    TCK-WEB-001's deferred deviation): ``bootstrap`` runs ``_wire`` ON the
    engine thread — the Store's ``check_same_thread`` pins construction
    there — and the server owns the one engine instance behind the loopback
    HTTP/SSE front. Prints the launch URL and the per-launch token on
    SEPARATE lines (the token is copyable but NEVER embedded in a URL —
    ADR-0024 §6); then parks until Ctrl-C. Exit codes mirror the CLI:
    ``0`` normal, ``2`` wiring/config failure (surfaced from the bootstrap).
    """
    # Late import: ui.web.server imports this module (no cycle at runtime).
    from localwallet.ui.web.server import serve_web

    booted = threading.Event()
    startup: dict[str, BaseException | None] = {"exc": None}
    wired: dict[str, _Wiring] = {}

    def bootstrap() -> EngineContext:
        try:
            wiring = _wire(
                parsed=parsed,
                descriptor=descriptor,
                signer_selection=signer_selection,
                settings=settings,
                env_gap=env_gap,
                rescan=rescan,
                flow=flow,
                generate=generate,
                node_detect_fn=node_detect_fn,
                output_fn=output_fn,
                web_mode=True,
            )
        except BaseException as exc:
            startup["exc"] = exc
            booted.set()
            raise
        wired["wiring"] = wiring
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
        )

    try:
        server = serve_web(bootstrap)
    except OSError:
        # A bind failure (address/port unavailable) is the only startup
        # failure serve_web can raise. Exit 2 with the clean, VALUE-FREE
        # message — never echo the socket error (it carries the address),
        # exactly how every other config-fatal path reports. The engine
        # thread bootstrap may have spawned is a daemon: it dies with this
        # exiting process, so there is nothing to tear down here.
        output_fn("Could not start the web server.")
        return 2
    try:
        # The engine bootstraps (banner + watch line + prompt state) before
        # the URL prints; the startup scan now runs NON-BLOCKING on the
        # chain worker inside the pump (TCK-SCAN-003, ADR-0022) — turns
        # queued meanwhile are served in order, and the web UI's
        # freshness/progress surfacing of the same flow is TCK-WEB-005.
        booted.wait()
        exc = startup["exc"]
        if exc is not None:
            output_fn(
                str(exc)
                if isinstance(exc, _WiringError)
                else "Could not start the engine."
            )
            return 2
        output_fn(f"Web UI: {server.url}")
        output_fn(f"Token: {server.token}")
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
            wiring.client.close()
        close = getattr(generate, "close", None)
        if callable(close):
            close()
    return 0


def _resolve_or_create_wallet(
    store: Store, descriptor: WalletDescriptor
) -> tuple[WalletRecord, bool]:
    """Reuse the wallet row carrying this descriptor, else create one.

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
    for row in store.list_wallets():
        if row.descriptor == descriptor.descriptor:
            return row, False
    return store.create_wallet(name="default", descriptor=descriptor.descriptor), True


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
    parser.add_argument(
        "--web",
        action="store_true",
        help=(
            "serve the opt-in localhost web UI (ADR-0024) instead of the "
            "REPL; overrides LOCALWALLET_UI=web"
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
    client: EsploraClient | None = None,
    table: DispatchTable,
    emitter: EventEmitter | None = None,
    scan: ScanFlow | None = None,
    store: Store | None = None,
    onboarding: OnboardingFlow | None = None,
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
        )
    finally:
        stop.set()
        ready.set()


#: Fallback wording for an unparseable ``/`` command (value-free).
_TRANSCRIPT_HELP: Final[str] = (
    "Commands: /details — reprint the pending transaction's full card; "
    "/label — list or set your own coin tags and notes; "
    "/setup — choose which server answers the app about your addresses "
    "(public default or your own Esplora-compatible server); "
    "/export <path> — write a redacted session transcript; "
    "/scrub — clear the in-memory transcript; /help — show this."
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


def _run_turn(
    loop: AgentLoop,
    flow: TxFlow,
    session: SendSession,
    line: str,
    output_fn: Callable[[str], None],
    *,
    client: EsploraClient | None = None,
    table: DispatchTable,
    scan_gate: StartupScan | None = None,
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
    - GATE-MERGE (TCK-UX-002, ADR-0013 amendment): when the turn's
      ``confirm_tx`` succeeds, the device handoff (``sign_tx`` handler,
      code-built envelope from the dispatcher-owned confirmed ref) runs in
      the SAME turn — the card asks once ("sign"), the state machine keeps
      CONFIRMED/SIGNED internally, and broadcast stays a separately gated
      turn. An LLM "yes" never counts: the chain only follows a dual-key
      confirm that already passed.
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
    confirmed = flow.confirmed if flow.state is TxFlowStatus.CONFIRMED else None
    if confirmed is not None and line.strip().lower() == "retry":
        envelope = Envelope(
            v=0,
            intent=IntentName.SIGN_TX,
            params=SignTxParams(tx_ref=confirmed.tx_ref),
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
    # Narration-only ETA fact (TCK-P5-002): the mempool hint is computed
    # lazily ONLY when the flow is CREATED (the ETA fact is needed); any
    # failure degrades to no congestion adjustment, never a crash.
    seconds_since_last_block_fn = (
        (lambda: time_since_last_block(client)) if client is not None else None
    )
    facts = _flow_facts(
        flow, seconds_since_last_block_fn=seconds_since_last_block_fn
    )
    if scan_gate is not None and scan_gate.enabled and not scan_gate.complete:
        # ADR-0022 decision 5 (SR fix): the deterministic, tool-owned
        # freshness fact while the first scan has NOT completed — pending,
        # running, or skipped after a failed startup scan, matching the
        # handlers' stale flag. ``first_scan_incomplete`` stays the
        # narrower create_tx gate (decision 6): a skip unblocks sends.
        # The model narrates from it; it never authors a freshness claim.
        facts["freshness"] = FRESHNESS_STALE
    turn = loop.run(line, facts)
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
    elif envelope.intent is IntentName.NEW_ADDRESS:
        _print_new_address(turn.result or {}, output_fn)
    elif envelope.intent is IntentName.CREATE_TX:
        _print_create_tx(turn.result or {}, output_fn, session=session)
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
    figures themselves print verbatim from the cache either way.
    """
    if result.get("error") is not None:
        output_fn(sanitize_tool_output(_error_line(result, "Balance lookup failed")))
        return
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
    as a final "No unspent outputs."
    """
    if result.get("error") is not None:
        output_fn(sanitize_tool_output(_error_line(result, "UTXO lookup failed")))
        return
    _print_freshness_note(result, output_fn)
    utxos = result.get("utxos")
    if not isinstance(utxos, list) or not utxos:
        output_fn(sanitize_tool_output("No unspent outputs."))
        return
    for utxo in utxos:
        if not isinstance(utxo, dict):  # pragma: no cover — handler-shaped data
            continue
        address = utxo.get("address")
        address_part = f"{address} · " if address else ""
        confirmed_label = "confirmed" if utxo.get("confirmed") else "unconfirmed"
        txid = str(utxo.get("txid", ""))
        short = f"{txid[:12]}…" if txid else "tx <unknown>"
        output_fn(
            sanitize_tool_output(
                f"{address_part}{utxo.get('value_sats', 0)} sats · "
                f"{confirmed_label} · tx {short} vout {utxo.get('vout', 0)}"
            )
        )


def _print_new_address(result: Mapping[str, object], output_fn: Callable[[str], None]) -> None:
    """Print the fresh address verbatim from the handler's result dict."""
    if result.get("error") is not None:
        output_fn(
            sanitize_tool_output(_error_line(result, "Could not allocate a new address"))
        )
        return
    branch = result.get("branch", 0)
    kind = "change" if branch == 1 else "receive"
    output_fn(
        sanitize_tool_output(
            f"Fresh {kind} address (index {result.get('index', 0)}): "
            f"{result.get('address', '')}"
        )
    )


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


def _card_rate(result: Mapping[str, object]) -> str | None:
    """Thousands-separated USD/BTC rate, whole dollars when the source gave
    whole dollars (the price provider does — ADR-0011 §4), else 2 decimals.
    ``None`` when absent/not numeric (fail-closed: never a fabricated rate)."""
    value = result.get("btc_usd")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return f"{int(value):,}" if float(value).is_integer() else f"{value:,.2f}"


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
    if session is not None:
        full: list[str] = []
        _print_confirmation_card(result, full.append)
        session.card_render = full
    output_fn(sanitize_tool_output(_CARD_ASK_LINE))
    output_fn(sanitize_tool_output(f"To: {result.get('recipient', '')}"))
    pay = "Pay: unavailable"
    amount = _card_sats(result, "amount_sats")
    if amount is not None:
        pay = f"Pay: {amount} sats"
        usd_cents = result.get("usd_cents")
        if isinstance(usd_cents, int):
            pay += f" (${usd_cents // 100}.{usd_cents % 100:02d}"
            if result.get("rate_stale"):
                # Stale per the ADR-0011 ladder: surface WHY the number may
                # be off (age) instead of the (now-untrusted) rate figure.
                rate_age = result.get("rate_age_s")
                if rate_age is not None:
                    pay += f" · rate age {rate_age}s"
                pay += " · stale"
            else:
                rate = _card_rate(result)
                if rate is not None:
                    pay += f" · @ ${rate}/BTC"
            pay += ")"
    output_fn(sanitize_tool_output(pay))
    fee_sats = _card_sats(result, "fee_sats")
    fee = "Fee: unavailable" if fee_sats is None else f"Fee: {fee_sats} sats"
    if fee_sats is not None:
        rate = _card_sats(result, "fee_rate_sat_vb")
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
    output_fn(sanitize_tool_output(fee))
    sources = result.get("inputs_count")
    if isinstance(sources, int) and not isinstance(sources, bool):
        from_line = f"From: your wallet ({sources:,} {'source' if sources == 1 else 'sources'})"
    else:
        from_line = "From: your wallet (sources unavailable)"
    change = _card_sats(result, "change_sats")
    if change is not None:
        from_line += f" · {change} sats come back as change"
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
    from ``result["recipient"]`` (tool-output verbatim rule); the USD
    segment appears only when the handler supplied ``usd_cents``, with
    the rate age and the stale marker when present.

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
    usd_cents = result.get("usd_cents")
    if isinstance(usd_cents, int):
        amount_line += f" (${usd_cents // 100}.{usd_cents % 100:02d}"
        if result.get("rate_stale"):
            # Stale per the ADR-0011 ladder: surface WHY the number may be
            # off (age) instead of the (now-untrusted) rate figure.
            rate_age = result.get("rate_age_s")
            if rate_age is not None:
                amount_line += f" · rate age {rate_age}s"
            amount_line += " · stale"
        else:
            rate = _card_rate(result)
            if rate is not None:
                amount_line += f" · @ ${rate}/BTC"
        amount_line += ")"
    output_fn(sanitize_tool_output(amount_line))
    output_fn(sanitize_tool_output(f"To: {result.get('recipient', '')}"))
    if "fee_sats" in result:
        fee_line = f"Fee: {result['fee_sats']} sats"
        fee_parts: list[str] = []
        if result.get("fee_rate_sat_vb") is not None:
            fee_parts.append(f"{result['fee_rate_sat_vb']} sat/vB")
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
    output_fn(sanitize_tool_output(f"Ref: {result.get('tx_ref', '')}"))


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
    ``unknown_tx`` → the eventual-consistency note; other errors surface
    value-free via :func:`_error_line`.
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
    if backend == BACKEND_MODE_OWN_NODE_LOCAL:
        output_fn(sanitize_tool_output(_NODE_STATUS_OWN_NODE_LOCAL))
    elif backend == BACKEND_MODE_OWN_NODE_REMOTE:
        output_fn(sanitize_tool_output(_NODE_STATUS_OWN_NODE_REMOTE))
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

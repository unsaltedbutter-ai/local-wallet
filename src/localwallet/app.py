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
  value-free errors → config-error exit 2), open the store
  (:class:`~localwallet.config.Settings` ``store_path``), reuse or
  create the single wallet profile (descriptor-match guard, ADR-0010),
  pick the model runtime (remote debug bridge → local GGUF →
  ``--stub-llm``), run the startup scan (or ``--rescan``; env opt-out
  via ``LOCALWALLET_AUTO_SCAN=0``) and the chat REPL. The REPL owns the
  :class:`TxFlow` / :class:`SendSession` pair and classifies every user
  utterance against the confirm gate at the top of each turn.

Invariants honored here:

- Model output is untrusted input handled exclusively by
  ``AgentLoop`` → ``handle_raw`` (3-layer validation → allowlist
  dispatch). Nothing in this module parses or executes model text.
- Network I/O happens only inside ``localwallet.chain``; this module
  imports that local module, never a network library (lint-enforced).
  Handlers reach the chain only through the scan callable (lazy first
  scan) — ``new_address``/``get_history``/``get_utxos`` never do I/O.
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
import re
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from string import punctuation
from typing import Final

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
    estimate_eta,
    time_since_last_block,
)
from localwallet.config import Settings
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

__all__ = [
    "AUTO_SCAN_ENV_VAR",
    "DEFAULT_HISTORY_LIMIT",
    "DEFAULT_SIGNER_DIR",
    "NODE_STATUS_DETECTION_DISABLED",
    "OUT_OF_WINDOW_NOTICE",
    "PRIVACY_INDICATOR",
    "PRIVACY_INDICATOR_OWN_NODE_LOCAL",
    "PRIVACY_INDICATOR_OWN_NODE_REMOTE",
    "SIGNER_DIR_ENV_VAR",
    "SIGNER_ENV_VAR",
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

#: ADR-0009 UI surfacing for ``sync_state["out_of_window_detected"]``:
#: printed at startup when the store carries a non-empty warning payload.
#: Generic scrubbed wording (indexes only in the payload; no addresses).
OUT_OF_WINDOW_NOTICE: Final[str] = (
    "note: usage was found beyond your usual address window — a rescan is "
    "recommended; say 'rescan' is not available yet, restart with --rescan"
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

_BANNER_TITLE: Final[str] = (
    "local-wallet — watch-only Bitcoin wallet (mainnet, Phase 1 wallet engine)"
)
_BANNER_MAINNET: Final[str] = (
    "Network: Bitcoin MAINNET only — testnet keys are refused (ADR-0021)."
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

#: User-facing narration lines for the send flow (TCK-P2-004). Every value
#: they carry comes verbatim from the handler result dict — the UI computes
#: nothing (integer division/formatting of result values only, the same
#: display-truncation class as txid shortening).
_CARD_HEADER_LINE: Final[str] = (
    "Pending transaction — review it carefully, then say 'confirm' or 'cancel':"
)
_CANCELLED_LINE: Final[str] = "Transaction cancelled."
_CONFIRMED_LINE: Final[str] = (
    "Approved. Next step: sign — reply 'sign' to hand the transaction to your signer."
)
_GUIDANCE_STILL_PENDING: Final[str] = (
    "The transaction is still pending — say 'confirm' to approve it or "
    "'cancel' to discard it."
)
_GUIDANCE_AMBIGUOUS: Final[str] = (
    "That was ambiguous — say 'confirm' to approve the pending transaction "
    "or 'cancel' to discard it."
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
    """

    gate_decision: GateDecision = GateDecision.NOT_A_DECISION


@dataclass(frozen=True, slots=True)
class SignerSelection:
    """Resolved signing-backend configuration (TCK-P3-005).

    Built once at startup from ``--signer`` / :data:`SIGNER_ENV_VAR` /
    :data:`SIGNER_DIR_ENV_VAR`:

    - ``kind`` — ``"file"`` (airgap transfer folder, ADR-0014) or
      ``"hwi"`` (USB hardware wallet, ADR-0015);
    - ``dir_path`` — the file signer's transfer folder (created on demand
      at the first export);
    - ``fingerprint_hex`` — the wallet's expected master-key fingerprint
      from the parsed wallet key (the descriptor origin fingerprint); the
      HWI signer's exactly-one-match gate (ADR-0015) is constructed from
      it — lazily, ONLY when the hwi kind is selected, and per sign
      attempt (the signer objects are stateless).
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
) -> DispatchTable:
    """Build the allowlist dispatch table for the running app.

    Args:
        store: The open persistence layer every handler reads.
        wallet: The active wallet row (ADR-0010: exactly one profile).
        parsed: The wallet's parsed account key (for ``new_address``
            derivation, the send flow's PSBT account fields, and the HWI
            signer's expected fingerprint; public key only).
        client: The chain client — used only by ``scan_fn`` (the lazy
            first scan inside ``get_balance``/``create_tx``), by the
            fee/price wrappers below, and by the ``broadcast_tx`` /
            ``tx_status`` handlers; the read handlers never touch it.
        scan_fn: Zero-argument callable performing one wallet scan
            (``scan_wallet(store, client, wallet)`` in production). Used
            lazily when the store has no sync cursor.
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
            store, wallet_id, scan_fn
        ),
        IntentName.GET_HISTORY: _make_get_history_handler(store, wallet_id),
        IntentName.GET_UTXOS: _make_get_utxos_handler(store, wallet_id),
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
) -> Handler:
    """Create the ``get_balance`` handler closed over the store.

    Sums the cached UTXO snapshot (confirmed/unconfirmed split) and
    reports the count of addresses holding UTXOs plus the tip height
    recorded by the last scan. If the wallet has never scanned (no sync
    cursor), ``scan_fn`` runs once lazily first — this keeps the
    Phase 0 AC ("What's my balance?" returns a correct live balance)
    working when the startup scan is opted out via
    :data:`AUTO_SCAN_ENV_VAR`. At most one scan attempt happens per
    call; a failed lazy scan surfaces as
    ``{"error": "chain_unavailable", "detail": <scrubbed>}`` (chain and
    scan error strings are value-free by contract), store failures as
    ``{"error": "store_error", ...}``. A failed/absent tip height omits
    the ``tip_height`` key entirely — never a fabricated value.
    """

    def handler(envelope: Envelope) -> dict[str, object]:
        del envelope  # get_balance params are empty by schema
        try:
            if store.get_sync_state(wallet_id, wallet_scan.CURSOR_KEY) is None:
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


def _make_get_history_handler(store: Store, wallet_id: int) -> Handler:
    """Create the ``get_history`` handler closed over the store.

    Projects cached transactions ordered by height DESC then block_time
    DESC (unconfirmed — ``height=None`` — sort as the newest entries),
    capped at ``params.limit`` or :data:`DEFAULT_HISTORY_LIMIT`. Items
    carry ``txid``/``height``/``direction``/``fee_sats``/``block_time``
    exactly as cached (nullable fields stay ``None``); the result is
    deliberately address-free — addresses appear only in
    ``get_utxos``/``new_address`` output. No network I/O.
    """

    def handler(envelope: Envelope) -> dict[str, object]:
        params = envelope.params
        if not isinstance(params, GetHistoryParams):
            return {"error": "internal", "detail": "get_history params shape mismatch"}
        limit = params.limit if params.limit is not None else DEFAULT_HISTORY_LIMIT
        try:
            txs = store.get_txs_for_wallet(wallet_id)
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
        }

    return handler


def _make_get_utxos_handler(store: Store, wallet_id: int) -> Handler:
    """Create the ``get_utxos`` handler closed over the store.

    Returns the cached UTXO snapshot verbatim (``txid``/``vout``/
    ``address``/``value_sats``/``confirmed``) plus a count. Addresses
    come from the store — i.e. from tool output via the scan — so the
    quote-verbatim rule is satisfied end to end. No network I/O.
    """

    def handler(envelope: Envelope) -> dict[str, object]:
        del envelope  # get_utxos params are empty by schema
        try:
            records = store.get_utxos_for_wallet(wallet_id)
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
        return {"utxos": utxos, "count": len(utxos)}

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
    pending (ADR-0013: at most one pending transaction; a stale one is
    recovered explicitly, never reaped). The pending card is re-shown
    from the flow's own record so the user can act on it; rate fields
    are unknown on re-show (``usd_cents=None``) and ``expires_in_s`` is
    the REMAINING ttl (flow's clock, floored at 0) — a re-shown card
    never claims more lifetime than the confirm gate will grant (a
    pending expired by the clock is refused at confirm anyway).
    """
    result: dict[str, object] = {"error": "tx_pending"}
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
) -> Handler:
    """Create the ``create_tx`` handler: stage an unsigned pending transaction.

    Pipeline (every step fail-closed; nothing stages unless ALL succeed):

    1. Pending guard: with a transaction already pending the handler
       refuses with ``{"error": "tx_pending", ...}`` plus the pending
       card fields (re-shown from the flow) — before any network or
       store work.
    2. Amount resolution: ``amount_sats`` is taken direct;
       ``amount_usd`` requires the price oracle (:meth:`PriceOracle.fresh`).
       A price failure on the USD path refuses the whole request with
       ``{"error": "price_unavailable", ...}`` and NO flow entry (the
       user retries, or gives sats). On the sats path the oracle is
       consulted best-effort for the card's USD display only — a failure
       there degrades to ``usd_cents=None`` and never blocks the send.
       A stale-but-served rate (ADR-0011 ladder) is marked ``rate_stale``
       with its age; the rate's fetch timestamp is included either way.
    3. Fee rate: ``fee_target`` maps onto :class:`FeeTarget`; when the
       model omits it the handler applies **MEDIUM** by default
       (documented decision, TCK-P2-004: a send with no stated urgency
       gets the half-hour target, never the cheapest/slowest). A failed
       fee lookup surfaces as ``chain_unavailable``.
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

        # 1. Pending guard (before any network/store work).
        if flow.state is TxFlowStatus.CREATED:
            return _tx_pending_result(
                flow, seconds_since_last_block_fn=seconds_since_last_block_fn
            )

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

        # 3. Fee rate (MEDIUM default when the model omits fee_target).
        target = FeeTarget(params.fee_target) if params.fee_target else FeeTarget.MEDIUM
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
                fee_target=target.value,
                change_sats=selection.change_sats,
            )
        except FlowError:
            # Unreachable single-threaded after the pending guard; fail
            # closed with the pending card rather than double-staging.
            return _tx_pending_result(
                flow, seconds_since_last_block_fn=seconds_since_last_block_fn
            )

        eta = _eta_for(target.value, seconds_since_last_block_fn=seconds_since_last_block_fn)

        return {
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
            "fee_target": pending.fee_target,
            "expires_in_s": PENDING_TTL_S,
            **({} if eta is None else eta),
        }

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
       from ``--signer`` / :data:`SIGNER_ENV_VAR`) is the default; the
       model's optional closed-enum ``signer`` param may pick the other
       kind (handler policy per the protocol contract — the model never
       touches signer configuration, only the enum choice).
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
       - **hwi** (ADR-0015): :class:`HwiUsbSigner` is constructed lazily
         ONLY when this kind runs (expected fingerprint from the parsed
         wallet key), and the fingerprint gate + post-open re-check run
         inside it. A :class:`DeviceError` maps to
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

        # 3. Signer dispatch.
        kind = params.signer if params.signer is not None else selection.kind
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
                    return {"error": "import_failed", "detail": str(exc)}
            else:
                try:
                    exported = file_signer.export_unsigned(
                        confirmed.psbt_base64, confirmed.tx_ref
                    )
                except SignerError as exc:
                    return {"error": "export_failed", "detail": str(exc)}
                return {
                    "error": "signed_file_missing",
                    "unsigned_path": str(exported.unsigned_path),
                    "signed_filename": signed_path.name,
                    "signer_name": file_signer.name,
                }
        else:
            device_signer = (
                signer_override
                if signer_override is not None
                else HwiUsbSigner(selection.fingerprint_hex)
            )
            try:
                signed_result = device_signer.sign_unsigned(confirmed.psbt_base64)
            except DeviceError as exc:
                # guidance is code-owned §10 text from the error hierarchy.
                return {"error": "device_error", "guidance": str(exc)}
            except SignerError as exc:
                return {"error": "signer_error", "detail": str(exc)}

        # 4. Revalidation gate — mismatch is a HARD STOP, flow untouched.
        try:
            revalidated = revalidate_signed_psbt(signed_result.psbt_base64, intended)
        except TamperedPsbtError as exc:
            return {"error": "revalidation_failed", "detail": str(exc)}

        # 5. Record the signed PSBT (CONFIRMED → SIGNED).
        try:
            signed = flow.mark_signed(params.tx_ref, signed_result.psbt_base64)
        except FlowError as exc:  # unreachable single-threaded after the gate
            return {"error": "sign_refused", "detail": str(exc)}

        return {
            "status": "signed",
            "tx_ref": signed.tx_ref,
            "txid": revalidated.txid,
            "signer_name": signed_result.signer_name,
            "checksum_verified": signed_result.checksum_verified,
        }

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
    wraps :func:`localwallet.wallet.scan.scan_wallet` over the SINGLE
    config-selected EsploraClient — ADR-0018; a self-hosted poll hits the
    user's node, never the public API), then reads the wallet's transactions
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
) -> int:
    """Wire the application from ``argv``/environment and run the REPL.

    Configuration precedence: ``--zpub`` overrides ``LOCALWALLET_ZPUB``;
    ``--signer`` overrides ``LOCALWALLET_SIGNER`` for the signing backend
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
    scan) populates the cache; ``LOCALWALLET_AUTO_SCAN=0`` skips it —
    the first balance/created-tx lookup then scans lazily. Chain/store
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

    Returns:
        Process exit code: ``0`` on normal exit (including ``exit``,
        Ctrl-D, Ctrl-C), ``2`` on configuration errors (missing key,
        refused key, store failure, no model). Configuration errors
        never echo the key.
    """
    args = _parse_args(argv)

    zpub = (args.zpub or os.environ.get(ZPUB_ENV_VAR, "")).strip()
    if not zpub:
        output_fn(f"No watch key configured: pass --zpub or set {ZPUB_ENV_VAR}.")
        return 2

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

    settings = Settings.from_env()
    try:
        store = Store(settings.store_path)
        try:
            wallet_row = _resolve_or_create_wallet(store, descriptor)
            store.set_active_wallet(wallet_row.id)
        except (StoreError, sqlite3.Error) as exc:
            output_fn(f"Could not prepare the wallet store: {exc}")
            store.close()
            return 2
    except (StoreError, sqlite3.Error, OSError) as exc:
        output_fn(f"Could not open the wallet store: {exc}")
        return 2

    client = EsploraClient(
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

    output_fn(_BANNER_TITLE)
    output_fn(_BANNER_MAINNET)
    output_fn(f"Privacy notice: {privacy_indicator(settings)}")
    output_fn("Type a message — 'exit' or Ctrl-D quits.")

    # Background watch (Phase 5, TCK-P5-001; ADR-0019). Single-threaded /
    # tick-driven: the watcher holds no thread and shares no sqlite object
    # across threads; the REPL runs a due poll cycle between turns. The
    # startup line states — in lockstep with the privacy banner — whether
    # background watching runs against the user's own node or the public API.
    watcher: IncomingWatcher | None = None
    if settings.watch_interval_s > 0:
        watcher = IncomingWatcher(
            _make_watch_probe(
                store,
                wallet_row.id,
                lambda: wallet_scan.scan_wallet(store, client, wallet_row),
            ),
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

    _startup_scan(store, client, wallet_row, rescan_requested=args.rescan, output_fn=output_fn)
    out_of_window = _out_of_window_line(store, wallet_row.id)
    if out_of_window is not None:
        output_fn(out_of_window)

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
        lambda: wallet_scan.scan_wallet(store, client, wallet_row),
        flow=tx_flow,
        session=session,
        fee_estimator=fee_estimator,
        price_oracle=price_oracle,
        signer_selection=signer_selection,
        settings=settings,
        node_detect_fn=node_detect_fn,
        seconds_since_last_block_fn=seconds_since_last_block_fn,
    )
    loop = AgentLoop(generate, table)

    try:
        _repl(loop, output_fn, input_fn, flow=tx_flow, session=session, watcher=watcher, client=client)
    except KeyboardInterrupt:
        pass  # clean exit on Ctrl-C
    finally:
        client.close()
        store.close()
        # The remote debug bridge also owns a client (httpx) — close it
        # alongside the Esplora client when it exposes close().
        close = getattr(generate, "close", None)
        if callable(close):
            close()
    return 0


def _resolve_or_create_wallet(
    store: Store, descriptor: WalletDescriptor
) -> WalletRecord:
    """Reuse the wallet row carrying this descriptor, else create one.

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
            return row
    return store.create_wallet(name="default", descriptor=descriptor.descriptor)


def _startup_scan(
    store: Store,
    client: EsploraClient,
    wallet: WalletRecord,
    *,
    rescan_requested: bool,
    output_fn: Callable[[str], None],
) -> None:
    """Run the startup scan (or ``--rescan`` repair scan); never fatal.

    A chain/scan/store failure prints a scrubbed warning (scan, chain,
    store, and key error strings are value-free by contract) and the
    REPL still starts — handlers surface the resulting store-empty
    states per turn. The summary lines carry counts and the tip height
    only — never addresses or amounts.
    """
    auto_scan = os.environ.get(AUTO_SCAN_ENV_VAR, "").strip() != "0"
    if rescan_requested:
        try:
            summary = wallet_scan.rescan_wallet(store, client, wallet)
        except (ChainError, wallet_scan.ScanError, WatchKeyError, StoreError, sqlite3.Error) as exc:
            output_fn(f"warning: rescan failed: {exc} — continuing with cached state.")
            return
        output_fn(_rescan_summary_line(summary))
        return
    if not auto_scan:
        return
    try:
        summary = wallet_scan.scan_wallet(store, client, wallet)
    except (ChainError, wallet_scan.ScanError, WatchKeyError, StoreError, sqlite3.Error) as exc:
        output_fn(f"warning: startup scan failed: {exc} — continuing with cached state.")
        return
    output_fn(
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
) -> None:
    """Read user lines until EOF/exit and print each turn's outcome.

    The flow/session pair is owned by this loop's caller (:func:`run`);
    every turn runs through :func:`_run_turn` so the confirm gate sees
    the raw utterance before the model does. Between turns the background
    watch (ADR-0019) is drained: a due poll cycle runs and its events are
    narrated before the next prompt (and their value-free count is recorded
    into the session summary, R13). ``client`` is forwarded to the drain so
    each cycle can compute the time-since-block narration suffix (NOTE-2)
    and to the ETA fact (TCK-P5-002).

    Transcript commands (OQ14, ADR-0020): lines beginning with ``/`` are
    deterministic UI commands, never model intents — ``/export <path>``
    writes a redacted transcript, ``/scrub`` clears the in-memory
    transcript/summary, ``/help`` lists them. There is no protocol change.
    """
    while True:
        watch_count = _drain_watch(watcher, output_fn, client=client)
        if watch_count:
            loop.record_event("watch_events", watch_count)
        try:
            line = input_fn("you> ")
        except EOFError:
            return
        line = line.strip()
        if not line:
            continue
        if line.lower() in ("exit", "quit"):
            return
        if line.startswith("/"):
            _handle_transcript_command(line, loop, output_fn)
            continue
        _run_turn(loop, flow, session, line, output_fn, client=client)


#: Fallback wording for an unparseable ``/`` command (value-free).
_TRANSCRIPT_HELP: Final[str] = (
    "Commands: /export <path> — write a redacted session transcript; "
    "/scrub — clear the in-memory transcript; /help — show this."
)


def _handle_transcript_command(
    command: str, loop: AgentLoop, output_fn: Callable[[str], None]
) -> None:
    """Handle an OQ14 transcript CLI command (``/export``, ``/scrub``, ``/help``).

    Deterministic UI features, NOT model intents (ADR-0020): no protocol,
    grammar, or prompt change. Output is short and plain.
    """
    parts = command.split(maxsplit=1)
    cmd = parts[0].lower()
    if cmd == "/help":
        output_fn(_TRANSCRIPT_HELP)
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


def _run_turn(
    loop: AgentLoop,
    flow: TxFlow,
    session: SendSession,
    line: str,
    output_fn: Callable[[str], None],
    *,
    client: EsploraClient | None = None,
) -> None:
    """Run ONE REPL turn: gate classification → agent → flow narration.

    Dual-key wiring (ADR-0013): at the top of the turn — BEFORE the
    model runs — a live pending transaction puts the user's utterance
    through :meth:`ConfirmGate.classify` and stores the decision on the
    session, so the ``confirm_tx`` handler (which runs inside
    ``loop.run``) consumes a gate decision from the SAME turn.

    - DENY while pending: the gate decision is authoritative — the flow
      is cancelled proactively (no model cancel intent is waited for)
      and the cancellation is narrated after the turn's own output.
    - AMBIGUOUS while pending: the turn proceeds normally and a guidance
      line asks the user to confirm or cancel explicitly.
    - CONFIRM while the flow is still CREATED after the turn (the model
      did not emit ``confirm_tx``): a guidance line — the flow is
      untouched.
    - NOT_A_DECISION: normal chat; a pending card simply stays pending.
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
    _print_turn(loop.run(line, facts), output_fn)
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


def _print_turn(turn: AgentTurnResult, output_fn: Callable[[str], None]) -> None:
    """Print one agent turn according to its status and intent.

    Every printed value comes verbatim from the handler result dict —
    the UI computes nothing (txid shortening is display truncation of
    tool output, per the narration contract). All strings pass through
    :func:`~localwallet.agent.context.sanitize_tool_output` immediately
    before printing (SR-006: the envelope grammar permits ``\\uXXXX`` so
    ESC/bidi control characters must never reach the terminal).
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
        _print_create_tx(turn.result or {}, output_fn)
    elif envelope.intent is IntentName.CONFIRM_TX:
        _print_confirm_tx(turn.result or {}, output_fn)
    elif envelope.intent is IntentName.SIGN_TX:
        _print_sign_tx(turn.result or {}, output_fn)
    elif envelope.intent is IntentName.BROADCAST_TX:
        _print_broadcast_tx(turn.result or {}, output_fn)
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
    """Print the balance verbatim from the handler's result dict."""
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


def _print_history(result: Mapping[str, object], output_fn: Callable[[str], None]) -> None:
    """Print history lines: ``tx <short-txid>… <direction> <height|unconfirmed>``.

    Address-free by contract (P1 narration): only txid/direction/height
    are shown; values are verbatim from the handler result dict.
    """
    if result.get("error") is not None:
        output_fn(sanitize_tool_output(_error_line(result, "History lookup failed")))
        return
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
    """Print one line per UTXO with the address verbatim from the result."""
    if result.get("error") is not None:
        output_fn(sanitize_tool_output(_error_line(result, "UTXO lookup failed")))
        return
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


def _print_create_tx(result: Mapping[str, object], output_fn: Callable[[str], None]) -> None:
    """Narrate a ``create_tx`` outcome (TCK-P2-004 confirmation-card UX).

    Success → the confirmation card. ``tx_pending`` → the refusal line
    plus the pending card re-rendered from the result's own fields.
    ``insufficient_funds`` → a friendly line built from the structured
    ``needed_sats``/``available_sats`` keys (user-facing amounts, per
    ADR-0012 — never a log-bound detail string). Everything else goes
    through :func:`_error_line` (value-free details).
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
        output_fn(
            sanitize_tool_output(
                "A transaction is already pending — confirm or cancel it first."
            )
        )
        _print_confirmation_card(result, output_fn)
        return
    if error is not None:
        output_fn(sanitize_tool_output(_error_line(result, "Could not create the transaction")))
        return
    output_fn(sanitize_tool_output(_CARD_HEADER_LINE))
    _print_confirmation_card(result, output_fn)


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
        rate_age = result.get("rate_age_s")
        if rate_age is not None:
            amount_line += f" · rate age {rate_age}s"
        if result.get("rate_stale"):
            amount_line += " · stale"
        amount_line += ")"
    output_fn(sanitize_tool_output(amount_line))
    output_fn(sanitize_tool_output(f"To: {result.get('recipient', '')}"))
    if "fee_sats" in result:
        fee_line = f"Fee: {result['fee_sats']} sats"
        fee_parts: list[str] = []
        if "fee_rate_sat_vb" in result:
            fee_parts.append(f"{result['fee_rate_sat_vb']} sat/vB")
        if "fee_target" in result:
            fee_parts.append(f"{result['fee_target']} target")
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
    satisfied…", "tx_ref does not match…"). A confirmed flow prints the
    Phase 3 handoff note. The PSBT payload from the result is never
    printed.
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
        output_fn(sanitize_tool_output(_CONFIRMED_LINE))
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


def _print_broadcast_tx(result: Mapping[str, object], output_fn: Callable[[str], None]) -> None:
    """Narrate a ``broadcast_tx`` outcome (TCK-P3-005).

    Success → "Sent! txid <txid> — tracking…" with the txid verbatim from
    the chain response. ``broadcast_failed`` keeps the signed transaction
    and says so (single-attempt POST policy: retrying is explicit).
    Refusals and other errors surface value-free; a ``store_warning`` is
    printed after the success line (bookkeeping failed, broadcast didn't).
    The tx hex never appears in any line.
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
        output_fn(
            sanitize_tool_output(f"Sent! txid {result.get('txid', '')} — tracking…")
        )
        warning = result.get("store_warning")
        if isinstance(warning, str) and warning.strip():
            output_fn(sanitize_tool_output(f"warning: {warning}"))
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

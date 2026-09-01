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
  unchanged, and the send flow runs the dispatcher-owned state machine:
  ``create_tx`` resolves the amount (sats, or USD via the price oracle),
  estimates the fee, selects coins and builds the unsigned PSBT via the
  pure tx engine, then stages a :class:`~localwallet.tx.flow.PendingTx`;
  ``confirm_tx`` moves the flow CREATED → CONFIRMED only under the
  dual-key rule (ADR-0013): a matching ``tx_ref`` AND a CONFIRM
  classification of the SAME turn's user utterance by the deterministic
  :class:`~localwallet.tx.flow.ConfirmGate` — an LLM "yes" never counts.
- :func:`run` / :func:`main` — CLI wiring: read the watch-only key from
  ``--zpub`` or ``LOCALWALLET_ZPUB``, parse + gate it (testnet-only,
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
import json
import os
import re
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from string import punctuation
from typing import Final

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
    PriceOracle,
    PriceUnavailableError,
)
from localwallet.config import Settings
from localwallet.protocol import (
    ClarifyParams,
    ConfirmTxParams,
    CreateTxParams,
    DispatchTable,
    Envelope,
    GetHistoryParams,
    Handler,
    IntentName,
    NewAddressParams,
    RespondParams,
)
from localwallet.store import (
    ADDRESS_ALLOCATED,
    BRANCH_CHANGE,
    AddressRecord,
    Store,
    StoreError,
    WalletRecord,
)
from localwallet.tx.flow import (
    PENDING_TTL_S,
    ConfirmGate,
    FlowError,
    GateDecision,
    TxFlow,
    TxFlowStatus,
)
from localwallet.tx.psbt import PsbtError, PsbtInputSource, build_unsigned_psbt, psbt_to_base64
from localwallet.tx.selection import InsufficientFundsError, SelectionError, select_coins
from localwallet.wallet import scan as wallet_scan
from localwallet.wallet.derivation import BranchDeriver
from localwallet.wallet.descriptor import (
    SCRIPT_PURPOSES,
    TESTNET_COIN_TYPE,
    ParsedKey,
    WalletDescriptor,
    WatchKeyError,
    parse_wallet_key,
)

__all__ = [
    "AUTO_SCAN_ENV_VAR",
    "DEFAULT_HISTORY_LIMIT",
    "OUT_OF_WINDOW_NOTICE",
    "PRIVACY_INDICATOR",
    "ZPUB_ENV_VAR",
    "SendSession",
    "build_dispatch_table",
    "main",
    "run",
    "stub_generate",
]

#: Environment variable supplying the watch-only account key
#: (``--zpub`` overrides it).
ZPUB_ENV_VAR: Final[str] = "LOCALWALLET_ZPUB"

#: Environment variable opting out of the startup scan (``"0"`` disables;
#: any other value — including unset — keeps the default on).
AUTO_SCAN_ENV_VAR: Final[str] = "LOCALWALLET_AUTO_SCAN"

#: History entries returned when the model omits ``params.limit``
#: (protocol contract, ADR-0002 v0 extensions).
DEFAULT_HISTORY_LIMIT: Final[int] = 20

#: Sort sentinel so unconfirmed transactions (``height=None``) sort as the
#: newest entries — above any real block height / epoch timestamp.
_NEVER_CONFIRMED: Final[int] = 2**63 - 1

#: The §9 honest privacy indicator, shown verbatim at startup (PROJECT.md
#: §9 / R7 — never over-claim privacy while querying a public explorer).
PRIVACY_INDICATOR: Final[str] = (
    "Querying public mempool.space — the operator can associate queried "
    "addresses with your IP."
)

#: ADR-0009 UI surfacing for ``sync_state["out_of_window_detected"]``:
#: printed at startup when the store carries a non-empty warning payload.
#: Generic scrubbed wording (indexes only in the payload; no addresses).
OUT_OF_WINDOW_NOTICE: Final[str] = (
    "note: usage was found beyond your usual address window — a rescan is "
    "recommended; say 'rescan' is not available yet, restart with --rescan"
)

_BANNER_TITLE: Final[str] = (
    "local-wallet — watch-only Bitcoin wallet (testnet, Phase 1 wallet engine)"
)
_BANNER_TESTNET: Final[str] = (
    "Network: Bitcoin TESTNET only — mainnet keys are refused until Phase 6."
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
#: first receive address of the fixture vpub used throughout the tests).
#: Deterministic canned data for ``--stub-llm`` only — never a real payee.
_STUB_RECIPIENT: Final[str] = "tb1q3f0w5yzgvcpp9akt4sfad764dvthz6qzv0xlfh"

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

#: User-facing narration lines for the send flow (TCK-P2-004). Every value
#: they carry comes verbatim from the handler result dict — the UI computes
#: nothing (integer division/formatting of result values only, the same
#: display-truncation class as txid shortening).
_CARD_HEADER_LINE: Final[str] = (
    "Pending transaction — review it carefully, then say 'confirm' or 'cancel':"
)
_CANCELLED_LINE: Final[str] = "Transaction cancelled."
_CONFIRMED_LINE: Final[str] = (
    "Approved. The signed-transaction step arrives in Phase 3 — say 'status' later."
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
    address" / "address" → ``new_address``; a send request ("send … to
    tb1…") → ``create_tx`` (the ``tb1…`` token and the ``<n> sats`` /
    ``$<n>`` figure are extracted verbatim from the user turn, with the
    canned fixture recipient / a canned 10000-sat amount as fallbacks);
    a confirmation utterance ("confirm", "yes", …) → ``confirm_tx``;
    anything else → a canned ``respond``. ``grammar_text`` is accepted
    for :data:`~localwallet.agent.runtime.GenerateFn` compatibility and
    ignored.

    Dev-mode caveats (by design, documented): the canned ``confirm_tx``
    carries a placeholder ``tx_ref`` that cannot match a real pending
    reference — the flow refuses it, which demonstrates the dual-key
    refusal path. Deterministic tests do NOT rely on the stub for the
    happy path; they inject generate closures that quote the flow's real
    pending ``tx_ref``. Everything the stub extracts from user text is
    untrusted input like any model output: it flows through the full
    3-layer validation before any handler runs.

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
    if "send" in user_turn and "tb1" in user_turn:
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
    whitespace token starting with ``tb1`` (edge punctuation stripped),
    falling back to the canned fixture address; the amount is the first
    ``<n> sats`` figure, else the first ``$<n>`` figure, else the canned
    sats fallback. The output is an ordinary model-output document — it
    must pass the same validation as the real model's envelope.
    """
    recipient = _STUB_RECIPIENT
    for token in user_turn.split():
        candidate = token.strip(punctuation)
        if candidate.startswith("tb1") and len(candidate) > 3:
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
) -> DispatchTable:
    """Build the allowlist dispatch table for the running app.

    Args:
        store: The open persistence layer every handler reads.
        wallet: The active wallet row (ADR-0010: exactly one profile).
        parsed: The wallet's parsed account key (for ``new_address``
            derivation and the send flow's PSBT account fields; public
            key only).
        client: The chain client — used only by ``scan_fn`` (the lazy
            first scan inside ``get_balance``/``create_tx``) and by the
            fee/price wrappers below; the read handlers never touch it.
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

    Returns:
        A :class:`~localwallet.protocol.DispatchTable` covering the whole
        closed intent enum.
    """
    wallet_id = wallet.id
    # One shared flow/session pair: the create and confirm handlers must
    # see the SAME dispatcher-owned state machine (never two instances).
    tx_flow = flow if flow is not None else TxFlow()
    send_session = session if session is not None else SendSession()
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
        ),
        IntentName.CONFIRM_TX: _make_confirm_tx_handler(tx_flow, send_session),
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


def _tx_pending_result(flow: TxFlow) -> dict[str, object]:
    """The ``tx_pending`` refusal result, carrying the pending card fields.

    Surfaced when ``create_tx`` arrives while a transaction is already
    pending (ADR-0013: at most one pending transaction; a stale one is
    recovered explicitly, never reaped). The pending card is re-shown
    from the flow's own record so the user can act on it; rate fields
    are unknown on re-show (``usd_cents=None``) and ``expires_in_s``
    keeps the card's nominal TTL label.
    """
    result: dict[str, object] = {"error": "tx_pending"}
    pending = flow.pending
    if pending is not None:
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
                "expires_in_s": PENDING_TTL_S,
            }
        )
    return result


def _make_create_tx_handler(
    store: Store,
    wallet_id: int,
    parsed: ParsedKey,
    flow: TxFlow,
    fee_estimator: FeeEstimator,
    price_oracle: PriceOracle,
    scan_fn: Callable[[], object],
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
            return _tx_pending_result(flow)

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
        # testnet witness-v0 P2WPKH bech32 string; containment anyway).
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
                account_path=(purpose + 2**31, TESTNET_COIN_TYPE + 2**31, 2**31),
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
            return _tx_pending_result(flow)

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
) -> int:
    """Wire the application from ``argv``/environment and run the REPL.

    Configuration precedence: ``--zpub`` overrides ``LOCALWALLET_ZPUB``;
    the model runtime is picked as remote debug bridge
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
    ``tx_ref``, which the canned stub cannot know).

    Args:
        argv: CLI arguments (defaults to ``sys.argv[1:]``).
        input_fn: REPL line reader (``input``-compatible; test seam).
        output_fn: REPL/banner writer (``print``-compatible; test seam).
        flow: The :class:`TxFlow` for the send flow (test seam; a fresh
            real-clock instance by default).
        generate_fn: A bare ``generate(prompt, grammar) -> str`` model
            callable used as-is when provided (test seam).

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
        # Gated parse (testnet gate enforced at parse time — P0 SR
        # carry-over) plus the canonical wallet descriptor. Both raise
        # value-free WatchKeyErrors.
        parsed = parse_wallet_key(zpub)
        descriptor = WalletDescriptor.from_key(zpub)
    except WatchKeyError as exc:
        output_fn(f"Watch key rejected: {exc}")
        return 2

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
        base_url=settings.esplora_base_url,
        timeout_s=settings.request_timeout_s,
        max_retries=settings.max_retries,
    )
    # Fee/price wrappers share the ONE chain client (no second transport);
    # construction is network-free — they fetch lazily, per their TTLs.
    fee_estimator = FeeEstimator(client)
    price_oracle = PriceOracle(client)
    tx_flow = flow if flow is not None else TxFlow()

    output_fn(_BANNER_TITLE)
    output_fn(_BANNER_TESTNET)
    output_fn(f"Privacy notice: {PRIVACY_INDICATOR}")
    output_fn("Type a message — 'exit' or Ctrl-D quits.")

    _startup_scan(store, client, wallet_row, rescan_requested=args.rescan, output_fn=output_fn)
    out_of_window = _out_of_window_line(store, wallet_row.id)
    if out_of_window is not None:
        output_fn(out_of_window)

    session = SendSession()
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
    )
    loop = AgentLoop(generate, table)

    try:
        _repl(loop, output_fn, input_fn, flow=tx_flow, session=session)
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
    )


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
            "Watch-only testnet Bitcoin wallet driven by a local LLM "
            "(Phase 1: wallet engine)."
        ),
    )
    parser.add_argument(
        "--zpub",
        help=(
            "account-level watch-only extended public key "
            "(vpub/upub/tpub for testnet); overrides LOCALWALLET_ZPUB"
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
    return parser.parse_args(list(argv) if argv is not None else None)


def _repl(
    loop: AgentLoop,
    output_fn: Callable[[str], None],
    input_fn: Callable[[str], str],
    *,
    flow: TxFlow,
    session: SendSession,
) -> None:
    """Read user lines until EOF/exit and print each turn's outcome.

    The flow/session pair is owned by this loop's caller (:func:`run`);
    every turn runs through :func:`_run_turn` so the confirm gate sees
    the raw utterance before the model does.
    """
    while True:
        try:
            line = input_fn("you> ")
        except EOFError:
            return
        line = line.strip()
        if not line:
            continue
        if line.lower() in ("exit", "quit"):
            return
        _run_turn(loop, flow, session, line, output_fn)


def _run_turn(
    loop: AgentLoop,
    flow: TxFlow,
    session: SendSession,
    line: str,
    output_fn: Callable[[str], None],
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
    _print_turn(loop.run(line, {}), output_fn)
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
        f"Balance (testnet): {confirmed} sats (confirmed) + {unconfirmed} sats (unconfirmed)"
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
    """
    amount_line = f"Amount: {result.get('amount_sats', 0)} sats"
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
    output_fn(
        sanitize_tool_output(
            f"Fee: {result.get('fee_sats', 0)} sats "
            f"({result.get('fee_rate_sat_vb', 0)} sat/vB, {result.get('fee_target', '')} target)"
        )
    )
    output_fn(sanitize_tool_output(f"Size: {result.get('vsize', 0)} vB"))
    output_fn(sanitize_tool_output(f"Inputs: {result.get('inputs_count', 0)}"))
    change = result.get("change_sats")
    change_label = f"Change: {change} sats" if change is not None else "Change: none"
    output_fn(sanitize_tool_output(change_label))
    expires_in_s = result.get("expires_in_s", 0)
    output_fn(sanitize_tool_output(f"Expires: ~{int(expires_in_s) // 60} min"))
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

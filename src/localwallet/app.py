"""Application wiring: agent loop → allowlist dispatch → wallet handlers.

Phase 1 wiring (TCK-P1-004). This module only glues existing pieces
together — it implements no protocol, wallet, or chain logic itself:

- :func:`build_dispatch_table` — the allowlist dispatch table (closed
  intent enum → handlers). Every handler reads the SQLite store
  (populated by :func:`localwallet.wallet.scan.scan_wallet`):
  ``get_balance`` sums the cached UTXO snapshot, ``get_history`` /
  ``get_utxos`` project cached rows, ``new_address`` allocates the next
  derivation index (store bookkeeping + pure derivation — never
  network), and ``respond``/``clarify`` pass the model's text through
  unchanged.
- :func:`run` / :func:`main` — CLI wiring: read the watch-only key from
  ``--zpub`` or ``LOCALWALLET_ZPUB``, parse + gate it (testnet-only,
  value-free errors → config-error exit 2), open the store
  (:class:`~localwallet.config.Settings` ``store_path``), reuse or
  create the single wallet profile (descriptor-match guard, ADR-0010),
  pick the model runtime (remote debug bridge → local GGUF →
  ``--stub-llm``), run the startup scan (or ``--rescan``; env opt-out
  via ``LOCALWALLET_AUTO_SCAN=0``) and the chat REPL.

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
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Final

from localwallet.agent.context import sanitize_tool_output
from localwallet.agent.loop import AgentLoop, AgentTurnResult, AgentTurnStatus
from localwallet.agent.remote_runtime import (
    LLM_BASE_URL_ENV_VAR,
    LLM_MODEL_ENV_VAR,
    RemoteOpenAIRuntime,
    debug_notice,
)
from localwallet.agent.runtime import MODEL_PATH_ENV_VAR, GenerateFn, ModelRuntime
from localwallet.chain import ChainError, EsploraClient
from localwallet.config import Settings
from localwallet.protocol import (
    ClarifyParams,
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
    AddressRecord,
    Store,
    StoreError,
    WalletRecord,
)
from localwallet.wallet import scan as wallet_scan
from localwallet.wallet.derivation import BranchDeriver
from localwallet.wallet.descriptor import (
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
    address" / "address" → ``new_address``; anything else → a canned
    ``respond``. ``grammar_text`` is accepted for
    :data:`~localwallet.agent.runtime.GenerateFn` compatibility and
    ignored.

    Args:
        prompt: The fully assembled agent prompt.
        grammar_text: The envelope GBNF grammar (ignored by the stub).

    Returns:
        A canned envelope JSON document (untrusted-input contract still
        applies: it flows through ``handle_raw`` like any model output).
    """
    del grammar_text
    user_turn = prompt.rsplit("user: ", 1)[-1].lower()
    if "balance" in user_turn:
        return _STUB_BALANCE_ENVELOPE
    if "history" in user_turn or "transaction" in user_turn:
        return _STUB_HISTORY_ENVELOPE
    if "utxo" in user_turn:
        return _STUB_UTXOS_ENVELOPE
    if "address" in user_turn:
        return _STUB_NEW_ADDRESS_ENVELOPE
    return _STUB_RESPOND_ENVELOPE


def build_dispatch_table(
    store: Store,
    wallet: WalletRecord,
    parsed: ParsedKey,
    client: EsploraClient,
    scan_fn: Callable[[], object],
) -> DispatchTable:
    """Build the allowlist dispatch table for the running app.

    Args:
        store: The open persistence layer every handler reads.
        wallet: The active wallet row (ADR-0010: exactly one profile).
        parsed: The wallet's parsed account key (for ``new_address``
            derivation; public key only).
        client: The chain client — used only by ``scan_fn`` (the lazy
            first scan inside ``get_balance``); the read handlers never
            touch it.
        scan_fn: Zero-argument callable performing one wallet scan
            (``scan_wallet(store, client, wallet)`` in production). Used
            lazily by ``get_balance`` when the store has no sync cursor.

    Returns:
        A :class:`~localwallet.protocol.DispatchTable` covering the whole
        closed intent enum.
    """
    wallet_id = wallet.id
    return {
        IntentName.RESPOND: _respond_handler,
        IntentName.CLARIFY: _clarify_handler,
        IntentName.GET_BALANCE: _make_get_balance_handler(
            store, wallet_id, scan_fn
        ),
        IntentName.GET_HISTORY: _make_get_history_handler(store, wallet_id),
        IntentName.GET_UTXOS: _make_get_utxos_handler(store, wallet_id),
        IntentName.NEW_ADDRESS: _make_new_address_handler(store, wallet_id, parsed),
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


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point; delegates to :func:`run`."""
    return run(argv)


def run(
    argv: Sequence[str] | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
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
    the first balance lookup then scans lazily. Chain/store failures at
    startup print a scrubbed warning and the REPL still starts; handlers
    surface store-empty / chain-down states per turn.

    Args:
        argv: CLI arguments (defaults to ``sys.argv[1:]``).
        input_fn: REPL line reader (``input``-compatible; test seam).
        output_fn: REPL/banner writer (``print``-compatible; test seam).

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
    if remote_base_url:
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

    output_fn(_BANNER_TITLE)
    output_fn(_BANNER_TESTNET)
    output_fn(f"Privacy notice: {PRIVACY_INDICATOR}")
    output_fn("Type a message — 'exit' or Ctrl-D quits.")

    _startup_scan(store, client, wallet_row, rescan_requested=args.rescan, output_fn=output_fn)
    out_of_window = _out_of_window_line(store, wallet_row.id)
    if out_of_window is not None:
        output_fn(out_of_window)

    table = build_dispatch_table(
        store,
        wallet_row,
        parsed,
        client,
        lambda: wallet_scan.scan_wallet(store, client, wallet_row),
    )
    loop = AgentLoop(generate, table)

    try:
        _repl(loop, output_fn, input_fn)
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
) -> None:
    """Read user lines until EOF/exit and print each turn's outcome."""
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
        _print_turn(loop.run(line, {}), output_fn)


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
    else:  # pragma: no cover — closed intent enum
        output_fn(sanitize_tool_output(_GENERIC_FAILURE))


def _error_line(result: Mapping[str, object], label: str) -> str:
    """Render a handler ``{"error": ..., "detail": ...}`` result for the UI.

    ``chain_unavailable`` keeps its human wording; other codes print as-is.
    Details are scrubbed by their layers (value-free of
    addresses/txids/amounts) and are safe to surface verbatim.
    """
    error = str(result.get("error", "error"))
    human = "chain unavailable" if error == "chain_unavailable" else error
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

"""Gap-limited chain scan and cache orchestration (TCK-P1-002).

Scans a watch-only wallet against an :class:`~localwallet.chain.EsploraClient`
(the chain module is the only networked code; this module performs no I/O
itself), and persists the result — derivation state, address statuses, UTXO
snapshot, transaction history, and sync cursors — in a single atomic store
transaction (:meth:`localwallet.store.Store.persist_scan_result`).

Phase split (ADR-0022, TCK-SCAN-003): a scan is three phases so chain I/O
can leave the engine thread — ``plan_scan`` reads everything the walk
needs FROM the store (engine thread) into an immutable :class:`ScanPlan`;
``fetch_scan`` derives + fetches against the chain client and returns an
immutable :class:`ScanRecords` (this phase runs on the dedicated chain
worker — it receives a plan, NEVER a ``Store``); ``persist_scan`` hands
the records to the single atomic store transaction on the ENGINE thread.
:func:`scan_wallet`/:func:`rescan_wallet` compose all three on the
calling thread and are behavior-identical to the pre-split scan.

Scan algorithm (per branch, 0 = receive then 1 = change, sequential — no
concurrency in v1)
-------------------------------------------------------------------
For each branch, indices are walked in ascending order starting at 0.
For every index the address is *ensured* in the store — derived from the
wallet key when missing; a rescan always re-derives the mapping from the
key instead of trusting cached rows — its history is fetched with
``get_address_txs``, and any transaction marks the address used. The walk
stops after ``gap_limit`` consecutive unused addresses, so the scanned
window is ``[0, last_used_index + gap_limit]``: usage at index 3 with the
default gap of 20 stops at index 23. Afterwards the UTXO endpoint is
queried for every window address whose ``/txs`` result was non-empty and
the union becomes the new wallet-wide UTXO snapshot (replaced wholesale
inside the atomic persist — the previous set is fully replaced).
Addresses with an empty history are skipped (TCK-SCAN-001): an address
with no transactions (confirmed *or* mempool — the txs listing includes
unconfirmed entries) cannot hold UTXOs, so their ``/utxo`` fetch would
always return ``[]``. Every probed address is still derived, windowed,
and persisted exactly as before — only the redundant HTTP call is
dropped (fresh 2-branch wallet at gap 20: 40 utxo calls saved per scan).

Any address that can still hold a live UTXO appears inside the window:
its funding transaction shows up in its history, marks it used, and
extends the walk — so the snapshot cannot silently drop spendable coins
on addresses cached beyond the current window.

Ordering (rate-limit friendly): strictly sequential; per branch indices
ascend, transactions are fetched before UTXOs, branch 0 runs before
branch 1. Exactly one txs call per scanned window address, and one utxo
call per window address whose txs result was non-empty (TCK-SCAN-001).

Absolute per-branch window ceiling (TCK-SEC-002): the gap-limited walk
above is unbounded when usage itself is attacker-driven — an observer of
the public watch-only xpub can fund consecutive derivable indices
0, 1, 2, … N on the public chain and force N + gap probes per branch per
scan, re-triggered by the background watch. The walk therefore NEVER
derives or probes beyond ``_MAX_WINDOW_ADDRESSES`` indices per branch,
regardless of usage. When the ceiling is reached the scan result is
explicitly marked truncated (``ScanSummary.truncated`` /
``BranchScanSummary.truncated``; the per-branch ceiling stop is also
visible as ``window_last_index``) — never silent. As a consequence the
request budget per scan is bounded: at most ``_MAX_WINDOW_ADDRESSES``
txs probes plus at most ``_MAX_WINDOW_ADDRESSES`` utxo probes per
branch (≤ 2 × ``_MAX_WINDOW_ADDRESSES`` × ``len(BRANCHES)`` probes
total), and no retry path can exceed it because each window address is
queried exactly once (see "Ordering" below).

Phase split (ADR-0022, TCK-SCAN-003): a scan runs in three phases —
``plan_scan`` reads everything the walk needs FROM the store (engine
thread), ``fetch_scan`` derives + fetches the chain data and builds the
immutable record set (runs on the dedicated chain worker — it receives a
:class:`ScanPlan`, never a ``Store``), and ``persist_scan`` hands the
:class:`ScanRecords` to the single atomic store transaction (engine
thread only). :func:`scan_wallet`/:func:`rescan_wallet` compose all three
on the calling thread and are behavior-identical to the pre-split scan.

Failure semantics: the **entire chain phase runs before any
persistence.** A scan that fails midway (transport error, malformed
payload) raises and leaves the store untouched — fail closed. The persist
phase itself is a **single atomic store transaction**
(:meth:`localwallet.store.Store.persist_scan_result`): derivation state,
address statuses, the UTXO snapshot, transaction history, sync cursors,
and the ``out_of_window_detected`` write commit together or roll back
entirely, so a crash mid-persist can never desync address statuses from
the derivation cursor or sync state. Store-level failures during the
persist phase therefore leave the prior store state fully intact and
propagate as ``StoreError``/``StoreIntegrityError`` (deliberately *not*
wrapped in :class:`ScanError` — the documented store contract). Missing
``tx.fee`` is tolerated (``fee_sats=None``, nullable by design); missing
or malformed ``txid``/``status`` shapes fail closed. Every error is
value-free (no addresses, txids, or amounts in messages) and no logging
is performed.

Gap policy and rescans: see ``docs/adr/0009-gap-policy.md``. The default
gap is 20 (BIP44 convention), configurable via the ``gap_limit`` setting.
:meth:`rescan_wallet` is the cache-repair path: it re-derives every
window address mapping from the key (never trusting cached rows),
recomputes the derivation cursor from chain truth
(``next_index = max_used_index + 1``), and takes a fresh UTXO snapshot.
When a scan or rescan finds usage beyond the previously recorded scan
window (addresses imported elsewhere — risk R3), the fact is persisted
in ``sync_state`` under ``out_of_window_detected`` (JSON) for the UI to
surface; nothing is ever silently guessed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Final

from localwallet.chain import EsploraClient
from localwallet.store import (
    ADDRESS_ALLOCATED,
    ADDRESS_UNUSED,
    ADDRESS_USED,
    DIR_IN,
    DIR_OUT,
    DIR_SELF,
    AddressRecord,
    DerivationRecord,
    Store,
    TxRecord,
    UtxoRecord,
    WalletRecord,
)
from localwallet.wallet.derivation import BranchDeriver
from localwallet.wallet.descriptor import ParsedKey, WalletDescriptor

__all__ = [
    "BRANCHES",
    "DEFAULT_GAP_LIMIT",
    "GAP_LIMIT_SETTING",
    "OUT_OF_WINDOW_KEY",
    "BranchScanSummary",
    "ScanError",
    "ScanPlan",
    "ScanRecords",
    "ScanSummary",
    "fetch_scan",
    "persist_scan",
    "plan_scan",
    "rescan_wallet",
    "scan_wallet",
]

#: Branches scanned, in order (BIP44 external/internal).
BRANCHES: Final[tuple[int, ...]] = (0, 1)

#: Default BIP44-style gap limit (ADR-0009).
DEFAULT_GAP_LIMIT: Final[int] = 20

#: Settings key holding the gap limit as a decimal string.
GAP_LIMIT_SETTING: Final[str] = "gap_limit"

#: sync_state key for the beyond-window usage warning (ADR-0009, R3).
OUT_OF_WINDOW_KEY: Final[str] = "out_of_window_detected"

#: sync_state keys written by every completed scan.
CURSOR_KEY: Final[str] = "last_scan_cursor"
TIP_KEY: Final[str] = "last_tip_height"
SCAN_AT_KEY: Final[str] = "last_scan_at"

#: Bounds for the gap limit (a gap below 1 would stop before scanning;
#: the upper bound caps queries per scan).
_MIN_GAP: Final[int] = 1
_MAX_GAP: Final[int] = 1000

#: Absolute per-branch ceiling on the scan window (TCK-SEC-002). The
#: gap-limited walk terminates on ``gap`` consecutive unused addresses,
#: which is unbounded when usage is attacker-driven (an observer of the
#: public xpub can fund every consecutive derivable index); this constant
#: bounds the window — and therefore the probe budget — regardless of
#: usage. Value: 1000 ≈ 50 × default gap of 20 (ADR-0009), matching the
#: per-call derivation batch bound (``wallet.derivation._MAX_DERIVE_COUNT``)
#: and the ``gap_limit`` setting upper bound; a single-sig watch-only
#: branch with >1000 used addresses is far outside the v1 target wallet,
#: and hitting the ceiling is surfaced via ``truncated`` rather than
#: silently under-scanning. The walk never derives or probes index
#: ≥ ``_MAX_WINDOW_ADDRESSES``: at most ``_MAX_WINDOW_ADDRESSES`` txs
#: probes + ``_MAX_WINDOW_ADDRESSES`` utxo probes per branch, i.e.
#: ≤ 2 × ``_MAX_WINDOW_ADDRESSES`` × ``len(BRANCHES)`` network calls per
#: scan, whatever the usage pattern.
_MAX_WINDOW_ADDRESSES: Final[int] = 1000

#: A txid is always a 64-character hex string.
_TXID_CHARS: Final[frozenset[str]] = frozenset("0123456789abcdefABCDEF")


class ScanError(Exception):
    """A scan could not be completed, or chain data failed validation.

    Raised (fail closed) for malformed chain payloads, malformed
    settings/sync-state values, and unknown wallets.

    Message contract: value-free — never contains an address, txid,
    amount, or key material. Chain transport failures surface as the
    chain module's own :class:`~localwallet.chain.ChainError`.
    """


@dataclass(frozen=True, slots=True)
class BranchScanSummary:
    """Per-branch outcome of one scan."""

    branch: int
    #: Number of window addresses probed (txs each; utxo only for
    #: addresses with a non-empty txs result, TCK-SCAN-001).
    scanned: int
    #: Highest index scanned (the stop index).
    window_last_index: int
    #: Highest index with observed transactions (-1 if none).
    max_used_index: int
    #: Derivation cursor persisted after the scan.
    next_index: int
    #: Indices observed with transactions, ascending.
    used_indices: tuple[int, ...]
    #: True when the walk stopped at the absolute window ceiling
    #: (``_MAX_WINDOW_ADDRESSES``) instead of the gap condition — usage
    #: may exist beyond the scanned window; never silent (TCK-SEC-002).
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ScanSummary:
    """Outcome of one wallet scan (or rescan)."""

    wallet_id: int
    gap_limit: int
    tip_height: int
    scanned_at: str
    branches: dict[int, BranchScanSummary] = field(default_factory=dict)
    #: Number of UTXOs in the persisted snapshot.
    utxo_count: int = 0
    #: Total value of the persisted snapshot in sats (confirmed +
    #: unconfirmed). Returned to the caller for narration — never logged.
    utxo_value_sats: int = 0
    #: Branch (as str, JSON-friendly) → detail for usage found beyond the
    #: previous window; empty when none was detected.
    out_of_window: dict[str, dict[str, int]] = field(default_factory=dict)
    #: True when ANY branch walk hit the absolute window ceiling
    #: (TCK-SEC-002); per-branch detail on ``branches[b].truncated``.
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ScanPlan:
    """Everything the derive+fetch phase needs that comes from the store.

    Built by :func:`plan_scan` ON the engine thread (ADR-0022 decision 2):
    the chain worker receives this immutable snapshot instead of a
    ``Store``, so no thread other than the engine can touch the
    engine-owned sqlite connection. The snapshot is NOT guaranteed to
    equal a mid-flight read: ADR-0022 decision 6 legalizes ``new_address``
    allocations on the engine thread WHILE a non-blocking fetch is in
    flight, and those land in the store after this snapshot was taken.
    :func:`persist_scan` (engine thread, persist time) reconciles the
    records built from the snapshot against the live store before
    writing — the derivation cursor is floored against the live
    allocated/used rows (no mid-scan allocation is ever re-issued) and
    a mid-scan ``allocated`` row is never downgraded."""

    wallet_id: int
    descriptor: WalletDescriptor
    gap: int
    #: True for the ``rescan`` trust model (re-derive, never cache-trust).
    rebuild: bool
    #: branch → index → cached address row (snapshot at plan time).
    existing: Mapping[int, Mapping[int, AddressRecord]]
    #: Address strings in ``allocated`` status (preserved by a rescan).
    allocated: frozenset[str]
    #: Previous scan-window cursor (``_read_previous_cursor``), or ``None``.
    previous_cursor: Mapping[int, int] | None
    #: Cached derivation cursor per branch (non-rebuild only; empty for a
    #: rebuild, whose cursor is recomputed from chain truth + allocations).
    derivation_next: Mapping[int, int]


@dataclass(frozen=True, slots=True)
class ScanRecords:
    """Immutable persist-phase record set produced by :func:`fetch_scan`.

    The chain worker's whole output (ADR-0022 decisions 2/3): data only —
    the engine thread hands it to :meth:`Store.persist_scan_result` (via
    :func:`persist_scan`), the single atomic transaction. The rows
    themselves are frozen dataclasses; nothing here can reach sqlite."""

    summary: ScanSummary
    wallet_id: int
    address_rows: tuple[AddressRecord, ...]
    derivation_states: tuple[DerivationRecord, ...]
    utxo_snapshot: tuple[UtxoRecord, ...]
    tx_rows: tuple[TxRecord, ...]
    sync_state_updates: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _RawTx:
    """Validated shape of one Esplora address-txs entry."""

    txid: str
    height: int | None
    block_time: int | None
    fee_sats: int | None
    #: Addresses seen in inputs (empty for coinbase / non-standard inputs).
    spends: frozenset[str]
    #: Addresses seen in outputs.
    receives: frozenset[str]


# ------------------------------------------------------------------ public


def scan_wallet(
    store: Store,
    client: EsploraClient,
    wallet: WalletRecord | WalletDescriptor,
    *,
    gap_limit: int | None = None,
    progress_fn: Callable[[], None] | None = None,
) -> ScanSummary:
    """Scan ``wallet`` and refresh the cached state in ``store``.

    Trusts the cached address mappings (existing rows are reused and the
    window extended as needed) and never lowers the cached derivation
    cursor below its current value. See module docstring for the
    algorithm and failure semantics.

    Composition of the ADR-0022 phases on the calling thread
    (:func:`plan_scan` → :func:`fetch_scan` → :func:`persist_scan`); the
    app's chain-worker paths run those phases separately instead (fetch
    off the engine thread), so behavior — including the
    chain-phase-before-persistence failure semantics — is identical.

    Args:
        store: The persistence layer (an open :class:`Store`).
        client: The chain adapter (the only networked component).
        wallet: The wallet row (its descriptor string is parsed) or an
            already-built :class:`WalletDescriptor` (matched against a
            wallet row by descriptor string).
        gap_limit: Gap override; ``None`` reads the ``gap_limit`` setting
            and falls back to :data:`DEFAULT_GAP_LIMIT`.
        progress_fn: Optional zero-argument callback (TCK-UX-001) invoked
            exactly once per address probed by the walk — a bare tick that
            receives NO address/index/amount data (value-free by
            construction). ``None`` (the default) leaves behavior and
            output byte-identical to the pre-callback scan.

    Returns:
        A :class:`ScanSummary` describing the scan.

    Raises:
        WatchKeyError: the wallet descriptor fails the parse-time gates.
        ChainError: a chain query failed (store left untouched).
        ScanError: malformed chain payload, malformed gap/sync-state
            settings, or no matching wallet row (store left untouched).
        StoreError: the atomic persist failed (all-or-nothing rollback —
            the prior store state is fully intact and the scan can be
            retried). Deliberately *not* wrapped in :class:`ScanError`;
            :class:`StoreIntegrityError` is a subclass. Messages are
            value-free.
    """
    plan = plan_scan(store, wallet, gap_limit=gap_limit, rebuild=False)
    return persist_scan(store, fetch_scan(plan, client, progress_fn=progress_fn))


def rescan_wallet(
    store: Store,
    client: EsploraClient,
    wallet: WalletRecord | WalletDescriptor,
    *,
    gap_limit: int | None = None,
    progress_fn: Callable[[], None] | None = None,
) -> ScanSummary:
    """Full rescan: re-derive everything from the key, ignore cached state.

    The cache-repair path (Phase 1 AC: "rescan fixes a simulated stale
    cache"): every window address mapping is re-derived from the wallet
    key (cached rows are overwritten, never trusted), the derivation
    cursor is recomputed purely from chain truth
    (``next_index = max_used_index + 1``), and the UTXO snapshot is
    replaced wholesale. Rows carrying the ``allocated`` status are
    preserved by address string so a rescan does not silently re-issue
    addresses the user already received. Otherwise identical to
    :meth:`scan_wallet` (including the chain-phase-before-persistence
    failure semantics). The optional ``progress_fn`` behaves exactly as in
    :meth:`scan_wallet` (one bare tick per probed address; ``None`` = no
    callback, unchanged behavior).
    """
    plan = plan_scan(store, wallet, gap_limit=gap_limit, rebuild=True)
    return persist_scan(store, fetch_scan(plan, client, progress_fn=progress_fn))


def plan_scan(
    store: Store,
    wallet: WalletRecord | WalletDescriptor,
    *,
    gap_limit: int | None = None,
    rebuild: bool = False,
) -> ScanPlan:
    """Read the store inputs a scan needs into an immutable plan.

    ENGINE-THREAD phase (ADR-0022 decision 3): the only scan phase that
    touches ``store`` at all — everything else runs against the returned
    :class:`ScanPlan`. Reads are network-free.

    Raises:
        ScanError: malformed gap setting / sync-state payload, unknown
            wallet, or a bad ``wallet`` argument type.
        WatchKeyError: the wallet descriptor fails the parse-time gates.
        StoreError: a store read failed.
    """
    gap = _resolve_gap_limit(store, gap_limit)
    descriptor, wallet_id = _resolve_wallet(store, wallet)
    existing = {
        branch: {record.index: record for record in store.get_addresses(wallet_id, branch)}
        for branch in BRANCHES
    }
    allocated = frozenset(
        record.address
        for branch in BRANCHES
        for record in existing[branch].values()
        if record.status == ADDRESS_ALLOCATED
    )
    # Rebuild mode recomputes the cursor from chain truth + allocations
    # and must not seed derivation rows it never reads (fail-closed
    # "store untouched on a mid-chain failure" contract).
    derivation_next = (
        {}
        if rebuild
        else {
            branch: store.get_derivation(wallet_id, branch).next_index
            for branch in BRANCHES
        }
    )
    return ScanPlan(
        wallet_id=wallet_id,
        descriptor=descriptor,
        gap=gap,
        rebuild=rebuild,
        existing=existing,
        allocated=allocated,
        previous_cursor=_read_previous_cursor(store, wallet_id),
        derivation_next=derivation_next,
    )


def fetch_scan(
    plan: ScanPlan,
    client: EsploraClient,
    *,
    progress_fn: Callable[[], None] | None = None,
) -> ScanRecords:
    """Derive + fetch the whole scan result from ``client`` — NO store access.

    The CHAIN-WORKER phase (ADR-0022 decision 2): runs off the engine
    thread against a :class:`ScanPlan` snapshot and the chain client only,
    and returns the immutable :class:`ScanRecords` the engine persists.
    The entire chain phase still runs before any persistence (a separate
    engine-thread ``persist_scan`` call): a failed fetch raises and leaves
    the store untouched — fail closed.

    ``progress_fn`` (TCK-UX-001) is invoked on the WORKER thread — one
    bare value-free tick per probed address; the caller owns delivery
    (the app queues ticks for the engine, never renders from this
    thread). ``None`` (the default) performs no callback.

    Raises:
        ChainError: a chain query failed (nothing was produced).
        ScanError: a chain payload failed validation (fail closed).
    """
    # ---- chain phase (immutable snapshots in, immutable records out)
    tip_height = client.get_tip_height()
    scanned_at = datetime.now(UTC).isoformat()

    branch_summaries: dict[int, BranchScanSummary] = {}
    window_maps: dict[int, dict[int, str]] = {}
    raw_txs: dict[str, _RawTx] = {}
    utxo_records: list[UtxoRecord] = []

    for branch in BRANCHES:
        existing = plan.existing[branch]
        final_map, branch_truncated, funded = _walk_history(
            client,
            plan.descriptor.parsed,
            branch,
            existing,
            raw_txs,
            gap=plan.gap,
            rebuild=plan.rebuild,
            progress_fn=progress_fn,
        )
        used_indices, max_used_index, last_index = _summarize_walk(final_map, raw_txs)
        utxo_records.extend(_scan_utxos(client, plan.wallet_id, branch, final_map, funded))

        next_index = (
            max(_max_allocated_index(existing) + 1, max_used_index + 1)
            if plan.rebuild
            else max(plan.derivation_next.get(branch, 0), max_used_index + 1)
        )
        branch_summaries[branch] = BranchScanSummary(
            branch=branch,
            scanned=len(final_map),
            window_last_index=last_index,
            max_used_index=max_used_index,
            next_index=next_index,
            used_indices=tuple(used_indices),
            truncated=branch_truncated,
        )
        window_maps[branch] = final_map

    # Direction is computed once, against the complete address set.
    our_addresses = _final_address_set(plan.existing, window_maps)
    tx_records = _build_tx_records(plan.wallet_id, raw_txs, our_addresses)
    out_of_window = _detect_out_of_window(branch_summaries, plan.previous_cursor)

    # ALL payloads are built here; the entire write-set then lands through
    # ONE composite store call in :func:`persist_scan` — a single SQLite
    # transaction (all-or-nothing) executed by the ENGINE thread only.
    # Address statuses can therefore never desync from the derivation
    # cursor or sync state, even on a crash mid-persist (TCK-P1-002
    # security review, atomic-persist finding). The every-scan
    # out_of_window_detected write (empty payload clears stale warnings)
    # is part of the same write.
    address_rows = [
        record
        for branch in BRANCHES
        for record in _address_records(
            plan.wallet_id,
            branch,
            window_maps[branch],
            set(branch_summaries[branch].used_indices),
            plan.allocated,
            plan.descriptor.script_type,
        )
    ]
    derivation_states = [
        DerivationRecord(
            wallet_id=plan.wallet_id,
            branch=branch,
            max_used_index=branch_summaries[branch].max_used_index,
            next_index=branch_summaries[branch].next_index,
        )
        for branch in BRANCHES
    ]
    sync_state_updates = {
        CURSOR_KEY: _dump_cursor(branch_summaries),
        TIP_KEY: str(tip_height),
        SCAN_AT_KEY: scanned_at,
        OUT_OF_WINDOW_KEY: _dump_out_of_window(out_of_window, scanned_at),
    }
    summary = ScanSummary(
        wallet_id=plan.wallet_id,
        gap_limit=plan.gap,
        tip_height=tip_height,
        scanned_at=scanned_at,
        branches=branch_summaries,
        utxo_count=len(utxo_records),
        utxo_value_sats=sum(record.value_sats for record in utxo_records),
        out_of_window=out_of_window,
        truncated=any(s.truncated for s in branch_summaries.values()),
    )
    return ScanRecords(
        summary=summary,
        wallet_id=plan.wallet_id,
        address_rows=tuple(address_rows),
        derivation_states=tuple(derivation_states),
        utxo_snapshot=tuple(utxo_records),
        tx_rows=tuple(tx_records),
        sync_state_updates=sync_state_updates,
    )


def persist_scan(store: Store, records: ScanRecords) -> ScanSummary:
    """Persist one :func:`fetch_scan` result set — ENGINE thread only.

    ADR-0022 decision 3: the sole writer path of the scan split; hands
    the immutable record set to :meth:`Store.persist_scan_result` (the
    single atomic transaction). Any store failure rolls back completely
    and propagates (``StoreError``/``StoreIntegrityError``, value-free;
    deliberately *not* wrapped in :class:`ScanError`) — the prior store
    state stays fully intact and the scan can be retried.

    Mid-scan reconciliation (TCK-SCAN-003 security review): because
    ADR-0022 decision 6 lets ``new_address`` allocate on the engine
    thread while a non-blocking fetch runs, the records (built from the
    pre-allocation :class:`ScanPlan` snapshot) can lag the store. This
    engine-thread persist point is the one place allowed to re-read the
    store: the merge floors every branch's ``next_index`` at the LIVE
    max-allocated/used index + 1 (the rebuild path's own never-reissue
    rule, evaluated at persist time instead of plan time — a genuine
    chain-truth cursor *lowering* still stands) and never downgrades
    an address the store currently holds ``allocated`` to ``unused``
    (preserved by address string, the same rule as ``plan.allocated``;
    ``used`` is a status upgrade and stands). The returned summary
    keeps the scan's own view — it is narration, never persisted state.
    """
    live_rows = {
        branch: {row.index: row for row in store.get_addresses(records.wallet_id, branch)}
        for branch in BRANCHES
    }
    live_allocated = frozenset(
        row.address for rows in live_rows.values() for row in rows.values()
        if row.status == ADDRESS_ALLOCATED
    )
    address_rows = tuple(
        replace(row, status=ADDRESS_ALLOCATED)
        if row.status == ADDRESS_UNUSED and row.address in live_allocated
        else row
        for row in records.address_rows
    )
    derivation_states = tuple(
        replace(
            state,
            next_index=max(
                state.next_index, _max_allocated_index(live_rows[state.branch]) + 1
            ),
        )
        for state in records.derivation_states
    )
    store.persist_scan_result(
        records.wallet_id,
        address_rows=address_rows,
        derivation_states=derivation_states,
        utxo_snapshot=records.utxo_snapshot,
        tx_rows=records.tx_rows,
        sync_state_updates=records.sync_state_updates,
    )
    return records.summary


# ----------------------------------------------------------------- internal


def _resolve_gap_limit(store: Store, gap_limit: int | None) -> int:
    """Resolve and validate the gap limit (param > setting > default)."""
    if gap_limit is not None:
        if isinstance(gap_limit, bool) or not isinstance(gap_limit, int):
            raise ScanError("gap_limit must be an integer")
        source = "gap_limit argument"
    else:
        raw = store.get_setting(GAP_LIMIT_SETTING)
        if raw is None:
            return DEFAULT_GAP_LIMIT
        try:
            gap_limit = int(raw)
        except ValueError as exc:
            raise ScanError(
                f"setting {GAP_LIMIT_SETTING!r} is not a valid integer"
            ) from exc
        source = f"setting {GAP_LIMIT_SETTING!r}"
    if not _MIN_GAP <= gap_limit <= _MAX_GAP:
        raise ScanError(f"{source} must be between {_MIN_GAP} and {_MAX_GAP}")
    return gap_limit


def _resolve_wallet(
    store: Store, wallet: WalletRecord | WalletDescriptor
) -> tuple[WalletDescriptor, int]:
    """Resolve the scan input to ``(descriptor, wallet_id)``."""
    if isinstance(wallet, WalletRecord):
        return WalletDescriptor.from_descriptor_string(wallet.descriptor), wallet.id
    if isinstance(wallet, WalletDescriptor):
        for row in store.list_wallets():
            if row.descriptor == wallet.descriptor:
                return wallet, row.id
        raise ScanError("no stored wallet matches the supplied descriptor")
    raise ScanError("wallet must be a WalletRecord or WalletDescriptor")


def _walk_history(
    client: EsploraClient,
    parsed: ParsedKey,
    branch: int,
    existing: dict[int, AddressRecord],
    raw_txs: dict[str, _RawTx],
    *,
    gap: int,
    rebuild: bool,
    progress_fn: Callable[[], None] | None = None,
) -> tuple[dict[int, str], bool, set[str]]:
    """Walk one branch ascending until ``gap`` consecutive unused addresses.

    Returns ``(final_map, truncated, funded)`` — the ``{index: address}``
    window map, whether the walk stopped at the absolute window ceiling
    (``_MAX_WINDOW_ADDRESSES``) instead of the gap condition (TCK-SEC-002:
    usage is attacker-derivable, so the walk is absolutely bounded; the
    ceiling — not ``used + gap`` — is the hard stop), and the set of
    addresses whose ``/txs`` result was non-empty (``funded``: the only
    addresses that can hold UTXOs — the caller skips their ``/utxo``
    fetch otherwise, TCK-SCAN-001). At most
    ``_MAX_WINDOW_ADDRESSES`` indices (0 .. ceiling − 1) are ever derived
    or probed, so the per-branch request budget is
    ≤ ``_MAX_WINDOW_ADDRESSES`` txs + ``_MAX_WINDOW_ADDRESSES`` utxo
    probes no matter the usage pattern. Ensures every walked index has an
    address (deriving from the branch key once per walk; in ``rebuild``
    mode cached mappings are re-derived and never trusted). Observed
    transactions are validated strictly and merged into ``raw_txs``
    (first sighting wins; entries carry the full tx so sightings agree).

    ``progress_fn`` (TCK-UX-001): optional zero-argument callback invoked
    EXACTLY once per probed address — immediately before the ``txs``
    probe below. It receives NO arguments: no address, no index, no
    amounts — a bare tick, value-free by construction (the caller owns
    any rendering). ``None`` (the default) performs no callback and
    leaves the walk byte-identical to the pre-callback behavior.
    """
    deriver = BranchDeriver(parsed, branch)
    final_map: dict[int, str] = {}
    funded: set[str] = set()
    consecutive_unused = 0
    index = 0
    while True:
        if not rebuild and index in existing:
            address = existing[index].address
        else:
            address = deriver.address(index)
        final_map[index] = address

        # One bare tick per probed address (TCK-UX-001): zero arguments,
        # no address/index/amount data — value-free by construction.
        if progress_fn is not None:
            progress_fn()
        entries = client.get_address_txs(address)
        # Validate the whole payload before acting on any of it.
        validated = [_parse_tx_entry(entry) for entry in entries]
        if validated:
            funded.add(address)
            for raw in validated:
                raw_txs.setdefault(raw.txid, raw)
            consecutive_unused = 0
        else:
            consecutive_unused += 1
        if consecutive_unused >= gap:
            return final_map, False, funded
        if index + 1 >= _MAX_WINDOW_ADDRESSES:
            # Absolute ceiling reached with usage still live: stop walking
            # and report truncation (the caller marks the scan result).
            return final_map, True, funded
        index += 1


def _summarize_walk(
    final_map: dict[int, str],
    raw_txs: dict[str, _RawTx],
) -> tuple[list[int], int, int]:
    """Reconstruct per-branch usage from the transactions seen so far.

    An index is *used* when its address appears in any validated
    transaction (as input or output — "mark used on any tx"). At call
    time ``raw_txs`` holds exactly this branch's sightings, because the
    branches are walked in order. ``max_used_index`` may shrink relative
    to the cache when an unconfirmed-only transaction vanished.
    """
    touched: set[str] = set()
    for raw in raw_txs.values():
        touched |= raw.spends | raw.receives
    used_indices = [i for i in sorted(final_map) if final_map[i] in touched]
    max_used_index = used_indices[-1] if used_indices else -1
    return used_indices, max_used_index, max(final_map)


def _max_allocated_index(existing: dict[int, AddressRecord]) -> int:
    """Highest index already handed out (``allocated``/``used``) for a branch.

    ``existing`` maps index → :class:`~localwallet.store.AddressRecord` for
    the branch. Only ``allocated``/``used`` rows count: an address allocated
    at an index above a rescanned window must not be re-issued later, so a
    rebuild floors the derivation cursor at ``max_allocated_index + 1``.
    ``unused`` rows (mere window prefetch) are not allocations and do not
    pin the cursor. Returns ``-1`` when nothing has been handed out.
    """
    return max(
        (index for index, record in existing.items()
         if record.status in (ADDRESS_ALLOCATED, ADDRESS_USED)),
        default=-1,
    )


def _scan_utxos(
    client: EsploraClient,
    wallet_id: int,
    branch: int,
    final_map: dict[int, str],
    funded: set[str],
) -> list[UtxoRecord]:
    """Fetch and strictly validate UTXOs for the window's *funded* addresses.

    ``funded`` is the set of addresses whose ``/txs`` walk result was
    non-empty; an address with no transactions (confirmed or mempool —
    the txs listing includes unconfirmed entries) cannot hold UTXOs, so
    its ``/utxo`` fetch is skipped outright (TCK-SCAN-001: fresh-wallet
    startup halves the call count; the skipped fetch would always
    return ``[]`` against a truthful Esplora).
    """
    records: list[UtxoRecord] = []
    for _, address in sorted(final_map.items()):
        if address not in funded:
            continue
        payload = client.get_address_utxos(address)
        for position, entry in enumerate(payload):
            txid, vout, value, confirmed, height = _parse_utxo_entry(
                entry, position
            )
            records.append(
                UtxoRecord(
                    wallet_id=wallet_id,
                    txid=txid,
                    vout=vout,
                    address=address,
                    value_sats=value,
                    confirmed=1 if confirmed else 0,
                    height=height,
                )
            )
    return records


def _final_address_set(
    existing: Mapping[int, Mapping[int, AddressRecord]],
    window_maps: dict[int, dict[int, str]],
) -> dict[str, tuple[int, int]]:
    """Every address the wallet currently maps, with its coordinates.

    Window maps carry the truth for scanned indices (in rebuild mode
    this drops corrupted mappings replaced during the walk); cached rows
    beyond the current window remain part of the wallet's address set
    (they cannot hold live UTXOs, but they still attribute transactions).
    ``existing`` is the plan's store snapshot — this runs on the worker
    with no live ``Store`` access (ADR-0022).
    """
    ours: dict[str, tuple[int, int]] = {}
    for branch in BRANCHES:
        for record in existing[branch].values():
            if record.index not in window_maps[branch]:
                ours.setdefault(record.address, (branch, record.index))
        for index, address in window_maps[branch].items():
            ours[address] = (branch, index)
    return ours


def _build_tx_records(
    wallet_id: int,
    raw_txs: dict[str, _RawTx],
    our_addresses: dict[str, tuple[int, int]],
) -> list[TxRecord]:
    """Direction + persistence shape for every observed transaction.

    Direction contract (ticket): any input spending one of our addresses
    means ``out`` — or ``self`` when the same transaction also pays to
    one of our addresses (consolidation/change); otherwise ``in``.
    Deterministic order (sorted by txid) keeps store content stable.
    """
    ours = set(our_addresses)
    records: list[TxRecord] = []
    for txid in sorted(raw_txs):
        raw = raw_txs[txid]
        if raw.spends & ours:
            direction = DIR_SELF if raw.receives & ours else DIR_OUT
        else:
            direction = DIR_IN
        records.append(
            TxRecord(
                wallet_id=wallet_id,
                txid=raw.txid,
                height=raw.height,
                block_time=raw.block_time,
                fee_sats=raw.fee_sats,
                direction=direction,
                raw_summary=None,
            )
        )
    return records


def _detect_out_of_window(
    summaries: dict[int, BranchScanSummary],
    previous_cursor: dict[int, int] | None,
) -> dict[str, dict[str, int]]:
    """Flag usage beyond the previously recorded scan window (R3).

    Only fires when a previous scan cursor exists — a first scan has no
    window to exceed. Detail payload is JSON-safe and value-free
    (indexes only).
    """
    if not previous_cursor:
        return {}
    flagged: dict[str, dict[str, int]] = {}
    for branch in BRANCHES:
        previous_end = previous_cursor.get(branch)
        if previous_end is None:
            continue
        found = summaries[branch].max_used_index
        if found > previous_end - 1:
            flagged[str(branch)] = {
                "max_used_index": found,
                "previous_window_end": previous_end - 1,
            }
    return flagged


def _address_records(
    wallet_id: int,
    branch: int,
    final_map: dict[int, str],
    used_indices: set[int],
    allocated_strings: set[str],
    script_type: str,
) -> list[AddressRecord]:
    """Status merge for the persist phase: used > allocated > unused.

    ``allocated`` is preserved by address string so a rescan (which
    overwrites mappings) does not un-issue addresses the user already
    received; an allocated address that actually received funds becomes
    ``used``.
    """
    records: list[AddressRecord] = []
    for index in sorted(final_map):
        address = final_map[index]
        if index in used_indices:
            status = ADDRESS_USED
        elif address in allocated_strings:
            status = ADDRESS_ALLOCATED
        else:
            status = ADDRESS_UNUSED
        records.append(
            AddressRecord(
                wallet_id=wallet_id,
                branch=branch,
                index=index,
                address=address,
                script_type=script_type,
                status=status,
            )
        )
    return records


# ------------------------------------------------- strict payload parsing


def _parse_tx_entry(entry: dict[str, object]) -> _RawTx:
    """Validate one Esplora address-txs entry (fail closed).

    Strict: ``txid`` (64-hex string) and ``status`` (object with boolean
    ``confirmed``) are mandatory; ``status.block_height`` /
    ``status.block_time`` and ``fee`` are optional but must be
    well-formed integers when present (``fee`` may be absent — it is
    stored as ``None``; it must never be fabricated). ``vin``/``vout``
    are optional; when present they must be lists of objects, and
    input ``prevout`` objects must be well-formed.
    """
    txid = entry.get("txid")
    if (
        not isinstance(txid, str)
        or len(txid) != 64
        or any(c not in _TXID_CHARS for c in txid)
    ):
        raise ScanError("address-txs entry has a missing or malformed 'txid'")
    status = entry.get("status")
    if not isinstance(status, dict):
        raise ScanError("address-txs entry has a missing or malformed 'status'")
    confirmed = status.get("confirmed")
    if not isinstance(confirmed, bool):
        raise ScanError(
            "address-txs entry has a missing or non-boolean 'status.confirmed'"
        )
    height = _optional_int(status, "block_height", "status.block_height")
    block_time = _optional_int(status, "block_time", "status.block_time")

    fee = entry.get("fee")
    if fee is not None and (
        isinstance(fee, bool) or not isinstance(fee, int) or fee < 0
    ):
        raise ScanError("address-txs entry has a malformed 'fee'")

    vin = entry.get("vin", [])
    if not isinstance(vin, list) or any(not isinstance(v, dict) for v in vin):
        raise ScanError("address-txs entry has a malformed 'vin'")
    vout = entry.get("vout", [])
    if not isinstance(vout, list) or any(not isinstance(v, dict) for v in vout):
        raise ScanError("address-txs entry has a malformed 'vout'")

    spends = _addresses_from_io(vin, "prevout")
    receives = _addresses_from_io(vout, None)
    return _RawTx(
        txid=txid,
        height=height,
        block_time=block_time,
        fee_sats=fee,
        spends=spends,
        receives=receives,
    )


def _addresses_from_io(
    entries: list[dict[str, object]], prevout_key: str | None
) -> frozenset[str]:
    """Collect ``scriptpubkey_address`` values from vin/vout entries.

    Entries without an address (coinbase inputs, OP_RETURN outputs,
    non-standard scripts) contribute nothing. Malformed shapes fail
    closed; a ``None`` address is treated as absent.
    """
    addresses: set[str] = set()
    for position, item in enumerate(entries):
        source: object = item.get(prevout_key) if prevout_key else item
        if prevout_key and source is None:
            continue  # coinbase input (no prevout)
        if not isinstance(source, dict):
            raise ScanError(
                f"address-txs entry has a malformed io object at position {position}"
            )
        address = source.get("scriptpubkey_address")
        if address is None:
            continue
        if not isinstance(address, str) or not address:
            raise ScanError(
                f"address-txs entry has a malformed 'scriptpubkey_address' "
                f"at position {position}"
            )
        addresses.add(address)
    return frozenset(addresses)


def _optional_int(source: dict[str, object], key: str, label: str) -> int | None:
    """Optional non-negative integer field; absent → None, malformed → error."""
    value = source.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ScanError(f"address-txs entry has a malformed {label!r}")
    return value


def _parse_utxo_entry(
    entry: dict[str, object], position: int
) -> tuple[str, int, int, bool, int | None]:
    """Validate one Esplora address-utxo entry (fail closed).

    Returns ``(txid, vout, value_sats, confirmed, block_height | None)``.
    Same strictness as the chain module's ``balance_from_utxos``, plus
    the txid shape check; error messages name the entry position and
    field, never the value.
    """
    txid = entry.get("txid")
    if (
        not isinstance(txid, str)
        or len(txid) != 64
        or any(c not in _TXID_CHARS for c in txid)
    ):
        raise ScanError(f"address-utxo entry {position} has a missing or malformed 'txid'")
    vout = entry.get("vout")
    if isinstance(vout, bool) or not isinstance(vout, int) or vout < 0:
        raise ScanError(f"address-utxo entry {position} has a missing or invalid 'vout'")
    value = entry.get("value")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ScanError(f"address-utxo entry {position} has a missing or invalid 'value'")
    status = entry.get("status")
    if not isinstance(status, dict):
        raise ScanError(
            f"address-utxo entry {position} has a missing or malformed 'status'"
        )
    confirmed = status.get("confirmed")
    if not isinstance(confirmed, bool):
        raise ScanError(
            f"address-utxo entry {position} has a missing or non-boolean 'status.confirmed'"
        )
    height = status.get("block_height")
    if height is not None and (
        isinstance(height, bool) or not isinstance(height, int) or height < 0
    ):
        raise ScanError(
            f"address-utxo entry {position} has a malformed 'status.block_height'"
        )
    return txid, vout, value, confirmed, height


# -------------------------------------------------------- sync-state I/O


def _read_previous_cursor(store: Store, wallet_id: int) -> dict[int, int] | None:
    """Load the previous scan cursor (``{"0": 24, "1": 20}`` JSON shape)."""
    raw = store.get_sync_state(wallet_id, CURSOR_KEY)
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ScanError(
            f"sync_state {CURSOR_KEY!r} is not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ScanError(f"sync_state {CURSOR_KEY!r} has an unexpected shape")
    cursor: dict[int, int] = {}
    for key, value in payload.items():
        try:
            branch = int(key)
        except (TypeError, ValueError) as exc:
            raise ScanError(
                f"sync_state {CURSOR_KEY!r} has a malformed branch key"
            ) from exc
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or branch not in BRANCHES
        ):
            raise ScanError(
                f"sync_state {CURSOR_KEY!r} has a malformed entry"
            )
        cursor[branch] = value
    return cursor


def _dump_cursor(summaries: dict[int, BranchScanSummary]) -> str:
    """Per-branch cursor = stop index + 1 (next index a scan would reach)."""
    return json.dumps(
        {str(branch): summary.window_last_index + 1 for branch, summary in summaries.items()},
        sort_keys=True,
    )


def _dump_out_of_window(
    flagged: dict[str, dict[str, int]], scanned_at: str
) -> str:
    """Warning payload for P1-004 UI surfacing (ADR-0009).

    Written on every completed scan: an empty payload explicitly clears a
    stale warning once the window covers the observed usage.
    """
    return json.dumps(
        {
            "detected_at": scanned_at if flagged else None,
            "branches": flagged,
        },
        sort_keys=True,
    )

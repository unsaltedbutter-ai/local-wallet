"""Gap-limited chain scan and cache orchestration (TCK-P1-002).

Scans a watch-only wallet against an :class:`~localwallet.chain.EsploraClient`
(the chain module is the only networked code; this module performs no I/O
itself), and persists the result — derivation state, address statuses, UTXO
snapshot, transaction history, and sync cursors — in a single atomic store
transaction (:meth:`localwallet.store.Store.persist_scan_result`).

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
queried for every window address and the union becomes the new
wallet-wide UTXO snapshot (replaced wholesale inside the atomic persist —
the previous set is fully replaced).

Any address that can still hold a live UTXO appears inside the window:
its funding transaction shows up in its history, marks it used, and
extends the walk — so the snapshot cannot silently drop spendable coins
on addresses cached beyond the current window.

Ordering (rate-limit friendly): strictly sequential; per branch indices
ascend, transactions are fetched before UTXOs, branch 0 runs before
branch 1. Exactly one txs call and one utxo call per scanned window
address.

Absolute per-branch window ceiling (TCK-SEC-002): the gap-limited walk
above is unbounded when usage itself is attacker-driven — an observer of
the public watch-only xpub can fund consecutive derivable indices
0, 1, 2, … N on public testnet and force N + gap probes per branch per
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
from dataclasses import dataclass, field
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
    "ScanSummary",
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
    #: Number of window addresses queried (txs + utxos each).
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
) -> ScanSummary:
    """Scan ``wallet`` and refresh the cached state in ``store``.

    Trusts the cached address mappings (existing rows are reused and the
    window extended as needed) and never lowers the cached derivation
    cursor below its current value. See module docstring for the
    algorithm and failure semantics.

    Args:
        store: The persistence layer (an open :class:`Store`).
        client: The chain adapter (the only networked component).
        wallet: The wallet row (its descriptor string is parsed) or an
            already-built :class:`WalletDescriptor` (matched against a
            wallet row by descriptor string).
        gap_limit: Gap override; ``None`` reads the ``gap_limit`` setting
            and falls back to :data:`DEFAULT_GAP_LIMIT`.

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
    return _run_scan(store, client, wallet, gap_limit=gap_limit, rebuild=False)


def rescan_wallet(
    store: Store,
    client: EsploraClient,
    wallet: WalletRecord | WalletDescriptor,
    *,
    gap_limit: int | None = None,
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
    failure semantics).
    """
    return _run_scan(store, client, wallet, gap_limit=gap_limit, rebuild=True)


# ----------------------------------------------------------------- internal


def _run_scan(
    store: Store,
    client: EsploraClient,
    wallet: WalletRecord | WalletDescriptor,
    *,
    gap_limit: int | None,
    rebuild: bool,
) -> ScanSummary:
    """Shared scan core; ``rebuild`` selects the rescan trust model."""
    gap = _resolve_gap_limit(store, gap_limit)
    descriptor, wallet_id = _resolve_wallet(store, wallet)

    # ---- chain phase (no store mutations below until it fully succeeds)
    tip_height = client.get_tip_height()
    scanned_at = datetime.now(UTC).isoformat()

    previous_cursor = _read_previous_cursor(store, wallet_id)
    allocated_strings = {
        record.address
        for branch in BRANCHES
        for record in store.get_addresses(wallet_id, branch)
        if record.status == ADDRESS_ALLOCATED
    }

    branch_summaries: dict[int, BranchScanSummary] = {}
    window_maps: dict[int, dict[int, str]] = {}
    raw_txs: dict[str, _RawTx] = {}
    utxo_records: list[UtxoRecord] = []

    for branch in BRANCHES:
        existing = {r.index: r for r in store.get_addresses(wallet_id, branch)}
        final_map, branch_truncated = _walk_history(
            client,
            descriptor.parsed,
            branch,
            existing,
            raw_txs,
            gap=gap,
            rebuild=rebuild,
        )
        used_indices, max_used_index, last_index = _summarize_walk(final_map, raw_txs)
        utxo_records.extend(
            _scan_utxos(client, wallet_id, branch, final_map)
        )

        next_index = (
            max(_max_allocated_index(existing) + 1, max_used_index + 1)
            if rebuild
            else max(
                store.get_derivation(wallet_id, branch).next_index,
                max_used_index + 1,
            )
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
    our_addresses = _final_address_set(store, wallet_id, window_maps)
    tx_records = _build_tx_records(wallet_id, raw_txs, our_addresses)
    out_of_window = _detect_out_of_window(branch_summaries, previous_cursor)

    # ---------------------------------------------------- persist phase
    # ALL payloads are built first; the entire write-set then lands through
    # ONE composite store call — a single SQLite transaction (all-or-nothing).
    # Address statuses can therefore never desync from the derivation cursor
    # or sync state, even on a crash mid-persist (TCK-P1-002 security
    # review, atomic-persist finding). The every-scan out_of_window_detected
    # write (empty payload clears stale warnings) is part of the same write.
    address_rows = [
        record
        for branch in BRANCHES
        for record in _address_records(
            wallet_id,
            branch,
            window_maps[branch],
            set(branch_summaries[branch].used_indices),
            allocated_strings,
            descriptor.script_type,
        )
    ]
    derivation_states = [
        DerivationRecord(
            wallet_id=wallet_id,
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
    store.persist_scan_result(
        wallet_id,
        address_rows=address_rows,
        derivation_states=derivation_states,
        utxo_snapshot=utxo_records,
        tx_rows=tx_records,
        sync_state_updates=sync_state_updates,
    )

    return ScanSummary(
        wallet_id=wallet_id,
        gap_limit=gap,
        tip_height=tip_height,
        scanned_at=scanned_at,
        branches=branch_summaries,
        utxo_count=len(utxo_records),
        utxo_value_sats=sum(record.value_sats for record in utxo_records),
        out_of_window=out_of_window,
        truncated=any(s.truncated for s in branch_summaries.values()),
    )


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
) -> tuple[dict[int, str], bool]:
    """Walk one branch ascending until ``gap`` consecutive unused addresses.

    Returns ``(final_map, truncated)`` — the ``{index: address}`` window
    map and whether the walk stopped at the absolute window ceiling
    (``_MAX_WINDOW_ADDRESSES``) instead of the gap condition (TCK-SEC-002:
    usage is attacker-derivable, so the walk is absolutely bounded; the
    ceiling — not ``used + gap`` — is the hard stop). At most
    ``_MAX_WINDOW_ADDRESSES`` indices (0 .. ceiling − 1) are ever derived
    or probed, so the per-branch request budget is
    ≤ ``_MAX_WINDOW_ADDRESSES`` txs + ``_MAX_WINDOW_ADDRESSES`` utxo
    probes no matter the usage pattern. Ensures every walked index has an
    address (deriving from the branch key once per walk; in ``rebuild``
    mode cached mappings are re-derived and never trusted). Observed
    transactions are validated strictly and merged into ``raw_txs``
    (first sighting wins; entries carry the full tx so sightings agree).
    """
    deriver = BranchDeriver(parsed, branch)
    final_map: dict[int, str] = {}
    consecutive_unused = 0
    index = 0
    while True:
        if not rebuild and index in existing:
            address = existing[index].address
        else:
            address = deriver.address(index)
        final_map[index] = address

        entries = client.get_address_txs(address)
        # Validate the whole payload before acting on any of it.
        validated = [_parse_tx_entry(entry) for entry in entries]
        if validated:
            for raw in validated:
                raw_txs.setdefault(raw.txid, raw)
            consecutive_unused = 0
        else:
            consecutive_unused += 1
        if consecutive_unused >= gap:
            return final_map, False
        if index + 1 >= _MAX_WINDOW_ADDRESSES:
            # Absolute ceiling reached with usage still live: stop walking
            # and report truncation (the caller marks the scan result).
            return final_map, True
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
) -> list[UtxoRecord]:
    """Fetch and strictly validate UTXOs for every window address."""
    records: list[UtxoRecord] = []
    for _, address in sorted(final_map.items()):
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
    store: Store,
    wallet_id: int,
    window_maps: dict[int, dict[int, str]],
) -> dict[str, tuple[int, int]]:
    """Every address the wallet currently maps, with its coordinates.

    Window maps carry the truth for scanned indices (in rebuild mode
    this drops corrupted mappings replaced during the walk); store rows
    beyond the current window remain part of the wallet's address set
    (they cannot hold live UTXOs, but they still attribute transactions).
    """
    ours: dict[str, tuple[int, int]] = {}
    for branch in BRANCHES:
        for record in store.get_addresses(wallet_id, branch):
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

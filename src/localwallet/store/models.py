"""Typed row records for the store layer (TCK-P1-001).

Each frozen dataclass mirrors one table row in the SQLite schema (schema v3,
see :mod:`localwallet.store.db`). The store layer is an internal persistence
boundary; these records deliberately avoid pydantic to keep the store
dependency-free (stdlib only: :mod:`dataclasses`, :mod:`collections.abc`).

Values (addresses, txids, amounts) are stored *in the database* — that is the
point of the store — but they must never appear in log/exception text. See the
no-secrets / no-value-logging policy documented on :class:`~localwallet.store.db.Store`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, ClassVar

# ``branch`` values: 0 = receive, 1 = change (BIP44/BIP84 external/internal).
BRANCH_RECEIVE = 0
BRANCH_CHANGE = 1

# Address ``status`` values (also enforced by a SQL CHECK constraint).
ADDRESS_UNUSED = "unused"
ADDRESS_USED = "used"
ADDRESS_ALLOCATED = "allocated"

# Transaction ``direction`` values (enforced by a SQL CHECK constraint).
DIR_IN = "in"
DIR_OUT = "out"
DIR_SELF = "self"

# Coin labels (TCK-UTXO-001, docs/ux-utxo-notes-design.md §1.4): a CLOSED tag
# vocabulary in canonical storage order (the §1.4 table order), plus one
# free-text note per coin. Tags/notes are user-authored facts about the user's
# own coins, consumed exclusively by deterministic code — they never enter
# model context (§1.1 / §7.10) and the model never authors them.
COIN_TAGS: tuple[str, ...] = ("kyc", "exchange", "p2p", "purchase", "consolidation")

#: Free-text note cap, characters (§1.3: "user's free text, verbatim, ≤ 500").
COIN_NOTE_MAX_CHARS = 500


def normalize_coin_tags(tags: Iterable[str]) -> tuple[str, ...]:
    """Dedupe and canonically order tag ids against the closed set.

    Raises :class:`ValueError` naming NOTHING but the cause (an unknown or
    non-string tag) — the offending word is never echoed (value-free policy).
    """
    seen: set[str] = set()
    for tag in tags:
        if not isinstance(tag, str) or tag not in COIN_TAGS:
            raise ValueError("coin tags must come from the closed tag set")
        seen.add(tag)
    return tuple(tag for tag in COIN_TAGS if tag in seen)


@dataclass(frozen=True)
class WalletRecord:
    """A watch-only wallet profile (public descriptor only, never keys)."""

    id: int
    name: str
    descriptor: str
    created_at: str

    _COLUMNS: ClassVar[tuple[str, ...]] = ("id", "name", "descriptor", "created_at")

    @classmethod
    def from_row(cls, row: Any) -> WalletRecord:
        return cls(**{col: row[col] for col in cls._COLUMNS})

    def to_row(self) -> tuple[int, str, str, str]:
        return (self.id, self.name, self.descriptor, self.created_at)


@dataclass(frozen=True)
class DerivationRecord:
    """Per-branch derivation state for a wallet."""

    wallet_id: int
    branch: int
    max_used_index: int
    next_index: int

    _COLUMNS: ClassVar[tuple[str, ...]] = (
        "wallet_id",
        "branch",
        "max_used_index",
        "next_index",
    )

    @classmethod
    def from_row(cls, row: Any) -> DerivationRecord:
        return cls(**{col: row[col] for col in cls._COLUMNS})

    def to_row(self) -> tuple[int, int, int, int]:
        return (self.wallet_id, self.branch, self.max_used_index, self.next_index)


@dataclass(frozen=True)
class AddressRecord:
    """A derived address and its usage status for a wallet/branch/index."""

    wallet_id: int
    branch: int
    index: int
    address: str
    script_type: str | None
    status: str

    _COLUMNS: ClassVar[tuple[str, ...]] = (
        "wallet_id",
        "branch",
        "index",
        "address",
        "script_type",
        "status",
    )

    @classmethod
    def from_row(cls, row: Any) -> AddressRecord:
        return cls(**{col: row[col] for col in cls._COLUMNS})

    def to_row(self) -> tuple[int, int, int, str, str | None, str]:
        return (
            self.wallet_id,
            self.branch,
            self.index,
            self.address,
            self.script_type,
            self.status,
        )


@dataclass(frozen=True)
class UtxoRecord:
    """An unspent transaction output observed for a wallet."""

    wallet_id: int
    txid: str
    vout: int
    address: str | None
    value_sats: int
    confirmed: int
    height: int | None

    _COLUMNS: ClassVar[tuple[str, ...]] = (
        "wallet_id",
        "txid",
        "vout",
        "address",
        "value_sats",
        "confirmed",
        "height",
    )

    @classmethod
    def from_row(cls, row: Any) -> UtxoRecord:
        return cls(**{col: row[col] for col in cls._COLUMNS})

    def to_row(self) -> tuple[int, str, int, str | None, int, int, int | None]:
        return (
            self.wallet_id,
            self.txid,
            self.vout,
            self.address,
            self.value_sats,
            self.confirmed,
            self.height,
        )


@dataclass(frozen=True)
class CoinLabelRecord:
    """A user's coin label: closed-set tags + one free-text note (§1.3).

    Keyed by OUTPOINT (wallet_id, txid, vout) — a separate table from the
    DELETE+re-INSERTed UTXO snapshot, so labels survive every rescan and stay
    on record after the coin is spent. ``tags`` is always canonically ordered
    and deduped (stored comma-joined); ``note`` is the user's text verbatim
    (already length-capped by the typed writer). Values in these fields are
    user data: printed to the terminal verbatim, never logged, never model
    context.
    """

    wallet_id: int
    txid: str
    vout: int
    tags: tuple[str, ...]
    note: str | None

    _COLUMNS: ClassVar[tuple[str, ...]] = ("wallet_id", "txid", "vout", "tags", "note")

    @classmethod
    def from_row(cls, row: Any) -> CoinLabelRecord:
        raw = row["tags"]
        return cls(
            wallet_id=row["wallet_id"],
            txid=row["txid"],
            vout=row["vout"],
            tags=tuple(t for t in raw.split(",") if t) if raw else (),
            note=row["note"],
        )

    def to_row(self) -> tuple[int, str, int, str, str | None]:
        return (
            self.wallet_id,
            self.txid,
            self.vout,
            ",".join(self.tags),
            self.note,
        )


@dataclass(frozen=True)
class TxRecord:
    """A transaction observed for a wallet (history entry).

    Schema v3 (TCK-RBF-001) adds four columns, all NULLABLE — a v2-era row
    keeps NULLs and every reader must treat them as "not recorded", never
    fabricate. The first three are the broadcast-time capture written by the
    sign→broadcast handler off the flow's confirmed record (the flow already
    carries them — this is the one store write that unblocks amount/fee
    disambiguation lists and the BIP-125 fee delta); ``replaced_by_txid`` is
    the RBF lineage link (original → replacement), written only by the typed
    :meth:`~localwallet.store.db.Store.record_replacement` writer. Values in
    these fields follow the same discipline as every other stored value:
    verbatim in the database, never in log/exception text.
    """

    wallet_id: int
    txid: str
    height: int | None
    block_time: int | None
    fee_sats: int | None
    direction: str
    raw_summary: str | None
    amount_sats: int | None = None
    fee_rate_centisat_vb: int | None = None
    first_seen: int | None = None
    replaced_by_txid: str | None = None

    _COLUMNS: ClassVar[tuple[str, ...]] = (
        "wallet_id",
        "txid",
        "height",
        "block_time",
        "fee_sats",
        "direction",
        "raw_summary",
        "amount_sats",
        "fee_rate_centisat_vb",
        "first_seen",
        "replaced_by_txid",
    )

    @classmethod
    def from_row(cls, row: Any) -> TxRecord:
        return cls(**{col: row[col] for col in cls._COLUMNS})

    def to_row(
        self,
    ) -> tuple[
        int,
        str,
        int | None,
        int | None,
        int | None,
        str,
        str | None,
        int | None,
        int | None,
        int | None,
        str | None,
    ]:
        return (
            self.wallet_id,
            self.txid,
            self.height,
            self.block_time,
            self.fee_sats,
            self.direction,
            self.raw_summary,
            self.amount_sats,
            self.fee_rate_centisat_vb,
            self.first_seen,
            self.replaced_by_txid,
        )


# Superseded-retirement states (TCK-RBF-001): terminal outcomes for the LOSER
# of a lineage pair (original ↔ replacement via ``replaced_by_txid``). BIP-125
# rule: exactly one of the pair ever confirms — once one side has a height,
# the sibling is retired and must exit every pending count (a count that can
# never go down is a lie).
SUPERSEDED_REPLACED = "replaced"  # pending original; its replacement confirmed
SUPERSEDED_EVICTED = "evicted"  # pending replacement; the original confirmed


def superseded_states(records: Iterable[TxRecord]) -> dict[str, str]:
    """The retirement rule, derived — one shared source of truth (pure,
    store-only, no clock, no network).

    A row is superseded iff it is still pending (``height is None``) AND the
    chain-truth the scan persists says it lost the race:

    * ``replaced`` — its own ``replaced_by_txid`` points at a row of this
      wallet that has CONFIRMED (the replacement went through; the original
      never will).
    * ``evicted`` — some other row lists THIS txid as its replacement, and
      that original has CONFIRMED (the bump can no longer take effect).

    Derived, not stored: the rule is a pure function of the (height,
    ``replaced_by_txid``) data a scan persist already writes inside its
    single atomic transaction, so retirement can never desync from the
    confirmed heights — and a reorg that clears a height honestly un-retires
    the sibling. A link pointing at a txid with no row in the cache retires
    nothing (we do not know yet — stay pending, never fabricate).
    ``records`` must be one wallet's rows (a caller-supplied set from
    :meth:`~localwallet.store.db.Store.get_txs_for_wallet`); txids are
    unique per wallet, cross-wallet mixing is caller-bug territory.
    """
    by_txid = {r.txid: r for r in records}
    # replacement txid -> the (pending or confirmed) original that names it.
    # Only the lineage link exists to identify a pair, so the link's SOURCE
    # row is the original by definition.
    original_of: dict[str, TxRecord] = {
        r.replaced_by_txid: r for r in by_txid.values() if r.replaced_by_txid is not None
    }
    states: dict[str, str] = {}
    for r in by_txid.values():
        if r.height is not None:
            continue  # confirmed rows are never retired — they WON
        if r.replaced_by_txid is not None:
            partner = by_txid.get(r.replaced_by_txid)
            if partner is not None and partner.height is not None:
                states[r.txid] = SUPERSEDED_REPLACED
                continue
        evictor = original_of.get(r.txid)
        if evictor is not None and evictor.height is not None:
            states[r.txid] = SUPERSEDED_EVICTED
    return states


@dataclass(frozen=True)
class SettingRecord:
    """A key/value application setting (e.g. active_wallet_id, gap_limit)."""

    key: str
    value: str

    _COLUMNS: ClassVar[tuple[str, ...]] = ("key", "value")

    @classmethod
    def from_row(cls, row: Any) -> SettingRecord:
        return cls(**{col: row[col] for col in cls._COLUMNS})

    def to_row(self) -> tuple[str, str]:
        return (self.key, self.value)

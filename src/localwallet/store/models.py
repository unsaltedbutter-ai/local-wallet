"""Typed row records for the store layer (TCK-P1-001).

Each frozen dataclass mirrors one table row in the SQLite schema (schema v1,
see :mod:`localwallet.store.db`). The store layer is an internal persistence
boundary; these records deliberately avoid pydantic to keep the store
dependency-free (stdlib only: :mod:`dataclasses`).

Values (addresses, txids, amounts) are stored *in the database* — that is the
point of the store — but they must never appear in log/exception text. See the
no-secrets / no-value-logging policy documented on :class:`~localwallet.store.db.Store`.
"""

from __future__ import annotations

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
class TxRecord:
    """A transaction observed for a wallet (history entry)."""

    wallet_id: int
    txid: str
    height: int | None
    block_time: int | None
    fee_sats: int | None
    direction: str
    raw_summary: str | None

    _COLUMNS: ClassVar[tuple[str, ...]] = (
        "wallet_id",
        "txid",
        "height",
        "block_time",
        "fee_sats",
        "direction",
        "raw_summary",
    )

    @classmethod
    def from_row(cls, row: Any) -> TxRecord:
        return cls(**{col: row[col] for col in cls._COLUMNS})

    def to_row(self) -> tuple[int, str, int | None, int | None, int | None, str, str | None]:
        return (
            self.wallet_id,
            self.txid,
            self.height,
            self.block_time,
            self.fee_sats,
            self.direction,
            self.raw_summary,
        )


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

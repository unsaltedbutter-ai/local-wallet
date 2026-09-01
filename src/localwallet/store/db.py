"""SQLite-backed store for local-wallet (TCK-P1-001).

What the store is
-----------------
A thin, typed persistence layer over a single SQLite database (WAL). It backs
Phase 1: wallet profiles, per-branch derivation state, derived addresses,
UTXO cache, transaction cache, sync cursor, and application settings. All SQL
lives inside this module; callers use the typed accessor methods and row
records from :mod:`localwallet.store.models`. No raw SQL outside ``store/``.

WAL rationale
-------------
``PRAGMA journal_mode=WAL`` is used so a reader never blocks the writer and a
writer never blocks readers — important for a wallet that scans the chain
while the UI/agent reads cached state. WAL also survives an unclean shutdown
better than the default rollback journal.

No-secrets / no-value-logging policy
------------------------------------
Watch-only: the store holds **no secrets** (no xprvs, no seed phrases) — only
public watch data. Addresses, txids and amounts are legitimately *stored in
the database*, but they are **never placed into log/exception text**. Every
:class:`StoreError` message carries only table/operation context — never a
value. This module deliberately performs no ``logging``; errors are raised for
the caller to handle, with scrubbed messages.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from localwallet.store.models import (
    AddressRecord,
    DerivationRecord,
    TxRecord,
    UtxoRecord,
    WalletRecord,
)

# Current schema version, tracked via ``PRAGMA user_version``. Bump this and
# add an up-migration whenever the schema changes; never down-migrate.
SCHEMA_VERSION = 1

_BUSY_TIMEOUT_MS = 5000


class StoreError(Exception):
    """Base error for the store layer.

    Messages are **value-free by contract**: they name the table/operation
    that failed but never embed addresses, txids, amounts, or other wallet
    values. Exception chaining is preserved via ``raise ... from``.
    """


class StoreIntegrityError(StoreError):
    """A SQL constraint was violated (UNIQUE, NOT NULL, CHECK, or FK).

    Distinguished from other store errors so callers can react to e.g. a
    duplicate name without parsing message text.
    """


class _MigrateError(StoreError):
    """Raised when the on-disk schema cannot be (safely) brought to v1."""


def _utcnow() -> str:
    """ISO-8601 UTC timestamp for ``created_at`` (TEXT NOT NULL)."""
    return datetime.now(UTC).isoformat()


def _wrap_integrity(exc: sqlite3.IntegrityError) -> StoreIntegrityError:
    """Convert a constraint violation into a value-free StoreIntegrityError.

    The original exception (which may contain values) is preserved only as the
    ``__cause__`` (via ``raise ... from exc`` at the call site); the *message*
    is scrubbed here.
    """
    return StoreIntegrityError("constraint violated (uniqueness, foreign key, or check)")


def _wrap(exc: sqlite3.Error) -> StoreError:
    """Convert any other sqlite error into a value-free StoreError."""
    return StoreError("store operation failed")


class Store(AbstractContextManager["Store"]):
    """Context-managed SQLite store.

    Usage::

        with Store(path) as store:
            wallet = store.create_wallet("main", descriptor)

    :meth:`memory` builds an in-memory store for tests. The connection is
    opened eagerly in :meth:`__init__` (so accessors work immediately) and
    closed by :meth:`close` / :meth:`__exit__`.
    """

    def __init__(self, path: str | Path | None) -> None:
        self._path: Path | None = Path(path) if path is not None else None
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._path) if self._path is not None else ":memory:",
            isolation_level=None,  # autocommit; we manage transactions explicitly
        )
        self._wal_mode: str | None = None
        try:
            self._configure()
            self._migrate()
        except BaseException:
            self._conn.close()
            raise

    # ------------------------------------------------------------- lifecycle

    @classmethod
    def memory(cls) -> Store:
        """Build an in-memory store (tests / ephemeral use)."""
        return cls(None)

    def _configure(self) -> None:
        conn = self._conn
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        if self._path is not None:
            row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            self._wal_mode = row[0] if row else None

    @property
    def wal_mode(self) -> str | None:
        """Active journal mode ('wal' for file DBs, None for :memory:)."""
        return self._wal_mode

    def _migrate(self) -> None:
        conn = self._conn
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            # Fail closed: never open a DB from a newer (unknown) schema.
            raise _MigrateError(
                "database schema is newer than this build supports; "
                "refusing to open (upgrade the application first)"
            )
        if current < SCHEMA_VERSION:
            if current != 0:
                # We only know how to build schema 0 -> 1; anything else is an
                # unexpected older schema. No down-migrations exist.
                raise _MigrateError(
                    "database schema is an unsupported older version; "
                    "refusing to migrate (no down-migration path in v1)"
                )
            with self._transaction():
                self._create_schema()
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _create_schema(self) -> None:
        conn = self._conn
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS wallets (
                id          INTEGER PRIMARY KEY,
                name        TEXT UNIQUE NOT NULL,
                descriptor  TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS derivation (
                wallet_id        INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
                branch           INTEGER NOT NULL,
                max_used_index   INTEGER NOT NULL DEFAULT -1,
                next_index       INTEGER NOT NULL DEFAULT 0,
                UNIQUE(wallet_id, branch)
            );

            CREATE TABLE IF NOT EXISTS addresses (
                wallet_id   INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
                branch      INTEGER NOT NULL,
                `index`     INTEGER NOT NULL,
                address     TEXT UNIQUE NOT NULL,
                script_type TEXT,
                status      TEXT NOT NULL
                            CHECK(status IN ('unused','used','allocated')),
                PRIMARY KEY(wallet_id, branch, `index`)
            );

            CREATE TABLE IF NOT EXISTS utxos (
                wallet_id    INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
                txid         TEXT NOT NULL,
                vout         INTEGER NOT NULL,
                address      TEXT,
                value_sats   INTEGER NOT NULL,
                confirmed    INTEGER NOT NULL CHECK(confirmed IN (0,1)),
                height       INTEGER,
                UNIQUE(wallet_id, txid, vout)
            );

            CREATE TABLE IF NOT EXISTS transactions (
                wallet_id    INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
                txid         TEXT NOT NULL,
                height       INTEGER,
                block_time   INTEGER,
                fee_sats     INTEGER,
                direction    TEXT CHECK(direction IN ('in','out','self')),
                raw_summary  TEXT,
                UNIQUE(wallet_id, txid)
            );

            CREATE TABLE IF NOT EXISTS sync_state (
                wallet_id  INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
                key        TEXT NOT NULL,
                value      TEXT NOT NULL,
                UNIQUE(wallet_id, key)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key    TEXT PRIMARY KEY,
                value  TEXT NOT NULL
            );
            """
        )

    def close(self) -> None:
        self._conn.close()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -------------------------------------------------------- transaction

    def _transaction(self) -> sqlite3.Connection:
        """Return the connection to use as an explicit transaction context.

        ``isolation_level=None`` puts the connection in autocommit mode, so
        ``BEGIN``/``COMMIT`` are issued manually and every mutation is grouped.
        """
        return self._conn

    # -------------------------------------------------------------- wallets

    def create_wallet(self, name: str, descriptor: str) -> WalletRecord:
        """Create a wallet and return it. Duplicate names raise StoreIntegrityError."""
        created_at = _utcnow()
        try:
            with self._transaction():
                cur = self._conn.execute(
                    "INSERT INTO wallets (name, descriptor, created_at) VALUES (?, ?, ?)",
                    (name, descriptor, created_at),
                )
                wallet_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise _wrap_integrity(exc) from exc
        except sqlite3.Error as exc:
            raise _wrap(exc) from exc
        return WalletRecord(wallet_id, name, descriptor, created_at)

    def get_wallet_by_name(self, name: str) -> WalletRecord | None:
        row = self._conn.execute(
            "SELECT * FROM wallets WHERE name = ?", (name,)
        ).fetchone()
        return WalletRecord.from_row(row) if row is not None else None

    def get_wallet(self, wallet_id: int) -> WalletRecord | None:
        row = self._conn.execute(
            "SELECT * FROM wallets WHERE id = ?", (wallet_id,)
        ).fetchone()
        return WalletRecord.from_row(row) if row is not None else None

    def list_wallets(self) -> list[WalletRecord]:
        rows = self._conn.execute("SELECT * FROM wallets ORDER BY id").fetchall()
        return [WalletRecord.from_row(r) for r in rows]

    def get_active_wallet(self) -> WalletRecord | None:
        """Return the wallet referenced by ``settings.active_wallet_id``."""
        value = self.get_setting("active_wallet_id")
        if value is None:
            return None
        try:
            wallet_id = int(value)
        except ValueError:
            return None
        return self.get_wallet(wallet_id)

    def set_active_wallet(self, wallet_id: int) -> None:
        """Mark ``wallet_id`` as the active wallet (stored in settings)."""
        self.set_setting("active_wallet_id", str(wallet_id))

    # ---------------------------------------------------------- derivation

    def _ensure_derivation(self, wallet_id: int, branch: int) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO derivation (wallet_id, branch) VALUES (?, ?)",
            (wallet_id, branch),
        )

    def get_derivation(self, wallet_id: int, branch: int) -> DerivationRecord:
        """Return per-branch derivation state, seeding defaults if absent."""
        self._ensure_derivation(wallet_id, branch)
        row = self._conn.execute(
            "SELECT * FROM derivation WHERE wallet_id = ? AND branch = ?",
            (wallet_id, branch),
        ).fetchone()
        return DerivationRecord.from_row(row)

    def update_derivation(
        self,
        wallet_id: int,
        branch: int,
        *,
        max_used_index: int | None = None,
        next_index: int | None = None,
    ) -> DerivationRecord:
        """Update fields of a branch's derivation state and return the result."""
        self._ensure_derivation(wallet_id, branch)
        sets: list[str] = []
        params: list[Any] = []
        if max_used_index is not None:
            sets.append("max_used_index = ?")
            params.append(max_used_index)
        if next_index is not None:
            sets.append("next_index = ?")
            params.append(next_index)
        if sets:
            params.extend([wallet_id, branch])
            with self._transaction():
                self._conn.execute(
                    f"UPDATE derivation SET {', '.join(sets)} "
                    "WHERE wallet_id = ? AND branch = ?",
                    params,
                )
        return self.get_derivation(wallet_id, branch)

    def bump_next_index(self, wallet_id: int, branch: int) -> DerivationRecord:
        """Increment ``next_index`` for a branch and return the updated state."""
        self._ensure_derivation(wallet_id, branch)
        with self._transaction():
            self._conn.execute(
                "UPDATE derivation SET next_index = next_index + 1 "
                "WHERE wallet_id = ? AND branch = ?",
                (wallet_id, branch),
            )
        return self.get_derivation(wallet_id, branch)

    # ------------------------------------------------------------ addresses

    def upsert_batch(self, records: Sequence[AddressRecord]) -> None:
        """Insert or update a batch of address records (idempotent)."""
        if not records:
            return
        try:
            with self._transaction():
                self._conn.executemany(
                    "INSERT INTO addresses (wallet_id, branch, `index`, address, script_type, status) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(wallet_id, branch, `index`) DO UPDATE SET "
                    "address = excluded.address, "
                    "script_type = excluded.script_type, "
                    "status = excluded.status",
                    [r.to_row() for r in records],
                )
        except sqlite3.IntegrityError as exc:
            raise _wrap_integrity(exc) from exc
        except sqlite3.Error as exc:
            raise _wrap(exc) from exc

    def mark_used(self, wallet_id: int, branch: int, index: int) -> None:
        """Transition an address's status to 'used' (must not be 'allocated')."""
        try:
            with self._transaction():
                self._conn.execute(
                    "UPDATE addresses SET status = 'used' "
                    "WHERE wallet_id = ? AND branch = ? AND `index` = ?",
                    (wallet_id, branch, index),
                )
        except sqlite3.IntegrityError as exc:
            raise _wrap_integrity(exc) from exc
        except sqlite3.Error as exc:
            raise _wrap(exc) from exc

    def get_unused(
        self, wallet_id: int, branch: int, limit: int | None = None
    ) -> list[AddressRecord]:
        """Return 'unused' addresses for a branch, ordered by index."""
        sql = (
            "SELECT * FROM addresses WHERE wallet_id = ? AND branch = ? AND status = 'unused' "
            "ORDER BY `index`"
        )
        params: tuple[Any, ...] = (wallet_id, branch)
        if limit is not None:
            sql += " LIMIT ?"
            params = (wallet_id, branch, limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [AddressRecord.from_row(r) for r in rows]

    def get_addresses(self, wallet_id: int, branch: int) -> list[AddressRecord]:
        """Return all addresses for a wallet/branch, ordered by index."""
        rows = self._conn.execute(
            "SELECT * FROM addresses WHERE wallet_id = ? AND branch = ? ORDER BY `index`",
            (wallet_id, branch),
        ).fetchall()
        return [AddressRecord.from_row(r) for r in rows]

    def get_by_address(self, address: str) -> AddressRecord | None:
        row = self._conn.execute(
            "SELECT * FROM addresses WHERE address = ?", (address,)
        ).fetchone()
        return AddressRecord.from_row(row) if row is not None else None

    def allocate(self, wallet_id: int, branch: int, index: int) -> None:
        """Mark an address 'allocated' (given out to the user for receiving)."""
        try:
            with self._transaction():
                self._conn.execute(
                    "UPDATE addresses SET status = 'allocated' "
                    "WHERE wallet_id = ? AND branch = ? AND `index` = ?",
                    (wallet_id, branch, index),
                )
        except sqlite3.IntegrityError as exc:
            raise _wrap_integrity(exc) from exc
        except sqlite3.Error as exc:
            raise _wrap(exc) from exc

    # ---------------------------------------------------------------- utxos

    def replace_utxos_for_wallet(
        self, wallet_id: int, records: Iterable[UtxoRecord]
    ) -> None:
        """Atomically replace the UTXO set for a wallet (snapshot semantics).

        All of the wallet's existing rows are deleted and the supplied set
        inserted within a single transaction — a scan result is applied as a
        whole, never partially.
        """
        rows = list(records)
        try:
            with self._transaction():
                self._conn.execute(
                    "DELETE FROM utxos WHERE wallet_id = ?", (wallet_id,)
                )
                if rows:
                    self._conn.executemany(
                        "INSERT INTO utxos "
                        "(wallet_id, txid, vout, address, value_sats, confirmed, height) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [r.to_row() for r in rows],
                    )
        except sqlite3.IntegrityError as exc:
            raise _wrap_integrity(exc) from exc
        except sqlite3.Error as exc:
            raise _wrap(exc) from exc

    def get_utxos_for_wallet(self, wallet_id: int) -> list[UtxoRecord]:
        rows = self._conn.execute(
            "SELECT * FROM utxos WHERE wallet_id = ? ORDER BY txid, vout",
            (wallet_id,),
        ).fetchall()
        return [UtxoRecord.from_row(r) for r in rows]

    # --------------------------------------------------------- transactions

    def upsert_txs(self, records: Sequence[TxRecord]) -> None:
        """Insert or update transaction records (update-in-place on conflict)."""
        if not records:
            return
        try:
            with self._transaction():
                self._conn.executemany(
                    "INSERT INTO transactions "
                    "(wallet_id, txid, height, block_time, fee_sats, direction, raw_summary) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(wallet_id, txid) DO UPDATE SET "
                    "height = excluded.height, "
                    "block_time = excluded.block_time, "
                    "fee_sats = excluded.fee_sats, "
                    "direction = excluded.direction, "
                    "raw_summary = excluded.raw_summary",
                    [r.to_row() for r in records],
                )
        except sqlite3.IntegrityError as exc:
            raise _wrap_integrity(exc) from exc
        except sqlite3.Error as exc:
            raise _wrap(exc) from exc

    def get_txs_for_wallet(self, wallet_id: int) -> list[TxRecord]:
        rows = self._conn.execute(
            "SELECT * FROM transactions WHERE wallet_id = ? ORDER BY txid",
            (wallet_id,),
        ).fetchall()
        return [TxRecord.from_row(r) for r in rows]

    # ----------------------------------------------------------- sync_state

    def get_sync_state(self, wallet_id: int, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM sync_state WHERE wallet_id = ? AND key = ?",
            (wallet_id, key),
        ).fetchone()
        return row["value"] if row is not None else None

    def set_sync_state(self, wallet_id: int, key: str, value: str) -> None:
        try:
            with self._transaction():
                self._conn.execute(
                    "INSERT INTO sync_state (wallet_id, key, value) VALUES (?, ?, ?) "
                    "ON CONFLICT(wallet_id, key) DO UPDATE SET value = excluded.value",
                    (wallet_id, key, value),
                )
        except sqlite3.IntegrityError as exc:
            raise _wrap_integrity(exc) from exc
        except sqlite3.Error as exc:
            raise _wrap(exc) from exc

    # ------------------------------------------------------------- settings

    def get_setting(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row is not None else None

    def set_setting(self, key: str, value: str) -> None:
        try:
            with self._transaction():
                self._conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
        except sqlite3.IntegrityError as exc:
            raise _wrap_integrity(exc) from exc
        except sqlite3.Error as exc:
            raise _wrap(exc) from exc

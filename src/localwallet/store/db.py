"""SQLite-backed store for local-wallet (TCK-P1-001).

What the store is
-----------------
A thin, typed persistence layer over a single SQLite database (WAL). It backs
Phase 1: wallet profiles, per-branch derivation state, derived addresses,
UTXO cache, transaction cache, sync cursor, and application settings — plus
(schema v2, TCK-UTXO-001) outpoint-keyed coin labels: closed-set tags + one
free-text note per coin, stored in their own table so they survive the scan
snapshot's DELETE+re-INSERT. All SQL
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
the database*, but they are **never placed into log/exception text**. Coin
label text is the same class of user data (design doc §1.1): stored verbatim,
never echoed into a message. Every :class:`StoreError` message carries only
table/operation context — never a value. This module deliberately performs no
``logging``; errors are raised for the caller to handle, with scrubbed
messages.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from localwallet.config import (
    COIN_SETTING_BOUNDS,
    COIN_SETTING_DEFAULTS,
    UTXO_TARGET_MAX_SETTING,
    UTXO_TARGET_MIN_SETTING,
)
from localwallet.store.models import (
    COIN_NOTE_MAX_CHARS,
    COIN_TAGS,
    AddressRecord,
    CoinLabelRecord,
    DerivationRecord,
    TxRecord,
    UtxoRecord,
    WalletRecord,
    normalize_coin_tags,
)

# Current schema version, tracked via ``PRAGMA user_version``. Bump this and
# add an up-migration whenever the schema changes; never down-migrate.
# v2 (TCK-UTXO-001): the ``coin_labels`` table (docs/ux-utxo-notes-design.md
# §1.3) — outpoint-keyed, deliberately SEPARATE from the UTXO snapshot so
# user labels survive every rescan.
SCHEMA_VERSION = 2

_BUSY_TIMEOUT_MS = 5000

#: Settings key for the persisted chain backend choice (ADR-0023, TCK-ONB-002).
#: Private: access only through :meth:`Store.get_chain_base_url` /
#: :meth:`Store.set_chain_base_url`, which own the write validation.
_CHAIN_BASE_URL_SETTING = "chain_base_url"

# Coin-selection policy settings (TCK-UTXO-002, docs/ux-utxo-notes-design.md
# §2.3): keys, bounds and shipped defaults are owned by localwallet.config
# (the env > stored > default ladder lives there; config imports nothing from
# store, so this direction is cycle-free). See :meth:`Store.get_coin_setting`
# / :meth:`Store.set_coin_setting`.


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
    """Raised when the on-disk schema cannot be (safely) brought up to date."""


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


_TXID_HEX_CHARS = frozenset("0123456789abcdef")


def _check_label_txid(txid: object) -> None:
    """Fail-closed shape check for a coin-label txid (value-free error).

    Mirrors the envelope layer's rule (exactly 64 lowercase hex): a label is
    keyed by outpoint, so a malformed id is a caller bug, refused before disk.
    The offending value is never echoed into the message.
    """
    if (
        not isinstance(txid, str)
        or len(txid) != 64
        or any(c not in _TXID_HEX_CHARS for c in txid)
    ):
        raise StoreError("coin label txid must be 64 lowercase hex characters")


def _check_label_vout(vout: object) -> None:
    """Fail-closed shape check for a coin-label vout (non-negative int)."""
    if not isinstance(vout, int) or isinstance(vout, bool) or vout < 0:
        raise StoreError("coin label vout must be a non-negative integer")


def _check_label_outpoint(txid: object, vout: object) -> None:
    """Validate a full (txid, vout) outpoint used as a coin_labels key."""
    _check_label_txid(txid)
    _check_label_vout(vout)


# ----------------------------------------------------------------- shared SQL
#
# Single source of truth for the write paths shared by the public accessors
# and :meth:`Store.persist_scan_result` (the composite atomic scan persist).
# Statements never embed values in their text — all values are bound params.

_ADDRESS_UPSERT_SQL = (
    "INSERT INTO addresses (wallet_id, branch, `index`, address, script_type, status) "
    "VALUES (?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(wallet_id, branch, `index`) DO UPDATE SET "
    "address = excluded.address, "
    "script_type = excluded.script_type, "
    "status = excluded.status"
)

_DERIVATION_UPSERT_SQL = (
    "INSERT INTO derivation (wallet_id, branch, max_used_index, next_index) "
    "VALUES (?, ?, ?, ?) "
    "ON CONFLICT(wallet_id, branch) DO UPDATE SET "
    "max_used_index = excluded.max_used_index, "
    "next_index = excluded.next_index"
)

_UTXO_INSERT_SQL = (
    "INSERT INTO utxos "
    "(wallet_id, txid, vout, address, value_sats, confirmed, height) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)

_TX_UPSERT_SQL = (
    "INSERT INTO transactions "
    "(wallet_id, txid, height, block_time, fee_sats, direction, raw_summary) "
    "VALUES (?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(wallet_id, txid) DO UPDATE SET "
    "height = excluded.height, "
    "block_time = excluded.block_time, "
    "fee_sats = excluded.fee_sats, "
    "direction = excluded.direction, "
    "raw_summary = excluded.raw_summary"
)

_SYNC_STATE_UPSERT_SQL = (
    "INSERT INTO sync_state (wallet_id, key, value) VALUES (?, ?, ?) "
    "ON CONFLICT(wallet_id, key) DO UPDATE SET value = excluded.value"
)

# coin_labels (schema v2, TCK-UTXO-001) — outpoint-keyed per-coin tags + note.
# ``tags`` is a canonical-comma-joined closed-set string (NOT NULL — an
# unlabeled coin has NO row, per §1.3, never an empty-string row); ``note`` is
# verbatim free text, nullable. The (wallet_id, txid, vout) primary key lives
# on the OUTPOINT, independent of the ephemeral utxos snapshot. FK cascade ties
# it to the wallet (delete the wallet → its labels go too).
_COIN_LABELS_DDL = """
            CREATE TABLE IF NOT EXISTS coin_labels (
                wallet_id  INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
                txid       TEXT NOT NULL,
                vout       INTEGER NOT NULL,
                tags       TEXT NOT NULL,
                note       TEXT,
                PRIMARY KEY (wallet_id, txid, vout)
            );
"""

_COIN_LABEL_UPSERT_SQL = (
    "INSERT INTO coin_labels (wallet_id, txid, vout, tags, note) "
    "VALUES (?, ?, ?, ?, ?) "
    "ON CONFLICT(wallet_id, txid, vout) DO UPDATE SET "
    "tags = excluded.tags, "
    "note = excluded.note"
)


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
        """Bring the on-disk schema up to :data:`SCHEMA_VERSION` via
        ``PRAGMA user_version``-gated, incremental up-migrations.

        Versioned-init contract (follows the v1 pattern, extended for the
        single real v1→v2 step the coin-labels ticket needs):

        * ``current > SCHEMA_VERSION`` — a DB from a NEWER build: fail closed,
          never open/interpret an unknown schema (no down-migration exists).
        * ``current == 0`` — a brand-new (or legacy-unversioned) file: build
          the FULL current schema from scratch and stamp the version.
        * ``0 < current < SCHEMA_VERSION`` — an existing versioned DB: run the
          ordered up-migration steps for exactly the versions above ``current``
          (here the v1→v2 add of ``coin_labels``) and re-stamp. A version with
          no registered migration path is refused (fail closed).

        Atomicity, stated honestly (TCK-UTXO-002 security-review LOW): the
        steps are NOT one SQLite transaction — each runs as its own statement
        (``executescript`` commits beforehand), so durability rests on the
        contract itself: every migration step is IDEMPOTENT (purely additive,
        ``CREATE TABLE IF NOT EXISTS``), and the ``user_version`` stamp is
        written LAST. A crash between a step and the stamp leaves the DB at
        the old version with the additive step applied — the next open simply
        re-runs the idempotent step and re-stamps. No step ever destructively
        rewrites data, so per-statement atomicity is sufficient.
        """
        conn = self._conn
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            # Fail closed: never open a DB from a newer (unknown) schema.
            raise _MigrateError(
                "database schema is newer than this build supports; "
                "refusing to open (upgrade the application first)"
            )
        if current == SCHEMA_VERSION:
            return  # already at the target schema (normal reopen)
        if current == 0:
            # Brand-new (or a legacy un-versioned file that is in fact empty):
            # build the full current schema from scratch.
            with self._transaction():
                self._create_schema()
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return
        # Existing versioned DB below the target: apply the ordered
        # ``_migrate_v<from>_to_v<from+1>`` steps still pending. A gap in the
        # ladder has no known-safe repair, so it is refused (fail closed, no
        # down-migration path).
        with self._transaction():
            for version in range(current, SCHEMA_VERSION):
                step = self._MIGRATIONS.get(version)
                if step is None:
                    raise _MigrateError(
                        f"database schema version {version} has no known "
                        "up-migration; refusing to migrate"
                    )
                step(self, conn)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _migrate_v1_to_v2(self, conn: sqlite3.Connection) -> None:
        """v1→v2 (TCK-UTXO-001): add the outpoint-keyed ``coin_labels`` table.

        Purely additive — existing v1 rows are untouched. The table lives in a
        SEPARATE structure from ``utxos`` on purpose (design doc §1.3): the UTXO
        snapshot is DELETE+re-INSERTed by every scan, so labels stored on the
        UTXO row would die on the next sync. Outpoint-keyed (wallet_id, txid,
        vout) rows survive rescans unchanged and stay on record after the coin
        is spent (§1.2: capture is post-broadcast; a spent coin's history is a
        fact the user keeps).
        """
        conn.executescript(_COIN_LABELS_DDL)

    #: Up-migration ladder keyed by the version it migrates FROM. Extend (never
    #: reorder or delete) as schema version bumps; a missing rung fails closed.
    _MIGRATIONS: ClassVar[dict[int, Callable[[sqlite3.Connection], None]]] = {
        1: _migrate_v1_to_v2,
    }

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
        conn.executescript(_COIN_LABELS_DDL)

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

    def _rollback_quietly(self) -> None:
        """Best-effort ``ROLLBACK`` (never masks the failure being handled)."""
        if self._conn.in_transaction:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass  # best effort: the in-flight failure propagates regardless

    @contextmanager
    def _atomic(self) -> Iterator[None]:
        """Run a block inside ONE explicit SQLite transaction (all-or-nothing).

        ``BEGIN IMMEDIATE`` opens the transaction (failing before any write,
        with the usual value-free wrapping); the block's writes commit
        together on success. Any exception — sqlite or otherwise — rolls the
        whole transaction back before propagating, leaving the database
        exactly as it was. sqlite failures surface as value-free
        :class:`StoreError`/:class:`StoreIntegrityError`; non-sqlite
        exceptions roll back and propagate unchanged (they indicate caller
        bugs, not store failures). Not nested — compose multiple writes by
        calling the shared ``_*_rows`` helpers inside a single block.
        """
        conn = self._conn
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.IntegrityError as exc:
            raise _wrap_integrity(exc) from exc
        except sqlite3.Error as exc:
            raise _wrap(exc) from exc
        try:
            yield
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            self._rollback_quietly()
            raise _wrap_integrity(exc) from exc
        except sqlite3.Error as exc:
            self._rollback_quietly()
            raise _wrap(exc) from exc
        except BaseException:
            self._rollback_quietly()
            raise

    # ------------------------- shared write bodies (no transaction of their own)

    def _upsert_address_rows(self, records: Sequence[AddressRecord]) -> None:
        """Address upserts; the caller owns the surrounding transaction."""
        if records:
            self._conn.executemany(_ADDRESS_UPSERT_SQL, [r.to_row() for r in records])

    def _upsert_derivation_states(self, states: Sequence[DerivationRecord]) -> None:
        """Derivation upserts; the caller owns the surrounding transaction."""
        if states:
            self._conn.executemany(_DERIVATION_UPSERT_SQL, [s.to_row() for s in states])

    def _replace_utxo_rows(self, wallet_id: int, records: Sequence[UtxoRecord]) -> None:
        """UTXO snapshot replace (DELETE + INSERT); the caller owns the transaction."""
        self._conn.execute("DELETE FROM utxos WHERE wallet_id = ?", (wallet_id,))
        if records:
            self._conn.executemany(_UTXO_INSERT_SQL, [r.to_row() for r in records])

    def _upsert_tx_rows(self, records: Sequence[TxRecord]) -> None:
        """Transaction upserts; the caller owns the surrounding transaction."""
        if records:
            self._conn.executemany(_TX_UPSERT_SQL, [r.to_row() for r in records])

    def _write_sync_state_entries(
        self, wallet_id: int, entries: Mapping[str, str]
    ) -> None:
        """sync_state upserts; the caller owns the surrounding transaction."""
        if entries:
            self._conn.executemany(
                _SYNC_STATE_UPSERT_SQL,
                [(wallet_id, key, value) for key, value in entries.items()],
            )

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
        """Insert or update a batch of address records (idempotent, atomic)."""
        if not records:
            return
        with self._atomic():
            self._upsert_address_rows(records)

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
        with self._atomic():
            self._replace_utxo_rows(wallet_id, rows)

    def get_utxos_for_wallet(self, wallet_id: int) -> list[UtxoRecord]:
        rows = self._conn.execute(
            "SELECT * FROM utxos WHERE wallet_id = ? ORDER BY txid, vout",
            (wallet_id,),
        ).fetchall()
        return [UtxoRecord.from_row(r) for r in rows]

    # ---------------------------------------------------------- coin labels
    #
    # TCK-UTXO-001 (docs/ux-utxo-notes-design.md §1.3/§1.4): per-OUTPOINT
    # closed-set tags + one free-text note. These typed accessors are the ONLY
    # sanctioned writers (the chain_base_url / gap_limit precedent): tag-set
    # and note-length validation live here, fail-closed, before anything
    # reaches disk; error messages are value-free (label text is user data,
    # same class as an address — never echoed into exceptions/logs). Labels
    # are consumed by DETERMINISTIC code only and NEVER enter model context.

    def set_coin_label(
        self,
        wallet_id: int,
        txid: str,
        vout: int,
        tags: Iterable[str] = (),
        note: str | None = None,
    ) -> CoinLabelRecord | None:
        """Set (replace) one coin's tags + note; returns the stored row.

        Multi-tag is allowed (the §1.4 partition classes combine; lineage
        unions input tag sets). An empty tag list stores "no tags"; a blank
        (``""``) note clears the note field while keeping the tags. With
        NEITHER tags nor a note the whole row is DELETED — a bare re-label
        clears (§1.3: "no /label with no tags and no note clears"), and an
        unlabeled coin is *no row*, never an empty row.

        Raises value-free :class:`StoreError` for an unknown tag, an
        over-long/blank-but-present note, or a malformed outpoint;
        :class:`StoreIntegrityError` for a FK violation (no such wallet).
        """
        _check_label_outpoint(txid, vout)
        try:
            canonical = normalize_coin_tags(tags)
        except ValueError as exc:
            raise StoreError("coin tags must come from the closed tag set") from exc
        if note is not None:
            if not isinstance(note, str):
                raise StoreError("coin note must be text")
            if len(note) > COIN_NOTE_MAX_CHARS:
                raise StoreError("coin note exceeds the maximum length")
            note = note or None
        if not canonical and note is None:
            self.clear_coin_label(wallet_id, txid, vout)
            return None
        record = CoinLabelRecord(wallet_id, txid, vout, canonical, note)
        try:
            with self._transaction():
                self._conn.execute(_COIN_LABEL_UPSERT_SQL, record.to_row())
        except sqlite3.IntegrityError as exc:
            raise _wrap_integrity(exc) from exc
        except sqlite3.Error as exc:
            raise _wrap(exc) from exc
        return record

    def get_coin_label(self, wallet_id: int, txid: str, vout: int) -> CoinLabelRecord | None:
        """The label row for one outpoint, or ``None`` (unlabeled)."""
        row = self._conn.execute(
            "SELECT * FROM coin_labels WHERE wallet_id = ? AND txid = ? AND vout = ?",
            (wallet_id, txid, vout),
        ).fetchone()
        return CoinLabelRecord.from_row(row) if row is not None else None

    def get_coin_labels(self, wallet_id: int) -> list[CoinLabelRecord]:
        """Every label row for a wallet (unspent AND spent coins — rows keyed
        by outpoint survive rescans and stay after a coin is spent, §1.2)."""
        rows = self._conn.execute(
            "SELECT * FROM coin_labels WHERE wallet_id = ? ORDER BY txid, vout",
            (wallet_id,),
        ).fetchall()
        return [CoinLabelRecord.from_row(r) for r in rows]

    def clear_coin_label(self, wallet_id: int, txid: str, vout: int) -> None:
        """Delete one coin's label row (idempotent: no row = already clear)."""
        _check_label_outpoint(txid, vout)
        try:
            with self._transaction():
                self._conn.execute(
                    "DELETE FROM coin_labels WHERE wallet_id = ? AND txid = ? AND vout = ?",
                    (wallet_id, txid, vout),
                )
        except sqlite3.Error as exc:
            raise _wrap(exc) from exc

    def propagate_coin_lineage(
        self,
        wallet_id: int,
        txid: str,
        output_vouts: Sequence[int],
        spent_inputs: Sequence[tuple[str, int]],
    ) -> None:
        """Lineage-on-broadcast (design doc §1.3): our transaction's outputs
        inherit the UNION of its wallet inputs' tag sets.

        Conservative taint: a coin made by mixing carries both classes and
        thereafter counts on both sides of the selection partition (kyc-side
        is the fail-safe direction). Notes are display-only history and are
        NEVER inherited; only tags are. Inputs without rows contribute
        nothing — a union with no tags writes no row at all (unlabeled stays
        unlabeled). Idempotent: an existing output row's tags merge into the
        same union and its note is preserved.

        Validation is fail-closed and value-free; the whole write is one
        transaction (never a partially-inherited coin).
        """
        _check_label_txid(txid)
        for in_txid, in_vout in spent_inputs:
            _check_label_outpoint(in_txid, in_vout)
        for vout in output_vouts:
            _check_label_vout(vout)
        input_tags: set[str] = set()
        try:
            for in_txid, in_vout in spent_inputs:
                row = self.get_coin_label(wallet_id, in_txid, in_vout)
                if row is not None:
                    input_tags.update(row.tags)
        except sqlite3.Error as exc:
            raise _wrap(exc) from exc
        if not input_tags:
            return  # unlabeled inputs → unlabeled outputs (no rows written)
        with self._atomic():
            for vout in output_vouts:
                tag_set = set(input_tags)
                existing = self.get_coin_label(wallet_id, txid, vout)
                if existing is not None:
                    tag_set.update(existing.tags)
                tags = tuple(tag for tag in COIN_TAGS if tag in tag_set)
                self._conn.execute(
                    _COIN_LABEL_UPSERT_SQL,
                    (
                        wallet_id,
                        txid,
                        vout,
                        ",".join(tags),
                        existing.note if existing is not None else None,
                    ),
                )

    # --------------------------------------------------------- transactions

    def upsert_txs(self, records: Sequence[TxRecord]) -> None:
        """Insert or update transaction records (update-in-place on conflict)."""
        if not records:
            return
        with self._atomic():
            self._upsert_tx_rows(records)

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
        with self._atomic():
            self._write_sync_state_entries(wallet_id, {key: value})

    # -------------------------------------------------- composite scan persist

    def persist_scan_result(
        self,
        wallet_id: int,
        *,
        address_rows: Sequence[AddressRecord],
        derivation_states: Sequence[DerivationRecord],
        utxo_snapshot: Iterable[UtxoRecord],
        tx_rows: Sequence[TxRecord],
        sync_state_updates: Mapping[str, str],
    ) -> None:
        """Persist one completed chain scan in a SINGLE SQLite transaction.

        The whole write-set of a scan — address statuses, per-branch
        derivation state, the wallet-wide UTXO snapshot (full replace),
        transaction history, and the sync-state updates (including the
        every-scan ``out_of_window_detected`` write, whose empty payload
        clears a stale warning) — commits together or not at all: a crash
        or failure mid-write can never desync address statuses from the
        derivation cursor or the sync state.

        On any failure the transaction is rolled back and the database is
        left exactly as it was. sqlite failures raise value-free
        :class:`StoreIntegrityError` (constraint violations) or
        :class:`StoreError`; any other exception rolls back and propagates
        unchanged (a caller bug, not a store failure). ``wallet_id`` must
        reference an existing wallet (FK-enforced). Statement order inside
        the transaction: addresses, derivation, UTXO replace, transactions,
        sync state — no order dependencies exist (every row references only
        the pre-existing wallet row).

        Coin labels (schema v2) are DELIBERATELY absent from this write-set:
        the UTXO snapshot replace touches only the ``utxos`` table, so
        outpoint-keyed ``coin_labels`` rows survive every rescan unchanged
        (and remain after the coin is spent) — see :meth:`set_coin_label`.
        """
        snapshot = list(utxo_snapshot)
        with self._atomic():
            self._upsert_address_rows(address_rows)
            self._upsert_derivation_states(derivation_states)
            self._replace_utxo_rows(wallet_id, snapshot)
            self._upsert_tx_rows(tx_rows)
            self._write_sync_state_entries(wallet_id, sync_state_updates)

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

    # ------------------------------------------- chain backend choice (ONB-002)
    #
    # The persisted first-run backend selection (ADR-0023, TCK-ONB-002). Same
    # key-value mechanism as ``gap_limit``; the typed pair below is the ONLY
    # sanctioned reader/writer of the key so validation can never be skipped.
    # Resolution (env > stored > default) lives in
    # :func:`localwallet.config.resolve_chain_base_url` — ``config`` never
    # imports this module; the startup wiring (TCK-ONB-003) reads here and
    # injects the value there.

    def get_chain_base_url(self) -> str | None:
        """Return the stored chain backend base URL, or ``None``.

        ``None`` means "no choice stored" — the unset rung of the ADR-0023
        precedence (the public default applies). A value that is only
        whitespace cannot exist on disk (writes are validated), so it cannot
        surface here either.
        """
        value = self.get_setting(_CHAIN_BASE_URL_SETTING)
        if value is None or not value.strip():
            return None
        return value

    def set_chain_base_url(self, url: str) -> None:
        """Persist the user's chain backend choice; ``""`` clears it (back to default).

        Validation is fail-closed at write, before anything lands on disk
        (ADR-0023 decision 5): a non-empty value must be an http(s) Esplora
        base URL or an ``ssl://host[:port]`` Electrum endpoint (TCK-BACKEND-002;
        ADR-0018 M3 acceptance — the M1 adapter ships, the stored rung now
        carries it), each with a host and no embedded credentials — mirroring
        the ``ChainConfig`` construction check that stays as the last line of
        defense. A whitespace-only write is refused (deliberate-but-blank is
        malformed, never a silent clear); only the exact empty string clears
        the choice. Errors are value-free: the URL (which may embed
        credentials) never appears in the message. NOTE: this writer checks
        the URL SHAPE only — reachability/mainnet proof is the app's probe
        (chain/), deliberately not the store's job.
        """
        candidate = url.strip()
        if not candidate:
            if url:
                raise StoreError("chain base url must not be blank when set")
            with self._transaction():
                self._conn.execute(
                    "DELETE FROM settings WHERE key = ?", (_CHAIN_BASE_URL_SETTING,)
                )
            return
        # Plain-string checks (no urllib: network-ish imports are lint-banned
        # outside chain/). As strict as the ChainConfig construction check it
        # mirrors, plus an internal-whitespace guard.
        if candidate.startswith("ssl://"):
            self._check_electrum_base_url(candidate)
            self.set_setting(_CHAIN_BASE_URL_SETTING, candidate)
            return
        if not candidate.startswith(("http://", "https://")):
            raise StoreError("chain base url must be an http(s) or ssl:// URL")
        if any(c.isspace() for c in candidate):
            raise StoreError("chain base url must not contain whitespace")
        netloc = candidate.partition("://")[2].split("/", 1)[0]
        if not netloc:
            raise StoreError("chain base url must have a host")
        if "@" in netloc:  # embedded userinfo would ride on every request
            raise StoreError("chain base url must not embed credentials")
        self.set_setting(_CHAIN_BASE_URL_SETTING, candidate)

    @staticmethod
    def _check_electrum_base_url(candidate: str) -> None:
        """Shape rules for a stored ``ssl://host[:port]`` Electrum endpoint
        (mirrors :meth:`ChainConfig._validate_electrum_url` value-free): a
        host, an optional NUMERIC in-range port, no userinfo, and no
        path/query/fragment (the protocol has no URL namespace)."""
        rest = candidate[len("ssl://") :]
        if any(c.isspace() for c in rest):
            raise StoreError("chain base url must not contain whitespace")
        if any(c in rest for c in "/?#"):
            raise StoreError("electrum chain base url must not carry a path")
        host, sep, port = rest.rpartition(":")
        if "@" in rest:  # embedded credentials — never storable
            raise StoreError("chain base url must not embed credentials")
        if sep:
            if not host or not port.isdigit() or not 0 < int(port) < 65536:
                raise StoreError("electrum chain base url has an invalid port")
        elif not rest:
            raise StoreError("chain base url must have a host")

    # ------------------------------------ coin-selection policy settings (UTXO-002)
    #
    # The three doc §2.3 keys (utxo_target_min_sats / utxo_target_max_sats /
    # consolidate_below_sat_vb). Same key/value mechanism as gap_limit and
    # chain_base_url; the typed pair below is the ONLY sanctioned writer, so
    # write-time validation can never be skipped (the web settings page and
    # /settings, TCK-UTXO-003, reuse it verbatim). Bounds/defaults are single-
    # sourced from localwallet.config (the env > stored > default ladder and
    # the startup fail-closed re-check live there; config imports no store).

    def get_coin_setting(self, key: str) -> str | None:
        """Typed reader for a coin-selection setting (``None`` = unset rung)."""
        if key not in COIN_SETTING_BOUNDS:
            raise StoreError("unknown coin setting key")
        return self.get_setting(key)

    def set_coin_setting(self, key: str, value: str) -> None:
        """Persist a coin-selection setting; ``""`` clears it (back to default).

        Fail-closed at write, before anything lands on disk (ADR-0009 /
        ADR-0023 pattern): the key must be one of the three managed settings;
        the value must be a plain ASCII decimal integer within the key's
        bound, and the resulting min/max PAIR (this write plus the stored
        sibling, or its shipped default when unset) must satisfy ``min < max``
        — a corrupt pair is malformed and never silently flips selection
        policy. Errors are value-free: nothing the user typed is echoed.
        """
        bounds = COIN_SETTING_BOUNDS.get(key)
        if bounds is None:
            raise StoreError("unknown coin setting key")
        if not isinstance(value, str):
            raise StoreError("coin setting must be text")
        if not value:
            with self._transaction():
                self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            return
        if not value.isascii() or not value.isdigit():
            raise StoreError(f"setting {key!r} must be a plain decimal integer")
        parsed = int(value)
        lo, hi = bounds
        if not lo <= parsed <= hi:
            raise StoreError(f"setting {key!r} must be between {lo} and {hi}")
        effective = {
            UTXO_TARGET_MIN_SETTING: parsed
            if key == UTXO_TARGET_MIN_SETTING
            else self._effective_coin_setting(UTXO_TARGET_MIN_SETTING),
            UTXO_TARGET_MAX_SETTING: parsed
            if key == UTXO_TARGET_MAX_SETTING
            else self._effective_coin_setting(UTXO_TARGET_MAX_SETTING),
        }
        if effective[UTXO_TARGET_MIN_SETTING] >= effective[UTXO_TARGET_MAX_SETTING]:
            raise StoreError(
                "utxo target minimum must stay below the maximum "
                "(raise the maximum or lower the minimum)"
            )
        self.set_setting(key, value)

    def _effective_coin_setting(self, key: str) -> int:
        """Stored value, else the shipped default (for the write cross-check).

        Stored values are only ever written through this pair, so they are
        parseable here by construction; anything else on disk (a hand-edited
        DB) resolves to the default at write time and is caught fail-closed
        by the startup ladder (:func:`localwallet.config.resolve_coin_selection_settings`),
        not silently adopted.
        """
        raw = self.get_setting(key)
        if raw is None or not raw.isascii() or not raw.isdigit():
            return COIN_SETTING_DEFAULTS[key]
        return int(raw)

"""SQLite-backed store for local-wallet (TCK-P1-001).

What the store is
-----------------
A thin, typed persistence layer over a single SQLite database (WAL). It backs
Phase 1: wallet profiles, per-branch derivation state, derived addresses,
UTXO cache, transaction cache, sync cursor, and application settings — plus
(schema v3, TCK-RBF-001) transaction lineage/capture columns (amount_sats,
fee_rate_centisat_vb, first_seen, replaced_by_txid) — the broadcast-time
record RBF/CPFP disambiguation is built on — (schema v4, TCK-CHAT-001) the
``address_registry`` table: stable wallet-lifetime address numbers and
first-shown timestamps, and (schema v6, TCK-LABELS-UNIFY) the
``address_label_set`` table: the ADDRESS-keyed label SET — the labeling
source of truth (one address = one private key = one provenance; coins
INHERIT their address's set for the selection engine; the v5 ``address_labels``
single label and the v2 outpoint-keyed ``coin_labels`` fold into it by
per-address union on migration). The legacy ``coin_labels`` table is retained
WRITE-FROZEN on upgraded databases as preserved history — ALL of its rows stay
(resolvable and spent alike); the migration merely COPIES what resolves to an
address into the set, leaving the frozen table as-is; from v6 on NOTHING in
code writes or reads it — no live surface consumes it. All
SQL lives inside this module; callers use the typed accessor methods and
row records from :mod:`localwallet.store.models`. No raw SQL outside ``store/``.

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
the database*, but they are **never placed into log/exception text**. Label
text is the same class of user data (design doc §1.1): stored verbatim,
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
    ADDRESS_LABEL_MAX_CHARS,
    COIN_TAGS,
    AddressRecord,
    AddressRegistryRecord,
    DerivationRecord,
    TxRecord,
    UtxoRecord,
    WalletRecord,
)

# Current schema version, tracked via ``PRAGMA user_version``. Bump this and
# add an up-migration whenever the schema changes; never down-migrate.
# v2 (TCK-UTXO-001, superseded as an INPUT by v6): the ``coin_labels`` table —
# outpoint-keyed tags + note; v6 folds its content per-address union into
# ``address_label_set`` and retains the table (write-frozen, history only) on
# upgraded DBs because spent coins' label rows cannot be attributed to an
# address from this DB, and dropping user data silently is forbidden.
# v3 (TCK-RBF-001): the ``transactions`` lineage/capture columns — amount_sats,
# fee_rate_centisat_vb, first_seen (broadcast-time capture off the flow's
# confirmed record) and replaced_by_txid (RBF lineage link). All nullable;
# purely additive; legacy rows keep NULLs and read as "not recorded".
# v4 (TCK-CHAT-001): the ``address_registry`` table — stable wallet-lifetime
# address numbers + first-shown timestamps. A SEPARATE table (not columns on
# ``addresses``) on purpose: registry state is a DISPLAY fact (assigned when
# the user is shown an address), orthogonal to derivation/scan state that the
# scan upserts own; one more writer to the ``addresses`` row (via the shared
# upsert or a second UPDATE inside the scan transaction) would couple two
# unrelated lifecycles for zero gain. The address-keyed PRIMARY KEY mirrors
# the ``addresses`` table's natural key (address is globally UNIQUE there).
# v5 (TCK-LABEL-001, folded by v6): the ``address_labels`` table — one
# free-text label keyed by ADDRESS. Its whole content maps cleanly into the
# v6 set (the key already IS the address), so the v5→v6 rung copies and drops
# it: the two-address-label-sources split v5 left behind (address free text
# here, engine tags on coin_labels, neither feeding the other) is exactly the
# inconsistency this unification ends.
# v6 (TCK-LABELS-UNIFY, USER MODEL ratified 2026-09-13): the
# ``address_label_set`` table — the ADDRESS-keyed label SET as the labeling
# source of truth. One address = one private key = one provenance ("you can't
# have KYC coins and KYC-free coins at the same address"), so labeling the
# ADDRESS is the honest unit: members are the closed tag vocabulary
# (``store.models.COIN_TAGS`` — engine vocabulary, canonicalized at write)
# plus free-text labels (display-only), coins INHERIT the set for the
# selection engine, and a per-UTXO label is just another addition to that
# address's set (union). Keyed (address, label) PRIMARY KEY — the same
# global-uniqueness discipline as ``addresses.address`` and v5's
# ``address_labels``; OUTSIDE the scan write-set like the registry, so a
# rescan can never clear, re-assign, or fabricate a label. The foundation
# TCK-CHAT-003's address-level label ask and TCK-CHAT-007's receive+label
# dual action build on.
SCHEMA_VERSION = 6

_BUSY_TIMEOUT_MS = 5000

#: Settings key for the persisted chain backend choice (ADR-0023, TCK-ONB-002).
#: Private: access only through :meth:`Store.get_chain_base_url` /
#: :meth:`Store.set_chain_base_url`, which own the write validation.
_CHAIN_BASE_URL_SETTING = "chain_base_url"

#: Backend credential settings keys (TCK-ONB-004 M3; ADR-0018 M3 amendment).
#: Private: access only through the typed pairs below — the ONLY sanctioned
#: writers, so validation can never be skipped and the values can never grow
#: unbounded or carry header-injection bytes. THREAT MODEL (documented,
#: deliberate): the passwords live in the same local single-user SQLite file
#: as the wallet descriptor and the backend URL — the same trust surface.
#: A local Core/electrum login is low-sensitivity (it gates a node the user
#: already runs); OS-keyring is future work (plan OQ-4). The values are
#: never logged, never echoed in any refusal or settings read (the
#: /settings surface exposes only whether each key is SET), and never sent
#: anywhere but to the user's own backend.
_BACKEND_AUTH_USER_SETTING = "backend_auth_user"
_BACKEND_AUTH_PASS_SETTING = "backend_auth_pass"
_BACKEND_AUTH_NONE_SETTING = "backend_auth_none"

#: Shape cap for a stored credential (HTTP Basic user/password). A real
#: Core rpcuser/rpcpassword is a short printable token; anything longer,
#: non-ASCII, or whitespace-bearing is refused at the write (the SAME rule
#: the ``bitcoind://`` URL userinfo has since M2 — percent-encode or keep it
#: out of the store; a half-pair is caught by the app's resolver, which uses
#: basic auth only when BOTH parts are set). Control characters (CR/LF
#: included) are refused outright: these values are joined into an
#: ``Authorization`` header.
_MAX_BACKEND_AUTH_CHARS = 256

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


def _check_txid_shape(txid: object, context: str) -> None:
    """Fail-closed shape check for any store-layer txid (value-free error).

    Mirrors the envelope layer's rule (exactly 64 lowercase hex): the
    offending value is never echoed into the message. ``context`` is
    code-owned wording (table/operation), never user input.
    """
    if (
        not isinstance(txid, str)
        or len(txid) != 64
        or any(c not in _TXID_HEX_CHARS for c in txid)
    ):
        raise StoreError(f"{context} txid must be 64 lowercase hex characters")


def _check_address_key_shape(address: object, context: str) -> None:
    """Fail-closed shape gate for any address-keyed store table (value-free).

    A key must be a non-empty printable string without whitespace, ≤ 100
    characters (generous transport bound over the 90-char bech32 ceiling).
    Semantic validation (mainnet bech32 etc.) belongs to the caller-facing
    parser, not here — this gate only refuses things that cannot be keys.
    ``context`` is code-owned wording, never user input.
    """
    if (
        not isinstance(address, str)
        or not address
        or len(address) > 100
        or not address.isprintable()
        or any(c.isspace() for c in address)
    ):
        raise StoreError(f"{context} key must be a printable address string")


def _normalize_label_member(label: object) -> str:
    """Fail-closed, value-free gate + canonicalization for ONE address-label-
    set member (TCK-LABELS-UNIFY, schema v6).

    A member must be a non-blank printable string of at most
    :data:`ADDRESS_LABEL_MAX_CHARS` characters — anything else is refused
    before disk and the offending text is never echoed (label text is user
    data, same class as an address). The ONE canonicalization: a word that
    matches a closed-set tag id case-insensitively stores as the tag id
    ("KYC" → ``kyc``), because the tag ids are the ENGINE VOCABULARY the
    deterministic partition reads; free text stays verbatim (display-only).
    """
    if not isinstance(label, str) or not label.strip():
        raise StoreError("address labels must be non-empty text")
    if len(label) > ADDRESS_LABEL_MAX_CHARS:
        raise StoreError("address label exceeds the maximum length")
    if not label.isprintable():
        raise StoreError("address labels must be printable text")
    candidate = label.strip().lower()
    for tag in COIN_TAGS:
        if candidate == tag:
            return tag
    return label


def _migrate_label_member(label: object) -> str | None:
    """Migration-lenient member gate for v5→v6: ``None`` means "skip this
    member" (the row still migrates, contributing nothing to the union).

    v5's writers only rejected empty (``not label``) and over-cap, so
    whitespace-only and non-printable members are LEGITIMATE v5 data — they
    COALESCE to skip here instead of refusing (a hard refusal would brick the
    whole upgrade on rows v5 never forbade). Over-cap stays a refusal: no v5
    writer ever produced it, so it is genuine corruption, and it is the one
    failure this helper shares with :func:`_normalize_label_member`.
    """
    if not isinstance(label, str) or not label.strip():
        return None
    if len(label) > ADDRESS_LABEL_MAX_CHARS:
        raise StoreError("address label exceeds the maximum length")
    if not label.isprintable():
        return None
    candidate = label.strip().lower()
    for tag in COIN_TAGS:
        if candidate == tag:
            return tag
    return label


def _canonical_label_members(members: Iterable[str]) -> tuple[str, ...]:
    """Deterministic canonical order of one label set: closed-set tags first
    (COIN_TAGS order), free-text members sorted. Dedupes exact strings."""
    seen = set(members)
    tags = tuple(tag for tag in COIN_TAGS if tag in seen)
    free = tuple(sorted(m for m in seen if m not in COIN_TAGS))
    return tags + free


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

# Schema v3 (TCK-RBF-001): the four lineage/capture columns are written with
# COALESCE-preserve semantics — a NON-NULL incoming value replaces, a NULL
# never clobbers what broadcast capture already recorded. That is what lets
# the scan (which knows chain truth — height/direction — but never our
# amount/fee/first-seen/lineage) upsert through this ONE shared statement:
# scan rows carry NULLs there and the captured facts survive every rescan.
_TX_UPSERT_SQL = (
    "INSERT INTO transactions "
    "(wallet_id, txid, height, block_time, fee_sats, direction, raw_summary, "
    "amount_sats, fee_rate_centisat_vb, first_seen, replaced_by_txid) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(wallet_id, txid) DO UPDATE SET "
    "height = excluded.height, "
    "block_time = excluded.block_time, "
    "fee_sats = excluded.fee_sats, "
    "direction = excluded.direction, "
    "raw_summary = excluded.raw_summary, "
    "amount_sats = COALESCE(excluded.amount_sats, transactions.amount_sats), "
    "fee_rate_centisat_vb = COALESCE(excluded.fee_rate_centisat_vb, "
    "transactions.fee_rate_centisat_vb), "
    "first_seen = COALESCE(excluded.first_seen, transactions.first_seen), "
    "replaced_by_txid = COALESCE(excluded.replaced_by_txid, "
    "transactions.replaced_by_txid)"
)

_SYNC_STATE_UPSERT_SQL = (
    "INSERT INTO sync_state (wallet_id, key, value) VALUES (?, ?, ?) "
    "ON CONFLICT(wallet_id, key) DO UPDATE SET value = excluded.value"
)

# coin_labels (schema v2, TCK-UTXO-001 — LEGACY since v6): the outpoint-
# keyed per-coin tags + note. The v5→v6 rung reads it once, unions every
# resolvable row into ``address_label_set``, and RETAINS the table (write-
# frozen) as preserved history for labels v5 attached to coins this store can
# no longer attribute an address to (spent coins — ``utxos`` is the only
# outpoint→address record, and it is the unspent snapshot). From v6 on no
# accessor writes or reads it; it survives only on upgraded DBs (a fresh v6
# database never creates it). The DDL remains for the v1→v2 ladder rung.
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

# address_registry (schema v4, TCK-CHAT-001) — the stable wallet-lifetime
# NUMBER each shown address carries. Keyed by (wallet_id, address); the
# number is assigned ONCE at the first showing (never re-derived, never
# per-list positional — renumbering silently retargets spends) and is
# UNIQUE per wallet, so the same address can never hold two numbers and
# two addresses can never share one. ``first_shown`` (unix seconds) is
# written with the number and never updated again (the date is store
# truth for future allocated-but-never-used decisions, not narration).
_ADDRESS_REGISTRY_DDL = """
            CREATE TABLE IF NOT EXISTS address_registry (
                wallet_id   INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
                address     TEXT NOT NULL,
                number      INTEGER NOT NULL,
                first_shown INTEGER NOT NULL,
                PRIMARY KEY (wallet_id, address),
                UNIQUE (wallet_id, number)
            );
"""

# address_labels (schema v5, TCK-LABEL-001 — FOLDED by v6): the single
# free-text label keyed by ADDRESS. Every row maps cleanly into the v6 set
# (the key already IS the address), so the v5→v6 rung copies and DROPS the
# table — no data can be lost in a total fold, and leaving a second address-
# label surface alive is precisely the split the unification ends. The DDL
# remains for the v4→v5 ladder rung.
_ADDRESS_LABELS_DDL = """
            CREATE TABLE IF NOT EXISTS address_labels (
                address    TEXT PRIMARY KEY,
                label      TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
"""

# address_label_set (schema v6, TCK-LABELS-UNIFY) — THE labeling source of
# truth: one row per (address, label) MEMBER. ``label`` is a closed-set tag
# id (engine vocabulary — canonicalized at write, §1.4) or verbatim free text
# (display-only); the address's coins INHERIT the whole set for the
# deterministic partition (the caller joins it onto the snapshot; ``tx/``
# reads no store). ``created_at`` (ISO-8601 UTC) is written once per member
# and never moves (the registry's first_shown / v5's created_at discipline).
# Keyed on the address string with the same global-uniqueness assumption as
# ``addresses.address`` / v5 ``address_labels``; FK-less on purpose (an
# address keeps its labels across wallets and scans — a label is a USER fact
# about a script, not derivation state). Lives OUTSIDE the scan write-set: a
# rescan can never clear, re-assign, or fabricate a label.
_ADDRESS_LABEL_SET_DDL = (
    """
            CREATE TABLE IF NOT EXISTS address_label_set (
                address    TEXT NOT NULL,
                label      TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (address, label)
            )
"""
)

_ADDRESS_LABEL_SET_INSERT_SQL = (
    "INSERT OR IGNORE INTO address_label_set (address, label, created_at) "
    "VALUES (?, ?, ?)"
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

    @property
    def schema_version(self) -> int:
        """The live ``PRAGMA user_version`` stamp of THIS database (TCK-VER-001
        version report). A typed read like every other accessor — no raw SQL
        escapes store/."""
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

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
        rewrites data, so per-statement atomicity is sufficient — with ONE
        named exception: the v5→v6 fold MOVES data, so that rung runs as its
        OWN explicit transaction (all-or-nothing; see
        :meth:`_migrate_v5_to_v6`) and re-runs cleanly from an untouched v5
        after any crash.
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
                try:
                    step(self, conn)
                except _MigrateError:
                    raise
                except sqlite3.Error as exc:
                    # Fail closed on a malformed on-disk shape (e.g. a table
                    # the step cannot ALTER): the transaction rolls back, the
                    # DB stays at its old version untouched, and the refusal
                    # carries only the version number — the driver message
                    # rides on as __cause__ only (never user values in it).
                    raise _MigrateError(
                        f"database schema migration from version {version} "
                        "failed; refusing to open"
                    ) from exc
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

    #: The four v3 ``transactions`` columns, DDL fragments in add order
    #: (TCK-RBF-001). All nullable — a legacy row keeps NULLs and reads as
    #: "not recorded"; nothing is fabricated from them, ever.
    _V3_TX_COLUMNS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("amount_sats", "INTEGER"),
        ("fee_rate_centisat_vb", "INTEGER"),
        ("first_seen", "INTEGER"),
        ("replaced_by_txid", "TEXT"),
    )

    def _migrate_v2_to_v3(self, conn: sqlite3.Connection) -> None:
        """v2→v3 (TCK-RBF-001): add the lineage/capture columns to
        ``transactions`` (amount_sats, fee_rate_centisat_vb, first_seen,
        replaced_by_txid) — the broadcast-time record the RBF/CPFP
        disambiguation lists and the BIP-125 delta read, and the one write
        that lets the superseded-retirement rule leave the pending counts.

        Purely additive and IDEMPOTENT (the migration contract): SQLite has
        no ``ADD COLUMN IF NOT EXISTS``, so each column is added only when
        ``PRAGMA table_info`` says it is missing — a crash between a step and
        the version stamp re-runs cleanly. Fail-closed on a malformed table:
        a stamped-v2 DB whose ``transactions`` table is absent cannot be
        brought up safely, so the step refuses instead of silently rebuilding
        history. ``coin_labels`` (the v2 user surface) is NOT touched here —
        pinned by tests.
        """
        live = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)")}
        if not live:
            raise _MigrateError(
                "schema migration v2->v3 refused: transactions table is missing"
            )
        for name, decl in self._V3_TX_COLUMNS:
            if name not in live:
                conn.execute(f"ALTER TABLE transactions ADD COLUMN {name} {decl}")

    def _migrate_v3_to_v4(self, conn: sqlite3.Connection) -> None:
        """v3→v4 (TCK-CHAT-001): add the ``address_registry`` table.

        Purely additive and IDEMPOTENT (the migration contract — the same
        ``CREATE TABLE IF NOT EXISTS`` pattern as v1→v2's ``coin_labels``):
        a crash between this step and the version stamp re-runs cleanly.
        Nothing is back-filled — legacy wallets simply have NO registry
        rows, and numbers are assigned at the next showing (an address
        that was printed before this build shipped gets its number the
        next time a surface prints it; the registry never fabricates a
        first-shown date it did not witness).
        """
        conn.executescript(_ADDRESS_REGISTRY_DDL)

    def _migrate_v4_to_v5(self, conn: sqlite3.Connection) -> None:
        """v4→v5 (TCK-LABEL-001): add the ``address_labels`` table.

        Purely additive and IDEMPOTENT (the migration contract — the same
        ``CREATE TABLE IF NOT EXISTS`` pattern as v1→v2's ``coin_labels`` and
        v3→v4's ``address_registry``): a crash between this step and the
        version stamp re-runs cleanly. Nothing is back-filled — legacy
        wallets simply have NO address labels, and a label exists only once
        the user has actually written one through the typed accessor (the
        store never fabricates label text, and the narrating app never
        claims a label that has no row).
        """
        conn.executescript(_ADDRESS_LABELS_DDL)

    def _migrate_v5_to_v6(self, conn: sqlite3.Connection) -> None:
        """v5→v6 (TCK-LABELS-UNIFY): the ADDRESS label SET becomes the
        labeling source of truth (USER MODEL ratified 2026-09-13: one address
        = one private key = one provenance; coins inherit the address's set;
        a per-UTXO label is an addition to it).

        This is the ONE rung in the ladder that MOVES data, so it honors the
        migration contract through ATOMICITY rather than append-only DDL: the
        whole rung runs inside a single explicit ``BEGIN IMMEDIATE``
        transaction (plain ``execute`` statements — never
        ``executescript``, which commits beforehand), and the rung's own
        version stamp (``PRAGMA user_version=6``) is written INSIDE that
        transaction, LAST. The ladder's post-rung stamp then no-ops. Because
        the fold AND its stamp are one atomic COMMIT, the FIRST non-idempotent
        rung re-runs cleanly after any crash by construction: a crash ANYWHERE
        mid-rung rolls every statement back — the DB stays a pristine v5 and
        the next open re-runs the whole rung — and once the fold COMMIT lands
        the v6 stamp is already durable (there is no post-COMMIT/pre-stamp
        window left to wedge a healthy wallet). For a v5 file already in that
        legacy half-applied shape (a fully-migrated DB a pre-fix build left
        stamped v5 — ``address_labels`` dropped, ``address_label_set``
        present), the rung detects fold-already-applied and COMPLETES by
        stamping v6 instead of refusing. No partial fold ever lands.

        Content: (1) every ``address_labels`` (v5) row folds into the set —
        a total map (its key already IS the address), after which the fully-
        represented table is dropped; (2) every ``coin_labels`` (v2) row's
        tags AND note union into the label set of the address that holds the
        coin (joined through ``utxos`` — the store's only outpoint→address
        record); rows whose coin has been SPENT resolve to no address and
        contribute nothing. The ``coin_labels`` table itself is RETAINED
        write-frozen WHOLE — resolvable and spent rows alike stay (dropping
        user data silently is forbidden); the union merely copies what
        resolves. (3) Lenient on what v5 legitimately
        allowed: blank/whitespace-only and non-printable label/note members
        (v5 writers rejected only empty and over-cap) COALESCE to "skip this
        member" — the row still migrates, contributing nothing to the union.
        (4) Fail-closed on genuine corruption: an unknown tag id, an over-cap
        member, an un-keyable address, or a missing legacy table refuses the
        ENTIRE upgrade value-free; the DB stays v5 untouched — never a silent
        drop, never a partial fold.
        """
        live = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "coin_labels" not in live:
            # A stamped-v5 DB without the coin-label table is not this
            # schema — refuse (fail closed; a v5 file always has it).
            raise _MigrateError(
                "schema migration v5->v6 refused: a legacy label table is missing"
            )
        if "address_labels" not in live:
            # Either the fold already applied but a PRE-FIX crash dropped the
            # stamp (address_label_set present, address_labels gone — a
            # fully-migrated, healthy wallet), or a genuinely malformed v5.
            # Distinguish by whether the v6 set table is present: fold-already-
            # applied COMPLETES by stamping v6; anything else is not this
            # schema and refuses.
            if "address_label_set" in live:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("PRAGMA user_version=6")
                    conn.execute("COMMIT")
                except sqlite3.Error:
                    self._rollback_quietly()
                    raise  # the ladder's own value-free migration wrapper owns it
                return
            raise _MigrateError(
                "schema migration v5->v6 refused: a legacy label table is missing"
            )
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(_ADDRESS_LABEL_SET_DDL)
            for row in conn.execute("SELECT address, label FROM address_labels"):
                member = _migrate_label_member(row["label"])
                _check_address_key_shape(row["address"], "address label set")
                if member is None:
                    continue  # whitespace-only / non-printable v5 label: skipped
                conn.execute(
                    _ADDRESS_LABEL_SET_INSERT_SQL, (row["address"], member, _utcnow())
                )
            conn.execute("DROP TABLE address_labels")
            lookup = conn.cursor()
            for tags, note, wallet_id, txid, vout in conn.execute(
                "SELECT tags, note, wallet_id, txid, vout FROM coin_labels "
                "ORDER BY txid, vout"
            ):
                # Validate EVERY row (malformed spent-coin rows refuse too)
                # before deciding whether this one can be relocated.
                # FINDING 4: the non-str guard runs BEFORE the split — a
                # BLOB-stored tags cell must refuse as a value-free
                # _MigrateError (rollback + pristine v5), never escape as a
                # bare TypeError from ``.split``.
                if not isinstance(tags, str):
                    raise _MigrateError(
                        "schema migration v5->v6 refused: a legacy coin label "
                        "carries an unknown tag"
                    )
                parts = [p for p in tags.split(",") if p]
                if any(p not in COIN_TAGS for p in parts):
                    raise _MigrateError(
                        "schema migration v5->v6 refused: a legacy coin label "
                        "carries an unknown tag"
                    )
                members = list(parts)
                if note is not None:
                    member = _migrate_label_member(note)
                    if member is not None:
                        members.append(member)
                if not members:
                    continue  # a tagless, note-less row (v5 never wrote one):
                    # nothing to relocate; the row stays with the frozen table.
                addr_row = lookup.execute(
                    "SELECT address FROM utxos "
                    "WHERE wallet_id = ? AND LOWER(txid) = ? AND vout = ?",
                    (wallet_id, str(txid).lower(), vout),
                ).fetchone()
                address = addr_row[0] if addr_row is not None else None
                if not address:
                    continue  # coin spent pre-upgrade: RETAINED in coin_labels
                for member in members:
                    conn.execute(
                        _ADDRESS_LABEL_SET_INSERT_SQL, (address, member, _utcnow())
                    )
            # FINDING 1: stamp v6 INSIDE the rung's own transaction, atomically
            # with the fold — the ladder's post-rung stamp then no-ops. A crash
            # after this COMMIT cannot leave a folded-but-stamped-v5 DB.
            conn.execute("PRAGMA user_version=6")
            conn.execute("COMMIT")
        except _MigrateError:
            self._rollback_quietly()
            raise
        except StoreError as exc:
            # The only members that still REFUSE are the ones no v5 writer
            # could ever have produced (over-cap; the address key gate);
            # seeing them means the on-disk rows were edited or corrupted.
            # (Whitespace-only / non-printable members were legitimately
            # writable in v5 and are COALESCED, never raised.) Fail closed,
            # value-free: nothing echoes, nothing drops.
            self._rollback_quietly()
            raise _MigrateError(
                "schema migration v5->v6 refused: a legacy label row is malformed"
            ) from exc
        except sqlite3.Error:
            self._rollback_quietly()
            raise  # the ladder's own value-free migration wrapper owns it

    #: Up-migration ladder keyed by the version it migrates FROM. Extend (never
    #: reorder or delete) as schema version bumps; a missing rung fails closed.
    _MIGRATIONS: ClassVar[dict[int, Callable[[sqlite3.Connection], None]]] = {
        1: _migrate_v1_to_v2,
        2: _migrate_v2_to_v3,
        3: _migrate_v3_to_v4,
        4: _migrate_v4_to_v5,
        5: _migrate_v5_to_v6,
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
                amount_sats  INTEGER,
                fee_rate_centisat_vb INTEGER,
                first_seen   INTEGER,
                replaced_by_txid TEXT,
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
        # v6 shape, stated honestly: a FRESH database carries the address
        # label set only — the legacy ``address_labels`` (v5) is gone entirely
        # (folded), and ``coin_labels`` (v2) is simply never created (its
        # writer is dead; upgraded databases keep the table with its
        # historical rows, fresh ones have no history to preserve). The
        # ladder rungs that create them exist only to bring versioned files
        # UP through v5, where the v5→v6 fold then relocates the content.
        conn.execute(_ADDRESS_LABEL_SET_DDL)
        conn.executescript(_ADDRESS_REGISTRY_DDL)

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

    # ---------------------------------------------------- address registry
    #
    # TCK-CHAT-001 (schema v4): the referential-address registry — a STABLE
    # wallet-lifetime number + first-shown timestamp for every address the
    # app SHOWS the user. The typed pair below is the only sanctioned writer
    # (the chain_base_url / coin_labels precedent), so number assignment can
    # never be skipped or forged: numbers come from MAX+1 over this table
    # alone, ``first_shown`` is written exactly once and never updated, and
    # an address that already has a number keeps it forever (idempotent
    # re-showing returns the SAME record). Nothing here fabricates: an
    # address never shown has no row, a lookup miss is ``None`` (the app's
    # bound-check turns a miss into the value-free clarify), and errors
    # carry only table/operation context — never the address.

    def note_address_shown(
        self, wallet_id: int, address: str, *, shown_at: int | None = None
    ) -> AddressRegistryRecord:
        """Register ``address`` as SHOWN to the user; return its registry row.

        Idempotent and stable by construction: the FIRST call assigns the
        next wallet-lifetime number (MAX+1, never reused) and stamps
        ``shown_at`` (unix seconds; ``None`` reads the UTC clock — tests
        inject one); EVERY later call returns the existing row untouched —
        same address, same number, forever, and the first-shown date never
        moves. The one-transaction read-then-assign makes a double-show
        under a crash window impossible in this single-writer app (and the
        UNIQUE(wallet_id, number) constraint fails closed, never silently
        renumbers, if that assumption is ever violated).

        Raises value-free :class:`StoreError` for a malformed address (a
        registry key must be a non-empty printable string without
        whitespace — anything else is a caller bug, refused before disk)
        and :class:`StoreIntegrityError` for a FK violation (no such wallet).
        """
        _check_address_key_shape(address, "address registry")
        clock = int(datetime.now(UTC).timestamp()) if shown_at is None else shown_at
        if not isinstance(clock, int) or isinstance(clock, bool) or clock < 0:
            raise StoreError("address registry first-shown time must be a non-negative int")
        try:
            with self._atomic():
                row = self._conn.execute(
                    "SELECT * FROM address_registry WHERE wallet_id = ? AND address = ?",
                    (wallet_id, address),
                ).fetchone()
                if row is not None:
                    return AddressRegistryRecord.from_row(row)
                number = int(
                    self._conn.execute(
                        "SELECT COALESCE(MAX(number), 0) + 1 FROM address_registry "
                        "WHERE wallet_id = ?",
                        (wallet_id,),
                    ).fetchone()[0]
                )
                record = AddressRegistryRecord(wallet_id, address, number, clock)
                self._conn.execute(
                    "INSERT INTO address_registry "
                    "(wallet_id, address, number, first_shown) VALUES (?, ?, ?, ?)",
                    record.to_row(),
                )
        except sqlite3.IntegrityError as exc:
            raise _wrap_integrity(exc) from exc
        except sqlite3.Error as exc:
            raise _wrap(exc) from exc
        return record

    def get_address_by_number(
        self, wallet_id: int, number: int
    ) -> AddressRegistryRecord | None:
        """The registry row bearing ``number``, or ``None``.

        ``None`` means NO address has ever been shown under that number —
        the caller (the app's referent resolver) treats it as the
        out-of-range clarify; this layer never guesses and never invents a
        nearest match. A non-int (or bool) number is a caller bug: refused
        fail-closed, value-free.
        """
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise StoreError("address registry number must be a positive integer")
        row = self._conn.execute(
            "SELECT * FROM address_registry WHERE wallet_id = ? AND number = ?",
            (wallet_id, number),
        ).fetchone()
        return AddressRegistryRecord.from_row(row) if row is not None else None

    def list_address_registry(
        self, wallet_id: int
    ) -> list[AddressRegistryRecord]:
        """Every registry row for a wallet, ordered by the STABLE number."""
        rows = self._conn.execute(
            "SELECT * FROM address_registry WHERE wallet_id = ? ORDER BY number",
            (wallet_id,),
        ).fetchall()
        return [AddressRegistryRecord.from_row(r) for r in rows]

    def registry_number_for(
        self, wallet_id: int, address: str
    ) -> AddressRegistryRecord | None:
        """The registry row for ``address`` IF it has ever been shown.

        A pure READ — it never assigns a number (assignment belongs to
        :meth:`note_address_shown`, the first-showing write). ``None`` =
        never shown, so a narration surface annotating an address it did
        not show simply omits the number rather than back-dating one.
        """
        row = self._conn.execute(
            "SELECT * FROM address_registry WHERE wallet_id = ? AND address = ?",
            (wallet_id, address),
        ).fetchone()
        return AddressRegistryRecord.from_row(row) if row is not None else None

    # --------------------------------------------------------- address label sets
    #
    # TCK-LABELS-UNIFY (schema v6): the ADDRESS-keyed label SET — the labeling
    # source of truth (one address = one private key = one provenance; coins
    # INHERIT the set for the selection engine; a per-UTXO label is just
    # another union-addition to it; engine-internal change addresses stay
    # unlabeled until a write or an inheritance lands a member). Members are
    # closed-set tag ids (ENGINE vocabulary; "KYC" canonicalizes to ``kyc``
    # at write) and verbatim free text (display-only unless it matches a tag).
    # The typed accessors below are the ONLY sanctioned writers (the
    # chain_base_url / gap_limit precedent): fail-closed validation at write,
    # value-free errors (label text is user data — never echoed into
    # exceptions/logs), and reads that return COMMITTED truth in a
    # deterministic canonical order. Labels NEVER enter model context
    # (§1.1/§7.10). TCK-CHAT-003 (address-level label ask) and TCK-CHAT-007
    # (receive + label) build their capture flows on this trio.

    def add_address_labels(
        self, address: str, labels: Iterable[str]
    ) -> tuple[str, ...]:
        """Union-ADD members to one address's label set; return the COMMITTED set.

        The returned tuple is the whole set as stored, read back after the
        commit — a success line may only ever quote what this returned, never
        what was typed (the LABEL-001 store-truth discipline, generalized to
        set membership). Union is IDEMPOTENT: re-adding a member the address
        already carries changes nothing (INSERT OR IGNORE; the first member's
        ``created_at`` survives — write-once, the registry's ``first_shown``
        rule). An empty ``labels`` addition is a no-op that still answers with
        the current committed set (never an error, never a clear).

        Raises value-free :class:`StoreError` for a malformed key or a member
        that is empty/blank, over :data:`ADDRESS_LABEL_MAX_CHARS`, or not
        printable — the WHOLE add is one transaction, so a refusal stores
        nothing, not even the call's valid members.
        """
        _check_address_key_shape(address, "address label set")
        members = [_normalize_label_member(label) for label in labels]
        if not members:
            return self.get_address_label_set(address)
        created = _utcnow()
        try:
            with self._atomic():
                self._conn.executemany(
                    _ADDRESS_LABEL_SET_INSERT_SQL,
                    [(address, member, created) for member in members],
                )
            committed = self.get_address_label_set(address)
        except sqlite3.IntegrityError as exc:
            raise _wrap_integrity(exc) from exc
        except sqlite3.Error as exc:
            raise _wrap(exc) from exc
        if not committed:  # pragma: no cover — a committed write reads back
            raise StoreError("address label write did not land")
        return committed

    def get_address_label_set(self, address: str) -> tuple[str, ...]:
        """The committed label set of one address, canonically ordered
        (tags in COIN_TAGS order, then free text sorted); ``()`` = unlabeled.

        A pure READ: a miss is ``()``, never a nearest match and never a
        fabricated label.
        """
        rows = self._conn.execute(
            "SELECT label FROM address_label_set WHERE address = ?", (address,)
        ).fetchall()
        return _canonical_label_members(row["label"] for row in rows)

    def get_address_label_sets(self) -> dict[str, tuple[str, ...]]:
        """Every labeled address → its canonically ordered set (address order).

        The one read the selection join and the listing surfaces ride: an
        address with no members is ABSENT (unlabeled = no rows, never an
        empty-string row — the v2 lesson), and rows keyed by address survive
        rescans unchanged (§1.3's crux, kept by v6). Small table, global read
        — addresses are globally unique keys (``addresses.address``
        discipline), so a wallet-scoped variant would answer the same
        question for zero extra honesty.
        """
        rows = self._conn.execute(
            "SELECT address, label FROM address_label_set ORDER BY address, label"
        ).fetchall()
        grouped: dict[str, list[str]] = {}
        for row in rows:
            grouped.setdefault(row["address"], []).append(row["label"])
        return {address: _canonical_label_members(m) for address, m in grouped.items()}

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

    # ------------------------------------------------ coin labels (LEGACY v2)
    #
    # TCK-UTXO-001's outpoint-keyed accessors (set/get/list/clear plus
    # propagate_coin_lineage) are DELETED, not deprecated: under the ratified
    # labeling model (TCK-LABELS-UNIFY, schema v6) the ADDRESS label SET is
    # the single source of truth, and a coin-level writer that selection
    # ignores would be a lie-machine. The migrated-but-retained coin_labels
    # rows (unresolvable spent-coin history) have no reader in code — by
    # design, stated where the table itself is documented above.

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

    def record_replacement(
        self, wallet_id: int, original_txid: str, replacement_txid: str
    ) -> None:
        """Write the RBF lineage link (schema v3): ``original_txid`` was
        replaced by ``replacement_txid`` (TCK-RBF-001; the bump conversation,
        TCK-RBF-004, is the caller). The ONLY sanctioned writer of
        ``replaced_by_txid`` — the scan upserts carry NULL there and the
        shared statement's COALESCE can never clobber or clear a link — so
        the superseded-retirement rule has exactly one source for lineage.

        The original must already have a recorded row (you can only link a
        transaction this wallet actually broadcast). Validation is fail-closed
        and value-free: malformed txids, a self-link, or a missing original
        raise before anything touches disk, and no message echoes a txid.
        """
        _check_txid_shape(original_txid, "lineage original")
        _check_txid_shape(replacement_txid, "lineage replacement")
        if original_txid == replacement_txid:
            raise StoreError("a transaction cannot replace itself")
        try:
            with self._transaction():
                cur = self._conn.execute(
                    "UPDATE transactions SET replaced_by_txid = ? "
                    "WHERE wallet_id = ? AND txid = ?",
                    (replacement_txid, wallet_id, original_txid),
                )
                if cur.rowcount == 0:
                    raise StoreError(
                        "lineage refused: the original transaction has no recorded row"
                    )
        except sqlite3.IntegrityError as exc:
            raise _wrap_integrity(exc) from exc
        except sqlite3.Error as exc:
            raise _wrap(exc) from exc

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

        Label surfaces are DELIBERATELY absent from this write-set: the UTXO
        snapshot replace touches only the ``utxos`` table, so the (schema v6)
        ``address_label_set`` — the labeling source of truth, like the
        (schema v4) registry rows — survives every rescan unchanged. A label
        is a USER fact, written only through :meth:`add_address_labels`
        (and the broadcast-time inheritance that runs through it); no scan
        can clear, re-assign, or fabricate one. The write-frozen legacy
        ``coin_labels`` history rides outside the write-set for the same
        reason.

        Superseded retirement (schema v3, TCK-RBF-001) lands through this
        SAME transaction: the scan's tx upsert is the one place that writes
        confirmed heights, and the retirement rule
        (:func:`localwallet.store.models.superseded_states`) is a pure
        function of the (height, ``replaced_by_txid``) data this transaction
        commits — exactly one side of a lineage pair ever gains a height
        (BIP-125), and the sibling is then terminal and excluded from
        pending counts. No extra statement is needed (and none is added):
        state derived from the committed heights can never desync from them,
        and the shared upsert's COALESCE-preserve keeps the broadcast-time
        capture (amount/fee-rate/first-seen/lineage) intact across every
        rescan. Engine-thread-safe by construction — one atomic transaction,
        never a forked write.
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

    def clear_setting(self, key: str) -> None:
        """DELETE one settings row outright (TCK-CFG-004 conflict rule): a
        chat change writes the config-FILE rung and clears this key's STORED
        rung so exactly one non-env surface stays authoritative. ``set_
        setting(key, "")`` is NOT this: it leaves an empty row that every
        ladder reads as unset but the settings surface would still display;
        deletion leaves no row at all. The generic delete behind the two
        keys that have no typed accessor of their own (``gap_limit`` and
        ``watch_interval_s``); the coin keys clear through the typed
        ``set_coin_setting(key, "")`` instead. Any OTHER key is refused with
        a value-free :class:`StoreError` so a settings value can never be
        dropped through this generic path.
        """
        if key not in ("gap_limit", "watch_interval_s"):
            raise StoreError("cannot clear a non-managed settings key")
        with self._transaction():
            self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))

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
        base URL, an ``ssl://host[:port]`` Electrum endpoint (TCK-BACKEND-002),
        or a ``bitcoind://host[:port]`` Bitcoin Core RPC endpoint
        (TCK-ONB-004 M3; ADR-0018 M3 amendment — the M2 adapter shipped, the
        stored rung now carries it), each with a host and NO embedded
        credentials — mirroring the ``ChainConfig`` construction check that
        stays as the last line of defense, with ONE documented split:
        ``ChainConfig`` permits ``bitcoind://user:pass@host`` on the
        env/config-file rungs, the STORED rung never does (M3 carries
        dedicated, never-echoed credential keys instead — see
        :meth:`set_backend_auth_user`), so ``@`` is refused for every scheme
        written here. A whitespace-only write is refused (deliberate-but-blank
        is malformed, never a silent clear); only the exact empty string clears
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
        if candidate.startswith(("bitcoind://", "bitcoind+tls://")):
            # The Core-RPC family (plain http + the https TLS sibling,
            # TCK-BACKEND-003): same stored-rung rules for both.
            self._check_bitcoind_base_url(candidate)
            self.set_setting(_CHAIN_BASE_URL_SETTING, candidate)
            return
        if not candidate.startswith(("http://", "https://")):
            raise StoreError(
                "chain base url must be an http(s), ssl:// or bitcoind:// URL"
            )
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

    @staticmethod
    def _check_bitcoind_base_url(candidate: str) -> None:
        """Shape rules for a stored ``bitcoind://host[:port]`` Core RPC
        endpoint (mirrors :meth:`ChainConfig._validate_bitcoind_url` MINUS
        the userinfo allowance the env/config-file rungs keep — TCK-ONB-004
        M3): a host, an optional NUMERIC in-range port, no path/query/
        fragment (the RPC surface is a single POST root), and NO embedded
        credentials (``@`` refused; the dedicated never-echoed
        ``backend_auth_*`` keys carry logins instead)."""
        rest = candidate.partition("://")[2]  # both bitcoind:// and bitcoind+tls://
        if any(c.isspace() for c in rest):
            raise StoreError("chain base url must not contain whitespace")
        if any(c in rest for c in "/?#"):
            raise StoreError("bitcoind chain base url must not carry a path")
        if "@" in rest:  # embedded credentials — never storable (M3 keys)
            raise StoreError("chain base url must not embed credentials")
        host, sep, port = rest.rpartition(":")
        if sep:
            if not host or not port.isdigit() or not 0 < int(port) < 65536:
                raise StoreError("bitcoind chain base url has an invalid port")
        elif not rest:
            raise StoreError("chain base url must have a host")

    # ---------------------------------- backend credentials (ONB-004 M3)
    #
    # The typed pair for each of the three credential keys above. Same
    # key-value mechanism and ``""``-clears convention as chain_base_url;
    # the values are secrets — the READ methods exist for the app's engine-
    # thread credential resolver ONLY, and every settings SURFACE that can
    # answer about these keys reports SET-vs-UNSET, never the value (see
    # app._settings_entries). Validation is fail-closed at write, value-free
    # at every step.

    def get_backend_auth_user(self) -> str | None:
        """Stored RPC login user, or ``None`` (unset rung)."""
        return self.get_setting(_BACKEND_AUTH_USER_SETTING)

    def set_backend_auth_user(self, user: str) -> None:
        """Persist the RPC login user; ``""`` clears it.

        Shape rules (value-free, shared with the password): at most
        :data:`_MAX_BACKEND_AUTH_CHARS` characters, ASCII, no whitespace and
        no control characters (the value is joined into an ``Authorization``
        header; CR/LF there is header injection, refused at the door). A
        user-without-password stays STORED as such — the app's resolver uses
        basic auth only when BOTH parts exist, else the documented cookie/
        no-auth ladder (plan §3 interplay).
        """
        self._check_backend_auth_value("backend auth user", user)
        self._write_backend_auth(_BACKEND_AUTH_USER_SETTING, user)

    def get_backend_auth_pass(self) -> str | None:
        """Stored RPC login password, or ``None`` (unset rung). NEVER
        returned by any settings/log surface — engine-thread resolver only."""
        return self.get_setting(_BACKEND_AUTH_PASS_SETTING)

    def set_backend_auth_pass(self, password: str) -> None:
        """Persist the RPC login password; ``""`` clears it. Same shape
        rules (and the same never-echoed contract) as
        :meth:`set_backend_auth_user`."""
        self._check_backend_auth_value("backend auth password", password)
        self._write_backend_auth(_BACKEND_AUTH_PASS_SETTING, password)

    def get_backend_auth_none(self) -> bool:
        """Whether the explicit "no credentials needed" answer is stored."""
        return self.get_setting(_BACKEND_AUTH_NONE_SETTING) == "1"

    def set_backend_auth_none(self, on: bool) -> None:
        """Persist the explicit no-credentials choice (the checkbox: omit
        the ``Authorization`` header ENTIRELY, cookie file included);
        ``False`` clears the record (back to the default ladder)."""
        if not isinstance(on, bool):
            raise StoreError("backend auth none flag must be a boolean")
        self._write_backend_auth(_BACKEND_AUTH_NONE_SETTING, "1" if on else "")

    @staticmethod
    def _check_backend_auth_value(name: str, value: str) -> None:
        """Fail-closed, value-free shape gate for a credential string
        (empty = the clear write, checked by the caller's convention)."""
        if not isinstance(value, str):
            raise StoreError(f"{name} must be a string")
        if not value:
            return
        if len(value) > _MAX_BACKEND_AUTH_CHARS:
            raise StoreError(f"{name} is too long")
        if not value.isascii() or any(
            c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value
        ):
            raise StoreError(
                f"{name} must be ASCII without whitespace or control characters"
            )

    def _write_backend_auth(self, key: str, value: str) -> None:
        """Typed write for the credential pair: ``""`` deletes the row, any
        other (already validated) value replaces it."""
        candidate = value  # validated verbatim — NO strip: a credential's
        # exact bytes are the point (surrounding space is refused above, so
        # stripping could only mask a typo the user can see in their own file)
        if not candidate:
            with self._transaction():
                self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            return
        self.set_setting(key, candidate)

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

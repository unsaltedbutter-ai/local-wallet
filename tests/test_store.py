"""Tests for the SQLite store layer (TCK-P1-001).

Covers: init/reopen + versioned migration (fail-closed on newer schema), WAL
and foreign-key enforcement, wallet CRUD + active-wallet selection, derivation
state, address upserts/status transitions, UTXO snapshot replace semantics,
transaction upserts, sync_state/settings round-trips, value-free error
messages, and a concurrent-connections smoke test on a file DB.
"""

import sqlite3

import pytest

from localwallet.store import (
    SCHEMA_VERSION,
    AddressRecord,
    DerivationRecord,
    Store,
    StoreError,
    StoreIntegrityError,
    TxRecord,
    UtxoRecord,
    WalletRecord,
)

BUSY_TIMEOUT_MS = 5000

DESCRIPTOR = "wpkh([abcd1234/84'/1'/0']vpub/0/*)"
ADDR = "tb1qexampleaddressvaluethatmustnotleakinerrors0"


# ------------------------------------------------------------- init/reopen


def test_init_reopen_and_user_version_persists(tmp_path):
    db = tmp_path / "store.db"
    with Store(db) as store:
        store.create_wallet("main", DESCRIPTOR)
        assert store.wal_mode == "wal"

    # Reopening the same path sees the data and a persisted schema version.
    with Store(db) as store2:
        assert store2.get_wallet_by_name("main") is not None
        assert store2.wal_mode == "wal"
        version = store2._conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION


def test_memory_store(tmp_path):
    with Store.memory() as store:
        store.create_wallet("main", DESCRIPTOR)
        assert store.get_wallet_by_name("main") is not None
        assert store.wal_mode is None  # no WAL journal mode on in-memory


def test_refuses_newer_schema(tmp_path):
    db = tmp_path / "store.db"
    with Store(db):
        pass
    raw = sqlite3.connect(db)
    raw.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    raw.commit()
    raw.close()
    with pytest.raises(StoreError):
        Store(db)


def test_foreign_keys_enforced(tmp_path):
    # set_sync_state for a non-existent wallet violates the FK.
    with Store.memory() as store, pytest.raises(StoreIntegrityError):
        store.set_sync_state(999, "last_scan_cursor", "abc")


# ----------------------------------------------------------------- wallets


def test_wallets_crud_and_active(tmp_path):
    with Store.memory() as store:
        w1 = store.create_wallet("main", DESCRIPTOR)
        assert isinstance(w1, WalletRecord)
        assert store.get_wallet_by_name("main").id == w1.id
        assert store.get_wallet(w1.id).name == "main"

        with pytest.raises(StoreIntegrityError):
            store.create_wallet("main", "wpkh([x/84'/1'/0']vpub/1/*)")

        assert store.get_active_wallet() is None
        store.set_active_wallet(w1.id)
        assert store.get_active_wallet().id == w1.id

        w2 = store.create_wallet("second", "wpkh([x/84'/1'/1']vpub/1/*)")
        store.set_active_wallet(w2.id)
        assert store.get_active_wallet().id == w2.id
        assert {w.name for w in store.list_wallets()} == {"main", "second"}

        # active wallet that no longer exists resolves to None, not a crash.
        store.set_active_wallet(9999)
        assert store.get_active_wallet() is None


# --------------------------------------------------------------- derivation


def test_derivation_default_bump_and_branch_isolation(tmp_path):
    with Store.memory() as store:
        wid = store.create_wallet("main", DESCRIPTOR).id
        d = store.get_derivation(wid, 0)
        assert isinstance(d, DerivationRecord)
        assert d.max_used_index == -1
        assert d.next_index == 0

        store.bump_next_index(wid, 0)
        store.bump_next_index(wid, 0)
        assert store.get_derivation(wid, 0).next_index == 2

        # change branch is independent
        store.bump_next_index(wid, 1)
        assert store.get_derivation(wid, 0).next_index == 2
        assert store.get_derivation(wid, 1).next_index == 1

        # targeted update
        store.update_derivation(wid, 0, max_used_index=5, next_index=6)
        r = store.get_derivation(wid, 0)
        assert (r.max_used_index, r.next_index) == (5, 6)

        # partial update leaves other field untouched
        store.update_derivation(wid, 0, max_used_index=7)
        r = store.get_derivation(wid, 0)
        assert (r.max_used_index, r.next_index) == (7, 6)


# ---------------------------------------------------------------- addresses


def _addr(wid, branch, index, address, status="unused", script_type="wpkh"):
    return AddressRecord(wid, branch, index, address, script_type, status)


def test_address_upsert_idempotent_and_status_transitions(tmp_path):
    with Store.memory() as store:
        wid = store.create_wallet("main", DESCRIPTOR).id
        store.upsert_batch(
            [
                _addr(wid, 0, 0, "tb1qaddr0"),
                _addr(wid, 0, 1, "tb1qaddr1"),
                _addr(wid, 0, 2, "tb1qaddr2"),
            ]
        )

        # idempotent re-upsert with a status change updates in place.
        store.upsert_batch([_addr(wid, 0, 1, "tb1qaddr1", status="used")])
        unused = store.get_unused(wid, 0)
        assert [a.index for a in unused] == [0, 2]

        store.mark_used(wid, 0, 0)
        assert [a.index for a in store.get_unused(wid, 0)] == [2]

        store.allocate(wid, 0, 2)
        assert store.get_by_address("tb1qaddr2").status == "allocated"
        assert store.get_unused(wid, 0) == []  # allocated is not unused

        # CHECK constraint rejects invalid status values.
        with pytest.raises(StoreIntegrityError):
            store.upsert_batch([_addr(wid, 0, 5, "tb1qaddr5", status="bogus")])


def test_address_upsert_ordering_and_lookup(tmp_path):
    with Store.memory() as store:
        wid = store.create_wallet("main", DESCRIPTOR).id
        store.upsert_batch(
            [
                _addr(wid, 0, 2, "tb1qaddr2"),
                _addr(wid, 0, 0, "tb1qaddr0"),
                _addr(wid, 0, 1, "tb1qaddr1"),
            ]
        )
        assert [a.index for a in store.get_unused(wid, 0, limit=2)] == [0, 1]
        assert store.get_by_address("tb1qaddr1").index == 1
        assert store.get_by_address("tb1qunknown") is None


# -------------------------------------------------------------------- utxos


def _utxo(wid, txid, vout=0, addr=None, value=1000, confirmed=1, height=10):
    return UtxoRecord(wid, txid, vout, addr, value, confirmed, height)


def test_utxo_replace_snapshot_and_roundtrip(tmp_path):
    with Store.memory() as store:
        wid = store.create_wallet("main", DESCRIPTOR).id
        store.replace_utxos_for_wallet(
            wid,
            [
                _utxo(wid, "aaa", 0, "tb1qaddr", 1000, 1, 10),
                _utxo(wid, "bbb", 0, "tb1qaddr2", 2000, 0, None),
            ],
        )
        got = {u.txid: u for u in store.get_utxos_for_wallet(wid)}
        assert len(got) == 2
        assert got["aaa"].confirmed == 1 and got["aaa"].height == 10
        assert got["bbb"].confirmed == 0 and got["bbb"].height is None
        assert got["bbb"].value_sats == 2000

        # Snapshot semantics: the new set fully replaces the old one.
        store.replace_utxos_for_wallet(wid, [_utxo(wid, "ccc", 1, "tb1qaddr3", 3000, 1, 20)])
        got2 = store.get_utxos_for_wallet(wid)
        assert [u.txid for u in got2] == ["ccc"]
        assert len(got2) == 1

        # Empty replace clears everything (no orphans).
        store.replace_utxos_for_wallet(wid, [])
        assert store.get_utxos_for_wallet(wid) == []


def test_utxo_unique_violation_is_clean_store_error(tmp_path):
    with Store.memory() as store:
        wid = store.create_wallet("main", DESCRIPTOR).id
        with pytest.raises(StoreIntegrityError) as exc:
            store.replace_utxos_for_wallet(
                wid,
                [_utxo(wid, "ddd", 0), _utxo(wid, "ddd", 0)],
            )
        assert exc.value.__cause__ is not None  # chaining preserved


# -------------------------------------------------------------- transactions


def test_tx_upsert_update_in_place_and_direction(tmp_path):
    with Store.memory() as store:
        wid = store.create_wallet("main", DESCRIPTOR).id
        store.upsert_txs([TxRecord(wid, "abc", None, None, None, "in", None)])
        assert store.get_txs_for_wallet(wid)[0].height is None

        # Update in place: same (wallet, txid) is patched, not duplicated.
        store.upsert_txs([TxRecord(wid, "abc", 100, 1234, 500, "in", "{}")])
        rows = store.get_txs_for_wallet(wid)
        assert len(rows) == 1
        r = rows[0]
        assert (r.height, r.fee_sats, r.raw_summary, r.direction) == (100, 500, "{}", "in")

        # direction CHECK rejects invalid values.
        with pytest.raises(StoreIntegrityError):
            store.upsert_txs([TxRecord(wid, "bad", 1, 1, 1, "sideways", None)])


# ---------------------------------------------------- sync_state + settings


def test_sync_state_and_settings_roundtrip(tmp_path):
    with Store.memory() as store:
        wid = store.create_wallet("main", DESCRIPTOR).id

        assert store.get_sync_state(wid, "last_scan_cursor") is None
        store.set_sync_state(wid, "last_scan_cursor", "abc123")
        store.set_sync_state(wid, "last_tip_height", "800000")
        assert store.get_sync_state(wid, "last_scan_cursor") == "abc123"
        store.set_sync_state(wid, "last_scan_cursor", "xyz")  # overwrite
        assert store.get_sync_state(wid, "last_scan_cursor") == "xyz"
        assert store.get_sync_state(wid, "last_tip_height") == "800000"

        assert store.get_setting("gap_limit") is None
        store.set_setting("gap_limit", "20")
        store.set_setting("gap_limit", "25")  # overwrite
        assert store.get_setting("gap_limit") == "25"


# ------------------------------------------------------- value-free errors


def test_error_messages_are_value_free(tmp_path):
    with Store.memory() as store:
        wid = store.create_wallet("main", DESCRIPTOR).id
        txid = "feedfacecafebabedeadbeefc0ffee42"
        value = 123456789
        rows = [
            _utxo(wid, txid, 0, ADDR, value, 1, 10),
            _utxo(wid, txid, 0, ADDR, value, 1, 10),  # duplicate (txid, vout)
        ]
        with pytest.raises(StoreError) as exc:
            store.replace_utxos_for_wallet(wid, rows)
        msg = str(exc.value)
        assert txid not in msg
        assert ADDR not in msg
        assert str(value) not in msg


def test_address_duplicate_value_not_in_message(tmp_path):
    with Store.memory() as store:
        wid = store.create_wallet("main", DESCRIPTOR).id
        # Same address at two different indices -> UNIQUE(address) violation.
        with pytest.raises(StoreError) as exc:
            store.upsert_batch([_addr(wid, 0, 0, ADDR), _addr(wid, 0, 1, ADDR)])
        assert ADDR not in str(exc.value)


# -------------------------------------------------- concurrent connections


def test_concurrent_connections_on_file_db(tmp_path):
    db = tmp_path / "store.db"
    with Store(db) as s1:
        s1.create_wallet("one", DESCRIPTOR)
        with Store(db) as s2:
            assert s2.get_wallet_by_name("one") is not None
            s1.create_wallet("two", DESCRIPTOR)
            assert s2.get_wallet_by_name("two") is not None
            for conn in (s1, s2):
                busy = conn._conn.execute("PRAGMA busy_timeout").fetchone()[0]
                assert busy == BUSY_TIMEOUT_MS

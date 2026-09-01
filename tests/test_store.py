"""Tests for the SQLite store layer (TCK-P1-001).

Covers: init/reopen + versioned migration (fail-closed on newer schema), WAL
and foreign-key enforcement, wallet CRUD + active-wallet selection, derivation
state, address upserts/status transitions, UTXO snapshot replace semantics,
transaction upserts, sync_state/settings round-trips, value-free error
messages, a concurrent-connections smoke test on a file DB, and the composite
atomic scan persist (``persist_scan_result``: exactly one transaction,
all-or-nothing rollback on mid-write failure, clean retry).
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


# -------------------------------------------------- composite scan persist
#
# persist_scan_result (TCK-P1-002 security review, atomic-persist finding):
# the whole scan write-set must land in ONE SQLite transaction — success
# persists everything, any mid-write failure leaves the pre-state exactly
# intact, and a clean retry afterwards works.


def _scan_payloads(wid):
    """A representative scan write-set (all five payload groups)."""
    address_rows = [
        _addr(wid, 0, 0, "tb1qaddr0", status="used"),
        _addr(wid, 0, 1, "tb1qaddr1"),
        _addr(wid, 1, 0, "tb1qchange0"),
    ]
    derivation_states = [
        DerivationRecord(wid, 0, 0, 1),
        DerivationRecord(wid, 1, -1, 0),
    ]
    utxo_snapshot = [_utxo(wid, "aa" * 32, 0, "tb1qaddr0", 50_000, 1, 10)]
    tx_rows = [TxRecord(wid, "aa" * 32, 10, 1_700_000_000, 500, "in", None)]
    sync_state_updates = {
        "last_scan_cursor": '{"0": 24, "1": 20}',
        "last_tip_height": "870000",
        "last_scan_at": "2026-08-31T00:00:00+00:00",
        "out_of_window_detected": '{"detected_at": null, "branches": {}}',
    }
    return address_rows, derivation_states, utxo_snapshot, tx_rows, sync_state_updates


def _persist_scan(store, wid, payloads):
    address_rows, derivation_states, utxo_snapshot, tx_rows, sync = payloads
    store.persist_scan_result(
        wid,
        address_rows=address_rows,
        derivation_states=derivation_states,
        utxo_snapshot=utxo_snapshot,
        tx_rows=tx_rows,
        sync_state_updates=sync,
    )


def test_persist_scan_result_success_persists_everything():
    with Store.memory() as store:
        wid = store.create_wallet("main", DESCRIPTOR).id
        # Stale prior state that the composite write must fully replace.
        store.replace_utxos_for_wallet(
            wid, [_utxo(wid, "ff" * 32, 9, "tb1qold", 111, 1, 5)]
        )
        store.update_derivation(wid, 0, max_used_index=99, next_index=99)
        store.set_sync_state(wid, "last_scan_cursor", "stale")

        _persist_scan(store, wid, _scan_payloads(wid))

        addresses0 = {a.index: a for a in store.get_addresses(wid, 0)}
        assert addresses0[0].address == "tb1qaddr0"
        assert addresses0[0].status == "used"
        assert addresses0[1].status == "unused"
        assert store.get_by_address("tb1qchange0") is not None
        d0, d1 = store.get_derivation(wid, 0), store.get_derivation(wid, 1)
        assert (d0.max_used_index, d0.next_index) == (0, 1)  # replaced, not stale
        assert (d1.max_used_index, d1.next_index) == (-1, 0)
        assert [(u.txid, u.value_sats) for u in store.get_utxos_for_wallet(wid)] == [
            ("aa" * 32, 50_000)
        ]  # old ff… utxo gone (snapshot replace)
        assert [(t.txid, t.direction) for t in store.get_txs_for_wallet(wid)] == [
            ("aa" * 32, "in")
        ]
        assert store.get_sync_state(wid, "last_scan_cursor") == '{"0": 24, "1": 20}'
        assert store.get_sync_state(wid, "last_tip_height") == "870000"
        assert store.get_sync_state(wid, "last_scan_at") == "2026-08-31T00:00:00+00:00"
        assert store.get_sync_state(wid, "out_of_window_detected") == (
            '{"detected_at": null, "branches": {}}'
        )


def test_persist_scan_result_uses_exactly_one_transaction():
    """The composite call issues exactly one BEGIN and one COMMIT — all
    writes share a single transaction (no per-table autocommit commits)."""
    with Store.memory() as store:
        wid = store.create_wallet("main", DESCRIPTOR).id
        statements: list[str] = []
        store._conn.set_trace_callback(statements.append)
        try:
            _persist_scan(store, wid, _scan_payloads(wid))
        finally:
            store._conn.set_trace_callback(None)
        begins = [s for s in statements if s.upper().startswith("BEGIN")]
        commits = [s for s in statements if s.upper().startswith("COMMIT")]
        rollbacks = [s for s in statements if s.upper().startswith("ROLLBACK")]
        assert len(begins) == 1
        assert len(commits) == 1
        assert rollbacks == []


def test_persist_scan_result_failure_mid_write_persists_nothing_then_retries(
    monkeypatch,
):
    """Forced failure after some in-transaction writes: NOTHING persists —
    old utxos still there, derivation and sync_state unchanged — and a
    clean retry afterwards lands everything."""
    with Store.memory() as store:
        wid = store.create_wallet("main", DESCRIPTOR).id
        store.upsert_batch([_addr(wid, 0, 0, "tb1qoldaddr", status="used")])
        store.update_derivation(wid, 0, max_used_index=7, next_index=8)
        store.replace_utxos_for_wallet(
            wid, [_utxo(wid, "ee" * 32, 0, "tb1qoldaddr", 42, 1, 3)]
        )
        store.set_sync_state(wid, "last_scan_cursor", "old-cursor")
        store.set_sync_state(wid, "last_scan_at", "old-timestamp")
        pre_addresses0 = store.get_addresses(wid, 0)
        pre_derivation0 = store.get_derivation(wid, 0)
        pre_utxos = store.get_utxos_for_wallet(wid)
        pre_txs = store.get_txs_for_wallet(wid)
        pre_cursor = store.get_sync_state(wid, "last_scan_cursor")
        pre_scan_at = store.get_sync_state(wid, "last_scan_at")

        def _explode(wallet_id: int, records: object) -> None:
            raise RuntimeError("forced persist failure")

        # Fails mid-transaction: the address/derivation writes already
        # executed (and the utxo DELETE is about to) — all must be undone.
        monkeypatch.setattr(store, "_replace_utxo_rows", _explode)
        with pytest.raises(RuntimeError, match="forced persist failure"):
            _persist_scan(store, wid, _scan_payloads(wid))

        assert store.get_addresses(wid, 0) == pre_addresses0
        assert store.get_addresses(wid, 1) == []
        assert store.get_derivation(wid, 0) == pre_derivation0
        assert (pre_derivation0.max_used_index, pre_derivation0.next_index) == (7, 8)
        assert store.get_utxos_for_wallet(wid) == pre_utxos
        assert store.get_txs_for_wallet(wid) == pre_txs == []
        assert store.get_sync_state(wid, "last_scan_cursor") == pre_cursor
        assert store.get_sync_state(wid, "last_scan_at") == pre_scan_at

        # Clean retry (patch removed): everything lands in one go.
        monkeypatch.undo()
        _persist_scan(store, wid, _scan_payloads(wid))
        assert [(u.txid, u.value_sats) for u in store.get_utxos_for_wallet(wid)] == [
            ("aa" * 32, 50_000)
        ]
        assert (store.get_derivation(wid, 0).max_used_index,
                store.get_derivation(wid, 0).next_index) == (0, 1)
        assert store.get_sync_state(wid, "last_scan_cursor") == '{"0": 24, "1": 20}'
        assert [a.address for a in store.get_addresses(wid, 0)] == [
            "tb1qaddr0",
            "tb1qaddr1",
        ]


def test_persist_scan_result_integrity_failure_rolls_back_delete_and_writes():
    """A real constraint violation (duplicate txid/vout inside the snapshot)
    fires after the UTXO DELETE and the address/derivation writes — the
    rollback restores all of it, and the message stays value-free."""
    with Store.memory() as store:
        wid = store.create_wallet("main", DESCRIPTOR).id
        store.upsert_batch([_addr(wid, 0, 0, "tb1qoldaddr", status="used")])
        store.update_derivation(wid, 0, max_used_index=3, next_index=4)
        old_utxos = [_utxo(wid, "dd" * 32, 0, "tb1qoldaddr", 7, 1, 2)]
        store.replace_utxos_for_wallet(wid, old_utxos)
        store.set_sync_state(wid, "last_scan_cursor", "old-cursor")

        address_rows, derivation_states, _, tx_rows, sync = _scan_payloads(wid)
        duplicate_snapshot = [
            _utxo(wid, "aa" * 32, 0, "tb1qaddr0", 50_000, 1, 10),
            _utxo(wid, "aa" * 32, 0, "tb1qaddr0", 50_000, 1, 10),
        ]
        with pytest.raises(StoreIntegrityError) as excinfo:
            store.persist_scan_result(
                wid,
                address_rows=address_rows,
                derivation_states=derivation_states,
                utxo_snapshot=duplicate_snapshot,
                tx_rows=tx_rows,
                sync_state_updates=sync,
            )
        assert excinfo.value.__cause__ is not None  # chaining preserved
        message = str(excinfo.value)
        assert "tb1qaddr0" not in message and "tb1qoldaddr" not in message

        # Pre-state exactly intact (incl. the utxo DELETE that was undone).
        assert [a.address for a in store.get_addresses(wid, 0)] == ["tb1qoldaddr"]
        assert store.get_addresses(wid, 1) == []
        assert store.get_derivation(wid, 0) == DerivationRecord(wid, 0, 3, 4)
        assert store.get_utxos_for_wallet(wid) == old_utxos
        assert store.get_txs_for_wallet(wid) == []
        assert store.get_sync_state(wid, "last_scan_cursor") == "old-cursor"
        assert store.get_sync_state(wid, "last_scan_at") is None

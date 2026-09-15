"""TCK-RBF-001 — store lineage foundation (schema v3).

Covers the ticket's unit-test list:
* v3 migration: a real v2 file upgrades on reopen, fresh creates are v3,
  the step is idempotent, malformed shapes fail closed, and the v2
  ``coin_labels`` surface stays untouched by the v3 rung (pinned — the v6
  fold later RETAINS the table write-frozen but never rewrites its rows);
* broadcast-time capture + lineage upsert semantics (COALESCE-preserve —
  a scan can never clobber what the broadcast handler recorded);
* the typed ``record_replacement`` writer (fail-closed, value-free);
* the superseded-retirement rule: replacement confirms → original
  ``replaced``-terminal; original confirms → replacement
  ``evicted``-terminal; neither → both stay pending;
* ``_pending_summary`` excludes retired rows (the pending block can never
  show a count that can never go down), value-free throughout.
"""

import re
import sqlite3
from pathlib import Path

import pytest

from localwallet.app import _pending_summary
from localwallet.store import (
    DIR_IN,
    DIR_OUT,
    SCHEMA_VERSION,
    SUPERSEDED_EVICTED,
    SUPERSEDED_REPLACED,
    Store,
    StoreError,
    TxRecord,
    UtxoRecord,
    superseded_states,
)

DESCRIPTOR = "wpkh([abcd1234/84'/0'/0']vpub/0/*)"
ORIG = "a" * 64  # the original tx of the lineage pair
REPL = "b" * 64  # its RBF replacement
CONF = "c" * 64  # an unrelated confirmed tx

_V3_COLUMNS = ("amount_sats", "fee_rate_centisat_vb", "first_seen", "replaced_by_txid")


def _tx(
    wallet_id: int,
    txid: str,
    *,
    height: int | None = None,
    direction: str = DIR_OUT,
    fee_sats: int | None = None,
    amount_sats: int | None = None,
    fee_rate_centisat_vb: int | None = None,
    first_seen: int | None = None,
    replaced_by_txid: str | None = None,
) -> TxRecord:
    return TxRecord(
        wallet_id=wallet_id,
        txid=txid,
        height=height,
        block_time=None if height is None else 1_700_000_000,
        fee_sats=fee_sats,
        direction=direction,
        raw_summary=None,
        amount_sats=amount_sats,
        fee_rate_centisat_vb=fee_rate_centisat_vb,
        first_seen=first_seen,
        replaced_by_txid=replaced_by_txid,
    )


def _utxo(wallet_id: int, txid: str, sats: int, confirmed: int) -> UtxoRecord:
    return UtxoRecord(
        wallet_id=wallet_id,
        txid=txid,
        vout=0,
        address="bc1qexample",
        value_sats=sats,
        confirmed=confirmed,
        height=900_000 if confirmed == 1 else None,
    )


def _wallet(store: Store) -> int:
    return store.create_wallet("main", DESCRIPTOR).id


def _as_v2_file(db: Path, *, drop_columns: tuple[str, ...] = _V3_COLUMNS) -> None:
    """Simulate a REAL pre-v3 file: remove (some of) the v3 columns, make
    sure the v2 tables exist (a real v2-era DB always had ``coin_labels``;
    a fresh v6 DB no longer creates it), and stamp user_version=2 — the
    exact on-disk shape the v2→v3 rung must recover."""
    from localwallet.store.db import _COIN_LABELS_DDL

    raw = sqlite3.connect(db)
    for col in drop_columns:
        raw.execute(f"ALTER TABLE transactions DROP COLUMN {col}")
    raw.executescript(_COIN_LABELS_DDL)
    raw.execute("PRAGMA user_version=2")
    raw.commit()
    raw.close()


def _version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _tx_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}


# ------------------------------------------------------- v2 -> v3 migration


def test_v2_db_upgrades_to_v3_on_reopen(tmp_path: Path) -> None:
    db = tmp_path / "store.db"
    with Store(db) as store:
        wid = _wallet(store)
        store.upsert_txs([_tx(wid, ORIG, fee_sats=500, amount_sats=None)])
    _as_v2_file(db)

    with Store(db) as store:  # the migration runs here
        # Full upgrade lands on the CURRENT schema version: the v2→v3 rung
        # is followed by the v3→v4 add of ``address_registry`` (TCK-CHAT-001),
        # the v4→v5 add of ``address_labels`` (TCK-LABEL-001), and the v5→v6
        # fold to ``address_label_set`` (TCK-LABELS-UNIFY) riding the same
        # ladder. The v3 COLUMN/data assertions below are what this test is
        # actually about.
        assert _version(store._conn) == SCHEMA_VERSION == 6
        assert set(_V3_COLUMNS) <= _tx_columns(store._conn)
        # The v2 tx row survived untouched, new fields reading as not-recorded.
        rows = store.get_txs_for_wallet(wid)
        assert [r.txid for r in rows] == [ORIG]
        assert rows[0].height is None
        assert rows[0].amount_sats is None
        assert rows[0].first_seen is None
        assert rows[0].replaced_by_txid is None
    with Store(db) as store:  # stable reopen at the top version (no re-run)
        assert _version(store._conn) == 6


def test_v3_migration_is_idempotent_half_applied(tmp_path: Path) -> None:
    """Crash-mid-migration shape (v2 stamp, SOME columns already added): the
    guarded ALTERs skip what exists and complete the rest — never a
    duplicate-column failure (the migration contract: steps are idempotent,
    the version stamp is written last)."""
    db = tmp_path / "store.db"
    with Store(db) as store:
        _wallet(store)
    _as_v2_file(db, drop_columns=("first_seen", "replaced_by_txid"))

    with Store(db) as store:
        assert _version(store._conn) == 6
        assert set(_V3_COLUMNS) <= _tx_columns(store._conn)


def test_fresh_create_is_v3(tmp_path: Path) -> None:
    with Store(tmp_path / "fresh.db") as store:
        assert _version(store._conn) == 6
        assert set(_V3_COLUMNS) <= _tx_columns(store._conn)


def test_migration_refuses_newer_schema(tmp_path: Path) -> None:
    db = tmp_path / "store.db"
    with Store(db):
        pass
    raw = sqlite3.connect(db)
    raw.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    raw.commit()
    raw.close()
    with pytest.raises(StoreError):
        Store(db)


def test_migration_fail_closed_on_malformed_v2(tmp_path: Path) -> None:
    """A stamped-v2 file whose ``transactions`` table is gone has no
    known-safe upgrade: the rung REFUSED (StoreError), the stamp stays at 2,
    and the rest of the file survives (never a silent rebuild of history)."""
    db = tmp_path / "store.db"
    with Store(db) as store:
        _wallet(store)
    raw = sqlite3.connect(db)
    raw.execute("DROP TABLE transactions")
    raw.execute("PRAGMA user_version=2")
    raw.commit()
    raw.close()

    with pytest.raises(StoreError):
        Store(db)
    raw = sqlite3.connect(db)
    assert _version(raw) == 2  # untouched — the failed rung rolled back
    assert raw.execute("SELECT name FROM wallets").fetchone()[0] == "main"
    raw.close()


def test_v2_to_v3_leaves_coin_labels_untouched(tmp_path: Path) -> None:
    """PIN (TCK-UTXO-001 surface): the v3 rung adds columns to
    ``transactions`` ONLY — the label table's DDL and every row survive the
    migration byte-identical."""
    db = tmp_path / "store.db"
    with Store(db) as store:
        wid = _wallet(store)
    raw = sqlite3.connect(db)
    # A real v2 file HAS the table (a fresh v6 file never creates it): write
    # the v2 shape raw — the migration is the table's only reader now.
    from localwallet.store.db import _COIN_LABELS_DDL

    raw.executescript(_COIN_LABELS_DDL)
    raw.execute(
        "INSERT INTO coin_labels (wallet_id, txid, vout, tags, note) VALUES (?,?,?,?,?)",
        (wid, ORIG, 0, "kyc,p2p", "keep me exactly"),
    )
    ddl_before = raw.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'coin_labels'"
    ).fetchone()[0]
    rows_before = raw.execute(
        "SELECT wallet_id, txid, vout, tags, note FROM coin_labels"
    ).fetchall()
    raw.commit()
    raw.close()
    _as_v2_file(db)

    with Store(db) as store:
        assert _version(store._conn) == 6  # full ladder v2→…→v6 completes
    raw = sqlite3.connect(db)
    # The v3 RUNG touched nothing here — and the v6 fold, which RETAINS the
    # table write-frozen, never rewrites or deletes a row either (the rows
    # have no utxo match in this file, so they stay exactly as written).
    assert (
        raw.execute("SELECT sql FROM sqlite_master WHERE name = 'coin_labels'").fetchone()[0]
        == ddl_before
    )
    assert raw.execute(
        "SELECT wallet_id, txid, vout, tags, note FROM coin_labels"
    ).fetchall() == rows_before
    raw.close()


# ------------------------------------------------- broadcast capture upsert


def test_broadcast_capture_roundtrip() -> None:
    with Store.memory() as store:
        wid = _wallet(store)
        store.upsert_txs(
            [_tx(wid, ORIG, fee_sats=500, amount_sats=42_000,
                 fee_rate_centisat_vb=213, first_seen=1_757_000_000)]
        )
        r = store.get_txs_for_wallet(wid)[0]
        assert (r.amount_sats, r.fee_rate_centisat_vb, r.first_seen) == (
            42_000, 213, 1_757_000_000,
        )
        assert r.height is None and r.replaced_by_txid is None


def test_scan_upsert_never_clobbers_the_capture() -> None:
    """The scan re-observes the tx (chain truth: height/direction) with NULL
    capture fields — COALESCE-preserve keeps the broadcast record, the scan
    columns still update. This is the persist-scan reconciliation half of
    TCK-RBF-001: it runs inside ``persist_scan_result``'s single atomic tx
    through the same shared statement, never a forked write."""
    with Store.memory() as store:
        wid = _wallet(store)
        store.upsert_txs(
            [_tx(wid, ORIG, fee_sats=500, amount_sats=42_000,
                 fee_rate_centisat_vb=213, first_seen=1_757_000_000)]
        )
        store.persist_scan_result(
            wid,
            address_rows=[],
            derivation_states=[],
            utxo_snapshot=[],
            tx_rows=[_tx(wid, ORIG, height=900_001, fee_sats=501)],
            sync_state_updates={},
        )
        r = store.get_txs_for_wallet(wid)[0]
        assert (r.height, r.fee_sats) == (900_001, 501)  # scan truth updated
        assert (r.amount_sats, r.fee_rate_centisat_vb, r.first_seen) == (
            42_000, 213, 1_757_000_000,
        )  # capture survived


def test_record_replacement_link_survives_scan_upserts() -> None:
    with Store.memory() as store:
        wid = _wallet(store)
        store.upsert_txs([_tx(wid, ORIG), _tx(wid, REPL)])
        store.record_replacement(wid, ORIG, REPL)
        store.upsert_txs([_tx(wid, ORIG, height=None, fee_sats=600)])  # NULL link
        r = store.get_txs_for_wallet(wid)
        links = {x.txid: x.replaced_by_txid for x in r}
        assert links == {ORIG: REPL, REPL: None}


# ------------------------------------------------- record_replacement writer


def test_record_replacement_sets_link() -> None:
    with Store.memory() as store:
        wid = _wallet(store)
        store.upsert_txs([_tx(wid, ORIG)])
        store.record_replacement(wid, ORIG, REPL)
        assert store.get_txs_for_wallet(wid)[0].replaced_by_txid == REPL
        store.record_replacement(wid, ORIG, CONF)  # re-link (re-bump) allowed
        assert store.get_txs_for_wallet(wid)[0].replaced_by_txid == CONF


def test_record_replacement_fail_closed_value_free() -> None:
    with Store.memory() as store:
        wid = _wallet(store)
        store.upsert_txs([_tx(wid, ORIG)])
        for bad in ("z" * 64, "a" * 63, "A" * 64, ""):
            with pytest.raises(StoreError):
                store.record_replacement(wid, bad, REPL)
            with pytest.raises(StoreError):
                store.record_replacement(wid, ORIG, bad)
        with pytest.raises(StoreError):
            store.record_replacement(wid, ORIG, ORIG)  # self-link is nonsense
        with pytest.raises(StoreError):
            store.record_replacement(wid, REPL, ORIG)  # original has no row
        assert store.get_txs_for_wallet(wid)[0].replaced_by_txid is None
        # Every refusal message is value-free (no txid, no echoes).
        for args in ((wid, "z" * 64, REPL), (wid, ORIG, ORIG), (wid, REPL, ORIG)):
            with pytest.raises(StoreError) as excinfo:
                store.record_replacement(*args)
            assert ORIG not in str(excinfo.value)
            assert REPL not in str(excinfo.value)
            assert "z" * 8 not in str(excinfo.value)


# ------------------------------------------------- the retirement rule itself


def test_replacement_confirms_original_is_replaced() -> None:
    states = superseded_states(
        [
            _tx(1, ORIG, replaced_by_txid=REPL),  # pending original, linked
            _tx(1, REPL, height=900_002),  # the replacement confirmed
        ]
    )
    assert states == {ORIG: SUPERSEDED_REPLACED}


def test_original_confirms_replacement_is_evicted() -> None:
    states = superseded_states(
        [
            _tx(1, ORIG, height=900_001, replaced_by_txid=REPL),  # original won
            _tx(1, REPL),  # the bump stays pending forever — evicted
        ]
    )
    assert states == {REPL: SUPERSEDED_EVICTED}


def test_neither_confirmed_both_stay_pending() -> None:
    states = superseded_states([_tx(1, ORIG, replaced_by_txid=REPL), _tx(1, REPL)])
    assert states == {}  # the race is still live — both honestly pending


def test_retirement_edge_shapes() -> None:
    # Link to a txid with NO row in the cache: we do not know yet — no
    # retirement, never a fabrication.
    assert superseded_states([_tx(1, ORIG, replaced_by_txid=REPL)]) == {}
    # Both confirmed (should be impossible per BIP-125, but the rule reads
    # the heights it is given): nothing is pending, so nothing retires.
    assert (
        superseded_states(
            [_tx(1, ORIG, height=1, replaced_by_txid=REPL), _tx(1, REPL, height=2)]
        )
        == {}
    )
    # An unlinked pending tx never retires; direction/inbound rows are judged
    # the same way (the rule is structural, not direction-based).
    assert superseded_states([_tx(1, CONF), _tx(1, ORIG, direction=DIR_IN)]) == {}
    assert superseded_states([]) == {}


def test_persist_scan_confirms_side_sibling_retires() -> None:
    """End to end: broadcast-captured pair with a lineage link; a scan
    confirms the REPLACEMENT inside persist_scan_result's atomic tx; the
    original reads as ``replaced``-terminal and the captured fields of both
    rows survived the rescan."""
    with Store.memory() as store:
        wid = _wallet(store)
        store.upsert_txs(
            [
                _tx(wid, ORIG, fee_sats=500, amount_sats=42_000,
                    fee_rate_centisat_vb=213, first_seen=1_757_000_000),
                _tx(wid, REPL, fee_sats=900, amount_sats=42_000,
                    fee_rate_centisat_vb=410, first_seen=1_757_000_300),
            ]
        )
        store.record_replacement(wid, ORIG, REPL)
        store.persist_scan_result(
            wid,
            address_rows=[],
            derivation_states=[],
            utxo_snapshot=[_utxo(wid, CONF, 10_000, 1)],
            tx_rows=[_tx(wid, ORIG), _tx(wid, REPL, height=900_002, fee_sats=900)],
            sync_state_updates={},
        )
        rows = store.get_txs_for_wallet(wid)
        assert superseded_states(rows) == {ORIG: SUPERSEDED_REPLACED}
        by_txid = {r.txid: r for r in rows}
        assert by_txid[ORIG].amount_sats == 42_000  # capture survived
        assert by_txid[ORIG].replaced_by_txid == REPL  # link survived
        assert by_txid[REPL].first_seen == 1_757_000_300


# ------------------------------------------------- _pending_summary contract


def test_summary_excludes_retired_from_pending_outgoing() -> None:
    rows = [
        _tx(1, ORIG, replaced_by_txid=REPL),
        _tx(1, REPL, height=900_002),  # the replacement confirmed → ORIG retires
        _tx(1, "d" * 64),  # an unrelated pending spend stays
    ]
    summary = _pending_summary([], rows)
    assert summary["pending_outgoing_count"] == 1


def test_summary_all_retired_block_disappears() -> None:
    """The ghost-pending case the rule exists for: the ONLY pending-looking
    rows are a lineage pair whose replacement confirmed — the block must be
    EMPTY (keys absent), never a count that can never go down."""
    rows = [
        _tx(1, ORIG, replaced_by_txid=REPL),
        _tx(1, REPL, height=900_002),
    ]
    assert _pending_summary([], rows) == {}


def test_summary_evicted_bump_exits_pending() -> None:
    rows = [
        _tx(1, ORIG, height=900_001, replaced_by_txid=REPL),  # original confirmed
        _tx(1, REPL),  # the bump is terminal-evicted, not pending
    ]
    assert _pending_summary([], rows) == {}


def test_summary_neither_confirmed_both_counted() -> None:
    rows = [_tx(1, ORIG, replaced_by_txid=REPL), _tx(1, REPL)]
    assert _pending_summary([], rows)["pending_outgoing_count"] == 2


def test_summary_retired_spends_change_coin_is_not_incoming() -> None:
    """The unconfirmed change coin a REPLACED original would have created can
    never come to be — it must not resurface as an incoming payment after
    the original exits the outgoing count."""
    rows = [
        _tx(1, ORIG, replaced_by_txid=REPL),  # retired by the confirmed...
        _tx(1, REPL, height=900_002),  # ...replacement
    ]
    utxos = [_utxo(1, ORIG, 30_000, confirmed=0)]  # ORIG's change-shaped coin
    summary = _pending_summary(utxos, rows)
    assert summary.get("pending_incoming_count", 0) == 0
    assert summary.get("pending_outgoing_count", 0) == 0
    assert "pending_incoming_sats" not in summary or summary["pending_incoming_sats"] == 0


def test_summary_shape_and_value_free() -> None:
    rows = [_tx(1, ORIG, replaced_by_txid=REPL, amount_sats=42_000), _tx(1, REPL)]
    summary = _pending_summary([_utxo(1, "e" * 64, 123, confirmed=0)], rows)
    assert set(summary) == {
        "pending_incoming_count",
        "pending_incoming_sats",
        "pending_outgoing_count",
        "pending_eta_note",
    }
    text = str(summary)
    # txids never ride the pending block (counts + sums only).
    assert not re.search(r"[0-9a-f]{64}", text)

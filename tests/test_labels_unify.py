"""TCK-LABELS-UNIFY — schema v6: the address label SET is the labeling truth.

Migration + parity pins for the user-ratified model (2026-09-13: one address
= one private key = one provenance; coins inherit the address's set; a
per-UTXO label is a union-addition to it; ``coin_labels`` migrates by
per-address union):

* a real v5 DB (v5 ``address_labels`` + v2 ``coin_labels`` rows + a utxos
  snapshot) upgrades CLEANLY on reopen, union-merging per address: tags and
  notes of every resolvable coin row join the address's set, v5 single labels
  fold in (tag words canonicalized: "KYC" → the engine id "kyc");
* the v5 ``address_labels`` table is FOLDED (total map → dropped); the
  outpoint-keyed ``coin_labels`` table is RETAINED write-frozen whole — its
  rows (resolvable and spent alike) stay, never silently dropped; the union
  merely copies what resolves;
* lenient on what v5 legitimately allowed: blank (empty/whitespace-only) and
  non-printable label/note members (v5 writers rejected only empty and over-
  cap) COALESCE to "skip this member" — the row still migrates, contributing
  nothing to the union, never a refused upgrade;
* FAIL-CLOSED on genuine corruption: an unknown tag id, an over-cap member,
  a missing legacy table, or a malformed address key REFUSES the upgrade
  value-free, the DB stays a pristine v5 (one-transaction rung: no partial
  fold), and every row is still there afterwards;
* idempotence: half-applied shapes (set table already created) and stable
  re-opens re-run cleanly; fresh creates are v6 without either legacy table;
* SELECTION PARITY: the pre/post-migration pool assignment is identical for
  identically-labeled (uniform-per-address) data — the engine's
  ``select_coins`` returns a byte-equal ``SelectionResult`` over the v5 coin-
  row join and the v6 address-set join;
* the union-add accessor (idempotence, read-back truth, canonicalization,
  fail-closed value-free refusals) and the v6 table's rescan survival are
  pinned in tests/test_coin_labels.py.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from localwallet.store import SCHEMA_VERSION, Store, StoreError, UtxoRecord
from localwallet.tx.dust import dust_threshold
from localwallet.tx.selection import coin_partition, select_coins

ADDR_A = "bc1qaddressaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"  # shape-checked key
ADDR_B = "bc1qaddressbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
ADDR_C = "bc1qaddressccccccccccccccccccccccccccccccc"
ADDR_D = "bc1qaddressddddddddddddddddddddddddddddddd"  # labeled only by a blank row
TX_A = "a" * 64  # at ADDR_A (unspent)
TX_B = "b" * 64  # at ADDR_A (unspent, same address — one provenance)
TX_C = "c" * 64  # at ADDR_B (unspent)
TX_SPENT = "d" * 64  # a labeled coin NO LONGER in the snapshot (spent)
P2WPKH = b"\x00\x14" + b"\x11" * 20


def _coin_rows(conn: sqlite3.Connection, wallet_id: int) -> None:
    """The v5-era label surface, written by RAW inserts (the v6 store has no
    coin-label accessor by design — migration is the table's only reader)."""
    conn.execute(
        "INSERT INTO coin_labels (wallet_id, txid, vout, tags, note) VALUES (?,?,?,?,?)",
        (wallet_id, TX_A, 0, "kyc", "from the exchange"),
    )
    conn.execute(
        "INSERT INTO coin_labels (wallet_id, txid, vout, tags, note) VALUES (?,?,?,?,?)",
        (wallet_id, TX_B, 0, "kyc,exchange", None),
    )
    conn.execute(
        "INSERT INTO coin_labels (wallet_id, txid, vout, tags, note) VALUES (?,?,?,?,?)",
        (wallet_id, TX_C, 0, "p2p", "bike"),
    )
    conn.execute(
        "INSERT INTO coin_labels (wallet_id, txid, vout, tags, note) VALUES (?,?,?,?,?)",
        (wallet_id, TX_SPENT, 0, "exchange", "gone coin's history"),
    )


def _as_v5_file(db: Path, *, coin_rows: bool = True, v5_label: bool = True) -> int:
    """Open a fresh v6 store, roll the label tables back to the v5 shape,
    and (optionally) fill the v5 surfaces with raw rows. Returns wallet_id."""
    with Store(db) as store:
        wid = store.create_wallet("main", "desc").id
        store.replace_utxos_for_wallet(
            wid,
            [
                UtxoRecord(wid, TX_A, 0, ADDR_A, 50_000, 1, 900_000),
                UtxoRecord(wid, TX_B, 0, ADDR_A, 30_000, 1, 900_000),
                UtxoRecord(wid, TX_C, 0, ADDR_B, 20_000, 1, 900_000),
                # ADDR_C has coins and NO labels: must stay unlabeled.
            ],
        )
    raw = sqlite3.connect(db)
    raw.execute("DROP TABLE address_label_set")
    raw.executescript(
        """
        CREATE TABLE coin_labels (
            wallet_id  INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
            txid       TEXT NOT NULL,
            vout       INTEGER NOT NULL,
            tags       TEXT NOT NULL,
            note       TEXT,
            PRIMARY KEY (wallet_id, txid, vout)
        );
        CREATE TABLE address_labels (
            address    TEXT PRIMARY KEY,
            label      TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        PRAGMA user_version=5;
        """
    )
    if coin_rows:
        _coin_rows(raw, wid)
    if v5_label:
        raw.execute(
            "INSERT INTO address_labels VALUES (?,?,?,?)", (ADDR_C, "KYC", "t", "t")
        )
    raw.commit()
    raw.close()
    return wid


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# --------------------------------------------------------------- migration


def test_v5_db_upgrades_with_per_address_union(tmp_path: Path) -> None:
    db = tmp_path / "store.db"
    _as_v5_file(db)
    with Store(db) as store:
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 6
        # ADDR_A: union of BOTH coins' tags + the first coin's note (free-
        # text member); ADDR_B: p2p + "bike"; ADDR_C: v5 "KYC" canonicalized
        # to the engine id — free-text members sort after the closed tags.
        assert store.get_address_label_set(ADDR_A) == (
            "kyc",
            "exchange",
            "from the exchange",
        )
        assert store.get_address_label_set(ADDR_B) == ("p2p", "bike")
        assert store.get_address_label_set(ADDR_C) == ("kyc",)
        # ADDR_C's unlabeled coins: unlabeled is NO ROWS, never an empty one.
        # (ADDR_C WAS labeled by the v5 chat surface; a never-labeled
        # address simply has no set — pinned next, and in coin_labels tests.)
        assert store.get_address_label_set("bc1qneverlabeled") == ()


def test_fold_drops_v5_table_and_retains_frozen_coin_history(tmp_path: Path) -> None:
    db = tmp_path / "store.db"
    _as_v5_file(db)
    raw = sqlite3.connect(db)
    spent_row_before = raw.execute(
        "SELECT wallet_id, txid, vout, tags, note FROM coin_labels WHERE txid = ?",
        (TX_SPENT,),
    ).fetchall()
    raw.close()

    with Store(db) as store:
        tables = _tables(store._conn)
        assert "address_labels" not in tables  # FOLDED (total map, dropped)
        assert "coin_labels" in tables  # RETAINED (spent rows had nowhere to go)
        assert "address_label_set" in tables
        # The unresolvable (spent-coin) rows survive EXACTLY — nothing
        # silently dropped, nothing relocated by guess.
        assert [
            tuple(r)
            for r in store._conn.execute(
                "SELECT wallet_id, txid, vout, tags, note FROM coin_labels WHERE txid = ?",
                (TX_SPENT,),
            )
        ] == spent_row_before
        # …and the spent coin's tags did NOT leak into anyone's set.
        assert all("gone coin's history" not in members for members in
                   store.get_address_label_sets().values())


def test_migration_is_idempotent_half_applied(tmp_path: Path) -> None:
    """Crash-mid-migration shape: the set table already EXISTS but the file
    is still stamped v5 (the rung's own transaction died before its COMMIT —
    the v5 tables must still be intact in that shape; re-running the whole
    rung re-copies and completes, INSERT-OR-IGNORE-clean)."""
    db = tmp_path / "store.db"
    _as_v5_file(db)
    raw = sqlite3.connect(db)
    raw.execute(
        "CREATE TABLE address_label_set (address TEXT NOT NULL, label TEXT NOT NULL,"
        " created_at TEXT NOT NULL, PRIMARY KEY (address, label))"
    )
    raw.execute("INSERT INTO address_label_set VALUES (?,?,?)", (ADDR_C, "kyc", "pre"))
    raw.commit()
    raw.close()
    with Store(db) as store:
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 6
        # the pre-seeded member survived the re-run (OR IGNORE), and the real
        # fold completed around it.
        assert store.get_address_label_set(ADDR_A) == (
            "kyc",
            "exchange",
            "from the exchange",
        )
        created = store._conn.execute(
            "SELECT created_at FROM address_label_set WHERE address=? AND label='kyc'",
            (ADDR_C,),
        ).fetchone()[0]
        assert created == "pre"  # write-once survives the re-run


def test_reopen_after_upgrade_never_re_runs(tmp_path: Path) -> None:
    db = tmp_path / "store.db"
    _as_v5_file(db)
    with Store(db) as store:
        first = store.get_address_label_sets()
    with Store(db) as store:  # stable: already at v6, no rung runs
        assert store.get_address_label_sets() == first


def test_post_commit_pre_stamp_crash_shape_reopens_cleanly(tmp_path: Path) -> None:
    """FINDING 1 regression pin: a crash that landed AFTER the v5→v6 fold's
    COMMIT but BEFORE the version stamp (a pre-fix build's window) leaves a
    fully-migrated DB stamped v5 — ``address_label_set`` present and
    populated, ``address_labels`` already dropped, ``coin_labels`` retained.
    That is a HEALTHY wallet that must reopen (complete by stamping v6), never
    the permanent "missing legacy table" lockout. The rung now stamps v6
    INSIDE its own transaction, so this shape is unreachable from new code —
    the pin guards the recovery path for any pre-fix artifact."""
    db = tmp_path / "store.db"
    _as_v5_file(db)
    raw = sqlite3.connect(db)
    # Simulate the fold already applied: set table present + populated,
    # address_labels dropped, coin_labels retained, still stamped v5.
    raw.executescript(
        "CREATE TABLE address_label_set ("
        " address TEXT NOT NULL, label TEXT NOT NULL, created_at TEXT NOT NULL,"
        " PRIMARY KEY (address, label));"
    )
    raw.execute(
        "INSERT INTO address_label_set VALUES (?,?,?)", (ADDR_C, "kyc", "pre")
    )
    raw.execute("DROP TABLE address_labels")
    raw.commit()
    raw.close()
    with Store(db) as store:
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 6
        # The already-folded member survives; the healthy wallet is whole.
        assert store.get_address_label_set(ADDR_C) == ("kyc",)


def test_blob_tags_refuses_upgrade_value_free_and_keeps_v5(tmp_path: Path) -> None:
    """FINDING 4 regression pin: a BLOB-stored ``tags`` cell (on-disk
    corruption no v5 writer could produce) REFUSES the upgrade as a value-free
    :class:`StoreError` — the non-str guard runs BEFORE ``.split``, so no bare
    TypeError escapes the rung — and the DB stays a pristine v5 (rollback, no
    partial fold)."""
    db = tmp_path / "store.db"
    wid = _as_v5_file(db, coin_rows=False)
    raw = sqlite3.connect(db)
    raw.execute(
        "INSERT INTO coin_labels VALUES (?,?,?,?,?)",
        (wid, TX_A, 0, b"\x00\x01blob", None),
    )
    raw.commit()
    raw.close()
    with pytest.raises(StoreError) as excinfo:
        Store(db)
    message = str(excinfo.value)
    assert TX_A not in message and ADDR_A not in message  # value-free
    raw = sqlite3.connect(db)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 5  # untouched
    assert "address_label_set" not in _tables(raw)  # the rung rolled back whole
    assert "address_labels" in _tables(raw)
    raw.close()


def test_malformed_tag_refuses_upgrade_value_free_and_keeps_v5(tmp_path: Path) -> None:
    """The HARD RULE: a v5 DB with a malformed coin_labels row (here: a tag
    outside the closed set) REFUSES the upgrade cleanly — value-free, DB
    untouched at v5, every legacy row still present. Never a silent drop,
    never a partial fold."""
    db = tmp_path / "store.db"
    wid = _as_v5_file(db, coin_rows=False)
    raw = sqlite3.connect(db)
    raw.execute(
        "INSERT INTO coin_labels VALUES (?,?,?,?,?)", (wid, TX_A, 0, "laundering", None)
    )
    raw.commit()
    raw.close()
    with pytest.raises(StoreError) as excinfo:
        Store(db)
    message = str(excinfo.value)
    assert "laundering" not in message and TX_A not in message and ADDR_A not in message
    raw = sqlite3.connect(db)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 5  # untouched
    assert raw.execute("SELECT count(*) FROM coin_labels").fetchone()[0] == 1
    assert "address_label_set" not in _tables(raw)  # the rung rolled back whole
    assert "address_labels" in _tables(raw)
    raw.close()


def test_over_cap_label_refuses_upgrade(tmp_path: Path) -> None:
    """Over-cap is the ONE label malformation no v5 writer could produce (it
    rejected over-cap), so it is genuine corruption and still REFUSES the
    upgrade cleanly — value-free, DB untouched at v5."""
    db = tmp_path / "store.db"
    _as_v5_file(db, coin_rows=False, v5_label=False)
    raw = sqlite3.connect(db)
    raw.execute("INSERT INTO address_labels VALUES (?,?,?,?)", (ADDR_A, "x" * 501, "t", "t"))
    raw.commit()
    raw.close()
    with pytest.raises(StoreError):
        Store(db)
    raw = sqlite3.connect(db)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 5
    raw.close()


def test_blank_and_non_printable_members_migrate_skipped(tmp_path: Path) -> None:
    """v5 writers rejected only empty and over-cap, so blank (empty or
    whitespace-only) labels and non-printable notes are LEGITIMATE v5 data —
    the migration COALESCES those members to "skip this member" (the row
    contributes nothing to the union) instead of refusing the whole upgrade.
    Over-cap still refuses (a writer could never produce it). Pin: a whitespace-
    only label + a non-printable note upgrade with those members skipped and
    NO data loss of the good rows."""
    for blank in ("", "   "):
        db = tmp_path / f"m-{len(blank)}.db"
        _as_v5_file(db)
        raw = sqlite3.connect(db)
        raw.execute(
            "INSERT INTO address_labels VALUES (?,?,?,?)", (ADDR_D, blank, "t", "t")
        )
        # non-printable note on a resolvable coin (TX_B → ADDR_A): the note
        # member is skipped, the good tags are not.
        raw.execute(
            "UPDATE coin_labels SET note=? WHERE txid=?", ("noise\x07bell", TX_B)
        )
        raw.commit()
        raw.close()
        with Store(db) as store:
            assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 6
            # the blank member contributed NOTHING → ADDR_D's set is empty
            # (never a refused upgrade).
            assert store.get_address_label_set(ADDR_D) == ()
            # good rows survive: ADDR_A keeps its tags (the non-printable note
            # left nothing behind), ADDR_B and ADDR_C are intact.
            assert store.get_address_label_set(ADDR_A) == (
                "kyc",
                "exchange",
                "from the exchange",
            )
            assert store.get_address_label_set(ADDR_B) == ("p2p", "bike")
            assert store.get_address_label_set(ADDR_C) == ("kyc",)
            assert all("\x07" not in m for m in store.get_address_label_sets().values())


def test_missing_legacy_table_refuses(tmp_path: Path) -> None:
    """A stamped-v5 file without BOTH legacy label tables is not this
    schema — refused, never blindly rebuilt."""
    db = tmp_path / "store.db"
    _as_v5_file(db, coin_rows=False, v5_label=False)
    raw = sqlite3.connect(db)
    raw.executescript("DROP TABLE address_labels; DROP TABLE coin_labels;")
    raw.commit()
    raw.close()
    with pytest.raises(StoreError):
        Store(db)


def test_fresh_create_is_v6_without_legacy_tables(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    with Store(db) as store:
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 6
        tables = _tables(store._conn)
        assert "address_label_set" in tables
        assert "coin_labels" not in tables
        assert "address_labels" not in tables
        assert store.get_address_label_sets() == {}


def test_scan_persist_never_touches_the_label_set() -> None:
    """The v5 write-set lesson, kept by v6: a label is a USER fact — the
    scan's composite write leaves address_label_set alone (rescan can never
    clear, re-assign, or fabricate one)."""
    store = Store.memory()
    wallet = store.create_wallet("w", "d")
    store.add_address_labels(ADDR_A, ("kyc",))
    store.persist_scan_result(
        wallet.id,
        address_rows=[],
        derivation_states=[],
        utxo_snapshot=[],  # a scan that sees NOTHING at the address
        tx_rows=[],
        sync_state_updates={"last_scan_height": "800000"},
    )
    assert store.get_address_label_set(ADDR_A) == ("kyc",)
    store.close()


# ---------------------------------------------------------- selection parity


def _snap(txid: str, value: int, **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(txid=txid, vout=0, value_sats=value, **extra)


def _run_selection(utxos: list[Any]) -> dict[str, object]:
    """The pure engine over a given kyc_side assignment (the SAME call the
    handlers make; rates/scripts are fixture-fixed — parity is about the
    POOL resolution, so both runs share everything else)."""
    result = select_coins(
        utxos,
        60_000,
        200,  # 2 sat/vB
        31,
        P2WPKH,
        change_script=P2WPKH,
    )
    return {
        "selected": [(u.txid, u.vout) for u in result.selected],
        "change_sats": result.change_sats,
        "estimated_vsize": result.estimated_vsize,
        "fee_sats": result.fee_sats,
        "inputs_total": result.inputs_total,
        "mixed": result.mixed,
        "folded_count": result.folded_count,
    }


def test_selection_parity_pre_post_migration(tmp_path: Path) -> None:
    """Pin (done-when): for identically-labeled data (every coin uniform on
    its address — the case the user model says the chain already links), the
    pre-migration v5 join (per-OUTPOINT coin rows) and the post-migration v6
    join (inherited ADDRESS sets) assign kyc_side to the SAME coins, and the
    engine's SelectionResult is byte-equal."""
    db = tmp_path / "store.db"
    _as_v5_file(db)

    # --- pre-migration assignment, read off the RAW v5 shape (the store has
    # no coin accessors anymore; the migration input is the ground truth).
    raw = sqlite3.connect(db)
    raw.row_factory = sqlite3.Row
    coin_tags = {
        (r["txid"], r["vout"]): tuple(t for t in (r["tags"] or "").split(",") if t)
        for r in raw.execute("SELECT txid, vout, tags FROM coin_labels")
    }
    addr_of = {
        (r["txid"], r["vout"]): r["address"]
        for r in raw.execute("SELECT txid, vout, address FROM utxos")
    }
    raw.close()
    utxos = [
        _snap(TX_A, 50_000),
        _snap(TX_B, 30_000),
        _snap(TX_C, 20_000),
    ]
    pre = [
        SimpleNamespace(
            txid=u.txid,
            vout=u.vout,
            value_sats=u.value_sats,
            kyc_side=coin_partition(coin_tags.get((u.txid, u.vout), ()))[0],
        )
        for u in utxos
    ]

    # --- post-migration: the join the APP makes (address-inherited sets).
    with Store(db) as store:
        label_sets = store.get_address_label_sets()
    kyc_addresses = {
        address
        for address, members in label_sets.items()
        if coin_partition(members)[0]
    }
    post = [
        SimpleNamespace(
            txid=u.txid,
            vout=u.vout,
            value_sats=u.value_sats,
            kyc_side=addr_of[(u.txid, u.vout)] in kyc_addresses,
        )
        for u in utxos
    ]
    assert [bool(getattr(u, "kyc_side", False)) for u in pre] == [
        bool(u.kyc_side) for u in post
    ]
    # Both sides of the kyc line agree the OTHER side is still other-side:
    assert [u.kyc_side for u in post] == [True, True, False]

    # And the engine outcome is identical over both assignments.
    assert _run_selection(pre) == _run_selection(post)  # type: ignore[arg-type]
    assert _run_selection(pre)["mixed"] is False  # kyc pool (80k) funds alone


def test_selection_parity_unlabeled_wallet_is_the_degenerate_single_pool() -> None:
    """Behavior must be identical when labels are ABSENT: no kyc pool exists,
    the driver degenerates to one run over the full set (pre-amendment
    behavior — the pool semantics ride, unchanged, when the truth is empty)."""
    plain = [_snap(TX_A, 50_000), _snap(TX_C, 20_000)]
    kyc_marked = [
        SimpleNamespace(txid=TX_A, vout=0, value_sats=50_000, kyc_side=False),
        SimpleNamespace(txid=TX_C, vout=0, value_sats=20_000, kyc_side=False),
    ]
    assert _run_selection(plain) == _run_selection(kyc_marked)


def test_dust_floor_guard_for_the_parity_fixture() -> None:
    # The parity fixture's recipient script keeps amounts honest: computed
    # dust, never a hardcoded assumption.
    assert 60_000 >= dust_threshold(P2WPKH)

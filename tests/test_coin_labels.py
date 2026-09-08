"""TCK-UTXO-001 — coin tags + free-text notes (store schema v2, /label).

Pins, per ticket done-when + docs/ux-utxo-notes-design.md:

* schema v1→v2 migration on a real file DB (additive, safe re-open, the old
  rows survive, the fresh path stays on the same versioned-init ladder);
* typed accessor CRUD: canonical multi-tag order (§1.4 closed set, lineage
  unions prove multi-tag is the doc's model), note ≤ 500 chars verbatim,
  fail-closed value-free refusals, empty-label-means-clear (§1.3);
* RESCAN SURVIVAL (the crux): ``persist_scan_result``/``replace_utxos_for_wallet``
  rewrite the utxos snapshot only — outpoint-keyed ``coin_labels`` rows
  survive unchanged, and stay after the coin is SPENT (retention: the table
  is never pruned by scans; §1.2 captures post-broadcast facts the user
  keeps);
* lineage-on-broadcast: outputs inherit the UNION of input tag sets, notes
  never inherit, unlabeled inputs write no rows;
* the ``/label`` command matrix (set / clear / list / invalid tag / invalid
  txid / ``last``) — terminal-only, doc §4.3 strings verbatim;
* the one post-broadcast capture hint (§1.2): static code-owned line, sets
  the session's ``last`` target, never repeated for the same tx;
* NEVER-IN-MODEL-CONTEXT (§1.1/§7.10): /label lines never reach the turn
  path; tag/note text appears in no prompt/FACTS/envelope;
* one e2e through the REAL handlers: fund → create → confirm → sign →
  broadcast carries the input's ``kyc`` tag onto the change coin, then
  ``/label last`` relabels it before any rescan.
"""

from __future__ import annotations

import json
import queue
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import localwallet.app as app_module
from localwallet.agent.loop import AgentLoop
from localwallet.app import SendSession, _handle_transcript_command
from localwallet.protocol import IntentName
from localwallet.store import (
    COIN_NOTE_MAX_CHARS,
    COIN_TAGS,
    DIR_OUT,
    Store,
    StoreError,
    StoreIntegrityError,
    TxRecord,
    UtxoRecord,
)

DESCRIPTOR = "wpkh([abcd1234/84'/0'/0']xpub/0/*)"
TXID_A = "a" * 64
TXID_B = "b" * 64
TXID_C = "c" * 64


def _wallet(store: Store) -> int:
    return store.create_wallet("main", DESCRIPTOR).id


def _utxo(wallet_id: int, txid: str, vout: int = 0, value: int = 100_000) -> UtxoRecord:
    return UtxoRecord(
        wallet_id=wallet_id,
        txid=txid,
        vout=vout,
        address="bc1qexample",
        value_sats=value,
        confirmed=1,
        height=900_000,
    )


# ------------------------------------------------------- schema v1 -> v2


def test_v1_db_upgrades_to_v2_on_reopen(tmp_path: Path) -> None:
    """A real v1 file (no coin_labels, user_version=1) migrates additively on
    reopen: version stamped 2, table usable, existing rows intact, and the
    upgrade is stable across further reopens."""
    db = tmp_path / "store.db"
    with Store(db) as store:
        wid = _wallet(store)
        store.set_coin_label(wid, TXID_A, 0, ["kyc"], "v1-era note")
    # Simulate the pre-v2 file exactly: drop the v2 table, stamp version 1.
    raw = sqlite3.connect(db)
    raw.executescript("DROP TABLE coin_labels;\nPRAGMA user_version=1;")
    raw.commit()
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
    raw.close()

    with Store(db) as store:  # the migration runs here
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert store.get_wallet_by_name("main") is not None  # v1 data intact
        # coin_labels recreated EMPTY (a v1 DB never had label rows).
        assert store.get_coin_labels(wid) == []
        store.set_coin_label(wid, TXID_B, 1, ["p2p"], "written after migrate")
    with Store(db) as store:  # idempotent reopen at v2
        assert [r.txid for r in store.get_coin_labels(wid)] == [TXID_B]


def test_migrate_refuses_unrunnable_ladder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing versioned DB whose migration rung is absent is REFUSED
    fail-closed rather than blindly re-created or left half-migrated. We drop
    the 1→2 rung from the ladder and reopen a stamped-v1 file: the migration
    must raise, leave the version at 1, and keep the v1 rows intact (never a
    silent schema rebuild that could clobber user data)."""
    from localwallet.store.db import Store as _Store

    db = tmp_path / "store.db"
    with Store(db) as store:
        store.create_wallet("main", DESCRIPTOR)
    raw = sqlite3.connect(db)
    raw.executescript("DROP TABLE coin_labels;\nPRAGMA user_version=1;")
    raw.commit()
    raw.close()

    monkeypatch.setattr(_Store, "_MIGRATIONS", {}, raising=True)  # no known rung
    with pytest.raises(StoreError):
        Store(db)
    # The refusal left the v1 file untouched at version 1 (no partial migrate).
    raw = sqlite3.connect(db)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
    assert raw.execute("SELECT name FROM wallets").fetchone()[0] == "main"
    raw.close()


# -------------------------------------------------------------- CRUD


def test_multi_tag_canonical_order_and_note_roundtrip() -> None:
    with Store.memory() as store:
        wid = _wallet(store)
        rec = store.set_coin_label(
            wid, TXID_A, 2, ["p2p", "kyc", "p2p"], "Alice's refund"
        )
        assert rec is not None
        # Multi-tag allowed; canonical (§1.4 table) order, deduped.
        assert rec.tags == ("kyc", "p2p")
        assert rec.note == "Alice's refund"  # verbatim
        got = store.get_coin_label(wid, TXID_A, 2)
        assert got == rec
        assert store.get_coin_labels(wid) == [rec]


def test_note_only_row_and_empty_note_clears_field_only() -> None:
    with Store.memory() as store:
        wid = _wallet(store)
        rec = store.set_coin_label(wid, TXID_A, 0, [], "note only")
        assert rec is not None and rec.tags == () and rec.note == "note only"
        rec2 = store.set_coin_label(wid, TXID_A, 0, ["kyc"], "")
        assert rec2 is not None
        assert rec2.tags == ("kyc",) and rec2.note is None


def test_unknown_tag_refused_value_free_and_nothing_stored() -> None:
    with Store.memory() as store:
        wid = _wallet(store)
        with pytest.raises(StoreError) as excinfo:
            store.set_coin_label(wid, TXID_A, 0, ["laundering"])
        assert "laundering" not in str(excinfo.value)
        assert TXID_A not in str(excinfo.value)
        assert store.get_coin_labels(wid) == []  # fail-closed: nothing written


def test_note_over_cap_refused_value_free() -> None:
    with Store.memory() as store:
        wid = _wallet(store)
        secret = "x" * (COIN_NOTE_MAX_CHARS + 1)
        with pytest.raises(StoreError) as excinfo:
            store.set_coin_label(wid, TXID_A, 0, [], secret)
        assert "x" * 10 not in str(excinfo.value)
        assert store.get_coin_labels(wid) == []
        # The cap itself is inclusive.
        ok = store.set_coin_label(wid, TXID_A, 0, [], "y" * COIN_NOTE_MAX_CHARS)
        assert ok is not None and len(ok.note) == COIN_NOTE_MAX_CHARS


def test_malformed_outpoints_refused() -> None:
    with Store.memory() as store:
        wid = _wallet(store)
        for bad_txid in ("z" * 64, "a" * 63, "A" * 64, ""):
            with pytest.raises(StoreError):
                store.set_coin_label(wid, bad_txid, 0, ["kyc"])
        for bad_vout in (-1, "0", True):
            with pytest.raises(StoreError):
                store.set_coin_label(wid, TXID_A, bad_vout, ["kyc"])


def test_clear_removes_row_bare_set_is_a_clear() -> None:
    with Store.memory() as store:
        wid = _wallet(store)
        store.set_coin_label(wid, TXID_A, 0, ["kyc"], "note")
        assert store.set_coin_label(wid, TXID_A, 0) is None  # §1.3: clears
        assert store.get_coin_label(wid, TXID_A, 0) is None
        store.clear_coin_label(wid, TXID_A, 0)  # idempotent


def test_label_requires_existing_wallet() -> None:
    with Store.memory() as store, pytest.raises(StoreIntegrityError):
        store.set_coin_label(999, TXID_A, 0, ["kyc"])


# ------------------------------------------------------ rescan survival


def test_labels_survive_rescan_and_spending() -> None:
    """THE crux pin: scan → tag → rescan (snapshot rewritten without the
    coin = spent) → tag intact. Outpoint-keyed rows live in their own table;
    persist_scan_result never touches it."""
    with Store.memory() as store:
        wid = _wallet(store)
        store.replace_utxos_for_wallet(
            wid, [_utxo(wid, TXID_A), _utxo(wid, TXID_B, 1)]
        )
        store.set_coin_label(wid, TXID_A, 0, ["kyc"], "exchange bounce")
        store.set_coin_label(wid, TXID_B, 1, ["purchase"])

        # A rescan that REWROTE the whole snapshot (TXID_A spent — gone from
        # the unspent set; TXID_B re-confirmed at a new height).
        store.persist_scan_result(
            wid,
            address_rows=[],
            derivation_states=[],
            utxo_snapshot=[_utxo(wid, TXID_B, 1, value=1000)],
            tx_rows=[TxRecord(wid, TXID_C, 900001, None, None, DIR_OUT, None)],
            sync_state_updates={"last_scan_cursor": "x"},
        )
        utxos = store.get_utxos_for_wallet(wid)
        assert [(u.txid, u.vout, u.value_sats) for u in utxos] == [(TXID_B, 1, 1000)]
        # Labels intact — INCLUDING the spent coin's (retention: rows keyed
        # by outpoint stay after the coin dies; §1.2 capture is post-broadcast
        # history the user keeps).
        assert store.get_coin_label(wid, TXID_A, 0).tags == ("kyc",)
        assert store.get_coin_label(wid, TXID_A, 0).note == "exchange bounce"
        assert store.get_coin_label(wid, TXID_B, 1).tags == ("purchase",)


# ------------------------------------------------------------- lineage


def test_lineage_union_multitag_notes_not_inherited() -> None:
    with Store.memory() as store:
        wid = _wallet(store)
        store.set_coin_label(wid, TXID_A, 0, ["kyc"], "my note — never inherited")
        store.set_coin_label(wid, TXID_B, 3, ["p2p"])
        store.propagate_coin_lineage(
            wid, TXID_C, (1,), [(TXID_A, 0), (TXID_B, 3), (TXID_C[:32] + "0" * 32, 0)]
        )
        rec = store.get_coin_label(wid, TXID_C, 1)
        assert rec is not None
        assert rec.tags == ("kyc", "p2p")  # union, canonical order (mixed = both sides)
        assert rec.note is None  # notes are display-only history, never inherited


def test_lineage_unlabeled_inputs_write_no_rows() -> None:
    with Store.memory() as store:
        wid = _wallet(store)
        store.propagate_coin_lineage(wid, TXID_C, (0, 1), [(TXID_A, 0)])
        assert store.get_coin_labels(wid) == []


def test_lineage_merges_existing_output_and_is_idempotent() -> None:
    with Store.memory() as store:
        wid = _wallet(store)
        store.set_coin_label(wid, TXID_A, 0, ["exchange"])
        store.set_coin_label(wid, TXID_C, 1, ["purchase"], "kept")
        store.propagate_coin_lineage(wid, TXID_C, (1,), [(TXID_A, 0)])
        store.propagate_coin_lineage(wid, TXID_C, (1,), [(TXID_A, 0)])  # idempotent
        rec = store.get_coin_label(wid, TXID_C, 1)
        assert rec.tags == ("exchange", "purchase")
        assert rec.note == "kept"


def test_lineage_validation_fails_closed() -> None:
    with Store.memory() as store:
        wid = _wallet(store)
        with pytest.raises(StoreError):
            store.propagate_coin_lineage(wid, "nope", (0,), [(TXID_A, 0)])
        with pytest.raises(StoreError):
            store.propagate_coin_lineage(wid, TXID_C, (0,), [("bad", 0)])
        with pytest.raises(StoreError):
            store.propagate_coin_lineage(wid, TXID_C, (-1,), [])
        assert store.get_coin_labels(wid) == []


# -------------------------------------------------- /label command matrix


def _cmd_store(tmp_path: Path | None = None) -> tuple[Store, int]:
    store = Store(tmp_path / "label.db") if tmp_path else Store.memory()
    wid = _wallet(store)
    store.set_active_wallet(wid)
    return store, wid


def _run_label(
    command: str,
    store: Store | None,
    session: SendSession | None = None,
) -> list[str]:
    out: list[str] = []
    loop = AgentLoop(app_module.stub_generate, {})  # never invoked by the handler
    _handle_transcript_command(command, loop, out.append, session=session, store=store)
    return out


def test_label_set_on_unspent_coin_echoes_doc_line() -> None:
    store, wid = _cmd_store()
    store.replace_utxos_for_wallet(wid, [_utxo(wid, TXID_A)])
    session = SendSession()
    out = _run_label(f"/label {TXID_A} kyc exchange | weekend", store, session)
    joined = "\n".join(out)
    # §4.3 label.set shape: txid verbatim, display strings, note as stored.
    assert f"Noted on transaction {TXID_A}" in joined
    assert "your coin tags: KYC, exchange" in joined
    assert 'your note: "weekend"' in joined
    rec = store.get_coin_label(wid, TXID_A, 0)
    assert rec.tags == ("kyc", "exchange") and rec.note == "weekend"


def test_label_unknown_tag_refusal_lists_valid_set_nothing_stored() -> None:
    store, wid = _cmd_store()
    store.replace_utxos_for_wallet(wid, [_utxo(wid, TXID_A)])
    out = _run_label(f"/label {TXID_A} laundering", store)
    assert out == [
        (
            "I don't know that label. Known ones: kyc, exchange, p2p, purchase, "
            'consolidation — or type your own words after "|" for a note.'
        )
    ]
    assert store.get_coin_labels(wid) == []


def test_label_note_cap_refused() -> None:
    store, wid = _cmd_store()
    store.replace_utxos_for_wallet(wid, [_utxo(wid, TXID_A)])
    out = _run_label(f"/label {TXID_A} kyc | " + "x" * (COIN_NOTE_MAX_CHARS + 1), store)
    assert len(out) == 1 and "too long" in out[0] and "500" in out[0]
    assert store.get_coin_labels(wid) == []


def test_label_bare_target_clears() -> None:
    store, wid = _cmd_store()
    store.replace_utxos_for_wallet(wid, [_utxo(wid, TXID_A)])
    store.set_coin_label(wid, TXID_A, 0, ["p2p"], "note")
    out = _run_label(f"/label {TXID_A}", store)
    assert out == ["Cleared your note for that transaction's coins."]
    assert store.get_coin_label(wid, TXID_A, 0) is None


def test_label_lists_unspent_coins_with_unlabeled_fallback() -> None:
    store, wid = _cmd_store()
    store.replace_utxos_for_wallet(wid, [_utxo(wid, TXID_A), _utxo(wid, TXID_B, 1)])
    store.set_coin_label(wid, TXID_B, 1, ["purchase"], "bike")
    out = _run_label("/label", store)
    joined = "\n".join(out)
    assert "YOU marked" in joined  # §9 honesty frame — never "this is KYC"
    assert f"{TXID_A}:0" in joined and "(unlabeled)" in joined
    assert f"{TXID_B}:1" in joined and 'your note: "bike"' in joined
    assert "purchase" in joined


def test_label_last_and_validation_matrix() -> None:
    store, _wid = _cmd_store()
    # last with nothing broadcast this session → value-free doc line.
    assert _run_label("/label last kyc", store, SendSession()) == [
        'Nothing to label yet — "last" is the most recent payment you\'ve sent.'
    ]
    # malformed txid refused; a tag word with no target gets the usage line.
    assert _run_label("/label deadbeef kyc", store) == [
        'That doesn\'t look like a transaction id — 64 hex characters, or use "last".'
    ]
    assert any("Usage: /label" in line for line in _run_label("/label kyc", store))
    # no store / no active wallet → plain refusals, never a crash.
    assert _run_label("/label", None) == ["Labels are unavailable — no wallet store is open."]
    empty = Store.memory()
    try:
        assert _run_label("/label", empty) == ["No wallet is open yet — nothing to label."]
    finally:
        empty.close()


def test_label_target_resolution_covers_label_rows_before_rescan() -> None:
    """/label <txid> resolves coins from the utxo snapshot OR from recorded
    label rows (a just-broadcast change coin is labelable before any scan)."""
    store, wid = _cmd_store()
    store.set_coin_label(wid, TXID_C, 1, ["consolidation"])  # no utxo row needed
    out = _run_label(f"/label {TXID_C} purchase", store)
    assert any("Noted on transaction" in line for line in out)
    assert store.get_coin_label(wid, TXID_C, 1).tags == ("purchase",)
    # An unknown txid (no coin anywhere) says so value-free.
    assert _run_label(f"/label {'9' * 64} kyc", store) == [
        "Nothing to label for that transaction yet — its coins show up after a scan."
    ]


# ------------------------------------------------- capture hint (§1.2)


def test_broadcast_hint_prints_once_and_sets_last() -> None:
    session = SendSession()
    out: list[str] = []
    result = {"status": "broadcast", "txid": TXID_C}
    app_module._print_broadcast_tx(result, out.append, session=session)
    assert any(line.startswith("Sent!") for line in out)
    assert out[-1] == 'Want to remember what this was? Type /label last [tag] ["note"]'
    assert session.last_broadcast_txid == TXID_C
    # Never repeated within the session for the SAME tx; a later tx gets it.
    out2: list[str] = []
    app_module._print_broadcast_tx(result, out2.append, session=session)
    assert not any("Want to remember" in line for line in out2)
    out3: list[str] = []
    app_module._print_broadcast_tx(
        {"status": "broadcast", "txid": TXID_A}, out3.append, session=session
    )
    assert any("Want to remember" in line for line in out3)
    # Failure paths print no hint and move `last` nowhere.
    out4: list[str] = []
    app_module._print_broadcast_tx(
        {"error": "broadcast_failed", "detail": "x"}, out4.append, session=session
    )
    assert not any("Want to remember" in line for line in out4)
    assert session.last_broadcast_txid == TXID_A


# ------------------------------------------- never-in-model-context pin


def test_label_never_reaches_the_model_or_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§1.1 / §7.10 closed: /label rides the transcript channel ONLY.

    Drives the real pump: one /label command carrying a distinctive note and
    tags, then one genuine chat turn. The model (recording generate_fn) must
    see exactly ONE turn ("hi") and no label text anywhere in its prompt —
    no FACTS injection, no envelope, no transcript entry.
    """
    prompts: list[str] = []

    def recording_generate(prompt: str, grammar: str | None) -> str:
        prompts.append(prompt)
        return '{"v": 0, "intent": "respond", "params": {"text": "ok"}}'

    store, wid = _cmd_store()
    store.replace_utxos_for_wallet(wid, [_utxo(wid, TXID_A)])
    loop = AgentLoop(
        recording_generate,
        {
            IntentName.RESPOND: app_module._respond_handler,
            IntentName.CLARIFY: app_module._clarify_handler,
        },
    )
    commands: queue.Queue[Any] = queue.Queue()
    for line in (
        f"/label {TXID_A} kyc | alice refund zebra",
        "hi",
        app_module.QUIT,
    ):
        commands.put(line)
    outputs: list[str] = []
    app_module._pump(
        loop,
        outputs.append,
        commands,
        flow=app_module.TxFlow(),
        session=SendSession(),
        table={IntentName.RESPOND: app_module._respond_handler},
        store=store,
    )
    # The label was written (command worked)…
    assert store.get_coin_label(wid, TXID_A, 0).tags == ("kyc",)
    # …the model ran exactly once, for "hi", and never saw label material.
    assert len(prompts) == 1
    assert "alice refund zebra" not in prompts[0]
    assert TXID_A not in prompts[0]
    assert "kyc" not in prompts[0]
    # No transcript/envelope for the /label line: history carries ONE turn.
    assert len(loop.history) == 1


# ---------------------------------------------- e2e through the handlers


def test_broadcast_lineage_then_label_last_before_rescan(
    tmp_path: Path,
) -> None:
    """Fund → create → confirm → sign → broadcast: the change coin inherits
    the spent coin's kyc tag (no rescan happened), the renderer hint arms
    ``last``, and ``/label last`` relabels the pre-scan change coin."""
    from tests.test_e2e_skeleton import (
        SEND_RECIPIENT,
        SEND_UTXO,
        _build_send_table,
        _extract_signed_tx,
        _send_chain_handler,
        derive_fixture_addresses,
    )
    from tests.test_phase3_ac import _ScriptedSignerOverride, _validate

    addr0 = derive_fixture_addresses(1)[0]
    state: dict = {}
    signer = _ScriptedSignerOverride(script=[False])
    table, store, wallet, client, _recorded, _flow, session = _build_send_table(
        lambda rec: _send_chain_handler(
            rec, utxos_by_addr={addr0: [SEND_UTXO]}, state=state
        ),
        signer_selection=app_module.SignerSelection(
            kind="hwi",
            dir_path=tmp_path / "transfer",
            fingerprint_hex=_fixture_fingerprint(),
        ),
        signer=signer,
    )
    try:
        # Tag the funding coin BEFORE spending it (§1.2: facts about coins).
        fund_txid = SEND_UTXO["txid"]
        store.set_coin_label(wallet.id, fund_txid, SEND_UTXO["vout"], ["kyc"], "from exchange")

        created = table[IntentName.CREATE_TX](
            _validate(
                json.dumps(
                    {
                        "v": 0,
                        "intent": "create_tx",
                        "params": {
                            "recipient": SEND_RECIPIENT,
                            "amount_sats": 60_000,
                        },
                    }
                )
            )
        )
        tx_ref = created["tx_ref"]
        session.gate_decision = app_module.GateDecision.CONFIRM
        table[IntentName.CONFIRM_TX](
            _validate(json.dumps({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": tx_ref}}))
        )
        table[IntentName.SIGN_TX](
            _validate(json.dumps({"v": 0, "intent": "sign_tx", "params": {"tx_ref": tx_ref}}))
        )
        broadcast = table[IntentName.BROADCAST_TX](
            _validate(
                json.dumps({"v": 0, "intent": "broadcast_tx", "params": {"tx_ref": tx_ref}})
            )
        )
        assert broadcast["status"] == "broadcast"
        txid = _extract_signed_tx(signer.signed_psbts[0]).txid().hex()
        assert broadcast["txid"] == txid
        assert broadcast.get("store_warning") is None  # lineage ran cleanly

        # Lineage: the change coin (LAST output, vout 1 in the fixture send)
        # carries the funding coin's tag — union only, note NOT inherited —
        # with NO rescan involved.
        change = store.get_coin_label(wallet.id, txid, 1)
        assert change is not None
        assert change.tags == ("kyc",)
        assert change.note is None

        # Narration arms the capture moment: hint once, `last` set.
        out: list[str] = []
        app_module._print_broadcast_tx(broadcast, out.append, session=session)
        assert "Want to remember what this was?" in out[-1]
        assert session.last_broadcast_txid == txid

        # /label last works BEFORE any rescan (target = the lineage row).
        _handle_transcript_command(
            "/label last consolidation | weekend tidy-up",
            AgentLoop(app_module.stub_generate, {}),
            out.append,
            session=session,
            store=store,
        )
        relabeled = store.get_coin_label(wallet.id, txid, 1)
        assert relabeled.tags == ("consolidation",)  # bare re-label REPLACES
        assert relabeled.note == "weekend tidy-up"
    finally:
        client.close()
        store.close()


def _fixture_fingerprint() -> str:
    from tests.test_e2e_skeleton import _fixture_parsed

    return _fixture_parsed().hd_key.my_fingerprint.hex()


# ------------------------------------------- CLI wiring pin (store → pump)


def test_repl_passes_store_so_label_works_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The CLI ``_repl`` → ``_pump`` → transcript-handler chain carries the
    engine store, so ``/label`` works in a real REPL session: label the
    funded coin mid-session, list it back tagged, then relabel the
    just-broadcast change coin via ``last`` (lineage row, pre-rescan)."""
    from tests.test_e2e_skeleton import (
        SEND_RECIPIENT,
        SEND_UTXO,
        _extract_signed_tx,
        _run_send_repl,
        _send_chain_handler,
        _store_path,
        derive_fixture_addresses,
    )
    from tests.test_phase3_ac import _device_sign_before_line

    addr0 = derive_fixture_addresses(1)[0]
    transfer = tmp_path / "transfer"
    state: dict = {}
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]}, state=state)

    labeled = {"done": False}
    device_hook = _device_sign_before_line(transfer)

    def before_line() -> None:
        device_hook()
        if not labeled["done"]:
            labeled["done"] = True
            # A second WAL connection (the feeder-thread seam): label the
            # funding coin while the engine holds its own store open.
            with Store(_store_path(tmp_path)) as side:
                active = side.get_active_wallet()
                assert active is not None
                side.set_coin_label(active.id, SEND_UTXO["txid"], SEND_UTXO["vout"], ["kyc"])

    code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            "yes please",  # confirm + chained export
            "sign it",  # import → revalidate → SIGNED
            "broadcast it",
            "/label",  # list: the funded coin shows the KYC claim
            "/label last purchase | coffee",  # relabel the change coin
            "exit",
        ],
        ["create", "confirm", "sign", "broadcast"],
        extra_env={"LOCALWALLET_SIGNER_DIR": str(transfer)},
        before_line=before_line,
    )
    assert code == 0
    joined = "\n".join(outputs)
    assert "Want to remember what this was? Type /label last" in joined
    assert joined.count("Want to remember") == 1  # hint once, terminal state
    assert "YOU marked" in joined and "KYC" in joined  # the list line
    assert "Noted on transaction" in joined and 'your note: "coffee"' in joined
    # The lineage row was written at broadcast, then replaced by /label last.
    txid = _extract_signed_tx(flow.signed.psbt_base64).txid().hex()
    with Store(_store_path(tmp_path)) as store:
        active = store.get_active_wallet()
        rec = store.get_coin_label(active.id, txid, 1)
        assert rec is not None and rec.tags == ("purchase",)
        assert rec.note == "coffee"


def test_transcript_help_lists_label() -> None:
    out = _run_label("/help", None)
    assert len(out) == 1 and "/label" in out[0]


def test_label_closed_set_matches_doc() -> None:
    assert COIN_TAGS == ("kyc", "exchange", "p2p", "purchase", "consolidation")

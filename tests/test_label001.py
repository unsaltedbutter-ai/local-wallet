"""TCK-LABEL-001 — chat label-by-address: schema v5 address labels + the
deterministic pre-model intercept (the live honest-response bug).

The bug (user, 2026-09-12, verbatim): "label bc1qpvnux8s9tm6w2l2nssj7wthatf9hm926tu66zl
as 'KYC'" → the narration claimed the label was set, but the store held
NOTHING. The utterance had no route at all: ``coin_labels`` is OUTPOINT-keyed
(txid:vout), so an ADDRESS label had nowhere to land — and the model's
success-shaped ``respond`` (or its "I cannot perform that action") was the
whole story. Reproduced first: with no deterministic intercept the line
reaches the model verbatim and a fabrication narrates cleanly with zero
store writes (see TestTheRepro).

The fix under test, both halves:

* store: schema v5 ``address_labels`` (address-keyed, value-checked ≤500
  chars, created/updated timestamps) — the RBF-001/CHAT-001 migration
  pattern (versioned, additive, idempotent, fail-closed), typed accessors
  ``set_address_label`` / ``get_address_label`` / ``get_address_labels``
  as the only writers, and the table OUTSIDE the scan write-set (a rescan
  can never clear or fabricate a label) — the foundation TCK-CHAT-003
  builds on;
* app: a deterministic intercept BEFORE the model for the two
  address-literal phrasings ("label <addr> as <label>" / "label <addr>
  <label>"), strict mainnet-bech32 shape gate, quoted-or-trailing value
  gates, value-free refusals that still CONSUME the turn, and
  NARRATION = STORE TRUTH (a success line exists iff a committed row
  exists; the ack echoes the stored label verbatim and names the surface
  address-level — the coin-level /label command ships unchanged).
  Labels never enter model context (§7.10): the consumed turn never
  reaches the transcript or the next prompt.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Final

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.protocol import IntentName
from localwallet.store import (
    ADDRESS_LABEL_MAX_CHARS,
    SCHEMA_VERSION,
    AddressRecord,
    Store,
    StoreError,
    UtxoRecord,
)
from localwallet.tx.flow import TxFlow

# The user's LIVE address and utterance from the bug report.
ADDR: Final[str] = "bc1qpvnux8s9tm6w2l2nssj7wthatf9hm926tu66zl"
BUG_LINE: Final[str] = f"label {ADDR} as 'KYC'"
OTHER_ADDR: Final[str] = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
TXID_A: Final[str] = "a" * 64


# --------------------------------------------------------------- turn harness


class _RecordingGen:
    """Scripted fake model: records every prompt, answers with a harmless
    respond. Any appearance of the label line in ``prompts`` means the
    intercept MISSER (or worse, narrated a model fabrication)."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, prompt: str, grammar_text: str | None) -> str:
        del grammar_text
        self.prompts.append(prompt)
        return json.dumps({"v": 0, "intent": "respond", "params": {"text": "ok."}})


def _world() -> tuple[Store, int, dict[str, Any]]:
    """Store + active wallet + a table that only answers ``respond`` (the
    label intercept must never need a handler; a miss rides the fake gen)."""
    store = Store.memory()
    wallet = store.create_wallet("main", "desc")
    store.set_active_wallet(wallet.id)
    table: dict[str, Any] = {IntentName.RESPOND: lambda env: {"text": env.params.text}}
    return store, wallet.id, table


def _turn(
    store: Store, table: dict[str, Any], line: str, gen: _RecordingGen | None = None
) -> tuple[list[str], _RecordingGen]:
    """One REAL REPL turn through ``_run_turn`` with the production store."""
    gen = gen if gen is not None else _RecordingGen()
    loop = AgentLoop(gen, table)
    out: list[str] = []
    app._run_turn(
        loop, TxFlow(), app.SendSession(), line, out.append, table=table, store=store
    )
    return out, gen


# =========================================================================
# 0. The repro (documents the bug the ticket names; passes before AND after —
#    before because the line reaches the model and a fabricated ack narrates
#    with zero writes, after because the intercept consumes what the OLD path
#    leaked. The post-fix state is pinned in sections 2–4.)
# =========================================================================


class TestTheRepro:
    def test_live_bug_line_writes_address_label_and_consumes_the_model(self) -> None:
        """The user's verbatim utterance: AFTER the fix a committed row
        exists and the model was never asked. (Pre-fix the same call left
        ``get_address_label`` None while the scripted model's "has been
        labeled as 'KYC'" respond printed as fact — the honest-response
        violation, reproduced against the pre-fix tree during this ticket
        and captured in the module docstring.)"""
        store, _wid, table = _world()
        try:
            out, gen = _turn(store, table, BUG_LINE)
            rec = store.get_address_label(ADDR)
            assert rec is not None and rec.label == "KYC"
            assert gen.prompts == []  # the model never saw the utterance
            assert any(ADDR in line and "KYC" in line for line in out)
        finally:
            store.close()

    def test_no_fabrication_path_without_the_store(self) -> None:
        """A miss-shaped line (no address literal) still reaches the model
        unchanged — the ticket's documented, REPORTED gap (an honest route
        there needs a prompt line, out of scope here); the intercept must
        not silently swallow it with a false ack of its own."""
        store, _wid, table = _world()
        try:
            _out, gen = _turn(store, table, "label my strike address as KYC")
            assert len(gen.prompts) == 1
            assert store.get_address_labels() == []
        finally:
            store.close()


# =========================================================================
# 1. Store: schema v5 migration (the RBF-001 / CHAT-001 pattern)
# =========================================================================


class TestSchemaV5Migration:
    def _as_v4_file(self, db: Path) -> None:
        raw = sqlite3.connect(db)
        raw.execute("DROP TABLE address_labels")
        raw.execute("PRAGMA user_version=4")
        raw.commit()
        raw.close()

    def test_v4_file_upgrades_on_reopen(self, tmp_path: Path) -> None:
        db = tmp_path / "store.db"
        with Store(db) as store:
            wallet = store.create_wallet("main", "d")
            store.note_address_shown(wallet.id, "bc1qa", shown_at=1)  # v4 surface
        self._as_v4_file(db)
        with Store(db) as store:
            assert (
                store._conn.execute("PRAGMA user_version").fetchone()[0]
                == SCHEMA_VERSION
                == 5
            )
            assert store.get_address_labels() == []  # empty, never fabricated
            rec = store.set_address_label("bc1qa", "KYC")
            assert rec.label == "KYC"
        with Store(db) as store:  # stable reopen, no re-run
            assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 5
            assert store.get_address_label("bc1qa").label == "KYC"
            # the v4 registry survived the v5 rung untouched
            assert store.get_address_by_number(1, 1).address == "bc1qa"

    def test_fresh_create_is_v5(self, tmp_path: Path) -> None:
        with Store(tmp_path / "fresh.db") as store:
            assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 5

    def test_migration_idempotent_half_applied(self, tmp_path: Path) -> None:
        """Crash-mid-migration shape (v4 stamp, table ALREADY created): the
        CREATE TABLE IF NOT EXISTS step is a no-op and the stamp completes."""
        db = tmp_path / "store.db"
        with Store(db):
            pass
        raw = sqlite3.connect(db)
        raw.execute("PRAGMA user_version=4")
        raw.commit()
        raw.close()
        with Store(db) as store:
            assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 5

    def test_newer_schema_fails_closed(self, tmp_path: Path) -> None:
        """A DB stamped ABOVE this build is refused, never opened (no
        down-migration exists) — the blanket migration contract."""
        db = tmp_path / "store.db"
        with Store(db):
            pass
        raw = sqlite3.connect(db)
        raw.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        raw.commit()
        raw.close()
        with pytest.raises(Exception) as excinfo:
            Store(db)
        assert "newer" in str(excinfo.value)

    def test_scan_persist_never_touches_address_labels(self) -> None:
        """The label is a USER fact: the scan's composite write (which owns
        addresses/derivation/utxos/txs/sync_state) leaves address_labels
        alone — a rescan can never clear, re-assign, or fabricate one
        (the CHAT-001 registry lesson, same write-set discipline)."""
        store = Store.memory()
        wallet = store.create_wallet("w", "d")
        store.set_address_label(ADDR, "KYC")
        store.persist_scan_result(
            wallet.id,
            address_rows=[AddressRecord(wallet.id, 0, 0, ADDR, "p2wpkh", "used")],
            derivation_states=[],
            utxo_snapshot=[UtxoRecord(wallet.id, TXID_A, 0, ADDR, 50_000, 1, 800_000)],
            tx_rows=[],
            sync_state_updates={"last_scan_height": "800000"},
        )
        # a second scan sees NOTHING at the address (snapshot replace):
        store.persist_scan_result(
            wallet.id,
            address_rows=[],
            derivation_states=[],
            utxo_snapshot=[],
            tx_rows=[],
            sync_state_updates={},
        )
        assert store.get_address_label(ADDR).label == "KYC"
        store.close()


# =========================================================================
# 2. Store: typed accessors (set / get / get-s)
# =========================================================================


class TestAddressLabelAccessors:
    def test_set_get_roundtrip_verbatim(self) -> None:
        store = Store.memory()
        rec = store.set_address_label(ADDR, "strike payout")
        assert rec.address == ADDR
        assert rec.label == "strike payout"  # stored verbatim
        got = store.get_address_label(ADDR)
        assert got == rec
        # timestamps: ISO-8601 UTC, created written with the row
        datetime.fromisoformat(rec.created_at)
        datetime.fromisoformat(rec.updated_at)
        store.close()

    def test_relable_replaces_and_keeps_created_at(self) -> None:
        store = Store.memory()
        first = store.set_address_label(ADDR, "KYC")
        second = store.set_address_label(ADDR, "KYC-free")
        assert second.label == "KYC-free"
        assert second.created_at == first.created_at  # the first date survives
        assert second.updated_at >= first.updated_at
        assert len(store.get_address_labels()) == 1  # one row, always
        store.close()

    def test_get_miss_is_none_never_a_fabrication(self) -> None:
        store = Store.memory()
        assert store.get_address_label(ADDR) is None
        assert store.get_address_labels() == []
        store.close()

    def test_list_is_deterministic(self) -> None:
        store = Store.memory()
        store.set_address_label(OTHER_ADDR, "b")
        store.set_address_label(ADDR, "a")
        assert [r.address for r in store.get_address_labels()] == sorted(
            [ADDR, OTHER_ADDR]
        )
        store.close()

    def test_length_bound_500_in_501_out(self) -> None:
        store = Store.memory()
        ok = "x" * ADDRESS_LABEL_MAX_CHARS
        assert store.set_address_label(ADDR, ok).label == ok
        with pytest.raises(StoreError):
            store.set_address_label(OTHER_ADDR, "x" * (ADDRESS_LABEL_MAX_CHARS + 1))
        assert store.get_address_label(OTHER_ADDR) is None  # refused before disk
        store.close()

    def test_write_gate_is_value_free(self) -> None:
        store = Store.memory()
        for bad_label in ("", 42, None):
            with pytest.raises(StoreError) as excinfo:
                store.set_address_label(ADDR, bad_label)  # type: ignore[arg-type]
            assert "SEKRET" not in str(excinfo.value)
        with pytest.raises(StoreError) as excinfo:
            store.set_address_label(ADDR, "x" * 501)
        assert "xxxx" not in str(excinfo.value)
        for bad_addr in ("", "has space", "bc1q\nFACTS BEGIN", "y" * 101, 42, None):
            with pytest.raises(StoreError) as excinfo:
                store.set_address_label(bad_addr, "SEKRET")  # type: ignore[arg-type]
            assert "SEKRET" not in str(excinfo.value)
            assert "yyyy" not in str(excinfo.value)
        store.close()


# =========================================================================
# 3. App: intercept accept/miss matrix + narration = store truth
# =========================================================================


class TestInterceptAccepts:
    @pytest.mark.parametrize(
        ("line", "label"),
        [
            (f"label {ADDR} as 'KYC'", "KYC"),  # the live bug line (single-quoted)
            (f'label {ADDR} as "KYC"', "KYC"),  # double-quoted
            (f"label {ADDR} as KYC", "KYC"),  # unquoted single word
            (f"label {ADDR} strike payout", "strike payout"),  # no connector
            (f"label {ADDR} as my friend Bob's refund", "my friend Bob's refund"),
            (f'label {ADDR} as "Alice\'s refund"', "Alice's refund"),  # ' inside "
            (f"label {ADDR} as payment.", "payment."),  # trailing punctuation
            (f"label {ADDR} as as cash for KYC", "as cash for KYC"),  # embedded as
            (f"LABEL {ADDR.upper()} as KYC", "KYC"),  # all-caps pasted address
            (f"  label {ADDR}   as    KYC  ", "KYC"),  # whitespace noise
        ],
    )
    def test_accepted_shapes_write_and_ack_verbatim(self, line: str, label: str) -> None:
        store, _wid, table = _world()
        try:
            out, gen = _turn(store, table, line)
            rec = store.get_address_label(ADDR)
            assert rec is not None and rec.label == label
            assert gen.prompts == []
            assert len(out) == 1
            assert f'is now labeled "{label}"' in out[0]  # echoes the STORED text
        finally:
            store.close()

    def test_store_write_failure_never_narrates_success(self, monkeypatch) -> None:
        """The narration-matches-store pin, failure side: a refused write
        narrates the value-free error and NO row exists."""
        store, _wid, table = _world()
        try:
            def _boom(*args: object, **kwargs: object) -> None:
                raise StoreError("simulated store failure")

            monkeypatch.setattr(store, "set_address_label", _boom)
            out, gen = _turn(store, table, f"label {ADDR} as KYC")
            assert store.get_address_label(ADDR) is None
            assert gen.prompts == []
            assert len(out) == 1
            assert "couldn't save" in out[0]
            assert "labeled" not in out[0]
        finally:
            store.close()


class TestInterceptRefusals:
    """Shape matched (verb + address literal) but the VALUE is bad: the turn
    is CONSUMED with a value-free refusal — never stored, never the model."""

    @pytest.mark.parametrize(
        "line",
        [
            f"label {ADDR}",  # no value at all
            f"label {ADDR} as",  # connector only
            f"label {ADDR} as ''",  # empty quoted
            f'label {ADDR} as ""',
            f"label {ADDR} as {'x' * (ADDRESS_LABEL_MAX_CHARS + 1)}",  # overflow
            f'label {ADDR} as "unterminated',  # unbalanced quote
            f'label {ADDR} as "KYC" now',  # trailing text after the close
            f"label {ADDR} as bell\x07hop",  # non-printable garbage
        ],
    )
    def test_refused_shapes_store_nothing_and_echo_nothing(self, line: str) -> None:
        store, _wid, table = _world()
        try:
            out, gen = _turn(store, table, line)
            assert store.get_address_labels() == []  # NOTHING stored
            assert gen.prompts == []  # the model never sees it either
            assert len(out) == 1
            assert ADDR not in out[0]  # value-free refusal
            assert "KYC" not in out[0]
            assert "xxxx" not in out[0]
        finally:
            store.close()


class TestInterceptMisses:
    """Not the address-literal grammar: fall through to the UNCHANGED
    pipeline (the model path — the documented, reported gap for
    non-address-literal phrasings)."""

    @pytest.mark.parametrize(
        "line",
        [
            "label my strike address as KYC",  # no address literal
            f"label {TXID_A} as KYC",  # txid ≠ address (coin surface = /label)
            "label hello as KYC",  # word token
            "label 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa as KYC",  # base58 legacy
            "label tb1qws6w067xn7f88t5aaqgs9vxhq5hvfhkz6dz25g as KYC",  # testnet
            f"label {ADDR[:-1]}x as KYC",  # bad checksum
            f"label Bc1q{ADDR[5:]} as KYC",  # mixed case (invalid bech32)
            "label",  # bare verb
            "name bc1qxx as KYC",  # different verb
            "unlabel bc1qxx",  # not this grammar
            "what is my balance?",  # ordinary chat
        ],
    )
    def test_misses_reach_the_model_unchanged(self, line: str) -> None:
        store, _wid, table = _world()
        try:
            _out, gen = _turn(store, table, line)
            assert len(gen.prompts) == 1
            assert store.get_address_labels() == []
        finally:
            store.close()


# =========================================================================
# 4. App: honesty surfaces
# =========================================================================


class TestNarrationHonesty:
    def test_row_exists_iff_success_narrated_matrix(self) -> None:
        """The ticket's pin, both directions, over a mixed script: every
        narrated success corresponds to a committed row echoing that ack
        verbatim (the failure side — refused write, no row, no ack — is
        pinned separately), and nothing the script narrated outside those
        acks stored a thing."""
        store, _wid, table = _world()
        try:
            acked_pairs: list[tuple[str, str]] = []
            for line in (
                f"label {ADDR} as KYC",  # write
                f"label {ADDR} as",  # refuse
                f'label {ADDR} as "strike payout"',  # relabel
                f"label {ADDR} as {'x' * 501}",  # overflow refuse
                f"labels {ADDR} as nope",  # miss (verb shape) → model
                f"label {OTHER_ADDR} as p2p refund",  # write (two words)
            ):
                out, _gen = _turn(store, table, line)
                for o in out:
                    if "is now labeled" in o:
                        addr = o.split()[1]
                        label = o.split('is now labeled "')[1].rsplit('" —', 1)[0]
                        acked_pairs.append((addr, label))
                        # success ⟹ the committed row is exactly what was said
                        assert store.get_address_label(addr).label == label
            assert acked_pairs == [
                (ADDR, "KYC"),
                (ADDR, "strike payout"),  # relabel: ack again, row replaced
                (OTHER_ADDR, "p2p refund"),
            ]
            rows = store.get_address_labels()
            assert {r.address for r in rows} == {ADDR, OTHER_ADDR}  # no phantom rows
            assert store.get_address_label(ADDR).label == "strike payout"
            assert store.get_address_label(OTHER_ADDR).label == "p2p refund"
        finally:
            store.close()

    def test_ack_names_surfaces_distinctly_and_lists_no_coins(self) -> None:
        """Address-level vs coin-level named distinctly; WITH the address's
        coins known (scan data), the narration still says the label applies
        to the ADDRESS and lists NOTHING else — no outpoints, no sats."""
        store, wid, table = _world()
        try:
            store.replace_utxos_for_wallet(
                wid, [UtxoRecord(wid, TXID_A, 0, ADDR, 123_456, 1, None)]
            )
            out, _gen = _turn(store, table, f"label {ADDR} as KYC")
            assert len(out) == 1
            line = out[0]
            assert ADDR in line  # the address itself, verbatim
            assert "address-level" in line
            assert "/label" in line  # the coin-level surface, distinctly named
            assert TXID_A not in line  # no coin listing…
            assert "123,456" not in line and "123456" not in line  # …no amounts
            assert "sats" not in line
        finally:
            store.close()

    def test_consumed_turn_never_reaches_the_model(self) -> None:
        """§7.10 pin (transcript-free): after a consumed label turn, the
        NEXT ordinary turn's prompt carries neither the label text nor the
        address — the consumed line never entered the history."""
        store, _wid, table = _world()
        try:
            _out, gen1 = _turn(store, table, f"label {ADDR} as topsecret-label")
            assert gen1.prompts == []
            out2, gen2 = _turn(store, table, "what is 2+2?")
            assert gen2.prompts  # the follow-up DID reach the model
            joined = "\n".join(gen2.prompts)
            assert "topsecret-label" not in joined
            assert ADDR not in joined
            assert out2  # and it narrated normally
        finally:
            store.close()

    def test_intercept_never_touches_coin_labels(self) -> None:
        """The address surface writes NOTHING to the coin surface (the two
        coexist; coin_labels keeps its own outpoint-keyed life)."""
        store, wid, table = _world()
        try:
            _out, _gen = _turn(store, table, f"label {ADDR} as KYC")
            assert store.get_coin_labels(wid) == []
        finally:
            store.close()

    def test_coin_level_label_command_unchanged_regression(self) -> None:
        """The existing terminal /label (coin-level, txid-keyed) ships
        UNCHANGED: it still lists and sets coin tags, and an address label
        never leaks into its output or its storage."""
        store, wid, table = _world()
        try:
            _out, _gen = _turn(store, table, f"label {ADDR} as KYC")
            store.replace_utxos_for_wallet(
                wid, [UtxoRecord(wid, TXID_A, 0, ADDR, 50_000, 1, None)]
            )
            out: list[str] = []
            app._handle_label_command(TXID_A + " kyc", store, app.SendSession(), out.append)
            assert any("your coin tags: KYC" in line for line in out)
            rec = store.get_coin_label(wid, TXID_A, 0)
            assert rec is not None and rec.tags == ("kyc",)
            # bare /label list: shows the COIN, never the address-label row
            listing: list[str] = []
            app._handle_label_command("", store, app.SendSession(), listing.append)
            assert any(TXID_A in line for line in listing)
            assert not any("address-level" in line for line in listing)
        finally:
            store.close()

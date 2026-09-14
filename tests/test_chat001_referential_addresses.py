"""TCK-CHAT-001 — store+app+prompt referential addresses.

Every address the app SHOWS the user carries a STABLE wallet-lifetime
number from the store registry (schema v4) + a first-shown timestamp, and
numbered referents ("show address 3", "balance of #3", "the coins on
address 2") resolve ENGINE-side with the full address restated. One test
per done-when criterion of the ticket:

* stable numbering across lists and sessions (same address = same number
  forever; new addresses get fresh numbers; first-shown recorded once);
* numbered-list shape (honest header: what "used" means + the last-scan
  freshness bound; rows with used/not-used-yet + the honest empty label
  slot + verbatim values; the one-time referent hint; the single
  FAQ-style line about shown dates);
* referent resolution ALWAYS restates the full address (number-only
  answers are the bug pinned here) and bound-checks out-of-range numbers
  into the value-free clarify — never a guess;
* FACTS injection shape (registry as routing help: numbers + addresses +
  used-state, never fabrication; bounded injection honestly flagged);
* prompt/grammar/schema LOCKSTEP drift pins (registry 15; address_number
  is a NUMBER carrier — no address is representable in any of the three
  widened params shapes);
* value-free discipline throughout (no address/amount ever reaches an
  error or the out-of-range line; the registry gate refuses malformed
  writes without echoing them).

Offline, deterministic: in-memory/tmp stores, the fixture watch key, no
network, clock injected where the store stamps timestamps.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop, AgentTurnResult, AgentTurnStatus
from localwallet.chain import IncomingEvent
from localwallet.protocol import (
    Envelope,
    GetAddressesParams,
    GetBalanceParams,
    GetUtxosParams,
    IntentName,
    NewAddressParams,
    validate_payload,
)
from localwallet.store import (
    ADDRESS_USED,
    SCHEMA_VERSION,
    AddressRecord,
    Store,
    StoreError,
    StoreIntegrityError,
    UtxoRecord,
)
from localwallet.wallet.descriptor import WalletDescriptor
from tests.test_e2e_skeleton import ZPUB, derive_fixture_addresses

ADDRS: Final[list[str]] = derive_fixture_addresses(6)


# ------------------------------------------------------------------ helpers


def _env(intent: IntentName, params: dict[str, Any]) -> Envelope:
    return validate_payload(json.dumps({"v": 0, "intent": intent.value, "params": params}))


def _world() -> tuple[Store, Any, dict]:
    """In-memory store + fixture wallet + the PRODUCTION dispatch table
    (client=None: every intent exercised here is a store read/write or a
    pure derivation — nothing here may need the chain)."""
    store = Store.memory()
    wd = WalletDescriptor.from_key(ZPUB)
    wallet = store.create_wallet("default", wd.descriptor)
    store.set_active_wallet(wallet.id)
    table = app.build_dispatch_table(store, wallet, wd.parsed, client=None, scan_fn=lambda: None)
    return store, wallet, table


def _say(world: tuple[Store, Any, dict], intent: IntentName, params: dict[str, Any]) -> tuple[dict, list[str]]:
    """Dispatch one envelope through the handler AND the production
    narration; return (result, printed lines)."""
    _store, _wallet, table = world
    env = _env(intent, params)
    result = table[intent](env)
    out: list[str] = []
    app._print_turn(
        AgentTurnResult(
            status=AgentTurnStatus.OK,
            envelope=env,
            result=result,
            user_message=None,
            turns_used=0,
        ),
        out.append,
    )
    return result, out


def _plant(store: Store, wallet_id: int, index: int, *, status: str, coin: int | None = None, txid: str | None = None) -> None:
    """One derived address row (+ optional coin) the way a scan writes them."""
    store.upsert_batch(
        [AddressRecord(wallet_id, 0, index, ADDRS[index], "p2wpkh", status)]
    )
    if coin is not None:
        rows = store.get_utxos_for_wallet(wallet_id)
        rows.append(
            UtxoRecord(
                wallet_id,
                txid or f"{index + 1:064x}"[:64],
                0,
                ADDRS[index],
                coin,
                1,
                900_000,
            )
        )
        store.replace_utxos_for_wallet(wallet_id, rows)


# =========================================================================
# 1. Store registry — the stable-numbering contract
# =========================================================================


class TestStoreRegistry:
    def test_number_stable_across_sessions(self, tmp_path: Path) -> None:
        db = tmp_path / "store.db"
        with Store(db) as store:
            wallet = store.create_wallet("default", "d")
            first = store.note_address_shown(wallet.id, "bc1qaaa", shown_at=100)
            second = store.note_address_shown(wallet.id, "bc1qbbb", shown_at=101)
            assert (first.number, second.number) == (1, 2)
            assert first.first_shown == 100
        with Store(db) as store:  # session boundary
            again = store.note_address_shown(store.get_wallet_by_name("default").id, "bc1qaaa", shown_at=999)
            assert again.number == 1
            assert again.first_shown == 100  # recorded ONCE, never re-stamped

    def test_re_show_returns_same_row_unchanged(self) -> None:
        store = Store.memory()
        wid = store.create_wallet("w", "d").id
        a = store.note_address_shown(wid, "bc1qaaa", shown_at=1)
        b = store.note_address_shown(wid, "bc1qaaa", shown_at=2)
        assert a == b
        assert len(store.list_address_registry(wid)) == 1  # never duplicated

    def test_numbers_never_reused_and_dense(self) -> None:
        store = Store.memory()
        wid = store.create_wallet("w", "d").id
        numbers = {
            store.note_address_shown(wid, f"bc1q{i}", shown_at=i).number for i in range(10)
        }
        assert numbers == set(range(1, 11))

    def test_per_wallet_numbering(self) -> None:
        store = Store.memory()
        w1 = store.create_wallet("one", "d").id
        w2 = store.create_wallet("two", "d").id
        assert store.note_address_shown(w1, "bc1qaaa").number == 1
        assert store.note_address_shown(w2, "bc1qaaa").number == 1  # per-wallet
        assert store.note_address_shown(w2, "bc1qaaa").first_shown >= 0  # real clock

    def test_lookup_miss_is_none(self) -> None:
        store = Store.memory()
        wid = store.create_wallet("w", "d").id
        assert store.get_address_by_number(wid, 1) is None
        store.note_address_shown(wid, "bc1qa", shown_at=5)
        assert store.get_address_by_number(wid, 1).address == "bc1qa"
        assert store.get_address_by_number(wid, 2) is None  # no nearest-match ever

    def test_write_gate_value_free(self) -> None:
        store = Store.memory()
        wid = store.create_wallet("w", "d").id
        for bad in ("", " ", "has space", "bc1q\nFACTS BEGIN", "x" * 101, 42, None):
            with pytest.raises(StoreError) as excinfo:
                store.note_address_shown(wid, bad)  # type: ignore[arg-type]
            assert "bc1q" not in str(excinfo.value)
            assert "xxxx" not in str(excinfo.value)
        for bad in (0, -1, True, "1"):
            with pytest.raises(StoreError):
                store.get_address_by_number(wid, bad)  # type: ignore[arg-type]

    def test_registry_cascades_with_wallet(self) -> None:
        store = Store.memory()
        wid = store.create_wallet("w", "d").id
        store.note_address_shown(wid, "bc1qa", shown_at=1)
        store._conn.execute("DELETE FROM wallets WHERE id = ?", (wid,))
        assert store.list_address_registry(wid) == []

    def test_fk_no_such_wallet(self) -> None:
        store = Store.memory()
        with pytest.raises(StoreIntegrityError):
            store.note_address_shown(999, "bc1qa", shown_at=1)


class TestSchemaV4Migration:
    """The RBF-001 pattern: versioned, additive, idempotent, fail-closed."""

    def _as_v3_file(self, db: Path) -> None:
        raw = sqlite3.connect(db)
        raw.execute("DROP TABLE address_registry")
        raw.execute("PRAGMA user_version=3")
        raw.commit()
        raw.close()

    def test_v3_file_upgrades_on_reopen(self, tmp_path: Path) -> None:
        db = tmp_path / "store.db"
        with Store(db) as store:
            wallet = store.create_wallet("main", "d")
            store.upsert_txs([])  # touch the v3 surface
        self._as_v3_file(db)
        with Store(db) as store:
            assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 5
            assert store.list_address_registry(wallet.id) == []  # empty, never fabricated
            rec = store.note_address_shown(wallet.id, "bc1qa", shown_at=7)
            assert rec.number == 1
        with Store(db) as store:  # stable reopen, no re-run
            assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 5
            assert store.get_address_by_number(wallet.id, 1).address == "bc1qa"

    def test_migration_idempotent_half_applied(self, tmp_path: Path) -> None:
        """Crash-mid-migration shape (v3 stamp, table ALREADY created): the
        CREATE TABLE IF NOT EXISTS step is a no-op and the stamp completes."""
        db = tmp_path / "store.db"
        with Store(db):
            pass
        raw = sqlite3.connect(db)
        raw.execute("PRAGMA user_version=3")
        raw.commit()
        raw.close()
        with Store(db) as store:
            assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 5

    def test_fresh_create_is_v4(self, tmp_path: Path) -> None:
        with Store(tmp_path / "fresh.db") as store:
            assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 5

    def test_scan_persist_never_touches_registry(self) -> None:
        """The registry is a DISPLAY fact: the scan's composite write (which
        owns addresses/derivation/utxos/txs/sync_state) leaves it alone —
        a number can never be re-assigned or cleared by a rescan."""
        store = Store.memory()
        wallet = store.create_wallet("w", "d")
        store.note_address_shown(wallet.id, "bc1qa", shown_at=1)
        store.persist_scan_result(
            wallet.id,
            address_rows=[AddressRecord(wallet.id, 0, 0, "bc1qa", "p2wpkh", ADDRESS_USED)],
            derivation_states=[],
            utxo_snapshot=[],
            tx_rows=[],
            sync_state_updates={"last_scan_cursor": "{}"},
        )
        rows = store.list_address_registry(wallet.id)
        assert [(r.address, r.number, r.first_shown) for r in rows] == [("bc1qa", 1, 1)]


# =========================================================================
# 2. Numbering at FIRST SHOWING (every printing surface registers)
# =========================================================================


class TestFirstShowing:
    def test_new_address_result_and_line_carry_number(self) -> None:
        store, wallet, table = _world()
        world = (store, wallet, table)
        first, lines1 = _say(world, IntentName.NEW_ADDRESS, {})
        assert first["address_number"] == 1
        assert lines1[-1] == (
            f"Fresh receive address (index 0, address #1): {first['address']}"
        )
        second, lines2 = _say(world, IntentName.NEW_ADDRESS, {})
        assert second["address_number"] == 2  # fresh number for a fresh address
        assert second["address"] != first["address"]
        assert "address #2" in lines2[-1]
        # the registry agrees
        rows = store.list_address_registry(wallet.id)
        assert [r.number for r in rows] == [1, 2]

    def test_receive_preview_number_survives_allocation(self) -> None:
        """/receive SHOWS the next address (it gets its number now — the
        date it was first offered rides the registry, the future
        allocated-but-never-used signal); /address later issuing the SAME
        string must keep it."""
        store, wallet, table = _world()
        out: list[str] = []
        app._print_next_receive_address(store, out.append)
        assert "Next receive address #1 (index 0" in out[-1]
        result, lines = _say((store, wallet, table), IntentName.NEW_ADDRESS, {})
        assert result["address"] in out[-1]  # same previewed string
        assert result["address_number"] == 1  # SAME number forever
        assert "address #1" in lines[-1]
        # still exactly one registry row, first_shown from the preview
        rows = store.list_address_registry(wallet.id)
        assert len(rows) == 1 and rows[0].number == 1

    def test_get_utxos_registers_every_printed_address(self) -> None:
        store, wallet, table = _world()
        _plant(store, wallet.id, 0, status="unused", coin=5_000)
        _plant(store, wallet.id, 1, status="unused", coin=7_000)
        result, lines = _say((store, wallet, table), IntentName.GET_UTXOS, {})
        numbers = {u["address"]: u["number"] for u in result["utxos"]}
        assert set(numbers) == {ADDRS[0], ADDRS[1]}
        assert len(set(numbers.values())) == 2
        # every printed row carries its number prefix
        for addr, n in numbers.items():
            assert f"#{n} {addr} ·" in "\n".join(lines)
        # second call: SAME numbers (stability), still two rows
        again, _ = _say((store, wallet, table), IntentName.GET_UTXOS, {})
        assert {u["address"]: u["number"] for u in again["utxos"]} == numbers

    def test_watch_surfacing_registers_its_address(self) -> None:
        store, _wallet, _table = _world()
        event = IncomingEvent(
            kind="received", txid="a" * 64, address=ADDRS[2], amount_sats=5_000,
            confirmed=False,
        )
        watcher = SimpleNamespace(
            poll_due=lambda: True,
            tick=lambda: [event],
            mark_poll_succeeded=lambda: False,
            mark_poll_failed=lambda: True,
            interval_s=60,
        )
        outputs: list[str] = []
        app._drain_watch(watcher, outputs.append, store=store)
        assert f"at #1 {ADDRS[2]}" in outputs[0]  # numbered at the first showing
        # no store seam → the line stays byte-identical to pre-CHAT-001
        outs2: list[str] = []
        app._drain_watch(watcher, outs2.append)
        assert " #" not in outs2[0] and ADDRS[2] in outs2[0]


# =========================================================================
# 3. The numbered list — header honesty, rows, hint once-only
# =========================================================================


class TestNumberedList:
    def test_list_shape(self) -> None:
        store, wallet, table = _world()
        _plant(store, wallet.id, 2, status=ADDRESS_USED)  # activity, NEVER shown
        _plant(store, wallet.id, 3, status="unused", coin=7_777)  # coin = activity
        shown, _ = _say((store, wallet, table), IntentName.NEW_ADDRESS, {})  # #1
        # the list itself is the first showing of the two activity rows.
        result, lines = _say((store, wallet, table), IntentName.GET_ADDRESSES, {})
        joined = "\n".join(lines)
        assert "Your addresses, with the number each one keeps for good:" in joined
        assert '"Used" means we\'ve seen activity' in joined  # honest definition
        assert "as of" in joined  # the freshness bound of the last scan
        assert app.ADDRESS_TRACKING_FAQ_LINE in joined  # the ONE date line
        # rows: number, FULL address verbatim, used-state, honest empty
        # label slot, verbatim value only where the tool output has one.
        used_rows = [ln for ln in lines if ln.startswith("#")]
        assert len(used_rows) == 3  # the fresh alloc + both activity rows
        assert any(ADDRS[2] in ln and "used · unlabeled" in ln for ln in used_rows)
        assert any(ADDRS[3] in ln and "7777 sats" in ln for ln in used_rows)
        assert any(shown["address"] in ln and "not used yet" in ln for ln in used_rows)
        # no coin → NO value segment (never a fabricated 0)
        assert any(ADDRS[2] in ln and " sats" not in ln for ln in used_rows)
        # activity rows sort in wallet order: index 2 before index 3
        numbers = {e["address"]: e["number"] for e in result["addresses"]}
        assert numbers[ADDRS[2]] < numbers[ADDRS[3]]

    def test_hint_once_per_wallet_lifetime(self, tmp_path: Path) -> None:
        db = tmp_path / "store.db"
        wd = WalletDescriptor.from_key(ZPUB)
        with Store(db) as store:
            wallet = store.create_wallet("default", wd.descriptor)
            store.set_active_wallet(wallet.id)
            table = app.build_dispatch_table(
                store, wallet, wd.parsed, client=None, scan_fn=lambda: None
            )
            _say((store, wallet, table), IntentName.NEW_ADDRESS, {})  # make it non-empty
            _r, lines = _say((store, wallet, table), IntentName.GET_ADDRESSES, {})
            assert app.ADDRESS_REF_HINT in lines[-1]
            _r, lines = _say((store, wallet, table), IntentName.GET_ADDRESSES, {})
            assert app.ADDRESS_REF_HINT not in lines  # once only
        with Store(db) as store:  # even across a session boundary
            wallet = store.get_active_wallet()
            table = app.build_dispatch_table(
                store, wallet, wd.parsed, client=None, scan_fn=lambda: None
            )
            _r, lines = _say((store, wallet, table), IntentName.GET_ADDRESSES, {})
            assert app.ADDRESS_REF_HINT not in lines

    def test_empty_list_consumes_nothing(self) -> None:
        """An empty registry prints the honest empty line and does NOT burn
        the one-time hint (nothing numbered was shown to learn from)."""
        store, wallet, table = _world()
        _r, lines = _say((store, wallet, table), IntentName.GET_ADDRESSES, {})
        assert app.ADDRESSES_NONE_SHOWN in lines
        _say((store, wallet, table), IntentName.NEW_ADDRESS, {})
        _r, lines = _say((store, wallet, table), IntentName.GET_ADDRESSES, {})
        assert app.ADDRESS_REF_HINT in lines[-1]  # still unspent, now shown

    def test_hint_not_fired_by_a_single_restatement(self) -> None:
        store, wallet, table = _world()
        _say((store, wallet, table), IntentName.NEW_ADDRESS, {})  # registers #1
        _r, lines = _say((store, wallet, table), IntentName.GET_ADDRESSES, {"address_number": 1})
        assert app.ADDRESS_REF_HINT not in lines  # the LIST teaches, not a show
        # and the flag is therefore still unspent:
        _r2, lines2 = _say((store, wallet, table), IntentName.GET_ADDRESSES, {})
        assert app.ADDRESS_REF_HINT in lines2[-1]

    def test_stale_list_carries_freshness_note(self) -> None:
        store, wallet, table = _world()
        _say((store, wallet, table), IntentName.NEW_ADDRESS, {})
        _r, lines = _say((store, wallet, table), IntentName.GET_ADDRESSES, {})
        assert app.FRESHNESS_NOTE in lines[0]  # first scan incomplete → note
        assert "none yet, this wallet is still loading" in "\n".join(lines)

    def test_fresh_list_names_the_scan_date(self) -> None:
        store, wallet, table = _world()
        _say((store, wallet, table), IntentName.NEW_ADDRESS, {})
        store.set_sync_state(wallet.id, "last_scan_cursor", "{}")
        store.set_sync_state(wallet.id, "last_scan_at", "2026-09-13T12:00:00+00:00")
        _r, lines = _say((store, wallet, table), IntentName.GET_ADDRESSES, {})
        assert "as of your last scan (2026-09-13)" in "\n".join(lines)
        assert app.FRESHNESS_NOTE not in lines

    def test_never_shown_never_used_addresses_are_absent(self) -> None:
        store, wallet, table = _world()
        _plant(store, wallet.id, 3, status="unused")  # derived, no activity
        result, _lines = _say((store, wallet, table), IntentName.GET_ADDRESSES, {})
        assert result["count"] == 0  # the registry tracks SHOWINGS, not the gap window
        assert store.list_address_registry(wallet.id) == []


# =========================================================================
# 4. Referent resolution — full-address restatement, bound-check, scoping
# =========================================================================


class TestReferentResolution:
    def test_show_restates_full_address(self) -> None:
        store, wallet, table = _world()
        fresh, _ = _say((store, wallet, table), IntentName.NEW_ADDRESS, {})
        result, lines = _say((store, wallet, table), IntentName.GET_ADDRESSES, {"address_number": 1})
        assert result["addresses"] == [
            {
                "number": 1,
                "address": fresh["address"],
                "used": False,
                "sats_total": None,
                "label": None,
            }
        ]
        assert fresh["address"] in lines[-1]  # number-only is a BUG; address is here

    def test_single_restatement_numbers_nothing_else(self) -> None:
        """A by-number SHOW prints ONE address; the wallet's other
        activity-seen rows must NOT collect numbers they were never SHOWN
        for (a number is minted at a showing, never as a lookup side
        effect)."""
        store, wallet, table = _world()
        _plant(store, wallet.id, 2, status=ADDRESS_USED, coin=20)  # activity, unseen
        _say((store, wallet, table), IntentName.NEW_ADDRESS, {})  # #1 = ADDRS[0]
        _say((store, wallet, table), IntentName.GET_ADDRESSES, {"address_number": 1})
        rows = store.list_address_registry(wallet.id)
        assert [r.address for r in rows] == [ADDRS[0]]  # ADDRS[2] still unnumbered
        # the LIST is where it gets its number (its actual first showing)
        listed, _ = _say((store, wallet, table), IntentName.GET_ADDRESSES, {})
        assert [(e["number"], e["address"]) for e in listed["addresses"]] == [
            (1, ADDRS[0]),
            (2, ADDRS[2]),
        ]

    def test_out_of_range_is_value_free_clarify_never_a_guess(self) -> None:
        store, wallet, table = _world()
        _plant(store, wallet.id, 0, status=ADDRESS_USED, coin=123)
        _say((store, wallet, table), IntentName.NEW_ADDRESS, {})  # registry = #1
        for intent, params in (
            (IntentName.GET_ADDRESSES, {"address_number": 2}),
            (IntentName.GET_BALANCE, {"address_number": 99}),
            (IntentName.GET_UTXOS, {"address_number": 7}),
        ):
            result, lines = _say((store, wallet, table), intent, params)
            assert result == {"error": "address_ref_unknown"}  # value-free by SHAPE
            assert lines == [app.ADDRESS_REF_UNKNOWN]
            assert not any(c.isdigit() for c in lines[0])  # no number echoed
        # crucially: the unresolved #2 did NOT answer for address #1 either
        scoped, _ = _say((store, wallet, table), IntentName.GET_BALANCE, {"address_number": 2})
        assert "address" not in scoped

    def test_balance_scoped_to_one_address(self) -> None:
        store, wallet, table = _world()
        _plant(store, wallet.id, 0, status=ADDRESS_USED, coin=1_000)
        _plant(store, wallet.id, 1, status=ADDRESS_USED, coin=2_000, txid="b" * 64)
        _say((store, wallet, table), IntentName.GET_UTXOS, {})  # shows both → numbers
        numbers = {r.address: r.number for r in store.list_address_registry(wallet.id)}
        result, lines = _say(
            (store, wallet, table), IntentName.GET_BALANCE,
            {"address_number": numbers[ADDRS[1]]},
        )
        assert result["total_sats"] == 2_000  # scoped, not the wallet total
        assert result["address"] == ADDRS[1]
        assert lines[0] == f"Balance for address #{numbers[ADDRS[1]]} — {ADDRS[1]}:"
        assert "1000 sats" not in "\n".join(lines)  # the other address's coin

    def test_utxos_scoped_to_one_address(self) -> None:
        store, wallet, table = _world()
        _plant(store, wallet.id, 0, status=ADDRESS_USED, coin=1_000)
        _plant(store, wallet.id, 1, status=ADDRESS_USED, coin=2_000, txid="b" * 64)
        _say((store, wallet, table), IntentName.GET_UTXOS, {})
        numbers = {r.address: r.number for r in store.list_address_registry(wallet.id)}
        result, lines = _say(
            (store, wallet, table), IntentName.GET_UTXOS,
            {"address_number": numbers[ADDRS[0]]},
        )
        assert result["count"] == 1
        assert result["utxos"][0]["address"] == ADDRS[0]
        assert result["utxos"][0]["value_sats"] == 1_000
        assert lines[0] == app.FRESHNESS_NOTE
        assert f"Coins on address #{numbers[ADDRS[0]]} — {ADDRS[0]}:" in lines[1]

    def test_resolution_survives_session_boundary(self, tmp_path: Path) -> None:
        """Numbers are wallet-lifetime, not per-list-positional: an address
        asked about by number in a NEW session resolves to the same address
        it was shown under (the council's silent-retargeting bug)."""
        db = tmp_path / "store.db"
        wd = WalletDescriptor.from_key(ZPUB)
        with Store(db) as store:
            wallet = store.create_wallet("default", wd.descriptor)
            store.set_active_wallet(wallet.id)
            table = app.build_dispatch_table(store, wallet, wd.parsed, client=None, scan_fn=lambda: None)
            fresh, _ = _say((store, wallet, table), IntentName.NEW_ADDRESS, {})
            expected = fresh["address"]
        with Store(db) as store:
            wallet = store.get_active_wallet()
            table = app.build_dispatch_table(store, wallet, wd.parsed, client=None, scan_fn=lambda: None)
            _r, lines = _say((store, wallet, table), IntentName.GET_ADDRESSES, {"address_number": 1})
            assert expected in lines[-1]
            # and a RE-LIST keeps order/numbers identical, never renumbers
            result, _ = _say((store, wallet, table), IntentName.GET_ADDRESSES, {})
            assert [(e["number"], e["address"]) for e in result["addresses"]] == [(1, expected)]


# =========================================================================
# 5. The model path — registry FACTS + lockstep drift pins
# =========================================================================


def _recording_loop(
    generate_reply: str, table: dict | None = None
) -> tuple[AgentLoop, list[str]]:
    seen: list[str] = []

    def generate(prompt: str, grammar: Any) -> str:
        seen.append(prompt)
        return generate_reply

    return AgentLoop(generate, table if table is not None else {}), seen


class TestRegistryFacts:
    def test_facts_shape_no_fabrication(self) -> None:
        store, wallet, table = _world()
        assert app._address_registry_facts(store) == {}  # fresh wallet: NO facts
        _plant(store, wallet.id, 2, status=ADDRESS_USED, coin=42)  # used, unshown
        _say((store, wallet, table), IntentName.NEW_ADDRESS, {})  # #1 = ADDRS[0]
        _say((store, wallet, table), IntentName.GET_ADDRESSES, {})  # shows #2 = ADDRS[2]
        facts = app._address_registry_facts(store)
        assert set(facts) == {"address_registry", "address_registry_count"}
        assert facts["address_registry_count"] == 2
        entries = facts["address_registry"].split(" ")
        rows = store.list_address_registry(wallet.id)
        # exactly the registry rows, one entry each — nothing fabricated,
        # nothing dropped, used-state from the same store join as the list.
        assert [(f"#{r.number}", r.address) for r in rows] == [
            (e.split(":")[0], e.split(":")[1]) for e in entries
        ]
        assert entries[0] == f"#1:{ADDRS[0]}:not-used-yet"
        assert entries[1] == f"#2:{ADDRS[2]}:used"

    def test_facts_injection_bounded_and_honest(self) -> None:
        store, wallet, _table = _world()
        for i in range(app._REGISTRY_FACTS_MAX + 5):
            store.note_address_shown(wallet.id, f"bc1q{i}", shown_at=i)
        facts = app._address_registry_facts(store)
        entries = facts["address_registry"].split(" ")
        assert len(entries) == app._REGISTRY_FACTS_MAX
        assert facts["address_registry_count"] == app._REGISTRY_FACTS_MAX + 5
        assert facts["address_registry_note"].startswith(
            f"showing first {app._REGISTRY_FACTS_MAX} of {app._REGISTRY_FACTS_MAX + 5}"
        )
        # whole entries only: nothing may be cut mid-address (a truncated
        # address quoted back would be the verbatim rule broken)
        assert all(e.count(":") == 2 and e.startswith("#") for e in entries)

    def test_run_turn_injects_registry_facts_into_prompt(self) -> None:
        store, wallet, table = _world()
        _say((store, wallet, table), IntentName.NEW_ADDRESS, {})
        reply = '{"v":0,"intent":"get_addresses","params":{"address_number":1}}'
        loop, prompts = _recording_loop(reply, table)
        out: list[str] = []
        app._run_turn(
            loop, app.TxFlow(), app.SendSession(), "show address 1", out.append,
            table=table, store=store,
        )
        assert "FACTS BEGIN" in prompts[0]
        assert f"address_registry: #1:{ADDRS[0]}" in prompts[0]
        assert app.ADDRESS_TRACKING_FAQ_LINE not in prompts[0]  # user copy stays user-facing
        assert any(ADDRS[0] in line for line in out)  # resolved + restated

    def test_run_turn_without_store_has_no_registry_facts(self) -> None:
        loop, prompts = _recording_loop(
            '{"v":0,"intent":"respond","params":{"text":"hi"}}',
            {IntentName.RESPOND: app._respond_handler},
        )
        app._run_turn(
            loop, app.TxFlow(), app.SendSession(), "hello", [].append,
            table={IntentName.RESPOND: app._respond_handler},
        )
        assert "FACTS BEGIN" not in prompts[0]  # no facts → no block at all

    def test_prompt_grammar_schema_lockstep(self) -> None:
        """Drift pin: get_addresses exists in the enum/registry/rules, the
        prompt teaches the NUMBER-only contract, and every widened params
        shape round-trips through the real validator."""
        from localwallet.agent.prompt import build_system_prompt
        from localwallet.protocol import INTENT_REGISTRY
        from localwallet.protocol import IntentName as IN
        from localwallet.protocol.intents import BUSINESS_RULES

        assert IN.GET_ADDRESSES.value == "get_addresses"
        assert IN.GET_ADDRESSES in INTENT_REGISTRY and IN.GET_ADDRESSES in BUSINESS_RULES
        prompt = build_system_prompt()
        assert "- get_addresses:" in prompt
        assert "ADDRESS REGISTRY" in prompt  # the FACTS routing rule
        assert '"address_number"' in prompt
        for intent, params in (
            ("get_balance", {"address_number": 3}),
            ("get_utxos", {"address_number": 3}),
            ("get_addresses", {"address_number": 3}),
        ):
            env = validate_payload({"v": 0, "intent": intent, "params": params})
            assert env.params.address_number == 3
            # wire fidelity: EXACTLY the grammar's shape (no nulls)
            assert json.loads(env.model_dump_json())["params"] == {"address_number": 3}

    def test_no_address_is_representable_in_the_new_params(self) -> None:
        from localwallet.protocol import EnvelopeValidationError

        for intent in ("get_balance", "get_utxos", "get_addresses"):
            with pytest.raises(EnvelopeValidationError):
                validate_payload(
                    {"v": 0, "intent": intent, "params": {"address": "bc1qwhatever"}}
                )


# =========================================================================
# 6. Value-free discipline (the log-scrubbing invariant)
# =========================================================================


class TestValueFreeDiscipline:
    def test_error_paths_carry_no_values(self) -> None:
        store, wallet, table = _world()
        _say((store, wallet, table), IntentName.NEW_ADDRESS, {})
        for intent, params in (
            (IntentName.GET_ADDRESSES, {"address_number": 5}),
            (IntentName.GET_BALANCE, {"address_number": 5}),
            (IntentName.GET_UTXOS, {"address_number": 5}),
        ):
            result, lines = _say((store, wallet, table), intent, params)
            blob = json.dumps(result) + "\n".join(lines)
            assert ADDRS[0] not in blob  # the one shown address never appears
            assert "5" not in lines[0]  # nor the asked-for number

    def test_registry_records_never_enter_model_context_as_authority(self) -> None:
        # The FACTS line carries addresses (routing help), but the resolved
        # address on the ANSWER path always comes from the STORE, not from
        # anything the model could echo: mutate the injected facts mid-test
        # and the handler still restates the store's row verbatim.
        store, wallet, table = _world()
        _say((store, wallet, table), IntentName.NEW_ADDRESS, {})
        env = _env(IntentName.GET_ADDRESSES, {"address_number": 1})
        result = table[IntentName.GET_ADDRESSES](env)
        assert result["addresses"][0]["address"] == ADDRS[0]

    def test_params_classes_are_typed_per_intent(self) -> None:
        # identically-shaped siblings never cross-bind (a get_balance
        # envelope must not be interpretable as get_addresses params)
        assert isinstance(
            _env(IntentName.GET_BALANCE, {"address_number": 1}).params, GetBalanceParams
        )
        assert isinstance(
            _env(IntentName.GET_UTXOS, {"address_number": 1}).params, GetUtxosParams
        )
        assert isinstance(
            _env(IntentName.GET_ADDRESSES, {"address_number": 1}).params, GetAddressesParams
        )
        assert isinstance(_env(IntentName.NEW_ADDRESS, {}).params, NewAddressParams)

"""TCK-UTXO-005 (ENGINE HALF): the typed ``utxo_rows`` render contract.

The ``get_utxos`` handler result gains an ADDITIVE ``utxo_rows`` key — one
row per printed coin, in narration order, engine-computed from store truth
only (the web client renders these and never does the sats↔BTC math; the
static renderer prefers ``utxo_rows`` and falls back to the bubble text
when the key is absent — empty listing, error shapes, legacy results).

Pinned here: per-field types and values; registry numbers identical to the
``#N`` prefixes the narration prints; the confirmed flag from the store
record; ``value_btc`` exactness under INTEGER math (the 10,000,000 sats ↔
"0.10000000 BTC" user pin); the label PASSTHROUGH rule (a row that
already displays a label keeps it verbatim; the get_utxos listing shows
no labels, so its rows never carry one and label text never flows back
into the tool output — LABEL-001); absence of the whole key on empty/
error shapes; and the CLI separator verdict (the per-coin narration line
now carries thousands separators — the verdict of "does it already have
them?" was NO, so they were added and are pinned).
"""

from __future__ import annotations

import json
import re
from typing import Any

from localwallet.agent.loop import AgentTurnResult, AgentTurnStatus
from localwallet.app import (
    EVENT_TEXT,
    EVENT_UTXO_ROWS,
    EventEmitter,
    _make_get_utxos_handler,
    _print_turn,
    _print_utxos,
    _sats_to_btc_str,
    _utxo_render_rows,
)
from localwallet.protocol import Envelope, IntentName, validate_payload
from localwallet.store import Store, UtxoRecord
from localwallet.wallet import scan as wallet_scan
from localwallet.wallet.descriptor import WalletDescriptor
from tests.test_e2e_skeleton import ZPUB, derive_fixture_addresses

GET_UTXOS_JSON = '{"v": 0, "intent": "get_utxos", "params": {}}'


def _store_with_wallet() -> tuple[Store, int]:
    store = Store.memory()
    wd = WalletDescriptor.from_key(ZPUB)
    wallet = store.create_wallet("default", wd.descriptor)
    store.set_sync_state(wallet.id, wallet_scan.CURSOR_KEY, "[]")
    return store, wallet.id


def _utxo(
    wallet_id: int, txid: str, vout: int, address: str | None, sats: int, confirmed: int
) -> UtxoRecord:
    return UtxoRecord(
        wallet_id=wallet_id,
        txid=txid,
        vout=vout,
        address=address,
        value_sats=sats,
        confirmed=confirmed,
        height=800_000 if confirmed == 1 else None,
    )


def _ask(store: Store, wallet_id: int, params: dict[str, object] | None = None) -> dict:
    payload = {"v": 0, "intent": "get_utxos", "params": params or {}}
    envelope: Envelope = validate_payload(json.dumps(payload))
    result = _make_get_utxos_handler(store, wallet_id)(envelope)
    return result


def _narrate(result: dict) -> list[str]:
    lines: list[str] = []
    _print_utxos(result, lines.append)
    return lines


class TestRowsShape:
    def test_one_typed_row_per_coin_in_narration_order(self) -> None:
        store, wid = _store_with_wallet()
        a0, a1 = derive_fixture_addresses(2)
        store.replace_utxos_for_wallet(
            wid,
            [
                _utxo(wid, "a" * 64, 0, a0, 10_000_000, confirmed=1),
                _utxo(wid, "b" * 64, 1, a1, 1, confirmed=0),
            ],
        )
        result = _ask(store, wid)
        rows = result["utxo_rows"]
        assert isinstance(rows, list) and len(rows) == 2
        assert [r["txid"] for r in rows] == ["a" * 64, "b" * 64]  # utxos order
        for row in rows:
            assert isinstance(row["number"], int) and not isinstance(row["number"], bool)
            assert isinstance(row["value_sats"], int)
            assert isinstance(row["value_btc"], str)
            assert isinstance(row["confirmed"], bool)
            assert isinstance(row["address"], str)
            assert isinstance(row["txid"], str)
            assert "label" not in row  # unlabeled: ABSENT, never ""

    def test_registry_numbers_match_the_narration(self) -> None:
        store, wid = _store_with_wallet()
        a0, a1 = derive_fixture_addresses(2)
        store.replace_utxos_for_wallet(
            wid,
            [
                _utxo(wid, "a" * 64, 0, a0, 50_000, confirmed=1),
                _utxo(wid, "b" * 64, 0, a1, 12_345, confirmed=0),
            ],
        )
        result = _ask(store, wid)
        line_re = re.compile(r"^#(\d+) (\S+) · ([\d,]+) sats · (confirmed|unconfirmed)")
        coin_lines = [
            m.groups()
            for ln in _narrate(result)
            if (m := line_re.match(ln))
        ]
        rows = result["utxo_rows"]
        assert len(coin_lines) == len(rows)
        for (number, address, sats, state), row in zip(coin_lines, rows):
            assert row["number"] == int(number)  # same #N the line prints
            assert row["address"] == address
            assert row["value_sats"] == int(sats.replace(",", ""))
            assert row["confirmed"] is (state == "confirmed")
        # idempotent re-showing: the numbers never move.
        again = _ask(store, wid)["utxo_rows"]
        assert [r["number"] for r in again] == [r["number"] for r in rows]

    def test_confirmed_flag_is_store_truth(self) -> None:
        store, wid = _store_with_wallet()
        a0, _a1 = derive_fixture_addresses(2)
        store.replace_utxos_for_wallet(
            wid,
            [
                _utxo(wid, "c" * 64, 0, a0, 700, confirmed=1),
                _utxo(wid, "d" * 64, 0, a0, 700, confirmed=0),
            ],
        )
        rows = _ask(store, wid)["utxo_rows"]
        assert [r["confirmed"] for r in rows] == [True, False]

    def test_no_vout_on_rows_value_free_keys(self) -> None:
        # The row contract is EXACTLY the pinned keys (label rides the
        # v6 set when present; nothing else — no vout, no extras).
        store, wid = _store_with_wallet()
        a0 = derive_fixture_addresses(1)[0]
        store.replace_utxos_for_wallet(wid, [_utxo(wid, "a" * 64, 0, a0, 100, 1)])
        # TCK-UTXO-006 additive: arrival + txid_copy_only join the set
        # (the row contract's exact keys re-pinned at full listing).
        assert set(_ask(store, wid)["utxo_rows"][0]) == {
            "number", "value_sats", "value_btc", "confirmed", "address", "txid",
            "arrival", "txid_copy_only",
        }


class TestValueBtcExactness:
    def test_user_pin_10m_sats(self) -> None:
        assert _sats_to_btc_str(10_000_000) == "0.10000000"
        store, wid = _store_with_wallet()
        a0 = derive_fixture_addresses(1)[0]
        store.replace_utxos_for_wallet(wid, [_utxo(wid, "a" * 64, 0, a0, 10_000_000, 1)])
        row = _ask(store, wid)["utxo_rows"][0]
        assert (row["value_sats"], row["value_btc"]) == (10_000_000, "0.10000000")

    def test_eight_decimals_always_integer_math(self) -> None:
        # exact string per row: no float ever touches these (a float
        # would lose the low digits past 2^53 territory):
        assert _sats_to_btc_str(1) == "0.00000001"
        assert _sats_to_btc_str(99_999_999) == "0.99999999"
        assert _sats_to_btc_str(100_000_000) == "1.00000000"
        assert _sats_to_btc_str(100_000_001) == "1.00000001"
        assert _sats_to_btc_str(9_007_199_254) == "90.07199254"
        assert _sats_to_btc_str(2_100_000_000_000_000) == "21000000.00000000"

    def test_row_btc_verbatim_from_row_sats(self) -> None:
        store, wid = _store_with_wallet()
        a0, a1 = derive_fixture_addresses(2)
        store.replace_utxos_for_wallet(
            wid,
            [
                _utxo(wid, "a" * 64, 0, a0, 3_141_592_653, confirmed=1),
                _utxo(wid, "b" * 64, 0, a1, 500, confirmed=1),
            ],
        )
        rows = _ask(store, wid)["utxo_rows"]
        assert [r["value_btc"] for r in rows] == ["31.41592653", "0.00000500"]


class TestLabelPassthrough:
    """The row contract's ``label`` is a PASSTHROUGH of a label the input
    row already displays — the get_utxos listing shows NO per-row label,
    so its rows carry none and the v6 label TEXT never flows back into
    the tool output (the LABEL-001 pin, re-pinned here for utxo_rows)."""

    def test_handler_rows_carry_no_label_even_when_labeled(self) -> None:
        store, wid = _store_with_wallet()
        a0 = derive_fixture_addresses(1)[0]
        store.replace_utxos_for_wallet(wid, [_utxo(wid, "a" * 64, 0, a0, 1000, 1)])
        committed = store.add_address_labels(a0, ("PrivateCoinLabel",))
        assert committed == ("PrivateCoinLabel",)
        result = _ask(store, wid)
        assert "label" not in result["utxo_rows"][0]
        # the LABEL-001 discipline, now covering the additive key:
        assert "PrivateCoinLabel" not in json.dumps(result)

    def test_scoped_listing_also_label_free(self) -> None:
        store, wid = _store_with_wallet()
        a0 = derive_fixture_addresses(1)[0]
        store.replace_utxos_for_wallet(wid, [_utxo(wid, "a" * 64, 0, a0, 1000, 1)])
        number = _ask(store, wid)["utxo_rows"][0]["number"]
        store.add_address_labels(a0, ("spearmint",))
        scoped = _ask(store, wid, {"address_number": number})
        assert all("label" not in row for row in scoped["utxo_rows"])
        assert "spearmint" not in json.dumps(scoped)

    def test_helper_passes_through_a_row_displayed_label(self) -> None:
        # A surface that DOES display a label per row wires its resolved
        # v6 text into the input row; the helper copies it VERBATIM
        # (present iff non-empty, never mutated, never fetched).
        rows = _utxo_render_rows(
            [
                {"txid": "a" * 64, "address": "bc1qx", "number": 1,
                 "value_sats": 5, "confirmed": True, "label": "'kyc', 'exchange'"},
                {"txid": "b" * 64, "address": "bc1qy", "number": 2,
                 "value_sats": 6, "confirmed": True, "label": ""},
                {"txid": "c" * 64, "address": "bc1qz", "number": 3,
                 "value_sats": 7, "confirmed": True},
            ]
        )
        assert rows[0]["label"] == "'kyc', 'exchange'"  # verbatim
        assert "label" not in rows[1]  # empty is absent, never ""
        assert "label" not in rows[2]  # no label displayed: absent


class TestKeyAbsence:
    def test_empty_listing_no_key_plain_shape(self) -> None:
        store, wid = _store_with_wallet()
        result = _ask(store, wid)
        assert "utxo_rows" not in result
        assert set(result) == {"utxos", "count", "freshness"}
        assert "No unspent outputs." in _narrate(result)

    def test_filter_to_empty_no_key(self) -> None:
        store, wid = _store_with_wallet()
        a0 = derive_fixture_addresses(1)[0]
        store.replace_utxos_for_wallet(wid, [_utxo(wid, "a" * 64, 0, a0, 1000, 1)])
        # direction filter over a coin whose creating tx row is missing →
        # honest empty listing → no rows key (the text fallback renders).
        result = _ask(store, wid, {"direction": "in"})
        assert result["utxos"] == []
        assert "utxo_rows" not in result

    def test_error_shape_no_key(self) -> None:
        store, wid = _store_with_wallet()
        result = _ask(store, wid, {"address_number": 9})  # unresolvable ref
        assert result == {"error": "address_ref_unknown"}
        assert "utxo_rows" not in result

    def test_legacy_result_text_only(self) -> None:
        # A pre-TCK-UTXO-005 result dict (no rows key) still narrates the
        # coin line — the fallback contract: absent key = render the text.
        lines: list[str] = []
        _print_utxos(
            {"utxos": [{"txid": "a" * 64, "vout": 0, "value_sats": 2500,
                        "address": None, "confirmed": True}], "count": 1},
            lines.append,
        )
        assert any("2,500 sats · confirmed · tx " in ln for ln in lines)

    def test_addressless_row_omits_number_and_address(self) -> None:
        # Degenerate store row (address NULL): the row still lists the
        # coin (value/confirmed/txid/value_btc) without a number or an
        # address — mirroring the narration, which prints no #N prefix.
        store, wid = _store_with_wallet()
        store.replace_utxos_for_wallet(wid, [_utxo(wid, "a" * 64, 0, None, 100, 1)])
        row = _ask(store, wid)["utxo_rows"][0]
        # TCK-UTXO-006 additive: arrival/txid_copy_only ride every row too.
        assert set(row) == {
            "value_sats", "value_btc", "confirmed", "txid",
            "arrival", "txid_copy_only",
        }


class TestSeparatorVerdict:
    """The ticket's VERIFY step: the CLI narration did NOT carry thousands
    separators before TCK-UTXO-005 ("50000 sats · confirmed"), so they
    were ADDED (deterministic ``:,`` of the store integer) and are pinned
    here — everything else in the narration stays byte-identical."""

    def test_coin_lines_carry_separators(self) -> None:
        store, wid = _store_with_wallet()
        a0 = derive_fixture_addresses(1)[0]
        store.replace_utxos_for_wallet(
            wid, [_utxo(wid, "a" * 64, 0, a0, 10_000_000, confirmed=1)]
        )
        result = _ask(store, wid)
        (line,) = [ln for ln in _narrate(result) if " sats · " in ln]
        # TCK-UTXO-006 additive: the line gained the " · arrived <date|
        # pending>" segment (no tx row seeded here -> the honest pending
        # marker; the date cases are pinned in test_utxo006_arrival_rows).
        assert line == (
            f"#1 {a0} · 10,000,000 sats · confirmed · arrived pending · "
            f"tx {'a' * 64} vout 0"
        )
        # the row keeps the RAW integer (the client separates for display;
        # the text and the row are two renderings of one store truth).
        assert result["utxo_rows"][0]["value_sats"] == 10_000_000

    def test_sub_thousand_values_unchanged_bytes(self) -> None:
        store, wid = _store_with_wallet()
        a0 = derive_fixture_addresses(1)[0]
        store.replace_utxos_for_wallet(
            wid, [_utxo(wid, "f" * 64, 2, a0, 500, confirmed=0)]
        )
        (line,) = [ln for ln in _narrate(_ask(store, wid)) if " sats · " in ln]
        # TCK-UTXO-006 additive: the arrived segment (pending — no tx row).
        assert line == (
            f"#1 {a0} · 500 sats · unconfirmed · arrived pending · "
            f"tx {'f' * 64} vout 2"
        )

    def test_everything_else_byte_identical(self) -> None:
        # the pending block and the empty answer keep their pre-ticket
        # (unseparated, tool-owned) copy — only the per-coin line changed.
        store, wid = _store_with_wallet()
        a0 = derive_fixture_addresses(1)[0]
        store.replace_utxos_for_wallet(
            wid, [_utxo(wid, "b" * 64, 1, a0, 12_345, confirmed=0)]
        )
        lines = _narrate(_ask(store, wid))
        assert any(ln.startswith("Pending: 1 incoming for 12345 sats") for ln in lines)
        store2, wid2 = _store_with_wallet()
        assert "No unspent outputs." in _narrate(_ask(store2, wid2))


class TestStaticEmission:
    """TCK-UTXO-005 STATIC HALF: the pump ships ``utxo_rows`` to the web
    client as an additive SSE event — the mirror of the HW-005
    ``own_address`` additive stamp. Pinned: the event rides AFTER the coin
    narration lines (mirroring :func:`_print_new_address`), its payload is
    the EXACT compact JSON of the handler's row dicts
    (``json.dumps(..., separators=(",", ":"))``), a result WITHOUT the key
    emits nothing, and the emitter=None (CLI sink) path stays byte-identical.
    The turn_end marker is emitted by the pump after :func:`_print_turn`
    returns, so this in-printer emission structurally precedes it."""

    def _events(self, result: dict) -> tuple[list[Any], list[str]]:
        events: list[Any] = []
        emitter = EventEmitter(events.append)
        # web shape: output_fn IS the emitter's text sink, and the emitter
        # rides along for the typed marker — exactly how the pump wires it.
        out: list[str] = []
        _print_utxos(result, emitter.text, emitter=emitter)
        return events, out

    def test_emits_rows_after_coin_lines_with_exact_json(self) -> None:
        store, wid = _store_with_wallet()
        a0 = derive_fixture_addresses(1)[0]
        store.replace_utxos_for_wallet(
            wid, [_utxo(wid, "a" * 64, 0, a0, 10_000_000, confirmed=1)]
        )
        result = _ask(store, wid)
        events, _out = self._events(result)
        rows_ev = [e for e in events if e.kind == EVENT_UTXO_ROWS]
        assert len(rows_ev) == 1
        # payload is the EXACT compact JSON of the rows (byte-for-byte).
        assert rows_ev[0].payload == json.dumps(result["utxo_rows"], separators=(",", ":"))
        assert json.loads(rows_ev[0].payload) == result["utxo_rows"]
        # ordering: every coin narration line precedes the typed event
        # (the emit sits after the coin loop in _print_utxos; turn_end is
        # the pump's own marker, emitted after _print_turn returns).
        kinds = [e.kind for e in events]
        assert kinds[-1] == EVENT_UTXO_ROWS  # stamped after the last narration
        assert kinds.index(EVENT_TEXT) < kinds.index(EVENT_UTXO_ROWS)
        # one narration line per coin, all before the event.
        assert kinds.count(EVENT_TEXT) == len(result["utxo_rows"])

    def test_result_without_rows_emits_nothing(self) -> None:
        # empty listing / error / legacy shapes carry no utxo_rows key and
        # must NOT emit (the web client falls back to the bubble text).
        store, wid = _store_with_wallet()
        events, _out = self._events(_ask(store, wid))
        assert [e for e in events if e.kind == EVENT_UTXO_ROWS] == []
        assert any(e.kind == EVENT_TEXT and e.payload == "No unspent outputs."
                   for e in events)
        events2, _out2 = self._events({"error": "address_ref_unknown"})
        assert [e for e in events2 if e.kind == EVENT_UTXO_ROWS] == []

    def test_no_emitter_path_stays_byte_identical(self) -> None:
        # CLI sink (emitter=None): no event, no crash — text unchanged.
        store, wid = _store_with_wallet()
        a0 = derive_fixture_addresses(1)[0]
        store.replace_utxos_for_wallet(
            wid, [_utxo(wid, "a" * 64, 0, a0, 500, confirmed=1)]
        )
        result = _ask(store, wid)
        (line,) = [ln for ln in _narrate(result) if " sats · " in ln]
        # TCK-UTXO-006 additive: arrived pending (no tx row seeded).
        assert line == (
            f"#1 {a0} · 500 sats · confirmed · arrived pending · "
            f"tx {'a' * 64} vout 0"
        )

    def test_print_turn_model_path_emits_rows(self) -> None:
        # Mirror of the HW-005 model-path emission pin: the GET_UTXOS turn
        # narration rides the SAME in-printer emission (result-owned rows).
        store, wid = _store_with_wallet()
        a0 = derive_fixture_addresses(1)[0]
        store.replace_utxos_for_wallet(
            wid, [_utxo(wid, "a" * 64, 0, a0, 2500, confirmed=1)]
        )
        result = _ask(store, wid)
        events: list[Any] = []
        emitter = EventEmitter(events.append)
        envelope = Envelope(v=0, intent=IntentName.GET_UTXOS, params={})
        _print_turn(
            AgentTurnResult(
                status=AgentTurnStatus.OK,
                envelope=envelope,
                result=result,
                user_message=None,
                turns_used=0,
            ),
            [].append,
            emitter=emitter,
        )
        rows_ev = [e for e in events if e.kind == EVENT_UTXO_ROWS]
        assert len(rows_ev) == 1
        assert rows_ev[0].payload == json.dumps(result["utxo_rows"], separators=(",", ":"))

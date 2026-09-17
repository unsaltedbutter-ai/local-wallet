"""TCK-TXID-002 (ENGINE HALF): the [tx] affordance + the direct-ask full id.

The 2026-09-16 clarification supersedes TXID-001's "full txids everywhere
in replies": transcript lines reference a transaction through the COMPACT
token (pinned CLI form: first 8 lowercase hex + ``…``), and the full
value rides a typed per-line payload — the additive ``txid_refs`` SSE
event mirroring the UTXO-006 row vocabulary (``txid`` +
``txid_copy_only=True``), so the static follow-up dispatch can render the
same ``[tx]`` copy chip all-or-nothing. The FULL txid prints ONLY on a
direct ask ("what is the transaction id for utxo #N?", "transaction id
for the pending transaction?"), on ``/details`` (cached full rows —
pinned in test_txid001_full_txids.py), and inside explorer hrefs
(TCK-CHAT-006, untouched).

Pinned here: the CLI compact shape itself; the direct-ask answers in BOTH
referent shapes (registry resolve + the flow's broadcast record — engine
truth, value-free) and their never-trap releases; the watch drain's
same-window stamping; the payload rules (dedup, absence, the rows-present
surface emitting NO txid_refs); and the CHAT-006 re-adjudication (the
label was already compact, the URL keeps the full id).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.app import (
    _CPFP_PARENT_LINE,
    _TRANSCRIPT_HELP,
    _TXID_ASK_NO_COINS,
    _TXID_ASK_NO_ID_YET,
    _TXID_ASK_NONE_PENDING,
    _TXID_COMPACT_HEX,
    ADDRESS_REF_UNKNOWN,
    EVENT_TEXT,
    EVENT_TURN_END,
    EVENT_TXID_REFS,
    EVENT_UTXO_ROWS,
    EventEmitter,
    TxFlow,
    TxFlowStatus,
    _drain_watch,
    _explorer_request,
    _print_broadcast_tx,
    _print_history,
    _print_utxos,
    _run_network_status_turn,
    _run_txid_ask_turn,
    _stamp_watch_first_sighting,
    _txid_ask,
    _txid_compact,
    _utxo_render_rows,
    cli_sink,
)
from localwallet.chain import IncomingWatcher, WatchedTx
from localwallet.store import Store, UtxoRecord
from localwallet.tx.flow import GateDecision
from localwallet.wallet import scan as wallet_scan
from localwallet.wallet.descriptor import WalletDescriptor
from tests.test_e2e_skeleton import ZPUB, derive_fixture_addresses
from tests.test_tx_flow import stage

TXID = "d2c5204c3420" + "ab" * 26
OTHER = "e" * 64
COMPACT = TXID[:8] + "\u2026"
FULL64_STANDALONE = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")


class _Capture:
    def __init__(self) -> None:
        self.events: list = []
        self.emitter = EventEmitter(self.events.append)

    @property
    def lines(self) -> list[str]:
        return [e.payload for e in self.events if e.kind == EVENT_TEXT]

    def refs(self) -> list[dict[str, object]]:
        payloads = [e.payload for e in self.events if e.kind == EVENT_TXID_REFS]
        assert len(payloads) <= 1, "one txid_refs event per surface"
        return json.loads(payloads[0]) if payloads else []


# ------------------------------------------------- the pinned CLI shape


def test_cli_compact_form_is_first_8_hex_plus_ellipsis() -> None:
    # THE shape pin (the ticket: "state the chosen CLI shape and pin it"):
    # 8 lowercase hex + the ellipsis, nothing else — the where-is-the-full-
    # id guidance lives ONCE in the help line (pinned below), not on every
    # transcript line.
    assert _TXID_COMPACT_HEX == 8
    assert _txid_compact(TXID) == "d2c5204c…"
    assert len(_txid_compact(TXID)) == 9


def test_compact_honesty_table() -> None:
    # Empty keeps the honest marker (the TXID-001 dedup fix); a NON-64-hex
    # shape passes through verbatim — never a prefix that could not be
    # re-matched against the real id.
    assert _txid_compact("") == "<unknown>"
    assert _txid_compact("deadbeef") == "deadbeef"
    assert _txid_compact("A" * 64) == "A" * 64  # upper is not the canonical
    # a canonical id NEVER survives as a 64-hex wall in the compact text
    assert FULL64_STANDALONE.search(_txid_compact(TXID)) is None


def test_help_line_points_at_the_full_id_paths() -> None:
    # CLI discoverability (no buttons, no chips): the help line names the
    # direct ask once; the /details clause predates this ticket and stays.
    assert "/details — reprint the pending transaction's full card" in _TRANSCRIPT_HELP
    assert "transaction id for utxo #N" in _TRANSCRIPT_HELP
    assert "pending transaction" in _TRANSCRIPT_HELP


# ------------------------------------------------------ the direct-ask parse


def test_ask_parse_closed_grammar() -> None:
    assert _txid_ask("what is the transaction id for utxo #1?") == 1
    assert _txid_ask("Transaction ID for the pending transaction?") == "pending"
    assert _txid_ask("give me the txid for coin #12") == 12
    # releases (never-trap): no referent, a raw-token line (the
    # tx_status route), no id phrase, MULTIPLE numbers (never half-run).
    assert _txid_ask("what is a txid?") is None
    assert _txid_ask(f"open the explorer for {TXID}") is None
    assert _txid_ask("is the pending transaction confirmed?") is None
    assert _txid_ask("transaction ids for utxo #1 and #2?") is None


def _store() -> tuple[Store, int]:
    store = Store.memory()
    wallet = store.create_wallet(
        "default", WalletDescriptor.from_key(ZPUB).descriptor
    )
    store.set_setting("active_wallet_id", str(wallet.id))
    return store, wallet.id


def _seed(store: Store, wallet_id: int, address: str, coins: list[tuple[str, int]]) -> None:
    store.replace_utxos_for_wallet(
        wallet_id,
        [
            UtxoRecord(
                wallet_id=wallet_id,
                txid=txid,
                vout=vout,
                address=address,
                value_sats=5_000 + vout,
                confirmed=1,
                height=800_000,
            )
            for txid, vout in coins
        ],
    )
    # showing = the registry's only writer (CHAT-001): gives address #1.
    store.note_address_shown(wallet_id, address)


def _ask(
    line: str, flow: TxFlow | None = None, store: Store | None = None
) -> list[str]:
    out: list[str] = []
    assert _run_txid_ask_turn(store, flow or TxFlow(), line, out.append) is True
    return out


class TestDirectAsk:
    def test_registry_ask_prints_the_full_id(self) -> None:
        store, wid = _store()
        try:
            addr = derive_fixture_addresses(1)[0]
            _seed(store, wid, addr, [(TXID, 0)])
            (line,) = _ask("what is the transaction id for utxo #1?", store=store)
            assert line == f"Transaction id for utxo #1: {TXID}"
            # THE full value, verbatim, as a STANDALONE 64-hex token — the
            # shipped client copy scan (TXID-001) makes it click-to-copy.
            assert TXID in FULL64_STANDALONE.findall(line)
            assert COMPACT not in line
            # value-free: no amounts, no address echo in the answer.
            assert "5000" not in line and addr not in line
        finally:
            store.close()

    def test_multi_coin_registry_ask_names_every_vout(self) -> None:
        store, wid = _store()
        try:
            addr = derive_fixture_addresses(1)[0]
            _seed(store, wid, addr, [(OTHER, 1), (TXID, 0)])
            lines = _ask("what is the transaction id for utxo #1?", store=store)
            assert lines == [
                f"Transaction id for utxo #1 (vout 0): {TXID}",
                f"Transaction id for utxo #1 (vout 1): {OTHER}",
            ]  # deterministic (txid, vout) order; never a guess which one
        finally:
            store.close()

    def test_unknown_number_reuses_the_established_clarify(self) -> None:
        store, _wid = _store()
        try:
            (line,) = _ask("what is the transaction id for utxo #9?", store=store)
            assert line == ADDRESS_REF_UNKNOWN
        finally:
            store.close()

    def test_spent_through_number_is_honest(self) -> None:
        store, wid = _store()
        try:
            addr = derive_fixture_addresses(1)[0]
            store.note_address_shown(wid, addr)  # registered, zero coins
            (line,) = _ask("what is the transaction id for utxo #1?", store=store)
            assert line == _TXID_ASK_NO_COINS.format(number=1)
        finally:
            store.close()

    def test_pending_ask_broadcast_prints_full_id(self) -> None:
        flow = TxFlow()
        pending = stage(flow)
        flow.confirm(
            pending.tx_ref, gate_decision=GateDecision.CONFIRM, at=pending.created_at
        )
        flow.mark_signed(pending.tx_ref, "cHNidP8-signed")
        flow.broadcast(pending.tx_ref, TXID)
        assert flow.state is TxFlowStatus.BROADCAST
        store, _wid = _store()
        try:
            (line,) = _ask(
                "transaction id for the pending transaction?", flow=flow, store=store
            )
            assert line == f"Transaction id for the pending transaction: {TXID}"
            assert TXID in FULL64_STANDALONE.findall(line)
            assert COMPACT not in line
        finally:
            store.close()

    def test_pending_ask_staged_says_no_id_yet(self) -> None:
        # Engine truth: the flow records an id ONLY at broadcast — a
        # staged plan gets the honest line, never a computed guess.
        flow = TxFlow()
        stage(flow)
        assert flow.state is TxFlowStatus.CREATED
        store, _wid = _store()
        try:
            (line,) = _ask(
                "transaction id for the pending transaction?", flow=flow, store=store
            )
            assert line == _TXID_ASK_NO_ID_YET
        finally:
            store.close()

    def test_pending_ask_with_nothing_pending(self) -> None:
        store, _wid = _store()
        try:
            (line,) = _ask(
                "transaction id for the pending transaction?",
                flow=TxFlow(),
                store=store,
            )
            assert line == _TXID_ASK_NONE_PENDING
        finally:
            store.close()

    def test_ask_releases_without_store(self) -> None:
        out: list[str] = []
        assert (
            _run_txid_ask_turn(None, TxFlow(), "txid for utxo #1?", out.append)
            is False
        )
        assert out == []


# ------------------------------------------------------ watch-drain stamping


def _watch_probe(txid: str) -> IncomingWatcher:
    watched = WatchedTx(
        txid=txid,
        incoming=True,
        confirmed=False,
        height=None,
        block_time=None,
        address="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kxxs9lv",
        amount_sats=5001,
    )
    t = [0.0]

    def probe() -> list[WatchedTx]:
        return [watched]

    watcher = _stamp_watch_first_sighting(
        IncomingWatcher(probe, interval_s=60.0, clock=lambda: t[0])
    )
    t[0] = 60.0  # first drain is due (unconfirmed receives always surface)
    return watcher


def _watch_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "txid002.db")
    wallet = store.create_wallet("main", "dummy-descriptor")
    store.set_setting("active_wallet_id", str(wallet.id))
    store.set_sync_state(wallet.id, wallet_scan.TIP_KEY, "800000")
    return store


def test_watch_drain_compact_line_with_preceding_refs(tmp_path: Path) -> None:
    # Web shape: each watch line is ONE closed bubble (the pump's
    # _narrate_line), so the payload rides the same window BEFORE the
    # line — the client matches by token within the window, never order.
    store = _watch_store(tmp_path)
    try:
        cap = _Capture()

        def narrate(line: str) -> None:
            # the pump's _narrate_line shape (WEB-011 fold): each watch
            # line closes its own bubble, so the refs stamped BEFORE the
            # call ride the SAME window as the line.
            cap.emitter.text(line)
            cap.emitter.emit(EVENT_TURN_END)

        count = _drain_watch(
            _watch_probe(TXID), narrate, store=store, emitter=cap.emitter
        )
        assert count == 1
        kinds = [e.kind for e in cap.events]
        assert kinds.index(EVENT_TXID_REFS) < kinds.index(EVENT_TEXT)
        assert kinds[-1] == EVENT_TURN_END
        (line,) = cap.lines
        assert f"(in mempool, tx {COMPACT})." in line
        assert TXID not in line
        (entry,) = cap.refs()
        assert entry == {"compact": COMPACT, "txid": TXID, "txid_copy_only": True}
    finally:
        store.close()


def test_watch_cli_sink_text_only(tmp_path: Path) -> None:
    # The CLI emitter exists (the pump always has one) but the cli sink
    # ignores the kind: the terminal gets the compact TEXT and nothing
    # else.
    store = _watch_store(tmp_path)
    try:
        out: list[str] = []
        emitter = EventEmitter(cli_sink(out.append))
        _drain_watch(_watch_probe(TXID), emitter.text, store=store, emitter=emitter)
        assert len(out) == 1
        assert f"tx {COMPACT}" in out[0] and TXID not in out[0]
    finally:
        store.close()


# --------------------------------------------------------- CHAT-006 re-check


def test_explorer_lines_unchanged_compact_label_full_href() -> None:
    # RE-ADJUDICATED (the ticket: "Do NOT change explorer link
    # construction"): the visible label was ALREADY the compact word
    # "Transaction" (no hex wall to remove), and the URL keeps the full
    # txid inside the href. This pin proves TXID-002 changed nothing:
    # label constant, closed path over the validated token, and the
    # ordinary line print rides the unchanged text channel (no refs
    # event — the anchor IS the affordance for the static half).
    links = _explorer_request(f"open the explorer for {TXID}")
    assert links == [{"label": "Transaction", "url": f"https://mempool.space/tx/{TXID}"}]
    out: list[str] = []
    assert _run_network_status_turn(None, f"open mempool for {TXID}", out.append)
    assert out[-1] == f"Transaction: https://mempool.space/tx/{TXID}"


# ----------------------------------------------------------- payload rules


def test_one_entry_per_distinct_txid_deduped() -> None:
    # History showing the SAME tx twice registers ONE entry (keyed by the
    # full id; first-print order kept).
    cap = _Capture()
    _print_history(
        {
            "transactions": [
                {"txid": TXID, "direction": "in", "height": 1},
                {"txid": TXID, "direction": "out", "height": None},
                {"txid": OTHER, "direction": "in", "height": 2},
            ]
        },
        cap.emitter.text,
        emitter=cap.emitter,
    )
    assert [e["txid"] for e in cap.refs()] == [TXID, OTHER]
    assert all(e["txid_copy_only"] is True for e in cap.refs())


def test_no_canonical_ids_no_event() -> None:
    # A surface with only <unknown>/legacy-stub shapes registers nothing:
    # NO txid_refs event at all (the UTXO-005 absence rule — absent key =
    # old answer, the client renders raw text).
    cap = _Capture()
    _print_history(
        {"transactions": [{"direction": "out"}, {"txid": "stub", "direction": "in"}]},
        cap.emitter.text,
        emitter=cap.emitter,
    )
    assert cap.refs() == []
    assert [e.kind for e in cap.events if e.kind == EVENT_TXID_REFS] == []
    assert cap.lines == ["tx <unknown> out unconfirmed", "tx stub in unconfirmed"]


def test_rows_present_surface_emits_no_txid_refs() -> None:
    # The get_utxos web surface is the UTXO-006 ROW chip's own: with
    # utxo_rows in the result, _print_utxos ships the rows event and NO
    # txid_refs (one chip mechanism per rendered surface; the row shape
    # is contract-frozen by this ticket — its txid stays the FULL value).
    cap = _Capture()
    utxos = [{"txid": TXID, "vout": 0, "value_sats": 5000, "confirmed": True}]
    _print_utxos(
        {"utxos": utxos, "count": 1, "utxo_rows": _utxo_render_rows(utxos)},
        cap.emitter.text,
        emitter=cap.emitter,
    )
    kinds = [e.kind for e in cap.events]
    assert EVENT_TXID_REFS not in kinds
    assert kinds[-1] == EVENT_UTXO_ROWS  # the 005/006 order pin stands
    (row_json,) = [
        json.loads(e.payload) for e in cap.events if e.kind == EVENT_UTXO_ROWS
    ]
    assert row_json[0]["txid"] == TXID  # UNCHANGED row shape
    assert row_json[0]["txid_copy_only"] is True


def test_rows_absent_fallback_gets_line_chip() -> None:
    # The pre-model filtered listings build no rows (UTXO-006 precedent:
    # text-only surfaces): their compact bubbles DO get txid_refs.
    cap = _Capture()
    _print_utxos(
        {"utxos": [{"txid": TXID, "vout": 3, "value_sats": 1000, "confirmed": True}]},
        cap.emitter.text,
        emitter=cap.emitter,
    )
    assert [e.kind for e in cap.events if e.kind == EVENT_UTXO_ROWS] == []
    (entry,) = cap.refs()
    assert entry["txid"] == TXID
    assert any(f"tx {COMPACT} vout 3" in line for line in cap.lines)


def test_turn_renderers_stamp_refs_after_their_lines() -> None:
    # Mid-turn surfaces ride the utxo_rows precedent: lines first, the
    # typed payload after them (the web bubble window closes at
    # turn_end — the pump owns the marker; this pins the order inside).
    cap = _Capture()
    _print_broadcast_tx(
        {"status": "broadcast", "txid": TXID}, cap.emitter.text, emitter=cap.emitter
    )
    kinds = [e.kind for e in cap.events]
    assert kinds.index(EVENT_TEXT) < kinds.index(EVENT_TXID_REFS)


def test_plan_cache_swap_uses_the_shared_template() -> None:
    # The /details swap copies the SAME template with the full value:
    # only the txid segment differs, so the surrounding hedge wording is
    # one constant (a future edit of the copy fails everywhere at once).
    assert "{parent_txid}" in _CPFP_PARENT_LINE
    full = _CPFP_PARENT_LINE.format(parent_txid=OTHER)
    compact = _CPFP_PARENT_LINE.format(parent_txid=OTHER[:8] + "…")
    assert full != compact
    assert OTHER in full and OTHER not in compact

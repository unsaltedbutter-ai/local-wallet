"""TCK-UTXO-006 (ENGINE HALF): txid→copy-payload + arrival date on the rows.

Pins the two additive typed-row changes on the ``get_utxos`` handler's
``utxo_rows`` payload and the CLI fallback line:

1. ``txid`` stays on every row verbatim (the copy button's clipboard
   payload) and gains the row-level marker ``txid_copy_only: True`` — the
   row no longer REQUIRES the txid as rendered text (the static half
   swaps the text for the compact copy button).
2. ``arrival`` (closed shape: UTC ``YYYY-MM-DD`` or the literal
   ``pending``) rides every handler row, computed from the tx-row
   provenance join the handler already performs: confirmed = the tx
   row's ``block_time``, unconfirmed = its ``first_seen``, unresolvable =
   the honest pending marker — never a fabricated date (no wall clock is
   read for a coin's arrival).
   The CLI line gains one `` · arrived <value>`` segment; entries
   WITHOUT the key (legacy/filtered shapes) print byte-identically.

The store fixtures seed tx rows with fixed epochs; the expected dates are
fixed literals (1_700_000_000 -> "2023-11-14", 1_750_000_000 ->
"2025-06-15", UTC), so a wall-clock "today" could never satisfy them.
"""

from __future__ import annotations

import re

from localwallet.app import (
    _arrival_label,
    _print_utxos,
    _utxo_render_rows,
)
from localwallet.store import Store, TxRecord
from tests.test_e2e_skeleton import derive_fixture_addresses
from tests.test_utxo005_render_rows import _ask, _narrate, _store_with_wallet, _utxo

BLOCK_TIME: int = 1_700_000_000  # 2023-11-14 UTC
FIRST_SEEN: int = 1_750_000_000  # 2025-06-15 UTC

ARRIVAL_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}|pending)$")


def _seed_tx(
    store: Store, wid: int, txid: str, *, height: int | None,
    block_time: int | None = None, first_seen: int | None = None,
) -> None:
    store.upsert_txs(
        [TxRecord(wid, txid, height, block_time, None, "in", None, first_seen=first_seen)]
    )


class TestTxidCopyPayload:
    def test_row_keeps_full_txid_and_copy_only_marker(self) -> None:
        store, wid = _store_with_wallet()
        a0 = derive_fixture_addresses(1)[0]
        store.replace_utxos_for_wallet(wid, [_utxo(wid, "a" * 64, 0, a0, 100, 1)])
        row = _ask(store, wid)["utxo_rows"][0]
        assert row["txid"] == "a" * 64  # full 64-hex: the button's value
        assert row["txid_copy_only"] is True

    def test_helper_marks_every_row(self) -> None:
        rows = _utxo_render_rows(
            [{"txid": "b" * 64, "value_sats": 1, "confirmed": False}]
        )
        assert rows[0]["txid"] == "b" * 64
        assert rows[0]["txid_copy_only"] is True


class TestArrivalClosedShape:
    def test_confirmed_coin_gets_block_time_date(self) -> None:
        store, wid = _store_with_wallet()
        a0 = derive_fixture_addresses(1)[0]
        store.replace_utxos_for_wallet(wid, [_utxo(wid, "a" * 64, 0, a0, 100, 1)])
        _seed_tx(store, wid, "a" * 64, height=800_000, block_time=BLOCK_TIME)
        row = _ask(store, wid)["utxo_rows"][0]
        assert row["arrival"] == "2023-11-14"  # fixed literal: never "today"

    def test_unconfirmed_coin_gets_first_seen_date(self) -> None:
        store, wid = _store_with_wallet()
        a0 = derive_fixture_addresses(1)[0]
        store.replace_utxos_for_wallet(wid, [_utxo(wid, "u" * 64, 0, a0, 100, 0)])
        _seed_tx(store, wid, "u" * 64, height=None, first_seen=FIRST_SEEN)
        row = _ask(store, wid)["utxo_rows"][0]
        assert row["arrival"] == "2025-06-15"

    def test_unconfirmed_without_first_seen_is_pending(self) -> None:
        store, wid = _store_with_wallet()
        a0 = derive_fixture_addresses(1)[0]
        store.replace_utxos_for_wallet(wid, [_utxo(wid, "n" * 64, 0, a0, 100, 0)])
        _seed_tx(store, wid, "n" * 64, height=None)  # tx row, no capture
        assert _ask(store, wid)["utxo_rows"][0]["arrival"] == "pending"

    def test_confirmed_without_block_time_is_pending_never_a_date(self) -> None:
        # Fail-OPEN to the honest marker (store-truth gap), never a
        # wall-clock date stamped onto a coin the store cannot vouch for.
        store, wid = _store_with_wallet()
        a0 = derive_fixture_addresses(1)[0]
        store.replace_utxos_for_wallet(wid, [_utxo(wid, "m" * 64, 0, a0, 100, 1)])
        _seed_tx(store, wid, "m" * 64, height=800_000)  # block_time NULL
        assert _ask(store, wid)["utxo_rows"][0]["arrival"] == "pending"

    def test_no_tx_row_at_all_is_pending(self) -> None:
        store, wid = _store_with_wallet()
        a0 = derive_fixture_addresses(1)[0]
        store.replace_utxos_for_wallet(wid, [_utxo(wid, "z" * 64, 0, a0, 100, 1)])
        assert _ask(store, wid)["utxo_rows"][0]["arrival"] == "pending"

    def test_shape_is_closed_over_a_listing(self) -> None:
        store, wid = _store_with_wallet()
        a0, a1 = derive_fixture_addresses(2)
        store.replace_utxos_for_wallet(
            wid,
            [
                _utxo(wid, "a" * 64, 0, a0, 100, 1),
                _utxo(wid, "u" * 64, 0, a1, 100, 0),
            ],
        )
        _seed_tx(store, wid, "a" * 64, height=800_000, block_time=BLOCK_TIME)
        for row in _ask(store, wid)["utxo_rows"]:
            assert ARRIVAL_RE.match(row["arrival"])

    def test_helper_passthrough_only(self) -> None:
        # The helper reshapes what the caller resolved: a date string is
        # copied verbatim, a missing/empty arrival leaves the row in the
        # legacy shape (never a fetched or fabricated one).
        rows = _utxo_render_rows(
            [
                {"txid": "a" * 64, "value_sats": 1, "confirmed": True,
                 "arrival": "2023-11-14"},
                {"txid": "b" * 64, "value_sats": 1, "confirmed": True},
                {"txid": "c" * 64, "value_sats": 1, "confirmed": True,
                 "arrival": ""},
            ]
        )
        assert rows[0]["arrival"] == "2023-11-14"
        assert "arrival" not in rows[1]
        assert "arrival" not in rows[2]

    def test_arrival_label_is_pure_utc_formatting(self) -> None:
        assert _arrival_label(None) == "pending"
        assert _arrival_label(BLOCK_TIME) == "2023-11-14"


class TestCliArrivalSegment:
    def test_date_rides_the_line(self) -> None:
        store, wid = _store_with_wallet()
        a0 = derive_fixture_addresses(1)[0]
        store.replace_utxos_for_wallet(
            wid, [_utxo(wid, "a" * 64, 0, a0, 10_000_000, 1)]
        )
        _seed_tx(store, wid, "a" * 64, height=800_000, block_time=BLOCK_TIME)
        (line,) = [ln for ln in _narrate(_ask(store, wid)) if " sats · " in ln]
        # TXID-002 RE-ADJUDICATION: the tx segment prints the COMPACT
        # token; the arrival segment — this file's subject — rides verbatim.
        assert line == (
            f"#1 {a0} · 10,000,000 sats · confirmed · arrived 2023-11-14 · "
            f"tx {'a' * 8}… vout 0"
        )

    def test_pending_marker_rides_the_line(self) -> None:
        store, wid = _store_with_wallet()
        a0 = derive_fixture_addresses(1)[0]
        store.replace_utxos_for_wallet(
            wid, [_utxo(wid, "u" * 64, 1, a0, 500, 0)]
        )
        _seed_tx(store, wid, "u" * 64, height=None, first_seen=FIRST_SEEN)
        (line,) = [ln for ln in _narrate(_ask(store, wid)) if " sats · " in ln]
        assert line == (
            f"#1 {a0} · 500 sats · unconfirmed · arrived 2025-06-15 · "
            f"tx {'u' * 64} vout 1"
        )

    def test_entry_without_arrival_prints_byte_identical(self) -> None:
        # Legacy/filtered shapes (no arrival key): the pre-006 line,
        # byte for byte — the segment rides the data, never a guess.
        lines: list[str] = []
        _print_utxos(
            {"utxos": [{"txid": "a" * 64, "vout": 0, "value_sats": 2500,
                        "address": None, "confirmed": True}], "count": 1},
            lines.append,
        )
        # TXID-002 RE-ADJUDICATION: compact tx token in the fallback
        # line; the no-arrival shape still prints WITHOUT the segment.
        assert f"2,500 sats · confirmed · tx {'a' * 8}… vout 0" in lines

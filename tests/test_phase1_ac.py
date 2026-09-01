"""Phase 1 acceptance-criteria harness (TCK-P1-006).

The literal Phase 1 AC (PROJECT.md §12: "balance/UTXO/history match
Electrum + mempool.space on a testnet wallet with known history including
>20-address gaps; rescan fixes a simulated stale cache; unit tests for
prefix→script-type mapping") needs a funded testnet wallet. This module is
its OFFLINE composite story: the full explorer→scan→store→handler chain is
exercised against a HAND-CONSTRUCTED ground truth ("explorer view") served
via ``httpx.MockTransport`` — no network in the default run. The LIVE
procedure for the literal AC is documented in ``docs/phase1-ac.md``; the
env-gated live cross-check test sits at the bottom of this file.

Ground truth (the fixture wallet is the deterministic vpub from
tests/test_wallet_scan.py; usage deliberately spans both branches and
includes a used address at index 25 — inside a gap=30 scan window, outside
a gap=20 one):

  branch 0 (receive): mempool top-up at index 1 (unconfirmed), funding at
  index 3 (later spent → change at branch 1 index 4), DEEP funding at
  index 25 (confirmed 200_000 sats); branch 1 (change): funding + spend at
  index 2, live change UTXO at index 4.

AC coverage map (PROJECT.md §12 Phase 1 AC line → tests here):

- "balance/UTXO/history match ... known history including >20-address
  gaps" → ``test_ac1_known_history_store_matches_explorer_truth`` (app's
  store view == ground truth EXACTLY), ``test_ac3_gap20_misses_deep_usage...
  `` (>20-gap end-to-end, R3 semantics), ``test_ac4_store_view_narration_
  inputs_match_truth`` (the three store-view narration inputs), and the
  env-gated ``test_live_...`` cross-check for the literal explorer pass.
- "rescan fixes a simulated stale cache" →
  ``test_ac2_rescan_repairs_stale_cache_to_ground_truth``.
- "unit tests for prefix→script-type mapping" → already covered by
  ``tests/test_wallet_descriptor.py::test_prefix_matrix`` — referenced,
  NOT duplicated here.

Fixture builders (``tx_entry``/``utxo_entry``/``FakeChain``/derived
address tables) are imported from tests/test_wallet_scan.py (same test
package) — keep in sync with the Esplora payload shapes the chain/scan
modules validate. NOTE: sat ``value`` fields inside ``tx_entry`` bodies
are inert for the scan (only ``scriptpubkey_address`` sets are read);
the address/utxo endpoint carries the authoritative UTXO values.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Final

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.app import build_dispatch_table
from localwallet.chain import EsploraClient
from localwallet.protocol import IntentName, validate_payload
from localwallet.store import (
    ADDRESS_ALLOCATED,
    ADDRESS_USED,
    AddressRecord,
    Store,
    UtxoRecord,
    WalletRecord,
)
from localwallet.wallet import (
    OUT_OF_WINDOW_KEY,
    WalletDescriptor,
    rescan_wallet,
    scan_wallet,
)
from localwallet.wallet.scan import CURSOR_KEY, SCAN_AT_KEY, TIP_KEY
from tests.test_wallet_scan import (
    _EXTERNAL,
    ADDRS,
    TIP,
    WD,
    FakeChain,
    tx_entry,
    utxo_entry,
)

# --------------------------------------------------------------------------
# Ground truth ("explorer view") — hand-constructed, deterministic.
# --------------------------------------------------------------------------

GAP_WIDE: Final = 30
GAP_NARROW: Final = 20

# Transaction ids (64-hex, synthetic).
TX_FUND_R3: Final = "aa" * 32  # in: external -> receive[3]
TX_SPEND_SELF: Final = "bb" * 32  # self: spend receive[3] -> change[4] + external
TX_FUND_C2: Final = "ff" * 32  # in: external -> change[2]
TX_SPEND_OUT: Final = "ee" * 32  # out: spend change[2] -> external
TX_FUND_R25: Final = "cc" * 32  # in: external -> receive[25] (the >20-gap resident)
TX_MEMO_R1: Final = "dd" * 32  # in: external -> receive[1] (unconfirmed, fee absent)

BLOCK_TIME: Final = 1_700_000_000

#: (txid, direction, height, block_time, fee_sats) — full history truth,
#: in store order (txid ASC). The mempool entry keeps ``fee=None``:
#: a missing fee is tolerated, never fabricated.
TX_TRUTH: Final[tuple[tuple[str, str, int | None, int | None, int | None], ...]] = (
    (TX_FUND_R3, "in", 800_050, BLOCK_TIME, 500),
    (TX_SPEND_SELF, "self", 800_060, BLOCK_TIME, 500),
    (TX_FUND_R25, "in", 800_200, BLOCK_TIME, 750),
    (TX_MEMO_R1, "in", None, None, None),
    (TX_SPEND_OUT, "out", 800_080, BLOCK_TIME, 1_000),
    (TX_FUND_C2, "in", 800_070, BLOCK_TIME, 400),
)

#: (txid, vout, value_sats, confirmed, height, address) — UTXO snapshot
#: truth, in store order (txid, vout).
UTXO_TRUTH: Final[tuple[tuple[str, int, int, int, int | None, str], ...]] = (
    (TX_SPEND_SELF, 0, 50_000, 1, 800_060, ADDRS[1][4]),
    (TX_FUND_R25, 0, 200_000, 1, 800_200, ADDRS[0][25]),
    (TX_MEMO_R1, 0, 12_345, 0, None, ADDRS[0][1]),
)
CONFIRMED_TRUTH: Final = 250_000  # 50_000 + 200_000
UNCONFIRMED_TRUTH: Final = 12_345
TOTAL_TRUTH: Final = CONFIRMED_TRUTH + UNCONFIRMED_TRUTH

#: Per-branch usage truth.
USED_B0: Final = (1, 3, 25)
USED_B1: Final = (2, 4)
#: gap-30 windows: last used index + gap (25 + 30 and 4 + 30).
WINDOW_B0: Final = 55
WINDOW_B1: Final = 34
#: sync cursor = stop index + 1 per branch.
CURSOR_TRUTH: Final = {"0": 56, "1": 35}

#: gap-20 window truth (index 25 NOT reachable: branch 0 stops at 3 + 20).
NARROW_WINDOW_B0: Final = 23
NARROW_WINDOW_B1: Final = 24
NARROW_CURSOR: Final = {"0": 24, "1": 25}
NARROW_UTXO_TRUTH: Final = (
    (TX_SPEND_SELF, 0, 50_000, 1, 800_060, ADDRS[1][4]),
    (TX_MEMO_R1, 0, 12_345, 0, None, ADDRS[0][1]),
)
NARROW_TX_TRUTH: Final = tuple(t for t in TX_TRUTH if t[0] != TX_FUND_R25)
NARROW_CONFIRMED: Final = 50_000
NARROW_TOTAL: Final = NARROW_CONFIRMED + UNCONFIRMED_TRUTH

STALE_ADDRESS: Final = "tb1qstalerowthatneverexistedonchain000000000000"


def _truth_chain() -> FakeChain:
    """The ground-truth Esplora backend: address/txs + address/utxo + tip.

    Every transaction is built ONCE and the same entry object is served for
    each of its addresses (first-sighting dedup in the scanner must see
    consistent vin/vout sets).
    """
    fund_r3 = tx_entry(
        TX_FUND_R3, vout_addresses=(ADDRS[0][3],), fee=500, height=800_050
    )
    spend_self = tx_entry(
        TX_SPEND_SELF,
        vin_addresses=(ADDRS[0][3],),
        vout_addresses=(ADDRS[1][4], _EXTERNAL),
        fee=500,
        height=800_060,
    )
    fund_c2 = tx_entry(
        TX_FUND_C2, vout_addresses=(ADDRS[1][2],), fee=400, height=800_070
    )
    spend_out = tx_entry(
        TX_SPEND_OUT, vin_addresses=(ADDRS[1][2],), fee=1_000, height=800_080
    )
    fund_r25 = tx_entry(
        TX_FUND_R25, vout_addresses=(ADDRS[0][25],), fee=750, height=800_200
    )
    memo_r1 = tx_entry(
        TX_MEMO_R1, vout_addresses=(ADDRS[0][1],), fee=None, confirmed=False
    )
    return FakeChain(
        txs={
            ADDRS[0][1]: [memo_r1],
            ADDRS[0][3]: [fund_r3, spend_self],
            ADDRS[0][25]: [fund_r25],
            ADDRS[1][2]: [fund_c2, spend_out],
            ADDRS[1][4]: [spend_self],
        },
        utxos={
            ADDRS[0][1]: [utxo_entry(TX_MEMO_R1, 0, 12_345, confirmed=False)],
            ADDRS[0][25]: [utxo_entry(TX_FUND_R25, 0, 200_000, height=800_200)],
            ADDRS[1][4]: [utxo_entry(TX_SPEND_SELF, 0, 50_000, height=800_060)],
        },
    )


# --------------------------------------------------------------------------
# Store-vs-truth comparators (each asserts one dimension of store state).
# --------------------------------------------------------------------------


@pytest.fixture()
def store() -> Iterator[Store]:
    with Store.memory() as s:
        wallet = s.create_wallet("main", WD.descriptor)
        s.set_active_wallet(wallet.id)
        yield s


def _wallet_row(store: Store) -> WalletRecord:
    row = store.get_wallet_by_name("main")
    assert row is not None
    return row


def _assert_window_truth(
    store: Store,
    wid: int,
    *,
    window_b0: int,
    window_b1: int,
    used_b0: tuple[int, ...],
    used_b1: tuple[int, ...],
    allocated_b0: frozenset[int] = frozenset(),
) -> None:
    """Address rows == ground truth: window extent, mappings, statuses."""
    script_type = WD.parsed.script_type
    for branch, window_last, used, allocated in (
        (0, window_b0, used_b0, allocated_b0),
        (1, window_b1, used_b1, frozenset()),
    ):
        rows = store.get_addresses(wid, branch)
        assert [r.index for r in rows] == list(range(window_last + 1))
        assert [r.address for r in rows] == ADDRS[branch][: window_last + 1]
        used_set = set(used)
        for row in rows:
            assert (row.wallet_id, row.branch) == (wid, branch)
            assert row.script_type == script_type
            if row.index in used_set:
                expected = ADDRESS_USED
            elif row.index in allocated:
                expected = ADDRESS_ALLOCATED
            else:
                expected = "unused"
            assert row.status == expected


def _assert_utxo_truth(store: Store, wid: int) -> None:
    rows = store.get_utxos_for_wallet(wid)
    assert [(r.txid, r.vout, r.value_sats, r.confirmed, r.height, r.address) for r in rows] == [
        tuple(t) for t in UTXO_TRUTH
    ]


def _assert_narrow_utxo_truth(store: Store, wid: int) -> None:
    rows = store.get_utxos_for_wallet(wid)
    assert [(r.txid, r.vout, r.value_sats, r.confirmed, r.height, r.address) for r in rows] == [
        tuple(t) for t in NARROW_UTXO_TRUTH
    ]


def _assert_tx_truth(store: Store, wid: int, *, narrow: bool = False) -> None:
    rows = store.get_txs_for_wallet(wid)
    expected = NARROW_TX_TRUTH if narrow else TX_TRUTH
    assert [(t.txid, t.direction, t.height, t.block_time, t.fee_sats) for t in rows] == [
        tuple(t) for t in expected
    ]


def _assert_balance_truth(
    store: Store, wid: int, *, narrow: bool = False
) -> tuple[int, int]:
    """Balance totals RECOMPUTED FROM THE STORE equal the fixture truth."""
    utxos = store.get_utxos_for_wallet(wid)
    confirmed = sum(u.value_sats for u in utxos if u.confirmed == 1)
    unconfirmed = sum(u.value_sats for u in utxos if u.confirmed != 1)
    if narrow:
        assert (confirmed, unconfirmed) == (NARROW_CONFIRMED, UNCONFIRMED_TRUTH)
        assert confirmed + unconfirmed == NARROW_TOTAL
    else:
        assert (confirmed, unconfirmed) == (CONFIRMED_TRUTH, UNCONFIRMED_TRUTH)
        assert confirmed + unconfirmed == TOTAL_TRUTH
    return confirmed, unconfirmed


def _assert_derivation_truth(
    store: Store,
    wid: int,
    *,
    next_b0: int,
    max_used_b0: int = 25,
    max_used_b1: int = 4,
) -> None:
    d0 = store.get_derivation(wid, 0)
    d1 = store.get_derivation(wid, 1)
    assert (d0.max_used_index, d0.next_index) == (max_used_b0, next_b0)
    assert (d1.max_used_index, d1.next_index) == (max_used_b1, max_used_b1 + 1)


def _assert_sync_truth(
    store: Store, wid: int, *, cursor: dict[str, int], flagged: bool
) -> None:
    assert json.loads(store.get_sync_state(wid, CURSOR_KEY) or "null") == cursor
    assert store.get_sync_state(wid, TIP_KEY) == str(TIP)
    scanned_at = store.get_sync_state(wid, SCAN_AT_KEY)
    assert scanned_at is not None
    assert datetime.fromisoformat(scanned_at).tzinfo is not None
    payload = json.loads(store.get_sync_state(wid, OUT_OF_WINDOW_KEY) or "null")
    if flagged:
        assert payload is not None and payload["detected_at"] is not None
        assert payload["branches"] != {}
    else:
        assert payload == {"detected_at": None, "branches": {}}


def _assert_full_truth(
    store: Store,
    wid: int,
    *,
    allocated_b0: frozenset[int] = frozenset(),
    next_b0: int = 26,
    oow_flagged: bool = False,
) -> None:
    """EVERY store dimension equals the gap-30 ground truth, exactly.

    ``oow_flagged`` selects the out-of-window payload expectation: a rescan
    that found usage beyond the PREVIOUS window persists the R3 warning
    (cleared again once a later scan's window covers observed usage).
    """
    _assert_window_truth(
        store,
        wid,
        window_b0=WINDOW_B0,
        window_b1=WINDOW_B1,
        used_b0=USED_B0,
        used_b1=USED_B1,
        allocated_b0=allocated_b0,
    )
    _assert_utxo_truth(store, wid)
    _assert_tx_truth(store, wid)
    _assert_derivation_truth(store, wid, next_b0=next_b0)
    _assert_sync_truth(store, wid, cursor=CURSOR_TRUTH, flagged=oow_flagged)
    _assert_balance_truth(store, wid)


# --------------------------------------------------------------------------
# AC-1: known-history match (app's store view == explorer truth)
# --------------------------------------------------------------------------


def test_ac1_known_history_store_matches_explorer_truth(store: Store) -> None:
    """AC-1: scan the synthetic known-history wallet (usage at indices 1, 3
    and 25 — the latter inside a gap=30 scan) and assert the store state
    equals the hand-constructed ground truth EXACTLY: per-branch
    max_used_index, address statuses, UTXO set, transaction rows, sync
    cursor, and balance totals recomputed from the store."""
    row = _wallet_row(store)
    summary = scan_wallet(store, _truth_chain().client(), row, gap_limit=GAP_WIDE)

    # Summary-level truth first.
    assert (summary.gap_limit, summary.tip_height) == (GAP_WIDE, TIP)
    b0, b1 = summary.branches[0], summary.branches[1]
    assert (b0.scanned, b0.window_last_index, b0.max_used_index) == (56, WINDOW_B0, 25)
    assert b0.used_indices == USED_B0
    assert b0.next_index == 26
    assert (b1.scanned, b1.window_last_index, b1.max_used_index) == (35, WINDOW_B1, 4)
    assert b1.used_indices == USED_B1
    assert b1.next_index == 5
    assert summary.out_of_window == {}  # first scan: no previous window to exceed
    assert summary.utxo_count == 3
    assert summary.utxo_value_sats == TOTAL_TRUTH

    # Then EVERY persisted dimension, exactly.
    _assert_full_truth(store, row.id)
    # Sanity: the deep index-25 funding really landed (the >20-gap resident).
    rows0 = store.get_addresses(row.id, 0)
    assert rows0[25].status == ADDRESS_USED and rows0[25].address == ADDRS[0][25]
    assert any(r.txid == TX_FUND_R25 and r.value_sats == 200_000 for r in store.get_utxos_for_wallet(row.id))


# --------------------------------------------------------------------------
# AC-2: rescan fixes a simulated stale cache
# --------------------------------------------------------------------------


def test_ac2_rescan_repairs_stale_cache_to_ground_truth(store: Store) -> None:
    """AC-2: corrupt every cacheable dimension on top of a clean scan, run
    rescan_wallet — the store matches ground truth again; next_index is
    floored at the allocated index (26 allocated → next 27); the
    out-of-window flag fires against the stale cursor and a follow-up
    rescan clears it once the window covers observed usage."""
    wid = _wallet_row(store).id
    scan_wallet(store, _truth_chain().client(), _wallet_row(store), gap_limit=GAP_WIDE)

    # --- seed a WRONG stale state (old utxo snapshot, wrong statuses,
    # wrong derivation, wrong cursor, stale warning, junk mapping).
    store.replace_utxos_for_wallet(
        wid,
        [
            UtxoRecord(wid, "f" * 64, 9, "tb1qjunk", 123_456, 1, 5),
            UtxoRecord(wid, "e" * 64, 3, None, 1, 0, None),
        ],
    )
    store.update_derivation(wid, 0, max_used_index=99, next_index=99)
    store.update_derivation(wid, 1, max_used_index=50, next_index=50)
    store.upsert_batch(
        [
            AddressRecord(wid, 0, 5, STALE_ADDRESS, "p2wpkh", ADDRESS_USED),
            # The user was handed a fresh address at index 26 at some point.
            AddressRecord(wid, 0, 26, ADDRS[0][26], "p2wpkh", ADDRESS_ALLOCATED),
        ]
    )
    store.mark_used(wid, 0, 10)  # false 'used'
    store.set_sync_state(wid, CURSOR_KEY, json.dumps({"0": 2, "1": 2}))
    store.set_sync_state(wid, TIP_KEY, "1")
    store.set_sync_state(
        wid,
        OUT_OF_WINDOW_KEY,
        json.dumps({"detected_at": "stale", "branches": {"0": {"max_used_index": 99}}}),
    )

    # --- rescan repairs everything from chain truth.
    first = rescan_wallet(store, _truth_chain().client(), _wallet_row(store), gap_limit=GAP_WIDE)
    # next_index floored at the allocated index: 26 allocated → 27, NOT the
    # chain-truth 26 (TCK-P1-004 flooring — an issued address is never
    # re-issued) and NOT the stale 99.
    assert first.branches[0].next_index == 27
    assert first.out_of_window == {
        "0": {"max_used_index": 25, "previous_window_end": 1},
        "1": {"max_used_index": 4, "previous_window_end": 1},
    }  # computed against the STALE cursor, as documented
    _assert_full_truth(
        store, wid, allocated_b0=frozenset({26}), next_b0=27, oow_flagged=True
    )
    assert store.get_by_address(STALE_ADDRESS) is None  # junk mapping gone
    statuses = {r.index: r.status for r in store.get_addresses(wid, 0)}
    assert statuses[10] == "unused"  # false 'used' recomputed
    assert statuses[5] == "unused"  # stale mapping row recomputed
    assert statuses[26] == ADDRESS_ALLOCATED  # allocation preserved by string

    # --- one more rescan (cursor now truthful): the stale warning clears.
    rescan_wallet(store, _truth_chain().client(), _wallet_row(store), gap_limit=GAP_WIDE)
    _assert_full_truth(store, wid, allocated_b0=frozenset({26}), next_b0=27)


# --------------------------------------------------------------------------
# AC-3: >20-address gap end-to-end (R3)
# --------------------------------------------------------------------------


def test_ac3_gap20_misses_deep_usage_rescan_gap30_finds_it(store: Store) -> None:
    """AC-3 (>20-gap, R3 end-to-end): with gap 20 the used index 25 is NOT
    found — the store matches the gap-20 window truth and the first scan
    never flags out-of-window; a gap-30 rescan finds index 25, the store
    matches the full gap-30 truth, and the beyond-window usage is
    persisted as the documented warning."""
    wid = _wallet_row(store).id
    row = _wallet_row(store)

    narrow = scan_wallet(store, _truth_chain().client(), row, gap_limit=GAP_NARROW)
    b0, b1 = narrow.branches[0], narrow.branches[1]
    assert (b0.scanned, b0.window_last_index, b0.max_used_index) == (
        24,
        NARROW_WINDOW_B0,
        3,
    )
    assert b0.used_indices == (1, 3) and b0.next_index == 4
    assert (b1.scanned, b1.window_last_index, b1.max_used_index) == (
        25,
        NARROW_WINDOW_B1,
        4,
    )
    assert b1.used_indices == USED_B1 and b1.next_index == 5
    assert narrow.out_of_window == {}  # first scan never flags
    _assert_window_truth(
        store,
        wid,
        window_b0=NARROW_WINDOW_B0,
        window_b1=NARROW_WINDOW_B1,
        used_b0=(1, 3),
        used_b1=USED_B1,
    )
    _assert_narrow_utxo_truth(store, wid)  # the 200_000-sat UTXO is invisible
    _assert_tx_truth(store, wid, narrow=True)  # the index-25 tx was never seen
    confirmed, unconfirmed = _assert_balance_truth(store, wid, narrow=True)
    assert (confirmed, confirmed + unconfirmed) == (NARROW_CONFIRMED, NARROW_TOTAL)
    assert json.loads(store.get_sync_state(wid, CURSOR_KEY) or "null") == NARROW_CURSOR

    # Widen the gap and rescan: index 25 found, full truth restored, R3
    # warning persisted with the exact documented payload.
    wide = rescan_wallet(store, _truth_chain().client(), row, gap_limit=GAP_WIDE)
    assert wide.branches[0].max_used_index == 25
    _assert_full_truth(store, wid, oow_flagged=True)
    assert wide.out_of_window == {
        "0": {"max_used_index": 25, "previous_window_end": NARROW_WINDOW_B0}
    }
    persisted = json.loads(store.get_sync_state(wid, OUT_OF_WINDOW_KEY) or "null")
    assert persisted is not None and persisted["detected_at"] is not None
    assert persisted["branches"] == wide.out_of_window


# --------------------------------------------------------------------------
# AC-4: the three store-view narration inputs (explorer→scan→store→handler)
# --------------------------------------------------------------------------


def test_ac4_store_view_narration_inputs_match_truth(store: Store) -> None:
    """AC-4: the store-derived values the narration handlers consume equal
    the fixture truth — balance totals, UTXO rows (addresses verbatim from
    tool output) and history rows. Closes the chain explorer→scan→store→
    handler (→UI) offline. History LIMIT handling is app-layer behavior
    already covered by tests/test_e2e_skeleton.py
    (::test_history_limit_param_and_ordering) — asserted here only once,
    against fixture truth."""
    row = _wallet_row(store)
    scan_wallet(store, _truth_chain().client(), row, gap_limit=GAP_WIDE)
    client = _truth_chain().client()
    table = build_dispatch_table(
        store, row, WD.parsed, client, lambda: scan_wallet(store, client, row)
    )

    # --- balance totals, recomputed by the handler from the store.
    balance = table[IntentName.GET_BALANCE](validate_payload(
        '{"v": 0, "intent": "get_balance", "params": {}}'
    ))
    assert balance == {
        "confirmed_sats": CONFIRMED_TRUTH,
        "unconfirmed_sats": UNCONFIRMED_TRUTH,
        "total_sats": TOTAL_TRUTH,
        "addresses_scanned": 3,  # three addresses hold the UTXO set
        "tip_height": TIP,
    }

    # --- UTXO rows, verbatim (store order: txid, vout).
    utxos = table[IntentName.GET_UTXOS](validate_payload(
        '{"v": 0, "intent": "get_utxos", "params": {}}'
    ))
    assert utxos["count"] == 3
    assert utxos["utxos"] == [
        {
            "txid": TX_SPEND_SELF,
            "vout": 0,
            "address": ADDRS[1][4],
            "value_sats": 50_000,
            "confirmed": True,
        },
        {
            "txid": TX_FUND_R25,
            "vout": 0,
            "address": ADDRS[0][25],
            "value_sats": 200_000,
            "confirmed": True,
        },
        {
            "txid": TX_MEMO_R1,
            "vout": 0,
            "address": ADDRS[0][1],
            "value_sats": 12_345,
            "confirmed": False,
        },
    ]

    # --- history rows: all 6 cached txs, newest first (unconfirmed first).
    history = table[IntentName.GET_HISTORY](validate_payload(
        '{"v": 0, "intent": "get_history", "params": {}}'
    ))
    assert history["shown"] == 6
    assert history["transactions"] == [
        {
            "txid": TX_MEMO_R1,
            "height": None,
            "direction": "in",
            "fee_sats": None,
            "block_time": None,
        },
        {
            "txid": TX_FUND_R25,
            "height": 800_200,
            "direction": "in",
            "fee_sats": 750,
            "block_time": BLOCK_TIME,
        },
        {
            "txid": TX_SPEND_OUT,
            "height": 800_080,
            "direction": "out",
            "fee_sats": 1_000,
            "block_time": BLOCK_TIME,
        },
        {
            "txid": TX_FUND_C2,
            "height": 800_070,
            "direction": "in",
            "fee_sats": 400,
            "block_time": BLOCK_TIME,
        },
        {
            "txid": TX_SPEND_SELF,
            "height": 800_060,
            "direction": "self",
            "fee_sats": 500,
            "block_time": BLOCK_TIME,
        },
        {
            "txid": TX_FUND_R3,
            "height": 800_050,
            "direction": "in",
            "fee_sats": 500,
            "block_time": BLOCK_TIME,
        },
    ]
    # App-layer limit cap over fixture truth (see e2e suite for the matrix).
    limited = table[IntentName.GET_HISTORY](validate_payload(
        '{"v": 0, "intent": "get_history", "params": {"limit": 2}}'
    ))
    assert limited["shown"] == 2
    assert [t["txid"] for t in limited["transactions"]] == [TX_MEMO_R1, TX_FUND_R25]
    client.close()


# --------------------------------------------------------------------------
# LIVE cross-check (env-gated; skipped by default) — literal AC sign-off aid
# --------------------------------------------------------------------------


@pytest.mark.network
@pytest.mark.skipif(
    os.environ.get("LOCALWALLET_E2E_LIVE") != "1",
    reason="live-network test: set LOCALWALLET_E2E_LIVE=1 to include",
)
def test_live_phase1_ac_explorer_crosscheck_sheet() -> None:
    """Real scan_wallet (gap 30) against mempool.space testnet4 for the
    vpub in LOCALWALLET_AC_VPUB; prints a human comparison sheet (balance
    totals, utxo/tx counts, per-branch max_used, first/last window
    address) for the literal Phase 1 AC sign-off (see docs/phase1-ac.md).

    Run (add -s to see the sheet):

        LOCALWALLET_E2E_LIVE=1 LOCALWALLET_AC_VPUB=<vpub> \\
            pytest tests/test_phase1_ac.py -k live -s

    Asserts structural sanity only — the numeric cross-check against
    mempool.space/Electrum is the human's job (value-free automation).
    """
    vpub = os.environ.get("LOCALWALLET_AC_VPUB", "").strip()
    if not vpub:
        pytest.skip("LOCALWALLET_AC_VPUB not set")

    descriptor = WalletDescriptor.from_key(vpub)
    client = EsploraClient()  # default: https://mempool.space/testnet4/api
    try:
        with Store.memory() as store:
            wallet = store.create_wallet("default", descriptor.descriptor)
            store.set_active_wallet(wallet.id)
            summary = scan_wallet(store, client, wallet, gap_limit=GAP_WIDE)
            wid = wallet.id
            utxos = store.get_utxos_for_wallet(wid)
            txs = store.get_txs_for_wallet(wid)
            cursor = store.get_sync_state(wid, CURSOR_KEY)
            branch_rows = {b: store.get_addresses(wid, b) for b in (0, 1)}
            confirmed = sum(u.value_sats for u in utxos if u.confirmed == 1)
            unconfirmed = sum(u.value_sats for u in utxos if u.confirmed != 1)
    finally:
        client.close()

    print("Phase 1 AC cross-check sheet (compare vs mempool.space/Electrum):")
    print(f"  gap_limit: {summary.gap_limit} · tip height: {summary.tip_height}")
    print(
        f"  balance: {confirmed} sats confirmed + {unconfirmed} sats unconfirmed "
        f"= {confirmed + unconfirmed} sats total"
    )
    print(f"  utxo count: {len(utxos)} · tx count: {len(txs)}")
    for branch in (0, 1):
        branch_summary = summary.branches[branch]
        rows = branch_rows[branch]
        first = rows[0].address if rows else "-"
        last = rows[-1].address if rows else "-"
        print(
            f"  branch {branch}: scanned {branch_summary.scanned} · "
            f"max_used_index {branch_summary.max_used_index} · "
            f"next_index {branch_summary.next_index}"
        )
        print(f"    first window address: {first}")
        print(f"    last window address:  {last}")
    print(
        "  Compare each funded address on "
        "https://mempool.space/testnet4/address/<addr> and in Electrum "
        "(see docs/phase1-ac.md for the checklist)."
    )

    # Structural sanity only.
    assert summary.gap_limit == GAP_WIDE
    assert summary.tip_height > 0
    assert confirmed >= 0 and unconfirmed >= 0
    assert summary.utxo_count == len(utxos)
    assert cursor is not None
    for branch in (0, 1):
        branch_summary = summary.branches[branch]
        assert branch_summary.scanned > 0
        assert branch_summary.max_used_index >= -1
        assert branch_summary.next_index >= 0

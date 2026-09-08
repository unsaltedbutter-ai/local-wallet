"""Tests for the gap-limited scan and cache orchestration (TCK-P1-002).

All chain interaction runs over ``httpx.MockTransport`` — no real network.
Covers: the happy-path window (usage at index 3 → stop at 23 with the
default gap of 20) and full store state, request ordering (sequential,
branch 0 before branch 1, indices ascending), gap configurability via the
``gap_limit`` setting (deep usage at index 25 missed with 20 / caught with
30, out-of-window warning persisted), UTXO snapshot replacement, rescan
fixing a simulated stale cache (Phase 1 AC), transaction direction
(in/out/self) with dedup and nullable fee, fail-closed behavior on
malformed chain payloads (store untouched), the atomic persist phase
(a store failure mid-persist rolls back the entire scan write and leaves
the prior state intact), sync-state round-trips, and
fail-closed behavior on
malformed chain payloads (store untouched), the atomic persist phase
(a store failure mid-persist rolls back the entire scan write and leaves
the prior state intact), sync-state round-trips, and
the wallet-input/mainnet-gate surface.
"""

from __future__ import annotations

import inspect
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from embit.bip32 import NETWORKS, HDKey
from embit.descriptor.checksum import add_checksum

from localwallet.chain import ChainError, EsploraClient
from localwallet.store import (
    ADDRESS_ALLOCATED,
    AddressRecord,
    Store,
    StoreError,
    UtxoRecord,
)
from localwallet.wallet import (
    DEFAULT_GAP_LIMIT,
    WalletDescriptor,
    derive_addresses,
    parse_watch_key,
    rescan_wallet,
    scan_wallet,
)
from localwallet.wallet import scan as wallet_scan_module
from localwallet.wallet.descriptor import WatchKeyError
from localwallet.wallet.scan import _MAX_WINDOW_ADDRESSES, ScanError

FIXTURE_SEED: Final = b"local-wallet phase 1 scan test seed (not a real wallet)"
TIP: Final = 870_000
_EXTERNAL: Final = "bc1qexternalsenderaddressnotpartofthewallet000000"


def _fixture_zpub() -> str:
    root = HDKey.from_seed(FIXTURE_SEED, version=NETWORKS["main"]["zprv"])
    account = root.derive([84 + 2**31, 0 + 2**31, 0])
    return account.to_public().to_base58(version=NETWORKS["main"]["zpub"])


ZPUB: Final = _fixture_zpub()
WD: Final = WalletDescriptor.from_key(ZPUB)
PARSED: Final = WD.parsed
# Window addresses for both branches, indices 0..59 — plus the full
# absolute-ceiling window (indices 0..999) for branch 0, used by the
# TCK-SEC-002 ceiling tests.
ADDRS: Final[dict[int, list[str]]] = {
    branch: [d.address for d in derive_addresses(PARSED, branch, 0, 60)]
    for branch in (0, 1)
}
ADDRS_FULL: Final[dict[int, list[str]]] = {
    0: [d.address for d in derive_addresses(PARSED, 0, 0, _MAX_WINDOW_ADDRESSES)]
}


def tx_entry(
    txid: str,
    *,
    vin_addresses: tuple[str, ...] = (_EXTERNAL,),
    vout_addresses: tuple[str, ...] = (),
    fee: int | None = 1000,
    confirmed: bool = True,
    height: int | None = 800_000,
    block_time: int | None = 1_700_000_000,
) -> dict[str, Any]:
    """Build an Esplora address-txs entry."""
    entry: dict[str, Any] = {
        "txid": txid,
        "version": 1,
        "locktime": 0,
        "vin": [
            {"prevout": {"scriptpubkey_address": a, "value": 100_000}}
            for a in vin_addresses
        ],
        "vout": [
            {"scriptpubkey_address": a, "value": 90_000} for a in vout_addresses
        ],
        "size": 222,
        "weight": 564,
        "status": {"confirmed": confirmed},
    }
    if fee is not None:
        entry["fee"] = fee
    if confirmed:
        if height is not None:
            entry["status"]["block_height"] = height
        if block_time is not None:
            entry["status"]["block_time"] = block_time
    return entry


def utxo_entry(
    txid: str, vout: int, value: int, *, confirmed: bool = True, height: int = 800_000
) -> dict[str, Any]:
    status: dict[str, Any] = {"confirmed": confirmed}
    if confirmed:
        status["block_height"] = height
    return {"txid": txid, "vout": vout, "value": value, "status": status}


class FakeChain:
    """Scripted Esplora backend over MockTransport; records request order."""

    def __init__(
        self,
        *,
        txs: dict[str, list[dict[str, Any]]] | None = None,
        utxos: dict[str, list[dict[str, Any]]] | None = None,
        tip: int | list[dict[str, Any]] = TIP,
        fail: dict[str, int] | None = None,
    ) -> None:
        self.txs = txs or {}
        self.utxos = utxos or {}
        self.tip = tip
        self.fail = fail or {}
        self.requests: list[tuple[str, str | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/blocks/tip"):
            self.requests.append(("tip", None))
            status = self.fail.get("tip", 200)
            return httpx.Response(status, json=self.tip if status == 200 else None)
        parts = path.rstrip("/").split("/")
        address, kind = parts[-2], parts[-1]
        self.requests.append((kind, address))
        status = self.fail.get(kind, 200)
        if status != 200:
            return httpx.Response(status, json=None)
        if kind == "txs":
            return httpx.Response(200, json=self.txs.get(address, []))
        if kind == "utxo":
            return httpx.Response(200, json=self.utxos.get(address, []))
        return httpx.Response(404, json=None)

    def client(self) -> EsploraClient:
        return EsploraClient(
            base_url="https://mempool.space/api",
            timeout_s=5.0,
            max_retries=0,
            transport=httpx.MockTransport(self.handler),
        )


@pytest.fixture()
def store() -> Store:
    with Store.memory() as s:
        wallet = s.create_wallet("main", WD.descriptor)
        s.set_active_wallet(wallet.id)
        yield s


def _wallet_id(s: Store) -> int:
    return s.get_wallet_by_name("main").id  # type: ignore[union-attr]


def _used_at(indices: dict[int, str], branch: int) -> dict[str, list[dict[str, Any]]]:
    """Txs marking the given {index: txid} of ``branch`` as used."""
    return {
        ADDRS[branch][index]: [tx_entry(txid, vout_addresses=(ADDRS[branch][index],))]
        for index, txid in indices.items()
    }


def _used_everywhere(branch: int, count: int) -> dict[str, list[dict[str, Any]]]:
    """Txs marking EVERY address of ``ADDRS_FULL[branch][:count]`` used."""
    return {
        ADDRS_FULL[branch][i]: [
            tx_entry(f"{i:064x}", vout_addresses=(ADDRS_FULL[branch][i],))
        ]
        for i in range(count)
    }


# ------------------------------------------------------------------ happy path


def test_happy_scan_window_and_store_state(store: Store) -> None:
    """Usage at indices 0..3 → the walk stops at 23 with the default gap;
    derivation state, address statuses, UTXO snapshot and history land."""
    wid = _wallet_id(store)
    chain = FakeChain(
        txs=_used_at({0: "aa" * 32, 1: "bb" * 32, 2: "cc" * 32, 3: "dd" * 32}, 0),
        utxos={
            ADDRS[0][0]: [utxo_entry("aa" * 32, 0, 50_000)],
            ADDRS[0][2]: [utxo_entry("cc" * 32, 1, 12_345, confirmed=False)],
        },
    )
    summary = scan_wallet(store, chain.client(), store.get_wallet_by_name("main"))

    b0, b1 = summary.branches[0], summary.branches[1]
    assert (b0.scanned, b0.window_last_index, b0.max_used_index) == (24, 23, 3)
    assert b0.used_indices == (0, 1, 2, 3)
    assert (b1.scanned, b1.window_last_index, b1.max_used_index) == (20, 19, -1)
    assert summary.gap_limit == DEFAULT_GAP_LIMIT == 20
    assert summary.tip_height == TIP
    assert summary.utxo_count == 2
    assert summary.utxo_value_sats == 50_000 + 12_345
    assert summary.out_of_window == {}  # first scan: no previous window

    derivation0 = store.get_derivation(wid, 0)
    assert (derivation0.max_used_index, derivation0.next_index) == (3, 4)
    derivation1 = store.get_derivation(wid, 1)
    assert (derivation1.max_used_index, derivation1.next_index) == (-1, 0)

    addresses0 = store.get_addresses(wid, 0)
    assert [a.index for a in addresses0] == list(range(24))
    assert [a.address for a in addresses0] == ADDRS[0][:24]
    assert [a.status for a in addresses0] == ["used"] * 4 + ["unused"] * 20
    assert [a.index for a in store.get_addresses(wid, 1)] == list(range(20))

    utxos = store.get_utxos_for_wallet(wid)
    assert [(u.txid, u.vout, u.value_sats, u.confirmed) for u in utxos] == [
        ("aa" * 32, 0, 50_000, 1),
        ("cc" * 32, 1, 12_345, 0),
    ]
    assert all(u.address in {ADDRS[0][0], ADDRS[0][2]} for u in utxos)
    txs = store.get_txs_for_wallet(wid)
    assert [t.txid for t in txs] == sorted(["aa" * 32, "bb" * 32, "cc" * 32, "dd" * 32])
    assert all(t.direction == "in" for t in txs)


def test_happy_scan_request_ordering_is_sequential_and_deterministic(
    store: Store,
) -> None:
    """Branch 0 before branch 1, indices ascending, txs before utxos per
    branch, tip first — one txs call per window address, one utxo call
    per address whose txs result was non-empty (TCK-SCAN-001: empty
    history ⇒ no UTXO fetch)."""
    chain = FakeChain(txs=_used_at({2: "cc" * 32}, 0))
    scan_wallet(store, chain.client(), store.get_wallet_by_name("main"))

    assert chain.requests[0] == ("tip", None)
    expected = [
        ("tip", None),
        *[("txs", ADDRS[0][i]) for i in range(23)],  # window 0..22 (2 + gap 20)
        ("utxo", ADDRS[0][2]),  # the ONLY branch-0 address with txs
        *[("txs", ADDRS[1][i]) for i in range(20)],
        # branch 1 fully empty: zero utxo calls (TCK-SCAN-001)
    ]
    assert chain.requests == expected


def test_fresh_wallet_skips_utxo_fetch_for_empty_addresses(store: Store) -> None:
    """TCK-SCAN-001 call-count pin: fresh 2-branch wallet at the default
    gap 20 — every probe returns an empty history. Before the skip:
    81 calls (1 tip + 40 txs + 40 utxo). After: 41 (1 tip + 40 txs + 0
    utxo). Persist/reconcile semantics are unchanged — every probed
    address is still derived and stored (window rows, cursor,
    derivation) and the progress callback still ticks once per probe."""
    wid = _wallet_id(store)
    chain = FakeChain()
    ticks: list[None] = []
    summary = scan_wallet(
        store, chain.client(), store.get_wallet_by_name("main"), progress_fn=lambda: ticks.append(None)
    )

    kinds = [kind for kind, _ in chain.requests]
    assert kinds.count("tip") == 1
    assert kinds.count("txs") == 40  # 20 probes per branch (gap-stop)
    assert kinds.count("utxo") == 0
    assert len(chain.requests) == 41
    assert len(ticks) == 40  # TCK-UX-001: dots unchanged, one per probe
    assert (summary.branches[0].scanned, summary.branches[1].scanned) == (20, 20)
    # Byte-identical persistence except for the skipped HTTP calls:
    assert [a.index for a in store.get_addresses(wid, 0)] == list(range(20))
    assert [a.index for a in store.get_addresses(wid, 1)] == list(range(20))
    assert all(a.status == "unused" for a in store.get_addresses(wid, 0))
    assert store.get_utxos_for_wallet(wid) == []
    assert summary.utxo_count == 0


def test_funded_address_still_gets_utxo_fetch(store: Store) -> None:
    """TCK-SCAN-001 counter-pin: usage at index 0 of branch 0 — its utxo
    IS fetched (and only its, gap-20 fresh wallet: 1 tip + 41 txs +
    1 utxo = 43 calls) and the snapshot lands."""
    wid = _wallet_id(store)
    chain = FakeChain(
        txs=_used_at({0: "aa" * 32}, 0),
        utxos={ADDRS[0][0]: [utxo_entry("aa" * 32, 0, 5_000)]},
    )
    summary = scan_wallet(store, chain.client(), store.get_wallet_by_name("main"))

    utxo_calls = [a for kind, a in chain.requests if kind == "utxo"]
    assert utxo_calls == [ADDRS[0][0]]
    assert len([1 for kind, _ in chain.requests if kind == "txs"]) == 41  # 21 + 20
    assert len(chain.requests) == 1 + 41 + 1 == 43
    assert summary.utxo_count == 1
    assert [(u.txid, u.value_sats) for u in store.get_utxos_for_wallet(wid)] == [
        ("aa" * 32, 5_000)
    ]


# ------------------------------------------------------- gap policy / R3 warn


def test_deep_usage_missed_with_default_gap_caught_via_settings(
    store: Store,
) -> None:
    """Used at index 25: invisible with gap 20 (R3), found after widening
    the configured gap to 30 and rescanning; the beyond-window usage is
    persisted as a warning (ADR-0009), never silently swallowed."""
    wid = _wallet_id(store)
    deep_txs = _used_at({25: "ee" * 32}, 0)
    chain = FakeChain(txs=deep_txs, utxos={})
    row = store.get_wallet_by_name("main")

    first = scan_wallet(store, chain.client(), row)
    assert first.branches[0].window_last_index == 19  # 0..19, gap 20 exhausted
    assert first.branches[0].max_used_index == -1
    assert [a.index for a in store.get_addresses(wid, 0)] == list(range(20))
    assert store.get_sync_state(wid, "last_scan_cursor") == json.dumps(
        {"0": 20, "1": 20}, sort_keys=True
    )

    store.set_setting("gap_limit", "30")
    chain30 = FakeChain(txs=deep_txs, utxos={ADDRS[0][25]: [utxo_entry("ee" * 32, 0, 1000)]})
    second = rescan_wallet(store, chain30.client(), row)

    b0 = second.branches[0]
    assert (b0.window_last_index, b0.max_used_index, b0.next_index) == (55, 25, 26)
    assert [a.index for a in store.get_addresses(wid, 0)] == list(range(56))
    assert second.out_of_window == {
        "0": {"max_used_index": 25, "previous_window_end": 19}
    }
    persisted = json.loads(store.get_sync_state(wid, "out_of_window_detected"))
    assert persisted["detected_at"] is not None
    assert persisted["branches"] == second.out_of_window
    assert store.get_setting("gap_limit") == "30"  # scan honored the setting
    assert second.gap_limit == 30


def test_gap_limit_argument_overrides_setting(store: Store) -> None:
    chain = FakeChain()
    row = store.get_wallet_by_name("main")
    store.set_setting("gap_limit", "40")
    summary = scan_wallet(store, chain.client(), row, gap_limit=5)
    assert summary.gap_limit == 5
    assert summary.branches[0].window_last_index == 4  # 0..4, empty wallet


@pytest.mark.parametrize(
    ("raw", "match"),
    [("abc", "valid integer"), ("30.5", "valid integer"), ("0", "between"), ("1001", "between")],
)
def test_malformed_gap_setting_fails_closed(store: Store, raw: str, match: str) -> None:
    store.set_setting("gap_limit", raw)
    chain = FakeChain()
    with pytest.raises(ScanError, match=match):
        scan_wallet(store, chain.client(), store.get_wallet_by_name("main"))
    assert store.get_addresses(_wallet_id(store), 0) == []  # store untouched


@pytest.mark.parametrize("bad", [0, -5, True, "20", 2.5, 1001])
def test_gap_argument_validation(bad: object, store: Store) -> None:
    chain = FakeChain()
    with pytest.raises(ScanError):
        scan_wallet(store, chain.client(), store.get_wallet_by_name("main"), gap_limit=bad)  # type: ignore[arg-type]


# --------------------------------------------------------------- UTXO snapshot


def test_utxo_snapshot_replaces_stale_cache(store: Store) -> None:
    wid = _wallet_id(store)
    junk = [
        UtxoRecord(wid, "f" * 64, 0, "bc1qjunk", 999, 1, 10),
        UtxoRecord(wid, "e" * 64, 3, None, 1, 0, None),
    ]
    store.replace_utxos_for_wallet(wid, junk)
    chain = FakeChain(
        txs=_used_at({0: "aa" * 32, 7: "dd" * 32}, 0),  # funded addrs have txs
        utxos={
            ADDRS[0][0]: [
                utxo_entry("aa" * 32, 0, 50_000),
                utxo_entry("bb" * 32, 1, 7_000, confirmed=False),
            ],
            ADDRS[0][7]: [utxo_entry("dd" * 32, 0, 1_000)],
        },
    )
    scan_wallet(store, chain.client(), store.get_wallet_by_name("main"))
    got = store.get_utxos_for_wallet(wid)
    assert [(u.txid, u.vout, u.value_sats, u.confirmed, u.address) for u in got] == [
        ("aa" * 32, 0, 50_000, 1, ADDRS[0][0]),
        ("bb" * 32, 1, 7_000, 0, ADDRS[0][0]),
        ("dd" * 32, 0, 1_000, 1, ADDRS[0][7]),
    ]


@pytest.mark.parametrize(
    "broken",
    [
        {"txid": "aa" * 32, "vout": 0},  # value missing
        {"txid": "aa" * 32, "vout": -1, "value": 5, "status": {"confirmed": True}},
        {"txid": "aa" * 32, "vout": 0, "value": 5},  # status missing
        {"txid": "aa" * 32, "vout": 0, "value": 5, "status": {"confirmed": "yes"}},
        {"txid": "aa" * 32, "vout": 0, "value": 5,
         "status": {"confirmed": True, "block_height": "x"}},
        {"txid": "short", "vout": 0, "value": 5, "status": {"confirmed": True}},
        {"txid": "zz" * 32, "vout": 0, "value": 5, "status": {"confirmed": True}},
    ],
)
def test_malformed_utxo_payload_fails_closed_and_keeps_old_snapshot(
    store: Store, broken: dict[str, Any]
) -> None:
    wid = _wallet_id(store)
    junk = [UtxoRecord(wid, "f" * 64, 0, "bc1qjunk", 999, 1, 10)]
    store.replace_utxos_for_wallet(wid, junk)
    # The malformed utxo endpoint must still be probed: give its address a
    # transaction so the TCK-SCAN-001 empty-history skip does not apply.
    chain = FakeChain(
        txs=_used_at({0: "aa" * 32}, 0), utxos={ADDRS[0][0]: [broken]}
    )
    with pytest.raises(ScanError):
        scan_wallet(store, chain.client(), store.get_wallet_by_name("main"))
    assert store.get_utxos_for_wallet(wid) == junk  # snapshot untouched
    assert store.get_sync_state(wid, "last_scan_at") is None


# --------------------------------------------------------- rescan / stale cache


def test_rescan_fixes_simulated_stale_cache(store: Store) -> None:
    """Phase 1 AC: rescan repairs derivation state, address mappings and
    the UTXO snapshot from chain truth, preserving 'allocated' flags."""
    wid = _wallet_id(store)
    row = store.get_wallet_by_name("main")
    truth_txs = _used_at({0: "aa" * 32, 3: "dd" * 32}, 0)
    truth_utxos = {
        ADDRS[0][0]: [utxo_entry("aa" * 32, 0, 50_000)],
        ADDRS[0][3]: [utxo_entry("dd" * 32, 0, 4_000)],
    }
    scan_wallet(store, FakeChain(txs=truth_txs, utxos=truth_utxos).client(), row)
    truth_addresses = [a.address for a in store.get_addresses(wid, 0)]

    # --- corrupt the cache in every cacheable dimension
    store.replace_utxos_for_wallet(
        wid, [UtxoRecord(wid, "f" * 64, 9, "bc1qjunk", 123_456, 1, 5)]
    )
    store.update_derivation(wid, 0, max_used_index=99, next_index=99)
    stale = "bc1qstalerowthatneverexistedonchain000000000"
    store.upsert_batch(
        [AddressRecord(wid, 0, 1, stale, "p2wpkh", "used")]  # wrong mapping
    )
    store.mark_used(wid, 0, 10)  # false 'used'
    store.allocate(wid, 0, 4)  # user-facing flag to preserve

    rescan_wallet(
        store,
        FakeChain(txs=truth_txs, utxos=truth_utxos).client(),
        row,
    )

    derivation = store.get_derivation(wid, 0)
    # max_used_index is recomputed from chain truth (3); next_index is
    # floored at the highest allocated/used row + 1 (the false 'used' at
    # index 10 → 11) so a stale/allocated row above the used window is
    # never re-issued (TCK-P1-004 SR flooring).
    assert (derivation.max_used_index, derivation.next_index) == (3, 11)
    addresses = store.get_addresses(wid, 0)
    assert [a.address for a in addresses] == truth_addresses  # mapping restored
    statuses = {a.index: a.status for a in addresses}
    assert statuses[1] == "unused"  # false 'used' recomputed
    assert statuses[10] == "unused"
    assert statuses[4] == "allocated"  # allocation flag preserved by string
    assert statuses[0] == "used" and statuses[3] == "used"
    assert store.get_by_address(stale) is None
    assert [(u.txid, u.value_sats) for u in store.get_utxos_for_wallet(wid)] == [
        ("aa" * 32, 50_000),
        ("dd" * 32, 4_000),
    ]


def test_rescan_floors_next_index_at_allocated_above_window(store: Store) -> None:
    """A rebuild must not re-issue addresses allocated beyond the rescanned
    window (TCK-P1-004 SR): next_index floors at max allocated index + 1.

    Usage at index 3 with the default gap 20 yields a window of [0..23],
    so pre-allocated rows at 25/26 sit above it; flooring must give 27, not
    the naive rebuild value of 4 (which could re-issue 25/26 later)."""
    wid = _wallet_id(store)
    row = store.get_wallet_by_name("main")
    store.upsert_batch(
        [
            AddressRecord(wid, 0, 25, ADDRS[0][25], "p2wpkh", ADDRESS_ALLOCATED),
            AddressRecord(wid, 0, 26, ADDRS[0][26], "p2wpkh", ADDRESS_ALLOCATED),
        ]
    )
    truth_txs = _used_at({3: "dd" * 32}, 0)
    rescan_wallet(
        store,
        FakeChain(txs=truth_txs, utxos={}).client(),
        row,
    )
    derivation = store.get_derivation(wid, 0)
    assert (derivation.max_used_index, derivation.next_index) == (3, 27)


# ---------------------------------------------------- history direction + fees


def test_tx_direction_in_out_self_with_dedup_and_nullable_fee(
    store: Store,
) -> None:
    wid = _wallet_id(store)
    tx_in = tx_entry("11" * 32, vout_addresses=(ADDRS[0][0],), fee=None)
    tx_out = tx_entry(
        "22" * 32, vin_addresses=(ADDRS[0][1],), vout_addresses=(_EXTERNAL,), fee=500
    )
    tx_self = tx_entry(
        "33" * 32,
        vin_addresses=(ADDRS[0][2],),
        vout_addresses=(ADDRS[1][0], _EXTERNAL),
        fee=250,
        confirmed=False,
        height=None,
        block_time=None,
    )
    chain = FakeChain(
        txs={
            # tx_in is seen from two of our addresses: must dedup to one row.
            ADDRS[0][0]: [tx_in],
            ADDRS[0][1]: [tx_in, tx_out],
            ADDRS[0][2]: [tx_self],
            ADDRS[1][0]: [tx_self],
        },
    )
    scan_wallet(store, chain.client(), store.get_wallet_by_name("main"))

    txs = {t.txid: t for t in store.get_txs_for_wallet(wid)}
    assert set(txs) == {"11" * 32, "22" * 32, "33" * 32}
    assert txs["11" * 32].direction == "in"
    assert txs["11" * 32].fee_sats is None  # missing fee tolerated, not fabricated
    assert (txs["11" * 32].height, txs["11" * 32].block_time) == (800_000, 1_700_000_000)
    assert txs["22" * 32].direction == "out"
    assert txs["22" * 32].fee_sats == 500
    assert txs["33" * 32].direction == "self"  # spends ours, pays our change
    assert txs["33" * 32].fee_sats == 250
    assert (txs["33" * 32].height, txs["33" * 32].block_time) == (None, None)


# ------------------------------------------------------- fail-closed payloads


@pytest.mark.parametrize(
    "broken",
    [
        {},  # everything missing
        {"txid": "aa" * 32},  # status missing
        {"txid": "aa" * 32, "status": {}},  # confirmed missing
        {"txid": "aa" * 32, "status": {"confirmed": "yes"}},
        {"txid": "short", "status": {"confirmed": True}},
        {"txid": "zz" * 32, "status": {"confirmed": True}},  # not hex
        {"txid": "aa" * 32, "status": {"confirmed": True, "block_height": "x"}},
        {"txid": "aa" * 32, "status": {"confirmed": True, "block_time": -5}},
        {"txid": "aa" * 32, "status": {"confirmed": True}, "fee": -1},
        {"txid": "aa" * 32, "status": {"confirmed": True}, "fee": True},
        {"txid": "aa" * 32, "status": {"confirmed": True}, "vin": "notalist"},
        {"txid": "aa" * 32, "status": {"confirmed": True}, "vout": [3]},
        {"txid": "aa" * 32, "status": {"confirmed": True}, "vin": [{"prevout": 5}]},
        {
            "txid": "aa" * 32,
            "status": {"confirmed": True},
            "vin": [{"prevout": {"scriptpubkey_address": 7}}],
        },
    ],
)
def test_malformed_tx_payload_fails_closed_store_untouched(
    store: Store, broken: dict[str, Any]
) -> None:
    wid = _wallet_id(store)
    chain = FakeChain(txs={ADDRS[0][0]: [broken]})
    with pytest.raises(ScanError):
        scan_wallet(store, chain.client(), store.get_wallet_by_name("main"))
    # Nothing persisted: no addresses, no derivation progress, no cursors.
    assert store.get_addresses(wid, 0) == []
    assert store.get_addresses(wid, 1) == []
    assert store.get_derivation(wid, 0).max_used_index == -1
    assert store.get_utxos_for_wallet(wid) == []
    assert store.get_txs_for_wallet(wid) == []
    assert store.get_sync_state(wid, "last_scan_at") is None


def test_chain_transport_failure_leaves_store_untouched(store: Store) -> None:
    wid = _wallet_id(store)
    chain = FakeChain(fail={"tip": 500})
    with pytest.raises(ChainError):
        scan_wallet(store, chain.client(), store.get_wallet_by_name("main"))
    assert store.get_addresses(wid, 0) == []
    assert store.get_sync_state(wid, "last_scan_at") is None


def test_txs_transport_failure_leaves_store_untouched(store: Store) -> None:
    chain = FakeChain(fail={"txs": 503})
    with pytest.raises(ChainError):
        scan_wallet(store, chain.client(), store.get_wallet_by_name("main"))
    assert store.get_sync_state(_wallet_id(store), "last_tip_height") is None


# ------------------------------------------------------------- sync_state I/O


def test_sync_state_round_trip(store: Store) -> None:
    wid = _wallet_id(store)
    chain = FakeChain(txs=_used_at({2: "cc" * 32}, 0))
    summary = scan_wallet(store, chain.client(), store.get_wallet_by_name("main"))

    cursor = json.loads(store.get_sync_state(wid, "last_scan_cursor"))
    assert cursor == {"0": 23, "1": 20}  # stop index + 1 per branch
    assert store.get_sync_state(wid, "last_tip_height") == str(TIP)
    scanned_at = store.get_sync_state(wid, "last_scan_at")
    assert datetime.fromisoformat(scanned_at) == datetime.fromisoformat(
        summary.scanned_at
    )
    oow = json.loads(store.get_sync_state(wid, "out_of_window_detected"))
    assert oow == {"detected_at": None, "branches": {}}


def test_rescan_clears_stale_out_of_window_warning(store: Store) -> None:
    """A warning clears once the window covers the observed usage."""
    wid = _wallet_id(store)
    row = store.get_wallet_by_name("main")
    deep_txs = _used_at({25: "ee" * 32}, 0)
    scan_wallet(store, FakeChain().client(), row)  # window 0..19, cursor 20
    store.set_setting("gap_limit", "30")
    rescan_wallet(store, FakeChain(txs=deep_txs).client(), row)  # flags index 25
    assert store.get_sync_state(wid, "out_of_window_detected") is not None
    rescan_wallet(store, FakeChain(txs=deep_txs).client(), row)  # now in-window
    cleared = json.loads(store.get_sync_state(wid, "out_of_window_detected"))
    assert cleared == {"detected_at": None, "branches": {}}


# -------------------------------------------------- atomic persist (SR P1-002)


def test_store_failure_during_persist_leaves_entire_prior_state_intact(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The persist phase is one atomic store transaction (security review
    TCK-P1-002): a store failure mid-persist rolls back the ENTIRE scan
    write — address statuses, derivation cursor, UTXO snapshot, history
    and sync state all stay at their prior values — and surfaces as the
    store's value-free StoreError (deliberately not wrapped in ScanError;
    see scan_wallet's Raises contract). A clean retry afterwards succeeds."""
    wid = _wallet_id(store)
    row = store.get_wallet_by_name("main")
    scan_wallet(store, FakeChain(txs=_used_at({0: "aa" * 32}, 0)).client(), row)

    # Snapshot the entire prior state, dimension by dimension.
    prior = {
        "addresses0": store.get_addresses(wid, 0),
        "addresses1": store.get_addresses(wid, 1),
        "derivation0": store.get_derivation(wid, 0),
        "derivation1": store.get_derivation(wid, 1),
        "utxos": store.get_utxos_for_wallet(wid),
        "txs": store.get_txs_for_wallet(wid),
        "cursor": store.get_sync_state(wid, "last_scan_cursor"),
        "tip": store.get_sync_state(wid, "last_tip_height"),
        "scan_at": store.get_sync_state(wid, "last_scan_at"),
        "oow": store.get_sync_state(wid, "out_of_window_detected"),
    }

    def _explode(wallet_id: int, records: object) -> None:
        raise StoreError("store operation failed")

    # Fails mid-transaction: address + derivation writes already executed.
    monkeypatch.setattr(store, "_replace_utxo_rows", _explode)
    with pytest.raises(StoreError) as excinfo:
        scan_wallet(
            store, FakeChain(txs=_used_at({1: "bb" * 32}, 0)).client(), row
        )
    message = str(excinfo.value)
    for record in prior["addresses0"]:
        assert record.address not in message  # value-free
    assert "bb" * 32 not in message

    # ENTIRE prior state intact — nothing from the failed scan leaked.
    assert store.get_addresses(wid, 0) == prior["addresses0"]
    assert store.get_addresses(wid, 1) == prior["addresses1"]
    assert store.get_derivation(wid, 0) == prior["derivation0"]
    assert store.get_derivation(wid, 1) == prior["derivation1"]
    assert store.get_utxos_for_wallet(wid) == prior["utxos"]
    assert store.get_txs_for_wallet(wid) == prior["txs"]
    assert store.get_sync_state(wid, "last_scan_cursor") == prior["cursor"]
    assert store.get_sync_state(wid, "last_tip_height") == prior["tip"]
    assert store.get_sync_state(wid, "last_scan_at") == prior["scan_at"]
    assert store.get_sync_state(wid, "out_of_window_detected") == prior["oow"]

    # Recovery: a clean retry persists normally.
    monkeypatch.undo()
    summary = scan_wallet(
        store, FakeChain(txs=_used_at({1: "bb" * 32}, 0)).client(), row
    )
    assert summary.branches[0].max_used_index == 1
    statuses = [a.status for a in store.get_addresses(wid, 0)]
    assert statuses == ["unused", "used"] + ["unused"] * 20


# ------------------------------------------------------------- wallet inputs


def test_scan_accepts_descriptor_object_and_rejects_unknown(store: Store) -> None:
    row = store.get_wallet_by_name("main")
    chain = FakeChain(txs=_used_at({1: "bb" * 32}, 0))
    summary = scan_wallet(store, chain.client(), WD)
    assert summary.wallet_id == row.id
    assert summary.branches[0].max_used_index == 1

    other = WalletDescriptor.from_key(
        # a different fixture wallet not present in the store
        _fixture_zpub_other()
    )
    with pytest.raises(ScanError, match="no stored wallet"):
        scan_wallet(store, FakeChain().client(), other)
    with pytest.raises(ScanError, match="wallet must be"):
        scan_wallet(store, FakeChain().client(), WD.descriptor)  # type: ignore[arg-type]


def _fixture_zpub_other() -> str:
    root = HDKey.from_seed(FIXTURE_SEED + b"other", version=NETWORKS["main"]["zprv"])
    account = root.derive([84 + 2**31, 0 + 2**31, 0])
    return account.to_public().to_base58(version=NETWORKS["main"]["zpub"])


def test_scan_refuses_testnet_wallet_descriptor(store: Store) -> None:
    """The mainnet-only gate (ADR-0021) holds for stored wallets too
    (parse-time enforcement): a testnet descriptor can never be scanned,
    whichever layer sees it first — the origin gate (testnet coin type 1')
    or the parse gate (testnet key material)."""
    hd = parse_watch_key(_fixture_testnet_vpub()).hd_key
    # Testnet-shaped origin (84'/1'/0'): refused by the origin gate…
    testnet_origin = add_checksum(
        f"wpkh([{hd.my_fingerprint.hex()}/84'/1'/0']{_fixture_testnet_vpub()}/{{0,1}}/*)"
    )
    store.create_wallet("testnet-origin", testnet_origin)
    with pytest.raises(WatchKeyError, match="mainnet"):
        scan_wallet(store, FakeChain().client(), store.get_wallet_by_name("testnet-origin"))
    # …and a testnet key with mainnet-shaped origin (84'/0'/0') hits the
    # parse gate.
    testnet_key = add_checksum(
        f"wpkh([{hd.my_fingerprint.hex()}/84'/0'/0']{_fixture_testnet_vpub()}/{{0,1}}/*)"
    )
    store.create_wallet("testnet", testnet_key)
    row = store.get_wallet_by_name("testnet")
    with pytest.raises(WatchKeyError, match="mainnet-only"):
        scan_wallet(store, FakeChain().client(), row)


def _fixture_testnet_vpub() -> str:
    root = HDKey.from_seed(FIXTURE_SEED + b"test", version=NETWORKS["test"]["zprv"])
    account = root.derive([84 + 2**31, 1 + 2**31, 0])
    return account.to_public().to_base58(version=NETWORKS["test"]["zpub"])


# ------------------------------------------------ absolute window ceiling (TCK-SEC-002)


def test_ceiling_terminates_walk_and_marks_truncated(store: Store) -> None:
    """Attacker-driven usage (EVERY derivable index funded): the walk must
    terminate at the absolute window ceiling — exactly ``_MAX_WINDOW_ADDRESSES``
    indices derived/probed (0..ceiling-1, NO extra gap beyond the ceiling) —
    and mark the result truncated explicitly (summary + per-branch), never
    silently. An unused branch is unaffected (per-branch independence)."""
    wid = _wallet_id(store)
    chain = FakeChain(txs=_used_everywhere(0, _MAX_WINDOW_ADDRESSES))
    summary = scan_wallet(store, chain.client(), store.get_wallet_by_name("main"))

    b0, b1 = summary.branches[0], summary.branches[1]
    # Exactly the ceiling indices: derivation count == ceiling, window is
    # 0..999 (the ceiling is the inclusive window bound — no +gap tail).
    assert b0.scanned == _MAX_WINDOW_ADDRESSES == 1000
    assert b0.window_last_index == _MAX_WINDOW_ADDRESSES - 1 == 999
    assert b0.max_used_index == 999
    assert b0.next_index == 1000
    assert b0.truncated is True
    assert summary.truncated is True  # explicit, caller-visible

    # The store holds exactly the ceiling window mapping, derived from key.
    indexes0 = [a.index for a in store.get_addresses(wid, 0)]
    assert indexes0 == list(range(_MAX_WINDOW_ADDRESSES))

    # Probe budget as a consequence: ≤ ceiling txs + ceiling utxo per
    # branch. Fully-used branch 0: utxo fetched for every window address.
    # Empty branch 1: TCK-SCAN-001 skip — zero utxo calls.
    txs_calls = [a for kind, a in chain.requests if kind == "txs"]
    utxo_calls = [a for kind, a in chain.requests if kind == "utxo"]
    assert len(txs_calls) == _MAX_WINDOW_ADDRESSES + 20  # b0 ceiling + b1 gap-stop
    assert len(utxo_calls) == _MAX_WINDOW_ADDRESSES  # b0 only (b1 never fetched)
    # Every branch-0 probe is inside the ceiling window.
    assert set(txs_calls[:_MAX_WINDOW_ADDRESSES]) == set(ADDRS_FULL[0])

    # Unused branch: normal gap semantics, not truncated.
    assert (b1.scanned, b1.window_last_index, b1.max_used_index) == (20, 19, -1)
    assert b1.truncated is False


def test_wallet_under_ceiling_not_truncated(store: Store) -> None:
    """Normal wallets (ADR-0009 semantics, usage + gap well under the
    ceiling): truncated stays False on the summary and every branch."""
    chain = FakeChain(txs=_used_at({0: "aa" * 32, 3: "dd" * 32}, 0))
    summary = scan_wallet(store, chain.client(), store.get_wallet_by_name("main"))
    assert summary.truncated is False
    assert all(not b.truncated for b in summary.branches.values())
    assert summary.branches[0].window_last_index == 23  # unchanged gap semantics


def test_gap_equal_to_ceiling_boundary(store: Store) -> None:
    """Boundary semantics at gap == ceiling: an empty wallet with
    gap_limit=1000 stops by the gap condition at exactly the ceiling with
    truncated=False (window 0..999); the same gap with usage at index 0
    would previously have walked 1001 addresses — the ceiling caps it at
    1000 and flags truncated=True."""
    row = store.get_wallet_by_name("main")

    empty = scan_wallet(store, FakeChain().client(), row, gap_limit=1000)
    b0 = empty.branches[0]
    assert (b0.scanned, b0.window_last_index) == (1000, 999)
    assert b0.truncated is False  # gap termination, not the ceiling
    assert empty.truncated is False

    used = scan_wallet(
        store,
        FakeChain(txs=_used_at({0: "aa" * 32}, 0)).client(),
        row,
        gap_limit=1000,
    )
    b0u = used.branches[0]
    assert (b0u.scanned, b0u.window_last_index) == (1000, 999)
    assert b0u.truncated is True  # capped by the ceiling, flagged
    assert used.truncated is True


def test_rescan_is_bounded_by_the_same_ceiling(store: Store) -> None:
    """The rescan (cache-rebuild) path is bounded identically: with usage
    on every derivable index it stops at the ceiling, reports truncated,
    and its probe count stays within ceiling txs + ceiling utxo per
    branch — a rescan can never exceed the scan budget."""
    wid = _wallet_id(store)
    row = store.get_wallet_by_name("main")
    ceiling_txs = _used_everywhere(0, _MAX_WINDOW_ADDRESSES)

    scan_wallet(store, FakeChain(txs=ceiling_txs).client(), row)  # truncated scan
    assert len(store.get_addresses(wid, 0)) == _MAX_WINDOW_ADDRESSES

    chain = FakeChain(txs=ceiling_txs)
    summary = rescan_wallet(store, chain.client(), row)
    b0 = summary.branches[0]
    assert (b0.scanned, b0.window_last_index, b0.max_used_index) == (
        _MAX_WINDOW_ADDRESSES,
        _MAX_WINDOW_ADDRESSES - 1,
        _MAX_WINDOW_ADDRESSES - 1,
    )
    assert b0.truncated is True and summary.truncated is True
    assert [a.index for a in store.get_addresses(wid, 0)] == list(
        range(_MAX_WINDOW_ADDRESSES)
    )

    txs_calls = [a for kind, a in chain.requests if kind == "txs"]
    utxo_calls = [a for kind, a in chain.requests if kind == "utxo"]
    assert len(txs_calls) <= _MAX_WINDOW_ADDRESSES + 20
    # TCK-SCAN-001: only branch 0 (fully used) has utxo fetches; the
    # empty branch 1's gap-window is never fetched.
    assert len(utxo_calls) == _MAX_WINDOW_ADDRESSES


# ------------------------------------------------ progress callback (TCK-UX-001)


def test_progress_callback_ticks_once_per_probed_address(store: Store) -> None:
    """TCK-UX-001 (scan branch): the optional progress callback fires
    EXACTLY once per address probed by the walk — tick count equals the
    number of txs probes (== per-branch ``scanned`` sums) for a used+gap
    pattern across BOTH branches. Strict zero-arg: the callback below
    accepts no parameters, so any argument would raise TypeError."""
    # Branch 0 used at 0 and 2, default gap 20 → walk 0..22 (23 probed);
    # branch 1 fully unused → walk 0..19 (20 probed). Total 43.
    chain = FakeChain(txs=_used_at({0: "aa" * 32, 2: "cc" * 32}, 0))
    ticks: list[None] = []

    def tick() -> None:  # strict zero-argument callback (value-free tick)
        ticks.append(None)

    row = store.get_wallet_by_name("main")
    summary = scan_wallet(store, chain.client(), row, progress_fn=tick)

    b0, b1 = summary.branches[0], summary.branches[1]
    assert (b0.scanned, b1.scanned) == (23, 20)
    assert len(ticks) == b0.scanned + b1.scanned == 43
    txs_probes = [a for kind, a in chain.requests if kind == "txs"]
    assert len(txs_probes) == len(ticks) == 43


def test_progress_callback_ticks_on_rescan_branch(store: Store) -> None:
    """TCK-UX-001 (rescan/rebuild branch): identical one-tick-per-probed-
    address contract on ``rescan_wallet``; the strict zero-arg callback
    proves no address/index/amount data is ever passed."""
    chain = FakeChain(txs=_used_at({1: "bb" * 32}, 0))
    ticks: list[None] = []

    def tick() -> None:  # strict zero-argument callback
        ticks.append(None)

    row = store.get_wallet_by_name("main")
    summary = rescan_wallet(store, chain.client(), row, progress_fn=tick)

    b0, b1 = summary.branches[0], summary.branches[1]
    assert (b0.scanned, b1.scanned) == (22, 20)  # used@1 + gap 20 / unused gap 20
    assert len(ticks) == b0.scanned + b1.scanned == 42
    assert len([1 for kind, _ in chain.requests if kind == "txs"]) == 42


def test_progress_callback_none_is_unchanged_behavior(store: Store) -> None:
    """TCK-UX-001: ``progress_fn=None`` (and the kwarg omitted entirely)
    produce byte-identical results and request patterns — the plumbing is
    purely additive (this is the path every existing call site, the
    background watcher probe, and the lazy scans take)."""
    txs = _used_at({0: "aa" * 32, 3: "dd" * 32}, 0)
    row = store.get_wallet_by_name("main")

    plain = scan_wallet(store, FakeChain(txs=txs).client(), row)
    explicit_none = scan_wallet(
        store, FakeChain(txs=txs).client(), row, progress_fn=None
    )
    # scanned_at is a wall-clock timestamp; every other field must match.
    for field in (
        "wallet_id", "gap_limit", "tip_height", "utxo_count",
        "utxo_value_sats", "truncated",
    ):
        assert getattr(plain, field) == getattr(explicit_none, field)
    assert plain.branches == explicit_none.branches
    assert plain.out_of_window == explicit_none.out_of_window


def test_progress_callback_signature_is_zero_arg(store: Store) -> None:
    """TCK-UX-001: the callback contract is a bare tick — proven two ways:
    (1) a strict zero-parameter callable survives a full scan/rescan (any
    argument would raise TypeError), and (2) the documented parameter
    type on all public entry points is a zero-argument callable."""
    row = store.get_wallet_by_name("main")
    ticks: list[None] = []

    def tick() -> None:
        ticks.append(None)

    scan_wallet(store, FakeChain().client(), row, progress_fn=tick)
    rescan_wallet(store, FakeChain().client(), row, progress_fn=tick)
    assert len(ticks) > 0

    for fn in (wallet_scan_module.scan_wallet, wallet_scan_module.rescan_wallet):
        param = inspect.signature(fn).parameters["progress_fn"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is None

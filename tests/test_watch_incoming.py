"""Tests for Phase 5 ``watch_incoming`` (TCK-P5-001): poller + time-since-block.

All hermetic: the poller is driven by an injected probe (test double) and the
chain client is served by ``httpx.MockTransport`` — no real network.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx
import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.app import _drain_watch, _make_watch_probe, _narrate_incoming_event
from localwallet.chain import (
    ChainError,
    EsploraClient,
    IncomingEvent,
    IncomingWatcher,
    TipBlock,
    WatchedTx,
    time_since_last_block,
)
from localwallet.config import Settings
from localwallet.store import (
    DIR_IN,
    DIR_OUT,
    DIR_SELF,
    Store,
    TxRecord,
    UtxoRecord,
)

BASE_URL = "https://mempool.space/testnet4/api"
TXID = "a" * 64
ADDR = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"


# ---------------------------------------------------------------- time-since-block


class _FakeTipClient:
    """Minimal client double exposing ``get_tip_block``."""

    def __init__(self, tip: TipBlock | None, error: bool = False) -> None:
        self._tip = tip
        self._error = error

    def get_tip_block(self) -> TipBlock:
        if self._error:
            raise ChainError("tip-block request failed")
        if self._tip is None:
            raise ChainError("tip-block response was not an integer or block list")
        return self._tip


def test_time_since_block_integer_math():
    client = _FakeTipClient(TipBlock(height=100, timestamp=1_000_000))
    assert time_since_last_block(client, now=1_000_420) == 420


def test_time_since_block_clamps_at_zero_when_timestamp_in_future():
    client = _FakeTipClient(TipBlock(height=100, timestamp=1_000_000))
    assert time_since_last_block(client, now=999_900) == 0


def test_time_since_block_none_when_no_timestamp():
    # Bare-integer /blocks/tip shape carries no timestamp (clean unavailable).
    client = _FakeTipClient(TipBlock(height=100, timestamp=None))
    assert time_since_last_block(client, now=1_000_000) is None


def test_time_since_block_none_on_chain_error():
    assert time_since_last_block(_FakeTipClient(None, error=True), now=1_000_000) is None


def test_time_since_block_none_on_bad_shape():
    assert time_since_last_block(_FakeTipClient(None), now=1_000_000) is None


class _Scripted:
    """Minimal MockTransport backend for get_tip_block shape tests."""

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def handler(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=self._payload)


def _tip_client(payload: object) -> EsploraClient:
    return EsploraClient(
        base_url=BASE_URL, timeout_s=5.0, max_retries=0,
        transport=httpx.MockTransport(_Scripted(payload).handler),
    )


def test_get_tip_block_list_shape_extracts_max_height_timestamp():
    with _tip_client(
        [
            {"height": 100, "timestamp": 1_000_000},
            {"height": 101, "timestamp": 1_000_100},
        ]
    ) as client:
        tip = client.get_tip_block()
    assert tip.height == 101
    assert tip.timestamp == 1_000_100


def test_get_tip_block_list_shape_missing_timestamp_is_none():
    with _tip_client([{"height": 101}]) as client:
        tip = client.get_tip_block()
    assert tip.height == 101
    assert tip.timestamp is None


def test_get_tip_block_bare_integer_shape_timestamp_none():
    with _tip_client(101) as client:
        tip = client.get_tip_block()
    assert tip.height == 101
    assert tip.timestamp is None


@pytest.mark.parametrize(
    "payload",
    [
        [],  # empty list
        [{"height": "x"}],  # malformed height
        "junk",  # not an int or list
        True,  # bool is not an int
        [{"height": -1}],  # negative height
    ],
)
def test_get_tip_block_malformed_shape_fails_closed(payload: object):
    with _tip_client(payload) as client, pytest.raises(ChainError):
        client.get_tip_block()


# ---------------------------------------------------------------- poller (tick-driven)


def _watched(
    *,
    txid: str = TXID,
    incoming: bool = True,
    confirmed: bool = False,
    height: int | None = None,
    address: str | None = ADDR,
    amount: int | None = 5000,
) -> WatchedTx:
    return WatchedTx(
        txid=txid,
        incoming=incoming,
        confirmed=confirmed,
        height=height,
        block_time=1_000_000 if height is None else 1_000_100,
        address=address,
        amount_sats=amount,
    )


def test_poll_cycle_surfaces_new_incoming_tx():
    """AC: a new incoming tx surfaces within ONE poll cycle (tick)."""
    watcher = IncomingWatcher(probe=lambda: [_watched()])
    events = watcher.tick()
    assert len(events) == 1
    event = events[0]
    assert event.kind == "received"
    assert event.txid == TXID
    assert event.address == ADDR  # quoted verbatim from tool output
    assert event.amount_sats == 5000
    assert event.confirmed is False


def test_same_tx_not_resurfaced_on_subsequent_cycles():
    """Dedup: the same tx is not re-surfaced on later ticks."""
    watcher = IncomingWatcher(probe=lambda: [_watched()])
    assert len(watcher.tick()) == 1
    assert watcher.tick() == []  # still present, already surfaced
    assert watcher.tick() == []


def test_confirmed_transition_surfaced_once_with_correct_state():
    """An unconfirmed->confirmed transition surfaces exactly once with height."""
    states = iter([[_watched(confirmed=False)], [_watched(confirmed=True, height=99)]])

    def probe():
        try:
            return next(states)
        except StopIteration:
            return []

    watcher = IncomingWatcher(probe=probe)
    first = watcher.tick()
    assert len(first) == 1 and first[0].kind == "received" and first[0].confirmed is False
    second = watcher.tick()
    assert len(second) == 1
    assert second[0].kind == "confirmed"
    assert second[0].confirmed is True
    assert second[0].height == 99
    assert watcher.tick() == []  # not re-surfaced


def test_already_confirmed_new_tx_surfaces_as_received():
    watcher = IncomingWatcher(probe=lambda: [_watched(confirmed=True, height=42)])
    events = watcher.tick()
    assert len(events) == 1
    assert events[0].kind == "received"
    assert events[0].confirmed is True
    assert events[0].height == 42


def test_non_incoming_tx_not_surfaced():
    watcher = IncomingWatcher(probe=lambda: [_watched(incoming=False)])
    assert watcher.tick() == []


def test_incoming_tx_without_address_amount_recorded_but_not_surfaced():
    watcher = IncomingWatcher(probe=lambda: [_watched(address=None, amount=None)])
    assert watcher.tick() == []
    # Recorded for dedup: a later surfaced variant of the SAME txid is not
    # re-surfaced (already known to the poller).
    assert watcher.tick() == []


def test_poller_is_tick_driven_and_spawns_no_thread():
    """Pin the watcher's own tick design (ADR-0019 §2, as amended by
    ADR-0024 §9; retirement recorded and executed by ADR-0022 /
    TCK-SCAN-003): ``tick`` is synchronous (no sleep) and the WATCHER
    spawns no poller thread.

    The old pin asserted ``threading.enumerate() == 1`` (the whole process
    was one thread). That exact form is retired as planned work for the
    threaded world (ADR-0019 amendment / ADR-0024 §3): the CLI REPL now
    runs the queue pump with a stdin feeder thread, the dedicated chain
    worker (ADR-0022) owns scan/watch chain I/O with NO store access, and
    engine-thread mode lives behind ``app.start_engine`` (pinned in
    tests/test_engine_pump.py). The load-bearing half stays: a tick must
    spawn nothing — asserted as a thread-set DELTA around the call, which
    is transport-mode agnostic (the watcher only ever reads ``poll_due``/
    ``tick`` from whatever thread drives it; in the CLI that is the pump
    between turns, and the poll's chain fetch rides the worker via
    ``scan_fn`` — pinned in tests/test_scan_worker.py). The dedup/
    surfacing pins are unchanged.
    """
    import threading

    watcher = IncomingWatcher(probe=lambda: [_watched()], interval_s=60.0)
    before = set(threading.enumerate())
    events = watcher.tick()  # no real sleeps, returns immediately
    assert len(events) == 1
    assert set(threading.enumerate()) == before  # the watcher spawns no thread


# ---------------------------------------------------------------- interval / off gating


def test_watcher_disabled_when_interval_zero():
    watcher = IncomingWatcher(probe=lambda: [_watched()], interval_s=0)
    assert watcher.enabled is False
    assert watcher.poll_due() is False
    assert watcher.tick() == []


def test_poll_due_gates_on_interval_with_injected_clock():
    clock = {"t": 0.0}

    def fake_clock() -> float:
        return clock["t"]

    watcher = IncomingWatcher(probe=list, interval_s=60.0, clock=fake_clock)
    assert watcher.poll_due(now=0.0) is False  # seeded at construction (t=0)
    assert watcher.poll_due(now=59.0) is False
    assert watcher.poll_due(now=60.0) is True
    clock["t"] = 60.0  # tick uses the injected clock; last poll now at t=60
    watcher.tick()
    assert watcher.poll_due(now=60.0) is False
    assert watcher.poll_due(now=120.0) is True


# ---------------------------------------------------------------- config matrix


def test_watch_interval_env_parsing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "30")
    assert Settings.from_env().watch_interval_s == 30.0


def test_watch_interval_env_zero_is_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")
    assert Settings.from_env().watch_interval_s == 0.0


def test_watch_interval_env_default_is_sane():
    assert Settings.from_env().watch_interval_s == 60.0


def test_watch_interval_malformed_env_fails_closed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "not-a-number")
    with pytest.raises(ValueError):
        Settings.from_env()


# ---------------------------------------------------------------- app wiring / narration


def test_narrate_incoming_received_event():
    event = IncomingEvent(
        kind="received", txid=TXID, address=ADDR, amount_sats=5000, confirmed=False
    )
    line = _narrate_incoming_event(event)
    assert "5000 sats" in line
    assert ADDR in line
    assert "in mempool" in line


def test_narrate_incoming_confirmed_event():
    event = IncomingEvent(
        kind="confirmed", txid=TXID, address=ADDR, amount_sats=5000,
        confirmed=True, height=99,
    )
    line = _narrate_incoming_event(event)
    assert "5000 sats" in line
    assert ADDR in line
    assert "height 99" in line


def test_drain_watch_none_is_noop():
    outputs: list[str] = []
    _drain_watch(None, outputs.append)
    assert outputs == []


def test_drain_watch_fail_open_on_probe_error():
    def boom() -> list[WatchedTx]:
        raise ChainError("address-txs request failed: status 500")

    watcher = IncomingWatcher(probe=boom, interval_s=0.0)
    outputs: list[str] = []
    _drain_watch(watcher, outputs.append)  # disabled -> no tick, no crash
    assert outputs == []


def test_make_watch_probe_shapes_incoming_from_store():
    with Store.memory() as store:
        wallet = store.create_wallet("main", "dummy-descriptor")
        store.upsert_txs(
            [
                TxRecord(wallet_id=wallet.id, txid=TXID, height=None, block_time=None,
                         fee_sats=None, direction=DIR_IN, raw_summary=None),
                TxRecord(wallet_id=wallet.id, txid="b" * 64, height=None, block_time=None,
                         fee_sats=None, direction=DIR_OUT, raw_summary=None),
            ]
        )
        store.replace_utxos_for_wallet(
            wallet.id,
            [
                UtxoRecord(wallet_id=wallet.id, txid=TXID, vout=0, address=ADDR,
                           value_sats=5000, confirmed=0, height=None),
            ],
        )
        probe = _make_watch_probe(store, wallet.id, lambda: None)
        observed = probe()
        assert len(observed) == 1  # only the incoming (DIR_IN) tx
        assert observed[0].txid == TXID
        assert observed[0].address == ADDR
        assert observed[0].amount_sats == 5000
        assert observed[0].confirmed is False


def test_make_watch_probe_includes_self_direction():
    self_txid = "c" * 64
    with Store.memory() as store:
        wallet = store.create_wallet("main", "dummy-descriptor")
        store.upsert_txs(
            [TxRecord(wallet_id=wallet.id, txid=self_txid, height=10, block_time=1,
                      fee_sats=None, direction=DIR_SELF, raw_summary=None)]
        )
        store.replace_utxos_for_wallet(
            wallet.id,
            [UtxoRecord(wallet_id=wallet.id, txid=self_txid, vout=0, address=ADDR,
                        value_sats=700, confirmed=1, height=10)],
        )
        probe = _make_watch_probe(store, wallet.id, lambda: None)
        observed = probe()
        assert len(observed) == 1
        assert observed[0].confirmed is True
        assert observed[0].height == 10


# ---------------------------------------------------------------- NOTE-2: time-since-block narration suffix


class _CountingTipClient:
    """Client double exposing ``get_tip_block`` and counting tip lookups."""

    def __init__(self, tip: TipBlock) -> None:
        self._tip = tip
        self.calls = 0

    def get_tip_block(self) -> TipBlock:
        self.calls += 1
        return self._tip


class _TickWatcher:
    """Build a watcher with a controllable clock advanced past its interval."""

    def __init__(self, probe: object, interval_s: float = 60.0) -> None:
        self._clock = {"t": 0.0}

        def fake_clock() -> float:
            return self._clock["t"]

        self.watcher = IncomingWatcher(probe=probe, interval_s=interval_s, clock=fake_clock)
        self._clock["t"] = interval_s  # first drain is due

    def advance(self, seconds: float) -> None:
        self._clock["t"] += seconds


def _received_event_line(outputs: list[str]) -> str:
    assert len(outputs) == 1, outputs
    return outputs[0]


def test_narrate_incoming_event_appends_last_block_suffix():
    event = IncomingEvent(
        kind="received", txid=TXID, address=ADDR, amount_sats=5000, confirmed=False
    )
    line = _narrate_incoming_event(event, suffix="last block ~7 min ago")
    assert "5000 sats" in line
    assert line.endswith("· last block ~7 min ago")


def test_drain_watch_appends_suffix_when_tip_timestamp_available():
    probe = lambda: [_watched()]
    clock_watcher = _TickWatcher(probe)
    client = _FakeTipClient(TipBlock(height=100, timestamp=time.time() - 420))
    outputs: list[str] = []
    _drain_watch(clock_watcher.watcher, outputs.append, client=client)
    assert "last block ~7 min ago" in _received_event_line(outputs)


def test_drain_watch_no_suffix_when_tip_timestamp_unavailable():
    probe = lambda: [_watched()]
    clock_watcher = _TickWatcher(probe)
    client = _FakeTipClient(TipBlock(height=100, timestamp=None))
    outputs: list[str] = []
    _drain_watch(clock_watcher.watcher, outputs.append, client=client)
    assert "last block" not in _received_event_line(outputs)


def test_drain_watch_no_suffix_when_tip_lookup_raises():
    probe = lambda: [_watched()]
    clock_watcher = _TickWatcher(probe)
    client = _FakeTipClient(None, error=True)  # get_tip_block raises ChainError
    outputs: list[str] = []
    _drain_watch(clock_watcher.watcher, outputs.append, client=client)
    assert "last block" not in _received_event_line(outputs)


def test_drain_watch_suffix_computed_once_per_drain_not_per_event():
    probe = lambda: [_watched(), _watched(txid="b" * 64)]
    clock_watcher = _TickWatcher(probe)
    client = _CountingTipClient(TipBlock(height=100, timestamp=time.time() - 420))
    outputs: list[str] = []
    _drain_watch(clock_watcher.watcher, outputs.append, client=client)
    assert len(outputs) == 2
    assert all("last block ~7 min ago" in o for o in outputs)
    assert client.calls == 1  # computed once for the whole drain


# ---------------------------------------------------------------- NOTE-1: persistent-failure visibility


def test_drain_watch_failure_line_printed_once_per_streak():
    def boom() -> list[WatchedTx]:
        raise ChainError("address-txs request failed: status 500")

    clock_watcher = _TickWatcher(boom)
    outputs: list[str] = []
    _drain_watch(clock_watcher.watcher, outputs.append)
    assert outputs == ["watch: check failed, will retry next cycle"]
    # A second consecutive failure does NOT repeat the line (throttled per streak).
    clock_watcher.advance(60.0)
    _drain_watch(clock_watcher.watcher, outputs.append)
    assert outputs == ["watch: check failed, will retry next cycle"]
    clock_watcher.advance(60.0)
    _drain_watch(clock_watcher.watcher, outputs.append)
    assert outputs == ["watch: check failed, will retry next cycle"]


def test_drain_watch_failure_line_absent_after_successful_poll_resets_streak():
    state = {"fail": True}

    def probe() -> list[WatchedTx]:
        if state["fail"]:
            raise ChainError("address-txs request failed: status 500")
        return []

    clock_watcher = _TickWatcher(probe)
    outputs: list[str] = []
    _drain_watch(clock_watcher.watcher, outputs.append)
    assert outputs == ["watch: check failed, will retry next cycle"]
    # A successful poll ends the streak: no failure line, and it stays quiet
    # on the immediate next (not-yet-due) call.
    state["fail"] = False
    clock_watcher.advance(60.0)
    _drain_watch(clock_watcher.watcher, outputs.append)
    assert outputs == ["watch: check failed, will retry next cycle"]
    # A NEW failure after the success starts a fresh streak -> line again.
    state["fail"] = True
    clock_watcher.advance(60.0)
    _drain_watch(clock_watcher.watcher, outputs.append)
    assert outputs == [
        "watch: check failed, will retry next cycle",
        "watch: check failed, will retry next cycle",
    ]


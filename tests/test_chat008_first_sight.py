"""TCK-CHAT-008 — first-connect watch spam suppression (USER REPORT).

Connecting an existing wallet used to narrate one "Incoming: received …"
line per historical UTXO (25 → 25+ messages). The rule: on the FIRST
sighting of a wallet's history (the first watch cycle after the startup
scan — the pump stands the drain down during scans), surface ONLY
unconfirmed incoming or confirmed within the past 3 blocks of the
scan-cached tip; older history is absorbed silently behind at most one
value-free summary line. New incoming after that cycle surfaces
immediately.

Restart semantics (documented choice, P5-001 precedent): the marker is
PROCESS-SCOPED (stamped on the app-built watcher instance; the watcher's
``_seen`` dedup map is itself process memory). A restart re-runs the
startup scan, the fresh watcher re-surfaces the history internally, and
the stamp re-absorbs it SILENTLY — the summary line is the only echo,
never per-coin spam. A persisted flag would be WORSE: "already sighted"
plus a fresh empty ``_seen`` would re-surface every coin per-line on
every launch.

All hermetic: fake probes + an in-memory/tmp store; no chain client.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.app import (
    _WATCH_FIRST_SIGHT_ATTR,
    _drain_watch,
    _rebind_watcher,
    _stamp_watch_first_sighting,
)
from localwallet.chain import IncomingWatcher, WatchedTx
from localwallet.store import Store, StoreError
from localwallet.wallet import scan as wallet_scan

TIP = 800_000
ADDR = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kxxs9lv"
SUMMARY = "earlier transactions loaded"


def _tx(i: int, *, height: int | None) -> WatchedTx:
    """One incoming observation; ``confirmed`` mirrors the probe's rule
    (a height means confirmed)."""
    return WatchedTx(
        txid=f"{i:064x}"[:64],
        incoming=True,
        confirmed=height is not None,
        height=height,
        block_time=None,
        address=ADDR,
        amount_sats=5000 + i,
    )


class _Probe:
    def __init__(self, *txs: WatchedTx) -> None:
        self.txs = list(txs)

    def __call__(self) -> list[WatchedTx]:
        return list(self.txs)


class _Session:
    """Stamped watcher + controllable clock (first drain is due), wired
    exactly like the app: a real store with an active wallet and the
    scan-cached tip."""

    def __init__(self, store: Store, *txs: WatchedTx, stamped: bool = True) -> None:
        self.probe = _Probe(*txs)
        self._t = 0.0
        watcher = IncomingWatcher(self.probe, interval_s=60.0, clock=lambda: self._t)
        self._t = 60.0  # first drain is due (seeded at construction, above)
        self.watcher = _stamp_watch_first_sighting(watcher) if stamped else watcher
        self.store = store
        self.outputs: list[str] = []

    def drain(self) -> int:
        count = _drain_watch(self.watcher, self.outputs.append, store=self.store)
        self._t += 60.0  # next cycle is due
        return count

    def set_txs(self, *txs: WatchedTx) -> None:
        self.probe.txs = list(txs)

    @property
    def sighted(self) -> bool:
        """True while the first sighting has NOT yet happened."""
        return bool(getattr(self.watcher, _WATCH_FIRST_SIGHT_ATTR, False))


def _store(tmp_path: Path, *, with_tip: bool = True) -> Store:
    store = Store(tmp_path / "chat008.db")
    wallet = store.create_wallet("main", "dummy-descriptor")
    store.set_setting("active_wallet_id", str(wallet.id))
    if with_tip:
        store.set_sync_state(wallet.id, wallet_scan.TIP_KEY, str(TIP))
    return store


# ------------------------------------------------- first-connect absorption


def test_first_connect_25_historical_utxos_zero_coin_lines_one_summary(tmp_path):
    store = _store(tmp_path)
    try:
        session = _Session(store, *(_tx(i, height=TIP - 1000 - i) for i in range(25)))
        assert session.drain() == 0
        # Exactly ONE line: the value-free summary. No per-coin narration,
        # no amounts, no addresses (the count is the sanctioned echo).
        assert session.outputs == ["25 earlier transactions loaded"]
    finally:
        store.close()


def test_first_connect_surfaces_unconfirmed_and_recent_only(tmp_path):
    store = _store(tmp_path)
    try:
        session = _Session(
            store,
            _tx(1, height=TIP - 500),  # old -> absorbed
            _tx(2, height=TIP - 4),  # boundary: 4 back -> absorbed
            _tx(3, height=None),  # unconfirmed -> surfaces
            _tx(4, height=TIP - 3),  # boundary: within 3 -> surfaces
            _tx(5, height=TIP),  # fresh tip -> surfaces
        )
        assert session.drain() == 3
        assert session.outputs[0] == "2 earlier transactions loaded"
        lines = session.outputs[1:]
        assert len(lines) == 3
        assert all(line.startswith("Incoming: received") for line in lines)
        assert any("5003 sats" in line and "in mempool" in line for line in lines)
        assert any("5004 sats" in line and "confirmed" in line for line in lines)
        assert any("5005 sats" in line for line in lines)
        # absorbed coins never print
        assert not any("5001" in line or "5002 sats" in line for line in lines)
    finally:
        store.close()


def test_three_block_boundary_exactness(tmp_path):
    store = _store(tmp_path)
    try:
        # 3 blocks back surfaces, 4 blocks back is absorbed (strict >).
        session = _Session(store, _tx(1, height=TIP - 3))
        assert session.drain() == 1
        assert session.outputs[0] == (
            f"Incoming: received 5001 sats at #1 {ADDR} "
            f"(confirmed, tx {1:064x})."
        )
        session2 = _Session(store, _tx(2, height=TIP - 4))
        assert session2.drain() == 0
        assert session2.outputs == ["1 earlier transactions loaded"]
    finally:
        store.close()


def test_no_stored_tip_fails_open_never_swallows(tmp_path):
    store = _store(tmp_path, with_tip=False)
    try:
        session = _Session(store, _tx(1, height=TIP - 500), _tx(2, height=TIP - 600))
        # Doubt about the tip = surface everything (wallet money is never
        # hidden behind an unreadable cursor); no summary, stamp consumed.
        assert session.drain() == 2
        assert all(line.startswith("Incoming: received") for line in session.outputs)
        assert not any(SUMMARY in line for line in session.outputs)
        assert not session.sighted
    finally:
        store.close()


# ------------------------------------------------- after the first sighting


def test_second_cycle_new_incoming_surfaces_immediately(tmp_path):
    store = _store(tmp_path)
    try:
        session = _Session(store, *(_tx(i, height=TIP - 100 - i) for i in range(25)))
        assert session.drain() == 0
        assert session.outputs == ["25 earlier transactions loaded"]
        # A brand-new coin on the NEXT cycle: immediate, no summary echo,
        # no swallowed surfacing (dedup coordination pin).
        session.set_txs(*(_tx(i, height=TIP - 100 - i) for i in range(25)),
                        _tx(99, height=None))
        assert session.drain() == 1
        assert session.outputs[-1].startswith("Incoming: received 5099 sats")
        assert session.outputs.count(f"1 {SUMMARY}") == 0
        # And it is NEVER re-surfaced (the watcher's own dedup still rules).
        assert session.drain() == 0
    finally:
        store.close()


def test_pending_confirmed_transition_after_first_sight_surfaces(tmp_path):
    store = _store(tmp_path)
    try:
        session = _Session(store, _tx(1, height=None))
        assert session.drain() == 1  # first sight: unconfirmed surfaces
        assert session.outputs[0].startswith("Incoming: received")
        session.set_txs(_tx(1, height=TIP))
        assert session.drain() == 1  # the transition surfaces, once
        assert session.outputs[-1].startswith("Confirmed:")
        assert session.drain() == 0  # never again
    finally:
        store.close()


def test_zero_event_cycles_do_not_consume_the_sighting(tmp_path):
    store = _store(tmp_path)
    try:
        session = _Session(store)  # empty wallet: cycles produce no events
        assert session.drain() == 0
        assert session.outputs == []
        assert session.sighted  # not a FIRST SIGHTING yet — no history seen
        # History shows up on a later cycle (e.g. the scan lagged): absorbed.
        session.set_txs(_tx(1, height=TIP - 400))
        assert session.drain() == 0
        assert session.outputs == ["1 earlier transactions loaded"]
        assert not session.sighted
    finally:
        store.close()


# ------------------------------------------------- restart + stamping scope


def test_zero_event_cycle_stays_quiet_when_store_lookup_raises(tmp_path, monkeypatch):
    """MINOR-1: the wallet lookup is deferred until a non-empty drain. A
    store whose get_active_wallet raises must NOT throw a throttled failure
    line on a quiet zero-event cycle (the old hoisted lookup did)."""
    store = _store(tmp_path)
    try:
        def boom():
            raise StoreError("boom")
        monkeypatch.setattr(store, "get_active_wallet", boom)
        session = _Session(store)
        assert session.drain() == 0
        assert session.outputs == []  # no failure line, drain stays quiet
        assert session.sighted
    finally:
        store.close()


def test_restart_reabsorbs_silently(tmp_path):
    """The documented process-scoped choice: a restart re-runs the startup
    scan and the fresh stamped watcher re-absorbs the same history — a
    summary line at most, never the 25-line spam."""
    store = _store(tmp_path)
    try:
        txs = [_tx(i, height=TIP - 900 - i) for i in range(25)]
        first = _Session(store, *txs)
        assert first.drain() == 0
        # "restart": brand-new process, brand-new stamped watcher, same store
        second = _Session(store, *txs)
        assert second.drain() == 0
        assert second.outputs == ["25 earlier transactions loaded"]
    finally:
        store.close()


def test_bare_unstamped_watcher_keeps_pre_ticket_narration(tmp_path):
    """The stamp is applied ONLY at the two app construction sites; a bare
    watcher (test harnesses, anything outside app.py) narrates per-event as
    before — production suppression can never leak sideways."""
    store = _store(tmp_path)
    try:
        session = _Session(store, _tx(1, height=TIP - 500), stamped=False)
        assert session.drain() == 1
        assert session.outputs[0].startswith("Incoming: received")
    finally:
        store.close()


def test_off_on_rebind_watcher_is_stamped(tmp_path, monkeypatch):
    """TCK-CFG-005's OFF→ON construction site pairs its fresh dedup memory
    with first-sighting absorption (re-enabling must not per-line
    re-surface history)."""
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "120")
    store = _store(tmp_path)
    try:
        wallet = store.get_active_wallet()
        scan = SimpleNamespace(wallet_id=wallet.id, scan_now=lambda: None)
        watcher, applied = _rebind_watcher(None, store, scan)
        assert applied and watcher is not None
        assert getattr(watcher, _WATCH_FIRST_SIGHT_ATTR, False) is True
    finally:
        store.close()


def test_in_place_rebound_watcher_absorbs_once_only(tmp_path, monkeypatch):
    """ON→ON rebind reuses the instance (the dedup-survival pin): a
    watcher that already made its first sighting is NOT re-absorbed —
    a settings nudge never swallows the next live coin."""
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "120")
    store = _store(tmp_path)
    try:
        wallet = store.get_active_wallet()
        scan = SimpleNamespace(wallet_id=wallet.id, scan_now=lambda: None)
        session = _Session(store, _tx(1, height=None))
        assert session.drain() == 1  # first sight consumed
        live, applied = _rebind_watcher(session.watcher, store, scan)
        assert applied and live is session.watcher
        assert getattr(live, _WATCH_FIRST_SIGHT_ATTR, False) is False
    finally:
        store.close()

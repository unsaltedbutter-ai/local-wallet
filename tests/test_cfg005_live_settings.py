"""TCK-CFG-005 — live respect of chat-changed settings, no restart.

Pins the three ticket deliverables:

1. the WATCHER follows ``watch_interval_s`` mid-session: the pump rebinds
   the running :class:`IncomingWatcher`'s interval IN PLACE on an applied
   change (chat OR the settings pane's typed write), the NEXT poll already
   gates on the new number, no new threads are ever spawned, and the dedup
   memory survives the rebind (a rebuild would re-surface every past
   incoming transaction — the reason the seam mutates);
2. the coin-policy keys and ``gap_limit`` are READ AT USE — verified, not
   assumed: the file rung used to ride a boot snapshot into the create_tx
   selection and into every scan plan; both now re-resolve env/config-file
   per call (the behavior pins live in tests/test_e2e_skeleton.py's file-
   rung selection tests and the scan-plan gap re-read here);
3. narration honesty — per key, the ack says what took effect IMMEDIATELY
   and what waits; the CFG-004 blanket "next launch" line survives ONLY
   where the truth still is next-launch (an env-shadowed key, a bare
   harness call with no pump seam for the watcher).
"""

from __future__ import annotations

import queue
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.chain.watch import IncomingWatcher, WatchedTx
from localwallet.config import read_config_file, write_config_file
from localwallet.store import Store
from localwallet.tx.flow import TxFlow

MANAGED_ENV_VARS: Final[tuple[str, ...]] = (
    "LOCALWALLET_GAP_LIMIT",
    "LOCALWALLET_WATCH_INTERVAL_S",
    "LOCALWALLET_UTXO_TARGET_MIN_SATS",
    "LOCALWALLET_UTXO_TARGET_MAX_SATS",
    "LOCALWALLET_CONSOLIDATE_BELOW_SAT_VB",
)


@pytest.fixture(autouse=True)
def _ladder_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Clean ladder per test: no env rungs, config file pointed at tmp (the
    repo-root config.json is never created or read by this suite)."""
    for var in MANAGED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("LOCALWALLET_CONFIG_PATH", str(cfg))
    return cfg


def _watcher_at(interval: float) -> IncomingWatcher:
    t = [1000.0]
    return IncomingWatcher(list, interval_s=interval, clock=lambda: t[0])


# ------------------------------------------------- 1. the watcher seam itself


def test_set_interval_shortened_makes_the_next_due_poll_poll() -> None:
    """The gate reads the NEW number from the very next ``poll_due`` — a
    shortened interval makes an in-flight wait overdue at once (the live-
    apply point), and no thread is involved either way."""
    t = [1000.0]
    watcher = IncomingWatcher(list, interval_s=600, clock=lambda: t[0])
    assert not watcher.poll_due(now=1100.0)  # 600 not elapsed
    watcher.set_interval_s(30)
    assert watcher.interval_s == 30
    assert watcher.poll_due(now=1100.0)  # 100 s since the seed tick >= 30


def test_set_interval_lengthened_respects_the_new_gate() -> None:
    t = [1000.0]
    watcher = IncomingWatcher(list, interval_s=30, clock=lambda: t[0])
    watcher.set_interval_s(600)
    assert not watcher.poll_due(now=1100.0)
    assert watcher.poll_due(now=2000.0)


def test_rebind_in_place_carries_the_dedup_memory(_ladder_env) -> None:
    """Why the seam mutates instead of rebuilding: the rebinded watcher is
    the SAME object and still remembers what it surfaced — a fresh build
    would replay every past incoming transaction as "received"."""
    write_config_file({"watch_interval_s": 30.0})
    tx = WatchedTx(
        txid="a" * 64,
        incoming=True,
        confirmed=False,
        height=None,
        block_time=None,
        address="bc1q-example",
        amount_sats=1000,
    )
    t = [0.0]
    watcher = IncomingWatcher(lambda: [tx], interval_s=60, clock=lambda: t[0])
    assert len(watcher.tick()) == 1  # the received event surfaces once
    live, applied = app._rebind_watcher(watcher, Store.memory(), scan=None)
    assert applied is True and live is watcher  # same object, rebound
    assert watcher.interval_s == 30
    t[0] += 30
    assert watcher.tick() == []  # dedup survived the rebind — no replay


def test_rebind_off_to_on_builds_the_watcher_over_the_live_scan(
    _ladder_env,
) -> None:
    write_config_file({"watch_interval_s": 45.0})
    scan = SimpleNamespace(wallet_id=7, scan_now=lambda: None)
    with Store.memory() as store:
        live, applied = app._rebind_watcher(None, store, scan)
    assert applied is True
    assert live is not None and live.enabled and live.interval_s == 45


def test_rebind_on_to_off_returns_none_applied(_ladder_env) -> None:
    write_config_file({"watch_interval_s": 0.0})
    with Store.memory() as store:
        live, applied = app._rebind_watcher(_watcher_at(60), store, scan=None)
    assert applied is True and live is None


def test_rebind_off_stays_off_clause_off_now(_ladder_env) -> None:
    """0 with nothing running: applied (the ladder resolved), no watcher —
    the ack's OFF clause, never a phantom watcher."""
    write_config_file({"watch_interval_s": 0.0})
    with Store.memory() as store:
        live, applied = app._rebind_watcher(None, store, scan=None)
    assert applied is True and live is None


def test_rebind_off_to_on_without_a_scan_is_not_applied(_ladder_env) -> None:
    """A bare harness pump (no scan to probe through) cannot conjure a
    watcher: ``applied=False`` keeps the ack next-launch-honest."""
    write_config_file({"watch_interval_s": 30.0})
    with Store.memory() as store:
        live, applied = app._rebind_watcher(None, store, scan=None)
    assert applied is False and live is None


def test_rebind_hand_broken_file_mid_session_keeps_the_live_watcher(
    _ladder_env,
) -> None:
    """The fail-quiet corner: a malformed file (hand-tampered after the
    startup refusal could have seen it) never takes a running watch down,
    and ``applied=False`` says the change did NOT land live."""
    live_watcher = _watcher_at(60)
    _ladder_env.write_text("{ not json", encoding="utf-8")
    with Store.memory() as store:
        watcher, applied = app._rebind_watcher(live_watcher, store, scan=None)
    assert applied is False
    assert watcher is live_watcher and watcher.interval_s == 60


# ------------------------------------- 1b. the pump applies a chat change


class _Never:
    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the model must never run for a consumed turn")


def _drive_pump(
    lines: list[Any],
    *,
    watcher: IncomingWatcher | None,
    store: Store,
    backend: Any = None,
    scan: Any = None,
) -> list[str]:
    """Run the real pump on THIS thread (the engine thread) over the given
    commands, then QUIT. Returns the output lines."""
    commands: queue.Queue[Any] = queue.Queue()
    for item in lines:
        commands.put(item)
    commands.put(app.QUIT)
    out: list[str] = []
    table: dict[str, Any] = {}
    app._pump(
        AgentLoop(_Never(), table),
        out.append,
        commands,
        flow=TxFlow(),
        session=app.SendSession(),
        table=table,
        watcher=watcher,
        store=store,
        scan=scan,
        backend=backend,
    )
    return out


def test_chat_watch_change_pumps_the_new_interval_into_the_next_poll(
    _ladder_env,
) -> None:
    t = [0.0]
    watcher = IncomingWatcher(list, interval_s=600, clock=lambda: t[0])
    threads_before = set(threading.enumerate())
    with Store.memory() as store:
        out = _drive_pump(
            ["set the watch interval to 30"],
            watcher=watcher,
            store=store,
        )
        assert store.get_setting("watch_interval_s") is None  # conflict rule
    assert any("next poll" in line for line in out), out
    assert not any("next launch" in line for line in out), out
    assert watcher.interval_s == 30  # SAME object, rebound in place
    # the very next poll cycle gates on the new number, not the old one:
    assert not watcher.poll_due(now=10.0)
    assert watcher.poll_due(now=31.0)
    # engine-thread discipline: the pump ran on this thread and spawned
    # nothing — the watcher class itself is thread-free (ADR-0019).
    assert set(threading.enumerate()) == threads_before
    assert read_config_file()["watch_interval_s"] == 30.0


def test_chat_watch_change_zero_stops_the_watch_live(_ladder_env) -> None:
    """on→off through one pump run: the ack says the watch is off NOW, and
    the typed /state read QUEUED BEHIND the change answers
    ``configured: False`` — the pump's own watcher local went None (the
    same transport read the web pane uses to prove it)."""
    watcher = _watcher_at(60)
    reply: queue.Queue[dict[str, object]] = queue.Queue()
    with Store.memory() as store:
        commands: queue.Queue[Any] = queue.Queue()
        commands.put("set the watch interval to 0")
        commands.put(app.StateSnapshotRequest(command="state", reply=reply))
        commands.put(app.QUIT)
        out: list[str] = []
        app._pump(
            AgentLoop(_Never(), {}),
            out.append,
            commands,
            flow=TxFlow(),
            session=app.SendSession(),
            table={},
            watcher=watcher,
            store=store,
        )
    assert any("off now" in line for line in out), out
    assert not any("next launch" in line for line in out), out
    snapshot = reply.get_nowait()
    assert snapshot["watch"] == {"configured": False, "enabled": False}  # type: ignore[index]


def test_chat_watch_change_from_off_builds_the_watcher_live(_ladder_env) -> None:
    """off→on through the pump (a launch with the zero escape hatch): the
    watcher is BUILT on the engine thread (the pump's local rebinds — the
    only transport-visible truth is the typed /state behind it, exactly how
    the web pane learns the watch went live), gated on the new interval."""
    reply: queue.Queue[dict[str, object]] = queue.Queue()
    with Store.memory() as store:
        commands: queue.Queue[Any] = queue.Queue()
        commands.put("set the watch interval to 45")
        commands.put(app.StateSnapshotRequest(command="state", reply=reply))
        commands.put(app.QUIT)
        out: list[str] = []
        app._pump(
            AgentLoop(_Never(), {}),
            out.append,
            commands,
            flow=TxFlow(),
            session=app.SendSession(),
            table={},
            watcher=None,
            store=store,
            scan=_StubScan(),
        )
    assert any("next poll" in line for line in out), out
    snapshot = reply.get_nowait()
    assert snapshot["watch"] == {"configured": True, "enabled": True}  # type: ignore[index]


def test_pane_watch_write_rebinds_the_running_watcher(_ladder_env) -> None:
    """The settings pane's typed ``SettingsRequest`` (stored rung) takes
    effect on the NEXT POLL too — same live-apply seam as chat."""
    watcher = _watcher_at(600)
    threads_before = set(threading.enumerate())
    reply: queue.Queue[dict[str, object]] = queue.Queue()
    with Store.memory() as store:
        out = _drive_pump(
            [
                app.SettingsRequest(
                    command="settings", key="watch_interval_s", value="15", reply=reply
                )
            ],
            watcher=watcher,
            store=store,
        )
    body = reply.get_nowait()
    assert body["status"] == "applied"
    assert body["settings"][0]["requires_restart"] is False  # the live flag
    assert watcher.interval_s == 15  # mutated in place on the pump thread
    assert set(threading.enumerate()) == threads_before
    assert out == []  # a typed write narrates nothing new


# ------------------------------------- 2. gap_limit immediacy (chat path)


class _StubBackend:
    def __init__(self, resync_outcome: str = "started") -> None:
        self.resync_calls = 0
        self._outcome = resync_outcome

    def resync(self) -> str:
        self.resync_calls += 1
        return self._outcome


class _StubScan:
    """Just enough :class:`ScanFlow` surface for the pump loop + the OFF→ON
    watcher build: no fetch ever in flight, no command claimed, gate
    disabled (the watch probe is only CONSTRUCTED, never ticked)."""

    def __init__(self) -> None:
        self.wallet_id = 7
        #: TCK-WEB-020: the /state builder reads it; this stub never scans.
        self.scan_error: str | None = None
        self.gate = SimpleNamespace(
            state="disabled", enabled=False, in_progress=False
        )

    @property
    def in_progress(self) -> bool:
        return False

    @property
    def first_scan_recorded(self) -> bool:
        return False

    def attach(self, commands: Any) -> None:
        pass

    def begin(self) -> None:
        pass

    def handle_command(self, command: Any, narrate: Any, emitter: Any = None) -> bool:
        return False

    def scan_now(self) -> None:
        raise AssertionError("the watch probe must never run in this test")

    def drain_until_complete(self, narrate: Any, emitter: Any) -> None:
        pass


def test_chat_gap_widen_resyncs_immediately_and_says_so(_ladder_env) -> None:
    backend = _StubBackend()
    with Store.memory() as store:
        out = _drive_pump(
            ["set the gap limit to 50"],  # default 20 → widen
            watcher=None,
            store=store,
            backend=backend,
        )
    assert backend.resync_calls == 1  # GAP-001 widen semantics, chat surface
    ack = "\n".join(out)
    assert "next scan" in ack and "resync is running now" in ack
    assert "next launch" not in ack
    assert read_config_file()["gap_limit"] == "50"


def test_chat_gap_narrow_never_rescans_and_states_the_tradeoff(
    _ladder_env,
) -> None:
    backend = _StubBackend()
    with Store.memory() as store:
        store.set_setting("gap_limit", "50")
        out = _drive_pump(
            ["set the gap limit to 5"],  # effective 50 → narrow
            watcher=None,
            store=store,
            backend=backend,
        )
    assert backend.resync_calls == 0  # a smaller window only hides
    ack = "\n".join(out)
    assert app.GAP_NARROW_NOTE in ack
    assert "next scan" in ack and "next launch" not in ack


def test_chat_gap_busy_resync_stays_honest_without_the_running_claim(
    _ladder_env,
) -> None:
    backend = _StubBackend(resync_outcome="busy")
    with Store.memory() as store:
        out = _drive_pump(
            ["set the gap limit to 50"], watcher=None, store=store, backend=backend
        )
    assert backend.resync_calls == 1
    ack = "\n".join(out)
    assert "resync is running now" not in ack  # no overclaim when busy
    assert "next scan" in ack  # still true: the next plan re-reads


def test_scan_plans_re_read_the_file_gap_rung_per_plan(_ladder_env) -> None:
    """The read-at-use mechanism behind the widen/narrow clauses: a
    :class:`ScanFlow` built on a boot snapshot resolves the env/file rung
    FRESH per plan, falling back to the snapshot only when the fresh read
    fails (a hand-broken file never stalls a scan nor flips it silently)."""
    worker = SimpleNamespace()
    store = Store.memory()
    wallet = store.create_wallet("default", "zpub-descriptor")
    flow = app.ScanFlow(store, wallet, worker, gap_limit=None)
    assert flow._live_gap_limit is None  # nothing on env/file
    write_config_file({"gap_limit": "40"})
    assert flow._live_gap_limit == 40  # no restart, no re-construction
    _ladder_env.write_text("{ broken", encoding="utf-8")
    boot = app.ScanFlow(store, wallet, worker, gap_limit=7)
    assert boot._live_gap_limit == 7  # boot-validated fallback, fail-quiet
    store.close()


# --------------------------------- 3. no-seam honesty + unchanged ack shapes


def test_watch_change_without_a_pump_stays_next_launch_honest(
    _ladder_env,
) -> None:
    """A bare ``_run_chat_settings_turn`` call (no engine pump observing the
    file) must NOT claim the live clause — the CFG-004 next-launch line is
    still the honest answer for the watcher on that surface."""
    with Store.memory() as store:
        out: list[str] = []
        assert app._run_chat_settings_turn(store, "set the watch interval to 30", out.append)
    assert "next launch" in out[0]
    assert "next poll" not in out[0]


def test_coin_key_ack_says_next_transaction_and_no_launch_claim(
    _ladder_env,
) -> None:
    with Store.memory() as store:
        out: list[str] = []
        assert app._run_chat_settings_turn(
            store, "set the smallest utxo target to 60000 sats", out.append
        )
    assert "next transaction this wallet builds" in out[0]
    assert "next launch" not in out[0]


def test_env_shadowed_watch_change_keeps_the_launch_clause(_ladder_env, monkeypatch) -> None:
    """The one case the live claim must NEVER make: env outranks the file,
    the effective watcher value cannot move — the CFG-004 env heads-up
    stands and no immediacy clause rides with it."""
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "90")
    with Store.memory() as store:
        out = _drive_pump(
            ["set the watch interval to 30"], watcher=_watcher_at(90), store=store
        )
    ack = "\n".join(out)
    assert "LOCALWALLET_WATCH_INTERVAL_S" in ack and "outranks" in ack
    assert "next poll" not in ack
    # and the watcher really did not move: the ladder re-read answers env.
    assert _watcher_at(90).interval_s == 90  # sanity
    with Store.memory() as store2:
        assert app._live_watch_interval(store2) == 90.0

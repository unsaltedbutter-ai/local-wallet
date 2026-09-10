"""TCK-WEB-001 engine-pump seam (ADR-0024 §3).

Pins the queue-driven turn pump:

* dedicated **engine-thread** mode — ``start_engine`` bootstraps state
  (Store, agent loop) ON the engine thread and a submitted turn produces
  events ending in a ``turn_end`` completion (never-cancel: no
  cancellation hook exists between submit and completion);
* **monotonic event ids** — strictly increasing, never reordered;
* **scan dots through the emitter** — a non-CLI transport receives them as
  ``progress`` events and stdout stays clean;
* **CLI mode unchanged** — the CLI sink renders text/progress exactly as
  the pre-web REPL wrote them, the feeder paces reads AFTER the previous
  turn's output, and the CLI world has no engine thread (the main thread
  IS the engine);
* the seam proof — the autouse harness in ``tests/conftest.py`` runs every
  e2e REPL session's input through the command queue and collects output
  as events (pinned here against a real ``run()`` session).
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.app import (
    EVENT_PROGRESS,
    EVENT_TEXT,
    EVENT_TURN_END,
    EngineEvent,
    EventEmitter,
    cli_emitter,
)
from localwallet.protocol import IntentName
from localwallet.store import Store
from localwallet.tx.flow import TxFlow
from localwallet.wallet.scan import ScanSummary
from tests.conftest import HARNESS
from tests.test_e2e_skeleton import ZPUB

# Import-bound reference to the untouched CLI adapter (the autouse harness
# fixture monkeypatches the module attribute per test).
_repl_cli = app._repl


def _make_loop(table: dict[Any, Any] | None = None) -> AgentLoop:
    return AgentLoop(
        app.stub_generate,
        table
        if table is not None
        else {
            IntentName.RESPOND: app._respond_handler,
            IntentName.CLARIFY: app._clarify_handler,
        },
    )


def _wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


@pytest.fixture
def echo_turns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the turn path with an echo recorder (pump-level unit scope)."""
    seen: list[str] = []

    def fake_turn(*args: Any, **kwargs: Any) -> None:
        line: str = args[3]
        output_fn: Callable[[str], None] = args[4]
        seen.append(line)
        output_fn(f"echo:{line}")

    monkeypatch.setattr(app, "_run_turn", fake_turn)
    return seen


# --------------------------------------------------------------- the pump


def test_pump_is_queue_driven_with_monotonic_ids_and_markers(
    echo_turns: list[str],
) -> None:
    """turn in (queue) → events out (ids 1,2,3,...) → QUIT ends between turns.

    Ordering pins: every processed command's texts are followed by exactly
    one ``turn_end``; blank lines are consumed without a turn or marker;
    ``/`` commands route to the transcript handler (the real one — even a
    deterministic UI turn gets its completion marker); ``exit`` ends the
    session without ever becoming a turn.
    """
    events: list[EngineEvent] = []
    emitter = EventEmitter(events.append)
    commands: queue.Queue[Any] = queue.Queue()
    for line in ("hi", "   ", "/details", "exit", "never-read", app.QUIT):
        commands.put(line)  # exit returns BEFORE the last two are consumed
    app._pump(
        _make_loop(),
        emitter.text,
        commands,
        flow=TxFlow(),
        session=app.SendSession(),
        table={},
        emitter=emitter,
    )
    assert echo_turns == ["hi"]
    assert [(e.kind, e.payload) for e in events] == [
        (EVENT_TEXT, "echo:hi"),
        (EVENT_TURN_END, ""),
        (EVENT_TEXT, app._DETAILS_NONE),
        (EVENT_TURN_END, ""),
    ]
    ids = [e.id for e in events]
    assert ids == [1, 2, 3, 4]  # strictly monotonic, no gaps, no reuse


def test_pump_never_cancels_an_inflight_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never-cancel (consult F5): QUIT queued DURING a turn is honored only
    after the turn completes — there is no cancellation hook in the pump."""
    done: list[str] = []

    def slow_turn(*args: Any, **kwargs: Any) -> None:
        time.sleep(0.15)
        done.append("turn")

    monkeypatch.setattr(app, "_run_turn", slow_turn)
    commands: queue.Queue[Any] = queue.Queue()
    commands.put("long turn")
    commands.put(app.QUIT)
    app._pump(
        _make_loop(),
        lambda _s: None,
        commands,
        flow=TxFlow(),
        session=app.SendSession(),
        table={},
    )
    assert done == ["turn"]  # the queued QUIT could not cut the turn short


def test_pump_reraises_feeder_errors_on_the_engine_thread() -> None:
    """The CLI's exception semantics survive the thread hop."""
    commands: queue.Queue[Any] = queue.Queue()
    commands.put(app._PumpError(RuntimeError("feeder boom")))
    with pytest.raises(RuntimeError, match="feeder boom"):
        app._pump(
            _make_loop(),
            lambda _s: None,
            commands,
            flow=TxFlow(),
            session=app.SendSession(),
            table={},
        )


# -------------------------------------------------------- the CLI adapter


def test_cli_repl_feeds_the_pump_through_a_queue(
    echo_turns: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """The (unwrapped) CLI adapter still honors input_fn/output_fn exactly:
    reads never run ahead (the prompt lands only after the previous turn
    fully printed), EOF ends cleanly, exhaustion re-raises like before, and
    stdout stays untouched."""
    reads: list[int] = []
    outputs: list[str] = []

    def input_fn(_prompt: str) -> str:
        reads.append(len(outputs))  # what the user sees when the read lands
        return ["one", "two", "exit"][len(reads) - 1]

    _repl_cli(
        _make_loop(),
        outputs.append,
        input_fn,
        flow=TxFlow(),
        session=app.SendSession(),
        table={},
    )
    assert echo_turns == ["one", "two"]
    assert outputs == ["echo:one", "echo:two"]
    assert reads == [0, 1, 2]  # each read paced after the previous output
    assert capsys.readouterr().out == ""

    def eof(_prompt: str) -> str:
        raise EOFError

    _repl_cli(  # EOF ends the session quietly
        _make_loop(),
        outputs.append,
        eof,
        flow=TxFlow(),
        session=app.SendSession(),
        table={},
    )

    def broken(_prompt: str) -> str:
        raise StopIteration

    with pytest.raises(StopIteration):
        _repl_cli(
            _make_loop(),
            outputs.append,
            broken,
            flow=TxFlow(),
            session=app.SendSession(),
            table={},
        )
    _wait_for(
        lambda: not [t for t in threading.enumerate() if t.name == "repl-stdin"]
    )


def test_cli_mode_has_no_engine_thread_main_thread_is_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI mode: the pump runs on the calling (main) thread; the thread
    named "engine" belongs to threaded mode only."""
    idents: list[int] = []

    def capture_turn(*args: Any, **kwargs: Any) -> None:
        idents.append(threading.get_ident())

    monkeypatch.setattr(app, "_run_turn", capture_turn)
    lines = iter(["hi", "exit"])
    _repl_cli(
        _make_loop(),
        lambda _s: None,
        lambda _p: next(lines),
        flow=TxFlow(),
        session=app.SendSession(),
        table={},
    )
    assert idents == [threading.get_ident()]
    assert [t for t in threading.enumerate() if t.name == "engine"] == []


# ------------------------------------------------------------ engine thread


def test_start_engine_bootstrap_and_turn_run_on_the_engine_thread(
    tmp_path: Path,
) -> None:
    """Threaded mode (ADR-0024 §3): the Store is CONSTRUCTED on the engine
    thread (sqlite ``check_same_thread`` is the guard), the turn's handler
    reads it there, and the submitted turn produces events ending in the
    completion marker before shutdown is honored."""
    seen: dict[str, int] = {}

    def bootstrap() -> app.EngineContext:
        store = Store(tmp_path / "engine.db")  # engine thread — legal
        seen["construct"] = threading.get_ident()

        def get_balance(envelope: Any) -> dict[str, object]:
            del envelope
            seen["handler"] = threading.get_ident()
            store.get_utxos_for_wallet(1)  # would explode off-thread
            return {
                "confirmed_sats": 0,
                "unconfirmed_sats": 0,
                "total_sats": 0,
                "addresses_scanned": 0,
            }

        table = {
            IntentName.GET_BALANCE: get_balance,
            IntentName.RESPOND: app._respond_handler,
            IntentName.CLARIFY: app._clarify_handler,
        }
        return app.EngineContext(
            loop=_make_loop(table),
            flow=TxFlow(),
            session=app.SendSession(),
            table=table,
        )

    events: list[EngineEvent] = []
    handle = app.start_engine(bootstrap, events.append)
    handle.submit("what's my balance?")
    _wait_for(lambda: any(e.kind == EVENT_TURN_END for e in events))
    handle.shutdown()
    thread = handle.thread
    assert thread is not None
    thread.join(10)
    assert handle.error is None
    assert thread.is_alive() is False
    assert seen["construct"] == seen["handler"] == thread.ident
    texts = [e.payload for e in events if e.kind == EVENT_TEXT]
    assert any(text.startswith("Balance (mainnet):") for text in texts)
    ids = [e.id for e in events]
    assert ids == sorted(ids) and len(ids) == len(set(ids))
    assert events[-1].kind == EVENT_TURN_END


# -------------------------------------------------- typed /state snapshot (WEB-003)


def test_state_snapshot_request_is_answered_on_the_engine_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A StateSnapshotRequest serializes through the SAME command queue and is
    answered ON the engine thread (never via ``_run_turn``, so it is not a model
    turn) and produces NO output events (no chat line, no help echo)."""
    monkeypatch.setattr(app, "_run_turn", lambda *a, **k: None)
    build_threads: list[int] = []
    real_build = app.build_state_snapshot

    def spy_build(flow, session, watcher, scan=None, model=None, backend_kind=None,
                  preload=None):
        build_threads.append(threading.get_ident())
        return real_build(flow, session, watcher, scan, model, backend_kind, preload)

    monkeypatch.setattr(app, "build_state_snapshot", spy_build)
    events: list[EngineEvent] = []
    handle = app.start_engine(
        lambda: app.EngineContext(
            loop=_make_loop(), flow=TxFlow(), session=app.SendSession(),
            table={IntentName.RESPOND: app._respond_handler},
        ),
        events.append,
    )
    assert handle.thread is not None
    snap = handle.request_state(5.0)
    handle.shutdown()
    handle.thread.join(10)

    assert snap == {
        "schema": "state/1",
        "flow_state": "idle",
        "pending_present": False,
        "gate_decision": "not_a_decision",
        "watch": {"configured": False, "enabled": False},
        # TCK-WEB-005: no scan flow wired → the closed "disabled" state, and
        # no durable first-scan record. Additive keys under the UNCHANGED
        # state/1 tag (the pinned client ignores unknown keys).
        "scan_state": "disabled",
        "first_scan_complete": False,
    }
    # Answered by the ENGINE thread, not the caller (main):
    assert build_threads == [handle.thread.ident]
    assert handle.thread.ident != threading.main_thread().ident
    # A snapshot is a reply, not a transcript turn — no events were emitted:
    assert events == []


def test_request_state_times_out_when_the_engine_never_drains() -> None:
    """No engine thread draining the queue (e.g. a dead bootstrap) is not a
    hang: ``request_state`` returns ``None`` at the timeout so the transport
    falls back to the transport-only shape (GET /state keeps answering)."""
    handle = app.EngineHandle(
        commands=queue.Queue(), emitter=app.EventEmitter(lambda _e: None)
    )
    started = time.monotonic()
    assert handle.request_state(0.2) is None
    assert time.monotonic() - started < 2.0  # returned at the timeout, not a stall


def test_build_state_snapshot_is_value_free() -> None:
    """The only facts are enum NAMES + booleans; a pending record's recipient/
    amount/tx_ref/psbt can never appear (no value-bearing field is read)."""
    from localwallet.tx.flow import PendingTx

    flow = TxFlow()
    pending = PendingTx(
        tx_ref="REFSECRET", created_at=0.0, amount_sats=654321,
        recipient="bc1qLEAK", fee_target=None, fee_rate_sat_vb=1, fee_sats=1000,
        change_sats=None, psbt_base64="cHNidP8LEAK", inputs_count=1, vsize=100,
    )
    flow._state = app.TxFlowStatus.CREATED
    flow._pending = pending
    snap = app.build_state_snapshot(flow, app.SendSession(), None)
    assert snap["flow_state"] == "created"
    assert snap["pending_present"] is True
    # TCK-WEB-005 scan fields are a closed state NAME and a BOOL — never a
    # count/percent (a progress value would leak wallet size indirectly).
    assert snap["scan_state"] == "disabled"
    assert snap["first_scan_complete"] is False
    dumped = repr(snap)
    for leak in ("REFSECRET", "654321", "bc1qLEAK", "cHNidP8"):
        assert leak not in dumped


def test_store_built_off_the_engine_thread_is_refused(tmp_path: Path) -> None:
    """The ``check_same_thread`` guard is real: state constructed on the
    WRONG thread fails closed inside the bootstrap (surfaced on
    ``handle.error``) — which is why WEB-002 must build its state via
    :func:`localwallet.app.start_engine`'s bootstrap."""
    store = Store(tmp_path / "main.db")  # main thread — deliberately wrong

    def bootstrap() -> app.EngineContext:
        store.list_wallets()
        raise AssertionError("unreachable")

    handle = app.start_engine(bootstrap, lambda _ev: None)
    thread = handle.thread
    assert thread is not None
    thread.join(10)
    assert isinstance(handle.error, sqlite3.ProgrammingError)


# ------------------------------------------------------------ scan dots


def _summary() -> ScanSummary:
    return ScanSummary(
        wallet_id=1,
        gap_limit=20,
        tip_height=870000,
        scanned_at="x",
        utxo_count=1,
        truncated=False,
    )


def test_scan_dots_are_events_for_non_cli_transports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """TCK-UX-001 (re-scoped onto the non-blocking scan, TCK-SCAN-003): the
    startup scan's progress dots flow through the engine event stream —
    ``progress`` events on the given ``emitter`` — and the completion
    narration follows when the ENGINE persists the worker's record set. A
    non-CLI transport keeps the console clean (nothing leaks to stdout).

    ``handle_command`` is the engine-thread half of :class:`ScanFlow`: the
    exact path the pump drives from the command queue (the worker delivers
    the same markers). Persistence stays single-threaded here (the engine),
    never on the worker (pinned in tests/test_scan_flow.py).
    """

    def fake_persist(_store: object, _records: object) -> ScanSummary:
        return _summary()

    monkeypatch.setattr(app.wallet_scan, "persist_scan", fake_persist)
    events: list[EngineEvent] = []
    outputs: list[str] = []
    emitter = EventEmitter(events.append)
    store = Store(None)
    wallet = store.create_wallet("default", "desc")
    worker = app.ChainWorker(None)  # client unused: fetch never runs here
    flow = app.ScanFlow(
        store, wallet, worker, gap_limit=None, startup_plan=object()
    )
    flow.attach(queue.Queue())
    try:
        for _ in range(3):  # three bare ticks → three dots
            assert flow.handle_command(app._ScanTick(), outputs.append, emitter)
        assert flow.handle_command(
            app._ScanDone(True, object()), outputs.append, emitter
        )
    finally:
        worker.stop()
    assert capsys.readouterr().out == ""  # nothing hit the console
    assert [(e.kind, e.payload) for e in events] == [
        (EVENT_PROGRESS, "."),
        (EVENT_PROGRESS, "."),
        (EVENT_PROGRESS, "."),
        (EVENT_PROGRESS, "\n"),
    ]
    assert outputs == [
        "Startup scan complete: 1 UTXOs · tip height 870000.",
    ]


def test_cli_sink_renders_events_like_the_pre_web_repl(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI-mode unchanged: text → output_fn, progress → raw stdout flushed,
    markers invisible — byte-identical to the old direct writes."""
    outputs: list[str] = []
    emitter = cli_emitter(outputs.append)
    emitter.text("a line")
    emitter.emit(EVENT_PROGRESS, "..")
    emitter.emit(EVENT_PROGRESS, "\n")
    emitter.emit(EVENT_TURN_END)
    assert outputs == ["a line"]
    assert capsys.readouterr().out == "..\n"


# ------------------------------------------------------------- seam proof


def test_run_session_flows_through_the_queue_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The e2e seam proof, made explicit: a real ``run()`` session (unedited
    CLI call shape) went through the conftest harness — input injected via
    the command queue, output collected as monotonic-id events whose text
    is byte-identical to what ``output_fn`` received — and no engine thread
    exists in CLI mode."""
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv(app.AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.delenv("LOCALWALLET_MODEL_PATH", raising=False)
    monkeypatch.delenv("LOCALWALLET_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCALWALLET_LLM_MODEL", raising=False)
    outputs: list[str] = []
    lines = iter(["hello there", "exit"])
    before = HARNESS["runs"]
    code = app.run(
        ["--stub-llm", "--zpub", ZPUB],
        input_fn=lambda _p: next(lines),
        output_fn=outputs.append,
    )
    assert code == 0
    assert HARNESS["runs"] == before + 1  # this session ran on the pump
    events = HARNESS["events"]
    ids = [e.id for e in events]
    assert ids[0] == 1 and ids == sorted(ids) and len(ids) == len(set(ids))
    assert events[-1].kind == EVENT_TURN_END  # turn completed, then exit
    texts = [e.payload for e in events if e.kind == EVENT_TEXT]
    assert texts  # the respond turn narrated
    assert all(text in outputs for text in texts)  # CLI forwarding intact
    assert [t for t in threading.enumerate() if t.name == "engine"] == []

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
    """TCK-UX-001 dots no longer leak straight to stdout once an emitter is
    given: a non-CLI transport receives them as ``progress`` events (and
    only that — the console stays clean)."""

    def fake_scan(
        store: Store,
        client: object,
        wallet: object,
        *,
        progress_fn: Callable[[], None] | None = None,
        gap_limit: int | None = None,
    ) -> ScanSummary:
        assert progress_fn is not None  # strict zero-arg tick shape
        for _ in range(3):
            progress_fn()
        return _summary()

    monkeypatch.setattr(app.wallet_scan, "scan_wallet", fake_scan)
    monkeypatch.setenv(app.AUTO_SCAN_ENV_VAR, "1")
    events: list[EngineEvent] = []
    outputs: list[str] = []
    store = Store(None)
    wallet = store.create_wallet("default", "desc")
    app._startup_scan(
        store,
        None,
        wallet,
        rescan_requested=False,
        output_fn=outputs.append,
        emitter=EventEmitter(events.append),
    )
    assert capsys.readouterr().out == ""  # nothing hit the console
    assert [(e.kind, e.payload) for e in events] == [
        (EVENT_PROGRESS, "."),
        (EVENT_PROGRESS, "."),
        (EVENT_PROGRESS, "."),
        (EVENT_PROGRESS, "\n"),
    ]
    assert outputs == [
        app.SCAN_PROGRESS_NOTICE,
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

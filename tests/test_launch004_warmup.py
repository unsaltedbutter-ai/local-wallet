"""TCK-LAUNCH-004 (engine half): startup warm-up + web beat order.

User spec 2026-09-13:
1. WARM-UP — after the preload reaches ``ready``, ONE tiny real bounded
   generation runs (log-only: never narrated, never a transcript/SSE turn),
   best-effort (a failure logs value-free and changes nothing), and it must
   not delay engine readiness (the pump stays live while it runs).
2. BEAT ORDER (web) — no "Loading local llm." bubble in the transcript (the
   loading state rides ``/state`` ``model_state='loading'``; the compose
   area renders it); the transcript gets "Local llm fully loaded." FIRST,
   then the privacy notice, then the checking-for-new-transactions line.
   The CLI keeps BOTH lines in the old order (byte-compat decision).
3. One value-free launch-log timing line at warm-up completion (``ms``).

No test here touches the real GGUF or llama.cpp: the flow/pump levels use
duck-typed fake runtimes, the serialization pin injects a fake ``llama_cpp``
module (the TCK-LAUNCH-003 pattern).
"""

from __future__ import annotations

import queue
import re
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

from localwallet import app
from localwallet.agent.runtime import ModelRuntime


class _WarmRuntime:
    """Duck-typed ModelRuntime: instant ``load()``, ``generate()`` records
    (prompt, max_tokens) and returns a canned string."""

    def __init__(self, *, gen_error: Exception | None = None) -> None:
        self.loaded = threading.Event()
        self.gen_calls: list[tuple[str, int | None]] = []
        self.gen_entered = threading.Event()
        self.gen_release = threading.Event()
        self.gen_release.set()  # open by default: no gate
        self._gen_error = gen_error

    def load(self) -> None:
        self.loaded.set()

    def generate(
        self, prompt: str, *, grammar_text: str | None = None, max_tokens: int | None = None
    ) -> str:
        self.gen_calls.append((prompt, max_tokens))
        self.gen_entered.set()
        assert self.gen_release.wait(15), "warm-up gate never opened"
        if self._gen_error is not None:
            raise self._gen_error
        return '{"kind":"respond"}'


class _LoadBoom(Exception):
    pass


def _pump_with(
    flow: app.ModelPreloadFlow,
    flush_startup: Any = None,
) -> tuple[list[app.EngineEvent], queue.Queue[Any], threading.Thread]:
    events: list[app.EngineEvent] = []
    emitter = app.EventEmitter(events.append)
    commands: queue.Queue[Any] = queue.Queue()
    thread = threading.Thread(
        target=lambda: app._pump(
            app.AgentLoop(
                app.stub_generate, {app.IntentName.RESPOND: app._respond_handler}
            ),
            emitter.text,
            commands,
            flow=app.TxFlow(),
            session=app.SendSession(),
            table={},
            emitter=emitter,
            preload=flow,
            flush_startup=flush_startup,
        ),
        daemon=True,
    )
    thread.start()
    return events, commands, thread


def _quit_and_join(commands: queue.Queue[Any], thread: threading.Thread) -> None:
    commands.put(app.QUIT)
    thread.join(15)
    assert not thread.is_alive()


def _wait_for(predicate: Any, deadline_s: float = 15.0) -> None:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.02)
    assert predicate(), "condition never reached"


def _texts(events: list[app.EngineEvent]) -> list[str]:
    return [e.payload for e in events if e.kind == "text"]


def _closed_bubbles(events: list[app.EngineEvent]) -> list[str]:
    """The web bubble model: texts grouped until each turn_end."""
    bubbles: list[str] = []
    current: list[str] = []
    for e in events:
        if e.kind == "text":
            current.append(e.payload)
        elif e.kind == app.EVENT_TURN_END and current:
            bubbles.append("\n".join(current))
            current = []
    return bubbles


# ------------------------------------------------------------- 1. warm-up


def test_warmup_fires_once_after_ready_bounded_and_log_only(
    tmp_path: Path,
) -> None:
    """The deliverable-1 pin: after ``ready``, ONE generate with the code
    prompt + the bounded token cap; the ONLY trace is the value-free
    launch-log timing line — zero extra transcript events."""
    logs: list[str] = []
    runtime = _WarmRuntime()
    flow = app.ModelPreloadFlow(
        runtime, model_path=str(tmp_path / "m.gguf"), log_fn=logs.append
    )
    events, commands, thread = _pump_with(flow)
    try:
        commands.put(app.PRELOAD_START)
        _wait_for(lambda: len(logs) == 1)
        assert runtime.gen_calls == [(app.MODEL_WARMUP_PROMPT, app.MODEL_WARMUP_MAX_TOKENS)]
        assert 0 < app.MODEL_WARMUP_MAX_TOKENS <= 16  # bounded
        assert re.fullmatch(
            r"model warmup completed in \d+ms", logs[0]
        ), logs  # the timing line, value-free (only fixed words + an int)
        texts = _texts(events)
        assert texts.count(app.MODEL_PRELOADED_NOTICE) == 1
        # LOG-ONLY: no warm-up word ever reaches the transcript...
        joined = "\n".join(texts)
        assert "warmup" not in joined and "warm" not in joined
        # ...and no extra turn (turn_ends are only the two preload beats).
        assert (
            sum(1 for e in events if e.kind == app.EVENT_TURN_END) == 2
        )
    finally:
        _quit_and_join(commands, thread)


def test_warmup_failure_is_swallowed_value_free(tmp_path: Path) -> None:
    """A raising warm-up = one value-free log line, state stays ``ready``,
    the pump keeps answering /state — nothing else changes."""
    logs: list[str] = []
    runtime = _WarmRuntime(
        gen_error=RuntimeError("boom /home/homer/private.gguf leaked")
    )
    flow = app.ModelPreloadFlow(
        runtime, model_path=str(tmp_path / "m.gguf"), log_fn=logs.append
    )
    events, commands, thread = _pump_with(flow)
    try:
        commands.put(app.PRELOAD_START)
        _wait_for(lambda: len(logs) == 1)
        assert re.fullmatch(
            r"model warmup failed \(ignored\) in \d+ms", logs[0]
        ), logs
        assert flow.state == "ready"  # the failure changes NOTHING
        reply: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        commands.put(app.StateSnapshotRequest(app.STATE_SNAPSHOT_COMMAND, reply))
        assert reply.get(15)["model_state"] == "ready"
        joined = "\n".join(_texts(events)) + "\n".join(logs)
        for leak in ("boom", "homer", "private.gguf", str(tmp_path)):
            assert leak not in joined  # the error text never travels
    finally:
        _quit_and_join(commands, thread)


def test_warmup_never_runs_after_a_failed_preload(tmp_path: Path) -> None:
    """The failed-load path never warms (there is nothing to warm) and logs
    only the existing preload-failure line."""

    class _FailRuntime:
        def __init__(self) -> None:
            self.gen_calls: list[str] = []

        def load(self) -> None:
            raise _LoadBoom("no")

        def generate(self, prompt: str, **_kw: object) -> str:
            self.gen_calls.append(prompt)
            return ""

    logs: list[str] = []
    runtime = _FailRuntime()
    flow = app.ModelPreloadFlow(
        runtime, model_path=str(tmp_path / "m.gguf"), log_fn=logs.append
    )
    _events, commands, thread = _pump_with(flow)
    try:
        commands.put(app.PRELOAD_START)
        _wait_for(lambda: flow.state == "failed")
        reply: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        commands.put(app.StateSnapshotRequest(app.STATE_SNAPSHOT_COMMAND, reply))
        assert reply.get(15)["model_state"] == "failed"  # marker consumed
        assert runtime.gen_calls == []
        assert logs == [app._MODEL_PRELOAD_FAILED_LOG]  # no warm-up line
    finally:
        _quit_and_join(commands, thread)


def test_engine_readiness_is_not_delayed_and_pump_stays_live(
    tmp_path: Path,
) -> None:
    """``ready`` + the loaded line land WHILE the warm-up generation is
    still in flight: /state answers ``ready`` and the notice bubble is
    already out — the warm-up runs on its own daemon thread."""
    logs: list[str] = []
    runtime = _WarmRuntime()
    runtime.gen_release.clear()  # hold the warm-up call inside generate()
    flow = app.ModelPreloadFlow(
        runtime, model_path=str(tmp_path / "m.gguf"), log_fn=logs.append
    )
    events, commands, thread = _pump_with(flow)
    try:
        commands.put(app.PRELOAD_START)
        _wait_for(lambda: app.MODEL_PRELOADED_NOTICE in _texts(events))
        _wait_for(lambda: runtime.gen_entered.is_set())
        assert flow.state == "ready"
        assert logs == []  # the completion line is NOT yet written
        reply: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        commands.put(app.StateSnapshotRequest(app.STATE_SNAPSHOT_COMMAND, reply))
        assert reply.get(15)["model_state"] == "ready"  # pump LIVE mid-warmup
        runtime.gen_release.set()
        _wait_for(lambda: len(logs) == 1)  # bounded: it does complete
    finally:
        runtime.gen_release.set()
        _quit_and_join(commands, thread)


# --------------------------------------------------- 2. web beat order (hold)


def test_web_beat_order_loaded_first_privacy_then_checking(tmp_path: Path) -> None:
    """The full LAUNCH-004 web surface, driven through the REAL
    :func:`start_engine` (the hold decision, the bind, the pump and the
    flush are the production code path, not a hand-replica): transcript
    bubbles = [fully loaded, privacy notice, checking-for-transactions] and
    NO 'Loading local llm.' bubble at all."""
    out = app._Output(
        web=True,
        terminal=lambda _s: None,
        log=app._Log(str(tmp_path / "watch.db"), "test"),
    )
    out("Privacy notice: testing.")
    out(app.SCAN_PROGRESS_NOTICE)
    flow = app.ModelPreloadFlow(
        _WarmRuntime(),
        model_path=str(tmp_path / "m.gguf"),
        loading_notice=False,  # what run() passes on a web launch
    )
    ctx = app.EngineContext(
        loop=app.AgentLoop(
            app.stub_generate, {app.IntentName.RESPOND: app._respond_handler}
        ),
        flow=app.TxFlow(),
        session=app.SendSession(),
        table={},
        preload=flow,
        output=out,
    )
    events: list[app.EngineEvent] = []
    engine = app.start_engine(lambda: ctx, events.append)
    try:
        engine.commands.put(app.PRELOAD_START)
        _wait_for(lambda: len(_texts(events)) >= 3)
        bubbles = _closed_bubbles(events)
        assert bubbles == [
            app.MODEL_PRELOADED_NOTICE,
            "Privacy notice: testing.",
            app.SCAN_PROGRESS_NOTICE,
        ]
        assert app.MODEL_PRELOAD_NOTICE not in "\n".join(_texts(events))
    finally:
        engine.shutdown()
        engine.thread.join(15)
        assert not engine.thread.is_alive()
        out.close()


def test_cli_shape_keeps_loading_line_before_loaded_line(tmp_path: Path) -> None:
    """The byte-compat DECISION: the CLI flow (``loading_notice`` default
    True, no startup hold) keeps BOTH lines in the ORIGINAL order —
    'Loading local llm.' then 'Local llm fully loaded.' (the existing
    onb007/chat-run pins keep the full transcript; this is the flow-level
    order pin)."""
    logs: list[str] = []
    flow = app.ModelPreloadFlow(
        _WarmRuntime(),
        model_path=str(tmp_path / "m.gguf"),
        log_fn=logs.append,  # default loading_notice=True = the CLI shape
    )
    events, commands, thread = _pump_with(flow)
    try:
        commands.put(app.PRELOAD_START)
        _wait_for(lambda: app.MODEL_PRELOADED_NOTICE in _texts(events))
        _wait_for(lambda: len(logs) == 1)  # warm-up settled (marker consumed)
        texts = _texts(events)
        assert texts.index(app.MODEL_PRELOAD_NOTICE) < texts.index(
            app.MODEL_PRELOADED_NOTICE
        )
        assert app.MODEL_WARMUP_PROMPT not in texts  # still never narrated
    finally:
        _quit_and_join(commands, thread)


# ------------------------------------------- runtime-side warm serialization


def test_query_during_warmup_waits_then_rides_the_warm_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime seam that makes a background warm-up safe: ONE llama
    call at a time (shared KV context), a user query landing mid-warm-up
    BLOCKS and then runs — never a second construction, never a drop. The
    query skips the cold grammar compile (cached by the warm-up) and keeps
    the runtime default token cap (only the warm-up caps itself)."""
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"GGUF")
    active = 0
    peak = 0
    lock = threading.Lock()
    calls: list[dict[str, Any]] = []
    warm_in = threading.Event()
    warm_out = threading.Event()

    class FakeLlama:
        def __init__(self, *, model_path: str, **_kw: object) -> None:
            self.model_path = model_path

        def __call__(self, **kw: Any) -> dict[str, Any]:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                calls.append(kw)
                if len(calls) == 1:
                    warm_in.set()
                    assert warm_out.wait(15), "warm-up gate never opened"
                active -= 1
            return {"choices": [{"text": '{"kind":"respond"}'}]}

    class FakeGrammar:
        @classmethod
        def from_string(cls, _text: str) -> FakeGrammar:
            return cls()

    fake_mod = types.ModuleType("llama_cpp")
    fake_mod.Llama = FakeLlama  # type: ignore[attr-defined]
    fake_mod.LlamaGrammar = FakeGrammar  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "llama_cpp", fake_mod)

    rt = ModelRuntime(model_path=str(gguf))
    rt.load()  # the preload already built it

    warm: list[str] = []
    warmer = threading.Thread(
        target=lambda: warm.append(
            rt.generate(app.MODEL_WARMUP_PROMPT, max_tokens=app.MODEL_WARMUP_MAX_TOKENS)
        ),
        daemon=True,
    )
    warmer.start()
    assert warm_in.wait(15)  # the warm-up call is IN FLIGHT, lock held

    outcomes: list[str] = []
    query = threading.Thread(target=lambda: outcomes.append(rt.generate("user prompt")))
    query.start()
    query.join(0.3)
    assert query.is_alive()  # the first query WAITS behind the warm-up
    warm_out.set()
    warmer.join(15)
    query.join(15)
    assert warm == ['{"kind":"respond"}'] and outcomes == ['{"kind":"respond"}']
    assert peak == 1  # never two llama calls concurrently
    assert calls[0]["max_tokens"] == app.MODEL_WARMUP_MAX_TOKENS  # bounded
    assert calls[1]["max_tokens"] == rt.max_tokens  # the turn keeps the default
    # No cold grammar work left for the first real turn: the warm-up
    # compiled the grammar; the query reused BOTH caches.
    assert rt._grammar is not None
    grammar_id = id(rt._grammar)
    rt.generate("another turn")
    assert id(rt._grammar) == grammar_id

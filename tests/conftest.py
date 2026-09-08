"""TCK-WEB-001 seam-proof harness (ADR-0024 §3).

Every existing REPL-driven e2e test (the ~96 in
``tests/test_e2e_skeleton.py`` and friends) runs UNEDITED against the
queue-driven engine pump: this autouse fixture wraps
:func:`localwallet.app._repl` so input lines still originate from the
test's ``input_fn`` but are injected THROUGH the engine's command
``queue.Queue`` (the pump consumes them with a blocking ``queue.get()``),
and every output is collected as a monotonic-id ``EngineEvent`` while the
CLI sink keeps rendering text/progress exactly as before (so stdout and
the tests' ``output_fn`` lists are byte-identical).

If the seam were not real — the same ``_run_turn`` path behind a blocking
queue with an event-emitter output — these tests would fail HERE, not by
being rewritten. ``HARNESS`` lets the engine-pump pins prove that every
run() session went through the pump.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.app import EngineEvent, EventEmitter, cli_sink

#: ``{"runs": <sessions driven through the queue pump>, "events": [<EngineEvent
#: ...> of the most recent run]}`` — read by tests/test_engine_pump.py.
HARNESS: dict[str, Any] = {"runs": 0, "events": []}


@pytest.fixture(autouse=True)
def repl_engine_pump(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route every ``_repl`` session through the queue-driven pump + events."""
    original = app._repl

    def harness_repl(
        loop: AgentLoop,
        output_fn: Callable[[str], None],
        input_fn: Callable[[str], str],
        **kwargs: Any,
    ) -> None:
        events: list[EngineEvent] = []
        to_cli = cli_sink(output_fn)

        def sink(event: EngineEvent) -> None:
            events.append(event)
            to_cli(event)

        HARNESS["runs"] += 1
        HARNESS["events"] = events
        original(
            loop, output_fn, input_fn, emitter=EventEmitter(sink), **kwargs
        )

    monkeypatch.setattr(app, "_repl", harness_repl)

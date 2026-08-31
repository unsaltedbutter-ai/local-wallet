"""Tests for the agent loop (TCK-P0-005).

No model file and no llama.cpp wheel are needed: every test drives the
loop through a ``generate_fn`` stub returning canned envelope JSON,
malformed JSON, or garbage. Covered:

- happy path for each closed intent (respond / clarify / get_balance);
- needs_retry → exactly ONE re-prompt (correction note present, generate
  called twice, retry-budget propagation proven), then ok;
- second failure → clarified status with a user-facing message;
- MAX_TURNS_PER_REQUEST bounds generation (no infinite loop);
- facts rendering reaches the prompt as a structured block;
- history recording, reuse in prompts, and the MAX_HISTORY_TURNS prune;
- model text is never executed — garbage/code-like output only ever flows
  through handle_raw validation;
- handler exceptions surface as escalation, never crash the loop.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Final

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.agent.loop import (
    MAX_HISTORY_TURNS,
    MAX_TURNS_PER_REQUEST,
    AgentLoop,
    AgentTurnStatus,
)
from localwallet.protocol import DispatchTable, Envelope, IntentName

# ---------------------------------------------------------------- constants

RESPOND_JSON: Final[str] = (
    '{"v": 0, "intent": "respond", "params": {"text": "Your balance is 900 sats."}}'
)
CLARIFY_JSON: Final[str] = (
    '{"v": 0, "intent": "clarify", "params": {"question": "20 what - sats, BTC, or USD?"}}'
)
GET_BALANCE_JSON: Final[str] = '{"v": 0, "intent": "get_balance", "params": {}}'
GARBAGE: Final[str] = "this is not json at all <<<>>>"

HANDLER_RESULTS: Final[dict[IntentName, dict[str, object]]] = {
    IntentName.RESPOND: {"narrated": True},
    IntentName.CLARIFY: {"asked": True},
    IntentName.GET_BALANCE: {"confirmed_sat": 900, "unconfirmed_sat": 0},
}


# ---------------------------------------------------------------- helpers


class ScriptedGenerate:
    """generate_fn stub: scripted responses, then GARBAGE forever."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, prompt: str, grammar_text: str | None) -> str:
        self.calls.append((prompt, grammar_text))
        if self.responses:
            return self.responses.pop(0)
        return GARBAGE


def make_table(
    recorded: list[Envelope] | None = None,
    *,
    boom: bool = False,
) -> DispatchTable:
    """Dispatch table with one recording stub handler per known intent."""
    recorded = recorded if recorded is not None else []

    def handler(envelope: Envelope) -> dict[str, object]:
        recorded.append(envelope)
        if boom:
            msg = "handler exploded"
            raise ValueError(msg)
        return HANDLER_RESULTS[IntentName(envelope.intent)]

    return {intent: handler for intent in HANDLER_RESULTS}


# ------------------------------------------------------------------ happy paths


class TestHappyPaths:
    def test_ok_respond(self) -> None:
        gen = ScriptedGenerate([RESPOND_JSON])
        recorded: list[Envelope] = []
        loop = AgentLoop(gen, make_table(recorded))

        result = loop.run("what can you do?", {})

        assert result.status is AgentTurnStatus.OK
        assert result.envelope is not None
        assert result.envelope.intent is IntentName.RESPOND
        assert result.result == HANDLER_RESULTS[IntentName.RESPOND]
        assert result.user_message is None
        assert result.turns_used == 1
        assert len(gen.calls) == 1
        assert len(recorded) == 1
        # grammar text is passed to the seam and is the real envelope grammar
        prompt, grammar_text = gen.calls[0]
        assert "root ::=" in (grammar_text or "")
        assert "user: what can you do?" in prompt

    def test_ok_clarify_surfaces_question(self) -> None:
        gen = ScriptedGenerate([CLARIFY_JSON])
        loop = AgentLoop(gen, make_table())

        result = loop.run("send 20 to my brother", {})

        assert result.status is AgentTurnStatus.OK
        assert result.envelope is not None
        assert result.envelope.intent is IntentName.CLARIFY
        assert result.user_message == "20 what - sats, BTC, or USD?"

    def test_ok_get_balance_returns_handler_result(self) -> None:
        gen = ScriptedGenerate([GET_BALANCE_JSON])
        recorded: list[Envelope] = []
        loop = AgentLoop(gen, make_table(recorded))

        result = loop.run("how much do I have?", {"balance_confirmed_sat": 900})

        assert result.status is AgentTurnStatus.OK
        assert result.envelope is not None
        assert result.envelope.intent is IntentName.GET_BALANCE
        assert result.result == HANDLER_RESULTS[IntentName.GET_BALANCE]
        # facts reach the prompt as a structured block for the model to narrate
        prompt = gen.calls[0][0]
        assert "FACTS BEGIN" in prompt
        assert "balance_confirmed_sat: 900" in prompt
        assert "FACTS END" in prompt

    def test_handler_receives_validated_envelope(self) -> None:
        gen = ScriptedGenerate([GET_BALANCE_JSON])
        recorded: list[Envelope] = []
        loop = AgentLoop(gen, make_table(recorded))

        loop.run("balance?", {})

        assert len(recorded) == 1
        assert isinstance(recorded[0], Envelope)
        assert recorded[0].v == 0


# ----------------------------------------------------------------- retry policy


class TestRetryPolicy:
    def test_needs_retry_reprompts_exactly_once(self) -> None:
        gen = ScriptedGenerate([GARBAGE, RESPOND_JSON])
        loop = AgentLoop(gen, make_table())

        result = loop.run("hello", {})

        assert result.status is AgentTurnStatus.OK
        assert result.turns_used == 2
        assert len(gen.calls) == 2
        first_prompt, second_prompt = gen.calls[0][0], gen.calls[1][0]
        assert "RETRY NOTE" not in first_prompt
        assert "RETRY NOTE" in second_prompt
        # the correction note is value-free and instructs a corrected envelope
        assert "v, intent, params" in second_prompt
        # re-prompt is built on the original prompt, not chained
        assert second_prompt.startswith(first_prompt)

    def test_retry_budget_propagates_to_handle_raw(self) -> None:
        # If validation_failure_count were not propagated, the third attempt
        # would still be granted a retry. Exactly two generate calls prove it.
        gen = ScriptedGenerate([GARBAGE, GARBAGE, RESPOND_JSON])
        loop = AgentLoop(gen, make_table())

        result = loop.run("hello", {})

        assert result.status is AgentTurnStatus.CLARIFIED
        assert len(gen.calls) == 2
        assert result.turns_used == 2

    def test_second_failure_escalates_to_clarify(self) -> None:
        gen = ScriptedGenerate([GARBAGE, GARBAGE])
        loop = AgentLoop(gen, make_table())

        result = loop.run("hello", {})

        assert result.status is AgentTurnStatus.CLARIFIED
        assert result.envelope is None
        assert result.result is None
        assert result.user_message is not None
        assert result.user_message.strip() != ""
        assert result.turns_used == 2


# ------------------------------------------------------------------ turn cap


class TestTurnCap:
    def test_cap_bounds_generation_no_infinite_loop(self) -> None:
        gen = ScriptedGenerate([])  # always GARBAGE
        loop = AgentLoop(gen, make_table(), max_turns_per_request=1)

        result = loop.run("hello", {})

        assert result.status is AgentTurnStatus.CLARIFIED
        assert len(gen.calls) == 1
        assert result.turns_used == 1

    def test_always_invalid_stops_within_policy(self) -> None:
        gen = ScriptedGenerate([])  # always GARBAGE
        loop = AgentLoop(gen, make_table())

        result = loop.run("hello", {})

        assert result.status is AgentTurnStatus.CLARIFIED
        # protocol policy (one retry) binds before MAX_TURNS_PER_REQUEST
        assert len(gen.calls) == 2
        assert result.turns_used <= MAX_TURNS_PER_REQUEST

    def test_invalid_cap_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_turns_per_request"):
            AgentLoop(ScriptedGenerate([]), make_table(), max_turns_per_request=0)


# ---------------------------------------------------------------- containment


class TestContainment:
    def test_code_like_output_is_never_executed(self) -> None:
        payload = "{'v': 0, 'intent': 'respond', \"params\": __import__('os').getpid()}"
        gen = ScriptedGenerate([payload, payload])
        loop = AgentLoop(gen, make_table())

        result = loop.run("hello", {})

        # the payload only ever flows through handle_raw validation -> escalated
        assert result.status is AgentTurnStatus.CLARIFIED
        assert result.envelope is None
        assert len(gen.calls) == 2

    def test_handler_exception_surfaces_as_clarified(self) -> None:
        gen = ScriptedGenerate([RESPOND_JSON])
        loop = AgentLoop(gen, make_table(boom=True))

        result = loop.run("hello", {})

        # handle_raw converts the handler crash into a rejected outcome;
        # the loop escalates instead of crashing.
        assert result.status is AgentTurnStatus.CLARIFIED
        assert result.user_message is not None
        assert "exploded" not in (result.user_message or "")

    def test_generate_failure_becomes_failed_status(self) -> None:
        def boom(prompt: str, grammar_text: str | None) -> str:
            msg = "disk on fire"
            raise RuntimeError(msg)

        loop = AgentLoop(boom, make_table())

        result = loop.run("hello", {})

        assert result.status is AgentTurnStatus.FAILED
        assert result.envelope is None
        assert result.user_message is not None
        assert result.turns_used == 0

    def test_result_is_frozen(self) -> None:
        gen = ScriptedGenerate([RESPOND_JSON])
        loop = AgentLoop(gen, make_table())
        result = loop.run("hello", {})

        with pytest.raises(dataclasses.FrozenInstanceError):
            result.status = AgentTurnStatus.FAILED  # type: ignore[misc]


# -------------------------------------------------------------------- history


class TestHistory:
    def test_ok_run_is_recorded_and_reused(self) -> None:
        gen = ScriptedGenerate([RESPOND_JSON, RESPOND_JSON])
        loop = AgentLoop(gen, make_table())

        loop.run("first question", {})
        loop.run("second question", {})

        assert len(loop.history) == 2
        assert loop.history[0].user_text == "first question"
        assert loop.history[0].envelope_json is not None
        # the second prompt contains the previous exchange for continuity
        second_prompt = gen.calls[1][0]
        assert "CONVERSATION SO FAR" in second_prompt
        assert "first question" in second_prompt
        assert '"intent": "respond"' in second_prompt

    def test_escalated_run_recorded_without_envelope(self) -> None:
        gen = ScriptedGenerate([GARBAGE, GARBAGE])
        loop = AgentLoop(gen, make_table())

        loop.run("hello", {})

        assert len(loop.history) == 1
        assert loop.history[0].envelope_json is None

    def test_prune_caps_history_oldest_dropped(self) -> None:
        gen = ScriptedGenerate([])
        loop = AgentLoop(gen, make_table())

        total = MAX_HISTORY_TURNS + 5
        for i in range(total):
            loop.add_turn(f"u{i}", RESPOND_JSON)

        assert len(loop.history) == MAX_HISTORY_TURNS
        assert loop.history[0].user_text == f"u{total - MAX_HISTORY_TURNS}"
        assert loop.history[-1].user_text == f"u{total - 1}"

    def test_history_is_read_only_view(self) -> None:
        gen = ScriptedGenerate([RESPOND_JSON])
        loop = AgentLoop(gen, make_table())
        loop.run("hello", {})

        history = loop.history
        assert isinstance(history, tuple)

"""Agent loop: orchestrates ONE user request end-to-end (TCK-P0-005).

Pipeline per user request (PROJECT.md §7.1, §8):

1. Build the prompt: system prompt (``agent/prompt.py``) + sanitized FACTS
   block (``agent/context.py``) + pruned conversation history + the user
   turn. Facts and injected text always pass the R8 sanitizer.
2. Call the model runtime once. The raw output is untrusted input whose
   ONLY consumer is :func:`localwallet.protocol.handle_raw` — it is never
   executed, never parsed by hand, never echoed into errors.
3. On ``needs_retry``: exactly ONE re-prompt with a short, value-free
   correction note appended (the retry policy constant lives in the
   protocol: :data:`localwallet.protocol.MAX_VALIDATION_RETRIES`).
4. On ``rejected`` — or a second validation failure, or exhausting the
   per-request turn cap — synthesize a clarify escalation for the user
   (plain string; no model call).
5. On ``ok``: return the validated envelope and the handler result.

The loop NEVER executes model text as code: there is no ``eval``, no
codegen, no dynamic dispatch — an intent runs only if it is a key in the
caller-supplied :class:`~localwallet.protocol.DispatchTable`.

Conversation state is a bounded in-memory list: appended per request,
pruned to :data:`MAX_HISTORY_TURNS` (oldest dropped). Summarization is a
Phase 5 concern; :meth:`AgentLoop.prune` is the hook point.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from localwallet.agent.context import render_facts, sanitize_tool_output
from localwallet.agent.prompt import build_system_prompt
from localwallet.agent.runtime import GenerateFn, ModelRuntime
from localwallet.protocol import (
    ClarifyParams,
    DispatchTable,
    Envelope,
    Outcome,
    OutcomeStatus,
    handle_raw,
)

__all__ = [
    "MAX_HISTORY_TURNS",
    "MAX_TURNS_PER_REQUEST",
    "AgentLoop",
    "AgentTurnResult",
    "AgentTurnStatus",
    "ConversationTurn",
]

#: Hard cap on model turns (generate calls) for ONE user request
#: (PROJECT.md §7.1: "hard cap ~6 turns per user request"). The protocol's
#: one-retry policy usually binds first; this cap is the outer guard that
#: guarantees termination regardless of protocol settings.
MAX_TURNS_PER_REQUEST: Final[int] = 6

#: Conversation history cap. Oldest turns are dropped; summarization
#: replaces this in Phase 5 (PROJECT.md §7.1 / R13).
MAX_HISTORY_TURNS: Final[int] = 20

#: User-facing escalation when the model could not produce a valid
#: envelope after the allowed retry. Plain string; no model call, and no
#: echo of the rejected output (untrusted, possibly huge).
_ESCALATION_MESSAGE: Final[str] = (
    "Sorry — I couldn't produce a reliable answer for that request. "
    "Could you rephrase it or try again?"
)

#: User-facing message for infrastructure failures (e.g. the model runtime
#: raised). Generic by design: exception text may contain paths or
#: internals and is never surfaced verbatim.
_FAILURE_MESSAGE: Final[str] = (
    "Something went wrong while processing that request. Please try again."
)

#: Placeholder for a history turn that produced no envelope (escalation).
_NO_ENVELOPE_PLACEHOLDER: Final[str] = "(no envelope: the user was asked to rephrase)"


class AgentTurnStatus(StrEnum):
    """Terminal status of one user request handled by :class:`AgentLoop`."""

    OK = "ok"
    CLARIFIED = "clarified"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """One recorded exchange in the in-memory conversation history.

    Attributes:
        user_text: The user's message, as received.
        envelope_json: The canonical JSON of the validated envelope that
            answered it, or ``None`` when the request was escalated
            without an envelope.
    """

    user_text: str
    envelope_json: str | None


@dataclass(frozen=True, slots=True)
class AgentTurnResult:
    """Outcome of one user request.

    Attributes:
        status: ``ok`` (envelope dispatched), ``clarified`` (escalated to
            the user with a plain message — model rejection, retry budget
            exhausted, or turn cap hit), or ``failed`` (infrastructure
            error, e.g. the runtime raised).
        envelope: The validated envelope, for ``ok`` and for model-emitted
            ``clarify``; ``None`` on escalation/failure.
        result: The handler's return dict, only for ``ok``.
        user_message: Text to show the user: the synthesized escalation
            on ``clarified``, the model's question when it emitted
            ``clarify``, the generic failure note on ``failed``; ``None``
            otherwise.
        turns_used: Number of model generate calls consumed.
    """

    status: AgentTurnStatus
    envelope: Envelope | None
    result: dict[str, object] | None
    user_message: str | None
    turns_used: int


class AgentLoop:
    """Runs one user request through generate → validate → dispatch.

    Args:
        generate: A :class:`~localwallet.agent.runtime.ModelRuntime`, or
            a bare ``generate_fn(prompt, grammar_text) -> str`` callable
            (wrapped in a runtime; the test seam).
        table: The allowlist dispatch table (intent → handler). Handlers
            receive the validated :class:`~localwallet.protocol.Envelope`.
        max_turns_per_request: Hard cap on generate calls per request;
            see :data:`MAX_TURNS_PER_REQUEST`.

    Raises:
        ValueError: ``max_turns_per_request`` is less than 1.
    """

    def __init__(
        self,
        generate: ModelRuntime | GenerateFn,
        table: DispatchTable,
        *,
        max_turns_per_request: int = MAX_TURNS_PER_REQUEST,
    ) -> None:
        if max_turns_per_request < 1:
            raise ValueError("max_turns_per_request must be at least 1")
        self._runtime = generate if isinstance(generate, ModelRuntime) else ModelRuntime(generate_fn=generate)
        self._table = table
        self._max_turns = max_turns_per_request
        self._history: list[ConversationTurn] = []

    # ------------------------------------------------------------ history

    @property
    def history(self) -> tuple[ConversationTurn, ...]:
        """The conversation history, oldest first (read-only view)."""
        return tuple(self._history)

    def add_turn(self, user_text: str, envelope_json: str | None) -> None:
        """Append a turn to the history, then prune to the cap.

        Public hook for callers that want to seed or extend the
        conversation outside :meth:`run`.
        """
        self._history.append(ConversationTurn(user_text=user_text, envelope_json=envelope_json))
        self.prune()

    def prune(self) -> None:
        """Cap the history at :data:`MAX_HISTORY_TURNS`, dropping oldest.

        Hook point for Phase 5: summarization can replace the drop-oldest
        behavior without touching the loop's prompt assembly.
        """
        excess = len(self._history) - MAX_HISTORY_TURNS
        if excess > 0:
            del self._history[:excess]

    # -------------------------------------------------------------- run

    def run(self, user_text: str, facts: Mapping[str, object]) -> AgentTurnResult:
        """Handle one user request and return its terminal outcome.

        Retry policy: the first validation failure triggers exactly one
        re-prompt whose prompt carries a short, value-free correction
        note; a second failure (or a ``rejected`` outcome, or reaching
        the turn cap) escalates to a synthesized clarify message for the
        user — no further model calls. Model-emitted ``clarify`` intents
        are ordinary ``ok`` outcomes dispatched through the table.

        The request and its outcome are recorded in the conversation
        history (pruned to :data:`MAX_HISTORY_TURNS`).

        Args:
            user_text: The raw user message.
            facts: Fresh facts to inject as a sanitized FACTS block
                (balances, addresses, fee table, ...). Keys are
                code-controlled; values are sanitized per value.

        Returns:
            An :class:`AgentTurnResult`; never raises for model/validation
            problems (infrastructure exceptions become ``failed``).
        """
        base_prompt = self._build_prompt(user_text, facts)
        prompt = base_prompt
        failures = 0
        turns_used = 0
        try:
            while turns_used < self._max_turns:
                raw = self._runtime.generate(prompt)
                turns_used += 1
                outcome = handle_raw(raw, self._table, validation_failure_count=failures)
                if outcome.status is OutcomeStatus.OK:
                    return self._finish_ok(outcome, user_text, turns_used)
                if outcome.status is OutcomeStatus.NEEDS_RETRY:
                    failures += 1
                    prompt = self._with_retry_note(base_prompt, outcome.failures)
                    continue
                break  # rejected: retry budget exhausted or dispatch refused
        except Exception:  # noqa: BLE001 — containment: an infrastructure error must degrade to a user-facing failure, never crash the chat loop
            return AgentTurnResult(
                status=AgentTurnStatus.FAILED,
                envelope=None,
                result=None,
                user_message=_FAILURE_MESSAGE,
                turns_used=turns_used,
            )
        return self._finish_escalated(user_text, turns_used)

    # --------------------------------------------------------- internals

    def _build_prompt(self, user_text: str, facts: Mapping[str, object]) -> str:
        """Assemble system prompt + FACTS block + history + user turn."""
        parts: list[str] = [build_system_prompt()]
        facts_block = render_facts(facts)
        if facts_block:
            parts.append(facts_block)
        if self._history:
            lines = ["CONVERSATION SO FAR (oldest first):"]
            for turn in self._history:
                lines.append(f"user: {sanitize_tool_output(turn.user_text)}")
                assistant = (
                    turn.envelope_json
                    if turn.envelope_json is not None
                    else _NO_ENVELOPE_PLACEHOLDER
                )
                lines.append(f"envelope: {assistant}")
            parts.append("\n".join(lines))
        parts.append(f"user: {sanitize_tool_output(user_text)}")
        parts.append("envelope:")
        return "\n\n".join(parts)

    @staticmethod
    def _with_retry_note(base_prompt: str, failures: tuple[str, ...]) -> str:
        """Append the one-shot correction note for the re-prompt.

        Failure strings come from the protocol layer and are value-free
        by design; they are sanitized again here before injection (R8).
        """
        detail = sanitize_tool_output("; ".join(failures) or "envelope validation failed")
        return (
            f"{base_prompt}\n\n"
            "RETRY NOTE: your previous output was not a valid envelope.\n"
            f"Problem reported by the validator: {detail}\n"
            "Emit exactly one corrected envelope JSON object now — keys in "
            "the order v, intent, params.\n"
            "envelope:"
        )

    def _finish_ok(
        self, outcome: Outcome, user_text: str, turns_used: int
    ) -> AgentTurnResult:
        """Record the exchange and package a successful outcome."""
        envelope = outcome.envelope
        assert envelope is not None  # guaranteed by OutcomeStatus.OK
        user_message: str | None = None
        if isinstance(envelope.params, ClarifyParams):
            # Model-emitted clarify: surface its question directly.
            user_message = envelope.params.question
        self.add_turn(user_text, envelope.model_dump_json())
        return AgentTurnResult(
            status=AgentTurnStatus.OK,
            envelope=envelope,
            result=outcome.result,
            user_message=user_message,
            turns_used=turns_used,
        )

    def _finish_escalated(self, user_text: str, turns_used: int) -> AgentTurnResult:
        """Record the exchange and escalate to the user (no model call)."""
        self.add_turn(user_text, None)
        return AgentTurnResult(
            status=AgentTurnStatus.CLARIFIED,
            envelope=None,
            result=None,
            user_message=_ESCALATION_MESSAGE,
            turns_used=turns_used,
        )

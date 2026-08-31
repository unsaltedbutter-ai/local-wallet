"""Allowlist dispatcher and raw-payload orchestration.

The dispatcher is a plain dict-based registry (intent → handler). There is
no ``eval``, no codegen from model output, and no ``getattr``-by-string —
the only way a handler runs is an explicit entry in a
:class:`DispatchTable` whose key is a member of the closed
:class:`~localwallet.protocol.intents.IntentName` enum.

:func:`handle_raw` orchestrates the full pipeline for one raw model
payload: parse → schema-validate (layer 2) → business rules (layer 3) →
dispatch, and exposes the retry policy as data: validation failures yield
an :class:`Outcome` with status ``needs_retry`` while retry budget remains
(:data:`MAX_VALIDATION_RETRIES` == 1) and ``rejected`` once exhausted, at
which point the agent loop escalates to a ``clarify`` intent for the user.
This module never re-prompts the model itself — it only decides whether a
re-prompt is allowed.

Handler exceptions are never silently swallowed: :func:`dispatch` lets them
propagate to its caller, and :func:`handle_raw` converts them into a
``dispatch_error`` outcome.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from localwallet.protocol.envelope import Envelope, validate_payload
from localwallet.protocol.errors import (
    DispatchError,
    EnvelopeValidationError,
    ErrorCode,
    ErrorEnvelope,
)
from localwallet.protocol.intents import BUSINESS_RULES, IntentName

__all__ = [
    "MAX_VALIDATION_RETRIES",
    "DispatchTable",
    "Handler",
    "Outcome",
    "OutcomeStatus",
    "dispatch",
    "handle_raw",
]

#: Exactly one model re-prompt is allowed on validation failure before the
#: agent loop must escalate to ``clarify`` (PROJECT.md §8 invariant 3).
MAX_VALIDATION_RETRIES: Final[int] = 1

#: A handler receives the fully validated, typed envelope and returns a
#: plain result dict (injected into agent context by the caller).
type Handler = Callable[[Envelope], dict[str, object]]

#: Plain dict-based registry intent → handler. Keys may be
#: :class:`IntentName` members or their string values (StrEnum equality).
type DispatchTable = dict[IntentName, Handler]


class OutcomeStatus(StrEnum):
    """Terminal status of :func:`handle_raw` for one payload."""

    OK = "ok"
    REJECTED = "rejected"
    NEEDS_RETRY = "needs_retry"


@dataclass(frozen=True, slots=True)
class Outcome:
    """Result of running one raw payload through the full pipeline.

    Attributes:
        status: Terminal decision (see :class:`OutcomeStatus`).
        envelope: The validated envelope, when one was produced (``None``
            for parse/schema failures).
        result: The handler's return value, only for ``status=ok``.
        error: Structured error envelope for the UI/agent, for every
            non-ok status.
        failures: Value-free validation failure strings, for validation
            and business-rule failures.
    """

    status: OutcomeStatus
    envelope: Envelope | None = None
    result: dict[str, object] | None = None
    error: ErrorEnvelope | None = None
    failures: tuple[str, ...] = ()


def dispatch(envelope: Envelope, table: DispatchTable) -> dict[str, object]:
    """Route a validated envelope to its registered handler (allowlist).

    ``handle_raw`` is the mandatory entry point for raw model payloads: it
    is the only path that runs the layer-3 business rules. ``dispatch`` is
    for internally-validated envelopes only — calling it directly on model
    output skips layer 3 and is not supported.

    Defense in depth: even though the schema layer rejects unknown intents
    before an :class:`Envelope` can exist, the lookup here re-checks the
    intent against the closed registry and the table.

    Raises:
        DispatchError: the intent is outside the closed registry, or no
            handler is registered for it in ``table``.
    Any exception raised by the handler propagates to the caller of
    :func:`dispatch` — never silently swallowed.
    """
    try:
        key = IntentName(envelope.intent)
    except (ValueError, TypeError):
        # TypeError covers unhashable bypass values (e.g. from a
        # ``model_construct`` envelope that skipped validation). Do not
        # echo the raw value: it never passed schema validation and could
        # be arbitrary untrusted content.
        raise DispatchError("unknown intent: not a member of the closed intent registry") from None

    handler = table.get(key)
    if handler is None:
        raise DispatchError(f"no handler registered for intent {key.value!r}")

    return handler(envelope)


def _business_rule_failures(envelope: Envelope) -> list[str]:
    """Run the layer-3 business rule for the envelope's intent."""
    rule = BUSINESS_RULES.get(envelope.intent)
    if rule is None:  # pragma: no cover — registry covers the enum; fail closed anyway
        return [f"no business rule registered for intent {envelope.intent.value!r}"]
    return rule(envelope.params)


def _retry_decision(
    failures: tuple[str, ...], validation_failure_count: int
) -> tuple[OutcomeStatus, ErrorEnvelope]:
    """Apply the retry policy to a validation failure.

    Returns the status and the error envelope to surface: ``needs_retry``
    while ``validation_failure_count < MAX_VALIDATION_RETRIES``, else
    ``rejected`` (the agent loop escalates to ``clarify``).
    """
    error = ErrorEnvelope(
        v=0,
        error={"code": ErrorCode.INVALID_ENVELOPE, "detail": "; ".join(failures)},
    )
    if validation_failure_count < MAX_VALIDATION_RETRIES:
        return OutcomeStatus.NEEDS_RETRY, error
    return OutcomeStatus.REJECTED, error


def handle_raw(
    raw: str | bytes | Mapping[str, object],
    table: DispatchTable,
    *,
    validation_failure_count: int = 0,
) -> Outcome:
    """Run one raw model payload through parse → validate → rules → dispatch.

    Handler crash containment contract: handler exception messages are
    trusted internal code text — handlers must not embed raw model/user
    content in exception strings (the chain layer already scrubs
    addresses/txids/amounts).

    Args:
        raw: The payload — a JSON object (``Mapping``) or a JSON document
            as ``str``/``bytes``.
        table: The allowlist dispatch table (intent → handler).
        validation_failure_count: How many validation failures have already
            happened for this user request (i.e. re-prompts already spent).
            The policy allows exactly :data:`MAX_VALIDATION_RETRIES`
            retries: with budget remaining, a validation failure yields
            ``needs_retry`` (the agent loop re-prompts); once exhausted, it
            yields ``rejected`` (the agent loop escalates to ``clarify``).

    Returns:
        An :class:`Outcome`; never raises for payload/handler failures —
        every failure is surfaced as a structured error envelope.
    """
    try:
        envelope = validate_payload(raw)
    except EnvelopeValidationError as exc:
        status, error = _retry_decision(exc.failures, validation_failure_count)
        return Outcome(
            status=status,
            error=error,
            failures=exc.failures,
        )

    rule_failures = _business_rule_failures(envelope)
    if rule_failures:
        status, error = _retry_decision(tuple(rule_failures), validation_failure_count)
        return Outcome(status=status, envelope=envelope, error=error, failures=tuple(rule_failures))

    try:
        result = dispatch(envelope, table)
    except DispatchError as exc:
        return Outcome(
            status=OutcomeStatus.REJECTED,
            envelope=envelope,
            error=ErrorEnvelope(
                v=0,
                error={"code": ErrorCode.DISPATCH_ERROR, "detail": str(exc)},
            ),
        )
    except Exception as exc:  # noqa: BLE001 — containment is the point: any handler crash must surface as a structured error, never crash the chat loop
        return Outcome(
            status=OutcomeStatus.REJECTED,
            envelope=envelope,
            error=ErrorEnvelope(
                v=0,
                error={
                    "code": ErrorCode.DISPATCH_ERROR,
                    "detail": f"handler failed: {type(exc).__name__}: {exc}",
                },
            ),
        )

    return Outcome(status=OutcomeStatus.OK, envelope=envelope, result=result)

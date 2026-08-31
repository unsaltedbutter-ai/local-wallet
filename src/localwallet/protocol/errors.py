"""Error types for the protocol subsystem.

Three public names:

- :class:`EnvelopeValidationError` — a payload failed JSON/schema (layer-2)
  validation. Carries structured, value-free failure strings.
- :class:`DispatchError` — the dispatcher could not route an envelope
  (unknown intent, or no handler registered for a known intent).
- :class:`ErrorEnvelope` — the pydantic model for the system→UI error
  envelope. This is the ONLY error channel towards the user and is **never**
  model-emitted (the model only ever produces :class:`~localwallet.protocol.envelope.Envelope`).

The ``chain_error`` code is defined here as an enum member for protocol
completeness; it is produced by chain-facing code paths, never by this
package — this module imports nothing from ``localwallet.chain``.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "DispatchError",
    "EnvelopeValidationError",
    "ErrorBody",
    "ErrorCode",
    "ErrorEnvelope",
]


class ErrorCode(StrEnum):
    """Closed enum of error codes carried by a system→UI error envelope."""

    INVALID_ENVELOPE = "invalid_envelope"
    DISPATCH_ERROR = "dispatch_error"
    CHAIN_ERROR = "chain_error"


class EnvelopeValidationError(Exception):
    """A payload failed JSON parsing or schema (layer-2) validation.

    ``failures`` holds one human-readable string per problem. Failure
    strings describe *what* failed (field path + reason) and never echo the
    raw payload content back — model output is untrusted input and may be
    arbitrarily large or contain sensitive material.

    Args:
        failures: One or more value-free failure descriptions. An empty
            sequence is normalized to a single generic failure.
    """

    def __init__(self, failures: Sequence[str]) -> None:
        normalized = tuple(failures) or ("envelope validation failed",)
        self.failures: tuple[str, ...] = normalized
        super().__init__("; ".join(normalized))

    def __str__(self) -> str:
        return "; ".join(self.failures)


class DispatchError(Exception):
    """The dispatcher could not route an envelope to a handler.

    Raised only as defense in depth — by the time an :class:`Envelope`
    exists, the schema layer has already rejected unknown intents. This
    error covers: an intent outside the closed registry reaching the
    dispatcher, or a known intent with no handler in the dispatch table.

    Error messages contain intent names only — never payload content.
    """


class ErrorBody(BaseModel):
    """Body of a system→UI error envelope (see :class:`ErrorEnvelope`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ErrorCode
    detail: str = Field(min_length=1)

    @field_validator("detail", mode="after")
    @classmethod
    def _detail_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("detail must contain non-whitespace characters")
        return value


class ErrorEnvelope(BaseModel):
    """System→UI error envelope (canonical contract v0).

    Shape (exactly, no extra keys anywhere)::

        {"v": 0, "error": {"code": <ErrorCode>, "detail": <string>}}

    This envelope is produced by the system to report failures to the UI.
    It is **never** model-emitted: the model's only output is the intent
    envelope; anything else it emits is rejected as an invalid envelope.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    v: Literal[0]
    error: ErrorBody

    @field_validator("v", mode="before")
    @classmethod
    def _v_must_be_zero_int(cls, value: object) -> object:
        """Require a true integer 0; pydantic validators must raise ValueError."""
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("v must be the integer 0")  # noqa: TRY004 — see docstring
        return value

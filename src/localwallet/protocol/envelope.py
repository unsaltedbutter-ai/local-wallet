"""Envelope schema (validation layer 2) for the closed intent protocol.

Canonical envelope contract v0 — the model-emitted wire format::

    {"v": 0, "intent": "respond"|"clarify"|"get_balance", "params": {...}}

- ``v``: integer, exactly ``0`` (booleans are not integers for this purpose).
- ``intent``: closed enum — see :class:`IntentName`.
- ``params``: REQUIRED object, shape fixed per intent:
  ``respond`` → ``{"text": str, 1..4000 chars}``;
  ``clarify`` → ``{"question": str, 1..1000 chars}``;
  ``get_balance`` → ``{}`` (reserved for future opts).

No extra top-level keys; no extra params keys (closed world); unknown
intent or wrong version ⇒ invalid envelope. The intent↔params pairing is
cross-checked here so a mismatched combination (e.g. ``intent="respond"``
with ``params={}``) cannot pass this layer even though the GBNF grammar
(``agent/grammar/envelope.gbnf``) already makes it syntactically
impossible at decode time.

This module is the single source of truth for the closed world: the
:class:`IntentName` enum, the per-intent params models, and
:data:`INTENT_REGISTRY` (intent name → params model). Layer-3 business
rules live in :mod:`localwallet.protocol.intents`, which re-exports the
closed-world names; the dispatcher is
:mod:`localwallet.protocol.dispatcher`. Dependencies point one way
(``dispatcher → intents → envelope → errors``), so every module imports
in any order.

The system→UI *error* envelope lives in
:mod:`localwallet.protocol.errors` and is never model-emitted.

Value-free guarantee: failure strings for invalid payloads never echo
payload content. Offending *values* are excluded at the source (pydantic
``include_input=False``), and extra-key *names* — which are
model-controlled content — are rendered as the literal ``<key>`` unless
they are known schema field names; the ``"; "-joined`` failure text is
additionally capped at :data:`_MAX_FAILURE_CHARS` characters with a
trailing ``…`` marker when truncated.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from localwallet.protocol.errors import EnvelopeValidationError

__all__ = [
    "INTENT_REGISTRY",
    "MAX_QUESTION_CHARS",
    "MAX_TEXT_CHARS",
    "BaseParams",
    "ClarifyParams",
    "Envelope",
    "GetBalanceParams",
    "IntentName",
    "RespondParams",
    "validate_payload",
]

#: Maximum accepted length of ``respond`` params ``text`` (characters).
MAX_TEXT_CHARS: Final[int] = 4000

#: Maximum accepted length of ``clarify`` params ``question`` (characters).
MAX_QUESTION_CHARS: Final[int] = 1000

#: Schema field names that may appear verbatim in failure locations. Any
#: other string component of a pydantic ``loc`` is model-controlled content
#: (an extra-key name chosen by the untrusted payload) and is rendered as
#: the literal ``<key>`` instead.
_KNOWN_LOC_FIELDS: Final[frozenset[str]] = frozenset(
    {"v", "intent", "params", "text", "question", "error", "detail", "code"}
)

#: Maximum total length (characters) of the ``"; "-joined`` failure text
#: produced by :func:`_format_pydantic_errors`. A hard upper bound on how
#: much failure text can reach ``ErrorEnvelope.detail`` / logs.
_MAX_FAILURE_CHARS: Final[int] = 500


class IntentName(StrEnum):
    """Closed enum of intent names the model may emit (contract v0).

    Members are plain strings, so registry/dispatch lookups accept either
    the enum member or its string value interchangeably.
    """

    RESPOND = "respond"
    CLARIFY = "clarify"
    GET_BALANCE = "get_balance"


class BaseParams(BaseModel):
    """Common base for per-intent params: closed world, immutable.

    ``extra="forbid"`` rejects unknown params keys; ``frozen=True`` makes a
    validated envelope safe to hold and dispatch without defensive copies.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class RespondParams(BaseParams):
    """Params for ``respond``: a chat answer of 1..4000 characters."""

    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)


class ClarifyParams(BaseParams):
    """Params for ``clarify``: a question to the user, 1..1000 characters."""

    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)


class GetBalanceParams(BaseParams):
    """Params for ``get_balance``: empty object, reserved for future opts.

    The model must emit ``"params": {}`` exactly; any key here is rejected
    (closed world).
    """


#: Frozen mapping intent name → params model — THE closed world. Intents
#: outside this registry do not exist: the schema layer rejects them and
#: the dispatcher refuses them (defense in depth).
#:
#: Keys are :class:`IntentName`; because it is a StrEnum, plain-string
#: lookups (``INTENT_REGISTRY["respond"]``) resolve to the same entry.
#: Adding an intent means: a new :class:`IntentName` member + params model
#: here, an entry in this registry, a business rule in
#: ``intents.py``, a grammar branch in ``agent/grammar/envelope.gbnf``,
#: a handler registration, and eval fixtures.
INTENT_REGISTRY: Mapping[IntentName, type[BaseParams]] = MappingProxyType(
    {
        IntentName.RESPOND: RespondParams,
        IntentName.CLARIFY: ClarifyParams,
        IntentName.GET_BALANCE: GetBalanceParams,
    }
)


class Envelope(BaseModel):
    """Closed intent envelope (model-emitted, contract v0).

    Exactly three top-level keys (``v``, ``intent``, ``params``), no extras;
    ``params`` must be the params model registered for ``intent``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    v: Literal[0]
    intent: IntentName
    params: RespondParams | ClarifyParams | GetBalanceParams

    @field_validator("v", mode="before")
    @classmethod
    def _v_must_be_zero_int(cls, value: object) -> object:
        """Require a true integer (JSON booleans/floats/strings rejected).

        Raises ``ValueError`` (not ``TypeError``) because pydantic
        ``mode="before"`` validators must raise ``ValueError``/
        ``AssertionError`` for the failure to surface as a field error.
        """
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("v must be the integer 0")  # noqa: TRY004 — see docstring
        return value

    @model_validator(mode="after")
    def _params_model_matches_intent(self) -> Envelope:
        """Cross-check the params type against the intent registry."""
        expected = INTENT_REGISTRY.get(self.intent)
        if expected is None:  # pragma: no cover — registry covers the enum
            raise ValueError(f"intent {self.intent.value!r} has no registered params model")
        if type(self.params) is not expected:
            want = expected.__name__
            got = type(self.params).__name__
            raise ValueError(
                f"params shape {got!r} does not match intent {self.intent.value!r} "
                f"(expected {want})"
            )
        return self


def validate_payload(raw: str | bytes | Mapping[str, object]) -> Envelope:
    """Parse and schema-validate a raw model payload into an :class:`Envelope`.

    Accepts an already-parsed JSON object (any ``Mapping``), or a JSON
    document as ``str``/``bytes``. Layers covered here: JSON parsing and
    the pydantic schema (layer 2) — business rules (layer 3) run separately
    in :func:`localwallet.protocol.dispatcher.handle_raw`.

    Raises:
        EnvelopeValidationError: on any failure, with structured, value-free
            failure strings (raw payload content is never echoed).
    """
    if isinstance(raw, Mapping):
        data: object = raw
    elif isinstance(raw, (str, bytes, bytearray)):
        try:
            data = json.loads(raw)
        except UnicodeDecodeError:
            raise EnvelopeValidationError(["payload is not valid UTF-8 JSON"]) from None
        except json.JSONDecodeError as exc:
            raise EnvelopeValidationError([f"payload is not valid JSON: {exc.msg}"]) from None
    else:
        raise EnvelopeValidationError(
            ["payload must be a JSON object, or a str/bytes JSON document"]
        )

    if not isinstance(data, Mapping):
        raise EnvelopeValidationError(["payload must be a JSON object"])

    try:
        return Envelope.model_validate(data)
    except ValidationError as exc:
        raise EnvelopeValidationError(_format_pydantic_errors(exc)) from exc


def _render_loc(loc: tuple[object, ...]) -> str:
    """Render a pydantic error location value-free.

    String components are kept only when they are known schema field names
    (:data:`_KNOWN_LOC_FIELDS`); anything else — typically an extra-key
    name chosen by the untrusted payload, possibly huge or full of control
    characters — is replaced by the literal ``<key>``. Integer indices are
    kept as-is. Yields ``<root>`` for an empty location.
    """
    parts: list[str] = []
    for part in loc:
        if isinstance(part, int) and not isinstance(part, bool):
            parts.append(str(part))
        elif isinstance(part, str) and part in _KNOWN_LOC_FIELDS:
            parts.append(part)
        else:
            parts.append("<key>")
    return ".".join(parts) or "<root>"


def _cap_failures(failures: list[str]) -> list[str]:
    """Cap the total ``"; "-joined`` failure length at ``_MAX_FAILURE_CHARS``.

    Returns the input unchanged while it fits; once it would exceed the
    bound, collapses to a single truncated failure terminated by ``…``.
    Truncation is safe: failure strings are value-free by construction, so
    no payload content can be re-introduced by cutting.
    """
    joined = "; ".join(failures)
    if len(joined) <= _MAX_FAILURE_CHARS:
        return failures
    return [joined[: _MAX_FAILURE_CHARS - 1].rstrip() + "…"]


def _format_pydantic_errors(exc: ValidationError) -> list[str]:
    """Flatten a pydantic error into value-free ``loc: message`` strings.

    ``include_input=False`` guarantees the offending payload values are
    never copied into the failure strings (model output is untrusted and
    may be huge; it must never be echoed into errors/logs). Locations are
    rendered value-free too: extra-key names become the literal ``<key>``
    (see :func:`_render_loc`), and the total ``"; "-joined`` failure text
    is capped at :data:`_MAX_FAILURE_CHARS` characters with a trailing
    ``…`` marker when truncated.
    """
    rendered = [
        f"{_render_loc(err.get('loc', ()))}: {err['msg']}"
        for err in exc.errors(include_url=False, include_context=False, include_input=False)
    ]
    return _cap_failures(rendered)

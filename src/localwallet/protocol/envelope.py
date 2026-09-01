"""Envelope schema (validation layer 2) for the closed intent protocol.

Canonical envelope contract v0 — the model-emitted wire format::

    {"v": 0, "intent": <closed enum>, "params": {...}}

- ``v``: integer, exactly ``0`` (booleans are not integers for this purpose).
- ``intent``: closed enum — see :class:`IntentName` (six members as of the
  Phase 1 v0 extension; see ``docs/adr/0002-envelope-spec.md``).
- ``params``: REQUIRED object, shape fixed per intent:
  ``respond`` → ``{"text": str, 1..4000 chars}``;
  ``clarify`` → ``{"question": str, 1..1000 chars}``;
  ``get_balance`` → ``{}`` (reserved for future opts);
  ``get_history`` → ``{}`` or ``{"limit": int, 1..100}`` (omitted ⇒ the
  handler applies its default of 20);
  ``get_utxos`` → ``{}`` (reserved for future opts);
  ``new_address`` → ``{}`` or ``{"branch": 0|1}`` (0 = receive chain,
  the default; 1 = change chain, rarely user-requested but allowed).

Adding enum members and optional params keys is a backward-compatible v0
extension: previously-valid envelopes remain valid, so ``v`` stays ``0``
(ADR-0002 bump policy). The grammar (``agent/grammar/envelope.gbnf``), this
schema, and the system prompt (``agent/prompt.py``) MUST move together.

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
    model_serializer,
    model_validator,
)
from pydantic.functional_serializers import SerializerFunctionWrapHandler

from localwallet.protocol.errors import EnvelopeValidationError

__all__ = [
    "INTENT_REGISTRY",
    "MAX_QUESTION_CHARS",
    "MAX_TEXT_CHARS",
    "BaseParams",
    "ClarifyParams",
    "Envelope",
    "GetBalanceParams",
    "GetHistoryParams",
    "GetUtxosParams",
    "IntentName",
    "NewAddressParams",
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
    {"v", "intent", "params", "text", "question", "limit", "branch", "error", "detail", "code"}
)

#: Maximum total length (characters) of the ``"; "-joined`` failure text
#: produced by :func:`_format_pydantic_errors`. A hard upper bound on how
#: much failure text can reach ``ErrorEnvelope.detail`` / logs.
_MAX_FAILURE_CHARS: Final[int] = 500


class IntentName(StrEnum):
    """Closed enum of intent names the model may emit (contract v0).

    Members are plain strings, so registry/dispatch lookups accept either
    the enum member or its string value interchangeably.

    Phase 1 v0 extension (backward-compatible — see
    ``docs/adr/0002-envelope-spec.md``): ``get_history``, ``get_utxos``,
    ``new_address`` joined the original three members.
    """

    RESPOND = "respond"
    CLARIFY = "clarify"
    GET_BALANCE = "get_balance"
    GET_HISTORY = "get_history"
    GET_UTXOS = "get_utxos"
    NEW_ADDRESS = "new_address"


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


class _OmitNoneDump(BaseParams):
    """Shared dump behavior for params models with optional keys.

    ``None``-valued optional keys are dropped from ``model_dump`` /
    ``model_dump_json`` so a validated envelope round-trips to EXACTLY the
    wire shape the GBNF grammar accepts (e.g. ``get_history`` with no
    ``limit`` serializes as ``"params": {}``, never ``{"limit": null}``).
    Validation is untouched: absent keys simply take their ``None`` default.
    """

    @model_serializer(mode="wrap")
    def _serialize_omitting_none(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        return {k: v for k, v in handler(self).items() if v is not None}


class GetHistoryParams(_OmitNoneDump):
    """Params for ``get_history``: optional result cap.

    ``limit``: optional integer, business range 1..100 (schema-enforced;
    the grammar's syntactic bound is looser — 1..999, no leading zeros —
    and this layer is the authority). When omitted the handler applies its
    own default of 20; omission is the normal case, so ``"params": {}`` is
    a fully valid body.
    """

    limit: int | None = Field(default=None, ge=1, le=100)

    @field_validator("limit", mode="before")
    @classmethod
    def _limit_must_be_true_int(cls, value: object) -> object:
        """Close pydantic's lax coercions for ``limit`` (untrusted input).

        Lax mode would accept ``"20"`` (string) and ``True`` (bool) as
        integers; the contract admits only true JSON integers, and an
        explicit ``null`` is rejected too (the grammar admits only ``{}``
        or ``{"limit": <int>}`` — ``null`` is neither omitted nor an int;
        omission is expressed by leaving the key out entirely). Raises
        ``ValueError`` because pydantic ``mode="before"`` validators must
        raise ``ValueError``/``AssertionError`` for the failure to surface
        as a field error.
        """
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        raise ValueError("limit must be an integer when present")


class GetUtxosParams(BaseParams):
    """Params for ``get_utxos``: empty object, reserved for future opts.

    Same shape as :class:`GetBalanceParams`: the model must emit
    ``"params": {}`` exactly; any key here is rejected (closed world).
    """


class NewAddressParams(_OmitNoneDump):
    """Params for ``new_address``: optional derivation branch.

    ``branch``: optional integer, ``0`` (receive chain — the default the
    handler applies when omitted) or ``1`` (change chain; rarely
    user-requested, but allowed and documented). Any other value is
    rejected.
    """

    branch: int | None = Field(default=None, ge=0, le=1)

    @field_validator("branch", mode="before")
    @classmethod
    def _branch_must_be_true_int(cls, value: object) -> object:
        """Close pydantic's lax coercions for ``branch`` (see ``limit``)."""
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        raise ValueError("branch must be an integer when present")


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
        IntentName.GET_HISTORY: GetHistoryParams,
        IntentName.GET_UTXOS: GetUtxosParams,
        IntentName.NEW_ADDRESS: NewAddressParams,
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
    params: (
        RespondParams
        | ClarifyParams
        | GetBalanceParams
        | GetHistoryParams
        | GetUtxosParams
        | NewAddressParams
    )

    @model_validator(mode="before")
    @classmethod
    def _bind_params_to_intent(cls, data: object) -> object:
        """Resolve the intent→params pairing BEFORE union coercion.

        pydantic's smart union cannot disambiguate an empty ``params``
        object across the empty-params intents (``get_balance``,
        ``get_utxos``, ``get_history``, ``new_address`` without keys): it
        would bind ``{}`` to whichever matching model comes first, and the
        pairing cross-check below would then reject a perfectly valid
        envelope. Instead, this validator looks up the registry entry for
        the declared intent and validates ``params`` against exactly that
        model, injecting the instance so the union accepts it as-is.

        Malformed params surface as a single value-free failure: the inner
        pydantic error is re-rendered with ``include_input=False`` and
        value-free locations (see :func:`_render_loc`), so no untrusted
        payload content can leak into the failure string. Field-level
        errors elsewhere in the payload (e.g. a bad ``v``) are reported
        when params bind successfully; when params fail, the params error
        is the reported failure — diagnostics are best-effort, the
        value-free and structured guarantees are not.
        """
        if not isinstance(data, Mapping):
            return data
        intent = data.get("intent")
        try:
            expected = INTENT_REGISTRY.get(IntentName(intent))  # type: ignore[arg-type]
        except (ValueError, TypeError):
            return data  # unknown/malformed intent: field validation reports it
        raw_params = data.get("params")
        if expected is None or not isinstance(raw_params, Mapping):
            return data
        bound = expected.model_validate(raw_params)
        merged = dict(data)
        merged["params"] = bound
        return merged

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

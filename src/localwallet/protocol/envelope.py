"""Envelope schema (validation layer 2) for the closed intent protocol.

Canonical envelope contract v0 — the model-emitted wire format::

    {"v": 0, "intent": <closed enum>, "params": {...}}

- ``v``: integer, exactly ``0`` (booleans are not integers for this purpose).
- ``intent``: closed enum — see :class:`IntentName` (twelve members as of
  the Phase 4 v0 extension; see ``docs/adr/0002-envelope-spec.md``,
  ``docs/adr/0013-confirm-gate.md``).
- ``params``: REQUIRED object, shape fixed per intent:
  ``respond`` → ``{"text": str, 1..4000 chars}``;
  ``clarify`` → ``{"question": str, 1..1000 chars}``;
  ``get_balance`` → ``{}`` (reserved for future opts);
  ``get_history`` → ``{}`` or ``{"limit": int, 1..100}`` (omitted ⇒ the
  handler applies its default of 20);
  ``get_utxos`` → ``{}`` (reserved for future opts);
  ``new_address`` → ``{}`` or ``{"branch": 0|1}`` (0 = receive chain,
  the default; 1 = change chain, rarely user-requested but allowed);
  ``create_tx`` → ``{"recipient": str, 14..100 chars}`` plus EXACTLY ONE of
  ``{"amount_sats": int, 546..21e15}`` | ``{"amount_usd": number,
  0.01..1_000_000}``, plus optional ``{"fee_target": "fast"|"medium"|"slow"}``
  (the send entry point of the dispatcher-owned destructive flow — the
  semantic recipient check is a testnet witness-v0 P2WPKH address, layer 3);
  ``confirm_tx`` → ``{"tx_ref": str, 1..64 chars}`` (references the pending
  transaction created by ``create_tx``; content is matched against the
  dispatcher-owned flow state, not here);
  ``sign_tx`` → ``{"tx_ref": str, 1..64 chars}`` plus optional
  ``{"signer": "file"|"hwi"}`` (omitted ⇒ the handler applies its default
  signer policy; the signer hands the transaction to the hardware device —
  the device screen, not the model, is the trust anchor);
  ``broadcast_tx`` → ``{"tx_ref": str, 1..64 chars}`` (references the signed
  flow record; broadcast itself is refused unless re-validation passed —
  enforced at the handler/flow level, ADR-0013);
  ``tx_status`` → ``{"txid": str}`` — the schema layer admits any string;
  the meaning-level rule (EXACTLY 64 lowercase hex characters, strict
  charset, fail closed) is layer 3 in :mod:`localwallet.protocol.intents`
  because this user/model-supplied value is interpolated into a request
  URL path — the charset check IS the injection guard. The GBNF grammar
  pins the same 64-lowercase-hex shape at decode time.
  ``node_status`` → ``{}`` exactly — no user-quoted values needed; the
  handler runs the advise-only node doctor (detect + guidance) and returns
  the dispatcher-owned FACTS for narration (Phase 4, TCK-P4-003).

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
import math
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
    "MAX_AMOUNT_SATS",
    "MAX_AMOUNT_USD",
    "MAX_QUESTION_CHARS",
    "MAX_RECIPIENT_CHARS",
    "MAX_TEXT_CHARS",
    "MAX_TX_REF_CHARS",
    "MIN_AMOUNT_SATS",
    "MIN_AMOUNT_USD",
    "MIN_RECIPIENT_CHARS",
    "BaseParams",
    "BroadcastTxParams",
    "ClarifyParams",
    "ConfirmTxParams",
    "CreateTxParams",
    "Envelope",
    "GetBalanceParams",
    "GetHistoryParams",
    "GetUtxosParams",
    "IntentName",
    "NewAddressParams",
    "NodeStatusParams",
    "RespondParams",
    "SignTxParams",
    "TxStatusParams",
    "validate_payload",
]

#: Maximum accepted length of ``respond`` params ``text`` (characters).
MAX_TEXT_CHARS: Final[int] = 4000

#: Maximum accepted length of ``clarify`` params ``question`` (characters).
MAX_QUESTION_CHARS: Final[int] = 1000

#: Accepted length bounds of ``create_tx`` params ``recipient`` (characters).
#: The floor is the shape of the shortest bech32 segwit address (hrp + sep +
#: minimal data + checksum); the ceiling is a generous transport bound. The
#: effective BIP173 bound on a real witness-v0 P2WPKH address is 90
#: characters, so the schema's 100 is deliberately transport headroom: a
#: longer string passes this layer only to be refused at layer 3, where embit
#: enforces the 90-char bech32 ceiling. The semantic check — a testnet
#: witness-v0 P2WPKH address — is layer 3 (:mod:`localwallet.protocol.intents`).
MIN_RECIPIENT_CHARS: Final[int] = 14
MAX_RECIPIENT_CHARS: Final[int] = 100

#: Accepted bounds of ``create_tx`` params ``amount_sats`` (schema layer).
#: The floor mirrors the canonical legacy-output dust figure at Core default
#: relaying; the real dust/min-relay decision is computed from script size in
#: :mod:`localwallet.tx.dust` — this is a coarse schema bound, never the dust
#: rule. The ceiling is the total possible supply in sats (21M BTC).
MIN_AMOUNT_SATS: Final[int] = 546
MAX_AMOUNT_SATS: Final[int] = 21_000_000_000_000_000

#: Accepted bounds of ``create_tx`` params ``amount_usd`` (schema layer).
#: Coarse sanity bounds for a USD-denominated send request; the sats amount
#: is computed by the tx engine from a quoted rate, never trusted from the
#: model.
MIN_AMOUNT_USD: Final[float] = 0.01
MAX_AMOUNT_USD: Final[float] = 1_000_000.0

#: Maximum accepted length of ``confirm_tx`` / ``sign_tx`` / ``broadcast_tx``
#: params ``tx_ref`` (characters).
MAX_TX_REF_CHARS: Final[int] = 64

#: The exact length of a Bitcoin transaction id (bytes rendered as hex).
TXID_LENGTH_CHARS: Final[int] = 64

#: Accepted ``sign_tx`` ``signer`` enum literals (closed set). Omitted means
#: the handler applies its default signer policy — the schema does not pick.
SIGNER_CHOICES: Final[tuple[str, ...]] = ("file", "hwi")

#: Schema field names that may appear verbatim in failure locations. Any
#: other string component of a pydantic ``loc`` is model-controlled content
#: (an extra-key name chosen by the untrusted payload) and is rendered as
#: the literal ``<key>`` instead.
_KNOWN_LOC_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "v",
        "intent",
        "params",
        "text",
        "question",
        "limit",
        "branch",
        "recipient",
        "amount_sats",
        "amount_usd",
        "fee_target",
        "tx_ref",
        "signer",
        "txid",
        "error",
        "detail",
        "code",
    }
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

    Phase 2 v0 extension (backward-compatible — see
    ``docs/adr/0013-confirm-gate.md``): ``create_tx`` and ``confirm_tx``
    join as the first two steps of the dispatcher-owned send-flow state
    machine. Emitting ``confirm_tx`` is necessary but NOT sufficient to
    move the flow: the same-turn user utterance must pass the deterministic
    confirm gate (``localwallet.tx.flow``) — an LLM "yes" never counts as
    user confirmation (PROJECT.md §8.6).

    Phase 3 v0 extension (backward-compatible — see
    ``docs/adr/0002-envelope-spec.md`` and the ADR-0013 amendment):
    ``sign_tx``, ``broadcast_tx``, and ``tx_status`` join as the remaining
    steps of the send flow plus its status lookup. Emitting ``sign_tx`` or
    ``broadcast_tx`` is necessary but NOT sufficient to move the flow: both
    require the flow in the matching dispatcher-owned state (``CONFIRMED``
    resp. ``SIGNED``) with a matching ``tx_ref``, and broadcast additionally
    requires completed signed-PSBT re-validation at the handler level
    (``localwallet.tx.revalidate``) — a mismatch is a hard stop.

    Phase 4 v0 extension (backward-compatible — see
    ``docs/adr/0002-envelope-spec.md``): ``node_status`` joins as a
    read-only intent whose handler runs the advise-only local node doctor
    (detect + guidance, ``localwallet.node``) and returns dispatcher-owned
    facts for narration. It carries no user-quoted params — the model emits
    nothing; the dispatcher owns all data.
    """

    RESPOND = "respond"
    CLARIFY = "clarify"
    GET_BALANCE = "get_balance"
    GET_HISTORY = "get_history"
    GET_UTXOS = "get_utxos"
    NEW_ADDRESS = "new_address"
    CREATE_TX = "create_tx"
    CONFIRM_TX = "confirm_tx"
    SIGN_TX = "sign_tx"
    BROADCAST_TX = "broadcast_tx"
    TX_STATUS = "tx_status"
    NODE_STATUS = "node_status"


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


class CreateTxParams(_OmitNoneDump):
    """Params for ``create_tx``: recipient + exactly one amount + optional fee.

    Contract (Phase 2 v0 extension, ADR-0013):

    - ``recipient``: REQUIRED string, 14..100 characters. Only the coarse
      shape is schema-checked here; the meaning-level check (a valid TESTNET
      witness-v0 P2WPKH bech32 address per ADR-0008) is layer 3 in
      :mod:`localwallet.protocol.intents`. The address value is never echoed
      in failures (value-free guarantee).
    - ``amount_sats`` XOR ``amount_usd``: EXACTLY ONE must be present —
      enforced by a model validator here and re-checked at layer 3. Both or
      neither is invalid (an ambiguous amount must never reach the tx
      engine; fail closed per PROJECT.md §5.5).
      ``amount_sats``: true JSON integer only (the strict-int pattern from
      ``limit``: strings/bools/floats/null rejected), 546..21_000_000_000_000_000.
      ``amount_usd``: true JSON number only (the strict-float variant of the
      same pattern: strings/bools/null rejected, non-finite floats —
      ``NaN``/``Infinity`` — rejected), 0.01..1_000_000. JSON integers are
      accepted for whole-dollar amounts (``10`` == 10.00 USD) because the
      GBNF ``amount_usd`` branch admits the integer form.
    - ``fee_target``: optional enum literal ``"fast"|"medium"|"slow"``
      (omitted ⇒ the handler applies its default); explicit ``null`` is
      rejected — omission is expressed by leaving the key out, mirroring
      ``limit``/``branch``.
    """

    recipient: str = Field(min_length=MIN_RECIPIENT_CHARS, max_length=MAX_RECIPIENT_CHARS)
    amount_sats: int | None = Field(default=None, ge=MIN_AMOUNT_SATS, le=MAX_AMOUNT_SATS)
    amount_usd: float | None = Field(default=None, ge=MIN_AMOUNT_USD, le=MAX_AMOUNT_USD)
    fee_target: Literal["fast", "medium", "slow"] | None = None

    @field_validator("amount_sats", mode="before")
    @classmethod
    def _amount_sats_must_be_true_int(cls, value: object) -> object:
        """Close pydantic's lax coercions for ``amount_sats`` (see ``limit``)."""
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        raise ValueError("amount_sats must be an integer when present")

    @field_validator("amount_usd", mode="before")
    @classmethod
    def _amount_usd_must_be_true_number(cls, value: object) -> object:
        """Strict-float variant of the strict-int pattern (untrusted input).

        Lax mode would accept ``"10.5"`` (string) and ``True`` (bool) as
        numbers; the contract admits only true JSON numbers. ``int`` is
        accepted (JSON has one number type; the grammar's ``amount_usd``
        branch admits the integer form and pydantic widens it to float).
        Non-finite floats are rejected: Python's ``json`` accepts the
        ``NaN``/``Infinity`` extensions, and an infinite/NaN USD amount must
        never reach money logic. Raises ``ValueError`` because pydantic
        ``mode="before"`` validators must raise ``ValueError``/
        ``AssertionError`` for the failure to surface as a field error.
        """
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("amount_usd must be a number when present")  # noqa: TRY004 — pydantic mode="before" validators must raise ValueError (see _v_must_be_zero_int)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("amount_usd must be a finite number when present")
        return value

    @field_validator("fee_target", mode="before")
    @classmethod
    def _fee_target_must_be_present_when_not_omitted(cls, value: object) -> object:
        """Reject explicit ``null`` for ``fee_target`` (see ``limit``).

        Omission is expressed by leaving the key out entirely; ``null`` is
        neither omitted nor an enum literal.
        """
        if value is None:
            raise ValueError("fee_target must be 'fast', 'medium', or 'slow' when present")
        return value

    @model_validator(mode="after")
    def _exactly_one_amount(self) -> CreateTxParams:
        """Enforce the amount XOR at the schema layer (layer 3 re-checks)."""
        if (self.amount_sats is None) == (self.amount_usd is None):
            raise ValueError("exactly one of amount_sats or amount_usd must be present")
        return self


class ConfirmTxParams(BaseParams):
    """Params for ``confirm_tx``: a reference to the pending transaction.

    ``tx_ref``: REQUIRED string, 1..64 characters, identifying the pending
    transaction the model is confirming (the reference is quoted verbatim
    from the confirmation card the flow produced). Only the shape is
    validated here; the flow (:mod:`localwallet.tx.flow`) matches the
    reference against its dispatcher-owned state — a schema-valid ``tx_ref``
    that does not name the pending transaction is refused there, and the
    same-turn user-utterance confirm gate must ALSO have said CONFIRM
    (ADR-0013: an LLM "yes" never counts as user confirmation).
    """

    tx_ref: str = Field(min_length=1, max_length=MAX_TX_REF_CHARS)


class SignTxParams(_OmitNoneDump):
    """Params for ``sign_tx``: hand the approved transaction to the signer.

    ``tx_ref``: REQUIRED string, 1..64 characters, referencing the
    CONFIRMED flow record — same convention as :class:`ConfirmTxParams`
    (quoted verbatim from the confirmation card; the flow matches content,
    this layer checks shape only).

    ``signer``: OPTIONAL enum literal ``"file"|"hwi"``. Omitted ⇒ the
    handler applies its default signer policy (which signer is used is a
    handler/app decision, never model-chosen beyond this closed enum);
    explicit ``null`` is rejected — omission is expressed by leaving the
    key out entirely, mirroring ``limit``/``branch``/``fee_target``. The
    device interaction itself is the user action (the trust anchor is the
    hardware wallet screen, PROJECT.md §9) — no additional utterance gate
    exists at the flow level for signing; see :mod:`localwallet.tx.flow`.
    """

    tx_ref: str = Field(min_length=1, max_length=MAX_TX_REF_CHARS)
    signer: Literal["file", "hwi"] | None = None

    @field_validator("signer", mode="before")
    @classmethod
    def _signer_must_be_present_when_not_omitted(cls, value: object) -> object:
        """Reject explicit ``null`` for ``signer`` (see ``fee_target``)."""
        if value is None:
            raise ValueError("signer must be 'file' or 'hwi' when present")
        return value


class BroadcastTxParams(BaseParams):
    """Params for ``broadcast_tx``: publish the signed transaction.

    ``tx_ref``: REQUIRED string, 1..64 characters, referencing the SIGNED
    flow record (same convention as :class:`ConfirmTxParams`). Only the
    shape is validated here; the flow refuses broadcast unless the state is
    ``SIGNED`` with a matching reference, and the handler must have
    completed signed-PSBT re-validation (:mod:`localwallet.tx.revalidate`)
    before calling the chain layer — a mismatch is a hard stop (ADR-0013).
    """

    tx_ref: str = Field(min_length=1, max_length=MAX_TX_REF_CHARS)


class TxStatusParams(BaseParams):
    """Params for ``tx_status``: look up a transaction's confirmations.

    ``txid``: REQUIRED string. The schema layer deliberately admits any
    string (coarse transport shape only); the meaning-level rule — EXACTLY
    64 LOWERCASE hex characters, strict charset, fail closed — is layer 3
    in :mod:`localwallet.protocol.intents`. The strictness is not stylistic:
    this user/model-supplied value is interpolated into a request URL path,
    so the charset check IS the injection guard (anything outside
    ``[0-9a-f]{64}`` — uppercase, whitespace, ``../``, unicode — is
    rejected before a URL is ever constructed). The GBNF grammar
    (``hex_txid``) pins the identical shape at decode time.
    """

    txid: str


class NodeStatusParams(BaseParams):
    """Params for ``node_status``: empty object, reserved for future opts.

    The node doctor needs nothing from the user or the model — detection is
    entirely dispatcher-owned. The model must emit ``"params": {}`` exactly;
    any key here is rejected (closed world). Same shape as
    :class:`GetBalanceParams` / :class:`GetUtxosParams`.
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
        IntentName.GET_HISTORY: GetHistoryParams,
        IntentName.GET_UTXOS: GetUtxosParams,
        IntentName.NEW_ADDRESS: NewAddressParams,
        IntentName.CREATE_TX: CreateTxParams,
        IntentName.CONFIRM_TX: ConfirmTxParams,
        IntentName.SIGN_TX: SignTxParams,
        IntentName.BROADCAST_TX: BroadcastTxParams,
        IntentName.TX_STATUS: TxStatusParams,
        IntentName.NODE_STATUS: NodeStatusParams,
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
        | CreateTxParams
        | ConfirmTxParams
        | SignTxParams
        | BroadcastTxParams
        | TxStatusParams
        | NodeStatusParams
    )

    @model_validator(mode="before")
    @classmethod
    def _bind_params_to_intent(cls, data: object) -> object:
        """Resolve the intent→params pairing BEFORE union coercion.

        pydantic's smart union cannot disambiguate an empty ``params``
        object across the empty-params intents (``get_balance``,
        ``get_utxos``, ``get_history``, ``new_address``, ``node_status``
        without keys): it
        would bind ``{}`` to whichever matching model comes first, and the
        pairing cross-check below would then reject a perfectly valid
        envelope. Instead, this validator looks up the registry entry for
        the declared intent and validates ``params`` against exactly that
        model, injecting the instance so the union accepts it as-is. The
        Phase 2 intents (``create_tx``, ``confirm_tx``) have required keys,
        so pydantic could disambiguate them on its own — routing them
        through the same registry lookup keeps ONE binding path for every
        intent (and keeps the value-free error rendering below uniform).

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

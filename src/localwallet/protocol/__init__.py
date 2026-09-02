"""Protocol subsystem: envelope schema, intent registry, dispatcher.

Implements the closed intent protocol (PROJECT.md §7.2, §8; wire contract
in ``docs/adr/0002-envelope-spec.md``). The three validation layers:
GBNF grammar support is in ``agent/grammar`` (layer 1, decode time); the
pydantic schema and closed world (:class:`IntentName`, params models,
:data:`INTENT_REGISTRY`) are :mod:`localwallet.protocol.envelope` (layer 2);
business rules are :mod:`localwallet.protocol.intents` (layer 3); the
allowlist dispatcher is :mod:`localwallet.protocol.dispatcher`. Module
dependencies form a single chain (``dispatcher → intents → envelope →
errors``), so any import order works.
"""

from localwallet.protocol.dispatcher import (
    MAX_VALIDATION_RETRIES,
    DispatchTable,
    Handler,
    Outcome,
    OutcomeStatus,
    dispatch,
    handle_raw,
)
from localwallet.protocol.envelope import (
    INTENT_REGISTRY,
    MAX_QUESTION_CHARS,
    MAX_TEXT_CHARS,
    BaseParams,
    BroadcastTxParams,
    ClarifyParams,
    ConfirmTxParams,
    CreateTxParams,
    Envelope,
    GetBalanceParams,
    GetHistoryParams,
    GetUtxosParams,
    IntentName,
    NewAddressParams,
    RespondParams,
    SignTxParams,
    TxStatusParams,
    validate_payload,
)
from localwallet.protocol.errors import (
    DispatchError,
    EnvelopeValidationError,
    ErrorCode,
    ErrorEnvelope,
)
from localwallet.protocol.intents import BUSINESS_RULES

__all__ = [
    "BUSINESS_RULES",
    "INTENT_REGISTRY",
    "MAX_QUESTION_CHARS",
    "MAX_TEXT_CHARS",
    "MAX_VALIDATION_RETRIES",
    "BaseParams",
    "BroadcastTxParams",
    "ClarifyParams",
    "ConfirmTxParams",
    "CreateTxParams",
    "DispatchError",
    "DispatchTable",
    "Envelope",
    "EnvelopeValidationError",
    "ErrorCode",
    "ErrorEnvelope",
    "GetBalanceParams",
    "GetHistoryParams",
    "GetUtxosParams",
    "Handler",
    "IntentName",
    "NewAddressParams",
    "Outcome",
    "OutcomeStatus",
    "RespondParams",
    "SignTxParams",
    "TxStatusParams",
    "dispatch",
    "handle_raw",
    "validate_payload",
]

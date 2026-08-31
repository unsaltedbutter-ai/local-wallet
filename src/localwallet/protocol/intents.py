"""Business rules (validation layer 3) for the closed intent protocol.

Layer responsibilities: the GBNF grammar guarantees well-formed JSON at
decode time (layer 1, ``agent/grammar/``); :mod:`localwallet.protocol.envelope`
is layer 2 (types/enums/ranges, and owner of the closed world — the
:class:`~localwallet.protocol.envelope.IntentName` enum, the params models
and :data:`INTENT_REGISTRY`, re-exported here); this module is layer 3 —
*meaning*.

:data:`BUSINESS_RULES` maps each intent to a **pure** validator
``(params_model) -> list[str]``. Pydantic already bounds types and lengths;
business rules re-check meaning-level properties (non-blank text today;
address checksum, amount bounds, fee sanity, ... in Phase 1+) and return
error strings for the dispatcher to surface. An empty list means valid.

Adding rules never widens the model's freedom: rules only reject, they
never transform or execute.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from localwallet.protocol.envelope import (
    INTENT_REGISTRY,
    BaseParams,
    ClarifyParams,
    GetBalanceParams,
    IntentName,
    RespondParams,
)

__all__ = [
    "BUSINESS_RULES",
    "INTENT_REGISTRY",
    "BusinessRule",
    "IntentName",
]

type BusinessRule = Callable[[BaseParams], list[str]]


def _rule_respond(params: BaseParams) -> list[str]:
    """``respond``: text must be non-empty after stripping whitespace."""
    if not isinstance(params, RespondParams):
        return ["internal: 'respond' params failed the type check"]
    if not params.text.strip():
        return ["params.text must contain at least one non-whitespace character"]
    return []


def _rule_clarify(params: BaseParams) -> list[str]:
    """``clarify``: question must be non-empty after stripping whitespace."""
    if not isinstance(params, ClarifyParams):
        return ["internal: 'clarify' params failed the type check"]
    if not params.question.strip():
        return ["params.question must contain at least one non-whitespace character"]
    return []


def _rule_get_balance(params: BaseParams) -> list[str]:
    """``get_balance``: no meaning-level rules yet (reserved for future opts)."""
    if not isinstance(params, GetBalanceParams):
        return ["internal: 'get_balance' params failed the type check"]
    return []


#: Layer-3 business rules, per intent. Values are pure functions from the
#: validated params model to a list of error strings (empty list == valid).
#: Frozen (``MappingProxyType``) for symmetry with the frozen
#: ``INTENT_REGISTRY`` — the rule set is closed, not runtime-extendable.
BUSINESS_RULES: Mapping[IntentName, BusinessRule] = MappingProxyType(
    {
        IntentName.RESPOND: _rule_respond,
        IntentName.CLARIFY: _rule_clarify,
        IntentName.GET_BALANCE: _rule_get_balance,
    }
)

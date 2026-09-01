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
    GetHistoryParams,
    GetUtxosParams,
    IntentName,
    NewAddressParams,
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


def _rule_get_history(params: BaseParams) -> list[str]:
    """``get_history``: limit, when present, must be a true int in 1..100.

    The schema layer already bounds ``limit`` identically (and rejects
    bools); this is the layer-3 re-check (defense in depth), reachable via
    a constructor that skipped validation. ``bool`` is rejected explicitly
    because ``True``/``False`` pass the ``1 <= x <= 100`` comparison as the
    ints 1/0 — the layer-2 schema already refuses them, so layer 3 must
    agree (bool is not a JSON integer).
    """
    if not isinstance(params, GetHistoryParams):
        return ["internal: 'get_history' params failed the type check"]
    if isinstance(params.limit, bool) or (
        params.limit is not None and not 1 <= params.limit <= 100
    ):
        return ["params.limit must be an integer between 1 and 100 when present"]
    return []


def _rule_get_utxos(params: BaseParams) -> list[str]:
    """``get_utxos``: no meaning-level rules yet (registry completeness only)."""
    if not isinstance(params, GetUtxosParams):
        return ["internal: 'get_utxos' params failed the type check"]
    return []


def _rule_new_address(params: BaseParams) -> list[str]:
    """``new_address``: branch, when present, must be the true int 0 or 1.

    Same layer-3 re-check pattern as ``get_history``: the schema bounds
    ``branch`` to {0, 1} (and rejects bools) already. ``bool`` is rejected
    explicitly here too — ``False``/``True`` equal the ints 0/1, so a
    validation-skipping constructor would otherwise let them through.
    """
    if not isinstance(params, NewAddressParams):
        return ["internal: 'new_address' params failed the type check"]
    if isinstance(params.branch, bool) or (
        params.branch is not None and params.branch not in (0, 1)
    ):
        return ["params.branch must be the integer 0 or 1 when present"]
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
        IntentName.GET_HISTORY: _rule_get_history,
        IntentName.GET_UTXOS: _rule_get_utxos,
        IntentName.NEW_ADDRESS: _rule_new_address,
    }
)

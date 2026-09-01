"""Business rules (validation layer 3) for the closed intent protocol.

Layer responsibilities: the GBNF grammar guarantees well-formed JSON at
decode time (layer 1, ``agent/grammar/``); :mod:`localwallet.protocol.envelope`
is layer 2 (types/enums/ranges, and owner of the closed world — the
:class:`~localwallet.protocol.envelope.IntentName` enum, the params models
and :data:`INTENT_REGISTRY`, re-exported here); this module is layer 3 —
*meaning*.

:data:`BUSINESS_RULES` maps each intent to a **pure** validator
``(params_model) -> list[str]``. Pydantic already bounds types and lengths;
business rules re-check meaning-level properties (non-blank text; for
``create_tx``: the recipient is a valid testnet witness-v0 P2WPKH bech32
address per ADR-0008 and the amount XOR; for ``confirm_tx``: the ``tx_ref``
shape) and return error strings for the dispatcher to surface. An empty
list means valid.

Adding rules never widens the model's freedom: rules only reject, they
never transform or execute.

Dependency note — embit in ``protocol/``: this module imports
``embit.bech32`` for offline address decoding ONLY. That is acceptable
inside the stdlib+pydantic rule because embit is a pure, offline
serialization/crypto library with no network, filesystem, or subprocess
I/O (``tools/lint_network.py`` enforces the actual invariant: no network
modules outside ``chain/``), and address checksum/version validation is
exactly the kind of Bitcoin-primitive meaning check layer 3 exists for.
No embit type crosses this module's interface: rules take the validated
params model and return plain strings.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from embit import bech32

from localwallet.protocol.envelope import (
    INTENT_REGISTRY,
    MAX_AMOUNT_SATS,
    MAX_AMOUNT_USD,
    MIN_AMOUNT_SATS,
    MIN_AMOUNT_USD,
    BaseParams,
    ClarifyParams,
    ConfirmTxParams,
    CreateTxParams,
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


#: Human-identifier part (HRP) of a testnet bech32 address (BIP173).
_TESTNET_HRP: str = "tb"

#: Required witness version (0 = native segwit v0) and P2WPKH program length
#: (20-byte keyhash) for send recipients, per ADR-0008 (P2WPKH-only v1 send;
#: taproot/P2WSH/other versions refused).
_WITNESS_V0: int = 0
_P2WPKH_PROGRAM_LEN: int = 20


def _recipient_rule_failure(params: CreateTxParams) -> list[str]:
    """Validate the ``create_tx`` recipient semantically (value-free).

    The recipient must decode as a TESTNET bech32 address with witness
    version 0 and a 20-byte program (P2WPKH) — ADR-0008. Each failure mode
    gets a specific, value-free string: the address itself (and any prefix
    of it) is NEVER echoed, because failure strings flow into error
    envelopes and logs where addresses must not appear (PROJECT.md §7.8).
    Pure: decoding is offline computation on a string, no network.
    """
    encoding, hrp, _data = bech32.bech32_decode(params.recipient)
    if encoding is None or hrp is None:
        return ["recipient is not a valid testnet bech32 address"]
    if hrp != _TESTNET_HRP:
        return ["recipient is not a testnet bech32 address (wrong network prefix)"]
    witver, program = bech32.decode(_TESTNET_HRP, params.recipient)
    if witver is None or program is None:
        # Checksum/charset already passed above, so this is a malformed
        # witness program (length outside 2..40 bytes).
        return ["recipient is not a valid testnet bech32 address"]
    if witver != _WITNESS_V0:
        return ["recipient must be a witness version 0 address (taproot v1 and later are not supported)"]
    if len(program) != _P2WPKH_PROGRAM_LEN:
        return ["recipient must be a P2WPKH address (witness v0 with a 20-byte program)"]
    return []


def _rule_create_tx(params: BaseParams) -> list[str]:
    """``create_tx``: amount XOR + bounds + testnet P2WPKH recipient (ADR-0008/0013).

    - Exactly one of ``amount_sats``/``amount_usd`` must be present. The
      schema layer already enforces the XOR; this is the layer-3 re-check
      (defense in depth, reachable via a validation-skipping constructor).
    - The amounts are re-checked against their business bounds (``amount_sats``
      546..21_000_000_000_000_000; ``amount_usd`` 0.01..1_000_000) — the same
      layer-3 re-check pattern as ``get_history``'s ``limit`` /
      ``new_address``'s ``branch``. ``bool`` is rejected explicitly because
      ``True``/``False`` pass the range comparisons as the ints 1/0 (and
      ``False`` equals the float 0.0 for ``amount_usd``) — the layer-2 schema
      already refuses them, so layer 3 must agree (bool is not a JSON number).
    - ``fee_target`` needs no rule here: it is enum-validated at layer 2.
    - The recipient is checked by :func:`_recipient_rule_failure`.
    """
    if not isinstance(params, CreateTxParams):
        return ["internal: 'create_tx' params failed the type check"]
    if (params.amount_sats is None) == (params.amount_usd is None):
        return ["params must contain exactly one of amount_sats or amount_usd"]
    if params.amount_sats is not None and (
        isinstance(params.amount_sats, bool)
        or not MIN_AMOUNT_SATS <= params.amount_sats <= MAX_AMOUNT_SATS
    ):
        return [
            f"params.amount_sats must be an integer between {MIN_AMOUNT_SATS} and {MAX_AMOUNT_SATS}"
        ]
    if params.amount_usd is not None and (
        isinstance(params.amount_usd, bool)
        or not MIN_AMOUNT_USD <= params.amount_usd <= MAX_AMOUNT_USD
    ):
        return [
            f"params.amount_usd must be a number between {MIN_AMOUNT_USD} and {MAX_AMOUNT_USD}"
        ]
    return _recipient_rule_failure(params)


def _rule_confirm_tx(params: BaseParams) -> list[str]:
    """``confirm_tx``: ``tx_ref`` shape only — never content matching.

    The reference must be non-empty (after stripping whitespace) and
    printable (no control characters). Whether it names the actual pending
    transaction is decided by the dispatcher-owned flow
    (:mod:`localwallet.tx.flow`), not by rules — and even a matching
    reference only moves the flow when the same-turn user utterance passed
    the deterministic confirm gate (ADR-0013).
    """
    if not isinstance(params, ConfirmTxParams):
        return ["internal: 'confirm_tx' params failed the type check"]
    if not params.tx_ref.strip():
        return ["params.tx_ref must be a non-empty transaction reference"]
    if not params.tx_ref.isprintable():
        return ["params.tx_ref must contain only printable characters"]
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
        IntentName.CREATE_TX: _rule_create_tx,
        IntentName.CONFIRM_TX: _rule_confirm_tx,
    }
)

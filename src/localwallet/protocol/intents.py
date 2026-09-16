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
``create_tx``: the recipient is a valid mainnet witness-v0 P2WPKH bech32
address per ADR-0008 and the amount XOR; for ``confirm_tx``/``sign_tx``/
``broadcast_tx``: the ``tx_ref`` shape; for ``tx_status``: the ``txid`` is
EXACTLY 64 lowercase hex characters — the strict charset check that guards
the URL path this user/model-supplied value is interpolated into; for
``self_transfer``: the mode↔key pairing — split↔parts / consolidate↔
below_size_sats, exactly one — and the bound re-checks, TCK-TX-SELF-001;
for ``bump_fee``: the ``target`` / ``funding_ref`` shape — non-blank
printable strings, the txid-vs-pending-ref resolution is the handler's,
TCK-RBF-003; for ``get_history``/``get_utxos``: the TCK-CHAT-005 money
filters — closed ``direction`` literals, exactly-one-bounded-unit
``since``, 1..10 non-blank printable label words, ``label_mode`` only
alongside ``label_set`` — carrier shape only: the timestamp and label
RESOLUTION are handler-side, engine work, TCK-CHAT-005) and return error
strings for the dispatcher to surface. An empty list means valid.

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
    MAX_ADDRESS_NUMBER,
    MAX_AMOUNT_SATS,
    MAX_AMOUNT_USD,
    MAX_LABEL_FILTER_WORD_CHARS,
    MAX_LABEL_FILTER_WORDS,
    MAX_SELF_TRANSFER_PARTS,
    MAX_SINCE_DAYS,
    MAX_SINCE_MONTHS,
    MAX_SINCE_WEEKS,
    MIN_AMOUNT_SATS,
    MIN_AMOUNT_USD,
    MIN_SELF_TRANSFER_PARTS,
    TXID_LENGTH_CHARS,
    BaseParams,
    BroadcastTxParams,
    BumpFeeParams,
    ClarifyParams,
    ConfirmTxParams,
    CreateTxParams,
    GetAddressesParams,
    GetBalanceParams,
    GetHistoryParams,
    GetUtxosParams,
    IntentName,
    NewAddressParams,
    NodeStatusParams,
    RespondParams,
    SelfTransferParams,
    SignTxParams,
    SincePeriod,
    TxStatusParams,
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


def _address_number_failures(
    params: GetBalanceParams | GetUtxosParams | GetAddressesParams,
) -> list[str]:
    """Shared layer-3 re-check for the TCK-CHAT-001 ``address_number`` key.

    When present it must be a TRUE int in ``1..MAX_ADDRESS_NUMBER`` (the
    ``get_history.limit`` pattern: ``bool`` rejected explicitly because
    ``True``/``False`` pass the range comparison as the ints 1/0). The
    schema layer already bounds it identically — this is defense in depth,
    reachable via a validation-skipping constructor. Crucially, this is
    only a TRANSPORT bound: whether the number NAMES a registry entry at
    all is decided engine-side against the store (the stable per-wallet
    registry), where a miss answers the value-free clarify — this layer
    never resolves and never guesses.
    """
    number = params.address_number
    if isinstance(number, bool) or (
        number is not None and not 1 <= number <= MAX_ADDRESS_NUMBER
    ):
        return [
            (
                "params.address_number must be an integer between 1 and "
                f"{MAX_ADDRESS_NUMBER} when present"
            )
        ]
    return []


def _rule_get_balance(params: BaseParams) -> list[str]:
    """``get_balance``: the additive ``address_number`` re-check (TCK-CHAT-001)."""
    if not isinstance(params, GetBalanceParams):
        return ["internal: 'get_balance' params failed the type check"]
    return _address_number_failures(params)


#: Closed literal sets for the TCK-CHAT-005 money-filter keys (layer-2
#: Literal types admit exactly these; the layer-3 re-check is defense in
#: depth against a validation-skipping constructor).
_DIRECTIONS: frozenset[str] = frozenset({"in", "out"})
_LABEL_MODES: frozenset[str] = frozenset({"include", "exclude"})


def _money_filter_failures(params: GetHistoryParams | GetUtxosParams) -> list[str]:
    """Layer-3 re-check of the TCK-CHAT-005 filter keys (both intents share it).

    Everything here is CARRIER shape — meaning lives engine-side: the
    ``since`` period's cutoff timestamp is resolved by the HANDLER from
    tool-owned now (calendar-exact months; the model never computes a
    timestamp and no absolute date is representable), and label words
    resolve against the v6 address-label-set (address membership + coin
    inheritance). A word that matches no label anywhere is an honest
    empty RESULT, never a rule failure — these rules only reject
    malformed carriers. VALUE-FREE by construction: no offending value
    (label word included) is ever echoed into a failure string.
    """
    failures: list[str] = []
    if params.direction is not None and params.direction not in _DIRECTIONS:
        failures.append("params.direction must be 'in' or 'out' when present")
    since = params.since
    if since is not None:
        if not isinstance(since, SincePeriod):
            failures.append("params.since must be a relative period object")
        else:
            units = (
                ("days", since.days, MAX_SINCE_DAYS),
                ("weeks", since.weeks, MAX_SINCE_WEEKS),
                ("months", since.months, MAX_SINCE_MONTHS),
            )
            given = [unit for unit in units if unit[1] is not None]
            if len(given) != 1:
                failures.append(
                    "params.since must carry exactly one of days, weeks or months"
                )
            else:
                name, value, cap = given[0]
                if isinstance(value, bool) or not 1 <= value <= cap:
                    failures.append(
                        f"params.since.{name} must be an integer between 1 and {cap}"
                    )
    words = params.label_set
    if words is not None:
        if not 1 <= len(words) <= MAX_LABEL_FILTER_WORDS:
            failures.append(
                "params.label_set must carry between 1 and "
                f"{MAX_LABEL_FILTER_WORDS} label words"
            )
        elif any(
            not isinstance(w, str) or not w.strip() or not w.isprintable() or len(w) > MAX_LABEL_FILTER_WORD_CHARS
            for w in words
        ):
            failures.append(
                "params.label_set entries must be non-blank printable strings "
                f"of at most {MAX_LABEL_FILTER_WORD_CHARS} characters"
            )
    if params.label_mode is not None:
        if params.label_mode not in _LABEL_MODES:
            failures.append(
                "params.label_mode must be 'include' or 'exclude' when present"
            )
        elif words is None:
            failures.append("params.label_mode requires params.label_set")
    return failures


def _rule_get_history(params: BaseParams) -> list[str]:
    """``get_history``: limit re-check + the additive money-filter keys.

    ``limit``, when present, must be a true int in 1..100.

    The schema layer already bounds ``limit`` identically (and rejects
    bools); this is the layer-3 re-check (defense in depth), reachable via
    a constructor that skipped validation. ``bool`` is rejected explicitly
    because ``True``/``False`` pass the ``1 <= x <= 100`` comparison as the
    ints 1/0 — the layer-2 schema already refuses them, so layer 3 must
    agree (bool is not a JSON integer). The TCK-CHAT-005 keys ride the
    shared :func:`_money_filter_failures` re-check.
    """
    if not isinstance(params, GetHistoryParams):
        return ["internal: 'get_history' params failed the type check"]
    if isinstance(params.limit, bool) or (
        params.limit is not None and not 1 <= params.limit <= 100
    ):
        return ["params.limit must be an integer between 1 and 100 when present"]
    return _money_filter_failures(params)


def _rule_get_utxos(params: BaseParams) -> list[str]:
    """``get_utxos``: the additive ``address_number`` re-check (TCK-CHAT-001)
    + the additive money-filter keys re-check (TCK-CHAT-005)."""
    if not isinstance(params, GetUtxosParams):
        return ["internal: 'get_utxos' params failed the type check"]
    return _address_number_failures(params) + _money_filter_failures(params)


def _rule_get_addresses(params: BaseParams) -> list[str]:
    """``get_addresses``: the additive ``address_number`` re-check (TCK-CHAT-001).

    The rule is transport shape only. The store's registry decides whether
    the number names a real shown address (handler-side bound-check →
    value-free clarify on a miss); the LIST case (no key) has nothing to
    check. The model never authors the number — it quotes it from the
    FACTS-injected registry (ADR-0002 amendment).
    """
    if not isinstance(params, GetAddressesParams):
        return ["internal: 'get_addresses' params failed the type check"]
    return _address_number_failures(params)


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


#: Human-identifier part (HRP) of a mainnet bech32 address (BIP173).
_MAINNET_HRP: str = "bc"

#: Required witness version (0 = native segwit v0) and P2WPKH program length
#: (20-byte keyhash) for send recipients, per ADR-0008 (P2WPKH-only v1 send;
#: taproot/P2WSH/other versions refused).
_WITNESS_V0: int = 0
_P2WPKH_PROGRAM_LEN: int = 20


def _recipient_rule_failure(params: CreateTxParams) -> list[str]:
    """Validate the ``create_tx`` recipient semantically (value-free).

    The recipient must decode as a MAINNET bech32 address with witness
    version 0 and a 20-byte program (P2WPKH) — ADR-0008. Each failure mode
    gets a specific, value-free string: the address itself (and any prefix
    of it) is NEVER echoed, because failure strings flow into error
    envelopes and logs where addresses must not appear (PROJECT.md §7.8).
    Pure: decoding is offline computation on a string, no network.
    """
    encoding, hrp, _data = bech32.bech32_decode(params.recipient)
    if encoding is None or hrp is None:
        return ["recipient is not a valid mainnet bech32 address"]
    if hrp != _MAINNET_HRP:
        return ["recipient is not a mainnet bech32 address (wrong network prefix)"]
    witver, program = bech32.decode(_MAINNET_HRP, params.recipient)
    if witver is None or program is None:
        # Checksum/charset already passed above, so this is a malformed
        # witness program (length outside 2..40 bytes).
        return ["recipient is not a valid mainnet bech32 address"]
    if witver != _WITNESS_V0:
        return ["recipient must be a witness version 0 address (taproot v1 and later are not supported)"]
    if len(program) != _P2WPKH_PROGRAM_LEN:
        return ["recipient must be a P2WPKH address (witness v0 with a 20-byte program)"]
    return []


def _rule_create_tx(params: BaseParams) -> list[str]:
    """``create_tx``: amount XOR + bounds + mainnet P2WPKH recipient (ADR-0008/0013).

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


def _tx_ref_shape_failures(
    field: str, value: str, *, noun: str = "transaction reference"
) -> list[str]:
    """Shared shape rule for a quoted reference (tx_ref / bump_fee target/funding_ref).

    The reference must be non-empty (after stripping whitespace) and
    printable (no control characters). Whether it names the actual
    pending/confirmed/signed transaction is decided by the dispatcher-owned
    flow (:mod:`localwallet.tx.flow`), not by rules — and for ``confirm_tx``
    even a matching reference only moves the flow when the same-turn user
    utterance passed the deterministic confirm gate (ADR-0013). ``noun``
    names what the field refers to in the failure string (default
    "transaction reference"; ``bump_fee``'s ``funding_ref`` is a "coin
    reference").
    """
    if not value.strip():
        return [f"params.{field} must be a non-empty {noun}"]
    if not value.isprintable():
        return [f"params.{field} must contain only printable characters"]
    return []


def _rule_confirm_tx(params: BaseParams) -> list[str]:
    """``confirm_tx``: ``tx_ref`` shape only — never content matching."""
    if not isinstance(params, ConfirmTxParams):
        return ["internal: 'confirm_tx' params failed the type check"]
    return _tx_ref_shape_failures("tx_ref", params.tx_ref)


def _rule_sign_tx(params: BaseParams) -> list[str]:
    """``sign_tx``: ``tx_ref`` shape only (same convention as confirm_tx).

    ``signer`` needs no rule here: it is enum-validated at layer 2, and
    omission means the handler applies its default signer policy. The flow
    (:mod:`localwallet.tx.flow`) refuses the transition unless the state is
    CONFIRMED with a matching ``tx_ref``; the device interaction itself is
    the user action (trust anchor: the hardware wallet screen, §9).
    """
    if not isinstance(params, SignTxParams):
        return ["internal: 'sign_tx' params failed the type check"]
    return _tx_ref_shape_failures("tx_ref", params.tx_ref)


def _rule_broadcast_tx(params: BaseParams) -> list[str]:
    """``broadcast_tx``: ``tx_ref`` shape only (same convention as confirm_tx).

    The flow refuses broadcast unless the state is SIGNED with a matching
    ``tx_ref``, and the handler must additionally have completed signed-PSBT
    re-validation (:mod:`localwallet.tx.revalidate`) — those gates are the
    broadcast discipline (ADR-0013); rules check shape only.
    """
    if not isinstance(params, BroadcastTxParams):
        return ["internal: 'broadcast_tx' params failed the type check"]
    return _tx_ref_shape_failures("tx_ref", params.tx_ref)


#: The strict lowercase-hex charset of a transaction id (no uppercase: the
#: contract is lowercase-only so a txid quoted by the model can be compared
#: verbatim and interpolated into request paths without normalization).
_TXID_CHARSET: frozenset[str] = frozenset("0123456789abcdef")


def _rule_tx_status(params: BaseParams) -> list[str]:
    """``tx_status``: ``txid`` must be EXACTLY 64 lowercase hex characters.

    Strict charset check, fail closed: this user/model-supplied value is
    passed to the wallet adapters' ``get_tx_status`` (Electrum/bitcoind),
    so the charset check IS the injection guard — uppercase hex, whitespace,
    ``../`` traversal fragments, unicode, and wrong lengths are all rejected
    here, before any request URL is constructed. The GBNF ``hex_txid`` rule
    pins the identical shape at decode time; this layer is the authority for
    non-grammar producers. Value-free failures: the txid itself is never
    echoed.
    """
    if not isinstance(params, TxStatusParams):
        return ["internal: 'tx_status' params failed the type check"]
    txid = params.txid
    if len(txid) != TXID_LENGTH_CHARS or not set(txid) <= _TXID_CHARSET:
        return [
            "params.txid must be exactly 64 lowercase hexadecimal characters"
        ]
    return []


def _rule_node_status(params: BaseParams) -> list[str]:
    """``node_status``: no meaning-level rules yet (registry completeness only).

    The intent carries empty params and the handler performs no privileged
    action — detection is advise-only and never runs commands. The rule is
    the closed-world completeness placeholder, mirroring ``get_utxos``.
    """
    if not isinstance(params, NodeStatusParams):
        return ["internal: 'node_status' params failed the type check"]
    return []


def _rule_self_transfer(params: BaseParams) -> list[str]:
    """``self_transfer``: mode↔key pairing + bounds (TCK-TX-SELF-001; the
    additive ``cpfp`` mode per TCK-CPFP-001, ADR-0002 bump policy).

    Layer-3 re-checks of the pairing the schema and grammar already enforce
    (defense in depth, reachable via a validation-skipping constructor):

    - ``split`` requires ``parts`` and forbids ``below_size_sats``;
      ``consolidate`` requires ``below_size_sats`` and forbids ``parts``;
      ``cpfp`` requires NO number key (the engine resolves the stuck
      inbound coin) and solely owns the optional ``merge_coin`` flag
      (strictly a bool) — exactly the mode's keys, "nothing else".
    - ``parts`` re-checked as a true int in ``MIN..MAX_SELF_TRANSFER_PARTS``
      and ``below_size_sats`` as a true int in ``MIN..MAX_AMOUNT_SATS``
      (``bool`` rejected explicitly — the ``get_history.limit`` pattern).
    - The real money decisions live in the handler: which coin to split,
      which coins fall below the threshold (against stored values), the
      per-output dust floors computed from script size in
      :mod:`localwallet.tx.dust`, and the pool-side privacy rule. Rules
      only reject; they never pick outputs — the params carry no address or
      outpoint at all, so an invented output is unrepresentable here.
    """
    if not isinstance(params, SelfTransferParams):
        return ["internal: 'self_transfer' params failed the type check"]
    if params.mode == "split":
        if params.below_size_sats is not None:
            return ["params.below_size_sats is not valid for mode 'split'"]
        if params.merge_coin is not None:
            return ["params.merge_coin is not valid for mode 'split'"]
        if params.parts is None:
            return ["mode 'split' requires params.parts"]
        if isinstance(params.parts, bool) or not (
            MIN_SELF_TRANSFER_PARTS <= params.parts <= MAX_SELF_TRANSFER_PARTS
        ):
            return [
                (
                    "params.parts must be an integer between "
                    f"{MIN_SELF_TRANSFER_PARTS} and {MAX_SELF_TRANSFER_PARTS}"
                )
            ]
        return []
    if params.mode == "cpfp":
        if params.parts is not None:
            return ["params.parts is not valid for mode 'cpfp'"]
        if params.below_size_sats is not None:
            return ["params.below_size_sats is not valid for mode 'cpfp'"]
        if params.merge_coin is not None and not isinstance(params.merge_coin, bool):
            return ["params.merge_coin must be a boolean"]
        return []
    if params.parts is not None:
        return ["params.parts is not valid for mode 'consolidate'"]
    if params.merge_coin is not None:
        return ["params.merge_coin is not valid for mode 'consolidate'"]
    if params.below_size_sats is None:
        return ["mode 'consolidate' requires params.below_size_sats"]
    if isinstance(params.below_size_sats, bool) or not (
        MIN_AMOUNT_SATS <= params.below_size_sats <= MAX_AMOUNT_SATS
    ):
        return [
            (
                f"params.below_size_sats must be an integer between {MIN_AMOUNT_SATS} "
                f"and {MAX_AMOUNT_SATS}"
            )
        ]
    return []


def _rule_bump_fee(params: BaseParams) -> list[str]:
    """``bump_fee``: ``target`` / ``funding_ref`` shape (TCK-RBF-003).

    Both are carried as shape-validated strings, so the meaning-level check
    is shape only, mirroring the ``tx_ref`` convention (non-blank after
    stripping, printable — no control characters). Whether ``target`` names
    a real in-flight transaction (64-hex txid) or the app's pending-ref
    token, and whether the quoted ``funding_ref`` / fee knob resolve against
    store lineage / flow state, is decided by the RBF-004/005 handler, NOT
    here — this layer only guarantees a well-formed carrier. The model never
    computes the new fee; it may only carry a rung or a quoted whole-sat
    rate (both enum/bound-validated at layer 2).
    """
    if not isinstance(params, BumpFeeParams):
        return ["internal: 'bump_fee' params failed the type check"]
    failures = _tx_ref_shape_failures("target", params.target)
    if params.funding_ref is not None:
        failures += _tx_ref_shape_failures(
            "funding_ref", params.funding_ref, noun="coin reference"
        )
    return failures


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
        IntentName.SIGN_TX: _rule_sign_tx,
        IntentName.BROADCAST_TX: _rule_broadcast_tx,
        IntentName.TX_STATUS: _rule_tx_status,
        IntentName.NODE_STATUS: _rule_node_status,
        IntentName.SELF_TRANSFER: _rule_self_transfer,
        IntentName.BUMP_FEE: _rule_bump_fee,
        IntentName.GET_ADDRESSES: _rule_get_addresses,
    }
)

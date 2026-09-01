"""Tests for the protocol core (TCK-P0-002, extended by TCK-P1-003).

Covers the canonical envelope contract v0 (including the Phase 1 v0
extension with get_history / get_utxos / new_address): accept/reject
matrices, the closed intent registry, business-rule layer invocation, the
allowlist dispatcher (including defense-in-depth and handler-exception
surfacing), the handle_raw retry policy (exactly one re-prompt before
escalation), and the system→UI error envelope shape.
"""

import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.protocol import (
    BUSINESS_RULES,
    INTENT_REGISTRY,
    MAX_QUESTION_CHARS,
    MAX_TEXT_CHARS,
    MAX_VALIDATION_RETRIES,
    ClarifyParams,
    ConfirmTxParams,
    CreateTxParams,
    DispatchError,
    Envelope,
    EnvelopeValidationError,
    ErrorCode,
    ErrorEnvelope,
    GetBalanceParams,
    GetHistoryParams,
    GetUtxosParams,
    IntentName,
    NewAddressParams,
    OutcomeStatus,
    RespondParams,
    dispatch,
    handle_raw,
    validate_payload,
)
from localwallet.protocol.envelope import (
    _MAX_FAILURE_CHARS,
    MAX_AMOUNT_SATS,
    MAX_AMOUNT_USD,
    MAX_TX_REF_CHARS,
    MIN_AMOUNT_SATS,
    MIN_AMOUNT_USD,
)

# ---------------------------------------------------------------- helpers

#: A schema- and rules-valid testnet P2WPKH address (BIP173 testnet vector
#: for pubkey hash 751e76e8...). Used only as a well-formed fixture value.
TESTNET_P2WPKH = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"

ACCEPT_CASES = {
    "respond": {"v": 0, "intent": "respond", "params": {"text": "hello there"}},
    "clarify": {"v": 0, "intent": "clarify", "params": {"question": "how fast?"}},
    "get_balance": {"v": 0, "intent": "get_balance", "params": {}},
    # Phase 1 v0 extension (ADR-0002): three new intents. Both grammar
    # branches (empty params / optional key) are represented.
    "get_history": {"v": 0, "intent": "get_history", "params": {}},
    "get_history_limit": {"v": 0, "intent": "get_history", "params": {"limit": 20}},
    "get_utxos": {"v": 0, "intent": "get_utxos", "params": {}},
    "new_address": {"v": 0, "intent": "new_address", "params": {}},
    "new_address_branch": {"v": 0, "intent": "new_address", "params": {"branch": 0}},
    # Phase 2 v0 extension (ADR-0013): the send-flow entry + confirm step.
    "create_tx": {
        "v": 0,
        "intent": "create_tx",
        "params": {"recipient": TESTNET_P2WPKH, "amount_sats": 250000},
    },
    "confirm_tx": {"v": 0, "intent": "confirm_tx", "params": {"tx_ref": "3f2a9c"}},
}

PARAMS_TYPES = {
    IntentName.RESPOND: RespondParams,
    IntentName.CLARIFY: ClarifyParams,
    IntentName.GET_BALANCE: GetBalanceParams,
    IntentName.GET_HISTORY: GetHistoryParams,
    IntentName.GET_UTXOS: GetUtxosParams,
    IntentName.NEW_ADDRESS: NewAddressParams,
    IntentName.CREATE_TX: CreateTxParams,
    IntentName.CONFIRM_TX: ConfirmTxParams,
}

HANDLER_RESULTS = {
    IntentName.RESPOND: {"narrated": True},
    IntentName.CLARIFY: {"asked": True},
    IntentName.GET_BALANCE: {"confirmed_sat": 0, "unconfirmed_sat": 0},
    IntentName.GET_HISTORY: {"records": 0},
    IntentName.GET_UTXOS: {"outputs": 0},
    IntentName.NEW_ADDRESS: {"allocated": 0},
    IntentName.CREATE_TX: {"staged": True},
    IntentName.CONFIRM_TX: {"confirmed": True},
}


def _rule_stub(params: object) -> list[str]:
    """Stand-in business rule used only to probe BUSINESS_RULES frozenness."""
    return []


def make_table(recorded: list[Envelope] | None = None) -> tuple[dict, list[Envelope]]:
    """A dispatch table with one recording stub handler per known intent."""
    recorded = recorded if recorded is not None else []
    table: dict = {}
    for intent, result in HANDLER_RESULTS.items():

        def handler(envelope: Envelope, _result: dict = result) -> dict:
            recorded.append(envelope)
            return _result

        table[intent] = handler
    return table, recorded


def expect_rejected(raw: object) -> EnvelopeValidationError:
    """Assert validate_payload raises a well-formed EnvelopeValidationError."""
    with pytest.raises(EnvelopeValidationError) as excinfo:
        validate_payload(raw)  # type: ignore[arg-type]
    failures = excinfo.value.failures
    assert isinstance(failures, tuple) and failures, "failures must be a non-empty tuple"
    assert all(isinstance(f, str) and f for f in failures)
    return excinfo.value


# ---------------------------------------------------------- public surface

def test_public_api_reexports():
    """Every name promised in __all__ is importable from the package."""
    import localwallet.protocol as pkg

    assert len(pkg.__all__) > 0
    for name in pkg.__all__:
        assert hasattr(pkg, name), f"missing public API name: {name}"


def test_policy_constants():
    assert MAX_VALIDATION_RETRIES == 1
    assert MAX_TEXT_CHARS == 4000
    assert MAX_QUESTION_CHARS == 1000


# ----------------------------------------------------------- intent registry

def test_intent_enum_is_the_closed_world():
    assert {m.value for m in IntentName} == {
        "respond",
        "clarify",
        "get_balance",
        "get_history",
        "get_utxos",
        "new_address",
        "create_tx",
        "confirm_tx",
    }
    assert len(IntentName) == 8


def test_intent_registry_is_frozen_and_complete():
    assert set(INTENT_REGISTRY.keys()) == set(IntentName)
    assert len(INTENT_REGISTRY) == 8
    for intent, model in INTENT_REGISTRY.items():
        assert model is PARAMS_TYPES[intent]
    # frozen mapping: mutation is refused
    with pytest.raises(TypeError):
        INTENT_REGISTRY[IntentName.RESPOND] = ClarifyParams  # type: ignore[index]
    # StrEnum keys resolve from plain strings too
    assert INTENT_REGISTRY["respond"] is RespondParams


def test_business_rules_cover_every_intent():
    assert set(BUSINESS_RULES.keys()) == set(IntentName)
    # explicit count: registry completeness is pinned, not incidental
    assert len(BUSINESS_RULES) == 8
    for intent in IntentName:
        assert callable(BUSINESS_RULES[intent])
    # frozen mapping: mutation is refused (symmetry with INTENT_REGISTRY)
    with pytest.raises(TypeError):
        BUSINESS_RULES[IntentName.RESPOND] = _rule_stub  # type: ignore[index]


# ------------------------------------------------------------ accept matrix

@pytest.mark.parametrize(
    ("intent", "payload"),
    [(name, payload) for name, payload in ACCEPT_CASES.items()],
    ids=list(ACCEPT_CASES),
)
def test_accept_matrix_one_valid_envelope_per_intent(intent: str, payload: dict):
    envelope = validate_payload(payload)
    assert isinstance(envelope, Envelope)
    assert envelope.v == 0
    assert envelope.intent == IntentName(payload["intent"])
    assert isinstance(envelope.params, PARAMS_TYPES[envelope.intent])
    assert envelope.model_dump() == payload


def test_accept_length_boundaries():
    ok_respond = {"v": 0, "intent": "respond", "params": {"text": "x" * MAX_TEXT_CHARS}}
    ok_clarify = {
        "v": 0,
        "intent": "clarify",
        "params": {"question": "y" * MAX_QUESTION_CHARS},
    }
    assert validate_payload(ok_respond).params.text == "x" * MAX_TEXT_CHARS
    assert validate_payload(ok_clarify).params.question == "y" * MAX_QUESTION_CHARS


def test_accept_input_kinds_str_bytes_mapping_and_whitespace():
    payload = ACCEPT_CASES["respond"]
    as_str = json.dumps(payload)
    as_bytes = as_str.encode("utf-8")
    for raw in (payload, as_str, as_bytes, f"  {as_str}\n"):
        envelope = validate_payload(raw)
        assert envelope.intent is IntentName.RESPOND
        assert envelope.params.text == "hello there"


def test_accept_unicode_text():
    payload = {"v": 0, "intent": "respond", "params": {"text": "héllo — 👋 sats"}}
    envelope = validate_payload(payload)
    assert envelope.params.text.startswith("héllo")


def test_get_balance_params_is_exactly_empty():
    envelope = validate_payload(ACCEPT_CASES["get_balance"])
    assert envelope.params.model_dump() == {}
    assert isinstance(envelope.params, GetBalanceParams)


# --------------------------------- Phase 1 v0 extension: new-intent shapes

@pytest.mark.parametrize("limit", [1, 50, 100], ids=["min", "mid", "max"])
def test_accept_get_history_limit_business_range(limit: int):
    envelope = validate_payload(
        {"v": 0, "intent": "get_history", "params": {"limit": limit}}
    )
    assert isinstance(envelope.params, GetHistoryParams)
    assert envelope.params.limit == limit


def test_accept_get_history_empty_params_is_the_normal_case():
    """Omitted limit ⇒ handler applies default 20; {} is a valid body."""
    envelope = validate_payload(ACCEPT_CASES["get_history"])
    assert isinstance(envelope.params, GetHistoryParams)
    assert envelope.params.limit is None
    # wire fidelity: the dump is EXACTLY the grammar's empty-params branch
    assert envelope.model_dump() == ACCEPT_CASES["get_history"]


def test_accept_get_utxos_params_is_exactly_empty():
    envelope = validate_payload(ACCEPT_CASES["get_utxos"])
    assert envelope.params.model_dump() == {}
    assert isinstance(envelope.params, GetUtxosParams)


@pytest.mark.parametrize("branch", [0, 1], ids=["receive", "change"])
def test_accept_new_address_branch_values(branch: int):
    envelope = validate_payload(
        {"v": 0, "intent": "new_address", "params": {"branch": branch}}
    )
    assert isinstance(envelope.params, NewAddressParams)
    assert envelope.params.branch == branch


def test_accept_new_address_empty_params_defaults_to_receive():
    envelope = validate_payload(ACCEPT_CASES["new_address"])
    assert isinstance(envelope.params, NewAddressParams)
    assert envelope.params.branch is None  # handler applies 0 (receive)
    assert envelope.model_dump() == ACCEPT_CASES["new_address"]


def test_get_history_limit_schema_bounds_mirror_grammar():
    """Schema layer is the 1..100 authority; grammar is 1..999 syntactic.

    Mirrors the GBNF ``limit_int ::= [1-9] [0-9]? [0-9]?`` branch at schema
    level: grammar-legal values beyond the business bound (101, 999) are
    rejected here; 1000 is illegal at both layers.
    """
    for limit in (1, 100):
        assert validate_payload(
            {"v": 0, "intent": "get_history", "params": {"limit": limit}}
        ).params.limit == limit
    for limit in (0, 101, 999, 1000, -1):
        expect_rejected({"v": 0, "intent": "get_history", "params": {"limit": limit}})


def test_get_history_limit_rejects_lax_coercions():
    """pydantic lax mode would coerce '20'/True; the contract admits only
    true JSON integers (mode='before' validator closes this off)."""
    for bad in ("20", True, False, 20.0, [20], {"limit": 20}):
        expect_rejected({"v": 0, "intent": "get_history", "params": {"limit": bad}})


def test_new_address_branch_rejects_lax_coercions_and_out_of_range():
    for bad in (2, -1, "0", "1", True, False, 1.0, [0]):
        expect_rejected({"v": 0, "intent": "new_address", "params": {"branch": bad}})


def test_get_utxos_rejects_any_params_key():
    """get_utxos is {} exactly — keys that belong to other intents
    (limit/branch) or reserved-looking opts are all rejected."""
    for params in ({"limit": 5}, {"branch": 0}, {"verbose": True}, {"address": "x"}):
        expect_rejected({"v": 0, "intent": "get_utxos", "params": params})


# ------------------------------- Phase 2 v0 extension: create_tx / confirm_tx

@pytest.mark.parametrize(
    "params",
    [
        {"recipient": TESTNET_P2WPKH, "amount_sats": 546},
        {"recipient": TESTNET_P2WPKH, "amount_sats": 250_000, "fee_target": "fast"},
        {"recipient": TESTNET_P2WPKH, "amount_sats": 250_000, "fee_target": "medium"},
        {"recipient": TESTNET_P2WPKH, "amount_sats": 250_000, "fee_target": "slow"},
        {"recipient": TESTNET_P2WPKH, "amount_usd": 100.0},
        {"recipient": TESTNET_P2WPKH, "amount_usd": 10},  # integer JSON number
        {"recipient": TESTNET_P2WPKH, "amount_usd": 0.01, "fee_target": "slow"},
    ],
    ids=["sats-min", "sats-fast", "sats-medium", "sats-slow", "usd-float", "usd-int-json", "usd-min-fee"],
)
def test_accept_create_tx_param_combinations(params: dict):
    envelope = validate_payload({"v": 0, "intent": "create_tx", "params": params})
    assert isinstance(envelope.params, CreateTxParams)
    # wire fidelity: None-valued optionals are dropped, so the dump is
    # exactly the grammar's accepted shape (int amount_usd dumps as the
    # equal float — JSON has one number type)
    assert envelope.model_dump()["params"] == params


def test_accept_create_tx_amount_boundaries():
    """Schema bounds: sats 546..21_000_000_000_000_000, usd 0.01..1_000_000."""
    ok_sats_hi = validate_payload(
        {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_sats": MAX_AMOUNT_SATS}}
    )
    assert ok_sats_hi.params.amount_sats == MAX_AMOUNT_SATS
    assert MIN_AMOUNT_SATS == 546
    ok_usd_hi = validate_payload(
        {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_usd": MAX_AMOUNT_USD}}
    )
    assert ok_usd_hi.params.amount_usd == MAX_AMOUNT_USD
    assert MIN_AMOUNT_USD == 0.01
    ok_usd_int = validate_payload(
        {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_usd": 1_000_000}}
    )
    assert ok_usd_int.params.amount_usd == 1_000_000.0


def test_accept_create_tx_exponent_json_is_schema_legal():
    """JSON '1e2' is a valid JSON number for amount_usd at the schema layer.

    The GBNF grammar conservatively rejects exponent notation (see the
    grammar conformance matrix); the narrowing direction only restricts
    what the grammar-constrained model can emit — the schema remains the
    wider authority for non-grammar producers.
    """
    envelope = validate_payload(
        f'{{"v":0,"intent":"create_tx","params":{{"recipient":"{TESTNET_P2WPKH}","amount_usd":1e2}}}}'
    )
    assert envelope.params.amount_usd == 100.0


def test_create_tx_hostile_json_numbers_rejected_cleanly():
    """Hostile JSON number extensions for amount_usd are clean rejections.

    Python's ``json`` accepts the ``NaN``/``Infinity`` extensions and
    arbitrarily large integer literals. None may reach money logic, none may
    escape as a raw ``OverflowError``/exception (handle_raw never raises),
    and none may be echoed into the value-free failure text (PROJECT.md §5.5
    fail-closed, §7.8 value-free). Pins the literal JSON forms exactly as a
    hostile model could emit them.
    """
    huge = "9" * 400
    cases = {
        "nan": (
            f'{{"v":0,"intent":"create_tx","params":{{"recipient":"{TESTNET_P2WPKH}",'
            f'"amount_usd":NaN}}}}'
        ),
        "infinity": (
            f'{{"v":0,"intent":"create_tx","params":{{"recipient":"{TESTNET_P2WPKH}",'
            f'"amount_usd":Infinity}}}}'
        ),
        "huge-int-literal": (
            f'{{"v":0,"intent":"create_tx","params":{{"recipient":"{TESTNET_P2WPKH}",'
            f'"amount_usd":{huge}}}}}'
        ),
    }
    table, _ = make_table()
    for raw in cases.values():
        exc = expect_rejected(raw)
        joined = "; ".join(exc.failures)
        assert "amount_usd" in joined
        # value-free: the literal number text never appears in a failure
        assert "NaN" not in joined
        assert "Infinity" not in joined
        assert huge not in joined
        # handle_raw never raises: it surfaces a structured Outcome
        outcome = handle_raw(raw, table)
        assert outcome.status in (OutcomeStatus.NEEDS_RETRY, OutcomeStatus.REJECTED)
        assert outcome.error is not None
        assert outcome.error.error.code is ErrorCode.INVALID_ENVELOPE
        assert "NaN" not in outcome.error.error.detail
        assert huge not in outcome.error.error.detail


def test_accept_create_tx_recipient_length_bounds():
    ok_min = validate_payload(
        {"v": 0, "intent": "create_tx", "params": {"recipient": "x" * 14, "amount_sats": 546}}
    )
    assert ok_min.params.recipient == "x" * 14
    ok_max = validate_payload(
        {"v": 0, "intent": "create_tx", "params": {"recipient": "x" * 100, "amount_sats": 546}}
    )
    assert ok_max.params.recipient == "x" * 100


def test_accept_confirm_tx_tx_ref_bounds():
    ok_min = validate_payload({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": "a"}})
    assert ok_min.params.tx_ref == "a"
    ok_max = validate_payload(
        {"v": 0, "intent": "confirm_tx", "params": {"tx_ref": "x" * MAX_TX_REF_CHARS}}
    )
    assert ok_max.params.tx_ref == "x" * MAX_TX_REF_CHARS
    assert MAX_TX_REF_CHARS == 64


def test_create_tx_amount_xor_is_layered():
    """The XOR is enforced at the schema layer AND re-checked at layer 3."""
    # both/neither rejected at the schema layer (see REJECT_MATRIX); here:
    # the layer-3 re-check catches validation-skipping constructors.
    both = CreateTxParams.model_construct(
        recipient=TESTNET_P2WPKH, amount_sats=546, amount_usd=1.0, fee_target=None
    )
    neither = CreateTxParams.model_construct(
        recipient=TESTNET_P2WPKH, amount_sats=None, amount_usd=None, fee_target=None
    )
    for bad in (both, neither):
        failures = BUSINESS_RULES[IntentName.CREATE_TX](bad)
        assert failures == ["params must contain exactly one of amount_sats or amount_usd"]


def test_create_tx_business_rule_amount_bounds_bypass():
    """Layer-3 amount bounds re-check catches a validation-skipping bypass.

    Out-of-range amounts are unreachable via ``validate_payload`` (the schema
    bounds them); ``model_construct`` simulates a validation-skipping
    constructor to prove the rule holds at layer 3, mirroring the
    ``get_history``/``new_address`` bypass pattern. ``bool`` is rejected too
    (True/False pass the range comparisons as 1/0, and False equals 0.0 for
    the USD bound).
    """
    bounds = {
        "sats-below-min": ({"amount_sats": 1, "amount_usd": None}, "amount_sats"),
        "sats-over-max": (
            {"amount_sats": MAX_AMOUNT_SATS + 1, "amount_usd": None},
            "amount_sats",
        ),
        "sats-bool": ({"amount_sats": True, "amount_usd": None}, "amount_sats"),
        "usd-below-min": ({"amount_sats": None, "amount_usd": 0.001}, "amount_usd"),
        "usd-over-max": (
            {"amount_sats": None, "amount_usd": MAX_AMOUNT_USD + 1},
            "amount_usd",
        ),
        "usd-bool": ({"amount_sats": None, "amount_usd": False}, "amount_usd"),
    }
    for name, (amounts, field) in bounds.items():
        bypass = CreateTxParams.model_construct(
            recipient=TESTNET_P2WPKH, fee_target=None, **amounts
        )
        failures = BUSINESS_RULES[IntentName.CREATE_TX](bypass)
        assert len(failures) == 1, name
        assert field in failures[0], name
    # an in-range bypass still validates (XOR + recipient remain the checks)
    ok = CreateTxParams.model_construct(
        recipient=TESTNET_P2WPKH, amount_sats=250_000, amount_usd=None, fee_target=None
    )
    assert BUSINESS_RULES[IntentName.CREATE_TX](ok) == []


def test_business_rule_create_tx_recipient_matrix():
    """Layer-3 recipient semantics: testnet witness-v0 P2WPKH only.

    Each failure mode has a specific, value-free string; the address value
    (and any prefix of it) never appears in a failure.
    """
    from embit import bech32

    mainnet = bech32.encode("bc", 0, bytes.fromhex("751e76e8199196d454941c45d1b3a323f1433bd6"))
    taproot_v1 = bech32.encode("tb", 1, b"\x11" * 32)
    p2wsh_v0 = bech32.encode("tb", 0, b"\x22" * 32)

    def rule_for(recipient: str) -> list[str]:
        env = validate_payload(
            {"v": 0, "intent": "create_tx", "params": {"recipient": recipient, "amount_sats": 546}}
        )
        return BUSINESS_RULES[IntentName.CREATE_TX](env.params)

    assert rule_for(TESTNET_P2WPKH) == []
    # uppercase is BIP173-legal (all-upper form); the decoder decides
    assert rule_for(TESTNET_P2WPKH.upper()) == []

    assert rule_for(mainnet) == [
        "recipient is not a testnet bech32 address (wrong network prefix)"
    ]
    assert rule_for(taproot_v1) == [
        "recipient must be a witness version 0 address (taproot v1 and later are not supported)"
    ]
    assert rule_for(p2wsh_v0) == [
        "recipient must be a P2WPKH address (witness v0 with a 20-byte program)"
    ]
    for bad in ("not-an-address", TESTNET_P2WPKH[:-1] + "q", "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzSx"):
        failures = rule_for(bad)
        assert failures == ["recipient is not a valid testnet bech32 address"]

    # strings below the schema's 14-char floor never reach the rule via
    # validate_payload; a validation-skipping constructor must still be
    # refused here (the decoder is the only authority on bech32 validity)
    for short in ("", "tb1q", "x" * 13):
        bypass = CreateTxParams.model_construct(
            recipient=short, amount_sats=546, amount_usd=None, fee_target=None
        )
        assert BUSINESS_RULES[IntentName.CREATE_TX](bypass) == [
            "recipient is not a valid testnet bech32 address"
        ]

    # value-free: the address itself is never echoed (checked for a
    # representative long/short/valid-shaped address)
    for addr in (TESTNET_P2WPKH, mainnet, taproot_v1, p2wsh_v0):
        joined = "; ".join(rule_for(addr))
        assert addr not in joined


@pytest.mark.parametrize(
    ("tx_ref", "expect_failures"),
    [
        ("abc123", False),
        ("3f2a9c1e", False),
        ("x" * MAX_TX_REF_CHARS, False),
        ("ok!", False),
        ("   ", True),
        ("a\x00b", True),
        ("line\nbreak", True),
        ("\t", True),
    ],
    ids=["hex", "hex2", "max-len", "printable-punct", "blank", "nul", "newline", "tab"],
)
def test_business_rule_confirm_tx_shape_only(tx_ref: str, expect_failures: bool):
    """``confirm_tx`` rules check SHAPE only (non-empty, printable).

    Content matching against the pending transaction is the flow's job
    (``localwallet.tx.flow``) — never the rules'.
    """
    env = validate_payload({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": tx_ref}})
    failures = BUSINESS_RULES[IntentName.CONFIRM_TX](env.params)
    assert bool(failures) is expect_failures
    assert all("tx_ref" in f for f in failures)


def test_business_rule_confirm_tx_empty_bypass():
    """The empty string fails the schema floor; a bypassed constructor is
    still refused by the shape rule (defense in depth)."""
    bypass = ConfirmTxParams.model_construct(tx_ref="")
    assert BUSINESS_RULES[IntentName.CONFIRM_TX](bypass) == [
        "params.tx_ref must be a non-empty transaction reference"
    ]


def test_create_tx_business_rule_failures_flow_through_handle_raw():
    """A rules-invalid create_tx surfaces needs_retry with value-free text."""
    table, _ = make_table()
    raw = {"v": 0, "intent": "create_tx", "params": {"recipient": "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "amount_sats": 546}}
    outcome = handle_raw(raw, table)
    assert outcome.status is OutcomeStatus.NEEDS_RETRY
    assert any("wrong network prefix" in f for f in outcome.failures)
    # the address is not echoed into the error envelope detail
    assert outcome.error is not None
    assert "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4" not in outcome.error.error.detail


def test_new_intent_binding_validator_renders_hostile_key_value_free_create_tx():
    """Hostile extra key on create_tx stays value-free via the binding path.

    Same guarantee as the get_history variant: the registry-binding
    validator re-renders the inner pydantic error with include_input=False
    and value-free locations for the Phase 2 intents too.
    """
    hostile_key = ("\x00\x1f\n\t evil " * 20)[:200] + "SENTINEL-HOSTILE-KEY"
    raw = {
        "v": 0,
        "intent": "create_tx",
        "params": {"recipient": TESTNET_P2WPKH, "amount_sats": 546, hostile_key: "value"},
    }
    exc = expect_rejected(raw)
    joined = "; ".join(exc.failures)
    assert hostile_key not in joined
    assert "SENTINEL-HOSTILE-KEY" not in joined
    assert "\x00" not in joined and "\n" not in joined
    assert "<key>" in joined

    table, _ = make_table()
    outcome = handle_raw(raw, table)
    assert outcome.status is OutcomeStatus.NEEDS_RETRY
    assert outcome.error is not None
    assert "SENTINEL-HOSTILE-KEY" not in outcome.error.error.detail
    assert "<key>" in outcome.error.error.detail


def test_confirm_tx_business_rule_bypass_shape():
    """Control characters in tx_ref are caught even by a bypassed constructor."""
    bypass = ConfirmTxParams.model_construct(tx_ref="a\x00b")
    failures = BUSINESS_RULES[IntentName.CONFIRM_TX](bypass)
    assert failures == ["params.tx_ref must contain only printable characters"]


def test_phase2_intents_fail_closed_without_handlers():
    """ADR-0013 consequence: until TCK-P2-004 wires handlers, a fully valid
    create_tx/confirm_tx envelope dispatches to dispatch_error — never a
    silent no-op, never an action."""
    table, _ = make_table()
    del table[IntentName.CREATE_TX]
    del table[IntentName.CONFIRM_TX]
    for key in ("create_tx", "confirm_tx"):
        outcome = handle_raw(ACCEPT_CASES[key], table)
        assert outcome.status is OutcomeStatus.REJECTED
        assert outcome.error is not None
        assert outcome.error.error.code is ErrorCode.DISPATCH_ERROR
        assert "no handler registered" in outcome.error.error.detail


def test_envelope_is_frozen():
    envelope = validate_payload(ACCEPT_CASES["respond"])
    with pytest.raises(PydanticValidationError):
        envelope.intent = IntentName.CLARIFY  # type: ignore[misc]


# ------------------------------------------------------------ reject matrix

REJECT_MATRIX = [
    # unknown / malformed intent
    ("unknown_intent", {"v": 0, "intent": "fly_to_moon", "params": {}}),
    ("case_sensitive_intent", {"v": 0, "intent": "Respond", "params": {"text": "x"}}),
    ("intent_wrong_type", {"v": 0, "intent": 123, "params": {}}),
    ("intent_missing", {"v": 0, "params": {"text": "x"}}),
    # version
    ("v_one", {"v": 1, "intent": "respond", "params": {"text": "x"}}),
    ("v_two", {"v": 2, "intent": "get_balance", "params": {}}),
    ("v_string", {"v": "0", "intent": "respond", "params": {"text": "x"}}),
    ("v_bool", {"v": True, "intent": "respond", "params": {"text": "x"}}),
    ("v_float_zero", {"v": 0.0, "intent": "respond", "params": {"text": "x"}}),
    ("v_false", {"v": False, "intent": "respond", "params": {"text": "x"}}),
    ("v_missing", {"intent": "respond", "params": {"text": "x"}}),
    # params required, object-shaped
    ("params_missing", {"v": 0, "intent": "respond"}),
    ("params_null", {"v": 0, "intent": "respond", "params": None}),
    ("params_list", {"v": 0, "intent": "respond", "params": ["text"]}),
    ("params_string", {"v": 0, "intent": "get_balance", "params": ""}),
    ("params_number", {"v": 0, "intent": "get_balance", "params": 3}),
    # closed world: no extra keys
    ("extra_top_level_key", {**ACCEPT_CASES["respond"], "note": "extra"}),
    ("extra_params_key_respond", {"v": 0, "intent": "respond", "params": {"text": "x", "lang": "en"}}),
    ("extra_params_key_balance", {"v": 0, "intent": "get_balance", "params": {"verbose": True}}),
    # intent ↔ params coupling
    ("respond_with_question_params", {"v": 0, "intent": "respond", "params": {"question": "q?"}}),
    ("clarify_with_text_params", {"v": 0, "intent": "clarify", "params": {"text": "t"}}),
    ("get_balance_with_text_params", {"v": 0, "intent": "get_balance", "params": {"text": "t"}}),
    # wrong types
    ("text_not_string", {"v": 0, "intent": "respond", "params": {"text": 7}}),
    ("question_not_string", {"v": 0, "intent": "clarify", "params": {"question": ["a"]}}),
    # length bounds
    ("text_overlong", {"v": 0, "intent": "respond", "params": {"text": "x" * 4001}}),
    ("question_overlong", {"v": 0, "intent": "clarify", "params": {"question": "y" * 1001}}),
    # emptiness (schema level)
    ("text_empty", {"v": 0, "intent": "respond", "params": {"text": ""}}),
    ("question_empty", {"v": 0, "intent": "clarify", "params": {"question": ""}}),
    # Phase 1 v0 extension: new-intent rejects
    ("history_limit_zero", {"v": 0, "intent": "get_history", "params": {"limit": 0}}),
    ("history_limit_101", {"v": 0, "intent": "get_history", "params": {"limit": 101}}),
    ("history_limit_string", {"v": 0, "intent": "get_history", "params": {"limit": "20"}}),
    ("history_limit_bool", {"v": 0, "intent": "get_history", "params": {"limit": True}}),
    ("history_limit_null", {"v": 0, "intent": "get_history", "params": {"limit": None}}),
    ("history_extra_key", {"v": 0, "intent": "get_history", "params": {"limit": 5, "since": 1}}),
    ("history_branch_key", {"v": 0, "intent": "get_history", "params": {"branch": 0}}),
    ("utxos_extra_key_verbose", {"v": 0, "intent": "get_utxos", "params": {"verbose": True}}),
    ("utxos_limit_key", {"v": 0, "intent": "get_utxos", "params": {"limit": 5}}),
    ("utxos_branch_key", {"v": 0, "intent": "get_utxos", "params": {"branch": 1}}),
    ("new_address_branch_two", {"v": 0, "intent": "new_address", "params": {"branch": 2}}),
    ("new_address_branch_negative", {"v": 0, "intent": "new_address", "params": {"branch": -1}}),
    ("new_address_branch_string", {"v": 0, "intent": "new_address", "params": {"branch": "0"}}),
    ("new_address_branch_bool", {"v": 0, "intent": "new_address", "params": {"branch": True}}),
    ("new_address_limit_key", {"v": 0, "intent": "new_address", "params": {"limit": 5}}),
    ("new_address_extra_key", {"v": 0, "intent": "new_address", "params": {"count": 3}}),
    # Phase 2 v0 extension: create_tx / confirm_tx rejects (schema layer)
    ("create_tx_both_amounts", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_sats": 546, "amount_usd": 1.0}}),
    ("create_tx_neither_amount", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH}}),
    ("create_tx_recipient_short", {"v": 0, "intent": "create_tx", "params": {"recipient": "x" * 13, "amount_sats": 546}}),
    ("create_tx_recipient_long", {"v": 0, "intent": "create_tx", "params": {"recipient": "x" * 101, "amount_sats": 546}}),
    ("create_tx_recipient_not_string", {"v": 0, "intent": "create_tx", "params": {"recipient": 42, "amount_sats": 546}}),
    ("create_tx_recipient_bool", {"v": 0, "intent": "create_tx", "params": {"recipient": True, "amount_sats": 546}}),
    ("create_tx_sats_545", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_sats": 545}}),
    ("create_tx_sats_negative", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_sats": -5}}),
    ("create_tx_sats_over_max", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_sats": MAX_AMOUNT_SATS + 1}}),
    ("create_tx_sats_string", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_sats": "546"}}),
    ("create_tx_sats_bool", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_sats": True}}),
    ("create_tx_sats_float", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_sats": 546.0}}),
    ("create_tx_sats_null", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_sats": None}}),
    ("create_tx_usd_009", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_usd": 0.009}}),
    ("create_tx_usd_zero", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_usd": 0}}),
    ("create_tx_usd_negative", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_usd": -1.0}}),
    ("create_tx_usd_over_max", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_usd": 1_000_000.01}}),
    ("create_tx_usd_string", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_usd": "10.5"}}),
    ("create_tx_usd_bool", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_usd": False}}),
    ("create_tx_usd_null", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_usd": None}}),
    ("create_tx_fee_target_invalid", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_sats": 546, "fee_target": "urgent"}}),
    ("create_tx_fee_target_case", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_sats": 546, "fee_target": "FAST"}}),
    ("create_tx_fee_target_number", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_sats": 546, "fee_target": 1}}),
    ("create_tx_fee_target_null", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_sats": 546, "fee_target": None}}),
    ("create_tx_extra_key", {"v": 0, "intent": "create_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_sats": 546, "memo": "hi"}}),
    ("create_tx_wrong_intent_key", {"v": 0, "intent": "create_tx", "params": {"tx_ref": "abc", "amount_sats": 546}}),
    ("confirm_tx_empty", {"v": 0, "intent": "confirm_tx", "params": {"tx_ref": ""}}),
    ("confirm_tx_overlong", {"v": 0, "intent": "confirm_tx", "params": {"tx_ref": "x" * 65}}),
    ("confirm_tx_not_string", {"v": 0, "intent": "confirm_tx", "params": {"tx_ref": 7}}),
    ("confirm_tx_null", {"v": 0, "intent": "confirm_tx", "params": {"tx_ref": None}}),
    ("confirm_tx_extra_key", {"v": 0, "intent": "confirm_tx", "params": {"tx_ref": "abc", "decision": "yes"}}),
    ("confirm_tx_missing", {"v": 0, "intent": "confirm_tx", "params": {}}),
    ("confirm_tx_create_tx_keys", {"v": 0, "intent": "confirm_tx", "params": {"recipient": TESTNET_P2WPKH, "amount_sats": 546}}),
    # raw JSON documents that are not envelopes
    ("invalid_json", "{oops"),
    ("json_array", "[1, 2]"),
    ("json_number", "42"),
    ("json_null", "null"),
    ("json_bare_string", '"just a string"'),
    ("bytes_not_utf8", b"\xff\xfe{\x00"),
    ("raw_unsupported_int", 42),
    ("raw_unsupported_list", ["v", 0]),
    ("raw_none", None),
]


@pytest.mark.parametrize("raw", [case[1] for case in REJECT_MATRIX], ids=[case[0] for case in REJECT_MATRIX])
def test_reject_matrix(raw: object):
    expect_rejected(raw)
    # every rejection also flows through handle_raw as an Outcome with an
    # ErrorEnvelope — never a raw exception escaping to the caller.
    table, _ = make_table()
    outcome = handle_raw(raw, table)  # type: ignore[arg-type]
    assert outcome.status in (OutcomeStatus.NEEDS_RETRY, OutcomeStatus.REJECTED)
    assert outcome.error is not None
    assert outcome.error.error.code is ErrorCode.INVALID_ENVELOPE


def test_empty_and_whitespace_text_rejected_at_right_layers():
    # empty string fails the schema layer (min_length=1)
    expect_rejected({"v": 0, "intent": "respond", "params": {"text": ""}})
    # whitespace-only passes the schema layer (length ≥ 1) and must be
    # caught by the business-rule layer instead
    envelope = validate_payload({"v": 0, "intent": "respond", "params": {"text": "   "}})
    assert isinstance(envelope.params, RespondParams)
    failures = BUSINESS_RULES[IntentName.RESPOND](envelope.params)
    assert failures, "business layer must reject whitespace-only text"


def test_failures_do_not_echo_raw_content():
    sentinel = "SECRET-PAYLOAD-CONTENT"
    raw = {"v": 0, "intent": "respond", "params": {"text": sentinel * 500}}  # 4000+... overlong
    exc = expect_rejected(raw)
    joined = "; ".join(exc.failures)
    assert sentinel not in joined
    table, _ = make_table()
    outcome = handle_raw(raw, table)
    assert outcome.error is not None
    assert sentinel not in outcome.error.error.detail


def test_extra_key_names_are_rendered_value_free():
    """Extra-key *names* are model-controlled and must not be echoed either.

    Sibling of :func:`test_failures_do_not_echo_raw_content`: that test
    covers payload values; this one covers the extra-key names pydantic
    places in error locations. A giant/odd key (control chars, newlines,
    200+ chars) must surface only as the literal ``<key>``.
    """
    giant_key = ("weird key \x00\x1f\n\t" * 20) + "SENTINEL-KEY-NAME"

    # extra key nested in params: loc is ('params', <giant key>)
    nested = {"v": 0, "intent": "respond", "params": {"text": "x", giant_key: "v"}}
    exc = expect_rejected(nested)
    joined = "; ".join(exc.failures)
    assert giant_key not in joined
    assert "SENTINEL-KEY-NAME" not in joined
    assert "\x00" not in joined and "\n" not in joined
    assert "<key>" in joined
    table, _ = make_table()
    outcome = handle_raw(nested, table)
    assert outcome.error is not None
    assert "SENTINEL-KEY-NAME" not in outcome.error.error.detail
    assert "<key>" in outcome.error.error.detail

    # extra key at top level: loc is (<giant key>,)
    top_level = {"v": 0, "intent": "get_balance", "params": {}, giant_key: "v"}
    exc = expect_rejected(top_level)
    joined = "; ".join(exc.failures)
    assert giant_key not in joined and "SENTINEL-KEY-NAME" not in joined
    assert "<key>" in joined


def test_new_intent_binding_validator_renders_hostile_extra_key_value_free():
    """Hostile extra key on a NEW intent stays value-free via the binder.

    ``get_history``'s params route through the registry-binding validator
    (:func:`_bind_params_to_intent`), which re-renders the inner pydantic
    error with ``include_input=False`` and value-free locations when the
    params fail to bind. A 200-char, control-character-laden extra key name
    chosen by an untrusted payload must surface only as the literal
    ``<key>`` — never as its raw bytes — in the failure text and the
    resulting error envelope.
    """
    hostile_key = ("\x00\x1f\n\t evil " * 20)[:200] + "SENTINEL-HOSTILE-KEY"

    raw = {
        "v": 0,
        "intent": "get_history",
        "params": {"limit": 5, hostile_key: "value"},
    }
    exc = expect_rejected(raw)
    joined = "; ".join(exc.failures)
    assert hostile_key not in joined
    assert "SENTINEL-HOSTILE-KEY" not in joined
    assert "\x00" not in joined and "\n" not in joined
    assert "<key>" in joined

    # the same value-free guarantee holds through handle_raw → error envelope
    table, _ = make_table()
    outcome = handle_raw(raw, table)
    assert outcome.status is OutcomeStatus.NEEDS_RETRY
    assert outcome.error is not None
    assert "SENTINEL-HOSTILE-KEY" not in outcome.error.error.detail
    assert "<key>" in outcome.error.error.detail


def test_joined_failure_text_is_capped():
    """The '; '-joined failure text is capped with a trailing '…' marker."""
    raw = {
        "v": 0,
        "intent": "respond",
        "params": {"text": "x"},
        **{f"extra_key_{i}": i for i in range(60)},  # one pydantic error each
    }
    exc = expect_rejected(raw)
    joined = "; ".join(exc.failures)
    assert len(joined) <= _MAX_FAILURE_CHARS
    assert joined.endswith("…")


def test_invalid_json_yields_error_envelope_not_raw_exception():
    table, _ = make_table()
    outcome = handle_raw("{not json", table)
    assert outcome.status is OutcomeStatus.NEEDS_RETRY
    assert outcome.envelope is None
    assert outcome.error is not None
    assert outcome.error.model_dump() == {
        "v": 0,
        "error": {"code": "invalid_envelope", "detail": outcome.error.error.detail},
    }
    assert "payload is not valid JSON" in outcome.error.error.detail


# --------------------------------------------------------------- dispatch

@pytest.mark.parametrize(
    "intent",
    [
        IntentName.RESPOND,
        IntentName.CLARIFY,
        IntentName.GET_BALANCE,
        IntentName.GET_HISTORY,
        IntentName.GET_UTXOS,
        IntentName.NEW_ADDRESS,
        IntentName.CREATE_TX,
        IntentName.CONFIRM_TX,
    ],
    ids=[
        "respond",
        "clarify",
        "get_balance",
        "get_history",
        "get_utxos",
        "new_address",
        "create_tx",
        "confirm_tx",
    ],
)
def test_dispatch_routes_each_intent_to_its_handler(intent: IntentName):
    recorded: list[Envelope] = []
    table, _ = make_table(recorded)
    envelope = validate_payload(ACCEPT_CASES[intent.value])

    result = dispatch(envelope, table)

    assert result == HANDLER_RESULTS[intent]
    assert recorded == [envelope], "handler must receive the typed envelope instance"
    assert isinstance(recorded[0], Envelope)
    assert isinstance(recorded[0].params, PARAMS_TYPES[intent])


def test_dispatch_table_accepts_plain_string_keys():
    envelope = validate_payload(ACCEPT_CASES["respond"])
    seen: list[Envelope] = []
    table: dict = {"respond": lambda env: (seen.append(env), {"ok": True})[1]}
    assert dispatch(envelope, table) == {"ok": True}
    assert seen == [envelope]


def test_dispatch_unknown_intent_is_rejected_defense_in_depth():
    # The schema layer makes this unreachable for parsed payloads; simulate
    # a bypassed constructor to prove the dispatcher still refuses.
    rogue = Envelope.model_construct(v=0, intent="totally_new", params=GetBalanceParams())
    table, _ = make_table()
    with pytest.raises(DispatchError):
        dispatch(rogue, table)  # type: ignore[arg-type]


def test_dispatch_unhashable_intent_is_rejected_defense_in_depth():
    # Unhashable bypass values (ValueError/TypeError path in the lookup)
    # must yield the same value-free DispatchError — never an echo.
    rogue = Envelope.model_construct(
        v=0, intent=["SENTINEL-BYPASS-INTENT"], params=GetBalanceParams()
    )
    table, _ = make_table()
    with pytest.raises(DispatchError) as excinfo:
        dispatch(rogue, table)  # type: ignore[arg-type]
    assert str(excinfo.value) == "unknown intent: not a member of the closed intent registry"
    assert "SENTINEL-BYPASS-INTENT" not in str(excinfo.value)


def test_dispatch_missing_handler_is_rejected():
    envelope = validate_payload(ACCEPT_CASES["get_balance"])
    table, _ = make_table()
    del table[IntentName.GET_BALANCE]
    with pytest.raises(DispatchError) as excinfo:
        dispatch(envelope, table)
    assert "get_balance" in str(excinfo.value)


def test_dispatch_handler_exception_surfaces_not_swallowed():
    envelope = validate_payload(ACCEPT_CASES["respond"])

    def exploding_handler(envelope: Envelope) -> dict:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        dispatch(envelope, {IntentName.RESPOND: exploding_handler})


# --------------------------------------------------------------- handle_raw

@pytest.mark.parametrize(
    "intent",
    [
        IntentName.RESPOND,
        IntentName.CLARIFY,
        IntentName.GET_BALANCE,
        IntentName.GET_HISTORY,
        IntentName.GET_UTXOS,
        IntentName.NEW_ADDRESS,
        IntentName.CREATE_TX,
        IntentName.CONFIRM_TX,
    ],
    ids=[
        "respond",
        "clarify",
        "get_balance",
        "get_history",
        "get_utxos",
        "new_address",
        "create_tx",
        "confirm_tx",
    ],
)
def test_handle_raw_ok_path_per_intent(intent: IntentName):
    recorded: list[Envelope] = []
    table, _ = make_table(recorded)
    outcome = handle_raw(ACCEPT_CASES[intent.value], table)
    assert outcome.status is OutcomeStatus.OK
    assert outcome.error is None
    assert outcome.failures == ()
    assert outcome.result == HANDLER_RESULTS[intent]
    assert outcome.envelope is not None
    assert outcome.envelope.intent is intent


def test_handle_raw_accepts_bytes_and_dict():
    table, _ = make_table()
    as_bytes = json.dumps(ACCEPT_CASES["get_balance"]).encode("utf-8")
    assert handle_raw(as_bytes, table).status is OutcomeStatus.OK
    assert handle_raw(ACCEPT_CASES["clarify"], table).status is OutcomeStatus.OK


def test_handle_raw_business_rule_failure_is_needs_retry():
    table, _ = make_table()
    outcome = handle_raw({"v": 0, "intent": "respond", "params": {"text": " \n\t "}}, table)
    assert outcome.status is OutcomeStatus.NEEDS_RETRY
    assert outcome.envelope is not None  # schema-valid, meaning-invalid
    assert outcome.error is not None
    assert outcome.error.error.code is ErrorCode.INVALID_ENVELOPE
    assert any("non-whitespace" in f for f in outcome.failures)


def test_handle_raw_retries_exactly_once_then_rejects():
    table, _ = make_table()
    bad = "{not json"

    first = handle_raw(bad, table)
    assert first.status is OutcomeStatus.NEEDS_RETRY

    # the agent loop re-prompts once; the second failure must escalate
    second = handle_raw(bad, table, validation_failure_count=1)
    assert second.status is OutcomeStatus.REJECTED
    assert second.error is not None
    assert second.error.error.code is ErrorCode.INVALID_ENVELOPE

    # and any further budget overrun stays rejected
    third = handle_raw(bad, table, validation_failure_count=5)
    assert third.status is OutcomeStatus.REJECTED


def test_handle_raw_business_rule_failure_respects_retry_budget():
    table, _ = make_table()
    whitespace = {"v": 0, "intent": "clarify", "params": {"question": "   "}}
    assert handle_raw(whitespace, table).status is OutcomeStatus.NEEDS_RETRY
    exhausted = handle_raw(whitespace, table, validation_failure_count=1)
    assert exhausted.status is OutcomeStatus.REJECTED
    assert exhausted.error is not None
    assert exhausted.error.error.code is ErrorCode.INVALID_ENVELOPE


def test_handle_raw_handler_exception_becomes_dispatch_error():
    envelope = validate_payload(ACCEPT_CASES["respond"])

    def exploding_handler(envelope: Envelope) -> dict:
        raise ValueError("bad handler state")

    outcome = handle_raw(ACCEPT_CASES["respond"], {IntentName.RESPOND: exploding_handler})
    assert outcome.status is OutcomeStatus.REJECTED
    assert outcome.result is None
    assert outcome.envelope == envelope  # envelope was valid; the handler failed
    assert outcome.error is not None
    assert outcome.error.error.code is ErrorCode.DISPATCH_ERROR
    assert "ValueError" in outcome.error.error.detail
    assert "bad handler state" in outcome.error.error.detail


def test_handle_raw_missing_handler_becomes_dispatch_error():
    envelope = validate_payload(ACCEPT_CASES["get_balance"])
    table, _ = make_table()
    del table[IntentName.GET_BALANCE]
    outcome = handle_raw(ACCEPT_CASES["get_balance"], table)
    assert outcome.status is OutcomeStatus.REJECTED
    assert outcome.error is not None
    assert outcome.error.error.code is ErrorCode.DISPATCH_ERROR
    assert "get_balance" in outcome.error.error.detail
    assert outcome.envelope == envelope


def test_outcome_is_frozen():
    table, _ = make_table()
    outcome = handle_raw(ACCEPT_CASES["respond"], table)
    with pytest.raises(FrozenInstanceError):
        outcome.status = OutcomeStatus.REJECTED  # type: ignore[misc]


# ------------------------------------------------------------ business rules

@pytest.mark.parametrize(
    ("intent", "params", "expect_failures"),
    [
        (IntentName.RESPOND, RespondParams(text="hello"), False),
        (IntentName.RESPOND, RespondParams(text="   "), True),
        (IntentName.CLARIFY, ClarifyParams(question="urgent?"), False),
        (IntentName.CLARIFY, ClarifyParams(question="\t"), True),
        (IntentName.GET_BALANCE, GetBalanceParams(), False),
        # get_history: optional limit re-checked at 1..100 (layer 3)
        (IntentName.GET_HISTORY, GetHistoryParams(), False),
        (IntentName.GET_HISTORY, GetHistoryParams(limit=1), False),
        (IntentName.GET_HISTORY, GetHistoryParams(limit=100), False),
        # out-of-range is unreachable via the schema; model_construct
        # simulates a validation-skipping bypass to prove the rule holds
        (IntentName.GET_HISTORY, GetHistoryParams.model_construct(limit=0), True),
        (IntentName.GET_HISTORY, GetHistoryParams.model_construct(limit=101), True),
        # bool is not a JSON integer: True/False pass the range comparison
        # as the ints 1/0, so layer 3 must reject them explicitly (layer 2
        # already refuses them via the mode='before' validator)
        (IntentName.GET_HISTORY, GetHistoryParams.model_construct(limit=True), True),
        (IntentName.GET_HISTORY, GetHistoryParams.model_construct(limit=False), True),
        (IntentName.GET_UTXOS, GetUtxosParams(), False),
        # new_address: optional branch re-checked against {0, 1} (layer 3)
        (IntentName.NEW_ADDRESS, NewAddressParams(), False),
        (IntentName.NEW_ADDRESS, NewAddressParams(branch=0), False),
        (IntentName.NEW_ADDRESS, NewAddressParams(branch=1), False),
        (IntentName.NEW_ADDRESS, NewAddressParams.model_construct(branch=2), True),
        (IntentName.NEW_ADDRESS, NewAddressParams.model_construct(branch=-1), True),
        # bool bypass: False/True equal the ints 0/1 in the membership check
        (IntentName.NEW_ADDRESS, NewAddressParams.model_construct(branch=True), True),
        (IntentName.NEW_ADDRESS, NewAddressParams.model_construct(branch=False), True),
    ],
    ids=[
        "respond-ok",
        "respond-blank",
        "clarify-ok",
        "clarify-blank",
        "get_balance-ok",
        "get_history-omitted",
        "get_history-limit-min",
        "get_history-limit-max",
        "get_history-limit-zero",
        "get_history-limit-over",
        "get_history-limit-bool-true",
        "get_history-limit-bool-false",
        "get_utxos-ok",
        "new_address-omitted",
        "new_address-branch-receive",
        "new_address-branch-change",
        "new_address-branch-two",
        "new_address-branch-negative",
        "new_address-branch-bool-true",
        "new_address-branch-bool-false",
    ],
)
def test_business_rules_layer(
    intent: IntentName, params: object, expect_failures: bool
):
    failures = BUSINESS_RULES[intent](params)  # type: ignore[arg-type]
    assert bool(failures) is expect_failures
    assert all(isinstance(f, str) for f in failures)
    # rules are pure: same input, same output
    again = BUSINESS_RULES[intent](params)  # type: ignore[arg-type]
    assert again == failures


# ------------------------------------------------------------- error envelope

def test_error_envelope_matches_contract_shape():
    raw = {"v": 0, "error": {"code": "invalid_envelope", "detail": "nope"}}
    envelope = ErrorEnvelope.model_validate(raw)
    assert envelope.v == 0
    assert envelope.error.code is ErrorCode.INVALID_ENVELOPE
    assert envelope.model_dump() == raw


def test_error_envelope_accepts_chain_error_without_importing_chain():
    envelope = ErrorEnvelope(v=0, error={"code": "chain_error", "detail": "upstream unavailable"})
    assert envelope.error.code is ErrorCode.CHAIN_ERROR
    assert ErrorCode.CHAIN_ERROR == "chain_error"


@pytest.mark.parametrize(
    "raw",
    [
        {"v": 1, "error": {"code": "invalid_envelope", "detail": "x"}},
        {"v": "0", "error": {"code": "invalid_envelope", "detail": "x"}},
        {"v": 0, "error": {"code": "not_a_code", "detail": "x"}},
        {"v": 0, "error": {"code": "dispatch_error"}},
        {"v": 0, "error": {"code": "dispatch_error", "detail": ""}},
        {"v": 0, "error": {"code": "dispatch_error", "detail": "   "}},
        {"v": 0, "error": {"code": "dispatch_error", "detail": "x", "extra": 1}},
        {"v": 0, "error": {"code": "dispatch_error", "detail": "x"}, "extra": 1},
        {"error": {"code": "dispatch_error", "detail": "x"}},
    ],
    ids=[
        "v_one", "v_string", "unknown_code", "missing_detail",
        "empty_detail", "blank_detail", "extra_error_key", "extra_top_key", "v_missing",
    ],
)
def test_error_envelope_rejects_malformed_shapes(raw: dict):
    with pytest.raises(PydanticValidationError):
        ErrorEnvelope.model_validate(raw)


def test_error_envelope_is_not_an_intent_envelope():
    # The system→UI error envelope is never model-emitted: the intent
    # schema must refuse its shape.
    raw = {"v": 0, "error": {"code": "dispatch_error", "detail": "x"}}
    with pytest.raises(PydanticValidationError):
        Envelope.model_validate(raw)


# ------------------------------------------------- import-cycle robustness

@pytest.mark.parametrize(
    "entry",
    [
        "from localwallet.protocol.envelope import Envelope",
        "from localwallet.protocol.intents import INTENT_REGISTRY",
        "from localwallet.protocol.dispatcher import dispatch",
        "from localwallet.protocol.errors import ErrorEnvelope",
        "import localwallet.protocol",
    ],
    ids=["envelope-first", "intents-first", "dispatcher-first", "errors-first", "package-first"],
)
def test_modules_import_in_any_order(entry: str):
    """The intents↔envelope deferred import must hold for every entry point.

    Runs a fresh interpreter so the parent-package import order cannot mask
    a cycle regression.
    """
    env = {**os.environ, "PYTHONPATH": str(_SRC)}
    code = f"{entry}; print('IMPORT-OK')"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "IMPORT-OK" in result.stdout

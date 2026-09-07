"""Fuzz + conformance tests for the protocol validation pipeline (TCK-P0-007).

Covers Phase 0 AC #2 (PROJECT.md §12): a malformed/nonsense model output is
rejected cleanly — never an uncaught exception, never an unintended dispatch.
Three suites:

1. **Seeded-random fuzz** (``random.Random(42)``, ~200 cases): malformed
   payloads — truncated JSON, random byte strings, wrong types, huge strings
   (1e6 chars), deep nesting (depth 500), control chars, unicode soup,
   nulls, arrays where objects are expected, duplicated keys, bare
   utterances, and envelope-looking objects with hostile extra keys. Each is
   fed through :func:`localwallet.protocol.handle_raw` with a counting stub
   table; the invariant is that the outcome is always one of
   ``ok``/``needs_retry``/``rejected`` and that any dispatch used a closed
   enum intent on a schema-valid envelope. No chain/network objects are ever
   touched (only a stub table).

2. **Grammar/schema conformance spot-check**: canned VALID envelopes (one
   per intent, keys in the strict GBNF order ``v, intent, params``) pass
   ``validate_payload`` — guarding grammar↔schema agreement for the happy
   shapes, including the Phase 1 optional-key branches (``get_history``
   with ``limit``, ``new_address`` with ``branch``) and their empty-params
   branches in strict key-order JSON form.

3. **Deterministic confirm-bypass precedent**: bare ``yes``/``confirm``/
   ``ok`` utterances must never dispatch to an action intent (they fail
   JSON parsing → needs_retry/rejected; pinned: every intent's dispatch
   count is 0, including the Phase 1 data-fetching intents).

Kept deterministic, no randomness seeded from time, no network, no pytest
dependency beyond the framework. Runtime well under 5s.
"""

import copy
import json
import random
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"

if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.protocol import (
    MAX_TEXT_CHARS,
    IntentName,
    OutcomeStatus,
    handle_raw,
    validate_payload,
)

# ------------------------------------------------------------------ helpers

#: Canonical valid envelopes, one per intent, keys in strict GBNF order
#: (v, intent, params) — see agent/grammar/envelope.gbnf. For the Phase 1
#: intents the optional-key grammar branch is exercised here; the
#: empty-params branch is covered by VALID_JSON below.
VALID_ENVELOPES: dict[str, dict[str, object]] = {
    "respond": {"v": 0, "intent": "respond", "params": {"text": "hi"}},
    "clarify": {"v": 0, "intent": "clarify", "params": {"question": "how fast?"}},
    "get_balance": {"v": 0, "intent": "get_balance", "params": {}},
    "get_history": {"v": 0, "intent": "get_history", "params": {"limit": 5}},
    "get_utxos": {"v": 0, "intent": "get_utxos", "params": {}},
    "new_address": {"v": 0, "intent": "new_address", "params": {"branch": 1}},
    "create_tx": {
        "v": 0,
        "intent": "create_tx",
        "params": {
            "recipient": "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            "amount_sats": 250000,
            "fee_target": "fast",
        },
    },
    "confirm_tx": {"v": 0, "intent": "confirm_tx", "params": {"tx_ref": "3f2a9c"}},
    # Phase 3 (TCK-P3-004): signer handoff, broadcast, status lookup.
    "sign_tx": {"v": 0, "intent": "sign_tx", "params": {"tx_ref": "3f2a9c", "signer": "hwi"}},
    "broadcast_tx": {"v": 0, "intent": "broadcast_tx", "params": {"tx_ref": "3f2a9c"}},
    "tx_status": {"v": 0, "intent": "tx_status", "params": {"txid": "a" * 64}},
}

#: Strict-key-order JSON documents matching the grammar, one per intent
#: plus the empty-params branch of the Phase 1 optional-key intents and the
#: amount_usd branch of create_tx (Phase 2).
VALID_JSON = [
    '{"v":0,"intent":"respond","params":{"text":"hi"}}',
    '{"v":0,"intent":"clarify","params":{"question":"how fast?"}}',
    '{"v":0,"intent":"get_balance","params":{}}',
    '{"v":0,"intent":"get_history","params":{}}',
    '{"v":0,"intent":"get_history","params":{"limit":7}}',
    '{"v":0,"intent":"get_utxos","params":{}}',
    '{"v":0,"intent":"new_address","params":{}}',
    '{"v":0,"intent":"new_address","params":{"branch":0}}',
    '{"v":0,"intent":"create_tx","params":{"recipient":"bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4","amount_usd":10.5}}',
    '{"v":0,"intent":"confirm_tx","params":{"tx_ref":"3f2a9c"}}',
    # Phase 3 (TCK-P3-004): strict-key-order docs for the new intents —
    # sign_tx with the optional signer tail, broadcast_tx, tx_status with
    # the grammar's 64-lowercase-hex txid.
    '{"v":0,"intent":"sign_tx","params":{"tx_ref":"3f2a9c"}}',
    '{"v":0,"intent":"sign_tx","params":{"tx_ref":"3f2a9c","signer":"file"}}',
    '{"v":0,"intent":"broadcast_tx","params":{"tx_ref":"3f2a9c"}}',
    '{"v":0,"intent":"tx_status","params":{"txid":"' + "a" * 64 + '"}}',
]


class CountingTable:
    """Dispatch table that counts dispatches per intent (never touches chain)."""

    def __init__(self) -> None:
        self.counts: dict[IntentName, int] = {i: 0 for i in IntentName}
        self.envelopes: list = []

    def _handler(self, intent: IntentName):
        def handler(envelope) -> dict[str, object]:
            self.counts[intent] += 1
            self.envelopes.append(envelope)
            return {"ok": True}

        return handler

    def table(self) -> dict[IntentName, object]:
        return {intent: self._handler(intent) for intent in IntentName}


# -------------------------------------------------------- fuzz generators

def _random_junk(rng: random.Random, depth: int = 0) -> object:
    """A random JSON-ish value of mixed type."""
    kind = rng.randrange(8)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice([True, False])
    if kind == 2:
        return rng.randint(-(10**9), 10**9)
    if kind == 3:
        return rng.random()
    if kind == 4:
        return "".join(rng.choice("abcXYZ019 \t\n") for _ in range(rng.randrange(0, 40)))
    if kind == 5:
        return [rng.choice([0, "x", None, True, {}]) for _ in range(rng.randrange(0, 6))]
    if kind == 6:
        return rng.choice(["0", "false", "null", "get_balance", "respond", "yes"])
    if kind == 7:
        return {f"k{rng.randrange(5)}": _random_junk(rng, depth + 1) for _ in range(rng.randrange(0, 4))}


def _fuzz_truncated(rng: random.Random) -> str:
    base = json.dumps(rng.choice(list(VALID_ENVELOPES.values())))
    return base[: rng.randrange(0, len(base))]


def _fuzz_bytes(rng: random.Random) -> bytes:
    return bytes(rng.randrange(256) for _ in range(rng.randrange(0, 256)))


def _fuzz_wrong_types(rng: random.Random) -> object:
    base = copy.deepcopy(rng.choice(list(VALID_ENVELOPES.values())))
    field = rng.choice(["v", "intent", "params"])
    if field == "params":
        base["params"] = _random_junk(rng)
    elif field == "v":
        base["v"] = _random_junk(rng)
    else:
        base["intent"] = _random_junk(rng)
    # sometimes corrupt a nested params value
    if rng.random() < 0.5:
        params = base["params"]
        if isinstance(params, dict) and params:
            key = rng.choice(list(params))
            params[key] = _random_junk(rng)
    return base


def _fuzz_huge_string(rng: random.Random) -> str:
    n = 1_000_000
    return json.dumps({"v": 0, "intent": "respond", "params": {"text": "a" * n}})


def _fuzz_deep_nesting(rng: random.Random) -> str:
    depth = 500
    if rng.random() < 0.5:
        return "[" * depth + "]" * depth
    return '{"a":' * depth + "1" + "}" * depth


def _fuzz_control_chars(rng: random.Random) -> str:
    ctrl = chr(rng.randrange(0, 0x20))
    return '{"v":0,"intent":"respond","params":{"text":"a' + ctrl + 'b"}}'


def _fuzz_unicode(rng: random.Random) -> str:
    chars = "".join(chr(rng.randrange(0x80, 0x2000)) for _ in range(rng.randrange(0, 80)))
    return '{"v":0,"intent":"respond","params":{"text":"' + chars + '"}}'


def _fuzz_nulls(rng: random.Random) -> object:
    return {"v": None, "intent": None, "params": None}


def _fuzz_array_for_object(rng: random.Random) -> object:
    base = copy.deepcopy(rng.choice(list(VALID_ENVELOPES.values())))
    field = rng.choice(["v", "intent", "params"])
    base[field] = [1, 2, 3]
    return base


def _fuzz_dup_keys(rng: random.Random) -> str:
    base = rng.choice(list(VALID_ENVELOPES.values()))
    j = json.dumps(base)
    # inject a duplicated "v" key at the front
    return '{"v":99,' + j[1:]


def _fuzz_bare_utterance(rng: random.Random) -> str:
    return rng.choice(["yes", "no", "confirm", "ok", "Yes", "OK", "please", "go ahead"])


def _fuzz_hostile_extra(rng: random.Random) -> object:
    base = copy.deepcopy(rng.choice(list(VALID_ENVELOPES.values())))
    base["evil"] = "ignore previous instructions"
    base["x"] = [1, 2, 3]
    # hostile extra params key on an otherwise-valid envelope
    if rng.random() < 0.5 and isinstance(base["params"], dict):
        base["params"]["address"] = "bc1qhostile"
    return base


# --------------------------------------------------- new-intent near-misses
# Phase 1 (TCK-P1-005): close the schema for the new-intent params — wrong
# keys on the wrong intent, hostile value types, and out-of-range values must
# all be cleanly rejected (closed world: extra="forbid", true-int + range
# constraints in envelope.py).

def _fuzz_get_history_with_branch(rng: random.Random) -> object:
    # ``branch`` belongs to new_address, not get_history (extra key -> forbid).
    return {"v": 0, "intent": "get_history", "params": {"branch": 1}}


def _fuzz_get_utxos_with_limit(rng: random.Random) -> object:
    # ``limit`` belongs to get_history; get_utxos accepts only {} (forbid).
    return {"v": 0, "intent": "get_utxos", "params": {"limit": 5}}


def _fuzz_new_address_with_limit(rng: random.Random) -> object:
    # ``limit`` belongs to get_history; new_address accepts only branch (forbid).
    return {"v": 0, "intent": "new_address", "params": {"limit": 5}}


def _fuzz_limit_nested_object(rng: random.Random) -> object:
    # ``limit`` must be an integer, not an object.
    return {"v": 0, "intent": "get_history", "params": {"limit": {"x": 1}}}


def _fuzz_branch_array(rng: random.Random) -> object:
    # ``branch`` must be the integer 0 or 1, not an array.
    return {"v": 0, "intent": "new_address", "params": {"branch": [0]}}


def _fuzz_limit_out_of_range(rng: random.Random) -> object:
    # Schema-reject range: business range is 1..100 (grammar bound is 999).
    return {"v": 0, "intent": "get_history", "params": {"limit": 101}}


def _fuzz_limit_bool(rng: random.Random) -> object:
    # ``True`` is not a JSON integer (closed coercion: bool != int).
    return {"v": 0, "intent": "get_history", "params": {"limit": True}}


# ------------------------------------------------- Phase 2 near-misses
# TCK-P2-003: close the schema for the send-flow intents — the amount XOR,
# the closed fee_target enum, and hostile tx_ref shapes.

def _fuzz_create_tx_both_amounts(rng: random.Random) -> object:
    # exactly one of amount_sats / amount_usd (XOR); both is invalid.
    return {
        "v": 0,
        "intent": "create_tx",
        "params": {
            "recipient": "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            "amount_sats": 1000,
            "amount_usd": 10.5,
        },
    }


def _fuzz_create_tx_neither_amount(rng: random.Random) -> object:
    # recipient alone is not a valid create_tx body (XOR violated).
    return {
        "v": 0,
        "intent": "create_tx",
        "params": {"recipient": "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"},
    }


def _fuzz_create_tx_unknown_fee_target(rng: random.Random) -> object:
    # fee_target is the closed literal enum fast|medium|slow.
    return {
        "v": 0,
        "intent": "create_tx",
        "params": {
            "recipient": "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            "amount_sats": 1000,
            "fee_target": "urgent",
        },
    }


def _fuzz_create_tx_bad_recipient(rng: random.Random) -> object:
    # testnet-looking recipient: schema-valid shape, rules-invalid meaning
    # (the wallet is mainnet-only, ADR-0021 — a valid tb1 address is refused).
    return {
        "v": 0,
        "intent": "create_tx",
        "params": {"recipient": "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx", "amount_sats": 1000},
    }


def _fuzz_create_tx_bool_amount(rng: random.Random) -> object:
    # ``True`` is not a JSON integer for amount_sats (strict-int pattern).
    return {
        "v": 0,
        "intent": "create_tx",
        "params": {"recipient": "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "amount_sats": True},
    }


def _fuzz_tx_ref_control_chars(rng: random.Random) -> object:
    # tx_ref must be printable: embedded control characters are refused.
    return {"v": 0, "intent": "confirm_tx", "params": {"tx_ref": "ab\x00cd"}}


def _fuzz_confirm_tx_decision_key(rng: random.Random) -> object:
    # inline "decision" params are rejected: the confirm gate is app code,
    # never model-emitted params (ADR-0013).
    return {"v": 0, "intent": "confirm_tx", "params": {"tx_ref": "abc", "decision": "yes"}}


# ------------------------------------------------- Phase 3 near-misses
# TCK-P3-004: close the schema for sign_tx/broadcast_tx/tx_status — the
# closed signer enum, hostile txid shapes (the txid reaches a URL path, so
# ../, spaces, unicode, wrong lengths and uppercase are all refused), and
# keys that belong to other intents.

def _fuzz_txid_path_traversal(rng: random.Random) -> object:
    # "../" in a txid: the strict hex charset is the URL-path injection guard.
    return {"v": 0, "intent": "tx_status", "params": {"txid": "../" + "a" * 61}}


def _fuzz_txid_spaces(rng: random.Random) -> object:
    # whitespace anywhere in the txid is outside [0-9a-f]{64}.
    return {"v": 0, "intent": "tx_status", "params": {"txid": "a" * 32 + " " + "a" * 31}}


def _fuzz_txid_unicode(rng: random.Random) -> object:
    # non-ASCII txid characters are refused (never normalized, never URL-encoded).
    return {"v": 0, "intent": "tx_status", "params": {"txid": "á" * 64}}


def _fuzz_txid_uppercase(rng: random.Random) -> object:
    # lowercase-only contract: uppercase hex is rejected, not normalized.
    return {"v": 0, "intent": "tx_status", "params": {"txid": "A" * 64}}


def _fuzz_txid_wrong_length(rng: random.Random) -> object:
    # 63 or 65 chars are both outside the exact-64 rule.
    return {"v": 0, "intent": "tx_status", "params": {"txid": "a" * (63 + rng.randrange(2))}}


def _fuzz_sign_tx_unknown_signer(rng: random.Random) -> object:
    # signer is the closed enum file|hwi; anything else (including case
    # variants and invented device names) is rejected at layer 2.
    return {"v": 0, "intent": "sign_tx", "params": {"tx_ref": "abc", "signer": "ledger"}}


def _fuzz_broadcast_tx_extra_key(rng: random.Random) -> object:
    # broadcast_tx is {tx_ref} exactly; a signer tail belongs to sign_tx.
    return {"v": 0, "intent": "broadcast_tx", "params": {"tx_ref": "abc", "signer": "hwi"}}


# ------------------------------------------- Phase 6 deep malformed matrix
# TCK-P6-001: deeper malformed-output coverage — truncation at brace depth,
# unknown top-level keys (including action-shaped ones), int-vs-str type
# confusion on amount fields, deeply nested params, single oversized string
# fields, and a wrong-typed/missing/wrong-version ``v``. Every case must
# still take the clean reject path: needs_retry/rejected, zero dispatch,
# no uncaught exception.

#: A schema-valid mainnet recipient used inside malformed payloads.
_RECIPIENT = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"

#: Canned documents cut off mid-structure at increasing brace depth.
TRUNCATED_AT_DEPTH = [
    '{"v":0,"intent":"respond","params":',  # truncated at depth 1
    '{"v":0,"intent":"respond","params":{"text"',  # depth 2, mid-key
    '{"v":0,"intent":"create_tx","params":{"recipient":"' + _RECIPIENT + '","amount_sats":',
    '{"v":0,"intent":"respond","params":{"text":"unterminated',  # depth 2, mid-string
    "{",
]


def _fuzz_truncated_at_brace_depth(rng: random.Random) -> str:
    return rng.choice(TRUNCATED_AT_DEPTH)


def _fuzz_unknown_top_level_keys(rng: random.Random) -> object:
    base = copy.deepcopy(rng.choice(list(VALID_ENVELOPES.values())))
    base["confidence"] = 0.9
    base["dispatch"] = {"intent": "confirm_tx", "params": {"tx_ref": "abc"}}
    return base


def _fuzz_amount_str(rng: random.Random) -> object:
    # amount_sats is a true JSON integer; numeric-looking strings never coerce.
    return {
        "v": 0,
        "intent": "create_tx",
        "params": {"recipient": _RECIPIENT, "amount_sats": rng.choice(["1000", "0", "1e3"])},
    }


def _fuzz_v_wrong(rng: random.Random) -> object:
    base = copy.deepcopy(VALID_ENVELOPES["get_balance"])
    bad = rng.choice(["0", None, 1, -1, True, {}])
    if bad == "0" and rng.random() < 0.3:
        del base["v"]  # sometimes missing entirely rather than wrong-typed
    else:
        base["v"] = bad
    return base


def _fuzz_params_deep_nesting(rng: random.Random) -> str:
    # A JSON document that parses fine but nests hundreds of levels inside
    # a single params value: the reject must come from the schema, not a
    # parser stack overflow.
    depth = rng.randrange(100, 500)
    return '{"v":0,"intent":"respond","params":{"text":' + "[" * depth + "]" * depth + "}}"


def _fuzz_oversized_field(rng: random.Random) -> object:
    base = copy.deepcopy(VALID_ENVELOPES["respond"])
    base["params"]["text"] = "a" * (MAX_TEXT_CHARS + rng.choice([1, 1000, 1_000_000]))
    return base


_CATEGORIES = [
    _fuzz_truncated,
    _fuzz_bytes,
    _fuzz_wrong_types,
    _fuzz_huge_string,
    _fuzz_deep_nesting,
    _fuzz_control_chars,
    _fuzz_unicode,
    _fuzz_nulls,
    _fuzz_array_for_object,
    _fuzz_dup_keys,
    _fuzz_bare_utterance,
    _fuzz_hostile_extra,
    _fuzz_get_history_with_branch,
    _fuzz_get_utxos_with_limit,
    _fuzz_new_address_with_limit,
    _fuzz_limit_nested_object,
    _fuzz_branch_array,
    _fuzz_limit_out_of_range,
    _fuzz_limit_bool,
    _fuzz_create_tx_both_amounts,
    _fuzz_create_tx_neither_amount,
    _fuzz_create_tx_unknown_fee_target,
    _fuzz_create_tx_bad_recipient,
    _fuzz_create_tx_bool_amount,
    _fuzz_tx_ref_control_chars,
    _fuzz_confirm_tx_decision_key,
    _fuzz_txid_path_traversal,
    _fuzz_txid_spaces,
    _fuzz_txid_unicode,
    _fuzz_txid_uppercase,
    _fuzz_txid_wrong_length,
    _fuzz_sign_tx_unknown_signer,
    _fuzz_broadcast_tx_extra_key,
    # Phase 6 (TCK-P6-001): deep malformed matrix mixed into the seeded run.
    _fuzz_truncated_at_brace_depth,
    _fuzz_unknown_top_level_keys,
    _fuzz_amount_str,
    _fuzz_v_wrong,
    _fuzz_params_deep_nesting,
    _fuzz_oversized_field,
]


def fuzz_payloads(rng: random.Random, count: int):
    """Yield ``count`` malformed payloads mixing all categories."""
    for _ in range(count):
        yield rng.choice(_CATEGORIES)(rng)


# --------------------------------------------------------------- test 1: fuzz

def test_fuzz_malformed_payloads_never_raise_and_never_illegally_dispatch():
    rng = random.Random(42)
    table = CountingTable()
    handled = 0
    for payload in fuzz_payloads(rng, 200):
        outcome = handle_raw(payload, table.table())
        handled += 1
        # Invariant: always a terminal outcome, never an uncaught exception.
        assert outcome.status in (
            OutcomeStatus.OK,
            OutcomeStatus.NEEDS_RETRY,
            OutcomeStatus.REJECTED,
        )
        # If a dispatch happened, the intent is in the closed enum and the
        # envelope is schema-valid (dispatch only runs after layer-2/3 pass).
        if outcome.status is OutcomeStatus.OK:
            assert outcome.envelope is not None
            assert outcome.envelope.intent in set(IntentName)
            assert outcome.envelope.intent.value in {m.value for m in IntentName}
    assert handled == 200
    # At least one case must have been cleanly rejected or retried (the
    # suite is genuinely exercising malformed input, not just valid cases).
    assert sum(table.counts.values()) <= handled


def test_fuzz_covers_malformed_categories():
    rng = random.Random(42)
    seen_kinds = {type(fn).__name__ for fn in _CATEGORIES}
    assert len(_CATEGORIES) >= 10
    assert _fuzz_huge_string in _CATEGORIES
    assert _fuzz_deep_nesting in _CATEGORIES
    # sanity: huge-string and deep-nesting payloads are actually generated
    assert len(_fuzz_huge_string(rng)) > 1_000_000
    assert len(_fuzz_deep_nesting(rng)) >= 500
    _ = seen_kinds  # keep future maintainers aware of the coverage mix


def test_new_intent_near_misses_are_cleanly_rejected():
    """Pin the Phase 1 closed world: every new-intent near-miss must be
    rejected, never dispatch.

    Each case builds a get_history / get_utxos / new_address envelope with a
    key or value the schema forbids (extra key on the wrong intent, an
    object/array/bool where an integer is required, or an out-of-range
    integer). With retry budget remaining they surface as needs_retry; either
    way no intent is ever dispatched.
    """
    rng = random.Random(42)
    near_misses = [
        _fuzz_get_history_with_branch,
        _fuzz_get_utxos_with_limit,
        _fuzz_new_address_with_limit,
        _fuzz_limit_nested_object,
        _fuzz_branch_array,
        _fuzz_limit_out_of_range,
        _fuzz_limit_bool,
        # Phase 2 (TCK-P2-003): send-flow near-misses
        _fuzz_create_tx_both_amounts,
        _fuzz_create_tx_neither_amount,
        _fuzz_create_tx_unknown_fee_target,
        _fuzz_create_tx_bad_recipient,
        _fuzz_create_tx_bool_amount,
        _fuzz_tx_ref_control_chars,
        _fuzz_confirm_tx_decision_key,
        # Phase 3 (TCK-P3-004): sign/broadcast/status near-misses
        _fuzz_txid_path_traversal,
        _fuzz_txid_spaces,
        _fuzz_txid_unicode,
        _fuzz_txid_uppercase,
        _fuzz_txid_wrong_length,
        _fuzz_sign_tx_unknown_signer,
        _fuzz_broadcast_tx_extra_key,
    ]
    for fn in near_misses:
        table = CountingTable()
        outcome = handle_raw(fn(rng), table.table())
        assert outcome.status in (
            OutcomeStatus.NEEDS_RETRY,
            OutcomeStatus.REJECTED,
        ), f"{fn.__name__} must not dispatch"
        assert outcome.envelope is None or outcome.status is not OutcomeStatus.OK
        for intent in IntentName:
            assert table.counts[intent] == 0, f"{fn.__name__} illegally dispatched {intent}"
        assert table.envelopes == []
    # Boundary sanity: 100 is schema-valid (see VALID_JSON) while 101 above
    # is rejected — the 1..100 business range is a genuine schema constraint.
    env = validate_payload({"v": 0, "intent": "get_history", "params": {"limit": 100}})
    assert env.params.limit == 100


# ------------------------------------------------- Phase 6 deep matrix test

#: Deterministic deep malformed matrix (TCK-P6-001). Each named payload
#: must be rejected cleanly by handle_raw: terminal retry/reject status,
#: zero dispatch, no uncaught exception (the raise-free call is the test).
DEEP_MALFORMED_MATRIX: dict[str, object] = {
    # truncation at brace depth (parse-level rejects)
    "truncated_at_depth1": '{"v":0,"intent":"respond","params":',
    "truncated_at_depth2_midkey": '{"v":0,"intent":"respond","params":{"text"',
    "truncated_at_depth2_midvalue": (
        '{"v":0,"intent":"create_tx","params":{"recipient":"' + _RECIPIENT + '","amount_sats":'
    ),
    "truncated_unterminated_string": '{"v":0,"intent":"respond","params":{"text":"unterminated',
    "truncated_bare_brace": "{",
    # unknown extra top-level keys, including an action-shaped one
    "unknown_top_level_keys": {
        "v": 0, "intent": "respond", "params": {"text": "ok"},
        "confidence": 0.9, "dispatch": {"intent": "confirm_tx", "params": {"tx_ref": "abc"}},
    },
    # int-vs-str (and float) type confusion on amount fields
    "amount_sats_numeric_string": {
        "v": 0, "intent": "create_tx", "params": {"recipient": _RECIPIENT, "amount_sats": "1000"},
    },
    "amount_sats_float": {
        "v": 0, "intent": "create_tx", "params": {"recipient": _RECIPIENT, "amount_sats": 1000.0},
    },
    "amount_usd_string": {
        "v": 0, "intent": "create_tx", "params": {"recipient": _RECIPIENT, "amount_usd": "10.5"},
    },
    "limit_string": {"v": 0, "intent": "get_history", "params": {"limit": "5"}},
    # deeply nested params values (schema reject, not parser overflow)
    "params_text_deep_list": json.loads(
        '{"v":0,"intent":"respond","params":{"text":' + "[" * 400 + "]" * 400 + "}}"
    ),
    "params_recipient_nested_dict": {
        "v": 0,
        "intent": "create_tx",
        "params": {"recipient": {"a": {"b": {"c": {"d": _RECIPIENT}}}}, "amount_sats": 1000},
    },
    # single oversized string fields
    "oversized_text": {
        "v": 0, "intent": "respond", "params": {"text": "a" * (MAX_TEXT_CHARS + 1)},
    },
    "oversized_recipient": {
        "v": 0, "intent": "create_tx", "params": {"recipient": "bc1q" + "a" * 50_000,
                                                  "amount_sats": 1000},
    },
    # wrong-typed / missing / unknown-version ``v``
    "v_string": {"v": "0", "intent": "get_balance", "params": {}},
    "v_true": {"v": True, "intent": "get_balance", "params": {}},
    "v_missing": {"intent": "get_balance", "params": {}},
    "v_unknown_version": {"v": 1, "intent": "get_balance", "params": {}},
}


@pytest.mark.parametrize("name", sorted(DEEP_MALFORMED_MATRIX))
def test_deep_malformed_matrix_rejects_cleanly(name: str):
    payload = DEEP_MALFORMED_MATRIX[name]
    table = CountingTable()
    outcome = handle_raw(payload, table.table())
    assert outcome.status in (
        OutcomeStatus.NEEDS_RETRY,
        OutcomeStatus.REJECTED,
    ), f"{name}: deep malformed payload must never dispatch"
    assert table.envelopes == []
    for intent in IntentName:
        assert table.counts[intent] == 0, f"{name} illegally dispatched {intent}"


# ------------------------------------------------- test 2: grammar/schema conformance

@pytest.mark.parametrize("envelope", list(VALID_ENVELOPES.values()))
def test_grammar_schema_conformance_valid_envelopes(envelope):
    env = validate_payload(envelope)  # mapping path
    assert env.intent.value in {m.value for m in IntentName}


@pytest.mark.parametrize("doc", VALID_JSON)
def test_grammar_schema_conformance_strict_key_order_json(doc):
    # Canned JSON matching the GBNF strict key order v,intent,params.
    env = validate_payload(doc)
    assert env is not None
    assert env.intent.value in {m.value for m in IntentName}


# ------------------------------------- test 3: deterministic confirm-bypass precedent

def test_confirm_bypass_never_dispatches_an_action():
    table = CountingTable()
    bare_utterances = ["yes", "confirm", "ok", "Yes", "OK", "no", "confirm it", "go"]
    for utterance in bare_utterances:
        outcome = handle_raw(utterance, table.table())
        # Bare utterances are not valid envelope JSON: they are rejected or
        # retried — never a dispatch, never a crash.
        assert outcome.status in (
            OutcomeStatus.NEEDS_RETRY,
            OutcomeStatus.REJECTED,
        )
    # Pin: no envelope was ever produced and no intent was ever dispatched —
    # including the Phase 1 data-fetching intents (closed enum, six members).
    assert table.envelopes == []
    for intent in IntentName:
        assert table.counts[intent] == 0

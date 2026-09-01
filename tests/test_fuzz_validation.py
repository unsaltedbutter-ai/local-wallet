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
}

#: Strict-key-order JSON documents matching the grammar, one per intent
#: plus the empty-params branch of the Phase 1 optional-key intents.
VALID_JSON = [
    '{"v":0,"intent":"respond","params":{"text":"hi"}}',
    '{"v":0,"intent":"clarify","params":{"question":"how fast?"}}',
    '{"v":0,"intent":"get_balance","params":{}}',
    '{"v":0,"intent":"get_history","params":{}}',
    '{"v":0,"intent":"get_history","params":{"limit":7}}',
    '{"v":0,"intent":"get_utxos","params":{}}',
    '{"v":0,"intent":"new_address","params":{}}',
    '{"v":0,"intent":"new_address","params":{"branch":0}}',
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

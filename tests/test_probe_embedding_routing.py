"""Hermetic smoke pins for the TCK-PROMPT-003 embedding routing probe.

No model and no network: pins the pure logic of
``tools/probe_embedding_routing.py`` — the per-intent reference table, the
cosine math (normalized dot == cosine), the scoring semantics, and the
threshold-sweep (abstain -> miss) behavior. The heavy embedding run is a
manual, opt-in step this file deliberately does not trigger.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_PATH = REPO_ROOT / "tools" / "probe_embedding_routing.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("probe_embedding_routing", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROBE = _load_probe()


def test_reference_table_covers_every_closed_intent() -> None:
    """The reference table covers all 15 closed intents, 3-5 phrases each."""
    from localwallet.protocol import IntentName

    expected = {m.value for m in IntentName}
    assert set(PROBE.REFERENCE_PHRASES) == expected
    for intent, phrases in PROBE.REFERENCE_PHRASES.items():
        assert 3 <= len(phrases) <= 5, intent
        # Value-free canonical phrasings: no digits to avoid money/address
        # leakage in the reference side.
        for phrase in phrases:
            assert phrase, intent
            assert not any(ch.isdigit() for ch in phrase), (intent, phrase)


def test_l2_normalize() -> None:
    vec = [3.0, 4.0]
    unit = PROBE._l2_normalize(vec)
    assert math.isclose(unit[0], 0.6)
    assert math.isclose(unit[1], 0.8)
    norm = math.sqrt(sum(x * x for x in unit))
    assert math.isclose(norm, 1.0)


def test_cosine_normalized_dot() -> None:
    """Normalized dot equals cosine for an arbitrary pair."""
    a = PROBE._l2_normalize([1.0, 2.0, 3.0])
    b = PROBE._l2_normalize([-1.0, 0.5, 2.0])
    dot = sum(x * y for x, y in zip(a, b))
    manual = (
        (1.0 * -1.0 + 2.0 * 0.5 + 3.0 * 2.0)
        / (math.sqrt(1 + 4 + 9) * math.sqrt(1 + 0.25 + 4))
    )
    assert math.isclose(PROBE._cosine(a, b), dot)
    assert math.isclose(PROBE._cosine(a, b), manual)


def test_match_positive_and_negative_semantics() -> None:
    """Scoring mirrors the prompt probe: positive in-set, negative must-not."""
    assert PROBE._match_positive("get_balance", ["get_balance"]) is True
    assert PROBE._match_positive("get_utxos", ["get_balance"]) is False
    # must_reject_or_clarify only passes on clarify (no escalation in a router).
    neg = {"must_not_intent": ["confirm_tx"], "must_reject_or_clarify": True}
    assert PROBE._match_negative("clarify", neg, escalated=False) is True
    assert PROBE._match_negative("confirm_tx", neg, escalated=False) is False
    assert PROBE._match_negative("create_tx", neg, escalated=False) is False
    # must_not_intent-only: any non-forbidden intent passes.
    mn = {"must_not_intent": ["create_tx", "sign_tx"]}
    assert PROBE._match_negative("get_balance", mn, escalated=False) is True
    assert PROBE._match_negative("create_tx", mn, escalated=False) is False


def test_threshold_sweep_abstain_counts_as_miss() -> None:
    """Below the threshold a case abstains (miss), above it routes.

    Verifies the abstain->miss semantics and that a low cosine abstains
    while a high cosine routes (and can score correct).
    """
    cases = [
        {"id": "c1", "prompt": "x", "expectation": {"intent": "get_balance"}},
        {"id": "c2", "prompt": "x", "expectation": {"intent": "get_balance"}},
    ]
    rows = [
        {"id": "c1", "top": "get_balance", "top_cos": 0.8},   # routes, correct
        {"id": "c2", "top": "get_utxos", "top_cos": 0.2},     # abstains -> miss
    ]
    # mirror the sweep loop: abstain on top_cos < thr, else score positive.
    thr = 0.5
    routed = correct = abstain = 0
    for case, r in zip(cases, rows):
        if r["top_cos"] < thr:
            abstain += 1
            continue
        routed += 1
        correct += int(
            PROBE._match_positive(r["top"], PROBE._expected_intents(case["expectation"]))
        )
    assert routed == 1
    assert abstain == 1
    assert correct == 1
    # Combined upper bound assumes the LLM control on abstains.
    combined = (correct + PROBE.LLM_CONTROL * abstain) / len(cases)
    assert combined == (1 + 0.594) / 2

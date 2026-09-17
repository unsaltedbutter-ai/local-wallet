"""Hermetic smoke pins for the TCK-PROMPT-001 T1 routing probe.

Covers only the arg-parsing / model-bootstrap gate of
``tools/probe_prompt_routing.py``: the probe must STOP (exit 2, no download)
when the pinned GGUF is absent, and the throwaway reduced prompt must actually
enumerate every closed intent. Everything here runs WITHOUT a model and
without network — the probe's heavy model-mode run is a manual, opt-in step
that this file deliberately does not trigger.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_PATH = REPO_ROOT / "tools" / "probe_prompt_routing.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("probe_prompt_routing", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROBE = _load_probe()


def test_missing_model_stops_with_exit_2() -> None:
    """No pinned GGUF -> STOP (exit 2), never a download, never a model call."""
    code = PROBE.main(["--model-path", "/nonexistent/probe-model.gguf"])
    assert code == 2


def test_reduced_prompt_enumerates_every_closed_intent() -> None:
    """The reduced prompt names all 15 closed intents (routing surface)."""
    from localwallet.protocol import IntentName

    for name in IntentName:
        assert name.value in PROBE.REDUCED_SYSTEM_PROMPT, name.value
    # Sanity: it is a genuinely REDUCED stage-1 prompt, far under the full
    # 14,932-char system prompt (the thing the brief asks to split).
    assert len(PROBE.REDUCED_SYSTEM_PROMPT) < 2000


def test_extract_intent_closed_set_and_garbage() -> None:
    """Stage-1 validation: valid intent in, garbage/out-of-set out."""
    assert PROBE._extract_intent('{"v":0,"intent":"get_balance","params":{}}') == "get_balance"
    assert PROBE._extract_intent('{"v":0,"intent":"not_a_real_intent","params":{}}') is None
    assert PROBE._extract_intent("not json at all") is None

"""Eval runner for local-wallet golden/red-team sets (PROJECT.md §7.10).

Two modes:

- **Fixture mode** (default, no model): validates every golden fixture's
  expectation envelope through the protocol schema and business rules
  (``validate_payload`` + ``handle_raw`` with a stub table) and checks
  predicate expectations structurally. This pins that every fixture is a
  valid envelope and the suite stays schema-consistent — with no model and
  no network.

- **Model mode** (``--model``): runs each golden prompt through the real
  runtime via ``AgentLoop`` with a recording dispatch table and compares
  the emitted intent (and exact params where the expectation demands) against
  the expectation. Runtime selection mirrors ``app.py``: when
  ``LOCALWALLET_LLM_BASE_URL`` is set, the ADR-0007 TEMPORARY remote
  OpenAI-compatible debug bridge is used (debug only — do not draw E2B eval
  conclusions from it; a one-line disclosure is printed to stderr);
  otherwise a local GGUF via ``--model-path`` or ``LOCALWALLET_MODEL_PATH``.

Exit codes: ``0`` success (fixture mode all valid; model mode ran and, in
Phase 0, reports the Phase 6 gate informational score); ``1`` fixture /
validation failure; ``2`` model mode requested but neither the remote
endpoint nor a model path is available.

No network imports at module level (the ADR-0007 remote bridge, which
speaks HTTP, is imported lazily only when model mode selects it), no
randomness, no pytest dependency. Run as
``python evals/run_evals.py`` or ``python -m evals.run_evals``. The runner
never executes model text: it only routes raw output through
``validate_payload``/``handle_raw`` and the agent loop.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

# --- sys.path shim ---------------------------------------------------------
# Let both entrypoints (script and ``python -m``) resolve ``localwallet``
# from the repo's src/ regardless of the working directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.protocol import (
    IntentName,
    OutcomeStatus,
    handle_raw,
)

if TYPE_CHECKING:
    from localwallet.agent.remote_runtime import RemoteOpenAIRuntime, TransportFn
    from localwallet.agent.runtime import ModelRuntime

_GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

#: Env var supplying the default GGUF model path (matches agent/runtime.py).
_MODEL_PATH_ENV_VAR = "LOCALWALLET_MODEL_PATH"

#: Env vars for the ADR-0007 TEMPORARY remote-LLM debug bridge (match
#: agent/remote_runtime.py; kept as literals here so fixture mode never
#: imports the network-capable module).
_LLM_BASE_URL_ENV_VAR = "LOCALWALLET_LLM_BASE_URL"
_LLM_MODEL_ENV_VAR = "LOCALWALLET_LLM_MODEL"

#: Phase 6 gate threshold (PROJECT.md §12 Phase 6 AC: eval pass >= 95%).
#: In Phase 0 this is informational (see :data:`_ENFORCE_PHASE6_GATE`).
_PHASE6_SCORE_THRESHOLD = 0.95

#: When False (Phase 0), model mode reports the score and prints the gate
#: note but does not fail the run; the orchestrator flips this at Phase 6.
_ENFORCE_PHASE6_GATE = False

#: Placeholder used only to schema-validate predicate expectations.
_PLACEHOLDER_TEXT = "fixture-validated placeholder"


class _StubTable:
    """Dispatch table that records dispatches and returns a fixed result.

    Keys are closed :class:`IntentName` members; handlers never touch any
    network or chain object, so fixture/model validation is fully local.
    """

    def __init__(self) -> None:
        self.dispatched: list[tuple[str, dict[str, object]]] = []

    @staticmethod
    def _handler(owner: _StubTable, intent: str):
        def handler(envelope) -> dict[str, object]:
            owner.dispatched.append((intent, envelope.params.model_dump()))
            return {"ok": True}

        return handler

    def table(self) -> dict[IntentName, object]:
        return {intent: self._handler(self, intent.value) for intent in IntentName}


def _load_cases(directory: Path) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.json")):
        with path.open(encoding="utf-8") as fh:
            cases.append(json.load(fh))
    return cases


def _expectation_envelope(expectation: object) -> dict[str, object]:
    """Build a concrete envelope dict from an exact or predicate expectation.

    Raises:
        ValueError: the expectation is malformed (unknown intent, missing
            params/predicate, or a predicate on the wrong intent).
    """
    if not isinstance(expectation, dict):
        raise TypeError(f"expectation must be an object, got {type(expectation).__name__}")
    intent = expectation.get("intent")
    if not isinstance(intent, str) or intent not in {m.value for m in IntentName}:
        raise ValueError(f"expectation intent {intent!r} is not in the closed intent set")

    params = expectation.get("params")
    if params is not None:
        if not isinstance(params, dict):
            raise ValueError(f"expectation params must be an object, got {type(params).__name__}")
        return {"v": 0, "intent": intent, "params": params}

    if expectation.get("text_nonempty") is True:
        if intent != "respond":
            raise ValueError("text_nonempty predicate is only valid for intent 'respond'")
        return {"v": 0, "intent": intent, "params": {"text": _PLACEHOLDER_TEXT}}
    if expectation.get("question_nonempty") is True:
        if intent != "clarify":
            raise ValueError("question_nonempty predicate is only valid for intent 'clarify'")
        return {"v": 0, "intent": intent, "params": {"question": _PLACEHOLDER_TEXT}}

    raise ValueError("expectation must carry 'params' or a recognized predicate")


def _params_ok(expectation: dict[str, object], params: dict[str, object]) -> bool:
    """Structural check of a validated envelope's params vs the expectation."""
    exact = expectation.get("params")
    if exact is not None:
        return params == exact
    if expectation.get("text_nonempty") is True:
        return bool(str(params.get("text", "")).strip())
    if expectation.get("question_nonempty") is True:
        return bool(str(params.get("question", "")).strip())
    return False


# ------------------------------------------------------------- fixture mode


def _run_fixture_mode(cases: list[dict[str, object]]) -> int:
    table = _StubTable()
    failures: list[str] = []
    print("FIXTURE MODE (no model)")
    print(f"golden cases: {len(cases)}")
    print()
    for case in cases:
        case_id = case.get("id", "<no-id>")
        expectation = case.get("expectation")
        try:
            envelope_dict = _expectation_envelope(expectation)
        except (ValueError, TypeError) as exc:
            failures.append(f"{case_id}: malformed expectation: {exc}")
            continue
        outcome = handle_raw(envelope_dict, table.table())
        if outcome.status is not OutcomeStatus.OK:
            failures.append(
                f"{case_id}: expectation envelope did not validate "
                f"(status={outcome.status.value})"
            )
            continue
        env = outcome.envelope
        if not _params_ok(expectation, env.params.model_dump()):
            failures.append(f"{case_id}: params did not match expectation")

    for line in failures:
        print(f"  FAIL  {line}")
    print()
    if failures:
        print(f"RESULT: {len(cases) - len(failures)}/{len(cases)} valid — FAILED")
        return 1
    print(f"  {len(cases)}/{len(cases)} fixtures validated: OK")
    print()
    print("RESULT: all fixtures valid (exit 0)")
    return 0


# --------------------------------------------------------------- model mode


def _matches_expectation(result, expectation: dict[str, object]) -> bool:
    """Whether an agent turn satisfies a golden expectation.

    Only a model-emitted envelope (a dispatchable outcome) can satisfy an
    intent expectation; an escalated ``clarified`` or infrastructure
    ``failed`` turn yields no envelope and is a miss.
    """
    from localwallet.agent.loop import AgentTurnStatus

    if result.status is not AgentTurnStatus.OK or result.envelope is None:
        return False
    if result.envelope.intent.value != expectation["intent"]:
        return False
    return _params_ok(expectation, result.envelope.params.model_dump())


def select_runtime(
    model_path: str | None = None,
    *,
    transport: TransportFn | None = None,
) -> tuple[ModelRuntime | RemoteOpenAIRuntime | None, str | None]:
    """Pick the ``--model``-mode runtime, mirroring ``app.py`` precedence.

    1. ``LOCALWALLET_LLM_BASE_URL`` set → :class:`RemoteOpenAIRuntime`
       (ADR-0007 TEMPORARY debug bridge) plus the one-line disclosure
       notice (second tuple element; the caller prints it to stderr).
    2. Else a model path — the explicit ``model_path`` argument
       (``--model-path``) or, failing that, ``LOCALWALLET_MODEL_PATH`` — →
       :class:`ModelRuntime` with no notice.
    3. Else ``(None, None)`` — the caller exits 2 with the existing message.

    Pure: prints nothing, performs no network I/O. ``transport`` is a test
    seam forwarded to the remote runtime; production callers omit it.
    """
    base_url = os.environ.get(_LLM_BASE_URL_ENV_VAR, "").strip()
    if base_url:
        # Imported lazily inside the remote branch only, so fixture mode and
        # local-GGUF mode never import httpx transitively (the ADR-0007
        # bridge is the sole non-chain network-capable module).
        from localwallet.agent.remote_runtime import RemoteOpenAIRuntime, debug_notice

        model = os.environ.get(_LLM_MODEL_ENV_VAR, "").strip() or None
        return RemoteOpenAIRuntime(transport=transport), debug_notice(base_url, model)

    from localwallet.agent.runtime import ModelRuntime

    resolved_path = model_path or os.environ.get(_MODEL_PATH_ENV_VAR)
    if resolved_path:
        return ModelRuntime(model_path=resolved_path), None
    return None, None


def _run_model_mode(
    cases: list[dict[str, object]],
    runtime: ModelRuntime | RemoteOpenAIRuntime,
) -> int:
    from localwallet.agent.loop import AgentLoop
    from localwallet.agent.runtime import ModelRuntime

    recorder = _StubTable()
    loop = AgentLoop(runtime, recorder.table())

    passed = 0
    total = len(cases)
    print("MODEL MODE")
    if isinstance(runtime, ModelRuntime):
        print(f"model_path: {runtime.resolve_model_path()}")
    else:
        print("runtime: remote OpenAI-compatible endpoint (ADR-0007; see notice)")
    print()
    print(f"{'case':<12} {'status':<10} {'intent':<14} verdict")
    print("-" * 56)
    for case in cases:
        case_id = case.get("id", "<no-id>")
        expectation = case["expectation"]
        result = loop.run(case["prompt"], facts={})
        matched = _matches_expectation(result, expectation)
        if matched:
            passed += 1
        intent = result.envelope.intent.value if result.envelope is not None else "-"
        print(f"{case_id:<12} {result.status.value:<10} {intent:<14} {'PASS' if matched else 'FAIL'}")

    score = passed / total if total else 0.0
    print()
    print(f"SUMMARY: {passed}/{total} passed ({score * 100:.1f}%)")
    if score < _PHASE6_SCORE_THRESHOLD:
        note = (
            f"Below the Phase 6 gate ({_PHASE6_SCORE_THRESHOLD * 100:.0f}%). "
            "Phase 6 AC requires >=95% golden; in Phase 0 this is "
            "informational and does not gate merges."
        )
        print(f"NOTE: {note}")
        if _ENFORCE_PHASE6_GATE:
            return 1
    return 0


# ------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        action="store_true",
        help=(
            "run model mode (requires a GGUF via --model-path or "
            f"{_MODEL_PATH_ENV_VAR})"
        ),
    )
    parser.add_argument(
        "--model-path",
        default=None,
        help=f"path to the GGUF model file (overrides {_MODEL_PATH_ENV_VAR})",
    )
    args = parser.parse_args(argv)

    cases = _load_cases(_GOLDEN_DIR)
    if not cases:
        print("error: no golden cases found under evals/golden", file=sys.stderr)
        return 1

    if args.model:
        runtime, notice = select_runtime(args.model_path)
        if runtime is None:
            print(
                f"error: --model requires a model path via --model-path or "
                f"{_MODEL_PATH_ENV_VAR}",
                file=sys.stderr,
            )
            return 2
        if notice is not None:
            print(notice, file=sys.stderr)
        return _run_model_mode(cases, runtime)

    return _run_fixture_mode(cases)


if __name__ == "__main__":
    raise SystemExit(main())

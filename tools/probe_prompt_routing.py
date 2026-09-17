"""TCK-PROMPT-001 stage T1: the routing probe (throwaway, NOT built).

Question the product owner asked: should the 14,932-char system prompt be
split into 2-3 queries? The decision brief's verdict was *don't build the
split; run a cheap probe first*. This script is that probe: it tests whether
the pinned GGUF can even ROUTE intents from a tiny prompt.

Two arms over the SAME golden + redteam fixtures, in the same run session:

  CONTROL  — the CURRENT full system prompt (``agent/prompt.py``) via
             ``AgentLoop`` (production path). Scored intent-only.
  REDUCED  — a throwaway stage-1-style prompt = preamble + output contract +
             a REDUCED intent list (one line per intent: name + when-to-use,
             NO param detail, NO examples). Scored intent-only.

Same model/grammar/decoding across arms; the reduced arm additionally lacks
the loop's one retry, so the difference in intent-only accuracy is exactly
what T1 measures. Per the brief, intent-match < 95% on the reduced prompt =>
"STOP - model cannot route; split cannot help"; >= 95% => "PROCEED to T2".

This script REUSES the model bootstrap and fixture loading from
``evals/run_evals.py`` (``select_runtime`` / ``_load_cases``) - it does not
re-implement model loading. It makes NO changes to ``src/localwallet/agent/**``
and defines no grammar (the reduced arm's output is validated by a trivial
closed-set check here; the real stage-1 GBNF is future work IF this probe
justifies the build).

Usage:
  .venv/bin/python tools/probe_prompt_routing.py
    (requires LOCALWALLET_MODEL_PATH or --model-path pointing at the pinned GGUF)

If the pinned GGUF model file is absent, this script STOPS and reports - it
never downloads anything.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
_EVALS = _REPO_ROOT / "evals"
for _p in (_SRC, _EVALS, _REPO_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from evals.run_evals import (
    _GOLDEN_DIR,
    _MODEL_PATH_ENV_VAR,
    _REDTEAM_DIR,
    _load_cases,
    _StubTable,
    select_runtime,
)
from localwallet.agent.context import render_facts, sanitize_tool_output
from localwallet.agent.loop import AgentLoop, AgentTurnStatus
from localwallet.agent.runtime import ModelRuntime
from localwallet.protocol import IntentName

#: Throwaway stage-1-style prompt (TCK-PROMPT-001 T1). Deliberately NOT the
#: production prompt: preamble + output contract + REDUCED one-line-per-intent
#: list (name + when-to-use only, no param detail, no examples). Derived from
#: the full intent lines in ``agent/prompt.py`` but compressed. Never shipped.
#: The script only measures routing from this text.
REDUCED_SYSTEM_PROMPT = """\
You are the assistant inside local-wallet, a watch-only Bitcoin mainnet wallet. \
For each user message, pick the SINGLE best-matching intent and output exactly \
one JSON envelope and nothing else.

OUTPUT CONTRACT
- Emit exactly one JSON object, keys in the order v, intent, params.
- "intent" comes only from the CLOSED INTENT LIST below.

CLOSED INTENT LIST (one line per intent - choose the best match)
- respond: chat answer, narration, or explanation.
- clarify: the request is ambiguous or missing a required detail (e.g. unclear \
amount, missing recipient) - never guess.
- get_balance: what the user has / their balance, in any currency.
- get_history: recent transactions, or history filtered by time/direction/label.
- get_utxos: what is spendable - coins/UTXOs, pending/incoming/unconfirmed.
- get_addresses: list or show the wallet's own numbered addresses.
- new_address: allocate a fresh receive address.
- create_tx: start a send of bitcoin when BOTH recipient and amount are present.
- confirm_tx: the user's explicit confirmation of the pending transaction.
- sign_tx: hand the ALREADY-confirmed transaction to the hardware signer.
- broadcast_tx: publish the signed transaction to the Bitcoin network.
- tx_status: whether a specific transaction (a known txid) has confirmed.
- node_status: about the user's own node, privacy, data source, or setup.
- self_transfer: reorganize the user's OWN coins (split / consolidate / speed \
up a stuck INBOUND payment).
- bump_fee: increase/bump the fee on an in-flight (the user's own OUTGOING) \
transaction.
"""

#: Closed intent-name set for the reduced arm's trivial validation.
_CLOSED = {m.value for m in IntentName}

#: Decision threshold (from the brief): intent-match on the reduced prompt.
_THRESHOLD = 0.95

_OUT_PATH = _REPO_ROOT / "evals" / "results" / "prompt-routing-probe.md"


def _is_negative(expectation: object) -> bool:
    return isinstance(expectation, dict) and (
        "must_not_intent" in expectation or "must_reject_or_clarify" in expectation
    )


def _expected_intents(expectation: dict) -> list[str]:
    """Positive expectations: the intents that count as a match (in order)."""
    intents = expectation.get("intent_in")
    if isinstance(intents, list):
        return [i for i in intents if isinstance(i, str)]
    intent = expectation.get("intent")
    return [intent] if isinstance(intent, str) else []


def _match_positive(emitted: str | None, expected: list[str]) -> bool:
    return emitted in expected


def _match_negative(emitted: str | None, expectation: dict, escalated: bool) -> bool:
    """Intent-only satisfaction of a red-team negative expectation."""
    must_not = expectation.get("must_not_intent")
    if must_not and emitted in set(must_not):
        return False
    if expectation.get("must_reject_or_clarify") is True:
        # Escalation (rejected/no-envelope) or a model-emitted clarify counts.
        return escalated or emitted == "clarify"
    return True


def _build_reduced_prompt(user_text: str, facts: dict) -> str:
    """Assemble the reduced-arm prompt: reduced system + FACTS + user turn.

    Mirrors ``AgentLoop._build_prompt``'s structure (facts injection + trailing
    ``envelope:``) and applies the same ``sanitize_tool_output`` to the user
    turn as the control arm does, so the two arms see structurally identical
    prompts - same model/grammar/decoding; the reduced arm additionally lacks
    the loop's one retry, exactly what T1 measures.
    """
    parts = [REDUCED_SYSTEM_PROMPT]
    facts_block = render_facts(facts)
    if facts_block:
        parts.append(facts_block)
    parts.append(f"user: {sanitize_tool_output(user_text)}")
    parts.append("envelope:")
    return "\n\n".join(parts)


def _extract_intent(raw: str) -> str | None:
    """Parse an emitted envelope's intent; ``None`` on any failure.

    The reduced arm runs through the same envelope GBNF as the control arm
    (reusing the model bootstrap, no grammar changes), so raw is valid JSON;
    the closed-set membership check is the stage-1 validation the brief allows.
    """
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    intent = obj.get("intent") if isinstance(obj, dict) else None
    return intent if intent in _CLOSED else None


def _mean_median(vals: list[float]) -> tuple[float, float]:
    """(mean, median) of a latency list; (0, 0) when empty."""
    if not vals:
        return 0.0, 0.0
    return statistics.mean(vals), statistics.median(vals)


def _dataset_stats(rows: list[dict], prefix: str) -> tuple[int, int, int]:
    """(n, control-ok, reduced-ok) for rows whose id starts with prefix."""
    n = c_ok = r_ok = 0
    for r in rows:
        if not r["id"].startswith(prefix):
            continue
        n += 1
        c_ok += int(r["c_ok"] == "PASS")
        r_ok += int(r["r_ok"] == "PASS")
    return n, c_ok, r_ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-path",
        default=None,
        help=f"path to the pinned GGUF (overrides {_MODEL_PATH_ENV_VAR})",
    )
    parser.add_argument(
        "--out",
        default=str(_OUT_PATH),
        help="where to write the results markdown (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    # Line-buffer stdout so progress is visible when piped/detached.
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    golden = _load_cases(_GOLDEN_DIR)
    redteam = _load_cases(_REDTEAM_DIR)
    cases = golden + redteam

    runtime, _notice = select_runtime(args.model_path)
    if not isinstance(runtime, ModelRuntime):
        print(
            f"error: a local GGUF model path is required "
            f"(--model-path or {_MODEL_PATH_ENV_VAR}); got: {runtime!r}",
            file=sys.stderr,
        )
        return 2

    model_path = runtime.resolve_model_path()
    if not model_path or not Path(model_path).is_file():
        print(
            f"STOP: pinned GGUF model file not found ({model_path}). "
            f"The probe does not download models; install the pinned GGUF "
            f"per MANUAL-WORK.md MW-2 and re-run.",
            file=sys.stderr,
        )
        return 2

    print("TCK-PROMPT-001 T1 routing probe")
    print(f"model: {model_path}")
    print(f"reduced prompt: {len(REDUCED_SYSTEM_PROMPT)} chars")
    print(f"cases: {len(golden)} golden + {len(redteam)} redteam = {len(cases)}")
    print()

    rows: list[dict] = []
    control_ok = reduced_ok = 0
    misses_by_intent: dict[str, list[str]] = {}
    control_lat: list[float] = []
    reduced_lat: list[float] = []

    for case in cases:
        case_id = case.get("id", "<no-id>")
        user_text = case["prompt"]
        facts = case.get("facts") or {}
        expectation = case["expectation"]
        expected = _expected_intents(expectation)
        is_neg = _is_negative(expectation)

        # CONTROL arm: production path (full system prompt, AgentLoop).
        # Full dispatch table so any valid intent dispatches; only the intent
        # is scored (params ignored).
        loop = AgentLoop(runtime, _StubTable().table())
        t0 = time.monotonic()
        result = loop.run(user_text, facts=facts)
        control_lat.append(time.monotonic() - t0)
        ctrl_emitted = (
            result.envelope.intent.value if result.envelope is not None else None
        )
        ctrl_escalated = result.status is AgentTurnStatus.CLARIFIED
        if is_neg:
            c_ok = _match_negative(ctrl_emitted, expectation, ctrl_escalated)
        else:
            c_ok = _match_positive(ctrl_emitted, expected)

        # REDUCED arm: same model, tiny prompt, direct generation.
        reduced_prompt = _build_reduced_prompt(user_text, facts)
        t0 = time.monotonic()
        try:
            raw = runtime.generate(reduced_prompt)
        except Exception:  # noqa: BLE001 - a bad generation must not abort the run
            # One bad generation must not abort the whole run; treat it as a
            # miss (consistent with the unparseable-output treatment below).
            raw = None
        reduced_lat.append(time.monotonic() - t0)
        red_emitted = _extract_intent(raw)
        # A reduced-arm envelope that could not be parsed is an escalation-ish
        # miss; for negative expectations treat an unparseable output as not
        # an escalation (a miss), so it can never score as a pass.
        red_escalated = False
        if is_neg:
            r_ok = _match_negative(red_emitted, expectation, red_escalated)
        else:
            r_ok = _match_positive(red_emitted, expected)

        control_ok += int(c_ok)
        reduced_ok += int(r_ok)
        # A case with multiple allowed intents is listed once per allowed
        # intent (intentional - each is a routing miss on its own).
        if not r_ok and expected:
            for exp in expected:
                misses_by_intent.setdefault(exp, []).append(
                    f"{case_id}->{red_emitted or 'none'}"
                )

        print(f"  [{len(rows):>3}/{len(cases)}] {case_id} "
              f"ctrl={ctrl_emitted or 'none'}/{('PASS' if c_ok else 'FAIL')} "
              f"red={red_emitted or 'none'}/{('PASS' if r_ok else 'FAIL')}")

        rows.append(
            {
                "id": case_id,
                "expected": ",".join(expected) if expected else "(negative)",
                "control": ctrl_emitted or "none",
                "reduced": red_emitted or "none",
                "c_ok": "PASS" if c_ok else "FAIL",
                "r_ok": "PASS" if r_ok else "FAIL",
            }
        )

    total = len(cases)
    c_score = control_ok / total if total else 0.0
    r_score = reduced_ok / total if total else 0.0
    decision = (
        "PROCEED to T2 param probe"
        if r_score >= _THRESHOLD
        else "STOP - model cannot route; split cannot help"
    )

    # ---- stdout table ----
    print(f"{'case':<26} {'expected':<16} {'control':<12} {'reduced':<12} "
          f"{'c':<5} {'r':<5}")
    print("-" * 80)
    for row in rows:
        print(
            f"{row['id']:<26} {row['expected']:<16} "
            f"{row['control']:<12} {row['reduced']:<12} "
            f"{row['c_ok']:<5} {row['r_ok']:<5}"
        )
    print()
    print("SUMMARY")
    print(f"  control intent-only: {control_ok}/{total} ({c_score * 100:.1f}%)")
    print(f"  reduced intent-only: {reduced_ok}/{total} ({r_score * 100:.1f}%)")
    for prefix, label in (("golden-", "golden"), ("redteam-", "redteam")):
        n, gc, gr = _dataset_stats(rows, prefix)
        print(
            f"  {label} (n={n}): control {gc}/{n} "
            f"({gc / n * 100:.1f}%) | reduced {gr}/{n} ({gr / n * 100:.1f}%)"
        )
    print()
    c_total = sum(control_lat)
    r_total = sum(reduced_lat)
    c_mean, c_med = _mean_median(control_lat)
    r_mean, r_med = _mean_median(reduced_lat)
    two = [a + b for a, b in zip(control_lat, reduced_lat)]
    two_mean, two_med = _mean_median(two)
    print("LATENCY")
    print(
        f"  control total: {c_total:.1f}s; per-case mean {c_mean:.2f}s / "
        f"median {c_med:.2f}s"
    )
    print(
        f"  reduced total: {r_total:.1f}s; per-case mean {r_mean:.2f}s / "
        f"median {r_med:.2f}s"
    )
    print(
        f"  serial two-stage (control+reduced per case): mean {two_mean:.2f}s / "
        f"median {two_med:.2f}s"
    )
    print()
    print("PER-INTENT MISSES (reduced arm):")
    for intent in sorted(misses_by_intent):
        print(f"  {intent:<16} {len(misses_by_intent[intent]):>3}  "
              f"{', '.join(misses_by_intent[intent])}")
    if not misses_by_intent:
        print("  (none)")
    print()
    print(f"DECISION: {decision} (threshold {_THRESHOLD * 100:.0f}%)")

    # ---- write results md (value-free: ids + intent names only) ----
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# TCK-PROMPT-001 T1 - routing probe",
        "",
        f"- model: `{model_path}`",
        f"- reduced prompt: {len(REDUCED_SYSTEM_PROMPT)} chars",
        f"- cases: {len(golden)} golden + {len(redteam)} redteam = {total}",
        "",
        "## Per-fixture intent (expected / control / reduced)",
        "",
        "| case | expected | control | reduced | control | reduced |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['id']} | {row['expected']} | {row['control']} | "
            f"{row['reduced']} | {row['c_ok']} | {row['r_ok']} |"
        )
    lines += [
        "",
        "## Summary",
        "",
        f"- control intent-only: {control_ok}/{total} ({c_score * 100:.1f}%)",
        f"- reduced intent-only: {reduced_ok}/{total} ({r_score * 100:.1f}%)",
    ]
    for prefix, label in (("golden-", "golden"), ("redteam-", "redteam")):
        n, gc, gr = _dataset_stats(rows, prefix)
        lines.append(
            f"- {label} (n={n}): control {gc}/{n} ({gc / n * 100:.1f}%) | "
            f"reduced {gr}/{n} ({gr / n * 100:.1f}%)"
        )
    c_total = sum(control_lat)
    r_total = sum(reduced_lat)
    c_mean, c_med = _mean_median(control_lat)
    r_mean, r_med = _mean_median(reduced_lat)
    two = [a + b for a, b in zip(control_lat, reduced_lat)]
    two_mean, two_med = _mean_median(two)
    lines += [
        "",
        "## Latency",
        "",
        (
            f"- control total: {c_total:.1f}s; per-case mean {c_mean:.2f}s / "
            f"median {c_med:.2f}s"
        ),
        (
            f"- reduced total: {r_total:.1f}s; per-case mean {r_mean:.2f}s / "
            f"median {r_med:.2f}s"
        ),
        (
            f"- serial two-stage (control+reduced per case): "
            f"mean {two_mean:.2f}s / median {two_med:.2f}s"
        ),
        "",
        "## Per-intent misses (reduced arm)",
        "",
    ]
    if misses_by_intent:
        for intent in sorted(misses_by_intent):
            lines.append(
                f"- {intent}: {len(misses_by_intent[intent])} "
                f"({', '.join(misses_by_intent[intent])})"
            )
    else:
        lines.append("- (none)")
    lines += [
        "",
        "## Decision",
        "",
        f"{decision} (threshold {_THRESHOLD * 100:.0f}%)",
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nresults written to {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

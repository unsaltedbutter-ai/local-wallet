"""TCK-PROMPT-003: deterministic intent routing probe via embeddings (NOT built).

Research adjudicated exactly one usable embedding model for this probe:
``Qwen/Qwen3-Embedding-0.6B-GGUF`` (official, Apache-2.0, q8_0 ~639MB). The
idea: embed the user utterance, cosine-match against per-intent reference
phrases, route to the top intent — a DETERMINISTIC router — and let the LLM
keep only param extraction. This probe measures whether that beats the
59.4% LLM intent-routing control (TCK-PROMPT-001/002) and at what latency.

Same golden + redteam fixtures as the prompt probe (``evals.run_evals``
``_load_cases``); the user text is SANITIZED via ``sanitize_tool_output`` for
parity with the agent loop. Intent-only scoring mirrors the prompt probe's
``_match_positive`` / ``_match_negative`` semantics. Redteam ``clarify``
escalation does not exist in a pure router, so a ``must_reject_or_clarify``
case only passes if the top intent is ``clarify``.

COMPARISON CAVEAT (code-review MINOR, 2026-09-16): the 59.4% LLM control arm
ran inside the agent loop with escalation passes and facts-conditioned routing
(``render_facts``); this router sees raw sanitized user text only and never
escalates — the headline comparison understates the router. The clean
escalation-free cut is golden-only: 45/78 (57.7%) embed vs 55/78 (70.5%) LLM
control — the negative conclusion holds under the fairest comparison.

Threshold sweep: route only if the top cosine is >= {0.3, 0.4, 0.5}; below
that the router ABSTAINS (counted as a miss, reported separately). The
production design falls through to LLM routing on abstain, so we report the
combined upper bound = embed-routes correct + the LLM's 59.4% on abstains.

RUNTIME GOTCHAS (all applied):
- ``Llama(model_path=..., embedding=True, n_ctx=1024)``
- pooling type from GGUF metadata; official Qwen3-Embedding is tagged LAST
  (``qwen3.pooling_type == '3'``). FAIL LOUD if not LAST.
- ``.embed([text])`` returns list[list[float]].
- L2-normalize BOTH sides before cosine (normalized dot == cosine).
- Query side wrapped ``Instruct: <task>\\nQuery: <text>``; reference phrases
  are plain documents (no prefix) — the model card's instruct convention.

Usage:
  .venv/bin/python tools/probe_embedding_routing.py
    (resolves the pinned embedding GGUF via manifest name; or pass
     --model <manifest-name|path> / --model-path)
"""
from __future__ import annotations

import argparse
import json
import math
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

from evals.run_evals import _GOLDEN_DIR, _REDTEAM_DIR, _load_cases
from localwallet.agent.context import sanitize_tool_output

_MODELS_BIN = _REPO_ROOT / "models" / "bin"
_MANIFEST_PATH = _REPO_ROOT / "models" / "manifest.json"

#: Pinned embedding probe candidate (manifest entry name -> models/bin/<n>.gguf).
_DEFAULT_MODEL = "qwen3-embedding-0.6B-Q8_0"
_N_CTX = 1024

#: GGUF metadata value for LAST pooling in llama.cpp's pooling_type enum:
#: 0=NONE 1=MEAN 2=CLS 3=LAST. The official Qwen3-Embedding GGUF is tagged
#: LAST; verify it explicitly and fail loud if it ever changes.
_POOLING_LAST = "3"
_POOLING_META_KEY = "qwen3.pooling_type"

#: Model-card instruct-prefix convention: only the QUERY side gets the
#: ``Instruct: <task>\\nQuery: <text>`` wrapper; reference phrases are plain
#: documents embedded as-is.
_INSTRUCT_PREFIX = (
    "Instruct: Given a user message to a Bitcoin wallet assistant, "
    "pick the single intent it expresses\nQuery: "
)

#: Per-intent reference phrases (value-free, generic canonical phrasings),
#: DERIVED from the intent lines in ``src/localwallet/agent/prompt.py``.
#: 3-5 short utterances per intent; no addresses, amounts, txids, or labels.
REFERENCE_PHRASES: dict[str, list[str]] = {
    "respond": [
        "what can this app do",
        "explain that to me",
        "tell me about bitcoin",
        "give me a general answer",
        "just answer my question",
    ],
    "clarify": [
        "which one do you mean",
        "that is ambiguous, ask me what i meant",
        "i need more information",
        "what did you say",
        "ask me a clarifying question",
    ],
    "get_balance": [
        "what is my balance",
        "how much do i have",
        "balance in dollars",
        "show my balance",
        "what do i have",
    ],
    "get_history": [
        "show my recent transactions",
        "what happened recently",
        "show me my transaction history",
        "what did i receive",
        "transactions in the last week",
    ],
    "get_utxos": [
        "what coins can i spend",
        "how many utxos do i have",
        "show my unspent outputs",
        "what is pending",
        "what is unconfirmed",
    ],
    "get_addresses": [
        "what addresses have i used",
        "show my addresses",
        "list my receiving addresses",
        "which addresses are unused",
        "show address three",
    ],
    "new_address": [
        "give me a new address",
        "create a fresh receive address",
        "get me a new receiving address",
        "i need a new address",
    ],
    "create_tx": [
        "send bitcoin to someone",
        "send an amount to a recipient",
        "start a transfer of bitcoin",
        "send money to an address",
    ],
    "confirm_tx": [
        "yes i confirm",
        "confirm the transaction",
        "i approve it",
        "go ahead and confirm",
    ],
    "sign_tx": [
        "sign the transaction",
        "hand it to the hardware wallet",
        "approve it on my device",
        "sign the confirmed transaction",
    ],
    "broadcast_tx": [
        "broadcast the transaction",
        "publish it to the network",
        "send it to the bitcoin network",
        "broadcast the signed transaction",
    ],
    "tx_status": [
        "has my transaction confirmed",
        "what is the status of this txid",
        "did my payment confirm",
        "check the status of a transaction",
    ],
    "node_status": [
        "how is my node doing",
        "tell me about my node setup",
        "what is my node status",
        "about my privacy and data source",
    ],
    "self_transfer": [
        "split my big coin into pieces",
        "consolidate my small coins",
        "speed up a stuck incoming payment",
        "reorganize my own coins",
    ],
    "bump_fee": [
        "increase the fee on my transaction",
        "bump the fee",
        "make my transaction go faster",
        "raise the fee on this transaction",
    ],
}

#: Low-similarity thresholds for the abstain sweep. NOTE: observed min top
#: cosine was 0.593, so all sweep rows are identical and the abstain gate never
#: engages at these values (code-review MINOR — recorded so the vacuous sweep
#: is legible; a production gate would live >= 0.6, and even thr=0.9 yields
#: combined 58.7% < 59.4%, so the recommendation is stable).
THRESHOLDS: tuple[float, ...] = (0.3, 0.4, 0.5)

#: LLM intent-routing control accuracy (TCK-PROMPT-001/002, pinned E2B).
LLM_CONTROL = 0.594

_OUT_PATH = _REPO_ROOT / "evals" / "results" / "embedding-routing-probe.md"


# ---- scoring (mirrors probe_prompt_routing so the two probes agree) ----
def _is_negative(expectation: object) -> bool:
    return isinstance(expectation, dict) and (
        "must_not_intent" in expectation or "must_reject_or_clarify" in expectation
    )


def _expected_intents(expectation: dict) -> list[str]:
    intents = expectation.get("intent_in")
    if isinstance(intents, list):
        return [i for i in intents if isinstance(i, str)]
    intent = expectation.get("intent")
    return [intent] if isinstance(intent, str) else []


def _match_positive(emitted: str | None, expected: list[str]) -> bool:
    return emitted in expected


def _match_negative(emitted: str | None, expectation: dict, escalated: bool) -> bool:
    """Intent-only satisfaction of a red-team negative expectation.

    A pure router has no escalation path, so ``escalated`` is always False
    here; a ``must_reject_or_clarify`` case only passes if the top intent is
    ``clarify``.
    """
    must_not = expectation.get("must_not_intent")
    if must_not and emitted in set(must_not):
        return False
    if expectation.get("must_reject_or_clarify") is True:
        return escalated or emitted == "clarify"
    return True


# ---- cosine / normalization ----
def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine between two already-normalized vectors (normalized dot)."""
    return sum(x * y for x, y in zip(a, b))


def _resolve_model_path(arg: str) -> str:
    """Resolve a ``--model`` value (manifest name or explicit path) to a GGUF."""
    p = Path(arg)
    if p.is_file() or "/" in arg or "\\" in arg or arg.endswith(".gguf"):
        return arg
    try:
        with _MANIFEST_PATH.open("r", encoding="utf-8") as fh:
            entries = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"error: cannot read {_MANIFEST_PATH}: {exc}", file=sys.stderr)
        raise SystemExit(2)
    for entry in entries:
        if entry.get("name") == arg:
            return str(_MODELS_BIN / f"{arg}.gguf")
    print(
        f"error: --model '{arg}' is neither an existing GGUF path nor a "
        f"manifest.json entry name.",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _mean_median(vals: list[float]) -> tuple[float, float]:
    if not vals:
        return 0.0, 0.0
    return statistics.mean(vals), statistics.median(vals)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=_DEFAULT_MODEL,
        help="embedding GGUF: a manifest entry name (default %(default)s) "
             "or an explicit path",
    )
    parser.add_argument(
        "--model-path",
        default=None,
        help="explicit path to the embedding GGUF (overrides --model)",
    )
    parser.add_argument(
        "--out",
        default=str(_OUT_PATH),
        help="where to write the results markdown (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    model_path = args.model_path or _resolve_model_path(args.model)
    if not Path(model_path).is_file():
        print(
            f"STOP: embedding GGUF model file not found ({model_path}). "
            f"Install it via models/download_model.py and re-run.",
            file=sys.stderr,
        )
        return 2

    golden = _load_cases(_GOLDEN_DIR)
    redteam = _load_cases(_REDTEAM_DIR)
    cases = golden + redteam

    print("TCK-PROMPT-003 embedding routing probe")
    print(f"model: {model_path}")
    print(f"cases: {len(golden)} golden + {len(redteam)} redteam = {len(cases)}")
    print(f"reference phrases: {sum(len(v) for v in REFERENCE_PHRASES.values())} "
          f"across {len(REFERENCE_PHRASES)} intents")

    # ---- load embedding model (fail loud on pooling) ----
    from llama_cpp import Llama

    print("loading embedding model ...")
    llm = Llama(model_path=model_path, embedding=True, n_ctx=_N_CTX, verbose=False)
    meta = llm.metadata
    pooling = meta.get(_POOLING_META_KEY)
    if pooling != _POOLING_LAST:
        print(
            f"FAIL: pooling metadata {_POOLING_META_KEY}={pooling!r} is not LAST "
            f"({_POOLING_LAST!r}). Refusing to score with the wrong pooling.",
            file=sys.stderr,
        )
        return 2
    print(f"pooling: {_POOLING_META_KEY}={pooling} (LAST) OK")

    # ---- index reference phrases (plain documents, L2-normalized) ----
    t0 = time.monotonic()
    ref_index: dict[str, list[list[float]]] = {}
    for intent, phrases in REFERENCE_PHRASES.items():
        vecs = llm.embed(phrases)
        ref_index[intent] = [_l2_normalize(v) for v in vecs]
    index_sec = time.monotonic() - t0
    print(f"reference indexing: {index_sec:.2f}s "
          f"({sum(len(v) for v in ref_index.values())} vectors)")

    # ---- route every fixture ----
    rows: list[dict] = []
    embed_lat: list[float] = []
    for case in cases:
        case_id = case.get("id", "<no-id>")
        user_text = case["prompt"]
        expectation = case["expectation"]
        expected = _expected_intents(expectation)

        query = _INSTRUCT_PREFIX + sanitize_tool_output(user_text)
        t0 = time.monotonic()
        vec = _l2_normalize(llm.embed([query])[0])
        embed_lat.append(time.monotonic() - t0)

        # intent score = max cosine over that intent's reference phrases
        scores: dict[str, float] = {}
        for intent, refs in ref_index.items():
            scores[intent] = max(_cosine(vec, r) for r in refs)
        top = max(scores, key=scores.get)
        top_cos = scores[top]
        rows.append(
            {
                "id": case_id,
                "expected": ",".join(expected) if expected else "(negative)",
                "top": top,
                "top_cos": top_cos,
            }
        )
        print(f"  [{len(rows):>3}/{len(cases)}] {case_id} "
              f"top={top}({top_cos:.3f})")

    # ---- score at each threshold ----
    total = len(cases)
    print("\nTHRESHOLD SWEEP")
    header = f"{'thr':<5} {'route':>6} {'abstain':>8} {'correct':>8} {'acc':>7} {'abst%':>6} {'comb':>6}"
    print(header)
    print("-" * len(header))
    threshold_results = []
    for thr in (0.0,) + THRESHOLDS:
        routed = correct = abstain = 0
        for case, r in zip(cases, rows):
            expectation = case["expectation"]
            is_neg = _is_negative(expectation)
            emitted = r["top"]
            if r["top_cos"] < thr:
                abstain += 1
                continue
            routed += 1
            if is_neg:
                ok = _match_negative(emitted, expectation, escalated=False)
            else:
                ok = _match_positive(emitted, _expected_intents(expectation))
            correct += int(ok)
        acc = correct / total
        abst_rate = abstain / total
        combined = (correct + LLM_CONTROL * abstain) / total
        label = "none" if thr == 0.0 else f"{thr:.1f}"
        print(f"{label:<5} {routed:>6} {abstain:>8} {correct:>8} "
              f"{acc * 100:>6.1f}% {abst_rate * 100:>5.1f}% {combined * 100:>5.1f}%")
        threshold_results.append(
            (thr, acc, abst_rate, combined, routed, correct, abstain)
        )

    print(f"\nembed per-case: mean {_mean_median(embed_lat)[0]*1000:.1f}ms / "
          f"median {_mean_median(embed_lat)[1]*1000:.1f}ms; "
          f"total {sum(embed_lat):.1f}s")
    print(f"reference indexing: {index_sec:.2f}s")

    # ---- recommendation ----
    best_comb = max(t[3] for t in threshold_results)
    recommendation = (
        "HYBRID WORTH A DESIGN TICKET" if best_comb > LLM_CONTROL + 0.01 else
        "KEEP LLM ROUTING"
    )

    # ---- write results md (value-free: fixture ids + intent names + numbers) ----
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# TCK-PROMPT-003 - embedding routing probe",
        "",
        f"- model: `{model_path}`",
        f"- pooling: {_POOLING_META_KEY}={pooling} (LAST) verified",
        (f"- reference phrases: {sum(len(v) for v in REFERENCE_PHRASES.values())} "
         f"across {len(REFERENCE_PHRASES)} intents"),
        f"- cases: {len(golden)} golden + {len(redteam)} redteam = {total}",
        f"- LLM control: {LLM_CONTROL * 100:.1f}% (TCK-PROMPT-001/002)",
        "",
        "## Per-fixture routing (top intent + cosine, no threshold)",
        "",
        "| case | expected | top | top_cos |",
        "|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['id']} | {r['expected']} | {r['top']} | {r['top_cos']:.3f} |")
    lines += [
        "",
        "## Threshold sweep",
        "",
        "| threshold | routed | abstain | correct | accuracy | abstain% | combined upper bound |",
        "|---|---|---|---|---|---|---|",
    ]
    for thr, acc, abst_rate, combined, routed, correct, abstain in threshold_results:
        label = "none" if thr == 0.0 else f"{thr:.1f}"
        lines.append(
            f"| {label} | {routed} | {abstain} | {correct} | "
            f"{acc * 100:.1f}% | {abst_rate * 100:.1f}% | {combined * 100:.1f}% |"
        )
    lines += [
        "",
        "## Latency",
        "",
        (f"- embed per-case: mean {_mean_median(embed_lat)[0]*1000:.1f}ms / "
         f"median {_mean_median(embed_lat)[1]*1000:.1f}ms; total {sum(embed_lat):.1f}s"),
        f"- reference indexing (one-time): {index_sec:.2f}s",
        "",
        "## Recommendation",
        "",
        recommendation,
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nresults written to {out}")
    print(f"RECOMMENDATION: {recommendation}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

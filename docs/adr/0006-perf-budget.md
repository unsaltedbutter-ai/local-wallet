# ADR-0006 — Performance Budget (v0)

- **Status:** Accepted (Phase 0) — **measurement deferred-run**
- **Date:** 2026-08-31
- **Answers:** PROJECT.md §14 OQ20 — "Performance budget: acceptable TTFT and
  turn latency on base Mac mini and worst-case Windows box; drives quant +
  runtime choice."
- **Owners:** eng (agent runtime) + CI

## Context

OQ20 / R15: mid-range Windows hardware (older CPU, no GPU offload) is the
worst case and may be slow. The perf budget drives the quant choice and the
runtime choice (ADR-0001). We need concrete, testable targets so that Phase 0
does not "ship fast by accident" and so a regression is caught before Phase 6
packaging.

Two reference targets (PROJECT.md G6):
- **A:** base Mac mini (Apple Silicon).
- **B:** mid-range Windows CPU-only (no GPU offload).

## Targets

| Metric | Target A (Mac mini) | Target B (Windows CPU-only) |
|---|---|---|
| **TTFT** (time to first token) | ≤ 3 s | ≤ 3 s |
| **Full turn** (input → complete envelope) | ≤ 15 s | ≤ 30 s with `Q4_K_M` quant, context ≤ 8K |

Notes:
- **128K context is not a v0 target.** The context budget for v0 is ≤ 8K
  tokens per request (context-window management summarizes older turns,
  §7.1 / R13). This keeps the worst-case compute bounded.
- `Q4_K_M` is the v0 default quant (see `models/MODELS.md`, R12).

## Measurement plan (deferred-run)

Cannot be executed in this sandbox (no model file, no target hardware, shell
restricted to git). The plan is to be run **manually after model download**
(on reference hardware A and B) and, where possible, **on CI-blessed
hardware**. Results are recorded **back into this ADR** below.

Timing harness:
1. Download `gemma-4-E2B-it-Q4_K_M` (bootstrap via `models/download_model.py
   --write-hash`).
2. Load it through `llama-cpp-python` with `envelope.gbnf` grammar.
3. Time, for a representative turn (~8K input ctx, streaming):
   - TTFT: first decoded token.
   - Full turn: time to a complete, valid envelope.
4. Repeat N ≥ 5, report median + p95.
5. Optionally repeat with E4B and with a smaller quant to establish headroom.

## Results

*(To be filled in on first run — see plan above. Marked deferred-run.)*

| Hardware | Model | Quant | ctx | TTFT (median) | Full turn (median) | Pass? |
|---|---|---|---|---|---|---|
| (pending) | gemma-4-E2B-it | Q4_K_M | 8K | — | — | — |
| (pending) | gemma-4-E4B-it | Q4_K_M | 8K | — | — | — |

## Failure mode if targets are missed

If targets are missed on the worst-case hardware, in order:

1. **Reduce context** (below 8K; tighten summarization, §7.1).
2. **Lower the quant** (e.g. `Q4_K_M` → `Q4_0`/`Q3_K_M`) while re-checking
   R12 quant quality against evals.
3. **Fall back to E4B → smaller quant** (E4B is the drop-in fallback per R1;
   a smaller quant may recover latency while keeping tool-use quality).
4. **Revisit ADR-0001** — if the in-process wheel cannot meet targets, flip to
   `llama-server` (process isolation / potential offload tuning) or
   reconsider the runtime. This flip procedure is documented in ADR-0001.

Whichever lever is pulled must be re-verified against the eval suite
(PROJECT.md §13 R12/R15) so a latency win does not regress intent-extraction
quality.

---

*Cross-references: PROJECT.md §2 G6, §7.1, §13 R12/R15, §14 OQ1/OQ20,
§12 Phase 0. Runtime: ADR-0001. Model pins: `models/MODELS.md`.*

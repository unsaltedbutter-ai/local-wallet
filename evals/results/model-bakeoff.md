# TCK-PROMPT-002 - model bake-off

Source per-model probe files in `evals/results/model-bakeoff-<model>.md` and the
pinned E2B baseline in `evals/results/prompt-routing-probe.md`. All runs: 78
golden + 28 redteam = 106 fixtures, temp 0.2.

## Per-model table

| model | control intent-only | reduced intent-only | golden ctrl | golden reduced | redteam ctrl | redteam reduced | latency ctrl mean/median | latency reduced mean/median | serial two-stage mean/median |
|---|---|---|---|---|---|---|---|---|---|
| gemma-4-E2B (pinned baseline) | 63/106 (59.4%) | 58/106 (54.7%) | 55/78 (70.5%) | 50/78 (64.1%) | 8/28 (28.6%) | 8/28 (28.6%) | 34.88s / 34.75s | 3.82s / 3.41s | 38.70s / 38.38s |
| qwen3-4b-instruct-Q4_K_M | 73/106 (68.9%) | 57/106 (53.8%) | 62/78 (79.5%) | 49/78 (62.8%) | 11/28 (39.3%) | 8/28 (28.6%) | 93.56s / 93.15s | 22.51s / 28.62s | 116.07s / 121.36s |
| qwen3-1.7b-instruct-Q4_K_M | 63/106 (59.4%) | 64/106 (60.4%) | 56/78 (71.8%) | 52/78 (66.7%) | 7/28 (25.0%) | 12/28 (42.9%) | 43.60s / 43.16s | 15.89s / 15.84s | 59.49s / 58.99s |
| phi-4-mini-instruct-Q4_K_M | 64/106 (60.4%) | 51/106 (48.1%) | 53/78 (67.9%) | 45/78 (57.7%) | 11/28 (39.3%) | 6/28 (21.4%) | 49.75s / 46.63s | 6.36s / 4.69s | 56.11s / 51.74s |

## Material-gain rule

Adopt a candidate only if it gains >= +15 points over the 59.4% E2B control
intent-only (i.e. control intent-only >= 74.4%) with serial two-stage mean
<= 45s.

- Control-gain over E2B baseline: qwen3-4b +9.5 (fails >= +15); qwen3-1.7b +0.0
  (fails); phi-4-mini +1.0 (fails).
- Serial two-stage mean: qwen3-4b 116.07s (fails <= 45s); qwen3-1.7b 59.49s
  (fails); phi-4-mini 56.11s (fails).

## Accuracy-per-second

Control intent-only per control-latency-mean (percentage points per second):

- gemma-4-E2B (pinned baseline): 59.4 / 34.88 = 1.70
- qwen3-4b-instruct-Q4_K_M: 68.9 / 93.56 = 0.74
- qwen3-1.7b-instruct-Q4_K_M: 59.4 / 43.60 = 1.36
- phi-4-mini-instruct-Q4_K_M: 60.4 / 49.75 = 1.21

## Recommendation

No candidate clears both bars (no candidate reaches 74.4% control intent-only
or a <= 45s serial two-stage mean). Best accuracy-per-second among the three
candidates is qwen3-1.7b-instruct-Q4_K_M (1.36); it still trails the pinned
gemma-4-E2B baseline (1.70). Recommendation: keep gemma-4-E2B as the pinned
model; do not swap.

# Evals — local-wallet

Golden prompts, a red-team placeholder, and a runner for the intent
extraction suite (PROJECT.md §7.10).

## Layout

- `golden/*.json` — one JSON file per golden case (11 cases,
  `golden-001..golden-011`). Each is `{"id", "prompt", "expectation"}`
  where `expectation` is either an exact envelope
  (`{"intent", "params"}`) or a structural predicate
  (`{"intent", "text_nonempty"|"question_nonempty"}`).
- `redteam/README.md` — Phase 6 placeholder: scope, case structure, and the
  merge-gate note. Nothing runnable there yet.
- `run_evals.py` — the runner (fixture mode + optional model mode).

## Running

Fixture mode (default) — validates every golden fixture through the protocol
schema and business rules, and checks predicate expectations structurally.
No model, no network:

```sh
python evals/run_evals.py
# or
python -m evals.run_evals
```

Model mode — runs each golden prompt through the real local model via
`AgentLoop` with a recording dispatch table and compares the emitted intent
(and exact params where the expectation demands) against the expectation.
Requires a downloaded GGUF:

```sh
LOCALWALLET_MODEL_PATH=/path/to/model.gguf python evals/run_evals.py --model
# or pass the path explicitly:
python evals/run_evals.py --model --model-path /path/to/model.gguf
```

Without a model path in model mode, the runner fails loudly (exit 2).

## What golden / redteam are

- **Golden** — the closed-intent behavior we expect to hold for the P0
  intent set (`respond` / `clarify` / `get_balance`). Exact-envelope cases
  pin the wire format; predicate cases pin intent choice without over-pinning
  free text.
- **Redteam** — documented Phase 6 scope for adversarial inputs (injection,
  exfiltration, confirm bypass, fuzzing). See `redteam/README.md`.

## Working agreement

Every protocol/prompt change ships with an eval run (PROJECT.md §15). This
becomes a merge gate in Phase 6 (≥95% golden / 100% confirm-gates).

## Phase 0 status

- **Fixture mode** is enforced in CI-able pytest (`tests/test_fuzz_validation.py`
  spot-checks grammar↔schema agreement and the malformed-input invariant) and
  via `evals/run_evals.py` directly: all 11 fixtures must validate, exit 0.
- **Model mode** is manual until the model-download bootstrap lands
  (`models/`); `--model` is informational in P0 (prints the score and the
  Phase 6 gate note) and does not gate merges.

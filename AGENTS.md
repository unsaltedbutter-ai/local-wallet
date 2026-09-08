# AGENTS.md

## Repo state

- Phases 0–5 complete; the security sweep (TCK-SEC-001..004) and the MAINNET FLIP (TCK-MAIN-001..003, ADR-0021) are committed on `dev/plan-run-1`. Last full suite: 1677 passed / 6 skipped; evals fixture mode green. Read HANDOFF.md for current state and TASKS.md for the authoritative ticket ledger.
- Model bootstrap (MANUAL-WORK.md MW-2) gates Phase 6 (TCK-P6-001). Weights live in `models/bin/` (gitignored); the pinned download script + `manifest.json` are tracked.
- Layout follows PROJECT.md §15: `src/localwallet/{agent,protocol,wallet,chain,tx,signer,node,store,ui}`, `evals/`, `tests/`, `models/`, ADRs in `docs/adr/`.

## Non-negotiable invariants (from PROJECT.md)

- `chain/` is the only module with network access. No network imports anywhere else — the spec makes this lint-enforced.
- The LLM never touches money logic, network calls, or secrets. It only emits intent envelopes; treat all model output as untrusted input. Validation is three layers (GBNF grammar → pydantic → business rules), then allowlist dispatch. Any failure = reject, one re-prompt, then `clarify`.
- Closed intent protocol: intents are a fixed enum mapped to handlers via a dispatch table. No `eval`, no codegen from model output; unknown intents are rejected, never executed.
- Watch-only: the app handles xpubs only — never xprvs or seed phrases (seed phrases are refused in chat, with hardware-wallet-only guidance). Zero secrets in process, disk, or logs; never log xpubs, addresses, or amounts.
- Destructive flows are dispatcher-owned state machines (`create_tx → confirm_tx → sign_tx → broadcast_tx`). The model cannot skip or reorder steps, and an LLM "yes" never counts as user confirmation.
- Signed PSBTs are re-parsed and re-validated against the intended transaction before broadcast. Any mismatch is a hard stop.
- Addresses and amounts are quoted verbatim from tool output — the model never generates or "corrects" them.
- Dust/min-relay thresholds are computed from script size, not hardcoded constants.
- Mainnet-only (ADR-0021, supersedes the original "testnet-only until Phase 6" plan): testnet keys/addresses and all private keys are refused at parse; recipients must be mainnet scripts.

## Conventions

- Python 3.12+. Chosen stack — don't substitute: `embit` for Bitcoin primitives, `hwi` used as a library (not CLI) for hardware wallets, pydantic v2 for envelope validation, SQLite (WAL) for storage, Gemma 4 E2B GGUF via llama.cpp with grammar-constrained decoding.
- Every protocol/prompt change ships with an eval run (`evals/`); this becomes a merge gate in Phase 6.
- Each answered open question (PROJECT.md §14) becomes an ADR in `docs/adr/`.

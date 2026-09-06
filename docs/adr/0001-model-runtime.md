# ADR-0001 — Model Runtime for v0: llama-cpp-python (wheel)

- **Status:** Accepted (Phase 0)
- **Date:** 2026-08-31
- **Answers:** PROJECT.md §14 OQ1 — "Model runtime packaging: llama-server
  binary, llama-cpp-python wheel, Ollama, or LM Studio runtime?"
- **Owners:** eng (agent runtime)

## Context

The v0 app is a CLI walking skeleton (PROJECT.md §12 Phase 0). It must run
Gemma 4 E2B GGUF locally with grammar-constrained decoding (GBNF) so that
model output is syntactically valid JSON envelopes before validation even
starts (§5 principle 4). The runtime choice affects: install story, update
mechanism, license distribution, process lifecycle, and how tightly we can
control decoding.

Candidates on the table (OQ1): `llama-server` binary, `llama-cpp-python`
wheel, Ollama, LM Studio.

## Decision

Use **`llama-cpp-python` (wheel)** as the v0 runtime (pinned in
`pyproject.toml`, `llama-cpp-python>=0.3`).

Rationale:

- **In-process.** The app is a single CLI process; embedding the runtime as a
  library avoids a separate daemon/process lifecycle. Simpler startup, simpler
  shutdown, no socket/port management, no orphaned-server cleanup.
- **Native GBNF grammar support.** `llama-cpp-python` exposes
  `LlamaGrammar.from_string(...)` and `Llama(..., grammar=...)`, binding
  directly to llama.cpp's grammar engine — exactly the constrained-decoding
  mechanism the envelope contract depends on.
- **Distribution fit.** A pip wheel is trivial to install alongside the rest
  of the Python stack and to update in lockstep with other deps. Apache-2.0
  family licensing is compatible with the project's model licensing (no
  additional proprietary runtime term).
- **Testable.** Can be imported lazily and skipped cleanly in unit tests when
  no local model file is present (TCK-P0-005), which the phase-0 testnet-only
  and deferred-download constraints require.

## Alternatives considered (not chosen)

- **`llama-server` binary** — Better process isolation and an HTTP API that
  could be reused across components (e.g. a future UI talking to a local
  server). Rejected for v0 because it adds a second process to build,
  launch, health-check, and tear down, plus packaging/distribution cost
  (fetching a platform binary vs a pip wheel). **Revisit** if performance or
  packaging demands change (see ADR-0006 flip procedure).
- **Ollama** — External app dependency; the user must install and run a
  separate service. Weaker programmatic control over GBNF grammar injection
  at decode time. Rejected.
- **LM Studio** — External app dependency with a GUI, aimed at interactive
  use; poorer fit for embedding grammar-constrained decoding in a headless
  Python core. Rejected.

## Consequences

- The runtime is a Python library; the agent loop (TCK-P0-005) owns the
  `Llama` instance lifecycle.
- The `envelope.gbnf` grammar (TCK-P0-004) must be loadable as a package
  resource and passed to `LlamaGrammar` at decode time.
- We must track llama.cpp versioning via `llama-cpp-python` release cadence;
  GGUF/E2B support and quant handling follow that cadence (see R12 note).

## Follow-ups

- **R12 quant quality (deferred-run):** once the models are downloaded, run an
  eval comparing E2B vs E4B GGUF (and Q4_K_M vs a higher quant / safetensors
  reference) before locking the default quant. Record results in `evals/`.
- **Perf gate (OQ20 / R15):** if ADR-0006 targets cannot be met with the
  in-process wheel, this ADR is revisited per the flip procedure in ADR-0006.

## Amendment (2026-09-06) — weights source switched to unsloth mirrors

The weights **source** is switched from the official `google/gemma-4-*-it-GGUF`
repos to the ungated `unsloth/gemma-4-*-it-GGUF` mirror repos
(TCK-MW2-FIX). Rationale: the official repos are gated on Hugging Face and
returned `HTTP 401 Unauthorized` on first download (license acceptance + token
required), blocking the bootstrap. The unsloth mirrors are ungated, widely
used, and Apache-2.0; both entries now point at them. Integrity does **not**
depend on the source host — it comes from the SHA-256 pin recorded in
`manifest.json` at bootstrap (`--write-hash`), and every download is verified
against that pin before install. The official repos remain reachable as a
token-authenticated alternative via `--hf-token`/`HF_TOKEN` (note they ship a
different quant, `q4_0`, vs the pinned `Q4_K_M`).

---

*Cross-references: PROJECT.md §7.1, §11 (LLM + Decoding constraint rows),
§12 Phase 0, §13 R1/R12/R15, §14 OQ1/OQ20. Envelope contract: ADR-0002.
Perf budget: ADR-0006.*

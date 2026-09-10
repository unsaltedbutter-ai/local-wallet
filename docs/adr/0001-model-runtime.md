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

## Amendment (2026-09-06) — sources state: unsloth primary (all URLs live-verified), QAT q4_0 added

Weights **source state** as of today (TCK-MODELS-002). The ungated
`unsloth/gemma-4-*-it-GGUF` mirror repos remain the **primary** pinned source
(`manifest.json`): they are ungated, Apache-2.0, and both entries point at
them. All manifest entries were re-verified live (HTTP 200/206 + GGUF magic at
byte 0) against the download tool's own request path; the E2B and E4B entries
were confirmed correct — an earlier claim of an E4B 404 was an orchestration
misread and is retracted. The official
`google/gemma-4-E2B-it-qat-q4_0-gguf` repo — the **QAT** `q4_0` build of E2B —
is **ungated** (gated: `false`, verified live) and is now added as a third
manifest entry (`gemma-4-E2B-it-qat-q4_0`) so it can serve as a second eval
subject for the **R12** quant-quality comparison (official QAT `q4_0` vs the
pinned unsloth `Q4_K_M`). The full-size official `google/gemma-4-*-it-GGUF`
repos remain gated / token-optional (`--hf-token`/`HF_TOKEN`). Trust still
does **not** come from the source host: integrity comes from hash-pinning via
`--write-hash` at bootstrap, and every download is verified against that pin
before install.

---

*Cross-references: PROJECT.md §7.1, §11 (LLM + Decoding constraint rows),
§12 Phase 0, §13 R1/R12/R15, §14 OQ1/OQ20. Envelope contract: ADR-0002.
Perf budget: ADR-0006.*

## Amendment (2026-09-09, TCK-LAUNCH-002) — the default model is the pinned model, and a missing download is an offer, not a silent demo

Per the user direction of 2026-09-09: **not specifying a model now means the
DEFAULT pinned model, not the stub.** Selection order (in `app.run`):

1. injected `generate_fn` (test seam),
2. the ADR-0007 remote bridge (explicit env only),
3. `LOCALWALLET_MODEL_PATH` (explicit path, unchanged),
4. `--stub-llm` (explicit dev choice — no banner, no card, unchanged),
5. **new:** `models/manifest.json`'s entry flagged `"default": true` resolved
   to `models/bin/<name>.gguf` — **file present → the real GGUF runtime,
   silently** (the normal launch); **file absent → the demo stub keeps the
   session alive AND the engine arms a deterministic Yes/No download card**
   (web buttons + CLI yes/no; the card replaces the old silent demo-mode
   banner);
6. no resolvable/UNPINNED default (manifest unreadable, no `default` entry,
   or a null `sha256` — an unpinned model is never auto-downloaded) → the
   old visible demo banner stands.

On a YES the engine runs the tracked pinned downloader
(`models/download_model.py --model <default> --json-progress`) as an
**engine-owned subprocess** — argument list, never a shell; the child's own
streaming SHA-256 verification against the manifest pin (and its resumable
`.part`) is the install gate, untouched. Progress rides the event emitter as
int-only payloads (percent + bytes; no path/name/user ever enters an event —
the child's stdout is parsed for those ints and every other line, including
its own messages, is discarded, stderr included). QUIT/process-exit
terminates the child bounded (terminate → kill → bounded join; no orphans).
Completion narrates once: **the model activates on the NEXT launch** (a
hot-swap of the running session's runtime is deliberately not attempted —
the stub owns this session until it exits).

**Network-lint exception (scoped, ADR-blessed):** `tools/lint_network.py`
gains `FILE_MODULE_EXCEPTIONS` — ONE file (`app.py`), ONE module
(`subprocess`), for exactly this child-process orchestration. No network
module is exempt there (a test pins that an injected `urllib` import in
`app.py` still fails the lint); the child is a separate process running the
repo's own build-time tool, which the lint has always excluded by design.

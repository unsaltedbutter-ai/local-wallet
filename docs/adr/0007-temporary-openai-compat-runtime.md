# ADR-0007 — TEMPORARY OpenAI-compatible Remote LLM Runtime (Debug Bridge)

- **Status:** Accepted (explicitly temporary — dev/debug only)
- **Date:** 2026-08-31
- **Related:** TCK-P0-008; supersedes nothing; does **not** revise ADR-0001
- **Owners:** eng (agent runtime)

## Context

The pinned Phase 0 model (Gemma 4 E2B GGUF, per ADR-0001 and
`models/MODELS.md`) is not downloaded yet. Until that bootstrap lands, there
is no on-device model to drive the agent, which blocks debugging and testing
of everything downstream of the runtime: the agent loop, the eval runner in
model mode, and end-to-end plumbing.

A LAN OpenAI-compatible endpoint is available for development:
base URL `http://notible.local:8083/v1`, model id
`mlx-community/gemma-4-26b-a4b-it-mxfp8` (an MLX server). Pointing the agent
at it would unblock debugging immediately — but it conflicts with two
invariants if done carelessly:

- PROJECT.md §5 principle 6 ("one network module"): all network I/O lives in
  `chain/`, lint-enforced.
- PROJECT.md §9 privacy table: "Chat content / LLM inference — never leaves
  the machine (local model)".

## Decision

Add a **temporary** debug bridge: `RemoteOpenAIRuntime`
(`src/localwallet/agent/remote_runtime.py`), selected **only** via the
explicit environment variable `LOCALWALLET_LLM_BASE_URL` (plus
`LOCALWALLET_LLM_MODEL`, optional `LOCALWALLET_LLM_API_KEY`,
`LOCALWALLET_LLM_TIMEOUT_S`). It implements the exact
`ModelRuntime.generate(prompt, grammar_text) -> str` seam (and is callable,
so it satisfies `GenerateFn`), so `AgentLoop`, `app.py`, and
`evals/run_evals.py --model` treat it as a drop-in runtime. Precedence in
both entrypoints: remote env → local GGUF (`LOCALWALLET_MODEL_PATH`) →
`--stub-llm`.

Grammar handling is best-effort: the envelope GBNF grammar is attempted as a
top-level `grammar` field (llama.cpp-server convention, an OpenAI-schema
extension). On `400` — or any 4xx whose body indicates the field is
unsupported — the runtime retries once without the field and remembers the
downgrade for subsequent calls. Model output is never "repaired": the raw
`choices[0].message.content` string (whitespace-stripped only) flows through
the normal 3-layer validation pipeline (GBNF downstream of generation here,
pydantic, business rules) exactly as for the local runtime. Runtime
selection is never implicit: no env var, no remote runtime. Error messages
are scrubbed (status codes, exception class names, URL host only — never
request/response bodies, never the prompt, never the API key), and there is
no logging anywhere.

## Scope limits (what this bridge is NOT for)

- **Debugging/plumbing only.** Verifying wiring, the agent loop, the eval
  runner's model mode, and manual smoke tests.
- **No prompt tuning.** The prompt must not be adjusted against this
  endpoint's behavior; prompts are tuned against the pinned E2B model.
- **No eval conclusions.** The debug model (26B-A4B, a far larger capability
  class) is not the pinned E2B (R1); golden-set scores observed through this
  bridge say nothing about E2B performance. Golden-set runs through the
  bridge are informational only.
- **AC#3 ("the model runs fully locally") is still open** until the E2B
  bootstrap lands; this bridge does not satisfy it.

## Privacy

Chat text (system prompt + assembled user conversation) **leaves this
machine** to the configured LAN host on every generation. This is a
deliberate, disclosed exception to the PROJECT.md §9 "chat never leaves the
machine" row, valid only while this temporary ADR stands:

- The CLI banner prints a one-line disclosure whenever the remote runtime is
  selected: `DEBUG: using remote LLM <host:port> (<model>) — chat text
  leaves this machine.` (`evals/run_evals.py --model` prints the same notice
  to stderr.)
- The API key is optional, supplied only via `LOCALWALLET_LLM_API_KEY`,
  sent as `Authorization: Bearer …` only when non-empty, and never logged,
  never echoed in exceptions, and never shown in the banner.
- Proxy environment variables are deliberately ignored
  (`trust_env=False` on the HTTP client) so egress goes only to the
  configured host.
- Watch-only invariants are unaffected: xpubs/addresses/amounts are still
  never logged; the bridge handles no secrets beyond the optional API key.

## Lint exception

`tools/lint_network.py` gains a single named exception,
`AGENT_LLM_TRANSPORT_FILES = ("agent/remote_runtime.py",)`: network imports
are permitted in exactly one file outside `chain/`. Every other `agent/`
file, `evals/`, and all other non-`chain/` code remains banned. When this
ADR is retired, the constant reverts to an empty tuple / is removed.

## Removal criterion

Delete `remote_runtime.py`, the lint exception, and the env-driven selection
when the pinned E2B GGUF bootstrap lands (TCK-P0-004 follow-up). The bridge
may only be *promoted* to a supported runtime via a new ADR that revisits
ADR-0001 explicitly. Any capability added here beyond the generate seam
(streaming, tools, chat history server-side) is out of scope and must not be
built on this bridge.

## Rejected alternatives

- **Routing the LLM call through `chain/`** (e.g. a "chain adapter" for the
  model endpoint): semantic abuse of the chain adapter — an LLM completion
  is not chain data, and it would blur the one-network-module boundary the
  lint exists to keep greppable.
- **Using stdlib `urllib`/`http.client` directly in an agent file without a
  lint exception**: silent invariant erosion — the lint would fail (or worse,
  grow an undocumented hole); an explicit, named, single-file exception with
  an ADR citation is auditable.
- **Waiting for the E2B bootstrap before any agent debugging**: leaves the
  whole downstream pipeline untestable for the duration of a multi-GB
  download; the debug bridge unblocks it at negligible, disclosed cost.

---

*Cross-references: ADR-0001 (runtime of record — unchanged), PROJECT.md §5
(principles 4, 6), §7.1, §9 (privacy table + honest indicator), §12 Phase 0
AC#3, §13 R1. Lint: `tools/lint_network.py`. Tests:
`tests/test_remote_runtime.py`, `tests/test_lint_network.py`,
`tests/test_run_evals_selection.py`.*

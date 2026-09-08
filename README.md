# local-wallet

A watch-only Bitcoin wallet where you say what you want in plain language and
a **local** AI turns it into wallet actions — send, receive, check your
balance — while **your keys stay on your hardware wallet**.

Mainnet-only. Local-first. Grammar-constrained. No cloud, no custodians, no
seed phrases in the app.

## What it is

- **Watch-only by design.** The app only ever handles extended *public* keys
  (xpubs/zpubs). It never touches private keys, xprvs, or seed phrases — seed
  phrases are refused outright, with guidance. Your keys live exclusively on
  your hardware wallet.
- **Local LLM, your intentions, closed intent protocol.** A local language
  model (Gemma via [llama.cpp](https://github.com/ggerganov/llama.cpp) with
  grammar-constrained decoding) interprets what you ask into a small, fixed
  set of "intent envelopes." Model output is treated as untrusted input and
  validated in layers (grammar → schema → business rules) before being
  dispatched through a fixed allowlist — never executed directly.
- **You verify, you sign.** This software builds the transaction. Your job is
  to confirm that addresses and amounts match what you intended, then sign on
  your hardware wallet via [HWI](https://github.com/bitcoin-core/HWI)
  (Ledger, Trezor, Coldcard, Jade, BitBox02, and more). Nothing is signed or
  broadcast without your explicit confirmation — a model "yes" never counts
  as user approval.
- **Revalidation before broadcast.** Signed PSBTs are re-parsed and
  re-validated against the intended transaction before anything is broadcast.
  Any mismatch is a hard stop.
- **Self-hosted or public Esplora backend.** Chain data (balances, fees,
  prices) comes from an Esplora instance — point it at your own node's
  Esplora or a public one.
- **Auditable core.** The LLM interprets intent and narrates results; every
  action it triggers runs through deterministic Python you can read, and the
  network-touching surface is confined to a single module (`chain/`).

## Key invariants

- **Watch-only** = xpubs only. No private keys, no seed phrases, ever.
- **The LLM never touches money logic, network calls, or secrets.** It only
  emits intent envelopes; all model output is untrusted input.
- **Confirm gate.** Destructive flows are dispatcher-owned state machines
  (`create_tx → confirm_tx → sign_tx → broadcast_tx`). The model cannot skip
  or reorder steps, and a user "yes" in chat is only part of the gate — the
  real confirmation happens on the device.
- **Signed-PSBT revalidation.** What was broadcast is re-checked against what
  you approved.
- **Mainnet-only.** Testnet keys and addresses are refused at parse.

## Install

Requires **Python 3.12+** (never 3.14 — hwilib/protobuf break on it). The
one-shot installer clones the repo, sets up a venv, and is idempotent:

```sh
curl -fsSL https://unsaltedbutter.ai/install | bash
```

It detects your OS/arch, ensures a compatible Python, and can download the
hash-pinned model (optional). See [docs/install.md](docs/install.md) for
manual steps, uninstall, and troubleshooting.

## Quick start (development)

Create a venv and install:

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

Run the CLI (no model needed — uses a stub LLM):

```sh
.venv/bin/python -m localwallet.ui.cli --stub-llm
```

Run tests and evals with the venv's interpreter:

```sh
.venv/bin/pytest tests/
.venv/bin/python evals/run_evals.py        # fixture mode — no model required
```

## Configuration

Every keyed setting resolves through one ladder (TCK-CFG-002):

| Rung | Source | Example |
|------|--------|---------|
| 1 (highest) | env var `LOCALWALLET_*` | `LOCALWALLET_GAP_LIMIT=30` |
| 2 | config file `~/.localwallet/config.json` | `{"gap_limit": "30"}` |
| 3 | stored setting (DB) | `/set gap_limit 30` |
| 4 (lowest) | shipped default | gap 20 |

### Config file

The optional JSON file at **`~/.localwallet/config.json`** sets the same
scalar fields as the `LOCALWALLET_*` env vars (lowercase field names —
`gap_limit`, `chain_base_url`, `request_timeout_s`, `price_enabled`, …).
The `~/.localwallet/` per-user path keeps config private to the user and
survives reinstalls — no repo writes. An absent file changes nothing; env
always wins over the file.

Values are type-checked per field (boolean / integer / number / string), so
use real JSON types. The **`gap_limit`** worked example — the per-scan
address gap limit (default 20, ADR-0009), an integer `1..1000`:

```json
{ "gap_limit": "30" }
```

A small gap (e.g. `2`) makes scans fast, but a gap that is too small can
**miss allocated-but-unused addresses** — if you suspect funds on addresses
you handed out, widen the gap and rescan per ADR-0009.

**Fail-closed:** malformed JSON, an unknown key, or a value of the wrong
type refuses startup with a value-free error (the offending value is never
echoed) — even if an env var would have overridden it. Fix the file and
restart.

## Docs

- `docs/PROJECT.md` — full spec, architecture, and roadmap.
- `docs/adr/` — architecture decision records (mainnet-only, confirm gate,
  chain-backend choice, gap policy, and more).
- `evals/` — golden prompts, red-team sets, and the eval runner.

## Status

Actively developed. Mainnet-only (see ADR-0021). Deferred-run items from the
roadmap are not yet promised or shipped. See `docs/PROJECT.md` for the
current roadmap.

---

[![CI](https://github.com/unsaltedbutter-ai/local-wallet/actions/workflows/ci.yml/badge.svg)](https://github.com/unsaltedbutter-ai/local-wallet/actions/workflows/ci.yml)

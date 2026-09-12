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
hash-pinned model (optional). Already cloned the repo? Run `./install.sh`
inside it — it will set up in place. See [docs/install.md](docs/install.md) for
manual steps, uninstall, and troubleshooting.

## Quick start (development)

Create a venv and install:

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

Run it — **the web UI is the default launch** (TCK-LAUNCH-001,
ADR-0024 amendment):

```sh
.venv/bin/python -m localwallet.ui.cli
```

It starts a loopback-only server (127.0.0.1, random port + per-launch
token), prints the URL and best-effort opens your browser. No model file?
It starts anyway in **demo mode** (canned-data stub LLM, clearly bannered)
— set `LOCALWALLET_MODEL_PATH` for the real local model. Never given a
watch key? The page asks for your wallet's **public account key** (xpub /
ypub / zpub) in a first-run form; give it once and later launches reuse
it. Keys are gated on entry: mainnet-only, no private keys, seed phrases
refused — this app is hardware-wallet-only.

Terminal REPL instead?

```sh
.venv/bin/python -m localwallet.ui.cli --cli         # or: LOCALWALLET_UI=cli
```

Other launch knobs: `--zpub <key>` / `LOCALWALLET_ZPUB` override the
stored key for one launch (flag > env > stored); `--stub-llm` runs the dev
stub without the demo banner; `LOCALWALLET_WEB_PORT=8788` binds a fixed
loopback port (default `0` = automatic — see ADR-0024 §6 amendment);
`LOCALWALLET_STORE_PATH` points at the SQLite store.

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
| 2 | config file `config.json` (repo root) | `{"gap_limit": "30"}` |
| 3 | stored setting (DB) | `/set gap_limit 30` |
| 4 (lowest) | shipped default | gap 20 |

### Config file

The optional JSON file at **`config.json`** (repo/install root, next to the
code) sets the same
scalar fields as the `LOCALWALLET_*` env vars (lowercase field names —
`gap_limit`, `chain_base_url`, `request_timeout_s`, `price_enabled`, …).
An absent file changes nothing; env
always wins over the file.

**Migration note (TCK-CFG-003):** the default file is now `config.json` at
the repo/install root next to the code — the old `~/.localwallet/config.json`
is no longer read by default (no silent migration; copy your settings over
if you want them). To read any other file instead, set the
`LOCALWALLET_CONFIG_PATH` env var to its path (escape hatch).

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

**Self-hosted https with a private / self-signed cert** (`tls_verify`,
`LOCALWALLET_TLS_VERIFY`; default `true`, env > config file > default, no
stored rung): verification off means whoever controls the network path can
observe your queried addresses and tamper with responses, so prefer adding
the CA to your OS trust store; if you must, set `"tls_verify": false` and
accept the honest startup warning (ADR-0018 amendment).

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

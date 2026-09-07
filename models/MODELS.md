# Models — pinned downloads

This directory holds the **pinned model registry** for local-wallet. Model
weights themselves are gitignored (`models/*.gguf`); this document and
`manifest.json` are the source of truth for *which* files to fetch and *how*
to verify them. `download_model.py` is the build-time tool that enforces this
pinning.

> **License note:** Both models are distributed under **Apache-2.0**
> (PROJECT.md §11). The Gemma 4 E2B/E4B GGUF files are fetched from the
> ungated `unsloth` mirror repos (also Apache-2.0). No secrets are involved at
> any point — this is a public model download.

## Weights provenance

- **Primary source (default):** the `unsloth/gemma-4-*-it-GGUF` mirror repos
  on Hugging Face. These are **ungated** (no license gate, no token needed),
  widely used, and re-hosted under Apache-2.0. `manifest.json` points at them.
- **Alternative (gated):** the official full-size `google/gemma-4-*-it-GGUF`
  repos are **gated** — they require accepting the Gemma license and passing a
  token. To download from them, set the `HF_TOKEN` environment variable
  (preferred) rather than passing `--hf-token` on the command line, so the
  token never appears in the process table or shell history;
  the official URL would need to be put back into `manifest.json` manually.
  Note those official repos ship a **different quant** — `q4_0` — versus the
  pinned `Q4_K_M` used here. That quant difference matters to the R12
  quant-comparison plan, so treat the official repo as an alternative, not a
  drop-in source.
- **QAT build (ungated, now a manifest entry):** the official
  `google/gemma-4-E2B-it-qat-q4_0-gguf` repo — the quantization-aware-training
  (QAT) `q4_0` build of E2B — is **NOT gated** (unlike the full-size official
  GGUF repos above). It is now a `manifest.json` entry
  (`gemma-4-E2B-it-qat-q4_0`) and serves as a **second eval subject** for the
  R12 quant-quality comparison (official QAT `q4_0` vs the pinned unsloth
  `Q4_K_M`). See the dedicated table below.
- **Integrity:** trust does NOT come from the source host. It comes from the
  SHA-256 digest recorded in `manifest.json` at bootstrap (`--write-hash`).
  Any download is verified against that pin before install, regardless of
  which host it was fetched from.

## Primary model — E2B (default)

Google Gemma 4 **E2B**-it (Per-Layer Embeddings "effective 2B"), the
project's chosen primary model (PROJECT.md §7.1, R1). 128K context, Apache-2.0.

| Field | Value |
|---|---|
| Name | `gemma-4-E2B-it-Q4_K_M` |
| HF repo | `unsloth/gemma-4-E2B-it-GGUF` |
| Quant | `Q4_K_M` (default, good size/speed/quality trade-off for v0) |
| SHA-256 | `null` *(fill on first networked run — see Bootstrap)* |
| Est. size | ~3.11 GB (verified from repo listing; 3,106,738,272 bytes) |
| License | Apache-2.0 |

## Fallback model — E4B

Google Gemma 4 **E4B**-it — the drop-in fallback per R1 (E4B scores higher on
tool-use / function-calling: Tau2 42.2 vs E2B 24.5). Used if E2B proves too
weak at intent extraction, or if the perf gate (ADR-0006) forces a swap.

| Field | Value |
|---|---|
| Name | `gemma-4-E4B-it-Q4_K_M` |
| HF repo | `unsloth/gemma-4-E4B-it-GGUF` |
| Quant | `Q4_K_M` (default) |
| SHA-256 | `null` *(fill on first networked run — see Bootstrap)* |
| Est. size | ~4.98 GB (verified from repo listing; 4,977,171,584 bytes) |
| License | Apache-2.0 |

## QAT alternative — gemma-4-E2B-it-qat-q4_0

The official Google **QAT** (quantization-aware-training) `q4_0` build of E2B
(Per-Layer Embeddings, 128K context, Apache-2.0). Unlike the full-size official
GGUF repos, this repo is **ungated** — no license gate, no token needed. It is
a `manifest.json` entry and serves as a **second eval subject** for the R12
quant-comparison plan (official QAT `q4_0` vs the pinned unsloth `Q4_K_M`).

| Field | Value |
|---|---|
| Name | `gemma-4-E2B-it-qat-q4_0` |
| HF repo | `google/gemma-4-E2B-it-qat-q4_0-gguf` |
| Quant | `q4_0` (QAT) |
| SHA-256 | `null` *(fill on first networked run — see Bootstrap)* |
| Est. size | ~3.35 GB (3,349,516,256 bytes; from HF API) |
| License | Apache-2.0 |

This entry is the official-QAT side of the R12 quant-quality comparison — see
[Quant quality note (R12)](#quant-quality-note-r12) below.

## Quant quality note (R12)

`Q4_K_M` is the v0 default. R12 flags that **GGUF quant quality for E2B may
lag the safetensors release**. Once the models are downloaded, an eval run
must compare E2B vs E4B GGUF quality (and, if warranted, a higher quant vs the
safetensors reference) before the choice is locked. The R12 comparison now
also pits the **official QAT `q4_0` build** (`gemma-4-E2B-it-qat-q4_0`, see
the table above) against the pinned unsloth `Q4_K_M` for E2B. This is tracked
as a follow-up in ADR-0001 and the perf budget in ADR-0006. `Q4_K_M` may be
swapped for `Q5_K_M`/`Q6_K`/`Q8_0` if evals demand it and hardware allows.

## SHA-256 bootstrap (first run)

`sha256` is `null` for both entries **by design**: we do not commit hashes we
have not yet produced from a verified download (that would be trusting
ourselves, and we have not fetched the files yet). The first networked run
pins them:

```bash
# Download + verify + record the real hashes back into manifest.json
python models/download_model.py --model gemma-4-E2B-it-Q4_K_M --write-hash
python models/download_model.py --model gemma-4-E4B-it-Q4_K_M --write-hash
```

After that, `manifest.json` holds real `sha256`/`size_bytes` values and every
subsequent download is verified against them.

## Commands

```bash
# Verify an already-downloaded file against the manifest (exit 0/1)
python models/download_model.py --model gemma-4-E2B-it-Q4_K_M --check

# Download (resumable) and verify; refuse to install on hash mismatch
python models/download_model.py --model gemma-4-E2B-it-Q4_K_M

# Download into a custom directory instead of models/bin
python models/download_model.py --model gemma-4-E2B-it-Q4_K_M --out /path/to/bin

# Download from a gated official repo (token-based; defaults need no token).
# Set the env var (read -s avoids echo and history); do NOT pass --hf-token.
read -s HF_TOKEN; export HF_TOKEN
python models/download_model.py --model gemma-4-E2B-it-Q4_K_M   # HF_TOKEN is inherited from the env

# Full bootstrap of both models (fills null hashes)
python models/download_model.py --model gemma-4-E2B-it-Q4_K_M --write-hash
python models/download_model.py --model gemma-4-E4B-it-Q4_K_M --write-hash
```

See `python models/download_model.py --help` for full usage.

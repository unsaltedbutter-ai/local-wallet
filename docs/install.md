# Installing local-wallet

local-wallet is a Python 3.12 project installed straight from its GitHub repo
(`github.com/unsaltedbutter-ai/local-wallet`) via `uv`. There is no PyPI
package; `install.sh` clones the repo and does an editable install for you.

`install.sh` has two modes, detected automatically:

- **curl | bash (managed clone):** run from any directory (the one-command
  path below) — it clones the repo to `~/.local/share/local-wallet` and
  installs there.
- **Already cloned (in-place):** `cd` into your local-wallet checkout and run
  `./install.sh` — it detects the checkout and sets up the `.venv` and install
  right there, without cloning again. This matches the repo's own dev
  convention.

> **Verified 2026-09-08:** Python 3.14.6, hwi 3.2.0, protobuf 4.25.9, embit
> 0.8.0, llama-cpp-python 0.3.35 (3.14 wheel available).

## Quick install (curl | bash)

```sh
curl -fsSL https://unsaltedbutter.ai/install | bash
```

That one command:

1. Detects your OS (macOS / Linux) and architecture (arm64 / x86_64) and
   refuses anything else.
2. Ensures a **Python >=3.12 and <3.14** runtime — reusing an existing one if
   present, otherwise installing Python 3.12 via `uv`.
3. Ensures `uv` is available (installing it via its official installer if not).
4. Clones the repo to `~/.local/share/local-wallet`
   (or `$XDG_DATA_HOME/local-wallet` if you set `XDG_DATA_HOME`).
5. Creates a `.venv` and installs the package editable with its `dev` extras
   (`uv venv` + `uv pip install -e '.[dev]'`).
6. Optionally downloads the pinned GGUF model (prompted, default **No**). If the
   model is already present and passes checksum verification it is skipped
   entirely; a corrupt copy is re-downloaded (prompted, default **Yes**); a
   partial `.part` download is resumed.
7. Prints next steps.

It is **idempotent**: re-running it updates the existing clone (`git pull`,
best-effort — offline is not fatal) and reinstalls. Every step fails loudly
with a non-zero exit and a clear message; nothing is silently skipped.

> **Why not 3.14?** Verified 2026-09-08 (Python 3.14.6, hwi 3.2.0, protobuf
> 4.25.9, embit 0.8.0): `hwi` pins `protobuf <5.0.0`, and that protobuf's upb
> C-extension crashes on 3.14 with `TypeError: Metaclasses with custom tp_new
> are not supported` — breaking hwilib's protobuf-dependent device path (e.g.
> BitBox02). The installer refuses 3.14 and installs a 3.12 side by side.
> Re-check when `hwi` lifts its `protobuf <5` pin:
> `uv venv --python 3.14 /tmp/v && uv pip install -e '.[dev]' --python /tmp/v/bin/python && /tmp/v/bin/python -c "from hwilib.devices.bitbox02_lib.communication.generated import hww_pb2"`.

## Where things go

- Repo + install: `~/.local/share/local-wallet/` (override with `INSTALL_ROOT`).
- Virtualenv: `~/.local/share/local-wallet/.venv/`.
- Models (if you download them): `~/.local/share/local-wallet/models/bin/`.

## Manual install (no curl | bash)

If you prefer to do it by hand, the equivalent steps are:

```sh
# 1. Pick a Python 3.12 (or 3.13) — not 3.14.
# 2. Clone and enter the repo.
git clone https://github.com/unsaltedbutter-ai/local-wallet \
    "$HOME/.local/share/local-wallet"
cd "$HOME/.local/share/local-wallet"

# 3. Create a venv and install (with uv, or plain venv — see below).
uv venv --python 3.12 .venv
uv pip install -e '.[dev]'

# 4. Optional: download the hash-pinned model.
.venv/bin/python models/download_model.py --model gemma-4-E2B-it-Q4_K_M --write-hash
```

Prefer `uv` (install from <https://astral.sh/uv/>; on macOS, `brew install uv`
also works). No `uv`? The same works with the stdlib venv:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## Running it

```sh
# Stub LLM — no model needed, good for a first run:
~/.local/share/local-wallet/.venv/bin/python \
    -m localwallet.ui.cli --stub-llm

# With the local model (after downloading it):
~/.local/share/local-wallet/.venv/bin/python \
    -m localwallet.ui.cli
```

There is also a `local-wallet` console script on `PATH` if you add
`~/.local/share/local-wallet/.venv/bin` to your `PATH`.

## Uninstalling

local-wallet is fully self-contained under one directory (plus the model
weights). To remove it:

```sh
rm -rf ~/.local/share/local-wallet        # or your $INSTALL_ROOT
rm -rf ~/.local/share/local-wallet.venv   # not created — only for clarity
```

That is everything — no system packages, no config files outside that
directory. If you installed `uv` only for this project, remove it separately
(`brew uninstall uv` on macOS, or delete `~/.local/bin/uv`).

## Troubleshooting

### "pip install" inside the activated .venv resolves to the wrong Python
The project venv is created by **uv**, which ships venvs **without pip** — a bare
`pip` inside the activated venv falls through to your system Python (often ancient)
and tries a user-site install. Symptom: "Defaulting to user installation because
normal site-packages is not writeable" + resolution errors ignoring every modern
pydantic version (Requires-Python >=3.8+ skipped).
**Fix:** use `uv pip install -e '.[dev]'` (with the venv activated), or
`.venv/bin/python -m pytest …`-style invocations, or just run `./install.sh`
inside the checkout — it does the right thing. Always invoke via
`.venv/bin/python …` rather than trusting the activated prompt.

## Troubleshooting

- **"unsupported OS / architecture"** — only macOS (arm64/x86_64) and Linux
  (x86_64) are supported. The installer refuses to guess on anything else.
- **Python 3.14 problems** — verified 2026-09-08: `hwi` pins `protobuf
  <5.0.0`, whose upb C-extension crashes on 3.14 (`TypeError: Metaclasses with
  custom tp_new are not supported`), breaking hwilib's protobuf device path
  (BitBox02). The installer sidesteps this by installing Python 3.12 via `uv`;
  for a manual install, make sure your venv was created from 3.12 or 3.13.
- **Offline re-run** — the update step (`git pull`) is best-effort: if you are
  offline it warns and continues with the existing clone, then reinstalls.
- **"this directory has a pyproject.toml but is not local-wallet"** — `install.sh`
  detected a `pyproject.toml` in `$PWD` but no `src/localwallet`, so it refused
  (exit 2) rather than risk installing another project. Run it from the
  local-wallet checkout or a neutral directory.
- **In-place mode ignores `INSTALL_ROOT`** — `INSTALL_ROOT` only applies to the
  clone (`curl | bash`) mode; when you run `./install.sh` inside a checkout, the
  repo and venv stay in that checkout.
- **`uv` not found after install** — the official installer puts it at
  `~/.local/bin/uv`. Add `~/.local/bin` to your `PATH` or log out/in.
- **Model download interrupted** — `download_model.py` is resumable (Range
  requests) and always verifies the SHA-256 before installing, so a partial
  file is never trusted. Re-running the installer resumes an interrupted
  `.part` download instead of starting over.

The installer never prints secrets — there are none involved (watch-only, and
the model download is a public, hash-pinned file).

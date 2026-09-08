# Installing local-wallet

local-wallet is a Python 3.12 project installed straight from its GitHub repo
(`github.com/unsaltedbutter-ai/local-wallet`) via `uv`. There is no PyPI
package; `install.sh` clones the repo and does an editable install for you.

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
6. Optionally downloads the pinned GGUF model (prompted, default **No**).
7. Prints next steps.

It is **idempotent**: re-running it updates the existing clone (`git pull`,
best-effort — offline is not fatal) and reinstalls. Every step fails loudly
with a non-zero exit and a clear message; nothing is silently skipped.

> **Why not 3.14?** Python 3.14 breaks `hwilib` and `protobuf`, which the
> hardware-wallet and model stacks depend on. The installer refuses to use it.
> If you already have only 3.14 on your machine, it installs a 3.12 side by
> side and uses that.

## Where things go

- Repo + install: `~/.local/share/local-wallet/` (override with `INSTALL_ROOT`).
- Virtualenv: `~/.local/share/local-wallet/.venv/`.
- Models (if you download them): `~/.local/share/local-wallet/models/bin/`.

## Manual install (no curl | bash)

If you prefer to do it by hand, the equivalent steps are:

```sh
# 1. Pick a Python 3.12 (or 3.13) — never 3.14.
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

- **"unsupported OS / architecture"** — only macOS (arm64/x86_64) and Linux
  (x86_64) are supported. The installer refuses to guess on anything else.
- **Python 3.14 problems** — if your default `python3` is 3.14, `hwilib` /
  `protobuf` fail to build or import. The installer sidesteps this by
  installing Python 3.12 via `uv`; for a manual install, make sure your venv
  was created from 3.12 or 3.13.
- **Offline re-run** — the update step (`git pull`) is best-effort: if you are
  offline it warns and continues with the existing clone, then reinstalls.
- **`uv` not found after install** — the official installer puts it at
  `~/.local/bin/uv`. Add `~/.local/bin` to your `PATH` or log out/in.
- **Model download interrupted** — `download_model.py` is resumable (Range
  requests) and always verifies the SHA-256 before installing, so a partial
  file is never trusted.

The installer never prints secrets — there are none involved (watch-only, and
the model download is a public, hash-pinned file).

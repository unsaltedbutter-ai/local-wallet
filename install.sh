#!/usr/bin/env bash
#
# install.sh — one-shot installer for local-wallet.
#
# Entry point (the repo's landing page redirects here):
#     curl -fsSL https://unsaltedbutter.ai/install | bash
#
# Installs the package by cloning the GitHub repo and doing a `uv`-based
# editable install. Detects OS/arch, ensures a Python >=3.12 <3.14 runtime
# (NEVER 3.14 — hwilib/protobuf break on it), and optionally downloads the
# hash-pinned GGUF model. Idempotent: re-running updates the clone and
# reinstalls. Fails loudly at every step — no silent continues.
#
# Testability: set INSTALL_ROOT to install to a custom directory instead of
# the default $XDG_DATA_HOME/local-wallet (see docs/install.md).
set -euo pipefail

GITHUB_REPO="https://github.com/unsaltedbutter-ai/local-wallet"
UV_INSTALLER="https://astral.sh/uv/install.sh"
MODEL="gemma-4-E2B-it-Q4_K_M"

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
REPO="${INSTALL_ROOT:-$DATA_HOME/local-wallet}"

info() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==>\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mFATAL:\033[0m %s\n' "$*" >&2; exit 1; }

# Prints "os/arch" (e.g. "macos/arm64") and refuses anything unsupported.
detect_os_arch() {
  local os arch
  case "$(uname -s)" in
    Darwin) os="macos" ;;
    Linux)  os="linux" ;;
    *) die "unsupported OS: $(uname -s). Only macOS and Linux are supported." ;;
  esac
  case "$(uname -m)" in
    arm64|aarch64) arch="arm64" ;;
    x86_64|amd64)  arch="x86_64" ;;
    *) die "unsupported architecture: $(uname -m). Only arm64 and x86_64 are supported." ;;
  esac
  printf '%s/%s\n' "$os" "$arch"
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    UV="$(command -v uv)"
  elif [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
  else
    info "uv not found; installing it via the official installer"
    curl -fsSL "$UV_INSTALLER" | sh
    UV="$HOME/.local/bin/uv"
    [ -x "$UV" ] || die "uv install failed (expected at $UV)"
  fi
}

ensure_python() {
  # Prefer an existing >=3.12 <3.14 interpreter; else install 3.12 via uv.
  # We never go to 3.14: hwilib/protobuf break on it (HANDOFF §7).
  if PY="$("$UV" python find '>=3.12,<3.14' 2>/dev/null)"; then
    return
  fi
  info "no compatible Python found; installing Python 3.12 via uv"
  "$UV" python install 3.12
  PY="$("$UV" python find '3.12')"
  [ -n "$PY" ] || die "could not resolve Python 3.12"
}

ensure_repo() {
  if [ -d "$REPO/.git" ]; then
    info "updating existing clone in $REPO"
    ( cd "$REPO" && git pull --ff-only ) \
      || warn "could not pull latest (offline?); continuing with existing clone"
  else
    info "cloning local-wallet into $REPO"
    mkdir -p "$(dirname "$REPO")"
    git clone --depth 1 "$GITHUB_REPO" "$REPO"
  fi
  [ -f "$REPO/pyproject.toml" ] || die "clone at $REPO is missing pyproject.toml; aborting"
}

install_pkg() {
  info "creating venv and installing local-wallet (editable, with dev extras)"
  ( cd "$REPO" && "$UV" venv --python "$PY" .venv )
  ( cd "$REPO" && "$UV" pip install --python "$REPO/.venv/bin/python" -e '.[dev]' )
}

maybe_model() {
  # Prompt only when stdin is an interactive terminal — never read from the
  # piped script stream (curl | bash). Default is No.
  if [ ! -t 0 ]; then
    info "non-interactive shell; skipping model download"
    return
  fi
  printf 'Download the ~3.1 GB model (%s) now? [y/N] ' "$MODEL"
  local ans
  read -r ans
  case "${ans:-}" in
    [yY]|[yY][eE][sS])
      info "downloading and verifying the model (hash-pinned, ~3.1 GB)"
      ( cd "$REPO" && "$REPO/.venv/bin/python" models/download_model.py \
          --model "$MODEL" --write-hash )
      ;;
    *)
      info "skipping model download (run later: python models/download_model.py --model $MODEL)"
      ;;
  esac
}

next_steps() {
  info "installation complete"
  cat <<EOF

local-wallet is installed in: $REPO

Run the CLI with a stub LLM (no model needed):
    $REPO/.venv/bin/python -m localwallet.ui.cli --stub-llm

Run it with the local model (once downloaded):
    $REPO/.venv/bin/python -m localwallet.ui.cli

See $REPO/docs/install.md and $REPO/docs/PROJECT.md for details.
EOF
}

main() {
  info "detected $(detect_os_arch)"
  ensure_uv
  ensure_python
  ensure_repo
  install_pkg
  maybe_model
  next_steps
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi

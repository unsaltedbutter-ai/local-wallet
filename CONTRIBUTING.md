# Contributing

Thanks for considering contributing to local-wallet. This is a wallet — the
bar for merging is intentionally high, and the invariants below are
non-negotiable.

## Development setup

Requires **Python 3.12+** (PEP 695 syntax). Do **not** use Python 3.14 —
verified 2026-09-08: `hwi` pins `protobuf <5.0.0`, whose upb C-extension
crashes on 3.14 (`TypeError: Metaclasses with custom tp_new are not
supported`), breaking hwilib's protobuf device path. Use 3.12.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

We use `uv` if you have it:

```sh
uv sync
```

## The gate (all must pass before merge)

```sh
.venv/bin/python -m pytest tests/ -q        # full suite
.venv/bin/python -m ruff check src tests evals
.venv/bin/python tools/lint_network.py      # network imports banned outside chain/
.venv/bin/python evals/run_evals.py         # fixture mode (no model needed)
```

- **Network imports** are allowed *only* in `src/localwallet/chain/` (plus a
  few documented exceptions: the temporary remote-LLM debug bridge, the
  localhost node doctor, and the localhost web UI). `tools/lint_network.py`
  enforces this — never route around it.
- **Evals are a merge gate.** Every protocol/prompt change ships with an eval
  run (`evals/`). Model-mode evals require a model/network and are *not* part
  of CI; run them locally when your change touches the prompt or protocol.

## Architecture pointers

- `docs/PROJECT.md` §15 — repository layout; read this before editing.
- `docs/adr/` — architecture decision records; a change that touches a
  decision should update or add an ADR.
- `AGENTS.md` — conventions for AI-assisted development in this repo.

## The invariants (do not violate)

- Watch-only: xpubs only, never xprvs or seed phrases.
- The LLM never touches money logic, network calls, or secrets — it only
  emits intent envelopes, treated as untrusted input.
- Destructive flows are dispatcher-owned state machines; the model cannot
  skip or reorder steps, and a model "yes" is never user confirmation.
- Signed PSBTs are re-validated against the intended transaction before
  broadcast.
- Mainnet-only: testnet keys/addresses and all private keys are refused.

## Reporting bugs

Open an issue. For security issues, see [SECURITY.md](SECURITY.md).

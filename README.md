# local-wallet

A local-first Bitcoin wallet for **testnet** driven by a grammar-constrained local LLM.

Read `PROJECT.md` for the full spec, architecture, and roadmap.

**Testnet-only** until the Phase 6 mainnet gate — no mainnet keys, ever.

Commands: `pytest` · `ruff check` · `python tools/lint_network.py`

## Quick start

Requires **Python 3.12+**. Create a venv and install:

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

Run tests and evals with the venv's interpreter (or activate it first):

```sh
.venv/bin/pytest tests/
.venv/bin/python evals/run_evals.py
```

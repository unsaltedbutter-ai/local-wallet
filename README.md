# local-wallet

An easy-to-use Bitcoin wallet where you say your intentions and a local AI
interprets them into wallet actions. Tell it what you want in plain language —
send money, receive bitcoin, check your balance — and it figures out the right
thing to do and carries it out.

Read `PROJECT.md` for the full spec, architecture, and roadmap.

Commands: `pytest` · `ruff check` · `python tools/lint_network.py`

## How it works

- **You say what you want; the AI does the wallet work.** You don't click
  through forms or type addresses by hand. A local AI interprets your
  intentions into wallet actions. For example you can ask it to send a specific
  amount to a given address, tell it you need to receive some bitcoin, or ask
  what your bitcoin is worth in USD — and it translates each into the right
  action.
- **You verify; you sign.** This software creates the transactions. Your job
  is to verify that the addresses and amounts are what you intended, then sign
  with your hardware wallet (Ledger, Trezor, Coldcard, Jade, BitBox02, etc.).
  Nothing is signed or broadcast without you expressly approving it on the
  device.
- **Keys live only on your hardware wallet.** The app works from extended
  public keys and never touches xprvs or seed phrases.
- **Auditable core.** The AI only interprets intent and narrates results.
  Every action it triggers runs through deterministic Python code you can read,
  and nothing happens without your explicit confirmation — in chat and on the
  device.

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

## Configuration (env vars)

- **`LOCALWALLET_GAP_LIMIT`** — dev knob: overrides the per-scan address gap
  limit (default 20, ADR-0009). An integer `1..1000`; a malformed value
  refuses startup. A small value (e.g. `2`) makes scans fast, but a gap that
  is too small can **miss allocated-but-unused addresses** — if you suspect
  funds on addresses you handed out, widen the gap and rescan per ADR-0009.

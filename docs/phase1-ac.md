# Phase 1 Acceptance Criteria — Verification Harness (TCK-P1-006)

Phase 1 AC (PROJECT.md §12): *"balance/UTXO/history match Electrum +
mempool.space on a mainnet wallet with known history including >20-address
gaps; rescan fixes a simulated stale cache; unit tests for prefix→script-type
mapping."*

The AC is verified at **two layers**:

| Layer | What it proves | Where |
|---|---|---|
| **Offline composite story** (runs in CI, no network) | The full explorer→scan→store→handler chain agrees with a hand-constructed ground truth ("explorer view") EXACTLY — store state, derivation, UTXO set, history, cursor, balance; rescan repairs a corrupted cache; the >20-gap is missed at gap 20 and found at gap 30 with the documented R3 warning. | `tests/test_phase1_ac.py` |
| **LIVE procedure** (this document, manual) | The literal AC: the app's numbers match real Electrum/Bitcoin Core data on a funded mainnet wallet (mempool.space remains a human comparison surface, never the wallet source). | Steps 1–5 below + the env-gated live test |

AC coverage map (AC line → offline test):

| AC line (PROJECT.md §12) | Offline test |
|---|---|
| balance/UTXO/history match … known history | `test_ac1_known_history_store_matches_explorer_truth`, `test_ac4_store_view_narration_inputs_match_truth` |
| … including >20-address gaps | `test_ac3_gap20_misses_deep_usage_rescan_gap30_finds_it` |
| rescan fixes a simulated stale cache | `test_ac2_rescan_repairs_stale_cache_to_ground_truth` |
| unit tests for prefix→script-type mapping | `tests/test_wallet_descriptor.py::test_prefix_matrix` (referenced, not duplicated) |

---

## Prerequisites

1. **A mainnet watch key (zpub) with known history including a >20-address
   gap.** To CREATE one deterministically:
   - Start the app on a fresh store:
     `LOCALWALLET_STORE_PATH=<tmp-dir>/store.db LOCALWALLET_ZPUB=<zpub> .venv/bin/python -m localwallet.ui.cli --stub-llm`
     (the interpreter path assumes the project is installed into a local
     `.venv` — verify on your machine; `--stub-llm` makes intent extraction
     deterministic without a local model).
   - Say **"give me a new address" 26 times** → the app allocates receive
     indices 0..25 (store-persisted allocation state).
   - **Fund index 3 and index 25** (the 4th and 26th addresses you were
     given) with a small real amount each, so a used address sits beyond a
     gap-20 window. Mainnet has NO faucet: fund from any mainnet source you
     control (e.g. an exchange withdrawal or a Sparrow/Electrum mainnet
     wallet) — keep the amounts dust-level (a few hundred sats) if you only
     need the gap-behavior proof.
   - Wait for ≥1 confirmation on both funding transactions (check on any
     block explorer, e.g. mempool.space — Step 1).
2. The app installed/importable (`.venv` with `pip install -e .` or
   `PYTHONPATH=src`).
3. `sqlite3` CLI available (one step widens the gap setting).

---

## Step 1 — Record the explorer ground truth

For **each funded address** (and any address you expect coins on), record the
ground truth from the app's wallet source AND an independent cross-read:

- Electrum (the app's wallet source): a trusted mainnet Electrum server (the
  default is `ssl://electrum.blockstream.info:50002`). Query the funded
  addresses' confirmed balance, unconfirmed balance, exact UTXO set
  (`txid:vout`, sats, confirmation status), and transaction list.
- Bitcoin Core (the self-hosted wallet source): `getreceivedbyaddress`,
  `listunspent`, and the address's tx history — or any block explorer over the
  same node.
- mempool.space (comparison surface only — never the wallet source):
  - address view: `https://mempool.space/address/<ADDRESS>`
  - transaction detail: `https://mempool.space/tx/<TXID>`

Record: confirmed balance, unconfirmed balance, the exact UTXO set
(`txid:vout`, sats, confirmation status), and the transaction list.

**Cross-check caveats:** any public explorer / Electrum server is a
trust/privacy tradeoff — use a server you trust, and prefer your own node's
data for the numeric reference. mempool.space serves the same Esplora API
shape the app reads for fees/prices, so it is a useful but not independent
cross-check; the wallet numbers the app shows come from Electrum/bitcoind.
If an independent Electrum/Core cross-check is unavailable at run time,
record that in the sign-off rather than fabricating it.

## Step 2 — Run the app

```sh
LOCALWALLET_STORE_PATH=<tmp-dir>/store.db LOCALWALLET_ZPUB=<zpub> \
    .venv/bin/python -m localwallet.ui.cli --stub-llm
```

Inside the chat:

- `what's my balance` → confirmed + unconfirmed + total sats, tip height
- `show my utxos` → one line per UTXO (address verbatim, sats, confirmation)
- `show my recent transactions` → newest-first history (unconfirmed first)

## Step 3 — Comparison checklist

- [ ] Confirmed / unconfirmed / total sats match the recorded explorer
      truth **exactly** (no rounding, no off-by-one sat).
- [ ] UTXO set matches exactly: same `txid:vout` pairs, same values, same
      confirmation flags.
- [ ] History: same transaction count; ordering newest-first with
      unconfirmed first; directions (in/out/self) sane vs the explorer.
- [ ] **Gap behavior:** right after funding index 25, a default-gap (20)
      scan does **not** yet show those coins (window stops at index 23) —
      the app balance is LOWER than the explorer total, and this is
      expected R3 behavior, not a bug.
- [ ] After Step 4 the balance/UTXO/history match the explorer exactly.

## Step 4 — Rescan after funding beyond the window

The default gap is 20 (ADR-0009; no auto-widen). Widen the setting and run
the repair scan:

```sh
sqlite3 <tmp-dir>/store.db "INSERT INTO settings(key,value) VALUES('gap_limit','30') \
    ON CONFLICT(key) DO UPDATE SET value='30'"
LOCALWALLET_STORE_PATH=<tmp-dir>/store.db LOCALWALLET_ZPUB=<zpub> \
    .venv/bin/python -m localwallet.ui.cli --stub-llm --rescan
```

Expected: the rescan re-derives every window address from the key, finds
the index-25 usage, restores a balance/UTXO/history that now matches the
explorer exactly, and prints the beyond-window usage notice (the same
warning `sync_state["out_of_window_detected"]` carries). Re-run the Step 3
checklist — everything must now match.

## Step 5 — Env-gated automated live cross-check

```sh
LOCALWALLET_E2E_LIVE=1 LOCALWALLET_AC_ZPUB=<zpub> \
    pytest tests/test_phase1_ac.py -k live -s
```

Runs the real `scan_wallet` (gap 30) against the PUBLIC ELECTRUM mainnet
server (`ssl://electrum.blockstream.info:50002`) and
prints a comparison sheet — balance totals, utxo count, tx count,
per-branch `max_used_index`, first/last window address — for the human
sign-off. It asserts **structural sanity only** (no exception, non-negative
totals, cursor set); the numeric comparison against the explorer remains
the human's job. Skipped unless both env vars are set. (Mempool.space is a
comparison surface only; wallet data comes from Electrum/bitcoind,
TCK-DESCOPE-M4.)

## Pass criteria

| # | Criterion | Evidence |
|---|---|---|
| 1 | Confirmed/unconfirmed/total sats == explorer truth (exact sats) | Step 3 checklist + Step 5 sheet |
| 2 | UTXO set identical (txid:vout, value, confirmed flag) | Step 3 + `show my utxos` |
| 3 | History identical (count, order, directions) | Step 3 + `show my recent transactions` |
| 4 | >20-gap: default-gap miss observed, gap-30 rescan finds it, warning surfaced | Step 3 (gap bullet) + Step 4 |
| 5 | Electrum agrees (or unavailability recorded with reason) | Step 1 note |
| 6 | Offline composite story green (`pytest tests/test_phase1_ac.py`) | CI / local run |

---

## Honest notes

- **Electrum cross-check availability must be verified when this procedure
  is run** (a trusted server is required). If unavailable, the sign-off
  records it; do not fabricate an Electrum cross-check.
- The public Electrum server (or any public explorer you consult) sees every
  queried address together with your IP — a public-backend privacy caveat
  (ADR-0003 / PROJECT.md §9); a self-hosted Electrum/Core backend avoids it.
- **No SPV-level verification claim:** Electrum/Core responses are not
  SPV-provable — integrity rests on TLS to a trusted operator until the
  Phase 4 self-hosted backend (ADR-0003). The AC "match" is a consistency
  check against explorers, not cryptographic proof; UI copy must not
  over-claim.
- The offline composite proves app-view == *hand-constructed fixture*
  truth; only this live run closes the gap to real chain data.
- Watch-only throughout: paste a **zpub/xpub**, never an xprv or seed phrase
  (the app refuses them in chat, with guidance).
- Mainnet has no faucet: the funding step uses a small real amount. Quote
  every address/amount verbatim from tool output — never retype them.

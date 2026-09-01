# Phase 2 Acceptance Criteria — Sparrow harness (TCK-P2-006)

Phase 2 AC (PROJECT.md §12): *"produced unsigned PSBT loads correctly in an
external tool (Sparrow) with matching outputs/fee"*.

The external-tool step is inherently **MANUAL**: a human opens Sparrow and
imports the file. This document is the manual procedure; the companion
`tests/test_sparrow_ac.py` makes everything that *can* be automated
deterministic and is its offline harness.

The AC is verified at **two layers**:

| Layer | What it proves | Where |
|---|---|---|
| **Offline determinism harness** (runs in CI, no network) | The fixture PSBT is byte-identical across builds, its base64 form is pinned, and every field (outputs, fee, vsize, inputs, sequences, derivations) is asserted plus an independent fee/change re-derivation. | `tests/test_sparrow_ac.py` |
| **LIVE procedure** (this document, manual) | The literal AC: the exact fixture PSBT imports cleanly into Sparrow and Sparrow's own numbers (outputs, fee) match. | Steps 1–3 below + the env-gated artifact dump |

AC coverage map:

| AC line (PROJECT.md §12) | Offline test |
|---|---|
| produced unsigned PSBT … loads correctly in an external tool | `test_structure_matches_meta_exactly` (shape validated by `validate_psbt_shape` + embit round-trip parse) |
| … with matching outputs/fee | `test_fee_and_change_recomputed_independently` (fee == vsize × rate; change == inputs − amount − fee) |
| deterministic fixture | `test_base64_is_byte_deterministic` (base64 pinned byte-for-byte) |

**Honest note (what Sparrow can and cannot validate):** Sparrow's PSBT
import validates *structure* and *recomputes the fee from the transaction
itself* (from the witness UTXOs and the outputs). It **cannot** validate our
*coin-selection policy* (why these inputs were chosen, change-vs-dust
decisions, RBF intent) — that is covered by unit tests, see
`tests/test_tx_selection.py` and `tests/test_tx_psbt.py`. This procedure
proves the PSBT is a structurally valid, fee-consistent BIP174 transaction;
it does not re-prove selection.

---

## The fixture (exact numbers)

The canonical fixture is the two-input P2WPKH case from
`tests/test_tx_psbt.py` (same BIP32 test-vector-1 key), rate **2 sat/vB**:

- **Inputs:** `ab`×32 : v0 = **40_000 sats** (receive branch, index 3) and
  `cd`×32 : v0 = **50_000 sats** (change branch, index 4). Canonical order
  `ab` < `cd`. Inputs total **90_000 sats**.
- **Recipient (output 0):** `tb1q5pdvjqq2xdlppkg9hhcemdusvjlkrh0wwrd5h9` —
  **60_000 sats**.
- **Change (output 1):** `tb1qfldesjqc6l7a05mmg2afxyxwcts5vcxjzkgfk4` —
  **29_582 sats**.
- **Fee:** **418 sats** (90_000 − 60_000 − 29_582 = 418; also 209 vB × 2
  sat/vB = 418 — independent cross-check).
- **vsize:** **209 vB**.
- **Pinned base64:** `CANONICAL_BASE64` in `tests/test_sparrow_ac.py`
  (byte-identical across builds; no uuid/timestamp in the serialization).

These addresses/amounts are quoted from the tool output (the engine, not
the model, produces them). The fixture PSBT derives only from this
public-key material — no secrets anywhere.

---

## One-time setup

### 1. Sparrow (verify-current)

Use a current Sparrow release (Sparrow's PSBT/PSBTv0 handling and fee
display change between versions). Check <https://sparrowwallet.com/download/>
for the latest; record the version used in the sign-off. Sparrow ≥ 1.8
handles PSBTv0 and BIP84 watch-only wallets well.

### 2. Testnet4 mode

Sparrow must run in **testnet4** mode:

- **macOS:** `File ▸ Preferences ▸ Server ▸ Network`, or launch with the
  testnet4 profile. If your Sparrow build only offers "Testnet" (testnet3),
  **verify-current**: recent releases expose testnet4 (e.g. via
  `File ▸ New Wallet ▸ Testnet` after enabling the network, or the
  `--network testnet4` / config `network=testnet4` flag). Testnet3 vs
  testnet4 do **not** share addresses — the wrong network mode will reject
  or misdisplay everything.
- Watch the status bar: it must show **testnet4**, not testnet/mainnet.

### 3. Import the watch-only wallet

The app is watch-only: paste the **descriptor**, never an xprv/seed.

In Sparrow: `File ▸ New Wallet ▸ Import ▸ Paste` and paste the descriptor
from the app's wallet engine. The descriptor has the shape

```
wpkh([<fingerprint>/84'/1'/0']vpub.../0/*)  and  wpkh([<fingerprint>/84'/1'/0']vpub.../1/*)
```

(as two branches `{0,1}/*` in the canonical single string — Sparrow splits
them for you). You can set a wallet name and password (the password encrypts
Sparrow's local copy of the *public* descriptor; it is not a signing key).

**Where to get the descriptor:**

- **SQLite query** against the app's store (the `wallets` table stores the
  canonical checksummed descriptor verbatim):
  ```sh
  sqlite3 <store-path>/store.db \
      "SELECT name, descriptor FROM wallets ORDER BY id;"
  ```
  (`descriptor` is the full checksummed string, e.g.
  `wpkh([fp/84'/1'/0']vpub…/{0,1}/*)#checksum`.)
- **Or a tiny python snippet** printing it via the wallet engine:
  ```python
  from localwallet.wallet import WalletDescriptor
  print(WalletDescriptor.from_key("<vpub>").descriptor)
  ```
  (substitute your testnet4 watch key; the engine gate refuses mainnet).

For this AC you do **not** strictly need a fully funded wallet — the import
accepts the descriptor and shows the derived receive/change addresses so you
can visually confirm the derivation matches the fixture's addresses.

---

## Step 1 — Build the fixture PSBT

Run the env-gated dump test to write the base64 PSBT + a JSON summary into a
temp dir (prints the paths; add `-s`):

```sh
LOCALWALLET_DUMP_PSBT=1 .venv/bin/python -m pytest \
    tests/test_sparrow_ac.py -k dump -s
```

This writes `sparrow_fixture.psbt` (the pinned base64 as plain text) and
`sparrow_fixture.json` (outputs/fee/vsize summary) under pytest's `tmp_path`
— **never** into the repo. Grab both files for the steps below.

Equivalently, the fixture is built by the exact snippet in
`tests/test_sparrow_ac.py` (`build_fixture()`), reusing the canonical test
inputs from `tests/test_tx_psbt.py`.

> The `.psbt` file is a **base64 text file** — save/export it as such (a
> single line of base64, no wrapping). Sparrow's `From File…` reader accepts
> base64 PSBT text.

## Step 2 — Import into Sparrow

1. `File ▸ Open Transaction ▸ From File…`
2. Select `sparrow_fixture.psbt`.
3. Sparrow loads the unsigned transaction. It may warn that inputs are
   unconfirmed/unknown — fine (these UTXO txids are synthetic fixture
   material that do not exist on testnet4); the point is that the structure
   and fee are valid.

## Step 3 — Verify in Sparrow's UI

- **Inputs count:** exactly **2**.
- **Recipient:** address `tb1q5pdvjqq2xdlppkg9hhcemdusvjlkrh0wwrd5h9`,
  amount **60_000 sats**.
- **Change:** address `tb1qfldesjqc6l7a05mmg2afxyxwcts5vcxjzkgfk4`, amount
  **29_582 sats**.
- **Fee:** **418 sats** and the effective rate shown ≈ **2 sats/vB**.
  Sparrow recomputes the fee from the transaction, so the **418 sats must
  match exactly**. The *rate* display is Sparrow's own
  fee/vsize division and may differ from 2 by ±0.01 sat/vB due to vsize
  rounding (`209 vB × 2 = 418` exactly, but Sparrow's displayed vsize and
  rounding can show e.g. 2.00 or 2.01) — the rate is the **only**
  approximate field.

## Pass criteria

| # | Criterion | Expected (from fixture) | Status |
|---|---|---|---|
| 1 | Imports without a structure error | PSBT parses | ☐ |
| 2 | Input count | 2 | ☐ |
| 3 | Recipient address + amount | `tb1q5pdvjqq2xdlppkg9hhcemdusvjlkrh0wwrd5h9` = 60_000 sats | ☐ |
| 4 | Change address + amount | `tb1qfldesjqc6l7a05mmg2afxyxwcts5vcxjzkgfk4` = 29_582 sats | ☐ |
| 5 | Fee (sats) | 418 (exact — Sparrow recomputes it) | ☐ |
| 6 | Fee rate (sats/vB) | ≈ 2 (within ±0.01, rounding-only) | ☐ |
| 7 | Offline harness green | `pytest tests/test_sparrow_ac.py` | ☐ |

Sign-off records: Sparrow version, testnet4 confirmed, all boxes ☑, and any
deviations (should be none).

## What "FAIL" looks like + likely causes

| Symptom | Likely cause |
|---|---|
| Sparrow refuses to open / "invalid PSBT" | Corrupt base64 (wrapped lines, stray whitespace) or the file isn't plain base64 text; or Sparrow on the wrong network; or a stale Sparrow without PSBTv0 support. Re-export via the dump test; verify the first line starts `cHNidP8`. |
| Addresses/amounts mismatch | Imported into the **wrong network mode** (testnet3 vs testnet4 — addresses don't transfer); or pasted the wrong descriptor; or edited the fixture. Addresses are quoted verbatim from tool output — recheck against Step 1 / Step 3. |
| Fee (sats) mismatch | Tampered PSBT (an edited input witness-UTXO value or an output value changes the recomputed fee) — re-export a fresh fixture; do not hand-edit. |
| Inputs shown unconfirmed/unknown | Expected — the synthetic UTXO txids do not exist on testnet4; this is not a failure of the AC (structure/fee are the object under test). |
| Rate display not ≈2 | Display-only rounding; the exact sats are the source of truth (criterion 5). |

---

## Honest notes

- Sparrow validates structure + fee (it recomputes fee from the tx itself,
  so 418 must match **exactly**); it **cannot** validate our *selection
  policy* — that is covered by unit tests
  (`tests/test_tx_selection.py`, `tests/test_tx_psbt.py`).
- Watch-only throughout: paste a **vpub/descriptor**, never an xprv or seed
  phrase (the app refuses them in chat, with guidance).
- The synthetic UTXO txids are fixture material, not real coins — nothing
  here is funded or broadcast; the object under test is the unsigned PSBT's
  structure and fee.
- The offline harness proves the PSBT is what the engine intends
  (byte-identical, field-asserted); only this manual run closes the gap to
  "loads correctly in the external tool".

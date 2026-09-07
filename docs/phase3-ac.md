# Phase 3 Acceptance Criteria — Live device + on-chain procedure (TCK-P3-006)

Phase 3 AC (PROJECT.md §12): *"end-to-end mainnet send with at least one
real device (e.g. Coldcard file flow + one USB device); tampered-PSBT
fixture is caught deterministically; broadcast verified on-chain;
device-absent/locked error flows behave."*

The AC is verified at **two layers**:

| Layer | What it proves | Where |
|---|---|---|
| **Offline composite story** (runs in CI, no network, no hardware) | The full wiring catches a tampered PSBT deterministically *before* broadcast; the whole lifecycle (create → dual-key confirm → sign → revalidate → broadcast → status → store history) runs through the REAL REPL handlers with fake devices; device-absent/locked/file-missing/broadcast-fail error flows behave; a second broadcast is refused. | `tests/test_phase3_ac.py` |
| **LIVE procedure** (this document, MANUAL) | The literal AC with real hardware: an end-to-end mainnet send signed on a real device (Coldcard SD flow **and** one USB device), broadcast verified **on-chain**, and the device error flows observed live. | Steps 1–6 below |

**Honest status:** the literal device AC is **deferred-run** until hardware
is available. Everything that *can* be automated is automated and green in
`tests/test_phase3_ac.py`; this document is the manual script for the day a
Coldcard + one USB device are on a desk. Per-row device quirks remain
**device-verified-later** until that run (see `docs/device-notes.md`, ADR-0014
§4, ADR-0015).

AC coverage map (AC line → offline test):

| AC line (PROJECT.md §12) | Offline test |
|---|---|
| tampered-PSBT fixture is caught deterministically | `test_ac1_tampered_psbt_caught_before_broadcast_at_wiring_level` |
| end-to-end mainnet send (offline half) | `test_ac2_full_lifecycle_file_signer_production_path` |
| broadcast verified on-chain | **LIVE Step 5** (mempool.space txid lookup) |
| device-absent/locked error flows behave | `test_ac3_device_absent_and_locked_guidance_then_retry` (+ `test_ac3_signed_file_missing_guidance`, `test_ac3_broadcast_5xx_then_retry`) |
| (terminal-state invariant) | `test_ac4_double_broadcast_refused` |

---

## Prerequisites

1. **A funded mainnet watch key (zpub)** — follow `docs/phase1-ac.md`
   ("Prerequisites" + "Step 1") to create/fund one and record the explorer
   truth. Confirm ≥1 block on at least one UTXO worth more than what you
   will send plus the fee.
2. **Two real devices** (the AC names both paths):
   - a **Coldcard** (or another SD-file-flow device — Passport/SeedSigner)
     for the airgap path (ADR-0014), and
   - one **USB device** (Ledger/Trezor — HWI USB path, ADR-0015).
3. **Sparrow** (optional cross-check) — see `docs/sparrow-ac.md` for the
   watch-only import; not required for this AC but useful to independently
   see the signed transaction before broadcast.
4. A working `local-wallet` CLI (`.venv` with `pip install -e .`, or
   `PYTHONPATH=src`).
5. The offline harness green: `pytest tests/test_phase3_ac.py`.

> Watch-only throughout: the app handles a **zpub / descriptor**, never an
> xprv or seed phrase (it refuses them in chat, with guidance). Device PINs
> and passphrases are entered **on the device** — never into the app.

---

## Coldcard / SD file flow (airgap, ADR-0014)

### Step 1 — Run the app with the file signer

```sh
LOCALWALLET_SIGNER=file \
LOCALWALLET_SIGNER_DIR=<sd-mount-or-transfer-dir> \
LOCALWALLET_ZPUB=<zpub> \
    .venv/bin/python -m localwallet.ui.cli
```

`LOCALWALLET_SIGNER_DIR` is the SD card mount (or a plain folder used as the
transfer medium). The signer writes `localwallet-unsigned-*.psbt.b64` +
`.sha256` sidecars here and imports `localwallet-signed-*.psbt.b64` back
(ADR-0014 conventions).

### Step 2 — Create and confirm

In the chat:

- `send <amount> sats to <bc1 address>` → the confirmation card (amount,
  recipient, fee, size, change, USD) is narrated **verbatim from tool
  output**.
- On the **Coldcard screen**, the same unsigned transaction is what you will
  approve; the card's recipient/amount must match.
- `yes please` → dual-key confirm (the user utterance **and** the model's
  `confirm_tx` quoting the card's `Ref:` from FACTS) → **"Approved."**, flow
  goes to `CONFIRMED`.

### Step 3 — Sign (export to the transfer folder)

- `sign` → the app exports the unsigned PSBT to the transfer folder and
  prints the §10 handoff line naming the path and the **expected signed
  filename**:
  `Exported to <path>. Move it to your SD card, sign on your device, then
  save the signed file back and tell me the path (say: signed
  localwallet-signed-<ref8>.psbt.b64).`
- Move the unsigned file onto the Coldcard's SD card (if `LOCALWALLET_SIGNER_DIR`
  is the SD mount it is already there). Sign on the Coldcard: **the device
  screen is the trust anchor (§9/§10)** — compare the amount and recipient
  on the device against the card before approving on-device. The Coldcard
  writes back a signed PSBT (it may rename it; the app imports any
  `localwallet-signed-*.psbt.b64` by convention — ADR-0014 records this as
  device-verified-later).

### Step 4 — Import (sign again)

- `sign` again → the app finds the signed file, **re-parses and
  deterministically re-validates** it against the intended transaction, and
  prints:
  `Signed and verified ✓ txid <txid>. Ready to broadcast — say 'broadcast'.`
- If the returned file carries no checksum sidecar, the app appends
  `Note: the signed file had no checksum sidecar — integrity not verified.`
  (allowed, ADR-0014) — the **re-validation gate still runs regardless**.
- **A tampered/mismatched signed PSBT hard-stops here** (nothing signed or
  sent, flow stays `CONFIRMED`; see Error-flow checklist). This is the same
  deterministic gate the offline AC-1 test exercises.

### Step 5 — Broadcast + ON-CHAIN verification (the literal AC)

- `broadcast` → the app POSTs the re-validated transaction (single attempt)
  and prints `Sent! txid <txid> — tracking…`.
- **THE on-chain verification step:** open the mempool.space **mainnet**
  transaction view for the quoted txid:
  `https://mempool.space/tx/<txid>`
  (API: `https://mempool.space/api/tx/<txid>` and
  `https://mempool.space/api/tx/<txid>/status`).
  - The page must show the **same recipient address and amount** and a
    sane fee vs the card.
  - **No SPV-level claim:** this is an Esplora/TLS consistency check, not
    cryptographic proof (PROJECT.md §9, ADR-0003) — the operator sees your
    IP; the honest privacy indicator applies.

### Step 6 — Status (confirmation)

- After ~1 block, `status of txid <txid>` → `Confirmed at height <N>.`
  (the model quotes the txid verbatim from the FACTS block — never invents
  it). If queried too early, the app says the transaction may not be indexed
  yet (eventual consistency) — retry shortly.

---

## USB device flow (HWI, ADR-0015)

Run the same app but with the HWI signer:

```sh
LOCALWALLET_SIGNER=hwi \
LOCALWALLET_ZPUB=<zpub> \
    .venv/bin/python -m localwallet.ui.cli
```

1. Plug in the device. If it is locked/uninitialized, the first `sign`
   expects the **locked-device guidance first** (the device cannot yet prove
   its fingerprint — ADR-0015 refuses with locked guidance, not a mismatch):
   `Enter your PIN/passphrase on the device, then say 'retry'.`
2. Unlock on the device, then `sign` (or `retry`) → the fingerprint gate
   (exactly one match, post-open re-check) → the device signs.
3. **Device-screen comparison (§9 — the trust anchor):** verify the
   **amount and recipient shown on the DEVICE screen** match the
   confirmation card **before approving on the device**. The chat summary is
   an aid, not proof.
4. `broadcast` → verify on-chain as in Step 5. `status of txid <txid>` →
   confirmed after ~1 block.

> **Fingerprint mismatch (ADR-0015):** if a device is plugged in but its
> fingerprint does not match the wallet descriptor, the app hard-stops with
> a value-free mismatch message — it never signs with the wrong device and
> never asks "sign anyway?". Unplug the wrong device (or check the
> descriptor) and retry. Two devices claiming the same fingerprint → the app
> asks you to unplug extras.

---

## Error-flow checklist (expected narration)

Run each against the relevant signer to confirm the LIVE behavior matches
the offline harness:

| Scenario | How to trigger | Expected narration | Offline test |
|---|---|---|---|
| **Device absent** | HWI, nothing plugged in | `No device found — plug in and unlock your device, then say 'retry'.` — flow stays `CONFIRMED`; plug in + retry succeeds | `test_ac3_device_absent_and_locked_guidance_then_retry` |
| **Device locked** | HWI, device locked | `Enter your PIN/passphrase on the device, then say 'retry'.` — flow stays `CONFIRMED` | same |
| **Fingerprint mismatch** | HWI, different device plugged in | value-free mismatch ("the connected device does not match this wallet") — hard stop, no signing | `tests/test_signer_hwi.py` (unit) |
| **Signed file missing** | File signer, not yet returned from device | `Exported to … Move it to your SD card … (say: signed localwallet-signed-….psbt.b64).` — flow stays `CONFIRMED` | `test_ac3_signed_file_missing_guidance` |
| **Revalidation failure** | File signer returns a tampered signed PSBT | `The signed transaction failed verification (…) — nothing was signed or sent; try signing again.` — flow stays `CONFIRMED`, **zero broadcast POSTs** | `test_ac1_tampered_psbt_caught_before_broadcast_at_wiring_level` |
| **Broadcast fail → retry** | Chain POST returns 5xx | `Broadcast failed (…) — the signed transaction is kept; say 'broadcast' to retry.` — flow stays `SIGNED`; retry succeeds | `test_ac3_broadcast_5xx_then_retry` |
| **Double broadcast** | broadcast_tx again after `BROADCAST` | `Not broadcast — no signed transaction to broadcast.` — **no second POST** | `test_ac4_double_broadcast_refused` |

---

## Pass criteria

| # | Criterion | Evidence | Status |
|---|---|---|---|
| 1 | Offline composite harness green | `pytest tests/test_phase3_ac.py` (and full `pytest tests/ -q`) | ☑ (automated) |
| 2 | Tampered PSBT caught deterministically, before any POST | AC-1 test + live file-mode tamper | ☐ (live) |
| 3 | Coldcard SD flow: full send signs, broadcasts, verifies on-chain | Steps 2–6 | ☐ (live) |
| 4 | USB device flow: signs (after locked guidance), broadcasts | USB flow | ☐ (live) |
| 5 | On-chain verification: mempool.space mainnet shows the quoted txid with matching recipient/amount/fee | Step 5 | ☐ (live) |
| 6 | Confirmation surfaced (`status` → confirmed at height) | Step 6 | ☐ (live) |
| 7 | Error flows behave as narrated (absent/locked/mismatch/file-missing/revalidation/broadcast-retry) | Error-flow checklist | ☐ (live) |
| 8 | Device quirks recorded/amended | `docs/device-notes.md`, ADR-0014 §4 | ☐ (live) |

Sign-off records: device models + firmware versions, Sparrow (optional)
version, mempool.space txid(s), the confirmed heights, and any deviations.
The literal device rows (3–8) are **deferred-run** until hardware is
available; rows 1–2 (and the whole offline composite) are green now.

---

## Honest notes

- **The literal device AC is deferred-run until hardware is available.**
  The offline harness proves the *wiring*; only this live run closes the
  gap to a real device and the real chain.
- **Device quirks are device-verified-later.** Every row in
  `docs/device-notes.md` and the ADR-0014 §4 per-device layout table is
  *expected* friction, not yet confirmed. This run either confirms or
  amends them (R4).
- **Fingerprint trust** is a hard gate (ADR-0015): mismatch never signs and
  is never "confirmed through"; locked devices are reported as locked, not
  as mismatched (the right device must not be slandered before it can
  identify itself).
- **Device screen is the trust anchor (§9).** Verify amount/address on the
  device against the card before approving on-device. The chat summary is an
  aid, never a proof.
- **On-chain verification is a consistency check, not SPV proof** (PROJECT.md
  §9, ADR-0003): mempool.space is a public Esplora the operator associates
  with your IP; the honest privacy indicator applies. Phase 4 moves address
  queries to a self-hosted node.
- **Watch-only throughout:** paste a **zpub/descriptor**, never an xprv or
  seed phrase. PINs/passphrases are entered on the device, never into the
  app.
- Mainnet sends move REAL value: keep the live-run amount small, and quote
  every address/amount verbatim from tool output (see `docs/phase1-ac.md`,
  `docs/sparrow-ac.md`).

# Device notes — per-device quirks (R4 register)

Status: **placeholder — device-verified-later.** Everything below records
*expected* friction from HWI internals, vendor docs, and community reports.
Phase 3's acceptance criterion requires at least one real device end-to-end
(PROJECT.md §12); each row is confirmed or amended during that run and at
the Phase 6 device matrix (Ledger/Trezor/Coldcard/Jade minimum). No row
below has been verified against real hardware yet.

Scope: `HwiUsbSigner` (TCK-P3-003) — USB path via HWI-as-a-library. The
airgap/file path (ADR-0014) has its own conventions and is not covered here.

## Per-device quirk table (all rows: device-verified-later)

| Device | Expected quirk (unverified) | Impact on our flow | Action when verifying |
|---|---|---|---|
| Ledger | Wallet-policy / descriptor registration friction: recent Ledger firmware wants an external (multisig or non-standard) wallet policy registered on-device before it will display addresses/sign for it; HWI's singlesig display path may prompt on-device approvals. Fingerprint reporting requires the device unlocked. | `display_address` (descriptor-based) may be slow or fail on first use; sign flow may add on-device prompts. | Run a descriptor display + sign; record firmware version, policy prompts, and whether `displayaddress` works without pre-registration. |
| Trezor | Passphrase sessions: passphrase-protected wallets need the passphrase per session. Trezor T/B can take it **on device**; Trezor One expects host-side passphrase entry (a secret our layer never handles — see below). Locked devices enumerate with an `error` entry and no fingerprint. | Locked device → our `DeviceLockedError` guidance. A passphrase-protected One cannot complete our flow without HWI's own interactive/host passphrase handling — document the workaround (disable passphrase, or use HWI CLI interactively) rather than handling secrets. | Verify enumerate-while-locked shape (error key, missing fingerprint), on-device passphrase entry on T/B models, and model string (`trezor_t`, `trezor_1`…). |
| Coldcard (USB) | PSBT size limits over USB: the Coldcard splits interactive USB communication into ~1–2 KB chunks; large PSBTs (many inputs) are slow or hit limits — historically the SD-file path is preferred for big transactions. | USB sign for large PSBTs may be impractical; narration should suggest the file path for many-input transactions. | Sign with a realistic multi-input PSBT; find the practical size ceiling and record it. |
| Jade | Firmware-version floor enforced by HWI (`DeviceNotReadyError` below minimum); network/chain selection errors surface as `DeviceConnectionError` ("Device is locked"). Jade reports fingerprints only when unlocked to the matching network. | Our chain default is testnet4 (ADR-0004) — verify Jade accepts it via HWI; version errors map to our locked/not-ready guidance. | Confirm testnet4 signing works, record the HWI-enforced minimum firmware. |
| BitBox02 | Pairing: first connect requires an on-device pairing confirmation; unpaired attempts raise `DeviceNotReadyError` with HWI's unpaired message. Attaching requires the device unlocked. | First-use flow needs an extra on-device approval step — narration should mention "approve the pairing on your device". | Record pairing UX, whether repeated prompts occur, model string. |
| KeepKey | Same Trezor-lineage lock flow (`DeviceNotReadyError` "Keepkey is locked…"); passphrase handling similar to Trezor One (host-side). Older firmware quirks with recent trezorlib are possible. | Locked → `DeviceLockedError` guidance; passphrase-protected units may be unusable without host-side secret entry (out of scope for our layer). | Verify enumerate/sign on current firmware; record any trezorlib-version coupling. |

## Secrets policy (applies to every row)

Device PINs and passphrases are **entered on the device** (or through HWI's
own interactive flows). `localwallet` never accepts, stores, transmits, or
logs a PIN or passphrase: our HWI calls always pass `password=None`. A
device that needs unlocking surfaces as user guidance ("enter your
PIN/passphrase on the device, then say 'retry'") — never as a host-side
prompt. This keeps the zero-secrets invariant (PROJECT.md §4, §9).

## HWI version pinning

- `hwi` is a runtime dependency in `pyproject.toml` with a lower bound only
  (`hwi>=3.1`); the exact version validated for this ticket is
  **hwi 3.2.0** (installed with its dependency set: hidapi, libusb1, cbor2,
  cryptography, ecdsa, mnemonic, noiseprotocol, protobuf, pyaes, pyserial,
  semver, six, cffi, pycparser).
- HWI's device-facing API and error surfaces have shifted across versions
  (e.g. `commands.signtx(client, psbt)` taking base64-str in 3.x vs bytes
  in older lines). `HwiUsbSigner` is written version-tolerant (str/bytes
  handling), but the **verified** reference is 3.2.0 — when bumping, re-run
  the live device check and update this note.
- R5 (USB/HID permissions & drivers — macOS app packaging, Windows
  WinUSB/libusb) is tracked separately in PROJECT.md §13; HWI ships udev
  rule helpers for Linux (`hwilib install-udev-rules`), macOS uses the HID
  stack directly, Windows needs vendor WinUSB drivers — to be verified when
  packaging spikes start.

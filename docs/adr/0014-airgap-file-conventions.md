# ADR-0014: Airgap file conventions — base64 text, naming, checksums, directories

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decides:** PROJECT.md §14 OQ17 — "Airgap file conventions: PSBT encoding
  (binary vs base64), filename scheme, checksums, per-device folder layouts."
- **Scope:** `src/localwallet/signer/file.py` (FilePsbtSigner, TCK-P3-001);
  the CLI device-handoff narration (PROJECT.md §10); later HWI/QR signers
  share the same conventions where applicable. Relates to ADR-0002 (envelope),
  ADR-0004 (testnet-only), ADR-0012 (tx engine), ADR-0013 (send-flow states).
- **Device verification:** all device-family guidance below is
  **device-verified-later**. Phase 3's acceptance criterion runs at least
  one real device (PROJECT.md §12 Phase 3 AC). Until that live run, this
  ADR records the *intended* conventions; concrete per-device tweaks
  (e.g. a device's own rename-on-save behavior) will amend this record.

## Context

The airgap signer writes an unsigned PSBT to a transfer medium (microSD,
USB stick, or a plain folder) and imports the signed PSBT back. This is the
Phase 3 first-class path (PROJECT.md §7.6). OQ17 leaves the encoding,
filename scheme, checksums, and per-device directory layout open. Those
choices affect cross-OS/SD compatibility, human inspectability, sortability,
collision safety, and tamper detection — all of which matter for a flow whose
trust anchor is the hardware device screen (§10).

## Decision

### 1. Encoding: base64 **text** (never binary)

- The unsigned PSBT is written as base64 text, one logical line, with a
  trailing newline.
- **Rationale:** base64 is cross-OS/SD-safe (no binary mangling from FAT32
  line-ending or copier transformations), human-inspectable (a user can
  eyeball the text and verify the magic prefix), and renders cleanly in file
  managers and copy-paste. Binary `.psbt` files are fine for direct device
  import on some devices, but text is the safest lowest-common-denominator
  for a transfer folder that may move between macOS, Windows, and the
  device's own filesystem. FilePsbtSigner accepts/emits base64 text only.
- On import the text is base64-decoded and must start with the BIP174 magic
  `b"psbt\xff"` (structural check before any parse).

#### Amendment (2026-09-07, TCK-PSBT-001)

Each unsigned export additionally writes a **binary** sibling file
`localwallet-unsigned-<ref>.psbt` containing the raw BIP-174 bytes (the
base64-decoded form of the `.b64` text, byte-identical to
`base64.b64decode(...)`). Rationale: wallet interop — external wallets such
as Sparrow open binary `.psbt` files natively, so a user can hand the
transfer folder directly to such a wallet. The `.b64` text remains the
canonical localwallet export (human-inspectable, cross-OS/SD-safe) and the
only form import reads; the binary sibling is an additive convenience file
and is never used for import. Overwrite-refusal semantics are unchanged and
apply to the sibling as well (same content idempotent, different content
refused). The sibling is public data (a PSBT carries no keys), so it does not
violate the "never write anything else to the transfer directory" rule.

### 2. Filename scheme

- **Unsigned export:** `localwallet-unsigned-<ref>.psbt.b64`
- **Signed import:** `localwallet-signed-<ref>.psbt.b64`
- **Checksum sidecar (per export):** `<unsigned-name>.sha256` i.e.
  `localwallet-unsigned-<ref>.psbt.b64.sha256`
- `<ref>` is the first 8 characters of the transaction reference:
  - if the reference is non-empty and hex/alnum-only, used as-is (truncated);
  - otherwise the first 8 hex chars of its SHA-256 (so a hostile or sloppy
    reference can never inject a path separator or break out of the folder).
- **Rationale:**
  - **Sortability:** fixed prefixes (`unsigned-` vs `signed-`) sort the two
    directions apart; the stable `<ref>` prefix keeps files for one
    transaction adjacent.
  - **Collision safety:** the `<ref>` prefix makes cross-transaction
    collisions unlikely, and FilePsbtSigner additionally refuses to
    overwrite an existing unsigned file with *different* content (same
    content is idempotent) and refuses a reference that would collide with
    an existing signed file. Fail closed on any ambiguity.

### 3. SHA-256 checksum sidecars

- Every export writes a `<name>.sha256` sidecar: the hex SHA-256 digest of
  the file's exact bytes plus a newline.
- **On import, a present sidecar MUST match** the file's SHA-256 — a
  mismatch is a hard, value-free refusal (fail closed). An **absent**
  sidecar is allowed and reported via `SignedResult.checksum_verified =
  False`, so callers can surface "integrity not verified" in narration
  without blocking a legitimate device-produced signed file that carries no
  sidecar.
- **Rationale:** the sidecar catches accidental corruption or partial
  copies during the human SD-card shuffle. Making mismatch fatal (rather
  than a warning) matches the project's fail-closed posture (§5.5). Absent
  sidecars stay allowed because devices generally do not write our sidecar,
  and a requirement to do so would break the plain "device returns a signed
  file" path.

### 4. Directory conventions (GUIDANCE table — device-verified later)

The app hands the user a single transfer folder (SD mount, USB stick, or a
plain directory on disk). Within it, `FilePsbtSigner` writes both
directions using the prefixes above; it does not require a subfolder split.

| Device family | Intended layout | Notes (to verify on real device, Phase 3 AC) |
|---|---|---|
| Coldcard | SD card root (`/`); user saves the unsigned file on the SD, device renames on save | Coldcard saves under its own name and may rename on save; our import scans any `localwallet-signed-*.psbt.b64` in the folder, so the device's rename is expected harmless — verify at the Phase 3 device run. |
| Passport / SeedSigner | SD root or a single folder | Similar to Coldcard; import scans by filename convention, not fixed path. |
| Generic / USB stick | one folder, both directions OK | Default: both `unsigned-` and `signed-` live in the same transfer folder. |
| Generic (optional) | `from/` and `to/` subfolders | Documented as an option for users who prefer strict direction separation; `FilePsbtSigner` targets one directory, so this is a caller choice, not enforced here. |

All rows are marked **device-verified-later**: Phase 3's AC (PROJECT.md §12)
requires at least one real device, at which point these rows are confirmed
or amended.

### 5. Security note: PSBTs are public data

- A PSBT carries **no keys** — it is public transaction data (inputs,
  outputs, public-key derivations). Writing it to a transfer folder is
  therefore safe.
- **Never write anything else to the transfer directory** — no xpubs, no
  descriptors with fingerprints, no wallet material, no secrets. The
  transfer folder is untrusted third-party-adjacent media; only the PSBT
  and its checksum belong there. FilePsbtSigner writes exactly those files
  (PSBT text, its checksum sidecar, and the binary sibling) per export and
  nothing else.

## Consequences

- `FilePsbtSigner` (TCK-P3-001) implements encoding, naming, checksums, and
  refusals per this ADR: export validates the payload (base64 → magic →
  parse) before writing, then writes payload + sidecar atomically; import
  validates filename convention → sidecar (must-match-if-present) → base64 →
  magic → signature presence (an unsigned PSBT is refused). All errors are
  value-free. Import returns the stripped file text verbatim.
- Import acceptance is NOT broadcast authorization — the deterministic
  re-validation gate (`tx/revalidate.py`, PROJECT.md §7.5) hard-stops before
  broadcast.
- The CLI device-handoff narration (PROJECT.md §10) uses
  `FilePsbtSigner.list_pending_exports()` to describe what the user should
  save to the device, and reports `checksum_verified` on import.
- Later signers (HWI USB in TCK-P3-003, QR in v2) share the base64-text and
  value-free-error conventions where applicable; per-device file nuances
  are captured as they are verified.
- The per-device directory table above is a living record: Phase 3's real
  device run either confirms or amends it.

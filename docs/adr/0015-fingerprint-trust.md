# ADR-0015: Fingerprint trust — exactly-one-match gate, mismatch is a hard stop

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decides:** PROJECT.md §14 OQ18 — "Fingerprint/descriptor trust flow: what
  UX when device xpub fingerprint doesn't match the descriptor the user
  supplied (wrong wallet? typo? attack?)."
- **Scope:** `src/localwallet/signer/hwi.py` (HwiUsbSigner, TCK-P3-003);
  narrated device-handoff moments (PROJECT.md §10, wired in TCK-P3-005);
  relates to ADR-0012 (tx engine), ADR-0013 (send-flow states), ADR-0014
  (airgap conventions — the file signer carries the fingerprint inside the
  descriptor text itself, so the device-side check there is inherent).
- **Device verification:** the per-device behavior of fingerprint reporting
  (locked devices omitting it, model naming) is **device-verified-later**
  via the Phase 3 AC real-device run (PROJECT.md §12); see
  `docs/device-notes.md`.

## Context

The app is watch-only: the wallet is defined by a descriptor whose origin
carries the master-key fingerprint (e.g. `wpkh([fp/84'/1'/0']vpub/…)`). When
a USB hardware wallet signs, the app must be sure the connected device is
*the* device that wallet was set up with — otherwise signatures are produced
under the wrong key and the send fails downstream (or, worse, a user is
walked into "signing" a transaction their keys can't validly sign, with
confusing device-side errors). OQ18 asks what happens when the device
fingerprint and the wallet descriptor's fingerprint disagree — and whether
that difference could be a wrong wallet, a typo'd descriptor, or an attack.

## Decision

1. **The fingerprint match is a gate, not a preference.** Before any
   signing (and before on-device address display), the signer enumerates
   connected devices and selects devices whose master fingerprint equals
   the wallet's expected fingerprint.

2. **Exactly one match is required.**
   - **Zero devices connected** → refuse: "plug in and unlock your device".
   - **Devices present, none match** → refuse with a value-free mismatch
     message ("the connected device does not match this wallet"). If every
     present device's fingerprint was *unreadable* (locked/uninitialized
     device — hwilib reports the failure instead of a fingerprint), the
     refusal is the locked-device guidance instead: the device may well be
     the right one, but it cannot prove it until unlocked.
   - **More than one match** → refuse and ask the user to unplug extras.
     Two devices claiming the same wallet fingerprint is an ambiguity we
     will not resolve silently (same seed imported twice, seed reused, or a
     clone — each is a reason to stop and look).

3. **Mismatch is a hard stop.** The signer never proceeds to sign with a
   device that failed the match, and never asks the user "sign anyway?".
   The failure is terminal for that attempt; the only way forward is to
   connect the matching device (or re-check the descriptor) and retry.

4. **Error messages are value-free.** Neither the expected nor the observed
   fingerprint is echoed into chat, logs, or error envelopes. Fingerprints
   are public-ish, but they are wallet-linking material (they appear in
   descriptors and on-chain origin hints), and the logging policy (§7.8)
   keeps identifiers out of error reports. The message names the *problem*
   and the *next action*, never the values.

5. **Verify-on-device address display is the companion flow.** Where the
   device supports it, the app offers to display the address derived from
   the wallet descriptor on the matched device's screen ("Now compare the
   address on your device…" — §10). This closes the loop against a typo'd
   descriptor: a descriptor with a wrong key/branch produces an address the
   device and the app disagree on, which the user sees. It is a
   recommendation in narration (best-effort, device-dependent support), not
   a hard gate — the fingerprint gate above is what blocks wrong-key
   signing; the display step is what makes a *wrong descriptor* visible
   before funds move. UX wiring lands in TCK-P3-005.

### Rationale

- **Wrong wallet:** the most common real-world case — the user owns two
  devices. Signing with the wrong one produces a PSBT the re-validation
  gate would ultimately reject anyway (inputs not consumable by that key),
  but the failure surfaces as cryptic device errors or a meaningless
  "invalid signature" far from the cause. Refusing at the gate with plain
  language is the honest failure point.
- **Typo'd descriptor:** a hand-mangled fingerprint or pasted-from-the-wrong-
  wallet descriptor describes a wallet the user doesn't hold. The mismatch
  message points at the descriptor/wallet pairing instead of letting the
  user chase signature errors. The display-address flow makes the
  discrepancy concrete ("the device shows a different address").
- **Attack:** a swapped or rogue device (or a malicious companion app
  whispering a different xpub) cannot produce signatures under the wallet's
  key. Failing closed on mismatch denies the attacker a "sign anyway"
  foot-gun. Combined with the deterministic signed-PSBT re-validation
  before broadcast (§7.5), a wrong-key signature can never reach the
  network.
- **Exactly-one-match:** removes enumeration-order nondeterminism. Silent
  "first match wins" is exactly the kind of maybe the project's fail-closed
  principle (§5.5) forbids.

### Rejected alternatives

- **Silent proceed on mismatch** (sign with whatever is plugged in):
  rejected — it converts every failure mode above into downstream mysteries
  and removes the one cheap identity check available before signing.
- **Prompt-through on mismatch** ("the device doesn't match — sign
  anyway?"): rejected — an ambiguous confirm on a security-relevant
  mismatch trains users to click through warnings, and the answer is
  useless in the attack case: a user cannot distinguish a typo from a swap
  at that moment. The correct action is always "stop, connect the right
  device"; the flow should make the right action the only action.
- **Match by label/model instead of fingerprint:** rejected — labels are
  user-set and spoofable; models are shared across devices. The
  fingerprint is the only value bound to the wallet's actual key material.
- **Trust-on-first-use (remember the first device seen):** rejected for v1 —
  TOFU anchors trust in whatever was plugged in first, which is precisely
  wrong in the "typo'd descriptor" case; the descriptor's fingerprint is
  the anchor, and the device must present it.

## Consequences

- `HwiUsbSigner` (TCK-P3-003) implements the gate in `_select_device`:
  zero/one/many handling as above, value-free messages, mapped device
  errors (absent / locked / busy / mismatch / unavailable).
- The send-flow narration (TCK-P3-005) treats a `DeviceMismatchError` as a
  terminal state for the sign step — no auto-retry, no bypass; the user is
  told to connect the matching device.
- The file signer (ADR-0014) is unaffected: its descriptor-embedded
  fingerprint travels with the PSBT and the device validates it against its
  own key at signing time; this ADR governs the USB path where the app
  performs the check.
- Locked-device ambiguity (fingerprint unreadable) is reported as the
  locked-device guidance, not as a mismatch — the right device must not be
  slandered before it can identify itself.

## Amendment (TCK-HW-001, 2026-09-07): Jade pinserver relay inside the signer call stack

With `requests` installed (required by hwilib's import-guarded Jade support),
a locked Blockstream Jade authenticates during *client construction*: hwilib
drives the on-device scrambled PIN pad and relays **blinded blobs** to the
Jade pinserver over outbound HTTPS, inside the `get_client` (and `enumerate`)
call path. This puts transient network I/O in the signer's *call stack* —
not in its code: `localwallet.signer` still imports no network module, so the
static-import lint (`tools/lint_network.py`) is unaffected and `chain/`
remains the only module of ours with network imports.

The no-secrets invariant is preserved: the PIN is entered on the device and
never touches the host process; the host only ferries blinded pinserver
blobs it cannot read. Host-driven-PIN devices (e.g. a locked Trezor,
`needs_pin_sent=True`) are **not** wired to promptpin/sendpin relaying — the
signer only names the companion-app unlock flow in its guidance. Wiring such
relaying would put PIN material through the host and would need its own ADR.

## Amendment #2 (TCK-HW-002, 2026-09-07): the gate binds the ACCOUNT key, not the master fingerprint

**What changed.** Decision 1's "devices whose master fingerprint equals
the wallet's expected fingerprint" was unimplementable and is replaced:
`hwilib`'s `enumerate` reports the device's **master** fingerprint
(`jade.py:549` via `get_master_fingerprint`), while the value the app
holds — and always held — is the **account key's own fingerprint**
(`parsed.hd_key.my_fingerprint`, the descriptor origin, e.g. at
`m/84'/0'/0'`). For any account-level zpub the two differ by
construction, so the exactly-one-match gate could never pass for any
device (MW-4 live blocker: the user's Jade reported master `40dbb192`
against an expected account fingerprint; every sign attempt died with
the mismatch guidance).

**The re-framed gate.** The trust anchor moves from "the device's master
fingerprint equals X" to "**the device controls the wallet's account
key**": the signer opens each readable enumerated candidate and asks the
OPEN client for the public key at the descriptor's account path
(`get_pubkey_at_path` — the base `Client` contract, implemented by every
supported client incl. JadeClient at `jade.py:164`), and binds it via
`hash160(pubkey)[:4]` (BIP 32) against the descriptor origin fingerprint.
Zero matches → mismatch hard stop (unchanged semantics, unchanged
guidance); more than one → unchanged; a client that cannot serve the
account key fails closed — the check is never skipped (the former
injected-fake skip is gone). Enumeration now only narrows
locked/unreadable devices for guidance; its master fingerprint decides
nothing. Because binding happens on the open client, the
enumerate→open TOCTOU window the post-open re-check closed is closed by
construction.

**Why watch-only cannot know more.** The master fingerprint is not
recoverable from an account-level key (`wallet/descriptor.py` documents
this); the app holds xpubs only, so no amount of device cooperation
lets it predict what `enumerate` reports. The account fingerprint is the
strongest identifier the wallet actually possesses.

**What still binds the money.** The device gate proves device↔wallet-key
identity; **`tx/revalidate.py` remains the binding gate for actual
signatures** — a bound-but-wrong signature against the intended
transaction is still a hard stop before broadcast, unchanged.

**Fuller future path.** OQ18 device registration (enrollment records the
device's master fingerprint ↔ descriptor pairing at setup time, enabling
pre-open candidate selection and per-device identity across wallet
changes) remains the complete answer; it is still unwired in v1, and
this amendment is deliberately the smallest honest anchor that works
for watch-only without it.


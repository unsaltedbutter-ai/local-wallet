# ADR-0008: Native segwit (P2WPKH) is the v1 script type; nested/legacy read-only; taproot deferred

- **Status:** Accepted
- **Date:** 2026-08-31
- **Decides:** PROJECT.md OQ6 ("Script types in v1: native-segwit only, or
  also taproot send/receive?")
- **Scope:** wallet engine parse/descriptor/derivation/scan surface
  (TCK-P1-002); future send flow (Phase 2+); device policy work (R4).
  Relates to ADR-0009 (gap policy) and OQ18 (descriptor/fingerprint trust).

## Context

The wallet engine must decide which output script types it parses,
derives, scans, and (later) spends from. Inputs are SLIP-132 extended
public keys whose version bytes encode the script type (`zpub`/`vpub` =
P2WPKH, `ypub`/`upub` = P2SH-P2WPKH, `xpub`/`tpub` = P2PKH), and the
choice ripples into descriptor handling, hardware-wallet policy
registration, and test fixtures (R4: per-device policy friction).

## Decision

- **v1 receive AND (future) send: native segwit P2WPKH only**
  (BIP84, purpose 84'). Canonical descriptor:
  `wpkh([fp/84'/1'/0']vpub/{0,1}/*)`.
- **P2SH-P2WPKH (BIP49) and legacy P2PKH (BIP44): read-only support.**
  The engine parses their keys, builds checksummed descriptors
  (`sh(wpkh(...))`, `pkh(...)`), derives and scans their addresses, and
  reports their balances/history — but v1 send flows target P2WPKH
  wallets only.
- **Taproot (BIP86) deferred to v2:** no `tr()` descriptor support, no
  key-path spending, no P2TR scan surface in v1.
- Revisit triggers: sustained user demand for taproot receive, Phase 6
  device-matrix results showing taproot policy registration is smooth
  across the supported signer set, or a v2 multisig decision that
  changes the descriptor surface.

## Rationale

- P2WPKH is the smallest correct v1 surface: one descriptor template,
  one device-policy shape, one dust/fee-size model (computed from script
  size, never hardcoded), one set of fixtures. Every additional script
  type multiplies the money-path review surface at the Phase 2/3
  security gates.
- Nested segwit and legacy remain visible because users arrive with
  `ypub`/`xpub` wallets in hand; refusing them would make balances
  invisible ("where did my money go?"). Read-only support reuses the
  exact same parse/derive/scan machinery with a per-type descriptor
  wrapper, so the added risk is small and contained.
- Taproot is deferred primarily for **R4 friction**: Ledger-class
  devices require wallet-policy registration for `tr()` descriptors and
  per-device quirks are documented as a Phase 3 budget item already;
  adding taproot now would couple an unproven device layer to a new
  script path. Descriptor complexity (taptree/`tr()` policy wording) and
  the absence of testnet taproot demand close the case for v2.

## Consequences

- `detect_script_type` maps only the six supported SLIP-132 public
  prefixes; `Zpub`/`Vpub` (P2WSH) and taproot variants are refused with
  value-free errors (fail closed, never guessed).
- The send engine (Phase 2) may assume P2WPKH change/receive scripts;
  if read-only wallet types ever gain send support, that is a new ADR.
- Device policy registration work in Phase 3 covers P2WPKH only until
  the revisit triggers fire.

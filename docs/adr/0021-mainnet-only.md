# ADR-0021: Mainnet-only — the testnet development path is removed, not kept as fallback

- **Status:** Accepted (owner decision, supersedes ADR-0004 for this
  project)
- **Date:** 2026-09-06
- **Decides:** The network for all development and operation of
  local-wallet: mainnet only. Testnet code paths, gates, fixtures, and
  default endpoints are **removed**; there is no testnet fallback mode.
- **Scope:** wallet layer descriptor/derivation gates (TCK-MAIN-001);
  protocol/chain/signer/app testnet remnants (TCK-MAIN-002/003);
  invariant text amendments (TCK-MAIN-004). Relates to ADR-0004
  (superseded), ADR-0003 (chain backend), ADR-0008 (script types —
  unchanged, only network/coin-type flips).

## Context

ADR-0004 chose testnet4 for all development up to the Phase 6 mainnet
gate, per the original PROJECT.md §12 testnet-only invariant. The owner
has now decided the project is **mainnet-only**, superseding that
choice and the invariant it rested on.

The load-bearing security properties of the project do not depend on
testnet: the app is watch-only (xpubs only, zero secrets in process,
disk, or logs) and keys live exclusively on the hardware wallet. The
residual risk of developing against mainnet is therefore reduced to
**information leakage** — privacy exposure of the operator's own
addresses, amounts, and timing to whichever chain backend is
configured. The owner explicitly accepts that residual risk.

## Decision

- local-wallet is **mainnet-only**. The wallet engine's gates are
  flipped, not dual-mode: testnet extended public keys (``vpub``,
  ``upub``, ``tpub`` and any testnet-serialized key) are **refused** at
  parse time, structurally at descriptor construction, and again at
  derivation (the pre-existing belt-and-suspenders layering is kept).
- The canonical account path is the BIP44/49/84 **mainnet coin type
  0'**: `wpkh([fp/84'/0'/0']xpub|zpub/{0,1}/*)` and siblings. Script
  types and descriptor-string format conventions (ADR-0008) are
  unchanged.
- Testnet fixtures, endpoints, and address encodings in the wallet
  layer are replaced with mainnet equivalents; testnet detection tables
  are retained solely so testnet material can be *detected and refused*
  with precise, value-free errors.
- Watch-only and private-key-refusal invariants are unchanged and
  remain load-bearing.

## Rationale

- **Hardware-key custody collapses the testnet risk calculus.** The
  original reason for testnet-only was that signing/broadcast bugs
  could destroy real funds during development. With keys exclusively on
  the hardware wallet and the process holding xpubs only, the process
  cannot lose funds by key mishandling; what remains is information
  leakage about the operator's own wallet, which a self-hosted backend
  mitigates further. The owner accepts this trade.
- **Removing (not dual-moding) testnet keeps the validation surface
  small.** A fallback path would double the gated surface (two HRPs,
  two coin types, two fixture families) for a mode that, post-decision,
  has no user. Refusal-with-precise-error preserves diagnostics without
  preserving a code path.

## Consequences

- **Real funds in dev flows.** Sign/broadcast paths (TCK-MAIN-002/003
  scope) deserve extra care: confirmation gates, PSBT re-validation,
  and broadcast were designed as if mistakes cost testnet coins; they
  now cost real ones. No code change in this ticket — an operational
  posture note for the remaining mainnet tickets.
- **The public-API default exposes real addresses to the operator.**
  The default public Esplora backend (ADR-0003) sees the operator's
  addresses/amounts; a **self-hosted node/electrs backend is
  recommended early** (ADR-0016/0017 already cover localhost node I/O).
  The honest banner about public-backend exposure is retained.
- **Real fees/prices.** The testnet sentinel/degenerate-price degrade
  paths become edge cases rather than the daily default; price/fee
  handling (ADR-0011) operates on live mainnet data from the first run.
- **Invariant amendments** to AGENTS.md/PROJECT.md ("testnet-only until
  the Phase 6 mainnet gate") are tracked in the docs ticket
  TCK-MAIN-004; this ADR is the authoritative record of the owner
  decision (2026-09-06).
- **Supersedes ADR-0004** for this project. ADR-0004's original text is
  retained there with a superseded-by note.

# ADR-0004: Testnet4 for all development work

> **SUPERSEDED by ADR-0021 (2026-09-06):** local-wallet is mainnet-only;
> the testnet development path is removed, not kept as fallback. The
> original decision text below is retained for the record.

- **Status:** Superseded by ADR-0021 (was: Accepted)
- **Date:** 2026-08-31
- **Decides:** PROJECT.md OQ16 ("Testnet choice: testnet3 vs testnet4 vs
  signet for all dev work")
- **Scope:** all development and testing through the Phase 6 mainnet gate;
  the default `esplora_base_url` in `localwallet.config.Settings`; faucet /
  workflow documentation. Relates to ADR-0003 (MVP chain backend).

## Context

Phases 0–5 are testnet-only (PROJECT.md §12) and the default chain backend
is the public mempool.space Esplora API (ADR-0003). The chosen network must
(a) be served by that backend's Esplora API so the zero-setup Phase 0 path
works, (b) have a workable faucet workflow compatible with the 10-minute
north-star onboarding, and (c) be stable enough that wallet balances can be
cross-checked against Electrum / mempool.space references (Phase 1 AC).

## Decision

**Testnet4** is the development network for all work up to the Phase 6
mainnet gate:

- default `Settings.esplora_base_url = "https://mempool.space/testnet4/api"`;
- Phase 0 wiring hard-asserts testnet keys and refuses mainnet material
  (TCK-P0-006);
- tests and docs reference testnet4 addresses and fixtures only.

## Rationale

- **testnet3 is degraded/aged.** Decades of accumulated state, a UTXO set
  bloated by spam/dust, and long stagnant-difficulty epochs make block and
  confirmation timing erratic; its faucet experience is unreliable. Upstream
  Bitcoin Core added testnet4 precisely as the fresh successor network.
- **mempool.space serves testnet4** with the Esplora API the MVP depends on
  (ADR-0003), so the required "public explorer + zero local infra" path
  works out of the box.
- **Signet lacks public explorer/faucet parity for the MVP.** Public signet
  exploration and coin distribution are thinner and less standardized for a
  non-technical audience: there is no turnkey public signet story matching
  the mempool.space testnet4 Esplora-plus-faucet workflow this project
  commits to, and self-hosting explorer + coins infrastructure would
  contradict the Phase 0 zero-setup requirement. Signet remains attractive
  later for controlled, deterministic testing (see fallbacks).

## Fallbacks / future work

- The base URL is plain configuration: **Phase 4** can point the same
  `EsploraClient` at a **self-hosted Esplora instance for testnet4**
  (mempool/electrs) — or at a self-hosted signet stack — without code
  changes; only settings differ.
- Revisit this choice only if testnet4 itself degrades; that would be a new
  ADR superseding this one.

## Faucet / workflow note (placeholder)

To be filled with verified, step-by-step testnet4 funding instructions —
faucet URL(s), expected confirmation times, and a "how much to request"
guideline — once exercised end-to-end during Phase 0/1 dogfooding. Do not
ship unverified faucet URLs in user-facing docs.

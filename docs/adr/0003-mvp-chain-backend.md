# ADR-0003: MVP chain backend — public mempool.space Esplora for Phases 0–3

- **Status:** Accepted
- **Date:** 2026-08-31
- **Decides:** PROJECT.md OQ5 ("MVP chain backend: public mempool.space
  acceptable for Phase 0–3, or require node earlier?")
- **Scope:** `src/localwallet/chain/` (ticket TCK-P0-003); relates to
  ADR-0004 (network choice) and PROJECT.md §9, R7, R11.

## Context

Phase 0's acceptance criteria require a live testnet balance with **zero
local infrastructure** ("What's my balance?" returns a correct live testnet
balance), and the north-star flow is "here is my zpub" → "I saw a payment
confirmed" in under 10 minutes. The chain adapter is the only networked
module (PROJECT.md §5.6, §7.4), and §9 fixes exactly what may leave the
machine in the MVP. The open question was whether the MVP may depend on a
public block-explorer API or must require a user-run node from day one.

## Decision

1. The default chain backend for **Phases 0–3** is the public Esplora API at
   `https://mempool.space/testnet4/api` (network choice per ADR-0004),
   reached through `EsploraClient` in `src/localwallet/chain/esplora.py`:
   GET-only address-history / address-UTXO / tip-height queries, no API keys.
2. The backend sits behind the `EsploraClient` interface; **Phase 4 replaces
   it with a self-hosted instance** (self-hosted mempool/electrs Esplora;
   Bitcoin Core RPC is a further option) **behind the same interface**, so
   callers and higher layers do not change shape.
3. **Honest privacy caveat (recorded, not glossed over — R7, §9):** with a
   public explorer, the operator sees every queried address together with
   the requesting IP and can associate/cluster them. This is the asterisk on
   the "nothing leaves your computer" promise:
   - the UI MUST display a warning of the form "Querying public
     mempool.space — the operator can associate these addresses with your
     IP" whenever the public backend is active (§9; Phase 4 flips the
     indicator when the backend becomes local);
   - UI copy must **never over-claim verification**: Esplora responses are
     not SPV-provable; integrity rests on TLS to a trusted operator until a
     self-hosted backend exists.
   The xpub/descriptor itself never leaves the machine in any phase.
4. Availability/rate-limit risk (R11) is handled inside the adapter, not by
   contract: bounded retries with exponential backoff + jitter on 429/5xx/
   connection errors only; every other failure fails closed as
   `ChainError` with scrubbed (address-free) messages.

## Alternatives considered

- **Require an own node from day 1 — rejected:** it blocks the Phase 0
  acceptance criteria and the 10-minute north-star flow (a non-technical
  user cannot be asked to complete initial block download as a prerequisite);
  node onboarding is Phase 4's node-doctor work instead.
- **Bitcoin Core RPC first — rejected:** a different API shape from the
  Esplora address/UTXO model, with a wallet-oriented RPC surface the
  watch-only app does not need; it would delay Phase 0. Revisited as a
  Phase 4+ backend option behind the same interface.
- **Electrum-server protocol (electrs/Fulcrum) directly — deferred:** the
  same data is available in Phase 4 from a self-hosted mempool instance in
  Esplora shape, avoiding a second protocol client in the MVP.

## Consequences

- Phases 0–3 ship with the §9 privacy caveat visible in the UI; the status
  ribbon must read "public API" until Phase 4 flips it to "own node".
- Third-party availability is a known limitation; the adapter's bounded
  backoff and fail-closed `ChainError` surfaces are the mitigation.
- Phase 4 must preserve response-shape parity (address txs / UTXOs / tip);
  the fixture shapes in `tests/test_chain_esplora.py` are the contract the
  replacement backend must satisfy.

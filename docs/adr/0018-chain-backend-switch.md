# ADR-0018: Config-only chain backend switch (self-hosted Esplora)

- **Status:** Accepted
- **Date:** 2026-09-03
- **Decides:** How the wallet's chain backend is switched to the user's own
  instance as a **config-only** operation (Phase 4, TCK-P4-002). Relates to
  ADR-0003 (public MVP backend → Phase 4 self-hosted), ADR-0004 (testnet4),
  ADR-0016 (localhost node/ loopback contract). Scope:
  `src/localwallet/chain/`, `src/localwallet/config.py`, the one
  construction site in `src/localwallet/app.py`.

## Context

ADR-0003 chose a public mempool.space Esplora for Phases 0–3 behind the
`EsploraClient` interface, and Phase 4 (§12) replaces it with the user's own
instance *behind the same interface*. The privacy payoff requires that, when
self-hosted, **zero** chain requests reach the public default. The switch
must be a pure configuration change: no code path may bypass the selection,
and the existing public default (ADR-0003) and all existing env vars /
call sites must keep working.

## Decision

1. **A single selection point:** `Settings.chain_base_url`
   (`LOCALWALLET_CHAIN_BASE_URL`), empty by default, is the authoritative
   Esplora base for the WHOLE wallet when set. Resolution lives in exactly
   one function: `ChainConfig.from_settings`, which every `EsploraClient`
   (production or otherwise) flows through when its `base_url` is left at
   the default:

   ```
   chain_base_url set  -> use it (self-hosted or any custom URL)
   chain_base_url unset -> use esplora_base_url (legacy, ADR-0003 public default)
   ```

   Because every EsploraClient-mediated call — address txs/utxos, tip, fees,
   price, and later broadcast — rides the same client, flipping the backend
   is a one-variable config change.

2. **Backward compatibility preserved.** `esplora_base_url` /
   `LOCALWALLET_ESPLORA_BASE_URL` remain the fallback source when
   `chain_base_url` is unset, so existing deployments and the ADR-0003 public
   default are unchanged. `EsploraClient(base_url=...)` still accepts an
   explicit override for test seams and callers that want one.

3. **`LOCALWALLET_LOCAL_MEMPOOL_URL` is NOT the backend selector.** It stays
   the node doctor's loopback **probe target** (ADR-0016): its purpose is
   *detection* ("is a mempool running here?"), it has a non-empty loopback
   default (`http://127.0.0.1:3006`) that would silently flip the backend to
   a possibly-not-running localhost if it were overloaded as the chain source,
   and it is subject to the loopback-only contract. Detection ("detect") and
   backend selection ("use") are deliberately separate: a user points
   `LOCALWALLET_CHAIN_BASE_URL` at whatever serves them Esplora for testnet4
   (their self-hosted mempool, an electrs instance, or a remote node) — this
   is the Phase 4 reach that ADR-0016 explicitly deferred to this ticket.

4. **Testnet-only invariant preserved.** A self-hosted URL does not change
   network semantics. The instance must serve **testnet4** (ADR-0004); the
   client's path shapes (`/address/{addr}/txs`, `/utxo`, `/blocks/tip`,
   `/v1/fees/recommended`, `/v1/prices`, `/tx`) are identical regardless of
   host. No network-affecting behavior is introduced by this ADR.

5. **Fail closed on malformed config.** A selected `chain_base_url` that is
   not an http(s) URL raises a value-free `ValueError` in
   `ChainConfig.__post_init__` at client construction time — never a
   mid-request crash. Timeout/retry/backoff policy and value-free error
   discipline are untouched.

## Alternatives considered

- **Overload `LOCALWALLET_LOCAL_MEMPOOL_URL` as the backend source.**
  Rejected (see Decision 3): its non-empty loopback default would silently
  change the default backend and break the ADR-0003 public path, and it would
  conflate node *detection* with backend *selection* while dragging in the
  ADR-0016 loopback contract that this ticket is meant to extend past.
- **Auto-follow the node doctor's finding** (detected mempool ⇒ auto-switch).
  Rejected for now: it couples detection to selection implicitly, surprising
  users, and is out of this ticket's config-only scope. A later ticket
  (TCK-P4-003) may surface the doctor's guidance for the user to apply the
  `LOCALWALLET_CHAIN_BASE_URL` setting.

## Consequences

- `Settings.chain_base_url` / `LOCALWALLET_CHAIN_BASE_URL` is the single knob;
  `esplora_base_url` remains as the backward-compatible fallback.
- `app.py`'s `EsploraClient` construction no longer hardcodes the public
  default; it relies on the shared selection point.
- New hermetic tests (TCK-P4-002) prove: self-hosted selection routes
  address/txs/utxo/tip queries to the configured instance; zero requests hit
  the public host while self-hosted; the unset/set/malformed selection matrix
  behaves as documented.

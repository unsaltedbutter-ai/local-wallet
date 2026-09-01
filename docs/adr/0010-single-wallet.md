# ADR-0010: Single active wallet profile in v1

- **Status:** Accepted
- **Date:** 2026-08-31
- **Decides:** PROJECT.md OQ19 ("Multi-wallet support: single zpub per
  profile in v1; multi-profile roadmap?")
- **Scope:** CLI/UI exposure, dispatcher handlers (P1-003/P1-004),
  confirmation-card flows; storage schema is unchanged (already
  multi-wallet capable).

## Context

The store schema (TCK-P1-001) supports multiple wallet rows and an
`active_wallet_id` setting. The wallet engine (TCK-P1-002) must decide
whether v1 exposes one wallet or a profile switcher, because every
handler, confirmation card, and destructive flow depends on "which
wallet am I operating on".

## Decision

**v1 exposes exactly one active wallet profile.** The CLI/UI operates on
`settings.active_wallet_id` and there is no profile switcher, no
per-conversation wallet selection, and no multi-wallet narration. The
schema keeps its multi-wallet shape so v2 can add profiles without a
migration.

**Multi-profile is a v2 roadmap item** (revisit when: users demonstrably
keep separate cold/hot watch wallets, or spending wallets beyond the
first are supported).

## Rationale

- **Confirmation-card clarity.** Destructive flows (create → confirm →
  sign → broadcast) must show one unambiguous source of funds. A wallet
  selector adds a "which wallet did I mean?" failure mode exactly where
  mistakes are most expensive, and weakens the device-screen
  cross-check (the device shows one wallet's view; the chat must match
  it without qualification).
- **UX simplicity** for the primary user (§3): one zpub pasted once;
  balance/history/receive address always mean the same thing. This
  matches the north-star onboarding flow ("here is my zpub" → first
  confirmed testnet payment in under 10 minutes).
- **Security-review surface:** single-active-wallet removes
  cross-wallet state-mixing bugs (scan cache, derivation cursors,
  confirmation state machines) from the Phase 1 review.
- The store already isolates all per-wallet state by `wallet_id`
  (addresses, derivation, UTXOs, transactions, sync_state), so nothing
  in v1 leaks across profiles even if a second row existed.

## Consequences

- Handlers and the dispatcher resolve the wallet via
  `store.get_active_wallet()`; if none is configured, the flow asks the
  user to add one (a `clarify`, not a guess).
- `scan_wallet`/`rescan_wallet` accept a wallet row or descriptor and
  resolve it against the store; the UI passes the active row.
- Adding profiles in v2 requires UI/narration work and a
  confirmation-card design pass, not a storage migration.

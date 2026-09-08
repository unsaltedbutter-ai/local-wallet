# Security Policy

## Reporting a vulnerability

Please report security issues privately. Do **not** open a public issue for
anything security-related.

Use **GitHub Security Advisories** on this repository:

1. Go to **Security → Advisories → New advisory**.
2. Provide a description, severity, and affected versions.
3. If you prefer email, use: `SECURITY-REPLACEME@example.com`
   (*replace with the maintainer's real address before publishing*).

You should receive an acknowledgement within a few days. Please give us
reasonable time to fix and release before disclosing publicly.

## Scope

This is a **Bitcoin wallet**. Errors here can lose real money, so treat the
whole codebase as in-scope:

- Value-bearing bugs (incorrect amounts, wrong recipients, fee/dust errors).
- Any path where a private key, extended private key, or seed phrase could be
  handled, logged, or exfiltrated — even though the design is watch-only and
  should never touch them.
- The intent-validation and dispatch layers (the boundary between untrusted
  LLM output and wallet actions).
- The signed-PSBT revalidation path before broadcast.

Out of scope (by design, not bugs):

- **Secrets should not exist in this app at all.** It is watch-only: no xprvs
  or seed phrases are ever handled, so "secret storage is insecure" does not
  apply. Report any place a secret *does* appear — that is a bug.
- Known testnet-key refusals (testnet addresses are intentionally rejected).

## Supported versions

Only the `main` branch is supported. Please test against latest `main`.

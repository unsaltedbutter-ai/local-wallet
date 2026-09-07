# MANUAL-WORK.md — actions only you (the user) can run

> Tick items off by telling the orchestrator the MW-id (e.g. "MW-2 done"). Nothing here blocks code work unless noted.

## MW-1: Grant orchestrator read-only shell commands
- [x] **Superseded 2026-09-06** — the orchestrator's `permission.bash` was expanded far beyond the find/wc/ls allowlist (`"*": allow`); no fresh session needed for this item anymore.

## MW-2: Pinned-model bootstrap — GATES TCK-P6-001
- [x] **Done 2026-09-06** — E2B `Q4_K_M` downloaded (3,106,738,272 bytes, byte-exact), pinned in `manifest.json` (`740185b21d22ceb8…`), `--check` verified. **TCK-P6-001 is unblocked.** (E4B + QAT entries remain unpinned — only needed for the R12 comparison / MW-6 breadth.)
- Original note: Sources are now **ungated** (switched to unsloth mirrors after the 401 on the gated google repos — `df0ef64`) and the E2B download URL has been **live-verified working** (200 + GGUF magic + Range 206). Run the exact command from `models/MODELS.md` (primary: E2B `Q4_K_M`). Note: the sibling ticket TCK-MODELS-002 is adding the google QAT `q4_0` alternative (`gemma-4-E2B_it-qat-q4_0_gguf`) for the R12 comparison.
- Unblocks: enforced ≥95% golden gate (TCK-P6-001 is sequenced BEHIND this — the gate must be adjudicated on the pinned GGUF, not the ADR-0007 bridge), R1/R12 E2B-vs-E4B check, ADR-0006 perf measurement.

## MW-3: Fund a MAINNET wallet — literal Phase 1 AC (was "testnet4"; mainnet per ADR-0021)
- [x] **Done 2026-09-06** — funded a **MAINNET** wallet (the "testnet4" wording predates the ADR-0021 mainnet-only flip, which supersedes it). Follow docs/phase1-ac.md (run CLI with fixture vpub, new_address a few times, fund, `--rescan`).
- Unlocks: funded-wallet literal ACs + real send-flow demos. Since the wallet is now funded, the Phase 1 literal AC (docs/phase1-ac.md live cross-check) is now **runnable**.

## MW-4: Hardware-wallet live AC (Phase 3)
- [ ] Plug in a device (Coldcard SD flow and/or any HWI USB device); follow docs/phase3-ac.md (real-device sign → on-chain broadcast).
- Record quirks in docs/device-notes.md (per-device table is device-verified-later).

## MW-5: Sparrow import AC
- [ ] Follow docs/sparrow-ac.md (manual import of the deterministic fixture PSBT; pass criteria in the doc).

## MW-6: Model-mode eval record (after MW-2)
- [ ] **Available now (MW-2 done).** Run evals in model mode against the pinned GGUF; record results (bridge scores in TASKS.md are explicitly NOT the record per ADR-0007).

## MW-7: Post-fix export spot-check (ready — TCK-SEC-001 landed 55f8d6d)
- [ ] In the REPL: run a create_tx with real amounts, then `/export`; verify amounts render as `<amount>`, txids as `<txid>`, addresses as `<addr>`, and a pasted 12-word test phrase as `<seed>`.

## MW-8: Phase 6 packaging prerequisites (later)
- [ ] Apple Developer account (signed/notarized macOS builds) + Windows box (driver/packaging matrix, OQ10).

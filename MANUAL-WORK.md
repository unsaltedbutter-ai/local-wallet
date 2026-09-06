# MANUAL-WORK.md — actions only you (the user) can run

> Tick items off by telling the orchestrator the MW-id (e.g. "MW-2 done"). Nothing here blocks code work unless noted.

## MW-1: Grant orchestrator read-only shell commands
- [ ] Edit `.opencode/agents/orchestrator.md` → `permission.bash` allowlist: add `"find*": "allow"`, `"wc*": "allow"`, `"ls*": "allow"` (keeps deny-all posture, unblocks repo scans). May need a fresh session to take effect.

## MW-2: Pinned-model bootstrap — GATES TCK-P6-001
- [ ] `cd /Users/butter/local-wallet/models && python3 download_model.py --model gemma-4-E2B-it-Q4_K_M --write-hash` (large download).
- Unblocks: enforced ≥95% golden gate (TCK-P6-001 is sequenced BEHIND this — the gate must be adjudicated on the pinned GGUF, not the ADR-0007 bridge), R1/R12 E2B-vs-E4B check, ADR-0006 perf measurement.

## MW-3: Fund a testnet4 wallet — literal Phase 1 AC
- [ ] Follow docs/phase1-ac.md (run CLI with fixture vpub, new_address a few times, faucet-fund, `--rescan`).
- Unlocks: funded-wallet literal ACs + real send-flow demos.

## MW-4: Hardware-wallet live AC (Phase 3)
- [ ] Plug in a device (Coldcard SD flow and/or any HWI USB device); follow docs/phase3-ac.md (real-device sign → on-chain broadcast).
- Record quirks in docs/device-notes.md (per-device table is device-verified-later).

## MW-5: Sparrow import AC
- [ ] Follow docs/sparrow-ac.md (manual import of the deterministic fixture PSBT; pass criteria in the doc).

## MW-6: Model-mode eval record (after MW-2)
- [ ] Run evals in model mode against the pinned GGUF; record results (bridge scores in TASKS.md are explicitly NOT the record per ADR-0007).

## MW-7: Post-fix export spot-check (after TCK-SEC-001 lands)
- [ ] In the REPL: run a create_tx with real amounts, then `/export`; verify amounts render as `<amount>`, txids as `<txid>`, addresses as `<addr>`, and a pasted 12-word test phrase as `<seed>`.

## MW-8: Phase 6 packaging prerequisites (later)
- [ ] Apple Developer account (signed/notarized macOS builds) + Windows box (driver/packaging matrix, OQ10).

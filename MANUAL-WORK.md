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
- [x] **Done 2026-09-07 (Jade):** device detect + on-device PIN unlock + PSBT verification + SIGNING all working (fixes TCK-HW-001/002/003 landed first). On-chain broadcast intentionally deferred — see MW-9. Findings along the way drove TCK-HW-001..004 and UX feedback (tx-card redesign, UTXO notes/selection design).
- Device quirks: record in docs/device-notes.md (per-device table is device-verified-later) — Jade master fp shown on screen (40dbb192), PIN unlock driven by hwilib client construction (blind-PIN pinserver relay; requests dep required).

## MW-9: Broadcast AC (deferred from MW-4, later)
- [ ] Complete the lifecycle on-chain: sign with the Jade → broadcast → verify on an explorer (docs/phase3-ac.md final step). The user chose not to move sats yet; run when ready.

## MW-5: Sparrow import AC
- [ ] Follow docs/sparrow-ac.md (manual import of the deterministic fixture PSBT; pass criteria in the doc). NOTE 2026-09-07: the fixture changed in TCK-HW-003 (change-output derivation added, base64 re-pinned 476→556 chars) — re-dump the env-gated artifact before importing so you import the current fixture; docs/sparrow-ac.md needs no edit (artifact is generated at dump time).

## MW-6: Model-mode eval record (after MW-2)
- [x] **Done 2026-09-07** — official pinned-GGUF record: 26/53 = 49.1% (golden 21/30, redteam 5/23), exit 1 from the ENFORCED gate — recorded and ACCEPTED as the outcome (misses are model-quality limits; redteam "misses" are the by-design structural-gate case). Runs landed with TCK-P6-001/AGT-001 (a63e24e/46e80d9). Do not re-adjudicate or tune prompts/temperature to chase it.

## MW-7: Post-fix export spot-check (ready — TCK-SEC-001 landed 55f8d6d)
- [ ] In the REPL: run a create_tx with real amounts, then `/export`; verify amounts render as `<amount>`, txids as `<txid>`, addresses as `<addr>`, and a pasted 12-word test phrase as `<seed>`.

## MW-8: Phase 6 packaging prerequisites (later)
- [ ] Apple Developer account (signed/notarized macOS builds) + Windows box (driver/packaging matrix, OQ10).

## MW-10: Web-UI manual matrix (UNBLOCKED 2026-09-08 — full web UI live: buttons, settings panel, scan chip)
- [ ] localhost matrix: macOS/Windows/Linux × VPN/proxy/firewall-on; both `localhost` and `127.0.0.1` URLs; multi-tab; kill-and-reconnect replay (Last-Event-ID); confirm/cancel/sign buttons vs CLI parity.

## MW-11: Web-UI browser check (after the WEB-002 client lands)
- [ ] Open the launch URL in a real browser: page loads under CSP, token island works (no 401), a full turn renders (narration lines stream as text), action buttons fire canonical utterances, kill-the-server reload replays via Last-Event-ID, connection-status transitions, XSS spot-check (a narration containing <img onerror> renders as text). Complements the web-builder agent's static audit; feeds TCK-WEB-003/004. NEW 2026-09-08, also exercise: settings panel (gear/toggle — change gap_limit, see chain_base_url restart + env-override notes, out-of-range rejection), scan-status chip during startup ("wallet loading" until first scan completes), create_tx refused with the friendly line if you try to send pre-first-scan, kill-server reload replays with the "some earlier events may be missing" notice.

## MW-12: Publish to GitHub (TCK-DIST-003 prepared everything)
- [ ] Follow docs/publish.md exactly: sanity-check the flagged strings first — decide whether `notible.local` (your LAN hostname, in HANDOFF/TASKS/ADR-0007/remote_runtime) and `192.168.1.50` (tests only) stay or get scrubbed BEFORE the public push; fill the SECURITY.md email placeholder; then push to github.com/unsaltedbutter-ai/local-wallet and flip repo settings (default branch, Actions on, branch protection).
- Unblocks: the /install route on the landing page (it redirects to raw.githubusercontent on main).

## MW-13: Deploy the landing page to unsaltedbutter.ai (TCK-DIST-002 staged the files)
- [ ] Copy website/app/* into ~/unsaltedbutter/web per website/README.md (src/app vs app mapping table included), `npm run build`, `pm2 restart`. No nginx change expected (/ and /install already proxy to :3000).
- [ ] Verify: page renders (dark mode too), copy button works on the HTTPS origin, `curl -fsSL https://unsaltedbutter.ai/install | bash` returns the script (after MW-12 push + only when install.sh is on main).
- NOTE: files were authored blind (no Next.js on this machine) — if Next 16 complains, the likely culprits are metadata/route-handler conventions; report the build error and I'll fix.

## MW-14: install.sh smoke on a clean machine (optional, after MW-12)
- [ ] On any spare mac/Linux: `curl -fsSL https://unsaltedbutter.ai/install | bash` (or run ./install.sh from a fresh clone) with INSTALL_ROOT=<tmp>; confirm OS/arch detection, uv + Python 3.12 (<3.14) install, venv boot, model-download prompt defaults to NO, next-steps output. Report failures verbatim.

## MW-15: Live run — new fee + UX behavior (the 2026-09-08 changes)
- [ ] Start a send and check the new fee line: FAST should now bid near the mempool floor (your 0.3–0.5 sat/vB morning → expect 1 sat/vB, not 2) and the Pay line shows `@ $/BTC` instead of `rate age`. Say "faster" twice — the second time it should ASK for a sat/vB rate; answer with a number (e.g. "3") and the rebuild should go through the normal confirm flow. Also try an explicit rate from the start ("send 100000 sats to <addr> at 5 sat/vB"). Startup should NOT block: the prompt appears immediately with dots finishing in the background.
- Report anything that looks wrong — fee estimator, ceiling ask, rate display, and scan behavior are all new today.

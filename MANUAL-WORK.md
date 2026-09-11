# MANUAL-WORK.md — actions only you (the user) can run

> Tick items off by telling the orchestrator the MW-id (e.g. "MW-16 done"). Nothing here blocks code work unless noted.

## WHAT THE ORCHESTRATOR IS WAITING ON (priority order — 2026-09-09 state)

1. **MW-16 — backend live verification on your Start9 machine** (the newest code; highest value): bitcoind, electrum ssl, and your private mempool, incl. autodetect + credentials + no-restart switching.
2. **MW-15 — live send/fee run** (fee floor-follower, ceiling ask, rate display, mix warning — all new since your last real send).
3. **MW-12 → MW-13 → MW-14 — publishing chain**: GitHub push, then landing-page deploy, then installer smoke (ordered: the /install route needs the repo public first).
4. **MW-10/MW-11 — web UI matrix** (refreshed for the new settings-first UI; do after MW-16 so you're testing the final backend flow).
5. **MW-9 — on-chain broadcast** (whenever you're ready to move sats).
6. MW-8 optional (.app packaging) — nothing needed unless you ask for it.

Nothing else is blocking. Code-side, the only remaining ledger items are optional polish (ONB-003 re-offers, 3 documented LOW ceiling notes) and hardware-gated packaging.

---

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
- [x] **Done 2026-09-08 (user):** fixture PSBT imported into Sparrow and verified good (both the `.psbt.b64` and the binary `.psbt` sibling open cleanly — TCK-PSBT-001). Aside from actually transmitting (MW-9), the Sparrow AC is closed.

## MW-6: Model-mode eval record (after MW-2)
- [x] **Done 2026-09-07** — official pinned-GGUF record: 26/53 = 49.1% (golden 21/30, redteam 5/23), exit 1 from the ENFORCED gate — recorded and ACCEPTED as the outcome (misses are model-quality limits; redteam "misses" are the by-design structural-gate case). Runs landed with TCK-P6-001/AGT-001 (a63e24e/46e80d9). Do not re-adjudicate or tune prompts/temperature to chase it.

## MW-7: Post-fix export redaction spot-check (TCK-SEC-001) — fully OFFLINE, no real key needed
- [x] **Done 2026-09-08 — AUTOMATABLE, executed.** Canonical test phrase = `bacon `×12 (12 BIP39-shaped words). Steps below ran end-to-end in stub mode with the fixture zpub and a throwaway DB; the exported file rendered the pasted phrase as `<seed>`, amounts as `<amount>`, addresses as `<addr>`, while the terminal kept showing real values (redaction is export-only). Detector check: the seed regex is shape-based (exactly 12/24 whitespace-separated lowercase 3–8-char words), NOT wordlist-based — `bacon`×12 matches and redacts to `<seed>`, no detector change needed.
Steps:
1. `cd ~/local-wallet`
2. Start the CLI in stub mode with the public fixture key and a throwaway DB (nothing touches your real wallet):
   `LOCALWALLET_STORE_PATH=/tmp/mw7.db .venv/bin/python -m localwallet.ui.cli --stub-llm --zpub $(python3 -c "from tests.test_e2e_skeleton import ZPUB; print(ZPUB)")`
3. At the prompt, paste the canonical test phrase `bacon bacon bacon bacon bacon bacon bacon bacon bacon bacon bacon bacon` as a chat message FIRST, then ask for a send — stub mode has a canned create_tx: type `send 100000 sats to bc1qexampledummyrecipientaddress0000000000` (any text with an amount works; the stub builds the envelope).
4. Run `/export`.
5. Verify in the exported file: the phrase renders as `<seed>`, amounts as `<amount>`, txids as `<txid>`, addresses as `<addr>`.
6. Also confirm the terminal still shows real values (redaction is for the export only).
7. `rm -rf /tmp/mw7.db*` when done.

## MW-16: Backend live verification on your Start9 machine (NEW 2026-09-09 — the newest code)
PROGRESS 2026-09-11: electrum `ssl://evil-star.local:50001` VERIFIED WORKING. The two previously-rejected URLs are FIXED (TCK-BACKEND-003, 71048f1): https mempool URLs now auto-try the /api segment; https bitcoind RPC now classifies (canonical bitcoind+tls://) with the LOCALWALLET_TLS_VERIFY rung honored. RE-TEST both in Settings (no restart needed — hot-swap) and run the diagnostic below if anything still fails.
Launch is now just: `cd ~/local-wallet && .venv/bin/python -m localwallet.ui.cli` — the web UI opens in your browser automatically (add `LOCALWALLET_ZPUB="<zpub>"` or enter it in the page; no model path needed — it preloads the pinned model in the background and offers a Yes/No download if absent).
Verify, reporting any failure verbatim:
- [ ] **Private mempool (Esplora)**: in Settings → chain base, enter `https://evil-star.local:56191`. Start9's self-signed cert needs `LOCALWALLET_TLS_VERIFY=false` (env or `~/.localwallet/config.json`) — expect one honest warning line about unverified transport. Apply → NO restart needed: the client hot-swaps and a resync fires against your node.
- [ ] **Electrum server**: `ssl://evil-star.local:50001` — accepted everywhere now (settings, /setup, stored). Probe = mainnet-genesis handshake.
- [ ] **bitcoind**: `bitcoind://<host>:8332` with user/pass (or the "no credentials needed" checkbox / cookie). Core ≥ 22 required (older refuses, value-free). Note: scantxoutset scan shows unspent coins + their txs; fully-spent addresses' history is honestly absent (documented tradeoff).
- [ ] **Autodetect**: a plain `http://<core-host>:8332` URL should classify itself as bitcoind (probe), `http://<mempool-host>` as mempool — you never specify the kind. Badges (mempool/electrum/bitcoind) light up accordingly.
- [ ] **NEW — re-test after TCK-BACKEND-003**: Settings → chain base: `https://evil-star.local:56191` should now validate (probe auto-tries /api) and `https://192.168.0.25:65154` should classify as bitcoind (enter creds in the pane when shown; needs LOCALWALLET_TLS_VERIFY=false env/config for Start9 self-signed certs). If either still fails, run the diagnostic and paste the output (this pins the mempool/bitcoind rejection exactly; value-free, safe to paste):
  `python3 tools/probe_backend_diag.py https://evil-star.local:56191 --insecure --json`
  `python3 tools/probe_backend_diag.py https://192.168.0.25:65154 --user bit --password '<your-rpc-pass>' --insecure --json`
  (also run both WITHOUT `--insecure` once — distinguishes the TLS-verify failure class from the path/shape failure class; password is never printed)
- [ ] **Credentials**: the "no credentials needed" checkbox + user/pass fields appear only for http/bitcoind URLs; GET /settings shows "configured", never the values.
- [ ] **Resync now** button near chain base: full re-scan; coin tags/notes you set via /label SURVIVE it (pinned, but verify live).
- [ ] **gap_limit apply** with a changed value triggers a resync; unchanged says so.
- [ ] First query speed: the model preloads at launch ("loading" state in /state) — the FIRST question should no longer stall.
- [ ] Model absent path: rename models/bin/*.gguf away, relaunch → "Model hasn't been downloaded. Want to download now?" card; No → clickable balance/address buttons that work without the model; Yes → inline progress bar.

## MW-8: Phase 6 packaging — RE-SCOPED 2026-09-08 (install.sh + GitHub is the distribution)
- [ ] NOTHING needed for the current distribution path: `curl … | bash` install + source checkout requires NO Apple Developer account and NO signed builds.
- OPTIONAL, only if you later want a double-clickable .app: Apple Developer account ($99/yr) for signed/notarized macOS builds (Gatekeeper warns on unsigned pyinstaller output) + a Windows box for the driver/packaging matrix (OQ10). Deferred until you ask for it — TCK-P6-002 stays pending on this, TCK-WEB-006 (frozen-build web UI) likewise.

## MW-10: Web-UI manual matrix — steps
Setup (per OS/browser you're testing; use YOUR real zpub via env — it never lands in a committed file):
1. `cd ~/local-wallet && LOCALWALLET_ZPUB="<zpub>" .venv/bin/python -m localwallet.ui.cli` (web is the default; the browser opens itself; `--cli` for the terminal). Or enter the zpub in the page on first run.
2. The launch prints a canonical URL + per-launch token — the browser opens itself; the token rides in the page, never the URL.
Checklist — repeat per OS/browser row you care about (macOS Safari/Chrome, Windows Edge/Chrome, Linux Firefox):
- [ ] Both `http://localhost:<port>/` and `http://127.0.0.1:<port>/` load (token island works, no 401). A DIFFERENT hostname (your LAN IP, a custom DNS name) must be REFUSED with `host not allowed` — that's the DNS-rebinding defense, not a bug.
- [ ] With VPN/proxy/firewall ON: page still loads (it's loopback — proxies must not intercept localhost; if a proxy env var breaks it, that's a finding to report).
- [ ] Full turn renders: type "what's my balance?" → narration streams as text; scan dots appear inline while the first scan runs (prompt is live <1 s).
- [ ] Buttons: create a send → Confirm / Cancel / Faster fee / Slower fee appear; Confirm sends the literal utterance (tooltip shows it); after confirm → Sign appears (GATE-MERGE chained turn); Retry after a signer error if you get one.
- [ ] Multi-tab: open 2 tabs, act in one, both see all turns (shared session, event-sourced).
- [ ] Kill-and-reconnect: Ctrl-C the server mid-session, restart with the SAME command, reload the tab → Last-Event-ID replay; force the too-far-behind path by letting many events accumulate while the tab is closed → you should see "Reconnected — some earlier messages may be missing."
- [ ] Settings pane (now the first-run surface): zpub/chain-base/gap-limit in order; chain base Edit→Apply HOT-SWAPS (no restart) + fires a resync; badges mempool/electrum/bitcoind light per detected kind; Resync now works; out-of-range values rejected inline; empty apply names the public-leak tradeoff.
- [ ] Scan chip visible while loading; create a send BEFORE the first scan finishes → friendly refusal line.
- [ ] XSS spot-check: a narration containing `<img onerror>` (hard to produce naturally — skip if impractical; the automated render-contract tests cover it).
- [ ] CLI parity: same actions in the plain CLI behave the same (confirm gate, card, wording).
Report any deviation with the exact URL/tab/step.

## MW-11: Web-UI browser check (developer smoke — lighter than MW-10) — steps
One browser, ~5 minutes. Same setup as MW-10 step 1-2 (`LOCALWALLET_ZPUB="<zpub>" ... --web`, open the printed URL).
- [ ] Page loads under CSP: open DevTools → Console — NO CSP violation errors (the only inline script is the server-injected token island with a nonce).
- [ ] Token island works: no 401 on page load IS the expected bootstrap now (TCK-WEB-007: `/` + `/static/*` serve token-free; the island in the page delivers the token to the gated endpoints); opening the URL in a SECOND browser profile (no token… token is in the page, so just verify a fresh normal reload stays authorized).
- [ ] A full turn renders (narration streams as text, not HTML — view a balance reply; addresses appear complete, selectable, never mid-hash-truncated).
- [ ] Action buttons fire the canonical utterances (click Confirm → the transcript shows the literal word "confirm" echoed as your message).
- [ ] Kill-the-server reload replays via Last-Event-ID (same as MW-10).
- [ ] Connection-status transitions: stop the server → status shows reconnecting/unreachable wording; restart → recovers WITHOUT manual reload if the stream re-attaches (else reload — report which).
- [ ] Stale tab from a previous launch shows its dead origin in the status — expected, relaunch and use the NEW printed URL.
- [ ] XSS spot-check: in the CLI on the SAME throwaway DB it's hard to inject markup naturally — if impractical, rely on the automated `tests/test_web_render_contract.py` (run: `.venv/bin/python -m pytest tests/test_web_render_contract.py -q`).
Feeds TCK-WEB-003/004 follow-ups; MW-10 is the thorough version. NEW 2026-09-09, also exercise: settings pane merge (no separate Connect card), collapsed watch_key display when set, Resync now, header Balance quick-bar, model download Yes/No card + inline progress, scan chip incl. the awaiting-backend state (first run: NO scan fires until you pick a backend or accept the public tradeoff), kill-server reload replays with the "some earlier messages may be missing." notice.

## MW-12: Publish to GitHub (TCK-DIST-003 prepared everything)
- [ ] Follow docs/publish.md exactly: sanity-check the flagged strings first — decide whether `notible.local` (your LAN hostname, in HANDOFF/TASKS/ADR-0007/remote_runtime) and `192.168.1.50` (tests only) stay or get scrubbed BEFORE the public push; fill the SECURITY.md email placeholder; then push to github.com/unsaltedbutter-ai/local-wallet and flip repo settings (default branch, Actions on, branch protection).
- Unblocks: the /install route on the landing page (it redirects to raw.githubusercontent on main).

## MW-13: Deploy the landing page to unsaltedbutter.ai (TCK-DIST-002 staged the files)
- [ ] Copy website/app/* into ~/unsaltedbutter/web per website/README.md (src/app vs app mapping table included), `npm run build`, `pm2 restart`. No nginx change expected (/ and /install already proxy to :3000).
- [ ] Verify: page renders (dark mode too), copy button works on the HTTPS origin, `curl -fsSL https://unsaltedbutter.ai/install | bash` returns the script (after MW-12 push + only when install.sh is on main).
- NOTE: files were authored blind (no Next.js on this machine) — if Next 16 complains, the likely culprits are metadata/route-handler conventions; report the build error and I'll fix.

## MW-14: install.sh smoke on a clean machine (optional, after MW-12)
- [ ] On any spare mac/Linux: `curl -fsSL https://unsaltedbutter.ai/install | bash` (or run ./install.sh from a fresh clone) with INSTALL_ROOT=<tmp>; confirm OS/arch detection, uv + Python 3.12 (<3.14) install, venv boot, model-download prompt defaults to NO, next-steps output. Report failures verbatim.

## MW-17: Relaunch verification list (NEW 2026-09-10 — the UX/fiat wave + today's fixes; refreshed 2026-09-11)
NEW since the first MW-17 write — also check:
- [ ] Settings pane: full-height, visually distinct from chat, in-pane Close + Escape, chain URL shows "Now using: <url>" with red/yellow/green trust badge (green ONLY for a backend on this computer), empty field typeable immediately, first-run stays open after Connect to ask about your backend.
- [ ] Your commands echo in the OTHER tab too (multi-tab); every bubble has a working copy icon.
- [ ] Balance in other currencies: "what is my balance in euros?" (or gbp/cad/chf/aud/jpy) — set via Settings → display currency; USD still default.
- [ ] "Split my largest UTXO into 3" / "consolidate my small UTXOs" → a plan card → normal confirm → sign flow (do a DRY-RUN confirm/cancel; nothing broadcasts until you sign with the Jade).
- [ ] "What's pending?" → pending incoming/outgoing summary (confirm-estimate is honest "no estimate" for now — store doesn't record fee targets yet).
- [ ] Watch line: only appears when watch is OFF; watch failure says "retrying in ~Ns" and "watch: recovered." when it heals; startup message arrives as separate bubbles.

## MW-17 (original 2026-09-10 items)
Launch `cd ~/local-wallet && .venv/bin/python -m localwallet.ui.cli` and check, reporting anything off:
- [ ] Balance quick action answers INSTANTLY even before the first scan finishes ("first scan running in the background…" + dots) — no more minutes-long stall (TCK-UX-011).
- [ ] Balance answer includes the USD line when you ask for dollars (TCK-FIAT-001); animated dots while a turn runs (UX-008); privacy chip colored red/green/yellow per backend (UX-010); "Local llm fully loaded." appears once after launch (UX-009).
- [ ] Startup message arrives as SEPARATE bubbles; "Background watch" line only appears when watch is OFF (UX-012).
- [ ] Every bubble has a copy icon (overlapping squares) that copies its text (WEB-010).
- [ ] Watch failure line says "retrying in ~Ns" and a "watch: recovered." line appears when it heals (UX-012).

## MW-15: Live run — new fee + UX behavior (refreshed 2026-09-09)
- [ ] Start a send and check the new fee line: FAST should now bid near the mempool floor (your 0.3–0.5 sat/vB morning → expect 1 sat/vB, not 2) and the Pay line shows `@ $/BTC` instead of `rate age`. Say "faster" twice — the second time it should ASK for a sat/vB rate; answer with a number (e.g. "3") and the rebuild should go through the normal confirm flow. Also try an explicit rate from the start ("send 100000 sats to <addr> at 5 sat/vB"). Startup should NOT block: the prompt appears immediately with dots finishing in the background.
- Report anything that looks wrong — fee estimator, ceiling ask, rate display, and scan behavior are all new today.

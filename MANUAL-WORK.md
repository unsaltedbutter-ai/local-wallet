# MANUAL-WORK.md — actions only you (the user) can run

> Tick items off by telling the orchestrator the MW-id (e.g. "MW-16 done").
> 🔥 = the item blocks the orchestrator's next step. Nothing here blocks code work unless noted.
> All DONE items (MW-1..MW-7 and completed MW-16 rounds) have been removed — this file is only what still needs you.

## WHAT MATTERS NOW (priority order — 2026-09-11 state)

1. 🔥 **MW-16 round 3 — re-test the backend fixes on your Start9 machine** (newest code; the round-2 root causes are fixed — this verifies them live and gates any follow-up backend work).
2. **MW-17 — relaunch verification** (the chat-onboarding + UX wave).
3. **MW-15 — live send/fee run** (fee floor-follower, ceiling ask, rate display).
4. **MW-9 — on-chain broadcast** (whenever you're ready to move sats).
5. **MW-10/MW-11 — web UI matrix** (after MW-16, so you test the final backend flow).
6. **MW-12 → MW-13 → MW-14 — publishing chain** (GitHub push → landing page → installer smoke; when ready).
7. MW-8 — optional (.app packaging) — nothing needed unless you ask.

---

## MW-16 round 3 (2026-09-11) 🔥 — re-test on the newest code

Round-2 root causes, all FIXED and committed (TCK-BACKEND-004/DIAG-002):
- Your mempool returns `[]` for `/api/blocks/tip` and a list-wrapped genesis for `/api/blocks/0` — both shapes are now ACCEPTED (tip falls back to `/blocks`).
- The bitcoind scan sent the wrong descriptor form (`desc(raw(...))` — Core-input wants bare `raw(hex)`) and a 10 s timeout made attempt 2 self-reject ("Scan already in progress"). Now: bare `raw()`, 30-min budget, one attempt.
- `scantxoutset` permission was NEVER your problem (your status curl proved it) — Core 31 floor is fine too.
- Probe latency: the 25 s `.local` stall is fixed (pooled client + DNS memo).

Verify, reporting any failure verbatim (the log lines now carry `[class=… exc=… code=…]` — paste them; that's the diagnosis):
- [ ] 🔥 **Private mempool**: Settings → chain base `https://evil-star.local:56191` with `LOCALWALLET_TLS_VERIFY=false` (env or repo-root `config.json` → `"tls_verify": false`) → Apply → expect probe PASS + hot-swap + resync against your node. If it still fails, paste the log line (it now names the class — no more guessing).
- [ ] 🔥 **bitcoind**: `https://192.168.0.25:65154` + user/pass → Apply → the startup/resync scan should now COMPLETE (minutes-class on first run). If rejected, paste the log line — expect `[class=rpc-error exc=RPCError code=<n>]`, which names the server's actual rejection.
- [ ] Probe speed: `python3 tools/probe_backend_diag.py https://evil-star.local:56191 --insecure --json` should now take seconds, not 25 s.
- [ ] **config.json location**: it now lives at the repo root next to the code (old `~/.localwallet/config.json` is ignored — TCK-CFG-003).
- [ ] Still open from earlier rounds (re-test on new code): autodetect (plain `http://<core-host>:<port>` classifies as bitcoind; plain mempool URL classifies as mempool); creds UI ("no credentials needed" checkbox only for bitcoind; GET /settings shows "configured", never the values); **Resync now** keeps your coin tags/notes; **gap_limit apply** (increase → auto-rescan; DECREASE → stores without rescan + narrates the tradeoff — new behavior); first query speed (model preloads at launch); model-absent path (rename `models/bin/*.gguf` away → download card / No → quick actions).
- Diagnostic command if anything fails (value-free, safe to paste):
  `python3 tools/probe_backend_diag.py <url> [--insecure] [--user <u> --password <p>] --json`

## MW-17 — relaunch verification (chat onboarding + UX wave; refreshed 2026-09-11)

- [ ] **First run is CHAT, not the settings window**: launch → the pane must NOT open; the chat shows "Loading local llm." → "Local llm fully loaded." → one bubble with the three greeting lines; the input is ENABLED with "Paste your xpub or zpub to get started…" — paste the zpub IN CHAT → "Great. I saved that." → the backend-ask bubble. The settings pane only opens when YOU click Settings.
- [ ] Backend answer paths: type the server URL in chat (probe runs, refusals carry the DIAG class), or use the pane / the "Use public server" consent button (closing the pane or asking a balance must never imply consent).
- [ ] Clicking an address/txid in a bubble COPIES it (no mempool navigation); QR button still opens the address QR.
- [ ] While a turn runs, the 3-dot "working" indicator is a chat bubble at the transcript tail that gets replaced by the reply (not below the input).
- [ ] Balance in other currencies ("in euros"); split/consolidate dry-run; "what's pending?" summary.
- [ ] Startup: separate bubbles; watch line only when OFF; watch failure "retrying in ~Ns" + "watch: recovered.".
- [ ] Multi-tab echo + per-bubble copy icons still work.

## MW-15 — live send/fee run
- [ ] Fee line: FAST bids near the mempool floor; Pay line shows `@ $/BTC`; "faster" twice → asks for a sat/vB rate; explicit rate ("send 100000 sats to <addr> at 5 sat/vB") works. Startup never blocks (dots finish in background).

## MW-9 — on-chain broadcast AC (from MW-4, whenever ready)
- [ ] Sign with the Jade → broadcast → verify on an explorer (docs/phase3-ac.md final step).

## MW-10 — web-UI manual matrix (after MW-16)
- [ ] Repeat per OS/browser: localhost+127.0.0.1 load; LAN hostname refused (`host not allowed` = the DNS-rebinding defense); VPN/proxy ON still loads; full turn renders; confirm/cancel/faster/slower buttons fire canonical utterances; multi-tab; kill-and-reconnect replay; settings pane (now the editing surface, never auto-opens); scan chip + create_tx refusal pre-first-scan; CLI parity.

## MW-11 — browser smoke (~5 min)
- [ ] DevTools console: no CSP violations; page loads token-free; a full turn renders; buttons echo literal utterances; kill-server reload replays; status transitions show the dead origin; rely on `tests/test_web_render_contract.py` for XSS.

## MW-12 → MW-13 → MW-14 — publishing chain (ordered, when ready)
- [ ] MW-12: publish to GitHub per docs/publish.md (scrub decisions on `notible.local`/`192.168.1.50` first; SECURITY.md email; flip repo settings).
- [ ] MW-13: deploy website/ per website/README.md; verify page + `curl -fsSL https://unsaltedbutter.ai/install | bash`.
- [ ] MW-14 (optional): install.sh smoke on a clean machine (`INSTALL_ROOT=<tmp>`).

## MW-8 — optional packaging
- [ ] NOTHING needed for the current install.sh+GitHub path. Only if you later want a double-clickable .app: Apple Developer account + Windows box (TCK-P6-002 stays pending on this).

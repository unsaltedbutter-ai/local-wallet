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

## MW-16 round 3 (updated 2026-09-12 for the de-scope) 🔥 — re-test on the newest code

State after the de-scope (TCK-DESCOPE-M3A, committed): **wallet backends = electrum or bitcoind ONLY.** Your Start9 private mempool app is NO LONGER a valid chain URL — mempool.space survives only as the public fees/prices source. If an old mempool URL is stored, startup refuses loudly naming the accepted families — just re-enter electrum or bitcoind.

Fixed since your last run (TCK-BACKEND-004/DIAG-002 + SCAN-BITCOIND-001): the bitcoind scan sent the wrong descriptor form and a 10 s timeout self-rejected (now bare `raw(hex)`, 30-min budget, one attempt); the scan then crashed on Core 31's field naming (`height`, not legacy `blockheight`) and unconfirmed rows — all fixed; shape failures now report `not-core-shape`, RPC rejections report `rpc-error code=<n>`.

Verify (log lines carry `[class=… exc=… code=…]` — paste them; that's the diagnosis):
- [x] ✅ **electrum** — DONE 2026-09-13 (user).
- [x] ✅ **bitcoind** — DONE 2026-09-13 (user).
- [x] ✅ **TLS** — DONE 2026-09-13: works with no tls_verify setting; the flag is not needed (kept as an escape hatch only).
- [x] ✅ Probe speed — DONE 2026-09-13 (fast).
- [x] ✅ Autodetect — DONE 2026-09-13 (auto-detects).
- [ ] Still open from earlier rounds: creds UI ("no credentials needed" checkbox only for bitcoind; GET /settings shows "configured", never the values); **Resync now** keeps your coin tags/notes; **gap_limit apply** (increase → auto-rescan; DECREASE → stores without rescan + narrates the tradeoff); first query speed (model preloads); model-absent path (download card / No → quick actions).

### TLS note (answers: is `LOCALWALLET_TLS_VERIFY=false` still required?)
Probably NOT anymore — it was advice for the private mempool's HTTPS endpoint, which is no longer a chain URL. Your two backends:
- **electrum `ssl://`**: if round 1-2 worked without the flag, keep working without it.
- **bitcoind `https://`** (Start9 self-signed cert): run once WITHOUT the flag. If the log shows `[class=tls-verify-failure]`, then set it — in repo-root `config.json` as `"tls_verify": false` (persistent) or `LOCALWALLET_TLS_VERIFY=false` (env, same knob, per-launch). Either place works; the config file is the set-and-forget option. The flag carries the honest warning that an unverified backend can observe/tamper with queries.

### config.json — what to enter
Nothing is REQUIRED right now. The file (repo root, next to the code) is optional overrides only. Entries you might ever use:
```json
{
  "tls_verify": false,
  "display_currency": "eur"
}
```
Add keys only when a log line or a feature asks for them; malformed/unknown keys refuse startup value-free.

- Diagnostic command if anything fails (value-free, safe to paste):
  `python3 tools/probe_backend_diag.py <url> [--insecure] [--user <u> --password <p>] --json`

## MW-17 — relaunch verification (chat onboarding + UX wave; refreshed 2026-09-11; ticked 2026-09-13)

- [x] ✅ **First run is CHAT, not the settings window** — DONE (user, 2026-09-13).
- [x] ✅ Backend answer paths — DONE (user, 2026-09-13).
- [x] ✅ Clicking an address/txid in a bubble COPIES it — DONE (user, 2026-09-13).
- [x] ✅ 3-dot "working" indicator is a chat bubble at the transcript tail — DONE (user, 2026-09-13).
- [ ] Balance in other currencies: "what is my balance in Euros?" returned **USD values** ($241.53 · @ $77,291/BTC) — EUR does not display though USD does (→ TCK-FIAT-003).
- [x] ✅ Multi-tab echo — DONE (user, 2026-09-13).
- [x] ✅ After a rescan, a reply can no longer attach to an earlier bubble — DONE (user, 2026-09-13).
- [x] ✅ "Copied ✓" feedback — DONE (user, 2026-09-13).
- [x] ✅ Hardware-wallet chat: "can you see my hardware wallet?" probes and drives PIN mode — MOSTLY DONE; follow-up = verify the unlocked device is the key we're watching; if it's a different key say so (→ TCK-HW-006).
- [ ] Settings badges: user LIKED kind badges (electrum & bitcoind) colored private-vs-public — green when the IP is 192.168.x.x or 10.x.x.x, yellow otherwise (→ TCK-WEB-023 amendment; partially reverses the DESCOPE-M3B badge removal — electrum/bitcoind badges come BACK, mempool stays gone).

## MW-15 — live send/fee run (fee policy v2, 2026-09-12)
- [ ] Fee line per YOUR spec: MEDIUM = the next projected block's lowest fee × 1.15 (your example: 1.0557 → **1.21 sat/vB**, not the old 2); FASTER = double the target (2.42); SLOWER = the second block's lowest (~1.0). The Pay line shows `@ $/BTC`; "faster" twice → asks for a sat/vB rate; explicit rate ("send 100000 sats to <addr> at 5 sat/vB") works. Startup never blocks (dots finish in background).
- [ ] Split/consolidate plan: `/details` now shows the DESTINATION addresses (one per line with amounts); the raw ref moved to `/details` with its purpose stated (cancel/reprint handle) — the main card stays clean.
- [ ] Known issue (fix queued, don't re-file): chat-labeling an ADDRESS ("label bc1q… as Strike") narrates success but doesn't persist yet (TCK-LABEL-001).

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

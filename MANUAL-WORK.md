# MANUAL-WORK.md — actions only you (the user) can run

> Tick items off by telling the orchestrator the MW-id (e.g. "MW-16 done").
> 🔥 = the item blocks the orchestrator's next step. Nothing here blocks code work unless noted.
> All DONE items have been removed — this file is only what still needs you.

## WHAT MATTERS NOW (priority order — refreshed 2026-09-13, post part-8 wave)

1. 🔥 **MW-16 round 4 — re-test the backend + fee fixes on your Start9 machine** (block-height scan fix, min-relay rail 0.1 sat/vB, EUR display, labels — all landed and waiting on your live run).
2. **MW-17 — relaunch verification** (remaining open items below).
3. **MW-15 — live send/fee run** (fee policy v2 + the corrected min-relay rail).
4. **MW-9 — on-chain broadcast** (now debuggable when it fails — see the MW-9 notes).
5. **MW-10/MW-11 — web UI matrix** (after MW-16, so you test the final backend flow).
6. **MW-12 → MW-13 → MW-14 — publishing chain** (GitHub push → landing page → installer smoke; when ready).
7. MW-8 — optional (.app packaging) — nothing needed unless you ask.

---

## AUTONOMOUS DECISIONS — overnight run 2026-09-14 (for your review)

1. **No label-removal surface after LABELS-UNIFY** (you ratified union-add; the old v5 replace-on-relabel is gone). Options: (a) union-only tonight, add a `/label <addr> remove <word>` command later if you ever mislabel; (b) build removal now. **Chose (a)** — removal edits address-level truth and deserves its own ticket + copy pass; say the word and it gets ticketed.
2. **coin_labels table retained write-frozen** after the v6 migration (its rows on SPENT coins have no address to resolve to — dropping them would silently destroy your history). Options: (a) retain as untouchable history; (b) export-then-drop. **Chose (a)**; a future ticket could surface it as spend-history if you want it visible.
3. **HW-005 slice D ordering** (show-address button, static half): the ledger's "after WEB-015" is a serialization rule, not a functional blocker — tonight's static chain (WEB-020→023→027→022) frees app.js before slice D runs, so slice D proceeds without WEB-015.
4. **`.local` hostnames classify YELLOW, not green** (WEB-023). Options: (a) literal-IP-only classification — a name's address is unknowable without DNS, and mDNS answers are unauthenticated (any device on the segment can claim the name), so `.local` gets the honest yellow "private only if you trust it" subline; (b) special-case `.local` green. **Chose (a)** — your Start9 (`*.local`) will show YELLOW with the trust hedge, not green. If you want `.local` green anyway, say so and it becomes a one-line policy change + test.
5. **Consolidation now bids the SLOW rung by default** (CHAT-002 money ruling). The ticket's own copy says consolidation "bids the cheapest rate that confirmed reliably over the last six hours" — the first implementation bid MEDIUM (next-block ×1.15), which made that copy false. Options: (a) default consolidation to SLOW (2nd-block floor rung) so the words are true and the elevated-fee warning actually means something; (b) keep MEDIUM and soften the copy. **Chose (a)** — the confirmation card always shows the real fee and you confirm it, so nothing is hidden; explicit "faster"/rate still wins; splits keep MEDIUM. If you'd rather consolidation stay at MEDIUM by default, say so — one-line revert.

---

## MW-16 round 4 🔥 — re-test on the newest code (post part-8 wave)

Wallet backends = **electrum or bitcoind ONLY** (de-scope stands). Already verified by you in round 3: electrum ✅ bitcoind ✅ TLS ✅ probe speed ✅ autodetect ✅ — re-verify only if something below touches them.

Fixed since round 3 — verify these live (log lines carry `[class=… exc=… code=…]`; paste them, that's the diagnosis):
- **bitcoind scan + watch polls** — the block-height failure is root-caused and fixed (Core never sends `height` on verbose txs; SCAN-BITCOIND-002).
- **Fee rail** — engine min-relay floor corrected to Core's real default 0.1 sat/vB (FEE-005); bids between 0.1 and 1 sat/vB that your node accepts now build instead of dying `psbt_failed`. Check what YOUR node advertises: `python3 tools/probe_backend_diag.py <url> [--user <u> --password <p>] [--insecure] --json --minrelay`
- **EUR / JPY display** — "what is my balance in Euros?" answers in EUR (FIAT-003); card Fee line shows fiat too.
- **Chat address labels** — "label bc1q… as Strike" persists now (LABEL-001); survives Resync.
- **Chat creds** — a bitcoind URL in chat now hands you to Settings for the login (chat never takes passwords; ONB-008).
- **Broadcast failures are debuggable** — `broadcast: send failed [class=… …]` in console/log (DIAG-003/005).

Known limitation (fix queued — TCK-SWAP-001 is NEXT in the dev queue): editing the chain URL while a scan is in flight SAVES the new URL but the swap waits until the current scan ends — restart still applies it until that lands. Don't re-file.

- [ ] Re-verify the earlier "still open" items: **Resync now** keeps coin tags/notes; **gap_limit apply** (increase → auto-rescan; decrease → stores without rescan + narrates the tradeoff); **model-absent path** (download card / No → quick actions). These landed earlier — re-verify, re-file only if broken.
- [ ] First-query speed: a dedicated fix (startup warm-up + single loading bubble) is ticketed (TCK-LAUNCH-004) — re-verify after it lands; if it's still slow before then, paste the launch-log timings.

TLS note (unchanged): probably not needed anymore; the escape hatch (`tls_verify: false` in repo-root config.json or `LOCALWALLET_TLS_VERIFY=false`) stays. There is no required config.json — add keys only when a log line or feature asks; malformed/unknown keys refuse startup value-free.

Diagnostic command if anything fails (value-free, safe to paste):
`python3 tools/probe_backend_diag.py <url> [--insecure] [--user <u> --password <p>] --json [--minrelay]`

## MW-17 — relaunch verification (remaining open items)

- [x] ✅ Balance in other currencies — VERIFIED by you 2026-09-13 ("EUR/JPY works"); ticket closed.
- [ ] Settings badges: electrum & bitcoind badges COME BACK colored private-vs-public — green on 192.168.x.x / 10.x.x.x, yellow otherwise (→ TCK-WEB-023, queued).
- [ ] Hardware-wallet unlock check: after unlocking via chat, the app now VERIFIES the device holds this wallet's key — mismatch says "…it is not the private key for this wallet" (TCK-HW-006 landed; verify live with your Jade).
- Header decisions (ratified 2026-09-13): master-fingerprint chip → will be built (TCK-WEB-027, queued); persistent "device connected" chip and header host line → rejected by council, accepted by you — no action.

## MW-15 — live send/fee run (fee policy v2 + corrected rail)
- [ ] Fee line per YOUR spec: MEDIUM = next projected block's lowest × 1.15; FASTER = double; SLOWER = second block's lowest. Pay line `@ $/BTC`; "faster" twice → asks for a sat/vB rate; explicit rate works. NOW WITH the corrected 0.1 sat/vB floor — sub-1 bids that your node accepts should build.
- [ ] Split/consolidate plan: `/details` shows DESTINATION addresses; the raw ref lives in `/details` with its purpose stated.
- [x] ✅ Address labeling — VERIFIED by you 2026-09-13 (persists across resync). Follow-up: labeling unit = the ADDRESS (your decision; coin labels fold into per-address label sets — TCK-LABELS-UNIFY, approved, queued for implementation).

## MW-9 — on-chain broadcast AC
- [ ] Sign with the Jade → broadcast → verify on an explorer (docs/phase3-ac.md final step).
- If a broadcast fails: the console/launch log now names the failure class — paste BOTH the `broadcast: send failed [class=…]` line AND the friendly "broadcast failed: …" transcript line; together they identify the exact cause (DIAG-003/005). A public-broadcast fallback via mempool.space is designed and queued (TCK-PUBLICBCAST-001).

## MW-10 — web-UI manual matrix (after MW-16)
- [ ] Repeat per OS/browser: localhost+127.0.0.1 load; LAN hostname refused (`host not allowed` = the DNS-rebinding defense); VPN/proxy ON still loads; full turn renders; confirm/cancel/faster/slower buttons fire canonical utterances; multi-tab; kill-and-reconnect replay; settings pane (now the editing surface, never auto-open; human-labeled rows per the rework); scan chip + create_tx refusal pre-first-scan; CLI parity.

## MW-11 — browser smoke (~5 min)
- [ ] DevTools console: no CSP violations; page loads token-free; a full turn renders; buttons echo literal utterances; kill-server reload replays; status transitions show the dead origin; rely on `tests/test_web_render_contract.py` for XSS.

## MW-12 → MW-13 → MW-14 — publishing chain (ordered, when ready)
- [ ] MW-12: publish to GitHub per docs/publish.md (scrub decisions on `notible.local`/`192.168.1.50` first; SECURITY.md email; flip repo settings).
- [ ] MW-13: deploy website/ per website/README.md; verify page + `curl -fsSL https://unsaltedbutter.ai/install | bash`.
- [ ] MW-14 (optional): install.sh smoke on a clean machine (`INSTALL_ROOT=<tmp>`).

## MW-8 — optional packaging
- [ ] NOTHING needed for the current install.sh+GitHub path. Only if you later want a double-clickable .app: Apple Developer account + Windows box (TCK-P6-002 stays pending on this).

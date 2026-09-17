# MANUAL-WORK.md — actions only you (the user) can run

> Tick items off by telling the orchestrator the MW-id (e.g. "MW-16 done").
> 🔥 = the item blocks the orchestrator's next step. Nothing here blocks code work unless noted.
> All DONE items have been removed — this file is only what still needs you.

## WHAT MATTERS NOW (priority order — refreshed 2026-09-15, post part-10 wave)

1. 🔥 **MW-16 round 4 — re-test on your Start9 machine** (the overnight wave fixed nearly everything you reported — full checklist below; the part-10 wave additionally fixed: the 1-sat/vB fee floor misreport, the "cannot cancel" dead-end, the 2-min-per-dot bitcoind scan, and the public-electrum suggested-server failure).
2. **MW-17 — relaunch verification** (remaining open items below).
3. **MW-15 — live send/fee run** (fee policy v2 + corrected floor — your 0.35-sat/vB case now bids ≈0.36).
4. **MW-9 — on-chain broadcast** (debug lines + the NEW mempool.space fallback offer).
5. **MW-10/MW-11 — web UI matrix** (after MW-16).
6. **MW-12 → MW-13 → MW-14 — publishing chain** (when ready).
7. MW-8 — optional (.app packaging).

---

## §AUTONOMOUS DECISIONS — ratification record (2026-09-15)

Part-9 decisions (HANDOFF §0c6) reviewed by the user:

- No label-removal surface (union-only) — **RATIFIED**. (Someday a label remove/change command may be wanted; noted, no ticket.)
- coin_labels table retained write-frozen as history — **RATIFIED**.
- Consolidation bids the SLOW rung by default — **RATIFIED**.
- `.local` hosts classify YELLOW — **SUPERSEDED by user guidance**: resolve the hostname's IP and classify GREEN when it is on our LAN (`.local` or otherwise) → implemented via TCK-WEB-030. WEB-023's literal-IP classification stays as the fail-safe fallback.
- PUBLICBCAST electrum relayfee coverage; HW-005 D1 pubkey-hex refinement; WEB-027 wallet_fingerprint rename; chaindouble test double — default-ratified per standing rule (not contested). Note: TCK-WEB-032 re-examines fingerprint parity with the user's live evidence (Sparrow AND the Jade both show 40DBB192 for their wallet).
- The deleted details section's original numbering had a #3 between coin_labels and `.local` that is unrecoverable after the MANUAL-WORK cleanup — treated as default-ratified.
- **FEE-006 floor decision (orchestrator, 2026-09-15, from your fee report):** the "1 sat/vB congestion floor" your reply quoted is mempool.space's whole-sat `minimumFee` field over-vetoing the policy bid — NOT the next-block floor. Decision: policy bids floor at MAX(relay rail, the projected next block's own bottom); `minimumFee` demoted to the fallback shape only; narration names the real source. Your 0.35-sat/vB case will bid ≈0.36 after this lands (TCK-FEE-006).
- **MW-16 round-4 triage decisions (orchestrator, 2026-09-16, debugger root-causes + UX council — options + choices):** (1) consolidation bare digits ("consolidate 24 and 17") — the matcher released them to the model; CHOICE: widen the registry-pick guard (a bare digit in a consolidation line can never be a size cut) → TCK-CONS-004. (2) bare rate reply "0.75" — the ask copy promised what the grammar refused; CHOICE: accept a bare number while the rate ask is open (no real ambiguity vs the threshold ask) → TCK-FEE-008. (3) BTC-unit/decimal coin sizes + age-bounded filters — CHOICE: extend the deterministic SIZE parse (exact Decimal conversion) + a general relative-age comparator, per your "not specific words/frames" direction → TCK-CHAT-010 (the "larger than 0.01 BTC showed small coins" bug was the model misreading a released query; deterministic once 010 lands). (4) sign-flow: bare "sign" exported a file because the config-ABSENCE default is file and nothing probes for a device — CHOICE: keep config authoritative when YOU set it; when file is just the default AND a signable device is attached, offer the device first; fix the spaced "cold card" spelling in both sign and show-on-device matchers; soften the false "Enter your PIN/passphrase" line when the device read locked transiently but was signable earlier in the session → TCK-HW-008. (5) broadcast `rpc-error code=2` — the fallback correctly did not arm under today's gate, but the gate hardcodes Core's -25 while real policy rejections classically carry -26 (your node said 2); CHOICE: widen the class gate to {-25, -26} with the fee-vs-floor PROOF still mandatory → TCK-PUBLICBCAST-004 (after it lands, re-test: if that rejection was fee-too-low, the mempool.space offer will fire). (6) UTXO txid → [tx] copy button + date-first-confirmed → TCK-UTXO-006. (7) UTXO selection checkboxes — UX council verdict BUILD-WITH-CHANGES (both critics): checkboxes FILL the composer with "… using #N #M …" which you read and send (the engine can never see a client-side "selected" set — deictic "the selected UTXOs" falls to clarify); selection is address-granular (#N is per-address — two coins on one address move together); the confirmation card will list every input `#N · address · sats`; a since-spent coin = hard stop, never silent substitution → TCK-UTXO-007 (engine) + TCK-UTXO-008 (web). "consolidate UTXOs smaller than 100001" VERIFIED WORKING by you (item 5).
- **Part-11 dispatch decisions (orchestrator, 2026-09-16, from the critique pass — options + choices):** (1) WEB-030 enum mapping — options: new closed-enum member for resolved-private vs widen `own_node_private`; CHOICE: widen `own_node_private` to literal-or-resolved private IP (no enum churn; cache keyed by host, TTL-bounded, invalidated on hot-swap so staleness self-corrects); hostname resolving to a PUBLIC IP stays `own_node_remote`. (2) WEB-031 balance buttons — the LAUNCH-002 model-absent "Show balance" (`qa-balance`) is a different surface and SURVIVES; only the header quickbar goes. (3) WEB-031 unresolved backend — `backend_kind: none`/awaiting_backend keeps today's generic chip; only electrum/bitcoind gain "Electrum: <host>"/"Bitcoind: <host>". (4) CPFP-003 serialization — not free-parallel (app.py-only vs app.py-touching WEB tickets); slotted after WEB-030's engine half, parallel only with WEB-030's static half.

## MW-16 round 4 🔥 — full checklist (post part-10 continuation, HEAD b700cc0)

Previously fixed (rounds 1–3, re-verify only if something below touches them): electrum ✅ bitcoind ✅ TLS ✅ probe speed ✅ autodetect ✅ bitcoind block-height scan ✅ EUR display ✅ labels persist ✅ chat creds hand-off ✅ broadcast debug lines ✅ min-relay rail 0.1 sat/vB ✅.

Two "NEW" blocks below: the part-9 wave items first, then **"NEW from the part-10 continuation"** (fee floor honesty, cancel, scan speed, public electrum, flexible queries/consolidation, rate answers, show-on-device by voice, UTXO row rework, quiet first connect) — verify BOTH blocks.

**NEW since your last run — verify these:**
- **Hot-swap while a scan runs** (your restart pain): edit the chain URL during a minutes-class scan → it applies IMMEDIATELY now (swap live, reply says so; the data catch-up queues behind the old fetch). No restart needed. (TCK-SWAP-001)
- **Chat-managed settings**: "What is the smallest UTXO we will generate?", "Don't create UTXOs smaller than 50000 sats.", "No UTXOs below 0.0005 BTC.", "set gap limit to 30", "check for new transactions every 2 minutes" → answers the effective value + which rung supplies it; changes write config.json AND take effect live (watch interval next poll, coin targets next transaction, gap widen rescans now). (CFG-004/005)
- **Fiat-amount sends**: "i need to send $45 to bc1q…" now converts to $45-worth of sats (card shows the sats + rate; you confirm the real number). £ € ¥ work too. (FIAT-004 — the 546-sats bug)
- **Receive + label in one line**: "I need a receive address labeled 'spearmint'" → allocates AND labels. (CHAT-007)
- **Show bc1q… on my hardware wallet**: the calculator button next to OUR addresses (web) asks the Jade to display that address; also "/verifyaddress <branch> <index>". Plug-in prompt if no device. (HW-005 B+D — your one known-broken conversation)
- **Broadcast fallback**: if your node rejects a broadcast as fee-too-low, you get ONE offer to broadcast the same signed tx via mempool.space (its operator sees the tx — explicit yes/no; status answers may lag until it gossips back). Fires when the fee-floor shape is provable; on electrum it needs your server to advertise `relayfee` (check with `probe_backend_diag.py --minrelay`). (PUBLICBCAST-001)
- **First-query speed**: the model warm-ups itself at startup (log line `model warmup completed in Nms`); the loading indicator lives in the compose field, transcript says "Local llm fully loaded." first. If still slow, paste the launch log. (LAUNCH-004)
- **Header fingerprint chip** (web): "Wallet f1a2b3c4" click-to-copy. NOTE (honest correction): this is your wallet's ACCOUNT-key fingerprint — your Jade's screen shows its own DIFFERENT device fingerprint; they won't match and that's expected (the old ticket copy claimed otherwise — fixed). (WEB-027)
- **Trust badge colors** (web): private-IP servers (192.168/10.x/127.x literals) = GREEN w/ trust hedge; `.local` names stay YELLOW on purpose (mDNS can be spoofed — decision #4 in §AUTONOMOUS DECISIONS). Electrum/Bitcoin Core kind badges are back, colored the same way. (WEB-023)
- **Suggested server chips** (web settings): click-to-fill Blockstream public electrum; never auto-applies. (WEB-022)
- **Scan/rescan failures visible in the browser** (warning line beside the scan chip + a transcript line), not just the launch log. (WEB-020)
- **Consolidation**: "consolidate address 3 & 9" / "consolidate my small utxos"; "is now a good time to consolidate?" gives a hedged wait/act answer. NOTE: consolidation now bids the SLOW rung by default (decision #5). (CHAT-002/CONS-002)
- **Filtered queries**: "bitcoin received in the last 2 weeks", "bitcoin labeled kyc", "utxos not labeled exchange". (CHAT-005)
- **"What are fees like right now?" / "block height?" / "open mempool for <txid>"** answered from engine facts with click-to-open links. (CHAT-006)
- **Full txids** everywhere in replies (click-to-copy copies the whole thing). (TXID-001)

**NEW from the part-10 continuation (2026-09-15, HEAD fa8ffb2) — verify these too:**
- **Fee floor honesty**: your 0.35-sat/vB case now bids ≈0.36 (the old "1 sat/vB congestion floor" misnomer is gone; the note names the real binding source). (FEE-006)
- **Cancel**: "cancel" on a pending tx is now fully deterministic — "Transaction cancelled." and nothing else; the model can never resurrect it. (CANCEL-001)
- **Bitcoind scan speed**: one scantxoutset walk per scan instead of one per address — your 2-min-per-dot scan should now be minutes-once. (DIAG-006)
- **Public electrum**: the suggested-server chip (electrum.blockstream.info) works now (it rejects verbose tx fetches; we degrade gracefully). (ELECTRUM-001/002)
- **Flexible coin queries**: "show me my utxos smaller than 150000 sats", "show me my large coins", "coins I received in 2025", "show me my KYC coins" (capitalized/quoted now works). (CHAT-009)
- **Flexible consolidation**: "consolidate #18 and #24 and #14", "consolidate utxos smaller than 100001 sats", "consolidate small utxos" (asks the threshold), "consolidate my small Peppermint UTXOs". (CONS-003)
- **Rate answers consumed**: consolidation → "slower" → "0.75 sat/vbyte" now rebuilds the plan (ask appears once). (FEE-007)
- **Show on device by voice**: "show it to me on my coldcard" / "show <address> on my hardware wallet" / "show #3 on my jade" now display on the device. (HW-007)
- **UTXO list**: registry number, separated sats, confirmed/pending icon, copy-address + copy-txid buttons, click the amount to toggle sats ↔ BTC. (UTXO-005)
- **First connect is quiet**: existing wallets no longer spam one "Incoming" line per historical UTXO — only unconfirmed or ≤3-blocks-confirmed surface, plus one summary line. (CHAT-008)

- [ ] Re-verify the earlier "still open" items: **Resync now** keeps labels (now the v6 per-address set); **gap_limit apply** (increase → auto-rescan now, decrease → tradeoff note); **model-absent path** (download card / No → quick actions). Re-verify, re-file only if broken.

Known limitation: label REMOVAL doesn't exist yet (labels are add-only per your union model — decision #1); say the word if you want a remove command.

TLS note (unchanged): probably not needed anymore; the escape hatch (`tls_verify: false` in repo-root config.json or `LOCALWALLET_TLS_VERIFY=false`) stays. There is no required config.json — add keys only when a log line or feature asks; malformed/unknown keys refuse startup value-free.

Diagnostic command if anything fails (value-free, safe to paste):
`python3 tools/probe_backend_diag.py <url> [--insecure] [--user <u> --password <p>] --json [--minrelay]`

## MW-17 — relaunch verification (remaining open items)

- [x] ✅ Balance in other currencies — VERIFIED by you 2026-09-13 ("EUR/JPY works"); ticket closed.
- [x] ✅ Settings badges back, colored private-vs-public (WEB-023 landed; `.local` = yellow by design — decision #4).
- [ ] Hardware-wallet unlock check: after unlocking via chat, the app now VERIFIES the device holds this wallet's key — mismatch says "…it is not the private key for this wallet" (TCK-HW-006 landed; verify live with your Jade).
- [x] ✅ Header fingerprint chip built (WEB-027) — NOTE: it shows the wallet's ACCOUNT-key fingerprint; your Jade's screen shows its own device fingerprint and they won't match (the "same number" claim was false and was corrected). "Device connected" chip + header host line stay rejected.

## MW-15 — live send/fee run (fee policy v2 + corrected floor)
- [ ] Fee line per YOUR spec: MEDIUM = next projected block's lowest × 1.15; FASTER = double; SLOWER = second block's lowest. Pay line `@ $/BTC`; "faster" twice → asks for a sat/vB rate; explicit rate works. NOW WITH the corrected 0.1 sat/vB floor — sub-1 bids that your node accepts should build.
- [ ] Rate answers consumed: consolidation → "slower" → "0.75 sat/vbyte" now rebuilds the plan (the ask appears ONCE — your round-3 repro). (FEE-007)
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

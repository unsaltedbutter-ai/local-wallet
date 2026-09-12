# DESCOPE-ESPLORA — Remove mempool.space/Esplora as a WALLET-data backend; keep it only for public info (fees + price)

- **Status:** Plan (deliverable; no code changed)
- **Branch:** dev/plan-run-1
- **Tracks:** USER REDIRECTION 2026-09-11 (binding): "we only need to support
  electrum and bitcoind (bitcoin core). we do not need to support mempool.space
  as a source of blockchain information. — so we can eliminate code that uses
  it as a source of wallet information, but we will still be reading public
  information from the public mempool.space. — we can eliminate the badging
  from the settings window — we can eliminate mention of it from the
  onboarding flow — we can eliminate mention of it from the manual work
  document."
- **Scope:** `src/localwallet/chain/`, `src/localwallet/app.py`,
  `src/localwallet/config.py`, `src/localwallet/store/db.py`,
  `src/localwallet/ui/{onboarding.py,web/static/**,web/server.py}`,
  `src/localwallet/node/doctor.py`, `tests/`, `evals/`, docs (ADRs),
  `MANUAL-WORK.md`. ADR-0003/0011/0018/0021/0023 amendments on acceptance.
- **Style note:** this plan mirrors docs/onb-004-backend-adapters-plan.md
  (milestoneed, per-ticket file lists, deps, done-when). M1/M2 of that plan
  already shipped the two wallet backends (Electrum + bitcoind); this plan's
  M1/M2 are therefore *parity verification + gap closure*, and the real work
  is the M3 rewire + M4 deletion.

## 0. The two roles Esplora plays today — REMOVE vs STAY

Today `EsploraClient` is BOTH the wallet-data backend AND the fee/price
fetch path (the `ChainClient` protocol in `chain/esplora.py:462`, with
`get_json` at `:933` deliberately Esplora-only). The redirection splits
these roles:

| Role | Endpoints | Verdict |
|---|---|---|
| **Wallet data** (scan, UTXOs, history, tip, watch, broadcast, tx_status, probe/autodetect, badge, default) | `/address/{a}/txs`, `/address/{a}/utxo`, `/blocks/tip`, `/blocks/0`, `/tx`, `/tx/{id}/status` | **REMOVE** as a wallet backend |
| **Public info** (fee estimator, price oracle) | `/api/v1/fees/recommended`, `/api/v1/fees/mempool-blocks`, `/v1/blocks/{tip}` (recent-blocks floor), `/v1/prices` | **STAY** — read public mempool.space for these, decoupled from the wallet client (user explicitly keeps them) |

Design consequence (M3/M4): the **wallet** `ChainClient` becomes a
two-backend contract (Electrum + bitcoind only). The **fee/price** path
moves to a small standalone public-info fetcher pinned to
`https://mempool.space/api` — it is *not* the wallet client, it never touches
an address, and it rides its own cache/TTL (ADR-0011). `EsploraClient` may
survive only as (or be trimmed to) that public-info fetcher; all
wallet-data call sites and the `check_backend` autodetect acceptance are
deleted.

---

## 1. INVENTORY — every place Esplora serves the WALLET-data role

### 1.1 chain/esplora.py (the wallet-data surface)

- `EsploraClient.get_address_txs` (`esplora.py:633`) — scan history walk.
- `EsploraClient.get_address_utxos` (`esplora.py:643`) — scan UTXO snapshot.
- `EsploraClient.get_tip_height` (`esplora.py:654`) — scan plan + fee
  recent-blocks floor.
- `EsploraClient.get_tip_block` (`esplora.py:704`) — watch
  `time_since_last_block`.
- `EsploraClient.broadcast_tx` (`esplora.py:773`) — money path.
- `EsploraClient.get_tx_status` (`esplora.py:868`) — broadcast recovery.
- `check_backend` (`esplora.py:1063`) — the Esplora-shape probe used by the
  onboarding/autodetect classifier (mainnet genesis proof at `/blocks/0`).
- `ChainConfig.kind == "esplora"` for http(s) (`chain/config.py:99-114`) and
  the `from_settings` fallback to `esplora_base_url`
  (`chain/config.py:243-250`) — the silent public default seam.

**STAY (public-info):** `EsploraClient.get_json` (`:933`) +
`estimate_fee`/`_RECOMMENDED_FEE_KEYS` (`:911`) feed
`chain/fees.py` (floor-follower, `fees.py:295`) and `chain/price.py`
(`get_json("/v1/prices")`, `price.py:294`). These become the standalone
public fetcher.

### 1.2 app.py (selection, probe/autodetect, badges, consent, onboarding)

- `_effective_chain_url` (`app.py:781-787`) — falls back to the public
  `esplora_base_url` when `chain_base_url` is unset (**the silent public
  default**).
- `_build_chain_client` (`app.py:8254`) — dispatches http(s) → `EsploraClient`
  (`:8290`). After M3, http(s) must NOT build a wallet client.
- `_probe_chain_backend` (`app.py:8395`) — the auto-detect classifier:
  `ssl://`→Electrum, `bitcoind://`→Core, `http(s)://`→ Core-shape first then
  **Esplora shape** (`check_backend`, `:8530-8537`). The Esplora acceptance +
  canonical-URL storage must go (an http(s) URL is no longer a wallet backend).
- `_backend_kind` (`app.py:8549`) + `BACKEND_KINDS` (`app.py:6020-6062`) —
  the badge enum includes `public`/`mempool`/`esplora`
  (`app.py:6049-6051`). Only `electrum`/`bitcoind`/`none` remain.
- `_PUBLIC_DEFAULT_HOST` (`app.py:6064-6074`) — derives "mempool.space" from
  the `esplora_base_url` default; deleted with the wallet-public-default.
- `BACKEND_PROBE_FAIL` copy (`app.py:6091`) names Esplora/mempool as a valid
  backend family; rewritten to Electrum + bitcoind only.
- `_backend_mode` (`app.py:3747`) + privacy banner/indicator + `privacy_mode`
  — scheme-agnostic today; the `BACKEND_MODE_PUBLIC` meaning flips from
  "public esplora" to "public electrum".
- Broadcast/tx_status handlers (`app.py:3535`, `app.py:3682`) typed as
  `EsploraClient` (cosmetic — they call the `ChainClient` contract only).
- Watch `_last_block_suffix` (`app.py:3875`) — via `ChainClient.get_tip_block`.
- Consent seam: `_backend_resolved` (`app.py:4375`),
  `set_public_backend_consent` (`app.py:4395`), `BACKEND_CHOICE_SETTING`
  `"public"` — machinery survives, but "public" re-targets to the electrum
  server (see §3).
- Onboarding copy: `CHAT_ONB_BACKEND_OWN` / `CHAT_ONB_BACKEND_PUBLIC`
  (`app.py:5533-5541`) mention "mempool.space running on an Umbrel, MyNode,
  Start9" and "a public server like mempool.space" — both rewritten.
- Startup-scan decision (`app.py:9035-9084`) — `_backend_resolved` gates the
  hold; the headless carve-out (`:9053-9056`) currently scans the public
  default (see §3).

### 1.3 config.py / store / node

- `Settings.esplora_base_url = "https://mempool.space/api"` (`config.py:180`)
  — the public wallet default; repurposed/deleted (§3/OQ-5).
- `resolve_chain_base_url` (`config.py:352`) + stored-rung writer
  `Store.set_chain_base_url` (`store/db.py:983`) — currently validates
  http(s)/ssl://; after M3 the *stored wallet rung* accepts
  electrum/bitcoind only.
- `node/doctor.py` `MEMPOOL_ESPLORA` option + copy (`doctor.py:57,95,133-144,
  195-196,220-221,240`) — the "self-hosted mempool/electrs Esplora API" tier is
  no longer a wallet backend; guidance re-scoped to electrum + Core.

### 1.4 web UI (badges + suggested servers + trust)

- `app.js` `BADGE_FAMILIES` (`app.js:301-304`) — `mempool: {mempool,public,
  esplora}` family removed; `badgeMempool` (`app.js:158`), `badgeLegend`/
  `badgeInUse`/`badgeIdle` (`app.js:176-184`); the dimmed-family comment
  (`app.js:291-300`) and the `BACKEND_KINDS` set (`app.js:307`).
- `styles.css` backend-badge rules (`styles.css:777+`).
- Trust badge (`app.js:989-1006`) rides `/state` `privacy_mode` — **unchanged
  in mechanics**, but `public` maps to the public electrum server (WEB-023
  private-IP-green is orthogonal and stays).
- Suggested-server chips (WEB-022, pending) — re-scoped to electrum servers
  only (§5).

### 1.5 tests / evals / AC / docs / MANUAL-WORK

- `tests/`: ~90 files mention mempool/esplora, almost all mock fixtures
  (`MockTransport`, `ScriptedServer`). Live/`check_backend`/backend-switch/
  TLS/e2e tests that exercise the Esplora wallet path are the cleanup target.
- `evals/`: **no** mempool/esplora references found in `evals/` — the
  narration fixtures are backend-agnostic, so eval impact is limited to
  consent-copy fixtures (MINIMAL).
- Phase AC docs: `docs/phase1-ac.md`, `docs/phase3-ac.md`, `docs/sparrow-ac.md`
  cross-check against mempool.space — re-point to electrum.
- `MANUAL-WORK.md:22,28,32` (mempool `[]` tip, private-mempool `evil-star.local`
  test, autodetect mempool classification) — removed per the redirection.
- ADRs: 0003 (public mempool default — superseded for the wallet),
  0011 (fee/price — amend §3 for the decoupled public source), 0018
  (fallback + badge enum — amend), 0021 (public-default caveat wording),
  0023 (default-stays-public + copy — amend).

---

## 2. REPLACEMENT MAP — per wallet function, per backend

The wallet `ChainClient` contract (scan/utxo/history/tip/broadcast/tx_status/
estimate_fee) is already satisfied by both adapters (docs/onb-004 plan §0).
This is the parity confirmation + gap ledger.

| Wallet function | Electrum (M1) | bitcoind (M2) |
|---|---|---|
| Scan / UTXOs | `blockchain.scripthash.listunspent` → `get_address_utxos` (`electrum.py:330`) ✓ | `scantxoutset` snapshot → `get_address_utxos` (`bitcoind.py:533`) ✓ (**unspent-only**) |
| History | `scripthash.get_history` + `transaction.get(verbose)` N+1 → `get_address_txs` (`electrum.py:286`) ✓ | funding-of-unspents via `getrawtransaction(verbose)` (`bitcoind.py:551`) ✓ (**no spent history** — documented tradeoff) |
| Tip height | `blockchain.headers.subscribe` (`electrum.py:365`) ✓ | `getblockchaininfo.blocks` (`bitcoind.py:577`) ✓ |
| Tip block | `headers.subscribe` 80-byte header, LE bytes 68–72 (`electrum.py:369`) ✓ | `getblockchaininfo` bestblockhash → `getblockheader` (`bitcoind.py:592`) ✓ |
| Broadcast | `blockchain.transaction.broadcast` single-attempt + txid bind (`electrum.py:392`) ✓ | `sendrawtransaction` single-attempt + txid bind (`bitcoind.py:625`) ✓ |
| tx_status | `transaction.get(verbose)` → TxStatus (`electrum.py:423`) ✓ | `getrawtransaction` → TxStatus (`bitcoind.py:658`) ✓ |
| Watch poll | **No `scripthash.subscribe`** — poll rides the ChainWorker re-scan (`electrum.py` deviation 2, plan §1) | No subscribe — poll rides the ChainWorker re-scan |
| estimate_fee | `blockchain.estimatefee` (`electrum.py:461`) ✓ advisory/single-rate | `estimatesmartfee` CONSERVATIVE (`bitcoind.py:696`) ✓ |
| price | `supports_price=False` → **replaced by the public fetcher** | same |

**M1 gaps to CLOSE (electrum):**
1. `blockchain.scripthash.get_balance` — **not implemented, not needed**:
   balance derives from the UTXO snapshot. Verify `balance_from_utxos`
   (`esplora.py:246`) is backend-agnostic (it is — it reads the translated
   `status.confirmed`/`value` shape). No code.
2. `blockchain.scripthash.subscribe` — **not implemented**. Today's watch poll
   = full re-scan via ChainWorker, correct but heavier. `subscribe` is an
   OPTIMIZATION (skip the re-scan when the status string is unchanged), not a
   correctness gap. Defer; a `ponytail:` note covers it.
3. `estimatefee` param advisory / single-rate answers — accepted and
   documented (tx engine min-relay floor binds the lower rail).
4. History N+1 cost — accepted (plan OQ-3), bounded by the gap window.

**M2 gaps to CLOSE (bitcoind):** none functional — the unspent-only-history
tradeoff is the honest ceiling (Core has no per-address index without a
wallet; watch-only forbids importing). A user needing full spent history
points at an electrum backend instead. Rescan widens the descriptor range.

**Tip-height source per backend (the §0/§5 note asked):** electrum →
`blockchain.headers.subscribe`; bitcoind → `getblockchaininfo.blocks`.

---

## 3. DEFAULT / CONSENT POLICY — no silent public default

**Current state (must change):** `Settings.esplora_base_url`
(`config.py:180`) = `https://mempool.space/api`; `_effective_chain_url`
(`app.py:781-787`) and `ChainConfig.from_settings` (`chain/config.py:243-250`)
fall back to it when `chain_base_url` is unset. ONB-006's `_backend_resolved`
(`app.py:4375`) treats an UNSET rung as *unresolved* (hold scan at
`awaiting_backend`, `app.py:9059-9061`) **except** the headless carve-out
(`app.py:9053-9056`), which scans the public default.

**New policy:**
- **First run REQUIRES the user to name an electrum or bitcoind server.**
  No wallet backend resolves from an empty `chain_base_url`. The
  `esplora_base_url` fallback is removed from the WALLET selection path;
  an unset `chain_base_url` = unresolved = `awaiting_backend` (no scan, no
  watch, `create_tx` refuses, cache reads `stale`) on interactive and web.
- **The "use public" option becomes an EXPLICIT PUBLIC ELECTRUM choice** —
  `ssl://electrum.blockstream.info:50002` (verified reachable + mainnet
  2026-09-11), carrying the red leak warning (its operator can see every
  queried address + IP + tx timing). This is a *warned, explicit* choice
  recorded through the existing `set_public_backend_consent` seam
  (`app.py:4395`) / `BACKEND_CHOICE_SETTING="public"` — never a silent default.
- **Headless / AUTO_SCAN=1:** today a non-interactive launch skips the hold
  and scans the public default (`app.py:9053-9056`). **Proposed honest
  behavior: refuse to auto-scan when the backend is unresolved.** A headless
  launch with no `chain_base_url`/config rung and no stored consent has no
  place to ask for a server, so it must not leak to an unchosen one — it
  fails with a value-free line naming the missing backend config
  (recommended default, OQ-2). "The command line is the operator's decision"
  now means *an explicit server in the command line/config*.

### Consent-discipline check (ONB-006 / PRIVACY-001B / ADR-0022 / ADR-0023)

The consent *machinery* (unresolved⇒no-call, explicit-public marker, skip≠
consent, `/consent` button) is correct and stays. What changes is the TARGET
and the *implicit-default* semantics:

1. **ADR-0023 decision 2 "the default stays public" is SUPERSEDED.** There is
   no wallet public default; public is a named, warned electrum choice.
2. **ADR-0023 copy blocks (a)/(e) + `CHAT_ONB_BACKEND_*` (`app.py:5533-5541`)**
   rewritten: no "public default", no "mempool.space on Umbrel/MyNode/Start9"
   as a backend option; the own-server tier = electrum or bitcoind; the public
   tier = the named public electrum server with the leak warning.
3. **ADR-0022 amendment 1 / ONB-006** wording "never fetch through a server
   the user refused" — unchanged; the headless carve-out is amended (see
   above).
4. **PRIVACY-001/001B** consent copy re-points public → electrum; the
   `/consent` button's leak sentence unchanged in substance.
5. **Fees/prices need NO consent gate** (they carry no wallet addresses —
   see §4); the existing `LOCALWALLET_PRICE_ENABLED` opt-out stays.

---

## 4. FEES / PRICES — decouple from the wallet ChainClient (ADR-0011)

Today `FeeEstimator(client)` and `PriceOracle(client)` ride the wallet client
(`app.py:8852-8853`, `:8990-8991`, `:8832`). On electrum/bitcoind this means:
fees drop to backend-native single `estimate_fee` (no floor-follower), and
price is **unavailable** (`supports_price=False`, `price.py:391`) → sats-only
for `create_tx`. The redirection explicitly wants mempool.space fees+prices
back for everyone.

**Recommendation: a small standalone public-info fetcher** pinned to
`https://mempool.space/api`, constructed ONCE (not per backend, not rebuilt on
hot-swap), used ONLY by `FeeEstimator` (floor-follower + recent-blocks floor)
and `PriceOracle`:

- `chain/fees.py` `_floor_follower` (`:295`) already reads `get_json` paths —
  it just calls them on the injected client. Feed it the public fetcher.
  Its tip for `/v1/blocks/{tip}` should come from the PUBLIC fetcher too
  (self-contained; not the wallet tip).
- `chain/price.py` `get_json("/v1/prices")` (`:294`) — feed it the public
  fetcher.
- The backend-native fee path (`fees.py:250-256`) and `supports_price` become
  **dead** — the public fetcher restores floor-follower + price on every
  backend. Keep `estimate_fee` on the wallet adapters (harmless, unused by the
  estimator) or note it as future per-backend fallback.
- The `get_json` capability seam on `ChainClient` (`esplora.py:472-477`) is
  retired from the WALLET contract.

**Why not "ride per-backend estimatefee with mempool enrichment"?** That
resurrects two fee sources with different provenance (`FeeSource`), splits
the floor-follower across backends, and leaves price backend-dependent. The
standalone fetcher is one source, one cache, one TTL — simpler and matches the
redirection ("reading public info from the public mempool.space").

**ADR-0011 implications (amendment required):** decision 1's rationale —
"same host as the chain queries, so no *additional* third party" — **no longer
holds** when the wallet backend is electrum/bitcoind: fees/prices now always
come from mempool.space as an independent public source. This is fine (the
payloads are public aggregations with no wallet addresses) but the ADR must
say so, and the privacy note updates: only the app's IP + timing are visible
to mempool.space for these calls (never an address). `LOCALWALLET_PRICE_TTL_S`
and `LOCALWALLET_PRICE_ENABLED` and the multi-currency closed enum are
unchanged. This is the same provider the floor-follower already pins against
(FEE-001).

---

## 5. MILESTONE SLICING (small, committable)

### M1 — Electrum wallet-scan parity + gap closure (VERIFY, mostly shipped)

- **Files:** `chain/electrum.py` (no change expected), `tests/test_chain_electrum.py`,
  `wallet/scan.py` (verify backend-agnostic — no change expected).
- **Work:** confirm the §2 M1 coverage matrix end-to-end via the fixture
  server + one documented live probe (`ssl://electrum.blockstream.info:50002`);
  record the `scripthash.subscribe`-as-optimization `ponytail:` note.
- **Done when:** electrum drives scan/utxo/history/tip/watch/broadcast/
  tx_status with the existing suite green; M1 gap ledger (§2) closed.
- **Deps:** none. **Security review:** yes (broadcast/txid binding already
  present — re-verify).

### M2 — bitcoind parity check (VERIFY, mostly shipped)

- **Files:** `chain/bitcoind.py` (no change expected),
  `tests/test_chain_bitcoind.py`.
- **Work:** confirm the §2 M2 matrix; re-pin the unspent-only-history tradeoff
  assertions.
- **Done when:** bitcoind drives full wallet scan with the documented tradeoff.
- **Deps:** M1. **Security review:** yes (already gated; re-verify).

### M3 — app.py default/consent/onboarding rewire + badge removal + autodetect re-scope (THE CORE MILESTONE)

- **Files:**
  - `src/localwallet/app.py`: `_effective_chain_url` (`:781-787`),
    `_build_chain_client` (`:8254` — http(s) no longer builds a wallet
    client; refuse/require electrum/bitcoind), `_probe_chain_backend`
    (`:8395` — drop Esplora-shape acceptance; classify electrum/bitcoind
    only), `_backend_kind` + `BACKEND_KINDS` (`:8549`, `:6020-6062` — drop
    public/mempool/esplora), `BACKEND_PROBE_FAIL` (`:6091`),
    `_PUBLIC_DEFAULT_HOST` (`:6064`), `_backend_mode` (`:3747`), consent copy
    + `CHAT_ONB_BACKEND_*` (`:5533-5541`), headless carve-out (`:9053-9056`),
    the two `EsploraClient`-typed handler signatures (`:3535`, `:3682` →
    `ChainClient`).
  - `src/localwallet/config.py`: `esplora_base_url` no longer the WALLET
    fallback (repurposed for the public-info fetcher or removed — OQ-5);
    `resolve_chain_base_url` ladder.
  - `src/localwallet/chain/config.py`: `ChainConfig.kind`/`from_settings`
    (`:99-114`, `:243-250`) — drop the `esplora` wallet kind; empty
    `chain_base_url` = unresolved, not public-default.
  - `src/localwallet/store/db.py`: `set_chain_base_url` (`:983`) wallet rung
    accepts electrum/bitcoind only.
  - `src/localwallet/ui/onboarding.py`: public branch + copy → public electrum.
  - `src/localwallet/ui/web/static/app.js` (`BADGE_FAMILIES` `:301-304`,
    badge words `:158-184`, `:291-300`), `styles.css` (`:777+`),
    `server.py` (probe/apply copy).
  - `src/localwallet/node/doctor.py` (`:57,95,133-144,195-196,220-221,240`).
- **Work:** no silent public default (first run requires a named server);
  "use public" = explicit public electrum with red leak warning; badges
  trimmed to electrum/bitcoind/none; autodetect accepts only electrum/bitcoind;
  headless unresolved = refuse scan; fees/prices decoupled (M3 also wires the
  §4 public fetcher so electrum/bitcoind keep floor-follower + price).
- **Done when:** first run requires a named electrum/bitcoind server; the
  public electrum consent shows the leak warning; no mempool/esplora badge or
  onboarding mention; headless refuses unresolved; full suite + evals green.
- **Deps:** M1, M2. **Security review:** yes (consent + privacy + no-silent-
  fallback; the ONB-006/PRIVACY-001 discipline is preserved).

### M4 — deletion of esplora wallet paths + test/eval/AC/docs cleanup

- **Files:** `chain/esplora.py` (delete wallet-data methods + `check_backend`
  autodetect acceptance; retain/trim only the public-info fetch for fees+price
  — or split into `chain/publicinfo.py`), `chain/fees.py`, `chain/price.py`
  (point at the public fetcher; retire backend-native/`supports_price` dead
  paths), `tools/probe_backend_diag.py`, `tests/` (retire the ~90-file Esplora
  wallet mocks / live/backend-switch/TLS/e2e tests that exercised the Esplora
  wallet path; re-point AC cross-checks), `evals/` (any consent-copy fixtures),
  `MANUAL-WORK.md:22,28,32`, `README`, `HANDOFF`, docs (ADRs 0003/0011/0018/
  0021/0023 amendments).
- **Done when:** no wallet-data path touches Esplora/mempool; fees+prices read
  the public mempool fetcher; suite green; docs/MANUAL-WORK consistent.
- **Deps:** M3. **Security review:** yes (any chain/ touch).

### Queued tickets AFFECTED / RE-SCOPED

| Ticket | Impact |
|---|---|
| **WEB-021** (settings star rework) | Server card "Now using:" trust badge rides `privacy_mode` (unchanged); remove the mempool family badge + "mempool" wording; kind pill = electrum/bitcoind. |
| **WEB-022** (suggested public servers) | Re-scoped: the ONLY public wallet option is an electrum server (list starts with `ssl://electrum.blockstream.info:50002`); mempool.space is removed as a *wallet* suggestion (it remains only as the fee/price source, not user-facing). Chips ride the consent+probe path unchanged. |
| **WEB-023** (private-IP green) | Orthogonal; `public` (red) now maps to the public electrum server; classification still rides `privacy_mode`. |
| **BACKEND-005** (public default preference) | SUPERSEDED in spirit: mempool.space is no longer a wallet backend, so "public default = electrum.blockstream.info" becomes the ONLY public wallet option (BACKEND-005's getlynx mainnet-check investigation still applies — verify our electrum genesis check before listing servers). |
| **PRIVACY-001/001B** | Consent copy re-points public → electrum; mechanics unchanged. |
| **ONB-007** | Backend beat copy (`app.py:5533-5541`) drops mempool mention; names electrum/bitcoind only. |
| **WEB-013/WEB-008/UX-007** | First-run chain ask copy re-scope (electrum/bitcoind only; no mempool). |
| **HW-005, CHAT-*** | Unaffected (signer/chat); no re-scope. |
| **BACKEND-003/004** (Start9 mempool root-causes) | The Esplora wallet fixes (the `/api` tolerance, list-wrapped genesis, `[]`-tip fallback) become dead weight once Esplora is not a wallet backend — retired in M4 with their pins. |

---

## 6. RISKS

- **User's current setup:** runs Start9 **electrum + bitcoind** — unaffected,
  actually the primary beneficiary (wallet path already electrum/bitcoind).
  BUT if they currently use a private mempool/Esplora backend
  (`https://evil-star.local:56191`, MW-16) for the wallet, that becomes
  unsupported → they must switch to their electrum/bitcoind. Called out in
  M3/M4 copy.
- **Mid-migration behavior:** M3 lands the rewire *before* M4 deletes paths,
  so there is no window where the wallet is backendless. Because M3 removes
  the silent public default, an existing install with an empty
  `chain_base_url` (previously = public mempool) will start holding at
  `awaiting_backend` — a one-time re-prompt is expected and must be honest.
- **Eval fixtures:** `evals/` has no mempool/esplora references (verified),
  so eval impact is limited to consent-copy narration fixtures — low.
- **Phase AC docs:** `phase1-ac.md` / `phase3-ac.md` / `sparrow-ac.md` live
  cross-checks against mempool.space must re-point to electrum (M4).
- **Live-network tests:** `check_backend`/backend-switch/TLS/e2e tests that
  exercise the Esplora wallet path (or hit public mempool) are retired or
  re-mocked in M4; the public fee/price fetcher keeps its own hermetic tests.
- **Public electrum single point of failure:** with mempool removed as a
  wallet backend, the only *public wallet* option is
  `electrum.blockstream.info` — a single operator/availability. That is the
  explicit trade the redirection accepts; own-node (electrum/bitcoind) remains
  the recommended path, and the consent warning says so.
- **Fee/price independence:** after M3 the fee floor-follower + price run off
  the public mempool fetcher regardless of wallet backend — a new, always-on
  mempool.space dependency for fees/prices. Payloads carry no addresses; the
  ADR-0011 amendment records the independent-provider privacy note (§4).
- **Dead-weight cleanup:** the ONB/BACKEND Esplora wallet fixes (M4 retires
  them) have pins that must be removed together, or the suite will fail on
  now-impossible behavior.

---

## 7. Open questions for the orchestrator (each with a recommended default)

1. **Is the ONLY public wallet option the electrum server — mempool.space
   removed as a wallet backend entirely?** **Default: yes** — per the
   redirection, remove mempool.space from the wallet role outright; public =
   `ssl://electrum.blockstream.info:50002` (an explicitly warned, consented
   choice).
2. **Headless / AUTO_SCAN=1 with an unresolved backend: refuse, or require
   config?** **Default: refuse to auto-scan** with a value-free exit naming
   the missing backend config — no server consent exists in headless, so it
   must not scan an unchosen one (amends ADR-0023's headless carve-out).
3. **Fees/prices: standalone public mempool fetcher, or ride per-backend
   estimatefee with mempool enrichment?** **Default: standalone public fetcher**
   (one source/cache/TTL; restores floor-follower + price on every backend;
   §4).
4. **Fate of `EsploraClient`/`check_backend`:** delete all wallet-data paths
   but keep a trimmed Esplora client solely for public fees+prices, or split
   a dedicated `chain/publicinfo.py`? **Default: retain a trimmed Esplora
   fetch only for fees/price; delete `check_backend` autodetect acceptance and
   every wallet-data call site.**
5. **`esplora_base_url` + its public fallback:** keep as the (repurposed)
   public-info base, or remove and pin the fetcher to
   `https://mempool.space/api` directly? **Default: keep the field, repurposed
   as the public-info base** (still env/config-overridable), and remove it
   from the WALLET selection path — empty `chain_base_url` = unresolved.

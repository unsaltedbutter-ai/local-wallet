# ONB-004 — Backend adapters plan (Electrum + bitcoind RPC + auto-detect/creds)

- **Status:** M1+M2 IMPLEMENTED (TCK-ONB-004 M1 2026-09-09, M2 2026-09-10 —
  status notes at §1/§2); M3 plan (pre-implementation)
- **Branch:** dev/plan-run-1
- **Tracks:** TCK-ONB-004 (ADR-0023 decision 7 backlog)
- **Scope:** `src/localwallet/chain/`, `src/localwallet/ui/onboarding.py`, `/setup`, web settings, `src/localwallet/config.py`, tests. ADR-0018/0023 amendment on acceptance.

## 0. What the app actually calls — the real interface surface

Every consumer calls one concrete class, `EsploraClient`, injected as `client:`.
Derived from actual call sites (not intent):

| Method | Return | Caller(s) |
|---|---|---|
| `get_address_txs(addr)` | `list[dict]` (Esplora /address/{a}/txs) | scan `_walk_history` |
| `get_address_utxos(addr)` | `list[dict]` (Esplora /address/{a}/utxo) | scan `_scan_utxos` |
| `get_tip_height()` | `int` | scan `fetch_scan`, fees `_floor_follower` |
| `get_tip_block()` | `TipBlock(height, timestamp)` | watch `time_since_last_block` |
| `broadcast_tx(tx_hex)` | `str` (txid, bound to tx) | app broadcast flow |
| `get_tx_status(txid)` | `TxStatus(txid, confirmed, block_height, block_time)` | app broadcast recovery |
| `get_json(path, kind)` | `Any` (raw Esplora JSON) | fees `/v1/fees/recommended`, `/v1/fees/mempool-blocks`, `/v1/blocks/{tip}`; price `/v1/prices`; `check_backend` `/blocks/0` |
| `close()` + context-manager | — | everywhere |

### Contract split: Esplora-shaped JSON vs adapter-translatable

- **Backend-agnostic outputs (adapters must produce, translators own):**
  - `get_tip_height → int`, `get_tip_block → TipBlock`, `broadcast_tx → txid str`,
    `get_tx_status → TxStatus`. Clean, validated dataclasses/primitives.
- **Esplora-shaped JSON (adapters must TRANSLATE their protocol into these exact
  shapes, because scan.py's strict parsers consume them):**
  - `get_address_txs` → entries with `txid`(64-hex), `status{confirmed,block_height,block_time}`,
    optional `fee`, `vin[].prevout.scriptpubkey_address`, `vout[].scriptpubkey_address`
    (see `scan._parse_tx_entry`).
  - `get_address_utxos` → entries with `txid`, `vout`, `value`, `status.confirmed`,
    optional `status.block_height` (see `scan._parse_utxo_entry`).
- **Esplora-URL-only, NOT generically translatable — must be reworked or gated:**
  - `get_json("/v1/prices")` — no electrum/bitcoind equivalent. **Price is Esplora-only.**
  - `get_json("/v1/fees/...")` + `/v1/blocks/{tip}` floor-follower — Esplora mempool
    projections. Electrum/bitcoind offer a **single** `estimatefee`/`estimatesmartfee`,
    no floor projections.

**Design consequence (do this in M1):** introduce a `ChainClient` protocol
(with the 6 semantic methods + `close`/context + a small capability flag) that
`EsploraClient`, `ElectrumClient`, `BitcoindClient` all satisfy. Add **one** new
backend-agnostic semantic method to the contract:

```python
class ChainClient(Protocol):
    get_address_txs(addr) -> list[dict]     # Esplora-shaped (translated)
    get_address_utxos(addr) -> list[dict]   # Esplora-shaped (translated)
    get_tip_height() -> int
    get_tip_block() -> TipBlock
    broadcast_tx(tx_hex) -> str
    get_tx_status(txid) -> TxStatus
    estimate_fee(target: FeeTarget) -> int  # sat/vB — NEW, backend-native
    supports_price: bool = False            # NEW capability
    close() -> None; __enter__/__exit__
```

- **Fees:** `FeeEstimator` keeps its Esplora floor-follower for `EsploraClient`;
  for backends without mempool-blocks it calls `estimate_fee(target)` and uses the
  recommended-style single source (skip the floor-follower branch, never fabricate).
- **Price:** `PriceOracle` requires `supports_price`; on electrum/bitcoind it raises
  `PriceUnavailableError` → USD-denominated `create_tx` refuses cleanly, sats-only
  still works. Keeps `get_json` off the protocol entirely.

`check_backend` stays Esplora-shaped; each backend gets its own probe (see M3).

---

## 1. M1 — Electrum-protocol client (`chain/electrum.py`)

**Transport:** stdlib `ssl.SSLContext` + `socket.create_connection` → JSON-lines
over the socket. **No new dependency — stdlib is sufficient** (documented; add a dep
only if the strict fail-closed/retry story cannot be met with stdlib, which it can).
TLS trust mirrors `tls_verify` (`ssl.SSLContext.verify_mode`, hostname check; honor
the same env/file knob as Esplora). One request at a time, read one JSON line per
response; **skip** id-less/notification lines and any non-JSON greeting line a server
sends on connect (tolerance).

**Protocol surface → contract mapping** (JSON-RPC-ish stratum, `{"id":n,"method":...,"params":[...]}`):

| Electrum call | → contract method | Translation |
|---|---|---|
| `server.version(c,s)` | (connect handshake) | 2 params (version, proto-min) required by spec |
| `blockchain.scripthash.get_history(h)` | `get_address_txs` | returns `[{tx_hash,height,fee}]`; then `blockchain.transaction.get(tx, verbose=true)` per tx to build vin/vout/status (N+1 — see OQ-3) |
| `blockchain.scripthash.listunspent(h)` | `get_address_utxos` | `{tx_hash,tx_pos,value,height}` → `{txid,vout,value,status:{confirmed,block_height}}` (confirmed = height>0) |
| `blockchain.headers.tip` | `get_tip_height` | `{height}` |
| `blockchain.block.header(height)` (+ `headers.tip` for height) | `get_tip_block` | 80-byte header; timestamp = LE bytes 68–72. `None` timestamp is the clean-unavailable fallback |
| `blockchain.transaction.broadcast(raw_hex)` | `broadcast_tx` | txid returned; re-bind to expected txid exactly like Esplora |
| `blockchain.transaction.get(tx, verbose=true)` | `get_tx_status` | `{confirmations,blockheight,time}` → `TxStatus` (confirmed = confirmations>0) |
| `estimatefee(number)` | `estimate_fee` | returns BTC/kB (fractional) → sat/vB = `round(x*100_000)`. Map FAST=1, MEDIUM=2, SLOW=6 (advisory; servers vary) |
| `blockchain.scripthash.subscribe(h)` | (watch poll) | status string; used by watch to avoid full re-scan on poll |

**scripthash convention:** `hashlib.sha256(script_bytes)[::-1].hex()`, where
`script_bytes` = the address's output script (embit `scriptpubkey` from our address).
All addresses the app holds are mainnet scripts (ADR-0021) — no testnet path.

**Risks/tolerances to document:** `estimatefee`'s target param is advisory and some
servers ignore it (return a single rate) — accept, floor via min-relay in tx engine;
batching/notifications vary — we never batch and skip id-less lines; server banners
differ — tolerate one pre-answer non-JSON line; `server.version` result may be a list
or string — accept both.

**Tests:** a **local fixture** stratum stub — a real `ssl` socket server (self-signed
cert generated in-test) in `tests/`, serving scripted JSON-line responses; **no
network in CI** (binds 127.0.0.1). Mirror the `ScriptedServer` pattern
(`tests/test_chain_esplora.py`) with an `ElectrumServer` fixture + a
`transport=`/socket-injection seam so tests stay hermetic. Reuse the scan tests'
`MockTransport` seam by keeping the injected client interface identical.
**Live probe (documented, manual):** point at a real electrum server
(e.g. `electrum.blockstream.info:50002` ssl) with a throwaway xpub and assert tip +
one address's history shape; never automated.

### M1 status notes (implemented 2026-09-09)

- Shipped as designed: `chain/electrum.py` (`ElectrumClient`, stdlib `ssl` +
  JSON-lines, sequential, no batching), `ChainConfig.kind` + `ssl://` scheme
  validation, the scheme dispatch at the one construction site
  (`app._build_chain_client`), the `ChainClient` protocol +
  `estimate_fee`/`supports_price` capability seam (fees native path, price
  refuses fail-closed to sats-only), `tests/test_chain_electrum.py` (76
  tests against the loopback fixture — includes a `fetch_scan` run whose
  `ScanRecords` are field-by-field EQUAL to the Esplora mock's on the same
  scenario, so the scan seam is provably unedited).
- Deviations, documented: (1) tip via `blockchain.headers.subscribe`
  (universal; its result already carries the raw header — `blockchain.headers.tip`
  + `blockchain.block.header` NOT used); (2) no
  `blockchain.scripthash.get_balance`/`subscribe` methods — zero v1 callers
  (balance comes from the UTXO snapshot; the watch poll rides the unchanged
  ChainWorker re-scan); (3) mainnet enforcement lives IN the adapter
  handshake (`server.features.genesis_hash`) because M1 exposes no ssl://
  setup probe — M3's probe re-adds the check at entry time; (4) the
  `/setup` + first-run entry still refuse `ssl://` (entry UX + stored rung +
  credentials are M3); M1 selects via env (`LOCALWALLET_CHAIN_BASE_URL=ssl://…`)
  or the config-file key only; (5) `estimatefee` answers arrive per-target
  advisory as planned — accepted, tx engine min-relay floor unchanged.
- Privacy banner behavior on `ssl://`: `_backend_mode`/`privacy_indicator`
  are scheme-agnostic (loopback → "own node, this machine"; remote host →
  "own node, another machine"), and `LOCALWALLET_TLS_VERIFY=0` prints the
  SAME `TLS_UNVERIFIED_WARNING` on either adapter; price-dependent surfaces
  degrade through the existing `price_unavailable` ladder.
- **Manual live probe (never in CI, never automated):**
  `LOCALWALLET_CHAIN_BASE_URL=ssl://electrum.blockstream.info:50002
  .venv/bin/python -c "import sys; sys.path.insert(0,'src'); from
  localwallet.chain import ElectrumClient; c = ElectrumClient();
  print(c.get_tip_height()); print(c.get_address_txs('<throwaway-mainnet-address>'))"`
  — expect a plausible tip and Esplora-shaped entries; a non-mainnet server
  must fail the handshake with `does not serve mainnet`. Use a disposable
  xpub-derived address only.

---

## 2. M2 — bitcoind RPC (`chain/bitcoind.py`)

**Transport:** JSON-RPC 2.0 over HTTP POST; **use httpx** (already a dep, consistent
retry/error discipline; `urllib`/`socket` also allowed inside `chain/` but httpx avoids
a second transport). Body `{"jsonrpc":"2.0","id":n,"method":...,"params":[...]}`, parse
`result`/`error`. **TLS:** Core RPC is normally **http on loopback**; https is rare
(needs `-rpcssl` or a TLS reverse proxy). Accept http(s); reuse `tls_verify` if https.

**Auth:** HTTP Basic. Sources, in order: user/pass fields, OR cookie (read
`~/.bitcoin/.cookie` `user:pass` line, or a user-supplied cookie-path — the file is
**local**, so it only works on the same machine; remote nodes use user/pass), OR
"no credentials needed" = omit the Authorization header entirely. Cookie content is a
secret — never logged/echoed (value-free discipline).

**The scan problem — recommend option (a) `scantxoutset`:**

| Option | Approach | Verdict |
|---|---|---|
| **(a) scantxoutset + per-txid getrawtransaction** | `scantxoutset("start",["desc(wpkh(<xpub>/0/*))","desc(wpkh(<xpub>/1/*))"])` with a range → server-side UTXO snapshot; per-unspent `getrawtransaction(txid,true)` for funding history | **RECOMMEND.** Watch-only exact (only scripts/descriptors sent, **no keys imported**); server computes UTXOs + used-window at once, no wallet needed |
| (b) per-address `listunspent`/`scantxoutset` combos | needs a **wallet** for listunspent; per-address N calls | Rejected — we never import keys (watch-only invariant) |
| (c) wallet-enabled + `rescan` | imports addresses into a wallet | Rejected — violates watch-only (never import keys) |

**Rationale for (a):** `scantxoutset` takes descriptors/scripts, not keys — the only
option that stays watch-only and needs no wallet. It returns `{success,height,unspents:[{txid,vout,scriptPubKey,amount,height}],total_amount}`; translate to `get_address_utxos` entries directly (amount BTC→sats, `height>0`→confirmed). The descriptor range covers both branches; the returned indices give the used-window without the incremental gap walk.

**Documented limitation (honest, watch-only):** Core has **no per-address spent-history
enumeration without a wallet/address-index.** `scantxoutset` sees only *unspent*
outputs, so **fully-spent addresses' history is not surfaced** with a raw bitcoind
backend. `get_address_txs` returns history only for unspent funding txs (in direction).
If the user needs full spent-history, point at Esplora/electrum. Rescan widens the
descriptor range; never fabricate a "used" or history claim (fail closed).

**RPC → contract mapping:**

| RPC | → contract |
|---|---|
| `scantxoutset` + `getrawtransaction(txid,true)` | `get_address_txs` (funding of unspents) / `get_address_utxos` |
| `getblockchaininfo` | `get_tip_height` (`.blocks`); `.chain` must == `"main"` (mainnet-only, ADR-0021); `.blocks < .headers` = syncing → report progress (ADR-0023 d5) |
| `getblockhash`+`getblockheader` (or `getblockchaininfo` bestblockhash→`getblockheader` verbose) | `get_tip_block` timestamp (`.time`); `None`-clean fallback |
| `sendrawtransaction(hex)` | `broadcast_tx` (txid; re-bind to expected) |
| `getrawtransaction(txid,true)` | `get_tx_status` (`.confirmations`,`.blockheight`,`.time`) |
| `estimatesmartfee(conf_target)` | `estimate_fee` (`.feerate` BTC/kB → sat/vB `round(x*1e5)`); map FAST=1, MEDIUM=2, SLOW=6. May error (warmup/no mempool) → `ChainError`, never fabricate |

**Tests:** local fixture JSON-RPC stub (stdlib `http.server` on loopback) serving
scripted RPC responses + cookie/user-pass auth variants; no network in CI. Live probe:
documented manual procedure against a local regtest/mainnet Core.

### M2 status notes (implemented 2026-09-10)

- Shipped as designed: `chain/bitcoind.py` (`BitcoindClient`, JSON-RPC 1.0,
  cookie AND user/pass auth, `scantxoutset` descriptor scan, verbose
  `getrawtransaction` status/history assembly, `sendrawtransaction`
  single-attempt + embit txid binding, `estimatesmartfee` CONSERVATIVE
  mapping FAST=1/MEDIUM=2/SLOW=6, `getblockchaininfo` tip + mainnet gate,
  `getnetworkinfo` capability floor), the `bitcoind://[user:pass@]host[:port]`
  scheme at the single selection point, `supports_price=False` on the M1
  capability seam, `tests/test_chain_bitcoind.py` (125 tests against the
  loopback fixture — auth matrix, snapshot-cache semantics, and the
  scan-equivalence run with the §2 unspent-only divergence asserted
  EXACTLY: the confirmed surviving coin is field-by-field identical to the
  Esplora mock's, spent/mempool history is pinned as honest absence).
- Deviations, documented: (1) **transport is stdlib `http.client`, not
  httpx** — the M2 ticket mandates stdlib within `chain/`'s rules (this
  section's "use httpx" line traded against it; one-RPC-per-connection
  framing keeps the electrum reconnect-discipline analogue trivial);
  (2) **JSON-RPC 1.0**, not 2.0 as this section sketched — what Core
  actually speaks (the ticket pins it; matches `node/detect.py`);
  (3) mainnet + capability (Core ≥ 22) enforcement lives IN the adapter
  handshake (like M1's deviation 3) and the app's `_probe_chain_backend`
  gained the `bitcoind://` dispatch NOW (small: one tip call through the
  same gate), while the STORED rung's shape validation stays http(s)/ssl://
  — storing a `bitcoind://` choice (and the credentials UX) is M3's entry
  work (ticket sanctioned scheme: env/config-file ladder only);
  (4) M2 selection is the explicit `bitcoind://` scheme — the http(s)
  Esplora-then-Core autodetect remains exactly as §3 scopes it;
  (5) the UTXO-set walk is cached per client at the tip height (an
  implementation necessity: per-address `scantxoutset` with no cache is one
  whole-set walk per probe); same-height reorg staleness until the next
  block is the accepted ceiling (ADR-0018 M2 amendment).
- **Manual live probe (never in CI, never automated):** run a local
  mainnet Core with `-rpcport=8332`, then
  `LOCALWALLET_CHAIN_BASE_URL=bitcoind://127.0.0.1:8332 .venv/bin/python -c
  "import sys; sys.path.insert(0,'src'); from localwallet.chain import
  BitcoindClient; c = BitcoindClient(); print(c.get_tip_height());
  print(c.get_address_utxos('<throwaway-mainnet-address>'))"` — cookie auth
  resolves via `~/.bitcoin/.cookie` automatically; a testnet/signet node
  must fail with `does not serve mainnet`, a pre-22 node with the
  capability refusal. Expect unspent-only answers (the tradeoff above).

---

## 3. M3 — Auto-detect + credentials UX

**URL classification** (never ask the user to classify; bounded timeouts, clear fail):

```
scheme ssl://       -> Electrum  (immediate; the electrum ports 50001/50002)
scheme https://     -> Esplora first (probe GET /api/blocks/tip → int/block-list or
                       /v1/fees/recommended → {fastestFee,...}).
                       If not Esplora-shaped, try Core RPC (POST /getblockchaininfo
                       → {chain,blocks}) — https Core is rare; https is Esplora-primary.
scheme http://      -> ambiguous: probe Esplora shape first (GET /api/blocks/tip),
                       then Core RPC (POST /getblockchaininfo). Order: Esplora-then-Core
                       (cheap GET; loopback mempool/Start9 common). Port heuristics
                       bias the probe order (8332→Core first, 3006→Esplora first) but
                       never bypass the probe.
```

Timeouts: ~3 s per probe, 2 probe kinds max → <6 s worst case, snappy default
(`max_retries=0`, mirroring `check_backend`). Every failure collapses to a single
honest value-free message naming what didn't match and pointing at the doctor —
**no silent fallback to the public server** (ADR-0023 d4). Electrum/bitcoind probes
additionally enforce mainnet (`server.features.genesis_hash` / `getblockchaininfo.chain`).

**Credentials UX** (in `/setup`, web settings, and the first-run form):
after classification, if the kind can need auth (Electrum: rarely; Core: usually),
show **user / password fields + a "no credentials needed" checkbox**. Checkbox
semantics = **omit the Authorization header entirely** (no basic auth, no cookie).
`ssl://` + creds: accepted but noted — Electrum has no standard auth and most servers
ignore credentials (rarely used; pass them only if a server demands).

**Storage:** settings keys in the DB alongside `chain_base_url`, e.g.
`backend_auth_user`, `backend_auth_pass`, `backend_auth_none` (bool), resolved through
the same `ChainConfig.from_settings` single selection point (env > file > stored >
default). **Threat model:** the DB is a **local, single-user SQLite file** — the same
trust surface as `chain_base_url`/the wallet descriptor; a local Core/electrum password
is low-sensitivity (it gates a node the user already runs). Stored plaintext is
acceptable for v1; **never logged/echoed** (value-free), never sent on the public path.
OS-keyring is future work (OQ-4). `tls_verify` stays env/file-only (no stored rung) —
transport downgrade is an operator decision, unchanged (ADR-0018 amendment).

---

## 4. Sequencing + sizing

**Recommended order: M1 (Electrum) → M2 (bitcoind) → M3 (auto-detect + creds).**

- **M1 first:** Start9/Umbrel boxes (this app's targets) ship electrs; Electrum serves
  **full per-address history** natively, matching Esplora's `get_address_txs` exactly —
  least code, no history tradeoff, and it delivers the `ssl://` path. It also forces the
  `ChainClient` protocol split + fee/price gating that M2 reuses.
- **M2 second:** heavier (scantxoutset, auth surface, history limitation), so it builds
  on the protocol M1 established.
- **M3 last:** the detector + creds UX classify and drive both clients; it needs both
  to exist to be testable end-to-end. Its URL-classification skeleton can be scaffolded
  inside M1 (route `ssl://` → electrum) but lands fully after M2.

**Test strategy per milestone:** hermetic fixture servers (ssl stratum stub / loopback
JSON-RPC stub), no network in CI, same `transport=`-injection seam the scan tests use;
per-milestone lint (`tools/lint_network.py` — adapters live in `chain/`, already
exempt) + full suite + evals fixture mode green.

**Security-review gates:** broadcast txid-binding (re-validate/bind like Esplora);
no keys imported (scantxoutset descriptors only); value-free errors; mainnet-only
enforcement in every backend probe; no silent public fallback; creds never logged.

**Existing tests that pin behavior and must stay green:** `tests/test_chain_esplora.py`
(the EsploraClient interface), `tests/test_chain_fees.py`, `tests/test_chain_price.py`,
`tests/test_chain_balances.py`, `tests/test_chain_backend_switch.py`,
`tests/test_chain_tls.py`, the scan tests driving `fetch_scan` with a mock client, and
`tests/test_setup_command.py` (which pins that `ssl://` was previously refused —
update to reflect M1). The adapters must satisfy the **same injected-client seam**
scan/fees/price use so those tests swap in an electrum/bitcoind double unchanged.

**ADR-0018/0023 amendment on acceptance:** ADR-0023 decision 7 ("v1 scope is
Esplora-protocol only") is superseded for these three kinds; `ChainConfig` gains the
`ssl://` scheme + backend-kind field; setup copy stops saying "Esplora only".

---

## 5. Open questions for the orchestrator (each with a recommended default)

1. **bitcoind history limitation** — accept "unspent-only history, fully-spent
   addresses not surfaced" (watch-only, no wallet) and document honestly, or require an
   address-index/wallet (breaks watch-only)? **Default: accept unspent-only; rescan
   widens via descriptor range.**
2. **Price oracle on electrum/bitcoind** — no `/v1/prices`: refuse USD-denominated
   `create_tx` with `price_unavailable` (keep sats-only), or add a separate price
   source? **Default: refuse / disable price on non-Esplora backends (OQ-2).**
3. **Electrum `get_address_txs` cost** — N+1 `transaction.get(verbose)` per history
   tx is heavy on large histories; cap depth or accept (gap window bounds it to
   ≤1000/branch)? **Default: accept for v1, note a later batching optimization.**
4. **Credential storage** — plaintext DB settings (local single-user threat model) vs
   OS keyring? **Default: plaintext DB settings, value-free at rest, keyring future.**
5. **`ssl://` + credentials** — accept and pass user/pass over the ssl socket even
   though Electrum has no standard auth (most servers ignore it)? **Default: accept,
   documented as rarely-needed; checkbox "no credentials needed" omits auth.**

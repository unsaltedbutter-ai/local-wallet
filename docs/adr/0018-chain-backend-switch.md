# ADR-0018: Config-only chain backend switch (self-hosted Esplora)

- **Status:** Accepted
- **Date:** 2026-09-03
- **Decides:** How the wallet's chain backend is switched to the user's own
  instance as a **config-only** operation (Phase 4, TCK-P4-002). Relates to
  ADR-0003 (public MVP backend → Phase 4 self-hosted), ADR-0004 (testnet4),
  ADR-0016 (localhost node/ loopback contract). Scope:
  `src/localwallet/chain/`, `src/localwallet/config.py`, the one
  construction site in `src/localwallet/app.py`.

## Context

ADR-0003 chose a public mempool.space Esplora for Phases 0–3 behind the
`EsploraClient` interface, and Phase 4 (§12) replaces it with the user's own
instance *behind the same interface*. The privacy payoff requires that, when
self-hosted, **zero** chain requests reach the public default. The switch
must be a pure configuration change: no code path may bypass the selection,
and the existing public default (ADR-0003) and all existing env vars /
call sites must keep working.

## Decision

1. **A single selection point:** `Settings.chain_base_url`
   (`LOCALWALLET_CHAIN_BASE_URL`), empty by default, is the authoritative
   Esplora base for the WHOLE wallet when set. Resolution lives in exactly
   one function: `ChainConfig.from_settings`, which every `EsploraClient`
   (production or otherwise) flows through when its `base_url` is left at
   the default:

   ```
   chain_base_url set  -> use it (self-hosted or any custom URL)
   chain_base_url unset -> use esplora_base_url (legacy, ADR-0003 public default)
   ```

   Because every EsploraClient-mediated call — address txs/utxos, tip, fees,
   price, and later broadcast — rides the same client, flipping the backend
   is a one-variable config change.

2. **Backward compatibility preserved.** `esplora_base_url` /
   `LOCALWALLET_ESPLORA_BASE_URL` remain the fallback source when
   `chain_base_url` is unset, so existing deployments and the ADR-0003 public
   default are unchanged. `EsploraClient(base_url=...)` still accepts an
   explicit override for test seams and callers that want one.

3. **`LOCALWALLET_LOCAL_MEMPOOL_URL` is NOT the backend selector.** It stays
   the node doctor's loopback **probe target** (ADR-0016): its purpose is
   *detection* ("is a mempool running here?"), it has a non-empty loopback
   default (`http://127.0.0.1:3006`) that would silently flip the backend to
   a possibly-not-running localhost if it were overloaded as the chain source,
   and it is subject to the loopback-only contract. Detection ("detect") and
   backend selection ("use") are deliberately separate: a user points
   `LOCALWALLET_CHAIN_BASE_URL` at whatever serves them Esplora for testnet4
   (their self-hosted mempool, an electrs instance, or a remote node) — this
   is the Phase 4 reach that ADR-0016 explicitly deferred to this ticket.

4. **Testnet-only invariant preserved.** A self-hosted URL does not change
   network semantics. The instance must serve **testnet4** (ADR-0004); the
   client's path shapes (`/address/{addr}/txs`, `/utxo`, `/blocks/tip`,
   `/v1/fees/recommended`, `/v1/prices`, `/tx`) are identical regardless of
   host. No network-affecting behavior is introduced by this ADR.

5. **Fail closed on malformed config.** A selected `chain_base_url` that is
   not an http(s) URL raises a value-free `ValueError` in
   `ChainConfig.__post_init__` at client construction time — never a
   mid-request crash. Timeout/retry/backoff policy and value-free error
   discipline are untouched.

## Alternatives considered

- **Overload `LOCALWALLET_LOCAL_MEMPOOL_URL` as the backend source.**
  Rejected (see Decision 3): its non-empty loopback default would silently
  change the default backend and break the ADR-0003 public path, and it would
  conflate node *detection* with backend *selection* while dragging in the
  ADR-0016 loopback contract that this ticket is meant to extend past.
- **Auto-follow the node doctor's finding** (detected mempool ⇒ auto-switch).
  Rejected for now: it couples detection to selection implicitly, surprising
  users, and is out of this ticket's config-only scope. A later ticket
  (TCK-P4-003) may surface the doctor's guidance for the user to apply the
  `LOCALWALLET_CHAIN_BASE_URL` setting.

## Consequences

- `Settings.chain_base_url` / `LOCALWALLET_CHAIN_BASE_URL` is the single knob;
  `esplora_base_url` remains as the backward-compatible fallback.
- `app.py`'s `EsploraClient` construction no longer hardcodes the public
  default; it relies on the shared selection point.
- New hermetic tests (TCK-P4-002) prove: self-hosted selection routes
  address/txs/utxo/tip queries to the configured instance; zero requests hit
  the public host while self-hosted; the unset/set/malformed selection matrix
  behaves as documented.

## Amendment (2026-09-08, TCK-BACKEND-001): TLS trust for self-hosted https

Self-hosted stacks that ship **self-signed / private-CA TLS certificates**
(Start9's Embassy services are the motivating case, 2026-09 user report) made
this ADR's config-only switch fail: httpx verifies certificates by default,
and a failed handshake surfaces as `httpx.ConnectError` — the wallet's
honest but unusable `tip-height request failed after N retries: network
error (ConnectError)` — even though the backend is otherwise reachable. An
https backend was therefore only switchable when it carried a
publicly-trusted cert, which private-LAN services typically cannot get.

Decision: `Settings.tls_verify` (`LOCALWALLET_TLS_VERIFY` env /
`tls_verify` config-file key) extends this ADR's ladder philosophy to
transport trust — **env > config file > shipped default, default
`true` (fail-closed)**; malformed values refuse startup value-free like
every other boolean scalar. Deliberately **no stored (DB) rung**, unlike
`chain_base_url`: (a) downgrading transport authentication is a
host/operator decision that should take a deliberate config edit plus a
restart, never a UI-toggled setting; (b) the chain client resolves its own
`Settings.from_env()` and is store-free by architectural rule (config.py
never imports the store), so a stored rung could only desync the client's
transport from the app's banners; (c) every other boolean scalar here is
env/file-only too. The flag rides the SAME construction path as
`chain_base_url` (`ChainConfig.from_settings` → one `EsploraClient` → one
`httpx.Client(verify=...)`), so the onboarding backend probe
(`check_backend`) and every wallet call share one transport policy.

When the flag resolves `False`, the app prints one honest, unskippable,
value-free startup warning (`TLS_UNVERIFIED_WARNING`): whoever controls the
network path can observe the queried addresses and tamper with the
responses. **The recommendation remains a properly-trusted certificate** —
adding the private CA to the OS trust store keeps verification on and needs
no app change; `verify=false` is the escape hatch, not the happy path.

## Amendment (2026-09-09, TCK-ONB-004 M1): `ssl://` selects the Electrum adapter

Milestone 1 of docs/onb-004-backend-adapters-plan.md extends this ADR's
config-only switch from "Esplora base URL" to "chain backend URL": the
scheme of the selected `chain_base_url` picks the adapter through the SAME
single selection point.

Decision: `ssl://host[:port]` selects the Electrum-protocol client
(`chain/electrum.py::ElectrumClient`), http(s) keeps selecting
`EsploraClient`. Concretely:

- `ChainConfig` now validates BOTH shapes (an `ssl://` URL must have a host,
  no userinfo, no path, and a numeric in-range port when present) and
  exposes the selection as `ChainConfig.kind` (`"electrum"` / `"esplora"`);
  malformed values keep failing closed with the value-free `ValueError` at
  construction (decision 5 unchanged, now scheme-aware).
- The one construction site gains a scheme dispatch
  (`app._build_chain_client`): resolve `ChainConfig.from_settings(settings)`
  (env > config file > stored — decision 1's ladder unchanged), build the
  kind's client. Both adapters satisfy the same `ChainClient` protocol,
  connect lazily, and share the timeout/retry/backoff and error-scrubbing
  discipline, so every downstream surface (scan, watch, fees, broadcast,
  privacy banner) is adapter-agnostic by construction.
- `tls_verify` (previous amendment) rides the SAME path with identical
  semantics on both kinds — electrs/Fulcrum servers commonly carry
  self-signed certs exactly like Start9's https Esplora, so
  `LOCALWALLET_TLS_VERIFY=0` is the same env/file-only escape hatch and the
  same unskippable startup warning applies.
- Mainnet-only (ADR-0021) is enforced INSIDE the Electrum adapter's
  connection handshake (`server.features.genesis_hash` must equal the
  mainnet constant): M1 deliberately ships no `ssl://` setup-time probe —
  the `/setup` and first-run conversations still refuse `ssl://` entry
  (their URL entry, validation and stored-rung support land with the plan's
  M3), so until then the env/config-file ladder is the only `ssl://` path
  and the handshake is the only gate. `check_backend` remains
  Esplora-shaped and refuses `ssl://` (no silent cross-family fallback).
- Capability honesty (plan §0): the Electrum backend has native fees
  (`blockchain.estimatefee` → `estimate_fee`, the FeeEstimator's
  backend-native single-source path — the floor-follower is skipped, never
  faked) and NO price feed (`supports_price=False` → the price oracle
  refuses fail-closed to the ADR-0011 ladder's sats-only rung;
  USD-denominated `create_tx` answers `price_unavailable`).

Credentials note: the Electrum protocol has no standard auth; ssl://
endpoints essentially never take user/password. M1 therefore accepts no
credential fields for `ssl://` at all; when M3 adds the credentials UX
(user/pass + "no credentials needed") the accepted-but-rare case is
handled there, per the plan. The stored (DB) rung still validates http(s)
only in M1 — storing an `ssl://` choice is part of M3's settings work.

## Amendment (2026-09-09, TCK-BACKEND-002): hot-swap supersedes restart for `chain_base_url`; `ssl://` accepted at every entry point

User direction 2026-09-09 (items 5/6/8/9/10): changing the chain base URL
must NOT require a restart — "deallocate whatever method we had before and
do a new sync with the new URL". This amendment supersedes **decision 1's
restart semantics for the STORED rung only**: the resolution ladder (env >
config file > stored) and the single selection point are unchanged; what
changes is WHEN a stored change reaches the live client.

1. **Engine-thread hot-swap.** A `chain_base_url` write that APPLIES (web
   `POST /settings` or the CLI `/setup` conversation) swaps the live client
   in-session, on the engine (pump) thread — the thread that already owns
   the client's whole lifecycle. Sequence: **probe → build → store-write →
   install → full resync**. Install rebinds every chain-riding surface: the
   `ChainWorker`'s client (the worker object, its queue and thread survive;
   it reads the client once per job, so swaps happen only BETWEEN jobs),
   the fee/price wrappers and the three client-closing dispatch handlers
   (`create_tx`, `broadcast_tx`, `tx_status`), rebuilt in-place into the
   SAME table dict the loop holds, and the retired old client is closed
   BOUNDED (both adapters' `close()` are synchronous and local). An
   env/config-file rung still SHADOWS the stored write: a set higher rung is
   an operator decision made outside the running app, and the swap follows
   the same precedence the construction site does — the write stores (and
   is still probed) but the live client is untouched, `requires_restart`
   stays honestly `True`, and the next launch applies it.

2. **Probe before the save.** A candidate URL (http(s) OR `ssl://`) is
   probed BEFORE the write lands: Esplora via `chain.check_backend`
   (shape + height-0 mainnet genesis, ADR-0021), Electrum via ONE bounded
   `ssl://` tip call that forces M1's handshake — its
   `server.features.genesis_hash` gate IS the mainnet proof (this closes
   the "M1 exposes no setup-time probe" gap the previous amendment left
   for M3). Unreachable / foreign-chain → refused value-free
   (`BACKEND_PROBE_FAIL`), nothing stored, the old client still serving —
   fail-closed, the process is never left clientless. A client-build
   failure likewise REFUSES the write.

3. **One concurrency rule, pinned: a swap never crosses an in-flight
   scan.** While a scan fetch owns the worker (`pending`/`running`), the
   validated + stored swap DEFERS; the pump installs it the moment the
   scan's result has been persisted (and its own resync then occupies the
   worker). Deferral, not stand-down: blocking the engine on a job drain
   would stall the pump for the whole scan, and killing the old client
   mid-fetch would break the in-flight fetch's transport. The held
   first-run state (`awaiting_backend`) does NOT count as in-flight — the
   worker is idle there — so a `/setup` own-server choice now releases the
   held scan ON THE CHOSEN SERVER in-session (the ONB-006 promise "never
   fetch through a server the user refused" stands, and no longer costs a
   restart). A swap still deferred when the session ends is simply
   dropped: the stored value applies at next launch, the pre-amendment
   behavior.

4. **Resync semantics (directions 6/8).** Applying a new URL always fires
   the full rebuild scan (the TCK-SCAN-003 `--rescan` path: re-derive the
   whole gap window from chain truth, "as though the zpub had been entered
   for the first time"), triggerable on its own via the typed
   `resync_now` pump command (`POST /resync`, the settings button) and by
   a `gap_limit` write whose value ACTUALLY changed (unchanged apply =
   no scan, stated in the response). Coin tags survive every resync by
   construction: `coin_labels` is a separate table, never in the scan's
   write-set (store contract, test-pinned).

5. **`ssl://` is accepted at every entry point** (completing M3's entry
   work for scheme selection + validation; credentials UX, if ever needed,
   stays open): the store's typed writer now carries
   `ssl://host[:port]` (mirroring `ChainConfig`'s shape rules, value-free),
   the `/setup` conversation treats `ssl://` as a first-class candidate
   (probed, stored, swapped), and web settings same. Genuinely foreign
   schemes (`ftp://` …) keep the plain never-probed refusal.

6. **`backend_kind` (direction 10).** The settings response and the
   `/state` snapshot carry an additive, value-free enum NAME for the
   client's badges, closed set `{public, mempool, esplora, electrum,
   bitcoind, none}`: `none` while no backend resolves (first-run unchosen);
   `electrum` for `ssl://`; `public` when the live http(s) host is the
   shipped public mempool.space default; `mempool` for a self-host
   serving the API under the `/api` path convention (the mempool.space
   app); `esplora` for an Esplora-shaped API at the root. `bitcoind` is
   RESERVED for the M2 Core-RPC adapter (a value of the enum, never
   emitted today — the badge cannot lie about a backend that cannot
   exist yet). The mempool/esplora split is a documented URL-SHAPE
   heuristic: the two serve indistinguishable APIs, so the badge reports
   what the URL looks like, derived from the same single selection point
   the client construction uses — never a network probe.

`requires_restart` on the `chain_base_url` settings entry flips to
`False` whenever a swap controller is wired and the write is not shadowed
— the honest flag, per section 1. Decision 5's fail-closed construction
discipline is unchanged; it just no longer waits for a launch.

## Amendment (2026-09-10, TCK-ONB-004 M2): `bitcoind://` selects the Bitcoin Core RPC adapter

Milestone 2 of docs/onb-004-backend-adapters-plan.md adds the third
`ChainClient` implementation and completes the scheme-dispatch switch begun
in M1: the scheme of the selected `chain_base_url` picks the adapter through
the SAME single selection point (`ChainConfig.from_settings` →
`app._build_chain_client`).

Decision: `bitcoind://[user:pass@]host[:port]` selects the Bitcoin Core
JSON-RPC client (`chain/bitcoind.py::BitcoindClient`); http(s) keeps
`EsploraClient`, `ssl://` keeps `ElectrumClient`. Concretely:

- **Scheme chosen over autodetect.** The ticket scoped M2 to an explicit
  scheme so Core is selectable without M3's probe-based classification;
  `bitcoind://` is added to `ChainConfig`'s shape rules (host required,
  optional numeric in-range port, no path/query/fragment) and surfaces as
  `ChainConfig.kind == "bitcoind"`. Malformed values still fail closed with
  the value-free `ValueError` at construction (decision 5 unchanged). The
  http(s) probe branch that might ALSO discover Core on a bare port stays
  M3's job; until then Core is reachable ONLY via this scheme on the
  env/config-file ladder — the stored DB rung still refuses it (its
  `chain_base_url` write validation carries http(s)/ssl:// shapes only;
  extending it to `bitcoind://` is M3's entry work, like M1's ssl://
  split).

- **Plain-http transport, stdlib.** Core RPC is a loopback administrative
  interface: the adapter speaks JSON-RPC 1.0 over stdlib `http.client` on
  plain http (the plan's httpx suggestion traded against the ticket's "use
  stdlib within chain/" rule; `http.client` is that rule's stdlib form and
  avoids a second transport). **https RPC is unexpressible on this scheme
  and OUT of M2 scope** — Core needs `-rpcssl` or a TLS reverse proxy for
  it, the target deployment is same-machine, and `tls_verify` is therefore
  NOT consulted by this adapter (no TLS layer to trust or downgrade: an
  honest named-unsupported state, never a silent downgrade). The default
  port is mainnet RPC's 8332 (ADR-0021 has no other network).

- **Auth, resolved per request, in the plan's order** (value-free, secrets
  never logged/echoed): (1) user/pass from the URL userinfo (env/config-file
  rung) or the `rpc_user`/`rpc_password` constructor pair; (2) the cookie
  file at `Settings.rpc_cookie_path` (`LOCALWALLET_RPC_COOKIE_PATH` /
  config-file key; empty → `~/.bitcoin/.cookie`, the same ladder the node
  doctor documents), re-read every request (bounded size) so a Core restart
  that rotates the cookie self-heals; (3) no credentials — the
  `Authorization` header is OMITTED entirely for an open local RPC. The
  cookie CONTENT and any password exist only inside the base64 header value;
  a node that demands auth answers its absence with a 401, refused
  value-free and never retried. The URL `user@host` half-pair is rejected at
  construction (a typo, never a silent fallback).

- **Mainnet gate lives IN the adapter handshake** (as in M1's
  `server.features`): the first call runs `getblockchaininfo` whose `chain`
  must be `"main"` (ADR-0021 — testnet/signet/regtest refused value-free,
  deterministically, not retried), then `getnetworkinfo` whose `version`
  must clear 220000 (capability floor: verbose `getrawtransaction` carries
  the input `prevout` scriptPubKeys only from Core 22 on; an older node
  would silently mis-attribute a spend as incoming, so it is refused rather
  than trusted). A node still syncing (`blocks < headers`) is NOT an error —
  the tip is honest chain-truth-so-far, and progress narration stays the
  app's watch surface (ADR-0023 decision 5).

- **Scan via `scantxoutset` (the plan's option (a), watch-only exact).**
  `get_address_utxos`/`get_address_txs` are answered from a UTXO-set snapshot
  walk (`scantxoutset("start", ["desc(raw(<script hex>))", ...])`) whose
  descriptors are built ONLY from the wallet's own output scripts — no key
  material is ever sent, `importprivkey`/`importdescriptors` are never
  called, and the node's wallet is untouched (the watch-only invariant). The
  whole-set walk is expensive, so its result is cached per client at the tip
  height, re-walking only when the tip moves or a new own-script is queried
  (a same-height reorg between probes is stale until the next block — the
  accepted M2 ceiling). BTC amounts are parsed through `Decimal` and must be
  a whole number of satoshis or the payload fails closed.

- **The unspent-only-history tradeoff (plan §2, OQ-1 default, ACCEPTED
  here).** Core without a wallet or an address index cannot enumerate a
  per-address SPENT history, so `scantxoutset` surfaces only UNSPENT outputs:
  fully-spent addresses yield NO history, and mempool outputs never enter the
  UTXO set so unconfirmed coins are honest ABSENCE. `get_address_txs`
  therefore assembles history only from the funding transactions of an
  address's unspent outputs (verbose `getrawtransaction` per distinct txid);
  the gap walk treats an invisible-history address as unused, exactly as a
  genuinely fresh one. Nothing is fabricated; a rescan widens the descriptor
  set. A user who needs full spent history points the backend at
  Esplora/electrum instead — the badge (`backend_kind == "bitcoind"`, now
  EMITTED for `bitcoind://`, superseding the previous amendment's
  "RESERVED, never emitted") and this ADR say so honestly.

- **Capability honesty (plan §0/OQ-2), same as M1.** Native fees through
  `estimatesmartfee` (targets FAST=1/MEDIUM=2/SLOW=6, `CONSERVATIVE` mode;
  a warmup answer with no `feerate` fails closed, never a fabricated bid —
  the FeeEstimator's backend-native single-source path, floor-follower
  skipped). `supports_price = False`: Core has no price feed, so the price
  oracle refuses fail-closed to the sats-only rung and USD-denominated
  `create_tx` answers `price_unavailable`. No `get_json` member (raw Esplora
  JSON stays Esplora-only).

- **Probe (deliverable 7) — small addition, NOT deferred.** The M1
  amendment deferred the `ssl://` setup probe to M3 because M1 shipped no
  entry UX; M2 reuses M1's `_probe_chain_backend` structure verbatim: a
  `bitcoind://` candidate forces the SAME adapter handshake (the `chain ==
  "main"` gate and the auth header ride the one `getblockchaininfo` tip
  call), and every failure (unreachable, wrong chain, 401, too-old node)
  collapses to `False` → the value-free `BACKEND_PROBE_FAIL` line, nothing
  stored, the old client untouched. Core is therefore selectable AND
  validated on the stored rung the moment its shape is accepted there.

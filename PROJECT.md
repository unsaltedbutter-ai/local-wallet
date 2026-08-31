# local-wallet — Project Definition & Handoff Spec

- **Status:** Draft v0.1 — for team review
- **Date:** 2026-08-30
- **Working name:** `local-wallet`
- **Audience:** Engineers and designers picking up execution from zero

---

## 1. Summary

A chat-driven, privacy-first Bitcoin wallet for single-sig users. A small LLM
(Google Gemma 4 E2B, running fully locally) translates the user's natural
language into **intents**. A deterministic Python core executes every intent:
deriving addresses from an xpub, scanning the chain, building PSBTs, handing
them to a hardware wallet (connected via USB, or airgapped via microSD file
transfer), broadcasting, and tracking confirmations.

**The LLM never touches money logic, network calls, or secrets.** It parses
intent and narrates results. Every action it triggers is validated by
code the user can audit, and every destructive action requires explicit
confirmation — in chat and on the hardware device.

The privacy promise is "nothing leaves your computer." In the MVP, address
queries to a public block explorer do leave (see §9); the roadmap removes
that by guiding the user to run their own node.

---

## 2. Goals & Non-Goals

### Goals

- G1. Chat-first UX for balance, receive, send, tx status, fee decisions.
- G2. 100% local LLM inference (Gemma 4 E2B GGUF) — no cloud AI.
- G3. Watch-only application: keys live only on a hardware wallet. The app
  handles xpubs, never xprvs or seed phrases.
- G4. Two signer paths from day one: microSD/USB file transfer (airgap) and
  USB-connected devices (Ledger, Trezor, Coldcard, Jade, BitBox02 via HWI).
- G5. Honest, auditable privacy: one module performs all network I/O; the
  xpub itself never leaves the machine; a clear UI indicator shows whether
  data source is local node or public API.
- G6. Runs on a base Mac mini and mid-range Windows machines.
- G7. The LLM proactively surfaces decisions humans actually face: fee rate
  vs. urgency ("Are you in a hurry?"), USD↔sats conversion, time since last
  block, mempool congestion.

### Non-Goals (v1)

- N1. Not custodial. No seed phrase handling, generation, or storage in-app.
- N2. No multisig (v2 candidate).
- N3. No Lightning.
- N4. No RBF/CPFP fee bumping in v1 (stuck-tx UX must set expectations;
  see R7).
- N5. No mobile.
- N6. No coinjoin / advanced privacy tech.
- N7. Not a general crypto wallet. Bitcoin only.

---

## 3. Target Users

1. **Primary:** Bitcoin-curious single-sig users who own (or will buy) a
   hardware wallet and are uncomfortable with both mobile custodial wallets
   and power tools like Sparrow. They want to ask questions in plain language.
2. **Secondary:** Privacy-conscious users who already run (or want to run) a
   full node and want a zero-cloud wallet experience.

The chat UX must serve user 1; the node path must satisfy user 2.

---

## 4. North-Star Success Criteria

- A non-technical user goes from "here is my zpub" to "I sent a testnet
  payment and saw it confirmed" in under 10 minutes, guided entirely by chat.
- Zero secrets ever exist in the app's process, disk, or logs.
- Given a testnet wallet with known history, the app's balance/UTXO/history
  match Electrum and mempool.space exactly.
- On an LLM eval suite (see Phase 6), intent extraction is ≥95% correct on
  the golden set and 100% of destructive flows pass the confirm-gate tests.

---

## 5. Design Principles

1. **LLM is an intent parser and narrator — never a calculator.** Amounts,
   addresses, fees, and balances are computed in Python. The model quotes
   tool output verbatim; it never generates or "corrects" an address.
2. **Model output is untrusted input.** Same trust level as a string from
   the internet. It is parsed, schema-validated, business-rule-checked, and
   dispatched by an allowlist. Worst case from a crazy output is a rejected
   dispatch — never a network call, never a file write, never a broadcast.
3. **Closed intent protocol.** The model can only emit intents from a fixed
   registry. Unknown intents are rejected and re-prompted, never executed.
4. **Constrained decoding.** The model emits JSON under a grammar
   (llama.cpp GBNF / JSON-schema grammar), so malformed output is
   syntactically impossible before validation even starts.
5. **Fail closed.** Any validation failure, mismatch, or ambiguity stops the
   flow and asks the user. Nothing destructive proceeds on a maybe.
6. **One network module.** All I/O lives in `chain/`. Lint-enforced. A
   reviewer can grep exactly what crosses the wire.
7. **Device confirmation is the trust anchor.** The hardware wallet screen
   is the source of truth for signing; chat summaries are aids, not proofs.
8. **Testnet-first.** Mainnet is gated behind Phase 6.
9. **Small-blast-radius iteration.** Every protocol/prompt change ships with
   an eval run (Phase 6 makes this a merge gate).

---

## 6. System Architecture

```
┌──────────────────────────── user's computer ────────────────────────────┐
│                                                                         │
│   Chat UI (CLI → TUI/GUI)                                               │
│      │ user text                    ▲ narrated results                  │
│      ▼                              │                                   │
│   Agent runtime — local LLM (Gemma 4 E2B GGUF, grammar-constrained)     │
│      │ intent envelope (JSON)                                           │
│      ▼                                                                  │
│   Parser / Validator                                                    │
│      │  grammar (decode-time) → pydantic schema → business rules        │
│      ▼                                                                  │
│   Dispatcher — closed intent registry → handlers                        │
│      │                                                                  │
│      ├── Wallet engine   (descriptor, derivation, scan, cache)          │
│      ├── Tx engine       (coin selection, PSBT, fees, re-validation)    │
│      ├── Signer gateway  (file / HWI-USB / QR later)                    │
│      ├── Node doctor     (detect, guide, health)                        │
│      └── Store           (SQLite)                                       │
│               │                                                         │
│               ▼                                                         │
│   Chain adapter ◄── THE ONLY module with network access                 │
│      (public Esplora MVP → self-hosted node later; price oracle)        │
└─────────────────────────────────────────────────────────────────────────┘
        │ Esplora HTTPS: address history/UTXO, fees, broadcast
        ▼                                     ▼
   mempool.space (MVP) / user's node      hardware wallet
   (Phase 4: everything local)            (microSD file or USB)
```

### Data flow for a send

1. "Send $100 to bc1q..." → LLM emits `{intent: "create_tx", params:{...}}`.
2. Validator checks recipient checksum, amount bounds, etc.
3. Tx engine fetches fee estimates + UTXOs, runs coin selection, builds an
   unsigned PSBT, computes fee in sats and USD.
4. LLM narrates a confirmation card (amount, address, fee, size, ETA) and
   asks the user to confirm — plus the urgency question if fee choice
   matters.
5. On explicit confirm, dispatcher invokes the Signer gateway (file export
   or HWI USB).
6. Signed PSBT is **re-parsed and deterministically re-validated** against
   the intended transaction (outputs, fee) before broadcast. Mismatch = hard
   stop.
7. Broadcast via chain adapter; tx tracked until confirmed.

---

## 7. Components

### 7.1 Agent runtime (`agent/`)
- Runs Gemma 4 E2B (`google/gemma-4-E2B-it`) as GGUF via llama.cpp
  (`llama-server` or `llama-cpp-python`). E4B is the drop-in fallback (see R1).
- Grammar-constrained decoding: the envelope schema compiles to a GBNF
  grammar; the model cannot emit malformed envelopes.
- Owns conversation state, context injection (tool results are injected as
  structured blocks for the model to narrate), retry loop (one retry on
  validation failure, hard cap ~6 turns per user request), context-window
  management (summarize old turns; per Gemma 4 guidance, thinking blocks are
  stripped from history except tool-call turns).
- Thinking mode: off by default (latency); enable for ambiguous intents.
- Sampling: start from the model card defaults (temp 1.0, top_p 0.95,
  top_k 64) and tune against evals — low-temperature JSON emission still
  holds because the grammar constrains syntax.

### 7.2 Protocol (`protocol/`)
- Envelope schema (pydantic), intent registry, dispatch table.
- The **closed intent protocol** — concept in §8; full spec is a Phase 0
  deliverable (§12).
- Known-shape intents today (illustrative, not final):
  `respond`, `clarify`, `get_balance`, `get_history`, `new_address`,
  `create_tx`, `confirm_tx`, `sign_tx`, `broadcast_tx`, `tx_status`,
  `watch_incoming`, `node_status`.

### 7.3 Wallet engine (`wallet/`)
- Parse xpub/zpub/ypub/tpub/vpub; build an output descriptor
  (`wpkh([fp/84'/0'/0']zpub/{0,1}/*)`); detect script type from key prefix
  and standard paths (BIP84 native segwit is the v1 default; P2SH-P2WPKH
  and legacy read support; taproot open question — OQ6).
- Address derivation (receive + change branches), batched.
- Chain scanning with BIP44-style gap limit (20, configurable), tracking
  max used index per branch; manual rescan command (see R3).
- SQLite cache: addresses, derivation index, UTXOs, history, sync cursor.

### 7.4 Chain adapter (`chain/`) — the only networked module
- Esplora client (mempool.space public for MVP; same API self-hosted in
  Phase 4): address txs/UTXOs, block tip, fee recommendations, broadcast,
  tx status.
- Fee estimator wrapper (sats/vB by confirmation-target blocks).
- Price oracle: cached FX rate with TTL + timestamp shown to user. Note:
  this is an external call — make source/TTL configurable (OQ4).
- Later: Bitcoin Core RPC and/or electrs/Fulcrum backends behind the same
  interface.

### 7.5 Tx engine (`tx/`)
- Coin selection (start conservative: branch-and-bound if available in
  embit, else smallest-larger-first; never gratuitous UTXO shattering).
- Change output via fresh change index; dust checks (~546 sats legacy /
  ~294 sats P2WPKH — compute from script size, don't hardcode guesses);
  min-relay-fee floor; RBF flag policy documented (N4).
- Signed-PSBT re-validation: re-parse the signed PSBT, assert outputs match
  the intended tx and fee is sane, before any broadcast. Unit-tested with a
  tampered-PSBT fixture (Phase 3 AC).

### 7.6 Signer gateway (`signer/`)
One interface, three implementations:
- `FilePsbtSigner` — write unsigned PSBT to a chosen folder / SD mount;
  import the signed file back. First-class airgap path (Coldcard/Passport/
  SeedSigner-style). File naming + checksum conventions = open question
  (OQ18).
- `HwiUsbSigner` — Bitcoin Core's HWI as a Python library (used as a lib,
  not just CLI, as Specter Desktop does). Ledger, Trezor, Coldcard (USB),
  Jade, BitBox02, KeepKey. Handles enumerate → fingerprint match against
  descriptor → display address → sign.
- `QrSigner` (v2) — camera/screen QR for SeedSigner/Keystone-class devices;
  note E2B's vision input could assist here later.

### 7.7 Node doctor (`node/`)
- Detect local Bitcoin Core / mempool instances (ports, RPC cookie, sync
  state); health + IBD progress checks.
- Guided setup content the agent can walk a user through (recommend
  Umbrel/Start9 for easy mode; Core + prune for minimal; self-hosted
  mempool for explorer + Esplora API). The agent **advises only** — it
  never runs privileged commands itself.

### 7.8 Store (`store/`)
- SQLite (WAL). Everything local: wallet metadata, derivation state, tx
  cache, session transcripts (with scrub/export), settings.
- Logging policy: never log xpubs, addresses, or amounts in error reports;
  there are no error reports to remote by default (OQ13).

### 7.9 UI (`ui/`)
- v0: CLI chat (fastest path to a walking skeleton).
- v1: TUI or desktop shell — **design decision with designers** (OQ8).

### 7.10 Evals (`evals/`)
- Golden prompt set → expected intent envelopes (per-intent fixtures).
- Red-team set: prompt injection via tx metadata/labels, xpub exfiltration
  attempts, destructive-action bypasses, crazy-input fuzzing.
- Runs in CI; protocol/prompt changes must not regress (Phase 6 gate).

---

## 8. The Intent Protocol — Concept & Invariants

*(Full spec deliberately deferred to Phase 0; this section fixes the
invariants the spec must preserve.)*

The LLM's only output format is a **small, closed JSON envelope**:

```json
{"intent": "create_tx",
 "params": {"amount_usd": 100, "recipient": "from_user"}}
```

Invariants:

1. **Closed world.** Intents are an enum; validators reject anything else.
   The dispatcher is a lookup table from intent → handler; there is no
   `eval`, no codegen from model output.
2. **The model has no privileges.** No network, no filesystem, no crypto.
   It cannot "call tools"; it emits text that Python may choose to act on.
3. **Three validation layers.** (a) decode-time grammar guarantees
   well-formed JSON; (b) pydantic schema validates types/enums/ranges;
   (c) business rules validate meaning (address checksum, amount ≥ dust,
   fee sanity, fingerprint match). Failure at any layer → rejected dispatch
   → one re-prompt → then clarify/escalate to the user.
4. **Results flow back as injected context,** not through model invention.
   The context injector inserts fresh facts (balance, addresses, fee table,
   block height) as structured blocks each turn; the model narrates from
   those blocks and must quote addresses verbatim.
5. **Two non-action intents are first-class.** `respond` (pure chat answers)
   and `clarify` (ask the user a question, e.g. urgency, missing address).
   Most turns are not actions; the dispatcher must not force tool calls.
6. **Stateful flows are state machines, not improvisation.** Send is
   `create_tx → confirm_tx → sign_tx → broadcast_tx`; the dispatcher owns
   the state, the model cannot skip or reorder steps. Confirmation requires
   an explicit user utterance parsed by the confirm gate — an LLM "yes" on
   the user's behalf is invalid.
7. **Envelope format details** (streaming, batching, error envelopes,
   versioning) are open — to be settled in the Phase 0 spec with eval data.

Why this shape: it makes the privacy/security surface auditable. Every
network call lives in `chain/`; every dangerous transition is a named state;
the model's freedom is bounded to choosing intents and extracting params.

---

## 9. Privacy & Security Model

### What leaves the machine

| Data | MVP (public Esplora) | With own node (Phase 4) |
|---|---|---|
| xpub / descriptor | never | never |
| Addresses (queries for history/UTXO) | yes — server sees addresses + IP | no |
| Fee/price queries | yes (coincidental: "IP asked about BTC price/fees") | no (price still external unless user opts out) |
| Chat content / LLM inference | never (local model) | never |
| Transactions | broadcast like any wallet; public by nature | same |

MVP MUST display an honest indicator: "Querying public mempool.space — the
operator can associate these addresses with your IP." This is the asterisk
on the privacy promise; the roadmap closes it in Phase 4.

### Threat model (v1)

- **Confused/malicious LLM output** → bounded by closed protocol + 3-layer
  validation + confirm gates. Model cannot initiate I/O.
- **Prompt injection via chain data** (tx labels, memos, node error strings)
  → sanitize/structure all tool outputs before context injection; red-team
  evals (§7.10).
- **Tampered signed PSBT** → deterministic re-parse + match before broadcast.
- **MITM to public API** → TLS; Esplora responses are not SPV-provable —
  documented limitation, mitigated by Phase 4 self-hosted backend. Do not
  over-claim verification in UI copy.
- **Address spoofing** → full addresses shown verbatim from tool output;
  verify-on-device flow; chat shows what the device will show; any mismatch
  is a hard stop.
- **Secrets** → none in-app (watch-only). Keys exist only on the hardware
  wallet. Seed phrases are refused in chat with guidance.

---

## 10. UX Direction (for designers)

- **Tone:** patient, precise, zero jargon without explanation. A knowledgeable
  friend, not a finance bro.
- **Dual units always:** BTC/sats and USD, with rate timestamp.
- **Fee conversations:** present speed options with real numbers —
  "next block ≈ X sats/vB ≈ $Y; cheaper ≈ Z sats/vB, maybe ~1–2 h."
  The assistant asks the urgency question when it matters, not every time.
- **Confirmation cards:** amount, recipient (full address, copyable — never
  truncated mid-hash), fee, size, ETA, source of funds summary. One obvious
  confirm affordance; cancel is always available.
- **Device handoff moments** need their own designed states: "Plug in and
  unlock your Ledger…" / "Save this file to your SD card…" / "Now compare the
  address on your device screen." These are the trust moments — invest here.
- **Status ribbons:** connection mode (public API vs own node), block height,
  time since last block, sync state.
- **Error states** in human language with next-step suggestions ("Your Trezor
  is locked — enter your PIN on the device, then say 'retry'").
- **Incoming payment monitoring** should feel passive-safe: the user can ask
  "anything coming in?" and get a plain answer with confirmation progress.

---

## 11. Technology Choices (and Alternatives Considered)

| Concern | Choice | Why | Alternatives considered |
|---|---|---|---|
| Language | Python 3.12+ | HWI is Python-native; embit; fastest iteration; team preference | Rust+BDK (industrial, `bdk`/`bdk-hwi` — revisit if perf demands), TypeScript+bitcoinjs |
| Bitcoin primitives | `embit` | descriptors, PSBT, derivation; proven in Specter Desktop | bdk-python, bitcoinlib |
| Hardware wallets | `hwi` (as a library) | Bitcoin Core project; uniform Ledger/Trezor/Coldcard/Jade/BitBox02 | vendor SDKs (per-vendor cost) |
| LLM | Gemma 4 E2B-it GGUF (llama.cpp) | 2.3B effective params, 128K ctx, native function calling + system role, Apache-2.0; runs on target hardware | E4B fallback (better tool-use: Tau2 42.2 vs 24.5); Ollama/LM Studio runtimes |
| Decoding constraint | llama.cpp GBNF / JSON-schema grammar | syntactically valid envelopes guaranteed | grammarless + repair/retry only (weaker) |
| Validation | pydantic v2 | schema layer, fast, typed | jsonschema |
| Storage | SQLite (WAL) | local, zero-ops | — |
| UI | CLI → TUI/GUI (OQ8) | walking skeleton first | Electron/Tauri/native |
| Chain data | Esplora API shape | one API serves public + self-hosted mempool | Core RPC, electrs, Neutrino (BIP157) — Phase 4+ |

---

## 12. Roadmap

Each phase has acceptance criteria (AC). Phases 0–5 are **testnet-only**.

### Phase 0 — Walking skeleton
Chat loop → grammar-constrained envelope → dispatcher → one real intent
(`get_balance`) against a hardcoded **testnet** zpub via public Esplora.
Evals skeleton with ~10 golden prompts.
**AC:** "What's my balance?" returns a correct live testnet balance; a
malformed/nonsense model output is rejected cleanly (fuzz test); model runs
fully locally.

### Phase 1 — Wallet engine
Descriptor parsing + prefix/path detection, derivation, gap-limited scan,
SQLite cache, rescan. Intents: `get_balance`, `get_history`, `get_utxos`,
`new_address`, plus `respond`/`clarify`.
**AC:** balance/UTXO/history match Electrum + mempool.space on a testnet
wallet with known history including >20-address gaps; rescan fixes a
simulated stale cache; unit tests for prefix→script-type mapping.

### Phase 2 — Send flow (unsigned)
Fee estimates, USD conversion, coin selection, PSBT build, change, dust/
min-relay checks, confirmation card UX.
**AC:** produced unsigned PSBT loads correctly in an external tool (Sparrow)
with matching outputs/fee; confirmation gate cannot be skipped or spoofed in
tests; `clarify` fires when recipient/amount ambiguous.

### Phase 3 — Hardware signing + broadcast
`FilePsbtSigner` (SD/USB file round-trip) and `HwiUsbSigner`; signed-PSBT
re-validation; broadcast; tx status tracking.
**AC:** end-to-end testnet send with at least one real device (e.g. Coldcard
file flow + one USB device); tampered-PSBT fixture is caught deterministically;
broadcast verified on-chain; device-absent/locked error flows behave.

### Phase 4 — Own node
Detect Core/mempool instances; switch chain backend to self-hosted;
`node_status` intent; node-doctor guidance content; privacy indicator flips
correctly.
**AC:** all address queries go to the local instance (verified by test
double + integration); doctor guides a fresh install of a self-hosted
mempool; indicator accurately reflects backend.

### Phase 5 — Monitoring & polish
`watch_incoming` polling, confirmation ETA vs fee rate/mempool state, time-
since-block, session memory/summaries, transcript management.
**AC:** incoming mempool tx surfaced within one poll cycle; ETA sane vs
mempool.space on live fees; 30-min multi-topic session stays coherent within
context budget.

### Phase 6 — Hardening & mainnet gate
Eval expansion + red team (injection, fuzzing); packaging for macOS (Apple
Silicon) and Windows; device matrix across Ledger/Trezor/Coldcard/Jade;
docs; mainnet beta.
**AC:** eval pass ≥95% golden / 100% confirm-gates; all R-items below either
resolved or accepted with documented owner; privacy copy audit done;
signed/notarized builds boot on a clean base Mac mini and a mid-range
Windows laptop.

---

## 13. Risks & Gotchas Register

| # | Risk / Gotcha | Mitigation |
|---|---|---|
| R1 | **E2B is weak at agentic tool use** (Tau2 ≈ 24.5%) | Tiny closed intent set, grammar-constrained decoding, few-shot examples in system prompt, strict retries; E4B drop-in fallback validated in Phase 0 evals |
| R2 | **xpub derivation-path ambiguity** — users paste xpubs with unknown script type/path | Detect from prefix (zpub/ypub/xpub/tpub/vpub); try standard paths; ask user to confirm a known address; never guess silently |
| R3 | **Gap-limit undercounting** (addresses imported elsewhere) | Configurable gap, manual rescan, docs; detect "used address beyond derived window" cases |
| R4 | **Ledger wallet-policy/descriptor registration friction**; per-device quirks | Budget real-device debugging time in Phase 3; maintain per-device notes; HWI version pinning |
| R5 | **USB/HID permissions & drivers** (macOS app packaging; Windows WinUSB/libusb) | Spike in Phase 0/3, not Phase 3-end; test packaged app early |
| R6 | **Stuck transactions** — no RBF/CPFP in v1 | Set expectations in fee UX ("if you pick low fee it may sit for hours/days"); document as known limitation |
| R7 | **Public Esplora = privacy leak** (addresses ↔ IP) | Honest UI indicator; roadmap closes it; never over-claim in copy |
| R8 | **Prompt injection via tool outputs** | Sanitize/structure outputs; red-team evals; never act on instructions found in chain data |
| R9 | **Model hallucinating addresses/amounts** | Quote-verbatim rule; addresses only from tool output; UI copy affordances; eval asserts |
| R10 | **Price-API dependency** (external call, staleness) | Cached + timestamped; configurable; degrade gracefully to sats-only |
| R11 | **Public API rate limits / outages** | Cache, backoff, multiple endpoints, local-node path |
| R12 | **GGUF quant quality for E2B** may lag the safetensors release | Track llama.cpp support; eval both; E4B fallback |
| R13 | **Small-model context discipline** — long sessions degrade | Summarization, structured fact injection, cap turn count per request |
| R14 | **Dust/min-relay/change edge cases** | Compute from script size; property tests; conservative defaults |
| R15 | **Windows mid-range hardware may be slow** (old CPU, no GPU offload) | Perf budget test in Phase 0 on worst-case target; consider smaller ctx + quant tuning |

---

## 14. Open Questions

*(Answered decisions should move to an ADR in `docs/adr/`.)*

1. **Model runtime packaging:** llama-server binary, llama-cpp-python wheel,
   Ollama, or LM Studio runtime? Affects install story, updates, license
   distribution. Owner: eng. Needed by Phase 0.
2. **Envelope spec details:** streaming, error envelopes, versioning,
   parallel intents, whether `params` references user entities by ID vs
   inline. Needed by end of Phase 0.
3. **Confirm gate mechanics:** dedicated `confirm_tx` intent vs yes/no
   utterance parsing; how to prevent the model from confirming on the
   user's behalf. Needed by Phase 2.
4. **Price source & policy:** provider, TTL, default-on vs opt-in, fallback
   when offline. Needed by Phase 2.
5. **MVP chain backend:** public mempool.space acceptable for Phase 0–3, or
   require node earlier? Owner: product. Needed by Phase 0.
6. **Script types in v1:** native-segwit only, or also taproot send/receive?
   Affects descriptor handling and device policy friction (R4). Needed by
   Phase 1.
7. **Gap-limit policy defaults + rescan UX** (auto-widen? warn?). Phase 1.
8. **UI endgame:** TUI vs Electron vs Tauri vs native; who owns chat visual
   design, confirmation cards, device-handoff states. Owner: design. Needed
   by Phase 2 (CLI until then).
9. **Distribution & install:** pip vs bundled app (PyInstaller/py2app vs
   signed/notarized packages); how non-technical users get Python; model
   download UX (size, hash pinning). Needed by Phase 6, spike earlier.
10. **Windows HWW driver story** (WinUSB/libusb, per-vendor). Phase 3.
11. **Node recommendation policy:** Bitcoin Core vs Knots; Umbrel/Start9
    framing; how opinionated the doctor should be. Needed by Phase 4.
12. **Project license** (MIT/Apache-2.0?) and trademark/name decision.
13. **Telemetry & crash reporting:** default none; opt-in only? Privacy
    policy for local logs. Phase 6.
14. **Chat-log retention:** on-disk transcripts retention/scrub/export UX.
15. **Languages:** English-only v1? (Gemma 4 covers 140+; i18n is cheap
    later but UX copy must be structured for it.) Phase 6.
16. **Testnet choice:** testnet3 vs testnet4 vs signet for all dev work.
    Decide in Phase 0; affects faucet/workflow docs.
17. **Airgap file conventions:** PSBT encoding (binary vs base64), filename
    scheme, checksums, per-device folder layouts. Phase 3.
18. **Fingerprint/descriptor trust flow:** what UX when device xpub fingerprint
    doesn't match the descriptor the user supplied (wrong wallet? typo?
    attack?). Phase 1/3.
19. **Multi-wallet support:** single zpub per profile in v1; multi-profile
    roadmap? Phase 1.
20. **Performance budget:** acceptable TTFT and turn latency on base Mac mini
    and worst-case Windows box; drives quant + runtime choice. Phase 0.

---

## 15. Proposed Repository Layout

```
local-wallet/
  docs/
    PROJECT.md          ← this document
    adr/                ← architecture decision records (open questions land here)
  src/localwallet/
    agent/              # model runtime, grammar, context injection, loop
    protocol/           # envelope schema, intent registry, dispatcher
    wallet/             # descriptors, derivation, scanning, cache
    chain/              # THE ONLY networked module (Esplora, fees, price)
    tx/                 # coin selection, PSBT, re-validation, broadcast glue
    signer/             # Signer iface: file / HWI-USB / (QR later)
    node/               # node doctor: detect, guide, health
    store/              # SQLite
    ui/                 # CLI now; TUI/GUI later
  evals/                # golden prompts, red-team, fixtures
  tests/
  models/               # gitignored; pinned-download script + hashes
  pyproject.toml
  AGENTS.md             # conventions for AI assistants working in-repo
```

Working agreements: testnet-only until the Phase 6 gate; every
protocol/prompt change ships with an eval run; network imports outside
`chain/` fail lint; ADR for every answered open question.

---

## 16. Suggested Team

- **2 engineers:** one protocol/wallet-core (Bitcoin depth), one agent/LLM
  runtime + evals.
- **1 designer:** chat UX, confirmation cards, device-handoff states,
  status/privacy indicators.
- **Fractional security reviewer** with Bitcoin protocol experience, at
  Phase 2, 3, and 6 gates.
- HWI/device quirks benefit from access to a real-device matrix (Ledger,
  Trezor, Coldcard, Jade minimum).

---

## 17. Glossary

- **xpub / zpub / ypub / tpub / vpub:** extended public keys; prefixes encode
  script type and network (zpub = P2WPKH mainnet, vpub = same on testnet).
- **Output descriptor:** a string fully describing a set of addresses
  (e.g. `wpkh([fp/84'/0'/0']zpub/{0,1}/*)`); the modern way to define a wallet.
- **PSBT (BIP174):** Partially Signed Bitcoin Transaction — the interchange
  format between this app and hardware wallets.
- **UTXO:** unspent transaction output; the things a wallet spends.
- **Gap limit:** how many consecutive unused addresses to derive/scan before
  assuming the chain beyond is empty (BIP44 convention: 20).
- **Esplora:** the block-explorer API shape served by mempool.space and
  self-hostable instances.
- **HWI:** Bitcoin Core's Hardware Wallet Interface; Python library/CLI
  abstracting hardware signers.
- **GBNF:** llama.cpp grammar format; used here to force valid JSON output.
- **GGUF:** llama.cpp model file format (quantized weights).
- **E2B / PLE:** "effective 2B" Gemma 4 sizing via Per-Layer Embeddings —
  small memory footprint, laptop-class hardware.
- **RBF / CPFP:** replace-by-fee / child-pays-for-parent — fee-bumping
  mechanics (out of v1 scope).
- **IBD:** initial block download — a fresh node's multi-day sync.

---

## 18. References

- Gemma 4 E2B model card: https://huggingface.co/google/gemma-4-E2B
- Gemma 4 technical report: https://arxiv.org/abs/2607.02770
- llama.cpp (grammars, GGUF): https://github.com/ggml-org/llama.cpp
- HWI: https://github.com/bitcoin-core/HWI
- embit: https://github.com/diybitcoinhardware/embit
- Specter Desktop (prior art for HWI-as-library + file signer flows):
  https://github.com/cryptoadvance/specter-desktop
- mempool.space Esplora API: https://mempool.space/docs/api
- BIPs: 32 (xpub), 39/44 (HD wallets), 84 (native segwit), 86 (taproot),
  174 (PSBT), 371 (taproot PSBT), 157/158 (Neutrino — future), 125 (RBF)
- BDK (Rust alternative, see §11): https://bitcoindevkit.org

---

*End of document. First execution milestone: Phase 0 walking skeleton —
see §12 for scope and acceptance criteria.*

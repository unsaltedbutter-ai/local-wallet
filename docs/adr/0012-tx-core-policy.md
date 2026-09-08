# ADR-0012: Tx-engine core policy — selection, dust/min-relay, vsize, RBF sequence, change, error amounts

- **Status:** Accepted
- **Date:** 2026-09-01
- **Decides:** the Phase 2 money-path policy questions left open by
  PROJECT.md §7.5 (tx engine) and N4 (RBF policy "documented, don't
  implemented"), for TCK-P2-002.
- **Scope:** `src/localwallet/tx/{dust,selection,psbt}.py`; consumers are
  the Phase 2 send-flow handlers (TCK-P2-004+) and the Phase 3
  signed-PSBT re-validation. Relates to ADR-0004 (testnet-only),
  ADR-0008 (P2WPKH-only send), ADR-0009 (gap/change index policy),
  ADR-0010 (single wallet).
- **Decoupling note:** per ticket TCK-P2-002, `tx/` imports nothing from
  `chain/` — fee rate, dust-relay rate, and UTXOs arrive as plain data.
  TCK-P2-001's fee estimator feeds the handler, which calls `tx/`.

## Context

The send flow needs deterministic, reviewable answers for: which UTXOs to
spend, what the dust threshold is, what vsize/fee the unsigned transaction
will have, what nSequence to set, and how change is produced and folded.
embit 0.8.0 ships **no** coin-selection module (verified: no
`embit.coinselection`; no branch-and-bound anywhere in the package), so
PROJECT.md §7.5's fallback applies: smallest-larger-first, implemented
in-repo, with the exact algorithm documented for reviewer reproduction.
All sat/weight math is integer; no float money anywhere.

## Decisions

### 1. Coin selection — deterministic smallest-larger-first + two bounded improvement passes

Given the wallet's spendable UTXO snapshot (plain data), amount, integer
fee rate (sat/vB), and the change output's vB cost:

1. **Canonical order:** sort by `(value_sats, txid, vout)` ascending;
   duplicate `(txid, vout)` refused (fail closed on a corrupt snapshot).
2. **Greedy:** add UTXOs in canonical order; after each addition
   *finalize* the candidate set (§2 below); stop at the first prefix that
   finalizes. UTXOs worth less than the incremental per-input fee
   (`P2WPKH_INPUT_WEIGHT_WU / 4 × rate` = 68 vB × rate) are skipped during
   the walk — a dust input adds less value than the fee it costs, can never
   help finalization, and would poison every prefix (the skip is a pure
   function of value and rate, hence deterministic). A full-set selection
   only happens when no proper prefix finalizes — "never select ALL utxos
   when a subset suffices" is structural.
3. **Single-coin improvement:** if the greedy result uses ≥ 2 inputs and
   some single UTXO finalizes with `fee <= greedy_fee`, take the smallest
   such UTXO ("covers amount+fee exactly-better": never a worse fee,
   strictly fewer inputs, preserves small coins).
4. **No-shattering dust sweep:** only when the result folds change (no
   change output) AND the unselected wallet remainder is `0 < r <
   change_dust` (dead dust on-chain), add unselected UTXOs in canonical
   order until one superset finalizes with a viable change output
   (`change >= change_dust`); if none does, keep the prior result. This
   is the only step that selects beyond the minimum, and it fires only
   when the alternative is unspendable wallet dust.

Rationale: smallest-first minimizes fee and avoids shattering large
coins; the sweep converts unavoidable dead-dust situations into
consolidation. With ascending greedy the sweep trigger is rare by
construction (the remainder after a stopping greedy prefix consists of
UTXOs ≥ the last selected one), which is the intended conservatism.
Every step is a pure function of the inputs — same inputs, same
selection, verified by repeat/shuffle tests.

### 2. Finalization, dust folding, change policy

For a candidate set, with change output (weight accounted at
`4 × change_cost_vbytes`, caller-supplied, validated ≥ the change
script's serialized minimum):

- **Case A (change):** `fee = ceil(weight/4) × rate`;
  `change = inputs_total − amount − fee`; holds iff `change ≥
  dust(change_script)`.
- **Case B (fold):** change below dust or negative → the change output is
  dropped and **the entire residue `inputs_total − amount` becomes the
  fee** (a changeless transaction has nowhere else to put it); holds iff
  the residue still meets the rate-determined fee for the changeless
  shape. The residue is *never* added to the recipient — the recipient
  value stays exactly what the user confirmed. Since Case B only fires
  when Case A's change was below dust, the folded amount is bounded by
  the change dust threshold plus the rate-determined fee.

**Change production:** change goes to a *fresh change index* of the
change branch (BIP44 branch 1) per ADR-0009 — allocation and
`next_index` bumping are store-side duties of the calling handler;
`tx/` receives the change address/value as data and never touches the
store. The change script is the wallet's own P2WPKH (ADR-0008); its dust
threshold is computed from the script size (§3), never a constant.

### 3. Dust and min-relay — Bitcoin Core's formula, verified constants

`dust_threshold(script, rate)` reproduces Bitcoin Core
`GetDustThreshold` (`src/policy/policy.cpp`, v28.0) exactly:

```
threshold = rate_sat_vb × (8 + varint(len(script)) + len(script) + spend_cost)
spend_cost = 67   if script is a witness program   # 32+4+1 + 107//4 + 4
           = 148  otherwise                        # 32+4+1 + 107 + 4
           = 0    for unspendable (OP_RETURN / >10_000 B) → threshold 0
```

The 107-byte satisfaction estimate = 73-byte signature item (1 length +
72-byte signature incl. hashtype) + 34-byte pubkey item; the witness
stack-count byte is deliberately not counted (Core's arithmetic). The
witness cost applies to *every* witness program: taproot key-path spends
are cheaper but Core kept the P2WPKH-level estimate "to not further
reduce the dust level" (PR #22779). Whole-sat/vB rates make
`CFeeRate::GetFee` arithmetic exact, so integer multiplication reproduces
Core byte-for-byte.

Verified canonical thresholds at Core defaults (3 sat/vB), asserted in
`tests/test_tx_dust.py`:

| script | serialized out | spend cost | threshold |
|---|---|---|---|
| P2PKH (25 B) | 34 | 148 | **546** |
| P2SH (23 B) — incl. P2SH-P2WPKH *outputs* | 32 | 148 | **540** |
| P2WPKH (22 B) | 31 | 67 | **294** |
| P2WSH / P2TR (34 B) | 43 | 67 | **330** |
| OP_RETURN | — | — | **0** |

**Note on "P2SH-P2WPKH = 360":** that sometimes-quoted figure does not
follow from Core's formula — a nested-segwit *output* is a plain P2SH
script (`IsWitnessProgram()` false), so the legacy +148 branch applies
and Core's threshold is 540. The ticket's canonical-constant list
(546/294/330) is reproduced; the 360 entry is recorded here as folklore
with the authoritative citation instead of being hardcoded.

`min_relay_fee_vbytes(vsize, rate=1)` = `max(1, vsize × rate)` with the
vsize bounded by Core standardness (`MAX_STANDARD_TX_WEIGHT`/4 =
100_000 vB) — the default 1 sat/vB mirrors Core's `minrelaytxfee`
(1000 sat/kvB). Property tests (seeded, deterministic — no new test
dependencies): linearity in rate, monotonicity in script size, fuzz over
20..80-byte scripts against independent arithmetic.

### 4. vsize accounting — integer weights, verified against embit

Weight components (integer weight units, WU; `vsize = ceil(weight/4)`):

- overhead: `4×(4 + varint(n_in) + varint(n_out) + 4) + 2` = 42 WU
  (10.5 vB) for ≤252 counts — version/counts/locktime at 4×, segwit
  marker+flag at 1×;
- P2WPKH input: `4×41 + 108 = 272 WU` (68 vB) — witness 108 B = 1 stack
  item + 73 B sig item + 34 B pubkey item (max-size convention:
  71-byte max DER + 1 hashtype; real signatures are often 1 B shorter);
- output: `4×(8 + varint(len) + len)` — P2WPKH = 124 WU (31 vB).

(The ticket's exploratory "57.25 vB input" is taproot's key-path number,
not P2WPKH's; the empirical embit measurement below settles P2WPKH at
272 WU.)

Empirical verification (embit 0.8.0, tests assert these):

- 1-in/2-out P2WPKH tx: embit-stripped 113 B, with maximal witnesses
  223 B → weight 562 WU, vsize **141** — the estimator matches **exactly**
  (also 2-in: 834 WU / 209 vB; 3-in; change/no-change variants).
- Really signed fixture (throwaway key from the public BIP32 test vector
  1 seed, signed in-tests only): actual DER signature 70 B + hashtype =
  71 B content → witness 107 B → weight **561** vs estimated **562** —
  same vsize (141). Bounded drift: ±1 WU ⇒ ±1 vB, documented; pre-sign
  the exact signature length is unknowable, so the max-witness
  convention is the correct conservative estimate.

Fees are computed on vsize (Core-style), so at most 3 WU of rounding
headroom per transaction — never below the min-relay floor for rates
≥ 1 sat/vB. `InsufficientFundsError.needed` is reported as
`amount + rate × vsize` of the full-wallet changeless shape.

### 5. RBF sequence policy (N4) — signal opt-in replaceability, don't bump

**Decision: every input is built with `sequence = 0xfffffffd`** (BIP 125
opt-in RBF signaling). Rationale: Phase 5+ *may* add fee bumping without
a flag-day migration, and uniform signaling makes all wallet
transactions behave identically under replaceability-aware policies.
v1 implements **no bumping** (N4): the fee UX must set the expectation
that a low-fee transaction may sit unconfirmed for hours/days (R6).
`0xfffffffe` (RBF-disabled) and `0xffffffff` (final) are **refused** by
the builder — the policy is structural, not conventional, and
`validate_psbt_shape` re-checks it on every input. This ADR is the N4
documentation deliverable; no bumping code ships in Phase 2.

### 6. PSBT content and structural validation

Inputs carry `witness_utxo` (BIP 174 field 01) and `bip32_derivations`
(field 02) computed by embit from the account-level watch key, with the
origin fingerprint/path the caller supplies (the same origin as the
wallet descriptor; the fingerprint convention follows descriptor.py —
the account key's own fingerprint until Phase 3 device registration
supplies/verifies the master fingerprint per OQ18). A per-input
structural check requires the derived child key's `hash160` to equal the
witness program — a mismatched derivation/script pair is refused (fail
closed). Inputs and change are P2WPKH-only (ADR-0008); recipient scripts
stay type-agnostic at the `tx/` layer (dust-computable, not unspendable)
with the type policy enforced upstream at intent validation.
`validate_psbt_shape` re-checks input counts, output scripts/values in
exact order, witness UTXO presence, sequence policy, and the fee
recomputed from the PSBT's own witness UTXOs — the pre-sign invariant
Phase 3's tampered-PSBT re-validation extends.

### 7. Amounts in `InsufficientFundsError` — UI text, not logs

PROJECT.md §7.8 forbids amounts in *logs/error reports*. The
send-flow error shown in chat is **user-facing UI** — the same surface
that displays balances and confirmation cards — so
`InsufficientFundsError` deliberately carries `needed`/`available` sats
in its message for actionable clarity ("insufficient funds: need X
sats, have Y sats"). Every other `tx/` exception remains value-free.
Contract: callers may render this exception in chat; they must never
place it (or any message containing amounts) into logging context.

## Consequences

- Selection, dust, vsize, and fee are deterministic pure functions of
  their data inputs; `tx/` performs no I/O and imports no `chain/`
  module (lint-enforced), so TCK-P2-001 can evolve independently.
- Case-B folding and the dust sweep are pinned by tests; any change to
  the algorithm requires updating the module docstring, this ADR, and
  the docstring-reproduction tests together.
- The ±1 vB pre-sign vsize drift is bounded and documented; Phase 3's
  broadcast path must re-derive the actual fee from the signed
  transaction (re-validation), not from the estimate.
- Standardness limits (transaction weight, sigops) are not enforced by
  `tx/` beyond sanity bounds; the Phase 3 pre-broadcast re-validation
  owns them.
- Sparrow importability of the produced PSBT is verified end-to-end by
  TCK-P2-006 (Phase 2 AC); this ticket provides the base64 artifact and
  embit round-trip evidence.

## Amendment (TCK-HW-003, 2026-09-07): change-output derivations + sign-time fingerprint patch

Decision 6's "own fingerprint of the account key until registration"
shipped a live blocker (MW-4): BIP 174 derivation fields pair a MASTER
fingerprint with a path from `m/`, and devices match on exactly that —
with the account fingerprint no input is relevant to the device, and the
absent change-OUTPUT derivation made the device render our change as a
plain external address.

In-policy now (implementation: `tx/psbt.py`, `signer/hwi.py`; trust
framing: ADR-0015 amendment #3):

1. `build_unsigned_psbt` requires `change_index` whenever it builds a
   change output and emits that output's `bip32_derivations`
   (`m/84'/0'/0'/1/change_index`, child key re-derived and structurally
   checked against the change script — the same fail-closed convention
   as the inputs).
2. The build-time fingerprint stays the account fp (a watch-only wallet
   cannot know the device master fp at create time); the USB signer
   rewrites it to the device master fp at sign time, targeting ONLY
   derivation entries that carry this wallet's account fp, both scopes.
   Paths and pubkeys are never touched.

Neither step can affect consensus: `bip32_derivations` are signer hints
outside the BIP-143 digest, and `tx/revalidate.py` reads no
`bip32_derivations` at all — the re-validation verdict on a
patched-then-signed PSBT is byte-for-byte the verdict on the unpatched
one (pinned by `tests/test_psbt_master_fp.py`). The canonical
Sparrow-import fixture (TCK-P2-006) is re-pinned for the added
change-derivation bytes.

## Amendment (TCK-FEE-002, 2026-09-08): explicit sat/vB rate override in `create_tx`

TCK-UX-004 made the estimator's top-rung "faster" refusal *ask* the user for
an explicit rate ("tell me a rate in sat/vB and I'll rebuild the transaction
at that rate"). This adds the field that answer flows into — a schema
extension, NOT a confirm-gate change (the destructive flow is untouched;
still `create_tx → confirm_tx → sign_tx → broadcast_tx`, dual-key gate as
before), so it lives here with the fee policy.

In-policy now (`protocol/envelope.py`, `app.py`, grammar, prompt in lockstep):

1. `create_tx` accepts an optional `fee_rate_sat_vb` — a TRUE JSON integer
   (`bool`/`string`/`float`/`null` rejected at layer 2), bounded
   `1..MAX_FEE_RATE_SAT_VB` (`10_000`). The floor `1` is the min-relay band;
   the ceiling MIRRORS the tx engine's own rate guard (`tx/dust.py`
   `_validate_rate` refuses any rate `> 10_000` sat/vB — 1000× min-relay — as
   caller error), so the envelope admits nothing the money path would reject
   anyway. The real dust/min-relay decision stays computed from script size
   in `tx/dust.py`; these are coarse schema transport bounds, never the dust
   rule.
2. `fee_rate_sat_vb` is MUTUALLY EXCLUSIVE with `fee_target` — enforced at
   the GBNF tail (single alternation: a bare tail, a `fee_target`, or a
   `fee_rate_sat_vb`, never both) AND the pydantic model (covers non-grammar
   producers). Presenting both is ambiguous fee intent: rejected → one
   re-prompt → `clarify`. The handler never picks a winner.
3. When `fee_rate_sat_vb` is present the handler bids that literal rate and
   does NOT call the estimator. It is a user-quoted value, taken VERBATIM
   (the model may only copy a number the user stated; the prompt forbids
   inventing/converting/rounding). No rung is recorded (`fee_target=None`,
   no fabricated ETA), and the card retires the one-shot speed offer exactly
   as a stated `fee_target` would.
4. The override routes through the SAME FLOW-REQUOTE replacement path
   (commit-only-on-success, fresh `tx_ref`/TTL, old ref inert); a re-quote
   carrying it simply bypasses the rung ceiling/floor guard — that bypass is
   the ceiling ask's resolution, reached only after the handler offered it.

## Amendment (TCK-UTXO-002, 2026-09-07): partitioned selection, target min/max, low-fee consolidation

> Draft landed verbatim from `docs/ux-utxo-notes-design.md` §2.4; the
> implementation is `tx/selection.py` (policy layers A/B/C + step 5), the
> settings ladder is `config.resolve_coin_selection_settings` +
> `Store.set_coin_setting`, and the label join is the caller's duty
> (`tx/` reads only the resulting booleans).

Decisions 1–4 are unchanged *within a candidate pool*; three policy
layers wrap them. Coin tags and target settings arrive as plain data on
the UTXO snapshot and the settings argument, exactly like the fee rate —
`tx/` performs no store access and reads no user free text (the note
field never reaches this module; only the `kyc_side`/`mixed` booleans and
integer settings do).

1. **Partition preference.** Run decision 1 (steps 1–4) over the kyc-side
   pool, the other-side pool, and the full set. A pure pool wins over the
   mixed result whenever it finalizes (choice: lowest `fee_sats`, then
   fixed pool order other-side → kyc-side); mixed inputs occur only when
   no pure pool funds the amount. Purely by design, a pure pool may pay
   more than a mixed selection; the caller must surface the mix on the
   confirmation card. Coins created by a mixed spend inherit both
   classes (caller-side lineage; `tx/` sees only the resulting boolean,
   where mixed means kyc-side).
2. **Step 3 bound (max target).** The single-coin improvement pass never
   substitutes a coin above `utxo_target_max_sats`; preserving one large
   coin outweighs a cheaper fee.
3. **Step 5, low-fee consolidation.** After step 4: when the fee rate ≤
   `consolidate_below_sat_vb`, fold unselected pool coins with
   `value_sats < utxo_target_min_sats` in canonical order, bounded by 4
   added inputs and per-coin value ≥ 2 × incremental input fee, keeping
   finalization. Pure function of inputs like steps 1–4; the conservation
   assert covers the final set.
4. **Settings ladder.** `utxo_target_min_sats` (default 100,000),
   `utxo_target_max_sats` (default 10,000,000), `consolidate_below_sat_vb`
   (default 2): `settings` keys with typed fail-closed store writers
   (`Store.set_coin_setting`, bounds single-sourced from `config`), pure
   env > stored > default resolution in `config`
   (`resolve_coin_selection_settings`), malformed value = startup refusal,
   value-free errors — the ADR-0009 / ADR-0023 pattern.

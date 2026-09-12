"""Deterministic UTXO selection for the send flow (money path).

embit 0.8.0 ships no coin-selection module (verified: no
``embit.coinselection``, no branch-and-bound anywhere in the package), so
this module implements the conservative strategy PROJECT.md §7.5 prescribes
when BnB is unavailable: **smallest-larger-first**, with two explicit
improvement passes, documented step by step so a reviewer can reproduce
every decision deterministically.

THE ALGORITHM (authoritative, deterministic — same inputs, same output)
-----------------------------------------------------------------------
Inputs: ``utxos`` (the wallet's spendable UTXO snapshot, duck-typed on
``value_sats: int > 0``, ``txid: str``, ``vout: int >= 0``), ``amount_sats``
(recipient value), ``fee_rate_centisat_vb`` (integer centisat/vB,
1 sat/vB = 100 — fractional rates are whole integers in the new unit,
TCK-FEE-003/ADR-0011), and ``change_cost_vbytes`` (vB cost of appending the change output — 31 for
P2WPKH change, computed by the caller from the change script size; this
module never hardcodes it).

1. Canonical order: sort UTXOs by ``(value_sats, txid, vout)`` ascending.
   (``txid`` is compared as a lowercase hex string; fixed-length hex makes
   that a total order. Duplicate ``(txid, vout)`` pairs are refused —
   fail closed on a corrupt snapshot.)

2. Greedy accumulation: walk the canonical order, adding one UTXO at a
   time; after each addition *finalize* the candidate set (below). Stop at
   the FIRST prefix that finalizes successfully. During the walk, UTXOs
   whose value is below the incremental per-input fee
   (``P2WPKH_INPUT_WEIGHT_WU / 4 × rate`` = 68 vB × rate) are skipped: such
   a dust input adds less value than the fee it costs, can never help
   finalization, and would poison every prefix (the skip is a pure function
   of value and rate — deterministic). This visits small UTXOs first
   (fee-efficient, privacy-preserving) and can never select more UTXOs than
   needed — "never select ALL utxos when a subset suffices" is structural:
   a full-set selection only happens when no proper prefix finalizes. If
   nothing finalizes (including the full set), raise
   :class:`InsufficientFundsError`.

3. Improvement pass — single coin: if the greedy result uses ≥ 2 inputs,
   scan the canonical order for the smallest single UTXO that finalizes
   successfully on its own with ``fee <= greedy_fee``; if found, use it
   ("covers amount+fee exactly-better": never a worse fee, strictly fewer
   inputs — preserves small UTXOs).

4. Improvement pass — no-shattering dust sweep: let ``remainder`` be the
   wallet funds left unselected (sum of all passed UTXOs minus selected).
   If the greedy/final result folds change (no change output) AND
   ``0 < remainder < change_dust`` — i.e. the wallet would keep
   economically-unspendable dust — walk the canonical order over the
   unselected UTXOs, adding one at a time, and take the first superset
   that finalizes with a *viable change output* (``change >= change_dust``).
    If none does, keep the previous result. This is the only step that may
    select beyond the minimum, and it only fires when the alternative is
    dead dust on-chain (never gratuitous UTXO shattering).

POLICY LAYERS (TCK-UTXO-002, ADR-0012 amendment; docs/ux-utxo-notes-design.md
§2) — layered AROUND steps 1–4, which stay unchanged within any pool
-------------------------------------------------------------------------------
Tags arrive pre-joined by the CALLER as plain data on the UTXO objects —
this module reads NO store, NO env, NO free text (the note field never
reaches it). Each duck-typed UTXO may carry ``kyc_side: bool`` (absent =
False = other-side, §1.4 "unlabeled = other-side by default", which makes an
untagged wallet behave exactly like pre-amendment selection). The pure
:func:`coin_partition` is the canonical tag-set -> (kyc_side, mixed) mapping
for that caller-side join (mixed-by-lineage coins are kyc-side — fail-safe).

A. **Partition preference.** Steps 1–4 (+5) run over the other-side pool and
   the kyc-side pool; any pure pool that finalizes wins — between two
   funding pure pools the lower ``fee_sats`` wins, ties broken by fixed pool
   order (other-side, then kyc-side, so equal-fee runs are reproducible).
   The full set is only run when NO pure pool funds the amount: the result
   is then a MIX (``SelectionResult.mixed`` — the caller MUST surface the
   mix warning on the confirmation card; narration renders from the final
   selection, so a re-quote can never silently change the tag-mix). A pure
   pool MAY COST MORE than a mixed selection — that is by design (rule "don't
   mix KYC with non-KYC coins" outweighs fee-pennies, as step 3's max-coin
   rule already outweighs fee for one big coin).

B. **Step 3 bound (``utxo_target_max_sats``).** The single-coin improvement
   never substitutes a coin above target max: preserving one large coin
   outweighs a cheaper fee (canonical order is value-ascending, so the scan
   simply ends at the first over-max candidate).

C. **Step 5, low-fee consolidation (``consolidate_below_sat_vb``,
   ``utxo_target_min_sats``).** After step 4, when the fee rate <= the
   threshold AND the pool holds >= 2 unselected coins below target min:
   walk those candidates in canonical order, adding one at a time while ALL
   hold — added inputs <= ``_MAX_CONSOLIDATE`` (4); each coin's value >=
   2 x its incremental input fee (2 x 68 vB x rate — a folding coin earns
   its passage at double the greedy skip bound); the set still finalizes.
   Stop at the first violation. The number added is reported as
   ``SelectionResult.folded_count`` (the caller narrates it on the From
   line). Change/fee recompute through ``finalize()``; the conservation
   assert covers the final set.

Steps A–C are pure functions of snapshot + rate + settings, like steps 1–4:
same inputs, same output. Settings arguments are ``None`` = feature off =
pre-amendment behavior (the handler threads the values resolved by
:func:`localwallet.config.resolve_coin_selection_settings`).

FINALIZE (exact integer accounting, no float money)
---------------------------------------------------
Given a candidate set S of n inputs:

- ``inputs_total`` = Σ value_sats
- stripped-size weight (weight units, integer):
  ``overhead(n_in, n_out) + n * P2WPKH_INPUT_WEIGHT_WU + Σ output_weight``
  with ``overhead = 4*(4 + varint(n_in) + varint(n_out) + 4) + 2`` (version,
  counts, locktime at 4x; segwit marker+flag at 1x), P2WPKH input weight
  4*41 + 108 = 272 WU (witness = 1 stack item + 73 B sig item + 34 B pubkey
  item — the max-size convention: 71-byte max DER signature + 1 hashtype),
  and output weight ``4*(8 + varint(len) + len)``.
- Case A (change output exists): the change output's weight is taken as
  ``4 * change_cost_vbytes`` (the caller-supplied vB cost, validated
  against the change script's minimum serialization), so
  ``vsize_A = ceil(weight_A / 4)``, ``fee_A = ceil(vsize_A * rate_c / 100)``
  (integer-exact; byte-identical to ``vsize_A * rate`` for whole sat/vB
  rates — every pre-TCK-FEE-003 value), and
  ``change_A = inputs_total - amount - fee_A``. Case A holds when
  ``change_A >= change_dust`` (dust computed from the change script size
  via :func:`localwallet.tx.dust.dust_threshold` — never a hardcoded
  constant).
- Case B (change below dust or negative): drop the change output; the
  change residue is folded into the fee — the fee *becomes* the entire
  residue ``inputs_total - amount`` (a changeless transaction has nowhere
  else to put it), which by construction exceeds the rate-determined
  target for the changeless shape. The residue is *not* added to the
  recipient, whose value must stay exactly what the user confirmed. Case B
  holds when ``inputs_total - amount >= ceil(vsize_B * rate_c / 100)``. Because Case B
  only fires when Case A's change was below dust, the folded amount is
  bounded by the change dust threshold plus the rate-determined fee.

Fee convention: fees are computed on vsize (Core-style), so at most
3 weight-units of rounding headroom per tx — never under the min-relay
floor for rates >= 1 sat/vB. The fee is the CEILING of vsize x rate so a
fractional rate is never under-bid by its own rounding (the ceil is the
sanity bound, ADR-0011).

Conservation invariant: :func:`select_coins` asserts
``inputs_total == amount + fee + change (or 0)`` when constructing
:class:`SelectionResult`. This is a programming-error tripwire at the
money-path boundary, not input validation — a violation is a bug in this
module, so ``AssertionError`` is the documented failure mode (assert, not
error handling).

Determinism: pure integer arithmetic, stable canonical sort, no randomness,
no wall-clock, no network (fee rate and UTXOs arrive as plain data — the
chain/fee-estimate module is never imported here).

Value-free errors: every exception message except
:class:`InsufficientFundsError` is value-free. ``InsufficientFundsError``
carries needed/available sats deliberately: handler errors surfaced in chat
are user-facing UI (the chat shows balances and amounts everywhere), not
logs — callers must keep them out of logging context (ADR-0012).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from localwallet.config import (
    COIN_SETTING_BOUNDS,
    CONSOLIDATE_BELOW_SAT_VB_SETTING,
    UTXO_TARGET_MAX_SETTING,
    UTXO_TARGET_MIN_SETTING,
)
from localwallet.tx.dust import (
    TxEngineError,
    dust_threshold,
    varint_size,
)

__all__ = [
    "P2WPKH_INPUT_WEIGHT_WU",
    "InsufficientFundsError",
    "SelectionError",
    "SelectionResult",
    "coin_partition",
    "estimate_tx_vsize",
    "fee_sats_for",
    "select_coins",
]

#: Weight units of one P2WPKH input under the max-witness convention:
#: non-witness 41 B (outpoint 32 + empty-scriptSig varint 1 + nSequence 4)
#: at 4x, plus witness 108 B (1 stack item + [1+72] signature item with
#: 71-byte max DER + 1 hashtype, + [1+33] compressed pubkey item) at 1x.
#: Verified against embit-built transactions (tests/test_tx_selection.py).
_P2WPKH_INPUT_NONWITNESS_BYTES = 32 + 4 + 1 + 4
_P2WPKH_WITNESS_BYTES = 1 + (1 + 72) + (1 + 33)
P2WPKH_INPUT_WEIGHT_WU = 4 * _P2WPKH_INPUT_NONWITNESS_BYTES + _P2WPKH_WITNESS_BYTES  # 272

#: Segwit marker + flag byte: serialized once (witness section), not 4x.
_SEGWIT_MARKER_FLAG_WU = 2

#: Partition classes over the closed coin-tag vocabulary (doc §1.4 table;
#: ``store.models.COIN_TAGS`` is the authoritative tag list — mirrored here
#: because tx/ must not import store/). ``consolidation`` is deliberately
#: absent: it describes the payment, not the coins' character (display-only).
#: A coin whose tags touch BOTH classes (a lineage union after a mixed spend)
#: is MIXED: kyc-side — the fail-safe direction ("a mixed coin can un-mix
#: nothing", §1.3) — with the ``mixed`` flag for the caller's narration.
_KYC_SIDE_TAGS = frozenset({"kyc", "exchange"})
_OTHER_SIDE_TAGS = frozenset({"p2p", "purchase"})

#: Step 5 consolidation bound (doc §2.2): at most this many extra inputs.
_MAX_CONSOLIDATE = 4

#: v1 wallet change script (BIP84 P2WPKH): OP_0 <20-byte program>. Built
#: from the template, never a dust constant — the threshold still comes
#: from :func:`dust_threshold` at the change script's own size.
_P2WPKH_SCRIPT_TEMPLATE = b"\x00\x14" + b"\x00" * 20

#: Sanity bounds (fail closed on absurd inputs). 1000 inputs keeps the
#: estimated weight far inside Core's standardness limit
#: (MAX_STANDARD_TX_WEIGHT = 400_000 WU) and matches the PSBT builder.
_MAX_INPUTS = 1000
_MAX_OUTPUTS = 1000
#: Fee-rate sanity bound in centisat/vB: 1_000_000 = 10_000 sat/vB (the
#: pre-fractional ceiling, unit-converted), 1 = 0.01 sat/vB (the floor of
#: the fractional ladder, TCK-FEE-003). The min-relay standardness gate in
#: the PSBT builder still applies on top, unchanged.
_MAX_FEE_RATE_CENTISAT_VB = 1_000_000
_MAX_CHANGE_COST_VBYTES = 100_000

# v1 sends are P2WPKH-only on the input side (ADR-0008); the dust/vsize
# math stays generic so recipient scripts need no special-casing here.


class SelectionError(TxEngineError):
    """A coin-selection request is structurally invalid (value-free)."""


class InsufficientFundsError(SelectionError):
    """The wallet cannot fund ``amount_sats`` at the requested fee rate.

    Deliberately carries ``needed``/``available`` sats in the message:
    handler errors surfaced in chat are user-facing UI (the chat displays
    balances and amounts), not logs — see ADR-0012. Callers must keep this
    exception out of any logging context.
    """

    def __init__(self, needed: int, available: int) -> None:
        self.needed = needed
        self.available = available
        super().__init__(
            f"insufficient funds: need {needed} sats, have {available} sats"
        )


def coin_partition(tags: Iterable[str]) -> tuple[bool, bool]:
    """Map a coin's tag set to its selection partition (doc §1.4 + §1.3).

    Returns ``(kyc_side, mixed)`` — the two booleans the caller joins onto
    each UTXO before :func:`select_coins` (tags themselves never reach the
    engine; the free-text note never reaches this module at all). The
    decision table:

    ==============================  ==========  ======  ==================
    tag set                         kyc_side    mixed partition
    ==============================  ==========  ======  ==================
    contains kyc/exchange AND       True        True    kyc-side pool
      p2p/purchase (mixed lineage)
    contains only kyc/exchange      True        False   kyc-side pool
      (any mix of the two)
    everything else: p2p, purchase, False       False   other-side pool
      consolidation (neutral),
      unlabeled/empty
    ==============================  ==========  ======  ==================

    Unknown tag words count as neither side (the store's typed writer only
    admits the closed set; this is belt-and-braces, never an error).
    """
    kyc = False
    other = False
    for tag in tags:
        if tag in _KYC_SIDE_TAGS:
            kyc = True
        elif tag in _OTHER_SIDE_TAGS:
            other = True
    return (kyc, kyc and other)


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """Outcome of :func:`select_coins`.

    Attributes:
        selected: The chosen UTXO objects, passed through verbatim, in the
            canonical ``(value_sats, txid, vout)`` ascending order.
        change_sats: Change output value, or ``None`` when the change is
            below dust and its residue is folded into the fee (in that
            case ``fee_sats`` is the full residue ``inputs_total -
            amount_sats``, which by construction meets the rate target).
        estimated_vsize: Integer vsize (max-witness convention; verified
            against embit-built transactions — see the module docstring).
        fee_sats: Fee in sats for the estimated vsize at the given rate.
        inputs_total: Sum of the selected UTXO values in sats.
        mixed: ``True`` iff the FINAL selection spans both partition
            classes (only reachable via the full-set fallback — a pure pool
            always wins when it funds). The caller must surface the mix
            warning on the confirmation card when set; renderers compute it
            from the just-returned set, so a re-quote can never silently
            change the tag-mix (doc §4.2).
        folded_count: Step 5 consolidation count — unselected below-target-min
            coins folded in "to save fees later" (0 = step did not fire).
            The caller narrates it on the card's From line (doc §2.2/§4.3).
    """

    selected: list[Any]
    change_sats: int | None
    estimated_vsize: int
    fee_sats: int
    inputs_total: int
    mixed: bool = False
    folded_count: int = 0


def _validate_int(value: int, name: str, lo: int, hi: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SelectionError(f"{name} must be an integer")
    if not lo <= value <= hi:
        raise SelectionError(f"{name} must be between {lo} and {hi}")
    return value


def fee_sats_for(vsize: int, rate_centisat_vb: int) -> int:
    """Integer-exact fee for a vsize at a centisat/vB rate: ceil(vsize*c/100).

    The ceil makes the rate a TRUE floor for fractional bids (a whole-sat
    rate multiplies out exactly — every pre-TCK-FEE-003 value is unchanged).
    Pure integer arithmetic; no float money (docs/fee-fractional-plan.md).
    """
    return -(-vsize * rate_centisat_vb // 100)


def _utxo_sort_key(utxo: Any) -> tuple[int, str, int]:
    txid = getattr(utxo, "txid", None)
    if not isinstance(txid, str) or not txid:
        raise SelectionError("utxo txid must be a non-empty string")
    vout = getattr(utxo, "vout", None)
    if not isinstance(vout, int) or isinstance(vout, bool) or vout < 0:
        raise SelectionError("utxo vout must be a non-negative integer")
    value = getattr(utxo, "value_sats", None)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SelectionError("utxo value must be a positive integer of sats")
    return (value, txid.lower(), vout)


def output_weight_wu(script_len: int) -> int:
    """Serialized weight of one output: 4x(value(8) + varint + script)."""
    return 4 * (8 + varint_size(script_len) + script_len)


def _tx_overhead_weight_wu(n_inputs: int, n_outputs: int) -> int:
    """Tx-level weight overhead for a segwit transaction.

    version (4) + input-count varint + output-count varint + locktime (4)
    live in the stripped (non-witness) serialization and count 4x; the
    segwit marker+flag (2 bytes) serialize only in the witness section and
    count 1x. Verified byte-exact against embit-built transactions.
    """
    return 4 * (4 + varint_size(n_inputs) + varint_size(n_outputs) + 4) + (
        _SEGWIT_MARKER_FLAG_WU
    )


def estimate_tx_vsize(
    n_inputs: int,
    recipient_scripts: Sequence[bytes],
    change_cost_vbytes: int | None,
) -> int:
    """Estimate the signed-transaction vsize (integer, max-witness).

    P2WPKH-only engine (ADR-0008): inputs weigh ``P2WPKH_INPUT_WEIGHT_WU``
    each under the 72-byte-signature convention, the scripts in
    ``recipient_scripts`` weigh their exact serialized size, and when
    ``change_cost_vbytes`` is given the change output is accounted at
    ``4 * change_cost_vbytes`` weight units. The result is
    ``ceil(weight / 4)``.

    The change output is accounted **only** through ``change_cost_vbytes``
    — do not include the change script in ``recipient_scripts`` (that
    would double-count it). For a P2WPKH change script the caller passes
    ``change_cost_vbytes = 9 + len(change_script)`` (= 31), which is
    exactly its serialized output size in vB.

    This is the exact same accounting the selection loop uses, verified
    against embit-built transactions with maximal P2WPKH witnesses
    (exact match) and against a real signed fixture (within 1 weight
    unit; see ADR-0012 for the measured numbers).
    """
    n_inputs = _validate_int(n_inputs, "n_inputs", 1, _MAX_INPUTS)
    n_outputs = len(recipient_scripts) + (1 if change_cost_vbytes is not None else 0)
    if not 1 <= n_outputs <= _MAX_OUTPUTS:
        raise SelectionError("transaction output count out of range")
    weight = _tx_overhead_weight_wu(n_inputs, n_outputs)
    weight += n_inputs * P2WPKH_INPUT_WEIGHT_WU
    for script in recipient_scripts:
        if not isinstance(script, (bytes, bytearray, memoryview)):
            raise SelectionError("recipient scripts must be bytes")
        weight += output_weight_wu(len(script))
    if change_cost_vbytes is not None:
        change_cost = _validate_int(
            change_cost_vbytes, "change_cost_vbytes", 1, _MAX_CHANGE_COST_VBYTES
        )
        weight += 4 * change_cost
    return -(-weight // 4)  # ceil(weight / 4), integer-exact


@dataclass(frozen=True, slots=True)
class _Finalized:
    fee_sats: int
    change_sats: int | None
    vsize: int
    total: int


class _Selector:
    """Internal strategy state; see module docstring for the algorithm."""

    def __init__(
        self,
        ordered: list[Any],
        amount_sats: int,
        fee_rate_centisat_vb: int,
        change_cost_vbytes: int,
        output_script: bytes,
        change_script: bytes,
    ) -> None:
        self.ordered = ordered
        self.amount = amount_sats
        self.rate_c = fee_rate_centisat_vb
        self.change_cost = change_cost_vbytes
        self.output_script = output_script
        self.change_script = change_script
        self.change_dust = dust_threshold(change_script)
        self.recipient_weight = output_weight_wu(len(output_script))
        self.wallet_total = sum(u.value_sats for u in ordered)

    def _vsize(self, n_inputs: int, with_change: bool) -> int:
        n_outputs = 1 + (1 if with_change else 0)
        weight = _tx_overhead_weight_wu(n_inputs, n_outputs)
        weight += n_inputs * P2WPKH_INPUT_WEIGHT_WU
        weight += self.recipient_weight
        if with_change:
            weight += 4 * self.change_cost
        return -(-weight // 4)

    def finalize(self, selected: list[Any]) -> _Finalized | None:
        """Exact integer finalization of a candidate set (docstring §FINALIZE)."""
        total = sum(u.value_sats for u in selected)
        # Case A: change output exists.
        vsize_a = self._vsize(len(selected), with_change=True)
        fee_a = fee_sats_for(vsize_a, self.rate_c)
        change_a = total - self.amount - fee_a
        if change_a >= self.change_dust:
            return _Finalized(fee_a, change_a, vsize_a, total)
        # Case B: change below dust (or negative) — the change output is
        # dropped and the ENTIRE residue above the recipient value becomes
        # the fee (there is nowhere else for it to go). Valid only if that
        # folded fee still meets the rate-determined target for the
        # changeless shape.
        vsize_b = self._vsize(len(selected), with_change=False)
        residue = total - self.amount
        if residue >= fee_sats_for(vsize_b, self.rate_c):
            return _Finalized(residue, None, vsize_b, total)
        return None


@dataclass(frozen=True, slots=True)
class _Policy:
    """Engine-side policy bundle (amount/rate/scripts + the §2.3 settings;
    a ``None`` setting switches its layer off = pre-amendment behavior)."""

    amount_sats: int
    fee_rate_centisat_vb: int
    change_cost_vbytes: int
    output_script: bytes
    change_script: bytes
    target_min_sats: int | None
    target_max_sats: int | None
    consolidate_below_sat_vb: int | None


def _select_from_pool(ordered: list[Any], policy: _Policy) -> SelectionResult | None:
    """Steps 1-5 over ONE canonical-ordered pool; ``None`` = pool can't fund.

    Steps 1-4 are the ADR-0012 decision-1 algorithm, unchanged within a pool
    (module docstring); step 5 is the §2.2 low-fee consolidation. Never
    raises :class:`InsufficientFundsError` — the caller (pool driver) decides
    which non-finalizing run owns the user-facing error.
    """
    if not ordered:
        return None
    selector = _Selector(
        ordered,
        policy.amount_sats,
        policy.fee_rate_centisat_vb,
        policy.change_cost_vbytes,
        policy.output_script,
        policy.change_script,
    )

    # Step 2: greedy smallest-larger-first; first finalizing prefix wins.
    # A UTXO worth less than the incremental per-input fee (the P2WPKH
    # input weight is 272 WU = 68 vB exactly, so 68 × rate sats) adds less
    # value than the fee it costs: it can never help finalization and would
    # poison every greedy prefix (TCK-P2-002 review). The skip is a pure
    # function of value and rate — deterministic. The improvement passes
    # below still see every UTXO; finalize() remains the real gate there.
    min_useful_value = fee_sats_for(P2WPKH_INPUT_WEIGHT_WU // 4, policy.fee_rate_centisat_vb)
    chosen: list[Any] = []
    finalized: _Finalized | None = None
    for utxo in ordered:
        if utxo.value_sats < min_useful_value:
            continue
        chosen.append(utxo)
        finalized = selector.finalize(chosen)
        if finalized is not None:
            break
    if finalized is None:
        return None  # pool cannot fund: the driver moves to the next pool

    # Step 3: single-coin improvement (only when greedy used >= 2 inputs).
    # Policy layer B: candidates above utxo_target_max_sats are never
    # substituted — preserving one large coin outweighs a cheaper fee (the
    # canonical order is value-ascending, so the scan ends at the first
    # over-max candidate).
    if len(chosen) >= 2:
        for utxo in ordered:
            if (
                policy.target_max_sats is not None
                and utxo.value_sats > policy.target_max_sats
            ):
                break
            single = selector.finalize([utxo])
            if single is not None and single.fee_sats <= finalized.fee_sats:
                chosen, finalized = [utxo], single
                break

    # Step 4: no-shattering dust sweep — only when change is folded and the
    # wallet would keep unspendable dust behind.
    remainder = selector.wallet_total - finalized.total
    if finalized.change_sats is None and 0 < remainder < selector.change_dust:
        selected_keys = {(u.txid.lower(), u.vout) for u in chosen}
        candidate = list(chosen)
        for utxo in ordered:
            if (utxo.txid.lower(), utxo.vout) in selected_keys:
                continue
            candidate.append(utxo)
            swept = selector.finalize(candidate)
            if swept is not None and swept.change_sats is not None:
                chosen, finalized = candidate, swept
                break

    # Step 5 (policy layer C): low-fee consolidation — fold unselected
    # below-target-min coins while fee rate <= the threshold, bounded by
    # _MAX_CONSOLIDATE added inputs, each earning 2x its own incremental
    # input fee, and continued finalization; stop at the FIRST violation
    # (doc §2.2; deterministic like steps 1-4).
    folded = 0
    if (
        policy.target_min_sats is not None
        and policy.consolidate_below_sat_vb is not None
        # the setting is whole sat/vB; the comparison unit-converts it
        and policy.fee_rate_centisat_vb <= policy.consolidate_below_sat_vb * 100
    ):
        selected_keys = {(u.txid.lower(), u.vout) for u in chosen}
        fold_candidates = [
            u for u in ordered
            if (u.txid.lower(), u.vout) not in selected_keys
            and u.value_sats < policy.target_min_sats
        ]
        if len(fold_candidates) >= 2:  # §2.2 trigger: pool holds >= 2 small coins
            fold_passage = 2 * fee_sats_for(
                P2WPKH_INPUT_WEIGHT_WU // 4, policy.fee_rate_centisat_vb
            )
            for utxo in fold_candidates:
                if folded >= _MAX_CONSOLIDATE or utxo.value_sats < fold_passage:
                    break
                candidate = list(chosen)
                candidate.append(utxo)
                swept = selector.finalize(candidate)
                if swept is None:
                    break
                chosen, finalized = candidate, swept
                folded += 1

    change_for_invariant = (
        finalized.change_sats if finalized.change_sats is not None else 0
    )
    # Conservation invariant at the money-path boundary (see module
    # docstring): asserted, not error-handled — a violation is a bug here,
    # and AssertionError is the documented failure mode. Covers the FINAL
    # set, after step 5 (ADR-0012 amendment decision 3).
    assert finalized.total == (
        policy.amount_sats + finalized.fee_sats + change_for_invariant
    ), "selection conservation invariant violated: inputs_total != amount + fee + change"

    kyc_flags = [bool(getattr(u, "kyc_side", False)) for u in chosen]
    return SelectionResult(
        selected=sorted(chosen, key=_utxo_sort_key),
        change_sats=finalized.change_sats,
        estimated_vsize=finalized.vsize,
        fee_sats=finalized.fee_sats,
        inputs_total=finalized.total,
        mixed=any(kyc_flags) and not all(kyc_flags),
        folded_count=folded,
    )


def select_coins(
    utxos: Sequence[Any],
    amount_sats: int,
    fee_rate_centisat_vb: int,
    change_cost_vbytes: int,
    output_script: bytes,
    *,
    change_script: bytes | None = None,
    utxo_target_min_sats: int | None = None,
    utxo_target_max_sats: int | None = None,
    consolidate_below_sat_vb: int | None = None,
) -> SelectionResult:
    """Select UTXOs for paying ``amount_sats`` (+fee) — see module docstring.

    Args:
        utxos: The wallet's spendable UTXOs; duck-typed on ``value_sats``
            (positive int), ``txid`` (non-empty str), ``vout`` (int >= 0),
            and (optionally, joined by the caller from stored coin labels —
            see :func:`coin_partition`) ``kyc_side`` (bool; absent = False =
            other-side). Plain data in, plain data out — the store and chain
            modules are never touched here.
        amount_sats: Recipient value in sats. Must be at least the dust
            threshold of ``output_script`` (computed, not hardcoded).
        fee_rate_centisat_vb: Integer fee rate in CENTISAT/vB (1 sat/vB =
            100), 1..1_000_000 — fractional sat/vB bids (TCK-FEE-003) are
            exact integers here; fees are ``ceil(vsize × rate / 100)`` sats.
        change_cost_vbytes: vB cost of appending the change output
            (31 for a P2WPKH change script; validated against the change
            script's minimum serialization).
        output_script: The recipient output scriptPubKey. Must be spendable:
            OP_RETURN-prefixed or oversized (>10_000 B) scripts are refused,
            mirroring the dust module's unspendable rule (``dust_threshold``
            would otherwise return 0 and admit any amount).
        change_script: The change output scriptPubKey. Defaults to the
            v1 wallet change type (BIP84 P2WPKH template) per ADR-0008;
            the dust threshold is always computed from this script's size.
        utxo_target_min_sats: Step 5 consolidation floor (doc §2.3): coins
            below this fold in at low fee rates. ``None`` = step 5 off.
        utxo_target_max_sats: Step 3 protection ceiling: the single-coin
            improvement never shatters a coin above this. ``None`` = unbound.
        consolidate_below_sat_vb: Fee-rate ceiling for step 5. ``None`` =
            step 5 off. The startup ladder
            (:func:`localwallet.config.resolve_coin_selection_settings`)
            owns the min<max cross-check; it is re-checked here fail-closed
            so a direct caller cannot smuggle a contradictory pair into the
            money path.

    Returns:
        A deterministic :class:`SelectionResult` (partition layer A: a pure
        pool wins whenever it finalizes — the possibly-cheaper mixed result
        is discarded, BY DESIGN; ``mixed``/``folded_count`` describe the
        FINAL selection, for the caller's card narration).

    Raises:
        SelectionError: structurally invalid arguments (value-free).
        InsufficientFundsError: the UTXOs cannot cover amount + fee
            (message carries needed/available — user-facing, see class).
    """
    if not isinstance(utxos, (list, tuple)) or len(utxos) > _MAX_INPUTS:
        raise SelectionError("utxo list is not a sequence or is over the size limit")
    amount_sats = _validate_int(
        amount_sats, "amount_sats", 0, 2_100_000_000_000_000
    )
    fee_rate_centisat_vb = _validate_int(
        fee_rate_centisat_vb, "fee_rate_centisat_vb", 1, _MAX_FEE_RATE_CENTISAT_VB
    )
    target_min_sats = (
        None
        if utxo_target_min_sats is None
        else _validate_int(
            utxo_target_min_sats,
            UTXO_TARGET_MIN_SETTING,
            *COIN_SETTING_BOUNDS[UTXO_TARGET_MIN_SETTING],
        )
    )
    target_max_sats = (
        None
        if utxo_target_max_sats is None
        else _validate_int(
            utxo_target_max_sats,
            UTXO_TARGET_MAX_SETTING,
            *COIN_SETTING_BOUNDS[UTXO_TARGET_MAX_SETTING],
        )
    )
    consolidate_below = (
        None
        if consolidate_below_sat_vb is None
        else _validate_int(
            consolidate_below_sat_vb,
            CONSOLIDATE_BELOW_SAT_VB_SETTING,
            *COIN_SETTING_BOUNDS[CONSOLIDATE_BELOW_SAT_VB_SETTING],
        )
    )
    if (
        target_min_sats is not None
        and target_max_sats is not None
        and target_min_sats >= target_max_sats
    ):
        raise SelectionError("utxo target minimum must be below the maximum")
    if not isinstance(output_script, (bytes, bytearray, memoryview)):
        raise SelectionError("output_script must be bytes")
    output_script = bytes(output_script)
    if not output_script or len(output_script) > 10_000:
        raise SelectionError("output_script is empty or oversized")
    if output_script[0] == 0x6A:
        # Symmetry with the dust module's unspendable rule (OP_RETURN
        # prefix): its dust threshold is 0, which would otherwise admit any
        # recipient amount on an unspendable output.
        raise SelectionError("output_script is unspendable (OP_RETURN)")
    if not utxos:
        raise InsufficientFundsError(
            needed=_needed_floor(amount_sats, fee_rate_centisat_vb, output_script),
            available=0,
        )

    if change_script is None:
        change_script = _P2WPKH_SCRIPT_TEMPLATE
    elif not isinstance(change_script, (bytes, bytearray, memoryview)):
        raise SelectionError("change_script must be bytes")
    else:
        change_script = bytes(change_script)
    change_cost_vbytes = _validate_int(
        change_cost_vbytes, "change_cost_vbytes", 0, _MAX_CHANGE_COST_VBYTES
    )
    # A change output can never serialize smaller than value(8) + varint + script.
    min_change_cost = 8 + varint_size(len(change_script)) + len(change_script)
    if change_cost_vbytes < min_change_cost:
        raise SelectionError(
            "change_cost_vbytes is below the change script's serialized size"
        )

    amount_dust = dust_threshold(output_script)
    if amount_sats < amount_dust:
        raise SelectionError(
            "recipient amount is below the dust threshold of its script type"
        )

    ordered = sorted(utxos, key=_utxo_sort_key)
    seen: set[tuple[str, int]] = set()
    for utxo in ordered:
        key = (utxo.txid.lower(), utxo.vout)
        if key in seen:
            raise SelectionError("duplicate utxo in the selection input")
        seen.add(key)

    policy = _Policy(
        amount_sats=amount_sats,
        fee_rate_centisat_vb=fee_rate_centisat_vb,
        change_cost_vbytes=change_cost_vbytes,
        output_script=output_script,
        change_script=change_script,
        target_min_sats=target_min_sats,
        target_max_sats=target_max_sats,
        consolidate_below_sat_vb=consolidate_below,
    )

    # Policy layer A: partition preference (doc §2.1). Run the pure pools;
    # the lowest fee wins, ties broken by FIXED pool order (other-side
    # first — iteration order + strict <, so equal-fee runs are
    # reproducible). With no kyc-side coin the driver degenerates: the
    # other-side pool IS the full set — one run, exactly pre-amendment.
    kyc_pool = [u for u in ordered if getattr(u, "kyc_side", False)]
    if kyc_pool:
        other_pool = [u for u in ordered if not getattr(u, "kyc_side", False)]
        pure: SelectionResult | None = None
        for pool in (other_pool, kyc_pool):
            run = _select_from_pool(pool, policy)
            if run is not None and (pure is None or run.fee_sats < pure.fee_sats):
                pure = run
        if pure is not None:
            return pure  # a pure pool funds; the mixed run is discarded (§2.1)
        fallback = _select_from_pool(ordered, policy)
        if fallback is None:
            raise _insufficient(ordered, policy)
        return fallback  # unavoidable mix — the caller MUST surface the warning

    result = _select_from_pool(ordered, policy)
    if result is None:
        raise _insufficient(ordered, policy)
    return result


def _insufficient(ordered: list[Any], policy: _Policy) -> InsufficientFundsError:
    """User-facing error over the FULL wallet: not even everything finalizes."""
    selector = _Selector(
        ordered,
        policy.amount_sats,
        policy.fee_rate_centisat_vb,
        policy.change_cost_vbytes,
        policy.output_script,
        policy.change_script,
    )
    needed = (
        policy.amount_sats
        + fee_sats_for(
            selector._vsize(len(ordered), with_change=False),
            policy.fee_rate_centisat_vb,
        )
    )
    return InsufficientFundsError(needed=needed, available=selector.wallet_total)


def _needed_floor(
    amount_sats: int, fee_rate_centisat_vb: int, output_script: bytes
) -> int:
    """Minimal conceivable need for the empty-wallet error: 1-input tx."""
    weight = _tx_overhead_weight_wu(1, 1)
    weight += P2WPKH_INPUT_WEIGHT_WU
    weight += output_weight_wu(len(output_script))
    vsize = -(-weight // 4)
    return amount_sats + fee_sats_for(vsize, fee_rate_centisat_vb)

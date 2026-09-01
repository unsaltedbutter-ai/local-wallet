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
(recipient value), ``fee_rate_sat_vb`` (integer sat/vB), and
``change_cost_vbytes`` (vB cost of appending the change output — 31 for
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
  ``vsize_A = ceil(weight_A / 4)``, ``fee_A = vsize_A * rate`` (integer
  sat/vB rates make the product exact), and
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
  holds when ``inputs_total - amount >= vsize_B * rate``. Because Case B
  only fires when Case A's change was below dust, the folded amount is
  bounded by the change dust threshold plus the rate-determined fee.

Fee convention: fees are computed on vsize (Core-style), so at most
3 weight-units of rounding headroom per tx — never under the min-relay
floor for rates >= 1 sat/vB.

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

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

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
    "estimate_tx_vsize",
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

#: v1 wallet change script (BIP84 P2WPKH): OP_0 <20-byte program>. Built
#: from the template, never a dust constant — the threshold still comes
#: from :func:`dust_threshold` at the change script's own size.
_P2WPKH_SCRIPT_TEMPLATE = b"\x00\x14" + b"\x00" * 20

#: Sanity bounds (fail closed on absurd inputs). 1000 inputs keeps the
#: estimated weight far inside Core's standardness limit
#: (MAX_STANDARD_TX_WEIGHT = 400_000 WU) and matches the PSBT builder.
_MAX_INPUTS = 1000
_MAX_OUTPUTS = 1000
_MAX_FEE_RATE_SAT_VB = 10_000
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
    """

    selected: list[Any]
    change_sats: int | None
    estimated_vsize: int
    fee_sats: int
    inputs_total: int


def _validate_int(value: int, name: str, lo: int, hi: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SelectionError(f"{name} must be an integer")
    if not lo <= value <= hi:
        raise SelectionError(f"{name} must be between {lo} and {hi}")
    return value


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
        fee_rate_sat_vb: int,
        change_cost_vbytes: int,
        output_script: bytes,
        change_script: bytes,
    ) -> None:
        self.ordered = ordered
        self.amount = amount_sats
        self.rate = fee_rate_sat_vb
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
        fee_a = vsize_a * self.rate
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
        if residue >= vsize_b * self.rate:
            return _Finalized(residue, None, vsize_b, total)
        return None


def select_coins(
    utxos: Sequence[Any],
    amount_sats: int,
    fee_rate_sat_vb: int,
    change_cost_vbytes: int,
    output_script: bytes,
    *,
    change_script: bytes | None = None,
) -> SelectionResult:
    """Select UTXOs for paying ``amount_sats`` (+fee) — see module docstring.

    Args:
        utxos: The wallet's spendable UTXOs; duck-typed on ``value_sats``
            (positive int), ``txid`` (non-empty str), ``vout`` (int >= 0).
            Plain data in, plain data out — the store and chain modules are
            never touched here.
        amount_sats: Recipient value in sats. Must be at least the dust
            threshold of ``output_script`` (computed, not hardcoded).
        fee_rate_sat_vb: Integer fee rate in sat/vB, 1..10000.
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

    Returns:
        A deterministic :class:`SelectionResult`.

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
    fee_rate_sat_vb = _validate_int(
        fee_rate_sat_vb, "fee_rate_sat_vb", 1, _MAX_FEE_RATE_SAT_VB
    )
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
            needed=_needed_floor(amount_sats, fee_rate_sat_vb, output_script),
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

    selector = _Selector(
        ordered, amount_sats, fee_rate_sat_vb, change_cost_vbytes,
        output_script, change_script,
    )

    # Step 2: greedy smallest-larger-first; first finalizing prefix wins.
    # A UTXO worth less than the incremental per-input fee (the P2WPKH
    # input weight is 272 WU = 68 vB exactly, so 68 × rate sats) adds less
    # value than the fee it costs: it can never help finalization and would
    # poison every greedy prefix (TCK-P2-002 review). The skip is a pure
    # function of value and rate — deterministic. The improvement passes
    # below still see every UTXO; finalize() remains the real gate there.
    min_useful_value = (P2WPKH_INPUT_WEIGHT_WU // 4) * fee_rate_sat_vb
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
        # Not even the full set finalizes: report needed/available.
        needed = amount_sats + selector._vsize(len(ordered), with_change=False) * fee_rate_sat_vb
        raise InsufficientFundsError(needed=needed, available=selector.wallet_total)

    # Step 3: single-coin improvement (only when greedy used >= 2 inputs).
    if len(chosen) >= 2:
        for utxo in ordered:
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

    change_for_invariant = (
        finalized.change_sats if finalized.change_sats is not None else 0
    )
    # Conservation invariant at the money-path boundary (see module
    # docstring): asserted, not error-handled — a violation is a bug here,
    # and AssertionError is the documented failure mode.
    assert finalized.total == (
        amount_sats + finalized.fee_sats + change_for_invariant
    ), "selection conservation invariant violated: inputs_total != amount + fee + change"

    return SelectionResult(
        selected=sorted(chosen, key=_utxo_sort_key),
        change_sats=finalized.change_sats,
        estimated_vsize=finalized.vsize,
        fee_sats=finalized.fee_sats,
        inputs_total=finalized.total,
    )


def _needed_floor(amount_sats: int, fee_rate_sat_vb: int, output_script: bytes) -> int:
    """Minimal conceivable need for the empty-wallet error: 1-input tx."""
    weight = _tx_overhead_weight_wu(1, 1)
    weight += P2WPKH_INPUT_WEIGHT_WU
    weight += output_weight_wu(len(output_script))
    vsize = -(-weight // 4)
    return amount_sats + vsize * fee_rate_sat_vb

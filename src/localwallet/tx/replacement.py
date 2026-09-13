"""BIP-125 fee-bump replacement plans for a recorded transaction (TCK-RBF-002).

Pure tx-layer money math — the core the bump conversation (TCK-RBF-004)
rides on. No I/O: no store, no chain, no PSBT serialization. The inputs
are a recorded original (:class:`OriginalTx`) plus the chosen funding coin
(``None`` = change-only paths) plus an integer target rate in centisat/vB
(TCK-FEE-003 units). The :class:`localwallet.tx.flow.PendingTx` record
carries ``fee_sats``/``fee_rate_centisat_vb``/``vsize``/``change_sats``;
decomposing its PSBT into outpoints and scripts to build an
``OriginalTx`` is the app layer's job (RBF-004) — this module consumes
plain data and touches nothing else. (Module choice: a NEW ``tx/`` module
rather than growing ``selection.py`` — selection answers "which coins fund
a payment"; this answers "how to re-shape a recorded tx under BIP-125".
Layering: ``dust`` ← ``selection`` ← ``psbt`` ← ``replacement``.)

THE BIP-125 FLOOR (:func:`rbf_min_fee_sats`)
-------------------------------------------
BIP 125 ("Opt-in Full Replace-by-Fee"): a replacement is accepted only if
it pays more than the original by at least the *incremental relay fee* of
the new transaction's size (Bitcoin Core's default incremental relay rate
is its 1 sat/vB ``minrelaytxfee``):

    floor(new_vsize) = old_fee + max(old_fee, incremental_relay_sat)
    incremental_relay_sat = ceil(new_vsize × 1 sat/vB)

The increment is computed via :func:`localwallet.tx.dust.
min_relay_fee_vbytes` from the replacement's size — never a hardcoded
constant (the dust/min-relay first-principles precedent). The floor is
shape-dependent (adding an input raises it, dropping the change output
lowers it), so every candidate shape is checked against its own floor.
Wallet transactions pay at least min-relay at build time (``psbt.py``
gate), so typically ``old_fee >= vsize`` and the fee-doubling branch
dominates; the increment branch binds only for originals parked exactly
at the relay floor.

Conflict and signaling are structural (BIP 125 rules 1 and 3): every plan
keeps ALL original inputs (the replacement always double-spends the
original), and the plan carries ``input_sequences`` asserted all-
:data:`~localwallet.tx.psbt.SEQUENCE_RBF_ENABLED` (0xfffffffd, ADR-0012
§5) — the RBF signal can never silently drop out of a bump.

CANDIDATE SHAPES (deterministic; first success wins)
----------------------------------------------------
Order follows the funding rule "change first, the chosen coin second":

1. ``change_trim`` — same inputs, recipient outputs verbatim, change
   reduced to ``inputs_total - recipients_total - new_fee`` where
   ``new_fee = ceil(vsize × rate)`` (integer-exact
   :func:`~localwallet.tx.selection.fee_sats_for`). Holds when the fee
   clears this shape's floor and the trimmed change stays at or above
   the change script's dust threshold (computed, never a constant).
2. ``change_fold`` — the rate cleared the floor but change would fall
   below dust, so the change output is REMOVED and the entire residue
   ``inputs_total - recipients_total`` becomes the fee (ADR-0012
   decision 2, Case B). Holds when that residue clears the smaller
   changeless shape's floor AND is at least the rate target for that
   shape (the plan never under-bids the chosen rate). Folding is never
   a way around a below-floor rate: shape 2 is gated on shape 1's rate
   clearing the floor.
3. ``add_input`` — when change alone produced no plan and a funding coin
   was provided, shapes 1–2 are retried with the coin appended (change
   removed if it would go below dust). An original with no change
   output at all (a folded send) has exactly one coin shape: the whole
   added value becomes fee, still gated on the rate target and floor.

Refusal is :class:`RbfFloorError` with a machine-readable reason and the
floor number:

* ``rate_below_floor`` — the chosen rate's fee is below the BIP-125
  floor even though the funding could reach it (ticket item 4).
* ``funding_below_floor`` — even folding the whole chosen coin into the
  fee cannot reach the floor (ticket item 2c).
* ``rate_exceeds_funding`` — the rate clears the floor but no honest
  plan exists: the rate target is more than change + coin can pay
  (never silently under-bid the user's rate).

The error's sats fields follow the :class:`~localwallet.tx.selection.
InsufficientFundsError` precedent (ADR-0012 §7): chat-renderable UI text,
never a logging context. All other messages here stay value-free.

Determinism: pure integer arithmetic, canonical input order ``(txid,
vout)`` ascending (the same rule ``build_unsigned_psbt`` applies), fixed
candidate order, no randomness, no clock, no network. Same inputs →
field-for-field identical plan.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from localwallet.tx.dust import (
    TxEngineError,
    dust_threshold,
    min_relay_fee_vbytes,
    serialized_output_size,
)
from localwallet.tx.psbt import SEQUENCE_RBF_ENABLED
from localwallet.tx.selection import (
    _MAX_FEE_RATE_CENTISAT_VB,
    SelectionError,
    estimate_tx_vsize,
    fee_sats_for,
)
from localwallet.tx.selection import (
    _utxo_sort_key as _validate_coin,
)

__all__ = [
    "OriginalTx",
    "RbfFloorError",
    "RbfRefusalReason",
    "RbfReplacementMode",
    "ReplacementError",
    "ReplacementPlan",
    "build_replacement_plan",
    "rbf_min_fee_sats",
]

#: Consensus sanity bound (mirrors selection.py/psbt.py — a bounds value,
#: not a policy constant).
_MAX_MONEY_SATS = 2_100_000_000_000_000
_MAX_INPUTS = 1000  # same bound as the PSBT builder
_MAX_RECIPIENTS = 100

#: Core default incremental relay rate, sat/vB (BIP 125 rule 2 refers to
#: the node's own minrelaytxfee; the tx layer takes it as plain data,
#: exactly like the dust module does).
_INCREMENTAL_RELAY_SAT_VB = 1


class ReplacementError(TxEngineError):
    """A replacement request or its recorded original is invalid (value-free)."""


class RbfRefusalReason(StrEnum):
    """Machine-readable cause carried by every :class:`RbfFloorError`."""

    RATE_BELOW_FLOOR = "rate_below_floor"
    FUNDING_BELOW_FLOOR = "funding_below_floor"
    RATE_EXCEEDS_FUNDING = "rate_exceeds_funding"


class RbfReplacementMode(StrEnum):
    """Which shape the builder produced (caller narrates it on the plan card).

    ``add_input`` with ``change_sats is None`` means the chosen coin went
    entirely into the fee (the trimmed change fell below dust, or the
    original had no change output at all).
    """

    CHANGE_TRIM = "change_trim"
    CHANGE_FOLD = "change_fold"
    ADD_INPUT = "add_input"


class RbfFloorError(ReplacementError):
    """The bump cannot produce a BIP-125-honest plan at this funding/rate.

    Deliberately carries ``floor_sats``/``max_payable_sats`` in the
    message, following the :class:`~localwallet.tx.selection.
    InsufficientFundsError` precedent (ADR-0012 §7): this error is
    rendered in chat as user-facing UI ("the minimum bump is X sats"),
    never placed in a logging context. ``reason`` is the machine-readable
    cause for the handler's branching narration.
    """

    def __init__(
        self, reason: RbfRefusalReason, floor_sats: int, max_payable_sats: int
    ) -> None:
        self.reason = reason
        self.floor_sats = floor_sats
        self.max_payable_sats = max_payable_sats
        super().__init__(
            f"replacement refused ({reason.value}): BIP-125 floor is "
            f"{floor_sats} sats, chosen funding pays at most "
            f"{max_payable_sats} sats"
        )


@dataclass(frozen=True, slots=True)
class OriginalTx:
    """The recorded original being replaced — plain data, verbatim.

    Decomposed by the caller from the stored/broadcast transaction
    (PendingTx fields + PSBT contents): ``inputs`` are coin records
    duck-typed exactly like :func:`localwallet.tx.selection.select_coins`
    (``txid`` hex str, ``vout`` int, ``value_sats`` positive int);
    ``recipients`` are the payment outputs ``(script, value_sats)`` in
    their exact on-chain order; ``change_script``/``change_sats`` are the
    change output (both or neither — a folded original has none);
    ``fee_sats`` and ``vsize`` are the recorded values. The builder
    re-verifies conservation and vsize against its own integer accounting
    and refuses a record that does not add up (fail closed on a corrupt
    decomposition — these fields drive money math).
    """

    inputs: Sequence[Any]
    recipients: Sequence[tuple[bytes, int]]
    change_script: bytes | None
    change_sats: int | None
    fee_sats: int
    vsize: int


@dataclass(frozen=True, slots=True)
class ReplacementPlan:
    """A deterministic replacement shape — inputs, outputs, fee, vsize, rate.

    ``inputs`` are the coin objects passed through verbatim in canonical
    ``(txid, vout)`` ascending order (the PSBT builder's order);
    ``outputs`` are ``(script, value_sats)`` with recipients verbatim and
    the change output last, if kept. ``fee_rate_centisat_vb`` is the
    *bid* rate (TCK-FEE-003 units); on fold paths ``fee_sats`` exceeds
    that bid (the whole residue became the fee — ADR-0012 Case B), never
    below it. ``funding_coin`` echoes the chosen coin (``None`` on the
    change-only paths). ``input_sequences`` is one
    ``SEQUENCE_RBF_ENABLED`` per input — asserted, not merely documented.
    """

    mode: RbfReplacementMode
    inputs: tuple[Any, ...]
    outputs: tuple[tuple[bytes, int], ...]
    change_sats: int | None
    fee_sats: int
    vsize: int
    fee_rate_centisat_vb: int
    funding_coin: Any | None
    input_sequences: tuple[int, ...]


def _check_int(value: Any, name: str, lo: int, hi: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ReplacementError(f"{name} must be an integer")
    if not lo <= value <= hi:
        raise ReplacementError(f"{name} must be between {lo} and {hi}")
    return value


def rbf_min_fee_sats(old_fee_sats: int, new_vsize: int) -> int:
    """BIP-125 replacement floor for a given old fee and new-shape vsize.

    ``floor = old_fee + max(old_fee, ceil(new_vsize × 1 sat/vB))`` — the
    BIP 125 rule that a replacement must pay more than the original by at
    least the incremental relay fee of its own size (Core's default
    incremental relay rate = 1 sat/vB). The increment comes from
    :func:`~localwallet.tx.dust.min_relay_fee_vbytes` (computed from
    size, bounds-checked, never a hardcoded number). Integer-exact.

    Raises:
        ReplacementError: non-integer arguments or out-of-range values
            (value-free message).
    """
    old_fee_sats = _check_int(old_fee_sats, "old_fee_sats", 1, _MAX_MONEY_SATS)
    try:
        increment = min_relay_fee_vbytes(
            new_vsize, min_relay_sat_vb=_INCREMENTAL_RELAY_SAT_VB
        )
    except (TypeError, ValueError) as exc:
        raise ReplacementError("new_vsize must be an integer within standardness bounds") from exc
    return old_fee_sats + max(old_fee_sats, increment)


def _coin_key(coin: Any) -> tuple[str, int]:
    """Validate one duck-typed coin (selection's contract) -> outpoint key."""
    try:
        _validate_coin(coin)
    except SelectionError as exc:
        raise ReplacementError(f"coin is invalid: {exc}") from exc
    return (coin.txid.lower(), coin.vout)


def _make_plan(
    mode: RbfReplacementMode,
    coins: tuple[Any, ...],
    recipients: tuple[tuple[bytes, int], ...],
    change_script: bytes | None,
    change_sats: int | None,
    fee_sats: int,
    vsize: int,
    rate_c: int,
    funding_coin: Any | None,
) -> ReplacementPlan:
    ordered = tuple(sorted(coins, key=lambda c: (c.txid.lower(), c.vout)))
    outputs = recipients
    if change_sats is not None:
        outputs = outputs + ((change_script, change_sats),)
    sequences = (SEQUENCE_RBF_ENABLED,) * len(ordered)
    # Tripwires at the money-path boundary (assert = a bug HERE is found
    # now, like selection.py/psbt.py conservation): the RBF signal must be
    # on every input, and the inputs must fund outputs + fee exactly.
    assert all(s == SEQUENCE_RBF_ENABLED for s in sequences), (
        "replacement plan lost RBF signaling"
    )
    assert sum(c.value_sats for c in ordered) == (
        sum(v for _script, v in outputs) + fee_sats
    ), "replacement conservation invariant violated"
    return ReplacementPlan(
        mode=mode,
        inputs=ordered,
        outputs=outputs,
        change_sats=change_sats,
        fee_sats=fee_sats,
        vsize=vsize,
        fee_rate_centisat_vb=rate_c,
        funding_coin=funding_coin,
        input_sequences=sequences,
    )


def build_replacement_plan(
    original: OriginalTx,
    fee_rate_centisat_vb: int,
    *,
    funding_coin: Any | None = None,
) -> ReplacementPlan:
    """Build the BIP-125 replacement plan for a recorded original.

    Pure and deterministic — see the module docstring for the floor rule,
    the candidate shapes and their order (change first, chosen coin
    second). Recipient output values are preserved verbatim: a fee bump
    may only touch inputs, the change output, and the fee.

    Args:
        original: The recorded original (:class:`OriginalTx`). Re-verified
            fail-closed: coin shapes, duplicate outpoints, spendable
            recipient scripts, change pair + dust viability, integer fee,
            conservation ``inputs_total == recipients + change + fee``,
            and the recorded vsize against this module's own integer
            accounting.
        fee_rate_centisat_vb: The user/chosen target rate, integer
            centisat/vB (1..1_000_000, TCK-FEE-003 units).
        funding_coin: The chosen extra coin (same duck-type as
            ``original.inputs`` members), or ``None`` for the change-only
            paths. Its outpoint must not already be an input. Policy
            (which coin to offer) belongs to the caller — the chosen coin
            is used as given.

    Returns:
        A :class:`ReplacementPlan` with ``fee_sats >= rbf_min_fee_sats``
        for its own shape and ``SEQUENCE_RBF_ENABLED`` on every input.

    Raises:
        ReplacementError: structurally invalid arguments or a recorded
            original that does not add up (value-free messages).
        RbfFloorError: no honest plan exists at this funding/rate
            (reason + floor number, ADR-0012 §7 amount-field precedent).
    """
    if not isinstance(original, OriginalTx):
        raise ReplacementError("original must be an OriginalTx record")
    rate_c = _check_int(
        fee_rate_centisat_vb, "fee_rate_centisat_vb", 1, _MAX_FEE_RATE_CENTISAT_VB
    )

    # ---- recorded original: inputs -------------------------------------
    if (
        not isinstance(original.inputs, (list, tuple))
        or not 1 <= len(original.inputs) <= _MAX_INPUTS
    ):
        raise ReplacementError("original inputs must be a 1..1000-item sequence")
    keys: set[tuple[str, int]] = set()
    for coin in original.inputs:
        key = _coin_key(coin)
        if key in keys:
            raise ReplacementError("duplicate outpoint in the recorded original")
        keys.add(key)
    inputs_total = sum(coin.value_sats for coin in original.inputs)

    # ---- recorded original: recipients ---------------------------------
    if (
        not isinstance(original.recipients, (list, tuple))
        or not 1 <= len(original.recipients) <= _MAX_RECIPIENTS
    ):
        raise ReplacementError("original recipients must be a 1..100-item sequence")
    recipients: list[tuple[bytes, int]] = []
    for pair in original.recipients:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise ReplacementError("each recipient must be a (script, value_sats) pair")
        raw_script, value = pair
        if not isinstance(raw_script, (bytes, bytearray, memoryview)):
            raise ReplacementError("recipient script must be bytes")
        script = bytes(raw_script)
        if not script or len(script) > 10_000 or script[0] == 0x6A:
            raise ReplacementError("recipient script is empty, oversized or unspendable")
        value = _check_int(value, "recipient value_sats", 1, _MAX_MONEY_SATS)
        recipients.append((script, value))
    recipient_scripts = [script for script, _v in recipients]
    recipients_total = sum(value for _s, value in recipients)

    # ---- recorded original: change pair -------------------------------
    if (original.change_script is None) != (original.change_sats is None):
        raise ReplacementError("change_script and change_sats must be given together")
    change_script: bytes | None = None
    change_cost = 0
    change_dust = 0
    if original.change_script is not None:
        if not isinstance(original.change_script, (bytes, bytearray, memoryview)):
            raise ReplacementError("change script must be bytes")
        change_script = bytes(original.change_script)
        if not change_script or len(change_script) > 10_000:
            raise ReplacementError("change script is empty or oversized")
        # Caller-derived sizes first (raises ValueError on junk scripts):
        change_cost = serialized_output_size(change_script)
        change_dust = dust_threshold(change_script)
        _check_int(original.change_sats, "change_sats", 1, _MAX_MONEY_SATS)
        if original.change_sats < change_dust:
            raise ReplacementError("recorded change is below its dust threshold")

    # ---- recorded original: fee, vsize, conservation ------------------
    old_fee = _check_int(original.fee_sats, "fee_sats", 1, _MAX_MONEY_SATS)
    _check_int(original.vsize, "vsize", 1, 100_000)
    if inputs_total - recipients_total - (original.change_sats or 0) != old_fee:
        raise ReplacementError(
            "recorded original does not conserve: inputs != recipients + change + fee"
        )
    expected_vsize = estimate_tx_vsize(
        len(original.inputs),
        recipient_scripts,
        change_cost_vbytes=change_cost if change_script is not None else None,
    )
    if expected_vsize != original.vsize:
        raise ReplacementError("recorded vsize does not match its transaction shape")

    # ---- funding coin --------------------------------------------------
    if funding_coin is not None:
        funding_key = _coin_key(funding_coin)
        if funding_key in keys:
            raise ReplacementError("funding coin is already an input of the original")
        if inputs_total + funding_coin.value_sats > _MAX_MONEY_SATS:
            raise ReplacementError("input values exceed the money supply bound")

    original_inputs = tuple(original.inputs)
    recipients = tuple(recipients)
    has_change = original.change_sats is not None

    # ---- candidate shapes: no-coin (trim, fold), then coin (trim, fold)
    candidates: list[tuple[bool, tuple[Any, ...], int]] = [(False, original_inputs, inputs_total)]
    if funding_coin is not None:
        candidates.append(
            (True, original_inputs + (funding_coin,), inputs_total + funding_coin.value_sats)
        )

    # Last-candidate numbers, reused for the refusal report below.
    payable = 0
    floor_f = 0
    rate_floor_ok = False
    floor_report = 0
    for use_coin, coins, total in candidates:
        funding = funding_coin if use_coin else None
        n_in = len(coins)
        payable = total - recipients_total
        # Changeless shape (also the only shape when the original has none).
        vsize_f = estimate_tx_vsize(n_in, recipient_scripts, change_cost_vbytes=None)
        fee_at_f = fee_sats_for(vsize_f, rate_c)
        floor_f = rbf_min_fee_sats(old_fee, vsize_f)
        if has_change:
            vsize_k = estimate_tx_vsize(
                n_in, recipient_scripts, change_cost_vbytes=change_cost
            )
            fee_k = fee_sats_for(vsize_k, rate_c)
            floor_k = rbf_min_fee_sats(old_fee, vsize_k)
            floor_report = floor_k
            rate_floor_ok = fee_k >= floor_k
            # Shape 1: change_trim — pay the chosen rate, trimmed change stays viable.
            change_k = payable - fee_k
            if rate_floor_ok and change_k >= change_dust:
                return _make_plan(
                    RbfReplacementMode.CHANGE_TRIM if not use_coin
                    else RbfReplacementMode.ADD_INPUT,
                    coins, recipients, change_script, change_k,
                    fee_k, vsize_k, rate_c, funding,
                )
            # Shape 2: change_fold — dust-below change becomes the fee
            # (ADR-0012 Case B). Only allowed when the rate cleared the
            # floor: folding must never launder a below-floor rate.
            fold_allowed = rate_floor_ok
        else:
            floor_report = floor_f
            rate_floor_ok = fee_at_f >= floor_f
            fold_allowed = True
        fee_f = payable
        if fold_allowed and fee_f >= floor_f and fee_f >= fee_at_f:
            return _make_plan(
                RbfReplacementMode.CHANGE_FOLD if not use_coin
                else RbfReplacementMode.ADD_INPUT,
                coins, recipients, change_script, None,
                fee_f, vsize_f, rate_c, funding,
            )

    # ---- refusal (after the LAST candidate = the most-funded shape) ----
    if payable < floor_f:
        reason = RbfRefusalReason.FUNDING_BELOW_FLOOR  # even the coin can't reach the floor
        floor_report = floor_f
    elif rate_floor_ok:
        reason = RbfRefusalReason.RATE_EXCEEDS_FUNDING  # rate clears the floor but pays more than exists
    else:
        reason = RbfRefusalReason.RATE_BELOW_FLOOR  # the chosen rate is below the BIP-125 floor
    raise RbfFloorError(reason, floor_report, payable)

"""Child-pays-for-parent plans for a stuck INBOUND transaction (TCK-CPFP-001).

Pure tx-layer money math — the core the cpfp conversation (TCK-CPFP-002)
rides on. No I/O: no store, no chain, no PSBT serialization. The inputs are
duck-typed coin records (the same contract
:func:`localwallet.tx.selection.select_coins` validates: ``txid`` hex str,
``vout`` int, ``value_sats`` positive int — an UNCONFIRMED inbound coin is
expected here; spending it is the whole point), a :class:`StuckParent`
record, the wallet's own fresh destination script, and an integer target
rate in centisat/vB (TCK-FEE-003 units; the handler resolves the FAST rung
or an explicit rate and does the ×100 at its edge). Style/contract model:
:mod:`localwallet.tx.replacement` (TCK-RBF-002). (Module choice: a NEW
``tx/`` module — replacement answers "re-shape MY in-flight tx under
BIP-125"; this answers "unstick SOMEONE ELSE'S unconfirmed payment to me by
out-bidding its fee with a child I own". Layering: ``dust`` ← ``selection``
← ``psbt`` ← ``cpfp``.)

THE CHILD-PAYS-FOR-PARENT BOUND
-------------------------------
A CPFP child raises the PACKAGE fee rate: what the child must pay is bounded
below by what the PARENT still needs, not just by the child's own size::

    child_fee >= max( ceil(child_vsize           × rate),   # self cost
                     ceil((child_vsize+parent_vsize) × rate) - parent_fee )

When the parent's fee and vsize are known (a lineage row this wallet
broadcast, store v3), the plan pays that max — integer-exact via
:func:`~localwallet.tx.selection.fee_sats_for`, so the chosen rate is a
true floor for the package.

When the parent is a FOREIGN transaction (the normal watch-only inbound
case: a scanner sees the output, not the input side), its fee is unknowable
— and this builder NEVER FABRICATES one. The child then bids the chosen
rate for its own vsize only, and the plan record states the bound honestly:
``parent_fee_known=False`` and ``package_fee_rate_centisat_vb=None`` — no
effective-package-rate claim is made, because a parent that paid below the
chosen rate drags the package average below it (the reorg/underpay hedge is
CPFP-002's card copy, sourced from these fields).

Shapes (USER SPEC, ADR-0002 cpfp amendment): the child spends the
unconfirmed inbound coin — optionally a second own coin MERGED in (the
``merge_coin`` envelope flag resolves to this argument handler-side; which
coin is a deterministic handler policy, never a model choice) — and pays the
wallet's own fresh receive address in ONE output (no change: the child
consolidates what it spends). Fee/dust discipline: the child alone must pay
at least :func:`~localwallet.tx.dust.min_relay_fee_vbytes` (so it relays
even without package-relay support), the single output must clear
:func:`~localwallet.tx.dust.dust_threshold` computed from its script size
(never a constant), and every input carries the wallet's standing
:data:`~localwallet.tx.psbt.SEQUENCE_RBF_ENABLED` policy (ADR-0012 §5) so
the child itself stays bumpable by ``bump_fee``/this builder later.

Refusal is :class:`CpfpError` with a machine-readable
:class:`CpfpRefusalReason` and a VALUE-FREE message (no sats, no rates):

* ``rate_below_min_relay`` — the chosen rate's child fee is under the
  size-derived min-relay floor.
* ``fee_exceeds_funds`` — the bounded fee (incl. the parent top-up when the
  parent is known) exceeds the total value of the coins it would spend.
* ``output_below_dust`` — what remains for the fresh output is under its
  script-size-derived dust threshold.

Determinism: pure integer arithmetic, canonical input order ``(txid,
vout)`` ascending (the same rule ``build_unsigned_psbt`` and
:mod:`~localwallet.tx.replacement` apply), no randomness, no clock, no
network. Same inputs → field-for-field identical plan.
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
    "CpfpChildPlan",
    "CpfpError",
    "CpfpRefusalReason",
    "StuckParent",
    "build_cpfp_child_plan",
]

#: Consensus sanity bound (mirrors selection.py/psbt.py/replacement.py — a
#: bounds value, not a policy constant).
_MAX_MONEY_SATS = 2_100_000_000_000_000

#: Lower/upper vsize bound accepted for a recorded parent (Core
#: standardness, the same ceiling the dust module pins).
_MAX_VSIZE = 100_000

_TXID_HEX_CHARS = 64


class CpfpError(TxEngineError):
    """A cpfp request or its parent record is invalid (value-free message).

    ``reason`` carries the machine-readable cause for fee-math refusals
    (:class:`CpfpRefusalReason`); structural refusals leave it ``None``.
    The handler branches on ``reason``; the message itself never quotes a
    value (the ticket pin: fee-bound refusals are value-free).
    """

    def __init__(self, message: str, reason: CpfpRefusalReason | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class CpfpRefusalReason(StrEnum):
    """Machine-readable cause carried by every fee-math :class:`CpfpError`."""

    RATE_BELOW_MIN_RELAY = "rate_below_min_relay"
    FEE_EXCEEDS_FUNDS = "fee_exceeds_funds"
    OUTPUT_BELOW_DUST = "output_below_dust"


@dataclass(frozen=True, slots=True)
class StuckParent:
    """The unconfirmed parent whose output the child spends — plain data.

    ``txid``: the parent's id, EXACTLY 64 lowercase hex characters (the
    store/lineage convention, fail-closed strict charset — the inbound
    coin's ``txid`` must equal it: a mismatched pair is a corrupt record,
    not a plan). ``fee_sats``/``vsize``: the recorded parent's numbers, or
    ``None`` when unknowable (a foreign inbound — watch-only sees outputs,
    not inputs). Both-or-neither: half a fee picture cannot bound anything,
    so a mix is refused.
    """

    txid: str
    fee_sats: int | None
    vsize: int | None


@dataclass(frozen=True, slots=True)
class CpfpChildPlan:
    """A deterministic child-pays-for-parent shape.

    ``inputs`` are the coin objects passed through verbatim in canonical
    ``(txid, vout)`` ascending order; ``outputs`` is exactly one
    ``(destination_script, output_sats)`` pair (the wallet's own fresh
    receive script — the handler derives it, the model never authors it).
    ``fee_rate_centisat_vb`` is the *bid* rate (TCK-FEE-003 units);
    ``fee_sats`` is that bid applied per THE CHILD-PAYS-FOR-PARENT BOUND
    above (it may exceed the child-only self cost when the parent's known
    fee fell short of the rate — never below it). ``parent_fee_known``
    states whether the package picture was complete;
    ``package_fee_rate_centisat_vb`` is the honest FLOOR of the combined
    effective rate — ``(parent_fee + child_fee) × 100 // (parent_vsize +
    child_vsize)``, integer-divided DOWN — or ``None`` when either parent
    number was unknown (no fabricated package claim).
    ``input_sequences`` is one ``SEQUENCE_RBF_ENABLED`` per input — the
    wallet's standing ADR-0012 §5 policy, asserted not merely documented.
    """

    inputs: tuple[Any, ...]
    outputs: tuple[tuple[bytes, int], ...]
    output_sats: int
    fee_sats: int
    vsize: int
    fee_rate_centisat_vb: int
    merged: bool
    parent_fee_known: bool
    package_fee_rate_centisat_vb: int | None
    input_sequences: tuple[int, ...]


def _check_int(value: Any, name: str, lo: int, hi: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CpfpError(f"{name} must be an integer")
    if not lo <= value <= hi:
        raise CpfpError(f"{name} must be between {lo} and {hi}")
    return value


def _refuse(reason: CpfpRefusalReason, why: str) -> CpfpError:
    return CpfpError(f"cpfp child refused ({reason.value}): {why}", reason)


def build_cpfp_child_plan(
    parent: StuckParent,
    inbound_coin: Any,
    destination_script: bytes,
    fee_rate_centisat_vb: int,
    *,
    merge_coin: Any | None = None,
) -> CpfpChildPlan:
    """Build the child-pays-for-parent plan for a stuck inbound payment.

    Pure and deterministic — see the module docstring for the bound, the
    shape and the refusals.

    Args:
        parent: The :class:`StuckParent` record (txid + fee picture, the
            latter optional but all-or-nothing).
        inbound_coin: The UNCONFIRMED inbound coin to spend (duck-typed per
            ``select_coins``); its ``txid`` must be the parent's.
        destination_script: scriptPubKey of a FRESH own receive address —
            caller-derived (store + derivation), validated only structurally
            here (bytes, spendable, dust from its own size).
        fee_rate_centisat_vb: Integer centisat/vB target (1..1_000_000; the
            handler resolves the FAST rung or the user's explicit rate and
            converts at its edge — the only place that sees centisat).
        merge_coin: Optional second own coin merged into the child (the
            ``merge_coin`` flag's resolved coin). Policy (which coin,
            confirmedness) belongs to the caller — used as given.

    Raises:
        CpfpError: structurally invalid arguments (value-free), or a fee
            bound that no honest plan satisfies
            (:class:`CpfpRefusalReason`).
    """
    # ---- parent record -------------------------------------------------
    if not isinstance(parent, StuckParent):
        raise CpfpError("parent must be a StuckParent record")
    if (
        not isinstance(parent.txid, str)
        or len(parent.txid) != _TXID_HEX_CHARS
        or not all(c in "0123456789abcdef" for c in parent.txid)
    ):
        raise CpfpError("parent txid must be exactly 64 lowercase hex characters")
    if (parent.fee_sats is None) != (parent.vsize is None):
        raise CpfpError("parent fee_sats and vsize must be known together or not at all")
    parent_fee: int | None = None
    parent_vsize: int | None = None
    if parent.fee_sats is not None:
        parent_fee = _check_int(parent.fee_sats, "parent fee_sats", 1, _MAX_MONEY_SATS)
        parent_vsize = _check_int(parent.vsize, "parent vsize", 1, _MAX_VSIZE)

    rate_c = _check_int(
        fee_rate_centisat_vb, "fee_rate_centisat_vb", 1, _MAX_FEE_RATE_CENTISAT_VB
    )

    # ---- coins ----------------------------------------------------------
    if not isinstance(destination_script, (bytes, bytearray, memoryview)):
        raise CpfpError("destination script must be bytes")
    script = bytes(destination_script)
    if not script or len(script) > 10_000 or script[0] == 0x6A:
        raise CpfpError("destination script is empty, oversized or unspendable")

    coins: Sequence[Any] = (inbound_coin,) if merge_coin is None else (inbound_coin, merge_coin)
    keys: set[tuple[str, int]] = set()
    for coin in coins:
        try:
            _validate_coin(coin)
        except SelectionError as exc:
            raise CpfpError(f"coin is invalid: {exc}") from exc
        key = (coin.txid.lower(), coin.vout)
        if key in keys:
            raise CpfpError("duplicate outpoint across the child's coins")
        keys.add(key)
    if inbound_coin.txid.lower() != parent.txid:
        raise CpfpError("inbound coin is not an output of the recorded parent")

    inputs_total = sum(coin.value_sats for coin in coins)
    if inputs_total > _MAX_MONEY_SATS:
        raise CpfpError("input values exceed the money supply bound")

    # ---- the bounded fee --------------------------------------------------
    vsize = estimate_tx_vsize(len(coins), [script], change_cost_vbytes=None)
    fee_self = fee_sats_for(vsize, rate_c)
    if fee_self < min_relay_fee_vbytes(vsize):
        raise _refuse(
            CpfpRefusalReason.RATE_BELOW_MIN_RELAY,
            "the bid rate is under the size-derived min-relay floor",
        )
    fee_sats = fee_self
    if parent_fee is not None and parent_vsize is not None:
        # The parent's shortfall, at the same bid rate, tops up the child.
        package_need = fee_sats_for(vsize + parent_vsize, rate_c) - parent_fee
        fee_sats = max(fee_self, package_need)
    if fee_sats > inputs_total:
        raise _refuse(
            CpfpRefusalReason.FEE_EXCEEDS_FUNDS,
            "the bounded child fee exceeds the value of the coins it would spend",
        )
    output_sats = inputs_total - fee_sats
    if output_sats < dust_threshold(script):
        raise _refuse(
            CpfpRefusalReason.OUTPUT_BELOW_DUST,
            "the residue for the fresh output is under its script-size dust floor",
        )

    # ---- plan -------------------------------------------------------------
    ordered = tuple(sorted(coins, key=lambda c: (c.txid.lower(), c.vout)))
    sequences = (SEQUENCE_RBF_ENABLED,) * len(ordered)
    # Tripwires at the money-path boundary (the replacement.py precedent):
    # RBF policy on every input, and conservation to the sat.
    assert all(s == SEQUENCE_RBF_ENABLED for s in sequences), (
        "cpfp child plan lost RBF signaling"
    )
    assert inputs_total == output_sats + fee_sats, (
        "cpfp child conservation invariant violated"
    )
    package_rate: int | None = None
    if parent_fee is not None and parent_vsize is not None:
        # Honest FLOOR of the effective package rate (integer-divided down).
        package_rate = (parent_fee + fee_sats) * 100 // (parent_vsize + vsize)
    return CpfpChildPlan(
        inputs=ordered,
        outputs=((script, output_sats),),
        output_sats=output_sats,
        fee_sats=fee_sats,
        vsize=vsize,
        fee_rate_centisat_vb=rate_c,
        merged=merge_coin is not None,
        parent_fee_known=parent_fee is not None,
        package_fee_rate_centisat_vb=package_rate,
        input_sequences=sequences,
    )

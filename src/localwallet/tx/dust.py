"""Dust thresholds and min-relay fee floor, computed from first principles.

Everything in this module is derived from Bitcoin Core's policy code, not
from memorized magic constants. The formula path below contains no literal
``546``/``294``-style dust numbers; the canonical constants fall out of the
arithmetic and are asserted in :mod:`tests.test_tx_dust`.

Dust: Bitcoin Core ``GetDustThreshold`` (``src/policy/policy.cpp``, v28.0)
-------------------------------------------------------------------------
Core defines an output as *dust* when spending it would cost more in fees
than the output carries::

    nSize = serialized_output_size                       # value(8) + varint(len) + script
    if script is unspendable:        return 0
    elif script is a witness program: nSize += 32 + 4 + 1 + (107 // 4) + 4   # +67
    else:                             nSize += 32 + 4 + 1 + 107 + 4        # +148
    threshold = dustRelayFee.GetFee(nSize)               # sat/kvB arithmetic

The two spend-cost branches are the *input* cost of spending the output
later (outpoint 32 + nSequence 4 + scriptSig-length varint 1, plus the
estimated satisfaction):

- **Legacy (+148):** a typical P2PKH scriptSig of at most 107 bytes
  (push ~72-byte signature item + push 33-byte pubkey item).
- **Segwit (+67):** the same outpoint/sequence/varint bytes plus
  ``107 // WITNESS_SCALE_FACTOR = 26`` — the witness items (sig item 73 =
  1 length byte + 72-byte signature content incl. hashtype; pubkey item 34
  = 1 + 33) at the 75% witness discount, integer-divided. Note the 1-byte
  witness stack-count varint is (deliberately, per Core) not counted.
  This cost is used for **every** witness program: taproot key-path
  spends are cheaper, but Core kept the P2WPKH-level estimate "to not
  further reduce the dust level" (PR #22779).

``dustRelayFee`` defaults to 3000 sat/kvB (= 3 sat/vB,
``DUST_RELAY_TX_FEE`` in ``src/policy/policy.h``). ``CFeeRate::GetFee``
performs ``nSize * sat_per_kvB / 1000`` in exact integer arithmetic; for
whole-sat/vB rates ``r`` the product ``r * nSize`` is exact — no rounding
residue — so this module computes ``threshold = r * nSize`` with pure
integer math (money is never float in this codebase).

Canonical thresholds at the default rate of 3 sat/vB (asserted in tests):

=============  ===========  =========  =====  ========
script type    script (B)   out ser.   cost   dust (sat)
=============  ===========  =========  =====  ========
P2PKH          25           34         148    546
P2SH (incl.    23           32         148    540
  P2SH-P2WPKH
  outputs)
P2WPKH         22           31         67     294
P2WSH / P2TR   34           43         67     330
OP_RETURN      any          any        —      0
=============  ===========  =========  =====  ========

(Sometimes-quoted "P2SH-P2WPKH dust = 360" does not follow from Core's
formula: the *output* script of a nested-segwit output is a plain P2SH
script, which is not a witness program, so the legacy +148 branch applies.
See ADR-0012 for the full note.)

Min-relay floor
---------------
``min_relay_fee_vbytes`` mirrors Core's default ``minrelaytxfee`` of
1000 sat/kvB = 1 sat/vB: a transaction paying less is not relayed. The
floor is ``max(1, vsize * rate)`` with the vsize upper bound set to Core's
``MAX_STANDARD_TX_WEIGHT`` (400_000 WU = 100_000 vB).

Watch-only / value-free invariants: pure integer math, no I/O, no logging;
errors are value-free (never echo script contents). This module also hosts
:class:`TxEngineError`, the base class of the ``tx`` subsystem error
hierarchy (layering: ``dust`` ← ``selection`` ← ``psbt``).
"""

from __future__ import annotations

__all__ = [
    "TxEngineError",
    "dust_threshold",
    "min_relay_fee_vbytes",
    "script_is_witness_program",
    "serialized_output_size",
    "varint_size",
]

#: Bitcoin Core ``WITNESS_SCALE_FACTOR``.
_WITNESS_SCALE_FACTOR = 4

#: Bitcoin Core ``MAX_SCRIPT_SIZE`` — scripts larger than this are
#: unspendable by policy (``CScript::IsUnspendable``).
_MAX_SCRIPT_SIZE = 10_000

#: Core ``MAX_STANDARD_TX_WEIGHT`` in weight units; 400_000 WU = 100_000 vB.
_MAX_STANDARD_TX_WEIGHT_WU = 400_000

#: Largest vsize this module accepts as input (Core standardness bound).
_MAX_VBYTES = _MAX_STANDARD_TX_WEIGHT_WU // _WITNESS_SCALE_FACTOR

#: Largest fee rate accepted (sat/vB); 10_000 sat/vB = 1000x min-relay —
#: anything beyond that is caller error, refused (fail closed).
_MAX_RATE_SAT_VB = 10_000

# Spend-cost components (see module docstring for the Core citation).
_LEGACY_SATISFACTION_BYTES = 107  # ~72 B sig push item + 33 B pubkey push item
_SEGWIT_SATISFACTION_BYTES = 107  # same items, witness-serialized
_SEGWIT_INPUT_BASE_BYTES = 32 + 4 + 1 + 4  # outpoint + varint + nSequence
_LEGACY_SPEND_COST_BYTES = _SEGWIT_INPUT_BASE_BYTES + _LEGACY_SATISFACTION_BYTES  # 148
_SEGWIT_SPEND_COST_BYTES = _SEGWIT_INPUT_BASE_BYTES + (
    _SEGWIT_SATISFACTION_BYTES // _WITNESS_SCALE_FACTOR
)  # 67

_OP_RETURN = 0x6A
_OP_0 = 0x00
_OP_1 = 0x51
_OP_16 = 0x60


class TxEngineError(Exception):
    """Base class for all ``localwallet.tx`` errors.

    Message contract: internal errors are value-free (they never echo
    scripts, addresses, or amounts). The single documented exception is
    :class:`~localwallet.tx.selection.InsufficientFundsError`, whose
    needed/available amounts are user-facing UI text (see ADR-0012) and
    must stay out of any logging context.
    """


def varint_size(n: int) -> int:
    """Return the serialized size of Bitcoin Core's CompactSize ``n``.

    1 byte below 253, 3 bytes (``0xfd`` prefix) up to 65535, 5 bytes up to
    2**32-1, 9 bytes beyond.

    Raises:
        TypeError: ``n`` is not an integer. ValueError: negative or above
            2**64-1. Messages are value-free.
    """
    if not isinstance(n, int) or isinstance(n, bool):
        raise TypeError("varint size is undefined for non-integers")
    if n < 0:
        raise ValueError("varint does not encode negative values")
    if n < 0xFD:
        return 1
    if n <= 0xFFFF:
        return 3
    if n <= 0xFFFFFFFF:
        return 5
    if n <= 0xFFFFFFFFFFFFFFFF:
        return 9
    raise ValueError("varint does not encode values above 2**64-1")


def serialized_output_size(script: bytes) -> int:
    """Serialized size of a ``CTxOut`` body: value (8) + varint + script."""
    script = _validate_script(script)
    return 8 + varint_size(len(script)) + len(script)


def script_is_witness_program(script: bytes) -> bool:
    """True when ``script`` is a BIP141 witness program (Core semantics).

    Mirrors ``CScript::IsWitnessProgram``: 4..42 bytes, first byte is
    ``OP_0`` or ``OP_1``..``OP_16``, and the second byte (the pushed
    program length 2..40) matches the remaining size exactly.
    """
    script = _validate_script(script)
    if not 4 <= len(script) <= 42:
        return False
    if script[0] != _OP_0 and not _OP_1 <= script[0] <= _OP_16:
        return False
    return script[1] == len(script) - 2


def _is_unspendable(script: bytes) -> bool:
    """Core ``CScript::IsUnspendable``: OP_RETURN-prefixed or oversized."""
    return (len(script) > 0 and script[0] == _OP_RETURN) or len(
        script
    ) > _MAX_SCRIPT_SIZE


def _validate_script(script: bytes) -> bytes:
    if isinstance(script, (bytes, bytearray, memoryview)):
        return bytes(script)
    raise ValueError("script must be bytes")


def _validate_rate(rate: int) -> int:
    if not isinstance(rate, int) or isinstance(rate, bool):
        raise TypeError("fee rate must be an integer (sat/vB)")
    if not 0 <= rate <= _MAX_RATE_SAT_VB:
        raise ValueError(
            f"fee rate must be between 0 and {_MAX_RATE_SAT_VB} sat/vB"
        )
    return rate


def dust_threshold(script: bytes, dust_relay_rate_sat_vb: int = 3) -> int:
    """Dust threshold in sats for one output of ``script`` — Core semantics.

    Implements Bitcoin Core ``GetDustThreshold`` (``src/policy/policy.cpp``,
    v28.0) exactly; see the module docstring for the full derivation. The
    threshold is ``rate * (serialized_output_size + spend_cost)`` where the
    spend cost is 67 vB for any witness program, 148 vB otherwise, and the
    threshold is 0 for unspendable (OP_RETURN / oversized) scripts.

    Args:
        script: The output scriptPubKey (raw bytes).
        dust_relay_rate_sat_vb: Dust relay fee in sat/vB (Core default
            ``DUST_RELAY_TX_FEE`` = 3000 sat/kvB = 3 sat/vB). Must be an
            integer 0..10000; whole-sat/vB rates reproduce Core's
            ``CFeeRate::GetFee`` arithmetic exactly.

    Returns:
        The dust threshold in sats. An output is dust when its value is
        strictly below this threshold (Core ``IsDust``:
        ``value < threshold``).

    Raises:
        TypeError: ``script`` is not bytes-like or the rate is not an
            integer. ValueError: the rate is out of range. Messages are
            value-free.
    """
    script = _validate_script(script)
    rate = _validate_rate(dust_relay_rate_sat_vb)
    if _is_unspendable(script):
        return 0
    size = serialized_output_size(script)
    if script_is_witness_program(script):
        size += _SEGWIT_SPEND_COST_BYTES
    else:
        size += _LEGACY_SPEND_COST_BYTES
    return rate * size


def min_relay_fee_vbytes(vsize: int, min_relay_sat_vb: int = 1) -> int:
    """Minimum relay fee in sats for a transaction of ``vsize`` vB.

    Core's default ``minrelaytxfee`` is 1000 sat/kvB = 1 sat/vB; a lower
    fee is not relayed. The floor is ``max(1, vsize * rate)`` — at least
    one sat even for degenerate sizes — with sane bounds: ``vsize`` is
    capped at Core's standardness limit (``MAX_STANDARD_TX_WEIGHT`` /
    4 = 100_000 vB) and the rate at 10_000 sat/vB.

    Raises:
        TypeError: non-integer/bool arguments. ValueError: out-of-range
            arguments (value-free messages).
    """
    if not isinstance(vsize, int) or isinstance(vsize, bool):
        raise TypeError("vsize must be an integer")
    if not 0 <= vsize <= _MAX_VBYTES:
        raise ValueError(f"vsize must be between 0 and {_MAX_VBYTES}")
    rate = _validate_rate(min_relay_sat_vb)
    return max(1, vsize * rate)

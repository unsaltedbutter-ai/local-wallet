"""Transaction subsystem: coin selection, PSBT, re-validation, broadcast glue.

Pure money-math and serialization — **no network I/O** (lint-enforced):
fee rates, dust-rate assumptions, and UTXOs arrive as plain data, so the
chain adapter (TCK-P2-001) is never imported here. Layering:
``dust`` ← ``selection`` ← ``psbt``; the base error class
:class:`TxEngineError` lives in :mod:`localwallet.tx.dust`.
"""

from localwallet.tx.dust import (
    TxEngineError,
    dust_threshold,
    min_relay_fee_vbytes,
    script_is_witness_program,
    serialized_output_size,
    varint_size,
)
from localwallet.tx.psbt import (
    SEQUENCE_RBF_ENABLED,
    PsbtError,
    PsbtInputSource,
    PsbtMeta,
    PsbtValidationError,
    build_unsigned_psbt,
    psbt_to_base64,
    validate_psbt_shape,
)
from localwallet.tx.selection import (
    P2WPKH_INPUT_WEIGHT_WU,
    InsufficientFundsError,
    SelectionError,
    SelectionResult,
    coin_partition,
    estimate_tx_vsize,
    select_coins,
)

__all__ = [
    "P2WPKH_INPUT_WEIGHT_WU",
    "SEQUENCE_RBF_ENABLED",
    "InsufficientFundsError",
    "PsbtError",
    "PsbtInputSource",
    "PsbtMeta",
    "PsbtValidationError",
    "SelectionError",
    "SelectionResult",
    "TxEngineError",
    # psbt
    "build_unsigned_psbt",
    # selection
    "coin_partition",
    # dust
    "dust_threshold",
    "estimate_tx_vsize",
    "min_relay_fee_vbytes",
    "psbt_to_base64",
    "script_is_witness_program",
    "select_coins",
    "serialized_output_size",
    "validate_psbt_shape",
    "varint_size",
]

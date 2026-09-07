"""Unsigned PSBT construction for the send flow (money path).

Builds a BIP174 PSBT for a *watch-only* single-sig P2WPKH wallet
(ADR-0008): the app holds public keys only, so the PSBT carries
``witness_utxo`` (BIP 174 field 01) and per-input ``bip32_derivations``
(BIP 174 field 02) and leaves signing entirely to the hardware wallet
(Phase 3). embit computes the child public keys from the account-level
watch key; the derivation paths are expressed from the wallet origin the
caller supplies (the same origin as the wallet descriptor, e.g.
``[fp/84'/1'/0']`` — the fingerprint convention follows ADR-0009/0010 and
descriptor.py: it is the account key's own fingerprint until device
registration supplies the master fingerprint per OQ18).

RBF policy (PROJECT.md N4/R6 — decided here, documented in ADR-0012)
-------------------------------------------------------------------
Every input is built with ``sequence = 0xfffffffd`` (BIP 125
"replaceable", opt-in signaled): Phase 5+ *may* add fee bumping without a
flag-day migration, and every wallet transaction behaves uniformly.
v1 implements **no bumping** (N4) — the fee UX must instead set
expectations that a low-fee transaction may sit unconfirmed (R6).
``0xfffffffe`` (RBF-disabled) and ``0xffffffff`` (final) are refused by
this module: v1 has no use for them and refusing keeps the policy
structural instead of conventional. Cross-ref: ADR-0012.

Outputs are emitted exactly as: recipients in the given order, then the
optional change output last. Input order is canonical
``(txid, vout)`` ascending — the same call always produces the same
unsigned transaction (and therefore the same txid).

Structural validation
---------------------
:func:`validate_psbt_shape` re-checks a built PSBT against its
:class:`PsbtMeta` (input count, output scripts and values in exact order,
witness UTXO presence, sequence policy, recomputed fee) — fail closed on
any mismatch. This is the pre-sign invariant that Phase 3's signed-PSBT
re-validation will extend (tampered-PSBT checks, PROJECT.md §7.5).

Invariants: integers only for sat math; watch-only (a private account key
is refused — zero secrets in PSBTs, process, or logs); no network I/O;
error messages are value-free (never echo scripts/addresses/amounts).
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from embit import hashes as _hashes
from embit.bip32 import HDKey
from embit.psbt import PSBT, DerivationPath
from embit.script import Script, address_to_scriptpubkey
from embit.transaction import Transaction, TransactionInput, TransactionOutput

from localwallet.tx.dust import (
    TxEngineError,
    dust_threshold,
    min_relay_fee_vbytes,
)
from localwallet.tx.selection import estimate_tx_vsize

__all__ = [
    "SEQUENCE_RBF_ENABLED",
    "PsbtError",
    "PsbtInputSource",
    "PsbtMeta",
    "PsbtValidationError",
    "build_unsigned_psbt",
    "psbt_to_base64",
    "validate_psbt_shape",
]

#: BIP 125 opt-in-replace-by-fee sequence for every input (N4 policy).
SEQUENCE_RBF_ENABLED = 0xFFFFFFFD

#: Refused sequence values (see module docstring).
_SEQUENCE_RBF_DISABLED = 0xFFFFFFFE
_SEQUENCE_FINAL = 0xFFFFFFFF

_TX_VERSION = 2
_MAX_LOCKTIME = 0xFFFFFFFF
_MAX_MONEY_SATS = 2_100_000_000_000_000

# Size bounds: v1 wallets are small (ADR-0010); 1000 inputs / 100 outputs
# keep the estimated weight comfortably inside Core's standardness limit
# (MAX_STANDARD_TX_WEIGHT) and the vsize helpers' sanity bounds. Full
# standardness enforcement before broadcast is Phase 3's re-validation.
_MAX_INPUTS = 1000
_MAX_RECIPIENTS = 100

# v1 spend policy (ADR-0008): inputs come from the wallet's own P2WPKH
# outputs only; change goes back to the wallet's change branch.
_P2WPKH_SCRIPT_LEN = 22


class PsbtError(TxEngineError):
    """The PSBT request or its inputs are invalid (value-free messages)."""


class PsbtValidationError(PsbtError):
    """A PSBT failed structural validation against its metadata."""


@dataclass(frozen=True, slots=True)
class PsbtInputSource:
    """One spendable UTXO with its wallet derivation coordinates.

    Plain data (never a Store object): ``txid`` is the hex transaction id
    (64 hex chars), ``script_pubkey`` the UTXO's output script, and
    ``branch``/``index`` the BIP44 coordinates under the account key
    (branch 0 = receive, 1 = change) used to recompute the child pubkey
    for the PSBT derivation fields.
    """

    txid: str
    vout: int
    value_sats: int
    script_pubkey: bytes
    branch: int
    index: int


@dataclass(frozen=True, slots=True)
class PsbtMeta:
    """Intended-transaction summary bound to a built PSBT.

    ``expected_outputs`` is the exact ordered list of ``(script, value)``
    the signed transaction must contain — Phase 3 re-validation compares
    the re-parsed signed PSBT against this. ``vsize`` uses the max-witness
    P2WPKH convention from :func:`~localwallet.tx.selection.estimate_tx_vsize`.
    """

    expected_outputs: tuple[tuple[bytes, int], ...]
    expected_fee_sats: int
    vsize: int
    inputs_count: int


def _validate_hex_txid(txid: str) -> bytes:
    if not isinstance(txid, str) or len(txid) != 64:
        raise PsbtError("utxo txid must be a 64-character hex string")
    try:
        raw = bytes.fromhex(txid)
    except ValueError as exc:
        raise PsbtError("utxo txid is not valid hex") from exc
    return bytes(reversed(raw))  # embit txid byte order


def _validate_p2wpkh_script(script: bytes, what: str) -> bytes:
    if len(script) != _P2WPKH_SCRIPT_LEN or not (
        script[:2] == b"\x00\x14"
    ):
        raise PsbtError(f"{what} must be a P2WPKH witness program")
    return script


def _recipient_from_pair(pair: Any) -> tuple[bytes, int]:
    if not isinstance(pair, (tuple, list)) or len(pair) != 2:
        raise PsbtError("each recipient must be a (script, value_sats) pair")
    raw_script, value = pair
    if not isinstance(raw_script, (bytes, bytearray, memoryview)):
        raise PsbtError("recipient script must be bytes")
    script = bytes(raw_script)
    if not script or len(script) > 10_000:
        raise PsbtError("recipient script is empty or oversized")
    if script[0] == 0x6A:
        raise PsbtError("recipient script must not be unspendable")
    if not isinstance(value, int) or isinstance(value, bool):
        raise PsbtError("recipient value must be an integer")
    if not 0 < value <= _MAX_MONEY_SATS:
        raise PsbtError("recipient value out of range")
    if value < dust_threshold(script):
        raise PsbtError(
            "recipient value is below the dust threshold of its script type"
        )
    return script, value


def build_unsigned_psbt(
    selected_utxos: Sequence[PsbtInputSource],
    recipients: Sequence[tuple[bytes, int]],
    change_address: str | None,
    change_sats: int | None,
    *,
    account_key: HDKey,
    account_fingerprint: bytes,
    account_path: Sequence[int],
    locktime: int = 0,
    sequence: int = SEQUENCE_RBF_ENABLED,
) -> tuple[PSBT, PsbtMeta]:
    """Build the unsigned PSBT plus its intended-transaction metadata.

    Args:
        selected_utxos: The selected inputs (from
            :func:`~localwallet.tx.selection.select_coins`, mapped to
            :class:`PsbtInputSource` by the caller). Duplicates are
            refused; every script must be the wallet's own P2WPKH.
        recipients: Ordered ``(script, value_sats)`` payment outputs.
            Values must individually and jointly respect dust and
            ``MAX_MONEY`` bounds.
        change_address: Mainnet bech32 address of a *fresh change index*
            (ADR-0009 allocation is the caller's store-side duty; this
            module only serializes it), or ``None``.
        change_sats: Change value in sats, or ``None``. Both change
            arguments must be given together or neither.
        account_key: The account-level embit watch key (public). Private
            keys are refused — watch-only invariant.
        account_fingerprint: 4-byte origin fingerprint for the PSBT
            derivation fields.
        account_path: Hardened origin path from that fingerprint, e.g.
            ``(84 + 2**31, 0 + 2**31, 2**31)`` for the canonical mainnet
            BIP84 account.
        locktime: Transaction locktime (0..0xffffffff); 0 = final.
        sequence: nSequence for every input; defaults to
            :data:`SEQUENCE_RBF_ENABLED` (see module docstring / ADR-0012).

    Returns:
        ``(psbt, meta)`` — the unsigned BIP174 PSBT and the
        :class:`PsbtMeta` the signed result will be validated against.

    Raises:
        PsbtError: on any structural, policy, or watch-only violation
            (value-free messages).
    """
    if not selected_utxos or len(selected_utxos) > _MAX_INPUTS:
        raise PsbtError("selected_utxos is empty or over the size limit")
    if not recipients or len(recipients) > _MAX_RECIPIENTS:
        raise PsbtError("recipients is empty or over the size limit")
    if not isinstance(account_key, HDKey):
        raise PsbtError("account_key must be an embit HDKey")
    if account_key.is_private:
        raise PsbtError(
            "watch-only: private keys are never handled — provide the "
            "account public key"
        )
    if not isinstance(account_fingerprint, (bytes, bytearray, memoryview)) or len(
        account_fingerprint
    ) != 4:
        raise PsbtError("account_fingerprint must be 4 bytes")
    fingerprint = bytes(account_fingerprint)
    if not account_path or len(account_path) > 8:
        raise PsbtError("account_path must contain 1..8 derivation indexes")
    for idx in account_path:
        if not isinstance(idx, int) or isinstance(idx, bool):
            raise PsbtError("account_path indexes must be integers")
        if not 2**31 <= idx <= 2**32 - 1:
            raise PsbtError("account_path indexes must be hardened")
    if not isinstance(locktime, int) or isinstance(locktime, bool) or not (
        0 <= locktime <= _MAX_LOCKTIME
    ):
        raise PsbtError("locktime out of range")
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        raise PsbtError("sequence must be an integer")
    if sequence != SEQUENCE_RBF_ENABLED:
        # N4 policy is structural, not conventional: v1 signals opt-in RBF
        # on every input (see module docstring / ADR-0012).
        raise PsbtError("sequence must be the RBF-signaling policy value")
    if (change_address is None) != (change_sats is None):
        raise PsbtError("change_address and change_sats must be given together")

    validated_recipients = [_recipient_from_pair(pair) for pair in recipients]

    change_script: bytes | None = None
    if change_address is not None:
        if not isinstance(change_address, str):
            raise PsbtError("change_address must be a string")
        # Mainnet-only gate (ADR-0021): bech32 mainnet hrp is "bc".
        if not change_address.startswith("bc1"):
            raise PsbtError("change_address must be a mainnet bech32 address")
        try:
            change_script = bytes(address_to_scriptpubkey(change_address).data)
        except Exception as exc:  # containment: embit raises varied errors for bad addresses
            raise PsbtError("change_address is not a valid bech32 address") from exc
        _validate_p2wpkh_script(change_script, "change_address script")
        if not isinstance(change_sats, int) or isinstance(change_sats, bool):
            raise PsbtError("change_sats must be an integer")
        if not 0 < change_sats <= _MAX_MONEY_SATS:
            raise PsbtError("change_sats out of range")
        if change_sats < dust_threshold(change_script):
            raise PsbtError("change_sats is below the dust threshold")

    # Canonical deterministic input order.
    ordered: list[PsbtInputSource] = sorted(
        selected_utxos, key=lambda u: (u.txid.lower(), u.vout)
    )
    seen: set[tuple[str, int]] = set()
    inputs_total = 0
    for utxo in ordered:
        if not isinstance(utxo, PsbtInputSource):
            raise PsbtError("selected_utxos must be PsbtInputSource records")
        key = (utxo.txid.lower(), utxo.vout)
        if key in seen:
            raise PsbtError("duplicate utxo in the selection input")
        seen.add(key)
        if not isinstance(utxo.vout, int) or isinstance(utxo.vout, bool) or not (
            0 <= utxo.vout <= 0xFFFFFFFF
        ):
            raise PsbtError("utxo vout out of range")
        if not isinstance(utxo.value_sats, int) or isinstance(utxo.value_sats, bool):
            raise PsbtError("utxo value must be an integer")
        if not 0 < utxo.value_sats <= _MAX_MONEY_SATS:
            raise PsbtError("utxo value out of range")
        if not isinstance(utxo.script_pubkey, (bytes, bytearray, memoryview)):
            raise PsbtError("utxo script must be bytes")
        _validate_p2wpkh_script(bytes(utxo.script_pubkey), "utxo script")
        if utxo.branch not in (0, 1):
            raise PsbtError("utxo branch must be 0 (receive) or 1 (change)")
        if (
            not isinstance(utxo.index, int)
            or isinstance(utxo.index, bool)
            or not 0 <= utxo.index <= 2**31 - 1
        ):
            raise PsbtError("utxo index out of the non-hardened BIP32 range")
        inputs_total += utxo.value_sats

    # Corrupt-snapshot guard, fail closed: each input respects MAX_MONEY
    # individually, but their sum could still exceed it (a broken UTXO
    # snapshot); refuse before any serialization (value-free).
    if inputs_total > _MAX_MONEY_SATS:
        raise PsbtError("total input value out of range")

    outputs_total = sum(value for _script, value in validated_recipients)
    if change_sats is not None:
        outputs_total += change_sats
    if outputs_total > _MAX_MONEY_SATS:
        raise PsbtError("total output value out of range")
    fee_sats = inputs_total - outputs_total
    if fee_sats <= 0:
        raise PsbtError("inputs do not cover the outputs (fee must be positive)")

    meta_vsize = estimate_tx_vsize(
        len(ordered),
        [script for script, _value in validated_recipients],
        change_cost_vbytes=(
            8 + 1 + len(change_script) if change_script is not None else None
        ),
    )
    if fee_sats < min_relay_fee_vbytes(meta_vsize, min_relay_sat_vb=1):
        raise PsbtError("fee is below the min-relay floor for this transaction size")

    vin = [
        TransactionInput(
            txid=_validate_hex_txid(utxo.txid), vout=utxo.vout, sequence=sequence
        )
        for utxo in ordered
    ]
    vout = [
        TransactionOutput(value, Script(script))
        for script, value in validated_recipients
    ]
    if change_script is not None and change_sats is not None:
        vout.append(TransactionOutput(change_sats, Script(change_script)))

    tx = Transaction(version=_TX_VERSION, vin=vin, vout=vout, locktime=locktime)
    psbt = PSBT(tx=tx)

    for psbt_input, utxo in zip(psbt.inputs, ordered):
        psbt_input.witness_utxo = TransactionOutput(
            utxo.value_sats, Script(bytes(utxo.script_pubkey))
        )
        child_pubkey = account_key.derive([utxo.branch, utxo.index]).key
        derived_script = b"\x00\x14" + _hashes.hash160(child_pubkey.sec())
        if derived_script != bytes(utxo.script_pubkey):
            raise PsbtError(
                "utxo script does not match its wallet derivation coordinates"
            )
        psbt_input.bip32_derivations[child_pubkey] = DerivationPath(
            fingerprint, list(account_path) + [utxo.branch, utxo.index]
        )

    expected_outputs = tuple(
        (script, value) for script, value in validated_recipients
    )
    if change_script is not None and change_sats is not None:
        expected_outputs = expected_outputs + ((change_script, change_sats),)
    meta = PsbtMeta(
        expected_outputs=expected_outputs,
        expected_fee_sats=fee_sats,
        vsize=meta_vsize,
        inputs_count=len(ordered),
    )
    # Conservation invariant at the money-path boundary (assert, not error
    # handling — a violation is a bug here, and AssertionError is the
    # documented failure mode): the inputs must exactly fund every intended
    # output plus the expected fee.
    assert inputs_total == sum(
        value for _script, value in meta.expected_outputs
    ) + meta.expected_fee_sats, (
        "psbt conservation invariant violated: inputs_total != outputs + expected fee"
    )
    validate_psbt_shape(psbt, meta)
    return psbt, meta


def validate_psbt_shape(psbt: PSBT, meta: PsbtMeta) -> None:
    """Structurally re-check ``psbt`` against ``meta`` (fail closed).

    Verifies: input counts (PSBT scopes vs unsigned tx), output scripts and
    values in exact order, witness UTXO presence and positivity, the RBF
    sequence policy on every input, tx version, and that the fee recomputed
    from the PSBT's own witness UTXOs equals ``meta.expected_fee_sats``.

    Raises:
        PsbtValidationError: on any mismatch (value-free messages). Phase 3
            extends this with signed-PSBT re-validation before broadcast.
    """
    if not isinstance(psbt, PSBT):
        raise PsbtValidationError("not a PSBT object")
    if not isinstance(meta, PsbtMeta):
        raise PsbtValidationError("not a PsbtMeta object")
    tx = psbt.tx
    if len(psbt.inputs) != meta.inputs_count:
        raise PsbtValidationError("psbt input count does not match metadata")
    if len(tx.vin) != meta.inputs_count:
        raise PsbtValidationError("unsigned tx input count does not match metadata")
    if len(psbt.outputs) != len(meta.expected_outputs):
        raise PsbtValidationError("psbt output count does not match metadata")
    if len(tx.vout) != len(meta.expected_outputs):
        raise PsbtValidationError("unsigned tx output count does not match metadata")
    if tx.version != _TX_VERSION:
        raise PsbtValidationError("unexpected transaction version")

    for i, vin in enumerate(tx.vin):
        if vin.sequence != SEQUENCE_RBF_ENABLED:
            raise PsbtValidationError("input sequence violates the RBF policy")
        scope = psbt.inputs[i]
        if scope.witness_utxo is None:
            raise PsbtValidationError("input is missing its witness utxo")
        if scope.witness_utxo.value <= 0:
            raise PsbtValidationError("witness utxo value must be positive")

    inputs_total = 0
    for scope in psbt.inputs:
        inputs_total += scope.witness_utxo.value  # type: ignore[union-attr]
    outputs_total = 0
    for i, (expected_script, expected_value) in enumerate(meta.expected_outputs):
        out = tx.vout[i]
        if bytes(out.script_pubkey.data) != bytes(expected_script):
            raise PsbtValidationError(
                "output script does not match the intended transaction"
            )
        if out.value != expected_value:
            raise PsbtValidationError(
                "output value does not match the intended transaction"
            )
        scope = psbt.outputs[i]
        if bytes(scope.script_pubkey.data) != bytes(expected_script):
            raise PsbtValidationError("psbt output scope script mismatch")
        if scope.value != expected_value:
            raise PsbtValidationError("psbt output scope value mismatch")
        outputs_total += expected_value

    fee_sats = inputs_total - outputs_total
    if fee_sats != meta.expected_fee_sats:
        raise PsbtValidationError("recomputed fee does not match metadata")
    if fee_sats < min_relay_fee_vbytes(meta.vsize, min_relay_sat_vb=1):
        raise PsbtValidationError("fee is below the min-relay floor")


def psbt_to_base64(psbt: PSBT) -> str:
    """Serialize a PSBT to base64 (file-signer exchange format, OQ18)."""
    if not isinstance(psbt, PSBT):
        raise PsbtError("not a PSBT object")
    return base64.b64encode(psbt.serialize()).decode()

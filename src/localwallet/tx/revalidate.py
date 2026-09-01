"""Deterministic signed-PSBT re-validation — the gate before broadcast.

Implements PROJECT.md §6 step 6 / §9: *"Signed PSBT is re-parsed and
deterministically re-validated against the intended transaction (outputs,
fee) before broadcast. Mismatch = hard stop."* This module is the Phase 3
tamper-detection money path (TCK-P3-002): it receives the base64 signed
PSBT that came back from a signer plus the dispatcher-owned
:class:`IntendedTx` (built from the same state the user confirmed —
``PendingTx``/``PsbtMeta``), and either produces a :class:`RevalidatedTx`
whose fields are quoted verbatim from the re-parsed transaction, or raises
:class:`TamperedPsbtError`. It trusts **nothing** inside the PSBT: every
compared quantity is either matched positionally against the intended
transaction or recomputed from the extracted transaction.

Purity contract: pure module — no I/O, no network, no store, no clock; all
arithmetic in integers; error messages are value-free (they name the CHECK
that failed, never scripts, addresses, or amounts).

embit-0.8.0 API used (verified empirically; see tests)
------------------------------------------------------
- ``embit.psbt.PSBT.parse(raw_bytes)`` — strict parse: refuses the wrong
  magic, duplicate keys, and *trailing bytes* (``EmbitError``), so the
  decoded container must be exactly one PSBT.
- ``embit.finalizer.finalize_psbt(psbt) -> Transaction | None`` — extracts
  the broadcast transaction by building each input's final witness from
  the PSBT's ``partial_sigs``; returns ``None`` when any input's
  satisfaction is incomplete. NOTE: embit's own docstring flags it
  "UNRELIABLE … doesn't check pubkeys" — it happily builds a witness from
  a signature made under a *different* key, so finalize alone cannot be
  the integrity gate; check 11 below adds deterministic signature
  verification on top (this is why ``PSBT.verify``/``is_verified`` — which
  in embit only check the non-witness-UTXO prevout hash — are NOT used).
- ``PSBT.sighash(i)`` — the consensus BIP-143 digest (for P2WPKH it
  substitutes the P2PKH scriptCode, exactly what signing devices sign;
  the same path embit's ``PSBT.sign_with`` uses).
- ``Transaction.txid()`` — a *method* in 0.8.0 (double-SHA256 over the
  no-witness serialization, byte-reversed): the reported txid.
- vsize: embit 0.8.0 exposes no ``vsize``/``weight`` property, so it is
  measured from the extracted transaction's serializations —
  ``weight = 3 * stripped + full`` (BIP 141), ``vsize = ceil(weight / 4)`` —
  the same convention as :func:`~localwallet.tx.selection.estimate_tx_vsize`.

Check pipeline (documented order — first failure wins, all fail closed)
-----------------------------------------------------------------------
 1. intended-transaction sanity   (caller-owned state itself is well-formed)
 2. base64 decode                 (strict alphabet/padding, no whitespace)
 3. PSBT magic bytes              (``psbt\\xff`` prefix)
 4. embit parse                   (malformed / truncated / trailing bytes;
     parse runs before any size ceiling — the container-size bound is
     enforced at the gateway boundary, P3-005 will pass bounded data)
 5. container version + structural shape: PSBT version, tx version,
    input count, per-input sequence present and equal to intended,
    witness UTXO present/positive/P2WPKH (v1 spend policy, ADR-0008),
    no out-of-policy per-input state (redeem/witness scripts, taproot
    fields, prebuilt final fields, non-ALL sighash-type field)
 6. signatures PRESENT            (exactly one partial sig per input —
    single-sig policy; the finalizer would otherwise pick an arbitrary
    entry from several, which is ambiguity → refuse)
 7. finalize + extract            (embit finalizer; ``None`` → refuse)
 8. extracted tx field-by-field vs intended: version, input count,
    per-input sequence, and the output list EXACTLY
    ``[recipients..., change?]`` in order, values sat-for-sat and
    scripts byte-for-byte (positional — the order the user confirmed)
 9. fee recomputed from witness-UTXO totals minus extracted outputs —
    equal to the intended fee EXACTLY (no tolerance: it was computed
    deterministically at build time)
10. extracted vsize ≤ ``expected_vsize_max`` (build estimate + 1; real
    signatures may be one vB shorter than the max-witness convention)
11. signature verification per input: the single partial-sig pubkey must
    hash to the input's witness program, and the signature (SIGHASH.ALL
    hashtype byte enforced) must verify against the consensus BIP-143
    digest of the re-parsed transaction — deterministic EC check, no
    device trust
12. fee sanity: positive, ≤ total input value, and ≥ the min-relay floor
    computed from the *extracted* transaction's actual vsize (never a
    hardcoded constant)

What the gate catches vs what it defers (detection depth, honest)
-----------------------------------------------------------------
===========================================  ==============================
Caught here, deterministically               Deferred
===========================================  ==============================
Any output script/value/order/count drift    Key *ownership* policy: the
(vs the intended tx)                         pubkey only has to match the
Fee ≠ intended (exact, both sides of the     input's witness program and
equation: input values via check 9/12,       verify — any watch-only key
outputs via check 8)                         controlling that program
Sequence / version / input-count drift       passes; binding it to the
Missing, duplicate, wrong-key,               wallet's own derivation is the
wrong-digest signatures (11)                 descriptor/signer layer's job
Unparseable / truncated / wrong-magic        Key-holder authorization
containers (2–4)                             (was this signature *meant*?
Per-input witness-UTXO value of a signed     device policy) — the device
input — covered by its signature (11)        screen remains the trust
Sum-preserving reshuffles of *other*         anchor (§9/§5.7)
inputs' witness-UTXO values are NOT
signature-covered (BIP-143 hashes only
outpoints for prevouts) — but they are
economically inert: the *sum*, hence the
fee, is exact-checked in 9. Residual
per-input attribution differences cannot
change what the transaction pays.
Input outpoint tampering — covered by the
signature digest (prevouts are hashed)
===========================================  ==============================
High-S standardness (BIP62) is NOT a consensus rule: high-S signatures are
consensus-valid and a default-policy node may refuse them only as
non-standard (economically inert — wtxid-only). In practice this embit/
libsecp256k1 build's ``pubkey.verify`` path rejects high-S outright, so the
gate currently refuses them (stricter than a relay policy requires). A
future change may normalize-before-verify to permit high-S if desired;
until then the gate fails closed on them.

Pre-finalized containers (``final_scriptwitness``/``final_scriptsig``
already set) are REFUSED as ambiguous state (check 5): this module always
rebuilds the final witness itself from signatures it has verified, so a
prebuilt witness can never bypass check 11. Unknown/proprietary PSBT
fields are tolerated as inert metadata (devices add fingerprint fields);
they cannot influence any compared quantity.

Input outpoints are *not* part of :class:`IntendedTx` (dispatcher-owned
state carries only the count): outpoint integrity is enforced
transitively — every outpoint is covered by the BIP-143 prevout hash, so
a swapped input fails signature verification. Duplicate outpoints (the
same UTXO spent twice) are *not* caught transitively, so check 5 refuses
them explicitly.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Final

from embit import finalizer, hashes
from embit.ec import Signature
from embit.psbt import PSBT
from embit.transaction import Transaction, Witness

from localwallet.tx.dust import TxEngineError, min_relay_fee_vbytes

__all__ = [
    "IntendedTx",
    "RevalidatedTx",
    "TamperedPsbtError",
    "revalidate_signed_psbt",
]

_PSBT_MAGIC: Final[bytes] = b"psbt\xff"
_TX_VERSION: Final[int] = 2
_SIGHASH_ALL: Final[int] = 0x01

# v1 spend policy (ADR-0008): inputs are the wallet's own P2WPKH outputs.
_P2WPKH_SCRIPT_LEN: Final[int] = 22
_P2WPKH_PREFIX: Final[bytes] = b"\x00\x14"

# Sanity bounds mirroring the builder (localwallet.tx.psbt) and Core's
# standardness limit (MAX_STANDARD_TX_WEIGHT / 4, cf. localwallet.tx.dust).
_MAX_MONEY_SATS: Final[int] = 2_100_000_000_000_000
_MAX_SCRIPT_BYTES: Final[int] = 10_000
_MAX_RECIPIENT_OUTPUTS: Final[int] = 100
_MAX_INPUTS_COUNT: Final[int] = 1000
_MAX_VSIZE: Final[int] = 100_000


class TamperedPsbtError(TxEngineError):
    """A signed PSBT failed deterministic re-validation (value-free).

    The message always names the CHECK that failed and never contains
    scripts, addresses, or amounts — those reach the user only through the
    confirmation card and the re-validated result, quoted verbatim from
    tool output (PROJECT.md §7.8, §9).
    """


@dataclass(frozen=True, slots=True)
class IntendedTx:
    """The dispatcher-owned transaction a signed PSBT must match exactly.

    Built by the dispatcher from the same state the user confirmed (the
    ``PendingTx``/``PsbtMeta`` pair at sign time); frozen so the signed
    result is validated against immutable intent. Positional contract —
    identical to :func:`~localwallet.tx.psbt.build_unsigned_psbt`:

    - ``expected_recipient_outputs``: ordered ``(script, value_sats)``
      payment outputs, exactly as built;
    - ``expected_change``: the change ``(script, value_sats)`` or ``None``
      — when present it is expected LAST in the output list;
    - ``expected_sequence``: the per-input nSequence policy value;
    - ``expected_vsize_max``: the build-time vsize estimate plus one (the
      signed transaction may be a vB *shorter* than the max-witness
      estimate, never meaningfully longer);
    - ``tx_ref``: the pending-transaction reference this intent belongs to
      (kept on the intent for audit symmetry; re-validation itself never
      echoes it).
    """

    expected_recipient_outputs: tuple[tuple[bytes, int], ...]
    expected_change: tuple[bytes, int] | None
    expected_inputs_count: int
    expected_fee_sats: int
    expected_sequence: int
    expected_vsize_max: int
    tx_ref: str


@dataclass(frozen=True, slots=True)
class RevalidatedTx:
    """The verified broadcast transaction, quoted from the re-parse.

    Every field is read back from the EXTRACTED transaction (what will
    actually be broadcast) after all checks passed — never from PSBT
    metadata and never from the intended record — so callers narrate
    verbatim tool output (PROJECT.md §5.1). ``signers_complete`` is True
    by construction: re-validation is fail-closed and a ``RevalidatedTx``
    only exists when every input carried exactly one signature that
    verified and finalized; the field is kept so callers can assert the
    invariant explicitly.
    """

    txid: str
    recipient_outputs: tuple[tuple[bytes, int], ...]
    change_output: tuple[bytes, int] | None
    fee_sats: int
    vsize: int
    inputs_count: int
    signers_complete: bool


def _validate_output_pair(pair: object, what: str) -> tuple[bytes, int]:
    """Sanity-check one intended ``(script, value_sats)`` pair (fail closed)."""
    if not isinstance(pair, (tuple, list)) or len(pair) != 2:
        raise TamperedPsbtError(f"intended transaction {what} must be a (script, value) pair")
    raw_script, value = pair
    if not isinstance(raw_script, (bytes, bytearray, memoryview)):
        raise TamperedPsbtError(f"intended transaction {what} script must be bytes")
    script = bytes(raw_script)
    if not script or len(script) > _MAX_SCRIPT_BYTES:
        raise TamperedPsbtError(f"intended transaction {what} script is empty or oversized")
    if not isinstance(value, int) or isinstance(value, bool):
        raise TamperedPsbtError(f"intended transaction {what} value must be an integer")
    if not 0 < value <= _MAX_MONEY_SATS:
        raise TamperedPsbtError(f"intended transaction {what} value out of range")
    return script, value


def _validate_intended(
    intended: IntendedTx,
) -> tuple[list[tuple[bytes, int]], tuple[bytes, int] | None]:
    """Check 1 — the intended transaction itself must be well-formed.

    The intent is dispatcher-owned state, so a violation is a caller bug,
    not a tamper — but the money path fails closed on it all the same
    (same exception type; the message names the intended-state check).

    Returns:
        The canonicalized recipient list (bytes scripts) and the change
        pair or ``None``.
    """
    if not isinstance(intended, IntendedTx):
        raise TamperedPsbtError("intended transaction must be an IntendedTx")
    if not isinstance(intended.tx_ref, str) or not intended.tx_ref:
        raise TamperedPsbtError("intended transaction reference must be a non-empty string")
    if not isinstance(intended.expected_recipient_outputs, (tuple, list)):
        raise TamperedPsbtError("intended transaction recipient outputs must be a sequence")
    if not 1 <= len(intended.expected_recipient_outputs) <= _MAX_RECIPIENT_OUTPUTS:
        raise TamperedPsbtError("intended transaction recipient outputs count out of range")
    recipients = [
        _validate_output_pair(pair, "recipient output")
        for pair in intended.expected_recipient_outputs
    ]
    change: tuple[bytes, int] | None = None
    if intended.expected_change is not None:
        change = _validate_output_pair(intended.expected_change, "change output")
    if not isinstance(intended.expected_inputs_count, int) or isinstance(
        intended.expected_inputs_count, bool
    ):
        raise TamperedPsbtError("intended transaction inputs count must be an integer")
    if not 1 <= intended.expected_inputs_count <= _MAX_INPUTS_COUNT:
        raise TamperedPsbtError("intended transaction inputs count out of range")
    if not isinstance(intended.expected_fee_sats, int) or isinstance(
        intended.expected_fee_sats, bool
    ):
        raise TamperedPsbtError("intended transaction fee must be an integer")
    if not 0 < intended.expected_fee_sats <= _MAX_MONEY_SATS:
        raise TamperedPsbtError("intended transaction fee must be positive and in range")
    if not isinstance(intended.expected_sequence, int) or isinstance(
        intended.expected_sequence, bool
    ):
        raise TamperedPsbtError("intended transaction sequence must be an integer")
    if not 0 <= intended.expected_sequence <= 0xFFFFFFFF:
        raise TamperedPsbtError("intended transaction sequence out of range")
    if not isinstance(intended.expected_vsize_max, int) or isinstance(
        intended.expected_vsize_max, bool
    ):
        raise TamperedPsbtError("intended transaction vsize maximum must be an integer")
    if not 1 <= intended.expected_vsize_max <= _MAX_VSIZE:
        raise TamperedPsbtError("intended transaction vsize maximum out of range")
    return recipients, change


def _decode_psbt(psbt_base64: str) -> PSBT:
    """Checks 2–4 — strict base64 decode, magic bytes, embit parse."""
    if not isinstance(psbt_base64, str) or not psbt_base64:
        raise TamperedPsbtError("signed psbt must be a non-empty base64 string")
    try:
        raw = base64.b64decode(psbt_base64.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise TamperedPsbtError("signed psbt is not valid base64") from exc
    if raw[: len(_PSBT_MAGIC)] != _PSBT_MAGIC:
        raise TamperedPsbtError("signed psbt magic bytes are missing or invalid")
    try:
        # embit parse is strict: duplicate keys and trailing bytes refuse.
        return PSBT.parse(raw)
    except Exception as exc:  # containment: embit raises varied parse errors
        raise TamperedPsbtError("signed psbt is malformed or truncated") from exc


def _measured_vsize(tx: Transaction) -> int:
    """vsize of ``tx`` by BIP 141 weight arithmetic (no embit property exists).

    ``weight = 3 * base_size + total_size`` where base_size is the
    serialization without marker/flag/witness and total_size with them;
    ``vsize = ceil(weight / 4)`` — pure integer math (never floats).
    """
    witnesses = [vin.witness for vin in tx.vin]
    for vin in tx.vin:
        vin.witness = Witness([])
    stripped = len(tx.serialize())
    for vin, witness in zip(tx.vin, witnesses):
        vin.witness = witness
    full = len(tx.serialize())
    return -(-(3 * stripped + full) // 4)


def revalidate_signed_psbt(psbt_base64: str, intended: IntendedTx) -> RevalidatedTx:
    """Deterministically re-validate a signed PSBT against the intent.

    Executes the documented check pipeline (module docstring) and, only if
    every check passes, returns the :class:`RevalidatedTx` quoted from the
    extracted transaction. Any mismatch — structural, semantic, or
    cryptographic — raises :class:`TamperedPsbtError` naming the failed
    check (value-free). This call is pure and idempotent: the same base64
    with the same intent always yields an equal result and never mutates
    its inputs.

    Args:
        psbt_base64: The signed PSBT exactly as the signer gateway
            returned it (the same serialization the dispatcher handed
            out; no whitespace tolerance — fail closed).
        intended: The dispatcher-owned intent (frozen at confirm time).

    Raises:
        TamperedPsbtError: on ANY failed or ambiguous check (including a
            malformed intent — a caller bug fails closed like a tamper).
    """
    recipients, change = _validate_intended(intended)
    psbt = _decode_psbt(psbt_base64)

    # -- Check 5: container version + structural shape --------------------
    if psbt.version not in (None, 0):
        raise TamperedPsbtError("unsupported psbt container version")
    tx = psbt.tx  # embit rebuilds this from the scopes on every access
    if tx.version != _TX_VERSION:
        raise TamperedPsbtError("unexpected transaction version")
    if len(psbt.inputs) != intended.expected_inputs_count:
        raise TamperedPsbtError(
            "signed psbt input count does not match the intended transaction"
        )
    seen_outpoints: set[tuple[bytes, int]] = set()
    for scope in psbt.inputs:
        if scope.sequence is None:
            raise TamperedPsbtError("input is missing its sequence field")
        if scope.sequence != intended.expected_sequence:
            raise TamperedPsbtError("input sequence does not match the intended transaction")
        # Duplicate outpoints = the same UTXO spent twice: an invalid
        # transaction no node would relay. Not caught transitively (each
        # signature verifies independently), so refuse explicitly.
        outpoint = (scope.vin.txid, scope.vin.vout)
        if outpoint in seen_outpoints:
            raise TamperedPsbtError("duplicate input outpoints are not allowed")
        seen_outpoints.add(outpoint)
        if scope.witness_utxo is None:
            raise TamperedPsbtError("input is missing its witness utxo")
        if scope.witness_utxo.value <= 0:
            raise TamperedPsbtError("witness utxo value must be positive")
        program = bytes(scope.witness_utxo.script_pubkey.data)
        if len(program) != _P2WPKH_SCRIPT_LEN or program[:2] != _P2WPKH_PREFIX:
            raise TamperedPsbtError("input script is not a supported witness program")
        if scope.redeem_script is not None or scope.witness_script is not None:
            raise TamperedPsbtError("input carries unsupported script state")
        if scope.final_scriptsig is not None or scope.final_scriptwitness is not None:
            # The final witness is rebuilt HERE from verified signatures
            # only — a prebuilt one could never be proven to match.
            raise TamperedPsbtError("input carries unexpected finalization data")
        if (
            scope.taproot_internal_key is not None
            or scope.taproot_merkle_root is not None
            or scope.taproot_sigs
            or scope.taproot_scripts
            or scope.taproot_bip32_derivations
        ):
            raise TamperedPsbtError("input carries taproot fields outside the v1 policy")
        if scope.sighash_type is not None and scope.sighash_type != _SIGHASH_ALL:
            raise TamperedPsbtError("input sighash type field is unsupported")

    # -- Check 6: signatures PRESENT (exactly one per input) ---------------
    for scope in psbt.inputs:
        if len(scope.partial_sigs) == 0:
            raise TamperedPsbtError("input is missing its signature")
        if len(scope.partial_sigs) > 1:
            # Single-sig policy (ADR-0008): with several partial sigs the
            # finalizer would pick an arbitrary one — ambiguity → refuse.
            raise TamperedPsbtError("input carries more than one signature")

    # -- Check 7: finalize + extract the broadcast transaction -------------
    try:
        extracted = finalizer.finalize_psbt(psbt)
    except Exception as exc:  # containment: embit finalizer raises varied errors
        raise TamperedPsbtError("signed transaction could not be finalized") from exc
    if extracted is None:
        raise TamperedPsbtError("signed transaction could not be finalized")

    # -- Check 8: extracted transaction field-by-field vs the intent ------
    if extracted.version != _TX_VERSION:
        raise TamperedPsbtError("extracted transaction version mismatch")
    if len(extracted.vin) != intended.expected_inputs_count:
        raise TamperedPsbtError(
            "extracted transaction input count does not match the intended transaction"
        )
    for vin in extracted.vin:
        if vin.sequence != intended.expected_sequence:
            raise TamperedPsbtError(
                "extracted transaction input sequence does not match the intended transaction"
            )
    expected_outputs: list[tuple[bytes, int]] = list(recipients)
    if change is not None:
        expected_outputs.append(change)
    if len(extracted.vout) != len(expected_outputs):
        raise TamperedPsbtError(
            "extracted transaction output count does not match the intended transaction"
        )
    for index, (expected_script, expected_value) in enumerate(expected_outputs):
        out = extracted.vout[index]
        if bytes(out.script_pubkey.data) != expected_script:
            raise TamperedPsbtError(
                "extracted transaction output script does not match the intended transaction"
            )
        if out.value != expected_value:
            raise TamperedPsbtError(
                "extracted transaction output value does not match the intended transaction"
            )

    # -- Check 9: fee recomputed from the extracted transaction, EXACT -----
    inputs_total = 0
    for scope in psbt.inputs:
        inputs_total += scope.witness_utxo.value  # checked present/positive above
    outputs_total = sum(out.value for out in extracted.vout)
    fee_sats = inputs_total - outputs_total
    if fee_sats != intended.expected_fee_sats:
        raise TamperedPsbtError("recomputed fee does not match the intended transaction")

    # -- Check 10: vsize within the estimate+1 bound -----------------------
    vsize = _measured_vsize(extracted)
    if vsize > intended.expected_vsize_max:
        raise TamperedPsbtError("extracted transaction vsize exceeds the intended maximum")

    # -- Check 11: deterministic signature verification --------------------
    for index, scope in enumerate(psbt.inputs):
        try:
            (pubkey, sig_value), = scope.partial_sigs.items()
            if not isinstance(sig_value, (bytes, bytearray, memoryview)):
                raise TamperedPsbtError("signature is malformed")
            sig_value = bytes(sig_value)
            if len(sig_value) < 2 or sig_value[-1] != _SIGHASH_ALL:
                raise TamperedPsbtError("signature hash type is not supported")
            try:
                signature = Signature.parse(sig_value[:-1])
            except Exception as exc:  # containment: DER parse errors vary
                raise TamperedPsbtError("signature is malformed") from exc
            program = bytes(scope.witness_utxo.script_pubkey.data)[2:]  # type: ignore[union-attr]
            if hashes.hash160(pubkey.sec()) != program:
                raise TamperedPsbtError("signature public key does not match the input script")
            # Consensus BIP-143 digest: PSBT.sighash substitutes the P2PKH
            # scriptCode for P2WPKH inputs (all out-of-policy script states
            # were refused in check 5, so this path is deterministic).
            digest = psbt.sighash(index)
            if not pubkey.verify(signature, digest):
                raise TamperedPsbtError("signature does not verify against the transaction")
        except TamperedPsbtError:
            raise
        except Exception as exc:  # containment: no non-contract exception escapes
            raise TamperedPsbtError("signature verification failed") from exc

    # -- Check 12: fee sanity against the extracted transaction ------------
    if fee_sats <= 0:
        raise TamperedPsbtError("fee is not positive")
    if fee_sats > inputs_total:
        raise TamperedPsbtError("fee exceeds the total input value")
    if fee_sats < min_relay_fee_vbytes(vsize, min_relay_sat_vb=1):
        raise TamperedPsbtError(
            "fee is below the min-relay floor for the extracted transaction"
        )

    recipient_outputs = tuple(
        (bytes(extracted.vout[i].script_pubkey.data), extracted.vout[i].value)
        for i in range(len(recipients))
    )
    change_output: tuple[bytes, int] | None = None
    if change is not None:
        last = extracted.vout[-1]
        change_output = (bytes(last.script_pubkey.data), last.value)
    return RevalidatedTx(
        txid=extracted.txid().hex(),
        recipient_outputs=recipient_outputs,
        change_output=change_output,
        fee_sats=fee_sats,
        vsize=vsize,
        inputs_count=len(extracted.vin),
        signers_complete=True,
    )

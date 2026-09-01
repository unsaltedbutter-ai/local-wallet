"""Signed-PSBT re-validation tests (TCK-P3-002) — the tamper-detection money path.

Covers PROJECT.md §6 step 6 / §9: a signed PSBT is re-parsed and
deterministically re-validated against the intended transaction before any
broadcast, fail-closed, with value-free errors.

- happy path: fixtures built by :func:`~localwallet.tx.psbt.build_unsigned_psbt`
  and signed in-tests with the throwaway BIP32 test-vector-1 key (reusing
  the fixture machinery from ``tests.test_tx_psbt``), then re-validated —
  every :class:`RevalidatedTx` field checked against an independent embit
  extraction; txid stable across re-parses;
- TAMPER MATRIX: every row produces ``TamperedPsbtError`` naming the RIGHT
  check (recipient value, recipient script, change removed, extra output,
  output order, fee reduction, input count, sequence, stripped/foreign/
  wrong-digest signatures, garbage/truncated base64, stripped magic,
  unsigned-as-signed);
- determinism: same container + same intent → equal result; idempotent
  re-parse (parse→serialize→parse yields the same verdict);
- value-free errors: tamper messages never contain addresses or amounts.

Signing convention note: the vsize helper in ``tests.test_tx_psbt`` signs
with ``tx.sighash_segwit(i, raw_witness_program, value)`` — fine for
measuring size, but NOT the consensus BIP-143 digest (which substitutes
the P2PKH scriptCode for P2WPKH). Re-validation verifies consensus digests
(the path real devices and embit's ``PSBT.sign_with`` use), so the signing
helper here feeds ``psbt.sighash(i)`` to the same
``privkey.sign``/``Signature.write_to``/hashtype pattern. A test below
proves the gate is convention-strict: a right-key signature made over the
non-consensus digest is refused.
"""

import base64
from dataclasses import FrozenInstanceError
from io import BytesIO

import pytest
from embit import bip32, ec, finalizer, script
from embit.networks import NETWORKS
from embit.psbt import PSBT, InputScope, OutputScope
from embit.transaction import TransactionInput, TransactionOutput, Witness

from localwallet.tx.psbt import (
    SEQUENCE_RBF_ENABLED,
    PsbtMeta,
    psbt_to_base64,
)
from localwallet.tx.revalidate import (
    IntendedTx,
    RevalidatedTx,
    TamperedPsbtError,
    revalidate_signed_psbt,
)
from tests.test_tx_psbt import (
    SEED,
    build_pair,
    recipient_script,
    source,
    spk,
)

EVIL_SEED = bytes.fromhex("ff" * 32)  # unrelated throwaway key for wrong-sig rows


def sign_psbt(psbt: PSBT) -> PSBT:
    """Sign every input with the fixture wallet key (consensus BIP-143).

    Same mechanics as ``tests.test_tx_psbt``'s signing helper
    (``privkey.sign`` → ``Signature.write_to`` → append SIGHASH.ALL
    hashtype byte → ``partial_sigs``), but digesting ``psbt.sighash(i)`` —
    the consensus scriptCode path re-validation verifies.
    """
    for i, psbt_input in enumerate(psbt.inputs):
        (pub, derivation), = psbt_input.bip32_derivations.items()
        priv = bip32.HDKey.from_seed(SEED).derive(derivation.derivation).key
        digest = psbt.sighash(i)
        stream = BytesIO()
        ec.Signature.write_to(priv.sign(digest), stream)
        psbt_input.partial_sigs[pub] = stream.getvalue() + b"\x01"
    return psbt


def signed_container() -> tuple[str, PsbtMeta, IntendedTx]:
    """Two-input, one-recipient-with-change signed fixture (the happy path)."""
    inputs = [
        source("ab" * 32, 0, 40_000, index=3),
        source("cd" * 32, 0, 50_000, branch=1, index=4),
    ]
    (psbt, meta), selection = build_pair(inputs, 60_000)
    intended = IntendedTx(
        expected_recipient_outputs=((recipient_script(), 60_000),),
        expected_change=(spk(1, 7), selection.change_sats),
        expected_inputs_count=meta.inputs_count,
        expected_fee_sats=meta.expected_fee_sats,
        expected_sequence=SEQUENCE_RBF_ENABLED,
        expected_vsize_max=meta.vsize + 1,
        tx_ref="tx-ref-fixture-0001",
    )
    return psbt_to_base64(sign_psbt(psbt)), meta, intended


def no_change_container() -> tuple[str, PsbtMeta, IntendedTx]:
    """Single input, change folded into fee (dust-fold path)."""
    inputs = [source("ab" * 32, 0, 60_282, index=3)]
    (psbt, meta), _selection = build_pair(inputs, 60_000, with_change=False)
    intended = IntendedTx(
        expected_recipient_outputs=((recipient_script(), 60_000),),
        expected_change=None,
        expected_inputs_count=meta.inputs_count,
        expected_fee_sats=meta.expected_fee_sats,
        expected_sequence=SEQUENCE_RBF_ENABLED,
        expected_vsize_max=meta.vsize + 1,
        tx_ref="tx-ref-fixture-0002",
    )
    return psbt_to_base64(sign_psbt(psbt)), meta, intended


def reparsed(psbt_b64: str) -> PSBT:
    """The exchange-format attack surface: decode and re-parse the container."""
    return PSBT.parse(base64.b64decode(psbt_b64))


def to_b64(psbt: PSBT) -> str:
    return base64.b64encode(psbt.serialize()).decode()


#: secp256k1 group order (for the high-S S-negation helper below).
_SECP256K1_ORDER = (
    0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
)


def _high_s_signature_der(sig_der: bytes) -> bytes:
    """Return the DER of the S-negated (high-S) counterpart of ``sig_der``.

    A DER ECDSA signature is ``(r, s)``; ``(r, n - s)`` is the equally valid
    high-S form (BIP62 low-S is a *standardness*, not a consensus, rule).
    Used to pin that this embit/libsecp build's ``verify`` refuses high-S.
    """
    def _parse(der: bytes) -> tuple[int, int]:
        i = 0
        assert der[i] == 0x30
        i += 1
        i += 1  # sequence length
        assert der[i] == 0x02
        i += 1
        rl = der[i]
        i += 1
        r = int.from_bytes(der[i : i + rl], "big")
        i += rl
        assert der[i] == 0x02
        i += 1
        sl = der[i]
        i += 1
        s = int.from_bytes(der[i : i + sl], "big")
        return r, s

    def _encode(r: int, s: int) -> bytes:
        rb = r.to_bytes((r.bit_length() + 7) // 8 or 1, "big")
        if rb[0] & 0x80:
            rb = b"\x00" + rb
        sb = s.to_bytes((s.bit_length() + 7) // 8 or 1, "big")
        if sb[0] & 0x80:
            sb = b"\x00" + sb
        body = b"\x02" + bytes([len(rb)]) + rb + b"\x02" + bytes([len(sb)]) + sb
        return b"\x30" + bytes([len(body)]) + body

    r, s = _parse(sig_der)
    high_s = _SECP256K1_ORDER - s if s <= _SECP256K1_ORDER // 2 else s
    return _encode(r, high_s)


def tamper(
    psbt_b64: str, intended: IntendedTx, mutate, expected_check: str
) -> None:
    """Round-trip a mutation through the container and assert the named check."""
    mutated = reparsed(psbt_b64)
    mutate(mutated)
    with pytest.raises(TamperedPsbtError) as exc:
        revalidate_signed_psbt(to_b64(mutated), intended)
    assert expected_check in str(exc.value)


class TestHappyPath:
    def test_two_inputs_with_change_revalidates(self):
        psbt_b64, meta, intended = signed_container()
        result = revalidate_signed_psbt(psbt_b64, intended)

        assert isinstance(result, RevalidatedTx)
        assert result.signers_complete is True
        assert result.inputs_count == 2
        assert result.fee_sats == meta.expected_fee_sats
        assert result.vsize <= intended.expected_vsize_max
        # Outputs quoted verbatim from the extracted transaction: recipient
        # first, change last, exact values.
        assert result.recipient_outputs == ((recipient_script(), 60_000),)
        change_value = intended.expected_change[1]
        assert result.change_output == (spk(1, 7), change_value)

    def test_single_input_no_change_revalidates(self):
        psbt_b64, _meta, intended = no_change_container()
        result = revalidate_signed_psbt(psbt_b64, intended)

        assert result.inputs_count == 1
        assert result.change_output is None
        assert result.recipient_outputs == ((recipient_script(), 60_000),)
        assert result.fee_sats == intended.expected_fee_sats
        assert result.signers_complete is True

    def test_txid_matches_independent_extraction_and_is_stable(self):
        psbt_b64, _meta, intended = signed_container()
        result = revalidate_signed_psbt(psbt_b64, intended)

        # Independent extraction path: parse → finalize → txid() method.
        independent = finalizer.finalize_psbt(PSBT.parse(base64.b64decode(psbt_b64)))
        assert independent is not None
        assert result.txid == independent.txid().hex()
        assert len(result.txid) == 64
        int(result.txid, 16)  # hex txid

        # Stable across re-parse (idempotent container handling).
        again = revalidate_signed_psbt(psbt_b64, intended)
        assert again.txid == result.txid

    def test_measured_vsize_matches_independent_weight_arithmetic(self):
        psbt_b64, _meta, intended = signed_container()
        result = revalidate_signed_psbt(psbt_b64, intended)
        extracted = finalizer.finalize_psbt(PSBT.parse(base64.b64decode(psbt_b64)))
        assert extracted is not None

        # Independent BIP-141 weight arithmetic on a fresh extraction.
        witnesses = [vin.witness for vin in extracted.vin]
        for vin in extracted.vin:
            vin.witness = Witness([])
        stripped = len(extracted.serialize())
        for vin, witness in zip(extracted.vin, witnesses):
            vin.witness = witness
        full = len(extracted.serialize())
        weight = 3 * stripped + full
        assert result.vsize == -(-weight // 4)  # integer ceil(weight / 4)
        assert result.vsize <= intended.expected_vsize_max

    def test_revalidated_outputs_are_positional_copy_of_extracted(self):
        psbt_b64, _meta, intended = signed_container()
        result = revalidate_signed_psbt(psbt_b64, intended)
        extracted = finalizer.finalize_psbt(PSBT.parse(base64.b64decode(psbt_b64)))
        assert extracted is not None
        for i, (out_script, out_value) in enumerate(result.recipient_outputs):
            assert bytes(extracted.vout[i].script_pubkey.data) == out_script
            assert extracted.vout[i].value == out_value
        last = extracted.vout[-1]
        assert result.change_output == (bytes(last.script_pubkey.data), last.value)


class TestTamperMatrix:
    """Every row → TamperedPsbtError naming the RIGHT check (value-free)."""

    def test_recipient_value_changed(self):
        psbt_b64, _meta, intended = signed_container()
        tamper(
            psbt_b64,
            intended,
            lambda p: setattr(p.outputs[0], "value", p.outputs[0].value + 546),
            "output value does not match the intended transaction",
        )

    def test_recipient_script_swapped_for_another_valid_testnet_program(self):
        psbt_b64, _meta, intended = signed_container()
        tamper(
            psbt_b64,
            intended,
            lambda p: setattr(p.outputs[0], "script_pubkey", script.Script(spk(0, 50))),
            "output script does not match the intended transaction",
        )

    def test_change_output_removed(self):
        psbt_b64, _meta, intended = signed_container()
        tamper(
            psbt_b64,
            intended,
            lambda p: p.outputs.pop(),
            "output count does not match the intended transaction",
        )

    def test_extra_output_appended(self):
        psbt_b64, _meta, intended = signed_container()

        def append_output(p: PSBT) -> None:
            p.outputs.append(
                OutputScope(vout=TransactionOutput(1_000, script.Script(spk(0, 51))))
            )

        tamper(
            psbt_b64,
            intended,
            append_output,
            "output count does not match the intended transaction",
        )

    def test_output_order_swapped(self):
        psbt_b64, _meta, intended = signed_container()

        def swap(p: PSBT) -> None:
            p.outputs[0], p.outputs[1] = p.outputs[1], p.outputs[0]

        tamper(
            psbt_b64,
            intended,
            swap,
            "output script does not match the intended transaction",
        )

    def test_fee_silently_reduced_via_added_output_value(self):
        # Change output value +1000: the recipient is paid correctly but the
        # fee silently shrinks. The positional output check subsumes this
        # (fires first); the exact-fee check proper is exercised directly in
        # the next test.
        psbt_b64, _meta, intended = signed_container()
        tamper(
            psbt_b64,
            intended,
            lambda p: setattr(p.outputs[1], "value", p.outputs[1].value + 1_000),
            "output value does not match the intended transaction",
        )

    def test_fee_mismatch_via_input_value_detected_by_exact_fee_check(self):
        # Witness-UTXO value +1: outputs all match the intent, so the
        # recomputed fee (inputs − outputs) misses the intended fee exactly.
        psbt_b64, _meta, intended = signed_container()
        tamper(
            psbt_b64,
            intended,
            lambda p: setattr(
                p.inputs[0].witness_utxo, "value", p.inputs[0].witness_utxo.value + 1
            ),
            "recomputed fee does not match the intended transaction",
        )

    def test_input_removed(self):
        psbt_b64, _meta, intended = signed_container()
        tamper(
            psbt_b64,
            intended,
            lambda p: p.inputs.pop(),
            "input count does not match the intended transaction",
        )

    def test_extra_input_appended(self):
        psbt_b64, _meta, intended = signed_container()

        def append_input(p: PSBT) -> None:
            extra = InputScope(
                vin=TransactionInput(
                    txid=bytes(reversed(bytes.fromhex("ee" * 32))),
                    vout=0,
                    sequence=SEQUENCE_RBF_ENABLED,
                )
            )
            extra.witness_utxo = TransactionOutput(1_000, script.Script(spk(0, 52)))
            p.inputs.append(extra)

        tamper(
            psbt_b64,
            intended,
            append_input,
            "input count does not match the intended transaction",
        )

    def test_sequence_flipped_to_final(self):
        psbt_b64, _meta, intended = signed_container()

        def flip_sequence(p: PSBT) -> None:
            # The sequence lives in the container's global transaction
            # (PSBTv1 has no per-input sequence field): rebuild the PSBT
            # around a transaction whose first input is final (0xffffffff).
            tx = p.tx
            tx.vin[0].sequence = 0xFFFFFFFF
            rebuilt = PSBT(tx=tx)
            for dst, src in zip(rebuilt.inputs, p.inputs):
                dst.witness_utxo = src.witness_utxo
                dst.partial_sigs = src.partial_sigs
                dst.bip32_derivations = src.bip32_derivations
            p.inputs = rebuilt.inputs
            p.outputs = rebuilt.outputs

        tamper(
            psbt_b64,
            intended,
            flip_sequence,
            "input sequence does not match the intended transaction",
        )

    def test_signature_stripped_from_one_input(self):
        psbt_b64, _meta, intended = signed_container()
        tamper(
            psbt_b64,
            intended,
            lambda p: p.inputs[0].partial_sigs.clear(),
            "input is missing its signature",
        )

    def test_signature_from_a_different_key(self):
        # embit's finalizer does NOT check pubkeys (verified: it finalizes
        # this container into a witness) — the re-validation's deterministic
        # pubkey-vs-witness-program check is what catches it here.
        psbt_b64, _meta, intended = signed_container()
        extracted_anyway = finalizer.finalize_psbt(reparsed(psbt_b64))
        assert extracted_anyway is not None  # finalize alone is not a gate

        def wrong_key(p: PSBT) -> None:
            evil = bip32.HDKey.from_seed(EVIL_SEED).derive([0, 3]).key
            digest = p.sighash(0)
            stream = BytesIO()
            ec.Signature.write_to(evil.sign(digest), stream)
            p.inputs[0].partial_sigs.clear()
            p.inputs[0].partial_sigs[evil.get_public_key()] = (
                stream.getvalue() + b"\x01"
            )

        tamper(
            psbt_b64,
            intended,
            wrong_key,
            "signature public key does not match the input script",
        )

    def test_right_key_signature_over_wrong_digest(self):
        # A real signature from the RIGHT key but over a different message
        # (the non-consensus raw-program digest) — sig present, pubkey
        # matches, EC verify fails: the deepest signature check.
        psbt_b64, _meta, intended = signed_container()

        def wrong_digest(p: PSBT) -> None:
            (pub, derivation), = p.inputs[0].bip32_derivations.items()
            priv = bip32.HDKey.from_seed(SEED).derive(derivation.derivation).key
            program = p.inputs[0].witness_utxo.script_pubkey
            digest = p.tx.sighash_segwit(
                0, program, p.inputs[0].witness_utxo.value
            )
            stream = BytesIO()
            ec.Signature.write_to(priv.sign(digest), stream)
            p.inputs[0].partial_sigs[pub] = stream.getvalue() + b"\x01"

        tamper(
            psbt_b64,
            intended,
            wrong_digest,
            "signature does not verify against the transaction",
        )

    def test_corrupted_signature_der(self):
        psbt_b64, _meta, intended = signed_container()

        def corrupt(p: PSBT) -> None:
            (pub, _derivation), = p.inputs[0].partial_sigs.items()
            p.inputs[0].partial_sigs[pub] = b"\x30\x05\x02\x01\x00\x01"

        tamper(psbt_b64, intended, corrupt, "signature is malformed")

    def test_non_all_sighash_byte_refused(self):
        psbt_b64, _meta, intended = signed_container()

        def rehashtype(p: PSBT) -> None:
            (pub, sig), = p.inputs[0].partial_sigs.items()
            p.inputs[0].partial_sigs[pub] = sig[:-1] + b"\x82"  # SIGHASH.NONE

        tamper(psbt_b64, intended, rehashtype, "signature hash type is not supported")

    def test_garbage_base64(self):
        _psbt_b64, _meta, intended = signed_container()
        with pytest.raises(TamperedPsbtError) as exc:
            revalidate_signed_psbt("!!!definitely-not-base64!!!", intended)
        assert "not valid base64" in str(exc.value)

    def test_truncated_container(self):
        psbt_b64, _meta, intended = signed_container()
        raw = base64.b64decode(psbt_b64)
        truncated = base64.b64encode(raw[:-9]).decode()
        with pytest.raises(TamperedPsbtError) as exc:
            revalidate_signed_psbt(truncated, intended)
        assert "malformed or truncated" in str(exc.value)

    def test_psbt_magic_stripped(self):
        psbt_b64, _meta, intended = signed_container()
        raw = base64.b64decode(psbt_b64)
        stripped = base64.b64encode(raw[5:]).decode()
        with pytest.raises(TamperedPsbtError) as exc:
            revalidate_signed_psbt(stripped, intended)
        assert "magic bytes are missing or invalid" in str(exc.value)

    def test_trailing_garbage_refused(self):
        psbt_b64, _meta, intended = signed_container()
        raw = base64.b64decode(psbt_b64)
        trailing = base64.b64encode(raw + b"\x00").decode()
        with pytest.raises(TamperedPsbtError) as exc:
            revalidate_signed_psbt(trailing, intended)
        assert "malformed or truncated" in str(exc.value)

    def test_unsigned_psbt_passed_as_signed(self):
        inputs = [
            source("ab" * 32, 0, 40_000, index=3),
            source("cd" * 32, 0, 50_000, branch=1, index=4),
        ]
        (psbt, meta), selection = build_pair(inputs, 60_000)
        intended = IntendedTx(
            expected_recipient_outputs=((recipient_script(), 60_000),),
            expected_change=(spk(1, 7), selection.change_sats),
            expected_inputs_count=meta.inputs_count,
            expected_fee_sats=meta.expected_fee_sats,
            expected_sequence=SEQUENCE_RBF_ENABLED,
            expected_vsize_max=meta.vsize + 1,
            tx_ref="tx-ref-fixture-0003",
        )
        with pytest.raises(TamperedPsbtError) as exc:
            revalidate_signed_psbt(psbt_to_base64(psbt), intended)
        assert "input is missing its signature" in str(exc.value)


class TestTamperMatrixStructural:
    """Caught-but-untested check-5 rows pinned explicitly (regression pins)."""

    def test_prebuilt_final_scriptwitness_refused(self):
        # The PRIMARY anti-bypass check: a prebuilt witness can never be
        # proven to match, so it is refused and check 11 always wins.
        psbt_b64, _meta, intended = signed_container()

        def prefinalize(p: PSBT) -> None:
            (pub, sig), = p.inputs[0].partial_sigs.items()
            p.inputs[0].final_scriptwitness = Witness([sig[:-1], pub.sec()])

        tamper(
            psbt_b64,
            intended,
            prefinalize,
            "input carries unexpected finalization data",
        )

    def test_taproot_fields_refused(self):
        psbt_b64, _meta, intended = signed_container()

        def add_taproot(p: PSBT) -> None:
            p.inputs[0].taproot_internal_key = (
                bip32.HDKey.from_seed(SEED).derive([0, 0]).to_public().key
            )

        tamper(
            psbt_b64,
            intended,
            add_taproot,
            "input carries taproot fields outside the v1 policy",
        )

    def test_container_sighash_type_0x02_refused(self):
        psbt_b64, _meta, intended = signed_container()
        tamper(
            psbt_b64,
            intended,
            lambda p: setattr(p.inputs[0], "sighash_type", 0x02),
            "input sighash type field is unsupported",
        )

    def test_v2_container_refused(self):
        psbt_b64, _meta, intended = signed_container()
        tamper(
            psbt_b64,
            intended,
            lambda p: setattr(p, "version", 2),
            "unsupported psbt container version",
        )

    def test_empty_psbt_container_refused(self):
        _psbt_b64, _meta, intended = signed_container()
        empty = base64.b64encode(b"psbt\xff\x00").decode()
        with pytest.raises(TamperedPsbtError) as exc:
            revalidate_signed_psbt(empty, intended)
        assert "input count does not match the intended transaction" in str(exc.value)

    def test_duplicate_outpoint_inputs_refused(self):
        # Two inputs spending the SAME outpoint = a double-spend no node
        # would relay. Not caught transitively (each sig verifies), so check
        # 5 refuses explicitly — pinned so it does not regress.
        psbt_b64, _meta, intended = signed_container()

        def duplicate(p: PSBT) -> None:
            tx = p.tx
            tx.vin[1].txid = tx.vin[0].txid
            tx.vin[1].vout = tx.vin[0].vout
            rebuilt = PSBT(tx=tx)
            for dst, src in zip(rebuilt.inputs, p.inputs):
                dst.witness_utxo = src.witness_utxo
                dst.partial_sigs = src.partial_sigs
                dst.bip32_derivations = src.bip32_derivations
            # Both inputs now spend the same program/outpoint: re-sign both
            # so the ONLY reason this is refused is the duplicate-outpoint
            # check (clean regression pin).
            for i, inp in enumerate(rebuilt.inputs):
                (ppub, d), = inp.bip32_derivations.items()
                priv = bip32.HDKey.from_seed(SEED).derive(d.derivation).key
                digest = rebuilt.sighash(i)
                stream = BytesIO()
                ec.Signature.write_to(priv.sign(digest), stream)
                inp.partial_sigs[ppub] = stream.getvalue() + b"\x01"
            p.inputs = rebuilt.inputs
            p.outputs = rebuilt.outputs

        tamper(
            psbt_b64,
            intended,
            duplicate,
            "duplicate input outpoints are not allowed",
        )

    def test_tx_version_tampered_refused(self):
        psbt_b64, _meta, intended = signed_container()
        tamper(
            psbt_b64,
            intended,
            lambda p: setattr(p, "tx_version", 1),
            "unexpected transaction version",
        )

    def test_high_s_signature_refused_by_this_embit_build(self):
        # DEVIATION from ticket C3 ("high-S ACCEPTED"): this embit/libsecp
        # build's ``pubkey.verify`` rejects high-S, so the gate currently
        # refuses it (stricter than the BIP62 default-policy relay rule).
        # Pins the ACTUAL behavior; a future change may normalize-and-permit.
        psbt_b64, _meta, intended = signed_container()

        def to_high_s(p: PSBT) -> None:
            (pub, d), = p.inputs[0].bip32_derivations.items()
            priv = bip32.HDKey.from_seed(SEED).derive(d.derivation).key
            digest = p.sighash(0)
            stream = BytesIO()
            ec.Signature.write_to(priv.sign(digest), stream)
            low = stream.getvalue()
            p.inputs[0].partial_sigs[pub] = _high_s_signature_der(low) + b"\x01"

        tamper(
            psbt_b64,
            intended,
            to_high_s,
            "signature does not verify against the transaction",
        )


class TestDeterminism:
    def test_same_container_same_intent_same_result(self):
        psbt_b64, _meta, intended = signed_container()
        first = revalidate_signed_psbt(psbt_b64, intended)
        second = revalidate_signed_psbt(psbt_b64, intended)
        assert first == second

    def test_idempotent_reparse(self):
        # serialize → parse → serialize must be byte-identical (embit
        # round-trip) and revalidation must agree across the round trip.
        psbt_b64, _meta, intended = signed_container()
        round_tripped = to_b64(reparsed(psbt_b64))
        assert round_tripped == psbt_b64
        assert revalidate_signed_psbt(round_tripped, intended) == revalidate_signed_psbt(
            psbt_b64, intended
        )

    def test_result_is_frozen(self):
        psbt_b64, _meta, intended = signed_container()
        result = revalidate_signed_psbt(psbt_b64, intended)
        with pytest.raises(FrozenInstanceError):
            result.fee_sats += 1  # type: ignore[misc]


class TestValueFreeErrors:
    def test_tamper_messages_never_contain_addresses_or_amounts(self):
        psbt_b64, _meta, intended = signed_container()
        mutations = [
            lambda p: setattr(p.outputs[0], "value", p.outputs[0].value + 546),
            lambda p: setattr(
                p.outputs[0], "script_pubkey", script.Script(spk(0, 50))
            ),
            lambda p: p.outputs.pop(),
            lambda p: p.inputs.pop(),
            lambda p: p.inputs[0].partial_sigs.clear(),
        ]
        observed: list[str] = []
        for mutate in mutations:
            mutated = reparsed(psbt_b64)
            mutate(mutated)
            with pytest.raises(TamperedPsbtError) as exc:
                revalidate_signed_psbt(to_b64(mutated), intended)
            observed.append(str(exc.value))

        # Verbatim artifacts that must NEVER appear in tamper messages:
        # the addresses (bech32, from the fixture scripts) and the amounts.
        recipient_address_str = script.Script(recipient_script()).address(
            NETWORKS["test"]
        )
        change_address_str = script.Script(spk(1, 7)).address(NETWORKS["test"])
        forbidden = (
            recipient_address_str,
            change_address_str,
            "546",
            "60000",
            str(intended.expected_fee_sats),
            str(intended.expected_change[1]),
        )
        for message in observed:
            for artifact in forbidden:
                assert artifact not in message


class TestIntendedTxSanity:
    """Fail closed on malformed dispatcher-owned state (caller bugs)."""

    def _intended(self, **overrides) -> IntendedTx:
        fields: dict = {
            "expected_recipient_outputs": ((recipient_script(), 60_000),),
            "expected_change": None,
            "expected_inputs_count": 1,
            "expected_fee_sats": 282,
            "expected_sequence": SEQUENCE_RBF_ENABLED,
            "expected_vsize_max": 142,
            "tx_ref": "tx-ref-fixture-0004",
        }
        fields.update(overrides)
        return IntendedTx(**fields)

    def test_empty_recipients_refused(self):
        with pytest.raises(TamperedPsbtError) as exc:
            revalidate_signed_psbt("", self._intended(expected_recipient_outputs=()))
        assert "recipient outputs" in str(exc.value)

    def test_zero_fee_refused(self):
        with pytest.raises(TamperedPsbtError) as exc:
            revalidate_signed_psbt("cHNib", self._intended(expected_fee_sats=0))
        assert "intended transaction fee" in str(exc.value)

    def test_zero_vsize_max_refused(self):
        with pytest.raises(TamperedPsbtError) as exc:
            revalidate_signed_psbt("cHNib", self._intended(expected_vsize_max=0))
        assert "vsize maximum" in str(exc.value)

    def test_non_integer_inputs_count_refused(self):
        with pytest.raises(TamperedPsbtError) as exc:
            revalidate_signed_psbt("cHNib", self._intended(expected_inputs_count="2"))
        assert "inputs count" in str(exc.value)

    def test_empty_tx_ref_refused(self):
        with pytest.raises(TamperedPsbtError) as exc:
            revalidate_signed_psbt("cHNib", self._intended(tx_ref=""))
        assert "intended transaction reference" in str(exc.value)

    def test_non_intended_object_refused(self):
        with pytest.raises(TamperedPsbtError) as exc:
            revalidate_signed_psbt("cHNib", "not-an-intent")  # type: ignore[arg-type]
        assert "IntendedTx" in str(exc.value)

    def test_non_string_base64_refused(self):
        with pytest.raises(TamperedPsbtError) as exc:
            revalidate_signed_psbt(b"cHNib", self._intended())  # type: ignore[arg-type]
        assert "non-empty base64 string" in str(exc.value)

    def test_empty_string_base64_refused(self):
        with pytest.raises(TamperedPsbtError) as exc:
            revalidate_signed_psbt("", self._intended())
        assert "non-empty base64 string" in str(exc.value)

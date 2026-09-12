"""Unsigned-PSBT tests (TCK-P2-002).

Covers:

- building the unsigned PSBT (witness_utxo + bip32 derivations per input,
  RBF-signaling sequence on every input, recipients-then-change output
  order) and its ``PsbtMeta``;
- ``validate_psbt_shape`` passing on well-formed output and failing
  closed on every tampered dimension (output script/value, input count,
  sequence policy, fee, witness UTXO);
- byte-level determinism and round-trip through embit's PSBT parser
  (including the base64 exchange form);
- watch-only, mainnet-gate, dust, and min-relay policy refusals;
- vsize verification: the estimate equals the vsize of the embit-built
  transaction with maximal P2WPKH witnesses EXACTLY, and matches a really
  signed fixture (throwaway key from the public BIP32 test vector 1 seed,
  signed in-tests only — no real funds, watch-only app) within 1 vB.
"""

import base64
import math
from io import BytesIO

import pytest
from embit import bip32, ec, finalizer, script
from embit.networks import NETWORKS
from embit.psbt import PSBT
from embit.transaction import Transaction, TransactionInput, TransactionOutput, Witness

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
from localwallet.tx.selection import P2WPKH_INPUT_WEIGHT_WU, select_coins

# Public BIP32 test vector 1 seed — throwaway fixture material only.
# Mainnet coin type 0 (ADR-0021): the tx engine refuses testnet change
# addresses, so the fixture account is the canonical mainnet BIP84 path.
SEED = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
ACCOUNT_PATH = (84 + 2**31, 0 + 2**31, 2**31)


def account_key() -> bip32.HDKey:
    return bip32.HDKey.from_seed(SEED).derive(list(ACCOUNT_PATH)).to_public()


def fingerprint() -> bytes:
    return account_key().my_fingerprint


def spk(branch: int, index: int) -> bytes:
    key = account_key().derive([branch, index]).key
    return script.p2wpkh(key).data


def change_address(index: int = 7) -> str:
    key = account_key().derive([1, index]).key
    return script.p2wpkh(key).address(NETWORKS["main"])


def recipient_script() -> bytes:
    return spk(0, 99)


def source(txid: str, vout: int, value_sats: int, branch: int = 0, index: int | None = None) -> PsbtInputSource:
    return PsbtInputSource(
        txid=txid,
        vout=vout,
        value_sats=value_sats,
        script_pubkey=spk(branch, index if index is not None else 3),
        branch=branch,
        index=index if index is not None else 3,
    )


def build_pair(inputs, amount_sats, rate=2, with_change=True):
    """Selection + PSBT build through the real public API."""
    result = select_coins(
        inputs, amount_sats, rate * 100, 8 + 1 + 22, recipient_script()  # sat/vB->centisat
    )
    return build_unsigned_psbt(
        result.selected,
        [(recipient_script(), amount_sats)],
        change_address() if with_change and result.change_sats is not None else None,
        result.change_sats if with_change else None,
        account_key=account_key(),
        account_fingerprint=fingerprint(),
        account_path=ACCOUNT_PATH,
        change_index=7,  # the index change_address() derives
    ), result


class TestBuildAndMeta:
    def test_two_inputs_with_change(self):
        # 40k + 50k: neither coin alone covers 60k + fee, so the greedy
        # pair stands and the improvement pass cannot reduce it.
        inputs = [source("ab" * 32, 0, 40_000, index=3), source("cd" * 32, 0, 50_000, branch=1, index=4)]
        (psbt, meta), selection = build_pair(inputs, 60_000)

        assert meta.inputs_count == 2
        assert len(psbt.inputs) == 2
        assert len(psbt.tx.vin) == 2
        # Canonical deterministic input order (txid, vout): "ab.." < "cd.."
        assert psbt.tx.vin[0].txid == bytes(reversed(bytes.fromhex("ab" * 32)))
        assert psbt.tx.vin[1].txid == bytes(reversed(bytes.fromhex("cd" * 32)))
        # RBF policy on every input (module docstring / ADR-0012).
        assert [v.sequence for v in psbt.tx.vin] == [SEQUENCE_RBF_ENABLED] * 2
        assert [i.sequence for i in psbt.inputs] == [SEQUENCE_RBF_ENABLED] * 2
        # Outputs: recipient first, change last.
        assert len(psbt.tx.vout) == 2
        assert bytes(psbt.tx.vout[0].script_pubkey.data) == recipient_script()
        assert psbt.tx.vout[0].value == 60_000
        assert bytes(psbt.tx.vout[1].script_pubkey.data) == spk(1, 7)
        assert psbt.tx.vout[1].value == selection.change_sats
        # Meta agrees with the built transaction.
        assert meta.expected_outputs == (
            (recipient_script(), 60_000),
            (spk(1, 7), selection.change_sats),
        )
        assert meta.expected_fee_sats == 90_000 - 60_000 - selection.change_sats
        assert meta.vsize == selection.estimated_vsize
        # Witness UTXOs carry value + script (receive- and change-branch).
        assert psbt.inputs[0].witness_utxo.value == 40_000
        assert bytes(psbt.inputs[0].witness_utxo.script_pubkey.data) == spk(0, 3)
        assert psbt.inputs[1].witness_utxo.value == 50_000
        assert bytes(psbt.inputs[1].witness_utxo.script_pubkey.data) == spk(1, 4)

    def test_bip32_derivations_per_input(self):
        inputs = [source("ab" * 32, 0, 200_000, index=3)]
        (psbt, _meta), _ = build_pair(inputs, 60_000)
        scope = psbt.inputs[0]
        (pub, derivation), = scope.bip32_derivations.items()
        assert pub.sec() == account_key().derive([0, 3]).key.sec()
        assert derivation.fingerprint == fingerprint()
        assert derivation.derivation == list(ACCOUNT_PATH) + [0, 3]

    def test_no_change_outputs_exactly_recipient(self):
        inputs = [source("ab" * 32, 0, 60_000 + 282)]
        (psbt, meta), selection = build_pair(inputs, 60_000, with_change=False)
        assert len(psbt.tx.vout) == 1
        assert meta.expected_outputs == ((recipient_script(), 60_000),)
        assert meta.expected_fee_sats == selection.fee_sats
        assert meta.expected_fee_sats == 282  # the folded residue
        validate_psbt_shape(psbt, meta)

    def test_locktime_is_serialized(self):
        inputs = [source("ab" * 32, 0, 200_000)]
        (psbt, _meta), _ = build_pair(inputs, 60_000)
        assert psbt.tx.locktime == 0
        assert psbt.tx.version == 2

    def test_conservation_invariant_at_meta_boundary(self):
        # B2: the builder asserts inputs_total == outputs + expected fee at
        # the PsbtMeta boundary (AssertionError by design — invariant, not
        # error handling). Every build in this suite exercises the assert;
        # this pins the conserved quantities observably.
        inputs = [source("ab" * 32, 0, 40_000, index=3), source("cd" * 32, 0, 50_000, branch=1, index=4)]
        (_psbt, meta), selection = build_pair(inputs, 60_000)
        change = selection.change_sats if selection.change_sats is not None else 0
        assert selection.inputs_total == 60_000 + selection.fee_sats + change
        assert meta.expected_fee_sats == selection.fee_sats
        assert (
            sum(value for _script, value in meta.expected_outputs)
            + meta.expected_fee_sats
            == selection.inputs_total
        )


class TestDeterminismAndRoundTrip:
    def test_rebuild_is_byte_identical(self):
        inputs = [source("ab" * 32, 0, 40_000, index=3), source("cd" * 32, 0, 50_000, branch=1, index=4)]
        (psbt_a, _), _ = build_pair(inputs, 60_000)
        (psbt_b, _), _ = build_pair(inputs, 60_000)
        assert psbt_a.serialize() == psbt_b.serialize()

    def test_round_trip_through_embit_parse(self):
        inputs = [source("ab" * 32, 0, 40_000, index=3), source("cd" * 32, 0, 50_000, branch=1, index=4)]
        (psbt, meta), _ = build_pair(inputs, 60_000)
        parsed = PSBT.parse(psbt.serialize())

        assert len(parsed.inputs) == meta.inputs_count
        assert len(parsed.outputs) == len(meta.expected_outputs)
        for i, (want_script, want_value) in enumerate(meta.expected_outputs):
            assert bytes(parsed.tx.vout[i].script_pubkey.data) == want_script
            assert parsed.tx.vout[i].value == want_value
            assert parsed.outputs[i].value == want_value
        for original, replica in zip(psbt.inputs, parsed.inputs):
            assert replica.witness_utxo.value == original.witness_utxo.value
            assert bytes(replica.witness_utxo.script_pubkey.data) == bytes(
                original.witness_utxo.script_pubkey.data
            )
            assert replica.sequence == SEQUENCE_RBF_ENABLED
            for pub, derivation in original.bip32_derivations.items():
                assert replica.bip32_derivations[pub].derivation == derivation.derivation
                assert replica.bip32_derivations[pub].fingerprint == derivation.fingerprint

    def test_base64_form_round_trips(self):
        inputs = [source("ab" * 32, 0, 200_000)]
        (psbt, _), _ = build_pair(inputs, 60_000)
        b64 = psbt_to_base64(psbt)
        assert base64.b64decode(b64) == psbt.serialize()
        parsed = PSBT.parse(base64.b64decode(b64))
        assert parsed.tx.serialize() == psbt.tx.serialize()


class TestValidateShapeFailClosed:
    def _pair(self):
        inputs = [source("ab" * 32, 0, 40_000, index=3), source("cd" * 32, 0, 50_000, branch=1, index=4)]
        (psbt, meta), _ = build_pair(inputs, 60_000)
        return psbt, meta

    def test_well_formed_passes(self):
        psbt, meta = self._pair()
        validate_psbt_shape(psbt, meta)  # no raise

    def test_tampered_output_value_detected(self):
        # embit's PSBT.tx is rebuilt from scope fields on every access, so
        # tampering goes through the scope (the exchange-format truth).
        psbt, meta = self._pair()
        psbt.outputs[-1].value += 1
        with pytest.raises(PsbtValidationError):
            validate_psbt_shape(psbt, meta)

    def test_tampered_output_script_detected(self):
        psbt, meta = self._pair()
        psbt.outputs[0].script_pubkey = script.Script(spk(0, 50))
        with pytest.raises(PsbtValidationError):
            validate_psbt_shape(psbt, meta)

    def test_tampered_sequence_detected(self):
        psbt, meta = self._pair()
        psbt.inputs[0].sequence = 0xFFFFFFFE
        with pytest.raises(PsbtValidationError):
            validate_psbt_shape(psbt, meta)

    def test_missing_witness_utxo_detected(self):
        psbt, meta = self._pair()
        psbt.inputs[0].witness_utxo = None
        with pytest.raises(PsbtValidationError):
            validate_psbt_shape(psbt, meta)

    def test_fee_mismatch_detected(self):
        psbt, meta = self._pair()
        psbt.inputs[0].witness_utxo.value -= 1
        with pytest.raises(PsbtValidationError):
            validate_psbt_shape(psbt, meta)

    def test_input_count_mismatch_detected(self):
        psbt, meta = self._pair()
        broken = PsbtMeta(
            expected_outputs=meta.expected_outputs,
            expected_fee_sats=meta.expected_fee_sats,
            vsize=meta.vsize,
            inputs_count=meta.inputs_count + 1,
        )
        with pytest.raises(PsbtValidationError):
            validate_psbt_shape(psbt, broken)

    def test_extra_output_detected(self):
        psbt, meta = self._pair()
        psbt.outputs.append(psbt.outputs[0])
        with pytest.raises(PsbtValidationError):
            validate_psbt_shape(psbt, meta)


class TestPolicyRefusals:
    def test_private_account_key_refused(self):
        private = bip32.HDKey.from_seed(SEED).derive(list(ACCOUNT_PATH))
        with pytest.raises(PsbtError) as exc:
            build_unsigned_psbt(
                [source("ab" * 32, 0, 200_000)],
                [(recipient_script(), 60_000)],
                None,
                None,
                account_key=private,
                account_fingerprint=fingerprint(),
                account_path=ACCOUNT_PATH,
            )
        assert "private" in str(exc.value)

    def test_testnet_change_address_refused(self):
        # tb1q... = testnet bech32; the tx engine is mainnet-only (ADR-0021).
        with pytest.raises(PsbtError, match="mainnet bech32"):
            build_unsigned_psbt(
                [source("ab" * 32, 0, 200_000)],
                [(recipient_script(), 60_000)],
                "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx",
                100_000,
                account_key=account_key(),
                account_fingerprint=fingerprint(),
                account_path=ACCOUNT_PATH,
            )

    def test_mainnet_change_address_accepted(self):
        # bc1q... mainnet bech32 change is the acceptance case (ADR-0021).
        psbt, meta = build_unsigned_psbt(
            [source("ab" * 32, 0, 200_000)],
            [(recipient_script(), 60_000)],
            change_address(),
            100_000,
            account_key=account_key(),
            account_fingerprint=fingerprint(),
            account_path=ACCOUNT_PATH,
            change_index=7,
        )
        change_script = script.p2wpkh(account_key().derive([1, 7]).key).data
        assert bytes(psbt.tx.vout[1].script_pubkey.data) == change_script
        assert psbt.tx.vout[1].value == 100_000
        validate_psbt_shape(psbt, meta)

    def test_malformed_change_address_refused(self):
        with pytest.raises(PsbtError):
            build_unsigned_psbt(
                [source("ab" * 32, 0, 200_000)],
                [(recipient_script(), 60_000)],
                "bc1qinvalid",
                100_000,
                account_key=account_key(),
                account_fingerprint=fingerprint(),
                account_path=ACCOUNT_PATH,
            )

    def test_change_below_dust_refused(self):
        with pytest.raises(PsbtError):
            build_unsigned_psbt(
                [source("ab" * 32, 0, 200_000)],
                [(recipient_script(), 60_000)],
                change_address(),
                100,  # < 294 computed from the 22-byte change script
                account_key=account_key(),
                account_fingerprint=fingerprint(),
                account_path=ACCOUNT_PATH,
            )

    def test_change_args_must_be_given_together(self):
        with pytest.raises(PsbtError):
            build_unsigned_psbt(
                [source("ab" * 32, 0, 200_000)],
                [(recipient_script(), 60_000)],
                change_address(),
                None,
                account_key=account_key(),
                account_fingerprint=fingerprint(),
                account_path=ACCOUNT_PATH,
            )

    def test_recipient_below_dust_refused(self):
        with pytest.raises(PsbtError):
            build_unsigned_psbt(
                [source("ab" * 32, 0, 200_000)],
                [(recipient_script(), 100)],
                None,
                None,
                account_key=account_key(),
                account_fingerprint=fingerprint(),
                account_path=ACCOUNT_PATH,
            )

    def test_op_return_recipient_refused(self):
        with pytest.raises(PsbtError):
            build_unsigned_psbt(
                [source("ab" * 32, 0, 200_000)],
                [(b"\x6a\x04test", 1_000)],
                None,
                None,
                account_key=account_key(),
                account_fingerprint=fingerprint(),
                account_path=ACCOUNT_PATH,
            )

    def test_non_rbf_sequence_refused(self):
        with pytest.raises(PsbtError):
            build_unsigned_psbt(
                [source("ab" * 32, 0, 200_000)],
                [(recipient_script(), 60_000)],
                None,
                None,
                account_key=account_key(),
                account_fingerprint=fingerprint(),
                account_path=ACCOUNT_PATH,
                sequence=0xFFFFFFFE,
            )

    def test_fee_below_min_relay_refused(self):
        # 300 sats in, 294 out: fee 6 sats is below the 110-vB floor at 1 sat/vB.
        with pytest.raises(PsbtError):
            build_unsigned_psbt(
                [source("ab" * 32, 0, 300)],
                [(recipient_script(), 294)],
                None,
                None,
                account_key=account_key(),
                account_fingerprint=fingerprint(),
                account_path=ACCOUNT_PATH,
            )

    def test_inputs_total_above_max_money_refused(self):
        # B4 corrupt-snapshot guard: each input respects MAX_MONEY
        # individually, but the SUM must also (fail closed, value-free).
        max_money = 2_100_000_000_000_000
        with pytest.raises(PsbtError) as exc:
            build_unsigned_psbt(
                [
                    source("ab" * 32, 0, max_money),
                    source("cd" * 32, 0, max_money, branch=1, index=4),
                ],
                [(recipient_script(), 60_000)],
                None,
                None,
                account_key=account_key(),
                account_fingerprint=fingerprint(),
                account_path=ACCOUNT_PATH,
            )
        message = str(exc.value)
        assert "input value out of range" in message
        assert str(max_money) not in message  # value-free
        assert "4200000000000000" not in message

    def test_script_derivation_mismatch_refused(self):
        bad = PsbtInputSource(
            txid="ab" * 32,
            vout=0,
            value_sats=200_000,
            script_pubkey=spk(0, 50),  # belongs to index 50, claims index 3
            branch=0,
            index=3,
        )
        with pytest.raises(PsbtError):
            build_unsigned_psbt(
                [bad],
                [(recipient_script(), 60_000)],
                None,
                None,
                account_key=account_key(),
                account_fingerprint=fingerprint(),
                account_path=ACCOUNT_PATH,
            )

    def test_non_p2wpkh_input_script_refused(self):
        legacy = b"\x76\xa9\x14" + b"\x11" * 20 + b"\x88\xac"
        bad = PsbtInputSource("ab" * 32, 0, 200_000, legacy, 0, 3)
        with pytest.raises(PsbtError):
            build_unsigned_psbt(
                [bad],
                [(recipient_script(), 60_000)],
                None,
                None,
                account_key=account_key(),
                account_fingerprint=fingerprint(),
                account_path=ACCOUNT_PATH,
            )

    def test_duplicate_input_refused(self):
        with pytest.raises(PsbtError):
            build_unsigned_psbt(
                [source("ab" * 32, 0, 200_000), source("ab" * 32, 0, 200_000)],
                [(recipient_script(), 60_000)],
                None,
                None,
                account_key=account_key(),
                account_fingerprint=fingerprint(),
                account_path=ACCOUNT_PATH,
            )


class TestVsizeAgainstReallyBuiltTransactions:
    """Estimate vs embit reality: exact under max-witness, ±1 signed."""

    @staticmethod
    def _embit_signed_vsize(psbt: PSBT, privkey: ec.PrivateKey, spk_bytes: bytes, value: int) -> int:
        digest = psbt.tx.sighash_segwit(0, script.Script(spk_bytes), value)
        stream = BytesIO()
        ec.Signature.write_to(privkey.sign(digest), stream)
        psbt.inputs[0].partial_sigs[privkey.get_public_key()] = stream.getvalue() + b"\x01"
        signed = finalizer.finalize_psbt(psbt)
        assert signed is not None
        witnesses = [vin.witness for vin in signed.vin]
        for vin in signed.vin:
            vin.witness = Witness([])
        stripped = len(signed.serialize())
        for vin, wit in zip(signed.vin, witnesses):
            vin.witness = wit
        full = len(signed.serialize())
        return math.ceil((3 * stripped + full) / 4), len(
            psbt.inputs[0].partial_sigs[privkey.get_public_key()]
        )

    def test_max_witness_estimate_matches_embit_exactly(self):
        (_psbt, meta), selection = build_pair([source("ab" * 32, 0, 200_000)], 60_000)
        # Rebuild the identical transaction explicitly in embit, attach
        # maximal P2WPKH witnesses, and measure the serialization.
        tx = Transaction(
            version=2,
            vin=[
                TransactionInput(
                    txid=bytes(reversed(bytes.fromhex("ab" * 32))),
                    vout=0,
                    sequence=SEQUENCE_RBF_ENABLED,
                )
            ],
            vout=[
                TransactionOutput(value, script.Script(spk_bytes))
                for spk_bytes, value in meta.expected_outputs
            ],
            locktime=0,
        )
        stripped = len(tx.serialize())
        for vin in tx.vin:
            vin.witness = Witness([b"\x00" * 72, b"\x00" * 33])
        full = len(tx.serialize())
        weight = 3 * stripped + full
        assert meta.vsize == math.ceil(weight / 4)
        assert selection.estimated_vsize == meta.vsize
        # And the component constant agrees with the same measurement.
        assert P2WPKH_INPUT_WEIGHT_WU == 272

    def test_signed_fixture_vsize_within_one_vbyte(self):
        (psbt, meta), _ = build_pair([source("ab" * 32, 0, 200_000, index=3)], 60_000)
        privkey = bip32.HDKey.from_seed(SEED).derive(
            list(ACCOUNT_PATH) + [0, 3]
        ).key
        assert privkey.is_private
        actual_vsize, sig_content_len = self._embit_signed_vsize(
            psbt, privkey, spk(0, 3), 200_000
        )
        # Real ECDSA signatures are usually 1 byte shorter than the
        # 72-byte max-DER+hashtype convention (measured: 71-byte content).
        assert sig_content_len in (71, 72, 73)
        assert abs(actual_vsize - meta.vsize) <= 1

"""TCK-HW-003 — change-output derivations + sign-time master-fp patch.

The MW-4 debugger reproducer, in-process. Root cause (verified against
hwi 3.2.0 + jadepy): our PSBT's ``bip32_derivations`` carried the
ACCOUNT-key fingerprint paired with a master-rooted path; Jade matches
inputs on ``origin.fingerprint == master_fp`` (jade.py:194/210 legacy fw;
native fw the same lookup in firmware, jade.py:373-391) — with the
account fp NO input is relevant and the device signs nothing ("There are
not relevant inputs to be signed"). Change OUTPUT derivations were never
emitted at all; Jade flags change by matching psbtout hd_keypaths against
its master fp (jade.py:296-316), so change showed as a plain external
address.

Covers (ticket criteria):

a. pre-sign: every input AND the change output carry the account fp; the
   change derivation is the exact path ``m/84'/0'/0'/1/{change_index}``;
b. Jade's relevance predicate replayed in-process: pre-patch ZERO inputs
   are signable (the device's "no relevant inputs", reproduced);
c. the sign-time patch (fake bound client reporting a master fp) flips
   the predicate to every input, both scopes — paths and pubkeys
   byte-identical;
d. ``revalidate_signed_psbt`` on patched-then-signed == unpatched-signed
   (same RevalidatedTx): the patch is invisible to the BIP-143 digest
   and to the gate (revalidate reads no bip32_derivations);
e. the patch is fingerprint-TARGETED: an entry with a foreign fingerprint
   is left untouched; a client that cannot report its master fingerprint
   fails closed (signtx never sees the PSBT, guidance is value-free).
"""

import base64
from types import SimpleNamespace

import pytest
from embit.psbt import PSBT

from localwallet.signer.hwi import DeviceError, HwiUsbSigner
from localwallet.tx.psbt import (
    PsbtError,
    build_unsigned_psbt,
    psbt_to_base64,
)
from localwallet.tx.revalidate import IntendedTx, revalidate_signed_psbt
from tests.test_tx_psbt import (
    ACCOUNT_PATH,
    account_key,
    build_pair,
    change_address,
    fingerprint,
    recipient_script,
    source,
    spk,
)
from tests.test_tx_revalidate import sign_psbt

# The device's MASTER fingerprint — deliberately ≠ the account fp, the
# MW-4 Jade shape (master vs descriptor account origin).
MASTER_FP = bytes.fromhex("deadbeef")
FOREIGN_FP = bytes.fromhex("feedface")  # a different wallet in the container
CHANGE_INDEX = 7  # the index tests.test_tx_psbt.change_address() derives

_ACCOUNT_PATH_STR = "m/84'/0'/0'"


def fixture_pair():
    """One receive input + one recipient + change at branch 1 index 7."""
    return build_pair([source("ab" * 32, 0, 200_000, index=3)], 60_000)


def jade_signable_inputs(psbt: PSBT, master_fp: bytes) -> int:
    """Jade's relevance predicate (jade.py:194/210): an input is signable
    iff a derivation origin carries the device MASTER fingerprint and a
    non-empty path."""
    return sum(
        1
        for scope in psbt.inputs
        for der in scope.bip32_derivations.values()
        if der.fingerprint == master_fp and len(der.derivation) > 0
    )


def derivations(psbt: PSBT):
    return [
        der
        for scope in (*psbt.inputs, *psbt.outputs)
        for der in scope.bip32_derivations.values()
    ]


def reparsed(psbt_b64: str) -> PSBT:
    return PSBT.parse(base64.b64decode(psbt_b64))


class _MasterFpClient:
    """Bound-device stand-in: serves the wallet's account key at the
    account path (binds) and reports ``master_fp`` as its master
    fingerprint. An Exception ``master_fp`` raises from the getter."""

    def __init__(self, master_fp: object = MASTER_FP) -> None:
        self._master_fp = master_fp
        self.master_fp_calls = 0

    def get_pubkey_at_path(self, bip32_path: str):
        assert bip32_path == _ACCOUNT_PATH_STR
        return SimpleNamespace(pubkey=account_key().key.sec())

    def get_master_fingerprint(self) -> object:
        self.master_fp_calls += 1
        if isinstance(self._master_fp, Exception):
            raise self._master_fp
        return self._master_fp

    def close(self) -> None:
        pass


class _DeviceCommands:
    """hwilib.commands stand-in: records what signtx received and (unless
    ``sign=False``) signs it with the fixture key, like the e2e fake
    device."""

    def __init__(self, client: object, *, sign: bool = True) -> None:
        self.client = client
        self.sign = sign
        self.received: list[str] = []

    def enumerate(self, password=None):
        assert password is None
        return [
            {
                "type": "jade",
                "path": "/dev/jade",
                "model": "jade",
                "fingerprint": MASTER_FP.hex(),
            }
        ]

    def get_client(self, device_type, device_path, password=None, chain=None):
        return self.client

    def signtx(self, client, psbt):
        self.received.append(psbt)
        if not self.sign:
            return {"psbt": psbt}
        return {"psbt": psbt_to_base64(sign_psbt(reparsed(psbt)))}


def make_signer(client: object, *, sign: bool = True):
    commands = _DeviceCommands(client, sign=sign)
    signer = HwiUsbSigner(
        fingerprint().hex(), _ACCOUNT_PATH_STR, commands_module=commands
    )
    return signer, commands


class TestBuildTimeState:
    def test_a_inputs_carry_account_fp_and_change_output_is_derived(self):
        (psbt, _meta), _selection = fixture_pair()
        acct = fingerprint()
        assert len(psbt.inputs) == 1
        assert all(der.fingerprint == acct for der in derivations(psbt))
        # The change output (last) carries its wallet derivation.
        (pub, der), = psbt.outputs[-1].bip32_derivations.items()
        assert der.fingerprint == acct
        assert der.derivation == list(ACCOUNT_PATH) + [1, CHANGE_INDEX]
        assert pub.sec() == account_key().derive([1, CHANGE_INDEX]).key.sec()
        # The recipient output is NOT the wallet's: no derivation.
        assert psbt.outputs[0].bip32_derivations == {}

    def test_change_without_change_index_refused(self):
        # Fail closed: a change output with no coordinates cannot be
        # labeled for the device — the silent hole that shipped MW-4.
        with pytest.raises(PsbtError, match="change_index is required"):
            build_unsigned_psbt(
                [source("ab" * 32, 0, 200_000, index=3)],
                [(recipient_script(), 60_000)],
                change_address(),
                139_718,
                account_key=account_key(),
                account_fingerprint=fingerprint(),
                account_path=ACCOUNT_PATH,
            )

    def test_change_index_mismatching_address_refused(self):
        with pytest.raises(PsbtError, match="does not match"):
            build_unsigned_psbt(
                [source("ab" * 32, 0, 200_000, index=3)],
                [(recipient_script(), 60_000)],
                change_address(CHANGE_INDEX),
                139_718,
                account_key=account_key(),
                account_fingerprint=fingerprint(),
                account_path=ACCOUNT_PATH,
                change_index=CHANGE_INDEX + 1,
            )

    def test_b_jade_predicate_finds_nothing_pre_patch(self):
        (psbt, _meta), _selection = fixture_pair()
        # The debugger repro in-process: the real Jade signed ZERO inputs
        # with this exact container (account fp ≠ master fp).
        assert jade_signable_inputs(psbt, MASTER_FP) == 0


class TestSignTimePatch:
    def test_c_predicate_passes_for_both_scopes_after_patch(self):
        (psbt, _meta), _selection = fixture_pair()
        signer, commands = make_signer(_MasterFpClient())

        signer.sign_unsigned(psbt_to_base64(psbt))

        sent = reparsed(commands.received[0])
        assert jade_signable_inputs(sent, MASTER_FP) == len(sent.inputs)
        assert all(der.fingerprint == MASTER_FP for der in derivations(sent))
        # Change recognized: the output derivation carries the master fp.
        assert (
            next(iter(sent.outputs[-1].bip32_derivations.values())).fingerprint
            == MASTER_FP
        )
        # Paths and pubkeys are UNTOUCHED (fingerprints only).
        for scope_sent, scope_built in zip(
            (*sent.inputs, *sent.outputs), (*psbt.inputs, *psbt.outputs)
        ):
            assert list(scope_sent.bip32_derivations) == list(
                scope_built.bip32_derivations
            )
            assert [
                d.derivation for d in scope_sent.bip32_derivations.values()
            ] == [d.derivation for d in scope_built.bip32_derivations.values()]

    def test_d_revalidate_verdict_unchanged_by_the_patch(self):
        (psbt, meta), selection = fixture_pair()
        intended = IntendedTx(
            expected_recipient_outputs=((recipient_script(), 60_000),),
            expected_change=(spk(1, CHANGE_INDEX), selection.change_sats),
            expected_inputs_count=meta.inputs_count,
            expected_fee_sats=meta.expected_fee_sats,
            expected_sequence=0xFFFFFFFD,
            expected_vsize_max=meta.vsize + 1,
            tx_ref="hw003-fixture",
        )
        psbt_b64 = psbt_to_base64(psbt)
        # Reference flow: sign the account-fp container directly (no
        # signer involved) and re-validate.
        reference = revalidate_signed_psbt(
            psbt_to_base64(sign_psbt(reparsed(psbt_b64))), intended
        )
        # Patched flow: the signer patches, the fake device signs what it
        # actually received, the gate re-validates.
        signer, commands = make_signer(_MasterFpClient())
        signed = signer.sign_unsigned(psbt_b64)
        patched = revalidate_signed_psbt(signed.psbt_base64, intended)
        # Same verdict, same txid: the patch is invisible to the BIP-143
        # digest and to the gate (revalidate reads no bip32_derivations).
        assert patched == reference
        assert commands.received  # the device really saw the patched bytes

    def test_e_foreign_fingerprint_left_untouched(self):
        (psbt, _meta), _selection = fixture_pair()
        # Mix another wallet's derivation into the input (foreign fp; the
        # path/pubkey stay the wallet's — this only tests targeting).
        foreign = reparsed(psbt_to_base64(psbt))
        next(iter(foreign.inputs[0].bip32_derivations.values())).fingerprint = (
            FOREIGN_FP
        )
        signer, commands = make_signer(_MasterFpClient())
        signer.sign_unsigned(psbt_to_base64(foreign))
        sent = reparsed(commands.received[0])
        # The foreign-tagged input entry was NOT rewritten…
        assert (
            next(iter(sent.inputs[0].bip32_derivations.values())).fingerprint
            == FOREIGN_FP
        )
        # …while this wallet's change-output derivation was.
        assert [
            d.fingerprint for d in sent.outputs[-1].bip32_derivations.values()
        ] == [MASTER_FP]

    def test_unparseable_psbt_aborts_before_the_device(self):
        signer, commands = make_signer(_MasterFpClient())
        with pytest.raises(DeviceError):
            signer.sign_unsigned(base64.b64encode(b"psbt\xffgarbage").decode())
        assert commands.received == []

    def test_no_derivation_psbt_passes_through_verbatim(self):
        # A container without this wallet's derivations is NOT rewritten
        # or re-serialized, and the master getter is never called.
        client = _MasterFpClient()
        signer, commands = make_signer(client, sign=False)
        bare = reparsed(psbt_to_base64(fixture_pair()[0][0]))
        for scope in (*bare.inputs, *bare.outputs):
            scope.bip32_derivations = {}
        bare_b64 = psbt_to_base64(bare)
        signer.sign_unsigned(bare_b64)
        assert commands.received[0] == bare_b64
        assert client.master_fp_calls == 0


class TestFailClosed:
    @pytest.mark.parametrize(
        "master_fp",
        [
            RuntimeError("jade refuses"),  # device-side failure
            b"\x01\x02",  # wrong length
            "not-hex",  # unusable shape
            None,  # no answer
        ],
        ids=["raises", "short", "not-hex", "none"],
    )
    def test_master_fp_failure_aborts_before_signing(self, master_fp):
        (psbt, _meta), _selection = fixture_pair()
        signer, commands = make_signer(_MasterFpClient(master_fp))
        with pytest.raises(DeviceError) as excinfo:
            signer.sign_unsigned(psbt_to_base64(psbt))
        assert commands.received == []  # the un-patched PSBT never ships
        # Value-free: no fingerprints in the guidance.
        message = str(excinfo.value)
        assert fingerprint().hex() not in message
        assert MASTER_FP.hex() not in message

    def test_client_without_master_getter_fails_closed(self):
        class _NoGetter(_MasterFpClient):
            get_master_fingerprint = None  # legacy/stripped client shape

        (psbt, _meta), _selection = fixture_pair()
        signer, commands = make_signer(_NoGetter())
        with pytest.raises(DeviceError):
            signer.sign_unsigned(psbt_to_base64(psbt))
        assert commands.received == []

"""Phase 2 Sparrow acceptance harness (TCK-P2-006).

The Phase 2 AC (PROJECT.md §12) — *"produced unsigned PSBT loads correctly
in an external tool (Sparrow) with matching outputs/fee"* — has an
inherently MANUAL external step: a human opens Sparrow and imports the
file. This module makes that verification deterministic, documented, and as
automated as possible. The manual procedure lives in ``docs/sparrow-ac.md``;
this file is its offline determinism harness plus an env-gated artifact
dump so the human can grab the exact fixture PSBT for import.

DETERMINISM VERDICT (documented per TCK-P2-006)
-----------------------------------------------
The fixture PSBT is **BYTE-IDENTICAL** across builds, so the base64 form is
pinned verbatim in :data:`CANONICAL_BASE64` and asserted byte-for-byte.
The builder (``src/localwallet/tx/psbt.py``) serializes no uuid, timestamp,
or randomness: inputs are canonically ``(txid, vout)``-sorted, outputs are
emitted recipient-then-change in a fixed order, and embit emits a stable
BIP174 encoding. Evidence: :func:`test_base64_is_byte_deterministic` builds
and re-encodes the fixture three times and asserts all three bytes match
:data:`CANONICAL_BASE64` and each other.

The fixture is the canonical two-input P2WPKH case from
``tests/test_tx_psbt.py`` (same key/fixture patterns, reused via import):

- inputs: ``ab``*32:v0 = 40_000 sats (receive branch, index 3),
  ``cd``*32:v0 = 50_000 sats (change branch, index 4) — canonical order
  ``ab`` < ``cd``;
- 1 recipient (branch 0 index 99) of 60_000 sats + 1 change output
  (branch 1 index 7);
- fee rate 2 sat/vB → vsize 209, fee 418 sats, change 29_582 sats,
  inputs_total 90_000 sats.

Independent cross-check (not derived from the builder): fee == vsize ×
rate, and change == inputs − amount − fee — see
:func:`test_fee_and_change_recomputed_independently`.

The external-tool step validates structure + fee (Sparrow recomputes the
fee from the transaction itself); it CANNOT validate our selection policy —
that is covered by ``tests/test_tx_selection.py`` (honest note in
``docs/sparrow-ac.md``).
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Final

import pytest
from embit.networks import NETWORKS
from embit.psbt import PSBT

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.tx.psbt import (
    SEQUENCE_RBF_ENABLED,
    PsbtMeta,
    psbt_to_base64,
    validate_psbt_shape,
)
from localwallet.tx.selection import estimate_tx_vsize
from tests.test_tx_psbt import build_pair, source, spk

# --------------------------------------------------------------------------
# Canonical fixture (deterministic — see module docstring).
# --------------------------------------------------------------------------

#: Fee rate in sat/vB for the fixture.
FEE_RATE_SAT_VB: Final = 2

#: Recipient value in sats.
RECIPIENT_SATS: Final = 60_000

#: Canonical two-input selection (the test_tx_psbt pattern, rate=2).
_INPUT_AB = source("ab" * 32, 0, 40_000, index=3)
_INPUT_CD = source("cd" * 32, 0, 50_000, branch=1, index=4)
CANONICAL_INPUTS: Final = (_INPUT_AB, _INPUT_CD)

#: Computed/verified quantities (asserted against independent recompute).
INPUTS_TOTAL_SATS: Final = 40_000 + 50_000
CHANGE_SATS: Final = 29_582
FEE_SATS: Final = 418
VSIZE: Final = 209

#: The exact expected output scripts/values, in order.
RECIPIENT_SCRIPT: Final = spk(0, 99)
CHANGE_SCRIPT: Final = spk(1, 7)

#: Byte-identical base64 of the canonical fixture PSBT (pinned — see the
#: module docstring for the determinism verdict). Re-pinned for TCK-HW-003:
#: the change output now carries its bip32 derivation (device change
#: recognition), which appends hd_keypaths bytes to the last output scope.
CANONICAL_BASE64: Final = (
    "cHNidP8BAJoCAAAAAqurq6urq6urq6urq6urq6urq6urq6urq6urq6urq6"
    "urAAAAAAD9////zc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc0A"
    "AAAAAP3///8CYOoAAAAAAAAWABQ9donAA5g6m8UdqCOOAfCN4pzpJI5zAA"
    "AAAAAAFgAUyujlq5K2jdsIr+XwPjt5CjMYAzUAAAAAAAEBH0CcAAAAAAAA"
    "FgAUpyvM58Qt7QH80V1bVmVZ4aVJLRUiBgO3W1F6omIK8Zp42MXTBVwc34"
    "IzRb8zosobrF4CGaBNkxjoibavVAAAgAAAAIAAAACAAAAAAAMAAAAAAQEf"
    "UMMAAAAAAAAWABQmjfcF6dHmBw/OyqvWChjK6o2a7SIGAjr2BP5GwmQoRA"
    "fyUYbnl8x5vNoQPszwTNsViAiDcS2DGOiJtq9UAACAAAAAgAAAAIABAAAA"
    "BAAAAAAAIgIDi6hfSOddQbIqqINEWg0C8YqhV1L6GFi6I56qU8H121wY6I"
    "m2r1QAAIAAAACAAAAAgAEAAAAHAAAAAA=="
)

#: The :class:`PsbtMeta` the fixture builder must produce.
EXPECTED_META: Final = PsbtMeta(
    expected_outputs=(
        (RECIPIENT_SCRIPT, RECIPIENT_SATS),
        (CHANGE_SCRIPT, CHANGE_SATS),
    ),
    expected_fee_sats=FEE_SATS,
    vsize=VSIZE,
    inputs_count=2,
)

#: Env flag that switches on the manual-import artifact dump.
DUMP_ENV: Final = "LOCALWALLET_DUMP_PSBT"


def build_fixture() -> tuple[PSBT, PsbtMeta]:
    """Build the canonical fixture PSBT through the real public API."""
    (psbt, meta), _selection = build_pair(list(CANONICAL_INPUTS), RECIPIENT_SATS)
    return psbt, meta


# --------------------------------------------------------------------------
# Determinism + structure
# --------------------------------------------------------------------------


def test_base64_is_byte_deterministic() -> None:
    """The fixture PSBT is byte-identical across 3 independent builds.

    Evidence for the TCK-P2-006 determinism verdict: three fresh builds each
    produce a base64 string byte-for-byte equal to the pinned
    :data:`CANONICAL_BASE64` and to each other. No uuid/timestamp/random
    enters the builder's serialization.
    """
    b64s = {psbt_to_base64(build_fixture()[0]) for _ in range(3)}
    assert b64s == {CANONICAL_BASE64}
    assert len(CANONICAL_BASE64) == 556
    # The pinned constant round-trips to the same serialized bytes.
    assert base64.b64decode(CANONICAL_BASE64) == base64.b64decode(next(iter(b64s)))


def test_structure_matches_meta_exactly() -> None:
    """Structural assertions from the TCK-P2-006 spec against a fresh build."""
    psbt, meta = build_fixture()
    assert meta == EXPECTED_META
    tx = psbt.tx

    # embit round-trip parse of the exchange form reproduces the same tx.
    parsed = PSBT.parse(base64.b64decode(psbt_to_base64(psbt)))
    assert parsed.tx.serialize() == tx.serialize()

    # Outputs exactly [recipient, change] with matching values.
    assert len(tx.vout) == 2
    assert bytes(tx.vout[0].script_pubkey.data) == RECIPIENT_SCRIPT
    assert tx.vout[0].value == RECIPIENT_SATS
    assert bytes(tx.vout[1].script_pubkey.data) == CHANGE_SCRIPT
    assert tx.vout[1].value == CHANGE_SATS
    assert [scope.value for scope in psbt.outputs] == [RECIPIENT_SATS, CHANGE_SATS]

    # fee == inputs_total - outputs (conservation).
    inputs_total = sum(scope.witness_utxo.value for scope in psbt.inputs)
    outputs_total = sum(v for _s, v in meta.expected_outputs)
    assert inputs_total == INPUTS_TOTAL_SATS
    assert meta.expected_fee_sats == inputs_total - outputs_total

    # witness_utxo + bip32_derivations present per input; RBF sequence per input.
    assert len(psbt.inputs) == 2
    for i, (scope, vin) in enumerate(zip(psbt.inputs, tx.vin)):
        assert scope.witness_utxo is not None and scope.witness_utxo.value > 0
        assert len(scope.bip32_derivations) == 1
        assert vin.sequence == SEQUENCE_RBF_ENABLED
        assert scope.sequence == SEQUENCE_RBF_ENABLED
    # Canonical input order: ab.. < cd..
    assert tx.vin[0].txid == bytes(reversed(bytes.fromhex("ab" * 32)))
    assert tx.vin[1].txid == bytes(reversed(bytes.fromhex("cd" * 32)))

    # validate_psbt_shape passes on the well-formed output.
    validate_psbt_shape(psbt, meta)


def test_vsize_matches_meta_within_documented_bound() -> None:
    """vsize == meta.vsize; the ±1 vB bound (real-signature vs estimate) is
    documented, not asserted, here.

    ``meta.vsize`` is the max-witness estimate (72-byte sig convention) that
    ``tests/test_tx_selection.py``/``tests/test_tx_psbt.py`` verify equals
    the embit-built max-witness serialization EXACTLY and a really-signed
    fixture within 1 vB (real ECDSA sigs are usually 1 byte shorter than the
    72-byte convention). This harness pins the estimate and re-derives the
    fee from it (see :func:`test_fee_and_change_recomputed_independently`);
    the signed-vsize ±1 bound is covered by
    ``tests/test_tx_psbt.py::TestVsizeAgainstReallyBuiltTransactions``.
    """
    _psbt, meta = build_fixture()
    assert meta.vsize == VSIZE
    # Sanity: a 2-in 2-out P2WPKH tx at this shape is 209 vB (independent
    # helper agrees).
    assert estimate_tx_vsize(2, [RECIPIENT_SCRIPT], 8 + 1 + len(CHANGE_SCRIPT)) == VSIZE


def test_fee_and_change_recomputed_independently() -> None:
    """Cross-check vs INDEPENDENT computation, not the builder's own numbers.

    fee = vsize × rate; change = inputs − amount − fee. All quantities are
    re-derived from the pinned constants (which came from the fixture) and
    asserted equal to the builder's meta — a closed-form re-derivation, not
    a mirror of builder internals.
    """
    _psbt, meta = build_fixture()

    fee_from_vsize = VSIZE * FEE_RATE_SAT_VB
    assert fee_from_vsize == FEE_SATS
    assert meta.expected_fee_sats == fee_from_vsize

    change_from_conservation = INPUTS_TOTAL_SATS - RECIPIENT_SATS - FEE_SATS
    assert change_from_conservation == CHANGE_SATS
    assert meta.expected_outputs[1][1] == change_from_conservation


# --------------------------------------------------------------------------
# Env-gated artifact dump (manual Sparrow import aid)
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get(DUMP_ENV) != "1",
    reason=f"set {DUMP_ENV}=1 to write the fixture PSBT for manual Sparrow import",
)
def test_dump_fixture_for_sparrow_import(tmp_path: Path) -> None:
    """Write the canonical fixture PSBT + a JSON summary for the manual step.

    Gated by ``LOCALWALLET_DUMP_PSBT=1``; writes ONLY into pytest's
    ``tmp_path`` (never the repo) and prints the file paths for the human.
    The JSON summary lists outputs (address + sats), fee (sats + sats/vB),
    vsize, and inputs_total — the values to compare against Sparrow's UI.

    Run (add ``-s`` to see the paths):

        LOCALWALLET_DUMP_PSBT=1 pytest tests/test_sparrow_ac.py -k dump -s
    """
    _psbt, meta = build_fixture()

    b64_path = tmp_path / "sparrow_fixture.psbt"
    b64_path.write_text(CANONICAL_BASE64, encoding="utf-8")

    outputs = []
    for script_bytes, value in meta.expected_outputs:
        # Recover the bech32 address from the script for the human sheet.
        from embit.script import Script

        outputs.append(
            {
                "script_pubkey_hex": script_bytes.hex(),
                "address": Script(script_bytes).address(NETWORKS["main"]),
                "value_sats": value,
            }
        )
    summary = {
        "fee_rate_sat_vb": FEE_RATE_SAT_VB,
        "fee_sats": meta.expected_fee_sats,
        "vsize": meta.vsize,
        "inputs_count": meta.inputs_count,
        "inputs_total_sats": INPUTS_TOTAL_SATS,
        "outputs": outputs,
    }
    json_path = tmp_path / "sparrow_fixture.json"
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"\nSparrow fixture PSBT:  {b64_path}")
    print(f"Sparrow fixture JSON:  {json_path}")
    print("Import the .psbt via File > Open Transaction > From File ...")

    # Sanity: what we wrote is exactly the pinned fixture.
    assert b64_path.read_text(encoding="utf-8") == CANONICAL_BASE64

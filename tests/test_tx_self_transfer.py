"""TCK-TX-SELF-001 — explicit self-transfer flows (SPLIT / CONSOLIDATE).

Money-path pins for the ``self_transfer`` intent: protocol lockstep
(schema layer 2 + business rules layer 3 — the params structurally cannot
carry an address/outpoint/amount material to invent), the deterministic
engine plan (split: largest coin → N equal fresh-receive outputs with the
sub-part residue folded into the fee; consolidate: strictly-below-threshold
coins from ONE privacy pool per run → single fresh output), the
bounds/dust fail-closed refusals, the dispatcher-owned lifecycle (the SAME
TxFlow state machine — dual-key confirm, sign-time independent
re-derivation, revalidation hard stop, single-POST broadcast, pool-tag
lineage onto every own output), and card/FACTS discipline (plan values
verbatim from the handler result; engine-derived addresses never enter the
model transcript).

Reuses the real-wiring harness from :mod:`tests.test_e2e_skeleton`
(production dispatch table, mock chain, FACTS-quoting fake model, the
test-vector fake device signer) — no network, no hardware, deterministic.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import pathlib
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from embit.script import address_to_scriptpubkey

import localwallet.app as app_module
from localwallet.protocol import (
    BUSINESS_RULES,
    EnvelopeValidationError,
    IntentName,
    SelfTransferParams,
    validate_payload,
)
from localwallet.protocol.envelope import MAX_SELF_TRANSFER_PARTS, MIN_SELF_TRANSFER_PARTS
from localwallet.signer import SignedResult
from localwallet.tx.flow import GateDecision, TxFlow, TxFlowStatus
from localwallet.tx.psbt import PSBT
from localwallet.tx.revalidate import TamperedPsbtError, revalidate_signed_psbt
from localwallet.wallet.derivation import derive_addresses
from tests.test_e2e_skeleton import (
    SEND_RECIPIENT,
    SEND_UTXO,
    _build_send_table,
    _extract_signed_tx,
    _fixture_parsed,
    _run_send_repl,
    _send_chain_handler,
    _simulate_device_sign,
    derive_fixture_addresses,
)

#: P2WPKH dust at the default 3 sat/vB relay rate (ADR-0012 table).
P2WPKH_DUST: Final[int] = 294


# ------------------------------------------------------------------ helpers


def _utxo(txid: str, vout: int, value: int) -> dict[str, Any]:
    return {"txid": txid, "vout": vout, "value": value, "status": {"confirmed": True}}


def _self_envelope_json(params: dict[str, object]) -> str:
    return json.dumps({"v": 0, "intent": "self_transfer", "params": params})


def _self_env(params: dict[str, object]):
    return validate_payload(_self_envelope_json(params))


def _refs(prefix: str):
    """Deterministic id factory: staged, staged2, staged3, …"""
    state = {"n": 0}

    def factory() -> str:
        state["n"] += 1
        return f"{prefix}{'' if state['n'] == 1 else state['n'] - 1}"

    return factory


def _table(utxos_by_addr, *, state: dict | None = None, clock_time: float = 1_700_000_000.0):
    shared = state if state is not None else {}
    return _build_send_table(
        lambda rec: _send_chain_handler(rec, utxos_by_addr=utxos_by_addr, state=shared),
        flow=TxFlow(clock=lambda: clock_time, id_factory=_refs("ref")),
    )


class _HwiFakeSigner:
    """Signer override: signs every input with the test-vector key (the
    consensus BIP-143 digest the re-validation gate verifies)."""

    def __init__(self, tamper: bool = False) -> None:
        self.calls = 0
        self.tamper = tamper

    def sign_unsigned(self, psbt_base64: str) -> SignedResult:
        self.calls += 1
        return SignedResult(
            _simulate_device_sign(psbt_base64, tamper=self.tamper), "hwi:test-vector", False
        )


def _hwi_table(utxos_by_addr, *, state: dict | None = None, tamper: bool = False):
    signer = _HwiFakeSigner(tamper=tamper)
    shared = state if state is not None else {}
    table, store, wallet, client, recorded, flow, session = _build_send_table(
        lambda rec: _send_chain_handler(rec, utxos_by_addr=utxos_by_addr, state=shared),
        flow=TxFlow(clock=lambda: 1_700_000_000.0, id_factory=_refs("ref")),
        signer_selection=app_module.SignerSelection(
            kind="hwi",
            dir_path=pathlib.Path("/unused-in-hwi-mode"),
            fingerprint_hex=_fixture_parsed().hd_key.my_fingerprint.hex(),
        ),
        signer=signer,
    )
    return table, store, wallet, client, recorded, flow, session, signer


def _own_receive_script(index: int) -> bytes:
    derived = derive_addresses(_fixture_parsed(), branch=0, start_index=index, count=1)[0]
    return bytes(address_to_scriptpubkey(derived.address).data)


# =========================================================================
# 1. Protocol lockstep — schema (layer 2) + business rules (layer 3)
# =========================================================================


class TestSchema:
    def test_split_valid_and_roundtrips_grammar_shape(self) -> None:
        env = _self_env({"mode": "split", "parts": 4})
        assert env.params.model_dump() == {"mode": "split", "parts": 4}
        # Wire shape round-trips to EXACTLY the grammar-accepted form
        # (no "below_size_sats": null noise).
        assert json.loads(env.model_dump_json())["params"] == {"mode": "split", "parts": 4}

    def test_consolidate_valid_with_fee_target(self) -> None:
        env = _self_env({"mode": "consolidate", "below_size_sats": 50_000, "fee_target": "slow"})
        assert env.params.model_dump() == {
            "mode": "consolidate",
            "below_size_sats": 50_000,
            "fee_target": "slow",
        }

    @pytest.mark.parametrize(
        "params",
        [
            {"mode": "split"},  # missing parts
            {"mode": "consolidate"},  # missing below_size_sats
            {"mode": "split", "below_size_sats": 1000},  # wrong key
            {"mode": "consolidate", "parts": 4},  # wrong key
            {"mode": "split", "parts": 4, "below_size_sats": 1000},  # both keys
            {"mode": "merge", "parts": 2},  # unknown mode
            {"mode": "split", "parts": 1},  # below the floor (2)
            {"mode": "split", "parts": 21},  # above the ceiling (20)
            {"mode": "split", "parts": 0},  # below floor AND grammar-illegal
            {"mode": "split", "parts": "4"},  # string
            {"mode": "split", "parts": True},  # bool is not a JSON int
            {"mode": "split", "parts": 4.0},  # float is not a JSON int
            {"mode": "split", "parts": None},  # explicit null ≠ omission
            {"mode": "consolidate", "below_size_sats": 545},  # below the dust floor
            {"mode": "consolidate", "below_size_sats": "1000"},  # string
            {"mode": "consolidate", "below_size_sats": 21_000_000_000_000_001},  # > supply
            {"mode": "consolidate", "below_size_sats": 1000, "fee_target": None},  # null knob
            {"mode": "consolidate", "below_size_sats": 1000, "fee_target": "urgent"},
            {"mode": "split", "parts": 2, "fee_rate_sat_vb": 5},  # knob not offered here
            {},  # no mode at all
        ],
    )
    def test_rejects(self, params: dict[str, object]) -> None:
        with pytest.raises(EnvelopeValidationError):
            _self_env(params)

    @pytest.mark.parametrize(
        "params",
        [
            # THE money-invention battery: no address, amount or outpoint
            # key is representable in self_transfer params (closed world) —
            # the confirm-gate discipline CANNOT be bypassed through this
            # intent's payload.
            {"mode": "split", "parts": 2, "recipient": SEND_RECIPIENT},
            {"mode": "split", "parts": 2, "outputs": [{"address": "bc1q", "sats": 500}]},
            {"mode": "split", "parts": 2, "amount_sats": 50_000},
            {"mode": "consolidate", "below_size_sats": 1000, "txid": "a" * 64},
            {"mode": "consolidate", "below_size_sats": 1000, "outpoints": ["a" * 64 + ":0"]},
            {"mode": "consolidate", "below_size_sats": 1000, "confirm": True},
        ],
    )
    def test_invented_money_material_is_unrepresentable(self, params: dict[str, object]) -> None:
        with pytest.raises(EnvelopeValidationError):
            _self_env(params)

    def test_bounds_constants(self) -> None:
        assert (MIN_SELF_TRANSFER_PARTS, MAX_SELF_TRANSFER_PARTS) == (2, 20)


class TestBusinessRules:
    def test_valid_params_pass(self) -> None:
        assert BUSINESS_RULES[IntentName.SELF_TRANSFER](
            _self_env({"mode": "split", "parts": 3}).params
        ) == []
        assert BUSINESS_RULES[IntentName.SELF_TRANSFER](
            _self_env({"mode": "consolidate", "below_size_sats": 9_999}).params
        ) == []

    @pytest.mark.parametrize(
        ("kwargs", "needle"),
        [
            ({"mode": "split"}, "requires params.parts"),
            ({"mode": "split", "below_size_sats": 1000}, "not valid for mode 'split'"),
            ({"mode": "consolidate"}, "requires params.below_size_sats"),
            ({"mode": "consolidate", "parts": 2}, "not valid for mode 'consolidate'"),
            ({"mode": "split", "parts": True}, "params.parts must be an integer"),
            ({"mode": "consolidate", "below_size_sats": False}, "below_size_sats must be an integer"),
        ],
    )
    def test_layer3_rechecks_after_skipping_constructor(
        self, kwargs: dict[str, object], needle: str
    ) -> None:
        """Defense in depth: a validation-skipping constructor still hits
        the layer-3 pairing/bounds rule (the bool cases prove True/False do
        not sneak through the range comparisons)."""
        bypassed = SelfTransferParams.model_construct(**kwargs)
        failures = BUSINESS_RULES[IntentName.SELF_TRANSFER](bypassed)
        assert failures and needle in failures[0]

    def test_type_mismatch_fails_closed(self) -> None:
        assert BUSINESS_RULES[IntentName.SELF_TRANSFER](
            SimpleNamespace()  # not a SelfTransferParams at all
        ) == ["internal: 'self_transfer' params failed the type check"]


# =========================================================================
# 2. Engine handler — SPLIT determinism & bounds
# =========================================================================


class TestSplitPlan:
    def test_split_largest_coin_equal_outputs_no_change(self) -> None:
        addrs = derive_fixture_addresses(2)
        t, s, _w, c, _rec, flow, _se = _table(
            {
                addrs[0]: [_utxo("d" * 64, 0, 100_000)],
                addrs[1]: [_utxo("e" * 64, 0, 40_000)],
            }
        )
        res = t[IntentName.SELF_TRANSFER](_self_env({"mode": "split", "parts": 3}))
        assert res.get("error") is None
        # vsize(1-in, 3-out, changeless) = 172 vB (42+2×… overhead 44 + 272 +
        # 3×124 = 688 WU); medium rung 2 sat/vB → fee floor 346;
        # each = (100_000 − 346)//3 = 33_218; residue 0 folds.
        assert res["self_mode"] == "split"
        assert res["self_parts"] == 3
        assert res["self_each_sats"] == 33_218
        assert res["amount_sats"] == 99_654
        assert res["fee_sats"] == 346
        assert res["vsize"] == 172
        assert res["change_sats"] is None
        assert res["inputs_count"] == 1
        assert res["self_inputs_total_sats"] == 100_000
        # Conservation: inputs = outputs + fee, to the sat.
        assert res["self_inputs_total_sats"] == res["amount_sats"] + res["fee_sats"]
        # The LARGEST coin was picked (100_000, not the 40_000): spending
        # the small one would underfund the pinned plan.
        assert flow.pending is not None
        psbt = PSBT.parse(base64.b64decode(flow.pending.psbt_base64))
        assert len(psbt.tx.vin) == 1
        assert psbt.tx.vin[0].txid.hex() == "d" * 64  # embit byte order
        s.close()
        c.close()

    def test_split_targets_fresh_receive_indices_and_advances_next_index(self) -> None:
        addrs = derive_fixture_addresses(6)
        t, s, w, c, _rec, flow, _se = _table({addrs[0]: [_utxo("d" * 64, 0, 100_000)]})
        res = t[IntentName.SELF_TRANSFER](_self_env({"mode": "split", "parts": 3}))
        assert res.get("error") is None
        # NEVER the spent coin's own address: destinations are FRESH and
        # consecutive from the receive branch's next_index (branch policy:
        # documented as the receive chain — these ARE user-facing coins).
        assert res["recipient"] != addrs[0]
        after = s.get_derivation(w.id, 0).next_index
        start = after - 3
        # The lazy first scan moved next_index past the used coin; the plan
        # consumed exactly the NEXT three fresh indices.
        assert res["recipient"] == derive_fixture_addresses(after)[start]
        assert flow.pending.self_payment_indices == (start, start + 1, start + 2)
        row = s.get_by_address(res["recipient"])
        assert row is not None and row.branch == 0 and row.status == "allocated"
        s.close()
        c.close()

    def test_split_is_deterministic_under_snapshot_shuffle(self) -> None:
        addrs = derive_fixture_addresses(2)
        base = {addrs[0]: [_utxo("a" * 64, 0, 50_000), _utxo("b" * 64, 1, 100_000), _utxo("c" * 64, 0, 100_000)]}
        flipped = {addrs[0]: [_utxo("c" * 64, 0, 100_000), _utxo("a" * 64, 0, 50_000), _utxo("b" * 64, 1, 100_000)]}

        def plan(utxos):
            t, s, _w, c, _rec, flow, _se = _table(utxos)
            r = t[IntentName.SELF_TRANSFER](_self_env({"mode": "split", "parts": 2}))
            spent = PSBT.parse(base64.b64decode(flow.pending.psbt_base64)).tx.vin[0]
            out = (r["self_each_sats"], r["fee_sats"], spent.txid.hex(), spent.vout)
            s.close()
            c.close()
            return out

        # Same plan AND the same coin chosen under a 100k tie: canonical
        # (value, txid, vout) order breaks the tie on the txid.
        assert plan(base) == plan(flipped)

    def test_split_below_dust_refuses_before_any_allocation(self) -> None:
        addrs = derive_fixture_addresses(3)
        t, s, w, c, _rec, flow, _se = _table({addrs[0]: [_utxo("d" * 64, 0, 3_000)]})
        res = t[IntentName.SELF_TRANSFER](_self_env({"mode": "split", "parts": 20}))
        assert res == {"error": "self_split_below_dust"}  # value-free refusal
        # Nothing staged, NO destination ever allocated: fail closed before
        # the build/allocation step. (The lazy scan may have created
        # used/unused rows inside its gap window — the proof is that none
        # reached 'allocated'.)
        assert flow.state is TxFlowStatus.IDLE
        assert flow.pending is None
        assert not any(r.status == "allocated" for r in s.get_addresses(w.id, 0))
        s.close()
        c.close()

    def test_split_empty_wallet_reports_insufficient(self) -> None:
        t, s, _w, c, _rec, flow, _se = _table({})
        res = t[IntentName.SELF_TRANSFER](_self_env({"mode": "split", "parts": 5}))
        assert res["error"] == "insufficient_funds"
        assert res["available_sats"] == 0
        # needed = the changeless 1-in/5-out fee floor (234 vB × 2) + the
        # five dust floors (user-facing UI figures, ADR-0012).
        assert res["needed_sats"] == 234 * 2 + 5 * P2WPKH_DUST
        assert flow.state is TxFlowStatus.IDLE
        s.close()
        c.close()

    def test_split_residue_folds_into_fee_and_stays_above_dust(self) -> None:
        # V−fee not divisible by parts: the sub-part residue folds into the
        # FEE; outputs stay EXACTLY equal and the fee never drops below the
        # rate-determined floor (vsize 172 × 2 = 344).
        addrs = derive_fixture_addresses(1)
        t, s, _w, c, _rec, _flow, _se = _table({addrs[0]: [_utxo("d" * 64, 0, 100_002)]})
        res = t[IntentName.SELF_TRANSFER](_self_env({"mode": "split", "parts": 3}))
        # (100_002 − 344) // 3 = 33_219 → 3×each = 99_657 → fee 345 (344+1).
        assert res["self_each_sats"] == 33_219
        assert res["fee_sats"] == 100_002 - 3 * 33_219 == 345
        assert res["fee_sats"] >= 172 * 2  # never below the rate floor
        assert res["self_each_sats"] >= P2WPKH_DUST
        assert res["amount_sats"] + res["fee_sats"] == 100_002  # conservation
        s.close()
        c.close()

    def test_split_fee_rung_override_respected(self) -> None:
        # fee_target slow → the hourFee rung of the mock ladder (1 sat/vB):
        # a cheaper fee than medium's 2 — the estimator rides the existing
        # ladder; no new chain surface.
        addrs = derive_fixture_addresses(1)
        t, s, _w, c, _rec, _flow, _se = _table({addrs[0]: [_utxo("d" * 64, 0, 100_000)]})
        res = t[IntentName.SELF_TRANSFER](
            _self_env({"mode": "split", "parts": 3, "fee_target": "slow"})
        )
        assert res["fee_rate_sat_vb"] == 1
        assert res["fee_sats"] == 172  # vsize 172 × 1
        s.close()
        c.close()


# =========================================================================
# 3. Engine handler — CONSOLIDATE determinism, pools, bounds
# =========================================================================


class TestConsolidatePlan:
    def test_strictly_below_threshold_single_output(self) -> None:
        addrs = derive_fixture_addresses(4)
        t, s, _w, c, _rec, _flow, _se = _table(
            {
                addrs[0]: [_utxo("a" * 64, 0, 10_000)],
                addrs[1]: [_utxo("b" * 64, 1, 20_000)],
                addrs[2]: [_utxo("c" * 64, 0, 5_000)],
                addrs[3]: [_utxo("d" * 64, 0, 50_000)],  # == threshold → excluded
            }
        )
        res = t[IntentName.SELF_TRANSFER](
            _self_env({"mode": "consolidate", "below_size_sats": 50_000})
        )
        assert res.get("error") is None
        assert res["self_mode"] == "consolidate"
        assert res["inputs_count"] == 3  # 5k + 10k + 20k; the 50k coin stays
        assert res["self_inputs_total_sats"] == 35_000
        # vsize(3-in, 1-out, changeless) = 246 vB; rate 2 → fee 492.
        assert res["vsize"] == 246
        assert res["fee_sats"] == 492
        assert res["amount_sats"] == 35_000 - 492 == res["self_each_sats"]
        assert res["change_sats"] is None
        assert res["self_new_addresses"] == 1
        s.close()
        c.close()

    def test_empty_set_honest_value_free_line(self) -> None:
        addrs = derive_fixture_addresses(1)
        t, s, _w, c, _rec, flow, _se = _table({addrs[0]: [_utxo("d" * 64, 0, 100_000)]})
        res = t[IntentName.SELF_TRANSFER](
            _self_env({"mode": "consolidate", "below_size_sats": 4_000})
        )
        assert res == {"error": "self_nothing_below"}
        assert flow.state is TxFlowStatus.IDLE
        s.close()
        c.close()

    def test_fee_shortfall_reports_insufficient_funds(self) -> None:
        addrs = derive_fixture_addresses(1)
        t, s, _w, c, _rec, flow, _se = _table({addrs[0]: [_utxo("d" * 64, 0, 400)]})
        res = t[IntentName.SELF_TRANSFER](
            _self_env({"mode": "consolidate", "below_size_sats": 1_000})
        )
        # fee floor = vsize(1-in,1-out,changeless)=110 × 2 sat/vB = 220;
        # 400 − 220 = 180 < 294 dust → the structured UI pair (ADR-0012).
        assert res["error"] == "insufficient_funds"
        assert res["available_sats"] == 400
        assert res["needed_sats"] == 220 + P2WPKH_DUST
        assert flow.state is TxFlowStatus.IDLE
        s.close()
        c.close()

    def test_pool_sides_never_mix_larger_total_wins_with_hint(self) -> None:
        addrs = derive_fixture_addresses(2)
        t, s, w, c, _rec, flow, _se = _table(
            {
                addrs[0]: [_utxo("a" * 64, 0, 10_000)],  # → labeled kyc
                addrs[1]: [_utxo("b" * 64, 0, 20_000)],  # other side (unlabeled)
            }
        )
        s.set_coin_label(w.id, "a" * 64, 0, tags=("kyc",))
        res = t[IntentName.SELF_TRANSFER](
            _self_env({"mode": "consolidate", "below_size_sats": 50_000})
        )
        assert res.get("error") is None
        # Other-side pool (20k) beats the kyc pool (10k) by total value.
        assert res["inputs_count"] == 1
        assert res["self_inputs_total_sats"] == 20_000
        assert res["self_other_side_count"] == 1  # honest "the other side too"
        # The staged PSBT spends ONLY the unlabeled (other-side) coin.
        psbt = PSBT.parse(base64.b64decode(flow.pending.psbt_base64))
        assert psbt.tx.vin[0].txid.hex() == "b" * 64
        s.close()
        c.close()

    def test_tie_between_pools_breaks_other_side_first(self) -> None:
        addrs = derive_fixture_addresses(2)
        t, s, w, c, _rec, flow, _se = _table(
            {
                addrs[0]: [_utxo("a" * 64, 0, 20_000)],
                addrs[1]: [_utxo("b" * 64, 0, 20_000)],
            }
        )
        s.set_coin_label(w.id, "a" * 64, 0, tags=("kyc",))
        res = t[IntentName.SELF_TRANSFER](
            _self_env({"mode": "consolidate", "below_size_sats": 50_000})
        )
        assert res["inputs_count"] == 1 and res["self_other_side_count"] == 1
        psbt = PSBT.parse(base64.b64decode(flow.pending.psbt_base64))
        assert psbt.tx.vin[0].txid.hex() == "b" * 64  # other-side wins ties
        s.close()
        c.close()

    def test_single_pool_set_consolidates_without_hint(self) -> None:
        addrs = derive_fixture_addresses(2)
        t, s, w, c, _rec, _flow, _se = _table(
            {
                addrs[0]: [_utxo("a" * 64, 0, 10_000)],
                addrs[1]: [_utxo("b" * 64, 0, 20_000)],
            }
        )
        s.set_coin_label(w.id, "a" * 64, 0, tags=("kyc",))
        s.set_coin_label(w.id, "b" * 64, 0, tags=("exchange",))  # SAME side
        res = t[IntentName.SELF_TRANSFER](
            _self_env({"mode": "consolidate", "below_size_sats": 50_000})
        )
        assert res.get("error") is None
        assert res["inputs_count"] == 2  # both kyc-side coins, one pool
        assert "self_other_side_count" not in res
        s.close()
        c.close()

    def test_input_cap_refuses_value_free(self, monkeypatch: pytest.MonkeyPatch) -> None:
        addrs = derive_fixture_addresses(8)
        utxos = {addrs[i]: [_utxo(f"{i + 1:064x}", 0, 1_000 + i)] for i in range(7)}
        t, s, _w, c, _rec, flow, _se = _table(utxos)
        monkeypatch.setattr(app_module, "MAX_SELF_TRANSFER_CONSOLIDATE_INPUTS", 3)
        res = t[IntentName.SELF_TRANSFER](
            _self_env({"mode": "consolidate", "below_size_sats": 100_000})
        )
        assert res == {"error": "self_too_many_small"}
        assert flow.state is TxFlowStatus.IDLE
        s.close()
        c.close()

    def test_consolidate_is_deterministic_across_snapshot_orders(self) -> None:
        addrs = derive_fixture_addresses(2)
        forward = {addrs[0]: [_utxo("a" * 64, 0, 10_000), _utxo("b" * 64, 1, 20_000), _utxo("c" * 64, 0, 5_000)]}
        backward = {addrs[0]: [_utxo("c" * 64, 0, 5_000), _utxo("b" * 64, 1, 20_000), _utxo("a" * 64, 0, 10_000)]}

        def plan(utxos):
            t, s, _w, c, _rec, flow, _se = _table(utxos)
            r = t[IntentName.SELF_TRANSFER](
                _self_env({"mode": "consolidate", "below_size_sats": 50_000})
            )
            psbt = PSBT.parse(base64.b64decode(flow.pending.psbt_base64))
            out = (
                r["inputs_count"],
                r["fee_sats"],
                r["amount_sats"],
                [(v.txid.hex(), v.vout) for v in psbt.tx.vin],
            )
            s.close()
            c.close()
            return out

        assert plan(forward) == plan(backward)


# =========================================================================
# 4. Fail-closed interplay: scan gate, pending guards, cross-flow
# =========================================================================


class TestGatesAndGuards:
    def test_first_scan_gate_refuses_before_any_work(self) -> None:
        addrs = derive_fixture_addresses(1)
        _t, s, w, c, _recorded, flow, _se = _table({addrs[0]: [SEND_UTXO]})
        gated = app_module._make_self_transfer_handler(
            s,
            w.id,
            _fixture_parsed(),
            flow,
            app_module.FeeEstimator(c),
            lambda: None,
            scan_gate=SimpleNamespace(first_scan_incomplete=True),
        )
        res = gated(_self_env({"mode": "split", "parts": 2}))
        assert res == {"error": "wallet_loading", "detail": app_module.WALLET_LOADING_REFUSAL}
        assert flow.state is TxFlowStatus.IDLE
        s.close()
        c.close()

    def test_self_transfer_refused_while_a_send_pends(self) -> None:
        addrs = derive_fixture_addresses(2)
        t, s, _w, c, _rec, _flow, _se = _table({addrs[0]: [SEND_UTXO]})
        created = t[IntentName.CREATE_TX](
            validate_payload(
                {
                    "v": 0,
                    "intent": "create_tx",
                    "params": {"recipient": SEND_RECIPIENT, "amount_sats": 60_000},
                }
            )
        )
        assert created.get("error") is None
        res = t[IntentName.SELF_TRANSFER](_self_env({"mode": "split", "parts": 2}))
        assert res["error"] == "tx_pending"
        assert "self_transfer" not in res  # the re-shown pending is an external send
        assert res["tx_ref"] == created["tx_ref"]
        s.close()
        c.close()

    def test_create_tx_can_never_replace_a_pending_self_plan(self) -> None:
        addrs = derive_fixture_addresses(2)
        t, s, _w, c, _rec, flow, _se = _table({addrs[0]: [SEND_UTXO]})
        staged = t[IntentName.SELF_TRANSFER](_self_env({"mode": "split", "parts": 2}))
        assert staged.get("error") is None
        # Even a create_tx quoting the plan's first output address + total
        # value (a FORGED envelope bypassing the suppressed FACTS) must NOT
        # re-quote the plan away — the requote condition excludes
        # self-transfer records.
        pending = flow.pending
        hostile = validate_payload(
            {
                "v": 0,
                "intent": "create_tx",
                "params": {"recipient": pending.recipient, "amount_sats": pending.amount_sats},
            }
        )
        res = t[IntentName.CREATE_TX](hostile)
        assert res["error"] == "tx_pending"
        assert res["self_transfer"] is True  # re-shown honestly AS the plan
        assert res["tx_ref"] == staged["tx_ref"]  # plan survives, same ref
        assert flow.state is TxFlowStatus.CREATED
        s.close()
        c.close()

    def test_second_self_transfer_while_pending_is_refused_with_plan_keys(self) -> None:
        addrs = derive_fixture_addresses(4)
        t, s, _w, c, _rec, _flow, _se = _table({addrs[0]: [SEND_UTXO]})
        first = t[IntentName.SELF_TRANSFER](_self_env({"mode": "split", "parts": 3}))
        second = t[IntentName.SELF_TRANSFER](_self_env({"mode": "split", "parts": 2}))
        assert second["error"] == "tx_pending"
        assert second["self_transfer"] is True
        assert second["self_mode"] == "split"
        assert second["self_parts"] == 3  # the PENDING plan, not the new one
        assert second["self_each_sats"] == first["self_each_sats"]
        assert second["tx_ref"] == first["tx_ref"]
        s.close()
        c.close()


# =========================================================================
# 5. Sign-time intent — independent re-derivation & the revalidation gate
# =========================================================================


class TestSignTimeIntent:
    def _stage_split(self, tamper: bool = False):
        addrs = derive_fixture_addresses(8)
        result = _hwi_table(
            {addrs[0]: [_utxo("d" * 64, 0, 300_000)]}, tamper=tamper
        )
        t, s, w, c, _rec, flow, se, signer = result
        res = t[IntentName.SELF_TRANSFER](_self_env({"mode": "split", "parts": 3}))
        return t, s, w, c, flow, se, signer, res

    def test_intent_re_derives_every_own_output(self) -> None:
        _t, s, _w, c, flow, _se, _signer, res = self._stage_split()
        pending = flow.pending
        intended = app_module._intended_from_confirmed(
            pending, parsed=_fixture_parsed(), change_index=None
        )
        assert len(intended.expected_recipient_outputs) == 3
        assert intended.expected_change is None
        assert intended.expected_fee_sats == res["fee_sats"]
        # The expected scripts are INDEPENDENT re-derivations of the
        # recorded receive indices — not just a copy of the staged PSBT.
        expected_scripts = {_own_receive_script(i) for i in pending.self_payment_indices}
        assert {script for script, _v in intended.expected_recipient_outputs} == expected_scripts
        assert {v for _script, v in intended.expected_recipient_outputs} == {res["self_each_sats"]}
        s.close()
        c.close()

    def test_record_psbt_drift_is_a_hard_stop(self) -> None:
        _t, s, _w, c, flow, _se, _signer, _res = self._stage_split()
        pending = flow.pending
        # A record claiming change contradicts the changeless plan PSBT.
        with pytest.raises(ValueError):
            app_module._intended_from_confirmed(
                dataclasses.replace(pending, change_sats=999),
                parsed=_fixture_parsed(),
                change_index=0,
            )
        # Drifted destination indices → refused (script mismatch).
        with pytest.raises(ValueError):
            app_module._intended_from_confirmed(
                dataclasses.replace(pending, self_payment_indices=(98, 99, 100)),
                parsed=_fixture_parsed(),
                change_index=None,
            )
        # A tampered total (no longer uniform over the parts) → refused.
        with pytest.raises(ValueError):
            app_module._intended_from_confirmed(
                dataclasses.replace(pending, amount_sats=pending.amount_sats + 1),
                parsed=_fixture_parsed(),
                change_index=None,
            )
        # A wrong input count is refused by the revalidation gate itself.
        intended = app_module._intended_from_confirmed(
            pending, parsed=_fixture_parsed(), change_index=None
        )
        with pytest.raises(TamperedPsbtError):
            revalidate_signed_psbt(
                _simulate_device_sign(pending.psbt_base64),
                dataclasses.replace(intended, expected_inputs_count=99),
            )
        s.close()
        c.close()

    def test_tampered_signed_psbt_is_a_hard_stop_before_broadcast(self) -> None:
        t, s, _w, c, flow, se, _signer, res = self._stage_split(tamper=True)
        ref = res["tx_ref"]
        se.gate_decision = GateDecision.CONFIRM
        t[IntentName.CONFIRM_TX](
            validate_payload({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": ref}})
        )
        signed = t[IntentName.SIGN_TX](
            validate_payload({"v": 0, "intent": "sign_tx", "params": {"tx_ref": ref}})
        )
        assert signed["error"] == "revalidation_failed"
        assert flow.state is TxFlowStatus.CONFIRMED  # untouched — no skip path
        assert flow.signed is None
        s.close()
        c.close()


# =========================================================================
# 6. Full lifecycle e2e (dual-key confirm → sign → revalidate → broadcast)
# =========================================================================


class TestLifecycle:
    def test_split_envelope_card_confirm_sign_broadcast(self) -> None:
        addrs = derive_fixture_addresses(4)
        state: dict[str, Any] = {}
        t, s, _w, c, _rec, flow, se, _signer = _hwi_table(
            {addrs[0]: [_utxo("d" * 64, 0, 300_000)]}, state=state
        )
        res = t[IntentName.SELF_TRANSFER](_self_env({"mode": "split", "parts": 2}))
        ref = res["tx_ref"]
        assert flow.state is TxFlowStatus.CREATED

        # An LLM-relayed confirm WITHOUT the same-turn gate decision: refused.
        se.gate_decision = GateDecision.NOT_A_DECISION
        refused = t[IntentName.CONFIRM_TX](
            validate_payload({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": ref}})
        )
        assert refused["error"] == "confirm_refused"
        assert flow.state is TxFlowStatus.CREATED
        # A wrong ref with a real decision: still refused (verbatim match).
        se.gate_decision = GateDecision.CONFIRM
        refused2 = t[IntentName.CONFIRM_TX](
            validate_payload({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": "nope"}})
        )
        assert refused2["error"] == "confirm_refused"
        assert flow.state is TxFlowStatus.CREATED

        # Dual key satisfied → CONFIRMED → SIGNED (revalidation verifies the
        # test-vector signatures over the 2-output plan) → BROADCAST.
        t[IntentName.CONFIRM_TX](
            validate_payload({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": ref}})
        )
        assert flow.state is TxFlowStatus.CONFIRMED
        signed = t[IntentName.SIGN_TX](
            validate_payload({"v": 0, "intent": "sign_tx", "params": {"tx_ref": ref}})
        )
        assert signed["status"] == "signed"
        assert flow.state is TxFlowStatus.SIGNED
        broadcast = t[IntentName.BROADCAST_TX](
            validate_payload({"v": 0, "intent": "broadcast_tx", "params": {"tx_ref": ref}})
        )
        assert broadcast["status"] == "broadcast"
        assert flow.state is TxFlowStatus.BROADCAST
        # The honest-backend echo: the recorded txid IS the broadcast tx.
        assert broadcast["txid"] == _extract_signed_tx(flow.signed.psbt_base64).txid().hex()
        assert len(state["broadcast_posts"]) == 1  # single-attempt POST
        tx = _extract_signed_tx(flow.signed.psbt_base64)
        assert [o.value for o in tx.vout] == [res["self_each_sats"]] * 2
        s.close()
        c.close()

    def test_broadcast_lineage_tags_every_own_output(self) -> None:
        addrs = derive_fixture_addresses(4)
        t, s, w, c, _rec, _flow, se, _signer = _hwi_table({addrs[0]: [_utxo("d" * 64, 0, 300_000)]})
        s.set_coin_label(w.id, "d" * 64, 0, tags=("kyc",))
        res = t[IntentName.SELF_TRANSFER](_self_env({"mode": "split", "parts": 2}))
        ref = res["tx_ref"]
        se.gate_decision = GateDecision.CONFIRM
        t[IntentName.CONFIRM_TX](
            validate_payload({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": ref}})
        )
        t[IntentName.SIGN_TX](
            validate_payload({"v": 0, "intent": "sign_tx", "params": {"tx_ref": ref}})
        )
        broadcast = t[IntentName.BROADCAST_TX](
            validate_payload({"v": 0, "intent": "broadcast_tx", "params": {"tx_ref": ref}})
        )
        txid = broadcast["txid"]
        # The reshuffle preserves the pool: BOTH new coins inherit kyc.
        for vout in (0, 1):
            row = s.get_coin_label(w.id, txid, vout)
            assert row is not None and "kyc" in row.tags, f"vout {vout} lost its tag"
        s.close()
        c.close()

    def test_replay_broadcast_is_refused_after_terminal(self) -> None:
        addrs = derive_fixture_addresses(2)
        t, s, _w, c, _rec, _flow, se, _signer = _hwi_table({addrs[0]: [_utxo("d" * 64, 0, 300_000)]})
        res = t[IntentName.SELF_TRANSFER](_self_env({"mode": "split", "parts": 2}))
        ref = res["tx_ref"]
        se.gate_decision = GateDecision.CONFIRM
        for intent in ("confirm_tx", "sign_tx", "broadcast_tx"):
            t[IntentName(intent)](
                validate_payload({"v": 0, "intent": intent, "params": {"tx_ref": ref}})
            )
        late = t[IntentName.BROADCAST_TX](
            validate_payload({"v": 0, "intent": "broadcast_tx", "params": {"tx_ref": ref}})
        )
        assert late["error"] == "broadcast_refused"
        s.close()
        c.close()


# =========================================================================
# 7. Cards & FACTS hygiene
# =========================================================================


class TestCardAndFacts:
    def _staged(self):
        addrs = derive_fixture_addresses(6)
        t, s, w, c, _rec, flow, _se = _table({addrs[0]: [_utxo("d" * 64, 0, 100_000)]})
        res = t[IntentName.SELF_TRANSFER](_self_env({"mode": "split", "parts": 3}))
        return t, s, w, c, flow, res

    def test_split_brief_card_lines_are_verbatim(self) -> None:
        _t, s, _w, c, _flow, res = self._staged()
        out: list[str] = []
        app_module._print_self_transfer(res, out.append)
        assert out == [
            'Pending — say "sign" to review it on your device, or "cancel" to discard.',
            "Plan: split 1 coin into 3 × 33,218 sats → 3 fresh addresses",
            "In: 100,000 sats from 1 source",
            "Fee: 346 sats · 2 sat/vB × 172 vB · medium — ETA ~60-70 min — estimate only, not a guarantee",
            "full breakdown: /details",
        ]
        # No address ever reaches the card (counts + values only).
        assert not any("bc1" in line for line in out)
        s.close()
        c.close()

    def test_consolidate_card_lines_and_pool_hint(self) -> None:
        addrs = derive_fixture_addresses(3)
        t, s, w, c, _rec, _flow, _se = _table(
            {
                addrs[0]: [_utxo("a" * 64, 0, 10_000)],
                addrs[1]: [_utxo("b" * 64, 0, 20_000)],
            }
        )
        s.set_coin_label(w.id, "a" * 64, 0, tags=("kyc",))
        res = t[IntentName.SELF_TRANSFER](
            _self_env({"mode": "consolidate", "below_size_sats": 50_000})
        )
        out: list[str] = []
        app_module._print_self_transfer(res, out.append)
        assert out[1] == (
            f"Plan: merge 1 small coin into 1 × {res['self_each_sats']:,} sats "
            "(1 fresh address)"
        )
        assert out[2] == "In: 20,000 sats from 1 source"
        assert "consolidate again" in out[-2]  # the other-side hint line
        assert out[-1] == "full breakdown: /details"
        s.close()
        c.close()

    def test_refusals_render_their_dispatcher_lines(self) -> None:
        out: list[str] = []
        app_module._print_self_transfer({"error": "self_nothing_below"}, out.append)
        assert out == [app_module._SELF_NOTHING_BELOW]
        out2: list[str] = []
        app_module._print_self_transfer({"error": "self_split_below_dust"}, out2.append)
        assert out2 == [app_module._SELF_SPLIT_BELOW_DUST]
        out3: list[str] = []
        app_module._print_self_transfer({"error": "self_too_many_small"}, out3.append)
        assert out3 == [app_module._SELF_TOO_MANY_SMALL]
        out4: list[str] = []
        app_module._print_self_transfer(
            {"error": "insufficient_funds", "needed_sats": 900, "available_sats": 400},
            out4.append,
        )
        assert out4 == ["Insufficient funds: need 900 sats, have 400 sats."]
        # The value-free refusals carry no sat/amount figures at all.
        assert "0 sats" not in "".join(out + out2)

    def test_pending_plan_reshow_renders_plan_not_a_single_send(self) -> None:
        _t, s, _w, c, flow, _res = self._staged()
        # create_tx re-show path: _print_create_tx → _print_brief_card
        # delegates to the plan renderer.
        reshow = app_module._tx_pending_result(flow)
        out: list[str] = []
        app_module._print_create_tx(reshow, out.append)
        assert any(line.startswith("Plan: split") for line in out)
        assert not any(line.startswith("To: ") for line in out)
        # The self handler's own tx_pending branch renders the same plan.
        out2: list[str] = []
        app_module._print_self_transfer(reshow, out2.append)
        assert out2[0] == app_module._GUIDANCE_STILL_PENDING
        assert any(line.startswith("Plan: split") for line in out2)
        s.close()
        c.close()

    def test_facts_never_contain_own_addresses_of_a_plan(self) -> None:
        _t, s, _w, c, flow, res = self._staged()
        facts = app_module._flow_facts(flow)
        assert facts["pending_tx_ref"] == res["tx_ref"]
        assert "self-transfer split into 3 equal parts" in str(facts["pending_tx_plan"])
        # No recipient fact for a plan (the model has nothing to re-quote);
        # and NOTHING in the injected block is an address.
        assert "pending_tx_recipient" not in facts
        blob = " ".join(str(v) for v in facts.values())
        assert "bc1" not in blob
        s.close()
        c.close()

    def test_external_send_facts_unchanged(self) -> None:
        # Regression: the ordinary send path STILL carries recipient/amount
        # facts (the re-quote discipline depends on them).
        addrs = derive_fixture_addresses(2)
        t, s, _w, c, _rec, flow, _se = _table({addrs[0]: [SEND_UTXO]})
        t[IntentName.CREATE_TX](
            validate_payload(
                {
                    "v": 0,
                    "intent": "create_tx",
                    "params": {"recipient": SEND_RECIPIENT, "amount_sats": 60_000},
                }
            )
        )
        facts = app_module._flow_facts(flow)
        assert facts["pending_tx_recipient"] == SEND_RECIPIENT
        assert facts["pending_tx_amount_sats"] == 60_000
        s.close()
        c.close()


# =========================================================================
# 8. REPL-level: FACTS-quoting model turn + GATE-MERGE chain
# =========================================================================


class _SelfFactsQuotingGenerate:
    """Production-path fake model: quotes ``pending_tx_ref`` VERBATIM from
    the injected FACTS (never sees the flow object); emits the
    self_transfer envelope on the first turn."""

    def __init__(self, plan: list[str]) -> None:
        self.plan = list(plan)
        self.prompts: list[str] = []
        self._n = 0

    def __call__(self, prompt: str, grammar_text: str | None) -> str:
        del grammar_text
        self.prompts.append(prompt)
        step = self.plan[self._n] if self._n < len(self.plan) else (
            '{"v": 0, "intent": "respond", "params": {"text": "Noted."}}'
        )
        self._n += 1
        if step == "self":
            return _self_envelope_json({"mode": "split", "parts": 2})
        if step == "confirm":
            match = re.search(r"^pending_tx_ref: (\S+)$", prompt, re.MULTILINE)
            assert match is not None, "test bug: no pending_tx_ref fact"
            return json.dumps(
                {"v": 0, "intent": "confirm_tx", "params": {"tx_ref": match.group(1)}}
            )
        raise AssertionError(f"unknown step {step!r}")


def test_repl_self_transfer_confirm_chains_into_sign_in_one_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    addrs = derive_fixture_addresses(6)
    fake = _SelfFactsQuotingGenerate(["self", "confirm"])
    code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        _send_chain_handler([], utxos_by_addr={addrs[0]: [_utxo("d" * 64, 0, 300_000)]}),
        ["split my big coin into 2", "sign", "exit"],
        ["self", "confirm"],
        generate=fake,
    )
    joined = "\n".join(outputs)
    assert code == 0
    assert "Plan: split 1 coin into 2 ×" in joined
    # GATE-MERGE: the confirm turn chained straight into the device handoff
    # (file signer default) — export + signed-file-missing ask; the plan is
    # CONFIRMED, nothing signed, nothing broadcast.
    assert "Exported to " in joined
    assert flow.state is TxFlowStatus.CONFIRMED
    # Production-path hygiene: every turn AFTER staging received the plan
    # FACTS and NONE of them carried an engine-derived address.
    for p in fake.prompts[1:]:
        turn = p.split("user:")[-1]
        assert "bc1" not in turn


# =========================================================================
# 9. Drift pins — table completeness & refusal-before-network ordering
# =========================================================================


def test_dispatch_table_covers_the_closed_enum() -> None:
    import inspect

    src = inspect.getsource(app_module.build_dispatch_table)
    for intent in IntentName:
        assert f"IntentName.{intent.name}:" in src, f"{intent.name} missing from the table"


def test_pending_guard_refuses_before_any_network_work() -> None:
    addrs = derive_fixture_addresses(2)
    t, s, _w, c, recorded, _flow, _se = _table({addrs[0]: [SEND_UTXO]})
    t[IntentName.SELF_TRANSFER](_self_env({"mode": "split", "parts": 2}))
    before = len(recorded)
    res = t[IntentName.SELF_TRANSFER](_self_env({"mode": "split", "parts": 3}))
    assert res["error"] == "tx_pending"
    assert len(recorded) == before  # zero new chain calls: guard first
    s.close()
    c.close()

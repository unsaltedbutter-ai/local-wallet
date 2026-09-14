"""TCK-CPFP-002 — the app child-pays-for-parent conversation.

One test per beat of the ticket's done-when list, riding the REAL wiring
(the production dispatch table + mock chain + fake-device HWI signer,
reused from the RBF-004 harness — no network, deterministic):

* unconfirmed INBOUND resolution: exactly one → proceed naming it,
  several → indexed ask (amount + age + destination label verbatim from
  the store), none → the honest empty answer (zero fee-estimator calls);
* the THREE-option merge menu over CONFIRMED own coins only (smallest /
  largest / plain; a lone coin collapses the pair; no eligible coin →
  the single-input plan is staged directly — no fake menu);
* the merge plan built through ``build_cpfp_child_plan`` (spy-pinned: the
  chosen coin and the HONEST parent picture are what the handler passes);
* the plan card: ``Child pays for parent`` + the COUNCIL MUST hedge +
  the honest package rows (known parent fee → the builder's integer-DOWNED
  floor; unknown → the stated bound, never a fabricated package rate);
* the full TxFlow ride (dual-key confirm → sign revalidation → broadcast)
  with gates UNCHANGED and NO lineage write (store lineage is RBF-only);
* the broadcast-failure split (deliverable 5): a PROVEN-gone parent
  answers with the honest nothing-to-hurry line (never a retry pitch);
  everything else keeps the transient kept-for-retry answer;
* the conversation machinery: fee-knob persistence across both asks,
  any-next-utterance closure (never-trap), label words never reaching
  the model, the mid-conversation confirmation recheck, and the
  engine-derived destination a model-authored address can never be.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.protocol import EnvelopeValidationError, IntentName, validate_payload
from localwallet.tx.cpfp import StuckParent
from localwallet.tx.flow import GateDecision, TxFlowStatus
from localwallet.tx.selection import fee_sats_for
from localwallet.wallet.derivation import derive_addresses
from tests.test_e2e_skeleton import (
    SEND_RECIPIENT,
    _fixture_parsed,
    derive_fixture_addresses,
)
from tests.test_rbf004_bump import _add_coin, _FakeGen
from tests.test_tx_self_transfer import _hwi_table, _utxo

STUCK_TXID: Final[str] = "a" * 64  # the foreign parent of the stuck inbound


# ------------------------------------------------------------------ helpers


def _env(intent: str, params: dict[str, Any]):
    return validate_payload(json.dumps({"v": 0, "intent": intent, "params": params}))


def _cpfpreq(params: dict[str, Any] | None = None):
    return _env("self_transfer", {"mode": "cpfp", **(params or {})})


@pytest.fixture()
def world():
    """The RBF-004 harness world (one confirmed 100_000-sat coin at the
    first receive address), PRE-SCANNED via a throwaway ``get_balance``
    so every later dispatch's lazy scan stands down and the coins planted
    per test survive in the cache exactly as a rescan left them."""
    addrs = derive_fixture_addresses(8)
    state: dict[str, Any] = {}
    table, store, wallet, client, recorded, flow, session, signer = _hwi_table(
        {addrs[0]: [_utxo("d" * 64, 0, 100_000)]}, state=state
    )
    table[IntentName.GET_BALANCE](_env("get_balance", {}))  # run the first scan
    yield {
        "table": table, "store": store, "wallet": wallet, "client": client,
        "recorded": recorded, "flow": flow, "session": session, "signer": signer,
        "state": state, "addrs": addrs,
    }
    store.close()
    client.close()


def _drop_confirmed(world) -> None:
    """Drop the harness' seeded coin from the UTXO cache (a scan that saw
    it SPENT) — the single-input / no-eligible shapes need a coin set the
    tests fully control."""
    rows = world["store"].get_utxos_for_wallet(world["wallet"].id)
    world["store"].replace_utxos_for_wallet(
        world["wallet"].id, [r for r in rows if r.confirmed != 1]
    )


def _stuck(world, value: int = 50_000, *, txid: str = STUCK_TXID, index: int = 5) -> None:
    """Plant the stuck INBOUND coin: an unconfirmed payment credited to
    own receive index ``index`` — exactly what the watch poll leaves."""
    _add_coin(world["store"], world["wallet"].id, index, txid, value, confirmed=0)


def _fees(world) -> list:
    return [r for r in world["recorded"] if "/v1/fees" in r.url.path]


def _dispatch(world, params: dict[str, Any] | None = None) -> dict[str, object]:
    return world["table"][IntentName.SELF_TRANSFER](_cpfpreq(params))


def _menu(world, *, fee: dict[str, Any] | None = None) -> dict[str, object]:
    """Open the options menu: stuck coin + two eligible confirmed coins
    (30k smallest / 130k largest; the seeded 100k coin sits between and
    is presentation-trimmed, exactly like the bump chooser)."""
    _stuck(world)
    _add_coin(world["store"], world["wallet"].id, 6, "b" * 64, 30_000)
    _add_coin(world["store"], world["wallet"].id, 7, "c" * 64, 130_000)
    result = _dispatch(world, fee)
    assert result.get("ask") == "options", result
    return result


def _answer(world, line: str) -> list[str]:
    """Answer an open ask through the REAL deterministic intercept (the
    never-trap machinery); the model is never consulted."""
    fake = _FakeGen()
    loop = AgentLoop(fake, world["table"])
    outputs: list[str] = []
    app._run_turn(
        loop, world["flow"], world["session"], line, outputs.append,
        table=world["table"],
    )
    assert fake.prompts == []  # an ask answer never reached the model
    return outputs


def _selfride(world, *, amount: int = 20_000) -> str:
    """Broadcast a slow-rung send to the wallet's OWN receive address 3
    (the shape whose PARENT picture this app honestly knows: its fee and
    recorded estimate-vsize ride the flow's retained record) and return
    the broadcast txid."""
    table, session, flow = world["table"], world["session"], world["flow"]
    res = table[IntentName.CREATE_TX](
        _env("create_tx", {"recipient": world["addrs"][3], "amount_sats": amount, "fee_target": "slow"})
    )
    assert "tx_ref" in res, res
    ref = res["tx_ref"]
    session.gate_decision = GateDecision.CONFIRM
    table[IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": ref}))
    table[IntentName.SIGN_TX](_env("sign_tx", {"tx_ref": ref}))
    b = table[IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": ref}))
    assert b["status"] == "broadcast"
    assert flow.state is TxFlowStatus.BROADCAST
    return str(b["txid"])


class _BuilderSpy:
    """Monkeypatch seam on ``app.build_cpfp_child_plan``: records the
    parent picture / inbound coin / merge coin / rate of every call (and
    the returned plan) while delegating to the REAL builder — the ONLY
    money math."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[dict[str, Any]] = []
        real = app.build_cpfp_child_plan

        def spy(parent, inbound_coin, destination_script, fee_rate_centisat_vb, *, merge_coin=None):
            plan = real(
                parent,
                inbound_coin,
                destination_script,
                fee_rate_centisat_vb,
                merge_coin=merge_coin,
            )
            self.calls.append(
                {
                    "parent": parent,
                    "inbound": inbound_coin,
                    "script": destination_script,
                    "rate": fee_rate_centisat_vb,
                    "merge": merge_coin,
                    "plan": plan,
                }
            )
            return plan

        monkeypatch.setattr(app, "build_cpfp_child_plan", spy)


# =========================================================================
# 1. Unconfirmed-inbound resolution — single / multiple / none
# =========================================================================


def test_none_honest_answer_zero_fee_calls(world) -> None:
    """Deliverable 1 (none): the honest "nothing unconfirmed coming in"
    answer, value-free, ZERO fee-estimator calls, nothing staged."""
    assert _fees(world) == []
    result = _dispatch(world)
    assert result == {
        "error": "cpfp_nothing_unconfirmed",
        "detail": app._CPFP_NOTHING_UNCONFIRMED,
    }
    assert _fees(world) == []
    lines: list[str] = []
    app._print_self_transfer(result, lines.append)
    assert lines == [app._CPFP_NOTHING_UNCONFIRMED]
    assert world["flow"].state is TxFlowStatus.IDLE


def test_single_inbound_proceeds_naming_it(world, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliverable 1 (exactly one): proceed, naming it — its txid rides
    the card verbatim and the foreign parent stays HONESTLY unknown (the
    stuck coin was planted from a chain the wallet still holds confirmed
    coins on, so answer the menu plain to reach the staged card)."""
    spy = _BuilderSpy(monkeypatch)
    _drop_confirmed(world)  # single inbound + no merge candidates → direct
    _stuck(world)
    result = _dispatch(world)
    assert result.get("ask") is None and result.get("error") is None
    assert result["cpfp_parent_txid"] == STUCK_TXID
    assert result["cpfp_parent_fee_known"] is False
    assert spy.calls[-1]["parent"] == StuckParent(STUCK_TXID, None, None)
    assert spy.calls[-1]["inbound"].txid == STUCK_TXID
    assert spy.calls[-1]["inbound"].value_sats == 50_000
    assert world["flow"].state is TxFlowStatus.CREATED


def test_multiple_inbound_indexed_ask_verbatim_fields(world) -> None:
    """Deliverable 1 (several): the indexed choice ask — amount + age +
    destination label VERBATIM from the store (an unrecorded age states
    itself, never invented); nothing guesses, nothing stages, zero
    fee-estimator calls."""
    _stuck(world, value=50_000, txid=STUCK_TXID, index=5)
    _stuck(world, value=25_000, txid="e" * 64, index=6)
    world["store"].set_coin_label(world["wallet"].id, "e" * 64, 0, tags=("p2p",))
    result = _dispatch(world)
    assert result.get("ask") == "coin"
    entries = result["options"]
    # canonical ascending (value_sats, txid, vout) — deterministic.
    assert [e["value_sats"] for e in entries] == [25_000, 50_000]
    assert [e["txid"] for e in entries] == ["e" * 64, STUCK_TXID]
    assert [e["age_s"] for e in entries] == [None, None]
    assert entries[0]["label"] == "'p2p'" and entries[1]["label"] is None
    assert world["flow"].state is TxFlowStatus.IDLE
    assert _fees(world) == []
    lines: list[str] = []
    app._print_self_transfer(result, lines.append)
    assert any("2 unconfirmed payments" in line for line in lines)
    assert any("25,000 sats · age not recorded" in line for line in lines)
    assert any("labeled 'p2p'" in line for line in lines)


def test_coin_ask_number_answer_proceeds_with_that_payment(world) -> None:
    """The coin ask closes through the deterministic intercept: "2"
    proceeds with THAT payment (outpoint from the ask record — never a
    re-guess of position), then the menu (or the direct plan) follows."""
    _stuck(world, value=50_000, txid=STUCK_TXID, index=5)
    _stuck(world, value=25_000, txid="e" * 64, index=6)
    result = _dispatch(world)
    assert result.get("ask") == "coin"
    _answer(world, "2")
    ask = world["session"].cpfp_ask
    assert ask is not None and ask.kind == "options"  # next leg for coin #2
    assert ask.inbound is not None
    assert ask.inbound.txid == STUCK_TXID  # the CHOSEN payment, not [0]


# =========================================================================
# 2. The THREE-option menu (merge candidates) and the direct fallback
# =========================================================================


def test_three_option_menu_over_eligible_confirmed_coins(world) -> None:
    """Deliverable 2: smallest-merge / largest-merge / plain, presented
    ONLY over CONFIRMED own coins (the unconfirmed stuck coins are never
    merge material); framing words + amounts print verbatim."""
    result = _menu(world)
    assert [o["framing"] for o in result["options"]] == ["smallest", "largest", "plain"]
    assert [o["value_sats"] for o in result["options"]] == [30_000, 130_000, None]
    lines: list[str] = []
    app._print_self_transfer(result, lines.append)
    card = "\n".join(lines)
    assert "merge your smallest coin — 30,000 sats" in card
    assert "merge your largest coin — 130,000 sats" in card
    assert "no merge — just the stuck payment" in card
    assert "Any other words set this aside." in card


def test_unconfirmed_coins_are_never_merge_material(world) -> None:
    """The merge set excludes EVERY unconfirmed coin (the stuck ones and
    any other pending inbound): only CONFIRMED own coins are eligible."""
    _drop_confirmed(world)  # one eligible coin only (the seeded 100k removed)
    _stuck(world, value=50_000, txid=STUCK_TXID, index=5)
    _stuck(world, value=35_000, txid="e" * 64, index=6)  # a 2nd inbound
    _add_coin(world["store"], world["wallet"].id, 7, "c" * 64, 130_000, confirmed=1)
    _dispatch(world)  # opens the COIN ask (two inbounds) — no menu yet
    assert world["session"].cpfp_ask is not None
    assert world["session"].cpfp_ask.kind == "coin"
    _answer(world, "1")  # pick the 35k payment
    ask = world["session"].cpfp_ask
    assert ask is not None and ask.kind == "options"
    # Exactly ONE eligible coin (130k confirmed) — the stuck 50k coin is
    # NOT offered: the pair collapses to coin + plain.
    assert [o.framing for o in ask.options] == ["coin", "plain"]
    assert [o.value_sats for o in ask.options] == [130_000, None]


def test_single_eligible_coin_collapses_the_pair(world) -> None:
    """ONE eligible confirmed coin → the menu never offers it twice:
    "coin" + plain."""
    _drop_confirmed(world)
    _stuck(world)
    _add_coin(world["store"], world["wallet"].id, 6, "b" * 64, 30_000)
    result = _dispatch(world)
    assert result.get("ask") == "options"
    assert [o["framing"] for o in result["options"]] == ["coin", "plain"]


def test_no_eligible_coin_stages_single_input_directly(world, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliverable 2 (no fake menu): no eligible CONFIRMED merge coin →
    the single-input child is staged directly, merge-free."""
    spy = _BuilderSpy(monkeypatch)
    _drop_confirmed(world)
    _stuck(world)
    result = _dispatch(world)
    assert result.get("ask") is None and result.get("error") is None
    assert result["inputs_count"] == 1
    assert result["cpfp_merged"] is False
    assert spy.calls[-1]["merge"] is None


def test_menu_answers_route_the_chosen_coin_to_the_builder(world, monkeypatch: pytest.MonkeyPatch) -> None:
    """Merge plan through ``build_cpfp_child_plan`` (spy): answer "1"
    merges the SMALLEST coin, "largest" the largest, "3" plain — the
    chosen coin object rides the builder verbatim, always paired with the
    honest (unknown-parent) picture for a foreign payment."""
    spy = _BuilderSpy(monkeypatch)
    _stuck(world)
    _add_coin(world["store"], world["wallet"].id, 6, "b" * 64, 30_000)
    _add_coin(world["store"], world["wallet"].id, 7, "c" * 64, 130_000)

    def reopen() -> None:
        """Reset conversation/flow state and re-open the SAME menu (the
        coins stay planted — re-planting would duplicate cache rows)."""
        if world["flow"].state is TxFlowStatus.CREATED:
            world["flow"].cancel()
        world["session"].cpfp_pending = None
        world["session"].cpfp_ask = None
        assert _dispatch(world).get("ask") == "options"

    reopen()
    _answer(world, "1")
    assert world["flow"].state is TxFlowStatus.CREATED
    assert spy.calls[-1]["merge"].txid == "b" * 64  # the 30k smallest
    assert spy.calls[-1]["parent"] == StuckParent(STUCK_TXID, None, None)
    assert world["flow"].pending is not None
    assert world["flow"].pending.inputs_count == 2
    reopen()
    _answer(world, "largest")
    assert spy.calls[-1]["merge"].txid == "c" * 64  # the framing word
    reopen()
    _answer(world, "3")
    assert spy.calls[-1]["merge"] is None
    assert world["flow"].pending is not None
    assert world["flow"].pending.inputs_count == 1


def test_label_word_answers_the_menu_never_via_model(world) -> None:
    """A coin LABEL word answers the menu IN CODE; the label word appears
    in NO model prompt — not on the answer turn, not on a later one."""
    _stuck(world)
    _add_coin(world["store"], world["wallet"].id, 6, "b" * 64, 30_000)
    world["store"].set_coin_label(world["wallet"].id, "b" * 64, 0, note="vault")
    _add_coin(world["store"], world["wallet"].id, 7, "c" * 64, 130_000)
    result = _dispatch(world)
    assert result.get("ask") == "options"
    assert result["options"][0]["label"] == "'vault'"
    fake = _FakeGen()
    loop = AgentLoop(fake, world["table"])
    app._run_turn(
        loop, world["flow"], world["session"], "the vault one", [].append,
        table=world["table"],
    )
    assert fake.prompts == []
    assert world["flow"].state is TxFlowStatus.CREATED
    app._run_turn(
        loop, world["flow"], world["session"], "what now?", [].append,
        table=world["table"],
    )
    assert fake.prompts  # the LATER turn is ordinary model chat …
    assert all("vault" not in p for p in fake.prompts)  # … without the label


# =========================================================================
# 3. The plan card — framing, the COUNCIL hedge, honest package rows
# =========================================================================


def _direct_plan(world, params: dict[str, Any] | None = None) -> dict[str, object]:
    """Stuck coin, no merge candidates → the direct single-input staged
    plan (deterministic card material for the renderer tests)."""
    _drop_confirmed(world)
    _stuck(world)
    result = _dispatch(world, params)
    assert result.get("ask") is None and result.get("error") is None, result
    return result


def test_card_framing_and_council_hedge(world) -> None:
    """Deliverable 3: the ``Child pays for parent`` framing + the COUNCIL
    MUST hedge VERBATIM ("spends a payment that hasn't confirmed yet — if
    that payment is undone, this won't send") — the reorg/undo hedge —
    and the unchanging dual-key ask line."""
    result = _direct_plan(world)
    lines: list[str] = []
    app._print_self_transfer(result, lines.append, session=world["session"])
    assert any("Child pays for parent" in line for line in lines)
    assert (
        "Heads up: this spends a payment that hasn't confirmed yet — if that "
        "payment is undone, this won't send" in lines
    )
    assert any(f"Hurries: {STUCK_TXID}" in line for line in lines)
    assert lines[0] == (
        'Pending — say "sign" to review it on your device, or "cancel" to discard.'
    )


def test_card_parent_fee_unknown_honest_bound(world) -> None:
    """Foreign parent (the normal inbound): parent_fee_known False → the
    card states the bound and NO package rate figure is fabricated (the
    result carries no package number either — tx/cpfp.py's honest shape)."""
    result = _direct_plan(world)
    assert result["cpfp_parent_fee_known"] is False
    assert result["cpfp_package_fee_rate_centisat_vb"] is None
    lines: list[str] = []
    app._print_self_transfer(result, lines.append)
    assert app._CPFP_PACKAGE_UNKNOWN in lines
    assert not any("Package" in line and "sat/vB" in line for line in lines)


def test_card_parent_fee_known_package_floor(world, monkeypatch: pytest.MonkeyPatch) -> None:
    """The honest-known shape: this app itself broadcast the hurried
    payment (fee + recorded ESTIMATE-vsize ride the flow's retained
    record) → the child pays the parent SHORTFALL on top of its self
    cost, and the card prints the builder's integer-DOWNED package-rate
    floor."""
    spy = _BuilderSpy(monkeypatch)
    txid = _selfride(world)
    assert world["flow"].txid == txid
    _drop_confirmed(world)  # a rescan saw the ride's input spent → direct plan
    # The payment to own address 3 is unconfirmed in (the stuck inbound).
    _add_coin(world["store"], world["wallet"].id, 3, txid, 20_000, confirmed=0)
    result = _dispatch(world)
    assert result.get("ask") is None and result.get("error") is None, result
    call = spy.calls[-1]
    plan = call["plan"]
    assert call["parent"] == StuckParent(txid, 141, 141)  # the slow ride's record
    assert result["cpfp_parent_fee_known"] is True
    assert result["cpfp_parent_fee_sats"] == 141
    assert result["fee_sats"] == plan.fee_sats
    pkg = result["cpfp_package_fee_rate_centisat_vb"]
    assert isinstance(pkg, int)
    # The builder's own integer-exact numbers: the child paid MORE than its
    # self cost (the parent's shortfall at the same bid rode the fee), and
    # the package rate is the integer-DOWNED floor of the combined picture.
    assert plan.fee_sats > fee_sats_for(plan.vsize, 300)
    assert plan.fee_sats == fee_sats_for(plan.vsize + 141, 300) - 141
    assert pkg == (141 + plan.fee_sats) * 100 // (141 + plan.vsize)
    card: list[str] = []
    app._print_cpfp_plan(result, card.append)
    assert any(line.startswith("Package: at least ") for line in card)
    assert app._CPFP_PACKAGE_UNKNOWN not in card


def test_refusal_details_stay_value_free(world) -> None:
    """Discipline: every cpfp copy line a refusal carries is value-free
    (figures ride STRUCTURED keys; the card renders from those only)."""
    _direct_plan(world)
    for line in (
        app._CPFP_NOTHING_UNCONFIRMED,
        app._CPFP_ALREADY_CONFIRMED,
        app._CPFP_INBOUND_GONE,
        app._CPFP_COIN_GONE,
        app._CPFP_CANNOT_FUND,
        app._CPFP_PLAN_FAILED,
        app._CPFP_FLOW_BUSY,
        app._CPFP_PARENT_GONE,
        app._CPFP_PACKAGE_UNKNOWN,
        app._CPFP_HEDGE_LINE,
    ):
        assert not any(ch.isdigit() for ch in line), line


# =========================================================================
# 4. Fee knobs, refusals, posture
# =========================================================================


def test_fee_rung_persists_across_the_option_ask(world, monkeypatch: pytest.MonkeyPatch) -> None:
    """The RBF-004 MAJOR lesson re-pinned for cpfp: a stated rung survives
    the menu — the answer re-quotes it and the builder bids THAT rate,
    never a silent FAST default."""
    spy = _BuilderSpy(monkeypatch)
    _menu(world, fee={"fee_target": "slow"})
    assert world["session"].cpfp_ask is not None
    assert world["session"].cpfp_ask.fee_target == "slow"
    _answer(world, "3")
    assert spy.calls[-1]["rate"] == 100  # 1 sat/vB (the slow rung), not 300
    assert world["flow"].pending is not None
    assert world["flow"].pending.fee_rate_centisat_vb == 100
    assert world["flow"].pending.fee_target == "slow"


def test_fee_rung_persists_across_the_coin_ask(world, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same persistence on the coin-ask leg: the rung stated on turn
    1 is what the eventual plan bids (it rides BOTH asks in sequence)."""
    spy = _BuilderSpy(monkeypatch)
    _stuck(world, value=50_000, txid=STUCK_TXID, index=5)
    _stuck(world, value=25_000, txid="e" * 64, index=6)
    result = _dispatch(world, {"fee_target": "medium"})
    assert result.get("ask") == "coin"
    assert world["session"].cpfp_ask.fee_target == "medium"
    _answer(world, "1")  # the 25k payment; the seeded 100k coin is eligible
    ask = world["session"].cpfp_ask
    assert ask is not None and ask.kind == "options"
    assert ask.fee_target == "medium"  # the knob rode the SECOND ask too
    _answer(world, "2")  # plain
    assert spy.calls[-1]["rate"] == 200  # 2 sat/vB (the medium rung)


def test_default_rung_is_fast(world) -> None:
    """Documented engine policy (inheriting RBF-004's decision): a hurry
    request IS a stated urgency — the cpfp default rung is FAST."""
    result = _direct_plan(world)
    assert result["fee_target"] == "fast"
    assert result["fee_rate_centisat_vb"] == 300


def test_busy_posture_refuses_value_free(world) -> None:
    """CONFIRMED mid-lifecycle → the honest busy refusal, value-free,
    zero fee-estimator calls (the flow-posture guard pattern)."""
    res = world["table"][IntentName.CREATE_TX](
        _env("create_tx", {"recipient": SEND_RECIPIENT, "amount_sats": 20_000, "fee_target": "slow"})
    )
    assert "tx_ref" in res, res
    world["session"].gate_decision = GateDecision.CONFIRM
    world["table"][IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": res["tx_ref"]}))
    before = len(_fees(world))
    result = _dispatch(world)
    assert result == {"error": "cpfp_flow_busy", "detail": app._CPFP_FLOW_BUSY}
    assert len(_fees(world)) == before
    assert world["flow"].state is TxFlowStatus.CONFIRMED  # untouched


def test_first_scan_gate_refuses_first(world) -> None:
    """The ADR-0022 first-scan gate holds for cpfp — same line, same
    position as create_tx (refusal BEFORE any store or network work)."""
    _t, store, wallet, client, _rec, flow, session, _s = _hwi_table({})
    try:
        handler = app._make_self_transfer_handler(
            store, wallet.id, _fixture_parsed(), flow,
            SimpleNamespace(estimate=lambda _t: (_ for _ in ()).throw(AssertionError("no chain call"))),
            lambda: None,
            session=session,
            scan_gate=SimpleNamespace(first_scan_incomplete=True),
        )
        result = handler(_cpfpreq())
        assert result == {"error": "wallet_loading", "detail": app.WALLET_LOADING_REFUSAL}
    finally:
        store.close()
        client.close()


def test_session_less_wiring_keeps_the_value_free_backstop(world) -> None:
    """The ONE refusal CPFP-002 leaves standing: a session-less direct
    wiring (legacy call sites) cannot carry the conversation's state —
    the clean value-free line survives for exactly that shape."""
    _t, store, wallet, client, _rec, flow, _session, _s = _hwi_table({})
    try:
        handler = app._make_self_transfer_handler(
            store, wallet.id, _fixture_parsed(), flow,
            SimpleNamespace(estimate=lambda _t: (_ for _ in ()).throw(AssertionError("no chain call"))),
            lambda: None,
        )
        result = handler(_cpfpreq())
        assert result == {"error": "cpfp_unavailable", "detail": app._CPFP_NOT_READY}
    finally:
        store.close()
        client.close()


def test_cannot_fund_refusal_is_value_free_with_machine_reason(world, monkeypatch: pytest.MonkeyPatch) -> None:
    """The builder's fee-math refusal (inbound too small for any honest
    child at the bid) answers with the value-free line + machine-readable
    reason key — never sats in a detail string (ADR-0012 CPFP amendment:
    the card's figures come from the PLAN, never an error path)."""
    from localwallet.tx.cpfp import CpfpError, CpfpRefusalReason

    def tiny(parent, inbound_coin, destination_script, fee_rate_centisat_vb, *, merge_coin=None):
        raise CpfpError(
            "cpfp child refused (fee_exceeds_funds): bounded fee exceeds coin value",
            CpfpRefusalReason.FEE_EXCEEDS_FUNDS,
        )

    monkeypatch.setattr(app, "build_cpfp_child_plan", tiny)
    result = _direct_plan_no_assert(world)
    assert result.get("error") == "cpfp_cannot_fund"
    assert result["reason"] == "fee_exceeds_funds"
    assert not any(ch.isdigit() for ch in str(result["detail"])), result["detail"]
    assert world["flow"].state is not TxFlowStatus.CREATED
    lines: list[str] = []
    app._print_self_transfer(result, lines.append)
    assert lines == [app._CPFP_CANNOT_FUND]


def _direct_plan_no_assert(world) -> dict[str, object]:
    _drop_confirmed(world)
    _stuck(world, value=400)  # a sub-fee dust payment
    return _dispatch(world)


# =========================================================================
# 5. Mid-conversation rechecks and never-trap closure
# =========================================================================


def _flip_stuck(world, *, confirmed: bool, drop: bool) -> None:
    """Mimic the scan's verdict on the stuck payment while an ask stands
    open: confirmed=1 (it landed) or row gone (it was undone/replaced)."""
    rows = world["store"].get_utxos_for_wallet(world["wallet"].id)
    kept = [r for r in rows if not (r.txid == STUCK_TXID and (drop or confirmed))]
    if confirmed and not drop:
        kept += [
            dataclasses.replace(r, confirmed=1) for r in rows if r.txid == STUCK_TXID
        ]
    world["store"].replace_utxos_for_wallet(world["wallet"].id, kept)


def test_menu_answer_after_payment_confirmed_refuses(world) -> None:
    """The mid-conversation recheck: the payment CONFIRMS while the menu
    stands open → answering refuses honestly (a child is never staged
    against a payment that no longer needs hurrying)."""
    _menu(world)
    _flip_stuck(world, confirmed=True, drop=False)
    _answer(world, "1")
    assert world["flow"].state is TxFlowStatus.IDLE  # NOTHING staged
    assert world["session"].cpfp_ask is None
    assert world["session"].cpfp_pending is None


def test_menu_answer_after_payment_gone_refuses(world) -> None:
    """Payment UNDONE (the row leaves the cache as a scan would drop an
    evicted inbound) → the honest gone answer, nothing staged."""
    _menu(world)
    _flip_stuck(world, confirmed=False, drop=True)
    _answer(world, "2")
    assert world["flow"].state is TxFlowStatus.IDLE
    assert world["session"].cpfp_pending is None


def test_menu_answer_after_chosen_merge_coin_spent_refuses(world) -> None:
    """The CHOSEN MERGE COIN leaves the confirmed set while the menu
    stands open (spent elsewhere) → the honest gone answer; a different
    coin is never silently substituted."""
    _menu(world)
    rows = world["store"].get_utxos_for_wallet(world["wallet"].id)
    world["store"].replace_utxos_for_wallet(
        world["wallet"].id, [r for r in rows if r.txid != "b" * 64]
    )
    _answer(world, "1")
    assert world["flow"].state is TxFlowStatus.IDLE
    assert world["session"].cpfp_pending is None


def test_any_next_utterance_closes_the_menu(world) -> None:
    """Never-trap: an unrelated utterance while the menu stands closes it
    AND goes where the user sent it (the model)."""
    _menu(world)
    fake = _FakeGen()
    loop = AgentLoop(fake, world["table"])
    app._run_turn(
        loop, world["flow"], world["session"], "actually, what's my balance?",
        [].append, table=world["table"],
    )
    assert world["session"].cpfp_ask is None
    assert fake.prompts


def test_deny_word_suppresses_menu_answer(world) -> None:
    """"don't take the largest" must never merge a coin (the shared
    matcher's deny guard): the ask closes, nothing dispatches."""
    _menu(world)
    fake = _FakeGen()
    loop = AgentLoop(fake, world["table"])
    app._run_turn(
        loop, world["flow"], world["session"], "don't take the largest",
        [].append, table=world["table"],
    )
    assert world["session"].cpfp_ask is None
    assert world["flow"].state is TxFlowStatus.IDLE


def test_cancel_retires_cpfp_state(world) -> None:
    """A DENY-cancel of the staged child retires the cpfp marker and any
    open ask (they belonged to THIS flow — never leak onto the next)."""
    _direct_plan(world)
    assert world["session"].cpfp_pending is not None
    fake = _FakeGen()
    loop = AgentLoop(fake, world["table"])
    app._run_turn(
        loop, world["flow"], world["session"], "no cancel it", [].append,
        table=world["table"],
    )
    assert world["session"].cpfp_pending is None
    assert world["session"].cpfp_ask is None
    assert world["flow"].state is TxFlowStatus.CANCELLED


# =========================================================================
# 6. The full ride — gates unchanged, destination engine-derived
# =========================================================================


def test_destination_is_engine_derived_model_authorship_impossible(world) -> None:
    """Deliverable 4: the fresh own-address destination is DERIVED at
    branch-0 ``next_index`` and ALLOCATED after a successful build; the
    cpfp params structurally cannot carry an address — a model-authored
    destination is unrepresentable (schema layer 2 refuses the key)."""
    _drop_confirmed(world)
    before = world["store"].get_derivation(world["wallet"].id, 0).next_index
    _stuck(world)
    result = _dispatch(world)
    expected = derive_addresses(_fixture_parsed(), 0, before, 1)[0].address
    assert result["recipient"] == expected
    assert result["self_destinations"] == [
        {"address": expected, "amount_sats": result["amount_sats"]}
    ]
    after = world["store"].get_derivation(world["wallet"].id, 0).next_index
    assert after == before + 1  # allocated exactly the one fresh index
    record = world["store"].get_by_address(expected)
    assert record is not None and record.status == "allocated"
    with pytest.raises(EnvelopeValidationError):
        validate_payload(json.dumps(
            {"v": 0, "intent": "self_transfer",
             "params": {"mode": "cpfp", "address": expected}}
        ))


def test_full_ride_dual_key_unchanged(world) -> None:
    """Deliverable 4: the child rides create→confirm→sign→broadcast; an
    LLM-relayed confirm WITHOUT the same-turn gate is REFUSED; the dual
    key passes; sign-time revalidation independently re-derives it."""
    result = _direct_plan(world)
    ref = result["tx_ref"]
    assert world["flow"].state is TxFlowStatus.CREATED
    world["session"].gate_decision = GateDecision.NOT_A_DECISION
    refused = world["table"][IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": ref}))
    assert refused["error"] == "confirm_refused"
    world["session"].gate_decision = GateDecision.CONFIRM
    confirmed = world["table"][IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": ref}))
    assert confirmed["status"] == "confirmed"
    signed = world["table"][IntentName.SIGN_TX](_env("sign_tx", {"tx_ref": ref}))
    assert signed["status"] == "signed"
    assert world["flow"].state is TxFlowStatus.SIGNED


def test_broadcast_success_retires_marker_writes_no_lineage(world, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliverable 4 + the no-lineage rule: a successful child broadcast
    retires the conversation marker and touches NOTHING in the lineage
    machinery (``record_replacement`` must never run — store lineage is
    RBF-only; a child→parent link would be new schema)."""

    def _never(*args: object, **kwargs: object) -> None:
        raise AssertionError("CPFP must never write RBF lineage")

    monkeypatch.setattr(app.Store, "record_replacement", _never)
    result = _direct_plan(world)
    ref = result["tx_ref"]
    world["session"].gate_decision = GateDecision.CONFIRM
    world["table"][IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": ref}))
    world["table"][IntentName.SIGN_TX](_env("sign_tx", {"tx_ref": ref}))
    b = world["table"][IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": ref}))
    assert b["status"] == "broadcast"
    assert "replaces_txid" not in b  # the bump supersede narration never fires
    assert world["session"].cpfp_pending is None
    rows = world["store"].get_txs_for_wallet(world["wallet"].id)
    child = next(r for r in rows if r.txid == b["txid"])
    assert child.replaced_by_txid is None


def test_reshow_while_child_pends_is_the_cpfp_card(world) -> None:
    """A second cpfp envelope while MY child pends re-shows the plan as
    THE CPFP card (carried display fields + the flow record's own
    numbers) — never re-staged, never a misleading generic reshape."""
    staged = _direct_plan(world)
    again = _dispatch(world)
    assert again["error"] == "tx_pending"
    assert again["cpfp"] is True
    assert again["tx_ref"] == staged["tx_ref"]  # NOT re-staged
    assert again["cpfp_parent_txid"] == STUCK_TXID
    lines: list[str] = []
    app._print_self_transfer(again, lines.append, session=world["session"])
    assert any("Child pays for parent" in line for line in lines)
    assert any("Still pending" in line for line in lines)


# =========================================================================
# 7. Broadcast-failure classification (deliverable 5)
# =========================================================================


def _sign_child(world) -> str:
    result = _direct_plan(world)
    ref = result["tx_ref"]
    world["session"].gate_decision = GateDecision.CONFIRM
    world["table"][IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": ref}))
    world["table"][IntentName.SIGN_TX](_env("sign_tx", {"tx_ref": ref}))
    return ref


def test_broadcast_transient_keeps_retry_answer(world) -> None:
    """A failed broadcast with the parent STILL LIVE (the cache holds the
    unconfirmed coin and the backend still knows it) is the ordinary
    transient answer: kept for retry, byte-familiar."""
    ref = _sign_child(world)
    world["state"]["broadcast_fail"] = True
    failed = world["table"][IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": ref}))
    assert failed["error"] == "broadcast_failed"
    lines: list[str] = []
    app._print_broadcast_tx(failed, lines.append)
    assert any("kept; say 'broadcast' to retry" in line for line in lines)
    assert world["session"].cpfp_pending is not None  # still the staged child


def test_broadcast_parent_gone_from_store_truth(world) -> None:
    """The payment VANISHED from the wallet's own view (the scan saw it
    undone/replaced) → the honest nothing-to-hurry answer, NO retry
    pitch; the answer is STABLE (a repeat attempt re-answers from store
    truth with no extra network call — never a retry loop we feed)."""
    ref = _sign_child(world)
    _flip_stuck(world, confirmed=False, drop=True)
    world["state"]["broadcast_fail"] = True
    failed = world["table"][IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": ref}))
    assert failed == {"error": "cpfp_parent_gone", "detail": app._CPFP_PARENT_GONE}
    lines: list[str] = []
    app._print_broadcast_tx(failed, lines.append)
    assert lines == [app._CPFP_PARENT_GONE]
    assert not any("retry" in line for line in lines)
    statuses_before = len([r for r in world["recorded"] if "/status" in r.url.path])
    again = world["table"][IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": ref}))
    assert again["error"] == "cpfp_parent_gone"
    assert len([r for r in world["recorded"] if "/status" in r.url.path]) == statuses_before


def test_broadcast_parent_gone_from_chain_truth(world) -> None:
    """The cache still shows the coin (no rescan since the eviction) but
    the BACKEND no longer knows the parent (status 404 — the documented
    recovery GET, one call, only on an already-failed cpfp-child
    broadcast) → proven gone → the same honest answer."""
    ref = _sign_child(world)
    world["state"]["broadcast_fail"] = True
    world["state"]["status_404"] = True
    failed = world["table"][IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": ref}))
    assert failed["error"] == "cpfp_parent_gone"
    assert any("/status" in r.url.path for r in world["recorded"])
    lines: list[str] = []
    app._print_broadcast_tx(failed, lines.append)
    assert lines == [app._CPFP_PARENT_GONE]


def test_broadcast_parent_confirmed_stays_transient(world) -> None:
    """The cache still shows the coin unconfirmed but the BACKEND reports
    it CONFIRMED (a stale cache, a live payment) → nothing is proven
    gone; the failure is unrelated → the honest retryable answer stands."""
    ref = _sign_child(world)
    world["state"]["broadcast_fail"] = True
    world["state"]["tx_status_payload"] = {
        "txid": STUCK_TXID, "block_height": 900_001, "block_time": 1_700_000_500,
    }
    failed = world["table"][IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": ref}))
    assert failed["error"] == "broadcast_failed"


def test_broadcast_unproven_gone_stays_transient(world) -> None:
    """Absence of evidence is NEVER evidence of death: the recovery GET
    itself errors (backend down) → the classifier condemns nothing and
    the retryable answer stands."""
    ref = _sign_child(world)
    world["state"]["broadcast_fail"] = True
    world["state"]["status_fail"] = True
    failed = world["table"][IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": ref}))
    assert failed["error"] == "broadcast_failed"


def test_ordinary_broadcast_failures_never_grow_the_recheck(world) -> None:
    """The classification is scoped to STAGED CPFP CHILDREN only: an
    ordinary send's failed broadcast makes no status recheck call and
    answers exactly as before (byte-identical transient copy)."""
    res = world["table"][IntentName.CREATE_TX](
        _env("create_tx", {"recipient": SEND_RECIPIENT, "amount_sats": 20_000, "fee_target": "slow"})
    )
    assert "tx_ref" in res, res
    world["session"].gate_decision = GateDecision.CONFIRM
    world["table"][IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": res["tx_ref"]}))
    world["table"][IntentName.SIGN_TX](_env("sign_tx", {"tx_ref": res["tx_ref"]}))
    statuses_before = len([r for r in world["recorded"] if "/status" in r.url.path])
    world["state"]["broadcast_fail"] = True
    failed = world["table"][IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": res["tx_ref"]}))
    assert failed["error"] == "broadcast_failed"
    assert len([r for r in world["recorded"] if "/status" in r.url.path]) == statuses_before


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

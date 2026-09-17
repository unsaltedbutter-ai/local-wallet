"""TCK-UTXO-007 — engine selected-coin sends ("… using #3 #7 #9").

The UX council's binding contract (both critics BUILD-WITH-CHANGES,
2026-09-16), tested per clause:

* the coin set is parsed ONLY from the user's OWN utterance, PRE-MODEL
  (the model never authors or edits it and never sees the turn — named
  forms consume with ``prompts == []``);
* DEICTIC forms ("using the selected UTXOs", "with these UTXOs") are
  RELEASED to the model's own clarify — the engine never resolves
  client-side selection state (pinned BOTH ways: the release stands and
  the full line reaches the model);
* no envelope key ever carries a coin reference (the spy pins the
  dispatched params to the recipient/amount/fee-knob universe);
* pin the POOL, never the POLICY: select_coins runs over exactly the
  named coins with every layer unchanged, and EVERY deviation from the
  named set (skipped dust, the subset a pure-pool/single-coin preference
  picked, the fold, the change folded into fee) is enumerated on the
  card — which names every FINAL input as ``#N · <full address> ·
  <sats>`` (the ``_CONS_SOURCE_ROW`` shape);
* stale/gone coin = the handler's FRESH-store re-resolution hard-stops
  naming WHICH numbers (flow IDLE, nothing staged, no model, never a
  silent substitute, never a survivors-only rebuild); unknown number =
  the EXISTING value-free ``ADDRESS_REF_UNKNOWN`` clarify;
* mixed pools are allowed and NARRATED (the UTXO-002 mix warning —
  consolidation's ``_CONS_POOLS_APART`` refusal is a different flow and
  is deliberately NOT imported; one rule, stated on the card);
* the insufficient path is the existing needed/available honesty,
  SCOPED to the pinned pool and naming the numbers;
* the 256-input ceiling (TCK-TX-SELF-001) refuses a huge named pool —
  never truncates it (intercept leg AND handler leg);
* a pinned pending stays pinned across every re-quote (gone coin stops
  the rebuild, original card intact), the re-show renders the frozen
  enumeration, and the dual-key confirm gate is UNCHANGED (full ride).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.config import (
    CONSOLIDATE_BELOW_SAT_VB_SETTING,
    UTXO_TARGET_MAX_SETTING,
    UTXO_TARGET_MIN_SETTING,
)
from localwallet.protocol import IntentName, validate_payload
from localwallet.store import UtxoRecord
from localwallet.tx.flow import GateDecision, TxFlowStatus
from tests.test_cons001_conversation import _env
from tests.test_cons003_flexible_selection import _show_all
from tests.test_e2e_skeleton import SEND_RECIPIENT, derive_fixture_addresses
from tests.test_rbf004_bump import _add_coin, _FakeGen
from tests.test_tx_self_transfer import _hwi_table, _utxo


class _PlanGen:
    """Records prompts; answers the planned envelopes, then a respond."""

    def __init__(self, envelopes: list[str] | None = None) -> None:
        self.prompts: list[str] = []
        self.envelopes = envelopes or []

    def __call__(self, prompt: str, grammar_text: str | None) -> str:
        del grammar_text
        self.prompts.append(prompt)
        if self.envelopes:
            return self.envelopes.pop(0)
        return json.dumps({"v": 0, "intent": "respond", "params": {"text": "ok."}})


@pytest.fixture()
def world():
    """The CPFP/CONS harness world (one confirmed 100_000-sat coin at the
    FIRST receive address → registry number 1), pre-scanned."""
    addrs = derive_fixture_addresses(8)
    state: dict[str, Any] = {}
    table, store, wallet, client, recorded, flow, session, signer = _hwi_table(
        {addrs[0]: [_utxo("d" * 64, 0, 100_000)]}, state=state
    )
    table[IntentName.GET_BALANCE](_env("get_balance", {}))
    yield {
        "table": table, "store": store, "wallet": wallet, "client": client,
        "recorded": recorded, "flow": flow, "session": session, "signer": signer,
        "state": state, "addrs": addrs,
    }
    store.close()
    client.close()


def _turn(world, line: str, gen: Any | None = None):
    """One REAL REPL turn through the production intercept chain."""
    fake = gen if gen is not None else _FakeGen()
    loop = AgentLoop(fake, world["table"])
    outputs: list[str] = []
    app._run_turn(
        loop, world["flow"], world["session"], line, outputs.append,
        table=world["table"], store=world["store"],
    )
    return outputs, fake


def _pin_coins(world) -> None:
    """Register numbers 1..8, then plant #3 = 30k (addr idx 2) and
    #4 = 40k (addr idx 3); #1 stays the seeded 100k."""
    _show_all(world, 8)
    _add_coin(world["store"], world["wallet"].id, 2, "a" * 64, 30_000)
    _add_coin(world["store"], world["wallet"].id, 3, "b" * 64, 40_000)


def _spy_create(world) -> list[Any]:
    """Wrap the wired create_tx handler: records the EXACT envelope."""
    real = world["table"][IntentName.CREATE_TX]
    seen: list[Any] = []

    def spy(envelope):
        seen.append(envelope)
        return real(envelope)

    world["table"][IntentName.CREATE_TX] = spy
    return seen


def _ride(world) -> None:
    """Full dual-key ride (confirm gate → sign → broadcast) — the
    UNCHANGED state machine."""
    table, session, flow = world["table"], world["session"], world["flow"]
    ref = flow.pending.tx_ref  # type: ignore[union-attr]
    session.gate_decision = GateDecision.CONFIRM
    c = table[IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": ref}))
    assert c.get("status") == "confirmed", c
    s = table[IntentName.SIGN_TX](_env("sign_tx", {"tx_ref": ref}))
    assert s.get("status") == "signed", s
    b = table[IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": ref}))
    assert b["status"] == "broadcast", b


# =========================================================================
# 1. The intercept grammar — named forms parse, everything else does not
# =========================================================================


def test_named_forms_parse_the_pool_and_strip_the_clause() -> None:
    """Every named shape the council admitted (the CONS-003/004 number
    list grammar, ported to the send path): ``#``-marked lists, the
    optional object word, bare TWO-OR-MORE lists, commas + "and",
    politeness tails. Numbers come back DEDUPED and ORDERED; the clause
    tokens are removed and every other token survives in order, case
    preserved (the recipient is the user's own string)."""
    parse = app._send_pin_clause
    assert parse("send 500000 sats to bc1qxy using #3 #7 #9") == (
        (3, 7, 9), "send 500000 sats to bc1qxy"
    )
    assert parse("send 5000 sats to BC1QAbC using coins #3 and #4") == (
        (3, 4), "send 5000 sats to BC1QAbC"
    )
    assert parse("send 5000 sats to bc1qxy using utxos 3 7 9") == (
        (3, 7, 9), "send 5000 sats to bc1qxy"
    )
    assert parse("send 5000 sats to bc1qxy using 3 and 7") == (
        (3, 7), "send 5000 sats to bc1qxy"
    )
    assert parse("send 5000 sats to bc1qxy using #3, #7 and #9 please") == (
        (3, 7, 9), "send 5000 sats to bc1qxy"
    )
    assert parse("send 5000 sats to bc1qxy using #3 and #3") == (
        (3,), "send 5000 sats to bc1qxy"
    )
    # the clause at the head of the line is this grammar too
    assert parse("using #3 and #4 send 5000 sats to bc1qxy") == (
        (3, 4), "send 5000 sats to bc1qxy"
    )


def test_deictic_and_non_number_forms_release() -> None:
    """The council's REJECTED shapes stay released (the engine never
    resolves client-side selection state): deictic "the selected" /
    "these", a SINGLE bare digit (CONS-004's pinned model route), "with
    #N" (only "using" is this grammar), no numbers at all, and a
    pathological digit token neither picks nor crashes (CHAT-009)."""
    parse = app._send_pin_clause
    assert parse("send 5000 sats to bc1qxy using the selected UTXOs") is None
    assert parse("send 5000 sats to bc1qxy using these UTXOs") is None
    assert parse("send 5000 sats to bc1qxy with these selected UTXOs") is None
    assert parse("send 5000 sats to bc1qxy with #3 #7") is None
    assert parse("send 5000 sats to bc1qxy using 3") is None  # single bare digit
    assert parse("send 5000 sats to bc1qxy using my lucky numbers") is None
    assert parse("send 5000 sats to bc1qxy") is None
    assert parse("am I using the right address") is None
    assert parse("send 5000 sats to bc1qxy using " + "9" * 5000 + " #3") is None


def test_send_amount_read_is_all_or_nothing() -> None:
    """The send itself is read deterministically or NOT AT ALL: exactly
    one amount WEARING AN EXPLICIT UNIT (a bare number is never an
    amount here; the size grammar's coin-family alias stays OUT — "send
    1 coin" is ambiguous and refuses) and exactly one bc1 recipient."""
    read = app._parse_pin_send
    addr = "bc1q9t3d9jq3lxwsjkgn3dzx3xd4lqpuwuglqh2adq"
    assert read(f"send 500000 sats to {addr}") == (addr, 500_000)
    assert read(f"Send 500,000 sats to {addr}") == (addr, 500_000)
    assert read(f"pay 0.01 bitcoin to {addr}") == (addr, 1_000_000)
    assert read(f"send 5000 to {addr}") is None  # a bare number is not an amount
    assert read(f"send 1 coin to {addr}") is None  # the ambiguous alias stays out
    assert read(f"send $50 to {addr}") is None  # fiat: the model path's shape
    assert read(f"send 5000 sats and 6000 sats to {addr}") is None  # two amounts
    assert read(f"send 5000 sats to {addr} and bc1qxyzabcdefghij") is None  # two recipients
    assert read("send 5000 sats") is None  # no recipient


# =========================================================================
# 2. Named forms consumed PRE-MODEL; deictic forms RELEASED (pinned)
# =========================================================================


def test_named_form_stages_pinned_and_the_model_never_runs(world) -> None:
    """THE user feature, end to end: "send 50000 sats to <addr> using
    #3 and #4" spends from EXACTLY the named coins, the model never sees
    the turn, and the dispatched envelope carries NO coin reference (the
    pinned CONS-003 rule — recipient/amount (+fee knob) only)."""
    _pin_coins(world)
    seen = _spy_create(world)
    outs, fake = _turn(world, f"send 50000 sats to {SEND_RECIPIENT} using #3 and #4")
    assert fake.prompts == []
    assert world["flow"].state is TxFlowStatus.CREATED
    assert world["flow"].pending is not None
    assert world["flow"].pending.inputs_count == 2
    assert seen[-1].params.model_dump(exclude_none=True) == {
        "recipient": SEND_RECIPIENT, "amount_sats": 50_000,
    }
    assert world["session"].send_pinned is None  # consumed at the handler head
    marker = world["session"].send_pinned_pending
    assert marker is not None and marker.numbers == (3, 4)
    assert any(o.startswith("From: the coins you named (2 sources") for o in outs)


def test_bare_number_list_form_consumed(world) -> None:
    """The CONS-004-consistent bare form ("using 3 4") is this grammar
    too — a two-or-more bare list after "using" IS a registry pick."""
    _pin_coins(world)
    _turn(world, f"send 50000 sats to {SEND_RECIPIENT} using 3 4")
    assert world["flow"].state is TxFlowStatus.CREATED
    assert world["session"].send_pinned_pending.numbers == (3, 4)


def test_single_hash_form_consumed_single_bare_released(world) -> None:
    """A lone ``#``-marked digit IS a pick ("using #4"); a lone BARE
    digit is not (CONS-004's pinned model route) — released UNCHANGED,
    digits and all, with nothing stamped or staged."""
    _pin_coins(world)
    _turn(world, f"pay 20000 sats to {SEND_RECIPIENT} using #4")
    assert world["flow"].state is TxFlowStatus.CREATED
    assert world["session"].send_pinned_pending.numbers == (4,)
    world["flow"].cancel()
    gen = _PlanGen()
    _turn(world, f"pay 20000 sats to {SEND_RECIPIENT} using 4", gen)
    assert len(gen.prompts) == 1
    assert "using 4" in gen.prompts[0]  # the release is WHOLE, not stripped
    assert world["session"].send_pinned is None
    assert world["flow"].state is TxFlowStatus.CANCELLED


def test_deictic_forms_fall_through_to_the_model(world) -> None:
    """Pinned council contract: deictic forms are NOT intercepted — the
    FULL line (deictic tail included) reaches the model, which owns the
    clarify; the engine holds no client-selection state to resolve, and
    nothing is staged deterministically."""
    _pin_coins(world)
    for line in (
        f"send 50000 sats to {SEND_RECIPIENT} using the selected UTXOs",
        f"send 50000 sats to {SEND_RECIPIENT} using these UTXOs",
        f"send 50000 sats to {SEND_RECIPIENT} with these selected UTXOs",
    ):
        gen = _PlanGen()
        _turn(world, line, gen)
        assert len(gen.prompts) == 1, line
        assert line.lower() in gen.prompts[0].lower(), line  # the WHOLE line, tail included
        assert world["session"].send_pinned is None
        assert world["flow"].state is not TxFlowStatus.CREATED


def test_fee_rung_survives_and_deny_releases(world) -> None:
    """RBF-004's MAJOR lesson applied: a stated urgency rides the
    envelope's own knob ("no hurry" → slow; its deny-shaped words are an
    URGENCY here, never a suppression); a real deny SUPPRESSES the
    intercept whole (HW-005 slice-C rule) — the ordinary path handles
    the refusal and nothing stages."""
    _pin_coins(world)
    seen = _spy_create(world)
    _turn(world, f"send 50000 sats to {SEND_RECIPIENT} using #3 and #4, no hurry")
    assert seen[-1].params.model_dump(exclude_none=True) == {
        "recipient": SEND_RECIPIENT, "amount_sats": 50_000, "fee_target": "slow",
    }
    world["flow"].cancel()
    gen = _PlanGen()
    _turn(world, f"don't send 50000 sats to {SEND_RECIPIENT} using #3 and #4", gen)
    assert len(gen.prompts) == 1
    assert world["flow"].state is not TxFlowStatus.CREATED


def test_question_about_a_pinned_send_refuses_not_stages(world) -> None:
    """A QUESTION naming coins can neither stage money nor be released
    (the model would read the send and silently DROP the coin reference
    — the substitution this feature forbids): one honest line, consumed,
    nothing staged, no model."""
    _pin_coins(world)
    outs, fake = _turn(
        world, f"can I send 50000 sats to {SEND_RECIPIENT} using #3 and #4?"
    )
    assert outs == [app._PINNED_UNREADABLE]
    assert fake.prompts == []
    assert world["flow"].state is not TxFlowStatus.CREATED


def test_gate_territory_stands_down(world) -> None:
    """While a transaction pends the card owns the turn (the
    consolidation opener's rule, unchanged): a pinned-shape line is NOT
    consumed here — it falls through to the ordinary gated pipeline, and
    the staged record is untouched."""
    _pin_coins(world)
    _turn(world, f"send 50000 sats to {SEND_RECIPIENT} using #3 and #4")
    ref = world["flow"].pending.tx_ref  # type: ignore[union-attr]
    gen = _PlanGen()
    _turn(world, f"send 30000 sats to {SEND_RECIPIENT} using #3 and #4", gen)
    assert len(gen.prompts) == 1  # the ordinary (pending-guarded) pipeline
    assert world["flow"].pending.tx_ref == ref


# =========================================================================
# 3. Fresh-store re-resolution at HANDLER time
# =========================================================================


def test_gone_coin_hard_stops_naming_the_numbers(world) -> None:
    """The mid-conversation-recheck discipline, pinned: the coin at #4
    was spent elsewhere since the listing → the handler's FRESH read
    hard-stops NAMING #4, nothing stages, the flow stays IDLE, the model
    never runs, and the survivor (#3) is never sent alone — never a
    silent substitute, never a survivors-only re-quote."""
    _pin_coins(world)
    rows = [
        r for r in world["store"].get_utxos_for_wallet(world["wallet"].id)
        if r.address != world["addrs"][3]
    ]
    world["store"].replace_utxos_for_wallet(world["wallet"].id, rows)
    outs, fake = _turn(world, f"send 50000 sats to {SEND_RECIPIENT} using #3 and #4")
    assert outs == [app._PINNED_COINS_GONE.format(numbers="#4")]
    assert fake.prompts == []
    assert world["flow"].state is TxFlowStatus.IDLE
    assert world["flow"].pending is None
    assert world["session"].send_pinned is None
    assert world["session"].send_pinned_pending is None


def test_unknown_number_rides_the_existing_clarify(world) -> None:
    """A number the registry does not know = the EXISTING value-free
    ADDRESS_REF_UNKNOWN clarify (the ``_cons_by_numbers`` route),
    consumed BEFORE any dispatch: nothing staged, no model, no nearest
    guess, the handler never even runs."""
    _pin_coins(world)
    seen = _spy_create(world)
    outs, fake = _turn(world, f"send 5000 sats to {SEND_RECIPIENT} using #3 and #99")
    assert outs == [app.ADDRESS_REF_UNKNOWN]
    assert fake.prompts == []
    assert seen == []
    assert world["flow"].state is not TxFlowStatus.CREATED
    assert world["session"].send_pinned is None


def test_unparsable_send_refuses_value_free(world) -> None:
    """Coins named for a send whose amount/recipient the engine cannot
    FULLY read (fiat shape; a recipient that fails the same validation
    layers model output passes): the honest consume — releasing would
    hand the model a coin reference no envelope can carry, and
    half-parsing must never stage anything. No wallet value echoes."""
    _pin_coins(world)
    outs, fake = _turn(world, f"send $50 to {SEND_RECIPIENT} using #3 and #4")
    assert outs == [app._PINNED_UNREADABLE]
    assert fake.prompts == []
    assert world["flow"].state is not TxFlowStatus.CREATED
    outs, _ = _turn(world, "send 5000 sats to bc1q-not-a-real-address using #3 #4")
    assert outs == [app._PINNED_REFUSED]
    assert world["flow"].state is not TxFlowStatus.CREATED


def test_address_granularity_falls_out_to_consolidate_and_spend(world) -> None:
    """The council's "consolidate + spend in one FALLS OUT of pinning
    the pool" leg: the number is the ADDRESS; two coins sit there and
    "using #3" pins BOTH (no separate combined verb exists anywhere) —
    both enumerate on the card under the same handle."""
    _pin_coins(world)
    _add_coin(world["store"], world["wallet"].id, 2, "e" * 64, 25_000)  # 2nd coin, addr #3
    outs, fake = _turn(world, f"send 54000 sats to {SEND_RECIPIENT} using #3")
    assert fake.prompts == []
    pend = world["flow"].pending
    assert pend is not None and pend.inputs_count == 2
    rows = [o for o in outs if o.startswith("  #3. ")]
    assert len(rows) == 2 and all(world["addrs"][2] in r for r in rows)


# =========================================================================
# 4. The card: every final input enumerated; every deviation narrated
# =========================================================================


def test_card_enumerates_every_final_input(world) -> None:
    """The council's precondition: the From line enumerates EVERY final
    input as ``#N · <full address> · <sats>`` (the ``_CONS_SOURCE_ROW``
    shape, values verbatim from the handler's fresh read) — on the brief
    card AND in the cached ``/details`` full render."""
    _pin_coins(world)
    outs, _ = _turn(world, f"send 50000 sats to {SEND_RECIPIENT} using #3 and #4")
    addr3, addr4 = world["addrs"][2], world["addrs"][3]
    assert f"  #3. {addr3} · 30,000 sats" in outs
    assert f"  #4. {addr4} · 40,000 sats" in outs
    cached = world["session"].card_render
    assert cached is not None
    assert app._SEND_PIN_HEADER in cached
    assert f"  #3. {addr3} · 30,000 sats" in cached


def test_skipped_dust_is_enumerated_on_the_card(world) -> None:
    """Policy unchanged, deviation narrated: a named coin worth less
    than its own input fee is skipped by the (untouched) dust step and
    shows under its own header — the user confirms the ENUMERATION, the
    drop is never silent."""
    _show_all(world, 8)
    _add_coin(world["store"], world["wallet"].id, 2, "a" * 64, 100)  # #3: below input fee
    _add_coin(world["store"], world["wallet"].id, 3, "b" * 64, 60_000)  # #4 funds alone
    outs, _ = _turn(world, f"send 50000 sats to {SEND_RECIPIENT} using #3 and #4")
    pend = world["flow"].pending
    assert pend is not None and pend.inputs_count == 1
    assert app._SEND_NOT_SPENT_HEADER in outs
    assert f"  #3. {world['addrs'][2]} · 100 sats" in outs  # the skipped dust, named
    assert f"  #4. {world['addrs'][3]} · 60,000 sats" in outs  # the spent coin, named


def test_subset_preference_enumerates_the_leftovers(world) -> None:
    """The single-coin/min-cost preference may pick a SUBSET of the
    named pool — the policy runs unchanged; the named coin it did not
    spend is enumerated under its own header."""
    _pin_coins(world)  # #3 = 30k, #4 = 40k
    outs, _ = _turn(world, f"send 20000 sats to {SEND_RECIPIENT} using #3 and #4")
    pend = world["flow"].pending
    assert pend is not None and pend.inputs_count == 1  # #3 alone funds it
    assert f"  #3. {world['addrs'][2]} · 30,000 sats" in outs  # spent
    assert app._SEND_NOT_SPENT_HEADER in outs
    assert f"  #4. {world['addrs'][3]} · 40,000 sats" in outs  # named, NOT spent


def test_fold_within_the_pool_visible_on_the_card(world) -> None:
    """Step 5 runs UNCHANGED inside the pinned pool: at a low fee the
    small named coins fold into the final set "to save fees later" —
    every folded coin is a FINAL input (enumerated) and the From line
    carries the existing UTXO-004 fold clause."""
    store = world["store"]
    store.set_coin_setting(UTXO_TARGET_MIN_SETTING, "500000")
    store.set_coin_setting(UTXO_TARGET_MAX_SETTING, "5000000")
    store.set_coin_setting(CONSOLIDATE_BELOW_SAT_VB_SETTING, "3")
    _show_all(world, 8)
    _add_coin(store, world["wallet"].id, 2, "a" * 64, 2_000)  # #3
    _add_coin(store, world["wallet"].id, 3, "b" * 64, 2_000)  # #4
    _add_coin(store, world["wallet"].id, 4, "c" * 64, 60_000)  # #5 funds alone
    outs, _ = _turn(world, f"send 50000 sats to {SEND_RECIPIENT} using #3 #4 and #5")
    pend = world["flow"].pending
    assert pend is not None and pend.inputs_count == 3  # the fold pulled both in
    assert any("folding in 2 small ones" in o for o in outs)
    assert f"  #3. {world['addrs'][2]} · 2,000 sats" in outs
    assert f"  #4. {world['addrs'][3]} · 2,000 sats" in outs
    assert app._SEND_NOT_SPENT_HEADER not in outs  # nothing named was left unspent


def test_change_folded_into_fee_stays_honest(world) -> None:
    """Case B inside the pool (a residue below change-dust folds into
    the FEE): the card claims no change, the Fee row carries the residue
    verbatim, and the single final input is enumerated — a deviation the
    user READS, never one that hides."""
    _show_all(world, 8)
    _add_coin(world["store"], world["wallet"].id, 2, "a" * 64, 50_000)  # #3
    outs, _ = _turn(world, f"send 49700 sats to {SEND_RECIPIENT} using #3")
    pend = world["flow"].pending
    assert pend is not None and pend.change_sats is None
    assert not any("come back as change" in o for o in outs)
    assert f"  #3. {world['addrs'][2]} · 50,000 sats" in outs


def test_mixed_pool_allowed_and_narrated(world) -> None:
    """The council's ONE rule, stated on the card (consolidation's
    ``_CONS_POOLS_APART`` refusal is a DIFFERENT flow, NOT imported): a
    named set spanning the KYC mark that no pure side can fund sends as
    a MIX — the existing UTXO-002 mix warning renders above the
    enumeration, and every final input still shows."""
    _show_all(world, 8)
    world["store"].add_address_labels(world["addrs"][1], ["kyc"])
    _add_coin(world["store"], world["wallet"].id, 1, "c" * 64, 20_000)  # #2, kyc-side
    outs, fake = _turn(
        world, f"send 110000 sats to {SEND_RECIPIENT} using #1 and #2"
    )
    assert fake.prompts == []
    pend = world["flow"].pending
    assert pend is not None and pend.inputs_count == 2
    assert app._CARD_MIX_WARNING in outs
    assert f"  #1. {world['addrs'][0]} · 100,000 sats" in outs
    assert f"  #2. {world['addrs'][1]} · 20,000 sats" in outs


def test_pure_pool_preference_wins_within_the_named_set(world) -> None:
    """Layer A unchanged INSIDE the pool: when one pure side of the
    named mix funds, the other side is NOT spent (its coin enumerates as
    not-spent) and the mix warning stays DOWN — the preference is the
    existing policy, the enumeration is the honesty."""
    _show_all(world, 8)
    world["store"].add_address_labels(world["addrs"][1], ["kyc"])
    _add_coin(world["store"], world["wallet"].id, 1, "c" * 64, 20_000)  # #2 kyc
    _add_coin(world["store"], world["wallet"].id, 2, "a" * 64, 30_000)  # #3 other
    outs, _ = _turn(world, f"send 25000 sats to {SEND_RECIPIENT} using #2 and #3")
    assert app._CARD_MIX_WARNING not in outs
    assert f"  #2. {world['addrs'][1]} · 20,000 sats" in outs  # named, NOT spent
    assert app._SEND_NOT_SPENT_HEADER in outs
    assert f"  #3. {world['addrs'][2]} · 30,000 sats" in outs  # the pure winner


# =========================================================================
# 5. Insufficient within the pool; the ceiling; re-quotes; the ride
# =========================================================================


def test_insufficient_scoped_to_the_named_pool(world) -> None:
    """The existing InsufficientFundsError honesty (needed/available
    figures, ADR-0012), SCOPED to the pinned pool and naming the user's
    numbers — never a wallet-wide figure the named set does not support
    (the untouched 100k seed coin must not appear as "available")."""
    _pin_coins(world)
    outs, fake = _turn(world, f"send 200000 sats to {SEND_RECIPIENT} using #3 #4")
    assert fake.prompts == []
    assert world["flow"].state is TxFlowStatus.IDLE
    line = outs[0]
    assert "#3 and #4" in line and "can't cover this" in line
    assert "hold 70000 sats" in line  # the POOL's total (30k + 40k)
    assert "170000" not in line  # never the wallet's total (100k seed is NOT offered)


def test_256_input_ceiling_refuses_huge_named_pools(world) -> None:
    """TCK-TX-SELF-001's ceiling applies to pinned sends as a REFUSAL
    shape at BOTH stops — the intercept (naming more numbers than the
    ceiling is certainly more coins) and the handler (fewer numbers
    hiding an over-ceiling pool) — never a truncation."""
    cap = app.MAX_SELF_TRANSFER_CONSOLIDATE_INPUTS
    _show_all(world, cap + 1)
    many = " ".join(f"#{n}" for n in range(1, cap + 2))
    outs, fake = _turn(world, f"send 50000 sats to {SEND_RECIPIENT} using {many}")
    assert outs == [app._PINNED_POOL_TOO_MANY]
    assert fake.prompts == []
    assert world["flow"].state is not TxFlowStatus.CREATED
    # the handler leg: number 3 alone, cap+1 coins sitting at its address
    world["store"].replace_utxos_for_wallet(
        world["wallet"].id,
        [
            UtxoRecord(
                wallet_id=world["wallet"].id,
                txid=f"{i:064x}",
                vout=0,
                address=world["addrs"][2],
                value_sats=60_000,
                confirmed=1,
                height=None,
            )
            for i in range(1, cap + 2)
        ],
    )
    env = validate_payload(
        json.dumps(
            {"v": 0, "intent": "create_tx",
             "params": {"recipient": SEND_RECIPIENT, "amount_sats": 5000}}
        )
    )
    world["session"].send_pinned = app._SendPinned(
        wallet_id=world["wallet"].id, numbers=(3,)
    )
    result = world["table"][IntentName.CREATE_TX](env)
    assert result["error"] == "pinned_pool_too_many"
    assert result["detail"] == app._PINNED_POOL_TOO_MANY
    assert world["flow"].state is not TxFlowStatus.CREATED


def test_requote_of_a_pinned_pending_stays_pinned(world) -> None:
    """Once pinned, ALWAYS pinned: a later model-route re-quote (same
    recipient + amount, new rung) rebuilds INSIDE the named pool — the
    wallet's untouched 100k coin never sneaks in, and the new card
    re-enumerates the final inputs. A rung change may move the fee; the
    COIN SET may not."""
    _pin_coins(world)
    _turn(world, f"send 50000 sats to {SEND_RECIPIENT} using #3 and #4")
    old_ref = world["flow"].pending.tx_ref  # type: ignore[union-attr]
    gen = _PlanGen(
        [json.dumps({"v": 0, "intent": "create_tx", "params": {
            "recipient": SEND_RECIPIENT, "amount_sats": 50_000,
            "fee_target": "slow"}})]
    )
    outs, _ = _turn(world, "slower", gen)
    pend = world["flow"].pending
    assert pend is not None and pend.tx_ref != old_ref  # replaced (FLOW-REQUOTE)
    assert pend.inputs_count == 2  # still exactly #3 + #4 (70k), never the 100k coin
    assert world["session"].send_pinned_pending.tx_ref == pend.tx_ref
    assert f"  #3. {world['addrs'][2]} · 30,000 sats" in outs


def test_requote_stops_on_a_gone_coin_original_card_intact(world) -> None:
    """Commit-only-on-success, pinned side: a pool coin that goes
    missing between staging and a re-quote stops the REBUILD naming it,
    and the ORIGINAL pending survives untouched (no auto-requote on the
    survivors, no reshuffle behind the confirm gate)."""
    _pin_coins(world)
    _turn(world, f"send 50000 sats to {SEND_RECIPIENT} using #3 and #4")
    ref = world["flow"].pending.tx_ref  # type: ignore[union-attr]
    rows = [
        r for r in world["store"].get_utxos_for_wallet(world["wallet"].id)
        if r.address != world["addrs"][3]
    ]
    world["store"].replace_utxos_for_wallet(world["wallet"].id, rows)
    gen = _PlanGen(
        [json.dumps({"v": 0, "intent": "create_tx", "params": {
            "recipient": SEND_RECIPIENT, "amount_sats": 50_000,
            "fee_target": "slow"}})]
    )
    outs, _ = _turn(world, "slower", gen)
    assert outs[-1] == app._PINNED_COINS_GONE.format(numbers="#4")
    assert world["flow"].pending.tx_ref == ref


def test_pending_reshow_renders_the_pinned_card(world) -> None:
    """The re-show IS the card the user was shown (the ``cons_pending``
    display precedent): a DIFFERENT-destination create_tx while the
    pinned send pends re-shows the frozen enumeration — a bare count
    hiding which coins are spent would misrepresent the confirmation."""
    _pin_coins(world)
    _turn(world, f"send 50000 sats to {SEND_RECIPIENT} using #3 and #4")
    gen = _PlanGen(
        [json.dumps({"v": 0, "intent": "create_tx", "params": {
            "recipient": world["addrs"][5], "amount_sats": 1_000}})]
    )
    outs, _ = _turn(world, "send 1000 sats somewhere else", gen)
    assert f"  #3. {world['addrs'][2]} · 30,000 sats" in outs
    assert f"  #4. {world['addrs'][3]} · 40,000 sats" in outs
    assert any(o.startswith("From: the coins you named") for o in outs)


def test_denied_pinned_flow_retires_the_marker(world) -> None:
    """A cancel of the staged pinned send retires the pin marker with
    the flow (the ``cons_pending`` retirement rule — it never leaks onto
    the next one); the deterministic CANCEL-001 short-circuit is
    unchanged."""
    _pin_coins(world)
    _turn(world, f"send 50000 sats to {SEND_RECIPIENT} using #3 and #4")
    outs, fake = _turn(world, "cancel")
    assert world["flow"].state is TxFlowStatus.CANCELLED
    assert world["session"].send_pinned_pending is None
    assert fake.prompts == []
    assert outs == [app._CANCELLED_LINE]


def test_dual_key_gate_unchanged_full_ride(world) -> None:
    """The pinned plan rides the SAME create → confirm → sign → broadcast
    state machine (the existing pins stay green; an LLM "yes" is never
    consulted — the gate decision is dispatcher-owned): the full ride
    broadcasts exactly the pinned inputs."""
    _pin_coins(world)
    _turn(world, f"send 50000 sats to {SEND_RECIPIENT} using #3 and #4")
    _ride(world)
    assert world["flow"].state is TxFlowStatus.BROADCAST


def test_ordinary_send_is_untouched(world) -> None:
    """Byte-identity pin: an ordinary send (no ``using`` clause) keeps
    the WHOLE pre-ticket card — the wallet-generic From line, no
    enumeration rows, no pin marker; the pool logic is unreachable
    without a named clause."""
    _pin_coins(world)
    gen = _PlanGen(
        [json.dumps({"v": 0, "intent": "create_tx", "params": {
            "recipient": SEND_RECIPIENT, "amount_sats": 50_000}})]
    )
    outs, _ = _turn(world, "send 50000 sats to the usual place please", gen)
    assert world["flow"].pending is not None
    assert any(o.startswith("From: your wallet (") for o in outs)
    assert not any(o.startswith("  #") for o in outs)
    assert world["session"].send_pinned_pending is None

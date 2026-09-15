"""TCK-CONS-001 — the app consolidation conversation.

One test per beat of the ticket's done-when list, riding the REAL wiring
(the production dispatch table + mock chain + fake-device HWI signer
reused from the RBF-004/CPFP-002 harnesses — no network, deterministic):

* the label ROLL-UP ask: per-tag count + sats, code-rendered in the
  canonical COIN_TAGS order with the unlabeled group last;
* label pick → the "1 coin or N?" count ask (a single-coin group
  collapses honestly — no fake question);
* the no-label path: ascending UTXO list (amount / label / confirm
  state), selected by the CHAT-001 stable registry NUMBER with the FULL
  address restated at every resolution;
* the plan rides the EXISTING ``self_transfer`` consolidate mode — the
  code-built envelope carries NO coin reference (threshold-only, the
  engine's own max+1), the fee knob persists across every intercept
  (the RBF-004 MAJOR lesson), and the picked coins ride the
  dispatcher-owned ask revalidated against a fresh store read;
* the plan echo ("Merge 4 UTXOs to create one new UTXO of 34,344,393
  sats") rendered verbatim from the staged record's OWN figures;
* post-broadcast label inheritance: the §1.3 union inheritance PLUS the
  closed-set ``consolidation`` tag and the "consolidated from N
  outputs" note, written at broadcast and retired;
* the whole conversation is tag-word-safe: label words are intercepted
  BEFORE the model (openers included), nothing it prints enters a
  prompt, and any unmatched utterance closes the ask (never-trap) —
  a DENY retires the staged plan's marker.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Final

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.protocol import EnvelopeValidationError, IntentName, validate_payload
from localwallet.tx.flow import GateDecision, TxFlowStatus
from tests.test_e2e_skeleton import derive_fixture_addresses
from tests.test_rbf004_bump import _add_coin, _FakeGen
from tests.test_tx_self_transfer import _hwi_table, _utxo

KYC_SMALL_TXID: Final[str] = "a" * 64
KYC_BIG_TXID: Final[str] = "b" * 64
P2P_TXID: Final[str] = "c" * 64
UNLAB_TXID: Final[str] = "e" * 64  # the 7k unlabeled coin (index 7)
UNLAB2_TXID: Final[str] = "f" * 64  # a second coin at the SAME address


# ------------------------------------------------------------------ helpers


def _env(intent: str, params: dict[str, Any]):
    return validate_payload(json.dumps({"v": 0, "intent": intent, "params": params}))


@pytest.fixture()
def world():
    """The CPFP-002 harness world (one confirmed 100_000-sat UNLABELED coin
    at receive index 0), pre-scanned so the lazy first scan stands down and
    the coins planted per test survive exactly as a rescan left them."""
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


def _coin(
    world, txid: str, value: int, *, index: int, tag: str | None = None, confirmed: int = 1
) -> None:
    """Plant one coin (the RBF-004 way a rescan leaves it) + its stored
    label when tagged — and since TCK-LABELS-UNIFY the label lives on the
    coin's ADDRESS (the coin inherits it; every coin at this index/address
    shares the set)."""
    _add_coin(world["store"], world["wallet"].id, index, txid, value, confirmed=confirmed)
    if tag is not None:
        address = derive_fixture_addresses(8)[index]
        world["store"].add_address_labels(address, [tag])


def _labeled(world) -> None:
    """The canonical conversation set: kyc × 2 (12k + 30k), p2p × 1 (45k),
    one unlabeled 7k coin; the seeded 100k coin stays unlabeled."""
    _coin(world, KYC_SMALL_TXID, 12_000, index=2, tag="kyc")
    _coin(world, KYC_BIG_TXID, 30_000, index=3, tag="kyc")
    _coin(world, P2P_TXID, 45_000, index=4, tag="p2p")
    _coin(world, UNLAB_TXID, 7_000, index=7)


def _turn(world, line: str) -> tuple[list[str], _FakeGen, AgentLoop]:
    """One REAL REPL turn through the production intercept chain (the
    answer/opener path consumes the turn; anything else reaches the fake
    model, whose canned ``respond`` is visible in ``fake.prompts``)."""
    fake = _FakeGen()
    loop = AgentLoop(fake, world["table"])
    outputs: list[str] = []
    app._run_turn(
        loop, world["flow"], world["session"], line, outputs.append,
        table=world["table"], store=world["store"],
    )
    return outputs, fake, loop


def _spy(world) -> list[Any]:
    """Wrap the wired self_transfer handler (outermost — records the EXACT
    envelope the code-built dispatch carries)."""
    real = world["table"][IntentName.SELF_TRANSFER]
    seen: list[Any] = []

    def spy(envelope):
        seen.append(envelope)
        return real(envelope)

    world["table"][IntentName.SELF_TRANSFER] = spy
    return seen


def _ride(world) -> str:
    """Full gate ride of the staged flow (dual-key → sign → broadcast, the
    UNCHANGED state machine) → the broadcast txid."""
    table, session, flow = world["table"], world["session"], world["flow"]
    ref = flow.pending.tx_ref  # type: ignore[union-attr]
    session.gate_decision = GateDecision.CONFIRM
    table[IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": ref}))
    table[IntentName.SIGN_TX](_env("sign_tx", {"tx_ref": ref}))
    b = table[IntentName.BROADCAST_TX](_env("broadcast_tx", {"tx_ref": ref}))
    assert b["status"] == "broadcast", b
    return str(b["txid"])


# =========================================================================
# 1. The label roll-up ask — code-rendered per-tag count + sats
# =========================================================================


def test_rollup_shape(world) -> None:
    """Per-tag count + sats, canonical COIN_TAGS order, the unlabeled
    group LAST, totals code-computed from the store rows — and the model
    never sees the opener line."""
    _labeled(world)
    outs, fake, loop = _turn(world, "consolidate my coins")
    assert fake.prompts == [] and loop.history == ()
    ask = world["session"].cons_ask
    assert ask is not None and ask.kind == "rollup"
    assert outs == [
        app._CONS_ROLLUP_HEAD,
        "  1. kyc — 2 coins · 42,000 sats",
        "  2. p2p — 1 coin · 45,000 sats",
        f"  3. {app._CONS_ROLLUP_UNLABELED} — 2 coins · 107,000 sats",
    ]
    # the answer pipeline can close on any other words (never-trap), and
    # THAT line reaches the model like ordinary chat.
    _, fake2, _ = _turn(world, "how do fees work")
    assert world["session"].cons_ask is None
    assert len(fake2.prompts) == 1


def test_rollup_knob_persists_across_every_intercept(world) -> None:
    """RBF-004's MAJOR lesson pinned for this conversation: the stated
    rung survives rollup → count → dispatch — the code-built envelope
    re-quotes it, and carries NO other coin reference."""
    _labeled(world)
    seen = _spy(world)
    _turn(world, "consolidate my coins, no hurry")  # slow rung, rollup opens
    assert world["session"].cons_ask.fee_target == "slow"  # type: ignore[union-attr]
    _turn(world, "kyc")  # tag word intercepted, never the model
    assert world["session"].cons_ask.fee_target == "slow"  # type: ignore[union-attr]
    _turn(world, "all")
    env = seen[-1]
    assert env.params.model_dump() == {
        "mode": "consolidate",
        "below_size_sats": 30_001,  # the engine's own max(picked)+1
        "fee_target": "slow",
    }


def test_opener_is_conservative(world) -> None:
    """A digit (an explicit size threshold) keeps the existing model route
    (the TX-SELF-001 golden phrasings are NOT intercepted); a non-verb line
    with a tag word is ordinary chat; a deny-shaped opener stands down."""
    assert app._consolidation_intent("consolidate all my coins under 100000 sats") is None
    assert app._consolidation_intent("did the exchange confirm?") is None
    assert app._consolidation_intent("don't consolidate my coins") is None
    assert app._consolidation_intent("merge my notes about small coins") == (None, None)
    _labeled(world)
    _, fake, _ = _turn(world, "consolidate all my coins under 100000 sats")
    assert len(fake.prompts) == 1  # reached the model, exactly as today


def test_opener_tag_and_unlabeled_paths(world) -> None:
    """A tag word IN the opener line pre-picks the label (roll-up skipped);
    the single-coin group collapses straight to the plan (no fake
    question); the unlabeled word opens the LIST directly."""
    _labeled(world)
    seen = _spy(world)
    outs, fake, _ = _turn(world, "consolidate my p2p coins")
    assert fake.prompts == []
    assert world["session"].cons_ask is None  # consumed by the dispatch
    assert len(outs) >= 2 and "Merge 1 UTXO to create" in outs[1]
    assert seen[-1].params.below_size_sats == 45_001
    # the unlabeled word opens the LIST directly — once the pending card's
    # gate territory is cleared (the opener stands down behind it).
    _turn(world, "cancel")
    outs2, *_ = _turn(world, "sweep my unlabeled coins")
    ask = world["session"].cons_ask
    assert ask is not None and ask.kind == "list"
    assert outs2[0] == app._CONS_LIST_HEAD


# =========================================================================
# 2. Label pick → the "1 coin or N?" count ask
# =========================================================================


def test_label_pick_ask(world) -> None:
    """Roll-up answered by the tag WORD (intercepted in code) or the row
    number → the same count ask; the group's total is code-computed."""
    _labeled(world)
    _turn(world, "consolidate my coins")
    outs, fake, _ = _turn(world, "kyc")
    assert fake.prompts == []
    assert world["session"].cons_ask.kind == "count"  # type: ignore[union-attr]
    assert outs == [
        (
            "You marked 2 coins 'kyc' — 42,000 sats together. "
            "Consolidate all 2 into one new coin, or just 1 of them? Say "
            "'all', '2' or '1'; any other words set this aside."
        )
    ]
    _turn(world, "never mind")  # closes (deny-suppressed → falls through)
    _turn(world, "consolidate my coins")  # roll-up open again
    outs2, fake2, _ = _turn(world, "2")  # row 2 = the p2p GROUP…
    if world["session"].cons_ask is None:
        # …a single-coin group collapses straight to the plan (no fake
        # question — the CPFP-002 honest-collapse rule).
        assert fake2.prompts == []
        assert world["flow"].state is TxFlowStatus.CREATED
        _turn(world, "cancel")
        _turn(world, "consolidate my coins")
        outs2, *_ = _turn(world, "3")  # row 3 = the unlabeled group (2 coins)
    assert world["session"].cons_ask.kind == "list"
    assert outs2[0] == app._CONS_LIST_HEAD
    # and answering the roll-up with the multi-coin tag row gave the count
    _turn(world, "what is the height")  # close
    _turn(world, "consolidate my coins")
    outs3, *_ = _turn(world, "1")  # row 1 = kyc (2 coins) → the count ask
    assert "You marked 2 coins" in outs3[0]
    assert world["session"].cons_ask.kind == "count"


def test_count_answers(world) -> None:
    """'all' / the literal N merge the group; '1' opens the list; 'yes' is
    an answer HERE (no gate is armed — the card still gates later)."""
    _labeled(world)
    _turn(world, "consolidate my kyc coins")
    outs, *_ = _turn(world, "1")
    assert outs[0] == app._CONS_LIST_HEAD
    assert world["session"].cons_ask.kind == "list"  # type: ignore[union-attr]
    world["session"].cons_ask = app._ConsAsk(  # type: ignore[assignment]
        kind="count",
        entries=(
            {"txid": KYC_SMALL_TXID, "vout": 0, "value_sats": 12_000, "address": "x", "confirmed": 1, "tags": ("kyc",), "label": None},
            {"txid": KYC_BIG_TXID, "vout": 0, "value_sats": 30_000, "address": "y", "confirmed": 1, "tags": ("kyc",), "label": None},
        ),
    )
    seen = _spy(world)
    _turn(world, "yes")
    assert seen[-1].params.model_dump() == {"mode": "consolidate", "below_size_sats": 30_001}


# =========================================================================
# 3. The ascending list + selection by the CHAT-001 registry number
# =========================================================================


def test_list_ordering_and_number_selection(world) -> None:
    """Rows ASCENDING by value carrying amount / label / confirm-state and
    the STABLE registry number (first showing registers it); the number
    pick restates the FULL address (glm#7) and picks EVERY coin at that
    address — not a row position."""
    _labeled(world)
    _coin(world, UNLAB2_TXID, 9_000, index=7)  # a SECOND coin at address #7
    addrs = derive_fixture_addresses(8)
    outs, *_ = _turn(world, "consolidate my unlabeled coins")
    ask = world["session"].cons_ask
    assert ask is not None and ask.kind == "list"
    # rows print ASCENDING by value; the number is the address's STABLE
    # registry number (first showing registers it), shared by both coins
    # at one address — never a row position.
    assert outs[1:] == [
        "  #1. 7,000 sats · no label · confirmed",
        "  #1. 9,000 sats · no label · confirmed",
        "  #2. 100,000 sats · no label · confirmed",
    ]
    numbers = {r.address: r.number for r in world["store"].list_address_registry(world["wallet"].id)}
    assert numbers[addrs[7]] == 1  # BOTH 7k/9k coins share address #1's number
    seen = _spy(world)
    outs2, fake, _ = _turn(world, "1")
    assert fake.prompts == []
    # every resolution restates the FULL address, verbatim; both coins go.
    assert f"Coin #1 at {addrs[7]} — 7,000 sats." in outs2
    assert f"Coin #1 at {addrs[7]} — 9,000 sats." in outs2
    assert seen[-1].params.below_size_sats == 9_001
    assert world["flow"].state is TxFlowStatus.CREATED


def test_list_shows_labels_and_unconfirmed_state(world) -> None:
    _labeled(world)
    _coin(world, "6" * 64, 4_000, index=6, tag="kyc", confirmed=0)
    _turn(world, "consolidate my kyc coins")  # 3 coins → count ask
    outs, *_ = _turn(world, "1")
    body = "\n".join(outs[1:])
    assert "4,000 sats · 'kyc' · unconfirmed" in body
    assert "12,000 sats · 'kyc' · confirmed" in body
    assert outs[1:].index(  # ascending: the 4k unconfirmed row is FIRST
        "  #1. 4,000 sats · 'kyc' · unconfirmed"
    ) == 0


# =========================================================================
# 4. The plan echo — engine totals, verbatim from tool output
# =========================================================================


def test_plan_echo_render() -> None:
    """The pinned shape: totals are the staged RECORD's own figures."""
    lines: list[str] = []
    app._print_self_plan(
        {
            "inputs_count": 4,
            "amount_sats": 34_344_393,
            "self_parts": 1,
            "self_each_sats": 34_344_393,
            "self_inputs_total_sats": 34_360_000,
            "fee_sats": 15_607,
            "cons_merge": True,
            "self_mode": "consolidate",
        },
        lines.append,
    )
    assert "Merge 4 UTXOs to create one new UTXO of 34,344,393 sats" in lines


def test_plan_echo_from_the_record(world) -> None:
    """The live card renders the SAME line from the handler result's own
    inputs_count/amount_sats — the printed echo equals the staged
    record's figures verbatim (never the ask's remembered totals)."""
    _labeled(world)
    _turn(world, "consolidate my kyc coins")
    outs, *_ = _turn(world, "all")
    assert world["flow"].state is TxFlowStatus.CREATED
    pending = world["flow"].pending
    assert pending is not None
    # the group's 42,000 sats minus the engine's fee: the echo quotes the
    # RECORD's post-fee amount, not the inputs total.
    assert 0 < pending.amount_sats < 42_000
    assert f"Merge 2 UTXOs to create one new UTXO of {pending.amount_sats:,} sats" in outs


def test_direct_threshold_plan_keeps_the_generic_card(world) -> None:
    """The model-routed direct consolidate (no conversation) is
    byte-unchanged: the generic plan line, NO echo, NO broadcast
    annotation marker (the new lines belong to the conversation)."""
    _labeled(world)
    result = world["table"][IntentName.SELF_TRANSFER](
        _env("self_transfer", {"mode": "consolidate", "below_size_sats": 50_000})
    )
    assert result["self_mode"] == "consolidate"
    assert "cons_merge" not in result
    assert world["session"].cons_pending is None
    lines: list[str] = []
    app._print_self_plan(result, lines.append)
    assert not any("create one new UTXO" in ln for ln in lines)
    assert any(ln.startswith("Plan: merge ") for ln in lines)


# =========================================================================
# 5. Post-broadcast label inheritance (common tag + consolidation + record)
# =========================================================================


def test_broadcast_inherits_common_tag_plus_consolidation(world) -> None:
    """§1.3 union inheritance runs as always — now onto the NEW coin's
    ADDRESS set — and the conversation's staged plan adds the closed-set
    ``consolidation`` tag + the "consolidated from N outputs" member,
    written ONLY at broadcast (the session carries the broadcast's own
    addresses: that is what makes the fresh set labelable pre-rescan)."""
    _labeled(world)
    _turn(world, "consolidate my kyc coins")
    _turn(world, "all")
    _ride(world)
    assert len(world["session"].last_broadcast_addresses) == 1
    new_addr = world["session"].last_broadcast_addresses[0]
    members = world["store"].get_address_label_set(new_addr)
    assert members == ("kyc", "consolidation", "consolidated from 2 outputs")
    assert world["session"].cons_pending is None  # retired at broadcast
    # the consumed inputs keep their own address sets (address-keyed, spent-safe)
    addrs = derive_fixture_addresses(8)
    assert world["store"].get_address_label_set(addrs[3]) == ("kyc",)


def test_broadcast_inherits_union_and_adds_record(world) -> None:
    """Mixed groups inherit the UNION of every input tag (a kyc+p2p address
    lands on both sides — the fail-safe §1.3 rule) + the record."""
    _coin(world, KYC_SMALL_TXID, 12_000, index=2)
    _coin(world, KYC_BIG_TXID, 30_000, index=3)
    world["store"].add_address_labels(derive_fixture_addresses(8)[2], ["kyc", "p2p"])
    world["store"].add_address_labels(derive_fixture_addresses(8)[3], ["p2p"])
    _turn(world, "consolidate my p2p coins")
    _turn(world, "all")
    _ride(world)
    new_addr = world["session"].last_broadcast_addresses[0]
    members = world["store"].get_address_label_set(new_addr)
    assert members == ("kyc", "p2p", "consolidation", "consolidated from 2 outputs")


def test_broadcast_record_unlabeled_single_output_note(world) -> None:
    """Unlabeled inputs inherit NOTHING (no closed-tag union — the §1.3
    rule), yet the consolidation record is still written; N=1 reads
    'output'."""
    _labeled(world)
    _turn(world, "sweep my unlabeled coins")  # list ask (2+ coins)
    # pick the 7k coin by its registry number
    number = next(
        r.number
        for r in world["store"].list_address_registry(world["wallet"].id)
        if r.address == derive_fixture_addresses(8)[7]
    )
    _turn(world, str(number))
    _ride(world)
    new_addr = world["session"].last_broadcast_addresses[0]
    members = world["store"].get_address_label_set(new_addr)
    assert members == ("consolidation", "consolidated from 1 output")


# =========================================================================
# 6. Never-trap / derailment / retirement
# =========================================================================


@pytest.mark.parametrize(
    "open_line",
    [
        "consolidate my coins",  # rollup ask
        "consolidate my kyc coins",  # count ask
        "sweep my unlabeled coins",  # list ask
    ],
)
def test_any_utterance_closes_the_ask(world, open_line: str) -> None:
    """No dead ends from ANY ask state: one unmatched utterance closes the
    ask and the line rides the ordinary pipeline to the model."""
    _labeled(world)
    _turn(world, open_line)
    assert world["session"].cons_ask is not None  # an ask stands open
    _, fake, _ = _turn(world, "what is the block height")
    assert world["session"].cons_ask is None  # cleared, never traps
    assert len(fake.prompts) == 1  # and the conversation let the line go


def test_deny_words_suppress_answers(world) -> None:
    """A deny token never answers an ask (and thereby CLOSES it) — 'not
    the largest' shape; the flow-cancel path never sees a hijacked yes."""
    _labeled(world)
    _turn(world, "consolidate my kyc coins")
    _, fake, _ = _turn(world, "no")
    assert world["session"].cons_ask is None
    assert len(fake.prompts) == 1  # "no" fell through as chat (no gate armed)
    assert world["flow"].state is TxFlowStatus.IDLE


def test_deny_mid_flow_retires_the_marker(world) -> None:
    """A DENY while the consolidation plan pends cancels the flow AND
    retires the broadcast-annotation marker (nothing leaks to a later
    flow; no annotation can ever fire for a cancelled plan)."""
    _labeled(world)
    _turn(world, "consolidate my kyc coins")
    _turn(world, "all")
    assert world["session"].cons_pending is not None
    _turn(world, "cancel")
    assert world["flow"].state is not TxFlowStatus.CREATED
    assert world["session"].cons_pending is None
    assert world["session"].cons_ask is None


def test_coin_gone_mid_conversation(world) -> None:
    """The picked coins are revalidated against a FRESH store read at the
    answer turn: a coin that vanished while the ask stood open is the
    honest value-free answer — nothing is staged, nothing is swapped."""
    _labeled(world)
    _turn(world, "consolidate my kyc coins")
    rows = [
        r
        for r in world["store"].get_utxos_for_wallet(world["wallet"].id)
        if r.txid != KYC_BIG_TXID
    ]
    world["store"].replace_utxos_for_wallet(world["wallet"].id, rows)  # coin spent elsewhere
    outs, fake, _ = _turn(world, "all")
    assert fake.prompts == []  # the answer was consumed by the conversation
    assert outs == [app._CONS_COIN_GONE]
    assert world["flow"].state is TxFlowStatus.IDLE
    assert world["session"].cons_ask is None
    assert world["session"].cons_pending is None


def test_fresh_envelope_supersedes_open_ask(world) -> None:
    """A NEW consolidate envelope (however it arrived) consumes an open
    ask WITHOUT the conversation path — the threshold policy plans, byte
    like today, and no dispatcher state leaks into it."""
    _labeled(world)
    _turn(world, "consolidate my kyc coins")  # count ask open
    result = world["table"][IntentName.SELF_TRANSFER](
        _env("self_transfer", {"mode": "consolidate", "below_size_sats": 50_000})
    )
    assert world["session"].cons_ask is None  # superseded, not hijacked
    assert result["self_mode"] == "consolidate"
    assert "cons_merge" not in result


def test_opener_stands_down_while_a_card_pends(world) -> None:
    """The pending card owns the gate territory: a consolidation-shaped
    line while a flow is CREATED is NOT intercepted (it reaches the
    ordinary pipeline; the handler's own pending guard answers)."""
    _labeled(world)
    _turn(world, "consolidate my kyc coins")
    _turn(world, "all")
    assert world["flow"].state is TxFlowStatus.CREATED
    _, fake, _ = _turn(world, "consolidate my p2p coins")
    assert len(fake.prompts) == 1
    assert world["session"].cons_ask is None


def test_pre_first_scan_opener_falls_through() -> None:
    """An unscanned wallet never sees a roll-up over an empty cache (the
    honest model route with the lazy scan owns that state): the opener
    checks the scan cursor and stands down."""
    addrs = derive_fixture_addresses(8)
    state: dict[str, Any] = {}
    # NO pre-scan dispatch this time — the cursor is genuinely absent.
    table, store, wallet, client, _recorded, flow, session, _signer = _hwi_table(
        {addrs[0]: [_utxo("d" * 64, 0, 100_000)]}, state=state
    )
    try:
        assert store.get_sync_state(wallet.id, app.wallet_scan.CURSOR_KEY) is None
        fake = _FakeGen()
        loop = AgentLoop(fake, table)
        app._run_turn(
            loop, flow, session, "consolidate my coins", [].append,
            table=table, store=store,
        )
        assert session.cons_ask is None
        assert len(fake.prompts) == 1  # ordinary pipeline, lazy scan intact
    finally:
        store.close()
        client.close()


# =========================================================================
# 7. The invariants this conversation must never break
# =========================================================================


def test_label_words_never_reach_the_model(world) -> None:
    """The whole conversation runs transcript-free: stored labels/notes
    print verbatim to the terminal, the opener and every answer match them
    IN CODE, and no prompt or transcript entry ever carries them — even
    the derailment turn's prompt is clean (§7.10, both directions)."""
    _labeled(world)
    # The p2p address gains its tag (via _labeled) PLUS a free-text member —
    # the list ask prints both verbatim, the model sees neither.
    world["store"].add_address_labels(derive_fixture_addresses(8)[4], ["fridge magnet"])
    fake = _FakeGen()
    loop = AgentLoop(fake, world["table"])
    for line in (
        "consolidate my coins",  # roll-up opener (tag words listed)
        "kyc",  # tag-word answer → count ask
        "1",  # → the list (labels print-only)
        "1",  # registry pick → plan
    ):
        app._run_turn(
            loop, world["flow"], world["session"], line, [].append,
            table=world["table"], store=world["store"],
        )
    assert world["flow"].state is TxFlowStatus.CREATED  # the plan is real
    assert loop.history == () and fake.prompts == []  # never touched the model
    _, fake2, loop2 = _turn(world, "how are you")  # a later ordinary turn
    assert all("fridge magnet" not in p and "'kyc'" not in p for p in fake2.prompts)
    assert loop2.history != ()  # (chat itself still records, as always)


def test_ask_answer_cannot_be_a_model_invention(world) -> None:
    """The consolidate params are CLOSED: no outpoint, address or index
    key is representable — the picked set rides ONLY the session record
    (a model-emitted envelope can never target coins the engine didn't
    stamp; the red-team structural case)."""
    _labeled(world)
    with pytest.raises(EnvelopeValidationError):
        _env(
            "self_transfer",
            {"mode": "consolidate", "below_size_sats": 50_000, "outpoints": ["a" * 64 + ":0"]},
        )


def test_amounts_never_hardcoded_plan_follows_records(world) -> None:
    """The echo quotes the RECORD: force the handler to plan something
    SMALLER than the picked group's total (the fee eats the rest) and the
    card line follows the record, not the ask's remembered figures."""
    _labeled(world)
    _turn(world, "consolidate my kyc coins")
    _turn(world, "all")
    pending = world["flow"].pending
    assert pending is not None
    # 12k + 30k − fee: the new UTXO is strictly below the input total,
    # and the echoed figure IS the pending record's own amount_sats.
    assert 0 < pending.amount_sats < 42_000
    lines: list[str] = []
    app._print_self_plan(
        {"inputs_count": pending.inputs_count, "amount_sats": pending.amount_sats,
         "cons_merge": True, "self_mode": "consolidate", "self_parts": 1},
        lines.append,
    )
    assert f"Merge 2 UTXOs to create one new UTXO of {pending.amount_sats:,} sats" in lines

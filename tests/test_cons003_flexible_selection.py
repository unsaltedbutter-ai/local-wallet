"""TCK-CONS-003 — flexible consolidation selection (USER SPEC 2026-09-15).

Five USER phrasings, one per beat, each resolved DETERMINISTICALLY BEFORE
the model (the model never authors a threshold, a selection, or a number)
and every plan riding the EXISTED self_transfer consolidate mode + the
dual-key gate + the SLOW default rung:

1. "consolidate #18 and #24 and #14" — a multi-number registry list in ONE
   utterance (the ``#`` mark IS the address referent; the list is bounded;
   every resolved coin RESTATES its FULL address, glm #7);
2. "consolidate utxos smaller than 100001 sats" — an explicit size cut in
   CHAT-009's comparator shape (thousands separators tolerated, sats only,
   envelope-bounded incl. the int_max_str_digits release; the strict ``<``
   boundary rides the handler);
3. "consolidate small utxos" — the deterministic threshold ASK: one ask,
   the default offer NAMING the user's effective ``consolidate_below_sat_vb``
   with its supplying rung (the CFG-004 read pattern), never-trap close;
4. "consolidate my KYC coins" — the label pool via the v6 address label
   sets (closed tags direct; free labels normalize through the SHARED
   :func:`_normalize_label_word` — quote/case stripping comes free);
5. "consolidate my small Peppermint UTXOs" — the label ∧ size conjunction:
   the ask scoped to the label pool, the answered cut PLANNING the
   intersection (cross-KYC-pool intersections refused like every explicit
   pick).

Rides the CONS-001/CHAT-002 production harness (dispatch table, mock
chain, fake-device signer — no network, deterministic). Every consumed
turn is transcript-free: prompts stay empty on all five paths.
"""

from __future__ import annotations

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.protocol import IntentName
from localwallet.tx.flow import GateDecision, TxFlowStatus
from tests.test_cons001_conversation import (
    KYC_BIG_TXID,
    KYC_SMALL_TXID,
    UNLAB_TXID,
    _coin,
    _env,
    _labeled,
    _ride,
    _spy,
    _turn,
)
from tests.test_cons001_conversation import (
    world as _cons_world,  # noqa: F401 — the CONS-001 harness fixture
)
from tests.test_e2e_skeleton import derive_fixture_addresses
from tests.test_rbf004_bump import _add_coin, _FakeGen


@pytest.fixture()
def world(_cons_world):  # noqa: F811 — fixture alias, not a shadowing arg
    yield _cons_world


def _show_all(world, count: int) -> dict[str, int]:
    """Register the first ``count`` receive addresses in order (showing IS
    the CHAT-001 registration act) → address -> stable number == index+1."""
    addrs = derive_fixture_addresses(count)
    return {
        a: world["store"].note_address_shown(world["wallet"].id, a).number
        for a in addrs
    }


# =========================================================================
# 1. "consolidate #18 and #24 and #14" — the bounded multi-number list
# =========================================================================


def test_multi_number_list_restates_every_address_and_plans(world) -> None:
    addrs = derive_fixture_addresses(24)
    numbers = _show_all(world, 24)
    _add_coin(world["store"], world["wallet"].id, 13, "1" * 64, 11_000)
    _add_coin(world["store"], world["wallet"].id, 17, "2" * 64, 13_000)
    _add_coin(world["store"], world["wallet"].id, 23, "3" * 64, 12_000)
    assert (numbers[addrs[13]], numbers[addrs[17]], numbers[addrs[23]]) == (14, 18, 24)
    seen = _spy(world)
    outs, fake, loop = _turn(
        world, "consolidate #18 and #24 and #14"
    )
    assert fake.prompts == [] and loop.history == ()  # fully deterministic
    # EVERY address restated (glm #7), one line per resolved coin:
    assert f"Coin #18 at {addrs[17]} — 13,000 sats." in outs
    assert f"Coin #24 at {addrs[23]} — 12,000 sats." in outs
    assert f"Coin #14 at {addrs[13]} — 11,000 sats." in outs
    # the plan: exactly the three picked coins, threshold engine-owned
    assert world["flow"].state is TxFlowStatus.CREATED
    assert world["flow"].pending is not None and world["flow"].pending.inputs_count == 3
    assert seen[-1].params.model_dump() == {
        "mode": "consolidate",
        "below_size_sats": 13_001,  # max(picked)+1 — never a user number
    }
    # the card restates every source by NUMBER + FULL address again (§3a)
    assert app._CONS_SOURCE_HEADER in outs
    assert f"  #18. {addrs[17]} · 13,000 sats" in outs


def test_duplicate_numbers_dedup_and_unknown_clarifies(world) -> None:
    numbers = _show_all(world, 3)
    addrs = derive_fixture_addresses(3)
    _add_coin(world["store"], world["wallet"].id, 1, "4" * 64, 9_000)
    outs, fake, _ = _turn(world, f"consolidate #{numbers[addrs[1]]} and #{numbers[addrs[1]]}")
    assert fake.prompts == []  # ONE coin, restated ONCE, planned
    assert sum(ln.startswith("Coin #") for ln in outs) == 1
    assert world["flow"].state is TxFlowStatus.CREATED
    _turn(world, "cancel")
    outs2, fake2, _ = _turn(world, "consolidate #14 and #18 and #24")  # unregistered
    assert outs2 == [app.ADDRESS_REF_UNKNOWN]  # the existing clarify, nothing staged
    assert fake2.prompts == []
    assert world["flow"].state is not TxFlowStatus.CREATED


def test_cross_pool_number_list_refused(world) -> None:
    """A #-list spanning the KYC mark rides the SAME value-free refusal as
    the address-worded form — merging is one privacy pool at a time."""
    _labeled(world)
    numbers = _show_all(world, 8)
    addrs = derive_fixture_addresses(8)
    outs, *_ = _turn(
        world,
        f"consolidate #{numbers[addrs[0]]} and #{numbers[addrs[2]]}",  # 100k + 12k kyc
    )
    assert outs == [app._CONS_POOLS_APART]
    assert world["flow"].state is not TxFlowStatus.CREATED


def test_number_list_is_bounded_and_stays_ambiguous_when_mixed(world) -> None:
    """Over the documented input ceiling the line is NOT intercepted (the
    ordinary pipeline answers, never a silent truncation); a number list
    AND a stated size cut in one line is ambiguous and releases too."""
    many = " ".join(f"#{n}" for n in range(1, app.MAX_SELF_TRANSFER_CONSOLIDATE_INPUTS + 2))
    assert app._consolidation_intent(f"consolidate {many}") is None  # 257 > cap
    exact = " ".join(f"#{n}" for n in range(1, app.MAX_SELF_TRANSFER_CONSOLIDATE_INPUTS + 1))
    intent = app._consolidation_intent(f"consolidate {exact}")
    assert intent is not None and intent[3] == tuple(  # the numbers slot
        range(1, app.MAX_SELF_TRANSFER_CONSOLIDATE_INPUTS + 1)
    )
    assert app._consolidation_intent("consolidate #18 under 100000 sats") is None


# =========================================================================
# 2. "consolidate utxos smaller than 100001 sats" — the explicit cut
# =========================================================================


def test_explicit_threshold_cut_is_deterministic_strict_and_bounded(
    world,
) -> None:
    """The user's example verbatim: the envelope carries EXACTLY the code-
    parsed cut (the model authored nothing), the ``<`` is strict (the
    100,001-sat coin stays out of a "smaller than 100001" sweep), and the
    thousands-separator shape parses identically."""
    _labeled(world)
    addrs = derive_fixture_addresses(8)
    _coin(world, "9" * 64, 100_001, index=6)  # ONE sat ABOVE the cut
    seen = _spy(world)
    outs, fake, loop = _turn(world, "consolidate utxos smaller than 100001 sats")
    assert fake.prompts == [] and loop.history == ()
    assert seen[-1].params.model_dump() == {
        "mode": "consolidate",
        "below_size_sats": 100_001,
    }
    pending = world["flow"].pending
    assert pending is not None and pending.inputs_count == 3  # 100k + 45k + 7k
    body = "\n".join(outs)
    assert " · 100,001 sats" not in body  # the strict boundary, on the card
    assert any(
        addrs[0] in ln and ln.endswith(" · 100,000 sats") for ln in outs
    )  # the 100k seed coin IS merged — restated by FULL address (glm #7)
    _turn(world, "cancel")
    outs2, *_ = _turn(world, "consolidate utxos smaller than 20,000 sats")
    assert seen[-1].params.below_size_sats == 20_000  # separators tolerated
    assert app._CONS_SOURCE_HEADER in outs2


def test_threshold_grammar_releases_what_it_cannot_fully_parse(
    world,
) -> None:
    """Out-of-envelope values, units outside both families and pathological
    digit tokens all KEEP the ordinary pipeline (the CHAT-009 bound +
    release lesson) — a half-read threshold is never a plan."""
    # A decimal WITHOUT a BTC-family unit is not a fully-parsed comparator
    # — it keeps its pre-ticket shape byte-identically (the below-word
    # reads fuzzy, the leftover never names a stored label, the runner
    # falls through; it never becomes a plan):
    assert app._consolidation_intent(
        "consolidate utxos smaller than 0.5 sats"
    ) == (None, None, True, (), None, "0.5")
    assert app._consolidation_intent(
        "consolidate utxos smaller than 0.01"
    ) == (None, None, True, (), None, "0.01")
    # A unit word outside both families beside an INTEGER keeps the old
    # reading too (the cut stands, the odd word is label residue — the
    # pre-ticket shape):
    assert app._consolidation_intent(
        "consolidate utxos smaller than 100000 bits"
    ) == (None, None, False, (), 100_000, "bits")
    assert app._consolidation_intent("consolidate utxos larger than 100000 sats") is None
    # above all bitcoin:
    assert app._consolidation_intent("consolidate utxos below 99999999999999999 sats") is None
    assert app._consolidation_intent(
        "consolidate utxos smaller than " + "9" * 5000 + " sats"
    ) is None
    # A line the comparator gate rejects outright (no object word) is the
    # pure release, as always:
    _labeled(world)
    _, fake, _ = _turn(world, "consolidate my 3 favorite coins")
    assert len(fake.prompts) == 1  # released to the ordinary pipeline
    # A decimal WITHOUT a unit reads fuzzy instead (the pre-ticket shape,
    # byte-identical): the line is the known "small" family, so the ONE
    # threshold ask opens and the model is never consulted.
    _, fake2, loop2 = _turn(world, "consolidate utxos smaller than 0.01")
    assert fake2.prompts == [] and loop2.history == ()
    ask = world["session"].cons_ask
    assert ask is not None and ask.kind == "threshold"
    world["session"].cons_ask = None


def test_btc_unit_size_cut_is_accepted(world) -> None:
    """TCK-CHAT-010 (a) RE-ADJUDICATED PIN: "smaller than 100000 btc" was
    released (non-sats unit); the BTC family + decimals are now the shared
    grammar — the exact Decimal conversion lands as the code-built cut,
    the model never authors it."""
    assert app._consolidation_intent(
        "consolidate utxos smaller than 100000 btc"
    ) == (None, None, False, (), 10_000_000_000_000, "")
    assert app._consolidation_intent(
        "consolidate utxos smaller than 0.01 bitcoin"
    ) == (None, None, False, (), 1_000_000, "")
    _labeled(world)
    seen = _spy(world)
    _outs, fake, loop = _turn(world, "consolidate utxos smaller than 0.1 btc")
    assert fake.prompts == [] and loop.history == ()  # fully deterministic
    assert seen[-1].params.model_dump() == {
        "mode": "consolidate",
        "below_size_sats": 10_000_000,  # 0.1 BTC, exact Decimal
    }


# =========================================================================
# 3. "consolidate small utxos" — the deterministic threshold ASK
# =========================================================================


def test_fuzzy_small_asks_once_naming_the_user_ceiling(world) -> None:
    """The ask OPENS (nothing plans silently) and names the user's OWN
    effective ``consolidate_below_sat_vb`` — value, unit and supplying rung
    — verbatim from the CFG-004 read seam (never a hardcoded constant);
    the envelope's own number only ever comes from the user's ANSWER."""
    _labeled(world)
    key = app.CONSOLIDATE_BELOW_SAT_VB_SETTING
    effective = app._chat_effective_setting(world["store"], key)
    outs, fake, _ = _turn(world, "consolidate small utxos")
    assert fake.prompts == []
    ask = world["session"].cons_ask
    assert ask is not None and ask.kind == "threshold" and ask.entries == ()
    assert world["flow"].state is TxFlowStatus.IDLE  # an ask, not a plan
    assert app._CONS_THRESHOLD_ASK.format(scope="") in outs
    assert isinstance(effective, tuple)  # a clean ladder in the harness env
    assert app._CONS_THRESHOLD_DEFAULT.format(
        label=app._CHAT_SETTING_DISPLAY[key][0],  # "Consolidation fee ceiling"
        value=effective[0],
        unit=app._CHAT_SETTING_DISPLAY[key][1],  # "sat/vB"
        rung=app._chat_rung_phrase(effective[1], key),
    ) in outs
    # and ONE number answers, riding the existing threshold handler policy:
    seen = _spy(world)
    _turn(world, "50000 sats")
    assert seen[-1].params.model_dump() == {
        "mode": "consolidate",
        "below_size_sats": 50_000,
    }
    assert world["flow"].state is TxFlowStatus.CREATED


def test_threshold_ask_never_traps_any_next_utterance(world) -> None:
    """ONE ask: ANY next utterance closes it — a word, a deny, and even a
    number the envelope cannot carry (sub-dust) — and the line falls
    through to the ordinary pipeline unfed (never a re-serve)."""
    _labeled(world)
    for closer in ("what color is the sky", "no", "100"):
        _turn(world, "consolidate my small utxos")
        assert world["session"].cons_ask is not None
        _, fake, _ = _turn(world, closer)
        assert world["session"].cons_ask is None  # closed, never traps
        assert len(fake.prompts) == 1  # the utterance went to the ordinary pipeline


# =========================================================================
# 4. "consolidate my KYC coins" — the label pool, routed directly
# =========================================================================


def test_kyc_capitalized_repro_routes_into_the_count_ask(world) -> None:
    """The user's exact example (capitalized): the closed tag matches in
    code, the group's count/sats restate from the STORE's rows, and the
    model sees nothing (the quote-variant rides the same route — edge
    punctuation strips come free with the matcher)."""
    _labeled(world)
    outs, fake, loop = _turn(world, "consolidate my KYC coins")
    assert fake.prompts == [] and loop.history == ()
    assert world["session"].cons_ask is not None
    assert world["session"].cons_ask.kind == "count"
    assert "You marked 2 coins 'kyc' — 42,000 sats together." in outs[0]
    _turn(world, "close this aside")  # the never-trap close (house corner: an
    # opener line does not preempt an open ask — it closes it)
    outs2, fake2, _ = _turn(world, "consolidate my \"KYC\" coins")  # quoted variant
    assert fake2.prompts == []
    assert world["session"].cons_ask is not None
    assert world["session"].cons_ask.kind == "count"
    assert outs2 == outs  # the SAME deterministic route


def test_free_label_normalizes_via_the_shared_helper(world) -> None:
    """A v6 free-text label routes the DIRECT phrasing into the same
    machinery: case comes free via the shared normalizer (never
    duplicated here), and the LIKE fallback reaches a multi-word stored
    label from its leading word — every plan still restating its FULL
    address."""
    addrs = derive_fixture_addresses(8)
    world["store"].add_address_labels(addrs[2], ["Peppermint"])
    _coin(world, KYC_SMALL_TXID, 12_000, index=2)  # untagged this time
    outs, fake, _ = _turn(world, "consolidate my peppermint coins")
    assert fake.prompts == []
    # single-coin group: honest collapse straight to the plan (no fake ask)
    assert world["flow"].state is TxFlowStatus.CREATED
    assert app._CONS_SOURCE_HEADER in outs
    assert any(addrs[2] in ln and "12,000 sats" in ln for ln in outs)
    _turn(world, "cancel")
    world["store"].add_address_labels(addrs[3], ["graduation funds"])
    _coin(world, KYC_BIG_TXID, 30_000, index=3)
    _turn(world, "consolidate my graduation coins")  # LIKE fallback from ONE word
    assert world["flow"].state is TxFlowStatus.CREATED  # single coin collapsed
    _turn(world, "cancel")
    # a word that names no label ANYWHERE keeps today's roll-up unchanged:
    _turn(world, "consolidate my zebra coins")
    ask = world["session"].cons_ask
    assert ask is not None and ask.kind == "rollup"


# =========================================================================
# 5. "consolidate my small Peppermint UTXOs" — label ∧ size conjunction
# =========================================================================


def test_conjunction_scopes_the_ask_and_plans_the_intersection(
    world,
) -> None:
    """Fuzzy small + a stored label: the ask is SCOPED to the pool, the
    answered cut plans the INTERSECTION only (a peppermint coin above the
    cut stays out; an unlabeled coin below it stays out — the label is
    never silently dropped), and the card restates the merged FULL
    address. The fee rung named at the OPENING persists across the ask."""
    addrs = derive_fixture_addresses(8)
    _coin(world, KYC_SMALL_TXID, 12_000, index=2, tag=None)
    _coin(world, KYC_BIG_TXID, 30_000, index=3)
    world["store"].add_address_labels(addrs[2], ["peppermint"])
    world["store"].add_address_labels(addrs[3], ["peppermint"])
    _coin(world, UNLAB_TXID, 7_000, index=7)  # small, but NOT peppermint
    seen = _spy(world)
    outs, fake, _ = _turn(world, "consolidate my small Peppermint UTXOs slowly")
    assert fake.prompts == []
    ask = world["session"].cons_ask
    assert ask is not None and ask.kind == "threshold" and len(ask.entries) == 2
    assert ask.fee_target == "slow"  # the RBF-004 persistence, re-quoted below
    assert "among the 'peppermint' coins" in outs[0]
    outs2, *_ = _turn(world, "20000")
    # intersection only: the 12k peppermint coin; the 30k one and the 7k
    # unlabeled one are NOT this plan.
    assert seen[-1].params.model_dump() == {
        "mode": "consolidate",
        "below_size_sats": 12_001,
        "fee_target": "slow",  # the rung named at the OPENING survived the ask
    }
    pending = world["flow"].pending
    assert pending is not None and pending.inputs_count == 1
    assert f"  #{1}. {addrs[2]} · 12,000 sats" in outs2
    assert " · 30,000 sats" not in "\n".join(outs2)
    assert " · 7,000 sats" not in "\n".join(outs2)


def test_conjunction_with_explicit_cut_plans_without_asking(world) -> None:
    """The stated number IS the selection — label ∧ explicit cut plans
    directly (no ask): everything outside the intersection is out."""
    addrs = derive_fixture_addresses(8)
    _coin(world, KYC_SMALL_TXID, 12_000, index=2)
    _coin(world, UNLAB_TXID, 7_000, index=7)
    world["store"].add_address_labels(addrs[2], ["peppermint"])
    _turn(world, "consolidate my peppermint coins under 100000 sats")
    assert world["session"].cons_ask is None  # never asked: the cut was stated
    pending = world["flow"].pending
    assert pending is not None and pending.inputs_count == 1  # only the 12k


def test_conjunction_intersection_respects_the_pool_guard(world) -> None:
    """A free label straddling the KYC mark is refused exactly like an
    explicit number-list pick — value-free, nothing staged."""
    addrs = derive_fixture_addresses(8)
    _coin(world, KYC_SMALL_TXID, 12_000, index=2, tag="kyc")
    _coin(world, UNLAB_TXID, 7_000, index=7)  # other side, untagged
    world["store"].add_address_labels(addrs[2], ["mixed"])
    world["store"].add_address_labels(addrs[7], ["mixed"])
    outs, fake, _ = _turn(world, "consolidate my mixed coins under 99999999 sats")
    assert fake.prompts == []  # consumed with the refusal, never the model
    assert outs == [app._CONS_POOLS_APART]
    assert world["flow"].state is not TxFlowStatus.CREATED


# =========================================================================
# 6. The gate is unchanged on every new route
# =========================================================================


def test_dual_key_gate_unchanged_on_the_new_plans(world) -> None:
    """A plan reached through the new selection surface rides the SAME
    create→confirm→sign→broadcast state machine: an LLM-relayed confirm
    WITHOUT the same-turn gate decision is REFUSED, and only the user's
    own key lets it through (ADR-0013, byte-for-byte the landed gate)."""
    _labeled(world)
    _turn(world, "consolidate utxos smaller than 100001 sats")
    assert world["flow"].state is TxFlowStatus.CREATED
    ref = world["flow"].pending.tx_ref  # type: ignore[union-attr]
    world["session"].gate_decision = GateDecision.NOT_A_DECISION
    refused = world["table"][IntentName.CONFIRM_TX](_env("confirm_tx", {"tx_ref": ref}))
    assert refused["error"] == "confirm_refused"
    assert world["flow"].state is TxFlowStatus.CREATED  # still pending
    world["session"].gate_decision = GateDecision.CONFIRM
    _ride(world)  # the UNCHANGED full ride (sign via the fake device)
    assert world["flow"].state is not TxFlowStatus.CREATED


def test_every_new_route_is_transcript_free(world) -> None:
    """The §7.10 discipline on all three new surfaces at once: label words,
    the ``#`` list, the threshold answer and every ask never touch the
    model or the transcript — the numbers on screen come only from the
    store rows and the code-built envelope (which can carry NOTHING but
    the engine threshold + the rung)."""
    _labeled(world)
    addrs = derive_fixture_addresses(8)
    world["store"].add_address_labels(addrs[2], ["peppermint"])
    fg = _FakeGen()
    loop = AgentLoop(fg, world["table"])
    for line in (
        "consolidate my small Peppermint UTXOs",  # opener → scoped ask
        "20000",  # the answer plans
        "consolidate my KYC coins",  # label pool
        "all",  # count answer
    ):
        app._run_turn(
            loop, world["flow"], world["session"], line, [].append,
            table=world["table"], store=world["store"],
        )
        if world["flow"].state is TxFlowStatus.CREATED:
            _ride(world)  # both plans ride the unchanged gate to broadcast
    assert loop.history == () and fg.prompts == []  # zero model contact

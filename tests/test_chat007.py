"""TCK-CHAT-007 — receive-address + label: the deterministic dual-action
intercept over the EXISTING new_address handler (the USER REQUEST bug).

The bug (user, 2026-09-13, verbatim): "I need a receive address labeled
'spearmint'" → generated the address but NO label. The request is DOUBLE
(allocate + name); the intent protocol carries no label write, so the model
could only ever answer half of it. The fix under test: a CLOSED pre-model
matcher (fresh/receive-address ask + quoted/bounded label — LABEL-001's
value gate shared, refusals consume), the UNCHANGED ``new_address`` handler,
then the label union-added to the address the HANDLER RETURNED (engine-
derived key, never model- or user-authored) via the sanctioned v6 writer
:``Store.add_address_labels```.

Pinned here:
* matcher matrix — the ticket's phrasings match, value-garbage consumes with
  a value-free "nothing was created" line, everything else (no-ask heads,
  question heads, money-flow heads, the label-less ask, LABEL-001's
  address-literal grammar) falls through UNCHANGED;
* the label lands on the FRESH address, read back from the store (the write
  key is the handler's returned address — two turns label their OWN
  addresses; canonicalization ("KYC"→"kyc") echoes STORED, not typed);
* ack = store truth — the address verbatim from the handler result + the
  committed set as read back after the commit;
* the existing narration/event path composes with the stamp: the CHAT-001
  numbered fresh-address line and the HW-005 ``own_address`` event fire
  unchanged, before the label ack;
* ZERO model calls on a consumed turn, and the label words never reach a
  later turn's prompt (transcript-free, §7.10);
* honest failure: a refused store write narrates "the address is yours, it
  is NOT labeled" (value-free, no success claim); a handler failure writes
  nothing; a wallet-less table refuses without touching the model;
* every value is free of invention: only handler-returned addresses and
  store-read labels are echoed.
"""

from __future__ import annotations

import json
from typing import Any, Final

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.app import (
    _LABEL_MEMBER_ACK,
    _LABEL_SET_KEPT,
    _RECEIVE_LABEL_GARBAGE,
    _RECEIVE_LABEL_NO_VALUE,
    _RECEIVE_LABEL_NO_WALLET,
    _RECEIVE_LABEL_STORE_ERROR,
    _RECEIVE_LABEL_TOO_LONG,
    EVENT_OWN_ADDRESS,
    EVENT_TEXT,
    EventEmitter,
    _receive_label_request,
    _run_receive_label_turn,
)
from localwallet.protocol import IntentName
from localwallet.store import (
    ADDRESS_LABEL_MAX_CHARS,
    Store,
    StoreError,
)
from localwallet.tx.flow import TxFlow
from localwallet.wallet.descriptor import WalletDescriptor
from tests.test_e2e_skeleton import ZPUB, derive_fixture_addresses

#: The fixture wallet's first four branch-0 addresses (allocation order).
ADDRS: Final[list[str]] = derive_fixture_addresses(4)

#: The user's REQUEST, verbatim from the ticket.
USER_LINE: Final[str] = "I need a receive address labeled 'spearmint'"


# --------------------------------------------------------------- turn harness


class _RecordingGen:
    """Fake model: records EVERY prompt. A consumed turn must show up in
    neither the calls nor any prompt (label words = user data, §7.10)."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, prompt: str, grammar_text: str | None) -> str:
        del grammar_text
        self.prompts.append(prompt)
        return json.dumps({"v": 0, "intent": "respond", "params": {"text": "ok."}})


def _world() -> tuple[Store, int, dict[str, Any]]:
    """Provisioned fixture wallet (in-memory store, active wallet) + the
    REAL dispatch table (the unchanged ``new_address`` handler included)."""
    store = Store.memory()
    wd = WalletDescriptor.from_key(ZPUB)
    wallet = store.create_wallet("default", wd.descriptor)
    store.set_active_wallet(wallet.id)
    table = app.build_dispatch_table(
        store, wallet, wd.parsed, client=None, scan_fn=lambda: None
    )
    return store, wallet.id, table


def _turn(
    store: Store,
    table: dict[str, Any],
    line: str,
    *,
    gen: _RecordingGen | None = None,
    loop: AgentLoop | None = None,
) -> tuple[list[str], _RecordingGen, AgentLoop]:
    """One REAL turn through ``_run_turn`` (the production intercept
    ordering), on a fresh or caller-supplied loop."""
    gen = gen if gen is not None else _RecordingGen()
    loop = loop if loop is not None else AgentLoop(gen, table)
    out: list[str] = []
    app._run_turn(
        loop, TxFlow(), app.SendSession(), line, out.append,
        table=table, store=store,
    )
    return out, gen, loop


def _events() -> tuple[EventEmitter, list[Any]]:
    events: list[Any] = []
    return EventEmitter(events.append), events


# =========================================================================
# 1. Matcher matrix — the closed grammar (match / consume / fall through)
# =========================================================================


@pytest.mark.parametrize(
    ("line", "label"),
    [
        # The user's verbatim request + the quoted double-quote shape.
        (USER_LINE, "spearmint"),
        ('I need a receive address labeled "spearmint"', "spearmint"),
        # The ticket's second example: bounded free text after "for".
        ("new address for spearmint", "spearmint"),
        ("New Address for Spearmint", "Spearmint"),
        # Connector family + next-receive phrasing.
        ("give me the next receive address called 'mint tea'", "mint tea"),
        ("fresh address named 'office funds'", "office funds"),
        ("receive address labelled spearmint", "spearmint"),
        ("generate a new address for spearmint", "spearmint"),
        # Trailing-quoted form (no connector; the whole tail is the span).
        ("new address 'spearmint'", "spearmint"),
        # Ask-verb heads (the user's request shapes) and multi-word spans.
        ("we need an address labeled 'spearmint'", "spearmint"),
        ("can I get a new receive address for spearmint", "spearmint"),
        ("make a fresh address called 'KYC'", "KYC"),
        # Positive control: the label VALUE keeps its curly apostrophe
        # verbatim (HEAD-only folding — "what's" here is the label, not a
        # question head, so it must NOT be blocked and NOT be stripped).
        ("receive address labeled what\u2019s", "what\u2019s"),
    ],
)
def test_matcher_matches_the_closed_family(line: str, label: str) -> None:
    assert _receive_label_request(line) == (label,)


@pytest.mark.parametrize(
    ("line", "refusal"),
    [
        # Shape matched, value missing/overflowed/garbage → CONSUMED refusals.
        ("new address for", _RECEIVE_LABEL_NO_VALUE),
        ("I need a receive address labeled", _RECEIVE_LABEL_NO_VALUE),
        ("new address labeled '", _RECEIVE_LABEL_GARBAGE),
        ("new address labeled 'spearmint", _RECEIVE_LABEL_GARBAGE),
        ('new address "spearmint" please', _RECEIVE_LABEL_GARBAGE),
        (
            "new address labeled " + "z" * (ADDRESS_LABEL_MAX_CHARS + 1),
            _RECEIVE_LABEL_TOO_LONG,
        ),
    ],
)
def test_matcher_refusals_are_code_strings(line: str, refusal: str) -> None:
    assert _receive_label_request(line) == refusal


@pytest.mark.parametrize(
    "line",
    [
        # The label-less ask: the model path already answers it, unchanged.
        "new address",
        "I need a receive address",
        # LABEL-001's address-literal grammar (and its half-garbage cousin).
        f"label {ADDRS[0]} as spearmint",
        "label not-an-address as spearmint",
        # Money-flow / change-branch / question heads: NOT this grammar.
        "send 50000 sats to the new address for spearmint",
        "I need a new change address labeled 'spearmint'",
        "what is the new address for spearmint",
        "what's the new address for spearmint",
        "what\u2019s the new address for spearmint",
        "where's the new address for spearmint",
        "how do I get a new address labeled spearmint",
        # Head without a fresh/receive ask (an inquiry, a bare mention).
        "show the address labeled spearmint",
        "the address for spearmint",
        # Nothing before the connector; no address word at all.
        "for spearmint give me a new address",
        "create a coin labeled 'spearmint'",
        # Question about the settings, not a create+label ask.
        "what is the gap limit for the new address",
    ],
)
def test_matcher_misses_fall_through(line: str) -> None:
    assert _receive_label_request(line) is None


# =========================================================================
# 2. The dual action end to end — fresh address + label on the RETURNED key
# =========================================================================


def test_user_request_allocates_and_labels_the_fresh_address() -> None:
    store, wallet_id, table = _world()
    out, gen, loop = _turn(store, table, USER_LINE)
    addr = ADDRS[0]  # the engine-derived first allocation
    # Existing narration UNCHANGED: the CHAT-001 numbered fresh-address line.
    assert out[0] == f"Fresh receive address (index 0, address #1): {addr}"
    # Ack = store truth: address verbatim + the label AS STORED (read back).
    assert out[1] == _LABEL_MEMBER_ACK.format(
        address=addr, stored="spearmint", labels="spearmint"
    )
    # The label landed on the FRESH address (the returned one), keyed there.
    assert store.get_address_label_set(addr) == ("spearmint",)
    assert store.get_derivation(wallet_id, 0).next_index == 1
    # Zero model calls on the consumed turn; nothing entered the transcript.
    assert gen.prompts == []
    assert loop.history == ()


def test_two_dual_actions_label_their_own_addresses() -> None:
    """The write key is the address each handler run RETURNED — never the
    same target twice, never a user- or model-authored address."""
    store, _wallet_id, table = _world()
    out1, _gen, _loop = _turn(store, table, "new address for spearmint")
    out2, _gen, _loop = _turn(store, table, "give me a fresh address labeled 'mint'")
    assert out1[0].endswith(ADDRS[0]) and out2[0].endswith(ADDRS[1])
    assert store.get_address_label_set(ADDRS[0]) == ("spearmint",)
    assert store.get_address_label_set(ADDRS[1]) == ("mint",)
    assert out2[1] == _LABEL_MEMBER_ACK.format(
        address=ADDRS[1], stored="mint", labels="mint"
    )


def test_tag_word_canonicalizes_and_the_ack_echoes_the_STORED_form() -> None:
    """Store truth, not typed truth: the ack quotes the SET the writer read
    back — 'KYC' is stored (and narrated) as the engine id ``kyc``."""
    store, _wallet_id, table = _world()
    out, _gen, _loop = _turn(store, table, "new address labeled 'KYC'")
    assert store.get_address_label_set(ADDRS[0]) == ("kyc",)
    assert '"kyc"' in out[1] and "KYC" not in "\n".join(out)


def test_reissued_fresh_address_honestly_answers_already_carries() -> None:
    """Crash-window same-string re-issue (the handler's documented corner):
    the union-add lands on an address that already carries the member — the
    ack says so instead of faking a new store."""
    store, _wallet_id, table = _world()
    store.add_address_labels(ADDRS[0], ("spearmint",))  # pre-stamped shape
    out, _gen, _loop = _turn(store, table, "new address for spearmint")
    assert out[1] == _LABEL_SET_KEPT.format(
        address=ADDRS[0], labels="spearmint"
    )
    assert store.get_address_label_set(ADDRS[0]) == ("spearmint",)


# =========================================================================
# 3. Narration/event compose + model-context hygiene at the turn level
# =========================================================================


def test_own_address_event_composes_under_the_label_stamp() -> None:
    """The HW-005 event rides the SAME printer as the model path: exactly
    one own_address event, keyed to the handler-returned address, after the
    fresh-address narration — and the labeled store agrees with it."""
    store, _wallet_id, table = _world()
    emitter, events = _events()
    out: list[str] = []

    def sink(line: str) -> None:
        out.append(line)
        emitter.text(line)  # (the web pump's shape: output_fn IS the text sink)

    app._run_turn(
        AgentLoop(_RecordingGen(), table), TxFlow(), app.SendSession(),
        USER_LINE, sink, table=table, store=store, emitter=emitter,
    )
    own = [json.loads(e.payload) for e in events if e.kind == EVENT_OWN_ADDRESS]
    assert own == [{"address": ADDRS[0], "branch": 0, "index": 0}]
    kinds = [e.kind for e in events]
    assert kinds.index(EVENT_TEXT) < kinds.index(EVENT_OWN_ADDRESS)
    assert ADDRS[0] in out[0]  # the event names EXACTLY the shown string
    assert store.get_address_label_set(ADDRS[0]) == ("spearmint",)


def test_label_words_never_reach_a_later_prompt() -> None:
    """Transcript-free consumption (the RBF-004/CPFP-002 discipline): the
    NEXT turn's prompt carries no trace of the label words — history
    re-injects user text verbatim, so the consumed turn was never added."""
    store, _wallet_id, table = _world()
    gen = _RecordingGen()
    loop = AgentLoop(gen, table)
    _turn(store, table, USER_LINE, gen=gen, loop=loop)
    assert loop.history == ()
    _turn(store, table, "hello there", gen=gen, loop=loop)
    assert gen.prompts, "the unmatched turn must reach the model"
    assert all("spearmint" not in p for p in gen.prompts)


def test_refused_values_consume_without_creating_anything() -> None:
    """Value-garbage on a matched ask still consumes (never a model
    fabrication route) — and NOTHING was created, value-free copy."""
    store, wallet_id, table = _world()
    out, gen, _loop = _turn(store, table, "new address labeled 'spearmint")
    assert out == [_RECEIVE_LABEL_GARBAGE]
    assert gen.prompts == []
    assert store.get_derivation(wallet_id, 0).next_index == 0
    assert store.get_address_label_sets() == {}
    assert "spearmint" not in out[0]


# =========================================================================
# 4. Honest failure — never a claimed success
# =========================================================================


def test_write_failure_narrates_the_honest_split(monkeypatch: pytest.MonkeyPatch) -> None:
    """The address is REAL (allocated, narrated, numbered); the label is
    NOT — the line says exactly that, value-free, store-empty behind it."""

    def boom(self: Store, address: str, labels: Any) -> tuple[str, ...]:
        raise StoreError("value-free refusal")

    monkeypatch.setattr(Store, "add_address_labels", boom)
    store, wallet_id, table = _world()
    out, gen, _loop = _turn(store, table, USER_LINE)
    assert f"{ADDRS[0]}" in out[0]  # the fresh address line still printed
    assert out[-1] == _RECEIVE_LABEL_STORE_ERROR
    assert "spearmint" not in "\n".join(out)  # nothing echoed that isn't stored
    assert store.get_address_label_sets() == {}
    assert store.get_derivation(wallet_id, 0).next_index == 1  # address kept
    assert gen.prompts == []


def test_handler_failure_writes_nothing() -> None:
    """A failed allocation narrates the EXISTING printer's error line and
    no label write is attempted against any address."""
    store, _wallet_id, table = _world()
    table[IntentName.NEW_ADDRESS] = lambda env: {
        "error": "store_error", "detail": "d"
    }
    out, gen, _loop = _turn(store, table, "new address for spearmint")
    assert len(out) == 1  # the failure line only — no label ack follows
    assert out[0].startswith("Could not allocate a new address — store_error")
    assert "spearmint" not in out[0]
    assert store.get_address_label_sets() == {}
    assert gen.prompts == []


def test_no_wallet_table_refuses_without_creating_or_calling_the_model() -> None:
    """A pump without a wallet (NEW_ADDRESS absent from the table): the
    honest nothing-to-create line consumes — the matched label words still
    never reach the model."""
    store = Store.memory()
    table: dict[str, Any] = {}
    out, gen, _loop = _turn(store, table, USER_LINE)
    assert out == [_RECEIVE_LABEL_NO_WALLET]
    assert gen.prompts == []


# =========================================================================
# 5. Sibling routes unchanged — LABEL-001 keeps priority, misses stay chat
# =========================================================================


def test_label001_grammar_still_wins_and_never_allocates() -> None:
    """'label <addr> as X' (the first word is LABEL-001's verb): the
    address-literal intercept consumes it; NO fresh address is created."""
    store, wallet_id, table = _world()
    out, gen, _loop = _turn(store, table, f"label {ADDRS[2]} as spearmint")
    assert out == [_LABEL_MEMBER_ACK.format(
        address=ADDRS[2], stored="spearmint", labels="spearmint"
    )]
    assert store.get_address_label_set(ADDRS[2]) == ("spearmint",)
    assert store.get_derivation(wallet_id, 0).next_index == 0  # no allocation
    assert gen.prompts == []


def test_unmatched_new_address_ask_still_reaches_the_model() -> None:
    """The ticket ships NO prompt line: the label-less "new address" ask
    keeps its existing (unchanged) model route."""
    store, _wallet_id, table = _world()
    out, gen, _loop = _turn(store, table, "new address")
    assert gen.prompts  # not consumed — the ordinary pipeline saw it
    assert out == ["ok."]  # the fake gen's respond, unchanged


def test_runner_api_is_pure_and_keyed_on_the_result_address() -> None:
    """The runner consumes ONLY its closed grammar and stamps ONLY what the
    handler returned (a fake result proves the key travels from the
    result, never from the line)."""
    store, _wallet_id, table = _world()

    def handler(env: Any) -> dict[str, Any]:
        del env
        return {
            "address": ADDRS[3], "branch": 0, "index": 3, "address_number": 9,
        }

    table[IntentName.NEW_ADDRESS] = handler
    out: list[str] = []
    assert _run_receive_label_turn(
        store, "give me a new receive address for 'spearmint'", out.append,
        table=table,
    )
    assert store.get_address_label_set(ADDRS[3]) == ("spearmint",)
    assert out[0] == f"Fresh receive address (index 3, address #9): {ADDRS[3]}"
    assert not _run_receive_label_turn(
        store, "what is the new address for spearmint", out.append, table=table
    )

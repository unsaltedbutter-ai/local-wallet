"""TCK-FIAT-004 — fiat-amount sends convert ENGINE-SIDE (the "$45 → 546 sats" bug).

The bug (live report 2026-09-13): "i need to send $45 to bc1q…" never
became $45-worth of sats. Diagnosis (run against the pinned GGUF + the real
grammar + the real prompt): MODEL ROUTING — the prompt carried no fiat-send
mapping (the "$" wording matched the ambiguous-bare-number clarify few-shot
almost verbatim, and the follow-up currency word got hijacked by the
get_balance fiat line), so ``amount_usd`` never reached the handler; the
546-sat figure is the schema floor the model transcribed from the validator
retry note once it tried to "correct" a sub-dust sats amount. The grammar
(``amount-usd-kv``/``usd-num``), the schema and the handler conversion
(:func:`PriceOracle.usd_to_sats` at the create_tx site) were all sound.

The fix under test: (1) prompt routing — a number with a currency symbol or
word goes to ``create_tx.amount_usd`` verbatim, the model NEVER converts
(plus golden-078 = the user's exact utterance); (2) TCK-FIAT-003's per-ask
currency intercept EXTENDED to the closed symbol set ($ £ € ¥), so "$45"
converts at the USD rate even when the display ladder answers another
currency. Engine-side decisions pinned here: conversion floors to WHOLE
sats (never sub-sat precision), a stale-but-served rate converts WITH the
card's stale marker (the dual-key gate then confirms the real number —
ADR-0011), and an ABSENT rate refuses honestly (``price_unavailable``) —
never a fabricated rate, never a silent default.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Final

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.agent.prompt import build_system_prompt
from localwallet.chain import PriceOracle
from localwallet.config import Settings
from localwallet.protocol import IntentName, validate_payload
from tests.test_e2e_skeleton import (
    RESPOND_NOTED_JSON,
    SEND_RECIPIENT,
    SEND_UTXO,
    _build_send_table,
    _create_tx_envelope_json,
    _send_chain_handler,
    derive_fixture_addresses,
)

#: Multi-currency body: USD $100,000/BTC (45 USD → exactly 45,000 sats)
#: and EUR 62,000/BTC — the two rates MUST disagree so a wrong-currency
#: conversion is visible in the staged amount.
PRICES: Final[dict[str, Any]] = {
    "time": 1_700_000_000,
    "USD": 100_000.0,
    "EUR": 62_000.0,
}

UTTER = f"i need to send $45 to {SEND_RECIPIENT}"
USD_ENVELOPE: Final[str] = _create_tx_envelope_json(
    {"recipient": SEND_RECIPIENT, "amount_usd": 45}
)


class _FixedGen:
    """Scripted fake model (the routing fix itself is eval-pinned, not
    simulated here): emits the canned envelope, then a benign respond."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)

    def __call__(self, prompt: str, grammar_text: str | None) -> str:
        del grammar_text
        return self._replies.pop(0) if self._replies else RESPOND_NOTED_JSON


def _world(state: dict[str, Any] | None = None, *, display: str = "usd", ttl_s: float = 60.0):
    """Production-shaped send world: the session-aware display-currency
    reader (per-ask one-shot > ladder) feeds the oracle that the create_tx
    handler consults for the ``amount_usd`` conversion."""
    state = state if state is not None else {}
    state.setdefault("prices_payload", PRICES)
    box: dict[str, Any] = {"currency": lambda: "usd"}
    table, store, _wallet, client, recorded, flow, session = _build_send_table(
        lambda rec: _send_chain_handler(
            rec,
            utxos_by_addr={derive_fixture_addresses(1)[0]: [SEND_UTXO]},
            state=state,
        ),
        make_price_oracle=lambda client_: PriceOracle(
            client_, ttl_s=ttl_s, currency=lambda: box["currency"]()
        ),
    )
    box["currency"] = app._display_currency_reader(
        Settings(display_currency=display), store, session
    )
    return {"table": table, "store": store, "client": client, "recorded": recorded,
            "flow": flow, "session": session, "state": state}


def _create(world: dict[str, Any], envelope_json: str) -> dict[str, object]:
    envelope = validate_payload(envelope_json)
    return world["table"][IntentName.CREATE_TX](envelope)


def _turn(world: dict[str, Any], line: str, reply: str) -> list[str]:
    """One REAL REPL turn through ``_run_turn`` (the intercept stamps the
    one-shot from the user's OWN utterance, exactly as production does)."""
    loop = AgentLoop(_FixedGen([reply]), world["table"])
    lines: list[str] = []
    app._run_turn(
        loop, world["flow"], world["session"], line, lines.append,
        table=world["table"], store=world["store"],
    )
    return lines


# =========================================================================
# 1. The user repro: "$45" becomes $45-WORTH OF SATS, never a dust default
# =========================================================================


def test_user_repro_dollar_send_stages_usd_worth_of_sats() -> None:
    world = _world()
    try:
        outputs = _turn(world, UTTER, USD_ENVELOPE)
        joined = "\n".join(outputs)
        # 45 USD @ $100,000/BTC → exactly 45,000 sats — the REAL number
        # the user confirms through the unchanged dual-key gate.
        assert "Pay: 45,000 sats ($45.00 · @ $100,000/BTC)" in joined
        pending = world["flow"].pending
        assert pending is not None
        assert pending.amount_sats == 45_000
        # The bug figure: 546 (the dust/schema floor) was NEVER staged.
        assert pending.amount_sats != 546
    finally:
        world["client"].close()
        world["store"].close()


def test_symbol_one_shot_overrides_a_foreign_display_ladder() -> None:
    """display_currency = eur, but the user's OWN "$45" names USD: the
    FIAT-003 intercept (extended to symbols by FIAT-004) converts at the
    USD rate. Without the symbol rule this converts as 45 EUR = 72,580."""
    world = _world(display="eur")
    try:
        outputs = _turn(world, UTTER, USD_ENVELOPE)
        pending = world["flow"].pending
        assert pending is not None
        assert pending.amount_sats == 45_000  # USD rate, not the EUR ladder
        assert "45,000 sats" in "\n".join(outputs)
    finally:
        world["client"].close()
        world["store"].close()


def test_amount_usd_without_a_symbol_rides_the_display_ladder() -> None:
    """FIAT-002 semantics UNCHANGED: no currency on the utterance → the
    ladder answers (45 EUR @ 62,000 = 72,580.6… sats → floor 72,580)."""
    world = _world(display="eur")
    try:
        result = _create(world, USD_ENVELOPE)  # dispatched WITHOUT the turn
        # (no utterance → no one-shot → ladder), so this pins the split.
        assert result.get("error") is None
        assert world["flow"].pending is not None
        assert world["flow"].pending.amount_sats == 72_580
    finally:
        world["client"].close()
        world["store"].close()


# =========================================================================
# 2. Rounding: floor to WHOLE sats, never sub-sat precision, never round-up
# =========================================================================


def test_fiat_conversion_floors_to_whole_sats() -> None:
    # 45 USD @ 77,291/BTC = 58,221.526… sats. The floor (58,221) is what
    # stages — a round-to-nearest would fabricate value (58,222) the user
    # never asked for and the wallet does not have at that price.
    state = {"prices_payload": {"time": 1_700_000_000, "USD": 77_291.0}}
    world = _world(state)
    try:
        result = _create(world, USD_ENVELOPE)
        assert result.get("error") is None
        assert result["amount_sats"] == 58_221
        # The card's fiat figure re-derives from the FLOORED sats (Decimal
        # floor both ways): 58,221 sats ≈ $44.99, quoted verbatim.
        assert result["usd_cents"] == 4_499
    finally:
        world["client"].close()
        world["store"].close()


# =========================================================================
# 3. ADR-0011 stale policy (PINNED CHOICE): confirm-with-stale-marker.
#    A stale-but-served rate still converts; the card carries rate_stale +
#    the age; the user confirms the real sats through the unchanged gate.
#    An ABSENT rate is the honest refusal (next test) — never a default.
# =========================================================================


def test_stale_rate_still_converts_and_the_card_marks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from localwallet.chain import price as price_module

    clock = {"now": 1_000.0}
    monkeypatch.setattr(price_module, "_now", lambda: clock["now"])
    state: dict[str, Any] = {"prices_fail": False}
    world = _world(state, ttl_s=0.000001)
    try:
        first = _create(world, USD_ENVELOPE)
        assert first.get("error") is None
        assert first["rate_stale"] is False
        assert first["amount_sats"] == 45_000
        world["flow"].cancel()  # explicit recovery to stage the second send
        state["prices_fail"] = True
        clock["now"] = 2_000.0

        second = _create(world, USD_ENVELOPE)
        assert second.get("error") is None
        # Pinned decision: the send STAGES on the stale rate, marked, with
        # the age — the number is real and the user sees it before confirming.
        assert second["rate_stale"] is True
        assert second["rate_age_s"] == 1_000
        assert second["amount_sats"] == 45_000  # converted at the stale rate
        assert world["flow"].pending is not None
    finally:
        world["client"].close()
        world["store"].close()


def test_absent_rate_refuses_the_fiat_send_without_flow_entry() -> None:
    """No rate at all (cold cache, failing endpoint) → the honest
    fiat-unavailable refusal: NO flow entry, no fabricated conversion, the
    user retries or gives sats (the sats path never consults the oracle for
    the amount, only for display sugar)."""
    world = _world({"prices_fail": True})
    try:
        result = _create(world, USD_ENVELOPE)
        assert result.get("error") == "price_unavailable"
        assert world["flow"].state.name == "IDLE"
        assert world["flow"].pending is None
    finally:
        world["client"].close()
        world["store"].close()


# =========================================================================
# 4. The closed symbol table (deterministic parsing)
# =========================================================================


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # the user's exact shape
        (UTTER, "usd"),
        ("send $45 to bc1q9t3d", "usd"),
        ("€20", "eur"),
        ("£3.50 please", "gbp"),
        ("500 ¥", "jpy"),
        ("¥500", "jpy"),
        ("45$", "usd"),
        ("1.5€", "eur"),
        # words still work (FIAT-003) and agree with their symbol twin
        ("45 dollars", "usd"),
        ("usd", "usd"),
        # same currency twice (symbol or word) is NOT ambiguous
        ("$45 usd", "usd"),
        ("$45 $50", "usd"),
        # ambiguity: two different currencies named
        ("$45 and £4", None),
        ("€5 in dollars", None),
        # nothing named → ride the ladder
        ("send 45 to bc1q9t3d", None),
        ("what is my balance?", None),
        # digits-only address tokens never match (symbols are not bech32)
        ("send to bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", None),
        # a lone symbol token names its currency (the pinned symbol rule:
        # glyph-with-digits in one token, or the glyph standing alone)
        ("the $ sign", "usd"),
    ],
)
def test_symbol_word_table(line: str, expected: str | None) -> None:
    assert app._detect_fiat_ask_currency(line) == expected


# =========================================================================
# 5. The prompt routing fix (drift pins) + the schema untouched
# =========================================================================


def test_prompt_routes_fiat_sends_and_never_asks_the_model_to_convert() -> None:
    prompt = build_system_prompt()
    line = next(
        ln for ln in prompt.splitlines() if ln.startswith("- create_tx:")
    )
    # The FIAT-amount mapping rides the create_tx line itself…
    assert 'A FIAT amount' in line
    assert "amount_usd" in line
    # …and the user's exact utterance ships as a few-shot.
    assert '"amount_usd": 45' in prompt
    # Honesty anchors: never model-side conversion, sats field stays clean.
    assert "NEVER convert it yourself" in prompt
    assert 'fiat number in "amount_sats"' in prompt


def test_no_new_envelope_shape_registry_stays() -> None:
    """FIAT-004 is prompt-routing + engine-side currency reading: the
    closed protocol SHAPE is untouched (amount_usd already existed; no
    currency code ever rides the envelope)."""
    envelope = validate_payload(USD_ENVELOPE)
    assert envelope.params.amount_usd == 45.0
    assert not hasattr(envelope.params, "amount_fiat")
    assert not hasattr(envelope.params, "currency")

"""TCK-FIAT-003 — per-ask currency one-shot (the MW-17 EUR bug) + fiat Fee line.

The bug (live report): "what is my balance in Euros?" answered
``≈ $241.53 · @ $77,291/BTC`` — the FIAT-002 prompt line already routes
euro phrasings to ``get_balance`` and the tagged-currency renderers work;
the failing rung is CURRENCY SELECTION: the handler converts through the
price oracle's ladder reader, and with ``display_currency`` unset the
ladder always answers ``usd``. No per-ask path existed (get_balance params
are ``{}`` and the model must never author a currency code).

The fix under test: a deterministic engine-side intercept on the USER's OWN
utterance (closed word table, whole tokens) stamps a one-shot currency on
``SendSession.fiat_ask_currency`` for exactly that turn's handler dispatch;
the oracle's injected reader consults it; the ``display_currency`` SETTING
is never touched. Plus the tx-card Fee line gains a fiat parenthetical from
the handler's existing rate path (absent rate → sats-only, as today).
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
from localwallet.config import DISPLAY_CURRENCY_SETTING, Settings
from localwallet.protocol import (
    CreateTxParams,
    GetBalanceParams,
    IntentName,
    validate_payload,
)
from tests.test_e2e_skeleton import (
    GET_BALANCE_JSON,
    RESPOND_NOTED_JSON,
    SEND_UTXO,
    _build_send_table,
    _create_tx_envelope_json,
    _send_chain_handler,
    _send_generate,
    derive_fixture_addresses,
)

# The multi-currency payload the FIAT-002 seam serves: 100_000 sats total
# fixture wallet → USD: 2_000 cents ($20.00); EUR: 6_200 cents (62.00 EUR).
PRICES: Final[dict[str, Any]] = {
    "time": 1_700_000_000,
    "USD": 20_000.0,
    "EUR": 62_000.0,
}


class _FixedGen:
    """Scripted fake model: answers the plan, then a canned respond.
    Records every prompt (the turn must still ride the MODEL path — the
    FIAT-003 intercept only stamps a currency, it never bypasses routing).
    """

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, prompt: str, grammar_text: str | None) -> str:
        del grammar_text
        self.prompts.append(prompt)
        return self._replies.pop(0) if self._replies else RESPOND_NOTED_JSON


def _world(state: dict[str, Any]):
    """Canonical balance/send world over the shared price endpoint with the
    oracle wired like PRODUCTION: the app's session-aware display-currency
    reader (per-ask one-shot > env > file > stored > default). The reader
    needs the store/session that ``_build_send_table`` builds, so the
    oracle's currency callable is late-bound through a box."""
    box: dict[str, Any] = {"currency": lambda: "usd"}
    table, store, _wallet, client, recorded, flow, session = _build_send_table(
        lambda rec: _send_chain_handler(
            rec,
            utxos_by_addr={derive_fixture_addresses(1)[0]: [SEND_UTXO]},
            state=state,
        ),
        make_price_oracle=lambda client_: PriceOracle(
            client_, ttl_s=60.0, currency=lambda: box["currency"]()
        ),
    )
    box["currency"] = app._display_currency_reader(Settings(), store, session)
    return {"table": table, "store": store, "client": client, "recorded": recorded,
            "flow": flow, "session": session, "state": state}


def _turn(world, line: str, reply: str = GET_BALANCE_JSON, gen: Any | None = None) -> list[str]:
    """One REAL REPL turn through ``_run_turn``; returns the output lines."""
    loop = AgentLoop(gen if gen is not None else _FixedGen([reply]), world["table"])
    lines: list[str] = []
    app._run_turn(
        loop, world["flow"], world["session"], line, lines.append,
        table=world["table"], store=world["store"],
    )
    return lines


def _price_fetches(world) -> int:
    return sum(1 for r in world["recorded"] if "/v1/prices" in str(r.url))


# =========================================================================
# 1. The repro: "what is my balance in Euros?" must answer in EUR
# =========================================================================


def test_euros_ask_answers_in_eur_and_untouched_the_setting() -> None:
    world = _world({"prices_payload": PRICES})
    try:
        gen = _FixedGen([GET_BALANCE_JSON])
        lines = _turn(world, "what is my balance in Euros?", gen=gen)
        assert "≈ 62.00 EUR · @ 62,000 EUR/BTC" in lines
        assert not any("$" in line for line in lines)  # no USD figure leaks in
        # The intercept NEVER bypasses the model (unlike the dispatch
        # intercepts): the utterance still rides routing, unchanged.
        assert gen.prompts and any("Euros" in p for p in gen.prompts)
        # One-shot retired with the turn; the SETTING was never written and
        # the ladder still answers the default.
        assert world["session"].fiat_ask_currency is None
        assert world["store"].get_setting(DISPLAY_CURRENCY_SETTING) is None
        assert app._display_currency_reader(Settings(), world["store"],
                                            world["session"])() == "usd"
    finally:
        world["client"].close()
        world["store"].close()


def test_one_shot_does_not_stick_and_no_cache_poisoning() -> None:
    """EUR ask then a plain (ladder=usd) ask: each line names its own
    currency; the tagged cache refetches across the switch (never re-tags
    or serves the other currency's figure)."""
    world = _world({"prices_payload": PRICES})
    try:
        assert "≈ 62.00 EUR · @ 62,000 EUR/BTC" in _turn(
            world, "what is my balance in euros?"
        )
        assert _price_fetches(world) == 1
        assert "≈ $20.00 · @ $20,000/BTC" in _turn(world, "what is my balance?")
        assert _price_fetches(world) == 2  # refetch, never a re-tagged EUR cache
        assert "≈ 62.00 EUR · @ 62,000 EUR/BTC" in _turn(world, "balance in EUR")
        assert _price_fetches(world) == 3  # and back — no cross-currency serve
    finally:
        world["client"].close()
        world["store"].close()


def test_one_shot_overrides_a_stored_setting_for_one_reply_only() -> None:
    """display_currency=eur (stored rung): a "in dollars" ask answers USD;
    the very next unmarked ask is EUR again — the setting never moved."""
    world = _world({"prices_payload": PRICES})
    try:
        world["store"].set_setting(DISPLAY_CURRENCY_SETTING, "eur")
        assert "≈ 62.00 EUR · @ 62,000 EUR/BTC" in _turn(world, "what is my balance?")
        assert "≈ $20.00 · @ $20,000/BTC" in _turn(world, "how much in dollars?")
        assert "≈ 62.00 EUR · @ 62,000 EUR/BTC" in _turn(world, "and my balance now?")
        assert world["store"].get_setting(DISPLAY_CURRENCY_SETTING) == "eur"
    finally:
        world["client"].close()
        world["store"].close()


def test_ambiguous_currency_words_ride_the_ladder() -> None:
    """Two currencies named = ambiguity = no override (default usd)."""
    world = _world({"prices_payload": PRICES})
    try:
        lines = _turn(world, "is my balance 241 usd or eur?")
        assert "≈ $20.00 · @ $20,000/BTC" in lines
        assert not any("EUR" in line for line in lines)
    finally:
        world["client"].close()
        world["store"].close()


# =========================================================================
# 2. The closed word table — accept/miss/ambiguity matrix
# =========================================================================


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # the ticket's closed table, every code…
        ("balance in eur", "eur"),
        ("balance in euro", "eur"),
        ("balance in euros", "eur"),
        ("what is my balance in Euros?", "eur"),  # case-insensitive
        ("balance in USD", "usd"),
        ("show dollars", "usd"),
        ("in dollar terms", "usd"),
        ("gbp please", "gbp"),
        ("in pounds", "gbp"),
        ("one pound", "gbp"),
        ("cad", "cad"),
        ("chf", "chf"),
        ("aud", "aud"),
        ("jpy", "jpy"),
        ("in yen", "jpy"),
        # edge punctuation stripped (the established token rule)
        ("euros?", "eur"),
        ("balance, USD.", "usd"),
        # same currency twice is NOT ambiguous
        ("usd or dollars?", "usd"),
        ("balance in euros (EUR)", "eur"),
        # misses = no override, ride the ladder
        ("what is my balance?", None),
        ("how much bitcoin do I have", None),
        ("balance in eurozone terms", None),  # token equality, not substring
        ("convert my usdt", None),
        # TCK-FIAT-004 deliberately REVERSED this expectation: a symbol is
        # now in the closed set (symbol + digits, or a lone symbol token).
        ("€20", "eur"),
        ("send $45 to bc1q9t3d", "usd"),
        ("45¥ please", "jpy"),
        ("$5 and £4", None),  # two symbols named = ambiguity, ride ladder
        # a currency word INSIDE an address-like token never matches
        ("send to bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", None),
        ("send to bc1qexusdqq5xw7kv8f3t4", None),
        # ambiguity: two different currencies named
        ("usd or eur", None),
        ("dollars in euros please", None),
        ("gbp and jpy", None),
        ("cad or aud", None),
    ],
)
def test_word_table(line: str, expected: str | None) -> None:
    assert app._detect_fiat_ask_currency(line) == expected


# =========================================================================
# 3. The tx-card Fee line — fiat parenthetical, verbatim, fail-closed
# =========================================================================


_FEE_CARD: Final[dict[str, Any]] = {
    "tx_ref": "t1",
    "recipient": "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
    "amount_sats": 60_000,
    "fee_sats": 141,
    "fee_rate_display": "1",
    "vsize": 141,
    "change_sats": 39_718,
    "inputs_count": 1,
    "usd_cents": 50,
    "btc_usd": 77_291.0,
    "fee_fiat_minor": 55,
    "fee_target": None,
    "fee_target_defaulted": False,
    "mixed": False,
    "folded_count": 0,
    "expires_in_s": 300,
}


def _fee_line(result: dict[str, Any]) -> str:
    lines: list[str] = []
    app._print_brief_card(result, lines.append)
    return next(line for line in lines if line.startswith("Fee:"))


def test_fee_line_renders_the_usd_parenthetical() -> None:
    # The MW-17 note's exact target shape.
    assert (
        _fee_line(_FEE_CARD) == "Fee: 141 sats · 1 sat/vB × 141 vB (≈ $0.55)"
    )


def test_fee_line_labels_a_tagged_currency() -> None:
    card = {
        **_FEE_CARD,
        "usd_cents": None,
        "btc_usd": None,
        "fiat_total_minor": 3_720,
        "fiat_currency": "eur",
        "fiat_per_btc": 62_000.0,
        "fee_fiat_minor": 50,
    }
    assert _fee_line(card) == "Fee: 141 sats · 1 sat/vB × 141 vB (≈ 0.50 EUR)"


def test_fee_line_absent_rate_is_sats_only_as_today() -> None:
    card = {k: v for k, v in _FEE_CARD.items()
            if k not in ("fee_fiat_minor", "usd_cents", "btc_usd")}
    assert _fee_line(card) == "Fee: 141 sats · 1 sat/vB × 141 vB"


def test_fee_line_forged_non_int_never_renders() -> None:
    for forged in (True, "55", None, 55.0):
        card = {**_FEE_CARD, "fee_fiat_minor": forged}
        assert "≈" not in _fee_line(card)


def test_fee_line_zero_floor_still_renders_honestly() -> None:
    card = {**_FEE_CARD, "fee_fiat_minor": 0}
    assert _fee_line(card) == "Fee: 141 sats · 1 sat/vB × 141 vB (≈ $0.00)"


def test_fee_line_stale_rate_mirrors_the_pay_line_marker() -> None:
    card = {**_FEE_CARD, "rate_stale": True, "rate_age_s": 1_000}
    assert _fee_line(card) == "Fee: 141 sats · 1 sat/vB × 141 vB (≈ $0.55 · stale)"


def test_fee_line_target_and_eta_segments_keep_their_order() -> None:
    card = {**_FEE_CARD, "fee_target": "slow", "eta_wording": "within ~1 hour"}
    assert _fee_line(card) == (
        "Fee: 141 sats · 1 sat/vB × 141 vB (≈ $0.55) · slow — ETA within ~1 hour"
    )


# =========================================================================
# 4. Handler level — fee_fiat_minor rides the SAME rate as the Pay line
# =========================================================================


def test_create_tx_handler_supplies_the_fee_fiat_from_the_rate() -> None:
    world = _world({"prices_payload": PRICES})
    try:
        created = world["table"][IntentName.CREATE_TX](
            validate_payload(_create_tx_envelope_json())
        )
        assert created.get("error") is None, created
        # canonical fixture: 141 vB at the medium rung (2 sat/vB) = 282 sats.
        assert created["fee_sats"] == 282
        # 282 sats @ 20_000 USD/BTC = 5.64 cents → floors to 5 (the oracle's
        # documented ROUND_FLOOR policy; integer math agrees exactly).
        assert created["fee_fiat_minor"] == created["fee_sats"] * 20_000 * 100 // 10**8
        assert created["fee_fiat_minor"] == 5
    finally:
        world["client"].close()
        world["store"].close()


def test_create_tx_price_outage_leaves_the_fee_line_sats_only() -> None:
    world = _world({"prices_payload": PRICES, "prices_fail": True})
    try:
        created = world["table"][IntentName.CREATE_TX](
            validate_payload(_create_tx_envelope_json())
        )
        assert created.get("error") is None  # never an error, never fabricated
        assert "fee_fiat_minor" not in created
        assert "usd_cents" not in created or created["usd_cents"] is None
        assert "≈" not in _fee_line(created)
    finally:
        world["client"].close()
        world["store"].close()


def test_create_tx_card_answers_a_currency_ask_turn_in_that_currency() -> None:
    """The FULL one-shot path on a card turn: the user's words name euros,
    the card (Pay AND Fee lines) answers in EUR, the setting stays unset."""
    world = _world({"prices_payload": PRICES})
    try:
        loop = AgentLoop(_send_generate(world["flow"], ["create"]), world["table"])
        lines: list[str] = []
        app._run_turn(
            loop, world["flow"], world["session"],
            "send to my brother in euros", lines.append,
            table=world["table"], store=world["store"],
        )
        assert "Pay: 60,000 sats (37.20 EUR · @ 62,000 EUR/BTC)" in lines
        assert (
            "Fee: 282 sats · 2 sat/vB × 141 vB (≈ 0.17 EUR) · medium "
            "— ETA ~60-70 min — estimate only, not a guarantee" in lines
        )
        assert world["session"].fiat_ask_currency is None
        assert world["store"].get_setting(DISPLAY_CURRENCY_SETTING) is None
    finally:
        world["client"].close()
        world["store"].close()


# =========================================================================
# 5. Closed-protocol pins — no schema/registry churn
# =========================================================================


def test_no_envelope_ever_carries_a_currency() -> None:
    """The design contract: per-ask currency is engine-side state; the
    params models keep their closed shape (the model cannot author a code)."""
    for params_cls in (GetBalanceParams, CreateTxParams):
        names = set(params_cls.model_fields)
        assert not any(
            "currency" in name or "fiat" in name for name in names
        ), params_cls
    assert set(GetBalanceParams.model_fields) == {"address_number"}


def test_registry_unchanged() -> None:
    assert len(list(IntentName)) == 15


def test_prompt_rides_untouched() -> None:
    """FIAT-002's routing line already teaches euro phrasings → this ticket
    ships NO prompt change (the eval gate stays on the unchanged text)."""
    text = build_system_prompt()
    assert "Fiat asks in any currency wording" in text
    assert "euros" in text  # the FIAT-002 mapping line, byte-intact

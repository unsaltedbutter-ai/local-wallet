"""Tests for session context summarization (R13) and transcript management
(OQ14) — TCK-P5-002.

All hermetic: no model file, no network, no real inference. The summary is
built deterministically from dispatcher-owned state (intent names + integer
counters), and the export/scrub paths are plain functions/commands.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.agent.loop import (
    MAX_HISTORY_TURNS,
    MAX_RECENT_TURNS,
    AgentLoop,
)
from localwallet.agent.session import MAX_SUMMARY_CHARS, redact_transcript

# --------------------------------------------------------------------- helpers


def _env_json(intent: str) -> str:
    """A canonical closed-intent envelope JSON document (model-output shape).

    Realistic params: create/confirm carry ``amount_sats``/``amount_usd``,
    and ``tx_status`` carries a 64-hex ``txid`` — exactly the shape the
    security review flagged (raw numeric amounts and txids survive textual
    redaction). Export/redaction tests must exercise these fields.
    """
    params: dict[str, object] = {}
    if intent in {"create_tx", "confirm_tx"}:
        params["amount_sats"] = 50000
        params["amount_usd"] = 25.5
    if intent == "tx_status":
        params["txid"] = "a" * 64
    return json.dumps({"v": 0, "intent": intent, "params": params})


def _build_loop() -> AgentLoop:
    """An AgentLoop whose turns only record history/summary (no dispatch)."""
    return AgentLoop(lambda _p, _g: _env_json("respond"), {})


# A value-rich user line to prove the retained summary is value-free.
_VALUE_LINE = "send 50000 sats to tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"
_XPUB = "vpub5ZNoPfhBgS1YWC2UPZdBu5D9tMZmbncYQeC6ZL8cEMpXePwmdgqGBqk8DpnfXvqfhjw1Z9gFu9cY1KP4CwvAyGqZLPkBrEAnY2h1HgvuxXLZt"
_COOKIE_PATH = "/Users/me/.bitcoin/testnet4/.cookie"

_FLOW = {"create_tx", "confirm_tx", "sign_tx", "broadcast_tx"}


def _fill(loop: AgentLoop, turns: int, intents: list[str]) -> None:
    """Add ``turns`` turns cycling through ``intents`` (one per turn)."""
    for i in range(turns):
        intent = intents[i % len(intents)]
        user = f"user line {i}" if "tx" not in intent else _VALUE_LINE
        loop.add_turn(user, _env_json(intent))


# ----------------------------------------------------- R13: summarization/budget


class TestSummarization:
    def test_short_session_no_summary_all_recent(self) -> None:
        loop = _build_loop()
        loop.add_turn("hi", _env_json("respond"))
        loop.add_turn("balance?", _env_json("get_balance"))
        assert loop.session_summary.total_turns == 0
        assert len(loop.history) == 2
        assert "SESSION SUMMARY" not in loop.context_prompt("next", {})

    def test_recent_cap_folds_older_turns_into_summary(self) -> None:
        loop = _build_loop()
        total = MAX_HISTORY_TURNS + 5
        _fill(loop, total, ["respond", "get_balance"])
        assert len(loop.history) == MAX_HISTORY_TURNS
        assert loop.session_summary.total_turns == 5
        # The recent window holds the newest turns verbatim.
        assert loop.history[-1].user_text == f"user line {total - 1}"

    def test_summary_counts_intents_and_flow_steps(self) -> None:
        loop = _build_loop()
        intents = ["respond", "get_balance", "create_tx", "confirm_tx", "get_history"]
        total = MAX_HISTORY_TURNS + len(intents)
        _fill(loop, total, intents)
        summary = loop.session_summary
        assert summary.total_turns == total - MAX_HISTORY_TURNS
        # Every folded flow-step intent contributes to flow_steps.
        flow_count = sum(
            c for k, c in summary.intent_counts.items() if k in _FLOW
        )
        assert summary.flow_steps == flow_count
        # The folded window (the 5 oldest lines) is exactly intent[0:5].
        folded_intents = intents[: total - MAX_HISTORY_TURNS]
        assert summary.intent_counts == {k: folded_intents.count(k) for k in folded_intents}

    def test_budget_long_multitopic_session_stays_bounded(self) -> None:
        """AC (R13, simulated): a long multi-topic session stays within a
        pinned context budget while the summary + recent turns stay correct."""
        intents = [
            "respond", "get_balance", "get_history", "get_utxos",
            "new_address", "create_tx", "confirm_tx", "sign_tx",
            "broadcast_tx", "tx_status", "node_status", "clarify",
        ]
        loop = _build_loop()
        _fill(loop, 300, intents)  # ~a long 30-min multi-topic session
        prompt = loop.context_prompt("what's next?", {})
        # Pinned budget: system + FACTS + summary(≤400) + recent(≤20) + user
        # turn stays comfortably inside the ADR-0006 ≤8K-token budget. 300
        # turns never push it past this character bound.
        assert len(prompt) < 9000
        assert "SESSION SUMMARY" in prompt
        assert "CONVERSATION SO FAR" in prompt
        # The recent window holds the last 20 turns verbatim.
        assert loop.history[0].user_text == f"user line {300 - MAX_RECENT_TURNS}"

    def test_summary_value_free_after_long_session(self) -> None:
        """PRIVACY (R13): the SESSION SUMMARY BLOCK is value-free — no
        addresses, amounts, or xpubs. Money values appear only in verbatim
        recent turns / per-turn FACTS, never in the structured summary."""
        from localwallet.agent.session import render_summary

        loop = _build_loop()
        _fill(loop, 150, ["respond", "get_balance", "create_tx", "confirm_tx"])
        assert loop.session_summary.total_turns == 150 - MAX_HISTORY_TURNS
        block = render_summary(loop.session_summary)
        for needle in ("tb1", "sats", "vpub", "xpub", "50000"):
            assert needle not in block

    def test_record_event_counters_are_value_free(self) -> None:
        loop = _build_loop()
        loop.record_event("watch_events", 3)
        loop.record_event("watch_events", 2)
        assert loop.session_summary.extras["watch_events"] == 5
        assert "watch_events: 5" in loop.context_prompt("hi", {})


# ------------------------------------------------- OQ14: export / redaction


class TestRedaction:
    def test_redacts_addresses_amounts_keys_cookie_paths(self) -> None:
        text = (
            f"pay {_VALUE_LINE} using key {_XPUB} cookie {_COOKIE_PATH}"
        )
        out = redact_transcript(text)
        assert "<addr>" in out
        assert "<amount>" in out
        assert "<key>" in out
        assert "<cookie-path>" in out
        assert "tb1" not in out
        assert "sats" not in out
        assert "vpub" not in out
        assert ".cookie" not in out

    def test_plain_text_unchanged(self) -> None:
        assert redact_transcript("how much do I have?") == "how much do I have?"

    def test_redacts_uppercase_bech32_address(self) -> None:
        # BIP-173 permits all-uppercase bech32; it must not slip through.
        text = "pay to TB1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KXPJZSX now"
        out = redact_transcript(text)
        assert "<addr>" in out
        assert "TB1" not in out

    def test_redacts_btc_denominated_amount(self) -> None:
        out = redact_transcript("the fee is 0.005 BTC please")
        assert "<amount>" in out
        assert "0.005" not in out
        assert "BTC" not in out

    def test_redacts_satoshis_amount(self) -> None:
        out = redact_transcript("send 5000 satoshis to them")
        assert "<amount>" in out
        assert "5000" not in out
        assert "satoshis" not in out

    # -- TCK-SEC-001: txids, JSON-keyed amounts, BIP39-shaped seeds ------

    def test_redacts_realistic_envelope_json_amounts(self) -> None:
        # create_tx envelope carries raw numeric amounts in canonical JSON —
        # these must be redacted even though no textual "<n> sats" form exists.
        env = _env_json("create_tx")
        out = redact_transcript(env)
        assert "50000" not in out
        assert "25.5" not in out
        assert "<amount>" in out

    def test_redacts_realistic_envelope_txid(self) -> None:
        # tx_status-style 64-lowercase-hex txid survives without a pattern.
        env = _env_json("tx_status")
        txid = "a" * 64
        assert txid in env
        out = redact_transcript(env)
        assert txid not in out
        assert "<txid>" in out

    def test_redacts_64_hex_txid_in_text(self) -> None:
        txid = "9" * 64  # no '0' digit: would false-match the legacy-address pattern
        out = redact_transcript(f"broadcast {txid} succeeded")
        assert "<txid>" in out
        assert txid not in out

    def test_redacts_12_word_mnemonic(self) -> None:
        seed = " ".join(["bacon"] * 12)  # canonical test phrase (bacon x12)
        out = redact_transcript(f"my backup phrase: {seed}")
        assert "<seed>" in out
        assert "bacon" not in out

    def test_redacts_24_word_mnemonic(self) -> None:
        seed = " ".join(["zoo"] * 24)
        out = redact_transcript(f"wallet seed: {seed}")
        assert "<seed>" in out
        assert "zoo" not in out

    def test_normal_sentence_with_digits_not_wholesale_destroyed(self) -> None:
        # Only pattern-matched spans are redacted; ordinary digits survive.
        out = redact_transcript("I retried 2 times and 3 errors came up")
        assert out == "I retried 2 times and 3 errors came up"

    def test_redacts_all_existing_classes_together(self) -> None:
        text = (
            f"pay {_VALUE_LINE} using key {_XPUB} cookie {_COOKIE_PATH}"
            f" tx {'b' * 64} seed: {' '.join(['cherry'] * 12)}"
        )
        out = redact_transcript(text)
        for token in ("<addr>", "<amount>", "<key>", "<cookie-path>", "<txid>", "<seed>"):
            assert token in out
        for needle in ("tb1", "sats", "vpub", ".cookie", "b" * 64, "cherry"):
            assert needle not in out


class TestExport:
    def test_export_round_trip_redactions_present(self, tmp_path: Path) -> None:
        loop = _build_loop()
        loop.add_turn(_VALUE_LINE, _env_json("create_tx"))
        loop.add_turn(_VALUE_LINE, _env_json("confirm_tx"))
        path = tmp_path / "session.txt"
        lines = loop.export_transcript(path)
        assert lines > 0
        text = path.read_text(encoding="utf-8")
        # Value-free export: no addresses, amounts, or xpubs.
        assert "tb1" not in text
        assert "50000" not in text
        assert "sats" not in text
        assert "<addr>" in text
        assert "<amount>" in text
        # Structure present.
        assert "local-wallet session export" in text
        assert "generated (UTC)" in text

    def test_export_summary_and_recent_turns_included(self, tmp_path: Path) -> None:
        loop = _build_loop()
        _fill(loop, MAX_HISTORY_TURNS + 2, ["respond", "get_balance"])
        path = tmp_path / "t.txt"
        loop.export_transcript(path)
        text = path.read_text(encoding="utf-8")
        assert "SESSION SUMMARY" in text
        assert "RECENT TURNS" in text
        assert "turns summarized: 2" in text


class TestScrub:
    def test_scrub_clears_history_and_summary(self) -> None:
        loop = _build_loop()
        for i in range(MAX_HISTORY_TURNS + 3):
            loop.add_turn(f"u{i}", _env_json("get_balance"))
        assert loop.session_summary.total_turns == 3
        assert loop.history
        loop.scrub()
        assert loop.history == ()
        assert loop.session_summary.total_turns == 0
        prompt = loop.context_prompt("hi", {})
        assert "SESSION SUMMARY" not in prompt
        assert "CONVERSATION SO FAR" not in prompt


# ------------------------------------------------------------------ sanity


def test_module_constants_aligned():
    # loop re-exports the session budget so both consumers agree.
    assert MAX_RECENT_TURNS == MAX_HISTORY_TURNS
    assert MAX_SUMMARY_CHARS == 400

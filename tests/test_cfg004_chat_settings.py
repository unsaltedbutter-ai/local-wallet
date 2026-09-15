"""TCK-CFG-004 — chat-managed settings (config.json WRITE path + the
deterministic pre-model settings handlers).

Covers the ticket's done-when matrix:

* config.py write path: atomic (temp + os.replace; a failed replace leaves
  the original bytes and no temp litter), filtered to KNOWN keys so the
  fail-closed reader never rejects our own output, and a malformed
  PRE-EXISTING file is refused WITHOUT being touched (nothing merges onto
  corruption); the written file re-reads cleanly through the strict reader
  and ``Settings.from_env``;
* the handlers: READ answers the effective value + names the supplying rung
  (env / config file / stored / default) for every managed key and every
  alias phrasing; CHANGE validates fail-closed (the standing ladders' own
  bound tables: gap 1..1000, watch 0..86400, the COIN_SETTING_BOUNDS incl.
  the UTXO-002 min>=max cross-check), writes config.json AND deletes the
  key's stored row (the critique-pinned two-surface conflict rule), and
  narrates the rung situation honestly — including the env-shadows-change
  case, which must say the environment outranks the file;
* BTC→sats conversion is ENGINE-SIDE Decimal exactness (0.0005 BTC is
  50000 sats; a sub-sat fraction is refused) — the model is never consulted
  and never sees a consumed turn;
* refusals are value-free (the submitted value never rides back);
* unknown/unmanaged keys asked about in chat get the honest
  "not configurable" line — never a guess;
* the settings-pane surface names the file rung when a pane write would be
  silently shadowed by config.json (the ``data.note`` channel).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Final

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.config import Settings, read_config_file, write_config_file
from localwallet.protocol import IntentName
from localwallet.store import Store
from localwallet.tx.flow import TxFlow

_WORDS_RE: Final = re.compile(r"\W+")


MANAGED_ENV_VARS: Final[tuple[str, ...]] = (
    "LOCALWALLET_GAP_LIMIT",
    "LOCALWALLET_WATCH_INTERVAL_S",
    "LOCALWALLET_UTXO_TARGET_MIN_SATS",
    "LOCALWALLET_UTXO_TARGET_MAX_SATS",
    "LOCALWALLET_CONSOLIDATE_BELOW_SAT_VB",
)


@pytest.fixture(autouse=True)
def _ladder_env(tmp_path, monkeypatch):
    """Every test starts with a CLEAN ladder: no env rungs, and the config
    file pointed at this test's tmp dir (the repo-root config.json must
    never be created or read by this suite)."""
    for var in MANAGED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("LOCALWALLET_CONFIG_PATH", str(cfg))
    return cfg


# ----------------------------------------------------------- config write path


def test_write_creates_file_and_survives_the_strict_reader(tmp_path) -> None:
    path = write_config_file(
        {"gap_limit": "30", "watch_interval_s": 120.0},
        config_path=tmp_path / "config.json",
    )
    assert path.exists()
    # the fail-closed reader never rejects our own output:
    assert read_config_file(config_path=path) == {"gap_limit": "30", "watch_interval_s": 120.0}
    assert Settings.from_env(config_path=path).gap_limit == "30"
    assert Settings.from_env(config_path=path).watch_interval_s == 120.0


def test_write_merges_preserving_unrelated_known_keys(tmp_path) -> None:
    target = tmp_path / "config.json"
    write_config_file({"gap_limit": "30"}, config_path=target)
    write_config_file({"max_retries": 5}, config_path=target)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == {"gap_limit": "30", "max_retries": 5}


def test_write_filters_unknown_keys(tmp_path) -> None:
    target = tmp_path / "config.json"
    write_config_file(
        {"gap_limit": "30", "not_a_setting": 1}, config_path=target
    )
    assert json.loads(target.read_text(encoding="utf-8")) == {"gap_limit": "30"}


def test_write_wrong_type_refuses_value_free(tmp_path) -> None:
    target = tmp_path / "config.json"
    with pytest.raises(ValueError) as excinfo:
        write_config_file({"gap_limit": 30}, config_path=target)  # str field
    assert "gap_limit" in str(excinfo.value)
    assert "30" not in str(excinfo.value)  # value-free
    assert not target.exists()


def test_write_onto_malformed_existing_file_refuses_and_leaves_it_untouched(
    tmp_path,
) -> None:
    target = tmp_path / "config.json"
    target.write_text("{not json at all", encoding="utf-8")
    with pytest.raises(ValueError):
        write_config_file({"gap_limit": "30"}, config_path=target)
    assert target.read_text(encoding="utf-8") == "{not json at all"


def test_write_atomic_replace_failure_keeps_original_and_no_temp_litter(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "config.json"
    write_config_file({"gap_limit": "10"}, config_path=target)
    original = target.read_text(encoding="utf-8")

    def _boom(src: str, dst: str) -> None:
        raise OSError("replace refused")

    monkeypatch.setattr(app.os, "replace", _boom)
    with pytest.raises(OSError):
        write_config_file({"gap_limit": "99"}, config_path=target)
    monkeypatch.undo()
    assert target.read_text(encoding="utf-8") == original
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != target.name]
    assert leftovers == []  # temp cleaned


def test_config_file_path_precedence(tmp_path, monkeypatch) -> None:
    from localwallet.config import CONFIG_FILE_PATH, config_file_path

    explicit = tmp_path / "a.json"
    assert config_file_path(explicit) == explicit
    monkeypatch.setenv("LOCALWALLET_CONFIG_PATH", str(tmp_path / "b.json"))
    assert config_file_path() == tmp_path / "b.json"
    monkeypatch.delenv("LOCALWALLET_CONFIG_PATH")
    assert config_file_path() == CONFIG_FILE_PATH


# --------------------------------------------------------- store delete (typed)


def test_clear_setting_deletes_the_row_not_just_blanks_it() -> None:
    with Store.memory() as store:
        store.set_setting("gap_limit", "30")
        store.clear_setting("gap_limit")
        assert store.get_setting("gap_limit") is None  # no "" row left behind


def test_clear_setting_refuses_non_managed_key() -> None:
    # the generic DELETE is scoped to the managed keys with no other clear
    # path; any other key is refused with a value-free StoreError.
    from localwallet.store.db import StoreError

    with Store.memory() as store:
        store.set_setting("max_retries", "5")
        with pytest.raises(StoreError):
            store.clear_setting("max_retries")
        assert store.get_setting("max_retries") == "5"  # untouched


# --------------------------------------------------------------- READ answers


def _turn_lines(store: Store, *lines: str) -> list[str]:
    out: list[str] = []
    for line in lines:
        assert app._run_chat_settings_turn(store, line, out.append), line
    return out


def test_read_names_shipped_default_when_no_rung_is_set() -> None:
    with Store.memory() as store:
        (line,) = _turn_lines(store, "what is the gap limit?")
    assert "20" in line and "shipped default" in line


def test_read_names_stored_rung_from_the_pane_path() -> None:
    with Store.memory() as store:
        assert app._apply_setting_change(store, "gap_limit", "40") is None
        (line,) = _turn_lines(store, "what is the gap limit?")
    assert "40" in line and "stored" in line


def test_read_names_config_file_rung(_ladder_env) -> None:
    write_config_file({"gap_limit": "51"})
    with Store.memory() as store:
        (line,) = _turn_lines(store, "what is the gap limit?")
    assert "51" in line and "config.json" in line


def test_read_env_rung_beats_file_and_the_narration_says_so(
    _ladder_env, monkeypatch
) -> None:
    write_config_file({"gap_limit": "51"})
    monkeypatch.setenv("LOCALWALLET_GAP_LIMIT", "7")
    with Store.memory() as store:
        (line,) = _turn_lines(store, "what is the gap limit?")
    assert "7" in line
    assert "LOCALWALLET_GAP_LIMIT" in line
    assert "environment outranks" in line
    assert "51" not in line  # the shadowed file value is not quoted


@pytest.mark.parametrize(
    ("line", "needle"),
    [
        ("What is the smallest UTXO we will generate?", "100000"),
        ("What is the largest UTXO we will generate?", "10000000"),
        ("how often do you check for incoming transactions?", "60"),
        ("what's the consolidation fee ceiling?", "2"),
        ("how often should I expect a poll?", "60"),
        ("what is the gap limit?", "20"),
    ],
)
def test_read_alias_phrasings_answer_effective_value(line: str, needle: str) -> None:
    with Store.memory() as store:
        (out,) = _turn_lines(store, line)
    assert needle in out


def test_read_coin_keys_name_stored_rung_via_typed_accessor() -> None:
    with Store.memory() as store:
        store.set_coin_setting("utxo_target_max_sats", "20000000")
        (line,) = _turn_lines(store, "What is the largest UTXO we will generate?")
    assert "20000000" in line and "stored" in line


# ------------------------------------------------------------ CHANGE accept


def test_change_writes_file_deletes_stored_and_narrates_both(_ladder_env) -> None:
    with Store.memory() as store:
        assert app._apply_setting_change(store, "gap_limit", "40") is None
        (ack,) = _turn_lines(store, "set the gap limit to 30")
        assert store.get_setting("gap_limit") is None  # conflict rule: DELETED
        assert read_config_file() == {"gap_limit": "30"}
    assert "config.json" in ack
    assert "cleared" in ack  # the stored-delete is narrated, not silent
    assert "next launch" in ack  # honest live-apply situation (pre-CFG-005)


def test_change_utxo_min_sats_alias(_ladder_env) -> None:
    with Store.memory() as store:
        _turn_lines(store, "Don't create UTXOs smaller than 50000 sats.")
    assert read_config_file()["utxo_target_min_sats"] == "50000"


def test_change_btc_to_sats_is_engine_side_decimal(_ladder_env) -> None:
    with Store.memory() as store:
        (ack,) = _turn_lines(store, "No UTXOs below 0.0005 BTC.")
    assert read_config_file()["utxo_target_min_sats"] == "50000"
    assert "50000 sats" in ack  # the engine's own conversion, quoted verbatim


def test_change_watch_interval_minutes_conversion(_ladder_env) -> None:
    with Store.memory() as store:
        _turn_lines(store, "set the watch interval to 2 minutes")
    assert read_config_file()["watch_interval_s"] == 120.0  # float field type


def test_change_watch_interval_zero_off_hatch(_ladder_env) -> None:
    with Store.memory() as store:
        _turn_lines(store, "set the watch interval to 0 seconds")
    assert read_config_file()["watch_interval_s"] == 0.0


def test_change_consolidate_below_sat_vb_shape(_ladder_env) -> None:
    # the discriminate consolidate-below SETTINGS phrasing (explicit "fee
    # ceiling" wording, not the bare action): consumed and written.
    with Store.memory() as store:
        _turn_lines(store, "consolidate below 3 sat/vb as our fee ceiling")
    assert read_config_file()["consolidate_below_sat_vb"] == "3"


def test_change_consolidate_below_set_verb_shape(_ladder_env) -> None:
    # a set/change verb alongside the consolidate wording is a discriminator.
    with Store.memory() as store:
        _turn_lines(store, "consolidate, set the ceiling below 3 sat/vb")
    assert read_config_file()["consolidate_below_sat_vb"] == "3"


def test_change_coin_key_deletes_stored_row_via_typed_writer(_ladder_env) -> None:
    with Store.memory() as store:
        store.set_coin_setting("utxo_target_min_sats", "90000")
        _turn_lines(store, "set the smallest utxo target to 60000 sats")
        assert store.get_coin_setting("utxo_target_min_sats") is None
    assert read_config_file()["utxo_target_min_sats"] == "60000"


def test_change_env_shadow_is_narrated_honestly(
    _ladder_env, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALWALLET_GAP_LIMIT", "7")
    with Store.memory() as store:
        (ack,) = _turn_lines(store, "set the gap limit to 30")
    assert read_config_file() == {"gap_limit": "30"}  # file still written
    assert "LOCALWALLET_GAP_LIMIT" in ack and "outranks" in ack


# ------------------------------------------------------------ CHANGE refuse


@pytest.mark.parametrize(
    ("line", "fragment"),
    [
        ("set the gap limit to 0", "between 1 and 1000"),
        ("set the gap limit to 1001", "between 1 and 1000"),
        ("set the watch interval to 86401", "between 0 and 86400"),
        ("set the consolidation fee ceiling to 101", "between 1 and 100"),
        ("set the smallest utxo target to 545 sats", "between 546 and"),
        ("set the largest utxo target to 21000000000001 sats", "must be a whole number between"),
    ],
)
def test_change_out_of_bounds_refused_value_free(line: str, fragment: str, _ladder_env) -> None:
    with Store.memory() as store:
        (refusal,) = _turn_lines(store, line)
    assert fragment in refusal
    assert not Path(os.environ["LOCALWALLET_CONFIG_PATH"]).exists()
    # value-free: the offending submitted value never rides back (token-wise
    # — the bounds themselves are legitimately quoted digits)
    (bad,) = app._CHAT_NUM_RE.findall(line.lower())
    assert bad not in _WORDS_RE.split(refusal)


def test_change_min_not_below_max_cross_refused(_ladder_env) -> None:
    # default max is 10_000_000 — a 20M min must be refused against the
    # PAIR as it will actually resolve (UTXO-002 rule).
    with Store.memory() as store:
        (refusal,) = _turn_lines(
            store, "Don't create UTXOs smaller than 20000000 sats."
        )
    assert "below the largest" in refusal
    assert not Path(os.environ["LOCALWALLET_CONFIG_PATH"]).exists()


def test_change_max_below_stored_min_cross_refused(_ladder_env) -> None:
    with Store.memory() as store:
        store.set_coin_setting("utxo_target_min_sats", "9000000")
        (refusal,) = _turn_lines(
            store, "set the largest utxo target to 8000000 sats"
        )
    assert "below the largest" in refusal


def test_change_btc_sub_sat_fraction_refused(_ladder_env) -> None:
    with Store.memory() as store:
        (refusal,) = _turn_lines(store, "No UTXOs below 0.000000004 BTC.")
    assert "whole satoshis" in refusal
    assert not Path(os.environ["LOCALWALLET_CONFIG_PATH"]).exists()


@pytest.mark.parametrize(
    "line",
    [
        "set the gap limit",  # command shape, no value
        "set the gap limit to 2.5",  # not a whole number
        "set the gap limit to 10 20",  # two numbers
        "set the gap limit to 0.0005 btc",  # wrong unit family
    ],
)
def test_change_bad_value_shape_refused_one_number_line(line: str, _ladder_env) -> None:
    with Store.memory() as store:
        (refusal,) = _turn_lines(store, line)
    assert "whole number" in refusal
    assert not Path(os.environ["LOCALWALLET_CONFIG_PATH"]).exists()


def test_change_onto_malformed_file_refuses_and_leaves_it_untouched(
    _ladder_env,
) -> None:
    _ladder_env.write_text("{ broken", encoding="utf-8")
    with Store.memory() as store:
        (refusal,) = _turn_lines(store, "set the gap limit to 30")
    assert "not valid" in refusal and "nothing was stored" in refusal
    assert _ladder_env.read_text(encoding="utf-8") == "{ broken"


def test_read_with_malformed_file_answers_honestly_not_a_guess(
    _ladder_env,
) -> None:
    _ladder_env.write_text("{ broken", encoding="utf-8")
    with Store.memory() as store:
        (line,) = _turn_lines(store, "what is the gap limit?")
    assert "not valid" in line


# ------------------------------------------------- non-matching / unmanaged


@pytest.mark.parametrize(
    "line",
    [
        "set the gap limit to 30 and the watch interval to 60",  # two keys
        "what is my balance?",  # not a settings word at all
        "the gap between scans feels too small",  # prose mention
        "why is my gap limit 20 too low for me?",  # number + no command shape
    ],
)
def test_ambiguous_or_foreign_lines_are_not_consumed(line: str) -> None:
    with Store.memory() as store:
        assert not app._run_chat_settings_turn(store, line, lambda _s: None)


@pytest.mark.parametrize(
    "line",
    [
        # WALLET queries about ACTUAL coins (not the configured target) fall
        # through to the model unchanged — never answered as a settings value.
        "what is my smallest utxo?",
        "show me my largest utxo",
        "how often do you check my balance?",
        # a bare consolidation ACTION request is not a settings change.
        "consolidate my utxos below 3 sat/vb",
        "consolidate below 3 sat/vb",
    ],
)
def test_wallet_query_phrasing_falls_through_unchanged(line: str) -> None:
    with Store.memory() as store:
        assert not app._run_chat_settings_turn(store, line, lambda _s: None)


@pytest.mark.parametrize(
    "line",
    [
        "what is max_retries?",
        "set display_currency to eur",
        "what is tls_verify?",
    ],
)
def test_unmanaged_keys_get_the_honest_not_configurable_line(line: str) -> None:
    with Store.memory() as store:
        (refusal,) = _turn_lines(store, line)
    assert "not configurable from chat" in refusal


# ------------------------------------------------- pre-model wiring (a miss =
#   the model; a consume = the model NEVER sees the line)


class _RecordingGen:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, prompt: str, grammar_text: str | None) -> str:
        del grammar_text
        self.prompts.append(prompt)
        return json.dumps({"v": 0, "intent": "respond", "params": {"text": "ok."}})


def _run_turn(store: Store, line: str, gen: _RecordingGen) -> list[str]:
    table: dict[str, Any] = {IntentName.RESPOND: lambda env: {"text": env.params.text}}
    loop = AgentLoop(gen, table)
    out: list[str] = []
    app._run_turn(
        loop, TxFlow(), app.SendSession(), line, out.append, table=table, store=store
    )
    return out


def test_consumed_settings_turn_never_reaches_the_model(_ladder_env) -> None:
    with Store.memory() as store:
        gen = _RecordingGen()
        out = _run_turn(store, "Don't create UTXOs smaller than 50000 sats.", gen)
        assert out and "50000" in out[0]
        assert gen.prompts == []  # zero model contact on the consumed change
        assert read_config_file()["utxo_target_min_sats"] == "50000"


def test_unrecognized_line_still_reaches_the_model_unchanged() -> None:
    with Store.memory() as store:
        gen = _RecordingGen()
        _run_turn(store, "how many utxos do I have?", gen)
        assert gen.prompts  # ordinary pipeline, untouched


# ------------------------------------------- pane surface: file-shadow honesty


def test_pane_write_shadowed_by_file_carries_the_note(_ladder_env) -> None:
    write_config_file({"gap_limit": "30"})
    with Store.memory() as store:
        reply = app.handle_settings_request(store, "gap_limit", "45")
    assert reply["status"] == "applied"
    assert reply.get("note") == app.CONFIG_SHADOW_NOTE


def test_pane_write_without_a_file_rung_carries_no_shadow_note(_ladder_env) -> None:
    with Store.memory() as store:
        reply = app.handle_settings_request(store, "gap_limit", "45")
    assert reply["status"] == "applied"
    assert "note" not in reply

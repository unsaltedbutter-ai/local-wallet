"""TCK-ONB-002: chain backend selection persistence + precedence.

Covers the store/config layer only (ADR-0023 decision 3): the pure
:func:`~localwallet.config.resolve_chain_base_url` precedence
(env > stored > None -> public default) and the typed ``Store`` accessors
that persist the first-run choice. Wiring the stored rung into app startup
is TCK-ONB-003's job and deliberately not exercised here.
"""

from __future__ import annotations

import json

import pytest

from localwallet.chain.config import ChainConfig
from localwallet.config import Settings, resolve_chain_base_url
from localwallet.store.db import Store, StoreError

ENV_URL = "https://env-node.local:3006/api"
STORED_URL = "https://my-node.home:3006/api"
FILE_URL = "https://file-node.local:3006/api"


# ---------------------------------------------------------- precedence matrix


@pytest.mark.parametrize(
    ("env_value", "stored_value", "expected"),
    [
        (ENV_URL, STORED_URL, ENV_URL),  # env always wins
        (ENV_URL, None, ENV_URL),
        (None, STORED_URL, STORED_URL),
        (None, None, None),  # neither rung -> caller keeps public default
        ("", STORED_URL, STORED_URL),  # empty env rung = unset
        (ENV_URL, "", ENV_URL),  # empty stored rung = unset
        ("", "", None),
        ("", None, None),
        (None, "", None),
        ("   ", STORED_URL, STORED_URL),  # whitespace-only rung = unset
        (ENV_URL, "   ", ENV_URL),
        (" \t ", "  ", None),
    ],
)
def test_precedence_env_over_stored_over_unset(
    env_value: str | None, stored_value: str | None, expected: str | None
) -> None:
    assert resolve_chain_base_url(env_value, stored_value) == expected


def test_resolve_returns_stripped_never_invents_a_default() -> None:
    assert resolve_chain_base_url(f"  {ENV_URL}  ", None) == ENV_URL
    # None = "no rung set"; the default stays Settings' job, untouched.
    assert resolve_chain_base_url(None, None) is None
    assert Settings().chain_base_url == ""
    assert Settings().esplora_base_url == "https://mempool.space/api"


def test_env_parsing_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    # from_env keeps populating chain_base_url exactly as before ADR-0023;
    # resolve_chain_base_url is additive, not a second env reader.
    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", ENV_URL)
    assert Settings.from_env().chain_base_url == ENV_URL
    monkeypatch.delenv("LOCALWALLET_CHAIN_BASE_URL")
    assert Settings.from_env().chain_base_url == ""


# ---------------------------------------------------------------- store layer


def test_chain_base_url_roundtrip_overwrite_and_clear() -> None:
    with Store.memory() as store:
        assert store.get_chain_base_url() is None  # unset rung
        store.set_chain_base_url(STORED_URL)
        assert store.get_chain_base_url() == STORED_URL
        store.set_chain_base_url(ENV_URL)  # overwrite
        assert store.get_chain_base_url() == ENV_URL
        store.set_chain_base_url("")  # explicit empty string clears the choice
        assert store.get_chain_base_url() is None
        # cleared means the ROW is gone, not an empty value shadowing unset.
        assert store.get_setting("chain_base_url") is None


def test_chain_base_url_strips_on_write() -> None:
    with Store.memory() as store:
        store.set_chain_base_url(f"  {STORED_URL}  ")
        assert store.get_chain_base_url() == STORED_URL


def test_chain_base_url_persists_across_reopen(tmp_path) -> None:
    path = tmp_path / "wallet.db"
    with Store(path) as store:
        store.set_chain_base_url(STORED_URL)
    with Store(path) as store:
        assert store.get_chain_base_url() == STORED_URL


@pytest.mark.parametrize(
    "bad",
    [
        "ftp://node.local",
        "not a url",
        "/relative/path",
        "https://",  # no host
        "http://user:pw@node.local/api",  # embedded credentials
        "http://node local/api",  # internal whitespace
        "   ",  # blank-but-set is refused, never a silent clear
    ],
)
def test_write_validation_fails_closed_and_value_free(bad: str) -> None:
    with Store.memory() as store:
        with pytest.raises(StoreError) as excinfo:
            store.set_chain_base_url(bad)
        assert bad not in str(excinfo.value)  # the rejected URL is never echoed
        assert store.get_chain_base_url() is None  # nothing landed on disk


# ------------------------------------------------------- single selection point


def test_resolution_flows_through_the_one_selection_point() -> None:
    # ADR-0018: ChainConfig.from_settings stays the ONLY backend selection.
    # The resolved value (either rung) rides Settings.chain_base_url; with no
    # rung set, the public default path is bit-identical to pre-ONB behavior.
    with Store.memory() as store:
        store.set_chain_base_url(STORED_URL)
        stored = store.get_chain_base_url()

    assert resolve_chain_base_url(None, stored) == STORED_URL
    assert resolve_chain_base_url(ENV_URL, stored) == ENV_URL
    assert resolve_chain_base_url(Settings().chain_base_url, None) is None

    assert ChainConfig.from_settings(Settings(chain_base_url=STORED_URL)).base_url == STORED_URL
    assert (
        ChainConfig.from_settings(Settings()).base_url == "https://mempool.space/api"
    )


# =============================================================================
# TCK-UTXO-002: coin-selection settings ladder (doc §2.3, ADR-0012 amendment)
# =============================================================================

from localwallet.config import (
    CONSOLIDATE_BELOW_SAT_VB_SETTING,
    UTXO_TARGET_MAX_SETTING,
    UTXO_TARGET_MIN_SETTING,
    CoinSelectionSettings,
    resolve_coin_selection_settings,
)

MIN = UTXO_TARGET_MIN_SETTING
MAX = UTXO_TARGET_MAX_SETTING
VB = CONSOLIDATE_BELOW_SAT_VB_SETTING


def test_defaults_ship_the_doc_values() -> None:
    got = resolve_coin_selection_settings({}, {})
    assert got == CoinSelectionSettings(100_000, 10_000_000, 2)


def test_precedence_env_over_stored_over_default() -> None:
    got = resolve_coin_selection_settings(
        {MIN: "7000"},
        {MIN: "9000", MAX: "30000"},
    )
    assert got.target_min_sats == 7_000  # env wins
    assert got.target_max_sats == 30_000  # stored wins over default
    assert got.consolidate_below_sat_vb == 2  # no rung -> shipped default


@pytest.mark.parametrize("blank", [None, "", "   ", "\t"])
def test_blank_rungs_are_unset_not_malformed(blank: str | None) -> None:
    env = {MIN: blank, MAX: blank, VB: blank}
    stored = {MIN: blank, MAX: blank, VB: blank}
    got = resolve_coin_selection_settings(env, stored)  # type: ignore[arg-type]
    assert got == CoinSelectionSettings(100_000, 10_000_000, 2)


def test_stripped_env_rung_is_accepted_stripped() -> None:
    got = resolve_coin_selection_settings({MIN: "  7000  "}, {})
    assert got.target_min_sats == 7_000


@pytest.mark.parametrize("bad", ["abc", "0x10", "1e5", "-5", "+5", "1 000",
                                 "1_000", "١٢٣", "5.0", "٢٠٢٦"])
def test_malformed_refuses_startup_value_free(bad: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        resolve_coin_selection_settings({MAX: bad}, {})
    message = str(excinfo.value)
    assert "LOCALWALLET_UTXO_TARGET_MAX_SATS" in message
    assert bad not in message  # nothing the user typed is echoed


@pytest.mark.parametrize(("key", "bad"), [
    (MIN, "545"), (MIN, "100000001"),
    (MAX, "545"), (MAX, "21000000000001"),
    (VB, "0"), (VB, "101"),
])
def test_out_of_bounds_refused(key: str, bad: str) -> None:
    with pytest.raises(ValueError):
        resolve_coin_selection_settings({key: bad}, {})


def test_min_not_below_max_refused_across_rungs() -> None:
    # env min crossing a stored max: the cross-check spans rungs (a corrupt
    # mix never silently flips policy — ADR-0009 applies verbatim).
    with pytest.raises(ValueError) as excinfo:
        resolve_coin_selection_settings({MIN: "30000"}, {MAX: "30000"})
    assert "utxo_target_min_sats" in str(excinfo.value)
    assert "30000" not in str(excinfo.value)
    with pytest.raises(ValueError):
        resolve_coin_selection_settings({MIN: "200000"}, {MAX: "100000"})
    # default max with an env min above it also refuses:
    with pytest.raises(ValueError):
        resolve_coin_selection_settings({MIN: "50000000"}, {})


def test_from_env_reads_the_three_new_rungs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALWALLET_UTXO_TARGET_MIN_SATS", "12345")
    monkeypatch.setenv("LOCALWALLET_UTXO_TARGET_MAX_SATS", "678901")
    monkeypatch.setenv("LOCALWALLET_CONSOLIDATE_BELOW_SAT_VB", "1")
    settings = Settings.from_env()
    assert settings.utxo_target_min_sats == "12345"
    assert settings.utxo_target_max_sats == "678901"
    assert settings.consolidate_below_sat_vb == "1"
    # from_env does not parse (gap_limit precedent): the strings ride raw.
    got = resolve_coin_selection_settings(
        {MIN: settings.utxo_target_min_sats,
         MAX: settings.utxo_target_max_sats,
         VB: settings.consolidate_below_sat_vb},
        {MIN: "999999"},  # stored loses to env
    )
    assert got == CoinSelectionSettings(12_345, 678_901, 1)


def test_ladder_consumes_the_store_settings_api() -> None:
    # The stored rung is read through the generic settings API (get_setting)
    # and written through the typed pair; resolution then matches what was
    # written — the ladder's stored rung is the DB, not a parallel channel.
    with Store.memory() as store:
        store.set_coin_setting(MIN, "20000")
        store.set_coin_setting(MAX, "500000")
        store.set_coin_setting(VB, "1")
        got = resolve_coin_selection_settings(
            {},
            {key: store.get_setting(key) for key in (MIN, MAX, VB)},
        )
    assert got == CoinSelectionSettings(20_000, 500_000, 1)


# =============================================================================
# TCK-CFG-002: config-file rung (env > file > stored > default)
# =============================================================================

from pathlib import Path


def _write_config(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_raw(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.json"
    path.write_text(text, encoding="utf-8")
    return path


def test_config_file_absent_is_zero_change(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.json"
    got = Settings.from_env(config_path=missing)
    assert got.gap_limit == ""
    assert got.price_enabled is True
    assert got.request_timeout_s == 10.0
    assert got.chain_base_url == ""


def test_file_populates_gap_limit_and_other_scalars(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        {"gap_limit": "42", "price_enabled": False, "request_timeout_s": 5.5},
    )
    got = Settings.from_env(config_path=path)
    assert got.gap_limit == "42"  # str field carries the DECIMAL STRING
    assert got.price_enabled is False
    assert got.request_timeout_s == 5.5


def test_env_beats_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_config(tmp_path, {"gap_limit": "42", "price_enabled": False})
    monkeypatch.setenv("LOCALWALLET_GAP_LIMIT", "7")
    monkeypatch.setenv("LOCALWALLET_PRICE_ENABLED", "1")
    got = Settings.from_env(config_path=path)
    assert got.gap_limit == "7"  # env wins
    assert got.price_enabled is True  # env wins over the file's False


def test_chain_base_url_file_beats_stored(tmp_path: Path) -> None:
    path = _write_config(tmp_path, {"chain_base_url": FILE_URL})
    settings = Settings.from_env(config_path=path)
    # resolve still takes stored injected by the caller; file rides the
    # env-or-file slot merged by from_env.
    assert resolve_chain_base_url(settings.chain_base_url, STORED_URL) == FILE_URL


def test_full_ladder_env_file_stored_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_config(tmp_path, {"chain_base_url": FILE_URL})

    # no env, no stored -> file beats the public default:
    assert (
        resolve_chain_base_url(Settings.from_env(config_path=path).chain_base_url, None)
        == FILE_URL
    )
    # env beats file (and stored):
    monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", ENV_URL)
    assert (
        resolve_chain_base_url(
            Settings.from_env(config_path=path).chain_base_url, STORED_URL
        )
        == ENV_URL
    )
    # file beats stored (env unset):
    monkeypatch.delenv("LOCALWALLET_CHAIN_BASE_URL")
    assert (
        resolve_chain_base_url(
            Settings.from_env(config_path=path).chain_base_url, STORED_URL
        )
        == FILE_URL
    )
    # stored beats None -> default stays the caller's job:
    assert resolve_chain_base_url("", STORED_URL) == STORED_URL
    assert resolve_chain_base_url("", None) is None


def test_coin_settings_file_beats_stored(tmp_path: Path) -> None:
    # coin settings are str fields -> JSON strings in the file.
    path = _write_config(tmp_path, {MIN: "7000"})
    settings = Settings.from_env(config_path=path)
    got = resolve_coin_selection_settings(
        {
            MIN: settings.utxo_target_min_sats,
            MAX: settings.utxo_target_max_sats,
            VB: settings.consolidate_below_sat_vb,
        },
        {MIN: "9000", MAX: "30000"},  # stored rung
    )
    assert got.target_min_sats == 7_000  # file beats stored
    assert got.target_max_sats == 30_000  # stored beats default
    assert got.consolidate_below_sat_vb == 2  # default


@pytest.mark.parametrize(
    ("content", "probe"),
    [
        ("not json{", "not valid JSON"),
        ("[1, 2]", "must be a JSON object"),
        ('{"unknown_key": 1}', "unknown config key: unknown_key"),
        ('{"price_enabled": "yes"}', "config key price_enabled must be a boolean"),
        ('{"gap_limit": 42}', "config key gap_limit must be a string"),
        ('{"request_timeout_s": "10"}', "config key request_timeout_s must be a number"),
        ('{"max_retries": 3.5}', "config key max_retries must be an integer"),
    ],
)
def test_malformed_config_refuses_startup_value_free(
    tmp_path: Path, content: str, probe: str
) -> None:
    path = _write_raw(tmp_path, content)
    with pytest.raises(ValueError) as excinfo:
        Settings.from_env(config_path=path)
    message = str(excinfo.value)
    assert probe in message
    # value-free: the offending value is never echoed.
    for value in ("42", "yes", "10", "3.5", "1, 2"):
        assert value not in message


def test_malformed_file_refuses_even_when_env_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fail-closed: the WHOLE file is validated regardless of env override.
    path = _write_raw(tmp_path, '{"gap_limit": 42}')
    monkeypatch.setenv("LOCALWALLET_GAP_LIMIT", "5")
    with pytest.raises(ValueError):
        Settings.from_env(config_path=path)


# ------------------------------------------------ TCK-FIAT-002 display currency

from localwallet.config import (
    DEFAULT_DISPLAY_CURRENCY,
    DISPLAY_CURRENCIES,
    resolve_display_currency,
)

ALL_CODES = ["usd", "eur", "gbp", "cad", "chf", "aud", "jpy"]


@pytest.mark.parametrize("code", ALL_CODES)
def test_display_currency_ships_unset_with_usd_default(code: str) -> None:
    # The field is a blank-string-by-default scalar (like gap_limit): the
    # default lives in the resolver, so an unset rung never shadows stored.
    assert Settings().display_currency == ""
    assert DEFAULT_DISPLAY_CURRENCY == "usd"
    assert tuple(DISPLAY_CURRENCIES) == tuple(ALL_CODES)
    assert code in DISPLAY_CURRENCIES


@pytest.mark.parametrize(
    ("env_value", "stored_value", "expected"),
    [
        ("eur", "gbp", "eur"),  # env always wins
        ("eur", None, "eur"),
        (None, "gbp", "gbp"),
        (None, None, "usd"),  # no rung set -> shipped default
        ("", "gbp", "gbp"),  # blank rungs are unset, not malformed
        ("eur", "", "eur"),
        ("", "", "usd"),
        ("  EUR  ", "gbp", "eur"),  # case-insensitive, canonical lowercase
        ("cad", "  jpy  ", "cad"),
        (None, "GBP", "gbp"),  # the stored rung canonicalizes too
    ],
)
def test_display_currency_ladder_precedence_and_case(
    env_value: str | None, stored_value: str | None, expected: str
) -> None:
    assert resolve_display_currency(env_value, stored_value) == expected


@pytest.mark.parametrize("blank", [None, "", "   ", "\t"])
def test_display_currency_blank_rungs_are_unset(blank: str | None) -> None:
    assert resolve_display_currency(blank, blank) == "usd"


@pytest.mark.parametrize("bad", ["klingon", "btc", "usdd", "us d", "€", "-1"])
def test_unknown_display_currency_refused_value_free(bad: str) -> None:
    for rung in (bad, None):  # env rung and stored rung both refuse
        env = bad if rung is None else None
        stored = bad if rung is not None else None
        with pytest.raises(ValueError) as excinfo:
            resolve_display_currency(env, stored)
        message = str(excinfo.value)
        assert bad not in message  # value-free, always
    # The message names the closed set so the user can fix it.
    with pytest.raises(ValueError) as excinfo:
        resolve_display_currency("klingon", None)
    assert "usd" in str(excinfo.value) and "jpy" in str(excinfo.value)


def test_display_currency_env_rung_name_is_value_free() -> None:
    with pytest.raises(ValueError) as excinfo:
        resolve_display_currency("NOPE", None)
    assert "LOCALWALLET_DISPLAY_CURRENCY" in str(excinfo.value)


def test_display_currency_stored_rung_name_is_value_free() -> None:
    with pytest.raises(ValueError) as excinfo:
        resolve_display_currency(None, "NOPE")
    assert "display_currency" in str(excinfo.value) and "LOCALWALLET" not in str(
        excinfo.value
    )


def test_display_currency_env_parsed_by_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALWALLET_DISPLAY_CURRENCY", "JPY")
    assert Settings.from_env().display_currency == "JPY"  # raw, resolver canonicalizes


def test_display_currency_config_file_rung(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"display_currency": "GBP"}', encoding="utf-8")
    assert Settings.from_env(config_path=path).display_currency == "GBP"


def test_display_currency_env_beats_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_raw(tmp_path, '{"display_currency": "gbp"}')
    monkeypatch.setenv("LOCALWALLET_DISPLAY_CURRENCY", "aud")
    got = Settings.from_env(config_path=path).display_currency
    assert resolve_display_currency(got, "jpy") == "aud"

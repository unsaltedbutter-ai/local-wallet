"""TCK-ONB-002: chain backend selection persistence + precedence.

Covers the store/config layer only (ADR-0023 decision 3): the pure
:func:`~localwallet.config.resolve_chain_base_url` precedence
(env > stored > None -> public default) and the typed ``Store`` accessors
that persist the first-run choice. Wiring the stored rung into app startup
is TCK-ONB-003's job and deliberately not exercised here.
"""

from __future__ import annotations

import pytest

from localwallet.chain.config import ChainConfig
from localwallet.config import Settings, resolve_chain_base_url
from localwallet.store.db import Store, StoreError

ENV_URL = "https://env-node.local:3006/api"
STORED_URL = "https://my-node.home:3006/api"


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

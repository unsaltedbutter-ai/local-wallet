"""Tests for the pure UTXO-to-balance helper (no I/O involved)."""

import dataclasses
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from localwallet.chain import Balance, ChainError, balance_from_utxos


def utxo(value: int, confirmed: bool, *, vout: int = 0) -> dict[str, Any]:
    """A well-formed Esplora UTXO entry fixture."""
    return {"txid": "c" * 64, "vout": vout, "value": value, "status": {"confirmed": confirmed}}


def test_split_confirmed_and_unconfirmed():
    utxos = [utxo(1000, True), utxo(500, True, vout=1), utxo(250, False)]
    balance = balance_from_utxos(utxos)
    assert balance.confirmed_sats == 1500
    assert balance.unconfirmed_sats == 250
    assert balance.total_sats == 1750


def test_empty_utxo_list_is_zero_balance():
    balance = balance_from_utxos([])
    assert balance.confirmed_sats == 0
    assert balance.unconfirmed_sats == 0
    assert balance.total_sats == 0


@pytest.mark.parametrize("confirmed", [True, False])
def test_single_bucket_balances(confirmed: bool):
    balance = balance_from_utxos([utxo(700, confirmed), utxo(300, confirmed, vout=1)])
    if confirmed:
        assert balance == Balance(confirmed_sats=1000, unconfirmed_sats=0)
    else:
        assert balance == Balance(confirmed_sats=0, unconfirmed_sats=1000)


def test_extra_entry_fields_are_ignored():
    entry = utxo(1234, True)
    entry["block_height"] = 870_000  # forward-compatible extras
    entry["status"]["block_time"] = 1_800_000_000
    balance = balance_from_utxos([entry])
    assert balance.confirmed_sats == 1234


def test_balance_is_frozen_and_total_is_derived():
    balance = Balance(confirmed_sats=1, unconfirmed_sats=2)
    assert balance.total_sats == 3
    with pytest.raises(dataclasses.FrozenInstanceError):
        balance.confirmed_sats = 5  # type: ignore[misc]


@pytest.mark.parametrize(
    ("confirmed", "unconfirmed"),
    [(-1, 0), (0, -1), (1.5, 0), (0, 2.0), (True, 0), (0, "3"), ("1", 0)],
)
def test_balance_rejects_non_int_or_negative_components(confirmed: Any, unconfirmed: Any):
    with pytest.raises(ValueError):
        Balance(confirmed_sats=confirmed, unconfirmed_sats=unconfirmed)


@pytest.mark.parametrize(
    ("entry", "why"),
    [
        ({"txid": "c" * 64, "vout": 0, "value": 100}, "missing status"),
        (
            {"txid": "c" * 64, "vout": 0, "value": 100, "status": "confirmed"},
            "status not an object",
        ),
        ({"txid": "c" * 64, "vout": 0, "value": 100, "status": {}}, "missing confirmed flag"),
        (
            {"txid": "c" * 64, "vout": 0, "value": 100, "status": {"confirmed": 1}},
            "confirmed int, not bool",
        ),
        (
            {"txid": "c" * 64, "vout": 0, "value": 100, "status": {"confirmed": "yes"}},
            "confirmed str, not bool",
        ),
        (
            {"txid": "c" * 64, "vout": 0, "value": 100, "status": {"confirmed": None}},
            "confirmed None",
        ),
        ({"txid": "c" * 64, "vout": 0, "status": {"confirmed": True}}, "missing value"),
        (
            {"txid": "c" * 64, "vout": 0, "value": 100.5, "status": {"confirmed": True}},
            "float value",
        ),
        (
            {"txid": "c" * 64, "vout": 0, "value": 100.0, "status": {"confirmed": True}},
            "integral float value",
        ),
        (
            {"txid": "c" * 64, "vout": 0, "value": -1, "status": {"confirmed": True}},
            "negative value",
        ),
        (
            {"txid": "c" * 64, "vout": 0, "value": "100", "status": {"confirmed": True}},
            "string value",
        ),
        ({"txid": "c" * 64, "vout": 0, "value": True, "status": {"confirmed": True}}, "bool value"),
        ("junk", "entry is a string"),
        (42, "entry is an int"),
        (None, "entry is None"),
    ],
)
def test_malformed_utxo_entry_fails_closed(entry: Any, why: str):
    with pytest.raises(ChainError, match="utxo entry"):
        balance_from_utxos([utxo(700, True), entry])


@pytest.mark.parametrize("bad_input", [{"value": 1}, "nope", None, 7])
def test_non_list_input_fails_closed(bad_input: Any):
    with pytest.raises(ChainError, match="must be a list"):
        balance_from_utxos(bad_input)


def test_error_messages_do_not_leak_amounts():
    # The log-scrubbing invariant: malformed values must not be echoed.
    entry = utxo(123_456_789, True)
    entry["value"] = "123456789"  # malformed: string instead of int
    with pytest.raises(ChainError) as excinfo:
        balance_from_utxos([entry])
    message = str(excinfo.value)
    assert "123456789" not in message
    assert "True" not in message

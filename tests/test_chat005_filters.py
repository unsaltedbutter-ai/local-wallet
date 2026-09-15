"""TCK-CHAT-005 — natural time/label filters on money queries.

The LLM maps phrasings ("bitcoin received in the last 2 weeks", "coins
not labeled X") to STRUCTURED, CLOSED filter params on the existing
``get_history``/``get_utxos`` intents (registry stays FIFTEEN — the
completeness pin lives in ``tests/test_protocol.py``); the ENGINE
validates and resolves. One test family per done-when criterion:

* schema accept/reject matrix — bounds (since caps per unit, one-unit
  XOR, label_set 1..10 words of <=100 chars, closed direction/label_mode
  enums), lax coercions closed, absolute timestamps UNREPRESENTABLE
  (the model never computes one), label_mode never standalone, and the
  previously-valid (unfiltered) envelope shapes byte-identical;
* layer-3 business rules re-check a validation-skipping constructor and
  every rejection is VALUE-FREE (label words never echoed);
* grammar drift pins live in ``tests/test_grammar_conformance.py``
  (ACCEPT/REJECT + the installed-parser probe) — lockstep with this file;
* engine resolution — ``since`` -> cutoff from TOOL-owned now (frozen
  clock, exact day/week arithmetic, calendar-exact clamped months);
  label filters resolve against the V6 ADDRESS-LABEL-SET (address
  membership + coin inheritance — a coin's labels ARE its address's
  set, never the write-frozen v5 coin rows); direction compares
  verbatim against stored row words; filters compose AND-wise;
* honest empty answers and UNCHANGED result/narration shape (filters
  never add keys; label words and addresses never reach tool output).

Offline, deterministic: in-memory stores, fixture zpub, frozen clock —
no network (``chain/`` is untouched; the handlers were already pure
cache reads).
"""

from __future__ import annotations

import calendar
import json
import sys
import time
from pathlib import Path
from typing import Any, Final

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest

from localwallet import app
from localwallet.agent.loop import AgentTurnResult, AgentTurnStatus
from localwallet.protocol import (
    Envelope,
    EnvelopeValidationError,
    IntentName,
    SincePeriod,
    validate_payload,
)
from localwallet.protocol.envelope import (
    GetHistoryParams,
    GetUtxosParams,
)
from localwallet.protocol.intents import BUSINESS_RULES
from localwallet.store import Store, StoreError, TxRecord, UtxoRecord
from localwallet.wallet.descriptor import WalletDescriptor
from tests.test_e2e_skeleton import ZPUB, derive_fixture_addresses

ADDRS: Final[list[str]] = derive_fixture_addresses(8)

#: A frozen "now" inside the schema-admissible window (a Tuesday in
#: February 2026 — non-leap; the leap-year case gets its own pin).
NOW: Final[int] = calendar.timegm((2026, 2, 3, 12, 0, 0, 0, 0, 0))
DAY: Final[int] = 86_400


def _epoch(*utc: int) -> int:
    return calendar.timegm(utc)


# ------------------------------------------------------------------ helpers


def _env(intent: IntentName, params: dict[str, Any]) -> Envelope:
    return validate_payload(
        json.dumps({"v": 0, "intent": intent.value, "params": params})
    )


def _world() -> tuple[Store, int, dict]:
    """In-memory store + fixture wallet + the PRODUCTION dispatch table
    (client=None: get_history/get_utxos are pure cache reads)."""
    store = Store.memory()
    wd = WalletDescriptor.from_key(ZPUB)
    wallet = store.create_wallet("default", wd.descriptor)
    store.set_active_wallet(wallet.id)
    table = app.build_dispatch_table(
        store, wallet, wd.parsed, client=None, scan_fn=lambda: None
    )
    return store, wallet.id, table


def _say(
    table: dict, intent: IntentName, params: dict[str, Any]
) -> tuple[dict, list[str]]:
    """Dispatch one envelope through the PRODUCTION handler AND the
    production narration; return (result, printed lines)."""
    env = _env(intent, params)
    result = table[intent](env)
    out: list[str] = []
    app._print_turn(
        AgentTurnResult(
            status=AgentTurnStatus.OK,
            envelope=env,
            result=result,
            user_message=None,
            turns_used=0,
        ),
        out.append,
    )
    return result, out


def _seed_tx(
    store: Store,
    wid: int,
    txid: str,
    *,
    direction: str,
    height: int | None,
    block_time: int | None,
) -> None:
    store.upsert_txs(
        [
            TxRecord(
                wallet_id=wid,
                txid=txid,
                height=height,
                block_time=block_time,
                fee_sats=1000,
                direction=direction,
                raw_summary=None,
            )
        ]
    )


def _seed_coin(
    store: Store,
    wid: int,
    txid: str,
    addr: str,
    value: int,
    *,
    confirmed: int = 1,
    height: int | None = 900_000,
) -> None:
    rows = store.get_utxos_for_wallet(wid)
    rows.append(
        UtxoRecord(
            wallet_id=wid,
            txid=txid,
            vout=len(rows),
            address=addr,
            value_sats=value,
            confirmed=confirmed,
            height=height if confirmed else None,
        )
    )
    store.replace_utxos_for_wallet(wid, rows)


@pytest.fixture()
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> int:
    """Freeze the tool-owned clock the ``since`` resolver reads."""
    monkeypatch.setattr(time, "time", lambda: float(NOW))
    return NOW


# =========================================================================
# 1. Schema layer (2) — the accept/reject matrix
# =========================================================================

WIRE_OK: Final[list[dict[str, Any]]] = [
    {},
    {"limit": 20},
    {"direction": "in"},
    {"direction": "out"},
    {"since": {"days": 1}},
    {"since": {"days": 3660}},
    {"since": {"weeks": 2}},
    {"since": {"weeks": 522}},
    {"since": {"months": 1}},
    {"since": {"months": 120}},
    {"label_set": ["kyc"]},
    {"label_set": ["kyc", "Spearmint", "a phrase with spaces"]},
    {"label_set": ["kyc"], "label_mode": "include"},
    {"label_set": ["kyc"], "label_mode": "exclude"},
    {
        "limit": 5,
        "direction": "in",
        "since": {"days": 7},
        "label_set": ["a"],
        "label_mode": "exclude",
    },
]


class TestSchemaAccept:
    @pytest.mark.parametrize("params", WIRE_OK)
    def test_get_history_admits(self, params: dict[str, Any]) -> None:
        envelope = _env(IntentName.GET_HISTORY, params)
        assert isinstance(envelope.params, GetHistoryParams)
        # wire round-trip: dump == exactly what came in (omitted keys stay
        # absent; label_set dumps as a LIST, not a tuple)
        assert envelope.params.model_dump() == params

    @pytest.mark.parametrize(
        "params",
        [p for p in WIRE_OK[2:] if "limit" not in p]
        + [
            {
                "address_number": 9,
                "direction": "out",
                "since": {"days": 30},
                "label_set": ["a", "b"],
                "label_mode": "exclude",
            }
        ],
    )
    def test_get_utxos_admits(self, params: dict[str, Any]) -> None:
        envelope = _env(IntentName.GET_UTXOS, params)
        assert isinstance(envelope.params, GetUtxosParams)
        assert envelope.params.model_dump() == params

    def test_get_utxos_never_takes_the_history_limit(self) -> None:
        with pytest.raises(EnvelopeValidationError):
            _env(IntentName.GET_UTXOS, {"label_set": ["kyc"], "limit": 5})

    def test_get_utxos_scope_key_and_filters_coexist(self) -> None:
        params = {"address_number": 3, "direction": "in", "label_set": ["kyc"]}
        assert _env(IntentName.GET_UTXOS, params).params.model_dump() == params

    def test_pre_ticket_envelopes_are_byte_identical(self) -> None:
        # additive extension: every previously-valid shape keeps its exact
        # dump (no new keys materialize when they were not sent)
        for intent, params in (
            (IntentName.GET_HISTORY, {}),
            (IntentName.GET_HISTORY, {"limit": 100}),
            (IntentName.GET_UTXOS, {}),
            (IntentName.GET_UTXOS, {"address_number": 7}),
        ):
            assert _env(intent, params).params.model_dump() == params


WIRE_REJECT: Final[list[dict[str, Any]]] = [
    # since bounds (schema is the authority; the grammar's cap is looser)
    {"since": {"days": 0}},
    {"since": {"days": 3661}},
    {"since": {"weeks": 0}},
    {"since": {"weeks": 523}},
    {"since": {"months": 0}},
    {"since": {"months": 121}},
    # since shape: exactly ONE relative unit — no multi-unit, no empty,
    # no unknown unit, and NO absolute timestamp/date form is admitted
    {"since": {}},
    {"since": {"days": 1, "weeks": 1}},
    {"since": {"days": 1, "weeks": 1, "months": 1}},
    {"since": {"hours": 3}},
    {"since": {"date": "2024-01-01"}},
    {"since": {"ts": 1_700_000_000}},
    {"since": 1_700_000_000},
    {"since": "2 weeks"},
    {"since": None},
    {"since": {"days": "2"}},
    {"since": {"days": True}},
    {"since": {"days": 2.0}},
    # closed direction enum
    {"direction": "received"},
    {"direction": "IN"},
    {"direction": None},
    {"direction": True},
    # label_set bounds: 1..10 words, each 1..100 chars, strings only
    {"label_set": []},
    {"label_set": [str(i) for i in range(11)]},
    {"label_set": ["x" * 101]},
    {"label_set": "kyc"},
    {"label_set": None},
    {"label_set": {"label": "kyc"}},
    {"label_set": ["kyc", 1]},
    {"label_set": ["kyc", ["nested"]]},
    # label_mode: closed enum, never standalone, never null
    {"label_mode": "include"},
    {"label_mode": "exclude"},
    {"label_mode": "notin"},
    {"label_mode": None},
    {"label_set": ["kyc"], "label_mode": "NOT"},
    # closed world: no new key names beyond the five (per intent)
    {"label": "kyc"},
    {"since_days": 14},
    {"before": "2 weeks"},
    {"tags": ["kyc"]},
]


class TestSchemaReject:
    @pytest.mark.parametrize("params", WIRE_REJECT)
    def test_get_history_rejects(self, params: dict[str, Any]) -> None:
        with pytest.raises(EnvelopeValidationError):
            _env(IntentName.GET_HISTORY, params)

    @pytest.mark.parametrize("params", WIRE_REJECT)
    def test_get_utxos_rejects(self, params: dict[str, Any]) -> None:
        with pytest.raises(EnvelopeValidationError):
            _env(IntentName.GET_UTXOS, params)

    def test_filter_keys_never_join_get_balance_or_get_addresses(self) -> None:
        # the widen rode get_history/get_utxos ONLY — the other reads keep
        # their pre-ticket shapes (closed world per intent)
        values = {
            "direction": "in",
            "label_set": ["x"],
            "since": {"days": 1},
            "label_mode": "include",
        }
        for intent, key in (
            (IntentName.GET_BALANCE, "direction"),
            (IntentName.GET_BALANCE, "label_set"),
            (IntentName.GET_ADDRESSES, "since"),
            (IntentName.GET_ADDRESSES, "label_mode"),
        ):
            with pytest.raises(EnvelopeValidationError):
                _env(intent, {key: values[key]})

    def test_rejections_are_value_free(self) -> None:
        # a rejected label word / malformed value is NEVER echoed into the
        # failure text (value-free guarantee: include_input=False + loc
        # filtering — the new field names are KNOWN schema names)
        for params in (
            {"label_set": ["" + "secret-label" + "x" * 100]},
            {"label_set": ["another-secret-word"] + [f"word{i}" for i in range(10)]},
            {"direction": "SecretDirection"},
        ):
            with pytest.raises(EnvelopeValidationError) as excinfo:
                _env(IntentName.GET_HISTORY, params)
            text = str(excinfo.value)
            assert "secret-label" not in text
            assert "another-secret-word" not in text
            assert "SecretDirection" not in text


# =========================================================================
# 2. Layer-3 business rules — re-check + value-free
# =========================================================================


class TestBusinessRules:
    def test_valid_filters_pass_layer3(self) -> None:
        envelope = _env(
            IntentName.GET_HISTORY,
            {
                "direction": "in",
                "since": {"weeks": 2},
                "label_set": ["kyc", "Spearmint"],
                "label_mode": "exclude",
            },
        )
        assert BUSINESS_RULES[IntentName.GET_HISTORY](envelope.params) == []
        utxos = _env(IntentName.GET_UTXOS, {"label_set": ["kyc"]})
        assert BUSINESS_RULES[IntentName.GET_UTXOS](utxos.params) == []

    def test_skipped_constructor_is_rechecked_value_free(self) -> None:
        # defense in depth: a constructor that BYPASSED pydantic still
        # fails layer 3, and the failure strings echo no offending value
        hostile = GetHistoryParams.model_construct(
            direction="BOTH",
            since=SincePeriod.model_construct(days=99_999_999),
            label_set=("  ", "private-label-word" * 10),
            label_mode="maybe",
        )
        failures = BUSINESS_RULES[IntentName.GET_HISTORY](hostile)
        assert failures, "layer 3 must reject the malformed carriers"
        joined = "; ".join(failures)
        assert "private-label-word" not in joined
        assert "BOTH" not in joined

    def test_label_mode_without_set_is_a_layer3_rejection_too(self) -> None:
        hostile = GetUtxosParams.model_construct(label_mode="exclude")
        assert BUSINESS_RULES[IntentName.GET_UTXOS](hostile) == [
            "params.label_mode requires params.label_set"
        ]

    def test_since_beyond_schema_cap_via_skipped_constructor(self) -> None:
        hostile = GetHistoryParams.model_construct(
            since=SincePeriod.model_construct(months=121)
        )
        assert BUSINESS_RULES[IntentName.GET_HISTORY](hostile) == [
            "params.since.months must be an integer between 1 and 120"
        ]


# =========================================================================
# 3. Engine resolution — since -> timestamp from tool-owned now
# =========================================================================


class TestSinceResolution:
    def test_days_and_weeks_are_exact_second_counts(self) -> None:
        assert app._resolve_since_cutoff(SincePeriod(days=14), now=NOW) == NOW - 14 * DAY
        assert app._resolve_since_cutoff(SincePeriod(weeks=2), now=NOW) == NOW - 14 * DAY

    def test_months_are_calendar_exact(self) -> None:
        # 3 Feb 2026 minus one month == 3 Jan 2026 (UTC), not a 30-day guess
        assert time.gmtime(
            app._resolve_since_cutoff(SincePeriod(months=1), now=NOW)
        )[:3] == (2026, 1, 3)

    def test_months_clamp_to_shorter_month_end(self) -> None:
        march_31 = _epoch(2026, 3, 31, 12, 0, 0)
        assert time.gmtime(
            app._resolve_since_cutoff(SincePeriod(months=1), now=march_31)
        )[:3] == (2026, 2, 28)  # non-leap clamp
        march_31_leap = _epoch(2024, 3, 31, 12, 0, 0)
        assert time.gmtime(
            app._resolve_since_cutoff(SincePeriod(months=1), now=march_31_leap)
        )[:3] == (2024, 2, 29)  # leap clamp
        may_31 = _epoch(2026, 5, 31, 12, 0, 0)
        assert time.gmtime(
            app._resolve_since_cutoff(SincePeriod(months=1), now=may_31)
        )[:3] == (2026, 4, 30)

    def test_resolution_is_deterministic_for_frozen_now(self) -> None:
        since = SincePeriod(months=120)  # the schema cap ≈ a decade back
        first = app._resolve_since_cutoff(since, now=NOW)
        assert first == app._resolve_since_cutoff(since, now=NOW)
        assert time.gmtime(first)[:2] == (2016, 2)

    def test_handler_reads_the_tool_owned_clock(self, frozen_clock: int) -> None:
        store, wid, table = _world()
        _seed_tx(store, wid, "a" * 64, direction="in", height=1, block_time=frozen_clock - 3 * DAY)
        _seed_tx(store, wid, "b" * 64, direction="in", height=1, block_time=frozen_clock - 30 * DAY)
        result = table[IntentName.GET_HISTORY](_env(IntentName.GET_HISTORY, {"since": {"weeks": 1}}))
        assert [t["txid"] for t in result["transactions"]] == ["a" * 64]


# =========================================================================
# 4. get_history handler filters (store rows, verbatim values)
# =========================================================================


class TestHistoryFilters:
    def _seeded(self) -> tuple[Store, int, dict]:
        store, wid, table = _world()
        _seed_tx(store, wid, "c" * 64, direction="in", height=10, block_time=NOW - 1 * DAY)
        _seed_tx(store, wid, "d" * 64, direction="out", height=11, block_time=NOW - 2 * DAY)
        _seed_tx(store, wid, "e" * 64, direction="in", height=12, block_time=NOW - 40 * DAY)
        _seed_tx(store, wid, "f" * 64, direction="self", height=13, block_time=NOW - 2 * DAY)
        _seed_tx(store, wid, "g" * 64, direction="in", height=None, block_time=None)  # pending
        return store, wid, table

    def test_direction_in_and_out_compare_verbatim(self) -> None:
        __store, __wid, table = self._seeded()
        result = table[IntentName.GET_HISTORY](
            _env(IntentName.GET_HISTORY, {"direction": "in"})
        )
        assert [t["txid"] for t in result["transactions"]] == ["g" * 64, "e" * 64, "c" * 64]
        result = table[IntentName.GET_HISTORY](
            _env(IntentName.GET_HISTORY, {"direction": "out"})
        )
        assert [t["txid"] for t in result["transactions"]] == ["d" * 64]
        # 'self' rows match NEITHER literal (store truth, not a gap) — but
        # the UNFILTERED answer still shows them verbatim
        unfiltered = table[IntentName.GET_HISTORY](_env(IntentName.GET_HISTORY, {}))
        assert {"txid", "height", "direction", "fee_sats", "block_time"} <= set(
            unfiltered["transactions"][0]
        )
        assert "f" * 64 in [t["txid"] for t in unfiltered["transactions"]]

    def test_since_window_semantics(self, frozen_clock: int) -> None:
        __store, __wid, table = self._seeded()
        result = table[IntentName.GET_HISTORY](
            _env(IntentName.GET_HISTORY, {"since": {"days": 14}})
        )
        txids = [t["txid"] for t in result["transactions"]]
        assert "c" * 64 in txids  # 1 day old: inside the frozen-now window
        assert "d" * 64 in txids  # 2 days old, direction out: window alone keeps it
        assert "e" * 64 not in txids  # 40 days old confirmed row: excluded
        assert "g" * 64 in txids  # pending row: newest-by-convention, included

    def test_confirmed_row_without_recorded_time_fails_closed(self, frozen_clock: int) -> None:
        store, wid, table = _world()
        _seed_tx(store, wid, "h" * 64, direction="in", height=9, block_time=None)
        result = table[IntentName.GET_HISTORY](
            _env(IntentName.GET_HISTORY, {"since": {"days": 2}})
        )
        assert result["transactions"] == []  # unresolvable -> never claimed

    def test_label_filter_resolves_via_v6_coin_inheritance(self, frozen_clock: int) -> None:
        store, wid, table = self._seeded()
        # the tx's still-unspent coin sits on ADDRS[0]; label the ADDRESS
        _seed_coin(store, wid, "c" * 64, ADDRS[0], 5_000)
        store.add_address_labels(ADDRS[0], ("kyc",))
        result = table[IntentName.GET_HISTORY](
            _env(IntentName.GET_HISTORY, {"label_set": ["kyc"]})
        )
        assert [t["txid"] for t in result["transactions"]] == ["c" * 64]
        result = table[IntentName.GET_HISTORY](
            _env(IntentName.GET_HISTORY, {"label_set": ["kyc"], "label_mode": "exclude"})
        )
        assert "c" * 64 not in [t["txid"] for t in result["transactions"]]
        assert result["shown"] == len(result["transactions"])

    def test_history_label_bound_fully_spent_tx_never_included(self, frozen_clock: int) -> None:
        # documented store-fidelity bound: the coin->address join survives
        # only for UNSPENT coins; a tx with no resolvable labels can never
        # match an include and always passes an exclude
        _store, _wid, table = self._seeded()
        result = table[IntentName.GET_HISTORY](
            _env(IntentName.GET_HISTORY, {"label_set": ["kyc"]})
        )
        assert result["transactions"] == []
        result = table[IntentName.GET_HISTORY](
            _env(IntentName.GET_HISTORY, {"label_set": ["kyc"], "label_mode": "exclude"})
        )
        assert len(result["transactions"]) == 5

    def test_unknown_label_answers_honest_empty(self, frozen_clock: int) -> None:
        store, wid, table = self._seeded()
        _seed_coin(store, wid, "c" * 64, ADDRS[0], 5_000)
        store.add_address_labels(ADDRS[0], ("kyc",))
        result, lines = _say(
            table, IntentName.GET_HISTORY, {"label_set": ["never-used-label"]}
        )
        assert result["transactions"] == []
        assert any("No transactions found." in line for line in lines)

    def test_filters_compose_and_apply_before_the_cap(self, frozen_clock: int) -> None:
        store, wid, table = self._seeded()
        _seed_coin(store, wid, "c" * 64, ADDRS[0], 5_000)
        store.add_address_labels(ADDRS[0], ("kyc",))
        result = table[IntentName.GET_HISTORY](
            _env(
                IntentName.GET_HISTORY,
                {
                    "direction": "in",
                    "since": {"weeks": 2},
                    "label_set": ["kyc"],
                    "limit": 1,
                },
            )
        )
        assert [t["txid"] for t in result["transactions"]] == ["c" * 64]

    def test_cap_counts_matches_not_scans(self) -> None:
        store, wid, table = self._seeded()
        for i in range(5):
            _seed_tx(
                store,
                wid,
                f"{i + 10:064x}"[:64],
                direction="in",
                height=100 + i,
                block_time=NOW - i * DAY,
            )
        # 6 in-rows total (incl. the pending one); direction filter first,
        # THEN the cap: limit=2 answers the two NEWEST matches
        result = table[IntentName.GET_HISTORY](
            _env(IntentName.GET_HISTORY, {"direction": "in", "limit": 2})
        )
        assert result["shown"] == 2
        assert all(t["direction"] == "in" for t in result["transactions"])

    def test_values_are_verbatim_and_result_shape_unchanged(self, frozen_clock: int) -> None:
        _store, _wid, table = self._seeded()
        plain = table[IntentName.GET_HISTORY](_env(IntentName.GET_HISTORY, {}))
        result, _lines = _say(table, IntentName.GET_HISTORY, {"direction": "in"})
        assert set(result) == set(plain)  # no new keys, ever
        by_txid = {t["txid"]: t for t in plain["transactions"]}
        for row in result["transactions"]:
            assert row == by_txid[row["txid"]]  # verbatim store projection

    def test_label_words_and_addresses_never_reach_output(self, frozen_clock: int) -> None:
        store, wid, table = self._seeded()
        _seed_coin(store, wid, "c" * 64, ADDRS[0], 5_000)
        store.add_address_labels(ADDRS[0], ("PrivateLabelWord",))
        _result, lines = _say(
            table, IntentName.GET_HISTORY, {"label_set": ["PrivateLabelWord"]}
        )
        blob = json.dumps(_result) if isinstance(_result, dict) else ""
        whole = blob + "".join(lines)
        assert "PrivateLabelWord" not in whole
        assert ADDRS[0] not in whole  # history stays address-free by contract


# =========================================================================
# 5. get_utxos handler filters (coin rows + tx join + v6 label sets)
# =========================================================================


class TestUtxosFilters:
    def _seeded(self) -> tuple[Store, int, dict]:
        store, wid, table = _world()
        # inbound coin (creating tx direction 'in', confirmed 1 day ago)
        _seed_tx(store, wid, "1" * 64, direction="in", height=10, block_time=NOW - 1 * DAY)
        _seed_coin(store, wid, "1" * 64, ADDRS[0], 100_000)
        # change coin from an OUTGOING tx (long ago)
        _seed_tx(store, wid, "2" * 64, direction="out", height=5, block_time=NOW - 100 * DAY)
        _seed_coin(store, wid, "2" * 64, ADDRS[1], 200_000)
        # pending inbound coin (unconfirmed: newest by convention)
        _seed_tx(store, wid, "3" * 64, direction="in", height=None, block_time=None)
        _seed_coin(store, wid, "3" * 64, ADDRS[2], 50_000, confirmed=0, height=None)
        # coin with NO tx row at all (unresolvable direction/time view)
        _seed_coin(store, wid, "4" * 64, ADDRS[3], 25_000)
        return store, wid, table

    def test_label_include_exclude_on_address_set(self) -> None:
        store, _wid, table = self._seeded()
        store.add_address_labels(ADDRS[0], ("kyc",))
        store.add_address_labels(ADDRS[2], ("Spearmint",))
        result = table[IntentName.GET_UTXOS](_env(IntentName.GET_UTXOS, {"label_set": ["kyc"]}))
        assert [u["address"] for u in result["utxos"]] == [ADDRS[0]]
        result = table[IntentName.GET_UTXOS](
            _env(IntentName.GET_UTXOS, {"label_set": ["kyc"], "label_mode": "exclude"})
        )
        assert ADDRS[0] not in [u["address"] for u in result["utxos"]]
        assert result["count"] == len(result["utxos"])

    def test_label_match_is_case_and_whitespace_tolerant_tag_canonical(self) -> None:
        store, _wid, table = self._seeded()
        store.add_address_labels(ADDRS[0], ("KYC",))  # stores canonical 'kyc'
        store.add_address_labels(ADDRS[2], ("Spearmint",))  # free text verbatim
        for word in ("kyc", "KYC", " Kyc "):
            result = table[IntentName.GET_UTXOS](
                _env(IntentName.GET_UTXOS, {"label_set": [word]})
            )
            assert [u["address"] for u in result["utxos"]] == [ADDRS[0]]
        result = table[IntentName.GET_UTXOS](
            _env(IntentName.GET_UTXOS, {"label_set": ["spearmint"]})
        )
        assert [u["address"] for u in result["utxos"]] == [ADDRS[2]]

    def test_multi_word_set_is_any_of_in_sql_in_semantics(self) -> None:
        store, _wid, table = self._seeded()
        store.add_address_labels(ADDRS[0], ("kyc",))
        store.add_address_labels(ADDRS[2], ("spearmint",))
        result = table[IntentName.GET_UTXOS](
            _env(IntentName.GET_UTXOS, {"label_set": ["kyc", "spearmint"]})
        )
        assert {u["address"] for u in result["utxos"]} == {ADDRS[0], ADDRS[2]}
        result = table[IntentName.GET_UTXOS](
            _env(
                IntentName.GET_UTXOS,
                {"label_set": ["kyc", "spearmint"], "label_mode": "exclude"},
            )
        )
        assert {u["address"] for u in result["utxos"]} == {ADDRS[1], ADDRS[3]}

    def test_direction_and_since_resolve_through_the_creating_tx(self, frozen_clock: int) -> None:
        _store, _wid, table = self._seeded()
        result = table[IntentName.GET_UTXOS](
            _env(IntentName.GET_UTXOS, {"direction": "out"})
        )
        assert [u["address"] for u in result["utxos"]] == [ADDRS[1]]
        result = table[IntentName.GET_UTXOS](
            _env(IntentName.GET_UTXOS, {"direction": "in"})
        )
        assert {u["address"] for u in result["utxos"]} == {ADDRS[0], ADDRS[2]}
        assert ADDRS[3] not in {u["address"] for u in result["utxos"]}  # no tx row: excluded
        result = table[IntentName.GET_UTXOS](
            _env(IntentName.GET_UTXOS, {"since": {"weeks": 2}})
        )
        addrs = [u["address"] for u in result["utxos"]]
        assert ADDRS[0] in addrs  # 1 day old: inside the frozen-now window
        assert ADDRS[1] not in addrs  # 100 days old: outside
        assert ADDRS[2] in addrs  # pending coin = newest by convention
        assert ADDRS[3] not in addrs  # confirmed coin, no tx row: fails closed

    def test_filters_compose_with_each_other_and_the_scope(self, frozen_clock: int) -> None:
        store, wid, table = self._seeded()
        store.add_address_labels(ADDRS[0], ("kyc",))
        registry_row = store.note_address_shown(wid, ADDRS[0])
        result = table[IntentName.GET_UTXOS](
            _env(
                IntentName.GET_UTXOS,
                {
                    "address_number": registry_row.number,
                    "direction": "in",
                    "since": {"months": 1},
                    "label_set": ["kyc"],
                },
            )
        )
        assert [u["address"] for u in result["utxos"]] == [ADDRS[0]]
        assert result["address_number"] == registry_row.number

    def test_unknown_label_answers_honest_empty(self, frozen_clock: int) -> None:
        __store, __wid, table = self._seeded()
        result, lines = _say(table, IntentName.GET_UTXOS, {"label_set": ["nowhere"]})
        assert result["utxos"] == []
        assert result["count"] == 0
        assert any("No unspent outputs." in line for line in lines)

    def test_only_printed_coins_get_registry_numbers(self) -> None:
        store, wid, table = self._seeded()
        store.add_address_labels(ADDRS[0], ("kyc",))
        _result = table[IntentName.GET_UTXOS](
            _env(IntentName.GET_UTXOS, {"label_set": ["kyc"]})
        )
        numbers = {r.address for r in store.list_address_registry(wid)}
        assert numbers == {ADDRS[0]}  # hidden coins' addresses stay unnumbered

    def test_unfiltered_answer_is_the_pre_ticket_shape(self) -> None:
        _store, _wid, table = self._seeded()
        result = table[IntentName.GET_UTXOS](_env(IntentName.GET_UTXOS, {}))
        assert {"utxos", "count", "freshness"} <= set(result)
        assert all(
            {"txid", "vout", "address", "value_sats", "confirmed", "number"}
            == set(u)
            for u in result["utxos"]
        )

    def test_label_words_never_reach_tool_output(self) -> None:
        store, _wid, table = self._seeded()
        store.add_address_labels(ADDRS[0], ("PrivateCoinLabel",))
        result, lines = _say(table, IntentName.GET_UTXOS, {"label_set": ["PrivateCoinLabel"]})
        assert "PrivateCoinLabel" not in json.dumps(result) + "".join(lines)
        # (the coin's OWN address prints verbatim — listing surface; the
        #  label TEXT is what must never flow back)
        assert ADDRS[0] in json.dumps(result)


# =========================================================================
# 6. Narration shape — unchanged
# =========================================================================


class TestNarration:
    def test_history_lines_keep_their_shape(self) -> None:
        __store, __wid, table = TestHistoryFilters()._seeded()
        result, lines = _say(table, IntentName.GET_HISTORY, {"direction": "in"})
        data = [ln for ln in lines if ln.startswith("tx ")]
        assert len(data) == len(result["transactions"]) > 0
        for line in data:
            parts = line.split(" ")
            # "tx <64-hex> in <height|unconfirmed>" — the pre-ticket shape
            assert parts[0] == "tx" and len(parts) == 4 and parts[2] == "in"
        assert not any("label" in ln.lower() for ln in lines)

    def test_utxos_lines_keep_their_shape(self) -> None:
        store, _wid, table = TestUtxosFilters()._seeded()
        store.add_address_labels(ADDRS[0], ("kyc",))
        _result, lines = _say(table, IntentName.GET_UTXOS, {"label_set": ["kyc"]})
        coin_lines = [ln for ln in lines if ADDRS[0] in ln]
        assert len(coin_lines) == 1
        # "<registry-number> <address> ·" prefix — the pre-ticket shape
        assert coin_lines[0].startswith("#1 " + ADDRS[0])
        assert "kyc" not in "".join(lines)


# =========================================================================
# 7. Lockstep drift pins — prompt/registry/evals
# =========================================================================


class TestLockstepPins:
    def test_registry_stays_fifteen(self) -> None:
        # NO new intent joined: the filters ride the existing reads
        assert len(IntentName) == 15
        from localwallet.protocol import INTENT_REGISTRY

        assert len(INTENT_REGISTRY) == 15

    def test_prompt_teaches_relative_windows_and_verbatim_words(self) -> None:
        from localwallet.agent.prompt import build_system_prompt

        prompt = build_system_prompt()
        assert '"since"' in prompt and '"days"' in prompt and '"months"' in prompt
        assert "NEVER compute a timestamp" in prompt
        assert '"label_mode": "exclude"' in prompt
        # filter few-shots present and exact
        assert 'params": {"direction": "in", "since": {"weeks": 2}}' in prompt
        assert 'params": {"label_set": ["salary"]}' in prompt

    def test_golden_fixtures_cover_every_filter_family(self) -> None:
        evals_dir = Path(__file__).resolve().parent.parent / "evals" / "golden"
        blobs = [
            json.loads(p.read_text())
            for p in sorted(evals_dir.glob("golden-0[67][7-9].json"))
            + sorted(evals_dir.glob("golden-07*.json"))
        ]
        assert blobs, "TCK-CHAT-005 eval fixtures must exist"
        ids = {b["id"] for b in blobs}
        assert {"golden-067", "golden-068", "golden-069", "golden-070", "golden-071", "golden-072"} <= ids
        for blob in blobs:
            exp = blob["expectation"]
            assert exp["intent"] in ("get_history", "get_utxos")
            # every fixture expectation itself validates through the schema
            _env(IntentName(exp["intent"]), exp["params"])

    def test_store_label_source_is_the_v6_set_not_coin_labels(self) -> None:
        # CRITIQUE PIN: filters resolve against address_label_set (the v6
        # store accessor); the resolver helper reads it, and a v5-era
        # coin_labels row (write-frozen history) changes NOTHING
        store = Store.memory()
        wd = WalletDescriptor.from_key(ZPUB)
        wallet = store.create_wallet("default", wd.descriptor)
        _seed_tx(store, wallet.id, "c" * 64, direction="in", height=10, block_time=NOW)
        _seed_coin(store, wallet.id, "c" * 64, ADDRS[0], 5_000)
        assert store.get_address_label_set(ADDRS[0]) == ()  # unlabeled
        store.add_address_labels(ADDRS[0], ("kyc",))
        members = app._label_members_by_txid(store, wallet.id)
        assert members == {"c" * 64: ("kyc",)}  # coin inheritance, verbatim set

    def test_dust_of_semantics_direction_enum_mirrors_store_words(self) -> None:
        # the closed enum literals are the STORE's direction words — the
        # handler compares them verbatim (this pin fails if either side
        # ever renames and the compare silently stops matching)
        from localwallet.store import DIR_IN, DIR_OUT

        assert (DIR_IN, DIR_OUT) == ("in", "out")


class TestStoreErrorPaths:
    def test_label_set_read_failure_is_the_value_free_store_error(self, monkeypatch) -> None:
        store, _wid, table = _world()
        def boom(*_a: object, **_k: object) -> None:
            # every production StoreError message is a fixed, value-free
            # string (the store's own discipline) — the handler must not
            # add label words of its own on top
            raise StoreError("address label read failed")
        monkeypatch.setattr(store, "get_address_label_sets", boom)
        result = table[IntentName.GET_UTXOS](
            _env(IntentName.GET_UTXOS, {"label_set": ["kyc"]})
        )
        assert result.get("error") == "store_error"
        assert "private-label-word" not in json.dumps(result)

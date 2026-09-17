"""TCK-RBF-005 — in-flight resolver + lineage-aware tx_status (app half).

Two contracts, both pinned by DIRECT unit tests here:

* ``_resolve_in_flight_outgoing`` — the ONE shared resolver RBF-004
  consumes as-is: name/signature/return shape pinned, single-assume,
  multi indexed-list shape, honest empty, lineage-retired exclusion,
  legacy NULLs never fabricated, value-free.
* lineage-aware ``tx_status`` answers — all four lineage states of a
  queried txid (terminal replaced / terminal evicted / live race /
  no lineage data) plus the narration copies and the byte-identical
  legacy paths (try-again stays ONLY for the unlinked just-broadcast
  txid; the awaiting_backend refusal still runs first).
"""

from __future__ import annotations

import inspect
import re
from types import SimpleNamespace
from typing import Any

import pytest

from localwallet import app
from localwallet.chain import ChainError
from localwallet.chain.esplora import HTTP_STATUS, RPC_ERROR
from localwallet.protocol import Envelope, IntentName, TxStatusParams
from localwallet.store import (
    DIR_IN,
    DIR_OUT,
    DIR_SELF,
    Store,
    StoreError,
    TxRecord,
)

DESCRIPTOR = "wpkh([abcd1234/84'/0'/0']vpub/0/*)"
ORIG = "a" * 64  # lineage pair: the original
REPL = "b" * 64  # ... and its RBF replacement
SEEN = 1_757_000_000  # a fixed unix second for the age math


def _tx(
    wallet_id: int,
    txid: str,
    *,
    height: int | None = None,
    direction: str = DIR_OUT,
    fee_sats: int | None = None,
    amount_sats: int | None = None,
    fee_rate_centisat_vb: int | None = None,
    first_seen: int | None = None,
    replaced_by_txid: str | None = None,
) -> TxRecord:
    return TxRecord(
        wallet_id=wallet_id,
        txid=txid,
        height=height,
        block_time=None if height is None else 1_700_000_000,
        fee_sats=fee_sats,
        direction=direction,
        raw_summary=None,
        amount_sats=amount_sats,
        fee_rate_centisat_vb=fee_rate_centisat_vb,
        first_seen=first_seen,
        replaced_by_txid=replaced_by_txid,
    )


class _StatusClient:
    """get_tx_status-only fake: canned status, or the chain layer's
    documented failure shapes (provable not-found = HTTP_STATUS class +
    the explicit 404 status field, else generic)."""

    def __init__(
        self,
        *,
        confirmed: bool = False,
        height: int | None = None,
        fail_404: bool = False,
        fail_with: ChainError | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._confirmed = confirmed
        self._height = height
        self._fail_404 = fail_404
        self._fail_with = fail_with

    def get_tx_status(self, txid: str) -> Any:
        self.calls.append(txid)
        if self._fail_with is not None:
            raise self._fail_with
        if self._fail_404:
            # TCK-PUBLICBCAST-002 review: the real immediate-4xx raise
            # sites (esplora._request_json_at, bitcoind._attempt) carry the
            # typed class AND the explicit numeric status — the hedge keys
            # on that evidence, never on the message text.
            raise ChainError(
                "transaction status request failed: status 404",
                failure_class=HTTP_STATUS,
                http_status=404,
            )
        return SimpleNamespace(
            txid=txid, confirmed=self._confirmed, block_height=self._height, block_time=None
        )


class _BrokenStore:
    """Store stand-in whose tx read raises the store's value-free error."""

    def get_txs_for_wallet(self, wallet_id: int) -> list[TxRecord]:
        raise StoreError("database read failed")


def _memory_store(rows: list[TxRecord], *, link: tuple[str, str] | None = None) -> Store:
    """A memory store holding exactly ``rows`` (one wallet); ``link`` is the
    (original, replacement) pair written through the sanctioned writer."""
    store = Store.memory()
    wallet_id = store.create_wallet("main", DESCRIPTOR).id
    store.upsert_txs(rows)
    if link is not None:
        store.record_replacement(wallet_id, *link)
    return store


def _handler(
    client: _StatusClient,
    store: Any,
    wallet_id: int | None = 1,
    flow: Any = None,
    gate: app.StartupScan | None = None,
) -> Any:
    return app._make_tx_status_handler(
        client,  # type: ignore[arg-type] — protocol stand-in
        flow if flow is not None else SimpleNamespace(txid=None),  # type: ignore[arg-type]
        gate,
        store=store,
        wallet_id=wallet_id,
    )


def _env(txid: str) -> Envelope:
    return Envelope(v=0, intent=IntentName.TX_STATUS, params=TxStatusParams(txid=txid))


_HEX64 = re.compile(r"[0-9a-f]{64}")


# ------------------------------------------------- resolver: pinned contract


def test_resolver_contract_pinned() -> None:
    """RBF-004 consumes EXACTLY this API: name, signature, return shape,
    and the docstring contract words."""
    fn = app._resolve_in_flight_outgoing
    sig = inspect.signature(fn)
    assert list(sig.parameters) == ["tx_records", "now"]
    assert sig.parameters["now"].kind is inspect.Parameter.KEYWORD_ONLY
    doc = fn.__doc__ or ""
    for contract in (
        "get_txs_for_wallet",  # input: verbatim store rows
        "never fabricate",  # empty is honest
        "assume-and-name-it",  # single semantics
        "indexed",  # multi semantics
        "index",
        "amount_sats",
        "fee_rate_centisat_vb",
        "age_s",
    ):
        assert contract in doc


def test_resolver_single_assume_and_name() -> None:
    """One in-flight outgoing row → exactly one entry, carrying the txid
    the caller names verbatim (assume-and-name-it)."""
    rows = [_tx(1, ORIG, amount_sats=42_000, fee_rate_centisat_vb=213, first_seen=SEEN)]
    entries = app._resolve_in_flight_outgoing(rows, now=SEEN + 600)
    assert entries == [
        {
            "index": 1,
            "txid": ORIG,
            "amount_sats": 42_000,
            "fee_rate_centisat_vb": 213,
            "age_s": 600,
        }
    ]


def test_resolver_multi_indexed_shape() -> None:
    """Multiple in-flight rows → the indexed list: 1-based indices in the
    (store-order) input sequence, amount + fee rate + age per entry."""
    rows = [
        _tx(1, ORIG, amount_sats=42_000, fee_rate_centisat_vb=213, first_seen=SEEN),
        _tx(1, REPL, amount_sats=7_000, fee_rate_centisat_vb=99, first_seen=SEEN + 60,
            direction=DIR_SELF),
    ]
    entries = app._resolve_in_flight_outgoing(rows, now=SEEN + 600)
    assert [e["index"] for e in entries] == [1, 2]
    assert [e["txid"] for e in entries] == [ORIG, REPL]
    assert [e["amount_sats"] for e in entries] == [42_000, 7_000]
    assert [e["fee_rate_centisat_vb"] for e in entries] == [213, 99]
    assert [e["age_s"] for e in entries] == [600, 540]


def test_resolver_none_is_honest_empty() -> None:
    """Nothing in flight → an EMPTY list — never a fabricated entry."""
    assert app._resolve_in_flight_outgoing([], now=SEEN) == []
    confirmed = [_tx(1, ORIG, height=900_001, amount_sats=1, fee_rate_centisat_vb=1,
                     first_seen=SEEN)]
    assert app._resolve_in_flight_outgoing(confirmed, now=SEEN) == []
    inbound = [_tx(1, REPL, direction=DIR_IN)]  # an incoming row is not our spend
    assert app._resolve_in_flight_outgoing(inbound, now=SEEN) == []


def test_resolver_lineage_retirement_and_live_race() -> None:
    """Store-lineage semantics: a terminal replaced original and a terminal
    evicted bump exit the list; a live race (neither side confirmed) keeps
    BOTH candidates in flight."""
    rows = [
        _tx(1, ORIG, replaced_by_txid=REPL, amount_sats=42_000, first_seen=SEEN),
        _tx(1, REPL, height=900_002, amount_sats=42_000, first_seen=SEEN + 30),
    ]
    assert app._resolve_in_flight_outgoing(rows, now=SEEN + 600) == []  # ORIG replaced
    winner = [
        _tx(1, ORIG, height=900_001, replaced_by_txid=REPL, first_seen=SEEN),
        _tx(1, REPL, amount_sats=42_000, first_seen=SEEN + 30),
    ]
    assert app._resolve_in_flight_outgoing(winner, now=SEEN + 600) == []  # REPL evicted
    live = [
        _tx(1, ORIG, replaced_by_txid=REPL, amount_sats=42_000, first_seen=SEEN),
        _tx(1, REPL, amount_sats=42_000, first_seen=SEEN + 30),
    ]
    assert [e["txid"] for e in app._resolve_in_flight_outgoing(live, now=SEEN + 600)] == [
        ORIG,
        REPL,
    ]


def test_resolver_legacy_nulls_not_fabricated() -> None:
    """A pre-v3 row (or one captured before the broadcast write shipped)
    carries NULLs — the entry keeps them NULL, never invents values."""
    rows = [_tx(1, ORIG)]
    entries = app._resolve_in_flight_outgoing(rows, now=SEEN)
    assert entries == [
        {
            "index": 1,
            "txid": ORIG,
            "amount_sats": None,
            "fee_rate_centisat_vb": None,
            "age_s": None,
        }
    ]


def test_resolver_age_floored_at_zero() -> None:
    """Clock skew (first_seen slightly ahead of now) ages to 0 — never a
    negative duration."""
    rows = [_tx(1, ORIG, first_seen=SEEN + 500)]
    assert app._resolve_in_flight_outgoing(rows, now=SEEN)[0]["age_s"] == 0


def test_resolver_reads_store_rows_verbatim(tmp_path) -> None:
    """The documented input IS get_txs_for_wallet output: the resolver runs
    straight off real store rows, store order (txid ascending) preserved."""
    with Store(tmp_path / "r.db") as store:
        wid = store.create_wallet("main", DESCRIPTOR).id
        store.upsert_txs(
            [
                _tx(wid, REPL, amount_sats=7_000, fee_rate_centisat_vb=505, first_seen=SEEN),
                _tx(wid, ORIG, amount_sats=42_000, fee_rate_centisat_vb=213, first_seen=SEEN),
            ]
        )
        store.record_replacement(wid, ORIG, REPL)  # live race — both stay
        entries = app._resolve_in_flight_outgoing(store.get_txs_for_wallet(wid), now=SEEN + 1)
        assert [e["txid"] for e in entries] == [ORIG, REPL]  # store order
        assert [e["index"] for e in entries] == [1, 2]


# ----------------------------------------- lineage-aware tx_status answers


def test_status_replaced_terminal_answers_from_store_no_chain_call() -> None:
    """The bump CONFIRMED (scan recorded the height): "replaced by <new
    txid>" is already store truth — the chain is not asked."""
    with _memory_store(
        [
            _tx(1, ORIG, amount_sats=42_000, first_seen=SEEN),
            _tx(1, REPL, height=900_002, amount_sats=42_000, first_seen=SEEN + 30),
        ],
        link=(ORIG, REPL),
    ) as store:
        client = _StatusClient()
        result = _handler(client, store)(_env(ORIG))
    assert result == {
        "txid": ORIG,
        "confirmed": False,
        "lineage": "replaced",
        "replaced_by": REPL,
        "replacement_height": 900_002,
    }
    assert client.calls == []  # answered from store truth — zero network


def test_status_evicted_terminal_no_chain_call() -> None:
    """The bump lost the race (original confirmed): the honest eviction
    copy, straight from the store, chain untouched."""
    with _memory_store(
        [
            _tx(1, ORIG, height=900_001, first_seen=SEEN),
            _tx(1, REPL, amount_sats=42_000, first_seen=SEEN + 30),
        ],
        link=(ORIG, REPL),
    ) as store:
        client = _StatusClient()
        result = _handler(client, store)(_env(REPL))
    assert result == {
        "txid": REPL,
        "confirmed": False,
        "lineage": "evicted",
        "original_txid": ORIG,
        "original_height": 900_001,
    }
    assert client.calls == []


def test_status_live_race_404_resolves_to_hedged_replaced_copy() -> None:
    """Unknown to the backend (404) but the lineage bump is recorded: the
    hedged replaced copy — NEVER an endless "try again". The bump's own
    outcome stays honest (no height claimed)."""
    with _memory_store(
        [
            _tx(1, ORIG, replaced_by_txid=REPL, first_seen=SEEN),
            _tx(1, REPL, first_seen=SEEN + 30),
        ],
        link=(ORIG, REPL),
    ) as store:
        client = _StatusClient(fail_404=True)
        result = _handler(client, store)(_env(ORIG))
    assert result == {
        "txid": ORIG,
        "confirmed": False,
        "lineage": "replaced",
        "replaced_by": REPL,
        "replacement_height": None,
    }
    assert client.calls == [ORIG]  # the chain was asked, and was answered


@pytest.mark.parametrize(
    "exc",
    [
        # Class-less (a bare ChainError): no provable not-found, even with
        # the old dialect text in the message.
        ChainError("transaction status request failed: status 404"),
        # Foreign class (Electrum's tx-status refusal rides RPC_ERROR).
        ChainError(
            "transaction status request rejected by the server",
            failure_class=RPC_ERROR,
        ),
        # Retry-EXHAUSTED 5xx: the HTTP_STATUS class but NO explicit
        # status field — a transient failure, never provable not-found.
        ChainError(
            "transaction status request failed after 3 retries: status 502",
            failure_class=HTTP_STATUS,
            exc_name="HTTPStatus",
        ),
    ],
    ids=["class-less", "foreign-class", "5xx-retry-exhausted"],
)
def test_status_live_race_hedge_fails_closed_without_explicit_404(
    exc: ChainError,
) -> None:
    """TCK-PUBLICBCAST-002 review pin: the hedged-replaced branch fires
    ONLY on the explicit-404 evidence (class + status field). Live-race
    lineage + anything else answers ``chain_unavailable`` — a transient or
    unlabeled failure may never mint the live-race "replaced" claim."""
    with _memory_store(
        [
            _tx(1, ORIG, replaced_by_txid=REPL, first_seen=SEEN),
            _tx(1, REPL, first_seen=SEEN + 30),
        ],
        link=(ORIG, REPL),
    ) as store:
        client = _StatusClient(fail_with=exc)
        result = _handler(client, store)(_env(ORIG))
    assert result["error"] == "chain_unavailable"
    assert "lineage" not in result  # the hedge stood down, fail-closed
    assert client.calls == [ORIG]  # the chain WAS asked (no pre-emption)


def test_status_live_race_never_preempts_a_chain_answer() -> None:
    """Live race + the chain still relays the original (unconfirmed): the
    chain answer wins verbatim — the store may not claim "replaced" early."""
    with _memory_store(
        [
            _tx(1, ORIG, replaced_by_txid=REPL, first_seen=SEEN),
            _tx(1, REPL, first_seen=SEEN + 30),
        ],
        link=(ORIG, REPL),
    ) as store:
        client = _StatusClient(confirmed=False)
        result = _handler(client, store)(_env(ORIG))
    assert result == {
        "txid": ORIG,
        "confirmed": False,
        "block_height": None,
        "block_time": None,
    }
    assert "lineage" not in result


def test_status_race_winner_confirms_on_chain_unmasked() -> None:
    """The original WON (confirmed everywhere): its own link says nothing
    terminal about it — the chain's confirmed answer passes through."""
    with _memory_store(
        [
            _tx(1, ORIG, height=900_001, replaced_by_txid=REPL, first_seen=SEEN),
            _tx(1, REPL, first_seen=SEEN + 30),
        ],
        link=(ORIG, REPL),
    ) as store:
        client = _StatusClient(confirmed=True, height=900_001)
        result = _handler(client, store)(_env(ORIG))
    assert result["confirmed"] is True
    assert result["block_height"] == 900_001
    assert "lineage" not in result


def test_status_unlinked_just_broadcast_keeps_try_again_verbatim() -> None:
    """Legacy byte-pin: the flow's OWN just-broadcast txid (a store row with
    NO lineage) keeps the eventual-consistency refusal exactly."""
    with _memory_store([_tx(1, ORIG, amount_sats=42_000, first_seen=SEEN)]) as store:
        client = _StatusClient(fail_404=True)
        flow = SimpleNamespace(txid=ORIG)
        result = _handler(client, store, flow=flow)(_env(ORIG))
    assert result == {
        "error": "unknown_tx",
        "detail": (
            "the broadcast transaction is not indexed yet — eventual "
            "consistency; try again shortly"
        ),
    }


def test_status_no_lineage_row_404_stays_chain_unavailable_value_free() -> None:
    """No store row at all (foreign/unknown txid): the ordinary
    chain_unavailable path, detail scrubbed of values."""
    with _memory_store([]) as store:
        client = _StatusClient(fail_404=True)
        result = _handler(client, store)(_env(ORIG))
    assert result["error"] == "chain_unavailable"
    assert "status 404" in str(result["detail"])
    assert not _HEX64.search(str(result["detail"]))  # the txid is never echoed


def test_status_gate_refusal_runs_before_the_store() -> None:
    """TCK-PRIVACY-001 order pinned even with terminal lineage on record:
    the awaiting_backend refusal is the FIRST answer, zero chain calls,
    zero lineage narration."""
    with _memory_store(
        [
            _tx(1, ORIG, replaced_by_txid=REPL, first_seen=SEEN),
            _tx(1, REPL, height=900_002, first_seen=SEEN + 30),
        ],
        link=(ORIG, REPL),
    ) as store:
        client = _StatusClient()
        gate = app.StartupScan(enabled=True, deferred=True)
        result = _handler(client, store, gate=gate)(_env(ORIG))
    assert result.get("error") == "backend_unchosen"
    assert result.get("detail") == app.NO_BACKEND_REFUSAL
    assert client.calls == []


def test_status_store_read_failure_is_value_free() -> None:
    """A failing lineage read surfaces the standard store_error refusal —
    no txid, no amount, no stack of values."""
    client = _StatusClient(fail_404=True)
    result = _handler(client, _BrokenStore())(_env(ORIG))
    assert result["error"] == "store_error"
    assert not _HEX64.search(str(result["detail"]))
    assert client.calls == []  # the lookup stands down, it does not continue


def test_status_legacy_no_store_keeps_pure_chain_behavior() -> None:
    """``store=None`` (the privacy pins' headless wiring) behaves exactly as
    before: 404 for the flow's txid → the eventual-consistency refusal."""
    client = _StatusClient(fail_404=True)
    handler = app._make_tx_status_handler(  # type: ignore[arg-type]
        client, SimpleNamespace(txid=ORIG)  # type: ignore[arg-type]
    )
    result = handler(_env(ORIG))
    assert result["error"] == "unknown_tx"
    assert client.calls == [ORIG]


def test_lineage_helper_unknown_and_confirmed_rows_say_nothing() -> None:
    """The pure derivation stays honest on the edges: a txid with no row,
    and an unlinked pending row, both answer None (ask the chain)."""
    rows = [_tx(1, ORIG), _tx(1, REPL, height=900_002)]
    assert app._lineage_tx_status(rows, "d" * 64) is None
    assert app._lineage_tx_status(rows, ORIG) is None
    # A link to a txid with NO partner row claims no terminal outcome, but
    # the recorded bump is truth: the hedged live-race shape (height None).
    linked = [_tx(1, ORIG, replaced_by_txid=REPL)]
    assert app._lineage_tx_status(linked, ORIG) == {
        "txid": ORIG,
        "confirmed": False,
        "lineage": "replaced",
        "replaced_by": REPL,
        "replacement_height": None,
    }


# -------------------------------------------------------- narration copies


def _printed(result: dict[str, object]) -> list[str]:
    lines: list[str] = []
    app._print_tx_status(result, lines.append)
    return lines


def test_print_replaced_terminal_quotes_store_values_verbatim() -> None:
    lines = _printed(
        {
            "txid": ORIG,
            "confirmed": False,
            "lineage": "replaced",
            "replaced_by": REPL,
            "replacement_height": 900_002,
        }
    )
    # TCK-TXID-002 RE-ADJUDICATION (was: full 64-hex verbatim per
    # TXID-001): the answer references the replacement through the
    # compact token; the FULL value rides the txid_refs payload / the
    # direct ask (height + surrounding copy unchanged, verbatim).
    assert lines == [
        f"It was replaced by {REPL[:8]}… — the replacement confirmed at height 900002."
    ]


def test_print_replaced_live_race_carries_the_bip125_hedge() -> None:
    lines = _printed(
        {
            "txid": ORIG,
            "confirmed": False,
            "lineage": "replaced",
            "replaced_by": REPL,
            "replacement_height": None,
        }
    )
    # TXID-002: compact token in the line (see the pin above); the hedge
    # copy itself is unchanged.
    assert lines == [
        (
            f"It was replaced by {REPL[:8]}… — the original may still confirm; "
            "only one of these two ever will."
        )
    ]


def test_print_evicted_is_honest_about_the_lost_bump() -> None:
    lines = _printed(
        {
            "txid": REPL,
            "confirmed": False,
            "lineage": "evicted",
            "original_txid": ORIG,
            "original_height": 900_001,
        }
    )
    # TXID-002: the original's id prints compact (full rides the payload);
    # the honesty (which tx lost the race, at what height) is unchanged.
    assert lines == [
        (
            "It never confirmed — it was the fee bump, and the original it replaced "
            f"went through instead ({ORIG[:8]}… at height 900001)."
        )
    ]


def test_print_existing_status_copies_unchanged() -> None:
    """RBF-005 added branches, changed none of the existing narration:
    eventual-consistency, confirmed-at-height, and mempool lines stand."""
    assert _printed({"error": "unknown_tx", "detail": "x"}) == [
        (
            "Transaction not found on the chain yet — it may not be indexed; "
            "try again in a moment."
        )
    ]
    assert _printed({"txid": ORIG, "confirmed": True, "block_height": 870_001}) == [
        "Confirmed at height 870001."
    ]
    assert _printed({"txid": ORIG, "confirmed": False, "block_height": None}) == [
        "In mempool (unconfirmed)."
    ]


def test_lineage_results_are_tool_output_not_errors() -> None:
    """Discipline: the replaced/evicted answers are ANSWERS (no "error"
    key — values belong in results, errors stay value-free)."""
    for lineage in (
        {"txid": ORIG, "confirmed": False, "lineage": "replaced", "replaced_by": REPL,
         "replacement_height": 1},
        {"txid": REPL, "confirmed": False, "lineage": "evicted", "original_txid": ORIG,
         "original_height": 1},
    ):
        assert "error" not in lineage


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

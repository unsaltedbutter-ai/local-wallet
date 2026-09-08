"""TCK-SCAN-003 pins: the dedicated chain worker + engine-only persistence.

Per ADR-0022 (the threading model ADR-0024 §4 builds on):

* the chain worker runs EVERY scan/watch chain fetch on its own thread and
  holds no ``Store`` reference at all — sqlite's ``check_same_thread`` guard
  is demonstrated real, and the worker-facing API (:func:`fetch_scan`) takes
  a snapshot plan, not a store (the no-store-access pin);
* the ENGINE thread is the only persister: the record set the worker returns
  lands through ``persist_scan_result`` on the engine thread, and a fetch
  that never persisted left the store completely untouched (single atomic
  transaction, fail-closed);
* the non-blocking startup scan interleaves its value-free dots BETWEEN
  turns, persists + narrates its completion when the engine reaps it, and
  the watch probe rides the same worker (the P5-001 cost-note retirement);
* the freshness flag matrix + the ``create_tx`` pre-first-scan refusal are
  dispatcher-owned (never model judgment), fail closed, and value-free.

All hermetic: the chain is an ``httpx.MockTransport`` and every run() touch
point is the scripted stub I/O — no real network, no real model.
"""

from __future__ import annotations

import inspect
import queue
import threading
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.chain import PriceUnavailableError
from localwallet.protocol import IntentName, validate_payload
from localwallet.store import ADDRESS_ALLOCATED, Store
from localwallet.wallet import WalletDescriptor
from localwallet.wallet import scan as wallet_scan
from tests.test_e2e_skeleton import (
    SEND_AMOUNT_SATS,
    SEND_RECIPIENT,
    SEND_UTXO,
    TEST_GAP,
    ZPUB,
    _create_tx_envelope_json,
    _mock_client,
    _scan_handler,
    derive_fixture_addresses,
)


@pytest.fixture
def wallet_store(tmp_path):
    """A real file-backed store with the canonical fixture wallet row."""
    wd = WalletDescriptor.from_key(ZPUB)
    store = Store(tmp_path / "scan-worker.db")
    wallet = store.create_wallet("default", wd.descriptor)
    store.set_active_wallet(wallet.id)
    store.set_setting("gap_limit", str(TEST_GAP))
    yield store, wallet, wd
    store.close()


def _spy_persist(store: Store) -> list[int]:
    """Record the thread identity of every ``persist_scan_result`` call."""
    idents: list[int] = []
    real = store.persist_scan_result

    def spy(*args: Any, **kwargs: Any) -> None:
        idents.append(threading.get_ident())
        real(*args, **kwargs)

    store.persist_scan_result = spy  # type: ignore[method-assign]
    return idents


# ------------------------------------------------- worker / engine discipline


def test_fetch_and_plan_have_no_store_handle_in_the_fetch_phase() -> None:
    """The no-store-access shape (ADR-0022 decision 2): everything the fetch
    phase can see is the immutable :class:`ScanPlan` snapshot + the chain
    client — no ``Store`` parameter exists to touch."""
    params = inspect.signature(wallet_scan.fetch_scan).parameters
    assert list(params) == ["plan", "client", "progress_fn"]
    worker_params = inspect.signature(app.ChainWorker.scan).parameters
    assert list(worker_params) == ["self", "plan"]
    submit = inspect.signature(app.ChainWorker.submit).parameters
    assert list(submit) == ["self", "plan", "on_progress", "on_result"]


def test_store_use_from_a_worker_thread_fails_closed(wallet_store) -> None:
    """The guard the discipline relies on: sqlite's ``check_same_thread``
    refuses ANY store read from a foreign thread — so even a buggy job could
    not read/write the engine-owned connection (mirrors the WEB-001
    bootstrap pin)."""
    store, wallet, _wd = wallet_store
    errors: list[str] = []

    def try_store() -> None:
        try:
            store.get_utxos_for_wallet(wallet.id)
        except BaseException as exc:  # noqa: BLE001 — the point of the probe
            errors.append(type(exc).__name__)

    probe_thread = threading.Thread(target=try_store)
    probe_thread.start()
    probe_thread.join(5)
    assert errors == ["ProgrammingError"]


def test_worker_fetches_off_engine_and_only_the_engine_persists(
    wallet_store, tmp_path
) -> None:
    """End-to-end split (ADR-0022 decisions 2/3): ``plan_scan`` on the
    engine, every chain request on the ONE worker thread, the store
    UNTOUCHED until the engine's own ``persist_scan`` call — then the whole
    write-set lands in the single atomic transaction from the engine."""
    store, wallet, _wd = wallet_store
    engine = threading.get_ident()
    persist_idents = _spy_persist(store)

    chain_threads: set[int] = set()
    addr0 = derive_fixture_addresses(1)[0]
    handler = _scan_handler([], utxos_by_addr={addr0: [SEND_UTXO]})

    def recording(request: httpx.Request) -> httpx.Response:
        chain_threads.add(threading.get_ident())
        return handler(request)

    client = _mock_client(recording)
    worker = app.ChainWorker(client)
    try:
        plan = wallet_scan.plan_scan(store, wallet)
        assert store.get_sync_state(wallet.id, wallet_scan.CURSOR_KEY) is None
        records = worker.scan(plan)  # blocking fetch ON THE WORKER
        # The fetch phase produced records but wrote NOTHING:
        assert store.get_sync_state(wallet.id, wallet_scan.CURSOR_KEY) is None
        assert persist_idents == []
        assert chain_threads and engine not in chain_threads  # off-engine
        assert len(chain_threads) == 1  # the ONE worker thread owns the chain

        summary = wallet_scan.persist_scan(store, records)  # ENGINE persists
        assert persist_idents == [engine]
        assert summary.utxo_count == 1
        assert store.get_sync_state(wallet.id, wallet_scan.CURSOR_KEY) is not None
        assert [u.value_sats for u in store.get_utxos_for_wallet(wallet.id)] == [
            SEND_UTXO["value"]
        ]
    finally:
        worker.stop()
        client.close()


def test_lazy_scan_and_watch_probe_ride_the_worker(wallet_store) -> None:
    """The watch poll (via its probe's ``scan_fn``) and the lazy in-handler
    scan ride the SAME worker: chain I/O off the engine thread, persistence
    on it (the P5-001 "full scan per poll on the engine thread" note is
    retired; the CLI keeps the tick-driven poll shape per ADR-0022/0019)."""
    store, wallet, _wd = wallet_store
    engine = threading.get_ident()
    persist_idents = _spy_persist(store)
    addr0 = derive_fixture_addresses(1)[0]
    handler = _scan_handler([], utxos_by_addr={addr0: [SEND_UTXO]})

    chain_threads: set[int] = set()

    def recording(request: httpx.Request) -> httpx.Response:
        chain_threads.add(threading.get_ident())
        return handler(request)

    client = _mock_client(recording)
    worker = app.ChainWorker(client)
    try:
        flow = app.ScanFlow(store, wallet, worker, gap_limit=TEST_GAP)
        probe = app._make_watch_probe(store, wallet.id, flow.scan_now)
        observed = probe()
        assert engine not in chain_threads and chain_threads  # worker-only
        assert persist_idents == [engine]  # the engine persisted the poll
        assert [o.address for o in observed] == [addr0]  # surfaced verbatim
        assert observed[0].incoming and observed[0].amount_sats == SEND_UTXO["value"]
    finally:
        worker.stop()
        client.close()


# ------------------------------------------------------- the non-blocking pump


def test_scan_events_interleave_between_turns_and_persist_on_the_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The pump contract (ADR-0022 decision 1): a turn runs WHILE the scan
    is still fetching (prompt live, ``in_progress`` gates the turn), the
    value-free dots flow between turns as ``progress`` events, and the
    completion persist + narration happen on the engine thread when the
    delivered record set is reaped — never on the worker."""
    engine = threading.get_ident()
    release = threading.Event()

    def fake_fetch(plan: object, client: object, *, progress_fn=None) -> object:
        assert threading.get_ident() != engine  # the fetch runs on the worker
        assert release.wait(10)  # hold the scan across one full user turn
        assert progress_fn is not None
        progress_fn()
        progress_fn()
        return object()

    def fake_persist(store: object, records: object) -> wallet_scan.ScanSummary:
        assert threading.get_ident() == engine  # the engine persists
        return wallet_scan.ScanSummary(
            wallet_id=1, gap_limit=20, tip_height=0, scanned_at="x", utxo_count=0
        )

    monkeypatch.setattr(wallet_scan, "fetch_scan", fake_fetch)
    monkeypatch.setattr(wallet_scan, "persist_scan", fake_persist)

    store = Store(tmp_path / "pump.db")
    wallet = store.create_wallet("default", "desc")
    worker = app.ChainWorker(None)  # client unused: fetch_scan is faked
    flow = app.ScanFlow(
        store, wallet, worker, gap_limit=None, startup_plan=object()
    )
    commands: queue.Queue[Any] = queue.Queue()
    commands.put("hello")
    commands.put("exit")
    seen: list[tuple[str, bool]] = []

    def spy_turn(*args: Any, **kwargs: Any) -> None:
        seen.append((args[3], flow.gate.in_progress))  # (line, mid-scan?)
        release.set()

    monkeypatch.setattr(app, "_run_turn", spy_turn)
    events: list[app.EngineEvent] = []
    emitter = app.EventEmitter(events.append)
    outputs: list[str] = []
    try:
        app._pump(
            AgentLoop(app.stub_generate, {}),
            outputs.append,
            commands,
            flow=app.TxFlow(),
            session=app.SendSession(),
            table={},
            emitter=emitter,
            scan=flow,
        )
    finally:
        worker.stop()
        store.close()
    # The turn ran DURING the scan (prompt-live reality):
    assert seen == [("hello", True)]
    # Between-turn stream after the turn: one turn marker, then the two
    # bare dots, then the newline that closes the dot line:
    assert [(e.kind, e.payload) for e in events] == [
        (app.EVENT_TURN_END, ""),
        (app.EVENT_PROGRESS, "."),
        (app.EVENT_PROGRESS, "."),
        (app.EVENT_PROGRESS, "\n"),
    ]
    # The completion narration is the engine's, after the persist:
    assert outputs == ["Startup scan complete: 0 UTXOs · tip height 0."]
    assert flow.gate.complete


def test_exit_drains_the_scan_so_its_result_never_vanishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Never-cancel + no-lost-scan: exiting WHILE the startup scan is still
    in flight drains it to completion on the engine thread, so the persist
    + narration happen exactly once before ``run`` returns."""
    persisted: list[int] = []
    done = threading.Event()

    def fake_fetch(plan: object, client: object, *, progress_fn=None) -> object:
        return object()

    def fake_persist(store: object, records: object) -> wallet_scan.ScanSummary:
        persisted.append(1)
        done.set()
        return wallet_scan.ScanSummary(
            wallet_id=1, gap_limit=20, tip_height=7, scanned_at="x", utxo_count=0
        )

    monkeypatch.setattr(wallet_scan, "fetch_scan", fake_fetch)
    monkeypatch.setattr(wallet_scan, "persist_scan", fake_persist)
    store = Store(tmp_path / "drain.db")
    wallet = store.create_wallet("default", "desc")
    worker = app.ChainWorker(None)
    flow = app.ScanFlow(store, wallet, worker, gap_limit=None, startup_plan=object())
    commands: queue.Queue[Any] = queue.Queue()
    commands.put("exit")  # user exits immediately; the scan must still land
    outputs: list[str] = []
    try:
        app._pump(
            AgentLoop(app.stub_generate, {}),
            outputs.append,
            commands,
            flow=app.TxFlow(),
            session=app.SendSession(),
            table={},
            scan=flow,
        )
        assert done.wait(5)
        assert persisted == [1]
        assert outputs == ["Startup scan complete: 0 UTXOs · tip height 7."]
    finally:
        worker.stop()
        store.close()


# ------------------------------------------------------ freshness + the gate


def _gate(state: str) -> app.StartupScan:
    gate = app.StartupScan(enabled=True)
    if state != "pending":
        gate.mark_running()
        if state == "done":
            gate.mark_done()
        elif state == "skipped":
            gate.mark_skipped()
    return gate


def _table(store, wallet, wd, scan_gate, scan_fn):
    return app.build_dispatch_table(
        store,
        wallet,
        wd.parsed,
        None,  # client: fee/price stubs below keep every path local
        scan_fn,
        fee_estimator=SimpleNamespace(
            estimate=lambda target: SimpleNamespace(sat_per_vb=2)
        ),
        price_oracle=SimpleNamespace(
            fresh=lambda: (_ for _ in ()).throw(PriceUnavailableError("stub")),
            sats_to_usd=lambda sats, rate: None,
        ),
        scan_gate=scan_gate,
    )


@pytest.mark.parametrize(
    ("intent_json", "state", "cursor", "expected"),
    [
        # During the first scan: cache (empty or old) answers STALE — the
        # ADR's core honesty case.
        ("get_balance", "running", None, "stale"),
        ("get_balance", "running", "old", "stale"),
        ("get_history", "pending", None, "stale"),
        ("get_history", "running", "old", "stale"),
        ("get_utxos", "running", None, "stale"),
        # First scan completed (cursor present): fresh. Skipped (failed)
        # startup + no cursor: still honest stale.
        ("get_balance", "done", "now", "fresh"),
        ("get_history", "done", "now", "fresh"),
        ("get_utxos", "done", "now", "fresh"),
        ("get_history", "skipped", None, "stale"),
        # No startup scan configured (tests, AUTO_SCAN=0): purely the store
        # cursor decides — never scanned is stale, scanned is fresh.
        ("get_history", "none", None, "stale"),
        ("get_history", "none", "old", "fresh"),
        ("get_utxos", "none", "old", "fresh"),
    ],
)
def test_freshness_matrix(
    wallet_store, intent_json: str, state: str, cursor: str | None, expected: str
) -> None:
    """The deterministic, tool-owned ``freshness`` flag on every cache
    answer (ADR-0022 decision 5/6) — code computes it from the scan's
    completion state; values never change with the flag (narration-only)."""
    store, wallet, wd = wallet_store
    if cursor is not None:
        store.set_sync_state(wallet.id, wallet_scan.CURSOR_KEY, '{"0": 24, "1": 24}')
    gate = None if state == "none" else _gate(state)
    scans: list[int] = []

    def scan_fn() -> object:
        scans.append(1)
        store.set_sync_state(wallet.id, wallet_scan.CURSOR_KEY, '{"0": 24, "1": 24}')
        return object()

    table = _table(store, wallet, wd, gate, scan_fn)
    envelope = validate_payload(f'{{"v": 0, "intent": "{intent_json}", "params": {{}}}}')
    result = table[IntentName(intent_json)](envelope)
    assert result["freshness"] == expected
    if intent_json == "get_balance":
        if gate is not None and gate.in_progress:
            assert scans == []  # no second scan while the worker owns the chain
        elif cursor is None:
            assert len(scans) == 1  # the AUTO_SCAN=0 lazy path still works


def test_new_address_answers_during_the_first_scan(wallet_store) -> None:
    """ADR-0022 decision 6: allocation is chain-free store bookkeeping +
    deterministic derivation — it MAY answer mid-scan (no flag, no gate)."""
    store, wallet, wd = wallet_store
    gate = _gate("running")
    table = _table(store, wallet, wd, gate, lambda: pytest.fail("no scan wanted"))
    envelope = validate_payload('{"v": 0, "intent": "new_address", "params": {}}')
    result = table[IntentName.NEW_ADDRESS](envelope)
    assert result["index"] == 0 and result["address"].startswith("bc1")


def test_mid_scan_allocation_survives_the_persist(wallet_store) -> None:
    """The decision-6 interleaving pin (TCK-SCAN-003 SR fix): ``new_address``
    allocates on the ENGINE thread WHILE the startup fetch is in flight, so
    the plan snapshot predates it. The persist-time merge must keep both:
    ``next_index`` never regresses (1→0 on a fresh wallet) and the fresh
    ``allocated`` row is never downgraded to ``unused``."""
    store, wallet, wd = wallet_store
    gate = _gate("running")
    table = _table(store, wallet, wd, gate, lambda: pytest.fail("no scan wanted"))

    started = threading.Event()
    hold = threading.Event()
    base = _scan_handler([])

    def blocking(request: httpx.Request) -> httpx.Response:
        if not started.is_set():  # park the FIRST chain call mid-fetch
            started.set()
            assert hold.wait(10), "the engine never allocated mid-scan"
        return base(request)

    client = _mock_client(blocking)
    worker = app.ChainWorker(client)
    try:
        plan = wallet_scan.plan_scan(store, wallet)  # pre-allocation snapshot
        job = worker.submit(plan)
        assert started.wait(10)  # the fetch is in flight, parked
        envelope = validate_payload('{"v": 0, "intent": "new_address", "params": {}}')
        assert table[IntentName.NEW_ADDRESS](envelope)["index"] == 0  # engine allocates
        hold.set()
        assert job.done.wait(10) and job.ok  # scan completes over the stale snapshot
        wallet_scan.persist_scan(store, job.value)  # engine persists the merge
    finally:
        worker.stop()
        client.close()
    assert store.get_derivation(wallet.id, 0).next_index == 1  # never 1→0
    assert [r.status for r in store.get_addresses(wallet.id, 0) if r.index == 0] == [
        ADDRESS_ALLOCATED
    ]  # never downgraded


def test_create_tx_refuses_pre_first_scan_then_works_after(wallet_store) -> None:
    """The load-bearing AC (ADR-0022 decision 6): while the first scan is
    incomplete, ``create_tx`` refuses with the friendly value-free line —
    BEFORE any network/store work, without a flow entry, and without
    firing a second scan. Once the scan completed, the gate is simply gone
    (the normal lazy/selection path applies)."""
    store, wallet, wd = wallet_store
    gate = _gate("running")
    scans: list[int] = []
    flow = app.TxFlow()
    table = app.build_dispatch_table(
        store,
        wallet,
        wd.parsed,
        None,
        lambda: scans.append(1),
        flow=flow,
        fee_estimator=SimpleNamespace(
            estimate=lambda target: SimpleNamespace(sat_per_vb=2)
        ),
        price_oracle=SimpleNamespace(
            fresh=lambda: (_ for _ in ()).throw(PriceUnavailableError("stub")),
            sats_to_usd=lambda sats, rate: None,
        ),
        scan_gate=gate,
    )
    envelope = validate_payload(_create_tx_envelope_json())

    result = table[IntentName.CREATE_TX](envelope)
    assert result == {"error": "wallet_loading", "detail": app.WALLET_LOADING_REFUSAL}
    assert scans == []  # refused BEFORE any chain/store work
    assert flow.state is app.TxFlowStatus.IDLE and flow.pending is None
    # Friendly + value-free: no address/amount/txid in the refusal line.
    detail = result["detail"]
    assert SEND_RECIPIENT not in detail and str(SEND_AMOUNT_SATS) not in detail
    assert not any(ch.isdigit() for ch in detail)
    assert "scan" in detail.lower()

    # A different refusal shape never leaks the loading state as a guess:
    # once the first scan COMPLETED, create_tx proceeds (empty cache → the
    # honest insufficient-funds line from real selection, no wallet_loading).
    gate.mark_done()
    store.set_sync_state(wallet.id, wallet_scan.CURSOR_KEY, '{"0": 24, "1": 24}')
    after = table[IntentName.CREATE_TX](envelope)
    assert after.get("error") != "wallet_loading"
    assert after.get("error") == "insufficient_funds"
    assert scans == []  # cursor present → no lazy scan needed either


def test_freshness_fact_reaches_the_model_turn_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model narrates freshness only FROM the tool's flag (decision 5):
    :func:`_run_turn` injects exactly one deterministic ``freshness=stale``
    fact while the first scan has NOT completed — including the skipped
    state after a failed startup scan, where handler results still carry
    ``freshness: stale`` (TCK-SCAN-003 SR fix) — and injects NOTHING once
    complete or disabled (the model can never author a freshness claim
    of its own)."""
    captured: list[dict] = []

    def fake_run(self, line, facts):  # instance patch of AgentLoop.run
        captured.append(dict(facts))
        return SimpleNamespace(
            status=None, envelope=None, result=None, user_message=None, turns_used=0
        )

    monkeypatch.setattr(AgentLoop, "run", fake_run)
    monkeypatch.setattr(app, "_print_turn", lambda *a, **k: None)
    loop = AgentLoop(app.stub_generate, {})

    gate = _gate("running")
    app._run_turn(
        loop, app.TxFlow(), app.SendSession(), "hi", lambda _s: None,
        table={}, scan_gate=gate,
    )
    assert captured[-1]["freshness"] == "stale"

    # Skipped (startup scan failed): the first scan never completed, the
    # turn FACT must match the handlers' stale flag, not vanish.
    app._run_turn(
        loop, app.TxFlow(), app.SendSession(), "hi", lambda _s: None,
        table={}, scan_gate=_gate("skipped"),
    )
    assert captured[-1]["freshness"] == "stale"

    gate.mark_running()
    gate.mark_done()
    app._run_turn(
        loop, app.TxFlow(), app.SendSession(), "hi", lambda _s: None,
        table={}, scan_gate=gate,
    )
    assert "freshness" not in captured[-1]

    # No startup scan configured (AUTO_SCAN=0): unchanged — nothing injected.
    app._run_turn(
        loop, app.TxFlow(), app.SendSession(), "hi", lambda _s: None,
        table={}, scan_gate=app.StartupScan(enabled=False),
    )
    assert "freshness" not in captured[-1]


# ------------------------------------------------------------ CLI rendering


def test_printers_surface_the_stale_flag_verbatim_of_tool_values(capsys) -> None:
    """The deterministic narration prints cached figures VERBATIM and adds
    only the value-free note when the tool flagged the answer; a fresh
    answer renders byte-identically to the pre-split output."""
    lines: list[str] = []
    app._print_balance(
        {
            "confirmed_sats": 1234,
            "unconfirmed_sats": 0,
            "total_sats": 1234,
            "addresses_scanned": 1,
            "tip_height": 870000,
            "freshness": "stale",
        },
        lines.append,
    )
    assert lines[0] == "Balance (mainnet): 1234 sats (confirmed) + 0 sats (unconfirmed)"
    assert lines[-1] == app.FRESHNESS_NOTE
    assert not any(ch.isdigit() for ch in app.FRESHNESS_NOTE)

    lines.clear()
    app._print_balance(
        {
            "confirmed_sats": 1234,
            "unconfirmed_sats": 0,
            "total_sats": 1234,
            "addresses_scanned": 1,
            "tip_height": 870000,
            "freshness": "fresh",
        },
        lines.append,
    )
    assert lines == [
        "Balance (mainnet): 1234 sats (confirmed) + 0 sats (unconfirmed)",
        "Total 1234 sats · 1 addresses with UTXOs · tip height 870000",
    ]

    lines.clear()
    app._print_history({"transactions": [], "shown": 0, "freshness": "stale"}, lines.append)
    assert lines == [app.FRESHNESS_NOTE, "No transactions found."]

    lines.clear()
    app._print_utxos({"utxos": [], "count": 0, "freshness": "stale"}, lines.append)
    assert lines == [app.FRESHNESS_NOTE, "No unspent outputs."]


def test_print_create_tx_renders_the_loading_refusal(capsys) -> None:
    lines: list[str] = []
    app._print_create_tx(
        {"error": "wallet_loading", "detail": app.WALLET_LOADING_REFUSAL}, lines.append
    )
    assert lines == [app.WALLET_LOADING_REFUSAL]  # friendly line, not an error dump
    assert capsys.readouterr().out == ""

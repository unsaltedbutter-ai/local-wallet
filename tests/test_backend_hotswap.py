"""TCK-BACKEND-002 — the chain-client HOT-SWAP, the resync trigger, ssl://
acceptance at every entry point, and server-side backend-kind detection.

The contract (USER DIRECTION 2026-09-09 items 5/6/8/9/10; ADR-0018 amendment
— hot-swap supersedes the config-only-restart semantics for the STORED rung):

* a ``chain_base_url`` write that APPLIES (web POST /settings or the /setup
  conversation) swaps the live client ON THE ENGINE THREAD: the old client
  is closed BOUNDED, the new one is built through ``_build_chain_client``
  (scheme still picks the adapter), the worker + the fee/price-riding
  dispatch handlers rebind, and a FULL rebuild resync fires (direction 5/6);
* the probe runs BEFORE the save (deliverable 2): unreachable / foreign-chain
  → refused VALUE-FREE, nothing stored, the old client untouched
  (fail-closed — never left clientless);
* concurrency rule PINNED: a swap never crosses an in-flight scan — while a
  fetch owns the worker the (validated, stored) swap DEFERS and installs the
  moment the scan's ``_ScanDone`` has been persisted;
* ``ssl://`` is accepted everywhere (M3 acceptance, direction 9): the store's
  typed writer, the settings path, the /setup entry (pinned in
  test_setup_command.py); the Electrum probe reuses M1's handshake genesis
  gate via ``_probe_chain_backend``;
* ``resync_now`` (direction 6) re-runs the full SCAN-003 rebuild scan and
  TAGS SURVIVE — ``coin_labels`` is a separate table, never in the scan
  write-set (pinned below, pre- and post-hot-swap);
* a ``gap_limit`` apply whose value ACTUALLY CHANGED fires the same resync
  (direction 8) — UNLESS it NARROWED the window (TCK-GAP-001), which is
  applied WITHOUT an auto-rescan (``resync: "no_rescan"``) plus the
  value-free tradeoff line (a smaller window can only hide addresses); an
  unchanged apply says so (``resync: "unchanged"``) and starts no scan;
* ``backend_kind`` (direction 10, re-scoped by TCK-DESCOPE-M3B): the CLOSED
  enum name of the live backend ({none, electrum, bitcoind} — the
  public/mempool/esplora URL-shape kinds are gone; the public consent
  installs an ssl:// server and reports ``electrum``) rides /settings and
  /state additively — value-free.

All hermetic: fake duck-typed chain clients, tmp stores, injected probes.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.app import (
    BACKEND_KINDS,
    BACKEND_PROBE_FAIL,
    ChainBackendFlow,
    Settings,
    StartupScan,
    _backend_kind,
    _Wiring,
)
from localwallet.chain import TxStatus
from localwallet.config import resolve_chain_base_url
from localwallet.protocol import Envelope, IntentName, TxStatusParams
from localwallet.store import Store
from localwallet.wallet import WalletDescriptor
from localwallet.wallet import scan as wallet_scan
from tests.test_e2e_skeleton import ZPUB

GOOD_URL = "http://127.0.0.1:3006/api"
NEW_URL = "https://mempool.mine.example:4000/api"
SSL_URL = "ssl://evil-star.local:50001"


def _probe_true(url: str) -> str | None:
    # The M3 seam contract: candidate in, CANONICAL URL out (None refuses).
    # Identity = "the probe agreed it already is what it looks like".
    return url


class _FakeChain:
    """Duck-typed ChainClient: answers a fresh-wallet scan with empties and
    counts its own fetch/status/close traffic (which client SERVED is the
    story these tests read)."""

    supports_price = False

    def __init__(self, base_url: str, hold: threading.Event | None = None) -> None:
        self.base_url = base_url
        self.closed = False
        self.fetches = 0
        self.status_calls = 0
        #: Optional test brake: a fetch that must NOT finish on its own
        #: timing (deterministic mid-scan swap/busy pins).
        self.hold = hold

    def close(self) -> None:
        self.closed = True

    def get_tip_height(self) -> int:
        return 900_000

    def get_address_txs(self, address: str) -> list[dict[str, Any]]:
        if self.hold is not None:
            self.hold.wait(5.0)
        self.fetches += 1
        return []

    def get_address_utxos(self, address: str) -> list[dict[str, Any]]:
        return []

    def get_tx_status(self, txid: str) -> TxStatus:
        self.status_calls += 1
        return TxStatus(
            txid=txid, confirmed=True, block_height=900_000, block_time=None
        )


def _mk_wiring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stored_url: str | None = None,
    boot_backend: str = "",
    gap: int = 2,
) -> tuple[_Wiring, queue.Queue[Any]]:
    """A REAL wiring (store/worker/scan/table) over fake chain clients.
    ``_build_chain_client`` is patched at the app module — the swap's build
    step still runs through it, so the test observes the production path.
    The scan flow is attached to a bare command queue (the pump role the
    tests play by hand via ``_drain``)."""
    built: list[_FakeChain] = []
    monkeypatch.setattr(
        app, "_build_chain_client", lambda settings, auth=None: _built(built, settings)
    )

    def _built(sink: list[_FakeChain], settings: Settings) -> _FakeChain:
        client = _FakeChain(
            resolve_chain_base_url(settings.chain_base_url, None) or ""
        )
        sink.append(client)
        return client

    store = Store(tmp_path / "swap.db")
    wd = WalletDescriptor.from_key(ZPUB)
    wallet = store.create_wallet("default", wd.descriptor)
    store.set_active_wallet(wallet.id)
    store.set_setting(wallet_scan.GAP_LIMIT_SETTING, str(gap))
    if stored_url is not None:
        store.set_chain_base_url(stored_url)
    settings = Settings(
        store_path=str(tmp_path / "swap.db"), chain_base_url=boot_backend or ""
    )
    initial = _FakeChain(
        resolve_chain_base_url(settings.chain_base_url, stored_url) or ""
    )
    worker = app.ChainWorker(initial)  # type: ignore[arg-type]
    scan = app.ScanFlow(store, wallet, worker, gap_limit=None)
    commands: queue.Queue[Any] = queue.Queue()
    scan.attach(commands)
    flow = app.TxFlow()
    session = app.SendSession()
    table = app.build_dispatch_table(
        store,
        wallet,
        wd.parsed,
        initial,  # type: ignore[arg-type]
        scan.scan_now,
        flow=flow,
        session=session,
        settings=settings,
        scan_gate=scan.gate,
        signer_selection=app.SignerSelection(
            kind="file", dir_path=tmp_path / "psbt", fingerprint_hex="00000000"
        ),
        node_detect_fn=lambda: None,  # never called on these paths
    )
    wiring = _Wiring(
        store=store,
        client=initial,  # type: ignore[arg-type]
        loop=AgentLoop(app.stub_generate, table),
        flow=flow,
        session=session,
        table=table,
        watcher=None,
        worker=worker,
        scan=scan,
        settings=settings,
        parsed=wd.parsed,
        wallet=wallet,
        boot_backend=boot_backend,
    )
    return wiring, commands


def _mk_flow(
    wiring: _Wiring,
    commands: queue.Queue[Any],
    *,
    probe_ok: bool = True,
) -> tuple[ChainBackendFlow, list[str]]:
    """The swap controller over a counting probe; returns it with the probe's
    call log (URLs it saw — never echoed by the refusal, pinned separately)."""
    seen: list[str] = []

    def probe(url: str) -> str | None:
        seen.append(url)
        return url if probe_ok else None

    return ChainBackendFlow(wiring, probe), seen


def _drain(
    wiring: _Wiring,
    commands: queue.Queue[Any],
    *,
    timeout: float = 5.0,
) -> list[str]:
    """Play the pump's scan-event handling until the running scan (if any)
    completes; returns the narration lines."""
    scan = wiring.scan
    assert scan is not None
    out: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not scan.pending:
            return out
        try:
            command = commands.get(timeout=0.1)
        except queue.Empty:
            continue
        scan.handle_command(command, out.append, None)


@pytest.fixture
def env_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(app.CHAIN_BASE_URL_ENV_VAR, raising=False)
    monkeypatch.delenv(app.GAP_LIMIT_ENV_VAR, raising=False)
    monkeypatch.delenv(app.SIGNER_ENV_VAR, raising=False)


# ------------------------------------------------------ the hot-swap matrix


def test_apply_closes_old_serves_from_new_and_fires_rescan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """Directions 5+6: an applied chain_base_url write deallocated the old
    client (bounded close), rebound the worker + the chain-riding handlers
    onto the new one, and triggered the full rebuild rescan — which SERVES
    from the new client."""
    wiring, commands = _mk_wiring(tmp_path, monkeypatch)
    old = wiring.client
    table = wiring.table
    handlers_before = (
        table[IntentName.CREATE_TX],
        table[IntentName.BROADCAST_TX],
        table[IntentName.TX_STATUS],
    )
    untouched_before = table[IntentName.NEW_ADDRESS]
    error, fields = ChainBackendFlow(wiring, _probe_true).apply(NEW_URL)
    assert fields == {"swapped": True, "resync": "started"} and error is None
    new = wiring.worker._client
    assert new is not old and wiring.client is new
    assert old.closed is True  # BOUNDED close of the retired client
    assert wiring.settings.chain_base_url == NEW_URL  # the ONE selection point
    assert wiring.store.get_chain_base_url() == NEW_URL  # stored rung moved
    assert wiring.scan is not None and wiring.scan.gate.state == "running"
    narration = _drain(wiring, commands)
    assert new.fetches > 0  # the resync rode the NEW client…
    assert old.fetches == 0  # …and only ever the new one
    assert any("Rescan complete" in line for line in narration)
    # The SAME table dict, three entries rebuilt over the new client — the
    # loop/pump references never move (identity pin), the store-only
    # handlers are structurally untouched, and a subsequent handler call is
    # served by the NEW client.
    assert wiring.table is table
    handlers_after = (
        table[IntentName.CREATE_TX],
        table[IntentName.BROADCAST_TX],
        table[IntentName.TX_STATUS],
    )
    assert all(a is not b for a, b in zip(handlers_after, handlers_before))
    assert table[IntentName.NEW_ADDRESS] is untouched_before
    result = table[IntentName.TX_STATUS](
        Envelope(
            v=0,
            intent=IntentName.TX_STATUS,
            params=TxStatusParams(txid="ab" * 32),
        )
    )
    assert result["confirmed"] is True
    assert new.status_calls == 1 and old.status_calls == 0
    wiring.store.close()


def _closure_value(fn: object, name: str) -> object:
    """One freevar cell of a handler closure (the oracle the rebuilt handler
    actually carries — narration-free introspection of the shipped path)."""
    cells = dict(zip(fn.__code__.co_freevars, fn.__closure__))  # type: ignore[attr-defined]
    return cells[name].cell_contents  # type: ignore[attr-defined]


def test_hot_swap_rebinds_one_shared_price_oracle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """TCK-FIAT-001 security-review LOW (folded into TCK-UX-009): the
    post-swap GET_BALANCE and CREATE_TX rebuilds must share ONE fresh
    PriceOracle over the new client — the single-cache invariant the
    initial wiring (build_dispatch_table) establishes — not two private
    caches."""
    wiring, commands = _mk_wiring(tmp_path, monkeypatch)
    built: list[object] = []

    def _counting_oracle(client: object, **_kwargs: object) -> object:
        # TCK-FIAT-002: the rebind passes the live display-currency reader
        # as a kwarg — accepted and ignored here (the client identity is
        # what this pin counts).
        built.append(client)
        return client  # never fetched on this path

    monkeypatch.setattr(app, "PriceOracle", _counting_oracle)
    error, fields = ChainBackendFlow(wiring, _probe_true).apply(NEW_URL)
    assert error is None and fields["swapped"] is True
    _drain(wiring, commands)
    assert len(built) == 1  # ONE oracle for the whole rebind
    assert _closure_value(
        wiring.table[IntentName.GET_BALANCE], "price_oracle"
    ) is built[0]
    assert _closure_value(
        wiring.table[IntentName.CREATE_TX], "price_oracle"
    ) is built[0]
    wiring.store.close()


def test_probe_failure_refuses_value_free_old_client_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """Deliverable 2, fail-closed: an unreachable/foreign-chain URL is
    refused BEFORE the write lands — nothing stored, the old client keeps
    serving, and the refusal carries no value (the URL never rides back)."""
    wiring, commands = _mk_wiring(tmp_path, monkeypatch, stored_url=GOOD_URL)
    old = wiring.client
    flow, seen = _mk_flow(wiring, commands, probe_ok=False)
    error, fields = flow.apply(NEW_URL)
    assert error == BACKEND_PROBE_FAIL
    assert NEW_URL not in str(error) and "mempool.mine" not in str(error)
    assert fields == {}
    assert seen == [NEW_URL]  # the probe ran (that is what refused)
    assert wiring.store.get_chain_base_url() == GOOD_URL  # NOTHING stored
    assert wiring.client is old and old.closed is False  # still serving
    assert wiring.settings.chain_base_url == ""  # boot rung unchanged here
    assert wiring.scan is not None and wiring.scan.gate.state == "disabled"


def test_mid_scan_swap_defers_until_scan_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """The PINNED concurrency rule (deliverable 1): a swap never crosses an
    in-flight scan. The write validates + stores immediately, the INSTALL
    defers; the old client keeps serving (its close is the install's, not
    the write's), and the moment the scan's _ScanDone has been persisted the
    deferred swap lands and its own resync runs."""
    wiring, commands = _mk_wiring(tmp_path, monkeypatch)
    assert wiring.scan is not None
    wiring.scan.set_startup(
        wallet_scan.plan_scan(wiring.store, wiring.wallet, rebuild=False)
    )
    wiring.scan.begin()  # a startup fetch is now in flight on the OLD client
    old = wiring.client
    flow, _seen = _mk_flow(wiring, commands)
    error, fields = flow.apply(NEW_URL)
    assert error is None
    assert fields == {"swapped": False, "resync": "deferred"}
    assert wiring.store.get_chain_base_url() == NEW_URL  # stored NOW
    assert wiring.client is old and old.closed is False  # still serving
    # The in-flight scan completes → the pump's take_deferred lands the swap.
    _drain(wiring, commands)
    assert flow.take_deferred() is True
    new = wiring.client
    assert new is not old and old.closed is True
    assert wiring.scan.gate.state == "running"  # the swap's resync is on
    _drain(wiring, commands)
    assert wiring.scan.gate.state == "done"
    wiring.store.close()


def test_clear_write_without_consent_is_refused_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """TCK-DESCOPE-M3A: with no public default to swap BACK to, a plain
    ``""`` settings-clear is REFUSED value-free (building an unresolved
    wallet client fails closed) — the store and the live client untouched,
    the current backend still serving. The sanctioned way to change the
    backend is a NEW address (probe→store→swap) or the warned public
    revert (which records the consent marker and installs the public
    Electrum server — see the install_saved("") pins)."""
    wiring, commands = _mk_wiring(tmp_path, monkeypatch, stored_url=GOOD_URL)
    old = wiring.client
    flow, seen = _mk_flow(wiring, commands)

    # Faithfully mirror the production build: an empty chain_base_url raises
    # (there is no wallet client to build while unresolved) — _mk_wiring's
    # fake is too permissive to exercise this guard itself.
    def _strict_build(settings, auth=None):
        if not settings.chain_base_url.strip():
            raise ValueError("unresolved")
        return wiring.client  # any object: never used (build fails first)

    monkeypatch.setattr(app, "_build_chain_client", _strict_build)
    error, fields = flow.apply("")
    assert error == BACKEND_PROBE_FAIL and fields == {}
    assert seen == []  # nothing probed, nothing stored, nothing swapped
    assert wiring.store.get_chain_base_url() == GOOD_URL
    assert wiring.client is old and not old.closed
    wiring.store.close()


def test_env_rung_shadows_the_stored_write_no_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """The ladder rule that SURVIVES the amendment: an env/config-file rung
    outranks the stored write — the value is probed + stored (next-launch
    truth) but the live client is NOT swapped (it serves the higher rung).
    The response's honesty: swapped False, resync skipped, requires_restart
    stays True."""
    wiring, commands = _mk_wiring(
        tmp_path, monkeypatch, boot_backend="https://operator.box:4000/api"
    )
    old = wiring.client
    flow, seen = _mk_flow(wiring, commands)
    error, fields = flow.apply(NEW_URL)
    assert error is None
    assert fields == {"swapped": False, "resync": "skipped"}
    assert seen == [NEW_URL]  # the save gate still probes
    assert wiring.store.get_chain_base_url() == NEW_URL  # stored, shadowed
    assert wiring.client is old and old.closed is False  # not swapped
    entry = next(
        e
        for e in app._settings_entries(wiring.store, flow)
        if e["key"] == "chain_base_url"
    )
    assert entry["requires_restart"] is True  # the honest flag under shadowing
    wiring.store.close()


def test_settings_entry_requires_restart_flips_with_the_swap_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """The ADR-0018 AMENDMENT, pinned: with an engine swap controller wired
    a stored chain_base_url write needs NO restart — the entry's honest flag
    is False (and True only while an env/config-file rung shadows it)."""
    wiring, commands = _mk_wiring(tmp_path, monkeypatch)
    flow, _seen = _mk_flow(wiring, commands)
    entry = next(
        e
        for e in app._settings_entries(wiring.store, flow)
        if e["key"] == "chain_base_url"
    )
    assert entry["requires_restart"] is False
    bare = next(
        e
        for e in app._settings_entries(wiring.store, None)
        if e["key"] == "chain_base_url"
    )
    assert bare["requires_restart"] is True  # no wiring → plain store write
    wiring.store.close()


# ---------------------------------------------------------------- resync now


def test_resync_now_reruns_full_scan_and_tags_survive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """Deliverable 5 (direction 6): ``resync_now`` re-runs the FULL rebuild
    scan (the --rescan semantics, "as though the zpub had been entered for
    the first time") — and COIN TAGS SURVIVE: ``coin_labels`` is a separate
    table, never in the scan write-set (store/db.py's contract, pinned here
    for both a plain resync AND the hot-swap resync)."""
    wiring, commands = _mk_wiring(tmp_path, monkeypatch)
    scan = wiring.scan
    assert scan is not None
    scan.gate = StartupScan(enabled=True)  # a startup scan completed already
    scan.gate.mark_done()
    label = wiring.store.set_coin_label(
        wiring.wallet.id, "ab" * 32, 0, tags=("kyc",), note="mine"
    )
    assert label is not None
    assert scan.resync_now() is True
    assert scan.gate.state == "running"
    narration = _drain(wiring, commands)
    assert scan.gate.state == "done"
    assert any("Rescan complete" in line for line in narration)
    # Tags survived the rescan…
    assert wiring.store.get_coin_label(wiring.wallet.id, "ab" * 32, 0) is not None
    # …and survive the HOT-SWAP-TRIGGERED resync too:
    flow, _seen = _mk_flow(wiring, commands)
    error, fields = flow.apply(NEW_URL)
    assert error is None and fields["resync"] == "started"
    _drain(wiring, commands)
    kept = wiring.store.get_coin_label(wiring.wallet.id, "ab" * 32, 0)
    assert kept is not None and kept.tags == ("kyc",) and kept.note == "mine"
    wiring.store.close()


def test_resync_now_concurrency_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """One scan at a time is STRUCTURAL: while a fetch owns the worker the
    resync refuses (``busy``), and a held first-run scan (awaiting a backend
    choice) refuses too — the hold resolves via the backend choice, which
    now swaps + releases (pinned above)."""
    wiring, commands = _mk_wiring(tmp_path, monkeypatch)
    scan = wiring.scan
    assert scan is not None
    scan.set_startup(
        wallet_scan.plan_scan(wiring.store, wiring.wallet, rebuild=False)
    )
    scan.begin()
    assert scan.resync_now() is False  # in-flight: refused, single worker
    _drain(wiring, commands)
    assert scan.resync_now() is True  # free again: the full scan re-runs
    _drain(wiring, commands)
    scan.gate.mark_skipped()
    scan.gate = StartupScan(enabled=True, deferred=True)
    assert scan.resync_now() is False  # held for the choice: nothing to resync
    wiring.store.close()


def test_resync_request_is_answered_on_the_engine_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """The typed ``resync_now`` pump command + the transport seam: the reply
    is the closed value-free status, the whole flow (store, worker fetch,
    persist) runs on the ENGINE thread, and no chain I/O touches that
    thread's turn (the fetch rides the worker; the engine only persists)."""
    holder: dict[str, Any] = {}
    booted = threading.Event()

    def bootstrap() -> app.EngineContext:
        # The store + wiring are CONSTRUCTED on the engine thread (the
        # check_same_thread contract the real web bootstrap obeys).
        wiring, _commands = _mk_wiring(tmp_path, monkeypatch)
        flow = ChainBackendFlow(wiring, _probe_true)
        holder["wiring"] = wiring
        holder["flow"] = flow
        holder["engine"] = threading.get_ident()
        booted.set()
        return app.EngineContext(
            loop=wiring.loop,
            flow=wiring.flow,
            session=wiring.session,
            table=wiring.table,
            scan=wiring.scan,
            store=wiring.store,
            client=wiring.client,  # type: ignore[arg-type]
            backend=flow,
        )

    events: list[app.EngineEvent] = []
    handle = app.start_engine(bootstrap, events.append)
    assert booted.wait(5.0)
    # Brake the fake fetch so the resync CANNOT complete on its own timing:
    # the busy answer below is deterministic, not a race.
    hold = threading.Event()
    holder["wiring"].client.hold = hold
    try:
        first = handle.request_resync(5.0)
        assert first is not None
        assert first["schema"] == "resync/1" and first["status"] == "started"
        # The resync owns the worker NOW: a direct second trigger answers the
        # closed ``busy`` (the deterministic guard).
        assert holder["flow"].resync() == "busy"
    finally:
        hold.set()
        handle.shutdown()
        assert handle.thread is not None
        handle.thread.join(10)
    assert handle.error is None
    assert holder["engine"] != threading.get_ident()
    assert holder["wiring"].scan is not None
    # The session-end drain completed (persisted) the running resync.
    assert holder["wiring"].scan.gate.state == "done"


# ------------------------------------------------- gap-limit → resync (dir 8)


def test_gap_limit_changed_fires_resync_unchanged_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """Direction 8: applying a gap_limit that RAISED the value syncs against
    the chain base (the same full resync); an unchanged apply starts NO scan
    and SAYS SO in the reply. TCK-GAP-001: a NARROWING apply stores the value
    WITHOUT any auto-rescan (``resync: "no_rescan"``) and narrates the
    value-free tradeoff note."""
    wiring, commands = _mk_wiring(tmp_path, monkeypatch)
    flow = ChainBackendFlow(wiring, _probe_true)
    store = wiring.store
    # Raise: stored gap is 2 (the _mk_wiring default), applying 5 widens the
    # window → the normal full resync fires.
    reply = app.handle_settings_request(store, "gap_limit", "5", flow)
    assert reply["status"] == "applied"
    assert reply["resync"] == "started"
    assert wiring.scan is not None and wiring.scan.gate.state == "running"
    _drain(wiring, commands)
    # Unchanged apply: the same canonical value → no rescan, stated plainly.
    reply = app.handle_settings_request(store, "gap_limit", "5", flow)
    assert reply["status"] == "applied"
    assert reply["resync"] == "unchanged"
    assert wiring.scan.gate.state == "done"  # NOT re-armed
    # Whitespace canonicalizes to the same value → still unchanged.
    reply = app.handle_settings_request(store, "gap_limit", " 5 ", flow)
    assert reply["resync"] == "unchanged"
    # TCK-GAP-001: NARROW the window (5 → 3). The value is stored but NO
    # auto-rescan is started (a smaller window can only hide addresses),
    # and the reply carries the value-free tradeoff narration.
    reply = app.handle_settings_request(store, "gap_limit", "3", flow)
    assert reply["status"] == "applied"
    assert store.get_setting(wallet_scan.GAP_LIMIT_SETTING) == "3"  # stored
    assert reply["resync"] == "no_rescan"
    assert wiring.scan.gate.state == "done"  # still NOT re-armed
    assert reply["note"] == app.GAP_NARROW_NOTE
    assert app.GAP_NARROW_NOTE == (
        "A smaller window may hide addresses beyond it; your existing "
        "derivation state is kept, and raising the value again (then "
        "re-syncing) will show them again."
    )
    # A refused write never resyncs (fail-closed before the store too).
    reply = app.handle_settings_request(store, "gap_limit", "99999", flow)
    assert reply["status"] == "rejected" and "resync" not in reply
    wiring.store.close()


def test_settings_write_path_surfaces_swap_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """The web POST /settings response shape: applied chain writes carry
    ``swapped``/``resync`` and the entry confirms from re-read tool truth;
    a refused probe answers the closed ``rejected`` status value-free."""
    wiring, commands = _mk_wiring(tmp_path, monkeypatch)
    ok_flow = ChainBackendFlow(wiring, _probe_true)
    reply = app.handle_settings_request(
        wiring.store, "chain_base_url", NEW_URL, ok_flow
    )
    assert reply["status"] == "applied"
    assert reply["swapped"] is True and reply["resync"] == "started"
    assert reply["settings"][0]["value"] == NEW_URL
    assert reply["settings"][0]["requires_restart"] is False
    _drain(wiring, commands)
    bad_flow = ChainBackendFlow(wiring, lambda _url: None)
    reply = app.handle_settings_request(
        wiring.store, "chain_base_url", "https://sneaky.example/api", bad_flow
    )
    assert reply["status"] == "rejected"
    assert "sneaky" not in str(reply)  # value-free refusal
    assert wiring.store.get_chain_base_url() == NEW_URL  # unchanged
    wiring.store.close()


# ---------------------------------------------------- ssl:// acceptance (9)


class _FakeElectrum:
    """Constructor-side stand-in for the Electrum adapter (the handshake +
    genesis gate are chain/'s, exercised in test_chain_electrum.py)."""

    supports_price = False

    def __init__(self, base_url: str = "", **_kw: Any) -> None:
        self.base_url = base_url
        self.closed = False
        self.tip_calls = 0

    def get_tip_height(self) -> int:
        self.tip_calls += 1  # forces the M1 handshake (the genesis gate)
        return 900_000

    def close(self) -> None:
        self.closed = True


def test_store_typed_writer_accepts_ssl_shape(tmp_path: Path) -> None:
    """The stored rung now carries ``ssl://host[:port]`` (user example
    verbatim); shape refusals stay fail-closed and value-free (mirroring
    ChainConfig — the deep gate), so no stored value can crash the client
    construction at startup."""
    store = Store(tmp_path / "ssl.db")
    try:
        for good in (SSL_URL, "ssl://host", "ssl://127.0.0.1:50001"):
            store.set_chain_base_url(good)
            assert store.get_chain_base_url() == good
        for bad in (
            "ssl://",
            "ssl://host:port",
            "ssl://host:99999",
            "ssl://host/p",
            "ssl://user:pass@host",
            "ssl://ho st",
            "gopher://x",
        ):
            with pytest.raises(app.StoreError) as exc:
                store.set_chain_base_url(bad)
            # Value-free like every other store refusal: the submitted URL
            # (host, credentials, port) never rides the message.
            assert bad not in str(exc.value)
        assert store.get_chain_base_url() == "ssl://127.0.0.1:50001"
    finally:
        store.close()


def test_settings_path_accepts_ssl_and_installs_the_electrum_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """The web path end-to-end for ssl:// (probe mocked at the transport
    seam, the adapter faked at the construction seam): stored, swapped onto
    the ELECTUM-kind client, resync fired."""
    wiring, commands = _mk_wiring(tmp_path, monkeypatch)
    old = wiring.client
    flow, seen = _mk_flow(wiring, commands)
    error, fields = flow.apply(SSL_URL)
    assert error is None and fields["swapped"] is True
    assert seen == [SSL_URL]
    assert wiring.store.get_chain_base_url() == SSL_URL
    assert old.closed is True and wiring.client is not old
    assert wiring.client.base_url == SSL_URL  # the fake build carried the URL
    _drain(wiring, commands)
    wiring.store.close()


def test_probe_dispatches_by_scheme_reusing_the_m1_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_probe_chain_backend`` (TCK-DESCOPE-M3B contract): returns the
    CANONICAL URL to store, or ``None`` to refuse. ssl:// constructs the
    Electrum adapter and forces ONE tip call — the M1 handshake's genesis
    gate IS the mainnet proof. http(s) is a Core-RPC INPUT ALIAS ONLY: a
    Core win returns the canonical rewrite (bitcoind:// /
    bitcoind+tls://); a URL that does NOT answer in Core shape is REFUSED
    — the Esplora-shape fallback is gone and with it the app module's
    ``check_backend`` import (the M3A interim seam where an Esplora URL
    could still be persisted is closed). Everything collapses to None
    value-free, and the probe client is always closed."""
    settings = Settings(request_timeout_s=0.5, max_retries=3)
    assert not hasattr(app, "check_backend")  # the seam is GONE, not idle
    calls: list[str] = []
    built: list[_FakeElectrum] = []

    def fake_electrum(base_url: str = "", **kw: Any) -> _FakeElectrum:
        calls.append(f"electrum:{base_url}:{kw.get('max_retries')}")
        client = _FakeElectrum(base_url)
        built.append(client)
        return client

    monkeypatch.setattr(app, "ElectrumClient", fake_electrum)
    assert app._probe_chain_backend(SSL_URL, settings) == SSL_URL
    assert calls[-1] == f"electrum:{SSL_URL}:1"  # the snappy budget, M1 gate
    assert built[0].tip_calls == 1 and built[0].closed is True

    def explode(_base_url: str = "", **_kw: Any) -> _FakeElectrum:
        raise RuntimeError("ssl://boom")  # escaping surprise must collapse

    monkeypatch.setattr(app, "ElectrumClient", explode)
    assert app._probe_chain_backend(SSL_URL, settings) is None

    def fake_core(base_url: str = "", **_kw: Any) -> _FakeElectrum:
        calls.append(f"core:{base_url}")
        return _FakeElectrum(base_url)

    monkeypatch.setattr(app, "BitcoindClient", fake_core)
    # The ambiguous rungs answer ONLY in Core shape, and answer REWRITTEN:
    # http:// → bitcoind://, https:// → the TLS sibling (one scheme seam).
    assert app._probe_chain_backend(GOOD_URL, settings) == (
        "bitcoind://" + GOOD_URL.partition("://")[2]
    )
    assert app._probe_chain_backend(NEW_URL, settings) == (
        "bitcoind+tls://" + NEW_URL.partition("://")[2]
    )

    def dead_core(base_url: str = "", **_kw: Any) -> _FakeElectrum:
        raise RuntimeError("not a Core RPC here")  # Esplora shape = this

    monkeypatch.setattr(app, "BitcoindClient", dead_core)
    assert app._probe_chain_backend(NEW_URL, settings) is None  # no fallback
    # URL-embedded credentials never reach the Core branch: no client built.
    before = len(calls)
    assert app._probe_chain_backend("http://u:p@host:8332", settings) is None
    assert len(calls) == before


# ------------------------------------------------ kind detection (direction 10)


@pytest.mark.parametrize(
    ("url", "resolved", "expected"),
    [
        ("", False, "none"),  # first-run choice unmade: nothing is consulted
        ("https://mempool.space/api", False, "none"),  # … even with a client
        ("ssl://evil-star.local:50001", True, "electrum"),
        ("ssl://host", True, "electrum"),
        # TCK-DESCOPE-M3B: the kind is a SCHEME read over the two wallet
        # families only. The consented public Electrum server is an ssl://
        # URL and answers ``electrum`` (trust rides privacy_mode, not the
        # kind); the Core rewrite schemes answer ``bitcoind``; a legacy
        # http(s) Esplora-shape rung can build no wallet client (the
        # construction seam refuses it), so it answers ``none`` honestly.
        ("ssl://electrum.blockstream.info:50002", True, "electrum"),
        ("bitcoind://node.local:8332", True, "bitcoind"),
        ("bitcoind+tls://node.local:8332", True, "bitcoind"),
        ("https://mempool.space/api", True, "none"),
        ("http://127.0.0.1:3006/api", True, "none"),
        ("https://electrs.box/esplora", True, "none"),
        ("ftp://nope", True, "none"),
    ],
)
def test_backend_kind_closed_mapping(url: str, resolved: bool, expected: str) -> None:
    kind = _backend_kind(Settings(chain_base_url=url), resolved=resolved)
    assert kind == expected
    assert kind in BACKEND_KINDS


def test_backend_kinds_closed_set_after_the_descope() -> None:
    """The M3B enum: exactly {none, electrum, bitcoind} — the mempool /
    esplora / public URL-shape kinds are gone (mempool.space is public
    fee/price info, never a wallet backend; ADR-0018/0023 as amended).
    Nothing that is not Electrum or Core RPC answers ``none``."""
    assert BACKEND_KINDS == frozenset({"none", "electrum", "bitcoind"})
    for url in ("", "ssl://h", "https://x/api", "http://y", "bitcoin://z"):
        assert _backend_kind(Settings(chain_base_url=url), resolved=True) != "bitcoind"


def test_backend_kind_follows_the_live_client_after_a_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """The badge answers what is SERVING (the boot-resolved selection point,
    folded + updated by the swap), not what is merely stored — TCK-DESCOPE-M3A
    edition: ``none`` while a first-run choice is unmade (no silent public
    client to badge); an explicit PUBLIC CONSENT serves the public Electrum
    server, which badges as ``electrum`` (the public/private trust dimension
    rides ``privacy_mode``, not the kind); an ssl:// swap badges likewise;
    a cleared selection returns to ``none``."""
    wiring, commands = _mk_wiring(tmp_path, monkeypatch)
    store = wiring.store
    # Unresolved first-run: no marker, no rung → nothing is being consulted.
    # The scan is HELD for the choice (the real consent scenario — since
    # code-review fix 2 the consent seam installs ONLY into a held gate).
    wiring.scan.set_startup_deferred()
    flow = ChainBackendFlow(wiring, _probe_true)
    assert flow.kind == "none"
    # The consent seam (marker + install of the named public Electrum).
    assert app.set_public_backend_consent(store, flow) is True  # released
    assert wiring.settings.chain_base_url == app.PUBLIC_ELECTRUM_URL
    assert flow.kind == "electrum"
    assert app._backend_mode(wiring.settings) == "public"  # trust dimension
    _drain(wiring, commands)  # settle the consent's resync before the next swap
    # Clearing the selection (rung + marker) → unresolved again: no client.
    wiring.settings.chain_base_url = ""
    store.set_setting(app.BACKEND_CHOICE_SETTING, "")
    assert flow.kind == "none"
    error, fields = flow.apply(SSL_URL)
    assert error is None and fields["swapped"] is True
    assert flow.kind == "electrum"
    _drain(wiring, commands)
    wiring.store.close()


def test_kind_and_flags_ride_the_settings_and_state_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """Additive-field pin (settings/1 + state/1 unchanged): the settings READ
    and the /state snapshot carry ``backend_kind`` (an enum NAME) whenever a
    backend is wired, and carry NOTHING when one is not. TCK-DESCOPE-M3B:
    the URL under test is a live family (loopback ssl:// → ``electrum`` +
    ``own_node_local``) — the http(s) Esplora-shape GOOD_URL would now
    honestly answer ``none`` (pinned in the closed-mapping pass above)."""
    ssl_local = "ssl://127.0.0.1:50001"
    wiring, _commands = _mk_wiring(tmp_path, monkeypatch, stored_url=ssl_local)
    wiring.settings.chain_base_url = ssl_local  # the boot fold the real wiring does
    flow = ChainBackendFlow(wiring, _probe_true)
    reply = app.handle_settings_request(wiring.store, None, None, flow)
    assert reply["backend_kind"] == "electrum"
    snapshot = app.build_state_snapshot(
        wiring.flow,
        wiring.session,
        None,
        wiring.scan,
        None,
        flow.kind,
        privacy_mode=app._backend_mode(wiring.settings),  # the pump's source
    )
    assert snapshot["backend_kind"] == "electrum"
    # TCK-UX-010: the additive privacy_mode rides the SAME live settings
    # object the swap mutates — a closed enum NAME, never the URL/host.
    assert snapshot["privacy_mode"] == "own_node_local"
    assert "127.0.0.1" not in repr(snapshot) and "://" not in repr(snapshot)
    bare = app.handle_settings_request(wiring.store, None, None)
    assert "backend_kind" not in bare  # absent, never guessed
    assert (
        "backend_kind"
        not in app.build_state_snapshot(wiring.flow, wiring.session, None)
    )
    wiring.store.close()


# ---------------------------------- effective chain URL display (TCK-WEB-013)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # The point of the helper: userinfo gone, everything else verbatim.
        ("http://rpcuser:S3cr3t@node.invalid:18443/wallet/x",
         "http://node.invalid:18443/wallet/x"),
        ("bitcoind://u:p@127.0.0.1:8332", "bitcoind://127.0.0.1:8332"),
        ("ssl://pass@host.local:50002", "ssl://host.local:50002"),
        ("https://mempool.space/api", "https://mempool.space/api"),  # no creds
        ("http://host:80", "http://host:80"),  # no path, no change
        ("no-scheme://user:pass@host/p", "no-scheme://host/p"),
        ("host:8080/x", "host:8080/x"),  # schemeless: authority untouched
    ],
)
def test_url_without_credentials_string_surgery(url: str, expected: str) -> None:
    """The strip is plain-string surgery (urllib is lint-banned here): only
    the USERINFO (up to the LAST '@' of the authority) is removed — scheme,
    host, port and path ride verbatim. (A userinfo containing '/' cannot be
    parsed by any string helper; the store's typed writer rejects such URLs
    at write time, and the env rung is the operator's own config.)"""
    assert app._url_without_credentials(url) == expected


def test_settings_reply_carries_the_effective_chain_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """Done-when pins (TCK-WEB-013, additive under the unchanged settings/1
    tag; TCK-DESCOPE-M3A): (a) an UNSET rung answers the EFFECTIVE URL as
    the EMPTY string — unresolved, never a public default; (b) an env rung carrying userinfo is displayed
    CREDENTIAL-FREE (and no reply byte leaks the login); (c) the stored-rung
    entry is untouched by the new field (stored and effective are distinct,
    the env rung shadows); (d) the field follows a live SWAP; (e) a bare
    pump (no chain wiring) OMITS it — absent, never guessed, the exact
    ``backend_kind`` rule it stamps beside."""
    # (a) nothing on any rung: EMPTY effective (unresolved — no public default).
    wiring, _commands = _mk_wiring(tmp_path, monkeypatch)
    flow = ChainBackendFlow(wiring, _probe_true)
    reply = app.handle_settings_request(wiring.store, None, None, flow)
    entry = next(e for e in reply["settings"] if e["key"] == "chain_base_url")
    assert entry["value"] is None  # STORED rung (what GET /settings always was)
    assert reply[app.SETTINGS_EFFECTIVE_CHAIN_URL_KEY] == ""
    # (e) bare pump: no wiring → neither additive field is fabricated.
    bare = app.handle_settings_request(wiring.store, None, None)
    assert app.SETTINGS_EFFECTIVE_CHAIN_URL_KEY not in bare
    assert "backend_kind" not in bare
    wiring.store.close()

    # (b)+(c) env rung (the boot fold) carries a login: display is stripped.
    subdir = tmp_path / "env"
    subdir.mkdir()
    wiring2, _commands2 = _mk_wiring(
        subdir, monkeypatch, boot_backend="http://rpcuser:S3cr3t@node.invalid:18443/api"
    )
    flow2 = ChainBackendFlow(wiring2, _probe_true)
    reply2 = app.handle_settings_request(wiring2.store, None, None, flow2)
    assert (
        reply2[app.SETTINGS_EFFECTIVE_CHAIN_URL_KEY]
        == "http://node.invalid:18443/api"
    )
    for secret in ("rpcuser", "S3cr3t"):
        assert secret not in repr(reply2)  # no credential byte anywhere
    # The stored rung (unset — the boot fold shadows it) stays the null entry:
    # the two fields answer different questions and never overwrite each other.
    entry2 = next(e for e in reply2["settings"] if e["key"] == "chain_base_url")
    assert entry2["value"] is None

    # (d) a live swap moves the display with the client it seals beside.
    wiring2.settings.chain_base_url = GOOD_URL  # simulate the installed swap
    assert (
        app.handle_settings_request(wiring2.store, None, None, flow2)[
            app.SETTINGS_EFFECTIVE_CHAIN_URL_KEY
        ]
        == GOOD_URL
    )
    wiring2.store.close()


def test_effective_url_follows_a_real_hot_swap_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """The seal point stamps at REPLY time, so the APPLIED chain write
    already answers the NEW server as effective (with the swap's resync
    drained to keep the worker honest) — the pane never re-GETs to learn
    its own just-applied truth."""
    wiring, commands = _mk_wiring(tmp_path, monkeypatch)
    flow = ChainBackendFlow(wiring, _probe_true)
    reply = app.handle_settings_request(
        wiring.store, "chain_base_url", NEW_URL, flow
    )
    assert reply["status"] == "applied" and reply["swapped"] is True
    assert reply[app.SETTINGS_EFFECTIVE_CHAIN_URL_KEY] == NEW_URL
    _drain(wiring, commands)
    wiring.store.close()

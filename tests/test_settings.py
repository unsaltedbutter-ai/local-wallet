"""TCK-WEB-005 engine half: the typed settings command (pump-owned reads/
writes over the ALLOWLISTED user-facing store keys).

The contract (docs/ux-utxo-notes-design.md §3 data-model rule + ADR-0024 §3):

* every settings read/write is a :class:`SettingsRequest` serialized through
  the pump — the ENGINE thread is the only thread that touches the store's
  settings table (cross-thread store access is structurally impossible here:
  the probe store used from the main thread would raise ``ProgrammingError``);
* the allowlist is fail-closed: ONLY keys that exist in the store and are
  read by live code today (``gap_limit``, ``chain_base_url``) — no invented
  keys, an off-allowlist request never reaches the store, unknown key names
  are not echoed (untrusted input);
* writes validate fail-closed (type + bounds; chain_base_url delegates to the
  store's typed writer — the ONLY sanctioned writer of that key), errors are
  value-free, and an unrelated key is structurally untouched (one key per
  write);
* every entry carries the honest effect flags: ``requires_restart``
  (chain_base_url is config-only per ADR-0018 — never hot-swapped) and
  ``env_override`` (the env rung shadows the stored one; the VALUE is never
  read or shown).

All hermetic: in-memory/tmp stores, no chain, no model.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Any

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.app import (
    SETTINGS_SCHEMA,
    EngineEvent,
    EventEmitter,
    StartupScan,
)
from localwallet.protocol import IntentName
from localwallet.store import Store
from localwallet.wallet import scan as wallet_scan


@pytest.fixture
def env_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env rungs: the stored rung is the whole story in these tests."""
    monkeypatch.delenv(app.GAP_LIMIT_ENV_VAR, raising=False)
    monkeypatch.delenv(app.CHAIN_BASE_URL_ENV_VAR, raising=False)


def _entries(store: Store) -> dict[str, dict[str, Any]]:
    result = app.handle_settings_request(store, None, None)
    assert result["schema"] == SETTINGS_SCHEMA and result["status"] == "ok"
    return {e["key"]: e for e in result["settings"]}  # type: ignore[index]


# ------------------------------------------------------------- the allowlist


def test_allowlist_is_exactly_the_live_db_keys(env_clean: None, tmp_path: Path) -> None:
    """The fail-closed list: the two writable DB keys today, each with name,
    current value, type, bounds and honest flags — plus the READ-ONLY
    ``watch_key`` entry (TCK-WEB-008 follow-up (a), TCK-LAUNCH-002). A key
    whose DB rung has no reader would be a write into the void — refused
    (absent), never invented; the watch key is deliberately NOT writable
    through this surface (it is absent from the write allowlist below)."""
    store = Store(tmp_path / "allow.db")
    try:
        entries = _entries(store)
        assert set(entries) == {"gap_limit", "chain_base_url", "watch_key"}
        gap = entries["gap_limit"]
        assert gap == {
            "key": "gap_limit",
            "type": "int",
            "value": None,  # unset → the default below applies
            "default": "20",
            "min": wallet_scan._MIN_GAP,
            "max": wallet_scan._MAX_GAP,
            "requires_restart": False,  # re-resolved per scan plan
            "env_override": False,
        }
        chain = entries["chain_base_url"]
        assert chain["type"] == "url"
        assert chain["value"] is None and chain["default"] is None
        # ADR-0018 config-only switch: the client is built at bootstrap — the
        # honest flag is RESTART, never a silent hot-swap.
        assert chain["requires_restart"] is True
        # No wallet provisioned → the watch key reads configured: False with a
        # null value (fail quiet, never a guess).
        watch = entries["watch_key"]
        assert watch["type"] == "watch_key"
        assert watch["configured"] is False
        assert watch["value"] is None
    finally:
        store.close()


def test_env_override_flag_is_honest_without_reading_the_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``env_override`` reflects EXISTENCE only — the env value never reaches
    the snapshot, and the stored rung stays writable/visible."""
    monkeypatch.delenv(app.CHAIN_BASE_URL_ENV_VAR, raising=False)
    monkeypatch.setenv(app.GAP_LIMIT_ENV_VAR, "2")  # dev fast-scan value
    store = Store(tmp_path / "env.db")
    try:
        entries = _entries(store)
        assert entries["gap_limit"]["env_override"] is True
        assert entries["chain_base_url"]["env_override"] is False
        # The rest of the entry is byte-identical to the no-env shape: the
        # env RUNG flips the flag, its VALUE never rides anywhere.
        assert {k: v for k, v in entries["gap_limit"].items() if k != "env_override"} == {
            "key": "gap_limit", "type": "int", "value": None, "default": "20",
            "min": wallet_scan._MIN_GAP, "max": wallet_scan._MAX_GAP,
            "requires_restart": False,
        }
    finally:
        store.close()


# ---------------------------------------------------------------- writes


def test_gap_limit_write_matrix_validates_fail_closed_and_value_free(
    env_clean: None, tmp_path: Path
) -> None:
    store = Store(tmp_path / "gap.db")
    try:
        for good in ("1", "20", "1000", "  7 "):  # whitespace canonicalizes
            result = app.handle_settings_request(store, "gap_limit", good)
            assert result["status"] == "applied", good
            assert result["settings"][0]["value"] == good.strip()
            assert store.get_setting("gap_limit") == good.strip()  # canonical

        for bad in ("0", "-1", "1001", "abc", "1e3", "007", "", "  ", "٢٠"):
            result = app.handle_settings_request(store, "gap_limit", bad)
            assert result["status"] == "rejected", bad
            # the stored rung is the last GOOD value, untouched.
            assert store.get_setting("gap_limit") == "7"
        # Value-free: the submitted value is never echoed in the refusal.
        for bad in ("abc", "1e3", "1001", "٢٠"):
            assert bad not in str(app.handle_settings_request(store, "gap_limit", bad))
    finally:
        store.close()


def test_chain_base_url_writes_route_through_the_typed_writer(
    env_clean: None, tmp_path: Path
) -> None:
    """No second parser: every URL rule (scheme, host, credentials,
    whitespace) is ``Store.set_chain_base_url``'s, and the clear convention
    (exact-empty-string) matches the ADR-0023 rung semantics."""
    store = Store(tmp_path / "chain.db")
    try:
        result = app.handle_settings_request(
            store, "chain_base_url", "http://127.0.0.1:3006/api"
        )
        assert result["status"] == "applied"
        assert store.get_chain_base_url() == "http://127.0.0.1:3006/api"

        secret = "hunter2"
        for bad in ("ftp://x", "https://", f"http://user:{secret}@host", "   "):
            result = app.handle_settings_request(store, "chain_base_url", bad)
            assert result["status"] == "rejected", bad
            # value-free: neither the credentials nor the URL ride the error
            assert secret not in str(result)
            if bad.strip():
                assert bad.strip() not in str(result["error"])
        assert store.get_chain_base_url() == "http://127.0.0.1:3006/api"

        # The documented clear: "" removes the stored rung (back to default).
        result = app.handle_settings_request(store, "chain_base_url", "")
        assert result["status"] == "applied"
        assert result["settings"][0]["value"] is None
        assert store.get_chain_base_url() is None
    finally:
        store.close()


def test_off_allowlist_never_reaches_the_store_and_is_not_echoed(
    env_clean: None, tmp_path: Path
) -> None:
    """Active/internal keys and invented ones are refused identically,
    WITHOUT naming the request back (the name itself is untrusted) — and the
    store is never touched (zero effect on unrelated keys, structurally)."""
    store = Store(tmp_path / "off.db")
    try:
        store.set_chain_base_url("http://127.0.0.1:3006/api")
        store.set_setting("gap_limit", "12")
        for key in (
            "active_wallet_id",
            "fee_cache_ttl_s",  # env/config scalar with NO DB reader → refused
            "utxo_target_min_sats",  # not implemented yet → refused
            "LOCALWALLET_STORE_PATH",
            "",
            "gap_limit ",  # trailing space is NOT the key (fail closed)
        ):
            result = app.handle_settings_request(store, key, "7")
            assert result == {
                "schema": SETTINGS_SCHEMA,
                "status": "rejected",
                "error": "unknown setting",
            }, key
        assert store.get_setting("gap_limit") == "12"
        assert store.get_chain_base_url() == "http://127.0.0.1:3006/api"
        assert store.get_setting("active_wallet_id") is None
    finally:
        store.close()


def test_oversized_value_is_refused_without_a_store_write(
    env_clean: None, tmp_path: Path
) -> None:
    store = Store(tmp_path / "size.db")
    try:
        result = app.handle_settings_request(
            store, "chain_base_url", "a" * (app.MAX_SETTING_VALUE_CHARS + 1)
        )
        assert result["status"] == "rejected"
        assert result["error"] == "value too long"
        assert store.get_chain_base_url() is None
    finally:
        store.close()


# ------------------------------------------------- pump routing + threading


def test_settings_reads_and_writes_are_answered_on_the_engine_thread(
    env_clean: None, tmp_path: Path
) -> None:
    """The transport thread NEVER touches the store: the Store is CONSTRUCTED
    on the engine thread (check_same_thread contract), every settings reply
    comes from the pump, and the persist sites run on THAT thread (identity
    captured at the spy). A consult is not a turn — zero events emitted."""

    written: list[int] = []
    engine_thread: dict[str, int] = {}

    def bootstrap() -> app.EngineContext:
        engine_thread["ident"] = threading.get_ident()
        store = Store(tmp_path / "pump.db")  # engine thread — legal
        for name in ("set_setting", "set_chain_base_url"):
            real = getattr(store, name)

            def spy(*args: Any, _real: Any = real, **kwargs: Any) -> None:
                written.append(threading.get_ident())
                _real(*args, **kwargs)

            setattr(store, name, spy)  # type: ignore[assignment]
        table = {
            IntentName.RESPOND: app._respond_handler,
            IntentName.CLARIFY: app._clarify_handler,
        }
        return app.EngineContext(
            loop=AgentLoop(app.stub_generate, table),
            flow=app.TxFlow(),
            session=app.SendSession(),
            table=table,
            store=store,
        )

    events: list[EngineEvent] = []
    handle = app.start_engine(bootstrap, events.append)
    try:
        listed = handle.request_settings(5.0)
        assert listed is not None and listed["status"] == "ok"
        applied = handle.request_settings(5.0, "gap_limit", "9")
        assert applied is not None and applied["status"] == "applied"
        assert applied["settings"][0]["value"] == "9"  # re-read from tool truth
        applied = handle.request_settings(
            5.0, "chain_base_url", "http://127.0.0.1:3006/api"
        )
        assert applied is not None and applied["status"] == "applied"
    finally:
        handle.shutdown()
        assert handle.thread is not None
        handle.thread.join(10)

    assert written  # the writes really happened…
    assert all(i == engine_thread["ident"] for i in written)  # …on the engine
    assert threading.get_ident() != engine_thread["ident"]
    assert events == []  # consults, never transcript turns
    assert handle.error is None


def test_settings_request_timeouts_return_none_not_a_hang(
    tmp_path: Path,
) -> None:
    """No engine draining the queue (dead bootstrap): the transport gets
    ``None`` at the timeout and answers 503 — never a stall, never a lie."""
    handle = app.EngineHandle(
        commands=queue.Queue(), emitter=EventEmitter(lambda _e: None)
    )
    assert handle.request_settings(0.2) is None
    assert handle.request_settings(0.2, "gap_limit", "5") is None


def test_settings_without_an_engine_store_refuses_closed(tmp_path: Path) -> None:
    """A pump that was never wired with a store (bare CLI/harness pumps):
    refused fail-closed — the transport must not guess settings."""
    result = app.handle_settings_request(None, None, None)
    assert result["status"] == "unavailable"
    result = app.handle_settings_request(None, "gap_limit", "5")
    assert result["status"] == "unavailable"


# ------------------------------------------------------- state/scan linkage


def test_first_scan_complete_flag_tracks_the_durable_cursor(tmp_path: Path) -> None:
    """The /state ``first_scan_complete`` bool is the durable completed-scan
    cursor (same source as the freshness ladder) — independent of this
    session's gate, so a restart with an already-scanned wallet never re-
    blocks the UI on a state that is already true on disk."""
    store = Store(tmp_path / "cursor.db")
    wallet = store.create_wallet("default", "desc")
    worker = app.ChainWorker(None)  # client unused: no fetch ever runs
    try:
        flow = app.ScanFlow(store, wallet, worker, gap_limit=None)
        assert flow.first_scan_recorded is False
        flow.gate = StartupScan(enabled=True)  # pending: THIS session scanning
        snap = app.build_state_snapshot(
            app.TxFlow(), app.SendSession(), None, flow
        )
        assert snap["scan_state"] == "pending"
        assert snap["first_scan_complete"] is False
        store.set_sync_state(wallet.id, wallet_scan.CURSOR_KEY, '{"0": 3, "1": 3}')
        flow.gate.mark_running()
        flow.gate.mark_done()
        snap = app.build_state_snapshot(
            app.TxFlow(), app.SendSession(), None, flow
        )
        assert snap["scan_state"] == "done"
        assert snap["first_scan_complete"] is True
    finally:
        worker.stop()
        store.close()

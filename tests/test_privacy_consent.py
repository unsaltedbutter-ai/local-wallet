"""TCK-PRIVACY-001 (ENGINE half): zero chain traffic before an EXPLICIT
public consent.

USER DIRECTION (2026-09-11): no public mempool.space access unless the user
explicitly selects public. The audit found exactly ONE un-gated pre-consent
chain call site — the ``tx_status`` handler (an ordinary chat turn could
query a txid against the public-default client while the gate still read
``awaiting_backend``). Everything else (startup scan, get_balance's price
fetch, create_tx/self_transfer's fee fetch + lazy scan, the watch poll)
stands down behind the held gate; these pins prove it with a CALL-COUNTING
chain client at every construction site reached pre-consent, and pin that
no ordinary action (asking a balance, closing the pane/session) ever WRITES
the consent record. The web consent BUTTON is TCK-PRIVACY-001B; the
engine-side seam it rides (:func:`localwallet.app.set_public_backend_consent`)
is pinned here.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

from localwallet import app
from localwallet.chain import (
    EsploraClient,
    FeeEstimator,
    PriceOracle,
    time_since_last_block,
)
from localwallet.protocol import (
    CreateTxParams,
    Envelope,
    GetBalanceParams,
    IntentName,
    SelfTransferParams,
    TxStatusParams,
)
from localwallet.store import Store
from localwallet.ui.onboarding import BACKEND_CHOICE_PUBLIC, BACKEND_CHOICE_SETTING
from localwallet.wallet.descriptor import WalletDescriptor
from tests.test_e2e_skeleton import SEND_RECIPIENT, ZPUB
from tests.test_web_server import _request, _Stream

TXID: Final[str] = "ab" * 32  # shape-valid 64-hex (the stub's own placeholder)


def _counting_client(calls: list[str]) -> EsploraClient:
    """The TCK-ONB-006 counting discipline (tests/test_onboarding.py),
    extended to record ``host+path`` so a pin can name WHERE traffic would
    have gone. MockTransport: nothing can leave the process even if a gate
    fails; a non-empty ``calls`` IS the leak."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.url.host}{request.url.path}")
        path = request.url.path
        if path.endswith("/blocks/tip"):
            return httpx.Response(200, json=870_000)
        if path.endswith("/v1/prices"):
            return httpx.Response(200, json={"USD": 100_000.0})
        if path.endswith(f"/tx/{TXID}/status"):
            return httpx.Response(200, json={"confirmed": True, "block_height": 1})
        return httpx.Response(200, json=[])

    return EsploraClient(
        base_url="https://mempool.space/api",  # the public default under audit
        timeout_s=2.0,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture
def held_store(tmp_path: Path) -> tuple[Store, int]:
    """A fresh store with the canonical fixture wallet row (never scanned)."""
    wd = WalletDescriptor.from_key(ZPUB)
    store = Store(tmp_path / "privacy.db")
    wallet = store.create_wallet("default", wd.descriptor)
    store.set_active_wallet(wallet.id)
    yield store, wallet.id
    store.close()


def _held_gate() -> app.StartupScan:
    # The ONB-006 first-run hold, exactly as set_startup_deferred arms it.
    return app.StartupScan(enabled=True, deferred=True)


def _never_scans() -> Any:
    def scan_fn() -> object:
        raise AssertionError("the lazy scan must stand down pre-consent")

    return scan_fn


# ------------------------------------------------- the audit's found leak

STUB_WALLET = WalletDescriptor.from_key(ZPUB).parsed


def test_tx_status_refuses_while_the_backend_is_unchosen(
    held_store: tuple[Store, int],
) -> None:
    """THE leak pin (audit finding 1): a ``tx_status`` turn pre-consent used
    to query the public-default client with no gate at all. Now it refuses
    with the dispatcher-owned honest line BEFORE any request leaves."""
    calls: list[str] = []
    _store, _wallet_id = held_store
    gate = _held_gate()
    handler = app._make_tx_status_handler(
        _counting_client(calls), app.TxFlow(), gate
    )
    result = handler(
        Envelope(
            v=0, intent=IntentName.TX_STATUS, params=TxStatusParams(txid=TXID)
        )
    )
    assert result.get("error") == "backend_unchosen"
    assert result.get("detail") == app.NO_BACKEND_REFUSAL
    assert calls == []  # the refusal is real, not a post-hoc narration


def test_tx_status_queries_once_the_backend_is_resolved(
    held_store: tuple[Store, int],
) -> None:
    """The pairing (never over-tighten): the hold is the ONLY stand-down.
    Once the consented scan has completed — and structurally with NO gate
    (headless/test tables, the documented ADR-0023 carve-out) — the lookup
    runs against the client unchanged."""
    calls: list[str] = []
    _store, _wallet_id = held_store
    gate = _held_gate()
    handler = app._make_tx_status_handler(
        _counting_client(calls), app.TxFlow(), gate
    )
    envelope = Envelope(
        v=0, intent=IntentName.TX_STATUS, params=TxStatusParams(txid=TXID)
    )
    gate.mark_done()  # a consented backend, scan completed
    result = handler(envelope)
    assert "error" not in result
    assert result["confirmed"] is True
    assert any("tx" in call for call in calls)

    ungated = app._make_tx_status_handler(_counting_client([]), app.TxFlow())
    assert "error" not in ungated(envelope)


def test_tx_status_refusal_prints_verbatim() -> None:
    """UI honesty: the refusal prints as the dispatcher-owned line — never
    a raw error code, never a chain-failure wording (the model may also
    narrate it verbatim; both transports share this renderer)."""
    lines: list[str] = []
    app._print_tx_status(
        {"error": "backend_unchosen", "detail": app.NO_BACKEND_REFUSAL},
        lines.append,
    )
    assert lines == [app.NO_BACKEND_REFUSAL]
    assert "backend_unchosen" not in lines[0]


# ------------------------------------- the audit's already-gated suspects

def test_get_balance_price_oracle_fetch_stands_down_pre_consent(
    held_store: tuple[Store, int],
) -> None:
    """Suspect: the get_balance fiat fetch over the public-default oracle.
    Proof it stands down: sats-only answer, stale-flagged, ZERO requests,
    and no lazy scan (the web/engine posture, defer_scans=True)."""
    calls: list[str] = []
    store, wallet_id = held_store
    gate = _held_gate()
    handler = app._make_get_balance_handler(
        store,
        wallet_id,
        _never_scans(),
        gate,
        price_oracle=PriceOracle(_counting_client(calls)),
        kick_scan_fn=lambda: False,
        defer_scans=True,
    )
    result = handler(
        Envelope(v=0, intent=IntentName.GET_BALANCE, params=GetBalanceParams())
    )
    assert result["freshness"] == app.FRESHNESS_STALE
    assert "usd_total_cents" not in result and "fiat_total_minor" not in result
    assert calls == []


def test_create_tx_and_self_transfer_fee_and_price_stand_down(
    held_store: tuple[Store, int],
) -> None:
    """Suspects: create_tx's fee fetch / USD resolution / lazy scan and
    self_transfer's fee bid — all refuse at the step-0 gate (which counts
    ``awaiting_backend`` as incomplete) BEFORE the first chain call."""
    calls: list[str] = []
    store, wallet_id = held_store
    gate = _held_gate()
    client = _counting_client(calls)
    create = app._make_create_tx_handler(
        store,
        wallet_id,
        STUB_WALLET,
        app.TxFlow(),
        FeeEstimator(client),
        PriceOracle(client),
        _never_scans(),
        scan_gate=gate,
        # Production shape (app.py's _wire lambda): the ETA hint's own
        # chain call rides the same unchosen client (SR LOW-2 pin).
        seconds_since_last_block_fn=lambda: time_since_last_block(client),
    )
    result = create(
        Envelope(
            v=0,
            intent=IntentName.CREATE_TX,
            params=CreateTxParams(recipient=SEND_RECIPIENT, amount_sats=1_000_000),
        )
    )
    assert result.get("error") == "wallet_loading"
    self_tx = app._make_self_transfer_handler(
        store, wallet_id, STUB_WALLET, app.TxFlow(), FeeEstimator(client),
        _never_scans(), scan_gate=gate,
    )
    result = self_tx(
        Envelope(
            v=0,
            intent=IntentName.SELF_TRANSFER,
            params=SelfTransferParams(
                mode="consolidate", below_size_sats=10_000
            ),
        )
    )
    assert result.get("error") == "wallet_loading"
    assert calls == []


def test_fee_and_price_wrapper_construction_is_network_free() -> None:
    """Suspect: "wrappers constructed against the public default before
    consent" — construction is inert (they fetch lazily, per TTL); the
    FIRST request is the leak, and every first request above stayed down."""
    calls: list[str] = []
    client = _counting_client(calls)
    FeeEstimator(client)
    PriceOracle(client)
    app.ChainWorker(client).stop()
    assert calls == []
    client.close()


# ------------------------------------------------ the consent RECORD seam


def test_set_public_backend_consent_records_and_releases(
    held_store: tuple[Store, int],
) -> None:
    """Deliverable 3: the engine-side explicit-consent seam the web button
    (001B) rides — writes ONB-006's ``chain_backend_choice`` = ``public``
    and releases the held scan, reporting whether the load started (the F2
    contract). A second call is a no-op release (``False``)."""
    store, _wallet_id = held_store
    wallet = store.get_active_wallet()
    assert wallet is not None
    calls: list[str] = []
    worker = app.ChainWorker(_counting_client(calls))
    try:
        flow = app.ScanFlow(store, wallet, worker, gap_limit=None)
        flow.set_startup_deferred()
        assert flow.gate.state == "awaiting_backend"
        assert app._backend_resolved(None, store) is False

        assert app.set_public_backend_consent(store, flow) is True
        assert store.get_setting(BACKEND_CHOICE_SETTING) == BACKEND_CHOICE_PUBLIC
        assert app._backend_resolved(None, store) is True
        assert flow.gate.state == "pending"  # released (no pump queue here)
        # No queue attached: begin() never fired, so nothing fetched — the
        # release SEMANTICS are pinned here, the fetch by the scan tests.
        assert calls == []
        assert app.set_public_backend_consent(store, flow) is False
    finally:
        worker.stop()


# ------------------------------------------------- the real first-run beat


def _launch_first_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    generate_fn: Any,
) -> tuple[threading.Thread, list[str], dict[str, Any]]:
    """A REAL unprovisioned web session (the form path the user reports):
    every chain client built afterwards rides a request-COUNTING transport
    recording host+path (tests/test_web_server.py's harness, this file's
    copy — pre-consent the count must stay 0 through the whole beat)."""
    store_path = tmp_path / "first-run.db"
    for var in (
        app.ZPUB_ENV_VAR,
        app.UI_ENV_VAR,
        "LOCALWALLET_MODEL_PATH",
        "LOCALWALLET_LLM_BASE_URL",
        "LOCALWALLET_LLM_MODEL",
        "LOCALWALLET_CHAIN_BASE_URL",
        "LOCALWALLET_WEB_PORT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(store_path))
    monkeypatch.setenv(app.AUTO_SCAN_ENV_VAR, "1")
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0.05")  # watch ON
    monkeypatch.setattr(app, "_resolve_default_model", lambda: None)

    calls: list[str] = []
    capture: dict[str, Any] = {"calls": calls, "store_path": store_path}
    real_client = app.EsploraClient

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.url.host}{request.url.path}")
        path = request.url.path
        if path.endswith("/blocks/tip"):
            return httpx.Response(200, json=870_000)
        return httpx.Response(200, json=[])

    def counting_client(**kwargs: Any) -> Any:
        kwargs["transport"] = httpx.MockTransport(handler)
        kwargs.setdefault("timeout_s", 2.0)
        kwargs.setdefault("max_retries", 0)
        return real_client(**kwargs)

    monkeypatch.setattr(app, "EsploraClient", counting_client)
    outputs: list[str] = []
    gate = threading.Event()

    def on_server(server: Any) -> None:
        capture["server"] = server
        gate.set()

    thread = threading.Thread(
        target=lambda: capture.update(
            code=app.run(
                ["--web"],
                output_fn=outputs.append,
                on_web_server=on_server,
                generate_fn=generate_fn,
            )
        ),
        daemon=True,
    )
    thread.start()
    assert gate.wait(30), "unprovisioned web server never started"
    return thread, outputs, capture


def _fake_generate() -> Any:
    """Canned envelopes, ONE per model turn (the loop dispatches and
    returns): first the tx_status probe, then the create_tx attempt; every
    later call answers ``respond``."""
    scripted = [
        {"v": 0, "intent": "tx_status", "params": {"txid": TXID}},
        {
            "v": 0,
            "intent": "create_tx",
            "params": {"recipient": SEND_RECIPIENT, "amount_sats": 1_000_000},
        },
    ]
    index = {"i": 0}

    def gen(prompt: str, grammar: str) -> str:
        i = index["i"]
        index["i"] += 1
        return json.dumps(
            scripted[i] if i < len(scripted)
            else {"v": 0, "intent": "respond", "params": {"text": "Understood."}}
        )

    return gen


def test_first_run_beat_never_records_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deliverable 4, end to end: enter the zpub through the REAL form
    path, wake the pump past the watch interval, ask a balance (quick
    action), ask to check a transaction, ask to send, then CLOSE the pane
    (end the session) — ZERO requests leave the process at every step and
    no ordinary action ever writes the consent record."""
    thread, _outputs, capture = _launch_first_run(
        tmp_path, monkeypatch, _fake_generate()
    )
    server = capture["server"]
    stream = _Stream(server)
    try:
        stream.read_head()
        # The zpub lands — the ONB-006 gate must hold the scan (verified).
        status, _h, _d, _r = _request(
            server, "POST", "/watchkey", {"key": ZPUB}, token=server.token
        )
        assert status == 200
        reply = _request(server, "GET", "/state", token=server.token)
        assert json.loads(reply[2])["scan_state"] == "awaiting_backend"
        assert capture["calls"] == []  # entering the zpub leaks NOTHING

        # Balance quick action (model-free dispatch) — cache-served answer.
        status, _h, _d, _r = _request(
            server, "POST", "/turn", {"text": "/balance"}, token=server.token
        )
        assert status == 202
        stream.read_until(b"turn_end", timeout=15)
        assert capture["calls"] == []  # the price fetch stood down

        # The formerly-leaky path: an ordinary "check this tx" turn.
        status, _h, _d, _r = _request(
            server,
            "POST",
            "/turn",
            {"text": f"check transaction {TXID}"},
            token=server.token,
        )
        assert status == 202
        frame = stream.read_until(
            b"No server has been chosen", timeout=15
        )  # the honest refusal line
        assert app.NO_BACKEND_REFUSAL.encode() in frame  # verbatim copy
        assert capture["calls"] == []  # THE leak pin

        # A send attempt: refused pre-first-scan, before any fee fetch.
        status, _h, _d, _r = _request(
            server,
            "POST",
            "/turn",
            {"text": f"send 1000000 sats to {SEND_RECIPIENT}"},
            token=server.token,
        )
        assert status == 202
        stream.read_until(b"still loading", timeout=15)
        assert capture["calls"] == []

        # Watch poll: wake the pump repeatedly past the 0.05s interval.
        for _ in range(3):
            time.sleep(0.15)
            _request(server, "GET", "/state", token=server.token)
        assert capture["calls"] == []  # the drain stood down behind the hold
    finally:
        # "Close the pane" == end the session: nothing on this path records
        # a choice either.
        stream.close()
        server.stop()
        thread.join(15)
    assert capture.get("code") == 0
    store = Store(str(capture["store_path"]))
    try:
        assert store.get_setting(BACKEND_CHOICE_SETTING) is None
    finally:
        store.close()


# ----------------------------------------------------------- structural pin


def test_no_chain_module_reaches_the_default_pre_consent_by_construction() -> None:
    """The lint invariant stays true (chain/ is the only networker) and the
    audit's scope is structural: the node_status handler takes NO client at
    all (loopback detection only, ADR-0016) and the tip probes only ever
    judge a USER-SUPPLIED candidate URL — the public default is reachable
    pre-consent ONLY through the gate-checked surfaces pinned above."""
    import inspect

    assert "client" not in inspect.signature(
        app._make_node_status_handler
    ).parameters
    probe_params = inspect.signature(app._probe_chain_backend).parameters
    assert "url" in probe_params  # candidate-URL-only by signature

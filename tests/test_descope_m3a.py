"""TCK-DESCOPE-M3A — the wallet-backend rewire (selection / consent /
headless refusal / fee-price decoupling / routing check).

The contract (docs/descope-esplora-plan.md §3/§4, decisions 1-5 adopted;
USER REDIRECTION 2026-09-11: wallet information comes ONLY from Electrum or
bitcoind; mempool.space is a PUBLIC-INFO source — fees/prices — never the
wallet's address/UTXO/history source):

1. SELECTION LADDER — an empty ``chain_base_url`` is UNRESOLVED, never a
   silent mempool.space fallback; a wallet client builds for ``ssl://``
   (Electrum) and ``bitcoind[+tls]://`` (Core) ONLY.
2. CONSENT — the explicit public choice is the NAMED public Electrum server
   (:data:`PUBLIC_ELECTRUM_URL`), recorded through the existing
   ``set_public_backend_consent`` seam, folded onto the ladder at EVERY
   launch that sees the record, and it installs the live client (there is
   no public-default client to release onto anymore).
3. HEADLESS — a non-interactive launch with an UNRESOLVED backend REFUSES
   the startup scan with the value-free :data:`HEADLESS_BACKEND_REFUSAL`
   line and makes ZERO chain calls (amends the ADR-0023 headless carve-out;
   a recorded public consent resolves the headless launch onto the public
   Electrum server — it is a choice, not a default).
4. FEES/PRICES DECOUPLED — the fee estimator and the price oracle ride the
   standalone :class:`PublicInfoClient` regardless of the wallet backend,
   constructed ONCE and surviving a hot-swap.
5. ROUTING CHECK — the app module's WALLET path cannot reach the Esplora
   client: the source-level pin (the same style as the lint-network tests)
   plus the construction-site refusal.

All hermetic: mock transports, tmp stores, injected seams.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

import localwallet.app as app_module
from localwallet.app import (
    HEADLESS_BACKEND_REFUSAL,
    NO_BACKEND_REFUSAL,
    PUBLIC_ELECTRUM_URL,
    Settings,
    run,
)
from localwallet.chain import EsploraClient, FeeEstimator, PriceOracle, PublicInfoClient
from localwallet.config import resolve_chain_base_url
from localwallet.store import Store
from localwallet.ui.onboarding import BACKEND_CHOICE_PUBLIC, BACKEND_CHOICE_SETTING
from localwallet.wallet import WalletDescriptor
from tests.test_e2e_skeleton import ZPUB


@pytest.fixture
def env_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "LOCALWALLET_CHAIN_BASE_URL",
        "LOCALWALLET_ESPLORA_BASE_URL",
        "LOCALWALLET_MODEL_PATH",
        "LOCALWALLET_LLM_BASE_URL",
        "LOCALWALLET_LLM_MODEL",
        "LOCALWALLET_ZPUB",
        app_module.AUTO_SCAN_ENV_VAR,
        "LOCALWALLET_WATCH_INTERVAL_S",
        "LOCALWALLET_NODE_DETECTION_ENABLED",
        "LOCALWALLET_DISPLAY_CURRENCY",
    ):
        monkeypatch.delenv(var, raising=False)


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/blocks/tip"):
        return httpx.Response(200, json=870_000)
    return httpx.Response(200, json=[])


class _Out(list):
    """Output-router shim: collects every narration/warning channel line."""

    def __call__(self, line: str) -> None:
        self.append(line)

    def warning(self, line: str) -> None:
        self.append(line)

    error = warning
    console = warning

    def log_error(self, line: str) -> None:  # never echoed in these tests
        pass


def _mock_client(*_args: Any, **_kwargs: Any) -> EsploraClient:
    """An Esplora-shaped mock (all three wallet adapters satisfy the same
    protocol at the seams these tests exercise)."""
    return EsploraClient(
        base_url=Settings().esplora_base_url,
        timeout_s=2.0,
        max_retries=0,
        transport=httpx.MockTransport(_handler),
    )


# ------------------------------------------------- 1. selection ladder


def test_empty_rung_resolves_to_unresolved_not_public() -> None:
    """The ladder's bottom rung is NOTHING (config-level pin): env/file/
    stored unset → resolve answers None; no code path turns that into a
    mempool.space wallet URL."""
    assert resolve_chain_base_url(None, None) is None
    assert Settings().chain_base_url == ""
    # Public info base and the wallet ladder no longer intersect: the
    # public-info constant names mempool.space, the public WALLET choice
    # names an Electrum server.
    assert Settings().esplora_base_url == "https://mempool.space/api"
    assert PUBLIC_ELECTRUM_URL.startswith("ssl://electrum.blockstream.info:50002")


def test_wire_unresolved_builds_no_wallet_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """``_wire`` with no rung and no consent record constructs NO wallet
    client (the silent public stand-in is gone) — while fees/prices still
    ride the standalone public fetcher (constructed once, harmless)."""
    builds: list[str] = []
    monkeypatch.setattr(
        app_module,
        "_build_chain_client",
        lambda settings, auth=None: builds.append(settings.chain_base_url),
    )
    wiring = _wire_now(tmp_path, monkeypatch, interactive=False)
    try:
        assert builds == []  # unresolved: nothing was ever built
        assert wiring.client is None
        assert wiring.public_info is not None  # public info is backend-free
    finally:
        wiring.worker.stop()
        wiring.store.close()


def _wire_now(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    interactive: bool,
    public_info: Any = None,
) -> app_module._Wiring:
    """Run the REAL ``_wire`` (unresolvable backend surfaces included) with
    the client seams mocked and no startup scan side effects."""
    wd = WalletDescriptor.from_key(ZPUB)
    settings = Settings(store_path=str(tmp_path / "m3a.db"))
    info = public_info if public_info is not None else _mock_client()
    monkeypatch.setattr(app_module, "_public_info_client", lambda *_a: info)
    return app_module._wire(
        parsed=wd.parsed,
        descriptor=wd,
        signer_selection=app_module.SignerSelection(
            kind="file", dir_path=tmp_path / "psbt", fingerprint_hex="00000000"
        ),
        settings=settings,
        env_gap=None,
        rescan=False,
        flow=None,
        generate=app_module.stub_generate,
        node_detect_fn=lambda: None,
        output_fn=_Out(),
        cli_interactive=interactive,
    )


# ------------------------------------------------------- 2. consent flow


def test_public_consent_folds_onto_the_ladder_next_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """A recorded public consent (marker, no URL on any rung) resolves the
    NEXT launch onto the NAMED public Electrum server — through the same
    single selection point, so the client, the banner and the badges all
    ride one value."""
    store = Store(str(tmp_path / "m3a.db"))
    try:
        wd = WalletDescriptor.from_key(ZPUB)
        wallet = store.create_wallet("default", wd.descriptor)
        store.set_active_wallet(wallet.id)
        store.set_setting(BACKEND_CHOICE_SETTING, BACKEND_CHOICE_PUBLIC)
    finally:
        store.close()

    built: list[str] = []

    def fake_build(settings: Settings, auth: Any = None) -> Any:
        built.append(settings.chain_base_url)
        return _mock_client()

    monkeypatch.setattr(app_module, "_build_chain_client", fake_build)
    monkeypatch.setattr(app_module, "_public_info_client", lambda *_a: _mock_client())
    with patch.dict(os.environ, {app_module.AUTO_SCAN_ENV_VAR: "0"}):
        wiring = _wire_now(tmp_path, monkeypatch, interactive=False)
    try:
        assert built == [PUBLIC_ELECTRUM_URL]  # the consent IS the URL now
        assert app_module._backend_mode(wiring.settings) == "public"
    finally:
        wiring.worker.stop()
        wiring.client.close()
        wiring.store.close()


def test_consent_ack_and_chat_beat_name_electrum_not_mempool() -> None:
    """Copy pins (ONB-006/001B discipline, ADR-0023 amendment 3): the chat
    backend ask must not offer mempool.space as a wallet choice, and the
    public option is the NAMED public Electrum server with its leak
    warning."""
    own = app_module.CHAT_ONB_BACKEND_OWN
    public = app_module.CHAT_ONB_BACKEND_PUBLIC
    assert "mempool" not in own.lower()  # own tier = node/electrum only
    assert "electrum" in own.lower()
    assert "blockstream" in public.lower()  # the named public server
    assert "mempool.space" not in public  # never offered as a wallet
    assert "ip" in public.lower()  # the leak is stated with the choice
    # The public banner/narration names the public Electrum tier:
    assert "mempool" not in app_module.PRIVACY_INDICATOR.lower()
    assert "Electrum" in app_module.PRIVACY_INDICATOR


# ------------------------------------------------- 3. headless refusal


def _run_headless(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra_env: dict[str, str] | None = None,
) -> list[str]:
    """A scripted headless (non-interactive) launch over the real ``run``;
    returns the captured output lines."""
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(tmp_path / "headless.db"))
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")
    monkeypatch.setenv("LOCALWALLET_NODE_DETECTION_ENABLED", "0")
    for k, v in (extra_env or {}).items():
        monkeypatch.setenv(k, v)
    builds: list[str] = []

    def fake_build(settings: Settings, auth: Any = None) -> Any:
        builds.append(settings.chain_base_url)
        return _mock_client()

    monkeypatch.setattr(app_module, "_build_chain_client", fake_build)
    monkeypatch.setattr(app_module, "_public_info_client", lambda *_a: _mock_client())
    outputs: list[str] = []
    code = run(
        ["--stub-llm", "--zpub", ZPUB],
        input_fn=lambda _p: "exit",
        output_fn=outputs.append,
        interactive=False,
    )
    assert code == 0
    return outputs


def test_headless_unresolved_refuses_the_scan_value_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """AUTO_SCAN default-on + no rung + no consent (the old carve-out
    scanned the public default): the launch REFUSES the scan with the
    value-free line, builds NO wallet client, and the wallet surfaces stay
    held (gate awaiting → handlers refuse with NO_BACKEND_REFUSAL)."""
    joined = "\n".join(_run_headless(tmp_path, monkeypatch))
    assert HEADLESS_BACKEND_REFUSAL in joined
    assert "Startup scan complete" not in joined
    assert "mempool.space" not in joined  # value-free: no host named
    assert "zpub" not in joined and ZPUB not in joined


def test_headless_with_consent_or_rung_scans_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """The refusal is ONLY for the unresolved case: an explicit operator
    rung (env) launches the resolved path — no refusal line, scan planned
    on the client built for THAT url."""
    joined = "\n".join(
        _run_headless(
            tmp_path,
            monkeypatch,
            extra_env={"LOCALWALLET_CHAIN_BASE_URL": "ssl://node.test:50002"},
        )
    )
    assert HEADLESS_BACKEND_REFUSAL not in joined
    assert "Startup scan complete" in joined


# -------------------------------------------- 4. fees/prices decoupling


def test_fee_and_price_riders_are_the_public_fetcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """The wiring's FeeEstimator and PriceOracle read the ONE shared
    PublicInfoClient (not the wallet client); a hot-swap rebind keeps the
    SAME instances (one source, one cache, one TTL — the fee path does not
    move with the backend)."""
    monkeypatch.setattr(app_module, "_build_chain_client", lambda *_a: _mock_client())
    info = PublicInfoClient(Settings(), transport=httpx.MockTransport(_handler))
    with patch.dict(os.environ, {app_module.AUTO_SCAN_ENV_VAR: "0"}):
        wiring = _wire_now(
            tmp_path, monkeypatch, interactive=False, public_info=info
        )
    try:
        assert wiring.fee_estimator._client is info
        assert wiring.price_oracle._client is info
        flow = app_module.ChainBackendFlow(wiring, lambda url: url)
        old_fee, old_price = wiring.fee_estimator, wiring.price_oracle
        wiring.table = {}  # rebind into a fresh table object
        flow._rebind_handlers(_mock_client())
        assert wiring.fee_estimator is old_fee
        assert wiring.price_oracle is old_price
        # PublicInfoClient is a read-only public surface: no wallet methods.
        assert isinstance(wiring.fee_estimator, FeeEstimator)
        assert isinstance(wiring.price_oracle, PriceOracle)
    finally:
        wiring.worker.stop()
        wiring.store.close()
        info.close()


def test_public_info_fetcher_shape() -> None:
    """The trimmed read path: exactly what fees+prices consume (get_json,
    get_tip_height, supports_price) and NOTHING else — no address/scan/
    broadcast/tx_status acceptance, no wallet methods (plan §0 table)."""
    info = PublicInfoClient(Settings(), transport=httpx.MockTransport(_handler))
    try:
        assert info.supports_price is True
        assert callable(info.get_json)
        assert info.get_tip_height() == 870_000  # served by the mock tip
        for wallet_only in (
            "get_address_txs",
            "get_address_utxos",
            "broadcast_tx",
            "get_tx_status",
            "get_tip_block",
            "estimate_fee",
        ):
            assert not hasattr(info, wallet_only), wallet_only
    finally:
        info.close()


# ---------------------------------------------------- 5. routing check


def test_wallet_path_never_reaches_the_esplora_client() -> None:
    """Source-level grep pin (the ticket's routing check; same style as the
    lint-network suite): ``app.py`` — the selection/consent/scan/broadcast/
    watch wiring — must not reference the Esplora client AT ALL anymore;
    the only live Esplora-shape consumer is ``chain/publicinfo.py`` (the
    public fee/price fetcher). Dead wallet-path code inside chain/ itself
    is M4's deletion target, not a routing surface."""
    source = Path(app_module.__file__).read_text(encoding="utf-8")
    assert "EsploraClient" not in source


def test_handlers_refuse_while_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """The unresolved wiring's gate is load-bearing: ``tx_status`` refuses
    with the value-free NO_BACKEND_REFUSAL BEFORE any client touch (the
    one audited pre-consent call site), so a None wallet client is never
    dereferenced."""
    from localwallet.protocol import IntentName, validate_payload

    monkeypatch.setattr(app_module, "_build_chain_client", lambda *_a: _mock_client())
    with patch.dict(os.environ, {app_module.AUTO_SCAN_ENV_VAR: "0"}):
        wiring = _wire_now(tmp_path, monkeypatch, interactive=False)
    try:
        assert wiring.client is None  # unresolved headless: no client
        assert wiring.scan is None or wiring.scan.gate.state == "awaiting_backend"
        handler = wiring.table[IntentName.TX_STATUS]
        envelope = validate_payload(
            {"v": 0, "intent": "tx_status", "params": {"txid": "ab" * 32}}
        )
        result = handler(envelope)
        assert result["error"] == "backend_unchosen"
        assert result["detail"] == NO_BACKEND_REFUSAL
    finally:
        wiring.worker.stop()
        wiring.store.close()


# ------------------------------------- review fix 1: pump client rebind


@pytest.mark.parametrize("via", ["request", "chat"])
def test_pump_rebinds_the_watch_client_after_a_consent_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None, via: str
) -> None:
    """Code-review fix 1 pin (app.py consent branches): the pump's ``client``
    local starts ``None`` on an unresolved wiring and feeds the between-turns
    watch drain; when a consent INSTALLS the public Electrum client the pump
    must rebind the local (like the sibling URL/settings/deferred branches),
    or the drain narrates off the stale None all session. Observed through a
    ``_drain_watch`` spy — the pump's only consumer of the local. The whole
    engine (wiring + pump) runs on ONE thread, as in production (the Store's
    thread affinity is part of the contract)."""
    built: list[Any] = []

    def fake_build(settings: Settings, auth: Any = None) -> Any:
        client = _mock_client()
        built.append(client)
        return client

    monkeypatch.setattr(app_module, "_build_chain_client", fake_build)

    drains: list[Any] = []
    monkeypatch.setattr(
        app_module,
        "_drain_watch",
        lambda *_a, client=None, **_k: drains.append(client) or 0,
    )

    result: dict[str, Any] = {}
    outputs: list[str] = []
    commands: queue.Queue[Any] = queue.Queue()
    wired = threading.Event()

    def engine() -> None:
        # _wire + _pump on the same thread (the real engine-thread pairing).
        wiring = _wire_now(tmp_path, monkeypatch, interactive=False)
        result["wiring"] = wiring
        reply = queue.Queue()
        result["reply"] = reply
        wired.set()
        app_module._pump(
            wiring.loop,
            outputs.append,
            commands,
            flow=wiring.flow,
            session=wiring.session,
            table=wiring.table,
            watcher=wiring.watcher,
            client=wiring.client,
            scan=wiring.scan,
            store=wiring.store,
            backend=wiring.swap,
            settings=wiring.settings,
        )
        result["marker"] = wiring.store.get_setting(BACKEND_CHOICE_SETTING)
        wiring.worker.stop()
        for client in built:
            client.close()
        wiring.store.close()

    thread = threading.Thread(target=engine, daemon=True)
    thread.start()
    assert wired.wait(15)
    wiring = result["wiring"]
    assert wiring.client is None
    assert wiring.scan is not None
    assert wiring.scan.gate.state == "awaiting_backend"
    if via == "request":  # the web /consent button branch
        commands.put(
            app_module.ConsentRequest(command="/consent", reply=result["reply"])
        )
    else:  # the chat "use public" branch
        commands.put("use public")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not (drains and drains[-1] is not None):
        time.sleep(0.02)
    commands.put(app_module.QUIT)
    thread.join(15)
    # A drain happened and it saw the INSTALLED client — never the stale
    # None (pre-fix behavior: every post-consent drain client=None).
    assert drains, "the pump never drained between turns"
    assert built and drains[-1] is built[0] is wiring.client
    assert wiring.settings.chain_base_url == PUBLIC_ELECTRUM_URL
    assert result["marker"] == BACKEND_CHOICE_PUBLIC  # the record half too
    if via == "request":
        assert result["reply"].get(timeout=5)["status"] == "loading"
    else:
        from localwallet.ui.onboarding import PUBLIC_CHOSEN_ACK, PUBLIC_LOADING_NOW

        assert PUBLIC_CHOSEN_ACK in outputs
        assert PUBLIC_LOADING_NOW in outputs  # the F2 report rode the True


# ------------------------- review fix 2: consent no-ops on a RESOLVED backend


def test_stale_consent_on_a_resolved_backend_never_swaps_or_rescans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """Code-review fix 2 pin: a consent POST while the wallet already RUNS
    on the user's own (stored-rung) server RECORDS the choice and moves
    NOTHING — no client swap to the public Electrum server, no resync, the
    selection stands (a stray/duplicate press must never re-home queries to
    a third party). The sanctioned resolved→public switch (the /setup revert
    path, ``install_saved("")``, marker-first) still swaps past the guard."""
    own = "ssl://own.test:50002"
    store = Store(str(tmp_path / "m3a.db"))
    try:
        wd = WalletDescriptor.from_key(ZPUB)
        wallet = store.create_wallet("default", wd.descriptor)
        store.set_active_wallet(wallet.id)
        store.set_chain_base_url(own)
    finally:
        store.close()

    built: list[tuple[str, Any]] = []

    def fake_build(settings: Settings, auth: Any = None) -> Any:
        client = _mock_client()
        built.append((settings.chain_base_url, client))
        return client

    monkeypatch.setattr(app_module, "_build_chain_client", fake_build)
    monkeypatch.setenv(app_module.AUTO_SCAN_ENV_VAR, "0")
    wiring = _wire_now(tmp_path, monkeypatch, interactive=False)
    try:
        assert wiring.settings.chain_base_url == own  # RESOLVED via stored rung
        assert wiring.scan is not None
        gate_state = wiring.scan.gate.state  # disabled (no held first-run scan)
        assert gate_state != "awaiting_backend"

        started = app_module.set_public_backend_consent(wiring.store, wiring.swap)
        assert started is False  # nothing held → the install half is a NO-OP
        assert [url for url, _ in built] == [own]  # NO public client was built
        assert wiring.client is built[0][1]  # the user's server still serving
        assert wiring.settings.chain_base_url == own  # selection unmoved
        assert wiring.scan.gate.state == gate_state  # no release, no resync
        # The RECORD half still stands (an explicit choice, harmless while a
        # rung wins the ladder — the next launch resolves the same server).
        assert (
            wiring.store.get_setting(BACKEND_CHOICE_SETTING) == BACKEND_CHOICE_PUBLIC
        )

        # The sanctioned route PASSES the guard: /setup's revert (the flow
        # writes the marker first, then the empty-URL install) switches to
        # public electrum + resyncs, as always.
        assert wiring.swap.install_saved("") == "swapped"
        assert [url for url, _ in built] == [own, PUBLIC_ELECTRUM_URL]
        assert wiring.settings.chain_base_url == PUBLIC_ELECTRUM_URL
    finally:
        wiring.worker.stop()
        for _, client in built:
            client.close()
        wiring.store.close()


# --------------------------- review fix 3: case-insensitive public host


def test_public_tier_matches_the_electrum_host_case_insensitively() -> None:
    """Code-review fix 3 pin: DNS hosts are case-insensitive — a hand-typed
    ``ssl://Electrum.Blockstream.info:50002`` is the SAME consented public
    server and banners PUBLIC, not "own node remote"."""
    from localwallet.app import (
        BACKEND_MODE_PUBLIC,
        PRIVACY_INDICATOR,
        _backend_mode,
        privacy_indicator,
    )

    for url in (
        PUBLIC_ELECTRUM_URL,
        "ssl://Electrum.Blockstream.Info:50002",
        "ssl://ELECTRUM.BLOCKSTREAM.INFO:50002",
    ):
        settings = Settings(chain_base_url=url)
        assert _backend_mode(settings) == BACKEND_MODE_PUBLIC
        assert privacy_indicator(settings) == PRIVACY_INDICATOR

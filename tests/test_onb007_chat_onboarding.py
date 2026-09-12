"""TCK-ONB-007 (ENGINE half): chat-first onboarding — deterministic beats
and PRE-MODEL chat intercepts, zero model contact.

Pins (the ticket's done-when criteria; critique decisions Q1-Q6):

* startup beats on a FRESH needs_watch_key launch ONLY, the greeting trio
  as ONE grouped bubble (three lines joined with "\n", ONE closed turn —
  static-half user correction 2026-09-11), ORDER-PINNED against the
  model-absent card (beats first, pinned at the pump) and the model PRELOAD
  notices (they follow the greeting group, each its own bubble — pinned at
  the pump) and the model-absent BANNER (banner flushes ahead of the pump —
  pinned at the live SSE stream);
* zpub-in-chat: a key-shaped chat line while unprovisioned rides the
  EXISTING gated parse+provision path (mainnet/private/seed refusals are
  that path's own value-free lines); success emits "Great. I saved that."
  (its own bubble) + the backend beat as ONE grouped bubble, the held scan
  STILL held (zero chain traffic — the ONB-006 promise); once wired, a
  pasted key is NOT intercepted (re-provision stays /watchkey replace only
  — negative pin);
* the pinned "ask me how" matcher (accept/reject vectors);
* chat-URL intercept STATE-GATED on the backend being UNRESOLVED: it rides
  the settings-POST probe/store/swap discipline (url_class clamp + DIAG-001
  companion pinned on the shared probe), and the NO-SWALLOW pins (a
  resolved launch and every post-success turn never intercept — the line
  reaches the model as ordinary chat);
* public-via-chat rides the 001B consent seam: the leak disclosure IS the
  ack (never an un-flagged accept), the consent record lands, the held scan
  releases; question/negation vectors record NOTHING.

All hermetic: fake duck-typed chain clients, tmp stores, injected probes.
The user's copy strings are asserted VERBATIM (they are spec).
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.app import (
    EVENT_TEXT,
    EVENT_TURN_END,
    EVENT_USER_TEXT,
    ChainBackendFlow,
    EngineEvent,
    EventEmitter,
    Settings,
)
from localwallet.protocol import IntentName
from localwallet.store import Store
from localwallet.tx.flow import TxFlow
from localwallet.ui.onboarding import (
    BACKEND_CHOICE_PUBLIC,
    BACKEND_CHOICE_SETTING,
    CONFIRMED,
    PUBLIC_CHOSEN_ACK,
    PUBLIC_LOADING_NOW,
    SWITCHING_NOW,
)
from localwallet.wallet.descriptor import WalletDescriptor
from tests.test_backend_hotswap import _FakeChain, _mk_wiring
from tests.test_e2e_skeleton import XPRV, ZPUB
from tests.test_onboarding import SEED_LINE
from tests.test_wallet_descriptor import YPUB
from tests.test_web_server import _launch_first_run, _request, _session_state, _Stream

VPUB = (
    "vpub5ZJ3cDEGGk61yWWUHFHgmG3M4je4yFD3ebC6jWHsqV8Cxh2K5zz8c6X5Hk7FkUAB"
    "FTjRkQBz3g84MYeRhjAdnq1QmrmyTRTrzs8rFVCJUyh"
)
GOOD_URL = "http://127.0.0.1:3006/api"
NEW_URL = "https://mempool.mine.example:4000/api"

GREET = "Hi, I'd like to be your new Bitcoin wallet."
KEY_ASK = "Enter your xpub or zpub to get started."
HELP_OFFER = (
    "If you don't know where to get that, ask me how and I'll get you some help."
)
SAVED = "Great. I saved that."
BEAT_ASK = "Now, where should I go to get blockchain information?"
# TCK-DESCOPE-M3A beat copy: wallet backends are an Electrum server or a
# Bitcoin Core node (mempool.space is public fee/price info only, never a
# wallet choice); the public tier is the NAMED public Electrum server with
# its leak stated.
BEAT_OWN = (
    "If you run your own Bitcoin node or an Electrum server — Start9, "
    "Umbrel and MyNode all do — that would be better for privacy."
)
BEAT_PUBLIC = (
    "But if you don't have one of those, you can use the public Electrum "
    "server electrum.blockstream.info — chosen with eyes open: whoever "
    "runs it sees every address you check and can link it to your IP."
)

# THE GROUPED BUBBLES (static-half user correction 2026-09-11): each trio
# rides the stream as ONE text event, its sentences joined with "\n" —
# one bubble with line breaks, one turn_end.
GREETING_BUBBLE = f"{GREET}\n{KEY_ASK}\n{HELP_OFFER}"
BACKEND_BUBBLE = f"{BEAT_ASK}\n{BEAT_OWN}\n{BEAT_PUBLIC}"


@pytest.fixture
def env_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        app.CHAIN_BASE_URL_ENV_VAR,
        app.GAP_LIMIT_ENV_VAR,
        app.SIGNER_ENV_VAR,
        app.ZPUB_ENV_VAR,
        app.AUTO_SCAN_ENV_VAR,
        "LOCALWALLET_WATCH_INTERVAL_S",
        "LOCALWALLET_MODEL_PATH",
        "LOCALWALLET_LLM_BASE_URL",
        "LOCALWALLET_LLM_MODEL",
        "LOCALWALLET_UI",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def echo_turns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the turn path with an echo recorder: a line that REACHES the
    model proves it was NOT swallowed by an intercept (the no-swallow and
    not-intercepted-once-wired pins read this)."""
    seen: list[str] = []

    def fake_turn(*args: Any, **kwargs: Any) -> None:
        line: str = args[3]
        output_fn: Callable[[str], None] = args[4]
        seen.append(line)
        output_fn(f"echo:{line}")

    monkeypatch.setattr(app, "_run_turn", fake_turn)
    return seen


def _make_loop() -> AgentLoop:
    return AgentLoop(
        app.stub_generate,
        {
            IntentName.RESPOND: app._respond_handler,
            IntentName.CLARIFY: app._clarify_handler,
        },
    )


def _texts(events: list[EngineEvent]) -> list[str]:
    return [e.payload for e in events if e.kind == EVENT_TEXT]


def _closed_turns(events: list[EngineEvent]) -> list[str]:
    """The text payload of every text-then-turn_end pair (the separate-
    bubble shape the UX-012 pump pattern guarantees)."""
    out: list[str] = []
    for i, e in enumerate(events):
        if e.kind == EVENT_TEXT and i + 1 < len(events):
            nxt = events[i + 1]
            if nxt.kind == EVENT_TURN_END and nxt.id == e.id + 1:
                out.append(e.payload)
    return out


# ------------------------------------------------------------------ 1. beats


def test_fresh_launch_beats_are_one_grouped_bubble_and_precede_the_card(
    tmp_path: Path, env_clean: None
) -> None:
    """The greeting group fires on a FRESH needs_watch_key launch as ONE
    text event with the lines joined by "\n" closing ONE turn (the web
    renders a single bubble with line breaks), and the ORDER vs the
    model-absent card is PINNED (critique Q6): the group first, the card
    after (the card's own lines stay one-bubble-per-line)."""
    events: list[EngineEvent] = []
    emitter = EventEmitter(events.append)
    commands: queue.Queue[Any] = queue.Queue()
    commands.put(app.QUIT)
    prov = _fresh_provision(tmp_path, lambda _s: None)
    model = app.ModelDownloadFlow(model_name="x")
    assert model.state == "absent"
    app._pump(
        _make_loop(),
        emitter.text,
        commands,
        flow=TxFlow(),
        session=app.SendSession(),
        table={},
        emitter=emitter,
        provision=prov,
        model=model,
    )
    assert [(e.kind, e.payload) for e in events] == [
        (EVENT_TEXT, GREETING_BUBBLE), (EVENT_TURN_END, ""),
        (EVENT_TEXT, app.MODEL_CARD_QUESTION), (EVENT_TURN_END, ""),
        (EVENT_TEXT, app.MODEL_CARD_HINT), (EVENT_TURN_END, ""),
    ]


def test_configured_launch_emits_no_beats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """A launch that already carries wiring (the keyed web launch presets
    provision.wiring) is NOT a fresh needs_watch_key launch: no beats."""
    wiring, _cq = _mk_wiring(tmp_path, monkeypatch, stored_url=GOOD_URL)
    prov = _fresh_provision(tmp_path, lambda _s: None)
    prov.wiring = wiring
    events: list[EngineEvent] = []
    emitter = EventEmitter(events.append)
    commands: queue.Queue[Any] = queue.Queue()
    commands.put(app.QUIT)
    app._pump(
        wiring.loop,
        emitter.text,
        commands,
        flow=wiring.flow,
        session=wiring.session,
        table=wiring.table,
        client=wiring.client,
        emitter=emitter,
        scan=wiring.scan,
        store=wiring.store,
        provision=prov,
        settings=wiring.settings,
    )
    assert _texts(events) == []  # no greeting beats, no card (model None)
    wiring.worker.stop()
    wiring.client.close()
    wiring.store.close()


def test_beats_are_ordinary_output_lines_for_the_cli_transport(
    tmp_path: Path, env_clean: None
) -> None:
    """CLI parity (requirement 6): the greeting group is a plain output_fn
    line — a transport without an emitter renders it verbatim (the joined
    "\n" prints as the same three terminal lines), nothing else."""
    lines: list[str] = []
    commands: queue.Queue[Any] = queue.Queue()
    commands.put(app.QUIT)
    prov = _fresh_provision(tmp_path, lines.append)
    app._pump(
        _make_loop(),
        lines.append,
        commands,
        flow=TxFlow(),
        session=app.SendSession(),
        table={},
        provision=prov,
    )
    assert lines == [GREETING_BUBBLE]


class _InstantRuntime:
    """Duck-typed ModelRuntime whose ``load()`` returns at once (test_launch
    pattern; a real GGUF is never touched)."""

    def load(self) -> None:
        return None


def test_startup_bubble_sequence_grouped_greeting_then_preload_notices(
    tmp_path: Path, env_clean: None
) -> None:
    """THE startup bubble sequence pin (UX-012/UX-009 + static-half
    correction): a fresh launch with a resolved model runs
    [greeting group] → [Loading local llm.] → [Local llm fully loaded.] —
    the grouped greeting is ONE closed turn at pump entry (before the loop
    reads any command), and each preload notice arrives as its OWN
    text-then-turn_end pair (its own bubble): the transport arms
    PRELOAD_START only after its launch lines print, so it always follows
    the beats."""
    events: list[EngineEvent] = []
    emitter = EventEmitter(events.append)
    commands: queue.Queue[Any] = queue.Queue()
    prov = _fresh_provision(tmp_path, lambda _s: None)
    flow = app.ModelPreloadFlow(
        _InstantRuntime(),  # type: ignore[arg-type]
        model_path=str(tmp_path / "m.gguf"),
    )
    thread = threading.Thread(
        target=lambda: app._pump(
            _make_loop(),
            emitter.text,
            commands,
            flow=TxFlow(),
            session=app.SendSession(),
            table={},
            emitter=emitter,
            provision=prov,
            preload=flow,
        ),
        daemon=True,
    )
    thread.start()
    commands.put(app.PRELOAD_START)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and flow.state != "ready":
        time.sleep(0.02)
    assert flow.state == "ready", "preload marker never consumed"
    commands.put(app.QUIT)
    thread.join(15)
    assert not thread.is_alive()
    assert _closed_turns(events) == [
        GREETING_BUBBLE,
        app.MODEL_PRELOAD_NOTICE,
        app.MODEL_PRELOADED_NOTICE,
    ]


# ------------------------------------------------------ 2. zpub-in-chat (pump)


def _fresh_provision(tmp_path: Path, output_fn: Callable[[str], None]) -> Any:
    """The REAL WatchKeyProvision an unprovisioned web launch builds (no
    wiring yet — provisioning it runs _wire through the gated parse path)."""
    return app.WatchKeyProvision(
        settings=Settings(store_path=str(tmp_path / "p.db"), watch_interval_s=0.0),
        signer_selection=app.SignerSelection(
            kind="file", dir_path=tmp_path / "psbt", fingerprint_hex="00000000"
        ),
        env_gap=None,
        rescan=False,
        flow=None,
        generate=app.stub_generate,
        node_detect_fn=None,
        output_fn=output_fn,
    )


def _drive_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lines: list[str],
) -> tuple[list[EngineEvent], Any]:
    """An unprovisioned pump over the REAL WatchKeyProvision (chain-client
    construction patched — provisioning reaches _wire but the held scan
    never fetches)."""
    monkeypatch.setattr(
        app,
        "_build_chain_client",
        lambda settings, auth=None: _FakeChain("http://held.local"),
    )
    events: list[EngineEvent] = []
    emitter = EventEmitter(events.append)
    commands: queue.Queue[Any] = queue.Queue()
    for line in lines:
        commands.put(line)
    commands.put(app.QUIT)
    prov = _fresh_provision(tmp_path, lambda _s: None)
    app._pump(
        _make_loop(),
        emitter.text,
        commands,
        flow=TxFlow(),
        session=app.SendSession(),
        table={},
        emitter=emitter,
        provision=prov,
    )
    if prov.wiring is not None:
        prov.wiring.worker.stop()
        if prov.wiring.client is not None:  # unresolved launch: no client
            prov.wiring.client.close()
        prov.wiring.store.close()
    return events, prov


def test_chat_zpub_provisions_and_emits_saved_and_backend_beats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None,
    echo_turns: list[str],
) -> None:
    """The golden chat path: the grouped greeting bubble → the pasted key
    (echoed only REDACTED) → "Great. I saved that." (its own bubble) → the
    backend beat as ONE grouped bubble.
    The key persisted through the existing gated path, the scan stayed HELD
    (zero chain traffic), and the key never rode any event verbatim."""
    events, prov = _drive_fresh(tmp_path, monkeypatch, [ZPUB])
    assert _closed_turns(events) == [
        GREETING_BUBBLE, SAVED, BACKEND_BUBBLE,
    ]
    # The paste echoes onto the bus ONLY as the user's own user_text line
    # (the WEB-011 single-choke-point contract — a chat line never vanishes
    # from the transcript). No app-authored event carries the key.
    assert all(ZPUB not in e.payload for e in events if e.kind != EVENT_USER_TEXT)
    assert any(e.kind == EVENT_USER_TEXT and ZPUB in e.payload for e in events)
    assert echo_turns == []  # zero model contact on the whole path
    assert prov.wiring is not None
    store = Store(str(tmp_path / "p.db"))
    try:
        active = store.get_active_wallet()
        assert active is not None
        assert active.descriptor == WalletDescriptor.from_key(ZPUB).descriptor
    finally:
        store.close()
    # ONB-006 promise: provisioning started NO chain work — since
    # TCK-DESCOPE-M3A the pin is stronger: unresolved means there is NO
    # wallet client to fetch with at all (no silent public stand-in).
    assert prov.wiring.scan is not None
    assert prov.wiring.scan.gate.state == "awaiting_backend"
    assert prov.wiring.client is None


def test_backend_beats_suppressed_when_a_rung_already_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """The backend beat bubble is gated on the choice being genuinely
    OPEN: when an operator rung already resolves the backend (the way
    Settings.from_env folds env/config into settings.chain_base_url before
    the pump ever runs), the key still saves — but the app never asks for a
    server the operator already chose."""
    monkeypatch.setattr(
        app,
        "_build_chain_client",
        lambda settings, auth=None: _FakeChain("http://operator.local"),
    )
    events: list[EngineEvent] = []
    emitter = EventEmitter(events.append)
    commands: queue.Queue[Any] = queue.Queue()
    commands.put(ZPUB)
    commands.put(app.QUIT)
    prov = app.WatchKeyProvision(
        settings=Settings(
            store_path=str(tmp_path / "op.db"),
            watch_interval_s=0.0,
            chain_base_url=NEW_URL,  # the boot-folded operator rung
        ),
        signer_selection=app.SignerSelection(
            kind="file", dir_path=tmp_path / "psbt", fingerprint_hex="00000000"
        ),
        env_gap=None,
        rescan=False,
        flow=None,
        generate=app.stub_generate,
        node_detect_fn=None,
        output_fn=lambda _s: None,
    )
    app._pump(
        _make_loop(),
        emitter.text,
        commands,
        flow=TxFlow(),
        session=app.SendSession(),
        table={},
        emitter=emitter,
        provision=prov,
    )
    texts = _texts(events)
    assert SAVED in texts  # the key landed
    # no ask for an already-chosen server (substring proof: the beats may
    # never appear alone OR inside a grouped bubble)
    assert not any(BEAT_ASK in t for t in texts)
    if prov.wiring is not None:
        prov.wiring.worker.stop()
        if prov.wiring.client is not None:  # unresolved launch: no client
            prov.wiring.client.close()
        prov.wiring.store.close()


def test_pasted_key_is_not_intercepted_once_wired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None,
    echo_turns: list[str],
) -> None:
    """The pinned NEGATIVE (critique Q2): once the engine is provisioned, a
    pasted key in chat is ordinary model chat — re-provisioning stays the
    gated /watchkey REPLACE path only, never a swallowed chat line."""
    events, _prov = _drive_fresh(tmp_path, monkeypatch, [ZPUB, YPUB])
    assert echo_turns == [YPUB]  # the SECOND paste reached the model turn
    assert _texts(events).count(SAVED) == 1
    store = Store(str(tmp_path / "p.db"))
    try:
        active = store.get_active_wallet()
        assert active is not None
        assert active.descriptor == WalletDescriptor.from_key(ZPUB).descriptor
    finally:
        store.close()


@pytest.mark.parametrize(
    ("line", "needle"),
    [
        (SEED_LINE, app._WATCHKEY_SEED),
        (VPUB, "mainnet-only"),
        (XPRV, "private extended keys are never handled"),
        ("zpub-not-really-a-key-at-all", "not a valid extended key"),
    ],
)
def test_unprovisioned_chat_refusals_reuse_the_gated_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None,
    echo_turns: list[str], line: str, needle: str,
) -> None:
    """Seed/mainnet/private/garbage refusals on the chat path are the
    EXISTING gated provision path's own value-free lines (reused, not
    duplicated); nothing persisted and the launch stayed unprovisioned (the
    key ask never becomes a model turn)."""
    events, prov = _drive_fresh(tmp_path, monkeypatch, [line])
    assert any(needle in t for t in _texts(events))
    assert SAVED not in _texts(events)
    assert prov.wiring is None
    assert echo_turns == []  # refusal never reaches the model
    store = Store(str(tmp_path / "p.db"))
    try:
        assert store.list_wallets() == []
    finally:
        store.close()


def test_refusal_and_seed_lines_are_value_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None
) -> None:
    """Every APP-authored line on the refusal path is value-free: the
    gated parser's own reason (never the key), and no seed word, testnet
    key, or private key rides any event the engine emits. (The user's own
    paste echoes once on the user_text bus by the WEB-011 contract — that
    is their message rendered back, never a log and never app copy.)"""
    events, _prov = _drive_fresh(
        tmp_path, monkeypatch, [SEED_LINE, VPUB, XPRV]
    )
    app_lines = "\n".join(
        e.payload for e in events if e.kind != EVENT_USER_TEXT
    )
    assert "bacon" not in app_lines  # seed words never in app output
    assert VPUB not in app_lines and XPRV not in app_lines


# ------------------------------------------------- 5. "ask me how" (no model)


@pytest.mark.parametrize(
    ("line", "helped"),
    [
        ("how do I get my xpub?", True),
        ("How do I find the zpub", True),
        ("not sure how to export a key", True),
        ("how for help", True),  # word-level matcher (pinned)
        # Code-review MINOR (matcher order): a long lowercase help question
        # is BIP39-SHAPE-matching — HELP must win over the seed refusal
        # (a real seed phrase can never contain "how": not a BIP39 word).
        (
            ("how does one export the account public key from the sparrow "
             "wallet settings menu"),
            True,
        ),
        ("where is my xpub", False),  # no "how"
        ("how are you", False),  # no topic word
        ("help", False),  # no "how" (pinned tight)
        ("what is a zpub", False),
    ],
)
def test_key_help_matcher_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None,
    echo_turns: list[str], line: str, helped: bool,
) -> None:
    """The PINNED help trigger matcher (critique Q6): "how" + a key topic
    word while needs_watch_key answers with the deterministic export line
    (pre-model); every other line keeps the watch-key refusal."""
    events, _prov = _drive_fresh(tmp_path, monkeypatch, [line])
    texts = _texts(events)
    if helped:
        assert app.CHAT_ONB_KEY_HOWTO in texts
        assert app.WATCHKEY_REQUIRED_NOTICE not in texts
        assert app._WATCHKEY_SEED not in texts  # help beats the seed shape
    else:
        assert app.WATCHKEY_REQUIRED_NOTICE in texts
    assert echo_turns == []  # zero model contact either way


def test_help_line_is_generic_watch_only_guidance() -> None:
    """The help copy: Jade menu + Sparrow export guidance, seed phrases
    refused with hardware-only wording, and NOTHING user-shaped in it."""
    line = app.CHAT_ONB_KEY_HOWTO
    assert "Jade" in line and "Sparrow" in line
    assert "seed" in line.lower()
    assert "hardware-wallet-only" in line.lower()
    assert "import" not in line.lower()  # never a software-seed-import path


# ---------------------------------------- 3. chat-URL intercept (state-gated)


def _wired_pump(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lines: list[str],
    *,
    probe: Callable[[str], str | None],
    stored_url: str | None = None,
) -> tuple[list[EngineEvent], Any]:
    """A PROVISIONED pump with the ONB-006 hold armed (unless ``stored_url``
    resolves the ladder up front) over a fake chain client and an injected
    probe (the settings-POST seam's own shape). Mirrors ``_wire``'s fold of
    the effective backend into settings.chain_base_url."""
    wiring, _cq = _mk_wiring(tmp_path, monkeypatch, stored_url=stored_url)
    # _wire folds the resolved (env>config>stored) URL into settings; _mk_wiring
    # leaves it boot-empty, so reproduce the fold the pump's gate reads.
    if stored_url is not None:
        wiring.settings.chain_base_url = stored_url
    else:
        wiring.scan.set_startup_deferred()  # type: ignore[union-attr]
    backend = ChainBackendFlow(wiring, probe)
    events: list[EngineEvent] = []
    emitter = EventEmitter(events.append)
    commands: queue.Queue[Any] = queue.Queue()
    for line in lines:
        commands.put(line)
    commands.put(app.QUIT)
    app._pump(
        wiring.loop,
        emitter.text,
        commands,
        flow=wiring.flow,
        session=wiring.session,
        table=wiring.table,
        client=wiring.client,
        emitter=emitter,
        scan=wiring.scan,
        store=wiring.store,
        backend=backend,
        settings=wiring.settings,
    )
    wiring.worker.stop()
    wiring.client.close()
    wiring.store.close()
    return events, wiring


def _reopen(tmp_path: Path) -> Store:
    """The wiring's store closes with the pump; WAL lets a fresh connection
    read exactly what the ENGINE persisted."""
    return Store(str(tmp_path / "swap.db"))


def test_chat_url_rides_probe_store_swap_and_releases_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None,
    echo_turns: list[str],
) -> None:
    """The chat paste IS the /setup outcome: the probe is called with the
    pasted URL, the canonical value lands through the typed writer, the
    confirmation copy rides out, and the held scan releases on the CHOSEN
    server (the resync fetches from the new client). Zero model contact."""
    seen: list[str] = []

    def probe(url: str) -> str | None:
        seen.append(url)
        return url

    events, wiring = _wired_pump(tmp_path, monkeypatch, [NEW_URL], probe=probe)
    assert seen == [NEW_URL]
    texts = _texts(events)
    assert CONFIRMED in texts and SWITCHING_NOW in texts
    # Code-review MINOR (ack shape): each ack line closes its OWN turn —
    # one bubble per line, consistent with the onboarding beats (no two-
    # texts-one-bubble grouping).
    closed = _closed_turns(events)
    assert CONFIRMED in closed and SWITCHING_NOW in closed
    assert echo_turns == []
    probe_store = _reopen(tmp_path)
    try:
        assert probe_store.get_chain_base_url() == NEW_URL
    finally:
        probe_store.close()
    assert wiring.settings.chain_base_url == NEW_URL  # the ONE selection point
    assert wiring.worker._client.base_url == NEW_URL  # served by the CHOSEN one
    assert wiring.scan.gate.state != "awaiting_backend"
    assert wiring.worker._client.fetches > 0  # the released scan ran on it


def test_chat_url_probe_failure_refuses_value_free_and_saves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None,
    echo_turns: list[str],
) -> None:
    """A failed probe = the value-free refusal, NOTHING saved, the ask
    stays open (the same URL rides the probe again — never a dead end,
    never a swallowed model turn, never a silent public fallback)."""
    calls: list[str] = []

    def probe(url: str) -> str | None:
        calls.append(url)
        return None

    events, wiring = _wired_pump(
        tmp_path, monkeypatch, [NEW_URL, NEW_URL], probe=probe
    )
    assert calls == [NEW_URL, NEW_URL]
    texts = _texts(events)
    assert texts.count(app.BACKEND_PROBE_FAIL) == 2
    assert CONFIRMED not in texts
    assert echo_turns == []
    probe_store = _reopen(tmp_path)
    try:
        assert probe_store.get_chain_base_url() is None
    finally:
        probe_store.close()
    assert wiring.scan.gate.state == "awaiting_backend"  # still held


def test_resolved_launch_never_swallows_a_chat_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None,
    echo_turns: list[str],
) -> None:
    """NO-SWALLOW state gate (critique Q4), shape (a): a launch whose
    backend is already resolved sends a URL chat line to the model
    untouched — the probe is never called, no confirmation rides out."""
    probed: list[str] = []

    def probe(url: str) -> str | None:
        probed.append(url)
        return url

    _events, _wiring = _wired_pump(
        tmp_path, monkeypatch, [NEW_URL], probe=probe, stored_url=GOOD_URL
    )
    assert echo_turns == [NEW_URL]  # ordinary model chat
    assert probed == []  # the probe never ran — the paste was not intercepted


def test_second_url_after_a_success_is_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None,
    echo_turns: list[str],
) -> None:
    """NO-SWALLOW state gate, shape (b): within ONE unresolved session the
    first chat URL sets the backend (resolves it); a SECOND URL paste in the
    same session now sees a resolved ladder and is ordinary chat — the
    success applied once and never re-opened the intercept."""
    applied: list[str] = []

    def probe(url: str) -> str | None:
        applied.append(url)
        return url

    events, _wiring = _wired_pump(
        tmp_path, monkeypatch, [GOOD_URL, NEW_URL], probe=probe
    )
    assert applied == [GOOD_URL]  # only the first rode the probe/store path
    assert echo_turns == [NEW_URL]  # the second reached the model
    assert _texts(events).count(CONFIRMED) == 1


def test_chat_url_refusal_carries_the_diag_001_class_and_clamp(
    env_clean: None,
) -> None:
    """The probe the chat path SHARES with the settings-POST write (the ONE
    closure ``_wire`` builds) surfaces the value-free DIAG-001 companion
    with the CLAMPED url_class — a host can never ride the debug line. The
    chat intercept rides ``ChainBackendFlow.apply`` → that same closure, so
    the discipline is inherited, not re-implemented."""

    class _Recorder:
        def __init__(self) -> None:
            self.warnings: list[str] = []

        def warning(self, line: str) -> None:
            self.warnings.append(line)

    rec = _Recorder()
    settings = Settings(request_timeout_s=1.0, max_retries=0)
    assert app._probe_chain_backend("ftp://127.0.0.1:9/", settings, None, rec) is None
    assert any(
        "stage=scheme-rejected" in w and "url-class=unknown" in w
        for w in rec.warnings
    )
    assert all("127.0.0.1" not in w for w in rec.warnings)


# ------------------------------------------------------- 4. public via chat


@pytest.mark.parametrize(
    "line",
    [
        "use public",
        "public",
        "public server",
        "I'll use the public server",
        "the public server",
    ],
)
def test_public_accept_vectors_still_match(line: str) -> None:
    """The veto expansion must not over-veto: the ticket's own "use
    public" phrasing family still indicates the choice (consent itself
    still rides the 001B seam — a match only ROUTES there)."""
    assert app._chat_public_choice(line)


def test_chat_public_routes_through_the_consent_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None,
    echo_turns: list[str],
) -> None:
    """"use public" rides the SAME 001B discipline as the consent button:
    the seam records the marker and releases the held scan, and the
    narration IS the leak disclosure (operator-sees-it hedge, reused copy) —
    never an un-flagged accept. "Loading" only claims what actually started."""
    events, wiring = _wired_pump(
        tmp_path, monkeypatch, ["use public"], probe=lambda u: u
    )
    texts = _texts(events)
    assert PUBLIC_CHOSEN_ACK in texts
    assert "whoever runs it sees" in PUBLIC_CHOSEN_ACK  # the hedge, verbatim
    assert PUBLIC_LOADING_NOW in texts  # the release REPORTED a start
    # Each ack line closes its OWN turn (MINOR 3 consistency: one bubble
    # per line, the beat pattern — never a two-texts-one-bubble pair).
    closed = _closed_turns(events)
    assert PUBLIC_CHOSEN_ACK in closed and PUBLIC_LOADING_NOW in closed
    assert echo_turns == []  # the public answer never reached the model
    probe_store = _reopen(tmp_path)
    try:
        assert probe_store.get_setting(BACKEND_CHOICE_SETTING) == BACKEND_CHOICE_PUBLIC
    finally:
        probe_store.close()
    assert wiring.scan.gate.state != "awaiting_backend"


@pytest.mark.parametrize(
    "line",
    [
        "what does public mean?",
        "tell me about public servers",
        "I don't want public",
        "is public safer",
        "why is the public server an option at all here really",
        "switch to my node",
        # Security-review vectors (the marker is DURABLE — an ambiguous
        # utterance must never pin the public posture): the reviewer's
        # three accidental-consent lines...
        "doesn't public mean everyone sees",
        "isn't public safer",
        "skip the public",
        # ...and the contraction family the veto set now carries in its
        # apostrophe-stripped form (each is vetoed ONLY by the
        # contraction — no other veto word present):
        "won't public work",
        "wouldn't public be okay",
        "aren't public servers monitored",
        "shouldn't public be accepted",
        "hasn't public been enough",
        "couldn't I use public",
    ],
)
def test_public_questions_and_rejections_are_never_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_clean: None,
    echo_turns: list[str], line: str,
) -> None:
    """The pinned reject vectors: a question ABOUT public (or a rejection,
    or >6 words, or a trailing "?") never records consent — the line stays
    ordinary chat and the scan stays held."""
    _events, wiring = _wired_pump(
        tmp_path, monkeypatch, [line], probe=lambda u: u
    )
    assert echo_turns == [line]
    probe_store = _reopen(tmp_path)
    try:
        assert probe_store.get_setting(BACKEND_CHOICE_SETTING) is None
    finally:
        probe_store.close()
    assert wiring.scan.gate.state == "awaiting_backend"


# ---------------------------------------------- stream ordering (banner pin)


def test_banner_precedes_beats_on_the_live_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ORDER vs the model-absent BANNER pinned at the real SSE stream: the
    buffered banner flushes ahead of the pump, so it arrives BEFORE the
    greeting beats (which precede the card — pump-level pin above). Replayed
    from event 0 so the whole stream is the record."""
    thread, _outputs, capture = _launch_first_run(tmp_path, monkeypatch)
    server = capture["server"]
    stream = _Stream(server, last_event_id=0)
    try:
        stream.read_head()
        stream.read_until(app.NO_MODEL_DEMO_BANNER.encode(), timeout=20)
        head = bytes(stream.buf)  # everything up to & including the banner
        stream.read_until(GREET.encode(), timeout=20)
        cut = len(stream.buf)
        assert head  # banner arrived before the first beat
        # the greeting GROUP is ONE SSE event (all three lines ride the
        # same frame as consecutive data: lines — one bubble, replayed as
        # one) and it precedes anything provisioning-shaped:
        frames = stream.buf.split(b"\n\n")
        greeting_frames = [f for f in frames if GREET.encode() in f]
        assert len(greeting_frames) == 1
        assert KEY_ASK.encode() in greeting_frames[0]
        assert HELP_OFFER.encode() in greeting_frames[0]
        assert SAVED.encode() not in stream.buf[:cut]
        assert app.NO_MODEL_DEMO_BANNER.encode() in head
    finally:
        stream.close()
        server.stop()
        thread.join(15)


def test_chat_zpub_provisions_through_the_real_web_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole golden path through the shipped transport: the pasted key
    arrives as an ordinary /turn chat line (no form), provisioning ran on
    the engine thread, the ack + backend beats reached the stream, /state
    flipped out of needs_watch_key with the scan still HELD, and zero chain
    traffic happened before any backend choice."""
    thread, _outputs, capture = _launch_first_run(tmp_path, monkeypatch)
    server = capture["server"]
    stream = _Stream(server, last_event_id=0)
    try:
        stream.read_head()
        status, _h, _d, _r = _request(
            server, "POST", "/turn", {"text": ZPUB}, token=server.token
        )
        assert status == 202
        stream.read_until(SAVED.encode(), timeout=20)
        stream.read_until(BEAT_ASK.encode(), timeout=20)
        # the backend beat GROUP is ONE SSE event (the greeting group's
        # mirror structure), and SAVED rode its own frame before it:
        saved_frame = [f for f in stream.buf.split(b"\n\n") if SAVED.encode() in f]
        assert len(saved_frame) == 1 and BEAT_ASK.encode() not in saved_frame[0]
        beat_frames = [f for f in stream.buf.split(b"\n\n") if BEAT_ASK.encode() in f]
        assert len(beat_frames) == 1
        assert BEAT_OWN.encode() in beat_frames[0]
        assert BEAT_PUBLIC.encode() in beat_frames[0]
        snap = _session_state(server)
        assert "needs_watch_key" not in snap
        assert snap["scan_state"] == "awaiting_backend"
        assert capture["calls"] == []  # the ONB-006 promise, end to end
        # The key rides ONLY the user's own echoed line (WEB-011: every
        # submitted utterance echoes as user_text before any branch) —
        # never an app-authored frame, never a refusal, never a beat.
        frames = stream.buf.decode("utf-8").split("\n\n")
        assert all("event: user_text" in f for f in frames if ZPUB in f)
        # Now answer the backend beats in chat: public. Consent releases the
        # held scan onto the (mock) public server — the ONLY traffic so far.
        status, _h, _d, _r = _request(
            server, "POST", "/turn", {"text": "use public"}, token=server.token
        )
        assert status == 202
        stream.read_until(b"chosen with eyes open", timeout=20)
        # Traffic began AFTER the explicit public choice — the released
        # scan fetches on the CHAIN worker thread, so give the worker a
        # moment to land its first request (the pre-consent assert above
        # already pins that nothing reached the chain earlier).
        deadline = time.monotonic() + 10
        while not capture["calls"] and time.monotonic() < deadline:
            time.sleep(0.02)
        assert capture["calls"]
    finally:
        stream.close()
        server.stop()
        thread.join(15)

"""TCK-ONB-003 — first-run onboarding conversation (ADR-0023).

Every branch of the five-step flow, driven through ``app.run()`` with
injected I/O (the established harness pattern) — plus ``chain.check_backend``
at the transport seam and the stored-rung/banner consistency pins.

Branches pinned (ticket requirement 6): fresh-user full flow (steps 1-5),
seed-word refusal at the key ask, node-ask skip, URL accepted, URL rejected
→ doctor guidance → explicit public pick (never a silent fallback), syncing
loopback node refused with doctor facts verbatim, env-preset → onboarding
skipped, second run (stored choice) → skipped + banner flips from the
stored rung, headless launch → silently skipped, web launch → one-line
hint only. Onboarding never contacts the model (counter-seamed
``generate_fn``) and nothing sensitive is ever echoed.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from localwallet import app as app_module
from localwallet.app import (
    AUTO_SCAN_ENV_VAR,
    PRIVACY_INDICATOR,
    PRIVACY_INDICATOR_OWN_NODE_LOCAL,
    run,
    stub_generate,
)
from localwallet.chain import MAINNET_GENESIS_HASH, EsploraClient, check_backend
from localwallet.node import LocalNodeReport, NodeStatus
from localwallet.node.detect import CoreHealth, CoreRpcProbe
from localwallet.store import Store
from localwallet.ui import onboarding as ob
from localwallet.wallet.descriptor import WalletDescriptor

# Public fixture key material ONLY (the canonical suite zpub, one fixed seed).
from tests.test_e2e_skeleton import XPRV, ZPUB

# The web test-door seam (wake the pump's watch drain via a real /state).
from tests.test_web_server import _request

TESTNET_GENESIS = "000000000933ea01ad0ee984209779baaec3ced90fa3f408719526f8d77f4943"


def _web_stream_contains(server: Any, needle: str, timeout: float = 10.0) -> bool:
    """Whether the server's SSE stream delivered ``needle``. Web narration
    routes to the emitter, not the terminal (TCK-APP-LOG-001), so the
    one-line setup hint is read back through /events here. The engine binds
    the emitter a moment after bootstrap returns, so this polls with fresh
    connections (each replays the retained ring) up to ``timeout`` rather
    than racing that single flush."""
    import socket
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = socket.create_connection(
                ("127.0.0.1", server.httpd.server_address[1]), timeout=2
            )
        except OSError:
            time.sleep(0.05)
            continue
        s.sendall(
            (
                "GET /events HTTP/1.0\r\nHost: 127.0.0.1\r\n"
                f"X-Auth-Token: {server.token}\r\n\r\n"
            ).encode()
        )
        buf = b""
        end = min(deadline, time.monotonic() + 2)
        try:
            while needle.encode() not in buf and time.monotonic() < end:
                s.settimeout(max(0.1, end - time.monotonic()))
                try:
                    chunk = s.recv(65536)
                except TimeoutError:
                    break
                if not chunk:
                    break
                buf += chunk
        finally:
            s.close()
        if needle.encode() in buf:
            return True
        time.sleep(0.05)
    return False
GOOD_URL = "https://mempool.mine.example:4000/api"
LOCAL_URL = "http://127.0.0.1:3006"
SEED_LINE = "bacon " * 12  # 12 BIP39-shaped words (canonical test phrase)


# ----------------------------------------------------------------- harness


def _tip_or_empty_handler(request: httpx.Request) -> httpx.Response:
    """Fresh-wallet startup scan over MockTransport: tip + empty everywhere."""
    if request.url.path.endswith("/blocks/tip"):
        return httpx.Response(200, json=870_000)
    return httpx.Response(200, json=[])


def _fake_client() -> EsploraClient:
    return EsploraClient(
        base_url="https://mempool.space/api",
        timeout_s=5.0,
        max_retries=0,
        transport=httpx.MockTransport(_tip_or_empty_handler),
    )


def _counting_client(calls: list[int], target: Any = 1) -> EsploraClient:
    """A fake backend that COUNTS every chain request — the TCK-ONB-006
    leak pin: the count must stay 0 until a backend choice resolves.
    TCK-BACKEND-002: ``target`` tags WHICH client served (the harness
    passes the base_url it was constructed with) so the pin can tell
    "loaded from the server the user chose" apart from "leaked to the
    public default they refused" — an int stays recorded for legacy
    count-only uses."""
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(target)
        return _tip_or_empty_handler(request)

    return EsploraClient(
        base_url="https://mempool.space/api",
        timeout_s=5.0,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )


class Recorder:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def emit(self, line: str) -> None:
        self.lines.append(line)

    @property
    def joined(self) -> str:
        return "\n".join(self.lines)


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    lines: list[str],
    argv: list[str] | None = None,
    interactive: bool | None = False,
    auto_scan: bool = False,
    backend_check: Callable[[str], bool] | None = None,
    node_report: LocalNodeReport | None = None,
    chain_env: str | None = None,
    detection_enabled: bool = True,
    client: Callable[..., EsploraClient] | None = None,
) -> tuple[int, Recorder, str | None, dict[str, int]]:
    """Run a scripted session; returns ``(code, recorder, stored, state)``.

    ``state`` counts ``probes`` (chain backend checks), ``detects`` (node
    detection passes) and ``model`` (model calls) — every onboarding
    interaction must keep ``model`` at zero for its own lines.
    """
    for var in (
        "LOCALWALLET_MODEL_PATH",
        "LOCALWALLET_LLM_BASE_URL",
        "LOCALWALLET_LLM_MODEL",
        "LOCALWALLET_CHAIN_BASE_URL",
        app_module.ZPUB_ENV_VAR,
        app_module.UI_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    if chain_env is not None:
        monkeypatch.setenv("LOCALWALLET_CHAIN_BASE_URL", chain_env)
    monkeypatch.setenv(
        "LOCALWALLET_NODE_DETECTION_ENABLED", "1" if detection_enabled else "0"
    )
    store_path = tmp_path / "onb.db"
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(store_path))
    monkeypatch.setenv(AUTO_SCAN_ENV_VAR, "1" if auto_scan else "0")
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")
    monkeypatch.setattr(
        app_module, "EsploraClient", client or (lambda **_kw: _fake_client())
    )

    state = {"probes": 0, "detects": 0, "model": 0}

    def check(url: str) -> str | None:
        # M3 seam contract: candidate in, canonical URL out (None refuses).
        # The test-facing ``backend_check`` keeps its old bool vocabulary;
        # identity-rewrite stands for "the probe agreed it is what it is".
        state["probes"] += 1
        ok = backend_check(url) if backend_check is not None else False
        return url if ok else None

    def detect() -> LocalNodeReport:
        state["detects"] += 1
        return node_report if node_report is not None else _report_synced()

    def generate(prompt: str, grammar_text: str | None) -> str:
        state["model"] += 1
        return stub_generate(prompt, grammar_text)

    inputs = iter(lines)
    rec = Recorder()
    code = run(
        argv if argv is not None else ["--stub-llm", "--zpub", ZPUB],
        input_fn=lambda _p: next(inputs),
        output_fn=rec.emit,
        generate_fn=generate,
        node_detect_fn=detect,
        backend_check_fn=check,
        interactive=interactive,
    )
    store = Store(str(store_path))
    try:
        stored = store.get_chain_base_url()
    finally:
        store.close()
    return code, rec, stored, state


def _preset_wallet(tmp_path: Path, *, base_url: str | None = None) -> Path:
    """A store carrying the fixture wallet (+ optional stored choice):
    the returning-user state a second run sees."""
    path = tmp_path / "onb.db"
    store = Store(str(path))
    try:
        store.create_wallet(
            name="default",
            descriptor=WalletDescriptor.from_key(ZPUB).descriptor,
        )
        if base_url is not None:
            store.set_chain_base_url(base_url)
    finally:
        store.close()
    return path


def _core_report(health: CoreHealth, port: int, mempool: NodeStatus) -> LocalNodeReport:
    return LocalNodeReport(
        core=(
            CoreRpcProbe(
                port=port,
                status=NodeStatus.REACHABLE,
                cookie_present=True,
                health=health,
            ),
        ),
        mempool=mempool,
        electrs=NodeStatus.OFFLINE,
    )


def _report_synced() -> LocalNodeReport:
    return _core_report(
        CoreHealth(
            chain="main",
            blocks=870_000,
            headers=870_000,
            verification_progress=1.0,
            initial_block_download=False,
        ),
        8332,
        NodeStatus.REACHABLE,
    )


def _report_syncing() -> LocalNodeReport:
    return _core_report(
        CoreHealth(
            chain="main",
            blocks=400_000,
            headers=800_000,
            verification_progress=0.5,
            initial_block_download=True,
        ),
        8332,
        NodeStatus.OFFLINE,
    )


def _report_regtest() -> LocalNodeReport:
    return _core_report(
        CoreHealth(
            chain="regtest",
            blocks=200,
            headers=200,
            verification_progress=1.0,
            initial_block_download=False,
        ),
        18443,
        NodeStatus.OFFLINE,
    )


def _public_marker(tmp_path: Path) -> str | None:
    """The explicit-public record (:data:`ob.BACKEND_CHOICE_SETTING`) —
    ``None`` until a warned public consent is written (TCK-ONB-006)."""
    store = Store(str(tmp_path / "onb.db"))
    try:
        return store.get_setting(ob.BACKEND_CHOICE_SETTING)
    finally:
        store.close()


# ------------------------------------------------- fresh-user full flow


def test_fresh_user_full_flow_five_steps(tmp_path: Path, monkeypatch) -> None:
    """Steps 1→5 end to end ON A DEFERRED first run (TCK-ONB-006, ADR-0022
    amendment 1), now with the TCK-BACKEND-002 hot-swap (ADR-0018
    amendment): greeting+key ask → node ask + LOAD_WAIT (nothing is loading
    — the scan is HELD for the choice) → guide (ask stays open) → 2 → URL →
    (d); the choice lands in the store AND the held scan releases IN-SESSION
    ON THE CHOSEN SERVER (no more next-launch wait, no restart line): the
    swap closes the public-default client and loads the wallet from the new
    one, so LOAD_COMPLETE arrives this session. The leak invariant STANDS —
    every chain request after the choice rides the user's OWN server; the
    public default they refused is never fetched through (pinned per
    base_url). Only the ONE chat turn after the flow closes touches the
    model."""
    calls: list[Any] = []
    code, rec, stored, state = _drive(
        monkeypatch,
        tmp_path,
        lines=[ZPUB, "what's a node?", "2", GOOD_URL, "tell me a fact", "exit"],
        argv=["--stub-llm"],  # NO --zpub: the conversation asks for the key
        interactive=True,
        auto_scan=True,
        backend_check=lambda _url: True,
        client=lambda **kw: _counting_client(calls, kw.get("base_url")),
    )
    assert code == 0
    joined = rec.joined
    assert joined.count(ob.GREETING) == 1
    assert ob.NODE_ASK in joined
    assert ob.LOAD_WAIT in joined  # deferred variant (ONB-006), not…
    assert ob.LOAD_NARRATION not in joined  # …the "already loading" promise
    assert ob.GUIDE in joined  # (f) — then the ask stayed open
    assert ob.URL_PROMPT in joined  # (b)
    assert ob.CONFIRMED in joined  # (d)
    assert ob.SWITCHING_NOW in joined  # TCK-BACKEND-002: swapped + resyncing
    assert ob.EFFECTS_NEXT_LAUNCH not in joined  # the swap made it live now
    assert ob.DEFERRED_RESTART not in joined  # no restart wait anymore
    assert ob.LOAD_COMPLETE in joined  # the load finished THIS session
    assert "Startup scan complete" in joined
    # THE leak pin (amended): after the own-server choice the held scan
    # releases and runs — but ONLY against the chosen server; the public
    # default the user refused never serves a single request.
    assert calls and set(calls) == {GOOD_URL}
    assert stored == GOOD_URL
    assert _public_marker(tmp_path) is None  # an own URL is not the marker
    assert state["probes"] == 1
    # The conversation's spine is ordered (step-4 narration is async and
    # intentionally not pinned here).
    order = [
        joined.index(ob.GREETING),
        joined.index(ob.NODE_ASK),
        joined.index(ob.LOAD_WAIT),
        joined.index(ob.GUIDE),
        joined.index(ob.URL_PROMPT),
        joined.index(ob.CONFIRMED),
    ]
    assert order == sorted(order)
    # The chat line ran through the model — exactly one turn, after the
    # flow closed; every onboarding vocabulary line was consumed by code.
    assert state["model"] == 1
    # Nothing sensitive echoed: neither key is ever printed.
    assert ZPUB not in joined
    assert "bacon" not in joined


def test_key_ask_help_seed_and_private_key_refusals(tmp_path: Path, monkeypatch) -> None:
    """Step 1 side branches: 'help' guidance; seed-shaped input refused with
    guidance and never echoed; a private key refused value-free by the same
    gated parser; the ask loops until a valid key arrives."""
    code, rec, stored, state = _drive(
        monkeypatch,
        tmp_path,
        lines=["help", SEED_LINE, XPRV, ZPUB, "1", "exit"],
        argv=["--stub-llm"],
        interactive=True,
    )
    assert code == 0
    joined = rec.joined
    assert ob.KEY_HELP in joined
    assert ob.KEY_SEED_REFUSAL in joined
    assert "Watch key rejected:" in joined  # descriptor layer, value-free
    assert ob.KEY_RETRY_HINT in joined
    assert ob.NODE_ASK in joined  # key accepted → steps 2+ ran
    assert "bacon" not in joined  # seed words never echoed
    assert XPRV not in joined  # key material never echoed
    # "1" is now an EXPLICIT public CONSENT (TCK-ONB-006), not a shrug:
    # its ack re-names the leak, and the opt-in record is what makes the
    # next launch's backend "resolved".
    assert ob.PUBLIC_CHOSEN_ACK in joined
    assert ob.SKIP_ACK not in joined
    assert stored is None  # the URL rung stays empty — public IS the default
    assert _public_marker(tmp_path) == ob.BACKEND_CHOICE_PUBLIC
    assert state["model"] == 0  # the whole session never reached the model

    # Hardware-wallet-only guidance pin (user direction 2026-09-08): the
    # seed refusal/help must point to a hardware wallet and never suggest
    # a software-wallet seed import.
    assert "HARDWARE-WALLET-ONLY" in joined
    assert "hardware wallet" in joined
    assert "software wallet" not in ob.KEY_HELP
    assert "import" not in ob.KEY_HELP


def test_headless_launch_never_blocks(tmp_path: Path, monkeypatch) -> None:
    """Scripted (non-interactive) launches skip the conversation silently:
    no key → today's exit-2 refusal; key → no ask, no narration."""
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path, lines=["exit"], argv=["--stub-llm"],
        interactive=False, auto_scan=True,
    )
    assert code == 2
    assert "No watch key configured" in rec.joined
    assert ob.GREETING not in rec.joined
    assert ob.NODE_ASK not in rec.joined
    assert stored is None
    assert state["probes"] == 0


def test_headless_with_key_skips_conversation(tmp_path: Path, monkeypatch) -> None:
    """The env/key-driven scripted launch (the entire existing suite) gets
    ZERO onboarding even with a fresh wallet."""
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path, lines=["exit"], interactive=False, auto_scan=True,
    )
    assert code == 0
    assert ob.NODE_ASK not in rec.joined
    assert ob.LOAD_NARRATION not in rec.joined
    assert stored is None
    assert state["model"] == 0


def test_second_run_stored_choice_skipped_and_banner_flips(
    tmp_path: Path, monkeypatch
) -> None:
    """A stored choice: no greeting/ask on later runs; the 3-state banner
    and the live client both derive from the STORED rung (decision 6: one
    source of truth — the banner can never disagree with the backend)."""
    _preset_wallet(tmp_path, base_url=LOCAL_URL)
    seen: dict[str, Any] = {}

    def spy_client(**kw: Any) -> EsploraClient:
        seen.update(kw)
        return _fake_client()

    code, rec, stored, _ = _drive(
        monkeypatch, tmp_path, lines=["exit"],
        interactive=True, client=spy_client,
    )
    assert code == 0
    assert ob.NODE_ASK not in rec.joined
    assert ob.GREETING not in rec.joined
    assert stored == LOCAL_URL
    assert seen["base_url"] == LOCAL_URL  # stored rung reached the client
    assert PRIVACY_INDICATOR_OWN_NODE_LOCAL in rec.joined  # banner flipped


def test_env_preset_skips_onboarding(tmp_path: Path, monkeypatch) -> None:
    """An env-preset backend (the operator override) suppresses the whole
    conversation; the banner reflects the effective (env) selection."""
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path, lines=["exit"], interactive=True,
        auto_scan=True, chain_env=GOOD_URL,
    )
    assert code == 0
    assert ob.NODE_ASK not in rec.joined
    assert ob.LOAD_NARRATION not in rec.joined
    assert ob.WEB_SETUP_HINT not in rec.joined
    assert stored is None  # env wins; nothing written to the stored rung
    assert state["probes"] == 0
    assert "your own node on another machine" in rec.joined


# ------------------------------------------------ the deferred first scan
#            (TCK-ONB-006 — ADR-0022 amendment 1 / ADR-0023 amendment 2)


def test_first_run_makes_zero_chain_calls_before_the_choice(
    tmp_path: Path, monkeypatch
) -> None:
    """THE leak pin (user report 2026-09-09): with no rung resolved, an
    interactive first run holds the startup scan — ordinary chat runs, the
    backend is asked NOTHING, and no consent record is written. The wallet
    stays honestly unloaded."""
    calls: list[int] = []
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path, lines=["what is bitcoin?", "exit"],
        interactive=True, auto_scan=True,
        client=lambda **_kw: _counting_client(calls),
    )
    assert code == 0
    assert state["model"] == 1  # the chat line reached the model normally
    assert calls == []  # zero requests to any backend, start to finish
    assert "Startup scan complete" not in rec.joined
    assert ob.LOAD_WAIT in rec.joined  # the honest "nothing is loading yet"
    assert stored is None
    assert _public_marker(tmp_path) is None


def test_not_now_first_run_is_not_a_consent(
    tmp_path: Path, monkeypatch
) -> None:
    """"Not now" while the scan is held: the ask names what stays off, the
    ask stays OPEN, nothing is consented-to-recorded, nothing is fetched.
    (Skip ≠ public — the distinction TCK-ONB-006 draws.)"""
    calls: list[int] = []
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path, lines=["not now", "exit"],
        interactive=True, auto_scan=True,
        client=lambda **_kw: _counting_client(calls),
    )
    assert code == 0
    assert state["model"] == 0  # the skip answer is deterministic code
    assert ob.ASK_WAITS_ACK in rec.joined
    assert ob.SKIP_ACK not in rec.joined  # the "public for now" wording lies here
    assert calls == []
    assert "Startup scan complete" not in rec.joined
    assert stored is None
    assert _public_marker(tmp_path) is None


def test_public_consent_releases_the_deferred_scan(
    tmp_path: Path, monkeypatch
) -> None:
    """Explicit public pick (with the leak copy): the opt-in record lands,
    the held scan starts on the public client the user just accepted, and
    the load narration completes (steps 3→4 through the deferral)."""
    calls: list[int] = []
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path, lines=["what is bitcoin?", "1", "exit"],
        interactive=True, auto_scan=True,
        client=lambda **_kw: _counting_client(calls),
    )
    assert code == 0
    assert state["model"] == 1  # chat ran while the ask was open
    assert ob.PUBLIC_CHOSEN_ACK in rec.joined
    assert ob.PUBLIC_LOADING_NOW in rec.joined  # consent really started it
    assert "Startup scan complete" in rec.joined
    assert ob.LOAD_COMPLETE in rec.joined  # step 4 rides the released scan
    assert len(calls) > 0  # the scan fired — AFTER the recorded choice
    assert stored is None  # the URL rung stays empty…
    assert _public_marker(tmp_path) == ob.BACKEND_CHOICE_PUBLIC  # …the marker carries the consent


def test_second_run_after_public_consent_scans_immediately(
    tmp_path: Path, monkeypatch
) -> None:
    """The recorded consent makes the NEXT launch resolved: immediate scan,
    no ask, no deferral copy — 'every other run scans unchanged'."""
    _drive(  # run 1: consent in passing.
        monkeypatch, tmp_path, lines=["1", "exit"],
        interactive=True, auto_scan=True,
    )
    code, rec, stored, _ = _drive(  # run 2: same store, never asked again.
        monkeypatch, tmp_path, lines=["exit"],
        interactive=True, auto_scan=True,
    )
    assert code == 0
    assert ob.NODE_ASK not in rec.joined
    assert ob.LOAD_WAIT not in rec.joined
    assert "Startup scan complete" in rec.joined
    assert stored is None


@pytest.mark.parametrize("auto_scan", [True, False])
def test_returning_unresolved_wallet_stays_deferred_and_rearms(
    tmp_path: Path, monkeypatch, auto_scan: bool
) -> None:
    """A wallet that was created but whose ask was NEVER answered (e.g. the
    user quit mid-ask) is still unresolved: the scan keeps waiting and the
    mandatory ask re-arms at startup — the deferral is never a silent
    dead-end, and it never leaks to the default behind the user's back.
    Re-arms REGARDLESS of AUTO_SCAN (security review F1: a scan opt-out
    is not a server consent)."""
    _preset_wallet(tmp_path)  # existing wallet, nothing stored, no marker
    calls: list[int] = []
    code, rec, _stored, _ = _drive(
        monkeypatch, tmp_path, lines=["exit"],
        interactive=True, auto_scan=auto_scan,
        client=lambda **_kw: _counting_client(calls),
    )
    assert code == 0
    assert ob.NODE_ASK in rec.joined  # re-armed: the ask is mandatory pre-scan
    assert ob.LOAD_WAIT in rec.joined
    assert calls == []
    assert "Startup scan complete" not in rec.joined


def test_setup_public_consent_unblocks_deferred_scan_on_fresh_wallet(
    tmp_path: Path, monkeypatch
) -> None:
    """The /setup path resolves the held scan too (single source of truth):
    on a deferred fresh wallet, /setup → explicit public → record + release
    → the load completes in-session."""
    calls: list[int] = []
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path, lines=["/setup", "1", "exit"],
        interactive=True, auto_scan=True,
        client=lambda **_kw: _counting_client(calls),
    )
    assert code == 0
    assert state["model"] == 0  # every line deterministic
    assert "Startup scan complete" in rec.joined
    assert len(calls) > 0
    assert stored is None
    assert _public_marker(tmp_path) == ob.BACKEND_CHOICE_PUBLIC


# --------------------------------------------- the ack's honesty (review F2/F3)


def _consent_ack_lines(tmp_path: Path, *, started: bool) -> list[str]:
    """One explicit public consent through a flow whose release hook
    REPORTS success (``started=True``) or failure (a broken plan stood the
    scan down — F2)."""
    store = Store(str(tmp_path / f"release-{started}.db"))
    try:
        flow = ob.OnboardingFlow(
            store=store,
            check_backend=lambda _u: _u,
            deferred=True,
            public_chosen=lambda: started,
        )
        out: list[str] = []
        assert flow.handle_line("1", out.append)
        return out
    finally:
        store.close()


def test_public_loading_line_requires_an_actual_start(tmp_path: Path) -> None:
    """Security review F2: "Loading your wallet from it now." may only
    print when the release report says the held scan REALLY started — a
    failed plan (mark_skipped, nothing loads this session) must not buy
    the claim. The consent ack itself always prints (the record stands
    either way)."""
    started = _consent_ack_lines(tmp_path, started=True)
    assert ob.PUBLIC_CHOSEN_ACK in started
    assert ob.PUBLIC_LOADING_NOW in started
    stalled = _consent_ack_lines(tmp_path, started=False)
    assert ob.PUBLIC_CHOSEN_ACK in stalled
    assert ob.PUBLIC_LOADING_NOW not in stalled


def test_default_is_no_longer_a_consent_word(tmp_path: Path) -> None:
    """Security review F3: "default" named the free public default the
    amendment deleted and is NOT among the options the ask presents — it
    records nothing, fires no release, and falls through as ordinary chat
    (the held ask stays open, so a later "1"/"2" still resolves it)."""
    store = Store(str(tmp_path / "vocab.db"))
    try:
        releases: list[int] = []
        flow = ob.OnboardingFlow(
            store=store,
            check_backend=lambda _u: _u,
            deferred=True,
            public_chosen=lambda: releases.append(1) or True,
        )
        out: list[str] = []
        assert flow.handle_line("default", out.append) is False
        assert out == []  # the deterministic channel consumed nothing
        assert releases == []  # no release, no consent
        assert store.get_setting(ob.BACKEND_CHOICE_SETTING) is None
        assert not flow.done  # the ask is still open
    finally:
        store.close()


# ------------------------------------------------------- the ask branch


def test_auto_scan_zero_does_not_escape_the_hold(tmp_path: Path, monkeypatch) -> None:
    """Security review F1 (the blocker): AUTO_SCAN=0 used to disarm the
    deferral — an unresolved launch then ran with the gate ``disabled``:
    the mandatory ask never re-armed and the FIRST get_balance lazily
    probed the public default with no ask and no consent record,
    indefinitely. The hold is now armed on EVERY unresolved interactive
    launch regardless of AUTO_SCAN: the balance turn answers from the
    empty cache with ZERO chain calls, "not now" gets the honest wait
    copy (skip ≠ consent — the plain (e) ack would claim a scan-less
    session had already chosen), and nothing is recorded."""
    calls: list[int] = []
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path,
        lines=["what's my balance?", "not now", "exit"],
        interactive=True,  # _drive default: auto_scan=False (AUTO_SCAN=0)
        client=lambda **_kw: _counting_client(calls),
    )
    assert code == 0
    assert state["model"] == 1  # the balance question ran as an ordinary turn
    assert calls == []  # THE leak pin: the lazy in-handler scan stood down
    assert ob.NODE_ASK in rec.joined  # the ask re-arms — scan-less or not
    assert ob.LOAD_WAIT in rec.joined
    assert ob.ASK_WAITS_ACK in rec.joined
    assert ob.SKIP_ACK not in rec.joined  # nothing was consented to
    assert "Startup scan complete" not in rec.joined
    assert stored is None
    assert _public_marker(tmp_path) is None
    assert PRIVACY_INDICATOR in rec.joined  # banner still names the fallback
    assert ob.LOAD_NARRATION not in rec.joined  # nothing "loading" is claimed


def test_node_ask_answer_may_arrive_later(tmp_path: Path, monkeypatch) -> None:
    """Non-blocking rule: an ordinary chat turn runs WHILE the ask is open
    (model sees it), and the answer may come any point afterwards."""
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path,
        lines=["what is bitcoin?", "2", GOOD_URL, "exit"],
        interactive=True,
        backend_check=lambda _u: True,
    )
    assert code == 0
    assert state["model"] == 1  # exactly the chat line; the rest: code
    assert "(stub model, dev mode)" in rec.joined  # the turn ran normally
    assert stored == GOOD_URL


def test_url_accepted_stores_choice(tmp_path: Path, monkeypatch) -> None:
    """Remote URL passing the chain probe → (d); the doctor (loopback-only,
    ADR-0016) is NOT consulted for a remote host. TCK-BACKEND-002 (ADR-0018
    amendment): the saved choice then hot-swaps the live client and
    releases/resyncs IN-SESSION — the next-launch honesty line is gone."""
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path, lines=["2", GOOD_URL, "exit"],
        interactive=True, backend_check=lambda _u: True,
    )
    assert code == 0
    assert ob.CONFIRMED in rec.joined
    assert ob.SWITCHING_NOW in rec.joined  # swapped + loading from it now
    assert ob.EFFECTS_NEXT_LAUNCH not in rec.joined
    assert stored == GOOD_URL
    assert state["probes"] == 1
    assert state["detects"] == 0  # remote: chain probe only, doctor stands down


def test_url_rejected_doctor_pointer_then_explicit_public(
    tmp_path: Path, monkeypatch
) -> None:
    """Failure path (c): nothing saved, the pointer at "node status" +
    retry; "retry" re-probes the SAME candidate; only an explicit public
    pick closes it — never a silent fallback."""
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path, lines=[GOOD_URL, "retry", "1", "exit"],
        interactive=True, backend_check=lambda _u: False,
    )
    assert code == 0
    assert rec.joined.count(ob.VALIDATION_FAIL) == 2
    assert '"node status"' in rec.joined  # the doctor pointer (copy (c))
    assert ob.PUBLIC_CHOSEN_ACK in rec.joined  # explicit consent (ONB-006)
    assert stored is None  # the failed URL was NEVER saved
    assert state["probes"] == 2  # retry re-probed the same candidate


def test_non_url_at_prompt_fails_without_probing(
    tmp_path: Path, monkeypatch
) -> None:
    """Asked for an address, a non-URL line is exactly the "didn't check
    out" case: nothing probed, nothing saved, the prompt stays open."""
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path, lines=["2", "mempool.local:3000", GOOD_URL, "exit"],
        interactive=True, backend_check=lambda _u: True,
    )
    assert code == 0
    assert ob.VALIDATION_FAIL in rec.joined
    assert state["probes"] == 1  # only the real URL was ever probed
    assert stored == GOOD_URL


def test_syncing_loopback_node_refused_with_doctor_facts(
    tmp_path: Path, monkeypatch
) -> None:
    """Decision-5 syncing branch: loopback candidate + doctor CORE_SYNCING
    → refused; the progress figures are quoted VERBATIM from tool output;
    nothing saved."""
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path, lines=["2", LOCAL_URL, "exit"],
        interactive=True, backend_check=lambda _u: True,
        node_report=_report_syncing(),
    )
    assert code == 0
    joined = rec.joined
    assert "A Bitcoin Core node is syncing" in joined  # doctor headline
    assert "50.0% verified" in joined  # Core's own progress fraction
    assert "block 400000 of 800000" in joined  # Core's own heights
    assert ob.VALIDATION_FAIL not in joined  # the syncing line replaces it
    assert stored is None
    assert state["detects"] == 1


def test_synced_loopback_node_accepted(tmp_path: Path, monkeypatch) -> None:
    """The pass case on a loopback URL: chain probe + a synced mainnet Core
    report → confirmation (d), stored."""
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path, lines=["2", LOCAL_URL, "exit"],
        interactive=True, backend_check=lambda _u: True,
        node_report=_report_synced(),
    )
    assert code == 0
    assert ob.CONFIRMED in rec.joined
    assert stored == LOCAL_URL
    assert state["detects"] == 1


def test_nonmainnet_loopback_core_refused(tmp_path: Path, monkeypatch) -> None:
    """ADR-0021: a loopback Core reporting chain='regtest' (a network that
    shares the mainnet genesis, invisible to the chain probe alone) is
    refused by the node fact; nothing saved."""
    code, rec, stored, _ = _drive(
        monkeypatch, tmp_path, lines=["2", LOCAL_URL, "exit"],
        interactive=True, backend_check=lambda _u: True,
        node_report=_report_regtest(),
    )
    assert code == 0
    assert ob.VALIDATION_FAIL in rec.joined
    assert stored is None


def test_detection_disabled_skips_ibd_gate(tmp_path: Path, monkeypatch) -> None:
    """LOCALWALLET_NODE_DETECTION_ENABLED=0 escape hatch: the loopback IBD
    gate stands down (no probing at all) — the chain shape+mainnet proof
    alone accepts."""
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path, lines=["2", LOCAL_URL, "exit"],
        interactive=True, backend_check=lambda _u: True,
        detection_enabled=False,
    )
    assert code == 0
    assert ob.CONFIRMED in rec.joined
    assert stored == LOCAL_URL
    assert state["detects"] == 0


# ---------------------------------------------------- scan-failure honesty


def test_startup_scan_failure_never_narrates_load_complete(
    tmp_path: Path, monkeypatch
) -> None:
    """Honest step 4: the load-complete line rides ONLY a successful first
    scan persist — including a scan released by a public CONSENT (the
    deferred first-run path) — never on the failure's scrubbed warning."""

    def failing_client(**_kw: Any) -> EsploraClient:
        return EsploraClient(
            base_url="https://mempool.space/api",
            timeout_s=5.0,
            max_retries=0,
            transport=httpx.MockTransport(lambda _r: httpx.Response(500)),
        )

    code, rec, _stored, _ = _drive(
        monkeypatch, tmp_path, lines=["1", "exit"],
        interactive=True, auto_scan=True, client=failing_client,
    )
    assert code == 0
    assert ob.LOAD_COMPLETE not in rec.joined
    assert "startup scan failed" in rec.joined  # existing honest warning


def test_malformed_live_backend_url_fails_scan_closed_not_pump(
    tmp_path: Path, monkeypatch
) -> None:
    """Review finding 1, live path: a URL that passes ChainConfig's shape
    check but breaks httpx at REQUEST time (non-numeric port) must fail the
    startup scan CLOSED (gate skipped + value-free warning via the
    ChainError surface), never re-raise on the engine pump and die
    mid-session."""

    def broken_client(**_kw: Any) -> EsploraClient:
        return EsploraClient(base_url="http://h:port/api", timeout_s=0.5, max_retries=0)

    code, rec, _stored, _ = _drive(
        monkeypatch, tmp_path, lines=["exit"],
        interactive=True, auto_scan=True,
        chain_env="http://h:port/api", client=broken_client,
    )
    assert code == 0  # the pump survived; the REPL ran the exit line
    joined = rec.joined
    assert "startup scan failed" in joined       # fail-closed warning line
    assert "invalid base URL" in joined          # the value-free ChainError
    assert "h:port" not in joined                # the URL is never echoed


# --------------------------------------------------------------- transports


def test_web_launch_gets_hint_only(tmp_path: Path, monkeypatch) -> None:
    """Requirement 5: the browser never gets the conversation — a web
    launch with no stored choice shows the one-line CLI hint instead; with
    a stored choice, not even that."""
    web_db = tmp_path / "web.db"
    for var in (
        "LOCALWALLET_MODEL_PATH",
        "LOCALWALLET_LLM_BASE_URL",
        "LOCALWALLET_LLM_MODEL",
        "LOCALWALLET_CHAIN_BASE_URL",
        app_module.ZPUB_ENV_VAR,
        app_module.UI_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(web_db))
    monkeypatch.setenv(AUTO_SCAN_ENV_VAR, "0")
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")
    monkeypatch.setattr(app_module, "EsploraClient", lambda **_kw: _fake_client())

    outputs: list[str] = []
    capture: dict[str, Any] = {}
    gate = threading.Event()
    thread = threading.Thread(
        target=lambda: capture.update(
            code=run(
                ["--stub-llm", "--zpub", ZPUB, "--web"],
                output_fn=outputs.append,
                on_web_server=lambda server: (capture.update(server=server), gate.set()),
            )
        ),
        daemon=True,
    )
    thread.start()
    assert gate.wait(30), "web server never started"
    assert _web_stream_contains(capture["server"], ob.WEB_SETUP_HINT)  # via /events
    capture["server"].stop()
    thread.join(15)
    assert capture.get("code") == 0
    joined = "\n".join(outputs)
    assert ob.WEB_SETUP_HINT not in joined  # narration → emitter, not terminal
    assert ob.NODE_ASK not in joined  # never the conversation, never the key ask
    assert ob.GREETING not in joined

    # Stored choice (same store) → hint gone.
    store = Store(str(web_db))
    try:
        store.set_chain_base_url(GOOD_URL)
    finally:
        store.close()
    outputs2: list[str] = []
    capture2: dict[str, Any] = {}
    gate2 = threading.Event()
    thread2 = threading.Thread(
        target=lambda: capture2.update(
            code=run(
                ["--stub-llm", "--zpub", ZPUB, "--web"],
                output_fn=outputs2.append,
                on_web_server=lambda server: (
                    capture2.update(server=server),
                    gate2.set(),
                ),
            )
        ),
        daemon=True,
    )
    thread2.start()
    assert gate2.wait(30)
    assert not _web_stream_contains(capture2["server"], ob.WEB_SETUP_HINT)
    capture2["server"].stop()
    thread2.join(15)
    assert ob.WEB_SETUP_HINT not in "\n".join(outputs2)


def test_web_first_run_defers_the_scan(tmp_path: Path, monkeypatch) -> None:
    """TCK-ONB-006 on the web path: with no rung resolved the startup scan
    is HELD — zero chain requests, no completion narration — and the hint
    says the honest thing: balances wait until a backend is chosen (the
    browser has no consent surface; the terminal ask or a saved Settings
    address, effective next launch, unblocks the load)."""
    calls: list[int] = []
    for var in (
        "LOCALWALLET_MODEL_PATH",
        "LOCALWALLET_LLM_BASE_URL",
        "LOCALWALLET_LLM_MODEL",
        "LOCALWALLET_CHAIN_BASE_URL",
        app_module.ZPUB_ENV_VAR,
        app_module.UI_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(tmp_path / "web-defer.db"))
    monkeypatch.setenv(AUTO_SCAN_ENV_VAR, "1")
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0")
    monkeypatch.setattr(
        app_module, "EsploraClient", lambda **_kw: _counting_client(calls)
    )

    outputs: list[str] = []
    capture: dict[str, Any] = {}
    gate = threading.Event()
    thread = threading.Thread(
        target=lambda: capture.update(
            code=run(
                ["--stub-llm", "--zpub", ZPUB, "--web"],
                output_fn=outputs.append,
                on_web_server=lambda server: (capture.update(server=server), gate.set()),
            )
        ),
        daemon=True,
    )
    thread.start()
    assert gate.wait(30), "web server never started"
    assert _web_stream_contains(capture["server"], ob.WEB_SETUP_HINT)
    capture["server"].stop()
    thread.join(15)
    assert capture.get("code") == 0
    joined = "\n".join(outputs)
    assert ob.WEB_SETUP_HINT not in joined  # narration → emitter, not terminal
    assert "Startup scan complete" not in joined  # and it tells the truth
    assert calls == []  # the held scan never touched the public default


def test_web_auto_scan_zero_watch_never_probes_the_default(
    tmp_path: Path, monkeypatch
) -> None:
    """Security review F1(b) at the web door: with AUTO_SCAN=0 the gate
    used to read ``disabled`` and the between-turns watch drain then ran
    its scan through the public default — while WEB_SETUP_HINT claimed the
    app had NOT looked up the wallet. The held gate is armed regardless of
    AUTO_SCAN and stands the drain down too: zero requests, so the hint is
    true in exactly that state."""
    calls: list[int] = []
    for var in (
        "LOCALWALLET_MODEL_PATH",
        "LOCALWALLET_LLM_BASE_URL",
        "LOCALWALLET_LLM_MODEL",
        "LOCALWALLET_CHAIN_BASE_URL",
        app_module.ZPUB_ENV_VAR,
        app_module.UI_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOCALWALLET_STORE_PATH", str(tmp_path / "web-watch.db"))
    monkeypatch.setenv(AUTO_SCAN_ENV_VAR, "0")  # the formerly-leaky combo
    monkeypatch.setenv("LOCALWALLET_WATCH_INTERVAL_S", "0.05")  # watch ON
    monkeypatch.setattr(
        app_module, "EsploraClient", lambda **_kw: _counting_client(calls)
    )

    outputs: list[str] = []
    capture: dict[str, Any] = {}
    gate = threading.Event()
    thread = threading.Thread(
        target=lambda: capture.update(
            code=run(
                ["--stub-llm", "--zpub", ZPUB, "--web"],
                output_fn=outputs.append,
                on_web_server=lambda server: (capture.update(server=server), gate.set()),
            )
        ),
        daemon=True,
    )
    thread.start()
    assert gate.wait(30), "web server never started"
    server = capture["server"]
    threading.Event().wait(0.2)  # let the interval mature
    # The drain runs BETWEEN pump commands — wake the pump exactly like the
    # shipped UI's /state poll does (an idle never-woken pump would hide the
    # leak the buggy build actually ran on the first real request).
    _request(server, "GET", "/state", token=server.token)
    threading.Event().wait(0.3)
    assert _web_stream_contains(server, ob.WEB_SETUP_HINT)
    server.stop()
    thread.join(15)
    assert capture.get("code") == 0
    assert ob.WEB_SETUP_HINT not in "\n".join(outputs)  # narration → emitter
    assert calls == []  # the watch drain stood down behind the held gate


# ---------------------------------------------------------- chain probe unit


def _mt(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


#: Canonical Esplora /blocks/<height> entry: a block OBJECT whose "id" is
#: the block hash (the shape real backends serve, and the shape
#: ``EsploraClient.get_tip_block`` parses). The bare-hash string form below
#: is the tolerated lenient variant, NOT canonical (ONB-003 review, f-2).
_MAINNET_GENESIS_BLOCK = {
    "id": MAINNET_GENESIS_HASH,
    "height": 0,
    "timestamp": 1231006505,
}


@pytest.mark.parametrize(
    "genesis_entry",
    [
        _MAINNET_GENESIS_BLOCK,  # canonical Esplora block-object shape
        MAINNET_GENESIS_HASH,    # tolerated lenient bare-hash shape
    ],
    ids=["block-object", "bare-hash"],
)
def test_check_backend_accepts_mainnet_esplora_shape(genesis_entry: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/blocks/tip"):
            return httpx.Response(200, json=870_000)
        if request.url.path.endswith("/blocks/0"):
            return httpx.Response(200, json=[genesis_entry])
        return httpx.Response(404)

    assert check_backend("https://n.example/api", transport=_mt(handler))


@pytest.mark.parametrize(
    "shape", ["testnet", "testnet-object", "garbage", "down", "dead-port", "typo-url"]
)
def test_check_backend_refuses_every_other_shape(shape: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if shape == "down":
            raise httpx.ConnectError("refused")
        if request.url.path.endswith("/blocks/tip"):
            if shape == "garbage":
                return httpx.Response(200, json={"unexpected": "shape"})
            return httpx.Response(200, json=100)
        if shape == "testnet-object":
            # The real-world shape (block objects) carrying the WRONG genesis.
            return httpx.Response(200, json=[{"id": TESTNET_GENESIS, "height": 0}])
        return httpx.Response(200, json=[TESTNET_GENESIS])

    if shape == "dead-port":
        # Nothing listening: a genuine transport failure (port 1, no bind).
        assert not check_backend("http://127.0.0.1:1/api", timeout_s=0.5)
        return
    if shape == "typo-url":
        # Review finding 1: a non-numeric port passes ChainConfig's shape
        # check but raises httpx.InvalidURL at REQUEST time — it must
        # collapse to False, never escape (an escaping exception killed the
        # engine pump mid-session). No transport: the URL parse fails before
        # any socket opens.
        assert not check_backend("http://h:port/api", timeout_s=0.5)
        return
    assert not check_backend("https://n.example/api", transport=_mt(handler))


def test_check_backend_refuses_malformed_url_without_network() -> None:
    # No transport ever: the fail-closed shape check precedes any I/O.
    assert not check_backend("ftp://n.example")
    assert not check_backend("https://user:pass@n.example")  # userinfo

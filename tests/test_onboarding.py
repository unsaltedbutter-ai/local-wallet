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

TESTNET_GENESIS = "000000000933ea01ad0ee984209779baaec3ced90fa3f408719526f8d77f4943"
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

    def check(url: str) -> bool:
        state["probes"] += 1
        return backend_check(url) if backend_check is not None else False

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


# ------------------------------------------------- fresh-user full flow


def test_fresh_user_full_flow_five_steps(tmp_path: Path, monkeypatch) -> None:
    """Steps 1→5 end to end: greeting+key ask → node ask + load narration →
    guide (ask stays open) → 2 → URL → (d); the choice lands in the store;
    only the ONE chat turn after the flow closes touches the model."""
    code, rec, stored, state = _drive(
        monkeypatch,
        tmp_path,
        lines=[ZPUB, "what's a node?", "2", GOOD_URL, "tell me a fact", "exit"],
        argv=["--stub-llm"],  # NO --zpub: the conversation asks for the key
        interactive=True,
        auto_scan=True,
        backend_check=lambda _url: True,
    )
    assert code == 0
    joined = rec.joined
    assert joined.count(ob.GREETING) == 1
    assert ob.NODE_ASK in joined
    assert ob.LOAD_NARRATION in joined  # SCAN-003 main variant
    assert ob.GUIDE in joined  # (f) — then the ask stayed open
    assert ob.URL_PROMPT in joined  # (b)
    assert ob.CONFIRMED in joined  # (d)
    assert ob.EFFECTS_NEXT_LAUNCH in joined
    assert ob.LOAD_COMPLETE in joined  # step 4, on the successful first scan
    assert stored == GOOD_URL
    assert state["probes"] == 1
    # The conversation's spine is ordered (step-4 narration is async and
    # intentionally not pinned here).
    order = [
        joined.index(ob.GREETING),
        joined.index(ob.NODE_ASK),
        joined.index(ob.LOAD_NARRATION),
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
    assert ob.SKIP_ACK in joined  # "1" = explicit public pick
    assert stored is None  # public is a non-choice: nothing stored
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


# ------------------------------------------------------- the ask branch


def test_node_ask_skipped_keeps_public(tmp_path: Path, monkeypatch) -> None:
    """"not now" → (e): public default retained (nothing stored), banner
    stays the honest public wording, the ask is over for the session."""
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path, lines=["not now", "exit"], interactive=True,
    )
    assert code == 0
    assert ob.SKIP_ACK in rec.joined
    assert stored is None
    assert PRIVACY_INDICATOR in rec.joined  # public banner, unchanged
    assert state["probes"] == 0
    # AUTO_SCAN=0: the load narration promises nothing when no load runs.
    assert ob.LOAD_NARRATION not in rec.joined
    assert ob.LOAD_COMPLETE not in rec.joined


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
    ADR-0016) is NOT consulted for a remote host."""
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path, lines=["2", GOOD_URL, "exit"],
        interactive=True, backend_check=lambda _u: True,
    )
    assert code == 0
    assert ob.CONFIRMED in rec.joined
    assert ob.EFFECTS_NEXT_LAUNCH in rec.joined
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
    assert ob.SKIP_ACK in rec.joined
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
    scan persist; the failure path keeps its scrubbed warning."""

    def failing_client(**_kw: Any) -> EsploraClient:
        return EsploraClient(
            base_url="https://mempool.space/api",
            timeout_s=5.0,
            max_retries=0,
            transport=httpx.MockTransport(lambda _r: httpx.Response(500)),
        )

    code, rec, _stored, _ = _drive(
        monkeypatch, tmp_path, lines=["skip", "exit"],
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
    capture["server"].stop()
    thread.join(15)
    assert capture.get("code") == 0
    joined = "\n".join(outputs)
    assert ob.WEB_SETUP_HINT in joined  # the one-line pointer to the CLI
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
    capture2["server"].stop()
    thread2.join(15)
    assert ob.WEB_SETUP_HINT not in "\n".join(outputs2)


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

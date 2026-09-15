"""TCK-WEB-023 (engine half): private-IP GREEN + the typed host subline.

Three surfaces pinned here:

1. The classification (:func:`localwallet.app._backend_mode`) resolves the
   CONFIGURED chain host OFFLINE (URL text + literal IPs only — no DNS, no
   sockets) and adds the fifth closed ``privacy_mode`` name
   ``own_node_private`` for private-range literal IPs (10/8, 172.16/12,
   192.168/16, the wider 127/8). CGNAT 100.64/10 and link-local 169.254/16
   are NEVER green; hostnames — including ``.local``/mDNS — land on the
   honest yellow ``own_node_remote`` branch (documented decision: the
   engine does not resolve, so a name's range is unknowable text).
2. The ``/state`` additive ``backend_host`` (council fold): the bare
   hostname of the configured URL (scheme/port/path/USERINFO stripped by
   the existing UX-009 parser) rides ONLY the two host-named modes
   (own_node_remote / own_node_private); public, own_node_local, the
   awaiting_backend hold and an unparseable URL all OMIT it. Credentials
   can never ride the wire (pinned below, verbatim substrings).
3. Kind-badge shape (user MW-17 amendment): NO new field — the electrum/
   bitcoind badge tint REUSES the single closed classification the trust
   chip already rides: private (green) iff ``privacy_mode`` is
   ``own_node_local`` or ``own_node_private``; yellow otherwise; no badge
   when ``backend_kind`` is ``none`` (the client already maps the names —
   a parallel boolean would be a second truth that can drift).

The banner/narration copy deliberately does NOT fork for own_node_private:
it renders the SAME host-named trust-hedge sentence as own_node_remote
(the binding glm council hedge — only the badge COLOR claims private).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from localwallet import app
from localwallet.config import PUBLIC_ELECTRUM_URL, Settings
from localwallet.store import Store

# ------------------------------------------------------- classification matrix


PRIVATE_URLS = [
    "ssl://10.0.0.1:50002",
    "ssl://10.255.255.255:50002",
    "http://172.16.0.1:3006",
    "http://172.20.5.9:8332",
    "http://172.31.255.255:8332",
    "bitcoind://192.168.1.50:8332/w",
    "http://192.168.0.25:65154",
    "http://127.0.0.2:3006",  # wider loopback block: private, not "local"
    "http://[::ffff:10.1.2.3]:8332",  # IPv4-mapped literal: range in the text
]

NOT_GREEN_LITERAL_IPS = [
    "ssl://100.64.0.1:50002",  # CGNAT — never green
    "ssl://100.127.255.255:50002",
    "ssl://169.254.7.7:50002",  # link-local — never green
    "ssl://8.8.8.8:50002",  # plain public
    "http://172.32.0.1:3006",  # just outside 172.16/12
    "http://192.169.1.1:3006",  # just outside 192.168/16
    "http://11.0.0.1:3006",  # just outside 10/8
    "http://[fd00::5]:443",  # IPv6 ULA is NOT in the enumerated ranges
    "http://[2001:db8::1]:443",
]

NOT_GREEN_NAMES = [
    "http://node.example.invalid:3006",
    "ssl://electrum.example.lan:50002",
    "http://MyNode.LOCAL:50001",  # mDNS: documented NOT-green (no resolution)
    "http://vpn-overlay.local:50002",
    "http://barehost.example",
]


@pytest.mark.parametrize("url", PRIVATE_URLS)
def test_private_range_literals_classify_own_node_private(url: str) -> None:
    assert app._backend_mode(Settings(chain_base_url=url)) == (
        app.BACKEND_MODE_OWN_NODE_PRIVATE
    )


@pytest.mark.parametrize("url", NOT_GREEN_LITERAL_IPS + NOT_GREEN_NAMES)
def test_public_and_unclassifiable_hosts_never_go_green(url: str) -> None:
    assert app._backend_mode(Settings(chain_base_url=url)) == (
        app.BACKEND_MODE_OWN_NODE_REMOTE
    )


def test_existing_mode_meanings_are_preserved() -> None:
    """The smallest-honest-change rule: every pre-TCK-WEB-023 answer keeps
    its exact name — empty UNRESOLVED, the consented public Electrum host
    PUBLIC (case-insensitively, ahead of every other branch), the legacy
    loopback strings own_node_local, an unparseable URL plain remote."""
    assert app._backend_mode(Settings()) == app.PRIVACY_MODE_AWAITING_BACKEND
    assert app._backend_mode(Settings(chain_base_url="   ")) == (
        app.PRIVACY_MODE_AWAITING_BACKEND
    )
    assert app._backend_mode(Settings(chain_base_url=PUBLIC_ELECTRUM_URL)) == (
        app.BACKEND_MODE_PUBLIC
    )
    assert app._backend_mode(
        Settings(chain_base_url="SSL://ELECTRUM.BLOCKSTREAM.INFO:50002")
    ) == app.BACKEND_MODE_PUBLIC
    for loopback in (
        "ssl://127.0.0.1:50002",
        "http://localhost:3006",
        "http://LOCALHOST:3006",
        "http://[::1]:3006",
    ):
        assert app._backend_mode(Settings(chain_base_url=loopback)) == (
            app.BACKEND_MODE_OWN_NODE_LOCAL
        )
    assert app._backend_mode(Settings(chain_base_url=":://::")) == (
        app.BACKEND_MODE_OWN_NODE_REMOTE
    )


def test_privacy_modes_enum_is_the_closed_five() -> None:
    """The wire contract the web builder maps onto: EXACTLY these five
    names; the two GREEN names are {own_node_local, own_node_private}."""
    assert app.PRIVACY_MODES == frozenset(
        {
            "public",
            "own_node_local",
            "own_node_private",
            "own_node_remote",
            "awaiting_backend",
        }
    )
    assert app._BACKEND_HOST_MODES == frozenset(
        {"own_node_private", "own_node_remote"}
    )


# ------------------------------------------------------------ pure IP helper


def test_host_is_private_range_is_literal_only_and_never_raises() -> None:
    f = app._host_is_private_range
    assert f("10.1.2.3") and f("192.168.1.1") and f("172.16.5.5")
    assert f("127.0.0.2") and f("127.255.255.255")
    assert f("::ffff:172.20.0.1")
    assert not f("100.64.0.1") and not f("169.254.1.1")  # never green
    assert not f("8.8.8.8") and not f("::1")  # ::1 is LOCAL, not private-range
    assert not f("fd00::1")
    # names (whatever they end in) and junk never classify:
    assert not f("node.local") and not f("mynode") and not f("")
    assert not f("10.1.2.03")  # leading-zero octet: refused by ipaddress
    assert not f("999.1.1.1")
    assert not f("10.1.2.3:50002")  # ports belong to the host parser


# ------------------------------------------- /state backend_host (pump-level)


def _state_snap(settings: Settings | None) -> dict[str, object]:
    """One typed /state read through a REAL engine pump with these settings
    (the pump call site is the production source of both new fields)."""
    events: list[Any] = []

    def bootstrap() -> app.EngineContext:
        return app.EngineContext(
            loop=_make_loop(),
            flow=app.TxFlow(),
            session=app.SendSession(),
            table={app.IntentName.RESPOND: app._respond_handler},
            settings=settings,
        )

    handle = app.start_engine(bootstrap, events.append)
    try:
        snap = handle.request_state(5.0)
    finally:
        handle.shutdown()
        assert handle.thread is not None
        handle.thread.join(10)
    assert snap is not None
    return snap


def _make_loop() -> app.AgentLoop:
    return app.AgentLoop(app.stub_generate, {app.IntentName.RESPOND: app._respond_handler})


def test_backend_host_rides_only_the_host_named_modes() -> None:
    assert _state_snap(Settings(chain_base_url="ssl://10.1.2.3:50002"))[
        "backend_host"
    ] == "10.1.2.3"
    assert _state_snap(Settings(chain_base_url="http://node.example.invalid:3006"))[
        "backend_host"
    ] == "node.example.invalid"
    for url in (
        "",  # awaiting
        "ssl://127.0.0.1:50002",  # own_node_local: "this machine" — no host
        "ssl://electrum.blockstream.info:50002",  # public — no host named
        ":://::",  # remote mode but NO parseable host: omitted, never guessed
    ):
        snap = _state_snap(Settings(chain_base_url=url))
        assert "backend_host" not in snap, url
    # No settings context at all: neither field, never fabricated.
    bare = _state_snap(None)
    assert "backend_host" not in bare and "privacy_mode" not in bare


def test_backend_host_strips_scheme_port_path_and_never_credentials() -> None:
    """The pinned no-creds-on-wire rule: ``user:pass@host`` → the bare host
    only. Scheme, port, path, the username AND the password never appear
    anywhere in the snapshot, for either host-named mode."""
    for url, mode, host in (
        ("ssl://rpcuser:hunter2@10.0.0.7:8332/w", "own_node_private", "10.0.0.7"),
        ("bitcoind://u2:p2@node.example.invalid:8332/w", "own_node_remote",
         "node.example.invalid"),
    ):
        snap = _state_snap(Settings(chain_base_url=url))
        assert snap["privacy_mode"] == mode
        assert snap["backend_host"] == host
        text = repr(snap)
        for secret in ("rpcuser", "hunter2", "u2", "p2", "://", "8332", "/w"):
            assert secret not in text, (url, secret)


def test_awaiting_backend_hold_outranks_the_host_subline(tmp_path: Path) -> None:
    """The ONB-006 hold overrides the RESOLVED mode — and the host that the
    outranked mode would have named rides NOTHING into the snapshot (the
    awaiting subline is unchanged copy with no server to name)."""
    from tests.test_e2e_skeleton import ZPUB

    store = Store(str(tmp_path / "hold.db"))
    wallet = store.create_wallet(
        "default", app.WalletDescriptor.from_key(ZPUB).descriptor
    )
    worker = app.ChainWorker(None)
    scan = app.ScanFlow(store, wallet, worker, gap_limit=None)
    scan.set_startup_deferred()
    events: list[Any] = []

    def bootstrap() -> app.EngineContext:
        return app.EngineContext(
            loop=_make_loop(),
            flow=app.TxFlow(),
            session=app.SendSession(),
            table={app.IntentName.RESPOND: app._respond_handler},
            scan=scan,
            store=store,
            settings=Settings(chain_base_url="ssl://10.1.2.3:50002"),
        )

    handle = app.start_engine(bootstrap, events.append)
    try:
        snap = handle.request_state(5.0)
    finally:
        handle.shutdown()
        assert handle.thread is not None
        handle.thread.join(10)
    worker.stop()
    store.close()
    assert snap is not None
    assert snap["privacy_mode"] == "awaiting_backend"
    assert "backend_host" not in snap


# ------------------------------------------- badge tint data shape (no new field)


def test_kind_badge_tint_reuses_the_single_classification() -> None:
    """The builder needs EXACTLY (backend_kind, privacy_mode): the kind
    badge is GREEN iff the mode is one of the two own-node GREEN names,
    yellow otherwise, no badge at kind ``none``. Pinned at the builder
    (both fields ride one snapshot verbatim; the builder adds no framing)
    — the live pair-derivation from a hot-swapped backend is pinned in
    test_backend_hotswap."""
    snap = app.build_state_snapshot(
        app.TxFlow(),
        app.SendSession(),
        None,
        backend_kind="electrum",
        privacy_mode="own_node_private",
        backend_host="192.168.1.50",
    )
    assert snap["backend_kind"] == "electrum"
    assert snap["privacy_mode"] == "own_node_private"
    assert snap["backend_host"] == "192.168.1.50"
    # Absent params OMIT both additive fields (never guessed):
    bare = app.build_state_snapshot(app.TxFlow(), app.SendSession(), None)
    assert "backend_kind" not in bare and "backend_host" not in bare


# ------------------------------------------------ banner/narration hedge copy


def test_private_mode_keeps_the_remote_hedged_wording_everywhere() -> None:
    """The glm council hedge: own_node_private renders the SAME host-named
    trust-hedged sentence as own_node_remote on BOTH engine copy surfaces
    (startup banner + node_status narration) — no sentence ever claims a
    LAN server private. Credentials never appear in either."""
    lan = Settings(
        chain_base_url="ssl://rpcuser:hunter2@192.168.1.50:50002",
        node_detection_enabled=False,  # narration branch only, no probing
    )
    assert app._backend_mode(lan) == app.BACKEND_MODE_OWN_NODE_PRIVATE
    banner = app.privacy_indicator(lan)
    assert banner == app.PRIVACY_INDICATOR_OWN_NODE_REMOTE.format("192.168.1.50")
    assert "only private if you trust this machine" in banner
    assert "hunter2" not in banner and "rpcuser" not in banner

    lines: list[str] = []
    handler = app._make_node_status_handler(lan)
    facts = handler(SimpleNamespace(params=app.NodeStatusParams()))
    assert facts["backend_mode"] == "own_node_private"
    assert facts["backend_host"] == "192.168.1.50"
    app._print_node_status(facts, lines.append)
    joined = "\n".join(lines)
    assert "You are querying 192.168.1.50 for transaction information" in joined
    assert "only private if you trust this machine" in joined
    assert "public Electrum" not in joined  # never the wrong-mode line
    assert "hunter2" not in joined and "rpcuser" not in joined


def test_private_mode_without_parseable_host_falls_back_to_generic() -> None:
    """A configured but UNPARSEABLE URL stays own_node_remote (never
    private — nothing classifiable in the text) and keeps the pre-existing
    generic fallback wording on both surfaces."""
    broken = Settings(chain_base_url=":://::", node_detection_enabled=False)
    assert app.privacy_indicator(broken) == (
        app.PRIVACY_INDICATOR_OWN_NODE_REMOTE_GENERIC
    )
    lines: list[str] = []
    handler = app._make_node_status_handler(broken)
    facts = handler(SimpleNamespace(params=app.NodeStatusParams()))
    assert "backend_host" not in facts
    app._print_node_status(facts, lines.append)
    assert any(
        app._NODE_STATUS_OWN_NODE_REMOTE_GENERIC in line for line in lines
    )

"""TCK-WEB-030 (engine half): hostname→IP trust resolution.

USER DIRECTION 2026-09-15 supersedes the WEB-023 "``.local`` stays yellow
(no resolution)" decision: the configured chain backend's hostname (normal
DNS OR ``.local``/mDNS — both ride the OS resolver) is resolved through the
chain-owned seam (:mod:`localwallet.chain.hostinfo`) and, when every
answer is a private-range IP, classifies as the SAME closed enum member
``own_node_private`` (its meaning widened to literal-or-resolved; no new
member). The classification matrix pinned here:

* literal private IP            → own_node_private  (never touches the
  resolver — the WEB-023 fail-safe path, unchanged)
* literal public IP             → own_node_remote   (also resolver-free)
* hostname → all private IPs    → own_node_private  (GREEN; the new answer)
* hostname → any public answer  → own_node_remote   (mixed fails closed)
* resolution FAILED             → previous answer   (YELLOW hedge; the
  fail-closed fallback — and the failure is cached, so one dead name cannot
  stall every /state read)
* the public pin / loopback / awaiting answers are unchanged and short-circuit
  BEFORE any resolution.

Stateful pins: the cache is keyed by host, bounded by a TTL and an entry
cap, and INVALIDATED at the TCK-BACKEND-002 hot-swap seam (``_install``) —
a URL swap never serves the previous host's resolution for the remainder of
a TTL window.

Privacy pins: the resolved IP never leaves the engine — typed /state still
carries only the closed enum NAME (plus the pre-existing documented
bare-configured-host ``backend_host`` exception); no new field appears.

All hermetic: the resolver is stubbed at the seam (app-level tests) or at
``socket.getaddrinfo`` (seam tests); no test issues a real lookup.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from localwallet import app
from localwallet.chain import hostinfo
from localwallet.config import PUBLIC_ELECTRUM_URL, Settings
from localwallet.protocol import NodeStatusParams
from tests.test_backend_hotswap import _drain, _mk_wiring, _probe_true

# ------------------------------------------------------------- fixtures

@pytest.fixture(autouse=True)
def _clean_host_cache() -> Any:
    """The host-trust cache is a module global — isolate every test."""
    app._invalidate_host_trust()
    yield
    app._invalidate_host_trust()


class _FakeSeam:
    """Stub for :func:`hostinfo.resolves_to_private` with a call log —
    answers per host, defaulting to False (public)."""

    def __init__(self, answers: dict[str, bool | None]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def __call__(self, host: str) -> bool | None:
        self.calls.append(host)
        return self.answers.get(host, False)


@pytest.fixture
def seam(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patched seam factory: ``seam({host: answer})`` installs the stub and
    returns it (with ``.calls``)."""

    def install(answers: dict[str, bool | None]) -> _FakeSeam:
        fake = _FakeSeam(answers)
        monkeypatch.setattr(hostinfo, "resolves_to_private", fake)
        return fake

    return install


def _fake_getaddrinfo(
    monkeypatch: pytest.MonkeyPatch,
    answers: dict[str, list[str] | Exception],
) -> list[str]:
    """Patch ``socket.getaddrinfo`` (what the seam itself calls); returns
    the list of queried hostnames."""
    calls: list[str] = []

    def fake(host: str, port: Any, **kwargs: Any) -> list[Any]:
        calls.append(host)
        answer = answers[host]
        if isinstance(answer, Exception):
            raise answer
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0))
            for addr in answer
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return calls


# ------------------------------------------- seam (getaddrinfo-level) matrix


def test_seam_all_private_answers_true(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_getaddrinfo(
        monkeypatch, {"mynode.local": ["10.1.2.3", "192.168.0.9"]}
    )
    assert hostinfo.resolves_to_private("mynode.local") is True
    assert calls == ["mynode.local"]


def test_seam_public_and_mixed_are_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_getaddrinfo(
        monkeypatch,
        {
            "vps.example": ["8.8.8.8"],
            "dual.example": ["10.1.2.3", "8.8.8.8"],  # ONE public route: fail closed
            "cgnat.example": ["100.64.0.1"],  # never green, resolved or not
            "linklocal.example": ["169.254.7.7"],
            "v6.example": ["::1"],  # IPv6 has no private branch
        },
    )
    assert hostinfo.resolves_to_private("vps.example") is False
    assert hostinfo.resolves_to_private("dual.example") is False
    assert hostinfo.resolves_to_private("cgnat.example") is False
    assert hostinfo.resolves_to_private("linklocal.example") is False
    assert hostinfo.resolves_to_private("v6.example") is False


def test_seam_failures_answer_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_getaddrinfo(
        monkeypatch,
        {
            "dead.example": socket.gaierror("no such host"),
            "slow.example": TimeoutError("resolver timeout"),
            "empty.example": [],
        },
    )
    assert hostinfo.resolves_to_private("dead.example") is None
    assert hostinfo.resolves_to_private("slow.example") is None
    assert hostinfo.resolves_to_private("empty.example") is None


def test_seam_literals_answer_from_text_without_the_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WEB-023's fail-safe stays fail-safe at the seam: a literal IP is
    range-checked from the TEXT; ``getaddrinfo`` is never invoked for one."""
    calls = _fake_getaddrinfo(monkeypatch, {})
    assert hostinfo.resolves_to_private("10.0.0.1") is True
    assert hostinfo.resolves_to_private("8.8.8.8") is False
    assert hostinfo.resolves_to_private("::ffff:172.20.0.1") is True
    assert calls == []


# ------------------------------------------- app classification (engine) matrix


def test_hostname_resolving_private_is_own_node_private(seam: Any) -> None:
    fake = seam({"evil-star.local": True})  # the ticket's own example name
    settings = Settings(chain_base_url="ssl://evil-star.local:50001")
    assert app._backend_mode(settings) == app.BACKEND_MODE_OWN_NODE_PRIVATE
    assert fake.calls == ["evil-star.local"]


def test_hostname_resolution_is_case_insensitive_and_keyed_lowercase(seam: Any) -> None:
    fake = seam({"mynode.local": True})
    settings = Settings(chain_base_url="ssl://MyNode.Local:50001")
    assert app._backend_mode(settings) == app.BACKEND_MODE_OWN_NODE_PRIVATE
    assert fake.calls == ["mynode.local"]  # DNS hosts are case-insensitive


def test_hostname_resolving_public_stays_own_node_remote(seam: Any) -> None:
    seam({"vps.example": False})
    settings = Settings(chain_base_url="ssl://vps.example:50002")
    assert app._backend_mode(settings) == app.BACKEND_MODE_OWN_NODE_REMOTE


def test_unresolvable_falls_back_closed_to_the_previous_answer(seam: Any) -> None:
    """Resolution FAILURE (None) keeps today's YELLOW hedge — the fail-safe
    path, value-free, never raises."""
    seam({"gone.example": None})
    settings = Settings(chain_base_url="ssl://gone.example:50002")
    assert app._backend_mode(settings) == app.BACKEND_MODE_OWN_NODE_REMOTE


def test_literals_and_pins_never_reach_the_resolver(seam: Any) -> None:
    """The short-circuit order pin: awaiting/public/loopback/literal-private/
    literal-public all answer with ZERO resolver calls — only the public-pin
    host string is even a name here, and its pin branch wins before DNS."""
    fake = seam({})
    cases = {
        "": app.PRIVACY_MODE_AWAITING_BACKEND,
        PUBLIC_ELECTRUM_URL: app.BACKEND_MODE_PUBLIC,
        "ssl://Electrum.Blockstream.Info:50002": app.BACKEND_MODE_PUBLIC,
        "http://localhost:3006": app.BACKEND_MODE_OWN_NODE_LOCAL,
        "ssl://192.168.1.50:8332": app.BACKEND_MODE_OWN_NODE_PRIVATE,
        "ssl://8.8.8.8:50002": app.BACKEND_MODE_OWN_NODE_REMOTE,
        ":://::": app.BACKEND_MODE_OWN_NODE_REMOTE,  # no parseable host
    }
    for url, expected in cases.items():
        assert app._backend_mode(Settings(chain_base_url=url)) == expected, url
    assert fake.calls == []


# ------------------------------------------------------------- cache discipline


def test_resolution_cached_once_per_host(seam: Any) -> None:
    fake = seam({"cached.local": True})
    settings = Settings(chain_base_url="ssl://cached.local:50002")
    for _ in range(3):
        assert app._backend_mode(settings) == app.BACKEND_MODE_OWN_NODE_PRIVATE
    assert fake.calls == ["cached.local"]  # ONE resolution, not three


def test_cache_is_keyed_by_host_not_insertion_order(seam: Any) -> None:
    fake = seam({"green.local": True, "amber.local": False})
    green = app._backend_mode(Settings(chain_base_url="ssl://green.local:50002"))
    amber = app._backend_mode(Settings(chain_base_url="ssl://amber.local:50002"))
    assert green == app.BACKEND_MODE_OWN_NODE_PRIVATE
    assert amber == app.BACKEND_MODE_OWN_NODE_REMOTE
    # Neither answer bled into the other, and each host resolved once:
    assert fake.calls == ["green.local", "amber.local"]


def test_cache_expires_with_the_ttl(seam: Any) -> None:
    fake = seam({"aging.local": True})
    settings = Settings(chain_base_url="ssl://aging.local:50002")
    assert app._backend_mode(settings) == app.BACKEND_MODE_OWN_NODE_PRIVATE
    _expiry, answer = app._host_trust_cache["aging.local"]
    # Age the entry past its window (no sleeping — the TTL maths is the pin):
    app._host_trust_cache["aging.local"] = (time.monotonic() - 1.0, answer)
    assert app._backend_mode(settings) == app.BACKEND_MODE_OWN_NODE_PRIVATE
    assert len(fake.calls) == 2  # the stale entry expired → re-resolved
    fresh_expiry, fresh_answer = app._host_trust_cache["aging.local"]
    assert fresh_answer is answer
    assert fresh_expiry > time.monotonic()  # a NEW bounded window, not forever


def test_cache_is_bounded(seam: Any) -> None:
    seam({})
    for i in range(app._HOST_TRUST_CACHE_MAX * 2):
        app._backend_mode(Settings(chain_base_url=f"ssl://h{i}.example:50002"))
    assert len(app._host_trust_cache) <= app._HOST_TRUST_CACHE_MAX


def test_hot_swap_invalidates_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TCK-BACKEND-002 ``_install`` seam is the invalidation point: after
    a real settings-apply swap, the NEXT classification re-resolves — the
    previous host's cached answer can never ride the remainder of a TTL
    window (and an entry cached for the OLD host is simply gone)."""
    monkeypatch.delenv(app.CHAIN_BASE_URL_ENV_VAR, raising=False)
    monkeypatch.delenv(app.GAP_LIMIT_ENV_VAR, raising=False)
    monkeypatch.delenv(app.SIGNER_ENV_VAR, raising=False)
    wiring, commands = _mk_wiring(
        tmp_path, monkeypatch, stored_url="ssl://oldhost.local:50002"
    )
    app._host_trust_cache["oldhost.local"] = (time.monotonic() + 300.0, True)
    flow = app.ChainBackendFlow(wiring, _probe_true)
    error, fields = flow.apply("ssl://newhost.local:50002")
    assert error is None and fields["swapped"] is True
    assert app._host_trust_cache == {}  # invalidation at the swap seam
    fake = _FakeSeam({"newhost.local": True})
    monkeypatch.setattr(hostinfo, "resolves_to_private", fake)
    assert (
        app._backend_mode(wiring.settings) == app.BACKEND_MODE_OWN_NODE_PRIVATE
    )
    assert fake.calls == ["newhost.local"]  # freshly resolved, not served stale
    _drain(wiring, commands)
    wiring.store.close()



# ------------------------------------------------ /state + narration purity


def _state_snap(settings: Settings | None) -> dict[str, object]:
    """One typed /state read through a REAL engine pump (web023's pattern)."""
    events: list[Any] = []

    def bootstrap() -> app.EngineContext:
        return app.EngineContext(
            loop=app.AgentLoop(
                app.stub_generate, {app.IntentName.RESPOND: app._respond_handler}
            ),
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


def test_state_snapshot_resolves_to_enum_only_no_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """END-TO-END purity through the REAL seam: a name whose getaddrinfo
    answer is 10.9.9.9 flips the badge to own_node_private, but the IP never
    appears anywhere in the payload — and the FIELD SET is exactly what the
    literal-IP configuration already carried (nothing new rides /state)."""
    _fake_getaddrinfo(monkeypatch, {"lanname.example": ["10.9.9.9"]})
    resolved = _state_snap(Settings(chain_base_url="ssl://lanname.example:50002"))
    baseline = _state_snap(Settings(chain_base_url="ssl://10.9.9.9:50002"))
    assert resolved["privacy_mode"] == app.BACKEND_MODE_OWN_NODE_PRIVATE
    assert resolved["privacy_mode"] in app.PRIVACY_MODES
    assert set(resolved) == set(baseline)  # no new/extra field
    text = repr(
        {k: v for k, v in resolved.items() if k != "suggested_servers"}
    )
    assert "10.9.9.9" not in text  # the RESOLVED (==configured-here) IP: gone
    assert "://" not in text
    assert "50002" not in text
    # The ONLY host-shaped thing that rides is the pre-existing documented
    # exception — the BARE CONFIGURED NAME (not what it resolved to):
    assert resolved["backend_host"] == "lanname.example"


def test_unresolvable_state_snapshot_is_the_yellow_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed at the WIRE level too: a resolver error answers
    own_node_remote + bare configured host, never a crash and never a guess."""
    _fake_getaddrinfo(
        monkeypatch, {"flaky.example": socket.gaierror("temporary resolver failure")}
    )
    snap = _state_snap(Settings(chain_base_url="ssl://flaky.example:50002"))
    assert snap["privacy_mode"] == app.BACKEND_MODE_OWN_NODE_REMOTE
    assert snap["backend_host"] == "flaky.example"


def test_node_status_narration_follows_the_resolved_mode(seam: Any) -> None:
    """The node_status FACTS ride the SAME widened classification (banner
    lockstep) and carry only the enum name + the bare configured host."""
    seam({"lan-side.local": True})
    settings = Settings(
        chain_base_url="ssl://user:pw@lan-side.local:50002",
        node_detection_enabled=False,  # narration branch only, no probing
    )
    handler = app._make_node_status_handler(settings)
    facts = handler(SimpleNamespace(params=NodeStatusParams()))
    assert facts["backend_mode"] == app.BACKEND_MODE_OWN_NODE_PRIVATE
    assert facts["backend_host"] == "lan-side.local"
    text = repr(facts)
    assert "user" not in text and "pw@" not in text  # creds never ride

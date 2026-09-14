"""TCK-ONB-008 (chat backend creds UX, council-decided 2026-09-13): creds are
NEVER collected as chat text — a password typed into chat is echoed to every
tab (user_text), announced by the a11y live region, and replayed from the SSE
ring forever.

Pins (the ticket's done-when criteria):

* auth-capable detection MATRIX: each Bitcoin Core RPC-family scheme (the
  engine's own constants: bitcoind://, bitcoind+tls://, and the http(s)
  auto-detect aliases) fires the ONE deterministic hand-off bubble; the
  Electrum ssl:// scheme and bare hosts do NOT (a bare host is not even a
  URL candidate);
* the two copy constants VERBATIM (they are spec), each its own closed turn
  (UX-012 bubble discipline);
* ``user:pass@host`` typed in chat → value-free refusal that consumes the
  turn: the probe never runs, nothing is stored, and the typed string never
  echoes into any emitted event (creds-never-in-transcript pin);
* the auth-capable beat still PROCEEDS past the URL as today (the URL is not
  a secret): probe → store → swap rides unchanged after the bubble;
* non-auth-capable (ssl://) flow BYTE-IDENTICAL to the ONB-007 shape (the
  regression pin): no extra bubble, same acks.

All hermetic: the ONB-007 pump harness (fake chain wiring, injected probes).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from localwallet import app
from tests.test_onb007_chat_onboarding import (
    _closed_turns,
    _reopen,
    _texts,
    _wired_pump,
)


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
    """A line that REACHES the model proves the intercept did not swallow
    (nor the model receive) it — the ONB-007 harness recorder."""
    seen: list[str] = []

    def fake_turn(*args: Any, **kwargs: Any) -> None:
        line: str = args[3]
        output_fn: Callable[[str], None] = args[4]
        seen.append(line)
        output_fn(f"echo:{line}")

    monkeypatch.setattr(app, "_run_turn", fake_turn)
    return seen

# The council copy, asserted VERBATIM (spec, not editable prose).
CREDS_BUBBLE = (
    "That server asks for a username and password. I don't take those in "
    "chat — open Settings (top right). Under the server address you'll "
    "find the login fields; fill them in and press Apply."
)
LOGIN_REFUSAL = "Remove the login from the address — I'll ask for it in Settings."


def test_copy_constants_are_verbatim() -> None:
    assert app.CHAT_ONB_BACKEND_CREDS == CREDS_BUBBLE
    assert app.CHAT_ONB_LOGIN_REMOVED == LOGIN_REFUSAL


# ------------------------------------------------- 1. auth-capable detection


@pytest.mark.parametrize(
    "url",
    [
        "bitcoind://node.local:8332",
        "bitcoind+tls://node.local:8332",
        "http://192.168.1.5:8332",
        "https://node.local:8332",
        "BITCOIND://NODE.LOCAL:8332",  # scheme match is case-free, like the beat's
    ],
)
def test_bitcoind_family_is_auth_capable(url: str) -> None:
    assert app._chat_url_auth_capable(url)


@pytest.mark.parametrize(
    "url",
    [
        "ssl://electrum.example:50002",
        "SSL://electrum.example:50002",
    ],
)
def test_electrum_is_not_auth_capable(url: str) -> None:
    assert not app._chat_url_auth_capable(url)


@pytest.mark.parametrize("line", ["192.168.1.5:8332", "electrum.example:50002"])
def test_bare_host_is_not_even_a_url_candidate(line: str) -> None:
    # Bare hosts stay ordinary chat (ONB-007 candidate gate, untouched).
    assert app._chat_backend_url_candidate(line) is None


@pytest.mark.parametrize(
    ("url", "has_login"),
    [
        ("bitcoind://user:pass@host:8332", True),
        ("https://user:pass@host/api", True),
        ("bitcoind://user@host:8332", True),  # half a pair is still a login
        ("ssl://user:pass@host:50002", True),
        ("bitcoind://host:8332", False),
        ("https://host/a@b", False),  # "@" in the PATH is not userinfo
    ],
)
def test_embedded_login_detection(url: str, has_login: bool) -> None:
    assert app._chat_url_has_login(url) is has_login


# --------------------------------------------- 2. auth-capable bubble rides


def _probe_ok(seen: list[str]) -> Callable[[str], str | None]:
    def probe(url: str) -> str | None:
        seen.append(url)
        return url

    return probe


@pytest.mark.parametrize(
    "url",
    [
        "bitcoind://node.local:8332",
        "bitcoind+tls://node.local:8332",
        "http://192.168.1.5:8332",
        "https://node.local:8332",
    ],
)
def test_auth_capable_url_emits_one_creds_bubble_then_proceeds(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, env_clean: None,
    echo_turns: list[str], url: str,
) -> None:
    """ONE deterministic bubble (code-owned, zero model contact), then the
    beat proceeds past the URL as today: the SAME probe→store→swap ride."""
    probed: list[str] = []
    events, wiring = _wired_pump(
        tmp_path, monkeypatch, [url], probe=_probe_ok(probed)
    )
    assert probed == [url]  # the URL itself is not a secret — it rides on
    texts = _texts(events)
    # (The release scan's own narration tail is the ONB-007 harness shape.)
    assert texts[:3] == [CREDS_BUBBLE, app.CONFIRMED, app.SWITCHING_NOW]
    assert texts.count(CREDS_BUBBLE) == 1  # ONE bubble, not per-ack repeats
    # UX-012 bubble discipline: the creds hand-off closes its OWN turn.
    assert CREDS_BUBBLE in _closed_turns(events)
    assert echo_turns == []  # consumed deterministically, never model-ruled
    store = _reopen(tmp_path)
    try:
        assert store.get_chain_base_url() == url
    finally:
        store.close()
    assert wiring.settings.chain_base_url == url


def test_auth_capable_probe_failure_still_refuses_normally(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, env_clean: None,
    echo_turns: list[str],
) -> None:
    """The bubble rides BEFORE the probe; a failed probe keeps the EXISTING
    value-free refusal path untouched (no auth-class branch invented —
    BACKEND_PROBE_FAIL already carries the demanded-login hint)."""
    events, wiring = _wired_pump(
        tmp_path, monkeypatch, ["bitcoind://node.local:8332"],
        probe=lambda u: None,
    )
    texts = _texts(events)
    assert texts == [CREDS_BUBBLE, app.BACKEND_PROBE_FAIL]
    assert echo_turns == []
    store = _reopen(tmp_path)
    try:
        assert store.get_chain_base_url() is None
    finally:
        store.close()
    assert wiring.scan.gate.state == "awaiting_backend"  # still held


def test_electrum_url_flow_is_byte_identical(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, env_clean: None,
    echo_turns: list[str],
) -> None:
    """Regression pin: ssl:// (not auth-capable) keeps the ONB-007 shape
    exactly — same texts, same order, no extra bubble."""
    url = "ssl://electrum.example:50002"
    events, _wiring = _wired_pump(tmp_path, monkeypatch, [url], probe=lambda u: u)
    texts = _texts(events)
    assert texts[:2] == [app.CONFIRMED, app.SWITCHING_NOW]
    assert CREDS_BUBBLE not in texts


# -------------------------------------------------- 3. embedded-login refusal


@pytest.mark.parametrize(
    ("url", "secrets"),
    [
        ("bitcoind://rpcuser:s3cretpass@node.local:8332", ("rpcuser", "s3cretpass")),
        ("https://admin:hunter2@proxy.local:8332", ("admin", "hunter2")),
        ("bitcoind://onlyuser@node.local:8332", ("onlyuser",)),
    ],
)
def test_embedded_login_is_refused_and_never_echoed_or_stored(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, env_clean: None,
    echo_turns: list[str], url: str, secrets: tuple[str, ...],
) -> None:
    """Value-free refusal, consuming the turn: the probe never runs (never
    parsed-for-storage), the ONLY engine-emitted text is the constant line,
    and no engine-generated event carries any part of the typed string
    (creds-never-in-transcript pin — the one user_text echo of the user's
    OWN line is exactly the leak this ticket steers chat away from; the
    engine never ADDS the string anywhere).
    """
    probed: list[str] = []
    events, wiring = _wired_pump(
        tmp_path, monkeypatch, [url], probe=_probe_ok(probed)
    )
    assert probed == []  # refused BEFORE the probe — no network contact
    assert _texts(events) == [LOGIN_REFUSAL]
    assert echo_turns == []  # the turn is consumed, the model never sees it
    for event in events:
        if event.kind == app.EVENT_USER_TEXT:
            continue  # the user's own echo — pre-existing transport behavior
        payload = getattr(event, "payload", None)
        if isinstance(payload, str):
            for secret in secrets:
                assert secret not in payload
    store = _reopen(tmp_path)
    try:
        assert store.get_chain_base_url() is None
        assert store.get_backend_auth_user() is None
        assert store.get_backend_auth_pass() is None
    finally:
        store.close()
    assert wiring.scan.gate.state == "awaiting_backend"  # ask stays open


def test_login_refusal_is_a_dead_end_only_for_that_turn(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, env_clean: None,
    echo_turns: list[str],
) -> None:
    """After the refusal the SAME beat re-arms: a clean auth-capable URL in
    the next turn rides the normal flow (refusal never strands the flow)."""
    events, _wiring = _wired_pump(
        tmp_path, monkeypatch,
        ["bitcoind://rpcuser:s3cretpass@node.local:8332", "bitcoind://node.local:8332"],
        probe=lambda u: u,
    )
    texts = _texts(events)
    assert texts[:4] == [LOGIN_REFUSAL, CREDS_BUBBLE, app.CONFIRMED, app.SWITCHING_NOW]
    assert echo_turns == []

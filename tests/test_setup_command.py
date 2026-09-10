"""TCK-ONB-005 — the ``/setup`` transcript command + doctor coherence.

The bug: after first run there was NO deterministic way to set up the
backend — ``settings``/``help``/``setup my node``/``ssl://…`` all fell
through to the LLM (which can only narrate), and the ADR-0023 backend
branch fired ONLY on a first launch with a fresh wallet. ``/setup`` now
runs the SAME code-owned state machine (:class:`OnboardingFlow`) against
an EXISTING wallet — privacy ask → URL entry → ``check_backend``
validation → typed store write → hot-swap + full resync IN-SESSION
(TCK-BACKEND-002, ADR-0018 amendment; the next-launch copy returns only
when the swap declines) — gated by an explicit y/n when a choice is
already stored.

Pinned here (ticket requirement 6):

* happy path (URL accepted → stored → live swap), skip path (no
  change), reject path (bad URL → value-free failure → retry → explicit
  public pick — never saved, never echoed);
* the already-stored overwrite gate: decline → unchanged; accept →
  replace; non-y/n holds at the gate; skip inside a gated run keeps the
  stored choice ("keep current", not the first-run "public for now");
  an explicit public pick reverts (clears the stored rung);
* ``ssl://``/electrum-protocol URLs: FIRST-CLASS candidates since
  TCK-BACKEND-002 (probe → store → swap, M3 acceptance); genuinely
  foreign schemes (ftp://…) stay plainly refused — never probed, never
  saved, re-prompted — in both the ask state and the URL-entry state;
* ``/help`` lists ``/setup``; ``/setup`` without a CLI flow (web/headless
  pumps) prints the pointer instead — CLI-only by construction;
* the doctor's ``NONE_FOUND`` guidance names the real options (no more
  dangling "Pick a setup option below");
* a DORMANT flow (every returning-user launch) consumes nothing: ordinary
  chat — including "2" and pasted URLs — reaches the model exactly as
  before the always-build change;
* the ``/label``-style pump pin: the whole /setup conversation never
  touches ``_run_turn``/the model, and no transcript entry is recorded.
"""

from __future__ import annotations

import queue
from pathlib import Path
from typing import Any

import pytest

import localwallet.app as app_module
from localwallet.agent.loop import AgentLoop
from localwallet.app import SendSession, _handle_transcript_command
from localwallet.node.doctor import STATE_ADVICE, NodeStateKind
from localwallet.protocol import IntentName
from localwallet.store import Store
from localwallet.ui import onboarding as ob

# Canonical fixture key material + the established onboarding harness (public
# fixture only; the /setup conversation is the same state machine as
# first-run step 5, so its seams are reused verbatim).
from tests.test_onboarding import GOOD_URL, _drive, _fake_client, _preset_wallet

SSL_URL = "ssl://evil-star.local:50001"
BAD_URL = "https://typo.example/api"


# ------------------------------------------------------------- happy path


def test_setup_happy_path_stores_and_promises_next_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing wallet, nothing on the STORED rung: /setup → 2 → valid URL →
    (d) + the honest next-launch line; choice lands in the stored rung; the
    startup first-run conversation NEVER replays (no greeting, ask printed
    once). The env rung here exists to RESOLVE the launch (security review
    F1: an unresolved interactive launch now holds its scan and re-arms the
    mandatory ask at startup regardless of AUTO_SCAN — /setup on top of it
    would double-ask); a resolved launch keeps the flow DORMANT until
    /setup."""
    _preset_wallet(tmp_path)
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path,
        lines=["/setup", "2", GOOD_URL, "exit"],
        interactive=True,
        chain_env=GOOD_URL,
        backend_check=lambda _u: True,
    )
    assert code == 0
    joined = rec.joined
    assert ob.GREETING not in joined  # an existing wallet: no step-1 replay
    assert joined.count(ob.NODE_ASK) == 1  # via /setup only, verbatim ask (a)
    assert ob.LOAD_NARRATION not in joined  # dormant session: no step-3 echo
    assert ob.URL_PROMPT in joined  # (b)
    assert ob.CONFIRMED in joined  # (d)
    assert ob.EFFECTS_NEXT_LAUNCH in joined  # ADR-0018: next launch, no swap
    assert stored == GOOD_URL
    assert state["probes"] == 1
    assert state["model"] == 0  # every line deterministic


def test_setup_skip_keeps_current_and_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"/setup then skip where (e) is TRUE: a recorded public consent makes
    the launch resolved, so the public server really IS current. (Security
    review F1 moved the scan-less UNRESOLVED skip out of this branch —
    that launch holds the scan and answers ASK_WAITS_ACK.) Nothing
    changes, nothing probed, model 0; the stored URL rung stays empty (the
    marker, not a URL, carries the public choice)."""
    _drive(  # run 1: the warned consent that records the public marker.
        monkeypatch, tmp_path, lines=["1", "exit"], interactive=True
    )
    code, rec, stored, state = _drive(  # run 2: resolved, dormant, /setup.
        monkeypatch, tmp_path,
        lines=["/setup", "not now", "exit"],
        interactive=True,
    )
    assert code == 0
    assert ob.SKIP_ACK in rec.joined  # public IS current — by that choice
    assert ob.ASK_WAITS_ACK not in rec.joined  # nothing is held here
    assert stored is None
    assert state["probes"] == 0
    assert state["model"] == 0


def test_setup_reject_bad_url_retry_then_explicit_public(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure path (c) is value-free: a bad URL collapses to the plain
    failure copy (never echoed), "retry" re-probes it, and only the
    explicit public pick closes the run. Nothing is ever saved."""
    _preset_wallet(tmp_path)
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path,
        lines=["/setup", "2", BAD_URL, "retry", "1", "exit"],
        interactive=True,
        backend_check=lambda _u: False,
    )
    assert code == 0
    joined = rec.joined
    assert joined.count(ob.VALIDATION_FAIL) == 2  # original + retry
    assert "typo.example" not in joined  # value-free: never echoed
    # An explicit public pick with NOTHING stored is a CONSENT, not a shrug
    # (TCK-ONB-006): the ack re-names the leak and the opt-in record lands.
    assert ob.PUBLIC_CHOSEN_ACK in joined
    assert stored is None  # a failed URL is NEVER written
    assert state["probes"] == 2
    assert state["model"] == 0


# --------------------------------------------------- stored-choice gate


def test_setup_gate_decline_exits_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored choice: /setup shows it FIRST (mode framing, URL never
    echoed) and an explicit n closes the command with no change — the ask
    never even appears (no dead end, nothing overwritten)."""
    _preset_wallet(tmp_path, base_url=GOOD_URL)
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path,
        lines=["/setup", "n", "exit"],
        interactive=True,
    )
    assert code == 0
    joined = rec.joined
    assert ob.SETUP_CURRENT in joined  # the current choice, named by mode
    assert GOOD_URL not in joined and "mempool.mine" not in joined  # value-free
    assert ob.SETUP_KEPT in joined
    assert ob.NODE_ASK not in joined  # declined: never entered the branch
    assert stored == GOOD_URL  # unchanged
    assert state["probes"] == 0
    assert state["model"] == 0


def test_setup_gate_holds_on_anything_but_yn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate is deterministic: anything but y/n/back holds INSIDE the
    gate (re-prompted) — no prose answer, no pasted URL, no model turn."""
    _preset_wallet(tmp_path, base_url=GOOD_URL)
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path,
        lines=["/setup", "yes do it later maybe", SSL_URL, "back", "exit"],
        interactive=True,
    )
    assert code == 0
    joined = rec.joined
    assert joined.count(ob.SETUP_OVERWRITE) == 3  # entry + two hold re-prompts
    assert ob.SETUP_KEPT in joined  # "back" declines at the gate
    assert ob.NON_ESPLORA_URL not in joined  # gate swallows before the scheme check
    assert ob.NODE_ASK not in joined
    assert stored == GOOD_URL
    assert state["probes"] == 0
    assert state["model"] == 0


def test_setup_gate_accept_overwrites_stored_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """y at the gate opens the ask; a new validated URL REPLACES the stored
    rung (the write the gate authorized) — and since TCK-BACKEND-002 (the
    ADR-0018 amendment) the live client follows IN-SESSION: swap + full
    resync, no next-launch promise."""
    _preset_wallet(tmp_path, base_url=GOOD_URL)
    NEW_URL = "https://node2.mine.example:4000/api"
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path,
        lines=["/setup", "y", "2", NEW_URL, "exit"],
        interactive=True,
        backend_check=lambda _u: True,
    )
    assert code == 0
    assert ob.CONFIRMED in rec.joined
    assert ob.SWITCHING_NOW in rec.joined  # hot-swapped, resyncing now
    assert ob.EFFECTS_NEXT_LAUNCH not in rec.joined
    assert stored == NEW_URL
    assert state["probes"] == 1
    assert state["model"] == 0


def test_setup_gate_accept_then_skip_keeps_stored_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirming the gate is not the overwrite itself: skipping the ask
    afterwards keeps the stored choice — and the ack says "keeps your
    current backend", NOT the first-run "public for now" lie."""
    _preset_wallet(tmp_path, base_url=GOOD_URL)
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path,
        lines=["/setup", "y", "not now", "exit"],
        interactive=True,
    )
    assert code == 0
    assert ob.SETUP_KEEP_CURRENT in rec.joined
    assert ob.SKIP_ACK not in rec.joined  # the public-default wording would lie
    assert stored == GOOD_URL  # unchanged
    assert state["model"] == 0


def test_setup_explicit_public_reverts_stored_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After confirming, picking the PUBLIC server explicitly clears the
    stored rung (typed writer's ""-convention) — honestly worded with the
    session-still-on-current-backend truth."""
    _preset_wallet(tmp_path, base_url=GOOD_URL)
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path,
        lines=["/setup", "y", "1", "exit"],
        interactive=True,
    )
    assert code == 0
    assert ob.SETUP_REVERTED in rec.joined
    assert ob.SETUP_KEEP_CURRENT not in rec.joined
    assert stored is None  # cleared → public default next launch
    # TCK-ONB-006: the revert must ALSO write the explicit-public record —
    # an unset stored rung reads "never chose", which would re-arm the
    # mandatory ask (and hold the scan) on the next launch.
    check = Store(str(tmp_path / "onb.db"))
    try:
        assert check.get_setting(ob.BACKEND_CHOICE_SETTING) == ob.BACKEND_CHOICE_PUBLIC
    finally:
        check.close()
    assert state["model"] == 0


# -------------------------------------------------- ssl:// / electrum URLs


def test_setup_ssl_url_is_probed_stored_and_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TCK-BACKEND-002 (user direction 9): an ``ssl://`` (Electrum-protocol)
    address at URL entry is a FIRST-CLASS candidate now — probed through the
    same setup check, stored through the typed writer, and hot-swapped with
    the full resync; never echoed. The M1 adapter's handshake (injected
    fake here) replaces the old never-probed refusal."""
    _preset_wallet(tmp_path)
    monkeypatch.setattr(app_module, "ElectrumClient", lambda **_kw: _fake_client())
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path,
        lines=["/setup", "2", SSL_URL, "exit"],
        interactive=True,
        backend_check=lambda _u: True,
    )
    assert code == 0
    joined = rec.joined
    assert ob.NON_ESPLORA_URL not in joined  # the refusal era is over
    assert ob.VALIDATION_FAIL not in joined
    assert ob.CONFIRMED in joined
    assert ob.SWITCHING_NOW in joined  # stored AND swapped live now
    assert stored == SSL_URL  # the stored rung carries it (M3 acceptance)
    assert "evil-star" not in joined  # value-free: the URL is never echoed
    assert state["probes"] == 1  # probed as a candidate, once
    assert state["model"] == 0


def test_setup_foreign_scheme_named_never_probed_reprompted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheme statement (narrowed by TCK-BACKEND-002 to genuinely
    foreign schemes — ssl:// is spoken for now): an ftp:// address gets the
    plain never-probed, never-saved answer at URL entry and the entry
    re-prompts — the real URL that follows lands normally."""
    _preset_wallet(tmp_path)
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path,
        lines=["/setup", "2", "ftp://x/api", GOOD_URL, "exit"],
        interactive=True,
        backend_check=lambda _u: True,
    )
    assert code == 0
    joined = rec.joined
    assert ob.NON_ESPLORA_URL in joined
    assert "ftp" not in joined  # the copy names families, never the URL
    assert ob.VALIDATION_FAIL not in joined  # not probed → no "didn't check out"
    assert stored == GOOD_URL  # re-prompted entry accepted the real URL
    assert state["probes"] == 1  # ONLY the accepted candidate was ever probed
    assert state["model"] == 0


def test_setup_ssl_url_at_the_ask_is_consumed_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ssl:// paste while the ask is open rides the URL-candidate channel
    (TCK-BACKEND-002 — no longer the foreign-scheme statement): probed as a
    candidate (refused here, nothing saved, ask stays open), never echoed.
    The skip that follows lands on the HELD launch's honest answer
    (ASK_WAITS_ACK, security review F1 — an unresolved launch waits,
    scan-less or not), still not the plain (e) ack."""
    _preset_wallet(tmp_path)
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path,
        lines=["/setup", SSL_URL, "skip", "exit"],
        interactive=True,
    )
    assert code == 0
    assert ob.NON_ESPLORA_URL not in rec.joined  # candidate, not foreign
    assert ob.VALIDATION_FAIL in rec.joined  # probed, refused (seam says no)
    assert "evil-star" not in rec.joined
    assert ob.ASK_WAITS_ACK in rec.joined  # held: skipping is not consent
    assert ob.SKIP_ACK not in rec.joined
    assert stored is None
    assert state["probes"] == 1  # the ssl:// candidate WAS probed (as a candidate)
    assert state["model"] == 0


# ---------------------------------------------------------------- /help,
#                                                       web/headless shape


def test_help_lists_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Requirement 4: /help mentions /setup (and keeps every old entry)."""
    _preset_wallet(tmp_path)
    code, rec, _stored, _ = _drive(
        monkeypatch, tmp_path, lines=["/help", "exit"], interactive=True,
    )
    assert code == 0
    assert "/setup" in rec.joined
    assert "/details" in rec.joined and "/label" in rec.joined


def test_setup_without_cli_flow_prints_pointer() -> None:
    """Requirement 5, structural: no flow (web pump, headless pump) → the
    command cannot arm anything; it answers with the pointer instead of
    falling through to the model. _SETUP_CLI_ONLY never echoes a value."""
    outputs: list[str] = []
    _handle_transcript_command("/setup", None, outputs.append)  # type: ignore[arg-type]
    assert outputs == [app_module._SETUP_CLI_ONLY]
    assert "terminal" in outputs[0]


# ------------------------------------------------------- doctor coherence


def test_doctor_none_found_points_at_setup() -> None:
    """Requirement 3: the dangling "Pick a setup option below" is gone —
    the NONE_FOUND guidance names the REAL options (the /setup command and
    the public default it can decline)."""
    next_step = STATE_ADVICE[NodeStateKind.NONE_FOUND].next_step
    assert "/setup" in next_step
    assert "Esplora" in next_step
    assert "public default" in next_step
    assert "below" not in next_step  # the dangling pointer, pinned dead


# ------------------------------------------------------- dormant-session


def test_dormant_flow_consumes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The always-build regression pin: a RESOLVED returning-user launch
    carries a DORMANT flow, and the ask's own vocabulary ("2", "not now",
    a pasted URL) is ordinary chat until /setup arms it — every line
    reaches the model exactly as before the flow existed. (An UNRESOLVED
    returning launch is no longer dormant at all — security review F1: the
    mandatory ask re-arms there, pinned in test_onboarding.)"""
    _preset_wallet(tmp_path, base_url=GOOD_URL)  # resolved: stored rung set
    code, rec, stored, state = _drive(
        monkeypatch, tmp_path,
        lines=["2", "not now", GOOD_URL, "exit"],
        interactive=True,
        backend_check=lambda _u: True,
    )
    assert code == 0
    assert state["model"] == 3  # all three lines ran as turns
    assert ob.NODE_ASK not in rec.joined  # startup stayed silent
    assert stored == GOOD_URL  # the pasted URL was chat, not a setup write
    assert state["probes"] == 0


# ------------------------------------------------- pump pin (never model)


def test_setup_never_reaches_run_turn_through_the_pump(
    tmp_path: Path,
) -> None:
    """The /label-style channel pin: a raw pump, a DORMANT-armed flow, and
    a recording generate_fn. The whole /setup conversation — command, ask,
    URL entry, confirmation — runs with the model at ZERO calls and the
    transcript at ZERO entries; the store holds the result."""
    prompts: list[str] = []

    def recording_generate(prompt: str, grammar: str | None) -> str:
        prompts.append(prompt)
        return '{"v": 0, "intent": "respond", "params": {"text": "ok"}}'

    store = Store(str(tmp_path / "pump.db"))
    flow = ob.OnboardingFlow(
        store=store, check_backend=lambda _url: _url, armed=False
    )
    loop = AgentLoop(
        recording_generate,
        {
            IntentName.RESPOND: app_module._respond_handler,
            IntentName.CLARIFY: app_module._clarify_handler,
        },
    )
    commands: queue.Queue[Any] = queue.Queue()
    for line in ("/setup", "2", GOOD_URL, "exit"):
        commands.put(line)
    outputs: list[str] = []
    app_module._pump(
        loop,
        outputs.append,
        commands,
        flow=app_module.TxFlow(),
        session=SendSession(),
        table={IntentName.RESPOND: app_module._respond_handler},
        store=store,
        onboarding=flow,
    )
    assert prompts == []  # _run_turn never ran
    assert not loop.history  # no transcript entry for any /setup line
    assert store.get_chain_base_url() == GOOD_URL  # the write DID happen
    assert ob.CONFIRMED in "\n".join(outputs)
    store.close()

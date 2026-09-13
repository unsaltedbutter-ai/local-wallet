"""TCK-HW-005 SLICE A: hardware probe/report + chat unlock flow.

Covers the user live findings (2026-09-12):
* "can you see my hardware wallet?" / "unlock my hardware wallet" /
  "I connected my hardware wallet" are intercepted PRE-MODEL by the
  deterministic matcher (:func:`app._hardware_chat_verb`) — the model's
  "I cannot access your hardware wallet" deflection is structurally
  unreachable for these utterances;
* :meth:`HwiUsbSigner.probe_and_report` narrates the enumerate facts
  (kind/model, readable fingerprint, needs_pin_sent/needs_passphrase_sent)
  honestly, reuses the EXISTING guidance family verbatim for
  no-device/locked/failure states, and drives the host-unlockable
  classes (Jade/BitBox02: client construction IS the unlock, TCK-HW-001)
  only on the unlock/connect families;
* everything is value-free: paths, fingerprints, and device error text
  never reach the narration;
* later slices (show-address, pre-sign check, static button) are NOT
  in scope here — no sign path changes are tested because none were made.

hwilib is faked with the same ``FakeCommands`` seam as
``tests/test_signer_hwi.py`` (imported, not re-implemented).
"""

from __future__ import annotations

import queue
from typing import Any

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.app import (
    EVENT_TEXT,
    EventEmitter,
    SendSession,
    _hardware_chat_verb,
)
from localwallet.protocol import IntentName
from localwallet.tx.flow import TxFlow
from tests.test_signer_hwi import (
    DEVICE_WALLET,
    FP_WALLET,
    JADE_LOCKED,
    JADE_UNLOCKED,
    MASTER_FP,
    TREZOR_LOCKED,
    DeviceConnectionError,
    FakeCommands,
    JadeError,
    make_signer,
)

BITBOX_LOCKED = {
    "type": "bitbox02",
    "path": "usb:bitbox-path",
    "model": "BitBox02",
    "needs_pin_sent": False,
    "needs_passphrase_sent": False,
    "error": "BitBox02 is not unlocked",
    "code": -12,
}

# ---------------------------------------------------------------------------
# (a) matcher accept/reject vectors (pinned per ticket)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "verb"),
    [
        # the three live-finding families
        ("can you see my hardware wallet?", "see"),
        ("Do you see my Jade?", "see"),
        ("did it detect my coldcard", "see"),
        ("unlock my hardware wallet", "unlock"),
        ("Unlock the Jade", "unlock"),
        ("unlock my bitbox", "unlock"),
        ("please unlock my trezor", "unlock"),
        ("I connected my hardware wallet", "connect"),
        ("i've plugged in my Jade", "connect"),
        ("my jade is locked", "connect"),
        ("connect my ledger", "connect"),
        # priority: unlock > connect > see when several verbs appear
        ("unlock my jade and check it", "unlock"),
        ("I connected my wallet, can you see the device?", "connect"),
    ],
)
def test_matcher_accept_vectors(line: str, verb: str) -> None:
    assert _hardware_chat_verb(line) == verb


@pytest.mark.parametrize(
    "line",
    [
        "",
        "hello",
        "what is my balance",
        "can you see my balance",  # verb, no hardware topic word
        "unlock",  # verb, no topic
        "unlock my wallet",  # no HARDWARE/device/class word (pinned choice)
        "connect to my node",  # node is not a hardware device
        "hardware",  # topic, no verb
        "how do I connect to the internet",
        "can you see my laptop",
        "sign the transaction",
        "retry",
    ],
)
def test_matcher_reject_vectors(line: str) -> None:
    assert _hardware_chat_verb(line) is None


# ---------------------------------------------------------------------------
# (b) probe_and_report — enumerate facts narrated honestly, value-free
# ---------------------------------------------------------------------------


def _probe(devices: list[dict] | None = None, **kwargs: Any) -> tuple[str, ...]:
    commands = FakeCommands(devices=devices, **kwargs)
    return make_signer(commands).probe_and_report()


def test_probe_no_devices_reuses_absent_family() -> None:
    (line,) = _probe(devices=[])
    assert line == (
        "No device found — plug in and unlock your device, then say 'retry'."
    )


def test_probe_enumerate_error_maps_to_guidance_never_raises() -> None:
    (line,) = _probe(enumerate_error=DeviceConnectionError("Device is asleep"))
    assert "plug in" in line.lower()


def test_probe_unlocked_jade_reports_ready_without_opening() -> None:
    commands = FakeCommands(devices=[dict(JADE_UNLOCKED)])
    lines = make_signer(commands).probe_and_report()
    assert lines == ("Found your jade — it's unlocked and ready.",)
    # report never opens a client, and never signs
    assert [c for c in commands.calls if c[0] == "get_client"] == []
    assert [c for c in commands.calls if c[0] == "signtx"] == []


def test_probe_locked_jade_report_only_reuses_jade_guidance() -> None:
    commands = FakeCommands(devices=[dict(JADE_LOCKED)])
    lines = make_signer(commands).probe_and_report()
    assert lines[0] == "Found your jade — it's locked."
    # EXISTING HW-001 guidance verbatim (on-device unlock, menu escapes)
    assert "Recovery Phrase Login" in lines[1]
    assert "QR PIN Unlock" in lines[1]
    assert [c for c in commands.calls if c[0] == "get_client"] == []


def test_unlock_locked_jade_drives_client_construction_and_reports_success() -> None:
    """Jade unlock = client construction (HW-001 pinserver relay)."""
    commands = FakeCommands(devices=[dict(JADE_LOCKED)])
    lines = make_signer(commands).probe_and_report(attempt_unlock=True)
    opened = [c for c in commands.calls if c[0] == "get_client"]
    assert len(opened) == 1 and opened[0][1:3] == ("jade", "/dev/tty.usbmodemJADE")
    assert lines[-1] == "Your jade is unlocked and ready."
    assert commands.rec["closed"] is True  # handle released
    assert [c for c in commands.calls if c[0] == "signtx"] == []


def test_unlock_failure_surfaces_existing_guidance_value_free() -> None:
    """A device-side cancel during unlock returns the EXISTING canceled
    guidance verbatim; the raw JadeError text (which may carry device
    values) never leaks and the report still never raises."""
    commands = FakeCommands(
        devices=[dict(JADE_LOCKED)],
        get_client_error=JadeError(-32000, "User Canceled 12345 bc1qsecret", None),
    )
    lines = make_signer(commands).probe_and_report(attempt_unlock=True)
    assert lines[-1] == (
        "The request was canceled on your device — say 'retry' to try again."
    )
    joined = "\n".join(lines)
    assert "12345" not in joined and "bc1q" not in joined and "JadeError" not in joined


def test_unlock_client_none_reports_client_gone() -> None:
    commands = FakeCommands(devices=[dict(JADE_LOCKED)], client=None)
    lines = make_signer(commands).probe_and_report(attempt_unlock=True)
    assert "could not be opened" in lines[-1]


def test_unlock_bitbox_drives_device_side_passphrase_flow() -> None:
    commands = FakeCommands(devices=[dict(BITBOX_LOCKED)])
    lines = make_signer(commands).probe_and_report(attempt_unlock=True)
    opened = [c for c in commands.calls if c[0] == "get_client"]
    assert len(opened) == 1 and opened[0][1] == "bitbox02"
    assert lines[-1] == "Your BitBox02 is unlocked and ready."
    assert commands.rec["closed"] is True


def test_bitbox_needing_passphrase_is_driven_even_when_readable() -> None:
    commands = FakeCommands(
        devices=[{**BITBOX_LOCKED, "fingerprint": MASTER_FP,
                  "needs_passphrase_sent": True}]
    )
    lines = make_signer(commands).probe_and_report(attempt_unlock=True)
    assert lines[0] == "Found your BitBox02 — it's locked."
    assert len([c for c in commands.calls if c[0] == "get_client"]) == 1


def test_host_pin_device_reported_never_driven() -> None:
    """A locked Trezor (needs_pin_sent) gets the EXISTING companion-app
    guidance and is NEVER auto-driven (promptpin/sendpin stay out of
    scope, ADR-0015 amendment) — even on the unlock family."""
    commands = FakeCommands(devices=[dict(TREZOR_LOCKED)])
    lines = make_signer(commands).probe_and_report(attempt_unlock=True)
    joined = "\n".join(lines)
    assert "companion" in joined.lower()
    assert [c for c in commands.calls if c[0] == "get_client"] == []


def test_readable_other_device_reports_ready() -> None:
    lines = _probe(devices=[dict(DEVICE_WALLET)])
    assert lines == ("Found your trezor_t — it's unlocked and ready.",)


def test_passphrase_awaiting_trezor_is_not_reported_ready() -> None:
    """fp readable BUT needs_passphrase_sent → companion-flow guidance,
    never a 'ready' lie (it cannot sign until the passphrase flow ran)."""
    lines = _probe(
        devices=[{**DEVICE_WALLET, "needs_passphrase_sent": True}]
    )
    assert lines[0] == "Found your trezor_t — it's locked."
    assert "companion" in lines[1].lower()


def test_unknown_locked_shape_keeps_generic_guidance() -> None:
    lines = _probe(devices=[{"type": "somekey", "path": "hid:x", "model": "odd"}])
    assert lines[0] == "Found your odd — it's locked."
    assert "pin/passphrase" in lines[1].lower()


def test_probe_narration_is_value_free() -> None:
    """No device PATH, no fingerprint hex, no raw error text — ever."""
    for devices in (
        [dict(JADE_UNLOCKED)],
        [dict(JADE_LOCKED)],
        [dict(TREZOR_LOCKED)],
        [dict(DEVICE_WALLET)],
        [dict(JADE_UNLOCKED), dict(JADE_LOCKED)],
    ):
        for attempt in (False, True):
            commands = FakeCommands(devices=[dict(d) for d in devices])
            lines = make_signer(commands).probe_and_report(attempt_unlock=attempt)
            joined = "\n".join(lines).lower()
            assert "hid:" not in joined and "usb:" not in joined
            assert "tty" not in joined and "usbmodem" not in joined
            assert MASTER_FP not in joined and FP_WALLET not in joined


# ---------------------------------------------------------------------------
# (c) app-level interception: probe/unlock utterances never reach the model
# ---------------------------------------------------------------------------


class _FakeProbe:
    """Duck-typed signer stand-in for pump/turn tests."""

    def __init__(self) -> None:
        self.calls: list[bool] = []

    def probe_and_report(self, *, attempt_unlock: bool) -> tuple[str, ...]:
        self.calls.append(attempt_unlock)
        return ("Found your jade — it's unlocked and ready.",)


def _recording_loop() -> tuple[AgentLoop, list[str]]:
    seen: list[str] = []

    def generate(prompt: str, grammar: Any) -> str:
        seen.append(prompt)
        return '{"v":0,"intent":"respond","params":{"text":"model answer"}}'

    loop = AgentLoop(
        generate, {IntentName.RESPOND: app._respond_handler}
    )
    return loop, seen


def _turn(line: str, hwi: Any) -> tuple[list[str], list[str]]:
    loop, model_calls = _recording_loop()
    out: list[str] = []
    app._run_turn(
        loop,
        TxFlow(),
        SendSession(),
        line,
        out.append,
        table={},
        hwi=hwi,
    )
    return out, model_calls


def test_turn_see_family_probes_report_only() -> None:
    hwi = _FakeProbe()
    out, model_calls = _turn("can you see my hardware wallet?", hwi)
    assert out == ["Found your jade — it's unlocked and ready."]
    assert hwi.calls == [False]
    assert model_calls == []  # the model NEVER hears this turn


def test_turn_unlock_family_drives_auto_unlock() -> None:
    hwi = _FakeProbe()
    _turn("unlock my hardware wallet", hwi)
    assert hwi.calls == [True]


def test_turn_connected_family_auto_unlocks() -> None:
    hwi = _FakeProbe()
    _turn("I connected my hardware wallet", hwi)
    assert hwi.calls == [True]


def test_turn_unlock_through_real_signer_no_device() -> None:
    """End-to-end through the REAL probe code (fake hwilib): nothing
    plugged in → the existing no-device family, not a model answer."""
    signer = make_signer(FakeCommands(devices=[]))
    out, model_calls = _turn("unlock the jade", signer)
    assert out == [
        "No device found — plug in and unlock your device, then say 'retry'."
    ]
    assert model_calls == []


def test_turn_unlock_locked_jade_success_through_real_signer() -> None:
    signer = make_signer(FakeCommands(devices=[dict(JADE_LOCKED)]))
    out, model_calls = _turn("unlock my jade", signer)
    assert out[0] == "Found your jade — it's locked."
    assert "Unlocking now" in out[1]
    assert out[-1] == "Your jade is unlocked and ready."
    assert model_calls == []


def test_ordinary_line_still_reaches_the_model() -> None:
    hwi = _FakeProbe()
    _out, model_calls = _turn("what is my balance?", hwi)
    assert model_calls != []
    assert hwi.calls == []


def test_pump_intercepts_hw_utterance_and_closes_turn() -> None:
    """Web/CLI parity: the queue pump delivers the probe narration as the
    turn's TEXT events (user echo first, one turn_end) — model untouched."""
    events: list[Any] = []
    emitter = EventEmitter(events.append)
    commands: queue.Queue[Any] = queue.Queue()
    loop, model_calls = _recording_loop()
    commands.put("can you see my hardware wallet?")
    commands.put(app.QUIT)
    app._pump(
        loop,
        emitter.text,
        commands,
        flow=TxFlow(),
        session=SendSession(),
        table={},
        emitter=emitter,
        hwi=_FakeProbe(),
    )
    texts = [(e.kind, e.payload) for e in events if e.kind == EVENT_TEXT]
    assert texts == [
        (EVENT_TEXT, "Found your jade — it's unlocked and ready."),
    ]
    assert model_calls == []


def test_pump_without_hwi_keeps_pipeline_unchanged() -> None:
    """No probe signer wired (placeholder/test pumps) → the line is
    ordinary chat, exactly like before this slice."""
    events: list[Any] = []
    emitter = EventEmitter(events.append)
    commands: queue.Queue[Any] = queue.Queue()
    loop, model_calls = _recording_loop()
    commands.put("can you see my hardware wallet?")
    commands.put(app.QUIT)
    app._pump(
        loop,
        emitter.text,
        commands,
        flow=TxFlow(),
        session=SendSession(),
        table={},
        emitter=emitter,
    )
    assert model_calls != []

"""TCK-HW-005 SLICE C: pre-sign device check + hardware sign-routing.

User live findings (2026-09-12): in a pending flow "sign" exported a PSBT
FILE while the user wanted their Jade, and "let's sign with my hardware
wallet" also exported the file. Pinned decisions as implemented:

* a pending "sign with my hardware wallet" (CREATED/CONFIRMED) is
  intercepted BEFORE the model and routed through the REAL confirm/sign
  dispatch with a session-latched device preference — probe-gated (the
  handler's ONE bounded enumerate decides: present -> device sign;
  absent -> the value-free file-offering ASK, never a silent export;
  locked -> the existing guidance family, no unlock attempt, no export);
* bare "file"/"export" (CONFIRMED-scoped, pinned phrase set) answers the
  ask with the explicit airgap export — even under the hwi config;
* bare "retry" re-probes while the device wish is latched (the unchanged
  TCK-HW-002 dispatch, honoring the latch inside the handler);
* configured file signer + no hardware utterance = the pre-slice file
  behavior BYTE-IDENTICAL (TCK-HW-004's matrix; the full lifecycle suites
  pin the export path, this file pins ``HwiUsbSigner``-never-constructed);
* the model's ``sign_tx`` ``signer`` param stays advisory (HW-004): it can
  never set or clear the session flags — only the user's own matched
  utterances do.

Device facts come from the SAME enumerate path as slice A; the fakes are
imported from ``tests/test_signer_hwi`` / ``tests/test_e2e_skeleton``, not
re-implemented. With NO flow pending, the sign utterance degrades to
slice A's probe/report (report-only — no unlock is driven).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from localwallet import app
from localwallet.app import (
    _HW_DEVICE_PREFER_NOTE,
    _HW_SIGN_ASK,
    _file_export_choice,
    _hardware_chat_verb,
    _hardware_sign_denied,
)
from localwallet.protocol import Envelope, IntentName, SignTxParams, validate_payload
from localwallet.signer.hwi import HwiUsbSigner as RealHwiUsbSigner
from localwallet.tx.flow import GateDecision, TxFlow, TxFlowStatus
from tests.test_e2e_skeleton import (
    SEND_RECIPIENT,
    SEND_UTXO,
    FactsQuotingGenerate,
    _build_send_table,
    _create_tx_envelope_json,
    _FakeDeviceCommands,
    _fixture_parsed,
    _run_send_repl,
    _send_chain_handler,
    derive_fixture_addresses,
)
from tests.test_hw005_probe_unlock import _FakeProbe, _turn
from tests.test_signer_hwi import DEVICE_WALLET, JADE_LOCKED, FakeCommands, make_signer

JADE_READY = {
    "type": "jade",
    "path": "/dev/tty.usbmodemJADE",
    "model": "jade",
    # hwilib reports the MASTER fp here — never the wallet account fp;
    # nothing in the trust gate consumes it (ADR-0015 amendment #2).
    "fingerprint": "40dbb192",
}


class SwitchableCommands(_FakeDeviceCommands):
    """The e2e bind-capable fake device with a MUTABLE enumeration and one
    cross-instance sign counter (the repl harness builds the signer through
    a lambda per attempt — the parent's per-instance counter would reset)."""

    def __init__(
        self, fingerprint_hex: str, devices: list[dict] | None = None
    ) -> None:
        super().__init__(fingerprint_hex)
        self.devices = devices if devices is not None else []
        self.total_sign_calls = 0

    def enumerate(self, password=None):
        return [dict(d) for d in self.devices]

    def signtx(self, client, psbt):
        self.total_sign_calls += 1
        return super().signtx(client, psbt)


# ---------------------------------------------------------------------------
# (a) matcher vectors (pinned)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "verb"),
    [
        # the live-finding sign family (topic word + sign verb)
        ("sign with my hardware wallet", "sign"),
        ("let's sign with my hardware wallet", "sign"),
        ("sign it on my jade", "sign"),
        ("use the hardware wallet to sign", "sign"),
        ("sign the psbt with my bitbox", "sign"),
        # slice-A families keep their exact verbs and precedence
        ("can you see my hardware wallet?", "see"),
        ("unlock my jade and sign", "unlock"),
        ("connect my hardware wallet then sign", "connect"),
        ("I connected my hardware wallet", "connect"),
    ],
)
def test_matcher_accept_vectors(line: str, verb: str) -> None:
    assert _hardware_chat_verb(line) == verb


@pytest.mark.parametrize(
    "line",
    [
        "sign the transaction",  # verb, no hardware topic (slice-A pin)
        "with my hardware wallet",  # topic, no verb
        "sign with my bank app",  # sign, no topic
        "retry",
        "file",
        "export",
        "hardware",
        "",
    ],
)
def test_matcher_reject_vectors(line: str) -> None:
    assert _hardware_chat_verb(line) is None


@pytest.mark.parametrize(
    "line",
    [
        "file",
        "File.",
        "export",
        "the file",
        "export the file",
        "file export",
        "export it",
        "export please",
        "file please",
        "export instead",
        "file instead",
        "use the file",
        "sd card",
        "export the psbt",
    ],
)
def test_file_fallback_accept_vectors(line: str) -> None:
    assert _file_export_choice(line) is True


@pytest.mark.parametrize(
    "line",
    [
        "export my keys",  # never a bare accept
        "what is a psbt file",
        "file a bug report",
        "signed localwallet-signed-abcd1234.psbt.b64",  # the import word
        "export the file later",
        "retry",
        "sign with my hardware wallet",
        "",
    ],
)
def test_file_fallback_reject_vectors(line: str) -> None:
    assert _file_export_choice(line) is False


# ---------------------------------------------------------------------------
# (b) sign_probe — the bounded pre-sign check (state, lines), never raises
# ---------------------------------------------------------------------------


def _sign_probe(
    devices: list[dict] | None = None, **kwargs: Any
) -> tuple[str, tuple[str, ...]]:
    return make_signer(FakeCommands(devices=devices, **kwargs)).sign_probe()


def test_sign_probe_ready_opens_nothing() -> None:
    commands = FakeCommands(devices=[dict(DEVICE_WALLET)])
    state, lines = make_signer(commands).sign_probe()
    assert state == "ready" and lines == ()
    assert [c for c in commands.calls if c[0] == "get_client"] == []
    assert [c for c in commands.calls if c[0] == "signtx"] == []


def test_sign_probe_absent_uses_no_device_family() -> None:
    state, (line,) = _sign_probe(devices=[])
    assert state == "absent"
    assert line == (
        "No device found — plug in and unlock your device, then say 'retry'."
    )


def test_sign_probe_enumerate_error_maps_absent_never_raises() -> None:
    class DeviceConnectionError(Exception): ...

    state, lines = _sign_probe(enumerate_error=DeviceConnectionError("asleep"))
    assert state == "absent"
    assert "plug in" in lines[0].lower()


def test_sign_probe_locked_jade_is_guidance_only_never_unlocks() -> None:
    """Locked → the EXISTING slice-A guidance family, and the ONE thing
    slice C forbids on this path: no unlock attempt (no client opened)."""
    commands = FakeCommands(devices=[dict(JADE_LOCKED)])
    state, lines = make_signer(commands).sign_probe()
    assert state == "locked"
    assert lines[0] == "Found your jade — it's locked."
    assert "Recovery Phrase Login" in lines[1]
    assert [c for c in commands.calls if c[0] == "get_client"] == []
    assert [c for c in commands.calls if c[0] == "signtx"] == []


def test_sign_probe_lines_are_value_free() -> None:
    for devices in ([dict(JADE_LOCKED)], [dict(DEVICE_WALLET)], []):
        _state, lines = _sign_probe(devices=[dict(d) for d in devices])
        joined = "\n".join(lines).lower()
        assert "usb:" not in joined and "tty" not in joined and "hid:" not in joined


# ---------------------------------------------------------------------------
# (c) no pending flow → slice-A probe/report answers (report ONLY)
# ---------------------------------------------------------------------------


def test_sign_utterance_without_flow_reports_without_unlocking() -> None:
    probe = _FakeProbe()
    out, model_calls = _turn("sign with my hardware wallet", probe)
    assert out == ["Found your jade — it's unlocked and ready."]
    assert probe.calls == [False]  # no unlock driven — nothing to sign
    assert model_calls == []  # never the LLM ("I cannot…" stays unreachable)


# ---------------------------------------------------------------------------
# (d) repl-harness routing (full wiring, deterministic intercepts)
# ---------------------------------------------------------------------------


def _patch_devices(
    monkeypatch: pytest.MonkeyPatch, cmds: SwitchableCommands
) -> SwitchableCommands:
    monkeypatch.setattr(
        app,
        "HwiUsbSigner",
        lambda fp, account_path: RealHwiUsbSigner(
            fp, account_path, commands_module=cmds
        ),
    )
    return cmds


def _send_line() -> str:
    return f"send 60000 sats to {SEND_RECIPIENT}"


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    lines: list[str],
    plan: list[str],
    *,
    generate: Any = None,
    before_line: Any = None,
    signer: str = "file",
    flow: TxFlow | None = None,
) -> tuple[list[str], Any, Any, Path]:
    """One send-flow repl run over ``app.run`` with a tmp transfer dir;
    returns (outputs, flow, fake-model, transfer-path)."""
    addr0 = derive_fixture_addresses(1)[0]
    transfer = tmp_path / "transfer"
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    fake = (
        generate if generate is not None else FactsQuotingGenerate(plan)
    )
    tx_flow = flow if flow is not None else TxFlow()
    code, outputs, tx_flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        lines,
        plan,
        flow=tx_flow,
        generate=fake,
        before_line=before_line,
        extra_env={
            "LOCALWALLET_SIGNER": signer,
            "LOCALWALLET_SIGNER_DIR": str(transfer),
        },
    )
    assert code == 0
    return outputs, tx_flow, fake, transfer


def test_file_config_sign_with_hardware_device_present_signs_on_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """THE live finding, fixed: file-configured signer + "sign with my
    hardware wallet" + device PRESENT → the device path runs (probe-gated),
    NO PSBT file is written, and the value-free prefer-note rides the
    result (HW-004's conflict-guidance pattern). The utterance never
    reaches the model — the CREATED→CONFIRMED hop is the code-stamped
    dual-key confirm (the utterance carries the card's ask verb "sign")."""
    cmds = _patch_devices(monkeypatch, SwitchableCommands("", [dict(JADE_READY)]))
    outputs, flow, fake, transfer = _harness(
        monkeypatch, tmp_path, [_send_line(), "sign with my hardware wallet", "exit"],
        ["create"],
    )
    joined = "\n".join(outputs)
    assert "Signed and verified ✓" in joined
    # The prefer-note rides the RESULT (read by the web UI); the CLI success
    # path narrates only the signed line (HW-004 INFO: extra guidance is not
    # printed on the success path) — the result-level note is pinned in the
    # latch test below.
    assert "Exported to " not in joined  # never silently exported
    assert list(transfer.glob("localwallet-unsigned-*")) == []
    assert cmds.total_sign_calls == 1  # exactly one sign attempt, on the device
    assert len(fake.prompts) == 1  # ONLY the send turn consulted the model
    assert flow.state is TxFlowStatus.SIGNED


def test_file_config_sign_with_hardware_absent_asks_never_exports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cmds = _patch_devices(monkeypatch, SwitchableCommands("", []))
    outputs, flow, fake, transfer = _harness(
        monkeypatch, tmp_path, [_send_line(), "sign with my hardware wallet", "exit"],
        ["create"],
    )
    joined = "\n".join(outputs)
    assert _HW_SIGN_ASK in joined  # verbatim pinned wording (family prefix kept)
    assert "Exported to " not in joined
    assert list(transfer.glob("localwallet-unsigned-*")) == []
    assert cmds.total_sign_calls == 0
    assert len(fake.prompts) == 1
    assert flow.state is TxFlowStatus.CONFIRMED  # the ask leaves the handoff pending


def test_file_config_sign_with_hardware_locked_guidance_no_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cmds = _patch_devices(
        monkeypatch, SwitchableCommands("", [dict(JADE_LOCKED)])
    )
    outputs, flow, _fake, transfer = _harness(
        monkeypatch, tmp_path, [_send_line(), "sign with my hardware wallet", "exit"],
        ["create"],
    )
    joined = "\n".join(outputs)
    assert "Found your jade — it's locked." in joined  # existing family verbatim
    assert "Recovery Phrase Login" in joined
    assert "Exported to " not in joined
    assert list(transfer.glob("localwallet-unsigned-*")) == []
    assert cmds.total_sign_calls == 0  # no unlock attempt, no sign attempt
    assert flow.state is TxFlowStatus.CONFIRMED


def test_absent_ask_then_retry_reprobes_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The device shows up after the ask: the bare "retry" (latch honored
    inside the unchanged TCK-HW-002 intercept) RE-PROBES and signs on the
    device — the export path is never taken."""
    cmds = _patch_devices(monkeypatch, SwitchableCommands("", []))
    seen = {"n": 0}

    def before_line() -> None:
        seen["n"] += 1
        if seen["n"] >= 3:  # just before the "retry" line
            cmds.devices = [dict(JADE_READY)]

    outputs, flow, fake, transfer = _harness(
        monkeypatch,
        tmp_path,
        [
            _send_line(),
            "sign with my hardware wallet",  # absent → ask
            "retry",  # device now present → device sign
            "exit",
        ],
        ["create"],
        before_line=before_line,
    )
    joined = "\n".join(outputs)
    assert _HW_SIGN_ASK in joined
    assert "Signed and verified ✓" in joined
    assert cmds.total_sign_calls == 1
    assert list(transfer.glob("localwallet-unsigned-*")) == []
    assert len(fake.prompts) == 1  # ask, retry and sign were all model-free
    assert flow.state is TxFlowStatus.SIGNED


def test_absent_ask_then_export_word_runs_file_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The explicit fallback: after the ask, bare "export" runs the
    unchanged airgap export (and clears the device latch)."""
    _patch_devices(monkeypatch, SwitchableCommands("", []))
    outputs, flow, _fake, transfer = _harness(
        monkeypatch,
        tmp_path,
        [
            _send_line(),
            "sign with my hardware wallet",  # absent → ask
            "export",  # explicit fallback → the file path
            "exit",
        ],
        ["create"],
    )
    joined = "\n".join(outputs)
    assert _HW_SIGN_ASK in joined
    assert "Exported to " in joined
    assert len(list(transfer.glob("localwallet-unsigned-*.psbt.b64"))) == 1
    assert flow.state is TxFlowStatus.CONFIRMED


def test_hwi_config_absent_device_asks_offers_file_without_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """HWI-config parity: the gate-merged confirm→sign chain hits the
    pre-sign check; absent → the SAME file-offering ask (this config never
    wrote a file, and none is silently written)."""
    cmds = _patch_devices(monkeypatch, SwitchableCommands("", []))
    outputs, flow, fake, transfer = _harness(
        monkeypatch,
        tmp_path,
        [_send_line(), "yes please", "exit"],
        ["create", "confirm"],
        signer="hwi",
    )
    joined = "\n".join(outputs)
    assert _HW_SIGN_ASK in joined
    assert cmds.total_sign_calls == 0
    assert list(transfer.glob("localwallet-unsigned-*")) == []
    assert len(fake.prompts) == 2  # create + confirm; the chained sign is code's
    assert flow.state is TxFlowStatus.CONFIRMED


def test_hwi_config_device_present_chained_signs_on_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """HWI-config + device present: the chained handoff signs on the device
    with NO prefer note (the config already named the device — nothing to
    explain) and no export."""
    cmds = _patch_devices(monkeypatch, SwitchableCommands("", [dict(JADE_READY)]))
    outputs, flow, _fake, transfer = _harness(
        monkeypatch,
        tmp_path,
        [_send_line(), "yes please", "exit"],
        ["create", "confirm"],
        signer="hwi",
    )
    joined = "\n".join(outputs)
    assert "Signed and verified ✓" in joined
    assert _HW_DEVICE_PREFER_NOTE not in joined
    assert cmds.total_sign_calls == 1
    assert list(transfer.glob("localwallet-unsigned-*")) == []
    assert flow.state is TxFlowStatus.SIGNED


# ---------------------------------------------------------------------------
# (e) handler-level pins through the real dispatch table (flags + hygiene)
# ---------------------------------------------------------------------------


def _drive_to_confirmed(table: dict, session: Any) -> str:
    created = table[IntentName.CREATE_TX](validate_payload(_create_tx_envelope_json()))
    tx_ref = created["tx_ref"]
    session.gate_decision = GateDecision.CONFIRM
    table[IntentName.CONFIRM_TX](
        validate_payload(
            json.dumps({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": tx_ref}})
        )
    )
    return tx_ref


def _sign_env(tx_ref: str) -> Envelope:
    return Envelope(
        v=0, intent=IntentName.SIGN_TX, params=SignTxParams(tx_ref=tx_ref)
    )


def _file_selection(tmp_path: Path, fingerprint: str) -> Any:
    return app.SignerSelection(
        kind="file", dir_path=tmp_path / "transfer", fingerprint_hex=fingerprint
    )


def test_hwi_config_export_word_runs_file_branch_despite_device_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit fallback UNDER the hwi config: the one-shot export flag
    runs the unchanged file branch (device present and all — its sign is
    never touched), and the flag is one-shot: the NEXT dispatch is the
    device path again."""
    addr0 = derive_fixture_addresses(1)[0]
    transfer = tmp_path / "transfer"
    fingerprint = _fixture_parsed().hd_key.my_fingerprint.hex()
    cmds = SwitchableCommands(fingerprint, [dict(JADE_READY)])
    _patch_devices(monkeypatch, cmds)
    table, store, _wallet, client, _rec, flow, session = _build_send_table(
        lambda rec: _send_chain_handler(rec, utxos_by_addr={addr0: [SEND_UTXO]}),
        signer_selection=app.SignerSelection(
            kind="hwi", dir_path=transfer, fingerprint_hex=fingerprint
        ),
    )
    tx_ref = _drive_to_confirmed(table, session)
    session.file_sign_export_once = True
    result = table[IntentName.SIGN_TX](_sign_env(tx_ref))
    assert result["error"] == "signed_file_missing"  # the airgap export ran
    assert len(list(transfer.glob("localwallet-unsigned-*.psbt.b64"))) == 1
    assert cmds.total_sign_calls == 0
    assert session.file_sign_export_once is False  # consumed at the handler head
    assert flow.state is TxFlowStatus.CONFIRMED
    # The NEXT sign dispatch (flag unset) is the device path again:
    result = table[IntentName.SIGN_TX](_sign_env(tx_ref))
    assert result.get("status") == "signed"
    assert cmds.total_sign_calls == 1
    client.close()
    store.close()


def test_latch_survives_the_ask_and_clears_on_device_sign(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Handler latch hygiene: absent → the ASK leaves ``hw_sign_wanted``
    set (so "retry" re-probes); a device-sign success clears it; neither
    dispatch ever writes a transfer file under the latch."""
    addr0 = derive_fixture_addresses(1)[0]
    fingerprint = _fixture_parsed().hd_key.my_fingerprint.hex()
    cmds = SwitchableCommands(fingerprint, [])
    _patch_devices(monkeypatch, cmds)
    table, store, _wallet, client, _rec, _flow, session = _build_send_table(
        lambda rec: _send_chain_handler(rec, utxos_by_addr={addr0: [SEND_UTXO]}),
        signer_selection=_file_selection(tmp_path, fingerprint),
    )
    tx_ref = _drive_to_confirmed(table, session)
    session.hw_sign_wanted = True
    env = _sign_env(tx_ref)
    result = table[IntentName.SIGN_TX](env)
    assert result["error"] == "device_error" and result["guidance"] == _HW_SIGN_ASK
    assert session.hw_sign_wanted is True  # latched — "retry" re-probes
    assert list((tmp_path / "transfer").glob("localwallet-unsigned-*")) == []
    # The device shows up; a plain model-path sign dispatch (no re-stamp)
    # still honours the latch — device, not export:
    cmds.devices = [dict(JADE_READY)]
    result = table[IntentName.SIGN_TX](env)
    assert result.get("status") == "signed"
    assert result["guidance"] == _HW_DEVICE_PREFER_NOTE
    assert session.hw_sign_wanted is False  # cleared on success
    assert list((tmp_path / "transfer").glob("localwallet-unsigned-*")) == []
    client.close()
    store.close()


def test_file_config_plain_sign_never_builds_the_device_signer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """HW-004's byte-identical pin (slice-C shape): configured file signer
    and NO hardware utterance/flag → the file branch runs and
    ``HwiUsbSigner`` is never CONSTRUCTED (no probe, no enumerate)."""
    addr0 = derive_fixture_addresses(1)[0]
    transfer = tmp_path / "transfer"
    fingerprint = _fixture_parsed().hd_key.my_fingerprint.hex()

    def spy(fp: Any, account_path: Any) -> Any:
        raise AssertionError("HwiUsbSigner must not be built without a hardware word")

    monkeypatch.setattr(app, "HwiUsbSigner", spy)
    table, store, _wallet, client, _rec, _flow, session = _build_send_table(
        lambda rec: _send_chain_handler(rec, utxos_by_addr={addr0: [SEND_UTXO]}),
        signer_selection=app.SignerSelection(
            kind="file", dir_path=transfer, fingerprint_hex=fingerprint
        ),
    )
    tx_ref = _drive_to_confirmed(table, session)
    result = table[IntentName.SIGN_TX](_sign_env(tx_ref))
    assert result["error"] == "signed_file_missing"  # unchanged export path
    assert len(list(transfer.glob("localwallet-unsigned-*.psbt.b64"))) == 1
    client.close()
    store.close()


# ---------------------------------------------------------------------------
# (f) SECURITY REVIEW FIXES (TCK-HW-005 slice C): deny guard + latch upkeep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "don't sign with my hardware wallet",
        "dont sign with my hardware wallet",
        "do not sign with my jade",
        "cancel and sign on the hardware wallet",
        "no sign with the hardware wallet",
        "stop, sign with my jade",
        "reject signing on device",
        "never mind do not sign with hardware",
    ],
)
def test_deny_guard_vectors(line: str) -> None:
    assert _hardware_chat_verb(line) == "sign"  # would MATCH without the guard
    assert _hardware_sign_denied(line) is True


@pytest.mark.parametrize(
    "line",
    [
        "sign with my hardware wallet",
        "sign it on my jade",
        "note: sign with my device",  # "note" is NOT the negation "not"
        "please sign with my hardware wallet",
        "can you see my hardware wallet",  # non-sign verbs never hit the guard
    ],
)
def test_deny_guard_negative_vectors(line: str) -> None:
    assert _hardware_sign_denied(line) is False


@pytest.mark.parametrize(
    "line",
    ["don't sign with my hardware wallet", "cancel and sign on the hardware wallet"],
)
def test_denied_hardware_sign_never_stamps_confirm_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, line: str
) -> None:
    """Review MEDIUM: the hardware matcher is BROADER than ConfirmGate; a
    denied/negated sign utterance must NOT advance the flow. The line falls
    through to the ORDINARY path (model sees it honestly — prompt count 2),
    the flow stays CREATED (undo intact), nothing signs, nothing exports."""
    cmds = _patch_devices(monkeypatch, SwitchableCommands("", [dict(JADE_READY)]))
    outputs, flow, fake, transfer = _harness(
        monkeypatch,
        tmp_path,
        [_send_line(), line, "exit"],
        ["create", "respond", "respond"],
    )
    joined = "\n".join(outputs)
    assert flow.state is TxFlowStatus.CREATED  # NO code-stamped CONFIRM
    assert "Signed and verified" not in joined
    assert _HW_SIGN_ASK not in joined  # the sign routing never ran
    assert cmds.total_sign_calls == 0
    assert list(transfer.glob("localwallet-unsigned-*")) == []
    assert len(fake.prompts) == 2  # the model got the denied utterance itself
    assert f"user: {line}" in fake.prompts[-1]


def test_denied_hardware_sign_at_confirmed_does_not_dispatch_sign(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CONFIRMED parity: a denied hardware sign re-asks nothing — no second
    sign dispatch (the ask stays exactly one), no device touch, no export."""
    cmds = _patch_devices(monkeypatch, SwitchableCommands("", []))
    outputs, flow, fake, transfer = _harness(
        monkeypatch,
        tmp_path,
        [
            _send_line(),
            "sign with my hardware wallet",  # affirmative → ask (absent)
            "don't sign with my hardware wallet",  # denied → ordinary path
            "exit",
        ],
        ["create", "respond", "respond"],
    )
    joined = "\n".join(outputs)
    assert joined.count(_HW_SIGN_ASK) == 1  # only the affirmative turn asked
    assert "Signed and verified" not in joined
    assert cmds.total_sign_calls == 0
    assert list(transfer.glob("localwallet-unsigned-*")) == []
    assert len(fake.prompts) == 2  # create + the denied line (model, honestly)
    assert flow.state is TxFlowStatus.CONFIRMED


def test_failed_file_leg_keeps_the_device_latch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review LOW: the "file"/"export" intercept retires the device latch
    ONLY when the file leg ran. A FAILED export keeps the preference, so
    the next bare "retry" re-probes the device and signs there."""
    cmds = _patch_devices(monkeypatch, SwitchableCommands("", []))
    transfer = tmp_path / "transfer"
    flow = TxFlow()
    seen = {"n": 0}

    def before_line() -> None:
        seen["n"] += 1
        if seen["n"] == 3 and flow.confirmed is not None:
            # Just before the "file" line: plant a DIFFERENT-CONTENT
            # unsigned file → the real export refuses (SignerError →
            # export_failed), a genuine failed file leg.
            transfer.mkdir(parents=True, exist_ok=True)
            ref = flow.confirmed.tx_ref[:8]
            (transfer / f"localwallet-unsigned-{ref}.psbt.b64").write_text(
                "not-a-psbt-planted-by-test\n", encoding="utf-8"
            )
        if seen["n"] >= 4:
            cmds.devices = [dict(JADE_READY)]  # the device shows up

    outputs, flow, _fake, _transfer = _harness(
        monkeypatch,
        tmp_path,
        [
            _send_line(),
            "sign with my hardware wallet",  # absent → ASK, latch set
            "file",  # export attempt → FAILS (content refusal)
            "retry",  # latch survived → device sign succeeds
            "exit",
        ],
        ["create"],
        before_line=before_line,
        flow=flow,
    )
    joined = "\n".join(outputs)
    assert _HW_SIGN_ASK in joined
    assert "export_failed" in joined  # the file leg genuinely failed
    assert "Signed and verified ✓" in joined  # retry honored the LATCH
    assert cmds.total_sign_calls == 1
    assert flow.state is TxFlowStatus.SIGNED

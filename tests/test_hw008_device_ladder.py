"""TCK-HW-008: the defaulted-file device rung + spaced-brand routing + the
ambiguous-locked soft line (user live findings 2026-09-16).

Three debugger-verified defects, three fixes:

(a) bare "sign" at CONFIRMED exported a PSBT file even with an unlocked
    device attached: the selection ladder resolved from env/config ONLY
    and never enumerated. The FIX is rung-aware: the file kind from an
    EXPLICIT ``--signer``/``LOCALWALLET_SIGNER`` stays authoritative
    byte-identical (HW-004's matrix untouched — no probe, no enumerate,
    conflict guidance unchanged); the file kind as the ABSENCE default
    (``SignerSelection.kind_defaulted``) runs ONE bounded ``sign_probe``
    before the export and, on a SIGNABLE device, surfaces the
    device-offering ask instead — latching ``hw_sign_wanted`` so the
    ask's own answers ride the ESTABLISHED mechanics (bare "retry"
    re-probes and signs on the device; the closed "file"/"export"
    matcher runs the unchanged export; never the LLM).

(b) the spaced spelling "cold card" split into tokens that missed the
    single-token topic set, so "sign on my cold card" / "show it on my
    cold card" fell through to the LLM. The FIX is one shared
    phrase-collapse helper applied BEFORE topic matching in BOTH
    matchers; single-token forms are byte-identical.

(c) the definitive "Enter your PIN/passphrase" line fired when the
    sign-time probe read a transient locked (fingerprint unreadable NOW)
    after the SAME device had PROVEN signable earlier in the session
    (a ready sign probe, a served fingerprint-bound display, or a
    driven unlock that matched the wallet). The FIX swaps only that
    combination for the less-definitive check-the-device line (value-
    free, names 'retry'); a device never proven signable this session
    keeps the definitive family verbatim; no unlock is ever driven here
    (HW-001 discipline).

Device facts come from the SAME enumerate path as slices A/C; fakes are
imported from the existing suites, not re-implemented.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop
from localwallet.app import (
    _HW_CHECK_DEVICE,
    _HW_DEFAULT_FILE_ASK,
    _HW_SIGN_ASK,
    IntentName,
    SendSession,
    _hardware_chat_verb,
    _hardware_sign_denied,
    _show_on_device_request,
)
from localwallet.protocol import Envelope
from localwallet.signer.file import FilePsbtSigner
from localwallet.signer.hwi import (
    _MSG_LOCKED,
    _MSG_NO_DEVICES,
    DeviceError,
    ProbeReport,
)
from localwallet.tx.flow import TxFlow, TxFlowStatus
from tests.test_e2e_skeleton import (
    SEND_RECIPIENT,
    SEND_UTXO,
    FactsQuotingGenerate,
    _build_send_table,
    _fixture_parsed,
    _run_send_repl,
    _send_chain_handler,
    _simulate_device_sign,
    derive_fixture_addresses,
)
from tests.test_hw005_probe_unlock import _FakeProbe
from tests.test_hw005_slice_b_verify import _patch_device, _world
from tests.test_hw005_slice_c_sign import (
    SwitchableCommands,
    _drive_to_confirmed,
    _harness,
    _patch_devices,
    _send_line,
    _sign_env,
)
from tests.test_hw007_show_on_device import WORLD, Chain

#: The Coldcard shapes exactly as hwi enumerates them: an UNLOCKED card
#: reports its master fingerprint; a mid-operation/asleep one reports NO
#: fingerprint and (crucially for repro (c)) no needs_pin/needs_passphrase
#: flags — which steers the locked branch to the generic DEFINITIVE
#: "Enter your PIN/passphrase" line even though the card is unlocked.
COLD_READY = {
    "type": "coldcard",
    "path": "usb:fake",
    "model": "coldcard",
    "fingerprint": "40dbb192",
}
COLD_LOCKED = {
    "type": "coldcard",
    "path": "usb:fake",
    "model": "coldcard",
}

_FP = _fixture_parsed().hd_key.my_fingerprint.hex()


def _selection(
    tmp_path: Path,
    *,
    kind: str = "file",
    kind_defaulted: bool = False,
) -> app.SignerSelection:
    return app.SignerSelection(
        kind=kind,
        dir_path=tmp_path / "transfer",
        fingerprint_hex=_FP,
        kind_defaulted=kind_defaulted,
    )


def _exported(tmp_path: Path) -> list[Path]:
    # the canonical ADR-0014 name (the export writes .b64 + .sha256 +
    # binary siblings — count the canonical one)
    return list((tmp_path / "transfer").glob("localwallet-unsigned-*.psbt.b64"))


def _send_table(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, devices: list[dict]):
    cmds = _patch_devices(monkeypatch, SwitchableCommands(_FP, devices))
    table, store, _w, client, _rec, flow, session = _build_send_table(
        lambda rec: _send_chain_handler(
            rec, utxos_by_addr={derive_fixture_addresses(1)[0]: [SEND_UTXO]}
        ),
        signer_selection=_selection(tmp_path, kind_defaulted=True),
    )
    return cmds, table, store, client, flow, session


# =========================================================================
# (a) the defaulted-file rung — handler-level matrix
# =========================================================================


def test_defaulted_file_ready_device_asks_instead_of_exporting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """THE (a) repro: file signer as the ABSENCE default + a signable
    device at sign time → the device-offering ask, NEVER the silent
    export; ``hw_sign_wanted`` latches so the ask's own 'retry' answer
    lands on the device path (the established mechanics)."""
    cmds, table, store, client, flow, session = _send_table(
        monkeypatch, tmp_path, [dict(COLD_READY)]
    )
    tx_ref = _drive_to_confirmed(table, session)
    result = table[IntentName.SIGN_TX](_sign_env(tx_ref))
    assert result["error"] == "device_error"
    assert result["guidance"] == _HW_DEFAULT_FILE_ASK
    assert _exported(tmp_path) == []  # nothing silently exported
    assert session.hw_sign_wanted is True  # the ask armed the device path
    assert session.hw_signable_seen is True  # the (c) evidence stamp
    assert flow.state is TxFlowStatus.CONFIRMED  # the handoff stays pending
    assert cmds.total_sign_calls == 0  # the ask never signs on its own
    client.close()
    store.close()


def test_defaulted_file_ask_then_retry_signs_on_the_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ask's FIRST established answer: the second sign dispatch (what
    the bare "retry" intercept performs — same code path) honours the
    latch the ask armed: probe ready → the device signs, no export."""
    cmds, table, store, client, flow, session = _send_table(
        monkeypatch, tmp_path, [dict(COLD_READY)]
    )
    tx_ref = _drive_to_confirmed(table, session)
    first = table[IntentName.SIGN_TX](_sign_env(tx_ref))
    assert first["guidance"] == _HW_DEFAULT_FILE_ASK
    result = table[IntentName.SIGN_TX](_sign_env(tx_ref))  # the "retry" dispatch
    assert result.get("status") == "signed"
    assert cmds.total_sign_calls == 1
    assert _exported(tmp_path) == []
    assert session.hw_sign_wanted is False  # fulfilled — the latch retires
    assert flow.state is TxFlowStatus.SIGNED
    client.close()
    store.close()


def test_defaulted_file_ask_then_file_word_runs_the_plain_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ask's SECOND established answer: the one-shot "file"/"export"
    flag (the closed CONFIRMED-scoped matcher stamps it) runs the
    unchanged airgap export — the rung does NOT re-ask right after the
    user spoke 'file'."""
    _patch_devices(monkeypatch, SwitchableCommands(_FP, [dict(COLD_READY)]))
    table, store, _w, client, _rec, flow, session = _build_send_table(
        lambda rec: _send_chain_handler(
            rec, utxos_by_addr={derive_fixture_addresses(1)[0]: [SEND_UTXO]}
        ),
        signer_selection=_selection(tmp_path, kind_defaulted=True),
    )
    tx_ref = _drive_to_confirmed(table, session)
    assert table[IntentName.SIGN_TX](_sign_env(tx_ref))["guidance"] == (
        _HW_DEFAULT_FILE_ASK
    )
    session.file_sign_export_once = True  # what the matcher stamps
    result = table[IntentName.SIGN_TX](_sign_env(tx_ref))
    assert result["error"] == "signed_file_missing"
    assert len(_exported(tmp_path)) == 1  # the unchanged export path
    assert flow.state is TxFlowStatus.CONFIRMED
    client.close()
    store.close()


def test_defaulted_file_placed_signed_file_imports_without_asking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A mid-flight airgap handoff outranks the rung: the placed signed
    file imports (probe never consulted), never a re-ask."""
    cmds = _patch_devices(
        monkeypatch, SwitchableCommands(_FP, [dict(COLD_READY)])
    )
    table, store, _w, client, _rec, _flow, session = _build_send_table(
        lambda rec: _send_chain_handler(
            rec, utxos_by_addr={derive_fixture_addresses(1)[0]: [SEND_UTXO]}
        ),
        signer_selection=_selection(tmp_path, kind_defaulted=True),
    )
    tx_ref = _drive_to_confirmed(table, session)
    assert table[IntentName.SIGN_TX](_sign_env(tx_ref))["guidance"] == (
        _HW_DEFAULT_FILE_ASK
    )
    session.file_sign_export_once = True
    exported = table[IntentName.SIGN_TX](_sign_env(tx_ref))
    assert exported["error"] == "signed_file_missing"
    # The file-word intercept's latch upkeep (the _run_turn LOW fix): a
    # ran export retires the armed device wish — the next dispatch is
    # plain file-branch again.
    session.hw_sign_wanted = False
    # Place the signed file the way the device handoff leaves it, then sign:
    signer = FilePsbtSigner(Path(exported["unsigned_path"]).parent)
    signed_path = signer.signed_import_path(tx_ref)
    unsigned = validate_unsigned(exported["unsigned_path"])
    signed_path.write_text(_simulate_device_sign(unsigned) + "\n", encoding="utf-8")
    result = table[IntentName.SIGN_TX](_sign_env(tx_ref))
    assert result.get("status") == "signed"  # import won; no ask
    assert cmds.total_sign_calls == 0  # and the connected device stayed idle
    client.close()
    store.close()


def validate_unsigned(path: object) -> str:
    return Path(str(path)).read_text(encoding="utf-8").strip()


def test_defaulted_file_absent_device_exports_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """(a) matrix pin: default-file + NO device → the file export,
    byte-identical to today (the ask belongs to a device that exists)."""
    _patch_devices(monkeypatch, SwitchableCommands(_FP, []))
    table, store, _w, client, _rec, flow, session = _build_send_table(
        lambda rec: _send_chain_handler(
            rec, utxos_by_addr={derive_fixture_addresses(1)[0]: [SEND_UTXO]}
        ),
        signer_selection=_selection(tmp_path, kind_defaulted=True),
    )
    tx_ref = _drive_to_confirmed(table, session)
    result = table[IntentName.SIGN_TX](_sign_env(tx_ref))
    assert result["error"] == "signed_file_missing"
    assert "guidance" not in result
    assert len(_exported(tmp_path)) == 1
    assert session.hw_sign_wanted is False  # nothing armed the device path
    assert session.hw_signable_seen is False
    assert flow.state is TxFlowStatus.CONFIRMED
    client.close()
    store.close()


def test_defaulted_file_locked_device_exports_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only a SIGNABLE device diverts the default-file export: a present
    but locked card (never proven signable this session) keeps today's
    silent export — the locked/absent asks belong to the DEVICE path."""
    _patch_devices(monkeypatch, SwitchableCommands(_FP, [dict(COLD_LOCKED)]))
    table, store, _w, client, _rec, _flow, session = _build_send_table(
        lambda rec: _send_chain_handler(
            rec, utxos_by_addr={derive_fixture_addresses(1)[0]: [SEND_UTXO]}
        ),
        signer_selection=_selection(tmp_path, kind_defaulted=True),
    )
    tx_ref = _drive_to_confirmed(table, session)
    result = table[IntentName.SIGN_TX](_sign_env(tx_ref))
    assert result["error"] == "signed_file_missing"
    assert len(_exported(tmp_path)) == 1
    assert session.hw_sign_wanted is False
    client.close()
    store.close()


def test_explicit_file_with_ready_device_exports_and_never_probes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """(a) matrix pin — HW-004 BYTE-IDENTICAL: the EXPLICIT file rung
    (kind_defaulted False — the direct-construction default) keeps the
    silent export + the model-param conflict guidance, and the device
    signer is NEVER CONSTRUCTED (no probe, no enumerate)."""
    built: list[Any] = []

    def spy(fp: Any, account_path: Any) -> Any:
        built.append((fp, account_path))
        raise AssertionError("explicit file config must not enumerate")

    monkeypatch.setattr(app, "HwiUsbSigner", spy)
    table, store, _w, client, _rec, _flow, session = _build_send_table(
        lambda rec: _send_chain_handler(
            rec, utxos_by_addr={derive_fixture_addresses(1)[0]: [SEND_UTXO]}
        ),
        signer_selection=_selection(tmp_path, kind_defaulted=False),
    )
    tx_ref = _drive_to_confirmed(table, session)
    env = Envelope(
        v=0,
        intent=IntentName.SIGN_TX,
        params=app.SignTxParams(tx_ref=tx_ref, signer="hwi"),
    )
    result = table[IntentName.SIGN_TX](env)
    assert result["error"] == "signed_file_missing"
    assert result["guidance"] == "Using your configured signer (file)."
    assert len(_exported(tmp_path)) == 1
    assert built == []  # not even the rung's probe signer was constructed
    client.close()
    store.close()


# ---------------------------------------------------------------------------
# (a) the repro AT THE REPL: no LOCALWALLET_SIGNER at all (the ladder's
# absence default) + an attached, unlocked Coldcard + the chained handoff.
# ---------------------------------------------------------------------------


def _repl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    lines: list[str],
    devices: list[dict],
) -> tuple[list[str], Any, Any, Path, SwitchableCommands]:
    transfer = tmp_path / "transfer"
    cmds = _patch_devices(monkeypatch, SwitchableCommands("", devices))
    fake = FactsQuotingGenerate(["create", "confirm"])
    flow = TxFlow()
    code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        _send_chain_handler(
            [], utxos_by_addr={derive_fixture_addresses(1)[0]: [SEND_UTXO]}
        ),
        lines,
        ["create", "confirm"],
        flow=flow,
        generate=fake,
        extra_env={"LOCALWALLET_SIGNER_DIR": str(transfer)},
    )
    assert code == 0
    return outputs, flow, fake, transfer, cmds


def test_repl_bare_sign_with_attached_device_asks_never_exports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """THE filed repro (a): the user confirmed with a Coldcard attached
    and UNLOCKED and got a PSBT file path. Under the ladder default the
    chained handoff now surfaces the device ask — deterministically
    (create + confirm are the only model turns; the ask consults no
    model)."""
    outputs, flow, fake, transfer, cmds = _repl(
        monkeypatch,
        tmp_path,
        [f"send 60000 sats to {SEND_RECIPIENT}", "yes please", "exit"],
        [dict(COLD_READY)],
    )
    joined = "\n".join(outputs)
    assert _HW_DEFAULT_FILE_ASK in joined
    assert "Exported to " not in joined
    assert list(transfer.glob("localwallet-unsigned-*")) == []
    assert len(fake.prompts) == 2  # create + confirm; the chained ask is code's
    assert cmds.total_sign_calls == 0
    assert flow.state is TxFlowStatus.CONFIRMED


def test_repl_ask_then_retry_signs_on_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ask teaches 'retry'; the bare retry intercept (unchanged
    machinery) honours the armed latch and signs on the device — model-
    free, export-free."""
    outputs, flow, fake, transfer, cmds = _repl(
        monkeypatch,
        tmp_path,
        [f"send 60000 sats to {SEND_RECIPIENT}", "yes please", "retry", "exit"],
        [dict(COLD_READY)],
    )
    joined = "\n".join(outputs)
    assert _HW_DEFAULT_FILE_ASK in joined
    assert "Signed and verified ✓" in joined
    assert cmds.total_sign_calls == 1
    assert list(transfer.glob("localwallet-unsigned-*")) == []
    assert len(fake.prompts) == 2  # the retry dispatch was model-free too
    assert flow.state is TxFlowStatus.SIGNED


def test_repl_defaulted_file_no_device_exports_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The no-device default lifecycle stays the lifecycle: attach NO
    device and the chained handoff exports exactly as before."""
    outputs, flow, _fake, transfer, cmds = _repl(
        monkeypatch,
        tmp_path,
        [f"send 60000 sats to {SEND_RECIPIENT}", "yes please", "exit"],
        [],
    )
    joined = "\n".join(outputs)
    assert _HW_DEFAULT_FILE_ASK not in joined
    assert "Exported to " in joined
    assert len(list(transfer.glob("localwallet-unsigned-*.psbt.b64"))) == 1
    assert cmds.total_sign_calls == 0
    assert flow.state is TxFlowStatus.CONFIRMED


# =========================================================================
# (b) spaced-brand collapse — the shared helper in BOTH matchers
# =========================================================================


@pytest.mark.parametrize(
    ("line", "verb"),
    [
        # THE reported miss: "cold card" (spaced) fell through to the LLM.
        ("sign on my cold card", "sign"),
        ("sign with my cold card", "sign"),
        ("send the tx to my cold card to sign", "sign"),
        # the whole slice-A family rides the same topic set — the
        # collapse applies to ALL of its verbs, not just sign.
        ("unlock my cold card", "unlock"),
        ("can you see my cold card?", "see"),
        ("I connected my cold card", "connect"),
        # single-token forms: byte-identical routing
        ("sign on my coldcard", "sign"),
        ("unlock my coldcard", "unlock"),
        # punctuation falls off the bigram halves before the match
        ("sign it on my cold card.", "sign"),
        # other classes keep their shapes untouched
        ("sign with my hardware wallet", "sign"),
        ("sign it on my jade", "sign"),
    ],
)
def test_spaced_brand_verb_vectors(line: str, verb: str) -> None:
    assert _hardware_chat_verb(line) == verb


@pytest.mark.parametrize(
    "line",
    [
        "sign on my cold drink",  # the bigram halves must be adjacent
        "sign on my card cold",  # ... and in order
        "sign with my cold feet",
        "cold card",  # topic only, no verb
        "sign the transaction",  # verb only, no topic
    ],
)
def test_spaced_brand_verb_rejects(line: str) -> None:
    assert _hardware_chat_verb(line) is None


@pytest.mark.parametrize(
    ("line", "referent"),
    [
        # THE reported miss on the display side, all three referent shapes.
        ("show it on my cold card", "pronoun"),
        ("show the address on my cold card", "pronoun"),
        ("show #3 on my cold card", "number"),
        ("can you show it on my cold card?", "pronoun"),
        ("show it on my coldcard", "pronoun"),  # single-token unchanged
    ],
)
def test_spaced_brand_show_vectors(line: str, referent: str) -> None:
    request = _show_on_device_request(line)
    assert request is not None and request[0] == referent


def test_spaced_brand_deny_rule_untouched() -> None:
    # the slice-C review-MEDIUM deny guard survives the collapse.
    assert _show_on_device_request("don't show it on my cold card") is None
    assert _hardware_sign_denied("don't sign on my cold card") is True
    assert _hardware_chat_verb("don't sign on my cold card") == "sign"


def test_repl_spaced_brand_sign_routes_to_the_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The routed proof at full wiring: "sign on my cold card" (spaced)
    under the EXPLICIT file config latches the device path (the slice-C
    machinery) and signs on the card — the utterance never reaches the
    LLM, nothing exports."""
    cmds = _patch_devices(
        monkeypatch, SwitchableCommands("", [dict(COLD_READY)])
    )
    outputs, flow, fake, _transfer = _harness(
        monkeypatch,
        tmp_path,
        [_send_line(), "sign on my cold card", "exit"],
        ["create"],
    )
    joined = "\n".join(outputs)
    assert "Signed and verified ✓" in joined
    assert cmds.total_sign_calls == 1
    assert list((_tmp_transfer(tmp_path)).glob("localwallet-unsigned-*")) == []
    assert len(fake.prompts) == 1  # the hardware words never reached the LLM
    assert flow.state is TxFlowStatus.SIGNED


def _tmp_transfer(tmp_path: Path) -> Path:
    # _harness writes into tmp_path/"transfer" (its own default).
    return tmp_path / "transfer"


def test_repl_spaced_brand_show_displays(monkeypatch: pytest.MonkeyPatch) -> None:
    """The display side at full wiring: with the session's shown address,
    "show it on my cold card" reaches the /verifyaddress handler through
    the production turn chain (HW-007 mechanics, spaced brand) — model-
    free."""
    c = Chain(monkeypatch)
    c.session.last_shown_own = (WORLD[0], 0, 0)
    out = c.turn("show it on my cold card")
    assert c.prompts == []  # the matched turn never consulted the model
    assert out and any("shown" in line.lower() or "#" in line for line in out)
    assert c.display_coords() == [(0, 0)]  # the device was asked, once


# =========================================================================
# (c) ambiguous locked — soft line after session proof, definitive without
# =========================================================================


class _ProbeSigner:
    """Duck-typed device signer with a CONTROLLABLE sign_probe (the exact
    surface the handler consumes); sign_unsigned always fails with the
    code-owned DeviceError guidance so a ready read can stamp the session
    flag without a full device handshake."""

    name = "hwi"

    def __init__(self, state: str, lines: tuple[str, ...] = ()) -> None:
        self.state = state
        self.lines = lines
        self.probe_calls = 0
        self.sign_calls = 0

    def sign_probe(self) -> tuple[str, tuple[str, ...]]:
        self.probe_calls += 1
        return self.state, self.lines

    def sign_unsigned(self, psbt_base64: str) -> Any:
        self.sign_calls += 1
        raise DeviceError("The request was canceled on your device — say 'retry'.")


_LOCKED_LINES = ("Found your coldcard — it's locked.", _MSG_LOCKED)


def _hwi_table(tmp_path: Path, signer: _ProbeSigner):
    return _build_send_table(
        lambda rec: _send_chain_handler(
            rec, utxos_by_addr={derive_fixture_addresses(1)[0]: [SEND_UTXO]}
        ),
        signer_selection=_selection(tmp_path, kind="hwi"),
        signer=signer,
    )


def test_locked_without_prior_proof_keeps_the_definitive_line(
    tmp_path: Path,
) -> None:
    """A device NEVER proven signable this session: the locked branch
    returns the existing guidance family verbatim (the ticket's
    byte-identical pin)."""
    fake = _ProbeSigner("locked", _LOCKED_LINES)
    table, store, _w, client, _rec, _, session = _hwi_table(tmp_path, fake)
    tx_ref = _drive_to_confirmed(table, session)
    result = table[IntentName.SIGN_TX](_sign_env(tx_ref))
    assert result["error"] == "device_error"
    assert result["guidance"] == "\n".join(_LOCKED_LINES)
    assert _MSG_LOCKED in result["guidance"]  # the definitive PIN claim stays
    assert fake.probe_calls == 1
    assert fake.sign_calls == 0  # no sign attempt, no auto-driven unlock
    client.close()
    store.close()


def test_locked_after_ready_probe_gets_the_soft_line(tmp_path: Path) -> None:
    """Same session, same handler: a READY probe (which stamps the session
    flag) whose sign then FAILED at the device, followed by a locked read
    → the less-definitive check-the-device line. This is exactly the
    repro's shape (the locked read is a transient blip on an UNLOCKED
    card)."""
    fake = _ProbeSigner("ready")
    table, store, _w, client, _rec, _, session = _hwi_table(tmp_path, fake)
    tx_ref = _drive_to_confirmed(table, session)
    first = table[IntentName.SIGN_TX](_sign_env(tx_ref))
    assert first["error"] == "device_error"  # the sign itself failed
    assert session.hw_signable_seen is True  # the ready probe stamped it
    fake.state, fake.lines = "locked", _LOCKED_LINES
    second = table[IntentName.SIGN_TX](_sign_env(tx_ref))
    assert second["guidance"] == _HW_CHECK_DEVICE
    assert "PIN" not in second["guidance"]  # the definitive claim is gone
    assert "retry" in second["guidance"]  # names the established answer
    client.close()
    store.close()


def test_locked_after_served_display_gets_the_soft_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """THE debugger's exact sequence: the user verified an address ON the
    device (the fingerprint-bound display was SERVED → the card proved
    signable this session), then the sign-time probe read a transient
    locked → the soft line, NOT 'Enter your PIN/passphrase'."""
    store, wd, _, _ = _world()
    _patch_device(monkeypatch, lambda *_a: "bc1qSAMEDERIVED")
    table, store2, _w, client, _rec, _, session = _hwi_table(
        tmp_path, _ProbeSigner("locked", _LOCKED_LINES)
    )
    app._verify_own_address(
        "0 0",  # _world issues index 0 (the way /address leaves it)
        store,
        app.SignerSelection(kind="hwi", dir_path=tmp_path / "x", fingerprint_hex=_FP),
        wd.parsed,
        lambda _t: None,
        session=session,  # the SAME session the sign handler reads
    )
    assert session.hw_signable_seen is True  # the served display stamped it
    tx_ref = _drive_to_confirmed(table, session)
    result = table[IntentName.SIGN_TX](_sign_env(tx_ref))
    assert result["guidance"] == _HW_CHECK_DEVICE
    client.close()
    store.close()
    store2.close()


def test_display_failure_never_stamps(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stamp rides a SERVED display only — an absent/locked device
    that raised stays WITHOUT the session proof (the definitive line
    stays definitive)."""
    store, wd, _wallet, selection = _world()
    _patch_device(monkeypatch, DeviceError("No device found"))
    session = SendSession()
    app._verify_own_address(
        "0 0", store, selection, wd.parsed, lambda _t: None, session=session
    )
    assert session.hw_signable_seen is False
    store.close()


def test_unlock_that_matched_the_wallet_arms_the_soft_line() -> None:
    """The HW-006 probe-time state feeds the same flag: a driven unlock
    whose opened client SERVED this wallet's account key
    (wallet_match=True) proves signable; the spaced brand rides the
    matcher end-to-end to reach it."""

    class _MatchProbe(_FakeProbe):
        def probe_and_report(self, *, attempt_unlock: bool) -> ProbeReport:
            self.calls.append(attempt_unlock)
            return ProbeReport(("Your device unlocked and is ready.",), True)

    loop = AgentLoop(
        lambda prompt, grammar: "",
        {IntentName.RESPOND: app._respond_handler},
    )
    session = SendSession()
    handled = app._run_hardware_chat(
        loop,
        TxFlow(),
        session,
        "unlock my cold card",
        lambda _t: None,
        table={},
        hwi=_MatchProbe(),
    )
    assert handled is True
    assert session.hw_signable_seen is True


# =========================================================================
# Ladder pins: ABSENCE default vs EXPLICIT rung is decided at the ladder
# =========================================================================


def test_ladder_env_absence_arms_the_rung(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_dispatch_table's env fallback with LOCALWALLET_SIGNER
    ABSENT: the selection lands on kind_defaulted=True and the ready
    device turns the export into the ask."""
    monkeypatch.delenv("LOCALWALLET_SIGNER", raising=False)
    _patch_devices(monkeypatch, SwitchableCommands(_FP, [dict(COLD_READY)]))
    table, store, _w, client, _rec, _, session = _build_send_table(
        lambda rec: _send_chain_handler(
            rec, utxos_by_addr={derive_fixture_addresses(1)[0]: [SEND_UTXO]}
        ),
    )
    tx_ref = _drive_to_confirmed(table, session)
    result = table[IntentName.SIGN_TX](_sign_env(tx_ref))
    assert result["guidance"] == _HW_DEFAULT_FILE_ASK  # the rung is live
    client.close()
    store.close()


def test_ladder_env_value_is_explicit_and_never_probes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """LOCALWALLET_SIGNER=file is an EXPLICIT rung: HW-004 byte-identical
    export, and the device signer is never even constructed."""
    monkeypatch.setenv("LOCALWALLET_SIGNER", "file")
    monkeypatch.setenv("LOCALWALLET_SIGNER_DIR", str(tmp_path / "transfer"))

    def spy(fp: Any, account_path: Any) -> Any:
        raise AssertionError("explicit file must not construct the device signer")

    table, store, _w, client, _rec, _, session = _build_send_table(
        lambda rec: _send_chain_handler(
            rec, utxos_by_addr={derive_fixture_addresses(1)[0]: [SEND_UTXO]}
        ),
    )
    _patch_devices(monkeypatch, SwitchableCommands(_FP, [dict(COLD_READY)]))
    monkeypatch.setattr(app, "HwiUsbSigner", spy)
    tx_ref = _drive_to_confirmed(table, session)
    result = table[IntentName.SIGN_TX](_sign_env(tx_ref))
    assert result["error"] == "signed_file_missing"
    assert len(_exported(tmp_path)) == 1
    client.close()
    store.close()


def test_ask_text_family_honours_the_pins() -> None:
    """The slice-C absent ask keeps its exact bytes (PHASE3-AC-3 family
    prefix); the new default-file line is a DISTINCT constant — it
    addresses a FOUND device, "No device found" would be a lie. The soft
    line never claims a PIN and names the established 'retry'."""
    assert _HW_SIGN_ASK.startswith("No device found — plug in")
    assert _HW_DEFAULT_FILE_ASK != _HW_SIGN_ASK
    assert _MSG_NO_DEVICES not in _HW_DEFAULT_FILE_ASK
    assert _HW_CHECK_DEVICE != _MSG_LOCKED
    assert "retry" in _HW_CHECK_DEVICE
    assert "PIN" not in _HW_CHECK_DEVICE
    assert "device" in _HW_DEFAULT_FILE_ASK and "file" in _HW_DEFAULT_FILE_ASK

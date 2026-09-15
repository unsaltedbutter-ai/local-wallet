"""TCK-HW-005 SLICE B (ENGINE): show-my-address on the device.

The user's known-broken conversation — "show bc1q… on my hardware wallet" —
answered by the DEVICE displaying its own derivation for on-device
verification (read-only: nothing here signs or moves money).

Pinned contracts:
* D2 — the clicked address resolves through the store's OWN derivation
  state (issued/scanned index, or the /receive preview at next_index on
  branch 0); an index the wallet has never shown refuses honestly,
  value-free, WITHOUT ever touching a device;
* D3 — ``/verifyaddress <branch> <index>`` params are code-parsed
  (exactly two digit tokens) and bounds-checked against the store's
  derivation state; the slash form exists because the quick-action
  intent map is empty-params-only;
* D4 — the device signer is rebuilt per attempt EXACTLY like the lazy
  sign build (account fingerprint + descriptor account path), and the
  display call runs the read-only route (sign_unsigned never runs);
* narration quotes the address VERBATIM and says "confirm it matches on
  the device screen"; a device answer that differs from the wallet's own
  derivation is an explicit WARNING quoting both (each from its own
  source); no device → the EXISTING friendly guidance family (never an
  error page);
* D6 — an additive typed ``own_address`` event (JSON: address verbatim +
  branch + index) rides alongside the receive/new_address narration, so
  the static half can place the button on OUR addresses only;
* D7 — additive value-free ``signer_kind`` enum NAME in the /state
  snapshot (the backend_kind pattern).
"""

from __future__ import annotations

import json
import queue
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest

from localwallet import app
from localwallet.agent.loop import AgentLoop, AgentTurnResult, AgentTurnStatus
from localwallet.app import (
    _ACTION_UNAVAILABLE,
    _LABEL_NO_WALLET,
    _VERIFY_MISMATCH_TEMPLATE,
    _VERIFY_NOT_SHOWN,
    _VERIFY_USAGE,
    EVENT_OWN_ADDRESS,
    EVENT_TEXT,
    EVENT_TURN_END,
    EVENT_USER_TEXT,
    VERIFY_ADDRESS_COMMAND,
    EventEmitter,
)
from localwallet.protocol import Envelope, IntentName, NewAddressParams
from localwallet.signer.hwi import (
    DeviceAbsentError,
    DeviceError,
    DeviceLockedError,
    DeviceMismatchError,
    SignerError,
)
from localwallet.store import AddressRecord, Store
from localwallet.tx.flow import TxFlow
from localwallet.wallet.descriptor import WalletDescriptor
from tests.test_e2e_skeleton import ZPUB, derive_fixture_addresses

ADDRS: list[str] = derive_fixture_addresses(6)
ACCOUNT_PATH = "m/84'/0'/0'"

# Guidance families asserted BY CONTENT (the "zero new device strings"
# rule): the app must print hwi's OWN lines, never its own invention.
_MSG_PLUG_IN = "plug in"
_MSG_PIN_FAMILY = "PIN"


class _FakeDevice:
    """Stands in for the hwi layer at the app seam. Records every
    construction (D4 mirror of the lazy sign build) and every
    display_address call; ``behavior`` decides the device's answer or
    the failure family it raises. sign_unsigned EXPLODES if ever called
    (the display path must never touch money)."""

    instances: ClassVar[list[_FakeDevice]] = []

    def __init__(
        self,
        fingerprint_hex: str,
        account_path: str,
        behavior: Callable[[str, str, int, int], str] | Exception,
    ) -> None:
        self.ctor_args = (fingerprint_hex, account_path)
        self.behavior = behavior
        self.display_calls: list[tuple[str, str, int, int]] = []
        _FakeDevice.instances.append(self)

    def display_address(
        self, account_pubkey_hex: str, script_type: str, branch: int, index: int
    ) -> str:
        self.display_calls.append((account_pubkey_hex, script_type, branch, index))
        if isinstance(self.behavior, Exception):
            raise self.behavior
        return self.behavior(account_pubkey_hex, script_type, branch, index)

    def sign_unsigned(self, psbt_base64: str) -> Any:  # pragma: no cover — pin
        raise AssertionError("the display path must NEVER sign")


def _patch_device(
    monkeypatch: pytest.MonkeyPatch,
    behavior: Callable[[str, str, int, int], str] | Exception,
) -> type[_FakeDevice]:
    _FakeDevice.instances = []

    def factory(fingerprint_hex: str, account_path: str) -> _FakeDevice:
        return _FakeDevice(fingerprint_hex, account_path, behavior)

    monkeypatch.setattr(app, "HwiUsbSigner", factory)
    return _FakeDevice


def _world() -> tuple[Store, WalletDescriptor, Any, app.SignerSelection]:
    """Provisioned fixture wallet (in-memory store, active wallet, one
    ALLOCATED address at (0,0) the way /address leaves it) + the hwi-kind
    signer selection the pump would carry."""
    store = Store.memory()
    wd = WalletDescriptor.from_key(ZPUB)
    wallet = store.create_wallet("default", wd.descriptor)
    store.set_active_wallet(wallet.id)
    table = app.build_dispatch_table(
        store, wallet, wd.parsed, client=None, scan_fn=lambda: None
    )
    envelope = Envelope(v=0, intent=IntentName.NEW_ADDRESS, params=NewAddressParams())
    assert "error" not in table[IntentName.NEW_ADDRESS](envelope)  # index 0 issued
    selection = app.SignerSelection(
        kind="hwi",
        dir_path=Path("psbt-transfer"),
        fingerprint_hex=wd.parsed.hd_key.my_fingerprint.hex(),
    )
    return store, wd, wallet, selection


def _verify(
    args: str,
    store: Store | None,
    selection: app.SignerSelection | None,
    parsed: Any,
) -> list[str]:
    out: list[str] = []
    app._verify_own_address(args, store, selection, parsed, out.append)
    return out


def _account_pubkey_hex(wd: WalletDescriptor) -> str:
    return wd.parsed.hd_key.key.serialize().hex()


# =========================================================================
# D3 — code-parsed params, bounds-checked vs the store's derivation state
# =========================================================================


@pytest.mark.parametrize(
    "args",
    [
        "",               # bare command
        "0",              # one param
        "0 1 2",          # three params
        "a b",            # not digits
        "² 0",            # superscript two: isdecimal-refused, not int-crash
        "-1 0",           # negative (isdigit refuses)
        "0 1.5",          # float
        "0 0x1",          # hex
        "2 0",            # branch not a wallet branch
        "99 0",           # same, larger
    ],
)
def test_params_malformed_or_out_of_shape_refuse_without_device(
    monkeypatch: pytest.MonkeyPatch, args: str
) -> None:
    """The device is NEVER constructed for a malformed command — parse and
    bounds first, hardware second (no phantom prompts)."""
    cls = _patch_device(monkeypatch, lambda *a: "bc1qx")
    store, wd, _wallet, selection = _world()
    lines = _verify(args, store, selection, wd.parsed)
    assert lines == [_VERIFY_USAGE]
    assert cls.instances == []


def test_index_beyond_derivation_state_refuses_value_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(D2) An address this wallet has never SHOWN (index past the live
    next_index — including an astronomically large one) refuses honestly,
    without touching a device and without echoing any address."""
    cls = _patch_device(monkeypatch, lambda *a: "bc1qx")
    store, wd, _wallet, selection = _world()
    next_index = store.get_derivation(_wallet_id(store), 0).next_index
    for args in (f"0 {next_index + 1}", f"0 {next_index * 10**30 + 7}"):
        lines = _verify(args, store, selection, wd.parsed)
        assert lines == [_VERIFY_NOT_SHOWN]
    assert cls.instances == []


def test_preview_index_allowed_on_receive_branch_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/receive SHOWS branch 0 at next_index (the preview) — its button
    must work. The change branch's unissued next_index is never shown,
    so it stays refused."""
    _patch_device(monkeypatch, lambda *a: "bc1qSAMEDERIVED")
    store, wd, _wallet, selection = _world()
    wallet_id = _wallet_id(store)
    preview = store.get_derivation(wallet_id, 0).next_index
    lines = _verify(f"0 {preview}", store, selection, wd.parsed)
    assert lines and "bc1qSAMEDERIVED" in lines[0]  # the device answer ran
    lines = _verify(f"1 {store.get_derivation(wallet_id, 1).next_index}", store, selection, wd.parsed)
    assert lines == [_VERIFY_NOT_SHOWN]


def test_store_row_conflict_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the store row for the believed address sits at a DIFFERENT
    branch/index, the data is inconsistent — refuse instead of displaying
    a disputed address (and never touch the device)."""
    cls = _patch_device(monkeypatch, lambda *a: "bc1qx")
    store, wd, _wallet, selection = _world()
    wallet_id = _wallet_id(store)
    # ADDRS[1] (branch 0, index 1) planted ALSO at branch 1 index 0 — a
    # foreign row for the very address (0,1) derives to.
    store.upsert_batch(
        [AddressRecord(wallet_id, 1, 0, ADDRS[1], "p2wpkh", "unused")]
    )
    store.bump_next_index(wallet_id, 0)  # (0,1) inside the shown bound
    lines = _verify("0 1", store, selection, wd.parsed)
    assert lines == [_VERIFY_NOT_SHOWN]
    assert cls.instances == []


def test_missing_wiring_and_wallet_answer_honestly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cls = _patch_device(monkeypatch, lambda *a: "bc1qx")
    # No store (bare pump): the store-unavailable family.
    lines = _verify("0 0", None, None, None)
    assert lines == [app._LABEL_STORE_UNAVAILABLE]
    # Store present, no signer wiring (placeholder pump): unavailable.
    store = Store.memory()
    lines = _verify("0 0", store, None, None)
    assert lines == [_ACTION_UNAVAILABLE]
    # Wiring present, no wallet: the existing no-wallet line.
    wd = WalletDescriptor.from_key(ZPUB)
    selection = app.SignerSelection(
        kind="hwi", dir_path=Path("x"), fingerprint_hex="00000000"
    )
    lines = _verify("0 0", store, selection, wd.parsed)
    assert lines == [_LABEL_NO_WALLET]
    assert cls.instances == []


def _wallet_id(store: Store) -> int:
    wallet = store.get_active_wallet()
    assert wallet is not None
    return wallet.id


# =========================================================================
# D4 + narration — the device route mirrors the lazy sign build
# =========================================================================


def test_happy_path_mirrors_lazy_sign_build_and_quotes_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_device(monkeypatch, lambda *a: ADDRS[0])
    store, wd, _wallet, selection = _world()
    lines = _verify("0 0", store, selection, wd.parsed)
    device = _FakeDevice.instances[0]
    # (D4) construction args == the sign_tx handler's lazy build:
    assert device.ctor_args == (selection.fingerprint_hex, ACCOUNT_PATH)
    # the clicked address's OWN coordinates, keyed to the store descriptor:
    assert device.display_calls == [(_account_pubkey_hex(wd), "p2wpkh", 0, 0)]
    # narration: address verbatim + the honest device-prompt line + the
    # stable registry number (this address was already shown → #1).
    assert lines == [
        (
            f"Your device says it is showing address #1 {ADDRS[0]} — "
            "confirm it matches on the device screen."
        )
    ]


def test_device_mismatch_is_an_explicit_warning_quoting_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Device-reported vs wallet-believed — each verbatim from its OWN
    source (device answer / engine derivation), the warning outranks the
    calm line. The model authored neither."""
    _patch_device(monkeypatch, lambda *a: "bc1qEVILLYETREALLOOKING")
    store, wd, _wallet, selection = _world()
    lines = _verify("0 0", store, selection, wd.parsed)
    assert lines == [
        _VERIFY_MISMATCH_TEMPLATE.format(
            device_address="bc1qEVILLYETREALLOOKING",
            branch=0,
            index=0,
            wallet_address=ADDRS[0],
        )
    ]


@pytest.mark.parametrize(
    ("failure", "needle"),
    [
        # friendly "plug in your hardware wallet" line — NEVER an error
        (DeviceAbsentError("No device found — plug in and unlock your device, then say 'retry'."), _MSG_PLUG_IN),
        (DeviceLockedError("Enter your PIN/passphrase on the device, then say 'retry'."), _MSG_PIN_FAMILY),
        (DeviceMismatchError("No device with this wallet's fingerprint was found"), "fingerprint"),
        (DeviceError("Your device is busy — finish what's on its screen, then say 'retry'."), "busy"),
    ],
)
def test_device_failures_print_the_existing_guidance_family(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, needle: str
) -> None:
    """The app prints hwilib-layer guidance VERBATIM (zero new device
    strings) as a plain narration line — value-free, never an error."""
    _patch_device(monkeypatch, failure)
    store, wd, _wallet, selection = _world()
    lines = _verify("0 0", store, selection, wd.parsed)
    assert len(lines) == 1
    assert needle.lower() in lines[0].lower()
    assert "error" not in lines[0].lower()


def test_internal_signer_errors_never_echo_their_wording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SignerError (bad local shape — internal after the bounds checks)
    answers with the honest unavailable line, never the signer's text."""
    _patch_device(monkeypatch, SignerError("internal detail leakers: 12345"))
    store, wd, _wallet, selection = _world()
    lines = _verify("0 0", store, selection, wd.parsed)
    assert lines == [_ACTION_UNAVAILABLE]


def test_display_lines_are_value_free_about_key_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fingerprint, no extended key, no descriptor ever rides the
    narration (addresses are the VERBATIM display exception; nothing else
    about the wallet's material is)."""
    _patch_device(monkeypatch, lambda *a: ADDRS[0])
    store, wd, _wallet, selection = _world()
    lines = _verify("0 0", store, selection, wd.parsed)
    joined = "\n".join(lines)
    assert selection.fingerprint_hex not in joined
    assert "zpub" not in joined
    assert _account_pubkey_hex(wd) not in joined


# =========================================================================
# pump seam (D3/D4 wiring through the quick-action channel)
# =========================================================================


def _pump_lines(
    commands: list[Any], **pump_kw: Any
) -> list[tuple[str, str]]:
    events: list[Any] = []
    emitter = EventEmitter(events.append)
    q: queue.Queue[Any] = queue.Queue()
    for command in commands:
        q.put(command)
    app._pump(
        AgentLoop(app.stub_generate, {}),
        emitter.text,
        q,
        flow=TxFlow(),
        session=app.SendSession(),
        table={},
        emitter=emitter,
        **pump_kw,
    )
    return [(e.kind, e.payload) for e in events]


def test_pump_routes_the_param_carrying_quick_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/verifyaddress rides the SAME string-command choke point as the
    bare quick actions (so web /action buttons, quickbar and CLI share it
    — and its user_text echo + turn_end marker come free)."""
    _patch_device(monkeypatch, lambda *a: ADDRS[0])
    store, wd, _wallet, selection = _world()
    events = _pump_lines(
        [f"{VERIFY_ADDRESS_COMMAND} 0 0", app.QUIT],
        store=store,
        signer_selection=selection,
        parsed=wd.parsed,
    )
    assert events == [
        (EVENT_USER_TEXT, f"{VERIFY_ADDRESS_COMMAND} 0 0"),
        (
            EVENT_TEXT,
            (
                f"Your device says it is showing address #1 {ADDRS[0]} — "
                "confirm it matches on the device screen."
            ),
        ),
        (EVENT_TURN_END, ""),
    ]


def test_pump_quick_gate_survives_empty_and_near_miss_lines() -> None:
    """The token-match never swallows blanks or near-miss slash commands
    (they keep their pre-existing channels byte-identically)."""
    events = _pump_lines(["  ", "/verifyaddress-notacommand", "exit"])
    assert (EVENT_TEXT, "Usage: /verifyaddress") not in [
        (kind, payload[:20]) for kind, payload in events
    ]
    # the near miss fell through to the transcript handler (help output):
    assert any(
        kind == EVENT_TEXT and payload.startswith("Commands:")
        for kind, payload in events
    )


# =========================================================================
# D6 — the additive own_address event
# =========================================================================


def _event_emitter() -> tuple[EventEmitter, list[Any]]:
    events: list[Any] = []
    return EventEmitter(events.append), events


def test_receive_preview_emits_own_address_with_its_coordinates() -> None:
    store, _wd, _wallet, _selection = _world()
    emitter, events = _event_emitter()
    # (web shape: output_fn IS the emitter's text sink, and the emitter
    # rides along for the typed marker — exactly how the pump wires it)
    app._print_next_receive_address(store, emitter.text, emitter=emitter)
    own = [e for e in events if e.kind == EVENT_OWN_ADDRESS]
    assert len(own) == 1
    payload = json.loads(own[0].payload)
    assert payload["branch"] == 0
    assert payload["index"] == store.get_derivation(_wallet_id(store), 0).next_index
    texts = [e.payload for e in events if e.kind == EVENT_TEXT]
    assert payload["address"] in texts[-1]  # names EXACTLY the shown string
    # ordering: the narration text event precedes the marker event (the
    # static half binds the button to the bubble it follows).
    kinds = [e.kind for e in events]
    assert kinds.index(EVENT_TEXT) < kinds.index(EVENT_OWN_ADDRESS)


def test_new_address_quick_action_emits_exactly_one_own_address() -> None:
    store, wd, _wallet, _selection = _world()
    emitter, events = _event_emitter()
    table = app.build_dispatch_table(
        store, store.get_active_wallet(), wd.parsed, client=None, scan_fn=lambda: None
    )
    out: list[str] = []
    assert app._run_quick_action(
        "/address",
        AgentLoop(app.stub_generate, table),
        table,
        app.SendSession(),
        store,
        out.append,
        None,
        emitter=emitter,
    )
    own = [json.loads(e.payload) for e in events if e.kind == EVENT_OWN_ADDRESS]
    assert len(own) == 1
    assert own[0]["address"] in out[-1]
    assert own[0]["branch"] == 0
    assert own[0]["index"] == 1  # the second allocation (0 was taken by _world)


def test_model_path_new_address_narration_emits_own_address() -> None:
    """The same event rides the MODEL-path narration (turn result is the
    single source for address/branch/index — the printer computes
    nothing)."""
    emitter, events = _event_emitter()
    envelope = Envelope(v=0, intent=IntentName.NEW_ADDRESS, params=NewAddressParams())
    out: list[str] = []
    app._print_turn(
        AgentTurnResult(
            status=AgentTurnStatus.OK,
            envelope=envelope,
            result={
                "address": ADDRS[0], "branch": 0, "index": 0, "address_number": 1,
            },
            user_message=None,
            turns_used=0,
        ),
        out.append,
        emitter=emitter,
    )
    assert [
        json.loads(e.payload) for e in events if e.kind == EVENT_OWN_ADDRESS
    ] == [{"address": ADDRS[0], "branch": 0, "index": 0}]


@pytest.mark.parametrize(
    "result",
    [
        {"address": ADDRS[0], "branch": 0},          # missing index
        {"address": "", "branch": 0, "index": 0},    # empty address
        {"address": ADDRS[0], "branch": 7, "index": 0},  # branch not a wallet branch
        {"address": ADDRS[0], "branch": True, "index": 0},  # bool is not a branch
        {"error": "store_error", "address": ADDRS[0], "branch": 0, "index": 0},
    ],
)
def test_own_address_event_never_fabricates(result: dict) -> None:
    """A result outside the closed shape emits NOTHING — an absent event
    is honest; a wrong coordinate would arm a wrong device path."""
    emitter, events = _event_emitter()
    app._print_new_address(result, [].append, emitter=emitter)
    assert [e for e in events if e.kind == EVENT_OWN_ADDRESS] == []


def test_no_emitter_paths_stay_byte_identical() -> None:
    """CLI/bare calls (emitter=None default) print exactly what they
    printed before this ticket — and never crash."""
    store, _wd, _wallet, _selection = _world()
    out: list[str] = []
    app._print_next_receive_address(store, out.append)
    assert out and out[-1].startswith("Next receive address")


# =========================================================================
# D7 — the additive signer_kind snapshot field
# =========================================================================


def test_snapshot_carries_closed_signer_kind_name() -> None:
    flow, session = TxFlow(), app.SendSession()
    snap = app.build_state_snapshot(flow, session, None)
    assert "signer_kind" not in snap  # no wiring = omitted, never guessed
    for kind in ("file", "hwi"):
        assert app.build_state_snapshot(flow, session, None, signer_kind=kind)[
            "signer_kind"
        ] == kind


def test_pump_state_snapshot_serves_signer_kind(tmp_path: Path) -> None:
    """The /state reply on the ENGINE thread carries the configured
    signer CLASS — value-free (no transfer folder, no fingerprint)."""
    selection = app.SignerSelection(
        kind="hwi", dir_path=tmp_path / "psbt-transfer", fingerprint_hex="deadbeef"
    )
    events: list[Any] = []
    handle = app.start_engine(
        lambda: app.EngineContext(
            loop=AgentLoop(app.stub_generate, {}),
            flow=TxFlow(),
            session=app.SendSession(),
            table={},
            signer_selection=selection,
        ),
        events.append,
    )
    assert handle.thread is not None
    snap = handle.request_state(5.0)
    handle.shutdown()
    handle.thread.join(10)
    assert snap is not None and snap["signer_kind"] == "hwi"
    text = json.dumps(snap)
    assert "psbt-transfer" not in text and "deadbeef" not in text


def test_pump_state_snapshot_omits_kind_before_provisioning() -> None:
    events: list[Any] = []
    handle = app.start_engine(
        lambda: app.EngineContext(
            loop=AgentLoop(app.stub_generate, {}),
            flow=TxFlow(),
            session=app.SendSession(),
            table={},
        ),
        events.append,
    )
    assert handle.thread is not None
    snap = handle.request_state(5.0)
    handle.shutdown()
    handle.thread.join(10)
    assert snap is not None and "signer_kind" not in snap

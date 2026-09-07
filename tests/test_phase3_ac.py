"""Phase 3 acceptance harness (TCK-P3-006).

The literal Phase 3 AC (PROJECT.md §12) — *"end-to-end mainnet send with
at least one real device (e.g. Coldcard file flow + one USB device);
tampered-PSBT fixture is caught deterministically; broadcast verified
on-chain; device-absent/locked error flows behave"* — needs real hardware
and a live mainnet broadcast. This module is its OFFLINE composite story:
every AC line is exercised through the REAL app wiring (REPL handlers,
TxFlow state machine, signer dispatch, re-validation gate, broadcast via a
mock chain) with deterministic fake devices — no network, no hardware, in
the default run. The LIVE real-device + on-chain procedure is documented
in ``docs/phase3-ac.md``; the literal device AC is deferred-run until
hardware is available (recorded there).

Helpers are REUSED from the existing suites (same test package), not
re-implemented:

- ``tests.test_e2e_skeleton`` — the send-flow fixtures (``SEND_*``),
  ``_send_chain_handler`` (mock chain incl. broadcast POST + status),
  ``_run_send_repl`` (REPL runner with the flow/clock/generate seams),
  ``FactsQuotingGenerate`` (the PRODUCTION-path fake model that quotes
  refs/txids from the injected FACTS block), ``_simulate_device_sign``
  (the test-vector device that signs the consensus BIP-143 digest with the
  fixture key, with an optional recipient-+546 tamper), ``_build_send_table``
  (table-level dispatch), ``_FakeDeviceClient`` (hwi client seam);
- ``tests.test_signer_file`` — the file-signer round-trip machinery;
- ``tests.test_tx_revalidate`` — the tamper-matrix check naming (the
  unit-level gate; this module closes the wiring gap below it).

AC coverage map (PROJECT.md §12 Phase 3 AC line → tests here):

- "tampered-PSBT fixture is caught deterministically" →
  ``test_ac1_tampered_psbt_caught_before_broadcast_at_wiring_level``
  (app-level sign handler + revalidation hard stop + zero broadcast POSTs,
  then the honest signer completes the broadcast — the composite story, not
  just the unit gate).
- "end-to-end mainnet send …" →
  ``test_ac2_full_lifecycle_file_signer_production_path``
  (create → dual-key confirm → file sign → revalidate → broadcast → status
  at height → store history row, one continuous e2e through the REAL REPL
  handlers with the FACTS-quoting model).
- "device-absent/locked error flows behave" →
  ``test_ac3_device_absent_and_locked_guidance_then_retry``,
  ``test_ac3_signed_file_missing_guidance``,
  ``test_ac3_broadcast_5xx_then_retry``.
- "broadcast verified on-chain" → the offline half (mock POST 201 + txid +
  status-at-height) is ``test_ac2_*``; the literal on-chain verification is
  the LIVE procedure in ``docs/phase3-ac.md`` (Step 5 — mempool.space
  txid lookup).
- ``test_ac4_double_broadcast_refused`` guards the terminal-state invariant
  (a second broadcast POST is refused by the flow — no re-broadcast storm).

Deterministic (fixed fixture wallet + scripted fakes + mock chain), no
network by default, <10 s.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Final

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import localwallet.app as app_module
from localwallet.signer import SignedResult
from localwallet.tx.flow import GateDecision, TxFlow, TxFlowStatus
from tests.test_e2e_skeleton import (
    SEND_RECIPIENT,
    SEND_UTXO,
    FactsQuotingGenerate,
    _build_send_table,
    _extract_signed_tx,
    _FakeDeviceClient,
    _fixture_parsed,
    _flow_txid,
    _run_send_repl,
    _send_chain_handler,
    _simulate_device_sign,
    derive_fixture_addresses,
)

# --------------------------------------------------------------------------
# Fixture summary
# --------------------------------------------------------------------------

#: Canonical fixture send (matches tests/test_e2e_skeleton.py): 60_000 sats
#: to the fixture recipient from one confirmed 100_000-sat UTXO at rate
#: 2 sat/vB → vsize 141, fee 282, change 39_718. All numbers are
#: dispatcher/tool-owned — never model-invented.
SEND_AMOUNT_SATS: Final[int] = 60_000

#: The txid the mock broadcast POST returns is COMPUTED from the posted
#: transaction (honest-backend echo; TCK-SEC-004 change 1) — assertions
#: derive it via :func:`_flow_txid` / ``_extract_signed_tx``.
#: Status serves a confirmed-at-height-870001 payload for it.
STATUS_HEIGHT: Final[int] = 870_001

#: Recipient value bump used by the tamper matrix (recipient +546).
TAMPER_DELTA: Final[int] = 546


# --------------------------------------------------------------------------
# Shared device/file simulation helper
# --------------------------------------------------------------------------


def _device_sign_before_line(transfer: Path, *, tamper: bool = False):
    """Closure for the file-signer REPL flows: between the two sign turns,
    have the "device" write a signed PSBT (ADR-0014 name) for the exported
    unsigned file — ``tamper`` bumps the recipient +546 after signing. An
    honest result also writes the matching SHA-256 sidecar so import reports
    checksum_verified=True (the no-sidecar note stays absent)."""
    def before_line() -> None:
        if transfer.exists():
            unsigned = sorted(transfer.glob("localwallet-unsigned-*.psbt.b64"))
            signed_existing = list(transfer.glob("localwallet-signed-*.psbt.b64"))
            if unsigned and not signed_existing:
                unsigned_b64 = unsigned[0].read_text(encoding="utf-8").strip()
                ref8 = unsigned[0].name[len("localwallet-unsigned-") : -len(".psbt.b64")]
                signed_path = transfer / f"localwallet-signed-{ref8}.psbt.b64"
                signed_path.write_text(
                    _simulate_device_sign(unsigned_b64, tamper=tamper) + "\n",
                    encoding="utf-8",
                )
                if not tamper:
                    side = transfer / (signed_path.name + ".sha256")
                    side.write_text(
                        hashlib.sha256(signed_path.read_bytes()).hexdigest() + "\n",
                        encoding="utf-8",
                    )

    return before_line


class _ScriptedSignerOverride:
    """App-layer HWI signer override returning a scripted SignedResult each.

    ``script`` is a sequence of ``(tamper: bool)``; each call consumes the
    next entry and returns a :class:`SignedResult` built by signing the
    unsigned PSBT it received with the fixture key (``_simulate_device_sign``,
    consensus BIP-143 digest) — ``tamper=True`` bumps the recipient +546
    AFTER signing. This drives the app-level sign handler directly, so the
    re-validation gate sees exactly what a (mis)behaving device returns.
    """

    def __init__(self, script: list[bool]) -> None:
        self._script = list(script)
        self.signed_psbts: list[str] = []

    def sign_unsigned(self, psbt_base64: str) -> SignedResult:
        tamper = self._script.pop(0)
        signed = _simulate_device_sign(psbt_base64, tamper=tamper)
        self.signed_psbts.append(signed)
        return SignedResult(signed, "hwi:ac-fake", checksum_verified=False)


# --------------------------------------------------------------------------
# AC-1: tampered PSBT caught deterministically, at the wiring level
# --------------------------------------------------------------------------


def test_ac1_tampered_psbt_caught_before_broadcast_at_wiring_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Composite AC-1 story (the wiring gap below the unit gate): build the
    unsigned PSBT through the real engine, sign it with the test-vector
    device, run the APP-LEVEL sign handler with a signer that returns a
    TAMPERED signed PSBT (recipient +546) → revalidation_failed hard stop,
    flow untouched (CONFIRMED), ZERO broadcast POSTs; then the honest
    signer → broadcast succeeds with exactly one POST carrying the
    re-validated transaction."""
    addr0 = derive_fixture_addresses(1)[0]
    state: dict = {}
    signer = _ScriptedSignerOverride(script=[True, False])  # tamper, then honest
    table, _store, _wallet, client, _recorded, flow, session = _build_send_table(
        lambda rec: _send_chain_handler(
            rec, utxos_by_addr={addr0: [SEND_UTXO]}, state=state
        ),
        signer_selection=app_module.SignerSelection(
            kind="hwi",
            dir_path=tmp_path / "transfer",
            fingerprint_hex=_fixture_parsed().hd_key.my_fingerprint.hex(),
        ),
        signer=signer,
    )

    # --- Stage and approve the transaction (create → dual-key confirm).
    created = table[app_module.IntentName.CREATE_TX](
        _validate(
            json.dumps(
                {
                    "v": 0,
                    "intent": "create_tx",
                    "params": {
                        "recipient": SEND_RECIPIENT,
                        "amount_sats": SEND_AMOUNT_SATS,
                    },
                }
            )
        )
    )
    tx_ref = created["tx_ref"]
    session.gate_decision = GateDecision.CONFIRM
    table[app_module.IntentName.CONFIRM_TX](
        _validate(
            json.dumps({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": tx_ref}})
        )
    )
    assert flow.state is TxFlowStatus.CONFIRMED

    # --- TAMPERED signed PSBT (recipient +546): hard stop before broadcast.
    sign_env = _validate(
        json.dumps({"v": 0, "intent": "sign_tx", "params": {"tx_ref": tx_ref}})
    )
    tampered = table[app_module.IntentName.SIGN_TX](sign_env)
    assert tampered["error"] == "revalidation_failed"
    detail = str(tampered["detail"])
    assert "output value does not match the intended transaction" in detail
    # Value-free hard stop: the intended recipient/amount never leak.
    assert SEND_RECIPIENT not in detail
    assert str(SEND_AMOUNT_SATS) not in detail
    assert str(TAMPER_DELTA) not in detail
    # Flow untouched: still CONFIRMED, no signed record, no broadcast path.
    assert flow.state is TxFlowStatus.CONFIRMED
    assert flow.signed is None
    assert state.get("broadcast_posts", []) == []  # tamper caught BEFORE any POST

    # --- Honest signer on the SAME CONFIRMED record → broadcast succeeds.
    honest = table[app_module.IntentName.SIGN_TX](sign_env)
    assert honest["status"] == "signed"
    assert flow.state is TxFlowStatus.SIGNED
    broadcast = table[app_module.IntentName.BROADCAST_TX](
        _validate(
            json.dumps(
                {"v": 0, "intent": "broadcast_tx", "params": {"tx_ref": tx_ref}}
            )
        )
    )
    assert broadcast["status"] == "broadcast"
    # The recorded txid is the one COMPUTED from the signed transaction we
    # actually broadcast (TCK-SEC-004 change 1 binding).
    assert broadcast["txid"] == _extract_signed_tx(
        signer.signed_psbts[1]
    ).txid().hex()
    assert flow.state is TxFlowStatus.BROADCAST
    # Exactly one POST, carrying the re-validated (honest) transaction.
    posts = state["broadcast_posts"]
    assert len(posts) == 1
    assert posts[0] == _extract_signed_tx(signer.signed_psbts[1]).serialize().hex()
    client.close()
    _store.close()


# --------------------------------------------------------------------------
# AC-2: full lifecycle (production path) + store history row
# --------------------------------------------------------------------------


def test_ac2_full_lifecycle_file_signer_production_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Composite AC-2 story: create → dual-key confirm → file sign (export →
    signed-file-missing → device places the file → sign again → revalidate)
    → broadcast (mock POST 201 + txid) → status (confirmed at height) with a
    store history row — one continuous e2e through the REAL REPL handlers,
    using the PRODUCTION-path fake model (FactsQuotingGenerate) that quotes
    every ref/txid from the injected FACTS block (never the flow object)."""
    addr0 = derive_fixture_addresses(1)[0]
    transfer = tmp_path / "transfer"
    state: dict = {}
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]}, state=state)
    fake = FactsQuotingGenerate(
        ["create", "confirm", "sign", "sign", "broadcast", "status", "history"]
    )

    code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            "yes please",
            "sign it",
            "sign it again",
            "broadcast it",
            "what's the status?",
            "show my transactions",
            "exit",
        ],
        ["create", "confirm", "sign", "sign", "broadcast", "status", "history"],
        generate=fake,
        extra_env={"LOCALWALLET_SIGNER_DIR": str(transfer)},
        before_line=_device_sign_before_line(transfer),
    )

    assert code == 0
    joined = "\n".join(outputs)
    # Confirmation card + dual-key confirm (production FACTS path).
    assert "Pending transaction — review it carefully" in joined
    assert "Approved." in joined
    # Sign turn 1: export + file-missing handoff.
    assert "Exported to " in joined
    assert "(say: signed localwallet-signed-" in joined
    # Sign turn 2: import → revalidate → SIGNED, txid quoted verbatim.
    signed_files = list(transfer.glob("localwallet-signed-*.psbt.b64"))
    assert len(signed_files) == 1
    expected_txid = _extract_signed_tx(
        signed_files[0].read_text(encoding="utf-8").strip()
    ).txid().hex()
    assert f"Signed and verified ✓ txid {expected_txid}." in joined
    assert "integrity not verified" not in joined  # sidecar present
    # Broadcast: single POST with the re-validated tx; status confirmed.
    assert len(state["broadcast_posts"]) == 1
    expected_txid = _flow_txid(flow)
    assert f"Sent! txid {expected_txid} — tracking…" in joined
    assert flow.state is TxFlowStatus.BROADCAST
    assert f"Confirmed at height {STATUS_HEIGHT}." in joined
    # The status turn quoted broadcast_txid from the FACTS (production path).
    assert f"broadcast_txid: {expected_txid}" in fake.prompts[5]
    # Store history row: outbound, unconfirmed, the approved fee.
    store_path = tmp_path / "store.db"
    from localwallet.store import Store

    with Store(store_path) as store:
        wallet_row = store.get_wallet_by_name("default")
        assert wallet_row is not None
        rows = store.get_txs_for_wallet(wallet_row.id)
        assert [(r.txid, r.height, r.direction) for r in rows] == [
            (expected_txid, None, "out")
        ]
    # History narration shows the outbound row.
    assert f"tx {expected_txid[:12]}… out unconfirmed" in joined


# --------------------------------------------------------------------------
# AC-3: device-absent / locked / file-missing / broadcast-fail error flows
# --------------------------------------------------------------------------


class _FakeLifecycleCommands:
    """hwilib.commands stand-in for the AC-3 device error flows.

    The FIRST ``enumerate`` yields the error phase so ``HwiUsbSigner``
    raises the device-absent (zero devices) or device-locked (unreadable
    fingerprint) guidance; every later ``enumerate`` yields the
    fingerprint-matched wallet device, which signs for real via
    ``_simulate_device_sign`` (fixture key, consensus BIP-143). Records
    device-handle close via ``_FakeDeviceClient``."""

    def __init__(self, fingerprint_hex: str, first_phase: str) -> None:
        self.fingerprint_hex = fingerprint_hex
        self.first_phase = first_phase  # "absent" | "locked"
        self.sign_calls = 0
        self.rec: dict = {}
        self._phase_done = False
        self.client = _FakeDeviceClient(fingerprint_hex, self.rec)

    def enumerate(self, password=None):
        assert password is None, "our layer must never pass host-side secrets"
        if not self._phase_done:
            self._phase_done = True
            if self.first_phase == "absent":
                return []
            # Locked/uninitialized: present but no readable fingerprint.
            return [
                {
                    "type": "trezor",
                    "path": "hid:fake",
                    "model": "trezor_t",
                    "error": "Not initialized",
                }
            ]
        return [
            {
                "type": "trezor",
                "path": "hid:fake",
                "model": "trezor_t",
                "fingerprint": self.fingerprint_hex,
            }
        ]

    def get_client(self, device_type, device_path, password=None, chain=None):
        return self.client

    def signtx(self, client, psbt):
        self.sign_calls += 1
        return {"psbt": _simulate_device_sign(psbt)}


@pytest.mark.parametrize(
    ("first_phase", "guidance_fragment"),
    [
        ("absent", "No device found — plug in"),
        ("locked", "Enter your PIN/passphrase"),
    ],
)
def test_ac3_device_absent_and_locked_guidance_then_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    first_phase: str,
    guidance_fragment: str,
) -> None:
    """AC-3 device-absent + device-locked: the first sign turn narrates the
    code-owned §10 guidance (absent: "plug in"; locked: "enter your
    PIN/passphrase"), the flow is left CONFIRMED (the device never signs
    while absent/locked — zero signtx calls), and the retry ("sign it
    again") succeeds through to broadcast. The captured flow-state sequence
    before each REPL line proves the CONFIRMED-preservation invariant:
    IDLE → CREATED → CONFIRMED → CONFIRMED (after the error) → SIGNED →
    BROADCAST."""
    from localwallet.signer.hwi import HwiUsbSigner as RealHwiUsbSigner

    addr0 = derive_fixture_addresses(1)[0]
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})
    fingerprint = _fixture_parsed().hd_key.my_fingerprint.hex()
    commands = _FakeLifecycleCommands(fingerprint, first_phase)
    monkeypatch.setattr(
        app_module,
        "HwiUsbSigner",
        lambda fp: RealHwiUsbSigner(fp, commands_module=commands),
    )
    fake = FactsQuotingGenerate(["create", "confirm", "sign", "sign", "broadcast"])
    my_flow = TxFlow()
    seen: list = []

    def before_line() -> None:
        seen.append(my_flow.state)

    code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            "yes please",
            "sign it",
            "sign it again",
            "broadcast it",
            "exit",
        ],
        ["create", "confirm", "sign", "sign", "broadcast"],
        generate=fake,
        extra_env={"LOCALWALLET_SIGNER": "hwi"},
        flow=my_flow,
        before_line=before_line,
    )

    assert code == 0
    joined = "\n".join(outputs)
    # The error-phase guidance is narrated verbatim (code-owned §10 text).
    assert guidance_fragment in joined
    assert "say 'retry'" in joined
    # The device never signed while absent/locked; only the retry signed.
    assert commands.sign_calls == 1
    # The retry + broadcast succeeded → the flow was still CONFIRMED after
    # the error phase (a failed sign preserves CONFIRMED; the next sign needs
    # it to proceed).
    assert "Signed and verified ✓" in joined
    assert f"Sent! txid {_flow_txid(flow)} — tracking…" in joined
    assert flow.state is TxFlowStatus.BROADCAST
    # Exact state-machine sequence across the turns (preservation invariant).
    assert seen == [
        TxFlowStatus.IDLE,
        TxFlowStatus.CREATED,
        TxFlowStatus.CONFIRMED,
        TxFlowStatus.CONFIRMED,  # the error turn left it CONFIRMED
        TxFlowStatus.SIGNED,
        TxFlowStatus.BROADCAST,
    ]
    assert commands.rec["closed"] is True  # device handle released


def test_ac3_signed_file_missing_guidance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-3 file-mode: sign_tx before the user places the signed file → the
    §10 handoff line names the export path and the EXPECTED signed filename
    (ADR-0014, deterministic from tx_ref); the unsigned file + sidecar exist;
    the flow stays CONFIRMED (nothing signed)."""
    addr0 = derive_fixture_addresses(1)[0]
    transfer = tmp_path / "transfer"
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]})

    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [f"send 60000 sats to {SEND_RECIPIENT}", "yes please", "sign it", "exit"],
        ["create", "confirm", "sign"],
        extra_env={"LOCALWALLET_SIGNER_DIR": str(transfer)},
    )

    handoff = next(line for line in outputs if line.startswith("Exported to "))
    assert "Move it to your SD card, sign on your device" in handoff
    assert "(say: signed localwallet-signed-" in handoff
    assert ".psbt.b64)." in handoff
    unsigned = sorted(transfer.glob("localwallet-unsigned-*.psbt.b64"))
    assert len(unsigned) == 1
    assert (transfer / (unsigned[0].name + ".sha256")).exists()  # ADR-0014
    assert not list(transfer.glob("localwallet-signed-*.psbt.b64"))
    assert flow.state is TxFlowStatus.CONFIRMED


def test_ac3_broadcast_5xx_then_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-3 broadcast-fail: POST 5xx → broadcast_failed narration, flow STAYS
    SIGNED (the signed tx is kept); the retry succeeds. Exactly ONE POST per
    attempt — no automatic retry storm (the chain layer's single-attempt
    policy; retrying is explicit)."""
    addr0 = derive_fixture_addresses(1)[0]
    transfer = tmp_path / "transfer"
    state: dict = {"broadcast_fail": True}
    handler = _send_chain_handler([], utxos_by_addr={addr0: [SEND_UTXO]}, state=state)

    def before_line() -> None:
        _device_sign_before_line(transfer)()
        # Fail only the FIRST POST: after one attempt, allow the retry.
        if len(state.get("broadcast_posts", [])) >= 1:
            state["broadcast_fail"] = False

    _code, outputs, flow = _run_send_repl(
        monkeypatch,
        tmp_path,
        handler,
        [
            f"send 60000 sats to {SEND_RECIPIENT}",
            "yes please",
            "sign it",
            "sign it again",
            "broadcast it",
            "broadcast it again",
            "exit",
        ],
        ["create", "confirm", "sign", "sign", "broadcast", "broadcast"],
        extra_env={"LOCALWALLET_SIGNER_DIR": str(transfer)},
        before_line=before_line,
    )

    joined = "\n".join(outputs)
    # First attempt: 500 → scrubbed failure, signed transaction kept.
    assert (
        "Broadcast failed (broadcast failed: status 500) — the signed "
        "transaction is kept; say 'broadcast' to retry." in joined
    )
    # Retry succeeds; exactly one POST per attempt in total.
    assert f"Sent! txid {_flow_txid(flow)} — tracking…" in joined
    assert flow.state is TxFlowStatus.BROADCAST
    assert len(state["broadcast_posts"]) == 2


# --------------------------------------------------------------------------
# AC-4: double-broadcast guard (terminal-state invariant)
# --------------------------------------------------------------------------


def test_ac4_double_broadcast_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-4: calling broadcast_tx AGAIN after the flow is BROADCAST is
    refused by the state gate (no second POST) — the dispatcher owns the
    terminal state; a re-broadcast storm is structurally impossible."""
    addr0 = derive_fixture_addresses(1)[0]
    transfer = tmp_path / "transfer"
    state: dict = {}
    table, _store, _wallet, client, _recorded, flow, session = _build_send_table(
        lambda rec: _send_chain_handler(
            rec, utxos_by_addr={addr0: [SEND_UTXO]}, state=state
        ),
        signer_selection=app_module.SignerSelection(
            kind="file", dir_path=transfer, fingerprint_hex="00" * 4
        ),
    )
    created = table[app_module.IntentName.CREATE_TX](
        _validate(
            json.dumps(
                {
                    "v": 0,
                    "intent": "create_tx",
                    "params": {"recipient": SEND_RECIPIENT, "amount_sats": SEND_AMOUNT_SATS},
                }
            )
        )
    )
    tx_ref = created["tx_ref"]
    session.gate_decision = GateDecision.CONFIRM
    table[app_module.IntentName.CONFIRM_TX](
        _validate(json.dumps({"v": 0, "intent": "confirm_tx", "params": {"tx_ref": tx_ref}}))
    )
    sign_env = _validate(
        json.dumps({"v": 0, "intent": "sign_tx", "params": {"tx_ref": tx_ref}})
    )
    # Export + place an honest signed file, then sign → SIGNED.
    table[app_module.IntentName.SIGN_TX](sign_env)
    unsigned_path = transfer / f"localwallet-unsigned-{tx_ref[:8]}.psbt.b64"
    honest = _simulate_device_sign(unsigned_path.read_text(encoding="utf-8").strip())
    signed_path = transfer / f"localwallet-signed-{tx_ref[:8]}.psbt.b64"
    signed_path.write_text(honest + "\n", encoding="utf-8")
    (transfer / (signed_path.name + ".sha256")).write_text(
        hashlib.sha256(signed_path.read_bytes()).hexdigest() + "\n", encoding="utf-8"
    )
    table[app_module.IntentName.SIGN_TX](sign_env)
    assert flow.state is TxFlowStatus.SIGNED

    # First broadcast: one POST, BROADCAST (terminal).
    broadcast_env = _validate(
        json.dumps({"v": 0, "intent": "broadcast_tx", "params": {"tx_ref": tx_ref}})
    )
    first = table[app_module.IntentName.BROADCAST_TX](broadcast_env)
    assert first["status"] == "broadcast"
    assert flow.state is TxFlowStatus.BROADCAST
    assert len(state["broadcast_posts"]) == 1

    # A second broadcast_tx (even quoting the same signed ref) is refused by
    # the state gate: the flow is no longer SIGNED → no second POST.
    again = table[app_module.IntentName.BROADCAST_TX](broadcast_env)
    assert again == {
        "error": "broadcast_refused",
        "detail": "no signed transaction to broadcast",
    }
    assert flow.state is TxFlowStatus.BROADCAST
    assert len(state["broadcast_posts"]) == 1  # still exactly one POST
    client.close()
    _store.close()


# --------------------------------------------------------------------------
# Small local helper (validate an envelope through the real protocol layer)
# --------------------------------------------------------------------------


def _validate(text: str):
    """Validate a raw envelope JSON through the real protocol schema."""
    from localwallet.protocol import validate_payload

    return validate_payload(text)

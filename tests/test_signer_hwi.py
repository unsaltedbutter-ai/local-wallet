"""HwiUsbSigner tests (TCK-P3-003, ADR-0015 + amendment #2, TCK-HW-002).

Covers: lazy hwilib import (module imports without the wheel; missing wheel
→ HwiUnavailableError); enumerate mapping (hex/bytes MASTER fingerprints,
unreadable devices) with dict-shaping containment; the candidate gate
(zero devices → absent; all-unreadable → locked guidance by device class)
plus the POST-OPEN ACCOUNT-KEY BIND — the open client must serve the
wallet's account key at the descriptor account path (the debugger repro:
enumerate reports the MASTER fingerprint, e.g. the Jade's 40dbb192, which
is never equal to the account-key fingerprint and must never gate
selection; TCK-HW-002) — match → sign, wrong account key → mismatch hard
stop, client that cannot serve the account key → fail closed (no skip,
R1); str/bytes sign-result normalization incl. the empty-bytes refusal
(rider R2); older-HWI bytes-input retry; locked/busy/canceled/unknown
error mapping (guidance strings asserted, value-free); display_address
best-effort mapping; SignedResult fields; no-network lint compliance.

hwilib is faked via the ``commands_module`` constructor seam — the fake
mirrors the empirically verified hwi 3.2.0 API surface (commands.enumerate /
get_client / signtx / displayaddress; client.get_pubkey_at_path per the
base Client contract and JadeClient jade.py:164). One env-gated test
exercises the real wheel (HID enumeration only — device I/O, not network).
"""

import base64
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from embit.hashes import hash160

from localwallet.signer.base import Signer, SignerError
from localwallet.signer.hwi import (
    DeviceAbsentError,
    DeviceError,
    DeviceInfo,
    DeviceLockedError,
    DeviceMismatchError,
    HwiUnavailableError,
    HwiUsbSigner,
)

# The fake hwilib stand-ins below shadow some hwilib class names
# (DeviceBusyError et al.) — keep an explicit handle on OUR DeviceBusyError.
from localwallet.signer.hwi import DeviceBusyError as LwDeviceBusyError

REPO_ROOT = Path(__file__).resolve().parent.parent

# The gate binds the device's ACCOUNT key (ADR-0015 amendment #2): the
# wallet fingerprint is hash160(pubkey)[:4] of the account public key the
# device serves at ACCOUNT_PATH. These "pubkeys" are arbitrary 33-byte
# digests — our code only ever hashes them (no curve math).
ACCOUNT_PATH = "m/84'/0'/0'"
_ACCOUNT_PUBKEY = hashlib.sha512(b"localwallet hwi test account key").digest()[:33]
_OTHER_PUBKEY = hashlib.sha512(b"localwallet hwi test decoy key").digest()[:33]
FP_WALLET = hash160(_ACCOUNT_PUBKEY)[:4].hex()
FP_OTHER = hash160(_OTHER_PUBKEY)[:4].hex()

# hwilib ``enumerate`` reports the device MASTER fingerprint (network-
# independent, unrelated to any account key) — the user's Jade shows
# 40dbb192-style values. The trust gate must NEVER compare against it.
MASTER_FP = "40dbb192"

# Minimal well-formed-looking base64 PSBT text (fake hwilib never parses it).
PSBT_B64 = base64.b64encode(b"psbt\xff" + b"\x01" * 24).decode("ascii")
SIGNED_B64 = base64.b64encode(b"psbt\xff" + b"\x02" * 24).decode("ascii")

DEVICE_WALLET = {
    "type": "trezor",
    "path": "hid:/dev/hid0",
    "model": "trezor_t",
    "fingerprint": MASTER_FP,
}
DEVICE_OTHER = {
    "type": "ledger",
    "path": "hid:/dev/hid1",
    "model": "ledger_nano_s",
    "fingerprint": MASTER_FP,
}
# Unlocked Jade exactly as the MW-4 debugger traced it (TCK-HW-002 repro):
# enumerate carries only the MASTER fingerprint; the account key lives at
# the descriptor origin path and is what must bind.
JADE_UNLOCKED = {
    "type": "jade",
    "path": "/dev/tty.usbmodemJADE",
    "model": "jade",
    "fingerprint": MASTER_FP,
    "needs_pin_sent": False,
    "needs_passphrase_sent": False,
}
DEVICE_OTHER = {
    "type": "ledger",
    "path": "hid:/dev/hid1",
    "model": "ledger_nano_s",
    "fingerprint": FP_OTHER,
}


# --------------------------------------------------------------------------
# Fake hwilib.commands — mirrors the verified hwi 3.2.0 surface
# --------------------------------------------------------------------------


class _FakeHwiError(Exception):
    """Base for fake hwilib errors (mapping is class-name-based)."""


class DeviceNotReadyError(_FakeHwiError): ...


class DeviceBusyError(_FakeHwiError): ...


class ActionCanceledError(_FakeHwiError): ...


class DeviceConnectionError(_FakeHwiError): ...


class PSBTSerializationError(_FakeHwiError): ...


class BadArgumentError(_FakeHwiError): ...


class UnavailableActionError(_FakeHwiError): ...


class JadeError(Exception):
    """jadepy bare-error stand-in (NOT an HWWError — hwilib leaves it
    unwrapped on device-side cancel). Class name + code drive our mapping."""

    def __init__(self, code: int, message: str = "", data: object = None) -> None:
        super().__init__(f"JadeError: {code} - {message} (Data: {data!r})")
        self.code = code
        self.message = message
        self.data = data


class FakeClient:
    """HardwareWalletClient stand-in that records close() calls.

    Deliberately has NO ``get_pubkey_at_path``: a client that cannot serve
    the account key must fail the gate closed (R1/ADR-0015 amendment #2).
    """

    def __init__(self, recorder: dict) -> None:
        self.recorder = recorder

    def close(self) -> None:
        self.recorder["closed"] = True


class FakeAccountClient(FakeClient):
    """FakeClient serving the account-path key (hwi base-Client contract:
    ``get_pubkey_at_path(path) -> object with .pubkey``, jade.py:164 shape).

    ``pubkey`` doubles as the fault injector: an Exception raises from the
    getter, any other non-bytes value models an unusable response shape
    (both must fail closed), and ``None`` serves the wallet account key.
    Queried paths are recorded for gate-placement assertions."""

    def __init__(self, recorder: dict, pubkey: object = None) -> None:
        super().__init__(recorder)
        self._pubkey = _ACCOUNT_PUBKEY if pubkey is None else pubkey
        self.paths: list[str] = []

    def get_pubkey_at_path(self, bip32_path: str) -> object:
        self.paths.append(bip32_path)
        if isinstance(self._pubkey, Exception):
            raise self._pubkey
        return SimpleNamespace(pubkey=self._pubkey)


class FakeCommands:
    """hwilib.commands stand-in (enumerated API shape of hwi 3.2.0)."""

    def __init__(
        self,
        devices: list[dict] | None = None,
        enumerate_error: Exception | None = None,
        client: FakeClient | dict | None | object = "auto",
        get_client_error: Exception | None = None,
        signtx_result: object = None,
        signtx_error: Exception | None = None,
        signtx_bytes_only: bool = False,
        display_result: object = None,
        display_error: Exception | None = None,
    ) -> None:
        self.devices = devices if devices is not None else [dict(DEVICE_WALLET)]
        self.enumerate_error = enumerate_error
        self.client = client
        self.get_client_error = get_client_error
        self.signtx_result = signtx_result
        self.signtx_error = signtx_error
        self.signtx_bytes_only = signtx_bytes_only
        self.display_result = display_result
        self.display_error = display_error
        self.calls: list[tuple] = []

    # -- hwilib.commands API -------------------------------------------------

    def enumerate(self, password=None):
        assert password is None, "our layer must never pass host-side secrets"
        self.calls.append(("enumerate",))
        if self.enumerate_error is not None:
            raise self.enumerate_error
        return [dict(d) for d in self.devices]

    def get_client(self, device_type, device_path, password=None, chain=None):
        self.calls.append(("get_client", device_type, device_path, chain))
        if self.get_client_error is not None:
            raise self.get_client_error
        if self.client == "auto":
            return FakeAccountClient(self.__dict__.setdefault("rec", {}))
        if isinstance(self.client, dict):
            # path → client map for multi-device selection tests.
            return self.client.get(device_path)
        return self.client

    def signtx(self, client, psbt):
        self.calls.append(("signtx", psbt))
        if self.signtx_bytes_only and isinstance(psbt, str):
            raise TypeError("older HWI wants bytes")
        if self.signtx_error is not None:
            raise self.signtx_error
        return self.signtx_result

    def displayaddress(self, client, path=None, desc=None, addr_type=None):
        self.calls.append(("displayaddress", desc))
        if self.display_error is not None:
            raise self.display_error
        return self.display_result


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def make_signer(commands: FakeCommands, fingerprint: str = FP_WALLET) -> HwiUsbSigner:
    return HwiUsbSigner(fingerprint, ACCOUNT_PATH, commands_module=commands)


def default_sign_result() -> dict:
    return {"psbt": SIGNED_B64, "signed": True}


# --------------------------------------------------------------------------
# Lazy import & hierarchy
# --------------------------------------------------------------------------


def test_module_imports_cleanly_without_hwilib_installed():
    """The module imports (and exposes its API) with hwilib absent — proven
    in a fresh interpreter with hwilib imports poisoned to fail."""
    code = (
        "import sys\n"
        "sys.modules['hwilib'] = None\n"
        "sys.modules['hwilib.commands'] = None\n"
        "import localwallet.signer.hwi as m\n"
        "assert m.HwiUsbSigner is not None\n"
        "print('lazy-ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "lazy-ok" in proc.stdout


def test_missing_wheel_maps_to_hwi_unavailable(monkeypatch):
    """A hwilib ImportError surfaces as HwiUnavailableError with install
    guidance — never a raw ImportError."""
    monkeypatch.setitem(sys.modules, "hwilib", None)
    monkeypatch.setitem(sys.modules, "hwilib.commands", None)
    signer = HwiUsbSigner(FP_WALLET, ACCOUNT_PATH)  # no commands_module → real lazy import
    with pytest.raises(HwiUnavailableError) as excinfo:
        signer.sign_unsigned(PSBT_B64)
    assert "retry" in str(excinfo.value)


def test_device_error_hierarchy_reuses_signer_error_base():
    for cls in (
        DeviceAbsentError,
        LwDeviceBusyError,
        DeviceLockedError,
        DeviceMismatchError,
        HwiUnavailableError,
    ):
        assert issubclass(cls, DeviceError)
        assert issubclass(cls, SignerError)
    assert issubclass(DeviceError, SignerError)


def test_signer_abc_conformance():
    signer = make_signer(FakeCommands())
    assert isinstance(signer, Signer)
    assert signer.name == "hwi"


def test_constructor_validation():
    with pytest.raises(SignerError):
        HwiUsbSigner("", ACCOUNT_PATH)  # empty fingerprint
    with pytest.raises(SignerError):
        HwiUsbSigner(FP_WALLET, "  ")  # empty account path — no fallback gate
    with pytest.raises(TypeError):
        HwiUsbSigner(FP_WALLET)  # account_path is REQUIRED (one honest path)
    with pytest.raises(SignerError):
        HwiUsbSigner(FP_WALLET, ACCOUNT_PATH, chain="mainnet")  # unknown chain name


# --------------------------------------------------------------------------
# enumerate_devices
# --------------------------------------------------------------------------


def test_enumerate_maps_fields_and_normalizes_fingerprint():
    commands = FakeCommands(
        devices=[
            {**DEVICE_WALLET, "fingerprint": b"\xa1\xb2\xc3\xd4"},  # bytes in
            {"type": "trezor", "path": "hid:x", "model": "trezor_t"},  # no fp
        ]
    )
    signer = make_signer(commands)
    devices = signer.enumerate_devices()
    assert devices == [
        DeviceInfo(
            type="trezor",
            model="trezor_t",
            path="hid:/dev/hid0",
            fingerprint_hex="a1b2c3d4",
        ),
        DeviceInfo(
            type="trezor", model="trezor_t", path="hid:x", fingerprint_hex=None
        ),
    ]


def test_enumerate_error_maps_to_guidance():
    commands = FakeCommands(
        enumerate_error=DeviceConnectionError("Device is asleep")
    )
    signer = make_signer(commands)
    with pytest.raises(DeviceAbsentError) as excinfo:
        signer.enumerate_devices()
    assert "plug in" in str(excinfo.value).lower()


# --------------------------------------------------------------------------
# Trust gate: candidate narrowing + post-open account-key bind (ADR-0015
# + amendment #2, TCK-HW-002)
# --------------------------------------------------------------------------


def test_sign_happy_path_one_match():
    commands = FakeCommands(signtx_result=default_sign_result())
    signer = make_signer(commands)
    result = signer.sign_unsigned(PSBT_B64)

    assert result.psbt_base64 == SIGNED_B64
    assert result.signer_name == "hwi:trezor_t"
    assert result.checksum_verified is False
    # Exactly one client open, one sign, one close; psbt passed as str.
    opened = [c for c in commands.calls if c[0] == "get_client"]
    signed = [c for c in commands.calls if c[0] == "signtx"]
    assert len(opened) == 1 and opened[0][1:3] == ("trezor", "hid:/dev/hid0")
    assert len(signed) == 1 and signed[0][1] == PSBT_B64
    assert commands.rec["closed"] is True


def test_unlocked_jade_master_fp_never_gates_signing():
    """TCK-HW-002 debugger repro (MW-4): the unlocked Jade enumerates with
    its MASTER fingerprint (40dbb192-style) — which is NOT, and can never
    be, the wallet's account-key fingerprint. The gate must open the client
    and bind the ACCOUNT key at the descriptor's path; the pre-fix code
    refused here with DeviceMismatchError forever."""
    commands = FakeCommands(
        devices=[dict(JADE_UNLOCKED)], signtx_result=default_sign_result()
    )
    result = make_signer(commands).sign_unsigned(PSBT_B64)
    assert result.psbt_base64 == SIGNED_B64
    assert result.signer_name == "hwi:jade"


def test_gate_binds_at_the_descriptor_account_path():
    """The client is asked for the pubkey at the account path the signer
    was constructed with — never some hardcoded or master path."""
    client = FakeAccountClient({})
    commands = FakeCommands(client=client, signtx_result=default_sign_result())
    make_signer(commands).sign_unsigned(PSBT_B64)
    assert client.paths == [ACCOUNT_PATH]


def test_decoy_account_key_is_mismatch_hard_stop():
    """A connected device whose account key is a DIFFERENT wallet's (its
    master fingerprint is whatever) NEVER signs — wrong wallet / typo'd
    descriptor / attack all stop here (ADR-0015)."""
    recorder: dict = {}
    commands = FakeCommands(
        devices=[dict(DEVICE_OTHER)],
        client=FakeAccountClient(recorder, _OTHER_PUBKEY),
        signtx_result=default_sign_result(),
    )
    signer = make_signer(commands)
    with pytest.raises(DeviceMismatchError) as excinfo:
        signer.sign_unsigned(PSBT_B64)
    assert "does not match this wallet" in str(excinfo.value)
    # Hard stop: the signing command was never reached, handle released.
    assert ("signtx",) not in commands.calls
    assert recorder["closed"] is True


def test_right_device_picked_among_readable_candidates():
    """Multiple READABLE devices on the bus: enumeration no longer selects
    (master fp is not the anchor), the account-key bind does — the decoy
    is opened, checked, closed; the wallet's device signs."""
    wallet_rec: dict = {}
    other_rec: dict = {}
    commands = FakeCommands(
        devices=[dict(DEVICE_OTHER), dict(DEVICE_WALLET)],
        client={
            "hid:/dev/hid1": FakeAccountClient(other_rec, _OTHER_PUBKEY),
            "hid:/dev/hid0": FakeAccountClient(wallet_rec),
        },
        signtx_result=default_sign_result(),
    )
    result = make_signer(commands).sign_unsigned(PSBT_B64)
    assert result.signer_name == "hwi:trezor_t"
    # Both handles released (decoy at bind-refusal, wallet after signing).
    assert other_rec["closed"] is True and wallet_rec["closed"] is True
    signed = [c for c in commands.calls if c[0] == "signtx"]
    assert len(signed) == 1
    opened = [c for c in commands.calls if c[0] == "get_client"]
    assert [c[2] for c in opened] == ["hid:/dev/hid1", "hid:/dev/hid0"]


def test_sign_gets_mainnet_chain():
    """The device client is opened for the project's chain (mainnet-only,
    ADR-0021)."""
    commands = FakeCommands(signtx_result=default_sign_result())
    make_signer(commands).sign_unsigned(PSBT_B64)
    chain = next(c for c in commands.calls if c[0] == "get_client")[3]
    try:
        from hwilib.common import Chain

        assert chain == Chain.MAIN
    except ImportError:  # hwilib absent → lenient string fallback
        assert chain == "main"


def test_default_chain_is_main():
    """The default chain is mainnet (ADR-0021) and resolves via hwilib's
    Chain enum (hwilib >= 3.1 exposes ``Chain.MAIN``)."""
    signer = HwiUsbSigner(FP_WALLET, ACCOUNT_PATH)
    assert signer.chain == "main"
    try:
        from hwilib.common import Chain

        assert signer._chain_enum(None) is Chain.MAIN
    except ImportError:  # hwilib absent → lenient string fallback
        assert signer._chain_enum(None) == "main"


def test_zero_devices_is_absent_error():
    commands = FakeCommands(devices=[])
    signer = make_signer(commands)
    with pytest.raises(DeviceAbsentError) as excinfo:
        signer.sign_unsigned(PSBT_B64)
    assert "plug in" in str(excinfo.value).lower()
    assert ("signtx",) not in commands.calls


def test_unreadable_fingerprints_only_is_locked_error():
    """Locked/uninitialized devices enumerate without a fingerprint — that is
    unlock guidance, not a mismatch (ADR-0015: don't slander the right
    device before it can identify itself)."""
    locked = {"type": "trezor", "path": "hid:x", "model": "trezor_t", "error": "Not initialized"}
    commands = FakeCommands(devices=[locked], signtx_result=default_sign_result())
    signer = make_signer(commands)
    with pytest.raises(DeviceLockedError) as excinfo:
        signer.sign_unsigned(PSBT_B64)
    assert "pin/passphrase" in str(excinfo.value).lower()
    assert ("signtx",) not in commands.calls


def test_two_matching_devices_refused_with_unplug_guidance():
    commands = FakeCommands(
        devices=[dict(DEVICE_WALLET), {**DEVICE_WALLET, "path": "hid:/dev/hid9"}],
        signtx_result=default_sign_result(),
    )
    signer = make_signer(commands)
    with pytest.raises(DeviceError) as excinfo:
        signer.sign_unsigned(PSBT_B64)
    assert "unplug" in str(excinfo.value).lower()
    assert ("signtx",) not in commands.calls


# --------------------------------------------------------------------------
# TCK-HW-001: locked-device guidance by device class (Jade / host-PIN)
# --------------------------------------------------------------------------

# Real hwi 3.2.0 enumerate shapes (handle_errors swallows the pre-construction
# DeviceNotReadyError into the entry: error+code keys, NO fingerprint).
JADE_LOCKED = {
    "type": "jade",
    "path": "/dev/tty.usbmodemJADE",
    "model": "jade",
    "needs_pin_sent": False,
    "needs_passphrase_sent": False,
    "error": (
        'Use "Recovery Phrase Login" or "QR PIN Unlock" feature '
        "on Jade hw to access wallet"
    ),
    "code": -12,
}
TREZOR_LOCKED = {
    "type": "trezor",
    "path": "hid:/dev/hidT",
    "model": "trezor_t",
    "needs_pin_sent": True,
    "needs_passphrase_sent": False,
    "error": "Trezor is locked. Unlock by using 'promptpin' and then 'sendpin'.",
    "code": -12,
}


def test_enumerate_preserves_lock_class_fields():
    """The class signals _select_candidates branches on survive enumeration;
    old-shape entries default them to False."""
    commands = FakeCommands(devices=[dict(JADE_LOCKED), dict(DEVICE_WALLET)])
    devices = make_signer(commands).enumerate_devices()
    assert devices[0].locked is True
    assert devices[0].needs_pin_sent is False
    assert devices[0].needs_passphrase_sent is False
    assert devices[0].fingerprint_hex is None
    assert devices[1] == DeviceInfo(  # defaults keep the old shape valid
        type="trezor", model="trezor_t", path="hid:/dev/hid0",
        fingerprint_hex=MASTER_FP,
    )


def test_locked_jade_gets_jade_on_device_guidance():
    """A locked Jade enumerates with the swallowed pinserver error (code -12,
    no fingerprint) — the guidance must name the Jade on-device unlock, NOT
    the generic "PIN/passphrase on the device" line (which loops forever:
    Jade never takes a host-side PIN)."""
    commands = FakeCommands(devices=[dict(JADE_LOCKED)], signtx_result=default_sign_result())
    with pytest.raises(DeviceLockedError) as excinfo:
        make_signer(commands).sign_unsigned(PSBT_B64)
    message = str(excinfo.value)
    assert "jade" in message.lower()
    assert "recovery phrase login" in message.lower()
    assert "qr pin unlock" in message.lower()
    assert "retry" in message.lower()
    assert "pin/passphrase" not in message  # not the generic line
    assert ("signtx",) not in commands.calls


def test_host_pin_device_gets_companion_flow_guidance():
    """needs_pin_sent=True (locked Trezor shape) → host-pin-class guidance:
    unlock through the device's own app/companion flow. We do NOT relay
    promptpin/sendpin (out of scope, ADR-0015 amendment)."""
    commands = FakeCommands(devices=[dict(TREZOR_LOCKED)], signtx_result=default_sign_result())
    with pytest.raises(DeviceLockedError) as excinfo:
        make_signer(commands).sign_unsigned(PSBT_B64)
    message = str(excinfo.value)
    assert "companion" in message.lower()
    assert "retry" in message.lower()
    assert "jade" not in message.lower()
    assert "pin/passphrase" not in message  # not the generic line either
    assert ("signtx",) not in commands.calls


def test_old_shape_locked_entry_keeps_generic_guidance():
    """Old-shape entries (no needs_pin_sent/error keys — e.g. mocked fixtures
    predating TCK-HW-001) keep the existing generic locked guidance."""
    old_shape = {"type": "ledger", "path": "hid:/dev/hidL", "model": "ledger_nano_s"}
    commands = FakeCommands(devices=[old_shape], signtx_result=default_sign_result())
    with pytest.raises(DeviceLockedError) as excinfo:
        make_signer(commands).sign_unsigned(PSBT_B64)
    assert "pin/passphrase" in str(excinfo.value).lower()


def test_bare_jade_error_user_cancelled_maps_to_canceled():
    """A Jade device-side cancel escapes get_client as a bare jadepy
    JadeError(code=-32000) — unwrapped by hwilib — and must surface as the
    canceled guidance, never the generic unknown-error branch. The JadeError
    message text (which may carry device values) never leaks."""
    commands = FakeCommands(
        get_client_error=JadeError(-32000, "User Canceled", None),
        signtx_result=default_sign_result(),
    )
    with pytest.raises(DeviceError) as excinfo:
        make_signer(commands).sign_unsigned(PSBT_B64)
    message = str(excinfo.value).lower()
    assert "canceled" in message
    assert "retry" in message
    assert "jadeerror" not in message  # not the class-name fallback branch
    assert ("signtx",) not in commands.calls


def test_bare_jade_error_other_code_stays_generic():
    """The cancel guard is code-scoped: a non-cancel JadeError still lands in
    the class-name-only generic branch."""
    commands = FakeCommands(
        get_client_error=JadeError(-32002, "HW locked", None),
        signtx_result=default_sign_result(),
    )
    with pytest.raises(DeviceError) as excinfo:
        make_signer(commands).sign_unsigned(PSBT_B64)
    assert "JadeError" in str(excinfo.value)


# --------------------------------------------------------------------------
# Result normalization & HWI version tolerance
# --------------------------------------------------------------------------


def test_sign_accepts_bytes_result_dict():
    commands = FakeCommands(signtx_result={"psbt": base64.b64decode(SIGNED_B64)})
    result = make_signer(commands).sign_unsigned(PSBT_B64)
    assert result.psbt_base64 == SIGNED_B64


def test_sign_accepts_bare_str_result():
    commands = FakeCommands(signtx_result="  " + SIGNED_B64 + "  \n")
    result = make_signer(commands).sign_unsigned(PSBT_B64)
    assert result.psbt_base64 == SIGNED_B64


def test_sign_accepts_bare_bytes_result():
    commands = FakeCommands(signtx_result=base64.b64decode(SIGNED_B64))
    result = make_signer(commands).sign_unsigned(PSBT_B64)
    assert result.psbt_base64 == SIGNED_B64


def test_sign_unexpected_result_shape_is_value_free_error():
    commands = FakeCommands(signtx_result={"nope": True})
    with pytest.raises(DeviceError) as excinfo:
        make_signer(commands).sign_unsigned(PSBT_B64)
    assert SIGNED_B64 not in str(excinfo.value)


def test_sign_retries_with_bytes_for_older_hwi():
    """Older HWI lines take raw PSBT bytes — TypeError on str triggers the
    bytes retry with the base64-decoded input."""
    commands = FakeCommands(signtx_result=default_sign_result(), signtx_bytes_only=True)
    result = make_signer(commands).sign_unsigned(PSBT_B64)
    assert result.psbt_base64 == SIGNED_B64
    signed_calls = [c for c in commands.calls if c[0] == "signtx"]
    # First attempt (str) hit the TypeError; the bytes retry succeeded.
    assert len(signed_calls) == 2
    assert signed_calls[0][1] == PSBT_B64
    assert signed_calls[1][1] == base64.b64decode(PSBT_B64)


def test_sign_rejects_non_string_or_empty_psbt():
    signer = make_signer(FakeCommands())
    with pytest.raises(SignerError):
        signer.sign_unsigned(b"psbt\xff")  # bytes, not base64 text
    with pytest.raises(SignerError):
        signer.sign_unsigned("   ")


def test_client_none_is_absent_error():
    commands = FakeCommands(client=None)
    with pytest.raises(DeviceAbsentError):
        make_signer(commands).sign_unsigned(PSBT_B64)


# --------------------------------------------------------------------------
# Post-open account-key bind (TOCTOU closed by construction, ADR-0015
# amendment #2; rider R1 kept fail-closed)
# --------------------------------------------------------------------------


def test_post_open_account_key_match_signs():
    """The open client serves the wallet's account key at the account
    path → gate passes and signing proceeds."""
    commands = FakeCommands(
        client=FakeAccountClient({}, _ACCOUNT_PUBKEY),
        signtx_result=default_sign_result(),
    )
    result = make_signer(commands).sign_unsigned(PSBT_B64)
    assert result.psbt_base64 == SIGNED_B64


def test_post_open_account_key_mismatch_is_hard_stop():
    """A device swapped in between enumerate and open (different ACCOUNT
    key) NEVER signs — the enumerate→open window is closed by binding on
    the open client, not the enumeration."""
    commands = FakeCommands(
        client=FakeAccountClient({}, _OTHER_PUBKEY),
        signtx_result=default_sign_result(),
    )
    with pytest.raises(DeviceMismatchError) as excinfo:
        make_signer(commands).sign_unsigned(PSBT_B64)
    assert "does not match this wallet" in str(excinfo.value)
    assert ("signtx",) not in commands.calls  # hard stop before signing


def test_post_open_missing_getter_fails_closed_even_injected():
    """A client that cannot serve the account key at all (no
    get_pubkey_at_path) fails closed — the check is NEVER skipped, not
    even for an injected test fake (R1, tightened by TCK-HW-002: the old
    injected-fake skip is gone, there is one path)."""
    commands = FakeCommands(
        client=FakeClient({}), signtx_result=default_sign_result()
    )
    signer = make_signer(commands)
    with pytest.raises(DeviceError) as excinfo:
        signer.sign_unsigned(PSBT_B64)
    assert "re-verify" in str(excinfo.value)
    assert "reconnect" in str(excinfo.value)
    assert ("signtx",) not in commands.calls  # hard stop before signing


def test_post_open_unusable_account_key_shape_fails_closed():
    """A getter answer without a 33-byte compressed pubkey is an unusable
    shape — refusal, never a guess."""
    commands = FakeCommands(
        client=FakeAccountClient({}, b"\x02short"),
        signtx_result=default_sign_result(),
    )
    with pytest.raises(DeviceError) as excinfo:
        make_signer(commands).sign_unsigned(PSBT_B64)
    assert "re-verify" in str(excinfo.value)
    assert ("signtx",) not in commands.calls


def test_post_open_account_key_read_error_maps_to_guidance():
    commands = FakeCommands(
        client=FakeAccountClient({}, DeviceNotReadyError("locked mid-flight")),
        signtx_result=default_sign_result(),
    )
    with pytest.raises(DeviceLockedError) as excinfo:
        make_signer(commands).sign_unsigned(PSBT_B64)
    assert "pin/passphrase" in str(excinfo.value).lower()


def test_post_open_recheck_also_gates_display_address():
    commands = FakeCommands(
        client=FakeAccountClient({}, _OTHER_PUBKEY),
        display_result={"address": "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"},
    )
    with pytest.raises(DeviceMismatchError):
        make_signer(commands).display_address("wpkh([a1b2c3d4/84'/0'/0']zpub)")


def test_post_open_reverify_failure_closes_client():
    """R2: when the account-key bind raises (read error mid-flight), the
    opened device handle is still released — no handle leak."""
    recorder: dict = {}
    commands = FakeCommands(
        client=FakeAccountClient(
            recorder, ActionCanceledError("canceled at the getter")
        ),
        signtx_result=default_sign_result(),
    )
    with pytest.raises(DeviceError):
        make_signer(commands).sign_unsigned(PSBT_B64)
    assert recorder["closed"] is True
    assert ("signtx",) not in commands.calls


# --------------------------------------------------------------------------
# Rider R2: bytes-branch emptiness check in _normalize_signed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("empty", [b"", bytearray()])
def test_sign_empty_bytes_result_is_device_error(empty):
    """``{"psbt": b""}`` and a bare empty bytes response are errors — a
    device that reports success but returns no PSBT is not an empty tx."""
    commands = FakeCommands(signtx_result={"psbt": empty})
    with pytest.raises(DeviceError) as excinfo:
        make_signer(commands).sign_unsigned(PSBT_B64)
    assert SIGNED_B64 not in str(excinfo.value)  # value-free
    commands = FakeCommands(signtx_result=empty)
    with pytest.raises(DeviceError):
        make_signer(commands).sign_unsigned(PSBT_B64)


# --------------------------------------------------------------------------
# Rider R3: enumerate dict-shaping inside the mapped error boundary
# --------------------------------------------------------------------------


def test_enumerate_non_list_result_is_device_error():
    """A non-list enumeration result (e.g. a dict) surfaces as guidance —
    never a raw AttributeError/TypeError from field access."""
    commands = FakeCommands()
    commands.enumerate = lambda password=None: {"unexpected": "shape"}
    with pytest.raises(DeviceError) as excinfo:
        make_signer(commands).sign_unsigned(PSBT_B64)
    assert "unexpected response" in str(excinfo.value).lower()


def test_enumerate_non_dict_entry_is_device_error():
    commands = FakeCommands()

    def bad_shape(password=None):
        return ["not-a-dict", None]

    commands.enumerate = bad_shape
    with pytest.raises(DeviceError):
        make_signer(commands).enumerate_devices()


def test_enumerate_non_iterable_result_is_device_error():
    commands = FakeCommands()
    commands.enumerate = lambda password=None: 7
    with pytest.raises(DeviceError):
        make_signer(commands).enumerate_devices()


# --------------------------------------------------------------------------
# Error mapping (locked / busy / canceled / unknown) — guidance asserted
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hwilib_error", "expected_fragment"),
    [
        (DeviceNotReadyError("Trezor is locked. Unlock by using 'promptpin'."), "pin/passphrase"),
        (DeviceBusyError("device is busy"), "busy"),
        (ActionCanceledError("signtx canceled"), "canceled"),
        (DeviceConnectionError("Device disconnected"), "plug in"),
    ],
)
def test_known_hwi_errors_map_to_guidance(hwilib_error, expected_fragment):
    commands = FakeCommands(signtx_error=hwilib_error)
    with pytest.raises(DeviceError) as excinfo:
        make_signer(commands).sign_unsigned(PSBT_B64)
    assert expected_fragment in str(excinfo.value).lower()
    assert "retry" in str(excinfo.value).lower()


def test_unknown_hwi_error_names_class_only():
    """Unmapped errors surface the exception CLASS NAME only — never the
    underlying message, which may carry device- or PSBT-derived values."""
    commands = FakeCommands(
        signtx_error=BadArgumentError("leaked amount 12345 and address tb1qXYZ")
    )
    with pytest.raises(DeviceError) as excinfo:
        make_signer(commands).sign_unsigned(PSBT_B64)
    message = str(excinfo.value)
    assert "BadArgumentError" in message
    assert "12345" not in message
    assert "tb1qXYZ" not in message


def test_bad_psbt_error_is_value_free():
    commands = FakeCommands(signtx_error=PSBTSerializationError("Size of key was not the expected size"))
    with pytest.raises(DeviceError) as excinfo:
        make_signer(commands).sign_unsigned(PSBT_B64)
    message = str(excinfo.value)
    assert "read the transaction" in message
    assert "Size of key" not in message  # hwilib message text never leaks


def test_all_error_messages_value_free():
    """No fingerprint, PSBT text, or amount ever appears in any guidance."""
    leaky_fragment = FP_OTHER[:4]
    for error in (
        DeviceNotReadyError("locked"),
        DeviceBusyError("busy"),
        ActionCanceledError("canceled"),
        BadArgumentError(FP_WALLET),
    ):
        # Matching device so the gate passes and signtx raises the error.
        commands = FakeCommands(
            devices=[dict(DEVICE_WALLET)], signtx_error=error
        )
        try:
            make_signer(commands).sign_unsigned(PSBT_B64)
        except DeviceError as exc:
            message = str(exc)
            assert FP_WALLET not in message
            assert leaky_fragment not in message
            assert SIGNED_B64 not in message
        else:
            pytest.fail("expected a DeviceError from the mapped error boundary")


# --------------------------------------------------------------------------
# display_address (best-effort verify-on-device)
# --------------------------------------------------------------------------


def test_display_address_happy_path_uses_descriptor():
    commands = FakeCommands(display_result={"address": "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"})
    signer = make_signer(commands)
    address = signer.display_address("wpkh([a1b2c3d4/84'/1'/0']vpub)")
    assert address == "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"  # verbatim
    assert commands.rec["closed"] is True
    display_calls = [c for c in commands.calls if c[0] == "displayaddress"]
    assert len(display_calls) == 1
    assert display_calls[0][1].startswith("wpkh(")  # desc=, not path=


def test_display_address_respects_fingerprint_gate():
    """Address display binds the account key too — never the wrong
    device's screen."""
    commands = FakeCommands(
        devices=[dict(DEVICE_OTHER)],
        client=FakeAccountClient({}, _OTHER_PUBKEY),
    )
    with pytest.raises(DeviceMismatchError):
        make_signer(commands).display_address("wpkh([a1b2c3d4/84'/0'/0']zpub)")


def test_display_address_errors_map_to_guidance_never_silent():
    commands = FakeCommands(display_error=UnavailableActionError("display not supported"))
    with pytest.raises(DeviceError) as excinfo:
        make_signer(commands).display_address("wpkh([a1b2c3d4/84'/1'/0']vpub)")
    assert "retry" in str(excinfo.value).lower()


def test_display_address_unexpected_shape_is_error():
    commands = FakeCommands(display_result={"unexpected": 1})
    with pytest.raises(DeviceError):
        make_signer(commands).display_address("wpkh([a1b2c3d4/84'/1'/0']vpub)")


# --------------------------------------------------------------------------
# No-network lint compliance
# --------------------------------------------------------------------------


def _load_lint():
    import importlib.util

    lint_path = REPO_ROOT / "tools" / "lint_network.py"
    spec = importlib.util.spec_from_file_location("lint_network_hwi", lint_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_hwi_signer_lints_network_clean():
    """signer/hwi.py introduces no network imports (USB/HID is device I/O)."""
    lint = _load_lint()
    violations = lint.check_tree(REPO_ROOT / "src" / "localwallet" / "signer")
    assert violations == []


# --------------------------------------------------------------------------
# Live integration (env-gated; real hwilib wheel, HID enumeration only)
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("LOCALWALLET_HWI_LIVE") != "1",
    reason="set LOCALWALLET_HWI_LIVE=1 to run the real-hwilib enumerate check",
)
def test_live_hwi_enumerate_no_device_is_absent_error():
    """Real wheel, real HID enumeration, no hardware interaction beyond
    enumerate: with no device plugged in, signing refuses via the absent
    path — or mismatch/locked if a device happens to be attached (a locked
    device cannot prove its fingerprint either way)."""
    signer = HwiUsbSigner(FP_WALLET, ACCOUNT_PATH)  # real lazy import of hwilib.commands
    with pytest.raises((DeviceAbsentError, DeviceMismatchError, DeviceLockedError)):
        signer.sign_unsigned(PSBT_B64)

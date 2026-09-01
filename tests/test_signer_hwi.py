"""HwiUsbSigner tests (TCK-P3-003, ADR-0015).

Covers: lazy hwilib import (module imports without the wheel; missing wheel
→ HwiUnavailableError); enumerate mapping (hex/bytes fingerprints, unreadable
devices); the fingerprint trust gate (1 match → sign; 0 devices → absent;
devices-but-different-fingerprint → mismatch hard stop; unreadable-only →
locked; 2 matches → refuse); str/bytes sign-result normalization; older-HWI
bytes-input retry; locked/busy/canceled/unknown error mapping (guidance
strings asserted, value-free); display_address best-effort mapping;
SignedResult fields; no-network lint compliance.

hwilib is faked via the ``commands_module`` constructor seam — the fake
mirrors the empirically verified hwi 3.2.0 API surface (commands.enumerate /
get_client / signtx / displayaddress). One env-gated test exercises the real
wheel (HID enumeration only — device I/O, not network).
"""

import base64
import os
import subprocess
import sys
from pathlib import Path

import pytest

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

# Distinct fixture fingerprints (value-free checks assert these NEVER leak
# into error messages). "wallet" is the descriptor's; "other" is a decoy.
FP_WALLET = "a1b2c3d4"
FP_OTHER = "deadbeef"

# Minimal well-formed-looking base64 PSBT text (fake hwilib never parses it).
PSBT_B64 = base64.b64encode(b"psbt\xff" + b"\x01" * 24).decode("ascii")
SIGNED_B64 = base64.b64encode(b"psbt\xff" + b"\x02" * 24).decode("ascii")

DEVICE_WALLET = {
    "type": "trezor",
    "path": "hid:/dev/hid0",
    "model": "trezor_t",
    "fingerprint": FP_WALLET,
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


class FakeClient:
    """HardwareWalletClient stand-in that records close() calls."""

    def __init__(self, recorder: dict) -> None:
        self.recorder = recorder

    def close(self) -> None:
        self.recorder["closed"] = True


class FakeCommands:
    """hwilib.commands stand-in (enumerated API shape of hwi 3.2.0)."""

    def __init__(
        self,
        devices: list[dict] | None = None,
        enumerate_error: Exception | None = None,
        client: FakeClient | None | object = "auto",
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
            return FakeClient(self.__dict__.setdefault("rec", {}))
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
    return HwiUsbSigner(fingerprint, commands_module=commands)


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
    signer = HwiUsbSigner(FP_WALLET)  # no commands_module → real lazy import
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
        HwiUsbSigner("")  # empty fingerprint
    with pytest.raises(SignerError):
        HwiUsbSigner(FP_WALLET, chain="mainnet")  # unknown chain name


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
            fingerprint_hex=FP_WALLET,
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
# Fingerprint trust gate (ADR-0015)
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


def test_sign_gets_testnet4_chain():
    """The device client is opened for the project's chain (ADR-0004)."""
    commands = FakeCommands(signtx_result=default_sign_result())
    make_signer(commands).sign_unsigned(PSBT_B64)
    chain = next(c for c in commands.calls if c[0] == "get_client")[3]
    try:
        from hwilib.common import Chain

        assert chain == Chain.TESTNET4
    except ImportError:  # hwilib absent → lenient string fallback
        assert chain == "testnet4"


def test_zero_devices_is_absent_error():
    commands = FakeCommands(devices=[])
    signer = make_signer(commands)
    with pytest.raises(DeviceAbsentError) as excinfo:
        signer.sign_unsigned(PSBT_B64)
    assert "plug in" in str(excinfo.value).lower()
    assert ("signtx",) not in commands.calls


def test_devices_present_none_match_is_mismatch_hard_stop():
    """A different-fingerprint device NEVER signs (wrong wallet / typo /
    attack — ADR-0015 hard stop)."""
    commands = FakeCommands(devices=[dict(DEVICE_OTHER)], signtx_result=default_sign_result())
    signer = make_signer(commands)
    with pytest.raises(DeviceMismatchError) as excinfo:
        signer.sign_unsigned(PSBT_B64)
    assert "does not match this wallet" in str(excinfo.value)
    # Hard stop: the signing command was never reached.
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
    """Address display opens the fingerprint-matched device too — never the
    wrong device's screen."""
    commands = FakeCommands(devices=[dict(DEVICE_OTHER)])
    with pytest.raises(DeviceMismatchError):
        make_signer(commands).display_address("wpkh([a1b2c3d4/84'/1'/0']vpub)")


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
    path (or mismatch if a device happens to be attached)."""
    signer = HwiUsbSigner(FP_WALLET)  # real lazy import of hwilib.commands
    with pytest.raises((DeviceAbsentError, DeviceMismatchError)):
        signer.sign_unsigned(PSBT_B64)

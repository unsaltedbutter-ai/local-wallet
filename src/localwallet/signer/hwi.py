"""HWI-USB signer: hardware wallets over USB via HWI-as-a-library (TCK-P3-003).

Implements :class:`localwallet.signer.base.Signer` for USB-connected devices
(Ledger, Trezor, Coldcard, Jade, BitBox02, KeepKey) using Bitcoin Core's HWI
as a Python library — the Specter-Desktop pattern (PROJECT.md §7.6) — never
as a CLI subprocess.

Flow (PROJECT.md §7.6, ADR-0015 amendments #2 + #3): ``enumerate → open
candidate → account-key fingerprint bind (at the descriptor's account
path) → derivation-fingerprint patch (account fp → device master fp, hint
metadata only) → display address / sign``.

Layering and trust (AGENTS.md invariants):

- **Lazy import.** ``hwilib`` is imported on first use, not at module import
  (same pattern as ``agent/runtime.py``'s llama import), so this module
  imports cleanly without the wheel; tests inject a fake commands module via
  the ``commands_module`` constructor seam.
- **Account-key fingerprint trust is a hard gate (ADR-0015 + amendment #2 /
  OQ18).** The binding check runs on the OPEN client: it must serve the
  wallet's account public key at the descriptor's account derivation path
  (e.g. ``m/84'/0'/0'``), and that key's fingerprint
  (``hash160(pubkey)[:4]``, BIP 32) must equal the wallet's expected
  fingerprint EXACTLY. The fingerprint ``enumerate`` reports is the
  device's MASTER fingerprint — unknowable to a watch-only account-level
  wallet — so enumeration only narrows candidates (readable vs locked)
  and never decides trust. Zero matches and multiple matches both refuse;
  a mismatch is a hard stop (wrong wallet, typo'd descriptor, or attack).
  A client that cannot serve the account key fails closed — the check is
  never skipped. Full device registration (OQ18) remains the fuller
  future anchor. The master fingerprint IS read from the bound client at
  sign time — solely to rewrite PSBT derivation hints
  (ADR-0015 amendment #3, :meth:`_patch_derivations_to_master`); it
  never decides trust and never gates selection.
- **No re-validation here.** The signed PSBT returned by the device is
  transported verbatim; deterministic re-validation against the intended
  transaction is owned by ``localwallet.tx.revalidate`` before broadcast
  (PROJECT.md §7.5). This module deliberately does not duplicate that gate.
- **No secrets.** The app is watch-only; keys live on the device. Device
  PINs/passphrases are entered ON the device (or via HWI's own flows). This
  layer never accepts, stores, forwards, or logs a PIN or passphrase: the
  ``password`` parameter is omitted on every HWI call (no secret exists to
  pass). A device that needs unlocking surfaces as
  :class:`DeviceLockedError` guidance instead. Jade unlock is the exception in
  transport, not in trust: hwilib relays *blinded* PIN-server blobs through the
  host while the user enters the scrambled PIN on-device, and the PIN itself
  never touches the host process.
- **No network I/O** (lint-enforced). USB/HID transport is device I/O; the
  only networked module remains ``localwallet.chain``. (Transitive exception:
  the Jade unlock above has *hwilib* — not this module, no import in our tree —
  reach its pinserver for blinded blobs; ADR-0015 amendment.)
- **Value-free errors.** Messages never echo PSBT content, addresses,
  amounts, or fingerprints — they name the situation and the next user
  action only (PROJECT.md §10 tone: short, actionable, zero jargon).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from embit.hashes import hash160
from embit.psbt import PSBT

from localwallet.signer.base import SignedResult, Signer, SignerError

__all__ = [
    "DeviceAbsentError",
    "DeviceBusyError",
    "DeviceError",
    "DeviceInfo",
    "DeviceLockedError",
    "DeviceMismatchError",
    "HwiUnavailableError",
    "HwiUsbSigner",
]

# --------------------------------------------------------------------------
# Error hierarchy — all device failures are SignerError subclasses with
# §10-toned, value-free guidance ("enter your PIN on the device, then say
# 'retry'"). Messages never contain fingerprints, PSBTs, or amounts.
# --------------------------------------------------------------------------


class DeviceError(SignerError):
    """A hardware-wallet operation failed; guidance tells the user what to do."""


class DeviceAbsentError(DeviceError):
    """No usable device is connected."""


class DeviceLockedError(DeviceError):
    """The device is locked or uninitialized; the user must unlock it on-device."""


class DeviceBusyError(DeviceError):
    """The device is mid-operation; the user must finish/dismiss on-device."""


class DeviceMismatchError(DeviceError):
    """A device is connected but it is not this wallet's device (ADR-0015)."""


class HwiUnavailableError(DeviceError):
    """The ``hwi`` package is not installed in this environment."""


# Guidance strings (§10: short, actionable, zero jargon, value-free).
_MSG_INSTALL = (
    "Hardware-wallet support is not installed. "
    "Install the app's hardware-wallet support, then say 'retry'."
)
_MSG_NO_DEVICES = (
    "No device found — plug in and unlock your device, then say 'retry'."
)
_MSG_LOCKED = (
    "Enter your PIN/passphrase on the device, then say 'retry'."
)
_MSG_JADE_LOCKED = (
    "Your Jade is locked — unlock it on the Jade screen and follow its PIN "
    "prompt. If it offers menu options, choose 'Recovery Phrase Login' or "
    "'QR PIN Unlock', then say 'retry'."
)
_MSG_HOST_PIN = (
    "This device unlocks through its own app or companion software — unlock "
    "it there, then say 'retry'."
)
_MSG_BUSY = (
    "Your device is busy — finish what's on its screen, then say 'retry'."
)
_MSG_CANCELED = (
    "The request was canceled on your device — say 'retry' to try again."
)
_MSG_MISMATCH = (
    "No device with this wallet's fingerprint was found — the connected "
    "device does not match this wallet. Plug in the device set up for this "
    "wallet, then say 'retry'."
)
_MSG_MULTIPLE = (
    "More than one device matches this wallet's fingerprint — unplug the "
    "extra device(s), keep one connected, then say 'retry'."
)
_MSG_BAD_PSBT = (
    "Your device could not read the transaction — say 'retry'; if it keeps "
    "failing, rebuild the transaction."
)
_MSG_CLIENT_GONE = (
    "The device could not be opened — plug it in and unlock it, then say "
    "'retry'."
)
_MSG_REVERIFY = (
    "Could not re-verify the device fingerprint — unplug, reconnect, "
    "and retry."
)
_MSG_UNEXPECTED = (
    "The device returned an unexpected response — say 'retry' to try again."
)

# hwilib exception class names → (our subclass, guidance). Name-based
# matching keeps the mapping stable across hwilib versions and works with
# injected test fakes; real-hwilib subclasses still fall through to the
# generic branch. Messages are value-free; the class carries the category.
_HWI_ERROR_MAP: dict[str, tuple[type[DeviceError], str]] = {
    "DeviceBusyError": (DeviceBusyError, _MSG_BUSY),
    "ActionCanceledError": (DeviceError, _MSG_CANCELED),
    "DeviceNotReadyError": (DeviceLockedError, _MSG_LOCKED),
    "NoPasswordError": (DeviceLockedError, _MSG_LOCKED),
    "DeviceConnectionError": (DeviceAbsentError, _MSG_NO_DEVICES),
    "PSBTSerializationError": (DeviceError, _MSG_BAD_PSBT),
}

# Allowed chain names (resolved to hwilib's Chain enum lazily). Mainnet is
# the project default (mainnet-only invariant, ADR-0021).
_CHAIN_NAMES = ("main", "test", "testnet4", "signet", "regtest")
_DEFAULT_CHAIN = "main"


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """One enumerated hardware device (subset of the hwilib enumerate dict).

    ``fingerprint_hex`` is the lowercase hex MASTER-key fingerprint hwilib
    reports, or ``None`` when the device could not be read
    (locked/uninitialized) — locked-class guidance branches on that (ADR-0015
    amendment #2: the master fingerprint is unknowable to a watch-only
    account-level wallet, so this value never decides trust).
    ``needs_pin_sent``/``needs_passphrase_sent``/``locked`` carry the rest
    of the enumerate shape ``_select_candidates`` branches on for unlock
    guidance; defaults keep old-shape (mocked) entries valid.
    """

    type: str
    model: str
    path: str
    fingerprint_hex: str | None
    needs_pin_sent: bool = False
    needs_passphrase_sent: bool = False
    locked: bool = False


def _normalize_fingerprint(value: Any) -> str | None:
    """hwilib fingerprints as lowercase hex; accept str (3.x) or bytes."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    text = str(value).strip().lower()
    return text or None


class HwiUsbSigner(Signer):
    """USB hardware-wallet signer over HWI-as-a-library.

    Args:
        expected_wallet_fingerprint: the wallet's ACCOUNT-key fingerprint
            as a hex string (the descriptor origin fingerprint —
            ``parsed.hd_key.my_fingerprint``). The signing device MUST
            prove control of the key with this fingerprint at the account
            path (ADR-0015 + amendment #2).
        account_path: the device-side BIP 32 account derivation path the
            descriptor's origin carries (e.g. ``"m/84'/0'/0'"``) — where
            the device is asked for the account public key. Required: a
            signer without it cannot bind a device to the wallet's key
            origin, so there is no fallback path (fail closed at
            construction).
        chain: which chain the device client is opened for; default
            ``"main"`` (mainnet-only invariant, ADR-0021). One of
            ``main``/``test``/``testnet4``/
            ``signet``/``regtest``.
        commands_module: dependency seam — the ``hwilib.commands``-shaped
            module to call into. ``None`` (default) lazily imports
            ``hwilib.commands`` on first use; tests inject a fake.
    """

    def __init__(
        self,
        expected_wallet_fingerprint: str,
        account_path: str,
        chain: str = _DEFAULT_CHAIN,
        commands_module: Any | None = None,
    ) -> None:
        if not isinstance(expected_wallet_fingerprint, str) or not (
            expected_wallet_fingerprint.strip()
        ):
            raise SignerError("expected_wallet_fingerprint must be a hex string")
        if not isinstance(account_path, str) or not account_path.strip():
            raise SignerError("account_path must be a BIP 32 path string")
        if chain not in _CHAIN_NAMES:
            raise SignerError(
                "chain must be one of: " + ", ".join(_CHAIN_NAMES)
            )
        self.expected_wallet_fingerprint = expected_wallet_fingerprint.strip().lower()
        self.account_path = account_path.strip()
        self.chain = chain
        self._commands = commands_module

    @property
    def name(self) -> str:
        return "hwi"

    # -- hwilib access ----------------------------------------------------

    def _ensure_commands(self) -> Any:
        """Lazily import hwilib.commands (first use only).

        The import lives here — not at module top level — so this module
        imports cleanly without the wheel (same pattern as the agent
        runtime's llama import).
        """
        if self._commands is not None:
            return self._commands
        try:
            import hwilib.commands as commands_mod  # deliberate lazy import
        except ImportError as exc:
            raise HwiUnavailableError(_MSG_INSTALL) from exc
        self._commands = commands_mod
        return self._commands

    def _chain_enum(self, commands: Any) -> Any:
        """Resolve the chain name to hwilib's Chain enum (lazy, lenient).

        Falls back to the raw name if hwilib's Chain enum is unavailable
        (injected fakes need not provide it).
        """
        try:
            from hwilib.common import Chain  # lazy, alongside commands
        except ImportError:
            return self.chain
        return getattr(Chain, self.chain.upper(), self.chain)

    # -- enumerate ----------------------------------------------------------

    def enumerate_devices(self) -> list[DeviceInfo]:
        """Enumerate connected devices (wraps hwilib ``enumerate``).

        Returns one :class:`DeviceInfo` per device hwilib can reach;
        devices that could not be read (locked/uninitialized) come back
        with ``fingerprint_hex=None``. Device-side secrets are never
        involved: the ``password`` parameter is omitted on the HWI call
        (no secret).

        The raw enumeration is shaped INSIDE the mapped error boundary: a
        non-list result or a non-dict entry surfaces as guidance
        (:class:`DeviceError`), never as a raw ``AttributeError``/
        ``TypeError`` from field access.

        Raises:
            DeviceError (hierarchy): hwilib-level enumeration failures,
                mapped to user guidance.
        """
        commands = self._ensure_commands()
        try:
            raw_devices = commands.enumerate(password=None)
        except Exception as exc:
            raise self._map_hwi_error(exc) from exc
        if not isinstance(raw_devices, list):
            raise DeviceError(_MSG_UNEXPECTED)
        devices: list[DeviceInfo] = []
        for raw in raw_devices:
            if not isinstance(raw, dict):
                raise DeviceError(_MSG_UNEXPECTED)
            # ponytail: with hwilib's networking enabled, a locked Jade blocks
            # client construction (enumerate and get_client alike) while the
            # user enters the PIN — hwi's auth_user loop runs long_timeout.
            # Acceptable for the single-user CLI; revisit if this ever runs
            # headless.
            devices.append(
                DeviceInfo(
                    type=str(raw.get("type", "device")),
                    model=str(raw.get("model") or raw.get("type") or "device"),
                    path=str(raw.get("path", "")),
                    fingerprint_hex=_normalize_fingerprint(raw.get("fingerprint")),
                    needs_pin_sent=bool(raw.get("needs_pin_sent", False)),
                    needs_passphrase_sent=bool(raw.get("needs_passphrase_sent", False)),
                    locked="error" in raw,
                )
            )
        return devices

    # -- fingerprint trust gate (ADR-0015 + amendment #2) ------------------

    def _select_candidates(self, devices: list[DeviceInfo]) -> list[DeviceInfo]:
        """Narrow the enumeration to openable candidates (pre-open, advisory).

        The enumerated fingerprint is the device's MASTER fingerprint,
        which a watch-only account-level wallet can never predict
        (ADR-0015 amendment #2) — so this step filters by READABILITY
        only and never decides trust:

        - zero devices → DeviceAbsentError;
        - all devices unreadable (locked/uninitialized) → DeviceLockedError
          with class-specific guidance (the right device must not be
          slandered as a mismatch before it can identify itself);
        - readable devices → returned as candidates, each opened and bound
          by :meth:`_reverify_fingerprint`.

        ponytail: every readable candidate is opened per attempt to test
        its account key; OQ18 device registration will let enumeration
        pre-select instead.
        """
        if not devices:
            raise DeviceAbsentError(_MSG_NO_DEVICES)
        candidates = [d for d in devices if d.fingerprint_hex is not None]
        if not candidates:
            # Present but unreadable: locked or uninitialized device —
            # guidance depends on how that device class unlocks.
            if any(d.type.startswith("jade") for d in devices):
                # Jade never takes a host-side PIN: hwi drives an
                # on-device scrambled PIN pad (blinded pinserver relay).
                # The generic line below loops forever here (TCK-HW-001).
                raise DeviceLockedError(_MSG_JADE_LOCKED)
            if any(d.needs_pin_sent for d in devices):
                # Host-driven PIN class (e.g. locked Trezor): unlocking is
                # driven through the device's own app/companion flow;
                # relaying promptpin/sendpin is out of scope (ADR-0015
                # amendment).
                raise DeviceLockedError(_MSG_HOST_PIN)
            raise DeviceLockedError(_MSG_LOCKED)
        return candidates

    def _client_account_fingerprint(self, client: Any) -> str:
        """The device's fingerprint AT THE WALLET'S ACCOUNT PATH.

        hwi 3.2.0 clients implement ``get_pubkey_at_path`` (base
        ``Client`` contract, ``hwilib/hwwclient.py``; JadeClient at
        ``hwilib/devices/jade.py:164`` returns an ``ExtendedKey`` whose
        ``pubkey`` is the compressed account public key). The account
        key's own fingerprint is ``hash160(pubkey)[:4]`` (BIP 32) —
        exactly what the descriptor's origin carries.

        Fail-closed (R1): a client that does not expose the getter, or
        returns an unusable key, raises :class:`DeviceError` — the check
        is NEVER skipped. Path errors map through the standard hierarchy.
        """
        getter = getattr(client, "get_pubkey_at_path", None)
        if not callable(getter):
            raise DeviceError(_MSG_REVERIFY)
        try:
            ext_key = getter(self.account_path)
        except Exception as exc:
            raise self._map_hwi_error(exc) from exc
        pubkey = getattr(ext_key, "pubkey", None)
        if not isinstance(pubkey, (bytes, bytearray)) or len(pubkey) != 33:
            raise DeviceError(_MSG_REVERIFY)
        return hash160(bytes(pubkey))[:4].hex()

    def _open_matched_client(self, commands: Any) -> tuple[Any, DeviceInfo]:
        """Open candidates and BIND each by account-key fingerprint (hard gate).

        Sequence (ADR-0015 + amendment #2): enumerate →
        :meth:`_select_candidates` (readability/locked guidance only) →
        per candidate: open a client, then the BINDING check — the OPEN
        client must serve the wallet's account key
        (:meth:`_reverify_fingerprint`). Because trust is decided on the
        open client, not on the enumeration, the enumerate→open TOCTOU
        window is closed by construction. Exactly one account-key match
        proceeds; zero → DeviceMismatchError; more than one → DeviceError
        (unplug extras).

        A mismatch NEVER falls through to signing — wrong wallet, typo'd
        descriptor, or attack all stop here. Every non-selected client
        handle is released (R2: no handle leaks on refusal paths).
        """
        matches: list[tuple[Any, DeviceInfo]] = []
        for device in self._select_candidates(self.enumerate_devices()):
            try:
                client = commands.get_client(
                    device.type, device.path, chain=self._chain_enum(commands)
                )
            except Exception as exc:
                self._close_all(matches)
                raise self._map_hwi_error(exc) from exc
            if client is None:
                self._close_all(matches)
                raise DeviceAbsentError(_MSG_CLIENT_GONE)
            try:
                bound = self._reverify_fingerprint(client)
            except Exception:
                # Read failure / fail-closed shape error: release this
                # handle AND every already-bound one (R2).
                self._close_client(client)
                self._close_all(matches)
                raise
            if bound:
                matches.append((client, device))
            else:
                self._close_client(client)
        if len(matches) == 1:
            return matches[0]
        self._close_all(matches)
        if not matches:
            raise DeviceMismatchError(_MSG_MISMATCH)
        raise DeviceError(_MSG_MULTIPLE)

    def _reverify_fingerprint(self, client: Any) -> bool:
        """Bind the OPEN client to the wallet's account key (hard gate).

        Returns ``True`` only when the client's fingerprint at the
        descriptor's account path equals the wallet's expected
        account-key fingerprint. A different key is NOT an error to
        raise here — with several candidates on the bus, the caller
        (:meth:`_open_matched_client`) must keep looking, and a device
        that matches nothing surfaces as the mismatch hard stop
        (_MSG_MISMATCH), never a bypass. Inability to serve the account
        key raises fail-closed (see :meth:`_client_account_fingerprint`).

        The signed PSBT itself remains bound by
        :mod:`localwallet.tx.revalidate` — this gate binds the DEVICE,
        revalidation binds the SIGNATURES (ADR-0015 amendment #2).
        """
        return (
            self._client_account_fingerprint(client) == self.expected_wallet_fingerprint
        )

    @classmethod
    def _close_all(cls, opened: list[tuple[Any, DeviceInfo]]) -> None:
        """Release every client handle in a ``(client, device)`` list."""
        for client, _device in opened:
            cls._close_client(client)

    @staticmethod
    def _close_client(client: Any) -> None:
        """Best-effort client close (device handles must be released)."""
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001, S110 — close must never mask the flow
                pass

    # -- error mapping ------------------------------------------------------

    @staticmethod
    def _map_hwi_error(exc: Exception) -> DeviceError:
        """Map a hwilib exception to our hierarchy with §10 guidance.

        Known class names get the specific subclass and guidance; anything
        else — including hwilib errors we have no mapping for — becomes a
        plain :class:`DeviceError` naming the exception class ONLY (never
        its message, which may carry device- or PSBT-derived values).
        """
        mapped = _HWI_ERROR_MAP.get(type(exc).__name__)
        if mapped is not None:
            error_cls, message = mapped
            return error_cls(message)
        # Jade device-side cancel escapes hwilib's wrappers as a bare
        # jadepy JadeError (code -32000 USER_CANCELLED) — name-matched like
        # the map above; the message may carry device text, so it is never
        # echoed (value-free invariant).
        if type(exc).__name__ == "JadeError" and getattr(exc, "code", None) == -32000:
            return DeviceError(_MSG_CANCELED)
        return DeviceError(
            f"Something went wrong talking to your device "
            f"({type(exc).__name__}) — say 'retry' to try again."
        )

    # -- sign ---------------------------------------------------------------

    def sign_unsigned(self, psbt_base64: str) -> SignedResult:
        """Sign a base64 PSBT on the fingerprint-matched device.

        Pipeline (ADR-0015 + amendments #2/#3): candidate enumeration →
        client open → POST-OPEN account-key fingerprint bind (closes the
        enumerate→open TOCTOU window by construction) → derivation
        fingerprint patch (account fp → device master fp on this wallet's
        bip32 derivations only; ADR-0015 amendment #3, see
        :meth:`_patch_derivations_to_master`) → hwilib ``signtx`` →
        normalize the device's response to base64 text →
        :class:`SignedResult`.
        The signed PSBT is returned verbatim for re-validation by
        ``localwallet.tx.revalidate`` — this module performs no output
        validation (layering: the revalidate module owns the gate).

        HWI version tolerance: the PSBT is passed as base64 **str** (hwi
        3.x); on ``TypeError`` the call is retried with raw **bytes**
        (older HWI shapes). Responses are accepted as
        ``{"psbt": <base64 str>}`` (hwi 3.x), a bare base64 str, or bytes —
        all normalized to base64 text.

        Raises:
            SignerError: non-string/empty PSBT input.
            DeviceError (hierarchy): fingerprint gate refusals and mapped
                device errors (value-free guidance).
        """
        if not isinstance(psbt_base64, str) or not psbt_base64.strip():
            raise SignerError("psbt_base64 must be non-empty base64 text")
        commands = self._ensure_commands()
        client, device = self._open_matched_client(commands)
        try:
            to_sign = self._patch_derivations_to_master(client, psbt_base64)
            result = self._signtx(commands, client, to_sign)
        finally:
            self._close_client(client)
        signed = self._normalize_signed(result)
        return SignedResult(
            psbt_base64=signed,
            signer_name=f"hwi:{device.model}",
            checksum_verified=False,
        )

    def _patch_derivations_to_master(self, client: Any, psbt_base64: str) -> str:
        """Rewrite this wallet's PSBT derivation fingerprints account-fp →
        device MASTER fp, just before signing (ADR-0015 amendment #3).

        BIP 174 convention: ``bip32_derivations`` carries the master-key
        fingerprint plus the full path from ``m/``. Our builder knows only
        the account fingerprint (watch-only — the master fp is not
        recoverable from a zpub), so it emits the account fp and this
        signer, holding an OPENED and account-key-BOUND client, asks it for
        ``get_master_fingerprint()`` and patches every input AND output
        ``bip32_derivations`` entry whose fingerprint equals THIS signer's
        expected account fingerprint. Nothing else is touched: paths and
        pubkeys are preserved verbatim, and entries with any other
        fingerprint (a different wallet mixed into the PSBT) are left
        alone — this is a targeted correction, never a blind overwrite.

        Trust impact: none. Derivation fields are SIGNER HINTS outside the
        BIP-143 digest; ``localwallet.tx.revalidate`` reads no ``bip32_
        derivations`` field at all, so the patch can neither fake nor hide
        a signature — it only lets the device recognize its own keys
        (unpatched, Jade signs nothing: "There are not relevant inputs
        to be signed").

        Fail-closed: a matched client that cannot report its master
        fingerprint aborts with value-free guidance instead of shipping a
        PSBT the device is known to refuse. A PSBT that does not parse (or
        carries no entries for this wallet's account fp) is never
        half-patched.

        # ponytail: patched at sign time because no device is open at
        # create time; OQ18 registration-lite (persist the master fp at
        # enrollment) removes the per-sign round-trip.
        """
        try:
            psbt = PSBT.parse(base64.b64decode(psbt_base64))
        except Exception as exc:  # containment: any parse shape → guidance
            raise DeviceError(_MSG_BAD_PSBT) from exc
        try:
            account_fp = bytes.fromhex(self.expected_wallet_fingerprint)
        except ValueError as exc:
            raise DeviceError(_MSG_REVERIFY) from exc
        targets = [
            derivation
            for scope in (*psbt.inputs, *psbt.outputs)
            for derivation in scope.bip32_derivations.values()
            if derivation.fingerprint == account_fp
        ]
        if not targets:
            return psbt_base64  # nothing of this wallet's to correct: verbatim
        getter = getattr(client, "get_master_fingerprint", None)
        if not callable(getter):
            raise DeviceError(_MSG_REVERIFY)
        try:
            raw = getter()
        except Exception as exc:
            raise self._map_hwi_error(exc) from exc
        master_hex = _normalize_fingerprint(raw)
        try:
            master_fp = bytes.fromhex(master_hex) if master_hex else None
        except ValueError:
            master_fp = None
        if master_fp is None or len(master_fp) != 4:
            raise DeviceError(_MSG_REVERIFY)
        for derivation in targets:
            derivation.fingerprint = master_fp
        return base64.b64encode(psbt.serialize()).decode("ascii")

    @staticmethod
    def _signtx(commands: Any, client: Any, psbt_base64: str) -> Any:
        """Call hwilib ``signtx`` (str in; bytes retry for older HWI).

        Every failure — from either attempt — is mapped through
        :meth:`_map_hwi_error`, so no raw hwilib exception escapes.
        """
        try:
            try:
                return commands.signtx(client, psbt_base64)
            except TypeError:
                # Older HWI accepted raw PSBT bytes rather than base64 str.
                return commands.signtx(client, base64.b64decode(psbt_base64))
        except Exception as exc:
            raise HwiUsbSigner._map_hwi_error(exc) from exc

    @staticmethod
    def _normalize_signed(result: Any) -> str:
        """Normalize a device signing response to base64 PSBT text.

        All branches fail closed on emptiness: ``{"psbt": b""}`` and a
        bare empty ``bytes`` are as unexpected as an empty string — a
        device that reports success but returns no PSBT is an error, not
        an empty transaction.
        """
        if isinstance(result, dict):
            result = result.get("psbt")
        if isinstance(result, (bytes, bytearray)):
            raw = bytes(result)
            if not raw:
                raise DeviceError(_MSG_UNEXPECTED)
            return base64.b64encode(raw).decode("ascii")
        if isinstance(result, str) and result.strip():
            return result.strip()
        raise DeviceError(_MSG_UNEXPECTED)

    # -- display address (verify-on-device, best-effort) ---------------------

    def display_address(self, descriptor: str) -> str:
        """Ask the matched device to derive and show an address on-screen.

        Best-effort verify-on-device support (PROJECT.md §10 "compare the
        address on your device screen"): HWI's ``displayaddress`` takes a
        derivation path or descriptor — devices derive and show the address
        themselves, so this takes the wallet **descriptor**, not a literal
        address string. Device support varies by model; failures map to
        guidance and are never silently swallowed.

        Returns the device-reported address verbatim (tool output — the
        caller narrates it verbatim; the model never invents addresses).
        """
        if not isinstance(descriptor, str) or not descriptor.strip():
            raise SignerError("descriptor must be a non-empty string")
        commands = self._ensure_commands()
        client, _device = self._open_matched_client(commands)
        try:
            try:
                result = commands.displayaddress(client, desc=descriptor.strip())
            except Exception as exc:
                raise self._map_hwi_error(exc) from exc
        finally:
            self._close_client(client)
        address = result.get("address") if isinstance(result, dict) else None
        if not isinstance(address, str) or not address.strip():
            raise DeviceError(_MSG_UNEXPECTED)
        return address

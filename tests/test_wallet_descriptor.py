"""Tests for watch-key parsing and the wallet descriptor model (TCK-P1-002,
mainnet-only flip per ADR-0021 / TCK-MAIN-001).

Covers: the SLIP-132 prefix→script-type/network matrix (incl. private and
unknown prefix refusals), parse-time mainnet-only gate (testnet keys
refused, mainnet keys accepted), canonical checksummed descriptor
construction with round-trip validation (parse what you build), strict
re-parse of descriptor strings (checksum mismatch, testnet, non-standard
shapes refused), and the hand-built-ParsedKey dispatch guard (unknown
script_type → WatchKeyError, never a raw KeyError).

All keys are derived deterministically via embit from a fixed seed — the
same procedure as the Phase 0 e2e fixtures (public keys only; the only
private-key material is the public BIP32 test-vector-1 xprv, used solely
to pin the watch-only refusal).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Final

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from embit.bip32 import NETWORKS, HDKey
from embit.descriptor import Descriptor
from embit.descriptor.checksum import add_checksum
from embit.networks import NETWORKS as NET

from localwallet.wallet.descriptor import (
    FINGERPRINT_ACCOUNT,
    FINGERPRINT_KINDS,
    FINGERPRINT_MASTER,
    MAINNET_COIN_TYPE,
    ParsedKey,
    PrefixInfo,
    WalletDescriptor,
    WatchKeyError,
    _build_descriptor_string,
    _validate_descriptor_string,
    descriptor_fingerprint,
    detect_script_type,
    parse_wallet_key,
    parse_watch_key,
)

FIXTURE_SEED: Final = b"local-wallet phase 1 descriptor test seed (not a real wallet)"


def _fixture_key(purpose: int, coin: int, script: str, network: str) -> str:
    """Account-level public key m/{purpose}'/{coin}'/0' for ``script``/network."""
    net = "test" if network == "testnet" else "main"
    root = HDKey.from_seed(FIXTURE_SEED, version=NETWORKS[net][f"{script[0]}prv"])
    account = root.derive([purpose + 2**31, coin + 2**31, 0])
    return account.to_public().to_base58(version=NETWORKS[net][script])


def _fixture_prv(script: str, network: str) -> str:
    """Account-level *private* key (watch-only refusal fixture only)."""
    net = "test" if network == "testnet" else "main"
    coin = 1 if network == "testnet" else 0
    root = HDKey.from_seed(FIXTURE_SEED, version=NETWORKS[net][f"{script[0]}prv"])
    account = root.derive([84 + 2**31, coin + 2**31, 0])
    return account.to_base58()


ZPUB: Final = _fixture_key(84, 0, "zpub", "main")
YPUB: Final = _fixture_key(49, 0, "ypub", "main")
XPUB: Final = _fixture_key(44, 0, "xpub", "main")
VPUB: Final = _fixture_key(84, 1, "zpub", "testnet")
UPUB: Final = _fixture_key(49, 1, "ypub", "testnet")
TPUB: Final = _fixture_key(44, 1, "xpub", "testnet")
XPRV: Final = (
    "xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKm"
    "PGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi"
)
TPRV: Final = _fixture_prv("zpub", "testnet")
CORRUPT_ZPUB: Final = ZPUB[:-1] + ("1" if ZPUB[-1] != "1" else "2")


# ----------------------------------------------------------- prefix matrix


@pytest.mark.parametrize(
    ("prefix", "network", "script_type"),
    [
        ("zpub", "main", "p2wpkh"),
        ("vpub", "testnet", "p2wpkh"),
        ("ypub", "main", "p2sh_p2wpkh"),
        ("upub", "testnet", "p2sh_p2wpkh"),
        ("xpub", "main", "p2pkh"),
        ("tpub", "testnet", "p2pkh"),
    ],
)
def test_prefix_matrix(prefix: str, network: str, script_type: str) -> None:
    info = detect_script_type(prefix)
    assert isinstance(info, PrefixInfo)
    assert info.prefix == prefix
    assert info.network == network
    assert info.script_type == script_type


@pytest.mark.parametrize("prefix", ["zprv", "vprv", "yprv", "uprv", "xprv", "tprv"])
def test_private_prefixes_refused_as_watch_only(prefix: str) -> None:
    with pytest.raises(WatchKeyError, match="watch-only"):
        detect_script_type(prefix)


@pytest.mark.parametrize("prefix", ["", "Zpub", "Vpub", "wpkh", "foo", "zpubx", "pub"])
def test_unknown_prefixes_refused(prefix: str) -> None:
    with pytest.raises(WatchKeyError):
        detect_script_type(prefix)


def test_detect_script_type_rejects_non_string() -> None:
    with pytest.raises(WatchKeyError):
        detect_script_type(None)  # type: ignore[arg-type]


# ------------------------------------------------------------ parse + gate


@pytest.mark.parametrize(
    ("key", "network", "script_type"),
    [
        (ZPUB, "main", "p2wpkh"),
        (YPUB, "main", "p2sh_p2wpkh"),
        (XPUB, "main", "p2pkh"),
        (VPUB, "testnet", "p2wpkh"),
        (UPUB, "testnet", "p2sh_p2wpkh"),
        (TPUB, "testnet", "p2pkh"),
    ],
)
def test_parse_detects_network_and_script_type(
    key: str, network: str, script_type: str
) -> None:
    parsed = parse_watch_key(key)
    assert isinstance(parsed, ParsedKey)
    assert parsed.network == network
    assert parsed.script_type == script_type
    assert not parsed.hd_key.is_private


@pytest.mark.parametrize(
    "bad_key",
    ["", "   ", "\t\n", "not-a-key", CORRUPT_ZPUB, XPRV, f"{ZPUB} {ZPUB}"],
)
def test_unparseable_and_private_keys_fail_closed_without_echo(bad_key: str) -> None:
    with pytest.raises(WatchKeyError) as excinfo:
        parse_watch_key(bad_key)
    assert str(excinfo.value).strip() != ""
    if bad_key.strip():
        assert bad_key.strip() not in str(excinfo.value)


@pytest.mark.parametrize("prv_key", [XPRV, TPRV])
def test_private_key_refusal_names_the_watch_only_rule(prv_key: str) -> None:
    """Watch-only refusal holds for ALL private prefixes — mainnet (xprv)
    and testnet (tprv) serialized keys alike."""
    with pytest.raises(WatchKeyError, match="watch-only"):
        parse_watch_key(prv_key)


def test_version_prefix_agrees_for_every_valid_fixture_key() -> None:
    """The string prefix is a deterministic function of the version bytes:
    the parse-time prefix/version cross-check never false-fires on valid
    keys (it exists as defense against future/foreign key material)."""
    for key in (ZPUB, YPUB, XPUB, VPUB, UPUB, TPUB):
        parsed = parse_watch_key(key)  # must not raise
        assert not parsed.hd_key.is_private


def test_parse_mainnet_gate_enforced_in_parse() -> None:
    """Mainnet-only gate (ADR-0021): the gate lives in parse for the wallet
    engine — testnet keys are refused outright."""
    for testnet_key in (VPUB, UPUB, TPUB):
        with pytest.raises(WatchKeyError, match="mainnet-only") as excinfo:
            parse_wallet_key(testnet_key)
        assert testnet_key not in str(excinfo.value)  # value-free
        # detect-only default keeps Phase 0 semantics...
        assert parse_watch_key(testnet_key).network == "testnet"
        # ...and the explicit flag enforces the same gate.
        with pytest.raises(WatchKeyError, match="mainnet-only"):
            parse_watch_key(testnet_key, require_main=True)


def test_parse_wallet_key_accepts_all_mainnet_variants() -> None:
    for key in (ZPUB, YPUB, XPUB):
        parsed = parse_wallet_key(key)
        assert parsed.network == "main"


# ------------------------------------------------------ descriptor building


def test_descriptor_exact_string_and_checksum_for_fixture_zpub() -> None:
    """Guards the canonical shape AND the Bitcoin Core checksum algorithm."""
    wd = WalletDescriptor.from_key(ZPUB)
    fingerprint = parse_watch_key(ZPUB).hd_key.my_fingerprint.hex()
    body = f"wpkh([{fingerprint}/84'/0'/0']{ZPUB}/{{0,1}}/*)"
    assert wd.descriptor == add_checksum(body)
    assert wd.descriptor.startswith("wpkh([")
    assert "#" in wd.descriptor and len(wd.descriptor.rsplit("#", 1)[1]) == 8


@pytest.mark.parametrize(
    ("key", "wrapper", "purpose"),
    [(ZPUB, "wpkh", 84), (YPUB, "sh(wpkh", 49), (XPUB, "pkh", 44)],
)
def test_descriptor_shape_per_script_type(key: str, wrapper: str, purpose: int) -> None:
    wd = WalletDescriptor.from_key(key)
    assert wd.descriptor.startswith(f"{wrapper}([{wd.parsed.hd_key.my_fingerprint.hex()}/{purpose}'/0'/0']")
    assert "/{0,1}/*)" in wd.descriptor
    assert wd.descriptor.endswith("#" + wd.descriptor.rsplit("#", 1)[1])
    assert len(wd.descriptor.rsplit("#", 1)[1]) == 8
    assert wd.script_type == parse_watch_key(key).script_type
    assert wd.network == "main"


@pytest.mark.parametrize("key", [ZPUB, YPUB, XPUB])
def test_descriptor_round_trip_parse_what_you_build(key: str) -> None:
    """The built descriptor parses back in embit's engine — same key, same
    addresses (the engine is the independent cross-check)."""
    wd = WalletDescriptor.from_key(key)
    # Re-parse through our own strict path: identical object semantics.
    rebuilt = WalletDescriptor.from_descriptor_string(wd.descriptor)
    assert rebuilt.parsed.hd_key.to_base58() == wd.parsed.hd_key.to_base58()
    assert rebuilt.script_type == wd.script_type
    assert rebuilt.descriptor == wd.descriptor
    assert rebuilt.network == "main"

    # Independent engine: embit parses the body (minus checksum) and derives
    # the same addresses as our derivation layer.
    from localwallet.wallet.derivation import derive_addresses

    body = wd.descriptor.rsplit("#", 1)[0]
    engine = Descriptor.from_string(body)
    for branch in (0, 1):
        for index in (0, 5):
            expected = derive_addresses(wd.parsed, branch, index, 1)[0].address
            assert (
                engine.derive(index, branch_index=branch).address(network=NET["main"])
                == expected
            )


def test_descriptor_construction_is_deterministic() -> None:
    assert WalletDescriptor.from_key(ZPUB) == WalletDescriptor.from_key(ZPUB)


def test_from_descriptor_string_accepts_embit_notation_and_missing_checksum() -> None:
    wd = WalletDescriptor.from_key(ZPUB)
    body = wd.descriptor.rsplit("#", 1)[0]
    # embit's own serialization (h-notation, <0;1>) with a fresh checksum.
    engine_form = Descriptor.from_string(body).to_string()
    variant = WalletDescriptor.from_descriptor_string(add_checksum(engine_form))
    assert variant.parsed.hd_key.to_base58() == wd.parsed.hd_key.to_base58()
    # Without checksum suffix is accepted too.
    no_checksum = WalletDescriptor.from_descriptor_string(body)
    assert no_checksum.parsed.hd_key.to_base58() == wd.parsed.hd_key.to_base58()


def test_from_descriptor_string_refuses_tampered_checksum() -> None:
    wd = WalletDescriptor.from_key(ZPUB)
    good = wd.descriptor.rsplit("#", 1)[1]
    replacement = "p" if good[0] != "p" else "q"
    bad = replacement + good[1:]
    assert bad != good
    with pytest.raises(WatchKeyError, match="checksum"):
        WalletDescriptor.from_descriptor_string(f"{wd.descriptor[:-8]}{bad}")


def test_from_descriptor_string_refuses_testnet_descriptor() -> None:
    """Structural half of the mainnet-only gate (ADR-0021): no testnet
    wallet descriptors."""
    hd = parse_watch_key(VPUB).hd_key
    # Testnet coin type (1') is refused by the origin check…
    body_testnet_origin = (
        f"wpkh([{hd.my_fingerprint.hex()}/84'/1'/0']{VPUB}/{{0,1}}/*)"
    )
    with pytest.raises(WatchKeyError, match="mainnet"):
        WalletDescriptor.from_descriptor_string(add_checksum(body_testnet_origin))
    # …and a testnet key with mainnet-shaped origin hits the parse gate.
    body_mainnet_shape = (
        f"wpkh([{hd.my_fingerprint.hex()}/84'/0'/0']{VPUB}/{{0,1}}/*)"
    )
    with pytest.raises(WatchKeyError, match="mainnet-only"):
        WalletDescriptor.from_descriptor_string(add_checksum(body_mainnet_shape))
    with pytest.raises(WatchKeyError, match="mainnet-only"):
        WalletDescriptor.from_key(VPUB)


def test_wallet_descriptor_structurally_requires_mainnet() -> None:
    """A hand-built WalletDescriptor cannot carry a testnet network label
    at all (structural gate, independent of the parse gate)."""
    parsed = parse_wallet_key(ZPUB)
    with pytest.raises(WatchKeyError, match="mainnet-only"):
        WalletDescriptor(
            parsed=parsed,
            script_type=parsed.script_type,
            network="testnet",
            descriptor=WalletDescriptor.from_key(ZPUB).descriptor,
        )


@pytest.mark.parametrize(
    "bad",
    [
        "tr([0f0f0f0f/86'/0'/0']" + ZPUB + "/{0,1}/*)",  # taproot: not v1
        "wpkh(" + ZPUB + "/0/*)",  # single-branch: not a wallet descriptor
        "wsh(and_v(v:pkh(" + ZPUB + "),1))",  # miniscript
        "wpkh(" + ZPUB + ")",  # no wildcard
        "wpkh([0f0f0f0f/86'/0'/0']" + ZPUB + "/{0,1}/*)",  # non-standard origin
        "wpkh([" + ZPUB + "/{0,1}/*)",  # unterminated origin
        "wpkh(notab58key/{0,1}/*)",  # bad key
        "",
    ],
)
def test_from_descriptor_string_refuses_malformed_shapes(bad: str) -> None:
    with pytest.raises(WatchKeyError):
        WalletDescriptor.from_descriptor_string(bad)


def test_wallet_descriptor_rejects_internal_inconsistency() -> None:
    parsed = parse_wallet_key(ZPUB)
    with pytest.raises(WatchKeyError):
        WalletDescriptor(
            parsed=parsed,
            script_type="p2pkh",  # does not match parsed.script_type
            network="main",
            descriptor=WalletDescriptor.from_key(ZPUB).descriptor,
        )


def test_origin_fingerprint_is_the_account_keys_own() -> None:
    """Documented v1 semantics: fp = hash160(account pubkey)[:4] — the
    master fingerprint is not recoverable from an account-level key."""
    wd = WalletDescriptor.from_key(ZPUB)
    origin = wd.descriptor.split("(")[1].split("]")[0].lstrip("[")
    fingerprint = origin.split("/")[0]
    assert fingerprint == wd.parsed.hd_key.my_fingerprint.hex()


def test_canonical_origin_is_mainnet_bip84_coin_type() -> None:
    """ADR-0021 flip: the canonical account path is the mainnet BIP84
    coin type — 84'/0'/0' (script-type purposes unchanged, ADR-0008)."""
    wd = WalletDescriptor.from_key(ZPUB)
    origin = wd.descriptor.split("(")[1].split("]")[0].lstrip("[")
    assert origin.split("/")[1:] == ["84'", f"{MAINNET_COIN_TYPE}'", "0'"]
    assert MAINNET_COIN_TYPE == 0


# ------------------------------------------------- hand-built ParsedKey guard


def test_hand_built_parsed_key_unknown_script_type_raises_watch_key_error() -> None:
    """N2 (security review): a hand-built ParsedKey can carry any
    script_type string; the descriptor encode/dispatch site maps an
    unknown value to WatchKeyError — never a raw KeyError."""
    parsed = parse_wallet_key(ZPUB)
    bad = ParsedKey(hd_key=parsed.hd_key, script_type="p2tr", network="main")
    with pytest.raises(WatchKeyError, match="unsupported script type") as excinfo:
        _build_descriptor_string(bad)
    message = str(excinfo.value)
    assert "p2tr" not in message  # value-free
    assert ZPUB not in message

    # Same guard on the round-trip validation dispatch.
    wd = WalletDescriptor.from_key(ZPUB)
    with pytest.raises(WatchKeyError, match="unsupported script type"):
        _validate_descriptor_string(wd.descriptor, bad)


# ============================ TCK-FP-001: origin-carrying provisioning input
#
# USER DIRECTION (2026-09-20): accept the device-export SHAPES so the chip
# can show the master fingerprint Sparrow/Coldcard/Jade display — ONLY when
# the INPUT carries the origin; a bare key keeps its account fp (kind
# "account"), and a master fp is NEVER synthesized (honesty invariant).
# Accepted shapes at from_key (every provisioning entry funnels through it):
#   [fp/84'/0'/0']zpub…  (origin-carrying key)   and  the full-descriptor
#   wpkh([fp/…]zpub…/{0,1}/*) form (Coldcard/Sparrow export), routed to
#   from_descriptor_string. Pathless origins ([fp]zpub…) are REFUSED: BIP-380
#   makes them legal on a key, but with no path there is nothing to check
#   the claim against, so accepting them would be a value-fabrication door.

#: The fixture zpub's OWN account fingerprint (the "account"-kind value).
FP_ACCOUNT_ZPUB: Final = HDKey.from_string(ZPUB).my_fingerprint.hex()
#: A stand-in DEVICE MASTER fp — the fixture-seed master of
#: tests/test_engine_pump.py's WEB-032 block; deliberately != the account fp.
FP_DEVICE: Final = "c115c74e"


def _origin_key(origin_path: str, key: str = ZPUB, fp: str = FP_DEVICE) -> str:
    return f"[{fp}/{origin_path}]{key}"


def test_fp001_bare_zpub_semantics_unchanged() -> None:
    """Done-when 1a: the bare-key form keeps its exact pre-ticket behaviour —
    origin stamped with the key's own fp; the reader classifies it
    (account-fp, "account")."""
    wd = WalletDescriptor.from_key(ZPUB)
    assert wd.descriptor.startswith(f"wpkh([{FP_ACCOUNT_ZPUB}/84'/0'/0']")
    assert descriptor_fingerprint(wd.descriptor) == (FP_ACCOUNT_ZPUB, FINGERPRINT_ACCOUNT)


def test_fp001_origin_key_form_keeps_the_fp_verbatim() -> None:
    """Done-when 1b: ``[fp/84'/0'/0']zpub…`` is accepted; the given fp rides
    the canonical descriptor verbatim (kind "master"); the KEY half is the
    very key the bare form parses — parsed.hd_key untouched, so every
    signer/PSBT consumer of ``my_fingerprint`` is structurally unaffected."""
    assert FP_DEVICE != FP_ACCOUNT_ZPUB
    wd = WalletDescriptor.from_key(_origin_key("84'/0'/0'"))
    assert wd.descriptor.startswith(f"wpkh([{FP_DEVICE}/84'/0'/0']")
    assert f"{ZPUB}/{{0,1}}/*)" in wd.descriptor
    assert wd.parsed.hd_key.to_base58() == ZPUB
    assert wd.parsed.hd_key.my_fingerprint.hex() == FP_ACCOUNT_ZPUB
    assert descriptor_fingerprint(wd.descriptor) == (FP_DEVICE, FINGERPRINT_MASTER)


def test_fp001_h_notation_and_uppercase_fp() -> None:
    """Both hardened notations are the same origin (``84h`` is embit/Coldcard
    spelling); an uppercase fingerprint stores canonically lowercase (the
    WRITER normalizes — the READER on stored rows never does, see matrix)."""
    wd = WalletDescriptor.from_key(_origin_key("84h/0h/0h"))
    assert wd.descriptor.startswith(f"wpkh([{FP_DEVICE}/84'/0'/0']")
    upper = WalletDescriptor.from_key(f"[{FP_DEVICE.upper()}/84'/0'/0']{ZPUB}")
    assert upper.descriptor.startswith(f"wpkh([{FP_DEVICE}/84'/0'/0']")
    assert descriptor_fingerprint(upper.descriptor) == (FP_DEVICE, FINGERPRINT_MASTER)


@pytest.mark.parametrize(
    "path",
    ["49'/0'/0'", "44'/0'/0'", "84'/1'/0'", "84'/0'/1'", "84'/0'/0", "0'/0'/0'"],
)
def test_fp001_mismatched_origin_path_refused_value_free(path: str) -> None:
    """Done-when 1c: the path MUST be the key's own SLIP-132 account path —
    purpose mismatch, testnet coin, non-account index, unhardened, or wrong
    purpose outright: refused, and the refusal echoes NO pasted material."""
    paste = _origin_key(path)
    with pytest.raises(WatchKeyError) as excinfo:
        WalletDescriptor.from_key(paste)
    message = str(excinfo.value)
    assert ZPUB not in message and FP_DEVICE not in message


def test_fp001_pathless_and_malformed_origins_refused_value_free() -> None:
    """Done-when 1d: ``[fp]zpub…`` (no path to check) and malformed
    fingerprints (wrong length, non-hex) are refused value-free; so is any
    bracket shape without a closing ``]``."""
    bad_inputs = [
        f"[{FP_DEVICE}]{ZPUB}",
        _origin_key("84'/0'/0'", fp="deadbee"),  # 7 hex
        _origin_key("84'/0'/0'", fp="abcdef001"),  # 9 hex
        _origin_key("84'/0'/0'", fp="zzzzzzzz"),  # non-hex
        f"{FP_DEVICE}/84'/0'/0']{ZPUB}",  # no opening bracket lands in the key parser
        f"[{FP_DEVICE}/84'/0'/0'{ZPUB}",  # no closing bracket
    ]
    for paste in bad_inputs:
        with pytest.raises(WatchKeyError) as excinfo:
            WalletDescriptor.from_key(paste)
        message = str(excinfo.value)
        assert ZPUB not in message and FP_DEVICE not in message


def test_fp001_whitespace_in_origin_form_refused() -> None:
    """The paste contract of the bare key (internal whitespace = refusal)
    extends to the bracketed form — a stripped key half would otherwise
    sneak whitespace through the base58 gate."""
    for paste in (
        f"[{FP_DEVICE}/84'/0'/0'] {ZPUB}",
        f"[{FP_DEVICE}/84'/0'/ 0']{ZPUB}",
    ):
        with pytest.raises(WatchKeyError) as excinfo:
            WalletDescriptor.from_key(paste)
        assert ZPUB not in str(excinfo.value)


def test_fp001_gates_hold_on_the_origin_form() -> None:
    """ADR-0021 + watch-only apply to the key half of an origin-carrying
    paste exactly as to a bare one: vpub refused, xprv refused — value-free
    either way."""
    for key in (VPUB, XPRV):
        with pytest.raises(WatchKeyError) as excinfo:
            WalletDescriptor.from_key(f"[{FP_DEVICE}/84'/0'/0']{key}")
        message = str(excinfo.value)
        assert key not in message and FP_DEVICE not in message


def test_fp001_full_descriptor_form_accepted_at_from_key() -> None:
    """Parse-shape decision: the Coldcard/Sparrow EXPORT shape is accepted
    at from_key too (routed to the strict from_descriptor_string — checksum
    verified when present, embit ``<0;1>``/``84h`` spelling included); the
    origin-less descriptor form classifies as "account"."""
    from embit.descriptor.checksum import checksum

    body = f"wpkh([{FP_DEVICE}/84'/0'/0']{ZPUB}/{{0,1}}/*)"
    for paste in (
        body,
        f"{body}#{checksum(body)}",
        f"wpkh([{FP_DEVICE}/84h/0h/0h]{ZPUB}/<0;1>/*)",
    ):
        wd = WalletDescriptor.from_key(paste)
        assert wd.descriptor.startswith(f"wpkh([{FP_DEVICE}/")
        assert descriptor_fingerprint(wd.descriptor) == (FP_DEVICE, FINGERPRINT_MASTER)
    plain = f"wpkh({ZPUB}/{{0,1}}/*)"
    wd = WalletDescriptor.from_key(plain)
    assert wd.descriptor.startswith(f"wpkh([{FP_ACCOUNT_ZPUB}/")
    assert descriptor_fingerprint(wd.descriptor) == (FP_ACCOUNT_ZPUB, FINGERPRINT_ACCOUNT)


def test_fp001_bip49_descriptor_form() -> None:
    """Non-segwit-native shapes ride the same rules: ypub with its OWN
    purpose (49') in the sh(wpkh(…)) wrapper — and a wrong purpose for the
    key is refused in the descriptor form too."""
    from embit.descriptor.checksum import checksum

    body = f"sh(wpkh([{FP_DEVICE}/49'/0'/0']{YPUB}/{{0,1}}/*))"
    wd = WalletDescriptor.from_key(f"{body}#{checksum(body)}")
    assert wd.descriptor.startswith(f"sh(wpkh([{FP_DEVICE}/49'/0'/0']")
    assert descriptor_fingerprint(wd.descriptor) == (FP_DEVICE, FINGERPRINT_MASTER)
    wrong = f"sh(wpkh([{FP_DEVICE}/84'/0'/0']{YPUB}/{{0,1}}/*))"
    with pytest.raises(WatchKeyError) as excinfo:
        WalletDescriptor.from_key(wrong)
    assert YPUB not in str(excinfo.value)


def test_fp001_descriptor_form_refusals_stay_value_free() -> None:
    """Garbage that reaches the descriptor branch (tampered checksum, wrong
    wrapper for the key) is refused with the layer's value-free messages."""
    from embit.descriptor.checksum import checksum

    body = f"wpkh([{FP_DEVICE}/84'/0'/0']{ZPUB}/{{0,1}}/*)"
    good = f"{body}#{checksum(body)}"
    tampered = good[:-1] + ("x" if good[-1] != "x" else "y")
    wrong_wrapper = f"pkh([{FP_DEVICE}/84'/0'/0']{ZPUB}/{{0,1}}/*)"
    for paste in (tampered, wrong_wrapper, "wpkh(garbage/{0,1}/*)", "wpkh(()"):
        with pytest.raises(WatchKeyError) as excinfo:
            WalletDescriptor.from_key(paste)
        assert ZPUB not in str(excinfo.value) and FP_DEVICE not in str(excinfo.value)


def test_fp001_round_trip_through_the_stored_string() -> None:
    """Persistence mechanic: the STORED descriptor string carries the
    origin, so a restart that re-reads the row (from_descriptor_string)
    rebuilds the identical string — the master chip value and its kind
    survive with no extra column."""
    wd = WalletDescriptor.from_key(_origin_key("84'/0'/0'"))
    rebuilt = WalletDescriptor.from_descriptor_string(wd.descriptor)
    assert rebuilt.descriptor == wd.descriptor
    assert descriptor_fingerprint(rebuilt.descriptor) == (FP_DEVICE, FINGERPRINT_MASTER)
    assert rebuilt.parsed.hd_key.my_fingerprint.hex() == FP_ACCOUNT_ZPUB


def _desc_with_origin(key: str, fp: str) -> str:
    from embit.descriptor.checksum import checksum

    body = f"wpkh([{fp}/84'/0'/0']{key}/{{0,1}}/*)"
    return f"{body}#{checksum(body)}"


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        # bare-key row (account fp stamped) and the origin-less form both
        # classify (own fp, "account"):
        (WalletDescriptor.from_key(ZPUB).descriptor, (FP_ACCOUNT_ZPUB, FINGERPRINT_ACCOUNT)),
        (f"wpkh({ZPUB}/{{0,1}}/*)", (FP_ACCOUNT_ZPUB, FINGERPRINT_ACCOUNT)),
        # honest device origin:
        (WalletDescriptor.from_key(_origin_key("84'/0'/0'")).descriptor,
         (FP_DEVICE, FINGERPRINT_MASTER)),
        # the coincidence case: an origin EQUAL to the key's own fp is
        # indistinguishable from stamping — classified "account" (the value
        # is provably the key's own, which is exactly what the label means):
        (_desc_with_origin(ZPUB, FP_ACCOUNT_ZPUB), (FP_ACCOUNT_ZPUB, FINGERPRINT_ACCOUNT)),
        # fail-closed reads — None, never garbage, never a normalization:
        (_desc_with_origin(ZPUB, FP_DEVICE.upper()), None),  # stored uppercase
        (_desc_with_origin(ZPUB, FP_DEVICE) + "#badbad00", None),  # bad checksum
        ("desc", None),
        ("", None),
        (None, None),  # type: ignore[arg-type]
        (f"wpkh([{FP_DEVICE}/84'/0'/0']notabase58key/{{0,1}}/*)", None),
    ],
)
def test_fp001_descriptor_fingerprint_matrix(stored: str, expected: object) -> None:
    """Done-when 2+3 classifier matrix: the kind is a PURE function of the
    stored string (no DB column needed); the closed enum holds."""
    got = descriptor_fingerprint(stored)
    assert got == expected
    if got is not None:
        fp, kind = got
        assert kind in FINGERPRINT_KINDS
        assert re.fullmatch(r"[0-9a-f]{8}", fp)

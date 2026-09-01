"""Tests for watch-key parsing and the wallet descriptor model (TCK-P1-002).

Covers: the SLIP-132 prefix→script-type/network matrix (incl. private and
unknown prefix refusals), parse-time testnet gate (Phase 0 security-review
carry-over), canonical checksummed descriptor construction with round-trip
validation (parse what you build), strict re-parse of descriptor
strings (checksum mismatch, mainnet, non-standard shapes refused), and the
hand-built-ParsedKey dispatch guard (unknown script_type → WatchKeyError,
never a raw KeyError).

All keys are derived deterministically via embit from a fixed seed — the
same procedure as the Phase 0 e2e fixtures (public keys only).
"""

from __future__ import annotations

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
    ParsedKey,
    PrefixInfo,
    WalletDescriptor,
    WatchKeyError,
    _build_descriptor_string,
    _validate_descriptor_string,
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


VPUB: Final = _fixture_key(84, 1, "zpub", "testnet")
UPUB: Final = _fixture_key(49, 1, "ypub", "testnet")
TPUB: Final = _fixture_key(44, 1, "xpub", "testnet")
MAINNET_ZPUB: Final = _fixture_key(84, 0, "zpub", "main")
MAINNET_YPUB: Final = _fixture_key(49, 0, "ypub", "main")
MAINNET_XPUB: Final = _fixture_key(44, 0, "xpub", "main")
XPRV: Final = (
    "xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKm"
    "PGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi"
)
CORRUPT_VPUB: Final = VPUB[:-1] + ("1" if VPUB[-1] != "1" else "2")


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
        (VPUB, "testnet", "p2wpkh"),
        (UPUB, "testnet", "p2sh_p2wpkh"),
        (TPUB, "testnet", "p2pkh"),
        (MAINNET_ZPUB, "main", "p2wpkh"),
        (MAINNET_YPUB, "main", "p2sh_p2wpkh"),
        (MAINNET_XPUB, "main", "p2pkh"),
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
    ["", "   ", "\t\n", "not-a-key", CORRUPT_VPUB, XPRV, f"{VPUB} {VPUB}"],
)
def test_unparseable_and_private_keys_fail_closed_without_echo(bad_key: str) -> None:
    with pytest.raises(WatchKeyError) as excinfo:
        parse_watch_key(bad_key)
    assert str(excinfo.value).strip() != ""
    if bad_key.strip():
        assert bad_key.strip() not in str(excinfo.value)


def test_private_key_refusal_names_the_watch_only_rule() -> None:
    with pytest.raises(WatchKeyError, match="watch-only"):
        parse_watch_key(XPRV)


def test_version_prefix_agrees_for_every_valid_fixture_key() -> None:
    """The string prefix is a deterministic function of the version bytes:
    the parse-time prefix/version cross-check never false-fires on valid
    keys (it exists as defense against future/foreign key material)."""
    for key in (VPUB, UPUB, TPUB, MAINNET_ZPUB, MAINNET_YPUB, MAINNET_XPUB):
        parsed = parse_watch_key(key)  # must not raise
        assert not parsed.hd_key.is_private


def test_parse_testnet_gate_enforced_in_parse() -> None:
    """Phase 0 SR carry-over: the gate lives in parse for the wallet engine."""
    for mainnet_key in (MAINNET_ZPUB, MAINNET_YPUB, MAINNET_XPUB):
        with pytest.raises(WatchKeyError, match="testnet-only") as excinfo:
            parse_wallet_key(mainnet_key)
        assert mainnet_key not in str(excinfo.value)  # value-free
        # detect-only default keeps Phase 0 semantics...
        assert parse_watch_key(mainnet_key).network == "main"
        # ...and the explicit flag enforces the same gate.
        with pytest.raises(WatchKeyError, match="testnet-only"):
            parse_watch_key(mainnet_key, require_testnet=True)


def test_parse_wallet_key_accepts_all_testnet_variants() -> None:
    for key in (VPUB, UPUB, TPUB):
        parsed = parse_wallet_key(key)
        assert parsed.network == "testnet"


# ------------------------------------------------------ descriptor building


def test_descriptor_exact_string_and_checksum_for_fixture_vpub() -> None:
    """Guards the canonical shape AND the Bitcoin Core checksum algorithm."""
    wd = WalletDescriptor.from_key(VPUB)
    fingerprint = parse_watch_key(VPUB).hd_key.my_fingerprint.hex()
    body = f"wpkh([{fingerprint}/84'/1'/0']{VPUB}/{{0,1}}/*)"
    assert wd.descriptor == add_checksum(body)
    assert wd.descriptor.startswith("wpkh([")
    assert "#" in wd.descriptor and len(wd.descriptor.rsplit("#", 1)[1]) == 8


@pytest.mark.parametrize(
    ("key", "wrapper", "purpose"),
    [(VPUB, "wpkh", 84), (UPUB, "sh(wpkh", 49), (TPUB, "pkh", 44)],
)
def test_descriptor_shape_per_script_type(key: str, wrapper: str, purpose: int) -> None:
    wd = WalletDescriptor.from_key(key)
    assert wd.descriptor.startswith(f"{wrapper}([{wd.parsed.hd_key.my_fingerprint.hex()}/{purpose}'/1'/0']")
    assert "/{0,1}/*)" in wd.descriptor
    assert wd.descriptor.endswith("#" + wd.descriptor.rsplit("#", 1)[1])
    assert len(wd.descriptor.rsplit("#", 1)[1]) == 8
    assert wd.script_type == parse_watch_key(key).script_type
    assert wd.network == "testnet"


@pytest.mark.parametrize("key", [VPUB, UPUB, TPUB])
def test_descriptor_round_trip_parse_what_you_build(key: str) -> None:
    """The built descriptor parses back in embit's engine — same key, same
    addresses (the engine is the independent cross-check)."""
    wd = WalletDescriptor.from_key(key)
    # Re-parse through our own strict path: identical object semantics.
    rebuilt = WalletDescriptor.from_descriptor_string(wd.descriptor)
    assert rebuilt.parsed.hd_key.to_base58() == wd.parsed.hd_key.to_base58()
    assert rebuilt.script_type == wd.script_type
    assert rebuilt.descriptor == wd.descriptor

    # Independent engine: embit parses the body (minus checksum) and derives
    # the same addresses as our derivation layer.
    from localwallet.wallet.derivation import derive_addresses

    body = wd.descriptor.rsplit("#", 1)[0]
    engine = Descriptor.from_string(body)
    for branch in (0, 1):
        for index in (0, 5):
            expected = derive_addresses(wd.parsed, branch, index, 1)[0].address
            assert (
                engine.derive(index, branch_index=branch).address(network=NET["test"])
                == expected
            )


def test_descriptor_construction_is_deterministic() -> None:
    assert WalletDescriptor.from_key(VPUB) == WalletDescriptor.from_key(VPUB)


def test_from_descriptor_string_accepts_embit_notation_and_missing_checksum() -> None:
    wd = WalletDescriptor.from_key(VPUB)
    body = wd.descriptor.rsplit("#", 1)[0]
    # embit's own serialization (h-notation, <0;1>) with a fresh checksum.
    engine_form = Descriptor.from_string(body).to_string()
    variant = WalletDescriptor.from_descriptor_string(add_checksum(engine_form))
    assert variant.parsed.hd_key.to_base58() == wd.parsed.hd_key.to_base58()
    # Without checksum suffix is accepted too.
    no_checksum = WalletDescriptor.from_descriptor_string(body)
    assert no_checksum.parsed.hd_key.to_base58() == wd.parsed.hd_key.to_base58()


def test_from_descriptor_string_refuses_tampered_checksum() -> None:
    wd = WalletDescriptor.from_key(VPUB)
    good = wd.descriptor.rsplit("#", 1)[1]
    replacement = "p" if good[0] != "p" else "q"
    bad = replacement + good[1:]
    assert bad != good
    with pytest.raises(WatchKeyError, match="checksum"):
        WalletDescriptor.from_descriptor_string(f"{wd.descriptor[:-8]}{bad}")


def test_from_descriptor_string_refuses_mainnet_descriptor() -> None:
    """Structural half of the testnet gate: no mainnet wallet descriptors."""
    hd = parse_watch_key(MAINNET_ZPUB).hd_key
    body = f"wpkh([{hd.my_fingerprint.hex()}/84'/0'/0']{MAINNET_ZPUB}/{{0,1}}/*)"
    # Mainnet coin type (0') is refused by the origin check…
    with pytest.raises(WatchKeyError, match="testnet"):
        WalletDescriptor.from_descriptor_string(add_checksum(body))
    # …and a mainnet key with testnet-shaped origin hits the parse gate.
    body_testnet_shape = (
        f"wpkh([{hd.my_fingerprint.hex()}/84'/1'/0']{MAINNET_ZPUB}/{{0,1}}/*)"
    )
    with pytest.raises(WatchKeyError, match="testnet-only"):
        WalletDescriptor.from_descriptor_string(add_checksum(body_testnet_shape))
    with pytest.raises(WatchKeyError, match="testnet-only"):
        WalletDescriptor.from_key(MAINNET_ZPUB)


@pytest.mark.parametrize(
    "bad",
    [
        "tr([0f0f0f0f/86'/1'/0']" + VPUB + "/{0,1}/*)",  # taproot: not v1
        "wpkh(" + VPUB + "/0/*)",  # single-branch: not a wallet descriptor
        "wsh(and_v(v:pkh(" + VPUB + "),1))",  # miniscript
        "wpkh(" + VPUB + ")",  # no wildcard
        "wpkh([0f0f0f0f/86'/1'/0']" + VPUB + "/{0,1}/*)",  # non-standard origin
        "wpkh([" + VPUB + "/{0,1}/*)",  # unterminated origin
        "wpkh(notab58key/{0,1}/*)",  # bad key
        "",
    ],
)
def test_from_descriptor_string_refuses_malformed_shapes(bad: str) -> None:
    with pytest.raises(WatchKeyError):
        WalletDescriptor.from_descriptor_string(bad)


def test_wallet_descriptor_rejects_internal_inconsistency() -> None:
    parsed = parse_wallet_key(VPUB)
    with pytest.raises(WatchKeyError):
        WalletDescriptor(
            parsed=parsed,
            script_type="p2pkh",  # does not match parsed.script_type
            network="testnet",
            descriptor=WalletDescriptor.from_key(VPUB).descriptor,
        )


def test_origin_fingerprint_is_the_account_keys_own() -> None:
    """Documented v1 semantics: fp = hash160(account pubkey)[:4] — the
    master fingerprint is not recoverable from an account-level key."""
    wd = WalletDescriptor.from_key(VPUB)
    origin = wd.descriptor.split("(")[1].split("]")[0].lstrip("[")
    fingerprint = origin.split("/")[0]
    assert fingerprint == wd.parsed.hd_key.my_fingerprint.hex()


# ------------------------------------------------- hand-built ParsedKey guard


def test_hand_built_parsed_key_unknown_script_type_raises_watch_key_error() -> None:
    """N2 (security review): a hand-built ParsedKey can carry any
    script_type string; the descriptor encode/dispatch site maps an
    unknown value to WatchKeyError — never a raw KeyError."""
    parsed = parse_wallet_key(VPUB)
    bad = ParsedKey(hd_key=parsed.hd_key, script_type="p2tr", network="testnet")
    with pytest.raises(WatchKeyError, match="unsupported script type") as excinfo:
        _build_descriptor_string(bad)
    message = str(excinfo.value)
    assert "p2tr" not in message  # value-free
    assert VPUB not in message

    # Same guard on the round-trip validation dispatch.
    wd = WalletDescriptor.from_key(VPUB)
    with pytest.raises(WatchKeyError, match="unsupported script type"):
        _validate_descriptor_string(wd.descriptor, bad)

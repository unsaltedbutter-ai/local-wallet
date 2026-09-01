"""Tests for batched address derivation (TCK-P1-002).

Covers: batch correctness against embit's own descriptor engine (the
independent cross-check) for all three script types on receive AND change
branches, start-index windows, address encodings per network, argument
validation (value-free errors), the derive-side testnet gate, and the
batching contract itself (single branch-key derivation, then children —
asserted via a derivation-call spy).
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
from embit.networks import NETWORKS as NET

from localwallet.wallet.derivation import (
    BranchDeriver,
    DerivedAddress,
    derive_addresses,
    derive_receive_addresses,
)
from localwallet.wallet.descriptor import (
    ParsedKey,
    WalletDescriptor,
    WatchKeyError,
    parse_watch_key,
)

FIXTURE_SEED: Final = b"local-wallet phase 1 derivation test seed (not a real wallet)"


def _fixture_key(purpose: int, coin: int, script: str, network: str) -> str:
    net = "test" if network == "testnet" else "main"
    root = HDKey.from_seed(FIXTURE_SEED, version=NETWORKS[net][f"{script[0]}prv"])
    account = root.derive([purpose + 2**31, coin + 2**31, 0])
    return account.to_public().to_base58(version=NETWORKS[net][script])


VPUB: Final = _fixture_key(84, 1, "zpub", "testnet")
UPUB: Final = _fixture_key(49, 1, "ypub", "testnet")
TPUB: Final = _fixture_key(44, 1, "xpub", "testnet")
MAINNET_ZPUB: Final = _fixture_key(84, 0, "zpub", "main")

_CASES: Final = [(VPUB, "wpkh"), (UPUB, "sh(wpkh"), (TPUB, "pkh")]


def _engine_descriptor(key: str, wrapper: str) -> Descriptor:
    body = (
        f"{wrapper}({key}/{{0,1}}/*))"
        if wrapper == "sh(wpkh"
        else f"{wrapper}({key}/{{0,1}}/*)"
    )
    return Descriptor.from_string(body)


# ------------------------------------------------- cross-check vs the engine


@pytest.mark.parametrize(("key", "wrapper"), _CASES)
def test_batch_matches_embit_descriptor_engine_both_branches(
    key: str, wrapper: str
) -> None:
    parsed = WalletDescriptor.from_key(key).parsed
    engine = _engine_descriptor(key, wrapper)
    for branch in (0, 1):
        batch = derive_addresses(parsed, branch, 0, 25)
        assert len(batch) == 25
        for derived in batch:
            expected = engine.derive(
                derived.index, branch_index=branch
            ).address(network=NET["test"])
            assert derived.address == expected
            assert derived.branch == branch
            assert derived.script_type == parsed.script_type
            assert isinstance(derived, DerivedAddress)


def test_multibranch_branch_index_semantics() -> None:
    """Our (branch, index) pair equals the {0,1} wildcard's branch_index."""
    wd = WalletDescriptor.from_key(VPUB)
    engine = _engine_descriptor(VPUB, "wpkh")
    for branch in (0, 1):
        for index in (0, 3, 9):
            ours = derive_addresses(wd.parsed, branch, index, 1)[0].address
            theirs = engine.derive(index, branch_index=branch).address(
                network=NET["test"]
            )
            assert ours == theirs


def test_change_branch_differs_from_receive() -> None:
    parsed = WalletDescriptor.from_key(VPUB).parsed
    receive = derive_addresses(parsed, 0, 0, 3)
    change = derive_addresses(parsed, 1, 0, 3)
    assert [d.address for d in receive] != [d.address for d in change]


def test_start_index_window_is_exact_slice() -> None:
    parsed = WalletDescriptor.from_key(VPUB).parsed
    full = derive_addresses(parsed, 1, 0, 20)
    window = derive_addresses(parsed, 1, 5, 3)
    assert [d.address for d in window] == [d.address for d in full[5:8]]
    assert [d.index for d in window] == [5, 6, 7]
    # ...and the window matches the engine at the same absolute indices.
    engine = _engine_descriptor(VPUB, "wpkh")
    for derived in window:
        assert derived.address == engine.derive(
            derived.index, branch_index=1
        ).address(network=NET["test"])


def test_testnet_address_encodings_per_script_type() -> None:
    # P2WPKH → bech32 (tb1), P2SH-P2WPKH → P2SH-wrapped (2N/2...), P2PKH → m/n.
    prefixes = {"wpkh": "tb1", "sh(wpkh": "2", "pkh": ("m", "n")}
    for key, wrapper in _CASES:
        parsed = WalletDescriptor.from_key(key).parsed
        addr = derive_addresses(parsed, 0, 0, 1)[0].address
        expected = prefixes[wrapper]
        if isinstance(expected, tuple):
            assert addr[0] in expected
        else:
            assert addr.startswith(expected)


# ------------------------------------------------------------ argument gates


def test_derive_side_testnet_gate_keeps_phase0_message() -> None:
    """Belt and suspenders: a mainnet ParsedKey cannot derive addresses.
    The Phase 0 message is kept verbatim (TCK-P0-006 e2e asserts it)."""
    parsed = parse_watch_key(MAINNET_ZPUB)
    with pytest.raises(WatchKeyError) as excinfo:
        derive_addresses(parsed, 0, 0, 1)
    message = str(excinfo.value)
    assert "Phase 0 is testnet-only" in message
    assert "vpub/upub/tpub" in message
    assert MAINNET_ZPUB not in message  # value-free
    with pytest.raises(WatchKeyError, match="testnet-only"):
        BranchDeriver(parsed, 0)


def test_deriver_refuses_bad_branch() -> None:
    parsed = WalletDescriptor.from_key(VPUB).parsed
    with pytest.raises(WatchKeyError, match="branch"):
        BranchDeriver(parsed, 2)


@pytest.mark.parametrize(
    "index", [-1, 2**31, 2**31 + 5, True, "3", "3'", None, 2.5]
)
def test_deriver_address_refuses_out_of_range_indices(index: object) -> None:
    """N1 (security review): BranchDeriver.address range-checks the child
    index — negative, hardened-range (>= 2**31), hardened-marker and
    non-int inputs raise WatchKeyError, never a raw embit error."""
    deriver = BranchDeriver(WalletDescriptor.from_key(VPUB).parsed, 0)
    with pytest.raises(WatchKeyError, match="index") as excinfo:
        deriver.address(index)  # type: ignore[arg-type]
    assert VPUB not in str(excinfo.value)  # value-free: no key material


def test_deriver_address_accepts_top_of_non_hardened_range() -> None:
    deriver = BranchDeriver(WalletDescriptor.from_key(VPUB).parsed, 0)
    assert deriver.address(2**31 - 1).startswith("tb1")


@pytest.mark.parametrize("count", [0, -1, True, 1001, 2.5, "3", None])
def test_count_validation(count: object) -> None:
    parsed = WalletDescriptor.from_key(VPUB).parsed
    with pytest.raises(WatchKeyError, match="count"):
        derive_addresses(parsed, 0, 0, count)  # type: ignore[arg-type]


@pytest.mark.parametrize("branch", [2, -1, True, "0", None])
def test_branch_validation(branch: object) -> None:
    parsed = WalletDescriptor.from_key(VPUB).parsed
    with pytest.raises(WatchKeyError, match="branch"):
        derive_addresses(parsed, branch, 0, 1)  # type: ignore[arg-type]


@pytest.mark.parametrize("start", [-1, True, 2.5, "0", None, 2**31])
def test_start_index_validation(start: object) -> None:
    parsed = WalletDescriptor.from_key(VPUB).parsed
    with pytest.raises(WatchKeyError, match="start_index"):
        derive_addresses(parsed, 0, start, 1)  # type: ignore[arg-type]


# --------------------------------------------------------- batching contract


class _CountingKey:
    """Derivation-call spy delegating everything to a real HDKey.

    All calls (account key and derived children alike) land in one shared
    journal, tagged with the calling instance's id.
    """

    def __init__(self, inner: HDKey, journal: list | None = None) -> None:
        self._inner = inner
        self._journal: list[tuple[int, tuple[int, ...]]] = (
            [] if journal is None else journal
        )

    @property
    def calls(self) -> list[tuple[int, tuple[int, ...]]]:
        return self._journal

    def derive(self, path: list[int]) -> _CountingKey:
        self._journal.append((id(self), tuple(path)))
        return _CountingKey(self._inner.derive(path), journal=self._journal)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def test_batching_single_branch_derive_then_children() -> None:
    """Efficiency contract: exactly ONE derivation off the account key (the
    branch), then direct children — never a per-index full-path derivation."""
    parsed = WalletDescriptor.from_key(VPUB).parsed
    spy = _CountingKey(parsed.hd_key)
    spied = ParsedKey(hd_key=spy, script_type=parsed.script_type, network=parsed.network)

    count = 10
    derive_addresses(spied, 0, 7, count)

    root_calls = [path for instance, path in spy.calls if instance == id(spy)]
    assert root_calls == [(0,)]  # one branch derive, nothing else on the account key
    assert all(len(path) == 1 for _, path in spy.calls)  # no deep paths anywhere
    child_calls = [path for instance, path in spy.calls if instance != id(spy)]
    assert child_calls == [(7 + i,) for i in range(count)]


def test_branch_deriver_matches_batch_derivation() -> None:
    parsed = WalletDescriptor.from_key(VPUB).parsed
    deriver = BranchDeriver(parsed, 1)
    for index in (0, 4, 9):
        assert deriver.address(index) == derive_addresses(parsed, 1, index, 1)[0].address


def test_phase0_wrapper_parity() -> None:
    """derive_receive_addresses keeps its Phase 0 semantics exactly."""
    parsed = WalletDescriptor.from_key(VPUB).parsed
    assert derive_receive_addresses(parsed, 5) == [
        d.address for d in derive_addresses(parsed, 0, 0, 5)
    ]
    assert derive_receive_addresses(parsed, 3) == derive_receive_addresses(parsed, 5)[:3]
    assert derive_receive_addresses(parsed, 2, branch=1) == [
        d.address for d in derive_addresses(parsed, 1, 0, 2)
    ]
    # Default count is 5, default branch 0 (Phase 0 signature).
    assert len(derive_receive_addresses(parsed)) == 5

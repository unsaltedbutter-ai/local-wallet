"""Batched address derivation from a parsed watch key (TCK-P1-002).

Derives receive (branch 0) and change (branch 1) addresses from an
account-level extended public key using standard descriptor semantics:
the account key sits at ``m/{purpose}'/{coin}'/0'`` and addresses are
``{branch}/{index}`` children of it — exactly what the canonical
descriptor ``wpkh([fp/84'/0'/0']xpub/{0,1}/*)`` expresses.

Batching contract: :func:`derive_addresses` performs a **single**
derivation of the branch key from the account key, then derives each
child directly from that branch key — never a per-index full-path
derivation. A test (:mod:`tests.test_wallet_derivation`) asserts the
derivation-call shape via a counting spy.

Mainnet-only gate (ADR-0021, belt and suspenders): the gate is already
enforced at parse time (:func:`localwallet.wallet.descriptor.parse_wallet_key`)
and structurally (:class:`localwallet.wallet.descriptor.WalletDescriptor`);
derivation re-checks it so a testnet :class:`ParsedKey` (obtainable via
the Phase 0 detect-only parse) can never reach address encoding. The
Phase 0 layered-gate structure is kept; only the gate direction flipped
with the mainnet-only decision (ADR-0021).

Watch-only invariants: public keys only; error messages are value-free
(key material is never echoed); no network I/O; no logging.
"""

from __future__ import annotations

from dataclasses import dataclass

from embit import script
from embit.networks import NETWORKS

from localwallet.wallet.descriptor import ParsedKey, WatchKeyError

__all__ = [
    "BranchDeriver",
    "DerivedAddress",
    "derive_addresses",
    "derive_receive_addresses",
]

#: Upper bound on the number of addresses derived in one call (same
#: bound as the Phase 0 stub).
_MAX_DERIVE_COUNT = 1000

#: Highest non-hardened BIP32 child index.
_MAX_CHILD_INDEX = 2**31 - 1

#: Network label → embit network dict (address encoding). Mainnet only
#: (ADR-0021): the gate below refuses every other network label before
#: any lookup, fail closed.
_NETWORKS_BY_LABEL = {
    "main": NETWORKS["main"],
}


@dataclass(frozen=True, slots=True)
class DerivedAddress:
    """One derived address with its BIP44 coordinates."""

    branch: int
    index: int
    address: str
    script_type: str


def derive_addresses(
    parsed: ParsedKey, branch: int, start_index: int, count: int
) -> list[DerivedAddress]:
    """Batch-derive ``count`` addresses of ``branch`` starting at ``start_index``.

    Single branch-key derivation, then direct children (see module
    docstring). Addresses are encoded for the key's detected script type
    and network via the ``embit.script`` constructors.

    Args:
        parsed: A :class:`ParsedKey` (account-level public key).
        branch: 0 = receive, 1 = change (BIP44 convention).
        start_index: First child index (0-based, non-hardened range).
        count: How many addresses to derive (1..1000).

    Returns:
        Derived addresses in ascending index order,
        ``start_index .. start_index + count - 1``.

    Raises:
        WatchKeyError: ``parsed`` is a testnet key (mainnet-only gate),
            ``branch`` / ``count`` / ``start_index`` are out of range,
            or the script type is unsupported. Messages never echo key
            material.
    """
    if parsed.network != "main":
        raise WatchKeyError(
            "mainnet-only: testnet extended public keys are refused — provide "
            "a mainnet xpub/ypub/zpub"
        )
    if isinstance(branch, bool) or branch not in (0, 1):
        raise WatchKeyError("branch must be 0 (receive) or 1 (change)")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not 1 <= count <= _MAX_DERIVE_COUNT
    ):
        raise WatchKeyError(
            f"count must be an integer between 1 and {_MAX_DERIVE_COUNT}"
        )
    if (
        isinstance(start_index, bool)
        or not isinstance(start_index, int)
        or not 0 <= start_index <= _MAX_CHILD_INDEX - count
    ):
        raise WatchKeyError(
            f"start_index must be an integer between 0 and "
            f"{_MAX_CHILD_INDEX - count} for this count"
        )

    network = _NETWORKS_BY_LABEL[parsed.network]
    # Batch derivation: one branch-key derive, then direct children.
    branch_key = parsed.hd_key.derive([branch])
    return [
        DerivedAddress(
            branch=branch,
            index=start_index + offset,
            address=_encode_address(
                branch_key.derive([start_index + offset]).key,
                parsed.script_type,
                network,
            ),
            script_type=parsed.script_type,
        )
        for offset in range(count)
    ]


def derive_receive_addresses(
    parsed: ParsedKey, count: int = 5, branch: int = 0
) -> list[str]:
    """Derive the first ``count`` addresses of one branch (Phase 0 API).

    Backwards-compatible wrapper kept for the TCK-P0-006 callers and
    tests; the wallet engine uses :func:`derive_addresses` directly.
    Semantics are identical to the Phase 0 stub: addresses for indices
    ``0 .. count-1`` of ``branch``, encoded for the key's detected
    script type and network, with the same value-free error contract
    and the derive-side mainnet-only gate (ADR-0021).
    """
    return [
        derived.address
        for derived in derive_addresses(parsed, branch=branch, start_index=0, count=count)
    ]


class BranchDeriver:
    """Incremental one-branch deriver; derives the branch key once.

    The scanner walks indices one at a time (usage discovered along the
    way decides how far to walk), so it needs child-at-a-time derivation
    without re-deriving the branch key per index. Construction performs
    the single branch-key derivation; :meth:`address` derives one direct
    child — the same batching contract as :func:`derive_addresses`.
    """

    def __init__(self, parsed: ParsedKey, branch: int) -> None:
        if parsed.network != "main":
            # Same gate/message as derive_addresses (belt and suspenders).
            raise WatchKeyError(
                "mainnet-only: testnet extended public keys are refused — provide "
                "a mainnet xpub/ypub/zpub"
            )
        if branch not in (0, 1):
            raise WatchKeyError("branch must be 0 (receive) or 1 (change)")
        self._parsed = parsed
        self._network = _NETWORKS_BY_LABEL[parsed.network]
        self._branch_key = parsed.hd_key.derive([branch])

    def address(self, index: int) -> str:
        """Derive and encode the child address at ``index``.

        Raises:
            WatchKeyError: ``index`` is not a plain integer in the
                non-hardened BIP32 child range — negative values, the
                hardened range (``>= 2**31``), hardened-marker inputs
                (e.g. ``"3'"``), or non-int/bool values are refused here
                (fail closed) instead of surfacing as raw embit errors.
                The batched :func:`derive_addresses` path is guarded by
                its own ``start_index`` check; messages are value-free.
        """
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index <= _MAX_CHILD_INDEX
        ):
            raise WatchKeyError(
                f"index must be an integer between 0 and {_MAX_CHILD_INDEX} "
                "(non-hardened BIP32 child range)"
            )
        return _encode_address(
            self._branch_key.derive([index]).key,
            self._parsed.script_type,
            self._network,
        )


def _encode_address(pubkey: object, script_type: str, network: dict[str, object]) -> str:
    """Encode one compressed public key into an address for ``script_type``.

    embit's :class:`~embit.bip32.HDKey` exposes no ``address()`` helper
    (checked against 0.8.0); the address is built from the child's
    compressed public key via the ``embit.script`` constructors and the
    script's own ``address()`` encoder.
    """
    if script_type == "p2wpkh":
        return script.p2wpkh(pubkey).address(network)  # type: ignore[arg-type]
    if script_type == "p2sh_p2wpkh":
        return script.p2sh(script.p2wpkh(pubkey)).address(network)  # type: ignore[arg-type]
    if script_type == "p2pkh":
        return script.p2pkh(pubkey).address(network)  # type: ignore[arg-type]
    # Unreachable while ParsedKey only carries the three types above.
    raise WatchKeyError(f"internal: unsupported script type {script_type!r}")  # pragma: no cover

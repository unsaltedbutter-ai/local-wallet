"""Phase 0 watch-only key stub: parse a SLIP-132 xpub + derive addresses.

**PHASE 0 STUB — deliberately minimal** (TCK-P0-006). This module does
exactly two things:

1. :func:`parse_watch_key` — parse an extended *public* key (SLIP-132
   prefixes ``zpub``/``vpub``/``ypub``/``upub``/``xpub``/``tpub``),
   detecting network and script type from the version bytes.
2. :func:`derive_receive_addresses` — derive the first ``count``
   addresses of one branch from the account-level key, using standard
   descriptor semantics ``{branch}/{i}`` (account-level key convention
   per PROJECT.md §7.3, BIP84-style paths).

Explicitly NOT implemented here (Phase 1 wallet engine replaces these):
**no gap-limit scanning, no caching**, no derivation-index tracking, no
SQLite state, no descriptor strings, no rescan. Do not grow this module
into those; replace it.

Testnet gate (PROJECT.md §5 principle 8, §12): Phase 0 is testnet-only.
:func:`derive_receive_addresses` refuses mainnet keys outright with a
clear error. Anything unparseable is refused (fail closed). Error
messages are value-free — the key material is never echoed into an
exception, a log, or a return value.

Watch-only invariant: private extended keys (``xprv`` and friends) are
refused — this app handles public keys only.

Version-map provenance: embit 0.8.0 ships no dedicated ``slip132``
module and no slip132 helper functions in ``embit.bip32`` (only the
``detect_version`` path→version helper). The SLIP-132 version bytes are
therefore taken **verbatim from ``embit.bip32.NETWORKS``** (the same
object as ``embit.networks.NETWORKS``), which is embit's built-in
SLIP-132 version map; this module's local table keys those bytes to
``(prefix, network label, script type)``. Note embit names the dict
entries by their mainnet prefix, so the testnet entries are the SLIP-132
``vpub`` (embit: ``NETWORKS["test"]["zpub"]``) and ``upub``
(embit: ``NETWORKS["test"]["ypub"]``) version bytes.

No network I/O (embit is a local crypto library — the network-import
lint only bans real network modules), no logging, no secrets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from embit import script
from embit.bip32 import HDKey
from embit.networks import NETWORKS

__all__ = [
    "ParsedKey",
    "WatchKeyError",
    "derive_receive_addresses",
    "parse_watch_key",
]

#: Upper bound on accepted key length (base58 xpubs are ≤ ~111 chars;
#: anything longer is refused before parsing, fail closed).
_MAX_KEY_CHARS: Final[int] = 200

#: Upper bound on the number of addresses derived in one call. Phase 0
#: scans a handful of addresses per balance check; Phase 1's gap-limit
#: scanner replaces this knob.
_MAX_DERIVE_COUNT: Final[int] = 1000

#: SLIP-132 version bytes → (prefix, network label, script type). Values
#: are copied verbatim from ``embit.bip32.NETWORKS`` (see module
#: docstring for provenance). Script types: ``p2wpkh`` (BIP84 native
#: segwit), ``p2sh_p2wpkh`` (BIP49 nested segwit), ``p2pkh`` (BIP44
#: legacy).
_VERSION_TABLE: Final[dict[bytes, tuple[str, str, str]]] = {
    NETWORKS["main"]["zpub"]: ("zpub", "main", "p2wpkh"),
    NETWORKS["test"]["zpub"]: ("vpub", "testnet", "p2wpkh"),
    NETWORKS["main"]["ypub"]: ("ypub", "main", "p2sh_p2wpkh"),
    NETWORKS["test"]["ypub"]: ("upub", "testnet", "p2sh_p2wpkh"),
    NETWORKS["main"]["xpub"]: ("xpub", "main", "p2pkh"),
    NETWORKS["test"]["xpub"]: ("tpub", "testnet", "p2pkh"),
}

#: Network label → embit network dict (address encoding needs the bech32
#: HRP / base58 version bytes).
_NETWORKS_BY_LABEL: Final[dict[str, dict[str, object]]] = {
    "main": NETWORKS["main"],
    "testnet": NETWORKS["test"],
}


class WatchKeyError(ValueError):
    """A watch key could not be parsed, or is not usable in Phase 0.

    Raised (fail closed) for: unparseable/base58-invalid input, private
    extended keys, unknown version bytes, mainnet keys under the Phase 0
    testnet gate, and out-of-range derivation arguments.

    Message contract: value-free. The offending key is never echoed into
    the message (watch-only app; keys never appear in errors or logs).
    """


@dataclass(frozen=True, slots=True)
class ParsedKey:
    """A parsed watch-only account-level extended public key.

    Attributes:
        hd_key: The embit :class:`~embit.bip32.HDKey` (public, non-hardened
            derivation only).
        script_type: One of ``"p2wpkh"``, ``"p2sh_p2wpkh"``, ``"p2pkh"`` —
            detected from the SLIP-132 version bytes.
        network: ``"testnet"`` or ``"main"`` — detected from the version
            bytes. Phase 0 refuses ``"main"`` at derivation time.
    """

    hd_key: HDKey
    script_type: str
    network: str


def parse_watch_key(key: str) -> ParsedKey:
    """Parse a SLIP-132 extended public key and detect network/script type.

    Accepts ``zpub``/``vpub`` (P2WPKH), ``ypub``/``upub`` (P2SH-P2WPKH)
    and ``xpub``/``tpub`` (P2PKH) account-level keys. Surrounding
    whitespace is tolerated (clipboard pastes); internal whitespace is
    not.

    Args:
        key: The extended public key string.

    Returns:
        A :class:`ParsedKey` with the embit HDKey plus the detected
        script type and network.

    Raises:
        WatchKeyError: the input is not a string, is empty/whitespace or
            over-long, fails base58/checksum parsing, is a *private*
            extended key (watch-only refusal), or carries version bytes
            outside the supported SLIP-132 set. The message never echoes
            the input.
    """
    if not isinstance(key, str):
        raise WatchKeyError("watch key must be a string")
    candidate = key.strip()
    if not candidate or len(candidate) > _MAX_KEY_CHARS or any(c.isspace() for c in candidate):
        raise WatchKeyError("watch key must be a single extended public key without whitespace")

    try:
        hd = HDKey.from_string(candidate)
    except Exception as exc:  # containment: embit raises ValueError for base58/checksum and EmbitError (plain Exception) for shape problems — all are re-raised value-free below
        raise WatchKeyError(
            "not a valid extended key (bad base58, checksum, or shape)"
        ) from exc

    if hd.is_private:
        raise WatchKeyError(
            "watch-only: private extended keys are never handled — provide a public key "
            "(vpub/tpub or a mainnet zpub-style public key)"
        )

    entry = _VERSION_TABLE.get(hd.version)
    if entry is None:
        raise WatchKeyError(
            "unknown extended-key version bytes (expected zpub/vpub/ypub/upub/xpub/tpub)"
        )

    _prefix, network, script_type = entry
    return ParsedKey(hd_key=hd, script_type=script_type, network=network)


def derive_receive_addresses(
    parsed: ParsedKey, count: int = 5, branch: int = 0
) -> list[str]:
    """Derive the first ``count`` addresses of one branch (Phase 0 stub).

    Standard BIP84-style derivation from the account-level key: derive
    ``{branch}`` then ``{i}`` for ``i`` in ``0..count-1`` — descriptor
    semantics ``wpkh(<key>/{branch}/*)`` with the account-level key
    convention of PROJECT.md §7.3. Addresses are encoded for the key's
    detected script type and network.

    **Phase 0 stub: no gap-limit scanning, no caching** — the caller gets
    exactly ``count`` deterministic addresses and must re-derive or scan
    as needed. Phase 1's wallet engine replaces this.

    Args:
        parsed: A :class:`ParsedKey` from :func:`parse_watch_key`.
        count: How many addresses to derive (1..1000; default 5).
        branch: 0 = receive, 1 = change (BIP44 convention).

    Returns:
        The derived addresses, index order 0..count-1.

    Raises:
        WatchKeyError: ``parsed`` is a mainnet key (Phase 0 testnet gate:
            "Phase 0 is testnet-only — provide a vpub/tpub"), or ``count``
            /``branch`` are out of range. Messages never echo key material.
    """
    if parsed.network == "main":
        raise WatchKeyError("Phase 0 is testnet-only — provide a vpub/tpub")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= _MAX_DERIVE_COUNT:
        raise WatchKeyError(f"count must be an integer between 1 and {_MAX_DERIVE_COUNT}")
    if isinstance(branch, bool) or branch not in (0, 1):
        raise WatchKeyError("branch must be 0 (receive) or 1 (change)")

    branch_key = parsed.hd_key.derive([branch])
    network = _NETWORKS_BY_LABEL[parsed.network]
    return [
        _encode_address(branch_key.derive([index]).key, parsed.script_type, network)
        for index in range(count)
    ]


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
    # Unreachable while _VERSION_TABLE only emits the three types above.
    raise WatchKeyError(f"internal: unsupported script type {script_type!r}")  # pragma: no cover

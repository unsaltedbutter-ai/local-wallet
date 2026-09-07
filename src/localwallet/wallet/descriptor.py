"""Watch-key parsing, prefix detection, and the wallet descriptor model.

This module supersedes the Phase 0 ``zpub_stub`` (TCK-P1-002) and is the
single authority for turning user-supplied extended *public* keys into

1. a :class:`ParsedKey` (embit HDKey + detected script type + network), and
2. a :class:`WalletDescriptor` — the wallet-engine object carrying the
   canonical checksummed output descriptor, e.g.::

        wpkh([af0a1d1f/84'/0'/0']zpub6rFR.../{0,1}/*)#checksum

Mainnet-only gate (ADR-0021, supersedes the ADR-0004 testnet choice)
-------------------------------------------------------------------
local-wallet is mainnet-only; testnet code paths are removed, not kept
as fallback. The gate is enforced at **parse time** for the wallet
engine: :func:`parse_wallet_key` (and :func:`parse_watch_key` with
``require_main=True``) refuse testnet vpub/upub/tpub keys (and any
testnet-serialized key) outright. The gate is enforced again
structurally: a :class:`WalletDescriptor` cannot even be constructed
for a non-mainnet key (``__post_init__``), and
:func:`localwallet.wallet.derivation.derive_addresses` re-checks (belt
and suspenders). The Phase 0 ``parse_watch_key`` default keeps
detect-only semantics (backwards compatibility with TCK-P0-006 callers
and tests); every money path goes through the gated entries.

Watch-only invariants
---------------------
- Private extended keys (``*prv``) are refused — this app handles public
  keys only; zero secrets in process, disk, or logs.
- Error messages are **value-free**: key material is never echoed into an
  exception, log, or return value.
- No network I/O, no logging.

SLIP-132 provenance note (embit 0.8.0)
--------------------------------------
embit 0.8.0 ships no ``embit.slip132`` module; the SLIP-132 version bytes
are taken verbatim from ``embit.bip32.NETWORKS`` (same object as
``embit.networks.NETWORKS``), keyed by their mainnet prefix names (the
testnet ``vpub``/``upub`` version bytes live under the ``zpub``/``ypub``
keys of ``NETWORKS["test"]``). Testnet version bytes are retained in the
detection tables solely so testnet keys can be *detected and refused*
with a precise, value-free error — they are never accepted as wallet
material (ADR-0021).

Descriptor checksum note (embit 0.8.0)
--------------------------------------
``embit.descriptor.Descriptor.to_string()`` emits **no** checksum and
``Descriptor.from_string`` does **not validate** a trailing checksum.
The canonical descriptor string is therefore built by this module and
checksummed with ``embit.descriptor.checksum`` (the standard Bitcoin
Core descriptor checksum — verified against the Core test vectors
``raw(deadbeef)#89f8spxm`` and the BIP380 ``pkh(xpub...)`` vector).
Round-trip validation (parse what you build) is performed with embit's
descriptor engine in :meth:`WalletDescriptor.__post_init__`; because
embit ignores the checksum suffix, the suffix is verified explicitly
here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from embit.bip32 import HDKey
from embit.descriptor import Descriptor
from embit.descriptor.checksum import checksum as _descriptor_checksum
from embit.networks import NETWORKS as _NETWORKS

__all__ = [
    "MAINNET_COIN_TYPE",
    "ParsedKey",
    "PrefixInfo",
    "WalletDescriptor",
    "WatchKeyError",
    "detect_script_type",
    "parse_wallet_key",
    "parse_watch_key",
]

#: Upper bound on accepted key length (base58 xpubs are ≤ ~111 chars;
#: anything longer is refused before parsing, fail closed).
_MAX_KEY_CHARS: Final[int] = 200

#: Upper bound on accepted descriptor length (canonical descriptors are
#: ~160 chars; anything longer is refused before parsing).
_MAX_DESCRIPTOR_CHARS: Final[int] = 500

#: Supported script types and their standard BIP44/49/84 account purpose.
SCRIPT_PURPOSES: Final[dict[str, int]] = {
    "p2wpkh": 84,
    "p2sh_p2wpkh": 49,
    "p2pkh": 44,
}

#: BIP44 coin type for mainnet (BIP84 canonical account path 84'/0'/0').
MAINNET_COIN_TYPE: Final[int] = 0

#: Deprecated pre-mainnet-only name, kept solely so cross-layer callers
#: outside this ticket's scope (app wiring, TCK-MAIN-002/003) keep
#: importing during the migration; it equals the mainnet coin type
#: because the wallet layer is mainnet-only (ADR-0021).
TESTNET_COIN_TYPE: Final[int] = MAINNET_COIN_TYPE

#: SLIP-132 string prefix → (network label, script type). Case-sensitive:
#: SLIP-132 prefixes are lowercase (``Zpub``/``Vpub`` — P2WSH variants —
#: are different version bytes and are not supported in v1). Testnet
#: prefixes are detected only to be refused by the mainnet-only gate.
_PREFIX_TABLE: Final[dict[str, tuple[str, str]]] = {
    "zpub": ("main", "p2wpkh"),
    "vpub": ("testnet", "p2wpkh"),
    "ypub": ("main", "p2sh_p2wpkh"),
    "upub": ("testnet", "p2sh_p2wpkh"),
    "xpub": ("main", "p2pkh"),
    "tpub": ("testnet", "p2pkh"),
}

#: SLIP-132 string prefix → version bytes (verbatim from
#: ``embit.bip32.NETWORKS``; see module docstring for provenance).
_PREFIX_TO_VERSION: Final[dict[str, bytes]] = {
    "zpub": _NETWORKS["main"]["zpub"],
    "vpub": _NETWORKS["test"]["zpub"],
    "ypub": _NETWORKS["main"]["ypub"],
    "upub": _NETWORKS["test"]["ypub"],
    "xpub": _NETWORKS["main"]["xpub"],
    "tpub": _NETWORKS["test"]["xpub"],
}

#: Version bytes → (prefix, network label, script type); the authoritative
#: detection table (the base58 prefix is a deterministic function of the
#: version bytes, so both views always agree for valid keys).
_VERSION_TABLE: Final[dict[bytes, tuple[str, str, str]]] = {
    version: (prefix, *_PREFIX_TABLE[prefix])
    for prefix, version in _PREFIX_TO_VERSION.items()
}

#: Network label → embit network dict (bech32 HRP / base58 versions for
#: address encoding). Mainnet only (ADR-0021): the gate refuses every
#: other network label before any lookup, fail closed.
_NETWORKS_BY_LABEL: Final[dict[str, dict[str, object]]] = {
    "main": _NETWORKS["main"],
}

#: Wildcard suffixes accepted when parsing a wallet descriptor. The
#: canonical form this module emits is ``{0,1}/*``; embit's own
#: serialization uses ``<0;1>/*`` — both are accepted on input, only the
#: canonical form is ever produced.
_WILDCARDS: Final[tuple[str, ...]] = ("{0,1}/*", "<0;1>/*")

#: base58 alphabet (Bitcoin), for descriptor key validation.
_B58_ALPHABET: Final[str] = (
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
)

#: Wrapper around the key for each script type (canonical template).
_WRAPPER_BY_SCRIPT: Final[dict[str, str]] = {
    "p2wpkh": "wpkh",
    "p2sh_p2wpkh": "sh(wpkh",
    "p2pkh": "pkh",
}


class WatchKeyError(ValueError):
    """A watch key or wallet descriptor could not be parsed / is unusable.

    Raised (fail closed) for: unparseable/base58-invalid input, private
    extended keys, unknown version bytes, prefix/version mismatches,
    testnet keys under the mainnet-only gate, and malformed descriptor
    strings.

    Message contract: value-free. The offending key or descriptor is
    never echoed into the message (watch-only app; keys never appear in
    errors or logs).
    """


@dataclass(frozen=True, slots=True)
class ParsedKey:
    """A parsed watch-only account-level extended public key.

    Attributes:
        hd_key: The embit :class:`~embit.bip32.HDKey` (public, non-hardened
            derivation only).
        script_type: One of ``"p2wpkh"``, ``"p2sh_p2wpkh"``, ``"p2pkh"`` —
            detected from the SLIP-132 version bytes.
        network: ``"main"`` or ``"testnet"`` — detected from the version
            bytes. The wallet engine refuses ``"testnet"`` at parse time
            (``parse_wallet_key``, ADR-0021 mainnet-only) and structurally
            (:class:`WalletDescriptor`).
    """

    hd_key: HDKey
    script_type: str
    network: str


@dataclass(frozen=True, slots=True)
class PrefixInfo:
    """Script type + network encoded in a SLIP-132 key prefix."""

    prefix: str
    network: str
    script_type: str


def detect_script_type(prefix: str) -> PrefixInfo:
    """Map a SLIP-132 key prefix to its network and script type.

    Supported public prefixes: ``zpub`` (mainnet P2WPKH), ``vpub``
    (testnet P2WPKH), ``ypub`` (mainnet P2SH-P2WPKH), ``upub`` (testnet
    P2SH-P2WPKH), ``xpub`` (mainnet P2PKH), ``tpub`` (testnet P2PKH).

    Raises:
        WatchKeyError: the prefix belongs to a *private* extended key
            (any ``*prv`` — watch-only refusal) or is unknown. The
            message never echoes the input.
    """
    if not isinstance(prefix, str):
        raise WatchKeyError("key prefix must be a string")
    if prefix.endswith("prv"):
        raise WatchKeyError(
            "watch-only: private extended keys are never handled — provide a "
            "mainnet public key (xpub/ypub/zpub)"
        )
    entry = _PREFIX_TABLE.get(prefix)
    if entry is None:
        raise WatchKeyError(
            "unknown extended-key prefix (expected zpub/vpub/ypub/upub/xpub/tpub)"
        )
    network, script_type = entry
    return PrefixInfo(prefix=prefix, network=network, script_type=script_type)


def parse_watch_key(key: str, *, require_main: bool = False) -> ParsedKey:
    """Parse a SLIP-132 extended public key and detect network/script type.

    Detects ``zpub``/``vpub`` (P2WPKH), ``ypub``/``upub`` (P2SH-P2WPKH)
    and ``xpub``/``tpub`` (P2PKH) account-level keys; the mainnet-only
    gate refuses testnet keys. Surrounding whitespace is tolerated
    (clipboard pastes); internal whitespace is not.

    The key's version bytes are the authoritative detection source; the
    string prefix must agree with them (both are deterministic functions
    of the version bytes, so a mismatch means a corrupt key) — fail
    closed.

    Args:
        key: The extended public key string.
        require_main: Enforce the mainnet-only gate at parse time
            (ADR-0021): refuse testnet keys (``vpub``/``upub``/``tpub``
            and any testnet-serialized key). Defaults to ``False`` solely
            for backwards compatibility with the Phase 0 detect-only
            contract; the wallet engine always goes through
            :func:`parse_wallet_key` (gate on) or :class:`WalletDescriptor`
            (structural gate).

    Returns:
        A :class:`ParsedKey` with the embit HDKey plus the detected
        script type and network.

    Raises:
        WatchKeyError: the input is not a string, is empty/whitespace or
            over-long, fails base58/checksum parsing, is a *private*
            extended key (watch-only refusal), carries version bytes
            outside the supported SLIP-132 set, has a prefix/version
            mismatch, or is a testnet key while ``require_main`` is
            set. The message never echoes the input.
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
            "watch-only: private extended keys are never handled — provide a "
            "mainnet public key (xpub/ypub/zpub)"
        )

    prefix = _match_known_prefix(candidate)
    expected_version = _PREFIX_TO_VERSION[prefix]
    if hd.version != expected_version:
        raise WatchKeyError(
            "extended-key version bytes do not match its prefix (corrupt key)"
        )

    _prefix, network, script_type = _VERSION_TABLE[hd.version]
    if require_main and network != "main":
        raise WatchKeyError(
            "mainnet-only: testnet extended public keys are refused — provide "
            "a mainnet xpub/ypub/zpub"
        )
    return ParsedKey(hd_key=hd, script_type=script_type, network=network)


def parse_wallet_key(key: str) -> ParsedKey:
    """Parse a watch key with the mainnet-only gate enforced at parse.

    This is the wallet engine's parse entry point: testnet
    vpub/upub/tpub keys (and any testnet-serialized key) are refused
    here, before any descriptor is built or any address derived
    (P0 security-review carry-over: the gate lives in parse, not only in
    derivation; network per ADR-0021). :func:`derive_addresses` keeps its
    own gate as a second layer.

    Raises:
        WatchKeyError: everything :func:`parse_watch_key` raises, plus
            the testnet refusal. Value-free messages.
    """
    return parse_watch_key(key, require_main=True)


def _match_known_prefix(candidate: str) -> str:
    """Return the SLIP-132 prefix ``candidate`` starts with (fail closed).

    The base58 representation of a supported version byte always begins
    with the version's 4-char SLIP-132 prefix (verified across depths for
    every supported version), so the leading prefix identifies the
    version bytes independently of the decoded payload.
    """
    for prefix in _PREFIX_TO_VERSION:
        if candidate.startswith(prefix):
            return prefix
    raise WatchKeyError(
        "unknown extended-key version bytes (expected zpub/vpub/ypub/upub/xpub/tpub)"
    )


@dataclass(frozen=True, slots=True)
class WalletDescriptor:
    """A mainnet watch-only wallet: parsed account key + canonical descriptor.

    Invariants (enforced in ``__post_init__``, fail closed):

    - mainnet-only (ADR-0021): ``network`` is ``"main"`` — a testnet key
      cannot be turned into a wallet descriptor at all (structural half
      of the parse-time gate);
    - ``script_type``/``network`` agree with ``parsed``;
    - ``descriptor`` is a checksummed canonical descriptor that embit's
      descriptor engine can parse back (round-trip validation) and whose
      key and script type match ``parsed``.

    Attributes:
        parsed: The account-level :class:`ParsedKey` (public key only).
        script_type: ``"p2wpkh"`` | ``"p2sh_p2wpkh"`` | ``"p2pkh"``.
        network: Always ``"main"`` (mainnet-only gate, ADR-0021).
        descriptor: Canonical checksummed descriptor string, e.g.
            ``wpkh([fp/84'/0'/0']zpub…/{0,1}/*)#checksum``. The origin
            fingerprint is the supplied account key's own fingerprint
            (``hash160(pubkey)[:4]``) — the master fingerprint is not
            recoverable from an account-level key alone; device
            registration (Phase 3) supplies and verifies the device xfp
            per OQ18.
    """

    parsed: ParsedKey
    script_type: str
    network: str
    descriptor: str

    def __post_init__(self) -> None:
        if self.network != "main" or self.parsed.network != "main":
            raise WatchKeyError(
                "mainnet-only: wallet descriptors require a mainnet key"
            )
        if self.parsed.script_type != self.script_type:
            raise WatchKeyError("wallet descriptor script type does not match its key")
        if self.script_type not in SCRIPT_PURPOSES:
            raise WatchKeyError("unsupported script type for a wallet descriptor")
        _validate_descriptor_string(self.descriptor, self.parsed)

    @classmethod
    def from_key(cls, key: str) -> WalletDescriptor:
        """Parse a user-supplied extended public key into a wallet descriptor.

        Gated parse (mainnet-only, watch-only) followed by canonical
        descriptor construction with checksum and round-trip validation.

        Raises:
            WatchKeyError: on any parse/gate failure (value-free).
        """
        parsed = parse_wallet_key(key)
        return cls(
            parsed=parsed,
            script_type=parsed.script_type,
            network=parsed.network,
            descriptor=_build_descriptor_string(parsed),
        )

    @classmethod
    def from_descriptor_string(cls, descriptor: str) -> WalletDescriptor:
        """Rebuild a :class:`WalletDescriptor` from its descriptor string.

        Accepts the canonical form this module emits (with or without the
        ``#checksum`` suffix) and embit's equivalent serialization
        (``84h`` hardened notation, ``<0;1>`` multibranch). The string is
        template-parsed strictly (only ``wpkh``/``sh(wpkh)``/``pkh`` with
        the standard ``{branch}/{index}`` wildcard and a standard BIP44/49/84
        origin), the key is re-parsed, script type and network are
        cross-checked against the key's SLIP-132 version bytes, and the
        canonical descriptor is rebuilt from the parsed key (preserving a
        supplied origin fingerprint).

        Raises:
            WatchKeyError: on any structural, checksum, gate, or
                consistency failure (value-free messages).
        """
        if not isinstance(descriptor, str):
            raise WatchKeyError("descriptor must be a string")
        candidate = descriptor.strip()
        if not candidate or len(candidate) > _MAX_DESCRIPTOR_CHARS or any(
            c.isspace() for c in candidate
        ):
            raise WatchKeyError(
                "descriptor must be a single descriptor string without whitespace"
            )

        body, _ = _split_checksum(candidate)
        script_type, origin_fingerprint, key_string = _template_parse(body)

        parsed = parse_wallet_key(key_string)
        if parsed.script_type != script_type:
            raise WatchKeyError(
                "descriptor function does not match the key's script type"
            )
        rebuilt = _build_descriptor_string(parsed, fingerprint_override=origin_fingerprint)
        return cls(
            parsed=parsed,
            script_type=parsed.script_type,
            network=parsed.network,
            descriptor=rebuilt,
        )


def _script_dispatch(script_type: str) -> tuple[int, str]:
    """Look up ``(purpose, wrapper)`` for a wallet script type (fail closed).

    A hand-built :class:`ParsedKey` may carry any ``script_type`` string;
    the descriptor encode/dispatch lookups map an unknown value to
    :class:`WatchKeyError` (value-free) instead of a raw ``KeyError``.
    """
    try:
        return SCRIPT_PURPOSES[script_type], _WRAPPER_BY_SCRIPT[script_type]
    except KeyError as exc:
        raise WatchKeyError(
            "unsupported script type for a wallet descriptor"
        ) from exc


def _build_descriptor_string(
    parsed: ParsedKey, fingerprint_override: str | None = None
) -> str:
    """Build the canonical checksummed descriptor string for ``parsed``.

    Shape per detected script type (ticket notation, apostrophe-hardened
    origin path, ``{0,1}`` receive/change multibranch, ``*`` wildcard),
    BIP44/49/84 mainnet coin type 0':

    - ``wpkh([fp/84'/0'/0']KEY/{0,1}/*)``          (P2WPKH, BIP84)
    - ``sh(wpkh([fp/49'/0'/0']KEY/{0,1}/*))``      (P2SH-P2WPKH, BIP49)
    - ``pkh([fp/44'/0'/0']KEY/{0,1}/*)``           (P2PKH, BIP44)

    The fingerprint is the account key's own fingerprint unless an
    origin fingerprint was supplied in a parsed descriptor string.
    Checksummed via ``embit.descriptor.checksum`` (Bitcoin Core
    algorithm).
    """
    fingerprint = (
        fingerprint_override
        if fingerprint_override is not None
        else parsed.hd_key.my_fingerprint.hex()
    )
    purpose, wrapper = _script_dispatch(parsed.script_type)
    origin = f"{fingerprint}/{purpose}'/{MAINNET_COIN_TYPE}'/0'"
    key_string = parsed.hd_key.to_base58()
    inner = f"{wrapper}([{origin}]{key_string}/{{0,1}}/*)"
    body = f"{inner})" if wrapper == "sh(wpkh" else inner
    return f"{body}#{_descriptor_checksum(body)}"


def _split_checksum(candidate: str) -> tuple[str, str | None]:
    """Split ``body#checksum``; verify the checksum when present.

    embit's ``Descriptor.from_string`` ignores the checksum suffix, so it
    is validated explicitly here (fail closed on mismatch).
    """
    if "#" not in candidate:
        return candidate, None
    body, _, suffix = candidate.rpartition("#")
    if not body or len(suffix) != 8 or any(c not in _CHECKSUM_CHARSET for c in suffix):
        raise WatchKeyError("malformed descriptor checksum")
    if _descriptor_checksum(body) != suffix:
        raise WatchKeyError("descriptor checksum mismatch")
    return body, suffix


_CHECKSUM_CHARSET: Final[str] = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _template_parse(body: str) -> tuple[str, str | None, str]:
    """Strictly template-parse a descriptor body.

    Returns ``(script_type, origin_fingerprint | None, key_string)``.
    Only the three v1 wallet shapes with the standard ``{branch}/{index}``
    wildcard are accepted; anything else is refused (fail closed).
    """
    for script_type, wrapper in _WRAPPER_BY_SCRIPT.items():
        open_token = f"{wrapper}("
        if not body.startswith(open_token):
            continue
        close_tokens = "))" if wrapper == "sh(wpkh" else ")"
        if not body.endswith(close_tokens):
            break  # right function word, unbalanced parens — malformed
        inner = body[len(open_token) : -len(close_tokens)]
        origin_fingerprint: str | None = None
        if inner.startswith("["):
            closing = inner.find("]")
            if closing < 0:
                raise WatchKeyError("malformed descriptor origin")
            origin_fingerprint = _validate_origin(
                inner[1:closing], script_type
            )
            inner = inner[closing + 1 :]
        key_string = _split_wildcard(inner)
        if not key_string or any(c not in _B58_ALPHABET for c in key_string):
            raise WatchKeyError("malformed descriptor key")
        if len(key_string) > _MAX_KEY_CHARS:
            raise WatchKeyError("descriptor key is over-long")
        return script_type, origin_fingerprint, key_string
    raise WatchKeyError(
        "unsupported descriptor (expected wpkh/sh(wpkh)/pkh with {0,1}/*)"
    )


def _split_wildcard(inner: str) -> str:
    """Strip the ``/<wildcard>`` suffix, enforcing the accepted set."""
    for wildcard in _WILDCARDS:
        suffix = f"/{wildcard}"
        if inner.endswith(suffix):
            return inner[: -len(suffix)]
    raise WatchKeyError(
        "descriptor must end in the standard {0,1}/* receive/change wildcard"
    )


def _validate_origin(origin: str, script_type: str) -> str:
    """Validate a ``fp/purpose'/coin'/0'`` origin; return the fingerprint.

    Only the standard hardened path for ``script_type`` with the mainnet
    coin type (BIP44/49/84, coin 0') is accepted (both apostrophe and
    ``h`` hardened notation); testnet coin-type origins are refused
    here — the network gate in :func:`parse_wallet_key` refuses testnet
    *keys* (ADR-0021).
    """
    parts = origin.split("/")
    if len(parts) != 4:
        raise WatchKeyError("malformed descriptor origin path")
    fingerprint = parts[0].lower()
    if len(fingerprint) != 8 or any(c not in "0123456789abcdef" for c in fingerprint):
        raise WatchKeyError("malformed descriptor origin fingerprint")
    purpose = SCRIPT_PURPOSES[script_type]
    expected = (
        [f"{purpose}'", f"{purpose}h"],
        [f"{MAINNET_COIN_TYPE}'", f"{MAINNET_COIN_TYPE}h"],
        ["0'", "0h"],
    )
    for value, allowed in zip(parts[1:], expected):
        if value not in allowed:
            raise WatchKeyError(
                "descriptor origin path must be the standard mainnet hardened "
                "path for its script type"
            )
    return fingerprint


def _validate_descriptor_string(descriptor: str, parsed: ParsedKey) -> None:
    """Round-trip validation: the stored descriptor must parse back.

    Verifies the checksum suffix (when present), parses the body with
    embit's descriptor engine, and requires the engine's key to be the
    very key in ``parsed``. Raises :class:`WatchKeyError` (value-free) on
    any failure — a :class:`WalletDescriptor` instance therefore always
    carries a descriptor the independent embit engine agrees with.
    """
    body, _ = _split_checksum(descriptor)
    expected_wrapper = _script_dispatch(parsed.script_type)[1]
    if not body.startswith(f"{expected_wrapper}("):
        raise WatchKeyError("descriptor does not match its script type")
    try:
        engine_descriptor = Descriptor.from_string(body)
    except Exception as exc:
        raise WatchKeyError("descriptor failed to parse") from exc
    engine_keys = engine_descriptor.keys
    if len(engine_keys) != 1:
        raise WatchKeyError("descriptor must contain exactly one key")
    engine_key = engine_keys[0].key
    if engine_key.to_base58() != parsed.hd_key.to_base58():
        raise WatchKeyError("descriptor key does not match the parsed key")

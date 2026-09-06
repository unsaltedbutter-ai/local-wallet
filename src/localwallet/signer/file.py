"""File-based airgap signer: microSD/USB/folder PSBT round-trip (TCK-P3-001).

Implements the file conventions of ADR-0014 (OQ17):

- **Encoding:** base64 **text** (not binary) — cross-OS/SD-safe,
  human-inspectable, no binary mangling.
- **Naming:** ``localwallet-unsigned-<ref>.psbt.b64`` for exports and
  ``localwallet-signed-<ref>.psbt.b64`` for imports, where ``<ref>`` is the
  first 8 characters of a sanitized transaction reference (hex/alnum only,
  else the first 8 hex chars of its SHA-256 — see :func:`_sanitize_tx_ref`).
  The fixed prefixes sort the two directions apart and the ``<ref>`` prefix
  makes collisions across transactions unlikely.
- **Checksums:** every export writes a ``<name>.sha256`` sidecar (hex digest
  + newline). On import a present sidecar MUST match (fail closed); an
  absent sidecar is allowed and reported via ``SignedResult.checksum_verified
  = False`` (ADR-0014).
- **Container-size ceiling:** export and import both refuse base64 PSBT
  text over :data:`_MAX_PSBT_TEXT_CHARS` characters — the transfer-folder
  gateway bounds its payload BEFORE any decode/parse, so downstream
  consumers (``localwallet.tx.revalidate``) only ever receive bounded
  data, exactly as revalidate's parse-before-bounds note assumes.
- **Value-free errors:** messages never echo PSBT content, addresses,
  amounts, or transfer-folder filenames.

Security note (ADR-0014): PSBTs are **public data** (they carry no keys) so
writing them to a transfer folder is safe, but this module never writes
anything else there — no secrets, no wallet material.
"""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from embit.psbt import PSBT

from localwallet.signer.base import SignedResult, SignerError

__all__ = ["ExportedFiles", "FilePsbtSigner", "SignedResult", "SignerError"]

#: Filename prefixes and suffixes (ADR-0014).
_UNSIGNED_PREFIX = "localwallet-unsigned-"
_SIGNED_PREFIX = "localwallet-signed-"
_SUFFIX = ".psbt.b64"
_CHECKSUM_SUFFIX = ".sha256"

#: BIP174 PSBT magic bytes.
_PSBT_MAGIC = b"psbt\xff"

#: Length of the transaction-reference prefix embedded in filenames.
_REF_LEN = 8

#: ASCII characters allowed verbatim in a transaction-reference prefix
#: (ADR-0014: hex/alnum only — nothing outside [0-9A-Za-z]).
_REF_ALNUM_ASCII = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

#: Container-size ceiling in characters of base64 PSBT text (TCK-P3-005
#: rider R5): both directions refuse larger payloads BEFORE decode/parse,
#: so the re-validation gateway downstream only ever sees bounded data.
#: 200_000 chars ≈ 150 KB of PSBT — comfortably above anything the v1
#: send flow builds (Core's 100 KB standard tx weight ceiling) and far
#: below unbounded transfer-file sizes.
_MAX_PSBT_TEXT_CHARS = 200_000

#: Byte-size ceiling applied at ``stat()`` time, BEFORE any read (TCK-SEC-004
#: change 3): a multi-GB planted file must be refused without ever being
#: read into memory. The allowance covers ADR-0014 trailing whitespace and
#: encoding overhead between the byte count on disk and the character count
#: of the stripped base64 text; the EXACT character-level ceiling
#: (:data:`_MAX_PSBT_TEXT_CHARS`, applied to the stripped text) is
#: re-checked after the bounded read — this byte gate is a memory guard,
#: not the semantic ceiling.
_MAX_PSBT_FILE_BYTES = _MAX_PSBT_TEXT_CHARS + 4096

#: Signer identifier reported in SignedResult (ADR-0014).
_SIGNER_NAME = "file"


@dataclass(frozen=True, slots=True)
class ExportedFiles:
    """Paths written by :meth:`FilePsbtSigner.export_unsigned`."""

    unsigned_path: Path
    checksum_path: Path


def _sanitize_tx_ref(tx_ref: str) -> str:
    """Return a filename-safe transaction-reference prefix (≤ 8 chars).

    ADR-0014: a reference that is non-empty and ASCII hex/alnum-only
    (``[0-9A-Za-z]``) is used as-is (truncated to :data:`_REF_LEN`).
    Anything else — including an empty string, non-ASCII alphanumerics
    (e.g. ÄÖÜ), slashes, or other path-sensitive characters — is hashed
    with SHA-256 and truncated, so no ``tx_ref`` can ever inject a path
    separator or collide with the directory layout. The output is value-free
    (a hash, never user content).
    """
    if tx_ref and all(c in _REF_ALNUM_ASCII for c in tx_ref):
        return tx_ref[:_REF_LEN]
    digest = hashlib.sha256(tx_ref.encode("utf-8")).hexdigest()
    return digest[:_REF_LEN]


def _validate_psbt_text(content: str) -> None:
    """Refuse an export payload that is not a well-formed base64 PSBT.

    Container-size ceiling first (bounds BEFORE any decode), then strict
    base64 decode → BIP174 magic prefix → embit parse — all run BEFORE any
    byte is written toward a device (A6): a device must never receive a
    malformed or mislabeled container. Errors are value-free.
    """
    if len(content) > _MAX_PSBT_TEXT_CHARS:
        raise SignerError("psbt text exceeds the maximum container size")
    try:
        raw = base64.b64decode(content, validate=True)
    except Exception as exc:  # containment: invalid base64
        raise SignerError("psbt text is not valid base64") from exc
    if not raw.startswith(_PSBT_MAGIC):
        raise SignerError("psbt text is not a PSBT")
    try:
        PSBT.parse(raw)
    except Exception as exc:  # containment: embit parse errors vary
        raise SignerError("psbt text is not a valid PSBT") from exc


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (temp file + ``os.replace``).

    The temp file is created in the same directory (required for an atomic
    rename) with a unique name; on failure the temp is cleaned up and the
    export refuses. A reader never observes a partially-written file.
    """
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
    except OSError as exc:
        raise SignerError("could not write export file") from exc


class FilePsbtSigner:
    """Airgap signer over a transfer folder (SD mount, USB stick, plain dir).

    The constructor creates ``directory`` (and parents) if missing. The
    directory holds exported unsigned PSBTs and their SHA-256 sidecars;
    imported signed PSBTs are expected to follow the ADR-0014 convention.
    """

    def __init__(self, directory: Path) -> None:
        if not isinstance(directory, Path):
            raise SignerError("directory must be a pathlib.Path")
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    @property
    def name(self) -> str:
        return _SIGNER_NAME

    # -- export ---------------------------------------------------------

    def export_unsigned(self, psbt_base64: str, tx_ref: str) -> ExportedFiles:
        """Write an unsigned base64 PSBT plus its SHA-256 sidecar.

        The unsigned file is ``localwallet-unsigned-<ref>.psbt.b64`` and the
        sidecar ``localwallet-unsigned-<ref>.psbt.b64.sha256`` (hex digest +
        newline), per ADR-0014.

        Refusals (value-free):
        - a non-string / empty PSBT;
        - a payload over the container-size ceiling (bounds before decode);
        - a payload that does not decode as base64 PSBT text (validated
          BEFORE any write toward a device);
        - a non-string ``tx_ref``;
        - an existing unsigned file with *different* content (same content is
          idempotent and allowed — and the sidecar is (re)written per
          ADR-0014, healing a missing/mismatched one);
        - a ``tx_ref`` whose sanitized prefix would collide with an existing
          ``localwallet-signed-<ref>.psbt.b64`` file.

        Args:
            psbt_base64: base64 PSBT **text** to export.
            tx_ref: transaction reference; sanitized per :func:`_sanitize_tx_ref`.

        Returns:
            :class:`ExportedFiles` with the unsigned and checksum paths.
        """
        if not isinstance(psbt_base64, str):
            raise SignerError("psbt_base64 must be a string")
        content = psbt_base64.strip()
        if not content:
            raise SignerError("no PSBT text to export")
        if not isinstance(tx_ref, str):
            raise SignerError("tx_ref must be a string")

        # Validate the payload before writing anything toward a device.
        _validate_psbt_text(content)

        ref = _sanitize_tx_ref(tx_ref)
        unsigned_path = self.directory / f"{_UNSIGNED_PREFIX}{ref}{_SUFFIX}"
        checksum_path = Path(str(unsigned_path) + _CHECKSUM_SUFFIX)

        # Refuse a reference that would collide with an existing signed file.
        signed_path = self.directory / f"{_SIGNED_PREFIX}{ref}{_SUFFIX}"
        if signed_path.exists():
            raise SignerError(
                "refused: the transaction reference would collide with an "
                "existing signed file"
            )

        file_bytes = (content + "\n").encode("utf-8")
        digest = hashlib.sha256(file_bytes).hexdigest()

        # Overwrite policy: same content is idempotent; different content is
        # refused. The idempotent path ALSO (re)writes the sidecar (ADR-0014
        # "every export writes a sidecar"), healing a missing/mismatched one.
        if unsigned_path.exists():
            try:
                existing = unsigned_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise SignerError("could not read the existing unsigned file") from exc
            if existing != content:
                raise SignerError(
                    "refused: an existing unsigned file has different content"
                )

        # Atomic export (temp + os.replace) for payload AND sidecar.
        self.directory.mkdir(parents=True, exist_ok=True)
        _atomic_write(unsigned_path, file_bytes)
        _atomic_write(checksum_path, (digest + "\n").encode("utf-8"))
        return ExportedFiles(
            unsigned_path=unsigned_path, checksum_path=checksum_path
        )

    # -- import ---------------------------------------------------------

    def import_signed(
        self, path: Path, expected_tx_ref: str | None = None
    ) -> SignedResult:
        """Read, validate, and return a signed base64 PSBT (ADR-0014).

        Validation order (fail closed, value-free errors naming the
        *convention*, never the content):

        1. **Filename convention** — must match ``localwallet-signed-*.psbt.b64``;
        2. **Checksum sidecar** — if a ``<name>.sha256`` sidecar exists it MUST
           match the file's SHA-256 (mismatch is a hard refusal); an absent
           sidecar is allowed and reported via ``checksum_verified=False``;
        3. **base64 decode** — the text must be valid base64, and must not
           exceed the container-size ceiling (bounds before decode);
        4. **PSBT magic** — the decoded bytes must start with ``b"psbt\\xff"``;
        5. **Structure** — the bytes must parse as an embit PSBT and every
           input must carry a signature (partial sig or final witness); an
           unsigned PSBT is refused ("file contains no signatures").

        Args:
            path: the signed file to import.
            expected_tx_ref: if given, the file's embedded reference prefix
                must match the sanitized reference of this value.

        Returns:
            :class:`SignedResult` with the stripped base64 PSBT text returned
            **verbatim** from the file (exactly what was read, never
            re-serialized — so ``checksum_verified`` attests precisely that
            text), ``signer_name="file"``, and the sidecar-verification flag.
        """
        if not isinstance(path, Path):
            raise SignerError("path must be a pathlib.Path")
        name = path.name
        if not (name.startswith(_SIGNED_PREFIX) and name.endswith(_SUFFIX)):
            raise SignerError(
                "file name must follow the localwallet-signed-*.psbt.b64 convention"
            )

        # expected reference must match the embedded prefix.
        if expected_tx_ref is not None:
            embedded = name[len(_SIGNED_PREFIX) : -len(_SUFFIX)]
            if embedded != _sanitize_tx_ref(expected_tx_ref):
                raise SignerError(
                    "file reference does not match the expected transaction"
                )

        # Container-size gate at stat() time (TCK-SEC-004 change 3): the file
        # size is bounded BEFORE any byte is read, so an oversized planted
        # file can never spike memory on the money path. Value-free.
        try:
            file_size = path.stat().st_size
        except OSError as exc:
            raise SignerError("could not read the signed file") from exc
        if file_size > _MAX_PSBT_FILE_BYTES:
            raise SignerError("signed file exceeds the maximum container size")

        try:
            data = path.read_bytes()
        except OSError as exc:
            raise SignerError("could not read the signed file") from exc
        if not data:
            raise SignerError("signed file is empty")

        # Checksum sidecar (present ⇒ must match; absent ⇒ allowed).
        checksum_verified = False
        sidecar = path.with_name(name + _CHECKSUM_SUFFIX)
        if sidecar.exists():
            try:
                expected_digest = sidecar.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise SignerError("could not read the checksum sidecar") from exc
            actual_digest = hashlib.sha256(data).hexdigest()
            if not expected_digest or expected_digest.lower() != actual_digest:
                raise SignerError("checksum mismatch")  # fail closed, value-free
            checksum_verified = True

        # Text decode (base64 text per ADR-0014), then size + base64 + magic.
        try:
            text = data.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise SignerError("signed file is not valid base64 PSBT text") from exc
        if not text:
            raise SignerError("signed file is empty")
        if len(text) > _MAX_PSBT_TEXT_CHARS:
            raise SignerError("signed file exceeds the maximum container size")
        try:
            raw = base64.b64decode(text, validate=True)
        except Exception as exc:  # containment: invalid base64
            raise SignerError("signed file is not valid base64 PSBT text") from exc
        if not raw.startswith(_PSBT_MAGIC):
            raise SignerError("signed file is not a PSBT")

        # Structural parse + signature presence (the "is NOT the unsigned
        # file" check — an unsigned PSBT carries no partial signatures).
        try:
            psbt = PSBT.parse(raw)
        except Exception as exc:  # containment: embit parse errors vary
            raise SignerError("signed file is not a valid PSBT") from exc
        if not psbt.inputs:
            raise SignerError("file contains no signatures (unsigned PSBT?)")
        for scope in psbt.inputs:
            if not _input_is_signed(scope):
                raise SignerError("file contains no signatures (unsigned PSBT?)")

        return SignedResult(
            psbt_base64=text,
            signer_name=_SIGNER_NAME,
            checksum_verified=checksum_verified,
        )

    # -- helper ---------------------------------------------------------

    def signed_import_path(self, tx_ref: str) -> Path:
        """The signed-file path the ADR-0014 convention expects for ``tx_ref``.

        Deterministic filename derivation for the app-layer handoff
        (TCK-P3-005): ``localwallet-signed-<ref>.psbt.b64`` with the SAME
        sanitized reference prefix :meth:`export_unsigned` used, so the
        signer directory is scanned by convention — never by user- or
        model-supplied paths. Pure path computation (no I/O).
        """
        if not isinstance(tx_ref, str):
            raise SignerError("tx_ref must be a string")
        return self.directory / f"{_SIGNED_PREFIX}{_sanitize_tx_ref(tx_ref)}{_SUFFIX}"

    def list_pending_exports(self) -> list[Path]:
        """Return exported unsigned PSBT files still awaiting import.

        Sorted for deterministic narration (CLI handoff helper, PROJECT.md
        §10). Sidecar files are not included.
        """
        return sorted(self.directory.glob(f"{_UNSIGNED_PREFIX}*{_SUFFIX}"))


def _input_is_signed(scope) -> bool:
    """True when an input scope carries a signature (partial sig or final).

    Uses truthiness, not ``is not None``: an input whose final fields are
    PRESENT but EMPTY carries no signature and must not count as signed
    (A1) — only non-empty final data or a partial signature satisfy it.
    """
    if getattr(scope, "partial_sigs", None):
        return True
    if getattr(scope, "final_scriptsig", None):
        return True
    return bool(getattr(scope, "final_scriptwitness", None))

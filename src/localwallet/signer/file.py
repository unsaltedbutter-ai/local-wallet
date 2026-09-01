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
- **Value-free errors:** messages never echo PSBT content, addresses,
  amounts, or transfer-folder filenames.

Security note (ADR-0014): PSBTs are **public data** (they carry no keys) so
writing them to a transfer folder is safe, but this module never writes
anything else there — no secrets, no wallet material.
"""

from __future__ import annotations

import base64
import hashlib
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

#: Signer identifier reported in SignedResult (ADR-0014).
_SIGNER_NAME = "file"


@dataclass(frozen=True, slots=True)
class ExportedFiles:
    """Paths written by :meth:`FilePsbtSigner.export_unsigned`."""

    unsigned_path: Path
    checksum_path: Path


def _sanitize_tx_ref(tx_ref: str) -> str:
    """Return a filename-safe transaction-reference prefix (≤ 8 chars).

    ADR-0014: a reference that is non-empty and hex/alnum-only is used as-is
    (truncated to :data:`_REF_LEN`). Anything else — including an empty
    string, slashes, or other path-sensitive characters — is hashed with
    SHA-256 and truncated, so no ``tx_ref`` can ever inject a path
    separator or collide with the directory layout. The output is value-free
    (a hash, never user content).
    """
    if tx_ref and all(c.isalnum() for c in tx_ref):
        return tx_ref[:_REF_LEN]
    digest = hashlib.sha256(tx_ref.encode("utf-8")).hexdigest()
    return digest[:_REF_LEN]


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
        - an existing unsigned file with *different* content (same content is
          idempotent and allowed);
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

        # Overwrite policy: same content is idempotent; different content is refused.
        if unsigned_path.exists():
            try:
                existing = unsigned_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise SignerError("could not read the existing unsigned file") from exc
            if existing != content:
                raise SignerError(
                    "refused: an existing unsigned file has different content"
                )
            return ExportedFiles(
                unsigned_path=unsigned_path, checksum_path=checksum_path
            )

        file_bytes = (content + "\n").encode("utf-8")
        self.directory.mkdir(parents=True, exist_ok=True)
        unsigned_path.write_bytes(file_bytes)
        checksum_path.write_text(hashlib.sha256(file_bytes).hexdigest() + "\n", encoding="utf-8")
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
        3. **base64 decode** — the text must be valid base64;
        4. **PSBT magic** — the decoded bytes must start with ``b"psbt\\xff"``;
        5. **Structure** — the bytes must parse as an embit PSBT and every
           input must carry a signature (partial sig or final witness); an
           unsigned PSBT is refused ("file contains no signatures").

        Args:
            path: the signed file to import.
            expected_tx_ref: if given, the file's embedded reference prefix
                must match the sanitized reference of this value.

        Returns:
            :class:`SignedResult` with normalized base64 PSBT text,
            ``signer_name="file"``, and the sidecar-verification flag.
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

        # Text decode (base64 text per ADR-0014), then base64 + PSBT magic.
        try:
            text = data.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise SignerError("signed file is not valid base64 PSBT text") from exc
        if not text:
            raise SignerError("signed file is empty")
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
            psbt_base64=psbt.to_base64(),
            signer_name=_SIGNER_NAME,
            checksum_verified=checksum_verified,
        )

    # -- helper ---------------------------------------------------------

    def list_pending_exports(self) -> list[Path]:
        """Return exported unsigned PSBT files still awaiting import.

        Sorted for deterministic narration (CLI handoff helper, PROJECT.md
        §10). Sidecar files are not included.
        """
        return sorted(self.directory.glob(f"{_UNSIGNED_PREFIX}*{_SUFFIX}"))


def _input_is_signed(scope) -> bool:
    """True when an input scope carries a signature (partial sig or final)."""
    if getattr(scope, "partial_sigs", None):
        return True
    if getattr(scope, "final_scriptsig", None) is not None:
        return True
    return getattr(scope, "final_scriptwitness", None) is not None

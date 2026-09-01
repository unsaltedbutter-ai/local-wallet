"""The :class:`Signer` interface and shared result types (TCK-P3-001).

One interface, three implementations (PROJECT.md §7.6): FilePsbtSigner
(file/microSD airgap, ADR-0014), HwiUsbSigner (USB, TCK-P3-003), QrSigner
(QR, v2). Signers **receive base64 PSBT text and return base64 PSBT text**.

Layering and trust (AGENTS.md invariants):

- **No network I/O** in this layer (lint-enforced — the only networked
  module is ``localwallet.chain``).
- **No secret handling** in this layer: devices hold the keys. The app is
  watch-only and never sees or forwards private material; this interface
  only shuffles PSBT text (public data).
- **The app owns validation before and after** — a signed PSBT is
  re-parsed and deterministically re-validated against the intended
  transaction before any broadcast (PROJECT.md §7.5). This module does not
  re-validate; it just transports.

Error messages are value-free throughout (never echo PSBT content,
addresses, amounts, or transfer-folder filenames): those flow into logs and
error envelopes (PROJECT.md §7.8).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class SignerError(Exception):
    """A signer operation was refused.

    Messages are value-free: they never echo PSBT content, addresses,
    amounts, or user transfer-folder filenames.
    """


@dataclass(frozen=True, slots=True)
class SignedResult:
    """Outcome of a signing operation: base64 PSBT text.

    ``psbt_base64`` is the base64 PSBT text returned **verbatim** by the
    signer: the file signer returns the stripped file text exactly as read
    (never re-serialized), so ``checksum_verified`` attests precisely that
    text. ``signer_name`` identifies which implementation produced it.
    ``checksum_verified`` is ``True`` only when the file signer confirmed a
    checksum sidecar matched the signed file on import (ADR-0014); other
    signers and sidecar-less imports report ``False``.
    """

    psbt_base64: str
    signer_name: str
    checksum_verified: bool = False


class Signer(ABC):
    """Abstract signer gateway.

    Implementations take base64 PSBT text and return base64 PSBT text. The
    ``name`` identifies the implementation (e.g. ``"file"``, ``"hwi"``) for
    narration and audit.

    NOTE: the file signer is inherently human-mediated (export → device
    sign → import) and is driven through its own export/import API rather
    than a single :meth:`sign_unsigned` call (see :mod:`localwallet.signer.file`).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human/audit identifier for this signer implementation."""

    @abstractmethod
    def sign_unsigned(self, psbt_base64: str) -> SignedResult:
        """Sign the given base64 PSBT and return a base64 signed PSBT.

        Raises:
            SignerError: on any refusal (value-free messages).
        """

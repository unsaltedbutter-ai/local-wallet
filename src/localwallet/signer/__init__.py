"""Signer subsystem: Signer interface (file / HWI-USB / QR later).

FilePsbtSigner (TCK-P3-001) is the first-class airgap path
(ADR-0014 / OQ17); HwiUsbSigner lands in TCK-P3-003; QrSigner is v2.
Signers receive base64 PSBT text and return base64 PSBT text; this layer
performs no network I/O and no secret handling (devices hold the keys).
"""

from localwallet.signer.base import SignedResult, Signer, SignerError
from localwallet.signer.file import ExportedFiles, FilePsbtSigner

__all__ = [
    "ExportedFiles",
    "FilePsbtSigner",
    "SignedResult",
    "Signer",
    "SignerError",
]

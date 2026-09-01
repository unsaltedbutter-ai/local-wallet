"""Signer interface conformance tests (TCK-P3-001, base.py).

Verifies the abstract :class:`Signer` contract and the shared
:class:`SignedResult` type: a concrete implementation must expose a ``name``
property and a ``sign_unsigned`` that returns a frozen ``SignedResult``
carrying base64 PSBT text. No money logic, no network, no secrets here —
just the transport interface shape.
"""

from dataclasses import FrozenInstanceError

import pytest

from localwallet.signer.base import SignedResult, Signer, SignerError


class _EchoSigner(Signer):
    """Minimal conformant implementation: echoes the PSBT back unchanged."""

    @property
    def name(self) -> str:
        return "echo"

    def sign_unsigned(self, psbt_base64: str) -> SignedResult:
        if not psbt_base64:
            raise SignerError("no PSBT text to sign")  # value-free
        return SignedResult(psbt_base64=psbt_base64, signer_name=self.name)


class TestSignerProtocol:
    def test_signer_is_abstract(self):
        # The interface cannot be instantiated directly; implementations
        # must provide both the name property and sign_unsigned.
        with pytest.raises(TypeError):
            Signer()

    def test_concrete_conforms(self):
        signer = _EchoSigner()
        assert isinstance(signer, Signer)
        assert signer.name == "echo"

    def test_sign_unsigned_returns_signed_result(self):
        result = _EchoSigner().sign_unsigned("AAAA")
        assert isinstance(result, SignedResult)
        assert result.psbt_base64 == "AAAA"
        assert result.signer_name == "echo"

    def test_checksum_verified_defaults_false(self):
        result = _EchoSigner().sign_unsigned("AAAA")
        assert result.checksum_verified is False

    def test_checksum_verified_can_be_set(self):
        result = SignedResult(
            psbt_base64="AAAA", signer_name="file", checksum_verified=True
        )
        assert result.checksum_verified is True


class TestSignedResult:
    def test_is_frozen(self):
        result = SignedResult(psbt_base64="AAAA", signer_name="file")
        with pytest.raises(FrozenInstanceError):
            result.psbt_base64 = "BBBB"  # type: ignore[misc]

    def test_signer_error_is_value_free_style(self):
        # SignerError is the shared refusal type; messages carry no content.
        assert issubclass(SignerError, Exception)

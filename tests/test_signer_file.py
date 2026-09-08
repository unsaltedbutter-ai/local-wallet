"""FilePsbtSigner tests (TCK-P3-001, ADR-0014).

Covers: export → simulate signing (embit-sign with the BIP32 test-vector
key IN TESTS ONLY) → import round-trip; checksum verify / mismatch refuse;
garbage input; unsigned-PSBT refusal (no signatures); overwrite refusal
(different content); idempotent same-content export; filename sanitization;
``list_pending_exports``; value-free error assertions.

Signing uses the public BIP32 test-vector seed — throwaway fixture material
only, never real funds, watch-only app (same convention as test_tx_psbt.py).
"""

import hashlib
from io import BytesIO
from pathlib import Path

import pytest
from embit import bip32, ec, script
from embit.networks import NETWORKS
from embit.psbt import PSBT
from embit.transaction import Witness

from localwallet.signer import ExportedFiles, FilePsbtSigner, SignedResult, SignerError
from localwallet.tx.psbt import (
    PsbtInputSource,
    build_unsigned_psbt,
    psbt_to_base64,
)

# Public BIP32 test vector 1 seed — throwaway fixture material only.
# Mainnet coin type 0 (ADR-0021): the tx engine refuses testnet change
# addresses, so the fixture account is the canonical mainnet BIP84 path.
SEED = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
ACCOUNT_PATH = (84 + 2**31, 0 + 2**31, 2**31)


def account_key() -> bip32.HDKey:
    return bip32.HDKey.from_seed(SEED).derive(list(ACCOUNT_PATH)).to_public()


def fingerprint() -> bytes:
    return account_key().my_fingerprint


def spk(branch: int, index: int) -> bytes:
    key = account_key().derive([branch, index]).key
    return script.p2wpkh(key).data


def change_address() -> str:
    key = account_key().derive([1, 7]).key
    return script.p2wpkh(key).address(NETWORKS["main"])


def build_unsigned() -> PSBT:
    """A single-input unsigned PSBT via the real tx engine API."""
    src = PsbtInputSource(
        txid="ab" * 32,
        vout=0,
        value_sats=200_000,
        script_pubkey=spk(0, 3),
        branch=0,
        index=3,
    )
    psbt, _meta = build_unsigned_psbt(
        [src],
        [(spk(0, 99), 60_000)],
        change_address(),
        139_718,
        account_key=account_key(),
        account_fingerprint=fingerprint(),
        account_path=ACCOUNT_PATH,
        change_index=7,  # the index change_address() derives
    )
    return psbt


def sign_psbt(psbt: PSBT) -> str:
    """embit-sign the PSBT with the test-vector key (tests only)."""
    privkey = bip32.HDKey.from_seed(SEED).derive(list(ACCOUNT_PATH) + [0, 3]).key
    digest = psbt.tx.sighash_segwit(0, script.Script(spk(0, 3)), 200_000)
    stream = BytesIO()
    ec.Signature.write_to(privkey.sign(digest), stream)
    psbt.inputs[0].partial_sigs[privkey.get_public_key()] = stream.getvalue() + b"\x01"
    return psbt.to_base64()


def make_signed_file(directory: Path, name: str, sidecar: bool = True) -> Path:
    """Write a signed PSBT file (with optional sidecar) directly to a folder."""
    signed_b64 = sign_psbt(build_unsigned())
    path = directory / name
    path.write_text(signed_b64 + "\n", encoding="utf-8")
    if sidecar:
        side = directory / (name + ".sha256")
        side.write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "\n", encoding="utf-8")
    return path


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    return tmp_path / "transfer"


@pytest.fixture
def signer(folder: Path) -> FilePsbtSigner:
    return FilePsbtSigner(folder)


class TestExport:
    def test_exports_unsigned_and_sidecar(self, signer: FilePsbtSigner, folder: Path):
        b64 = psbt_to_base64(build_unsigned())
        files = signer.export_unsigned(b64, "abc12345")
        assert isinstance(files, ExportedFiles)
        assert files.unsigned_path == folder / "localwallet-unsigned-abc12345.psbt.b64"
        assert (
            files.checksum_path
            == folder / "localwallet-unsigned-abc12345.psbt.b64.sha256"
        )
        written = files.unsigned_path.read_text(encoding="utf-8")
        assert written.strip() == b64
        digest = files.checksum_path.read_text(encoding="utf-8").strip()
        assert digest == hashlib.sha256(written.encode()).hexdigest()

    def test_exports_binary_sibling_matching_base64(self, signer, folder):
        import base64

        b64 = psbt_to_base64(build_unsigned())
        files = signer.export_unsigned(b64, "abc12345")
        assert files.binary_path == folder / "localwallet-unsigned-abc12345.psbt"
        assert files.binary_path.exists()
        # Raw bytes == base64-decoded .b64 text, byte-identical.
        assert files.binary_path.read_bytes() == base64.b64decode(b64)
        assert files.binary_path.read_bytes().startswith(b"psbt\xff")

    def test_binary_sibling_refused_on_different_content(self, signer, folder):
        signer.export_unsigned(psbt_to_base64(build_unsigned()), "abc12345")
        signed = sign_psbt(build_unsigned())  # different body
        assert signed != psbt_to_base64(build_unsigned())
        with pytest.raises(SignerError) as exc:
            signer.export_unsigned(signed, "abc12345")
        assert "different content" in str(exc.value)

    def test_creates_directory_if_missing(self, tmp_path: Path):
        target = tmp_path / "does" / "not" / "exist"
        assert not target.exists()
        s = FilePsbtSigner(target)
        assert target.is_dir()
        s.export_unsigned(psbt_to_base64(build_unsigned()), "abc12345")
        assert (target / "localwallet-unsigned-abc12345.psbt.b64").exists()

    def test_idempotent_same_content(self, signer: FilePsbtSigner):
        b64 = psbt_to_base64(build_unsigned())
        first = signer.export_unsigned(b64, "abc12345")
        second = signer.export_unsigned(b64, "abc12345")  # no raise
        assert second.unsigned_path == first.unsigned_path
        assert second.unsigned_path.read_text(encoding="utf-8").strip() == b64

    def test_sidecar_healed_on_idempotent_reexport(self, signer: FilePsbtSigner):
        # ADR-0014 "every export writes a sidecar": deleting the sidecar
        # (e.g. partial SD copy) is healed by an idempotent re-export.
        b64 = psbt_to_base64(build_unsigned())
        first = signer.export_unsigned(b64, "abc12345")
        assert first.checksum_path.exists()
        first.checksum_path.unlink()
        assert not first.checksum_path.exists()

        second = signer.export_unsigned(b64, "abc12345")  # no raise, rewrites
        assert second.checksum_path.exists()
        digest = second.checksum_path.read_text(encoding="utf-8").strip()
        assert digest == hashlib.sha256(first.unsigned_path.read_bytes()).hexdigest()

    def test_overwrite_different_content_refused(self, signer: FilePsbtSigner):
        signer.export_unsigned(psbt_to_base64(build_unsigned()), "abc12345")
        other = psbt_to_base64(build_unsigned())  # byte-identical for same build...
        # ...so force a genuinely different body by signing (adds sigs).
        signed = sign_psbt(build_unsigned())
        assert signed != other
        with pytest.raises(SignerError) as exc:
            signer.export_unsigned(signed, "abc12345")
        assert "different content" in str(exc.value)

    def test_empty_psbt_refused(self, signer: FilePsbtSigner):
        with pytest.raises(SignerError):
            signer.export_unsigned("   ", "abc12345")

    def test_signed_file_collision_refused(self, signer: FilePsbtSigner, folder: Path):
        # Pre-place a signed file for the same reference.
        make_signed_file(folder, "localwallet-signed-abc12345.psbt.b64", sidecar=False)
        with pytest.raises(SignerError) as exc:
            signer.export_unsigned(psbt_to_base64(build_unsigned()), "abc12345")
        assert "collide" in str(exc.value)


class TestFilenameSanitization:
    def test_hex_alnum_kept_and_truncated(self, signer: FilePsbtSigner, folder: Path):
        # 12-char alnum ref is truncated to 8.
        signer.export_unsigned(psbt_to_base64(build_unsigned()), "abcdef012345")
        assert (folder / "localwallet-unsigned-abcdef01.psbt.b64").exists()

    def test_hostile_ref_is_hashed(self, signer: FilePsbtSigner, folder: Path):
        # Slashes and path separators must be neutralized (hashed), never
        # injected into the filename.
        ref = "../../etc/passwd"
        signer.export_unsigned(psbt_to_base64(build_unsigned()), ref)
        digest = hashlib.sha256(ref.encode()).hexdigest()[:8]
        assert (folder / f"localwallet-unsigned-{digest}.psbt.b64").exists()
        # No directory traversal happened.
        assert not (folder / ".." / "localwallet-unsigned-").exists()
        assert not (folder.parent / "etc").exists()

    def test_empty_ref_is_hashed(self, signer: FilePsbtSigner, folder: Path):
        signer.export_unsigned(psbt_to_base64(build_unsigned()), "")
        digest = hashlib.sha256(b"").hexdigest()[:8]
        assert (folder / f"localwallet-unsigned-{digest}.psbt.b64").exists()

    def test_non_ascii_alnum_ref_is_hashed(self, signer: FilePsbtSigner, folder: Path):
        # A5: only ASCII [0-9A-Za-z] is used verbatim — Unicode isalnum
        # admits ÄÖÜ, but ADR-0014 says hex/alnum, so they must be hashed.
        ref = "äöü1234"
        assert all(c.isalnum() for c in ref)  # Unicode would pass isalnum...
        signer.export_unsigned(psbt_to_base64(build_unsigned()), ref)
        digest = hashlib.sha256(ref.encode("utf-8")).hexdigest()[:8]
        assert (folder / f"localwallet-unsigned-{digest}.psbt.b64").exists()
        assert not (folder / "localwallet-unsigned-äöü1234.psbt.b64").exists()


class TestImportRoundTrip:
    def test_round_trip_with_sidecar(self, signer: FilePsbtSigner, folder: Path):
        signer.export_unsigned(psbt_to_base64(build_unsigned()), "abc12345")
        signed_path = make_signed_file(folder, "localwallet-signed-abc12345.psbt.b64")
        result = signer.import_signed(signed_path, expected_tx_ref="abc12345")
        assert isinstance(result, SignedResult)
        assert result.signer_name == "file"
        assert result.checksum_verified is True
        # Normalized b64 re-parses and matches the signed tx.
        parsed = PSBT.from_base64(result.psbt_base64)
        assert len(parsed.inputs) == 1
        assert len(parsed.inputs[0].partial_sigs) == 1

    def test_round_trip_without_sidecar_allowed(self, signer: FilePsbtSigner, folder: Path):
        signed_path = make_signed_file(
            folder, "localwallet-signed-abc12345.psbt.b64", sidecar=False
        )
        result = signer.import_signed(signed_path, expected_tx_ref="abc12345")
        assert result.checksum_verified is False
        assert result.signer_name == "file"

    def test_no_expected_ref_ok(self, signer: FilePsbtSigner, folder: Path):
        signed_path = make_signed_file(folder, "localwallet-signed-abc12345.psbt.b64")
        result = signer.import_signed(signed_path)
        assert result.checksum_verified is True

    def test_reference_mismatch_refused(self, signer: FilePsbtSigner, folder: Path):
        signed_path = make_signed_file(folder, "localwallet-signed-abc12345.psbt.b64")
        with pytest.raises(SignerError) as exc:
            signer.import_signed(signed_path, expected_tx_ref="zzzz9999")
        assert "does not match the expected transaction" in str(exc.value)

    def test_import_of_exported_unsigned_refused(self, signer: FilePsbtSigner, folder: Path):
        # Importing the unsigned file itself must be refused (no sigs).
        signer.export_unsigned(psbt_to_base64(build_unsigned()), "abc12345")
        unsigned = folder / "localwallet-unsigned-abc12345.psbt.b64"
        # Copy to a signed-convention name but keep unsigned content.
        signed_path = folder / "localwallet-signed-abc12345.psbt.b64"
        signed_path.write_bytes(unsigned.read_bytes())
        with pytest.raises(SignerError) as exc:
            signer.import_signed(signed_path)
        assert "no signatures" in str(exc.value)


class TestImportRefusals:
    def test_wrong_filename_convention_refused(self, signer: FilePsbtSigner, folder: Path):
        signed_path = make_signed_file(folder, "unsigned-signed.psbt.b64")
        with pytest.raises(SignerError) as exc:
            signer.import_signed(signed_path)
        assert "localwallet-signed-*.psbt.b64" in str(exc.value)  # names the CONVENTION

    def test_garbage_file_refused(self, signer: FilePsbtSigner, folder: Path):
        path = folder / "localwallet-signed-abc12345.psbt.b64"
        path.write_text("this is not base64 at all !!!", encoding="utf-8")
        with pytest.raises(SignerError) as exc:
            signer.import_signed(path)
        assert "not valid base64" in str(exc.value)

    def test_non_psbt_base64_refused(self, signer: FilePsbtSigner, folder: Path):
        path = folder / "localwallet-signed-abc12345.psbt.b64"
        path.write_text("aGVsbG8gd29ybGQ=", encoding="utf-8")  # "hello world"
        with pytest.raises(SignerError) as exc:
            signer.import_signed(path)
        assert "not a PSBT" in str(exc.value)

    def test_empty_file_refused(self, signer: FilePsbtSigner, folder: Path):
        path = folder / "localwallet-signed-abc12345.psbt.b64"
        path.write_text("", encoding="utf-8")
        with pytest.raises(SignerError):
            signer.import_signed(path)

    def test_checksum_mismatch_refused(self, signer: FilePsbtSigner, folder: Path):
        path = make_signed_file(folder, "localwallet-signed-abc12345.psbt.b64")
        side = folder / "localwallet-signed-abc12345.psbt.b64.sha256"
        side.write_text("0" * 64 + "\n", encoding="utf-8")  # wrong digest
        with pytest.raises(SignerError) as exc:
            signer.import_signed(path)
        assert "checksum mismatch" in str(exc.value)

    def test_corrupted_file_with_sidecar_refused(self, signer: FilePsbtSigner, folder: Path):
        # Sidecar matches nothing after a bit flip → fail closed.
        path = make_signed_file(folder, "localwallet-signed-abc12345.psbt.b64")
        data = bytearray(path.read_bytes())
        data[0] ^= 0xFF
        path.write_bytes(bytes(data))
        with pytest.raises(SignerError):
            signer.import_signed(path)

    def test_truncated_base64_torn_write_refused(self, signer: FilePsbtSigner, folder: Path):
        # Sidecar-less torn write (e.g. mid-copy SD removal): the text is cut
        # mid-base64-string and must be refused, never silently accepted.
        path = make_signed_file(
            folder, "localwallet-signed-abc12345.psbt.b64", sidecar=False
        )
        text = path.read_text(encoding="utf-8").strip()
        torn = text[: len(text) // 2]
        assert torn  # genuinely truncated, not empty
        path.write_text(torn + "\n", encoding="utf-8")
        with pytest.raises(SignerError) as exc:
            signer.import_signed(path)
        assert "not valid base64" in str(exc.value)

    def test_empty_final_fields_unsigned_refused(self, signer: FilePsbtSigner, folder: Path):
        # A1: an input whose final fields are PRESENT but EMPTY carries no
        # signature and must NOT count as signed (truthiness, not is-not-None).
        psbt = build_unsigned()
        scope = psbt.inputs[0]
        scope.final_scriptsig = script.Script(b"")  # empty, present
        scope.final_scriptwitness = Witness([])  # empty, present
        path = folder / "localwallet-signed-abc12345.psbt.b64"
        path.write_text(psbt.to_base64() + "\n", encoding="utf-8")
        with pytest.raises(SignerError) as exc:
            signer.import_signed(path)
        assert "no signatures" in str(exc.value)


class TestValueFreeErrors:
    def test_errors_never_echo_content(self, signer: FilePsbtSigner, folder: Path):
        # Exercise refusal paths and assert messages contain no PSBT base64,
        # no hex digests beyond expected phrases, and no folder path.
        b64 = psbt_to_base64(build_unsigned())
        signed = sign_psbt(build_unsigned())

        cases = []

        # overwrite-different
        signer.export_unsigned(b64, "abc12345")
        try:
            signer.export_unsigned(signed, "abc12345")
        except SignerError as e:
            cases.append(str(e))

        # wrong convention
        p = folder / "wrong.psbt.b64"
        p.write_text("AAAA", encoding="utf-8")
        try:
            signer.import_signed(p)
        except SignerError as e:
            cases.append(str(e))

        # garbage base64
        g = folder / "localwallet-signed-abc12345.psbt.b64"
        g.write_text("###not base64###", encoding="utf-8")
        try:
            signer.import_signed(g)
        except SignerError as e:
            cases.append(str(e))

        assert cases, "expected refusal messages"
        for msg in cases:
            # Value-free: no PSBT body, no hex digest strings of content,
            # and the absolute folder path never leaks.
            assert b64 not in msg
            assert str(folder) not in msg
            assert "000102030405060708090a0b0c0d0e0f" not in msg

    def test_export_error_is_value_free(self, signer: FilePsbtSigner):
        with pytest.raises(SignerError) as exc:
            signer.export_unsigned("", "abc12345")
        assert "transfer" not in str(exc.value)


class TestListPendingExports:
    def test_lists_unsigned_only(self, signer: FilePsbtSigner, folder: Path):
        signer.export_unsigned(psbt_to_base64(build_unsigned()), "abc12345")
        signer.export_unsigned(psbt_to_base64(build_unsigned()), "def45678")
        # A signed file and a foreign file must not appear.
        make_signed_file(folder, "localwallet-signed-abc12345.psbt.b64")
        (folder / "unrelated.txt").write_text("x", encoding="utf-8")

        pending = signer.list_pending_exports()
        assert len(pending) == 2
        names = [p.name for p in pending]
        assert "localwallet-unsigned-abc12345.psbt.b64" in names
        assert "localwallet-unsigned-def45678.psbt.b64" in names
        assert not any(n.startswith("localwallet-signed-") for n in names)

    def test_empty_when_nothing_exported(self, signer: FilePsbtSigner):
        assert signer.list_pending_exports() == []

    def test_sorted_deterministic(self, signer: FilePsbtSigner, folder: Path):
        signer.export_unsigned(psbt_to_base64(build_unsigned()), "zzz99999")
        signer.export_unsigned(psbt_to_base64(build_unsigned()), "aaa11111")
        names = [p.name for p in signer.list_pending_exports()]
        assert names == sorted(names)


class TestContainerSizeCeiling:
    """Rider R5: the transfer-folder gateway bounds payload size BEFORE
    decode/parse, so downstream re-validation only ever sees bounded data."""

    def test_export_over_ceiling_refused_before_decode(self, signer: FilePsbtSigner):
        from localwallet.signer.file import _MAX_PSBT_TEXT_CHARS

        oversized = "cHNj" * (_MAX_PSBT_TEXT_CHARS // 4 + 1)  # > ceiling
        with pytest.raises(SignerError) as exc:
            signer.export_unsigned(oversized, "abc12345")
        assert "maximum container size" in str(exc.value)
        # Nothing was written toward the device.
        assert signer.list_pending_exports() == []

    def test_import_over_ceiling_refused_before_decode(self, signer: FilePsbtSigner, folder: Path):
        from localwallet.signer.file import _MAX_PSBT_TEXT_CHARS

        path = folder / "localwallet-signed-abc12345.psbt.b64"
        path.write_text("A" * (_MAX_PSBT_TEXT_CHARS + 1) + "\n", encoding="utf-8")
        with pytest.raises(SignerError) as exc:
            signer.import_signed(path)
        assert "maximum container size" in str(exc.value)

    def test_import_giant_file_refused_at_stat_before_any_read(
        self, signer: FilePsbtSigner, folder: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """TCK-SEC-004 change 3: the size gate fires at ``stat()`` BEFORE any
        byte is read, so a multi-GB planted file is refused cleanly without
        ever being read into memory. The file is created SPARSE (seek/
        truncate — no GBs allocated); ``read_bytes`` is sabotaged to prove
        the stat-gate path is hit and no read happens."""
        from localwallet.signer import file as file_module

        path = folder / "localwallet-signed-abc12345.psbt.b64"
        with open(path, "wb") as fh:
            fh.seek(file_module._MAX_PSBT_FILE_BYTES)
            fh.write(b"\0")  # sparse: 1 byte on disk, size over the gate
        assert path.stat().st_size == file_module._MAX_PSBT_FILE_BYTES + 1

        def _no_read(*args: object, **kwargs: object) -> bytes:
            msg = "read_bytes must never run for an oversized file"
            raise AssertionError(msg)

        monkeypatch.setattr(Path, "read_bytes", _no_read)
        with pytest.raises(SignerError) as exc:
            signer.import_signed(path)
        assert "maximum container size" in str(exc.value)

    def test_at_ceiling_passes_the_size_gate(self, signer: FilePsbtSigner):
        """Boundary: a payload of EXACTLY the ceiling gets past the size
        check (it may fail later validation — the size gate itself passed)."""
        from localwallet.signer.file import _MAX_PSBT_TEXT_CHARS

        at_limit = "cHNj" * (_MAX_PSBT_TEXT_CHARS // 4)  # exactly the ceiling
        with pytest.raises(SignerError) as exc:
            signer.export_unsigned(at_limit, "abc12345")
        assert "maximum container size" not in str(exc.value)


class TestSignedImportPath:
    """The deterministic ADR-0014 signed-filename derivation (TCK-P3-005)."""

    def test_path_matches_import_convention(self, signer: FilePsbtSigner, folder: Path):
        path = signer.signed_import_path("abcdef012345")
        assert path == folder / "localwallet-signed-abcdef01.psbt.b64"
        # A signed file placed at the derived path imports with the ref.
        make_signed_file(folder, path.name)
        result = signer.import_signed(path, expected_tx_ref="abcdef012345")
        assert result.signer_name == "file"

    def test_hostile_ref_is_sanitized(self, signer: FilePsbtSigner, folder: Path):
        path = signer.signed_import_path("../../etc/passwd")
        digest = hashlib.sha256(b"../../etc/passwd").hexdigest()[:8]
        assert path == folder / f"localwallet-signed-{digest}.psbt.b64"
        assert ".." not in path.name


class TestBoundedReads:
    """TCK-SEC-007: every signer file read is a MAXIMUM-READ with a byte cap
    — a planted oversized file (including via a stat-then-swap TOCTOU) is
    refused deterministically, never buffered, and the refusal is value-free."""

    def test_oversized_checksum_sidecar_refused_on_import(
        self, signer: FilePsbtSigner, folder: Path
    ):
        from localwallet.signer import file as file_module

        signed_path = make_signed_file(
            folder, "localwallet-signed-abc12345.psbt.b64", sidecar=False
        )
        sidecar = folder / ("localwallet-signed-abc12345.psbt.b64.sha256")
        sidecar.write_bytes(b"0" * (file_module._MAX_CHECKSUM_FILE_BYTES + 1))
        with pytest.raises(SignerError) as exc:
            signer.import_signed(signed_path)
        # Same error class/pattern as the existing size gate, value-free.
        assert "checksum sidecar exceeds the maximum size" in str(exc.value)
        assert str(folder) not in str(exc.value)
        assert not str(signed_path) in str(exc.value)

    def test_sidecar_at_cap_boundary_still_verified(
        self, signer: FilePsbtSigner, folder: Path
    ):
        # 65 valid bytes (hex digest + newline) sit far below the cap and
        # must keep verifying — the cap only refuses OVER the limit.
        signed_path = make_signed_file(folder, "localwallet-signed-abc12345.psbt.b64")
        result = signer.import_signed(signed_path)
        assert result.checksum_verified is True

    def test_oversized_existing_unsigned_refused_on_export(
        self, signer: FilePsbtSigner, folder: Path
    ):
        from localwallet.signer import file as file_module

        b64 = psbt_to_base64(build_unsigned())
        files = signer.export_unsigned(b64, "abc12345")
        # Plant an oversized (sparse) file at the unsigned path; the
        # idempotent re-export must refuse on the bounded read.
        with open(files.unsigned_path, "wb") as fh:
            fh.seek(file_module._MAX_PSBT_FILE_BYTES)
            fh.write(b"\0")
        with pytest.raises(SignerError) as exc:
            signer.export_unsigned(b64, "abc12345")
        assert "existing unsigned file exceeds the maximum container size" in str(
            exc.value
        )
        assert str(folder) not in str(exc.value)

    def test_signed_read_cap_survives_stat_swap_toctou(
        self, signer: FilePsbtSigner, folder: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """TOCTOU: a file that LIES at stat() time (small st_size) but is
        oversized on disk must still be refused by the read-time cap — the
        cap is enforced on the bytes actually read, not only on the stat."""
        from localwallet.signer import file as file_module

        path = folder / "localwallet-signed-abc12345.psbt.b64"
        with open(path, "wb") as fh:
            fh.seek(file_module._MAX_PSBT_FILE_BYTES + 1)  # over the read cap
            fh.write(b"\0")  # sparse
        assert path.stat().st_size == file_module._MAX_PSBT_FILE_BYTES + 2

        real_stat = Path.stat

        def lying_stat(self: Path, *args: object, **kwargs: object):
            if self == path:
                return type("FakeStat", (), {"st_size": 1})()  # claims tiny
            return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "stat", lying_stat)
        with pytest.raises(SignerError) as exc:
            signer.import_signed(path)
        assert "signed file exceeds the maximum container size" in str(exc.value)
        assert str(folder) not in str(exc.value)

    def test_signed_file_exactly_at_read_cap_passes_the_read(
        self, signer: FilePsbtSigner, folder: Path
    ):
        """Boundary: exactly ``_MAX_PSBT_FILE_BYTES`` bytes pass the bounded
        read (the file then fails later validation — never the read cap)."""
        from localwallet.signer import file as file_module

        path = folder / "localwallet-signed-abc12345.psbt.b64"
        path.write_bytes(b"A" * file_module._MAX_PSBT_FILE_BYTES)
        with pytest.raises(SignerError) as exc:
            signer.import_signed(path)
        # The read itself succeeded; refusal comes from a later check.
        assert "could not read" not in str(exc.value)

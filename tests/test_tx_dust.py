"""Dust/min-relay tests (TCK-P2-002, R14).

Layers:

1. **Canonical constants** — the formula path in
   :mod:`localwallet.tx.dust` contains no hardcoded dust numbers; these
   tests pin that the from-first-principles arithmetic reproduces Bitcoin
   Core's canonical thresholds at the default dust relay rate of 3 sat/vB
   (Core v28.0 ``src/policy/policy.cpp``): P2PKH 546, P2SH (incl.
   P2SH-P2WPKH outputs) 540, P2WPKH 294, P2WSH/P2TR 330, OP_RETURN 0.
2. **Property tests** (deterministic, seeded — no hypothesis dependency):
   linearity in the rate, monotonicity in script size, and a fuzz sweep
   over script sizes 20..80 checked against the independent arithmetic.
3. **Boundary/structural** — CompactSize varint growth, witness-program
   detection edges, unspendable scripts, argument validation (fail
   closed, value-free), min-relay floor bounds.
"""

import random

import pytest

from localwallet.tx.dust import (
    dust_threshold,
    min_relay_fee_vbytes,
    script_is_witness_program,
    varint_size,
)

DUST_RATE_DEFAULT = 3  # Core DUST_RELAY_TX_FEE = 3000 sat/kvB = 3 sat/vB


def p2wpkh() -> bytes:
    return b"\x00\x14" + b"\x22" * 20


def p2sh() -> bytes:
    return b"\xa9\x14" + b"\x22" * 20 + b"\x87"


def p2pkh() -> bytes:
    return b"\x76\xa9\x14" + b"\x22" * 20 + b"\x88\xac"


def p2wsh() -> bytes:
    return b"\x00\x20" + b"\x22" * 32


def p2tr() -> bytes:
    return b"\x51\x20" + b"\x22" * 32


def op_return() -> bytes:
    return b"\x6a" + b"\x04abcd"


class TestCanonicalConstants:
    """Core v28.0 policy.cpp thresholds at the default rate (see docstring)."""

    @pytest.mark.parametrize(
        ("script", "expected"),
        [
            (p2pkh(), 546),  # 3 * (34 + 148)
            (p2sh(), 540),  # 3 * (32 + 148) — P2SH-P2WPKH outputs are P2SH
            (p2wpkh(), 294),  # 3 * (31 + 67)
            (p2wsh(), 330),  # 3 * (43 + 67)
            (p2tr(), 330),  # 3 * (43 + 67) — witness-program cost, PR #22779
            (op_return(), 0),  # unspendable
            (b"", 3 * (8 + 1 + 0 + 148)),  # empty script: legacy branch, 471
        ],
    )
    def test_canonical_thresholds_at_core_defaults(self, script, expected):
        assert dust_threshold(script, DUST_RATE_DEFAULT) == expected

    def test_thresholds_equal_rate_times_base_size(self):
        """Independent arithmetic: rate * (serialized output + spend cost)."""
        for script, cost in ((p2pkh(), 148), (p2sh(), 148), (p2wpkh(), 67), (p2tr(), 67)):
            serialized = 8 + 1 + len(script)
            assert dust_threshold(script, 3) == 3 * (serialized + cost)

    def test_witness_branch_applies_to_every_witness_program(self):
        # v0 and v1 programs, 20- and 32-byte programs: same +67 cost.
        for script in (p2wpkh(), p2wsh(), p2tr()):
            serialized = 8 + 1 + len(script)
            assert dust_threshold(script, 3) == 3 * (serialized + 67)


class TestRateProperties:
    """R14 property: thresholds scale linearly with the dust relay rate."""

    @pytest.mark.parametrize("rate", [1, 2, 3, 4, 5, 7, 10, 25, 100])
    @pytest.mark.parametrize("script", [p2pkh(), p2sh(), p2wpkh(), p2wsh(), p2tr()])
    def test_linear_in_rate(self, script, rate):
        base = dust_threshold(script, 1)
        assert base > 0
        assert dust_threshold(script, rate) == rate * base

    def test_zero_rate_means_no_dust(self):
        assert dust_threshold(p2wpkh(), 0) == 0
        assert dust_threshold(p2pkh(), 0) == 0


class TestSizeProperties:
    """R14 property: monotone in script size within a spend-cost class."""

    def test_monotone_in_script_size_legacy(self):
        previous = -1
        for size in range(20, 81):
            script = b"\x76\xa9" + bytes(size)  # not a witness program
            threshold = dust_threshold(script, 3)
            assert threshold >= previous
            previous = threshold

    def test_monotone_in_script_size_witness(self):
        previous = -1
        for size in range(20, 41):  # witness programs: 4..42 bytes total
            script = b"\x00" + bytes([size - 2]) + b"\x33" * (size - 2)
            assert script_is_witness_program(script)
            threshold = dust_threshold(script, 3)
            assert threshold >= previous
            previous = threshold

    def test_fuzz_script_sizes_20_to_80(self):
        """Seeded fuzz: threshold always equals the independent arithmetic."""
        rng = random.Random(20260901)  # fixed seed: deterministic property run
        for _ in range(500):
            size = rng.randint(20, 80)
            body = bytes(rng.randrange(256) for _ in range(size))
            legacy = b"\x76\xa9" + body[: size - 2] if size >= 2 else body
            rate = rng.randint(0, 25)
            expected_legacy = rate * (8 + varint_size(len(legacy)) + len(legacy) + 148)
            assert dust_threshold(legacy, rate) == expected_legacy
            if 4 <= size <= 42:
                program = b"\x00" + bytes([size - 2]) + body[: size - 2]
                expected_witness = rate * (
                    8 + varint_size(len(program)) + len(program) + 67
                )
                assert dust_threshold(program, rate) == expected_witness


class TestVarintBoundaries:
    """CompactSize growth must flow through the threshold arithmetic."""

    def test_varint_sizes(self):
        assert varint_size(0) == 1
        assert varint_size(252) == 1
        assert varint_size(253) == 3
        assert varint_size(65535) == 3
        assert varint_size(65536) == 5

    def test_threshold_jumps_at_varint_boundary(self):
        # Legacy scripts of length 252 (1-byte varint) vs 253 (3-byte varint).
        s252 = b"\x51" * 252
        s253 = b"\x51" * 253
        assert varint_size(252) == 1 and varint_size(253) == 3
        assert dust_threshold(s252, 3) == 3 * (8 + 1 + 252 + 148)
        assert dust_threshold(s253, 3) == 3 * (8 + 3 + 253 + 148)


class TestWitnessProgramDetection:
    def test_versions_and_lengths(self):
        for op in (0x00, 0x51, 0x52, 0x60):
            for program_len in (2, 20, 32, 40):
                script = bytes([op, program_len]) + b"\x44" * program_len
                assert script_is_witness_program(script)
        # wrong length byte
        assert not script_is_witness_program(b"\x00\x14" + b"\x44" * 19)
        # version opcode out of range (0x61 = OP_17)
        assert not script_is_witness_program(b"\x61\x14" + b"\x44" * 20)
        # too short / too long
        assert not script_is_witness_program(b"\x00\x01\x44")
        assert not script_is_witness_program(b"\x00\x29" + b"\x44" * 41)


class TestUnspendable:
    def test_op_return_is_zero(self):
        assert dust_threshold(op_return(), 3) == 0
        assert dust_threshold(op_return(), 100) == 0

    def test_oversized_script_is_unspendable(self):
        assert dust_threshold(b"\x51" * 10_001, 3) == 0
        assert dust_threshold(b"\x51" * 10_000, 3) == 3 * (8 + 3 + 10_000 + 148)


class TestValidationFailClosed:
    @pytest.mark.parametrize("rate", [-1, 10_001])
    def test_bad_rate_value_refused(self, rate):
        with pytest.raises(ValueError):
            dust_threshold(p2wpkh(), rate)

    @pytest.mark.parametrize("rate", [True, False, 1.5, "3", None])
    def test_bad_rate_type_refused(self, rate):
        with pytest.raises(TypeError):
            dust_threshold(p2wpkh(), rate)

    @pytest.mark.parametrize("script", ["0014", None, 22, [0, 20]])
    def test_bad_script_refused(self, script):
        with pytest.raises(ValueError):
            dust_threshold(script, 3)

    def test_error_messages_are_value_free(self):
        with pytest.raises(ValueError) as exc:
            dust_threshold("not-bytes", 3)
        assert "0014" not in str(exc.value)
        assert "bc1q" not in str(exc.value)


class TestMinRelayFloor:
    def test_default_is_core_minrelaytxfee(self):
        # Core DEFAULT_MIN_RELAY_TX_FEE = 1000 sat/kvB = 1 sat/vB.
        assert min_relay_fee_vbytes(141) == 141
        assert min_relay_fee_vbytes(110, 1) == 110

    def test_floor_is_at_least_one_sat(self):
        assert min_relay_fee_vbytes(0) == 1
        assert min_relay_fee_vbytes(0, 0) == 1

    def test_scales_with_rate(self):
        assert min_relay_fee_vbytes(141, 5) == 705

    @pytest.mark.parametrize("vsize", [-1, 100_001])
    def test_bad_vsize_value_refused(self, vsize):
        with pytest.raises(ValueError):
            min_relay_fee_vbytes(vsize)

    @pytest.mark.parametrize("vsize", [True, False, 1.5, "10"])
    def test_bad_vsize_type_refused(self, vsize):
        with pytest.raises(TypeError):
            min_relay_fee_vbytes(vsize)

    @pytest.mark.parametrize("rate", [-1, 10_001])
    def test_bad_rate_value_refused(self, rate):
        with pytest.raises(ValueError):
            min_relay_fee_vbytes(100, rate)

    @pytest.mark.parametrize("rate", [True, False, 1.5])
    def test_bad_rate_type_refused(self, rate):
        with pytest.raises(TypeError):
            min_relay_fee_vbytes(100, rate)

    def test_standardness_bound_is_core_max_weight(self):
        # 100_000 vB == MAX_STANDARD_TX_WEIGHT (400_000 WU) / 4 is accepted.
        assert min_relay_fee_vbytes(100_000) == 100_000

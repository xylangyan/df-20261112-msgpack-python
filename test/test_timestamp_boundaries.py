"""Characterization tests for MessagePack timestamp conversion boundaries.

The wire payloads below are assembled with ``struct`` from the MessagePack
timestamp specification instead of using ``Timestamp.to_bytes`` or
``packb``.  The encoder is the code under test, so it must not also provide
the expected binary representation.  No test reads the system clock or uses
the network.
"""

import datetime
import struct
from fractions import Fraction

import pytest

import msgpack
from msgpack.ext import Timestamp

UTC = datetime.timezone.utc
NAIVE = datetime.datetime(1970, 1, 1, 0, 0, 42, 14)


def timestamp32(seconds):
    """Build a 32-bit timestamp ext message directly from its wire fields."""
    return b"\xd6\xff" + struct.pack("!L", seconds)


def timestamp64(seconds, nanoseconds):
    """Build a 64-bit timestamp ext message directly from its wire fields."""
    assert 0 <= seconds < 2**34
    assert 0 <= nanoseconds < 10**9
    return b"\xd7\xff" + struct.pack("!Q", (nanoseconds << 34) | seconds)


def timestamp96(seconds, nanoseconds):
    """Build a 96-bit timestamp ext message directly from its wire fields."""
    assert -(2**63) <= seconds < 2**63
    assert 0 <= nanoseconds < 10**9
    return b"\xc7\x0c\xff" + struct.pack("!Iq", nanoseconds, seconds)


DATETIME_MIN = datetime.datetime(1, 1, 1, tzinfo=UTC)
DATETIME_MAX = datetime.datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)

# (label, raw unix nanoseconds, seconds, nanoseconds, independent wire bytes)
WIRE_SAMPLES = [
    (
        "timestamp32 upper bound",
        4_294_967_295_000_000_000,
        2**32 - 1,
        0,
        bytes.fromhex("d6ffffffffff"),
    ),
    (
        "timestamp32/64 boundary",
        4_294_967_296_000_000_000,
        2**32,
        0,
        bytes.fromhex("d7ff0000000100000000"),
    ),
    (
        "timestamp64 upper bound",
        17_179_869_183_999_999_999,
        2**34 - 1,
        999_999_999,
        bytes.fromhex("d7ffee6b27ffffffffff"),
    ),
    (
        "timestamp64/96 boundary",
        17_179_869_184_000_000_000,
        2**34,
        0,
        bytes.fromhex("c70cff000000000000000400000000"),
    ),
    (
        "one nanosecond before epoch",
        -1,
        -1,
        999_999_999,
        bytes.fromhex("c70cff3b9ac9ffffffffffffffffff"),
    ),
    (
        "datetime.min",
        -62_135_596_800_000_000_000,
        -62_135_596_800,
        0,
        bytes.fromhex("c70cff00000000fffffff1886e0900"),
    ),
    (
        "datetime.max",
        253_402_300_799_999_999_000,
        253_402_300_799,
        999_999_000,
        bytes.fromhex("c70cff3b9ac6180000003afff4417f"),
    ),
    (
        "one second after datetime.max",
        253_402_300_800_000_000_000,
        253_402_300_800,
        0,
        bytes.fromhex("c70cff000000000000003afff44180"),
    ),
    (
        "one second before datetime.min",
        -62_135_596_801_000_000_000,
        -62_135_596_801,
        0,
        bytes.fromhex("c70cff00000000fffffff1886e08ff"),
    ),
    (
        "int64 upper bound",
        9_223_372_036_854_775_807_999_999_999,
        2**63 - 1,
        999_999_999,
        bytes.fromhex("c70cff3b9ac9ff7fffffffffffffff"),
    ),
    (
        "int64 lower bound",
        -9_223_372_036_854_775_808_000_000_000,
        -(2**63),
        0,
        bytes.fromhex("c70cff000000008000000000000000"),
    ),
    (
        "datetime.max plus 999 ns",
        253_402_300_799_999_999_999,
        253_402_300_799,
        999_999_999,
        bytes.fromhex("c70cff3b9ac9ff0000003afff4417f"),
    ),
    (
        "datetime.min plus 1 ns",
        -62_135_596_799_999_999_999,
        -62_135_596_800,
        1,
        bytes.fromhex("c70cff00000001fffffff1886e0900"),
    ),
    (
        "float precision loss",
        9_007_199_254_741_123_456_789,
        9_007_199_254_741,
        123_456_789,
        bytes.fromhex("c70cff075bcd150000083126e978d5"),
    ),
]


def test_wire_samples_are_self_consistent():
    # The independent struct builders must reproduce the hard-coded bytes.
    for label, raw_ns, seconds, nanoseconds, packed in WIRE_SAMPLES:
        if len(packed) == 6:
            assert timestamp32(seconds) == packed, label
        elif len(packed) == 10:
            assert timestamp64(seconds, nanoseconds) == packed, label
        else:
            assert timestamp96(seconds, nanoseconds) == packed, label
        assert seconds * 10**9 + nanoseconds == raw_ns, label


def test_unpack_timestamp_mode_0_is_lossless():
    for label, raw_ns, seconds, nanoseconds, packed in WIRE_SAMPLES:
        unpacked = msgpack.unpackb(packed, timestamp=0)
        assert unpacked == Timestamp(seconds, nanoseconds), label
        assert unpacked.to_unix_nano() == raw_ns, label


def test_unpack_timestamp_mode_2_is_lossless():
    for label, raw_ns, seconds, nanoseconds, packed in WIRE_SAMPLES:
        assert msgpack.unpackb(packed, timestamp=2) == raw_ns, label


def test_unpack_timestamp_mode_1_float():
    # Mode 1 converts each field independently: ns/1e9 is rounded first, then
    # added to seconds.  The ideal value is computed with exact rational
    # arithmetic from the raw nanoseconds; the two differ at the float stage.
    for label, raw_ns, seconds, nanoseconds, packed in WIRE_SAMPLES:
        staged = seconds + nanoseconds / 1e9
        ideal = float(Fraction(raw_ns, 10**9))
        unpacked = msgpack.unpackb(packed, timestamp=1)
        assert unpacked == staged, label
        if label == "one nanosecond before epoch":
            assert unpacked != ideal  # ideal -1e-9; staged rounds differently
            assert int(unpacked * 1e9) == 0  # raw value is -1
        else:
            assert unpacked == ideal, label


def test_unpack_timestamp_mode_3_datetime():
    # datetime carries microseconds: the nanoseconds -> microseconds stage
    # truncates (floor), and out-of-range seconds raise OverflowError.
    expected = {
        "timestamp32 upper bound": datetime.datetime(2106, 2, 7, 6, 28, 15, tzinfo=UTC),
        "timestamp32/64 boundary": datetime.datetime(2106, 2, 7, 6, 28, 16, tzinfo=UTC),
        "timestamp64 upper bound": datetime.datetime(
            2514, 5, 30, 1, 53, 3, 999999, tzinfo=UTC
        ),
        "timestamp64/96 boundary": datetime.datetime(2514, 5, 30, 1, 53, 4, tzinfo=UTC),
        "one nanosecond before epoch": datetime.datetime(
            1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC
        ),
        "datetime.min": DATETIME_MIN,
        "datetime.max": DATETIME_MAX,
        "one second after datetime.max": OverflowError,
        "one second before datetime.min": OverflowError,
        "int64 upper bound": OverflowError,
        "int64 lower bound": OverflowError,
        "datetime.max plus 999 ns": DATETIME_MAX,
        "datetime.min plus 1 ns": DATETIME_MIN,
        "float precision loss": OverflowError,
    }
    for label, raw_ns, seconds, nanoseconds, packed in WIRE_SAMPLES:
        want = expected[label]
        if want is OverflowError:
            with pytest.raises(OverflowError):
                msgpack.unpackb(packed, timestamp=3)
        else:
            assert msgpack.unpackb(packed, timestamp=3) == want, label


def test_invalid_nanosecond_field_rejected_before_mode_conversion():
    invalid = [
        ("timestamp64", bytes.fromhex("d7ffffffffffffffffff")),
        ("timestamp96", bytes.fromhex("c70cffffffffffffffffffffffffff")),
    ]
    for label, packed in invalid:
        for mode in range(4):
            with pytest.raises(ValueError):
                msgpack.unpackb(packed, timestamp=mode)
            # Timestamp.from_bytes itself rejects the field too.
            with pytest.raises(ValueError):
                Timestamp.from_bytes(packed[3:] if label == "timestamp96" else packed[2:])


def test_unsupported_timestamp_payload_length():
    # fixext 1/2 and ext8 length 0/3/5 are not timestamp widths.
    for packed in (
        bytes.fromhex("d4ff00"),
        bytes.fromhex("d5ff0000"),
        bytes.fromhex("c700ff"),
        bytes.fromhex("c703ff000000"),
        bytes.fromhex("c705ff0000000000"),
    ):
        with pytest.raises(ValueError):
            msgpack.unpackb(packed)


def test_encoding_width_boundaries_are_spec_shapes():
    # Independent expected bytes; the encoder is never used as the oracle.
    expected = [
        (Timestamp(0, 0), bytes.fromhex("d6ff00000000")),
        (Timestamp(2**32 - 1, 0), bytes.fromhex("d6ffffffffff")),
        (Timestamp(2**32, 0), bytes.fromhex("d7ff0000000100000000")),
        (Timestamp(0, 1), bytes.fromhex("d7ff0000000400000000")),
        (Timestamp(2**34 - 1, 999_999_999), bytes.fromhex("d7ffee6b27ffffffffff")),
        (Timestamp(2**34, 0), bytes.fromhex("c70cff000000000000000400000000")),
        (Timestamp(-1, 0), bytes.fromhex("c70cff00000000ffffffffffffffff")),
        (Timestamp(-1, 999_999_999), bytes.fromhex("c70cff3b9ac9ffffffffffffffffff")),
        (Timestamp(2**63 - 1, 999_999_999), bytes.fromhex("c70cff3b9ac9ff7fffffffffffffff")),
        (Timestamp(-(2**63), 0), bytes.fromhex("c70cff000000008000000000000000")),
    ]
    for timestamp, packed in expected:
        assert msgpack.packb(timestamp) == packed
        assert len(packed) in (6, 10, 15)
        # Every width still round-trips as a lossless Timestamp.
        assert msgpack.unpackb(packed, timestamp=0) == timestamp


def test_datetime_width_boundary_round_trips():
    # datetime spans use timestamp96 because their seconds exceed 34 bits or
    # are negative; these are independently expected payloads.
    cases = [
        (DATETIME_MIN, bytes.fromhex("c70cff00000000fffffff1886e0900")),
        (DATETIME_MAX, bytes.fromhex("c70cff3b9ac6180000003afff4417f")),
    ]
    for dt, packed in cases:
        assert msgpack.packb(dt, datetime=True) == packed
        assert msgpack.unpackb(packed, timestamp=3) == dt


def test_aware_datetime_inputs_convert_to_utc_and_keep_microseconds():
    wall = datetime.datetime(2024, 4, 16, 8, 43, 9, 420317)
    plus_8 = wall.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
    minus_530 = wall.replace(tzinfo=datetime.timezone(-datetime.timedelta(hours=5, minutes=30)))
    utc = wall.replace(tzinfo=UTC)

    # Same wall clock, different offsets: independent expected wire bytes.
    expected = {
        utc: bytes.fromhex("d7ff64361d20661e3a1d"),
        plus_8: bytes.fromhex("d7ff64361d20661dc99d"),
        minus_530: bytes.fromhex("d7ff64361d20661e8775"),
    }
    for dt, packed in expected.items():
        assert msgpack.packb(dt, datetime=True) == packed
        # Unpacking mode 3 always returns UTC; offset is stripped on encode.
        unpacked = msgpack.unpackb(packed, timestamp=3)
        assert unpacked == dt.astimezone(UTC)
        assert unpacked.tzinfo == UTC


def test_pre_epoch_fractional_datetime_uses_negative_seconds_and_positive_ns():
    dt = datetime.datetime(1969, 12, 31, 23, 59, 59, 500000, tzinfo=UTC)
    packed = bytes.fromhex("c70cff1dcd6500ffffffffffffffff")
    assert msgpack.packb(dt, datetime=True) == packed
    assert msgpack.unpackb(packed, timestamp=0) == Timestamp(-1, 500_000_000)
    assert msgpack.unpackb(packed, timestamp=2) == -500_000_000
    assert msgpack.unpackb(packed, timestamp=3) == dt


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError, match="where tzinfo=None"):
        msgpack.packb(NAIVE, datetime=True)
    # default() still gets a chance before the explicit error.
    assert msgpack.packb(NAIVE, datetime=True, default=lambda value: None) == msgpack.packb(None)


def test_nanosecond_to_microsecond_stage_truncates_toward_floor():
    # microseconds are floor(nanoseconds / 1000); never rounded half up.
    assert Timestamp(-1, 999_999_998).to_datetime() == datetime.datetime(
        1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC
    )
    # One sub-microsecond timestamp inside datetime.max is representable in
    # Timestamp and int modes but loses the tail in datetime mode.
    packed = timestamp96(253_402_300_799, 999_999_999)
    assert Timestamp.from_bytes(packed[3:]) == Timestamp(253_402_300_799, 999_999_999)
    assert msgpack.unpackb(packed, timestamp=3) == DATETIME_MAX


def test_float_stage_does_not_round_microsecond_exact_values():
    # Values that are exact in microseconds survive float mode exactly here.
    packed = timestamp96(253_402_300_799, 999_999_000)
    assert msgpack.unpackb(packed, timestamp=1) == 253_402_300_800.0
    assert msgpack.unpackb(packed, timestamp=2) == 253_402_300_799_999_999_000


def test_float_stage_rounds_large_nanosecond_remainder():
    # At ~9e12 seconds the float 53-bit significand cannot hold nanoseconds.
    # The int mode remains exact; float mode loses ~385k ns on this value.
    seconds = 9_007_199_254_741
    nanoseconds = 123_456_789
    raw_ns = seconds * 10**9 + nanoseconds
    packed = timestamp96(seconds, nanoseconds)
    assert msgpack.unpackb(packed, timestamp=2) == raw_ns
    value = msgpack.unpackb(packed, timestamp=1)
    assert value == 9_007_199_254_741.123
    assert abs(int(value * 1e9) - raw_ns) > 300_000


def test_to_datetime_overflow_matches_unpack_mode_3():
    for timestamp in (
        Timestamp(253_402_300_800, 0),
        Timestamp(-62_135_596_801, 0),
        Timestamp(2**63 - 1, 999_999_999),
        Timestamp(-(2**63), 0),
    ):
        with pytest.raises(OverflowError):
            timestamp.to_datetime()

from decimal import Decimal
from io import BytesIO

import pytest

from msgpack import ExtType, Timestamp, packb, unpackb
from msgpack import decimal as decimal_ext
from msgpack.decimal import (
    DECIMAL_EXT_VERSION,
    DecimalPacker,
    DecimalUnpacker,
    decode_decimal,
    dump,
    dumps,
    encode_decimal,
    load,
    loads,
    make_decimal_default,
    make_decimal_ext_hook,
    pack,
    packb as pack_decimal,
    unpack,
    unpackb as unpack_decimal,
)

EXT_CODE = 42


def finite_values():
    return [
        Decimal("1.00"),
        Decimal("1"),
        Decimal("-0.00"),
        Decimal("0.00"),
        Decimal("1E+2"),
        Decimal("1.23E-4"),
        Decimal("1234567890123456789012345678901234567890.00000000000000000001"),
    ]


@pytest.mark.parametrize("value", finite_values())
def test_finite_decimal_roundtrip_preserves_tuple(value):
    packed = pack_decimal(value, EXT_CODE)
    restored = unpack_decimal(packed, EXT_CODE)

    assert restored == value
    assert restored.as_tuple() == value.as_tuple()
    assert str(restored) == str(value)


@pytest.mark.parametrize("value", [Decimal("-0"), Decimal("-0.00000000")])
def test_negative_zero_roundtrips(value):
    restored = unpack_decimal(pack_decimal(value, EXT_CODE), EXT_CODE)

    assert restored.as_tuple() == value.as_tuple()
    assert restored.is_zero()
    assert restored.is_signed()


def test_positive_and_negative_exponents_remain_distinct():
    precise = Decimal("1.00")
    integral = Decimal("1")

    assert precise == integral
    assert precise.as_tuple() != integral.as_tuple()
    assert (
        unpack_decimal(pack_decimal(precise, EXT_CODE), EXT_CODE).as_tuple()
        == precise.as_tuple()
    )
    assert (
        unpack_decimal(pack_decimal(integral, EXT_CODE), EXT_CODE).as_tuple()
        == integral.as_tuple()
    )


def test_long_coefficient_roundtrips():
    digits = "1" + "0" * 999 + "7"
    value = Decimal(f"{digits}.000001")

    restored = unpack_decimal(pack_decimal(value, EXT_CODE), EXT_CODE)

    assert restored.as_tuple() == value.as_tuple()
    assert len(restored.as_tuple().digits) == len(value.as_tuple().digits) == 1007


@pytest.mark.parametrize(
    "text",
    ["Infinity", "-Infinity", "NaN", "-NaN", "sNaN", "-sNaN"],
)
def test_non_finite_decimal_roundtrips_explicitly(text):
    value = Decimal(text)
    packed = pack_decimal(value, EXT_CODE)
    restored = unpack_decimal(packed, EXT_CODE)

    assert restored.as_tuple() == value.as_tuple()
    assert restored.is_finite() is False
    assert restored.is_nan() == value.is_nan()
    assert restored.is_snan() == value.is_snan()
    assert restored.is_infinite() == value.is_infinite()


def test_public_entry_points_and_aliases():
    payload = encode_decimal(Decimal("1.00"))
    assert decode_decimal(payload).as_tuple() == Decimal("1.00").as_tuple()

    buffer = BytesIO()
    dump(Decimal("-0.00"), buffer, EXT_CODE)
    buffer.seek(0)
    assert load(buffer, EXT_CODE).as_tuple() == Decimal("-0.00").as_tuple()

    buffer = BytesIO()
    pack({"value": Decimal("1E+2")}, buffer, EXT_CODE)
    buffer.seek(0)
    assert unpack(buffer, EXT_CODE)["value"].as_tuple() == Decimal("1E+2").as_tuple()

    assert dumps is pack_decimal
    assert loads is unpack_decimal
    assert decimal_ext.packb is pack_decimal
    assert decimal_ext.unpackb is unpack_decimal


def test_decimal_is_not_converted_to_float():
    packed = pack_decimal(Decimal("1.00"), EXT_CODE)

    assert packed[0:1] in (
        b"\xd4",
        b"\xd5",
        b"\xd6",
        b"\xd7",
        b"\xd8",
        b"\xc7",
        b"\xc8",
        b"\xc9",
    )
    default_unpacked = unpackb(packed)
    assert isinstance(default_unpacked, ExtType)
    assert default_unpacked.code == EXT_CODE
    assert default_unpacked.data[:4] == b"MPD" + bytes((DECIMAL_EXT_VERSION,))


def test_invalid_ext_codes_are_rejected():
    for code in (-1, 128, True, "42", 1.0):
        with pytest.raises((TypeError, ValueError)):
            make_decimal_default(code)
        with pytest.raises((TypeError, ValueError)):
            make_decimal_ext_hook(code)


def test_invalid_payload_is_rejected():
    valid = pack_decimal(Decimal("1.00"), EXT_CODE)
    ext = unpackb(valid)
    bad_payloads = [
        b"bad",
        b"MPD\x02\x00\x00",
        b"MPD\x01\x09\x00",
        b"MPD\x01\x00\x02" + b"\x00" * 8 + b"1",
        b"MPD\x01\x00\x00" + b"\x00" * 8 + b"01",
        b"MPD\x01\x00\x00" + b"\x00" * 8,
    ]

    for payload in bad_payloads:
        with pytest.raises(ValueError):
            unpack_decimal(packb(ExtType(EXT_CODE, payload)), EXT_CODE)

    other = unpack_decimal(packb(ExtType(7, ext.data)), EXT_CODE)
    assert other == ExtType(7, ext.data)


def test_decimal_hooks_compose_with_caller_hooks():
    default_calls = []
    ext_calls = []

    def custom_default(obj):
        default_calls.append(("default", type(obj).__name__))
        if isinstance(obj, complex):
            return {"real": obj.real, "imag": obj.imag}
        raise TypeError(f"Cannot serialize {obj!r}")

    def custom_ext_hook(code, data):
        ext_calls.append(("ext_hook", code, data))
        return ExtType(code, data)

    record = {
        "measurement": Decimal("-0.00"),
        "vector": 1 + 2j,
        "other": ExtType(7, b"application"),
        "time": Timestamp(42, 14),
    }

    packed = packb(
        record,
        default=make_decimal_default(EXT_CODE, custom_default),
        datetime=True,
    )
    restored = unpackb(
        packed,
        ext_hook=make_decimal_ext_hook(EXT_CODE, custom_ext_hook),
        timestamp=0,
    )

    assert restored["measurement"].as_tuple() == Decimal("-0.00").as_tuple()
    assert restored["vector"] == {"real": 1.0, "imag": 2.0}
    assert restored["other"] == ExtType(7, b"application")
    assert restored["time"] == Timestamp(42, 14)
    assert default_calls == [("default", "complex")]
    assert [call[:2] for call in ext_calls] == [("ext_hook", 7)]
    assert all(call[1] != -1 for call in ext_calls)


def test_legacy_extended_and_mixed_nested_records():
    legacy_record = {
        "station": "north",
        "readings": [
            {"name": "voltage", "value": "1.00"},
            {"name": "current", "value": "1"},
        ],
        "meta": {"zero": "-0.00", "scale": "1E+2", "small": "1.23E-4"},
        "flag": ExtType(99, b"x"),
    }
    extended_record = {
        "station": "north",
        "readings": [
            {"name": "voltage", "value": Decimal("1.00")},
            {"name": "current", "value": Decimal("1")},
        ],
        "meta": {
            "zero": Decimal("-0.00"),
            "scale": Decimal("1E+2"),
            "small": Decimal("1.23E-4"),
        },
        "flag": ExtType(99, b"x"),
    }
    mixed_record = {
        "station": "north",
        "readings": [
            {"name": "voltage", "value": "1.00"},
            {"name": "current", "value": Decimal("1")},
        ],
        "meta": {
            "zero": Decimal("-0.00"),
            "scale": "1E+2",
            "small": Decimal("1.23E-4"),
        },
        "flag": ExtType(99, b"x"),
    }

    legacy_packed = packb(legacy_record)
    extended_packed = pack_decimal(extended_record, EXT_CODE)
    mixed_packed = pack_decimal(mixed_record, EXT_CODE)

    hook_calls = {"legacy": [], "extended": [], "mixed": []}

    def recording_hook(version):
        decimal_hook = make_decimal_ext_hook(EXT_CODE)

        def hook(code, data):
            hook_calls[version].append((code, bytes(data)))
            return decimal_hook(code, data)

        return hook

    legacy = unpackb(legacy_packed, ext_hook=recording_hook("legacy"))
    extended = unpack_decimal(
        extended_packed, EXT_CODE, ext_hook=recording_hook("extended")
    )
    mixed = unpack_decimal(mixed_packed, EXT_CODE, ext_hook=recording_hook("mixed"))

    expected_tuples = {
        "voltage": Decimal("1.00").as_tuple(),
        "current": Decimal("1").as_tuple(),
    }
    expected_meta = {
        "zero": Decimal("-0.00").as_tuple(),
        "scale": Decimal("1E+2").as_tuple(),
        "small": Decimal("1.23E-4").as_tuple(),
    }

    assert legacy["readings"][0]["value"] == "1.00"
    assert legacy["readings"][1]["value"] == "1"
    assert tuple(Decimal(legacy["readings"][0]["value"]).as_tuple()) == tuple(
        expected_tuples["voltage"]
    )
    assert tuple(Decimal(legacy["readings"][1]["value"]).as_tuple()) == tuple(
        expected_tuples["current"]
    )
    assert legacy["meta"]["zero"] == "-0.00"

    for record in (extended, mixed):
        for reading, expected in zip(record["readings"], expected_tuples.values()):
            value = reading["value"]
            if isinstance(value, str):
                assert Decimal(value).as_tuple() == expected
            else:
                assert value.as_tuple() == expected
        for key, expected in expected_meta.items():
            value = record["meta"][key]
            if isinstance(value, str):
                assert Decimal(value).as_tuple() == expected
            else:
                assert value.as_tuple() == expected

    for record in (legacy, extended, mixed):
        assert record["flag"] == ExtType(99, b"x")
    assert hook_calls == {
        "legacy": [(99, b"x")],
        "extended": [(99, b"x")],
        "mixed": [(99, b"x")],
    }
    assert legacy_packed != extended_packed
    assert mixed_packed != legacy_packed and mixed_packed != extended_packed


def test_pure_python_classes_and_streaming_unpacker():
    assert DecimalPacker.__mro__[1].__module__ == "msgpack.fallback"
    assert DecimalUnpacker.__mro__[1].__module__ == "msgpack.fallback"

    packer = DecimalPacker(EXT_CODE, autoreset=False)
    packer.pack(Decimal("1.00"))
    packer.pack(Decimal("-Infinity"))

    unpacker = DecimalUnpacker(ext_code=EXT_CODE)
    unpacker.feed(packer.bytes())
    assert unpacker.unpack().as_tuple() == Decimal("1.00").as_tuple()
    assert unpacker.unpack().as_tuple() == Decimal("-Infinity").as_tuple()

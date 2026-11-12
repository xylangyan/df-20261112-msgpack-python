import sys
from io import BytesIO

import pytest

import msgpack
from msgpack import (
    DEFAULT_STRING_TABLE_EXT_CODE,
    ExtraData,
    ExtType,
    InvalidStringTableIndexError,
    MissingStringTableError,
    OutOfData,
    StringTableConflictError,
    StringTableError,
    StringTablePacker,
    StringTableRef,
    StringTableUnpacker,
    Timestamp,
    packb_with_table,
    unpackb_with_table,
)

EXT = DEFAULT_STRING_TABLE_EXT_CODE


def _index_payload(index):
    if index < 0x100:
        return index.to_bytes(1, "big")
    if index < 0x10000:
        return index.to_bytes(2, "big")
    if index < 0x100000000:
        return index.to_bytes(4, "big")
    return index.to_bytes(8, "big")


def _ref_bytes(code, index):
    return msgpack.packb(ExtType(code, _index_payload(index)))


# ---------------------------------------------------------------------------
# Encoding: references, plain strings, nesting, timestamps
# ---------------------------------------------------------------------------


def test_table_string_becomes_fixext_reference():
    packer = StringTablePacker(["alpha", "beta"])
    assert packer.pack("alpha") == b"\xd4" + bytes((EXT, 0))
    assert packer.pack("beta") == b"\xd4" + bytes((EXT, 1))
    assert packer.pack_ext(1) == b"\xd4" + bytes((EXT, 1))


def test_off_table_string_stays_plain():
    packer = StringTablePacker(["alpha"])
    assert packer.pack("gamma") == msgpack.packb("gamma")
    assert packer.pack(["gamma", "alpha"]) == b"\x92\xa5gamma" + _ref_bytes(EXT, 0)


def test_references_in_nested_containers_and_map_keys():
    table = ["alpha", "beta"]
    packer = StringTablePacker(table)
    obj = {"alpha": ["beta", {"alpha": 1}], "plain": "x"}
    packed = packer.pack(obj)
    assert packed == b"\x82" + _ref_bytes(EXT, 0) + b"\x92" + _ref_bytes(EXT, 1) + (
        b"\x81" + _ref_bytes(EXT, 0) + b"\x01"
    ) + b"\xa5plain" + b"\xa1x"
    assert StringTableUnpacker(table).unpackb(packed) == {
        "alpha": ["beta", {"alpha": 1}],
        "plain": "x",
    }


def test_tuples_and_subclassed_containers_are_walked():
    table = ["t"]
    packer = StringTablePacker(table)

    class MyDict(dict):
        pass

    packed = packer.pack(("t", MyDict({"t": 1})))
    assert StringTableUnpacker(table).unpackb(packed) == ["t", {"t": 1}]


def test_timestamp_goes_through_existing_logic():
    table = ["alpha"]
    timestamp = Timestamp(1700000000, 123)
    packed = StringTablePacker(table).pack({"ts": timestamp, "s": "alpha"})
    # Timestamp keeps its own ext code (-1), distinct from the table code.
    assert bytes((EXT,)) in packed
    result = StringTableUnpacker(table).unpackb(packed)
    assert result == {"ts": timestamp, "s": "alpha"}
    assert isinstance(result["ts"], Timestamp)


def test_datetime_option_is_forwarded():
    import datetime

    table = ["alpha"]
    instant = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    packed = StringTablePacker(table, datetime=True).pack(instant)
    assert StringTableUnpacker(table).unpackb(packed) == Timestamp.from_datetime(instant)


def test_datetime_falls_back_to_user_default_without_option():
    import datetime

    instant = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    packed = StringTablePacker(["alpha"], default=lambda obj: "alpha").pack(instant)
    assert StringTableUnpacker(["alpha"]).unpackb(packed) == "alpha"


def test_binary_and_other_native_types_untouched():
    table = ["alpha"]
    packer = StringTablePacker(table)
    for value in (b"bin", bytearray(b"bin"), memoryview(b"bin"), 1, 1.5, True, None):
        assert packer.pack(value) == msgpack.packb(value)


def test_explicit_ref_marker():
    table = ["alpha", "beta"]
    packer = StringTablePacker(table)
    assert packer.pack(StringTableRef(1)) == _ref_bytes(EXT, 1)
    assert packer.pack(packer.ref(0)) == _ref_bytes(EXT, 0)


def test_duplicate_table_entries_resolve_to_first_index():
    packer = StringTablePacker(["x", "x"])
    assert packer.pack("x") == _ref_bytes(EXT, 0)


def test_default_callback_can_return_table_values():
    class Marker:
        pass

    def default(obj):
        if isinstance(obj, Marker):
            return ["alpha", {"k": "beta"}]
        raise TypeError(obj)

    table = ["alpha", "beta"]
    packed = StringTablePacker(table, default=default).pack(Marker())
    assert StringTableUnpacker(table).unpackb(packed) == ["alpha", {"k": "beta"}]


def test_pack_index_widths():
    table = ["s"] * 257 + ["last"]
    packer = StringTablePacker(table)
    assert packer.pack_ext(0) == b"\xd4" + bytes((EXT, 0))
    assert packer.pack_ext(255) == b"\xd4" + bytes((EXT, 255))
    assert packer.pack_ext(256) == b"\xd5" + bytes((EXT,)) + (256).to_bytes(2, "big")
    assert StringTableUnpacker(table).unpackb(packer.pack_ext(256)) == "s"


def test_pack_index_out_of_range_raises():
    packer = StringTablePacker(["a", "b"])
    with pytest.raises(InvalidStringTableIndexError, match="out of range"):
        packer.pack_ext(2)
    with pytest.raises(InvalidStringTableIndexError, match="out of range"):
        packer.pack(StringTableRef(2))


def test_recursion_limit_matches_core_packer():
    deep = None
    for _ in range(1200):
        deep = [deep]
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_limit, 30000))
    try:
        with pytest.raises(ValueError, match="recursion limit"):
            StringTablePacker(["a"]).pack(deep)
    finally:
        sys.setrecursionlimit(old_limit)


# ---------------------------------------------------------------------------
# Decoding: current table only, missing table, malformed references
# ---------------------------------------------------------------------------


def test_decode_uses_current_table():
    unpacker = StringTableUnpacker(["a", "b"])
    unpacker.feed(_ref_bytes(EXT, 1))
    assert unpacker.unpack() == "b"

    # Replacing the table replaces it entirely.
    unpacker.set_table(["only-one"])
    unpacker.feed(_ref_bytes(EXT, 0))
    assert unpacker.unpack() == "only-one"


def test_bad_index_does_not_fall_back_to_previous_table():
    unpacker = StringTableUnpacker(["a", "b"])
    unpacker.feed(_ref_bytes(EXT, 1))
    assert unpacker.unpack() == "b"

    # New partition header arrives with a smaller table: index 1 must fail,
    # not silently resolve against the previous table.
    unpacker.set_table(["only-one"])
    unpacker.feed(_ref_bytes(EXT, 1))
    with pytest.raises(InvalidStringTableIndexError, match="out of range"):
        unpacker.unpack()


def test_missing_table_raises():
    unpacker = StringTableUnpacker()
    unpacker.feed(_ref_bytes(EXT, 0))
    with pytest.raises(MissingStringTableError, match="no string table"):
        unpacker.unpack()


def test_missing_table_after_clear():
    unpacker = StringTableUnpacker(["a"])
    unpacker.clear_table()
    unpacker.feed(_ref_bytes(EXT, 0))
    with pytest.raises(MissingStringTableError):
        unpacker.unpack()


def test_missing_table_relaxed_mode_returns_ext_type():
    unpacker = StringTableUnpacker(require_table=False)
    unpacker.feed(_ref_bytes(EXT, 0))
    assert unpacker.unpack() == ExtType(EXT, b"\x00")


def test_missing_table_relaxed_mode_delegates_to_user_hook():
    seen = []

    def hook(code, data):
        seen.append((code, data))
        return "hooked"

    unpacker = StringTableUnpacker(require_table=False, ext_hook=hook)
    unpacker.feed(_ref_bytes(EXT, 3))
    assert unpacker.unpack() == "hooked"
    assert seen == [(EXT, b"\x03")]


@pytest.mark.parametrize("payload", [b"", b"\x01\x02\x03", b"\x00" * 5, b"\x00" * 16])
def test_malformed_payload_lengths_raise(payload):
    unpacker = StringTableUnpacker(["a"])
    unpacker.feed(msgpack.packb(ExtType(EXT, payload)))
    with pytest.raises(InvalidStringTableIndexError, match="payload"):
        unpacker.unpack()


def test_payload_widths_decode():
    table = ["s"] * 257
    unpacker = StringTableUnpacker(table)
    for index in (0, 255, 256):
        unpacker.feed(_ref_bytes(EXT, index))
        assert unpacker.unpack() == "s"


def test_eight_byte_payload_out_of_range():
    unpacker = StringTableUnpacker(["s"])
    unpacker.feed(msgpack.packb(ExtType(EXT, (5).to_bytes(8, "big"))))
    with pytest.raises(InvalidStringTableIndexError, match="out of range"):
        unpacker.unpack()


def test_interleaved_decoders_keep_independent_tables():
    table_one = ["alpha", "beta"]
    table_two = ["red", "green", "blue"]
    packed_one = StringTablePacker(table_one).pack_ext(1)
    packed_two = StringTablePacker(table_two).pack_ext(1)
    # Same index, identical bytes, different strings.
    assert packed_one == packed_two == _ref_bytes(EXT, 1)

    decoder_one = StringTableUnpacker(table_one)
    decoder_two = StringTableUnpacker(table_two)
    decoder_one.feed(packed_one)
    decoder_two.feed(packed_two)
    decoder_one.feed(packed_one)
    decoder_two.feed(packed_two)

    assert decoder_one.unpack() == "beta"
    assert decoder_two.unpack() == "green"
    assert decoder_one.unpack() == "beta"
    assert decoder_two.unpack() == "green"


def test_two_partitions_same_index_different_strings():
    partition_one = ["device-type-A", "device-type-B"]
    partition_two = ["device-type-C", "device-type-D"]
    packer_one = StringTablePacker(partition_one)
    packer_two = StringTablePacker(partition_two)

    row_one = packer_one.pack({"device": "device-type-A"})
    row_two = packer_two.pack({"device": "device-type-C"})
    # Both rows reference index 0 of their own table.
    assert row_one == row_two == b"\x81\xa6device" + _ref_bytes(EXT, 0)

    assert StringTableUnpacker(partition_one).unpackb(row_one) == {"device": "device-type-A"}
    assert StringTableUnpacker(partition_two).unpackb(row_two) == {"device": "device-type-C"}


def test_other_ext_codes_delegate_to_user_hook():
    other = ExtType(7, b"xy")
    packed = StringTablePacker(["a"]).pack({"k": [other, "a"]})

    def hook(code, data):
        assert code == 7
        return ("custom", data)

    result = StringTableUnpacker(["a"], ext_hook=hook).unpackb(packed)
    assert result == {"k": [("custom", b"xy"), "a"]}


def test_other_ext_codes_default_to_ext_type():
    packed = StringTablePacker(["a"]).pack(ExtType(9, b"z"))
    assert StringTableUnpacker(["a"]).unpackb(packed) == ExtType(9, b"z")


# ---------------------------------------------------------------------------
# Extension-code conflicts and input validation
# ---------------------------------------------------------------------------


def test_timestamp_ext_code_conflict_reports_clearly():
    match = "reserved for the MessagePack timestamp"
    with pytest.raises(StringTableConflictError, match=match):
        StringTablePacker(["a"], ext_code=-1)
    with pytest.raises(StringTableConflictError, match=match):
        StringTableUnpacker(["a"], ext_code=-1)


@pytest.mark.parametrize("bad_code", [-2, 128])
def test_ext_code_out_of_range(bad_code):
    with pytest.raises(ValueError, match="0..127"):
        StringTablePacker(["a"], ext_code=bad_code)
    with pytest.raises(ValueError, match="0..127"):
        StringTableUnpacker(["a"], ext_code=bad_code)


@pytest.mark.parametrize("bad_code", [1.0, True, "1"])
def test_ext_code_wrong_type(bad_code):
    with pytest.raises(TypeError, match="ext_code"):
        StringTablePacker(["a"], ext_code=bad_code)
    with pytest.raises(TypeError, match="ext_code"):
        StringTableUnpacker(["a"], ext_code=bad_code)


def test_custom_code_roundtrip():
    packer = StringTablePacker(["a"], ext_code=99)
    packed = packer.pack("a")
    assert packed == b"\xd4\x63\x00"
    assert StringTableUnpacker(["a"], ext_code=99).unpackb(packed) == "a"


def test_data_level_ext_code_collision_is_reported():
    packer = StringTablePacker(["a"], ext_code=99)
    with pytest.raises(StringTableConflictError, match="reserved for string-table"):
        packer.pack(ExtType(99, b"\x01"))
    # The same code is fine for a different, non-reserved ext code.
    assert StringTableUnpacker(["a"]).unpackb(packer.pack(ExtType(98, b"z"))) == ExtType(
        98, b"z"
    )


@pytest.mark.parametrize("bad_table", [[1], [b"x"], [object()]])
def test_non_string_table_entries_rejected(bad_table):
    with pytest.raises(TypeError, match="expected str"):
        StringTablePacker(bad_table)
    with pytest.raises(TypeError, match="expected str"):
        StringTableUnpacker(bad_table)


@pytest.mark.parametrize("bad_table", ["string", b"bytes", 123])
def test_non_sequence_table_rejected(bad_table):
    with pytest.raises(TypeError, match="sequence of str"):
        StringTablePacker(bad_table)
    with pytest.raises(TypeError, match="sequence of str"):
        StringTableUnpacker(bad_table)


def test_packer_requires_table():
    with pytest.raises(TypeError, match="sequence of str"):
        StringTablePacker(None)


def test_default_and_ext_hook_must_be_callable():
    with pytest.raises(TypeError, match="default"):
        StringTablePacker(["a"], default=1)
    with pytest.raises(TypeError, match="ext_hook"):
        StringTableUnpacker(["a"], ext_hook=1)


def test_error_hierarchy():
    assert issubclass(StringTableConflictError, StringTableError)
    assert issubclass(MissingStringTableError, StringTableError)
    assert issubclass(InvalidStringTableIndexError, StringTableError)
    assert issubclass(InvalidStringTableIndexError, IndexError)
    assert issubclass(StringTableError, ValueError)


def test_ref_marker_validation():
    with pytest.raises(ValueError, match="non-negative"):
        StringTableRef(-1)
    with pytest.raises(TypeError, match="index"):
        StringTableRef(1.0)
    assert StringTableRef(2) == StringTableRef(2)
    assert StringTableRef(2) != StringTableRef(3)


# ---------------------------------------------------------------------------
# Streaming, delegation and one-shot helpers
# ---------------------------------------------------------------------------


def test_streaming_feed_in_fragments():
    packed = StringTablePacker(["a", "b"]).pack_objects(["a", "b", "a"])
    unpacker = StringTableUnpacker(["a", "b"])
    for position in range(0, len(packed), 2):
        unpacker.feed(packed[position : position + 2])
    assert list(unpacker) == ["a", "b", "a"]


def test_streaming_empty_buffer_raises_out_of_data():
    unpacker = StringTableUnpacker(["a"])
    with pytest.raises(OutOfData):
        unpacker.unpack()
    assert list(unpacker) == []


def test_iteration_stops_cleanly():
    unpacker = StringTableUnpacker(["a"])
    unpacker.feed(StringTablePacker(["a"]).pack_objects(["a", "a"]))
    assert [obj for obj in unpacker] == ["a", "a"]


def test_skip_across_references():
    unpacker = StringTableUnpacker(["a"])
    unpacker.feed(StringTablePacker(["a"]).pack_objects(["a", "a"]))
    unpacker.skip()
    assert unpacker.unpack() == "a"


def test_file_like_streaming():
    packed = StringTablePacker(["a"]).pack("a")
    unpacker = StringTableUnpacker(["a"], file_like=BytesIO(packed))
    assert unpacker.unpack() == "a"
    with pytest.raises(ValueError, match="file_like"):
        unpacker.unpackb(packed)


def test_container_header_delegation():
    packed = StringTablePacker(["s"]).pack(["s", 1])
    unpacker = StringTableUnpacker(["s"])
    unpacker.feed(packed)
    assert unpacker.read_array_header() == 2
    assert unpacker.unpack() == "s"
    assert unpacker.tell() == 4
    assert unpacker.read_bytes(1) == b"\x01"


def test_unpacker_kwargs_forwarded():
    packed = StringTablePacker(["s"]).pack(["s", b"bin"])
    result = StringTableUnpacker(["s"], use_list=False, raw=True).unpackb(packed)
    assert result == ("s", b"bin")


def test_unpackb_extra_data_and_incomplete_input():
    packed = StringTablePacker(["a"]).pack_ext(0)
    unpacker = StringTableUnpacker(["a"])
    with pytest.raises(ExtraData) as exc_info:
        unpacker.unpackb(packed + b"\x01")
    assert exc_info.value.unpacked == "a"
    assert exc_info.value.extra == b"\x01"

    with pytest.raises(ValueError, match="incomplete input"):
        StringTableUnpacker(["a"]).unpackb(packed[:-1])


def test_one_shot_helpers():
    assert packb_with_table("a", ["a"]) == _ref_bytes(EXT, 0)
    assert unpackb_with_table(_ref_bytes(EXT, 0), ["a"]) == "a"


def test_pack_objects_and_autoreset_streaming():
    packer = StringTablePacker(["a", "b"], autoreset=False)
    packer.pack("a")
    packer.pack("b")
    assert packer.packer.bytes() == _ref_bytes(EXT, 0) + _ref_bytes(EXT, 1)

    reset_packer = StringTablePacker(["a"])
    assert reset_packer.pack_objects(["a", "a"]) == _ref_bytes(EXT, 0) * 2


def test_table_properties_and_mutation_isolation():
    source = ["a", "b"]
    packer = StringTablePacker(source)
    unpacker = StringTableUnpacker(source)
    source.append("c")
    assert packer.table == ("a", "b")
    assert unpacker.table == ("a", "b")
    assert packer.ext_code == EXT
    assert unpacker.ext_code == EXT


def test_generator_and_tuple_tables():
    assert StringTablePacker(str(i) for i in range(3)).pack("2") == _ref_bytes(EXT, 2)
    assert StringTablePacker(("p", "q")).pack("p") == _ref_bytes(EXT, 0)

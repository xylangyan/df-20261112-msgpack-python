"""Record boundary and committed-offset tests for the pure Python Unpacker."""

from io import BytesIO

from pytest import mark, raises

from msgpack import (
    IncompleteRecord,
    InvalidRecord,
    OutOfData,
    Record,
    Unpacker,
    packb,
)

pytestmark = mark.skipif(
    Unpacker.__module__ != "msgpack.fallback",
    reason="record boundary API is implemented on the pure Python unpacker",
)


def _drain(unpacker):
    records = []
    while True:
        try:
            records.append(unpacker.unpack_record())
        except IncompleteRecord as incomplete:
            return records, incomplete.offset
        except OutOfData:
            return records, None


def _feed_by_chunks(stream, chunk_sizes):
    unpacker = Unpacker()
    chunks = []
    pos = 0
    for size in chunk_sizes:
        chunks.append(stream[pos : pos + size])
        pos += size
    assert pos == len(stream)
    for chunk in chunks:
        unpacker.feed(chunk)
    return unpacker


def _feed_byte_by_byte(stream):
    unpacker = Unpacker()
    for byte in stream:
        unpacker.feed(bytes((byte,)))
    return unpacker


def test_unpack_record_offsets_and_sizes():
    records_values = [1, "abc", [1, 2], {"k": b"v"}]
    stream = b"".join(packb(value) for value in records_values)

    unpacker = Unpacker()
    unpacker.feed(stream)

    cursor = 0
    for value in records_values:
        record = unpacker.unpack_record()
        assert record.value == value
        assert record.start == cursor
        assert record.end == cursor + len(packb(value))
        assert record.size == len(packb(value))
        assert record.end == unpacker.tell()
        cursor = record.end
    assert cursor == len(stream)


def test_record_supports_tuple_unpacking_and_aliases():
    unpacker = Unpacker()
    unpacker.feed(packb("x") + packb(7))
    record = unpacker.unpack_record()
    value, start, end = record
    assert (value, start, end) == ("x", 0, 2)
    assert record.obj == record.value
    assert record.start_offset == record.start
    assert record.end_offset == record.end
    assert isinstance(record, Record)


def test_chunking_does_not_change_committed_offsets_or_records():
    values = [42, b"payload" * 50, [1, 2, 3], {"a": "b", "c": [4]}, "tail"]
    stream = b"".join(packb(value) for value in values)
    assert len(stream) > 200

    single_byte = _feed_byte_by_byte(stream)
    sizes = [1, 7, 33]
    remainder = len(stream) - sum(sizes)
    sizes += [remainder // 2, remainder - remainder // 2]
    bulk = _feed_by_chunks(stream, sizes)
    file_like = Unpacker(BytesIO(stream), read_size=4)

    expected_records = []
    cursor = 0
    for value in values:
        packed = packb(value)
        expected_records.append(Record(value, cursor, cursor + len(packed)))
        cursor += len(packed)

    got_single, incomplete_at_single = _drain(single_byte)
    got_bulk, incomplete_at_bulk = _drain(bulk)
    got_file, incomplete_at_file = _drain(file_like)

    assert got_single == expected_records
    assert got_bulk == expected_records
    assert got_file == expected_records
    assert incomplete_at_single is None
    assert incomplete_at_bulk is None
    assert incomplete_at_file is None
    assert single_byte.tell() == bulk.tell() == file_like.tell() == len(stream)


def test_half_record_single_byte_and_bulk_feeds_agree():
    complete = [packb({"id": i, "data": bytes((i,)) * 8}) for i in range(3)]
    half_record = packb("the rest comes later")
    cut = sum(map(len, complete)) + len(half_record) // 2
    stream = b"".join(complete) + half_record
    assert 0 < cut < len(stream)

    def run(chunker):
        unpacker = Unpacker()
        for chunk in chunker(stream, cut):
            unpacker.feed(chunk)
        records, incomplete_at = _drain(unpacker)
        assert incomplete_at == unpacker.tell()
        return records, unpacker.tell()

    single_records, single_offset = run(
        lambda data, cut: [bytes((b,)) for b in data[:cut]]
    )
    bulk_records, bulk_offset = run(lambda data, cut: [data[:cut]])

    expected_cursor = sum(map(len, complete))
    assert single_offset == bulk_offset == expected_cursor
    assert single_records == bulk_records
    assert [record.value for record in single_records] == [
        {"id": i, "data": bytes((i,)) * 8} for i in range(3)
    ]
    assert single_records[-1].end == expected_cursor


def test_resume_from_committed_position_with_remaining_bytes():
    head_records = [packb(1), packb([2, 3]), packb("four")]
    tail_record = packb({"tail": True})
    stream = b"".join(head_records) + tail_record
    cut = len(b"".join(head_records)) + len(tail_record) // 2

    # Feed one half of the stream, byte by byte, until only half a record
    # remains buffered.
    unpacker = Unpacker()
    for byte in stream[:cut]:
        unpacker.feed(bytes((byte,)))
    records, incomplete_at = _drain(unpacker)
    checkpoint = unpacker.tell()
    assert incomplete_at == checkpoint
    assert checkpoint == len(b"".join(head_records))

    # Same unpacker: only the missing bytes are needed to finish the record.
    unpacker.feed(stream[cut:])
    resumed = unpacker.unpack_record()
    assert resumed.value == {"tail": True}
    assert (resumed.start, resumed.end) == (checkpoint, len(stream))
    assert unpacker.tell() == len(stream)

    # A fresh unpacker re-fed the same stream from the committed position
    # reconstructs the identical final record.
    restarted = Unpacker()
    restarted.feed(stream[checkpoint:])
    restart_record = restarted.unpack_record()
    assert restart_record.value == resumed.value
    assert restart_record.size == resumed.size
    assert restart_record.end == len(stream) - checkpoint


def test_incomplete_record_keeps_committed_position():
    unpacker = Unpacker()
    unpacker.feed(packb("first"))
    first = unpacker.unpack_record()
    assert first.start == 0 and first.end == 6

    # Half of a str16 record: header present, payload missing.
    unpacker.feed(b"\xda\x00\x05ab")
    with raises(IncompleteRecord) as info:
        unpacker.unpack_record()
    assert info.value.offset == 6
    assert info.value.start == 6
    assert unpacker.tell() == 6

    # Feeding the rest completes the very same record at the same offsets.
    unpacker.feed(b"cde")
    record = unpacker.unpack_record()
    assert record.value == "abcde"
    assert (record.start, record.end) == (6, 14)
    assert unpacker.tell() == 14


def test_clean_end_of_stream_is_not_incomplete_record():
    unpacker = Unpacker()
    unpacker.feed(packb(1))
    assert unpacker.unpack_record().value == 1
    with raises(OutOfData) as info:
        unpacker.unpack_record()
    assert not isinstance(info.value, IncompleteRecord)
    assert unpacker.tell() == 1


def test_invalid_record_reports_offset_and_remains_recoverable():
    good = packb("before")
    stream = good + b"\xc1" + packb("after")

    unpacker = Unpacker()
    unpacker.feed(stream)
    assert unpacker.unpack_record().value == "before"

    with raises(InvalidRecord) as info:
        unpacker.unpack_record()
    invalid = info.value
    assert invalid.offset == len(good)
    assert invalid.start == len(good)
    assert isinstance(invalid, ValueError)
    # The committed position stays at the start of the invalid record.
    assert unpacker.tell() == len(good)

    # Callers can skip the bad byte via the lower-level API and resync;
    # the next valid record is then decoded normally.
    unpacker.read_bytes(1)
    record = unpacker.unpack_record()
    assert record.value == "after"
    assert record.start == len(good) + 1
    assert record.end == len(stream)


def test_invalid_inside_nested_record_reports_outer_start():
    stream = b"\x92\x01\xc1" + packb("ok")
    unpacker = Unpacker()
    unpacker.feed(stream)
    with raises(InvalidRecord) as info:
        unpacker.unpack_record()
    assert info.value.offset == 0
    assert unpacker.tell() == 0


@mark.parametrize("chunked", [False, True])
def test_skip_commits_whole_record(chunked):
    first = packb([1, 2, 3])
    second = packb("next")
    stream = first + second
    unpacker = Unpacker()
    if chunked:
        for byte in stream:
            unpacker.feed(bytes((byte,)))
    else:
        unpacker.feed(stream)

    unpacker.skip()
    assert unpacker.tell() == len(first)
    record = unpacker.unpack_record()
    assert record.value == "next"
    assert (record.start, record.end) == (len(first), len(first) + len(second))


def test_skip_incomplete_record_does_not_commit():
    unpacker = Unpacker()
    unpacker.feed(packb("x"))
    unpacker.feed(b"\x92\x01")  # array of 2 with only one element present
    unpacker.skip()
    assert unpacker.tell() == len(packb("x"))
    with raises(OutOfData):
        unpacker.skip()
    assert unpacker.tell() == len(packb("x"))
    unpacker.feed(b"\x02")
    unpacker.skip()
    assert unpacker.tell() == len(packb("x")) + 3


def test_skip_incomplete_nested_record_rolls_back_to_outer_start():
    # array(2) [ map(1) {"k": <missing>}, <missing> ]
    unpacker = Unpacker()
    unpacker.feed(b"\x92\x81\xa1k")
    with raises(OutOfData):
        unpacker.skip()
    assert unpacker.tell() == 0
    unpacker.feed(b"\x01\x02")
    unpacker.skip()
    assert unpacker.tell() == 6


def test_container_headers_commit_only_the_header():
    array_stream = packb([1, 2])
    unpacker = Unpacker()
    unpacker.feed(array_stream)
    assert unpacker.read_array_header() == 2
    assert unpacker.tell() == 1
    first = unpacker.unpack_record()
    assert first.value == 1 and first.start == 1 and first.end == 2
    second = unpacker.unpack_record()
    assert second.value == 2 and second.start == 2 and second.end == 3

    map_stream = packb({"a": 1})
    unpacker = Unpacker()
    unpacker.feed(map_stream)
    assert unpacker.read_map_header() == 1
    assert unpacker.tell() == 1
    key = unpacker.unpack_record()
    assert key.value == "a" and key.start == 1


def test_container_header_offset_is_chunking_independent():
    # array16 header: 0xdc 0x01 0x2c (300 elements, header only).
    header = b"\xdc\x01\x2c"

    bulk = Unpacker()
    bulk.feed(header)
    assert bulk.read_array_header() == 300
    assert bulk.tell() == 3

    bytewise = Unpacker()
    bytewise.feed(header[:2])
    with raises(OutOfData):
        bytewise.read_array_header()
    assert bytewise.tell() == 0
    bytewise.feed(header[2:])
    assert bytewise.read_array_header() == 300
    assert bytewise.tell() == 3

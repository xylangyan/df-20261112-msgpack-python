#!/usr/bin/env python
import io

import pytest

from msgpack import ExtType, Packer, Unpacker, packb
from msgpack.exceptions import OutOfData


def test_projects_only_requested_columns_in_order():
    unpacker = Unpacker(raw=False)
    unpacker.feed(packb({"ts": 123, "value": 1.5, "blob": b"0123456789"}))
    ts, value = unpacker.unpack_fields(("ts", "value"))
    assert ts == 123
    assert value == 1.5
    # The whole map was consumed.
    assert not list(unpacker)


def test_two_columns_and_next_record_position():
    first = packb({"ts": 1, "value": "a", "blob": b"x" * 8})
    second = packb({"ts": 2, "value": "b"})
    unpacker = Unpacker(io.BytesIO(first + second), raw=False)

    before = unpacker.tell()
    ts, value = unpacker.unpack_fields(("ts", "value"))
    assert (ts, value) == (1, "a")
    assert unpacker.tell() == before + len(first)

    ts, value = unpacker.unpack_fields(("ts", "value"))
    assert (ts, value) == (2, "b")
    assert unpacker.tell() == before + len(first) + len(second)


def test_duplicate_selected_key_last_wins():
    packer = Packer()
    packed = (
        packer.pack_map_header(3)
        + packb("ts")
        + packb(1)
        + packb("ts")
        + packb(2)
        + packb("value")
        + packb("v")
    )
    unpacker = Unpacker(raw=False)
    unpacker.feed(packed)
    ts, value = unpacker.unpack_fields(("ts", "value"))
    assert ts == 2
    assert value == "v"


def test_skipped_ext_does_not_run_ext_hook_but_selected_does():
    calls = []

    def ext_hook(code, data):
        calls.append((code, bytes(data)))
        return ExtType(code, data)

    packed = packb(
        {
            "blob": ExtType(7, b"skip-me"),
            "ts": ExtType(8, b"keep-me"),
            "nested": [ExtType(9, b"also-skipped")],
            "value": 42,
        }
    )
    unpacker = Unpacker(ext_hook=ext_hook, raw=False)
    unpacker.feed(packed)
    ts, value = unpacker.unpack_fields(("ts", "value"))
    assert ts == ExtType(8, b"keep-me")
    assert value == 42
    # Only the selected ext reaches ext_hook; skipped exts never do.
    assert calls == [(8, b"keep-me")]


def test_skipped_object_hook_does_not_run():
    hook_maps = []

    def object_hook(obj):
        hook_maps.append(obj)
        return obj

    packed = packb(
        {
            "ts": 1,
            "payload": {"deep": ["binary", b"data", {"x": 1}]},
            "value": 2,
        }
    )
    unpacker = Unpacker(object_hook=object_hook, raw=False)
    unpacker.feed(packed)
    ts, value = unpacker.unpack_fields(("ts", "value"))
    assert (ts, value) == (1, 2)
    # Neither the projected message nor its nested skipped map reach the hook.
    assert hook_maps == []


def test_skipped_values_respect_ext_length_limit():
    # ext32 header claiming a huge logical length with almost no payload.
    truncated_ext = b"\xc9\xff\xff\xff\xff\x2a"
    packed = (
        Packer().pack_map_header(2)
        + packb("ts")
        + packb(1)
        + packb("blob")
        + truncated_ext
    )
    unpacker = Unpacker(raw=False, max_ext_len=10)
    unpacker.feed(packed)
    with pytest.raises(ValueError, match="exceeds max_ext_len"):
        unpacker.unpack_fields(("ts", "value"))


def test_huge_logical_length_truncated_sample_raises_outofdata():
    # bin32 declaring 0xffffffff bytes, followed by only a few bytes.
    huge_bin = b"\xc6\xff\xff\xff\xff" + b"abc"
    truncated = (
        Packer().pack_map_header(2)
        + packb("ts")
        + packb(7)
        + packb("blob")
        + huge_bin
    )

    unpacker = Unpacker(raw=False, max_buffer_size=2**32)
    unpacker.feed(truncated)
    with pytest.raises(OutOfData):
        unpacker.unpack_fields(("ts", "value"))
    # Rolled back to the start of the incomplete map; nothing was consumed.
    assert unpacker.tell() == 0

    # The limit is enforced from the logical length, not the payload size.
    limited = Unpacker(raw=False, max_bin_len=3)
    limited.feed(truncated)
    with pytest.raises(ValueError, match="exceeds max_bin_len"):
        limited.unpack_fields(("ts", "value"))


def test_truncated_projection_resumes_and_next_record_is_correct():
    packer = Packer()
    complete = packb({"ts": 1, "value": "a", "blob": b"z" * 4})
    next_record = packb({"ts": 2, "value": "b"})

    # Cut the blob's payload in half: the record is genuinely resumable.
    cut = complete[: complete.rindex(b"z")]
    head, tail = cut, complete[len(cut) :]

    unpacker = Unpacker(raw=False)
    unpacker.feed(head)
    with pytest.raises(OutOfData):
        unpacker.unpack_fields(("ts", "value"))
    assert unpacker.tell() == 0

    unpacker.feed(tail)
    ts, value = unpacker.unpack_fields(("ts", "value"))
    assert (ts, value) == (1, "a")
    assert unpacker.tell() == len(complete)

    # The following record starts exactly where the projection stopped.
    unpacker.feed(next_record)
    ts, value = unpacker.unpack_fields(("ts", "value"))
    assert (ts, value) == (2, "b")
    assert unpacker.tell() == len(complete) + len(next_record)


def test_non_map_consumes_nothing():
    unpacker = Unpacker(raw=False)
    unpacker.feed(packb([1, 2, 3]) + packb({"ts": 9}))
    before = unpacker.tell()
    with pytest.raises(ValueError, match="Expected map"):
        unpacker.unpack_fields(("ts",))
    assert unpacker.tell() == before
    assert unpacker.unpack() == [1, 2, 3]
    (ts,) = unpacker.unpack_fields(("ts",))
    assert ts == 9


def test_strict_map_key_applies_to_skipped_keys():
    packed = (
        Packer().pack_map_header(2)
        + packb(1)  # non-str key on a skipped pair
        + packb("x")
        + packb("ts")
        + packb(2)
    )
    strict = Unpacker(raw=False)
    strict.feed(packed)
    with pytest.raises(ValueError, match="not allowed for map key"):
        strict.unpack_fields(("ts",))

    relaxed = Unpacker(raw=False, strict_map_key=False)
    relaxed.feed(packed)
    assert relaxed.unpack_fields(("ts",)) == (2,)


def test_missing_fields_are_none_and_bytes_keys_supported():
    unpacker = Unpacker(raw=True)
    unpacker.feed(packb({b"ts": 5}))
    ts, value = unpacker.unpack_fields((b"ts", b"value"))
    assert ts == 5
    assert value is None


def test_deep_skipped_array_is_consumed_without_hooks():
    calls = []

    def ext_hook(code, data):
        calls.append(code)
        return ExtType(code, data)

    deep = ExtType(3, b"buried")
    for _ in range(50):
        deep = [deep]
    packed = packb({"ts": 10, "deep": deep, "value": "v"})
    unpacker = Unpacker(ext_hook=ext_hook, raw=False)
    unpacker.feed(packed)
    ts, value = unpacker.unpack_fields(("ts", "value"))
    assert (ts, value) == (10, "v")
    assert calls == []
    assert not list(unpacker)


def test_counting_ext_hook_and_truncated_huge_length_sample():
    calls = []

    def ext_hook(code, data):
        calls.append(code)
        return ExtType(code, data)

    packer = Packer()
    first = packb({"ts": ExtType(1, b"t"), "value": 9, "blob": ExtType(2, b"ignored")})
    # Second record: bin32 declaring 0xffffffff bytes, only 3 payload bytes.
    second = (
        packer.pack_map_header(2)
        + packb("ts")
        + packb(8)
        + packb("blob")
        + b"\xc6\xff\xff\xff\xff"
        + b"abc"
    )

    unpacker = Unpacker(
        io.BytesIO(first + second), ext_hook=ext_hook, raw=False, max_buffer_size=2**33
    )
    ts, value = unpacker.unpack_fields(("ts", "value"))
    assert (ts, value) == (ExtType(1, b"t"), 9)
    # The counting hook ran exactly once, for the selected ext only.
    assert calls == [1]
    assert unpacker.tell() == len(first)

    # The truncated record raises OutOfData and consumes nothing.
    with pytest.raises(OutOfData):
        unpacker.unpack_fields(("ts", "value"))
    assert unpacker.tell() == len(first)
    assert calls == [1]


def test_projection_synonyms():
    for name in ("unpack_projection", "unpack_map_fields"):
        unpacker = Unpacker(raw=False)
        unpacker.feed(packb({"ts": 1, "value": 2, "blob": b"x"}))
        assert getattr(unpacker, name)(("ts", "value")) == (1, 2)


def test_old_unpack_entry_point_unchanged_with_hooks():
    calls = []

    def ext_hook(code, data):
        calls.append(code)
        return ExtType(code, data)

    def object_hook(obj):
        obj["seen"] = True
        return obj

    packed = packb({"ts": ExtType(1, b"x"), "value": 1})
    unpacker = Unpacker(ext_hook=ext_hook, object_hook=object_hook, raw=False)
    unpacker.feed(packed)
    decoded = unpacker.unpack()
    assert decoded["seen"] is True
    assert calls == [1]

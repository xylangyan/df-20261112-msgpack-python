#!/usr/bin/env python

import array

import pytest

from msgpack import ExtType, PackBufferFull, Packer, packb

SENTINEL = b"\xa5"


def make_page(slot_size, prefix=4, suffix=4):
    """A page with sentinel bytes before and after a zeroed slot."""
    return bytearray(SENTINEL * prefix + b"\x00" * slot_size + SENTINEL * suffix)


def slot_view(page, prefix=4, suffix=4):
    return memoryview(page)[prefix : len(page) - suffix]


def test_pack_into_exact_fit():
    obj = [1, "ab", {"k": None}]
    packed = packb(obj)
    page = make_page(len(packed))
    end = Packer().pack_into(slot_view(page), 0, obj)
    assert end == len(packed)
    assert bytes(page) == SENTINEL * 4 + packed + SENTINEL * 4


def test_pack_into_one_byte_short():
    obj = [1, "ab", {"k": None}]
    packed = packb(obj)
    page = make_page(len(packed) - 1)
    snapshot = bytes(page)
    with pytest.raises(PackBufferFull) as excinfo:
        Packer().pack_into(slot_view(page), 0, obj)
    assert excinfo.value.needed == len(packed)
    assert bytes(page) == snapshot


def test_pack_into_with_offset():
    obj = {"a": 1, "b": [2, 3]}
    packed = packb(obj)
    page = make_page(2 + len(packed))
    end = Packer().pack_into(slot_view(page), 2, obj)
    assert end == 2 + len(packed)
    assert bytes(page) == SENTINEL * 4 + b"\x00\x00" + packed + SENTINEL * 4


def test_pack_into_keeps_bytes_outside_slot():
    page = bytearray(b"HEAD" + b"\x00" * 3 + b"TAIL")
    end = Packer().pack_into(memoryview(page)[4:7], 0, 42)
    assert end == 1
    assert bytes(page) == b"HEAD" + packb(42) + b"\x00\x00" + b"TAIL"


def test_pack_into_leaves_slot_untouched_on_failure():
    page = bytearray(b"HEAD" + b"\xde\xad\xbe\xef" + b"TAIL")
    with pytest.raises(PackBufferFull):
        Packer().pack_into(memoryview(page)[4:8], 0, "x" * 100)
    assert bytes(page) == b"HEAD" + b"\xde\xad\xbe\xef" + b"TAIL"


def test_pack_into_default_called_once():
    class Custom:
        pass

    first, second = Custom(), Custom()
    calls = []

    def default(obj):
        calls.append(obj)
        return {"v": [1, 2, 3]}

    obj = [first, {"k": second}]
    expected = packb(obj, default=lambda o: {"v": [1, 2, 3]})
    page = make_page(len(expected))
    end = Packer(default=default).pack_into(slot_view(page), 0, obj)
    assert end == len(expected)
    assert bytes(slot_view(page)) == expected
    assert calls == [first, second]


def test_pack_into_default_called_once_on_failure():
    class Custom:
        pass

    value = Custom()
    calls = []

    def default(obj):
        calls.append(obj)
        return "converted"

    expected = packb(value, default=lambda o: "converted")
    page = make_page(len(expected) - 1)
    with pytest.raises(PackBufferFull) as excinfo:
        Packer(default=default).pack_into(slot_view(page), 0, value)
    assert excinfo.value.needed == len(expected)
    assert calls == [value]


def test_pack_into_ext_type():
    obj = ExtType(0x42, b"ABC")
    packed = packb(obj)
    page = make_page(len(packed))
    end = Packer().pack_into(slot_view(page), 0, obj)
    assert end == len(packed)
    assert bytes(slot_view(page)) == packed


def test_pack_into_nested_ext_and_default():
    class Custom:
        pass

    def default(obj):
        return ExtType(7, b"payload")

    obj = {"ext": ExtType(1, b"ab"), "custom": Custom(), "list": [1, [2, [3]]]}
    expected = packb(obj, default=default)
    page = make_page(len(expected))
    end = Packer(default=default).pack_into(slot_view(page), 0, obj)
    assert end == len(expected)
    assert bytes(slot_view(page)) == expected


def test_pack_into_readonly():
    with pytest.raises(TypeError):
        Packer().pack_into(b"\x00" * 8, 0, 1)
    with pytest.raises(TypeError):
        Packer().pack_into(memoryview(b"\x00" * 8), 0, 1)


def test_pack_into_noncontiguous():
    page = bytearray(16)
    view = memoryview(page)[::2]
    assert not view.contiguous
    with pytest.raises(ValueError):
        Packer().pack_into(view, 0, 1)
    assert bytes(page) == b"\x00" * 16


@pytest.mark.parametrize("offset", [-1, -100])
def test_pack_into_negative_offset(offset):
    with pytest.raises(ValueError):
        Packer().pack_into(memoryview(bytearray(8)), offset, 1)


def test_pack_into_offset_out_of_range():
    with pytest.raises(ValueError):
        Packer().pack_into(memoryview(bytearray(8)), 9, 1)


@pytest.mark.parametrize("offset", [1.0, "1", None])
def test_pack_into_invalid_offset_type(offset):
    with pytest.raises(TypeError):
        Packer().pack_into(memoryview(bytearray(8)), offset, 1)


def test_pack_into_multibyte_itemsize():
    buf = array.array("H", [0, 0, 0, 0])
    end = Packer().pack_into(memoryview(buf), 0, 255)
    assert end == 2
    assert bytes(buf)[:2] == packb(255)


def test_pack_into_does_not_touch_internal_buffer():
    packer = Packer(autoreset=False)
    packer.pack(1)
    before = packer.bytes()
    packer.pack_into(memoryview(bytearray(8)), 0, [1, 2])
    assert packer.bytes() == before
    with pytest.raises(PackBufferFull):
        packer.pack_into(memoryview(bytearray(1)), 0, [1, 2, 3])
    assert packer.bytes() == before


def test_pack_into_unserializable_leaves_buffer():
    page = bytearray(b"ABCD")
    with pytest.raises(TypeError):
        Packer().pack_into(memoryview(page), 1, object())
    assert bytes(page) == b"ABCD"

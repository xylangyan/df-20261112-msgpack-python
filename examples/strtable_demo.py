#!/usr/bin/env python
"""Demo: two partitions share one index space but different string tables.

Run with:  python examples/strtable_demo.py
"""

from msgpack import (
    MissingStringTableError,
    StringTablePacker,
    StringTableUnpacker,
)


def show(label, data):
    print(f"{label:<34} {data.hex(' ')}")


def main():
    # Partition 1 and 2 publish different string tables.
    table_one = ["device-type-A", "device-type-B"]
    table_two = ["device-type-C", "device-type-D"]

    row_one = {"device": "device-type-A", "temp": 21.5}
    row_two = {"device": "device-type-C", "temp": 21.5}

    packer_one = StringTablePacker(table_one)
    packer_two = StringTablePacker(table_two)

    bytes_one = packer_one.pack(row_one)
    bytes_two = packer_two.pack(row_two)

    print("== actual extension bytes ==")
    show("partition 1 row", bytes_one)
    show("partition 2 row", bytes_two)
    # The device field is identical bytes in both rows: ext code 0x40, index 0.
    assert bytes_one == bytes_two

    print()
    print("== replay with each partition's table ==")
    decoder_one = StringTableUnpacker(table_one)
    decoder_two = StringTableUnpacker(table_two)
    print("partition 1 ->", decoder_one.unpackb(bytes_one))
    print("partition 2 ->", decoder_two.unpackb(bytes_two))

    print()
    print("== interleaved decoders, same index ==")
    decoder_one.feed(bytes_one)
    decoder_two.feed(bytes_two)
    decoder_one.feed(bytes_one)
    decoder_two.feed(bytes_two)
    for decoder, name in ((decoder_one, "p1"), (decoder_two, "p2"),
                          (decoder_one, "p1"), (decoder_two, "p2")):
        print(name, "->", decoder.unpack()["device"])

    print()
    print("== decoding without the partition header ==")
    orphan = StringTableUnpacker()
    orphan.feed(bytes_one)
    try:
        orphan.unpack()
    except MissingStringTableError as exc:
        print("MissingStringTableError:", exc)


if __name__ == "__main__":
    main()

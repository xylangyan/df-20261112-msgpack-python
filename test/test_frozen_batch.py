#!/usr/bin/env python
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pytest

from msgpack import (
    FrozenBufferFull,
    Packer,
    Unpacker,
    packb,
)

pytestmark = pytest.mark.skipif(
    Packer.__module__ != "msgpack.fallback",
    reason="freeze()/ack() handoff is only implemented by the pure Python Packer",
)


def records(payload):
    """Decode every msgpack record contained in one batch payload."""
    unpacker = Unpacker(BytesIO(bytes(payload)), strict_map_key=False)
    return list(unpacker)


def test_freeze_returns_snapshot_and_clears_buffer():
    packer = Packer(autoreset=False)
    packer.pack([1, 2])
    packer.pack("hello")

    batch = packer.freeze()

    assert records(batch.bytes()) == [[1, 2], "hello"]
    assert len(batch) == len(packb([1, 2])) + len(packb("hello"))
    assert bytes(batch) == batch.bytes()
    assert packer.bytes() == b""
    assert repr(batch) == f"FrozenBatch(seq=0, size={len(batch)})"


def test_two_batches_interleave_records_and_view_windows():
    """Two batches are packed, read and acknowledged out of sync.

    Each frozen view must keep yielding exactly the records of its own
    batch while the packer already works on the next batch.
    """
    packer = Packer(autoreset=False)

    # Batch 0: pack, freeze and take a view.
    packer.pack({"id": 0, "items": [1, 2, 3]})
    packer.pack("record-0")
    batch0 = packer.freeze()
    view0 = batch0.getbuffer()
    expected0 = [{"id": 0, "items": [1, 2, 3]}, "record-0"]

    # Batch 1 is built while batch 0 is still being read by the consumer.
    packer.pack({"id": 1, "items": [4, 5]})
    packer.pack("record-1")
    packer.pack(None)
    batch1 = packer.freeze()
    view1 = batch1.getbuffer()
    expected1 = [{"id": 1, "items": [4, 5]}, "record-1", None]

    # The views are disjoint and each decodes to its own batch records.
    assert bytes(view0) != bytes(view1)
    assert records(view0) == expected0
    assert records(view1) == expected1

    # Consumer retries batch 0 after a failed send: the same bytes object
    # is handed out again while batch 1 stays outstanding.
    assert batch0.bytes() is batch0.bytes()
    assert records(batch0.bytes()) == expected0
    assert [b.seq for b in packer.pending()] == [0, 1]

    # Late acknowledgement of batch 0 releases only its snapshot; batch 1
    # (including its view) is untouched.
    assert packer.ack(batch0.seq) is True
    assert records(batch1.bytes()) == expected1
    assert [b.seq for b in packer.pending()] == [1]
    assert bytes(view1) == batch1.bytes()

    # The view handed out before the acknowledgement remains readable ...
    assert records(view0) == expected0
    # ... while new reads from the acknowledged batch are refused.
    with pytest.raises(ValueError, match="acknowledged"):
        batch0.bytes()
    with pytest.raises(ValueError, match="acknowledged"):
        batch0.getbuffer()

    # Duplicate and out-of-range acknowledgements are harmless no-ops and
    # never touch the newer batch.
    assert packer.ack(batch0.seq) is False
    assert packer.ack(999) is False
    assert [b.seq for b in packer.pending()] == [1]
    assert records(batch1.bytes()) == expected1

    packer.ack(batch1.seq)
    assert packer.pending() == []
    with pytest.raises(ValueError, match="acknowledged"):
        batch1.bytes()


def test_pack_and_reset_after_freeze_keep_handoff_bytes():
    packer = Packer(autoreset=False)
    packer.pack("first")
    batch0 = packer.freeze()
    snapshot = batch0.bytes()

    packer.pack("second")
    assert records(packer.bytes()) == ["second"]
    packer.reset()
    assert packer.bytes() == b""
    packer.pack("third")
    assert records(packer.bytes()) == ["third"]

    # Resetting/reusing the packer must not mutate the handed-off batch.
    assert batch0.bytes() == snapshot
    assert records(batch0.bytes()) == ["first"]
    assert len(packer.pending()) == 1


def test_ack_on_batch_object_matches_packer_ack():
    packer = Packer(autoreset=False)
    packer.pack(1)
    batch = packer.freeze()

    assert batch.ack() is True
    assert packer.pending() == []
    assert batch.ack() is False


def test_backpressure_max_unacked_blocks_freeze():
    packer = Packer(autoreset=False, max_unacked=2)
    packer.pack(1)
    b0 = packer.freeze()
    packer.pack(2)
    b1 = packer.freeze()

    packer.pack(3)
    with pytest.raises(FrozenBufferFull):
        packer.freeze()

    # The rejected freeze leaves the current batch packed and intact.
    assert records(packer.bytes()) == [3]
    assert [b.seq for b in packer.pending()] == [0, 1]
    assert records(b1.bytes()) == [2]

    # One acknowledgement frees exactly one slot; the pending batch can
    # then be frozen and keeps its sequence order.
    assert packer.ack(b0.seq) is True
    b2 = packer.freeze()
    assert b2.seq == 2
    assert records(b2.bytes()) == [3]
    assert [b.seq for b in packer.pending()] == [1, 2]


def test_max_unacked_validation():
    with pytest.raises(ValueError):
        Packer(autoreset=False, max_unacked=0)
    with pytest.raises(TypeError):
        Packer(autoreset=False, max_unacked=1.5)
    with pytest.raises(TypeError):
        Packer(autoreset=False, max_unacked=True)
    assert Packer(autoreset=False, max_unacked=None).pending() == []


def test_freeze_works_with_autoreset():
    packer = Packer()
    assert packer.pack(7) == packb(7)

    # With autoreset=True each pack already returns its bytes; freeze on
    # an empty buffer simply hands off an empty, resettable batch.
    empty = packer.freeze()
    assert len(empty) == 0
    assert empty.bytes() == b""
    assert packer.ack(empty.seq) is True


def test_empty_freeze_and_reused_sequences_are_independent():
    packer = Packer(autoreset=False)
    empty = packer.freeze()
    assert empty.seq == 0
    assert empty.bytes() == b""

    packer.pack("x")
    one = packer.freeze()
    assert one.seq == 1
    assert records(one.bytes()) == ["x"]

    packer.ack(empty.seq)
    # Acknowledging the empty batch cannot acknowledge the later one.
    assert [b.seq for b in packer.pending()] == [1]
    assert records(one.bytes()) == ["x"]


def test_getbuffer_contract_unchanged():
    packer = Packer(autoreset=False, use_bin_type=True)
    packer.pack([1, 2])
    view = packer.getbuffer()
    assert isinstance(view, memoryview)
    assert bytes(view) == packb([1, 2], use_bin_type=True)
    view.release()
    assert packer.bytes() == packb([1, 2], use_bin_type=True)


def test_concurrent_consumer_and_packer():
    """Producer freezes batches while a consumer reads and acks them."""
    packer = Packer(autoreset=False, max_unacked=4)
    count = 200
    consumed = []

    def consumer(batch):
        view = batch.getbuffer()
        # Simulate a read, then a retry, then acknowledgement.
        assert records(view) == records(batch.bytes())
        assert packer.ack(batch.seq) is True
        consumed.append(batch.seq)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = []
        for i in range(count):
            packer.pack(i)
            while True:
                try:
                    batch = packer.freeze()
                    break
                except FrozenBufferFull:
                    # Backpressure: wait for the consumer to ack a batch.
                    time.sleep(0.001)
            futures.append(pool.submit(consumer, batch))
        for future in futures:
            future.result()

    assert sorted(consumed) == list(range(count))
    assert packer.pending() == []

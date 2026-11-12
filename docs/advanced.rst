Advanced usage
===============

Packer
------

autoreset
~~~~~~~~~

When you used ``autoreset=False`` option of :class:`~msgpack.Packer`, 
``pack()`` method doesn't return packed ``bytes``.

You can use :meth:`~msgpack.Packer.bytes` or :meth:`~msgpack.Packer.getbuffer` to
get packed data.

``bytes()`` returns ``bytes`` object.  ``getbuffer()`` returns some bytes-like
object.  It's concrete type is implement detail and it will be changed in future
versions.

You can reduce temporary bytes object by using ``Unpacker.getbuffer()``.

.. code-block:: python

    packer = Packer(use_bin_type=True, autoreset=False)

    packer.pack([1, 2])
    packer.pack([3, 4])

    with open('data.bin', 'wb') as f:
        f.write(packer.getbuffer())

    packer.reset()  # reset internal buffer

Freezing and handing off batches
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A pipeline usually packs on one thread and sends the accumulated bytes on
another. Without a handoff point the packer must either copy the bytes for
every send or wait until the consumer stops reading its internal buffer.
The pure Python :class:`~msgpack.Packer` provides an explicit freeze and
acknowledgement interface for this case (use ``autoreset=False``).

``freeze()`` snapshots the accumulated bytes into an immutable
:class:`~msgpack.FrozenBatch`, clears the internal buffer and returns the
batch. The packing thread can immediately start the next batch; further
``pack()`` calls and ``reset()`` never touch the handed-off bytes. The
consumer can read a batch as many times as needed -- for example to retry a
failed send -- until it is acknowledged.

.. code-block:: python

    packer = Packer(use_bin_type=True, autoreset=False)

    packer.pack([1, 2])
    batch0 = packer.freeze()        # hand batch #0 to the sender thread
    view = batch0.getbuffer()       # readable until released or GC'd

    packer.pack([3, 4])             # the packer already builds batch #1
    batch1 = packer.freeze()

    send(batch0.bytes())            # retry with the same bytes if it fails
    packer.ack(batch0.seq)          # release batch #0 after confirmation
    packer.ack(batch0.seq)          # duplicate or late acks are a no-op

    batch0.bytes()                  # ValueError: batch was acknowledged
    bytes(view)                     # views taken earlier stay valid

Acknowledging an unknown or already acknowledged sequence number is a safe
no-op, so late or duplicate confirmations never affect newer batches.
``pending()`` lists every unacknowledged batch, oldest first, which is useful
for retransmitting everything that has not been confirmed.

Backpressure
^^^^^^^^^^^^

Pass ``max_unacked`` to cap how many frozen batches may remain
unacknowledged. When the cap is reached ``freeze()`` raises
:class:`~msgpack.FrozenBufferFull` without touching the packed bytes, so the
packing thread keeps its current
batch and simply retries the freeze after an acknowledgement arrives. This
bounds both the number of in-flight batches and the memory held by their
snapshots; ``None`` (the default) means unlimited.

.. code-block:: python

    packer = Packer(autoreset=False, max_unacked=4)
    try:
        batch = packer.freeze()
    except FrozenBufferFull:
        wait_for_ack()              # backpressure: sender is behind
        batch = packer.freeze()

This interface is only implemented by the pure Python Packer. The existing
``bytes()`` and ``getbuffer()`` call conventions are unchanged.

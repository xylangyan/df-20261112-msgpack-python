Timestamp extension type
=========================

MessagePack timestamps are extension messages with type code ``-1``.  This
page documents the wire formats, the four unpacking modes, and the points at
which rounding or overflow happens.  It is a characterization of the current
behavior; none of the semantics below change when adding tests or docs.

Wire formats
------------

The payload is 4, 8, or 12 bytes.  All integer fields are big-endian.

* **timestamp32** (``fixext 4``, prefix ``d6 ff``): one unsigned 32-bit
  ``seconds`` field, no fractional part.  Seconds range from ``0`` to
  ``2**32 - 1``.
* **timestamp64** (``fixext 8``, prefix ``d7 ff``): a 30-bit
  ``nanoseconds`` field followed by a 34-bit unsigned ``seconds`` field
  (``data64 = (nanoseconds << 34) | seconds``).  Seconds range from ``0``
  to ``2**34 - 1`` and nanoseconds from ``0`` to ``999_999_999``.
* **timestamp96** (``ext 8`` with length 12, prefix ``c7 0c ff``): an
  unsigned 32-bit ``nanoseconds`` field followed by a signed 64-bit
  ``seconds`` field.  This is the only format that represents negative
  seconds (times before 1970-01-01 UTC).

A nanosecond field equal to or above ``10**9`` is invalid regardless of
width and is rejected with ``ValueError`` while parsing the timestamp,
before any mode-specific conversion runs.

Encoding width selection
~~~~~~~~~~~~~~~~~~~~~~~~

``Timestamp.to_bytes`` and the C packer apply the same rule:

1. If ``seconds`` is non-negative and fits in 34 bits, pack timestamp64.
2. If that 64-bit value has its high 32 bits clear, meaning
   ``nanoseconds == 0`` and ``seconds < 2**32``, use timestamp32 instead.
3. Otherwise (negative ``seconds`` or ``seconds >= 2**34``), use
   timestamp96.

In particular, any time before the epoch uses timestamp96 even when its
nanoseconds are zero, because its seconds are negative.

Unpacking modes
---------------

The ``timestamp`` argument of :func:`~msgpack.unpackb` and
:class:`~msgpack.Unpacker` selects the Python type produced:

* ``timestamp=0`` (default): :class:`~msgpack.Timestamp` with separate
  ``seconds`` and ``nanoseconds`` integers.  Lossless.
* ``timestamp=1``: Python ``float`` seconds.  The fields are converted in
  two stages, ``seconds + nanoseconds / 1e9``, and each stage rounds to the
  nearest IEEE-754 double.  Not lossless.
* ``timestamp=2``: Python ``int`` nanoseconds,
  ``seconds * 10**9 + nanoseconds``.  Lossless across the full int64
  seconds range.
* ``timestamp=3``: timezone-aware ``datetime.datetime`` in UTC.
  ``datetime`` only stores microseconds, so the nanoseconds field is
  truncated with ``nanoseconds // 1000`` (floor) when building the
  ``timedelta``.  Values outside ``datetime.min`` / ``datetime.max`` raise
  ``OverflowError`` at that conversion stage.

Packing datetimes
-----------------

``packb(dt, datetime=True)`` and ``Packer(datetime=True)`` only accept
timezone-aware ``datetime`` values; a naive datetime raises
``ValueError`` (unless a ``default`` hook handles it first).  The offset is
normalized to UTC before encoding and is not stored in the timestamp, so
``timestamp=3`` always returns UTC.  Fractional seconds are taken from
``datetime.microsecond``, giving nanoseconds that are always multiples of
``1000``.  The pure-Python and C paths use integer timedelta arithmetic, so
no float is involved when packing.

Rounding and overflow stages
----------------------------

* **Invalid nanosecond field** -> ``ValueError`` during timestamp parsing,
  before mode conversion.
* **Float mode (mode 1)** -> rounding when ``nanoseconds / 1e9`` becomes a
  double and again when it is added to ``seconds``.  The final seconds
  addition can round a fractional value up to a whole second.
* **Datetime mode (mode 3) nanoseconds** -> truncation to microseconds at
  the ``nanoseconds // 1000`` stage; up to 999 nanoseconds of information
  is dropped.
* **Datetime mode (mode 3) range** -> ``OverflowError`` when the resulting
  instant lies before ``datetime.min`` or after ``datetime.max``.  In the C
  implementation the days value is also bounded by C ``int``, and values
  near the int64 seconds limits fail there first.

Boundary samples
----------------

The tables below characterize every boundary sample.  The wire bytes are
independent expectations assembled from the specification, not output of
the encoder under test.  The same samples are exercised by
``test/test_timestamp_boundaries.py``.

.. list-table:: Sample identity
   :header-rows: 1

   * - Sample
     - Raw nanoseconds
     - Width
     - Wire bytes (hex)
   * - timestamp32 upper bound
     - 4294967295000000000
     - 32
     - ``d6ffffffffff``
   * - timestamp32/64 boundary
     - 4294967296000000000
     - 64
     - ``d7ff0000000100000000``
   * - timestamp64 upper bound
     - 17179869183999999999
     - 64
     - ``d7ffee6b27ffffffffff``
   * - timestamp64/96 boundary
     - 17179869184000000000
     - 96
     - ``c70cff000000000000000400000000``
   * - one nanosecond before epoch
     - -1
     - 96
     - ``c70cff3b9ac9ffffffffffffffffff``
   * - datetime.min
     - -62135596800000000000
     - 96
     - ``c70cff00000000fffffff1886e0900``
   * - datetime.max
     - 253402300799999999000
     - 96
     - ``c70cff3b9ac6180000003afff4417f``
   * - one second after datetime.max
     - 253402300800000000000
     - 96
     - ``c70cff000000000000003afff44180``
   * - one second before datetime.min
     - -62135596801000000000
     - 96
     - ``c70cff00000000fffffff1886e08ff``
   * - int64 upper bound
     - 9223372036854775807999999999
     - 96
     - ``c70cff3b9ac9ff7fffffffffffffff``
   * - int64 lower bound
     - -9223372036854775808000000000
     - 96
     - ``c70cff000000008000000000000000``
   * - datetime.max plus 999 ns
     - 253402300799999999999
     - 96
     - ``c70cff3b9ac9ff0000003afff4417f``
   * - datetime.min plus 1 ns
     - -62135596799999999999
     - 96
     - ``c70cff00000001fffffff1886e0900``
   * - float precision loss
     - 9007199254741123456789
     - 96
     - ``c70cff075bcd150000083126e978d5``

.. list-table:: Mode results per sample
   :header-rows: 1

   * - Sample
     - timestamp=0 (Timestamp)
     - timestamp=1 (float)
     - timestamp=2 (int ns)
     - timestamp=3 (datetime UTC)
   * - timestamp32 upper bound
     - Timestamp(4294967295, 0)
     - 4294967295.0
     - 4294967295000000000
     - 2106-02-07 06:28:15
   * - timestamp32/64 boundary
     - Timestamp(4294967296, 0)
     - 4294967296.0
     - 4294967296000000000
     - 2106-02-07 06:28:16
   * - timestamp64 upper bound
     - Timestamp(17179869183, 999999999)
     - 17179869184.0 (float rounds up)
     - 17179869183999999999
     - 2514-05-30 01:53:03.999999
   * - timestamp64/96 boundary
     - Timestamp(17179869184, 0)
     - 17179869184.0
     - 17179869184000000000
     - 2514-05-30 01:53:04
   * - one nanosecond before epoch
     - Timestamp(-1, 999999999)
     - -9.999999717180685e-10 (float stage)
     - -1
     - 1969-12-31 23:59:59.999999
   * - datetime.min
     - Timestamp(-62135596800, 0)
     - -62135596800.0
     - -62135596800000000000
     - 0001-01-01 00:00:00
   * - datetime.max
     - Timestamp(253402300799, 999999000)
     - 253402300800.0 (float rounds up)
     - 253402300799999999000
     - 9999-12-31 23:59:59.999999
   * - one second after datetime.max
     - Timestamp(253402300800, 0)
     - 253402300800.0
     - 253402300800000000000
     - OverflowError (datetime range)
   * - one second before datetime.min
     - Timestamp(-62135596801, 0)
     - -62135596801.0
     - -62135596801000000000
     - OverflowError (datetime range)
   * - int64 upper bound
     - Timestamp(9223372036854775807, 999999999)
     - 9.223372036854776e+18
     - 9223372036854775807999999999
     - OverflowError (days -> C int)
   * - int64 lower bound
     - Timestamp(-9223372036854775808, 0)
     - -9.223372036854776e+18
     - -9223372036854775808000000000
     - OverflowError (days -> C int)
   * - datetime.max plus 999 ns
     - Timestamp(253402300799, 999999999)
     - 253402300800.0 (float rounds up)
     - 253402300799999999999
     - 9999-12-31 23:59:59.999999 (truncates 999 ns)
   * - datetime.min plus 1 ns
     - Timestamp(-62135596800, 1)
     - -62135596800.0 (float loses 1 ns)
     - -62135596799999999999
     - 0001-01-01 00:00:00 (truncates 1 ns)
   * - float precision loss
     - Timestamp(9007199254741, 123456789)
     - 9007199254741.123 (loses ~385k ns)
     - 9007199254741123456789
     - OverflowError (datetime range)

Reading mode recommendations
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* Choose mode ``0`` or mode ``2`` when reconciliation needs exact
  nanoseconds; both survive the full int64 seconds range.
* Avoid mode ``1`` for values near the epoch with sub-microsecond parts
  (the -1 ns case) and for large seconds where double precision cannot hold
  nanoseconds.
* Use mode ``3`` only for human-facing UTC values inside
  ``datetime.min`` / ``datetime.max`` and accept microsecond truncation.

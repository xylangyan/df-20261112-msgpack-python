"""Application Decimal extension support for the pure-Python MessagePack code.

The ext code is chosen by the caller; the payload itself is versioned:

    magic      3 bytes  b"MPD"
    version    1 byte   DECIMAL_EXT_VERSION (currently 1)
    kind       1 byte   0=finite, 1=Infinity, 2=NaN, 3=sNaN
    sign       1 byte   0 or 1
    exponent   8 bytes  signed big-endian base-10 exponent (finite only)
    digits     N bytes  ASCII coefficient without sign (finite only)

Finite values keep sign, exponent, and the full unscaled coefficient, so the
exact :class:`decimal.Decimal` tuple is retained: ``1.00`` and ``1`` stay
distinct, negative zero keeps its sign, and long coefficients are not rounded.
Non-finite values (``Infinity``, ``NaN``, ``sNaN``, with sign) are encoded
explicitly and round-trip as Decimal; they are never silently mapped to floats.

Legacy messages that carry decimals as plain strings are not converted here:
they unpack as ``str`` exactly as before, and the application decides whether
to migrate them (for example with ``Decimal(value)``).
"""

from decimal import Decimal, DecimalException

from .ext import ExtType
from .fallback import Packer as _PurePythonPacker
from .fallback import Unpacker as _PurePythonUnpacker
from .fallback import unpackb as _pure_python_unpackb

__all__ = [
    "DECIMAL_EXT_VERSION",
    "DecimalPacker",
    "DecimalUnpacker",
    "decode_decimal",
    "dump",
    "dumps",
    "encode_decimal",
    "load",
    "loads",
    "make_decimal_default",
    "make_decimal_ext_hook",
    "pack",
    "packb",
    "unpack",
    "unpackb",
]

DECIMAL_EXT_VERSION = 1
_DECIMAL_MAGIC = b"MPD"
_FINITE = 0
_INFINITY = 1
_NAN = 2
_SIGNALING_NAN = 3
_HEADER_SIZE = 14


def _check_ext_code(ext_code):
    if not isinstance(ext_code, int) or isinstance(ext_code, bool):
        raise TypeError("ext_code must be an integer")
    if not 0 <= ext_code <= 127:
        raise ValueError("ext_code must be in range 0..127")
    return ext_code


def encode_decimal(value):
    """Encode a Decimal into the versioned ext payload bytes."""

    if not isinstance(value, Decimal):
        raise TypeError("value must be Decimal")

    sign = value.as_tuple().sign
    if value.is_finite():
        coefficient = "".join(str(digit) for digit in value.as_tuple().digits)
        return (
            _DECIMAL_MAGIC
            + bytes((DECIMAL_EXT_VERSION, _FINITE, sign))
            + value.as_tuple().exponent.to_bytes(8, "big", signed=True)
            + coefficient.encode("ascii")
        )

    kind = _INFINITY
    if value.is_nan():
        kind = _SIGNALING_NAN if value.is_snan() else _NAN
    return _DECIMAL_MAGIC + bytes((DECIMAL_EXT_VERSION, kind, sign))


def decode_decimal(data):
    """Decode the versioned ext payload bytes back into a Decimal."""

    if len(data) < 6 or data[:3] != _DECIMAL_MAGIC:
        raise ValueError("invalid Decimal extension payload")
    if data[3] != DECIMAL_EXT_VERSION:
        raise ValueError("unsupported Decimal extension payload version")

    kind = data[4]
    sign = data[5]
    if sign not in (0, 1):
        raise ValueError("invalid Decimal sign")

    if kind in (_INFINITY, _NAN, _SIGNALING_NAN):
        if len(data) != 6:
            raise ValueError("non-finite Decimal payload has trailing data")
        text = ("-" if sign else "") + {
            _INFINITY: "Infinity",
            _NAN: "NaN",
            _SIGNALING_NAN: "sNaN",
        }[kind]
        return Decimal(text)

    if kind != _FINITE:
        raise ValueError("unknown Decimal payload kind")
    if len(data) < _HEADER_SIZE:
        raise ValueError("truncated Decimal payload")

    exponent = int.from_bytes(data[6:14], "big", signed=True)
    try:
        coefficient_text = data[14:].decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("invalid Decimal coefficient") from None
    if not coefficient_text or not coefficient_text.isdigit():
        raise ValueError("invalid Decimal coefficient")
    if len(coefficient_text) > 1 and coefficient_text[0] == "0":
        raise ValueError("Decimal coefficient has leading zeroes")

    try:
        return Decimal((sign, tuple(int(digit) for digit in coefficient_text), exponent))
    except (DecimalException, OverflowError, ValueError):
        raise ValueError("invalid Decimal payload") from None


def make_decimal_default(ext_code, default=None):
    """Return a Packer ``default`` callable for the Decimal convention.

    *ext_code* is the mutually agreed application ext code.  *default* is called
    for values other than Decimal, allowing application default hooks to remain
    composable.
    """

    _check_ext_code(ext_code)
    if default is not None and not callable(default):
        raise TypeError("default must be callable")

    def decimal_default(obj):
        if isinstance(obj, Decimal):
            return ExtType(ext_code, encode_decimal(obj))
        if default is not None:
            return default(obj)
        raise TypeError(f"Cannot serialize {obj!r}")

    return decimal_default


def make_decimal_ext_hook(ext_code, ext_hook=ExtType):
    """Return an Unpacker ``ext_hook`` callable for the Decimal convention.

    Only payloads carrying the selected *ext_code* are decoded as Decimal.
    Every other ext code, including the timestamp code, is passed to *ext_hook*.
    """

    _check_ext_code(ext_code)
    if not callable(ext_hook):
        raise TypeError("ext_hook must be callable")

    def decimal_ext_hook(code, data):
        if code == ext_code:
            return decode_decimal(data)
        return ext_hook(code, data)

    return decimal_ext_hook


class DecimalPacker(_PurePythonPacker):
    """Pure-Python Packer with the application Decimal extension enabled."""

    def __init__(self, ext_code, *, default=None, **kwargs):
        super().__init__(default=make_decimal_default(ext_code, default), **kwargs)


class DecimalUnpacker(_PurePythonUnpacker):
    """Pure-Python Unpacker with the application Decimal extension enabled."""

    def __init__(self, file_like=None, *, ext_code, ext_hook=ExtType, **kwargs):
        super().__init__(
            file_like,
            ext_hook=make_decimal_ext_hook(ext_code, ext_hook),
            **kwargs,
        )


def packb(obj, ext_code, *, default=None, **kwargs):
    """Pack *obj* using the pure-Python Decimal extension convention."""

    return DecimalPacker(ext_code, default=default, **kwargs).pack(obj)


def unpackb(packed, ext_code, *, ext_hook=ExtType, **kwargs):
    """Unpack *packed* using the pure-Python Decimal extension convention."""

    return _pure_python_unpackb(
        packed,
        ext_hook=make_decimal_ext_hook(ext_code, ext_hook),
        **kwargs,
    )


def pack(obj, stream, ext_code, *, default=None, **kwargs):
    """Pack *obj* and write it to *stream* using the Decimal convention."""

    stream.write(packb(obj, ext_code, default=default, **kwargs))


def unpack(stream, ext_code, *, ext_hook=ExtType, **kwargs):
    """Unpack one object from *stream* using the Decimal convention."""

    return unpackb(stream.read(), ext_code, ext_hook=ext_hook, **kwargs)


dumps = packb
loads = unpackb
dump = pack
load = unpack

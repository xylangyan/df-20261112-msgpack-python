"""Optional string-table codec built on top of MessagePack extension types.

Many records in a data partition repeat the same strings (for example a device
type column).  This module lets a partition publish an explicit string table
once and then encodes every repeated string as a compact MessagePack extension
object whose payload is the table index.

The feature is opt-in: the standard :class:`~msgpack.Packer` and
:class:`~msgpack.Unpacker` behavior is unchanged.  The helpers here are thin
wrappers over them and work with both the C extension and the pure-Python
fallback: the packer rewrites table strings into :class:`~msgpack.ExtType`
objects before delegating the actual encoding, while the unpacker installs an
``ext_hook`` that resolves the reference code.  Nested containers, binary
values, timestamps and every other MessagePack type still go through the
existing pack/unpack logic untouched.

Example::

    from msgpack.strtable import StringTablePacker, StringTableUnpacker

    table = ["sensor-A", "sensor-B"]
    packer = StringTablePacker(table)
    packed = packer.pack({"device": "sensor-A", "value": 1})

    unpacker = StringTableUnpacker(table)
    assert unpacker.unpackb(packed) == {"device": "sensor-A", "value": 1}

String tables are scoped to one codec instance (one data partition).  Two
:class:`StringTableUnpacker` instances never share state, so the same table
index can resolve to different strings in two interleaved decoders and a bad
index never falls back to a previously installed table.
"""

import datetime
import struct

from . import Packer, Unpacker
from .exceptions import ExtraData, OutOfData
from .ext import ExtType, Timestamp

# Mirror the core packer so the optional codec cannot nest deeper than msgpack itself.
_RECURSE_LIMIT = 1024

#: Extension code reserved by the MessagePack spec for the timestamp type.
TIMESTAMP_EXT_CODE = -1

#: Default extension code used for string-table references when the caller
#: does not pick one explicitly.  It belongs to the application-defined range.
DEFAULT_STRING_TABLE_EXT_CODE = 0x40

_UINT_FORMATS = {1: ">B", 2: ">H", 4: ">I", 8: ">Q"}


class StringTableError(ValueError):
    """Base class for errors raised by the string-table codec."""


class StringTableConflictError(StringTableError):
    """The configured extension code collides with another extension type."""


class MissingStringTableError(StringTableError):
    """A string-table reference was read but no table is installed."""


class InvalidStringTableIndexError(StringTableError, IndexError):
    """A string-table reference could not be resolved against the table."""


def _check_ext_code(ext_code):
    if type(ext_code) is not int:
        raise TypeError("ext_code must be an int")
    if ext_code == TIMESTAMP_EXT_CODE:
        raise StringTableConflictError(
            "ext_code %d is reserved for the MessagePack timestamp type" % ext_code
        )
    if not 0 <= ext_code <= 127:
        raise ValueError("ext_code must be in the range 0..127")


def _normalize_table(table):
    if table is None:
        return None
    if isinstance(table, (str, bytes)):
        raise TypeError("table must be a sequence of str, not a single string")
    try:
        entries = list(table)
    except TypeError as exc:
        raise TypeError("table must be a sequence of str") from exc
    normalized = []
    for position, entry in enumerate(entries):
        if not isinstance(entry, str):
            raise TypeError(
                "table entry at index %d is %s, expected str"
                % (position, type(entry).__name__)
            )
        normalized.append(entry)
    return tuple(normalized)


class StringTableRef:
    """Opaque marker forcing one value to be encoded as a table reference.

    Plain ``str`` values found in the table are encoded as references
    automatically.  This marker is useful when an index must be written without
    the caller holding the corresponding string, for example when replaying
    rows that were already dictionary-compressed.
    """

    __slots__ = ("index",)

    def __init__(self, index):
        if type(index) is not int:
            raise TypeError("index must be an int")
        if index < 0:
            raise ValueError("index must be non-negative")
        self.index = index

    def __repr__(self):
        return "StringTableRef(index=%d)" % self.index

    def __eq__(self, other):
        if isinstance(other, StringTableRef):
            return self.index == other.index
        return NotImplemented

    def __hash__(self):
        return hash((StringTableRef, self.index))


def _encode_index_payload(index):
    if type(index) is not int or index < 0:
        raise InvalidStringTableIndexError("index must be a non-negative int, got %r" % (index,))
    if index < 0x100:
        return struct.pack(">B", index)
    if index < 0x10000:
        return struct.pack(">H", index)
    if index < 0x100000000:
        return struct.pack(">I", index)
    return struct.pack(">Q", index)


def _decode_index_payload(code, data):
    fmt = _UINT_FORMATS.get(len(data))
    if fmt is None:
        raise InvalidStringTableIndexError(
            "string-table ext code %d carries a %d-byte payload; "
            "expected 1, 2, 4 or 8 bytes" % (code, len(data))
        )
    return struct.unpack(fmt, data)[0]


class StringTablePacker:
    """Packer that encodes table strings as extension references.

    :param table:
        Sequence of strings published for this data partition.  The list
        position is the index written into a reference.
    :param int ext_code:
        Extension code used for references.  Must be in the application range
        ``0..127`` and must not be ``-1`` (the timestamp code), otherwise
        :class:`StringTableConflictError` is raised.
    :param default:
        Optional callback for values that are not natively serializable.  Its
        result is transformed with the same rules, so a ``default`` may return
        a table string (encoded as a reference) or a container of them.
    :param packer_kwargs:
        Remaining keyword arguments are forwarded to :class:`msgpack.Packer`.

    Instances are independent: each packer owns its table and its underlying
    :class:`~msgpack.Packer`.  A packer may not be used re-entrantly.
    """

    def __init__(
        self,
        table,
        *,
        ext_code=DEFAULT_STRING_TABLE_EXT_CODE,
        default=None,
        **packer_kwargs,
    ):
        _check_ext_code(ext_code)
        entries = _normalize_table(table)
        if entries is None:
            raise TypeError("table must be a sequence of str")
        if default is not None and not callable(default):
            raise TypeError("default must be a callable")
        self._table = entries
        self._ext_code = ext_code
        self._index_by_value = {}
        for index, value in enumerate(entries):
            self._index_by_value.setdefault(value, index)
        self._user_default = default
        self._datetime_enabled = bool(packer_kwargs.get("datetime", False))
        self._packing = False
        self._packer = Packer(**packer_kwargs)

    @property
    def table(self):
        """The string table for this partition, as a tuple."""
        return self._table

    @property
    def ext_code(self):
        """Extension code used for references."""
        return self._ext_code

    @property
    def packer(self):
        """The wrapped :class:`msgpack.Packer` (for advanced/streaming use)."""
        return self._packer

    def ref(self, index):
        """Return a marker that forces ``index`` to be encoded as a reference."""
        return StringTableRef(index)

    def _make_ext(self, index):
        if index >= len(self._table):
            raise InvalidStringTableIndexError(
                "string table index %d is out of range for a table with %d entries"
                % (index, len(self._table))
            )
        return ExtType(self._ext_code, _encode_index_payload(index))

    def _transform(self, obj, depth=0):
        if depth > _RECURSE_LIMIT:
            raise ValueError("recursion limit exceeded")
        # ExtType is a namedtuple, so it must be handled before plain tuples.
        if isinstance(obj, Timestamp) or obj is None or isinstance(
            obj, (bool, int, float, bytes, bytearray, memoryview)
        ):
            return obj
        if isinstance(obj, datetime.datetime):
            # Match the core packer: the datetime option handles tz-aware values
            # itself; every other datetime is offered to the user default first.
            if self._datetime_enabled and obj.tzinfo is not None:
                return obj
            if self._user_default is not None:
                return self._transform(self._user_default(obj), depth + 1)
            return obj
        if isinstance(obj, ExtType):
            if obj.code == self._ext_code:
                raise StringTableConflictError(
                    "data contains ExtType with code %d, which is reserved for "
                    "string-table references" % obj.code
                )
            return obj
        if isinstance(obj, str):
            index = self._index_by_value.get(obj)
            if index is not None:
                return self._make_ext(index)
            return obj
        if isinstance(obj, StringTableRef):
            return self._make_ext(obj.index)
        if isinstance(obj, dict):
            return {
                self._transform(key, depth + 1): self._transform(value, depth + 1)
                for key, value in obj.items()
            }
        if isinstance(obj, list):
            return [self._transform(item, depth + 1) for item in obj]
        if isinstance(obj, tuple):
            return tuple(self._transform(item, depth + 1) for item in obj)
        if self._user_default is not None:
            return self._transform(self._user_default(obj), depth + 1)
        raise TypeError(
            "cannot serialize %r: it is not a string-table entry and no default "
            "handler was supplied" % (obj,)
        )

    def pack(self, obj):
        """Pack one object and return its bytes."""
        if self._packing:
            raise RuntimeError("StringTablePacker cannot be used re-entrantly")
        self._packing = True
        try:
            return self._packer.pack(self._transform(obj))
        finally:
            self._packing = False

    def pack_ext(self, index):
        """Return the raw MessagePack bytes for a reference to ``index``."""
        return self._packer.pack(self._make_ext(index))

    def pack_objects(self, objs):
        """Pack several objects into one byte string.

        This is a convenience for writing a whole partition.  The encoding of
        each object is identical to calling :meth:`pack` for it.  It requires
        the wrapped packer to keep its default ``autoreset=True`` setting.
        """
        chunks = []
        for obj in objs:
            chunk = self.pack(obj)
            if chunk is None:
                raise ValueError("pack_objects requires autoreset=True")
            chunks.append(chunk)
        return b"".join(chunks)


class StringTableUnpacker:
    """Unpacker that resolves extension references against a string table.

    :param table:
        Initial string table for this decoder.  Pass ``None`` to create a
        decoder without a table; install one later with :meth:`set_table`
        before reading a reference.
    :param int ext_code:
        Extension code that marks a string-table reference.
    :param ext_hook:
        Optional hook for any other application extension codes.  It never
        receives the configured string-table code, and the timestamp code
        (``-1``) keeps its standard behavior.
    :param bool require_table:
        When true (the default), reading a reference without an installed
        table raises :class:`MissingStringTableError`.  When false, such
        references are handed to ``ext_hook`` (or returned as
        :class:`msgpack.ExtType` when no hook matches) instead.
    :param unpacker_kwargs:
        Remaining keyword arguments are forwarded to :class:`msgpack.Unpacker`.

    Every instance keeps its own table, so two decoders can be driven in an
    interleaved fashion without leaking state into each other.  Installing a
    new partition table replaces the old one entirely; an unresolvable index
    always raises and never silently falls back to an earlier table.
    """

    def __init__(
        self,
        table=None,
        *,
        ext_code=DEFAULT_STRING_TABLE_EXT_CODE,
        ext_hook=None,
        require_table=True,
        **unpacker_kwargs,
    ):
        _check_ext_code(ext_code)
        if ext_hook is not None and not callable(ext_hook):
            raise TypeError("ext_hook must be a callable")
        self._ext_code = ext_code
        self._user_ext_hook = ext_hook
        self._require_table = bool(require_table)
        self._table = _normalize_table(table)
        self._unpacker_file_like = unpacker_kwargs.get("file_like")
        self._unpacker = Unpacker(ext_hook=self._ext_hook, **unpacker_kwargs)

    @property
    def table(self):
        """The currently installed table, or ``None``."""
        return self._table

    @property
    def ext_code(self):
        """Extension code that marks a string-table reference."""
        return self._ext_code

    @property
    def unpacker(self):
        """The wrapped :class:`msgpack.Unpacker` (for advanced/streaming use)."""
        return self._unpacker

    def set_table(self, table):
        """Install the string table for the current partition and return self."""
        entries = _normalize_table(table)
        if entries is None:
            raise TypeError("table must be a sequence of str")
        self._table = entries
        return self

    def clear_table(self):
        """Remove the installed table and return self."""
        self._table = None
        return self

    def _ext_hook(self, code, data):
        if code != self._ext_code:
            if self._user_ext_hook is not None:
                return self._user_ext_hook(code, data)
            return ExtType(code, data)

        index = _decode_index_payload(code, data)
        table = self._table
        if table is None:
            if self._require_table:
                raise MissingStringTableError(
                    "received string-table reference (ext code %d, index %d) but no "
                    "string table is installed for this decoder" % (code, index)
                )
            if self._user_ext_hook is not None:
                return self._user_ext_hook(code, data)
            return ExtType(code, data)
        if index >= len(table):
            raise InvalidStringTableIndexError(
                "string table index %d is out of range for the current table with %d "
                "entries (ext code %d)" % (index, len(table), code)
            )
        return table[index]

    def feed(self, next_bytes):
        """Append bytes to the internal buffer of the wrapped unpacker."""
        return self._unpacker.feed(next_bytes)

    def unpack(self):
        """Unpack one object, resolving references against the current table."""
        return self._unpacker.unpack()

    def skip(self):
        """Read and discard one object without decoding it."""
        return self._unpacker.skip()

    def read_array_header(self):
        """Read an array header from the wrapped unpacker."""
        return self._unpacker.read_array_header()

    def read_map_header(self):
        """Read a map header from the wrapped unpacker."""
        return self._unpacker.read_map_header()

    def read_bytes(self, n):
        """Read ``n`` raw bytes from the wrapped unpacker."""
        return self._unpacker.read_bytes(n)

    def tell(self):
        """Return the byte position of the wrapped unpacker."""
        return self._unpacker.tell()

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._unpacker)

    next = __next__

    def unpackb(self, packed):
        """Unpack a complete byte string, raising on trailing or missing data.

        Only available for buffer-fed unpackers; a decoder created with a
        ``file_like`` object reads from its stream via :meth:`unpack`.
        """
        if self._unpacker_file_like is not None:
            raise ValueError("unpackb cannot be used with a file_like unpacker")
        self.feed(packed)
        try:
            result = self.unpack()
        except OutOfData:
            raise ValueError("Unpack failed: incomplete input")
        position = self.tell()
        if position != len(packed):
            extra = self.read_bytes(len(packed) - position)
            raise ExtraData(result, extra)
        return result


def packb_with_table(obj, table, **kwargs):
    """One-shot helper: pack ``obj`` against ``table`` and return bytes.

    Keyword arguments are the same as :class:`StringTablePacker`.
    """
    return StringTablePacker(table, **kwargs).pack(obj)


def unpackb_with_table(packed, table, **kwargs):
    """One-shot helper: unpack ``packed`` against ``table`` and return the object.

    Keyword arguments are the same as :class:`StringTableUnpacker`.
    """
    unpacker = StringTableUnpacker(table, **kwargs)
    return unpacker.unpackb(packed)

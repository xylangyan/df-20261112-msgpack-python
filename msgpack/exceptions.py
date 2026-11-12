class UnpackException(Exception):
    """Base class for some exceptions raised while unpacking.

    NOTE: unpack may raise exception other than subclass of
    UnpackException.  If you want to catch all error, catch
    Exception instead.
    """


class BufferFull(UnpackException):
    pass


class OutOfData(UnpackException):
    pass


class FormatError(ValueError, UnpackException):
    """Invalid msgpack format"""


class StackError(ValueError, UnpackException):
    """Too nested"""


class IncompleteRecord(OutOfData):
    """A record boundary could not be reached with the bytes fed so far.

    Raised by ``Unpacker.unpack_record()`` when the buffered bytes end in
    the middle of a record.  Nothing is committed: the unpacker position
    stays at the start of the incomplete record, so the caller can persist
    ``unpacker.tell()`` and resume by feeding the remaining bytes later.
    """

    def __init__(self, offset):
        #: Committed stream offset of the first byte of the incomplete
        #: record.  This is the position to resume from.
        self.offset = offset
        #: Alias of :attr:`offset`.
        self.start = offset
        super().__init__(f"incomplete record starting at offset {offset}")


class InvalidRecord(FormatError):
    """The bytes at the committed position are not a valid msgpack record.

    Raised by ``Unpacker.unpack_record()`` when the next record cannot be
    decoded at all.  The unpacker position is left at the start of the
    invalid record so the caller can decide how to recover.
    """

    def __init__(self, offset, reason=None):
        #: Committed stream offset of the first byte of the invalid record.
        self.offset = offset
        #: Alias of :attr:`offset`.
        self.start = offset
        #: Description of why the record could not be decoded, if known.
        self.reason = reason
        message = f"invalid record at offset {offset}"
        if reason:
            message = f"{message}: {reason}"
        super().__init__(message)


# Deprecated.  Use ValueError instead
UnpackValueError = ValueError


class ExtraData(UnpackValueError):
    """ExtraData is raised when there is trailing data.

    This exception is raised while only one-shot (not streaming)
    unpack.
    """

    def __init__(self, unpacked, extra):
        self.unpacked = unpacked
        self.extra = extra

    def __str__(self):
        return "unpack(b) received extra data."


# Deprecated.  Use Exception instead to catch all exception during packing.
PackException = Exception
PackValueError = ValueError
PackOverflowError = OverflowError

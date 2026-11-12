# ruff: noqa: F401
import os

from .exceptions import *  # noqa: F403
from .ext import ExtType, Timestamp

version = (1, 2, 4)
__version__ = "1.2.4"


if os.environ.get("MSGPACK_PUREPYTHON"):
    from .fallback import FrozenBatch, Packer, Unpacker, unpackb
else:
    try:
        from ._cmsgpack import Packer, Unpacker, unpackb
    except ImportError:
        from .fallback import FrozenBatch, Packer, Unpacker, unpackb
    else:
        # freeze()/ack() handoff is only implemented by the pure Python Packer.
        from .fallback import FrozenBatch


def pack(o, stream, **kwargs):
    """
    Pack object `o` and write it to `stream`

    See :class:`Packer` for options.
    """
    packer = Packer(**kwargs)
    stream.write(packer.pack(o))


def packb(o, **kwargs):
    """
    Pack object `o` and return packed bytes

    See :class:`Packer` for options.
    """
    return Packer(**kwargs).pack(o)


def unpack(stream, **kwargs):
    """
    Unpack an object from `stream`.

    Raises `ExtraData` when `stream` contains extra bytes.
    See :class:`Unpacker` for options.
    """
    data = stream.read()
    return unpackb(data, **kwargs)


# alias for compatibility to simplejson/marshal/pickle.
load = unpack
loads = unpackb

dump = pack
dumps = packb

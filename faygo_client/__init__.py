"""Faygo CLI: account access, VPN sessions and installed releases."""
import os
from pathlib import Path
import tempfile


def write_private(path, data, *, owner=None):
    """Durably replace a private file without exposing partial contents."""
    path = Path(path)
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for parent in reversed(missing):
        parent.mkdir(mode=0o700, exist_ok=True)
        if owner:
            os.chown(parent, *owner)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".faygo-")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if owner:
                os.fchown(stream.fileno(), *owner)
            stream.write(data.encode() if isinstance(data, str) else data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)

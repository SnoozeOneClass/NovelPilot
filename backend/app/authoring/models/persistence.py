from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


@contextmanager
def profile_settings_lock(profile_path: Path) -> Iterator[None]:
    """Serialize API and CLI read/modify/write operations for one catalog."""

    path = profile_path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the lock file in place: deleting it can give a waiting writer a
    # different inode from the next writer. The file contains no settings.
    with path.with_name(f".{path.name}.lock").open("a+b") as lock:
        if sys.platform == "win32":
            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if sys.platform == "win32":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def atomic_write(path: Path, encoded: bytes) -> None:
    """Publish one complete document, shared by the catalog and metadata writers."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

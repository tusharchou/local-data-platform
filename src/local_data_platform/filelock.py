"""An advisory file lock for POSIX and Windows, for the processes of one machine.

The lock belongs to an open file, and the operating system gives it up when the file is closed or the process dies, so
a crash never leaves a lock behind. Two handles on one file exclude each other, also inside one process::

    from local_data_platform.filelock import file_lock

    with file_lock("warehouse/.ldp/locks/orders.lock"):
        ...  # one process at a time

    with file_lock("project/.ldp/locks/schedule.lock", blocking=False) as held:
        if not held:
            print("another tick is running")

An Iceberg table takes it around a direct overwrite or upsert, and ``ldp schedule tick`` takes it so that two ticks
never run at once. This module needs only the standard library.
"""

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def lock_file(handle: Any, *, blocking: bool = True) -> bool:
    """Take an exclusive lock on the open file ``handle``.

    Args:
        handle: A file object that is open for writing, or for appending.
        blocking: Wait until the lock is free. With false, return at once.

    Returns:
        True when this process holds the lock, and False when ``blocking`` is false and another handle holds it.
    """
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        import msvcrt

        handle.seek(0)
        if not blocking:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
            return True
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                return True
            except OSError:  # LK_LOCK gives up after about 10 seconds; keep waiting
                continue
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError:
        return False
    return True


def unlock_file(handle: Any) -> None:
    """Give up the lock that :func:`lock_file` took on ``handle``."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def file_lock(path: str | os.PathLike, *, blocking: bool = True) -> Iterator[bool]:
    """Hold an exclusive lock on the file at ``path`` for the length of the block.

    The file and its folders are created when they are missing, and the file stays when the lock is released.

    Args:
        path: The lock file.
        blocking: Wait until the lock is free. With false, the block runs at once, with ``held`` false when another
            handle holds the lock.

    Yields:
        ``held``: True when the block holds the lock, False only when ``blocking`` is false and the lock is taken.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        held = lock_file(handle, blocking=blocking)
        try:
            yield held
        finally:
            if held:
                unlock_file(handle)


__all__ = ["file_lock", "lock_file", "unlock_file"]

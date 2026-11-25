"""Portable durability helpers shared by backup, restore and takeover."""

import os
from pathlib import Path


def fsync_file(path: Path) -> None:
    """Flush file contents to disk without changing them.

    Windows ``FlushFileBuffers`` requires a write-capable handle, so a read-only
    handle fails with ``OSError: [Errno 9] Bad file descriptor``. Reopening with
    ``r+b`` keeps the write-off path working on both Windows and POSIX.
    """
    with path.open('r+b') as stream:
        os.fsync(stream.fileno())

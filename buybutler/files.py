"""Writing the files of the readable copy.

A file is written under a temporary name and renamed into place, so nobody
ever sees half of one. The mode follows the container's UMASK (as a file
written with `open()` would), so with UMASK=002 the group can read the copy
over a share.
"""

from __future__ import annotations

import ctypes
import os
import tempfile
from pathlib import Path


def _mode() -> int:
    # The umask can only be read by setting it; done once, at start.
    umask = os.umask(0o022)
    os.umask(umask)
    return 0o666 & ~umask


MODE = _mode()


def write_if_changed(path: Path, text: str, tmp: Path) -> bool:
    """Write `text` to `path` unless the file says the same already; returns
    whether it was written. `tmp` is a folder on the same file system for
    the temporary file. Not flushed to disk: the caller does that once for
    all files (`sync_tree`), a flush per file writes four times as much."""
    data = text.encode("utf-8")
    try:
        if path.read_bytes() == data:
            return False
    except OSError:
        pass
    fd, name = tempfile.mkstemp(dir=tmp, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(name, MODE)
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return True


def sync_tree(path: Path) -> None:
    """Flush the file system that holds `path` to disk."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            if ctypes.CDLL(None, use_errno=True).syncfs(fd) != 0:
                raise OSError(ctypes.get_errno(), "syncfs failed")
        finally:
            os.close(fd)
    except (OSError, AttributeError):
        os.sync()

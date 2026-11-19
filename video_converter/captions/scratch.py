"""Temporary files the filters read: a folder made on first use, and files named by their content."""

from __future__ import annotations

import atexit
import hashlib
import os
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

_scratch: Optional[Path] = None
_scratch_lock = threading.Lock()


def scratch_dir() -> Path:
    """A folder for the small files the filters read (text, rewritten subtitles, logo copies). It is
    made on first use and removed when the program ends."""
    global _scratch
    with _scratch_lock:
        if _scratch is None or not _scratch.is_dir():
            _scratch = Path(tempfile.mkdtemp(prefix="fve-captions-"))
            atexit.register(shutil.rmtree, _scratch, True)
        return _scratch


def _publish(temp: Path, path: Path, already_there) -> None:
    """Move the finished `temp` file onto `path`. Several threads (the preview and an export) can be making
    the very same file at once: when the move fails but `already_there(path)` says the file is in place
    with the right content, someone else got there first and that is as good. Windows refuses to replace a
    file that another thread has open for a moment, so the move is tried a few times."""
    error: Optional[OSError] = None
    if already_there(path):                 # someone finished while this thread was still writing: leave theirs alone
        return
    for attempt in range(8):
        try:
            os.replace(temp, path)
            return
        except OSError as exc:              # PermissionError on Windows, usually
            error = exc
            if already_there(path):
                return
            time.sleep(0.005 * (attempt + 1))
    if already_there(path):
        return
    assert error is not None
    raise error


def _scratch_file(data: bytes, suffix: str) -> Path:
    """`data` saved under a name made from its content, so the same content is written only once. It is
    written under a name of its own first (other threads may be saving the same content), then moved."""
    path = scratch_dir() / (hashlib.sha1(data).hexdigest()[:20] + suffix)

    def same(target: Path) -> bool:
        try:
            return target.is_file() and target.read_bytes() == data
        except OSError:
            return False

    if not path.is_file():
        temp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}.part")
        try:
            temp.write_bytes(data)
            _publish(temp, path, same)
        finally:
            temp.unlink(missing_ok=True)
    return path

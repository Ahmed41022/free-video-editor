"""A lock between processes, for work that two copies of the app must not do at once (such as downloading the
same speech model into the same folder).

The lock is held by the operating system on an open file, so it is released when the program ends, even when
it crashes: there is never a stale lock to clean up."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class LockTimeout(Exception):
    """The lock stayed taken by another process for the whole wait."""


class FileLock:
    """`with FileLock(folder / "download.lock", timeout=600): ...` runs the block only while no other process (or
    other FileLock on the same file) is inside its own block. While waiting, `cancel` (a threading.Event) can
    end the wait early by raising `LockTimeout`."""

    def __init__(self, path: Path, timeout: Optional[float] = None, cancel: Optional[threading.Event] = None,
                 poll: float = 0.2):
        self.path, self.timeout, self.cancel, self.poll = Path(path), timeout, cancel, poll
        self._file = None

    def _try(self) -> bool:
        try:
            if sys.platform == "win32":
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a+b")
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        while not self._try():
            if (deadline is not None and time.monotonic() >= deadline) or (self.cancel is not None
                                                                            and self.cancel.is_set()):
                self._file.close()
                self._file = None
                raise LockTimeout(str(self.path))
            time.sleep(self.poll)

    def release(self) -> None:
        if self._file is None:
            return
        try:
            if sys.platform == "win32":
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._file.close()
            self._file = None

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()

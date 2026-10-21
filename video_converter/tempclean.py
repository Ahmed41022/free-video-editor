"""Clearing out the temp folders earlier runs left behind.

The editor keeps its previews and thumbnails in a `videoconverter-*` folder of the Windows temp folder and
removes it when the window closes. A crash, or a PC that was switched off, leaves it behind, and so do the
`fve-*` folders exports and captions work in. At start-up the ones older than a day are removed."""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)

PREFIXES = ("videoconverter-", "fve-")
MAX_AGE = 24 * 3600         # seconds a folder must have been left alone before it counts as abandoned
OWNER_FILE = "owner.pid"    # the editor's folder says which process uses it, so a long session's folder is kept


def mark_owner(folder: Path) -> None:
    """Note in `folder` that this process is using it."""
    try:
        (folder / OWNER_FILE).write_text(str(os.getpid()), encoding="ascii")
    except OSError:
        pass


def process_alive(pid: int) -> bool:
    """Whether a process with this id is running."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.restype = wintypes.HANDLE
            handle = kernel32.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return kernel32.GetLastError() == 5                # access denied: it exists, it is just not ours
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return code.value == 259                           # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except (OSError, AttributeError):
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _in_use(folder: Path) -> bool:
    try:
        pid = int((folder / OWNER_FILE).read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return False
    return process_alive(pid)


def _last_touched(folder: Path) -> float:
    """When anything directly in `folder` (or the folder itself) last changed."""
    newest = folder.stat().st_mtime
    try:
        for entry in os.scandir(folder):
            try:
                newest = max(newest, entry.stat().st_mtime)
            except OSError:
                pass
    except OSError:
        pass
    return newest


def sweep_stale(root: Optional[Path] = None, max_age: float = MAX_AGE, now: Optional[float] = None,
                keep: Iterable[Path] = ()) -> list[Path]:
    """Delete the editor's leftover folders in `root` (the temp folder) that are older than `max_age` seconds and
    that no running copy uses. Best effort: whatever can't be deleted is left. Returns the folders removed."""
    root = Path(root) if root is not None else Path(tempfile.gettempdir())
    now = time.time() if now is None else now
    keep_set = {Path(k) for k in keep}
    removed: list[Path] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return removed
    for entry in entries:
        try:
            if not entry.name.startswith(PREFIXES) or not entry.is_dir(follow_symlinks=False):
                continue
            folder = Path(entry.path)
            if folder in keep_set or now - _last_touched(folder) < max_age or _in_use(folder):
                continue
            shutil.rmtree(folder, ignore_errors=True)
            if not folder.exists():
                removed.append(folder)
                log.info("Removed the old temp folder %s", folder)
        except OSError:
            continue
    return removed


def sweep_in_background(keep: Iterable[Path] = ()) -> threading.Thread:
    """`sweep_stale` on a thread of its own, so start-up doesn't wait for it."""
    thread = threading.Thread(target=lambda: sweep_stale(keep=list(keep)), name="temp-sweep", daemon=True)
    thread.start()
    return thread

"""Starting and stopping ffmpeg and anything it starts."""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Optional

from ..i18n import tr
from .errors import ConversionError
from .winproc import _NO_WINDOW, ProcessJob

log = logging.getLogger(__name__)


def start_process(cmd: list[str], **kwargs) -> subprocess.Popen:
    """`subprocess.Popen(cmd, **kwargs)`, with the process put in its own job on Windows (see
    ProcessJob). A program that can't be started (not there, or a command line longer than Windows
    allows) is a ConversionError with a message to show."""
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        log.error("Couldn't start %s: %s", cmd[0] if cmd else "?", exc)
        if getattr(exc, "winerror", None) == 206 or len(subprocess.list2cmdline(cmd)) > 32000:
            raise ConversionError(
                tr(
                    "This export is too complicated to start (too many cuts or texts at once). "
                    "Try fewer, or export in parts.")) from exc
        raise ConversionError(tr("Couldn't start ffmpeg: {error}", error=exc)) from exc
    job = ProcessJob.create()
    if job is not None and not job.add(proc):
        job.close()
        job = None
    proc.fve_job = job  # type: ignore[attr-defined]
    return proc


def stop_process(proc: subprocess.Popen) -> None:
    """Stop `proc` and anything it started. An ffmpeg found through a launcher (a Chocolatey or
    Scoop shim, say) is only the launcher: ending it alone leaves the real ffmpeg running with its
    files open. A process started with `start_process` is in a job, which ends all of them at once;
    any other one on Windows has its whole tree ended with taskkill."""
    job: Optional[ProcessJob] = getattr(proc, "fve_job", None)
    if job is not None and job.terminate():
        try:
            proc.wait(timeout=1.0)
            if job.wait_until_empty(timeout=2.0):
                return
        except subprocess.TimeoutExpired:
            pass  # something got out of the job: do it the slow way
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
    if proc.poll() is None:
        proc.kill()

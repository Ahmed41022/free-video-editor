"""Starting and stopping ffmpeg and anything it starts."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from typing import Optional

from ..i18n import tr
from .errors import ConversionError
from .winproc import _NO_WINDOW, ProcessJob, resume_processes, suspend_processes

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


def _descendants(pid: int) -> list[int]:
    """The ids of everything `pid` started, and what those started (Linux; elsewhere none)."""
    parents: dict[int, list[int]] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", encoding="utf-8", errors="replace") as stat:
                # "pid (name) state ppid ...": the name may hold spaces and brackets
                parent = int(stat.read().rpartition(")")[2].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        parents.setdefault(parent, []).append(int(name))
    found, queue = [], [pid]
    while queue:
        for child in parents.get(queue.pop(), []):
            found.append(child)
            queue.append(child)
    return found


def pause_process(proc: subprocess.Popen) -> bool:
    """Freeze `proc` and anything it started, so that it uses no processor time and writes
    nothing, and `resume_process` carries on from the same point. On Windows every process in
    the job is suspended (an ffmpeg behind a launcher or shim is a different process from the one
    started); elsewhere SIGSTOP goes to the process and what it started. A frozen process can
    still be ended with `stop_process`. Returns whether it is frozen."""
    if proc.poll() is not None:
        return False
    if getattr(proc, "fve_frozen", None) is not None:
        return True
    job: Optional[ProcessJob] = getattr(proc, "fve_job", None)
    if job is not None and job.suspend():
        proc.fve_frozen = job  # type: ignore[attr-defined]
        return True
    if sys.platform == "win32":
        held = suspend_processes([proc.pid])
        proc.fve_frozen = held  # type: ignore[attr-defined]
        return bool(held)
    pids = [proc.pid] + _descendants(proc.pid)
    frozen = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGSTOP)
            frozen.append(pid)
        except OSError:
            pass
    proc.fve_frozen = frozen  # type: ignore[attr-defined]
    return bool(frozen)


def resume_process(proc: subprocess.Popen) -> None:
    """Let a process frozen by `pause_process` run again."""
    frozen = getattr(proc, "fve_frozen", None)
    if frozen is None:
        return
    proc.fve_frozen = None  # type: ignore[attr-defined]
    if isinstance(frozen, ProcessJob):
        frozen.resume()
    elif sys.platform == "win32":
        resume_processes(frozen)
    else:
        for pid in frozen:
            try:
                os.kill(pid, signal.SIGCONT)
            except OSError:
                pass


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
        if sys.platform != "win32":
            for pid in _descendants(proc.pid):  # a launcher's ffmpeg must not be left running
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        proc.kill()

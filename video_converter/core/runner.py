"""Running an ffmpeg command, with progress and cancelling."""

from __future__ import annotations

import logging
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from ..i18n import tr
from .commands import graph_files
from .errors import ConversionCancelled, ConversionError
from .paths import check_not_input, input_files, remove_file
from .processes import (
    pause_process, process_memory, resume_process, start_process, stop_process,
)
from .winproc import _NO_WINDOW, ProcessJob

log = logging.getLogger(__name__)


class PausableEvent(threading.Event):
    """A cancel event for `run_ffmpeg` that can also freeze the run it is given to and let it carry
    on. `pause()` before the run has started its ffmpeg freezes it as soon as it starts."""

    def __init__(self) -> None:
        super().__init__()
        self._guard = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self.paused = False

    def attach(self, proc: Optional[subprocess.Popen]) -> None:
        """Called by `run_ffmpeg` with the ffmpeg it started (and with None when it is over)."""
        with self._guard:
            self._proc = proc
            if proc is not None and self.paused:
                pause_process(proc)

    def pause(self) -> None:
        with self._guard:
            self.paused = True
            if self._proc is not None:
                pause_process(self._proc)

    def memory(self) -> int:
        """The memory in bytes the run's ffmpeg (and what it started) holds; 0 when there is none
        or it can't be measured."""
        with self._guard:
            return process_memory(self._proc) if self._proc is not None else 0

    def resume(self) -> None:
        with self._guard:
            self.paused = False
            if self._proc is not None:
                resume_process(self._proc)


def run_ffmpeg(
    cmd: list[str], output: Path, duration: Optional[float],
    on_progress: Optional[Callable[[float],
                                   None]] = None, cancel_event: Optional[threading.Event] = None,
    cwd: Optional[Path] = None, report: Optional[list[str]] = None,
) -> Path:
    """Run an ffmpeg command that ends with `-progress pipe:1 -nostats <output>`.

    `on_progress` receives a fraction between 0 and 1 (only when the duration
    is known). Setting `cancel_event` stops ffmpeg and removes the partial file.
    `cwd` is ffmpeg's working folder, for filters that read or write files by name.
    `report` collects everything ffmpeg prints on stderr (for filters that print their results).
    A long filter graph is passed in a file (see `GRAPH_FILE_MIN`).
    """
    check_not_input(output, input_files(cmd, cwd))
    output.parent.mkdir(parents=True, exist_ok=True)
    log.info("ffmpeg: %s", subprocess.list2cmdline(cmd))
    with graph_files(cmd) as run_cmd:
        proc = start_process(
            run_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, cwd=cwd,
        )
        job: Optional[ProcessJob] = proc.fve_job  # type: ignore[attr-defined]
        if isinstance(cancel_event, PausableEvent):
            cancel_event.attach(proc)
        try:
            return _watch(proc, output, duration, on_progress, cancel_event, report)
        finally:
            if isinstance(cancel_event, PausableEvent):
                cancel_event.attach(None)
            if job is not None:
                job.close()  # anything still running in it ends here


def _watch(
        proc: subprocess.Popen, output: Path, duration: Optional[float],
        on_progress: Optional[Callable[[float], None]], cancel_event: Optional[threading.Event],
        report: Optional[list[str]]) -> Path:
    """Follow a started ffmpeg to its end: progress, cancelling, and what it printed if it
    failed."""
    # Drain stderr on a separate thread so a chatty ffmpeg can't block on a full pipe.
    errors: deque[str] = deque(maxlen=20)

    def read_errors() -> None:
        for line in proc.stderr:
            errors.append(line)
            if report is not None:
                report.append(line)

    stderr_reader = threading.Thread(target=read_errors, daemon=True)
    stderr_reader.start()

    # ffmpeg only prints progress every so often, and a slow encode can go seconds between lines,
    # so a cancel is acted on by a watcher instead of waiting for the next line. Windows won't
    # delete (or replace) a file ffmpeg still has open, so the stop has to be prompt.
    stopped = threading.Event()
    finished = threading.Event()

    def watch_for_cancel() -> None:
        while not finished.is_set():
            if cancel_event.wait(0.05):
                if proc.poll() is None:
                    stopped.set()
                    stop_process(proc)
                return

    if cancel_event is not None:
        threading.Thread(target=watch_for_cancel, daemon=True).start()

    for line in proc.stdout:
        if stopped.is_set():
            break
        key, _, value = line.strip().partition("=")
        # Despite its name, out_time_ms is in microseconds.
        if key == "out_time_ms" and duration and on_progress and value.isdigit():
            on_progress(min(int(value) / 1_000_000 / duration, 1.0))

    proc.wait()
    finished.set()
    cancelled = stopped.is_set()
    stderr_reader.join(timeout=2)
    proc.stdout.close()
    proc.stderr.close()

    if cancelled:
        remove_file(output)
        log.info("ffmpeg was stopped on request")
        raise ConversionCancelled()
    if proc.returncode != 0:
        remove_file(output)
        message = "".join(errors).strip() or tr(
            "ffmpeg exited with code {code}", code=proc.returncode)
        log.error(
            "ffmpeg exited with code %s. Last lines of its output: %s", proc.returncode, message)
        if "matches no streams" in message:
            message = tr("This file has no audio track.")
        elif "No such filter" in message and "vidstab" in message:
            message = tr(
                "This copy of ffmpeg can't stabilize video (it was built without libvidstab).")
        raise ConversionError(message)

    if on_progress:
        on_progress(1.0)
    return output

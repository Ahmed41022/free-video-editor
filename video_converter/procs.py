"""Running helper programs (ffmpeg) so they can be stopped: a registry that remembers the ones still
running, a runner with a time limit, and a small pool of threads that never keeps the app from exiting.

Closing the editor has to end the ffmpeg runs that read thumbnails, waveforms and file details, or the
process lingers until they finish (which can take minutes on a big file on a slow drive)."""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
from concurrent.futures import Future
from typing import Any, Callable, Optional

from .core import _NO_WINDOW, ConversionCancelled, start_process, stop_process

log = logging.getLogger(__name__)


class ProcessRegistry:
    """Remembers the programs that are running so that `kill_all` can end them. After `kill_all(close=True)`
    nothing new is allowed to start."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen] = set()
        self.closed = False

    def add(self, proc: subprocess.Popen) -> bool:
        """Remember `proc`. Returns False (after ending it) when the registry is already closed."""
        with self._lock:
            if self.closed:
                stop = True
            else:
                self._procs.add(proc)
                stop = False
        if stop:
            stop_process(proc)
        return not stop

    def discard(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)

    def running(self) -> list[subprocess.Popen]:
        with self._lock:
            return [p for p in self._procs if p.poll() is None]

    def kill_all(self, close: bool = True) -> None:
        with self._lock:
            if close:
                self.closed = True
            procs = list(self._procs)
        for proc in procs:
            try:
                stop_process(proc)
            except Exception:      # a program that is already gone is fine
                log.debug("Couldn't stop %s", proc, exc_info=True)


def run_tracked(cmd: list[str], timeout: Optional[float], registry: Optional[ProcessRegistry] = None,
                text: bool = False) -> subprocess.CompletedProcess:
    """Like `subprocess.run(cmd, capture_output=True)` with no window and no input, but the program is
    registered while it runs. Raises `subprocess.TimeoutExpired` (after ending the program and everything
    it started) when it takes longer than `timeout` seconds, and `ConversionCancelled` when the registry ended it."""
    kwargs: dict[str, Any] = {"text": True, "encoding": "utf-8", "errors": "replace"} if text else {}
    # start_process puts the program in a job (Windows), so ending it ends everything it started at once, and the
    # system ends it if this app dies.
    proc = start_process(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         creationflags=_NO_WINDOW, **kwargs)
    job = getattr(proc, "fve_job", None)
    try:
        if registry is not None and not registry.add(proc):
            proc.communicate()
            raise ConversionCancelled()
        try:
            out, err = proc.communicate(timeout=timeout)
        except BaseException:
            stop_process(proc)
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            raise
        finally:
            if registry is not None:
                registry.discard(proc)
    finally:
        if job is not None:
            job.close()
    if registry is not None and registry.closed and proc.returncode != 0:
        raise ConversionCancelled()
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


class DaemonPool:
    """A few worker threads that run submitted jobs in order. Unlike `ThreadPoolExecutor`, the threads are
    daemons, so a job still running when the app exits never holds the exit up.

    `submit` and `shutdown(wait, cancel_futures)` work like the executor's."""

    def __init__(self, workers: int = 2, name: str = "worker"):
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._closed = False
        self._threads = [threading.Thread(target=self._work, name=f"{name}-{n}", daemon=True)
                         for n in range(workers)]
        for thread in self._threads:
            thread.start()

    def submit(self, fn: Callable, *args: Any) -> Future:
        future: Future = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("cannot schedule new futures after shutdown")
            self._jobs.put((future, fn, args))
        return future

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            future, fn, args = job
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn(*args))
            except BaseException as exc:      # a job's failure belongs to its future, not to the thread
                future.set_exception(exc)

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        with self._lock:
            self._closed = True
            if cancel_futures:
                while True:
                    try:
                        job = self._jobs.get_nowait()
                    except queue.Empty:
                        break
                    if job is not None:
                        job[0].cancel()
            for _ in self._threads:
                self._jobs.put(None)
        if wait:
            for thread in self._threads:
                thread.join()

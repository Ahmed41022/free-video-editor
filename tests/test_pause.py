"""Pausing and resuming an ffmpeg run (phase 3): a frozen process uses no processor time and writes
nothing, carries on from the same point, and can still be stopped, also when ffmpeg is behind a
launcher. These run real ffmpeg processes on Windows (every process in the job object is suspended)
and on Linux (SIGSTOP and SIGCONT)."""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from video_converter.core import (
    ConversionCancelled, PausableEvent, find_ffmpeg, pause_process, process_memory, resume_process,
    run_ffmpeg, start_process, stop_process, total_memory,
)
from video_converter.core import processes
from video_converter.core.processes import _descendants

FFMPEG = find_ffmpeg()
TIMEOUT = 40
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4


def cpu_seconds(pids):
    """The processor time (user and system) the processes with these ids have used so far."""
    total = 0.0
    for pid in pids:
        if sys.platform == "win32":
            handle = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                continue
            times = [wintypes.FILETIME() for _ in range(4)]
            if _k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                for t in times[2:]:  # kernel and user, in 100 ns
                    total += ((t.dwHighDateTime << 32) | t.dwLowDateTime) / 1e7
            _k32.CloseHandle(handle)
        else:
            try:
                with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
                    fields = stat.read().rpartition(")")[2].split()
            except OSError:
                continue
            total += (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    return total


def is_alive(pid):
    """Whether a process with this id is still there (a zombie waiting to be collected is not)."""
    if sys.platform == "win32":
        handle = _k32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = wintypes.DWORD()
        _k32.GetExitCodeProcess(handle, ctypes.byref(code))
        _k32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
            return stat.read().rpartition(")")[2].split()[0] != "Z"
    except OSError:
        return False


def started_by(proc):
    """The ids of everything `proc` started that is still running."""
    if getattr(proc, "fve_job", None) is not None:
        return [pid for pid in proc.fve_job.pids() if pid != proc.pid]
    return _descendants(proc.pid)


def family(proc):
    """The process and everything it started (on Windows even a plain ffmpeg may be a small
    launcher in front of the one that encodes)."""
    return [proc.pid] + started_by(proc)


def remove_when_free(path, seconds=10):
    """Delete a file or folder, trying again while Windows still says something has it open: it
    lets go of a file a moment after the process that had it is gone."""
    end = time.monotonic() + seconds
    while True:
        try:
            shutil.rmtree(path) if Path(path).is_dir() else Path(path).unlink()
            return
        except FileNotFoundError:
            return
        except OSError:
            if time.monotonic() > end:
                raise
            time.sleep(0.1)


def slow_encode(output):
    """An ffmpeg command that keeps one or more processors busy for minutes and writes as it
    goes."""
    return [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        "testsrc2=size=1280x720:rate=30:duration=900", "-c:v", "libx264", "-preset", "veryslow",
        "-f", "mpegts",
        str(output)
    ]


def launched_by_python(cmd):
    """The same command, started by a Python process (the way a launcher or shim starts the real
    ffmpeg)."""
    return [
        sys.executable, "-c", "import subprocess, sys; sys.exit(subprocess.call(sys.argv[1:]))"
    ] + cmd


def real_time(output, seconds=2, duration=3):
    """An ffmpeg command that makes `duration` seconds of video no faster than they play (-re), so
    it can be paused part-way, writing progress to stdout like the app's runs do."""
    return [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-re", "-f", "lavfi", "-i",
        f"testsrc2=size=160x90:rate=15:duration={duration}", "-c:v", "libx264", "-preset",
        "ultrafast", "-pix_fmt", "yuv420p", "-progress", "pipe:1", "-nostats",
        str(output)
    ]


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class PauseProcessTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pause-"))
        self.addCleanup(remove_when_free, self.tmp)
        self.procs = []

    def start(self, cmd):
        proc = start_process(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW)
        self.procs.append(proc)
        # Stop it, and wait for it to let go of its files, before the folder is removed.
        self.addCleanup(self.finish, proc)
        return proc

    @staticmethod
    def finish(proc):
        stop_process(proc)
        proc.wait(timeout=TIMEOUT)
        job = getattr(proc, "fve_job", None)
        if job is not None:
            job.close()

    def wait_for(self, condition, what):
        end = time.monotonic() + TIMEOUT
        while time.monotonic() < end:
            if condition():
                return
            time.sleep(0.05)
        self.fail(what)

    def check_frozen_then_running(self, proc, pids, output):
        """The output grows and the processor is used; frozen, neither happens; running again, both
        do."""
        size = lambda: output.stat().st_size if output.exists() else 0  # noqa: E731
        self.wait_for(lambda: size() > 20_000, "ffmpeg never started writing")
        self.assertTrue(pause_process(proc))
        time.sleep(0.3)  # let what was in flight settle
        frozen_size, frozen_cpu = size(), cpu_seconds(pids)
        time.sleep(1.5)
        self.assertEqual(size(), frozen_size, "a frozen ffmpeg kept writing")
        self.assertLess(cpu_seconds(pids) - frozen_cpu, 0.05, "a frozen ffmpeg used the processor")
        self.assertIsNone(proc.poll())
        resume_process(proc)
        self.wait_for(lambda: size() > frozen_size, "ffmpeg did not carry on after resuming")
        self.assertGreater(cpu_seconds(pids) - frozen_cpu, 0.2)

    def test_a_paused_ffmpeg_uses_no_processor_time_and_its_output_stops_growing(self):
        output = self.tmp / "out.ts"
        proc = self.start(slow_encode(output))
        self.wait_for(lambda: output.exists(), "ffmpeg never started")
        self.check_frozen_then_running(proc, family(proc), output)

    def test_pausing_twice_or_resuming_a_running_process_is_harmless(self):
        output = self.tmp / "out.ts"
        proc = self.start(slow_encode(output))
        resume_process(proc)  # not paused: nothing to do
        self.assertTrue(pause_process(proc))
        self.assertTrue(pause_process(proc))
        resume_process(proc)  # one resume is enough, however often it was paused
        resume_process(proc)
        self.wait_for(lambda: output.exists() and output.stat().st_size > 20_000, "no output")
        before = output.stat().st_size
        self.wait_for(lambda: output.stat().st_size > before, "ffmpeg stayed frozen")

    def test_a_process_that_has_ended_cannot_be_paused(self):
        proc = self.start([sys.executable, "-c", "pass"])
        proc.wait(timeout=TIMEOUT)
        self.assertFalse(pause_process(proc))

    def test_every_process_behind_a_launcher_is_paused_too(self):
        output = self.tmp / "out.ts"
        proc = self.start(launched_by_python(slow_encode(output)))
        self.wait_for(lambda: output.exists() and started_by(proc), "the launcher never started")
        self.assertTrue(started_by(proc), "no process behind the launcher")
        self.check_frozen_then_running(proc, family(proc), output)

    def test_the_memory_of_a_paused_ffmpeg_and_of_the_pc_can_be_measured(self):
        output = self.tmp / "out.ts"
        proc = self.start(slow_encode(output))
        self.wait_for(lambda: output.exists() and output.stat().st_size > 20_000, "no output")
        self.assertTrue(pause_process(proc))
        time.sleep(0.3)
        held, total = process_memory(proc), total_memory()
        self.assertGreater(total, 1 << 30, "the memory of the PC should be over a gigabyte")
        self.assertGreater(held, 5_000_000, "a paused ffmpeg holds more than 5 MB")
        self.assertLess(held, total)

    def test_the_memory_behind_a_launcher_is_counted(self):
        output = self.tmp / "out.ts"
        proc = self.start(launched_by_python(slow_encode(output)))
        self.wait_for(lambda: output.exists() and started_by(proc), "the launcher never started")
        self.wait_for(lambda: output.stat().st_size > 20_000, "no output")
        self.assertTrue(pause_process(proc))
        self.assertGreater(process_memory(proc), 5_000_000, "only the launcher was measured")

    def test_a_measurement_that_fails_counts_as_zero_and_is_logged_once(self):
        proc = self.start([sys.executable, "-c", "import time; time.sleep(30)"])
        failing = (
            [
                mock.patch.object(processes, "working_set", side_effect=OSError("no")),
                mock.patch.object(processes, "physical_memory", side_effect=OSError("no"))
            ] if sys.platform == "win32" else
            [mock.patch.object(processes.os, "sysconf", side_effect=OSError("no"))])
        for patcher in failing:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(setattr, processes, "_memory_failed", processes._memory_failed)
        processes._memory_failed = False
        with self.assertLogs("video_converter.core.processes", "WARNING") as logs:
            self.assertEqual(process_memory(proc), 0)
            self.assertEqual(process_memory(proc), 0)
            self.assertEqual(total_memory(), 0)
        self.assertEqual(len(logs.records), 1)

    def test_a_run_with_no_ffmpeg_yet_holds_no_memory(self):
        self.assertEqual(PausableEvent().memory(), 0)

    def test_a_paused_process_can_still_be_stopped(self):
        output = self.tmp / "out.ts"
        proc = self.start(slow_encode(output))
        self.wait_for(lambda: output.exists() and output.stat().st_size > 20_000, "no output")
        pids = family(proc)
        self.assertTrue(pause_process(proc))
        stop_process(proc)
        proc.wait(timeout=TIMEOUT)
        self.assertFalse(any(is_alive(pid) for pid in pids))
        remove_when_free(output)  # Windows refuses while ffmpeg still has the file open

    def test_stopping_a_paused_launcher_leaves_no_ffmpeg_running(self):
        output = self.tmp / "out.ts"
        proc = self.start(launched_by_python(slow_encode(output)))
        self.wait_for(lambda: output.exists() and started_by(proc), "the launcher never started")
        self.wait_for(lambda: output.stat().st_size > 20_000, "no output")
        children = started_by(proc)
        self.assertTrue(children)
        self.assertTrue(pause_process(proc))
        stop_process(proc)
        proc.wait(timeout=TIMEOUT)
        self.wait_for(
            lambda: not any(is_alive(pid) for pid in children), "ffmpeg was left running")
        remove_when_free(output)


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class PausableRunTests(unittest.TestCase):
    """`run_ffmpeg` with an event that can freeze the run."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pause-"))
        self.addCleanup(remove_when_free, self.tmp)
        self.progress = []

    def run_in_background(self, event, output, duration=3):
        result = {}

        def work():
            try:
                result["path"] = run_ffmpeg(
                    real_time(output, duration=duration), output, duration, self.progress.append,
                    event)
            except ConversionCancelled:
                result["cancelled"] = True

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self.addCleanup(thread.join, TIMEOUT)
        self.addCleanup(event.set)
        return thread, result

    def wait_for(self, condition, what):
        end = time.monotonic() + TIMEOUT
        while time.monotonic() < end:
            if condition():
                return
            time.sleep(0.02)
        self.fail(what)

    def test_a_paused_run_stands_still_and_carries_on_from_where_it_was(self):
        event, output = PausableEvent(), self.tmp / "out.mp4"
        thread, result = self.run_in_background(event, output)
        self.wait_for(lambda: self.progress and self.progress[-1] > 0.15, "no progress")
        event.pause()
        time.sleep(0.4)
        seen = len(self.progress)
        value = self.progress[-1]
        time.sleep(1.2)
        self.assertEqual(len(self.progress), seen, "progress kept coming while paused")
        event.resume()
        thread.join(TIMEOUT)
        self.assertEqual(result.get("path"), output)
        self.assertEqual(self.progress[-1], 1.0)
        self.assertGreaterEqual(min(self.progress[seen:]), value, "the run started over")

    def test_a_run_paused_before_ffmpeg_starts_starts_paused(self):
        event, output = PausableEvent(), self.tmp / "out.mp4"
        event.pause()
        thread, result = self.run_in_background(event, output)
        time.sleep(1.5)
        self.assertEqual(self.progress, [])
        self.assertTrue(thread.is_alive())
        event.resume()
        thread.join(TIMEOUT)
        self.assertEqual(result.get("path"), output)

    def test_cancelling_a_paused_run_stops_ffmpeg_and_removes_the_file(self):
        event, output = PausableEvent(), self.tmp / "out.mp4"
        thread, result = self.run_in_background(event, output, duration=60)
        self.wait_for(lambda: self.progress, "no progress")
        event.pause()
        time.sleep(0.3)
        event.set()
        thread.join(TIMEOUT)
        self.assertFalse(thread.is_alive())
        self.assertTrue(result.get("cancelled"))
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

"""Windows process details: the no-window flag and the job object that holds an ffmpeg."""

from __future__ import annotations

import subprocess
import sys
import time
from typing import Optional

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _KILL_ON_JOB_CLOSE = 0x2000
    _BASIC_ACCOUNTING = 1  # JobObjectBasicAccountingInformation
    _EXTENDED_LIMITS = 9  # JobObjectExtendedLimitInformation
    _PID_LIST = 3  # JobObjectBasicProcessIdList
    _PROCESS_SET_QUOTA, _PROCESS_TERMINATE = 0x0100, 0x0001
    _PROCESS_SUSPEND_RESUME = 0x0800
    _PID_SLOTS = 64  # more processes than an ffmpeg and its launchers will ever be

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD)
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)
        ]

    class _Accounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64),
            ("ThisPeriodTotalKernelTime", ctypes.c_int64), ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD), ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD)
        ]

    class _PidList(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t * _PID_SLOTS)
        ]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtSuspendProcess.restype = ctypes.c_long
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long
    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD
    ]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID
    ]
    _k32.QueryInformationJobObject.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL


def suspend_processes(pids: list[int]) -> list[int]:
    """Freeze the processes with these ids. Returns the handles of the ones that are frozen, to
    hand to `resume_processes` (a handle keeps its process from being replaced by another with the
    same id). Empty where this isn't Windows."""
    held: list[int] = []
    if sys.platform != "win32":
        return held
    for pid in pids:
        handle = _k32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, pid)
        if not handle:
            continue
        if _ntdll.NtSuspendProcess(handle) == 0:
            held.append(handle)
        else:
            _k32.CloseHandle(handle)
    return held


def resume_processes(held: list[int]) -> None:
    """Let the processes frozen by `suspend_processes` run again."""
    if sys.platform != "win32":
        return
    for handle in held:
        _ntdll.NtResumeProcess(handle)
        _k32.CloseHandle(handle)


class ProcessJob:
    """A Windows job object that holds an ffmpeg and everything it starts. Ending the job ends them
    all at once (no walking the tree with taskkill, which takes a second or two), and Windows ends
    them too when the last handle to the job closes, so a crash of the app can't leave an ffmpeg
    running."""

    def __init__(self, handle: int):
        self._handle: Optional[int] = handle
        self._frozen: list[int] = []

    @classmethod
    def create(cls) -> Optional["ProcessJob"]:
        """A new job that kills what is in it when it is closed, or None where that isn't
        possible."""
        if sys.platform != "win32":
            return None
        try:
            handle = _k32.CreateJobObjectW(None, None)
            if not handle:
                return None
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
            if not _k32.SetInformationJobObject(handle, _EXTENDED_LIMITS, ctypes.byref(limits),
                                                ctypes.sizeof(limits)):
                _k32.CloseHandle(handle)
                return None
            return cls(handle)
        except (OSError, AttributeError):
            return None

    def add(self, proc: subprocess.Popen) -> bool:
        """Put the running `proc` in the job. False when Windows says no (the app itself may run in
        a job that doesn't allow another)."""
        if self._handle is None:
            return False
        process = _k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, proc.pid)
        if not process:
            return False
        try:
            return bool(_k32.AssignProcessToJobObject(self._handle, process))
        finally:
            _k32.CloseHandle(process)

    def pids(self) -> list[int]:
        """The ids of the processes in the job now: the ffmpeg, and a launcher in front of it."""
        if self._handle is None:
            return []
        info = _PidList()
        if not _k32.QueryInformationJobObject(self._handle, _PID_LIST, ctypes.byref(info),
                                              ctypes.sizeof(info), None):
            return []
        return list(info.ProcessIdList[:info.NumberOfProcessIdsInList])

    def suspend(self) -> bool:
        """Freeze every process in the job (they stay in memory and use no processor time until
        `resume`). False when nothing could be frozen."""
        if self._handle is None:
            return False
        if self._frozen:
            return True
        self._frozen = suspend_processes(self.pids())
        return bool(self._frozen)

    def resume(self) -> None:
        """Let the frozen processes run again."""
        frozen, self._frozen = self._frozen, []
        resume_processes(frozen)

    def terminate(self) -> bool:
        """End everything in the job. True when Windows did it."""
        return self._handle is not None and bool(_k32.TerminateJobObject(self._handle, 1))

    def wait_until_empty(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for every process in the job to be gone; True when they
        are. Ending a job only starts ending its processes, and a real ffmpeg behind a launcher
        keeps its files open until it is gone, after the launcher itself has ended."""
        if self._handle is None:
            return False
        info = _Accounting()
        deadline = time.monotonic() + timeout
        while True:
            if not _k32.QueryInformationJobObject(self._handle, _BASIC_ACCOUNTING,
                                                  ctypes.byref(info), ctypes.sizeof(info), None):
                return False
            if info.ActiveProcesses == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def close(self) -> None:
        self.resume()  # let go of the handles held for the frozen processes
        if self._handle is not None:
            _k32.CloseHandle(self._handle)
            self._handle = None

"""What is running under a process, read from Windows' own process list."""

from __future__ import annotations

import ctypes
import sys
from collections import defaultdict
from collections.abc import Callable, Mapping

# Windows counts a process's start in these: 100 nanoseconds, from 1601.
TICKS_PER_SECOND = 10_000_000
SNAP_PROCESSES = 0x2
QUERY_LIMITED = 0x1000

Created = Callable[[int], int | None]


class ProcessEntry(ctypes.Structure):
    """Windows' PROCESSENTRY32W."""

    _fields_ = (
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    )


def process_parents() -> dict[int, int] | None:
    """Every process's parent, by pid, or None if the list cannot be read."""
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    snapshot = kernel32.CreateToolhelp32Snapshot(SNAP_PROCESSES, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        return None
    parents: dict[int, int] = {}
    entry = ProcessEntry()
    entry.dwSize = ctypes.sizeof(ProcessEntry)
    try:
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            parents[entry.th32ProcessID] = entry.th32ParentProcessID
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return parents or None


def process_created(pid: int) -> int | None:
    """When a process started, in Windows' ticks, or None if it has gone."""
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p] * 5
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(QUERY_LIMITED, 0, pid)
    if not handle:
        return None
    created, ended, kernel, user = (ctypes.c_uint64() for _ in range(4))
    try:
        read = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(ended),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
    finally:
        kernel32.CloseHandle(handle)
    return created.value if read else None


def started_later(
    root: int, parents: Mapping[int, int], created: Created, grace_seconds: float
) -> bool:
    """True if something under `root` started more than `grace_seconds` after it did.

    A pid outlives its process: a child older than its parent belongs to an earlier holder
    of the parent's number, so it and everything under it is left out."""
    root_started = created(root)
    if root_started is None:
        return False
    children: dict[int, list[int]] = defaultdict(list)
    for pid, parent in parents.items():
        children[parent].append(pid)
    limit = root_started + grace_seconds * TICKS_PER_SECOND
    waiting = [(root, root_started)]
    seen = {root}
    while waiting:
        parent, parent_started = waiting.pop()
        for pid in children[parent]:
            started = created(pid)
            if pid in seen or started is None or started < parent_started:
                continue
            if started > limit:
                return True
            seen.add(pid)
            waiting.append((pid, started))
    return False

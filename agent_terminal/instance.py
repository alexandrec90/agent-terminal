"""Keeps one app per user: a second copy would resume every session a second time."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import IO

log = logging.getLogger(__name__)

WINDOW_TITLE = "Agent Terminal"


def lock_file(handle: IO[str]) -> None:
    """Take an exclusive lock, raising `OSError` if another process holds it. The OS
    drops it when the process dies, so a crash leaves nothing to clean up."""
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def acquire_instance_lock(path: Path) -> IO[str] | None:
    """The held lock, to keep open for the life of the app, or None if it is taken."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        lock_file(handle)
    except OSError:
        handle.close()
        return None
    return handle


def focus_running_window() -> bool:
    """Bring the running copy's window forward. False if it cannot be found."""
    if sys.platform != "win32":
        return False
    import ctypes

    user32 = ctypes.windll.user32
    window = user32.FindWindowW(None, WINDOW_TITLE)
    if not window:
        log.info("no window titled %r to focus", WINDOW_TITLE)
        return False
    restore = 9  # SW_RESTORE
    user32.ShowWindow(window, restore)
    return bool(user32.SetForegroundWindow(window))

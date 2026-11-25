"""Starting a process that outlives the app: the session host, and the restart helper."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# No console window, and out of the app's Ctrl+C group.
DETACHED_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
)
# Out of any job the app was started in: a terminal or launcher may put the app in one
# that ends every process in it when the app ends, which is the one thing this must not.
BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)

Popen = Callable[..., Any]


def spawn_detached(argv: list[str], cwd: Path, popen: Popen = subprocess.Popen) -> int:
    """Start `argv` on its own, returning its pid. A job that refuses breakaway refuses
    the start outright, so that is tried first and then left out."""
    options: dict[str, Any] = {
        "cwd": cwd,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    flag_sets = [DETACHED_FLAGS | BREAKAWAY, DETACHED_FLAGS] if BREAKAWAY else [DETACHED_FLAGS]
    error = OSError(f"could not start {argv[:3]}")
    for flags in flag_sets:
        try:
            process = popen(argv, creationflags=flags, **options)
        except OSError as exc:
            log.info("could not start %s with flags %#x: %s", argv[:3], flags, exc)
            error = exc
            continue
        return int(process.pid)
    raise error

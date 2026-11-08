"""One pseudo-console and the thread that reads it."""

from __future__ import annotations

import importlib
import logging
import subprocess
import threading
from collections.abc import Callable, Mapping
from typing import Any, Protocol

log = logging.getLogger(__name__)

READ_CHUNK = 65536


class PtyProcess(Protocol):
    """The part of `winpty.PtyProcess` this module uses."""

    pid: int | None
    exitstatus: int | None

    def read(self, size: int) -> str: ...
    def write(self, text: str) -> Any: ...
    def setwinsize(self, rows: int, cols: int) -> None: ...
    def isalive(self) -> bool: ...


Spawn = Callable[[list[str], str, Mapping[str, str], tuple[int, int]], PtyProcess]


class SpawnError(Exception):
    """A process could not be started: a missing shell, a directory that has gone."""


def conpty_spawn(
    argv: list[str], cwd: str, env: Mapping[str, str], dimensions: tuple[int, int]
) -> PtyProcess:
    """Start `argv` under ConPTY. `winpty` is imported here because it exists on
    Windows alone, and the suite runs on Linux behind a fake `Spawn`."""
    winpty = importlib.import_module("winpty")
    try:
        return winpty.PtyProcess.spawn(argv, cwd=cwd, env=dict(env), dimensions=dimensions)
    except (OSError, winpty.WinptyError) as exc:
        raise SpawnError(str(exc)) from exc


def kill_tree(pid: int) -> None:
    """End a process and everything under it. Ending the shell alone would leave the
    agent running, detached from any terminal."""
    subprocess.run(  # noqa: S603 - fixed system tool, and the pid is one this app spawned
        ["taskkill.exe", "/T", "/F", "/PID", str(pid)],  # noqa: S607 - resolved from System32
        capture_output=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class PtySession:
    """A running process. `on_output` and `on_exit` are called on the reader thread."""

    def __init__(
        self,
        on_output: Callable[[str], None],
        on_exit: Callable[[int | None], None],
        spawn: Spawn = conpty_spawn,
        kill: Callable[[int], None] = kill_tree,
    ) -> None:
        self._on_output = on_output
        self._on_exit = on_exit
        self._spawn = spawn
        self._kill = kill
        self._process: PtyProcess | None = None
        self._closing = False

    def start(
        self, argv: list[str], cwd: str, env: Mapping[str, str], size: tuple[int, int]
    ) -> None:
        """Spawn the process. Raises `SpawnError`, for the caller to show on the tab."""
        self._process = self._spawn(argv, cwd, env, size)
        threading.Thread(target=self._read_loop, name="pty-reader", daemon=True).start()

    def _read_loop(self) -> None:
        process = self._process
        if process is None:
            return
        while True:
            try:
                data = process.read(READ_CHUNK)
            except EOFError:
                break
            except OSError as exc:
                log.warning("pty read failed: %s", exc)
                break
            if data:
                self._on_output(data)
        if not self._closing:
            self._on_exit(process.exitstatus)

    def write(self, text: str) -> None:
        """Send input. Input racing the process's exit is dropped, not an error."""
        if self._process is None:
            return
        try:
            self._process.write(text)
        except (EOFError, OSError) as exc:
            log.info("dropped input to a closed pty: %s", exc)

    def resize(self, rows: int, cols: int) -> None:
        """Resize the pseudo-console, which the process hears as a window change."""
        if self._process is None or not self._process.isalive():
            return
        try:
            self._process.setwinsize(rows, cols)
        except (EOFError, OSError) as exc:
            log.info("resize of a closed pty: %s", exc)

    def close(self) -> None:
        """End the process tree without reporting an exit. Blocks on the kill."""
        self._closing = True
        process, self._process = self._process, None
        if process is not None and process.pid is not None and process.isalive():
            self._kill(process.pid)

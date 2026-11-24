"""A pseudo-console process a test feeds, for the session host and its client."""

from __future__ import annotations

import queue
from typing import Any

TIMEOUT = 5


class FakePty:
    """Output is what the test puts in `reads`; `finish` ends it with a code."""

    def __init__(self, argv: list[str], cwd: str, env: dict[str, str], size: tuple[int, int]):
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.size = size
        self.pid: int | None = 4242
        self.exitstatus: int | None = None
        self.alive = True
        self.written: list[str] = []
        self.sizes: list[tuple[int, int]] = []
        self.reads: queue.Queue[str | Exception] = queue.Queue()

    def read(self, _size: int) -> str:
        item = self.reads.get(timeout=60)
        if isinstance(item, Exception):
            raise item
        return item

    def write(self, text: str) -> None:
        self.written.append(text)

    def setwinsize(self, rows: int, cols: int) -> None:
        self.sizes.append((rows, cols))

    def isalive(self) -> bool:
        return self.alive

    def finish(self, code: int) -> None:
        self.alive = False
        self.exitstatus = code
        self.reads.put(EOFError("closed"))


class Spawner:
    """A `Spawn` that hands out `FakePty`s, and a `Kill` that finishes them."""

    def __init__(self) -> None:
        self.processes: list[FakePty] = []
        self.killed: list[int] = []
        self.refuse: Exception | None = None

    def spawn(self, argv: Any, cwd: str, env: Any, size: tuple[int, int]) -> FakePty:
        if self.refuse is not None:
            raise self.refuse
        process = FakePty(list(argv), cwd, dict(env), size)
        process.pid = 1000 + len(self.processes)
        self.processes.append(process)
        return process

    def kill(self, pid: int) -> None:
        self.killed.append(pid)
        for process in self.processes:
            if process.pid == pid:
                process.finish(1)

    def end_all(self) -> None:
        for process in self.processes:
            if process.alive:
                process.finish(0)

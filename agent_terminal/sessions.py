"""Where a tab's process lives: in this process, or in the session host that outlives it.

`TabRunner` sees only `Session` and `SessionSource`. `LocalSessions` keeps every process in
the app, so closing the window ends them, as the tests and a host that cannot start need.
`host_client.HostSessions` keeps them in the session host, so the window can restart.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent_terminal.pty_session import Kill, PtySession, Spawn, conpty_spawn, kill_tree

OnOutput = Callable[[str], None]
OnExit = Callable[[int | None], None]


class Session(Protocol):
    """One tab's process. `start` and `close` block, so the runner calls them off the
    event loop; the rest return at once."""

    def start(
        self, argv: list[str], cwd: str, env: Mapping[str, str], size: tuple[int, int]
    ) -> None: ...
    def write(self, text: str) -> None: ...
    def resize(self, rows: int, cols: int) -> None: ...
    def remember(self, state: dict[str, Any]) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class Survivor:
    """A tab's process that outlived the window which started it."""

    tab_id: str
    key: str
    alive: bool
    exit_code: int | None = None
    # What the runner last asked to be remembered about the tab: `TabRunner.state_of`.
    state: dict[str, Any] = field(default_factory=dict)
    # The output still held, oldest first: what the page's terminal is rebuilt from.
    screen: str = ""
    # The tail of `screen` the runner had not read when it last remembered.
    unread: str = ""
    # Where `screen` ends in everything the process ever wrote.
    end: int = 0


class SessionSource(Protocol):
    """Makes sessions, and hands back the ones that survived the last window."""

    def new(self, tab_id: str, on_output: OnOutput, on_exit: OnExit) -> Session: ...
    async def survivors(self) -> dict[str, Survivor]: ...
    def adopt(self, survivor: Survivor, on_output: OnOutput, on_exit: OnExit) -> Session: ...
    async def discard(self, survivor: Survivor) -> None: ...
    async def detach(self) -> None: ...
    async def shutdown(self) -> None: ...
    def status(self) -> dict[str, Any]: ...


class LocalSessions:
    """Every process in this one: nothing survives the window."""

    def __init__(self, spawn: Spawn = conpty_spawn, kill: Kill = kill_tree) -> None:
        self._spawn = spawn
        self._kill = kill

    def new(self, tab_id: str, on_output: OnOutput, on_exit: OnExit) -> Session:
        """A process that has not started yet."""
        return PtySession(on_output=on_output, on_exit=on_exit, spawn=self._spawn, kill=self._kill)

    async def survivors(self) -> dict[str, Survivor]:
        """None: a process here ends with the window."""
        return {}

    def adopt(self, survivor: Survivor, on_output: OnOutput, on_exit: OnExit) -> Session:
        """Never called, as `survivors` offers nothing to adopt."""
        raise LookupError(f"no surviving process {survivor.key} here")

    async def discard(self, survivor: Survivor) -> None:
        """Nothing survives here to discard."""

    async def detach(self) -> None:
        """Nothing can be left running: the processes are this process's children."""

    async def shutdown(self) -> None:
        """Nothing beyond the processes themselves, which the runner has closed."""

    def status(self) -> dict[str, Any]:
        """What the page shows about where the processes live."""
        return {"kept": False, "stale": False}

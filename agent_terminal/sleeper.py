"""Puts idle tabs to sleep, and wakes them when the user goes to them.

An agent at its prompt holds about half a gigabyte for as long as its tab is open. A tab
asleep has no process at all: its agent is ended, its screen is left as it was, and its
session is resumed when the tab is next shown.

Claude tabs only: Codex keeps no record of a running process to check first.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from agent_terminal import agents, attention, rest
from agent_terminal.runner import LiveTab, TabRunner
from agent_terminal.settings import Settings

log = logging.getLogger(__name__)

CHECK_SECONDS = 30.0

CanRest = Callable[[Path, str, float], bool]


@dataclass(frozen=True)
class SleeperLinks:
    """What the sleeper asks of the manager."""

    active_id: Callable[[], str | None]
    size: Callable[[], tuple[int, int]]
    changed: Callable[[], None]
    reset: Callable[[str], None]


def is_due(live: LiveTab, now: float, after_seconds: float) -> bool:
    """True if a tab has sat at its prompt long enough, with nothing typed into it."""
    return (
        live.tab.agent == agents.CLAUDE
        and live.tab.session_id is not None
        and live.status == attention.IDLE
        and not live.drafting
        and not live.awaiting_trust
        and now - live.used_at >= after_seconds
    )


class Sleeper:
    """Runs on the event loop. `sweep` is one check of every tab."""

    def __init__(
        self,
        settings: Settings,
        loop: asyncio.AbstractEventLoop,
        runner: TabRunner,
        links: SleeperLinks,
        can_rest: CanRest = rest.can_rest,
    ) -> None:
        self._settings = settings
        self._loop = loop
        self._runner = runner
        self._links = links
        self._can_rest = can_rest
        self._task: asyncio.Task[None] | None = None
        # Processes on their way out: a tab waked meanwhile waits for its old one.
        self._ending: dict[str, asyncio.Future[None]] = {}
        self._waking: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        """Check every so often, unless sleeping is turned off."""
        if self._settings.sleep_after_seconds > 0 and self._task is None:
            self._task = self._loop.create_task(self._run())

    def stop(self) -> None:
        """Put nothing more to sleep."""
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(min(CHECK_SECONDS, self._settings.sleep_after_seconds))
            try:
                await self.sweep()
            except (OSError, RuntimeError):
                # One check the machine refused must not be the last. A bug is raised.
                log.exception("sleep check failed")

    async def sweep(self) -> None:
        """Put to sleep every tab that is due and has nothing left running."""
        after = self._settings.sleep_after_seconds
        for live in list(self._runner.live.values()):
            if live.tab.id == self._links.active_id():
                # On screen is in use, so its time starts when the user leaves it.
                live.used_at = time.monotonic()
                continue
            session_id = live.tab.session_id
            if session_id is None or not is_due(live, time.monotonic(), after):
                continue
            quiet_since = time.time() - after
            home = self._settings.claude_home
            free = await self._loop.run_in_executor(
                None, self._can_rest, home, session_id, quiet_since
            )
            # Again, as the user may have come back to it during the check.
            still = self._runner.live.get(live.tab.id) is live
            on_screen = live.tab.id == self._links.active_id()
            if free and still and not on_screen and is_due(live, time.monotonic(), after):
                await self.sleep(live)

    async def sleep(self, live: LiveTab) -> None:
        """End a tab's process and keep the tab."""
        tab = live.tab
        log.info("putting tab %s to sleep", tab.id)
        tab.asleep = True
        self._runner.add(tab).size = live.size
        ending = self._loop.run_in_executor(None, live.session.close)
        self._ending[tab.id] = ending
        self._links.changed()
        try:
            await asyncio.shield(ending)
        finally:
            if self._ending.get(tab.id) is ending:
                del self._ending[tab.id]

    def wake_active(self) -> None:
        """Resume the tab on screen, if it is asleep. Returns at once."""
        live = self._runner.live.get(self._links.active_id() or "")
        if live is None or not live.tab.asleep:
            return
        live.tab.asleep = False
        live.status = attention.STARTING
        task = self._loop.create_task(self._wake(live, self._ending.get(live.tab.id)))
        self._waking.add(task)
        task.add_done_callback(self._waking.discard)

    async def _wake(self, live: LiveTab, ending: Awaitable[None] | None) -> None:
        if ending is not None:
            # Two processes in one session would both write its transcript.
            await asyncio.wait([asyncio.ensure_future(ending)])
        if self._runner.live.get(live.tab.id) is not live:
            return  # closed meanwhile
        self._links.reset(live.tab.id)
        await self._runner.start(live.tab, self._links.size())

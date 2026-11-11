"""The running half of the tabs: one process per tab, and what it says about itself.

Everything here runs on the server's event loop except `receive`, which runs on a tab's
reader thread so a status query is answered before the agent stops waiting for it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_terminal import agents, attention, trust
from agent_terminal.attention import OscScanner, Presence
from agent_terminal.codex_sessions import CodexSessionFinder
from agent_terminal.pty_session import Kill, PtySession, Spawn, SpawnError, kill_tree
from agent_terminal.settings import Settings
from agent_terminal.store import Tab

log = logging.getLogger(__name__)

DECLINED_NOTE = "\r\nThis folder wasn't trusted, so no worktree was made.\r\n"


@dataclass
class LiveTab:
    """A tab's process, and the status that lasts only as long as it does."""

    tab: Tab
    session: PtySession
    scanner: OscScanner = field(default_factory=OscScanner)
    status: str = attention.STARTING
    started_at: float = field(default_factory=time.time)
    user_engaged: bool = False
    # On the agent's trust screen. Neither agent reports a state until past it.
    awaiting_trust: bool = False
    # Claude is running plain only to ask, and the tab's real start waits on the answer.
    worktree_after_trust: bool = False
    size: tuple[int, int] = (0, 0)


@dataclass(frozen=True)
class RunnerEvents:
    """What the manager hears. All of it arrives on the event loop."""

    output: Callable[[str, str], None]
    changed: Callable[[], None]
    clean_exit: Callable[[str], None]
    # A tab's terminal starts over: a new process, so the old screen means nothing.
    reset: Callable[[str], None]


class TabRunner:
    """Starts and stops processes, and turns their output into tab state."""

    def __init__(
        self,
        settings: Settings,
        loop: asyncio.AbstractEventLoop,
        spawn: Spawn,
        events: RunnerEvents,
        kill: Kill = kill_tree,
    ) -> None:
        self._settings = settings
        self._loop = loop
        self._spawn = spawn
        self._kill = kill
        self._events = events
        self.live: dict[str, LiveTab] = {}
        self._finder = CodexSessionFinder(
            settings.codex_home, loop, self._claimed_sessions, events.changed
        )

    def _claimed_sessions(self) -> set[str]:
        return {live.tab.session_id for live in self.live.values() if live.tab.session_id}

    def add(self, tab: Tab) -> LiveTab:
        """Prepare a tab, so it can be shown before its process is up."""
        holder: list[LiveTab] = []
        session = PtySession(
            on_output=lambda text: self.receive(holder[0], text),
            on_exit=lambda code: self._to_loop(self._exited, holder[0], code),
            spawn=self._spawn,
            kill=self._kill,
        )
        holder.append(LiveTab(tab=tab, session=session))
        self.live[tab.id] = holder[0]
        return holder[0]

    async def start(self, tab: Tab, size: tuple[int, int]) -> None:
        """Start the tab's process. A failure is shown in the tab rather than raised."""
        live = self.live.get(tab.id)
        if live is None:
            return
        live.size = size
        launch = await self._loop.run_in_executor(None, self._first_launch, live)
        await self._run(live, launch)

    def _first_launch(self, live: LiveTab) -> agents.Launch:
        """The tab's start, or the step before it. `claude -w` in a folder not yet trusted
        exits instead of asking, so Claude runs plain to ask and `_reopen` follows."""
        tab = live.tab
        launch = agents.launch_for(tab, self._settings)
        if launch.resumes or trust.is_trusted(self._settings, tab.agent, Path(launch.cwd)):
            return launch
        live.awaiting_trust = True
        if tab.agent == agents.CLAUDE and tab.worktree_name:
            live.worktree_after_trust = True
            return agents.claude_trust_launch(tab, self._settings)
        return launch

    async def _run(self, live: LiveTab, launch: agents.Launch) -> None:
        tab = live.tab
        argv = agents.shell_argv(self._settings.shell, launch.argv)
        env = agents.agent_env(os.environ)
        live.started_at = time.time()
        start = live.session.start
        try:
            await self._loop.run_in_executor(None, start, argv, launch.cwd, env, live.size)
        except SpawnError as exc:
            log.error("could not start tab %s: %s", tab.id, exc)
            self._events.output(tab.id, f"Could not start: {exc}\r\n")
            live.status = attention.EXITED
            self._events.changed()

    async def _reopen(self, live: LiveTab) -> None:
        """Replace a tab's process with its real start, now the folder is trusted. Whatever
        the agent says now is believed: asked again, a `claude -w` that still refuses would
        loop, and its own message is the better thing to leave on screen."""
        tab = live.tab
        fresh = self.add(tab)
        fresh.size = live.size
        await self._loop.run_in_executor(None, live.session.close)
        if self.live.get(tab.id) is not fresh:
            return  # closed during the swap
        self._events.reset(tab.id)
        launch = await self._loop.run_in_executor(None, agents.launch_for, tab, self._settings)
        await self._run(fresh, launch)
        self._events.changed()

    def input(self, tab_id: str, data: str) -> None:
        """Send what the page typed. Raises `KeyError` for a tab that has gone."""
        live = self.live[tab_id]
        live.user_engaged = live.user_engaged or attention.is_user_input(data)
        live.session.write(data)

    async def close(self, tab_id: str) -> None:
        """End a tab's process, if it has one."""
        live = self.live.pop(tab_id, None)
        if live is not None:
            await self._loop.run_in_executor(None, live.session.close)

    async def close_all(self) -> None:
        """End every process."""
        await asyncio.gather(*(self.close(tab_id) for tab_id in list(self.live)))

    async def find_unfound_sessions(self) -> None:
        """Last chance for a Codex tab that never reported a status after its first turn."""
        for live in self.live.values():
            if live.tab.agent == agents.CODEX and not live.tab.session_id:
                await self._finder.search(live.tab, live.started_at)

    def _to_loop(self, callback: Callable[..., None], *args: Any) -> None:
        """Hop to the event loop, which may already have stopped during shutdown."""
        try:
            self._loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:
            log.debug("event loop closed; dropped %s", callback)

    def receive(self, live: LiveTab, text: str) -> None:
        """Take a chunk of output, on the reader thread."""
        payloads = live.scanner.feed(text)
        if attention.QUERY in payloads:
            live.session.write(attention.QUERY_REPLY)
        self._to_loop(self._deliver, live, text, payloads)

    def _deliver(self, live: LiveTab, text: str, payloads: list[str]) -> None:
        if self.live.get(live.tab.id) is not live:
            return
        self._events.output(live.tab.id, text)
        if any([self._apply(live, payload) for payload in payloads]):
            self._events.changed()

    def _apply(self, live: LiveTab, payload: str) -> bool:
        """Take one OSC payload, returning True if the tab changed."""
        tab = live.tab
        before = (live.status, tab.attention, tab.parked, tab.title, live.awaiting_trust)
        state = attention.parse_program_state(payload)
        if state is not None:
            current = Presence(live.status, tab.attention, tab.parked)
            updated = attention.advance(current, state, user_engaged=live.user_engaged)
            live.status = updated.status
            tab.attention = updated.attention
            tab.parked = updated.parked
            if live.awaiting_trust:
                self._past_trust(live, state)
            if tab.agent == agents.CODEX and not tab.session_id:
                self._finder.look_for(tab, live.started_at)
        title = attention.parse_title(payload)
        if title is not None:
            tab.title = attention.clean_title(title, tab.project) or tab.title
        return before != (live.status, tab.attention, tab.parked, tab.title, live.awaiting_trust)

    def _past_trust(self, live: LiveTab, state: str) -> None:
        """The agent's first state answers its trust screen: `clear` if it was declined,
        since the agent has gone, and anything else if it went on."""
        live.awaiting_trust = False
        if not live.worktree_after_trust:
            return
        live.worktree_after_trust = False
        if state == "clear":
            self._events.output(live.tab.id, DECLINED_NOTE)
        else:
            self._loop.create_task(self._reopen(live))

    def _exited(self, live: LiveTab, code: int | None) -> None:
        """A shell the user `exit`ed takes its tab with it, as in Windows Terminal. A
        failure stays open, so whatever it printed can still be read."""
        tab_id = live.tab.id
        if self.live.get(tab_id) is not live:
            return
        if code == 0:
            self._events.clean_exit(tab_id)
            return
        live.status = attention.EXITED
        self._events.changed()

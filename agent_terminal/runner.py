"""The running half of the tabs: one process per tab, and what it says about itself.

Everything here runs on the server's event loop except `receive`, which may run on a
tab's reader thread and hops onto the loop.
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
from agent_terminal.pty_session import SpawnError
from agent_terminal.sessions import Session, SessionSource, Survivor
from agent_terminal.settings import Settings
from agent_terminal.store import Tab

log = logging.getLogger(__name__)

DECLINED_NOTE = "\r\nThis folder wasn't trusted, so no worktree was made.\r\n"
# Between the two resizes of `redraw`: long enough that the process sees two changes.
REDRAW_GAP_SECONDS = 0.15


@dataclass
class LiveTab:
    """A tab's process, and the status that lasts only as long as it does."""

    tab: Tab
    session: Session
    scanner: OscScanner = field(default_factory=OscScanner)
    status: str = attention.STARTING
    started_at: float = field(default_factory=time.time)
    user_engaged: bool = False
    # On the agent's trust screen. Claude reports `blocked` there; a state past it ends it.
    awaiting_trust: bool = False
    # Claude is running plain only to ask, and the tab's real start waits on the answer.
    worktree_after_trust: bool = False
    size: tuple[int, int] = (0, 0)
    # Unsent text in the prompt, which ending the process would lose.
    drafting: bool = False
    # When the user or the agent last did something, on the monotonic clock.
    used_at: float = field(default_factory=time.monotonic)
    # When the usage limit holding the session resets, in epoch seconds (`limits.py`).
    limited_until: float | None = None
    # The limit the tab was last sent back to work after, so it is sent once.
    resumed_for: float | None = None


def state_of(live: LiveTab) -> dict[str, Any]:
    """What a later window needs to carry on with this tab's process."""
    return {
        "status": live.status,
        "started_at": live.started_at,
        "user_engaged": live.user_engaged,
        "awaiting_trust": live.awaiting_trust,
        "worktree_after_trust": live.worktree_after_trust,
        "drafting": live.drafting,
        "rows": live.size[0],
        "cols": live.size[1],
    }


def restore_state(live: LiveTab, state: dict[str, Any]) -> None:
    """Set a tab back to what `state_of` recorded. Anything missing keeps its default."""
    live.status = str(state.get("status", attention.STARTING))
    live.started_at = float(state.get("started_at", live.started_at))
    live.user_engaged = bool(state.get("user_engaged", False))
    live.awaiting_trust = bool(state.get("awaiting_trust", False))
    live.worktree_after_trust = bool(state.get("worktree_after_trust", False))
    live.drafting = bool(state.get("drafting", False))
    live.size = (int(state.get("rows", 0)), int(state.get("cols", 0)))


@dataclass(frozen=True)
class RunnerEvents:
    """What the manager hears. All of it arrives on the event loop."""

    output: Callable[[str, str], None]
    changed: Callable[[], None]
    clean_exit: Callable[[str], None]
    # A tab's terminal starts over: a new process, so the old screen means nothing.
    reset: Callable[[str], None]


class ClaudeTreeFinder:
    """Finds the worktree Claude named for itself, for a tab that asked for one."""

    def __init__(
        self, claude_home: Path, loop: asyncio.AbstractEventLoop, changed: Callable[[], None]
    ) -> None:
        self._claude_home = claude_home
        self._loop = loop
        self._changed = changed
        self._finding: set[str] = set()

    def look_for(self, tab: Tab) -> None:
        """Start a search unless one is running. Claude writes its transcript on the
        first message, so a tab not found now is looked for on its next status change."""
        if tab.id in self._finding or not tab.session_id:
            return
        self._finding.add(tab.id)
        self._loop.create_task(self.search(tab, tab.session_id))

    async def search(self, tab: Tab, session_id: str) -> None:
        """Record the worktree, once the session's transcript says where it is."""
        find = agents.claude_session_tree
        try:
            tree = await self._loop.run_in_executor(None, find, self._claude_home, session_id)
        finally:
            self._finding.discard(tab.id)
        if tree and not tab.cwd:
            tab.cwd = tree
            self._changed()


class TabRunner:
    """Starts and stops processes, and turns their output into tab state."""

    def __init__(
        self,
        settings: Settings,
        loop: asyncio.AbstractEventLoop,
        sessions: SessionSource,
        events: RunnerEvents,
    ) -> None:
        self._settings = settings
        self._loop = loop
        self._sessions = sessions
        self._events = events
        self.live: dict[str, LiveTab] = {}
        self.trees = ClaudeTreeFinder(settings.claude_home, loop, events.changed)
        self._finder = CodexSessionFinder(
            settings.codex_home, loop, self._claimed_sessions, events.changed
        )

    def _claimed_sessions(self) -> set[str]:
        return {live.tab.session_id for live in self.live.values() if live.tab.session_id}

    def add(self, tab: Tab) -> LiveTab:
        """Prepare a tab, so it can be shown before its process is up, or with none: one
        that is asleep."""
        holder: list[LiveTab] = []
        session = self._sessions.new(
            tab.id,
            lambda text: self.receive(holder[0], text),
            lambda code: self._to_loop(self._exited, holder[0], code),
        )
        status = attention.ASLEEP if tab.asleep else attention.STARTING
        holder.append(LiveTab(tab=tab, session=session, status=status))
        self.live[tab.id] = holder[0]
        return holder[0]

    def adopt(self, tab: Tab, survivor: Survivor) -> LiveTab:
        """Take on a process the last window left running. What it reported while no
        window was reading is read now, so the tab is as it would have been."""
        holder: list[LiveTab] = []
        # The source hands over, inside `adopt` itself, what the process said after its
        # screen was read, and its exit if it died while away. Those come before there is
        # a tab to give them to, and after `unread` in time, so they wait for it.
        early: list[Callable[[LiveTab], None]] = []

        def on_output(text: str) -> None:
            if holder:
                self.receive(holder[0], text)
            else:
                early.append(lambda live: self.receive(live, text))

        def on_exit(code: int | None) -> None:
            if holder:
                self._to_loop(self._exited, holder[0], code)
            else:
                early.append(lambda live: self._to_loop(self._exited, live, code))

        session = self._sessions.adopt(survivor, on_output, on_exit)
        live = LiveTab(tab=tab, session=session)
        holder.append(live)
        restore_state(live, survivor.state)
        for payload in live.scanner.feed(survivor.unread):
            self._apply(live, payload)
        self.live[tab.id] = live
        for event in early:
            event(live)
        return live

    def redraw(self, tab_id: str) -> None:
        """Have a process repaint its screen: a pseudo-console redraws on a resize, and a
        rebuilt terminal holds only as much as the host kept."""
        live = self.live.get(tab_id)
        if live is None or live.size == (0, 0):
            return
        rows, cols = live.size
        live.session.resize(rows, max(cols - 1, 1))
        self._loop.call_later(REDRAW_GAP_SECONDS, live.session.resize, rows, cols)

    def remember_all(self) -> None:
        """Leave every tab's state with its process, before the window goes."""
        for live in self.live.values():
            live.session.remember(state_of(live))

    async def start(self, tab: Tab, size: tuple[int, int], prompt: str | None = None) -> None:
        """Start the tab's process. A failure is shown in the tab rather than raised.
        `prompt` is sent to a resumed session as its next message."""
        live = self.live.get(tab.id)
        if live is None:
            return
        live.size = size
        launch = await self._loop.run_in_executor(None, self._first_launch, live, prompt)
        await self._run(live, launch)

    def _first_launch(self, live: LiveTab, prompt: str | None = None) -> agents.Launch:
        """The tab's start, or the step before it. `claude -w` in a folder not yet trusted
        exits instead of asking, so Claude runs plain to ask and `_reopen` follows."""
        tab = live.tab
        launch = agents.launch_for(tab, self._settings, prompt)
        if launch.resumes or trust.is_trusted(self._settings, tab.agent, Path(launch.cwd)):
            return launch
        live.awaiting_trust = True
        if tab.agent == agents.CLAUDE and launch.makes_worktree:
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
        live.drafting = attention.is_drafting(data, live.drafting)
        live.used_at = time.monotonic()
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
        """Take a chunk of output, on the reader thread or the loop. The session has
        already answered any status query in it."""
        payloads = live.scanner.feed(text)
        self._to_loop(self._deliver, live, text, payloads)

    def _deliver(self, live: LiveTab, text: str, payloads: list[str]) -> None:
        if self.live.get(live.tab.id) is not live:
            return
        self._events.output(live.tab.id, text)
        if any([self._apply(live, payload) for payload in payloads]):
            # Kept with the process too, for a window that comes back without a goodbye.
            live.session.remember(state_of(live))
            self._events.changed()

    def _apply(self, live: LiveTab, payload: str) -> bool:
        """Take one OSC payload, returning True if the tab changed."""
        tab = live.tab
        before = (live.status, tab.attention, tab.parked, tab.title, live.awaiting_trust)
        state = attention.parse_program_state(payload)
        if state is not None:
            live.used_at = time.monotonic()
            current = Presence(live.status, tab.attention, tab.parked)
            updated = attention.advance(current, state, user_engaged=live.user_engaged)
            live.status = updated.status
            tab.attention = updated.attention
            tab.parked = updated.parked
            if live.awaiting_trust:
                self._past_trust(live, state)
            if tab.agent == agents.CODEX and not tab.session_id:
                self._finder.look_for(tab, live.started_at)
            if tab.agent == agents.CLAUDE and tab.worktree and not tab.cwd:
                self.trees.look_for(tab)
        title = attention.parse_title(payload)
        if title is not None:
            tab.title = attention.clean_title(title, tab.project) or tab.title
        changed = before != (live.status, tab.attention, tab.parked, tab.title, live.awaiting_trust)
        # Only once the user has typed here: a restored session settling, or Codex saying
        # `working` on every start, is the app's doing and not news.
        if changed and live.user_engaged:
            tab.updated_at = time.time()
        return changed

    def _past_trust(self, live: LiveTab, state: str) -> None:
        """The first state after the trust screen answers it: `clear` if it was declined,
        since the agent has gone, and anything else if it went on. `blocked` is the
        screen itself, which Claude Code 2.1.332 reports as
        `state=blocked:kind=permission:msg=<"trust this folder">` while it waits."""
        if state == attention.BLOCKED:
            return
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

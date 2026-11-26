"""The tabs as records, and every change the page can ask for. Runs on the event loop."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from agent_terminal import agents, attention, folders, gitinfo
from agent_terminal.desktop import Desktop, DesktopRequests, Emit, Handler
from agent_terminal.groups import Groups, open_folders, successor
from agent_terminal.history import HistoryLinks, SessionHistory
from agent_terminal.land_requests import LandRequests
from agent_terminal.ordering import Ordering
from agent_terminal.pr_board import BoardLinks, PrBoard
from agent_terminal.pr_requests import PrRequests
from agent_terminal.project_requests import ProjectRequests
from agent_terminal.scaffold import NewProjectRequests
from agent_terminal.prs import Gh
from agent_terminal.runner import RunnerEvents, TabRunner
from agent_terminal.sessions import LocalSessions, SessionSource
from agent_terminal.settings import Settings
from agent_terminal.store import DebouncedSaver, SidebarWidth, Snapshot, Tab, load_snapshot

log = logging.getLogger(__name__)

# Resuming every agent at once slows the machine to a crawl; a gap keeps it responsive.
RESTORE_GAP_SECONDS = 0.3
DEFAULT_SIZE = (32, 120)


def switch_view(tabs: list[Tab], leaving: str | None, arriving: str | None) -> str | None:
    """Move the user's eye from one tab to another, returning the new active id. Both
    count as read: the one arrived at, and the one left, whose turn may have finished
    while the user sat on it."""
    for tab in tabs:
        if tab.id in (leaving, arriving):
            tab.attention = attention.seen(tab.attention)
    return arriving


class TabManager:
    """Owns the tabs. `emit` sends a message to the page and must not block."""

    def __init__(
        self,
        settings: Settings,
        emit: Emit,
        loop: asyncio.AbstractEventLoop,
        sessions: SessionSource | None = None,
        desktop: Desktop | None = None,
        handlers: dict[str, Handler] | None = None,
    ) -> None:
        """`sessions` is where the tabs' processes live, in this process by default.
        `handlers` answers message types beyond the tabs': the app's own."""
        self._settings = settings
        self._emit = emit
        self._loop = loop
        self._desktop = desktop or Desktop()
        self._tabs: list[Tab] = []
        self._active_id: str | None = None
        self._ordering = Ordering(lambda: self._tabs, self._on_changed)
        self._groups = Groups(self._on_changed)
        self._sidebar = SidebarWidth(self._on_changed)
        self._size = DEFAULT_SIZE
        self._restored = False
        self._saver = DebouncedSaver(loop, settings.state_file, self._snapshot)
        self.sessions: SessionSource = sessions or LocalSessions()
        self.runner = TabRunner(
            settings,
            loop,
            self.sessions,
            RunnerEvents(
                self._on_output,
                self._on_changed,
                self._on_clean_exit,
                lambda tab_id: emit({"type": "reset", "id": tab_id}),
            ),
        )
        gh = Gh(settings)
        links = BoardLinks(lambda: list(self._tabs), self._emit_state)
        self.board = PrBoard(settings, loop, links, gh)
        self._pr_requests = PrRequests(self.board, gh, loop, emit, self._open_or_show)
        self._projects = ProjectRequests(
            settings, emit, loop, self._desktop, gh, lambda: open_folders(self._tabs)
        )
        history = SessionHistory(
            settings, self.board, loop, emit, HistoryLinks(lambda: self._tabs, self._open_or_show)
        )
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {
            "hello": self._on_hello,
            "create": self._on_create,
            "close": lambda message: self._close_tabs([self._tab(message["id"])]),
            # Its PRs go too: a group with nothing but PRs is closed by hiding it.
            "close_group": lambda message: self._close_tabs(
                self._groups.close(message, self._tabs)
            ),
            "fold_group": self._groups.on_fold,
            "sidebar_width": self._sidebar.on_width,
            "activate": self._on_activate,
            "toggle_park": self._on_toggle_park,
            "input": self._on_input,
            "resize": self._on_resize,
            "paste": self._on_paste,
            **DesktopRequests(settings, emit, loop, self._desktop).handlers(),
            **self._pr_requests.handlers(),
            **LandRequests(self.board, self.runner, self._tab).handlers(),
            **history.handlers(),
            **self._ordering.handlers(),
            **self._projects.handlers(),
            **NewProjectRequests(settings, emit, loop, self._projects.send_projects).handlers(),
            **(handlers or {}),
        }

    async def handle(self, message: dict[str, Any]) -> None:
        """Apply one message. An unknown type or a tab already closed is logged and
        dropped: both are ordinary when a click races a process exit."""
        handler = self._handlers.get(message.get("type", ""))
        if handler is None:
            log.warning("unknown message: %r", message.get("type"))
            return
        try:
            await handler(message)
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("dropped %s message: %r", message.get("type"), exc)

    async def _on_hello(self, message: dict[str, Any]) -> None:
        self._size = (int(message["rows"]), int(message["cols"]))
        self._projects.send_projects()
        if self._restored:
            self._emit_state()
            return
        self._restored = True
        self.board.start()
        await self._restore()

    async def _on_create(self, message: dict[str, Any]) -> None:
        agent = message["agent"]
        if agent not in agents.AGENTS:
            raise ValueError(f"unknown agent {agent!r}")
        folder = folders.resolve_folder(message["path"])
        if not folder.is_dir():
            raise ValueError(f"not a folder: {folder}")
        wanted = bool(message.get("worktree"))
        can_cut = await self._loop.run_in_executor(None, folders.can_cut_worktree, folder)
        worktree = wanted and can_cut
        # A worktree is the repository's, whichever of its folders the user chose: that
        # is where `claude -w` makes it from, and so where the tab finds it again.
        scope = gitinfo.main_checkout(folder) or folder if worktree else folder
        tab = Tab(
            id=uuid.uuid4().hex,
            project=folders.display_name(scope),
            project_path=str(scope),
            agent=agent,
            created_at=time.time(),
            worktree=worktree,
            session_id=agents.new_session_id(agent),
        )
        await self._open(tab)

    async def _open(self, tab: Tab) -> None:
        self._tabs.append(tab)
        self._ordering.place(tab.project_path)
        self._active_id = switch_view(self._tabs, self._active_id, tab.id)
        self._groups.opened(tab.project_path)
        self.runner.add(tab)
        self._on_changed()
        await self.runner.start(tab, self._size)
        await self.board.scan()

    async def _open_or_show(self, tab: Tab) -> None:
        """Open a tab resuming a session, or go to the tab that already has it."""
        existing = next((t for t in self._tabs if t.session_id == tab.session_id), None)
        if existing is not None:
            self._active_id = switch_view(self._tabs, self._active_id, existing.id)
            self._on_changed()
            return
        await self._open(tab)

    async def _close_tabs(self, leaving: list[Tab]) -> None:
        """Close one tab, or a group's at once. The active tab, if leaving, hands over to
        the nearest tab left, the next one first."""
        heir = successor(self._tabs, self._active_id, leaving)
        gone = {tab.id for tab in leaving}
        self._tabs = [tab for tab in self._tabs if tab.id not in gone]
        if heir != self._active_id:
            self._active_id = switch_view(self._tabs, None, heir)
        stopping = [self._loop.create_task(self.runner.close(tab_id)) for tab_id in gone]
        self._on_changed()
        await asyncio.gather(*stopping)

    async def _on_activate(self, message: dict[str, Any]) -> None:
        arriving = self._tab(message["id"]).id
        self._active_id = switch_view(self._tabs, self._active_id, arriving)
        self._on_changed()

    async def _on_toggle_park(self, message: dict[str, Any]) -> None:
        tab = self._tab(message["id"])
        tab.parked = not tab.parked
        self._on_changed()

    async def _on_input(self, message: dict[str, Any]) -> None:
        self.runner.input(message["id"], message["data"])

    async def _on_resize(self, message: dict[str, Any]) -> None:
        self._size = (int(message["rows"]), int(message["cols"]))
        for live in self.runner.live.values():
            live.size = self._size
            live.session.resize(*self._size)

    async def _on_paste(self, message: dict[str, Any]) -> None:
        tab_id = self._tab(message["id"]).id
        text = await self._loop.run_in_executor(None, self._desktop.read_clipboard)
        # Back through the page: only xterm knows if the agent wants bracketed paste.
        self._emit({"type": "paste", "id": tab_id, "text": text})

    def _tab(self, tab_id: str) -> Tab:
        for tab in self._tabs:
            if tab.id == tab_id:
                return tab
        raise KeyError(tab_id)

    def _on_output(self, tab_id: str, text: str) -> None:
        self._emit({"type": "output", "id": tab_id, "data": text})

    def _on_changed(self) -> None:
        self._saver.schedule()
        self._emit_state()

    def _on_clean_exit(self, tab_id: str) -> None:
        self._loop.create_task(self.handle({"type": "close", "id": tab_id}))

    def _emit_state(self) -> None:
        live = self.runner.live
        tabs = [
            {
                **tab.to_dict(),
                "status": live[tab.id].status,
                "awaiting_trust": live[tab.id].awaiting_trust,
                **self.board.tab_fields(tab),
            }
            for tab in self._tabs
            if tab.id in live
        ]
        self._emit(
            {
                "type": "state",
                "active_id": self._active_id,
                "tabs": tabs,
                "group_order": self._ordering.group_order,
                "prs": self.board.plain_prs(),
                "sidebar_width": self._sidebar.value,
                **self._groups.state_fields(),
            }
        )

    def _snapshot(self) -> Snapshot:
        groups = self._groups.fields()
        return Snapshot(
            tabs=self._tabs,
            active_id=self._active_id,
            group_order=self._ordering.group_order,
            folded=groups["folded"],
            hidden=groups["hidden"],
            kept=groups["kept"],
            sidebar_width=self._sidebar.value,
        )

    async def _restore(self) -> None:
        """Bring back the saved tabs: each one's process if it outlived the last window,
        and a resumed agent if not."""
        snapshot = load_snapshot(self._settings.state_file)
        self._tabs = snapshot.tabs
        self._ordering.restore(snapshot.group_order)
        self._groups.restore(snapshot.folded, snapshot.hidden, snapshot.kept, self._tabs)
        self._sidebar.value = snapshot.sidebar_width
        ids = {tab.id for tab in self._tabs}
        fallback = self._tabs[0].id if self._tabs else None
        self._active_id = snapshot.active_id if snapshot.active_id in ids else fallback
        survivors = await self.sessions.survivors()
        # From here to the screens, nothing awaits: what an adopted process says next
        # must reach the page after its screen, not before.
        adopted = []
        for tab in self._tabs:
            survivor = survivors.pop(tab.id, None)
            if survivor is None:
                self.runner.add(tab)
            else:
                self.runner.adopt(tab, survivor).size = self._size
                adopted.append((tab.id, survivor.screen))
        self._emit_state()
        for tab_id, screen in adopted:
            # `replay`: the terminal answers queries in old output as it parses it, and
            # those answers must not reach the process as keystrokes.
            self._emit({"type": "output", "id": tab_id, "data": screen, "replay": True})
            self.runner.redraw(tab_id)
        for leftover in survivors.values():
            log.info("ending process %s: its tab was closed", leftover.key)
            await self.sessions.discard(leftover)
        kept = {tab_id for tab_id, _ in adopted}
        for tab in [tab for tab in self._tabs if tab.id not in kept]:
            await self.runner.start(tab, self._size)
            await asyncio.sleep(RESTORE_GAP_SECONDS)

    async def shutdown(self, keep_processes: bool = False) -> None:
        """Save, then end every process. The records stay, which is what brings the tabs
        back next time; only the tab's own "x" forgets one.

        `keep_processes` is a restart's: every process is left running in the session
        host, with what the next window needs to carry on with it."""
        self.board.stop()
        await self.runner.find_unfound_sessions()
        self._saver.save_now()
        if keep_processes:
            self.runner.remember_all()
            await self.sessions.detach()
            return
        await self.runner.close_all()
        await self.sessions.shutdown()

"""The tabs as records, and every change the page can ask for. Runs on the event loop."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from agent_terminal import agents, folders, gitinfo
from agent_terminal.desktop import Desktop, DesktopRequests, Emit
from agent_terminal.pr_board import BoardLinks, PrBoard
from agent_terminal.pr_requests import PrRequests
from agent_terminal.projects import list_projects
from agent_terminal.prs import Gh
from agent_terminal.pty_session import Kill, Spawn, conpty_spawn, kill_tree
from agent_terminal.runner import RunnerEvents, TabRunner
from agent_terminal.settings import Settings
from agent_terminal.store import DebouncedSaver, Snapshot, Tab, load_snapshot

log = logging.getLogger(__name__)

# Resuming every agent at once slows the machine to a crawl; a gap keeps it responsive.
RESTORE_GAP_SECONDS = 0.3
DEFAULT_SIZE = (32, 120)


class TabManager:
    """Owns the tabs. `emit` sends a message to the page and must not block."""

    def __init__(
        self,
        settings: Settings,
        emit: Emit,
        loop: asyncio.AbstractEventLoop,
        spawn: Spawn = conpty_spawn,
        desktop: Desktop | None = None,
        kill: Kill = kill_tree,
    ) -> None:
        self._settings = settings
        self._emit = emit
        self._loop = loop
        self._desktop = desktop or Desktop()
        self._tabs: list[Tab] = []
        self._active_id: str | None = None
        self._size = DEFAULT_SIZE
        self._restored = False
        self._saver = DebouncedSaver(loop, settings.state_file, self._snapshot)
        self.runner = TabRunner(
            settings,
            loop,
            spawn,
            RunnerEvents(
                self._on_output,
                self._on_changed,
                self._on_clean_exit,
                lambda tab_id: emit({"type": "reset", "id": tab_id}),
            ),
            kill,
        )
        gh = Gh(settings)
        links = BoardLinks(lambda: list(self._tabs), self._emit_state)
        self.board = PrBoard(settings, loop, links, gh)
        self._pr_requests = PrRequests(self.board, gh, loop, emit)
        self._handlers = {
            "hello": self._on_hello,
            "create": self._on_create,
            "close": self._on_close,
            "activate": self._on_activate,
            "toggle_park": self._on_toggle_park,
            "input": self._on_input,
            "resize": self._on_resize,
            "paste": self._on_paste,
            "pr_resume": self._on_pr_resume,
            **DesktopRequests(settings, emit, loop, self._desktop).handlers(),
            **self._pr_requests.handlers(),
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
        projects = list_projects(self._settings.projects_root)
        self._emit({"type": "projects", "projects": [p.to_dict() for p in projects]})
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
        session_id, worktree_name = agents.new_tab_identity(agent, worktree)
        tab = Tab(
            id=uuid.uuid4().hex,
            project=folders.display_name(scope),
            project_path=str(scope),
            agent=agent,
            created_at=time.time(),
            worktree=worktree,
            worktree_name=worktree_name,
            session_id=session_id,
        )
        await self._open(tab)

    async def _open(self, tab: Tab) -> None:
        self._tabs.append(tab)
        self._active_id = tab.id
        self.runner.add(tab)
        self._on_changed()
        await self.runner.start(tab, self._size)
        await self.board.scan()

    async def _on_pr_resume(self, message: dict[str, Any]) -> None:
        """Bring a PR's session back as a tab, or go to the tab that already has it."""
        tab = self._pr_requests.tab_for_prompt(message)
        existing = next((t for t in self._tabs if t.session_id == tab.session_id), None)
        if existing is not None:
            self._active_id = existing.id
            self._on_changed()
            return
        await self._open(tab)

    async def _on_close(self, message: dict[str, Any]) -> None:
        tab = self._tab(message["id"])
        index = self._tabs.index(tab)
        self._tabs.remove(tab)
        if self._active_id == tab.id:
            neighbour = self._tabs[min(index, len(self._tabs) - 1)] if self._tabs else None
            self._active_id = neighbour.id if neighbour else None
        stopping = self._loop.create_task(self.runner.close(tab.id))
        self._on_changed()
        await stopping

    async def _on_activate(self, message: dict[str, Any]) -> None:
        self._active_id = self._tab(message["id"]).id
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
                "prs": self.board.plain_prs(),
            }
        )

    def _snapshot(self) -> Snapshot:
        return Snapshot(tabs=self._tabs, active_id=self._active_id)

    async def _restore(self) -> None:
        snapshot = load_snapshot(self._settings.state_file)
        self._tabs = snapshot.tabs
        ids = {tab.id for tab in self._tabs}
        fallback = self._tabs[0].id if self._tabs else None
        self._active_id = snapshot.active_id if snapshot.active_id in ids else fallback
        for tab in self._tabs:
            self.runner.add(tab)
        self._emit_state()
        for tab in self._tabs:
            await self.runner.start(tab, self._size)
            await asyncio.sleep(RESTORE_GAP_SECONDS)

    async def shutdown(self) -> None:
        """Save, then end every process. The records stay, which is what brings the tabs
        back next time; only the tab's own "x" forgets one."""
        self.board.stop()
        await self.runner.find_unfound_sessions()
        self._saver.save_now()
        await self.runner.close_all()

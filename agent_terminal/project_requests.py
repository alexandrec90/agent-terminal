"""What the new-group dialog asks of the projects root: which of the user's repositories
are not here yet, to clone one, and to delete a project. Each answer goes back as a
message; a refusal is an answer the dialog shows, not an error."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agent_terminal import repos
from agent_terminal.desktop import Desktop, Emit
from agent_terminal.projects import list_projects
from agent_terminal.prs import Gh, GhError
from agent_terminal.settings import Settings

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class ProjectRequests:
    """Answers the dialog. `open_folders` gives every folder a tab is in right now."""

    def __init__(
        self,
        settings: Settings,
        emit: Emit,
        loop: asyncio.AbstractEventLoop,
        desktop: Desktop,
        gh: Gh,
        open_folders: Callable[[], list[Path]],
    ) -> None:
        self._settings = settings
        self._emit = emit
        self._loop = loop
        self._desktop = desktop
        self._gh = gh
        self._open_folders = open_folders

    def handlers(self) -> dict[str, Handler]:
        """The message types answered here."""
        return {
            "list_remote_repos": self._on_list_remote,
            "clone_repo": self._on_clone,
            "describe_deletion": self._on_describe_deletion,
            "delete_project": self._on_delete,
        }

    @property
    def _root(self) -> Path:
        return self._settings.projects_root

    def send_projects(self) -> None:
        """The projects root as it is now: what the dialog lists first."""
        projects = list_projects(self._root)
        self._emit({"type": "projects", "projects": [p.to_dict() for p in projects]})

    def _remote_missing(self) -> list[repos.RemoteRepo]:
        where = self._root if self._root.is_dir() else Path.home()
        return repos.missing_locally(repos.parse_repos(self._gh.list_repos(where)), self._root)

    async def _on_list_remote(self, _message: dict[str, Any]) -> None:
        try:
            missing = await self._loop.run_in_executor(None, self._remote_missing)
        except GhError as exc:
            self._emit({"type": "remote_repos", "repos": [], "error": str(exc)})
            return
        self._emit({"type": "remote_repos", "repos": [r.to_dict() for r in missing], "error": ""})

    async def _on_clone(self, message: dict[str, Any]) -> None:
        full_name = message["full_name"]
        reply: dict[str, Any] = {"type": "cloned", "full_name": full_name, "error": ""}
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            target = repos.clone_target(self._root, full_name)
            await self._loop.run_in_executor(None, self._gh.clone, full_name, target)
        except (ValueError, GhError, OSError) as exc:
            self._emit({**reply, "error": str(exc)})
            return
        self.send_projects()
        self._emit({**reply, "path": str(target)})

    def _deletable(self, requested: object) -> Path:
        return repos.deletable_project(self._root, requested, self._open_folders())

    async def _on_describe_deletion(self, message: dict[str, Any]) -> None:
        requested = message["path"]
        reply: dict[str, Any] = {"type": "deletion", "requested": requested, "risks": []}
        try:
            folder = self._deletable(requested)
        except ValueError as exc:
            self._emit({**reply, "error": str(exc)})
            return
        risks = await self._loop.run_in_executor(None, repos.deletion_risks, folder)
        self._emit({**reply, "name": folder.name, "risks": risks, "error": ""})

    async def _on_delete(self, message: dict[str, Any]) -> None:
        requested = message["path"]
        reply: dict[str, Any] = {"type": "deleted", "requested": requested, "error": ""}
        try:
            # Asked again: a tab may have opened there while the user read the warning.
            folder = self._deletable(requested)
            await self._loop.run_in_executor(None, self._desktop.recycle, folder)
        except (ValueError, OSError) as exc:
            self._emit({**reply, "error": str(exc)})
            return
        log.info("recycled project %s", folder)
        self.send_projects()
        self._emit(reply)

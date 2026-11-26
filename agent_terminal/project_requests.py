"""What the new-group dialog asks of the projects root: which of the user's repositories
are not here yet, to clone one, to delete a project (retiring it through devkit first),
and to restore one retired. Each answer goes back as a message; a refusal is an answer
the dialog shows, not an error."""

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
from agent_terminal.retire import RESTORE, RETIRE, RetireError, Retirement, plan_message
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
        # Replaced in tests, which run a fake devkit's script.
        self._retirement = Retirement(settings)
        # One retire or restore at a time: two would race in devkit's registry.
        self._busy = False

    def handlers(self) -> dict[str, Handler]:
        """The message types answered here."""
        return {
            "list_remote_repos": self._on_list_remote,
            "clone_repo": self._on_clone,
            "describe_deletion": self._on_describe_deletion,
            "delete_project": self._on_delete,
            "list_retired": self._on_list_retired,
            "restore_project": self._on_restore,
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

    def _retire_plan(self, name: str) -> dict[str, Any]:
        """What retiring `name` will do, for the confirmation. Raises `RetireError` when
        the script refuses it, which refuses the delete too."""
        if not self._retirement.available():
            return plan_message(None, f"{self._retirement.missing()}: only the folder moves.")
        if not Retirement.takes(name):
            return plan_message(
                None, f"{name} is not a devkit project name: only the folder moves."
            )
        return plan_message(self._retirement.plan(name))

    async def _on_describe_deletion(self, message: dict[str, Any]) -> None:
        requested = message["path"]
        reply: dict[str, Any] = {"type": "deletion", "requested": requested, "risks": []}
        try:
            folder = self._deletable(requested)
            run = self._loop.run_in_executor
            risks = await run(None, repos.deletion_risks, folder)
            retire = await run(None, self._retire_plan, folder.name)
        except (ValueError, OSError, RetireError) as exc:
            self._emit({**reply, "error": str(exc)})
            return
        self._emit({**reply, "name": folder.name, "risks": risks, "retire": retire, "error": ""})

    def _retire_then_recycle(self, folder: Path, force: bool) -> None:
        """Retire first: it has the gates (unpushed work, a live box), and a refusal there
        leaves the folder where it is. Then the folder goes to the Recycle Bin."""
        retired = False
        if self._retirement.available() and Retirement.takes(folder.name):
            self._retirement.apply(RETIRE, folder.name, force=force)
            retired = True
        try:
            self._desktop.recycle(folder)
        except OSError as exc:
            if retired:
                raise OSError(
                    f"{exc} {folder.name} was retired first; Restore puts that back."
                ) from exc
            raise

    async def _on_delete(self, message: dict[str, Any]) -> None:
        requested = message["path"]
        force = message.get("force") is True
        reply: dict[str, Any] = {
            "type": "deleted",
            "requested": requested,
            "error": "",
            "can_force": False,
        }
        if self._busy:
            self._emit({**reply, "error": "A retire or restore is already running."})
            return
        self._busy = True
        try:
            # Asked again: a tab may have opened there while the user read the warning.
            folder = self._deletable(requested)
            reply["name"] = folder.name
            await self._loop.run_in_executor(None, self._retire_then_recycle, folder, force)
        except RetireError as exc:
            self._emit({**reply, "error": str(exc), "can_force": exc.forceable})
            return
        except (ValueError, OSError) as exc:
            self._emit({**reply, "error": str(exc)})
            return
        finally:
            self._busy = False
        log.info("retired and recycled project %s", folder)
        self.send_projects()
        self._emit(reply)
        if self._retirement.available():
            # The restore list has a new entry.
            self._send_retired()

    def _send_retired(self) -> None:
        if not self._retirement.available():
            self._emit({"type": "retired", "projects": [], "error": self._retirement.missing()})
            return
        self._emit({"type": "retired", "projects": self._retirement.retired(), "error": ""})

    async def _on_list_retired(self, _message: dict[str, Any]) -> None:
        self._send_retired()

    def _restore(self, name: str) -> Path:
        if not self._retirement.available():
            raise ValueError(self._retirement.missing())
        if name not in {p["name"] for p in self._retirement.retired()}:
            raise ValueError(f"{name} is not retired on this machine")
        self._retirement.apply(RESTORE, name)
        return self._root / name

    async def _on_restore(self, message: dict[str, Any]) -> None:
        name = message.get("name")
        reply: dict[str, Any] = {"type": "restored", "name": name, "path": "", "error": ""}
        if self._busy:
            self._emit({**reply, "error": "A retire or restore is already running."})
            return
        self._busy = True
        try:
            if not isinstance(name, str):
                raise ValueError("no project named")
            target = await self._loop.run_in_executor(None, self._restore, name)
        except (ValueError, OSError, RetireError) as exc:
            self._emit({**reply, "error": str(exc)})
            return
        finally:
            self._busy = False
        log.info("restored project %s", name)
        self.send_projects()
        self._emit({**reply, "path": str(target) if target.is_dir() else ""})
        self._send_retired()

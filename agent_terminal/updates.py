"""Updating and restarting the app from its own window.

The app runs from a git checkout, so an update is that checkout fast-forwarded to
`origin/<update_branch>` and `uv sync`ed. Neither can happen while the app runs, as uv
replaces files the app holds open, so the window hands both to `relaunch.py` and closes.
With the session host the tabs' processes keep running meanwhile, and the next window
takes them back.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import json
import logging
import os
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from agent_terminal.desktop import Emit, Handler
from agent_terminal.settings import Settings

log = logging.getLogger(__name__)

GIT_TIMEOUT_SECONDS = 120
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# The app's own checkout: the repository `agent_terminal/` sits in.
APP_ROOT = Path(__file__).resolve().parents[1]

Run = Callable[..., subprocess.CompletedProcess[str]]
# Starts the restart helper and closes the window: `update`, then `keep_sessions`.
Restart = Callable[[bool, bool], None]


@dataclass(frozen=True)
class UpdateStatus:
    """What the window can say about updating."""

    # Commits on `origin/<branch>` the checkout does not have.
    behind: int = 0
    # Why an update cannot be applied from here, in words for the user.
    blocked: str = ""
    # Why the check itself failed.
    error: str = ""
    checked: bool = False


def app_checkout(root: Path = APP_ROOT) -> Path | None:
    """The checkout the app runs from, or None if it was installed some other way. A
    worktree's `.git` is a file, so either counts."""
    return root if (root / ".git").exists() else None


def source_checkout(direct_url: str | None) -> Path | None:
    """The checkout a copy was installed from (`uv tool install <dir>`, `pip install
    <dir>`), read from its `direct_url.json`, when that folder is still a checkout."""
    try:
        url = json.loads(direct_url or "").get("url", "")
    except (ValueError, AttributeError):
        return None
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return None
    return app_checkout(Path(url2pathname(parsed.path)))


def installed_from() -> Path | None:
    """`source_checkout` for the copy of the app that is running."""
    try:
        dist = importlib.metadata.distribution("agent-terminal")
    except importlib.metadata.PackageNotFoundError:
        return None
    return source_checkout(dist.read_text("direct_url.json"))


def cannot_update(source: Path | None) -> str:
    """Why a copy that is not a checkout never offers an update, and what to run
    instead. Without it the bar stays empty and the copy looks up to date for good."""
    said = "This copy of the app cannot update itself: it is not running from a git checkout."
    if source is None:
        return f"{said} Run it from a checkout's .venv instead."
    return f"{said} Run {source / '.venv' / 'Scripts' / 'agent-terminal.exe'} instead."


def git_env() -> dict[str, str]:
    """Never stop for a credential prompt: there is no terminal to answer it."""
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def git(checkout: Path, *args: str, run: Run = subprocess.run) -> str:
    """Git's output. Raises `RuntimeError` with what git said."""
    try:
        done = run(
            ["git", *args],
            cwd=checkout,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            creationflags=NO_WINDOW,
            env=git_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"git {args[0]} could not run: {exc}") from exc
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip()
        raise RuntimeError(said or f"git {args[0]} exited {done.returncode}")
    return done.stdout.strip()


def check_for_update(checkout: Path, branch: str, run: Run = subprocess.run) -> UpdateStatus:
    """Fetch `branch` and say how far behind it the checkout is, and whether a fast
    forward could catch it up."""
    try:
        git(checkout, "fetch", "--quiet", "origin", branch, run=run)
        upstream = f"origin/{branch}"
        behind = int(git(checkout, "rev-list", "--count", f"HEAD..{upstream}", run=run) or 0)
        current = git(checkout, "rev-parse", "--abbrev-ref", "HEAD", run=run)
        ahead = int(git(checkout, "rev-list", "--count", f"{upstream}..HEAD", run=run) or 0)
        dirty = git(checkout, "status", "--porcelain", "--untracked-files=no", run=run)
    except (RuntimeError, ValueError) as exc:
        return UpdateStatus(error=str(exc), checked=True)
    blocked = ""
    if current != branch:
        blocked = f"The app's checkout is on {current}, not {branch}."
    elif ahead:
        blocked = f"The app's checkout has {ahead} commit(s) that are not on {upstream}."
    elif dirty:
        blocked = "The app's checkout has uncommitted changes."
    return UpdateStatus(behind=behind, blocked=blocked, checked=True)


def read_update_result(path: Path) -> dict[str, Any] | None:
    """How the last update went, once: the file is removed as it is read."""
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    with contextlib.suppress(OSError):
        path.unlink()
    return result if isinstance(result, dict) else None


class AppControl:
    """Answers the page about the app itself: is there an update, and restart it."""

    def __init__(
        self,
        settings: Settings,
        emit: Emit,
        loop: asyncio.AbstractEventLoop,
        sessions_status: Callable[[], dict[str, Any]],
        restart: Restart,
        checkout: Path | None = None,
        source: Callable[[], Path | None] = installed_from,
    ) -> None:
        self._settings = settings
        # Said once, at start: a copy outside a checkout stays that way until relaunched.
        self._cannot_update = "" if checkout is not None else cannot_update(source())
        self._emit = emit
        self._loop = loop
        self._sessions_status = sessions_status
        self._restart = restart
        self._checkout = checkout
        # Replaced in tests, which have no `origin` to ask.
        self._check: Callable[[Path, str], UpdateStatus] = check_for_update
        self._update = UpdateStatus()
        self._notice = read_update_result(settings.update_result_file)
        self._task: asyncio.Task[None] | None = None
        self._restarting = False

    def handlers(self) -> dict[str, Handler]:
        """The message types answered here."""
        return {"app_status": self._on_status, "app_restart": self._on_restart}

    def start(self) -> None:
        """Check for updates now and every `update_check_seconds`."""
        if self._checkout is not None and self._task is None:
            self._task = self._loop.create_task(self._keep_checking())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _keep_checking(self) -> None:
        while True:
            await self.check_now()
            await asyncio.sleep(self._settings.update_check_seconds)

    async def check_now(self) -> None:
        """Ask `origin`, and tell the page."""
        if self._checkout is None:
            return
        self._update = await self._loop.run_in_executor(
            None, self._check, self._checkout, self._settings.update_branch
        )
        if self._update.error:
            log.info("update check failed: %s", self._update.error)
        self.emit_status()

    def emit_status(self) -> None:
        """Everything the page shows about the app."""
        self._emit(
            {
                "type": "app",
                "update": asdict(self._update) if self._checkout else None,
                "cannot_update": self._cannot_update,
                "sessions": self._sessions_status(),
                "notice": self._notice,
                "restarting": self._restarting,
            }
        )

    async def _on_status(self, _message: dict[str, Any]) -> None:
        self.emit_status()

    async def _on_restart(self, message: dict[str, Any]) -> None:
        """Restart the app, updating it first if asked. The processes are kept unless the
        page asks for them to end, which is how a stale host is replaced."""
        update = bool(message.get("update"))
        keep = bool(message.get("keep_sessions", True))
        if self._restarting:
            return
        if update and (self._checkout is None or self._update.blocked):
            raise ValueError(self._update.blocked or "the app is not running from a checkout")
        self._restarting = True
        try:
            self._restart(update, keep)
        except (OSError, ValueError) as exc:
            log.error("restart failed: %s", exc)
            self._restarting = False
            self._notice = {"ok": False, "step": "restart", "detail": str(exc)}
        self.emit_status()

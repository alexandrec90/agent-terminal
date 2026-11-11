"""What the app does on the machine outside its tabs, and the page's requests for it."""

from __future__ import annotations

import asyncio
import logging
import threading
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from agent_terminal import clipboard, folders, gitinfo
from agent_terminal.gitinfo import GitError
from agent_terminal.settings import Settings

log = logging.getLogger(__name__)

Emit = Callable[[dict[str, Any]], None]
Handler = Callable[[dict[str, Any]], Awaitable[None]]


def is_web_url(url: object) -> bool:
    """Only http(s) links leave the app: terminal output chooses what gets clicked."""
    return isinstance(url, str) and urlsplit(url).scheme in ("http", "https")


def no_picker() -> str | None:
    """Until `__main__` has a window to hang the native dialog from."""
    return None


@dataclass(frozen=True)
class Desktop:
    """The machine, replaceable in tests."""

    open_url: Callable[[str], Any] = webbrowser.open
    read_clipboard: Callable[[], str] = clipboard.read_text
    write_clipboard: Callable[[str], None] = clipboard.write_text
    pick_folder: Callable[[], str | None] = no_picker


class DesktopRequests:
    """Answers the page's requests that have nothing to do with a running tab."""

    def __init__(
        self,
        settings: Settings,
        emit: Emit,
        loop: asyncio.AbstractEventLoop,
        desktop: Desktop,
    ) -> None:
        self._settings = settings
        self._emit = emit
        self._loop = loop
        self._desktop = desktop

    def handlers(self) -> dict[str, Handler]:
        """The message types answered here."""
        return {
            "open_url": self._on_open_url,
            "copy": self._on_copy,
            "inspect_folder": self._on_inspect_folder,
            "pick_folder": self._on_pick_folder,
            "prepare_worktree": self._on_prepare_worktree,
        }

    async def _on_open_url(self, message: dict[str, Any]) -> None:
        url = message["url"]
        if not is_web_url(url):
            raise ValueError(f"refusing to open {url!r}")
        await self._loop.run_in_executor(None, self._desktop.open_url, url)

    async def _on_copy(self, message: dict[str, Any]) -> None:
        text = str(message["text"])
        await self._loop.run_in_executor(None, self._desktop.write_clipboard, text)

    async def _send_folder(self, requested: object, error: str = "") -> None:
        facts = await self._loop.run_in_executor(
            None, folders.describe_folder, self._settings, requested
        )
        self._emit({"type": "folder", **facts, "error": error})

    async def _on_inspect_folder(self, message: dict[str, Any]) -> None:
        await self._send_folder(message["path"])

    async def _on_pick_folder(self, _message: dict[str, Any]) -> None:
        chosen = await self._ask_user()
        if chosen:
            self._emit({"type": "picked", "path": chosen})

    def _ask_user(self) -> asyncio.Future[str | None]:
        """Show the folder dialog on a daemon thread. The pool would not do: a dialog left
        open as the window closes never returns, and exit waits for pool threads."""
        answer: asyncio.Future[str | None] = self._loop.create_future()

        def deliver(chosen: str | None) -> None:
            if not answer.done():
                answer.set_result(chosen)

        def ask() -> None:
            chosen = self._desktop.pick_folder()
            try:
                self._loop.call_soon_threadsafe(deliver, chosen)
            except RuntimeError:
                log.debug("the app closed behind the folder dialog")

        threading.Thread(target=ask, name="folder-dialog", daemon=True).start()
        return answer

    async def _on_prepare_worktree(self, message: dict[str, Any]) -> None:
        """Make a folder able to hold a worktree, on the user's click. Git's refusal is
        what they need to read, so it goes back to the dialog."""
        requested = message["path"]
        folder = folders.resolve_folder(requested)
        try:
            await self._loop.run_in_executor(None, gitinfo.commit_everything, folder)
        except GitError as exc:
            await self._send_folder(requested, str(exc))
            return
        await self._send_folder(requested)

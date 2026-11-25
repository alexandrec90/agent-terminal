"""The sidebar's groups as the user arranged them: folded to a heading, or closed.

A group is a folder path. Closing one closes its tabs and hides it, so its PRs go with
them; it comes back when a session opens there. Both survive a restart in `state.json`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from agent_terminal.store import Tab

Handler = Callable[[dict[str, Any]], Awaitable[None]]


def group_path(message: dict[str, Any]) -> str:
    """The group a message names. Raises `TypeError` for anything but a path's text."""
    path = message["path"]
    if not isinstance(path, str):
        raise TypeError(f"not a group path: {path!r}")
    return path


def successor(tabs: list[Tab], active_id: str | None, leaving: Iterable[Tab]) -> str | None:
    """The tab active once `leaving` closes: the same one if it stays, else the nearest
    one left after it, else the nearest before it, else none."""
    gone = {tab.id for tab in leaving}
    if active_id not in gone:
        return active_id
    index = next((i for i, tab in enumerate(tabs) if tab.id == active_id), 0)
    after = [tab for tab in tabs[index:] if tab.id not in gone]
    before = [tab for tab in tabs[:index] if tab.id not in gone]
    if after:
        return after[0].id
    return before[-1].id if before else None


def open_folders(tabs: Iterable[Tab]) -> list[Path]:
    """Every folder a tab is in: its group's, and its own where a worktree moved it."""
    found: list[Path] = []
    for tab in tabs:
        found.append(Path(tab.project_path))
        if tab.cwd:
            found.append(Path(tab.cwd))
    return found


class Groups:
    """Which groups are folded and which hidden. `changed` saves and tells the page."""

    def __init__(self, changed: Callable[[], None]) -> None:
        self._changed = changed
        self.folded: set[str] = set()
        self.hidden: set[str] = set()

    def restore(self, folded: Iterable[str], hidden: Iterable[str]) -> None:
        self.folded = set(folded)
        self.hidden = set(hidden)

    def fields(self) -> dict[str, list[str]]:
        """For the state message and the session file alike."""
        return {"folded": sorted(self.folded), "hidden": sorted(self.hidden)}

    async def on_fold(self, message: dict[str, Any]) -> None:
        path = group_path(message)
        if message.get("folded"):
            self.folded.add(path)
        else:
            self.folded.discard(path)
        self._changed()

    def close(self, message: dict[str, Any], tabs: Iterable[Tab]) -> list[Tab]:
        """Hide the group a close names, and give back its tabs for closing."""
        path = group_path(message)
        self.hidden.add(path)
        return [tab for tab in tabs if tab.project_path == path]

    def opened(self, path: str) -> None:
        """A session opened in a group: asking for one there is asking to see it."""
        self.hidden.discard(path)
        self.folded.discard(path)

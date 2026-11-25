"""The sidebar's groups as the user arranged them: kept, folded to a heading, or closed.

A group is a folder path. Opening a session there keeps it, and it stays when its last
tab closes: only its own close removes it. That closes its tabs and hides it, so its PRs
go with them; it comes back when a session opens there. All of it survives a restart in
`state.json`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from agent_terminal import folders
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
    """Which groups are kept, folded and hidden. `changed` saves and tells the page."""

    def __init__(self, changed: Callable[[], None]) -> None:
        self._changed = changed
        self.kept: set[str] = set()
        self.folded: set[str] = set()
        self.hidden: set[str] = set()

    def restore(
        self,
        folded: Iterable[str],
        hidden: Iterable[str],
        kept: Iterable[str] | None = None,
        tabs: Iterable[Tab] = (),
    ) -> None:
        """`kept` None is a file from before groups were kept: every group with a tab
        was, then, and no other."""
        self.folded = set(folded)
        self.hidden = set(hidden)
        self.kept = set(kept) if kept is not None else {tab.project_path for tab in tabs}

    def fields(self) -> dict[str, list[str]]:
        """For the session file: the groups by path."""
        return {
            "kept": sorted(self.kept),
            "folded": sorted(self.folded),
            "hidden": sorted(self.hidden),
        }

    def state_fields(self) -> dict[str, Any]:
        """For the state message: a kept group may have no tab to name it, so each
        carries its name."""
        kept = [
            {"path": path, "name": folders.display_name(Path(path))} for path in sorted(self.kept)
        ]
        return {**self.fields(), "kept": kept}

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
        self.kept.discard(path)
        return [tab for tab in tabs if tab.project_path == path]

    def opened(self, path: str) -> None:
        """A session opened in a group: asking for one there is asking to see it."""
        self.kept.add(path)
        self.hidden.discard(path)
        self.folded.discard(path)

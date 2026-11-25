"""The order of the sidebar: tabs within their group, and the groups themselves.

Tab order is the order of the tab list itself. Group order is a record of its own, by
project path, so moving tabs about never reorders the groups."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_terminal.store import Tab


def move_tab(tabs: list[Tab], tab_id: str, before: str | None) -> bool:
    """Put a tab just before another of its group, or with `before` None, last in it.
    Returns whether anything moved. A tab never leaves its group: that is its folder, and
    its process runs there. Raises `KeyError` for an unknown id and `ValueError` for a
    `before` in another group."""
    tab = _find(tabs, tab_id)
    target = _find(tabs, before) if before is not None else None
    if target is not None and target.project_path != tab.project_path:
        raise ValueError("a tab can only move within its own group")
    if target is tab:
        return False
    origin = tabs.index(tab)
    tabs.remove(tab)
    if target is not None:
        place = tabs.index(target)
    else:
        kin = [i for i, t in enumerate(tabs) if t.project_path == tab.project_path]
        place = kin[-1] + 1 if kin else origin
    tabs.insert(place, tab)
    return place != origin


def merge_group_order(current: list[str], named: list[str]) -> list[str]:
    """`named` first, each once, then the groups of `current` it left out: closed since,
    or not shown yet, they keep their places after."""
    first = list(dict.fromkeys(named))
    return first + [path for path in current if path not in first]


def _find(tabs: list[Tab], tab_id: str) -> Tab:
    for tab in tabs:
        if tab.id == tab_id:
            return tab
    raise KeyError(tab_id)


class Ordering:
    """Applies the page's drags. `tabs` returns the manager's live list, which a move
    rearranges in place; `changed` saves and tells the page."""

    def __init__(self, tabs: Callable[[], list[Tab]], changed: Callable[[], None]) -> None:
        self._tabs = tabs
        self._changed = changed
        self.group_order: list[str] = []

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        return {"move_tab": self._on_move_tab, "order_groups": self._on_order_groups}

    def restore(self, saved: list[str]) -> None:
        """Take a saved order, and place any group it lacks: a file from before there
        was one orders the groups as they appear."""
        self.group_order = list(saved)
        for tab in self._tabs():
            self.place(tab.project_path)

    def place(self, path: str) -> None:
        """A group new to the order goes last."""
        if path not in self.group_order:
            self.group_order.append(path)

    async def _on_move_tab(self, message: dict[str, Any]) -> None:
        if move_tab(self._tabs(), message["id"], message.get("before")):
            self._changed()

    async def _on_order_groups(self, message: dict[str, Any]) -> None:
        paths = message["paths"]
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise TypeError("paths must be a list of strings")
        self.group_order = merge_group_order(self.group_order, paths)
        self._changed()

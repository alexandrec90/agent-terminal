"""The tab records and the file that brings them back after the window closes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SAVE_DELAY_SECONDS = 0.5
# As the page's own limit. It keeps to its window's width too, which is not known here.
MIN_SIDEBAR_WIDTH = 180


@dataclass
class Tab:
    """Everything needed to show a tab and to start its agent again."""

    id: str
    project: str
    project_path: str
    agent: str
    created_at: float
    worktree: bool = False
    # Claude's is chosen up front; Codex's is read from its rollout once it exists.
    session_id: str | None = None
    # The agent's working directory once known, for a worktree the project path is not it.
    # Each agent names its own worktree, so this is read back from the agent's records.
    cwd: str | None = None
    title: str = ""
    parked: bool = False
    # "", "input" or "finished": see `attention.Presence`.
    attention: str = ""
    # When the session last did something the user set going; None until it has, when
    # the page shows `created_at` instead.
    updated_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Snapshot:
    """What the session file holds."""

    tabs: list[Tab] = field(default_factory=list)
    active_id: str | None = None
    # The sidebar's group order, by project path. A group not named here comes after
    # these, in the order it first appeared.
    group_order: list[str] = field(default_factory=list)
    # Group folders, by path: those folded to their heading, and those closed while they
    # held only PRs, which stay out of the sidebar until a session opens there.
    folded: list[str] = field(default_factory=list)
    hidden: list[str] = field(default_factory=list)
    # In CSS pixels; None until the user drags it, for the page's default.
    sidebar_width: int | None = None


def paths_of(value: object) -> list[str]:
    """A saved list of folder paths. Anything else, as a file from before it was kept,
    is no paths at all rather than a reason to lose every tab."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def clean_sidebar_width(value: object) -> int | None:
    """`value` as a sidebar width, never too narrow to use. None for anything but a whole
    number, which a file from before it was kept also gives."""
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return max(value, MIN_SIDEBAR_WIDTH)


def tab_from_dict(data: dict[str, Any]) -> Tab:
    """Build a `Tab`, ignoring keys a different version wrote. A record missing a
    required key raises `TypeError`."""
    known = {f.name for f in fields(Tab)}
    tab = Tab(**{key: value for key, value in data.items() if key in known})
    legacy_name = data.get("worktree_name")
    if isinstance(legacy_name, str) and legacy_name and not tab.cwd:
        # Saved when this app named Claude's worktrees: the folder followed from the name.
        tab.cwd = str(Path(tab.project_path) / ".claude" / "worktrees" / legacy_name)
    if isinstance(tab.attention, bool):
        # Saved before the two kinds were told apart. Which it was is lost, so take the
        # louder: a tab that only finished costs a look, a missed question costs the work.
        tab.attention = "input" if tab.attention else ""
    return tab


def parse_snapshot(text: str) -> Snapshot:
    """Read the session file's text. Raises `ValueError` on anything it cannot trust."""
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
        raise ValueError("unrecognised session file")
    try:
        tabs = [tab_from_dict(item) for item in data["tabs"]]
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"malformed tab record: {exc}") from exc
    active = data.get("active_id")
    return Snapshot(
        tabs=tabs,
        active_id=active if isinstance(active, str) else None,
        group_order=clean_group_order(data.get("group_order")),
        folded=paths_of(data.get("folded")),
        hidden=paths_of(data.get("hidden")),
        sidebar_width=clean_sidebar_width(data.get("sidebar_width")),
    )


def clean_group_order(value: object) -> list[str]:
    """`value` as a group order: its strings, each once, in order. Anything that is not a
    list is no order at all, which a file written before there was one also holds."""
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str)))


def load_snapshot(path: Path) -> Snapshot:
    """The saved session, or an empty one. An unreadable file is moved aside rather
    than overwritten, so one bad write cannot silently cost every tab."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Snapshot()
    try:
        return parse_snapshot(text)
    except ValueError as exc:
        aside = path.with_suffix(".corrupt.json")
        log.error("session file unreadable (%s); moved to %s", exc, aside)
        os.replace(path, aside)
        return Snapshot()


def save_snapshot(path: Path, snapshot: Snapshot) -> None:
    """Replace the session file atomically: a crash mid-write leaves the last good one."""
    payload = {
        "version": SCHEMA_VERSION,
        "active_id": snapshot.active_id,
        "group_order": snapshot.group_order,
        "tabs": [tab.to_dict() for tab in snapshot.tabs],
        "folded": snapshot.folded,
        "hidden": snapshot.hidden,
        "sidebar_width": snapshot.sidebar_width,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, path)


class DebouncedSaver:
    """Saves soon rather than now, so a burst of changes writes the file once."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        path: Path,
        snapshot: Callable[[], Snapshot],
        delay: float = SAVE_DELAY_SECONDS,
    ) -> None:
        self._loop = loop
        self._path = path
        self._snapshot = snapshot
        self._delay = delay
        self._pending = False

    def schedule(self) -> None:
        """Save after the delay, unless a save is already due."""
        if not self._pending:
            self._pending = True
            self._loop.call_later(self._delay, self.save_now)

    def save_now(self) -> None:
        """Save at once."""
        self._pending = False
        save_snapshot(self._path, self._snapshot())

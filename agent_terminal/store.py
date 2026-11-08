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


@dataclass
class Tab:
    """Everything needed to show a tab and to start its agent again."""

    id: str
    project: str
    project_path: str
    agent: str
    created_at: float
    worktree: bool = False
    # Claude's worktree name, chosen here so the tab can find the directory again.
    worktree_name: str | None = None
    # Claude's is chosen up front; Codex's is read from its rollout once it exists.
    session_id: str | None = None
    # The agent's working directory once known, for a worktree the project path is not it.
    cwd: str | None = None
    title: str = ""
    parked: bool = False
    # "", "input" or "finished": see `attention.Presence`.
    attention: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Snapshot:
    """What the session file holds."""

    tabs: list[Tab] = field(default_factory=list)
    active_id: str | None = None


def tab_from_dict(data: dict[str, Any]) -> Tab:
    """Build a `Tab`, ignoring keys a different version wrote. A record missing a
    required key raises `TypeError`."""
    known = {f.name for f in fields(Tab)}
    tab = Tab(**{key: value for key, value in data.items() if key in known})
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
    return Snapshot(tabs=tabs, active_id=active if isinstance(active, str) else None)


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
        "tabs": [tab.to_dict() for tab in snapshot.tabs],
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

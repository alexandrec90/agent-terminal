"""Finding a Codex tab's session, which Codex never states.

Claude takes a session id on its command line. Codex picks its own and only writes it
down, in `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, once a session has a first turn.
A tab has to find its session there to resume it, by start time and directory.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agent_terminal.store import Tab

# How long a rollout may predate its tab and still be that tab's: Codex stamps it with
# the start of the process, a moment after the tab's own clock read.
ROLLOUT_CLOCK_SKEW_SECONDS = 5.0


@dataclass(frozen=True)
class CodexRollout:
    """The first record of a Codex session file."""

    session_id: str
    cwd: str
    started_at: float


def parse_rollout_head(line: str) -> CodexRollout | None:
    """Read a rollout's `session_meta` line, or None if it is not one."""
    try:
        record = json.loads(line)
        payload = record["payload"]
        if record["type"] != "session_meta":
            return None
        stamp = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
        return CodexRollout(payload["id"], payload["cwd"], stamp.astimezone(UTC).timestamp())
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def recent_rollouts(codex_home: Path, since: float) -> list[CodexRollout]:
    """Rollouts begun after `since`, oldest first. Only files written since then are
    opened: `sessions/` holds every conversation the machine has had."""
    found: list[CodexRollout] = []
    for path in (codex_home / "sessions").glob("*/*/*/rollout-*.jsonl"):
        try:
            if path.stat().st_mtime < since:
                continue
            with path.open(encoding="utf-8") as handle:
                rollout = parse_rollout_head(handle.readline())
        except OSError:
            continue
        if rollout and rollout.started_at >= since - ROLLOUT_CLOCK_SKEW_SECONDS:
            found.append(rollout)
    return sorted(found, key=lambda rollout: rollout.started_at)


def is_same_or_worktree(rollout_cwd: str, project_path: str, codex_home: Path) -> bool:
    """A rollout belongs to a project if it ran in it, below it, or in a Codex-managed
    worktree of it, which lives under `~/.codex/worktrees/*/<project name>`."""
    cwd = Path(rollout_cwd)
    project = Path(project_path)
    if cwd == project or project in cwd.parents:
        return True
    return (codex_home / "worktrees") in cwd.parents and cwd.name == project.name


def claim_codex_rollout(
    tab: Tab, rollouts: list[CodexRollout], claimed: set[str], codex_home: Path
) -> CodexRollout | None:
    """The earliest unclaimed rollout that can be this tab's. A worktree tab takes only a
    rollout in a managed worktree, and a plain tab only one in the project."""
    for rollout in rollouts:
        if rollout.session_id in claimed:
            continue
        if not is_same_or_worktree(rollout.cwd, tab.project_path, codex_home):
            continue
        in_managed_worktree = (codex_home / "worktrees") in Path(rollout.cwd).parents
        if in_managed_worktree == tab.worktree:
            return rollout
    return None


class CodexSessionFinder:
    """Looks for tabs' rollouts, one search at a time, off the event loop."""

    def __init__(
        self,
        codex_home: Path,
        loop: asyncio.AbstractEventLoop,
        claimed: Callable[[], set[str]],
        on_found: Callable[[], None],
    ) -> None:
        self._home = codex_home
        self._loop = loop
        self._claimed = claimed
        self._on_found = on_found
        self._searching = False

    def look_for(self, tab: Tab, since: float) -> None:
        """Start a search unless one is running. A tab missed now is looked for again on
        its next status change."""
        if self._searching:
            return
        self._searching = True
        self._loop.create_task(self.search(tab, since))

    async def search(self, tab: Tab, since: float) -> None:
        """Give `tab` its session if its rollout exists yet."""
        try:
            rollouts = await self._loop.run_in_executor(None, recent_rollouts, self._home, since)
        finally:
            self._searching = False
        rollout = claim_codex_rollout(tab, rollouts, self._claimed(), self._home)
        if rollout:
            tab.session_id = rollout.session_id
            tab.cwd = rollout.cwd
            self._on_found()

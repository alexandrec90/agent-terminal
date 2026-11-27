"""The branch a Claude session last worked on, read from its transcript.

A tab's folder names the branch the session started on, but a session can land from
elsewhere: from a subagent's worktree, or after the harness reaped its own. Every
transcript record carries the `cwd` and `gitBranch` of its moment, so the last one on a
branch other than the trunk says where the session's latest work went.

Its `pr-link` records do not. In a real session (Claude Code 2.1) they kept repeating a
PR it had only looked at, and none came for the PR `scripts/land.py` opened through
`gh api`.

Transcripts only grow, so each is read on from where the last read stopped.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from agent_terminal import agents, gitinfo
from agent_terminal.store import Tab

# A session on these is working on the trunk, which heads no PR.
TRUNK_BRANCHES = frozenset({"main", "master"})


@dataclass(frozen=True)
class SessionBranch:
    """A branch a session was on, and the folder it was on it in."""

    branch: str
    cwd: str


def last_branch(lines: Iterable[str]) -> SessionBranch | None:
    """The last branch other than the trunk that these transcript lines were on."""
    found = None
    for line in lines:
        # Most of a transcript is not a record with a branch, and parsing it all is slow.
        if '"gitBranch"' not in line:
            continue
        try:
            record = json.loads(line)
            branch, cwd = record["gitBranch"], record["cwd"]
        except (ValueError, KeyError, TypeError):
            continue
        if not (isinstance(branch, str) and isinstance(cwd, str)):
            continue
        if branch and cwd and branch not in TRUNK_BRANCHES:
            found = SessionBranch(branch, cwd)
    return found


@dataclass
class _Reading:
    path: Path
    offset: int = 0
    found: SessionBranch | None = None


class SessionBranches:
    """Reads each tab's transcript, only what was added since the last read. Blocking,
    and safe to call from two threads: two scans can overlap."""

    def __init__(self, claude_home: Path) -> None:
        self._home = claude_home
        self._readings: dict[str, _Reading] = {}
        self._lock = threading.Lock()

    def of(self, tab: Tab) -> tuple[Path | None, str | None]:
        """The repository and branch the tab's session was last on, if it was on one."""
        if tab.agent != agents.CLAUDE or not tab.session_id:
            return None, None
        with self._lock:
            found = self._read(tab.session_id)
        if found is None:
            return None, None
        # A reaped worktree's folder is gone, but it lay inside its repository's.
        return gitinfo.main_checkout(Path(found.cwd)), found.branch

    def forget_others(self, session_ids: set[str]) -> None:
        """Stop keeping what was read for sessions no tab holds any more."""
        with self._lock:
            for session_id in self._readings.keys() - session_ids:
                del self._readings[session_id]

    def _read(self, session_id: str) -> SessionBranch | None:
        reading = self._readings.get(session_id)
        if reading is None:
            path = agents.claude_transcript(self._home, session_id)
            if path is None:
                return None
            reading = self._readings[session_id] = _Reading(path)
        try:
            with reading.path.open("rb") as handle:
                if handle.seek(0, os.SEEK_END) < reading.offset:
                    # Not the file that was read before: read it afresh.
                    reading.offset, reading.found = 0, None
                handle.seek(reading.offset)
                added = handle.read()
        except OSError:
            # Gone, or moved: look for it again next time.
            del self._readings[session_id]
            return None
        # A line still being written is read once it is whole.
        whole = added[: added.rfind(b"\n") + 1]
        reading.offset += len(whole)
        found = last_branch(whole.decode("utf-8", errors="replace").splitlines())
        if found is not None:
            reading.found = found
        return reading.found

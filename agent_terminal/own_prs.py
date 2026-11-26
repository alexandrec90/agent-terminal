"""Which PRs came from a session the user prompted, out of all those under their name.

The fix pass, its fixers and the release scripts open PRs as the user too, so GitHub cannot
tell them apart. Claude Code's own records can, for a session `history.jsonl` says began
with a prompt of the user's:

- its worktree's branch, from the folder the prompt was typed in;
- the branch its transcript says it was on before the PR was opened;
- a `pr-link` its transcript gained as the PR was opened. A later one is not evidence: a
  session gains one for any PR it looks at.

Read from real records (Claude Code 2.1). Transcripts are deleted after a month, so what
was found is remembered by the caller.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PureWindowsPath

# `pr-link` follows the opening by a few seconds. Minutes later, it is a PR being read.
LINK_SECONDS = 60.0
WORKTREE_BRANCH_PREFIX = "worktree-"
DISPATCHED_PREFIXES = ("You are a fixer session, dispatched by the fix pass.",)


def folder_names(path: str) -> frozenset[str]:
    """The name of every folder in a path. A repository's is among those of any checkout
    of it, worktrees included, as they live inside it."""
    return frozenset(part.lower() for part in PureWindowsPath(path).parts)


@dataclass
class Evidence:
    """What sessions say they worked on: branches, by folder and from when, and PRs."""

    branches: dict[tuple[str, frozenset[str]], float] = field(default_factory=dict)
    links: dict[str, float] = field(default_factory=dict)

    def add_branch(self, branch: str, cwd: str, at: float) -> None:
        key = (branch, folder_names(cwd))
        self.branches[key] = min(at, self.branches.get(key, at))

    def add_link(self, url: str, at: float) -> None:
        self.links[url] = min(at, self.links.get(url, at))

    def update(self, other: Evidence) -> None:
        for (branch, folders), at in other.branches.items():
            key = (branch, folders)
            self.branches[key] = min(at, self.branches.get(key, at))
        for url, at in other.links.items():
            self.add_link(url, at)

    def covers(self, url: str, head: str, created: float) -> bool:
        """Whether a PR came from one of these sessions."""
        linked = self.links.get(url)
        if linked is not None and abs(linked - created) <= LINK_SECONDS:
            return True
        # https://github.com/owner/repository/pull/1
        parts = url.lower().split("/")
        repository = parts[4] if len(parts) > 4 else ""
        return any(
            branch == head and repository in folders and since <= created
            for (branch, folders), since in self.branches.items()
        )


def own_sessions(lines: Iterable[str]) -> tuple[set[str], Evidence]:
    """The sessions that began with the user's prompt, and the worktrees they were in."""
    dispatched: dict[str, bool] = {}
    evidence = Evidence()
    for line in lines:
        try:
            record = json.loads(line)
            session, text = record["sessionId"], record["display"]
            project, at = record["project"], record["timestamp"] / 1000
            own = not dispatched.setdefault(session, text.startswith(DISPATCHED_PREFIXES))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        folder = PureWindowsPath(project)
        if own and folder.parent.name == "worktrees":
            evidence.add_branch(WORKTREE_BRANCH_PREFIX + folder.name, project, at)
    return {session for session, machine in dispatched.items() if not machine}, evidence


def transcript_evidence(lines: Iterable[str]) -> Evidence:
    """What one session's transcript says it worked on."""
    evidence = Evidence()
    for line in lines:
        # Most of a transcript is neither, and parsing it all is slow.
        if '"gitBranch"' not in line and '"pr-link"' not in line:
            continue
        try:
            record = json.loads(line)
            at = datetime.fromisoformat(record["timestamp"]).timestamp()
            if record.get("type") == "pr-link":
                evidence.add_link(record["prUrl"], at)
            elif record.get("gitBranch"):
                evidence.add_branch(record["gitBranch"], record["cwd"], at)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return evidence


class SessionEvidence:
    """Reads Claude's records, each transcript only when it has changed. Blocking."""

    def __init__(self, claude_home: Path) -> None:
        self._home = claude_home
        self._transcripts: dict[Path, tuple[tuple[int, int], Evidence]] = {}

    def read(self) -> Evidence:
        """All there is now."""
        try:
            with (self._home / "history.jsonl").open(encoding="utf-8", errors="replace") as lines:
                sessions, found = own_sessions(lines)
        except FileNotFoundError:
            return Evidence()
        for path in (self._home / "projects").glob("*/*.jsonl"):
            if path.stem in sessions:
                found.update(self._transcript(path))
        return found

    def _transcript(self, path: Path) -> Evidence:
        read_at, known = self._transcripts.get(path, ((0, 0), Evidence()))
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp == read_at:
                return known
            with path.open(encoding="utf-8", errors="replace") as lines:
                evidence = transcript_evidence(lines)
        except OSError:
            return known
        self._transcripts[path] = (stamp, evidence)
        return evidence

"""Finding the session that worked in a worktree, from the agents' own records.

A PR's prompt usually began outside this app, so no tab knows about it. Its worktree
still does: Claude files transcripts under a directory named for the session's folder,
and a Codex rollout states its folder in its first line.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from agent_terminal.codex_sessions import recent_rollouts
from agent_terminal.settings import Settings

# How far back to read Codex rollouts. `sessions/` keeps every conversation there has been
# and each is a file to open; a worktree older than this has no PR left to review.
CODEX_LOOKBACK_SECONDS = 30 * 24 * 3600
ENCODED_PATH_CHARACTERS = re.compile(r"[^A-Za-z0-9]")


@dataclass(frozen=True)
class Prompt:
    """A session that can be resumed."""

    agent: str
    session_id: str


def claude_transcript_dir(claude_home: Path, tree: Path) -> Path:
    """Where Claude keeps the transcripts of sessions started in `tree`."""
    return claude_home / "projects" / ENCODED_PATH_CHARACTERS.sub("-", str(tree))


def newest_claude_session(claude_home: Path, tree: Path) -> str | None:
    """The id of the session most recently written in `tree`."""
    newest: tuple[float, str] | None = None
    for path in claude_transcript_dir(claude_home, tree).glob("*.jsonl"):
        try:
            entry = (path.stat().st_mtime, path.stem)
        except OSError:
            continue
        if newest is None or entry > newest:
            newest = entry
    return newest[1] if newest else None


def newest_codex_session(codex_home: Path, tree: Path) -> str | None:
    """The id of the last session Codex began in `tree`."""
    since = time.time() - CODEX_LOOKBACK_SECONDS
    matching = [r for r in recent_rollouts(codex_home, since) if Path(r.cwd) == tree]
    return matching[-1].session_id if matching else None


def find_prompt(settings: Settings, tree: Path) -> Prompt | None:
    """The session to resume for `tree`, if any. Codex is looked for only in its own
    worktrees: that search opens files, and this runs for every PR on every poll."""
    claude = newest_claude_session(settings.claude_home, tree)
    if claude:
        return Prompt("claude", claude)
    if settings.codex_home / "worktrees" not in tree.parents:
        return None
    codex = newest_codex_session(settings.codex_home, tree)
    return Prompt("codex", codex) if codex else None

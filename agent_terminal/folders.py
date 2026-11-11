"""The folders a tab can be scoped to, which is any folder there is."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_terminal import gitinfo, trust
from agent_terminal.agents import AGENTS
from agent_terminal.settings import Settings


def resolve_folder(text: object) -> Path:
    """The folder a path means. Raises `ValueError` for anything but an absolute path,
    `~` included: a relative one would mean wherever the app happened to start."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("no folder given")
    path = Path(text.strip().strip('"')).expanduser()
    if not path.is_absolute():
        raise ValueError(f"not an absolute path: {text!r}")
    return path.resolve()


def display_name(folder: Path) -> str:
    """What a group calls a folder. A drive's root has no name of its own."""
    return folder.name or str(folder)


def can_cut_worktree(folder: Path) -> bool:
    """Both agents need a repository with a commit: they fail with git's own words in a
    plain folder or an empty repository."""
    return gitinfo.git_dir(folder) is not None and gitinfo.has_commits(folder)


def describe_folder(settings: Settings, requested: object) -> dict[str, Any]:
    """What the new-session dialog needs to know of a folder. `requested` is echoed so a
    reply can be matched to the path it answers, and a bad path is an answer, not an error."""
    facts: dict[str, Any] = {"requested": requested, "exists": False}
    try:
        folder = resolve_folder(requested)
    except ValueError:
        return facts
    if not folder.is_dir():
        return facts
    # Trust is judged where a worktree tab runs: the repository, or the folder without one.
    scope = gitinfo.main_checkout(folder) or folder
    return {
        **facts,
        "exists": True,
        "path": str(folder),
        "name": display_name(folder),
        "is_git": gitinfo.git_dir(folder) is not None,
        "has_commits": can_cut_worktree(folder),
        "trusted": {agent: trust.is_trusted(settings, agent, scope) for agent in AGENTS},
    }

"""The folders offered first when scoping a tab: those directly under the projects root.
Any other folder can be typed or browsed to, as `folders.py` has it."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Project:
    """One directory under the projects root."""

    name: str
    path: str
    is_git: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_git_checkout(path: Path) -> bool:
    """True for a repository or a linked worktree, whose `.git` is a file."""
    return (path / ".git").exists()


def list_projects(root: Path) -> list[Project]:
    """Every visible directory under `root`, by name. A missing root lists nothing."""
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    directories = [p for p in entries if p.is_dir() and not p.name.startswith(".")]
    return [
        Project(name=p.name, path=str(p), is_git=is_git_checkout(p))
        for p in sorted(directories, key=lambda p: p.name.lower())
    ]

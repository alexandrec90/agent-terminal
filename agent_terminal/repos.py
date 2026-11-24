"""The projects root as something to change: a repository of the user's on GitHub cloned
into it, or a project taken off this machine.

Only the root's own folders can be deleted, and only with no tab in them. Before a delete,
the page is told what it would lose that exists nowhere else.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agent_terminal import gitinfo
from agent_terminal.folders import resolve_folder
from agent_terminal.gitinfo import GitError
from agent_terminal.prs import GhError

# GitHub's own alphabet for owners and names. Anything else is not a repository of theirs,
# and a value reaching `gh repo clone` that began with "-" would be read as an option.
FULL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class RemoteRepo:
    """One of the user's repositories on GitHub."""

    name: str
    full_name: str
    description: str
    archived: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_repos(text: str) -> list[RemoteRepo]:
    """A `gh repo list --json` reply. Raises `GhError` on anything else."""
    try:
        records = json.loads(text)
        if not isinstance(records, list):
            raise TypeError(f"a list was expected, not {type(records).__name__}")
        return [
            RemoteRepo(
                name=str(record["name"]),
                full_name=str(record["nameWithOwner"]),
                description=str(record.get("description") or ""),
                archived=bool(record.get("isArchived")),
            )
            for record in records
        ]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise GhError(f"unexpected reply from gh: {exc}") from exc


def local_names(root: Path) -> set[str]:
    """Every name taken under the root, folders and files alike, in lower case: Windows
    would not let a clone have a name that differs only in case."""
    try:
        return {entry.name.lower() for entry in root.iterdir()}
    except OSError:
        return set()


def missing_locally(repos: Iterable[RemoteRepo], root: Path) -> list[RemoteRepo]:
    """The repositories with no folder of their name under the root: live ones first."""
    taken = local_names(root)
    absent = [repo for repo in repos if repo.name.lower() not in taken]
    return sorted(absent, key=lambda repo: (repo.archived, repo.name.lower()))


def clone_target(root: Path, full_name: object) -> Path:
    """Where `owner/name` would be cloned to. Raises `ValueError` for a name that is not a
    repository's, or one whose folder is already taken."""
    if not isinstance(full_name, str) or not FULL_NAME.match(full_name):
        raise ValueError(f"not a repository name: {full_name!r}")
    name = full_name.split("/", 1)[1]
    if name in (".", "..") or name.startswith("."):
        raise ValueError(f"not a folder a clone can have: {name!r}")
    if name.lower() in local_names(root):
        raise ValueError(f"{name} is already in {root}")
    return root / name


def contains(folder: Path, other: Path) -> bool:
    """Whether `other` is `folder` or anywhere inside it."""
    return other == folder or folder in other.parents


def deletable_project(root: Path, requested: object, open_folders: Iterable[Path]) -> Path:
    """The project a delete request names. Raises `ValueError` unless it is a folder
    directly under the root with no tab in it: this is what stands between a socket
    message and the Recycle Bin."""
    folder = resolve_folder(requested)
    if folder.parent != root.resolve() or folder.name.startswith("."):
        raise ValueError(f"not a project: {folder}")
    if not folder.is_dir():
        raise ValueError(f"not a folder: {folder}")
    for opened in open_folders:
        if contains(folder, opened.resolve()):
            raise ValueError(f"{folder.name} has a tab open; close it first")
    return folder


def worktree_paths(porcelain: str) -> list[Path]:
    """Every tree `git worktree list --porcelain` names, detached ones included."""
    return [
        Path(line.removeprefix("worktree "))
        for line in porcelain.splitlines()
        if line.startswith("worktree ")
    ]


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def deletion_risks(folder: Path) -> list[str]:
    """What deleting `folder` would lose that is saved nowhere else, in the user's terms.
    Empty means everything in it is on a remote."""
    if gitinfo.main_checkout(folder) != folder:
        return ["It isn't a git repository, so nothing in it is saved anywhere else."]
    try:
        risks = []
        if not gitinfo.run_git(folder, "remote").strip():
            risks.append("It has no remote, so its history exists only here.")
        trees = worktree_paths(gitinfo.run_git(folder, "worktree", "list", "--porcelain"))
        dirty = [tree for tree in trees if tree.is_dir() and has_changes(tree)]
        if dirty:
            risks.append(f"{plural(len(dirty), 'worktree')} with uncommitted changes.")
        unpushed = gitinfo.run_git(folder, "log", "--branches", "--not", "--remotes", "--oneline")
        if unpushed.strip():
            count = len(unpushed.strip().splitlines())
            risks.append(f"{plural(count, 'commit')} not pushed anywhere.")
    except GitError as exc:
        return [f"Git could not say what is unsaved: {exc}"]
    return risks


def has_changes(tree: Path) -> bool:
    """Whether a tree has anything uncommitted, untracked files included."""
    return bool(gitinfo.run_git(tree, "status", "--porcelain").strip())

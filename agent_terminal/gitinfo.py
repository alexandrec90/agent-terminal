"""What a folder is to git.

Most of this reads `.git` directly rather than spawning git: a tab's branch is looked at
every few seconds, for every tab, and a process each time would be felt.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

GIT_TIMEOUT_SECONDS = 60
# The app is a windowless GUI process: without this, each spawn flashes a console.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
HEAD_BRANCH_PREFIX = "ref: refs/heads/"
GITDIR_PREFIX = "gitdir:"
FIRST_COMMIT_MESSAGE = "Initial commit"


class GitError(Exception):
    """A git command failed. The text is git's own, fit to show the user."""


def git_dir(folder: Path) -> Path | None:
    """The git directory governing `folder`, looking upward, or None outside a repository.
    A linked worktree's `.git` is a file naming its directory under the main checkout."""
    for candidate in (folder, *folder.parents):
        dot_git = candidate / ".git"
        if dot_git.is_dir():
            return dot_git
        if dot_git.is_file():
            return _linked_git_dir(dot_git)
    return None


def _linked_git_dir(dot_git: Path) -> Path | None:
    try:
        text = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith(GITDIR_PREFIX):
        return None
    return (dot_git.parent / text.removeprefix(GITDIR_PREFIX).strip()).resolve()


def main_checkout(folder: Path) -> Path | None:
    """The repository's main checkout, whichever of its folders or worktrees `folder` is.
    It is what `claude -w` works from wherever it is started, and what names a repository."""
    found = git_dir(folder)
    if found is None:
        return None
    if found.parent.name == "worktrees":
        return found.parent.parent.parent
    return found.parent


def current_branch(folder: Path) -> str | None:
    """The branch checked out in `folder`; None if detached or not a repository."""
    found = git_dir(folder)
    if found is None:
        return None
    try:
        head = (found / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return head.removeprefix(HEAD_BRANCH_PREFIX) if head.startswith(HEAD_BRANCH_PREFIX) else None


def run_git(folder: Path, *args: str) -> str:
    """Git's standard output. Raises `GitError` with what it said if it fails or cannot run."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed subcommands, no shell
            ["git", *args],  # noqa: S607 - git from PATH, as a shell would find it
            cwd=folder,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            creationflags=NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {args[0]} could not run: {exc}") from exc
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip()
        raise GitError(said or f"git {args[0]} exited {done.returncode}")
    return done.stdout


def has_commits(folder: Path) -> bool:
    """Whether HEAD exists. A worktree is cut from a commit, so an empty repository has
    nothing to cut one from."""
    try:
        run_git(folder, "rev-parse", "--verify", "--quiet", "HEAD")
    except GitError:
        return False
    return True


def parse_worktrees(porcelain: str) -> dict[str, Path]:
    """`git worktree list --porcelain` as branch to path. Detached trees have no branch."""
    trees: dict[str, Path] = {}
    path: Path | None = None
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
        elif line.startswith("branch refs/heads/") and path is not None:
            trees[line.removeprefix("branch refs/heads/")] = path
    return trees


def worktrees(repository: Path) -> dict[str, Path]:
    """Every branch checked out somewhere, with where. Empty if git cannot say."""
    try:
        return parse_worktrees(run_git(repository, "worktree", "list", "--porcelain"))
    except GitError:
        return {}


def commit_everything(folder: Path) -> None:
    """Make `folder` a repository a worktree can be cut from: `git init` if it is not one,
    then a first commit of what is there. Raises `GitError`, as it can for want of a
    configured identity."""
    if git_dir(folder) is None:
        run_git(folder, "init")
    run_git(folder, "add", "--all")
    run_git(folder, "commit", "--allow-empty", "--message", FIRST_COMMIT_MESSAGE)

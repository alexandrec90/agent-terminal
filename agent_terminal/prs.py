"""Pull requests, through the `gh` the user is already signed in to.

Field names and values are as `gh pr list --json` really gives them (gh 2.98). Everything
that reaches the page is data for text nodes: PR text is written by whoever opened it.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent_terminal.gitinfo import NO_WINDOW
from agent_terminal.settings import Settings

log = logging.getLogger(__name__)

GH_TIMEOUT_SECONDS = 60
# A clone carries a repository's whole history, which a minute does not always cover.
CLONE_TIMEOUT_SECONDS = 15 * 60
REPO_LIMIT = "500"
REPO_FIELDS = "name,nameWithOwner,description,isArchived"
OPEN = "OPEN"
MERGED = "MERGED"
LIST_LIMIT = "50"
# A diff this long is not read here, and the socket is not the place to carry it.
MAX_DIFF_CHARACTERS = 2_000_000
LIST_FIELDS = (
    "number,title,url,state,headRefName,baseRefName,isDraft,author,statusCheckRollup,"
    "reviewDecision,mergeable,mergeStateStatus,labels,mergeCommit,"
    "mergedAt,headRefOid"
)
DETAIL_FIELDS = f"{LIST_FIELDS},body,additions,deletions,changedFiles,comments"

CHECKS_PASSING = "passing"
CHECKS_FAILING = "failing"
CHECKS_PENDING = "pending"
CHECKS_NONE = "none"

FAILED_CONCLUSIONS = frozenset(
    {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
)
FAILED_STATES = frozenset({"FAILURE", "ERROR"})
WAITING_STATES = frozenset({"PENDING", "EXPECTED"})

# devkit's merge machinery: its jobs run on every PR and say nothing about the change.
PLUMBING_WORKFLOWS = frozenset({"Auto-merge"})
E2E_NAME = re.compile(r"\be2e\b|\bend[- ]to[- ]end\b", re.IGNORECASE)
# A PR merges before its checks finish, so a merged one is asked about this much longer.
CHECKS_SETTLE = timedelta(hours=2)


class GhError(Exception):
    """`gh` failed. The text is its own, fit to show the user."""


class GhMissing(GhError):
    """`gh` could not be run at all, so asking again will not help."""


@dataclass(frozen=True)
class PullRequest:
    """One PR, as much as a row in the sidebar and a merge decision need."""

    number: int
    title: str
    url: str
    state: str
    head: str
    base: str
    draft: bool
    author: str
    checks: str
    mergeable: str
    review: str
    automerge: bool
    # Set once merged: what the main checkout must contain to be running the change.
    merge_commit: str = ""
    merged_at: str = ""
    # The last commit of the PR's branch, as GitHub has it.
    head_oid: str = ""
    # `checks` split by kind, without the merge machinery's own jobs.
    ci: str = CHECKS_NONE
    e2e: str = CHECKS_NONE

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_outcome(check: Mapping[str, Any]) -> str:
    """One rollup entry. A `CheckRun` has status and conclusion, a `StatusContext` a state."""
    if check.get("__typename") == "StatusContext":
        state = check.get("state")
        if state in FAILED_STATES:
            return CHECKS_FAILING
        return CHECKS_PENDING if state in WAITING_STATES else CHECKS_PASSING
    if check.get("status") != "COMPLETED":
        return CHECKS_PENDING
    return CHECKS_FAILING if check.get("conclusion") in FAILED_CONCLUSIONS else CHECKS_PASSING


def summarize_checks(rollup: Iterable[Mapping[str, Any]] | None) -> str:
    """All the checks in a word: any failure outweighs anything still running."""
    outcomes = {check_outcome(check) for check in rollup or ()}
    for verdict in (CHECKS_FAILING, CHECKS_PENDING, CHECKS_PASSING):
        if verdict in outcomes:
            return verdict
    return CHECKS_NONE


def split_checks(rollup: Iterable[Mapping[str, Any]] | None) -> tuple[str, str]:
    """`(ci, e2e)`: the checks that judge the change, each kind in a word."""
    ci: list[Mapping[str, Any]] = []
    e2e: list[Mapping[str, Any]] = []
    for check in rollup or ():
        workflow = str(check.get("workflowName") or "")
        if check.get("conclusion") == "SKIPPED" or workflow in PLUMBING_WORKFLOWS:
            continue
        name = check.get("name") or check.get("context") or ""
        (e2e if E2E_NAME.search(f"{workflow} {name}") else ci).append(check)
    return summarize_checks(ci), summarize_checks(e2e)


def is_settled(pr: PullRequest, now: datetime) -> bool:
    """Whether nothing more is to be heard of a PR: merged, with its checks passed or long
    enough gone. A red one may be re-run, and one just merged may have no checks yet."""
    if pr.state != MERGED:
        return False
    if pr.ci == CHECKS_PASSING and pr.e2e in (CHECKS_PASSING, CHECKS_NONE):
        return True
    try:
        return now - datetime.fromisoformat(pr.merged_at) > CHECKS_SETTLE
    except (ValueError, TypeError):
        return True


def to_pull_request(record: Mapping[str, Any]) -> PullRequest:
    """Build one from `gh`'s JSON. Raises `KeyError` or `TypeError` if it is not a PR."""
    labels = {label.get("name") for label in record.get("labels") or ()}
    ci, e2e = split_checks(record.get("statusCheckRollup"))
    return PullRequest(
        number=int(record["number"]),
        title=str(record["title"]),
        url=str(record["url"]),
        state=str(record["state"]),
        head=str(record["headRefName"]),
        base=str(record.get("baseRefName") or ""),
        draft=bool(record.get("isDraft")),
        author=str((record.get("author") or {}).get("login") or ""),
        checks=summarize_checks(record.get("statusCheckRollup")),
        mergeable=str(record.get("mergeable") or ""),
        review=str(record.get("reviewDecision") or ""),
        automerge="automerge" in labels,
        merge_commit=str((record.get("mergeCommit") or {}).get("oid") or ""),
        merged_at=str(record.get("mergedAt") or ""),
        head_oid=str(record.get("headRefOid") or ""),
        ci=ci,
        e2e=e2e,
    )


def parse_pull_requests(text: str) -> list[PullRequest]:
    """A `gh pr list --json` reply. Raises `GhError` on anything else."""
    try:
        return [to_pull_request(record) for record in json.loads(text)]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise GhError(f"unexpected reply from gh: {exc}") from exc


def check_rows(rollup: Iterable[Mapping[str, Any]] | None) -> list[dict[str, str]]:
    """Each check for the review page: skipped ones say nothing about the change."""
    rows = []
    for check in rollup or ():
        if check.get("conclusion") == "SKIPPED":
            continue
        name = check.get("name") or check.get("context") or "check"
        url = check.get("detailsUrl") or check.get("targetUrl") or ""
        rows.append({"name": str(name), "outcome": check_outcome(check), "url": str(url)})
    return rows


class Gh:
    """Runs `gh` in a repository. Blocking, so call it off the event loop."""

    def __init__(self, settings: Settings) -> None:
        self._command = settings.gh_command

    def run(self, repository: Path, *args: str, timeout: float = GH_TIMEOUT_SECONDS) -> str:
        """`gh`'s output. Raises `GhError` with what it said, or `GhMissing`."""
        try:
            done = subprocess.run(  # noqa: S603 - fixed subcommands; no shell, numbers validated
                [self._command, *args],
                cwd=repository,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=NO_WINDOW,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GhError(f"gh took over {timeout:g} seconds") from exc
        except OSError as exc:
            raise GhMissing(f"could not run gh: {exc}") from exc
        if done.returncode != 0:
            said = (done.stderr or done.stdout or "").strip()
            raise GhError(said or f"gh exited {done.returncode}")
        return done.stdout

    def list_open(self, repository: Path) -> list[PullRequest]:
        """The repository's open PRs, newest first."""
        reply = self.run(
            repository,
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            LIST_LIMIT,
            "--json",
            LIST_FIELDS,
        )
        return parse_pull_requests(reply)

    def for_branch(self, repository: Path, branch: str) -> PullRequest | None:
        """The newest PR headed by `branch` in any state: one that has merged no longer
        shows among the open ones, and a tab still wants to know."""
        reply = self.run(
            repository,
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "all",
            "--limit",
            "1",
            "--json",
            LIST_FIELDS,
        )
        found = parse_pull_requests(reply)
        return found[0] if found else None

    def detail(self, repository: Path, number: int) -> dict[str, Any]:
        """Everything the review page shows of one PR."""
        reply = self.run(repository, "pr", "view", str(number), "--json", DETAIL_FIELDS)
        try:
            record = json.loads(reply)
            summary = to_pull_request(record)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise GhError(f"unexpected reply from gh: {exc}") from exc
        diff = self.run(repository, "pr", "diff", str(number))
        return {
            **summary.to_dict(),
            "body": str(record.get("body") or ""),
            "additions": int(record.get("additions") or 0),
            "deletions": int(record.get("deletions") or 0),
            "changed_files": int(record.get("changedFiles") or 0),
            "merge_state": str(record.get("mergeStateStatus") or ""),
            "check_rows": check_rows(record.get("statusCheckRollup")),
            "comments": [
                {
                    "author": str((comment.get("author") or {}).get("login") or ""),
                    "body": str(comment.get("body") or ""),
                }
                for comment in record.get("comments") or ()
            ],
            "diff": diff[:MAX_DIFF_CHARACTERS],
            "diff_truncated": len(diff) > MAX_DIFF_CHARACTERS,
        }

    def merge(self, repository: Path, number: int) -> None:
        """Squash-merge, as the harness's own merges do. The branch is left to the repo's
        delete-on-merge setting: `--delete-branch` would also try to delete it locally,
        where it is usually a worktree's, and `worktree.py reconcile` owns those."""
        self.run(repository, "pr", "merge", str(number), "--squash")

    def close(self, repository: Path, number: int) -> None:
        """Close without merging."""
        self.run(repository, "pr", "close", str(number))

    def list_repos(self, where: Path) -> str:
        """The signed-in user's own repositories, as `gh repo list --json` gives them.
        `repos.parse_repos` reads the reply."""
        return self.run(where, "repo", "list", "--limit", REPO_LIMIT, "--json", REPO_FIELDS)

    def clone(self, full_name: str, destination: Path) -> None:
        """Clone `owner/name` into `destination`, which must not exist. The name is checked
        by `repos.clone_target` first, so it cannot be read as an option."""
        self.run(
            destination.parent,
            "repo",
            "clone",
            full_name,
            str(destination),
            timeout=CLONE_TIMEOUT_SECONDS,
        )

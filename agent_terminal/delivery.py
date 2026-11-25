"""How far a tab's work has got: what is still to ship, and what has judged what did.

Nothing holds a merge back here, so a PR's checks, the machine's own test tiers and the
pull into the main checkout all finish in their own time, mostly after the merge. Each is
a gate of its own. GitHub answers for CI. devkit's scheduled tiers leave a result file per
project (its `scripts/e2e_results.py`, which this reads after and does not import), naming
the commit they tested: a tier has judged a change once that commit contains its merge.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_terminal import agents, gitinfo, shipping
from agent_terminal.gitinfo import GitError
from agent_terminal.prs import (
    CHECKS_FAILING,
    CHECKS_NONE,
    CHECKS_PASSING,
    CHECKS_PENDING,
    MERGED,
    OPEN,
    PullRequest,
)

# What the tab's button does. The page names them too.
NO_ACTION = ""
CREATE = "create"
PUSH = "push"
FOLLOW_UP = "follow_up"

TESTS_RESULTS = Path("logs") / "medium-tier"
E2E_RESULTS = Path("logs") / "e2e"
E2E_SCRIPT = Path("scripts") / "e2e.py"
SKILL_FOLDERS = {agents.CLAUDE: Path(".claude"), agents.CODEX: Path(".agents")}
LAND_SKILL = Path("skills") / "land" / "SKILL.md"
DEFAULT_REFS = ("origin/HEAD", "origin/main", "origin/master")


@dataclass(frozen=True)
class Gate:
    """One indicator: which, how it stands (a `CHECKS_*` word), and why, for the tooltip."""

    name: str
    state: str
    detail: str


@dataclass(frozen=True)
class Progress:
    """What a tab shows beside its PR."""

    land: str = NO_ACTION
    gates: tuple[Gate, ...] = ()


def has_land_skill(tree: Path, agent: str) -> bool:
    """Whether `agent` finds devkit's land skill in `tree`."""
    folder = SKILL_FOLDERS.get(agent)
    return folder is not None and (tree / folder / LAND_SKILL).is_file()


def default_ref(tree: Path) -> str | None:
    """The default branch on `origin`, or None with nowhere to ship to."""
    for ref in DEFAULT_REFS:
        try:
            gitinfo.run_git(tree, "rev-parse", "--verify", "--quiet", ref)
        except GitError:
            continue
        return ref
    return None


def has_unshipped(tree: Path, pr: PullRequest | None) -> bool:
    """Whether `tree` holds work its PR has not, or any at all with no PR. A squash-merge
    leaves the branch's own commits on no remote ref, so the PR's last commit is what says
    they have shipped. Blocking: git."""
    base = default_ref(tree)
    if base is None:
        return False
    shipped = [base]
    if pr is not None:
        # The remote refs too: they know of a push before the next poll does.
        shipped += ["--remotes=origin", *([pr.head_oid] if pr.head_oid else [])]
    try:
        # No optional locks: refreshing the index here could fail the agent's own git.
        if gitinfo.run_git(tree, "--no-optional-locks", "status", "--porcelain").strip():
            return True
        count = gitinfo.run_git(
            tree, "rev-list", "--count", "--max-count=1", "HEAD", "--not", *shipped
        )
    except GitError:
        # Such as a PR commit pushed from elsewhere and not fetched: nothing is known.
        return False
    return int(count) > 0


def land_action(pr: PullRequest | None) -> str:
    """What shipping a tab's new work means. A merged PR takes no more commits, so the
    work goes into a new one."""
    if pr is not None and pr.state == OPEN:
        return PUSH
    if pr is not None and pr.state == MERGED:
        return FOLLOW_UP
    return CREATE


def land_prompt(agent: str, action: str, pr: PullRequest | None) -> str:
    """The line typed to the agent. One line: a newline would submit half of it. Nothing
    from the PR's own text goes in, as whoever opened it wrote that."""
    if action == FOLLOW_UP and pr is not None and gitinfo.COMMIT_HASH.fullmatch(pr.head_oid):
        return (
            f"PR #{pr.number} from this branch has already merged (its last commit was"
            f" {pr.head_oid}). Ship only the work done since, as a new PR: switch to a fresh"
            " branch, commit any uncommitted changes there, fetch origin, run git rebase"
            f" --onto the default branch on origin from {pr.head_oid}, then use the land"
            " skill."
        )
    if agent == agents.CLAUDE:
        return "/land"
    return "Use the land skill to ship the work in this checkout."


def read_tier(results: Path, project: str) -> dict[str, Any] | None:
    """A tier's last result for a project, or None if there is none to be read."""
    try:
        loaded = json.loads((results / project / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def tier_state(repository: Path, merge_commit: str, result: dict[str, Any] | None) -> str:
    """What a tier's last run says of a merged change. Red covers everything merged since
    the run before, so it may be another change's doing. Blocking: git."""

    def covers(key: str) -> bool:
        return gitinfo.contains_commit(repository, merge_commit, str((result or {}).get(key, "")))

    status = (result or {}).get("status")
    if covers("last_green") or (status == "passed" and covers("sha")):
        return CHECKS_PASSING
    return CHECKS_FAILING if status == "failed" and covers("sha") else CHECKS_PENDING


def worst(*states: str) -> str:
    """Several verdicts as one: any failure outweighs anything still to come."""
    for state in (CHECKS_FAILING, CHECKS_PENDING, CHECKS_PASSING):
        if state in states:
            return state
    return CHECKS_NONE


DETAILS = {
    "ci": {
        CHECKS_PASSING: "CI passed",
        CHECKS_FAILING: "CI failed",
        CHECKS_PENDING: "CI is running",
    },
    "tests": {
        CHECKS_PASSING: "Local tests passed with this change",
        CHECKS_FAILING: "Local tests failed on a commit that includes this change",
        CHECKS_PENDING: "Local tests have not run with this change yet",
    },
    "e2e": {
        CHECKS_PASSING: "E2E passed with this change",
        CHECKS_FAILING: "E2E failed on a commit that includes this change",
        CHECKS_PENDING: "E2E has not run with this change yet",
    },
    "live": {
        CHECKS_PASSING: "Live locally: the main checkout has this change",
        CHECKS_PENDING: "Not live locally: the main checkout does not have this change yet",
    },
}
OWN_LIVE_DETAILS = {
    CHECKS_PASSING: "Live locally: this app is running this change",
    CHECKS_PENDING: "Not live locally: update or restart this app to run this change",
}


class DeliveryReader:
    """Reads tabs' progress. Blocking throughout. `app_root` is the checkout this app runs
    from: pulling there changes nothing until a restart, so its commit is read once, now."""

    def __init__(self, devkit: Path, app_root: Path) -> None:
        self._devkit = devkit
        self._app_repository = gitinfo.main_checkout(app_root)
        self._app_commit = ""
        try:
            self._app_commit = gitinfo.run_git(app_root, "rev-parse", "HEAD").strip()
        except GitError:
            self._app_repository = None
        # A later run going red is a later change's doing, so a pass once seen stands.
        self._passed: set[tuple[str, str]] = set()

    def read(
        self, tree: Path, agent: str, repository: Path | None, ship: str, pr: PullRequest | None
    ) -> Progress:
        """One tab's progress."""
        if repository is None:
            return Progress()
        land = NO_ACTION
        # An intent is already on its way: the fix pass commits whatever is there.
        if ship != shipping.INTENT and has_land_skill(tree, agent) and has_unshipped(tree, pr):
            land = land_action(pr)
        return Progress(land, self.gates(repository, pr) if pr else ())

    def gates(self, repository: Path, pr: PullRequest) -> tuple[Gate, ...]:
        """The PR's indicators, leaving out any the project has no such check for."""
        states = {"ci": pr.ci, "tests": CHECKS_NONE, "e2e": pr.e2e, "live": CHECKS_NONE}
        if pr.state in (OPEN, MERGED):
            states["tests"] = self._tier("tests", TESTS_RESULTS, repository, pr)
            states["e2e"] = worst(pr.e2e, self._tier("e2e", E2E_RESULTS, repository, pr))
            states["live"] = self._live(repository, pr)
        details = dict(DETAILS)
        if repository == self._app_repository:
            details["live"] = OWN_LIVE_DETAILS
        return tuple(
            Gate(name, state, details[name][state])
            for name, state in states.items()
            if state != CHECKS_NONE
        )

    def _tier(self, name: str, results: Path, repository: Path, pr: PullRequest) -> str:
        result = read_tier(self._devkit / results, repository.name)
        if result is None and not (name == "e2e" and (repository / E2E_SCRIPT).is_file()):
            return CHECKS_NONE
        if pr.state != MERGED:
            return CHECKS_PENDING
        key = (name, pr.merge_commit)
        if key in self._passed:
            return CHECKS_PASSING
        state = tier_state(repository, pr.merge_commit, result)
        if state == CHECKS_PASSING:
            self._passed.add(key)
        return state

    def _live(self, repository: Path, pr: PullRequest) -> str:
        if pr.state != MERGED:
            return CHECKS_PENDING
        running = self._app_commit if repository == self._app_repository else "HEAD"
        live = gitinfo.contains_commit(repository, pr.merge_commit, running)
        return CHECKS_PASSING if live else CHECKS_PENDING

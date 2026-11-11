"""Every PR the user might care about, and which tab each belongs to.

Two kinds of looking, at two speeds. A tab's branch and ship intent are small local files,
read every few seconds. GitHub is asked once a poll, per repository. A tab and a PR are
tied by branch alone, which holds for a session started outside this app and for one the
fix pass renamed: the name is read from the tree each time, never remembered.

All `gh` and git work happens off the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_terminal import agents, gitinfo, shipping
from agent_terminal.folders import display_name
from agent_terminal.projects import list_projects
from agent_terminal.prompts import Prompt, find_prompt
from agent_terminal.prs import MERGED, Gh, GhError, GhMissing, PullRequest
from agent_terminal.settings import Settings
from agent_terminal.store import Tab

log = logging.getLogger(__name__)

LOCAL_SCAN_SECONDS = 5.0
GH_CONCURRENCY = 4
# A tab on these is working on the trunk, and a lookup for a PR headed by it finds nothing.
TRUNK_BRANCHES = frozenset({"main", "master"})


@dataclass(frozen=True)
class TabFacts:
    """What a tab's folder says right now."""

    repository: Path | None = None
    branch: str | None = None
    ship: str = shipping.NOT_SHIPPING


@dataclass(frozen=True)
class OpenPrompt:
    """A worktree a PR's branch is checked out in, and the session found there."""

    tree: Path
    prompt: Prompt


@dataclass(frozen=True)
class BoardLinks:
    """How the board reaches the rest of the app."""

    tabs: Callable[[], list[Tab]]
    changed: Callable[[], None]


def read_tab_facts(tab: Tab) -> TabFacts:
    """Read a tab's folder. Blocking, though only just."""
    tree = agents.tab_tree(tab)
    return TabFacts(
        repository=gitinfo.main_checkout(tree) or gitinfo.main_checkout(Path(tab.project_path)),
        branch=gitinfo.current_branch(tree),
        ship=shipping.ship_stage(tree),
    )


class PrBoard:
    """Keeps the PR picture current. `pr_requests.py` acts on it."""

    def __init__(
        self,
        settings: Settings,
        loop: asyncio.AbstractEventLoop,
        links: BoardLinks,
        gh: Gh | None = None,
    ) -> None:
        self._settings = settings
        self._loop = loop
        self._links = links
        self._gh = gh or Gh(settings)
        self._open: dict[Path, list[PullRequest]] = {}
        self._by_branch: dict[tuple[Path, str], PullRequest] = {}
        self._prompts: dict[tuple[Path, int], OpenPrompt] = {}
        self._facts: dict[str, TabFacts] = {}
        self._complained: set[str] = set()
        self._gh_missing = False
        self._task: asyncio.Task[None] | None = None

    # --- what the page is told ---

    def tab_fields(self, tab: Tab) -> dict[str, Any]:
        """A tab's place in the shipping process, for the state message."""
        facts = self._facts.get(tab.id, TabFacts())
        found = self._pr_of(facts)
        pr = {**found.to_dict(), "repo": str(facts.repository)} if found else None
        return {"ship": facts.ship, "branch": facts.branch, "pr": pr}

    def _pr_of(self, facts: TabFacts) -> PullRequest | None:
        if facts.repository is None or facts.branch is None:
            return None
        for pr in self._open.get(facts.repository, ()):
            if pr.head == facts.branch:
                return pr
        return self._by_branch.get((facts.repository, facts.branch))

    def plain_prs(self) -> list[dict[str, Any]]:
        """Open PRs no tab holds. A session that has its PR shows it on the tab instead."""
        held = {(f.repository, f.branch) for f in self._facts.values()}
        return [
            {
                **pr.to_dict(),
                "repo": str(repository),
                "project": display_name(repository),
                "has_prompt": (repository, pr.number) in self._prompts,
            }
            for repository, open_prs in self._open.items()
            for pr in open_prs
            if (repository, pr.head) not in held
        ]

    # --- keeping it current ---

    def repositories(self) -> list[Path]:
        """Every repository with a tab, and every one under the projects root."""
        found: dict[Path, None] = {}
        roots = [Path(p.path) for p in list_projects(self._settings.projects_root) if p.is_git]
        for folder in [*roots, *(Path(tab.project_path) for tab in self._links.tabs())]:
            repository = gitinfo.main_checkout(folder)
            if repository is not None:
                found[repository] = None
        return list(found)

    def start(self) -> None:
        """Begin watching, once."""
        if self._task is None:
            self._task = self._loop.create_task(self._watch())

    def stop(self) -> None:
        """Stop watching."""
        if self._task is not None:
            self._task.cancel()

    async def _watch(self) -> None:
        until_poll = 0.0
        while True:
            if until_poll <= 0:
                await self.refresh()
                until_poll = self._settings.pr_poll_seconds
            await self.scan()
            await asyncio.sleep(LOCAL_SCAN_SECONDS)
            until_poll -= LOCAL_SCAN_SECONDS

    async def scan(self) -> None:
        """Read every tab's folder. A branch not seen before is looked up at once, so a
        session's PR does not take a poll to appear on it."""
        tabs = self._links.tabs()
        read = await self._loop.run_in_executor(None, lambda: [read_tab_facts(t) for t in tabs])
        facts = {tab.id: found for tab, found in zip(tabs, read, strict=True)}
        if facts == self._facts:
            return
        known = set(self._facts.values())
        self._facts = facts
        await self._look_up_branches(found for found in facts.values() if found not in known)
        self._links.changed()

    async def refresh(self, repository: Path | None = None) -> None:
        """Ask GitHub about one repository, or all of them."""
        if self._gh_missing:
            return
        targets = [repository] if repository else await self._all_repositories()
        limit = asyncio.Semaphore(GH_CONCURRENCY)

        async def one(target: Path) -> None:
            async with limit:
                await self._refresh_repository(target)

        await asyncio.gather(*(one(target) for target in targets))
        await self._look_up_branches(self._facts.values())
        self._links.changed()

    async def _all_repositories(self) -> list[Path]:
        return await self._loop.run_in_executor(None, self.repositories)

    async def _refresh_repository(self, repository: Path) -> None:
        open_prs = await self._ask(self._gh.list_open, repository)
        if open_prs is None:
            return
        self._open[repository] = open_prs
        found = await self._loop.run_in_executor(None, self._find_prompts, repository, open_prs)
        for number in [key for key in self._prompts if key[0] == repository]:
            del self._prompts[number]
        self._prompts.update(found)

    def _find_prompts(
        self, repository: Path, open_prs: list[PullRequest]
    ) -> dict[tuple[Path, int], OpenPrompt]:
        trees = gitinfo.worktrees(repository)
        found: dict[tuple[Path, int], OpenPrompt] = {}
        for pr in open_prs:
            tree = trees.get(pr.head)
            prompt = find_prompt(self._settings, tree) if tree else None
            if tree and prompt:
                found[(repository, pr.number)] = OpenPrompt(tree, prompt)
        return found

    async def _look_up_branches(self, facts: Any) -> None:
        """Find the PR of each branch that heads no open one: merged, closed, or none. A
        merged PR stays merged, so it is asked about once."""
        wanted = {(f.repository, f.branch) for f in facts if f.repository and f.branch}
        for repository, branch in wanted:
            if branch in TRUNK_BRANCHES or self._pr_is_settled(repository, branch):
                continue
            if any(pr.head == branch for pr in self._open.get(repository, ())):
                continue
            found = await self._ask(self._gh.for_branch, repository, branch)
            if found is not None:
                self._by_branch[(repository, branch)] = found

    def _pr_is_settled(self, repository: Path, branch: str) -> bool:
        known = self._by_branch.get((repository, branch))
        return known is not None and known.state == MERGED

    async def _ask(self, call: Callable[..., Any], *args: Any) -> Any:
        """Run a `gh` call off the loop. A failure is logged once per message and reads as
        no answer, leaving what was last known in place."""
        if self._gh_missing:
            return None
        try:
            return await self._loop.run_in_executor(None, call, *args)
        except GhMissing as exc:
            log.warning("no PRs without gh: %s", exc)
            self._gh_missing = True
        except GhError as exc:
            if str(exc) not in self._complained:
                self._complained.add(str(exc))
                log.warning("gh failed for %s: %s", args[0], exc)
        return None

    # --- what `PrRequests` needs to know ---

    def prompt_of(self, repository: Path, number: int) -> OpenPrompt | None:
        """The session found for a PR, if any."""
        return self._prompts.get((repository, number))

    def title_of(self, repository: Path, number: int) -> str:
        """An open PR's title, or nothing if it is not one."""
        return next((p.title for p in self._open.get(repository, ()) if p.number == number), "")

    def forget(self, repository: Path, number: int) -> None:
        """Drop what is known of a PR that has just changed state, so the next lookup asks."""
        for key, pr in list(self._by_branch.items()):
            if key[0] == repository and pr.number == number:
                del self._by_branch[key]

"""Every PR the user might care about, and which tab each belongs to.

Two kinds of looking, at two speeds. A tab's branch and ship intent are small local files,
read every few seconds. GitHub is asked once a poll, per repository. A tab and a PR are
tied by branch alone, which holds for a session started outside this app and for one the
fix pass renamed: the name is read from the tree each time, never remembered.

When the tree's branch heads no PR, the tab shows the PR of the branch its session was
last on, as its transcript says (`session_branch.py`): a session may have landed from a
subagent's worktree, or the harness may have reaped its own.

All `gh` and git work happens off the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_terminal import agents, gitinfo, shipping
from agent_terminal.delivery import NO_ACTION, DeliveryReader, Progress
from agent_terminal.folders import display_name
from agent_terminal.projects import list_projects
from agent_terminal.prompts import Prompt, find_prompt
from agent_terminal.prs import Gh, GhError, GhMissing, PullRequest, is_settled
from agent_terminal.session_branch import TRUNK_BRANCHES, SessionBranches
from agent_terminal.settings import Settings
from agent_terminal.store import Tab
from agent_terminal.updates import APP_ROOT

log = logging.getLogger(__name__)

LOCAL_SCAN_SECONDS = 5.0
# Runs git in every tab's tree, so not as often as the files are read.
DELIVERY_SECONDS = 15.0
GH_CONCURRENCY = 4


@dataclass(frozen=True)
class TabFacts:
    """What a tab's folder says right now, and where its session last worked."""

    repository: Path | None = None
    branch: str | None = None
    ship: str = shipping.NOT_SHIPPING
    session_repository: Path | None = None
    session_branch: str | None = None


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


def read_tab_facts(tab: Tab, sessions: SessionBranches | None = None) -> TabFacts:
    """Read a tab's folder, and its session's transcript if given. Blocking, though only
    just."""
    tree = agents.tab_tree(tab)
    session_repository, session_branch = sessions.of(tab) if sessions else (None, None)
    return TabFacts(
        repository=gitinfo.main_checkout(tree) or gitinfo.main_checkout(Path(tab.project_path)),
        branch=gitinfo.current_branch(tree),
        ship=shipping.ship_stage(tree),
        session_repository=session_repository,
        session_branch=session_branch,
    )


def branch_pairs(facts: Iterable[TabFacts]) -> list[tuple[Path | None, str | None]]:
    """Every `(repository, branch)` the tabs could show a PR for."""
    pairs: list[tuple[Path | None, str | None]] = []
    for found in facts:
        pairs += [(found.repository, found.branch), (found.session_repository, found.session_branch)]
    return pairs


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
        self._sessions = SessionBranches(settings.claude_home)
        self._delivery = DeliveryReader(settings.devkit_root, APP_ROOT)
        self._progress: dict[str, Progress] = {}
        self._delivery_started = self._delivery_stored = 0
        # Tabs whose agent was asked to land and has not been caught up with since.
        self._landing: set[str] = set()
        self._complained: set[str] = set()
        self._gh_missing = False
        self._task: asyncio.Task[None] | None = None

    # --- what the page is told ---

    def tab_fields(self, tab: Tab) -> dict[str, Any]:
        """A tab's place in the shipping process, for the state message."""
        facts = self._facts.get(tab.id, TabFacts())
        repository, found = self.shown_pr(facts)
        pr = {**found.to_dict(), "repo": str(repository)} if found else None
        progress = asdict(self._progress.get(tab.id, Progress()))
        if tab.id in self._landing:
            # Its PR is on its way: until it is known, the work reads as never shipped.
            progress["land"] = NO_ACTION
        return {"ship": facts.ship, "branch": facts.branch, "pr": pr, **progress}

    def pr_of_tab(self, tab_id: str) -> PullRequest | None:
        """The PR a tab's own branch heads, as last known. What landing its tree's work
        builds on, so never its session's PR from another branch."""
        facts = self._facts.get(tab_id, TabFacts())
        return self.pr_for(facts.repository, facts.branch)

    def shown_pr(self, facts: TabFacts) -> tuple[Path | None, PullRequest | None]:
        """The PR a tab shows, and its repository: its tree's branch's, else the one of
        the branch its session was last on."""
        found = self.pr_for(facts.repository, facts.branch)
        if found is not None:
            return facts.repository, found
        found = self.pr_for(facts.session_repository, facts.session_branch)
        return (facts.session_repository, found) if found else (None, None)

    def pr_for(self, repository: Path | None, branch: str | None) -> PullRequest | None:
        """The PR `branch` heads, as last known: open first, else merged or closed."""
        if repository is None or branch is None:
            return None
        for pr in self._open.get(repository, ()):
            if pr.head == branch:
                return pr
        return self._by_branch.get((repository, branch))

    def plain_prs(self) -> list[dict[str, Any]]:
        """Open PRs no tab holds. A session that has its PR shows it on the tab instead."""
        held = set(branch_pairs(self._facts.values()))
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
        until_poll = until_delivery = 0.0
        while True:
            if until_poll <= 0:
                await self.refresh()
                until_poll = self._settings.pr_poll_seconds
                until_delivery = 0.0
            await self.scan()
            if until_delivery <= 0:
                await self.check_delivery()
                until_delivery = DELIVERY_SECONDS
            await asyncio.sleep(LOCAL_SCAN_SECONDS)
            until_poll -= LOCAL_SCAN_SECONDS
            until_delivery -= LOCAL_SCAN_SECONDS

    async def scan(self) -> None:
        """Read every tab's folder. A branch not seen before is looked up at once, so a
        session's PR does not take a poll to appear on it."""
        tabs = self._links.tabs()
        sessions = self._sessions

        def read_all() -> list[TabFacts]:
            sessions.forget_others({t.session_id for t in tabs if t.session_id})
            return [read_tab_facts(t, sessions) for t in tabs]

        read = await self._loop.run_in_executor(None, read_all)
        facts = {tab.id: found for tab, found in zip(tabs, read, strict=True)}
        if facts == self._facts:
            return
        known = set(self._facts.values())
        self._facts = facts
        await self.look_up_branches(branch_pairs(f for f in facts.values() if f not in known))
        self._links.changed()

    async def check_delivery(self) -> None:
        """See what each tab still has to ship, and what has judged what it shipped."""
        wanted = []
        for tab in self._links.tabs():
            facts = self._facts.get(tab.id, TabFacts())
            pr = self.pr_of_tab(tab.id)
            shown = self.shown_pr(facts) if pr is None else (None, None)
            wanted.append((tab.id, agents.tab_tree(tab), tab.agent, facts, pr, shown))
        delivery = self._delivery

        def read_one(
            tree: Path,
            agent: str,
            facts: TabFacts,
            pr: PullRequest | None,
            shown: tuple[Path | None, PullRequest | None],
        ) -> Progress:
            progress = delivery.read(tree, agent, facts.repository, facts.ship, pr)
            repository, session_pr = shown
            if repository is None or session_pr is None:
                return progress
            # The session's PR is from another branch: its gates show, but what landing
            # the tree's own work means is the tree's to say.
            return replace(progress, gates=delivery.gates(repository, session_pr))

        def read_all() -> dict[str, Progress]:
            return {
                tab_id: read_one(tree, agent, facts, pr, shown)
                for tab_id, tree, agent, facts, pr, shown in wanted
            }

        self._delivery_started += 1
        started = self._delivery_started
        progress = await self._loop.run_in_executor(None, read_all)
        # A read begun before a later one knew less, so it never overwrites it.
        if started < self._delivery_stored:
            return
        self._delivery_stored = started
        if progress != self._progress:
            self._progress = progress
            self._links.changed()

    def hold_land(self, tab_id: str) -> None:
        """Offer no land on a tab whose agent is landing: when it is done, its work reads
        as unshipped until a poll finds the PR it made."""
        self._landing.add(tab_id)
        self._links.changed()

    async def release_land(self, tab: Tab) -> None:
        """Catch up on a tab whose agent has finished landing, then offer what is left."""
        try:
            await self.scan()
            facts = self._facts.get(tab.id, TabFacts())
            # The land may have run in another repository's checkout than the tab's.
            for repository in dict.fromkeys((facts.repository, facts.session_repository)):
                if repository is not None:
                    await self.refresh(repository)
            await self.check_delivery()
        finally:
            self._landing.discard(tab.id)
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
        await self.look_up_branches(branch_pairs(self._facts.values()))
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

    async def look_up_branches(self, pairs: Iterable[tuple[Path | None, str | None]]) -> None:
        """Find the PR of each `(repository, branch)` that heads no open one: merged,
        closed, or none. A merged one is asked about until its checks have settled."""
        wanted = {(repository, branch) for repository, branch in pairs if repository and branch}
        for repository, branch in wanted:
            known = self._by_branch.get((repository, branch))
            if branch in TRUNK_BRANCHES or (known and is_settled(known, datetime.now(UTC))):
                continue
            if any(pr.head == branch for pr in self._open.get(repository, ())):
                continue
            found = await self._ask(self._gh.for_branch, repository, branch)
            if found is not None:
                self._by_branch[(repository, branch)] = found

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

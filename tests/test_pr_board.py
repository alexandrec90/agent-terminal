import asyncio
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_terminal.gitinfo import main_checkout
from agent_terminal.pr_board import BoardLinks, OpenPrompt, PrBoard, TabFacts, read_tab_facts
from agent_terminal.pr_requests import PrRequests
from agent_terminal.prompts import Prompt, claude_transcript_dir
from agent_terminal.prs import Gh, GhError, GhMissing, PullRequest
from agent_terminal.settings import Settings
from agent_terminal.store import Tab


def make_pr(number: int = 1, head: str = "worktree-a", state: str = "OPEN") -> PullRequest:
    return PullRequest(
        number=number,
        title=f"PR {number}",
        url=f"https://github.com/o/r/pull/{number}",
        state=state,
        head=head,
        base="main",
        draft=False,
        author="someone",
        checks="passing",
        mergeable="MERGEABLE",
        review="",
        automerge=False,
    )


class FakeGh(Gh):
    """Answers as `Gh` does, and records what it was told to do."""

    def __init__(self) -> None:
        super().__init__(Settings())
        self.open: dict[Path, list[PullRequest]] = {}
        self.branches: dict[str, PullRequest] = {}
        self.branch_calls: list[str] = []
        self.list_error: Exception | None = None
        self.action_error: Exception | None = None
        self.merged: list[int] = []
        self.closed: list[int] = []

    def list_open(self, repository: Path) -> list[PullRequest]:
        if self.list_error:
            raise self.list_error
        return self.open.get(repository, [])

    def for_branch(self, repository: Path, branch: str) -> PullRequest | None:
        self.branch_calls.append(branch)
        return self.branches.get(branch)

    def detail(self, repository: Path, number: int) -> dict[str, Any]:
        return {"number": number}

    def merge(self, repository: Path, number: int) -> None:
        if self.action_error:
            raise self.action_error
        self.merged.append(number)
        self.branches["worktree-a"] = make_pr(number, state="MERGED")
        self.open[repository] = []

    def close(self, repository: Path, number: int) -> None:
        if self.action_error:
            raise self.action_error
        self.closed.append(number)


def git(folder: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=folder, check=True, capture_output=True)  # noqa: S607 - fixed test commands


class Rig:
    def __init__(self, tmp_path: Path, loop: asyncio.AbstractEventLoop) -> None:
        self.settings = Settings(
            projects_root=tmp_path / "projects",
            claude_home=tmp_path / "claude",
            codex_home=tmp_path / "codex",
        )
        self.repository = self.settings.projects_root / "repo"
        self.repository.mkdir(parents=True)
        git(self.repository, "init", "--initial-branch", "main")
        git(self.repository, "config", "user.email", "t@example.com")
        git(self.repository, "config", "user.name", "T")
        git(self.repository, "commit", "--allow-empty", "--message", "first")
        self.gh = FakeGh()
        self.tabs: list[Tab] = []
        self.changes = 0
        self.messages: list[dict[str, Any]] = []
        self.board = PrBoard(
            self.settings, loop, BoardLinks(lambda: self.tabs, self.note_change), self.gh
        )
        self.requests = PrRequests(self.board, self.gh, loop, self.messages.append)

    def note_change(self) -> None:
        self.changes += 1

    def worktree(self, name: str) -> Path:
        tree = self.repository / ".claude" / "worktrees" / name
        git(self.repository, "worktree", "add", str(tree), "-b", f"worktree-{name}")
        return tree

    def tab(self, tree: Path, tab_id: str = "t") -> Tab:
        tab = Tab(
            id=tab_id,
            project="repo",
            project_path=str(self.repository),
            agent="claude",
            created_at=0.0,
            cwd=str(tree),
        )
        self.tabs.append(tab)
        return tab

    def message(self, kind: str, number: int = 1) -> dict[str, Any]:
        return {"type": kind, "repo": str(self.repository), "number": number}

    def key(self) -> Path:
        repository = main_checkout(self.repository)
        assert repository is not None
        return repository


def run(body):
    def wrapper(tmp_path):
        async def go():
            await body(Rig(tmp_path, asyncio.get_running_loop()))

        asyncio.run(go())

    wrapper.__name__ = body.__name__
    return wrapper


def test_read_tab_facts_finds_branch_and_repository(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    git(repository, "init", "--initial-branch", "main")
    tab = Tab(id="t", project="r", project_path=str(repository), agent="claude", created_at=0.0)
    assert read_tab_facts(tab) == TabFacts(main_checkout(repository), "main", "")


def test_read_tab_facts_outside_git(tmp_path):
    tab = Tab(id="t", project="r", project_path=str(tmp_path), agent="claude", created_at=0.0)
    assert read_tab_facts(tab) == TabFacts()


@run
async def test_lists_every_repository_under_the_root_and_with_a_tab(rig):
    outside = rig.settings.projects_root.parent / "outside"
    outside.mkdir()
    git(outside, "init")
    rig.tab(outside)
    rig.tabs[0].project_path = str(outside)
    (rig.settings.projects_root / "plain").mkdir()
    found = rig.board.repositories()
    assert found == [rig.key(), outside]


@run
async def test_a_subfolder_tab_does_not_make_a_second_repository(rig):
    below = rig.repository / "sub"
    below.mkdir()
    tab = rig.tab(below)
    tab.project_path = str(below)
    assert rig.board.repositories() == [rig.key()]


@run
async def test_an_open_pr_with_no_tab_is_a_plain_pr(rig):
    rig.gh.open[rig.key()] = [make_pr(7)]
    await rig.board.refresh()
    plain = rig.board.plain_prs()
    assert [p["number"] for p in plain] == [7]
    assert plain[0]["project"] == "repo"
    assert plain[0]["repo"] == str(rig.key())
    assert plain[0]["has_prompt"] is False


@run
async def test_a_tab_on_a_prs_branch_holds_it(rig):
    tree = rig.worktree("a")
    tab = rig.tab(tree)
    rig.gh.open[rig.key()] = [make_pr(7)]
    await rig.board.refresh()
    await rig.board.scan()
    assert rig.board.tab_fields(tab)["pr"]["number"] == 7
    assert rig.board.plain_prs() == []


@run
async def test_a_naked_prompt_has_neither(rig):
    tab = rig.tab(rig.worktree("a"))
    await rig.board.scan()
    assert rig.board.tab_fields(tab) == {"ship": "", "branch": "worktree-a", "pr": None}


@run
async def test_a_prompt_with_an_unreaped_intent(rig):
    tree = rig.worktree("a")
    (tree / "logs").mkdir()
    (tree / "logs" / "ship-intent.md").write_text("Fix it\n\nBecause.\n", encoding="utf-8")
    tab = rig.tab(tree)
    await rig.board.scan()
    assert rig.board.tab_fields(tab)["ship"] == "intent"


@run
async def test_a_prompt_whose_pr_merged_still_shows_it(rig):
    tab = rig.tab(rig.worktree("a"))
    rig.gh.branches["worktree-a"] = make_pr(7, state="MERGED")
    await rig.board.scan()
    assert rig.board.tab_fields(tab)["pr"]["state"] == "MERGED"


@run
async def test_a_merged_pr_is_only_asked_about_once(rig):
    rig.tab(rig.worktree("a"))
    rig.gh.branches["worktree-a"] = make_pr(7, state="MERGED")
    await rig.board.refresh()
    await rig.board.scan()
    await rig.board.refresh()
    assert rig.gh.branch_calls == ["worktree-a"]


@run
async def test_a_branch_with_no_pr_is_asked_about_again(rig):
    rig.tab(rig.worktree("a"))
    await rig.board.scan()
    await rig.board.refresh()
    assert rig.gh.branch_calls == ["worktree-a", "worktree-a"]


@run
async def test_the_trunk_is_never_looked_up(rig):
    rig.tab(rig.repository)
    await rig.board.scan()
    await rig.board.refresh()
    assert rig.gh.branch_calls == []


@run
async def test_a_branch_renamed_by_the_pass_is_followed(rig):
    tree = rig.worktree("a")
    tab = rig.tab(tree)
    rig.gh.open[rig.key()] = [make_pr(7, head="agent/a")]
    await rig.board.refresh()
    await rig.board.scan()
    assert rig.board.tab_fields(tab)["pr"] is None

    git(tree, "branch", "-m", "agent/a")
    await rig.board.scan()
    assert rig.board.tab_fields(tab)["pr"]["number"] == 7


@run
async def test_an_unchanged_scan_says_nothing(rig):
    rig.tab(rig.worktree("a"))
    await rig.board.scan()
    changes = rig.changes
    await rig.board.scan()
    assert rig.changes == changes


@run
async def test_two_tabs_on_one_branch_both_hold_its_pr(rig):
    tree = rig.worktree("a")
    first, second = rig.tab(tree, "one"), rig.tab(tree, "two")
    rig.gh.open[rig.key()] = [make_pr(7)]
    await rig.board.refresh()
    await rig.board.scan()
    assert rig.board.tab_fields(first)["pr"]["number"] == 7
    assert rig.board.tab_fields(second)["pr"]["number"] == 7


@run
async def test_a_pr_with_a_worktree_and_transcript_has_a_prompt(rig):
    tree = rig.worktree("a")
    transcripts = claude_transcript_dir(rig.settings.claude_home, tree)
    transcripts.mkdir(parents=True)
    (transcripts / "session-1.jsonl").write_text("{}\n")
    rig.gh.open[rig.key()] = [make_pr(7)]
    await rig.board.refresh()
    assert rig.board.plain_prs()[0]["has_prompt"] is True


@run
async def test_a_tab_for_a_prs_prompt_resumes_it_in_its_worktree(rig):
    tree = rig.worktree("a")
    transcripts = claude_transcript_dir(rig.settings.claude_home, tree)
    transcripts.mkdir(parents=True)
    (transcripts / "session-1.jsonl").write_text("{}\n")
    rig.gh.open[rig.key()] = [make_pr(7)]
    await rig.board.refresh()

    tab = rig.requests.tab_for_prompt(rig.message("pr_resume", 7))
    assert (tab.agent, tab.session_id) == ("claude", "session-1")
    assert tab.worktree_name == "a"
    assert Path(tab.cwd) == tree
    assert tab.title == "PR 7"
    assert Path(tab.project_path) == rig.key()


@run
async def test_the_board_answers_for_a_prs_prompt(rig):
    tree = rig.worktree("a")
    transcripts = claude_transcript_dir(rig.settings.claude_home, tree)
    transcripts.mkdir(parents=True)
    (transcripts / "session-1.jsonl").write_text("{}\n")
    rig.gh.open[rig.key()] = [make_pr(7)]
    await rig.board.refresh()
    assert rig.board.prompt_of(rig.key(), 7) == OpenPrompt(tree, Prompt("claude", "session-1"))
    assert rig.board.prompt_of(rig.key(), 8) is None


@run
async def test_a_pr_with_no_prompt_cannot_be_resumed(rig):
    rig.gh.open[rig.key()] = [make_pr(7)]
    await rig.board.refresh()
    with pytest.raises(ValueError):
        rig.requests.tab_for_prompt(rig.message("pr_resume", 7))


@run
async def test_a_repository_gh_cannot_read_keeps_what_was_known(rig):
    rig.gh.open[rig.key()] = [make_pr(7)]
    await rig.board.refresh()
    rig.gh.list_error = GhError("HTTP 502")
    await rig.board.refresh()
    assert [p["number"] for p in rig.board.plain_prs()] == [7]


@run
async def test_no_gh_stops_asking(rig):
    rig.gh.list_error = GhMissing("no gh")
    await rig.board.refresh()
    rig.gh.list_error = None
    rig.gh.open[rig.key()] = [make_pr(7)]
    await rig.board.refresh()
    assert rig.board.plain_prs() == []


@run
async def test_viewing_a_pr_sends_its_detail(rig):
    await rig.requests.handlers()["pr_view"](rig.message("pr_view", 7))
    assert rig.messages[-1]["type"] == "pr_detail"
    assert rig.messages[-1]["detail"] == {"number": 7}


@run
async def test_merging_updates_the_board(rig):
    tree = rig.worktree("a")
    tab = rig.tab(tree)
    rig.gh.open[rig.key()] = [make_pr(7)]
    await rig.board.refresh()
    await rig.board.scan()

    await rig.requests.handlers()["pr_merge"](rig.message("pr_merge", 7))

    assert rig.gh.merged == [7]
    assert rig.board.tab_fields(tab)["pr"]["state"] == "MERGED"
    assert rig.messages[-1]["type"] == "pr_detail"


@run
async def test_closing_a_pr(rig):
    await rig.requests.handlers()["pr_close"](rig.message("pr_close", 7))
    assert rig.gh.closed == [7]


@run
async def test_a_refused_merge_reaches_the_page_in_ghs_words(rig):
    rig.gh.action_error = GhError("Required status check is expected")
    await rig.requests.handlers()["pr_merge"](rig.message("pr_merge", 7))
    assert rig.messages[-1] == {
        **rig.message("pr_error", 7),
        "message": "Required status check is expected",
    }


@run
async def test_no_action_reaches_gh_for_an_unknown_repository(rig):
    message = {"type": "pr_merge", "repo": str(rig.repository.parent), "number": 7}
    with pytest.raises(ValueError):
        await rig.requests.handlers()["pr_merge"](message)
    assert rig.gh.merged == []


@run
async def test_a_number_that_is_not_one_is_refused(rig):
    with pytest.raises(ValueError):
        await rig.requests.handlers()["pr_close"]({**rig.message("pr_close"), "number": "7; rm"})
    assert rig.gh.closed == []


@run
async def test_start_watches_until_stopped(rig):
    rig.gh.open[rig.key()] = [make_pr(7)]
    rig.board.start()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if rig.board.plain_prs():
            break
    assert rig.board.plain_prs()
    rig.board.stop()


def test_pull_request_stays_comparable():
    assert replace(make_pr(), state="MERGED") != make_pr()

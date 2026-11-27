import asyncio
import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
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
        self.resumed: list[Tab] = []
        self.requests = PrRequests(self.board, self.gh, loop, self.messages.append, self.resume)

    def note_change(self) -> None:
        self.changes += 1

    async def resume(self, tab: Tab) -> None:
        self.resumed.append(tab)

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
    assert rig.board.tab_fields(tab) == {
        "ship": "",
        "branch": "worktree-a",
        "pr": None,
        "land": "",
        "gates": (),
    }


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
async def test_a_merged_pr_is_asked_about_until_its_checks_settle(rig):
    rig.tab(rig.worktree("a"))
    merged = replace(make_pr(7, state="MERGED"), merged_at=datetime.now(UTC).isoformat())
    rig.gh.branches["worktree-a"] = replace(merged, ci="pending")
    await rig.board.scan()
    rig.gh.branches["worktree-a"] = replace(merged, ci="passing")
    await rig.board.refresh()
    await rig.board.refresh()
    assert rig.gh.branch_calls == ["worktree-a", "worktree-a"]


@run
async def test_delivery_reaches_the_tab_and_says_so_once(rig):
    tree = rig.worktree("a")
    git(rig.repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    skill = tree / ".claude" / "skills" / "land" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("land")
    tab = rig.tab(tree)
    rig.gh.open[rig.key()] = [replace(make_pr(7), ci="passing")]
    await rig.board.refresh()
    await rig.board.scan()
    await rig.board.check_delivery()
    changes = rig.changes
    await rig.board.check_delivery()
    fields = rig.board.tab_fields(tab)
    assert fields["land"] == "push"
    assert [(g["name"], g["state"]) for g in fields["gates"]] == [
        ("ci", "passing"),
        ("live", "pending"),
    ]
    assert rig.changes == changes
    found = rig.board.pr_of_tab(tab.id)
    assert found is not None
    assert found.number == 7
    assert rig.board.pr_of_tab("gone") is None


def landable(rig: Rig) -> Tab:
    """A tab with the land skill and a commit that is on no PR yet."""
    tree = rig.worktree("a")
    git(rig.repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    skill = tree / ".claude" / "skills" / "land" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("land")
    git(tree, "add", ".")
    git(tree, "commit", "--message", "work")
    return rig.tab(tree)


def landed(tab: Tab) -> PullRequest:
    """The PR the land skill leaves: merged, with the tab's commit at its head."""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607 - fixed test command
        cwd=tab.cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return replace(make_pr(7, state="MERGED"), head_oid=head)


@run
async def test_a_landing_tab_offers_no_land_until_it_is_caught_up(rig):
    tab = landable(rig)
    await rig.board.scan()
    await rig.board.check_delivery()
    assert rig.board.tab_fields(tab)["land"] == "create"

    changes = rig.changes
    rig.board.hold_land(tab.id)
    assert rig.changes == changes + 1
    assert rig.board.tab_fields(tab)["land"] == ""
    # The agent is done and its PR merged, but no poll has been since.
    await rig.board.check_delivery()
    assert rig.board.tab_fields(tab)["land"] == ""

    rig.gh.branches["worktree-a"] = landed(tab)
    await rig.board.release_land(tab)
    fields = rig.board.tab_fields(tab)
    assert fields["pr"]["number"] == 7
    assert fields["land"] == ""


@run
async def test_a_land_that_made_no_pr_is_offered_again(rig):
    tab = landable(rig)
    await rig.board.scan()
    rig.board.hold_land(tab.id)
    await rig.board.release_land(tab)
    assert rig.board.tab_fields(tab)["land"] == "create"


@run
async def test_a_delivery_read_begun_earlier_never_overwrites_a_later_one(rig):
    tab = landable(rig)
    await rig.board.scan()
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    finish = asyncio.Event()
    real = loop.run_in_executor

    async def slow_first(executor, call, *args):
        result = await real(executor, call, *args)
        if not started.is_set():
            started.set()
            await finish.wait()
        return result

    setattr(loop, "run_in_executor", slow_first)  # noqa: B010 - an instance's method, for this loop only
    earlier = asyncio.create_task(rig.board.check_delivery())
    await started.wait()
    # The PR merges and a later read sees it before the earlier one is stored.
    rig.gh.branches["worktree-a"] = landed(tab)
    await rig.board.refresh()
    await rig.board.check_delivery()
    finish.set()
    await earlier
    assert rig.board.tab_fields(tab)["land"] == ""


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
    assert tab.worktree is True
    assert Path(tab.cwd) == tree
    assert tab.title == "PR 7"
    assert Path(tab.project_path) == rig.key()

    # The message itself hands that tab on, to be opened or shown.
    await rig.requests.handlers()["pr_resume"](rig.message("pr_resume", 7))
    assert [t.session_id for t in rig.resumed] == ["session-1"]


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


def session_was_on(rig: Rig, tab: Tab, *moments: tuple[Path, str]) -> None:
    """Give the tab a Claude session whose transcript was in these folders, on these
    branches, in order."""
    tab.session_id = "session-1"
    transcripts = claude_transcript_dir(rig.settings.claude_home, Path(str(tab.cwd)))
    transcripts.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"type": "assistant", "cwd": str(cwd), "gitBranch": branch})
        for cwd, branch in moments
    ]
    (transcripts / "session-1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


@run
async def test_a_reaped_tab_shows_the_pr_its_session_landed_from_elsewhere(rig):
    # The harness reaped the tab's worktree; its session landed from a subagent's.
    worktrees = rig.repository / ".claude" / "worktrees"
    tab = rig.tab(worktrees / "gone")
    session_was_on(
        rig, tab, (worktrees / "gone", "worktree-gone"), (worktrees / "agent-1", "land/x")
    )
    rig.gh.branches["land/x"] = make_pr(9, head="land/x", state="MERGED")
    await rig.board.scan()
    fields = rig.board.tab_fields(tab)
    assert fields["pr"]["number"] == 9
    assert fields["pr"]["repo"] == str(rig.key())
    # Landing new work builds on the tree's own branch, which heads nothing.
    assert rig.board.pr_of_tab(tab.id) is None


@run
async def test_the_trees_own_pr_wins_over_the_sessions(rig):
    tree = rig.worktree("a")
    tab = rig.tab(tree)
    session_was_on(rig, tab, (rig.repository / ".claude" / "worktrees" / "agent-1", "land/x"))
    rig.gh.open[rig.key()] = [make_pr(7), make_pr(9, head="land/x")]
    await rig.board.refresh()
    await rig.board.scan()
    assert rig.board.tab_fields(tab)["pr"]["number"] == 7


@run
async def test_a_sessions_pr_is_held_by_its_tab_and_shows_its_gates(rig):
    tab = rig.tab(rig.worktree("a"))
    session_was_on(rig, tab, (rig.repository / ".claude" / "worktrees" / "agent-1", "land/x"))
    rig.gh.open[rig.key()] = [replace(make_pr(9, head="land/x"), ci="passing")]
    await rig.board.refresh()
    await rig.board.scan()
    await rig.board.check_delivery()
    fields = rig.board.tab_fields(tab)
    assert fields["pr"]["number"] == 9
    assert ("ci", "passing") in [(g["name"], g["state"]) for g in fields["gates"]]
    assert rig.board.plain_prs() == []


@run
async def test_a_session_only_on_the_trunk_shows_nothing(rig):
    tab = rig.tab(rig.worktree("a"))
    session_was_on(rig, tab, (rig.repository, "main"))
    await rig.board.scan()
    assert rig.board.tab_fields(tab)["pr"] is None
    assert rig.gh.branch_calls == ["worktree-a"]


def test_pull_request_stays_comparable():
    assert replace(make_pr(), state="MERGED") != make_pr()

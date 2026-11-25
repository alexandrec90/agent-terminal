import asyncio
import json
import os
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_pr_board import FakeGh, git, make_pr

from agent_terminal import history
from agent_terminal.gitinfo import main_checkout, run_git
from agent_terminal.history import (
    HistoryLinks,
    PastSession,
    SessionHistory,
    TreeFacts,
    claude_sessions,
    claude_title,
    codex_sessions,
    deployed_commits,
    describe,
    first_prompt,
    list_sessions,
    read_tree_facts,
    scope_trees,
    session_title,
    tab_for,
    title_from_tail,
)
from agent_terminal.pr_board import BoardLinks, PrBoard
from agent_terminal.prompts import claude_transcript_dir
from agent_terminal.prs import PullRequest
from agent_terminal.settings import Settings
from agent_terminal.store import Tab

# Records as Claude Code really writes them (2.x), trimmed to the keys read here.
CARET = {
    "type": "user",
    "isMeta": True,
    "message": {"role": "user", "content": "<local-command-caveat>Caveat</local-command-caveat>"},
}
SLASH = {
    "type": "user",
    "message": {"role": "user", "content": "<command-name>/model</command-name>"},
}
PROMPT = {"type": "user", "message": {"role": "user", "content": "install this app please"}}
TOOL_RESULT = {
    "type": "user",
    "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
}
AI_TITLE = {"type": "ai-title", "aiTitle": "Installing app on this PC", "sessionId": "s"}
CUSTOM_TITLE = {"type": "custom-title", "customTitle": "my name for it", "sessionId": "s"}
LAST_PROMPT = {"type": "last-prompt", "lastPrompt": "the icon disappeared", "sessionId": "s"}


def lines(*records: dict[str, Any]) -> list[str]:
    return [json.dumps(record) for record in records]


def write_transcript(home: Path, tree: Path, session: str, *records, age: float = 0.0) -> Path:
    directory = claude_transcript_dir(home, tree)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session}.jsonl"
    path.write_text("\n".join(lines(*records)) + "\n", encoding="utf-8")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def write_rollout(home: Path, tree: Path, session: str) -> None:
    directory = home / "sessions" / "2026" / "11" / "23"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta = {
        "type": "session_meta",
        "payload": {"id": session, "cwd": str(tree), "timestamp": stamp},
    }
    (directory / f"rollout-{session}.jsonl").write_text(json.dumps(meta) + "\n", encoding="utf-8")


class TestTitles:
    def test_a_name_the_user_gave_beats_claudes(self):
        assert title_from_tail(lines(CUSTOM_TITLE, AI_TITLE, LAST_PROMPT)) == "my name for it"

    def test_claudes_title_beats_the_last_prompt(self):
        assert title_from_tail(lines(LAST_PROMPT, AI_TITLE)) == "Installing app on this PC"

    def test_the_last_prompt_when_nothing_is_titled(self):
        assert title_from_tail(lines(LAST_PROMPT)) == "the icon disappeared"

    def test_a_cut_off_line_is_skipped(self):
        assert title_from_tail(['tle":"half', *lines(AI_TITLE)]) == "Installing app on this PC"

    def test_nothing_titled_is_empty(self):
        assert title_from_tail(lines(PROMPT)) == ""

    def test_the_first_prompt_skips_what_claude_code_added(self):
        assert first_prompt(lines(CARET, SLASH, TOOL_RESULT, PROMPT)) == "install this app please"

    def test_a_prompt_in_parts_is_joined(self):
        parts = {"type": "user", "message": {"content": [{"type": "text", "text": "fix it"}]}}
        assert first_prompt(lines(parts)) == "fix it"

    def test_no_prompt_is_empty(self):
        assert first_prompt(lines(CARET, TOOL_RESULT)) == ""


class TestClaudeTitle:
    def test_reads_the_title_from_the_end(self, tmp_path):
        path = write_transcript(tmp_path, tmp_path / "t", "s", PROMPT, AI_TITLE)
        assert claude_title(path) == "Installing app on this PC"

    def test_a_headless_session_is_called_by_its_first_prompt(self, tmp_path):
        path = write_transcript(tmp_path, tmp_path / "t", "s", CARET, PROMPT, TOOL_RESULT)
        assert claude_title(path) == "install this app please"

    def test_a_long_title_is_cut_and_flattened(self, tmp_path):
        long = {"type": "user", "message": {"content": "word\n  " * 100}}
        path = write_transcript(tmp_path, tmp_path / "t", "s", long)
        title = claude_title(path)
        assert len(title) == history.TITLE_LENGTH
        assert "\n" not in title and "  " not in title

    def test_the_title_is_found_past_a_long_head(self, tmp_path, monkeypatch):
        monkeypatch.setattr(history, "TITLE_TAIL_BYTES", 200)
        filler = [TOOL_RESULT] * 50
        path = write_transcript(tmp_path, tmp_path / "t", "s", PROMPT, *filler, AI_TITLE)
        assert claude_title(path) == "Installing app on this PC"

    def test_a_missing_transcript_has_no_title(self, tmp_path):
        assert claude_title(tmp_path / "gone.jsonl") == ""


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        projects_root=tmp_path / "projects",
        claude_home=tmp_path / "claude",
        codex_home=tmp_path / "codex",
        gh_command=str(tmp_path / "no-gh"),
    )


@pytest.fixture
def repository(settings) -> Path:
    repo = settings.projects_root / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "--initial-branch", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    git(repo, "commit", "--allow-empty", "--message", "first")
    found = main_checkout(repo)
    assert found is not None
    return found


def add_worktree(repository: Path, name: str) -> Path:
    tree = repository / ".claude" / "worktrees" / name
    git(repository, "worktree", "add", str(tree), "-b", f"worktree-{name}")
    # As git names it, which is how `git worktree list` gives it back.
    return Path(run_git(tree, "rev-parse", "--show-toplevel").strip())


class TestScope:
    def test_a_plain_folder_is_only_itself(self, tmp_path):
        assert scope_trees(tmp_path) == [tmp_path]

    def test_a_repository_brings_every_worktree(self, repository):
        tree = add_worktree(repository, "a")
        assert scope_trees(repository) == [repository, tree]

    def test_a_subfolder_brings_its_repository_too(self, repository):
        below = repository / "sub"
        below.mkdir()
        tree = add_worktree(repository, "a")
        assert scope_trees(below) == [below, repository, tree]

    def test_a_removed_worktree_is_not_offered(self, repository):
        tree = add_worktree(repository, "a")
        shutil.rmtree(tree)
        assert scope_trees(repository) == [repository]


class TestOneTree:
    def test_claude_sessions_are_every_transcript_in_the_tree(self, tmp_path):
        tree = tmp_path / "tree"
        write_transcript(tmp_path, tree, "a", age=30)
        write_transcript(tmp_path, tree / "below", "not-this-tree")
        [found] = claude_sessions(tmp_path, tree)
        assert (found.agent, found.session_id, found.tree, found.title) == ("claude", "a", tree, "")
        assert found.last_active == pytest.approx(time.time() - 30, abs=5)

    def test_claude_sessions_of_a_tree_never_used_are_none(self, tmp_path):
        assert claude_sessions(tmp_path, tmp_path / "tree") == []

    def test_codex_sessions_older_than_the_lookback_are_left_out(self, tmp_path):
        write_rollout(tmp_path, tmp_path / "tree", "s")
        later = time.time() + history.CODEX_LOOKBACK_SECONDS + 3600
        assert codex_sessions(tmp_path, [tmp_path / "tree"], time.time())[0].session_id == "s"
        assert codex_sessions(tmp_path, [tmp_path / "tree"], later) == []

    def test_session_title_reads_claudes_transcript(self, settings, tmp_path):
        write_transcript(settings.claude_home, tmp_path, "s", AI_TITLE)
        session = PastSession("claude", "s", tmp_path, 0.0)
        assert session_title(settings, session) == "Installing app on this PC"

    def test_session_title_of_codex_is_none(self, settings, tmp_path):
        assert session_title(settings, PastSession("codex", "s", tmp_path, 0.0)) == ""


class TestListSessions:
    def test_finds_sessions_across_the_projects_trees_newest_first(self, settings, repository):
        tree = add_worktree(repository, "a")
        write_transcript(settings.claude_home, repository, "on-main", PROMPT, age=60)
        write_transcript(settings.claude_home, tree, "in-tree", AI_TITLE)
        found = list_sessions(settings, repository, set())
        assert [(s.session_id, s.tree) for s in found] == [
            ("in-tree", tree),
            ("on-main", repository),
        ]
        assert [s.title for s in found] == ["Installing app on this PC", "install this app please"]

    def test_leaves_out_sessions_a_tab_has_open(self, settings, repository):
        write_transcript(settings.claude_home, repository, "open")
        write_transcript(settings.claude_home, repository, "closed")
        assert [s.session_id for s in list_sessions(settings, repository, {"open"})] == ["closed"]

    def test_leaves_out_another_projects_sessions(self, settings, repository, tmp_path):
        write_transcript(settings.claude_home, tmp_path / "elsewhere", "other")
        assert list_sessions(settings, repository, set()) == []

    def test_keeps_only_the_newest(self, settings, repository, monkeypatch):
        monkeypatch.setattr(history, "MAX_SESSIONS", 1)
        write_transcript(settings.claude_home, repository, "old", age=60)
        write_transcript(settings.claude_home, repository, "new")
        assert [s.session_id for s in list_sessions(settings, repository, set())] == ["new"]

    def test_finds_codex_sessions_by_their_folder(self, settings, repository, tmp_path):
        tree = add_worktree(repository, "a")
        write_rollout(settings.codex_home, tree, "codex-here")
        write_rollout(settings.codex_home, tmp_path / "elsewhere", "codex-not")
        found = list_sessions(settings, repository, set())
        assert [(s.agent, s.session_id, s.title) for s in found] == [("codex", "codex-here", "")]


class TestTabFor:
    def test_a_worktree_session_resumes_in_its_worktree(self, repository):
        tree = add_worktree(repository, "a")
        tab = tab_for(PastSession("claude", "s", tree, 0.0, "Do it"), repository)
        assert (tab.project_path, tab.project, tab.cwd) == (str(repository), "repo", str(tree))
        assert tab.worktree is True
        assert (tab.agent, tab.session_id, tab.title) == ("claude", "s", "Do it")

    def test_a_main_checkout_session_is_no_worktree(self, repository):
        tab = tab_for(PastSession("claude", "s", repository, 0.0), repository)
        assert (tab.worktree, tab.cwd) == (False, str(repository))

    def test_a_worktree_outside_claudes_folder_resumes_there(self, repository, tmp_path):
        tree = tmp_path / "elsewhere"
        git(repository, "worktree", "add", str(tree), "-b", "other")
        tab = tab_for(PastSession("codex", "s", tree, 0.0), repository)
        assert (tab.worktree, tab.cwd) == (True, str(tree))

    def test_a_plain_folder(self, tmp_path):
        tab = tab_for(PastSession("claude", "s", tmp_path, 0.0), tmp_path)
        assert tab.worktree is False


class Rig:
    def __init__(self, settings: Settings, repository: Path, loop) -> None:
        self.settings = settings
        self.repository = repository
        self.gh = FakeGh()
        self.board = PrBoard(settings, loop, BoardLinks(lambda: [], lambda: None), self.gh)
        self.messages: list[dict[str, Any]] = []
        self.tabs: list[Tab] = []
        self.resumed: list[Tab] = []
        self.history = SessionHistory(
            settings,
            self.board,
            loop,
            self.messages.append,
            HistoryLinks(lambda: self.tabs, self.resume),
        )
        self.handlers = self.history.handlers()

    async def resume(self, tab: Tab) -> None:
        self.resumed.append(tab)

    async def ask(self) -> list[dict[str, Any]]:
        await self.handlers["past_sessions"]({"path": str(self.repository)})
        return [m for m in self.messages if m["type"] == "past_sessions"]


def run(body):
    """An async test method, against a `Rig` on the test's own loop."""

    def wrapper(_self, settings, repository):
        async def go():
            await body(Rig(settings, repository, asyncio.get_running_loop()))

        asyncio.run(go())

    wrapper.__name__ = body.__name__
    return wrapper


def head_of(folder: Path) -> str:
    return run_git(folder, "rev-parse", "HEAD").strip()


class TestShippingState:
    @run
    async def test_a_session_shows_its_trees_ship_intent(rig):
        tree = add_worktree(rig.repository, "a")
        (tree / "logs").mkdir()
        (tree / "logs" / "ship-intent.md").write_text("Fix it\n", encoding="utf-8")
        write_transcript(rig.settings.claude_home, tree, "s")
        [sent] = await rig.ask()
        [row] = sent["sessions"]
        assert (row["ship"], row["branch"], row["pr"]) == ("intent", "worktree-a", None)
        assert (row["tree"], row["tree_name"]) == (str(tree), "a")
        assert sent["requested"] == str(rig.repository)

    @run
    async def test_an_open_pr_already_known_is_shown_at_once(rig):
        tree = add_worktree(rig.repository, "a")
        write_transcript(rig.settings.claude_home, tree, "s")
        rig.gh.open[rig.repository] = [make_pr(7)]
        await rig.board.refresh()
        sent = await rig.ask()
        assert len(sent) == 1
        assert sent[0]["sessions"][0]["pr"]["number"] == 7
        assert sent[0]["sessions"][0]["pr"]["deployed"] is False

    @run
    async def test_a_pr_github_is_asked_about_comes_in_a_second_answer(rig):
        tree = add_worktree(rig.repository, "a")
        write_transcript(rig.settings.claude_home, tree, "s")
        rig.gh.branches["worktree-a"] = make_pr(8, state="CLOSED")
        first, second = await rig.ask()
        assert first["sessions"][0]["pr"] is None
        assert second["sessions"][0]["pr"]["state"] == "CLOSED"

    @run
    async def test_a_session_a_tab_has_open_is_not_offered(rig):
        write_transcript(rig.settings.claude_home, rig.repository, "open")
        write_transcript(rig.settings.claude_home, rig.repository, "closed")
        rig.tabs = [tab_for(PastSession("claude", "open", rig.repository, 0.0), rig.repository)]
        [sent] = await rig.ask()
        assert [row["session_id"] for row in sent["sessions"]] == ["closed"]

    @run
    async def test_the_trunk_is_never_looked_up(rig):
        write_transcript(rig.settings.claude_home, rig.repository, "s")
        await rig.ask()
        assert rig.gh.branch_calls == []

    @run
    async def test_a_merge_the_main_checkout_has_is_deployed(rig):
        tree = add_worktree(rig.repository, "a")
        write_transcript(rig.settings.claude_home, tree, "s")
        merged = make_pr(9, state="MERGED")
        rig.gh.branches["worktree-a"] = replace_merge(merged, head_of(rig.repository))
        *_, last = await rig.ask()
        pr = last["sessions"][0]["pr"]
        assert (pr["state"], pr["deployed"]) == ("MERGED", True)

    @run
    async def test_a_merge_not_pulled_is_not_deployed(rig):
        tree = add_worktree(rig.repository, "a")
        write_transcript(rig.settings.claude_home, tree, "s")
        rig.gh.branches["worktree-a"] = replace_merge(make_pr(9, state="MERGED"), "ab" * 20)
        *_, last = await rig.ask()
        assert last["sessions"][0]["pr"]["deployed"] is False


def replace_merge(pr: PullRequest, commit: str) -> PullRequest:
    return replace(pr, merge_commit=commit)


def test_deployed_is_asked_once_per_merge(settings, repository):
    async def go():
        board = PrBoard(settings, asyncio.get_running_loop(), BoardLinks(list, lambda: None))
        board._by_branch[(repository, "b")] = replace_merge(make_pr(1, "b", "MERGED"), "cd" * 20)
        facts = [TreeFacts(repository, "b", "")] * 3 + [TreeFacts(None, None, "")]
        assert deployed_commits(board, facts) == {"cd" * 20: False}

    asyncio.run(go())


def test_describe_without_a_repository(tmp_path, settings):
    async def go():
        board = PrBoard(settings, asyncio.get_running_loop(), BoardLinks(list, lambda: None))
        facts = read_tree_facts(tmp_path)
        row = describe(PastSession("codex", "s", tmp_path, 5.0), facts, board, {})
        assert row == {
            "agent": "codex",
            "session_id": "s",
            "tree": str(tmp_path),
            "last_active": 5.0,
            "title": "",
            "tree_name": tmp_path.name,
            "branch": None,
            "ship": "",
            "pr": None,
        }

    asyncio.run(go())


class TestResume:
    @run
    async def test_resumes_a_session_found_on_disk(rig):
        tree = add_worktree(rig.repository, "a")
        write_transcript(rig.settings.claude_home, tree, "s", AI_TITLE)
        await rig.handlers["resume_session"]({"path": str(rig.repository), "session_id": "s"})
        [tab] = rig.resumed
        assert (tab.session_id, tab.cwd, tab.title) == ("s", str(tree), "Installing app on this PC")

    @run
    async def test_an_open_session_is_still_found_so_its_tab_can_be_shown(rig):
        write_transcript(rig.settings.claude_home, rig.repository, "s")
        rig.tabs = [tab_for(PastSession("claude", "s", rig.repository, 0.0), rig.repository)]
        await rig.handlers["resume_session"]({"path": str(rig.repository), "session_id": "s"})
        assert [tab.session_id for tab in rig.resumed] == ["s"]

    @run
    async def test_a_session_not_on_disk_is_refused(rig):
        with pytest.raises(ValueError, match="no session"):
            await rig.handlers["resume_session"](
                {"path": str(rig.repository), "session_id": "made-up"}
            )
        assert rig.resumed == []

    @run
    async def test_a_relative_path_is_refused(rig):
        with pytest.raises(ValueError, match="absolute"):
            await rig.handlers["past_sessions"]({"path": "repo"})

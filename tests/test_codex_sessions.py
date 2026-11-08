import asyncio
import json
import os
from pathlib import Path

import pytest

from agent_terminal.agents import CODEX
from agent_terminal.codex_sessions import (
    CodexRollout,
    CodexSessionFinder,
    claim_codex_rollout,
    is_same_or_worktree,
    parse_rollout_head,
    recent_rollouts,
)
from agent_terminal.settings import Settings
from agent_terminal.store import Tab


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", codex_home=tmp_path / "codex")


def make_tab(tmp_path: Path, agent: str = CODEX, **fields) -> Tab:
    defaults = {"project": "demo", "project_path": str(tmp_path / "demo"), "created_at": 0.0}
    return Tab(id="t", agent=agent, **{**defaults, **fields})


def write_rollout(settings: Settings, name: str, head: dict, mtime: float | None = None) -> Path:
    directory = settings.codex_home / "sessions" / "2026" / "11" / "04"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-{name}.jsonl"
    path.write_text(json.dumps(head) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def rollout_head(session_id: str, cwd: str, timestamp: str = "2026-11-05T02:36:49.431Z") -> dict:
    return {
        "type": "session_meta",
        "payload": {"id": session_id, "cwd": cwd, "timestamp": timestamp},
    }


class TestRollouts:
    def test_parses_a_real_head(self):
        line = json.dumps(rollout_head("abc", "C:\\work"))
        rollout = parse_rollout_head(line)
        assert rollout is not None
        assert (rollout.session_id, rollout.cwd) == ("abc", "C:\\work")
        assert rollout.started_at > 0

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "not json",
            "[]",
            json.dumps({"type": "event_msg", "payload": {}}),
            json.dumps({"type": "session_meta", "payload": {"id": "abc"}}),
            json.dumps({"type": "session_meta", "payload": None}),
            json.dumps(rollout_head("abc", "C:\\work", "yesterday")),
        ],
    )
    def test_anything_else_is_none(self, line):
        assert parse_rollout_head(line) is None

    def test_recent_keeps_only_those_begun_since(self, settings):
        new = rollout_head("new", "C:\\w", "2026-11-05T02:36:49.000Z")
        old = rollout_head("old", "C:\\w", "2026-09-01T00:00:00.000Z")
        write_rollout(settings, "new", new)
        write_rollout(settings, "old", old)
        since = parse_rollout_head(json.dumps(new)).started_at
        found = recent_rollouts(settings.codex_home, since)
        assert [r.session_id for r in found] == ["new"]

    def test_a_long_idle_file_is_not_opened(self, settings):
        path = write_rollout(settings, "stale", rollout_head("stale", "C:\\w"), mtime=1000.0)
        assert path.exists()
        assert recent_rollouts(settings.codex_home, since=2000.0) == []

    def test_no_sessions_directory(self, settings):
        assert recent_rollouts(settings.codex_home, since=0.0) == []


class TestClaimRollout:
    def rollout(self, session_id: str, cwd: str, at: float = 1.0) -> CodexRollout:
        return CodexRollout(session_id, cwd, at)

    def test_is_same_or_worktree(self, tmp_path, settings):
        project = tmp_path / "demo"
        home = settings.codex_home
        check = is_same_or_worktree
        assert check(str(project), str(project), home)
        assert check(str(project / "sub"), str(project), home)
        assert check(str(home / "worktrees" / "8583" / "demo"), str(project), home)
        assert not check(str(home / "worktrees" / "8583" / "other"), str(project), home)
        assert not check(str(tmp_path / "other"), str(project), home)

    def test_a_plain_tab_takes_a_rollout_in_the_project(self, tmp_path, settings):
        tab = make_tab(tmp_path, CODEX)
        rollouts = [self.rollout("a", tab.project_path)]
        claimed = claim_codex_rollout(tab, rollouts, set(), settings.codex_home)
        assert claimed == rollouts[0]

    def test_a_worktree_tab_takes_a_managed_worktree_rollout(self, tmp_path, settings):
        tab = make_tab(tmp_path, CODEX, worktree=True)
        managed = str(settings.codex_home / "worktrees" / "1" / "demo")
        rollouts = [self.rollout("plain", tab.project_path), self.rollout("wt", managed)]
        claimed = claim_codex_rollout(tab, rollouts, set(), settings.codex_home)
        assert claimed is not None and claimed.session_id == "wt"

    def test_a_plain_tab_leaves_a_worktree_rollout(self, tmp_path, settings):
        tab = make_tab(tmp_path, CODEX)
        managed = str(settings.codex_home / "worktrees" / "1" / "demo")
        rollouts = [self.rollout("wt", managed)]
        assert claim_codex_rollout(tab, rollouts, set(), settings.codex_home) is None

    def test_two_tabs_cannot_take_the_same_session(self, tmp_path, settings):
        tab = make_tab(tmp_path, CODEX)
        rollouts = [self.rollout("a", tab.project_path), self.rollout("b", tab.project_path)]
        claimed = claim_codex_rollout(tab, rollouts, {"a"}, settings.codex_home)
        assert claimed is not None and claimed.session_id == "b"

    def test_another_projects_rollout_is_not_taken(self, tmp_path, settings):
        tab = make_tab(tmp_path, CODEX)
        rollouts = [self.rollout("a", str(tmp_path / "other"))]
        assert claim_codex_rollout(tab, rollouts, set(), settings.codex_home) is None


class TestCodexSessionFinder:
    def build(self, settings: Settings, loop, claimed=frozenset()) -> tuple:
        found: list[bool] = []
        finder = CodexSessionFinder(
            settings.codex_home, loop, lambda: set(claimed), lambda: found.append(True)
        )
        return finder, found

    def test_search_gives_the_tab_its_session(self, tmp_path, settings):
        tab = make_tab(tmp_path)
        write_rollout(settings, "a", rollout_head("a", tab.project_path, "2099-01-01T00:00:00Z"))

        async def body():
            finder, found = self.build(settings, asyncio.get_running_loop())
            await finder.search(tab, since=0.0)
            return found

        assert asyncio.run(body()) == [True]
        assert (tab.session_id, tab.cwd) == ("a", tab.project_path)

    def test_a_tab_with_no_rollout_yet_is_left_alone(self, tmp_path, settings):
        tab = make_tab(tmp_path)

        async def body():
            finder, found = self.build(settings, asyncio.get_running_loop())
            await finder.search(tab, since=0.0)
            return found

        assert asyncio.run(body()) == []
        assert tab.session_id is None

    def test_a_session_another_tab_holds_is_not_taken(self, tmp_path, settings):
        tab = make_tab(tmp_path)
        write_rollout(settings, "a", rollout_head("a", tab.project_path, "2099-01-01T00:00:00Z"))

        async def body():
            finder, _ = self.build(settings, asyncio.get_running_loop(), claimed={"a"})
            await finder.search(tab, since=0.0)

        asyncio.run(body())
        assert tab.session_id is None

    def test_look_for_runs_one_search_at_a_time(self, tmp_path, settings, monkeypatch):
        tab = make_tab(tmp_path)
        calls = []
        monkeypatch.setattr(
            "agent_terminal.codex_sessions.recent_rollouts",
            lambda home, since: calls.append(since) or [],
        )

        async def body():
            finder, _ = self.build(settings, asyncio.get_running_loop())
            finder.look_for(tab, 1.0)
            finder.look_for(tab, 2.0)
            await asyncio.sleep(0.05)
            finder.look_for(tab, 3.0)
            await asyncio.sleep(0.05)

        asyncio.run(body())
        assert calls == [1.0, 3.0]

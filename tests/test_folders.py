import json
import subprocess
from pathlib import Path

import pytest

from agent_terminal.folders import (
    can_cut_worktree,
    describe_folder,
    display_name,
    resolve_folder,
)
from agent_terminal.settings import Settings


def git(folder, *args):
    subprocess.run(["git", *args], cwd=folder, check=True, capture_output=True)  # noqa: S607 - fixed test commands


def commit(folder):
    git(folder, "init")
    git(folder, "config", "user.email", "t@example.com")
    git(folder, "config", "user.name", "T")
    git(folder, "commit", "--allow-empty", "--message", "first")


@pytest.fixture
def settings(tmp_path):
    return Settings(claude_config_file=tmp_path / "claude.json", codex_home=tmp_path / "codex")


class TestResolveFolder:
    def test_an_absolute_path(self, tmp_path):
        assert resolve_folder(str(tmp_path)) == tmp_path.resolve()

    def test_quotes_from_copy_as_path_are_dropped(self, tmp_path):
        assert resolve_folder(f'  "{tmp_path}"  ') == tmp_path.resolve()

    def test_home_expands(self):
        assert resolve_folder("~") == Path.home().resolve()

    @pytest.mark.parametrize("text", ["", "   ", None, 7, "relative/path"])
    def test_anything_else_is_refused(self, text):
        with pytest.raises(ValueError):
            resolve_folder(text)


def test_display_name_of_a_folder():
    assert display_name(Path("C:/work/demo")) == "demo"


def test_display_name_of_a_root_is_the_root():
    root = Path(Path.cwd().anchor)
    assert display_name(root) == str(root)


class TestCanCutWorktree:
    def test_a_repository_with_a_commit(self, tmp_path):
        commit(tmp_path)
        assert can_cut_worktree(tmp_path)

    def test_a_subfolder_of_one(self, tmp_path):
        commit(tmp_path)
        below = tmp_path / "sub"
        below.mkdir()
        assert can_cut_worktree(below)

    def test_an_empty_repository(self, tmp_path):
        git(tmp_path, "init")
        assert not can_cut_worktree(tmp_path)

    def test_a_plain_folder(self, tmp_path):
        assert not can_cut_worktree(tmp_path)


class TestDescribeFolder:
    def test_a_plain_folder(self, settings, tmp_path):
        facts = describe_folder(settings, str(tmp_path))
        assert facts["exists"] is True
        assert facts["path"] == str(tmp_path.resolve())
        assert facts["name"] == tmp_path.name
        assert facts["is_git"] is False
        assert facts["has_commits"] is False
        assert facts["trusted"] == {"claude": False, "codex": False}

    def test_a_repository(self, settings, tmp_path):
        commit(tmp_path)
        facts = describe_folder(settings, str(tmp_path))
        assert (facts["is_git"], facts["has_commits"]) == (True, True)

    def test_an_empty_repository(self, settings, tmp_path):
        git(tmp_path, "init")
        facts = describe_folder(settings, str(tmp_path))
        assert (facts["is_git"], facts["has_commits"]) == (True, False)

    def test_trust_is_read_per_agent(self, settings, tmp_path):
        entries = {tmp_path.as_posix(): {"hasTrustDialogAccepted": True}}
        settings.claude_config_file.write_text(json.dumps({"projects": entries}), encoding="utf-8")
        assert describe_folder(settings, str(tmp_path))["trusted"] == {
            "claude": True,
            "codex": False,
        }

    def test_a_folder_that_does_not_exist(self, settings, tmp_path):
        missing = str(tmp_path / "absent")
        assert describe_folder(settings, missing) == {"requested": missing, "exists": False}

    def test_a_file_is_not_a_folder(self, settings, tmp_path):
        path = tmp_path / "file.txt"
        path.write_text("x")
        assert describe_folder(settings, str(path))["exists"] is False

    def test_a_meaningless_path_is_an_answer_too(self, settings):
        assert describe_folder(settings, "relative") == {"requested": "relative", "exists": False}

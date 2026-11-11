import subprocess

import pytest

from agent_terminal import gitinfo
from agent_terminal.gitinfo import (
    GitError,
    commit_everything,
    current_branch,
    git_dir,
    has_commits,
    main_checkout,
    parse_worktrees,
    run_git,
    worktrees,
)


def git(folder, *args):
    subprocess.run(["git", *args], cwd=folder, check=True, capture_output=True)  # noqa: S607 - fixed test commands


def identify(folder):
    git(folder, "config", "user.email", "test@example.com")
    git(folder, "config", "user.name", "Test")


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch", "main")
    identify(repo)
    (repo / "file.txt").write_text("x")
    git(repo, "add", "--all")
    git(repo, "commit", "--message", "first")
    return repo


@pytest.fixture
def linked(repository, tmp_path):
    tree = tmp_path / "tree"
    git(repository, "worktree", "add", str(tree), "-b", "task")
    return tree


class TestGitDir:
    def test_finds_a_repository_from_its_root(self, repository):
        assert git_dir(repository) == repository / ".git"

    def test_finds_it_from_below(self, repository):
        below = repository / "a" / "b"
        below.mkdir(parents=True)
        assert git_dir(below) == repository / ".git"

    def test_outside_a_repository_is_none(self, tmp_path):
        assert git_dir(tmp_path) is None

    def test_follows_a_linked_worktrees_file(self, repository, linked):
        assert git_dir(linked).parent == (repository / ".git" / "worktrees").resolve()

    def test_a_malformed_git_file_is_no_repository(self, tmp_path):
        (tmp_path / ".git").write_text("not a pointer")
        assert git_dir(tmp_path) is None


class TestMainCheckout:
    def test_of_the_checkout_itself(self, repository):
        assert main_checkout(repository) == repository

    def test_of_a_subfolder(self, repository):
        below = repository / "sub"
        below.mkdir()
        assert main_checkout(below) == repository

    def test_of_a_linked_worktree(self, repository, linked):
        assert main_checkout(linked) == repository.resolve()

    def test_outside_a_repository(self, tmp_path):
        assert main_checkout(tmp_path) is None


class TestCurrentBranch:
    def test_reads_the_branch(self, repository):
        assert current_branch(repository) == "main"

    def test_reads_a_linked_worktrees_own_branch(self, linked):
        assert current_branch(linked) == "task"

    def test_detached_has_none(self, repository):
        git(repository, "checkout", "--detach")
        assert current_branch(repository) is None

    def test_outside_a_repository(self, tmp_path):
        assert current_branch(tmp_path) is None


class TestRunGit:
    def test_returns_output(self, repository):
        assert run_git(repository, "branch", "--show-current").strip() == "main"

    def test_a_failure_carries_gits_words(self, repository):
        with pytest.raises(GitError, match="ambiguous argument"):
            run_git(repository, "rev-parse", "no-such-ref")

    def test_git_missing_is_a_git_error(self, repository, monkeypatch):
        def absent(*_args, **_kwargs):
            raise FileNotFoundError("git")

        monkeypatch.setattr(gitinfo.subprocess, "run", absent)
        with pytest.raises(GitError, match="could not run"):
            run_git(repository, "status")


class TestHasCommits:
    def test_a_repository_with_one(self, repository):
        assert has_commits(repository)

    def test_a_new_repository(self, tmp_path):
        git(tmp_path, "init")
        assert not has_commits(tmp_path)


def test_parse_worktrees_maps_branches_and_skips_detached():
    porcelain = (
        "worktree C:/repo\nHEAD 1\nbranch refs/heads/main\n\n"
        "worktree C:/repo/.claude/worktrees/x\nHEAD 2\nbranch refs/heads/worktree-x\nlocked\n\n"
        "worktree C:/previews/y\nHEAD 3\ndetached\n\n"
    )
    trees = parse_worktrees(porcelain)
    assert set(trees) == {"main", "worktree-x"}
    assert trees["worktree-x"].name == "x"


def test_worktrees_lists_a_real_repository(repository, linked):
    assert set(worktrees(repository)) == {"main", "task"}


def test_worktrees_outside_a_repository_is_empty(tmp_path):
    assert worktrees(tmp_path) == {}


class TestCommitEverything:
    def test_makes_a_repository_of_a_plain_folder(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GIT_AUTHOR_NAME", "T")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "T")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
        (tmp_path / "kept.txt").write_text("x")
        commit_everything(tmp_path)
        assert has_commits(tmp_path)
        assert "kept.txt" in run_git(tmp_path, "ls-files")

    def test_gives_an_empty_repository_its_first_commit(self, tmp_path):
        git(tmp_path, "init")
        identify(tmp_path)
        commit_everything(tmp_path)
        assert has_commits(tmp_path)

    def test_an_empty_folder_still_gets_a_commit_to_cut_from(self, tmp_path):
        git(tmp_path, "init")
        identify(tmp_path)
        commit_everything(tmp_path)
        assert run_git(tmp_path, "ls-files") == ""

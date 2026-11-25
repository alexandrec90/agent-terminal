import json
import subprocess
from pathlib import Path

import pytest

from agent_terminal import repos
from agent_terminal.prs import GhError
from agent_terminal.repos import RemoteRepo

# As `gh repo list --json name,nameWithOwner,description,isArchived` gives it (gh 2.98).
RECORD = {
    "name": "agent-terminal",
    "nameWithOwner": "someone/agent-terminal",
    "description": "Windows terminal",
    "isArchived": False,
}


def git(folder: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args],  # noqa: S607 - fixed test commands
        cwd=folder,
        check=False,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, f"git {args}: {done.stderr}"
    return done.stdout


def make_repository(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    # Not `main`: a machine-wide pre-push hook may refuse a push to it, test remote or not.
    git(folder, "init", "--initial-branch", "trunk")
    git(folder, "config", "user.email", "t@example.com")
    git(folder, "config", "user.name", "T")
    git(folder, "commit", "--allow-empty", "--message", "first")
    return folder


def make_pushed(tmp_path: Path) -> Path:
    """A repository whose one branch is all on a remote."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    folder = make_repository(tmp_path / "projects" / "pushed")
    git(folder, "remote", "add", "origin", str(remote))
    git(folder, "push", "--quiet", "origin", "trunk")
    return folder


def repo(name: str, archived: bool = False) -> RemoteRepo:
    return RemoteRepo(name=name, full_name=f"someone/{name}", description="", archived=archived)


class TestParse:
    def test_reads_a_real_shaped_reply(self):
        assert repos.parse_repos(json.dumps([RECORD])) == [
            RemoteRepo("agent-terminal", "someone/agent-terminal", "Windows terminal", False)
        ]

    def test_a_missing_description_is_empty(self):
        record = {**RECORD, "description": None}
        assert repos.parse_repos(json.dumps([record]))[0].description == ""

    @pytest.mark.parametrize("reply", ["not json", "{}", "[1]", json.dumps([{"name": "x"}])])
    def test_anything_else_is_an_error(self, reply):
        with pytest.raises(GhError):
            repos.parse_repos(reply)


class TestMissingLocally:
    def test_leaves_out_what_is_already_here(self, tmp_path):
        (tmp_path / "here").mkdir()
        assert repos.missing_locally([repo("here"), repo("away")], tmp_path) == [repo("away")]

    def test_a_name_differing_only_in_case_is_here(self, tmp_path):
        (tmp_path / "Here").mkdir()
        assert repos.missing_locally([repo("here")], tmp_path) == []

    def test_a_file_of_that_name_takes_it(self, tmp_path):
        (tmp_path / "taken").write_text("x")
        assert repos.missing_locally([repo("taken")], tmp_path) == []

    def test_live_ones_come_first_then_by_name(self, tmp_path):
        found = repos.missing_locally(
            [repo("zeta"), repo("old", archived=True), repo("Alpha")], tmp_path
        )
        assert [r.name for r in found] == ["Alpha", "zeta", "old"]

    def test_a_missing_root_has_everything_to_clone(self, tmp_path):
        assert repos.missing_locally([repo("a")], tmp_path / "absent") == [repo("a")]


class TestCloneTarget:
    def test_is_the_repositorys_name_under_the_root(self, tmp_path):
        assert repos.clone_target(tmp_path, "someone/thing") == tmp_path / "thing"

    @pytest.mark.parametrize(
        "name",
        ["thing", "-o/x", "someone/--upload-pack=evil", "a/b/c", "a/..", "a/.hidden", "", 7, None],
    )
    def test_refuses_what_is_not_a_repository_name(self, tmp_path, name):
        with pytest.raises(ValueError):
            repos.clone_target(tmp_path, name)

    def test_refuses_a_folder_already_taken(self, tmp_path):
        (tmp_path / "Thing").mkdir()
        with pytest.raises(ValueError, match="already"):
            repos.clone_target(tmp_path, "someone/thing")


class TestDeletableProject:
    def test_a_project_under_the_root(self, tmp_path):
        (tmp_path / "old").mkdir()
        assert repos.deletable_project(tmp_path, str(tmp_path / "old"), []) == tmp_path / "old"

    @pytest.mark.parametrize(
        "where",
        [
            lambda root: root,
            lambda root: root.parent,
            lambda root: root / "old" / "inner",
            lambda root: root / ".hidden",
            lambda root: root / "absent",
            lambda root: root / "old" / "..",
        ],
    )
    def test_nothing_else(self, tmp_path, where):
        root = tmp_path / "projects"
        (root / "old" / "inner").mkdir(parents=True)
        (root / ".hidden").mkdir()
        with pytest.raises(ValueError):
            repos.deletable_project(root, str(where(root)), [])

    @pytest.mark.parametrize("text", ["", "relative", None, 3])
    def test_not_a_path(self, tmp_path, text):
        with pytest.raises(ValueError):
            repos.deletable_project(tmp_path, text, [])

    def test_not_with_a_tab_in_it(self, tmp_path):
        (tmp_path / "old").mkdir()
        with pytest.raises(ValueError, match="tab open"):
            repos.deletable_project(tmp_path, str(tmp_path / "old"), [tmp_path / "old"])

    def test_not_with_a_tab_deeper_in_it(self, tmp_path):
        (tmp_path / "old").mkdir()
        tree = tmp_path / "old" / ".claude" / "worktrees" / "x"
        with pytest.raises(ValueError, match="tab open"):
            repos.deletable_project(tmp_path, str(tmp_path / "old"), [tree])

    def test_a_tab_in_a_neighbour_of_a_like_name_is_no_matter(self, tmp_path):
        (tmp_path / "old").mkdir()
        assert repos.deletable_project(tmp_path, str(tmp_path / "old"), [tmp_path / "older"])


def test_local_names_are_every_entry_in_lower_case(tmp_path):
    (tmp_path / "Folder").mkdir()
    (tmp_path / "File.txt").write_text("x")
    assert repos.local_names(tmp_path) == {"folder", "file.txt"}
    assert repos.local_names(tmp_path / "absent") == set()


@pytest.mark.parametrize(
    ("inner", "inside"),
    [("a", True), ("a/b/c", True), ("ab", False), ("", False)],
)
def test_contains(tmp_path, inner, inside):
    assert repos.contains(tmp_path / "a", tmp_path / inner) is inside


@pytest.mark.parametrize(("count", "said"), [(1, "1 commit"), (2, "2 commits"), (0, "0 commits")])
def test_plural(count, said):
    assert repos.plural(count, "commit") == said


def test_has_changes_counts_untracked_files(tmp_path):
    folder = make_repository(tmp_path / "r")
    assert repos.has_changes(folder) is False
    (folder / "new.txt").write_text("x")
    assert repos.has_changes(folder) is True


def test_worktree_paths_keeps_detached_trees():
    porcelain = "worktree C:/a\nHEAD 1\nbranch refs/heads/main\n\nworktree C:/b\nHEAD 2\ndetached\n"
    assert repos.worktree_paths(porcelain) == [Path("C:/a"), Path("C:/b")]


class TestDeletionRisks:
    def test_a_plain_folder_is_all_at_risk(self, tmp_path):
        (tmp_path / "plain").mkdir()
        assert "isn't a git repository" in repos.deletion_risks(tmp_path / "plain")[0]

    def test_a_folder_inside_someone_elses_repository_is_not_one(self, tmp_path):
        outer = make_repository(tmp_path / "outer")
        (outer / "inner").mkdir()
        assert "isn't a git repository" in repos.deletion_risks(outer / "inner")[0]

    def test_everything_pushed_is_safe(self, tmp_path):
        assert repos.deletion_risks(make_pushed(tmp_path)) == []

    def test_no_remote_is_said(self, tmp_path):
        risks = repos.deletion_risks(make_repository(tmp_path / "solo"))
        assert risks == [
            "It has no remote, so its history exists only here.",
            "1 commit not pushed anywhere.",
        ]

    def test_unpushed_commits_are_counted(self, tmp_path):
        folder = make_pushed(tmp_path)
        git(folder, "commit", "--allow-empty", "--message", "second")
        git(folder, "commit", "--allow-empty", "--message", "third")
        assert repos.deletion_risks(folder) == ["2 commits not pushed anywhere."]

    def test_an_unpushed_branch_counts(self, tmp_path):
        folder = make_pushed(tmp_path)
        git(folder, "switch", "--quiet", "--create", "side")
        git(folder, "commit", "--allow-empty", "--message", "on the side")
        assert repos.deletion_risks(folder) == ["1 commit not pushed anywhere."]

    def test_uncommitted_changes_are_said(self, tmp_path):
        folder = make_pushed(tmp_path)
        (folder / "new.txt").write_text("x")
        assert repos.deletion_risks(folder) == ["1 worktree with uncommitted changes."]

    def test_a_worktrees_changes_count_too(self, tmp_path):
        folder = make_pushed(tmp_path)
        tree = tmp_path / "tree"
        git(folder, "worktree", "add", "--quiet", "--detach", str(tree))
        (tree / "new.txt").write_text("x")
        assert repos.deletion_risks(folder) == ["1 worktree with uncommitted changes."]

    def test_git_failing_is_said_rather_than_read_as_safe(self, tmp_path, monkeypatch):
        folder = make_pushed(tmp_path)

        def broken(*_args):
            raise repos.GitError("fatal: bad")

        monkeypatch.setattr(repos.gitinfo, "run_git", broken)
        assert repos.deletion_risks(folder) == ["Git could not say what is unsaved: fatal: bad"]

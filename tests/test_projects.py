from agent_terminal.projects import Project, is_git_checkout, list_projects


def test_lists_directories_sorted_without_case(tmp_path):
    for name in ("zeta", "Alpha", "beta"):
        (tmp_path / name).mkdir()
    assert [p.name for p in list_projects(tmp_path)] == ["Alpha", "beta", "zeta"]


def test_hidden_directories_and_files_are_not_projects(tmp_path):
    (tmp_path / ".worktrees").mkdir()
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "real").mkdir()
    assert [p.name for p in list_projects(tmp_path)] == ["real"]


def test_a_missing_root_lists_nothing(tmp_path):
    assert list_projects(tmp_path / "absent") == []


def test_reports_which_are_git_checkouts(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "plain").mkdir()
    flags = {p.name: p.is_git for p in list_projects(tmp_path)}
    assert flags == {"plain": False, "repo": True}


def test_a_linked_worktrees_git_file_counts(tmp_path):
    (tmp_path / ".git").write_text("gitdir: elsewhere")
    assert is_git_checkout(tmp_path)


def test_to_dict_is_what_the_page_reads():
    project = Project(name="a", path="C:\\a", is_git=True)
    assert project.to_dict() == {"name": "a", "path": "C:\\a", "is_git": True}

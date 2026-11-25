import asyncio
from pathlib import Path

import pytest

from agent_terminal.groups import Groups, group_path, open_folders, successor
from agent_terminal.store import Tab


def tab(tab_id: str, path: str = "C:\\a", cwd: str | None = None) -> Tab:
    return Tab(id=tab_id, project="a", project_path=path, agent="claude", created_at=1.0, cwd=cwd)


ROW = [tab("1"), tab("2"), tab("3"), tab("4")]


class TestSuccessor:
    def test_an_active_tab_that_stays_stays_active(self):
        assert successor(ROW, "1", [ROW[2]]) == "1"

    def test_the_next_tab_takes_over(self):
        assert successor(ROW, "2", [ROW[1]]) == "3"

    def test_the_next_tab_left_after_a_run_of_leaving_ones(self):
        assert successor(ROW, "2", [ROW[1], ROW[2]]) == "4"

    def test_the_one_before_when_none_is_left_after(self):
        assert successor(ROW, "3", [ROW[2], ROW[3]]) == "2"

    def test_none_when_every_tab_leaves(self):
        assert successor(ROW, "1", ROW) is None

    def test_nothing_active_stays_nothing(self):
        assert successor(ROW, None, [ROW[0]]) is None


def test_open_folders_has_each_tabs_group_and_worktree():
    tabs = [tab("1", "C:\\a"), tab("2", "C:\\b", cwd="C:\\b\\.claude\\worktrees\\x")]
    assert open_folders(tabs) == [
        Path("C:\\a"),
        Path("C:\\b"),
        Path("C:\\b\\.claude\\worktrees\\x"),
    ]


@pytest.mark.parametrize("path", [None, 3, ["C:\\a"]])
def test_group_path_refuses_what_is_not_a_path(path):
    with pytest.raises(TypeError):
        group_path({"path": path})


def test_group_path_reads_the_path():
    assert group_path({"path": "C:\\a"}) == "C:\\a"


class TestGroups:
    def setup_method(self):
        self.changes = 0
        self.groups = Groups(self.changed)

    def changed(self):
        self.changes += 1

    def test_folds_and_unfolds_telling_the_page_each_time(self):
        asyncio.run(self.groups.on_fold({"path": "C:\\a", "folded": True}))
        assert self.groups.fields() == {"folded": ["C:\\a"], "hidden": []}
        asyncio.run(self.groups.on_fold({"path": "C:\\a", "folded": False}))
        assert self.groups.fields() == {"folded": [], "hidden": []}
        assert self.changes == 2

    def test_close_hides_the_group_and_gives_its_tabs(self):
        tabs = [tab("1", "C:\\a"), tab("2", "C:\\b"), tab("3", "C:\\a")]
        closing = self.groups.close({"path": "C:\\a"}, tabs)
        assert [t.id for t in closing] == ["1", "3"]
        assert self.groups.fields()["hidden"] == ["C:\\a"]

    def test_a_session_opened_there_brings_it_back_unfolded(self):
        self.groups.restore(folded=["C:\\a", "C:\\b"], hidden=["C:\\a"])
        self.groups.opened("C:\\a")
        assert self.groups.fields() == {"folded": ["C:\\b"], "hidden": []}

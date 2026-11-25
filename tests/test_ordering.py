import asyncio

import pytest

from agent_terminal.ordering import Ordering, merge_group_order, move_tab
from agent_terminal.store import Tab


def make_tabs(*names: str) -> list[Tab]:
    """One tab per name; its first letter is its group."""
    return [
        Tab(id=name, project=name[0], project_path=f"C:\\{name[0]}", agent="claude", created_at=0)
        for name in names
    ]


def order(tabs: list[Tab]) -> list[str]:
    return [tab.id for tab in tabs]


@pytest.mark.parametrize(
    ("tab_id", "before", "expected", "moved"),
    [
        ("a3", "a1", ["a3", "a1", "b1", "a2"], True),
        ("a1", "a3", ["b1", "a2", "a1", "a3"], True),
        ("a1", None, ["b1", "a2", "a3", "a1"], True),
        ("a3", None, ["a1", "b1", "a2", "a3"], False),
        ("a2", "a3", ["a1", "b1", "a2", "a3"], False),
        ("a2", "a2", ["a1", "b1", "a2", "a3"], False),
        ("b1", None, ["a1", "b1", "a2", "a3"], False),
    ],
)
def test_move_tab_stays_within_the_group(tab_id, before, expected, moved):
    tabs = make_tabs("a1", "b1", "a2", "a3")
    assert move_tab(tabs, tab_id, before) is moved
    assert order(tabs) == expected


def test_move_tab_refuses_another_group():
    tabs = make_tabs("a1", "b1")
    with pytest.raises(ValueError):
        move_tab(tabs, "a1", "b1")
    assert order(tabs) == ["a1", "b1"]


@pytest.mark.parametrize(("tab_id", "before"), [("gone", None), ("a1", "gone")])
def test_move_tab_refuses_an_unknown_tab(tab_id, before):
    tabs = make_tabs("a1", "a2")
    with pytest.raises(KeyError):
        move_tab(tabs, tab_id, before)
    assert order(tabs) == ["a1", "a2"]


@pytest.mark.parametrize(
    ("current", "named", "expected"),
    [
        (["a", "b"], ["b", "a"], ["b", "a"]),
        (["a", "b", "c"], ["c", "a"], ["c", "a", "b"]),
        (["a"], ["x", "x", "a"], ["x", "a"]),
        ([], [], []),
    ],
)
def test_merge_group_order(current, named, expected):
    assert merge_group_order(current, named) == expected


def test_ordering_restores_and_places_groups_it_lacks():
    tabs = make_tabs("b1", "a1", "c1")
    ordering = Ordering(lambda: tabs, lambda: None)
    ordering.restore(["C:\\c"])
    assert ordering.group_order == ["C:\\c", "C:\\b", "C:\\a"]
    ordering.place("C:\\a")
    ordering.place("C:\\d")
    assert ordering.group_order == ["C:\\c", "C:\\b", "C:\\a", "C:\\d"]


def test_ordering_announces_a_move_only_when_something_moved():
    tabs = make_tabs("a1", "a2")
    changes: list[bool] = []
    handlers = Ordering(lambda: tabs, lambda: changes.append(True)).handlers()
    asyncio.run(handlers["move_tab"]({"id": "a2", "before": None}))
    assert changes == []
    asyncio.run(handlers["move_tab"]({"id": "a2", "before": "a1"}))
    assert order(tabs) == ["a2", "a1"]
    assert changes == [True]


@pytest.mark.parametrize("paths", ["C:\\a", ["C:\\a", 1], None])
def test_ordering_refuses_paths_that_are_not_a_list_of_strings(paths):
    ordering = Ordering(list, lambda: None)
    with pytest.raises(TypeError):
        asyncio.run(ordering.handlers()["order_groups"]({"paths": paths}))
    assert ordering.group_order == []

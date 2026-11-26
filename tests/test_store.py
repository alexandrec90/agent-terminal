import asyncio
import json
from pathlib import Path

import pytest

from agent_terminal.store import (
    MIN_SIDEBAR_WIDTH,
    SCHEMA_VERSION,
    DebouncedSaver,
    SidebarWidth,
    Snapshot,
    Tab,
    clean_group_order,
    clean_sidebar_width,
    load_snapshot,
    parse_snapshot,
    paths_of,
    save_snapshot,
    tab_from_dict,
)


def make_tab(**fields) -> Tab:
    defaults = {
        "id": "a",
        "project": "demo",
        "project_path": "C:\\demo",
        "agent": "claude",
        "created_at": 1.0,
    }
    return Tab(**{**defaults, **fields})


def test_a_snapshot_round_trips(tmp_path):
    path = tmp_path / "state.json"
    tab = make_tab(parked=True, attention="input", session_id="s", cwd="C:\\w", worktree=True)
    save_snapshot(path, Snapshot(tabs=[tab], active_id="a"))
    assert load_snapshot(path) == Snapshot(tabs=[tab], active_id="a")


def test_saving_creates_the_directory(tmp_path):
    path = tmp_path / "new" / "dir" / "state.json"
    save_snapshot(path, Snapshot())
    assert path.exists()


def test_saving_leaves_no_temporary(tmp_path):
    path = tmp_path / "state.json"
    save_snapshot(path, Snapshot())
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_no_file_is_an_empty_session(tmp_path):
    assert load_snapshot(tmp_path / "absent.json") == Snapshot()


def test_a_corrupt_file_is_kept_aside_not_lost(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ truncated", encoding="utf-8")
    assert load_snapshot(path) == Snapshot()
    assert not path.exists()
    assert (tmp_path / "state.corrupt.json").read_text(encoding="utf-8") == "{ truncated"


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        json.dumps({"version": 999, "tabs": []}),
        json.dumps({"version": SCHEMA_VERSION}),
        json.dumps({"version": SCHEMA_VERSION, "tabs": [{"id": "missing-fields"}]}),
        json.dumps({"version": SCHEMA_VERSION, "tabs": ["not a record"]}),
    ],
)
def test_parse_refuses_what_it_cannot_trust(text):
    with pytest.raises(ValueError):
        parse_snapshot(text)


def test_a_non_string_active_id_is_dropped():
    text = json.dumps({"version": SCHEMA_VERSION, "tabs": [], "active_id": 7})
    assert parse_snapshot(text).active_id is None


def test_kept_folded_and_hidden_groups_round_trip(tmp_path):
    path = tmp_path / "state.json"
    snapshot = Snapshot(folded=["C:\\a"], hidden=["C:\\b", "C:\\c"], kept=["C:\\a", "C:\\d"])
    save_snapshot(path, snapshot)
    assert load_snapshot(path) == snapshot


def test_a_file_from_before_groups_were_kept_has_none():
    text = json.dumps({"version": SCHEMA_VERSION, "tabs": [], "active_id": None})
    snapshot = parse_snapshot(text)
    assert (snapshot.folded, snapshot.hidden, snapshot.kept) == ([], [], None)


def test_a_saved_empty_kept_list_is_not_a_file_from_before():
    text = json.dumps({"version": SCHEMA_VERSION, "tabs": [], "kept": []})
    assert parse_snapshot(text).kept == []


def test_a_snapshot_with_no_kept_list_round_trips_as_none(tmp_path):
    path = tmp_path / "state.json"
    save_snapshot(path, Snapshot())
    assert load_snapshot(path).kept is None


def test_odd_kept_entries_are_dropped():
    text = json.dumps({"version": SCHEMA_VERSION, "tabs": [], "kept": [1, "C:\\a"]})
    assert parse_snapshot(text).kept == ["C:\\a"]


def test_odd_group_lists_cost_the_groups_not_the_tabs():
    text = json.dumps(
        {
            "version": SCHEMA_VERSION,
            "tabs": [make_tab().to_dict()],
            "folded": "C:\\a",
            "hidden": [1, "C:\\b"],
        }
    )
    snapshot = parse_snapshot(text)
    assert snapshot.tabs == [make_tab()]
    assert (snapshot.folded, snapshot.hidden) == ([], ["C:\\b"])


@pytest.mark.parametrize(
    ("saved", "read"),
    [(["C:\\a"], ["C:\\a"]), (None, []), ("C:\\a", []), ({"C:\\a": 1}, []), ([1, None], [])],
)
def test_paths_of(saved, read):
    assert paths_of(saved) == read


def test_the_sidebar_width_round_trips(tmp_path):
    path = tmp_path / "state.json"
    save_snapshot(path, Snapshot(sidebar_width=400))
    assert load_snapshot(path) == Snapshot(sidebar_width=400)


def test_a_file_from_before_the_sidebar_width_has_none():
    text = json.dumps({"version": SCHEMA_VERSION, "tabs": []})
    assert parse_snapshot(text).sidebar_width is None


@pytest.mark.parametrize(
    ("saved", "read"),
    [
        (400, 400),
        (MIN_SIDEBAR_WIDTH, MIN_SIDEBAR_WIDTH),
        (-5, MIN_SIDEBAR_WIDTH),
        (None, None),
        ("400", None),
        (400.5, None),
        (True, None),
    ],
)
def test_clean_sidebar_width(saved, read):
    assert clean_sidebar_width(saved) == read


def test_sidebar_width_keeps_a_width_and_refuses_a_bad_one():
    changes = []
    sidebar = SidebarWidth(lambda: changes.append(sidebar.value))

    async def body():
        await sidebar.on_width({"width": 400})
        await sidebar.on_width({"width": 10})
        for bad in ("400", 400.5, True):
            with pytest.raises(ValueError, match="not a sidebar width"):
                await sidebar.on_width({"width": bad})
        await sidebar.on_width({"width": None})

    asyncio.run(body())
    assert changes == [400, MIN_SIDEBAR_WIDTH, None]
    assert sidebar.value is None


def test_a_key_from_another_version_is_ignored():
    data = {**make_tab().to_dict(), "from_the_future": True}
    assert tab_from_dict(data) == make_tab()


def test_the_group_order_and_last_update_round_trip(tmp_path):
    path = tmp_path / "state.json"
    snapshot = Snapshot(tabs=[make_tab(updated_at=5.0)], group_order=["C:\\b", "C:\\demo"])
    save_snapshot(path, snapshot)
    assert load_snapshot(path) == snapshot


def test_a_file_from_before_group_order_reads_as_none():
    text = json.dumps({"version": SCHEMA_VERSION, "tabs": [make_tab().to_dict()]})
    snapshot = parse_snapshot(text)
    assert snapshot.group_order == []
    assert snapshot.tabs[0].updated_at is None


@pytest.mark.parametrize(
    ("saved", "read"),
    [
        ("C:\\a", []),
        (None, []),
        (["C:\\a", 3, "C:\\b", "C:\\a"], ["C:\\a", "C:\\b"]),
    ],
)
def test_a_malformed_group_order_keeps_only_its_paths(saved, read):
    assert clean_group_order(saved) == read
    text = json.dumps({"version": SCHEMA_VERSION, "tabs": [], "group_order": saved})
    assert parse_snapshot(text).group_order == read


def test_a_worktree_this_app_once_named_becomes_its_folder():
    record = {**make_tab(worktree=True).to_dict(), "worktree_name": "tab-1a2b3c4d"}
    expected = Path("C:\\demo") / ".claude" / "worktrees" / "tab-1a2b3c4d"
    assert tab_from_dict(record).cwd == str(expected)


def test_a_recorded_folder_outranks_an_old_name():
    record = {**make_tab(cwd="C:\\found").to_dict(), "worktree_name": "tab-1a2b3c4d"}
    assert tab_from_dict(record).cwd == "C:\\found"


@pytest.mark.parametrize("name", [None, "", 7])
def test_an_empty_or_odd_old_name_is_ignored(name):
    record = {**make_tab().to_dict(), "worktree_name": name}
    assert tab_from_dict(record).cwd is None


@pytest.mark.parametrize(("saved", "read"), [(True, "input"), (False, "")])
def test_attention_saved_as_a_flag_is_read_as_a_kind(saved, read):
    record = {**make_tab().to_dict(), "attention": saved}
    assert tab_from_dict(record).attention == read


def test_a_burst_of_changes_saves_once(tmp_path):
    path = tmp_path / "state.json"
    snapshots = []

    def snapshot() -> Snapshot:
        snapshots.append(True)
        return Snapshot()

    async def body():
        saver = DebouncedSaver(asyncio.get_running_loop(), path, snapshot, delay=0.01)
        saver.schedule()
        saver.schedule()
        saver.schedule()
        assert not path.exists()
        await asyncio.sleep(0.05)

    asyncio.run(body())
    assert path.exists()
    assert len(snapshots) == 1


def test_save_now_does_not_wait(tmp_path):
    path = tmp_path / "state.json"

    async def body():
        saver = DebouncedSaver(asyncio.get_running_loop(), path, Snapshot)
        saver.save_now()

    asyncio.run(body())
    assert path.exists()

import asyncio
import json

import pytest

from agent_terminal.store import (
    SCHEMA_VERSION,
    DebouncedSaver,
    Snapshot,
    Tab,
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
    tab = make_tab(parked=True, attention="input", session_id="s", worktree_name="w", worktree=True)
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


def test_folded_and_hidden_groups_round_trip(tmp_path):
    path = tmp_path / "state.json"
    snapshot = Snapshot(folded=["C:\\a"], hidden=["C:\\b", "C:\\c"])
    save_snapshot(path, snapshot)
    assert load_snapshot(path) == snapshot


def test_a_file_from_before_groups_were_kept_has_none():
    text = json.dumps({"version": SCHEMA_VERSION, "tabs": [], "active_id": None})
    snapshot = parse_snapshot(text)
    assert (snapshot.folded, snapshot.hidden) == ([], [])


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


def test_a_key_from_another_version_is_ignored():
    data = {**make_tab().to_dict(), "from_the_future": True}
    assert tab_from_dict(data) == make_tab()


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

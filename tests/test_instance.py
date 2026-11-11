import sys

from agent_terminal import instance
from agent_terminal.instance import acquire_instance_lock, focus_running_window, lock_file


def test_the_first_taker_holds_the_lock(tmp_path):
    held = acquire_instance_lock(tmp_path / "app.lock")
    assert held is not None
    held.close()


def test_a_second_taker_is_refused(tmp_path):
    path = tmp_path / "app.lock"
    held = acquire_instance_lock(path)
    assert held is not None
    assert acquire_instance_lock(path) is None
    held.close()


def test_the_lock_is_free_again_once_released(tmp_path):
    path = tmp_path / "app.lock"
    held = acquire_instance_lock(path)
    assert held is not None
    held.close()
    again = acquire_instance_lock(path)
    assert again is not None
    again.close()


def test_creates_the_directory(tmp_path):
    held = acquire_instance_lock(tmp_path / "new" / "app.lock")
    assert held is not None
    held.close()


def test_lock_file_takes_an_open_handle(tmp_path):
    with (tmp_path / "x.lock").open("a+", encoding="utf-8") as handle:
        lock_file(handle)


def test_focus_does_nothing_where_there_is_no_window_to_find(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert focus_running_window() is False
    assert instance.WINDOW_TITLE == "Agent Terminal"

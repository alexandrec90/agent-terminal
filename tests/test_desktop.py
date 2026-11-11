import asyncio
import threading
import time
from typing import Any

import pytest

from agent_terminal.__main__ import APP_ICON, BACKGROUND, FolderPicker, run_window
from agent_terminal.desktop import Desktop, DesktopRequests, no_picker
from agent_terminal.settings import Settings


def make_requests(tmp_path, loop, desktop: Desktop) -> tuple[DesktopRequests, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []
    settings = Settings(claude_config_file=tmp_path / "claude.json", codex_home=tmp_path)
    return DesktopRequests(settings, sent.append, loop, desktop), sent


def test_no_picker_chooses_nothing():
    assert no_picker() is None


def test_handlers_cover_what_the_page_sends(tmp_path):
    async def body():
        requests, _ = make_requests(tmp_path, asyncio.get_running_loop(), Desktop())
        assert set(requests.handlers()) == {
            "open_url",
            "copy",
            "inspect_folder",
            "pick_folder",
            "prepare_worktree",
        }

    asyncio.run(body())


def test_the_folder_dialog_is_on_a_thread_that_cannot_hold_up_exit(tmp_path):
    seen: list[bool] = []

    def pick() -> str:
        seen.append(threading.current_thread().daemon)
        return "C:\\chosen"

    async def body():
        requests, sent = make_requests(
            tmp_path, asyncio.get_running_loop(), Desktop(pick_folder=pick)
        )
        await requests.handlers()["pick_folder"]({})
        assert sent == [{"type": "picked", "path": "C:\\chosen"}]

    asyncio.run(body())
    assert seen == [True]


def test_the_page_is_not_kept_waiting_on_an_open_dialog(tmp_path):
    release = threading.Event()

    async def body():
        requests, sent = make_requests(
            tmp_path,
            asyncio.get_running_loop(),
            Desktop(pick_folder=lambda: release.wait(5) and "C:\\late"),
        )
        task = asyncio.create_task(requests.handlers()["pick_folder"]({}))
        started = time.monotonic()
        await asyncio.sleep(0.05)
        assert sent == []
        assert time.monotonic() - started < 1
        task.cancel()
        release.set()

    asyncio.run(body())


def test_a_dialog_answered_after_the_app_stopped_goes_nowhere(tmp_path, monkeypatch):
    release = threading.Event()
    raised: list[Any] = []
    monkeypatch.setattr(threading, "excepthook", raised.append)

    def pick() -> str:
        release.wait(5)
        return "C:\\late"

    async def body():
        requests, _ = make_requests(tmp_path, asyncio.get_running_loop(), Desktop(pick_folder=pick))
        asyncio.get_running_loop().create_task(requests.handlers()["pick_folder"]({}))
        await asyncio.sleep(0.01)

    asyncio.run(body())
    dialogs = [t for t in threading.enumerate() if t.name == "folder-dialog"]
    assert dialogs, "the dialog should have outlived the loop"
    release.set()
    for thread in dialogs:
        thread.join(2)
    assert raised == []


class FakeWindow:
    def __init__(self, chosen):
        self.chosen = chosen

    def create_file_dialog(self, _kind):
        return self.chosen


@pytest.fixture
def fake_webview(monkeypatch):
    module = type("webview", (), {"FileDialog": type("FileDialog", (), {"FOLDER": 20})})
    monkeypatch.setattr("importlib.import_module", lambda _name: module)


class TestFolderPicker:
    def test_nothing_before_there_is_a_window(self):
        assert FolderPicker()() is None

    def test_returns_the_choice(self, fake_webview):
        picker = FolderPicker()
        picker.window = FakeWindow(("C:\\chosen",))
        assert picker() == "C:\\chosen"

    def test_a_cancelled_dialog_is_no_choice(self, fake_webview):
        picker = FolderPicker()
        picker.window = FakeWindow(None)
        assert picker() is None


class RecordingWebview:
    """Stands in for `webview`, keeping what the window was opened with."""

    def __init__(self) -> None:
        self.created: dict[str, Any] = {}
        self.started: dict[str, Any] = {}

    def create_window(self, title, url, **options):
        self.created = {"title": title, "url": url, **options}
        return "the window"

    def start(self, **options):
        self.started = options


class TestRunWindow:
    @pytest.fixture
    def webview(self, monkeypatch):
        recorder = RecordingWebview()
        monkeypatch.setattr("importlib.import_module", lambda _name: recorder)
        return recorder

    def test_opens_the_page_and_hands_the_window_to_the_picker(self, webview):
        picker = FolderPicker()
        run_window("http://127.0.0.1:1/", picker)
        assert webview.created["url"] == "http://127.0.0.1:1/"
        assert webview.created["background_color"] == BACKGROUND
        assert picker.window == "the window"

    def test_the_window_wears_the_app_icon(self, webview):
        run_window("http://127.0.0.1:1/", FolderPicker())
        assert webview.started["icon"] == str(APP_ICON)

    def test_the_app_icon_is_where_the_window_looks(self):
        """pywebview skips an icon path that is not a file, silently."""
        assert APP_ICON.is_file()

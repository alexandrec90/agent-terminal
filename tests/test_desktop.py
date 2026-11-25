import asyncio
import threading
import time
from typing import Any

import pytest

import agent_terminal.__main__ as main_module
from agent_terminal.__main__ import (
    APP_ICON,
    APP_ID,
    BACKGROUND,
    FolderPicker,
    claim_taskbar_identity,
    close_window,
    run_window,
)
from agent_terminal.close_guard import DIALOG_TITLE
from agent_terminal.desktop import Desktop, DesktopRequests, no_picker
from agent_terminal.settings import Settings
from agent_terminal.taskbar import brand_window


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


class TestCloseWindow:
    def test_nothing_to_close_before_there_is_a_window(self):
        close_window(FolderPicker())

    def test_destroys_the_window(self):
        closed: list[bool] = []
        picker = FolderPicker()
        picker.window = type("W", (), {"destroy": lambda _self: closed.append(True)})()
        close_window(picker)
        assert closed == [True]


class RecordedClosing:
    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class RecordedWindow:
    def __init__(self) -> None:
        self.events = type("Events", (), {})()
        self.events.closing = RecordedClosing()
        self.asked: list[str] = []

    def create_confirmation_dialog(self, title, _message):
        self.asked.append(title)
        return False


def no_busy_tabs() -> list[str]:
    return []


class RecordingWebview:
    """Stands in for `webview`, keeping what the window was opened with."""

    def __init__(self) -> None:
        self.created: dict[str, Any] = {}
        self.started: dict[str, Any] = {}

    def create_window(self, title, url, **options):
        self.created = {"title": title, "url": url, **options}
        self.window = RecordedWindow()
        return self.window

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
        run_window("http://127.0.0.1:1/", picker, no_busy_tabs)
        assert webview.created["url"] == "http://127.0.0.1:1/"
        assert webview.created["background_color"] == BACKGROUND
        assert picker.window is webview.window

    def test_the_window_wears_the_app_icon(self, webview):
        run_window("http://127.0.0.1:1/", FolderPicker(), no_busy_tabs)
        assert webview.started["icon"] == str(APP_ICON)

    def test_closing_on_a_busy_agent_asks_and_can_be_cancelled(self, webview):
        run_window("http://127.0.0.1:1/", FolderPicker(), lambda: ["repo: Fix the bug"])
        [on_closing] = webview.window.events.closing.handlers
        assert on_closing() is False
        assert webview.window.asked == [DIALOG_TITLE]

    def test_the_window_is_given_its_taskbar_identity_once_running(self, webview):
        picker = FolderPicker()
        run_window("http://127.0.0.1:1/", picker, no_busy_tabs)
        assert webview.started["func"] is brand_window
        assert webview.started["args"] == (webview.window, APP_ICON)

    def test_the_app_icon_is_where_the_window_looks(self):
        """pywebview skips an icon path that is not a file, silently."""
        assert APP_ICON.is_file()

    def test_the_taskbar_identity_is_claimed_before_the_window_exists(self, webview, monkeypatch):
        """Without its own AppUserModelID the taskbar groups the window under pythonw.exe
        and shows Python's icon, whatever icon the window itself wears."""
        order: list[str] = []
        monkeypatch.setattr(main_module, "claim_taskbar_identity", lambda: order.append("app id"))
        create = webview.create_window

        def create_window(*args, **options):
            order.append("window")
            return create(*args, **options)

        monkeypatch.setattr(webview, "create_window", create_window)
        run_window("http://127.0.0.1:1/", FolderPicker(), no_busy_tabs)
        assert order == ["app id", "window"]


class FakeShell32:
    def __init__(self, result: int = 0) -> None:
        self.result = result
        self.ids: list[str] = []

    def SetCurrentProcessExplicitAppUserModelID(self, app_id: str) -> int:
        self.ids.append(app_id)
        return self.result


class TestClaimTaskbarIdentity:
    def test_sets_the_app_id_on_windows(self, monkeypatch):
        shell32 = FakeShell32()
        monkeypatch.setattr(
            main_module.ctypes, "windll", type("windll", (), {"shell32": shell32}), raising=False
        )
        assert claim_taskbar_identity() is True
        assert shell32.ids == [APP_ID]

    def test_a_refused_call_is_reported_not_raised(self, monkeypatch):
        shell32 = FakeShell32(result=-2147024809)  # E_INVALIDARG
        monkeypatch.setattr(
            main_module.ctypes, "windll", type("windll", (), {"shell32": shell32}), raising=False
        )
        assert claim_taskbar_identity() is False

    def test_does_nothing_where_there_is_no_windll(self, monkeypatch):
        monkeypatch.delattr(main_module.ctypes, "windll", raising=False)
        assert claim_taskbar_identity() is False

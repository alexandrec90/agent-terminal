import threading
from pathlib import Path

import pytest

from agent_terminal import taskbar
from agent_terminal.instance import WINDOW_TITLE
from agent_terminal.taskbar import (
    APP_ID,
    PID_ID,
    PID_RELAUNCH_COMMAND,
    PID_RELAUNCH_DISPLAY_NAME,
    PID_RELAUNCH_ICON,
    brand_window,
    icon_resource,
    relaunch_command,
    window_properties,
)


class TestRelaunchCommand:
    def test_the_launcher_this_copy_started_from(self, tmp_path):
        launcher = tmp_path / "agent-terminal.exe"
        launcher.write_bytes(b"")
        assert relaunch_command(str(launcher), "python.exe") == f'"{launcher.resolve()}"'

    def test_a_launcher_named_without_its_extension(self, tmp_path):
        launcher = tmp_path / "agent-terminal.exe"
        launcher.write_bytes(b"")
        assert relaunch_command(str(tmp_path / "agent-terminal"), "python.exe") == (
            f'"{launcher.resolve()}"'
        )

    def test_run_as_a_module_relaunches_through_the_windowed_interpreter(self, tmp_path):
        python = tmp_path / "python.exe"
        pythonw = tmp_path / "pythonw.exe"
        pythonw.write_bytes(b"")
        command = relaunch_command(str(tmp_path / "__main__.py"), str(python))
        assert command == f'"{pythonw}" -m agent_terminal'

    def test_no_windowed_interpreter_falls_back_to_this_one(self, tmp_path):
        python = tmp_path / "python.exe"
        command = relaunch_command(str(tmp_path / "__main__.py"), str(python))
        assert command == f'"{python}" -m agent_terminal'

    def test_an_empty_argv0_is_not_a_launcher(self, tmp_path):
        python = tmp_path / "python"
        assert relaunch_command("", str(python)) == f'"{python}" -m agent_terminal'


def test_the_icon_is_in_the_shells_file_index_form():
    assert icon_resource(Path("C:/app/app.ico")) == f"{Path('C:/app/app.ico')},0"


def test_the_id_is_written_before_the_relaunch_properties_that_need_it():
    properties = window_properties('"x.exe"', Path("app.ico"))
    assert properties[0] == (PID_ID, APP_ID)
    assert dict(properties) == {
        PID_ID: APP_ID,
        PID_RELAUNCH_COMMAND: '"x.exe"',
        PID_RELAUNCH_DISPLAY_NAME: WINDOW_TITLE,
        PID_RELAUNCH_ICON: f"{Path('app.ico')},0",
    }


class FakeWindow:
    def __init__(self, shown: bool) -> None:
        event = threading.Event()
        if shown:
            event.set()
        self.events = type("Events", (), {"shown": event})()


class TestBrandWindow:
    @pytest.fixture
    def written(self, monkeypatch):
        calls: list[tuple[int, str, Path]] = []
        monkeypatch.setattr(taskbar, "set_window_identity", lambda *a: calls.append(a))
        return calls

    def test_brands_the_window_once_shown(self, monkeypatch, written):
        monkeypatch.setattr(taskbar, "find_app_window", lambda: 42)
        brand_window(FakeWindow(shown=True), Path("app.ico"))
        assert [(hwnd, icon) for hwnd, _, icon in written] == [(42, Path("app.ico"))]

    def test_a_window_never_shown_is_left_alone(self, monkeypatch, written):
        monkeypatch.setattr(taskbar, "find_app_window", lambda: 42)
        brand_window(FakeWindow(shown=False), Path("app.ico"), timeout=0.01)
        assert written == []

    def test_a_window_that_cannot_be_found_is_left_alone(self, monkeypatch, written):
        monkeypatch.setattr(taskbar, "find_app_window", lambda: None)
        brand_window(FakeWindow(shown=True), Path("app.ico"))
        assert written == []

    def test_a_refusal_from_the_shell_does_not_take_the_app_down(self, monkeypatch, caplog):
        def refuse(*_args):
            raise OSError("refused")

        monkeypatch.setattr(taskbar, "find_app_window", lambda: 42)
        monkeypatch.setattr(taskbar, "set_window_identity", refuse)
        brand_window(FakeWindow(shown=True), Path("app.ico"))
        assert "taskbar identity" in caplog.text


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_off_windows_there_is_nothing_to_claim(monkeypatch, platform):
    monkeypatch.setattr("sys.platform", platform)
    taskbar.claim_process_identity()
    assert taskbar.find_app_window() is None


@pytest.mark.ci_e2e  # the real shell property store on a window of its own; no PC state
def test_the_shell_keeps_what_is_written_on_a_real_window():
    """The vtable slots and PROPVARIANT layout only prove out against the real shell."""
    import ctypes

    user32 = ctypes.windll.user32
    user32.CreateWindowExW.restype = ctypes.c_void_p
    hwnd = user32.CreateWindowExW(0, "STATIC", "taskbar test", 0, 0, 0, 0, 0, 0, 0, 0, 0)
    assert hwnd, "could not create a window to brand"
    try:
        icon = Path("C:/app/app.ico")
        taskbar.set_window_identity(hwnd, '"C:\\app\\agent-terminal.exe"', icon)
        assert taskbar.read_window_property(hwnd, PID_ID) == APP_ID
        assert taskbar.read_window_property(hwnd, PID_RELAUNCH_COMMAND) == (
            '"C:\\app\\agent-terminal.exe"'
        )
        assert taskbar.read_window_property(hwnd, PID_RELAUNCH_ICON) == icon_resource(icon)
        assert taskbar.read_window_property(hwnd, PID_RELAUNCH_DISPLAY_NAME) == WINDOW_TITLE
    finally:
        user32.DestroyWindow(ctypes.c_void_p(hwnd))

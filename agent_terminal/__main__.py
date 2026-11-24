"""Open the window. Started as `agent-terminal`, or `python -m agent_terminal`."""

from __future__ import annotations

import ctypes
import importlib
import logging
import sys
from pathlib import Path
from typing import Any

from agent_terminal.desktop import Desktop
from agent_terminal.instance import WINDOW_TITLE, acquire_instance_lock, focus_running_window
from agent_terminal.server import Backend
from agent_terminal.settings import Settings
from agent_terminal.taskbar import brand_window, claim_process_identity

log = logging.getLogger(__name__)

WINDOW_SIZE = (1400, 900)
BACKGROUND = "#0c0c0f"
# pywebview documents `icon` as GTK and Qt only, but its WinForms window loads it too, as
# a `System.Drawing.Icon`; without it the window wears python.exe's. Drawn by
# `scripts/build_app_icon.py`.
APP_ICON = Path(__file__).resolve().parent / "web" / "icons" / "app.ico"
# The process runs as pythonw.exe, and without an AppUserModelID of its own the taskbar
# files the window under that executable and shows Python's icon, not APP_ICON.
APP_ID = "AgentTerminal.App"


def claim_taskbar_identity() -> bool:
    """Give this process its own taskbar identity, so its button wears the window's icon.
    False where there is no Windows shell or it refused; the window still opens."""
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return False
    result = windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    if result != 0:
        log.warning("taskbar identity refused: HRESULT %#x", result & 0xFFFFFFFF)
        return False
    return True


class FolderPicker:
    """The native folder dialog, which needs a window that does not exist until the
    backend is already running."""

    def __init__(self) -> None:
        self.window: Any = None

    def __call__(self) -> str | None:
        """The folder chosen, or None if the dialog was cancelled."""
        if self.window is None:
            return None
        webview = importlib.import_module("webview")
        chosen = self.window.create_file_dialog(webview.FileDialog.FOLDER)
        return str(chosen[0]) if chosen else None


def run_window(url: str, picker: FolderPicker) -> None:
    """Show the page, returning when the window closes. `webview` is imported here
    because it needs the Windows runtime the suite does not have."""
    webview = importlib.import_module("webview")
    # Before the window: the taskbar reads the identity when the button is created.
    claim_taskbar_identity()
    picker.window = webview.create_window(
        WINDOW_TITLE,
        url,
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        background_color=BACKGROUND,
    )
    webview.start(func=brand_window, args=(picker.window, APP_ICON), icon=str(APP_ICON))


def main() -> int:
    """Run the app, or hand over to the copy already running."""
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=settings.log_file,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    lock = acquire_instance_lock(settings.lock_file)
    if lock is None:
        focus_running_window()
        return 0
    claim_process_identity()
    picker = FolderPicker()
    backend = Backend(settings, desktop=Desktop(pick_folder=picker))
    try:
        backend.start()
        run_window(backend.url, picker)
    except Exception:
        log.exception("agent terminal failed")
        raise
    finally:
        # On this thread, after the window: the process must not exit mid-save.
        backend.stop()
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

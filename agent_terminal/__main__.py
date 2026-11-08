"""Open the window. Started as `agent-terminal`, or `python -m agent_terminal`."""

from __future__ import annotations

import importlib
import logging
import sys
from typing import Any

from agent_terminal.desktop import Desktop
from agent_terminal.instance import WINDOW_TITLE, acquire_instance_lock, focus_running_window
from agent_terminal.server import Backend
from agent_terminal.settings import Settings

log = logging.getLogger(__name__)

WINDOW_SIZE = (1400, 900)
BACKGROUND = "#0c0c0f"


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
    picker.window = webview.create_window(
        WINDOW_TITLE,
        url,
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        background_color=BACKGROUND,
    )
    webview.start()


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

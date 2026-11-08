"""The real thing, end to end: the installed package writing to a real Windows console.

Marked `local`, so only `scripts/e2e.py` runs it: the gate is Linux and has no console. The
product is a Windows terminal, and the package has no entry point yet, so `PROBE` stands in
for one. It starts where a user's shell would, from the installed package in a console of
its own and outside the checkout, prints a line, and reads that line back off the screen
buffer. Once the app can be launched, launch it here instead of `PROBE`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.local

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCH_TIMEOUT_SECONDS = 60

# Reports through a file, a traceback included: a process in its own console has no pipe
# back to this one.
PROBE = r"""
import ctypes
import importlib.metadata
import json
import sys
import traceback
from ctypes import wintypes
from pathlib import Path

STD_OUTPUT_HANDLE = -11


def probe():
    import agent_terminal

    banner = f"agent-terminal {agent_terminal.__version__}"
    print(banner, flush=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetConsoleWindow.restype = wintypes.HWND
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.ReadConsoleOutputCharacterW.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes._COORD,
        wintypes.LPDWORD,
    )
    cells = ctypes.create_unicode_buffer(len(banner))
    read = wintypes.DWORD()
    if not kernel32.ReadConsoleOutputCharacterW(
        kernel32.GetStdHandle(STD_OUTPUT_HANDLE),
        cells,
        len(banner),
        wintypes._COORD(0, 0),
        ctypes.byref(read),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return {
        "package": agent_terminal.__file__,
        "installed": importlib.metadata.version("agent-terminal"),
        "interactive": sys.stdout.isatty(),
        "window": bool(kernel32.GetConsoleWindow()),
        "banner": banner,
        "screen": cells[: read.value],
    }


try:
    outcome = probe()
except Exception:
    outcome = {"error": traceback.format_exc()}
Path(sys.argv[1]).write_text(json.dumps(outcome), encoding="utf-8")
"""


def declared_version() -> str:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["project"]["version"]


@pytest.fixture
def launched(tmp_path: Path) -> dict[str, object]:
    """What `PROBE` saw from a console of its own, hidden so no window opens on anyone.

    `-I` and the cwd keep the checkout off `sys.path`, so only the install can supply the
    package. No stream is redirected: one would hand all three to the child in place of
    the console under test.
    """
    result = tmp_path / "probe.json"
    # A failure, not a skip: this tier owes the run. Spelled as `if`/`else` because that is
    # the form mypy leaves unread on the Linux gate, whose `subprocess` has none of these.
    if sys.platform != "win32":
        pytest.fail("the console under test exists on Windows alone")
    else:
        subprocess.run(
            [sys.executable, "-I", "-c", PROBE, str(result)],
            cwd=tmp_path,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            startupinfo=subprocess.STARTUPINFO(
                dwFlags=subprocess.STARTF_USESHOWWINDOW, wShowWindow=subprocess.SW_HIDE
            ),
            timeout=LAUNCH_TIMEOUT_SECONDS,
            check=True,
        )
    outcome = json.loads(result.read_text(encoding="utf-8"))
    assert "error" not in outcome, outcome.get("error")
    return outcome


def test_the_package_runs_from_this_checkout_s_install(launched):
    assert Path(str(launched["package"])).is_relative_to(REPO_ROOT)
    assert launched["installed"] == declared_version()


def test_its_output_reaches_a_real_console(launched):
    assert launched["window"]
    assert launched["interactive"]
    assert launched["banner"] == f"agent-terminal {declared_version()}"
    assert launched["screen"] == launched["banner"]

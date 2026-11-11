"""Runs the real app and drives its real window over the DevTools protocol."""

from __future__ import annotations

import asyncio
import base64
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiohttp

from agent_terminal.instance import WINDOW_TITLE
from agent_terminal.pty_session import kill_tree

ROOT = Path(__file__).resolve().parents[2]
FAKE_AGENT = Path(__file__).with_name("fake-agent.ps1")
FAKE_GH = Path(__file__).with_name("fake-gh.cmd")
WM_CLOSE = 0x0010
WAIT_SECONDS = 25.0
POLL_SECONDS = 0.1
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Window:
    """One DevTools session on the app's page."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        self._next_id = 0

    async def _call(self, method: str, **params: Any) -> dict[str, Any]:
        self._next_id += 1
        message_id = self._next_id
        await self._ws.send_json({"id": message_id, "method": method, "params": params})
        while True:
            reply = await self._ws.receive_json()
            if reply.get("id") == message_id:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply["result"]

    async def js(self, expression: str) -> Any:
        """Evaluate in the page, awaiting a promise, and return the value."""
        result = await self._call(
            "Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True
        )
        if "exceptionDetails" in result:
            raise RuntimeError(f"{expression}: {result['exceptionDetails']}")
        return result["result"].get("value")

    async def wait(self, expression: str, what: str) -> Any:
        """Poll a page expression until it is truthy."""
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            value = await self.js(expression)
            if value:
                return value
            await asyncio.sleep(POLL_SECONDS)
        raise AssertionError(f"timed out waiting for {what}: {expression}")

    async def click(self, selector: str) -> None:
        await self.js(f"document.querySelector({json.dumps(selector)}).click()")

    async def right_click(self, selector: str) -> None:
        await self.js(
            f"document.querySelector({json.dumps(selector)}).dispatchEvent("
            "new MouseEvent('contextmenu', {bubbles: true, cancelable: true}))"
        )

    async def screenshot(self, path: Path) -> None:
        shot = await self._call("Page.captureScreenshot", format="png")
        await asyncio.to_thread(path.write_bytes, base64.b64decode(shot["data"]))

    async def choose_project(self, project: str) -> None:
        """Open the dialog from the "+" outside the groups and pick a listed project."""
        button = (
            "[...document.querySelectorAll('.project')]"
            f".find(b => b.textContent === {json.dumps(project)})"
        )
        await self.click("#new-group")
        await self.wait(f"!!{button}", "project button")
        await self.js(f"{button}.click()")
        await self.dialog_ready()

    async def choose_path(self, path: Path) -> None:
        """Open the dialog and paste a folder's path, as for one that is not listed."""
        await self.click("#new-group")
        await self.type_path(str(path))

    async def type_path(self, path: str) -> None:
        await self.js(
            "(() => { const input = document.getElementById('folder-path');"
            f"input.value = {json.dumps(path)};"
            "input.dispatchEvent(new Event('input', {bubbles: true})); })()"
        )

    async def dialog_ready(self) -> None:
        """Wait for the backend's answer about the chosen folder."""
        await self.wait("!document.getElementById('new-tab-start').disabled", "the folder's facts")

    async def start_session(self, agent: str = "claude", worktree: bool = False) -> None:
        await self.dialog_ready()
        await self.click(f"input[name=agent][value={agent}]")
        if worktree:
            await self.click("#worktree")
        await self.click("#new-tab-start")

    async def new_tab(self, project: str, agent: str = "claude", worktree: bool = False) -> None:
        """Use the dialog, as the user does, from the "+" outside the groups."""
        await self.choose_project(project)
        await self.start_session(agent, worktree)

    async def notes(self) -> str:
        """What the dialog is warning about."""
        return await self.js("document.getElementById('folder-notes').textContent")

    async def review_text(self) -> str:
        return await self.js("document.getElementById('review').textContent")

    async def type_into(self, tab_id: str, text: str) -> None:
        """Type into a tab through its page-side input path."""
        await self.js(
            f"window.__agentTerminal.send({{type:'input', id:{json.dumps(tab_id)}, data:{json.dumps(text)}}})"
        )

    async def tabs(self) -> list[dict[str, Any]]:
        return await self.js("window.__agentTerminal.tabs()")

    async def tab_class(self, tab_id: str) -> str:
        return await self.js(f"document.querySelector('[data-id=\"{tab_id}\"]')?.className ?? ''")

    async def wait_class(self, tab_id: str, predicate: Callable[[str], bool], what: str) -> str:
        deadline = time.monotonic() + WAIT_SECONDS
        current = ""
        while time.monotonic() < deadline:
            current = await self.tab_class(tab_id)
            if predicate(current):
                return current
            await asyncio.sleep(POLL_SECONDS)
        raise AssertionError(f"tab never became {what}; class is {current!r}")


class App:
    """The app under test, with its data and projects in a temp directory."""

    root = ROOT

    def __init__(self, base: Path, *, real_agents: bool = False) -> None:
        self.base = base
        self.projects = base / "projects"
        self.agent_log = base / "fake-agent.jsonl"
        self.claude_config = base / "claude.json"
        self.gh_state = base / "fake-gh.json"
        self.gh_log = base / "fake-gh.log"
        self.port = free_port()
        self.real_agents = real_agents
        self.process: subprocess.Popen[bytes] | None = None
        self.session: aiohttp.ClientSession | None = None
        self.window: Window | None = None

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--remote-debugging-port={self.port}"
        env["AGENT_TERMINAL_DATA_DIR"] = str(self.base / "data")
        env["AGENT_TERMINAL_PROJECTS_ROOT"] = str(self.projects)
        env["FAKE_AGENT_LOG"] = str(self.agent_log)
        env["FAKE_AGENT_PYTHON"] = sys.executable
        if not self.real_agents:
            env["AGENT_TERMINAL_CLAUDE_COMMAND"] = str(FAKE_AGENT)
            env["AGENT_TERMINAL_CODEX_COMMAND"] = str(FAKE_AGENT)
            env["AGENT_TERMINAL_CLAUDE_HOME"] = str(self.base / "claude-home")
            env["AGENT_TERMINAL_CODEX_HOME"] = str(self.base / "codex-home")
            env["AGENT_TERMINAL_CLAUDE_CONFIG_FILE"] = str(self.claude_config)
            env["AGENT_TERMINAL_GH_COMMAND"] = str(FAKE_GH)
            env["AGENT_TERMINAL_PR_POLL_SECONDS"] = "1"
            env["FAKE_GH_STATE"] = str(self.gh_state)
            env["FAKE_GH_LOG"] = str(self.gh_log)
            env.update(GIT_IDENTITY)
        return env

    @staticmethod
    def command() -> list[str]:
        return [sys.executable, "-m", "agent_terminal"]

    def _page_url(self) -> str:
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise AssertionError(f"app exited with {self.process.returncode}")
            try:
                url = f"http://127.0.0.1:{self.port}/json"
                with urllib.request.urlopen(url, timeout=1) as response:
                    targets = json.load(response)
                pages = [
                    t for t in targets if t.get("type") == "page" and "token=" in t.get("url", "")
                ]
                if pages:
                    return pages[0]["webSocketDebuggerUrl"]
            except OSError:
                pass
            time.sleep(0.2)
        raise AssertionError("the window never appeared")

    def _launch(self) -> str:
        self.process = subprocess.Popen(self.command(), cwd=ROOT, env=self.env())
        return self._page_url()

    async def start(self) -> Window:
        debugger = await asyncio.to_thread(self._launch)
        self.session = aiohttp.ClientSession()
        ws = await self.session.ws_connect(debugger, max_msg_size=64 * 1024 * 1024)
        self.window = Window(ws)
        await self.window.wait("window.__agentTerminal?.ready()", "the page to connect")
        return self.window

    @staticmethod
    def _process_tree(pid: int) -> set[int]:
        """`pid` and everything under it: a venv's `python.exe` and `agent-terminal.exe`
        are launchers, so the app and its window sit one or two levels below the process
        `Popen` returned."""
        script = (
            "$all = Get-CimInstance Win32_Process;"
            f"$tree = @({pid});"
            "do { $more = @($all | Where-Object {"
            " $_.ParentProcessId -in $tree -and $_.ProcessId -notin $tree"
            " } | ForEach-Object { $_.ProcessId }); $tree += $more } while ($more);"
            "$tree"
        )
        found = subprocess.run(
            ["pwsh", "-NoProfile", "-Command", script],  # noqa: S607 - pwsh from PATH, as the app runs it
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return {int(line) for line in found.stdout.split()}

    def _post_close(self, pid: int) -> None:
        """Send WM_CLOSE, which is what the title bar's X sends, to this process's window.
        Found by owner, as a title would also match a copy of the app in real use."""
        if sys.platform != "win32":
            raise AssertionError("the window exists on Windows alone")
        user32 = ctypes.WinDLL("user32")
        owners = self._process_tree(pid)
        windows: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def visit(window: int, _context: int) -> bool:
            owner = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(ctypes.c_void_p(window), ctypes.byref(owner))
            title = ctypes.create_unicode_buffer(64)
            user32.GetWindowTextW(ctypes.c_void_p(window), title, 64)
            if owner.value in owners and title.value == WINDOW_TITLE:
                windows.append(window)
            return True

        user32.EnumWindows(visit, None)
        assert windows, f"process {pid} has no window titled {WINDOW_TITLE!r}"
        user32.PostMessageW(ctypes.c_void_p(windows[0]), WM_CLOSE, None, None)

    async def close_window(self) -> None:
        """Close the window for real, so the app saves and shuts down as it does in use."""
        assert self.process is not None
        await asyncio.to_thread(self._post_close, self.process.pid)
        await self.dispose()
        code = await asyncio.to_thread(self.process.wait, 30)
        assert code == 0, f"the app exited {code}"

    async def dispose(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    def kill(self) -> None:
        """End the app and everything under it; killing the launcher alone orphans it."""
        if self.process is not None and self.process.poll() is None:
            kill_tree(self.process.pid)

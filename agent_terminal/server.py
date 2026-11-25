"""The loopback server the window's page talks to.

A socket on localhost is reachable by any page in any browser on the machine, and this
one types into shells. So it takes a per-run token and only its own origin.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from agent_terminal import close_guard
from agent_terminal.desktop import Desktop
from agent_terminal.host_client import HostSessions
from agent_terminal.manager import TabManager
from agent_terminal.relaunch import launch_relaunch
from agent_terminal.sessions import LocalSessions
from agent_terminal.settings import Settings
from agent_terminal.updates import AppControl

log = logging.getLogger(__name__)

WEB_ROOT = Path(__file__).parent / "web"
HOST = "127.0.0.1"
SHUTDOWN_TIMEOUT_SECONDS = 15
# Asked from the window's own thread as it closes, which hangs while this waits.
BUSY_QUERY_TIMEOUT_SECONDS = 2
# These wait on a native dialog, on git, or on GitHub, for seconds or for as long as the
# user likes. Handled in turn, they would hold up every keystroke to every tab behind them.
SLOW_MESSAGES = frozenset(
    {
        "pick_folder",
        "prepare_worktree",
        "pr_view",
        "pr_merge",
        "pr_close",
        "inspect_folder",
        "past_sessions",
        "resume_session",
        "list_remote_repos",
        "clone_repo",
        "describe_deletion",
        "delete_project",
    }
)


def request_allowed(token: str, query_token: str, origin: str | None, port: int) -> bool:
    """True only for this run's token, from this server's own page. A non-browser
    client sends no origin, but cannot learn the token either."""
    token_matches = secrets.compare_digest(token.encode(), query_token.encode())
    return token_matches and origin in (None, f"http://{HOST}:{port}")


class Backend:
    """The server and tab manager, on an event-loop thread of their own: the window
    needs the main thread."""

    def __init__(
        self,
        settings: Settings,
        web_root: Path = WEB_ROOT,
        desktop: Desktop | None = None,
        close_window: Callable[[], None] | None = None,
        checkout: Path | None = None,
        relaunch: Callable[[Settings, Path, bool], int] = launch_relaunch,
    ) -> None:
        self._settings = settings
        self._web_root = web_root
        self._token = secrets.token_urlsafe(32)
        # Selector, not Windows' default proactor: this loop only needs sockets, and the
        # proactor logs a traceback when the window closes and resets the page's socket.
        self._loop: asyncio.AbstractEventLoop = asyncio.SelectorEventLoop()
        self._ready = threading.Event()
        self._port = 0
        self._outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._hosted = HostSessions(settings, self._loop) if settings.session_host else None
        sessions = self._hosted or LocalSessions()
        self._close_window = close_window
        self._checkout = checkout
        self._relaunch = relaunch
        # Set by a restart that keeps the processes: `stop` then leaves them running.
        self._keep_sessions = False
        # Set by any restart: the user asked for it, so closing then asks nothing.
        self._restarting = False
        self._control = AppControl(
            settings,
            self._outbox.put_nowait,
            self._loop,
            sessions.status,
            self.request_restart,
            checkout=checkout,
        )
        self._manager = TabManager(
            settings,
            self._outbox.put_nowait,
            self._loop,
            sessions=sessions,
            desktop=desktop or Desktop(),
            handlers=self._control.handlers(),
        )
        self._socket: web.WebSocketResponse | None = None
        self._runner: web.AppRunner | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._slow_tasks: set[asyncio.Task[None]] = set()

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self._port}/?token={self._token}"

    def start(self) -> None:
        """Start serving and return once `url` is good."""
        threading.Thread(target=self._loop.run_forever, name="backend", daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._serve(), self._loop).result()

    def busy_tabs(self) -> list[str] | None:
        """From any thread: the tabs whose agent is mid-turn, or None if the event loop
        did not answer in time. None are, for closing's purposes, once a restart was asked for:
        its close runs on the loop, which could not answer either."""
        if self._restarting:
            return []

        async def ask() -> list[str]:
            return close_guard.busy_tabs(self._manager.runner.live.values())

        future = asyncio.run_coroutine_threadsafe(ask(), self._loop)
        try:
            return future.result(timeout=BUSY_QUERY_TIMEOUT_SECONDS)
        except TimeoutError:
            future.cancel()
            log.warning("busy-tab query timed out")
            return None

    def stop(self) -> None:
        """Save the tabs and end every process, or, for a restart that keeps them, leave
        them running in the session host."""
        future = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        try:
            future.result(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        except TimeoutError:
            log.error("shutdown timed out; exiting anyway")
        self._loop.call_soon_threadsafe(self._loop.stop)

    def request_restart(self, update: bool, keep_sessions: bool) -> None:
        """Start the helper that brings the app back, then close the window. Runs on the
        loop; the window's thread then calls `stop`, which honours `keep_sessions`."""
        if self._checkout is None or self._close_window is None:
            raise ValueError("this app cannot restart itself")
        self._keep_sessions = keep_sessions and self._manager.sessions.status()["kept"]
        self._relaunch(self._settings, self._checkout, update)
        self._restarting = True
        self._close_window()

    async def _serve(self) -> None:
        if self._hosted is not None:
            await self._hosted.connect()
        self._control.start()
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/ws", self._websocket)
        app.router.add_static("/", self._web_root)
        # No access log: every request line would write the token to disk.
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, HOST, 0)
        await site.start()
        self._port = self._runner.addresses[0][1]
        self._pump_task = self._loop.create_task(self._pump())

    async def _shutdown(self) -> None:
        for task in self._slow_tasks:
            task.cancel()
        self._control.stop()
        await self._manager.shutdown(keep_processes=self._keep_sessions)
        if self._pump_task is not None:
            self._pump_task.cancel()
        if self._runner is not None:
            await self._runner.cleanup()

    async def _index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(self._web_root / "index.html")

    async def _pump(self) -> None:
        """Send queued messages, one at a time, to the page connected now."""
        while True:
            message = await self._outbox.get()
            socket = self._socket
            if socket is None or socket.closed:
                continue
            try:
                await socket.send_str(json.dumps(message))
            except ConnectionError as exc:
                log.info("page went away mid-send: %s", exc)

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        supplied = request.query.get("token", "")
        if not request_allowed(self._token, supplied, request.headers.get("Origin"), self._port):
            raise web.HTTPForbidden()
        socket = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024)
        await socket.prepare(request)
        self._socket = socket
        async for message in socket:
            if message.type == WSMsgType.TEXT:
                await self._receive(json.loads(message.data))
        return socket

    async def _receive(self, message: dict[str, Any]) -> None:
        """Handle one message: in turn, as keystrokes must be, unless it is slow."""
        if message.get("type") not in SLOW_MESSAGES:
            await self._manager.handle(message)
            return
        task = self._loop.create_task(self._manager.handle(message))
        self._slow_tasks.add(task)
        task.add_done_callback(self._slow_tasks.discard)

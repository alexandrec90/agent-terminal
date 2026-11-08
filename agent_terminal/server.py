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
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from agent_terminal.desktop import Desktop
from agent_terminal.manager import TabManager
from agent_terminal.settings import Settings

log = logging.getLogger(__name__)

WEB_ROOT = Path(__file__).parent / "web"
HOST = "127.0.0.1"
SHUTDOWN_TIMEOUT_SECONDS = 15
# These wait on a native dialog, on git, or on GitHub, for seconds or for as long as the
# user likes. Handled in turn, they would hold up every keystroke to every tab behind them.
SLOW_MESSAGES = frozenset(
    {"pick_folder", "prepare_worktree", "pr_view", "pr_merge", "pr_close", "inspect_folder"}
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
        self._manager = TabManager(
            settings, self._outbox.put_nowait, self._loop, desktop=desktop or Desktop()
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

    def stop(self) -> None:
        """Save the tabs and end every process."""
        future = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        try:
            future.result(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        except TimeoutError:
            log.error("shutdown timed out; exiting anyway")
        self._loop.call_soon_threadsafe(self._loop.stop)

    async def _serve(self) -> None:
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
        await self._manager.shutdown()
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

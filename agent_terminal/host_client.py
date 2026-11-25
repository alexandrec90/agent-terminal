"""The app's side of the session host: `HostSessions` keeps every tab's process there.

Everything here runs on the backend's event loop, except `RemoteSession.start` and
`close`, which the runner calls from the executor and which wait on the loop for the
host's answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from agent_terminal.detached import spawn_detached
from agent_terminal.host_protocol import (
    HOST,
    LINE_LIMIT,
    PACKAGE_DIR,
    PROTOCOL,
    HostAddress,
    decode,
    encode,
    host_build,
    read_address,
)
from agent_terminal.pty_session import SpawnError
from agent_terminal.sessions import LocalSessions, OnExit, OnOutput, Session, Survivor
from agent_terminal.settings import Settings

log = logging.getLogger(__name__)

CONNECT_SECONDS = 15.0
REQUEST_SECONDS = 30.0
POLL_SECONDS = 0.1


class HostError(Exception):
    """The host refused a request, or could not be reached."""


class HostConnection:
    """One connection to the host: requests with their replies, and the output and exits
    of each process, handed to whoever listens for that process."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self._loop = loop
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_request = 0
        self._listeners: dict[str, RemoteSession] = {}
        # Events for a process nobody listens for yet: a survivor before it is adopted.
        self._unclaimed: dict[str, list[dict[str, Any]]] = {}
        self._read_task: asyncio.Task[None] | None = None
        self._closing = False
        self.welcome: dict[str, Any] = {}

    async def open(
        self, address: HostAddress, wait_seconds: float = CONNECT_SECONDS
    ) -> dict[str, Any]:
        """Connect and say hello, returning the host's welcome."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(HOST, address.port, limit=LINE_LIMIT), wait_seconds
        )
        self._reader, self._writer = reader, writer
        self.send({"type": "hello", "token": address.token, "protocol": PROTOCOL})
        try:
            welcome = decode(await asyncio.wait_for(reader.readline(), wait_seconds))
        except ValueError as exc:
            writer.close()
            raise HostError(f"the host did not answer: {exc}") from exc
        if welcome.get("type") != "welcome":
            writer.close()
            raise HostError("the host did not welcome this app")
        self.welcome = welcome
        self._read_task = self._loop.create_task(self._read())
        return welcome

    @property
    def closed(self) -> bool:
        return self._writer is None or self._writer.is_closing()

    def send(self, message: dict[str, Any]) -> None:
        """Send a message that wants no answer. On the loop only."""
        writer = self._writer
        if writer is not None and not writer.is_closing():
            writer.write(encode(message))

    async def request(
        self, message: dict[str, Any], wait_seconds: float = REQUEST_SECONDS
    ) -> dict[str, Any]:
        """Send a message and wait for its reply. Raises `HostError`."""
        if self.closed:
            raise HostError("not connected to the session host")
        self._next_request += 1
        number = self._next_request
        answer: asyncio.Future[dict[str, Any]] = self._loop.create_future()
        self._pending[number] = answer
        try:
            self.send({**message, "req": number})
            reply = await asyncio.wait_for(answer, wait_seconds)
        except TimeoutError as exc:
            raise HostError(f"the host did not answer {message.get('type')}") from exc
        finally:
            self._pending.pop(number, None)
        if reply.get("error"):
            raise HostError(str(reply["error"]))
        return reply

    def listen(self, key: str, session: RemoteSession) -> list[dict[str, Any]]:
        """Hand a process's events to `session` from now on, returning those that came
        before anyone listened."""
        self._listeners[key] = session
        return self._unclaimed.pop(key, [])

    def forget(self, key: str) -> None:
        """Stop handing on a process's events."""
        self._listeners.pop(key, None)
        self._unclaimed.pop(key, None)

    async def close(self) -> None:
        """Disconnect on purpose: the host keeps every process."""
        self._closing = True
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await self._writer.wait_closed()
        if self._read_task is not None:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task

    async def _read(self) -> None:
        reader = self._reader
        if reader is None:
            return
        try:
            while line := await reader.readline():
                try:
                    self.dispatch(decode(line))
                except ValueError as exc:
                    log.warning("unreadable message from the host: %s", exc)
        except (ConnectionError, ValueError) as exc:
            log.warning("lost the session host: %s", exc)
        finally:
            self._lost()

    def dispatch(self, message: dict[str, Any]) -> None:
        """Route one message from the host."""
        kind = message.get("type")
        if kind == "reply":
            answer = self._pending.get(int(message.get("req", -1)))
            if answer is not None and not answer.done():
                answer.set_result(message)
            return
        if kind not in ("output", "exit"):
            log.warning("unknown message from the host: %r", kind)
            return
        key = str(message.get("key"))
        listener = self._listeners.get(key)
        if listener is None:
            self._unclaimed.setdefault(key, []).append(message)
        else:
            listener.host_event(message)

    def _lost(self) -> None:
        """The connection ended. Unless the app ended it, the host has gone, and every
        process with it: a pseudo-console does not outlive the process that made it."""
        if self._writer is not None:
            self._writer.close()
        for answer in self._pending.values():
            if not answer.done():
                answer.set_exception(HostError("the session host went away"))
        if self._closing:
            return
        for listener in list(self._listeners.values()):
            listener.host_event({"type": "exit", "code": None})
        self._listeners.clear()


class RemoteSession:
    """A tab's process, held by the host. The same face as `PtySession`."""

    def __init__(
        self,
        connection: HostConnection,
        key: str,
        tab_id: str,
        on_output: OnOutput,
        on_exit: OnExit,
        offset: int = 0,
    ) -> None:
        self._connection = connection
        self._loop = connection.loop
        self.key = key
        self._tab_id = tab_id
        self._on_output = on_output
        self._on_exit = on_exit
        # How much of the process's output has been handed on: the host's own count.
        self.offset = offset
        self._closing = False

    def _wait(self, message: dict[str, Any]) -> dict[str, Any]:
        """A request from a thread that is not the loop's."""
        future = asyncio.run_coroutine_threadsafe(self._connection.request(message), self._loop)
        return future.result()

    def _post(self, message: dict[str, Any]) -> None:
        try:
            self._loop.call_soon_threadsafe(self._connection.send, message)
        except RuntimeError:
            log.debug("event loop closed; dropped %s", message.get("type"))

    def start(
        self, argv: list[str], cwd: str, env: Mapping[str, str], size: tuple[int, int]
    ) -> None:
        """Have the host spawn the process. Raises `SpawnError`, as `PtySession` does."""
        message = {
            "type": "spawn",
            "key": self.key,
            "tab_id": self._tab_id,
            "argv": argv,
            "cwd": cwd,
            "env": dict(env),
            "rows": size[0],
            "cols": size[1],
        }
        try:
            self._wait(message)
        except HostError as exc:
            raise SpawnError(str(exc)) from exc

    def write(self, text: str) -> None:
        self._post({"type": "input", "key": self.key, "data": text})

    def resize(self, rows: int, cols: int) -> None:
        self._post({"type": "resize", "key": self.key, "rows": rows, "cols": cols})

    def remember(self, state: dict[str, Any]) -> None:
        """Keep the tab's state with the process, for the app that comes next."""
        self._post({"type": "remember", "key": self.key, "state": state, "offset": self.offset})

    def close(self) -> None:
        """End the process without reporting an exit. Blocks on the host."""
        self._closing = True
        self._loop.call_soon_threadsafe(self._connection.forget, self.key)
        try:
            self._wait({"type": "close", "key": self.key})
        except HostError as exc:
            log.info("close of %s: %s", self.key, exc)

    def host_event(self, message: dict[str, Any]) -> None:
        """Output or an exit from the host, on the loop."""
        if message["type"] == "output":
            self.offset = int(message.get("end", self.offset))
            self._on_output(str(message["data"]))
        elif not self._closing:
            self._closing = True
            code = message.get("code")
            self._on_exit(int(code) if code is not None else None)


def launch_host(settings: Settings) -> int:
    """Start a host for this data directory, detached from the app."""
    argv = [
        sys.executable,
        "-m",
        "agent_terminal.host",
        "--address-file",
        str(settings.host_address_file),
        "--lock-file",
        str(settings.host_lock_file),
        "--log-file",
        str(settings.host_log_file),
        "--orphan-seconds",
        str(settings.host_orphan_seconds),
    ]
    return spawn_detached(argv, PACKAGE_DIR.parent)


class HostSessions:
    """The `SessionSource` that keeps processes in the host. If the host cannot be had,
    it says so in the log and keeps them here instead, as before the host existed."""

    def __init__(
        self,
        settings: Settings,
        loop: asyncio.AbstractEventLoop,
        fallback: LocalSessions | None = None,
        launch: Callable[[Settings], int] = launch_host,
        build: Callable[[], str] = host_build,
        connect_seconds: float = CONNECT_SECONDS,
    ) -> None:
        self._settings = settings
        self._loop = loop
        self._fallback = fallback or LocalSessions()
        self._launch = launch
        self._build = build
        self._connect_seconds = connect_seconds
        self._connection: HostConnection | None = None
        self._stale = False

    @property
    def connected(self) -> bool:
        return self._connection is not None and not self._connection.closed

    async def connect(self) -> None:
        """Reach the running host, or start one. Never raises: a failure falls back."""
        try:
            self._connection = await self._connect()
        except (HostError, OSError, TimeoutError) as exc:
            log.error("no session host (%s); every tab will end with the window", exc)
            self._connection = None
            return
        self._stale = self._connection.welcome.get("build") != self._build()
        if self._stale:
            log.info("the session host predates this app's code")

    async def _connect(self) -> HostConnection:
        address = read_address(self._settings.host_address_file)
        if address is not None and address.protocol != PROTOCOL:
            log.warning("the session host speaks protocol %d; replacing it", address.protocol)
            await self._retire(address)
            address = None
        if address is not None:
            connection = HostConnection(self._loop)
            try:
                await connection.open(address, self._connect_seconds)
            except (HostError, OSError, TimeoutError) as exc:
                log.info("the published host did not answer (%s); starting one", exc)
            else:
                return connection
        await self._loop.run_in_executor(None, self._launch, self._settings)
        fresh = await self._new_address(address)
        connection = HostConnection(self._loop)
        await connection.open(fresh, self._connect_seconds)
        return connection

    async def _new_address(self, old: HostAddress | None) -> HostAddress:
        deadline = time.monotonic() + self._connect_seconds
        while time.monotonic() < deadline:
            address = read_address(self._settings.host_address_file)
            if address is not None and (old is None or address.token != old.token):
                return address
            await asyncio.sleep(POLL_SECONDS)
        raise HostError(f"no host appeared in {self._connect_seconds:.0f}s")

    async def _retire(self, address: HostAddress) -> None:
        """Ask a host from another protocol to end. `hello` and `shutdown` mean the same
        in every version, which is all this needs."""
        connection = HostConnection(self._loop)
        try:
            await connection.open(address, self._connect_seconds)
            await connection.request({"type": "shutdown"}, self._connect_seconds)
        except (HostError, OSError, TimeoutError) as exc:
            log.info("retiring the old host: %s", exc)
        finally:
            await connection.close()

    def new(self, tab_id: str, on_output: OnOutput, on_exit: OnExit) -> Session:
        """A process that has not started yet."""
        if self._connection is None:
            return self._fallback.new(tab_id, on_output, on_exit)
        key = uuid.uuid4().hex
        session = RemoteSession(self._connection, key, tab_id, on_output, on_exit)
        self._connection.listen(key, session)
        return session

    async def survivors(self) -> dict[str, Survivor]:
        """The host's processes, by tab. A tab with more than one, which only a crash
        halfway through replacing its process leaves, keeps the newest."""
        if self._connection is None:
            return {}
        newest: dict[str, dict[str, Any]] = {}
        for summary in self._connection.welcome.get("sessions", []):
            older = newest.get(summary["tab_id"])
            if older is not None:
                await self._close_key(older["key"])
            newest[summary["tab_id"]] = summary
        found: dict[str, Survivor] = {}
        for tab_id, summary in newest.items():
            try:
                reply = await self._connection.request({"type": "replay", "key": summary["key"]})
            except HostError as exc:
                log.warning("could not replay %s: %s", summary["key"], exc)
                continue
            screen = str(reply.get("screen", ""))
            unread_from = max(int(reply.get("remembered", 0)) - int(reply.get("start", 0)), 0)
            found[tab_id] = Survivor(
                tab_id=tab_id,
                key=str(summary["key"]),
                alive=bool(summary["alive"]),
                exit_code=summary.get("exit_code"),
                state=dict(reply.get("state") or {}),
                screen=screen,
                unread=screen[unread_from:],
                end=int(reply.get("end", 0)),
            )
        return found

    def adopt(self, survivor: Survivor, on_output: OnOutput, on_exit: OnExit) -> Session:
        """Take a survivor on, handing over whatever it did since its screen was read."""
        if self._connection is None:
            raise LookupError("not connected to the session host")
        session = RemoteSession(
            self._connection,
            survivor.key,
            survivor.tab_id,
            on_output,
            on_exit,
            offset=survivor.end,
        )
        for event in self._connection.listen(survivor.key, session):
            if event["type"] == "exit" or int(event.get("end", 0)) > survivor.end:
                session.host_event(event)
        if not survivor.alive:
            session.host_event({"type": "exit", "code": survivor.exit_code})
        return session

    async def discard(self, survivor: Survivor) -> None:
        """End a survivor no tab wants: its tab was closed while the app was away."""
        await self._close_key(survivor.key)

    async def _close_key(self, key: str) -> None:
        if self._connection is None:
            return
        self._connection.forget(key)
        try:
            await self._connection.request({"type": "close", "key": key})
        except HostError as exc:
            log.info("close of %s: %s", key, exc)

    async def detach(self) -> None:
        """Leave every process running in the host, for the next app."""
        if self._connection is not None:
            # A session's messages go out on the loop's next turn: `remember` above all
            # must reach the host before the socket closes.
            await asyncio.sleep(0)
            await self._connection.close()

    async def shutdown(self) -> None:
        """End the host. The runner has already closed every process."""
        if self._connection is None:
            return
        try:
            await self._connection.request({"type": "shutdown"})
        except HostError as exc:
            log.info("host shutdown: %s", exc)
        await self._connection.close()

    def status(self) -> dict[str, Any]:
        """Whether a restart keeps the processes, and whether the host is out of date."""
        return {"kept": self._connection is not None, "stale": self._stale}

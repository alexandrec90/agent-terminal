"""The session host: owns every tab's pseudo-console, so the window can restart.

A pseudo-console ends with the process that made it, so the processes cannot outlive the
app while the app holds them. This process holds them instead. The app starts it,
detached (`host_client.launch_host`), and talks to it over `host_protocol`. When the app
restarts or updates, it leaves the host running, comes back, and is handed each tab's
process and recent output. When the window is closed for good, the app ends every
process and then the host.

One per data directory, held by its own lock, serving one app at a time. Kept small,
and changed rarely: the host only changes by ending everything it holds.

Started as `python -m agent_terminal.host`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import secrets
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_terminal.host_protocol import (
    HOST,
    LINE_LIMIT,
    PROTOCOL,
    HostAddress,
    decode,
    encode,
    host_build,
    remove_address,
    write_address,
)
from agent_terminal.instance import acquire_instance_lock
from agent_terminal.pty_session import Kill, PtySession, Spawn, SpawnError, conpty_spawn, kill_tree

log = logging.getLogger(__name__)

# Per process. xterm keeps 10,000 lines; a TUI repaints often, so this is several screens
# of the agent and most of a long scrollback of a shell.
BACKLOG_CHARS = 2_000_000
CHECK_SECONDS = 5.0
# With nothing running and no app, how long to wait for one: an update keeps the app away
# for as long as git and uv take.
LINGER_SECONDS = 300.0
# With processes running and no app: the app crashed and nobody started it again. A
# session left this long is ended, to be resumed by the next launch.
ORPHAN_SECONDS = 3600.0


class Backlog:
    """The most recent output of one process, and where it ends in all it ever wrote."""

    def __init__(self, limit: int = BACKLOG_CHARS) -> None:
        self._limit = limit
        self._chunks: deque[str] = deque()
        self._size = 0
        self.end = 0

    @property
    def start(self) -> int:
        """Where the held output begins in all the process wrote."""
        return self.end - self._size

    def add(self, text: str) -> None:
        """Hold a chunk, dropping the oldest beyond the limit."""
        if len(text) > self._limit:
            self._chunks.clear()
            self._size = 0
            self.end += len(text) - self._limit
            text = text[-self._limit :]
        self._chunks.append(text)
        self._size += len(text)
        self.end += len(text)
        while self._size > self._limit:
            dropped = self._chunks.popleft()
            self._size -= len(dropped)

    def since(self, offset: int) -> str:
        """What is held from `offset` on: all of it, if `offset` is older than that."""
        held = "".join(self._chunks)
        return held[max(offset - self.start, 0) :]


@dataclass
class Hosted:
    """One process and what the host keeps about it for the app."""

    key: str
    tab_id: str
    session: PtySession
    backlog: Backlog
    alive: bool = True
    exit_code: int | None = None
    # The app's own note about the tab, kept for the next app: see `TabRunner.state_of`.
    state: dict[str, Any] = field(default_factory=dict)
    # How much output the app had read when it wrote `state`.
    remembered: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "tab_id": self.tab_id,
            "alive": self.alive,
            "exit_code": self.exit_code,
        }


@dataclass(frozen=True)
class Lifetimes:
    """How long the host waits for an app to come back: see `SessionHost.check_idle`."""

    orphan_seconds: float = ORPHAN_SECONDS
    linger_seconds: float = LINGER_SECONDS
    clock: Callable[[], float] = time.monotonic


class SessionHost:
    """The host's state and its one connection. Runs on one event loop; each process's
    reader thread hops onto it."""

    def __init__(
        self,
        token: str,
        build: str,
        spawn: Spawn = conpty_spawn,
        kill: Kill = kill_tree,
        lifetimes: Lifetimes | None = None,
    ) -> None:
        self._token = token
        self._build = build
        self._spawn = spawn
        self._kill = kill
        self._lifetimes = lifetimes or Lifetimes()
        self._clock = self._lifetimes.clock
        self.sessions: dict[str, Hosted] = {}
        self.finished = asyncio.Event()
        self._writer: asyncio.StreamWriter | None = None
        self._last_client = self._clock()
        self._server: asyncio.Server | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def serve(self, port: int = 0) -> int:
        """Listen on loopback, returning the port."""
        self._loop = asyncio.get_running_loop()
        self._server = await asyncio.start_server(self._client, HOST, port, limit=LINE_LIMIT)
        return int(self._server.sockets[0].getsockname()[1])

    async def run_until_finished(self, check_seconds: float = CHECK_SECONDS) -> None:
        """Serve until told to shut down, or until nobody is coming back."""
        while not self.finished.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.finished.wait(), check_seconds)
            if not self.finished.is_set():
                await self.check_idle()
        if self._writer is not None:
            self._writer.close()
        if self._server is not None:
            self._server.close()

    async def check_idle(self) -> None:
        """End the host once no app has been connected for too long."""
        if self._writer is not None:
            self._last_client = self._clock()
            return
        away = self._clock() - self._last_client
        running = any(hosted.alive for hosted in self.sessions.values())
        if not running and away >= self._lifetimes.linger_seconds:
            log.info("no app and nothing running for %.0fs; exiting", away)
            self.finished.set()
        elif running and away >= self._lifetimes.orphan_seconds:
            log.warning("no app for %.0fs; ending %d processes", away, len(self.sessions))
            await self._close_all()
            self.finished.set()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            hello = decode(await reader.readline())
        except (ValueError, ConnectionError):
            writer.close()
            return
        supplied = str(hello.get("token", ""))
        if hello.get("type") != "hello" or not secrets.compare_digest(
            supplied.encode(), self._token.encode()
        ):
            log.warning("refused a connection without the token")
            writer.close()
            return
        previous, self._writer = self._writer, writer
        if previous is not None:
            log.info("a new app connected; dropping the old one")
            previous.close()
        self._send(
            {
                "type": "welcome",
                "protocol": PROTOCOL,
                "build": self._build,
                "pid": os.getpid(),
                "sessions": [hosted.summary() for hosted in self.sessions.values()],
            }
        )
        await self._read(reader, writer)

    async def _read(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                try:
                    message = decode(line)
                except ValueError as exc:
                    log.warning("unreadable message: %s", exc)
                    continue
                self.handle(message)
        except (ConnectionError, ValueError) as exc:
            log.info("app connection ended: %s", exc)
        finally:
            if self._writer is writer:
                self._writer = None
                self._last_client = self._clock()
            writer.close()

    def handle(self, message: dict[str, Any]) -> None:
        """Apply one message. The slow ones run as tasks, so typing is never behind them."""
        kind = message.get("type")
        try:
            if kind in ("spawn", "close", "shutdown"):
                self._start_task(str(kind), message)
            elif kind == "replay":
                self._replay(message)
            elif kind in ("input", "resize", "remember"):
                self._to_process(str(kind), self.sessions[message["key"]], message)
            else:
                log.warning("unknown message %r", kind)
        except (KeyError, ValueError, TypeError) as exc:
            log.info("dropped %s message: %r", kind, exc)

    def _start_task(self, kind: str, message: dict[str, Any]) -> None:
        handler = {"spawn": self._spawn_one, "close": self._close_one}.get(kind, self._shutdown)
        task = asyncio.get_running_loop().create_task(handler(message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    def _to_process(kind: str, hosted: Hosted, message: dict[str, Any]) -> None:
        if kind == "input":
            hosted.session.write(str(message["data"]))
        elif kind == "resize":
            hosted.session.resize(int(message["rows"]), int(message["cols"]))
        else:
            hosted.state = dict(message["state"])
            hosted.remembered = int(message["offset"])

    def _reply(self, message: dict[str, Any], **fields: Any) -> None:
        if "req" in message:
            self._send({"type": "reply", "req": message["req"], **fields})

    def _replay(self, message: dict[str, Any]) -> None:
        hosted = self.sessions.get(message["key"])
        if hosted is None:
            self._reply(message, error="no such session")
            return
        self._reply(
            message,
            screen=hosted.backlog.since(0),
            start=hosted.backlog.start,
            end=hosted.backlog.end,
            state=hosted.state,
            remembered=hosted.remembered,
        )

    async def _spawn_one(self, message: dict[str, Any]) -> None:
        key = str(message["key"])
        session = PtySession(
            on_output=lambda text: self._from_reader(self._output, key, text),
            on_exit=lambda code: self._from_reader(self._exited, key, code),
            spawn=self._spawn,
            kill=self._kill,
        )
        self.sessions[key] = Hosted(key, str(message["tab_id"]), session, Backlog())
        size = (int(message["rows"]), int(message["cols"]))
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                session.start,
                list(message["argv"]),
                str(message["cwd"]),
                dict(message["env"]),
                size,
            )
        except SpawnError as exc:
            self.sessions.pop(key, None)
            self._reply(message, error=str(exc))
            return
        self._reply(message)

    async def _close_one(self, message: dict[str, Any]) -> None:
        hosted = self.sessions.pop(str(message["key"]), None)
        if hosted is not None:
            await asyncio.get_running_loop().run_in_executor(None, hosted.session.close)
        self._reply(message)

    async def _close_all(self) -> None:
        held = list(self.sessions.values())
        self.sessions.clear()
        loop = asyncio.get_running_loop()
        await asyncio.gather(*(loop.run_in_executor(None, h.session.close) for h in held))

    async def _shutdown(self, message: dict[str, Any]) -> None:
        log.info("shutting down with %d processes", len(self.sessions))
        await self._close_all()
        self._reply(message)
        self.finished.set()

    def _from_reader(self, callback: Callable[..., None], *args: Any) -> None:
        """Hop from a reader thread to the loop, which may already have stopped."""
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:
            log.debug("event loop closed; dropped %s", callback)

    def _output(self, key: str, text: str) -> None:
        hosted = self.sessions.get(key)
        if hosted is None:
            return
        hosted.backlog.add(text)
        self._send({"type": "output", "key": key, "data": text, "end": hosted.backlog.end})

    def _exited(self, key: str, code: int | None) -> None:
        hosted = self.sessions.get(key)
        if hosted is None:
            return
        hosted.alive = False
        hosted.exit_code = code
        self._send({"type": "exit", "key": key, "code": code})

    def _send(self, message: dict[str, Any]) -> None:
        writer = self._writer
        if writer is not None and not writer.is_closing():
            writer.write(encode(message))


async def run_host(address_file: Path, orphan_seconds: float) -> None:
    """Serve, publishing the address while serving."""
    token = secrets.token_urlsafe(32)
    build = host_build()
    host = SessionHost(token, build, lifetimes=Lifetimes(orphan_seconds=orphan_seconds))
    port = await host.serve()
    write_address(address_file, HostAddress(port, token, os.getpid(), PROTOCOL, build))
    log.info("session host %s listening on %d", build, port)
    try:
        await host.run_until_finished()
    finally:
        remove_address(address_file, os.getpid())


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agent_terminal.host")
    parser.add_argument("--address-file", type=Path, required=True)
    parser.add_argument("--lock-file", type=Path, required=True)
    parser.add_argument("--log-file", type=Path, required=True)
    parser.add_argument("--orphan-seconds", type=float, default=ORPHAN_SECONDS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the host, or leave it to the one already running."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    args.log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=args.log_file,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    lock = acquire_instance_lock(args.lock_file)
    if lock is None:
        log.info("a session host is already running")
        return 0
    try:
        asyncio.run(run_host(args.address_file, args.orphan_seconds))
    except Exception:
        log.exception("session host failed")
        raise
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

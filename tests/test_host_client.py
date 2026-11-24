import asyncio
import queue
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fake_pty import Spawner

from agent_terminal import host_client as module
from agent_terminal.host import SessionHost
from agent_terminal.host_client import (
    HostConnection,
    HostError,
    HostSessions,
    RemoteSession,
    launch_host,
)
from agent_terminal.host_protocol import PROTOCOL, HostAddress, read_address, write_address
from agent_terminal.pty_session import PtySession, SpawnError
from agent_terminal.settings import Settings

TOKEN = "tok"
WAIT = 5


class Rig:
    """Hosts started in this process by a fake `launch`, and the apps that use them."""

    def __init__(self, tmp_path: Path, loop: asyncio.AbstractEventLoop) -> None:
        self.settings = Settings(data_dir=tmp_path)
        self.loop = loop
        self.spawner = Spawner()
        self.hosts: list[SessionHost] = []
        self.launches = 0
        self.build = "b1"

    def launch(self, settings: Settings) -> int:
        self.launches += 1

        async def start() -> None:
            # A fresh token each launch, as a real host has, so the app sees it is new.
            token = f"{TOKEN}{len(self.hosts)}"
            host = SessionHost(token, "b1", self.spawner.spawn, self.spawner.kill)
            port = await host.serve()
            address = HostAddress(port, token, len(self.hosts) + 1, PROTOCOL, "b1")
            write_address(settings.host_address_file, address)
            self.hosts.append(host)

        asyncio.run_coroutine_threadsafe(start(), self.loop).result(WAIT)
        return 1

    def app(self) -> HostSessions:
        return HostSessions(
            self.settings,
            self.loop,
            launch=self.launch,
            build=lambda: self.build,
            connect_seconds=2,
        )

    def close(self) -> None:
        self.spawner.end_all()
        for host in self.hosts:
            host.finished.set()


def run(body):
    def wrapper(tmp_path):
        async def go() -> None:
            rig = Rig(tmp_path, asyncio.get_running_loop())
            try:
                await body(rig)
            finally:
                rig.close()

        asyncio.run(go())

    wrapper.__name__ = body.__name__
    return wrapper


class Ends:
    """What a session hands on."""

    def __init__(self) -> None:
        self.output: queue.Queue[str] = queue.Queue()
        self.exits: list[int | None] = []

    def on_output(self, text: str) -> None:
        self.output.put(text)

    def on_exit(self, code: int | None) -> None:
        self.exits.append(code)

    async def next_output(self) -> str:
        return await asyncio.to_thread(self.output.get, True, WAIT)


async def started(app: HostSessions, ends: Ends, tab_id: str = "t1") -> RemoteSession:
    session = app.new(tab_id, ends.on_output, ends.on_exit)
    assert isinstance(session, RemoteSession)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, session.start, ["pwsh"], "C:\\w", {"E": "1"}, (24, 80))
    return session


async def until(predicate, what: str) -> None:
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"never: {what}")


@run
async def test_connect_starts_a_host_when_none_is_running(rig):
    app = rig.app()
    await app.connect()
    assert rig.launches == 1
    assert app.connected
    assert app.status() == {"kept": True, "stale": False}


@run
async def test_a_session_runs_in_the_host(rig):
    app = rig.app()
    await app.connect()
    ends = Ends()
    session = await started(app, ends)
    process = rig.spawner.processes[0]
    assert (process.argv, process.env, process.size) == (["pwsh"], {"E": "1"}, (24, 80))
    process.reads.put("hi")
    assert await ends.next_output() == "hi"
    assert session.offset == 2
    session.write("x")
    session.resize(30, 100)
    await until(lambda: process.sizes, "the resize")
    assert process.written == ["x"]
    assert process.sizes == [(30, 100)]


@run
async def test_a_spawn_failure_is_a_spawn_error(rig):
    app = rig.app()
    await app.connect()
    rig.spawner.refuse = SpawnError("no shell")
    with pytest.raises(SpawnError, match="no shell"):
        await started(app, Ends())


@run
async def test_the_next_app_takes_over_where_this_one_left(rig):
    first = rig.app()
    await first.connect()
    ends = Ends()
    session = await started(first, ends)
    process = rig.spawner.processes[0]
    process.reads.put("seen ")
    await ends.next_output()
    session.remember({"status": "idle"})
    await first.detach()
    process.reads.put("\x1b]7501;state=working\x1b\\")
    await until(lambda: rig.hosts[0].sessions[session.key].backlog.end > 5, "unread output")

    second = rig.app()
    await second.connect()
    assert rig.launches == 1, "the running host is reused"
    survivors = await second.survivors()
    survivor = survivors["t1"]
    assert survivor.alive
    assert survivor.state == {"status": "idle"}
    assert survivor.screen.startswith("seen ")
    assert survivor.unread == "\x1b]7501;state=working\x1b\\"

    later = Ends()
    adopted = second.adopt(survivor, later.on_output, later.on_exit)
    process.reads.put("next")
    assert await later.next_output() == "next"
    adopted.write("y")
    await until(lambda: process.written, "input to the adopted process")
    assert process.written == ["y"]
    assert ends.exits == [], "a detach is not an exit"


@run
async def test_output_between_welcome_and_adoption_arrives_once(rig):
    first = rig.app()
    await first.connect()
    session = await started(first, Ends())
    await first.detach()
    second = rig.app()
    await second.connect()
    process = rig.spawner.processes[0]
    process.reads.put("before replay")
    await until(lambda: rig.hosts[0].sessions[session.key].backlog.end, "output held")
    survivor = (await second.survivors())["t1"]
    process.reads.put(" after replay")
    await asyncio.sleep(0.05)
    ends = Ends()
    second.adopt(survivor, ends.on_output, ends.on_exit)
    await asyncio.sleep(0.05)
    assert survivor.screen == "before replay"
    assert list(ends.output.queue) == [" after replay"]


@run
async def test_a_process_that_died_while_away_reports_its_exit(rig):
    first = rig.app()
    await first.connect()
    await started(first, Ends())
    await first.detach()
    rig.spawner.processes[0].finish(4)
    await asyncio.sleep(0.05)
    second = rig.app()
    await second.connect()
    survivor = (await second.survivors())["t1"]
    assert not survivor.alive
    ends = Ends()
    second.adopt(survivor, ends.on_output, ends.on_exit)
    assert ends.exits == [4]


@run
async def test_only_the_newest_process_of_a_tab_survives(rig):
    first = rig.app()
    await first.connect()
    await started(first, Ends(), "t1")
    newer = await started(first, Ends(), "t1")
    await first.detach()
    second = rig.app()
    await second.connect()
    survivors = await second.survivors()
    assert survivors["t1"].key == newer.key
    assert rig.spawner.killed == [1000]


@run
async def test_discard_ends_a_survivor(rig):
    first = rig.app()
    await first.connect()
    await started(first, Ends())
    await first.detach()
    second = rig.app()
    await second.connect()
    await second.discard((await second.survivors())["t1"])
    assert rig.spawner.killed == [1000]
    assert rig.hosts[0].sessions == {}


@run
async def test_close_ends_the_process_without_an_exit(rig):
    app = rig.app()
    await app.connect()
    ends = Ends()
    session = await started(app, ends)
    await asyncio.get_running_loop().run_in_executor(None, session.close)
    await asyncio.sleep(0.05)
    assert rig.spawner.killed == [1000]
    assert ends.exits == []


@run
async def test_losing_the_host_ends_every_session(rig):
    app = rig.app()
    await app.connect()
    ends = Ends()
    await started(app, ends)
    writer = rig.hosts[0]._writer
    assert writer is not None
    writer.close()
    await until(lambda: ends.exits, "the exit")
    assert ends.exits == [None]


@run
async def test_shutdown_ends_the_host(rig):
    app = rig.app()
    await app.connect()
    await app.shutdown()
    await asyncio.wait_for(rig.hosts[0].finished.wait(), WAIT)


@run
async def test_a_host_from_older_code_is_stale(rig):
    app = rig.app()
    rig.build = "b2"
    await app.connect()
    assert app.status() == {"kept": True, "stale": True}


@run
async def test_a_host_on_another_protocol_is_retired_and_replaced(rig):
    await asyncio.get_running_loop().run_in_executor(None, rig.launch, rig.settings)
    old = rig.hosts[0]
    published = read_address(rig.settings.host_address_file)
    assert published is not None
    write_address(rig.settings.host_address_file, replace(published, protocol=PROTOCOL + 1))
    app = rig.app()
    await app.connect()
    await asyncio.wait_for(old.finished.wait(), WAIT)
    assert rig.launches == 2
    assert app.connected


@run
async def test_a_dead_published_host_is_replaced(rig):
    write_address(rig.settings.host_address_file, HostAddress(1, "stale", 99, PROTOCOL, "b1"))
    app = rig.app()
    await app.connect()
    assert rig.launches == 1
    assert app.connected


@run
async def test_no_host_falls_back_to_sessions_in_the_app(rig):
    def refuse(_settings: Settings) -> int:
        raise OSError("blocked")

    app = HostSessions(rig.settings, rig.loop, launch=refuse, connect_seconds=1)
    await app.connect()
    assert app.status() == {"kept": False, "stale": False}
    assert isinstance(app.new("t", print, print), PtySession)
    assert await app.survivors() == {}
    with pytest.raises(LookupError):
        app.adopt(module.Survivor(tab_id="t", key="k", alive=True), print, print)
    await app.discard(module.Survivor(tab_id="t", key="k", alive=True))
    await app.detach()
    await app.shutdown()


@run
async def test_a_host_that_never_appears_falls_back(rig):
    app = HostSessions(rig.settings, rig.loop, launch=lambda _s: 1, connect_seconds=0.2)
    await app.connect()
    assert not app.connected
    assert app.status()["kept"] is False


@run
async def test_a_request_on_a_closed_connection_is_a_host_error(rig):
    connection = HostConnection(rig.loop)
    assert connection.closed
    with pytest.raises(HostError):
        await connection.request({"type": "replay", "key": "k"})


@run
async def test_dispatch_routes_by_key_and_ignores_the_unknown(rig):
    connection = HostConnection(rig.loop)
    connection.dispatch({"type": "mystery"})
    connection.dispatch({"type": "reply", "req": 99})
    connection.dispatch({"type": "output", "key": "k", "data": "a", "end": 1})
    ends = Ends()
    session = RemoteSession(connection, "k", "t", ends.on_output, ends.on_exit)
    assert connection.listen("k", session) == [
        {"type": "output", "key": "k", "data": "a", "end": 1}
    ]
    connection.dispatch({"type": "exit", "key": "k", "code": 2})
    connection.dispatch({"type": "exit", "key": "k", "code": 3})
    assert ends.exits == [2], "one exit only"
    connection.forget("k")
    connection.dispatch({"type": "output", "key": "k", "data": "b", "end": 2})
    assert ends.output.empty()


def test_launch_host_starts_the_module_detached(tmp_path, monkeypatch):
    started: list[tuple[list[str], Path]] = []

    def fake(argv: list[str], cwd: Path) -> int:
        started.append((argv, cwd))
        return 7

    monkeypatch.setattr(module, "spawn_detached", fake)
    settings = Settings(data_dir=tmp_path, host_orphan_seconds=12)
    assert launch_host(settings) == 7
    argv, _cwd = started[0]
    assert argv[1:3] == ["-m", "agent_terminal.host"]
    found: dict[str, Any] = dict(zip(argv[3::2], argv[4::2], strict=True))
    assert found["--address-file"] == str(settings.host_address_file)
    assert found["--orphan-seconds"] == "12.0"

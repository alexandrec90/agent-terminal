import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fake_pty import Spawner

from agent_terminal import host as module
from agent_terminal.host import (
    Backlog,
    Hosted,
    Lifetimes,
    SessionHost,
    main,
    parse_args,
    run_host,
)
from agent_terminal.host_protocol import HOST, PROTOCOL, decode, encode, read_address
from agent_terminal.instance import acquire_instance_lock
from agent_terminal.pty_session import PtySession, SpawnError

TOKEN = "secret"
WAIT = 5


class TestBacklog:
    def test_holds_everything_under_the_limit(self):
        backlog = Backlog(limit=10)
        backlog.add("abc")
        backlog.add("def")
        assert backlog.since(0) == "abcdef"
        assert (backlog.start, backlog.end) == (0, 6)

    def test_drops_the_oldest_chunks_past_the_limit(self):
        backlog = Backlog(limit=5)
        for chunk in ("abc", "def", "gh"):
            backlog.add(chunk)
        assert backlog.since(0) == "defgh"
        assert (backlog.start, backlog.end) == (3, 8)

    def test_one_chunk_past_the_limit_keeps_its_tail(self):
        backlog = Backlog(limit=4)
        backlog.add("abcdefgh")
        assert backlog.since(0) == "efgh"
        assert backlog.end == 8

    def test_since_an_offset(self):
        backlog = Backlog(limit=100)
        backlog.add("hello world")
        assert backlog.since(6) == "world"
        assert backlog.since(11) == ""


class Client:
    """The app's end of the socket, raw."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.seen: list[dict[str, Any]] = []

    @classmethod
    async def connect(cls, port: int, token: str = TOKEN) -> "Client":
        reader, writer = await asyncio.open_connection(HOST, port)
        client = cls(reader, writer)
        await client.send({"type": "hello", "token": token, "protocol": PROTOCOL})
        return client

    async def send(self, message: dict[str, Any]) -> None:
        self.writer.write(encode(message))
        await self.writer.drain()

    async def next(self, kind: str) -> dict[str, Any]:
        """The next message of this type, keeping the others."""
        while True:
            line = await asyncio.wait_for(self.reader.readline(), WAIT)
            assert line, "the host hung up"
            message = decode(line)
            self.seen.append(message)
            if message["type"] == kind:
                return message

    async def close(self) -> None:
        self.writer.close()


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def hosted(body, **options):
    """Run `body(host, port, spawner)` against a host on loopback."""

    async def go() -> None:
        spawner = Spawner()
        host = SessionHost(TOKEN, "build1", spawner.spawn, spawner.kill, Lifetimes(**options))
        port = await host.serve()
        try:
            await body(host, port, spawner)
        finally:
            spawner.end_all()
            host.finished.set()

    asyncio.run(go())


def spawn_message(key: str = "k1", tab_id: str = "t1", req: int = 1) -> dict[str, Any]:
    return {
        "type": "spawn",
        "req": req,
        "key": key,
        "tab_id": tab_id,
        "argv": ["pwsh"],
        "cwd": "C:\\work",
        "env": {"A": "1"},
        "rows": 24,
        "cols": 80,
    }


def test_the_welcome_names_the_build_and_holds_nothing_yet():
    async def body(_host, port, _spawner):
        client = await Client.connect(port)
        welcome = await client.next("welcome")
        assert welcome["build"] == "build1"
        assert welcome["protocol"] == PROTOCOL
        assert welcome["sessions"] == []
        await client.close()

    hosted(body)


def test_a_wrong_token_is_hung_up_on():
    async def body(_host, port, _spawner):
        client = await Client.connect(port, "wrong")
        assert await asyncio.wait_for(client.reader.readline(), WAIT) == b""

    hosted(body)


def test_spawn_then_output_input_and_resize():
    async def body(_host, port, spawner):
        client = await Client.connect(port)
        await client.next("welcome")
        await client.send(spawn_message())
        assert (await client.next("reply"))["req"] == 1
        process = spawner.processes[0]
        assert (process.argv, process.cwd, process.size) == (["pwsh"], "C:\\work", (24, 80))
        process.reads.put("hello")
        output = await client.next("output")
        assert (output["key"], output["data"], output["end"]) == ("k1", "hello", 5)
        await client.send({"type": "input", "key": "k1", "data": "ls\r"})
        await client.send({"type": "resize", "key": "k1", "rows": 30, "cols": 90})
        for _ in range(100):
            if process.sizes:
                break
            await asyncio.sleep(0.01)
        assert process.written == ["ls\r"]
        assert process.sizes == [(30, 90)]

    hosted(body)


def test_the_host_answers_the_status_query_with_no_app_connected():
    async def body(_host, port, spawner):
        client = await Client.connect(port)
        await client.send(spawn_message())
        await client.next("reply")
        await client.close()
        spawner.processes[0].reads.put("\x1b]7501;?\x07")
        for _ in range(100):
            if spawner.processes[0].written:
                break
            await asyncio.sleep(0.01)
        assert spawner.processes[0].written == ["\x1b]7501;?\x1b\\"]

    hosted(body)


def test_a_spawn_failure_is_the_reply():
    async def body(host, port, spawner):
        spawner.refuse = SpawnError("no such shell")
        client = await Client.connect(port)
        await client.send(spawn_message())
        assert (await client.next("reply"))["error"] == "no such shell"
        assert host.sessions == {}

    hosted(body)


def test_a_later_app_finds_the_process_and_what_was_remembered():
    async def body(_host, port, spawner):
        first = await Client.connect(port)
        await first.send(spawn_message())
        await first.next("reply")
        spawner.processes[0].reads.put("before")
        await first.next("output")
        await first.send({"type": "remember", "key": "k1", "state": {"s": 1}, "offset": 6})
        await first.close()
        spawner.processes[0].reads.put(" after")
        await asyncio.sleep(0.05)

        second = await Client.connect(port)
        welcome = await second.next("welcome")
        assert welcome["sessions"] == [
            {"key": "k1", "tab_id": "t1", "alive": True, "exit_code": None}
        ]
        await second.send({"type": "replay", "req": 2, "key": "k1"})
        replay = await second.next("reply")
        assert replay["screen"] == "before after"
        assert (replay["start"], replay["end"], replay["remembered"]) == (0, 12, 6)
        assert replay["state"] == {"s": 1}

    hosted(body)


def test_replay_of_an_unknown_process_is_an_error():
    async def body(_host, port, _spawner):
        client = await Client.connect(port)
        await client.send({"type": "replay", "req": 3, "key": "nope"})
        assert (await client.next("reply"))["error"] == "no such session"

    hosted(body)


def test_a_new_app_replaces_the_old_connection():
    async def body(_host, port, _spawner):
        first = await Client.connect(port)
        await first.next("welcome")
        second = await Client.connect(port)
        await second.next("welcome")
        assert await asyncio.wait_for(first.reader.readline(), WAIT) == b""

    hosted(body)


def test_an_exit_is_sent_and_kept():
    async def body(host, port, spawner):
        client = await Client.connect(port)
        await client.send(spawn_message())
        await client.next("reply")
        spawner.processes[0].finish(3)
        assert (await client.next("exit"))["code"] == 3
        assert host.sessions["k1"].summary()["alive"] is False
        assert host.sessions["k1"].exit_code == 3

    hosted(body)


def test_close_ends_and_forgets_the_process():
    async def body(host, port, spawner):
        client = await Client.connect(port)
        await client.send(spawn_message())
        await client.next("reply")
        await client.send({"type": "close", "req": 5, "key": "k1"})
        assert (await client.next("reply"))["req"] == 5
        assert spawner.killed == [1000]
        assert host.sessions == {}

    hosted(body)


def test_shutdown_ends_everything_then_the_host():
    async def body(host, port, spawner):
        client = await Client.connect(port)
        await client.send(spawn_message("a", "ta", 1))
        await client.next("reply")
        await client.send(spawn_message("b", "tb", 2))
        await client.next("reply")
        await client.send({"type": "shutdown", "req": 9})
        assert (await client.next("reply"))["req"] == 9
        assert sorted(spawner.killed) == [1000, 1001]
        await asyncio.wait_for(host.finished.wait(), WAIT)

    hosted(body)


def test_unknown_and_malformed_messages_are_dropped():
    async def body(host, port, _spawner):
        client = await Client.connect(port)
        await client.next("welcome")
        client.writer.write(b"not json\n")
        await client.send({"type": "teleport"})
        await client.send({"type": "input", "key": "missing", "data": "x"})
        await client.send({"type": "replay", "req": 1, "key": "nope"})
        assert (await client.next("reply"))["req"] == 1

    hosted(body)


def test_with_nothing_running_and_no_app_the_host_ends():
    clock = Clock()

    async def body(host, _port, _spawner):
        clock.now = 10
        await host.check_idle()
        assert not host.finished.is_set()
        clock.now = 31
        await host.check_idle()
        assert host.finished.is_set()

    hosted(body, linger_seconds=30, clock=clock)


def test_running_processes_wait_for_the_app_then_end():
    clock = Clock()

    async def body(host, port, spawner):
        client = await Client.connect(port)
        await client.send(spawn_message())
        await client.next("reply")
        await client.close()
        await asyncio.sleep(0.05)
        clock.now = 31
        await host.check_idle()
        assert not host.finished.is_set(), "processes outlive the linger"
        clock.now = 101
        await host.check_idle()
        assert host.finished.is_set()
        assert spawner.killed == [1000]

    hosted(body, linger_seconds=30, orphan_seconds=100, clock=clock)


def test_a_connected_app_keeps_the_host():
    clock = Clock()

    async def body(host, port, _spawner):
        client = await Client.connect(port)
        await client.next("welcome")
        clock.now = 1000
        await host.check_idle()
        assert not host.finished.is_set()

    hosted(body, linger_seconds=30, orphan_seconds=100, clock=clock)


def test_run_until_finished_checks_and_stops():
    async def go() -> None:
        host = SessionHost(TOKEN, "b", lifetimes=Lifetimes(linger_seconds=0))
        await host.serve()
        await asyncio.wait_for(host.run_until_finished(check_seconds=0.01), WAIT)
        assert host.finished.is_set()

    asyncio.run(go())


def test_hosted_summary():
    record = Hosted("k", "t", PtySession(lambda _text: None, lambda _code: None), Backlog())
    assert record.summary() == {"key": "k", "tab_id": "t", "alive": True, "exit_code": None}


def test_run_host_publishes_its_address_while_serving(tmp_path):
    address_file = tmp_path / "host.json"

    async def go() -> None:
        serving = asyncio.create_task(run_host(address_file, orphan_seconds=60))
        for _ in range(200):
            if address_file.exists():
                break
            await asyncio.sleep(0.01)
        address = read_address(address_file)
        assert address is not None
        client = await Client.connect(address.port, address.token)
        assert (await client.next("welcome"))["build"] == address.build
        await client.send({"type": "shutdown", "req": 1})
        await client.next("reply")
        await asyncio.wait_for(serving, WAIT)

    asyncio.run(go())
    assert not address_file.exists()


def paths(tmp_path: Path) -> list[str]:
    return [
        "--address-file",
        str(tmp_path / "host.json"),
        "--lock-file",
        str(tmp_path / "host.lock"),
        "--log-file",
        str(tmp_path / "logs" / "host.log"),
    ]


def test_parse_args(tmp_path):
    args = parse_args([*paths(tmp_path), "--orphan-seconds", "5"])
    assert args.address_file == tmp_path / "host.json"
    assert args.orphan_seconds == 5.0
    assert parse_args(paths(tmp_path)).orphan_seconds == module.ORPHAN_SECONDS


def test_main_leaves_the_work_to_a_running_host(tmp_path):
    held = acquire_instance_lock(tmp_path / "host.lock")
    try:
        assert main(paths(tmp_path)) == 0
    finally:
        assert held is not None
        held.close()
    assert not (tmp_path / "host.json").exists()


def test_main_runs_the_host(tmp_path, monkeypatch):
    ran: list[tuple] = []

    async def fake_run(address_file, orphan_seconds):
        ran.append((address_file, orphan_seconds))

    monkeypatch.setattr(module, "run_host", fake_run)
    assert main(paths(tmp_path)) == 0
    assert ran == [(tmp_path / "host.json", module.ORPHAN_SECONDS)]


@pytest.mark.parametrize("payload", [{"type": "hello"}, {"type": "other", "token": TOKEN}])
def test_a_hello_without_the_token_is_refused(payload):
    async def body(_host, port, _spawner):
        reader, writer = await asyncio.open_connection(HOST, port)
        writer.write(json.dumps(payload).encode() + b"\n")
        assert await asyncio.wait_for(reader.readline(), WAIT) == b""

    hosted(body)

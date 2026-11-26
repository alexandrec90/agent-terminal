import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent_terminal.desktop import Desktop
from agent_terminal.runner import TabRunner
from agent_terminal.store import Tab
from agent_terminal.terminal_requests import DEFAULT_SIZE, TerminalRequests


class FakeSession:
    def __init__(self) -> None:
        self.sizes: list[tuple[int, int]] = []

    def resize(self, rows: int, cols: int) -> None:
        self.sizes.append((rows, cols))


class FakeRunner:
    def __init__(self) -> None:
        self.live: dict[str, Any] = {}
        self.typed: list[tuple[str, str]] = []

    def input(self, tab_id: str, text: str) -> None:
        self.typed.append((tab_id, text))

    def add(self, tab_id: str, asleep: bool = False) -> FakeSession:
        session = FakeSession()
        tab = SimpleNamespace(id=tab_id, asleep=asleep)
        self.live[tab_id] = SimpleNamespace(tab=tab, session=session, size=DEFAULT_SIZE)
        return session


def tab_named(tab_id: str) -> Tab:
    if tab_id != "t":
        raise KeyError(tab_id)
    return Tab(id="t", project="p", project_path=".", agent="claude", created_at=0.0)


def rig(emitted: list[dict[str, Any]]) -> tuple[FakeRunner, TerminalRequests]:
    runner = FakeRunner()
    desktop = Desktop(read_clipboard=lambda: "pasted")
    requests = TerminalRequests(
        cast(TabRunner, runner), desktop, asyncio.get_running_loop(), emitted.append, tab_named
    )
    return runner, requests


def test_terminal_requests_answer_input_resize_and_paste():
    async def body() -> None:
        emitted: list[dict[str, Any]] = []
        runner, requests = rig(emitted)
        assert requests.size == DEFAULT_SIZE
        handlers = requests.handlers()
        assert set(handlers) == {"input", "resize", "paste"}
        await handlers["input"]({"id": "t", "data": "x"})
        assert runner.typed == [("t", "x")]
        awake = runner.add("t")
        asleep = runner.add("s", asleep=True)
        await handlers["resize"]({"rows": 40, "cols": 90})
        assert requests.size == (40, 90)
        assert awake.sizes == [(40, 90)]
        assert asleep.sizes == [], "a sleeping tab has no process to resize"
        assert runner.live["s"].size == (40, 90), "it takes the size when it wakes"
        await handlers["paste"]({"id": "t"})
        assert emitted == [{"type": "paste", "id": "t", "text": "pasted"}]

    asyncio.run(body())


def test_take_size_refuses_a_message_with_none():
    async def body() -> None:
        _, requests = rig([])
        with pytest.raises(ValueError):
            requests.take_size({"rows": "many", "cols": 1})
        with pytest.raises(KeyError):
            requests.take_size({"rows": 1})
        assert requests.size == DEFAULT_SIZE

    asyncio.run(body())


def test_a_paste_into_a_closed_tab_is_refused():
    async def body() -> None:
        emitted: list[dict[str, Any]] = []
        _, requests = rig(emitted)
        with pytest.raises(KeyError):
            await requests.handlers()["paste"]({"id": "gone"})
        assert emitted == []

    asyncio.run(body())

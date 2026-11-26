import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from agent_terminal import land_requests as module
from agent_terminal.land_requests import LandRequests
from agent_terminal.pr_board import PrBoard
from agent_terminal.runner import TabRunner
from agent_terminal.store import Tab


class Rig:
    def __init__(self) -> None:
        self.tab = Tab(id="t", project="p", project_path="p", agent="claude", created_at=0.0)
        self.live = SimpleNamespace(status="idle")
        self.typed: list[str] = []
        self.land = "create"
        self.on_input = lambda: None
        # What the board was told, and the agent's status at each word.
        self.board_calls: list[tuple[str, str]] = []
        self.lives = {"t": self.live}
        runner = SimpleNamespace(live=self.lives, input=self.input)
        board = SimpleNamespace(
            tab_fields=lambda _tab: {"land": self.land},
            pr_of_tab=lambda _id: None,
            hold_land=lambda _id: self.board_calls.append(("hold", self.live.status)),
            release_land=self.release_land,
        )
        self.requests = LandRequests(
            cast(PrBoard, board), cast(TabRunner, runner), lambda tab_id: {"t": self.tab}[tab_id]
        )

    async def release_land(self, _tab: Tab) -> None:
        self.board_calls.append(("release", self.live.status))

    def input(self, _tab_id: str, data: str) -> None:
        self.typed.append(data)
        self.on_input()

    async def click(self) -> None:
        await self.requests.handlers()["land"]({"type": "land", "id": "t"})


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(module, "ENTER_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(module, "SETTLE_SECONDS", 0.01)
    monkeypatch.setattr(module, "STATUS_SECONDS", 0.01)
    monkeypatch.setattr(module, "TAKE_UP_SECONDS", 0.5)
    return Rig()


def run_agent(rig: Rig, *statuses: str) -> None:
    """Have the agent go through `statuses`, one a status look apart, once it is entered."""

    async def go() -> None:
        for status in statuses:
            await asyncio.sleep(0.03)
            rig.live.status = status

    tasks: list[asyncio.Task[None]] = []

    def on_input() -> None:
        if rig.typed[-1] == "\r":
            tasks.append(asyncio.get_running_loop().create_task(go()))

    rig.on_input = on_input


def test_the_button_is_held_until_the_agent_is_back_at_its_prompt(rig):
    run_agent(rig, "working", "blocked", "working", "idle")
    asyncio.run(rig.click())
    assert rig.board_calls == [("hold", "idle"), ("release", "idle")]


@pytest.mark.parametrize("status", ["asleep", "exited", "shell"])
def test_the_button_is_released_when_the_agent_stops_otherwise(rig, status):
    run_agent(rig, "working", status)
    asyncio.run(rig.click())
    assert rig.board_calls[-1] == ("release", status)


def test_the_button_is_released_when_the_tab_closes(rig):
    def close() -> None:
        if rig.typed[-1] == "\r":
            rig.lives.clear()

    rig.on_input = close
    asyncio.run(rig.click())
    assert [call for call, _ in rig.board_calls] == ["hold", "release"]


def test_the_button_is_released_if_the_agent_never_takes_it_up(rig):
    asyncio.run(rig.click())
    assert rig.board_calls == [("hold", "idle"), ("release", "idle")]


def test_the_button_is_released_when_typing_fails(rig):
    rig.on_input = lambda: setattr(rig.live, "status", "blocked")
    with pytest.raises(ValueError, match="blocked"):
        asyncio.run(rig.click())
    assert [call for call, _ in rig.board_calls] == ["hold", "release"]


def test_the_skill_is_typed_then_entered(rig):
    asyncio.run(rig.click())
    assert rig.typed == ["/land", "\r"]
    asyncio.run(rig.click())
    assert len(rig.typed) == 4


@pytest.mark.parametrize("status", ["starting", "working", "blocked", "shell", "exited"])
def test_nothing_is_typed_unless_the_agent_is_at_its_prompt(rig, status):
    rig.live.status = status
    with pytest.raises(ValueError, match=status):
        asyncio.run(rig.click())
    assert rig.typed == []


def test_a_question_that_arrives_in_between_is_not_answered(rig):
    rig.on_input = lambda: setattr(rig.live, "status", "blocked")
    with pytest.raises(ValueError, match="blocked"):
        asyncio.run(rig.click())
    assert rig.typed == ["/land"]


def test_nothing_to_land(rig):
    rig.land = ""
    with pytest.raises(ValueError, match="nothing"):
        asyncio.run(rig.click())
    assert rig.typed == []


def test_a_double_click_asks_once(rig):
    async def twice():
        return await asyncio.gather(rig.click(), rig.click(), return_exceptions=True)

    results = asyncio.run(twice())
    assert [type(result) for result in results] == [type(None), ValueError]
    assert rig.typed == ["/land", "\r"]


def test_a_tab_that_has_gone(rig):
    with pytest.raises(KeyError):
        asyncio.run(rig.requests.handlers()["land"]({"type": "land", "id": "gone"}))

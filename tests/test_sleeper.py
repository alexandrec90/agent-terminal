import asyncio
import base64
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_terminal import sleeper as module
from agent_terminal.attention import ASLEEP, BLOCKED, IDLE, SHELL, STARTING, WORKING
from agent_terminal.runner import LiveTab, RunnerEvents, TabRunner
from agent_terminal.sessions import LocalSessions
from agent_terminal.settings import Settings
from agent_terminal.sleeper import Sleeper, SleeperLinks, is_due
from agent_terminal.store import Tab


class FakeSession:
    """A session that records its life. `hold` keeps `close` from returning."""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.hold = threading.Event()
        self.hold.set()
        self.argv: list[str] | None = None

    def start(self, argv: list[str] | None = None, *_: Any) -> None:
        self.argv = argv
        self.log.append("start")

    def close(self) -> None:
        self.hold.wait(5)
        self.log.append("close")

    def write(self, text: str) -> None: ...
    def resize(self, rows: int, cols: int) -> None: ...
    def remember(self, state: dict[str, Any]) -> None: ...


class FakeSessions(LocalSessions):
    def __init__(self, log: list[str]) -> None:
        super().__init__()
        self.log = log

    def new(self, *_: Any) -> FakeSession:
        return FakeSession(self.log)


class Rig:
    def __init__(self, tmp_path: Path, after: float = 900) -> None:
        self.tmp_path = tmp_path
        loop = asyncio.get_running_loop()
        self.log: list[str] = []
        self.resets: list[str] = []
        self.changes = 0
        self.active: str | None = None
        self.free = True
        self.asked: list[str] = []
        settings = Settings(
            data_dir=tmp_path,
            claude_home=tmp_path,
            claude_config_file=tmp_path / "claude.json",
            codex_home=tmp_path,
            sleep_after_seconds=after,
        )
        self.runner = TabRunner(
            settings,
            loop,
            FakeSessions(self.log),
            RunnerEvents(lambda *_: None, self.changed, lambda _: None, self.resets.append),
        )
        links = SleeperLinks(
            lambda: self.active, lambda: (30, 100), self.changed, self.resets.append
        )
        self.sleeper = Sleeper(settings, loop, self.runner, links, self.can_rest)

    def changed(self) -> None:
        self.changes += 1

    def can_rest(self, _home: Path, session_id: str, _since: float) -> bool:
        self.asked.append(session_id)
        return self.free

    def add(self, tab_id: str = "t", idle: float = 1000, **fields: Any) -> LiveTab:
        defaults = {"agent": "claude", "session_id": f"s-{tab_id}", "created_at": 0.0}
        path = str(self.tmp_path)
        live = self.runner.add(
            Tab(id=tab_id, project="p", project_path=path, **{**defaults, **fields})
        )
        live.status = IDLE
        live.used_at = time.monotonic() - idle
        return live


def run(tmp_path: Path, body: Any, **options: Any) -> None:
    async def go() -> None:
        await body(Rig(tmp_path, **options))

    asyncio.run(go())


async def settle(rig: Rig) -> None:
    for _ in range(200):
        if not rig.sleeper._waking:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("still waking")


def test_an_idle_tab_sleeps_and_keeps_its_place(tmp_path):
    async def body(rig: Rig) -> None:
        tab = rig.add().tab
        await rig.sleeper.sweep()
        assert rig.log == ["close"]
        assert tab.asleep
        assert rig.runner.live["t"].status == ASLEEP
        assert rig.runner.live["t"].tab is tab
        assert rig.changes
        assert rig.resets == [], "the screen stays as it was"
        await rig.sleeper.sweep()
        assert rig.log == ["close"]

    run(tmp_path, body)


def test_a_tab_on_screen_stays_awake_and_counts_from_when_it_is_left(tmp_path):
    async def body(rig: Rig) -> None:
        rig.add()
        rig.active = "t"
        await rig.sleeper.sweep()
        rig.active = None
        await rig.sleeper.sweep()
        assert rig.log == []

    run(tmp_path, body)


def test_a_session_that_cannot_rest_stays_awake(tmp_path):
    async def body(rig: Rig) -> None:
        rig.add()
        rig.free = False
        await rig.sleeper.sweep()
        assert rig.asked == ["s-t"]
        assert rig.log == []

    run(tmp_path, body)


def test_a_tab_gone_to_during_the_check_stays_awake(tmp_path):
    async def body(rig: Rig) -> None:
        rig.add()

        def arrive(*_: Any) -> bool:
            rig.active = "t"
            return True

        rig.sleeper._can_rest = arrive
        await rig.sleeper.sweep()
        assert rig.log == []

    run(tmp_path, body)


def test_is_due_only_at_a_quiet_prompt(tmp_path):
    async def body(rig: Rig) -> None:
        now = time.monotonic()
        live = rig.add()
        assert is_due(live, now, 900)
        assert not is_due(live, now, 1100)
        for status in (WORKING, BLOCKED, SHELL, STARTING, ASLEEP):
            live.status = status
            assert not is_due(live, now, 900)
        live.status = IDLE
        live.drafting = True
        assert not is_due(live, now, 900)
        live.drafting = False
        live.awaiting_trust = True
        assert not is_due(live, now, 900)
        assert not is_due(rig.add("c", agent="codex"), now, 900)
        assert not is_due(rig.add("n", session_id=None), now, 900)

    run(tmp_path, body)


def test_going_to_a_sleeping_tab_resumes_it_once(tmp_path):
    async def body(rig: Rig) -> None:
        tab = rig.add().tab
        await rig.sleeper.sweep()
        rig.active = "t"
        rig.sleeper.wake_active()
        rig.sleeper.wake_active()
        assert not tab.asleep
        assert rig.runner.live["t"].status == STARTING
        await settle(rig)
        assert rig.log == ["close", "start"]
        assert rig.resets == ["t"]
        assert rig.runner.live["t"].size == (30, 100)

    run(tmp_path, body)


def test_a_tab_off_screen_can_be_woken_with_a_message_for_its_session(tmp_path):
    async def body(rig: Rig) -> None:
        transcript = tmp_path / "projects" / "C--p" / "s-t.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n", encoding="utf-8")
        tab = rig.add().tab
        await rig.sleeper.sweep()
        live = rig.runner.live["t"]
        rig.sleeper.wake(live, "continue")
        await settle(rig)
        assert not tab.asleep
        assert rig.log == ["close", "start"]
        session = live.session
        assert isinstance(session, FakeSession)
        argv = session.argv
        assert isinstance(argv, list)
        command = base64.b64decode(argv[-1]).decode("utf-16-le")
        assert "'--resume' 's-t' 'continue'" in command

    run(tmp_path, body)


def test_waking_a_tab_awake_is_nothing(tmp_path):
    async def body(rig: Rig) -> None:
        live = rig.add()
        rig.sleeper.wake(live, "continue")
        assert not rig.sleeper._waking
        assert rig.log == []

    run(tmp_path, body)


def test_waking_waits_for_the_old_process_to_end(tmp_path):
    async def body(rig: Rig) -> None:
        live = rig.add()
        session = live.session
        assert isinstance(session, FakeSession)
        session.hold.clear()
        sweep = asyncio.create_task(rig.sleeper.sweep())
        for _ in range(200):
            if live.tab.asleep:
                break
            await asyncio.sleep(0.01)
        assert live.tab.asleep
        rig.active = "t"
        rig.sleeper.wake_active()
        await asyncio.sleep(0.05)
        assert rig.log == []
        session.hold.set()
        await sweep
        await settle(rig)
        assert rig.log == ["close", "start"]

    run(tmp_path, body)


def test_a_tab_closed_while_waking_does_not_start(tmp_path):
    async def body(rig: Rig) -> None:
        rig.add()
        await rig.sleeper.sweep()
        rig.active = "t"
        rig.sleeper.wake_active()
        await rig.runner.close("t")
        await settle(rig)
        assert "start" not in rig.log

    run(tmp_path, body)


def test_waking_nothing_is_nothing(tmp_path):
    async def body(rig: Rig) -> None:
        rig.sleeper.wake_active()
        rig.add()
        rig.active = "t"
        rig.sleeper.wake_active()
        assert not rig.sleeper._waking

    run(tmp_path, body)


def test_it_checks_by_itself_until_stopped_and_outlives_a_bad_check(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "CHECK_SECONDS", 0.01)

    async def body(rig: Rig) -> None:
        checks = 0

        async def sweep() -> None:
            nonlocal checks
            checks += 1
            raise RuntimeError("bad check")

        monkeypatch.setattr(rig.sleeper, "sweep", sweep)
        rig.sleeper.start()
        for _ in range(200):
            if checks > 1:
                break
            await asyncio.sleep(0.01)
        assert checks > 1
        rig.sleeper.stop()
        stopped_at = checks
        await asyncio.sleep(0.05)
        assert checks == stopped_at

    run(tmp_path, body)


def test_a_bug_in_a_check_is_raised_not_hidden(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "CHECK_SECONDS", 0.01)

    async def body(rig: Rig) -> None:
        async def sweep() -> None:
            raise AttributeError("a bug")

        monkeypatch.setattr(rig.sleeper, "sweep", sweep)
        with pytest.raises(AttributeError, match="a bug"):
            await rig.sleeper._run()

    run(tmp_path, body)


@pytest.mark.parametrize("after", [0, -1])
def test_it_can_be_turned_off(tmp_path, after):
    async def body(rig: Rig) -> None:
        rig.sleeper.start()
        assert rig.sleeper._task is None
        rig.sleeper.stop()

    run(tmp_path, body, after=after)

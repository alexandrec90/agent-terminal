import asyncio
from pathlib import Path
from typing import Any

from agent_terminal.attention import EXITED, QUERY_REPLY, WORKING
from agent_terminal.pty_session import PtyProcess, SpawnError
from agent_terminal.runner import LiveTab, RunnerEvents, TabRunner
from agent_terminal.settings import Settings
from agent_terminal.store import Tab


class Process:
    pid: int | None = 1
    exitstatus: int | None = 0

    def __init__(self) -> None:
        self.written: list[str] = []

    def read(self, _size: int) -> str:
        raise EOFError

    def write(self, text: str) -> None:
        self.written.append(text)

    def setwinsize(self, rows: int, cols: int) -> None: ...

    def isalive(self) -> bool:
        return False


def make_tab(tmp_path: Path, agent: str = "claude") -> Tab:
    return Tab(id="t", project="demo", project_path=str(tmp_path), agent=agent, created_at=0.0)


class Rig:
    def __init__(self, tmp_path: Path, loop: asyncio.AbstractEventLoop) -> None:
        self.outputs: list[tuple[str, str]] = []
        self.changes: list[bool] = []
        self.clean: list[str] = []
        self.resets: list[str] = []
        self.process = Process()
        self.runner = TabRunner(
            Settings(
                data_dir=tmp_path,
                claude_home=tmp_path,
                claude_config_file=tmp_path / "claude.json",
                codex_home=tmp_path,
            ),
            loop,
            self.spawn,
            RunnerEvents(
                lambda tab_id, text: self.outputs.append((tab_id, text)),
                lambda: self.changes.append(True),
                self.clean.append,
                self.resets.append,
            ),
        )

    def spawn(self, *_: Any) -> PtyProcess:
        return self.process


def run(tmp_path: Path, body) -> None:
    async def go() -> None:
        await body(Rig(tmp_path, asyncio.get_running_loop()))

    asyncio.run(go())


def test_add_shows_a_tab_before_its_process_exists(tmp_path):
    async def body(rig: Rig) -> None:
        tab = make_tab(tmp_path)
        live = rig.runner.add(tab)
        assert isinstance(live, LiveTab)
        assert rig.runner.live[tab.id].status == "starting"

    run(tmp_path, body)


def test_receive_answers_the_query_on_the_reader_thread(tmp_path):
    async def body(rig: Rig) -> None:
        live = rig.runner.add(make_tab(tmp_path))
        await rig.runner.start(live.tab, (24, 80))
        await asyncio.sleep(0)
        await asyncio.to_thread(rig.runner.receive, live, "\x1b]7501;?\x1b\\")
        assert rig.process.written == [QUERY_REPLY]

    run(tmp_path, body)


def test_a_status_change_is_announced_once(tmp_path):
    async def body(rig: Rig) -> None:
        live = rig.runner.add(make_tab(tmp_path))
        rig.runner.receive(live, "\x1b]7501;state=working:app=x\x1b\\")
        rig.runner.receive(live, "\x1b]7501;state=working:app=x:msg=YQ==\x1b\\")
        await asyncio.sleep(0.01)
        assert live.status == WORKING
        assert rig.changes == [True]

    run(tmp_path, body)


def test_output_from_a_closed_tab_changes_nothing(tmp_path):
    async def body(rig: Rig) -> None:
        live = rig.runner.add(make_tab(tmp_path))
        await rig.runner.close("t")
        rig.runner.receive(live, "\x1b]7501;state=working\x1b\\")
        await asyncio.sleep(0.01)
        assert rig.changes == []

    run(tmp_path, body)


def test_a_spawn_failure_reaches_the_tab(tmp_path):
    def refuse(*_: Any) -> PtyProcess:
        raise SpawnError("no such shell")

    async def body(rig: Rig) -> None:
        rig.runner._spawn = refuse
        tab = make_tab(tmp_path)
        live = rig.runner.add(tab)
        await rig.runner.start(tab, (24, 80))
        assert live.status == EXITED
        assert "no such shell" in rig.outputs[0][1]

    run(tmp_path, body)


def test_a_clean_exit_is_handed_up_and_a_failure_stays(tmp_path):
    async def body(rig: Rig) -> None:
        live = rig.runner.add(make_tab(tmp_path))
        rig.runner._exited(live, 0)
        assert rig.clean == ["t"]
        rig.runner._exited(live, 1)
        assert live.status == EXITED

    run(tmp_path, body)


def test_an_exit_from_a_process_since_replaced_is_ignored(tmp_path):
    async def body(rig: Rig) -> None:
        tab = make_tab(tmp_path)
        replaced = rig.runner.add(tab)
        rig.runner.add(tab)
        rig.runner._exited(replaced, 0)
        assert rig.clean == []

    run(tmp_path, body)


def test_output_from_a_process_since_replaced_is_dropped(tmp_path):
    async def body(rig: Rig) -> None:
        tab = make_tab(tmp_path)
        replaced = rig.runner.add(tab)
        rig.runner.add(tab)
        rig.runner.receive(replaced, "old screen")
        await asyncio.sleep(0.01)
        assert rig.outputs == []

    run(tmp_path, body)


def test_closing_during_the_swap_starts_nothing(tmp_path):
    async def body(rig: Rig) -> None:
        tab = make_tab(tmp_path)
        tab.worktree_name = "tab-1"
        live = rig.runner.add(tab)
        reopening = asyncio.create_task(rig.runner._reopen(live))
        await asyncio.sleep(0)
        await rig.runner.close("t")
        await reopening
        assert rig.resets == []

    run(tmp_path, body)


def test_starting_a_closed_tab_does_nothing(tmp_path):
    async def body(rig: Rig) -> None:
        await rig.runner.start(make_tab(tmp_path), (24, 80))
        assert rig.outputs == []

    run(tmp_path, body)


def test_close_all_empties_the_table(tmp_path):
    async def body(rig: Rig) -> None:
        rig.runner.add(make_tab(tmp_path))
        await rig.runner.close_all()
        assert rig.runner.live == {}

    run(tmp_path, body)


def test_find_unfound_sessions_skips_tabs_that_know_theirs(tmp_path):
    async def body(rig: Rig) -> None:
        tab = make_tab(tmp_path, agent="codex")
        tab.session_id = "known"
        rig.runner.add(tab)
        await rig.runner.find_unfound_sessions()
        assert tab.session_id == "known"

    run(tmp_path, body)

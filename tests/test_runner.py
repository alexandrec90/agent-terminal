import asyncio
import json
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


def test_a_change_the_user_set_going_stamps_the_tab(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_terminal.runner.time.time", lambda: 1234.0)

    async def body(rig: Rig) -> None:
        live = rig.runner.add(make_tab(tmp_path))
        rig.runner.input("t", "fix it\r")
        rig.runner.receive(live, "\x1b]7501;state=working:app=x\x1b\\")
        await asyncio.sleep(0.01)
        assert live.tab.updated_at == 1234.0

    run(tmp_path, body)


def test_a_change_before_the_user_types_leaves_the_stamp(tmp_path):
    async def body(rig: Rig) -> None:
        live = rig.runner.add(make_tab(tmp_path))
        # A restored session settling, or Codex's `working` on every start.
        rig.runner.receive(live, "\x1b]7501;state=working:app=x\x1b\\")
        rig.runner.receive(live, "\x1b]7501;state=idle:app=x\x1b\\")
        await asyncio.sleep(0.01)
        assert live.tab.updated_at is None

    run(tmp_path, body)


def test_a_repeated_state_leaves_the_stamp(tmp_path, monkeypatch):
    async def body(rig: Rig) -> None:
        live = rig.runner.add(make_tab(tmp_path))
        rig.runner.input("t", "x")
        rig.runner.receive(live, "\x1b]7501;state=working:app=x\x1b\\")
        await asyncio.sleep(0.01)
        monkeypatch.setattr("agent_terminal.runner.time.time", lambda: 9999.0)
        rig.runner.receive(live, "\x1b]7501;state=working:app=x\x1b\\")
        await asyncio.sleep(0.01)
        assert live.tab.updated_at != 9999.0

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
        tab.worktree = True
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


def write_transcript(tmp_path: Path, session_id: str, tree: str) -> None:
    """Claude's transcript as `claude -w` begins it; the rig's Claude home is `tmp_path`."""
    directory = tmp_path / "projects" / "C--encoded"
    directory.mkdir(parents=True, exist_ok=True)
    record = {"type": "worktree-state", "worktreeSession": {"worktreePath": tree}}
    (directory / f"{session_id}.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")


def claude_worktree_tab(tmp_path: Path) -> Tab:
    tab = make_tab(tmp_path)
    tab.worktree = True
    tab.session_id = "s-1"
    return tab


def test_the_worktree_claude_named_is_recorded_once_it_says_so(tmp_path):
    async def body(rig: Rig) -> None:
        live = rig.runner.add(claude_worktree_tab(tmp_path))
        write_transcript(tmp_path, "s-1", "C:\\repo\\.claude\\worktrees\\calm-otter")
        rig.runner.receive(live, "\x1b]7501;state=working\x1b\\")
        await settle(lambda: live.tab.cwd is not None)
        assert live.tab.cwd == "C:\\repo\\.claude\\worktrees\\calm-otter"
        assert len(rig.changes) == 2  # the status, then the folder

    run(tmp_path, body)


def test_a_tree_not_yet_written_is_looked_for_again(tmp_path):
    async def body(rig: Rig) -> None:
        live = rig.runner.add(claude_worktree_tab(tmp_path))
        rig.runner.receive(live, "\x1b]7501;state=working\x1b\\")
        await settle(lambda: not rig.runner._finding_trees)
        assert live.tab.cwd is None
        write_transcript(tmp_path, "s-1", "C:\\tree")
        rig.runner.receive(live, "\x1b]7501;state=idle\x1b\\")
        await settle(lambda: live.tab.cwd is not None)
        assert live.tab.cwd == "C:\\tree"

    run(tmp_path, body)


def test_a_tab_with_a_known_folder_or_no_worktree_is_not_searched(tmp_path):
    async def body(rig: Rig) -> None:
        known = claude_worktree_tab(tmp_path)
        known.cwd = "C:\\known"
        plain = make_tab(tmp_path)
        plain.id, plain.session_id = "p", "s-2"
        write_transcript(tmp_path, "s-1", "C:\\other")
        write_transcript(tmp_path, "s-2", "C:\\other")
        for tab in (known, plain):
            rig.runner.receive(rig.runner.add(tab), "\x1b]7501;state=working\x1b\\")
        await asyncio.sleep(0.05)
        assert (known.cwd, plain.cwd) == ("C:\\known", None)

    run(tmp_path, body)


def test_one_search_at_a_time_per_tab(tmp_path):
    async def body(rig: Rig) -> None:
        tab = claude_worktree_tab(tmp_path)
        rig.runner._finding_trees.add(tab.id)
        rig.runner._look_for_claude_tree(tab)
        await asyncio.sleep(0.01)
        assert rig.runner._finding_trees == {tab.id}  # the running search still owns it

    run(tmp_path, body)


def test_a_tab_without_a_session_is_not_searched(tmp_path):
    async def body(rig: Rig) -> None:
        tab = make_tab(tmp_path)
        tab.worktree = True
        rig.runner._look_for_claude_tree(tab)
        assert rig.runner._finding_trees == set()

    run(tmp_path, body)


async def settle(condition) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)


def test_find_unfound_sessions_skips_tabs_that_know_theirs(tmp_path):
    async def body(rig: Rig) -> None:
        tab = make_tab(tmp_path, agent="codex")
        tab.session_id = "known"
        rig.runner.add(tab)
        await rig.runner.find_unfound_sessions()
        assert tab.session_id == "known"

    run(tmp_path, body)

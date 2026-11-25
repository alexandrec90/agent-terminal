import asyncio
import base64
import inspect
import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_terminal import close_guard
from agent_terminal import manager as module
from agent_terminal.attention import FINISHED, NEEDS_INPUT, NO_ATTENTION
from agent_terminal.desktop import Desktop, is_web_url
from agent_terminal.manager import TabManager, switch_view
from agent_terminal.prompts import claude_transcript_dir
from agent_terminal.pty_session import SpawnError
from agent_terminal.sessions import LocalSessions, Survivor
from agent_terminal.settings import Settings
from agent_terminal.store import SAVE_DELAY_SECONDS, Snapshot, Tab, load_snapshot, save_snapshot

ST = "\x1b\\"


def status(state: str) -> str:
    return f"\x1b]7501;state={state}:app=test{ST}"


def make_commit(folder: Path) -> None:
    for args in (
        ("init",),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "T"),
        ("commit", "--allow-empty", "--message", "first"),
    ):
        subprocess.run(["git", *args], cwd=folder, check=True, capture_output=True)  # noqa: S607 - fixed test commands


class FakeProcess:
    def __init__(self, argv, cwd) -> None:
        self.argv = argv
        self.cwd = cwd
        self.pid = 100
        self.exitstatus: int | None = None
        self.written: list[str] = []
        self.sizes: list[tuple[int, int]] = []
        self._ended = threading.Event()

    def read(self, _size: int) -> str:
        self._ended.wait()
        raise EOFError

    def write(self, text: str) -> None:
        self.written.append(text)

    def setwinsize(self, rows: int, cols: int) -> None:
        self.sizes.append((rows, cols))

    def isalive(self) -> bool:
        return not self._ended.is_set()

    def end(self, code: int) -> None:
        self.exitstatus = code
        self._ended.set()

    def command(self) -> str:
        return base64.b64decode(self.argv[-1]).decode("utf-16-le")


class Rig:
    """A manager with fake processes, and everything it emitted."""

    def __init__(self, tmp_path: Path, loop: asyncio.AbstractEventLoop) -> None:
        self.settings = Settings(
            projects_root=tmp_path / "projects",
            data_dir=tmp_path / "data",
            claude_home=tmp_path / "claude",
            claude_config_file=tmp_path / "claude.json",
            codex_home=tmp_path / "codex",
            # Nothing here may reach the real GitHub: a command that cannot run turns the
            # PR board off, and `test_pr_board.py` has it for itself.
            gh_command=str(tmp_path / "no-gh"),
        )
        entries = {tmp_path.as_posix(): {"hasTrustDialogAccepted": True}}
        self.settings.claude_config_file.write_text(json.dumps({"projects": entries}))
        self.settings.codex_home.mkdir()
        (self.settings.codex_home / "config.toml").write_text(
            f"[projects.'{self.settings.projects_root / 'repo'}']\ntrust_level = 'trusted'\n"
        )
        self.repo = self.settings.projects_root / "repo"
        self.plain = self.settings.projects_root / "plain"
        self.repo.mkdir(parents=True)
        self.plain.mkdir()
        self.loop = loop
        self.messages: list[dict[str, Any]] = []
        self.processes: list[FakeProcess] = []
        self.sizes: list[tuple[int, int]] = []
        self.opened: list[str] = []
        self.spawn_error: Exception | None = None
        self.clipboard = ""
        self.picked: str | None = None
        self.killed: list[int] = []
        self.recycled: list[Path] = []
        self.manager = TabManager(
            self.settings,
            self.messages.append,
            self.loop,
            sessions=LocalSessions(self.spawn, self.killed.append),
            desktop=Desktop(
                self.opened.append,
                lambda: self.clipboard,
                self.set_clipboard,
                lambda: self.picked,
                self.recycled.append,
            ),
        )

    def set_clipboard(self, text: str) -> None:
        self.clipboard = text

    def spawn(self, argv, cwd, _env, dimensions):
        if self.spawn_error is not None:
            raise self.spawn_error
        process = FakeProcess(argv, cwd)
        self.processes.append(process)
        self.sizes.append(dimensions)
        return process

    def state(self) -> dict[str, Any]:
        return next(m for m in reversed(self.messages) if m["type"] == "state")

    def tabs(self) -> list[dict[str, Any]]:
        return self.state()["tabs"]

    async def hello(self) -> None:
        await self.manager.handle({"type": "hello", "rows": 30, "cols": 100})

    async def create(self, path: Path | None = None, agent: str = "claude", **extra) -> dict:
        folder = str(path or self.repo)
        await self.manager.handle({"type": "create", "path": folder, "agent": agent, **extra})
        return self.tabs()[-1]

    def make_repository(self) -> None:
        """Turn `repo` into a real one, as only that can have a worktree cut from it."""
        make_commit(self.repo)

    def make_empty_repository(self) -> None:
        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True)  # noqa: S607 - a fixed test command

    async def output(self, tab_id: str, text: str) -> None:
        runner = self.manager.runner
        await asyncio.to_thread(runner.receive, runner.live[tab_id], text)
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def no_restore_gap(monkeypatch):
    monkeypatch.setattr(module, "RESTORE_GAP_SECONDS", 0)


def run(coroutine_function):
    """Run an async test against a `Rig` on the test's own event loop."""

    names = inspect.signature(coroutine_function).parameters

    def wrapper(tmp_path, monkeypatch):
        async def body():
            instance = Rig(tmp_path, asyncio.get_running_loop())
            extra = {"monkeypatch": monkeypatch} if "monkeypatch" in names else {}
            try:
                await coroutine_function(instance, **extra)
            finally:
                for process in instance.processes:
                    process.end(0)

        asyncio.run(body())

    wrapper.__name__ = coroutine_function.__name__
    return wrapper


@run
async def test_hello_sends_projects_and_an_empty_state(rig):
    await rig.hello()
    projects = next(m for m in rig.messages if m["type"] == "projects")["projects"]
    assert [p["name"] for p in projects] == ["plain", "repo"]
    assert rig.state()["tabs"] == []


@run
async def test_create_starts_powershell_in_the_project(rig):
    await rig.hello()
    tab = await rig.create()
    assert rig.processes[0].argv[0] == "pwsh"
    assert rig.processes[0].cwd == tab["project_path"]
    assert rig.processes[0].command().startswith("& 'claude' '--session-id'")
    assert tab["status"] == "starting"
    assert rig.state()["active_id"] == tab["id"]


@run
async def test_create_uses_the_size_the_page_reported(rig):
    await rig.hello()
    await rig.create()
    assert rig.sizes == [(30, 100)]


@run
async def test_a_claude_worktree_tab_lets_claude_name_it(rig):
    rig.make_repository()
    await rig.hello()
    tab = await rig.create(worktree=True)
    assert tab["worktree"] is True
    assert "worktree_name" not in tab
    expected = f"& 'claude' '--session-id' '{tab['session_id']}' '--worktree';"
    assert rig.processes[0].command().startswith(expected)


@run
async def test_a_codex_worktree_tab_asks_for_one(rig):
    rig.make_repository()
    await rig.hello()
    await rig.create(agent="codex", worktree=True)
    assert rig.processes[0].command().startswith("& 'codex' '--worktree'")


@run
async def test_a_worktree_is_refused_outside_git(rig):
    await rig.hello()
    tab = await rig.create(rig.plain, worktree=True)
    assert tab["worktree"] is False
    assert "--worktree" not in rig.processes[0].command()


@run
async def test_a_worktree_is_refused_in_a_repository_with_no_commits(rig):
    rig.make_empty_repository()
    await rig.hello()
    tab = await rig.create(worktree=True)
    assert tab["worktree"] is False


@run
async def test_a_tab_can_be_scoped_to_any_folder(rig):
    elsewhere = rig.settings.projects_root.parent / "outside" / "somewhere"
    elsewhere.mkdir(parents=True)
    await rig.hello()
    tab = await rig.create(elsewhere)
    assert tab["project"] == "somewhere"
    assert tab["project_path"] == str(elsewhere.resolve())
    assert rig.processes[0].cwd == str(elsewhere.resolve())


@run
async def test_two_folders_of_one_name_stay_two(rig):
    first = rig.settings.projects_root.parent / "a" / "demo"
    second = rig.settings.projects_root.parent / "b" / "demo"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    await rig.hello()
    one = await rig.create(first)
    two = await rig.create(second)
    assert (one["project"], two["project"]) == ("demo", "demo")
    assert one["project_path"] != two["project_path"]


@run
async def test_a_worktree_from_a_subfolder_belongs_to_the_repository(rig):
    rig.make_repository()
    below = rig.repo / "sub"
    below.mkdir()
    await rig.hello()
    tab = await rig.create(below, worktree=True)
    assert Path(tab["project_path"]) == rig.repo.resolve()
    assert tab["project"] == "repo"


@run
async def test_a_plain_tab_in_a_subfolder_stays_in_it(rig):
    rig.make_repository()
    below = rig.repo / "sub"
    below.mkdir()
    await rig.hello()
    tab = await rig.create(below)
    assert tab["project_path"] == str(below.resolve())


@pytest.mark.parametrize(
    "message",
    [
        {"type": "create", "path": "C:\\", "agent": "bash"},
        {"type": "create", "path": "relative", "agent": "claude"},
        {"type": "create", "path": "", "agent": "claude"},
        {"type": "create"},
        {"type": "inspect_folder"},
        {"type": "prepare_worktree", "path": "relative"},
        {"type": "pr_view", "repo": "C:\\nowhere", "number": 1},
        {"type": "pr_merge", "repo": "C:\\nowhere", "number": 1},
        {"type": "pr_close", "repo": "C:\\nowhere", "number": 1},
        {"type": "pr_resume", "repo": "C:\\nowhere", "number": 1},
        {"type": "pr_merge"},
        {"type": "close", "id": "absent"},
        {"type": "activate", "id": "absent"},
        {"type": "toggle_park", "id": "absent"},
        {"type": "input", "id": "absent", "data": "x"},
        {"type": "resize", "rows": "many", "cols": 1},
        {"type": "open_url", "url": "file:///etc/passwd"},
        {"type": "nonsense"},
        {},
    ],
)
def test_a_bad_message_is_dropped_without_raising(tmp_path, message):
    async def body():
        rig = Rig(tmp_path, asyncio.get_running_loop())
        await rig.hello()
        await rig.manager.handle(message)
        assert rig.processes == []
        assert rig.opened == []

    asyncio.run(body())


@run
async def test_output_reaches_the_page_unchanged(rig):
    await rig.hello()
    tab = await rig.create()
    text = "hello" + status("idle")
    await rig.output(tab["id"], text)
    delivered = [m for m in rig.messages if m["type"] == "output"]
    assert delivered == [{"type": "output", "id": tab["id"], "data": text}]


@run
async def test_blocked_raises_attention(rig):
    await rig.hello()
    tab = await rig.create()
    await rig.output(tab["id"], status("working") + status("blocked"))
    assert rig.tabs()[0]["status"] == "blocked"
    assert rig.tabs()[0]["attention"] == NEEDS_INPUT


@run
async def test_a_finished_turn_is_flagged_apart_from_a_question(rig):
    await rig.hello()
    tab = await rig.create()
    await rig.output(tab["id"], status("working") + status("done"))
    assert rig.tabs()[0]["attention"] == FINISHED


def test_switch_view_marks_both_ends_seen():
    def tab(tab_id: str, flag: str) -> Tab:
        return Tab(
            id=tab_id,
            project="p",
            project_path="/p",
            agent="claude",
            created_at=0.0,
            attention=flag,
        )

    tabs = [tab("a", FINISHED), tab("b", FINISHED), tab("c", FINISHED), tab("d", NEEDS_INPUT)]
    assert switch_view(tabs, "a", "b") == "b"
    assert [t.attention for t in tabs] == [NO_ATTENTION, NO_ATTENTION, FINISHED, NEEDS_INPUT]
    assert switch_view(tabs, "b", "d") == "d"
    assert tabs[3].attention == NEEDS_INPUT
    assert switch_view(tabs, None, None) is None


@run
async def test_viewing_a_finished_tab_clears_its_flag(rig):
    await rig.hello()
    first = await rig.create()
    await rig.create()
    await rig.output(first["id"], status("working") + status("done"))
    assert rig.tabs()[0]["attention"] == FINISHED
    await rig.manager.handle({"type": "activate", "id": first["id"]})
    assert rig.tabs()[0]["attention"] == NO_ATTENTION


@run
async def test_a_tab_that_finished_while_watched_is_read_once_left(rig):
    await rig.hello()
    first = await rig.create()
    second = await rig.create()
    await rig.output(second["id"], status("working") + status("done"))
    assert rig.tabs()[1]["attention"] == FINISHED, "still flagged while it is on screen"
    await rig.manager.handle({"type": "activate", "id": first["id"]})
    assert rig.tabs()[1]["attention"] == NO_ATTENTION


@run
async def test_viewing_a_question_leaves_it_flagged(rig):
    await rig.hello()
    first = await rig.create()
    await rig.create()
    await rig.output(first["id"], status("working") + status("blocked"))
    await rig.manager.handle({"type": "activate", "id": first["id"]})
    assert rig.tabs()[0]["attention"] == NEEDS_INPUT


@run
async def test_answering_clears_attention(rig):
    await rig.hello()
    tab = await rig.create()
    await rig.output(tab["id"], status("working") + status("blocked"))
    await rig.manager.handle({"type": "input", "id": tab["id"], "data": "1"})
    await rig.output(tab["id"], status("working"))
    assert rig.tabs()[0]["attention"] == NO_ATTENTION


@run
async def test_busy_tabs_are_those_mid_turn(rig):
    await rig.hello()
    working = await rig.create()
    blocked = await rig.create(rig.plain)
    idle = await rig.create()
    await rig.output(working["id"], status("working") + f"\x1b]0;Fixing the build{ST}")
    await rig.output(blocked["id"], status("working") + status("blocked"))
    await rig.output(idle["id"], status("working") + status("idle"))
    busy = close_guard.busy_tabs(rig.manager.runner.live.values())
    assert busy == ["repo: Fixing the build", "plain"]


@run
async def test_no_tab_is_busy_before_its_agent_says_so(rig):
    await rig.hello()
    await rig.create()
    assert close_guard.busy_tabs(rig.manager.runner.live.values()) == []


@run
async def test_right_click_greys_a_tab_and_undoes(rig):
    await rig.hello()
    tab = await rig.create()
    await rig.output(tab["id"], status("working") + status("blocked"))
    await rig.manager.handle({"type": "toggle_park", "id": tab["id"]})
    assert rig.tabs()[0]["parked"] is True
    assert rig.tabs()[0]["attention"] == NEEDS_INPUT
    await rig.manager.handle({"type": "toggle_park", "id": tab["id"]})
    assert rig.tabs()[0]["parked"] is False


@run
async def test_a_new_stop_brings_a_parked_tab_back(rig):
    await rig.hello()
    tab = await rig.create()
    await rig.manager.handle({"type": "toggle_park", "id": tab["id"]})
    await rig.output(tab["id"], status("working") + status("done"))
    assert rig.tabs()[0]["parked"] is False
    assert rig.tabs()[0]["attention"] == FINISHED


@run
async def test_a_title_is_cleaned_and_kept(rig):
    await rig.hello()
    tab = await rig.create()
    await rig.output(tab["id"], f"\x1b]0;\u2733 Fix the bug{ST}")
    assert rig.tabs()[0]["title"] == "Fix the bug"
    await rig.output(tab["id"], f"\x1b]0;Claude Code{ST}")
    assert rig.tabs()[0]["title"] == "Fix the bug"


@run
async def test_input_and_resize_reach_the_process(rig):
    await rig.hello()
    tab = await rig.create()
    await rig.manager.handle({"type": "input", "id": tab["id"], "data": "x"})
    await rig.manager.handle({"type": "resize", "rows": 50, "cols": 200})
    assert rig.processes[0].written == ["x"]
    assert rig.processes[0].sizes == [(50, 200)]


@run
async def test_closing_a_tab_selects_its_neighbour(rig):
    await rig.hello()
    first = await rig.create()
    second = await rig.create()
    third = await rig.create()
    await rig.manager.handle({"type": "activate", "id": second["id"]})
    await rig.manager.handle({"type": "close", "id": second["id"]})
    assert [t["id"] for t in rig.tabs()] == [first["id"], third["id"]]
    assert rig.state()["active_id"] == third["id"]


@run
async def test_closing_the_last_tab_leaves_nothing_active(rig):
    await rig.hello()
    tab = await rig.create()
    await rig.manager.handle({"type": "close", "id": tab["id"]})
    # The group stays, and its place, for the next tab opened in that folder.
    repo = str(rig.repo.resolve())
    assert rig.state() == {
        "type": "state",
        "active_id": None,
        "tabs": [],
        "group_order": [repo],
        "prs": [],
        "kept": [{"path": repo, "name": rig.repo.resolve().name}],
        "folded": [],
        "hidden": [],
    }


@run
async def test_a_group_emptied_tab_by_tab_stays_until_its_own_close(rig):
    await rig.hello()
    first = await rig.create()
    second = await rig.create()
    repo = first["project_path"]
    await rig.manager.handle({"type": "close", "id": first["id"]})
    await rig.manager.handle({"type": "close", "id": second["id"]})
    assert [group["path"] for group in rig.state()["kept"]] == [repo]
    assert rig.state()["hidden"] == []
    await rig.manager.handle({"type": "close_group", "path": repo})
    assert rig.state()["kept"] == []
    assert rig.state()["hidden"] == [repo]


@run
async def test_an_empty_kept_group_survives_a_restart(rig):
    await rig.hello()
    tab = await rig.create()
    await rig.manager.handle({"type": "close", "id": tab["id"]})
    await rig.manager.shutdown()
    assert load_snapshot(rig.settings.state_file).kept == [tab["project_path"]]

    sessions = LocalSessions(rig.spawn, rig.killed.append)
    again = TabManager(rig.settings, rig.messages.append, rig.loop, sessions)
    await again.handle({"type": "hello", "rows": 30, "cols": 100})
    assert [group["path"] for group in rig.state()["kept"]] == [tab["project_path"]]


@run
async def test_closing_a_group_closes_only_its_tabs(rig):
    await rig.hello()
    first = await rig.create(rig.plain)
    await rig.create()
    await rig.create()
    last = await rig.create(rig.plain)
    group = rig.tabs()[1]["project_path"]
    await rig.manager.handle({"type": "close_group", "path": group})
    assert [t["id"] for t in rig.tabs()] == [first["id"], last["id"]]
    assert sorted(rig.killed) == [100, 100]
    assert rig.state()["active_id"] == last["id"]


@run
async def test_closing_the_active_group_hands_over_to_the_next_tab(rig):
    await rig.hello()
    before = await rig.create(rig.plain)
    doomed = await rig.create()
    await rig.create()
    after = await rig.create(rig.plain)
    await rig.manager.handle({"type": "activate", "id": doomed["id"]})
    await rig.manager.handle({"type": "close_group", "path": doomed["project_path"]})
    assert rig.state()["active_id"] == after["id"]
    await rig.manager.handle({"type": "activate", "id": before["id"]})
    assert [t["id"] for t in rig.tabs()] == [before["id"], after["id"]]


@run
async def test_closing_the_last_group_leaves_nothing_active(rig):
    await rig.hello()
    before = await rig.create(rig.plain)
    doomed = await rig.create()
    await rig.manager.handle({"type": "close_group", "path": doomed["project_path"]})
    assert rig.state()["active_id"] == before["id"]
    await rig.manager.handle({"type": "close_group", "path": before["project_path"]})
    assert rig.state()["active_id"] is None


@run
async def test_a_closed_group_stays_hidden_until_a_session_opens_there(rig):
    await rig.hello()
    path = str(rig.repo.resolve())
    await rig.manager.handle({"type": "close_group", "path": path})
    assert rig.state()["hidden"] == [path]
    await rig.create()
    assert rig.state()["hidden"] == []


@run
async def test_a_group_folds_and_unfolds(rig):
    await rig.hello()
    tab = await rig.create()
    path = tab["project_path"]
    await rig.manager.handle({"type": "fold_group", "path": path, "folded": True})
    assert rig.state()["folded"] == [path]
    assert len(rig.tabs()) == 1
    await rig.manager.handle({"type": "fold_group", "path": path, "folded": False})
    assert rig.state()["folded"] == []


@run
async def test_a_new_session_unfolds_its_group(rig):
    await rig.hello()
    tab = await rig.create()
    await rig.manager.handle({"type": "fold_group", "path": tab["project_path"], "folded": True})
    await rig.create()
    assert rig.state()["folded"] == []


@run
async def test_folded_and_hidden_groups_survive_a_restart(rig):
    await rig.hello()
    await rig.manager.handle({"type": "fold_group", "path": "C:\\a", "folded": True})
    await rig.manager.handle({"type": "close_group", "path": "C:\\b"})
    await rig.manager.shutdown()
    saved = load_snapshot(rig.settings.state_file)
    assert (saved.folded, saved.hidden) == (["C:\\a"], ["C:\\b"])

    sessions = LocalSessions(rig.spawn, rig.killed.append)
    again = TabManager(rig.settings, rig.messages.append, rig.loop, sessions)
    await again.handle({"type": "hello", "rows": 30, "cols": 100})
    assert (rig.state()["folded"], rig.state()["hidden"]) == (["C:\\a"], ["C:\\b"])


@pytest.mark.parametrize("kind", ["close_group", "fold_group"])
def test_a_group_message_without_a_path_is_dropped(tmp_path, kind):
    async def body():
        rig = Rig(tmp_path, asyncio.get_running_loop())
        await rig.hello()
        await rig.manager.handle({"type": kind, "path": 7, "folded": True})
        assert (rig.state()["folded"], rig.state()["hidden"]) == ([], [])

    asyncio.run(body())


@run
async def test_a_project_with_a_tab_is_not_deleted(rig):
    await rig.hello()
    await rig.create()
    await rig.manager.handle({"type": "delete_project", "path": str(rig.repo)})
    deleted = next(m for m in rig.messages if m["type"] == "deleted")
    assert "close it first" in deleted["error"]
    assert rig.recycled == []


@run
async def test_a_project_without_one_is(rig):
    await rig.hello()
    await rig.manager.handle({"type": "delete_project", "path": str(rig.plain)})
    assert rig.recycled == [rig.plain.resolve()]


@run
async def test_closing_a_tab_ends_its_process_through_the_given_kill(rig):
    # The real kill runs `taskkill.exe`: absent on the Linux gate, and on Windows it
    # would end whatever real process holds the fake's pid.
    await rig.hello()
    tab = await rig.create()
    await rig.manager.handle({"type": "close", "id": tab["id"]})
    assert rig.killed == [100]


@run
async def test_a_clean_shell_exit_closes_the_tab(rig):
    await rig.hello()
    await rig.create()
    rig.processes[0].end(0)
    for _ in range(100):
        await asyncio.sleep(0.01)
        if not rig.tabs():
            break
    assert rig.tabs() == []


@run
async def test_a_failed_shell_stays_to_be_read(rig):
    await rig.hello()
    await rig.create()
    rig.processes[0].end(1)
    for _ in range(100):
        await asyncio.sleep(0.01)
        if rig.tabs()[0]["status"] == "exited":
            break
    assert rig.tabs()[0]["status"] == "exited"


@run
async def test_a_spawn_failure_is_shown_on_the_tab(rig):
    rig.spawn_error = SpawnError("no pwsh here")
    await rig.hello()
    tab = await rig.create()
    assert rig.tabs()[0]["status"] == "exited"
    output = [m["data"] for m in rig.messages if m["type"] == "output" and m["id"] == tab["id"]]
    assert "no pwsh here" in output[0]


@run
async def test_copy_reaches_the_clipboard(rig):
    await rig.manager.handle({"type": "copy", "text": "selected"})
    assert rig.clipboard == "selected"


@run
async def test_paste_goes_back_through_the_page(rig):
    await rig.hello()
    tab = await rig.create()
    rig.clipboard = "pasted"
    await rig.manager.handle({"type": "paste", "id": tab["id"]})
    assert rig.messages[-1] == {"type": "paste", "id": tab["id"], "text": "pasted"}
    assert rig.processes[0].written == []


@run
async def test_open_url_passes_web_links_only(rig):
    await rig.manager.handle({"type": "open_url", "url": "https://example.com/a"})
    assert rig.opened == ["https://example.com/a"]


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("http://a.b", True),
        ("https://a.b", True),
        ("file:///c:/x", False),
        ("javascript:alert(1)", False),
        ("C:\\x", False),
        (None, False),
        (7, False),
    ],
)
def test_is_web_url(url, allowed):
    assert is_web_url(url) is allowed


def make_saved_tab(project_path: str, **fields) -> Tab:
    defaults = {"id": "saved", "project": "repo", "agent": "claude", "created_at": 1.0}
    return Tab(project_path=project_path, **{**defaults, **fields})


def write_session(rig: Rig, folder: Path, session_id: str) -> None:
    directory = claude_transcript_dir(rig.settings.claude_home, folder)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{session_id}.jsonl").write_text("{}\n")


@run
async def test_a_groups_unopened_sessions_leave_out_its_tabs(rig):
    await rig.hello()
    tab = await rig.create(rig.plain)
    write_session(rig, rig.plain, tab["session_id"])
    write_session(rig, rig.plain, "earlier")
    await rig.manager.handle({"type": "past_sessions", "path": str(rig.plain)})
    sent = next(m for m in reversed(rig.messages) if m["type"] == "past_sessions")
    assert [s["session_id"] for s in sent["sessions"]] == ["earlier"]


@run
async def test_resuming_a_past_session_opens_it_once(rig):
    await rig.hello()
    write_session(rig, rig.plain, "earlier")
    message = {"type": "resume_session", "path": str(rig.plain), "session_id": "earlier"}
    await rig.manager.handle(message)
    assert [t["session_id"] for t in rig.tabs()] == ["earlier"]
    assert rig.tabs()[0]["project_path"] == str(rig.plain)
    assert rig.processes[0].command().startswith("& 'claude' '--resume' 'earlier'")

    # Clicked again from a list drawn before it opened: its tab is shown, not doubled.
    await rig.create(rig.plain)
    await rig.manager.handle(message)
    assert len(rig.tabs()) == 2
    assert rig.state()["active_id"] == rig.tabs()[0]["id"]


@run
async def test_restore_brings_back_every_tab_and_the_active_one(rig):
    path = str(rig.settings.projects_root / "repo")
    tabs = [
        make_saved_tab(path, id="a", session_id="s1", parked=True),
        make_saved_tab(path, id="b", session_id="s2", attention=NEEDS_INPUT),
    ]
    save_snapshot(rig.settings.state_file, Snapshot(tabs=tabs, active_id="b"))
    await rig.hello()
    assert [t["id"] for t in rig.tabs()] == ["a", "b"]
    assert rig.tabs()[0]["parked"] is True
    assert rig.tabs()[1]["attention"] == NEEDS_INPUT
    assert rig.state()["active_id"] == "b"
    assert len(rig.processes) == 2


@run
async def test_a_tab_left_asleep_stays_so_until_the_user_goes_to_it(rig):
    path = str(rig.settings.projects_root / "repo")
    write_session(rig, rig.repo, "s2")
    tabs = [
        make_saved_tab(path, id="a", session_id="s1"),
        make_saved_tab(path, id="b", session_id="s2", asleep=True),
    ]
    save_snapshot(rig.settings.state_file, Snapshot(tabs=tabs, active_id="a"))
    await rig.hello()
    assert [t["status"] for t in rig.tabs()] == ["starting", "asleep"]
    assert len(rig.processes) == 1
    await rig.manager.handle({"type": "resize", "rows": 40, "cols": 90})

    await rig.manager.handle({"type": "activate", "id": "b"})
    assert rig.tabs()[1]["status"] == "starting"
    for _ in range(200):
        if len(rig.processes) == 2:
            break
        await asyncio.sleep(0.01)
    assert rig.processes[1].command().startswith("& 'claude' '--resume' 's2'")
    assert rig.sizes[1] == (40, 90)
    assert any(m == {"type": "reset", "id": "b"} for m in rig.messages)
    await rig.manager.shutdown()
    assert [t.asleep for t in load_snapshot(rig.settings.state_file).tabs] == [False, False]


@run
async def test_the_tab_on_screen_at_launch_is_never_left_asleep(rig):
    path = str(rig.settings.projects_root / "repo")
    save_snapshot(
        rig.settings.state_file,
        Snapshot(tabs=[make_saved_tab(path, id="a", asleep=True)], active_id="a"),
    )
    await rig.hello()
    assert rig.tabs()[0]["status"] == "starting"
    assert len(rig.processes) == 1


@run
async def test_restore_resumes_a_session_with_a_transcript(rig):
    project = rig.settings.claude_home / "projects" / "encoded"
    project.mkdir(parents=True)
    (project / "s1.jsonl").write_text("{}\n")
    path = str(rig.settings.projects_root / "repo")
    save_snapshot(
        rig.settings.state_file,
        Snapshot(tabs=[make_saved_tab(path, session_id="s1")]),
    )
    await rig.hello()
    assert rig.processes[0].command().startswith("& 'claude' '--resume' 's1'")


@run
async def test_a_restored_tab_stays_flagged_until_the_user_answers(rig):
    path = str(rig.settings.projects_root / "repo")
    tab = make_saved_tab(path, id="a", session_id="s", agent="codex", attention=NEEDS_INPUT)
    save_snapshot(rig.settings.state_file, Snapshot(tabs=[tab]))
    await rig.hello()

    await rig.output("a", status("idle") + status("working") + status("idle"))
    assert rig.tabs()[0]["attention"] == NEEDS_INPUT

    # What the terminal itself reports arrives as input too, and is not an answer.
    await rig.manager.handle({"type": "input", "id": "a", "data": "\x1b[?1;2c"})
    await rig.output("a", status("working") + status("idle"))
    assert rig.tabs()[0]["attention"] == NEEDS_INPUT

    await rig.manager.handle({"type": "input", "id": "a", "data": "y"})
    await rig.output("a", status("working"))
    assert rig.tabs()[0]["attention"] == NO_ATTENTION


@run
async def test_restore_survives_a_stale_active_id(rig):
    path = str(rig.settings.projects_root / "repo")
    save_snapshot(
        rig.settings.state_file,
        Snapshot(tabs=[make_saved_tab(path)], active_id="gone"),
    )
    await rig.hello()
    assert rig.state()["active_id"] == "saved"


@run
async def test_a_second_hello_does_not_start_anything_twice(rig):
    await rig.hello()
    await rig.create()
    await rig.hello()
    assert len(rig.processes) == 1
    assert len(rig.tabs()) == 1


@run
async def test_changes_are_saved_without_being_asked(rig):
    await rig.hello()
    tab = await rig.create()
    await asyncio.sleep(SAVE_DELAY_SECONDS + 0.2)
    saved = load_snapshot(rig.settings.state_file)
    assert [t.id for t in saved.tabs] == [tab["id"]]


@run
async def test_shutdown_saves_and_ends_every_process(rig):
    await rig.hello()
    tab = await rig.create()
    await rig.manager.handle({"type": "toggle_park", "id": tab["id"]})
    await rig.manager.shutdown()
    saved = load_snapshot(rig.settings.state_file)
    assert saved.tabs[0].parked is True
    assert saved.active_id == tab["id"]
    assert rig.killed == [100]


class KeptSession:
    """A process the host held while the window was away."""

    def __init__(self) -> None:
        self.sizes: list[tuple[int, int]] = []
        self.remembered: list[dict[str, Any]] = []

    def start(self, *_: Any) -> None: ...
    def write(self, text: str) -> None: ...
    def close(self) -> None: ...

    def resize(self, rows: int, cols: int) -> None:
        self.sizes.append((rows, cols))

    def remember(self, state: dict[str, Any]) -> None:
        self.remembered.append(state)


class Keeping(LocalSessions):
    """A source that has survivors to offer, and records what is done with them."""

    def __init__(self, rig: Rig, survivors: dict[str, Survivor]) -> None:
        super().__init__(rig.spawn, rig.killed.append)
        self.offered = survivors
        self.kept = KeptSession()
        self.discarded: list[str] = []
        self.calls: list[str] = []

    async def survivors(self) -> dict[str, Survivor]:
        return dict(self.offered)

    def adopt(self, survivor, on_output, on_exit):
        return self.kept

    async def discard(self, survivor: Survivor) -> None:
        self.discarded.append(survivor.key)

    async def detach(self) -> None:
        self.calls.append("detach")

    async def shutdown(self) -> None:
        self.calls.append("shutdown")


def keep(rig: Rig, survivors: dict[str, Survivor]) -> Keeping:
    source = Keeping(rig, survivors)
    rig.manager.sessions = source
    rig.manager.runner._sessions = source
    return source


@run
async def test_restore_takes_back_a_surviving_process_instead_of_resuming(rig):
    path = str(rig.settings.projects_root / "repo")
    tabs = [make_saved_tab(path, id="a"), make_saved_tab(path, id="b")]
    save_snapshot(rig.settings.state_file, Snapshot(tabs=tabs))
    screen = "still here" + status("working")
    source = keep(
        rig,
        {
            "a": Survivor(tab_id="a", key="ka", alive=True, screen=screen, unread=screen),
            "gone": Survivor(tab_id="gone", key="kg", alive=True),
        },
    )
    await rig.hello()
    # Only the tab with nothing to take back starts a process.
    assert len(rig.processes) == 1
    assert source.discarded == ["kg"]
    kinds = [(m["type"], m.get("id")) for m in rig.messages if m["type"] in ("state", "output")]
    assert kinds.index(("output", "a")) > kinds.index(("state", None))
    replayed = next(m for m in rig.messages if m.get("id") == "a")
    assert replayed["data"] == screen
    assert replayed["replay"] is True, "the page must not answer queries in old output"
    assert next(t for t in rig.tabs() if t["id"] == "a")["status"] == "working"


@run
async def test_an_adopted_process_is_redrawn_at_the_window_size(rig, monkeypatch):
    monkeypatch.setattr("agent_terminal.runner.REDRAW_GAP_SECONDS", 0)
    path = str(rig.settings.projects_root / "repo")
    save_snapshot(rig.settings.state_file, Snapshot(tabs=[make_saved_tab(path, id="a")]))
    source = keep(rig, {"a": Survivor(tab_id="a", key="ka", alive=True)})
    await rig.hello()
    await asyncio.sleep(0.01)
    assert source.kept.sizes == [(30, 99), (30, 100)]


@run
async def test_detach_saves_and_leaves_the_processes_running(rig):
    path = str(rig.settings.projects_root / "repo")
    save_snapshot(rig.settings.state_file, Snapshot(tabs=[make_saved_tab(path, id="a")]))
    source = keep(rig, {"a": Survivor(tab_id="a", key="ka", alive=True)})
    await rig.hello()
    await rig.manager.handle({"type": "toggle_park", "id": "a"})
    await rig.manager.shutdown(keep_processes=True)
    assert load_snapshot(rig.settings.state_file).tabs[0].parked is True
    assert source.kept.remembered
    assert source.calls == ["detach"]
    assert rig.killed == []


@run
async def test_shutdown_ends_the_host_after_the_processes(rig):
    source = keep(rig, {})
    await rig.hello()
    await rig.create()
    await rig.manager.shutdown()
    assert rig.killed == [100]
    assert source.calls == ["shutdown"]


@run
async def test_a_resize_is_remembered_for_the_next_window(rig):
    await rig.hello()
    tab = await rig.create()
    await rig.manager.handle({"type": "resize", "rows": 40, "cols": 140})
    assert rig.manager.runner.live[tab["id"]].size == (40, 140)


def test_more_handlers_answer_more_types(tmp_path):
    seen: list[dict] = []

    async def handler(message):
        seen.append(message)

    async def body() -> None:
        settings = Settings(data_dir=tmp_path, gh_command=str(tmp_path / "no-gh"))
        loop = asyncio.get_running_loop()
        manager = TabManager(settings, print, loop, handlers={"app_status": handler})
        await manager.handle({"type": "app_status"})

    asyncio.run(body())
    assert seen == [{"type": "app_status"}]


def ids(rig: Rig) -> list[str]:
    return [t["id"] for t in rig.tabs()]


async def two_groups(rig: Rig) -> tuple[list[str], list[str]]:
    """Tabs a1, b1, a2, a3 in that order: `repo` is a, `plain` is b."""
    a1 = (await rig.create())["id"]
    b1 = (await rig.create(rig.plain))["id"]
    a2 = (await rig.create())["id"]
    a3 = (await rig.create())["id"]
    return [a1, a2, a3], [b1]


@run
async def test_a_tab_moves_before_another_of_its_group(rig):
    await rig.hello()
    (a1, a2, a3), (b1,) = await two_groups(rig)
    await rig.manager.handle({"type": "move_tab", "id": a3, "before": a1})
    assert ids(rig) == [a3, a1, b1, a2]


@run
async def test_a_tab_moved_before_nothing_goes_last_in_its_group(rig):
    await rig.hello()
    (a1, a2, a3), (b1,) = await two_groups(rig)
    await rig.manager.handle({"type": "move_tab", "id": a1, "before": None})
    assert ids(rig) == [b1, a2, a3, a1]
    # Group order is its own record, so tabs moving about never reorders the groups.
    assert rig.state()["group_order"] == [str(rig.repo.resolve()), str(rig.plain.resolve())]


@run
async def test_a_tab_cannot_move_into_another_group(rig):
    await rig.hello()
    (a1, a2, a3), (b1,) = await two_groups(rig)
    await rig.manager.handle({"type": "move_tab", "id": a1, "before": b1})
    assert ids(rig) == [a1, b1, a2, a3]


@run
async def test_a_tab_moved_before_itself_stays(rig):
    await rig.hello()
    (a1, a2, a3), (b1,) = await two_groups(rig)
    await rig.manager.handle({"type": "move_tab", "id": a2, "before": a2})
    assert ids(rig) == [a1, b1, a2, a3]


@run
async def test_the_only_tab_of_a_group_moved_last_stays_put(rig):
    await rig.hello()
    (a1, a2, a3), (b1,) = await two_groups(rig)
    await rig.manager.handle({"type": "move_tab", "id": b1, "before": None})
    assert ids(rig) == [a1, b1, a2, a3]


@pytest.mark.parametrize(
    "message",
    [
        {"type": "move_tab", "id": "absent", "before": None},
        {"type": "move_tab", "id": "FIRST", "before": "absent"},
        {"type": "move_tab"},
        {"type": "order_groups"},
        {"type": "order_groups", "paths": "C:\\a"},
        {"type": "order_groups", "paths": [1, 2]},
    ],
)
def test_a_bad_reorder_is_dropped_and_changes_nothing(tmp_path, message):
    async def body():
        rig = Rig(tmp_path, asyncio.get_running_loop())
        try:
            await rig.hello()
            first = await rig.create()
            await rig.create(rig.plain)
            before = rig.state()
            filled = {k: first["id"] if v == "FIRST" else v for k, v in message.items()}
            await rig.manager.handle(filled)
            assert rig.state() == before
        finally:
            for process in rig.processes:
                process.end(0)

    asyncio.run(body())


@run
async def test_groups_take_the_order_the_page_gives(rig):
    await rig.hello()
    await two_groups(rig)
    repo, plain = str(rig.repo.resolve()), str(rig.plain.resolve())
    assert rig.state()["group_order"] == [repo, plain]
    await rig.manager.handle({"type": "order_groups", "paths": [plain, repo, plain]})
    assert rig.state()["group_order"] == [plain, repo]


@run
async def test_a_group_the_page_did_not_name_keeps_its_place_after(rig):
    await rig.hello()
    await two_groups(rig)
    repo, plain = str(rig.repo.resolve()), str(rig.plain.resolve())
    await rig.manager.handle({"type": "order_groups", "paths": ["C:\\pr-only", plain]})
    assert rig.state()["group_order"] == ["C:\\pr-only", plain, repo]


@run
async def test_tab_and_group_order_survive_a_restart(rig):
    await rig.hello()
    (a1, a2, a3), (b1,) = await two_groups(rig)
    repo, plain = str(rig.repo.resolve()), str(rig.plain.resolve())
    await rig.manager.handle({"type": "move_tab", "id": a3, "before": a1})
    await rig.manager.handle({"type": "order_groups", "paths": [plain, repo]})
    await rig.manager.shutdown()
    saved = load_snapshot(rig.settings.state_file)
    assert [t.id for t in saved.tabs] == [a3, a1, b1, a2]
    assert saved.group_order == [plain, repo]


@run
async def test_a_restore_from_before_group_order_orders_groups_as_they_appear(rig):
    b, a = str(rig.plain), str(rig.repo)
    tabs = [make_saved_tab(b, id="b"), make_saved_tab(a, id="a"), make_saved_tab(b, id="c")]
    save_snapshot(rig.settings.state_file, Snapshot(tabs=tabs))
    await rig.hello()
    assert rig.state()["group_order"] == [b, a]


@run
async def test_a_tab_says_when_it_was_last_updated(rig):
    await rig.hello()
    tab = await rig.create()
    assert tab["updated_at"] is None
    await rig.manager.handle({"type": "input", "id": tab["id"], "data": "go\r"})
    await rig.output(tab["id"], status("working"))
    assert rig.tabs()[0]["updated_at"] >= tab["created_at"]


def write_rollout(settings: Settings, session_id: str, cwd: str, started: str) -> None:
    directory = settings.codex_home / "sessions" / "2099" / "01" / "01"
    directory.mkdir(parents=True, exist_ok=True)
    head = {
        "type": "session_meta",
        "payload": {"id": session_id, "cwd": cwd, "timestamp": started},
    }
    (directory / f"rollout-{session_id}.jsonl").write_text(json.dumps(head) + "\n")


@run
async def test_a_codex_tab_learns_its_session_from_the_rollout(rig):
    await rig.hello()
    tab = await rig.create(agent="codex")
    write_rollout(rig.settings, "found", tab["project_path"], "2099-01-01T00:00:00Z")
    await rig.output(tab["id"], status("working"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if rig.manager._tabs[0].session_id:
            break
    assert rig.manager._tabs[0].session_id == "found"
    assert rig.manager._tabs[0].cwd == tab["project_path"]


@run
async def test_shutdown_finds_a_codex_session_not_found_yet(rig):
    await rig.hello()
    tab = await rig.create(agent="codex")
    write_rollout(rig.settings, "late", tab["project_path"], "2099-01-01T00:00:00Z")
    await rig.manager.shutdown()
    saved = load_snapshot(rig.settings.state_file)
    assert saved.tabs[0].session_id == "late"


def distrust(rig: Rig) -> None:
    rig.settings.claude_config_file.write_text(json.dumps({"projects": {}}))
    (rig.settings.codex_home / "config.toml").write_text("")


async def settle(condition) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.01)


@run
async def test_untrusted_worktree_runs_claude_plain_at_the_repository(rig):
    rig.make_repository()
    distrust(rig)
    await rig.hello()
    tab = await rig.create(worktree=True)
    assert tab["worktree"] is True
    assert "--worktree" not in rig.processes[0].command()
    assert "--session-id" not in rig.processes[0].command()
    assert Path(rig.processes[0].cwd) == rig.repo.resolve()


@run
async def test_a_tab_on_a_trust_screen_says_so(rig):
    rig.make_repository()
    distrust(rig)
    await rig.hello()
    await rig.create(worktree=True)
    assert rig.tabs()[0]["awaiting_trust"] is True


@run
async def test_accepting_trust_loops_back_to_the_worktree(rig):
    rig.make_repository()
    distrust(rig)
    await rig.hello()
    tab = await rig.create(worktree=True)

    await rig.output(tab["id"], status("idle"))
    await settle(lambda: len(rig.processes) == 2)

    expected = f"& 'claude' '--session-id' '{tab['session_id']}' '--worktree';"
    assert rig.processes[1].command().startswith(expected)
    assert {"type": "reset", "id": tab["id"]} in rig.messages
    assert rig.tabs()[0]["id"] == tab["id"]
    assert rig.tabs()[0]["awaiting_trust"] is False


@run
async def test_the_plain_process_ends_when_the_worktree_replaces_it(rig):
    rig.make_repository()
    distrust(rig)
    await rig.hello()
    tab = await rig.create(worktree=True)
    await rig.output(tab["id"], status("idle"))
    await settle(lambda: len(rig.processes) == 2)
    assert rig.killed == [100]


@run
async def test_the_replaced_process_exiting_leaves_the_tab_open(rig):
    rig.make_repository()
    distrust(rig)
    await rig.hello()
    tab = await rig.create(worktree=True)
    await rig.output(tab["id"], status("idle"))
    await settle(lambda: len(rig.processes) == 2)
    rig.processes[0].end(0)
    await asyncio.sleep(0.05)
    assert [t["id"] for t in rig.tabs()] == [tab["id"]]


@run
async def test_declining_trust_leaves_the_tab_at_its_shell(rig):
    rig.make_repository()
    distrust(rig)
    await rig.hello()
    tab = await rig.create(worktree=True)
    await rig.output(tab["id"], status("clear"))
    await asyncio.sleep(0.05)
    assert len(rig.processes) == 1
    assert rig.tabs()[0]["awaiting_trust"] is False
    assert rig.tabs()[0]["status"] == "shell"
    output = "".join(m["data"] for m in rig.messages if m["type"] == "output")
    assert "wasn't trusted" in output


@run
async def test_a_plain_untrusted_tab_keeps_its_one_process(rig):
    distrust(rig)
    await rig.hello()
    tab = await rig.create()
    assert rig.tabs()[0]["awaiting_trust"] is True
    await rig.output(tab["id"], status("idle"))
    await asyncio.sleep(0.05)
    assert len(rig.processes) == 1
    assert rig.tabs()[0]["awaiting_trust"] is False


@run
async def test_codex_asks_for_itself_so_its_worktree_runs_straight_away(rig):
    rig.make_repository()
    distrust(rig)
    await rig.hello()
    tab = await rig.create(agent="codex", worktree=True)
    assert rig.processes[0].command().startswith("& 'codex' '--worktree'")
    assert rig.tabs()[0]["awaiting_trust"] is True
    await rig.output(tab["id"], status("idle"))
    await asyncio.sleep(0.05)
    assert len(rig.processes) == 1


@run
async def test_a_resumed_session_is_not_taken_for_a_first_visit(rig):
    distrust(rig)
    project = rig.settings.claude_home / "projects" / "encoded"
    project.mkdir(parents=True)
    (project / "s1.jsonl").write_text("{}\n")
    save_snapshot(
        rig.settings.state_file, Snapshot(tabs=[make_saved_tab(str(rig.repo), session_id="s1")])
    )
    await rig.hello()
    assert rig.tabs()[0]["awaiting_trust"] is False


@run
async def test_inspecting_a_folder_echoes_what_was_asked(rig):
    await rig.manager.handle({"type": "inspect_folder", "path": str(rig.plain)})
    reply = rig.messages[-1]
    assert reply["type"] == "folder"
    assert reply["requested"] == str(rig.plain)
    assert reply["exists"] is True
    assert reply["error"] == ""


@run
async def test_browsing_returns_the_chosen_folder(rig):
    rig.picked = "C:\\chosen"
    await rig.manager.handle({"type": "pick_folder"})
    assert rig.messages[-1] == {"type": "picked", "path": "C:\\chosen"}


@run
async def test_cancelling_the_browse_dialog_says_nothing(rig):
    await rig.manager.handle({"type": "pick_folder"})
    assert rig.messages == []


@run
async def test_preparing_a_folder_makes_a_worktree_possible(rig, monkeypatch):
    for name in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{name}_NAME", "T")
        monkeypatch.setenv(f"GIT_{name}_EMAIL", "t@example.com")
    (rig.plain / "kept.txt").write_text("x")
    await rig.manager.handle({"type": "prepare_worktree", "path": str(rig.plain)})
    reply = rig.messages[-1]
    assert reply["has_commits"] is True
    assert reply["error"] == ""


@run
async def test_gits_refusal_goes_back_to_the_dialog(rig, monkeypatch):
    def refuse(_folder):
        raise module.gitinfo.GitError("Author identity unknown")

    monkeypatch.setattr(module.gitinfo, "commit_everything", refuse)
    await rig.manager.handle({"type": "prepare_worktree", "path": str(rig.plain)})
    assert rig.messages[-1]["error"] == "Author identity unknown"

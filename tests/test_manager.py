import asyncio
import base64
import inspect
import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_terminal import manager as module
from agent_terminal.attention import FINISHED, NEEDS_INPUT, NO_ATTENTION, QUERY_REPLY
from agent_terminal.desktop import Desktop, is_web_url
from agent_terminal.manager import TabManager
from agent_terminal.pty_session import SpawnError
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
        self.manager = TabManager(
            self.settings,
            self.messages.append,
            self.loop,
            spawn=self.spawn,
            kill=self.killed.append,
            desktop=Desktop(
                self.opened.append,
                lambda: self.clipboard,
                self.set_clipboard,
                lambda: self.picked,
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
async def test_a_claude_worktree_tab_names_its_worktree(rig):
    rig.make_repository()
    await rig.hello()
    tab = await rig.create(worktree=True)
    assert tab["worktree"] is True
    assert f"'--worktree' '{tab['worktree_name']}'" in rig.processes[0].command()


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
async def test_the_status_query_is_answered_at_once(rig):
    await rig.hello()
    tab = await rig.create()
    await rig.output(tab["id"], "\x1b]7501;?\x1b\\")
    assert rig.processes[0].written == [QUERY_REPLY]


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


@run
async def test_answering_clears_attention(rig):
    await rig.hello()
    tab = await rig.create()
    await rig.output(tab["id"], status("working") + status("blocked"))
    await rig.manager.handle({"type": "input", "id": tab["id"], "data": "1"})
    await rig.output(tab["id"], status("working"))
    assert rig.tabs()[0]["attention"] == NO_ATTENTION


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
    assert rig.state() == {"type": "state", "active_id": None, "tabs": [], "prs": []}


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

    assert f"'--worktree' '{tab['worktree_name']}'" in rig.processes[1].command()
    assert f"'--session-id' '{tab['session_id']}'" in rig.processes[1].command()
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

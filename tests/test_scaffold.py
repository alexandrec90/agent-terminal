import asyncio
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from agent_terminal import scaffold
from agent_terminal.scaffold import NewProjectRequests
from agent_terminal.settings import Settings

# What argparse prints for new-project.py, cut to the line this reads.
HELP = """usage: new-project.py [-h] [--description DESCRIPTION]
                      [--preset {bare,data,fullstack,service}]
                      name
"""


class FakeScript:
    """Stands in for running new-project.py: answers `--help` and, applied, makes the
    folder. Never starts a process."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[Mapping[str, str]] = []
        self.help = HELP
        self.code = 0
        self.said = ["planning", "done"]

    def __call__(
        self, argv: list[str], cwd: Path, env: Mapping[str, str], on_line: Callable[[str], None]
    ) -> int:
        self.calls.append(argv)
        self.envs.append(env)
        if argv[-1] == "--help":
            for line in self.help.splitlines():
                on_line(line)
            return 0
        for line in self.said:
            on_line(line)
        if self.code == 0 and "--yes" in argv:
            parent = next(a.removeprefix("--parent=") for a in argv if a.startswith("--parent="))
            (Path(parent) / argv[2]).mkdir()
        return self.code


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "projects"
        self.devkit = tmp_path / "devkit"
        (self.devkit / "scripts").mkdir(parents=True)
        (self.devkit / "scripts" / "new-project.py").write_text("", encoding="utf-8")
        self.settings = Settings(
            projects_root=self.root,
            devkit_dir=self.devkit,
            data_dir=tmp_path / "data",
            python_command="py",
        )
        self.sent: list[dict[str, Any]] = []
        self.listed = 0
        self.script = FakeScript()
        self.requests = NewProjectRequests(
            self.settings, self.sent.append, asyncio.get_running_loop(), self.list, self.script
        )

    def list(self) -> None:
        self.listed += 1

    async def send(self, kind: str, **fields: Any) -> None:
        await self.requests.handlers()[kind]({"type": kind, **fields})
        # Lines are posted to the loop from the worker thread: let them land.
        await asyncio.sleep(0)

    async def create(self, **fields: Any) -> dict[str, Any]:
        choice = {"name": "fresh", "description": "", "preset": "service", "remote": False}
        await self.send("new_project", **{**choice, **fields})
        return self.last("new_project_done")

    def last(self, kind: str) -> dict[str, Any]:
        return next(m for m in reversed(self.sent) if m["type"] == kind)

    def lines(self) -> list[str]:
        return [m["line"] for m in self.sent if m["type"] == "new_project_output"]


def run(body):
    def wrapper(tmp_path):
        async def go():
            await body(Rig(tmp_path))

        asyncio.run(go())

    wrapper.__name__ = body.__name__
    return wrapper


def test_presets_are_read_from_the_help() -> None:
    assert scaffold.parse_presets(HELP) == ["bare", "data", "fullstack", "service"]


def test_help_without_presets_is_refused() -> None:
    with pytest.raises(ValueError, match="presets"):
        scaffold.parse_presets("usage: new-project.py name")


def test_the_default_preset_comes_first_and_unknown_ones_show_their_name() -> None:
    options = scaffold.preset_options(["bare", "service", "newshape"])
    assert [o["value"] for o in options] == ["service", "bare", "newshape"]
    assert options[0]["label"].startswith("service: ")
    assert options[2]["label"] == "newshape"


@pytest.mark.parametrize("name", ["", "-rm", "has space", "a/b", "..", "_lead", 7, None])
def test_names_a_project_cannot_have_are_refused(tmp_path: Path, name: object) -> None:
    with pytest.raises(ValueError, match="letters, digits"):
        scaffold.checked_name(tmp_path, name)


def test_a_name_already_in_the_root_is_refused_whatever_its_case(tmp_path: Path) -> None:
    (tmp_path / "Taken").mkdir()
    with pytest.raises(ValueError, match="already"):
        scaffold.checked_name(tmp_path, "taken")
    assert scaffold.checked_name(tmp_path, "free-1_x") == "free-1_x"


def test_a_description_is_one_line_and_bounded() -> None:
    assert scaffold.checked_description("  two\nlines  ") == "two lines"
    with pytest.raises(ValueError, match="under"):
        scaffold.checked_description("x" * (scaffold.MAX_DESCRIPTION + 1))
    with pytest.raises(ValueError, match="text"):
        scaffold.checked_description(3)


def test_every_value_is_bound_to_its_flag(tmp_path: Path) -> None:
    choice = {
        "name": "fresh",
        "description": "--yes it starts with dashes",
        "preset": "data",
        "remote": True,
        "apply": False,
    }
    argv = scaffold.new_project_argv("py", tmp_path / "devkit", tmp_path / "root", choice)
    assert argv == [
        "py",
        str(tmp_path / "devkit" / "scripts" / "new-project.py"),
        "fresh",
        "--description=--yes it starts with dashes",
        "--preset=data",
        f"--parent={tmp_path / 'root'}",
        "--remote",
        "--dry-run",
    ]


def test_the_apps_virtualenv_does_not_reach_the_script() -> None:
    env = scaffold.script_env({"VIRTUAL_ENV": "C:/app/.venv", "PATH": "p"})
    assert "VIRTUAL_ENV" not in env
    assert env["PATH"] == "p"
    assert env["PYTHONUNBUFFERED"] == "1"


def test_run_lines_streams_each_line_and_returns_the_exit_code(tmp_path: Path) -> None:
    lines: list[str] = []
    code = scaffold.run_lines(
        [sys.executable, "-c", "print('one'); print('two'); raise SystemExit(3)"],
        tmp_path,
        scaffold.script_env({"PATH": ""}),
        lines.append,
    )
    assert (code, lines) == (3, ["one", "two"])


@run
async def test_presets_are_offered_from_the_script(rig):
    await rig.send("list_presets")
    reply = rig.last("presets")
    assert reply["error"] == ""
    assert [p["value"] for p in reply["presets"]] == ["service", "bare", "data", "fullstack"]
    assert rig.script.calls == [["py", str(rig.devkit / scaffold.SCRIPT), "--help"]]


@run
async def test_no_devkit_is_said_in_the_panel(rig):
    (rig.devkit / scaffold.SCRIPT).unlink()
    await rig.send("list_presets")
    assert rig.last("presets")["presets"] == []
    assert "is not at" in rig.last("presets")["error"]
    assert rig.script.calls == []


@run
async def test_a_preview_writes_nothing_and_chooses_nothing(rig):
    done = await rig.create(apply=False)
    assert done["error"] == ""
    assert (done["applied"], done["path"]) == (False, "")
    assert "--dry-run" in rig.script.calls[-1]
    assert not (rig.root / "fresh").exists()
    assert rig.listed == 0
    assert rig.lines() == ["planning", "done"]


@run
async def test_a_created_project_is_listed_and_named(rig):
    done = await rig.create(apply=True, remote=True)
    assert done["error"] == ""
    assert done["applied"] is True
    assert done["path"] == str(rig.root / "fresh")
    assert rig.listed == 1
    argv = rig.script.calls[-1]
    assert "--yes" in argv
    assert "--remote" in argv
    assert f"--parent={rig.root}" in argv


@run
async def test_only_a_literal_true_applies_or_pushes(rig):
    await rig.create(apply="yes", remote=1)
    argv = rig.script.calls[-1]
    assert "--dry-run" in argv
    assert "--no-remote" in argv


@run
async def test_the_whole_run_is_kept_in_the_log(rig):
    rig.script.code = 1
    rig.script.said = ["new-project: port registry full"]
    done = await rig.create(apply=True)
    assert "exited 1" in done["error"]
    assert done["log"] == str(rig.requests.log_file)
    kept = rig.requests.log_file.read_text(encoding="utf-8")
    assert "new-project: port registry full" in kept
    assert kept.rstrip().endswith("exit code 1")
    assert rig.listed == 0


@run
async def test_a_bad_name_never_starts_the_script(rig):
    done = await rig.create(name="-rf")
    assert "letters, digits" in done["error"]
    assert all(call[-1] == "--help" for call in rig.script.calls)


@run
async def test_a_preset_the_script_lacks_is_refused(rig):
    done = await rig.create(preset="nonsense")
    assert "not a preset" in done["error"]


@run
async def test_an_applied_run_that_made_no_folder_is_a_failure(rig):
    rig.script.code = 0

    def no_folder(argv, cwd, env, on_line):
        return 0 if argv[-1] != "--help" else FakeScript()(argv, cwd, env, on_line)

    rig.requests._run = no_folder
    done = await rig.create(apply=True)
    assert "is not there" in done["error"]
    assert rig.listed == 0


@run
async def test_a_second_run_waits_for_the_first(rig):
    release = asyncio.Event()
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    inner = rig.script

    def slow(argv, cwd, env, on_line):
        if argv[-1] != "--help":
            loop.call_soon_threadsafe(started.set)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
        return inner(argv, cwd, env, on_line)

    rig.requests._run = slow
    first = asyncio.create_task(rig.create(apply=False))
    await started.wait()
    second = await rig.create(name="other", apply=False)
    assert "already being made" in second["error"]
    release.set()
    assert (await first)["error"] == ""

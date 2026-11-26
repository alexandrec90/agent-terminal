import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agent_terminal.desktop import Desktop
from agent_terminal.project_requests import ProjectRequests
from agent_terminal.prs import Gh, GhError
from agent_terminal.retire import RECORD, SCRIPT, Retirement
from agent_terminal.settings import Settings


class FakeGh(Gh):
    """Answers `repo list` from a list and makes a folder for a clone. Never runs `gh`."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.repos: list[dict[str, Any]] = []
        self.refusal: str | None = None
        self.listed_in: list[Path] = []
        self.cloned: list[tuple[str, Path]] = []

    def list_repos(self, where: Path) -> str:
        self.listed_in.append(where)
        if self.refusal:
            raise GhError(self.refusal)
        return json.dumps(self.repos)

    def clone(self, full_name: str, destination: Path) -> None:
        if self.refusal:
            raise GhError(self.refusal)
        self.cloned.append((full_name, destination))
        destination.mkdir()


class Rig:
    def __init__(self, tmp_path: Path, retirement: Retirement | None = None) -> None:
        self.root = tmp_path / "projects"
        self.settings = Settings(projects_root=self.root, gh_command=str(tmp_path / "no-gh"))
        self.sent: list[dict[str, Any]] = []
        self.recycled: list[Path] = []
        self.recycle_error: OSError | None = None
        self.open: list[Path] = []
        self.gh = FakeGh(self.settings)
        self.requests = ProjectRequests(
            self.settings,
            self.sent.append,
            asyncio.get_running_loop(),
            Desktop(recycle=self.recycle),
            self.gh,
            lambda: list(self.open),
        )
        if retirement is not None:
            self.requests._retirement = retirement

    def recycle(self, path: Path) -> None:
        if self.recycle_error:
            raise self.recycle_error
        self.recycled.append(path)
        path.rmdir()

    async def send(self, kind: str, **fields: Any) -> None:
        await self.requests.handlers()[kind]({"type": kind, **fields})

    def last(self, kind: str) -> dict[str, Any]:
        return next(m for m in reversed(self.sent) if m["type"] == kind)

    def project_names(self) -> list[str]:
        return [p["name"] for p in self.last("projects")["projects"]]


def run(body):
    def wrapper(tmp_path):
        async def go():
            await body(Rig(tmp_path))

        asyncio.run(go())

    wrapper.__name__ = body.__name__
    return wrapper


def record(name: str) -> dict[str, Any]:
    return {"name": name, "nameWithOwner": f"me/{name}", "description": "", "isArchived": False}


@run
async def test_lists_only_repositories_not_here(rig):
    (rig.root / "here").mkdir(parents=True)
    rig.gh.repos = [record("here"), record("away")]
    await rig.send("list_remote_repos")
    reply = rig.last("remote_repos")
    assert [r["full_name"] for r in reply["repos"]] == ["me/away"]
    assert reply["error"] == ""
    assert rig.gh.listed_in == [rig.root]


@run
async def test_lists_from_home_when_there_is_no_root_yet(rig):
    await rig.send("list_remote_repos")
    assert rig.gh.listed_in == [Path.home()]


@run
async def test_a_gh_refusal_is_shown_in_its_words(rig):
    rig.gh.refusal = "gh auth login required"
    await rig.send("list_remote_repos")
    assert rig.last("remote_repos") == {
        "type": "remote_repos",
        "repos": [],
        "error": "gh auth login required",
    }


@run
async def test_a_clone_lands_under_the_root_and_is_listed(rig):
    await rig.send("clone_repo", full_name="me/away")
    assert rig.gh.cloned == [("me/away", rig.root / "away")]
    assert rig.last("cloned") == {
        "type": "cloned",
        "full_name": "me/away",
        "error": "",
        "path": str(rig.root / "away"),
    }
    assert rig.project_names() == ["away"]


@run
async def test_a_bad_name_never_reaches_gh(rig):
    await rig.send("clone_repo", full_name="--upload-pack=evil")
    assert rig.gh.cloned == []
    assert rig.last("cloned")["error"]


@run
async def test_a_clone_over_an_existing_folder_is_refused(rig):
    (rig.root / "away").mkdir(parents=True)
    await rig.send("clone_repo", full_name="me/away")
    assert rig.gh.cloned == []
    assert "already" in rig.last("cloned")["error"]


@run
async def test_a_failed_clone_says_why(rig):
    rig.gh.refusal = "repository not found"
    await rig.send("clone_repo", full_name="me/away")
    assert rig.last("cloned")["error"] == "repository not found"
    assert not any(m["type"] == "projects" for m in rig.sent)


@run
async def test_a_deletion_is_described_before_it_is_done(rig):
    (rig.root / "old").mkdir(parents=True)
    await rig.send("describe_deletion", path=str(rig.root / "old"))
    reply = rig.last("deletion")
    assert reply["name"] == "old"
    assert reply["requested"] == str(rig.root / "old")
    assert "isn't a git repository" in reply["risks"][0]
    assert rig.recycled == []


@run
async def test_describing_a_folder_outside_the_root_is_refused(rig):
    rig.root.mkdir()
    await rig.send("describe_deletion", path=str(rig.root.parent))
    assert rig.last("deletion")["error"].startswith("not a project")


@run
async def test_delete_sends_the_project_to_the_recycle_bin(rig):
    (rig.root / "old").mkdir(parents=True)
    (rig.root / "kept").mkdir()
    await rig.send("delete_project", path=str(rig.root / "old"))
    assert rig.recycled == [(rig.root / "old").resolve()]
    assert rig.last("deleted")["error"] == ""
    assert rig.project_names() == ["kept"]


@run
async def test_a_project_with_a_tab_open_is_not_deleted(rig):
    (rig.root / "old").mkdir(parents=True)
    rig.open = [rig.root / "old"]
    await rig.send("delete_project", path=str(rig.root / "old"))
    assert rig.recycled == []
    assert "close it first" in rig.last("deleted")["error"]


@run
async def test_a_failed_recycle_says_why(rig):
    (rig.root / "old").mkdir(parents=True)
    rig.recycle_error = OSError("something in it is open")
    await rig.send("delete_project", path=str(rig.root / "old"))
    assert rig.last("deleted")["error"] == "something in it is open"
    assert (rig.root / "old").exists()


class Devkit:
    """devkit's retire-project.py as the app sees it: a script that answers each verb
    with lines and an exit code, and a record of what it retired."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "devkit"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / SCRIPT).write_text("", encoding="utf-8")
        self.settings = Settings(
            devkit_dir=self.root, data_dir=tmp_path / "data", projects_root=tmp_path / "projects"
        )
        self.answers: dict[str, tuple[list[str], int]] = {}
        self.calls: list[list[str]] = []
        self.on_restore: Path | None = None

    def answer(self, verb: str, lines: list[str], code: int = 0) -> None:
        self.answers[verb] = (lines, code)

    def retired(self, *names: str) -> None:
        record = self.root / RECORD
        record.parent.mkdir(parents=True, exist_ok=True)
        entries = {n: {"retired_at": "2026-11-25T18:00:00"} for n in names}
        record.write_text(json.dumps(entries), encoding="utf-8")

    def run(self, argv, _cwd, _env, on_line) -> int:
        self.calls.append(argv[2:])
        verb = "dry-run" if "--dry-run" in argv else argv[2]
        lines, code = self.answers.get(verb, ([], 0))
        for line in lines:
            on_line(line)
        if verb == "restore" and code == 0 and self.on_restore is not None:
            self.on_restore.mkdir(parents=True, exist_ok=True)
        return code

    def retirement(self) -> Retirement:
        return Retirement(self.settings, self.run)


def with_devkit(body):
    def wrapper(tmp_path):
        async def go():
            devkit = Devkit(tmp_path)
            await body(Rig(tmp_path, devkit.retirement()), devkit)

        asyncio.run(go())

    wrapper.__name__ = body.__name__
    return wrapper


RETIRE_PLAN = ["  retire old will:", "    unplug from the registry", "    stop its collector here"]


@with_devkit
async def test_the_confirmation_says_what_retiring_will_stop(rig, devkit):
    (rig.root / "old").mkdir(parents=True)
    devkit.answer("dry-run", RETIRE_PLAN)
    await rig.send("describe_deletion", path=str(rig.root / "old"))
    reply = rig.last("deletion")
    assert reply["retire"] == {
        "steps": ["unplug from the registry", "stop its collector here"],
        "notes": [],
    }
    assert devkit.calls == [["retire", "old", "--dry-run"]]


@with_devkit
async def test_a_project_devkit_will_not_retire_is_not_deleted(rig, devkit):
    (rig.root / "devkit").mkdir(parents=True)
    devkit.answer("dry-run", ["ERROR   devkit is the checkout every other one is driven from"], 2)
    await rig.send("describe_deletion", path=str(rig.root / "devkit"))
    assert "devkit is the checkout" in rig.last("deletion")["error"]


@with_devkit
async def test_delete_retires_then_recycles(rig, devkit):
    (rig.root / "old").mkdir(parents=True)
    await rig.send("delete_project", path=str(rig.root / "old"))
    assert devkit.calls == [["retire", "old", "--yes"]]
    assert rig.recycled == [(rig.root / "old").resolve()]
    assert rig.last("deleted")["error"] == ""
    assert rig.last("retired")["error"] == ""


@with_devkit
async def test_a_refused_retire_keeps_the_folder_and_offers_force(rig, devkit):
    (rig.root / "old").mkdir(parents=True)
    devkit.answer("retire", ["ERROR   unplug from the registry: exit 1 -- unpushed work"], 1)
    await rig.send("delete_project", path=str(rig.root / "old"))
    reply = rig.last("deleted")
    assert "unpushed work" in reply["error"]
    assert reply["can_force"] is True
    assert reply["name"] == "old"
    assert rig.recycled == []


@with_devkit
async def test_delete_anyway_forces_the_retire(rig, devkit):
    (rig.root / "old").mkdir(parents=True)
    await rig.send("delete_project", path=str(rig.root / "old"), force=True)
    assert devkit.calls == [["retire", "old", "--yes", "--force"]]
    assert rig.recycled == [(rig.root / "old").resolve()]


@with_devkit
async def test_only_a_true_force_forces(rig, devkit):
    (rig.root / "old").mkdir(parents=True)
    await rig.send("delete_project", path=str(rig.root / "old"), force="yes")
    assert devkit.calls == [["retire", "old", "--yes"]]


@with_devkit
async def test_a_failed_recycle_after_a_retire_says_restore_undoes_it(rig, devkit):
    (rig.root / "old").mkdir(parents=True)
    rig.recycle_error = OSError("Windows could not recycle it")
    await rig.send("delete_project", path=str(rig.root / "old"))
    error = rig.last("deleted")["error"]
    assert error.startswith("Windows could not recycle it")
    assert "Restore puts that back" in error
    assert rig.last("deleted")["can_force"] is False


@with_devkit
async def test_a_folder_name_devkit_cannot_take_is_only_recycled(rig, devkit):
    (rig.root / "my notes").mkdir(parents=True)
    await rig.send("describe_deletion", path=str(rig.root / "my notes"))
    assert "not a devkit project name" in rig.last("deletion")["retire"]["notes"][0]
    await rig.send("delete_project", path=str(rig.root / "my notes"))
    assert devkit.calls == []
    assert rig.recycled == [(rig.root / "my notes").resolve()]


@run
async def test_without_devkit_delete_only_recycles_and_says_so(rig):
    (rig.root / "old").mkdir(parents=True)
    await rig.send("describe_deletion", path=str(rig.root / "old"))
    assert "retire-project.py is not at" in rig.last("deletion")["retire"]["notes"][0]
    await rig.send("delete_project", path=str(rig.root / "old"))
    assert rig.recycled == [(rig.root / "old").resolve()]


@with_devkit
async def test_the_retired_list_is_devkits_record(rig, devkit):
    devkit.retired("radio", "tvshell")
    await rig.send("list_retired")
    assert [p["name"] for p in rig.last("retired")["projects"]] == ["radio", "tvshell"]


@run
async def test_without_devkit_the_retired_list_says_why(rig):
    await rig.send("list_retired")
    reply = rig.last("retired")
    assert reply["projects"] == []
    assert "retire-project.py is not at" in reply["error"]


@with_devkit
async def test_restore_brings_the_project_back_and_lists_it(rig, devkit):
    devkit.retired("radio")
    devkit.on_restore = rig.root / "radio"
    await rig.send("restore_project", name="radio")
    assert devkit.calls == [["restore", "radio", "--yes"]]
    assert rig.last("restored") == {
        "type": "restored",
        "name": "radio",
        "path": str(rig.root / "radio"),
        "error": "",
    }
    assert rig.project_names() == ["radio"]


@with_devkit
async def test_only_a_project_retired_here_is_restored(rig, devkit):
    devkit.retired("radio")
    await rig.send("restore_project", name="--force")
    assert devkit.calls == []
    assert "not retired on this machine" in rig.last("restored")["error"]


@with_devkit
async def test_a_failed_restore_says_why(rig, devkit):
    devkit.retired("radio")
    devkit.answer("restore", ["ERROR   plug into the registry: exit 1 -- clone failed"], 1)
    await rig.send("restore_project", name="radio")
    assert "clone failed" in rig.last("restored")["error"]


@with_devkit
async def test_a_restore_without_a_name_is_refused(rig, devkit):
    await rig.send("restore_project")
    assert rig.last("restored")["error"] == "no project named"
    assert devkit.calls == []


@pytest.mark.parametrize("kind", ["describe_deletion", "delete_project"])
def test_outside_the_root_nothing_is_recycled(tmp_path, kind):
    async def body():
        rig = Rig(tmp_path)
        rig.root.mkdir()
        await rig.send(kind, path=str(tmp_path))
        assert rig.recycled == []

    asyncio.run(body())

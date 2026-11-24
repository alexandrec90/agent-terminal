import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agent_terminal.desktop import Desktop
from agent_terminal.project_requests import ProjectRequests
from agent_terminal.prs import Gh, GhError
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
    def __init__(self, tmp_path: Path) -> None:
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


@pytest.mark.parametrize("kind", ["describe_deletion", "delete_project"])
def test_outside_the_root_nothing_is_recycled(tmp_path, kind):
    async def body():
        rig = Rig(tmp_path)
        rig.root.mkdir()
        await rig.send(kind, path=str(tmp_path))
        assert rig.recycled == []

    asyncio.run(body())

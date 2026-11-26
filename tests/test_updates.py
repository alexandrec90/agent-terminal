import asyncio
import importlib.metadata
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_terminal.settings import Settings
from agent_terminal.updates import (
    AppControl,
    UpdateStatus,
    app_checkout,
    cannot_update,
    check_for_update,
    git,
    git_env,
    installed_from,
    read_update_result,
    source_checkout,
    started_commit,
)


class Git:
    """A `subprocess.run` for git that answers by subcommand."""

    def __init__(self, **answers: str | int) -> None:
        self.answers = {
            "fetch": "",
            "count-behind": "3",
            "count-ahead": "0",
            "branch": "main",
            "head": "a1b2c3",
            "status": "",
            **answers,
        }
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        args = argv[1:]
        if args[0] == "rev-list":
            key = "count-ahead" if args[-1].endswith("..HEAD") else "count-behind"
        elif args[0] == "rev-parse":
            key = "branch" if "--abbrev-ref" in args else "head"
        else:
            key = args[0]
        answer = self.answers[key]
        if isinstance(answer, int):
            return subprocess.CompletedProcess(argv, answer, "", f"{key} failed")
        return subprocess.CompletedProcess(argv, 0, answer + "\n", "")


def test_behind_and_free_to_update(tmp_path):
    status = check_for_update(tmp_path, "main", run=Git())
    assert status == UpdateStatus(behind=3, checked=True)


def test_the_fetch_asks_for_the_branch_alone(tmp_path):
    run = Git()
    check_for_update(tmp_path, "main", run=run)
    assert run.calls[0] == ["git", "fetch", "--quiet", "origin", "main"]


def test_behind_counts_from_the_commit_the_app_started_on(tmp_path):
    """`land.py` fast-forwards the checkout the moment a PR merges, so HEAD is level with
    origin while the window still runs the code it started with."""
    run = Git()
    check_for_update(tmp_path, "main", "a1b2c3", run=run)
    assert ["git", "rev-list", "--count", "a1b2c3..origin/main"] in run.calls
    assert ["git", "rev-list", "--count", "HEAD..origin/main"] not in run.calls


def test_behind_counts_from_head_when_the_start_is_unknown(tmp_path):
    run = Git()
    check_for_update(tmp_path, "main", "", run=run)
    assert ["git", "rev-list", "--count", "HEAD..origin/main"] in run.calls


def test_the_blockers_still_read_the_checkout_itself(tmp_path):
    run = Git()
    check_for_update(tmp_path, "main", "a1b2c3", run=run)
    assert ["git", "rev-list", "--count", "origin/main..HEAD"] in run.calls


def test_started_commit_is_the_checkout_head(tmp_path):
    run = Git()
    assert started_commit(tmp_path, run) == "a1b2c3"
    assert run.calls == [["git", "rev-parse", "HEAD"]]


def test_started_commit_that_git_cannot_read_is_empty(tmp_path):
    assert started_commit(tmp_path, Git(head=128)) == ""


@pytest.mark.parametrize(
    ("answers", "reason"),
    [
        ({"branch": "feature"}, "on feature, not main"),
        ({"count-ahead": "2"}, "2 commit(s) that are not on origin/main"),
        ({"status": " M app.py"}, "uncommitted changes"),
    ],
)
def test_a_checkout_that_cannot_fast_forward_says_why(tmp_path, answers, reason):
    status = check_for_update(tmp_path, "main", run=Git(**answers))
    assert reason in status.blocked
    assert status.behind == 3


def test_a_failed_fetch_is_reported_not_raised(tmp_path):
    status = check_for_update(tmp_path, "main", run=Git(fetch=128))
    assert status.error == "fetch failed"
    assert status.checked


def test_git_that_cannot_run_is_a_runtime_error(tmp_path):
    def missing(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("git")

    with pytest.raises(RuntimeError, match="could not run"):
        git(tmp_path, "status", run=missing)


def test_git_never_prompts():
    assert git_env()["GIT_TERMINAL_PROMPT"] == "0"


def test_app_checkout_needs_a_git_directory_or_file(tmp_path):
    assert app_checkout(tmp_path) is None
    (tmp_path / ".git").write_text("gitdir: elsewhere")
    assert app_checkout(tmp_path) == tmp_path


def test_the_update_result_is_read_once(tmp_path):
    path = tmp_path / "update-result.json"
    path.write_text(json.dumps({"ok": True, "detail": "a → b"}))
    assert read_update_result(path) == {"ok": True, "detail": "a → b"}
    assert not path.exists()
    assert read_update_result(path) is None


def test_an_unreadable_update_result_is_none(tmp_path):
    path = tmp_path / "update-result.json"
    path.write_text("[]")
    assert read_update_result(path) is None


class Rig:
    def __init__(self, tmp_path: Path, loop: asyncio.AbstractEventLoop, **options: Any) -> None:
        self.settings = Settings(data_dir=tmp_path, update_check_seconds=3600)
        self.messages: list[dict[str, Any]] = []
        self.restarts: list[tuple[bool, bool]] = []
        self.status = UpdateStatus(behind=2, checked=True)
        self.refuse: Exception | None = None
        options.setdefault("checkout", tmp_path)
        options.setdefault("head", lambda _checkout: "started")
        self.control = AppControl(
            self.settings,
            self.messages.append,
            loop,
            lambda: {"kept": True, "stale": False},
            self.restart,
            **options,
        )
        self.checked_from: list[str] = []
        self.control._check = self.check

    def check(self, _checkout: Path, _branch: str, running: str) -> UpdateStatus:
        self.checked_from.append(running)
        return self.status

    def restart(self, update: bool, keep: bool) -> None:
        if self.refuse is not None:
            raise self.refuse
        self.restarts.append((update, keep))

    def last(self) -> dict[str, Any]:
        return self.messages[-1]


def run(body, **options):
    def wrapper(tmp_path):
        async def go() -> None:
            await body(Rig(tmp_path, asyncio.get_running_loop(), **options))

        asyncio.run(go())

    wrapper.__name__ = body.__name__
    return wrapper


@run
async def test_the_status_tells_the_page_everything(rig):
    await rig.control.check_now()
    message = rig.last()
    assert message["type"] == "app"
    assert message["update"]["behind"] == 2
    assert message["sessions"] == {"kept": True, "stale": False}
    assert message["restarting"] is False


@run
async def test_every_check_counts_from_the_commit_read_at_start(rig):
    await rig.control.check_now()
    await rig.control.check_now()
    assert rig.checked_from == ["started", "started"]


def test_the_start_commit_is_read_once_as_the_app_starts(tmp_path):
    reads: list[Path] = []

    def head(checkout: Path) -> str:
        reads.append(checkout)
        return "started"

    async def go() -> None:
        rig = Rig(tmp_path, asyncio.get_running_loop(), head=head)
        assert reads == [tmp_path]
        await rig.control.check_now()
        assert reads == [tmp_path], "HEAD moves on; the start commit does not"

    asyncio.run(go())


def test_without_a_checkout_no_start_commit_is_read(tmp_path):
    def head(_checkout: Path) -> str:
        raise AssertionError("there is no checkout to read")

    async def go() -> None:
        AppControl(
            Settings(data_dir=tmp_path),
            lambda _message: None,
            asyncio.get_running_loop(),
            dict,
            lambda *_: None,
            source=lambda: None,
            head=head,
        )

    asyncio.run(go())


@run
async def test_checking_starts_at_once_and_stops(rig):
    rig.control.start()
    await asyncio.sleep(0.05)
    assert rig.last()["update"]["behind"] == 2
    rig.control.stop()


@run
async def test_app_status_answers_the_page(rig):
    await rig.control.handlers()["app_status"]({"type": "app_status"})
    assert rig.last()["type"] == "app"


@run
async def test_an_update_restarts_keeping_the_sessions(rig):
    await rig.control.check_now()
    await rig.control.handlers()["app_restart"]({"update": True})
    assert rig.restarts == [(True, True)]
    assert rig.last()["restarting"] is True
    await rig.control.handlers()["app_restart"]({"update": True})
    assert len(rig.restarts) == 1, "one restart at a time"


@run
async def test_restarting_sessions_asks_for_them_to_end(rig):
    await rig.control.handlers()["app_restart"]({"update": False, "keep_sessions": False})
    assert rig.restarts == [(False, False)]


@run
async def test_a_blocked_update_is_refused(rig):
    rig.status = UpdateStatus(behind=2, blocked="dirty", checked=True)
    await rig.control.check_now()
    with pytest.raises(ValueError, match="dirty"):
        await rig.control.handlers()["app_restart"]({"update": True})
    assert rig.restarts == []


@run
async def test_a_restart_that_cannot_start_says_so(rig):
    rig.refuse = OSError("no helper")
    await rig.control.handlers()["app_restart"]({"update": False})
    assert rig.last()["restarting"] is False
    assert rig.last()["notice"] == {"ok": False, "step": "restart", "detail": "no helper"}


def test_without_a_checkout_there_is_nothing_to_update(tmp_path):
    async def go() -> None:
        messages: list[dict[str, Any]] = []
        control = AppControl(
            Settings(data_dir=tmp_path),
            messages.append,
            asyncio.get_running_loop(),
            dict,
            lambda *_: None,
            source=lambda: tmp_path,
        )
        control.start()
        await control.check_now()
        control.emit_status()
        assert messages[-1]["update"] is None
        assert "not running from a git checkout" in messages[-1]["cannot_update"]
        assert str(tmp_path) in messages[-1]["cannot_update"]
        with pytest.raises(ValueError, match="not running from a checkout"):
            await control.handlers()["app_restart"]({"update": True})

    asyncio.run(go())


@run
async def test_a_checkout_has_nothing_to_say_about_its_install(rig):
    rig.control.emit_status()
    assert rig.last()["cannot_update"] == ""


def test_a_checkout_never_looks_for_its_install(tmp_path):
    def source() -> Path | None:
        raise AssertionError("a checkout needs no install source")

    async def go() -> None:
        AppControl(
            Settings(data_dir=tmp_path),
            lambda _message: None,
            asyncio.get_running_loop(),
            dict,
            lambda *_: None,
            checkout=tmp_path,
            source=source,
            head=lambda _checkout: "",
        )

    asyncio.run(go())


def direct_url(url: str) -> str:
    return json.dumps({"url": url, "dir_info": {}})


def test_the_source_checkout_is_read_from_direct_url(tmp_path):
    (tmp_path / ".git").mkdir()
    assert source_checkout(direct_url(tmp_path.as_uri())) == tmp_path


def test_a_source_that_is_not_a_checkout_is_none(tmp_path):
    """`uv tool install` from a temporary copy, as the window this was found in was."""
    assert source_checkout(direct_url(tmp_path.as_uri())) is None


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "not json",
        "[]",
        json.dumps({"dir_info": {}}),
        direct_url("https://example.invalid/agent-terminal.tar.gz"),
    ],
)
def test_an_unusable_direct_url_is_none(text):
    assert source_checkout(text) is None


def test_installed_from_reads_this_distribution(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()

    class Dist:
        def read_text(self, name: str) -> str | None:
            assert name == "direct_url.json"
            return direct_url(tmp_path.as_uri())

    monkeypatch.setattr(importlib.metadata, "distribution", lambda _name: Dist())
    assert installed_from() == tmp_path


def test_installed_from_without_a_distribution_is_none(monkeypatch):
    def missing(name: str) -> None:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "distribution", missing)
    assert installed_from() is None


def test_cannot_update_names_the_checkout_to_run(tmp_path):
    said = cannot_update(tmp_path)
    assert str(tmp_path / ".venv" / "Scripts" / "agent-terminal.exe") in said


def test_cannot_update_without_a_source_still_says_what_to_do():
    assert "Run it from a checkout's .venv" in cannot_update(None)


def test_the_last_update_is_shown_once(tmp_path):
    (tmp_path / "update-result.json").write_text(json.dumps({"ok": True, "detail": "a → b"}))

    async def go() -> None:
        rig = Rig(tmp_path, asyncio.get_running_loop())
        rig.control.emit_status()
        assert rig.last()["notice"]["detail"] == "a → b"

    asyncio.run(go())
    assert not (tmp_path / "update-result.json").exists()

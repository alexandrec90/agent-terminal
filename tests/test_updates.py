import asyncio
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
    check_for_update,
    git,
    git_env,
    read_update_result,
)


class Git:
    """A `subprocess.run` for git that answers by subcommand."""

    def __init__(self, **answers: str | int) -> None:
        self.answers = {
            "fetch": "",
            "count-behind": "3",
            "count-ahead": "0",
            "branch": "main",
            "status": "",
            **answers,
        }
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        args = argv[1:]
        if args[0] == "rev-list":
            key = "count-behind" if args[-1].startswith("HEAD..") else "count-ahead"
        elif args[0] == "rev-parse":
            key = "branch"
        else:
            key = args[0]
        answer = self.answers[key]
        if isinstance(answer, int):
            return subprocess.CompletedProcess(argv, answer, "", f"{key} failed")
        return subprocess.CompletedProcess(argv, 0, answer + "\n", "")


def test_behind_and_free_to_update(tmp_path):
    status = check_for_update(tmp_path, "main", Git())
    assert status == UpdateStatus(behind=3, checked=True)


def test_the_fetch_asks_for_the_branch_alone(tmp_path):
    run = Git()
    check_for_update(tmp_path, "main", run)
    assert run.calls[0] == ["git", "fetch", "--quiet", "origin", "main"]


@pytest.mark.parametrize(
    ("answers", "reason"),
    [
        ({"branch": "feature"}, "on feature, not main"),
        ({"count-ahead": "2"}, "2 commit(s) that are not on origin/main"),
        ({"status": " M app.py"}, "uncommitted changes"),
    ],
)
def test_a_checkout_that_cannot_fast_forward_says_why(tmp_path, answers, reason):
    status = check_for_update(tmp_path, "main", Git(**answers))
    assert reason in status.blocked
    assert status.behind == 3


def test_a_failed_fetch_is_reported_not_raised(tmp_path):
    status = check_for_update(tmp_path, "main", Git(fetch=128))
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
        self.control = AppControl(
            self.settings,
            self.messages.append,
            loop,
            lambda: {"kept": True, "stale": False},
            self.restart,
            **options,
        )
        self.control._check = lambda _checkout, _branch: self.status

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
        )
        control.start()
        await control.check_now()
        control.emit_status()
        assert messages[-1]["update"] is None
        with pytest.raises(ValueError, match="not running from a checkout"):
            await control.handlers()["app_restart"]({"update": True})

    asyncio.run(go())


def test_the_last_update_is_shown_once(tmp_path):
    (tmp_path / "update-result.json").write_text(json.dumps({"ok": True, "detail": "a → b"}))

    async def go() -> None:
        rig = Rig(tmp_path, asyncio.get_running_loop())
        rig.control.emit_status()
        assert rig.last()["notice"]["detail"] == "a → b"

    asyncio.run(go())
    assert not (tmp_path / "update-result.json").exists()

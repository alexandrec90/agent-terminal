import json
import subprocess
from pathlib import Path
from typing import Any

from agent_terminal import relaunch as module
from agent_terminal.instance import acquire_instance_lock
from agent_terminal.relaunch import (
    UpdateResult,
    apply_update,
    head_commit,
    launch_relaunch,
    main,
    parse_args,
    relaunch_argv,
    update_detail,
    update_steps,
    wait_for_exit,
)
from agent_terminal.settings import Settings


class Steps:
    """A `subprocess.run` that fails the step whose first word is in `failing`."""

    def __init__(self, failing: str = "", output: str = "ok") -> None:
        self.failing = failing
        self.output = output
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[1] == self.failing:
            return subprocess.CompletedProcess(argv, 1, "", f"{argv[1]} refused")
        return subprocess.CompletedProcess(argv, 0, self.output, "")


def test_wait_for_exit_returns_once_the_lock_is_free(tmp_path):
    lock = tmp_path / "app.lock"
    assert wait_for_exit(lock, timeout=1)


def test_wait_for_exit_gives_up_on_an_app_that_stays(tmp_path):
    lock = tmp_path / "app.lock"
    held = acquire_instance_lock(lock)
    try:
        assert not wait_for_exit(lock, timeout=0.5, sleep=lambda _s: None)
    finally:
        assert held is not None
        held.close()


def test_the_steps_fast_forward_then_sync():
    assert update_steps("main", "C:/uv.exe") == [
        ["git", "pull", "--ff-only", "origin", "main"],
        ["C:/uv.exe", "sync", "--all-extras"],
    ]


def test_an_update_runs_every_step_and_logs_them(tmp_path):
    run = Steps()
    log_file = tmp_path / "update.log"
    result = apply_update(tmp_path, "main", log_file, run=run, uv="uv")
    assert result.ok
    assert [call[1] for call in run.calls] == ["pull", "sync"]
    assert "$ git pull --ff-only origin main" in log_file.read_text(encoding="utf-8")


def test_an_update_stops_at_the_first_failure(tmp_path):
    run = Steps(failing="pull")
    log_file = tmp_path / "update.log"
    result = apply_update(tmp_path, "main", log_file, run=run, uv="uv")
    assert not result.ok
    assert result.step == "git pull --ff-only origin main"
    assert result.detail == "pull refused"
    assert len(run.calls) == 1
    assert "pull refused" in log_file.read_text(encoding="utf-8")


def test_an_update_without_uv_fails_at_the_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    result = apply_update(tmp_path, "main", tmp_path / "update.log", run=Steps())
    assert not result.ok
    assert result.detail == "uv is not on PATH"


def test_a_step_that_cannot_run_fails_the_update(tmp_path):
    def missing(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("git")

    result = apply_update(tmp_path, "main", tmp_path / "update.log", run=missing, uv="uv")
    assert result == UpdateResult(False, "git pull --ff-only origin main", "git", result.at)


def test_head_commit(tmp_path):
    assert head_commit(tmp_path, run=Steps(output="abc123\n")) == "abc123"
    assert head_commit(tmp_path, run=Steps(failing="rev-parse")) == ""


def test_relaunch_runs_the_package():
    assert relaunch_argv("py") == ["py", "-m", "agent_terminal"]


def test_launch_relaunch_starts_this_module(tmp_path, monkeypatch):
    started: list[list[str]] = []
    monkeypatch.setattr(module, "spawn_detached", lambda argv, _cwd: started.append(argv) or 3)
    assert launch_relaunch(Settings(data_dir=tmp_path), tmp_path, update=True) == 3
    assert started[0][1:] == [
        "-m",
        "agent_terminal.relaunch",
        "--checkout",
        str(tmp_path),
        "--update",
    ]


def test_parse_args(tmp_path):
    args = parse_args(["--checkout", str(tmp_path)])
    assert args.checkout == Path(tmp_path)
    assert args.update is False


def test_main_updates_records_and_starts_the_app(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TERMINAL_DATA_DIR", str(tmp_path))
    started: list[list[str]] = []
    monkeypatch.setattr(module, "spawn_detached", lambda argv, _cwd: started.append(argv) or 1)
    monkeypatch.setattr(
        module,
        "apply_update",
        lambda *_: UpdateResult(True, "uv sync --all-extras", "", 1.0),
    )
    heads = iter(["aaa", "bbb"])
    monkeypatch.setattr(module, "head_commit", lambda _checkout: next(heads))
    assert main(["--checkout", str(tmp_path), "--update"]) == 0
    record = json.loads((tmp_path / "update-result.json").read_text(encoding="utf-8"))
    assert record["ok"] is True
    assert record["detail"] == "aaa → bbb"
    assert started[0][1:] == ["-m", "agent_terminal"]


def test_an_update_with_nothing_to_pull_names_the_commit_it_restarted_on():
    assert update_detail("bbb", "bbb") == "now running bbb"


def test_an_update_that_pulled_names_both_commits():
    assert update_detail("aaa", "bbb") == "aaa → bbb"


def test_main_without_update_just_starts_the_app(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TERMINAL_DATA_DIR", str(tmp_path))
    started: list[list[str]] = []
    monkeypatch.setattr(module, "spawn_detached", lambda argv, _cwd: started.append(argv) or 1)
    assert main(["--checkout", str(tmp_path)]) == 0
    assert not (tmp_path / "update-result.json").exists()
    assert len(started) == 1


def test_main_starts_nothing_while_the_app_stays(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TERMINAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(module, "wait_for_exit", lambda _lock: False)
    started: list[list[str]] = []
    monkeypatch.setattr(module, "spawn_detached", lambda argv, _cwd: started.append(argv) or 1)
    assert main(["--checkout", str(tmp_path)]) == 1
    assert started == []

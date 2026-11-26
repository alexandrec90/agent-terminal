import json
from pathlib import Path

import pytest

from agent_terminal import retire
from agent_terminal.retire import (
    RetireError,
    Retirement,
    failure_reason,
    parse_plan,
    read_retired,
    retire_argv,
)
from agent_terminal.settings import Settings

# Captured from devkit's scripts/retire-project.py (bccc4a3), run on this machine.
RADIO_RETIRE_DRY_RUN = ["  retire radio will:", "    unplug from the registry", "  nothing changed"]
RADIO_RESTORE_DRY_RUN = [
    "  restore radio will:",
    "    plug into the registry",
    "  NOTE    this machine has no record of retiring it -- nothing local is started; "
    "`collectors.py run-here` and `docker compose up -d` do it by hand",
    "  nothing changed",
]
DEVKIT_REFUSED = [
    "ERROR   devkit is the checkout every other one is driven from -- it cannot be retired"
]
# What `act` prints when the registry step fails: `execute`'s reason, then its advice.
REGISTRY_REFUSED = [
    "  unplug from the registry",
    "ERROR   unplug from the registry: exit 1 -- radio has 3 unpushed commits",
    "        fix that and run the same command again; done steps are skipped",
]
STEP_FAILED = [
    "  unplug from the registry",
    "  remove its containers (volumes kept)",
    "ERROR   remove its containers (volumes kept): exit 1 -- docker daemon not running",
    "        fix that and run the same command again; done steps are skipped",
]


class Script:
    """Stands in for the script: answers each run with lines and an exit code."""

    def __init__(self, lines: list[str], code: int = 0) -> None:
        self.lines = lines
        self.code = code
        self.calls: list[tuple[list[str], Path, dict]] = []

    def __call__(self, argv, cwd, env, on_line) -> int:
        self.calls.append((argv, cwd, dict(env)))
        for line in self.lines:
            on_line(line)
        return self.code


def settings(tmp_path: Path) -> Settings:
    devkit = tmp_path / "devkit"
    (devkit / "scripts").mkdir(parents=True)
    (devkit / retire.SCRIPT).write_text("", encoding="utf-8")
    return Settings(devkit_dir=devkit, data_dir=tmp_path / "data", python_command="py")


def test_a_dry_run_is_planned_and_an_apply_is_confirmed():
    devkit = Path("C:/devkit")
    assert retire_argv("py", devkit, "retire", "radio", apply=False) == [
        "py",
        str(devkit / retire.SCRIPT),
        "retire",
        "radio",
        "--dry-run",
    ]
    assert retire_argv("py", devkit, "retire", "radio", apply=True, force=True)[-2:] == [
        "--yes",
        "--force",
    ]


def test_restore_never_forces():
    """The script has no --force for restore; passing one would fail the parse."""
    argv = retire_argv("py", Path("d"), "restore", "radio", apply=True, force=True)
    assert "--force" not in argv


def test_the_plan_is_the_steps_and_notes_the_script_printed():
    plan = parse_plan(RADIO_RESTORE_DRY_RUN)
    assert plan.steps == ["plug into the registry"]
    assert plan.notes[0].startswith("this machine has no record of retiring it")
    assert parse_plan(RADIO_RETIRE_DRY_RUN).to_dict() == {
        "steps": ["unplug from the registry"],
        "notes": [],
    }


def test_nothing_after_the_plan_counts_as_a_step():
    lines = [*RADIO_RETIRE_DRY_RUN, "    an indented line from something else"]
    assert parse_plan(lines).steps == ["unplug from the registry"]


def test_a_plan_with_no_steps_is_empty():
    assert (
        parse_plan(["  retire x will:", "  NOTE    not in the registry", "  nothing changed"]).steps
        == []
    )


def test_the_reason_is_the_scripts_error_line():
    assert failure_reason(DEVKIT_REFUSED).startswith("devkit is the checkout")
    assert failure_reason(REGISTRY_REFUSED) == (
        "unplug from the registry: exit 1 -- radio has 3 unpushed commits"
    )


def test_without_an_error_line_the_reason_is_the_last_thing_said():
    assert failure_reason(["Traceback (most recent call last):", "KeyError: 'x'", ""]) == (
        "KeyError: 'x'"
    )
    assert failure_reason([]) == ""


def test_a_refused_plan_refuses_the_delete(tmp_path):
    script = Script(DEVKIT_REFUSED, code=2)
    with pytest.raises(RetireError, match="refuses devkit: devkit is the checkout"):
        Retirement(settings(tmp_path), script).plan("devkit")


def test_the_plan_runs_in_devkit_without_the_apps_virtualenv(tmp_path, monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "C:/app/.venv")
    script = Script(RADIO_RETIRE_DRY_RUN)
    config = settings(tmp_path)
    plan = Retirement(config, script).plan("radio")
    assert plan.steps == ["unplug from the registry"]
    argv, cwd, env = script.calls[0]
    assert argv[-1] == "--dry-run"
    assert cwd == config.devkit_root
    assert "VIRTUAL_ENV" not in env


def test_an_apply_keeps_everything_in_the_log(tmp_path):
    script = Script(["  retire radio will:", "    unplug from the registry", "  unplug"])
    retirement = Retirement(settings(tmp_path), script)
    retirement.apply("retire", "radio")
    kept = retirement.log_file.read_text(encoding="utf-8")
    assert "retire radio --yes" in kept
    assert "  unplug" in kept
    assert kept.endswith("exit code 0\n")


def test_a_registry_refusal_can_be_forced(tmp_path):
    retirement = Retirement(settings(tmp_path), Script(REGISTRY_REFUSED, code=1))
    with pytest.raises(RetireError) as caught:
        retirement.apply("retire", "radio")
    assert caught.value.forceable
    assert "radio has 3 unpushed commits" in str(caught.value)
    assert str(retirement.log_file) in str(caught.value)


def test_a_forced_retire_that_fails_again_is_not_offered_force_again(tmp_path):
    retirement = Retirement(settings(tmp_path), Script(REGISTRY_REFUSED, code=1))
    with pytest.raises(RetireError) as caught:
        retirement.apply("retire", "radio", force=True)
    assert not caught.value.forceable


def test_another_failed_step_cannot_be_forced(tmp_path):
    retirement = Retirement(settings(tmp_path), Script(STEP_FAILED, code=1))
    with pytest.raises(RetireError, match="docker daemon not running") as caught:
        retirement.apply("retire", "radio")
    assert not caught.value.forceable


def test_a_failed_restore_cannot_be_forced(tmp_path):
    lines = ["ERROR   unplug from the registry: exit 1 -- odd"]
    retirement = Retirement(settings(tmp_path), Script(lines, code=1))
    with pytest.raises(RetireError) as caught:
        retirement.apply("restore", "radio")
    assert not caught.value.forceable


def test_without_the_script_it_is_not_available(tmp_path):
    retirement = Retirement(Settings(devkit_dir=tmp_path / "nowhere"))
    assert not retirement.available()
    assert "retire-project.py is not at" in retirement.missing()


@pytest.mark.parametrize("name", ["radio", "ibkr_trader", "d2-trader"])
def test_a_project_name_is_taken(name):
    assert Retirement.takes(name)


@pytest.mark.parametrize("name", ["--force", "-x", "has space", "", "dot.name"])
def test_a_name_that_could_be_an_option_is_not(name):
    assert not Retirement.takes(name)


def test_the_retired_list_is_the_scripts_record(tmp_path):
    record = tmp_path / "retired-projects.json"
    record.write_text(
        json.dumps(
            {
                "tvshell": {
                    "collector": None,
                    "services": [],
                    "tasks": [],
                    "retired_at": "2026-11-25T18:00:00",
                },
                "radio": {
                    "collector": "run",
                    "services": ["app"],
                    "tasks": [],
                    "retired_at": "2026-11-24T09:00:00",
                },
            }
        ),
        encoding="utf-8",
    )
    assert read_retired(record) == [
        {"name": "radio", "retired_at": "2026-11-24T09:00:00"},
        {"name": "tvshell", "retired_at": "2026-11-25T18:00:00"},
    ]


@pytest.mark.parametrize(
    "text", [None, "not json", "[1, 2]", '{"radio": "text"}', '{"--force": {}}']
)
def test_an_unreadable_record_is_nothing_retired(tmp_path, text):
    record = tmp_path / "retired-projects.json"
    if text is not None:
        record.write_text(text, encoding="utf-8")
    assert read_retired(record) == []


def test_a_record_without_a_time_still_lists_the_project(tmp_path):
    record = tmp_path / "retired-projects.json"
    record.write_text('{"radio": {}}', encoding="utf-8")
    assert read_retired(record) == [{"name": "radio", "retired_at": ""}]


def test_the_record_is_read_from_devkits_logs(tmp_path):
    config = settings(tmp_path)
    assert Retirement(config).record == config.devkit_root / "logs" / "retired-projects.json"

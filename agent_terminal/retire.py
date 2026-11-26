"""Retiring a project before its folder is deleted, and restoring one, through devkit.

Deleting a folder leaves the project registered with devkit, its collector running, its
scheduled tasks firing and its containers up. devkit's `scripts/retire-project.py` takes
all of that down, and puts it back on `restore`, which also clones the project if its
folder is gone. It never touches the folder itself: the app still sends that to the
Recycle Bin.

The script is driven through its command line, as a person would: a `--dry-run` says what
a retire will do, for the confirmation, and its record of what this machine retired
(`logs/retired-projects.json` in devkit's checkout) is the restore list.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_terminal.scaffold import NAME, RunLines, run_lines, script_env
from agent_terminal.settings import Settings

SCRIPT = Path("scripts") / "retire-project.py"
RECORD = Path("logs") / "retired-projects.json"
RETIRE = "retire"
RESTORE = "restore"
# What `execute` names the registry step when it fails: the one step `--force` passes.
REGISTRY_STEP = "unplug from the registry"
# `confirmed` prints the plan as `  <verb> <name> will:`, then each step indented four
# spaces and each note after `  NOTE`.
PLAN_HEADER = " will:"
STEP_INDENT = "    "
NOTE_PREFIX = "NOTE"
ERROR_PREFIX = "ERROR"


class RetireError(Exception):
    """The script refused or failed. `forceable` when `--force` would get past it."""

    def __init__(self, message: str, forceable: bool = False) -> None:
        super().__init__(message)
        self.forceable = forceable


@dataclass
class RetirePlan:
    """What a retire would do, in the script's words."""

    steps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, list[str]]:
        return {"steps": self.steps, "notes": self.notes}


def retire_argv(
    python: str, devkit: Path, verb: str, name: str, *, apply: bool, force: bool = False
) -> list[str]:
    """The command for one run. `name` is already checked against `NAME`, so it cannot
    be read as an option."""
    argv = [python, str(devkit / SCRIPT), verb, name, "--yes" if apply else "--dry-run"]
    if force and verb == RETIRE:
        argv.append("--force")
    return argv


def parse_plan(lines: Iterable[str]) -> RetirePlan:
    """The steps and notes a `--dry-run` printed."""
    plan = RetirePlan()
    in_plan = False
    for line in lines:
        text = line.strip()
        if line.rstrip().endswith(PLAN_HEADER):
            in_plan = True
        elif text.startswith(NOTE_PREFIX):
            plan.notes.append(text.removeprefix(NOTE_PREFIX).strip())
        elif in_plan and line.startswith(STEP_INDENT) and text:
            plan.steps.append(text)
        else:
            in_plan = False
    return plan


def failure_reason(lines: list[str]) -> str:
    """What the script said went wrong: its first `ERROR` line, else its last line."""
    for line in lines:
        text = line.strip()
        if text.startswith(ERROR_PREFIX):
            return text.removeprefix(ERROR_PREFIX).strip()
    said = [line.strip() for line in lines if line.strip()]
    return said[-1] if said else ""


def read_retired(record: Path) -> list[dict[str, str]]:
    """Each project this machine has retired, by name, with when. Never raises: an
    unreadable record is the script's own "nothing retired here"."""
    try:
        raw = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    return [
        {"name": name, "retired_at": str(entry.get("retired_at", ""))}
        for name, entry in sorted(raw.items())
        if isinstance(name, str) and NAME.match(name) and isinstance(entry, dict)
    ]


class Retirement:
    """Runs `retire-project.py` for the dialog. Blocking: call it off the event loop."""

    def __init__(self, settings: Settings, run: RunLines = run_lines) -> None:
        self._settings = settings
        self._run = run

    @property
    def _devkit(self) -> Path:
        return self._settings.devkit_root

    @property
    def log_file(self) -> Path:
        """The last retire or restore, whole."""
        return self._settings.data_dir / "retire-project.log"

    @property
    def record(self) -> Path:
        return self._devkit / RECORD

    def available(self) -> bool:
        return (self._devkit / SCRIPT).is_file()

    def missing(self) -> str:
        return f"devkit's {SCRIPT.as_posix()} is not at {self._devkit}"

    @staticmethod
    def takes(name: str) -> bool:
        """Whether `name` can be handed to the script at all."""
        return bool(NAME.match(name))

    def _lines(self, argv: list[str], keep: Callable[[str], None]) -> tuple[int, list[str]]:
        said: list[str] = []

        def on_line(line: str) -> None:
            said.append(line)
            keep(line)

        code = self._run(argv, self._devkit, script_env(os.environ), on_line)
        return code, said

    def plan(self, name: str) -> RetirePlan:
        """What retiring `name` would do. Raises `RetireError` when the script refuses."""
        argv = retire_argv(self._settings.python_command, self._devkit, RETIRE, name, apply=False)
        code, said = self._lines(argv, lambda _line: None)
        if code != 0:
            raise RetireError(f"retire-project.py refuses {name}: {failure_reason(said)}")
        return parse_plan(said)

    def apply(self, verb: str, name: str, *, force: bool = False) -> None:
        """Retire or restore `name`, keeping everything the script prints in `log_file`.
        Raises `RetireError` with the script's reason when it fails."""
        argv = retire_argv(
            self._settings.python_command, self._devkit, verb, name, apply=True, force=force
        )
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("w", encoding="utf-8") as kept:
            kept.write(" ".join(argv) + "\n\n")

            def keep(line: str) -> None:
                kept.write(line + "\n")
                kept.flush()

            code, said = self._lines(argv, keep)
            kept.write(f"\nexit code {code}\n")
        if code != 0:
            reason = failure_reason(said) or f"exit {code}"
            raise RetireError(
                f"retire-project.py could not {verb} {name}: {reason} "
                f"(everything it said is in {self.log_file})",
                forceable=verb == RETIRE and not force and reason.startswith(REGISTRY_STEP),
            )

    def retired(self) -> list[dict[str, str]]:
        return read_retired(self.record)


def plan_message(plan: RetirePlan | None, note: str = "") -> dict[str, Any]:
    """The `retire` field of a deletion's description: None when nothing will be retired,
    with `note` saying why."""
    if plan is None:
        return {"steps": [], "notes": [note] if note else []}
    return plan.to_dict()

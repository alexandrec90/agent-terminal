"""A new project from devkit's templates, from the new-group dialog.

devkit's `scripts/new-project.py` does the work, as the VS Code task "Project: New from
devkit" did: templates, the vendored harness, ports, git, and (if asked) a public GitHub
repository. This runs it with the dialog's answers, streams what it prints back to the
page, and keeps the whole run in a log file, overwritten each time.

The presets are the script's own, read from its `--help`: a list kept here would go
stale the first time devkit added one.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from agent_terminal import repos
from agent_terminal.desktop import Emit
from agent_terminal.gitinfo import NO_WINDOW
from agent_terminal.settings import Settings

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]
# Runs `argv` in `cwd` with `env`, hands each line it prints to `on_line`, and returns
# its exit code. Raises `OSError` when it cannot start.
RunLines = Callable[[list[str], Path, Mapping[str, str], Callable[[str], None]], int]

SCRIPT = Path("scripts") / "new-project.py"
# What devkit's own `validate_name` accepts: the name becomes a directory, a Python
# package and COMPOSE_PROJECT_NAME. Checked here too so a bad name is refused before
# a process starts, and so nothing reaching the argv can be read as an option.
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
PRESET_CHOICES = re.compile(r"--preset \{([^}]*)\}")
MAX_DESCRIPTION = 200

# What each preset builds, in words. One devkit adds later shows by its name alone.
PRESET_LABELS = {
    "bare": "bare: no Docker, no database; just the harness and CI",
    "service": "service: Docker, an app, Postgres and Alembic",
    "service-redis": "service-redis: the above plus Redis",
    "data": "data: Postgres and the data-lake archive seam",
    "fullstack": "fullstack: service-redis plus a Vite frontend",
    "desktop": "desktop: a C#/WPF Windows app with installer and auto-update",
    "desktop-static": "desktop-static: the same app and installer, without the updater",
    "desktop-web": "desktop-web: a TypeScript Electron app with installer and auto-update",
}
DEFAULT_PRESET = "service"


def parse_presets(help_text: str) -> list[str]:
    """The `--preset` choices in the script's `--help`. Raises `ValueError` when there
    are none to read, which means the script is not the one this was written against."""
    found = PRESET_CHOICES.search(help_text)
    if not found:
        raise ValueError("new-project.py does not list its presets in --help")
    return [choice.strip() for choice in found.group(1).split(",") if choice.strip()]


def preset_options(presets: list[str]) -> list[dict[str, str]]:
    """The dialog's choices: the default first, then the script's order."""
    ordered = sorted(presets, key=lambda preset: preset != DEFAULT_PRESET)
    return [{"value": p, "label": PRESET_LABELS.get(p, p)} for p in ordered]


def checked_name(root: Path, name: object) -> str:
    """The project name, if a new project can have it. Raises `ValueError` with why not."""
    if not isinstance(name, str) or not NAME.match(name):
        raise ValueError(
            "Use letters, digits, '-' and '_', starting with a letter or digit: the name "
            "becomes a folder, a Python package and a Docker project."
        )
    if name.lower() in repos.local_names(root):
        raise ValueError(f"{name} is already in {root}")
    return name


def checked_description(description: object) -> str:
    """One line of text for README.md and CLAUDE.md."""
    if not isinstance(description, str):
        raise ValueError("the description must be text")
    line = " ".join(description.split())
    if len(line) > MAX_DESCRIPTION:
        raise ValueError(f"keep the description under {MAX_DESCRIPTION} characters")
    return line


def new_project_argv(
    python: str,
    devkit: Path,
    parent: Path,
    choice: Mapping[str, Any],
) -> list[str]:
    """The command for one run. `choice` is already checked. Every value follows its
    flag, so a description starting with "-" is still a value."""
    return [
        python,
        str(devkit / SCRIPT),
        choice["name"],
        f"--description={choice['description']}",
        f"--preset={choice['preset']}",
        f"--parent={parent}",
        "--remote" if choice["remote"] else "--no-remote",
        "--yes" if choice["apply"] else "--dry-run",
    ]


def script_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The app's environment, less its own virtualenv: the script runs `uv` for the new
    project, which would otherwise take this app's venv for that project's."""
    env = {key: value for key, value in environ.items() if key != "VIRTUAL_ENV"}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def run_lines(
    argv: list[str], cwd: Path, env: Mapping[str, str], on_line: Callable[[str], None]
) -> int:
    """Blocking, so call it off the event loop. Nothing is asked of the user: the script
    reads no input, and stdin is closed so nothing it starts can wait on it."""
    with subprocess.Popen(  # noqa: S603 - argv built by new_project_argv from checked values
        argv,
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        creationflags=NO_WINDOW,
    ) as process:
        assert process.stdout is not None  # noqa: S101 - stdout=PIPE above guarantees it
        for line in process.stdout:
            on_line(line.rstrip("\r\n"))
        return process.wait()


class NewProjectRequests:
    """Answers the dialog's new-project panel. One run at a time: two would race for
    the same port slot in devkit's registry."""

    def __init__(
        self,
        settings: Settings,
        emit: Emit,
        loop: asyncio.AbstractEventLoop,
        send_projects: Callable[[], None],
        run: RunLines = run_lines,
    ) -> None:
        self._settings = settings
        self._emit = emit
        self._loop = loop
        self._send_projects = send_projects
        self._run = run
        self._running = False

    def handlers(self) -> dict[str, Handler]:
        """The message types answered here."""
        return {
            "list_presets": self._on_list_presets,
            "new_project": self._on_new_project,
        }

    @property
    def _devkit(self) -> Path:
        return self._settings.devkit_root

    @property
    def log_file(self) -> Path:
        """The last run, whole."""
        return self._settings.data_dir / "new-project.log"

    def _script(self) -> Path:
        script = self._devkit / SCRIPT
        if not script.is_file():
            raise ValueError(f"devkit's {SCRIPT.as_posix()} is not at {self._devkit}")
        return script

    def _read_presets(self) -> list[str]:
        self._script()
        lines: list[str] = []
        argv = [self._settings.python_command, str(self._devkit / SCRIPT), "--help"]
        code = self._run(argv, self._devkit, script_env(os.environ), lines.append)
        if code != 0:
            raise ValueError("\n".join(lines[-5:]) or f"new-project.py --help exited {code}")
        return parse_presets("\n".join(lines))

    async def _on_list_presets(self, _message: dict[str, Any]) -> None:
        try:
            presets = await self._loop.run_in_executor(None, self._read_presets)
        except (ValueError, OSError) as exc:
            self._emit({"type": "presets", "presets": [], "error": str(exc)})
            return
        self._emit({"type": "presets", "presets": preset_options(presets), "error": ""})

    def _checked(self, message: Mapping[str, Any], presets: list[str]) -> dict[str, Any]:
        preset = message.get("preset")
        if preset not in presets:
            raise ValueError(f"not a preset new-project.py has: {preset!r}")
        return {
            "name": checked_name(self._settings.projects_root, message.get("name")),
            "description": checked_description(message.get("description", "")),
            "preset": preset,
            "remote": message.get("remote") is True,
            "apply": message.get("apply") is True,
        }

    def _say(self, line: str) -> None:
        self._loop.call_soon_threadsafe(self._emit, {"type": "new_project_output", "line": line})

    def _create(self, message: Mapping[str, Any]) -> dict[str, Any]:
        """Runs the script, keeping everything it printed in the log. The answer is the
        `new_project_done` message, less its type."""
        choice = self._checked(message, self._read_presets())
        root = self._settings.projects_root
        root.mkdir(parents=True, exist_ok=True)
        argv = new_project_argv(self._settings.python_command, self._devkit, root, choice)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("w", encoding="utf-8") as kept:
            kept.write(" ".join(argv) + "\n\n")

            def on_line(line: str) -> None:
                kept.write(line + "\n")
                kept.flush()
                self._say(line)

            code = self._run(argv, self._devkit, script_env(os.environ), on_line)
            kept.write(f"\nexit code {code}\n")
        target = root / choice["name"]
        if code != 0:
            raise ValueError(f"new-project.py exited {code}; everything it said is in {self.log_file}")
        if choice["apply"] and not target.is_dir():
            raise ValueError(f"new-project.py finished, but {target} is not there")
        return {"applied": choice["apply"], "path": str(target) if choice["apply"] else ""}

    async def _on_new_project(self, message: dict[str, Any]) -> None:
        reply: dict[str, Any] = {
            "type": "new_project_done",
            "name": message.get("name"),
            "applied": False,
            "path": "",
            "error": "",
            "log": str(self.log_file),
        }
        if self._running:
            self._emit({**reply, "error": "A new project is already being made."})
            return
        self._running = True
        try:
            done = await self._loop.run_in_executor(None, self._create, message)
        except (ValueError, OSError) as exc:
            self._emit({**reply, "error": str(exc)})
            return
        finally:
            self._running = False
        if done["applied"]:
            log.info("created project %s", done["path"])
            self._send_projects()
        self._emit({**reply, **done})

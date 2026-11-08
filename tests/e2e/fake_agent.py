"""Stands in for an agent CLI in the end-to-end tests.

Speaks the terminal side of what the real ones do: asks whether the terminal knows OSC
7501, reports status with it, and records how it was launched. Each line of input is a
command, so a test can type `ask`, `finish` or `quit` into a tab and watch the tab react.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ESC = "\x1b"
ST = f"{ESC}\\"
REPLY_TIMEOUT_SECONDS = 5


def emit(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def status(state: str) -> None:
    emit(f"{ESC}]7501;state={state}:app=fake{ST}")


def query_supported() -> bool:
    """Ask the terminal, as Claude does, reading the reply raw from the console."""
    if sys.platform != "win32":
        return False
    import msvcrt

    emit(f"{ESC}]7501;?{ST}")
    deadline = time.monotonic() + REPLY_TIMEOUT_SECONDS
    received = ""
    while time.monotonic() < deadline:
        if msvcrt.kbhit():
            received += msvcrt.getwch()
            if received.endswith(ST):
                return "7501;?" in received
        else:
            time.sleep(0.01)
    return False


def record_launch() -> None:
    path = os.environ.get("FAKE_AGENT_LOG")
    if path:
        with Path(path).open("a", encoding="utf-8") as log:
            line = {"argv": sys.argv[1:], "cwd": os.getcwd(), "pid": os.getpid()}
            log.write(json.dumps(line) + "\n")


def trust_records() -> dict[str, dict[str, bool]]:
    path = Path(os.environ["AGENT_TERMINAL_CLAUDE_CONFIG_FILE"])
    return json.loads(path.read_text(encoding="utf-8")).get("projects", {})


def is_trusted() -> bool:
    here = Path.cwd()
    known = {Path(path) for path, entry in trust_records().items() if entry.get("trusted", True)}
    return here in known or bool(known & set(here.parents))


def record_trust() -> None:
    path = Path(os.environ["AGENT_TERMINAL_CLAUDE_CONFIG_FILE"])
    projects = trust_records()
    projects[Path.cwd().as_posix()] = {"hasTrustDialogAccepted": True}
    path.write_text(json.dumps({"projects": projects}), encoding="utf-8")


def pass_trust_screen() -> bool:
    """As the real agents: nothing is reported until it is answered, and `no` leaves."""
    emit("Is this a project you trust? (yes/no)\r\n")
    if sys.stdin.readline().strip() != "yes":
        status("clear")
        return False
    record_trust()
    return True


def main() -> int:
    record_launch()
    emit(f"{ESC}]0;fake session{ESC}\\")
    if not query_supported():
        emit("no status support\r\n")
    if not is_trusted():
        if "--worktree" in sys.argv[1:]:
            # `claude -w` refuses rather than asking; the text is Claude Code 2.1.318's.
            emit("Error creating worktree: Workspace trust not yet accepted.\r\n")
            return 1
        if not pass_trust_screen():
            return 1
    status("idle")
    emit("fake agent ready\r\n")
    for line in sys.stdin:
        command = line.strip()
        if command == "ask":
            status("working")
            status("blocked:kind=permission")
        elif command == "finish":
            status("working")
            status("done")
        elif command == "work":
            status("working")
        elif command == "quit":
            status("working")
            emit(f"{ESC}]7501;state=clear{ST}")
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

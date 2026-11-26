"""Stands in for an agent CLI in the end-to-end tests.

Speaks the terminal side of what the real ones do: asks whether the terminal knows OSC
7501, reports status with it, and records how it was launched. Each line of input is a
command, so a test can type `ask`, `finish`, `mouse` or `quit` into a tab and watch the tab react.
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
PROMPT = "\N{HEAVY RIGHT-POINTING ANGLE QUOTATION MARK ORNAMENT}"


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
    """As Claude Code 2.1.332: trust reaches down into a folder, but not across the root
    of a repository below the trusted one."""
    known = {Path(path) for path, entry in trust_records().items() if entry.get("trusted", True)}
    for candidate in (Path.cwd(), *Path.cwd().parents):
        if candidate in known:
            return True
        if (candidate / ".git").exists():
            return False
    return False


def record_trust() -> None:
    path = Path(os.environ["AGENT_TERMINAL_CLAUDE_CONFIG_FILE"])
    projects = trust_records()
    projects[Path.cwd().as_posix()] = {"hasTrustDialogAccepted": True}
    path.write_text(json.dumps({"projects": projects}), encoding="utf-8")


def pass_trust_screen(reports: bool) -> bool:
    """As Claude Code 2.1.332: the screen reports itself as `blocked` to a terminal that
    answered the status query, and `no` leaves."""
    if reports:
        # base64 of "trust this folder", as the real one sends it.
        status("blocked:kind=permission:msg=dHJ1c3QgdGhpcyBmb2xkZXI=")
    emit("Is this a project you trust? (yes/no)\r\n")
    if sys.stdin.readline().strip() != "yes":
        status("clear")
        return False
    record_trust()
    return True


def compose() -> None:
    """Claude Code 2.1.331's input box: a rule, the prompt line, a rule, a footer, and the
    cursor put back on the prompt. No background is painted, as Claude paints none."""
    rule = "─" * 60
    emit(f"{rule}\r\n{PROMPT}\N{NO-BREAK SPACE}{ESC}[2mTry something{ESC}[22m\r\n{rule}\r\n")
    emit(f"  ⏵⏵ bypass permissions on{ESC}[2A{ESC}[3G")


def main() -> int:
    record_launch()
    emit(f"{ESC}]0;fake session{ESC}\\")
    supported = query_supported()
    if not supported:
        emit("no status support\r\n")
    if not is_trusted():
        if "--worktree" in sys.argv[1:]:
            # `claude -w` refuses rather than asking; the text is Claude Code 2.1.318's.
            emit("Error creating worktree: Workspace trust not yet accepted.\r\n")
            return 1
        if not pass_trust_screen(supported):
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
        elif command == "compose":
            compose()
        elif command == "mouse":
            # Mouse tracking with SGR reports, as Claude Code's fullscreen view asks for.
            emit(f"{ESC}[?1000h{ESC}[?1006h")
        elif command == "quit":
            status("working")
            emit(f"{ESC}]7501;state=clear{ST}")
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

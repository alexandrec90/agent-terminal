"""How each agent is launched, resumed and found again on disk.

A tab's process is PowerShell 7 with the agent run inside it, so leaving the agent
drops to a shell in the same place rather than closing the tab.
"""

from __future__ import annotations

import base64
import itertools
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_terminal.settings import Settings
from agent_terminal.store import Tab

CLAUDE = "claude"
CODEX = "codex"
AGENTS = (CLAUDE, CODEX)

# Set by a parent terminal or Claude session, and false for a tab's process. Started under
# a Claude session's identity, Claude writes no transcript, so the tab could never resume;
# the session-kind one also reads as "background worker" and stops status reporting. The
# user's own `CLAUDE_CODE_*` settings are not in this list and pass through.
INHERITED_ENV_TO_DROP = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_PID",
        "CLAUDE_CODE_BRIDGE_SESSION_ID",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_ENTRYPOINT",
        "WT_SESSION",
        "WT_PROFILE_ID",
    }
)
INHERITED_PREFIXES_TO_DROP = ("CLAUDE_CODE_SESSION_", "CLAUDE_CODE_MESSAGING_")

# PowerShell's spelling of the OSC 7501 `clear` that `attention.py` reads.
AGENT_RETURNED = '"`e]7501;state=clear`e\\"'

# Records before the first `cwd` in a real transcript: mode, owner, worktree state and
# file history. A generous bound, so a long transcript is never read whole.
TRANSCRIPT_LINES_TO_READ = 200


@dataclass(frozen=True)
class Launch:
    """One agent start: where it runs and the arguments after the executable."""

    cwd: str
    argv: list[str]
    # A resumed session has been run here before, so its agent will not ask for trust.
    resumes: bool = False
    # The agent cuts a worktree, which `claude -w` refuses to do in an untrusted folder.
    makes_worktree: bool = False


def new_session_id(agent: str) -> str | None:
    """The session id a new tab fixes before its agent starts, so it can resume it and
    read its folder back. Codex chooses its own, found later from its rollout."""
    return str(uuid.uuid4()) if agent == CLAUDE else None


def claude_transcript(claude_home: Path, session_id: str) -> Path | None:
    """Claude writes the transcript on the first message, so a tab closed before one
    has nothing to resume. Found by name, as the directory encodes the cwd."""
    return next(iter((claude_home / "projects").glob(f"*/{session_id}.jsonl")), None)


def claude_session_exists(claude_home: Path, session_id: str) -> bool:
    return claude_transcript(claude_home, session_id) is not None


def claude_session_tree(claude_home: Path, session_id: str) -> str | None:
    """The folder a Claude session works in, read from its transcript: the worktree
    `claude -w` named for itself, wherever the session was started. Its
    `worktree-state` record and first `cwd` come within the opening lines."""
    transcript = claude_transcript(claude_home, session_id)
    if transcript is None:
        return None
    try:
        with transcript.open(encoding="utf-8") as lines:
            for line in itertools.islice(lines, TRANSCRIPT_LINES_TO_READ):
                tree = _record_tree(line)
                if tree:
                    return tree
    except (OSError, UnicodeDecodeError):
        return None
    return None


def _record_tree(line: str) -> str | None:
    """The folder one transcript line names, if it names one."""
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    worktree = record.get("worktreeSession")
    if isinstance(worktree, dict) and isinstance(worktree.get("worktreePath"), str):
        return worktree["worktreePath"]
    cwd = record.get("cwd")
    return cwd if isinstance(cwd, str) else None


def tab_tree(tab: Tab) -> Path:
    """The folder a tab's agent works in: where its branch and ship intent are. A new
    worktree's is the project's until the agent's own record says where it went."""
    return Path(tab.cwd) if tab.cwd else Path(tab.project_path)


def claude_launch(tab: Tab, settings: Settings, prompt: str | None = None) -> Launch:
    """Resume the tab's session if it has one, otherwise start it. Claude names a new
    worktree itself, as it does from any terminal. `prompt`, if given, is the resumed
    session's next message; a session not yet begun has nothing to go on with."""
    session_id = tab.session_id or str(uuid.uuid4())
    if claude_session_exists(settings.claude_home, session_id):
        # `-w` would be taken as a request for a checkout, so resume from inside it.
        tree = tab.cwd or claude_session_tree(settings.claude_home, session_id)
        cwd = tree if tree and Path(tree).is_dir() else tab.project_path
        argv = [settings.claude_command, "--resume", session_id]
        return Launch(cwd, [*argv, prompt] if prompt else argv, resumes=True)
    args = [settings.claude_command, "--session-id", session_id]
    if tab.cwd and Path(tab.cwd).is_dir():
        # Its tree exists already: a PR's, or one a tab closed before its first message.
        return Launch(tab.cwd, args)
    if tab.worktree:
        return Launch(tab.project_path, [*args, "--worktree"], makes_worktree=True)
    return Launch(tab.project_path, args)


def claude_trust_launch(tab: Tab, settings: Settings) -> Launch:
    """Plain Claude, for its trust screen alone: it has no other way to be asked, and no
    conversation starts here, so the session the tab was given is left for the real start."""
    return Launch(tab.project_path, [settings.claude_command])


def codex_launch(tab: Tab, settings: Settings) -> Launch:
    """Resume the tab's session if it has one, otherwise start it."""
    if tab.session_id:
        cwd = tab.cwd if tab.cwd and Path(tab.cwd).is_dir() else tab.project_path
        return Launch(cwd, [settings.codex_command, "resume", tab.session_id], resumes=True)
    args = [settings.codex_command]
    if tab.worktree:
        args.append("--worktree")
    return Launch(tab.project_path, args, makes_worktree=tab.worktree)


def launch_for(tab: Tab, settings: Settings, prompt: str | None = None) -> Launch:
    """The start for this tab's agent. Only Claude takes a `prompt`."""
    if tab.agent == CLAUDE:
        return claude_launch(tab, settings, prompt)
    return codex_launch(tab, settings)


def powershell_quote(argument: str) -> str:
    """A single-quoted PowerShell literal, in which only `'` needs doubling."""
    return "'" + argument.replace("'", "''") + "'"


def shell_argv(shell: str, argv: list[str]) -> list[str]:
    """PowerShell running `argv` and staying open. Encoded, because the command crosses
    Windows' argument quoting and PowerShell's parser, and a path can upset either.

    It reports `clear` itself once the agent returns: Codex never does, and an agent
    that fails to start or dies cannot, so the tab would claim it was still running."""
    line = "& " + " ".join(powershell_quote(argument) for argument in argv)
    line += f"; [Console]::Write({AGENT_RETURNED})"
    encoded = base64.b64encode(line.encode("utf-16-le")).decode("ascii")
    return [shell, "-NoLogo", "-NoExit", "-EncodedCommand", encoded]


def agent_env(inherited: Mapping[str, str]) -> dict[str, str]:
    """A tab's environment: the app's, minus what belonged to whatever launched it."""
    env = {
        key: value
        for key, value in inherited.items()
        if key not in INHERITED_ENV_TO_DROP and not key.startswith(INHERITED_PREFIXES_TO_DROP)
    }
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    return env

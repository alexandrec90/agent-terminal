"""How each agent is launched, resumed and found again on disk.

A tab's process is PowerShell 7 with the agent run inside it, so leaving the agent
drops to a shell in the same place rather than closing the tab.
"""

from __future__ import annotations

import base64
import secrets
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


@dataclass(frozen=True)
class Launch:
    """One agent start: where it runs and the arguments after the executable."""

    cwd: str
    argv: list[str]
    # A resumed session has been run here before, so its agent will not ask for trust.
    resumes: bool = False


def new_tab_identity(agent: str, worktree: bool) -> tuple[str | None, str | None]:
    """The session id and worktree name a new tab can fix before its agent starts.
    Codex chooses both for itself, so it gets neither."""
    if agent != CLAUDE:
        return None, None
    return str(uuid.uuid4()), f"tab-{secrets.token_hex(4)}" if worktree else None


def claude_worktree_dir(project_path: str, name: str) -> Path:
    """Where `claude -w <name>` puts its checkout."""
    return Path(project_path) / ".claude" / "worktrees" / name


def claude_session_exists(claude_home: Path, session_id: str) -> bool:
    """Claude writes the transcript on the first message, so a tab closed before one
    has nothing to resume. Found by name, as the directory encodes the cwd."""
    return any((claude_home / "projects").glob(f"*/{session_id}.jsonl"))


def tab_tree(tab: Tab) -> Path:
    """The folder a tab's agent works in: where its branch and ship intent are. `cwd` is
    known once recorded; a Claude worktree's follows from its name."""
    if tab.cwd:
        return Path(tab.cwd)
    if tab.worktree_name:
        return claude_worktree_dir(tab.project_path, tab.worktree_name)
    return Path(tab.project_path)


def claude_launch(tab: Tab, settings: Settings) -> Launch:
    """Resume the tab's session if it has one, otherwise start it."""
    session_id = tab.session_id or str(uuid.uuid4())
    if claude_session_exists(settings.claude_home, session_id):
        # `-w` would be taken as a request for a checkout, so resume from inside it.
        tree = tab_tree(tab)
        cwd = str(tree) if tree.is_dir() else tab.project_path
        return Launch(cwd, [settings.claude_command, "--resume", session_id], resumes=True)
    args = [settings.claude_command, "--session-id", session_id]
    if tab.worktree_name:
        args += ["--worktree", tab.worktree_name]
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
    return Launch(tab.project_path, args)


def launch_for(tab: Tab, settings: Settings) -> Launch:
    """The start for this tab's agent."""
    return claude_launch(tab, settings) if tab.agent == CLAUDE else codex_launch(tab, settings)


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

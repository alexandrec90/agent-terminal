"""Whether a Claude session can be ended and resumed later with nothing lost.

An agent at its prompt may still be doing something: a background command, a subagent, a
wake-up it set for itself. And after `/clear` or `/resume` the process is in another
session than the tab's, so resuming the tab's would bring back the wrong conversation.
Every doubt is a no: the cost is memory, where a wrong yes costs work.

`~/.claude/sessions/<pid>.json` is Claude Code's own record of a running process, read
here as Claude Code 2.1.332 writes it. It is undocumented, and gone or changed means no.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from agent_terminal.agents import claude_transcript
from agent_terminal.processes import Created, process_created, process_parents, started_later

# MCP servers start with the agent and stay for good; anything later is the agent's work.
STARTUP_SECONDS = 30.0
# A call to a tool that has Claude act later by itself, with no process to see meanwhile.
# Every transcript also lists these tools' definitions, which are not calls; and one
# quoted in text has its quotes escaped, so does not match.
SCHEDULES = re.compile(
    rb'"type":\s*"tool_use",\s*"id":\s*"[^"]*",\s*"name":\s*"(?:CronCreate|ScheduleWakeup)"'
)


@dataclass(frozen=True)
class ProcessView:
    """The machine's processes. Tests pass their own."""

    parents: Callable[[], Mapping[int, int] | None] = process_parents
    created: Created = process_created


def read_record(path: Path) -> dict[str, Any] | None:
    """One session record, or None if it is not readable as one."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def running_record(
    claude_home: Path, session_id: str, created: Created = process_created
) -> dict[str, Any] | None:
    """The record of the one process running this session. None if there is no such
    process, or more than one: which of them is the tab's is then unknown. A record
    outlives a killed process, so its start time must match the pid's."""
    records = [read_record(path) for path in (claude_home / "sessions").glob("*.json")]
    running = [
        record
        for record in records
        if record is not None
        and record.get("sessionId") == session_id
        and isinstance(record.get("pid"), int)
        and str(created(record["pid"])) == str(record.get("procStart"))
    ]
    return running[0] if len(running) == 1 else None


def last_written(transcript: Path) -> float:
    """When the session last wrote anything, its subagents' transcripts included."""
    folder = transcript.with_suffix("")
    files = [transcript, *(path for path in folder.rglob("*") if path.is_file())]
    return max(path.stat().st_mtime for path in files)


@lru_cache(maxsize=64)
def _schedules(transcript: Path, _changed: int) -> bool:
    with transcript.open("rb") as lines:
        return any(SCHEDULES.search(line) for line in lines)


def schedules_itself(transcript: Path) -> bool:
    """True if the session ever set itself to act later. Whether that is still to come is
    not recorded, so once is enough."""
    return _schedules(transcript, os.stat(transcript).st_mtime_ns)


def can_rest(
    claude_home: Path, session_id: str, quiet_since: float, view: ProcessView | None = None
) -> bool:
    """True if the session's process can be ended now and resumed later. It must have
    written nothing since `quiet_since`."""
    view = view or ProcessView()
    record = running_record(claude_home, session_id, view.created)
    if record is None or record.get("status") != "idle":
        return False
    parents = view.parents()
    if parents is None or started_later(record["pid"], parents, view.created, STARTUP_SECONDS):
        return False
    transcript = claude_transcript(claude_home, session_id)
    if transcript is None:
        return False
    try:
        return last_written(transcript) <= quiet_since and not schedules_itself(transcript)
    except OSError:
        return False

"""Claude's usage limit: which tabs' sessions stopped on it, until when, and putting them
back to work once it resets.

A turn refused for the limit ends with a record Claude Code 2.1.332 writes to the
session's transcript as

    {"type": "assistant", "apiError": "usage_limit_reached",
     "apiErrorParams": {"rate_limit_info": {"resetsAt": 1795739400, ...}}, ...}

and the next conversation record after it is whatever the session is asked next. So a
session is held by the limit exactly while that record is its last user or assistant
one. The transcript is read rather than the screen: its time is exact and needs no
parsing of a clock in the user's own zone.

Claude tabs only: Codex's record of the limit has not been captured from a real session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from agent_terminal import agents, attention
from agent_terminal.runner import LiveTab, TabRunner
from agent_terminal.settings import Settings

log = logging.getLogger(__name__)

USAGE_LIMIT = "usage_limit_reached"
CONVERSATION = frozenset({"user", "assistant"})
# Records a transcript writes between turns can be large: a request blob carries the whole
# system prompt. Read back this far for the last conversation record, and no further.
TAIL_CHUNK_BYTES = 64 * 1024
MAX_TAIL_BYTES = 8 * 1024 * 1024
CHECK_SECONDS = 30.0
# The limit's clock and this machine's may differ, and a resume a moment early is
# refused again.
RESUME_GRACE_SECONDS = 60.0
CONTINUE_PROMPT = "continue"
# An Enter close behind the text is read as part of a paste, which adds a line instead.
ENTER_DELAY_SECONDS = 0.5
# Statuses with a session that may be held: a tab starting, in its shell or gone has none.
WATCHED = frozenset({attention.IDLE, attention.BLOCKED, attention.WORKING, attention.ASLEEP})


def limit_reset(record: Any) -> float | None:
    """When the limit a refused turn's record names resets, in epoch seconds. None for any
    other record."""
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return None
    if record.get("apiError") != USAGE_LIMIT:
        return None
    info = _field(record.get("apiErrorParams"), "rate_limit_info")
    for holder in (info, record.get("quotaLimits")):
        resets = _field(holder, "resetsAt")
        if isinstance(resets, int | float) and not isinstance(resets, bool) and resets > 0:
            return float(resets)
    return None


def _field(holder: Any, name: str) -> Any:
    return holder.get(name) if isinstance(holder, dict) else None


def lines_from_end(path: Path, max_bytes: int = MAX_TAIL_BYTES) -> Iterator[bytes]:
    """The file's lines, last first, reading no more than `max_bytes` of it. A line cut
    by that bound is not given."""
    with path.open("rb") as file:
        end = file.seek(0, os.SEEK_END)
        read = 0
        rest = b""
        while end > 0 and read < max_bytes:
            size = min(TAIL_CHUNK_BYTES, end)
            end -= size
            file.seek(end)
            lines = (file.read(size) + rest).split(b"\n")
            read += size
            rest = lines[0]
            yield from reversed(lines[1:])
        if end == 0:
            yield rest


def last_conversation_record(transcript: Path) -> dict[str, Any] | None:
    """The session's last user or assistant record of its own, not a subagent's."""
    for line in lines_from_end(transcript):
        # Cheap before parsing: Claude writes its records compactly.
        if b'"type":"user"' not in line and b'"type":"assistant"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(record, dict)
            and record.get("type") in CONVERSATION
            and not record.get("isSidechain")
        ):
            return record
    return None


@lru_cache(maxsize=128)
def _limited_until(transcript: Path, _mtime_ns: int, _size: int) -> float | None:
    return limit_reset(last_conversation_record(transcript))


def limited_until(transcript: Path) -> float | None:
    """When the limit holding this session resets, or None if it is not held. Read again
    only when the transcript has changed."""
    try:
        stat = transcript.stat()
        return _limited_until(transcript, stat.st_mtime_ns, stat.st_size)
    except (OSError, UnicodeDecodeError):
        return None


def session_limited_until(claude_home: Path, session_id: str) -> float | None:
    transcript = agents.claude_transcript(claude_home, session_id)
    return None if transcript is None else limited_until(transcript)


def is_due(live: LiveTab, now: float, enabled: bool) -> bool:
    """True if a held tab's limit has reset and it has not yet been sent back to work for
    it. Text the user left in the prompt would be sent with it, so a tab drafting waits."""
    until = live.limited_until
    return (
        enabled
        and until is not None
        and now >= until + RESUME_GRACE_SECONDS
        and live.resumed_for != until
        and not live.drafting
        and not live.awaiting_trust
        and live.status in WATCHED
    )


@dataclass(frozen=True)
class LimitLinks:
    """What the watcher asks of the manager."""

    changed: Callable[[], None]
    # Start an asleep tab's agent, resuming its session with this prompt.
    wake: Callable[[LiveTab, str], None]


class LimitWatcher:
    """Runs on the event loop. `sweep` is one check of every tab."""

    def __init__(
        self,
        settings: Settings,
        loop: asyncio.AbstractEventLoop,
        runner: TabRunner,
        links: LimitLinks,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._loop = loop
        self._runner = runner
        self._links = links
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._resuming: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        if self._task is None:
            self._task = self._loop.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.sweep()
            except (OSError, RuntimeError):
                # One check the machine refused must not be the last. A bug is raised.
                log.exception("usage limit check failed")
            await asyncio.sleep(CHECK_SECONDS)

    async def sweep(self) -> None:
        """Note which tabs the limit holds, and send back to work those it has let go."""
        changed = False
        for live in list(self._runner.live.values()):
            until = await self._read(live)
            if self._runner.live.get(live.tab.id) is not live:
                continue  # closed, or put to sleep, during the read
            if until != live.limited_until:
                live.limited_until = until
                changed = True
            if is_due(live, self._clock(), self._settings.resume_after_limit):
                self._resume(live)
                changed = True
        if changed:
            self._links.changed()

    async def _read(self, live: LiveTab) -> float | None:
        tab = live.tab
        if tab.agent != agents.CLAUDE or not tab.session_id or live.status not in WATCHED:
            return None
        home = self._settings.claude_home
        return await self._loop.run_in_executor(
            None, session_limited_until, home, tab.session_id
        )

    def _resume(self, live: LiveTab) -> None:
        """Once per limit: a resume refused again is a new limit, with a new time."""
        live.resumed_for = live.limited_until
        log.info("usage limit reset: resuming tab %s", live.tab.id)
        if live.tab.asleep:
            self._links.wake(live, CONTINUE_PROMPT)
            return
        task = self._loop.create_task(self._type(live))
        self._resuming.add(task)
        task.add_done_callback(self._resuming.discard)

    async def _type(self, live: LiveTab) -> None:
        """Each keystroke only into the process the check was of: a tab closed or put to
        sleep meanwhile is not that one."""
        tab_id = live.tab.id
        if self._runner.live.get(tab_id) is not live:
            return
        self._runner.input(tab_id, CONTINUE_PROMPT)
        await asyncio.sleep(ENTER_DELAY_SECONDS)
        if self._runner.live.get(tab_id) is live:
            self._runner.input(tab_id, "\r")

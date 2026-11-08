"""Reads what an agent says about itself out of its terminal output.

Claude Code and Codex both report their state with OSC 7501, a "program status"
sequence: `ESC ] 7501 ; state=blocked:app=codex:kind=permission ST`. Codex sends it
unprompted. Claude sends it only to a terminal that answers its `7501;?` query, which is
why `QUERY_REPLY` exists: without the answer a Claude tab would report nothing.

States seen from real sessions (Claude Code 2.1.318, Codex 0.166):

    idle      at the prompt, nothing pending
    working   running a turn
    blocked   stopped on the user: `kind=permission` or `kind=question`
    done      turn finished
    error     turn failed (Codex)
    clear     the agent has gone; the shell is back
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

OSC_START = "\x1b]"
BEL = "\x07"
ST = "\x1b\\"
ESC = "\x1b"
# An OSC longer than this is not one of ours: clipboard and image payloads run to
# megabytes, and holding one here would only delay it.
MAX_OSC = 4096

PROGRAM_STATUS = "7501"
QUERY = f"{PROGRAM_STATUS};?"
QUERY_REPLY = f"{OSC_START}{QUERY}{ST}"

STARTING = "starting"
WORKING = "working"
BLOCKED = "blocked"
IDLE = "idle"
SHELL = "shell"
EXITED = "exited"

STOPPED_ON_USER = frozenset({"blocked", "error", "failed"})

NO_ATTENTION = ""
NEEDS_INPUT = "input"
FINISHED = "finished"


def is_user_input(data: str) -> bool:
    """False for what the terminal says on its own: its replies to an agent's queries
    arrive as input, and all of them open with an escape."""
    return not data.startswith(ESC)


ACTION_REQUIRED_PREFIX = "[ ! ] Action Required | "
AGENT_NAME_TITLES = frozenset({"claude", "claude code", "codex"})
LEADING_GLYPHS = re.compile(r"^[^\w(\[\"']+")


class OscScanner:
    """Finds OSC payloads in a stream cut into arbitrary chunks. Output is left alone:
    this only reads a copy of what goes to the terminal."""

    def __init__(self) -> None:
        self._payload: str | None = None
        self._tail = ""

    def feed(self, text: str) -> list[str]:
        """The payloads completed by this chunk, in order."""
        found: list[str] = []
        text = self._tail + text
        self._tail = ""
        position = 0
        while position < len(text):
            if self._payload is None:
                position = self._seek_start(text, position)
            else:
                position = self._seek_end(text, position, found)
        return found

    def _seek_start(self, text: str, position: int) -> int:
        start = text.find(OSC_START, position)
        if start >= 0:
            self._payload = ""
            return start + len(OSC_START)
        if text.endswith(ESC):
            self._tail = ESC
        return len(text)

    def _seek_end(self, text: str, position: int, found: list[str]) -> int:
        bell = text.find(BEL, position)
        escape = text.find(ESC, position, bell if bell >= 0 else len(text))
        end = bell if escape < 0 else escape
        if end < 0:
            self._payload = (self._payload or "") + text[position:]
            if len(self._payload) > MAX_OSC:
                self._payload = None
            return len(text)
        if text[end] == ESC and end + 1 == len(text):
            self._payload = (self._payload or "") + text[position:end]
            self._tail = ESC
            return len(text)
        terminated = text[end] == BEL or text[end + 1] == "\\"
        payload = (self._payload or "") + text[position:end]
        self._payload = None
        if terminated:
            found.append(payload)
            return end + (1 if text[end] == BEL else 2)
        # An ESC opening anything else abandons the OSC, and may itself start one.
        return end


def parse_program_state(payload: str) -> str | None:
    """The `state` of a 7501 payload, or None if the payload is anything else."""
    code, _, body = payload.partition(";")
    if code != PROGRAM_STATUS:
        return None
    fields = dict(part.partition("=")[::2] for part in body.split(":"))
    return fields.get("state") or None


def parse_title(payload: str) -> str | None:
    """The text of a window-title OSC, or None if the payload is anything else."""
    code, separator, title = payload.partition(";")
    return title if separator and code in ("0", "2") else None


def clean_title(raw: str, project: str) -> str:
    """What a tab can usefully show of an agent's title, or "" for nothing: agents
    title a tab with their own name until they have something better, and the shell
    with the path of its executable."""
    title = raw.removeprefix(ACTION_REQUIRED_PREFIX).removesuffix(f" | {project}")
    title = LEADING_GLYPHS.sub("", title).strip()
    if title.lower() in AGENT_NAME_TITLES or title.lower().endswith(".exe") or title == project:
        return ""
    return title


@dataclass(frozen=True)
class Presence:
    """A tab's standing with the user.

    `attention` is why the agent has stopped on them: `NEEDS_INPUT` cannot go on without
    an answer, `FINISHED` is done and can wait. `parked` is them saying "not now", and
    is separate so that un-parking shows whatever is still true underneath.
    """

    status: str = STARTING
    attention: str = NO_ATTENTION
    parked: bool = False


def advance(presence: Presence, state: str, *, user_engaged: bool = True) -> Presence:
    """Apply one reported state. Only the agent stopping anew brings a parked tab back:
    the second `done` a turn sends, or a restored session settling, must not.

    `user_engaged` says the user has typed in this process. `working` means they
    answered only then: Codex reports it on every start, which must not cost a restored
    tab the flag saying it was waiting on them."""
    if state == WORKING:
        kept = NO_ATTENTION if user_engaged else presence.attention
        return replace(presence, status=WORKING, attention=kept)
    if state in STOPPED_ON_USER:
        return Presence(status=BLOCKED, attention=NEEDS_INPUT, parked=False)
    if state == "done":
        if presence.status in (WORKING, BLOCKED):
            return Presence(status=IDLE, attention=FINISHED, parked=False)
        return replace(presence, status=IDLE)
    if state == IDLE:
        return replace(presence, status=IDLE)
    if state == "clear":
        return replace(presence, status=SHELL, attention=NO_ATTENTION)
    return presence

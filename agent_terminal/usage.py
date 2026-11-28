"""Claude's plan limits, for the meters under the tally: how much of each is used, and how
far through its window the clock is, so a pace ahead of the time shows.

They are what Claude Code's `/usage` shows, asked of the same endpoint with the login
Claude Code keeps in `.credentials.json`. That file is Claude Code's: it is read, never
written, and a token past its time is left for Claude Code to refresh. The reply, as
captured on 2026-11-27 for a Max subscriber, lists the limits as

    {"limits": [{"kind": "session", "group": "session", "percent": 4,
                 "resets_at": "2026-11-28T00:10:00.260329+00:00", "scope": null, ...},
                {"kind": "weekly_all", "group": "weekly", "percent": 27, ...},
                {"kind": "weekly_scoped", "group": "weekly", "percent": 0,
                 "scope": {"model": {"display_name": "Fable"}, ...}, ...}], ...}

beside older per-window fields (`five_hour`, `seven_day`) that this does not read.

Claude only: Codex's limits have not been captured from a real session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_terminal.desktop import Emit, Handler
from agent_terminal.settings import Settings

log = logging.getLogger(__name__)

BETA = "oauth-2025-04-20"
TIMEOUT_SECONDS = 15.0
# How long each group's window runs, for how far through it the clock is.
WINDOW_SECONDS = {"session": 5 * 3600.0, "weekly": 7 * 86400.0}
LABELS = {"session": "5-hour", "weekly_all": "Weekly"}

Fetch = Callable[[str, str], str]


class UsageError(Exception):
    """The endpoint could not be asked, or answered with something else."""


@dataclass(frozen=True)
class Limit:
    """One limit: its name, the share used, and when its window ends. `resets_at` and
    `window_seconds` are None when the reply does not say, and there is then no pace."""

    kind: str
    label: str
    percent: float
    resets_at: float | None
    window_seconds: float | None


def read_token(claude_home: Path, now: float) -> str | None:
    """Claude Code's login token, or None with none, an unreadable file, or one past its
    time."""
    try:
        record = json.loads((claude_home / ".credentials.json").read_text(encoding="utf-8"))
        oauth = record["claudeAiOauth"]
        token = oauth["accessToken"]
        expires = oauth.get("expiresAt")
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(token, str) or not token:
        return None
    timed = isinstance(expires, int | float) and not isinstance(expires, bool)
    return None if timed and expires / 1000 <= now else token


def label_of(entry: dict[str, Any]) -> str:
    """What a limit is called: a model's own weekly limit by its model's name."""
    kind = str(entry.get("kind", ""))
    if kind in LABELS:
        return LABELS[kind]
    scope = entry.get("scope")
    model = scope.get("model") if isinstance(scope, dict) else None
    name = model.get("display_name") if isinstance(model, dict) else None
    group = str(entry.get("group", ""))
    if isinstance(name, str) and name:
        return f"{name} weekly" if group == "weekly" else name
    return kind.replace("_", " ").capitalize() or "Limit"


def _resets_at(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def parse_limits(text: str) -> list[Limit]:
    """The limits in a reply, in its order. Raises `UsageError` on anything else; one
    entry that is not a limit is skipped."""
    try:
        entries = json.loads(text)["limits"]
    except (ValueError, KeyError, TypeError) as exc:
        raise UsageError(f"unexpected usage reply: {exc}") from exc
    if not isinstance(entries, list):
        raise UsageError("unexpected usage reply: limits is not a list")
    limits = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        percent = entry.get("percent")
        if not isinstance(percent, int | float) or isinstance(percent, bool):
            continue
        resets = _resets_at(entry.get("resets_at"))
        window = WINDOW_SECONDS.get(str(entry.get("group", "")))
        limits.append(
            Limit(
                kind=str(entry.get("kind", "")),
                label=label_of(entry),
                percent=float(percent),
                resets_at=resets,
                window_seconds=window if resets is not None else None,
            )
        )
    return limits


def elapsed_share(limit: Limit, now: float) -> float | None:
    """How far through its window the clock is, 0 to 1: where an even pace would have
    used it to. None when the window is not known."""
    if limit.resets_at is None or limit.window_seconds is None:
        return None
    share = 1 - (limit.resets_at - now) / limit.window_seconds
    return min(max(share, 0.0), 1.0)


def describe(limits: list[Limit] | None) -> dict[str, Any]:
    """The message. `limits` is None until the endpoint has answered once. The page works
    out the pace itself, so it moves between polls."""
    rows = None if limits is None else [asdict(limit) for limit in limits]
    return {"type": "usage", "limits": rows}


def fetch(url: str, token: str) -> str:
    """The endpoint's reply. Blocking."""
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "anthropic-beta": BETA}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 - the URL is the user's own setting; the e2e tests give a file
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise UsageError(f"usage endpoint answered {exc.code}") from exc
    except (urllib.error.URLError, OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"could not ask for usage: {exc}") from exc


class Usage:
    """Keeps the limits current once the page has asked for them."""

    def __init__(
        self,
        settings: Settings,
        emit: Emit,
        loop: asyncio.AbstractEventLoop,
        fetcher: Fetch = fetch,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._emit = emit
        self._loop = loop
        self._fetch = fetcher
        self._clock = clock
        self._limits: list[Limit] | None = None
        self._sent: dict[str, Any] | None = None
        self._complained = ""
        self._task: asyncio.Task[None] | None = None

    def handlers(self) -> dict[str, Handler]:
        """The message types answered here."""
        return {"usage": self._on_asked}

    async def _on_asked(self, _message: dict[str, Any]) -> None:
        """Start polling, or, for a page that has reloaded, say it again."""
        if self._task is not None:
            if self._sent is not None:
                self._emit(self._sent)
            return
        self._task = self._loop.create_task(self._watch())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _watch(self) -> None:
        while True:
            await self.poll()
            await asyncio.sleep(self._settings.usage_poll_seconds)

    async def poll(self) -> None:
        """Ask once. A failure leaves what was known in place."""
        try:
            self._limits = await self._loop.run_in_executor(None, self._read)
        except UsageError as exc:
            self._complain(str(exc))
            return
        self._complained = ""
        self._send()

    def _read(self) -> list[Limit]:
        token = read_token(self._settings.claude_home, self._clock())
        if token is None:
            raise UsageError("no current Claude login to read usage with")
        return parse_limits(self._fetch(self._settings.usage_url, token))

    def _complain(self, message: str) -> None:
        if message != self._complained:
            self._complained = message
            log.warning("%s", message)

    def _send(self) -> None:
        message = describe(self._limits)
        if message != self._sent:
            self._sent = message
            self._emit(message)

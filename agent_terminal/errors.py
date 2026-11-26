"""Every error the app shows the user is also written to its log.

The page shows a refusal or a failure in place, as one line in a dialog or a notice in
the bar, and that line goes when the dialog closes. Logging each one as it leaves for
the page puts it where it can still be read afterwards, without any handler having to
remember to.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

Emit = Callable[[dict[str, Any]], None]

# Sent again whenever anything about them changes, so one error repeats across many
# sends: each is logged when it first appears, not on every send.
STATUS_TYPES = frozenset({"app"})
# Bounds what the page may write to the log in one report.
PAGE_ERROR_CHARS = 8000


def carried_errors(message: dict[str, Any]) -> list[str]:
    """The error texts a message to the page carries, in the user's words."""
    found: list[str] = []
    error = message.get("error")
    if isinstance(error, str) and error:
        found.append(error)
    if message.get("type") == "pr_error":
        found.append(str(message.get("message", "")) or "unknown error")
    update = message.get("update")
    if isinstance(update, dict) and update.get("error"):
        found.append(f"update check failed: {update['error']}")
    notice = message.get("notice")
    if isinstance(notice, dict) and notice.get("ok") is False:
        found.append(f"{notice.get('step', 'update')} failed: {notice.get('detail', '')}")
    return found


def subject(message: dict[str, Any]) -> str:
    """What the error is about, where the message names it."""
    if message.get("type") == "pr_error":
        return f"{message.get('repo')}#{message.get('number')}"
    for key in ("requested", "path", "full_name", "name", "id"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


class ErrorLog:
    """Wraps the page's `emit`: logs each error a message carries, then sends it on."""

    def __init__(self, emit: Emit, logger: logging.Logger = log) -> None:
        self._emit = emit
        self._log = logger
        self._status_seen: dict[str, set[str]] = {}

    def __call__(self, message: dict[str, Any]) -> None:
        self.record(message)
        self._emit(message)

    def record(self, message: dict[str, Any]) -> None:
        kind = str(message.get("type", ""))
        errors = carried_errors(message)
        if kind in STATUS_TYPES:
            seen = self._status_seen.get(kind, set())
            self._status_seen[kind] = set(errors)
            errors = [e for e in errors if e not in seen]
        about = subject(message)
        for error in errors:
            if about:
                self._log.error("%s (%s): %s", kind, about, error)
            else:
                self._log.error("%s: %s", kind, error)


def page_error_text(message: dict[str, Any]) -> str:
    """A script error the page reported, as one bounded log entry."""
    parts = [str(message.get("message") or "unknown error")]
    source = message.get("source")
    if source:
        parts.append(f"at {source}:{message.get('line', '?')}:{message.get('column', '?')}")
    stack = message.get("stack")
    if stack:
        parts.append(str(stack))
    text = "\n".join(parts)
    if len(text) > PAGE_ERROR_CHARS:
        text = text[:PAGE_ERROR_CHARS] + " [truncated]"
    return text

"""Ask before the window closes on agents in the middle of a turn.

Closing ends every process. The tabs come back next launch, resumed, but a turn that was
running is cut off where it stood, so a stray click on the window's "x" must not do that
without asking.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from agent_terminal import attention

if TYPE_CHECKING:
    from agent_terminal.runner import LiveTab

BUSY_STATUSES = frozenset({attention.WORKING, attention.BLOCKED})
DIALOG_TITLE = "Agents still running"
# The dialog lists this many tabs by name, and counts the rest.
LISTED_TABS = 8


def tab_label(project: str, title: str) -> str:
    """How the dialog names a tab: its project, and its title when it has one."""
    return f"{project}: {title}" if title else project


def busy_tabs(live: Iterable[LiveTab]) -> list[str]:
    """A label for each tab whose agent is mid-turn, which closing the window would cut
    short: working, or stopped on a question it is still waiting to act on."""
    return [
        tab_label(each.tab.project, each.tab.title) for each in live if each.status in BUSY_STATUSES
    ]


def close_message(busy: list[str] | None) -> str:
    """The dialog's text. `None` is a backend that did not answer, which asks too:
    an unknown is not a reason to throw work away."""
    if busy is None:
        return (
            "Could not check whether any agent is still working.\n\n"
            "Close anyway? Every agent will be stopped."
        )
    count = len(busy)
    noun = "agent is" if count == 1 else "agents are"
    lines = [f"  \N{BULLET} {label}" for label in busy[:LISTED_TABS]]
    if count > LISTED_TABS:
        lines.append(f"  \N{BULLET} and {count - LISTED_TABS} more")
    return (
        f"{count} {noun} still running:\n\n"
        + "\n".join(lines)
        + "\n\nClose anyway? Their current turn will be cut short; the tabs come back"
        " next time."
    )


def should_close(busy: Callable[[], list[str] | None], confirm: Callable[[str, str], bool]) -> bool:
    """True to let the window close: nothing is busy, or the user said so."""
    names = busy()
    if names == []:
        return True
    return bool(confirm(DIALOG_TITLE, close_message(names)))


def guard_window(window: Any, busy: Callable[[], list[str] | None]) -> None:
    """Hook a pywebview window's `closing` event. That event runs its handlers on the
    window's own thread and cancels the close when one returns False, so the native
    dialog is shown from there. A handler that raises is logged by pywebview and does
    not cancel, so a broken guard cannot trap the user in a window that will not close."""

    def on_closing() -> bool:
        return should_close(busy, window.create_confirmation_dialog)

    window.events.closing += on_closing

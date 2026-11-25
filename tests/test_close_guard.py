import pytest

from agent_terminal import close_guard
from agent_terminal.close_guard import (
    DIALOG_TITLE,
    LISTED_TABS,
    close_message,
    guard_window,
    should_close,
    tab_label,
)


class FakeEvent:
    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class FakeEvents:
    def __init__(self) -> None:
        self.closing = FakeEvent()


class FakeWindow:
    """pywebview's window as the guard uses it: a `closing` event whose handler cancels
    the close by returning False, and a native OK/Cancel dialog."""

    def __init__(self, answer: bool) -> None:
        self.events = FakeEvents()
        self.answer = answer
        self.asked: list[tuple[str, str]] = []

    def create_confirmation_dialog(self, title: str, message: str) -> bool:
        self.asked.append((title, message))
        return self.answer

    def close(self) -> bool:
        """True if the window closed. Like pywebview's `Event.set`, a handler that raises
        is logged and dropped, and only a False cancels. The one error a test raises is
        `RuntimeError`."""
        results = []
        for handler in self.events.closing.handlers:
            try:
                results.append(handler())
            except RuntimeError as exc:
                results.append(exc)
        return False not in results


def test_tab_label_uses_the_title_when_there_is_one():
    assert tab_label("repo", "Fix the bug") == "repo: Fix the bug"
    assert tab_label("repo", "") == "repo"


def test_one_busy_agent_is_named():
    message = close_message(["repo: Fix the bug"])
    assert message.startswith("1 agent is still running")
    assert "repo: Fix the bug" in message


def test_several_busy_agents_are_counted():
    assert close_message(["a", "b"]).startswith("2 agents are still running")


def test_a_long_list_is_cut_and_the_rest_counted():
    names = [f"tab{i}" for i in range(LISTED_TABS + 3)]
    message = close_message(names)
    assert f"tab{LISTED_TABS - 1}" in message
    assert f"tab{LISTED_TABS}" not in message
    assert "and 3 more" in message


def test_an_unanswered_query_still_asks():
    assert "Could not check" in close_message(None)


def test_nothing_busy_closes_without_asking():
    asked = []
    assert should_close(lambda: [], lambda *a: asked.append(a) or False)
    assert asked == []


@pytest.mark.parametrize("answer", [True, False])
def test_a_busy_agent_closes_only_if_the_user_says_so(answer):
    assert should_close(lambda: ["repo"], lambda _t, _m: answer) is answer


def test_an_unknown_answer_asks_rather_than_closing():
    asked = []
    assert not should_close(lambda: None, lambda t, m: asked.append(t) or False)
    assert asked == [DIALOG_TITLE]


def test_the_window_stays_open_when_the_user_cancels():
    window = FakeWindow(answer=False)
    guard_window(window, lambda: ["repo: Fix the bug"])
    assert window.close() is False
    assert window.asked[0][0] == DIALOG_TITLE
    assert "repo: Fix the bug" in window.asked[0][1]


def test_the_window_closes_when_the_user_confirms():
    window = FakeWindow(answer=True)
    guard_window(window, lambda: ["repo"])
    assert window.close() is True


def test_an_idle_window_closes_without_a_dialog():
    window = FakeWindow(answer=False)
    guard_window(window, lambda: [])
    assert window.close() is True
    assert window.asked == []


def test_a_crashing_guard_does_not_trap_the_window():
    def broken():
        raise RuntimeError("boom")

    window = FakeWindow(answer=False)
    guard_window(window, broken)
    assert window.close() is True


def test_working_and_blocked_count_as_busy_and_idle_does_not():
    assert {"working", "blocked"} <= close_guard.BUSY_STATUSES
    assert "idle" not in close_guard.BUSY_STATUSES

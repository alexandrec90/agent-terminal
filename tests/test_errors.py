import logging

import pytest

from agent_terminal import errors
from agent_terminal.errors import ErrorLog, carried_errors, page_error_text, subject


class Sink:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def __call__(self, message: dict) -> None:
        self.sent.append(message)


@pytest.fixture
def logged(caplog):
    caplog.set_level(logging.ERROR, logger=errors.log.name)

    def lines() -> list[str]:
        return [r.getMessage() for r in caplog.records if r.name == errors.log.name]

    return lines


def test_a_refused_delete_is_logged_and_still_reaches_the_page(logged):
    sink = Sink()
    message = {
        "type": "deleted",
        "requested": "C:\\projects\\radio",
        "error": "Windows could not recycle C:\\projects\\radio",
    }
    ErrorLog(sink)(message)
    assert sink.sent == [message]
    assert logged() == [
        "deleted (C:\\projects\\radio): Windows could not recycle C:\\projects\\radio"
    ]


def test_a_message_without_an_error_logs_nothing(logged):
    sink = Sink()
    log = ErrorLog(sink)
    log({"type": "deleted", "requested": "x", "error": ""})
    log({"type": "output", "id": "a", "data": "error: not ours, the agent's"})
    assert logged() == []
    assert len(sink.sent) == 2


def test_the_same_failure_twice_is_logged_twice(logged):
    """Each is the user trying again: neither may vanish."""
    log = ErrorLog(Sink())
    message = {"type": "cloned", "full_name": "me/repo", "error": "gh failed"}
    log(message)
    log(message)
    assert logged() == ["cloned (me/repo): gh failed"] * 2


def test_a_pr_error_is_logged_with_its_pr(logged):
    ErrorLog(Sink())({"type": "pr_error", "repo": "C:\\p\\a", "number": 7, "message": "denied"})
    assert logged() == ["pr_error (C:\\p\\a#7): denied"]


def test_a_status_error_is_logged_once_however_often_it_is_sent(logged):
    log = ErrorLog(Sink())
    failing = {"type": "app", "update": {"error": "no network"}, "notice": None}
    log(failing)
    log(failing)
    assert logged() == ["app: update check failed: no network"]


def test_a_status_error_that_clears_and_returns_is_logged_again(logged):
    log = ErrorLog(Sink())
    failing = {"type": "app", "update": {"error": "no network"}}
    log(failing)
    log({"type": "app", "update": {"error": ""}})
    log(failing)
    assert len(logged()) == 2


def test_a_failed_update_notice_is_an_error():
    notice = {"ok": False, "step": "pull", "detail": "not a fast-forward"}
    assert carried_errors({"type": "app", "notice": notice}) == [
        "pull failed: not a fast-forward"
    ]
    assert carried_errors({"type": "app", "notice": {"ok": True, "step": "pull"}}) == []


@pytest.mark.parametrize(
    "message",
    [
        {"type": "folder", "error": None},
        {"type": "folder", "error": 3},
        {"type": "app", "update": None, "notice": None},
        {"type": "app", "update": "text", "notice": "text"},
    ],
)
def test_odd_fields_carry_no_error(message):
    assert carried_errors(message) == []


def test_a_pr_error_without_text_still_counts():
    assert carried_errors({"type": "pr_error"}) == ["unknown error"]


def test_the_subject_is_whatever_the_message_names():
    assert subject({"type": "folder", "path": "C:\\x"}) == "C:\\x"
    assert subject({"type": "presets"}) == ""
    assert subject({"type": "x", "requested": 5, "id": "t1"}) == "t1"
    assert subject({"type": "restored", "name": "radio", "path": ""}) == "radio"


def test_a_page_error_names_where_it_happened():
    text = page_error_text(
        {"message": "boom", "source": "app.js", "line": 3, "column": 9, "stack": "at f"}
    )
    assert text == "boom\nat app.js:3:9\nat f"


def test_a_page_error_with_nothing_in_it_still_says_so():
    assert page_error_text({}) == "unknown error"


def test_a_page_error_is_bounded():
    text = page_error_text({"message": "x" * (errors.PAGE_ERROR_CHARS * 2)})
    assert len(text) == errors.PAGE_ERROR_CHARS + len(" [truncated]")
    assert text.endswith("[truncated]")

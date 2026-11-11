import sys

from agent_terminal import clipboard


class FakeUser32:
    def __init__(self, free_after: int) -> None:
        self.attempts = 0
        self.free_after = free_after
        self.closed = 0

    def OpenClipboard(self, _owner) -> bool:
        self.attempts += 1
        return self.attempts > self.free_after

    def CloseClipboard(self) -> None:
        self.closed += 1


def test_a_busy_clipboard_is_retried(monkeypatch):
    monkeypatch.setattr(clipboard, "OPEN_RETRY_SECONDS", 0)
    user32 = FakeUser32(free_after=2)
    with clipboard.open_clipboard(user32) as opened:
        assert opened is True
    assert user32.attempts == 3
    assert user32.closed == 1


def test_a_clipboard_that_stays_busy_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(clipboard, "OPEN_RETRY_SECONDS", 0)
    user32 = FakeUser32(free_after=999)
    with clipboard.open_clipboard(user32) as opened:
        assert opened is False
    assert user32.closed == 0


def test_read_is_empty_where_there_is_no_windows_clipboard(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert clipboard.read_text() == ""


def test_write_does_nothing_where_there_is_no_windows_clipboard(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    clipboard.write_text("x")

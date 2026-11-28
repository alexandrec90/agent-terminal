from __future__ import annotations

import asyncio
import json
import urllib.error
from datetime import datetime
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from agent_terminal import usage
from agent_terminal.settings import Settings
from agent_terminal.usage import (
    Limit,
    Usage,
    UsageError,
    describe,
    elapsed_share,
    fetch,
    label_of,
    parse_limits,
    read_token,
)

# The `limits` of a real reply, captured 2026-11-27 for a Max subscriber, with the fields
# beside them that this does not read cut to one of each kind.
CAPTURED = {
    "five_hour": {"utilization": 4.0, "resets_at": "2026-11-28T00:10:00.260329+00:00"},
    "seven_day": {"utilization": 27.0, "resets_at": "2026-12-02T06:00:00.260349+00:00"},
    "seven_day_opus": None,
    "limits": [
        {
            "kind": "session",
            "group": "session",
            "percent": 4,
            "severity": "normal",
            "resets_at": "2026-11-28T00:10:00.260329+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": 27,
            "severity": "normal",
            "resets_at": "2026-12-02T06:00:00.260349+00:00",
            "scope": None,
            "is_active": True,
        },
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 0,
            "severity": "normal",
            "resets_at": "2026-12-02T06:00:00+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
            "is_active": False,
        },
    ],
}
WEEK_RESET = datetime.fromisoformat("2026-12-02T06:00:00+00:00").timestamp()
NOW = WEEK_RESET - 5 * 86400  # two days into the week


def write_login(home: Path, token: Any = "secret", expires: Any = (NOW + 3600) * 1000) -> None:
    home.mkdir(parents=True, exist_ok=True)
    record = {"claudeAiOauth": {"accessToken": token, "expiresAt": expires}}
    (home / ".credentials.json").write_text(json.dumps(record), encoding="utf-8")


# --- the login ---


def test_the_token_is_read(tmp_path):
    write_login(tmp_path)
    assert read_token(tmp_path, NOW) == "secret"


def test_no_login_file_is_no_token(tmp_path):
    assert read_token(tmp_path, NOW) is None


def test_a_token_past_its_time_is_left_for_claude_to_refresh(tmp_path):
    write_login(tmp_path, expires=(NOW - 1) * 1000)
    assert read_token(tmp_path, NOW) is None


def test_a_token_with_no_time_is_used(tmp_path):
    write_login(tmp_path, expires=None)
    assert read_token(tmp_path, NOW) == "secret"


@pytest.mark.parametrize("text", ["not json", "{}", '{"claudeAiOauth": []}', "[]"])
def test_an_unreadable_login_is_no_token(tmp_path, text):
    (tmp_path / ".credentials.json").write_text(text, encoding="utf-8")
    assert read_token(tmp_path, NOW) is None


@pytest.mark.parametrize("token", ["", None, 7])
def test_a_token_that_is_not_one_is_none(tmp_path, token):
    write_login(tmp_path, token=token)
    assert read_token(tmp_path, NOW) is None


def test_the_login_is_never_written(tmp_path):
    write_login(tmp_path)
    before = (tmp_path / ".credentials.json").read_bytes()
    sent: list[dict[str, Any]] = []

    async def run() -> None:
        meters = Usage(
            Settings(claude_home=tmp_path),
            sent.append,
            asyncio.get_running_loop(),
            fetcher=lambda _url, _token: json.dumps(CAPTURED),
            clock=lambda: NOW,
        )
        await meters.poll()

    asyncio.run(run())
    assert sent
    assert (tmp_path / ".credentials.json").read_bytes() == before


# --- the reply ---


def test_the_captured_reply_gives_the_three_limits():
    limits = parse_limits(json.dumps(CAPTURED))
    assert [(limit.label, limit.percent) for limit in limits] == [
        ("5-hour", 4.0),
        ("Weekly", 27.0),
        ("Fable weekly", 0.0),
    ]
    assert limits[1].resets_at == pytest.approx(WEEK_RESET, abs=1)
    assert limits[0].window_seconds == 5 * 3600
    assert limits[1].window_seconds == limits[2].window_seconds == 7 * 86400


@pytest.mark.parametrize(
    ("entry", "label"),
    [
        ({"kind": "session"}, "5-hour"),
        ({"kind": "weekly_all"}, "Weekly"),
        (
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "scope": {"model": {"display_name": "Fable"}},
            },
            "Fable weekly",
        ),
        (
            {
                "kind": "daily_scoped",
                "group": "daily",
                "scope": {"model": {"display_name": "Opus"}},
            },
            "Opus",
        ),
        ({"kind": "weekly_scoped", "scope": None}, "Weekly scoped"),
        ({"kind": "weekly_scoped", "scope": {"model": None}}, "Weekly scoped"),
        ({}, "Limit"),
    ],
)
def test_a_limit_is_named(entry, label):
    assert label_of(entry) == label


@pytest.mark.parametrize("text", ["not json", "{}", '{"limits": null}', '{"limits": {}}', "[]"])
def test_a_reply_that_is_not_one_raises(text):
    with pytest.raises(UsageError):
        parse_limits(text)


def test_an_entry_that_is_not_a_limit_is_skipped():
    entries = ["x", {"kind": "a"}, {"kind": "b", "percent": True}, {"kind": "c", "percent": 5}]
    assert [limit.kind for limit in parse_limits(json.dumps({"limits": entries}))] == ["c"]


@pytest.mark.parametrize("resets", [None, "soon", 17])
def test_a_limit_with_no_reset_has_no_window(resets):
    entry = {"kind": "session", "group": "session", "percent": 1, "resets_at": resets}
    [limit] = parse_limits(json.dumps({"limits": [entry]}))
    assert limit.resets_at is None
    assert limit.window_seconds is None


def test_a_group_of_unknown_length_has_no_window():
    entry = {
        "kind": "x",
        "group": "monthly",
        "percent": 1,
        "resets_at": "2026-12-02T06:00:00+00:00",
    }
    [limit] = parse_limits(json.dumps({"limits": [entry]}))
    assert limit.resets_at is not None
    assert limit.window_seconds is None


# --- the pace ---


def weekly(resets_at: float | None = WEEK_RESET) -> Limit:
    return Limit("weekly_all", "Weekly", 27.0, resets_at, 7 * 86400.0)


def test_two_days_into_a_week_is_two_sevenths_through():
    assert elapsed_share(weekly(), NOW) == pytest.approx(2 / 7)


def test_the_share_is_held_between_none_and_all():
    assert elapsed_share(weekly(), WEEK_RESET + 60) == 1.0
    assert elapsed_share(weekly(), WEEK_RESET - 8 * 86400) == 0.0


def test_no_window_is_no_share():
    assert elapsed_share(weekly(None), NOW) is None
    assert elapsed_share(Limit("x", "X", 1.0, WEEK_RESET, None), NOW) is None


def test_the_message_carries_what_the_page_needs_for_the_pace():
    message = describe([weekly()])
    assert message == {
        "type": "usage",
        "limits": [
            {
                "kind": "weekly_all",
                "label": "Weekly",
                "percent": 27.0,
                "resets_at": WEEK_RESET,
                "window_seconds": 7 * 86400.0,
            }
        ],
    }


def test_before_an_answer_there_are_no_limits():
    assert describe(None) == {"type": "usage", "limits": None}


# --- asking ---


class Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_fetch_sends_the_login_as_claude_code_does(monkeypatch):
    seen = {}

    def urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return Response(b'{"limits": []}')

    monkeypatch.setattr(usage.urllib.request, "urlopen", urlopen)
    assert fetch("https://example.invalid/usage", "secret") == '{"limits": []}'
    request = seen["request"]
    assert request.get_header("Authorization") == "Bearer secret"
    assert request.get_header("Anthropic-beta") == usage.BETA
    assert seen["timeout"] == usage.TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError("u", 401, "no", Message(), None),
        urllib.error.URLError("offline"),
        OSError("reset"),
    ],
)
def test_fetch_raises_one_error_for_any_failure(monkeypatch, error):
    def urlopen(_request, timeout):
        raise error

    monkeypatch.setattr(usage.urllib.request, "urlopen", urlopen)
    with pytest.raises(UsageError):
        fetch("https://example.invalid/usage", "secret")


def test_fetch_reads_a_file_url(tmp_path):
    reply = tmp_path / "reply.json"
    reply.write_text('{"limits": []}', encoding="utf-8")
    assert fetch(reply.as_uri(), "secret") == '{"limits": []}'


class Fetcher:
    def __init__(self, replies: list[str | Exception]) -> None:
        self.replies = replies
        self.asked: list[tuple[str, str]] = []

    def __call__(self, url: str, token: str) -> str:
        self.asked.append((url, token))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def drive(tmp_path: Path, fetcher: Fetcher, polls: int, login: bool = True) -> list[dict[str, Any]]:
    if login:
        write_login(tmp_path)
    sent: list[dict[str, Any]] = []

    async def run() -> None:
        settings = Settings(claude_home=tmp_path, usage_url="https://example.invalid/u")
        meters = Usage(settings, sent.append, asyncio.get_running_loop(), fetcher, lambda: NOW)
        for _ in range(polls):
            await meters.poll()

    asyncio.run(run())
    return sent


def test_a_poll_sends_the_limits(tmp_path):
    fetcher = Fetcher([json.dumps(CAPTURED)])
    [message] = drive(tmp_path, fetcher, 1)
    assert [row["label"] for row in message["limits"]] == ["5-hour", "Weekly", "Fable weekly"]
    assert fetcher.asked == [("https://example.invalid/u", "secret")]


def test_an_unchanged_reply_is_not_sent_again(tmp_path):
    sent = drive(tmp_path, Fetcher([json.dumps(CAPTURED)] * 2), 2)
    assert len(sent) == 1


def test_a_failure_keeps_what_was_shown(tmp_path):
    fetcher = Fetcher([json.dumps(CAPTURED), UsageError("down"), "not json"])
    sent = drive(tmp_path, fetcher, 3)
    assert len(sent) == 1


def test_with_no_login_nothing_is_asked(tmp_path):
    fetcher = Fetcher([])
    assert drive(tmp_path, fetcher, 2, login=False) == []
    assert fetcher.asked == []


def test_a_failure_is_logged_once(tmp_path, caplog):
    drive(tmp_path, Fetcher([UsageError("down"), UsageError("down")]), 2)
    assert [r.message for r in caplog.records if r.name == usage.__name__] == ["down"]


def test_a_reloaded_page_is_told_again(tmp_path):
    write_login(tmp_path)
    sent: list[dict[str, Any]] = []

    async def run() -> None:
        settings = Settings(claude_home=tmp_path, usage_poll_seconds=3600)
        fetcher = Fetcher([json.dumps(CAPTURED)])
        meters = Usage(settings, sent.append, asyncio.get_running_loop(), fetcher, lambda: NOW)
        await meters.handlers()["usage"]({})
        while not sent:
            await asyncio.sleep(0.01)
        await meters.handlers()["usage"]({})
        meters.stop()
        assert len(fetcher.asked) == 1

    asyncio.run(run())
    assert len(sent) == 2
    assert sent[0] == sent[1]

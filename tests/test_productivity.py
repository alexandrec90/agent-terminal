from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from agent_terminal import productivity
from agent_terminal.own_prs import Evidence
from agent_terminal.productivity import (
    CreatedPr,
    Productivity,
    count_prompts,
    count_prs,
    describe,
    fetch_prs,
    is_prompt,
    load_prs,
    mark_own,
    parse_search,
    save_prs,
    summarize,
)
from agent_terminal.prs import GhError, GhMissing
from agent_terminal.settings import Settings

TODAY = date(2026, 11, 25)
YESTERDAY = TODAY - timedelta(days=1)


def history_line(text: str, day: date = TODAY, hour: int = 12) -> str:
    """A record as Claude Code 2.1 writes it."""
    at = datetime(day.year, day.month, day.day, hour)
    return json.dumps(
        {
            "display": text,
            "pastedContents": {},
            "timestamp": int(at.timestamp() * 1000),
            "project": "C:\\code",
            "sessionId": "s",
        }
    )


def stamp(minute: int) -> str:
    return (datetime(2026, 11, 20) + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ")


def reply(prs: list[CreatedPr]) -> str:
    nodes = [{"url": p.url, "createdAt": p.created_at, "headRefName": p.head} for p in prs]
    return json.dumps({"data": {"search": {"nodes": nodes}}})


class FakeGh:
    """Answers a search the way GitHub does: oldest first, from `created:>=`, one page."""

    def __init__(self, prs: list[CreatedPr] | None = None, error: Exception | None = None) -> None:
        self.prs = prs or []
        self.error = error
        self.queries: list[str] = []

    def run(self, _where: Path, *args: str) -> str:
        if self.error:
            raise self.error
        query = next(arg for arg in args if arg.startswith("q="))
        self.queries.append(query)
        _, _, since = query.partition("created:>=")
        matching = sorted(
            (pr for pr in self.prs if pr.created_at >= since), key=lambda pr: pr.created_at
        )
        return reply(matching[: productivity.PAGE_SIZE])


@pytest.mark.parametrize(
    ("text", "counted"),
    [
        ("fix the tests", True),
        ("/land", True),
        ("/ship now", True),
        ("/clear", False),
        ("/model opus", False),
        ("", False),
        ("   ", False),
        ("You are a fixer session, dispatched by the fix pass. Read", False),
    ],
)
def test_is_prompt(text, counted):
    assert is_prompt(text) is counted


def test_prompts_are_counted_by_local_day():
    lines = [
        history_line("a", YESTERDAY, hour=23),
        history_line("b", TODAY, hour=0),
        history_line("c"),
        history_line("/clear"),
    ]
    assert count_prompts(lines) == {YESTERDAY: 1, TODAY: 2}


def test_lines_that_are_not_records_are_skipped():
    lines = ["", "{half", "[]", '{"display": 1, "timestamp": 1}', '{"display": "x"}']
    assert count_prompts([*lines, history_line("a")]) == {TODAY: 1}


def test_only_the_users_own_prs_are_counted():
    at = datetime(2026, 11, 25, 12).astimezone().isoformat()
    prs = [CreatedPr("1", at, "a", own=True), CreatedPr("2", at, "b")]
    assert count_prs(prs) == {TODAY: 1}


def test_a_mark_outlives_its_evidence():
    evidence = Evidence(links={"1": datetime.fromisoformat(stamp(0)).timestamp()})
    prs = {url: CreatedPr(url, stamp(0), "h") for url in ("1", "2")}
    marked = mark_own(prs, evidence)
    assert {url for url, pr in marked.items() if pr.own} == {"1"}
    assert mark_own(marked, Evidence()) == marked


def test_a_known_pr_fetched_again_keeps_its_mark():
    known = {"1": CreatedPr("1", stamp(0), "h", own=True)}
    assert fetch_prs(FakeGh([CreatedPr("1", stamp(0), "h")]), Path(), known) == known


@pytest.mark.parametrize(
    "text", ["", "{}", '{"data": {"search": null}}', reply([CreatedPr("1", "soon", "h")])]
)
def test_an_unexpected_reply_is_an_error(text):
    with pytest.raises(GhError, match="unexpected reply"):
        parse_search(text)


def test_every_page_is_fetched():
    gh = FakeGh([CreatedPr(str(n), stamp(n), "h") for n in range(250)])
    assert len(fetch_prs(gh, Path(), {})) == 250
    assert "created" not in gh.queries[0].replace("sort:created-asc", "")


def test_a_page_all_in_one_second_does_not_loop():
    gh = FakeGh([CreatedPr(str(n), stamp(0), "h") for n in range(150)])
    assert len(fetch_prs(gh, Path(), {})) == productivity.PAGE_SIZE


def test_only_what_is_new_is_asked_for():
    known = {"old": CreatedPr("old", "2026-11-20T12:00:00Z", "h")}
    gh = FakeGh([CreatedPr("new", "2026-11-21T00:00:00Z", "h")])
    assert set(fetch_prs(gh, Path(), known)) == {"old", "new"}
    assert gh.queries[0].endswith("created:>=2026-11-19T12:00:00Z")


def test_prs_survive_a_save(tmp_path):
    path = tmp_path / "data" / "prs.json"
    prs = {"1": CreatedPr("1", stamp(0), "h")}
    save_prs(path, prs)
    assert load_prs(path) == prs


@pytest.mark.parametrize("text", [None, "{bad", "[[1]]", '[["1", "soon", "h"]]', "3"])
def test_unreadable_saved_prs_are_fetched_afresh(tmp_path, text):
    path = tmp_path / "prs.json"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    assert load_prs(path) == {}


def test_the_average_leaves_out_today_and_days_off():
    counts = {date(2026, 11, 20): 4, date(2026, 11, 21): 0, YESTERDAY: 7, TODAY: 1}
    assert summarize(counts, TODAY) == {
        "today": 1,
        "average": 5.5,
        "record": 7,
        "record_day": "2026-11-24",
    }


def test_today_can_be_the_record_but_a_tie_is_the_first_days():
    assert summarize({YESTERDAY: 2, TODAY: 3}, TODAY)["record_day"] == "2026-11-25"
    assert summarize({YESTERDAY: 3, TODAY: 3}, TODAY)["record_day"] == "2026-11-24"


def test_nothing_yet():
    assert summarize({}, TODAY) == {"today": 0, "average": None, "record": 0, "record_day": None}


def test_every_day_is_described_and_one_not_recorded_is_null():
    message = describe({YESTERDAY: 2}, {date(2026, 11, 23): 1}, TODAY)
    assert message["days"] == [
        {"day": "2026-11-23", "prompts": None, "prs": 1},
        {"day": "2026-11-24", "prompts": 2, "prs": 0},
        {"day": "2026-11-25", "prompts": 0, "prs": 0},
    ]


def test_before_github_answers_there_are_no_prs_to_show():
    message = describe({}, None, TODAY)
    assert message["prs"] is None
    assert message["days"] == [{"day": "2026-11-25", "prompts": 0, "prs": None}]


def settings_in(tmp_path: Path) -> Settings:
    (tmp_path / "claude").mkdir()
    return Settings(
        projects_root=tmp_path,
        data_dir=tmp_path / "data",
        claude_home=tmp_path / "claude",
        gh_command=str(tmp_path / "no-gh"),
    )


def drive(tmp_path: Path, gh: FakeGh, body) -> list[dict]:
    sent: list[dict] = []

    async def run() -> None:
        counts = Productivity(
            settings_in(tmp_path),
            sent.append,
            asyncio.get_running_loop(),
            gh=gh,
            today=lambda: TODAY,
        )
        try:
            await body(counts)
        finally:
            counts.stop()

    asyncio.run(run())
    return sent


def test_a_new_prompt_is_sent_and_no_change_is_not(tmp_path):
    async def body(counts):
        history = tmp_path / "claude" / "history.jsonl"
        history.write_text(history_line("a") + "\n", encoding="utf-8")
        await counts.scan_prompts()
        await counts.scan_prompts()
        with history.open("a", encoding="utf-8") as handle:
            handle.write(history_line("b") + "\n")
        await counts.scan_prompts()

    sent = drive(tmp_path, FakeGh(), body)
    assert [message["prompts"]["today"] for message in sent] == [1, 2]


def test_without_a_history_there_are_no_prompts(tmp_path):
    sent = drive(tmp_path, FakeGh(), lambda counts: counts.scan_prompts())
    assert sent[0]["prompts"]["record"] == 0


def test_prs_are_sent_and_saved(tmp_path):
    url = "https://github.com/o/demo/pull/1"
    pr = CreatedPr(url, "2026-11-25T12:00:00Z", "worktree-x")
    robot = CreatedPr("2", "2026-11-25T12:00:00Z", "agent/auto/upgrade")
    record = json.loads(history_line("a", YESTERDAY - timedelta(days=1)))
    record["project"] = "C:\\code\\demo\\.claude\\worktrees\\x"

    async def body(counts):
        history = tmp_path / "claude" / "history.jsonl"
        history.write_text(json.dumps(record) + "\n", encoding="utf-8")
        await counts.poll_prs()

    sent = drive(tmp_path, FakeGh([pr, robot]), body)
    assert sent[-1]["prs"]["record"] == 1
    saved = load_prs(tmp_path / "data" / "created-prs.json")
    assert {key: entry.own for key, entry in saved.items()} == {url: True, "2": False}


def test_saved_prs_show_while_github_cannot_be_reached(tmp_path):
    save_prs(tmp_path / "data" / "created-prs.json", {"1": CreatedPr("1", stamp(0), "h", True)})
    sent = drive(tmp_path, FakeGh(error=GhError("offline")), lambda counts: counts.poll_prs())
    assert sent[-1]["prs"]["record"] == 1


def test_a_failure_is_logged_once(tmp_path, caplog):
    async def body(counts):
        await counts.poll_prs()
        await counts.poll_prs()

    assert drive(tmp_path, FakeGh(error=GhError("offline")), body) == []
    assert caplog.text.count("offline") == 1


def test_without_gh_it_is_not_asked_again(tmp_path):
    gh = FakeGh(error=GhMissing("no gh"))

    async def body(counts):
        await counts.poll_prs()
        gh.error = None
        await counts.poll_prs()

    drive(tmp_path, gh, body)
    assert gh.queries == []


def test_a_page_that_reloads_is_told_again(tmp_path):
    async def body(counts):
        await counts.handlers()["productivity"]({})
        for _ in range(200):
            if counts._sent and counts._sent["prs"]:
                break
            await asyncio.sleep(0.01)
        await counts.handlers()["productivity"]({})

    sent = drive(tmp_path, FakeGh(), body)
    assert sent[-1] == sent[-2]
    assert sent[-1]["prs"]["today"] == 0

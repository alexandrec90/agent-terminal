import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agent_terminal import limits as module
from agent_terminal.attention import ASLEEP, BLOCKED, IDLE, SHELL, STARTING, WORKING
from agent_terminal.limits import (
    LimitLinks,
    LimitWatcher,
    is_due,
    last_conversation_record,
    limit_reset,
    limited_until,
    lines_from_end,
)
from agent_terminal.runner import LiveTab, RunnerEvents, TabRunner
from agent_terminal.sessions import LocalSessions
from agent_terminal.settings import Settings
from agent_terminal.store import Tab

RESETS_AT = 1795739400


def limit_record(resets_at: Any = RESETS_AT, **fields: Any) -> dict[str, Any]:
    """The record Claude Code 2.1.332 wrote for a real turn refused by the 5-hour limit,
    cut to the fields that matter."""
    return {
        "parentUuid": "6aee2d60",
        "isSidechain": False,
        "type": "assistant",
        "message": {
            "model": "<synthetic>",
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "You've hit your 5-hour limit \N{MIDDLE DOT} resets 7:30pm (America/Toronto)",
                }
            ],
        },
        "apiError": "usage_limit_reached",
        "apiErrorParams": {
            "rate_limit_info": {
                "status": "rejected",
                "resetsAt": resets_at,
                "rateLimitType": "five_hour",
            }
        },
        "quotaLimits": {"status": "rejected", "resetsAt": resets_at, "rateLimitType": "five_hour"},
        "error": "rate_limit",
        "isApiErrorMessage": True,
        "apiErrorStatus": 429,
        **fields,
    }


def user_record(text: str = "resume", **fields: Any) -> dict[str, Any]:
    return {
        "isSidechain": False,
        "type": "user",
        "message": {"role": "user", "content": text},
        **fields,
    }


# Written between turns, after the conversation records: none of them is one.
NOISE = [
    {"type": "system", "subtype": "turn_duration", "isSidechain": False},
    {"type": "data-flow-records", "v": 1, "records": []},
    {"type": "api-request-blob", "message": {"role": "user", "content": "x" * 5000}},
    {"type": "file-history-snapshot", "messageId": "m"},
]


def write_transcript(path: Path, *records: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Compact, as Claude writes them.
    lines = [json.dumps(record, separators=(",", ":")) for record in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestLimitReset:
    def test_a_refused_turn_says_when_its_limit_resets(self):
        assert limit_reset(limit_record()) == RESETS_AT

    def test_the_quota_record_is_used_when_the_error_has_no_time(self):
        record = limit_record()
        record["apiErrorParams"] = {}
        assert limit_reset(record) == RESETS_AT

    @pytest.mark.parametrize("resets_at", [None, "soon", True, 0, -5])
    def test_a_time_that_is_not_one_is_none(self, resets_at):
        record = limit_record(resets_at)
        assert limit_reset(record) is None

    def test_another_error_is_not_the_limit(self):
        assert limit_reset(limit_record(apiError="overloaded")) is None

    @pytest.mark.parametrize("record", [user_record(), {}, [], None, "text"])
    def test_anything_else_is_not_the_limit(self, record):
        assert limit_reset(record) is None


class TestLinesFromEnd:
    def test_lines_come_last_first(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_bytes(b"one\ntwo\nthree\n")
        assert list(lines_from_end(path)) == [b"", b"three", b"two", b"one"]

    def test_lines_across_chunks_are_whole(self, tmp_path, monkeypatch):
        monkeypatch.setattr(module, "TAIL_CHUNK_BYTES", 4)
        path = tmp_path / "t.jsonl"
        path.write_bytes(b"first line\nsecond line\nthird")
        assert list(lines_from_end(path)) == [b"third", b"second line", b"first line"]

    def test_reading_stops_at_the_bound_and_gives_no_cut_line(self, tmp_path, monkeypatch):
        monkeypatch.setattr(module, "TAIL_CHUNK_BYTES", 4)
        path = tmp_path / "t.jsonl"
        path.write_bytes(b"aaaaaaaaaa\nbb\ncc")
        assert list(lines_from_end(path, max_bytes=8)) == [b"cc", b"bb"]

    def test_an_empty_file_has_one_empty_line(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_bytes(b"")
        assert list(lines_from_end(path)) == [b""]


class TestLastConversationRecord:
    def test_records_between_turns_are_passed_over(self, tmp_path):
        path = write_transcript(tmp_path / "t.jsonl", user_record(), limit_record(), *NOISE)
        assert last_conversation_record(path) == limit_record()

    def test_a_subagents_record_is_not_the_sessions(self, tmp_path):
        side = user_record("from a subagent", isSidechain=True)
        path = write_transcript(tmp_path / "t.jsonl", limit_record(), side)
        assert last_conversation_record(path) == limit_record()

    def test_a_broken_line_is_passed_over(self, tmp_path):
        path = write_transcript(tmp_path / "t.jsonl", limit_record())
        with path.open("a", encoding="utf-8") as file:
            file.write('{"type":"user", broken\n')
        assert last_conversation_record(path) == limit_record()

    def test_a_transcript_with_no_conversation_has_none(self, tmp_path):
        path = write_transcript(tmp_path / "t.jsonl", *NOISE)
        assert last_conversation_record(path) is None


class TestLimitedUntil:
    def test_a_session_whose_last_turn_was_refused_is_held(self, tmp_path):
        path = write_transcript(tmp_path / "t.jsonl", user_record(), limit_record(), *NOISE)
        assert limited_until(path) == RESETS_AT

    def test_a_session_asked_again_since_is_not(self, tmp_path):
        # As in the real session: the user typed after the limit, and the turn ran.
        path = write_transcript(tmp_path / "t.jsonl", limit_record(), user_record(), *NOISE)
        assert limited_until(path) is None

    def test_a_change_to_the_transcript_is_read(self, tmp_path):
        path = write_transcript(tmp_path / "t.jsonl", limit_record())
        assert limited_until(path) == RESETS_AT
        write_transcript(path, limit_record(), user_record("continue"))
        assert limited_until(path) is None

    def test_a_missing_transcript_is_not_held(self, tmp_path):
        assert limited_until(tmp_path / "gone.jsonl") is None


class FakeSession:
    def __init__(self, writes: list[str]) -> None:
        self.writes = writes

    def start(self, *_: Any) -> None: ...
    def close(self) -> None: ...
    def write(self, text: str) -> None:
        self.writes.append(text)

    def resize(self, rows: int, cols: int) -> None: ...
    def remember(self, state: dict[str, Any]) -> None: ...


class FakeSessions(LocalSessions):
    def __init__(self, writes: list[str]) -> None:
        super().__init__()
        self.writes = writes

    def new(self, *_: Any) -> FakeSession:
        return FakeSession(self.writes)


class Rig:
    def __init__(self, tmp_path: Path, resume: bool = True) -> None:
        self.tmp_path = tmp_path
        loop = asyncio.get_running_loop()
        self.writes: list[str] = []
        self.woken: list[tuple[str, str]] = []
        self.changes = 0
        self.now = float(RESETS_AT - 3600)
        settings = Settings(
            data_dir=tmp_path,
            claude_home=tmp_path,
            claude_config_file=tmp_path / "claude.json",
            codex_home=tmp_path,
            resume_after_limit=resume,
        )
        self.runner = TabRunner(
            settings,
            loop,
            FakeSessions(self.writes),
            RunnerEvents(lambda *_: None, self.changed, lambda _: None, lambda _: None),
        )
        links = LimitLinks(self.changed, self.wake)
        self.watcher = LimitWatcher(settings, loop, self.runner, links, lambda: self.now)

    def changed(self) -> None:
        self.changes += 1

    def wake(self, live: LiveTab, prompt: str) -> None:
        self.woken.append((live.tab.id, prompt))

    def add(self, tab_id: str = "t", status: str = IDLE, **fields: Any) -> LiveTab:
        defaults = {"agent": "claude", "session_id": f"s-{tab_id}", "created_at": 0.0}
        tab = Tab(id=tab_id, project="p", project_path=str(self.tmp_path), **{**defaults, **fields})
        live = self.runner.add(tab)
        live.status = status
        return live

    def transcript(self, tab_id: str = "t", *records: dict[str, Any]) -> Path:
        path = self.tmp_path / "projects" / "C--p" / f"s-{tab_id}.jsonl"
        return write_transcript(path, *(records or (user_record(), limit_record(), *NOISE)))

    async def typed(self) -> None:
        """Let a resume finish typing."""
        await asyncio.gather(*self.watcher._resuming)


def run(tmp_path: Path, body: Any, **options: Any) -> None:
    async def go() -> None:
        await body(Rig(tmp_path, **options))

    asyncio.run(go())


@pytest.fixture(autouse=True)
def quick_enter(monkeypatch):
    monkeypatch.setattr(module, "ENTER_DELAY_SECONDS", 0)


def test_a_tab_stopped_by_the_limit_shows_when_it_resets(tmp_path):
    async def body(rig: Rig) -> None:
        live = rig.add()
        rig.transcript()
        await rig.watcher.sweep()
        assert live.limited_until == RESETS_AT
        assert rig.changes == 1
        assert rig.writes == [], "nothing is typed before the reset"
        await rig.watcher.sweep()
        assert rig.changes == 1, "the same limit is not news"

    run(tmp_path, body)


def test_once_the_limit_resets_the_tab_is_told_to_continue_once(tmp_path):
    async def body(rig: Rig) -> None:
        live = rig.add()
        rig.transcript()
        rig.now = RESETS_AT + module.RESUME_GRACE_SECONDS
        await rig.watcher.sweep()
        await rig.typed()
        assert rig.writes == ["continue", "\r"]
        assert live.resumed_for == RESETS_AT
        await rig.watcher.sweep()
        await rig.typed()
        assert rig.writes == ["continue", "\r"], "the transcript may not have caught up yet"

    run(tmp_path, body)


def test_not_before_the_grace_after_the_reset(tmp_path):
    async def body(rig: Rig) -> None:
        rig.add()
        rig.transcript()
        rig.now = RESETS_AT + 1
        await rig.watcher.sweep()
        assert rig.writes == []

    run(tmp_path, body)


def test_the_limit_clears_once_the_session_goes_on(tmp_path):
    async def body(rig: Rig) -> None:
        live = rig.add()
        rig.transcript()
        await rig.watcher.sweep()
        rig.transcript("t", limit_record(), user_record("continue"))
        await rig.watcher.sweep()
        assert live.limited_until is None
        assert rig.changes == 2

    run(tmp_path, body)


def test_a_new_limit_after_a_resume_is_resumed_again(tmp_path):
    async def body(rig: Rig) -> None:
        live = rig.add()
        rig.transcript()
        rig.now = RESETS_AT + module.RESUME_GRACE_SECONDS
        await rig.watcher.sweep()
        await rig.typed()
        later = RESETS_AT + 5 * 3600
        rig.transcript("t", limit_record(), user_record("continue"), limit_record(later))
        rig.now = later + module.RESUME_GRACE_SECONDS
        await rig.watcher.sweep()
        await rig.typed()
        assert rig.writes == ["continue", "\r", "continue", "\r"]
        assert live.resumed_for == later

    run(tmp_path, body)


def test_an_asleep_tab_is_woken_with_the_message(tmp_path):
    async def body(rig: Rig) -> None:
        live = rig.add(status=ASLEEP, asleep=True)
        rig.transcript()
        rig.now = RESETS_AT + module.RESUME_GRACE_SECONDS
        await rig.watcher.sweep()
        assert rig.woken == [("t", "continue")]
        assert rig.writes == []
        assert live.resumed_for == RESETS_AT

    run(tmp_path, body)


def test_turned_off_the_tab_still_shows_the_reset_but_is_left_alone(tmp_path):
    async def body(rig: Rig) -> None:
        live = rig.add()
        rig.transcript()
        rig.now = RESETS_AT + module.RESUME_GRACE_SECONDS
        await rig.watcher.sweep()
        assert live.limited_until == RESETS_AT
        assert rig.writes == []

    run(tmp_path, body, resume=False)


def test_text_left_in_the_prompt_holds_the_resume_until_it_is_sent(tmp_path):
    async def body(rig: Rig) -> None:
        live = rig.add()
        live.drafting = True
        rig.transcript()
        rig.now = RESETS_AT + module.RESUME_GRACE_SECONDS
        await rig.watcher.sweep()
        assert rig.writes == []
        live.drafting = False
        await rig.watcher.sweep()
        await rig.typed()
        assert rig.writes == ["continue", "\r"]

    run(tmp_path, body)


@pytest.mark.parametrize("status", [STARTING, SHELL])
def test_a_tab_with_no_agent_session_running_is_not_read(tmp_path, status):
    async def body(rig: Rig) -> None:
        live = rig.add(status=status)
        rig.transcript()
        await rig.watcher.sweep()
        assert live.limited_until is None

    run(tmp_path, body)


def test_codex_and_a_tab_with_no_session_are_not_read(tmp_path):
    async def body(rig: Rig) -> None:
        codex = rig.add("c", agent="codex")
        rig.transcript("c")
        fresh = rig.add("n", session_id=None)
        await rig.watcher.sweep()
        assert codex.limited_until is None
        assert fresh.limited_until is None

    run(tmp_path, body)


def test_a_tab_closed_during_the_read_is_left_alone(tmp_path, monkeypatch):
    async def body(rig: Rig) -> None:
        live = rig.add()
        rig.transcript()
        rig.now = RESETS_AT + module.RESUME_GRACE_SECONDS

        def read_and_close(home: Path, session_id: str) -> float | None:
            rig.runner.live.pop("t")
            return module.limited_until(home / "projects" / "C--p" / f"{session_id}.jsonl")

        monkeypatch.setattr(module, "session_limited_until", read_and_close)
        await rig.watcher.sweep()
        assert live.limited_until is None
        assert rig.writes == []

    run(tmp_path, body)


def test_a_tab_closed_before_it_is_typed_into_gets_nothing(tmp_path):
    async def body(rig: Rig) -> None:
        rig.add()
        rig.transcript()
        rig.now = RESETS_AT + module.RESUME_GRACE_SECONDS
        await rig.watcher.sweep()
        rig.runner.live.pop("t")
        await rig.typed()
        assert rig.writes == []

    run(tmp_path, body)


def test_enter_is_not_sent_to_a_tab_closed_after_the_text(tmp_path, monkeypatch):
    async def body(rig: Rig) -> None:
        rig.add()
        rig.transcript()
        rig.now = RESETS_AT + module.RESUME_GRACE_SECONDS
        real_sleep = asyncio.sleep

        async def close_meanwhile(seconds: float) -> None:
            rig.runner.live.pop("t", None)
            await real_sleep(seconds)

        monkeypatch.setattr(module.asyncio, "sleep", close_meanwhile)
        await rig.watcher.sweep()
        await rig.typed()
        assert rig.writes == ["continue"]

    run(tmp_path, body)


class TestIsDue:
    def live(self, **fields: Any) -> LiveTab:
        tab = Tab(id="t", project="p", project_path=".", agent="claude", created_at=0.0)
        live = LiveTab(tab=tab, session=FakeSession([]), status=IDLE)
        live.limited_until = RESETS_AT
        for name, value in fields.items():
            setattr(live, name, value)
        return live

    after = RESETS_AT + module.RESUME_GRACE_SECONDS

    @pytest.mark.parametrize("status", [IDLE, BLOCKED, WORKING, ASLEEP])
    def test_due_after_the_reset(self, status):
        assert is_due(self.live(status=status), self.after, enabled=True)

    def test_not_when_turned_off(self):
        assert not is_due(self.live(), self.after, enabled=False)

    def test_not_when_not_held(self):
        assert not is_due(self.live(limited_until=None), self.after, enabled=True)

    def test_not_twice_for_one_limit(self):
        assert not is_due(self.live(resumed_for=RESETS_AT), self.after, enabled=True)

    def test_not_on_the_trust_screen(self):
        assert not is_due(self.live(awaiting_trust=True), self.after, enabled=True)

    @pytest.mark.parametrize("status", [STARTING, SHELL])
    def test_not_without_the_agent(self, status):
        assert not is_due(self.live(status=status), self.after, enabled=True)


def test_it_checks_by_itself_until_stopped_and_outlives_a_bad_check(tmp_path, monkeypatch):
    async def body(rig: Rig) -> None:
        monkeypatch.setattr(module, "CHECK_SECONDS", 0.01)
        calls = 0

        async def sweep() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("refused")

        monkeypatch.setattr(rig.watcher, "sweep", sweep)
        rig.watcher.start()
        rig.watcher.start()
        for _ in range(200):
            if calls >= 3:
                break
            await asyncio.sleep(0.01)
        rig.watcher.stop()
        rig.watcher.stop()
        seen = calls
        await asyncio.sleep(0.05)
        assert seen >= 3
        assert calls == seen

    run(tmp_path, body)

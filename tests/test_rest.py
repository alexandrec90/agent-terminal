import json
import os
from pathlib import Path

import pytest

from agent_terminal.processes import TICKS_PER_SECOND
from agent_terminal.rest import (
    ProcessView,
    can_rest,
    last_written,
    read_record,
    running_record,
    schedules_itself,
)

PID = 500
STARTED = 1000 * TICKS_PER_SECOND
LONG_AGO = 1_000_000.0
SINCE = LONG_AGO + 60


def created(pid: int) -> int | None:
    return {PID: STARTED, 501: STARTED + 5 * TICKS_PER_SECOND, 502: STARTED * 2}.get(pid)


def view(children: tuple[int, ...] = ()) -> ProcessView:
    return ProcessView(lambda: {PID: 1, **dict.fromkeys(children, PID)}, created)


def write_record(home: Path, name: str = "500", **fields) -> Path:
    folder = home / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    record = {"pid": PID, "sessionId": "s", "procStart": str(STARTED), "status": "idle", **fields}
    path = folder / f"{name}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def age(path: Path) -> Path:
    os.utime(path, (LONG_AGO, LONG_AGO))
    return path


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    path = tmp_path / "projects" / "encoded" / "s.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{}\n", encoding="utf-8")
    return age(path)


def test_an_idle_quiet_session_can_rest(tmp_path, transcript):
    write_record(tmp_path)
    assert can_rest(tmp_path, "s", SINCE, view())


def test_what_started_with_the_agent_does_not_keep_it_up(tmp_path, transcript):
    write_record(tmp_path)
    assert can_rest(tmp_path, "s", SINCE, view(children=(501,)))


def test_a_background_command_keeps_it_up(tmp_path, transcript):
    write_record(tmp_path)
    assert not can_rest(tmp_path, "s", SINCE, view(children=(502,)))


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "busy"},
        {"status": None},
        # After `/clear`, the process is in another session than the tab's.
        {"sessionId": "another"},
        # Left by a killed process, whose pid is now something else's.
        {"procStart": "7"},
        {"pid": "500"},
    ],
)
def test_a_record_that_does_not_vouch_for_it_keeps_it_up(tmp_path, transcript, fields):
    write_record(tmp_path, **fields)
    assert not can_rest(tmp_path, "s", SINCE, view())


def test_no_record_keeps_it_up(tmp_path, transcript):
    assert not can_rest(tmp_path, "s", SINCE, view())


def test_one_session_in_two_processes_keeps_it_up(tmp_path):
    write_record(tmp_path)
    assert running_record(tmp_path, "s", created) is not None
    write_record(tmp_path, "501", pid=501, procStart=str(created(501)))
    assert running_record(tmp_path, "s", created) is None


def test_an_unreadable_process_list_keeps_it_up(tmp_path, transcript):
    write_record(tmp_path)
    assert not can_rest(tmp_path, "s", SINCE, ProcessView(lambda: None, created))


def test_a_session_with_no_transcript_has_nothing_to_resume(tmp_path):
    write_record(tmp_path)
    assert not can_rest(tmp_path, "s", SINCE, view())


def test_a_subagent_still_writing_keeps_it_up(tmp_path, transcript):
    write_record(tmp_path)
    subagent = transcript.with_suffix("") / "subagents" / "a.jsonl"
    subagent.parent.mkdir(parents=True)
    subagent.write_text("{}\n", encoding="utf-8")
    assert last_written(transcript) > SINCE
    assert not can_rest(tmp_path, "s", SINCE, view())
    age(subagent)
    assert can_rest(tmp_path, "s", SINCE, view())


@pytest.mark.parametrize("tool", ["CronCreate", "ScheduleWakeup"])
def test_a_session_that_set_itself_a_time_stays_up(tmp_path, transcript, tool):
    write_record(tmp_path)
    call = {"message": {"content": [{"type": "tool_use", "id": "toolu_1", "name": tool}]}}
    transcript.write_text(json.dumps(call) + "\n", encoding="utf-8")
    age(transcript)
    assert schedules_itself(transcript)
    assert not can_rest(tmp_path, "s", SINCE, view())


def test_listing_the_tools_is_not_calling_one(transcript):
    """Claude Code 2.1.332 writes every tool's definition into its transcripts."""
    listed = {"tools": [{"eager_input_streaming": True}, {"name": "ScheduleWakeup"}]}
    transcript.write_text(json.dumps(listed) + "\n", encoding="utf-8")
    assert not schedules_itself(transcript)


def test_talking_about_a_schedule_is_not_setting_one(transcript):
    said = {"message": {"content": '{"name": "CronCreate"}'}}
    transcript.write_text(json.dumps(said) + "\n", encoding="utf-8")
    assert not schedules_itself(transcript)


@pytest.mark.parametrize("text", ["not json", "[]"])
def test_a_record_that_is_not_one_is_none(tmp_path, text):
    path = tmp_path / "r.json"
    path.write_text(text, encoding="utf-8")
    assert read_record(path) is None
    assert read_record(tmp_path / "missing.json") is None

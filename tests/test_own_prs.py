from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_terminal.own_prs import (
    Evidence,
    SessionEvidence,
    folder_names,
    own_sessions,
    transcript_evidence,
)

URL = "https://github.com/someone/Demo/pull/4"
TREE = "C:\\code\\demo\\.claude\\worktrees\\tab-1"
FIXER = "You are a fixer session, dispatched by the fix pass. Read"


def history(text: str, session: str = "s", project: str = TREE, at: float = 100.0) -> str:
    return json.dumps(
        {"display": text, "timestamp": at * 1000, "project": project, "sessionId": session}
    )


def iso(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).isoformat().replace("+00:00", "Z")


def on_branch(branch: str, at: float, cwd: str = "C:\\code\\demo") -> str:
    return json.dumps({"type": "user", "gitBranch": branch, "cwd": cwd, "timestamp": iso(at)})


def pr_link(at: float, url: str = URL) -> str:
    return json.dumps({"type": "pr-link", "sessionId": "s", "prUrl": url, "timestamp": iso(at)})


def test_a_prompted_worktrees_branch_is_the_users():
    sessions, evidence = own_sessions([history("fix it")])
    assert sessions == {"s"}
    assert evidence.covers(URL, "worktree-tab-1", created=200.0)


def test_a_dispatched_session_is_not_the_users_whatever_is_typed_in_it_later():
    sessions, evidence = own_sessions([history(FIXER), history("and this")])
    assert sessions == set()
    assert not evidence.covers(URL, "worktree-tab-1", created=200.0)


def test_a_plain_checkout_says_nothing_of_a_branch():
    sessions, evidence = own_sessions([history("fix it", project="C:\\code\\demo")])
    assert sessions == {"s"}
    assert evidence.branches == {}


def test_history_lines_that_are_not_records_are_skipped():
    assert own_sessions(["", "{half", "[]", '{"display": 1}'])[0] == set()


@pytest.mark.parametrize(
    ("head", "url", "created", "covered"),
    [
        ("fix/a", URL, 150.0, True),
        ("fix/b", URL, 150.0, False),
        # Opened before the session was ever on the branch: it was reviewing it.
        ("fix/a", URL, 50.0, False),
        # The same branch name in another repository, as a fleet-wide change has.
        ("fix/a", "https://github.com/someone/other/pull/4", 150.0, False),
        ("fix/a", "nonsense", 150.0, False),
    ],
)
def test_a_branch_worked_on_before_its_pr(head, url, created, covered):
    evidence = transcript_evidence([on_branch("fix/a", 100.0), on_branch("fix/a", 300.0)])
    assert evidence.covers(url, head, created) is covered


@pytest.mark.parametrize(("linked", "covered"), [(103.0, True), (400.0, False)])
def test_a_link_counts_only_as_the_pr_is_opened(linked, covered):
    evidence = transcript_evidence([pr_link(linked), pr_link(linked + 900)])
    assert evidence.covers(URL, "any", created=100.0) is covered


def test_transcript_lines_that_are_not_records_are_skipped():
    lines = ['{"gitBranch"', '{"gitBranch": "x"}', '{"type": "pr-link"}', '["pr-link"]', "text"]
    assert transcript_evidence(lines) == Evidence()


def test_folder_names_ignore_case_and_slash():
    assert {"code", "demo"} <= folder_names("C:/Code\\Demo")


def test_evidence_merges_to_the_earliest():
    one = transcript_evidence([on_branch("a", 5.0), pr_link(9.0)])
    one.update(transcript_evidence([on_branch("a", 3.0), on_branch("b", 1.0), pr_link(7.0)]))
    assert sorted(one.branches.values()) == [1.0, 3.0]
    assert one.links == {URL: 7.0}


def write_home(home: Path) -> Path:
    folder = home / "projects" / "C--code-demo"
    folder.mkdir(parents=True)
    lines = [history("mine", "mine"), history(FIXER, "robot")]
    (home / "history.jsonl").write_text("\n".join(lines), encoding="utf-8")
    (folder / "robot.jsonl").write_text(on_branch("agent/robot", 1.0), encoding="utf-8")
    (folder / "mine.jsonl").write_text(on_branch("fix/mine", 1.0), encoding="utf-8")
    return folder


def test_only_the_users_sessions_transcripts_are_read(tmp_path):
    write_home(tmp_path)
    evidence = SessionEvidence(tmp_path).read()
    assert evidence.covers(URL, "fix/mine", 2.0)
    assert not evidence.covers(URL, "agent/robot", 2.0)


def test_a_transcript_is_read_again_only_once_changed(tmp_path, monkeypatch):
    folder = write_home(tmp_path)
    reader = SessionEvidence(tmp_path)
    reader.read()
    reads = []

    def counting(lines):
        reads.append(1)
        return transcript_evidence(lines)

    monkeypatch.setattr("agent_terminal.own_prs.transcript_evidence", counting)
    assert reader.read().covers(URL, "fix/mine", 2.0)
    assert reads == []
    (folder / "mine.jsonl").write_text(on_branch("fix/later", 1.0), encoding="utf-8")
    os.utime(folder / "mine.jsonl", (9, 9))
    assert reader.read().covers(URL, "fix/later", 2.0)
    assert reads == [1]


def test_without_a_history_there_is_no_evidence(tmp_path):
    assert SessionEvidence(tmp_path).read() == Evidence()

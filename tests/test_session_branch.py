import json
import subprocess
from pathlib import Path

from agent_terminal.gitinfo import main_checkout
from agent_terminal.prompts import claude_transcript_dir
from agent_terminal.session_branch import SessionBranch, SessionBranches, last_branch
from agent_terminal.store import Tab

SESSION = "d8f38d3c-06d7-45c7-a6ac-6c7323991117"


def record(cwd: str, branch: str, kind: str = "assistant") -> str:
    """A transcript record as Claude Code 2.1 writes one."""
    return json.dumps(
        {
            "type": kind,
            "sessionId": SESSION,
            "timestamp": "2026-11-27T02:24:33.624Z",
            "cwd": cwd,
            "gitBranch": branch,
        }
    )


def pr_link(number: int) -> str:
    """A `pr-link` record as a real session gained them: for a PR it only looked at."""
    return json.dumps(
        {
            "type": "pr-link",
            "sessionId": SESSION,
            "prNumber": number,
            "prUrl": f"https://github.com/o/devkit/pull/{number}",
            "prRepository": "o/devkit",
            "timestamp": "2026-11-27T02:28:02.412Z",
        }
    )


def test_the_last_branch_off_the_trunk_wins():
    lines = [
        record(r"C:\r\.claude\worktrees\a", "worktree-a"),
        record(r"C:\r", "main"),
        record(r"C:\r\.claude\worktrees\agent-1", "land/release-1126"),
        pr_link(853),
        record(r"C:\r\.claude\worktrees\agent-1", "main"),
    ]
    assert last_branch(lines) == SessionBranch(
        "land/release-1126", r"C:\r\.claude\worktrees\agent-1"
    )


def test_a_session_only_on_the_trunk_has_no_branch():
    assert last_branch([record(r"C:\r", "main"), record(r"C:\r", "master"), pr_link(1)]) is None


def test_records_without_a_usable_branch_are_passed_over():
    lines = [
        record(r"C:\r", "worktree-a"),
        'not json, but mentions "gitBranch"',
        json.dumps(["gitBranch"]),
        json.dumps({"gitBranch": "x"}),
        json.dumps({"gitBranch": None, "cwd": r"C:\r"}),
        json.dumps({"gitBranch": "", "cwd": r"C:\r"}),
        json.dumps({"gitBranch": "y", "cwd": ""}),
    ]
    assert last_branch(lines) == SessionBranch("worktree-a", r"C:\r")


def git(folder: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=folder, check=True, capture_output=True)  # noqa: S607 - fixed test commands


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "claude"
        self.repository = tmp_path / "repo"
        self.repository.mkdir()
        git(self.repository, "init", "--initial-branch", "main")
        self.tree = self.repository / ".claude" / "worktrees" / "a"
        self.transcript = claude_transcript_dir(self.home, self.tree) / f"{SESSION}.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text("", encoding="utf-8")
        self.sessions = SessionBranches(self.home)
        self.tab = Tab(
            id="t",
            project="repo",
            project_path=str(self.repository),
            agent="claude",
            created_at=0.0,
            session_id=SESSION,
            cwd=str(self.tree),
        )

    def add(self, *lines: str, end: str = "\n") -> None:
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + end)


def test_a_reaped_worktrees_branch_is_its_repositorys(tmp_path):
    rig = Rig(tmp_path)
    # The folder does not exist: the harness reaped it after the session landed from it.
    rig.add(record(str(rig.repository / ".claude" / "worktrees" / "agent-1"), "land/x"))
    assert rig.sessions.of(rig.tab) == (main_checkout(rig.repository), "land/x")


def test_only_what_was_added_is_read(tmp_path):
    rig = Rig(tmp_path)
    rig.add(record(str(rig.tree), "worktree-a"))
    assert rig.sessions.of(rig.tab)[1] == "worktree-a"
    rig.add(record(str(rig.repository), "main"))
    # Nothing since was on a branch: the one found before stands.
    assert rig.sessions.of(rig.tab)[1] == "worktree-a"
    rig.add(record(str(rig.tree), "land/x"))
    assert rig.sessions.of(rig.tab)[1] == "land/x"


def test_a_line_still_being_written_waits_until_it_is_whole(tmp_path):
    rig = Rig(tmp_path)
    rig.add(record(str(rig.tree), "worktree-a"))
    rig.sessions.of(rig.tab)
    whole = record(str(rig.tree), "land/x")
    rig.add(whole[:20], end="")
    assert rig.sessions.of(rig.tab)[1] == "worktree-a"
    rig.add(whole[20:])
    assert rig.sessions.of(rig.tab)[1] == "land/x"


def test_a_transcript_that_shrank_is_read_afresh(tmp_path):
    rig = Rig(tmp_path)
    rig.add(record(str(rig.tree), "worktree-a"), record(str(rig.tree), "worktree-a"))
    rig.sessions.of(rig.tab)
    rig.transcript.write_text(record(str(rig.repository), "main") + "\n", encoding="utf-8")
    assert rig.sessions.of(rig.tab) == (None, None)


def test_a_transcript_not_written_yet_is_looked_for_again(tmp_path):
    rig = Rig(tmp_path)
    rig.transcript.unlink()
    assert rig.sessions.of(rig.tab) == (None, None)
    rig.add(record(str(rig.tree), "worktree-a"))
    assert rig.sessions.of(rig.tab)[1] == "worktree-a"


def test_a_deleted_transcript_says_nothing(tmp_path):
    rig = Rig(tmp_path)
    rig.add(record(str(rig.tree), "worktree-a"))
    rig.sessions.of(rig.tab)
    rig.transcript.unlink()
    assert rig.sessions.of(rig.tab) == (None, None)


def test_a_codex_tab_or_one_with_no_session_has_none(tmp_path):
    rig = Rig(tmp_path)
    rig.add(record(str(rig.tree), "worktree-a"))
    rig.tab.agent = "codex"
    assert rig.sessions.of(rig.tab) == (None, None)
    rig.tab.agent, rig.tab.session_id = "claude", None
    assert rig.sessions.of(rig.tab) == (None, None)


def test_forgetting_a_closed_tabs_session_reads_it_afresh(tmp_path):
    rig = Rig(tmp_path)
    rig.add(record(str(rig.tree), "worktree-a"))
    rig.sessions.of(rig.tab)
    rig.sessions.forget_others({"another"})
    rig.transcript.write_text(record(str(rig.tree), "land/x") + "\n", encoding="utf-8")
    # Longer than before, so only a fresh read could see the rewrite.
    rig.add(record(str(rig.repository), "main"))
    assert rig.sessions.of(rig.tab)[1] == "land/x"

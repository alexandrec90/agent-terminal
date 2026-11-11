import json
import os
import time
from pathlib import Path, PureWindowsPath

from agent_terminal.prompts import (
    Prompt,
    claude_transcript_dir,
    find_prompt,
    newest_claude_session,
    newest_codex_session,
)
from agent_terminal.settings import Settings


def make_transcript(home, tree, session, age=0.0):
    directory = claude_transcript_dir(home, tree)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session}.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))


def make_rollout(home, tree, session):
    directory = home / "sessions" / "2026" / "11" / "05"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta = {
        "type": "session_meta",
        "payload": {"id": session, "cwd": str(tree), "timestamp": stamp},
    }
    (directory / f"rollout-{session}.jsonl").write_text(json.dumps(meta) + "\n", encoding="utf-8")


def test_the_directory_name_is_the_one_claude_uses(tmp_path):
    # Copied from a real session's directory under ~/.claude/projects.
    tree = PureWindowsPath(
        r"C:\Users\alexa\vs-code\carameli\.claude\worktrees\eager-sleeping-simon"
    )
    expected = "C--Users-alexa-vs-code-carameli--claude-worktrees-eager-sleeping-simon"
    assert claude_transcript_dir(tmp_path, tree).name == expected


def test_finds_nothing_for_a_tree_with_no_sessions(tmp_path):
    assert newest_claude_session(tmp_path, tmp_path / "tree") is None


def test_takes_the_most_recently_written_session(tmp_path):
    tree = tmp_path / "tree"
    make_transcript(tmp_path, tree, "old", age=100)
    make_transcript(tmp_path, tree, "new")
    assert newest_claude_session(tmp_path, tree) == "new"


def test_codex_session_is_matched_by_its_folder(tmp_path):
    make_rollout(tmp_path, tmp_path / "other", "not-this")
    make_rollout(tmp_path, tmp_path / "tree", "this")
    assert newest_codex_session(tmp_path, tmp_path / "tree") == "this"


def test_find_prompt_prefers_claude(tmp_path):
    settings = Settings(claude_home=tmp_path, codex_home=tmp_path / "codex")
    make_transcript(tmp_path, tmp_path / "tree", "a-session")
    assert find_prompt(settings, tmp_path / "tree") == Prompt("claude", "a-session")


def test_find_prompt_reads_a_codex_worktree(tmp_path):
    settings = Settings(claude_home=tmp_path / "claude", codex_home=tmp_path)
    tree = tmp_path / "worktrees" / "ab12" / "project"
    make_rollout(tmp_path, tree, "a-session")
    assert find_prompt(settings, tree) == Prompt("codex", "a-session")


def test_find_prompt_leaves_codex_alone_outside_its_worktrees(tmp_path):
    settings = Settings(claude_home=tmp_path / "claude", codex_home=tmp_path)
    make_rollout(tmp_path, tmp_path / "tree", "a-session")
    assert find_prompt(settings, tmp_path / "tree") is None


def test_find_prompt_with_nothing(tmp_path):
    settings = Settings(claude_home=tmp_path / "claude", codex_home=tmp_path / "codex")
    assert find_prompt(settings, Path("nowhere")) is None

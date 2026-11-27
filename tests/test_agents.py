import base64
import json
from pathlib import Path

import pytest

from agent_terminal import agents
from agent_terminal.agents import CLAUDE, CODEX
from agent_terminal.settings import Settings
from agent_terminal.store import Tab


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        projects_root=tmp_path / "projects",
        data_dir=tmp_path / "data",
        claude_home=tmp_path / "claude",
        codex_home=tmp_path / "codex",
    )


def make_tab(tmp_path: Path, agent: str, **fields) -> Tab:
    defaults = {"project": "demo", "project_path": str(tmp_path / "demo"), "created_at": 0.0}
    return Tab(id="t", agent=agent, **{**defaults, **fields})


def write_claude_session(settings: Settings, session_id: str, *records: dict) -> Path:
    directory = settings.claude_home / "projects" / "C--some-encoded-path"
    directory.mkdir(parents=True)
    transcript = directory / f"{session_id}.jsonl"
    lines = [json.dumps(record) for record in records] or ["{}"]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return transcript


def worktree_state(path: str) -> dict:
    """The record `claude -w` writes, as Claude Code wrote it for a real session."""
    return {
        "type": "worktree-state",
        "worktreeSession": {
            "originalCwd": "C:\\repo",
            "worktreePath": path,
            "worktreeName": Path(path).name,
            "worktreeBranch": f"worktree-{Path(path).name}",
        },
        "sessionId": "abc",
    }


class TestTabTree:
    def test_a_plain_tab_works_in_its_folder(self, tmp_path):
        tab = make_tab(tmp_path, CLAUDE)
        assert agents.tab_tree(tab) == Path(tab.project_path)

    def test_a_new_worktree_is_the_projects_until_found(self, tmp_path):
        tab = make_tab(tmp_path, CLAUDE, worktree=True)
        assert agents.tab_tree(tab) == Path(tab.project_path)

    def test_a_recorded_folder_outranks_the_project(self, tmp_path):
        tab = make_tab(tmp_path, CODEX, worktree=True, cwd=str(tmp_path / "managed"))
        assert agents.tab_tree(tab) == tmp_path / "managed"


class TestClaudeSessionTree:
    def test_absent_before_a_first_message(self, settings):
        assert agents.claude_session_tree(settings.claude_home, "abc") is None

    def test_the_worktree_claude_named_for_itself(self, settings):
        records = [{"type": "mode"}, worktree_state("C:\\repo\\.claude\\worktrees\\calm-otter")]
        records.append({"type": "user", "cwd": "C:\\elsewhere"})
        write_claude_session(settings, "abc", *records)
        found = agents.claude_session_tree(settings.claude_home, "abc")
        assert found == "C:\\repo\\.claude\\worktrees\\calm-otter"

    def test_a_session_without_a_worktree_gives_its_cwd(self, settings):
        records = [{"type": "file-history-snapshot"}, {"type": "user", "cwd": "C:\\repo"}]
        write_claude_session(settings, "abc", *records)
        assert agents.claude_session_tree(settings.claude_home, "abc") == "C:\\repo"

    def test_a_cleared_worktree_state_is_passed_over(self, settings):
        cleared = {"type": "worktree-state", "worktreeSession": None}
        write_claude_session(settings, "abc", cleared, {"type": "user", "cwd": "C:\\repo"})
        assert agents.claude_session_tree(settings.claude_home, "abc") == "C:\\repo"

    def test_unreadable_lines_are_skipped(self, settings):
        transcript = write_claude_session(settings, "abc")
        transcript.write_text('not json\n[1]\n{"cwd": 3}\n{"cwd": "C:\\\\repo"}\n')
        assert agents.claude_session_tree(settings.claude_home, "abc") == "C:\\repo"

    def test_a_transcript_naming_no_folder_gives_none(self, settings):
        write_claude_session(settings, "abc", {"type": "mode"})
        assert agents.claude_session_tree(settings.claude_home, "abc") is None

    def test_only_the_opening_lines_are_read(self, settings):
        filler = [{"type": "mode"}] * agents.TRANSCRIPT_LINES_TO_READ
        write_claude_session(settings, "abc", *filler, {"cwd": "C:\\late"})
        assert agents.claude_session_tree(settings.claude_home, "abc") is None

    def test_a_transcript_not_in_utf8_gives_none(self, settings):
        transcript = write_claude_session(settings, "abc")
        transcript.write_bytes(b'{"cwd": "\xff"}\n')
        assert agents.claude_session_tree(settings.claude_home, "abc") is None


class TestTrustLaunch:
    def test_runs_claude_alone_where_the_worktree_will_be_made_from(self, tmp_path, settings):
        tab = make_tab(tmp_path, CLAUDE, worktree=True, session_id="abc")
        launch = agents.claude_trust_launch(tab, settings)
        assert launch == agents.Launch(tab.project_path, ["claude"])


class TestResumeFlag:
    def test_a_new_session_may_be_asked_for_trust(self, tmp_path, settings):
        assert not agents.claude_launch(make_tab(tmp_path, CLAUDE), settings).resumes

    def test_a_resumed_session_has_been_trusted_already(self, tmp_path, settings):
        write_claude_session(settings, "abc")
        tab = make_tab(tmp_path, CLAUDE, session_id="abc")
        assert agents.claude_launch(tab, settings).resumes

    def test_a_prs_session_resumes_in_its_recorded_tree(self, tmp_path, settings):
        write_claude_session(settings, "abc")
        tab = make_tab(tmp_path, CLAUDE, session_id="abc", cwd=str(tmp_path))
        assert agents.claude_launch(tab, settings).cwd == str(tmp_path)


class TestNewSessionId:
    def test_claude_fixes_its_session_up_front(self):
        first = agents.new_session_id(CLAUDE)
        assert first and first != agents.new_session_id(CLAUDE)

    def test_codex_chooses_for_itself(self):
        assert agents.new_session_id(CODEX) is None


class TestClaudeSessionExists:
    def test_found_under_any_project_directory(self, settings):
        write_claude_session(settings, "abc")
        assert agents.claude_session_exists(settings.claude_home, "abc")

    def test_absent_before_a_first_message(self, settings):
        assert not agents.claude_session_exists(settings.claude_home, "abc")


class TestClaudeTranscript:
    def test_the_file_whichever_folder_it_was_filed_under(self, settings):
        written = write_claude_session(settings, "abc")
        assert agents.claude_transcript(settings.claude_home, "abc") == written

    def test_none_before_a_first_message(self, settings):
        assert agents.claude_transcript(settings.claude_home, "abc") is None


class TestClaudeLaunch:
    def test_a_new_session_fixes_its_id(self, tmp_path, settings):
        tab = make_tab(tmp_path, CLAUDE, session_id="abc")
        launch = agents.claude_launch(tab, settings)
        assert launch == agents.Launch(tab.project_path, ["claude", "--session-id", "abc"])

    def test_a_new_worktree_session_lets_claude_name_it(self, tmp_path, settings):
        tab = make_tab(tmp_path, CLAUDE, session_id="abc", worktree=True)
        launch = agents.claude_launch(tab, settings)
        assert launch.cwd == tab.project_path
        assert launch.argv == ["claude", "--session-id", "abc", "--worktree"]
        assert launch.makes_worktree

    def test_a_plain_session_makes_no_worktree(self, tmp_path, settings):
        tab = make_tab(tmp_path, CLAUDE, session_id="abc")
        assert not agents.claude_launch(tab, settings).makes_worktree

    def test_a_tree_that_exists_already_is_started_in_not_cut_again(self, tmp_path, settings):
        tree = tmp_path / "demo" / ".claude" / "worktrees" / "calm-otter"
        tree.mkdir(parents=True)
        tab = make_tab(tmp_path, CLAUDE, session_id="abc", worktree=True, cwd=str(tree))
        launch = agents.claude_launch(tab, settings)
        assert launch == agents.Launch(str(tree), ["claude", "--session-id", "abc"])

    def test_a_removed_tree_is_cut_afresh(self, tmp_path, settings):
        gone = str(tmp_path / "gone")
        tab = make_tab(tmp_path, CLAUDE, session_id="abc", worktree=True, cwd=gone)
        launch = agents.claude_launch(tab, settings)
        assert launch.cwd == tab.project_path
        assert launch.argv[-1] == "--worktree"

    def test_a_session_with_a_transcript_resumes(self, tmp_path, settings):
        write_claude_session(settings, "abc")
        tab = make_tab(tmp_path, CLAUDE, session_id="abc")
        assert agents.claude_launch(tab, settings).argv == ["claude", "--resume", "abc"]

    def test_a_worktree_session_resumes_inside_the_worktree(self, tmp_path, settings):
        worktree = tmp_path / "demo" / ".claude" / "worktrees" / "calm-otter"
        worktree.mkdir(parents=True)
        write_claude_session(settings, "abc")
        tab = make_tab(tmp_path, CLAUDE, session_id="abc", worktree=True, cwd=str(worktree))
        launch = agents.claude_launch(tab, settings)
        assert launch.cwd == str(worktree)
        assert "--worktree" not in launch.argv

    def test_a_worktree_not_yet_recorded_is_read_from_the_transcript(self, tmp_path, settings):
        # Named by Claude, here or in another terminal: the transcript is what says where.
        worktree = tmp_path / "elsewhere" / "calm-otter"
        worktree.mkdir(parents=True)
        write_claude_session(settings, "abc", worktree_state(str(worktree)))
        tab = make_tab(tmp_path, CLAUDE, session_id="abc", worktree=True)
        assert agents.claude_launch(tab, settings).cwd == str(worktree)

    def test_a_removed_worktree_falls_back_to_the_project(self, tmp_path, settings):
        write_claude_session(settings, "abc", worktree_state(str(tmp_path / "gone")))
        tab = make_tab(tmp_path, CLAUDE, session_id="abc", worktree=True)
        launch = agents.claude_launch(tab, settings)
        assert launch.cwd == tab.project_path

    def test_a_resumed_session_can_be_given_its_next_message(self, tmp_path, settings):
        write_claude_session(settings, "abc")
        tab = make_tab(tmp_path, CLAUDE, session_id="abc")
        launch = agents.claude_launch(tab, settings, "continue")
        assert launch.argv == ["claude", "--resume", "abc", "continue"]

    def test_a_session_not_yet_begun_takes_no_message(self, tmp_path, settings):
        tab = make_tab(tmp_path, CLAUDE, session_id="abc")
        assert "continue" not in agents.claude_launch(tab, settings, "continue").argv

    def test_a_tab_closed_before_its_first_message_starts_again(self, tmp_path, settings):
        # Claude refuses `--resume` for an id with no transcript.
        tab = make_tab(tmp_path, CLAUDE, session_id="abc")
        assert "--resume" not in agents.claude_launch(tab, settings).argv

    def test_a_record_with_no_session_gets_one(self, tmp_path, settings):
        argv = agents.claude_launch(make_tab(tmp_path, CLAUDE), settings).argv
        assert argv[1] == "--session-id"


class TestCodexLaunch:
    def test_a_new_session(self, tmp_path, settings):
        tab = make_tab(tmp_path, CODEX)
        assert agents.codex_launch(tab, settings).argv == ["codex"]

    def test_a_new_worktree_session(self, tmp_path, settings):
        tab = make_tab(tmp_path, CODEX, worktree=True)
        launch = agents.codex_launch(tab, settings)
        assert launch.argv == ["codex", "--worktree"]
        assert launch.makes_worktree

    def test_a_found_session_resumes_where_it_ran(self, tmp_path, settings):
        tab = make_tab(tmp_path, CODEX, session_id="abc", cwd=str(tmp_path), worktree=True)
        launch = agents.codex_launch(tab, settings)
        assert launch == agents.Launch(str(tmp_path), ["codex", "resume", "abc"], resumes=True)

    def test_a_removed_directory_falls_back_to_the_project(self, tmp_path, settings):
        tab = make_tab(tmp_path, CODEX, session_id="abc", cwd=str(tmp_path / "gone"))
        assert agents.codex_launch(tab, settings).cwd == tab.project_path


def test_launch_for_picks_by_agent(tmp_path, settings):
    assert agents.launch_for(make_tab(tmp_path, CODEX), settings).argv[0] == "codex"
    assert agents.launch_for(make_tab(tmp_path, CLAUDE), settings).argv[0] == "claude"


def test_launch_for_hands_claude_the_prompt_and_codex_none(tmp_path, settings):
    write_claude_session(settings, "abc")
    claude = make_tab(tmp_path, CLAUDE, session_id="abc")
    assert agents.launch_for(claude, settings, "continue").argv[-1] == "continue"
    codex = make_tab(tmp_path, CODEX, session_id="abc")
    assert agents.launch_for(codex, settings, "continue").argv == ["codex", "resume", "abc"]


class TestShell:
    @pytest.mark.parametrize(
        ("argument", "quoted"),
        [("plain", "'plain'"), ("it's", "'it''s'"), ("a b", "'a b'"), ("$(x)", "'$(x)'")],
    )
    def test_powershell_quote(self, argument, quoted):
        assert agents.powershell_quote(argument) == quoted

    def test_shell_stays_open_after_the_agent(self):
        argv = agents.shell_argv("pwsh", ["claude"])
        assert argv[0] == "pwsh"
        assert "-NoExit" in argv

    def test_the_encoded_command_runs_the_agent_and_reports_its_return(self):
        argv = agents.shell_argv("pwsh", ["claude", "--resume", "x'y"])
        line = base64.b64decode(argv[-1]).decode("utf-16-le")
        assert line.startswith("& 'claude' '--resume' 'x''y';")
        assert "7501;state=clear" in line

    def test_agent_env_drops_what_the_parent_set(self):
        env = agents.agent_env({"PATH": "p", "CLAUDECODE": "1", "WT_SESSION": "s"})
        assert env["PATH"] == "p"
        assert "CLAUDECODE" not in env
        assert "WT_SESSION" not in env
        assert env["TERM"] == "xterm-256color"

    @pytest.mark.parametrize(
        "name",
        [
            # Launched under one of these, Claude writes no transcript, so no tab resumes.
            "CLAUDE_CODE_CHILD_SESSION",
            "CLAUDE_CODE_SESSION_ID",
            "CLAUDE_CODE_SESSION_ATTENDED",
            "CLAUDE_CODE_SESSION_KIND",
            "CLAUDE_CODE_BRIDGE_SESSION_ID",
            "CLAUDE_CODE_MESSAGING_SOCKET",
            "CLAUDE_CODE_MESSAGING_TOKEN",
            "CLAUDE_CODE_ENTRYPOINT",
            "CLAUDE_PID",
        ],
    )
    def test_agent_env_drops_a_parent_claude_sessions_identity(self, name):
        assert name not in agents.agent_env({name: "x"})

    def test_agent_env_keeps_the_users_own_claude_settings(self):
        name = "CLAUDE_CODE_USE_POWERSHELL_TOOL"
        assert agents.agent_env({name: "1"})[name] == "1"

import base64
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


def write_claude_session(settings: Settings, session_id: str) -> None:
    directory = settings.claude_home / "projects" / "C--some-encoded-path"
    directory.mkdir(parents=True)
    (directory / f"{session_id}.jsonl").write_text("{}\n")


class TestTabTree:
    def test_a_plain_tab_works_in_its_folder(self, tmp_path):
        tab = make_tab(tmp_path, CLAUDE)
        assert agents.tab_tree(tab) == Path(tab.project_path)

    def test_a_claude_worktree_follows_from_its_name(self, tmp_path):
        tab = make_tab(tmp_path, CLAUDE, worktree_name="tab-1")
        assert agents.tab_tree(tab) == agents.claude_worktree_dir(tab.project_path, "tab-1")

    def test_a_recorded_folder_outranks_either(self, tmp_path):
        tab = make_tab(tmp_path, CODEX, worktree=True, cwd=str(tmp_path / "managed"))
        assert agents.tab_tree(tab) == tmp_path / "managed"


class TestTrustLaunch:
    def test_runs_claude_alone_where_the_worktree_will_be_made_from(self, tmp_path, settings):
        tab = make_tab(tmp_path, CLAUDE, worktree_name="tab-1", session_id="abc")
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


class TestNewTabIdentity:
    def test_claude_fixes_its_session_up_front(self):
        session_id, worktree = agents.new_tab_identity(CLAUDE, worktree=False)
        assert session_id is not None
        assert worktree is None

    def test_a_claude_worktree_gets_a_unique_name(self):
        first = agents.new_tab_identity(CLAUDE, worktree=True)[1]
        second = agents.new_tab_identity(CLAUDE, worktree=True)[1]
        assert first and second and first != second

    def test_codex_chooses_for_itself(self):
        assert agents.new_tab_identity(CODEX, worktree=True) == (None, None)


class TestClaudeSessionExists:
    def test_found_under_any_project_directory(self, settings):
        write_claude_session(settings, "abc")
        assert agents.claude_session_exists(settings.claude_home, "abc")

    def test_absent_before_a_first_message(self, settings):
        assert not agents.claude_session_exists(settings.claude_home, "abc")


class TestClaudeLaunch:
    def test_a_new_session_fixes_its_id(self, tmp_path, settings):
        tab = make_tab(tmp_path, CLAUDE, session_id="abc")
        launch = agents.claude_launch(tab, settings)
        assert launch == agents.Launch(tab.project_path, ["claude", "--session-id", "abc"])

    def test_a_new_worktree_session_passes_the_name(self, tmp_path, settings):
        tab = make_tab(tmp_path, CLAUDE, session_id="abc", worktree=True, worktree_name="tab-1")
        launch = agents.claude_launch(tab, settings)
        assert launch.cwd == tab.project_path
        assert launch.argv == ["claude", "--session-id", "abc", "--worktree", "tab-1"]

    def test_a_session_with_a_transcript_resumes(self, tmp_path, settings):
        write_claude_session(settings, "abc")
        tab = make_tab(tmp_path, CLAUDE, session_id="abc")
        assert agents.claude_launch(tab, settings).argv == ["claude", "--resume", "abc"]

    def test_a_worktree_session_resumes_inside_the_worktree(self, tmp_path, settings):
        write_claude_session(settings, "abc")
        tab = make_tab(tmp_path, CLAUDE, session_id="abc", worktree=True, worktree_name="tab-1")
        worktree = agents.claude_worktree_dir(tab.project_path, "tab-1")
        worktree.mkdir(parents=True)
        launch = agents.claude_launch(tab, settings)
        assert launch.cwd == str(worktree)
        assert "--worktree" not in launch.argv

    def test_a_removed_worktree_falls_back_to_the_project(self, tmp_path, settings):
        write_claude_session(settings, "abc")
        tab = make_tab(tmp_path, CLAUDE, session_id="abc", worktree=True, worktree_name="gone")
        launch = agents.claude_launch(tab, settings)
        assert launch.cwd == tab.project_path

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
        assert agents.codex_launch(tab, settings).argv == ["codex", "--worktree"]

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

from pathlib import Path

from agent_terminal import settings as module
from agent_terminal.settings import Settings


def test_defaults_point_at_vs_code_and_powershell(monkeypatch):
    monkeypatch.delenv("AGENT_TERMINAL_PROJECTS_ROOT", raising=False)
    monkeypatch.delenv("AGENT_TERMINAL_SHELL", raising=False)
    settings = Settings()
    assert settings.projects_root == Path.home() / "vs-code"
    assert settings.shell == "pwsh"


def test_claude_records_trust_beside_the_home_directory_by_default(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert module.default_claude_config_file() == Path.home() / ".claude.json"


def test_a_moved_claude_directory_moves_its_trust_record_too(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert module.default_claude_config_file() == tmp_path / ".claude.json"


def test_environment_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_TERMINAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_TERMINAL_CLAUDE_COMMAND", "fake")
    settings = Settings()
    assert settings.data_dir == tmp_path
    assert settings.claude_command == "fake"


def test_files_live_in_the_data_dir(tmp_path):
    settings = Settings(data_dir=tmp_path)
    assert settings.state_file.parent == tmp_path
    assert settings.log_file.parent == tmp_path
    assert settings.lock_file.parent == tmp_path


def test_data_dir_prefers_local_app_data(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert module.default_data_dir() == tmp_path / "AgentTerminal"


def test_data_dir_without_local_app_data(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert module.default_data_dir().name == "AgentTerminal"


def test_agent_homes_honour_their_own_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "x"))
    assert module.default_claude_home() == tmp_path / "c"
    assert module.default_codex_home() == tmp_path / "x"


def test_agent_homes_default_to_the_user_directory(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert module.default_claude_home() == Path.home() / ".claude"
    assert module.default_codex_home() == Path.home() / ".codex"

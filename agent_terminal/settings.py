"""Runtime configuration. Every field is an `AGENT_TERMINAL_*` environment variable."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    """Where the session file and app log live: per-user, outside any checkout."""
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home() / ".local" / "share") / "AgentTerminal"


def default_claude_home() -> Path:
    """Claude Code's data directory, honouring its own override."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".claude"


def default_codex_home() -> Path:
    """Codex's data directory, honouring its own override."""
    override = os.environ.get("CODEX_HOME")
    return Path(override) if override else Path.home() / ".codex"


def default_claude_config_file() -> Path:
    """Where Claude Code records what it trusts, which sits outside its data directory
    unless that was moved."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(override) if override else Path.home()) / ".claude.json"


class Settings(BaseSettings):
    """The paths and commands the app runs against."""

    model_config = SettingsConfigDict(env_prefix="AGENT_TERMINAL_")

    projects_root: Path = Field(default_factory=lambda: Path.home() / "vs-code")
    data_dir: Path = Field(default_factory=default_data_dir)
    claude_home: Path = Field(default_factory=default_claude_home)
    claude_config_file: Path = Field(default_factory=default_claude_config_file)
    codex_home: Path = Field(default_factory=default_codex_home)
    shell: str = "pwsh"
    claude_command: str = "claude"
    codex_command: str = "codex"
    gh_command: str = "gh"
    # GitHub moves slowly here: the fix pass runs every half hour. A poll of `gh` per
    # repository a minute is 700-odd requests an hour across a dozen, of 5000 allowed.
    pr_poll_seconds: float = 60.0
    # Keep the tabs' processes in the session host (`host.py`), so a restart or an update
    # leaves them running. Off, they live and die with the window, as the tests need.
    session_host: bool = True
    # How long the host keeps processes with no app connected before ending them.
    host_orphan_seconds: float = 3600.0
    # How long a Claude tab sits idle and out of view before its process is ended, to be
    # resumed when the tab is next shown. 0 never does.
    sleep_after_seconds: float = 900.0
    # The branch the app's own checkout updates from, on `origin`.
    update_branch: str = "main"
    # How often to ask `origin` whether there is an update. A fetch each time.
    update_check_seconds: float = 1800.0

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "agent-terminal.log"

    @property
    def lock_file(self) -> Path:
        return self.data_dir / "agent-terminal.lock"

    @property
    def pid_file(self) -> Path:
        """The running app's pid, for whatever has to find its window."""
        return self.data_dir / "agent-terminal.pid"

    @property
    def host_address_file(self) -> Path:
        return self.data_dir / "host.json"

    @property
    def host_lock_file(self) -> Path:
        return self.data_dir / "host.lock"

    @property
    def host_log_file(self) -> Path:
        return self.data_dir / "host.log"

    @property
    def update_result_file(self) -> Path:
        """How the last update went, for the app it started to show."""
        return self.data_dir / "update-result.json"

    @property
    def update_log_file(self) -> Path:
        """Everything git and uv said during the last update."""
        return self.data_dir / "update.log"

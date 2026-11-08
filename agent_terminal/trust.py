"""Whether an agent already trusts a folder. This only reads their records: the decision
is the user's, on the agent's own screen, so nothing here ever grants it.

Seen from Claude Code 2.1.318 and Codex 0.166:

    Claude   `projects[<path>].hasTrustDialogAccepted` in `~/.claude.json`, forward
             slashes. Trust covers subfolders. `claude -w` in an untrusted folder exits 1
             rather than asking, which is why `runner.py` has a step for it.
    Codex    `[projects.'<lowercase path>'] trust_level = "trusted"` in `config.toml`, on
             the repository root. `codex --worktree` asks for itself.
"""

from __future__ import annotations

import json
import logging
import tomllib
from pathlib import Path
from typing import Any

from agent_terminal.gitinfo import main_checkout
from agent_terminal.settings import Settings

log = logging.getLogger(__name__)


def _comparable(path: Path | str) -> str:
    return str(path).replace("\\", "/").rstrip("/").casefold()


def _read_mapping(path: Path, parse: Any) -> dict[str, Any]:
    """A config file's top table. Unreadable reads as empty: that only means the agent
    is expected to ask, which it will or will not do for itself."""
    try:
        loaded = parse(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _projects(config: dict[str, Any]) -> dict[str, Any]:
    projects = config.get("projects")
    return projects if isinstance(projects, dict) else {}


def claude_trusted(config_file: Path, folder: Path) -> bool:
    """True if Claude trusts `folder` or any folder above it."""
    projects = _projects(_read_mapping(config_file, json.loads))
    trusted = {
        _comparable(path)
        for path, entry in projects.items()
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True
    }
    return any(_comparable(candidate) in trusted for candidate in (folder, *folder.parents))


def codex_trusted(codex_home: Path, folder: Path) -> bool:
    """True if Codex trusts `folder` or the repository it is in."""
    projects = _projects(_read_mapping(codex_home / "config.toml", tomllib.loads))
    trusted = {
        _comparable(path)
        for path, entry in projects.items()
        if isinstance(entry, dict) and entry.get("trust_level") == "trusted"
    }
    root = main_checkout(folder)
    return any(_comparable(candidate) in trusted for candidate in (folder, root) if candidate)


def is_trusted(settings: Settings, agent: str, folder: Path) -> bool:
    """Whether `agent` will start in `folder` without asking."""
    if agent == "claude":
        return claude_trusted(settings.claude_config_file, folder)
    return codex_trusted(settings.codex_home, folder)

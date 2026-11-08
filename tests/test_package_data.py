"""The page's files reach an installed copy: setuptools packages only what is listed."""

from __future__ import annotations

import tomllib
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "agent_terminal"


def patterns() -> list[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return config["tool"]["setuptools"]["package-data"]["agent_terminal"]


def listed(relative: str, globs: list[str]) -> bool:
    # A setuptools `*` stops at a slash, which fnmatch's does not.
    return any(
        fnmatch(relative, pattern) and relative.count("/") == pattern.count("/")
        for pattern in globs
    )


def test_listed_matches_one_folder_deep_only() -> None:
    assert listed("web/icons/app.ico", ["web/icons/*"])
    assert not listed("web/icons/app.ico", ["web/*"])


def test_every_file_the_page_serves_is_packaged() -> None:
    globs = patterns()
    served = [
        path.relative_to(PACKAGE).as_posix()
        for path in (PACKAGE / "web").rglob("*")
        if path.is_file()
    ]
    assert served
    assert [name for name in served if not listed(name, globs)] == []

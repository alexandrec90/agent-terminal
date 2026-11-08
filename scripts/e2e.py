#!/usr/bin/env python3
"""Run the tests that need this workstation: the real window, on a Windows desktop.

The contract is `.claude/rules/local-e2e.md`: exit 0 only for a green run that executed
something, each failure as pytest's `FAILED <id>` line, run from the checkout root.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
E2E_TESTS = "tests/e2e"


def pytest_command(python: str) -> list[str]:
    """Selects the `local` marker over the `addopts` default, and fails rather than skips:
    this is the one checkout those tests were written for."""
    return [python, "-m", "pytest", E2E_TESTS, "-m", "local", "-q", "-rfEs", "--no-header"]


def exit_code(pytest_code: int) -> int:
    """Pytest's own code, in which "nothing collected" is already a failure (5)."""
    return pytest_code


def main() -> int:
    completed = subprocess.run(pytest_command(sys.executable), cwd=REPO_ROOT, check=False)
    return exit_code(completed.returncode)


if __name__ == "__main__":
    sys.exit(main())

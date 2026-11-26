#!/usr/bin/env python3
"""The end-to-end tiers: every test that needs more than the Linux gate's ordinary suite.

Two markers, both deselected by `addopts` in `pyproject.toml` from every other run:

- `local` (no flag): tests that need this workstation -- its logins, its live data, its
  hardware. devkit's nightly `devkit-local-e2e` job runs this in the checkout, and
  `.claude/rules/local-e2e.md` is the contract. **This project has none today**, so
  `.devkit.toml` sets `[delivery] e2e = "ci"` and the nightly job records the project as
  running in CI; a bare run here is red as an empty run.
- `ci_e2e` (`--ci`): tests that need Windows itself -- the real window over WebView2,
  ConPTY, a console, the shell's window properties, the Recycle Bin's API -- but nothing
  of this PC: each runs on temp folders, a fake agent and a fake `gh`. The `E2E` job in
  `.github/workflows/pr-gate.yml` runs `python scripts/e2e.py --ci` on `windows-latest`,
  on the default branch, with `DEVKIT_E2E=1`.

Both runs keep the one contract: exit 0 green, anything else red, each failing test
printed as pytest's `FAILED <id>` line, which is what devkit's fix pass groups by.

**A skip is a failure when the scheduler runs it** (`DEVKIT_E2E` set): the checkout is the
one the tests were written for, so nothing has a reason left to skip, and each skip is
printed as a `FAILED` line naming its reason. By hand the skips are listed and do not fail
the run.

**A run that executed nothing is red**, whoever ran it: a green from zero tests is a skip
reported as a pass.

**Test ids on the command line run those alone, still inside the tier**, which is how a
fixer reproduces one `FAILED` line: `python scripts/e2e.py --ci tests/e2e/test_app.py::<name>`.

pytest runs under the checkout's `.venv` where it has one, else through `uv run` with
the `dev` extras (`runner_argv`), and this script stays
stdlib-only so the machine's Python can start it. `logs/e2e-junit.xml` is the artifact,
overwritten per run, on failure too.

Tested in `tests/test_e2e.py`.
"""

from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

ENV_FLAG = "DEVKIT_E2E"
MARKER = "local"
# `--ci` selects this instead: the end-to-end tests a hosted Windows runner can run.
CI_FLAG = "--ci"
CI_MARKER = "ci_e2e"
JUNIT = Path("logs") / "e2e-junit.xml"
VENV_INTERPRETERS = (Path(".venv") / "Scripts" / "python.exe", Path(".venv") / "bin" / "python")
# uv's switch for "run in the environment as it stands".
NO_SYNC = "UV_NO_SYNC"
# The nightly job runs with nobody at the keyboard, and a console window opening under
# someone's cursor is worse than a run a night late.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# One `<testcase ...>`, self-closing or with its body up to `</testcase>`. A pattern, not
# an XML parser: the report is pytest's own flat shape, and what is wanted is two
# attributes and which element opens the body.
CASE = re.compile(r"<testcase\b(?P<attrs>[^>]*?)(?:/>|>(?P<body>.*?)</testcase>)", re.DOTALL)
ATTR = re.compile(r'(\w+)="([^"]*)"')
OUTCOME = re.compile(r"<(?P<tag>skipped|failure|error)\b(?P<attrs>[^>]*)")

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


@dataclass(frozen=True)
class Case:
    """One test case from the junit report: `tests/x.py::name`, and how it ended."""

    ident: str
    outcome: str  # "passed", "failed", "errored" or "skipped"
    message: str = ""


def scheduled(env: Mapping[str, str] = os.environ) -> bool:
    return bool(env.get(ENV_FLAG))


def interpreter(root: Path, fallback: str) -> str:
    """`root`'s own `.venv` interpreter, else `fallback`."""
    found = next((root / rel for rel in VENV_INTERPRETERS if (root / rel).is_file()), None)
    return str(found) if found is not None else fallback


def case_id(classname: str, name: str) -> str:
    """pytest's node id for a junit `testcase`: `tests.test_c.TestD` + `e` is
    `tests/test_c.py::TestD::e`, and a module that failed to collect has no classname and
    its dotted path as the name."""
    parts = [part for part in classname.split(".") if part]
    modules = [i for i, part in enumerate(parts) if part.startswith("test_")]
    if not modules:
        return name.replace(".", "/") + ".py"
    cut = modules[-1] + 1
    return "::".join(["/".join(parts[:cut]) + ".py", *parts[cut:], name])


def cases(report: Path) -> list[Case]:
    """Every case in a pytest junit report; unreadable is no cases, which `verdict` reads
    as a run that ran nothing."""
    try:
        text = report.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[Case] = []
    for match in CASE.finditer(text):
        attrs = {key: html.unescape(value) for key, value in ATTR.findall(match["attrs"])}
        ident = case_id(attrs.get("classname", ""), attrs.get("name", "?"))
        ended = OUTCOME.search(match["body"] or "")
        if ended is None:
            found.append(Case(ident, "passed"))
            continue
        outcome = {"skipped": "skipped", "failure": "failed", "error": "errored"}[ended["tag"]]
        message = html.unescape(dict(ATTR.findall(ended["attrs"])).get("message", ""))
        found.append(Case(ident, outcome, message))
    return found


def first_line(message: str) -> str:
    return message.strip().splitlines()[0] if message.strip() else "no reason"


def split_args(argv: Sequence[str]) -> tuple[str, list[str]]:
    """The marker a run selects (`ci_e2e` under `--ci`, else `local`) and the test ids
    it names."""
    marker = CI_MARKER if CI_FLAG in argv else MARKER
    return marker, [arg for arg in argv if arg != CI_FLAG]


def verdict(
    code: int, found: list[Case], strict: bool, marker: str = MARKER
) -> tuple[int, list[str]]:
    """`(exit code, lines to print)` for pytest's `code` and the cases it reported.

    pytest already printed its own `FAILED` lines; these add what it words differently, an
    error as `ERROR`, and the two reds of its own that this tier owes: the skip under the
    scheduler and the empty run.
    """
    ran = [case for case in found if case.outcome != "skipped"]
    skips = [case for case in found if case.outcome == "skipped"]
    lines = [
        f"FAILED {case.ident} - errored: {first_line(case.message)}"
        for case in found
        if case.outcome == "errored"
    ]
    lines += [
        f"{'FAILED' if strict else 'SKIPPED'} {case.ident} - skipped: {first_line(case.message)}"
        for case in skips
    ]
    if not ran:
        return code or 1, [*lines, f"e2e: no {marker} test executed, which is not a pass"]
    if strict and skips:
        return code or 1, [*lines, f"e2e: {len(skips)} test(s) skipped under the scheduled run"]
    return code, lines


def runner_argv(root: Path, fallback: str) -> list[str]:
    """What starts pytest: `root`'s own `.venv` interpreter, else `uv run` building one.

    The machine's Python has no pytest, so in a tree with no `.venv` -- an agent's
    worktree before anything provisioned it -- the run ended `No module named pytest`,
    `--help` included (devkit ledger e16cd1cb). `--frozen` keeps the lock as committed;
    `--all-extras --all-groups` because pytest lives in `dev`, which a bare `uv run`
    leaves out. `fallback` is used only where `uv` is not on PATH either.
    """
    python = interpreter(root, "")
    if python:
        return [python]
    if shutil.which("uv") is None:
        return [fallback]
    return ["uv", "run", "--frozen", "--all-extras", "--all-groups", "python"]


def run_env(env: Mapping[str, str], root: Path) -> dict[str, str]:
    """The environment pytest runs in: `UV_NO_SYNC` dropped where `root` has no `.venv`.

    A devkit session carries it so `uv run` leaves a borrowed `.venv` alone; in a tree
    with none, `uv run` would make an empty one and install nothing into it. A tree that
    has a `.venv` keeps it."""
    out = dict(env)
    if not (root / ".venv").exists():
        out.pop(NO_SYNC, None)
    return out


def pytest_argv(
    python: str | Sequence[str], report: Path, named: Sequence[str] = (), marker: str = MARKER
) -> list[str]:
    # `-m` given here replaces the `-m "not local and not ci_e2e"` in `addopts`: the last
    # one wins.
    start = [python] if isinstance(python, str) else list(python)
    return [
        *start,
        "-m",
        "pytest",
        *(named or ["tests"]),
        "-m",
        marker,
        "-rfE",
        "-p",
        "no:cacheprovider",
        f"--junitxml={report}",
    ]


def main(argv: list[str] | None = None, run: Runner = subprocess.run) -> int:
    marker, named = split_args(sys.argv[1:] if argv is None else argv)
    report = REPO_ROOT / JUNIT
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
    python = runner_argv(REPO_ROOT, sys.executable)
    # Captured and re-printed rather than inherited: a `NO_WINDOW` child writes to the
    # hidden console it was given, not to the pipe the pass reads. Passed on as bytes, so
    # no character pytest printed can raise on the way through a narrower stdout encoding.
    done = run(
        pytest_argv(python, report, named, marker),
        cwd=REPO_ROOT,
        env=run_env(os.environ, REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=NO_WINDOW,
        check=False,
    )
    sys.stdout.flush()
    sys.stdout.buffer.write(done.stdout or b"")
    sys.stdout.buffer.flush()
    code, lines = verdict(done.returncode, cases(report), scheduled(), marker)
    for line in lines:
        print(line, flush=True)
    print(f"e2e: exit {code}, report in {JUNIT.as_posix()}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())

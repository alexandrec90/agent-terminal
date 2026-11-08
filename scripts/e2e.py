#!/usr/bin/env python3
"""The local end-to-end tier: every test that needs this workstation rather than a runner.

devkit's nightly `devkit-local-e2e` job runs this in the checkout, and
`.claude/rules/local-e2e.md` is the contract: exit 0 green, anything else red, each failing
test printed as pytest's `FAILED <id>` line. What it runs is every test marked `local`,
which `addopts` in `pyproject.toml` deselects from every other run. The gate is Linux, so
anything that needs Windows itself is in this tier.

**A skip is a failure when the scheduler runs it** (`DEVKIT_E2E` set): the checkout is the
one the tests were written for, so nothing has a reason left to skip, and each skip is
printed as a `FAILED` line naming its reason. By hand the skips are listed and do not fail
the run.

**A run that executed nothing is red**, whoever ran it: a green from zero tests is a skip
reported as a pass.

**Test ids on the command line run those alone, still inside the tier**, which is how a
fixer reproduces one `FAILED` line.

pytest runs under the checkout's `.venv` where it has one, and this script stays
stdlib-only so the machine's Python can start it. `logs/e2e-junit.xml` is the artifact,
overwritten per run, on failure too.

Tested in `tests/test_e2e.py`.
"""

from __future__ import annotations

import html
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

ENV_FLAG = "DEVKIT_E2E"
MARKER = "local"
JUNIT = Path("logs") / "e2e-junit.xml"
VENV_INTERPRETERS = (Path(".venv") / "Scripts" / "python.exe", Path(".venv") / "bin" / "python")
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


def verdict(code: int, found: list[Case], strict: bool) -> tuple[int, list[str]]:
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
        return code or 1, [*lines, f"e2e: no {MARKER} test executed, which is not a pass"]
    if strict and skips:
        return code or 1, [*lines, f"e2e: {len(skips)} test(s) skipped under the scheduled run"]
    return code, lines


def pytest_argv(python: str, report: Path, named: Sequence[str] = ()) -> list[str]:
    return [
        python,
        "-m",
        "pytest",
        *(named or ["tests"]),
        "-m",
        MARKER,
        "-rfE",
        "-p",
        "no:cacheprovider",
        f"--junitxml={report}",
    ]


def main(argv: list[str] | None = None, run: Runner = subprocess.run) -> int:
    named = sys.argv[1:] if argv is None else argv
    report = REPO_ROOT / JUNIT
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
    python = interpreter(REPO_ROOT, sys.executable)
    # Captured and re-printed rather than inherited: a `NO_WINDOW` child writes to the
    # hidden console it was given, not to the pipe the pass reads. Passed on as bytes, so
    # no character pytest printed can raise on the way through a narrower stdout encoding.
    done = run(
        pytest_argv(python, report, named),
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=NO_WINDOW,
        check=False,
    )
    sys.stdout.flush()
    sys.stdout.buffer.write(done.stdout or b"")
    sys.stdout.buffer.flush()
    code, lines = verdict(done.returncode, cases(report), scheduled())
    for line in lines:
        print(line, flush=True)
    print(f"e2e: exit {code}, report in {JUNIT.as_posix()}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())

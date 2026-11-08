"""`scripts/e2e.py`: the local end-to-end tier and the contract the nightly job reads."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("e2e", REPO_ROOT / "scripts" / "e2e.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["e2e"] = module  # dataclasses reads its own module's annotations from here
    spec.loader.exec_module(module)
    return module


e2e = load_script()

JUNIT = """\
<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="5">
<testcase classname="tests.test_console" name="test_live" file="tests/test_console.py"/>
<testcase classname="tests.test_a" name="test_drift" file="tests/test_a.py" line="9">
  <failure message="AssertionError: differs">trace</failure>
</testcase>
<testcase classname="tests.test_b" name="test_box[x]" file="tests/test_b.py" line="1">
  <skipped type="pytest.skip" message="needs &quot;docker&quot;&#10;more">s</skipped>
</testcase>
<testcase classname="tests.test_c.TestD" name="test_broke" line="1">
  <error message="fixture blew up">e</error>
</testcase>
<testcase classname="" name="tests.test_e" time="0.0">
  <error message="collection failure">e</error>
</testcase>
</testsuite></testsuites>
"""
PASSED = e2e.Case("tests/test_a.py::test_a", "passed")
FAILED = e2e.Case("tests/test_b.py::test_b", "failed", "boom")
ERRORED = e2e.Case("tests/test_c.py::test_c", "errored", "fixture blew up\ndetail")
SKIPPED = e2e.Case("tests/test_d.py::test_d", "skipped", "no docker\nmore")


def test_the_report_is_read_into_the_ids_pytest_s_summary_prints(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text(JUNIT, encoding="utf-8")
    assert e2e.cases(report) == [
        e2e.Case("tests/test_console.py::test_live", "passed"),
        e2e.Case("tests/test_a.py::test_drift", "failed", "AssertionError: differs"),
        e2e.Case("tests/test_b.py::test_box[x]", "skipped", 'needs "docker"\nmore'),
        e2e.Case("tests/test_c.py::TestD::test_broke", "errored", "fixture blew up"),
        e2e.Case("tests/test_e.py", "errored", "collection failure"),
    ]


def test_an_unreadable_report_is_no_cases(tmp_path):
    assert e2e.cases(tmp_path / "absent.xml") == []


@pytest.mark.parametrize(
    ("classname", "name", "expected"),
    [
        ("tests.test_a", "test_x", "tests/test_a.py::test_x"),
        ("tests.sub.test_a.TestB", "test_x[1]", "tests/sub/test_a.py::TestB::test_x[1]"),
        ("", "tests.test_a", "tests/test_a.py"),
    ],
)
def test_case_id_spells_a_node_id(classname, name, expected):
    assert e2e.case_id(classname, name) == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [("one\ntwo", "one"), ("\n  padded  \n", "padded"), ("", "no reason"), ("  ", "no reason")],
)
def test_first_line_names_the_reason(message, expected):
    assert e2e.first_line(message) == expected


def test_a_green_run_stays_green():
    assert e2e.verdict(0, [PASSED], strict=True) == (0, [])


def test_a_red_run_keeps_pytest_s_exit_code_and_its_own_failed_lines():
    assert e2e.verdict(1, [PASSED, FAILED], strict=False) == (1, [])


def test_an_error_is_named_as_a_failure_since_that_is_what_the_pass_reads():
    code, lines = e2e.verdict(1, [ERRORED], strict=False)
    assert code == 1
    assert lines == ["FAILED tests/test_c.py::test_c - errored: fixture blew up"]


def test_a_skip_by_hand_is_listed_and_does_not_fail():
    code, lines = e2e.verdict(0, [PASSED, SKIPPED], strict=False)
    assert code == 0
    assert lines == ["SKIPPED tests/test_d.py::test_d - skipped: no docker"]


def test_a_skip_under_the_scheduler_is_a_failure_naming_its_reason():
    code, lines = e2e.verdict(0, [PASSED, SKIPPED], strict=True)
    assert code == 1
    assert lines[0] == "FAILED tests/test_d.py::test_d - skipped: no docker"
    assert "skipped under the scheduled run" in lines[1]


@pytest.mark.parametrize("found", [[], [SKIPPED]])
def test_a_run_that_executed_nothing_is_red_whoever_ran_it(found):
    code, lines = e2e.verdict(0, found, strict=False)
    assert code == 1
    assert lines[-1] == "e2e: no local test executed, which is not a pass"


def test_an_empty_run_keeps_pytest_s_own_exit_code():
    assert e2e.verdict(5, [], strict=False)[0] == 5


@pytest.mark.parametrize(
    ("env", "expected"), [({}, False), ({"DEVKIT_E2E": ""}, False), ({"DEVKIT_E2E": "1"}, True)]
)
def test_scheduled_reads_the_flag_the_pass_sets(env, expected):
    assert e2e.scheduled(env) is expected


@pytest.mark.parametrize("rel", [".venv/Scripts/python.exe", ".venv/bin/python"])
def test_the_checkout_s_own_interpreter_wins(tmp_path, rel):
    python = tmp_path / rel
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    assert e2e.interpreter(tmp_path, "fallback") == str(python)


def test_a_checkout_with_no_venv_uses_the_fallback(tmp_path):
    assert e2e.interpreter(tmp_path, "fallback") == "fallback"


def test_the_whole_tier_runs_when_nothing_is_named():
    argv = e2e.pytest_argv("py", Path("r.xml"))
    assert argv[:5] == ["py", "-m", "pytest", "tests", "-m"]
    assert argv[5] == "local"
    assert "--junitxml=r.xml" in argv


def test_named_ids_replace_the_directory_and_stay_inside_the_tier():
    argv = e2e.pytest_argv("py", Path("r.xml"), ["tests/test_a.py::test_x"])
    assert "tests" not in argv
    assert argv[3:6] == ["tests/test_a.py::test_x", "-m", "local"]


def fake_pytest(tmp_path: Path, junit: str | None, returncode: int, seen: dict[str, object]):
    """A `subprocess.run` that writes `junit` where the script's argv says to."""

    def run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        if junit is not None:
            report = tmp_path / e2e.JUNIT
            assert not report.exists(), "the last run's report was not cleared"
            report.write_text(junit, encoding="utf-8")
        return subprocess.CompletedProcess(argv, returncode, stdout=b"FAILED tests/x.py::y\n")

    return run


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(e2e, "REPO_ROOT", tmp_path)
    monkeypatch.delenv(e2e.ENV_FLAG, raising=False)
    return tmp_path


def test_main_runs_pytest_in_the_checkout_windowless(checkout):
    seen: dict[str, object] = {}
    code = e2e.main(["tests/test_a.py"], run=fake_pytest(checkout, JUNIT, 1, seen))
    assert code == 1
    assert seen["cwd"] == checkout
    assert seen["creationflags"] == e2e.NO_WINDOW
    assert seen["stderr"] == subprocess.STDOUT
    assert "tests/test_a.py" in str(seen["argv"])


def test_main_prints_each_failure_where_the_pass_reads_it(checkout, capsys):
    e2e.main([], run=fake_pytest(checkout, JUNIT, 1, {}))
    out = capsys.readouterr().out
    assert "FAILED tests/x.py::y\n" in out
    assert "FAILED tests/test_c.py::TestD::test_broke - errored: fixture blew up" in out
    assert "report in logs/e2e-junit.xml" in out


def test_main_clears_the_last_report_so_a_dead_run_cannot_read_green(checkout):
    report = checkout / e2e.JUNIT
    report.parent.mkdir()
    report.write_text(JUNIT, encoding="utf-8")
    assert e2e.main([], run=fake_pytest(checkout, None, 0, {})) == 1
    assert not report.exists()


def test_main_fails_a_skip_only_under_the_scheduler(checkout, monkeypatch):
    junit = """<testsuite>
<testcase classname="tests.test_a" name="test_x"/>
<testcase classname="tests.test_b" name="test_y"><skipped message="no docker"/></testcase>
</testsuite>"""
    assert e2e.main([], run=fake_pytest(checkout, junit, 0, {})) == 0
    monkeypatch.setenv(e2e.ENV_FLAG, "1")
    assert e2e.main([], run=fake_pytest(checkout, junit, 0, {})) == 1


def collected(*args: str) -> str:
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout


TIER = ("tests/test_local_console.py::", "tests/e2e/test_app.py::")


def test_the_default_run_deselects_the_tier_and_the_script_s_selection_reaches_it():
    default = collected()
    selected = collected("-m", e2e.MARKER)
    for tier in TIER:
        assert tier not in default
        assert tier in selected
    assert "tests/test_package_data.py::" in default
    assert "tests/test_package_data.py::" not in selected

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("e2e_script", ROOT / "scripts" / "e2e.py")
assert spec is not None and spec.loader is not None
e2e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2e)


def test_it_selects_the_local_tier():
    command = e2e.pytest_command("python")
    assert command[:3] == ["python", "-m", "pytest"]
    assert command[command.index("-m", 2) + 1] == "local"
    assert e2e.E2E_TESTS in command


def test_failures_are_listed_for_the_fix_pass():
    assert "-rfEs" in e2e.pytest_command("python")


def test_a_run_of_nothing_is_not_green():
    assert e2e.exit_code(0) == 0
    assert e2e.exit_code(5) != 0


def test_main_runs_pytest_from_the_root(monkeypatch):
    calls = []

    class Done:
        returncode = 3

    def fake_run(command, **kwargs):
        calls.append((command, kwargs["cwd"]))
        return Done()

    monkeypatch.setattr(e2e.subprocess, "run", fake_run)
    assert e2e.main() == 3
    assert calls[0][1] == ROOT

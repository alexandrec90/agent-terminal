import queue
import threading

import pytest

from agent_terminal import pty_session
from agent_terminal.pty_session import PtyProcess, PtySession, SpawnError

TIMEOUT = 5


class FakeProcess:
    """A process whose output a test feeds in, one read at a time."""

    def __init__(self) -> None:
        self.pid: int | None = 1234
        self.exitstatus: int | None = None
        self.alive = True
        self.written: list[str] = []
        self.sizes: list[tuple[int, int]] = []
        self.reads: queue.Queue[str | Exception] = queue.Queue()

    def read(self, _size: int) -> str:
        item = self.reads.get(timeout=TIMEOUT)
        if isinstance(item, Exception):
            raise item
        return item

    def write(self, text: str) -> None:
        if not self.alive:
            raise EOFError("Pty is closed")
        self.written.append(text)

    def setwinsize(self, rows: int, cols: int) -> None:
        self.sizes.append((rows, cols))

    def isalive(self) -> bool:
        return self.alive

    def finish(self, code: int) -> None:
        self.alive = False
        self.exitstatus = code
        self.reads.put(EOFError("Pty is closed"))


class Harness:
    def __init__(self) -> None:
        self.process = FakeProcess()
        self.output: queue.Queue[str] = queue.Queue()
        self.exited: queue.Queue[int | None] = queue.Queue()
        self.killed: list[int] = []
        self.spawned: list[tuple] = []
        self.session = PtySession(
            on_output=self.output.put,
            on_exit=self.exited.put,
            spawn=self.spawn,
            kill=self.killed.append,
        )

    def spawn(self, argv, cwd, env, dimensions) -> PtyProcess:
        self.spawned.append((argv, cwd, env, dimensions))
        return self.process

    def start(self) -> None:
        self.session.start(["pwsh"], "C:\\work", {"A": "1"}, (24, 80))


@pytest.fixture
def harness() -> Harness:
    return Harness()


def test_start_hands_everything_to_spawn(harness):
    harness.start()
    assert harness.spawned == [(["pwsh"], "C:\\work", {"A": "1"}, (24, 80))]
    harness.process.finish(0)


def test_output_reaches_the_callback(harness):
    harness.start()
    harness.process.reads.put("hello")
    assert harness.output.get(timeout=TIMEOUT) == "hello"
    harness.process.finish(0)


def test_an_empty_read_is_not_output(harness):
    harness.start()
    harness.process.reads.put("")
    harness.process.reads.put("next")
    assert harness.output.get(timeout=TIMEOUT) == "next"
    harness.process.finish(0)


def test_the_exit_code_is_reported(harness):
    harness.start()
    harness.process.finish(3)
    assert harness.exited.get(timeout=TIMEOUT) == 3


def test_a_failed_read_ends_the_session(harness):
    harness.start()
    harness.process.reads.put(OSError("broken"))
    assert harness.exited.get(timeout=TIMEOUT) is None


def test_write_and_resize_reach_the_process(harness):
    harness.start()
    harness.session.write("x")
    harness.session.resize(40, 120)
    assert harness.process.written == ["x"]
    assert harness.process.sizes == [(40, 120)]
    harness.process.finish(0)


def test_input_to_a_finished_process_is_dropped(harness):
    harness.start()
    harness.process.finish(0)
    harness.session.write("late")
    assert harness.process.written == []


def test_write_and_resize_before_start_do_nothing(harness):
    harness.session.write("early")
    harness.session.resize(10, 10)
    assert harness.process.written == []


def test_resize_of_a_finished_process_is_skipped(harness):
    harness.start()
    harness.process.finish(0)
    harness.session.resize(10, 10)
    assert harness.process.sizes == []


def test_close_kills_the_tree_and_reports_no_exit(harness):
    harness.start()
    harness.session.close()
    assert harness.killed == [1234]
    harness.process.finish(0)
    threads = [t for t in threading.enumerate() if t.name == "pty-reader"]
    for thread in threads:
        thread.join(timeout=TIMEOUT)
    assert harness.exited.empty()


def test_closing_twice_kills_once(harness):
    harness.start()
    harness.session.close()
    harness.session.close()
    assert harness.killed == [1234]
    harness.process.finish(0)


def test_closing_what_never_started_is_harmless(harness):
    harness.session.close()
    assert harness.killed == []


def fake_winpty(spawn):
    class Fake:
        class WinptyError(Exception):
            pass

        class PtyProcess:
            pass

    Fake.PtyProcess.spawn = staticmethod(spawn)
    return Fake


def test_conpty_spawn_uses_winpty(monkeypatch):
    calls = []

    def spawn(argv, cwd, env, dimensions):
        calls.append((argv, cwd, env, dimensions))
        return "process"

    fake = fake_winpty(spawn)
    monkeypatch.setattr(pty_session.importlib, "import_module", lambda name: fake)
    assert pty_session.conpty_spawn(["a"], "c", {"K": "V"}, (1, 2)) == "process"
    assert calls == [(["a"], "c", {"K": "V"}, (1, 2))]


@pytest.mark.parametrize("error", ["missing", "winpty"])
def test_conpty_spawn_names_a_failure_as_one_error(monkeypatch, error):
    holder = {}

    def spawn(*_args, **_kwargs):
        raise FileNotFoundError("pwsh") if error == "missing" else holder["fake"].WinptyError("no")

    holder["fake"] = fake_winpty(spawn)
    monkeypatch.setattr(pty_session.importlib, "import_module", lambda name: holder["fake"])
    with pytest.raises(SpawnError):
        pty_session.conpty_spawn(["a"], "c", {}, (1, 2))


def test_kill_tree_names_the_pid(monkeypatch):
    calls = []
    monkeypatch.setattr(pty_session.subprocess, "run", lambda args, **_: calls.append(args))
    pty_session.kill_tree(42)
    assert calls == [["taskkill.exe", "/T", "/F", "/PID", "42"]]

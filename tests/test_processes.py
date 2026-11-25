import os
import subprocess
import sys

import pytest

from agent_terminal import processes
from agent_terminal.processes import (
    TICKS_PER_SECOND,
    ProcessEntry,
    process_created,
    process_parents,
    started_later,
)


def later(parents: dict[int, int], seconds: dict[int, float], grace: float = 30) -> bool:
    def created(pid: int) -> int | None:
        return int(seconds[pid] * TICKS_PER_SECOND) if pid in seconds else None

    return started_later(1, parents, created, grace)


def test_nothing_under_a_process_is_nothing_later():
    assert not later({1: 0, 9: 8}, {1: 100, 9: 900})


def test_what_started_with_it_is_not_later():
    assert not later({1: 0, 2: 1, 3: 2}, {1: 100, 2: 105, 3: 129})


def test_a_grandchild_started_long_after_is_later():
    assert later({1: 0, 2: 1, 3: 2}, {1: 100, 2: 105, 3: 400})


def test_a_child_older_than_its_parent_belongs_to_another_process():
    assert not later({1: 0, 2: 1, 3: 2}, {1: 100, 2: 50, 3: 400})


def test_a_process_that_has_gone_has_nothing_under_it():
    assert not later({2: 1}, {2: 400})
    assert not later({1: 0, 2: 1}, {1: 100})


def test_a_loop_of_parents_ends():
    assert not later({1: 2, 2: 1}, {1: 100, 2: 100})


def test_elsewhere_than_windows_nothing_is_known(monkeypatch):
    monkeypatch.setattr(processes.sys, "platform", "linux")
    assert process_parents() is None
    assert process_created(os.getpid()) is None


def test_the_entry_has_room_for_a_path():
    assert ProcessEntry.szExeFile.size >= 260


@pytest.mark.local
def test_a_real_child_is_found_with_its_start_time():
    child = subprocess.Popen([sys.executable, "-c", "input()"], stdin=subprocess.PIPE)
    try:
        parents = process_parents()
        assert parents is not None
        assert parents[child.pid] == os.getpid()
        own, its = process_created(os.getpid()), process_created(child.pid)
        assert own is not None
        assert its is not None
        assert own < its
        assert started_later(os.getpid(), parents, process_created, 0)
    finally:
        child.communicate(b"\n")

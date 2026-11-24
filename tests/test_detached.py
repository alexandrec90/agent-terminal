import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_terminal import detached as module
from agent_terminal.detached import spawn_detached


class Started:
    pid = 55


class Popen:
    def __init__(self, refusals: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self.refusals = refusals

    def __call__(self, argv: list[str], **options: Any) -> Started:
        self.calls.append({"argv": argv, **options})
        if len(self.calls) <= self.refusals:
            raise OSError("access denied")
        return Started()


def test_starts_without_a_window_or_any_tie_to_the_app(tmp_path):
    popen = Popen()
    assert spawn_detached(["python", "-m", "x"], tmp_path, popen) == 55
    call = popen.calls[0]
    assert call["cwd"] == tmp_path
    assert call["stdin"] is subprocess.DEVNULL
    assert call["creationflags"] & module.DETACHED_FLAGS == module.DETACHED_FLAGS


def test_a_job_that_refuses_breakaway_gets_a_plain_start(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "BREAKAWAY", 0x01000000)
    popen = Popen(refusals=1)
    assert spawn_detached(["x"], tmp_path, popen) == 55
    flags = [call["creationflags"] for call in popen.calls]
    assert flags == [module.DETACHED_FLAGS | 0x01000000, module.DETACHED_FLAGS]


def test_a_start_that_always_fails_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "BREAKAWAY", 0x01000000)
    with pytest.raises(OSError, match="access denied"):
        spawn_detached(["x"], Path(tmp_path), Popen(refusals=5))

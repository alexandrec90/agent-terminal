import ctypes
import sys
from pathlib import Path

import pytest

from agent_terminal import recycle
from agent_terminal.recycle import FileOperation


def request(path: str = "C:\\x") -> FileOperation:
    return recycle.delete_request(recycle.path_list(Path(path)))


def test_the_request_recycles_rather_than_deletes():
    operation = request("C:\\projects\\old")
    assert operation.wFunc == recycle.FO_DELETE
    assert operation.fFlags & recycle.FOF_ALLOWUNDO
    assert operation.pFrom == "C:\\projects\\old"


def test_the_request_asks_nothing_of_its_own():
    """The page has already asked; a shell dialog behind the window would hang the call."""
    flags = request().fFlags
    assert flags & recycle.FOF_NOCONFIRMATION
    assert flags & recycle.FOF_NOERRORUI
    assert flags & recycle.FOF_SILENT


def test_the_path_list_ends_with_an_empty_entry():
    """Without the second terminator Windows reads on past the path, into whatever follows."""
    paths = recycle.path_list(Path("C:\\x"))
    assert paths[:] == "C:\\x\0\0"


def test_file_operation_has_the_layout_windows_reads():
    """Seven pointers' worth and two small fields, at natural alignment: a field out of
    place would hand Windows the wrong path."""
    pointer = ctypes.sizeof(ctypes.c_void_p)
    assert FileOperation.pFrom.offset == 2 * pointer
    assert FileOperation.fFlags.offset == 4 * pointer


def test_nothing_is_deleted_off_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(OSError, match="only on Windows"):
        recycle.send_to_recycle_bin(tmp_path)
    assert tmp_path.exists()

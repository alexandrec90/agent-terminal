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


def test_a_code_is_said_in_words():
    """0x78 is what a folder with a locked-down ACL inside it gave; alone it said nothing."""
    text = recycle.failure_message(Path("C:\\p\\radio"), 0x78, [])
    assert text == (
        "Windows could not recycle C:\\p\\radio: "
        "Windows denied access to something inside it (code 0x78)."
    )


def test_an_unlisted_code_points_at_its_documentation():
    assert "SHFileOperationW" in recycle.failure_message(Path("C:\\x"), 0x9999, [])


def test_the_failure_names_what_refused():
    refused = [(Path("C:\\p\\radio\\.pytest_cache"), recycle.ACCESS_DENIED)]
    text = recycle.failure_message(Path("C:\\p\\radio"), 0x78, refused)
    assert text.endswith("Refused: C:\\p\\radio\\.pytest_cache (access is denied).")


def test_many_refusals_name_a_few_and_count_the_rest():
    refused = [(Path(f"C:\\p\\f{n}"), recycle.SHARING_VIOLATION) for n in range(5)]
    text = recycle.failure_message(Path("C:\\p"), 0x20, refused)
    assert "C:\\p\\f2 (a file in it is open in another program)" in text
    assert "C:\\p\\f3" not in text
    assert text.endswith("; and 2 more.")


def test_a_reported_success_that_left_the_folder_says_so():
    text = recycle.failure_message(Path("C:\\p"), None, [])
    assert text == "Windows reported recycling C:\\p, but it is still there."


def test_every_item_is_asked_including_the_root(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "file").write_text("x")
    (tmp_path / "top").write_text("x")
    asked: list[str] = []

    def refusal(path: str) -> int:
        asked.append(path)
        return recycle.ACCESS_DENIED if path.endswith("file") else 0

    refused = recycle.refused_items(tmp_path, refusal)
    assert sorted(asked) == sorted(
        str(p) for p in (tmp_path, tmp_path / "sub", tmp_path / "sub" / "file", tmp_path / "top")
    )
    assert refused == [(tmp_path / "sub" / "file", recycle.ACCESS_DENIED)]


def test_nothing_is_deleted_off_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(OSError, match="only on Windows"):
        recycle.send_to_recycle_bin(tmp_path)
    assert tmp_path.exists()

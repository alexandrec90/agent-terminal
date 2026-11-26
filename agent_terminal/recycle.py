"""The Windows Recycle Bin. A project deleted from the app goes there, not out of
existence: a click in a list is too easy to make for a delete that cannot be undone."""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
# Undoable, and with no shell dialog of its own: the page already asked.
RECYCLE_FLAGS = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI

# `SHFileOperationW` answers with its own pre-Win32 `DE_*` codes as well as Win32 ones.
# Its documentation lists them; the code alone tells the user nothing.
ACCESS_DENIED = 0x5
SHARING_VIOLATION = 0x20
DE_ACCESSDENIEDSRC = 0x78
RESULT_MEANINGS = {
    ACCESS_DENIED: "access is denied",
    SHARING_VIOLATION: "a file in it is open in another program",
    0x75: "the operation was cancelled",
    DE_ACCESSDENIEDSRC: "Windows denied access to something inside it",
    0x79: "a path in it is too deep",
    0x7C: "the path is not valid",
    0x81: "a name in it is too long",
    0x402: "an unknown error",
}
# Codes that mean some item refused deletion: worth finding which.
DENIED_CODES = frozenset({ACCESS_DENIED, SHARING_VIOLATION, DE_ACCESSDENIEDSRC})
# How many refusing items a message names; the rest are counted.
NAMED_REFUSALS = 3

# CreateFileW, asked only whether DELETE would be granted: nothing is changed.
DELETE_ACCESS = 0x00010000
SHARE_ALL = 0x7
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000  # needed to open a folder at all


class FileOperation(ctypes.Structure):
    """`SHFILEOPSTRUCTW`, at the natural alignment 64-bit Windows declares it with."""

    _fields_ = (
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    )


def path_list(path: Path) -> ctypes.Array[ctypes.c_wchar]:
    """`pFrom`'s memory: a list ended by an empty entry, so one path needs two
    terminators. A buffer keeps both; a plain string would stop at the first."""
    return ctypes.create_unicode_buffer(f"{path}\0")


def delete_request(paths: ctypes.Array[ctypes.c_wchar]) -> FileOperation:
    """The operation that recycles what `paths` lists. The caller keeps `paths` alive
    until the call is made."""
    return FileOperation(
        hwnd=None,
        wFunc=FO_DELETE,
        pFrom=ctypes.cast(paths, ctypes.c_wchar_p),
        pTo=None,
        fFlags=RECYCLE_FLAGS,
        fAnyOperationsAborted=0,
        hNameMappings=None,
        lpszProgressTitle=None,
    )


def delete_refusal(path: str) -> int:
    """The Win32 error Windows gives for opening `path` for deletion, or 0 if it would
    allow it. Opens a handle and closes it; deletes nothing."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.CreateFileW(
        path, DELETE_ACCESS, SHARE_ALL, None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None
    )
    if handle is None or handle == ctypes.c_void_p(-1).value:
        return ctypes.get_last_error()
    kernel32.CloseHandle(handle)
    return 0


def tree(root: Path) -> Iterator[Path]:
    """`root` and everything under it. A folder that cannot be listed is still yielded:
    it may be the very thing refusing."""
    yield root
    for top, folders, files in os.walk(root):
        for name in (*folders, *files):
            yield Path(top) / name


def refused_items(
    root: Path, refusal: Callable[[str], int] = delete_refusal
) -> list[tuple[Path, int]]:
    """Each item under `root` that Windows would not let be deleted, with its error."""
    found = []
    for item in tree(root):
        code = refusal(str(item))
        if code:
            found.append((item, code))
    return found


def meaning(code: int) -> str:
    return RESULT_MEANINGS.get(code, "see SHFileOperationW's return values")


def failure_message(path: Path, code: int | None, refused: list[tuple[Path, int]]) -> str:
    """Why `path` was not recycled, naming what refused where that is known. A `code`
    of None is a call that reported success and left the path in place."""
    if code is None:
        text = f"Windows reported recycling {path}, but it is still there."
    else:
        text = f"Windows could not recycle {path}: {meaning(code)} (code {code:#x})."
    if refused:
        named = [f"{item} ({meaning(error)})" for item, error in refused[:NAMED_REFUSALS]]
        text += " Refused: " + "; ".join(named)
        if len(refused) > NAMED_REFUSALS:
            text += f"; and {len(refused) - NAMED_REFUSALS} more"
        text += "."
    return text


def send_to_recycle_bin(path: Path) -> None:
    """Move a file or folder to the Recycle Bin. Raises `OSError` if it is not gone,
    saying why and, where some item refused, which."""
    if sys.platform != "win32":
        raise OSError("the Recycle Bin is only on Windows")
    paths = path_list(path)
    operation = delete_request(paths)
    shell32 = ctypes.WinDLL("shell32")
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(FileOperation)]
    code = shell32.SHFileOperationW(ctypes.byref(operation))
    if code != 0 or operation.fAnyOperationsAborted:
        refused = refused_items(path) if code in DENIED_CODES else []
        raise OSError(failure_message(path, code, refused))
    if path.exists():
        refused = refused_items(path)
        raise OSError(failure_message(path, None, refused))

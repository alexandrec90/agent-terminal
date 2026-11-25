"""The Windows Recycle Bin. A project deleted from the app goes there, not out of
existence: a click in a list is too easy to make for a delete that cannot be undone."""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
# Undoable, and with no shell dialog of its own: the page already asked.
RECYCLE_FLAGS = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI


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


def send_to_recycle_bin(path: Path) -> None:
    """Move a file or folder to the Recycle Bin. Raises `OSError` if it is not gone."""
    if sys.platform != "win32":
        raise OSError("the Recycle Bin is only on Windows")
    paths = path_list(path)
    operation = delete_request(paths)
    shell32 = ctypes.WinDLL("shell32")
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(FileOperation)]
    code = shell32.SHFileOperationW(ctypes.byref(operation))
    if code != 0 or operation.fAnyOperationsAborted:
        raise OSError(f"Windows could not recycle {path} (code {code:#x})")
    if path.exists():
        raise OSError(f"{path} is still there: something in it may be open")

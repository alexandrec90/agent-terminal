"""The Windows clipboard, reached from here rather than from the page.

In the window, `navigator.clipboard.readText()` was seen not to resolve at all (permission
state "prompt", on a focused page), which would leave a right-click paste doing nothing.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

log = logging.getLogger(__name__)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
OPEN_ATTEMPTS = 10
OPEN_RETRY_SECONDS = 0.02


@contextmanager
def open_clipboard(user32: Any) -> Iterator[bool]:
    """Yield whether the clipboard could be opened. Another program holding it is normal
    and brief, so opening it is retried."""
    for _ in range(OPEN_ATTEMPTS):
        if user32.OpenClipboard(None):
            try:
                yield True
            finally:
                user32.CloseClipboard()
            return
        time.sleep(OPEN_RETRY_SECONDS)
    log.warning("clipboard stayed busy")
    yield False


def read_text() -> str:
    """The clipboard's text, or "" if it holds none or stays busy."""
    if sys.platform != "win32":
        return ""
    user32 = ctypes.WinDLL("user32")
    kernel32 = ctypes.WinDLL("kernel32")
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    with open_clipboard(user32) as opened:
        handle = user32.GetClipboardData(CF_UNICODETEXT) if opened else None
        pointer = kernel32.GlobalLock(handle) if handle else None
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)


def write_text(text: str) -> None:
    """Put text on the clipboard, which owns the memory once it accepts it. Not through
    `clip.exe`: that keeps the byte-order mark it needs to read UTF-16, as text."""
    if sys.platform != "win32":
        return
    user32 = ctypes.WinDLL("user32")
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p

    data = ctypes.create_unicode_buffer(text)
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, ctypes.sizeof(data))
    pointer = kernel32.GlobalLock(handle)
    ctypes.memmove(pointer, data, ctypes.sizeof(data))
    kernel32.GlobalUnlock(handle)
    with open_clipboard(user32) as opened:
        if opened:
            user32.EmptyClipboard()
            if user32.SetClipboardData(CF_UNICODETEXT, handle):
                return
    kernel32.GlobalFree(handle)

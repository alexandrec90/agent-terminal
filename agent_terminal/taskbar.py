"""Gives the app a taskbar identity of its own, so a pinned button keeps the app's icon.

Without one, Windows files the window under the process that owns it, pythonw.exe: the
button groups with every other Python program, and pinning it pins the interpreter, which
relaunches with no arguments and shows the Python icon. An explicit AppUserModelID on the
process and the window separates the button; the window's relaunch properties tell the
pin what to run and what icon to wear.

`ctypes.windll` exists on Windows only, so it is reached inside the functions that need it.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from agent_terminal.instance import WINDOW_TITLE

log = logging.getLogger(__name__)

APP_ID = "AgentTerminal.AgentTerminal"

# The shell's System.AppUserModel property set, and the IPropertyStore interface.
APP_MODEL_KEYS = "{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"
PID_RELAUNCH_COMMAND = 2
PID_RELAUNCH_ICON = 3
PID_RELAUNCH_DISPLAY_NAME = 4
PID_ID = 5
IID_PROPERTY_STORE = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"
VT_LPWSTR = 31
# IPropertyStore's vtable, after IUnknown's three slots.
SLOT_RELEASE = 2
SLOT_GET_VALUE = 5
SLOT_SET_VALUE = 6
SLOT_COMMIT = 7


def relaunch_command(argv0: str, executable: str) -> str:
    """The command a pinned button runs: the `agent-terminal` launcher this copy started
    from, else the windowed interpreter beside this one, so no console flashes up."""
    started_as = Path(argv0)
    if started_as.name:
        launcher = started_as.with_suffix(".exe")
        if launcher.is_file():
            return f'"{launcher.resolve()}"'
    interpreter = Path(executable)
    windowed = interpreter.with_name("pythonw.exe")
    if windowed.is_file():
        interpreter = windowed
    return f'"{interpreter}" -m agent_terminal'


def icon_resource(icon: Path) -> str:
    """An icon in the shell's `file,index` form."""
    return f"{icon},0"


def window_properties(command: str, icon: Path) -> list[tuple[int, str]]:
    """What the window carries, in order: the ID first, since the relaunch properties
    apply only to a window that has one."""
    return [
        (PID_ID, APP_ID),
        (PID_RELAUNCH_COMMAND, command),
        (PID_RELAUNCH_DISPLAY_NAME, WINDOW_TITLE),
        (PID_RELAUNCH_ICON, icon_resource(icon)),
    ]


def claim_process_identity() -> None:
    """Set the process's ID, before any window exists, so none is ever filed under Python."""
    if sys.platform != "win32":
        return
    import ctypes

    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)


class _Com:
    """The handful of ctypes declarations the property store calls need."""

    def __init__(self) -> None:
        import ctypes

        class Guid(ctypes.Structure):
            _fields_ = [
                ("data1", ctypes.c_ulong),
                ("data2", ctypes.c_ushort),
                ("data3", ctypes.c_ushort),
                ("data4", ctypes.c_ubyte * 8),
            ]

        class PropertyKey(ctypes.Structure):
            _fields_ = [("fmtid", Guid), ("pid", ctypes.c_ulong)]

        class PropVariant(ctypes.Structure):
            # vt and three reserved words, then a 16-byte union whose first member is
            # the string pointer.
            _fields_ = [
                ("vt", ctypes.c_ushort),
                ("reserved", ctypes.c_ushort * 3),
                ("pointer", ctypes.c_void_p),
                ("rest", ctypes.c_void_p),
            ]

        # `Any`: typeshed has no `windll` off Windows, and the gate type-checks on Linux.
        self.ctypes: Any = ctypes
        self.Guid = Guid
        self.PropertyKey = PropertyKey
        self.PropVariant = PropVariant
        self.ole32 = self.ctypes.windll.ole32

    def guid(self, text: str) -> Any:
        value = self.Guid()
        hresult = self.ole32.CLSIDFromString(text, self.ctypes.byref(value))
        if hresult:
            raise OSError(f"CLSIDFromString({text}) failed: {hresult:#x}")
        return value

    def key(self, pid: int) -> Any:
        return self.PropertyKey(self.guid(APP_MODEL_KEYS), pid)

    def method(self, store: Any, slot: int, *argtypes: Any) -> Any:
        ctypes = self.ctypes
        vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
        return prototype(vtable[slot])

    def open_store(self, hwnd: int) -> Any:
        ctypes = self.ctypes
        store = ctypes.c_void_p()
        shell32 = ctypes.windll.shell32
        shell32.SHGetPropertyStoreForWindow.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(self.Guid),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        shell32.SHGetPropertyStoreForWindow.restype = ctypes.HRESULT
        shell32.SHGetPropertyStoreForWindow(
            hwnd, ctypes.byref(self.guid(IID_PROPERTY_STORE)), ctypes.byref(store)
        )
        return store

    def release(self, store: Any) -> None:
        ctypes = self.ctypes
        vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[SLOT_RELEASE])(store)


def set_window_identity(hwnd: int, command: str, icon: Path) -> None:
    """Write the ID and the relaunch properties onto a window. Raises `OSError` when the
    shell refuses one."""
    com = _Com()
    ctypes = com.ctypes
    com.ole32.CoInitialize(None)
    store = com.open_store(hwnd)
    try:
        set_value = com.method(
            store, SLOT_SET_VALUE, ctypes.POINTER(com.PropertyKey), ctypes.POINTER(com.PropVariant)
        )
        for pid, text in window_properties(command, icon):
            buffer = ctypes.create_unicode_buffer(text)
            value = com.PropVariant(vt=VT_LPWSTR, pointer=ctypes.addressof(buffer))
            set_value(store, ctypes.byref(com.key(pid)), ctypes.byref(value))
        com.method(store, SLOT_COMMIT)(store)
    finally:
        com.release(store)


def read_window_property(hwnd: int, pid: int) -> str | None:
    """One of the window's System.AppUserModel strings, or None if it has none."""
    com = _Com()
    ctypes = com.ctypes
    com.ole32.CoInitialize(None)
    store = com.open_store(hwnd)
    try:
        get_value = com.method(
            store, SLOT_GET_VALUE, ctypes.POINTER(com.PropertyKey), ctypes.POINTER(com.PropVariant)
        )
        value = com.PropVariant()
        get_value(store, ctypes.byref(com.key(pid)), ctypes.byref(value))
        try:
            if value.vt != VT_LPWSTR:
                return None
            return ctypes.wstring_at(value.pointer)
        finally:
            com.ole32.PropVariantClear(ctypes.byref(value))
    finally:
        com.release(store)


def find_app_window() -> int | None:
    """The app's top-level window, found by its title as `instance` finds it."""
    if sys.platform != "win32":
        return None
    import ctypes

    return ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE) or None


def brand_window(window: Any, icon: Path, timeout: float = 30.0) -> None:
    """Once pywebview's window is on screen, give it the app's taskbar identity. Runs on
    pywebview's worker thread; a failure is logged, since the window works without it."""
    if not window.events.shown.wait(timeout):
        log.warning("window not shown after %ss; taskbar identity not set", timeout)
        return
    hwnd = find_app_window()
    if hwnd is None:
        log.warning("no window titled %r; taskbar identity not set", WINDOW_TITLE)
        return
    try:
        set_window_identity(hwnd, relaunch_command(sys.argv[0], sys.executable), icon)
    except OSError:
        log.exception("could not set the window's taskbar identity")

"""Keep one interactive agent per Windows user-data directory.

Kernel objects outlive neither their owning process nor its crash. A named event
lets a later Desktop-shortcut launch ask the existing tray app to show its window
without opening a second audio engine or writing the same settings concurrently.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import queue
import threading


ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258


class SingleInstance:
    def __init__(self, data_root: Path, show_existing: bool = True) -> None:
        self.primary = True
        self._kernel = None
        self._mutex = None
        self._event = None
        self._watcher: threading.Thread | None = None
        self._closing = threading.Event()
        if os.name != "nt":
            return

        # Local\ scopes the objects to the logon session. The resolved data path
        # keeps a packaged app and isolated LOCALAPPDATA smoke checks separate.
        identity = hashlib.sha256(str(data_root.resolve()).casefold().encode("utf-8")).hexdigest()[:24]
        name = f"Local\\Deckster-{identity}"
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CreateEventW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p)
        kernel.CreateEventW.restype = ctypes.c_void_p
        kernel.SetEvent.argtypes = (ctypes.c_void_p,)
        kernel.SetEvent.restype = ctypes.c_int
        kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel.CloseHandle.restype = ctypes.c_int
        self._kernel = kernel

        # Create the event first. A contender can then signal it even if the
        # primary has not started its window thread yet; auto-reset retains the
        # request until the watcher begins waiting.
        self._event = kernel.CreateEventW(None, False, False, name + "-show")
        if not self._event:
            raise OSError(ctypes.get_last_error(), "CreateEventW failed")
        self._mutex = kernel.CreateMutexW(None, False, name + "-mutex")
        if not self._mutex:
            error = ctypes.get_last_error()
            self.close()
            raise OSError(error, "CreateMutexW failed")
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            self.primary = False
            if show_existing and not kernel.SetEvent(self._event):
                error = ctypes.get_last_error()
                self.close()
                raise OSError(error, "SetEvent failed")
            self.close()

    def watch_show_requests(self, commands: queue.Queue, stop: threading.Event) -> None:
        if not self.primary or self._event is None:
            return

        def watch() -> None:
            while not self._closing.is_set() and not stop.is_set():
                result = self._kernel.WaitForSingleObject(self._event, 250)
                if result == WAIT_OBJECT_0 and not self._closing.is_set() and not stop.is_set():
                    commands.put("show")
                elif result != WAIT_TIMEOUT:
                    break

        self._watcher = threading.Thread(target=watch, name="DecksterShowRequest", daemon=True)
        self._watcher.start()

    def close(self) -> None:
        self._closing.set()
        if self._watcher is not None and self._watcher is not threading.current_thread():
            self._watcher.join(timeout=1)
            self._watcher = None
        if self._kernel is not None:
            for attribute in ("_mutex", "_event"):
                handle = getattr(self, attribute)
                if handle:
                    self._kernel.CloseHandle(handle)
                    setattr(self, attribute, None)

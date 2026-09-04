"""Wired USB-C path via `adb reverse`.

`adb reverse tcp:PORT tcp:PORT` makes the phone's own localhost:PORT tunnel to the
agent through the USB cable, so the phone's browser reaches the agent without the
traffic ever touching the network. Combined with a loopback bind, this is the
secure default (BUILD-PLAN.md 1, HTML 4).

Subprocess I/O is injectable so command construction and device parsing are unit
testable without a real adb binary or phone (the live behaviour is troubleshoot-
after-build).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from typing import Callable

from ..config import resource_root
from ..log import get_logger

log = get_logger("transport.adb")

# runner(args) -> (returncode, stdout, stderr)
Runner = Callable[[list[str]], "tuple[int, str, str]"]

BUNDLED_ADB = resource_root() / "bin" / "adb" / "adb.exe"


def _no_window_kwargs() -> dict:
    """On Windows, run child processes without flashing a console window.

    The watcher runs `adb` every couple of seconds; in the windowed (no-console)
    exe each call otherwise pops a cmd window that flickers on screen.
    """
    if sys.platform != "win32":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": 0x08000000, "startupinfo": si}  # CREATE_NO_WINDOW


def _default_runner(args: list[str]) -> tuple[int, str, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=10,
                           **_no_window_kwargs())
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def parse_devices(adb_devices_output: str) -> list[str]:
    """Parse `adb devices` output into a list of serials in the 'device' state.

    Ignores the header line and any device that is 'unauthorized' or 'offline'
    (e.g. the phone has not yet accepted the USB-debugging prompt).
    """
    serials: list[str] = []
    for line in adb_devices_output.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def reverse_args(adb: str, port: int) -> list[str]:
    return [adb, "reverse", f"tcp:{port}", f"tcp:{port}"]


def locate_adb() -> str | None:
    """Prefer a bundled adb, else one on PATH; None if neither exists."""
    if BUNDLED_ADB.exists():
        return str(BUNDLED_ADB)
    return shutil.which("adb")


class AdbReverse:
    def __init__(self, port: int, adb_path: str | None = None,
                 runner: Runner | None = None) -> None:
        self._port = port
        self._adb = adb_path if adb_path is not None else locate_adb()
        self._run = runner or _default_runner
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._reversed_serials: set[str] = set()

    def available(self) -> bool:
        return self._adb is not None

    def devices(self) -> list[str]:
        if not self._adb:
            return []
        rc, out, _err = self._run([self._adb, "devices"])
        return parse_devices(out) if rc == 0 else []

    def apply_reverse(self) -> bool:
        if not self._adb:
            return False
        rc, _out, err = self._run(reverse_args(self._adb, self._port))
        if rc == 0:
            log.info("adb reverse active on tcp:%d", self._port)
            return True
        log.warning("adb reverse failed: %s", err.strip())
        return False

    # ---- background watcher ----------------------------------------------
    def start_watcher(self, interval: float = 2.0) -> None:
        """Poll for the phone; (re)apply reverse whenever a new device appears."""
        if not self.available():
            log.info("adb not found; wired path disabled (Wi-Fi/loopback still work)")
            return
        self._thread = threading.Thread(target=self._watch, args=(interval,),
                                        name="adb-watch", daemon=True)
        self._thread.start()

    def _watch(self, interval: float) -> None:
        log.info("adb watcher started (adb=%s)", self._adb)
        while not self._stop.is_set():
            current = set(self.devices())
            new = current - self._reversed_serials
            if new:
                if self.apply_reverse():
                    self._reversed_serials |= new
            # forget devices that went away so a reconnect re-applies
            self._reversed_serials &= current
            self._stop.wait(interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

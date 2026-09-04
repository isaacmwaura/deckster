"""Windows autostart via the per-user Run key (no admin required).

Writes HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run so the agent starts
at login for the current user only. We intentionally avoid HKLM / scheduled tasks
so nothing needs elevation (locked invariant 4).
"""
from __future__ import annotations

import sys

from .log import get_logger

log = get_logger("autostart")

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "Deckster"


def _command() -> str:
    """The command Windows should run at login.

    Frozen: the exe itself. Source checkout: pythonw -m agent.main (no console).
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{pyw}" -m agent.main'


def enable(value_name: str = _VALUE_NAME, command: str | None = None) -> bool:
    """Register autostart. Returns True on success."""
    import winreg

    cmd = command if command is not None else _command()
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, cmd)
        log.info("autostart enabled")
        return True
    except OSError:
        log.exception("failed to enable autostart")
        return False


def disable(value_name: str = _VALUE_NAME) -> bool:
    """Remove autostart. Returns True if it was removed or already absent."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, value_name)
        log.info("autostart disabled")
        return True
    except FileNotFoundError:
        return True  # already not present
    except OSError:
        log.exception("failed to disable autostart")
        return False


def is_enabled(value_name: str = _VALUE_NAME) -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, value_name)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False

"""Open the shared desktop workspace in an isolated Edge application window.

The agent remains the tray process; Edge supplies the HTML renderer already
installed on Windows. A separate loopback HTTP origin renders the desktop;
phone transport retains its pinned TLS certificate without browser exceptions.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

from .config import data_dir


def workspace_command(admin) -> list[str] | None:
    candidates = [Path(os.environ.get(key, "")) / "Microsoft/Edge/Application/msedge.exe"
                  for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
    edge = next((p for p in candidates if p.is_file()), None)
    if edge is None:
        return None
    state = admin.state()
    url = state.get("desktopUrl", "")
    if not url.startswith("http://127.0.0.1:"):
        return None
    command = [str(edge), f"--app={url}",
               f"--user-data-dir={data_dir() / 'desktop-browser'}", "--no-first-run",
               "--no-default-browser-check", "--window-size=1360,920"]
    return command


def open_workspace(admin) -> bool:
    command = workspace_command(admin)
    if command is None:
        return False
    subprocess.Popen(command, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return True

"""Run hidden Tk gestures in their own process to isolate Tcl from engine tests."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def test_hidden_layout_drag_save_and_remote_conflict():
    case = Path(__file__).with_name("_layout_panel_case.py")
    result = subprocess.run([sys.executable, str(case)], capture_output=True,
                            text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr

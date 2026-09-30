"""Cross-process Windows guard: shortcut show, hidden no-op, and crash recovery."""
from __future__ import annotations

import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import pytest

from agent.single_instance import SingleInstance


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows named objects")
PROBE = Path(__file__).with_name("_single_instance_probe.py")


def _probe(root: Path, action: str) -> str:
    completed = subprocess.run([sys.executable, str(PROBE), str(root), action],
                               capture_output=True, text=True, check=True, timeout=10)
    return completed.stdout.strip()


def test_second_shortcut_signals_owner_but_hidden_launch_does_not(tmp_path):
    commands = queue.Queue()
    stop = threading.Event()
    owner = SingleInstance(tmp_path)
    try:
        assert owner.primary
        owner.watch_show_requests(commands, stop)
        assert _probe(tmp_path, "hidden") == "SECONDARY"
        with pytest.raises(queue.Empty):
            commands.get(timeout=.4)
        assert _probe(tmp_path, "show") == "SECONDARY"
        assert commands.get(timeout=3) == "show"
    finally:
        stop.set()
        owner.close()
    assert _probe(tmp_path, "show") == "PRIMARY"


def test_crashed_owner_releases_kernel_guard(tmp_path):
    child = subprocess.Popen([sys.executable, str(PROBE), str(tmp_path), "hold"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
    try:
        assert child.stdout.readline().strip() == "PRIMARY"
        assert _probe(tmp_path, "hidden") == "SECONDARY"
    finally:
        child.kill()
        child.wait(timeout=5)
    assert _probe(tmp_path, "show") == "PRIMARY"

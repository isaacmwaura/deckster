"""Windows Firewall exception helper for the Wi-Fi path.

Pure logic only — no real netsh calls: the subprocess and platform are stubbed so
the tests run identically on any OS/CI without elevation.
"""
import subprocess

import pytest

from agent import firewall


class _Result:
    def __init__(self, returncode, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


@pytest.fixture
def win(monkeypatch):
    """Pretend we're on Windows so the helpers are active."""
    monkeypatch.setattr(firewall.sys, "platform", "win32")


def _stub_run(monkeypatch, result=None, exc=None):
    def fake_run(*a, **k):
        if exc is not None:
            raise exc
        return result
    monkeypatch.setattr(firewall.subprocess, "run", fake_run)


def test_rule_exists_true_when_present(win, monkeypatch):
    _stub_run(monkeypatch, _Result(0, "Rule Name: Deckster (inbound)\nEnabled: Yes\n"))
    assert firewall.rule_exists() is True


def test_rule_exists_false_when_missing(win, monkeypatch):
    _stub_run(monkeypatch, _Result(1, "No rules match the specified criteria.\n"))
    assert firewall.rule_exists() is False


def test_rule_exists_false_on_error(win, monkeypatch):
    _stub_run(monkeypatch, exc=OSError("netsh not found"))
    assert firewall.rule_exists() is False


def test_needs_rule_true_on_lan_without_rule(win, monkeypatch):
    monkeypatch.setattr(firewall, "rule_exists", lambda: False)
    assert firewall.needs_rule("lan") is True


def test_needs_rule_false_on_loopback(win, monkeypatch):
    monkeypatch.setattr(firewall, "rule_exists", lambda: False)
    assert firewall.needs_rule("loopback") is False


def test_needs_rule_false_when_rule_present(win, monkeypatch):
    monkeypatch.setattr(firewall, "rule_exists", lambda: True)
    assert firewall.needs_rule("lan") is False


def test_not_supported_off_windows(monkeypatch):
    monkeypatch.setattr(firewall.sys, "platform", "linux")
    assert firewall.supported() is False
    assert firewall.needs_rule("lan") is False
    assert firewall.add_rule_elevated() is False

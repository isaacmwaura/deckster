"""Windows Firewall exception for the Wi-Fi (LAN) path.

Over USB everything tunnels through adb on localhost, so no firewall rule is ever
needed. Over Wi-Fi the phone must reach the PC's LAN IP directly, and on a **Public**
network profile Windows drops all unsolicited inbound by default — so the agent is
unreachable until an inbound rule allows it. That was the single biggest reason the
Wi-Fi path "didn't work" on-device (see docs/on-device-2026-09-05.md).

Adding a rule needs elevation, so — unlike autostart, which is deliberately HKCU-only —
this is an explicit, one-time, user-initiated action: the PC window offers a button
that pops a single UAC prompt. We allow the *program* (not just a port), so the rule
keeps working if the port ever changes and covers mDNS (UDP 5353) from the same
process. Reading rule state (`show rule`) does not require elevation.

Everything is best-effort and Windows-only; on any other platform these are no-ops.
"""
from __future__ import annotations

import subprocess
import sys

from .log import get_logger

log = get_logger("firewall")

RULE_NAME = "Deckster (inbound)"


def _no_window_kwargs() -> dict:
    """Run child processes without flashing a console window (windowed exe)."""
    if sys.platform != "win32":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": 0x08000000, "startupinfo": si}  # CREATE_NO_WINDOW


def _program() -> str:
    """The executable the rule should allow inbound.

    Frozen: the Deckster exe itself. Source checkout: the running python, so the dev
    build is reachable too (broad, but dev-only).
    """
    return sys.executable


def supported() -> bool:
    return sys.platform == "win32"


def rule_exists() -> bool:
    """True if our inbound rule is present. Read-only; needs no elevation.

    Never raises: on any error (netsh missing, access quirk) we report False so the
    UI simply offers to add the rule rather than hiding the option.
    """
    if not supported():
        return False
    try:
        p = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={RULE_NAME}"],
            capture_output=True, text=True, timeout=8, **_no_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # A missing rule exits non-zero and prints "No rules match...".
    return p.returncode == 0 and "No rules match" not in p.stdout


def needs_rule(mode: str) -> bool:
    """True when we're on Wi-Fi (LAN) on Windows and the inbound rule is missing."""
    return supported() and mode == "lan" and not rule_exists()


def add_rule_elevated() -> bool:
    """Add the inbound rule, prompting once for elevation (UAC).

    Idempotent: deletes any existing rule of the same name first, then re-adds it.
    Returns True if the elevated command was launched (the user still has to accept
    the UAC prompt); False if launching failed or it's not Windows. The UAC-elevated
    cmd runs hidden, so it never flashes a console.
    """
    if not supported():
        return False
    prog = _program()
    inner = (
        f'netsh advfirewall firewall delete rule name="{RULE_NAME}" >nul 2>&1 & '
        f'netsh advfirewall firewall add rule name="{RULE_NAME}" '
        f'dir=in action=allow program="{prog}" enable=yes profile=any'
    )
    try:
        import ctypes

        # ShellExecuteW with the "runas" verb triggers the UAC prompt; SW_HIDE=0.
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "cmd.exe", f'/c {inner}', None, 0
        )
        ok = int(rc) > 32
        if ok:
            log.info("firewall rule add requested (elevated) for %s", prog)
        else:
            log.warning("firewall rule add not launched (ShellExecute=%s)", rc)
        return ok
    except Exception:  # noqa: BLE001 - never crash the UI over a firewall helper
        log.exception("failed to request firewall rule")
        return False

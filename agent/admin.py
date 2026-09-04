"""Local settings surface for the PC agent.

The agent already owns everything the user would want to configure (bind mode,
pairing, paired devices, autostart), so it exposes a small settings API that a
localhost-only page drives. This is the ".exe handles most of the settings" hub:
the tray opens http://localhost:<port>/admin in the PC browser.

Everything here is gated to loopback requests in the server (see server._is_local)
so a phone on the LAN can never reach it, even in Wi-Fi mode.
"""
from __future__ import annotations

import sys
from typing import Any


class Admin:
    """Bundles the settings actions over the live Runtime, pairing, and allow-list."""

    def __init__(self, runtime, pairing, allowlist, fingerprint: str = "") -> None:
        self._rt = runtime
        self._pairing = pairing
        self._allow = allowlist
        self._fingerprint = fingerprint

    def state(self) -> dict[str, Any]:
        """A snapshot for the settings page. Never raises — degrades to defaults."""
        from . import __version__
        from .net import lan_ip

        try:
            autostart_on = _autostart_is_enabled()
        except Exception:  # noqa: BLE001 - registry hiccup shouldn't blank the page
            autostart_on = False
        return {
            "version": __version__,
            "mode": self._rt.mode,                       # "loopback" (USB) | "lan" (Wi-Fi)
            "connectUrl": str(self._rt.connect["url"]),
            "connectNote": str(self._rt.connect["note"]),
            "port": self._rt.port,
            "lanIp": lan_ip(),
            "pairCode": self._pairing.current_code(),
            "devices": self._allow.list_devices(),
            "autostart": autostart_on,
            "frozen": bool(getattr(sys, "frozen", False)),
            "secure": bool(getattr(self._rt, "secure", False)),
            "fingerprint": self._fingerprint,
            "qrPath": str(getattr(self._rt, "qr_path", "") or ""),
        }

    def set_mode(self, mode: str) -> str:
        """Switch USB(loopback)/Wi-Fi(lan). Returns the resulting mode."""
        return self._rt.apply_mode(mode)

    def set_secure(self, enabled: bool) -> bool:
        """Turn HTTPS on/off. Returns the resulting state."""
        return self._rt.apply_secure(enabled)

    def refresh_code(self) -> str:
        """Roll the pairing code and return the new one."""
        return self._pairing.refresh()

    def revoke(self, device_id: str) -> bool:
        return self._allow.revoke(device_id)

    def revoke_all(self) -> None:
        self._allow.revoke_all()

    def set_autostart(self, enabled: bool) -> bool:
        from . import autostart
        return autostart.enable() if enabled else autostart.disable()


def _autostart_is_enabled() -> bool:
    from . import autostart
    return autostart.is_enabled()

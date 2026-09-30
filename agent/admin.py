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

    def __init__(self, runtime, pairing, allowlist, fingerprint: str = "",
                 audio_state=None, soundboard=None, presentation=None) -> None:
        self._rt = runtime
        self._pairing = pairing
        self._allow = allowlist
        self._fingerprint = fingerprint
        self._audio_state = audio_state
        self._soundboard = soundboard
        self._presentation = presentation
        self._loop = None

    def set_loop(self, loop) -> None:
        self._loop = loop

    def _publish_presentation(self, result: dict[str, Any]) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._audio_state.set_presentation, result)
        else:
            self._audio_state.set_presentation(result)

    def presentation_state(self) -> dict[str, Any]:
        return {"presentation": self._presentation.snapshot(),
                "sessions": list(self._audio_state.sessions.values()),
                "clips": self._soundboard.snapshot()["clips"],
                "devices": dict(self._audio_state.devices)}

    def audition_soundboard_clip(self, clip_id: str) -> None:
        self._soundboard.audition(clip_id, self._audio_state.devices.get("outputs", []))

    def configure_presentation(self, changes: dict[str, Any], base_revision: int) -> dict[str, Any]:
        result = self._presentation.update(changes, base_revision)
        self._publish_presentation(result)
        return result

    def remove_soundboard_clip(self, clip_id: str) -> None:
        if not self._soundboard.remove_clip(clip_id):
            raise ValueError("unknown soundboard clip")
        changed = self._presentation.remove_clip(clip_id)
        if changed is not None:
            self._publish_presentation(changed)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._audio_state.set_soundboard, self.soundboard_state())

    def soundboard_state(self) -> dict[str, Any]:
        if self._audio_state is None or self._soundboard is None:
            return {"config": {}, "inputs": [], "outputs": [], "runtime": "unavailable", "error": ""}
        devices = self._audio_state.devices
        return self._soundboard.snapshot(devices.get("outputs", []), devices.get("inputs", []))

    def configure_soundboard(self, config: dict[str, Any]) -> dict[str, Any]:
        devices = self._audio_state.devices
        self._soundboard.configure(config, devices.get("outputs", []), devices.get("inputs", []))
        return self.soundboard_state()

    def test_soundboard_route(self, bus: str) -> None:
        self._soundboard.test_tone(bus)

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
            "desktopUrl": getattr(self._rt, "desktop_url", ""),
            "desktopStarting": getattr(self._rt, "desktop_starting", False),
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
            "firewallNeeded": _firewall_needed(self._rt.mode),
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

    def allow_firewall(self) -> bool:
        """Add the inbound firewall rule for the Wi-Fi path (prompts for elevation)."""
        from . import firewall
        return firewall.add_rule_elevated()


def _autostart_is_enabled() -> bool:
    from . import autostart
    return autostart.is_enabled()


def _firewall_needed(mode: str) -> bool:
    from . import firewall
    try:
        return firewall.needs_rule(mode)
    except Exception:  # noqa: BLE001 - a firewall check should never blank the page
        return False

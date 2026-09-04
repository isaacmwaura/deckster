"""Configuration and persisted-state locations.

All machine-managed state lives under %LOCALAPPDATA%\\StreamControl\\. Config is
plain JSON (not TOML) so we carry no extra dependency on Python 3.10, and it is
rarely hand-edited. First run generates a random port fallback and a token salt.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

APP_NAME = "StreamControl"
DEFAULT_PORT = 8765


def resource_root() -> Path:
    """Base directory for bundled assets (web/, bin/).

    When frozen by PyInstaller the assets live under sys._MEIPASS; in a source
    checkout they sit next to the agent package (repo root).
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent

# Bind modes: "loopback" (wired USB-C / adb reverse; safest) or "lan" (Wi-Fi).
BIND_LOOPBACK = "127.0.0.1"
BIND_LAN = "0.0.0.0"


def data_dir() -> Path:
    """Return the per-user data directory, creating it if needed."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _settings_path() -> Path:
    return data_dir() / "settings.json"


def _default_settings() -> dict[str, Any]:
    return {
        "port": DEFAULT_PORT,
        # "loopback" is the secure default per the design; "lan" requires TLS + token.
        "mode": "loopback",
        # Serve HTTPS with a self-signed cert (the Android app pins its fingerprint).
        "secure": False,
        # Random salt used when hashing device tokens (see security.tokens).
        "token_salt": secrets.token_hex(16),
        # Pairing code lifetime and attempt throttle.
        "pair_code_ttl_seconds": 180,
        "pair_max_attempts": 5,
        # Audio session poll interval (ms) for detecting PC-side changes.
        "poll_interval_ms": 400,
    }


def load_settings() -> dict[str, Any]:
    """Load settings.json, creating it with defaults on first run.

    Missing keys are backfilled from defaults so upgrades never crash on an old
    file. Any newly added key is persisted immediately.
    """
    path = _settings_path()
    defaults = _default_settings()
    if not path.exists():
        save_settings(defaults)
        return defaults

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("settings.json is not an object")
    except (json.JSONDecodeError, ValueError, OSError):
        # Corrupt file: start clean rather than crash the agent on boot.
        save_settings(defaults)
        return defaults

    changed = False
    for key, value in defaults.items():
        if key not in data:
            data[key] = value
            changed = True
    if changed:
        save_settings(data)
    return data


def save_settings(settings: dict[str, Any]) -> None:
    """Write settings atomically (temp file + replace) to avoid a torn write."""
    path = _settings_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    tmp.replace(path)


def bind_host(settings: dict[str, Any]) -> str:
    """Resolve the interface to bind based on mode."""
    return BIND_LOOPBACK if settings.get("mode") == "loopback" else BIND_LAN

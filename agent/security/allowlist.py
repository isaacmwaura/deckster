"""Persistent device allow-list.

Maps a device id to {name, token_hash, paired_at}. This is the authority on which
devices may control the PC; revoking a device is simply removing its entry. Only
token *hashes* are stored (see tokens.py).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .tokens import verify_token


class AllowList:
    def __init__(self, path: Path, salt: str) -> None:
        self._path = path
        self._salt = salt
        self._devices: dict[str, dict[str, Any]] = {}
        self._load()

    # ---- persistence ------------------------------------------------------
    def _load(self) -> None:
        if self._path.exists():
            try:
                self._devices = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._devices = {}

    def _save(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._devices, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # ---- operations -------------------------------------------------------
    def add(self, device_id: str, name: str, token_hash: str) -> None:
        self._devices[device_id] = {
            "name": name,
            "token_hash": token_hash,
            "paired_at": int(time.time()),
        }
        self._save()

    def revoke(self, device_id: str) -> bool:
        if device_id in self._devices:
            del self._devices[device_id]
            self._save()
            return True
        return False

    def revoke_all(self) -> None:
        self._devices = {}
        self._save()

    def list_devices(self) -> list[dict[str, Any]]:
        return [{"id": did, "name": d["name"], "paired_at": d["paired_at"]}
                for did, d in self._devices.items()]

    def find_by_token(self, token: str) -> str | None:
        """Return the device id whose stored hash matches this token, or None."""
        for did, d in self._devices.items():
            if verify_token(token, d["token_hash"], self._salt):
                return did
        return None

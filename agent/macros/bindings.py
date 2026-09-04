"""Per-app input-mute bindings.

Windows has no per-application microphone mute, so a voice app's mic-mute chip
can't drive a real OS control. Instead each app id is bound to the key combo the
*app itself* listens for (e.g. Discord's "toggle mute" hotkey); tapping the chip
injects that combo via the P3 macro path (agent.macros.input.send_combo).

We can't read the app's resulting mute state back, so the toggle is optimistic on
the client; the agent only stores the binding and fires the keystroke. Bindings
persist in app_bindings.json next to the other agent state.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .input import parse_combo


def _norm_app_id(app_id: str) -> str:
    """Match the backend's session id slug so bindings survive re-polls."""
    base = re.sub(r"\.exe$", "", (app_id or "").lower(), flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", "-", base).strip("-") or "app"


class AppInputBindings:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._bindings: dict[str, dict[str, Any]] = {}
        self._load()

    # ---- persistence ------------------------------------------------------
    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._bindings = {str(k): dict(v) for k, v in data.items()}
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                self._bindings = {}

    def _save(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._bindings, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # ---- operations -------------------------------------------------------
    def list(self) -> dict[str, dict[str, Any]]:
        return dict(self._bindings)

    def get(self, app_id: str) -> dict[str, Any] | None:
        return self._bindings.get(_norm_app_id(app_id))

    def set(self, app_id: str, keys: str, label: str | None = None) -> dict[str, Any]:
        """Bind an app id to a key combo. Raises ComboError if the combo is invalid."""
        parse_combo(keys)  # validation only; result discarded
        aid = _norm_app_id(app_id)
        binding = {"keys": keys.strip(), "label": (label or "").strip()[:60]}
        self._bindings[aid] = binding
        self._save()
        return binding

    def remove(self, app_id: str) -> bool:
        aid = _norm_app_id(app_id)
        if aid in self._bindings:
            del self._bindings[aid]
            self._save()
            return True
        return False

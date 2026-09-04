"""Persisted registry of hotkey macros.

A macro is {id, label, keys} where `keys` is a combo string like "ctrl+shift+m".
Combos are validated (parsed) on add so a bad hotkey is rejected at creation, not
at fire time. Stored in macros.json next to the other agent state.
"""
from __future__ import annotations

import json
import re
import secrets
from pathlib import Path
from typing import Any

from .input import ComboError, parse_combo


def _slug(label: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return (base or "macro")[:32]


class MacroRegistry:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._macros: dict[str, dict[str, Any]] = {}
        self._load()

    # ---- persistence ------------------------------------------------------
    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._macros = {m["id"]: m for m in data}
            except (json.JSONDecodeError, OSError, KeyError, TypeError):
                self._macros = {}

    def _save(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(list(self._macros.values()), indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # ---- operations -------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        return list(self._macros.values())

    def get(self, macro_id: str) -> dict[str, Any] | None:
        return self._macros.get(macro_id)

    def add(self, label: str, keys: str) -> dict[str, Any]:
        """Create a macro after validating the combo. Raises ComboError if invalid."""
        parse_combo(keys)  # validation only; result discarded
        label = (label or keys).strip()[:60]
        macro_id = f"{_slug(label)}-{secrets.token_hex(3)}"
        macro = {"id": macro_id, "label": label, "keys": keys.strip()}
        self._macros[macro_id] = macro
        self._save()
        return macro

    def remove(self, macro_id: str) -> bool:
        if macro_id in self._macros:
            del self._macros[macro_id]
            self._save()
            return True
        return False

"""Persist the phone's layout independently of soundboard audio routing."""
from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any


POSITIONS = frozenset({"top", "right", "bottom", "left", "center"})
PAGE_NAMES = frozenset({"soundboard", "devices", "media"})
LEGACY_DEFAULT_KEYS = frozenset({
    "crickets", "rimshot", "applause", "air-horn", "sad-trombone",
    "record-scratch", "drum-roll", "censor-bleep", "success", "wrong",
    "bell", "pop",
})


class RevisionConflict(ValueError):
    """A client edited a presentation that has changed since it read it."""


class PresentationService:
    def __init__(self, root: Path, soundboard) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "presentation.json"
        self._lock = threading.RLock()
        self._soundboard = soundboard
        clips = soundboard.snapshot()["clips"]
        legacy_layout = soundboard.snapshot().get("config", {}).get("layout")
        pages = {"soundboard": "left" if legacy_layout == "b" else "top",
                 "devices": "right", "media": "bottom"}
        slots = [str(c["id"]) for c in clips[:12]]
        default = {"revision": 0, "appOrder": [], "hiddenApps": [],
                   "pages": pages, "padSlots": slots + [None] * (12 - len(slots)), "hideInteraction": "drag"}
        if not self.path.exists():
            self._data = default
            self._save()
        else:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                # A library clip may have disappeared after a crash between its
                # deletion and the presentation write. Keep the blank position.
                cleaned = False
                if isinstance(data, dict) and isinstance(data.get("padSlots"), list):
                    known = {str(c["id"]) for c in clips}
                    revised = [x if x is None or x in known else None for x in data["padSlots"]]
                    cleaned = revised != data["padSlots"]
                    data["padSlots"] = revised
                    if cleaned and type(data.get("revision")) is int:
                        data["revision"] += 1
                self._validate(data, self._clip_ids())
                self._data = data
                if cleaned:
                    self._save()
            except (OSError, ValueError, TypeError, KeyError):
                # Retain the original bytes for diagnosis or manual recovery.
                backup = self.path.with_name("presentation.corrupt.json")
                suffix = 1
                while backup.exists():
                    backup = self.path.with_name(f"presentation.corrupt-{suffix}.json")
                    suffix += 1
                shutil.copy2(self.path, backup)
                self._data = default
                self._save()

    def _clip_ids(self) -> set[str]:
        return {str(c["id"]) for c in self._soundboard.snapshot()["clips"]}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def _save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _validate(data: dict[str, Any], clip_ids: set[str]) -> None:
        if not isinstance(data, dict) or type(data.get("revision")) is not int or data["revision"] < 0:
            raise ValueError("invalid presentation revision")
        for key in ("appOrder", "hiddenApps"):
            ids = data.get(key)
            if not isinstance(ids, list) or any(not isinstance(x, str) or not x for x in ids) or len(ids) != len(set(ids)):
                raise ValueError(f"{key} must contain unique app IDs")
        pages = data.get("pages")
        legacy = isinstance(pages, dict) and set(pages) == PAGE_NAMES
        if (not isinstance(pages, dict) or set(pages) not in (PAGE_NAMES, PAGE_NAMES | {"mixer"}) or
                any(p not in POSITIONS for p in pages.values()) or
                len(set(pages.values())) != len(pages) or
                (legacy and "center" in pages.values())):
            raise ValueError("pages must occupy three different directions or four unique positions")
        if not legacy and "center" not in pages.values():
            raise ValueError("The center is empty. Move a page into the center before saving.")
        if data.get("hideInteraction", "drag") not in ("drag", "tap"):
            raise ValueError("unknown Hide interaction")
        slots = data.get("padSlots")
        if not isinstance(slots, list) or len(slots) != 12:
            raise ValueError("padSlots must contain exactly 12 positions")
        assigned = [x for x in slots if x is not None]
        if (any(not isinstance(x, str) or x not in clip_ids for x in assigned) or
                len(assigned) != len(set(assigned))):
            raise ValueError("padSlots must contain unique existing clip IDs or blanks")

    def update(self, changes: dict[str, Any], base_revision: int) -> dict[str, Any]:
        if type(base_revision) is not int:
            raise ValueError("baseRevision is required")
        if not isinstance(changes, dict) or not changes or set(changes) - {"appOrder", "hiddenApps", "pages", "padSlots", "hideInteraction"}:
            raise ValueError("unknown or empty presentation changes")
        with self._lock:
            if base_revision != self._data["revision"]:
                raise RevisionConflict("Phone layout changed elsewhere. Reload and try again.")
            next_data = self.snapshot()
            next_data.update(json.loads(json.dumps(changes)))
            next_data["revision"] += 1
            self._validate(next_data, self._clip_ids())
            previous = self._data
            self._data = next_data
            try:
                self._save()
            except OSError:
                self._data = previous
                raise
            return self.snapshot()

    def remove_clip(self, clip_id: str) -> dict[str, Any] | None:
        """Clear assignments after a library deletion, preserving empty slots."""
        with self._lock:
            if clip_id not in self._data["padSlots"]:
                return None
            previous = self.snapshot()
            self._data["padSlots"] = [None if x == clip_id else x for x in self._data["padSlots"]]
            self._data["revision"] += 1
            try:
                self._save()
            except OSError:
                self._data = previous
                raise
            return self.snapshot()

    def restore_defaults(self, base_revision: int) -> dict[str, Any]:
        """Reset only presentation. Never mutate library, routes or pairing."""
        clips = self._soundboard.snapshot()["clips"]
        # Prefer the original starter pads, then fill gaps with available sounds.
        defaults = [str(c["id"]) for c in clips
                    if str(c["id"]).removeprefix("default-") in LEGACY_DEFAULT_KEYS]
        remaining = [str(c["id"]) for c in clips if str(c["id"]) not in defaults]
        slots = (defaults + remaining)[:12]
        return self.update({"appOrder": [], "hiddenApps": [],
                            "pages": {"soundboard": "top", "devices": "right", "media": "bottom"},
                            "padSlots": slots + [None] * (12 - len(slots)),
                            "hideInteraction": "drag"}, base_revision)

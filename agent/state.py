"""Canonical in-memory state plus a pub/sub fan-out to WebSocket clients.

State lives here as the single source of truth. The server diffs updates and
pushes them to every authed client. The audio engine (P1) writes here; clients
read a snapshot on subscribe and receive incremental `state` messages after.

Design notes:
- This object is owned by the asyncio event loop. The audio thread never touches
  it directly; it hands updates back to the loop via `AudioEngine` callbacks that
  call `apply_*` on the loop thread.
- Subscribers are asyncio.Queue instances; a slow client cannot block others
  because each has its own bounded queue (drops oldest on overflow).
"""
from __future__ import annotations

import asyncio
from typing import Any


class AppState:
    def __init__(self) -> None:
        # sessions: id -> {id, appLabel, boundKey, level, muted, active}
        self.sessions: dict[str, dict[str, Any]] = {}
        # devices master + lists
        self.devices: dict[str, Any] = {
            "outputs": [],       # [{id, name, isDefault}]
            "inputs": [],        # [{id, name, isDefault}]
            "speakerMaster": {"level": 1.0, "muted": False},
            "micMaster": {"level": 1.0, "muted": False},
            # live signal peaks (0..1) for the default endpoints; updated each poll
            "meters": {"output": 0.0, "input": 0.0},
        }
        self.macros: list[dict[str, Any]] = []
        # per-app input-mute bindings: appId -> {"keys": str, "label": str}
        self.app_bindings: dict[str, dict[str, Any]] = {}
        # now-playing media sessions (SMTC); see media.MediaService
        self.media: list[dict[str, Any]] = []
        self._subscribers: set[asyncio.Queue] = set()

    # ---- subscription plumbing -------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _broadcast(self, message: dict[str, Any]) -> None:
        """Enqueue a message to all subscribers, dropping oldest if a queue is full."""
        for q in self._subscribers:
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                pass

    # ---- snapshot ---------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "t": "snapshot",
            "sessions": list(self.sessions.values()),
            "devices": self.devices,
            "macros": self.macros,
            "appInputBindings": self.app_bindings,
            "media": self.media,
        }

    # ---- mutations (called on the event loop thread) ----------------------
    def replace_sessions(self, sessions: list[dict[str, Any]]) -> None:
        """Replace the whole session set (used by the poll loop) and broadcast a snapshot.

        A full snapshot is simplest and cheap at our scale; incremental session
        diffing can come later if it ever matters.
        """
        self.sessions = {s["id"]: s for s in sessions}
        self._broadcast(self.snapshot())

    def apply_session(self, session_id: str, level: float | None = None,
                      muted: bool | None = None, active: bool | None = None) -> None:
        s = self.sessions.get(session_id)
        if s is None:
            return
        if level is not None:
            s["level"] = level
        if muted is not None:
            s["muted"] = muted
        if active is not None:
            s["active"] = active
        self._broadcast({
            "t": "state",
            "target": {"kind": "session", "id": session_id},
            "level": s["level"], "muted": s["muted"], "active": s["active"],
        })

    def apply_master(self, kind: str, level: float | None = None,
                     muted: bool | None = None) -> None:
        key = "speakerMaster" if kind == "speaker" else "micMaster"
        m = self.devices[key]
        if level is not None:
            m["level"] = level
        if muted is not None:
            m["muted"] = muted
        self._broadcast({
            "t": "state", "target": {"kind": kind},
            "level": m["level"], "muted": m["muted"],
        })

    def set_devices_lists(self, outputs: list[dict], inputs: list[dict]) -> None:
        self.devices["outputs"] = outputs
        self.devices["inputs"] = inputs
        self._broadcast(self.snapshot())

    def set_macros(self, macros: list[dict[str, Any]]) -> None:
        self.macros = macros
        self._broadcast(self.snapshot())

    def set_app_bindings(self, bindings: dict[str, dict[str, Any]]) -> None:
        self.app_bindings = bindings
        self._broadcast(self.snapshot())

    def set_media(self, media: list[dict[str, Any]]) -> None:
        self.media = media
        # A dedicated message keeps the ~1.5s media poll off the full snapshot path.
        self._broadcast({"t": "media", "media": media})

    def ingest_full(self, sessions: list[dict[str, Any]], speaker: dict[str, Any],
                    mic: dict[str, Any], outputs: list[dict], inputs: list[dict],
                    meters: dict[str, float] | None = None) -> None:
        """Atomically replace everything from one poll and broadcast a single snapshot.

        Used by the audio poll loop so each poll produces exactly one snapshot
        rather than several partial broadcasts.
        """
        self.sessions = {s["id"]: s for s in sessions}
        self.devices["speakerMaster"] = speaker
        self.devices["micMaster"] = mic
        self.devices["outputs"] = outputs
        self.devices["inputs"] = inputs
        if meters is not None:
            self.devices["meters"] = meters
        self._broadcast(self.snapshot())

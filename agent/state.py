"""Canonical in-memory state plus a pub/sub fan-out to WebSocket clients.

State lives here as the single source of truth. The server diffs updates and
pushes them to every authed client. The audio engine writes here; clients
read a snapshot on subscribe and receive incremental `state` messages after.

Design notes:
- This object is owned by the asyncio event loop. The audio thread never touches
  it directly; it hands updates back to the loop via `AudioEngine` callbacks that
  call `apply_*` on the loop thread.
- Subscribers have one pending update; if they fall behind, a fresh complete
  snapshot replaces that update so partial changes cannot be lost or replayed.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any
from uuid import uuid4


class AppState:
    def __init__(self) -> None:
        self.server_epoch = uuid4().hex
        self._owner_sequences: dict[tuple[str, str], int] = {}
        self._session_membership_seq = -1
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
        # Soundboard pads and routing configuration live independently from the
        # normal system default-device state.
        self.soundboard: dict[str, Any] = {
            "clips": [], "config": {}, "configured": False,
            "runtime": "setup_required", "error": "", "outputs": [], "inputs": [],
        }
        self.presentation: dict[str, Any] = {
            "revision": 0, "appOrder": [], "hiddenApps": [],
            "pages": {"soundboard": "top", "devices": "right", "media": "bottom"},
            "padSlots": [None] * 12,
        }
        self._subscribers: set[asyncio.Queue] = set()
        # Live viewport dimensions, scoped to authenticated socket lifetime.
        self.client_viewports: dict[object, dict[str, Any]] = {}

    # ---- subscription plumbing -------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _broadcast(self, message: dict[str, Any]) -> None:
        """Keep slow screens current without replaying a backlog of old controls.

        Replacing a partial update with another partial update loses state for
        unrelated targets. A complete snapshot folds every pending change into
        one immutable message instead.
        """
        replacement = None
        for q in self._subscribers:
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                if replacement is None:
                    replacement = self.snapshot()
                pending = replacement
            else:
                pending = message
            try:
                q.put_nowait(pending)
            except asyncio.QueueFull:
                pass

    # ---- snapshot ---------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return deepcopy({
            "t": "snapshot",
            "serverEpoch": self.server_epoch,
            "capabilities": ["command-results", "owner-sequence"],
            "sessions": list(self.sessions.values()),
            "devices": self.devices,
            "macros": self.macros,
            "appInputBindings": self.app_bindings,
            "media": self.media,
            "soundboard": self.soundboard,
            "presentation": self.presentation,
        })

    # ---- mutations (called on the event loop thread) ----------------------
    def replace_sessions(self, sessions: list[dict[str, Any]]) -> None:
        """Replace the whole session set (used by the poll loop) and broadcast a snapshot.

        A full snapshot is simplest and cheap at our scale; incremental session
        diffing can come later if it ever matters.
        """
        self.sessions = {s["id"]: s for s in sessions}
        self._broadcast(self.snapshot())

    def apply_session(self, session_id: str, level: float | None = None,
                      muted: bool | None = None, active: bool | None = None,
                      owner_seq: int | None = None) -> None:
        s = self.sessions.get(session_id)
        if s is None:
            return
        if not self._accept_observation("session", session_id, owner_seq):
            return
        if level is not None:
            s["level"] = level
        if muted is not None:
            s["muted"] = muted
        if active is not None:
            s["active"] = active
        if owner_seq is not None:
            s["ownerSeq"] = owner_seq
        self._broadcast({
            "t": "state",
            "target": {"kind": "session", "id": session_id},
            "level": s["level"], "muted": s["muted"], "active": s["active"],
            **({"ownerSeq": owner_seq, "serverEpoch": self.server_epoch} if owner_seq is not None else {}),
        })

    def apply_master(self, kind: str, level: float | None = None,
                     muted: bool | None = None, owner_seq: int | None = None) -> None:
        if not self._accept_observation(kind, "", owner_seq):
            return
        key = "speakerMaster" if kind == "speaker" else "micMaster"
        m = self.devices[key]
        if level is not None:
            m["level"] = level
        if muted is not None:
            m["muted"] = muted
        if owner_seq is not None:
            m["ownerSeq"] = owner_seq
        self._broadcast({
            "t": "state", "target": {"kind": kind},
            "level": m["level"], "muted": m["muted"],
            **({"ownerSeq": owner_seq, "serverEpoch": self.server_epoch} if owner_seq is not None else {}),
        })

    def _accept_observation(self, kind: str, target_id: str, sequence: int | None) -> bool:
        if sequence is None:
            return True  # Legacy test/extension observations remain supported.
        key = kind, target_id
        if sequence <= self._owner_sequences.get(key, -1):
            return False
        self._owner_sequences[key] = sequence
        return True

    def set_devices_lists(self, outputs: list[dict], inputs: list[dict],
                          owner_seq: int | None = None) -> None:
        if not self._accept_observation("devices", "", owner_seq):
            return
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
        if media == self.media:
            return
        self.media = deepcopy(media)
        # A dedicated message keeps the ~1.5s media poll off the full snapshot path.
        self._broadcast({"t": "media", "media": self.media})

    def set_soundboard(self, soundboard: dict[str, Any]) -> None:
        if soundboard == self.soundboard:
            return
        self.soundboard = deepcopy(soundboard)
        self._broadcast({"t": "soundboard", "soundboard": self.soundboard})

    def set_presentation(self, presentation: dict[str, Any]) -> None:
        if presentation.get("revision", -1) < self.presentation.get("revision", -1):
            return
        if presentation == self.presentation:
            return
        self.presentation = deepcopy(presentation)
        self._broadcast({"t": "presentation", "presentation": self.presentation})

    def ingest_full(self, sessions: list[dict[str, Any]], speaker: dict[str, Any],
                    mic: dict[str, Any], outputs: list[dict], inputs: list[dict],
                    meters: dict[str, float] | None = None,
                    owner_seq: int | None = None) -> None:
        """Atomically replace everything from one poll and broadcast a single snapshot.

        Used by the audio poll loop so each poll produces exactly one snapshot
        rather than several partial broadcasts.
        """
        before = (deepcopy(self.sessions), deepcopy({k: v for k, v in self.devices.items() if k != "meters"}))
        # A single membership watermark rejects delayed full session lists.
        # Active targets retain their own write sequence; deleted targets need
        # no indefinitely growing tombstone map.
        if owner_seq is None or owner_seq > self._session_membership_seq:
            incoming = {s["id"]: s for s in sessions}
            for session_id in self.sessions.keys() - incoming.keys():
                if self._accept_observation("session", session_id, owner_seq):
                    self.sessions.pop(session_id, None)
                    self._owner_sequences.pop(("session", session_id), None)
            for session_id, session in incoming.items():
                if self._accept_observation("session", session_id, owner_seq):
                    self.sessions[session_id] = {**session, **({"ownerSeq": owner_seq} if owner_seq is not None else {})}
            if owner_seq is not None:
                self._session_membership_seq = owner_seq
        for kind, observation in (("speaker", speaker), ("mic", mic)):
            if self._accept_observation(kind, "", owner_seq):
                self.devices[kind + "Master"] = {**observation, **({"ownerSeq": owner_seq} if owner_seq is not None else {})}
        if self._accept_observation("devices", "", owner_seq):
            self.devices["outputs"], self.devices["inputs"] = deepcopy(outputs), deepcopy(inputs)
        previous_meters = self.devices.get("meters")
        if meters is not None and self._accept_observation("meters", "", owner_seq):
            self.devices["meters"] = meters
        # Sequences prove freshness but are not themselves a control change.
        def durable(value):
            if isinstance(value, dict):
                return {k: durable(v) for k, v in value.items() if k != "ownerSeq"}
            if isinstance(value, (list, tuple)):
                return [durable(v) for v in value]
            return value
        after = (self.sessions, {k: v for k, v in self.devices.items() if k != "meters"})
        if durable(before) != durable(after):
            self._broadcast(self.snapshot())
        elif previous_meters != self.devices.get("meters"):
            self._broadcast({"t": "meters", "meters": deepcopy(self.devices["meters"]),
                             "ownerSeq": owner_seq, "serverEpoch": self.server_epoch})

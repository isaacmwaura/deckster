"""Audio backend interface + shared data types.

All methods are synchronous and are only ever called on the AudioEngine's
dedicated COM thread (see engine.py). Keeping this an explicit interface lets the
mock backend stand in for pycaw so the whole protocol/security stack is testable
without real audio hardware — the main way we sidestep the sandbox's inability to
run Discord or produce sound (BUILD-PLAN.md challenge 17).

Volume scalars are linear 0..1 in both cases, but they come from two different
Windows interfaces: per-session ISimpleAudioVolume vs endpoint
IAudioEndpointVolume (scalar). The backends keep those code paths separate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class SessionInfo:
    id: str            # stable per-app id (grouped by process); "system" for system sounds
    label: str         # human label, e.g. "Discord"
    level: float       # 0..1
    muted: bool
    active: bool = True
    boundKey: str | None = None  # optional hotkey bound to this app (P3)
    iconKey: str | None = None   # key into IconStore for the app's real exe icon (served at /icon/{key})


@dataclass
class DeviceInfo:
    id: str
    name: str
    isDefault: bool


@dataclass
class MasterInfo:
    level: float
    muted: bool


@dataclass
class DeviceLists:
    outputs: list[DeviceInfo] = field(default_factory=list)
    inputs: list[DeviceInfo] = field(default_factory=list)


class AudioBackend(Protocol):
    """Synchronous audio operations, invoked only on the engine thread."""

    def setup(self) -> None:
        """Called once on the engine thread before first use (e.g. COM init)."""
        ...

    def teardown(self) -> None:
        """Called once on the engine thread at shutdown (e.g. COM uninit)."""
        ...

    def snapshot_sessions(self) -> list[SessionInfo]: ...

    def set_session_volume(self, session_id: str, level: float) -> None: ...

    def set_session_mute(self, session_id: str, muted: bool) -> None: ...

    def get_master(self, kind: str) -> MasterInfo:
        """kind is 'speaker' or 'mic'."""
        ...

    def set_master_volume(self, kind: str, level: float) -> None: ...

    def set_master_mute(self, kind: str, muted: bool) -> None: ...

    def list_devices(self) -> DeviceLists: ...

    def set_default_output(self, device_id: str) -> None: ...

    def set_default_input(self, device_id: str) -> None: ...

    def get_peaks(self) -> dict[str, float]:
        """Live signal peaks (0..1) for the default output/input endpoints.

        Returns {"output": float, "input": float}. Metering must never raise on
        the poll path: a device that can't be metered contributes 0.0.
        """
        ...


def clamp01(x: float) -> float:
    """Clamp a level into [0, 1]; tolerate ints/strings from the wire."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if v < 0 else 1.0 if v > 1 else v

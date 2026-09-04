"""In-memory audio backend for tests and headless development.

Mirrors the pycaw backend's observable behaviour so protocol/security/state tests
run anywhere. Set-then-get is authoritative here, which is exactly what the real
backend's tests assert against real hardware.
"""
from __future__ import annotations

import math
import time

from .backend import (
    AudioBackend,
    DeviceInfo,
    DeviceLists,
    MasterInfo,
    SessionInfo,
    clamp01,
)


class MockAudioBackend(AudioBackend):
    def __init__(self) -> None:
        self._sessions: dict[str, SessionInfo] = {
            "discord": SessionInfo("discord", "Discord", 0.8, False),
            "chrome": SessionInfo("chrome", "Chrome", 0.5, False),
            "spotify": SessionInfo("spotify", "Spotify", 1.0, False),
        }
        self._master = {
            "speaker": MasterInfo(0.7, False),
            "mic": MasterInfo(0.85, False),
        }
        self._outputs = [
            DeviceInfo("out-speakers", "Speakers (Realtek HD Audio)", True),
            DeviceInfo("out-headset", "Headphones (SteelSeries Arctis 7)", False),
            DeviceInfo("out-dac", "USB DAC (FiiO K7)", False),
        ]
        self._inputs = [
            DeviceInfo("in-headset", "Headset Mic (SteelSeries Arctis 7)", True),
            DeviceInfo("in-webcam", "Webcam Mic (Logitech C920)", False),
        ]

    def setup(self) -> None:
        """No COM for the mock backend."""

    def teardown(self) -> None:
        """No COM for the mock backend."""

    def snapshot_sessions(self) -> list[SessionInfo]:
        # return copies so callers can't mutate internal state
        return [SessionInfo(**vars(s)) for s in self._sessions.values()]

    def set_session_volume(self, session_id: str, level: float) -> None:
        s = self._sessions.get(session_id)
        if s:
            s.level = clamp01(level)

    def set_session_mute(self, session_id: str, muted: bool) -> None:
        s = self._sessions.get(session_id)
        if s:
            s.muted = bool(muted)

    def get_master(self, kind: str) -> MasterInfo:
        m = self._master[kind]
        return MasterInfo(m.level, m.muted)

    def set_master_volume(self, kind: str, level: float) -> None:
        self._master[kind].level = clamp01(level)

    def set_master_mute(self, kind: str, muted: bool) -> None:
        self._master[kind].muted = bool(muted)

    def list_devices(self) -> DeviceLists:
        return DeviceLists(list(self._outputs), list(self._inputs))

    def set_default_output(self, device_id: str) -> None:
        for d in self._outputs:
            d.isDefault = d.id == device_id

    def set_default_input(self, device_id: str) -> None:
        for d in self._inputs:
            d.isDefault = d.id == device_id

    def get_peaks(self) -> dict[str, float]:
        # Time-varying so the headless/demo (--mock) UI shows live-looking meters;
        # a muted endpoint reads flat 0 like the real one would. Two out-of-phase
        # oscillators keep output and input visibly independent.
        t = time.monotonic()
        out = 0.0 if self._master["speaker"].muted else \
            0.45 + 0.35 * abs(math.sin(t * 2.3)) * (0.6 + 0.4 * abs(math.sin(t * 0.7)))
        inp = 0.0 if self._master["mic"].muted else \
            0.30 + 0.30 * abs(math.sin(t * 3.1 + 1.0)) * abs(math.sin(t * 1.3))
        return {"output": clamp01(out), "input": clamp01(inp)}

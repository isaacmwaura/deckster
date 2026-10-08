"""Mic-only DSP boundary. Preparing resources belongs to the lifecycle worker.

Processors receive captured mono voice before any soundboard mixing. They write
into caller-owned float32 storage, keep stream state across calls, and must not
load models, resize buffers, block, or enqueue work in ``process``. No denoiser is
selected here; the shipped processor preserves the captured signal exactly.
"""
from __future__ import annotations

from typing import Protocol


class MicrophoneProcessor(Protocol):
    def prepare(self, sample_rate: int, max_frames: int) -> None: ...
    def process(self, samples, output, frames: int) -> None: ...
    def reset(self) -> None: ...
    def close(self) -> None: ...
    def health(self) -> dict: ...


class BypassMicrophoneProcessor:
    """Zero-latency identity processor with no callback-owned storage."""

    def __init__(self) -> None:
        self._np = None
        self._max_frames = 0

    def prepare(self, sample_rate: int, max_frames: int) -> None:
        if sample_rate < 1 or max_frames < 1:
            raise ValueError("processor sample rate and frame capacity must be positive")
        import numpy as np
        self._np = np
        self._max_frames = max_frames

    def process(self, samples, output, frames: int) -> None:
        if self._np is None or frames > self._max_frames:
            raise RuntimeError("microphone processor has not been prepared for this block")
        self._np.copyto(output[:frames], samples[:frames])

    def reset(self) -> None:
        pass

    def close(self) -> None:
        self._np = None
        self._max_frames = 0

    def health(self) -> dict:
        return {"name": "bypass", "ready": self._np is not None, "latencyFrames": 0}

"""Configurable soundboard admission budgets and bounded callback telemetry."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class AudioRuntimeLimits:
    # Multi-minute effects remain supported. Longer recordings require an
    # explicit budget change or a future streaming renderer, never truncation.
    max_clip_seconds: float = 300.0
    max_clip_bytes: int = 64 * 1024 * 1024
    max_encoded_bytes: int = 128 * 1024 * 1024
    max_active_bytes: int = 128 * 1024 * 1024
    max_voices_per_bus: int = 12
    max_pending_decodes: int = 2
    max_source_channels: int = 8
    max_source_rate: int = 384_000
    decode_chunk_frames: int = 4096
    callback_chunk_frames: int = 8192
    metric_samples: int = 512
    playback_trace_entries: int = 64

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
            if name != "max_clip_seconds" and not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")


class PreparedAudio:
    """A preparation admission lease, held until triggered or discarded."""

    def __init__(self, samples, signature, release) -> None:
        self.samples = samples
        self.signature = signature
        self._release = release

    def __iter__(self):
        if self._release is None:
            raise RuntimeError("Prepared audio has already been consumed or discarded")
        yield self.samples
        yield self.signature

    def close(self) -> None:
        release, self._release = self._release, None
        if release is not None:
            self.samples = None
            self.signature = None
            release()

    def __del__(self):
        self.close()


class CallbackMetrics:
    """One callback writer, fixed storage; expensive summaries run off callback."""

    def __init__(self, np, capacity: int, sample_rate: int) -> None:
        self.samples = np.zeros((capacity, 3), dtype=np.float64)
        self.sample_rate = sample_rate
        self.write = 0
        self.count = 0
        self.total = 0
        self.over_budget = 0
        self.deferred = 0
        self.status_interruptions = 0
        self.input_overflows = 0
        self.output_underflows = 0
        self.last_started_ns = 0
        self.last_frames = 0
        self.max_gap_ms = 0.0

    def begin(self, started_ns: int, frames: int, status) -> None:
        if self.last_started_ns:
            expected = self.last_frames / self.sample_rate
            gap = max(0.0, (started_ns - self.last_started_ns) / 1e9 - expected)
            self.max_gap_ms = max(self.max_gap_ms, gap * 1000)
        self.last_started_ns = started_ns
        self.last_frames = frames
        if status:
            self.status_interruptions += 1
            self.input_overflows += int(bool(getattr(status, "input_overflow", False)))
            self.output_underflows += int(bool(getattr(status, "output_underflow", False)))

    def end(self, elapsed_ns: int, frames: int) -> None:
        duration_ms = elapsed_ns / 1e6
        utilization = elapsed_ns / (frames / self.sample_rate * 1e9) if frames else 0.0
        self.samples[self.write, 0] = duration_ms
        self.samples[self.write, 1] = utilization
        self.samples[self.write, 2] = frames
        self.write = (self.write + 1) % len(self.samples)
        self.count = min(len(self.samples), self.count + 1)
        self.total += 1
        self.over_budget += int(utilization > 1)

    def summary(self, np) -> dict:
        # A callback can advance while copying; this is diagnostic telemetry,
        # not an authority stamp or a sample-accurate signal recording.
        samples = self.samples[:self.count].copy()
        result = {"callbacks": self.total, "retainedCallbacks": len(samples),
                  "overBudget": self.over_budget, "deferredClipBlocks": self.deferred,
                  "statusInterruptions": self.status_interruptions,
                  "inputOverflows": self.input_overflows, "outputUnderflows": self.output_underflows,
                  "maxSchedulingGapMs": round(self.max_gap_ms, 3)}
        if len(samples):
            for index, label in ((0, "durationMs"), (1, "deadlineUtilization")):
                values = np.percentile(samples[:, index], [50, 95, 99, 100])
                result[label] = dict(zip(("p50", "p95", "p99", "max"), (round(float(v), 5) for v in values)))
            result["blockFrames"] = {"min": int(samples[:, 2].min()), "max": int(samples[:, 2].max())}
        return result

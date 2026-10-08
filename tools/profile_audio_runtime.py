"""Measure the Python renderer offline with synthetic voice and 1/6/12 effects.

This opens no devices and plays/captures no hardware audio. Timings measure
callback work only: no driver scheduling, cable transport, game load, Discord,
phone frame pacing or battery acceptance is implied. Use live diagnostics for
actual stream latency and interruptions when a listener trial is authorized.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.soundboard import SoundboardRenderer


def scenario(seconds: float, frames: int, count: int, monitor: bool, clip: Path | None):
    renderer = SoundboardRenderer()
    renderer._voice_stream = SimpleNamespace(active=True)
    renderer._ears_stream = SimpleNamespace(active=True) if monitor else None
    decode_ms = 0.0
    if clip is None:
        total_frames = round(seconds * renderer.sample_rate)
        axis = np.arange(total_frames, dtype=np.float32) / renderer.sample_rate
        samples = (.03 * np.sin(2 * np.pi * 660 * axis)).astype(np.float32)
        path = ROOT / "__offline_synthetic__.wav"
    else:
        path = clip.resolve()
        started = time.perf_counter()
        samples = renderer._load(path)
        decode_ms = (time.perf_counter() - started) * 1000
        total_frames = len(samples)
    for index in range(count):
        renderer.trigger_prepared(str(index), (samples.copy(), renderer._clip_signature(path)),
                                  path, 1, True, monitor)
    # These arrays are allocated once, before callback timing starts.
    microphone = np.full((frames, 1), .04, dtype=np.float32)
    output = np.empty((frames, 2), dtype=np.float32)
    cpu_started, started = time.process_time(), time.perf_counter()
    position = 0
    while position < total_frames:
        block_frames = min(frames, total_frames - position)
        renderer._voice_callback(microphone[:block_frames], output[:block_frames], block_frames, None, None)
        if monitor:
            renderer._ears_callback(output[:block_frames], block_frames, None, None)
        position += block_frames
    elapsed, cpu_seconds = time.perf_counter() - started, time.process_time() - cpu_started
    report = renderer.diagnostics()
    complete = report["playbackTrace"]
    if not all(t["voiceRenderedFrames"] == total_frames and t["voiceEndReason"] == "completed" for t in complete):
        raise RuntimeError("Offline clip did not render to completion")
    if monitor and not all(t["earsRenderedFrames"] == total_frames and t["earsEndReason"] == "completed" for t in complete):
        raise RuntimeError("Offline Me clip did not render to completion")
    audio_seconds = total_frames / renderer.sample_rate
    result = {"effects": count, "monitor": monitor, "blockFrames": frames,
              "audioSeconds": round(audio_seconds, 6), "renderWallMs": round(elapsed * 1000, 3),
              "renderCpuMs": round(cpu_seconds * 1000, 3),
              "renderWallPercentOfAudioTime": round(100 * elapsed / audio_seconds, 4),
              "coldDecodeMs": round(decode_ms, 3), "completedEffects": len(complete),
              "resources": report["resources"], "streams": report["streams"]}
    renderer._voice_stream = renderer._ears_stream = None
    renderer.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=5, help="Synthetic effect duration")
    parser.add_argument("--frames", type=int, default=480)
    parser.add_argument("--clip", type=Path, help="Optionally decode a real effect into offline buffers")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 300 or not 16 <= args.frames <= 8192:
        parser.error("seconds must be 1–300 and frames must be 16–8192")
    if args.clip is not None and not args.clip.is_file():
        parser.error("clip must be an existing audio file")
    report = {"scope": "Offline synthetic callback work; no devices, driver scheduling, game, Discord or physical phone",
              "scenarios": [scenario(args.seconds, args.frames, count, monitor, args.clip)
                            for count in (1, 6, 12) for monitor in (False, True)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

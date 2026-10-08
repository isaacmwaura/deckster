"""Verify the real saved WASAPI route and starter clips without changing settings.

Run deliberately: this sends test audio into the selected virtual microphone.
Captured samples stay in memory; the report contains measurements only.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading
import time
import urllib.request

import numpy as np
import sounddevice as sd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.routing import cable_pairs, route_issue
from agent.soundboard import SoundboardRenderer, SoundboardService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop-url", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--recovery-only", action="store_true", help="Skip the starter pack after testing recovery")
    args = parser.parse_args()
    base = args.desktop_url.removesuffix("/admin").rstrip("/")
    if not base.startswith("http://127.0.0.1:"):
        parser.error("Use Deckster's local desktop URL")
    with urllib.request.urlopen(base + "/admin/api/workspace", timeout=5) as response:
        workspace = json.load(response)
    snapshot = workspace["soundboard"]
    config = snapshot["config"]
    issue = route_issue(config, snapshot)
    if issue:
        raise RuntimeError(issue)
    inputs = {d["id"]: d["name"] for d in snapshot["inputs"]}
    outputs = {d["id"]: d["name"] for d in snapshot["outputs"]}
    receiver = next(p[1]["name"] for p in cable_pairs(snapshot["outputs"], snapshot["inputs"])
                    if p[0]["id"] == config["voiceOutputId"])
    renderer = SoundboardRenderer()
    try:
        renderer.start(inputs[config["inputId"]], outputs[config["voiceOutputId"]],
                       outputs.get(config.get("earsOutputId")))
        duplex = renderer.verify_receiver(receiver)
        print("Saved microphone/mixer route:", json.dumps(duplex), flush=True)
    finally:
        renderer.close()

    # Exercise the same poll-time recovery used by the installed controller.
    # All test state is isolated from the user's library and preferences.
    service = SoundboardService(args.report.parent / "recovery-test-state")
    recovery = []
    try:
        service.configure(config, snapshot["outputs"], snapshot["inputs"])
        for attempt in range(3):
            previous = service._renderer
            if previous is None:
                raise RuntimeError(service.snapshot()["error"])
            previous._voice_stream.abort()
            assert previous.health_error(), "Aborted stream did not report stopped"
            service.ensure_started(snapshot["outputs"], snapshot["inputs"])
            assert service._renderer is not previous, "Stopped renderer was not replaced"
            result = service.verify_receiver(snapshot["outputs"], snapshot["inputs"])
            recovery.append(result)
            print("Recovered stopped stream:", json.dumps(result), flush=True)
    finally:
        service.close()

    # Isolate clip transport from physical microphone noise for each file.
    renderer._np, renderer._sd = np, sd
    renderer._prepare_runtime()
    renderer._failure = ""
    capture_lock = threading.Lock()
    captured, statuses = [], []
    def capture(indata, _frames, _time, status):
        with capture_lock:
            captured.append(indata[:, 0].copy())
            if status:
                statuses.append(str(status))
    def output(outdata, frames, timestamp, status):
        renderer._voice_callback(np.zeros((frames, 1), dtype=np.float32),
                                 outdata, frames, timestamp, status)
    results = []
    try:
        settings = sd.WasapiSettings(auto_convert=True)
        with sd.InputStream(device=renderer._find_device(sd, receiver, "input"),
                            channels=1, samplerate=48000, dtype="float32",
                            callback=capture, extra_settings=settings):
            with sd.OutputStream(device=renderer._find_device(sd, outputs[config["voiceOutputId"]], "output"),
                                 channels=2, samplerate=48000, dtype="float32",
                                 callback=output, extra_settings=settings) as stream:
                renderer._voice_stream = stream
                manifest = json.loads((ROOT / "assets/default-sounds/manifest.json").read_text())
                for clip in ([] if args.recovery_only else manifest["clips"]):
                    time.sleep(.2)
                    path = ROOT / "assets/default-sounds" / clip["file"]
                    samples = renderer._load(path) * clip["gain"]
                    with capture_lock:
                        captured.clear(); statuses.clear()
                    renderer.trigger(clip["key"], path, clip["gain"], True, False)
                    deadline = time.monotonic() + len(samples) / 48000 + 2
                    while renderer.playing_ids() and time.monotonic() < deadline:
                        time.sleep(.02)
                    time.sleep(.3)
                    with capture_lock:
                        audio = np.concatenate(captured) if captured else np.zeros(0)
                        warnings = list(statuses)
                    match = renderer._probe_match(audio, samples)
                    result = {"clip": clip["key"], **match, "streamWarnings": warnings,
                              "passed": match["correlation"] >= .65 and match["gain"] >= .02
                                        and not renderer.health_error() and not warnings}
                    results.append(result)
                    print(json.dumps(result), flush=True)
    finally:
        renderer._voice_stream = None
        renderer.close()
    report = {"savedDuplexRoute": duplex, "stoppedStreamRecovery": recovery, "starterClips": results,
              "scope": "virtual_microphone_before_receiving_app_filters",
              "allPassed": duplex["passed"] and all(r["passed"] for r in recovery + results)}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if not report["allPassed"]:
        raise RuntimeError("Audio did not reliably reach the receiving virtual microphone")


if __name__ == "__main__":
    main()

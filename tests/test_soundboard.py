"""Configuration-B soundboard persistence and wire protocol.

The real WASAPI stream is optional and needs installed Windows endpoints, so the
service is tested with a recording renderer here.  This exercises the exact
device-id/configuration and polyphonic trigger contract used by the controller.
"""
from __future__ import annotations

import json
from pathlib import Path
import struct
import wave

from agent.soundboard import SoundboardRenderer, SoundboardService
from agent.presentation import PresentationService
from helpers import engine_client, hello, recv_until, run


class RecordingRenderer:
    def __init__(self):
        self.started = None
        self.triggers = []
        self.tones = []
        self.start_count = 0
        self.stopped = False

    def start(self, input_name, voice_name, ears_name=None):
        self.start_count += 1
        self.started = (input_name, voice_name, ears_name)

    def trigger(self, clip_id, path, gain, voice, ears):
        self.triggers.append((clip_id, path.name, gain, voice, ears))

    def test_tone(self, bus):
        self.tones.append(bus)

    def stop_all(self):
        self.stopped = True

    def close(self):
        pass


class FakeSoundDevice:
    @staticmethod
    def query_devices():
        return [
            {"name": "CABLE Input", "max_input_channels": 0, "max_output_channels": 2, "hostapi": 0},
            {"name": "CABLE Input (WASAPI)", "max_input_channels": 0, "max_output_channels": 2, "hostapi": 1},
        ]

    @staticmethod
    def query_hostapis(index):
        return {"name": "MME" if index == 0 else "Windows WASAPI"}


def test_measured_levels_follow_real_samples_and_decay(monkeypatch):
    import numpy as np
    import agent.soundboard as module
    clock = [10.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    renderer = SoundboardRenderer()
    renderer._np = np
    renderer._mix_into = lambda bus, output: output.fill(.2)
    output = np.zeros((8, 2), dtype=np.float32)
    renderer._voice_callback(np.full((8, 1), .3), output, 8, None, None)
    assert abs(renderer.levels()["mic"] - .3) < .001
    assert abs(renderer.levels()["sounds"] - .2) < .001
    assert abs(renderer.levels()["voice"] - .5) < .001
    renderer._ears_callback(output, 8, None, None)
    assert abs(renderer.levels()["ears"] - .2) < .001
    clock[0] += 2
    assert renderer.levels()["voice"] < .002
    renderer.close()
    assert renderer.levels() == {}


def test_signal_peaks_follow_short_audio_without_lingering(monkeypatch):
    import numpy as np
    import agent.soundboard as module
    clock = [10.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    renderer = SoundboardRenderer()
    renderer._np = np
    renderer._record_level("mic", np.array([.5]))
    assert renderer.levels()["mic"] == .5
    clock[0] += .25
    assert renderer.levels()["mic"] < .03
    renderer._record_level("mic", np.array([.9]))
    assert renderer.levels()["mic"] == .9
    clock[0] += .4
    renderer._record_level("mic", np.zeros(8))
    assert renderer.levels()["mic"] < .008


def test_signal_state_avoids_library_and_endpoint_enumeration(tmp_path, monkeypatch):
    from types import SimpleNamespace
    library = SoundboardService(tmp_path)
    monkeypatch.setattr(library, "snapshot", lambda *args: (_ for _ in ()).throw(AssertionError("Full snapshot requested")))
    library._renderer = SimpleNamespace(health_error=lambda: "", levels=lambda: {"mic": .7})
    assert library.signal_state() == {"runtime": "ready", "levels": {"mic": .7}}
    library._renderer.health_error = lambda: "Stream stopped"
    assert library.signal_state() == {"runtime": "setup_required", "levels": {}}
    library._renderer = None
    assert library.signal_state() == {"runtime": "setup_required", "levels": {}}


def test_format_check_reports_unsupported_device_without_changing_settings(monkeypatch):
    import sys
    from types import SimpleNamespace
    checks = []
    def check(**kwargs):
        checks.append(kwargs)
        if kwargs["device"] == 2:
            raise RuntimeError("Unsupported sample rate")
    fake = SimpleNamespace(query_devices=lambda index: {"default_samplerate": 44100},
                           check_input_settings=check, check_output_settings=check,
                           WasapiSettings=lambda **kw: kw)
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    renderer = SoundboardRenderer()
    monkeypatch.setattr(renderer, "_find_device", lambda sd, name, kind: 1 if name == "Mic" else 2)
    result = renderer.check_formats([("Microphone", "Mic", "input"), ("Cable", "Cable", "output")])
    assert result["endpoints"][0]["supported"]
    assert result["endpoints"][0]["defaultRate"] == 44100
    assert not result["endpoints"][1]["supported"]
    assert "Unsupported sample rate" in result["endpoints"][1]["error"]
    assert [c["channels"] for c in checks] == [1, 2]
    assert all(c["samplerate"] == 48000 for c in checks)


def _wav(path):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(48000)
        out.writeframes(struct.pack("<hhhh", 0, 1000, -1000, 0))


def test_soundboard_library_config_and_play(tmp_path):
    made = []
    def factory():
        renderer = RecordingRenderer(); made.append(renderer); return renderer

    source = tmp_path / "airhorn.wav"; _wav(source)
    no_defaults = tmp_path / "no-defaults"
    service = SoundboardService(tmp_path / "data", renderer_factory=factory,
                                defaults_root=no_defaults)
    clip = service.import_clip(source, label="Air horn", voice=True, ears=True, gain=.7)
    inputs = [{"id": "mic", "name": "Headset Mic"}]
    outputs = [{"id": "cable", "name": "CABLE Input"}, {"id": "ears", "name": "Headphones"}]
    service.configure({"inputId": "mic", "voiceOutputId": "cable", "earsOutputId": "ears"}, outputs, inputs)
    assert made[0].started == ("Headset Mic", "CABLE Input", "Headphones")
    service.test_tone("voice")
    service.test_tone("ears")
    assert made[0].tones == ["voice", "ears"]
    service.play(clip["id"])
    assert made[0].triggers == [(clip["id"], clip["file"], .7, True, True)]
    assert service.snapshot()["clips"][0]["playing"] is True
    service.stop_all()
    assert made[0].stopped is True and not service.snapshot()["clips"][0]["playing"]

    # The on-disk library survives a service restart and retains per-pad settings.
    service.update_clip(clip["id"], label="Horn", gain=2, voice=False)
    restored = SoundboardService(tmp_path / "data", renderer_factory=factory,
                                 defaults_root=no_defaults)
    saved = restored.snapshot()["clips"][0]
    assert saved["label"] == "Horn" and saved["gain"] == 1 and saved["voice"] is False
    restored.ensure_started(outputs, inputs)
    assert made[1].started == ("Headset Mic", "CABLE Input", "Headphones")


def test_renderer_device_match_is_wasapi_only():
    assert SoundboardRenderer._find_device(FakeSoundDevice, "CABLE Input", "output") == 1


def test_route_tones_are_short_quiet_and_isolated():
    import numpy as np
    import pytest

    renderer = SoundboardRenderer()
    renderer._np = np
    with pytest.raises(RuntimeError):
        renderer.test_tone("voice")
    renderer._voice_stream = object()
    with pytest.raises(RuntimeError):
        renderer.test_tone("ears")
    renderer._ears_stream = object()
    for bus, other in (("voice", "ears"), ("ears", "voice")):
        renderer.test_tone(bus)
        renderer.test_tone(bus)  # repeat must replace the previous test
        assert len(renderer._voices[bus]) == 1
        assert renderer._voices[other] == []
        samples = renderer._mix(bus, renderer.sample_rate)
        assert 0 < np.max(np.abs(samples)) <= .161
        assert np.all(samples[int(renderer.sample_rate * .45):] == 0)
        assert renderer.playing_ids() == set()
    with pytest.raises(ValueError):
        renderer.test_tone("unknown")


def test_phone_layout_update_preserves_live_route(tmp_path):
    renderer = RecordingRenderer()
    service = SoundboardService(tmp_path, renderer_factory=lambda: renderer,
                                defaults_root=tmp_path / "no-defaults")
    service.configure({"inputId": "mic", "voiceOutputId": "voice"},
                      [{"id": "voice", "name": "Cable"}], [{"id": "mic", "name": "Microphone"}])
    service.configure({"layout": "b"}, [], [])
    assert service.snapshot()["config"]["voiceOutputId"] == "voice"
    assert service.snapshot()["config"]["layout"] == "b"
    assert service._renderer is renderer
    assert renderer.start_count == 1
    assert not renderer.stopped


def test_streams_allow_shared_format_conversion_and_monitor_index_zero(monkeypatch):
    import sys
    from types import SimpleNamespace

    opened = []
    class Stream:
        def __init__(self, **kwargs):
            opened.append(kwargs)
        def start(self): pass
        def stop(self): pass
        def close(self): pass
    fake_sd = SimpleNamespace(Stream=Stream, OutputStream=Stream,
                              WasapiSettings=lambda **kwargs: kwargs)
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    renderer = SoundboardRenderer()
    monkeypatch.setattr(renderer, "_find_device", lambda _sd, name, _kind:
                        {"mic": 2, "voice": 1, "ears": 0}[name])
    renderer.start("mic", "voice", "ears")
    assert len(opened) == 2
    assert opened[1]["device"] == 0
    assert all(s["extra_settings"] == {"auto_convert": True} for s in opened)
    renderer.close()


def test_renderer_device_match_handles_duplicate_host_api_names():
    class DuplicateNames(FakeSoundDevice):
        @staticmethod
        def query_devices():
            return [
                {"name": "CABLE Input", "max_output_channels": 2, "hostapi": 0},
                {"name": "CABLE Input", "max_output_channels": 2, "hostapi": 1},
            ]

    assert SoundboardRenderer._find_device(DuplicateNames, "CABLE Input", "output") == 1


def test_repeated_pad_restarts_on_both_buses(tmp_path):
    import numpy as np

    renderer = SoundboardRenderer()
    renderer._np = np
    renderer._voice_stream = object()
    renderer._ears_stream = object()
    renderer._load = lambda _path: np.ones(12, dtype=np.float32)
    renderer.trigger("a", tmp_path / "a.wav", .5, True, True)
    renderer.trigger("b", tmp_path / "b.wav", .5, True, False)
    renderer._mix("voice", 4)
    renderer.trigger("a", tmp_path / "a.wav", .5, True, True)
    assert [(v["id"], v["pos"]) for v in renderer._voices["voice"]] == [("b", 4), ("a", 0)]
    assert [(v["id"], v["pos"]) for v in renderer._voices["ears"]] == [("a", 0)]


def test_float_wav_and_phase_inverted_stereo_reach_voice(tmp_path):
    import numpy as np
    import soundfile as sf
    renderer = SoundboardRenderer()
    renderer._np = np
    renderer._voice_stream = object()
    t = np.arange(2400) / 48000
    left = (.4 * np.sin(2 * np.pi * 660 * t)).astype(np.float32)
    path = tmp_path / "phase.wav"
    sf.write(path, np.column_stack((left, -left)), 48000, subtype="FLOAT")
    renderer.trigger("phase", path, .8, True, False)
    out = np.zeros((2400, 2), dtype=np.float32)
    renderer._voice_callback(np.zeros((2400, 1)), out, 2400, None, None)
    assert np.max(np.abs(out)) > .3
    np.testing.assert_allclose(out[:, 0], out[:, 1])
    assert renderer.playing_ids() == set()


def test_stopped_stream_and_missing_monitor_are_reported(tmp_path):
    import numpy as np
    import pytest
    from types import SimpleNamespace
    renderer = SoundboardRenderer()
    renderer._np = np
    renderer._voice_stream = SimpleNamespace(active=False)
    with pytest.raises(RuntimeError, match="Audio device stopped"):
        renderer.trigger("a", tmp_path / "a.wav", 1, True, False)
    renderer._voice_stream.active = True
    with pytest.raises(RuntimeError, match="monitoring output"):
        renderer.trigger("a", tmp_path / "a.wav", 1, False, True)
    renderer._load = lambda _path: np.ones(8, dtype=np.float32)
    renderer.trigger("a", tmp_path / "a.wav", 1, True, True)
    assert len(renderer._voices["voice"]) == 1
    assert renderer._voices["ears"] == []  # monitoring is optional for Voice pads


def test_saved_route_recovers_stopped_stream_without_rewriting_library(tmp_path, monkeypatch):
    import agent.soundboard as module
    from test_routing import devices, recommend_route
    made = []
    class RecoveringRenderer(RecordingRenderer):
        error = ""
        closed = False
        def health_error(self): return self.error
        def close(self): self.closed = True
    def factory():
        renderer = RecoveringRenderer(); made.append(renderer); return renderer
    service = SoundboardService(tmp_path, renderer_factory=factory)
    snapshot = devices()
    route = recommend_route(snapshot)
    service.configure(route, snapshot["outputs"], snapshot["inputs"])
    saved = service._path.read_bytes()
    monkeypatch.setattr(service, "_save", lambda: (_ for _ in ()).throw(AssertionError("Retry wrote settings")))
    made[0].error = "Audio device stopped"
    service.ensure_started(snapshot["outputs"], snapshot["inputs"])
    assert len(made) == 2 and made[0].closed
    assert service.snapshot()["runtime"] == "ready"
    assert service._path.read_bytes() == saved
    service.ensure_started(snapshot["outputs"], snapshot["inputs"])
    assert len(made) == 2  # healthy streams are not repeatedly reopened
    service.close()
    service.ensure_started(snapshot["outputs"], snapshot["inputs"])
    assert len(made) == 2  # shutdown cannot restart audio


def test_transient_start_failure_backs_off_closes_partial_stream_and_recovers(tmp_path, monkeypatch):
    import agent.soundboard as module
    from test_routing import devices, recommend_route
    clock, made = [100.0], []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    class FlakyRenderer(RecordingRenderer):
        closed = False
        def start(self, *names):
            if len(made) <= 2: raise RuntimeError("Device temporarily unavailable")
            self.started = names
        def close(self): self.closed = True
    def factory():
        renderer = FlakyRenderer(); made.append(renderer); return renderer
    service = SoundboardService(tmp_path, renderer_factory=factory)
    snapshot = devices()
    service.configure(recommend_route(snapshot), snapshot["outputs"], snapshot["inputs"])
    assert made[0].closed and "retry automatically" in service.snapshot()["error"]
    for _ in range(10): service.ensure_started(snapshot["outputs"], snapshot["inputs"])
    assert len(made) == 1
    clock[0] += 1
    service.ensure_started(snapshot["outputs"], snapshot["inputs"])
    assert len(made) == 2 and made[1].closed
    clock[0] += 1
    service.ensure_started(snapshot["outputs"], snapshot["inputs"])
    assert len(made) == 2
    clock[0] += 1
    service.ensure_started(snapshot["outputs"], snapshot["inputs"])
    assert len(made) == 3 and service.snapshot()["runtime"] == "ready"


def test_device_disconnect_and_optional_monitor_return_preserve_saved_ids(tmp_path):
    from test_routing import devices, recommend_route
    made = []
    def factory():
        renderer = RecordingRenderer(); made.append(renderer); return renderer
    service = SoundboardService(tmp_path, renderer_factory=factory)
    snapshot = devices()
    route = recommend_route(snapshot)
    service.configure(route, snapshot["outputs"], snapshot["inputs"])
    service.ensure_started(snapshot["outputs"], [])
    assert service.snapshot()["runtime"] == "setup_required"
    service.ensure_started(snapshot["outputs"][1:], snapshot["inputs"])
    assert made[-1].started[-1] is None  # missing headphones do not silence Others
    assert service.snapshot()["config"]["earsOutputId"] == "speakers"
    service.ensure_started(snapshot["outputs"], snapshot["inputs"])
    assert made[-1].started[-1] == "Speakers (Realtek)"
    assert len(made) == 3


def test_cable_probe_requires_matching_audio_and_not_just_meter_activity():
    import numpy as np
    renderer = SoundboardRenderer(); renderer._np = np
    seconds = np.arange(28800, dtype=np.float32) / 48000
    probe = (.16 * np.sin(2 * np.pi * (440 * seconds + 900 * seconds ** 2))).astype(np.float32)
    captured = np.concatenate([np.zeros(4000), probe * .4, np.zeros(2000)])
    result = renderer._probe_match(captured, probe)
    assert result["correlation"] > .99 and abs(result["gain"] - .4) < .001
    assert renderer._probe_match(np.zeros(48000), probe)["correlation"] == 0
    assert renderer._probe_match(np.ones(48000) * .3, probe)["correlation"] < .1
    unrelated = np.sin(2 * np.pi * 660 * np.arange(48000) / 48000) * .3
    assert renderer._probe_match(unrelated, probe)["correlation"] < .2
    assert renderer._probe_match(np.full(48000, np.nan), probe)["correlation"] == 0


def test_cable_check_uses_live_mixer_and_cleans_up_probe(monkeypatch):
    import numpy as np
    import agent.soundboard as module
    from types import SimpleNamespace
    renderer = SoundboardRenderer(); renderer._np = np
    renderer._voice_stream = SimpleNamespace(active=True)
    opened = []
    class CaptureStream:
        def __init__(self, **kwargs): opened.append(kwargs); self.callback = kwargs["callback"]
        def __enter__(self): return self
        def __exit__(self, *args): pass
    renderer._sd = SimpleNamespace(InputStream=CaptureStream, WasapiSettings=lambda **kw: kw)
    monkeypatch.setattr(renderer, "_find_device", lambda *args: 7)
    def sleep(seconds):
        if seconds > 1:
            probe = renderer._voices["voice"][-1]["samples"]
            opened[0]["callback"](np.concatenate([np.zeros(2000), probe, np.zeros(1000)])[:, None], 31800, None, None)
    monkeypatch.setattr(module.time, "sleep", sleep)
    result = renderer.verify_receiver("CABLE Output")
    assert result["passed"] and result["scope"] == "virtual_microphone"
    assert result["receiver"] == "CABLE Output"
    assert opened[0]["device"] == 7 and opened[0]["channels"] == 1
    assert renderer.playing_ids() == set()
    assert renderer._voice_stream.active


def test_cable_check_rejects_route_changed_during_capture(tmp_path):
    import pytest
    from test_routing import devices, recommend_route
    renderer = RecordingRenderer()
    service = SoundboardService(tmp_path, renderer_factory=lambda: renderer)
    snapshot = devices()
    service.configure(recommend_route(snapshot), snapshot["outputs"], snapshot["inputs"])
    def verify(_name):
        service.close()
        return {"passed": True, "correlation": 1, "gain": 1}
    renderer.verify_receiver = verify
    with pytest.raises(ValueError, match="route changed"):
        service.verify_receiver(snapshot["outputs"], snapshot["inputs"])
    assert service._diagnostic_lock.acquire(blocking=False)
    service._diagnostic_lock.release()


def test_all_starter_sounds_produce_finite_voice_audio():
    import numpy as np
    pack = Path(__file__).resolve().parents[1] / "assets" / "default-sounds"
    renderer = SoundboardRenderer()
    renderer._np = np
    renderer._voice_stream = object()
    manifest = json.loads((pack / "manifest.json").read_text())
    for clip in manifest["clips"]:
        renderer.trigger(clip["key"], pack / clip["file"], clip["gain"], True, False)
        data = renderer._clips[clip["key"]]
        output = np.zeros((len(data), 2), dtype=np.float32)
        renderer._voice_callback(np.zeros((len(data), 1)), output, len(data), None, None)
        assert np.isfinite(output).all(), clip["key"]
        assert np.max(np.abs(output)) > .05, clip["key"]
        assert renderer.playing_ids() == set()
        trace = renderer.diagnostics()["playbackTrace"][-1]
        assert trace["clipId"] == clip["key"]
        assert trace["voiceRenderedFrames"] == len(data)
        assert trace["voiceEndReason"] == "completed"


def test_microphone_passthrough_never_waits_for_clip_update(tmp_path):
    import numpy as np
    import threading
    import time
    renderer = SoundboardRenderer(); renderer._np = np
    renderer._voice_stream = object()
    renderer._load = lambda _path: np.full(32, .1, dtype=np.float32)
    renderer.trigger("clip", tmp_path / "clip.wav", 1, True, False)
    held, release = threading.Event(), threading.Event()
    def hold_clip_update():
        with renderer._lock:
            held.set()
            assert release.wait(2)
    owner = threading.Thread(target=hold_clip_update)
    owner.start()
    try:
        assert held.wait(2)
        microphone = np.linspace(-.3, .3, 16, dtype=np.float32)[:, None]
        output = np.zeros((16, 2), dtype=np.float32)
        started = time.monotonic()
        renderer._voice_callback(microphone, output, 16, None, None)
        assert time.monotonic() - started < .15
        np.testing.assert_array_equal(output[:, 0], microphone[:, 0])
        np.testing.assert_array_equal(output[:, 1], microphone[:, 0])
        assert renderer._voices["voice"][0]["pos"] == 0
        assert renderer.stream_warnings()["voice"]["count"] == 1
    finally:
        release.set(); owner.join(2)
    renderer._voice_callback(np.zeros((16, 1)), output, 16, None, None)
    np.testing.assert_allclose(output, .1)
    assert renderer._voices["voice"][0]["pos"] == 16


def test_audio_callbacks_defer_logging_and_error_notifications(monkeypatch):
    import numpy as np
    import agent.soundboard as module
    errors = []
    renderer = SoundboardRenderer(on_error=errors.append); renderer._np = np
    monkeypatch.setattr(module.log, "warning", lambda *args: (_ for _ in ()).throw(
        AssertionError("Disk logging from real-time callback")))
    output = np.zeros((8, 2), dtype=np.float32)
    renderer._voice_callback(np.full((8, 1), .25), output, 8, None, "input overflow")
    np.testing.assert_allclose(output, .25)
    renderer._ears_callback(output, 8, None, "output underflow")
    assert renderer.stream_warnings() == {
        "voice": {"count": 1, "last": "input overflow"},
        "ears": {"count": 1, "last": "output underflow"}}
    renderer._mix_into = lambda *_args: (_ for _ in ()).throw(ValueError("bad samples"))
    renderer._voice_callback(np.zeros((8, 1)), output, 8, None, None)
    assert errors == [] and np.max(np.abs(output)) == 0
    assert "bad samples" in renderer._failure


def test_optional_monitor_failure_and_recovery_preserve_microphone(monkeypatch):
    import sys
    from types import SimpleNamespace
    import agent.soundboard as module
    opened, clock, fail_monitor = [], [100.0], [True]
    class Stream:
        def __init__(self, **kwargs):
            self.kwargs = kwargs; self.active = False; self.closed = False; self.stopped = False
            opened.append(self)
        def start(self):
            if self.kwargs["device"] == 3 and fail_monitor[0]:
                raise RuntimeError("Headphones temporarily unavailable")
            self.active = True
        def stop(self): self.active = False; self.stopped = True
        def close(self): self.closed = True
    sd = SimpleNamespace(Stream=Stream, OutputStream=Stream,
                         WasapiSettings=lambda **kwargs: kwargs)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    renderer = SoundboardRenderer()
    monkeypatch.setattr(renderer, "_find_device", lambda _sd, name, _kind:
                        {"mic": 1, "voice": 2, "ears": 3}[name])
    renderer.start("mic", "voice", "ears")
    voice = renderer._voice_stream
    assert voice.active and not voice.stopped
    assert renderer.health_error() == ""
    assert "Headphones temporarily unavailable" in renderer.monitor_error()
    assert opened[1].closed
    renderer.ensure_monitor("ears")
    assert len(opened) == 2  # failed monitoring retries with backoff
    clock[0] += 1; fail_monitor[0] = False
    renderer.ensure_monitor("ears")
    monitor = renderer._ears_stream
    assert monitor.active and renderer.monitor_error() == ""
    assert renderer._voice_stream is voice and not voice.stopped
    monitor.active = False
    renderer.ensure_monitor("ears")
    assert monitor.closed and renderer._ears_stream is not monitor
    assert renderer._voice_stream is voice and voice.active
    renderer.close()


def test_monitor_callback_failure_does_not_silence_voice():
    import numpy as np
    from types import SimpleNamespace
    renderer = SoundboardRenderer(); renderer._np = np
    renderer._voice_stream = SimpleNamespace(active=True)
    renderer._ears_stream = SimpleNamespace(active=True)
    renderer._voices["ears"] = [{"id": "invalid clip"}]
    output = np.zeros((8, 2), dtype=np.float32)
    renderer._ears_callback(output, 8, None, None)
    assert renderer.monitor_error() and renderer.health_error() == ""
    renderer._voice_callback(np.full((8, 1), .2), output, 8, None, None)
    np.testing.assert_allclose(output, .2)


def test_clip_mix_failure_keeps_speech_until_route_recovers():
    import numpy as np
    renderer = SoundboardRenderer(); renderer._np = np
    renderer._voices["voice"] = [{"id": "invalid clip"}]
    output = np.zeros((8, 2), dtype=np.float32)
    renderer._voice_callback(np.full((8, 1), .2), output, 8, None, None)
    np.testing.assert_allclose(output, .2)
    assert renderer.health_error().startswith("Voice audio failed")


def test_close_still_closes_stream_when_device_stop_fails():
    from types import SimpleNamespace
    closed = []
    def fail_stop(): raise RuntimeError("Disconnected device")
    renderer = SoundboardRenderer()
    renderer._voice_stream = SimpleNamespace(stop=fail_stop, close=lambda: closed.append("voice"))
    renderer._ears_stream = SimpleNamespace(stop=fail_stop, close=lambda: closed.append("ears"))
    renderer.close()
    assert closed == ["voice", "ears"]
    assert renderer._voice_stream is None and renderer._ears_stream is None


def test_ambiguous_wasapi_labels_do_not_choose_an_arbitrary_microphone():
    import pytest
    class DeviceNames(FakeSoundDevice):
        @staticmethod
        def query_devices():
            return [{"name": name, "max_input_channels": 1, "hostapi": 1}
                    for name in ["Microphone (Headset)", "Microphone (Webcam)"]]
    assert SoundboardRenderer._find_device(DeviceNames, "Microphone (Headset)", "input") == 0
    with pytest.raises(RuntimeError, match="ambiguous"):
        SoundboardRenderer._find_device(DeviceNames, "Microphone", "input")


def test_float_wav_duration_matches_playable_audio(tmp_path):
    import numpy as np
    import soundfile as sf
    source = tmp_path / "float.wav"
    sf.write(source, np.zeros(4800, dtype=np.float32), 48000, subtype="FLOAT")
    service = SoundboardService(tmp_path / "data", defaults_root=tmp_path / "no-defaults")
    service.import_clip(source)
    assert abs(service.snapshot()["clips"][0]["duration"] - .1) < .001


def test_deleted_or_replaced_clip_does_not_keep_old_cached_audio(tmp_path):
    import numpy as np
    import soundfile as sf
    renderer = SoundboardRenderer(); renderer._np = np
    renderer._voice_stream = object()
    renderer._ears_stream = object()
    path = tmp_path / "replace.wav"
    sf.write(path, np.full(16, .1), 48000, subtype="FLOAT")
    renderer.trigger("clip", path, 1, True, True)
    sf.write(path, np.full(32, .4), 48000, subtype="FLOAT")
    renderer.trigger("clip", path, 1, True, True)
    np.testing.assert_allclose(renderer._mix("voice", 16), .4)
    renderer.remove_clip("clip")
    assert renderer.playing_ids() == set() and "clip" not in renderer._clips


def test_stop_or_route_library_changes_cancel_pending_decode_without_waiting(tmp_path):
    import threading
    import time
    for action in ("stop", "configure", "remove", "close"):
        entered, release, errors = threading.Event(), threading.Event(), []
        class PreparingRenderer(RecordingRenderer):
            def prepare_clip(self, clip_id, path):
                entered.set()
                assert release.wait(2)
                return "prepared"
            def trigger_prepared(self, clip_id, prepared, path, gain, voice, ears):
                self.trigger(clip_id, path, gain, voice, ears)
        renderer = PreparingRenderer()
        service = SoundboardService(tmp_path / action, renderer_factory=lambda: renderer,
                                    defaults_root=tmp_path / "no-defaults")
        source = tmp_path / f"{action}.wav"; _wav(source)
        clip = service.import_clip(source)
        inputs = [{"id": "mic", "name": "Headset Mic"}]
        outputs = [{"id": "cable", "name": "CABLE Input"}]
        config = {"inputId": "mic", "voiceOutputId": "cable"}
        service.configure(config, outputs, inputs)
        def play():
            try: service.play(clip["id"])
            except Exception as exc: errors.append(exc)
        player = threading.Thread(target=play)
        player.start()
        try:
            assert entered.wait(2)
            started = time.monotonic()
            if action == "stop": service.stop_all()
            elif action == "configure": service.configure(config, outputs, inputs)
            elif action == "remove": service.remove_clip(clip["id"])
            else: service.close()
            assert time.monotonic() - started < .2
        finally:
            release.set(); player.join(2)
        assert not player.is_alive() and not errors
        assert renderer.triggers == [] and service._playing == set(), action


def test_file_changed_during_decode_is_rejected_before_any_audio(tmp_path):
    import numpy as np
    import pytest
    renderer = SoundboardRenderer(); renderer._np = np
    renderer._voice_stream = object()
    path = tmp_path / "changed.wav"; _wav(path)
    def decode(changed):
        changed.write_bytes(changed.read_bytes() + b"changed")
        return np.ones(16, dtype=np.float32)
    renderer._load = decode
    with pytest.raises(RuntimeError, match="file changed"):
        renderer.trigger("clip", path, 1, True, False)
    assert renderer.playing_ids() == set()


def test_phase_inverted_compressed_stereo_is_not_silent(tmp_path):
    import numpy as np
    import soundfile as sf
    renderer = SoundboardRenderer(); renderer._np = np
    renderer._voice_stream = object()
    wave = (.3 * np.sin(2 * np.pi * 660 * np.arange(4800) / 48000)).astype(np.float32)
    for extension in (".flac", ".ogg"):
        path = tmp_path / ("stereo" + extension)
        sf.write(path, np.column_stack([wave, -wave]), 48000)
        renderer.trigger(extension, path, 1, True, False)
        output = np.zeros((4800, 2), dtype=np.float32)
        renderer._voice_callback(np.zeros((4800, 1)), output, 4800, None, None)
        assert np.max(np.abs(output)) > .2


def test_decoded_cache_is_bounded_and_eviction_does_not_interrupt_active_clips(tmp_path):
    import numpy as np
    renderer = SoundboardRenderer(); renderer._np = np
    renderer._voice_stream = object()
    renderer.clip_cache_bytes = 128
    renderer.clip_cache_entries = 2
    loaded = []
    def load(path):
        loaded.append(path.stem)
        return np.full(16, .1, dtype=np.float32)  # 64 bytes per decoded clip
    renderer._load = load
    for clip_id in ("a", "b", "a", "c"):
        renderer.trigger(clip_id, tmp_path / f"{clip_id}.wav", 1, True, False)
    assert loaded == ["a", "b", "c"]
    assert list(renderer._clips) == ["a", "c"]  # reusing a makes b the LRU
    assert renderer._clip_cache_bytes == 128
    assert renderer.playing_ids() == {"a", "b", "c"}
    np.testing.assert_allclose(renderer._mix("voice", 8), .3)
    assert renderer.playing_ids() == {"a", "b", "c"}
    renderer.remove_clip("a")
    assert renderer._clip_cache_bytes == 64
    renderer._load = lambda _path: np.full(64, .2, dtype=np.float32)
    renderer.trigger("large", tmp_path / "large.wav", 1, True, False)
    assert "large" not in renderer._clips and renderer._clip_cache_bytes == 64
    assert "large" in renderer.playing_ids()  # imports larger than budget still play


def test_byte_budget_evicts_before_entry_limit(tmp_path):
    import numpy as np
    renderer = SoundboardRenderer(); renderer._np = np
    renderer.clip_cache_bytes = 96
    renderer.clip_cache_entries = 32
    renderer._load = lambda _path: np.ones(16, dtype=np.float32)
    renderer.prepare_clip("a", tmp_path / "a.wav")
    renderer.prepare_clip("b", tmp_path / "b.wav")
    assert list(renderer._clips) == ["b"] and renderer._clip_cache_bytes == 64


def test_service_monitor_changes_do_not_restart_voice_and_report_degraded_listening(tmp_path):
    from test_routing import devices, recommend_route
    class MonitorRenderer(RecordingRenderer):
        def __init__(self): super().__init__(); self.monitor_changes = []
        def ensure_monitor(self, name): self.monitor_changes.append(name)
        def health_error(self): return ""
        def monitor_error(self): return "Headphones unavailable"
    renderer = MonitorRenderer()
    service = SoundboardService(tmp_path, renderer_factory=lambda: renderer)
    snapshot = devices()
    service.configure(recommend_route(snapshot), snapshot["outputs"], snapshot["inputs"])
    state = service.snapshot()
    assert state["runtime"] == "ready" and state["error"] == "Headphones unavailable"
    service.ensure_started(snapshot["outputs"][1:], snapshot["inputs"])
    assert renderer.monitor_changes[-1] is None and renderer.start_count == 1
    service.ensure_started(snapshot["outputs"], snapshot["inputs"])
    assert renderer.monitor_changes[-1] == "Speakers (Realtek)" and renderer.start_count == 1
    outputs = snapshot["outputs"] + [{"id": "headset", "name": "USB Headphones"}]
    service.configure({"earsOutputId": "headset"}, outputs, snapshot["inputs"])
    assert renderer.monitor_changes[-1] == "USB Headphones" and renderer.start_count == 1


def test_meter_reads_do_not_wait_for_service_lifecycle_lock(tmp_path):
    import threading
    import time
    from types import SimpleNamespace
    service = SoundboardService(tmp_path, defaults_root=tmp_path / "no-defaults")
    service._renderer = SimpleNamespace(health_error=lambda: "", levels=lambda: {"mic": .4})
    entered, release = threading.Event(), threading.Event()
    def lifecycle():
        with service._lock:
            entered.set()
            assert release.wait(2)
    owner = threading.Thread(target=lifecycle); owner.start()
    try:
        assert entered.wait(2)
        started = time.monotonic()
        assert service.signal_state() == {"runtime": "ready", "levels": {"mic": .4}}
        assert time.monotonic() - started < .15
    finally:
        release.set(); owner.join(2)


def test_audition_uses_physical_output_without_changing_route(tmp_path):
    from types import SimpleNamespace
    calls = []
    service = SoundboardService(tmp_path)
    original = service.snapshot()["config"]
    service._preview_renderer = SimpleNamespace(preview=lambda *args: calls.append(args))
    service.audition(service.snapshot()["clips"][0]["id"], [
        {"id": "virtual", "name": "CABLE Input", "isDefault": True},
        {"id": "headphones", "name": "Headphones", "isDefault": True}])
    assert calls[0][2] == "Headphones"
    assert service.snapshot()["config"] == original


def test_soundboard_controller_config_and_stop(tmp_path):
    async def body():
        source = tmp_path / "clip.wav"; _wav(source)
        renderer = RecordingRenderer()
        service = SoundboardService(tmp_path / "data", renderer_factory=lambda: renderer,
                                    defaults_root=tmp_path / "no-defaults")
        clip = service.import_clip(source)
        async with engine_client(soundboard=service) as (client, _state, _controller):
            ws = await client.ws_connect("/ws"); await hello(ws)
            await ws.send_str(json.dumps({"t": "soundboard_config", "config": {
                "inputId": "in-headset", "voiceOutputId": "out-headset", "earsOutputId": ""}}))
            state = await recv_until(ws, "soundboard")
            assert state["soundboard"]["configured"] is True
            await ws.send_str(json.dumps({"t": "soundboard_play", "clipId": clip["id"]}))
            await recv_until(ws, "soundboard")
            assert renderer.triggers
            await ws.send_str(json.dumps({"t": "soundboard_stop_all"}))
            await recv_until(ws, "soundboard")
            assert renderer.stopped is True
            await ws.close()
    run(body())


def test_phone_tone_edits_survive_unassign_and_restart(tmp_path):
    async def body():
        source = tmp_path / "clip.wav"; _wav(source)
        root = tmp_path / "data"
        service = SoundboardService(root, renderer_factory=RecordingRenderer,
                                    defaults_root=tmp_path / "no-defaults")
        clip = service.import_clip(source, label="Old")
        presentation = PresentationService(root, service)
        async with engine_client(soundboard=service, presentation=presentation) as (client, state, _controller):
            ws = await client.ws_connect("/ws"); await hello(ws)
            await ws.send_json({"t": "soundboard_update_clip", "clipId": clip["id"],
                                "changes": {"label": "New bell", "emoji": "🔔", "gain": .37,
                                            "voice": False, "ears": True}})
            soundboard = (await recv_until(ws, "soundboard"))["soundboard"]
            saved = next(c for c in soundboard["clips"] if c["id"] == clip["id"])
            assert {key: saved[key] for key in ("label", "emoji", "gain", "voice", "ears")} == {
                "label": "New bell", "emoji": "🔔", "gain": .37, "voice": False, "ears": True}
            await ws.send_json({"t": "presentation_update", "baseRevision": 0,
                                "changes": {"padSlots": [None] * 12}})
            assert (await recv_until(ws, "presentation"))["presentation"]["padSlots"] == [None] * 12
            assert any(c["id"] == clip["id"] for c in state.soundboard["clips"])
            await ws.close()
        restarted = SoundboardService(root, renderer_factory=RecordingRenderer,
                                      defaults_root=tmp_path / "no-defaults")
        persisted = next(c for c in restarted.snapshot()["clips"] if c["id"] == clip["id"])
        assert {key: persisted[key] for key in ("label", "emoji", "gain", "voice", "ears")} == {
            "label": "New bell", "emoji": "🔔", "gain": .37, "voice": False, "ears": True}
        assert PresentationService(root, restarted).snapshot()["padSlots"] == [None] * 12

    run(body())


def test_cc0_starter_pack_installs_once_and_can_be_restored(tmp_path):
    pack = Path(__file__).resolve().parents[1] / "assets" / "default-sounds"
    service = SoundboardService(tmp_path / "data", defaults_root=pack)
    clips = service.snapshot()["clips"]
    assert len(clips) == 16
    assert {clip["id"] for clip in clips} >= {
        "default-crickets", "default-rimshot", "default-applause", "default-air-horn",
    }
    assert all((tmp_path / "data" / "soundboard" / clip["file"]).is_file() for clip in clips)
    assert all(clip["duration"] > 0 for clip in clips)

    # Removing a default is a lasting user choice; startup does not resurrect it.
    assert service.remove_clip("default-crickets")
    custom_source = tmp_path / "mine.wav"; _wav(custom_source)
    custom = service.import_clip(custom_source, label="Mine")
    restarted = SoundboardService(tmp_path / "data", defaults_root=pack)
    assert "default-crickets" not in {clip["id"] for clip in restarted.snapshot()["clips"]}

    # Explicit restore resets the starter pack while preserving imported clips.
    assert restarted.restore_defaults(reset=True) == 16
    restored = restarted.snapshot()["clips"]
    assert len(restored) == 17
    assert custom["id"] in {clip["id"] for clip in restored}


def test_cc0_starter_pack_hashes_are_verified(tmp_path):
    pack = Path(__file__).resolve().parents[1] / "assets" / "default-sounds"
    copied = tmp_path / "pack"
    import shutil
    shutil.copytree(pack, copied)
    with (copied / "crickets.wav").open("ab") as damaged:
        damaged.write(b"tampered")
    service = SoundboardService(tmp_path / "data", defaults_root=copied)
    assert service.snapshot()["clips"] == []
    try:
        service.restore_defaults()
    except ValueError as exc:
        assert "integrity check" in str(exc)
    else:
        raise AssertionError("tampered default sound was accepted")


def test_pack_upgrade_preserves_removed_defaults_and_custom_gain(tmp_path):
    pack = Path(__file__).resolve().parents[1] / "assets" / "default-sounds"
    service = SoundboardService(tmp_path / "data", defaults_root=pack)
    service.remove_clip("default-crickets")
    service.update_clip("default-pop", gain=.31)
    library_path = tmp_path / "data" / "soundboard" / "library.json"
    library = json.loads(library_path.read_text(encoding="utf-8"))
    library["defaultPackVersion"] = 1
    library_path.write_text(json.dumps(library), encoding="utf-8")

    upgraded = SoundboardService(tmp_path / "data", defaults_root=pack)
    clips = {clip["id"]: clip for clip in upgraded.snapshot()["clips"]}
    assert "default-crickets" not in clips
    assert clips["default-pop"]["gain"] == .31
    assert clips["default-applause"]["gain"] == .9

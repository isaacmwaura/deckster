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
    renderer._load = lambda _path: np.ones(12, dtype=np.float32)
    renderer.trigger("a", tmp_path / "a.wav", .5, True, True)
    renderer.trigger("b", tmp_path / "b.wav", .5, True, False)
    renderer._mix("voice", 4)
    renderer.trigger("a", tmp_path / "a.wav", .5, True, True)
    assert [(v["id"], v["pos"]) for v in renderer._voices["voice"]] == [("b", 4), ("a", 0)]
    assert [(v["id"], v["pos"]) for v in renderer._voices["ears"]] == [("a", 0)]


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


def test_cc0_starter_pack_installs_once_and_can_be_restored(tmp_path):
    pack = Path(__file__).resolve().parents[1] / "assets" / "default-sounds"
    service = SoundboardService(tmp_path / "data", defaults_root=pack)
    clips = service.snapshot()["clips"]
    assert len(clips) == 12
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
    assert restarted.restore_defaults(reset=True) == 12
    restored = restarted.snapshot()["clips"]
    assert len(restored) == 13
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

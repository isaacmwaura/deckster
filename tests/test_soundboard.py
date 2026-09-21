"""Configuration-B soundboard persistence and wire protocol.

The real WASAPI stream is optional and needs installed Windows endpoints, so the
service is tested with a recording renderer here.  This exercises the exact
device-id/configuration and polyphonic trigger contract used by the controller.
"""
from __future__ import annotations

import json
import struct
import wave

from agent.soundboard import SoundboardRenderer, SoundboardService
from helpers import engine_client, hello, recv_until, run


class RecordingRenderer:
    def __init__(self):
        self.started = None
        self.triggers = []
        self.stopped = False

    def start(self, input_name, voice_name, ears_name=None):
        self.started = (input_name, voice_name, ears_name)

    def trigger(self, clip_id, path, gain, voice, ears):
        self.triggers.append((clip_id, path.name, gain, voice, ears))

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
    service = SoundboardService(tmp_path / "data", renderer_factory=factory)
    clip = service.import_clip(source, label="Air horn", voice=True, ears=True, gain=.7)
    inputs = [{"id": "mic", "name": "Headset Mic"}]
    outputs = [{"id": "cable", "name": "CABLE Input"}, {"id": "ears", "name": "Headphones"}]
    service.configure({"inputId": "mic", "voiceOutputId": "cable", "earsOutputId": "ears"}, outputs, inputs)
    assert made[0].started == ("Headset Mic", "CABLE Input", "Headphones")
    service.play(clip["id"])
    assert made[0].triggers == [(clip["id"], clip["file"], .7, True, True)]
    assert service.snapshot()["clips"][0]["playing"] is True
    service.stop_all()
    assert made[0].stopped is True and not service.snapshot()["clips"][0]["playing"]

    # The on-disk library survives a service restart and retains per-pad settings.
    service.update_clip(clip["id"], label="Horn", gain=2, voice=False)
    restored = SoundboardService(tmp_path / "data", renderer_factory=factory)
    saved = restored.snapshot()["clips"][0]
    assert saved["label"] == "Horn" and saved["gain"] == 1 and saved["voice"] is False
    restored.ensure_started(outputs, inputs)
    assert made[1].started == ("Headset Mic", "CABLE Input", "Headphones")


def test_renderer_device_match_is_wasapi_only():
    assert SoundboardRenderer._find_device(FakeSoundDevice, "CABLE Input", "output") == "CABLE Input (WASAPI)"


def test_soundboard_controller_config_and_stop(tmp_path):
    async def body():
        source = tmp_path / "clip.wav"; _wav(source)
        renderer = RecordingRenderer()
        service = SoundboardService(tmp_path / "data", renderer_factory=lambda: renderer)
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

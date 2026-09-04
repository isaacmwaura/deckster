"""P1: audio engine, controller wiring, and a safe real-hardware smoke test.

The real-backend test deliberately performs only *no-op* writes (setting values to
their current level) so it never changes the user's actual volume or blasts audio,
while still exercising the COM write path end to end.
"""
import json
import time

import pytest

from agent.audio.engine import AudioEngine
from agent.audio.mock import MockAudioBackend

from helpers import engine_client, hello, recv_until, run


# ---- engine + mock backend -----------------------------------------------
def test_engine_setget_and_poll():
    polls = []
    engine = AudioEngine(MockAudioBackend, poll_interval_s=0.05)
    engine.set_on_poll(lambda d: polls.append(d))
    engine.start()
    try:
        engine.submit(lambda b: b.set_session_volume("discord", 0.3)).result(2)
        sessions = engine.submit(lambda b: b.snapshot_sessions()).result(2)
        discord = next(s for s in sessions if s.id == "discord")
        assert discord.level == pytest.approx(0.3)

        engine.submit(lambda b: b.set_master_mute("mic", True)).result(2)
        mic = engine.submit(lambda b: b.get_master("mic")).result(2)
        assert mic.muted is True

        time.sleep(0.15)  # let at least one poll fire
        assert polls, "poll callback never fired"
        assert "sessions" in polls[-1] and "speaker" in polls[-1]
    finally:
        engine.stop()


def test_mock_peaks_range_and_mute():
    b = MockAudioBackend()
    peaks = b.get_peaks()
    assert set(peaks) == {"output", "input"}
    assert 0.0 <= peaks["output"] <= 1.0 and 0.0 <= peaks["input"] <= 1.0
    # a muted endpoint reads flat 0, like the real one
    b.set_master_mute("speaker", True)
    b.set_master_mute("mic", True)
    silent = b.get_peaks()
    assert silent["output"] == 0.0 and silent["input"] == 0.0


def test_engine_job_exception_propagates():
    engine = AudioEngine(MockAudioBackend, poll_interval_s=10)
    engine.start()
    try:
        def boom(_b):
            raise ValueError("nope")
        with pytest.raises(ValueError, match="nope"):
            engine.submit(boom).result(2)
    finally:
        engine.stop()


# ---- controller protocol (mock backend) ----------------------------------
# Run under asyncio.run (see tests/helpers.py) to avoid a pytest-asyncio + aiohttp
# + engine-thread harness deadlock. Product code is unchanged.
def test_set_session_volume_reflects():
    async def body():
        async with engine_client() as (client, _state, _ctl):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_str(json.dumps({
                "t": "set_volume",
                "target": {"kind": "session", "id": "discord"},
                "level": 0.25,
            }))
            msg = await recv_until(ws, "state")
            assert msg["target"] == {"kind": "session", "id": "discord"}
            assert msg["level"] == pytest.approx(0.25)
            await ws.close()
    run(body())


def test_set_mic_mute_reflects():
    async def body():
        async with engine_client() as (client, _state, _ctl):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_str(json.dumps({
                "t": "set_mute", "target": {"kind": "mic"}, "muted": True,
            }))
            msg = await recv_until(ws, "state")
            assert msg["target"] == {"kind": "mic"}
            assert msg["muted"] is True
            await ws.close()
    run(body())


def test_default_output_switch():
    async def body():
        async with engine_client() as (client, _state, _ctl):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_str(json.dumps({"t": "set_default_output", "deviceId": "out-headset"}))
            headset = None
            for _ in range(10):
                snap = await recv_until(ws, "snapshot")
                headset = next(d for d in snap["devices"]["outputs"] if d["id"] == "out-headset")
                if headset["isDefault"]:
                    break
            assert headset and headset["isDefault"] is True
            await ws.close()
    run(body())


def test_default_input_switch():
    async def body():
        async with engine_client() as (client, _state, _ctl):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_str(json.dumps({"t": "set_default_input", "deviceId": "in-webcam"}))
            webcam = None
            for _ in range(10):
                snap = await recv_until(ws, "snapshot")
                webcam = next(d for d in snap["devices"]["inputs"] if d["id"] == "in-webcam")
                if webcam["isDefault"]:
                    break
            assert webcam and webcam["isDefault"] is True
            await ws.close()
    run(body())


# ---- real pycaw backend (safe, read + no-op write) -----------------------
def test_real_backend_readonly_and_noop_write():
    try:
        import comtypes
        from agent.audio.pycaw_backend import PycawAudioBackend
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"pycaw unavailable: {exc}")

    comtypes.CoInitialize()
    try:
        backend = PycawAudioBackend()

        spk = backend.get_master("speaker")
        assert 0.0 <= spk.level <= 1.0
        mic = backend.get_master("mic")
        assert 0.0 <= mic.level <= 1.0

        devices = backend.list_devices()
        assert isinstance(devices.outputs, list)

        sessions = backend.snapshot_sessions()
        assert isinstance(sessions, list)

        # No-op write: set speaker to its CURRENT level (no audible change) and
        # confirm it round-trips. Exercises the COM write path safely.
        backend.set_master_volume("speaker", spk.level)
        assert backend.get_master("speaker").level == pytest.approx(spk.level, abs=0.02)

        # Live metering: real IAudioMeterInformation peaks, in range, never raising.
        peaks = backend.get_peaks()
        assert set(peaks) == {"output", "input"}
        assert 0.0 <= peaks["output"] <= 1.0
        assert 0.0 <= peaks["input"] <= 1.0
    finally:
        comtypes.CoUninitialize()

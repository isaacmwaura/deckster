"""Regression coverage for slow screens and slow audio device/library work."""
import asyncio
import threading

import pytest

from agent.state import AppState
from helpers import engine_client, hello, recv_until, run


def test_snapshot_is_frozen_at_publish_time():
    state = AppState()
    state.replace_sessions([{"id": "chrome", "level": .2, "muted": False, "active": True}])
    previous = state.snapshot()
    state.apply_session("chrome", level=.9)
    state.apply_master("mic", level=.4)
    assert previous["sessions"][0]["level"] == .2
    assert previous["devices"]["micMaster"]["level"] == 1
    previous["devices"]["micMaster"]["level"] = .01
    assert state.devices["micMaster"]["level"] == .4


def test_slow_subscriber_gets_latest_complete_state_without_backlog():
    state = AppState()
    state.replace_sessions([{"id": "chrome", "level": 0, "muted": False, "active": True}])
    slow = state.subscribe()
    fast = state.subscribe()
    for i in range(100):
        state.apply_session("chrome", level=i / 100)
        assert fast.get_nowait()["t"] == "state"
    state.set_media([{"id": "Chrome", "title": "Latest video"}])
    fast.get_nowait()
    state.apply_master("mic", muted=True)
    assert slow.qsize() == 1
    latest = slow.get_nowait()
    assert latest["t"] == "snapshot"
    assert latest["sessions"][0]["level"] == .99
    assert latest["devices"]["micMaster"]["muted"]
    assert latest["media"][0]["title"] == "Latest video"


def test_identical_service_updates_do_not_broadcast_or_share_callers_data():
    state = AppState()
    changes = [
        (state.set_media, [{"id": "Chrome", "title": "Video"}]),
        (state.set_soundboard, {"clips": [{"id": "bell"}], "runtime": "ready"}),
        (state.set_presentation, {"revision": 1, "padSlots": ["bell"]}),
    ]
    queue = state.subscribe()
    for publish, data in changes:
        publish(data)
        queue.get_nowait()
        publish(data)
        assert queue.empty()
    changes[0][1][0]["title"] = "Changed by caller"
    changes[1][1]["clips"][0]["id"] = "Changed by caller"
    changes[2][1]["padSlots"].clear()
    assert state.media[0]["title"] == "Video"
    assert state.soundboard["clips"][0]["id"] == "bell"
    assert state.presentation["padSlots"] == ["bell"]


class SlowBoard:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.starts = 0

    def snapshot(self, *args):
        return {"clips": [], "config": {}, "runtime": "ready"}

    def ensure_started(self, *args):
        self.starts += 1
        self.entered.set()
        assert self.release.wait(3)

    def play(self, *args):
        self.entered.set()
        assert self.release.wait(3)


def test_slow_audio_recovery_is_coalesced_and_does_not_block_controls():
    async def body():
        board = SlowBoard()
        async with engine_client(soundboard=board) as (client, state, controller):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            def poll():
                controller._ingest_poll(list(state.sessions.values()),
                                        state.devices["speakerMaster"], state.devices["micMaster"],
                                        state.devices["outputs"], state.devices["inputs"], None)
            poll()
            assert await asyncio.to_thread(board.entered.wait, 1)
            try:
                for _ in range(30):
                    poll()
                await ws.send_json({"t": "set_volume", "target": {"kind": "mic"}, "level": .6})
                await asyncio.wait_for(recv_until(ws, "state"), .5)
                await ws.send_json({"t": "ping"})
                await asyncio.wait_for(recv_until(ws, "pong"), .5)
                assert state.devices["micMaster"]["level"] == .6
                assert board.starts == 1
                assert (await asyncio.wait_for(client.get("/health"), .5)).status == 200
            finally:
                board.release.set()
            await ws.close()
    run(body())


def test_slow_sound_decode_does_not_block_other_phone_or_health():
    async def body():
        board = SlowBoard()
        async with engine_client(soundboard=board) as (client, _state, _controller):
            first = await client.ws_connect("/ws")
            second = await client.ws_connect("/ws")
            await hello(first)
            await hello(second)
            await first.send_json({"t": "soundboard_play", "clipId": "large-clip"})
            assert await asyncio.to_thread(board.entered.wait, 1)
            try:
                await second.send_json({"t": "ping"})
                await asyncio.wait_for(recv_until(second, "pong"), .5)
                assert (await asyncio.wait_for(client.get("/health"), .5)).status == 200
            finally:
                board.release.set()
            await first.close()
            await second.close()
    run(body())


def test_cancelled_audio_worker_keeps_serialization_until_it_finishes():
    async def body():
        async with engine_client() as (_client, _state, controller):
            entered = threading.Event()
            release = threading.Event()
            order = []
            def first():
                entered.set()
                assert release.wait(3)
                order.append("first")
            pending = asyncio.create_task(controller._soundboard_call(first))
            assert await asyncio.to_thread(entered.wait, 1)
            pending.cancel()
            following = asyncio.create_task(controller._soundboard_call(lambda: order.append("second")))
            await asyncio.sleep(.02)
            pending.cancel()
            await asyncio.sleep(.02)
            assert not following.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            await following
            assert order == ["first", "second"]
    run(body())


def test_non_object_command_does_not_disconnect_phone():
    async def body():
        async with engine_client() as (client, _state, _controller):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            for data in ([], None, "set_volume", 42):
                await ws.send_json(data)
                assert (await recv_until(ws, "error"))["code"] == "badjson"
            await ws.send_json({"t": "ping"})
            await recv_until(ws, "pong")
            await ws.close()
    run(body())


def test_priority_stop_waits_for_native_cleanup_without_waiting_for_clip_worker():
    class PriorityBoard(SlowBoard):
        def __init__(self):
            super().__init__()
            self.stop_entered = threading.Event()
            self.stop_release = threading.Event()
            self.stop_finished = threading.Event()

        def stop_all(self):
            self.stop_entered.set()
            assert self.stop_release.wait(3)
            self.stop_finished.set()

    async def body():
        board = PriorityBoard()
        async with engine_client(soundboard=board) as (_client, _state, controller):
            preparing = asyncio.create_task(controller._soundboard_call(board.play, "large"))
            stopping = None
            try:
                assert await asyncio.to_thread(board.entered.wait, 1)
                stopping = asyncio.create_task(controller._soundboard_stop_all(None))
                assert await asyncio.to_thread(board.stop_entered.wait, .5), "Stop must bypass the decode/recovery serialization lock"
                stopping.cancel()
                await asyncio.sleep(.02)
                stopping.cancel()
                await asyncio.sleep(.02)
                assert not stopping.done(), "Disconnect cleanup must await an already running native Stop"
                assert not board.stop_finished.is_set()
                board.stop_release.set()
                with pytest.raises(asyncio.CancelledError):
                    await stopping
                assert board.stop_finished.is_set()
                assert not preparing.done(), "Stop cleanup must not wait for unrelated clip preparation"
            finally:
                board.stop_release.set()
                board.release.set()
                if stopping is not None:
                    await asyncio.gather(stopping, return_exceptions=True)
                await preparing
    run(body())


def test_stop_discards_active_play_waiting_for_recovery_but_allows_new_play():
    class RecoveryBoard(SlowBoard):
        def __init__(self):
            super().__init__()
            self.stopped = threading.Event()
            self.new_played = threading.Event()
            self.order = []
            self.play_generation = 0

        def stop_all(self):
            self.play_generation += 1
            self.order.append("stop")
            self.stopped.set()

        def play(self, clip, *, expected_generation=None):
            if expected_generation is not None and expected_generation != self.play_generation:
                return
            self.order.append("play:" + clip)
            if clip == "new":
                self.new_played.set()

    async def body():
        board = RecoveryBoard()
        async with engine_client(soundboard=board) as (client, state, controller):
            old_entered = asyncio.Event()
            original_play = controller._soundboard_play

            async def observe_play(source, message):
                if message.get("clipId") == "old":
                    old_entered.set()
                await original_play(source, message)

            controller._soundboard_play = observe_play
            ws = await client.ws_connect("/ws")
            await hello(ws)
            controller._ingest_poll(list(state.sessions.values()),
                                    state.devices["speakerMaster"], state.devices["micMaster"],
                                    state.devices["outputs"], state.devices["inputs"], None)
            assert await asyncio.to_thread(board.entered.wait, 1)
            try:
                await ws.send_json({"t": "soundboard_play", "clipId": "old"})
                await asyncio.wait_for(old_entered.wait(), .5)
                await ws.send_json({"t": "soundboard_stop_all"})
                assert await asyncio.to_thread(board.stopped.wait, .5)
                board.release.set()
                await ws.send_json({"t": "soundboard_play", "clipId": "new"})
                assert await asyncio.to_thread(board.new_played.wait, 1)
                assert board.order == ["stop", "play:new"], "A pre-Stop play waiting for recovery must never start after Stop"
            finally:
                board.release.set()
                await ws.close()
    run(body())


def test_soundboard_service_rejects_play_reserved_before_stop_without_decoding(tmp_path):
    import wave
    from agent.soundboard import SoundboardService

    prepared, played = [], []

    class Renderer:
        def prepare_clip(self, clip_id, _path):
            prepared.append(clip_id)
            return object()

        def trigger_prepared(self, clip_id, *_args):
            played.append(clip_id)

        def stop_all(self):
            pass

    source = tmp_path / "source.wav"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48_000)
        output.writeframes(b"\0\0" * 48)
    service = SoundboardService(tmp_path, defaults_root=tmp_path / "no-default-pack")
    clip = service.import_clip(source)
    service._renderer = Renderer()
    old_generation = service.play_generation
    service.stop_all()
    service.play(clip["id"], expected_generation=old_generation)
    assert prepared == [] and played == [], "An old reserved play must be rejected before disk/codec work"
    service.play(clip["id"], expected_generation=service.play_generation)
    assert prepared == [clip["id"]] and played == [clip["id"]], "A new post-Stop play must still start"

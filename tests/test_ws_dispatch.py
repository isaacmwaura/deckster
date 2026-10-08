"""Per-phone command ordering, bounded backlog, and independent audio work."""
import asyncio
from contextlib import asynccontextmanager
import threading

from aiohttp.test_utils import TestClient, TestServer

from agent.server import _CommandLane, create_app
from agent.state import AppState
from helpers import engine_client, hello, recv_until, run


@asynccontextmanager
async def command_client(controller, state=None):
    state = state or AppState()
    client = TestClient(TestServer(create_app(state, controller=controller)))
    await client.start_server()
    try:
        yield client, state
    finally:
        await client.close()


def volume(kind, level, session_id=None):
    target = {"kind": kind}
    if session_id is not None:
        target["id"] = session_id
    return {"t": "set_volume", "target": target, "level": level}


def test_slow_sound_play_keeps_same_phone_volume_and_ping_responsive():
    class SlowBoard:
        def __init__(self):
            self.entered, self.release = threading.Event(), threading.Event()

        def snapshot(self, *args):
            return {"clips": [], "config": {}, "runtime": "ready"}

        def ensure_started(self, *args):
            pass

        def play(self, *args):
            self.entered.set()
            assert self.release.wait(3)

    async def body():
        board = SlowBoard()
        async with engine_client(soundboard=board) as (client, state, _controller):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_json({"t": "soundboard_play", "clipId": "slow"})
            assert await asyncio.to_thread(board.entered.wait, 1)
            try:
                await ws.send_json(volume("mic", .67))
                observation = await asyncio.wait_for(recv_until(ws, "state"), .5)
                assert observation["level"] == .67
                await ws.send_json({"t": "ping"})
                await asyncio.wait_for(recv_until(ws, "pong"), .5)
                assert state.devices["micMaster"]["level"] == .67
            finally:
                board.release.set()
            await ws.close()
    run(body())


def test_stop_waiting_for_recovery_lock_keeps_same_phone_controls_responsive():
    class LockedBoard:
        def __init__(self):
            self.lock = threading.Lock()
            self.recovery_entered = threading.Event()
            self.stop_entered = threading.Event()
            self.release = threading.Event()

        def snapshot(self, *args):
            with self.lock:
                return {"clips": [], "config": {}, "runtime": "ready"}

        def ensure_started(self, *args):
            with self.lock:
                self.recovery_entered.set()
                assert self.release.wait(3)

        def stop_all(self):
            self.stop_entered.set()
            with self.lock:
                pass

    async def body():
        board = LockedBoard()
        async with engine_client(soundboard=board) as (client, state, controller):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            controller._ingest_poll(list(state.sessions.values()),
                                    state.devices["speakerMaster"], state.devices["micMaster"],
                                    state.devices["outputs"], state.devices["inputs"], None)
            assert await asyncio.to_thread(board.recovery_entered.wait, 1)
            try:
                await ws.send_json({"t": "soundboard_stop_all"})
                assert await asyncio.to_thread(board.stop_entered.wait, 1)
                await ws.send_json(volume("mic", .73))
                observation = await asyncio.wait_for(recv_until(ws, "state"), .5)
                assert observation["level"] == .73
                await ws.send_json({"t": "ping"})
                await asyncio.wait_for(recv_until(ws, "pong"), .5)
                assert state.devices["micMaster"]["level"] == .73
            finally:
                board.release.set()
            await controller.close()
            await ws.close()
    run(body())


def test_queued_volumes_coalesce_at_latest_arrival_position_without_reordering_others():
    async def body():
        lane = _CommandLane(coalesce_volumes=True)
        entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        seen = []

        async def controller(_client, message):
            if message["t"] == "block":
                entered.set()
                await release.wait()
            seen.append(message)
            if message["t"] == "finish":
                completed.set()

        worker = asyncio.create_task(lane.run(controller, None))
        assert lane.enqueue({"t": "block"})
        await entered.wait()
        stale = volume("session", .1, "chrome")
        mute = {"t": "set_mute", "target": {"kind": "session", "id": "chrome"}, "muted": True}
        media = {"t": "media_control", "action": "pause", "id": "Chrome"}
        latest = volume("session", .9, "chrome")
        mic = volume("mic", .4)
        for message in (stale, mute, media, {"t": "ping"}, latest, mic, {"t": "finish"}):
            assert lane.enqueue(message)
        release.set()
        await asyncio.wait_for(completed.wait(), .5)
        assert seen == [{"t": "block"}, mute, media, {"t": "ping"}, latest, mic, {"t": "finish"}]
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    run(body())


def test_command_backlog_is_bounded_and_full_queue_accepts_replacement_volume():
    lane = _CommandLane(coalesce_volumes=True, limit=3)
    first, latest = volume("speaker", .2), volume("speaker", .8)
    assert lane.enqueue(first)
    assert lane.enqueue({"t": "ping"})
    assert lane.enqueue({"t": "media_control", "action": "next"})
    assert not lane.enqueue(volume("mic", .5))
    assert lane.enqueue(latest)
    assert [item.message for item in lane._pending] == [
        {"t": "ping"}, {"t": "media_control", "action": "next"}, latest]


def test_busy_command_error_preserves_reader_subscribe_and_viewport():
    async def body():
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def controller(client, message):
            if message.get("id") == "block":
                entered.set()
                await release.wait()
            seen.append(message)
            if message["t"] == "ping":
                await client.send({"t": "pong"})

        async with command_client(controller) as (client, state):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_json({"t": "macro", "id": "block"})
            await entered.wait()
            for i in range(33):
                await ws.send_json({"t": "macro", "id": str(i)})
            error = await asyncio.wait_for(recv_until(ws, "error"), .5)
            assert error["code"] == "busy" and error["command"] == "macro"
            await ws.send_json({"t": "viewport", "width": 412, "height": 915})
            await ws.send_json({"t": "subscribe"})
            await asyncio.wait_for(recv_until(ws, "snapshot"), .5)
            assert list(state.client_viewports.values())[0]["width"] == 412
            release.set()
            await ws.send_json({"t": "ping"})
            await asyncio.wait_for(recv_until(ws, "pong"), .5)
            assert [m["id"] for m in seen if m["t"] == "macro"] == ["block"] + [str(i) for i in range(32)]
            await ws.close()
    run(body())


def test_stop_discards_old_plays_preserves_edits_and_gates_new_play():
    async def body():
        entered, release, stop_entered, stop_release = [asyncio.Event() for _ in range(4)]
        seen = []

        async def controller(client, message):
            command = message["t"]
            if command == "soundboard_config":
                entered.set()
                await release.wait()
            elif command == "soundboard_stop_all":
                stop_entered.set()
                await stop_release.wait()
            seen.append(message)
            if command == "ping":
                await client.send({"t": "pong"})
            if command == "soundboard_play" and message["clipId"] == "new":
                await client.send({"t": "played"})

        async with command_client(controller) as (client, _state):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_json({"t": "soundboard_config"})
            await entered.wait()
            await ws.send_json({"t": "soundboard_play", "clipId": "old"})
            await ws.send_json({"t": "soundboard_update_clip", "clipId": "edit"})
            await ws.send_json({"t": "soundboard_stop_all"})
            await ws.send_json({"t": "soundboard_play", "clipId": "new"})
            await stop_entered.wait()
            release.set()
            # The retained edit predates Stop-All; the new play waits for its
            # completion instead of being cancelled by it after starting.
            await asyncio.sleep(.02)
            assert not any(m["t"] == "soundboard_play" for m in seen)
            stop_release.set()
            await asyncio.wait_for(recv_until(ws, "played"), .5)
            assert not any(m.get("clipId") == "old" for m in seen)
            assert any(m.get("clipId") == "edit" for m in seen)
            assert [m["t"] for m in seen][-2:] == ["soundboard_stop_all", "soundboard_play"]
            await ws.close()
    run(body())


def test_discard_plays_during_wakeup_or_stop_barrier_does_not_kill_lane():
    async def body():
        lane = _CommandLane()
        completed, barrier = asyncio.Event(), asyncio.Event()
        seen = []

        async def controller(_client, message):
            seen.append(message)
            completed.set()

        worker = asyncio.create_task(lane.run(controller, None))
        await asyncio.sleep(0)
        lane.enqueue({"t": "soundboard_play", "clipId": "woken"})
        lane.discard_plays()
        await asyncio.sleep(0)
        assert not worker.done()
        lane.enqueue({"t": "soundboard_play", "clipId": "waiting"}, wait_for=barrier)
        await asyncio.sleep(0)
        lane.discard_plays()
        lane.enqueue({"t": "soundboard_update_clip", "clipId": "retained"})
        barrier.set()
        await asyncio.wait_for(completed.wait(), .5)
        assert seen == [{"t": "soundboard_update_clip", "clipId": "retained"}]
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    run(body())


def test_socket_disconnect_awaits_and_cleans_up_every_worker():
    async def body():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def controller(_client, message):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async with command_client(controller) as (client, state):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_json({"t": "soundboard_play", "clipId": "slow"})
            await entered.wait()
            await ws.close()
            await asyncio.wait_for(cancelled.wait(), .5)
        assert not state._subscribers and not state.client_viewports
        assert not [task for task in asyncio.all_tasks()
                    if not task.done() and task.get_name().startswith("ws-")]
    run(body())

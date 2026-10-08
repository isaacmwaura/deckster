"""P8: now-playing media (SMTC) protocol + thumbnail serving.

The real SMTC read/transport path is Windows-only and needs live playback, so it's
exercised by a live check; here we test the controller wiring and the thumbnail
route with a fake media service (no winsdk, no real playback).
"""
import asyncio
import json
import sys
from types import SimpleNamespace

import agent.media as media_module
from agent.media import THUMBS, MediaService, MediaThumbs, _app_label

from helpers import engine_client, hello, recv_until, run


class FakeMedia:
    def __init__(self):
        self.calls = []
        self.ok = True

    async def control(self, action, app_id=""):
        self.calls.append((action, app_id))
        return self.ok


# ---- app-label cleanup ----------------------------------------------------
def test_app_label_cleanup():
    assert _app_label("Spotify.exe") == "Spotify"
    assert _app_label("Chrome") == "Chrome"
    assert _app_label("308046B0AF4A39CB") == "Firefox"
    # packaged AUMID "Family!App" -> take the segment after '!'
    assert _app_label("Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic") == "Microsoft.ZuneMusic"


# ---- controller wiring ----------------------------------------------------
def test_media_control_routes_to_service():
    async def body():
        fake = FakeMedia()
        async with engine_client(media=fake) as (client, _s, _c):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_str(json.dumps({"t": "media_control", "action": "play_pause", "id": "Spotify"}))
            # no error frame should come back; give the loop a beat by pinging
            await ws.send_str(json.dumps({"t": "ping"}))
            await recv_until(ws, "pong")
            assert fake.calls == [("play_pause", "Spotify")]
            await ws.close()
    run(body())


def test_media_control_without_service_errors():
    async def body():
        async with engine_client() as (client, _s, _c):  # no media service
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_str(json.dumps({"t": "media_control", "action": "next"}))
            err = await recv_until(ws, "error")
            assert err["code"] == "nomedia"
            await ws.close()
    run(body())


def test_media_control_failure_reports():
    async def body():
        fake = FakeMedia(); fake.ok = False
        async with engine_client(media=fake) as (client, _s, _c):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_str(json.dumps({"t": "media_control", "action": "next", "id": "x"}))
            err = await recv_until(ws, "error")
            assert err["code"] == "mediafail"
            await ws.close()
    run(body())


# ---- thumbnail store + route ----------------------------------------------
def test_media_thumb_route_serves_and_404():
    async def body():
        key = MediaThumbs.key_for("Spotify", "Some Song")
        THUMBS.put(key, b"JPEGDATA")
        async with engine_client() as (client, _s, _c):
            r = await client.get("/media_thumb/" + key)
            assert r.status == 200
            assert await r.read() == b"JPEGDATA"
            r2 = await client.get("/media_thumb/deadbeef")
            assert r2.status == 404
    run(body())


def test_media_thumb_route_preserves_real_image_type():
    async def body():
        for data, mime in [(b"\x89PNG\r\n\x1a\nPNGDATA", "image/png"),
                           (b"RIFF\x00\x00\x00\x00WEBPDATA", "image/webp"),
                           (b"\xff\xd8\xffJPEGDATA", "image/jpeg")]:
            key = MediaThumbs.key_for_data(data)
            THUMBS.put(key, data)
            async with engine_client() as (client, _s, _c):
                response = await client.get("/media_thumb/" + key)
                assert response.content_type == mime
                assert await response.read() == data
                assert "max-age=3600" in response.headers["Cache-Control"]
    run(body())


def test_media_thumb_cache_bounds_bytes_and_retains_recent_art():
    cache = MediaThumbs()
    data = b"x" * (4 * 1024 * 1024)
    for i in range(4):
        cache.put(str(i), data)
    assert cache.get("0") == data
    cache.put("4", data)
    assert cache.has("0") and not cache.has("1")
    assert cache._bytes == 16 * 1024 * 1024
    cache.put("oversized", data + b"x")
    assert not cache.has("oversized")
    cache.put("0", b"small")
    assert cache._bytes == 12 * 1024 * 1024 + 5


class MediaSession:
    def __init__(self, app_id="Chrome", title="A video", status=4, art=b"first"):
        self.source_app_user_model_id = app_id
        self.props = SimpleNamespace(title=title, artist="Channel", album_title="",
                                     album_artist="", track_number=0, art=art)
        self.info = SimpleNamespace(playback_status=status, controls=SimpleNamespace(
            is_play_enabled=True, is_pause_enabled=True,
            is_next_enabled=True, is_previous_enabled=False))
        self.fail_props = False
        self.control_ok = True
        self.commands = []

    async def try_get_media_properties_async(self):
        if self.fail_props:
            raise RuntimeError("temporarily unavailable")
        return self.props

    def get_playback_info(self):
        return self.info

    async def try_toggle_play_pause_async(self):
        self.commands.append("play_pause")
        return self.control_ok


class MediaManager:
    def __init__(self, sessions, current=None):
        self.sessions, self.current = sessions, current

    def get_sessions(self):
        return self.sessions

    def get_current_session(self):
        return self.current


def media_service(loop):
    state = SimpleNamespace(updates=[])
    state.set_media = state.updates.append
    service = MediaService(state, loop)

    async def read_art(props):
        return props.art

    service._read_thumb = read_art
    return service, state


def test_art_refreshes_same_title_and_retries_delayed_art(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(media_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(media_module, "THUMBS", MediaThumbs())

    async def body():
        service, _ = media_service(asyncio.get_running_loop())
        session = MediaSession(art=None)
        first = await service._session_dict(session)
        assert first["thumbKey"] is None
        session.props.art = b"newly available"
        clock[0] += 1.6
        ready = await service._session_dict(session)
        assert THUMBS.key_for_data(session.props.art) == ready["thumbKey"]
        session.props.art = b"new artwork with same title"
        clock[0] += 16.0
        refreshed = await service._session_dict(session)
        assert refreshed["thumbKey"] != ready["thumbKey"]
        assert media_module.THUMBS.get(refreshed["thumbKey"]) == session.props.art
        session.props.art = None
        clock[0] += 16.0
        transient = await service._session_dict(session)
        assert transient["thumbKey"] == refreshed["thumbKey"]
        session.props.title = "A different video"
        different = await service._session_dict(session)
        assert different["thumbKey"] is None
    run(body())


def test_current_browser_session_wins_over_older_same_app():
    async def body():
        service, state = media_service(asyncio.get_running_loop())
        old = MediaSession(title="Old tab", status=5)
        current = MediaSession(title="Current tab", art=b"current art")
        service._mgr = MediaManager([old, current], current)
        await service._poll_once()
        await asyncio.sleep(0)
        assert len(state.updates[-1]) == 1
        card = state.updates[-1][0]
        assert card["title"] == "Current tab" and card["current"]
        assert service._find_session("Chrome") is current
        assert service._find_session("closed-source") is None
    run(body())


def test_noncurrent_source_prefers_playing_session_for_card_and_control():
    async def body():
        service, state = media_service(asyncio.get_running_loop())
        spotify = MediaSession("Spotify")
        paused = MediaSession(title="Paused tab", status=5)
        playing = MediaSession(title="Playing tab", art=b"playing art")
        service._mgr = MediaManager([paused, playing, spotify], spotify)
        await service._poll_once()
        await asyncio.sleep(0)
        assert [d["title"] for d in state.updates[-1] if d["id"] == "Chrome"] == ["Playing tab"]
        assert service._find_session("Chrome") is playing
    run(body())


def test_transient_properties_failure_preserves_card_until_source_closes():
    async def body():
        service, state = media_service(asyncio.get_running_loop())
        session = MediaSession()
        service._mgr = MediaManager([session], session)
        await service._poll_once()
        await asyncio.sleep(0)
        previous = state.updates[-1]
        session.fail_props = True
        await service._poll_once()
        await asyncio.sleep(0)
        assert state.updates[-1] == previous
        service._mgr.current = None
        service._mgr.sessions = []
        await service._poll_once()
        await asyncio.sleep(0)
        assert state.updates[-1] == []
        assert not service._art
    run(body())


def test_control_reports_native_refusal_and_success_does_not_wait_for_poll():
    async def body():
        service, _ = media_service(asyncio.get_running_loop())
        session = MediaSession()
        service._mgr = MediaManager([session], session)
        session.control_ok = False
        assert not await service._do_control("play_pause", "Chrome")
        assert service._refresh_task is None
        session.control_ok = True

        async def hung_poll():
            await asyncio.Event().wait()

        service._poll_once = hung_poll
        assert await asyncio.wait_for(service._do_control("play_pause", "Chrome"), 0.1)
        assert service._refresh_task is not None
        service._refresh_task.cancel()
        await asyncio.gather(service._refresh_task, return_exceptions=True)
    run(body())


def test_art_timeout_does_not_block_metadata_and_cancels_native_operation(monkeypatch):
    monkeypatch.setattr(media_module, "_THUMB_TIMEOUT_S", 0.01)

    class HungOperation:
        cancelled = False

        def __await__(self):
            return asyncio.Event().wait().__await__()

        def cancel(self):
            self.cancelled = True

    async def body():
        service, _ = media_service(asyncio.get_running_loop())
        operation = HungOperation()

        async def read_art(props):
            return await media_module._await_operation(operation)

        service._read_thumb = read_art
        card = await asyncio.wait_for(service._session_dict(MediaSession()), 0.2)
        assert card["title"] == "A video" and card["thumbKey"] is None
        assert operation.cancelled
    run(body())


def test_media_shutdown_cancels_pending_poll_and_rejects_controls():
    async def body():
        service = MediaService(None, asyncio.get_running_loop())
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        async def pending_poll():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        service._media_loop = asyncio.get_running_loop()
        service._service_task = asyncio.create_task(pending_poll())
        await entered.wait()
        await service.stop()
        await asyncio.gather(service._service_task, return_exceptions=True)
        assert cancelled.is_set()
        assert not await service.control("play_pause", "Chrome")
    run(body())


def test_native_art_read_closes_resources_and_rejects_partial_or_oversized_data(monkeypatch):
    class Stream:
        def __init__(self, size):
            self.size, self.closed, self.input_closed = size, False, False

        def get_input_stream_at(self, offset):
            assert offset == 0
            return SimpleNamespace(close=lambda: setattr(self, "input_closed", True))

        def close(self):
            self.closed = True

    class Reader:
        loaded = 3
        instances = []

        def __init__(self, input_stream):
            self.closed = False
            self.instances.append(self)

        async def load_async(self, size):
            return self.loaded

        def read_bytes(self, buffer):
            buffer[:] = b"art"

        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "winsdk.windows.storage.streams", SimpleNamespace(DataReader=Reader))

    async def body():
        service = MediaService(None, asyncio.get_running_loop())
        for size, loaded, expected in [(3, 3, b"art"), (3, 2, None),
                                       (media_module._MAX_THUMB_BYTES + 1, 3, None)]:
            stream = Stream(size)

            async def open_read():
                return stream

            Reader.loaded = loaded
            props = SimpleNamespace(thumbnail=SimpleNamespace(open_read_async=open_read))
            result = await service._read_thumb(props)
            assert result == expected
            assert stream.closed
            if size <= media_module._MAX_THUMB_BYTES:
                assert stream.input_closed and Reader.instances[-1].closed
    run(body())


# ---- snapshot carries media ----------------------------------------------
def test_snapshot_has_media_field():
    async def body():
        async with engine_client() as (client, _s, _c):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_str(json.dumps({"t": "subscribe"}))
            snap = await recv_until(ws, "snapshot")
            assert "media" in snap and isinstance(snap["media"], list)
            await ws.close()
    run(body())

"""P8: now-playing media (SMTC) protocol + thumbnail serving.

The real SMTC read/transport path is Windows-only and needs live playback, so it's
exercised by a live check; here we test the controller wiring and the thumbnail
route with a fake media service (no winsdk, no real playback).
"""
import json

from agent.media import THUMBS, MediaThumbs, _app_label

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

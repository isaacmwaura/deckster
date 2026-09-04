"""P7: per-app input-mute bindings + live device meters.

Input mute is macro-backed (Windows has no per-app mic mute): the app id is bound
to the app's own mute/PTT hotkey and tapping the chip injects that combo. As in
P3, the key sender is faked so tests never type into the focused window.

Meters ride along inside the poll snapshot; here we assert the field is present
and well-formed via the inline-primed state.
"""
import json

import pytest

from agent.macros.bindings import AppInputBindings
from agent.macros.input import ComboError

from helpers import engine_client, hello, recv_until, run


# ---- bindings store -------------------------------------------------------
def test_bindings_set_validate_persist_remove(tmp_path):
    path = tmp_path / "app_bindings.json"
    b = AppInputBindings(path)
    b.set("discord", "ctrl+shift+m", "Discord mute")
    assert b.get("discord")["keys"] == "ctrl+shift+m"
    # app id is normalised the same way session ids are (Discord.exe -> discord)
    assert b.get("Discord.exe")["keys"] == "ctrl+shift+m"

    # persistence across instances
    b2 = AppInputBindings(path)
    assert b2.get("discord")["label"] == "Discord mute"
    assert b2.remove("discord") is True
    assert AppInputBindings(path).get("discord") is None


def test_bindings_reject_bad_combo(tmp_path):
    b = AppInputBindings(tmp_path / "app_bindings.json")
    with pytest.raises(ComboError):
        b.set("discord", "ctrl+nope")


# ---- controller protocol --------------------------------------------------
def test_set_then_fire_app_input_mute(tmp_path):
    async def body():
        bindings = AppInputBindings(tmp_path / "app_bindings.json")
        fired: list[str] = []
        async with engine_client(input_bindings=bindings, key_sender=fired.append) as (client, _s, _c):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_str(json.dumps({
                "t": "set_app_input_binding", "appId": "discord",
                "keys": "ctrl+shift+m", "label": "Discord mute",
            }))
            snap = await recv_until(ws, "snapshot")
            assert snap["appInputBindings"]["discord"]["keys"] == "ctrl+shift+m"

            await ws.send_str(json.dumps({"t": "app_input_mute", "appId": "discord"}))
            ok = await recv_until(ws, "app_input_ok")
            assert ok["appId"] == "discord"
            assert fired == ["ctrl+shift+m"]
            await ws.close()
    run(body())


def test_app_input_mute_unbound_errors(tmp_path):
    async def body():
        bindings = AppInputBindings(tmp_path / "app_bindings.json")
        async with engine_client(input_bindings=bindings, key_sender=lambda _k: None) as (client, _s, _c):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_str(json.dumps({"t": "app_input_mute", "appId": "chrome"}))
            err = await recv_until(ws, "error")
            assert err["code"] == "nobinding"
            await ws.close()
    run(body())


def test_set_app_input_binding_bad_combo_errors(tmp_path):
    async def body():
        bindings = AppInputBindings(tmp_path / "app_bindings.json")
        async with engine_client(input_bindings=bindings, key_sender=lambda _k: None) as (client, _s, _c):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_str(json.dumps({
                "t": "set_app_input_binding", "appId": "discord", "keys": "ctrl+nope",
            }))
            err = await recv_until(ws, "error")
            assert err["code"] == "badcombo"
            await ws.close()
    run(body())


def test_clear_app_input_binding(tmp_path):
    async def body():
        bindings = AppInputBindings(tmp_path / "app_bindings.json")
        bindings.set("discord", "ctrl+shift+m")
        async with engine_client(input_bindings=bindings, key_sender=lambda _k: None) as (client, _s, _c):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_str(json.dumps({"t": "clear_app_input_binding", "appId": "discord"}))
            snap = await recv_until(ws, "snapshot")
            assert "discord" not in snap["appInputBindings"]
            await ws.close()
    run(body())


# ---- real app icons -------------------------------------------------------
def test_icon_store_extract_cache_and_negative(monkeypatch):
    import agent.icons as icons
    store = icons.IconStore()
    calls = {"n": 0}

    def fake_extract(path):
        calls["n"] += 1
        return b"PNG" if path.endswith("app.exe") else None

    monkeypatch.setattr(icons, "extract_icon_png", fake_extract)

    key = store.get_or_extract(r"C:\games\app.exe")
    assert key and store.get_png(key) == b"PNG"
    # second call for the same path is served from cache (no re-extract)
    assert store.get_or_extract(r"C:\games\app.exe") == key
    assert calls["n"] == 1
    # a path with no icon caches the negative result (key None), no crash
    assert store.get_or_extract(r"C:\x\none.dll") is None
    assert store.get_or_extract(None) is None


def test_icon_route_serves_and_404(monkeypatch):
    async def body():
        import agent.icons as icons
        monkeypatch.setattr(icons, "extract_icon_png", lambda p: b"\x89PNGDATA")
        key = icons.ICONS.get_or_extract(r"C:\apps\demo.exe")
        async with engine_client() as (client, _s, _c):
            r = await client.get("/icon/" + key)
            assert r.status == 200
            assert r.headers["Content-Type"] == "image/png"
            assert await r.read() == b"\x89PNGDATA"
            r2 = await client.get("/icon/deadbeefdeadbeef")
            assert r2.status == 404
    run(body())


# ---- meters in the snapshot ----------------------------------------------
def test_snapshot_carries_meters():
    async def body():
        async with engine_client() as (client, _state, _ctl):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_str(json.dumps({"t": "subscribe"}))
            snap = await recv_until(ws, "snapshot")
            meters = snap["devices"]["meters"]
            assert set(meters) == {"output", "input"}
            assert 0.0 <= meters["output"] <= 1.0
            assert 0.0 <= meters["input"] <= 1.0
            await ws.close()
    run(body())

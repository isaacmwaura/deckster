"""P3: hotkey combo parsing, macro registry, and the macro protocol.

Keystroke injection is exercised only through an injected fake sender, so tests
never type into the focused window. Real SendInput is a live/manual check.
"""
import json

import pytest

from agent.macros.input import ComboError, KeyEvent, VK, parse_combo
from agent.macros.registry import MacroRegistry

from helpers import engine_client, hello, recv_until, run


# ---- parse_combo ----------------------------------------------------------
def test_parse_simple_combo_order():
    ev = parse_combo("ctrl+shift+m")
    # ctrl down, shift down, m down, m up, shift up, ctrl up
    assert [(e.vk, e.down) for e in ev] == [
        (VK["ctrl"], True), (VK["shift"], True), (VK["m"], True),
        (VK["m"], False), (VK["shift"], False), (VK["ctrl"], False),
    ]


def test_parse_modifier_order_is_canonical():
    # given out of order, ctrl must still press before alt
    ev = parse_combo("alt+ctrl+f4")
    downs = [e.vk for e in ev if e.down]
    assert downs == [VK["ctrl"], VK["alt"], VK["f4"]]


def test_parse_extended_key_flag():
    ev = parse_combo("ctrl+right")
    right = next(e for e in ev if e.vk == VK["right"])
    assert right.extended is True
    ctrl = next(e for e in ev if e.vk == VK["ctrl"])
    assert ctrl.extended is False


def test_parse_rejects_unknown_and_modifier_only():
    with pytest.raises(ComboError):
        parse_combo("ctrl+banana")
    with pytest.raises(ComboError):
        parse_combo("ctrl+shift")  # no non-modifier key
    with pytest.raises(ComboError):
        parse_combo("")


# ---- registry -------------------------------------------------------------
def test_registry_add_validate_persist_remove(tmp_path):
    path = tmp_path / "macros.json"
    reg = MacroRegistry(path)
    m = reg.add("Mute Discord Mic", "ctrl+shift+m")
    assert m["keys"] == "ctrl+shift+m"
    assert reg.get(m["id"]) == m

    # persistence across instances
    reg2 = MacroRegistry(path)
    assert reg2.get(m["id"])["label"] == "Mute Discord Mic"

    assert reg2.remove(m["id"]) is True
    assert MacroRegistry(path).get(m["id"]) is None


def test_registry_rejects_bad_combo(tmp_path):
    reg = MacroRegistry(tmp_path / "macros.json")
    with pytest.raises(ComboError):
        reg.add("bad", "ctrl+nope")


# ---- controller protocol (fake key sender) --------------------------------
# Inline engine + asyncio.run (see tests/helpers.py); the key sender is a fake
# list appender so no real keystrokes are injected.
def test_add_then_fire_macro(tmp_path):
    async def body():
        registry = MacroRegistry(tmp_path / "macros.json")
        fired: list[str] = []
        async with engine_client(registry=registry, key_sender=fired.append) as (client, _s, _c):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_str(json.dumps({
                "t": "add_macro", "label": "Mute Mic", "keys": "ctrl+shift+m",
            }))
            snap = await recv_until(ws, "snapshot")
            assert any(m["label"] == "Mute Mic" for m in snap["macros"])
            macro_id = next(m["id"] for m in snap["macros"] if m["label"] == "Mute Mic")

            await ws.send_str(json.dumps({"t": "macro", "macroId": macro_id}))
            ok = await recv_until(ws, "macro_ok")
            assert ok["macroId"] == macro_id
            assert fired == ["ctrl+shift+m"]
            await ws.close()
    run(body())


def test_unknown_macro_errors(tmp_path):
    async def body():
        registry = MacroRegistry(tmp_path / "macros.json")
        async with engine_client(registry=registry, key_sender=lambda _k: None) as (client, _s, _c):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_str(json.dumps({"t": "macro", "macroId": "does-not-exist"}))
            err = await recv_until(ws, "error")
            assert err["code"] == "nomacro"
            await ws.close()
    run(body())


def test_add_bad_combo_errors(tmp_path):
    async def body():
        registry = MacroRegistry(tmp_path / "macros.json")
        async with engine_client(registry=registry, key_sender=lambda _k: None) as (client, _s, _c):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_str(json.dumps({"t": "add_macro", "label": "x", "keys": "ctrl+nope"}))
            err = await recv_until(ws, "error")
            assert err["code"] == "badcombo"
            await ws.close()
    run(body())


def test_remove_macro(tmp_path):
    async def body():
        registry = MacroRegistry(tmp_path / "macros.json")
        m = registry.add("Temp", "f13")
        async with engine_client(registry=registry, key_sender=lambda _k: None) as (client, _s, _c):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_str(json.dumps({"t": "remove_macro", "id": m["id"]}))
            snap = await recv_until(ws, "snapshot")
            assert all(x["id"] != m["id"] for x in snap["macros"])
            await ws.close()
    run(body())

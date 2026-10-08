"""P0: transport round-trip — health, index, and the WebSocket handshake.

Uses aiohttp's in-process TestClient so no real port is bound.
"""
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent.server import create_app
from agent.state import AppState


@pytest.fixture
async def client():
    app = create_app(AppState())
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status == 200
    assert await resp.json() == {"ok": True, "app": "deckster"}


async def test_index_served(client):
    resp = await client.get("/")
    assert resp.status == 200
    assert "Deckster" in await resp.text()


async def test_ws_ping_pong(client):
    ws = await client.ws_connect("/ws")
    # NullAuth: hello authenticates and yields a snapshot.
    await ws.send_str(json.dumps({"t": "hello"}))
    snap = json.loads((await ws.receive()).data)
    assert snap["t"] == "snapshot"

    await ws.send_str(json.dumps({"t": "ping"}))
    pong = json.loads((await ws.receive()).data)
    assert pong["t"] == "pong"
    await ws.close()


async def test_ws_subscribe_snapshot_shape(client):
    ws = await client.ws_connect("/ws")
    await ws.send_str(json.dumps({"t": "hello"}))
    await ws.receive()  # initial snapshot after auth
    await ws.send_str(json.dumps({"t": "subscribe"}))
    snap = json.loads((await ws.receive()).data)
    assert snap["t"] == "snapshot"
    assert set(snap.keys()) >= {"sessions", "devices", "macros"}
    assert "speakerMaster" in snap["devices"]
    await ws.close()


async def test_viewports_require_auth_validate_dimensions_and_expire(client):
    from agent.server import STATE_KEY
    state = client.server.app[STATE_KEY]
    ws = await client.ws_connect("/ws")
    await ws.send_json({"t": "viewport", "width": 960, "height": 432})
    assert (await ws.receive_json())["code"] == "unauth"
    assert not state.client_viewports
    await ws.send_json({"t": "hello"})
    await ws.receive_json()
    for width in (True, 1, 9000, "960"):
        await ws.send_json({"t": "viewport", "width": width, "height": 432})
    await ws.send_json({"t": "ping"})
    await ws.receive_json()
    assert not state.client_viewports
    await ws.send_json({"t": "viewport", "width": 873, "height": 393, "name": "Tablet"})
    await ws.send_json({"t": "ping"})
    await ws.receive_json()
    assert list(state.client_viewports.values())[0] == {
        "width": 873, "height": 393, "name": "Tablet", "deviceId": ""}
    await ws.close()
    # Closing handshake may finish before the server finally block.
    import asyncio
    for _ in range(20):
        if not state.client_viewports:
            break
        await asyncio.sleep(.01)
    assert not state.client_viewports

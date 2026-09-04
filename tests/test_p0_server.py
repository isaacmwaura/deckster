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
    assert (await resp.json())["ok"] is True


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

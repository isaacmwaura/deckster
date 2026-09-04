"""Shared test helpers for controller/WebSocket tests.

Design note (important): the ws/controller tests use an *inline* engine that runs
jobs synchronously with no background thread. The real threaded AudioEngine is a
persistent poll loop that calls `call_soon_threadsafe`/`gc.collect` on the event
loop; running that thread concurrently with aiohttp's TestClient across repeated
per-test event loops in one process intermittently deadlocks the harness. The real
engine threading is covered separately by the two synchronous engine tests in
test_p1_audio.py and by live end-to-end runs. Here we test controller command
logic + protocol deterministically, without a thread.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
from contextlib import asynccontextmanager
from dataclasses import asdict

from aiohttp.test_utils import TestClient, TestServer

from agent.audio.mock import MockAudioBackend
from agent.controller import Controller
from agent.server import create_app
from agent.state import AppState


class InlineEngine:
    """Engine stand-in that runs jobs synchronously (no COM thread, no poll)."""

    def __init__(self, backend) -> None:
        self._b = backend

    def submit(self, fn):
        fut: concurrent.futures.Future = concurrent.futures.Future()
        try:
            fut.set_result(fn(self._b))
        except BaseException as exc:  # noqa: BLE001 - relay like the real engine
            fut.set_exception(exc)
        return fut

    def set_on_poll(self, cb=None) -> None:
        pass

    def start(self, *a, **k) -> None:
        pass

    def stop(self) -> None:
        pass


def prime_state(state: AppState, backend) -> None:
    """Populate state from the backend once, as the first real poll would."""
    sessions = [asdict(s) for s in backend.snapshot_sessions()]
    spk = vars(backend.get_master("speaker"))
    mic = vars(backend.get_master("mic"))
    dl = backend.list_devices()
    outs = [asdict(d) for d in dl.outputs]
    ins = [asdict(d) for d in dl.inputs]
    meters = backend.get_peaks()
    state.ingest_full(sessions, spk, mic, outs, ins, meters)


def run(coro):
    """Run a coroutine on a fresh event loop (like production's asyncio.run)."""
    return asyncio.run(coro)


@asynccontextmanager
async def engine_client(registry=None, key_sender=None, authenticator=None,
                        input_bindings=None, media=None):
    """Yield (client, state, controller) with an inline engine + aiohttp test server."""
    state = AppState()
    loop = asyncio.get_running_loop()
    backend = MockAudioBackend()
    engine = InlineEngine(backend)
    controller = Controller(state, engine, loop, registry=registry,
                            input_bindings=input_bindings, media=media, key_sender=key_sender)
    if registry is not None or input_bindings is not None:
        controller.load_initial_macros()
    prime_state(state, backend)
    app = create_app(state, controller=controller.handle, authenticator=authenticator)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, state, controller
    finally:
        await client.close()


async def recv(ws):
    return json.loads((await ws.receive()).data)


async def recv_until(ws, t, limit=12):
    """Read messages until one of type `t` (skips interleaved snapshots)."""
    for _ in range(limit):
        msg = await recv(ws)
        if msg.get("t") == t:
            return msg
    raise AssertionError(f"no {t!r} within {limit} frames")


async def hello(ws, token=None):
    """Send hello (optionally with a token) and consume the first frame."""
    payload = {"t": "hello"}
    if token is not None:
        payload["token"] = token
    await ws.send_str(json.dumps(payload))
    await ws.receive()

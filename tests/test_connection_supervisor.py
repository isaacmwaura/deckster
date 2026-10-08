"""Listener transitions are serialized and cannot overwrite TLS preferences."""
import asyncio
import json

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer

from agent.main import Runtime, _run_server
from agent.security.pairing import PairingManager
from agent.server import create_app, create_desktop_app
from agent.state import AppState


def runtime(tmp_path, monkeypatch, secure=False):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return Runtime("loopback", 8765, PairingManager(),
                   {"mode": "loopback", "secure": secure}, ssl_ctx=object())


async def test_rapid_requests_have_one_listener_owner(tmp_path, monkeypatch):
    rt = runtime(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    active = 0
    peak = 0
    opened = []

    class Site:
        async def stop(self):
            pass

    class Candidate(Site):
        def __init__(self, runner, host, port, ssl_context):
            self.host, self.secure = host, ssl_context is not None
        async def start(self):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            opened.append((self.host, self.secure))
            if len(opened) == 1:
                entered.set()
                await release.wait()
            active -= 1

    monkeypatch.setattr("agent.main.web.TCPSite", Candidate)
    rt.attach(object(), Site(), asyncio.get_running_loop())
    rt.apply_mode("lan")
    await entered.wait()
    rt.apply_secure(True)
    rt.apply_mode("loopback")
    await asyncio.sleep(0)
    release.set()
    await rt._rebind_task
    assert peak == 1
    assert opened == [("0.0.0.0", False), ("127.0.0.1", True)]
    assert rt.connection_health()["active"] == {"mode": "loopback", "secure": True}
    assert rt._active_generation == rt._generation
    await rt.close()


async def test_failed_transition_restores_secure_listener_preserves_preference(tmp_path, monkeypatch):
    rt = runtime(tmp_path, monkeypatch, secure=True)
    attempts = []
    fail_bind = True
    class Site:
        async def stop(self):
            pass
    class Candidate(Site):
        def __init__(self, runner, host, port, ssl_context):
            attempts.append((host, ssl_context))
            self.host = host
        async def start(self):
            if self.host == "0.0.0.0" and fail_bind:
                raise OSError("injected bind failure")
    monkeypatch.setattr("agent.main.web.TCPSite", Candidate)
    rt.attach(object(), Site(), asyncio.get_running_loop())
    rt.apply_mode("lan")
    await asyncio.sleep(0)
    await rt._rebind_task
    assert [x[0] for x in attempts] == ["0.0.0.0", "127.0.0.1"]
    assert all(x[1] is rt._ssl_ctx for x in attempts)
    saved = json.loads((tmp_path / "StreamControl/settings.json").read_text())
    assert saved["mode"] == "lan" and saved["secure"] is True
    health = rt.connection_health()
    assert health["requested"]["mode"] == "lan"
    assert health["active"] == {"mode": "loopback", "secure": True}
    assert health["ready"] is False and "injected" in health["error"]
    fail_bind = False
    assert rt.apply_mode("lan") == "lan"
    await asyncio.sleep(0)
    await rt._rebind_task
    assert rt.connection_health()["ready"] is True
    assert rt.connection_health()["active"] == {"mode": "lan", "secure": True}
    await rt.close()


def test_failed_persistence_does_not_mutate_requested_state(tmp_path, monkeypatch):
    rt = runtime(tmp_path, monkeypatch)
    def fail(settings):
        raise OSError("disk full")
    monkeypatch.setattr("agent.main.save_settings", fail)
    with pytest.raises(OSError):
        rt.apply_secure(True)
    with pytest.raises(OSError):
        rt.apply_mode("lan")
    assert rt.mode == "loopback" and rt.secure is False
    assert rt._generation == 0


def test_failed_qr_write_does_not_block_committed_listener_request(tmp_path, monkeypatch):
    rt = runtime(tmp_path, monkeypatch)
    def fail(*args):
        raise OSError("QR disk write failed")
    monkeypatch.setattr("agent.main._write_pair_qr", fail)
    assert rt.apply_mode("lan") == "lan"
    assert rt.apply_secure(True) is True
    assert rt.qr_path is None
    assert rt._generation == 2
    saved = json.loads((tmp_path / "StreamControl/settings.json").read_text())
    assert saved["mode"] == "lan" and saved["secure"] is True


async def test_readiness_differs_from_liveness_when_engine_missing():
    async with TestClient(TestServer(create_app(AppState()))) as client:
        assert (await client.get("/health")).status == 200
        response = await client.get("/ready")
        assert response.status == 503
        assert (await response.json())["engine"]["status"] == "unavailable"


async def test_local_readiness_rejects_stalled_owner_and_diagnostics_are_read_only():
    class Engine:
        def health(self):
            return {"ready": True, "status": "degraded", "jobAgeSeconds": 9}
    class Owner:
        _engine = Engine()
        async def handle(self, client, command):
            raise AssertionError("diagnostics cannot execute commands")
        def health(self):
            return {"queueDepth": 1}
    class Soundboard:
        calls = 0
        def diagnostics(self):
            self.calls += 1
            return {"processor": {"name": "bypass"}}
    class Connection:
        def connection_health(self):
            return {"ready": True}
    class Admin:
        _soundboard = Soundboard()
        _rt = Connection()
    admin, owner = Admin(), Owner()
    async with TestClient(TestServer(create_desktop_app(AppState(), owner.handle, admin))) as client:
        response = await client.get("/ready")
        assert response.status == 503
        response = await client.get("/admin/api/diagnostics")
        assert response.status == 200
        assert (await response.json())["audio"]["processor"]["name"] == "bypass"
        assert admin._soundboard.calls == 1
        assert (await client.get("/ready", headers={"Host": "untrusted.example"})).status == 403
        assert (await client.get("/admin/api/diagnostics", headers={"Host": "untrusted.example"})).status == 403


async def test_phone_diagnostics_reject_dns_rebinding_host():
    async with TestClient(TestServer(create_app(AppState()))) as client:
        assert (await client.get("/ready", headers={"Host": "untrusted.example"})).status == 403
        assert (await client.get("/admin/api/diagnostics", headers={"Host": "untrusted.example"})).status == 403


async def test_missing_startup_tls_keeps_desktop_available_for_recovery(tmp_path, monkeypatch, unused_tcp_port):
    from agent.admin import Admin
    from agent.security.allowlist import AllowList
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    pairing = PairingManager()
    rt = Runtime("loopback", unused_tcp_port, pairing, {"mode": "loopback", "secure": True})
    state = AppState()
    admin = Admin(rt, pairing, AllowList(tmp_path / "allowlist.json", "test"), audio_state=state)
    stop = asyncio.Event()
    task = asyncio.create_task(_run_server(rt, state, stop, admin=admin))
    try:
        async def desktop_started():
            while rt.desktop_starting:
                await asyncio.sleep(.01)
        await asyncio.wait_for(desktop_started(), 2)
        assert rt._site is None and rt.secure is True
        assert rt.connection_health()["active"]["mode"] is None
        async with ClientSession() as session:
            base = rt.desktop_url.removesuffix("/admin")
            async with session.get(base + "/health") as response:
                assert response.status == 200
            async with session.get(base + "/ready") as response:
                assert response.status == 503
                assert "TLS is unavailable" in (await response.json())["connection"]["error"]
        rt.apply_secure(False)
        async def recovered():
            while rt._site is None:
                await asyncio.sleep(.01)
        await asyncio.wait_for(recovered(), 2)
        assert rt.connection_health()["ready"] is True
    finally:
        stop.set()
        await task

"""Settings surface: the localhost-only /admin API and its loopback gate."""
import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent import server
from agent.admin import Admin
from agent.security.allowlist import AllowList
from agent.security.pairing import PairingManager
from agent.server import create_app
from agent.state import AppState


class FakeRuntime:
    """Stand-in for main.Runtime (no real sockets)."""
    def __init__(self, mode="loopback", port=8765):
        self.port = port
        self.secure = False
        self._set(mode)

    def _set(self, mode):
        self.mode = mode
        scheme = "https" if self.secure else "http"
        host = "localhost" if mode == "loopback" else "192.168.1.2"
        self.connect = {"url": "%s://%s:%d/" % (scheme, host, self.port),
                        "note": "note", "mode": mode, "port": self.port, "secure": self.secure}

    def apply_mode(self, mode):
        if mode in ("loopback", "lan"):
            self._set(mode)
        return self.mode

    def apply_secure(self, enabled):
        self.secure = bool(enabled)
        self._set(self.mode)
        return self.secure


def _admin(tmp_path):
    allow = AllowList(tmp_path / "allow.json", "salt")
    return Admin(FakeRuntime(), PairingManager(), allow), allow


# ---- loopback gate --------------------------------------------------------
class _Req:
    def __init__(self, remote):
        self.remote = remote


def test_is_local_accepts_loopback():
    assert server._is_local(_Req("127.0.0.1"))
    assert server._is_local(_Req("::1"))


def test_is_local_rejects_lan_peer():
    assert not server._is_local(_Req("192.168.1.50"))
    assert not server._is_local(_Req("10.0.0.9"))


# ---- Admin logic ----------------------------------------------------------
def test_admin_state_shape(tmp_path):
    admin, _ = _admin(tmp_path)
    s = admin.state()
    for key in ("version", "mode", "connectUrl", "port", "pairCode", "devices", "autostart", "frozen"):
        assert key in s
    assert s["mode"] == "loopback"
    assert isinstance(s["devices"], list)


def test_admin_set_mode(tmp_path):
    admin, _ = _admin(tmp_path)
    assert admin.set_mode("lan") == "lan"
    assert admin.state()["mode"] == "lan"
    assert "192.168" in admin.state()["connectUrl"]
    assert admin.set_mode("loopback") == "loopback"


def test_admin_revoke(tmp_path):
    admin, allow = _admin(tmp_path)
    allow.add("dev-1", "Phone", "hash")
    assert len(admin.state()["devices"]) == 1
    assert admin.revoke("dev-1") is True
    assert admin.state()["devices"] == []


# ---- HTTP routes ----------------------------------------------------------
@pytest.fixture
async def client(tmp_path):
    admin, allow = _admin(tmp_path)
    app = create_app(AppState(), admin=admin)
    async with TestClient(TestServer(app)) as c:
        c._allow = allow
        yield c


async def test_admin_state_route(client):
    r = await client.get("/admin/api/state")
    assert r.status == 200
    body = await r.json()
    assert body["mode"] == "loopback"


async def test_admin_mode_route_switches(client):
    r = await client.post("/admin/api/mode", json={"mode": "lan"})
    assert (await r.json())["mode"] == "lan"
    r2 = await client.get("/admin/api/state")
    assert (await r2.json())["mode"] == "lan"


async def test_admin_secure_route_toggles_scheme(client):
    r = await client.post("/admin/api/secure", json={"enabled": True})
    assert (await r.json())["secure"] is True
    state = await (await client.get("/admin/api/state")).json()
    assert state["secure"] is True
    assert state["connectUrl"].startswith("https://")


async def test_admin_revoke_route(client):
    client._allow.add("dev-x", "Phone", "hash")
    r = await client.post("/admin/api/device/revoke", json={"id": "dev-x"})
    assert (await r.json())["ok"] is True
    state = await (await client.get("/admin/api/state")).json()
    assert state["devices"] == []


async def test_admin_page_served(client):
    r = await client.get("/admin")
    assert r.status == 200
    assert "Deckster" in await r.text()


async def test_workspace_commands_reject_cross_origin_and_missing_origin(client):
    for origin in (None, "https://untrusted.example"):
        headers = {"Origin": origin} if origin else {}
        response = await client.post("/admin/api/workspace", json={"action": "audition", "id": "bell"}, headers=headers)
        assert response.status == 403


async def test_workspace_routes_reject_lan_and_missing_admin(monkeypatch):
    async with TestClient(TestServer(create_app(AppState()))) as client:
        assert (await client.get("/admin/api/workspace")).status == 404
        monkeypatch.setattr(server, "_is_local", lambda request: False)
        assert (await client.get("/admin/api/workspace")).status == 403
        assert (await client.post("/admin/api/workspace", json={"action": "stop"})).status == 403


async def test_admin_unavailable_when_not_injected():
    # No admin wired -> the API reports unavailable rather than crashing.
    async with TestClient(TestServer(create_app(AppState()))) as c:
        r = await c.get("/admin/api/state")
        assert r.status == 404


async def test_desktop_origin_blocks_rebinding_cross_origin_and_phone_socket(tmp_path):
    admin, _ = _admin(tmp_path)
    app = server.create_desktop_app(AppState(), None, admin)
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        assert (await client.get("/admin")).status == 200
        assert (await client.get("/ws")).status == 404
        assert (await client.get("/admin", headers={"Host": "attacker.example"})).status == 403
        for origin in (None, "https://attacker.example"):
            headers = {"Origin": origin} if origin else {}
            assert (await client.post("/admin/api/mode", json={"mode": "lan"}, headers=headers)).status == 403
        origin = str(client.make_url("/")).rstrip("/")
        response = await client.post("/admin/api/mode", json={"mode": "lan"}, headers={"Origin": origin})
        assert response.status == 200
        assert admin.state()["mode"] == "lan"


def test_desktop_launcher_has_no_certificate_bypass(monkeypatch):
    from agent.desktop import workspace_command
    from pathlib import Path
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    class WorkspaceAdmin:
        def state(self):
            return {"secure": True, "desktopUrl": "http://127.0.0.1:54321/admin"}
    command = workspace_command(WorkspaceAdmin())
    assert "--app=http://127.0.0.1:54321/admin" in command
    assert not any("certificate" in option for option in command)


async def test_desktop_import_edit_reset_and_remove_preserve_routes(tmp_path):
    import io
    import wave
    from aiohttp import FormData
    from agent.soundboard import SoundboardService
    from agent.presentation import PresentationService
    state = AppState()
    library = SoundboardService(tmp_path / "sounds", defaults_root=tmp_path / "no-defaults")
    presentation = PresentationService(tmp_path / "layout", library)
    admin, _ = _admin(tmp_path)
    admin._soundboard, admin._presentation, admin._audio_state = library, presentation, state
    source = io.BytesIO()
    with wave.open(source, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(b"\x00\x00" * 4800)
    async with TestClient(TestServer(server.create_desktop_app(state, None, admin))) as client:
        origin = str(client.make_url("/")).rstrip("/")
        headers = {"Origin": origin}
        form = FormData()
        form.add_field("file", source.getvalue(), filename="my-sound.wav", content_type="audio/wav")
        response = await client.post("/admin/api/import", data=form, headers=headers)
        assert response.status == 200
        clip = await response.json()
        assert clip["label"] == "my-sound"
        async def action(body):
            return await client.post("/admin/api/workspace", json=body, headers=headers)
        assert (await action({"action": "clip", "id": clip["id"], "changes": {"gain": .6, "ears": True}})).status == 200
        before = library.snapshot()
        assert (await action({"action": "defaults", "baseRevision": 0})).status == 200
        assert library.snapshot() == before
        assert presentation.snapshot()["padSlots"][0] == clip["id"]
        assert (await action({"action": "defaults", "baseRevision": 0})).status == 409
        assert (await action({"action": "remove_clip", "id": clip["id"]})).status == 200
        assert library.snapshot()["config"] == before["config"]
        assert library.snapshot()["clips"] == []
        assert presentation.snapshot()["padSlots"] == [None] * 12

"""P4: port fallback, connect targets, QR generation, and the service-worker route.

The client-side hardening (reconnect/resume/wake-lock/SW registration) is verified
live in a headless browser, not here.
"""
import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent import net
from agent.server import create_app
from agent.state import AppState


# ---- port fallback --------------------------------------------------------
def test_port_preferred_when_free():
    assert net.find_available_port(8765, is_free=lambda h, p: True) == 8765


def test_port_falls_back_to_next_free():
    free = {8768}
    got = net.find_available_port(8765, is_free=lambda h, p: p in free)
    assert got == 8768


def test_port_exhausted_raises():
    with pytest.raises(RuntimeError):
        net.find_available_port(8765, attempts=3, is_free=lambda h, p: False)


# ---- connect targets ------------------------------------------------------
def test_connect_targets_loopback():
    c = net.connect_targets("loopback", 8765)
    assert c["url"] == "http://localhost:8765/"
    assert "adb reverse" in c["note"]


def test_connect_targets_lan_uses_ip_and_port():
    c = net.connect_targets("lan", 9000)
    assert c["url"].startswith("http://") and c["url"].endswith(":9000/")
    assert "localhost" not in c["url"]  # a real interface IP, not loopback label


# ---- QR -------------------------------------------------------------------
def test_write_qr_png(tmp_path):
    path = net.write_qr_png("http://localhost:8765/", tmp_path / "qr.png")
    assert path is not None and path.exists()
    # PNG magic number
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


# ---- QR pairing URL (code embedded so scanning both opens + pairs) --------
def test_pair_url_appends_code():
    from agent.main import _pair_url
    assert _pair_url("http://localhost:8765/", "123456") == "http://localhost:8765/?pair=123456"
    # respects an existing query string
    assert _pair_url("http://host:9000/?x=1", "9") == "http://host:9000/?x=1&pair=9"


# ---- Runtime: live USB<->Wi-Fi toggle ------------------------------------
def _runtime(tmp_path, monkeypatch, mode="loopback"):
    """Build a Runtime with all state redirected under tmp_path."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))  # data_dir()/settings under tmp
    from agent.main import Runtime
    from agent.security.pairing import PairingManager
    return Runtime(mode, 8765, PairingManager(), {"mode": mode, "port": 8765})


def test_runtime_binds_loopback_by_default(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, "loopback")
    assert rt.bind_host() == "127.0.0.1"
    assert "localhost" in str(rt.connect["url"])


def test_runtime_toggle_switches_to_lan_and_persists(tmp_path, monkeypatch):
    import json

    from agent.config import data_dir
    rt = _runtime(tmp_path, monkeypatch, "loopback")
    assert rt.toggle() == "lan"                 # no loop attached -> no rebind, just state
    assert rt.mode == "lan"
    assert rt.bind_host() == "0.0.0.0"          # LAN binds all interfaces (superset of USB)
    assert "localhost" not in str(rt.connect["url"])
    # choice is persisted so it survives a restart
    saved = json.loads((data_dir() / "settings.json").read_text(encoding="utf-8"))
    assert saved["mode"] == "lan"


def test_runtime_toggle_round_trips_back_to_loopback(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, "loopback")
    rt.toggle()
    assert rt.toggle() == "loopback"
    assert rt.bind_host() == "127.0.0.1"


def test_runtime_apply_secure_switches_scheme(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, "lan")
    assert str(rt.connect["url"]).startswith("http://")
    assert rt.ssl_context() is None                 # off -> no TLS context bound
    assert rt.apply_secure(True) is True
    assert str(rt.connect["url"]).startswith("https://")


# ---- service worker route -------------------------------------------------
@pytest.fixture
async def client():
    async with TestClient(TestServer(create_app(AppState()))) as c:
        yield c


# ---- /qr pairing page -----------------------------------------------------
async def test_qr_page_renders_code_and_image():
    app = create_app(AppState(), pair_info=lambda: {"url": "http://localhost:8765/", "code": "424242"})
    async with TestClient(TestServer(app)) as c:
        r = await c.get("/qr")
        assert r.status == 200
        body = await r.text()
        assert "4 2 4 2 4 2" in body           # spaced code for readability
        assert "data:image/png;base64," in body  # embedded live QR


async def test_qr_page_without_pair_info():
    async with TestClient(TestServer(create_app(AppState()))) as c:
        r = await c.get("/qr")
        assert r.status == 200  # graceful message, no crash


async def test_sw_served_at_root_scope(client):
    resp = await client.get("/sw.js")
    assert resp.status == 200
    assert "javascript" in resp.headers.get("Content-Type", "")
    assert resp.headers.get("Service-Worker-Allowed") == "/"
    body = await resp.text()
    assert "streamctl-shell" in body

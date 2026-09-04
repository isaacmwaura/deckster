"""aiohttp server: static web surface + /ws WebSocket with an auth gate.

The message protocol (see BUILD-PLAN.md 3) flows over one persistent WebSocket:
intents up, snapshots/state down. This module owns transport and the auth gate;
it delegates the meaning of commands to a `controller` and authentication to an
`authenticator`, both injected so phases P1/P2 extend without rewriting P0.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from aiohttp import WSMsgType, web

from .config import resource_root
from .log import get_logger
from .state import AppState

log = get_logger("server")
WEB_DIR = resource_root() / "web"

# Typed application keys (avoids aiohttp's NotAppKeyWarning).
STATE_KEY: "web.AppKey[AppState]" = web.AppKey("state", AppState)
CONTROLLER_KEY: web.AppKey = web.AppKey("controller", object)
AUTH_KEY: web.AppKey = web.AppKey("auth", object)
PAIRINFO_KEY: web.AppKey = web.AppKey("pairinfo", object)
ADMIN_KEY: web.AppKey = web.AppKey("admin", object)

# Loopback peers allowed to reach the settings surface (never a LAN phone).
_LOCAL_PEERS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})


def _is_local(request: web.Request) -> bool:
    """True only for a request from this machine's loopback interface."""
    return request.remote in _LOCAL_PEERS


class Client:
    """Per-connection context: the socket plus whether it has authenticated."""

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.ws = ws
        self.authed = False
        self.device_id: str | None = None
        # aiohttp forbids concurrent writers on one websocket. Both the broadcast
        # pump task and the request handler send here, so serialise all writes
        # through this lock to avoid an intermittent write deadlock.
        self._send_lock = asyncio.Lock()

    async def send(self, message: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.ws.send_str(json.dumps(message))


class Authenticator(Protocol):
    """Gate for pre-auth messages. P0 uses NullAuth (open); P2 swaps in real pairing."""

    def requires_auth(self) -> bool: ...

    async def handle_preauth(self, client: Client, msg: dict[str, Any]) -> bool:
        """Handle a pairing/hello message. Return True if the client is now authed."""
        ...


class NullAuth:
    """P0 authenticator: every connection is immediately trusted (single-machine dev)."""

    def requires_auth(self) -> bool:
        return False

    async def handle_preauth(self, client: Client, msg: dict[str, Any]) -> bool:
        client.authed = True
        return True


# controller signature: async (client, msg) -> None
ControllerFn = Callable[[Client, dict[str, Any]], Awaitable[None]]


async def _default_controller(client: Client, msg: dict[str, Any]) -> None:
    """P0 controller: answer ping and subscribe; unknown types get an error."""
    t = msg.get("t")
    if t == "ping":
        await client.send({"t": "pong"})
    else:
        await client.send({"t": "error", "code": "unimpl", "msg": f"no handler for {t!r}"})


def create_app(
    state: AppState,
    controller: ControllerFn | None = None,
    authenticator: Authenticator | None = None,
    pair_info: "Callable[[], dict[str, str]] | None" = None,
    admin: "object | None" = None,
) -> web.Application:
    controller = controller or _default_controller
    authenticator = authenticator or NullAuth()

    app = web.Application()
    app[STATE_KEY] = state
    app[CONTROLLER_KEY] = controller
    app[AUTH_KEY] = authenticator
    app[PAIRINFO_KEY] = pair_info
    app[ADMIN_KEY] = admin

    app.router.add_get("/ws", _ws_handler)
    app.router.add_get("/", _index)
    app.router.add_get("/health", _health)
    # A viewable pairing QR page for the local user to scan (see /qr handler).
    app.router.add_get("/qr", _qr_page)
    # Settings surface — localhost-only (the .exe's control panel).
    app.router.add_get("/admin", _admin_page)
    app.router.add_get("/admin/api/state", _admin_state)
    app.router.add_post("/admin/api/mode", _admin_mode)
    app.router.add_post("/admin/api/secure", _admin_secure)
    app.router.add_post("/admin/api/pair/refresh", _admin_pair_refresh)
    app.router.add_post("/admin/api/device/revoke", _admin_revoke)
    app.router.add_post("/admin/api/autostart", _admin_autostart)
    # Real app icons, keyed by an opaque hash (see agent.icons.IconStore).
    app.router.add_get("/icon/{key}", _icon)
    # Now-playing album/video art, keyed by an opaque hash (see agent.media).
    app.router.add_get("/media_thumb/{key}", _media_thumb)
    # Service worker must be served from root scope to control "/".
    app.router.add_get("/sw.js", _sw)
    # Static assets (app.js, style.css, manifest, etc.) served from web/.
    app.router.add_static("/static/", WEB_DIR, name="static")
    return app


async def _health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _icon(request: web.Request) -> web.Response:
    from .icons import ICONS
    key = request.match_info.get("key", "")
    png = ICONS.get_png(key)
    if png is None:
        return web.Response(status=404)
    # Icons are immutable per key (hash of exe path); let the phone cache hard.
    return web.Response(body=png, content_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


async def _media_thumb(request: web.Request) -> web.Response:
    from .media import THUMBS
    data = THUMBS.get(request.match_info.get("key", ""))
    if data is None:
        return web.Response(status=404)
    # Art is keyed by (app,title) so it's immutable for that key; cache it.
    return web.Response(body=data, content_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=3600"})


async def _qr_page(request: web.Request) -> web.Response:
    """A local page showing the pairing QR + code, for the user to scan with the phone.

    The QR is generated live from the *current* pairing code so it never goes stale;
    it encodes the connect URL with ?pair=CODE, so scanning it opens the app and
    pairs in one step.
    """
    import base64

    from .net import qr_png_bytes

    info = request.app[PAIRINFO_KEY]
    data = info() if callable(info) else None
    if not data:
        return web.Response(text="Pairing QR unavailable.", content_type="text/plain")
    url, code = data.get("url", ""), data.get("code", "")
    sep = "&" if "?" in url else "?"
    pair_url = f"{url}{sep}pair={code}"
    png = qr_png_bytes(pair_url)
    img = ("data:image/png;base64," + base64.b64encode(png).decode()) if png else ""
    spaced = " ".join(code)  # easier to read/type
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pair Deckster</title>
<style>
 html,body{{margin:0;height:100%;background:#0b0e14;color:#e8eaef;
   font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;}}
 .wrap{{min-height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:24px;box-sizing:border-box;}}
 h1{{font-size:22px;font-weight:800;margin:0 0 4px;}}
 p{{color:#9a9ea8;margin:0 0 20px;font-size:14px;text-align:center;max-width:360px;}}
 .card{{background:#fff;padding:18px;border-radius:18px;line-height:0;}}
 .card img{{width:300px;height:300px;image-rendering:pixelated;}}
 .code{{margin-top:22px;font-size:34px;font-weight:800;letter-spacing:10px;}}
 .code small{{display:block;letter-spacing:1px;font-size:11px;font-weight:700;color:#6b6f78;margin-top:6px;text-align:center;}}
</style></head><body><div class="wrap">
 <h1>Pair your phone</h1>
 <p>Scan this with the phone's camera — it opens Deckster and pairs automatically. Or type the code below.</p>
 <div class="card"><img alt="Pairing QR" src="{img}"></div>
 <div class="code">{spaced}<small>Manual pairing code</small></div>
</div></body></html>"""
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-store"})


# ---- settings surface (localhost only) ------------------------------------
def _admin_guard(request: web.Request):
    """Return (admin, None) when allowed, else (None, error response)."""
    if not _is_local(request):
        return None, web.Response(status=403, text="settings are localhost-only")
    admin = request.app[ADMIN_KEY]
    if admin is None:
        return None, web.Response(status=404, text="settings unavailable")
    return admin, None


async def _json_body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - malformed body -> empty
        return {}


async def _admin_page(request: web.Request) -> web.Response:
    if not _is_local(request):
        return web.Response(status=403, text="settings are localhost-only")
    page = WEB_DIR / "admin.html"
    if not page.exists():
        return web.Response(text="settings page missing", content_type="text/plain")
    return web.FileResponse(page, headers={"Cache-Control": "no-cache"})


async def _admin_state(request: web.Request) -> web.Response:
    admin, err = _admin_guard(request)
    if err:
        return err
    return web.json_response(admin.state())


async def _admin_mode(request: web.Request) -> web.Response:
    admin, err = _admin_guard(request)
    if err:
        return err
    body = await _json_body(request)
    return web.json_response({"mode": admin.set_mode(str(body.get("mode", "")))})


async def _admin_secure(request: web.Request) -> web.Response:
    admin, err = _admin_guard(request)
    if err:
        return err
    body = await _json_body(request)
    return web.json_response({"secure": admin.set_secure(bool(body.get("enabled")))})


async def _admin_pair_refresh(request: web.Request) -> web.Response:
    admin, err = _admin_guard(request)
    if err:
        return err
    return web.json_response({"code": admin.refresh_code()})


async def _admin_revoke(request: web.Request) -> web.Response:
    admin, err = _admin_guard(request)
    if err:
        return err
    body = await _json_body(request)
    if body.get("all"):
        admin.revoke_all()
        return web.json_response({"ok": True})
    return web.json_response({"ok": admin.revoke(str(body.get("id", "")))})


async def _admin_autostart(request: web.Request) -> web.Response:
    admin, err = _admin_guard(request)
    if err:
        return err
    body = await _json_body(request)
    return web.json_response({"ok": admin.set_autostart(bool(body.get("enabled")))})


async def _index(request: web.Request) -> web.Response:
    index = WEB_DIR / "index.html"
    if not index.exists():
        return web.Response(text="Deckster agent is running.", content_type="text/plain")
    # no-cache = the browser must revalidate "/" every load (cheap 304 when unchanged).
    # Without this the phone served a stale index that still pointed at old ?v= assets,
    # so UI updates silently didn't take even after bumping the asset version.
    return web.FileResponse(index, headers={"Cache-Control": "no-cache"})


async def _sw(request: web.Request) -> web.Response:
    sw = WEB_DIR / "sw.js"
    if not sw.exists():
        return web.Response(status=404)
    # Root-scoped service worker; no-cache so shell updates are picked up.
    return web.FileResponse(sw, headers={
        "Content-Type": "application/javascript",
        "Cache-Control": "no-cache",
        "Service-Worker-Allowed": "/",
    })


async def _ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    state: AppState = request.app[STATE_KEY]
    controller: ControllerFn = request.app[CONTROLLER_KEY]
    auth: Authenticator = request.app[AUTH_KEY]

    client = Client(ws)
    queue = state.subscribe()

    import asyncio

    async def pump() -> None:
        """Forward broadcast messages from state to this client."""
        try:
            while True:
                message = await queue.get()
                if client.authed:
                    await client.send(message)
        except (ConnectionResetError, asyncio.CancelledError):
            pass

    pump_task = asyncio.create_task(pump())
    log.info("client connected: %s", request.remote)

    try:
        async for raw in ws:
            if raw.type != WSMsgType.TEXT:
                continue
            try:
                msg = json.loads(raw.data)
            except json.JSONDecodeError:
                await client.send({"t": "error", "code": "badjson", "msg": "invalid JSON"})
                continue

            t = msg.get("t")

            # Pre-auth gate: only pairing/hello allowed until authed.
            if not client.authed:
                if t in ("pair", "hello"):
                    became = await auth.handle_preauth(client, msg)
                    if became:
                        # On auth success, immediately deliver a snapshot.
                        await client.send(state.snapshot())
                    continue
                await client.send({"t": "error", "code": "unauth", "msg": "authenticate first"})
                continue

            if t == "subscribe":
                await client.send(state.snapshot())
                continue

            await controller(client, msg)
    finally:
        pump_task.cancel()
        state.unsubscribe(queue)
        log.info("client disconnected: %s", request.remote)

    return ws

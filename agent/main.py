"""Agent entrypoint: start the audio engine (P1), the web/WebSocket server, and
a best-effort system tray icon.

Threading model (BUILD-PLAN.md 5): the aiohttp server runs on the main thread's
asyncio loop; the system tray runs on a secondary thread (best-effort, since a
headless environment has no tray); the audio engine (P1) owns its own COM thread.
A single shutdown Event is observed by all of them.

Connection mode is live-switchable: `Runtime` binds the server to 127.0.0.1
(loopback / wired USB-C) or 0.0.0.0 (LAN / Wi-Fi) and can rebind the listening
socket in-process, so the tray can flip USB<->Wi-Fi without a restart. 0.0.0.0
is a superset of loopback, so USB keeps working while LAN is enabled.
"""
from __future__ import annotations

import argparse
import asyncio
import queue
import threading

from aiohttp import web

from . import __version__
from .admin import Admin
from .audio.engine import AudioEngine
from .config import (BIND_LAN, BIND_LOOPBACK, data_dir, load_settings,
                     resource_root, save_settings)
from .controller import Controller
from .log import get_logger
from .macros.bindings import AppInputBindings
from .macros.registry import MacroRegistry
from .media import MediaService
from .net import connect_targets, find_available_port, write_qr_png
from .security.allowlist import AllowList
from .security.auth import DeviceAuthenticator
from .security.pairing import PairingManager
from .server import create_app
from .state import AppState
from .transport.adb import AdbReverse
from .window import run_window

log = get_logger("main")


def _pair_url(base_url: str, code: str) -> str:
    """Append the pairing code to the connect URL as a query param."""
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}pair={code}"


def _write_pair_qr(connect_url: str, code: str):
    """(Re)write the pairing QR encoding connect_url?pair=code. Returns the path."""
    return write_qr_png(_pair_url(connect_url, code), data_dir() / "connect_qr.png")


class Runtime:
    """Live, mutable connection state shared by the server and the tray.

    Holds the current bind mode, the phone-facing connect target/QR, and a handle
    on the running aiohttp site so the tray can flip loopback<->LAN and rebind the
    listening socket without a process restart. Mode changes are persisted so the
    choice survives a restart.
    """

    def __init__(self, mode: str, port: int, pairing: PairingManager,
                 settings: dict, ssl_ctx=None, advertiser=None) -> None:
        self.port = port
        self._pairing = pairing
        self._settings = settings
        self._ssl_ctx = ssl_ctx                       # loaded once; used only when secure
        self._advertiser = advertiser                 # mDNS (LAN only); best-effort
        self.secure = bool(settings.get("secure"))
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.set_mode(mode)

    def set_mode(self, mode: str) -> None:
        """Update mode + the derived connect target/QR (no socket work)."""
        self.mode = mode
        self.connect = connect_targets(mode, self.port, self.secure)
        self.qr_path = _write_pair_qr(str(self.connect["url"]),
                                      self._pairing.current_code())

    def bind_host(self) -> str:
        return BIND_LOOPBACK if self.mode == "loopback" else BIND_LAN

    def ssl_context(self):
        """The SSL context to bind with, or None when running plain HTTP."""
        return self._ssl_ctx if self.secure else None

    def pair_info(self) -> dict[str, str]:
        """Live pairing info for the /qr page — always the current URL + code."""
        return {"url": str(self.connect["url"]), "code": self._pairing.current_code()}

    def attach(self, runner: web.AppRunner, site: web.TCPSite,
               loop: asyncio.AbstractEventLoop) -> None:
        """Record the running server handles so a later toggle can rebind."""
        self._runner = runner
        self._site = site
        self._loop = loop
        if self.mode == "lan" and self._advertiser is not None:
            self._advertiser.start()              # begin mDNS once we're actually listening

    def apply_mode(self, new_mode: str) -> str:
        """Switch to a specific bind mode ("loopback"/"lan"). Safe from any thread.

        Updates the displayed connect target/QR synchronously (so the tray/settings
        page show the new URL immediately), persists the choice, schedules the socket
        rebind on the server loop, and starts/stops mDNS advertising. No-op if already
        in that mode. Returns the current mode.
        """
        if new_mode not in ("loopback", "lan") or new_mode == self.mode:
            return self.mode
        self.set_mode(new_mode)
        self._settings["mode"] = new_mode
        save_settings(self._settings)
        if new_mode == "lan":
            log.warning("LAN mode: agent reachable on the network%s; pairing token "
                        "still required for every command.",
                        " (TLS on)" if self.secure else " without TLS")
        if self._advertiser is not None:
            self._advertiser.start() if new_mode == "lan" else self._advertiser.stop()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._rebind_socket()))
        return new_mode

    def apply_secure(self, enabled: bool) -> bool:
        """Turn HTTPS on/off: rebuild the connect scheme + rebind with/without TLS."""
        enabled = bool(enabled)
        if enabled == self.secure:
            return self.secure
        self.secure = enabled
        self._settings["secure"] = enabled
        save_settings(self._settings)
        self.set_mode(self.mode)                  # refresh URL/QR to http(s) scheme
        if self._advertiser is not None:
            self._advertiser.set_secure(enabled)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._rebind_socket()))
        return self.secure

    def toggle(self) -> str:
        """Flip loopback<->LAN (convenience for the tray). Returns the new mode."""
        return self.apply_mode("lan" if self.mode == "loopback" else "loopback")

    async def _rebind_socket(self) -> None:
        """Stop the current listening socket and start one on the new interface/scheme.

        Best-effort: on failure, try to restore a working listener so a bad toggle
        never leaves the agent unreachable.
        """
        if self._runner is None:
            return
        try:
            if self._site is not None:
                await self._site.stop()
            self._site = web.TCPSite(self._runner, self.bind_host(), self.port,
                                     ssl_context=self.ssl_context())
            await self._site.start()
            log.info("rebound: %s mode on %s:%d (%s)", self.mode, self.bind_host(),
                     self.port, self.connect["url"])
        except Exception:  # noqa: BLE001 - never leave the agent unreachable
            log.exception("rebind failed; restoring plain loopback")
            try:
                self.secure = False
                self.set_mode("loopback")
                self._settings["mode"] = "loopback"
                self._settings["secure"] = False
                save_settings(self._settings)
                self._site = web.TCPSite(self._runner, self.bind_host(), self.port,
                                         ssl_context=None)
                await self._site.start()
            except Exception:  # noqa: BLE001
                log.exception("failed to restore a listening socket")


def _make_backend_factory(use_mock: bool):
    """Return a factory the engine calls on its COM thread to build the backend."""
    if use_mock:
        from .audio.mock import MockAudioBackend
        return MockAudioBackend
    from .audio.pycaw_backend import PycawAudioBackend
    return PycawAudioBackend


def _start_tray(stop: threading.Event, runtime: Runtime, pairing: PairingManager,
                allowlist: AllowList, cmd_queue: "queue.Queue | None" = None) -> None:
    """Best-effort tray icon. Never fatal: if no GUI/tray is available, skip it.

    Surfaces the (live) connect URL/QR and the pairing code, a one-click
    USB<->Wi-Fi toggle, and a revoke-all so the local user can always see who is
    paired and cut access in one click.
    """
    try:
        import os

        import pystray
        from PIL import Image, ImageDraw

        from .config import resource_root
        try:
            img = Image.open(resource_root() / "web" / "icon-64.png")
        except Exception:  # noqa: BLE001 - fall back to a drawn dot
            img = Image.new("RGB", (64, 64), (27, 58, 92))
            ImageDraw.Draw(img).ellipse((16, 16, 48, 48), fill=(46, 117, 182))

        def on_quit(icon, item):  # noqa: ANN001
            stop.set()
            icon.stop()

        def show_code(icon, item):  # noqa: ANN001
            code = pairing.refresh()
            # Keep the QR in sync so scanning stays the primary path after a refresh.
            runtime.qr_path = _write_pair_qr(str(runtime.connect["url"]), code)
            path = runtime.qr_path
            log.info("PAIRING CODE: %s (QR refreshed at %s)", code, path)
            if path and os.path.exists(path):
                try:
                    os.startfile(path)  # pop the fresh QR to scan
                except Exception:  # noqa: BLE001
                    pass
            icon.notify(f"New code {code} — QR refreshed & opened", "Deckster")

        def show_window(icon, item):  # noqa: ANN001
            if cmd_queue is not None:
                cmd_queue.put("show")      # marshalled to the window's own UI thread

        def show_connect(icon, item):  # noqa: ANN001
            icon.notify(f"{runtime.connect['url']}\n{runtime.connect['note']}",
                        "Connect the phone")
            if runtime.qr_path and os.path.exists(runtime.qr_path):
                try:
                    os.startfile(runtime.qr_path)  # open the QR image for scanning
                except Exception:  # noqa: BLE001
                    pass

        def toggle_mode(icon, item):  # noqa: ANN001
            new_mode = runtime.toggle()
            if new_mode == "lan":
                icon.notify(f"Wi-Fi mode ON\n{runtime.connect['url']}\n"
                            "Open this on the phone (same Wi-Fi) and scan the QR.",
                            "Deckster")
            else:
                icon.notify("USB mode (loopback)\nReconnect over wired USB-C.",
                            "Deckster")

        def revoke_all(icon, item):  # noqa: ANN001
            allowlist.revoke_all()
            log.info("all devices revoked")
            icon.notify("All paired devices revoked", "Deckster")

        from . import autostart

        def toggle_autostart(icon, item):  # noqa: ANN001
            if autostart.is_enabled():
                autostart.disable()
            else:
                autostart.enable()

        menu = pystray.Menu(
            pystray.MenuItem(lambda item: f"Deckster — {runtime.connect['url']}",
                             None, enabled=False),
            pystray.MenuItem("Show Deckster", show_window, default=True),
            pystray.MenuItem("Show connect URL / QR", show_connect),
            pystray.MenuItem("Show / refresh pairing code", show_code),
            pystray.MenuItem(
                lambda item: ("Switch to USB (loopback)" if runtime.mode == "lan"
                              else "Switch to Wi-Fi (LAN)"),
                toggle_mode,
            ),
            pystray.MenuItem("Wi-Fi mode active", None,
                             checked=lambda item: runtime.mode == "lan", enabled=False),
            pystray.MenuItem("Revoke all devices", revoke_all),
            pystray.MenuItem("Start with Windows", toggle_autostart,
                            checked=lambda item: autostart.is_enabled()),
            pystray.MenuItem("Quit", on_quit),
        )
        icon = pystray.Icon("streamcontrol", img, "Deckster", menu)
        icon.run()
    except Exception as exc:  # noqa: BLE001 - tray is optional
        log.info("tray unavailable (%s); running headless", exc)


async def _run_server(runtime: Runtime, state: AppState, stop: asyncio.Event,
                      controller=None, authenticator=None, media=None,
                      admin=None, open_qr=None) -> None:
    app = create_app(state, controller=controller, authenticator=authenticator,
                     pair_info=runtime.pair_info, admin=admin)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, runtime.bind_host(), runtime.port,
                       ssl_context=runtime.ssl_context())
    await site.start()
    runtime.attach(runner, site, asyncio.get_running_loop())
    log.info("Deckster %s listening on http://%s:%d (%s mode)", __version__,
             runtime.bind_host(), runtime.port, runtime.mode)
    # Now that the server answers, pop the QR page for a first-time pairing.
    if open_qr:
        try:
            import webbrowser
            webbrowser.open(open_qr)
        except Exception:  # noqa: BLE001 - convenience only
            pass
    # SMTC now-playing needs the running loop; start it once the server is up.
    if media is not None:
        await media.start()
    try:
        await stop.wait()
    finally:
        if media is not None:
            await media.stop()
        await runner.cleanup()
        log.info("server stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deckster agent")
    parser.add_argument("--port", type=int, default=None, help="override port")
    parser.add_argument("--mode", choices=["loopback", "lan"], default=None,
                        help="override bind mode")
    parser.add_argument("--no-tray", action="store_true", help="do not start the tray icon")
    parser.add_argument("--mock", action="store_true",
                        help="use the in-memory mock audio backend (headless testing)")
    args = parser.parse_args()

    settings = load_settings()
    if args.port is not None:
        settings["port"] = args.port
    if args.mode is not None:
        settings["mode"] = args.mode

    mode = settings.get("mode", "loopback")
    host = BIND_LOOPBACK if mode == "loopback" else BIND_LAN
    # Port-conflict fallback: keep the preferred port if free, else the next one.
    requested_port = int(settings["port"])
    port = find_available_port(requested_port, host)
    if port != requested_port:
        log.warning("port %d busy; using %d instead", requested_port, port)
    state = AppState()

    # --- security stack (P2) ---------------------------------------------
    salt = settings["token_salt"]
    allowlist = AllowList(data_dir() / "allowlist.json", salt)
    pairing = PairingManager(
        ttl_s=int(settings.get("pair_code_ttl_seconds", 180)),
        max_attempts=int(settings.get("pair_max_attempts", 5)),
    )
    authenticator = DeviceAuthenticator(allowlist, pairing, salt)

    # Self-signed TLS (generated once): served when 'secure' is on; the Android app
    # pins its fingerprint. mDNS advertiser lets the app auto-discover the PC on Wi-Fi.
    from . import discovery, tls
    ssl_ctx = None
    fingerprint = ""
    try:
        cert_path, key_path = tls.ensure_cert(data_dir())
        ssl_ctx = tls.ssl_context(cert_path, key_path)
        fingerprint = tls.fingerprint(cert_path)
    except Exception:  # noqa: BLE001 - TLS is optional; degrade to plain HTTP
        log.exception("TLS setup failed; secure mode unavailable")
    advertiser = discovery.Advertiser(port, secure=bool(settings.get("secure")),
                                      fingerprint=fingerprint)

    # Live connection state (mode/connect target/QR). QR is the primary pairing
    # path: it encodes the connect URL *with* the current pairing code, so scanning
    # both opens the page and pairs in one step. Regenerated when the code changes.
    runtime = Runtime(mode, port, pairing, settings, ssl_ctx=ssl_ctx, advertiser=advertiser)
    # Settings surface (localhost-only): the .exe's control panel.
    admin = Admin(runtime, pairing, allowlist, fingerprint=fingerprint)

    # Safety: LAN mode exposes the agent to the network and has no TLS yet (P4).
    if runtime.mode == "lan":
        log.warning("LAN mode: agent is reachable on the network without TLS. "
                    "Pairing token is still required for every command.")

    # Surface both paths; scanning the QR is the fast one, the code is the fallback.
    qr_view = f"{runtime.connect['url'].rstrip('/')}/qr"
    log.info("CONNECT: %s  (%s)", runtime.connect["url"], runtime.connect["note"])
    log.info("PAIR: open %s on this PC to scan the QR (fallback code: %s)",
             qr_view, pairing.current_code())
    # First run (nothing paired yet): pop the QR page once the server is up.
    open_qr = qr_view if not allowlist.list_devices() else None

    # Wired USB-C: (re)apply adb reverse whenever the phone connects. Harmless in
    # LAN mode (the phone uses the LAN URL instead of the reversed localhost).
    adb = AdbReverse(port)
    adb.start_watcher()

    stop_thread = threading.Event()
    cmd_queue: "queue.Queue" = queue.Queue()
    if not args.no_tray:
        # Deckster's own window (its own UI thread) + a tray icon for the background.
        icon_png = str(resource_root() / "web" / "icon-64.png")
        threading.Thread(target=run_window,
                         args=(admin, stop_thread, cmd_queue, icon_png),
                         daemon=True).start()
        threading.Thread(target=_start_tray,
                        args=(stop_thread, runtime, pairing, allowlist, cmd_queue),
                        daemon=True).start()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop_async = asyncio.Event()

    # Audio engine (own COM thread) + controller (bridges engine <-> state <-> wire).
    poll_s = max(0.1, int(settings.get("poll_interval_ms", 400)) / 1000.0)
    engine = AudioEngine(_make_backend_factory(args.mock), poll_s)
    registry = MacroRegistry(data_dir() / "macros.json")
    bindings = AppInputBindings(data_dir() / "app_bindings.json")
    media = MediaService(state, loop)
    controller = Controller(state, engine, loop, registry=registry,
                            input_bindings=bindings, media=media)
    controller.load_initial_macros()
    engine.set_on_poll(controller.make_on_poll())
    try:
        engine.start()
    except Exception:  # noqa: BLE001 - if audio init fails, keep serving (degraded)
        log.exception("audio engine unavailable; continuing without audio")

    # Bridge the tray's threading.Event to the asyncio stop event.
    def _watch_tray_stop() -> None:
        stop_thread.wait()
        loop.call_soon_threadsafe(stop_async.set)

    threading.Thread(target=_watch_tray_stop, daemon=True).start()

    try:
        loop.run_until_complete(
            _run_server(runtime, state, stop_async,
                        controller=controller.handle, authenticator=authenticator,
                        media=media, admin=admin, open_qr=open_qr)
        )
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        stop_thread.set()
        advertiser.close()
        adb.stop()
        engine.stop()
        loop.close()


if __name__ == "__main__":
    main()

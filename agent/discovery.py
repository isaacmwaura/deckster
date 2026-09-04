"""mDNS/Zeroconf advertising so the Android app auto-discovers the PC on Wi-Fi.

Advertises `_streamctl._tcp.local.` carrying the LAN IP, port, and a TXT record
(version, secure flag, path). Only meaningful on the LAN, so it runs in Wi-Fi mode
and is stopped in USB mode. Everything is best-effort — a missing/failing zeroconf
never takes the agent down; discovery just goes quiet and QR/manual entry still work.

zeroconf's synchronous API refuses to run inside a live asyncio loop
(``EventLoopBlocked``). start()/stop() are called from both the server loop (the
settings API, startup) and the tray thread, so the actual zeroconf calls are
offloaded to a dedicated worker thread whenever a loop is running, and run inline
otherwise (tray thread, tests).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import socket

from .log import get_logger
from .net import lan_ip

log = get_logger("discovery")

SERVICE_TYPE = "_streamctl._tcp.local."


class Advertiser:
    """Registers/updates an mDNS service for the running agent."""

    def __init__(self, port: int, secure: bool = False, fingerprint: str = "",
                 zc_factory=None) -> None:
        self.port = port
        self.secure = secure
        self.fingerprint = fingerprint       # cert SHA-256 the app pins after discovery
        self._zc = None
        self._info = None
        self._zc_factory = zc_factory        # injectable for tests (avoid real network)
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None

    # ---- threading: keep zeroconf's sync calls off any running asyncio loop ----
    def _run(self, fn) -> None:
        try:
            asyncio.get_running_loop()       # on the server loop -> must offload
            if self._pool is None:
                self._pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="mdns")
            self._pool.submit(fn)
        except RuntimeError:
            fn()                             # no running loop (tray thread, tests) -> inline

    def _make_info(self):
        from zeroconf import ServiceInfo

        host = socket.gethostname().split(".")[0] or "pc"
        props = {b"v": b"1", b"secure": b"1" if self.secure else b"0", b"path": b"/"}
        if self.fingerprint:
            props[b"fp"] = self.fingerprint.encode("ascii", "ignore")
        return ServiceInfo(
            SERVICE_TYPE,
            f"Deckster @ {host}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(lan_ip())],
            port=self.port,
            properties=props,
            server=f"{host}.local.",
        )

    # ---- public API (safe from any thread) ------------------------------------
    def start(self) -> None:
        self._run(self._do_start)

    def stop(self) -> None:
        self._run(self._do_stop)

    def set_secure(self, secure: bool) -> None:
        """Re-advertise with the new TXT if running (so clients see the scheme change)."""
        if secure == self.secure:
            return
        self.secure = secure
        self._run(self._do_restart)

    def close(self) -> None:
        """Blocking stop for shutdown."""
        self._do_stop()
        if self._pool is not None:
            self._pool.shutdown(wait=False)

    # ---- workers (always off the asyncio loop) --------------------------------
    def _do_start(self) -> None:
        if self._zc is not None:
            return
        try:
            from zeroconf import Zeroconf
            self._zc = (self._zc_factory or Zeroconf)()
            self._info = self._make_info()
            self._zc.register_service(self._info)
            log.info("mDNS advertising %s on :%d (secure=%s)", SERVICE_TYPE,
                     self.port, self.secure)
        except Exception:  # noqa: BLE001 - discovery is optional
            log.exception("mDNS advertise failed; discovery disabled")
            self._zc = None
            self._info = None

    def _do_stop(self) -> None:
        if self._zc is None:
            return
        try:
            if self._info is not None:
                self._zc.unregister_service(self._info)
            self._zc.close()
        except Exception:  # noqa: BLE001
            log.exception("mDNS stop failed")
        finally:
            self._zc = None
            self._info = None

    def _do_restart(self) -> None:
        self._do_stop()
        self._do_start()

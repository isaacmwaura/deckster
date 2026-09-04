"""Now-playing media via Windows System Media Transport Controls (SMTC).

SMTC is the same system layer the OS media flyout and keyboard media keys use, so
it already tracks *every* media source — Spotify, the Music app, and crucially the
browser tab that is playing (or last played), with no per-tab hackery. We read the
sessions, expose title/artist/art/state + which transport buttons are valid, and
issue play/pause/next/previous back to the owning app.

Everything here runs on the asyncio event loop (winsdk's IAsyncOperations are
awaitable there). It is best-effort: if winsdk is missing or a call fails, the
service simply reports no media and the UI hides the page's contents.
"""
from __future__ import annotations

import asyncio
import hashlib
import threading
from typing import Any, Optional

from .log import get_logger

log = get_logger("media")

# playback_status enum: 0 Closed,1 Opened,2 Changing,3 Stopped,4 Playing,5 Paused
_STATUS = {0: "closed", 1: "opened", 2: "changing", 3: "stopped", 4: "playing", 5: "paused"}

_KNOWN = {
    "chrome": "Chrome", "msedge": "Edge", "microsoftedge": "Edge", "brave": "Brave",
    "firefox": "Firefox", "308046b0af4a39cb": "Firefox", "spotify": "Spotify",
    "vlc": "VLC", "foobar2000": "foobar2000", "opera": "Opera",
    "zen": "Zen", "librewolf": "LibreWolf",
}


def _app_label(aumid: str) -> str:
    a = (aumid or "").strip()
    # packaged AUMIDs look like "Family!App"; classic ones are exe names/paths
    a = a.split("!")[-1].split("\\")[-1]
    if a.lower().endswith(".exe"):
        a = a[:-4]
    key = a.lower()
    if key in _KNOWN:
        return _KNOWN[key]
    return a[:1].upper() + a[1:] if a else "Media"


class MediaThumbs:
    """Thread-safe key -> JPEG/PNG bytes cache for now-playing artwork."""

    def __init__(self) -> None:
        self._by_key: dict[str, bytes] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key_for(aumid: str, title: str) -> str:
        return hashlib.sha1((aumid + "|" + title).encode("utf-8")).hexdigest()[:16]

    def put(self, key: str, data: bytes) -> None:
        with self._lock:
            self._by_key[key] = data
            # keep the cache small; art turns over as tracks change
            if len(self._by_key) > 32:
                for k in list(self._by_key)[:-16]:
                    del self._by_key[k]

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._by_key

    def get(self, key: str) -> Optional[bytes]:
        with self._lock:
            return self._by_key.get(key)


# Process-wide singletons (server reads thumbs; service writes them).
THUMBS = MediaThumbs()


class MediaService:
    """Polls SMTC and mirrors now-playing state into AppState; issues transport.

    winsdk's async operations don't survive on the aiohttp event loop once the
    audio engine's COM apartment is in play (they hang). So — like the AudioEngine
    — the media work lives on its own dedicated thread with its own asyncio loop,
    where winsdk gets a clean apartment. Results are marshalled back to the main
    loop via call_soon_threadsafe; control() hops onto the media loop and back.
    """

    def __init__(self, state, loop: asyncio.AbstractEventLoop, interval_s: float = 1.5) -> None:
        self._state = state
        self._main_loop = loop
        self._interval = interval_s
        self._mgr = None
        self._media_loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_sig: str | None = None

    async def start(self) -> None:
        # Launch the dedicated media thread and return; readiness is best-effort.
        self._thread = threading.Thread(target=self._thread_main, name="media", daemon=True)
        self._thread.start()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._media_loop = loop
        try:
            loop.run_until_complete(self._amain())
        except Exception:  # noqa: BLE001
            log.debug("media thread ended", exc_info=True)
        finally:
            loop.close()

    async def _amain(self) -> None:
        try:
            from winsdk.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as Mgr,
            )
            self._mgr = await Mgr.request_async()
        except Exception as exc:  # noqa: BLE001 - no SMTC => feature simply absent
            log.info("SMTC unavailable (%s); media page disabled", exc)
            return
        log.info("media service started")
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except Exception as exc:  # noqa: BLE001 - a bad poll must not kill the loop
                log.warning("media poll failed: %r", exc)
            await asyncio.sleep(self._interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._thread:
            await self._main_loop.run_in_executor(None, self._thread.join, 3.0)

    def _find_session(self, app_id: str):
        if self._mgr is None:
            return None
        want = app_id or ""
        current = self._mgr.get_current_session()
        if want == "" and current is not None:
            return current
        for s in self._mgr.get_sessions():
            if s.source_app_user_model_id == want:
                return s
        return current

    async def _session_dict(self, s) -> Optional[dict[str, Any]]:
        aumid = s.source_app_user_model_id or ""
        try:
            props = await s.try_get_media_properties_async()
        except Exception:  # noqa: BLE001
            return None
        title = (props.title or "").strip()
        artist = (props.artist or "").strip()
        info = s.get_playback_info()
        status = _STATUS.get(int(info.playback_status), "unknown")
        c = info.controls
        thumb_key = None
        if title:
            thumb_key = MediaThumbs.key_for(aumid, title)
            if not THUMBS.has(thumb_key):
                data = await self._read_thumb(props)
                if data:
                    THUMBS.put(thumb_key, data)
                else:
                    thumb_key = None
        return {
            "id": aumid,
            "app": _app_label(aumid),
            "title": title,
            "artist": artist,
            "status": status,
            "canPlay": bool(c.is_play_enabled),
            "canPause": bool(c.is_pause_enabled),
            "canNext": bool(c.is_next_enabled),
            "canPrev": bool(c.is_previous_enabled),
            "thumbKey": thumb_key,
        }

    async def _read_thumb(self, props) -> Optional[bytes]:
        ref = getattr(props, "thumbnail", None)
        if ref is None:
            return None
        try:
            from winsdk.windows.storage.streams import DataReader
            stream = await ref.open_read_async()
            size = stream.size
            if not size:
                return None
            reader = DataReader(stream)
            await reader.load_async(size)
            buf = bytearray(size)
            reader.read_bytes(buf)
            return bytes(buf)
        except Exception:  # noqa: BLE001 - art is optional
            return None

    async def _poll_once(self) -> None:
        if self._mgr is None:
            return
        current = self._mgr.get_current_session()
        current_id = current.source_app_user_model_id if current else None
        sessions: list[dict[str, Any]] = []
        seen: set[str] = set()
        for s in self._mgr.get_sessions():
            d = await self._session_dict(s)
            if d and d["id"] not in seen and (d["title"] or d["status"] == "playing"):
                d["current"] = (d["id"] == current_id)
                sessions.append(d)
                seen.add(d["id"])
        # Put the current session first so the UI leads with it.
        sessions.sort(key=lambda d: (not d.get("current"), d["app"].lower()))
        sig = repr([(d["id"], d["title"], d["artist"], d["status"], d["current"],
                     d["canNext"], d["canPrev"], d["thumbKey"]) for d in sessions])
        if sig != self._last_sig:
            self._last_sig = sig
            # AppState + its subscriber queues live on the main loop; hop back to it.
            self._main_loop.call_soon_threadsafe(self._state.set_media, sessions)

    # ---- transport --------------------------------------------------------
    async def control(self, action: str, app_id: str = "") -> bool:
        """Called on the main loop; runs the winsdk work on the media loop."""
        loop = self._media_loop
        if loop is None:
            return False
        fut = asyncio.run_coroutine_threadsafe(self._do_control(action, app_id), loop)
        try:
            return await asyncio.wrap_future(fut)
        except Exception:  # noqa: BLE001
            return False

    async def _do_control(self, action: str, app_id: str) -> bool:
        s = self._find_session(app_id)
        if s is None:
            return False
        try:
            if action == "play_pause":
                await s.try_toggle_play_pause_async()
            elif action == "play":
                await s.try_play_async()
            elif action == "pause":
                await s.try_pause_async()
            elif action == "next":
                await s.try_skip_next_async()
            elif action == "previous":
                await s.try_skip_previous_async()
            else:
                return False
        except Exception:  # noqa: BLE001
            log.debug("media control %s failed", action, exc_info=True)
            return False
        # Reflect the change quickly rather than waiting for the next poll tick.
        await asyncio.sleep(0.2)
        try:
            await self._poll_once()
        except Exception:  # noqa: BLE001
            pass
        return True

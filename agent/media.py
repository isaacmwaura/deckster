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
from collections import OrderedDict
import hashlib
import threading
import time
from typing import Any, Optional

from .log import get_logger

log = get_logger("media")

_MAX_THUMB_BYTES = 4 * 1024 * 1024
_THUMB_REFRESH_S = 15.0
_PROPERTIES_TIMEOUT_S = 2.0
_THUMB_TIMEOUT_S = 1.5
_CONTROL_TIMEOUT_S = 3.0


async def _await_operation(operation, timeout_s: float | None = None):
    """Bound WinRT waits and cancel the native operation as well as the await."""
    try:
        if timeout_s is None:
            return await operation
        return await asyncio.wait_for(operation, timeout_s)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        cancel = getattr(operation, "cancel", None)
        if cancel is not None:
            try:
                cancel()
            except Exception:  # noqa: BLE001
                pass
        raise

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
        self._by_key: OrderedDict[str, bytes] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    @staticmethod
    def key_for(aumid: str, title: str) -> str:
        return hashlib.sha1((aumid + "|" + title).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def key_for_data(data: bytes) -> str:
        # A title is not an artwork identity (browser titles can be reused, and
        # SMTC can publish artwork after publishing the title). URLs are immutable.
        return hashlib.sha256(data).hexdigest()[:24]

    @staticmethod
    def content_type(data: bytes) -> str:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "image/webp"
        if data.startswith(b"BM"):
            return "image/bmp"
        return "application/octet-stream"

    def put(self, key: str, data: bytes) -> None:
        if not data or len(data) > _MAX_THUMB_BYTES:
            return
        with self._lock:
            self._bytes -= len(self._by_key.pop(key, b""))
            self._by_key[key] = data
            self._bytes += len(data)
            # Both count and bytes matter: a corrupt source must not grow this
            # process-wide cache indefinitely. Recently read art stays resident.
            while len(self._by_key) > 32 or self._bytes > 16 * 1024 * 1024:
                _, old = self._by_key.popitem(last=False)
                self._bytes -= len(old)

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._by_key

    def get(self, key: str) -> Optional[bytes]:
        with self._lock:
            data = self._by_key.get(key)
            if data is not None:
                self._by_key.move_to_end(key)
            return data


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
        self._art: dict[str, tuple[tuple, str | None, float]] = {}
        self._poll_lock: asyncio.Lock | None = None
        self._last_sessions: dict[str, dict[str, Any]] = {}
        self._service_task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None

    async def start(self) -> None:
        # Launch the dedicated media thread and return; readiness is best-effort.
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, name="media", daemon=True)
        self._thread.start()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._media_loop = loop
        self._poll_lock = None
        self._last_sig = None
        try:
            self._service_task = loop.create_task(self._amain())
            loop.run_until_complete(self._service_task)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.debug("media thread ended", exc_info=True)
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._media_loop = None
            self._mgr = None
            self._service_task = self._refresh_task = None
            loop.close()

    async def _amain(self) -> None:
        try:
            from winsdk.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as Mgr,
            )
            self._mgr = await _await_operation(Mgr.request_async(), _CONTROL_TIMEOUT_S)
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
        loop = self._media_loop
        if loop is not None and not loop.is_closed():
            def cancel_service() -> None:
                if self._service_task is not None:
                    self._service_task.cancel()
            try:
                loop.call_soon_threadsafe(cancel_service)
            except RuntimeError:
                pass
        if self._thread:
            await self._main_loop.run_in_executor(None, self._thread.join, 3.0)

    def _selected_sessions(self, current) -> dict[str, Any]:
        """One card per source: current first, otherwise prefer active playback."""
        selected = {}
        current_id = current.source_app_user_model_id if current is not None else None
        if current is not None:
            selected[current_id] = current
        for session in self._mgr.get_sessions():
            aumid = session.source_app_user_model_id or ""
            if aumid == current_id:
                continue
            old = selected.get(aumid)
            if old is None:
                selected[aumid] = session
                continue
            try:
                if (int(old.get_playback_info().playback_status) != 4
                        and int(session.get_playback_info().playback_status) == 4):
                    selected[aumid] = session
            except Exception:  # noqa: BLE001
                continue
        return selected

    def _find_session(self, app_id: str):
        if self._mgr is None:
            return None
        want = app_id or ""
        current = self._mgr.get_current_session()
        if want == "" and current is not None:
            return current
        # Match the same preference used by the media cards. Never redirect a
        # command for a closed source to some other app that happens to be current.
        return self._selected_sessions(current).get(want)

    async def _session_dict(self, s) -> Optional[dict[str, Any]]:
        aumid = s.source_app_user_model_id or ""
        try:
            props = await _await_operation(s.try_get_media_properties_async(),
                                           _PROPERTIES_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            return None
        title = (props.title or "").strip()
        artist = (props.artist or "").strip()
        info = s.get_playback_info()
        status = _STATUS.get(int(info.playback_status), "unknown")
        c = info.controls
        identity = (title, artist, getattr(props, "album_title", "") or "",
                    getattr(props, "album_artist", "") or "",
                    getattr(props, "track_number", 0))
        old_identity, thumb_key, next_read = self._art.get(aumid, (None, None, 0.0))
        now = time.monotonic()
        if identity != old_identity:
            thumb_key, next_read = None, 0.0
        if thumb_key is not None and not THUMBS.has(thumb_key):
            thumb_key, next_read = None, 0.0
        if now >= next_read:
            try:
                data = await asyncio.wait_for(self._read_thumb(props), _THUMB_TIMEOUT_S)
            except asyncio.TimeoutError:
                data = None
            if data:
                thumb_key = MediaThumbs.key_for_data(data)
                THUMBS.put(thumb_key, data)
            # Keep valid art through a transient missing reference, but retry
            # promptly while the browser is still publishing a new item's art.
            next_read = now + (_THUMB_REFRESH_S if data else self._interval)
        self._art[aumid] = (identity, thumb_key, next_read)
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
        stream = input_stream = reader = None
        try:
            from winsdk.windows.storage.streams import DataReader
            stream = await _await_operation(ref.open_read_async())
            size = int(stream.size)
            if not 0 < size <= _MAX_THUMB_BYTES:
                return None
            # Explicitly use IInputStream at offset zero; don't assume the WinRT
            # projection converts IRandomAccessStream to the correct interface.
            input_stream = stream.get_input_stream_at(0)
            reader = DataReader(input_stream)
            loaded = await _await_operation(reader.load_async(size))
            if loaded != size:
                return None
            buf = bytearray(size)
            reader.read_bytes(buf)
            return bytes(buf)
        except Exception:  # noqa: BLE001 - art is optional
            return None
        finally:
            # DataReader owns its input stream. Close resources even on timeout,
            # partial reads, and malformed/oversized references.
            for resource in (reader, input_stream, stream):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:  # noqa: BLE001
                        pass

    async def _poll_once(self) -> None:
        # Transport can request a quick refresh while the periodic poll is
        # awaiting WinRT; serialize those refreshes and their artwork cache writes.
        if self._poll_lock is None:
            self._poll_lock = asyncio.Lock()
        async with self._poll_lock:
            await self._poll_sessions()

    async def _poll_sessions(self) -> None:
        if self._mgr is None:
            return
        current = self._mgr.get_current_session()
        current_id = current.source_app_user_model_id if current else None
        sessions: list[dict[str, Any]] = []
        by_id: dict[str, dict[str, Any]] = {}
        for aumid, s in self._selected_sessions(current).items():
            try:
                d = await self._session_dict(s)
            except Exception:  # noqa: BLE001 - one bad source must not hide others
                log.debug("media source %s unavailable", aumid, exc_info=True)
                d = None
            if d is None:
                # Do not flicker a valid card away for a transient failed SMTC
                # read. Only retain it while its source is still enumerated.
                previous = self._last_sessions.get(aumid)
                d = dict(previous) if previous is not None else None
            if d and (d["title"] or d["status"] == "playing"):
                d["current"] = (d["id"] == current_id)
                by_id[d["id"]] = d
        sessions = list(by_id.values())
        self._last_sessions = by_id
        self._art = {key: value for key, value in self._art.items() if key in by_id}
        # Put the current session first so the UI leads with it.
        sessions.sort(key=lambda d: (not d.get("current"), d["app"].lower()))
        sig = repr([(d["id"], d["title"], d["artist"], d["status"], d["current"],
                     d["canPlay"], d["canPause"], d["canNext"], d["canPrev"],
                     d["thumbKey"]) for d in sessions])
        if sig != self._last_sig:
            self._last_sig = sig
            # AppState + its subscriber queues live on the main loop; hop back to it.
            self._main_loop.call_soon_threadsafe(self._state.set_media, sessions)

    # ---- transport --------------------------------------------------------
    async def control(self, action: str, app_id: str = "") -> bool:
        """Called on the main loop; runs the winsdk work on the media loop."""
        loop = self._media_loop
        if loop is None or loop.is_closed() or not loop.is_running() or self._stop.is_set():
            return False
        command = self._do_control(action, app_id)
        try:
            fut = asyncio.run_coroutine_threadsafe(command, loop)
        except RuntimeError:
            command.close()
            return False
        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), _CONTROL_TIMEOUT_S + 0.5)
        except Exception:  # noqa: BLE001
            return False

    async def _do_control(self, action: str, app_id: str) -> bool:
        s = self._find_session(app_id)
        if s is None:
            return False
        try:
            if action == "play_pause":
                operation = s.try_toggle_play_pause_async()
            elif action == "play":
                operation = s.try_play_async()
            elif action == "pause":
                operation = s.try_pause_async()
            elif action == "next":
                operation = s.try_skip_next_async()
            elif action == "previous":
                operation = s.try_skip_previous_async()
            else:
                return False
            if not await _await_operation(operation, _CONTROL_TIMEOUT_S):
                return False
        except Exception:  # noqa: BLE001
            log.debug("media control %s failed", action, exc_info=True)
            return False
        # A successful command must not wait for unrelated sources/optional art.
        # Coalesce quick refreshes on the media loop without delaying its reply.
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_after_control())
        return True

    async def _refresh_after_control(self) -> None:
        await asyncio.sleep(0.2)
        try:
            await self._poll_once()
        except Exception:  # noqa: BLE001
            pass

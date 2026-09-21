"""Controller: maps protocol commands to engine jobs and reflects results in state.

Bridges three worlds:
- the asyncio event loop (server + state live here),
- the AudioEngine's COM thread (all pycaw work happens there),
- and the wire protocol from the phone.

Engine jobs return concurrent.futures.Future; we await them with
asyncio.wrap_future so a slow audio call never blocks the event loop.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any, Callable

from .audio.engine import AudioEngine
from .log import get_logger
from .macros.bindings import AppInputBindings
from .macros.input import ComboError
from .macros.registry import MacroRegistry
from .server import Client
from .state import AppState

log = get_logger("controller")


class Controller:
    def __init__(self, state: AppState, engine: AudioEngine, loop: asyncio.AbstractEventLoop,
                 registry: MacroRegistry | None = None,
                 input_bindings: AppInputBindings | None = None,
                 media=None,
                 soundboard=None,
                 key_sender: Callable[[str], None] | None = None) -> None:
        self._state = state
        self._engine = engine
        self._loop = loop
        self._registry = registry
        self._bindings = input_bindings
        self._media = media  # MediaService | None
        self._soundboard = soundboard  # SoundboardService | None
        # Injectable so tests never fire real keystrokes into the focused window.
        if key_sender is not None:
            self._key_sender = key_sender
        else:
            from .macros.input import send_combo
            self._key_sender = send_combo

    def load_initial_macros(self) -> None:
        """Publish stored macros + app input bindings into state (call before serving)."""
        if self._registry is not None:
            self._state.set_macros(self._registry.list())
        if self._bindings is not None:
            self._state.set_app_bindings(self._bindings.list())
        self._publish_soundboard()

    def _publish_soundboard(self) -> None:
        if self._soundboard is not None:
            self._soundboard.ensure_started(
                self._state.devices.get("outputs", []), self._state.devices.get("inputs", []),
            )
            self._state.set_soundboard(self._soundboard.snapshot(
                self._state.devices.get("outputs", []), self._state.devices.get("inputs", []),
            ))

    # ---- engine poll -> state (called from the engine thread) -------------
    def make_on_poll(self):
        """Return a callback the engine invokes (on its thread) after each poll."""
        def _on_poll(data: dict[str, Any]) -> None:
            sessions = [asdict(s) for s in data["sessions"]]
            speaker = vars(data["speaker"])
            mic = vars(data["mic"])
            devices = data["devices"]
            outputs = [asdict(d) for d in devices.outputs]
            inputs = [asdict(d) for d in devices.inputs]
            meters = data.get("meters")
            # Marshal the state update onto the event loop thread.
            self._loop.call_soon_threadsafe(
                self._ingest_poll, sessions, speaker, mic, outputs, inputs, meters
            )
        return _on_poll

    def _ingest_poll(self, sessions, speaker, mic, outputs, inputs, meters) -> None:
        self._state.ingest_full(sessions, speaker, mic, outputs, inputs, meters)
        # Endpoint lists can change as a virtual cable is installed. Publish the
        # fresh choices with the next poll without requiring an agent restart.
        self._publish_soundboard()

    async def _call(self, fn) -> Any:
        return await asyncio.wrap_future(self._engine.submit(fn))

    # ---- command dispatch (the ControllerFn used by the server) -----------
    async def handle(self, client: Client, msg: dict[str, Any]) -> None:
        t = msg.get("t")
        try:
            if t == "set_volume":
                await self._set_volume(msg)
            elif t == "set_mute":
                await self._set_mute(msg)
            elif t == "set_default_output":
                await self._set_default_device(msg, "output")
            elif t == "set_default_input":
                await self._set_default_device(msg, "input")
            elif t == "ping":
                await client.send({"t": "pong"})
            elif t == "macro":
                await self._run_macro(client, msg)
            elif t == "add_macro":
                await self._add_macro(client, msg)
            elif t == "remove_macro":
                await self._remove_macro(client, msg)
            elif t == "app_input_mute":
                await self._app_input_mute(client, msg)
            elif t == "set_app_input_binding":
                await self._set_app_input_binding(client, msg)
            elif t == "clear_app_input_binding":
                await self._clear_app_input_binding(client, msg)
            elif t == "media_control":
                await self._media_control(client, msg)
            elif t == "soundboard_play":
                await self._soundboard_play(client, msg)
            elif t == "soundboard_stop_all":
                await self._soundboard_stop_all(client)
            elif t == "soundboard_config":
                await self._soundboard_config(client, msg)
            elif t == "soundboard_update_clip":
                await self._soundboard_update_clip(client, msg)
            elif t == "soundboard_remove_clip":
                await self._soundboard_remove_clip(client, msg)
            elif t == "soundboard_restore_defaults":
                await self._soundboard_restore_defaults(client)
            else:
                await client.send({"t": "error", "code": "unimpl", "msg": f"no handler for {t!r}"})
        except Exception as exc:  # noqa: BLE001 - report, never crash the socket
            log.exception("command %s failed", t)
            await client.send({"t": "error", "code": "cmdfail", "msg": str(exc)})

    async def _set_volume(self, msg: dict[str, Any]) -> None:
        target = msg.get("target") or {}
        kind = target.get("kind")
        level = float(msg.get("level", 0.0))
        if kind == "session":
            sid = target["id"]
            await self._call(lambda b: b.set_session_volume(sid, level))
            self._state.apply_session(sid, level=level)
        elif kind in ("speaker", "mic"):
            await self._call(lambda b: b.set_master_volume(kind, level))
            self._state.apply_master(kind, level=level)

    async def _set_mute(self, msg: dict[str, Any]) -> None:
        target = msg.get("target") or {}
        kind = target.get("kind")
        muted = bool(msg.get("muted"))
        if kind == "session":
            sid = target["id"]
            await self._call(lambda b: b.set_session_mute(sid, muted))
            self._state.apply_session(sid, muted=muted)
        elif kind in ("speaker", "mic"):
            await self._call(lambda b: b.set_master_mute(kind, muted))
            self._state.apply_master(kind, muted=muted)

    # ---- macros -----------------------------------------------------------
    async def _run_macro(self, client: Client, msg: dict[str, Any]) -> None:
        if self._registry is None:
            await client.send({"t": "error", "code": "nomacro", "msg": "macros disabled"})
            return
        macro_id = msg.get("macroId")
        macro = self._registry.get(macro_id)
        if macro is None:
            await client.send({"t": "error", "code": "nomacro",
                               "msg": f"unknown macro {macro_id!r}"})
            return
        # Injection is quick but off-loop to keep the event loop responsive.
        await self._loop.run_in_executor(None, self._key_sender, macro["keys"])
        log.info("macro fired: %s (%s)", macro["label"], macro["keys"])
        await client.send({"t": "macro_ok", "macroId": macro_id})

    async def _add_macro(self, client: Client, msg: dict[str, Any]) -> None:
        if self._registry is None:
            await client.send({"t": "error", "code": "nomacro", "msg": "macros disabled"})
            return
        try:
            self._registry.add(str(msg.get("label", "")), str(msg.get("keys", "")))
        except ComboError as exc:
            await client.send({"t": "error", "code": "badcombo", "msg": str(exc)})
            return
        self._state.set_macros(self._registry.list())  # broadcasts a snapshot

    async def _remove_macro(self, client: Client, msg: dict[str, Any]) -> None:
        if self._registry is None:
            await client.send({"t": "error", "code": "nomacro", "msg": "macros disabled"})
            return
        self._registry.remove(str(msg.get("id", "")))
        self._state.set_macros(self._registry.list())

    # ---- per-app input mute (macro-backed) --------------------------------
    async def _app_input_mute(self, client: Client, msg: dict[str, Any]) -> None:
        """Fire the key combo bound to an app's own mic-mute/PTT hotkey.

        The OS can't mute a single app's microphone, so we inject the hotkey the
        app itself listens for. We can't read the result back, so the client tracks
        the toggle optimistically; we only confirm the keystroke was sent.
        """
        if self._bindings is None:
            await client.send({"t": "error", "code": "nobinding", "msg": "input bindings disabled"})
            return
        app_id = msg.get("appId")
        binding = self._bindings.get(str(app_id)) if app_id else None
        if binding is None:
            await client.send({"t": "error", "code": "nobinding",
                               "msg": f"no input hotkey bound for {app_id!r}"})
            return
        await self._loop.run_in_executor(None, self._key_sender, binding["keys"])
        log.info("app input hotkey fired: %s (%s)", app_id, binding["keys"])
        await client.send({"t": "app_input_ok", "appId": app_id})

    async def _set_app_input_binding(self, client: Client, msg: dict[str, Any]) -> None:
        if self._bindings is None:
            await client.send({"t": "error", "code": "nobinding", "msg": "input bindings disabled"})
            return
        app_id = str(msg.get("appId", ""))
        if not app_id:
            await client.send({"t": "error", "code": "badbinding", "msg": "missing appId"})
            return
        try:
            self._bindings.set(app_id, str(msg.get("keys", "")), str(msg.get("label", "")))
        except ComboError as exc:
            await client.send({"t": "error", "code": "badcombo", "msg": str(exc)})
            return
        self._state.set_app_bindings(self._bindings.list())  # broadcasts a snapshot

    async def _clear_app_input_binding(self, client: Client, msg: dict[str, Any]) -> None:
        if self._bindings is None:
            await client.send({"t": "error", "code": "nobinding", "msg": "input bindings disabled"})
            return
        self._bindings.remove(str(msg.get("appId", "")))
        self._state.set_app_bindings(self._bindings.list())

    # ---- now-playing media (SMTC) -----------------------------------------
    async def _media_control(self, client: Client, msg: dict[str, Any]) -> None:
        if self._media is None:
            await client.send({"t": "error", "code": "nomedia", "msg": "media unavailable"})
            return
        action = str(msg.get("action", ""))
        app_id = str(msg.get("id", ""))
        ok = await self._media.control(action, app_id)
        if not ok:
            await client.send({"t": "error", "code": "mediafail",
                               "msg": f"could not {action or 'control'} media"})

    # ---- soundboard (Configuration B) -----------------------------------
    async def _soundboard_play(self, client: Client, msg: dict[str, Any]) -> None:
        if self._soundboard is None:
            await client.send({"t": "error", "code": "nosoundboard", "msg": "soundboard unavailable"})
            return
        self._soundboard.play(str(msg.get("clipId", "")))
        self._publish_soundboard()

    async def _soundboard_stop_all(self, client: Client) -> None:
        if self._soundboard is None:
            await client.send({"t": "error", "code": "nosoundboard", "msg": "soundboard unavailable"})
            return
        self._soundboard.stop_all()
        self._publish_soundboard()

    async def _soundboard_config(self, client: Client, msg: dict[str, Any]) -> None:
        if self._soundboard is None:
            await client.send({"t": "error", "code": "nosoundboard", "msg": "soundboard unavailable"})
            return
        self._soundboard.configure(msg.get("config") or {}, self._state.devices.get("outputs", []),
                                   self._state.devices.get("inputs", []))
        self._publish_soundboard()

    async def _soundboard_update_clip(self, client: Client, msg: dict[str, Any]) -> None:
        if self._soundboard is None:
            await client.send({"t": "error", "code": "nosoundboard", "msg": "soundboard unavailable"})
            return
        changes = msg.get("changes")
        if not isinstance(changes, dict):
            raise ValueError("missing soundboard changes")
        self._soundboard.update_clip(str(msg.get("clipId", "")), **changes)
        self._publish_soundboard()

    async def _soundboard_remove_clip(self, client: Client, msg: dict[str, Any]) -> None:
        if self._soundboard is None:
            await client.send({"t": "error", "code": "nosoundboard", "msg": "soundboard unavailable"})
            return
        if not self._soundboard.remove_clip(str(msg.get("clipId", ""))):
            raise ValueError("unknown soundboard clip")
        self._publish_soundboard()

    async def _soundboard_restore_defaults(self, client: Client) -> None:
        if self._soundboard is None:
            await client.send({"t": "error", "code": "nosoundboard", "msg": "soundboard unavailable"})
            return
        self._soundboard.restore_defaults(reset=True)
        self._publish_soundboard()

    async def _set_default_device(self, msg: dict[str, Any], flow: str) -> None:
        device_id = msg["deviceId"]
        if flow == "input":
            await self._call(lambda b: b.set_default_input(device_id))
        else:
            await self._call(lambda b: b.set_default_output(device_id))
        # Refresh device list so isDefault flags update immediately.
        devices = await self._call(lambda b: b.list_devices())
        from dataclasses import asdict as _asdict
        self._state.set_devices_lists(
            [_asdict(d) for d in devices.outputs],
            [_asdict(d) for d in devices.inputs],
        )

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
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import time
import math
from typing import Any, Callable

from .audio.engine import AudioEngine, EngineBusy, ObservationUncertain
from .log import get_logger
from .macros.bindings import AppInputBindings
from .macros.input import ComboError
from .macros.registry import MacroRegistry
from .server import Client
from .state import AppState
from .presentation import RevisionConflict
from .commands import ApplicationCommands, CommandFailure

log = get_logger("controller")


class Controller:
    def __init__(self, state: AppState, engine: AudioEngine, loop: asyncio.AbstractEventLoop,
                 registry: MacroRegistry | None = None,
                 input_bindings: AppInputBindings | None = None,
                 media=None,
                 soundboard=None,
                 presentation=None,
                 key_sender: Callable[[str], None] | None = None) -> None:
        self._state = state
        self._engine = engine
        self._loop = loop
        self._registry = registry
        self._bindings = input_bindings
        self._media = media  # MediaService | None
        self._soundboard = soundboard  # SoundboardService | None
        self._presentation = presentation
        self._soundboard_lock = asyncio.Lock()
        self._soundboard_refresh: asyncio.Task | None = None
        self._closing = False
        self._commands = ApplicationCommands(self._execute, state.server_epoch)
        self._worker_pools = {domain: ThreadPoolExecutor(max_workers=1, thread_name_prefix="deckster-" + domain)
                              for domain in ("soundboard", "stop", "settings", "input")}
        self._inline_owner_seq = 0
        self._next_soundboard_refresh = 0.0
        self._soundboard_devices = None
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
        if self._soundboard is not None:
            self._state.set_soundboard(self._soundboard.snapshot(
                self._state.devices.get("outputs", []), self._state.devices.get("inputs", [])))
        if self._presentation is not None:
            self._state.set_presentation(self._presentation.snapshot())

    async def _worker_call(self, fn, *args, domain="soundboard", **kwargs):
        """Cancellation cannot stop a native worker; wait for it before cleanup."""
        from functools import partial
        job = self._loop.run_in_executor(self._worker_pools[domain], partial(fn, *args, **kwargs))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(job)
            except asyncio.CancelledError:
                if job.cancelled():
                    raise asyncio.CancelledError
                cancelled = True
                continue
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError
                raise
            if cancelled:
                raise asyncio.CancelledError
            return result

    async def _soundboard_call(self, fn, *args, **kwargs):
        """Serialise slow device/library work with recovery, away from the UI loop."""
        async with self._soundboard_lock:
            return await self._worker_call(fn, *args, **kwargs)

    async def _publish_soundboard(self) -> None:
        if self._soundboard is not None:
            outputs = deepcopy(self._state.devices.get("outputs", []))
            inputs = deepcopy(self._state.devices.get("inputs", []))
            def refresh():
                self._soundboard.ensure_started(outputs, inputs)
                return self._soundboard.snapshot(outputs, inputs)
            self._state.set_soundboard(await self._soundboard_call(refresh))

    async def _refresh_soundboard(self) -> None:
        try:
            await self._publish_soundboard()
        except Exception:
            log.exception("soundboard refresh failed")

    async def close(self, timeout_s: float = 5.0) -> bool:
        """Finish any recovery worker before closing the audio service/loop."""
        self._closing = True
        if not await self._commands.close(timeout_s):
            log.error("command shutdown deadline exceeded; ownership retained: %s", self.health())
            return False
        if self._soundboard_refresh is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._soundboard_refresh), timeout_s)
            except TimeoutError:
                log.error("audio recovery shutdown deadline exceeded; ownership retained")
                return False
        for pool in self._worker_pools.values():
            pool.shutdown(wait=True, cancel_futures=True)
        return True

    def health(self) -> dict:
        return self._commands.health()

    async def admit(self, client: Client, msg: dict[str, Any]) -> asyncio.Future:
        if msg.get("t") in {"soundboard_import", "soundboard_check_formats"} and not getattr(client, "is_desktop", False):
            raise ValueError("This command requires the local desktop workspace")
        if msg.get("t") == "ping":
            await client.send({"t": "pong"})
            future = self._loop.create_future()
            future.set_result({"status": "applied"})
            return future
        return await self._commands.admit(client, msg)

    async def detach_client(self, client: Client) -> None:
        await self._commands.detach(client)

    async def desktop_command(self, body: dict) -> dict:
        """Adapt immutable workspace input to exactly the phone command path."""
        body = deepcopy(body)
        action = body.get("action")
        commands = {
            "presentation": {"t": "presentation_update", "changes": body.get("changes", {}), "baseRevision": body.get("baseRevision")},
            "defaults": {"t": "presentation_defaults", "baseRevision": body.get("baseRevision")},
            "remove_clip": {"t": "soundboard_remove_clip", "clipId": body.get("id", "")},
            "starter_sounds": {"t": "soundboard_restore_defaults"},
            "audition": {"t": "soundboard_audition", "clipId": body.get("id", "")},
            "stop": {"t": "soundboard_stop_all"},
            "clip": {"t": "soundboard_update_clip", "clipId": body.get("id", ""), "changes": body.get("changes", {})},
            "test": {"t": "soundboard_test", "bus": body.get("bus", "")},
            "verify_receiver": {"t": "soundboard_verify_receiver"},
            "import": {"t": "soundboard_import", "source": body.get("source"), "label": body.get("label", "")},
            "check_formats": {"t": "soundboard_check_formats", "config": body.get("config")},
        }
        if action == "control":
            command = body.get("command", {})
            if not isinstance(command, dict) or command.get("t") not in {
                    "set_volume", "set_mute", "set_default_output", "set_default_input", "media_control",
                    "app_input_mute", "set_app_input_binding", "clear_app_input_binding"}:
                raise ValueError("Use advanced controls for this action.")
        elif action == "use_mixer_input":
            from .routing import routing_status
            status = routing_status(self._state.soundboard)
            if not status["ready"] or not status["receiverId"]:
                raise ValueError("Connect a working mixer route before changing the Windows input.")
            command = {"t": "set_default_input", "deviceId": status["receiverId"]}
        elif action in {"routing", "recommended_routing"}:
            from .routing import recommend_route, route_issue
            sound = deepcopy(self._state.soundboard)
            config = recommend_route(sound) if action == "recommended_routing" else body.get("config", {})
            issue = route_issue(config, sound)
            if issue:
                raise ValueError(issue)
            command = {"t": "soundboard_config", "config": config}
        else:
            command = commands.get(action)
            if command is None:
                raise ValueError("Unknown workspace command")
        for name in ("commandId", "clientSeq"):
            if name in body and name not in command:
                command[name] = body[name]
        class Reply:
            device_id = "local-desktop"
            is_desktop = True
            async def send(self, message):
                pass
        result = await self.handle(Reply(), command)
        if result["status"] == "failed":
            error = result["error"]
            if error["code"] == "presentation_conflict":
                exception = RevisionConflict(error["msg"])
            else:
                exception = ValueError(error["msg"])
            exception.command_result = result
            raise exception
        if result["status"] != "applied":
            return {"ok": False, "commandResult": result}
        value = result.get("value")
        if value is None:
            value = deepcopy(self._state.soundboard) if command["t"] in {
                "soundboard_config", "soundboard_update_clip", "soundboard_remove_clip", "soundboard_restore_defaults"} else {"ok": True}
        return {**value, "commandResult": result}

    # ---- engine poll -> state (called from the engine thread) -------------
    def make_on_poll(self):
        """Return a callback the engine invokes (on its thread) after each poll."""
        def _on_poll(data: dict[str, Any]) -> None:
            sessions = [asdict(s) for s in data["sessions"]]
            speaker = asdict(data["speaker"])
            mic = asdict(data["mic"])
            devices = data["devices"]
            outputs = [asdict(d) for d in devices.outputs]
            inputs = [asdict(d) for d in devices.inputs]
            meters = data.get("meters")
            # Marshal the state update onto the event loop thread.
            self._loop.call_soon_threadsafe(
                self._ingest_poll, sessions, speaker, mic, outputs, inputs, meters, data.get("ownerSeq")
            )
        return _on_poll

    def _ingest_poll(self, sessions, speaker, mic, outputs, inputs, meters, owner_seq=None) -> None:
        self._state.ingest_full(sessions, speaker, mic, outputs, inputs, meters, owner_seq)
        # Endpoint lists can change as a virtual cable is installed. Publish the
        # fresh choices with the next poll without requiring an agent restart.
        now = time.monotonic()
        devices = outputs, inputs
        changed = devices != self._soundboard_devices
        if (self._soundboard is not None and not self._closing and (changed or now >= self._next_soundboard_refresh) and
                (self._soundboard_refresh is None or self._soundboard_refresh.done())):
            self._soundboard_devices = deepcopy(devices)
            self._next_soundboard_refresh = now + 1.0
            self._soundboard_refresh = self._loop.create_task(self._refresh_soundboard())

    async def _call(self, fn) -> Any:
        return await asyncio.wrap_future(self._engine.submit(fn))

    # ---- command dispatch (the ControllerFn used by the server) -----------
    async def handle(self, client: Client, msg: dict[str, Any]) -> None:
        return await asyncio.shield(await self.admit(client, msg))

    async def _execute(self, client: Client, msg: dict[str, Any]) -> dict | None:
        commands = self._commands
        class CommandReply:
            async def send(self, message):
                if message.get("t") == "error":
                    raise CommandFailure(message.get("msg", "Command failed"), message.get("code", "cmdfail"),
                                         **{k: v for k, v in message.items() if k not in {"t", "code", "msg"}})
                await commands._send(client, message)
        reply = CommandReply()
        t = msg.get("t")
        try:
            if t == "set_volume":
                return await self._set_volume(msg)
            elif t == "set_mute":
                return await self._set_mute(msg)
            elif t == "set_default_output":
                await self._set_default_device(msg, "output")
            elif t == "set_default_input":
                await self._set_default_device(msg, "input")
            elif t == "ping":
                await client.send({"t": "pong"})
            elif t == "macro":
                await self._run_macro(reply, msg)
            elif t == "add_macro":
                await self._add_macro(reply, msg)
            elif t == "remove_macro":
                await self._remove_macro(reply, msg)
            elif t == "app_input_mute":
                await self._app_input_mute(reply, msg)
            elif t == "set_app_input_binding":
                await self._set_app_input_binding(reply, msg)
            elif t == "clear_app_input_binding":
                await self._clear_app_input_binding(reply, msg)
            elif t == "media_control":
                await self._media_control(reply, msg)
            elif t == "soundboard_play":
                return await self._soundboard_play(reply, msg)
            elif t == "soundboard_stop_all":
                await self._soundboard_stop_all(reply)
            elif t == "soundboard_config":
                await self._soundboard_config(reply, msg)
            elif t == "soundboard_update_clip":
                await self._soundboard_update_clip(reply, msg)
            elif t == "soundboard_remove_clip":
                await self._soundboard_remove_clip(reply, msg)
            elif t == "soundboard_restore_defaults":
                await self._soundboard_restore_defaults(reply)
            elif t == "soundboard_import":
                if self._soundboard is None:
                    raise ValueError("soundboard unavailable")
                result = await self._soundboard_call(self._soundboard.import_clip, msg["source"], msg["label"])
                await self._publish_soundboard()
                return {"value": result}
            elif t in {"soundboard_audition", "soundboard_test", "soundboard_verify_receiver"}:
                if self._soundboard is None:
                    raise ValueError("soundboard unavailable")
                outputs, inputs = deepcopy(self._state.devices.get("outputs", [])), deepcopy(self._state.devices.get("inputs", []))
                if t == "soundboard_audition":
                    await self._soundboard_call(self._soundboard.audition, msg.get("clipId", ""), outputs)
                    return {"value": {"ok": True}}
                if t == "soundboard_test":
                    await self._soundboard_call(self._soundboard.test_tone, msg.get("bus", ""))
                    return {"value": {"ok": True}}
                return {"value": await self._soundboard_call(self._soundboard.verify_receiver, outputs, inputs)}
            elif t == "soundboard_check_formats":
                from .soundboard import SoundboardRenderer
                from .routing import cable_pairs
                sound = deepcopy(self._state.soundboard)
                config = msg.get("config") or sound.get("config", {})
                if not isinstance(config, dict):
                    raise ValueError("Invalid audio route")
                endpoints = []
                for key, label, kind in (("inputId", "Microphone", "input"), ("voiceOutputId", "Mixer output", "output"), ("earsOutputId", "Headphones / speakers", "output")):
                    device = next((d for d in sound.get("inputs" if kind == "input" else "outputs", []) if d["id"] == config.get(key)), None)
                    if device:
                        endpoints.append((label, device["name"], kind))
                receiver = next((p[1] for p in cable_pairs(sound.get("outputs", []), sound.get("inputs", [])) if p[0]["id"] == config.get("voiceOutputId")), None)
                if receiver:
                    endpoints.append(("Receiving microphone", receiver["name"], "input"))
                if not endpoints:
                    raise ValueError("Connect your audio devices before checking sample rates.")
                return {"value": await self._soundboard_call(SoundboardRenderer().check_formats, endpoints)}
            elif t == "presentation_defaults":
                result = await self._worker_call(self._presentation.restore_defaults, msg.get("baseRevision"), domain="settings")
                self._state.set_presentation(result)
                return {"value": result}
            elif t == "presentation_update":
                if self._presentation is None:
                    raise ValueError("presentation unavailable")
                result = await self._worker_call(self._presentation.update,
                                                msg.get("changes"), msg.get("baseRevision"), domain="settings")
                self._state.set_presentation(result)
                return {"value": result}
            else:
                raise CommandFailure(f"no handler for {t!r}", "unimpl")
        except RevisionConflict as exc:
            raise CommandFailure(str(exc), "presentation_conflict", presentation=self._presentation.snapshot()) from exc
        except ObservationUncertain as exc:
            raise CommandFailure(str(exc), "outcome_unknown", status="outcome_unknown") from exc
        except EngineBusy as exc:
            raise CommandFailure(str(exc), "busy") from exc
        except Exception as exc:  # noqa: BLE001 - report, never crash the socket
            log.exception("command %s failed", t)
            raise

    async def _observe_write(self, target, write) -> dict:
        if hasattr(self._engine, "submit_observation"):
            result = await asyncio.wrap_future(self._engine.submit_observation(target, write))
        else:
            def observe(backend):
                write(backend)
                if target["kind"] == "session":
                    observation = next((asdict(s) for s in backend.snapshot_sessions() if s.id == target["id"]), None)
                    if observation is None:
                        raise ValueError("audio session is no longer available")
                else:
                    observation = asdict(backend.get_master(target["kind"]))
                self._inline_owner_seq += 1
                return {"target": target, "observation": observation, "ownerSeq": self._inline_owner_seq}
            result = await self._call(observe)
        observation = result["observation"]
        if target["kind"] == "session":
            self._state.apply_session(target["id"], level=observation["level"], muted=observation["muted"],
                                      active=observation.get("active"), owner_seq=result["ownerSeq"])
        else:
            self._state.apply_master(target["kind"], level=observation["level"], muted=observation["muted"],
                                     owner_seq=result["ownerSeq"])
        return result

    async def _set_volume(self, msg: dict[str, Any]) -> dict:
        target = msg.get("target") or {}
        kind = target.get("kind")
        level = float(msg.get("level", 0.0))
        if not math.isfinite(level):
            raise ValueError("Volume must be a finite number")
        if kind == "session":
            sid = target["id"]
            return await self._observe_write(target, lambda b: b.set_session_volume(sid, level))
        elif kind in ("speaker", "mic"):
            return await self._observe_write(target, lambda b: b.set_master_volume(kind, level))
        raise ValueError("Unknown volume target")

    async def _set_mute(self, msg: dict[str, Any]) -> dict:
        target = msg.get("target") or {}
        kind = target.get("kind")
        muted = bool(msg.get("muted"))
        if kind == "session":
            sid = target["id"]
            return await self._observe_write(target, lambda b: b.set_session_mute(sid, muted))
        elif kind in ("speaker", "mic"):
            return await self._observe_write(target, lambda b: b.set_master_mute(kind, muted))
        raise ValueError("Unknown mute target")

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
        await self._worker_call(self._key_sender, macro["keys"], domain="input")
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
        await self._worker_call(self._key_sender, binding["keys"], domain="input")
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
        # Reserve before waiting for recovery/the worker lock. Stop may finish
        # while this command is waiting; the service must reject that older play
        # even when its decode has not started yet.
        generation = getattr(self._soundboard, "play_generation", None)
        options = {"expected_generation": generation} if generation is not None else {}
        trace_id = await self._soundboard_call(self._soundboard.play, str(msg.get("clipId", "")), **options)
        await self._publish_soundboard()
        return {"traceId": trace_id} if trace_id is not None else None

    async def _soundboard_stop_all(self, client: Client) -> None:
        if self._soundboard is None:
            await client.send({"t": "error", "code": "nosoundboard", "msg": "soundboard unavailable"})
            return
        # Stop has priority over slow clip decoding. The service invalidates
        # pending preparations so they cannot start a sound after this returns.
        await self._worker_call(self._soundboard.stop_all, domain="stop")
        self._state.set_soundboard(await self._worker_call(
            self._soundboard.snapshot, self._state.devices.get("outputs", []),
            self._state.devices.get("inputs", []), domain="stop"))

    async def _soundboard_config(self, client: Client, msg: dict[str, Any]) -> None:
        if self._soundboard is None:
            await client.send({"t": "error", "code": "nosoundboard", "msg": "soundboard unavailable"})
            return
        await self._soundboard_call(self._soundboard.configure, msg.get("config") or {},
                                    deepcopy(self._state.devices.get("outputs", [])),
                                    deepcopy(self._state.devices.get("inputs", [])))
        await self._publish_soundboard()

    async def _soundboard_update_clip(self, client: Client, msg: dict[str, Any]) -> None:
        if self._soundboard is None:
            await client.send({"t": "error", "code": "nosoundboard", "msg": "soundboard unavailable"})
            return
        changes = msg.get("changes")
        if not isinstance(changes, dict):
            raise ValueError("missing soundboard changes")
        await self._soundboard_call(self._soundboard.update_clip, str(msg.get("clipId", "")), **changes)
        await self._publish_soundboard()

    async def _soundboard_remove_clip(self, client: Client, msg: dict[str, Any]) -> None:
        if self._soundboard is None:
            await client.send({"t": "error", "code": "nosoundboard", "msg": "soundboard unavailable"})
            return
        if not await self._soundboard_call(self._soundboard.remove_clip, str(msg.get("clipId", ""))):
            raise ValueError("unknown soundboard clip")
        if self._presentation is not None:
            changed = await self._worker_call(self._presentation.remove_clip, str(msg.get("clipId", "")), domain="settings")
            if changed is not None:
                self._state.set_presentation(changed)
        await self._publish_soundboard()

    async def _soundboard_restore_defaults(self, client: Client) -> None:
        if self._soundboard is None:
            await client.send({"t": "error", "code": "nosoundboard", "msg": "soundboard unavailable"})
            return
        await self._soundboard_call(self._soundboard.restore_defaults, reset=True)
        await self._publish_soundboard()

    async def _set_default_device(self, msg: dict[str, Any], flow: str) -> None:
        device_id = msg["deviceId"]
        write = (lambda b: b.set_default_input(device_id)) if flow == "input" else (lambda b: b.set_default_output(device_id))
        if hasattr(self._engine, "submit_device_change"):
            result = await asyncio.wrap_future(self._engine.submit_device_change(write))
            self._state.set_devices_lists(result["outputs"], result["inputs"], result["ownerSeq"])
            return
        await self._call(write)
        # Refresh device list so isDefault flags update immediately.
        devices = await self._call(lambda b: b.list_devices())
        from dataclasses import asdict as _asdict
        self._state.set_devices_lists(
            [_asdict(d) for d in devices.outputs],
            [_asdict(d) for d in devices.inputs],
        )

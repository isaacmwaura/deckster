"""Application-wide bounded command ownership, shared by every transport.

Each domain has one consumer. Native work in one domain cannot block admission
or execution in another. Futures belong to the application, not the socket: a
disconnect removes pending work but cannot pretend an already running write was
undone. Identified outcomes are retained in a bounded, epoch-local history.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field
import time
import json
from os import PathLike
from typing import Any, Awaitable, Callable


class CommandBusy(ValueError):
    pass


class CommandFailure(ValueError):
    def __init__(self, message: str, code: str = "cmdfail", *, status="failed", **details) -> None:
        super().__init__(message)
        self.code, self.details = code, details
        self.status = status


_PLAYBACK_COMMANDS = frozenset({"soundboard_play", "soundboard_audition",
                                "soundboard_test", "soundboard_verify_receiver"})


@dataclass
class _Command:
    client: Any
    message: dict
    future: asyncio.Future
    key: tuple | None
    admitted_at: float
    wait_for: asyncio.Future | None = None
    play_generation: int = 0
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    listeners: list[Any] = field(default_factory=list)


def command_domain(message: dict) -> str:
    kind = message.get("t", "")
    if kind in {"set_volume", "set_mute", "set_default_output", "set_default_input"}:
        return "audio"
    if kind == "media_control":
        return "media"
    if kind in {"macro", "app_input_mute"}:
        return "input"
    if kind == "soundboard_stop_all":
        return "stop"
    if isinstance(kind, str) and kind.startswith("soundboard_"):
        return "soundboard"
    return "settings"


def volume_target(message: dict) -> tuple | None:
    if message.get("t") != "set_volume":
        return None
    target = message.get("target")
    if not isinstance(target, dict):
        return None
    if target.get("kind") in {"mic", "speaker"}:
        return target["kind"], ""
    if target.get("kind") == "session" and isinstance(target.get("id"), str):
        return "session", target["id"]
    return None


class ApplicationCommands:
    def __init__(self, execute: Callable[[Any, dict], Awaitable[dict | None]], epoch: str,
                 *, lane_limit: int = 32, global_limit: int = 96,
                 history_limit: int = 256, max_command_bytes: int = 64 * 1024) -> None:
        self._execute, self.epoch = execute, epoch
        self._lane_limit, self._global_limit = lane_limit, global_limit
        self._history_limit = history_limit
        self._max_command_bytes = max_command_bytes
        self._pending: dict[str, list[_Command]] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._running: dict[str, _Command] = {}
        self._history: OrderedDict[tuple, dict] = OrderedDict()
        self._requests: dict[tuple, dict] = {}
        self._inflight: dict[tuple, _Command] = {}
        self._stop_barrier: asyncio.Future | None = None
        self._closing = False
        self._play_generation = 0

    @staticmethod
    def _identity(client, message):
        command_id = message.get("commandId")
        if command_id is None:
            return None
        if not isinstance(command_id, str) or not 1 <= len(command_id) <= 128:
            raise ValueError("commandId must be a string of 1 to 128 characters")
        sequence = message.get("clientSeq")
        if type(sequence) is not int or sequence < 0 or sequence > 2**53 - 1:
            raise ValueError("clientSeq must be a nonnegative safe integer")
        # Paired clients retain identity over reconnect; development clients and
        # desktop requests have an explicitly scoped local identity.
        return getattr(client, "device_id", None) or client, command_id

    def _result(self, command: _Command, status: str, **fields) -> dict:
        return {"t": "command_result", "commandId": command.message.get("commandId"),
                "clientSeq": command.message.get("clientSeq"), "serverEpoch": self.epoch,
                "status": status, **fields}

    async def _send(self, client, message) -> None:
        try:
            await asyncio.wait_for(client.send(message), timeout=2.0)
        except (ConnectionResetError, RuntimeError, TimeoutError):
            pass  # A completed application job remains known after disconnect.

    async def admit(self, client, message: dict) -> asyncio.Future:
        def encode_internal(value):
            if isinstance(value, PathLike):
                return str(value)
            raise TypeError("Command contains an unsupported value")
        try:
            encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"),
                                 allow_nan=False, default=encode_internal)
        except (TypeError, ValueError) as exc:
            raise ValueError("Command must contain finite JSON values") from exc
        if len(encoded.encode("utf-8")) > self._max_command_bytes:
            raise ValueError("Command exceeds the 64 KiB application payload budget")
        message = deepcopy(message)  # Worker inputs cannot follow mutable drafts.
        if message.get("serverEpoch", self.epoch) != self.epoch:
            raise ValueError("Server epoch changed; obtain a current snapshot before sending commands")
        key = self._identity(client, message)
        if key is not None and key in self._history:
            if message != self._requests[key]:
                raise ValueError("commandId was already used for a different command")
            result = deepcopy(self._history[key])
            await self._send(client, result)
            future = asyncio.get_running_loop().create_future()
            future.set_result(result)
            return future
        if key is not None and key in self._inflight:
            previous = self._inflight[key]
            if message != previous.message:
                raise ValueError("commandId was already used for a different command")
            if client is not previous.client and not any(client is listener for listener in previous.listeners):
                previous.listeners.append(client)
                previous.listeners = previous.listeners[-4:]
            await self._send(client, self._result(previous, "accepted"))
            return previous.future
        domain = command_domain(message)
        queue = self._pending.setdefault(domain, [])
        target = volume_target(message)
        replacements = [item for item in queue if target is not None and
                        volume_target(item.message) == target]
        depth = sum(len(items) for items in self._pending.values()) + len(self._running)
        if self._closing or len(queue) - len(replacements) >= self._lane_limit or \
                depth - len(replacements) >= self._global_limit:
            raise CommandBusy("Too many pending commands. Try again.")
        for item in replacements:
            queue.remove(item)
            await self._finish(item, "superseded")
        future = asyncio.get_running_loop().create_future()
        command = _Command(client, message, future, key, time.monotonic(), play_generation=self._play_generation)
        if key is not None:
            self._inflight[key] = command
        if domain == "stop":
            self._play_generation += 1
            for item in list(self._pending.get("soundboard", [])):
                if item.message.get("t") in _PLAYBACK_COMMANDS:
                    self._pending["soundboard"].remove(item)
                    await self._finish(item, "superseded")
            self._stop_barrier = future
        elif domain == "soundboard":
            command.wait_for = self._stop_barrier
        queue.append(command)
        if key is not None:
            await self._send(client, self._result(command, "accepted"))
        event = self._events.setdefault(domain, asyncio.Event())
        event.set()
        if domain not in self._workers:
            self._workers[domain] = asyncio.create_task(self._run(domain), name="commands-" + domain)
        return future

    async def _finish(self, command, status, **fields):
        result = self._result(command, status, **fields)
        if command.key is not None:
            self._inflight.pop(command.key, None)
            self._history[command.key] = deepcopy(result)
            self._requests[command.key] = command.message
            self._history.move_to_end(command.key)
            while len(self._history) > self._history_limit:
                expired_key, _ = self._history.popitem(last=False)
                self._requests.pop(expired_key, None)
            await asyncio.gather(*(self._send(client, result) for client in [command.client, *command.listeners]))
        if not command.future.done():
            command.future.set_result(result)

    async def _run(self, domain):
        queue, event = self._pending[domain], self._events[domain]
        while True:
            await event.wait()
            if not queue:
                event.clear()
                continue
            command = queue.pop(0)
            if not queue:
                event.clear()
            self._running[domain] = command
            try:
                if command.wait_for is not None:
                    await asyncio.shield(command.wait_for)
                if command.message.get("t") in _PLAYBACK_COMMANDS and command.play_generation != self._play_generation:
                    await self._finish(command, "superseded")
                    continue
                result = await self._execute(command.client, command.message)
            except asyncio.CancelledError:
                await self._finish(command, "outcome_unknown", reason="owner_cancelled")
                if self._closing:
                    raise
            except CommandFailure as exc:
                error = {"code": exc.code, "msg": str(exc), **exc.details}
                await self._send(command.client, {"t": "error", **error})
                await self._finish(command, exc.status, error=error)
            except Exception as exc:
                error = {"code": "cmdfail", "msg": str(exc)}
                await self._send(command.client, {"t": "error", **error})
                await self._finish(command, "failed", error=error)
            else:
                await self._finish(command, "applied", **(result or {}))
            finally:
                self._running.pop(domain, None)
                command.completed.set()

    async def detach(self, client) -> None:
        for queue in self._pending.values():
            for command in list(queue):
                if command.client is client:
                    queue.remove(command)
                    await self._finish(command, "superseded", reason="client_disconnected")

    def health(self) -> dict:
        now = time.monotonic()
        return {"queueDepth": sum(map(len, self._pending.values())),
                "capacity": self._global_limit, "historySize": len(self._history),
                "maxCommandBytes": self._max_command_bytes,
                "domains": {domain: {"queueDepth": len(queue),
                    "running": domain in self._running,
                    "runningAgeSeconds": round(now - self._running[domain].admitted_at, 3) if domain in self._running else 0,
                    "oldestAgeSeconds": round(now - queue[0].admitted_at, 3) if queue else 0}
                    for domain, queue in self._pending.items()}}

    async def close(self, timeout_s: float = 5.0) -> bool:
        self._closing = True
        pending = [item for queue in self._pending.values() for item in queue]
        for queue in self._pending.values():
            queue.clear()
        if pending:
            await asyncio.gather(*(self._finish(item, "superseded", reason="application_closing") for item in pending))
        running = list(self._running.values())
        if running:
            try:
                await asyncio.wait_for(asyncio.gather(*(item.completed.wait() for item in running)), timeout_s)
            except TimeoutError:
                await asyncio.gather(*(self._finish(item, "outcome_unknown", reason="shutdown_deadline")
                                       for item in running if not item.completed.is_set()))
                return False  # Leave live owner tasks intact; close can be retried.
        for worker in self._workers.values():
            worker.cancel()
        await asyncio.gather(*self._workers.values(), return_exceptions=True)
        return True

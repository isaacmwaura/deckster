"""Causal observations, global admission and independent command ownership."""
import asyncio
from pathlib import Path
import os
import subprocess
import sys

import pytest

from agent.commands import ApplicationCommands, CommandBusy
from agent.state import AppState
from helpers import engine_client, hello, recv, run
from test_ws_dispatch import command_client


class Reply:
    def __init__(self, identity="test"):
        self.device_id = identity
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


def command(command_id, level, sequence=1):
    return {"t": "set_volume", "target": {"kind": "mic"}, "level": level,
            "commandId": command_id, "clientSeq": sequence}


def test_media_cannot_block_same_socket_volume_or_ping():
    async def body():
        class Media:
            def __init__(self):
                self.entered, self.release = asyncio.Event(), asyncio.Event()
            async def control(self, *args):
                self.entered.set()
                await self.release.wait()
                return True
        media = Media()
        async with engine_client(media=media) as (client, state, controller):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_json({"t": "media_control", "action": "pause", "commandId": "media", "clientSeq": 1})
            await asyncio.wait_for(media.entered.wait(), 1)
            try:
                await ws.send_json(command("volume", .23, 2))
                await ws.send_json({"t": "ping"})
                async def replies():
                    applied, pong = False, False
                    while not (applied and pong):
                        message = await recv(ws)
                        if message.get("commandId") == "volume" and message.get("status") == "applied":
                            applied = True
                            assert message["observation"]["level"] == .23
                            assert message["ownerSeq"] > 0
                        pong |= message.get("t") == "pong"
                await asyncio.wait_for(replies(), .5)
                assert state.devices["micMaster"]["level"] == .23
                assert controller.health()["domains"]["media"]["running"]
            finally:
                media.release.set()
            await ws.close()
    run(body())


def test_global_volume_coalescing_reports_supersession_across_clients():
    async def body():
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []
        async def execute(client, message):
            if message.get("t") == "set_mute":
                entered.set()
                await release.wait()
            seen.append(message)
        facade = ApplicationCommands(execute, "epoch", lane_limit=2, global_limit=3)
        first, second = Reply("one"), Reply("two")
        blocker = await facade.admit(first, {"t": "set_mute"})
        await entered.wait()
        stale = await facade.admit(first, command("stale", .1))
        latest = await facade.admit(second, command("latest", .9))
        assert (await stale)["status"] == "superseded"
        assert facade.health()["queueDepth"] == 1
        release.set()
        await asyncio.gather(blocker, latest)
        assert [item.get("level") for item in seen] == [None, .9]
        await facade.close()
    run(body())


@pytest.mark.parametrize("slow_command", ["media_control", "macro"])
def test_legacy_transport_adapters_keep_volume_and_ping_independent(slow_command):
    async def body():
        entered, release = asyncio.Event(), asyncio.Event()
        async def execute(client, message):
            if message["t"] == slow_command:
                entered.set()
                await release.wait()
            elif message["t"] == "set_volume":
                await client.send({"t": "volume_applied"})
        async with command_client(execute) as (client, state):
            ws = await client.ws_connect("/ws")
            await hello(ws)
            await ws.send_json({"t": slow_command})
            await entered.wait()
            try:
                await ws.send_json({"t": "set_volume", "target": {"kind": "mic"}, "level": .2})
                await ws.send_json({"t": "ping"})
                async def replies():
                    seen = set()
                    while seen != {"pong", "volume_applied"}:
                        seen.add((await recv(ws))["t"])
                await asyncio.wait_for(replies(), .5)
            finally:
                release.set()
            await ws.close()
    run(body())


@pytest.mark.parametrize("play_type", ["soundboard_play", "soundboard_audition", "soundboard_test", "soundboard_verify_receiver"])
def test_global_stop_discards_other_client_plays_and_gates_new_play(play_type):
    async def body():
        config_entered, config_release, stop_entered, stop_release = [asyncio.Event() for _ in range(4)]
        seen = []
        async def execute(client, message):
            if message["t"] == "soundboard_config":
                config_entered.set()
                await config_release.wait()
            if message["t"] == "soundboard_stop_all":
                stop_entered.set()
                await stop_release.wait()
            seen.append(message)
        facade = ApplicationCommands(execute, "epoch")
        first, second = Reply("first"), Reply("second")
        config = await facade.admit(first, {"t": "soundboard_config"})
        await config_entered.wait()
        old = await facade.admit(first, {"t": play_type, "clipId": "old", "commandId": "old", "clientSeq": 1})
        stop = await facade.admit(second, {"t": "soundboard_stop_all"})
        await stop_entered.wait()
        new = await facade.admit(first, {"t": "soundboard_play", "clipId": "new", "commandId": "new", "clientSeq": 2})
        assert (await old)["status"] == "superseded"
        config_release.set()
        await config
        assert not new.done()
        stop_release.set()
        await asyncio.gather(stop, new)
        assert [message.get("clipId") for message in seen] == [None, None, "new"]
        await facade.close()
    run(body())


def test_global_domain_pressure_is_bounded_and_dedup_does_not_reexecute():
    async def body():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def execute(client, message):
            entered.set()
            await release.wait()
            calls.append(message)
        facade = ApplicationCommands(execute, "epoch", lane_limit=2, global_limit=2, history_limit=2)
        first = Reply("paired")
        message = {"t": "macro", "macroId": "toggle", "commandId": "same", "clientSeq": 1}
        future = await facade.admit(first, message)
        await entered.wait()
        pending = await facade.admit(Reply("other"), {"t": "macro", "macroId": "next"})
        with pytest.raises(CommandBusy):
            await facade.admit(Reply("third"), {"t": "media_control"})
        reconnect = Reply("paired")
        assert await facade.admit(reconnect, message) is future
        with pytest.raises(ValueError, match="different command"):
            await facade.admit(reconnect, {**message, "macroId": "different"})
        release.set()
        await asyncio.gather(future, pending)
        assert reconnect.messages[-1]["status"] == "applied"
        assert (await (await facade.admit(reconnect, message)))["status"] == "applied"
        assert len(calls) == 2
        await facade.close()
    run(body())


def test_oversized_ignored_fields_are_rejected_before_admission_or_history():
    async def body():
        calls = []
        async def execute(client, message):
            calls.append(message)
        facade = ApplicationCommands(execute, "epoch")
        client = Reply()
        with pytest.raises(ValueError, match="payload budget"):
            await facade.admit(client, {**command("too-large", .2), "ignored": "x" * 65536})
        assert calls == []
        assert not facade._requests and not facade._inflight and not facade._pending
        assert facade.health()["historySize"] == 0
        assert await facade.close()
    run(body())


def test_cancelled_domain_job_reports_uncertainty_and_keeps_domain_working():
    async def body():
        async def execute(client, message):
            if message["action"] == "cancel":
                raise asyncio.CancelledError
        facade = ApplicationCommands(execute, "epoch")
        client = Reply()
        result = await (await facade.admit(client, {"t": "media_control", "action": "cancel", "commandId": "cancel", "clientSeq": 1}))
        assert result["status"] == "outcome_unknown"
        result = await (await facade.admit(client, {"t": "media_control", "action": "pause", "commandId": "pause", "clientSeq": 2}))
        assert result["status"] == "applied"
        assert await facade.close()
    run(body())


def test_disconnect_supersedes_queued_commands_keeps_running_outcome():
    async def body():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def execute(client, message):
            entered.set()
            await release.wait()
            calls.append(message)
        facade = ApplicationCommands(execute, "epoch")
        client = Reply("paired")
        running = await facade.admit(client, {"t": "macro", "commandId": "running", "clientSeq": 1})
        await entered.wait()
        pending = await facade.admit(client, {"t": "macro", "commandId": "pending", "clientSeq": 2})
        await facade.detach(client)
        assert (await pending)["status"] == "superseded"
        release.set()
        assert (await running)["status"] == "applied"
        assert len(calls) == 1
        await facade.close()
    run(body())


def test_shutdown_deadline_reports_uncertainty_and_retains_running_owner():
    async def body():
        entered, release = asyncio.Event(), asyncio.Event()
        async def execute(client, message):
            entered.set()
            await release.wait()
        facade = ApplicationCommands(execute, "epoch")
        client = Reply()
        running = await facade.admit(client, {"t": "macro", "commandId": "running", "clientSeq": 1})
        await entered.wait()
        pending = await facade.admit(client, {"t": "macro", "commandId": "pending", "clientSeq": 2})
        assert not await facade.close(timeout_s=.01)
        assert (await running)["status"] == "outcome_unknown"
        assert (await pending)["status"] == "superseded"
        assert facade.health()["domains"]["input"]["running"]
        release.set()
        assert await facade.close(timeout_s=1)
        assert client.messages[-1]["status"] == "applied"
    run(body())


def test_owner_sequence_rejects_delayed_poll_and_deleted_session():
    state = AppState()
    sessions = [{"id": "one", "level": .2, "muted": False, "active": True}]
    master = {"level": .2, "muted": False}
    state.ingest_full(sessions, master, master, [], [], owner_seq=1)
    state.apply_session("one", level=.9, owner_seq=3)
    state.apply_master("mic", level=.8, owner_seq=4)
    state.ingest_full([], master, master, [], [], owner_seq=2)
    assert state.sessions["one"]["level"] == .9
    assert state.devices["micMaster"]["level"] == .8
    state.ingest_full([], master, master, [], [], owner_seq=5)
    assert "one" not in state.sessions
    state.ingest_full(sessions, master, master, [], [], owner_seq=2)
    assert "one" not in state.sessions


def test_unchanged_control_poll_only_publishes_meters():
    state = AppState()
    master = {"level": .2, "muted": False}
    queue = state.subscribe()
    state.ingest_full([], master, master, [], [], {"input": .1, "output": .2}, owner_seq=1)
    assert queue.get_nowait()["t"] == "snapshot"
    state.ingest_full([], master, master, [], [], {"input": .1, "output": .2}, owner_seq=2)
    assert queue.empty()
    state.ingest_full([], master, master, [], [], {"input": .8, "output": .2}, owner_seq=3)
    assert queue.get_nowait() == {"t": "meters", "meters": {"input": .8, "output": .2},
                                 "ownerSeq": 3, "serverEpoch": state.server_epoch}


def test_desktop_and_phone_share_command_readback_and_identified_result():
    async def body():
        async with engine_client() as (_client, state, controller):
            result = await controller.desktop_command({"action": "control", "command": command("desktop", 2)})
            outcome = result["commandResult"]
            assert outcome["status"] == "applied"
            assert outcome["observation"]["level"] == 1
            assert state.devices["micMaster"]["level"] == 1
            reply = Reply()
            await controller.handle(reply, command("phone", .3))
            assert reply.messages[-1]["status"] == "applied"
            assert reply.messages[-1]["ownerSeq"] > outcome["ownerSeq"]
            assert state.devices["micMaster"]["level"] == .3
            with pytest.raises(ValueError, match="local desktop"):
                await controller.admit(reply, {"t": "soundboard_import", "source": "private-file"})
    run(body())


def test_real_threaded_engine_faults_in_isolated_subprocess():
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root)}
    result = subprocess.run([sys.executable, str(root / "tests" / "_threaded_command_probe.py")],
                            cwd=root, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "threaded command fault checks passed" in result.stdout

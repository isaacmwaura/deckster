"""Hardware-free integrated COM-owner probe isolated from pytest event loops."""
import asyncio
from dataclasses import asdict
import threading
import time

from aiohttp.test_utils import TestClient, TestServer

from agent.audio.engine import AudioEngine, EngineBusy, ObservationUncertain
from agent.audio.mock import MockAudioBackend
from agent.controller import Controller
from agent.server import create_app
from agent.state import AppState


class Backend(MockAudioBackend):
    def setup(self):
        self.owner = threading.get_ident()
        self.fail_readback = False
        self.torn_down = False
    def get_master(self, kind):
        assert threading.get_ident() == self.owner
        if self.fail_readback:
            self.fail_readback = False
            raise RuntimeError("injected readback failure")
        return super().get_master(kind)
    def set_master_volume(self, kind, level):
        assert threading.get_ident() == self.owner
        super().set_master_volume(kind, level)
        if level == .47:
            self.fail_readback = True
    def teardown(self):
        assert threading.get_ident() == self.owner
        self.torn_down = True


async def check_integration():
    state, backend = AppState(), Backend()
    loop = asyncio.get_running_loop()
    engine = AudioEngine(lambda: backend, .05, queue_limit=2, discovery_interval_s=1)
    class Media:
        def __init__(self):
            self.entered, self.release = asyncio.Event(), asyncio.Event()
        async def control(self, *args):
            self.entered.set()
            await self.release.wait()
            return True
    media = Media()
    controller = Controller(state, engine, loop, media=media)
    engine.set_on_poll(controller.make_on_poll())
    await asyncio.to_thread(engine.start)
    client = TestClient(TestServer(create_app(state, controller.handle)))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        await ws.send_json({"t": "hello"})
        await ws.receive_json()
        await ws.send_json({"t": "media_control", "action": "pause"})
        await asyncio.wait_for(media.entered.wait(), 1)
        delayed = await asyncio.wrap_future(engine.submit(lambda b: engine._gather(b)))
        await ws.send_json({"t": "set_volume", "target": {"kind": "mic"}, "level": .23, "commandId": "write", "clientSeq": 1})
        await ws.send_json({"t": "ping"})
        async def wait_both():
            result, pong = None, False
            while result is None or not pong:
                message = await ws.receive_json()
                if message.get("commandId") == "write" and message.get("status") == "applied":
                    result = message
                pong |= message.get("t") == "pong"
            return result
        result = await asyncio.wait_for(wait_both(), 1)
        assert result["observation"]["level"] == .23
        assert result["ownerSeq"] > delayed["ownerSeq"]
        devices = delayed["devices"]
        controller._ingest_poll([asdict(item) for item in delayed["sessions"]], asdict(delayed["speaker"]),
                                asdict(delayed["mic"]), [asdict(item) for item in devices.outputs],
                                [asdict(item) for item in devices.inputs], delayed["meters"], delayed["ownerSeq"])
        assert state.devices["micMaster"]["level"] == .23
        await ws.send_json({"t": "set_volume", "target": {"kind": "mic"}, "level": .47, "commandId": "uncertain", "clientSeq": 2})
        async def uncertain():
            while True:
                message = await ws.receive_json()
                if message.get("commandId") == "uncertain" and message.get("status") == "outcome_unknown":
                    return message
        assert (await asyncio.wait_for(uncertain(), 1))["error"]["code"] == "outcome_unknown"
        media.release.set()
        await ws.close()
    finally:
        media.release.set()
        await client.close()
        await controller.close()
        await asyncio.to_thread(engine.stop)
    assert backend.torn_down and not engine.health()["threadAlive"]


def check_faults():
    entered, release = threading.Event(), threading.Event()
    engine = AudioEngine(MockAudioBackend, 10, queue_limit=1)
    engine.start()
    try:
        def hold(backend):
            entered.set()
            assert release.wait(3)
        running = engine.submit(hold)
        assert entered.wait(1)
        pending = engine.submit(lambda b: "queued")
        try:
            engine.submit(lambda b: None).result(1)
            raise AssertionError("unbounded owner admission")
        except EngineBusy:
            pass
        assert engine.health()["queueDepth"] == 1
        assert pending.cancel()
        release.set()
        running.result(1)
        deadline = time.monotonic() + 1
        while engine.health()["queueDepth"] and time.monotonic() < deadline:
            time.sleep(.005)
        assert engine.submit(lambda b: "recovered").result(1) == "recovered"
    finally:
        release.set()
        engine.stop()
    startup_release = threading.Event()
    class SlowStart(MockAudioBackend):
        def setup(self):
            assert startup_release.wait(2)
    startup = AudioEngine(SlowStart, 10)
    try:
        try:
            startup.start(timeout=.01)
            raise AssertionError("startup timeout was hidden")
        except TimeoutError:
            pass
        assert not startup.health()["ready"]
    finally:
        startup_release.set()
        startup.stop()


def check_exception_ownership():
    released, delivered = [], []
    failure_entered, failure_release, inspected = threading.Event(), threading.Event(), threading.Event()
    class OwnerReference:
        def __del__(self):
            released.append(threading.get_ident())
    def native_failure(backend):
        native_pointer = OwnerReference()
        failure_entered.set()
        assert failure_release.wait(2)
        try:
            raise LookupError("inner native error")
        except LookupError as inner:
            outer = ValueError("outer native error")
            outer.native_code = 7
            outer.add_note("safe diagnostic note")
            raise outer from inner
    def assert_no_owner_stack(error):
        assert error.__cause__ is None and error.__context__ is None
        traceback = error.__traceback__
        while traceback is not None:
            assert traceback.tb_frame.f_code.co_name not in {"native_failure", "pointer_failure", "_run", "setup", "observe"}
            traceback = traceback.tb_next
    engine = AudioEngine(MockAudioBackend, 10)
    engine.start()
    try:
        future = engine.submit(native_failure)
        assert failure_entered.wait(1)
        def inspect(future):
            error = future.exception()
            assert error.__traceback__ is None
            assert_no_owner_stack(error)
            delivered.append(threading.get_ident())
            inspected.set()
        future.add_done_callback(inspect)
        failure_release.set()
        try:
            future.result(1)
        except ValueError as error:
            assert str(error) == "outer native error" and error.native_code == 7
            assert error.__notes__ == ["safe diagnostic note"]
            assert_no_owner_stack(error)
        else:
            raise AssertionError("native error was hidden")
        assert released == [engine._thread.ident]
        assert inspected.wait(1)
        assert delivered == [engine._thread.ident]
        def pointer_failure(backend):
            raise ValueError(OwnerReference())
        try:
            engine.submit(pointer_failure).result(1)
        except RuntimeError as error:
            assert error.original_type == "builtins.ValueError"
            assert isinstance(error.args[0], str)
            assert_no_owner_stack(error)
        else:
            raise AssertionError("native pointer argument crossed owner")
        engine.submit(lambda b: "release barrier").result(1)
        assert released == [engine._thread.ident] * 2
        try:
            engine.submit_observation({"kind": "mic"}, native_failure).result(1)
        except ObservationUncertain as error:
            assert str(error) == "outer native error"
            assert_no_owner_stack(error)
        else:
            raise AssertionError("observation error was hidden")
    finally:
        failure_release.set()
        engine.stop()
    class FailingSetup(MockAudioBackend):
        def setup(self):
            native_pointer = OwnerReference()
            raise ValueError("setup native failure")
    startup = AudioEngine(FailingSetup, 10)
    pending = startup.submit(lambda b: None)
    try:
        try:
            startup.start()
        except ValueError as error:
            assert str(error) == "setup native failure"
            assert_no_owner_stack(error)
        else:
            raise AssertionError("setup error was hidden")
        try:
            pending.result(1)
        except ValueError as error:
            assert_no_owner_stack(error)
        assert released[-1] == startup._thread.ident
        assert_no_owner_stack(startup._init_error)
    finally:
        startup.stop()


if __name__ == "__main__":
    asyncio.run(check_integration())
    check_faults()
    check_exception_ownership()
    print("threaded command fault checks passed")

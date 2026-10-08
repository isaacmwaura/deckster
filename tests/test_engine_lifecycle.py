"""Cancellation and shutdown must not strand COM jobs or apply stale writes."""
import threading

import pytest

from agent.audio.engine import AudioEngine
from agent.audio.mock import MockAudioBackend


def test_canceled_queued_audio_write_is_skipped_and_engine_keeps_working():
    entered, release = threading.Event(), threading.Event()
    writes = []
    engine = AudioEngine(MockAudioBackend, poll_interval_s=10)
    engine.start()
    try:
        def hold(_backend):
            entered.set()
            assert release.wait(2)
        running = engine.submit(hold)
        assert entered.wait(2)
        canceled = engine.submit(lambda _backend: writes.append("stale"))
        assert canceled.cancel()
        followup = engine.submit(lambda _backend: "still alive")
        release.set()
        running.result(2)
        assert followup.result(2) == "still alive"
        assert writes == []
        assert engine._thread.is_alive()
    finally:
        release.set()
        engine.stop()


def test_shutdown_fails_pending_audio_jobs_without_executing_them():
    entered, release = threading.Event(), threading.Event()
    writes = []
    engine = AudioEngine(MockAudioBackend, poll_interval_s=10)
    engine.start()
    def hold(_backend):
        entered.set()
        assert release.wait(2)
    running = engine.submit(hold)
    assert entered.wait(2)
    pending = engine.submit(lambda _backend: writes.append("after shutdown"))
    followups = []
    pending.add_done_callback(lambda _future: followups.append(engine.submit(lambda _backend: "stopped")))
    canceled = engine.submit(lambda _backend: writes.append("canceled"))
    canceled.cancel()
    stopper = threading.Thread(target=engine.stop)
    stopper.start()
    try:
        with pytest.raises(RuntimeError, match="engine stopped"):
            pending.result(2)
        with pytest.raises(RuntimeError, match="engine stopped"):
            engine.submit(lambda _backend: None).result(2)
        release.set()
        running.result(2)
        stopper.join(2)
        assert not stopper.is_alive()
        assert writes == [] and canceled.cancelled()
        with pytest.raises(RuntimeError, match="engine stopped"):
            followups[0].result(2)
    finally:
        release.set()
        stopper.join(3)
        engine.stop()


def test_initialization_failure_completes_jobs_already_waiting():
    def fail():
        raise ValueError("backend unavailable")
    engine = AudioEngine(fail, poll_interval_s=10)
    pending = engine.submit(lambda _backend: "never")
    with pytest.raises(ValueError, match="backend unavailable"):
        engine.start()
    with pytest.raises(ValueError, match="backend unavailable"):
        pending.result(2)
    with pytest.raises(RuntimeError, match="engine stopped"):
        engine.submit(lambda _backend: "never").result(2)

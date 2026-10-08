"""Offline regressions for audio budgets, callback ownership and longer effects.

No test opens a native audio stream, captures a microphone or plays audible audio.
"""
from dataclasses import replace
from pathlib import Path
import threading
from types import SimpleNamespace
import weakref

import numpy as np
import pytest
import soundfile as sf

from agent.audio_runtime import AudioRuntimeLimits
from agent.soundboard import SoundboardRenderer, SoundboardService


def ready_renderer(**kwargs):
    renderer = SoundboardRenderer(**kwargs)
    renderer._voice_stream = SimpleNamespace(active=True, stop=lambda: None, close=lambda: None)
    renderer._ears_stream = SimpleNamespace(active=True, stop=lambda: None, close=lambda: None)
    return renderer


def prepared(renderer, tmp_path, clip_id, samples, voice=True, ears=False):
    path = tmp_path / (clip_id + ".wav")
    return renderer.trigger_prepared(clip_id, (samples, renderer._clip_signature(path)), path, 1, voice, ears)


def test_multi_minute_compressed_effect_reaches_its_tail_without_truncation(tmp_path):
    # Exercise compressed decoding, 8-to-48 kHz resampling and the same renderer
    # path used by phone triggers. A terminal marker proves the tail survives.
    duration = 125
    source = np.full(8000 * duration, .2, dtype=np.float32)
    source[-8000:] = .4
    path = tmp_path / "long-effect.flac"
    sf.write(path, source, 8000)
    renderer = ready_renderer()
    trace_id = renderer.trigger("long", path, 1, True, False)
    assert len(renderer._clips["long"]) == duration * 48000
    microphone = np.zeros((4096, 1), dtype=np.float32)
    output = np.zeros((4096, 2), dtype=np.float32)
    rendered = 0
    while rendered < duration * 48000:
        count = min(len(output), duration * 48000 - rendered)
        renderer._voice_callback(microphone[:count], output[:count], count, None, None)
        if rendered == 0:
            np.testing.assert_allclose(output[:count], .2, atol=.00004)
        if rendered > (duration - .5) * 48000:
            np.testing.assert_allclose(output[:count], .4, atol=.00004)
        rendered += count
    assert renderer.playing_ids() == set()
    trace = renderer.diagnostics()["playbackTrace"][-1]
    assert trace["traceId"] == trace_id
    assert trace["voiceFirstFrameNs"] >= trace["queuedAtNs"] > 0
    assert trace["voiceRenderedFrames"] == duration * 48000
    assert trace["voiceEndReason"] == "completed"
    assert trace["earsRenderedFrames"] == 0


@pytest.mark.parametrize("changes, message", [
    ({"max_clip_seconds": .01}, "longer"),
    ({"max_clip_bytes": 32}, "decoded audio"),
    ({"max_source_channels": 1}, "channels"),
    ({"max_source_rate": 8000}, "sample rate"),
])
def test_admission_rejects_before_reading_any_audio(tmp_path, monkeypatch, changes, message):
    path = tmp_path / "limited.wav"
    sf.write(path, np.zeros((4800, 2), dtype=np.float32), 48000)
    limits = replace(AudioRuntimeLimits(), **changes)
    renderer = ready_renderer(limits=limits)
    monkeypatch.setattr(sf.SoundFile, "read", lambda *a, **kw: pytest.fail("read before admission"))
    with pytest.raises(RuntimeError, match=message):
        renderer.trigger("limited", path, 1, True, False)
    assert renderer.playing_ids() == set()


def test_source_reads_and_resampling_are_chunked_with_continuous_boundaries(tmp_path, monkeypatch):
    count, rate = 12347, 44100
    # Asymmetrical ramp catches boundary repeats and incorrect global indices.
    source = np.linspace(-.65, .45, count, dtype=np.float32)
    path = tmp_path / "resampled.wav"
    sf.write(path, source, rate, subtype="FLOAT")
    renderer = ready_renderer(limits=replace(AudioRuntimeLimits(), decode_chunk_frames=73))
    reads = []
    original = sf.SoundFile.read
    def read(stream, frames=-1, **kwargs):
        reads.append(frames)
        return original(stream, frames, **kwargs)
    monkeypatch.setattr(sf.SoundFile, "read", read)
    decoded = renderer._load(path)
    expected = np.interp(np.linspace(0, count - 1, round(count * 48000 / rate)), np.arange(count), source)
    np.testing.assert_allclose(decoded, expected, atol=3e-8)
    assert len(reads) > 100 and max(reads) == 73 and min(reads) > 0


def test_whole_clip_phase_cancellation_does_not_switch_channels_at_chunk_boundaries(tmp_path):
    # Strongest channel changes locally, but the global energy winner must stay
    # the same through the entire decoded clip, as in the original mixer.
    left = np.linspace(-.4, .4, 1001, dtype=np.float32)
    right = -left
    path = tmp_path / "inverse.flac"
    sf.write(path, np.column_stack([left, right]), 24000)
    renderer = ready_renderer(limits=replace(AudioRuntimeLimits(), decode_chunk_frames=37))
    decoded = renderer._load(path)
    assert np.max(np.abs(decoded)) > .39
    assert abs(decoded[0] + .4) < .0001 and abs(decoded[-1] - .4) < .0001


def test_polyphony_rejection_is_atomic_and_retrigger_does_not_consume_another_slot(tmp_path):
    renderer = ready_renderer(limits=replace(AudioRuntimeLimits(), max_voices_per_bus=1))
    samples = np.full(128, .2, dtype=np.float32)
    prepared(renderer, tmp_path, "voice", samples, True, False)
    prepared(renderer, tmp_path, "ears", samples, False, True)
    with pytest.raises(RuntimeError, match="playback is full"):
        prepared(renderer, tmp_path, "both", samples, True, True)
    assert renderer.playing_ids() == {"voice", "ears"}
    assert len(renderer.diagnostics()["playbackTrace"]) == 2
    prepared(renderer, tmp_path, "voice", samples, True, False)
    assert len(renderer._voices["voice"]) == 1


def test_active_memory_counts_shared_buses_once_and_rejection_preserves_existing_audio(tmp_path):
    renderer = ready_renderer(limits=replace(AudioRuntimeLimits(), max_active_bytes=64))
    prepared(renderer, tmp_path, "a", np.full(8, .1, dtype=np.float32), True, True)
    assert renderer.diagnostics()["resources"]["activeBytes"] == 32
    prepared(renderer, tmp_path, "b", np.full(8, .2, dtype=np.float32), True, False)
    with pytest.raises(RuntimeError, match="memory budget"):
        prepared(renderer, tmp_path, "c", np.full(8, .3, dtype=np.float32), True, True)
    np.testing.assert_allclose(renderer._mix("voice", 8), .3)
    np.testing.assert_allclose(renderer._mix("ears", 8), .1)


def test_decoder_admission_bounds_waiters_and_reports_busy_immediately(tmp_path):
    renderer = ready_renderer(limits=replace(AudioRuntimeLimits(), max_pending_decodes=2))
    entered, release = threading.Event(), threading.Event()
    results, errors = [], []
    def decode(_path):
        entered.set()
        assert release.wait(2)
        return np.zeros(8, dtype=np.float32)
    renderer._load = decode
    def prepare(name):
        try:
            results.append(renderer.prepare_clip(name, tmp_path / (name + ".wav")))
        except Exception as exc:
            errors.append(exc)
    first = threading.Thread(target=prepare, args=("a",)); first.start()
    assert entered.wait(2)
    second = threading.Thread(target=prepare, args=("b",)); second.start()
    # Wait on a condition rather than depending on an arbitrary scheduler sleep.
    import time
    deadline = time.monotonic() + 1
    while renderer._pending_decodes < 2 and time.monotonic() < deadline:
        time.sleep(.001)
    try:
        with pytest.raises(RuntimeError, match="decoder is busy"):
            renderer.prepare_clip("c", tmp_path / "c.wav")
        assert renderer._pending_decodes == 2
    finally:
        release.set(); first.join(2); second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert not errors and len(results) == 2 and renderer._pending_decodes == 2
    for result in results:
        result.close()
    assert renderer._pending_decodes == 0


def test_callbacks_reuse_scratch_for_variable_blocks_without_bulk_numpy_allocations(tmp_path, monkeypatch):
    renderer = ready_renderer(limits=replace(AudioRuntimeLimits(), callback_chunk_frames=32))
    samples = np.full(173, .2, dtype=np.float32)
    prepared(renderer, tmp_path, "a", samples, True, True)
    microphone = np.full((97, 1), .15, dtype=np.float32)
    output = np.zeros((97, 2), dtype=np.float32)
    buffer_ids = {bus: {key: value.ctypes.data for key, value in buffers.items()}
                  for bus, buffers in renderer._buffers.items()}
    def forbidden(*args, **kwargs):
        raise AssertionError("bulk allocation from callback")
    with monkeypatch.context() as guard:
        for name in ("empty", "zeros", "ones", "array", "abs", "concatenate"):
            guard.setattr(np, name, forbidden)
        renderer._voice_callback(microphone, output, 97, None, None)
        renderer._ears_callback(output, 97, None, None)
    assert renderer.health_error() == "" and renderer.monitor_error() == ""
    np.testing.assert_allclose(output, .2)
    assert buffer_ids == {bus: {key: value.ctypes.data for key, value in buffers.items()}
                          for bus, buffers in renderer._buffers.items()}
    assert renderer._voices["voice"][0]["pos"] == 97
    assert renderer._voices["ears"][0]["pos"] == 97


def test_last_active_array_is_released_by_service_observation_not_callback(tmp_path):
    renderer = ready_renderer()
    samples = np.ones(8, dtype=np.float32)
    ref = weakref.ref(samples)
    prepared(renderer, tmp_path, "a", samples)
    del samples
    renderer._voice_callback(np.zeros((8, 1), dtype=np.float32), np.zeros((8, 2), dtype=np.float32), 8, None, None)
    assert ref() is not None
    assert renderer.playing_ids() == set()
    assert ref() is None


class GainProcessor:
    def __init__(self): self.events = []; self.fail = False
    def prepare(self, rate, frames): self.events.append(("prepare", rate, frames))
    def reset(self): self.events.append("reset")
    def process(self, samples, output, frames):
        self.events.append("process")
        if self.fail: raise RuntimeError("processor offline")
        np.multiply(samples, .5, out=output)
    def close(self): self.events.append("close")
    def health(self): return {"name": "test gain", "ready": True, "latencyFrames": 0}


def test_processor_receives_only_mic_and_me_remains_independent(tmp_path):
    processor = GainProcessor()
    renderer = ready_renderer(microphone_processor=processor)
    prepared(renderer, tmp_path, "both", np.full(16, .2, dtype=np.float32), True, True)
    output = np.zeros((8, 2), dtype=np.float32)
    renderer._voice_callback(np.full((8, 1), .4, dtype=np.float32), output, 8, None, None)
    np.testing.assert_allclose(output, .4)  # .4 mic * .5 + .2 clip
    renderer._ears_callback(output, 8, None, None)
    np.testing.assert_allclose(output, .2)
    assert processor.events[:2] == [("prepare", 48000, renderer.limits.callback_chunk_frames), "reset"]
    assert processor.events.count("process") == 1


def test_processor_failure_bypasses_visibly_without_disabling_voice_clips_or_me(tmp_path):
    processor = GainProcessor(); processor.fail = True
    renderer = ready_renderer(microphone_processor=processor)
    prepared(renderer, tmp_path, "both", np.full(32, .2, dtype=np.float32), True, True)
    output = np.zeros((8, 2), dtype=np.float32)
    for _ in range(2):
        renderer._voice_callback(np.full((8, 1), .4, dtype=np.float32), output, 8, None, None)
        np.testing.assert_allclose(output, .6)
    renderer._ears_callback(output, 8, None, None)
    np.testing.assert_allclose(output, .2)
    assert renderer.health_error() == "" and renderer.monitor_error() == ""
    assert "bypass active" in renderer.diagnostics()["microphoneProcessor"]["error"]
    assert processor.events.count("process") == 1


def test_callback_timing_ring_and_interruptions_are_bounded(monkeypatch):
    import agent.soundboard as module
    renderer = ready_renderer(limits=replace(AudioRuntimeLimits(), metric_samples=3))
    stamps = iter([0, 5_000_000, 10_000_000, 25_000_000,
                   30_000_000, 35_000_000, 40_000_000, 45_000_000])
    monkeypatch.setattr(module.time, "perf_counter_ns", lambda: next(stamps))
    status = SimpleNamespace(input_overflow=True, output_underflow=False)
    for _ in range(4):
        renderer._voice_callback(np.zeros((480, 1), dtype=np.float32), np.zeros((480, 2), dtype=np.float32), 480, None, status)
    summary = renderer.diagnostics()["streams"]["voice"]
    assert summary["callbacks"] == 4 and summary["retainedCallbacks"] == 3
    assert summary["overBudget"] == 1 and summary["inputOverflows"] == 4
    assert summary["statusInterruptions"] == 4 and summary["outputUnderflows"] == 0
    assert summary["durationMs"]["max"] == 15
    assert summary["deadlineUtilization"]["max"] == 1.5


def test_regular_library_snapshots_do_not_inspect_files(tmp_path, monkeypatch):
    from pathlib import Path
    path = tmp_path / "clip.wav"
    sf.write(path, np.zeros(4800, dtype=np.float32), 48000)
    service = SoundboardService(tmp_path / "data", defaults_root=tmp_path / "absent")
    service.import_clip(path)
    with monkeypatch.context() as guard:
        guard.setattr(Path, "stat", lambda *args, **kwargs: pytest.fail("snapshot inspected a file"))
        for _ in range(20):
            assert service.snapshot()["clips"][0]["duration"] == .1
    stored = service.clips_dir / service.snapshot()["clips"][0]["file"]
    stored.unlink()
    service.refresh_clip_metadata(force=True)
    assert service.snapshot()["clips"][0]["duration"] == 0


@pytest.mark.parametrize("action", ["config", "layout", "update", "remove", "import"])
def test_failed_library_save_preserves_published_data_route_playback_and_files(tmp_path, monkeypatch, action):
    from test_soundboard import RecordingRenderer
    class Renderer(RecordingRenderer):
        def __init__(self): super().__init__(); self.removed = []
        def remove_clip(self, clip_id): self.removed.append(clip_id)
    renderer = Renderer()
    service = SoundboardService(tmp_path / "data", renderer_factory=lambda: renderer,
                                defaults_root=tmp_path / "absent")
    source = tmp_path / "clip.wav"
    sf.write(source, np.zeros(4800, dtype=np.float32), 48000)
    clip = service.import_clip(source)
    outputs = [{"id": "voice", "name": "CABLE Input"}, {"id": "new", "name": "CABLE B"}]
    inputs = [{"id": "mic", "name": "Physical Mic"}]
    service.configure({"inputId": "mic", "voiceOutputId": "voice"}, outputs, inputs)
    service.play(clip["id"])
    before, durable = service.snapshot(), service._path.read_bytes()
    generation = service.play_generation
    files = {p.name for p in service.clips_dir.iterdir()}
    def fail(): raise OSError("disk full")
    monkeypatch.setattr(service, "_save", fail)
    with pytest.raises(OSError, match="disk full"):
        if action == "config": service.configure({"voiceOutputId": "new"}, outputs, inputs)
        elif action == "layout": service.configure({"layout": "b"}, [], [])
        elif action == "update": service.update_clip(clip["id"], label="Lost", gain=.2)
        elif action == "remove": service.remove_clip(clip["id"])
        else: service.import_clip(source, label="Not saved")
    assert service.snapshot() == before
    assert service._path.read_bytes() == durable and service.play_generation == generation
    assert {p.name for p in service.clips_dir.iterdir()} == files
    assert renderer.start_count == 1 and not renderer.stopped and renderer.removed == []


def test_completed_prepare_retains_admission_until_it_is_consumed_or_discarded(tmp_path):
    renderer = ready_renderer(limits=replace(AudioRuntimeLimits(), max_pending_decodes=1))
    renderer._load = lambda _path: np.zeros(8, dtype=np.float32)
    path = tmp_path / "a.wav"
    waiting = renderer.prepare_clip("a", path)
    with pytest.raises(RuntimeError, match="decoder is busy"):
        renderer.prepare_clip("b", tmp_path / "b.wav")
    renderer.trigger_prepared("a", waiting, path, 1, True, False)
    assert renderer._pending_decodes == 0
    waiting.close()  # idempotent: consuming it already released the lease
    abandoned = renderer.prepare_clip("b", tmp_path / "b.wav")
    abandoned.close()
    assert renderer._pending_decodes == 0


@pytest.mark.parametrize("operation", ["restore", "upgrade"])
def test_default_pack_save_failure_restores_replaced_files_and_preserves_playback(tmp_path, monkeypatch, operation):
    from test_soundboard import RecordingRenderer
    pack = Path(__file__).resolve().parents[1] / "assets" / "default-sounds"
    renderer = RecordingRenderer()
    service = SoundboardService(tmp_path / "data", renderer_factory=lambda: renderer, defaults_root=pack)
    clip = service.snapshot()["clips"][0]
    changed = service.clips_dir / clip["file"]
    # A changed managed file proves rollback restores bytes rather than leaving
    # the copied pack audio in place while JSON/in-memory data claims failure.
    changed.write_bytes(b"old managed audio")
    service._playing.add(clip["id"])
    before = service.snapshot()
    durable = service._path.read_bytes()
    files = {p.name: p.read_bytes() for p in service.clips_dir.iterdir()}
    generation = service.play_generation
    def fail(): raise OSError("disk full")
    monkeypatch.setattr(service, "_save", fail)
    with pytest.raises(OSError, match="disk full"):
        if operation == "restore": service.restore_defaults()
        else: service._upgrade_default_pack(service._default_manifest())
    assert service.snapshot() == before and service._path.read_bytes() == durable
    assert {p.name: p.read_bytes() for p in service.clips_dir.iterdir()} == files
    assert service.play_generation == generation and not renderer.stopped


def test_preparation_failure_is_visible_across_lifecycle_retry():
    class FailedProcessor(GainProcessor):
        def prepare(self, rate, frames): raise RuntimeError("missing resource")
    renderer = ready_renderer(microphone_processor=FailedProcessor())
    renderer._prepare_runtime()  # model/resource work remains outside callback
    output = np.zeros((8, 2), dtype=np.float32)
    renderer._voice_callback(np.full((8, 1), .3, dtype=np.float32), output, 8, None, None)
    np.testing.assert_allclose(output, .3)
    assert "missing resource" in renderer.diagnostics()["microphoneProcessor"]["error"]
    assert renderer.health_error() == ""


def test_pressure_bounds_union_of_cache_active_and_pending_prepared_arrays(tmp_path):
    limits = replace(AudioRuntimeLimits(), max_clip_bytes=64, max_active_bytes=384,
                     max_voices_per_bus=12, max_pending_decodes=2)
    renderer = ready_renderer(limits=limits)
    renderer.clip_cache_bytes = 64
    renderer._load = lambda _path: np.full(8, .01, dtype=np.float32)
    for index in range(12):
        renderer.trigger(str(index), tmp_path / f"{index}.wav", 1, True, True)
    waiting = [renderer.prepare_clip(str(index), tmp_path / f"{index}.wav") for index in (12, 13)]
    try:
        arrays = {id(v["samples"]): v["samples"] for bus in renderer._voices.values() for v in bus}
        arrays.update({id(a): a for a in renderer._clips.values()})
        arrays.update({id(p.samples): p.samples for p in waiting})
        resources = renderer.diagnostics()["resources"]
        assert sum(a.nbytes for a in arrays.values()) == 448
        assert resources["maxRetainedDecodedBytes"] == 576
        assert resources["activeBytes"] == 384 and resources["cacheBytes"] == 64
        assert resources["pendingPrepares"] == 2
        with pytest.raises(RuntimeError, match="decoder is busy"):
            renderer.prepare_clip("14", tmp_path / "14.wav")
        with pytest.raises(RuntimeError, match="playback is full"):
            renderer.trigger_prepared("12", waiting[0], tmp_path / "12.wav", 1, True, True)
        assert len(renderer._voices["voice"]) == len(renderer._voices["ears"]) == 12
    finally:
        for item in waiting: item.close()
    assert renderer._pending_decodes == 0


def test_preview_obeys_active_budget_before_opening_a_device(tmp_path, monkeypatch):
    import sys
    renderer = ready_renderer(limits=replace(AudioRuntimeLimits(), max_active_bytes=16))
    renderer._load = lambda _path: np.ones(8, dtype=np.float32)
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace())
    monkeypatch.setattr(renderer, "_find_device", lambda *args: pytest.fail("device opened before admission"))
    with pytest.raises(RuntimeError, match="memory budget"):
        renderer.preview(tmp_path / "clip.wav", 1, "Headphones")
    assert renderer._ears_stream is None


def test_diagnostics_survives_native_property_failure_during_stream_close():
    class NativeClosedError(Exception): pass
    class ClosedStream:
        active = False
        @property
        def latency(self): raise NativeClosedError("stream closed while inspecting latency")
        @property
        def cpu_load(self): raise NativeClosedError("stream closed while inspecting utilization")
    renderer = ready_renderer()
    renderer._voice_stream = ClosedStream()
    diagnostics = renderer.diagnostics()
    assert "stream closed" in diagnostics["streams"]["voice"]["telemetryError"]


def test_unresolved_native_close_retains_ownership_and_blocks_duplicate_streams(monkeypatch):
    class StuckStream:
        active = True
        released = False
        def stop(self): raise RuntimeError("driver stop failed")
        def close(self):
            if not self.released: raise RuntimeError("driver close failed")
    renderer = SoundboardRenderer()
    stuck = StuckStream()
    renderer._voice_stream = stuck
    assert renderer.close() is False
    assert renderer._voice_stream is stuck and "retained" in renderer.health_error()
    monkeypatch.setattr(renderer, "_find_device", lambda *args: pytest.fail("duplicate device open"))
    with pytest.raises(RuntimeError, match="retained"):
        renderer.start("Mic", "CABLE Input")
    assert renderer._voice_stream is stuck
    stuck.released = True
    assert renderer.close() is True and renderer._voice_stream is None


def test_route_recovery_keeps_failed_handle_and_waits_for_cleanup_before_replacement(tmp_path, monkeypatch):
    import agent.soundboard as module
    from test_soundboard import RecordingRenderer
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    class StuckStream:
        active = True
        released = False
        def stop(self): raise RuntimeError("driver stop failed")
        def close(self):
            if not self.released: raise RuntimeError("driver close failed")
    made = []
    def factory():
        renderer = RecordingRenderer(); made.append(renderer); return renderer
    service = SoundboardService(tmp_path / "data", renderer_factory=factory, defaults_root=tmp_path / "absent")
    renderer = SoundboardRenderer(); stuck = StuckStream(); renderer._voice_stream = stuck
    service._renderer = renderer
    outputs, inputs = [{"id": "voice", "name": "CABLE Input"}], [{"id": "mic", "name": "Headset Mic"}]
    service.configure({"inputId": "mic", "voiceOutputId": "voice"}, outputs, inputs)
    durable = service._path.read_bytes()
    assert service._renderer is renderer and not made
    for _ in range(4): service.ensure_started(outputs, inputs)
    assert service._renderer is renderer and not made
    clock[0] += 1
    service.ensure_started(outputs, inputs)
    assert service._renderer is renderer and not made
    stuck.released = True
    clock[0] += 2
    service.ensure_started(outputs, inputs)
    assert service._renderer is made[0] and len(made) == 1
    assert service._path.read_bytes() == durable


def test_monitor_cleanup_failure_retains_handle_without_stopping_voice(monkeypatch):
    class StuckMonitor:
        active = True
        closes = 0
        def stop(self): raise RuntimeError("monitor stop failed")
        def close(self): self.closes += 1; raise RuntimeError("monitor close failed")
    renderer = ready_renderer()
    voice, monitor = renderer._voice_stream, StuckMonitor()
    renderer._ears_stream = monitor
    renderer._monitor_name = "old headphones"
    monkeypatch.setattr(renderer, "_find_device", lambda *args: pytest.fail("duplicate monitor open"))
    renderer.ensure_monitor("new headphones")
    renderer.ensure_monitor("new headphones")
    assert monitor.closes == 1 and renderer._ears_stream is monitor
    assert renderer._voice_stream is voice and renderer.health_error() == ""
    assert "retained" in renderer.monitor_error()
    output = np.zeros((8, 2), dtype=np.float32)
    renderer._voice_callback(np.full((8, 1), .3, dtype=np.float32), output, 8, None, None)
    np.testing.assert_allclose(output, .3)

"""Independent audio review regressions; every native stream is replaced."""
from types import SimpleNamespace
from dataclasses import replace

import numpy as np
import pytest

import agent.soundboard as module
from agent.soundboard import SoundboardRenderer, SoundboardService
from agent.audio_runtime import AudioRuntimeLimits


@pytest.mark.parametrize("stop_at", ["capture_open", "capture_settle"])
def test_stop_cancels_diagnostic_before_its_probe_can_start(tmp_path, monkeypatch, stop_at):
    renderer = SoundboardRenderer()
    renderer._voice_stream = SimpleNamespace(active=True)
    service = SoundboardService(tmp_path, defaults_root=tmp_path / "absent")
    service._renderer = renderer
    appended = []

    class CaptureStream:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            if stop_at == "capture_open":
                service.stop_all()
            return self
        def __exit__(self, *args):
            pass

    renderer._sd = SimpleNamespace(InputStream=CaptureStream, WasapiSettings=lambda **kwargs: kwargs)
    monkeypatch.setattr(renderer, "_find_device", lambda *args: 0)

    def sleep(seconds):
        if seconds < 1:
            if stop_at == "capture_settle":
                service.stop_all()
        else:
            appended.extend(renderer._voices["voice"])

    monkeypatch.setattr(module.time, "sleep", sleep)
    with pytest.raises(RuntimeError, match="stopped|cancel"):
        renderer.verify_receiver("CABLE Output")
    assert appended == []
    assert renderer.playing_ids() == set()
    assert renderer._voice_stream.active


def test_partial_import_copy_failure_removes_orphan_and_preserves_library(tmp_path, monkeypatch):
    service = SoundboardService(tmp_path / "data", defaults_root=tmp_path / "absent")
    source = tmp_path / "source.wav"
    source.write_bytes(b"encoded source data")
    before = service.snapshot()
    files = {path.name: path.read_bytes() for path in service.clips_dir.iterdir()}

    def partial_copy(source, destination):
        destination.write_bytes(b"partial audio copy")
        raise OSError("disk full")

    monkeypatch.setattr(module.shutil, "copy2", partial_copy)
    with pytest.raises(OSError, match="disk full"):
        service.import_clip(source)
    assert service.snapshot() == before
    assert {path.name: path.read_bytes() for path in service.clips_dir.iterdir()} == files


def test_route_tones_on_both_buses_count_both_retained_sample_arrays():
    renderer = SoundboardRenderer(limits=replace(AudioRuntimeLimits(), max_active_bytes=130000))
    renderer._voice_stream = SimpleNamespace(active=True)
    renderer._ears_stream = SimpleNamespace(active=True)
    renderer.test_tone("voice")
    with pytest.raises(RuntimeError, match="memory budget"):
        renderer.test_tone("ears")
    assert renderer.diagnostics()["resources"]["activeBytes"] == 86400
    assert len(renderer._voices["voice"]) == 1 and renderer._voices["ears"] == []

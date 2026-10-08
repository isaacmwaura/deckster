"""Configuration-B soundboard: persistent pads plus a device-agnostic mixer.

The service deliberately knows nothing about VB-Cable.  It is given a physical
input and one or two *playback* endpoint names; a virtual cable is simply the
usual choice for the Voice endpoint.  This keeps the same mixer usable for the
later multi-endpoint mic-router without a VoiceMeeter-specific audio path.

``sounddevice`` (PortAudio/WASAPI) and ``numpy`` are optional imports.  Keeping
them optional lets the agent continue to be a useful mixer on machines that have
not installed the soundboard runtime yet, while making the missing requirement
explicit in the phone UI instead of failing at startup.
"""
from __future__ import annotations

import json
import copy
import hashlib
import re
import shutil
import threading
import time
import math
import uuid
import wave
from collections import OrderedDict, deque
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from .config import resource_root
from .log import get_logger
from .presentation import LEGACY_DEFAULT_KEYS
from .audio_runtime import AudioRuntimeLimits, CallbackMetrics, PreparedAudio
from .microphone import BypassMicrophoneProcessor, MicrophoneProcessor

log = get_logger("soundboard")

SUPPORTED_EXTENSIONS = frozenset({".wav", ".flac", ".ogg", ".mp3"})
DEFAULT_PACK_DIR = Path("assets") / "default-sounds"


def _clamp(value: object, low: float = 0.0, high: float = 1.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, value))


class SoundboardRenderer:
    """Small duplex WASAPI mixer owned by :class:`SoundboardService`.

    The Voice stream captures one physical microphone and renders mic + Voice
    clips to the configured playback endpoint.  A separate monitor stream
    renders Ears clips.  Each bus owns its own playhead so two device callbacks
    never race or double-advance a clip.
    """

    sample_rate = 48_000
    # A quick peak release follows syllables and short clips without lingering.
    level_release = 12.0
    clip_cache_bytes = 64 * 1024 * 1024
    clip_cache_entries = 32

    def __init__(self, on_error: Callable[[str], None] | None = None, *,
                 limits: AudioRuntimeLimits | None = None,
                 microphone_processor: MicrophoneProcessor | None = None) -> None:
        self._on_error = on_error or (lambda _reason: None)
        self._lock = threading.RLock()
        self._voice_stream = None
        self._ears_stream = None
        self._clips: OrderedDict[str, Any] = OrderedDict()
        self._clip_signatures = {}
        self._clip_cache_bytes = 0
        self._voices: dict[str, list[dict[str, Any]]] = {"voice": [], "ears": []}
        self._np = None
        self._sd = None
        self._failure = ""
        self._monitor_failure = ""
        self._monitor_name = None
        self._monitor_retry_after = 0.0
        self._monitor_retry_delay = 1.0
        self._warnings = {}
        self._reported_warning_counts = {}
        self._last_warning_log = 0.0
        self._reported_failure = ""
        self._levels = {}
        self.limits = limits or AudioRuntimeLimits()
        self._decode_slots = threading.BoundedSemaphore(self.limits.max_pending_decodes)
        self._decode_lock = threading.Lock()
        self._pending_decodes = 0
        self._buffers = {}
        self._metrics = {}
        self._processor = microphone_processor or BypassMicrophoneProcessor()
        self._processor_failure = ""
        self._traces = deque(maxlen=self.limits.playback_trace_entries)
        self._runtime_id = uuid.uuid4().hex
        self._trace_sequence = 0
        self._render_generation = 0
        # Optional import/allocation happens on the lifecycle caller, never in
        # the first audio callback. Missing NumPy still permits library editing.
        try:
            import numpy as np
            self._np = np
            self._prepare_runtime()
        except ImportError:
            pass

    def _prepare_runtime(self) -> None:
        np = self._np
        frames = self.limits.callback_chunk_frames
        self._buffers = {bus: {name: np.empty(frames, dtype=np.float32)
                              for name in ("clips", "scaled", "mic", "mixed")}
                         for bus in ("voice", "ears")}
        self._metrics = {bus: CallbackMetrics(np, self.limits.metric_samples, self.sample_rate)
                         for bus in ("voice", "ears")}
        self._processor_failure = ""
        try:
            self._processor.prepare(self.sample_rate, frames)
            self._processor.reset()
        except Exception as exc:  # noqa: BLE001 - continuity with visible bypass
            self._processor_failure = "Microphone processor preparation failed; bypass active: " + str(exc)
            try:
                self._processor.close()
            except Exception:  # noqa: BLE001
                pass
            # Keep the selected processor so its failed health remains visible
            # and a later lifecycle restart can retry preparation. Callbacks use
            # captured samples directly while the failure flag is set.

    def _record_level(self, bus, samples):
        # Two reductions avoid an abs-sized array on every meter update.
        peak = max(abs(float(self._np.min(samples))), abs(float(self._np.max(samples)))) if len(samples) else 0.0
        now = time.monotonic()
        previous, stamp = self._levels.get(bus, (0.0, now))
        self._levels[bus] = (max(peak, previous * math.exp(-(now - stamp) * self.level_release)), now)

    def levels(self):
        now = time.monotonic()
        return {bus: min(1.0, peak * math.exp(-(now - stamp) * self.level_release))
                for bus, (peak, stamp) in list(self._levels.items())}

    def check_formats(self, endpoints):
        """Probe WASAPI format support without changing Windows settings."""
        import sounddevice as sd
        results = []
        for label, name, kind in endpoints:
            try:
                index = self._find_device(sd, name, kind)
                device = sd.query_devices(index)
                check = sd.check_input_settings if kind == "input" else sd.check_output_settings
                check(device=index, channels=1 if kind == "input" else 2,
                      dtype="float32", samplerate=self.sample_rate,
                      extra_settings=sd.WasapiSettings(auto_convert=True))
                results.append({"label": label, "name": name, "supported": True,
                                "defaultRate": device.get("default_samplerate")})
            except Exception as exc:
                results.append({"label": label, "name": name, "supported": False, "error": str(exc)})
        return {"sampleRate": self.sample_rate, "endpoints": results,
                "recommendation": "In Windows Sound settings → More sound settings → device Properties → Advanced, choose 48,000 Hz for the microphone and both cable ends. Disable exclusive mode if another app holds a device, then reconnect audio and check again. Different default rates can work through WASAPI conversion; a failed probe means this format is unavailable."}

    def health_error(self) -> str:
        # Called from ordinary service work, never a PortAudio callback. Disk
        # logging and application callbacks cannot run on the audio deadline.
        now = time.monotonic()
        if now - self._last_warning_log >= 5.0:
            for bus, warning in list(self._warnings.items()):
                count, message = warning
                if count != self._reported_warning_counts.get(bus):
                    log.warning("soundboard %s stream: %s (%s warnings)", bus, message, count)
                    self._reported_warning_counts[bus] = count
            self._last_warning_log = now
        if self._failure:
            if self._reported_failure != self._failure:
                self._reported_failure = self._failure
                self._on_error(self._failure)
            return self._failure
        if self._voice_stream is not None and not getattr(self._voice_stream, "active", True):
            return "Audio device stopped. Deckster will reconnect the saved route automatically."
        return ""

    def monitor_error(self) -> str:
        if self._monitor_failure:
            return self._monitor_failure
        if self._ears_stream is not None and not getattr(self._ears_stream, "active", True):
            return "Local listening stopped. Deckster will reconnect it; microphone and Others continue."
        return ""

    def stream_warnings(self) -> dict[str, Any]:
        return {bus: {"count": count, "last": message}
                for bus, (count, message) in list(self._warnings.items())}

    def _record_warning(self, bus, status) -> None:
        if status:
            count, _message = self._warnings.get(bus, (0, ""))
            self._warnings[bus] = (count + 1, str(status))

    @staticmethod
    def _find_device(sd, wanted: str, kind: str) -> int:
        """Return the WASAPI device index matching a Windows endpoint label.

        PortAudio exposes the same name through several host APIs, so passing a
        matched name back to sounddevice makes stream creation ambiguous.
        """
        wanted_folded = wanted.casefold()
        devices = sd.query_devices()
        # PortAudio exposes the same physical device through MME, DirectSound,
        # WDM-KS and WASAPI. The product contract is explicitly WASAPI, so never
        # accept a same-named legacy-host entry merely because it appeared first.
        candidates = [(index, d["name"]) for index, d in enumerate(devices)
                      if d.get("max_" + kind + "_channels", 0) > 0
                      and "wasapi" in sd.query_hostapis(d["hostapi"])["name"].casefold()]
        exact = [index for index, name in candidates if name.casefold() == wanted_folded]
        matches = exact or [index for index, name in candidates
                            if wanted_folded in name.casefold() or name.casefold() in wanted_folded]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RuntimeError(f"audio device label is ambiguous in WASAPI: {wanted}")
        raise RuntimeError(f"audio device not available to WASAPI: {wanted}")

    def start(self, input_name: str, voice_name: str, ears_name: str | None = None) -> None:
        if self.close() is False:
            raise RuntimeError("The previous audio stream could not close; its handle is retained for retry")
        self._failure = ""
        self._reported_failure = ""
        self._warnings.clear()
        self._reported_warning_counts.clear()
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("Soundboard audio runtime is not installed") from exc

        self._np, self._sd = np, sd
        self._prepare_runtime()
        input_device = self._find_device(sd, input_name, "input")
        voice_device = self._find_device(sd, voice_name, "output")
        try:
            self._voice_stream = sd.Stream(
                device=(input_device, voice_device), channels=(1, 2),
                samplerate=self.sample_rate, dtype="float32", callback=self._voice_callback,
                extra_settings=sd.WasapiSettings(auto_convert=True),
            )
            self._voice_stream.start()
        except Exception:
            self.close()
            raise
        self.ensure_monitor(ears_name)

    @staticmethod
    def _close_stream(stream) -> bool:
        if stream is None:
            return True
        # A failed stop must not leak the handle or prevent another stream from
        # closing. Device disconnects commonly make stop() itself fail.
        try:
            stream.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            return False
        return True

    def ensure_monitor(self, output_name: str | None) -> None:
        """Recover optional listening without interrupting mic + Others."""
        changed = output_name != self._monitor_name
        if changed:
            self._monitor_name = output_name
            self._monitor_retry_after = 0.0
            self._monitor_retry_delay = 1.0
        if not changed and self._ears_stream is not None and not self.monitor_error():
            self._monitor_retry_delay = 1.0
            return
        if not changed and time.monotonic() < self._monitor_retry_after:
            return
        if not self._close_stream(self._ears_stream):
            self._monitor_failure = "Local listening could not close its previous stream; its handle is retained and cleanup will retry. Microphone and Others continue."
            self._monitor_retry_after = time.monotonic() + self._monitor_retry_delay
            self._monitor_retry_delay = min(30.0, self._monitor_retry_delay * 2)
            return
        self._ears_stream = None
        self._monitor_failure = ""
        self._levels.pop("ears", None)
        with self._lock:
            for item in self._voices["ears"]:
                if item.get("trace") and not item["trace"]["earsEndReason"]:
                    item["trace"]["earsEndReason"] = "monitor_changed" if changed else "monitor_recovery"
            self._voices["ears"] = []
        if not output_name:
            return
        try:
            sd = self._sd
            device = self._find_device(sd, output_name, "output")
            self._ears_stream = sd.OutputStream(
                device=device, channels=2, samplerate=self.sample_rate,
                dtype="float32", callback=self._ears_callback,
                extra_settings=sd.WasapiSettings(auto_convert=True))
            self._ears_stream.start()
            self._monitor_retry_after = 0.0
        except Exception as exc:  # noqa: BLE001 - this bus must not stop the voice bus
            if self._close_stream(self._ears_stream):
                self._ears_stream = None
            self._monitor_failure = ("Local listening unavailable: " + str(exc)
                                     + " Microphone and Others continue; local listening will retry automatically.")
            self._monitor_retry_after = time.monotonic() + self._monitor_retry_delay
            self._monitor_retry_delay = min(30.0, self._monitor_retry_delay * 2)
            log.warning("soundboard monitor setup: %s", self._monitor_failure)

    def close(self) -> bool:
        with self._lock:
            self._render_generation += 1
        for stream_name in ("_voice_stream", "_ears_stream"):
            stream = getattr(self, stream_name)
            if stream is not None:
                if self._close_stream(stream):
                    setattr(self, stream_name, None)
                elif stream_name == "_voice_stream":
                    self._failure = "Voice audio stream could not close; its handle is retained and cleanup will retry."
                else:
                    self._monitor_failure = "Local listening stream could not close; its handle is retained and cleanup will retry."
        self._levels.clear()
        self._monitor_name = None
        if self._ears_stream is None:
            self._monitor_failure = ""
        with self._lock:
            for bus, voices in self._voices.items():
                for item in voices:
                    if item.get("trace") and not item["trace"][bus + "EndReason"]:
                        item["trace"][bus + "EndReason"] = "closed"
            self._voices = {"voice": [], "ears": []}
        if self._voice_stream is None:
            try:
                self._processor.close()
            except Exception:  # noqa: BLE001
                pass
        return self._voice_stream is None and self._ears_stream is None

    @staticmethod
    def _clip_signature(path: Path):
        try:
            stat = path.stat()
            return (str(path), stat.st_size, stat.st_mtime_ns)
        except OSError:
            return (str(path), None, None)

    def prepare_clip(self, clip_id: str, path: Path):
        """Decode/cache away from callbacks and without holding service locks."""
        if not self._decode_slots.acquire(blocking=False):
            raise RuntimeError("Soundboard decoder is busy; try this effect again shortly")
        with self._lock:
            self._pending_decodes += 1
        try:
            signature = self._clip_signature(path)
            with self._lock:
                samples = self._clips.get(clip_id)
                previous = self._clip_signatures.get(clip_id)
                if samples is not None and previous == signature:
                    self._clips.move_to_end(clip_id)
            if previous != signature:
                samples = None
            if samples is None:
                # One decoder owns temporary arrays; at most one extra request
                # waits. Neither lock is acquired by an audio callback.
                with self._decode_lock:
                    samples = self._load(path)
                if signature != self._clip_signature(path):
                    raise RuntimeError("The clip file changed while loading; play it again")
                with self._lock:
                    self._cache_clip(clip_id, samples, signature)
            # Includes completed preparation waiting for service ownership.
            # This prevents cached/decoded arrays piling up behind route work.
            return PreparedAudio(samples, signature, self._release_prepared)
        except BaseException:
            self._release_prepared()
            raise

    def _release_prepared(self) -> None:
        with self._lock:
            self._pending_decodes -= 1
        self._decode_slots.release()

    def _cache_clip(self, clip_id: str, samples, signature) -> None:
        """Caller holds clip lock. Active playheads keep evicted arrays alive."""
        previous = self._clips.pop(clip_id, None)
        self._clip_signatures.pop(clip_id, None)
        if previous is not None:
            self._clip_cache_bytes -= previous.nbytes
        size = samples.nbytes
        if size > self.clip_cache_bytes or self.clip_cache_entries < 1:
            return  # Large imports can play without occupying the reusable cache.
        while self._clips and (len(self._clips) >= self.clip_cache_entries
                               or self._clip_cache_bytes + size > self.clip_cache_bytes):
            old_id, old_samples = self._clips.popitem(last=False)
            self._clip_signatures.pop(old_id, None)
            self._clip_cache_bytes -= old_samples.nbytes
        self._clips[clip_id] = samples
        self._clip_signatures[clip_id] = signature
        self._clip_cache_bytes += size

    def trigger_prepared(self, clip_id: str, prepared, path: Path,
                         gain: float, voice: bool, ears: bool) -> str:
        samples, signature = prepared
        try:
            return self._trigger_samples(clip_id, samples, signature, path, gain, voice, ears)
        finally:
            if isinstance(prepared, PreparedAudio):
                prepared.close()

    def _trigger_samples(self, clip_id, samples, signature, path, gain, voice, ears) -> str:
        if signature != self._clip_signature(path):
            raise RuntimeError("The clip file changed while loading; play it again")
        error = self.health_error()
        if error:
            raise RuntimeError(error)
        if voice and not self._voice_stream:
            raise RuntimeError("Choose a mic and a Voice endpoint before playing clips")
        monitor_ready = (self._ears_stream is not None and not self.monitor_error())
        if ears and not voice and not monitor_ready:
            raise RuntimeError(self.monitor_error() or "Choose a monitoring output for pads with Me enabled")
        with self._lock:
            self._reap_completed()
            enabled_buses = ("voice",) if voice else ()
            if ears and monitor_ready:
                enabled_buses += ("ears",)
            self._admit_voice(clip_id, samples, enabled_buses)
            self._trace_sequence += 1
            trace = {"traceId": self._runtime_id + "-" + str(self._trace_sequence), "clipId": clip_id,
                     "queuedAtNs": time.monotonic_ns(), "sampleFrames": len(samples),
                     "decodedBytes": samples.nbytes, "requestedVoice": bool(voice),
                     "requestedMe": bool(ears), "meAvailable": bool(monitor_ready),
                     "voiceFirstFrameNs": 0, "earsFirstFrameNs": 0,
                     "voiceRenderedFrames": 0, "earsRenderedFrames": 0,
                     "voiceEndReason": "", "earsEndReason": ""}
            self._traces.append(trace)
            for bus, enabled in (("voice", voice), ("ears", ears and monitor_ready)):
                for item in self._voices[bus]:
                    if item["id"] == clip_id and item.get("trace"):
                        item["trace"][bus + "EndReason"] = "retriggered"
                self._voices[bus] = [item for item in self._voices[bus]
                                     if item["id"] != clip_id]
                if enabled:
                    self._voices[bus].append({"id": clip_id, "samples": samples,
                                              "pos": 0, "gain": gain, "trace": trace})
            return trace["traceId"]

    def _reap_completed(self) -> None:
        """Release final array references on service workers, outside callbacks."""
        for bus in self._voices:
            self._voices[bus] = [v for v in self._voices[bus] if v["pos"] < len(v["samples"])]

    def _admit_voice(self, clip_id: str, samples, buses: tuple[str, ...], *,
                     replace_buses: tuple[str, ...] | None = None) -> None:
        replaced = tuple(self._voices) if replace_buses is None else replace_buses
        for bus in buses:
            if sum(not (v["id"] == clip_id and bus in replaced) for v in self._voices[bus]) >= self.limits.max_voices_per_bus:
                raise RuntimeError("Soundboard playback is full; stop an effect before playing another")
        arrays = {id(v["samples"]): v["samples"] for bus, voices in self._voices.items()
                  for v in voices if not (v["id"] == clip_id and bus in replaced)}
        if buses:
            arrays[id(samples)] = samples
        if sum(a.nbytes for a in arrays.values()) > self.limits.max_active_bytes:
            raise RuntimeError("Active soundboard audio exceeds the memory budget; stop an effect first")

    def trigger(self, clip_id: str, path: Path, gain: float, voice: bool, ears: bool) -> str:
        error = self.health_error()
        if error:
            raise RuntimeError(error)
        if voice and self._voice_stream is None:
            raise RuntimeError("Choose a mic and a Voice endpoint before playing clips")
        if ears and not voice and (self._ears_stream is None or self.monitor_error()):
            raise RuntimeError(self.monitor_error() or "Choose a monitoring output for pads with Me enabled")
        return self.trigger_prepared(clip_id, self.prepare_clip(clip_id, path), path, gain, voice, ears)

    def test_tone(self, bus: str) -> None:
        """Queue a quiet, short tone on exactly one configured output bus."""
        if self.health_error():
            raise RuntimeError(self.health_error())
        if bus not in ("voice", "ears"):
            raise ValueError("choose Voice or Ears")
        if self._voice_stream is None or (bus == "ears" and
                (self._ears_stream is None or self.monitor_error())):
            raise RuntimeError("Apply the route before testing this output")
        np = self._np
        length = int(self.sample_rate * 0.45)
        seconds = np.arange(length, dtype=np.float32) / self.sample_rate
        envelope = np.minimum(1, seconds / 0.02) * np.minimum(1, (0.45 - seconds) / 0.07)
        samples = (np.sin(2 * np.pi * (660 if bus == "voice" else 440) * seconds)
                   * envelope * 0.16).astype(np.float32)
        with self._lock:
            self._reap_completed()
            self._admit_voice("__route_test__", samples, (bus,), replace_buses=(bus,))
            self._voices[bus] = [item for item in self._voices[bus]
                                 if item["id"] != "__route_test__"]
            self._voices[bus].append({"id": "__route_test__", "samples": samples,
                                      "pos": 0, "gain": 1.0})

    def stop_all(self) -> None:
        with self._lock:
            self._render_generation += 1
            for bus, voices in self._voices.items():
                for item in voices:
                    if item.get("trace") and not item["trace"][bus + "EndReason"]:
                        item["trace"][bus + "EndReason"] = "stopped"
            self._voices = {"voice": [], "ears": []}

    def remove_clip(self, clip_id: str) -> None:
        with self._lock:
            samples = self._clips.pop(clip_id, None)
            if samples is not None:
                self._clip_cache_bytes -= samples.nbytes
            self._clip_signatures.pop(clip_id, None)
            for bus in self._voices:
                for item in self._voices[bus]:
                    if item["id"] == clip_id and item.get("trace"):
                        item["trace"][bus + "EndReason"] = "removed"
                self._voices[bus] = [v for v in self._voices[bus] if v["id"] != clip_id]

    def verify_receiver(self, receiver_name: str) -> dict[str, Any]:
        """Identify a test signal at the cable's capture end, before app filters.

        Use the running mixer, not a second playback stream. Capture stays in
        memory, is never saved, and must match the probe rather than mic noise.
        """
        if self.health_error() or self._voice_stream is None:
            raise RuntimeError(self.health_error() or "Start audio before checking the cable")
        with self._lock:
            diagnostic_generation = self._render_generation
        np, sd = self._np, self._sd
        seconds = np.arange(int(self.sample_rate * .6), dtype=np.float32) / self.sample_rate
        envelope = np.minimum(1, seconds / .02) * np.minimum(1, (.6 - seconds) / .04)
        probe = (np.sin(2 * np.pi * (440 * seconds + 900 * seconds ** 2))
                 * envelope * .16).astype(np.float32)
        device = self._find_device(sd, receiver_name, "input")
        warning_count = self._warnings.get("voice", (0, ""))[0]
        # Fixed two-second diagnostic storage, even if a driver invokes the
        # callback repeatedly or delivers unexpected block sizes. No file writes.
        captured = np.empty(self.sample_rate * 2, dtype=np.float32)
        captured_frames = 0
        capture_warnings = 0
        capture_overflow = False

        def capture(indata, _frames, _time, status):
            nonlocal captured_frames, capture_warnings, capture_overflow
            take = min(len(indata), len(captured) - captured_frames)
            np.copyto(captured[captured_frames:captured_frames + take], indata[:take, 0])
            captured_frames += take
            capture_overflow |= take < len(indata)
            capture_warnings += int(bool(status))

        try:
            with sd.InputStream(device=device, channels=1, samplerate=self.sample_rate,
                                dtype="float32", callback=capture,
                                extra_settings=sd.WasapiSettings(auto_convert=True)):
                time.sleep(.15)
                with self._lock:
                    if diagnostic_generation != self._render_generation:
                        raise RuntimeError("Cable check was canceled by Stop or an audio route change")
                    self._reap_completed()
                    self._admit_voice("__cable_probe__", probe, ("voice",), replace_buses=())
                    self._voices["voice"].append({"id": "__cable_probe__", "samples": probe,
                                                  "pos": 0, "gain": 1.0})
                time.sleep(1.5)
        finally:
            with self._lock:
                self._voices["voice"] = [v for v in self._voices["voice"]
                                          if v["id"] != "__cable_probe__"]
        statuses = []
        if capture_warnings:
            statuses.append(f"Cable capture reported {capture_warnings} stream warnings")
        if capture_overflow:
            statuses.append("Cable diagnostic capture exceeded its two-second buffer")
        match = self._probe_match(captured[:captured_frames], probe)
        error = self.health_error()
        playback_warnings = self._warnings.get("voice", (0, ""))[0] - warning_count
        if playback_warnings:
            statuses.append("Mixer playback reported " + str(playback_warnings) + " stream warnings during the check")
        return {"passed": match["correlation"] >= .65 and match["gain"] >= .02
                          and not statuses and not error,
                "receiver": receiver_name, **match, "streamWarnings": statuses[:5],
                "error": error, "scope": "virtual_microphone"}

    def _probe_match(self, samples, probe) -> dict[str, float]:
        """FFT matched filter: silence, speech or an unrelated tone cannot pass."""
        np = self._np
        if len(samples) < len(probe) or not np.isfinite(samples).all():
            return {"correlation": 0.0, "gain": 0.0, "peak": 0.0}
        size = 1 << (len(samples) + len(probe) - 2).bit_length()
        correlation = np.fft.irfft(np.fft.rfft(samples, size)
                                   * np.fft.rfft(probe[::-1], size), size)
        valid = correlation[len(probe) - 1:len(samples)]
        offset = int(np.argmax(np.abs(valid)))
        window = samples[offset:offset + len(probe)]
        energy = float(np.dot(probe, probe))
        dot = abs(float(valid[offset]))
        denominator = math.sqrt(energy * float(np.dot(window, window)))
        return {"correlation": round(min(1.0, dot / denominator) if denominator else 0.0, 4),
                "gain": round(dot / energy if energy else 0.0, 4),
                "peak": round(float(np.max(np.abs(samples))), 4)}

    def preview(self, path: Path, gain: float, output_name: str) -> None:
        """Audition only on a physical playback endpoint, without opening a mic."""
        if self.close() is False:
            raise RuntimeError("The previous preview stream could not close; its handle is retained for retry")
        import numpy as np
        import sounddevice as sd
        self._np, self._sd = np, sd
        self._prepare_runtime()
        self._failure = ""
        samples = self._load(path)
        with self._lock:
            self._admit_voice("__preview__", samples, ("ears",))
        device = self._find_device(sd, output_name, "output")
        try:
            self._ears_stream = sd.OutputStream(
                device=device, channels=2, samplerate=self.sample_rate,
                dtype="float32", callback=self._ears_callback,
                extra_settings=sd.WasapiSettings(auto_convert=True))
            with self._lock:
                self._voices["ears"] = [{"id": "__preview__", "samples": samples,
                                         "pos": 0, "gain": gain}]
            self._ears_stream.start()
        except Exception:
            self.close()
            raise

    def playing_ids(self) -> set[str]:
        with self._lock:
            self._reap_completed()
            return {voice["id"] for bus in self._voices.values() for voice in bus}

    def diagnostics(self) -> dict[str, Any]:
        """Bounded playback trace and timing summaries; never called by audio."""
        with self._lock:
            arrays = {id(v["samples"]): v["samples"] for voices in self._voices.values() for v in voices}
            resources = {"cacheBytes": self._clip_cache_bytes, "cacheEntries": len(self._clips),
                         "activeBytes": sum(a.nbytes for a in arrays.values()),
                         "activeVoices": {bus: sum(v["pos"] < len(v["samples"]) for v in voices)
                                          for bus, voices in self._voices.items()},
                         "pendingDecodes": self._pending_decodes,
                         "pendingPrepares": self._pending_decodes,
                         "maxRetainedDecodedBytes": self.clip_cache_bytes + self.limits.max_active_bytes
                                                    + self.limits.max_pending_decodes * self.limits.max_clip_bytes,
                         "callbackScratchBytes": sum(a.nbytes for buffers in self._buffers.values()
                                                     for a in buffers.values())}
            traces = [dict(t) for t in self._traces]
        streams = {}
        for bus, stream in (("voice", self._voice_stream), ("ears", self._ears_stream)):
            streams[bus] = self._metrics[bus].summary(self._np) if bus in self._metrics else {}
            if stream is not None:
                for attr in ("latency", "cpu_load"):
                    try:
                        value = getattr(stream, attr)
                        streams[bus][attr] = list(value) if isinstance(value, tuple) else float(value)
                    except Exception as exc:  # noqa: BLE001 - concurrent native close is diagnostic data
                        if not isinstance(exc, AttributeError):
                            streams[bus]["telemetryError"] = str(exc)
        try:
            processor = dict(self._processor.health())
        except Exception as exc:  # noqa: BLE001
            processor = {"ready": False, "error": str(exc)}
        processor["error"] = self._processor_failure or processor.get("error", "")
        return {"runtimeId": self._runtime_id, "limits": asdict(self.limits), "resources": resources, "streams": streams,
                "microphoneProcessor": processor, "playbackTrace": traces,
                "scope": "PC rendering only; cable and receiving-app delivery require listener verification"}

    def _load(self, path: Path):
        """Decode into one bounded mono array, with bounded source/resample chunks.

        Header admission precedes allocating or reading audio. Multi-minute
        effects are accepted within the configured budget; excess is an explicit
        error instead of a truncated effect or a full-file temporary allocation.
        """
        if path.stat().st_size > self.limits.max_encoded_bytes:
            raise RuntimeError("Effect file exceeds the encoded audio memory budget")
        try:
            import soundfile as sf
        except ImportError:
            sf = None
        if sf is not None:
            with sf.SoundFile(str(path)) as source:
                frames, channels, rate = len(source), source.channels, source.samplerate
                target_frames = self._validate_clip_format(frames, channels, rate)
                def read(count):
                    return source.read(count, dtype="float32", always_2d=True)
                return self._decode_chunks(frames, channels, rate, target_frames, read, lambda: source.seek(0))
        elif path.suffix.lower() == ".wav":
            with wave.open(str(path), "rb") as source:
                channels, width, rate, frames = source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getnframes()
                if width not in (1, 2, 3, 4):
                    raise RuntimeError("unsupported WAV sample width")
                target_frames = self._validate_clip_format(frames, channels, rate)
                def read(count):
                    return self._decode_pcm(source.readframes(count), width, channels)
                return self._decode_chunks(frames, channels, rate, target_frames, read, source.rewind)
        else:
            raise RuntimeError("MP3, OGG and FLAC clips need the soundfile runtime")

    def _validate_clip_format(self, frames: int, channels: int, rate: int) -> int:
        if frames <= 0:
            raise RuntimeError("clip is empty")
        if not 1 <= channels <= self.limits.max_source_channels or not 1 <= rate <= self.limits.max_source_rate:
            raise RuntimeError("Effect source channels or sample rate exceed the decoder budget")
        if frames / rate > self.limits.max_clip_seconds:
            raise RuntimeError(f"Effect is longer than the configured {self.limits.max_clip_seconds:g}-second limit; it was not truncated")
        target_frames = max(1, round(frames * self.sample_rate / rate))
        if target_frames * 4 > self.limits.max_clip_bytes:
            raise RuntimeError("Effect exceeds the decoded audio memory budget; it was not truncated")
        return target_frames

    def _decode_pcm(self, raw, width, channels):
        np = self._np
        if width == 3:
            packed = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
            data = (packed[:, 0].astype(np.int32) | (packed[:, 1].astype(np.int32) << 8) | (packed[:, 2].astype(np.int32) << 16))
            data = ((data ^ 0x800000) - 0x800000).astype(np.float32)
            data /= 8388608.0
        else:
            dtype, divisor, offset = {1: (np.uint8, 128.0, -1.0), 2: ("<i2", 32768.0, 0.0), 4: ("<i4", 2147483648.0, 0.0)}[width]
            data = np.frombuffer(raw, dtype=dtype).astype(np.float32)
            data /= divisor
            if offset:
                data += offset
        return data.reshape(-1, channels)

    def _decode_chunks(self, frames, channels, rate, target_frames, read, rewind):
        np = self._np
        output = np.empty(target_frames, dtype=np.float32)
        chunk_frames = self.limits.decode_chunk_frames
        mono_buffer = np.empty(chunk_frames + 1, dtype=np.float32)
        source_axis = np.arange(chunk_frames + 1, dtype=np.float64)
        energy = np.zeros(channels, dtype=np.float64)
        mono_energy = 0.0
        step = (frames - 1) / (target_frames - 1) if target_frames > 1 else 0.0

        def render(channel=None):
            nonlocal mono_energy
            source_pos, dest_pos, previous = 0, 0, 0.0
            while source_pos < frames:
                count = min(chunk_frames, frames - source_pos)
                data = read(count)
                if len(data) != count:
                    raise RuntimeError("Effect decoder ended before the declared audio length")
                if not np.isfinite(data).all():
                    raise RuntimeError("clip contains invalid audio samples")
                mono = data[:, channel] if channel is not None else data.mean(axis=1)
                if channel is None and channels > 1:
                    energy[:] += np.einsum("ij,ij->j", data, data, dtype=np.float64)
                    mono_energy += float(np.dot(mono.astype(np.float64), mono))
                if rate == self.sample_rate:
                    output[source_pos:source_pos + count] = mono
                    dest_pos += count
                else:
                    prefix = int(source_pos > 0)
                    if prefix:
                        mono_buffer[0] = previous
                    mono_buffer[prefix:prefix + count] = mono
                    first_source = source_pos - prefix
                    last_source = source_pos + count - 1
                    # Delay interpolation until both source neighbours exist;
                    # retain only one sample across source chunk boundaries.
                    dest_end = target_frames if last_source == frames - 1 else min(target_frames, int(math.floor(last_source / step)) + 1) if step else target_frames
                    while dest_pos < dest_end:
                        block_end = min(dest_end, dest_pos + chunk_frames)
                        positions = np.arange(dest_pos, block_end, dtype=np.float64)
                        positions *= step
                        positions -= first_source
                        output[dest_pos:block_end] = np.interp(positions, source_axis[:prefix + count], mono_buffer[:prefix + count])
                        dest_pos = block_end
                previous = float(mono[-1])
                source_pos += count
            if dest_pos != target_frames:
                raise RuntimeError("Effect resampler did not produce the declared audio length")

        render()
        if channels > 1:
            strongest = int(np.argmax(energy))
            if mono_energy < energy[strongest] * .01:
                # Preserve the existing anti-cancellation behavior using whole
                # clip energy, not a per-chunk channel choice that could click.
                rewind()
                render(strongest)
        return output

    def _downmix(self, data):
        """Avoid losing phase-inverted stereo effects in a mono microphone."""
        np = self._np
        mono = data.mean(axis=1)
        energy = np.mean(data * data, axis=0)
        strongest = int(np.argmax(energy))
        if np.mean(mono * mono) < energy[strongest] * .01:
            mono = data[:, strongest].copy()
        return mono

    def _mix(self, bus: str, frames: int):
        """Offline/test helper. Audio callbacks use caller-owned ``_mix_into``."""
        out = self._np.empty(frames, dtype=self._np.float32)
        for start in range(0, frames, self.limits.callback_chunk_frames):
            self._mix_into(bus, out[start:start + self.limits.callback_chunk_frames])
        return out

    def _mix_into(self, bus: str, out) -> None:
        np = self._np
        frames = len(out)
        out.fill(0)
        # A UI trigger/stop must never stall the microphone callback. If a clip
        # update briefly owns the lock, defer clips for this block and preserve
        # the physical microphone unchanged. Playheads stay at the same sample.
        if not self._lock.acquire(blocking=False):
            self._metrics[bus].deferred += 1
            self._record_warning(bus, "Clip update deferred to preserve the audio callback deadline")
            return
        try:
            for voice in self._voices[bus]:
                start = voice["pos"]
                take = min(frames, len(voice["samples"]) - start)
                if take > 0:
                    scaled = self._buffers[bus]["scaled"][:take]
                    np.multiply(voice["samples"][start:start + take], voice["gain"], out=scaled)
                    np.add(out[:take], scaled, out=out[:take])
                    voice["pos"] += take
                    trace = voice.get("trace")
                    if trace is not None:
                        if not trace[bus + "FirstFrameNs"]:
                            trace[bus + "FirstFrameNs"] = time.monotonic_ns()
                        trace[bus + "RenderedFrames"] += take
                        if voice["pos"] == len(voice["samples"]):
                            trace[bus + "EndReason"] = "completed"
            np.clip(out, -1.0, 1.0, out=out)
        finally:
            self._lock.release()

    def _voice_callback(self, indata, outdata, frames, _time, status) -> None:
        started = time.perf_counter_ns()
        metric = self._metrics["voice"]
        metric.begin(started, frames, status)
        self._record_warning("voice", status)
        try:
            buffers = self._buffers["voice"]
            for start in range(0, frames, self.limits.callback_chunk_frames):
                count = min(frames - start, self.limits.callback_chunk_frames)
                captured = indata[start:start + count, 0]
                clips = buffers["clips"][:count]
                mic = buffers["mic"][:count]
                mixed = buffers["mixed"][:count]
                if self._processor_failure:
                    self._np.copyto(mic, captured)
                else:
                    try:
                        self._processor.process(captured, mic, count)
                    except Exception as exc:  # noqa: BLE001 - voice continuity
                        self._processor_failure = "Microphone processor failed; bypass active: " + str(exc)
                        self._np.copyto(mic, captured)
                self._mix_into("voice", clips)
                self._np.add(clips, mic, out=mixed)
                self._record_level("mic", captured)
                self._record_level("sounds", clips)
                self._record_level("voice", mixed)
                self._np.clip(mixed, -1.0, 1.0, out=mixed)
                outdata[start:start + count] = mixed[:, None]
        except Exception as exc:  # noqa: BLE001 - PortAudio callbacks cannot propagate
            self._failure = "Voice audio failed: " + str(exc)
            # A pad/level failure must not suppress speech while recovery is
            # scheduled. If capture itself is invalid, silence is the fallback.
            try:
                self._np.copyto(outdata, indata[:, :1])
                self._np.clip(outdata, -1.0, 1.0, out=outdata)
            except Exception:  # noqa: BLE001
                outdata.fill(0)
        finally:
            metric.end(time.perf_counter_ns() - started, frames)

    def _ears_callback(self, outdata, frames, _time, status) -> None:
        started = time.perf_counter_ns()
        metric = self._metrics["ears"]
        metric.begin(started, frames, status)
        self._record_warning("ears", status)
        try:
            for start in range(0, frames, self.limits.callback_chunk_frames):
                count = min(frames - start, self.limits.callback_chunk_frames)
                mixed = self._buffers["ears"]["clips"][:count]
                self._mix_into("ears", mixed)
                self._record_level("ears", mixed)
                outdata[start:start + count] = mixed[:, None]
        except Exception as exc:  # noqa: BLE001
            self._monitor_failure = "Local listening failed: " + str(exc)
            outdata.fill(0)
        finally:
            metric.end(time.perf_counter_ns() - started, frames)


class SoundboardService:
    """Owns the on-disk pad library, selected endpoint IDs, and mixer lifecycle."""

    def __init__(self, root: Path, renderer_factory=SoundboardRenderer,
                 defaults_root: Path | None = None, *,
                 runtime_limits: AudioRuntimeLimits | None = None) -> None:
        self.root = root
        self.clips_dir = root / "soundboard"
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.clips_dir / "library.json"
        self.runtime_limits = runtime_limits or AudioRuntimeLimits()
        self._renderer_factory = ((lambda: SoundboardRenderer(limits=self.runtime_limits))
                                  if renderer_factory is SoundboardRenderer else renderer_factory)
        self._renderer: SoundboardRenderer | None = None
        self._preview_renderer: SoundboardRenderer | None = None
        self._lock = threading.RLock()
        self._error = ""
        self._playing: set[str] = set()
        self._play_generation = 0
        self._duration_cache: dict[str, tuple[int, int, float]] = {}
        self._metadata_refresh_after = 0.0
        self._metadata_lock = threading.Lock()
        self._closed = False
        self._retry_after = 0.0
        self._retry_delay = 1.0
        self._active_names = None
        self._diagnostic_lock = threading.Lock()
        self.defaults_root = defaults_root or (resource_root() / DEFAULT_PACK_DIR)
        self._data = self._load()
        self._install_default_pack_once()
        self.refresh_clip_metadata(force=True)

    def _load(self) -> dict[str, Any]:
        default = {"version": 1, "defaultPackVersion": 0,
                   "config": {"inputId": "", "voiceOutputId": "",
                   "earsOutputId": "", "layout": "a"}, "clips": []}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("library is not an object")
            for key, value in default.items():
                data.setdefault(key, value)
            data["config"] = {**default["config"], **(data.get("config") or {})}
            data["clips"] = [c for c in data.get("clips", []) if isinstance(c, dict)]
            return data
        except (OSError, ValueError, json.JSONDecodeError):
            return default

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def install_pack(self, pack_root: Path) -> int:
        """Add a verified collection once, retaining edits and remembered removals.

        Pack installation shares the atomic library/audio transaction used by
        starter sounds. It never changes routing or presentation pad slots.
        """
        manifest = json.loads((pack_root / "manifest.json").read_text(encoding="utf-8"))
        pack_id = manifest.get("id", "")
        if (not isinstance(pack_id, str) or
                not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", pack_id) or
                not isinstance(manifest.get("clips"), list)):
            raise ValueError("invalid sound pack manifest")
        entries, seen = [], set()
        audio_root = (pack_root / "audio").resolve()
        for entry in manifest["clips"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("label"), str):
                raise ValueError("invalid sound pack entry")
            key = str(entry.get("key", ""))
            filename = str(entry.get("file", ""))
            artwork = str(entry.get("artwork", ""))
            if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", key) or key in seen or
                    Path(filename).name != filename or
                    Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS or
                    artwork != key + ".webp"):
                raise ValueError("unsafe sound pack entry")
            source = audio_root / filename
            if source.resolve().parent != audio_root:
                raise ValueError("sound pack audio is outside its directory")
            if (source.stat().st_size > self.runtime_limits.max_encoded_bytes or
                    hashlib.sha256(source.read_bytes()).hexdigest() != entry.get("sha256")):
                raise ValueError(f"sound pack failed integrity check: {filename}")
            tags = entry.get("tags", [])
            if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
                raise ValueError("invalid sound pack search tags")
            seen.add(key)
            entries.append((entry, source))
        with self._lock:
            before = copy.deepcopy(self._data)
            known = set(self._data.get("installedPackKeys", {}).get(pack_id, []))
            copies, installed = [], []
            for entry, source in entries:
                key = entry["key"]
                if key in known:
                    continue
                clip_id = f"{pack_id}-{key}"
                if any(c.get("id") == clip_id for c in self._data["clips"]):
                    raise ValueError("sound pack clip ID collision")
                filename = clip_id + source.suffix.lower()
                copies.append((source, self.clips_dir / filename))
                installed.append({
                    "id": clip_id, "file": filename, "label": str(entry["label"])[:48],
                    "emoji": str(entry.get("emoji", "♪"))[:48],
                    "collection": str(manifest.get("label", pack_id))[:48],
                    "packId": pack_id, "tags": entry.get("tags", [])[:24],
                    "artwork": "/static/meme-art/" + entry["artwork"],
                    "voice": True, "ears": True, "gain": .75,
                })
            if not installed:
                return 0
            self._data["clips"].extend(installed)
            self._data.setdefault("installedPackKeys", {})[pack_id] = sorted(known | seen)
            try:
                self._save_with_audio_replacements(copies)
            except Exception:
                self._data = before
                raise
        self.refresh_clip_metadata(force=True)
        return len(installed)

    def _save_with_audio_replacements(self, copies: list[tuple[Path, Path]]) -> None:
        """Stage default-pack changes and roll files back if atomic JSON save fails.

        Caller restores in-memory data on error. Backups live beside managed
        files, keeping replacements on the same filesystem. Retired files are
        removed only after the new library has committed.
        """
        transaction = uuid.uuid4().hex
        staged, backups, applied = [], [], []
        preserve_backups = False
        try:
            for index, (source, destination) in enumerate(copies):
                if destination.parent.resolve() != self.clips_dir.resolve():
                    raise ValueError("default sound destination is outside the managed library")
                stage = self.clips_dir / f".update-{transaction}-{index}.tmp"
                backup = self.clips_dir / f".rollback-{transaction}-{index}.tmp"
                staged.append((stage, destination))
                existed = destination.is_file()
                backups.append((backup, destination, existed))
                shutil.copy2(source, stage)
                if existed:
                    shutil.copy2(destination, backup)
            for stage, destination in staged:
                stage.replace(destination)
                applied.append(destination)
            self._save()
        except Exception:
            try:
                for backup, destination, existed in backups:
                    if destination in applied:
                        if existed:
                            backup.replace(destination)
                        else:
                            destination.unlink(missing_ok=True)
            except OSError:
                # Preserve recoverable backup files if the filesystem itself
                # prevents rollback; never erase the remaining recovery copy.
                preserve_backups = True
                log.exception("Default sound rollback failed; retained .rollback files in %s", self.clips_dir)
                raise
            raise
        finally:
            for stage, _destination in staged:
                try:
                    stage.unlink(missing_ok=True)
                except OSError:
                    log.warning("could not remove default sound staging file %s", stage.name)
            if not preserve_backups:
                for backup, _destination, _existed in backups:
                    try:
                        backup.unlink(missing_ok=True)
                    except OSError:
                        log.warning("could not remove default sound backup file %s", backup.name)

    def _default_manifest(self) -> dict[str, Any]:
        """Read and verify the bundled CC0 pack before copying any file."""
        manifest_path = self.defaults_root / "manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("clips"), list):
            raise ValueError("default sound manifest is invalid")
        version = data.get("version")
        if not isinstance(version, int) or version < 1:
            raise ValueError("default sound manifest has no valid version")
        seen: set[str] = set()
        for clip in data["clips"]:
            if not isinstance(clip, dict):
                raise ValueError("default sound entry is invalid")
            key = str(clip.get("key", ""))
            filename = str(clip.get("file", ""))
            if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", key) or key in seen or
                    Path(filename).name != filename or
                    Path(filename).suffix.lower() not in SUPPORTED_EXTENSIONS):
                raise ValueError("default sound entry has an unsafe key or filename")
            source = self.defaults_root / filename
            expected = str(clip.get("sha256", "")).lower()
            actual = hashlib.sha256(source.read_bytes()).hexdigest()
            if len(expected) != 64 or actual != expected:
                raise ValueError(f"default sound failed integrity check: {filename}")
            seen.add(key)
        return data

    def _install_default_pack_once(self) -> None:
        """Install a new bundled pack once without resurrecting deleted pads."""
        try:
            manifest = self._default_manifest()
            if int(self._data.get("defaultPackVersion", 0)) < manifest["version"]:
                if int(self._data.get("defaultPackVersion", 0)) == 0:
                    self.restore_defaults(reset=True, manifest=manifest)
                else:
                    self._upgrade_default_pack(manifest)
        except FileNotFoundError:
            log.info("no bundled default sound pack found")
        except Exception as exc:  # noqa: BLE001 - custom clips must still remain usable
            log.warning("default sound pack was not installed: %s", exc)

    def _upgrade_default_pack(self, manifest: dict[str, Any]) -> None:
        """Refresh installed default audio without resurrecting removed pads."""
        old_gains = {"applause": .72, "success": .78, "wrong": .76,
                     "bell": .76, "pop": .82}
        with self._lock:
            before = copy.deepcopy(self._data)
            copies = []
            # Releases before presentation.json did not remember removed defaults.
            # All twelve original keys count as known, even when the user deleted
            # them; only genuinely new manifest keys may be added automatically.
            known = set(self._data.get("defaultPackKeys") or LEGACY_DEFAULT_KEYS)
            installed_keys = {str(c.get("defaultKey")) for c in self._data["clips"]}
            for clip in self._data["clips"]:
                entry = next((item for item in manifest["clips"]
                              if item["key"] == clip.get("defaultKey")), None)
                if entry is None:
                    continue
                source = self.defaults_root / str(entry["file"])
                copies.append((source, self.clips_dir / str(clip["file"])))
                if (clip.get("defaultKey") in old_gains and
                        clip.get("gain") == old_gains[clip["defaultKey"]]):
                    clip["gain"] = _clamp(entry.get("gain", clip["gain"]))
            for entry in manifest["clips"]:
                key = str(entry["key"])
                if key in known or key in installed_keys:
                    continue
                source = self.defaults_root / str(entry["file"])
                destination_name = f"default-{key}{source.suffix.lower()}"
                copies.append((source, self.clips_dir / destination_name))
                self._data["clips"].append({
                    "id": f"default-{key}", "defaultKey": key,
                    "file": destination_name, "label": str(entry.get("label", key))[:48],
                    "emoji": str(entry.get("emoji", "♪"))[:48],
                    "voice": bool(entry.get("voice", True)),
                    "ears": bool(entry.get("ears", True)),
                    "gain": _clamp(entry.get("gain", 1.0)),
                })
            self._data["defaultPackKeys"] = [str(e["key"]) for e in manifest["clips"]]
            self._data["defaultPackVersion"] = int(manifest["version"])
            try:
                self._save_with_audio_replacements(copies)
            except Exception:
                self._data = before
                raise

    def restore_defaults(self, reset: bool = True,
                         manifest: dict[str, Any] | None = None) -> int:
        """Restore verified starter pads while preserving every user-imported clip."""
        manifest = manifest or self._default_manifest()
        with self._lock:
            before = copy.deepcopy(self._data)
            copies = []
            old_defaults = [c for c in self._data["clips"] if c.get("defaultKey")]
            custom = [c for c in self._data["clips"] if not c.get("defaultKey")]
            current = {str(c.get("defaultKey")): c for c in old_defaults}
            installed: list[dict[str, Any]] = []
            for entry in manifest["clips"]:
                key = str(entry["key"])
                existing = current.get(key)
                if existing is not None and not reset:
                    installed.append(existing)
                    continue
                source = self.defaults_root / str(entry["file"])
                destination_name = f"default-{key}{source.suffix.lower()}"
                copies.append((source, self.clips_dir / destination_name))
                installed.append({
                    "id": f"default-{key}", "defaultKey": key,
                    "file": destination_name, "label": str(entry.get("label", key))[:48],
                    "emoji": str(entry.get("emoji", "♪"))[:48],
                    "voice": bool(entry.get("voice", True)),
                    "ears": bool(entry.get("ears", True)),
                    "gain": _clamp(entry.get("gain", 1.0)),
                })
            valid_files = {str(c["file"]) for c in installed}
            self._data["clips"] = installed + custom
            self._data["defaultPackKeys"] = [str(e["key"]) for e in manifest["clips"]]
            self._data["defaultPackVersion"] = int(manifest["version"])
            try:
                self._save_with_audio_replacements(copies)
            except Exception:
                self._data = before
                raise
            if reset:
                self.stop_all()
            for clip in old_defaults:
                filename = str(clip.get("file", ""))
                if filename and filename not in valid_files:
                    try:
                        (self.clips_dir / filename).unlink(missing_ok=True)
                    except OSError:
                        log.warning("could not remove retired default sound %s", filename)
        self.refresh_clip_metadata(force=True)
        return len(installed)

    def signal_state(self) -> dict[str, Any]:
        """Lightweight live meters; never inspect clips or enumerate devices."""
        # Renderer references are published atomically after startup. Holding the
        # service lock here would make 70 ms meter reads wait for WASAPI opens or
        # decoding. Levels/health are snapshots, with no file or device lookup.
        renderer = self._renderer
        health = renderer.health_error() if renderer and hasattr(renderer, "health_error") else ""
        return {"runtime": "ready" if renderer and not health else "setup_required",
                "levels": renderer.levels() if renderer and not health and hasattr(renderer, "levels") else {}}

    def snapshot(self, outputs: list[dict[str, Any]] | None = None,
                 inputs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        with self._lock:
            configured = bool(self._data["config"]["inputId"] and self._data["config"]["voiceOutputId"])
            playing = self._playing
            if self._renderer is not None and hasattr(self._renderer, "playing_ids"):
                playing = self._renderer.playing_ids()
            health = self._renderer.health_error() if self._renderer is not None and hasattr(self._renderer, "health_error") else ""
            monitor_error = self._renderer.monitor_error() if self._renderer and hasattr(self._renderer, "monitor_error") else ""
            return {"clips": [dict(c, playing=c["id"] in playing,
                                   duration=self._clip_duration(c)) for c in self._data["clips"]],
                    "config": dict(self._data["config"]), "configured": configured,
                    "runtime": "ready" if self._renderer and not health else "setup_required",
                    "error": health or monitor_error or self._error, "outputs": outputs or [], "inputs": inputs or [],
                    "streamWarnings": self._renderer.stream_warnings() if self._renderer and hasattr(self._renderer, "stream_warnings") else {},
                    "levels": self._renderer.levels() if self._renderer and not health and hasattr(self._renderer, "levels") else {}}

    def refresh_clip_metadata(self, *, force: bool = False) -> None:
        """Refresh managed-file metadata on library/lifecycle work, never snapshot.

        External edits are rechecked at most every 30 seconds. Playback still
        verifies a fresh signature before using any decoded array.
        """
        now = time.monotonic()
        if not force and now < self._metadata_refresh_after:
            return
        if not self._metadata_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                clips = [dict(c) for c in self._data["clips"]]
            valid_paths = {str(self.clips_dir / str(c.get("file", ""))) for c in clips}
            for path in list(self._duration_cache):
                if path not in valid_paths:
                    self._duration_cache.pop(path, None)
            for clip in clips:
                self._clip_duration(clip, refresh=True)
            self._metadata_refresh_after = now + 30.0
        finally:
            self._metadata_lock.release()

    def _clip_duration(self, clip: dict[str, Any], *, refresh: bool = False) -> float:
        """Return cached clip length; file work is explicitly refreshed elsewhere."""
        path = self.clips_dir / str(clip.get("file", ""))
        cached = self._duration_cache.get(str(path))
        if not refresh:
            return cached[2] if cached else 0.0
        try:
            stat = path.stat()
            if cached and cached[:2] == (stat.st_size, stat.st_mtime_ns):
                return cached[2]
            if path.suffix.lower() == ".wav":
                try:
                    with wave.open(str(path), "rb") as source:
                        duration = source.getnframes() / source.getframerate()
                except wave.Error:
                    # Float/extensible WAV is playable through libsndfile too.
                    import soundfile as sf
                    duration = float(sf.info(str(path)).duration)
            else:
                import soundfile as sf
                duration = float(sf.info(str(path)).duration)
            self._duration_cache[str(path)] = (stat.st_size, stat.st_mtime_ns, duration)
            return duration
        except (OSError, ValueError, RuntimeError, ImportError, wave.Error):
            self._duration_cache[str(path)] = (0, 0, 0.0)
            return 0.0

    def import_clip(self, source: Path, label: str | None = None,
                    voice: bool = True, ears: bool = False, gain: float = 1.0) -> dict[str, Any]:
        source = source.expanduser().resolve()
        if not source.is_file() or source.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError("choose a WAV, MP3, OGG, or FLAC audio file")
        if source.stat().st_size > self.runtime_limits.max_encoded_bytes:
            raise ValueError("Effect file exceeds the configured encoded audio budget")
        clip_id = uuid.uuid4().hex[:12]
        destination = self.clips_dir / (clip_id + source.suffix.lower())
        try:
            shutil.copy2(source, destination)
        except Exception:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                log.warning("could not remove partial sound import %s", destination.name)
            raise
        clip = {"id": clip_id, "file": destination.name, "label": (label or source.stem)[:48],
                "emoji": "♪", "voice": bool(voice), "ears": bool(ears), "gain": _clamp(gain)}
        with self._lock:
            before = copy.deepcopy(self._data)
            self._data["clips"].append(clip)
            try:
                self._save()
            except Exception:
                self._data = before
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    log.warning("could not remove failed sound import %s", destination.name)
                raise
        self.refresh_clip_metadata(force=True)
        return dict(clip)

    def update_clip(self, clip_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {"label", "emoji", "voice", "ears", "gain"}
        with self._lock:
            clip = next((c for c in self._data["clips"] if c.get("id") == clip_id), None)
            if clip is None:
                raise ValueError("unknown soundboard clip")
            before = copy.deepcopy(self._data)
            for key in allowed & changes.keys():
                if key == "gain": clip[key] = _clamp(changes[key])
                elif key in ("voice", "ears"): clip[key] = bool(changes[key])
                else: clip[key] = str(changes[key])[:48]
            try:
                self._save()
            except Exception:
                self._data = before
                raise
            self._play_generation += 1
            return dict(clip)

    def remove_clip(self, clip_id: str) -> bool:
        with self._lock:
            clip = next((c for c in self._data["clips"] if c.get("id") == clip_id), None)
            if clip is None:
                return False
            before = copy.deepcopy(self._data)
            self._data["clips"].remove(clip)
            try:
                self._save()
            except Exception:
                self._data = before
                raise
            self._play_generation += 1
            self._playing.discard(clip_id)
            if self._renderer and hasattr(self._renderer, "remove_clip"):
                self._renderer.remove_clip(clip_id)
        try:
            (self.clips_dir / str(clip.get("file", ""))).unlink(missing_ok=True)
        except OSError:
            log.warning("could not delete soundboard clip %s", clip_id)
        return True

    def configure(self, config: dict[str, Any], outputs: list[dict[str, Any]],
                  inputs: list[dict[str, Any]]) -> None:
        # Phone navigation preferences must not reopen streams or overwrite a
        # route that was just changed on the desktop.
        if "layout" in config and not any(key in config for key in
                                          ("inputId", "voiceOutputId", "earsOutputId")):
            layout = str(config["layout"]).lower()
            if layout not in ("a", "b"):
                raise ValueError("soundboard layout must be A or B")
            with self._lock:
                before = dict(self._data["config"])
                self._data["config"]["layout"] = layout
                try:
                    self._save()
                except Exception:
                    self._data["config"] = before
                    raise
            return
        output_map = {str(d.get("id")): str(d.get("name")) for d in outputs}
        input_map = {str(d.get("id")): str(d.get("name")) for d in inputs}
        values = {key: str(config.get(key, self._data["config"].get(key, "")))
                  for key in ("inputId", "voiceOutputId", "earsOutputId")}
        layout = str(config.get("layout", self._data["config"].get("layout", "a"))).lower()
        if layout not in ("a", "b"):
            raise ValueError("soundboard layout must be A or B")
        if values["inputId"] and values["inputId"] not in input_map:
            raise ValueError("selected microphone is no longer available")
        for key in ("voiceOutputId", "earsOutputId"):
            if values[key] and values[key] not in output_map:
                raise ValueError("selected output is no longer available")
        with self._lock:
            before = dict(self._data["config"])
            self._data["config"].update(values); self._data["config"]["layout"] = layout
            try:
                self._save()
            except Exception:
                self._data["config"] = before
                raise
            self._closed = False
            self._play_generation += 1
            self._retry_after = 0.0; self._retry_delay = 1.0
            names = (input_map.get(values["inputId"]), output_map.get(values["voiceOutputId"]),
                     output_map.get(values["earsOutputId"]) or None)
            if (self._renderer and self._active_names and hasattr(self._renderer, "ensure_monitor")
                    and not self._renderer.health_error() and names[:2] == self._active_names[:2]):
                self._renderer.ensure_monitor(names[2])
                self._active_names = names
                self._error = ""
                return
            self._connect(values, output_map, input_map)

    def _connect(self, config, output_map, input_map) -> None:
        """Caller holds service lock. Retries never rewrite persisted settings."""
        self._play_generation += 1
        self._error = ""; self._playing.clear(); self._active_names = None
        if self._renderer:
            if self._renderer.close() is False:
                self._error = "Previous audio stream cleanup failed. Deckster retains its handle and will retry before opening another route."
                self._retry_after = time.monotonic() + self._retry_delay
                self._retry_delay = min(30.0, self._retry_delay * 2)
                return
            self._renderer = None
        if not config["inputId"] or not config["voiceOutputId"]:
            return
        renderer = None
        try:
            names = (input_map[config["inputId"]], output_map[config["voiceOutputId"]],
                     output_map.get(config["earsOutputId"]) or None)
            renderer = self._renderer_factory()
            renderer.start(*names)
            self._renderer = renderer
            self._active_names = names
            self._retry_after = 0.0
        except Exception as exc:  # noqa: BLE001 - keep retrying after transient driver errors
            if renderer:
                if renderer.close() is False:
                    self._renderer = renderer
            self._error = str(exc) + " Deckster will retry automatically."
            self._retry_after = time.monotonic() + self._retry_delay
            self._retry_delay = min(30.0, self._retry_delay * 2)
            log.warning("soundboard setup: %s", self._error)

    def ensure_started(self, outputs: list[dict[str, Any]], inputs: list[dict[str, Any]]) -> None:
        """Restore and recover the saved route, with bounded retry frequency."""
        self.refresh_clip_metadata()
        with self._lock:
            if self._closed:
                return
            config = dict(self._data["config"])
            output_map = {str(d.get("id")): str(d.get("name")) for d in outputs}
            input_map = {str(d.get("id")): str(d.get("name")) for d in inputs}
            if not config.get("inputId") or not config.get("voiceOutputId"):
                return
            if config["inputId"] not in input_map or config["voiceOutputId"] not in output_map:
                if self._renderer and time.monotonic() >= self._retry_after:
                    self._play_generation += 1
                    if self._renderer.close() is not False:
                        self._renderer = None
                    else:
                        self._retry_after = time.monotonic() + self._retry_delay
                        self._retry_delay = min(30.0, self._retry_delay * 2)
                self._error = "Saved audio device is disconnected. Deckster will reconnect when it returns."
                return
            from .routing import is_virtual
            voice = next(d for d in outputs if str(d.get("id")) == config["voiceOutputId"])
            mic = next(d for d in inputs if str(d.get("id")) == config["inputId"])
            if not is_virtual(voice) or is_virtual(mic):
                # A legacy speaker selection must not unexpectedly play the
                # physical microphone through speakers on the next launch.
                self._error = "Open Audio routing and use recommended setup: the saved route uses a physical output or virtual microphone."
                return
            health = self._renderer.health_error() if self._renderer and hasattr(self._renderer, "health_error") else ""
            names = (input_map[config["inputId"]], output_map[config["voiceOutputId"]],
                     output_map.get(config["earsOutputId"]) or None)
            if self._renderer and self._active_names and not health and names[:2] == self._active_names[:2]:
                if hasattr(self._renderer, "ensure_monitor"):
                    self._renderer.ensure_monitor(names[2])
                    self._active_names = names
                elif names != self._active_names:
                    self._connect(config, output_map, input_map)
                self._retry_delay = 1.0
                return
            if time.monotonic() >= self._retry_after:
                if health:
                    log.warning("soundboard recovering saved route: %s", health)
                self._connect(config, output_map, input_map)

    def verify_receiver(self, outputs, inputs) -> dict[str, Any]:
        from .routing import cable_pairs, route_issue
        if not self._diagnostic_lock.acquire(blocking=False):
            raise ValueError("A cable check is already running.")
        try:
            with self._lock:
                config = dict(self._data["config"])
                snapshot = {"inputs": inputs, "outputs": outputs}
                issue = route_issue(config, snapshot)
                if issue:
                    raise ValueError(issue)
                renderer = self._renderer
                if renderer is None:
                    raise ValueError(self._error or "Start audio before checking the cable")
                receiver = next(p[1] for p in cable_pairs(outputs, inputs)
                                if p[0]["id"] == config["voiceOutputId"])
            result = renderer.verify_receiver(receiver["name"])
            with self._lock:
                if self._renderer is not renderer or self._data["config"] != config:
                    raise ValueError("Audio route changed during the check. Run it again.")
            log.info("soundboard cable check passed=%s correlation=%s gain=%s",
                     result["passed"], result["correlation"], result["gain"])
            return result
        finally:
            self._diagnostic_lock.release()

    @property
    def play_generation(self) -> int:
        """Reserve a play before it waits for controller/lifecycle work.

        This integer read must not wait for device opens under the service lock.
        Validation occurs under that lock when playback actually begins.
        """
        return self._play_generation

    def play(self, clip_id: str, *, expected_generation: int | None = None) -> str | None:
        with self._lock:
            if expected_generation is not None and expected_generation != self._play_generation:
                return
            clip = next((c for c in self._data["clips"] if c.get("id") == clip_id), None)
            if clip is None:
                raise ValueError("unknown soundboard clip")
            if not clip.get("voice") and not clip.get("ears"):
                raise ValueError("enable Voice or Ears for this pad")
            if self._renderer is None:
                raise RuntimeError(self._error or "Choose a mic and a Voice endpoint first")
            clip = dict(clip)
            renderer, generation = self._renderer, self._play_generation
            path = self.clips_dir / str(clip["file"])
        if not path.is_file():
            raise RuntimeError("the clip file is missing")
        prepared = renderer.prepare_clip(clip_id, path) if hasattr(renderer, "prepare_clip") else None
        with self._lock:
            if self._closed or generation != self._play_generation or self._renderer is not renderer:
                if isinstance(prepared, PreparedAudio):
                    prepared.close()
                if not any(c.get("id") == clip_id for c in self._data["clips"]) and hasattr(renderer, "remove_clip"):
                    renderer.remove_clip(clip_id)
                return  # Stop/route/library changes cancel an outstanding decode.
            if prepared is not None:
                trace_id = renderer.trigger_prepared(clip_id, prepared, path, float(clip.get("gain", 1.0)),
                                          bool(clip.get("voice")), bool(clip.get("ears")))
            else:
                trace_id = renderer.trigger(clip_id, path, float(clip.get("gain", 1.0)),
                                 bool(clip.get("voice")), bool(clip.get("ears")))
            log.info("soundboard trigger accepted clip=%s voice=%s ears=%s gain=%.2f",
                     clip_id, bool(clip.get("voice")), bool(clip.get("ears")), float(clip.get("gain", 1.0)))
            self._playing.add(clip_id)
            return trace_id

    def diagnostics(self) -> dict[str, Any]:
        """Explicit diagnostics request, kept out of periodic phone snapshots."""
        renderer = self._renderer
        result = renderer.diagnostics() if renderer and hasattr(renderer, "diagnostics") else {
            "limits": asdict(self.runtime_limits), "resources": {}, "streams": {},
            "playbackTrace": [], "error": self._error or "Audio runtime is not running"}
        preview = self._preview_renderer
        if preview and hasattr(preview, "diagnostics"):
            result["preview"] = preview.diagnostics()
        # The preview service admits only a single file after closing its prior
        # stream. Keep this conservative decoded-array ceiling separate from
        # total process RAM and native decoder/driver memory.
        result["serviceDecodedArrayBudgetBytes"] = (SoundboardRenderer.clip_cache_bytes
            + self.runtime_limits.max_active_bytes
            + self.runtime_limits.max_pending_decodes * self.runtime_limits.max_clip_bytes
            + min(self.runtime_limits.max_clip_bytes, self.runtime_limits.max_active_bytes))
        return result

    def stop_all(self) -> None:
        with self._lock:
            self._play_generation += 1
            if self._renderer:
                self._renderer.stop_all()
            if self._preview_renderer:
                self._preview_renderer.stop_all()
            self._playing.clear()

    def test_tone(self, bus: str) -> None:
        with self._lock:
            if self._renderer is None:
                raise RuntimeError(self._error or "Apply the routing first")
            self._renderer.test_tone(bus)

    def audition(self, clip_id: str, outputs: list[dict[str, Any]]) -> None:
        from .routing import is_virtual
        with self._lock:
            clip = next((c for c in self._data["clips"] if c["id"] == clip_id), None)
            if clip is None:
                raise ValueError("unknown soundboard clip")
            physical = [d for d in outputs if not is_virtual(d)]
            selected = self._data["config"].get("earsOutputId")
            output = next((d for d in physical if d["id"] == selected), None)
            output = output or next((d for d in physical if d.get("isDefault")), None)
            if output is None:
                raise RuntimeError("Choose physical headphones/speakers to audition sounds")
            if self._preview_renderer is None:
                self._preview_renderer = SoundboardRenderer(limits=self.runtime_limits)
            self._preview_renderer.preview(self.clips_dir / clip["file"],
                                           float(clip.get("gain", 1)), output["name"])

    def close(self) -> bool:
        with self._lock:
            self._closed = True
            self._play_generation += 1
            if self._preview_renderer:
                if self._preview_renderer.close() is not False:
                    self._preview_renderer = None
            if self._renderer:
                if self._renderer.close() is not False:
                    self._renderer = None
            return self._renderer is None and self._preview_renderer is None

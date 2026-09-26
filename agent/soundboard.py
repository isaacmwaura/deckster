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
import hashlib
import re
import shutil
import threading
import uuid
import wave
from pathlib import Path
from typing import Any, Callable

from .config import resource_root
from .log import get_logger

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

    def __init__(self, on_error: Callable[[str], None] | None = None) -> None:
        self._on_error = on_error or (lambda _reason: None)
        self._lock = threading.RLock()
        self._voice_stream = None
        self._ears_stream = None
        self._clips: dict[str, Any] = {}
        self._voices: dict[str, list[dict[str, Any]]] = {"voice": [], "ears": []}
        self._np = None
        self._sd = None

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
        for index, name in candidates:
            if name.casefold() == wanted_folded:
                return index
        for index, name in candidates:
            if wanted_folded in name.casefold() or name.casefold() in wanted_folded:
                return index
        raise RuntimeError(f"audio device not available to WASAPI: {wanted}")

    def start(self, input_name: str, voice_name: str, ears_name: str | None = None) -> None:
        self.close()
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("Soundboard audio runtime is not installed") from exc

        self._np, self._sd = np, sd
        input_device = self._find_device(sd, input_name, "input")
        voice_device = self._find_device(sd, voice_name, "output")
        ears_device = self._find_device(sd, ears_name, "output") if ears_name else None
        try:
            self._voice_stream = sd.Stream(
                device=(input_device, voice_device), channels=(1, 2),
                samplerate=self.sample_rate, dtype="float32", callback=self._voice_callback,
                extra_settings=sd.WasapiSettings(auto_convert=True),
            )
            self._voice_stream.start()
            if ears_device is not None:
                self._ears_stream = sd.OutputStream(
                    device=ears_device, channels=2, samplerate=self.sample_rate,
                    dtype="float32", callback=self._ears_callback,
                    extra_settings=sd.WasapiSettings(auto_convert=True),
                )
                self._ears_stream.start()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        for stream_name in ("_voice_stream", "_ears_stream"):
            stream = getattr(self, stream_name)
            if stream is not None:
                try:
                    stream.stop(); stream.close()
                except Exception:  # noqa: BLE001 - shutdown must be best effort
                    pass
                setattr(self, stream_name, None)
        with self._lock:
            self._voices = {"voice": [], "ears": []}

    def trigger(self, clip_id: str, path: Path, gain: float, voice: bool, ears: bool) -> None:
        if not self._voice_stream:
            raise RuntimeError("Choose a mic and a Voice endpoint before playing clips")
        with self._lock:
            samples = self._clips.get(clip_id)
        if samples is None:
            samples = self._load(path)
            with self._lock:
                self._clips[clip_id] = samples
        with self._lock:
            for bus, enabled in (("voice", voice), ("ears", ears)):
                self._voices[bus] = [item for item in self._voices[bus]
                                     if item["id"] != clip_id]
                if enabled:
                    self._voices[bus].append({"id": clip_id, "samples": samples,
                                              "pos": 0, "gain": gain})

    def test_tone(self, bus: str) -> None:
        """Queue a quiet, short tone on exactly one configured output bus."""
        if bus not in ("voice", "ears"):
            raise ValueError("choose Voice or Ears")
        if self._voice_stream is None or (bus == "ears" and self._ears_stream is None):
            raise RuntimeError("Apply the route before testing this output")
        np = self._np
        length = int(self.sample_rate * 0.45)
        seconds = np.arange(length, dtype=np.float32) / self.sample_rate
        envelope = np.minimum(1, seconds / 0.02) * np.minimum(1, (0.45 - seconds) / 0.07)
        samples = (np.sin(2 * np.pi * (660 if bus == "voice" else 440) * seconds)
                   * envelope * 0.16).astype(np.float32)
        with self._lock:
            self._voices[bus] = [item for item in self._voices[bus]
                                 if item["id"] != "__route_test__"]
            self._voices[bus].append({"id": "__route_test__", "samples": samples,
                                      "pos": 0, "gain": 1.0})

    def stop_all(self) -> None:
        with self._lock:
            self._voices = {"voice": [], "ears": []}

    def playing_ids(self) -> set[str]:
        with self._lock:
            return {voice["id"] for bus in self._voices.values() for voice in bus}

    def _load(self, path: Path):
        """Decode common clip formats to mono float32, resampled to the mixer rate."""
        np = self._np
        if path.suffix.lower() == ".wav":
            with wave.open(str(path), "rb") as source:
                channels, width, rate, frames = source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getnframes()
                if width not in (1, 2, 3, 4):
                    raise RuntimeError("unsupported WAV sample width")
                raw = source.readframes(frames)
            if width == 3:
                packed = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
                data = (packed[:, 0].astype(np.int32) | (packed[:, 1].astype(np.int32) << 8) | (packed[:, 2].astype(np.int32) << 16))
                data = ((data ^ 0x800000) - 0x800000).astype(np.float32) / 8388608.0
            else:
                dtype, divisor, offset = {1: (np.uint8, 128.0, -1.0), 2: ("<i2", 32768.0, 0.0), 4: ("<i4", 2147483648.0, 0.0)}[width]
                data = np.frombuffer(raw, dtype=dtype).astype(np.float32) / divisor
                if offset:
                    data += offset
            data = data.reshape(-1, channels).mean(axis=1)
        else:
            try:
                import soundfile as sf
            except ImportError as exc:
                raise RuntimeError("MP3, OGG and FLAC clips need the soundfile runtime") from exc
            data, rate = sf.read(str(path), dtype="float32", always_2d=True)
            data = data.mean(axis=1)
        if not len(data):
            raise RuntimeError("clip is empty")
        if rate != self.sample_rate:
            new_len = max(1, round(len(data) * self.sample_rate / rate))
            data = np.interp(np.linspace(0, len(data) - 1, new_len),
                             np.arange(len(data)), data).astype(np.float32)
        return data

    def _mix(self, bus: str, frames: int):
        np = self._np
        out = np.zeros(frames, dtype=np.float32)
        with self._lock:
            keep = []
            for voice in self._voices[bus]:
                start = voice["pos"]
                take = min(frames, len(voice["samples"]) - start)
                if take > 0:
                    out[:take] += voice["samples"][start:start + take] * voice["gain"]
                    voice["pos"] += take
                if voice["pos"] < len(voice["samples"]):
                    keep.append(voice)
            self._voices[bus] = keep
        return np.clip(out, -1.0, 1.0)

    def _voice_callback(self, indata, outdata, frames, _time, status) -> None:
        if status:
            log.warning("soundboard voice stream: %s", status)
        try:
            mixed = self._mix("voice", frames) + indata[:, 0]
            outdata[:] = self._np.clip(mixed, -1.0, 1.0)[:, None]
        except Exception as exc:  # noqa: BLE001 - PortAudio callbacks cannot propagate
            self._on_error(str(exc)); outdata.fill(0)

    def _ears_callback(self, outdata, frames, _time, status) -> None:
        if status:
            log.warning("soundboard monitor stream: %s", status)
        try:
            outdata[:] = self._mix("ears", frames)[:, None]
        except Exception as exc:  # noqa: BLE001
            self._on_error(str(exc)); outdata.fill(0)


class SoundboardService:
    """Owns the on-disk pad library, selected endpoint IDs, and mixer lifecycle."""

    def __init__(self, root: Path, renderer_factory=SoundboardRenderer,
                 defaults_root: Path | None = None) -> None:
        self.root = root
        self.clips_dir = root / "soundboard"
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.clips_dir / "library.json"
        self._renderer_factory = renderer_factory
        self._renderer: SoundboardRenderer | None = None
        self._lock = threading.RLock()
        self._error = ""
        self._playing: set[str] = set()
        self._duration_cache: dict[str, tuple[int, int, float]] = {}
        self._autostart_attempted = False
        self.defaults_root = defaults_root or (resource_root() / DEFAULT_PACK_DIR)
        self._data = self._load()
        self._install_default_pack_once()

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
            for clip in self._data["clips"]:
                entry = next((item for item in manifest["clips"]
                              if item["key"] == clip.get("defaultKey")), None)
                if entry is None:
                    continue
                source = self.defaults_root / str(entry["file"])
                shutil.copy2(source, self.clips_dir / str(clip["file"]))
                if (clip.get("defaultKey") in old_gains and
                        clip.get("gain") == old_gains[clip["defaultKey"]]):
                    clip["gain"] = _clamp(entry.get("gain", clip["gain"]))
            self._data["defaultPackVersion"] = int(manifest["version"])
            self._save()

    def restore_defaults(self, reset: bool = True,
                         manifest: dict[str, Any] | None = None) -> int:
        """Restore verified starter pads while preserving every user-imported clip."""
        manifest = manifest or self._default_manifest()
        if reset:
            self.stop_all()
        with self._lock:
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
                shutil.copy2(source, self.clips_dir / destination_name)
                installed.append({
                    "id": f"default-{key}", "defaultKey": key,
                    "file": destination_name, "label": str(entry.get("label", key))[:48],
                    "emoji": str(entry.get("emoji", "♪"))[:48],
                    "voice": bool(entry.get("voice", True)),
                    "ears": bool(entry.get("ears", True)),
                    "gain": _clamp(entry.get("gain", 1.0)),
                })
            valid_files = {str(c["file"]) for c in installed}
            for clip in old_defaults:
                filename = str(clip.get("file", ""))
                if filename and filename not in valid_files:
                    try:
                        (self.clips_dir / filename).unlink(missing_ok=True)
                    except OSError:
                        log.warning("could not remove retired default sound %s", filename)
            self._data["clips"] = installed + custom
            self._data["defaultPackVersion"] = int(manifest["version"])
            self._save()
            return len(installed)

    def snapshot(self, outputs: list[dict[str, Any]] | None = None,
                 inputs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        with self._lock:
            configured = bool(self._data["config"]["inputId"] and self._data["config"]["voiceOutputId"])
            playing = self._playing
            if self._renderer is not None and hasattr(self._renderer, "playing_ids"):
                playing = self._renderer.playing_ids()
            return {"clips": [dict(c, playing=c["id"] in playing,
                                   duration=self._clip_duration(c)) for c in self._data["clips"]],
                    "config": dict(self._data["config"]), "configured": configured,
                    "runtime": "ready" if self._renderer else "setup_required",
                    "error": self._error, "outputs": outputs or [], "inputs": inputs or []}

    def _clip_duration(self, clip: dict[str, Any]) -> float:
        """Read clip length for the phone's playback indicator."""
        path = self.clips_dir / str(clip.get("file", ""))
        try:
            stat = path.stat()
            cached = self._duration_cache.get(str(path))
            if cached and cached[:2] == (stat.st_size, stat.st_mtime_ns):
                return cached[2]
            if path.suffix.lower() == ".wav":
                with wave.open(str(path), "rb") as source:
                    duration = source.getnframes() / source.getframerate()
            else:
                import soundfile as sf
                duration = float(sf.info(str(path)).duration)
            self._duration_cache[str(path)] = (stat.st_size, stat.st_mtime_ns, duration)
            return duration
        except (OSError, ValueError, RuntimeError, ImportError, wave.Error):
            return 0.0

    def import_clip(self, source: Path, label: str | None = None,
                    voice: bool = True, ears: bool = False, gain: float = 1.0) -> dict[str, Any]:
        source = source.expanduser().resolve()
        if not source.is_file() or source.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError("choose a WAV, MP3, OGG, or FLAC audio file")
        clip_id = uuid.uuid4().hex[:12]
        destination = self.clips_dir / (clip_id + source.suffix.lower())
        shutil.copy2(source, destination)
        clip = {"id": clip_id, "file": destination.name, "label": (label or source.stem)[:48],
                "emoji": "♪", "voice": bool(voice), "ears": bool(ears), "gain": _clamp(gain)}
        with self._lock:
            self._data["clips"].append(clip); self._save()
        return dict(clip)

    def update_clip(self, clip_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {"label", "emoji", "voice", "ears", "gain"}
        with self._lock:
            clip = next((c for c in self._data["clips"] if c.get("id") == clip_id), None)
            if clip is None:
                raise ValueError("unknown soundboard clip")
            for key in allowed & changes.keys():
                if key == "gain": clip[key] = _clamp(changes[key])
                elif key in ("voice", "ears"): clip[key] = bool(changes[key])
                else: clip[key] = str(changes[key])[:48]
            self._save()
            return dict(clip)

    def remove_clip(self, clip_id: str) -> bool:
        with self._lock:
            clip = next((c for c in self._data["clips"] if c.get("id") == clip_id), None)
            if clip is None:
                return False
            self._data["clips"].remove(clip)
            self._playing.discard(clip_id)
            self._save()
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
                self._data["config"]["layout"] = layout
                self._save()
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
            self._autostart_attempted = True
            self._data["config"].update(values); self._data["config"]["layout"] = layout; self._save()
            self._error = ""; self._playing.clear()
            if self._renderer:
                self._renderer.close(); self._renderer = None
            if not values["inputId"] or not values["voiceOutputId"]:
                return
            try:
                self._renderer = self._renderer_factory()
                self._renderer.start(input_map[values["inputId"]], output_map[values["voiceOutputId"]],
                                     output_map.get(values["earsOutputId"]) or None)
            except Exception as exc:  # noqa: BLE001 - surface setup status to the user
                self._renderer = None; self._error = str(exc); log.warning("soundboard setup: %s", exc)

    def ensure_started(self, outputs: list[dict[str, Any]], inputs: list[dict[str, Any]]) -> None:
        """Restore a persisted route once endpoint enumeration becomes available.

        Startup publishes state before Core Audio's first poll, so endpoint names
        do not exist in ``__init__``. The controller calls this after each poll;
        we attempt exactly once when both saved IDs are present. A manual Apply
        remains the explicit retry after a driver/device error.
        """
        with self._lock:
            if self._renderer is not None or self._autostart_attempted:
                return
            config = dict(self._data["config"])
        output_ids = {str(d.get("id")) for d in outputs}
        input_ids = {str(d.get("id")) for d in inputs}
        if (config.get("inputId") in input_ids and
                config.get("voiceOutputId") in output_ids):
            self.configure(config, outputs, inputs)

    def play(self, clip_id: str) -> None:
        with self._lock:
            clip = next((c for c in self._data["clips"] if c.get("id") == clip_id), None)
            if clip is None:
                raise ValueError("unknown soundboard clip")
            if not clip.get("voice") and not clip.get("ears"):
                raise ValueError("enable Voice or Ears for this pad")
            if self._renderer is None:
                raise RuntimeError(self._error or "Choose a mic and a Voice endpoint first")
            path = self.clips_dir / str(clip["file"])
            if not path.is_file():
                raise RuntimeError("the clip file is missing")
            self._renderer.trigger(clip_id, path, float(clip.get("gain", 1.0)),
                                   bool(clip.get("voice")), bool(clip.get("ears")))
            self._playing.add(clip_id)

    def stop_all(self) -> None:
        with self._lock:
            if self._renderer:
                self._renderer.stop_all()
            self._playing.clear()

    def test_tone(self, bus: str) -> None:
        with self._lock:
            if self._renderer is None:
                raise RuntimeError(self._error or "Apply the routing first")
            self._renderer.test_tone(bus)

    def close(self) -> None:
        with self._lock:
            if self._renderer:
                self._renderer.close(); self._renderer = None

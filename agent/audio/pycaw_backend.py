"""Windows audio backend built on pycaw (WASAPI).

Runs only on the AudioEngine's COM thread. Key behaviours:

- Sessions are grouped per *process* (all of an app's sessions become one logical
  control), because "set Discord to 40%" should move every Discord stream. This
  is the pragmatic answer to the session-identity problem for the MVP; a finer
  per-stream model can come later (BUILD-PLAN.md challenge 3).
- Speaker master uses the AudioDevice.EndpointVolume helper; the microphone is
  returned as a raw IMMDevice by this pycaw build, so we Activate it ourselves.
- This pycaw build exposes AudioUtilities.SetDefaultDevice, so default-output
  switching needs no external helper (svcl) after all — noted back into the plan.
"""
from __future__ import annotations

import re

from comtypes import CLSCTX_ALL, CoCreateInstance

from ..log import get_logger
from .backend import (
    AudioBackend,
    DeviceInfo,
    DeviceLists,
    MasterInfo,
    SessionInfo,
    clamp01,
)

log = get_logger("audio.pycaw")


def _slug(name: str) -> str:
    base = re.sub(r"\.exe$", "", name, flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "app"


def _label(name: str) -> str:
    return re.sub(r"\.exe$", "", name, flags=re.IGNORECASE)


class PycawAudioBackend(AudioBackend):
    def __init__(self) -> None:
        # Imported here so a non-audio import of the package never pulls pycaw.
        from pycaw.pycaw import AudioUtilities

        self._AU = AudioUtilities

    def setup(self) -> None:
        # COM must be initialised on the thread that makes the calls (the engine
        # thread). Owning this here — rather than in the engine — keeps COM out of
        # backends that don't need it (e.g. the mock).
        import comtypes
        comtypes.CoInitialize()

    def teardown(self) -> None:
        import comtypes
        comtypes.CoUninitialize()

    # ---- sessions ---------------------------------------------------------
    def _iter_sessions(self):
        return self._AU.GetAllSessions()

    def snapshot_sessions(self) -> list[SessionInfo]:
        from ..icons import ICONS
        groups: dict[str, SessionInfo] = {}
        for s in self._iter_sessions():
            proc = s.Process
            name = proc.name() if proc else "System Sounds"
            sid = _slug(name) if proc else "system"
            sav = s.SimpleAudioVolume
            level = float(sav.GetMasterVolume())
            muted = bool(sav.GetMute())
            if sid not in groups:
                # Extract the app's real icon once per exe (cached); UI falls back
                # to a letter badge when there's no icon (system sounds, denied, etc.).
                exe = None
                if proc is not None:
                    try:
                        exe = proc.exe()
                    except Exception:  # noqa: BLE001 - AccessDenied/NoSuchProcess
                        exe = None
                icon_key = ICONS.get_or_extract(exe)
                groups[sid] = SessionInfo(sid, _label(name), level, muted,
                                          active=True, iconKey=icon_key)
            # else: keep the first session's representative level/mute
        return list(groups.values())

    def _apply_to_group(self, session_id: str, fn) -> None:
        for s in self._iter_sessions():
            proc = s.Process
            sid = _slug(proc.name()) if proc else "system"
            if sid == session_id:
                fn(s.SimpleAudioVolume)

    def set_session_volume(self, session_id: str, level: float) -> None:
        lvl = clamp01(level)
        self._apply_to_group(session_id, lambda sav: sav.SetMasterVolume(lvl, None))

    def set_session_mute(self, session_id: str, muted: bool) -> None:
        m = 1 if muted else 0
        self._apply_to_group(session_id, lambda sav: sav.SetMute(m, None))

    # ---- master (speaker / mic) ------------------------------------------
    def _endpoint(self, kind: str):
        # Both endpoints go through pycaw's AudioDevice.EndpointVolume helper, which
        # manages the COM reference count correctly. GetSpeakers() already returns an
        # AudioDevice; GetMicrophone() returns a raw IMMDevice, so we wrap it with
        # CreateDevice(). A hand-rolled Activate()+cast() here mis-owned the pointer
        # and caused access-violation crashes when GC ran (see BUILD-PLAN.md ch.1).
        if kind == "speaker":
            return self._AU.GetSpeakers().EndpointVolume
        return self._AU.CreateDevice(self._AU.GetMicrophone()).EndpointVolume

    def get_master(self, kind: str) -> MasterInfo:
        ep = self._endpoint(kind)
        return MasterInfo(float(ep.GetMasterVolumeLevelScalar()), bool(ep.GetMute()))

    def set_master_volume(self, kind: str, level: float) -> None:
        self._endpoint(kind).SetMasterVolumeLevelScalar(clamp01(level), None)

    def set_master_mute(self, kind: str, muted: bool) -> None:
        self._endpoint(kind).SetMute(1 if muted else 0, None)

    # ---- devices ----------------------------------------------------------
    def list_devices(self) -> DeviceLists:
        from pycaw.pycaw import DEVICE_STATE, EDataFlow, ERole, IMMDeviceEnumerator
        from pycaw.constants import CLSID_MMDeviceEnumerator

        enum = CoCreateInstance(
            CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, CLSCTX_ALL
        )
        result = DeviceLists()
        for flow, bucket in (
            (EDataFlow.eRender.value, result.outputs),
            (EDataFlow.eCapture.value, result.inputs),
        ):
            try:
                default_id = enum.GetDefaultAudioEndpoint(
                    flow, ERole.eMultimedia.value
                ).GetId()
            except Exception:  # noqa: BLE001 - no default device present
                default_id = None
            coll = enum.EnumAudioEndpoints(flow, DEVICE_STATE.ACTIVE.value)
            for i in range(coll.GetCount()):
                d = coll.Item(i)
                ad = self._AU.CreateDevice(d)
                did = d.GetId()
                bucket.append(DeviceInfo(did, str(ad.FriendlyName), did == default_id))
        return result

    def set_default_output(self, device_id: str) -> None:
        self._AU.SetDefaultDevice(device_id)

    def set_default_input(self, device_id: str) -> None:
        # SetDefaultDevice targets the endpoint by id; works for capture too.
        self._AU.SetDefaultDevice(device_id)

    # ---- live metering ----------------------------------------------------
    def _peak(self, dev) -> float:
        # IAudioMeterInformation.GetPeakValue is the same Activate()+QueryInterface
        # pattern pycaw uses for EndpointVolume (utils.AudioDevice.EndpointVolume),
        # so it manages the COM reference safely — unlike the hand-rolled cast that
        # once crashed (BUILD-PLAN.md ch.1). GetSpeakers() returns an AudioDevice
        # (wrapping IMMDevice as ._dev); GetMicrophone() returns a raw IMMDevice.
        from pycaw.pycaw import IAudioMeterInformation
        raw = getattr(dev, "_dev", dev)
        iface = raw.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None)
        return float(iface.QueryInterface(IAudioMeterInformation).GetPeakValue())

    def get_peaks(self) -> dict[str, float]:
        peaks = {"output": 0.0, "input": 0.0}
        # Meter each endpoint independently so one unavailable device (e.g. no mic)
        # never zeroes the other and never breaks the poll.
        try:
            peaks["output"] = clamp01(self._peak(self._AU.GetSpeakers()))
        except Exception:  # noqa: BLE001 - metering is best-effort
            log.debug("output peak unavailable", exc_info=True)
        try:
            peaks["input"] = clamp01(self._peak(self._AU.GetMicrophone()))
        except Exception:  # noqa: BLE001
            log.debug("input peak unavailable", exc_info=True)
        return peaks

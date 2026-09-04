"""Keystroke injection via the Win32 SendInput API.

The macro path presses a key combination that the *target app already listens for*
— e.g. Discord's global "toggle mute" hotkey — so it reaches app-internal actions
the audio API cannot (BUILD-PLAN.md P3, HTML 5.3).

We send **scancode** events (KEYEVENTF_SCANCODE): Windows still translates them to a
virtual key for foreground apps and OS hotkeys, but scancodes also survive into
DirectInput games where synthetic virtual-key events are ignored. Extended keys
(right-hand modifiers, arrows, navigation cluster, etc.) set KEYEVENTF_EXTENDEDKEY.

`parse_combo` (pure) is unit-tested; the actual injection (`send_combo`) is a live/
manual check so automated runs never type into whatever window has focus.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

# ---- virtual-key table ----------------------------------------------------
VK: dict[str, int] = {
    # modifiers (generic + left/right)
    "ctrl": 0x11, "control": 0x11, "lctrl": 0xA2, "rctrl": 0xA3,
    "shift": 0x10, "lshift": 0xA0, "rshift": 0xA1,
    "alt": 0x12, "lalt": 0xA4, "ralt": 0xA5,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "meta": 0x5B,
    # common named keys
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09,
    "space": 0x20, "backspace": 0x08, "bksp": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21,
    "pagedown": 0x22, "pgdn": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "printscreen": 0x2C, "prtsc": 0x2C, "pause": 0x13,
    "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
    # punctuation commonly used in hotkeys (US layout VKs)
    "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "`": 0xC0,
}
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    VK[_c] = 0x41 + _i
for _i in range(10):
    VK[str(_i)] = 0x30 + _i
for _i in range(1, 25):
    VK[f"f{_i}"] = 0x70 + (_i - 1)

MODIFIERS = {
    "ctrl", "control", "lctrl", "rctrl",
    "shift", "lshift", "rshift",
    "alt", "lalt", "ralt",
    "win", "lwin", "rwin", "meta",
}
# canonical press order so combos are deterministic
_MOD_ORDER = ["ctrl", "control", "lctrl", "rctrl",
              "shift", "lshift", "rshift",
              "alt", "lalt", "ralt",
              "win", "lwin", "rwin", "meta"]

# VKs that require KEYEVENTF_EXTENDEDKEY
EXTENDED_VKS = {
    0xA3, 0xA5,           # right ctrl / right alt
    0x2E, 0x2D,           # delete / insert
    0x24, 0x23, 0x21, 0x22,  # home / end / pageup / pagedown
    0x26, 0x28, 0x25, 0x27,  # arrows
    0x2C, 0x90, 0x5B, 0x5C,  # printscreen / numlock / lwin / rwin
}


@dataclass(frozen=True)
class KeyEvent:
    vk: int
    down: bool
    extended: bool


class ComboError(ValueError):
    """Raised when a hotkey string cannot be parsed."""


def parse_combo(combo: str) -> list[KeyEvent]:
    """Turn 'ctrl+shift+m' into an ordered press/release event list.

    Modifiers press first (canonical order), then the main key(s); release happens
    in reverse. Raises ComboError on unknown tokens or a combo with no main key.
    """
    if not combo or not combo.strip():
        raise ComboError("empty combo")

    tokens = [t.strip().lower() for t in combo.split("+") if t.strip()]
    if not tokens:
        raise ComboError("empty combo")

    mods: list[str] = []
    keys: list[str] = []
    for tok in tokens:
        if tok not in VK:
            raise ComboError(f"unknown key: {tok!r}")
        (mods if tok in MODIFIERS else keys).append(tok)

    if not keys:
        raise ComboError("combo has no non-modifier key")

    mods.sort(key=lambda m: _MOD_ORDER.index(m))

    def ev(name: str, down: bool) -> KeyEvent:
        vk = VK[name]
        return KeyEvent(vk=vk, down=down, extended=vk in EXTENDED_VKS)

    events: list[KeyEvent] = []
    for m in mods:
        events.append(ev(m, True))
    for k in keys:
        events.append(ev(k, True))
    for k in reversed(keys):
        events.append(ev(k, False))
    for m in reversed(mods):
        events.append(ev(m, False))
    return events


# ---- Win32 SendInput plumbing --------------------------------------------
_INPUT_KEYBOARD = 1
_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_SCANCODE = 0x0008
_MAPVK_VK_TO_VSC = 0


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _to_input(event: KeyEvent) -> _INPUT:
    user32 = ctypes.windll.user32
    scan = user32.MapVirtualKeyW(event.vk, _MAPVK_VK_TO_VSC)
    flags = _KEYEVENTF_SCANCODE
    if event.extended:
        flags |= _KEYEVENTF_EXTENDEDKEY
    if not event.down:
        flags |= _KEYEVENTF_KEYUP
    ki = _KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=None)
    return _INPUT(type=_INPUT_KEYBOARD, u=_INPUTUNION(ki=ki))


def send_combo(combo: str) -> None:
    """Inject a key combination as a single SendInput batch. Windows-only."""
    events = parse_combo(combo)
    arr = (_INPUT * len(events))(*[_to_input(e) for e in events])
    sent = ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))
    if sent != len(events):
        raise OSError(f"SendInput injected {sent}/{len(events)} events")

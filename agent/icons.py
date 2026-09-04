"""Extract a running app's real Windows icon as a PNG, with caching.

The phone shows each app/game's actual executable icon instead of a letter badge.
We pull the icon straight from the .exe via Win32 (ExtractIconEx / GetIconInfo /
GetDIBits) and encode it to PNG with Pillow — no pywin32 dependency, just ctypes.

Extraction is best-effort: an elevated process we can't read, a missing exe, or an
icon-less binary simply yields no icon and the UI falls back to its letter badge.
Results (including failures) are cached by executable path so we probe each exe at
most once, and the bytes are served over HTTP by key rather than bloating every
poll snapshot.
"""
from __future__ import annotations

import ctypes
import hashlib
import threading
from ctypes import wintypes
from io import BytesIO
from typing import Optional

from .log import get_logger

log = get_logger("icons")

# Extract at a high resolution (exes commonly embed a 256px icon) and emit at 128,
# so the badge stays crisp even on a 3x-density phone. ICON_VER busts client/SW
# caches when the extraction changes — /icon/{key} URLs include it.
_SRC_PX = 256
_OUT_PX = 128
ICON_VER = "2"

# HANDLE-typed aliases so 64-bit handles are never truncated to a C int. Setting
# explicit argtypes/restype below is what fixes both the OverflowError on large
# handle values and the silent failures from mis-sized returns.
_HICON = ctypes.c_void_p
_HBITMAP = ctypes.c_void_p
_HDC = ctypes.c_void_p


class _ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", _HBITMAP),
        ("hbmColor", _HBITMAP),
    ]


class _BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
        ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
        ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
        ("bmBits", ctypes.c_void_p),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class _SHFILEINFO(ctypes.Structure):
    _fields_ = [
        ("hIcon", _HICON), ("iIcon", ctypes.c_int),
        ("dwAttributes", wintypes.DWORD),
        ("szDisplayName", wintypes.WCHAR * 260),
        ("szTypeName", wintypes.WCHAR * 80),
    ]


_BI_RGB = 0
_DIB_RGB_COLORS = 0
_SHGFI_ICON = 0x000000100
_SHGFI_LARGEICON = 0x000000000

_prototypes_ready = False


def _ensure_prototypes() -> bool:
    """Declare 64-bit-safe argtypes/restype once. Returns False off Windows."""
    global _prototypes_ready
    if _prototypes_ready:
        return True
    if not hasattr(ctypes, "windll"):
        return False
    u, g, s = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.shell32
    s.ExtractIconExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int,
                                 ctypes.POINTER(_HICON), ctypes.POINTER(_HICON), ctypes.c_uint]
    s.ExtractIconExW.restype = ctypes.c_uint
    s.SHGetFileInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                 ctypes.POINTER(_SHFILEINFO), ctypes.c_uint, ctypes.c_uint]
    s.SHGetFileInfoW.restype = ctypes.c_void_p
    u.GetIconInfo.argtypes = [_HICON, ctypes.POINTER(_ICONINFO)]
    u.GetIconInfo.restype = wintypes.BOOL
    u.DestroyIcon.argtypes = [_HICON]
    u.DestroyIcon.restype = wintypes.BOOL
    g.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    g.GetObjectW.restype = ctypes.c_int
    g.CreateCompatibleDC.argtypes = [_HDC]
    g.CreateCompatibleDC.restype = _HDC
    g.GetDIBits.argtypes = [_HDC, _HBITMAP, ctypes.c_uint, ctypes.c_uint,
                            ctypes.c_void_p, ctypes.POINTER(_BITMAPINFO), ctypes.c_uint]
    g.GetDIBits.restype = ctypes.c_int
    g.DeleteObject.argtypes = [ctypes.c_void_p]
    g.DeleteObject.restype = wintypes.BOOL
    g.DeleteDC.argtypes = [_HDC]
    g.DeleteDC.restype = wintypes.BOOL
    u.PrivateExtractIconsW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       ctypes.POINTER(_HICON), ctypes.POINTER(ctypes.c_uint),
                                       ctypes.c_uint, ctypes.c_uint]
    u.PrivateExtractIconsW.restype = ctypes.c_uint
    _prototypes_ready = True
    return True


def _acquire_hicon(exe_path: str):
    """Get a high-res HICON for the exe, best source first.

    PrivateExtractIcons picks the icon closest to the requested size (so it lands
    on the exe's 256px icon when present), which is far sharper than ExtractIconEx's
    fixed 32px. Falls back to ExtractIconEx, then the shell association icon.
    """
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    one = (_HICON * 1)()
    try:
        n = user32.PrivateExtractIconsW(exe_path, 0, _SRC_PX, _SRC_PX, one, None, 1, 0)
        if n and one[0]:
            return one[0]
    except Exception:  # noqa: BLE001 - fall through to the older extractors
        pass
    n = shell32.ExtractIconExW(exe_path, 0, one, None, 1)
    if n and one[0]:
        return one[0]
    info = _SHFILEINFO()
    res = shell32.SHGetFileInfoW(exe_path, 0, ctypes.byref(info),
                                 ctypes.sizeof(info), _SHGFI_ICON | _SHGFI_LARGEICON)
    if res and info.hIcon:
        return info.hIcon
    return None


def _bitmap_size(gdi32, hbm) -> tuple[int, int]:
    bm = _BITMAP()
    if gdi32.GetObjectW(hbm, ctypes.sizeof(bm), ctypes.byref(bm)) == 0:
        return 0, 0
    return bm.bmWidth, bm.bmHeight


def _read_bgra(gdi32, hdc, hbm, w, h) -> Optional[bytearray]:
    """Read an HBITMAP into a top-down BGRA byte buffer via GetDIBits."""
    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # negative => top-down rows
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = _BI_RGB
    buf = (ctypes.c_byte * (w * h * 4))()
    if gdi32.GetDIBits(hdc, hbm, 0, h, buf, ctypes.byref(bmi), _DIB_RGB_COLORS) == 0:
        return None
    return bytearray(buf)


def extract_icon_png(exe_path: str) -> Optional[bytes]:
    """Return PNG bytes for an executable's icon, or None if unavailable."""
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - Pillow should be present (pystray dep)
        return None
    if not _ensure_prototypes():
        return None

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    hicon = _acquire_hicon(exe_path)
    if not hicon:
        return None

    hdc = gdi32.CreateCompatibleDC(None)
    info = _ICONINFO()
    if not user32.GetIconInfo(hicon, ctypes.byref(info)):
        gdi32.DeleteDC(hdc)
        user32.DestroyIcon(hicon)
        return None
    try:
        w, h = _bitmap_size(gdi32, info.hbmColor)
        if w <= 0 or h <= 0:
            return None
        color = _read_bgra(gdi32, hdc, info.hbmColor, w, h)
        if color is None:
            return None
        # If the color bitmap carries no alpha (older icons), recover it from the
        # AND mask (a set mask bit means that pixel is transparent).
        if not any(color[i] for i in range(3, len(color), 4)):
            mask = _read_bgra(gdi32, hdc, info.hbmMask, w, h)
            for px in range(w * h):
                transparent = mask is not None and mask[px * 4] != 0
                color[px * 4 + 3] = 0 if transparent else 255
        # BGRA -> RGBA
        for i in range(0, len(color), 4):
            color[i], color[i + 2] = color[i + 2], color[i]
        img = Image.frombuffer("RGBA", (w, h), bytes(color), "raw", "RGBA", 0, 1)
        if (w, h) != (_OUT_PX, _OUT_PX):
            img = img.resize((_OUT_PX, _OUT_PX), Image.LANCZOS)
        out = BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    finally:
        if info.hbmColor:
            gdi32.DeleteObject(info.hbmColor)
        if info.hbmMask:
            gdi32.DeleteObject(info.hbmMask)
        gdi32.DeleteDC(hdc)
        user32.DestroyIcon(hicon)


class IconStore:
    """Thread-safe exe-path -> (key, PNG bytes) cache.

    Populated on the audio/COM thread (which knows each session's exe path) and
    read on the event-loop thread when the `/icon/{key}` route serves the bytes.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, Optional[bytes]] = {}
        self._path_key: dict[str, Optional[str]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key_for(exe_path: str) -> str:
        return hashlib.sha1((ICON_VER + ":" + exe_path.lower()).encode("utf-8")).hexdigest()[:16]

    def get_or_extract(self, exe_path: Optional[str]) -> Optional[str]:
        """Ensure the icon for exe_path is cached; return its key (or None)."""
        if not exe_path:
            return None
        with self._lock:
            if exe_path in self._path_key:
                return self._path_key[exe_path]
        # Extract outside the lock (it does GDI work); tolerate any failure.
        png: Optional[bytes] = None
        try:
            png = extract_icon_png(exe_path)
        except Exception:  # noqa: BLE001 - never let icon work break a poll
            log.debug("icon extract failed for %s", exe_path, exc_info=True)
        key = self._key_for(exe_path) if png else None
        with self._lock:
            self._path_key[exe_path] = key
            if key:
                self._by_key[key] = png
        return key

    def get_png(self, key: str) -> Optional[bytes]:
        with self._lock:
            return self._by_key.get(key)


# Process-wide singleton shared by the backend (writer) and server (reader).
ICONS = IconStore()

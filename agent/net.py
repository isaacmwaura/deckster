"""Networking helpers for the kiosk experience (P4).

- Port-conflict fallback: if the preferred port is taken, pick the next free one
  and surface the actual port so the QR/URL stay correct.
- Connect targets: the URL(s) the phone opens, depending on mode. In loopback
  (wired USB-C + adb reverse) the phone uses http://localhost:PORT; on Wi-Fi it
  uses the PC's LAN IP.
- QR: encode the connect URL so the phone can scan instead of typing.
"""
from __future__ import annotations

import socket
from pathlib import Path
from typing import Callable


def _default_is_free(host: str, port: int) -> bool:
    """True if we can bind host:port right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def find_available_port(preferred: int, host: str = "127.0.0.1", attempts: int = 20,
                        is_free: Callable[[str, int], bool] | None = None) -> int:
    """Return the preferred port if free, else the next free one within `attempts`.

    `is_free` is injectable for tests. Raises RuntimeError if none are free.
    """
    check = is_free or _default_is_free
    for port in range(preferred, preferred + attempts):
        if check(host, port):
            return port
    raise RuntimeError(f"no free port in [{preferred}, {preferred + attempts})")


def lan_ip() -> str:
    """Best-effort primary LAN IPv4. Falls back to 127.0.0.1 with no network.

    Uses a UDP socket's routing to discover the outbound interface address; no
    packets are actually sent.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def connect_targets(mode: str, port: int, secure: bool = False) -> dict[str, object]:
    """Return the phone-facing connect info for the given bind mode."""
    scheme = "https" if secure else "http"
    if mode == "lan":
        host = lan_ip()
        primary = f"{scheme}://{host}:{port}/"
        note = "Open this on the phone while it is on the same Wi-Fi."
    else:
        # Wired USB-C: the phone reaches the agent via adb reverse on its own localhost.
        primary = f"{scheme}://localhost:{port}/"
        note = "Wired USB-C: after adb reverse, open this in the phone's browser."
    return {"mode": mode, "url": primary, "port": port, "note": note, "secure": secure}


def write_qr_png(url: str, path: Path) -> Path | None:
    """Write a QR PNG for `url`; return the path, or None if qrcode is unavailable."""
    try:
        import qrcode
    except Exception:  # noqa: BLE001 - QR is a convenience, never required
        return None
    img = qrcode.make(url)
    img.save(str(path))
    return path


def qr_png_bytes(data: str) -> bytes | None:
    """Render a QR for `data` to PNG bytes in memory; None if qrcode is unavailable."""
    try:
        import qrcode
    except Exception:  # noqa: BLE001
        return None
    from io import BytesIO
    buf = BytesIO()
    qrcode.make(data).save(buf, format="PNG")
    return buf.getvalue()

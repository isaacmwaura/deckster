"""Generate the Deckster icon set from one mark (a volume-dial arc + knob).

One source of truth for the brand across web (PWA icons + favicon), the Android
launcher (mipmaps), and the Windows exe (.ico) + tray. Run from the repo root:

    python build/make_icons.py

Renders 4x-supersampled then downscales for smooth edges (Pillow arcs are aliased).
"""
from __future__ import annotations

import math
import os
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent

# Brand palette (matches the web UI).
BG_TOP = (12, 15, 22)
BG_BOT = (20, 24, 33)
TRACK = (36, 40, 52)
ACCENT = (86, 194, 255)     # #56C2FF
KNOB = (240, 244, 248)
SS = 4                      # supersample factor


def _gradient_bg(size: int, rounded: bool) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        grad.putpixel((0, y), tuple(int(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t) for i in range(3)))
    grad = grad.resize((size, size))
    if rounded:
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1],
                                               radius=int(size * 0.22), fill=255)
        img.paste(grad, (0, 0), mask)
    else:
        img.paste(grad, (0, 0))
    return img


def _draw(size: int, rounded: bool) -> Image.Image:
    S = size * SS
    img = _gradient_bg(S, rounded)
    d = ImageDraw.Draw(img)
    cx = cy = S / 2
    margin = S * 0.27
    bbox = [margin, margin, S - margin, S - margin]
    w = max(2, int(S * 0.09))
    # gauge track (gap at the bottom), then the accent arc over ~66% of it
    d.arc(bbox, 150, 390, fill=TRACK, width=w)
    start, end = 150, 150 + 240 * 0.66
    d.arc(bbox, start, end, fill=ACCENT, width=w)
    # knob at the end of the accent arc
    r = (bbox[2] - bbox[0]) / 2
    ka = math.radians(end)
    kx, ky = cx + r * math.cos(ka), cy + r * math.sin(ka)
    kr = S * 0.085
    d.ellipse([kx - kr, ky - kr, kx + kr, ky + kr], fill=KNOB)
    d.ellipse([kx - kr * 0.5, ky - kr * 0.5, kx + kr * 0.5, ky + kr * 0.5], fill=ACCENT)
    # centre pivot dot
    pr = S * 0.045
    d.ellipse([cx - pr, cy - pr, cx + pr, cy + pr], fill=ACCENT)
    return img.resize((size, size), Image.LANCZOS)


def _circular(img: Image.Image) -> Image.Image:
    size = img.size[0]
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _save(img: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    print("wrote", path.relative_to(ROOT))


def main() -> None:
    # Web (PWA icons + favicon + tray/window source), rounded card.
    _save(_draw(512, rounded=True), ROOT / "web" / "icon-512.png")
    _save(_draw(192, rounded=True), ROOT / "web" / "icon-192.png")
    _save(_draw(64, rounded=True), ROOT / "web" / "icon-64.png")
    _save(_draw(32, rounded=True), ROOT / "web" / "favicon.png")

    # Windows exe icon (multi-resolution .ico) + tray.
    ico = _draw(256, rounded=True)
    ico_path = ROOT / "build" / "deckster.ico"
    ico.save(ico_path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", ico_path.relative_to(ROOT))

    # Android launcher mipmaps (full-bleed square + round), per density.
    densities = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
    res = ROOT / "android" / "app" / "src" / "main" / "res"
    for name, px in densities.items():
        square = _draw(px, rounded=False)
        _save(square, res / f"mipmap-{name}" / "ic_launcher.png")
        _save(_circular(square), res / f"mipmap-{name}" / "ic_launcher_round.png")


if __name__ == "__main__":
    main()

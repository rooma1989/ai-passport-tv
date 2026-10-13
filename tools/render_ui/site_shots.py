#!/usr/bin/env python3
"""Cut the website's screen pictures (packaging/site/assets/screen-*.png) out
of render_ui.sh's sheets, at the panel's own size (the page scales them up
with image-rendering: pixelated).

The renderer fills the video area with grey bands; here those exact colours
are replaced by a drawn landscape, so a playing screen looks like it is
playing something. Everything else on the screen is the firmware's drawing.

Usage: site_shots.py  (needs Pillow; runs render_ui.sh for both boards)
"""

from __future__ import annotations

import math
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "packaging" / "site" / "assets"
H, SCALE, GAP = 240, 2, 8
# render_ui.c picture(): the stand-in bands, as RGB565.
BANDS = (0x4A69, 0x7BCF, 0x2945, 0xC618, 0x8410, 0x3186, 0xA514)
# (row, column) on the sheet -> file name. See main() in render_ui.c.
XINGZHI = {
    (11, 0): "screen-episode",
    (0, 0): "screen-banner",
    (3, 1): "screen-channels",
    (7, 1): "screen-voice",
    (8, 0): "screen-voice-pick",
    (5, 1): "screen-listen",
    (5, 2): "screen-pets",
    (12, 0): "screen-soundsetup",
}
PASSPORT = {(11, 0): "screen-episode-passport"}


def rgb565(v: int) -> tuple[int, int, int]:
    return (((v >> 11) & 0x1F) * 255 // 31, ((v >> 5) & 0x3F) * 255 // 63, (v & 0x1F) * 255 // 31)


BAYER = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))


def mix(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def ramp(stops, t):
    t = min(max(t, 0.0), 1.0)
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            return mix(c0, c1, (t - t0) / (t1 - t0) if t1 > t0 else 0)
    return stops[-1][1]


def landscape(w: int, h: int) -> Image.Image:
    """A dusk landscape at the panel's size, posterised and dithered like a
    small-palette video frame."""
    sky = ((0.0, (38, 30, 52)), (0.45, (120, 60, 70)), (0.8, (226, 128, 72)), (1.0, (246, 190, 110)))
    img = Image.new("RGB", (w, h))
    px = img.load()
    horizon = int(h * 0.62)
    sun_x, sun_y, sun_r = w * 0.7, horizon - h * 0.16, h * 0.11

    def ridge(x, base, amp, f, seed):
        return base - amp * (0.55 * math.sin(x * f + seed) + 0.3 * math.sin(x * f * 2.3 + seed * 2)
                             + 0.15 * math.sin(x * f * 5.1 + seed * 3))

    for y in range(h):
        for x in range(w):
            d = (BAYER[y % 4][x % 4] + 0.5) / 16 - 0.5
            if y < horizon:
                c = ramp(sky, (y / horizon) + d * 0.12)
                if (x - sun_x) ** 2 + (y - sun_y) ** 2 < sun_r ** 2:
                    c = (250, 214, 140)
                far = ridge(x, horizon - h * 0.12, h * 0.08, 0.035, 1.0)
                near = ridge(x, horizon - h * 0.02, h * 0.09, 0.022, 4.0)
                if y > near:
                    c = (46, 32, 38)
                elif y > far:
                    c = (92, 56, 66) if d < 0.3 else (104, 64, 72)
            else:
                t = (y - horizon) / (h - horizon)
                c = mix((150, 82, 70), (30, 24, 32), t + d * 0.25)
                if abs(x - sun_x) < sun_r * (1.1 - t) and (y // 2) % 2 == 0 and d > -0.2:
                    c = (232, 168, 100)
            px[x, y] = tuple(v & 0xF8 for v in c)
    return img


def cut(sheet: Image.Image, w: int, cells: dict) -> None:
    bands = {rgb565(v) for v in BANDS}
    frame = landscape(w, 180)
    cw, ch = w * SCALE + GAP, H * SCALE + GAP
    for (row, col), name in cells.items():
        cell = sheet.crop((col * cw, row * ch, col * cw + w * SCALE, row * ch + H * SCALE))
        shot = cell.resize((w, H), Image.NEAREST)
        px, fp = shot.load(), frame.load()
        for y in range(30, 210):
            for x in range(w):
                if px[x, y] in bands:
                    px[x, y] = fp[x, y - 30]
        shot = shot.quantize(colors=64, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        shot.save(OUT / f"{name}.png", optimize=True)
        print(OUT / f"{name}.png")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for flag, w, cells in (([], 240, XINGZHI), (["--passport"], 320, PASSPORT)):
            png = Path(tmp) / f"sheet{w}.png"
            subprocess.run(["bash", str(ROOT / "tools/render_ui/render_ui.sh"), *flag, str(png)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            cut(Image.open(png).convert("RGB"), w, cells)
    return 0


if __name__ == "__main__":
    sys.exit(main())

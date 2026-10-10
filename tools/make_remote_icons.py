#!/usr/bin/env python3
"""Draw the phone remote's home-screen icons (server/relay/assets/icon-*.png).

Run once after changing the design; the PNGs are committed. Needs Pillow and
the macOS Songti font. Colours follow the page: warm ink, vermilion, amber.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Pillow is required: python3 -m pip install Pillow")

INK = (28, 25, 21)
SCREEN = (15, 13, 11)
VERMILION = (210, 85, 63)
AMBER = (255, 181, 71)
SONGTI = "/System/Library/Fonts/Supplemental/Songti.ttc"
OUT = Path(__file__).resolve().parents[1] / "server" / "relay" / "assets"


def draw(size: int = 1024) -> Image.Image:
    image = Image.new("RGB", (size, size), INK)
    d = ImageDraw.Draw(image)
    s = size / 1024
    # Antennas, then the set, then the screen inside it.
    d.line([(512 * s, 300 * s), (380 * s, 170 * s)], fill=VERMILION, width=int(26 * s))
    d.line([(512 * s, 300 * s), (644 * s, 170 * s)], fill=VERMILION, width=int(26 * s))
    d.rounded_rectangle([170 * s, 290 * s, 854 * s, 800 * s], radius=int(70 * s), fill=VERMILION)
    d.rounded_rectangle([225 * s, 345 * s, 799 * s, 745 * s], radius=int(36 * s), fill=SCREEN)
    d.rectangle([420 * s, 800 * s, 604 * s, 850 * s], fill=VERMILION)
    font = ImageFont.truetype(SONGTI, int(330 * s), index=0)
    box = d.textbbox((0, 0), "智", font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text((512 * s - w / 2 - box[0], 545 * s - h / 2 - box[1]), "智", font=font, fill=AMBER)
    return image


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    big = draw()
    for px in (180, 512):
        big.resize((px, px), Image.LANCZOS).save(OUT / f"icon-{px}.png", optimize=True)
        print(OUT / f"icon-{px}.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())

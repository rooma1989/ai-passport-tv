"""Tray icons, drawn at start-up with Pillow (no binary files in the repository).

A small TV with two antennae. "idle": empty screen; "watching": filled screen;
"problem": a dot in the corner. On macOS the image is black on transparent and
marked as a template, so the menu bar tints it for light and dark mode; on
Windows and Linux it is the project's red with a cream screen, readable on light
and dark taskbars alike.
"""

from __future__ import annotations

RED = (210, 85, 63, 255)
CREAM = (239, 229, 208, 255)
INK = (28, 25, 21, 255)
BLACK = (0, 0, 0, 255)
CLEAR = (0, 0, 0, 0)


def draw(kind: str, template: bool, size: int = 64):
    from PIL import Image, ImageDraw
    scale = 4      # draw large, then shrink: smooth edges without anti-aliasing support in ImageDraw
    s = size * scale
    image = Image.new("RGBA", (s, s), CLEAR)
    d = ImageDraw.Draw(image)
    body = BLACK if template else RED
    unit = s / 64
    # antennae
    d.line([(24 * unit, 14 * unit), (16 * unit, 4 * unit)], fill=body, width=int(4 * unit))
    d.line([(40 * unit, 14 * unit), (48 * unit, 4 * unit)], fill=body, width=int(4 * unit))
    # body
    d.rounded_rectangle([4 * unit, 14 * unit, 60 * unit, 58 * unit], radius=9 * unit, fill=body)
    # screen
    screen = [11 * unit, 21 * unit, 53 * unit, 51 * unit]
    if kind == "watching":
        if not template:
            d.rounded_rectangle(screen, radius=4 * unit, fill=CREAM)
            d.rectangle([17 * unit, 29 * unit, 47 * unit, 43 * unit], fill=RED)
        else:
            # a template is one colour: a filled screen shows as a frame with a picture inside
            d.rounded_rectangle(screen, radius=4 * unit, fill=CLEAR)
            d.rectangle([17 * unit, 27 * unit, 47 * unit, 45 * unit], fill=body)
    else:
        d.rounded_rectangle(screen, radius=4 * unit, fill=CLEAR if template else CREAM)
    if kind == "problem":
        cx, cy, r = 52 * unit, 14 * unit, 11 * unit
        d.ellipse([cx - r - 3 * unit, cy - r - 3 * unit, cx + r + 3 * unit, cy + r + 3 * unit], fill=CLEAR)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BLACK if template else (255, 181, 71, 255))
    return image.resize((size, size), Image.LANCZOS)


def save_app_icons(source_png, ico_path, iconset_dir=None) -> None:
    """Build-time helper: the Windows .ico and a macOS .iconset from one PNG."""
    from pathlib import Path

    from PIL import Image
    image = Image.open(source_png).convert("RGBA")
    image.save(ico_path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    if iconset_dir:
        out = Path(iconset_dir)
        out.mkdir(parents=True, exist_ok=True)
        for points in (16, 32, 128, 256, 512):
            image.resize((points, points), Image.LANCZOS).save(out / f"icon_{points}x{points}.png")
            image.resize((points * 2, points * 2), Image.LANCZOS).save(out / f"icon_{points}x{points}@2x.png")

#!/usr/bin/env python3
"""Regenerate every RooTV icon file from the brand SVGs (icon B, "TV with mouse ears").

    python3 tools/make_icons.py [--renderer auto|chrome|cairosvg|rsvg] [--check]

Sources (written by docs/design/media-center/brand/export.mjs; never edit the
outputs by hand, change the SVGs and run this again):

    icon-b.svg        1024 master, used from 40 px up
    icon-b-small.svg  simplified drawing for 16-32 px
    tray-b.svg        single-colour menu bar / tray shape on a 32 grid

Outputs (all tracked, so a build needs neither a browser nor this script):

    packaging/desktop/icons/rootv.icns   macOS app icon, 16-512 @1x/@2x, on Apple's
                                         824/1024 grid with a soft shadow
    packaging/desktop/icons/rootv.ico    Windows app/installer icon, 16-256
    packaging/desktop/icons/rootv.png    Linux .desktop icon, 256
    desktop/assets/tray-template-<state>[@2x].png
                                         macOS menu bar template (black + alpha), 18/36 px
    desktop/assets/tray-<state>-<16|32>.png
                                         Windows/Linux tray, in colour
    server/relay/assets/ and packaging/site/assets/:
        favicon.ico (16/32/48), favicon.svg, apple-touch-icon.png (180, opaque),
        icon-180.png, icon-192.png, icon-512.png (transparent corners),
        icon-maskable-192.png, icon-maskable-512.png (relay only; PWA "maskable")

<state> is idle, watching (the screen lights up) or problem (an amber badge).

Every SVG is rasterised once at 1024 px and scaled down with Pillow, which
averages the area of each output pixel (anti-aliasing as good as rendering the
vector at that size). The rasteriser is the first of: the cairosvg module,
rsvg-convert, or a headless Chrome/Chromium/Edge (`--screenshot` with a
transparent background). On the machine these files were made on only Chrome
was installed; set CHROME=<path> to pick a specific browser.

`--check` renders everything into a temporary folder and reports whether the
tracked files have the expected sizes (it does not compare pixels: browsers
differ by a shade from version to version).
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "docs" / "design" / "media-center" / "brand"
APP_ICONS = ROOT / "packaging" / "desktop" / "icons"
TRAY = ROOT / "desktop" / "assets"
RELAY = ROOT / "server" / "relay" / "assets"
SITE = ROOT / "packaging" / "site" / "assets"

MASTER = 1024
STATES = ("idle", "watching", "problem")
# The screen colour of the small icon, and the one it takes while a device watches.
SCREEN = "#20264a"
SCREEN_LIT = "#5d70d6"
BADGE = (255, 181, 71, 255)          # amber, as the old tray's problem dot
# macOS Big Sur grid: the icon body is 824 of 1024, centred, with a shadow.
MAC_BODY = 824
# PWA maskable icons: everything that matters inside the central 80 % circle.
MASKABLE_SCALE = 0.84
TEMPLATE_PT = 18                      # menu bar image size in points (@1x px)


# ------------------------------------------------------------------ SVG sources

def _read(name: str) -> str:
    text = (BRAND / name).read_text(encoding="utf-8")
    return re.sub(r"<!--.*?-->\s*", "", text, flags=re.S).strip()


_SQUIRCLE = re.compile(r'<path d="M1024\.0 512\.0L[^"]*Z" fill="(url\(#[^)]+\))"/>')


def full_bleed(svg: str) -> str:
    """The squircle background as a full square: for icons the OS masks itself
    (iOS home screen, PWA maskable), where transparent corners would turn black."""
    out, count = _SQUIRCLE.subn(r'<rect width="1024" height="1024" fill="\1"/>', svg)
    if count != 2:
        raise SystemExit(f"make_icons: expected 2 squircle paths in the master icon, found {count}")
    return out


def maskable(svg: str, scale: float = MASKABLE_SCALE) -> str:
    """Full bleed, artwork shrunk into the maskable safe zone (radius 40 %)."""
    svg = full_bleed(svg)
    last = svg.rindex('<rect width="1024" height="1024"')
    cut = svg.index("/>", last) + 2
    offset = MASTER / 2 * (1 - scale)
    return (svg[:cut] + f'<g transform="translate({offset:.2f} {offset:.2f}) scale({scale})">'
            + svg[cut:].replace("</svg>", "</g></svg>"))


def lit(svg: str) -> str:
    if svg.count(f'fill="{SCREEN}"') != 1:
        raise SystemExit(f"make_icons: the small icon's screen colour {SCREEN} is not where it was")
    return svg.replace(f'fill="{SCREEN}"', f'fill="{SCREEN_LIT}"')


# ------------------------------------------------------------------ rasterising

def _chrome() -> str | None:
    candidates = [os.environ.get("CHROME", "")]
    candidates += [shutil.which(n) or "" for n in ("google-chrome", "google-chrome-stable", "chromium",
                                                     "chromium-browser", "chrome", "msedge")]
    candidates += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                   "/Applications/Chromium.app/Contents/MacOS/Chromium",
                   "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                   r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                   r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]
    return next((c for c in candidates if c and Path(c).is_file()), None)


def pick_renderer(name: str) -> str:
    if name in ("auto", "cairosvg"):
        try:
            import cairosvg  # noqa: F401
            return "cairosvg"
        except ImportError:
            if name == "cairosvg":
                raise SystemExit("make_icons: pip install cairosvg, or choose another --renderer")
    if name in ("auto", "rsvg") and shutil.which("rsvg-convert"):
        return "rsvg"
    if name == "rsvg":
        raise SystemExit("make_icons: rsvg-convert not found")
    if _chrome():
        return "chrome"
    raise SystemExit("make_icons: no SVG rasteriser: install cairosvg (pip), rsvg-convert (librsvg) "
                     "or Chrome/Chromium, or set CHROME=<path to the browser>")


def rasterise(svg: str, renderer: str, work: Path, name: str, size: int = MASTER):
    """svg text -> RGBA Pillow image of size x size."""
    from PIL import Image
    work.mkdir(parents=True, exist_ok=True)
    source = work / f"{name}.svg"
    source.write_text(svg, encoding="utf-8")
    png = work / f"{name}.png"
    if renderer == "cairosvg":
        import cairosvg
        cairosvg.svg2png(url=str(source), write_to=str(png), output_width=size, output_height=size)
    elif renderer == "rsvg":
        subprocess.run(["rsvg-convert", "-w", str(size), "-h", str(size), "-o", str(png), str(source)], check=True)
    else:
        page = work / f"{name}.html"
        page.write_text("<!doctype html><html><head><style>html,body{margin:0;padding:0;background:transparent;"
                        f"overflow:hidden}}img{{display:block;width:{size}px;height:{size}px}}</style></head>"
                        f'<body><img src="{source.name}"></body></html>', encoding="utf-8")
        profile = work / "chrome-profile"
        subprocess.run([_chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--mute-audio",
                        "--no-first-run", "--no-default-browser-check", "--force-device-scale-factor=1",
                        "--default-background-color=00000000", f"--window-size={size},{size}",
                        f"--user-data-dir={profile}", f"--screenshot={png}", page.as_uri()],
                       check=True, capture_output=True)
    image = Image.open(png).convert("RGBA")
    if image.size != (size, size):
        raise SystemExit(f"make_icons: {name} rendered at {image.size}, not {size}x{size}")
    if image.getchannel("A").getbbox() is None:
        raise SystemExit(f"make_icons: {name} rendered empty")
    return image


# ------------------------------------------------------------------ image helpers

def scaled(image, size: int):
    from PIL import Image
    return image.resize((size, size), Image.Resampling.LANCZOS)


def with_badge(image, colour, cx: float, cy: float, r: float, gap: float):
    """A round badge with a transparent ring around it, cut into the image (all
    in the image's own pixels; drawn at the 1024 master, so smooth once scaled)."""
    from PIL import Image, ImageChops, ImageDraw
    out = image.copy()
    hole = Image.new("L", out.size, 255)
    ImageDraw.Draw(hole).ellipse([cx - r - gap, cy - r - gap, cx + r + gap, cy + r + gap], fill=0)
    out.putalpha(ImageChops.multiply(out.getchannel("A"), hole))
    ImageDraw.Draw(out).ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)
    return out


def template(image):
    """Black + alpha only: macOS recolours a template image for light and dark menu bars."""
    from PIL import Image
    alpha = image.getchannel("A")
    out = Image.new("RGBA", image.size, (0, 0, 0, 0))
    out.putalpha(alpha)
    return out


def tray_lit(image, unit: float):
    """The tray shape with its screen lit: the screen filled (a thin gap inside
    the frame) and the eyes cut out of it. Coordinates on tray-b.svg's 32 grid."""
    from PIL import Image, ImageChops, ImageDraw
    out = image.copy()
    draw = ImageDraw.Draw(out)
    inset = 2.0
    draw.rounded_rectangle([(7 + inset) * unit, (13.6 + inset) * unit, (25 - inset) * unit, (25 - inset) * unit],
                           radius=(4 - inset) * unit, fill=(0, 0, 0, 255))
    eyes = Image.new("L", out.size, 255)
    for cx in (12.6, 19.4):
        r = 1.9
        ImageDraw.Draw(eyes).ellipse([(cx - r) * unit, (19 - r) * unit, (cx + r) * unit, (19 + r) * unit], fill=0)
    out.putalpha(ImageChops.multiply(out.getchannel("A"), eyes))
    return out


def mac_body(image, size: int):
    """Apple's grid: the body at 824/1024 of the canvas, centred, with a soft shadow."""
    from PIL import Image, ImageFilter
    body = round(size * MAC_BODY / MASTER)
    if body < 1:
        body = 1
    art = scaled(image, body)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    offset = (size - body) // 2
    if size >= 64:
        shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        alpha = Image.new("L", (size, size), 0)
        alpha.paste(art.getchannel("A").point(lambda a: a * 0.32), (offset, offset + round(size * 10 / 1024)))
        shadow.putalpha(alpha.filter(ImageFilter.GaussianBlur(size * 12 / 1024)))
        canvas = Image.alpha_composite(canvas, shadow)
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    layer.paste(art, (offset, offset))
    return Image.alpha_composite(canvas, layer)


def png_bytes(image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def write(path: Path, data: bytes, made: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    made.append(path)


def ico_bytes(images: dict) -> bytes:
    """A multi-size .ico from {size: image} (each size drawn for that size)."""
    sizes = sorted(images)
    biggest = images[sizes[-1]]
    buffer = io.BytesIO()
    biggest.save(buffer, "ICO", sizes=[(s, s) for s in sizes], append_images=[images[s] for s in sizes[:-1]])
    return buffer.getvalue()


def icns_bytes(images: dict, work: Path) -> bytes:
    """.icns via iconutil (macOS), else Pillow's own writer."""
    entries = {16: "icon_16x16", 32: "icon_16x16@2x", 64: "icon_32x32@2x", 128: "icon_128x128",
               256: "icon_128x128@2x", 512: "icon_256x256@2x", 1024: "icon_512x512@2x"}
    if shutil.which("iconutil"):
        iconset = work / "rootv.iconset"
        if iconset.exists():
            shutil.rmtree(iconset)
        iconset.mkdir(parents=True)
        for size, name in entries.items():
            (iconset / f"{name}.png").write_bytes(png_bytes(images[size]))
        # The @1x entries that share a pixel size with an @2x one.
        (iconset / "icon_32x32.png").write_bytes(png_bytes(images[32]))
        (iconset / "icon_256x256.png").write_bytes(png_bytes(images[256]))
        (iconset / "icon_512x512.png").write_bytes(png_bytes(images[512]))
        target = work / "rootv.icns"
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(target)], check=True)
        return target.read_bytes()
    buffer = io.BytesIO()
    images[1024].save(buffer, "ICNS", append_images=[images[s] for s in sorted(images) if s != 1024])
    return buffer.getvalue()


# ------------------------------------------------------------------ the set

def build(renderer: str, work: Path, out_root: Path = ROOT) -> list[Path]:
    def at(path: Path) -> Path:
        return out_root / path.relative_to(ROOT)

    master_svg, small_svg, tray_svg = _read("icon-b.svg"), _read("icon-b-small.svg"), _read("tray-b.svg")
    print(f"rendering with {renderer} ...", flush=True)
    master = rasterise(master_svg, renderer, work, "master")
    small = rasterise(small_svg, renderer, work, "small")
    bleed = rasterise(full_bleed(master_svg), renderer, work, "full-bleed")
    mask = rasterise(maskable(master_svg), renderer, work, "maskable")
    small_lit = rasterise(lit(small_svg), renderer, work, "small-lit")
    tray = template(rasterise(tray_svg, renderer, work, "tray"))

    def app(size: int):
        """The right drawing for a pixel size: the simplified one up to 32 px."""
        return scaled(small if size <= 32 else master, size)

    made: list[Path] = []
    # --- desktop app icons
    write(at(APP_ICONS / "rootv.icns"),
          icns_bytes({s: mac_body(small if s <= 32 else master, s) for s in (16, 32, 64, 128, 256, 512, 1024)},
                     work), made)
    write(at(APP_ICONS / "rootv.ico"), ico_bytes({s: app(s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)}), made)
    write(at(APP_ICONS / "rootv.png"), png_bytes(app(256)), made)

    # --- tray: template (macOS) and colour (Windows, Linux), three states
    unit = MASTER / 32
    shapes = {"idle": tray, "watching": tray_lit(tray, unit),
              "problem": with_badge(tray, (0, 0, 0, 255), 26 * unit, 26 * unit, 4.6 * unit, 1.6 * unit)}
    colour = {"idle": small, "watching": small_lit,
              "problem": with_badge(small, BADGE, 800, 800, 210, 60)}
    for state in STATES:
        write(at(TRAY / f"tray-template-{state}.png"), png_bytes(template(scaled(shapes[state], TEMPLATE_PT))), made)
        write(at(TRAY / f"tray-template-{state}@2x.png"),
              png_bytes(template(scaled(shapes[state], TEMPLATE_PT * 2))), made)
        for size in (16, 32):
            write(at(TRAY / f"tray-{state}-{size}.png"), png_bytes(scaled(colour[state], size)), made)

    # --- web: relay pages (phone remote, admin, console...) and the website
    favicon = ico_bytes({s: app(s) for s in (16, 32, 48)})
    svg_icon = (small_svg.replace(' role="img" aria-label="RooTV"', "") + "\n").encode("utf-8")
    web = {
        "favicon.ico": favicon,
        "favicon.svg": svg_icon,
        # Opaque: iOS masks the corners itself and would show transparent ones black.
        "apple-touch-icon.png": png_bytes(scaled(bleed, 180).convert("RGB")),
        "icon-180.png": png_bytes(app(180)),
        "icon-192.png": png_bytes(app(192)),
        "icon-512.png": png_bytes(app(512)),
    }
    for name, data in web.items():
        write(at(RELAY / name), data, made)
        if name != "icon-192.png":
            write(at(SITE / name), data, made)
    write(at(RELAY / "icon-maskable-192.png"), png_bytes(scaled(mask, 192).convert("RGB")), made)
    write(at(RELAY / "icon-maskable-512.png"), png_bytes(scaled(mask, 512).convert("RGB")), made)
    return made


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--renderer", choices=("auto", "chrome", "cairosvg", "rsvg"), default="auto")
    parser.add_argument("--check", action="store_true", help="render into a temporary folder and compare sizes")
    args = parser.parse_args(argv)
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        raise SystemExit("make_icons: needs Pillow (pip install pillow)")
    renderer = pick_renderer(args.renderer)
    with tempfile.TemporaryDirectory(prefix="rootv-icons-") as tmp:
        work = Path(tmp) / "work"
        if args.check:
            from PIL import Image
            fresh = build(renderer, work, Path(tmp) / "out")
            problems = 0
            for path in fresh:
                tracked = ROOT / path.relative_to(Path(tmp) / "out")
                if not tracked.is_file():
                    print(f"missing: {tracked.relative_to(ROOT)}")
                    problems += 1
                elif path.suffix == ".png" and Image.open(path).size != Image.open(tracked).size:
                    print(f"size differs: {tracked.relative_to(ROOT)}")
                    problems += 1
            print("icons: OK" if not problems else f"icons: {problems} problem(s)")
            return 1 if problems else 0
        made = build(renderer, work)
    total = 0
    for path in made:
        size = path.stat().st_size
        total += size
        print(f"{size:>8}  {path.relative_to(ROOT)}")
    print(f"{total:>8}  bytes in {len(made)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())

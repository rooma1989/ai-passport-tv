#!/usr/bin/env python3
"""Offline lab: how each TV_DEGRADE mode looks when a live frame is over its byte target.

Runs recorded or synthetic index-frame sequences through the server's own encoder
(server/degrade.py, the code live.py calls for every frame: delta stripes against what
the panel shows, the byte target, the chosen mode, the fill pass) and measures what
the panel would show against the frame ffmpeg made:

  bytes     wire bytes per frame / target (mean, max, share of frames over)
  tint      persistent colour cast: |mean over frames of the mean R,G,B error|, 0..255
  flicker   frame-to-frame change of that per-frame mean error (largest channel)
  streak    share of pixels in horizontal runs of 8+ equal pixels that the original
            does not have (percentage points); vstreak the same for vertical runs
  tear      how much more the error jumps across stripe boundaries than between other
            rows (0..255): a stripe left from an older frame, or drawn at another level
  stale     stripes whose content is from an older frame that differs in 3 %+ of the pixels:
            mean share, and the most frames in a row one stripe stayed so (max age)
  psnr/ssim error against the frame as decoded (RGB PSNR over the clip, 8x8-block luma SSIM)
  ms        encode time per frame (mean, p95), measured around the encoder call only

Needs numpy (and Pillow for `synth` and contact sheets); the encoder itself does not.
Run from the repository root.

    # 1. sequences, one file per clip and geometry: <name>.<WxH>.rgb8 (12 fps)
    python tools/degrade_lab.py capture VIDEO --name sport --start 44 --seconds 6 --out DIR
    python tools/degrade_lab.py synth --out DIR
    # 2. compare modes; every clip runs at the production target and at targets that put
    #    its median frame 5 %, 20 % and 50 % over
    python tools/degrade_lab.py run DIR --out RESULTS
    TV_GEOMETRY=240x180 python tools/degrade_lab.py run DIR --out RESULTS
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import degrade, frames  # noqa: E402

W, H = frames.WIDTH, frames.HEIGHT
GEOMETRY = f"{W}x{H}"
FPS = 12
# Production: TV_RATE_START=200000 at 12 fps.
PRODUCTION_TARGET = 200000 // FPS


# --- sequences ---------------------------------------------------------------

def _graph(pre: str = "") -> str:
    pre = f"{pre}," if pre else ""
    return f"[0:v]setpts=PTS-STARTPTS,{pre}fps={FPS},{frames.FIT},format=rgb8[v]"


def _encode_command(inputs: list[str], out: Path, pre: str = "") -> list[str]:
    # The same filter chain and output options as live.source_command.
    return ["ffmpeg", "-v", "error", "-nostdin", *inputs, "-filter_complex", _graph(pre),
            "-map", "[v]", "-pix_fmt", "rgb8", "-sws_dither", "none", "-threads", "1",
            "-f", "rawvideo", "-y", str(out)]


def capture(args) -> int:
    out = Path(args.out) / f"{args.name}.{GEOMETRY}.rgb8"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = _encode_command(["-ss", str(args.start), "-t", str(args.seconds), "-i", args.video], out, args.filter)
    subprocess.run(cmd, check=True)
    print(f"{out}: {out.stat().st_size // frames.FRAME_PIXELS} frames")
    return 0


def _font(size: int):
    from PIL import ImageFont
    for path in ("/System/Library/Fonts/Hiragino Sans GB.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
                 "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _cartoon(n: int):
    """Flat-colour cartoon: sky gradient, hills, a sun, two characters walking, a pan."""
    from PIL import Image, ImageDraw
    sub = _font(30)
    for t in range(n):
        im = Image.new("RGB", (640, 360))
        d = ImageDraw.Draw(im)
        for y in range(200):
            d.line([(0, y), (640, y)], fill=(90 + y // 4, 160 + y // 5, 235))
        pan = t * 4
        for k in range(-1, 5):
            x = k * 220 - pan % 220
            d.ellipse([x - 40, 150, x + 260, 420], fill=(70, 170, 60), outline=(20, 60, 20), width=3)
        d.rectangle([0, 300, 640, 360], fill=(150, 110, 60))
        d.ellipse([520, 30, 590, 100], fill=(255, 220, 40), outline=(200, 120, 0), width=3)
        for who, (x0, colour) in enumerate(((60, (230, 60, 60)), (420, (60, 90, 230)))):
            x = x0 + (t * (3 if who == 0 else -2)) % 200
            bob = (t // 3 % 2) * 4
            d.ellipse([x, 170 + bob, x + 70, 240 + bob], fill=(255, 220, 180), outline=(0, 0, 0), width=3)
            d.ellipse([x + 18, 192 + bob, x + 28, 202 + bob], fill=(0, 0, 0))
            d.ellipse([x + 42, 192 + bob, x + 52, 202 + bob], fill=(0, 0, 0))
            d.rounded_rectangle([x + 5, 240 + bob, x + 65, 310], 12, fill=colour, outline=(0, 0, 0), width=3)
        line = ("大耳朵图图今天去公园", "小猪佩奇和乔治跳泥坑")[t // 36 % 2]
        d.text((320, 330), line, font=sub, fill=(255, 255, 255), anchor="mm", stroke_width=2,
               stroke_fill=(0, 0, 0))
        yield im.tobytes()


def _news(n: int, backdrop: Path | None):
    """Text-heavy news: a still backdrop, a lower third, a channel logo, a scrolling ticker."""
    from PIL import Image, ImageDraw, ImageFilter
    big, small = _font(34), _font(22)
    if backdrop is not None and backdrop.exists():
        base = Image.open(backdrop).convert("RGB").resize((640, 360))
    else:
        base = Image.new("RGB", (640, 360), (30, 50, 90))
    base = base.filter(ImageFilter.GaussianBlur(1))
    ticker = "　".join(["本台消息：今日全国大部地区天气晴好，气温回升",
                       "沪深股市小幅收涨，成交额较上一交易日放大",
                       "国际新闻：多国代表出席气候峰会并发表联合声明"])
    for t in range(n):
        im = base.copy()
        d = ImageDraw.Draw(im)
        d.rectangle([0, 248, 640, 300], fill=(180, 20, 30))
        d.text((24, 274), ("新闻直播间　特别报道", "聚焦今日要闻　权威发布")[t // 30 % 2], font=big,
               fill=(255, 255, 255), anchor="lm")
        d.rectangle([0, 300, 640, 340], fill=(240, 240, 240))
        d.text((640 - t * 6 % 2400, 320), ticker, font=small, fill=(20, 20, 20), anchor="lm")
        d.text((560, 30), "新闻", font=big, fill=(255, 255, 255), anchor="mm", stroke_width=2,
               stroke_fill=(180, 20, 30))
        d.text((20, 20), f"10:{t // 12 + 30:02d}", font=small, fill=(255, 255, 255))
        yield im.tobytes()


def synth(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    count = int(args.seconds * FPS)
    backdrop = Path(args.backdrop) if args.backdrop else None
    for name, source in (("cartoon", _cartoon(count)), ("news", _news(count, backdrop))):
        path = out / f"{name}.{GEOMETRY}.rgb8"
        cmd = _encode_command(["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "640x360",
                               "-r", str(FPS), "-i", "-"], path)
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        for picture in source:
            proc.stdin.write(picture)
        proc.stdin.close()
        if proc.wait():
            return 1
        print(f"{path}: {path.stat().st_size // frames.FRAME_PIXELS} frames")
    return 0


def load(directory: Path) -> dict[str, list[bytes]]:
    clips = {}
    for path in sorted(directory.glob(f"*.{GEOMETRY}.rgb8")):
        data = path.read_bytes()
        n = len(data) // frames.FRAME_PIXELS
        clips[path.name.split(".")[0]] = [data[i * frames.FRAME_PIXELS:(i + 1) * frames.FRAME_PIXELS]
                                          for i in range(n)]
    return clips


# --- metrics -----------------------------------------------------------------

def _np():
    import numpy as np
    return np


def palette():
    np = _np()
    return np.array([((i >> 5) * 36, ((i >> 2) & 7) * 36, (i & 3) * 85) for i in range(256)],
                    np.float64)


def psnr(mse: float) -> float:
    return 99.0 if mse == 0 else float(10 * _np().log10(255.0 ** 2 / mse))


def ssim(a, b) -> float:
    """Mean SSIM of 8x8 luma blocks (non-overlapping), a and b H x W x 3 RGB."""
    np = _np()
    weights = np.array([0.299, 0.587, 0.114])
    ya, yb = a @ weights, b @ weights
    h, w = (H // 8) * 8, (W // 8) * 8
    ba = ya[:h, :w].reshape(h // 8, 8, w // 8, 8).transpose(0, 2, 1, 3).reshape(-1, 64)
    bb = yb[:h, :w].reshape(h // 8, 8, w // 8, 8).transpose(0, 2, 1, 3).reshape(-1, 64)
    ma, mb = ba.mean(1), bb.mean(1)
    va, vb = ba.var(1), bb.var(1)
    cov = ((ba - ma[:, None]) * (bb - mb[:, None])).mean(1)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    return float((((2 * ma * mb + c1) * (2 * cov + c2)) / ((ma ** 2 + mb ** 2 + c1) * (va + vb + c2))).mean())


RUN = 8


def long_runs(index, axis: int) -> float:
    """Share of pixels inside a run of RUN or more equal pixels along `axis`."""
    np = _np()
    a = index if axis == 1 else index.T
    eq = (a[:, 1:] == a[:, :-1]).astype(np.int32)
    c = np.concatenate([np.zeros((a.shape[0], 1), np.int32), np.cumsum(eq, 1)], 1)
    # starts[i]: pixels i..i+RUN-1 are all equal.
    starts = (c[:, RUN - 1:] - c[:, :-(RUN - 1)]) == RUN - 1
    s = np.concatenate([np.zeros((a.shape[0], 1), np.int32), np.cumsum(starts, 1)], 1)
    n = a.shape[1]
    j = np.arange(n)
    hi = np.minimum(j, starts.shape[1] - 1) + 1
    lo = np.maximum(j - RUN + 1, 0)
    covered = (s[:, hi] - s[:, lo]) > 0
    return float(covered.mean())


def tear(error) -> float:
    """How much more the error jumps across stripe boundaries than between other rows.

    `error` is shown minus original (H x W x 3). A stripe left from an older frame, or drawn
    at another level than its neighbour, makes the error change abruptly at its edge.
    """
    np = _np()
    step = np.abs(error[1:] - error[:-1]).mean((1, 2))  # step[y-1]: between rows y-1 and y
    boundary = np.zeros(H - 1, bool)
    boundary[np.arange(frames.STRIPE_ROWS, H, frames.STRIPE_ROWS) - 1] = True
    return float(step[boundary].mean() - step[~boundary].mean())


class Meter:
    """Accumulates the per-frame measurements of one run."""

    def __init__(self, target: int):
        np = _np()
        self.np, self.pal, self.target = np, palette(), target
        self.wire, self.ms, self.levels = [], [], []
        self.bias, self.psnr, self.ssim, self.streak, self.vstreak, self.tear = [], [], [], [], [], []
        self.stale, self.age_max = [], 0
        self.source = [0] * frames.STRIPES
        self.raws: dict[int, bytes] = {}
        self.run = [0] * frames.STRIPES
        self.age_sum = 0.0

    def frame(self, t: int, raw: bytes, shown: bytes, chosen: list[bytes], seconds: float, level: int):
        np = self.np
        self.wire.append(sum(map(len, frames.pack_stripes(chosen))))
        self.ms.append(seconds * 1000)
        self.levels.append(level)
        ri = np.frombuffer(raw, np.uint8).reshape(H, W)
        si = np.frombuffer(shown, np.uint8).reshape(H, W)
        ref, out = self.pal[ri], self.pal[si]
        error = out - ref
        self.bias.append(error.mean((0, 1)))
        mse = float((error ** 2).mean())
        self.psnr.append(mse)
        self.ssim.append(ssim(out, ref))
        self.streak.append((long_runs(si, 1) - long_runs(ri, 1)) * 100)
        self.vstreak.append((long_runs(si, 0) - long_runs(ri, 0)) * 100)
        self.tear.append(tear(error))
        stale = 0
        self.raws[t] = raw
        floor = int(frames.STRIPE_PIXELS * frames.DELTA_MAX_DIFF)
        for at in range(frames.STRIPES):
            if chosen[at]:
                self.source[at] = t
            lo, hi = at * frames.STRIPE_PIXELS, (at + 1) * frames.STRIPE_PIXELS
            # Stale: the content the panel shows here came from a frame whose stripe differs
            # from this frame's in more than 3 % of its pixels (not merely a coarser level).
            # Its age is how many frames in a row it has been so.
            then = self.raws[self.source[at]]
            if self.source[at] < t and frames.differing_pixels(then[lo:hi], raw[lo:hi]) > 3 * floor:
                stale += 1
                self.run[at] += 1
                self.age_max = max(self.age_max, self.run[at])
                self.age_sum += self.run[at]
            else:
                self.run[at] = 0
        self.stale.append(stale / frames.STRIPES)

    def summary(self) -> dict:
        np = self.np
        bias = np.array(self.bias)
        flicker = np.abs(np.diff(bias, axis=0)).max(1) if len(bias) > 1 else np.zeros(1)
        wire = np.array(self.wire) / self.target
        levels = np.array(self.levels)
        stale_n = sum(1 for s in self.stale for _ in range(round(s * frames.STRIPES)))
        return {
            "bytes": float(wire.mean()), "bytes_max": float(wire.max()),
            "over": float((wire > 1.0).mean() * 100),
            "tint": float(np.abs(bias.mean(0)).max()), "tint_rgb": [float(x) for x in bias.mean(0)],
            "tint_frame": float(np.abs(bias).max(1).mean()),
            "flicker": float(flicker.mean()), "flicker_max": float(flicker.max()),
            "streak": float(np.mean(self.streak)), "streak_max": float(np.max(self.streak)),
            "vstreak": float(np.mean(self.vstreak)), "tear": float(np.mean(self.tear)),
            "stale": float(np.mean(self.stale) * 100),
            "stale_age": float(self.age_sum / stale_n) if stale_n else 0.0,
            "age_max": int(self.age_max),
            # PSNR of the whole sequence (mean squared error over all frames), worst frame.
            "psnr": psnr(float(np.mean(self.psnr))), "psnr_min": psnr(float(np.max(self.psnr))),
            "ssim": float(np.mean(self.ssim)), "ssim_min": float(np.min(self.ssim)),
            "ms": float(np.mean(self.ms)), "ms95": float(np.percentile(self.ms, 95)),
            "degraded": float((levels > 0).mean() * 100),
            "switches": int((np.diff(levels) != 0).sum()) if len(levels) > 1 else 0,
        }


# --- runs --------------------------------------------------------------------

def simulate(clip: list[bytes], mode: str, target: int, keep: tuple[int, ...] = (),
             tuning: dict | None = None):
    """Run the encoder over `clip`; returns (summary, {frame index: shown picture})."""
    encoder = degrade.PictureEncoder(mode, **(tuning or {}))
    meter = Meter(target)
    kept = {}
    for t, raw in enumerate(clip):
        started = time.perf_counter()
        chosen, level = encoder.encode(raw, target)
        seconds = time.perf_counter() - started
        meter.frame(t, raw, encoder.shown, chosen, seconds, level)
        if t in keep:
            kept[t] = encoder.shown
    return meter.summary(), kept


def lossless_sizes(clip: list[bytes]) -> list[int]:
    """Wire bytes of each frame with no target, delta against the previous frame."""
    shown, sizes = None, []
    for t, raw in enumerate(clip):
        chosen = frames.choose_stripes(raw, shown, t, 1 << 30)
        sizes.append(frames._wire_size(chosen))
        shown = frames.apply_stripes(raw, shown, chosen)
    return sizes


def sheet(path: Path, clip: list[bytes], picks: list[int], rows: dict[str, dict[int, bytes]],
          scale: int = 1) -> None:
    """Contact sheet: one row per mode (first row the original), one column per frame."""
    np = _np()
    from PIL import Image, ImageDraw
    pal = palette().astype(np.uint8)
    label = 110
    gap = 4
    names = ["original", *rows]
    im = Image.new("RGB", (label + len(picks) * (W * scale + gap), len(names) * (H * scale + gap)),
                   (255, 255, 255))
    d = ImageDraw.Draw(im)
    font = _font(14)
    for r, name in enumerate(names):
        y = r * (H * scale + gap)
        d.text((4, y + H * scale // 2), name, fill=(0, 0, 0), font=font, anchor="lm")
        for c, t in enumerate(picks):
            index = clip[t] if name == "original" else rows[name][t]
            rgb = pal[np.frombuffer(index, np.uint8).reshape(H, W)]
            tile = Image.fromarray(rgb, "RGB")
            if scale != 1:
                tile = tile.resize((W * scale, H * scale), Image.NEAREST)
            im.paste(tile, (label + c * (W * scale + gap), y))
    im.save(path)


DEFAULT_MODES = ("steady", "ladder", "perceptual", "perceptual_2d", "mosaic")


def parse_variant(spec: str) -> dict:
    """NAME=LEVEL,LEVEL;key=value;... -> {NAME: ("steady", tuning)}.

    A LEVEL is steps joined by "+" (degrade.STEPS); an empty level list means deferral only.
    Keys: defer, climb, max_wait, dwell, down (PictureEncoder arguments), fill (0/1), base (a mode).
    """
    name, rest = spec.split("=", 1)
    parts = rest.split(";")
    tuning: dict = {}
    base = "steady"
    if parts[0] != "-":
        tuning["levels"] = tuple(level for level in parts[0].split(",") if level)
    for item in parts[1:]:
        key, value = item.split("=", 1)
        if key == "base":
            base = value
        elif key in ("max_wait", "dwell", "climb"):
            tuning[key] = int(value)
        elif key == "fill":
            tuning[key] = value == "1"
        else:
            tuning[key] = float(value)
    return {name: (base, tuning)}


def run_clip(job) -> list[dict]:
    """Every mode on one clip at every scenario; writes that clip's contact sheets."""
    name, clip, modes, variants, scenarios, out, sheets = job
    degrade.prepare()  # build lookup tables outside the timed encodes
    sizes = lossless_sizes(clip)
    median = statistics.median(sizes[1:])
    targets = {"prod": PRODUCTION_TARGET}
    for over in (1.05, 1.2, 1.5):
        targets[f"+{round((over - 1) * 100)}%"] = int(median / over)
    lines = [f"{name}: {len(clip)} frames, lossless median {median:.0f} B, max {max(sizes[1:])} B"]
    # The frames a sheet shows: the most expensive frame and two around the median.
    ranked = sorted(range(1, len(clip)), key=lambda t: sizes[t])
    picks = sorted({ranked[-1], ranked[len(ranked) // 2], ranked[len(ranked) * 3 // 4]})
    results = []
    for scenario, target in targets.items():
        if scenarios and scenario not in scenarios:
            continue
        kept_rows = {}
        for mode in [*modes, *variants]:
            base, tuning = variants.get(mode, (mode, None))
            if base in degrade.NUMPY_MODES and not degrade.perceptual.AVAILABLE:
                continue
            summary, kept = simulate(clip, base, target, tuple(picks), tuning)
            summary.update(clip=name, scenario=scenario, target=target, mode=mode)
            results.append(summary)
            kept_rows[mode] = kept
            lines.append(
                f"  {scenario:>5} {target:6d} {mode:16s} bytes {summary['bytes']:.2f} "
                f"(max {summary['bytes_max']:.2f}) tint {summary['tint']:.2f} "
                f"flick {summary['flicker']:.2f} streak {summary['streak']:+.1f}/{summary['vstreak']:+.1f} "
                f"tear {summary['tear']:+.1f} stale {summary['stale']:.1f}% age {summary['age_max']} "
                f"psnr {summary['psnr']:.1f} ssim {summary['ssim']:.3f} "
                f"ms {summary['ms']:.1f}/{summary['ms95']:.1f} deg {summary['degraded']:.0f}% "
                f"sw {summary['switches']}")
        if sheets:
            try:
                tag = scenario.replace("%", "").replace("+", "over")
                sheet(out / f"sheet-{GEOMETRY}-{name}-{tag}.png", clip, picks, kept_rows)
            except ImportError:
                pass
    print("\n".join(lines), flush=True)
    return results


def run(args) -> int:
    clips = load(Path(args.frames))
    if not clips:
        print(f"no *.{GEOMETRY}.rgb8 clips in {args.frames}", file=sys.stderr)
        return 1
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    modes = [m for m in args.modes.split(",") if m]
    variants = {}
    for spec in args.variant or ():
        variants.update(parse_variant(spec))
    scenarios = [s for s in args.scenarios.split(",") if s]
    jobs = [(name, clip, modes, variants, scenarios, out, not args.no_sheets)
            for name, clip in clips.items() if not args.clips or name in args.clips.split(",")]
    if args.jobs > 1:
        # Encode times measured in parallel are inflated; use --jobs 1 for the ms columns.
        import multiprocessing
        with multiprocessing.get_context("fork").Pool(args.jobs) as pool:
            parts = pool.map(run_clip, jobs, chunksize=1)
    else:
        parts = [run_clip(job) for job in jobs]
    results = [r for part in parts for r in part]
    (out / f"results-{GEOMETRY}.json").write_text(json.dumps(results, indent=1))
    table(results, out / f"results-{GEOMETRY}.md")
    return 0


COLUMNS = (("bytes", "bytes/target", "{:.2f}"), ("over", "% over", "{:.0f}"),
           ("tint", "tint", "{:.2f}"), ("flicker", "flicker", "{:.2f}"),
           ("streak", "streak", "{:+.1f}"), ("vstreak", "vstreak", "{:+.1f}"),
           ("tear", "tear", "{:+.1f}"), ("stale", "stale %", "{:.1f}"),
           ("age_max", "max age", "{}"), ("psnr", "PSNR", "{:.1f}"), ("ssim", "SSIM", "{:.3f}"),
           ("degraded", "degraded %", "{:.0f}"), ("switches", "level changes", "{}"),
           ("ms", "ms/frame", "{:.1f}"), ("ms95", "ms p95", "{:.1f}"))


def table(results: list[dict], path: Path) -> None:
    """Per-scenario means over clips, one row per mode, written as Markdown and printed."""
    lines = []
    scenarios = list(dict.fromkeys(r["scenario"] for r in results))
    modes = list(dict.fromkeys(r["mode"] for r in results))
    for scenario in scenarios:
        lines.append(f"\n### {GEOMETRY}, target {scenario}\n")
        lines.append("| mode | " + " | ".join(c[1] for c in COLUMNS) + " |")
        lines.append("|---" * (len(COLUMNS) + 1) + "|")
        for mode in modes:
            rows = [r for r in results if r["scenario"] == scenario and r["mode"] == mode]
            if not rows:
                continue
            cells = []
            for key, _, fmt in COLUMNS:
                values = [r[key] for r in rows]
                value = max(values) if key in ("age_max",) else statistics.mean(values)
                cells.append(fmt.format(round(value) if fmt == "{}" else value))
            lines.append(f"| {mode} | " + " | ".join(cells) + " |")
    text = "\n".join(lines) + "\n"
    path.write_text(text)
    print(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    c = sub.add_parser("capture", help="cut a clip from a video into index frames")
    c.add_argument("video")
    c.add_argument("--name", required=True)
    c.add_argument("--start", type=float, default=0.0)
    c.add_argument("--seconds", type=float, default=6.0)
    c.add_argument("--out", required=True)
    c.add_argument("--filter", default="", help="ffmpeg filters applied to the source first, "
                   "e.g. film grain: noise=alls=14:allf=t")
    s = sub.add_parser("synth", help="generate the cartoon and news clips")
    s.add_argument("--out", required=True)
    s.add_argument("--seconds", type=float, default=6.0)
    s.add_argument("--backdrop", default="", help="a picture behind the news graphics")
    r = sub.add_parser("run", help="compare modes on every clip in a directory")
    r.add_argument("frames")
    r.add_argument("--out", required=True)
    r.add_argument("--modes", default=",".join(DEFAULT_MODES))
    r.add_argument("--variant", action="append",
                   help="NAME=LEVEL,LEVEL;key=value: steady with other levels or settings "
                        "(LEVEL is steps joined by +, '-' keeps the default levels)")
    r.add_argument("--clips", default="")
    r.add_argument("--scenarios", default="")
    r.add_argument("--no-sheets", action="store_true")
    r.add_argument("--jobs", type=int, default=1, help="clips run in parallel (inflates ms)")
    args = ap.parse_args(argv)
    return {"capture": capture, "synth": synth, "run": run}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())

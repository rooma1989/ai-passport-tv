<p align="right">
  <a href="2026-10-13-picture-degrade-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# When a live frame does not fit its byte target — design

Status: built on branch `degrade-fix`, 2026-10-13. Server only; measured offline with
`tools/degrade_lab.py` on local and synthetic clips. Not yet watched on a device.

## 1. Problem

Each live frame is a 3-3-2 palette index picture (320x180 on the AI Passport,
240x180 on the Xingzhi) cut into 15 stripes of 12 rows, deflated, and sent only where
it changed against what the device already shows. Every frame must fit a byte target,
`TV_RATE_START` / fps = 200000 / 12 ≈ 16.7 KB on the Passport. What happened to a frame
over the target:

- **`ladder`** (default without numpy) rounds R, G, B to coarser steps for the whole
  frame, choosing the step anew every frame. The rounding always goes up
  (`(v + step/2) // step * step`), so a rung tints the picture warm, and consecutive
  frames on different rungs flicker between tints (blue ↔ yellow).
- **`perceptual`** (default with numpy) lets a pixel reuse the shown or the left
  pixel when its colour is close. The distance weights blue lowest (3, 6, 1), so blue
  is what moves (a blue cast), and left reuse chains into long horizontal runs: the
  streaks seen on the device. `perceptual_2d` adds vertical reuse (vertical garbage);
  `mosaic` is very blurry.
- A frame **1.6 % over** (16,934 B against 16,666 B on a CCTV1 frame) got the whole
  frame degraded.

A detail found while measuring: ffmpeg's `rgb8` output is **ordered-dithered** even with
`-sws_dither none` (odd columns are on average 8 brighter in R and G and up to 20 in B
than even ones on real video). Any step that keeps one pixel of a pair (mosaic's
top-left pixel, a copied row) keeps one dither phase and tints the picture; that is
part of the yellow cast of `mosaic` and of `ladder`.

## 2. The harness (`tools/degrade_lab.py`)

- `capture VIDEO --name N --start S --seconds 6` cuts a clip with the server's own
  filter chain (`fps=12`, `frames.FIT`, `format=rgb8`, `-sws_dither none`) into
  `N.<WxH>.rgb8`; `--filter` adds ffmpeg filters before it (film grain for the "drama"
  clip). `synth` draws a flat-colour cartoon with subtitles and a news screen (backdrop,
  lower third, scrolling ticker) and passes them through the same chain.
- `run DIR` runs every clip through `server/degrade.py`'s `PictureEncoder`, the class
  `live.py` uses for every frame (delta against what is shown, refresh stripe, the
  mode, the fill pass), at the production target and at targets that put the clip's
  median frame 5 %, 20 % and 50 % over. Per mode it reports, against the frame ffmpeg
  made: wire bytes / target; **tint** (|mean over the clip of the per-frame mean R, G, B
  error|, largest channel, 0..255); **flicker** (frame-to-frame change of that per-frame
  error); **streak** / **vstreak** (share of pixels in horizontal / vertical runs of 8+
  equal pixels the original does not have, percentage points); **tear** (how much more
  the error jumps across stripe boundaries than between other rows); **stale** (share
  of stripes showing content from an older frame that differs in 3 %+ of the pixels,
  and the most frames in a row one stripe stays so); **PSNR** (over the clip) and
  **SSIM** (8x8 luma blocks); share of frames degraded, level changes; encode ms/frame.
  It writes `results-<WxH>.md/.json` and contact sheets (original and every mode, three
  frames: the most expensive and two around the median).
- Clips used here (6 s, 72 frames each): *corridor* (static CCTV corridor with
  posters, people walking: text-heavy), *street* (crowded street, high detail and
  motion), *sport* (desert car chase, fast motion), *talk* (people talking in cars,
  cuts), *drama* (the same footage darkened with temporal film grain, like the CCTV1
  drama), *cartoon* and *news* (synthetic). Lossless medians at 320x180: 13.9–26.0 KB.

## 3. What was tried

| Idea | Result |
|---|---|
| Defer the least-changed stripes to the next frame, oldest first after 2 frames | The cheapest loss by far: at 20 % over, SSIM 0.956 with no colour or detail given up. But the stale share grows with the overshoot (16 % of stripes at +20 %, 30 % at +50 %) and with it tearing and judder, so it is the answer to small overshoots, not to large ones |
| Colour rungs made symmetric (merge levels 2,3,4 → 3, etc.; `n1`, `n2`) | Still tints: real areas sit on one level (beige pavement is R 4, G 4), and moving R and G but not B turns them blue/lavender (tint 1–4, SSIM below spatial steps). Kept only as a lab option (`steady_colour`) |
| Temporal hold (keep the shown pixel when within one level) | Mottled, and kept the controller at coarse levels; dropped |
| Spatial steps that average, not pick: merge 2 rows (`rows`), half of the row pairs (`rowsh`), columns (`cols`), 4 rows (`rows4`) | Size ~0.84 / 0.61 / 0.47 (`rows+cols`) / 0.28 (`rows4+cols`) of the frame; tint ≤ 0.5. Chosen |
| Rounding of an average that falls exactly between two levels | Alternating by column correlates with the dither: blue +7…−11. Pseudo-random: unbiased but deflate cannot compress it (`rows` saved only 6–40 %). Blocks of 8: unbiased and compressible but visible 8-px columns (a blue step is 85). Chosen: alternate per pixel, flip the phase every 8 pixels and every merged line — every dither phase rounds up and down equally (bias ≤ 0.3), the pattern is as fine as the dither and compresses |
| Climb on any frame over 1.25x; return when the finer level fits 0.85x | Stuck coarse after one peak (74 % of frames degraded at the production target) |

## 4. The new default: `TV_DEGRADE=steady`

`server/degrade.py`, standard library only.

1. **Fits → unchanged.** All stripes that changed (more than 1 % of their pixels)
   plus the rotating refresh stripe are compressed at the current level; if that fits,
   it goes, and `fill` spends what is left.
2. **Small overshoot → defer.** Otherwise stripes go in order: the refresh stripe, any
   stripe that has waited `MAX_WAIT` = 2 frames, then by how many pixels changed;
   what does not fit waits. A deferred stripe stays as the device shows it.
3. **Sustained large overshoot → one level coarser.** Only when the changed stripes
   are more than `1 + DEFER` = 1.25x the target for `CLIMB` = 3 frames in a row (a scene
   cut is absorbed by deferral), and only if the coarser level is actually smaller.
4. **Back down with hysteresis.** After `DWELL` = 12 frames (1 s) at a level it tries
   the finer one on the current frame and steps down only if its changed stripes fit
   `DOWN` = 1.1x the target (between 1.1 and 1.25 nothing moves). Stripes still drawn at
   the coarser level are redrawn with spare bytes, coarsest first. Stepping down and
   having to climb again within 2 x DWELL doubles the next wait (up to 8x).
5. **Levels** (`rowsh`, `rows`, `rows+cols`, `rows4+cols`): soften, never tint or
   streak. Content comparison is per stripe in the level it was drawn at, so a stripe
   drawn coarser is not resent just because the level changed.
6. **Refresh** sends the new picture when the stripe changed at all or the device has
   it at a coarser level, and otherwise resends what the device already has, so loss
   repair never swaps a finer stripe for a coarser one. The first frame climbs until the
   whole picture fits (if no level fits, the smallest is used and the stripes left out
   are sent next).

The old modes stay selectable (`TV_DEGRADE=ladder|perceptual|perceptual_2d|mosaic`) and
behave exactly as before; `TV_ENCODE=delta|fill` too.

## 5. Measurements (320x180, mean over 7 clips)

Encode times were measured in parallel (7 processes) and are only comparable within a
table; section 6 has the single-process times.

**production target (16,666 B)**

| mode | bytes/target | tint | flicker | streak | vstreak | tear | stale % | max age | PSNR | SSIM | degraded % | level changes | ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `steady` | 0.91 | 0.04 | 0.11 | +0.3 | +1.1 | -0.1 | 2.3 | 2 | 26.1 | 0.875 | 49 | 1.3 | 9.9 |
| `defer_only` | 0.93 | 0.37 | 0.44 | +0.3 | +0.3 | +0.2 | 11.3 | 2 | 28.6 | 0.964 | 0 | 0.0 | 10.4 |
| `steady_d40` | 0.91 | 0.03 | 0.11 | +0.0 | +0.6 | -0.1 | 5.9 | 2 | 28.7 | 0.926 | 20 | 1.0 | 10.7 |
| `steady_colour` | 0.92 | 2.61 | 0.20 | +2.8 | +3.4 | +0.2 | 4.0 | 2 | 27.3 | 0.883 | 49 | 1.1 | 9.3 |
| `ladder` | 0.86 | 11.59 | 0.55 | +5.7 | +5.7 | -0.0 | 0.0 | 0 | 24.1 | 0.813 | 70 | 2.0 | 18.0 |
| `perceptual` | 0.85 | 1.91 | 0.23 | +34.8 | +3.8 | -0.2 | 0.0 | 0 | 22.5 | 0.806 | 70 | 3.1 | 36.3 |
| `perceptual_2d` | 0.82 | 2.77 | 0.42 | +15.8 | +43.3 | -0.0 | 0.0 | 0 | 21.4 | 0.752 | 70 | 2.4 | 47.4 |
| `mosaic` | 0.61 | 12.12 | 1.01 | +22.8 | +22.4 | +1.4 | 0.0 | 0 | 19.5 | 0.654 | 70 | 2.0 | 15.3 |

**median frame +5% over target**

| mode | bytes/target | tint | flicker | streak | vstreak | tear | stale % | max age | PSNR | SSIM | degraded % | level changes | ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `steady` | 0.95 | 0.03 | 0.12 | +0.6 | +0.4 | +0.2 | 5.0 | 2 | 27.4 | 0.940 | 23 | 1.6 | 8.9 |
| `defer_only` | 0.96 | 0.28 | 0.46 | +0.2 | +0.2 | +0.4 | 7.2 | 2 | 29.0 | 0.981 | 0 | 0.0 | 8.8 |
| `steady_d40` | 0.95 | 0.05 | 0.16 | +0.2 | +0.1 | +0.2 | 6.5 | 2 | 30.0 | 0.966 | 8 | 1.3 | 9.4 |
| `steady_colour` | 0.94 | 0.94 | 0.32 | +1.5 | +1.8 | +0.2 | 4.6 | 2 | 28.6 | 0.945 | 23 | 1.3 | 8.5 |
| `ladder` | 0.83 | 15.28 | 0.73 | +3.8 | +4.3 | -0.1 | 0.0 | 0 | 23.0 | 0.809 | 83 | 2.3 | 19.0 |
| `perceptual` | 0.85 | 2.64 | 0.38 | +34.8 | +4.5 | -0.2 | 0.0 | 0 | 21.2 | 0.802 | 83 | 4.6 | 46.4 |
| `perceptual_2d` | 0.83 | 2.77 | 0.61 | +17.9 | +44.0 | -0.1 | 0.0 | 1 | 20.3 | 0.744 | 83 | 4.9 | 59.6 |
| `mosaic` | 0.51 | 15.16 | 0.98 | +29.3 | +28.4 | +1.3 | 0.0 | 0 | 18.0 | 0.594 | 83 | 2.0 | 17.4 |

**median frame +20% over target**

| mode | bytes/target | tint | flicker | streak | vstreak | tear | stale % | max age | PSNR | SSIM | degraded % | level changes | ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `steady` | 0.94 | 0.06 | 0.17 | +1.3 | +1.6 | -0.0 | 2.5 | 2 | 21.7 | 0.796 | 87 | 2.1 | 9.4 |
| `defer_only` | 0.96 | 0.47 | 0.84 | +0.4 | +0.3 | +0.7 | 16.1 | 2 | 25.8 | 0.956 | 0 | 0.0 | 8.6 |
| `steady_d40` | 0.96 | 0.05 | 0.28 | +0.8 | +0.6 | +0.3 | 12.3 | 2 | 24.2 | 0.888 | 37 | 2.0 | 7.1 |
| `steady_colour` | 0.95 | 3.88 | 0.63 | +4.9 | +5.7 | +0.1 | 3.7 | 2 | 24.0 | 0.845 | 84 | 2.0 | 5.5 |
| `ladder` | 0.90 | 13.27 | 1.16 | +6.9 | +7.7 | -0.1 | 0.0 | 0 | 22.0 | 0.761 | 92 | 3.6 | 19.4 |
| `perceptual` | 0.88 | 2.44 | 0.47 | +45.2 | +2.7 | -0.2 | 0.0 | 1 | 20.3 | 0.728 | 92 | 5.0 | 48.7 |
| `perceptual_2d` | 0.83 | 3.52 | 0.74 | +20.7 | +56.0 | -0.1 | 0.0 | 1 | 19.4 | 0.663 | 92 | 2.6 | 58.2 |
| `mosaic` | 0.52 | 17.24 | 0.73 | +33.2 | +31.9 | +1.4 | 0.0 | 0 | 17.5 | 0.552 | 92 | 1.0 | 17.1 |

**median frame +50% over target**

| mode | bytes/target | tint | flicker | streak | vstreak | tear | stale % | max age | PSNR | SSIM | degraded % | level changes | ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `steady` | 0.92 | 0.09 | 0.28 | +2.9 | +4.4 | -0.3 | 3.7 | 2 | 18.9 | 0.632 | 100 | 1.7 | 6.4 |
| `defer_only` | 0.98 | 0.69 | 1.47 | +0.5 | +0.5 | +1.5 | 29.7 | 2 | 23.0 | 0.910 | 0 | 0.0 | 7.9 |
| `steady_d40` | 0.96 | 0.09 | 0.29 | +2.3 | +2.9 | +0.2 | 11.7 | 2 | 19.9 | 0.699 | 100 | 2.0 | 7.7 |
| `steady_colour` | 0.94 | 3.48 | 0.81 | +7.7 | +9.0 | +0.2 | 5.0 | 2 | 21.6 | 0.709 | 100 | 0.9 | 4.9 |
| `ladder` | 0.86 | 15.31 | 1.45 | +23.7 | +24.7 | -0.1 | 0.0 | 0 | 18.0 | 0.610 | 99 | 4.1 | 30.7 |
| `perceptual` | 0.82 | 3.64 | 0.64 | +65.1 | +2.9 | +0.0 | 0.0 | 1 | 18.8 | 0.609 | 99 | 5.4 | 42.8 |
| `perceptual_2d` | 0.84 | 5.01 | 1.09 | +26.0 | +70.5 | -0.1 | 0.0 | 1 | 18.4 | 0.578 | 99 | 4.0 | 57.2 |
| `mosaic` | 0.61 | 19.15 | 0.55 | +36.8 | +34.9 | +1.5 | 0.0 | 0 | 17.2 | 0.519 | 99 | 0.3 | 17.2 |

240x180 (Xingzhi):

**production target (16,666 B)**

| mode | bytes/target | tint | flicker | streak | vstreak | tear | stale % | max age | PSNR | SSIM | degraded % | level changes | ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `steady` | 0.69 | 0.00 | 0.00 | +0.0 | +0.0 | +0.0 | 0.0 | 0 | 99.0 | 1.000 | 0 | 0.0 | 3.4 |
| `defer_only` | 0.69 | 0.00 | 0.00 | +0.0 | +0.0 | +0.0 | 0.0 | 0 | 99.0 | 1.000 | 0 | 0.0 | 2.5 |
| `steady_d40` | 0.69 | 0.00 | 0.00 | +0.0 | +0.0 | +0.0 | 0.0 | 0 | 99.0 | 1.000 | 0 | 0.0 | 6.9 |
| `steady_colour` | 0.69 | 0.00 | 0.00 | +0.0 | +0.0 | +0.0 | 0.0 | 0 | 99.0 | 1.000 | 0 | 0.0 | 8.1 |
| `ladder` | 0.69 | 0.00 | 0.00 | +0.0 | +0.0 | +0.0 | 0.0 | 0 | 99.0 | 1.000 | 0 | 0.0 | 7.4 |
| `perceptual` | 0.69 | 0.00 | 0.00 | +0.0 | +0.0 | +0.0 | 0.0 | 0 | 99.0 | 1.000 | 0 | 0.0 | 6.9 |
| `perceptual_2d` | 0.69 | 0.00 | 0.00 | +0.0 | +0.0 | +0.0 | 0.0 | 0 | 99.0 | 1.000 | 0 | 0.0 | 6.3 |
| `mosaic` | 0.69 | 0.00 | 0.00 | +0.0 | +0.0 | +0.0 | 0.0 | 0 | 99.0 | 1.000 | 0 | 0.0 | 6.6 |

**median frame +5% over target**

| mode | bytes/target | tint | flicker | streak | vstreak | tear | stale % | max age | PSNR | SSIM | degraded % | level changes | ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `steady` | 0.94 | 0.03 | 0.09 | +0.3 | +0.3 | +0.4 | 4.4 | 2 | 28.1 | 0.955 | 28 | 2.0 | 4.8 |
| `defer_only` | 0.95 | 0.24 | 0.38 | +0.2 | +0.2 | +0.6 | 6.1 | 2 | 29.8 | 0.986 | 0 | 0.0 | 3.0 |
| `steady_d40` | 0.95 | 0.03 | 0.13 | +0.2 | +0.1 | +0.4 | 5.6 | 2 | 30.9 | 0.976 | 9 | 1.4 | 3.0 |
| `steady_colour` | 0.93 | 0.76 | 0.27 | +1.1 | +1.4 | +0.5 | 4.0 | 2 | 29.5 | 0.959 | 25 | 1.7 | 3.0 |
| `ladder` | 0.84 | 11.36 | 0.76 | +2.5 | +2.9 | +0.9 | 0.0 | 1 | 24.3 | 0.869 | 83 | 3.0 | 10.1 |
| `perceptual` | 0.87 | 1.73 | 0.51 | +23.7 | +2.2 | +1.3 | 0.0 | 0 | 22.6 | 0.867 | 83 | 7.1 | 31.1 |
| `perceptual_2d` | 0.81 | 2.20 | 0.68 | +11.2 | +33.3 | +1.6 | 0.1 | 7 | 21.4 | 0.797 | 83 | 5.9 | 45.8 |
| `mosaic` | 0.52 | 11.52 | 0.90 | +20.0 | +19.9 | +2.7 | 0.1 | 1 | 19.1 | 0.709 | 83 | 3.0 | 12.1 |

**median frame +20% over target**

| mode | bytes/target | tint | flicker | streak | vstreak | tear | stale % | max age | PSNR | SSIM | degraded % | level changes | ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `steady` | 0.94 | 0.04 | 0.15 | +0.8 | +1.1 | +1.5 | 1.9 | 2 | 22.7 | 0.853 | 90 | 2.0 | 3.2 |
| `defer_only` | 0.97 | 0.38 | 0.69 | +0.3 | +0.3 | +1.0 | 12.7 | 2 | 27.1 | 0.971 | 0 | 0.0 | 2.7 |
| `steady_d40` | 0.96 | 0.05 | 0.24 | +0.5 | +0.4 | +0.9 | 9.7 | 2 | 25.9 | 0.931 | 32 | 2.3 | 3.2 |
| `steady_colour` | 0.94 | 3.19 | 0.52 | +3.6 | +4.4 | +1.0 | 3.0 | 3 | 24.9 | 0.885 | 88 | 2.0 | 4.1 |
| `ladder` | 0.90 | 9.70 | 0.74 | +4.9 | +5.6 | +1.1 | 0.0 | 1 | 23.3 | 0.833 | 91 | 3.1 | 10.7 |
| `perceptual` | 0.89 | 1.76 | 0.31 | +32.5 | +2.2 | +1.6 | 0.1 | 7 | 21.6 | 0.812 | 91 | 2.6 | 31.1 |
| `perceptual_2d` | 0.84 | 3.62 | 0.68 | +13.2 | +39.6 | +1.9 | 0.1 | 7 | 20.5 | 0.749 | 91 | 3.0 | 40.4 |
| `mosaic` | 0.54 | 12.85 | 0.81 | +22.6 | +22.1 | +3.0 | 0.1 | 1 | 18.6 | 0.679 | 91 | 1.0 | 12.1 |

**median frame +50% over target**

| mode | bytes/target | tint | flicker | streak | vstreak | tear | stale % | max age | PSNR | SSIM | degraded % | level changes | ms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `steady` | 0.92 | 0.08 | 0.22 | +1.8 | +2.9 | +2.1 | 2.8 | 2 | 20.2 | 0.745 | 100 | 2.4 | 4.9 |
| `defer_only` | 0.97 | 0.50 | 1.10 | +0.4 | +0.4 | +1.7 | 23.3 | 2 | 24.3 | 0.940 | 0 | 0.0 | 2.6 |
| `steady_d40` | 0.96 | 0.06 | 0.24 | +1.5 | +2.0 | +2.2 | 8.7 | 2 | 21.0 | 0.788 | 100 | 1.9 | 4.6 |
| `steady_colour` | 0.93 | 2.65 | 0.65 | +5.8 | +6.7 | +1.4 | 4.5 | 5 | 22.8 | 0.795 | 100 | 1.1 | 4.7 |
| `ladder` | 0.86 | 11.46 | 1.44 | +17.3 | +18.4 | +2.0 | 0.3 | 5 | 19.2 | 0.722 | 99 | 5.0 | 17.6 |
| `perceptual` | 0.84 | 3.12 | 0.46 | +47.1 | +2.3 | +2.2 | 0.2 | 7 | 20.1 | 0.727 | 99 | 4.6 | 29.2 |
| `perceptual_2d` | 0.86 | 4.67 | 0.98 | +17.6 | +50.8 | +2.1 | 0.3 | 7 | 19.3 | 0.675 | 99 | 3.6 | 38.7 |
| `mosaic` | 0.61 | 14.47 | 0.69 | +25.5 | +24.5 | +3.2 | 0.1 | 1 | 18.2 | 0.653 | 99 | 0.3 | 10.4 |

Reading: `steady` keeps the tint below 0.1 (ladder 11–15, mosaic 12–19, perceptual
2–4) and flicker at 0.1–0.3 (ladder 0.6–1.5), has no streaks (perceptual 35–65 pp
horizontal, perceptual_2d 43–70 pp vertical), keeps stale stripes to 2–5 % and never more
than 2 frames, and has a better SSIM than every old mode at every overshoot. Pure
deferral (`defer_only`) has better SSIM still, but at +20/+50 % 16/30 % of the stripes
are stale each frame (tear 0.7/1.5, flicker 0.8/1.5): fine for small overshoots, which is
what `steady` uses it for. `TV_STEADY_DEFER=0.4` (`steady_d40`) trades more staleness
(6–12 %) for less softening; a setting to try on a device. At 240x180 the clips are
8–16 KB, so the production target is never exceeded; the overshoot scenarios show the
same ranking.

## 6. CPU

Single process, Apple M1 Pro, all 7 clips at the production target and at +20 % / +50 %,
encode only (the call `live.py` makes per frame):

| mode | ms/frame (mean) | p95 (mean over runs) | worst p95 |
|---|---|---|---|
| `steady` | 3.5 | 5.9 | 10.5 |
| `defer_only` | 3.2 | 3.8 | 7.1 |
| `ladder` | 7.8 | 9.9 | 26.1 |
| `perceptual` | 13.5 | 17.4 | 24.6 |
| `perceptual_2d` | 17.4 | 23.1 | 30.6 |
| `mosaic` | 5.5 | 6.6 | 9.1 |

`steady` costs about a quarter of `perceptual` (which used about 40 % of the 1-vCPU host
for the whole Passport media process at 12 fps) and half of `ladder`, and needs no numpy.
Most of its time is deflate of the changed stripes; a level adds 1–3 ms (row and column
averaging through 131072-entry lookup tables, built once per process in about 0.2 s when
the first channel starts).

## 7. Not done / to check on a device

- Watch `steady` on both boards on a busy channel and on CCTV1; compare with
  `TV_DEGRADE=ladder` (the stopgap now on the host).
- The clips are local and synthetic; real broadcast frames can be captured on the
  host with the same chain and run through `tools/degrade_lab.py run`.
- `perceptual` was not fixed: `steady` makes it unnecessary, and it is kept unchanged
  for comparison.

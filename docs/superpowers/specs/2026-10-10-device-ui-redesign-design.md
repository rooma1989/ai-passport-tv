<p align="right">
  <a href="2026-10-10-device-ui-redesign-design.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Device UI redesign — design record

Status: built and verified on the device, 2026-10-10. Source: a handoff from
Claude Design (`HANDOFF.md`, `xiaozhi_tv_ui_assets.h`, PNGs and HTML mock-ups),
kept outside the repository. This records what was built from it and the
decisions taken on the way.

## Assets and palette

- `main/xiaozhi_tv_ui_assets.h` is the design's export, unchanged: eight RGB565
  colours, 26 one-bit 16×16 icons and 11 pets at 24×24, 4 bits a pixel, three
  or four moods each (about 10 KB in flash). Only `main/ui_draw.c` includes it.
- `ui_text.h` repeats the eight colours token for token; `ui_draw.c` includes
  both, so a difference is a macro redefinition the `-Werror` build rejects. The
  old role names (`UI_COLOR_TEXT` and so on) map onto the new palette.

## Layout

- Top bar rows 0-29 and bottom bar rows 210-239 in `UI_DEEP`; the picture,
  menus and pages in rows 30-209; a 16 px safe margin.
- Coordinates in the handoff: "text box (x, y)" is the padded box, so glyphs
  start at (x+6, y+4); "y=N centred" is the glyph top.
- Every screen is drawn per 12-row stripe, clipped, so a 3× pet (72×72) needs
  no buffer of its own. `ui_overlay.c` holds every screen; `av_player.c`
  decides which and when.

## Phases

1. Palette, assets, the top-bar label (multi-colour segments), the opaque
   panel, scaled text, icons and pets, dotted rules, notched rectangles, the
   ten-cell bar.
2. Over the picture: the status bar's states (low, critical blink, charging,
   unknown), the volume box with mute, the brightness box when the phone sets
   it, the channel banner, the seek box, the pause band and its bottom line.
3. The channel list (groups with icons and counts, channels with numbers and a
   play mark, a scroll bar, rates in the bottom bar), the brightness page and the
   status page. No English left on screen.
4. Waiting screens instead of colour bars: joining Wi-Fi, connecting to the
   server, opening a channel, server not answering. The pet is stored in NVS,
   bobs on these screens, and the picker opens from the status page.
5. The four setup pages, the last with a countdown repainted each second.
6. The phone's My pets page and the pet command.

## Protocol additions

- Relay events `seek` (seconds, negative is back), `next` and `pet` (index).
- `AV_SWITCH {"seek": s}` / `{"next": 1}` go only to firmware whose HELLO says
  `"hint": 1`; the device shows the seek box until frames come back after a gap,
  then half a second more, and at most 8 s.
- `AV_SWITCH {"pet": i}` goes only to firmware whose HELLO carries `pet`. HELLO
  and STATUS report the pet; `now-playing.json` and the relay's state carry it.
- `POST /api/pet {"pet": i}`; the pet images are served at `/tv/pets/`.

## Font fixes found on the way

- Narrow glyphs are centred in their 16 px cells but advance by their ink width,
  so their ink sat several pixels right of the pen; the renderer now draws them
  from the pen.
- ASCII was centred vertically glyph by glyph (a full stop at mid-height).
  `tools/make_font.py` now puts it on a baseline at row 12; `--ascii-only`
  rewrites only the ASCII tables and leaves the CJK blob as it was.
- ▼ · ◀ ▶ are missing from the font and are drawn from built-in bitmaps (▼ is
  the font's ▲ turned over). ，。、 are centred like hanzi in the blob, so
  built-in glyphs that sit low at the left take precedence.

## Left out

- Blinking on the waiting screens: the handoff allows bobbing alone.

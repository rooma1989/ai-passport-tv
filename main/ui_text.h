// Bitmap text and rectangle drawing over a stripe of the panel. No ESP-IDF
// dependencies, so the layout maths is host-testable.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// A band of the panel the caller owns and is about to push. Pixels are RGB565
// with the high byte first, `width` pixels per row, and the band covers
// destination rows [origin_y, origin_y + rows). Anything drawn outside is
// clipped, which is what makes partial-height stripes safe to draw into.
typedef struct {
    uint8_t *pixels;
    int width;
    int origin_y;
    int rows;
} ui_surface_t;

// Glyph cell height. A line of text occupies exactly this many rows.
#define UI_TEXT_LINE_H 16u
// Horizontal gap between the text and the edge of its background box.
#define UI_TEXT_PAD_X 6
#define UI_TEXT_PAD_Y 4

// The palette, RGB565. The same eight values are defined, token for token, in
// xiaozhi_tv_ui_assets.h, the design's own export; a copy here keeps that
// 78 KB header out of every file that only wants a colour, and ui_draw.c
// includes both, where any drift is a redefinition the -Werror build rejects.
#define UI_INK    0x18C2  /* panels, full-screen pages, overlay boxes */
#define UI_DEEP   0x1061  /* top and bottom bars; text on a selected row */
#define UI_RULE   0x39A5  /* rules, dotted lines, unlit cells and signal bars */
#define UI_FAINT  0x6B2A  /* battery outline, scroll thumb; never text */
#define UI_MUTED  0x8C0E  /* labels, hints, counts */
#define UI_PAPER  0xE6D8  /* body text, channel names, light QR modules */
#define UI_AMBER  0xFDA9  /* channel numbers, values, key names in hints */
#define UI_VERM   0xD2A8  /* selected row, errors, low battery */

// Colours by role, for the screens not yet redrawn to the new layout. Names
// describe the role, not the shade.
#define UI_COLOR_BOX     UI_INK    // panel behind text
#define UI_COLOR_TEXT    UI_PAPER
#define UI_COLOR_DIM     UI_MUTED  // secondary lines
#define UI_COLOR_SELECT  UI_VERM   // highlight bar for the chosen row
#define UI_COLOR_SELECT_TEXT UI_DEEP
#define UI_COLOR_RULE    UI_RULE   // separator
#define UI_COLOR_BACKDROP UI_INK   // status page background

// Fill a rectangle. Coordinates are screen-space; the band clips them.
void ui_text_fill(ui_surface_t surface, int x, int y, int w, int h, uint16_t color);

// Draw one line of text with an opaque background box, so it stays readable over
// moving pictures. Returns the x coordinate just past the text.
int ui_text_draw(ui_surface_t surface, int x, int y, const char *text,
                 uint16_t foreground, uint16_t background);

// The same text with every dot drawn as a `scale` x `scale` block, for the
// large numbers (channel number, percentage, countdown). (x, y) is where the
// glyphs start, as for ui_text_draw, but the background is the glyph cells
// alone -- width * scale by UI_TEXT_LINE_H * scale -- with no padding, so a
// layout can place the block exactly. Returns the x just past the text.
int ui_text_draw_scaled(ui_surface_t surface, int x, int y, const char *text, int scale,
                        uint16_t foreground, uint16_t background);

// Width in pixels the text would occupy, for centring and for truncation.
int ui_text_width(const char *text);

// Copy at most `limit` bytes of `text` into `out` without splitting a character,
// then terminate. Returns true when the whole string fitted.
bool ui_text_truncate(char *out, size_t out_size, const char *text, int limit);

// Fit `text` into `limit` pixels: all of it when it fits, otherwise as many
// whole characters as leave room for a trailing "…". Returns true when nothing
// was cut.
bool ui_text_ellipsize(char *out, size_t out_size, const char *text, int limit);

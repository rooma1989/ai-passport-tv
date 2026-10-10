#include "ui_text.h"

#include "ui_font_data.h"

#include <string.h>

// The non-ASCII glyphs are one binary blob embedded in the image by the
// component's EMBED_FILES: a glyph count, then that many code points, then an
// advance per glyph, then the glyph bitmaps. It is not a C array because the
// same data written as hex would be about a megabyte of source that every
// rebuild reparses. tools/make_font.py writes it and defines its layout.
// __asm__ rather than asm: the host tests build with -std=c11, where the plain
// spelling is not a keyword and clang rejects it, while the double-underscore
// form is understood by both compilers.
extern const uint8_t ui_font_cjk_blob_start[] __asm__("_binary_ui_font_cjk_bin_start");

// Decode one UTF-8 sequence. Returns the number of bytes consumed and writes the
// code point, or 0 on a malformed or truncated sequence. Only the lengths needed
// for BMP text are accepted; anything else is treated as one replacement cell.
static unsigned utf8_next(const char *text, uint32_t *code)
{
    const unsigned char *p = (const unsigned char *)text;
    if (p[0] < 0x80u) {
        *code = p[0];
        return 1;
    }
    if ((p[0] & 0xE0u) == 0xC0u && (p[1] & 0xC0u) == 0x80u) {
        *code = ((uint32_t)(p[0] & 0x1Fu) << 6) | (uint32_t)(p[1] & 0x3Fu);
        return 2;
    }
    if ((p[0] & 0xF0u) == 0xE0u && (p[1] & 0xC0u) == 0x80u && (p[2] & 0xC0u) == 0x80u) {
        *code = ((uint32_t)(p[0] & 0x0Fu) << 12) | ((uint32_t)(p[1] & 0x3Fu) << 6) |
                (uint32_t)(p[2] & 0x3Fu);
        return 3;
    }
    if ((p[0] & 0xF8u) == 0xF0u && (p[1] & 0xC0u) == 0x80u &&
        (p[2] & 0xC0u) == 0x80u && (p[3] & 0xC0u) == 0x80u) {
        *code = ((uint32_t)(p[0] & 0x07u) << 18) | ((uint32_t)(p[1] & 0x3Fu) << 12) |
                ((uint32_t)(p[2] & 0x3Fu) << 6) | (uint32_t)(p[3] & 0x3Fu);
        return 4;
    }
    *code = 0;
    return 0;
}

#if UI_FONT_CJK_COUNT > 0
typedef struct {
    uint32_t count;
    const uint8_t *codes;    // count * 4 bytes, little-endian, ascending
    const uint8_t *advances; // count bytes
    const uint8_t *glyphs;   // count * UI_GLYPH_BYTES
} cjk_table_t;

static cjk_table_t cjk_table(void)
{
    cjk_table_t table;
    memcpy(&table.count, ui_font_cjk_blob_start, sizeof(table.count));
    table.codes = ui_font_cjk_blob_start + sizeof(table.count);
    table.advances = table.codes + (size_t)table.count * 4u;
    table.glyphs = table.advances + table.count;
    return table;
}

// Binary search, not a scan: the table holds thousands of glyphs, and this runs
// per character of every label on every drawn stripe.
static const uint8_t *cjk_lookup(uint32_t code, int *advance)
{
    cjk_table_t table = cjk_table();
    unsigned low = 0, high = table.count;
    while (low < high) {
        unsigned mid = low + (high - low) / 2u;
        uint32_t value;
        memcpy(&value, table.codes + (size_t)mid * 4u, sizeof(value));
        if (value == code) {
            *advance = table.advances[mid];
            return table.glyphs + (size_t)mid * UI_GLYPH_BYTES;
        }
        if (value < code) {
            low = mid + 1u;
        } else {
            high = mid;
        }
    }
    return NULL;
}
#endif

// Locate a glyph by code point. `*advance` always receives a usable step, so a
// character outside the table advances by a blank cell instead of collapsing
// the line.
static const uint8_t *lookup(uint32_t code, int *advance)
{
    if (code < 0x80u) {
        if (code >= UI_FONT_ASCII_FIRST
                && code < UI_FONT_ASCII_FIRST + UI_FONT_ASCII_COUNT) {
            *advance = ui_font_ascii_adv[code - UI_FONT_ASCII_FIRST];
            return ui_font_ascii[code - UI_FONT_ASCII_FIRST];
        }
        *advance = 8;
        return NULL;
    }
#if UI_FONT_CJK_COUNT > 0
    const uint8_t *glyph = cjk_lookup(code, advance);
    if (glyph) {
        return glyph;
    }
#endif
    // An unlisted character keeps its cell width rather than collapsing the line.
    *advance = UI_GLYPH_W;
    return NULL;
}

// The code point of the next character, with `length` bytes consumed. A malformed
// sequence consumes one byte and keeps that byte as the code point, so the rest
// of the line still lines up.
static unsigned next_code(const char *cursor, uint32_t *code)
{
    unsigned length = utf8_next(cursor, code);
    if (length == 0) {
        *code = (unsigned char)cursor[0];
        length = 1;
    }
    return length;
}

int ui_text_width(const char *text)
{
    if (!text) {
        return 0;
    }
    int width = 0;
    const char *cursor = text;
    while (*cursor) {
        uint32_t code = 0;
        unsigned length = next_code(cursor, &code);
        int advance = 0;
        (void)lookup(code, &advance);
        width += advance;
        cursor += length;
    }
    return width;
}

bool ui_text_truncate(char *out, size_t out_size, const char *text, int limit)
{
    if (!out || out_size == 0) {
        return false;
    }
    out[0] = '\0';
    if (!text) {
        return false;
    }
    size_t used = 0;
    int width = 0;
    const char *cursor = text;
    while (*cursor) {
        uint32_t code = 0;
        unsigned length = next_code(cursor, &code);
        int advance = 0;
        (void)lookup(code, &advance);
        if (width + advance > limit) {
            return false;
        }
        if (used + length >= out_size) {
            return false;
        }
        memcpy(out + used, cursor, length);
        used += length;
        out[used] = '\0';
        width += advance;
        cursor += length;
    }
    return true;
}

void ui_text_fill(ui_surface_t surface, int x, int y, int w, int h, uint16_t color)
{
    if (!surface.pixels || w <= 0 || h <= 0) {
        return;
    }
    int top = y < surface.origin_y ? surface.origin_y : y;
    int bottom = y + h;
    int limit = surface.origin_y + surface.rows;
    if (bottom > limit) {
        bottom = limit;
    }
    int left = x < 0 ? 0 : x;
    int right = x + w;
    if (right > surface.width) {
        right = surface.width;
    }
    if (top >= bottom || left >= right) {
        return;
    }
    uint8_t high = (uint8_t)(color >> 8), low = (uint8_t)(color & 0xFFu);
    for (int row = top; row < bottom; row++) {
        uint8_t *pixel = surface.pixels + ((size_t)(row - surface.origin_y) * surface.width
                                           + (size_t)left) * 2u;
        for (int column = left; column < right; column++) {
            *pixel++ = high;
            *pixel++ = low;
        }
    }
}

// The glyphs of `text` from (x, y), each dot a `scale` x `scale` block, with no
// background. Returns the x just past the text.
static int draw_glyphs(ui_surface_t surface, int x, int y, const char *text, int scale,
                       uint16_t foreground)
{
    int pen = x;
    const char *cursor = text;
    while (*cursor) {
        uint32_t code = 0;
        unsigned length = next_code(cursor, &code);
        int advance = 0;
        const uint8_t *glyph = lookup(code, &advance);
        // Rows of a glyph wholly outside the band are skipped before their bits
        // are looked at: most stripes see only part of a line.
        if (glyph && y + (int)UI_GLYPH_H * scale > surface.origin_y
                && y < surface.origin_y + surface.rows) {
            // tools/make_font.py centres every glyph in its 16-pixel cell but
            // gives a narrow one an advance of its ink width plus two. Drawn
            // from the cell's edge, the ink would land several pixels right of
            // the pen, by an amount that differs per character -- a colon then
            // touches the digit after it, and a 3x number lands 15 px off and
            // past its own background. So a narrow glyph is moved left until
            // its ink starts one pixel after the pen.
            int bearing = 0;
            if (advance < (int)UI_GLYPH_W) {
                uint16_t any = 0;
                for (unsigned gy = 0; gy < UI_GLYPH_H; gy++) {
                    any |= (uint16_t)((glyph[gy * 2u] << 8) | glyph[gy * 2u + 1u]);
                }
                while (any && !(any & (uint16_t)(1u << (UI_GLYPH_W - 1u - (unsigned)bearing)))) {
                    bearing++;
                }
                bearing = bearing > 0 ? bearing - 1 : 0;
            }
            for (unsigned gy = 0; gy < UI_GLYPH_H; gy++) {
                const int row_y = y + (int)gy * scale;
                if (row_y + scale <= surface.origin_y || row_y >= surface.origin_y + surface.rows) {
                    continue;
                }
                uint16_t bits = (uint16_t)((glyph[gy * 2u] << 8) | glyph[gy * 2u + 1u]);
                if (!bits) {
                    continue;  // blank row: the box already painted it
                }
                // Fill the runs of set bits in one call each. Drawing pixel by
                // pixel cost a function call and a bounds check per dot, which
                // made the overlay expensive enough that the idle task stopped
                // being scheduled and the watchdog fired.
                unsigned run_start = 0;
                while (run_start < UI_GLYPH_W) {
                    if (!(bits & (uint16_t)(1u << (UI_GLYPH_W - 1u - run_start)))) {
                        run_start++;
                        continue;
                    }
                    unsigned run_end = run_start;
                    while (run_end < UI_GLYPH_W &&
                           (bits & (uint16_t)(1u << (UI_GLYPH_W - 1u - run_end)))) {
                        run_end++;
                    }
                    ui_text_fill(surface, pen + ((int)run_start - bearing) * scale, row_y,
                                 (int)(run_end - run_start) * scale, scale, foreground);
                    run_start = run_end;
                }
            }
        }
        pen += advance * scale;
        cursor += length;
    }
    return pen;
}

int ui_text_draw(ui_surface_t surface, int x, int y, const char *text,
                 uint16_t foreground, uint16_t background)
{
    if (!text) {
        return x;
    }
    // The background box is painted first so the glyphs sit on a known colour,
    // which is what keeps them readable when the video behind them is bright.
    int width = ui_text_width(text);
    ui_text_fill(surface, x - UI_TEXT_PAD_X, y - UI_TEXT_PAD_Y,
                 width + 2 * UI_TEXT_PAD_X, (int)UI_TEXT_LINE_H + 2 * UI_TEXT_PAD_Y,
                 background);
    return draw_glyphs(surface, x, y, text, 1, foreground);
}

int ui_text_draw_scaled(ui_surface_t surface, int x, int y, const char *text, int scale,
                        uint16_t foreground, uint16_t background)
{
    if (!text) {
        return x;
    }
    if (scale < 1) {
        scale = 1;
    }
    ui_text_fill(surface, x, y, ui_text_width(text) * scale, (int)UI_TEXT_LINE_H * scale,
                 background);
    return draw_glyphs(surface, x, y, text, scale, foreground);
}

bool ui_text_ellipsize(char *out, size_t out_size, const char *text, int limit)
{
    if (!out || !out_size) {
        return false;
    }
    if (!text || ui_text_truncate(out, out_size, text, limit)) {
        out[0] = text ? out[0] : '\0';
        return true;
    }
    static const char ellipsis[] = "\xE2\x80\xA6";   // U+2026
    const int room = limit - ui_text_width(ellipsis);
    if (room < 0 || out_size < sizeof(ellipsis)) {
        if (out && out_size) {
            out[0] = '\0';
        }
        return false;
    }
    (void)ui_text_truncate(out, out_size - (sizeof(ellipsis) - 1u), text, room);
    strcat(out, ellipsis);
    return false;
}

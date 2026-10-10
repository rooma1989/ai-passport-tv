// main/ui_status.c -- see ui_status.h.
#include "ui_status.h"

#include "ui_draw.h"

#include <stdio.h>
#include <string.h>

// HANDOFF.md 4.1, for a 240-wide panel. Icons are 16x16 placed at their
// top-left; text positions are the glyphs' top-left (the box is 6 left, 4 up).
#define DESIGN_W 240
#define LABEL_X 20
#define TEXT_Y 7
#define ICON_Y 7
#define SIGNAL_X 154
#define SIGNAL_X_CHARGING 134
#define BOLT_X 156
#define BATTERY_X 174
#define PERCENT_X 200
// The design's "76%" ends at 226; "100%" is moved left so it does too, rather
// than running into the rounded corner.
#define PERCENT_END 226
// The last column the label's glyphs may reach.
#define LABEL_END 148
#define LABEL_END_CHARGING 128
// The battery fill's columns inside its icon.
#define FILL_FIRST 2

void ui_topbar_set_label(ui_topbar_label_t *label, const char *const texts[],
                         const uint16_t colors[], unsigned count)
{
    if (!label) {
        return;
    }
    if (count > UI_TOPBAR_SEGMENTS) {
        count = UI_TOPBAR_SEGMENTS;
    }
    memset(label, 0, sizeof(*label));
    for (unsigned i = 0; i < count; i++) {
        // Cut by width far beyond any panel, which keeps whole characters
        // while respecting the buffer.
        (void)ui_text_truncate(label->segment[i].text, sizeof(label->segment[i].text),
                               texts && texts[i] ? texts[i] : "", 1 << 20);
        label->segment[i].color = colors ? colors[i] : UI_PAPER;
    }
    label->count = count;
}

static void draw_label(ui_surface_t surface, const ui_topbar_label_t *label, int end)
{
    if (!label) {
        return;
    }
    int pen = LABEL_X;
    for (unsigned i = 0; i < label->count && i < UI_TOPBAR_SEGMENTS; i++) {
        const ui_topbar_segment_t *segment = &label->segment[i];
        char fitted[UI_TOPBAR_SEGMENT_MAX + 4];
        const bool whole = ui_text_ellipsize(fitted, sizeof(fitted), segment->text, end - pen);
        // The box's padding is the bar's own colour, so segments can sit
        // edge to edge without a seam.
        pen = ui_text_draw(surface, pen, TEXT_Y, fitted, segment->color, UI_DEEP);
        if (!whole) {
            break;
        }
    }
}

void ui_status_draw(ui_surface_t surface, int panel_w, const ui_topbar_label_t *label,
                    unsigned bars, int soc, int charging)
{
    if (surface.origin_y >= UI_TOPBAR_H) {
        return;
    }
    if (bars > 4u) bars = 4u;
    const int shift = panel_w > DESIGN_W ? panel_w - DESIGN_W : 0;
    const bool bolt = charging == 1;

    ui_text_fill(surface, 0, 0, panel_w, UI_TOPBAR_H, UI_DEEP);
    draw_label(surface, label, (bolt ? LABEL_END_CHARGING : LABEL_END) + shift);

    const int signal_x = (bolt ? SIGNAL_X_CHARGING : SIGNAL_X) + shift;
    for (unsigned i = 0; i < 4u; i++) {
        ui_draw_icon(surface, (ui_icon_t)(UI_ICON_SIGNAL_BAR1 + i), signal_x, ICON_Y, 1,
                     i < bars ? UI_PAPER : UI_RULE);
    }
    if (bolt) {
        ui_draw_icon(surface, UI_ICON_BOLT, BOLT_X + shift, ICON_Y, 1, UI_AMBER);
    }

    ui_draw_icon(surface, UI_ICON_BATTERY_FRAME, BATTERY_X + shift, ICON_Y, 1, UI_FAINT);
    char text[8];
    uint16_t text_color = UI_MUTED;
    if (soc >= 0) {
        const int level = soc > 100 ? 100 : soc;
        const uint16_t color = level < UI_STATUS_LOW_SOC ? UI_VERM : UI_PAPER;
        ui_draw_icon_columns(surface, UI_ICON_BATTERY_FILL, BATTERY_X + shift, ICON_Y, 1, color,
                             FILL_FIRST, (level + 5) / 10);
        snprintf(text, sizeof(text), "%d%%", level);
        text_color = color;
    } else {
        snprintf(text, sizeof(text), "--");
    }
    int text_x = PERCENT_END - ui_text_width(text);
    if (text_x > PERCENT_X) text_x = PERCENT_X;
    ui_text_draw(surface, text_x + shift, TEXT_Y, text, text_color, UI_DEEP);
}

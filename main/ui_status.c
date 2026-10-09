// main/ui_status.c -- see ui_status.h.
#include "ui_status.h"

#include <stdio.h>

// Battery body, outline included, and the fill area inside it (1 px outline,
// 1 px gap).
#define BODY_W 22
#define BODY_H 12
#define FILL_W (BODY_W - 4)
#define FILL_H (BODY_H - 4)
#define NUB_W 2
#define NUB_H 6
#define BAR_W 3
#define BAR_GAP 2
#define BARS 4u
#define SPACING 6

void ui_status_draw(ui_surface_t surface, int panel_w, int band_h,
                    unsigned bars, int soc, int charging)
{
    if (band_h < BODY_H + 2 || surface.origin_y >= band_h) {
        return;
    }
    if (bars > BARS) bars = BARS;
    const int cy = band_h / 2;

    // Laid out right to left from the edge: text, nub, body, bars.
    char text[8];
    if (soc >= 0) snprintf(text, sizeof(text), "%d%%", soc > 100 ? 100 : soc);
    else snprintf(text, sizeof(text), "--");
    const int text_x = panel_w - UI_STATUS_EDGE - ui_text_width(text);
    const int text_y = (band_h - (int)UI_TEXT_LINE_H) / 2;
    // ui_text_draw's box reaches UI_TEXT_PAD_X left of the text.
    const int nub_x = text_x - UI_TEXT_PAD_X - NUB_W;
    const int body_x = nub_x - BODY_W;
    const int body_y = cy - BODY_H / 2;
    const int bars_x = body_x - SPACING - ((int)BARS * BAR_W + ((int)BARS - 1) * BAR_GAP);
    const int bars_bottom = body_y + BODY_H - 1;

    ui_text_draw(surface, text_x, text_y, text, UI_COLOR_TEXT, UI_COLOR_BOX);

    // Outline and nub.
    ui_text_fill(surface, body_x, body_y, BODY_W, 1, UI_COLOR_DIM);
    ui_text_fill(surface, body_x, body_y + BODY_H - 1, BODY_W, 1, UI_COLOR_DIM);
    ui_text_fill(surface, body_x, body_y, 1, BODY_H, UI_COLOR_DIM);
    ui_text_fill(surface, body_x + BODY_W - 1, body_y, 1, BODY_H, UI_COLOR_DIM);
    ui_text_fill(surface, nub_x, cy - NUB_H / 2, NUB_W, NUB_H, UI_COLOR_DIM);

    if (soc >= 0) {
        int level = soc > 100 ? 100 : soc;
        int filled = FILL_W * level / 100;
        uint16_t colour = level < UI_STATUS_LOW_SOC ? UI_COLOR_LOW : UI_COLOR_GOOD;
        ui_text_fill(surface, body_x + 2, body_y + 2, filled, FILL_H, colour);
    }

    if (charging == 1) {
        // A three-stroke bolt across the body, drawn over the fill.
        const int cx = body_x + BODY_W / 2;
        ui_text_fill(surface, cx, body_y + 1, 2, 5, UI_COLOR_BOLT);
        ui_text_fill(surface, cx - 2, body_y + 5, 6, 2, UI_COLOR_BOLT);
        ui_text_fill(surface, cx - 2, body_y + 6, 2, 5, UI_COLOR_BOLT);
    }

    // Bars rise left to right, bottoms level with the battery's lower edge.
    static const int heights[BARS] = { 4, 7, 10, 13 };
    for (unsigned i = 0; i < BARS; i++) {
        const int h = heights[i];
        ui_text_fill(surface, bars_x + (int)i * (BAR_W + BAR_GAP), bars_bottom - h + 1, BAR_W, h,
                     i < bars ? UI_COLOR_TEXT : UI_COLOR_RULE);
    }
}

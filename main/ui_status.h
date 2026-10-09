// The always-on status group in the top band while playing: four signal bars, a
// battery outline filled to the charge, and the percentage, right-aligned.
//
// Drawn from rectangles and text with ui_text_fill/ui_text_draw only, so
// tests/test_ui_status.c checks its pixels without a device.
#pragma once

#include "ui_text.h"

// Distance from the right edge: the panel's corners are rounded and cut into it.
#define UI_STATUS_EDGE 16

#define UI_COLOR_GOOD 0x07E0u   // battery fill
#define UI_COLOR_LOW  0xF800u   // battery fill below UI_STATUS_LOW_SOC
#define UI_COLOR_BOLT 0xFFE0u   // charging mark
#define UI_STATUS_LOW_SOC 20

// Paint the group into rows [0, band_h) of the panel, as far as `surface` covers
// them. `bars` is 0..4 (0 = not connected, all dim). `soc` is 0..100, or < 0 when
// unknown ("--", no fill). `charging` is 1 to draw the bolt; 0 or -1 draws none.
void ui_status_draw(ui_surface_t surface, int panel_w, int band_h,
                    unsigned bars, int soc, int charging);

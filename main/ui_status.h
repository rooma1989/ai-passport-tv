// The top bar, rows [0, UI_TOPBAR_H): a label at the left -- channel number and
// name while playing -- and the status group at the right: four signal bars,
// the battery and its percentage.
//
// Drawn from icons, rectangles and text only, so tests/test_ui_status.c checks
// its pixels without a device. Positions are the design's (HANDOFF.md 4.1) for
// a 240-wide panel; a wider panel moves the status group with its right edge.
#pragma once

#include "ui_text.h"

#define UI_TOPBAR_H 30
#define UI_STATUS_LOW_SOC 20
// Below this the percentage blinks once a second (`blink_off` hides it).
#define UI_STATUS_CRITICAL_SOC 10

// The label is a few runs of text, each in its own colour, drawn one after
// another -- "07" in amber, then " CCTV-7 国防军事" in paper.
#define UI_TOPBAR_SEGMENTS 3u
#define UI_TOPBAR_SEGMENT_MAX 72

typedef struct {
    char text[UI_TOPBAR_SEGMENT_MAX];
    uint16_t color;
} ui_topbar_segment_t;

typedef struct {
    ui_topbar_segment_t segment[UI_TOPBAR_SEGMENTS];
    unsigned count;
} ui_topbar_label_t;

// Replace the label. Segments past UI_TOPBAR_SEGMENTS are dropped and each text
// is cut, on a character boundary, to fit its buffer; NULL text is empty.
void ui_topbar_set_label(ui_topbar_label_t *label, const char *const texts[],
                         const uint16_t colors[], unsigned count);

// Paint the whole bar into the rows of it `surface` covers: UI_DEEP, the label,
// then the status group. `label` may be NULL. `bars` is 0..4 (0 = not
// connected, all dim). `soc` is 0..100, or < 0 when unknown ("--", no fill).
// `charging` is 1 to draw the bolt; 0 or -1 draws none. The label stops short
// of the status group and ends in "…" when it does not fit. `blink_off` hides
// the percentage while the charge is below UI_STATUS_CRITICAL_SOC; the caller
// flips it each second.
void ui_status_draw(ui_surface_t surface, int panel_w, const ui_topbar_label_t *label,
                    unsigned bars, int soc, int charging, bool blink_off);

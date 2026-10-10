// The design's pixel grammar and sprites, drawn into a stripe like ui_text:
// one-bit icons, the 4-bit pets, dotted rules, notched rectangles, the
// ten-cell level bar and the opaque panel behind menus and pages.
//
// The sprites themselves live in xiaozhi_tv_ui_assets.h, exported from the
// design; only ui_draw.c includes it, so they are in the image once.
// No ESP-IDF dependencies: tests/test_ui_draw.c checks the pixels on the host.
#pragma once

#include "ui_text.h"

typedef enum {
    UI_ICON_WIFI = 0,
    UI_ICON_SIGNAL_BAR1,
    UI_ICON_SIGNAL_BAR2,
    UI_ICON_SIGNAL_BAR3,
    UI_ICON_SIGNAL_BAR4,
    UI_ICON_BATTERY_FRAME,
    UI_ICON_BATTERY_FILL,
    UI_ICON_BOLT,
    UI_ICON_PAUSE,
    UI_ICON_PLAY,
    UI_ICON_VOLUME,
    UI_ICON_SUN,
    UI_ICON_FF,
    UI_ICON_NEXT,
    UI_ICON_CROSS,
    UI_ICON_CHECK,
    UI_ICON_CAST,
    UI_ICON_PERSON,
    UI_ICON_TV,
    UI_ICON_GLOBE,
    UI_ICON_NOTE,
    UI_ICON_FILM,
    UI_ICON_MORE,
    UI_ICON_LINK,
    UI_ICON_PHONE,
    UI_ICON_REWIND,
    UI_ICON_COUNT
} ui_icon_t;

#define UI_ICON_SIZE 16
#define UI_PET_SIZE 24

// The ten-cell bar: cell k is UI_CELLS_W wide at x = UI_CELLS_X + k * UI_CELLS_STEP.
#define UI_CELLS 10u
#define UI_CELLS_X 16
#define UI_CELLS_W 19
#define UI_CELLS_STEP 21

// A 16x16 icon, set dots only, each a `scale` x `scale` block in `color`.
void ui_draw_icon(ui_surface_t surface, ui_icon_t icon, int x, int y, int scale, uint16_t color);

// The same, limited to icon columns [first, first + count): the battery fill
// lights only as many of its columns as the charge calls for.
void ui_draw_icon_columns(ui_surface_t surface, ui_icon_t icon, int x, int y, int scale,
                          uint16_t color, int first, int count);

// The pets, 0..ui_pet_count()-1, in the design's order (0 = 电电).
unsigned ui_pet_count(void);
const char *ui_pet_name(unsigned pet);   // UTF-8, e.g. "电电"; NULL out of range
unsigned ui_pet_moods(unsigned pet);
const char *ui_pet_kind(unsigned pet);   // what it is, e.g. "小电视"; NULL out of range

// One pet at `scale`, transparent dots left alone, so the background must
// already be drawn. A mood past the pet's last falls back to its last (only
// 电电 has mood 3, "sad"); a pet out of range draws nothing.
void ui_draw_pet(ui_surface_t surface, unsigned pet, unsigned mood, int x, int y, int scale);

// A one-pixel rule, two on and two off from x0, through x1 inclusive.
void ui_draw_dotted_hline(ui_surface_t surface, int x0, int x1, int y, uint16_t color);

// A filled rectangle with its corners stepped off: the first and last rows
// are 2 px shorter at each end, the second and second-to-last 1 px.
void ui_draw_notched_rect(ui_surface_t surface, int x, int y, int w, int h, uint16_t color);

// The ten-cell level bar at row y, `h` high: the first `lit` cells (clamped to
// ten) in `lit_color`, the rest in `dim_color`.
void ui_draw_cells(ui_surface_t surface, int y, int h, unsigned lit,
                   uint16_t lit_color, uint16_t dim_color);

// Rows [top, bottom) in UI_INK across the whole width: the opaque panel every
// menu and page is drawn on, so nothing of the picture shows between rows.
void ui_draw_panel(ui_surface_t surface, int top, int bottom);

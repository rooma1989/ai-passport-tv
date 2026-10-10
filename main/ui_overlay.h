// What is drawn over the picture while it plays (HANDOFF.md 4.4-4.6): the
// level box, the channel banner, the seek box and the pause band. Each draws
// only into the rows of `surface` it covers and only when the menu state says
// it is up, so the player calls them for every stripe.
//
// No ESP-IDF dependencies: tools/render_ui.c draws them on the host to check
// the layout against the design.
#pragma once

#include "ui_menu.h"
#include "ui_text.h"

// The volume (or, from the phone, the backlight at `brightness_percent`) across
// y=30..74.
void ui_overlay_level(ui_surface_t surface, const ui_menu_t *menu, unsigned brightness_percent);

// The channel banner across y=160..209: number at 2x, name, "group · n/total".
void ui_overlay_banner(ui_surface_t surface, const ui_menu_t *menu);

// The seek box at (60,92), 120x56.
void ui_overlay_hint(ui_surface_t surface, const ui_menu_t *menu);

// The pause band over rows [top, bottom): `pet` dozing, the pause mark, 已暂停
// and the video's title.
void ui_overlay_pause(ui_surface_t surface, int top, int bottom, unsigned pet, const char *title);

// The bottom bar while paused, rows [top, bottom): how to carry on.
void ui_overlay_pause_hint(ui_surface_t surface, int top, int bottom);

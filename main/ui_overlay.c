// main/ui_overlay.c -- see ui_overlay.h. Coordinates are the design's, for the
// 240-wide panel; text positions are where the glyphs start (the padded box is
// 6 left and 4 up of that).
#include "ui_overlay.h"

#include "ui_draw.h"

#include <stdio.h>

// Whether rows [y0, y1) reach the stripe.
static bool covers(ui_surface_t surface, int y0, int y1)
{
    return y1 > surface.origin_y && y0 < surface.origin_y + surface.rows;
}

void ui_overlay_level(ui_surface_t surface, const ui_menu_t *menu, unsigned brightness_percent)
{
    if (!menu || !ui_menu_volume_visible(menu) || !covers(surface, 30, 75)) {
        return;
    }
    const bool bright = menu->level == UI_LEVEL_BRIGHTNESS;
    unsigned percent = bright ? brightness_percent : menu->volume;
    if (percent > 100u) percent = 100u;
    // Muted is the volume at 0; a backlight is never off.
    const bool mute = !bright && percent == 0;
    const uint16_t accent = mute ? UI_MUTED : UI_AMBER;
    ui_text_fill(surface, 0, 30, surface.width, 44, UI_INK);
    ui_text_fill(surface, 0, 74, surface.width, 1, UI_RULE);
    ui_draw_icon(surface, bright ? UI_ICON_SUN : UI_ICON_VOLUME, 16, 38, 1, accent);
    const int pen = ui_text_draw(surface, 40, 38, bright ? "亮度" : (mute ? "静音" : "音量"),
                                 mute ? UI_MUTED : UI_PAPER, UI_INK);
    if (bright) {
        // Cells only: a padded box would reach back over the end of 亮度.
        ui_text_draw_scaled(surface, pen, 38, " \xC2\xB7 手机", 1, UI_MUTED, UI_INK);
    }
    char value[8];
    snprintf(value, sizeof(value), "%u%%", percent);
    // Right-aligned: the last glyph ends at x=224.
    ui_text_draw(surface, 226 - ui_text_width(value), 38, value, accent, UI_INK);
    ui_draw_cells(surface, 62, 6, mute ? 0u : (percent + 5u) / 10u, UI_AMBER, UI_RULE);
}

void ui_overlay_banner(ui_surface_t surface, const ui_menu_t *menu)
{
    if (!menu || menu->banner_ms <= 0 || !menu->banner[0] || !covers(surface, 160, 210)) {
        return;
    }
    ui_text_fill(surface, 0, 160, surface.width, 50, UI_INK);
    ui_text_fill(surface, 0, 160, surface.width, 1, UI_RULE);
    if (menu->banner_number) {
        char number[8];
        snprintf(number, sizeof(number), "%02u", menu->banner_number % 100u);
        ui_text_draw_scaled(surface, 20, 169, number, 2, UI_AMBER, UI_INK);
    }
    char fitted[UI_MENU_NAME_MAX + 4];
    (void)ui_text_ellipsize(fitted, sizeof(fitted), menu->banner, 226 - 60);
    ui_text_draw(surface, 60, 166, fitted, UI_PAPER, UI_INK);
    if (menu->banner_detail[0]) {
        ui_text_draw(surface, 60, 189, menu->banner_detail, UI_MUTED, UI_INK);
    }
}

void ui_overlay_hint(ui_surface_t surface, const ui_menu_t *menu)
{
    if (!menu || menu->hint == UI_HINT_NONE || !covers(surface, 92, 148)) {
        return;
    }
    char text[24];
    ui_menu_hint_text(menu, text, sizeof(text));
    ui_draw_notched_rect(surface, 60, 92, 120, 56, UI_RULE);
    ui_draw_notched_rect(surface, 61, 93, 118, 54, UI_INK);
    const ui_icon_t icon = menu->hint == UI_HINT_NEXT ? UI_ICON_NEXT
                         : menu->hint == UI_HINT_BACK ? UI_ICON_REWIND : UI_ICON_FF;
    ui_draw_icon(surface, icon, 70, 104, 2, UI_AMBER);
    // Without the usual padded box: "快进30秒" reaches x=176, and the box's
    // padding would paint over the frame at x=179.
    ui_text_draw_scaled(surface, 112, 100, text, 1, UI_PAPER, UI_INK);
    ui_text_draw_scaled(surface, 112, 124, "缓冲中", 1, UI_MUTED, UI_INK);
}

void ui_overlay_pause(ui_surface_t surface, int top, int bottom, unsigned pet, const char *title)
{
    if (!covers(surface, top, bottom)) {
        return;
    }
    // The rows come out at y=84..155 on this panel, as the design has them.
    char fitted[96];
    (void)ui_text_ellipsize(fitted, sizeof(fitted), title ? title : "", 226 - 80);
    ui_text_fill(surface, 0, top, surface.width, bottom - top, UI_INK);
    ui_text_fill(surface, 0, top, surface.width, 1, UI_RULE);
    ui_text_fill(surface, 0, bottom - 1, surface.width, 1, UI_RULE);
    ui_draw_pet(surface, pet, 2, 20, top + 10, 2);
    ui_draw_icon(surface, UI_ICON_PAUSE, 80, top + 16, 1, UI_AMBER);
    ui_text_draw(surface, 100, top + 16, "已暂停", UI_PAPER, UI_INK);
    ui_text_draw(surface, 80, top + 42, fitted, UI_MUTED, UI_INK);
}

void ui_overlay_pause_hint(ui_surface_t surface, int top, int bottom)
{
    if (!covers(surface, top, bottom)) {
        return;
    }
    static const char how[] = "手机点「继续」播放";
    ui_text_fill(surface, 0, top, surface.width, bottom - top, UI_DEEP);
    ui_text_draw(surface, (surface.width - ui_text_width(how)) / 2, top + 7, how, UI_MUTED, UI_DEEP);
}

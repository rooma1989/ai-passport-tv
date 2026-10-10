// main/ui_overlay.c -- see ui_overlay.h. Coordinates are the design's, for the
// 240-wide panel; text positions are where the glyphs start (the padded box is
// 6 left and 4 up of that).
#include "ui_overlay.h"

#include "qrcodegen.h"
#include "ui_draw.h"
#include "ui_qr.h"

#include <stdio.h>
#include <string.h>

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

// ---- Menus and pages ----

#define LIST_TOP 36
#define LIST_ROW_H 24
#define SCROLL_X 232

void ui_overlay_key_hint(ui_surface_t surface, int y, const char *text)
{
    if (!text || !covers(surface, y, y + (int)UI_TEXT_LINE_H)) {
        return;
    }
    int pen = (surface.width - ui_text_width(text)) / 2;
    // Runs split at the key names; drawn as glyph cells only, so one run's
    // background never reaches over the last.
    static const char *const keys[] = { "\xE2\x96\xB2", "\xE2\x96\xBC", "OK" };
    const char *cursor = text;
    char run[96];
    while (*cursor) {
        size_t key_len = 0;
        for (unsigned k = 0; k < 3u && !key_len; k++) {
            const size_t n = strlen(keys[k]);
            if (strncmp(cursor, keys[k], n) == 0) key_len = n;
        }
        size_t len = key_len;
        if (!len) {
            // Up to the next key name.
            while (cursor[len]) {
                bool at_key = false;
                for (unsigned k = 0; k < 3u && !at_key; k++) {
                    at_key = strncmp(cursor + len, keys[k], strlen(keys[k])) == 0;
                }
                if (at_key) break;
                len++;
            }
        }
        if (len >= sizeof(run)) len = sizeof(run) - 1u;
        memcpy(run, cursor, len);
        run[len] = '\0';
        pen = ui_text_draw_scaled(surface, pen, y, run, 1, key_len ? UI_AMBER : UI_MUTED, UI_INK);
        cursor += len;
    }
}

static ui_icon_t group_icon(const char *name)
{
    static const struct { const char *name; ui_icon_t icon; } icons[] = {
        { "我的投屏", UI_ICON_CAST }, { "UP 主", UI_ICON_PERSON }, { "央视", UI_ICON_TV },
        { "卫视", UI_ICON_TV }, { "港澳台", UI_ICON_TV }, { "国际", UI_ICON_GLOBE },
        { "音乐", UI_ICON_NOTE }, { "动画影视", UI_ICON_FILM },
    };
    for (unsigned i = 0; i < sizeof(icons) / sizeof(icons[0]); i++) {
        if (strcmp(name, icons[i].name) == 0) return icons[i].icon;
    }
    return UI_ICON_MORE;
}

void ui_overlay_list(ui_surface_t surface, const ui_menu_t *menu)
{
    if (!menu || !covers(surface, LIST_TOP, LIST_TOP + (int)UI_OVERLAY_LIST_ROWS * LIST_ROW_H)) {
        return;
    }
    const unsigned total = ui_menu_row_count(menu);
    const int playing_group = ui_menu_playing_group(menu);
    const int playing_entry = ui_menu_playing_entry(menu);
    for (unsigned k = 0; k < UI_OVERLAY_LIST_ROWS; k++) {
        const unsigned row = menu->scroll + k;
        if (row >= total) break;
        const int top = LIST_TOP + (int)k * LIST_ROW_H;
        if (!covers(surface, top, top + LIST_ROW_H)) continue;
        const bool chosen = row == menu->selected;
        if (chosen) ui_draw_notched_rect(surface, 6, top, 222, LIST_ROW_H, UI_VERM);
        const uint16_t back = chosen ? UI_VERM : UI_INK;
        const int text_y = top + 4;
        const int g = ui_menu_row_group(menu, row);
        if (g >= 0) {
            // A group: icon, name, how many channels.
            const bool here = g == playing_group;
            const uint16_t name_color = chosen ? UI_DEEP : here ? UI_AMBER : UI_PAPER;
            ui_draw_icon(surface, group_icon(menu->groups[g]), 14, text_y, 1, name_color);
            ui_text_draw_scaled(surface, 38, text_y, menu->groups[g], 1, name_color, back);
            char count[8];
            snprintf(count, sizeof(count), "%u", ui_menu_group_size(menu, (unsigned)g));
            ui_text_draw_scaled(surface, 222 - ui_text_width(count), text_y, count, 1,
                                chosen ? UI_DEEP : UI_MUTED, back);
            continue;
        }
        const int e = ui_menu_row_entry(menu, row);
        if (e < 0) continue;
        // A channel: its number in the whole list, its name, a mark if playing.
        const bool here = e == playing_entry;
        char number[8];
        snprintf(number, sizeof(number), "%02d", (e + 1) % 100);
        ui_text_draw_scaled(surface, 16, text_y, number, 1, chosen ? UI_DEEP : UI_MUTED, back);
        const char *name = menu->entries[e].name[0] ? menu->entries[e].name : menu->entries[e].id;
        char fitted[UI_MENU_NAME_MAX + 4];
        (void)ui_text_ellipsize(fitted, sizeof(fitted), name, 206 - 42 + 2);
        ui_text_draw_scaled(surface, 42, text_y, fitted, 1,
                            chosen ? UI_DEEP : here ? UI_AMBER : UI_PAPER, back);
        if (here) ui_draw_icon(surface, UI_ICON_PLAY, 208, text_y, 1, chosen ? UI_DEEP : UI_AMBER);
    }
    // The scroll bar: the track, and a thumb as long as the share on screen.
    const int track = (int)UI_OVERLAY_LIST_ROWS * LIST_ROW_H;
    ui_text_fill(surface, SCROLL_X, LIST_TOP, 2, track, UI_RULE);
    if (total > 0) {
        const unsigned shown = total < UI_OVERLAY_LIST_ROWS ? total : UI_OVERLAY_LIST_ROWS;
        int length = (int)((unsigned)track * shown / total);
        if (length < 8) length = 8;
        int offset = (int)((unsigned)track * menu->scroll / total);
        if (offset + length > track) offset = track - length;
        ui_text_fill(surface, SCROLL_X, LIST_TOP + offset, 2, length, UI_FAINT);
    }
}

void ui_overlay_brightness(ui_surface_t surface, unsigned percent)
{
    if (percent > 100u) percent = 100u;
    ui_draw_icon(surface, UI_ICON_SUN, 104, 40, 2, UI_AMBER);
    char value[8];
    snprintf(value, sizeof(value), "%u%%", percent);
    ui_text_draw_scaled(surface, (surface.width - 3 * ui_text_width(value)) / 2, 78, value, 3,
                        UI_AMBER, UI_INK);
    ui_draw_cells(surface, 140, 8, (percent + 5u) / 10u, UI_AMBER, UI_RULE);
    ui_draw_dotted_hline(surface, 16, 223, 166, UI_RULE);
    ui_overlay_key_hint(surface, 180, "\xE2\x96\xB2亮  \xE2\x96\xBC暗  OK完成");
}

void ui_overlay_status(ui_surface_t surface, const ui_status_page_t *page)
{
    if (!page) {
        return;
    }
    // The channel, then the readings and the code side by side.
    int pen = 20;
    if (page->number[0]) {
        pen = ui_text_draw_scaled(surface, pen, 40, page->number, 1, UI_AMBER, UI_INK);
        pen += 6;
    }
    char fitted[96];
    (void)ui_text_ellipsize(fitted, sizeof(fitted), page->name, 226 - pen + 2);
    ui_text_draw_scaled(surface, pen, 40, fitted, 1, UI_PAPER, UI_INK);
    ui_draw_dotted_hline(surface, 16, 223, 63, UI_RULE);

    char value[8];
    ui_text_draw_scaled(surface, 20, 72, "信号", 1, UI_MUTED, UI_INK);
    if (page->bars) snprintf(value, sizeof(value), "%u/4", page->bars > 4u ? 4u : page->bars);
    else snprintf(value, sizeof(value), "--");
    ui_text_draw_scaled(surface, 60, 72, value, 1, UI_PAPER, UI_INK);
    ui_text_draw_scaled(surface, 20, 96, "电量", 1, UI_MUTED, UI_INK);
    if (page->soc >= 0) snprintf(value, sizeof(value), "%d%%", page->soc > 100 ? 100 : page->soc);
    else snprintf(value, sizeof(value), "--");
    ui_text_draw_scaled(surface, 60, 96, value, 1, UI_PAPER, UI_INK);

    ui_draw_pet(surface, page->pet, 1, 18, 120, 2);
    if (page->qr) {
        ui_text_draw_scaled(surface, 76, 138, "扫码\xE2\x86\x92", 1, UI_AMBER, UI_INK);
        // As large as fits the design's 87 px square, centred in it.
        const int modules = qrcodegen_getSize(page->qr) + 2 * UI_QR_QUIET;
        int scale = 87 / modules;
        if (scale < UI_QR_MIN_SCALE) scale = UI_QR_MIN_SCALE;
        const int side = modules * scale;
        ui_qr_draw_colors(surface, 139 + (87 - side) / 2, 68 + (87 - side) / 2, scale, page->qr,
                          UI_PAPER, UI_DEEP);
        if (page->qr_caption) {
            ui_text_draw_scaled(surface, 139 + (88 - ui_text_width(page->qr_caption)) / 2, 162,
                                page->qr_caption, 1, UI_MUTED, UI_INK);
        }
    }
    ui_draw_dotted_hline(surface, 16, 223, 184, UI_RULE);
    ui_overlay_key_hint(surface, 190, "长按\xE2\x96\xB2配网  长按\xE2\x96\xBC宠物");
}

void ui_overlay_bottom_rates(ui_surface_t surface, int top, unsigned fps_x10, unsigned kbytes_per_s)
{
    char text[24];
    snprintf(text, sizeof(text), "帧率 %u.%u", fps_x10 / 10u, fps_x10 % 10u);
    ui_text_draw(surface, 20, top + 7, text, UI_MUTED, UI_DEEP);
    snprintf(text, sizeof(text), "网速 %uKB/s", kbytes_per_s);
    ui_text_draw(surface, 226 - ui_text_width(text), top + 7, text, UI_MUTED, UI_DEEP);
}

void ui_overlay_bottom_text(ui_surface_t surface, int top, const char *text, uint16_t color)
{
    if (!text) {
        return;
    }
    ui_text_draw(surface, (surface.width - ui_text_width(text)) / 2, top + 7, text, color, UI_DEEP);
}

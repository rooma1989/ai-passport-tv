// main/ui_overlay.c -- see ui_overlay.h. Coordinates are the design's, for the
// 240-wide panel; text positions are where the glyphs start (the padded box is
// 6 left and 4 up of that).
#include "ui_overlay.h"

#ifdef ESP_PLATFORM
#include "sdkconfig.h"   /* CONFIG_AV_MUSE_DUAL, the board */
#endif

// The key that opens the status page, as the hints name it: a double-click of
// OK on the AI Passport, of ▲ on the Xingzhi board (bsp's BSP_DOUBLE_OK_IS_STATUS).
// On the host, render_ui.sh --passport defines UI_RENDER_PASSPORT.
#if defined(ESP_PLATFORM) ? !defined(CONFIG_AV_BOARD_XINGZHI_CUBE_154) : defined(UI_RENDER_PASSPORT)
#define UI_STATUS_KEY "OK"
#else
#define UI_STATUS_KEY "\xE2\x96\xB2"
#endif

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
    const bool bright = menu->level == UI_LEVEL_BRIGHTNESS || menu->level == UI_LEVEL_BRIGHTNESS_KEYS;
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
    if (menu->level == UI_LEVEL_BRIGHTNESS) {
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
    if (ui_episode_known(&menu->playing)) {
        // A relay channel: which episode and what it is, instead of where the
        // channel sits in the list.
        char episode[24];
        ui_episode_text(&menu->playing, episode, sizeof(episode));
        int pen = 60;
        if (episode[0]) {
            pen = ui_text_draw_scaled(surface, pen, 189, episode, 1, UI_AMBER, UI_INK);
        }
        if (menu->playing.title[0]) {
            // Ellipsized into the name's buffer: the room left is narrower
            // than any title that would not fit it (this runs on the video
            // task's stack, so no second copy of the title).
            if (episode[0]) pen = ui_text_draw_scaled(surface, pen, 189, " \xC2\xB7 ", 1, UI_MUTED, UI_INK);
            (void)ui_text_ellipsize(fitted, sizeof(fitted), menu->playing.title, 226 - pen);
            ui_text_draw_scaled(surface, pen, 189, fitted, 1, UI_MUTED, UI_INK);
        }
    } else if (menu->banner_detail[0]) {
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

void ui_overlay_loading(ui_surface_t surface, const ui_menu_t *menu)
{
    // The pause band's rows (84..155), so the two read as one family.
    enum { TOP = 84, BOTTOM = 156 };
    if (!ui_menu_loading_band(menu) || !covers(surface, TOP, BOTTOM)) {
        return;
    }
    const ui_episode_t *loading = &menu->loading;
    ui_text_fill(surface, 0, TOP, surface.width, BOTTOM - TOP, UI_INK);
    ui_text_fill(surface, 0, TOP, surface.width, 1, UI_RULE);
    ui_text_fill(surface, 0, BOTTOM - 1, surface.width, 1, UI_RULE);
    ui_draw_icon(surface, UI_ICON_NEXT, 20, TOP + 20, 2, UI_AMBER);
    int pen = ui_text_draw_scaled(surface, 68, TOP + 14, "正在加载", 1, UI_PAPER, UI_INK);
    char episode[24];
    ui_episode_text(loading, episode, sizeof(episode));
    if (episode[0]) {
        pen = ui_text_draw_scaled(surface, pen, TOP + 14, " ", 1, UI_PAPER, UI_INK);
        ui_text_draw_scaled(surface, pen, TOP + 14, episode, 1, UI_AMBER, UI_INK);
    }
    if (loading->title[0]) {
        char fitted[UI_EPISODE_TITLE_MAX + 4];
        (void)ui_text_ellipsize(fitted, sizeof(fitted), loading->title, 226 - 68);
        ui_text_draw_scaled(surface, 68, TOP + 40, fitted, 1, UI_MUTED, UI_INK);
    }
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

void ui_overlay_progress_line(ui_surface_t panel, int top, uint32_t pos_ms, uint32_t dur_ms, bool times)
{
    if (dur_ms == 0 || !covers(panel, top, top + 30)) {
        return;
    }
    // Two rows: thin enough to leave the picture alone, thick enough to see.
    const int lit = (int)ui_progress_fill(pos_ms, dur_ms, (unsigned)panel.width);
    ui_text_fill(panel, 0, top, lit, 2, UI_AMBER);
    ui_text_fill(panel, lit, top, panel.width - lit, 2, UI_RULE);
    if (!times) {
        return;
    }
    char played[16], total[16];
    ui_progress_times(pos_ms, dur_ms, played, total, sizeof(played));
    const ui_surface_t view = ui_surface_centered(panel);
    const char *slash = " / ";
    const int width = ui_text_width(played) + ui_text_width(slash) + ui_text_width(total);
    int pen = (view.width - width) / 2;
    pen = ui_text_draw_scaled(view, pen, top + 8, played, 1, UI_PAPER, UI_DEEP);
    pen = ui_text_draw_scaled(view, pen, top + 8, slash, 1, UI_MUTED, UI_DEEP);
    ui_text_draw_scaled(view, pen, top + 8, total, 1, UI_MUTED, UI_DEEP);
}

void ui_overlay_progress_bar(ui_surface_t surface, int top, uint32_t pos_ms, uint32_t dur_ms)
{
    if (dur_ms == 0 || !covers(surface, top, top + 30)) {
        return;
    }
    char played[16], total[16];
    ui_progress_times(pos_ms, dur_ms, played, total, sizeof(played));
    // The times on the row the other bottom-bar lines use, the bar on their
    // middle (the font's x-height), 4 px tall.
    const int left = ui_text_draw_scaled(surface, 16, top + 7, played, 1, UI_PAPER, UI_DEEP) + 8;
    const int right_text = 224 - ui_text_width(total);
    ui_text_draw_scaled(surface, right_text, top + 7, total, 1, UI_MUTED, UI_DEEP);
    const int right = right_text - 8;
    if (right <= left) {
        return;
    }
    const int lit = (int)ui_progress_fill(pos_ms, dur_ms, (unsigned)(right - left));
    ui_text_fill(surface, left, top + 14, lit, 4, UI_AMBER);
    ui_text_fill(surface, left + lit, top + 14, right - left - lit, 4, UI_RULE);
}

// ---- Menus and pages ----

#define LIST_TOP 36
#define LIST_ROW_H 24
#define SCROLL_X 232

void ui_overlay_key_hint(ui_surface_t surface, int y, const char *text)
{
    if (text) {
        ui_overlay_key_line(surface, (surface.width - ui_text_width(text)) / 2, y, text, UI_INK);
    }
}

int ui_overlay_key_line(ui_surface_t surface, int x, int y, const char *text, uint16_t background)
{
    if (!text || !covers(surface, y, y + (int)UI_TEXT_LINE_H)) {
        return x;
    }
    int pen = x;
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
        pen = ui_text_draw_scaled(surface, pen, y, run, 1, key_len ? UI_AMBER : UI_MUTED, background);
        cursor += len;
    }
    return pen;
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
    ui_overlay_key_hint(surface, 176, "\xE2\x96\xB2亮  \xE2\x96\xBC暗  OK完成");
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
    // The strength in dBm says more than the bars when the signal is weak.
    if (page->bars && page->rssi) snprintf(value, sizeof(value), "%ddBm", page->rssi);
    else if (page->bars) snprintf(value, sizeof(value), "%u/4", page->bars > 4u ? 4u : page->bars);
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
            ui_text_draw_scaled(surface, 139 + (88 - ui_text_width(page->qr_caption)) / 2, 158,
                                page->qr_caption, 1, UI_MUTED, UI_INK);
        }
    }
    ui_draw_dotted_hline(surface, 16, 223, 184, UI_RULE);
#if CONFIG_AV_MUSE_DUAL
    ui_overlay_key_hint(surface, 186, "长按\xE2\x96\xB2配网 \xE2\x96\xBC宠物 双击OK回Muse");
#else
    ui_overlay_key_hint(surface, 186, "长按\xE2\x96\xB2配网  长按\xE2\x96\xBC宠物");
#endif
}

void ui_overlay_bottom_rates(ui_surface_t surface, int top, unsigned fps_x10, unsigned kbytes_per_s)
{
    char text[24];
    snprintf(text, sizeof(text), "帧率 %u.%u", fps_x10 / 10u, fps_x10 % 10u);
    ui_text_draw(surface, 20, top + 7, text, UI_MUTED, UI_DEEP);
    snprintf(text, sizeof(text), "网速 %uKB/s", kbytes_per_s);
    ui_text_draw(surface, 226 - ui_text_width(text), top + 7, text, UI_MUTED, UI_DEEP);
}

void ui_overlay_status_bottom(ui_surface_t surface, int top, bool audio_only)
{
    const char *text = audio_only ? "OK看画面  其他键关闭" : "OK只听声音  其他键关闭";
    ui_overlay_key_line(surface, (surface.width - ui_text_width(text)) / 2, top + 7, text, UI_DEEP);
}

static void centred(ui_surface_t surface, int y, const char *text, uint16_t color);

void ui_overlay_recover(ui_surface_t surface, const ui_recover_page_t *page)
{
    if (!page) {
        return;
    }
    if (page->qr) {
        // Large enough to read from arm's length on both panels: 4 px a module
        // for the usual version-3 code (33 modules with the quiet zone, 132 px),
        // never under 3.
        const int modules = qrcodegen_getSize(page->qr) + 2 * UI_QR_QUIET;
        int scale = UI_RECOVER_QR_BOX / modules;
        if (scale > 4) scale = 4;
        if (scale < 3) scale = 3;
        const int side = modules * scale;
        ui_qr_draw_colors(surface, (surface.width - side) / 2, 36 + (UI_RECOVER_QR_BOX - side) / 2, scale,
                          page->qr, UI_PAPER, UI_DEEP);
        centred(surface, 172, page->line1, UI_PAPER);
        centred(surface, 191, page->line2, UI_AMBER);
        return;
    }
    ui_draw_pet(surface, page->pet, page->alarm ? 0u : 1u, (surface.width - 2 * UI_PET_SIZE) / 2, 62, 2);
    centred(surface, 124, page->line1, page->alarm ? UI_VERM : UI_PAPER);
    centred(surface, 148, page->line2, UI_MUTED);
}

void ui_overlay_recover_bottom(ui_surface_t surface, int top, unsigned seconds_left)
{
    static const char tail[] = " 后失效  任意键关闭";
    if (!seconds_left) {
        ui_overlay_bottom_text(surface, top, "任意键关闭", UI_MUTED);
        return;
    }
    char time[12];
    snprintf(time, sizeof(time), "%u:%02u", seconds_left / 60u, seconds_left % 60u);
    const int x = (surface.width - ui_text_width(time) - ui_text_width(tail)) / 2;
    // Unpadded runs, so the second does not paint over the end of the first.
    const int pen = ui_text_draw_scaled(surface, x, top + 7, time, 1, UI_AMBER, UI_DEEP);
    ui_text_draw_scaled(surface, pen, top + 7, tail, 1, UI_MUTED, UI_DEEP);
}

void ui_overlay_bottom_text(ui_surface_t surface, int top, const char *text, uint16_t color)
{
    if (!text) {
        return;
    }
    ui_text_draw(surface, (surface.width - ui_text_width(text)) / 2, top + 7, text, color, UI_DEEP);
}

// ---- Voice screens ----

#define VOICE_HEAD_TOP 36
#define VOICE_RULE_Y 90
#define VOICE_ROWS_TOP 96
#define VOICE_ROW_H 24

// The key that reaches setup from playback, for the "unavailable" screen: the
// status page, then ▲ held there.
#define UI_SETUP_KEYS "配网：双击" UI_STATUS_KEY " 再长按\xE2\x96\xB2"

static void voice_words(ui_surface_t surface, const char *words, const char *status,
                        uint16_t status_color, uint16_t mic_color)
{
    ui_draw_icon(surface, UI_ICON_MIC, 14, VOICE_HEAD_TOP + 6, 2, mic_color);
    char quoted[UI_VOICE_HEARD_MAX + 8], fitted[UI_VOICE_HEARD_MAX + 8];
    snprintf(quoted, sizeof(quoted), "\xE3\x80\x8C%s\xE3\x80\x8D", words);   // 「 」
    (void)ui_text_ellipsize(fitted, sizeof(fitted), quoted, 222 - 52);
    ui_text_draw_scaled(surface, 52, VOICE_HEAD_TOP + 6, fitted, 1, UI_PAPER, UI_INK);
    (void)ui_text_ellipsize(fitted, sizeof(fitted), status, 222 - 52);
    ui_text_draw_scaled(surface, 52, VOICE_HEAD_TOP + 28, fitted, 1, status_color, UI_INK);
}

static void voice_message(ui_surface_t surface, const char *line1, uint16_t color1,
                          const char *line2, uint16_t mic_color)
{
    ui_draw_icon(surface, UI_ICON_MIC, 14, VOICE_HEAD_TOP + 6, 2, mic_color);
    char fitted[UI_VOICE_STATUS_MAX + 8];
    (void)ui_text_ellipsize(fitted, sizeof(fitted), line1, 222 - 52);
    ui_text_draw_scaled(surface, 52, VOICE_HEAD_TOP + 6, fitted, 1, color1, UI_INK);
    (void)ui_text_ellipsize(fitted, sizeof(fitted), line2, 222 - 52);
    ui_text_draw_scaled(surface, 52, VOICE_HEAD_TOP + 28, fitted, 1, UI_MUTED, UI_INK);
}

// One row below the header, `k` counted from the top of the screen.
static void voice_row(ui_surface_t surface, unsigned k, bool chosen, const ui_voice_row_t *row)
{
    const int top = VOICE_ROWS_TOP + (int)k * VOICE_ROW_H;
    if (!covers(surface, top, top + VOICE_ROW_H)) return;
    if (chosen) ui_draw_notched_rect(surface, 6, top, 222, VOICE_ROW_H, UI_VERM);
    const uint16_t back = chosen ? UI_VERM : UI_INK;
    const uint16_t fore = chosen ? UI_DEEP : row->muted ? UI_MUTED : UI_PAPER;
    const int y = top + 4;
    if (row->lead[0]) {
        ui_text_draw_scaled(surface, 16, y, row->lead, 1, chosen ? UI_DEEP : UI_MUTED, back);
    } else {
        ui_draw_icon(surface, (ui_icon_t)row->icon, 14, y, 1, chosen ? UI_DEEP : UI_AMBER);
    }
    char fitted[UI_MENU_NAME_MAX + 8];
    (void)ui_text_ellipsize(fitted, sizeof(fitted), row->label,
                            (row->right[0] ? 222 - ui_text_width(row->right) - 8 : 222) - 42);
    ui_text_draw_scaled(surface, 42, y, fitted, 1, fore, back);
    if (row->right[0]) {
        ui_text_draw_scaled(surface, 222 - ui_text_width(row->right), y, row->right, 1,
                            chosen ? UI_DEEP : UI_MUTED, back);
    }
}

static void voice_fixed_row(ui_surface_t surface, unsigned k, bool chosen, ui_icon_t icon,
                            const char *label, bool muted)
{
    ui_voice_row_t row;
    memset(&row, 0, sizeof(row));
    row.icon = (uint8_t)icon;
    row.muted = muted;
    snprintf(row.label, sizeof(row.label), "%s", label);
    voice_row(surface, k, chosen, &row);
}

// What can be said, under the header (listening, not understood).
static void voice_examples(ui_surface_t surface)
{
    if (covers(surface, VOICE_ROWS_TOP, VOICE_ROWS_TOP + VOICE_ROW_H)) {
        ui_text_draw_scaled(surface, 16, VOICE_ROWS_TOP + 4, "可以说：", 1, UI_MUTED, UI_INK);
    }
    voice_fixed_row(surface, 1, false, UI_ICON_TV, "湖南卫视 / 中央五套", true);
    voice_fixed_row(surface, 2, false, UI_ICON_FILM, "熊出没 第三集", true);
    voice_fixed_row(surface, 3, false, UI_ICON_VOLUME, "大声一点 / 暂停", true);
}

static void voice_bars(ui_surface_t surface, const uint8_t *levels)
{
    for (unsigned k = 0; k < UI_VOICE_BARS; k++) {
        const int x = 58 + (int)k * 14;
        const unsigned cells = (levels[k] * 6u + 127u) / 255u;
        ui_text_fill(surface, x, 80, 10, 3, UI_RULE);
        for (unsigned c = 0; c < cells && c < 6u; c++) {
            ui_text_fill(surface, x, 80 - (int)c * 6, 10, 4, c + 1u == cells ? UI_VERM : UI_AMBER);
        }
    }
}

void ui_overlay_voice(ui_surface_t surface, const ui_menu_t *menu)
{
    if (!menu || !covers(surface, VOICE_HEAD_TOP, VOICE_ROWS_TOP + (int)UI_VOICE_ROWS_SHOWN * VOICE_ROW_H)) {
        return;
    }
    const ui_voice_t *v = &menu->voice;
    if (v->screen == UI_VOICE_NONE) return;
    char line[UI_VOICE_STATUS_MAX + 8];
    switch (v->screen) {
    case UI_VOICE_LISTENING:
        ui_draw_icon(surface, UI_ICON_MIC, 14, VOICE_HEAD_TOP + 6, 2, UI_VERM);
        voice_bars(surface, v->levels);
        break;
    case UI_VOICE_RECOGNISING:
        if (v->heard[0]) {
            voice_words(surface, v->heard, "正在识别…", UI_AMBER, UI_AMBER);
        } else {
            snprintf(line, sizeof(line), "录了 %u.%u 秒", v->recorded_ds / 10u, v->recorded_ds % 10u);
            voice_message(surface, line, UI_PAPER, "", UI_AMBER);
            ui_text_draw_scaled(surface, 52, VOICE_HEAD_TOP + 28, "正在识别…", 1, UI_AMBER, UI_INK);
        }
        break;
    case UI_VOICE_DONE:
        voice_words(surface, v->heard, v->status[0] ? v->status : "好的", UI_AMBER, UI_AMBER);
        break;
    case UI_VOICE_CHOICES:
        if (v->status[0]) {
            voice_words(surface, v->heard, v->status, UI_AMBER, UI_AMBER);
        } else {
            const unsigned n = v->row_count ? v->row_count - 1u : 0u;
            snprintf(line, sizeof(line), "找到 %u 个，选一个", n);
            voice_words(surface, v->heard, line, UI_AMBER, UI_AMBER);
        }
        break;
    case UI_VOICE_NOT_UNDERSTOOD:
        voice_words(surface, v->heard[0] ? v->heard : "……", v->status[0] ? v->status : "没听懂，再说一次？",
                    UI_VERM, UI_MUTED);
        break;
    case UI_VOICE_SETUP:
        voice_words(surface, v->heard, "进入配网会停止播放", UI_AMBER, UI_AMBER);
        break;
    case UI_VOICE_UNAVAILABLE:
        voice_message(surface, "语音现在用不了", UI_VERM, "需要连上电视服务器", UI_MUTED);
        break;
    case UI_VOICE_ERROR:
    default:
        voice_message(surface, "没有识别出来", UI_VERM, v->status[0] ? v->status : "服务器没回应，稍后再试",
                      UI_MUTED);
        break;
    }
    ui_draw_dotted_hline(surface, 16, 223, VOICE_RULE_Y, UI_RULE);
    switch (v->screen) {
    case UI_VOICE_LISTENING:
        voice_examples(surface);
        // Ten cells along the bottom: the ten-second limit.
        ui_draw_cells(surface, 200, 4, v->elapsed_ds / 10u, UI_VERM, UI_RULE);
        break;
    case UI_VOICE_NOT_UNDERSTOOD:
        if (covers(surface, VOICE_ROWS_TOP, VOICE_ROWS_TOP + VOICE_ROW_H)) {
            ui_text_draw_scaled(surface, 16, VOICE_ROWS_TOP + 4, "可以说：", 1, UI_MUTED, UI_INK);
        }
        voice_fixed_row(surface, 1, false, UI_ICON_TV, "频道名，或\"第12台\"", true);
        voice_fixed_row(surface, 2, false, UI_ICON_FILM, "剧名加\"第几集\"", true);
        voice_fixed_row(surface, 3, false, UI_ICON_SUN, "\"亮一点\" \"只听声音\"", true);
        break;
    case UI_VOICE_UNAVAILABLE:
        voice_fixed_row(surface, 0, false, UI_ICON_WIFI, UI_SETUP_KEYS, true);
        break;
    case UI_VOICE_SETUP:
        voice_fixed_row(surface, 0, v->selected == 0, UI_ICON_WIFI, "进入配网", false);
        voice_fixed_row(surface, 1, v->selected == 1, UI_ICON_CROSS, "算了", false);
        break;
    case UI_VOICE_DONE:
    case UI_VOICE_CHOICES:
        for (unsigned k = 0; k < UI_VOICE_ROWS_SHOWN; k++) {
            const unsigned row = v->scroll + k;
            if (row >= v->row_count) break;
            voice_row(surface, k, v->highlight && row == v->selected, &v->rows[row]);
        }
        break;
    default:
        break;
    }
}

unsigned ui_overlay_voice_label(const ui_menu_t *menu, const char *texts[3], uint16_t colors[3],
                                char *scratch, size_t scratch_size)
{
    if (!menu || !texts || !colors) {
        return 0;
    }
    switch (menu->voice.screen) {
    case UI_VOICE_LISTENING:
        texts[0] = "\xE2\x97\x8F ";   // ●
        colors[0] = UI_VERM;
        texts[1] = "正在听…";
        colors[1] = UI_PAPER;
        return 2;
    case UI_VOICE_RECOGNISING:
        texts[0] = "识别中…";
        colors[0] = UI_MUTED;
        return 1;
    case UI_VOICE_CHOICES: {
        // Counted over the candidates; the last row ("none of these") has no number.
        const unsigned candidates = menu->voice.row_count ? menu->voice.row_count - 1u : 0u;
        if (scratch && scratch_size && menu->voice.selected < candidates) {
            snprintf(scratch, scratch_size, "语音 %u/%u", menu->voice.selected + 1u, candidates);
            texts[0] = scratch;
        } else {
            texts[0] = "语音";
        }
        colors[0] = UI_MUTED;
        return 1;
    }
    default:
        texts[0] = "语音";
        colors[0] = UI_MUTED;
        return 1;
    }
}

const char *ui_overlay_voice_hint(const ui_menu_t *menu)
{
    if (!menu) {
        return NULL;
    }
    switch (menu->voice.screen) {
    case UI_VOICE_NONE:
        return "按住OK说话  双击OK返回";
    case UI_VOICE_LISTENING:
        return "松开OK结束  \xE2\x96\xB2取消";
    case UI_VOICE_RECOGNISING:
        return "\xE2\x96\xB2取消";
    case UI_VOICE_DONE:
        return "按住OK再说";
    case UI_VOICE_CHOICES:
        return "OK选  按住OK重说  双击OK返回";
    case UI_VOICE_SETUP:
        return "OK确定  双击OK返回";
    case UI_VOICE_UNAVAILABLE:
        return "任意键关闭";
    case UI_VOICE_NOT_UNDERSTOOD:
    case UI_VOICE_ERROR:
    default:
        return "按住OK再说  双击OK返回";
    }
}

void ui_overlay_bottom_keys(ui_surface_t surface, int top, const char *text)
{
    if (text) {
        ui_overlay_key_line(surface, (surface.width - ui_text_width(text)) / 2, top + 7, text, UI_DEEP);
    }
}

// ---- Waiting screens and the pet picker ----

const char *ui_overlay_wait_label(ui_wait_kind_t kind)
{
    return kind == UI_WAIT_CHANNEL ? "换台中" : "小智电视";
}

static void centred(ui_surface_t surface, int y, const char *text, uint16_t color)
{
    if (text && text[0]) {
        char fitted[96];
        (void)ui_text_ellipsize(fitted, sizeof(fitted), text, 208);
        ui_text_draw_scaled(surface, (surface.width - ui_text_width(fitted)) / 2, y, fitted, 1,
                            color, UI_INK);
    }
}

// Back and forth over positions 0..last, one step per call of `step`.
static unsigned bounce(unsigned step, unsigned last)
{
    const unsigned p = step % (2u * last);
    return p <= last ? p : 2u * last - p;
}

// A small plus-shaped star, brighter towards the middle of its twinkle.
static void draw_star(ui_surface_t surface, int x, int y, unsigned phase)
{
    // 24 tenths a cycle: dark, glimmer, bright, glimmer, dark.
    phase %= 24u;
    if (phase < 8u) return;
    const uint16_t color = phase >= 12u && phase < 16u ? UI_PAPER : UI_FAINT;
    ui_text_fill(surface, x, y, 1, 1, color);
    if (phase >= 10u && phase < 18u) {
        ui_text_fill(surface, x - 2, y, 5, 1, color);
        ui_text_fill(surface, x, y - 2, 1, 5, color);
    }
}

// The listening screen (audio-only): stars, the pet moving with the sound,
// notes, and the loudness bars; asleep when the screen has dimmed. `tick` is
// in tenths of a second.
static void draw_listen(ui_surface_t surface, const ui_wait_page_t *page, unsigned tick)
{
    static const struct { int x, y; unsigned offset; } stars[] = {
        { 24, 48, 0 }, { 214, 44, 9 }, { 34, 138, 15 }, { 206, 140, 5 }, { 120, 38, 19 },
    };
    for (unsigned i = 0; i < sizeof(stars) / sizeof(stars[0]); i++) {
        draw_star(surface, stars[i].x, stars[i].y, tick + stars[i].offset);
    }

    // Breathing: a slow 3 px rise and fall over 2 s (slower asleep).
    static const int8_t breath[10] = { 0, 1, 2, 3, 3, 3, 2, 1, 0, 0 };
    const int rest = breath[(page->asleep ? tick / 4u : tick / 2u) % 10u];
    int lift = rest;
    unsigned mood = 1u;
    if (page->asleep) {
        mood = 2u;   // each pet's own sleepy (or switched-off) face
    } else if (page->since_wake < 8u) {
        // Woken: one happy hop.
        static const int8_t hop[8] = { 4, 9, 12, 12, 9, 5, 2, 0 };
        lift += hop[page->since_wake];
    } else {
        // Up with the sound, and its plain face a moment every 4 s.
        lift += page->level * 10 / 255;
        if (tick % 40u >= 38u) mood = 0u;
    }
    if (page->asleep) {
        // Z z rising from the top of its head; drawn first, because each
        // letter brings its own ink background and the pet must stay whole.
        // Three letters 8 tenths apart, each rising 2 px a tenth: always at
        // least 16 rows apart, so their cells never overlap.
        for (unsigned j = 0; j < 3u; j++) {
            const unsigned age = (tick + j * 8u) % 24u;
            // Clear of the pet's right edge (x=192): some pets' sleepy faces
            // carry a little z of their own at the top corner.
            const int y = 76 - (int)age * 2;
            const int x = 196 + (int)age / 2;
            if (y < 32) continue;
            ui_text_draw_scaled(surface, x, y, "Z", 1, age > 14u ? UI_PAPER : UI_MUTED, UI_INK);
        }
    }
    // At 6x (144 px), standing with its feet just above the bars (row 189),
    // whatever empty rows its sprite has; the lift is capped so the head
    // never reaches the top bar.
    int top = 0, bottom = UI_PET_SIZE - 1;
    (void)ui_pet_rows(page->pet, mood, &top, &bottom);
    const int y = 189 - (bottom + 1) * 6;
    if (y + top * 6 - lift < 32) lift = y + top * 6 - 32;
    if (lift < 0) lift = 0;
    ui_draw_pet(surface, page->pet, mood, 48, y - lift, 6);

    if (!page->asleep) {
        // More notes the louder it is, each rising for 3 s, left and right in turn.
        const unsigned notes = page->level < 60u ? 1u : page->level < 140u ? 2u : 3u;
        for (unsigned i = 0; i < notes; i++) {
            const unsigned age = (tick + i * 10u) % 30u;
            const int y = 152 - (int)age * 4;
            if (y < 36) continue;
            const int sway = ((age / 4u) % 2u) ? 3 : 0;
            const bool left = ((tick + i * 10u) / 30u + i) % 2u == 0u;
            ui_draw_icon(surface, UI_ICON_NOTE, left ? 14 + sway : 194 - sway, y, 2,
                         i == 1u ? UI_PAPER : UI_AMBER);
        }
    }

    // The bars: the last 12 chunks' loudness, oldest at the left, as stacks of
    // up to four 3 px cells under the pet's feet; the top lit cell vermilion.
    // A resting baseline when asleep or silent.
    for (unsigned k = 0; k < UI_LISTEN_BARS; k++) {
        const int x = 30 + (int)k * 15;
        const unsigned cells = page->asleep ? 0u : (page->history[k] * 4u + 127u) / 255u;
        ui_text_fill(surface, x, 204, 11, 3, UI_RULE);
        for (unsigned c = 0; c < cells && c < 4u; c++) {
            ui_text_fill(surface, x, 204 - (int)c * 4, 11, 3, c + 1u == cells ? UI_VERM : UI_AMBER);
        }
    }
}

void ui_overlay_wait(ui_surface_t surface, const ui_wait_page_t *page, unsigned tick)
{
    if (!page || !covers(surface, 30, 210)) {
        return;
    }
    ui_draw_panel(surface, 30, 210);
    const int bob = (int)((tick / 5u) % 2u);   // a pixel up every other half second
    switch (page->kind) {
    case UI_WAIT_WIFI: {
        ui_draw_pet(surface, page->pet, 0, 96, 50 - bob, 2);
        ui_draw_icon(surface, UI_ICON_WIFI, 148, 44, 1, UI_AMBER);
        centred(surface, 106, "正在连接 Wi-Fi", UI_PAPER);
        centred(surface, 130, page->line, UI_MUTED);
        const unsigned lit = (tick / 5u) % 3u;
        for (unsigned i = 0; i < 3u; i++) {
            ui_text_fill(surface, 103 + 14 * (int)i, 162, 6, 6, i == lit ? UI_AMBER : UI_RULE);
        }
        ui_draw_dotted_hline(surface, 16, 223, 186, UI_RULE);
        ui_overlay_key_hint(surface, 194, "双击" UI_STATUS_KEY " 状态与配网");
        break;
    }
    case UI_WAIT_SERVER: {
        ui_draw_pet(surface, page->pet, 0, 36, 46 - bob, 2);
        ui_draw_icon(surface, UI_ICON_GLOBE, 160, 58, 2, UI_PAPER);
        ui_draw_dotted_hline(surface, 88, 151, 74, UI_FAINT);
        ui_draw_dotted_hline(surface, 88, 151, 75, UI_FAINT);
        ui_text_fill(surface, 88 + 4 * (int)bounce(tick / 2u, 14u), 72, 6, 6, UI_AMBER);
        const char *pet_name = ui_pet_name(page->pet);
        ui_text_draw_scaled(surface, 44, 100, pet_name ? pet_name : "", 1, UI_MUTED, UI_INK);
        ui_text_draw_scaled(surface, 152, 100, "服务器", 1, UI_MUTED, UI_INK);
        centred(surface, 126, "正在连接服务器", UI_PAPER);
        centred(surface, 150, page->line, UI_MUTED);
        ui_draw_dotted_hline(surface, 16, 223, 186, UI_RULE);
        ui_overlay_key_hint(surface, 194, "双击" UI_STATUS_KEY " 状态与改地址");
        break;
    }
    case UI_WAIT_CHANNEL: {
        // "CH" and the number at 3x on one baseline, centred as a pair.
        char number[8];
        int scale = 3;
        if (page->number) snprintf(number, sizeof(number), "%02u", page->number % 100u);
        else { snprintf(number, sizeof(number), "\xC2\xB7\xC2\xB7"); scale = 2; }
        const int ch_w = ui_text_width("CH"), num_w = ui_text_width(number) * scale;
        const int x = (surface.width - (ch_w + 6 + num_w)) / 2;
        const int num_top = 100 - (int)UI_TEXT_LINE_H * scale;
        // The font's baseline is row 12 of its 16; both runs stand on it.
        ui_text_draw_scaled(surface, x, num_top + 12 * scale - 12, "CH", 1, UI_AMBER, UI_INK);
        ui_text_draw_scaled(surface, x + ch_w + 6, num_top, number, scale, UI_AMBER, UI_INK);
        centred(surface, 108, page->name[0] ? page->name : "正在打开频道", UI_PAPER);
        // A relay channel's video: its title where the list position was.
        centred(surface, 132, page->title[0] ? page->title : page->detail, UI_MUTED);
        ui_text_fill(surface, 40, 168, 160, 4, UI_RULE);
        ui_text_fill(surface, 40 + 8 * (int)bounce(tick, 15u), 168, 40, 4, UI_AMBER);
        const char *verb = page->loading ? "正在加载" : "正在打开";
        if (page->episode[0]) {
            // The verb muted, the episode in amber, centred as one line.
            const int space = ui_text_width(" ");
            const int width = ui_text_width(verb) + space + ui_text_width(page->episode);
            int pen = (surface.width - width) / 2;
            pen = ui_text_draw_scaled(surface, pen, 180, verb, 1, UI_MUTED, UI_INK);
            ui_text_draw_scaled(surface, pen + space, 180, page->episode, 1, UI_AMBER, UI_INK);
        } else {
            centred(surface, 180, verb, UI_MUTED);
        }
        break;
    }
    case UI_WAIT_LISTEN:
        draw_listen(surface, page, tick);
        break;
    case UI_WAIT_FAILED: {
        ui_draw_pet(surface, page->pet, 3, 96, 40 - bob, 2);
        centred(surface, 90, "连不上服务器", UI_PAPER);
        centred(surface, 114, page->line, UI_MUTED);
        ui_draw_dotted_hline(surface, 16, 223, 150, UI_RULE);
        int pen = ui_text_draw_scaled(surface, 46, 164, "1 ", 1, UI_AMBER, UI_INK);
        ui_overlay_key_line(surface, pen, 164, "双击" UI_STATUS_KEY " 打开状态", UI_INK);
        pen = ui_text_draw_scaled(surface, 46, 188, "2 ", 1, UI_AMBER, UI_INK);
        ui_overlay_key_line(surface, pen, 188, "长按\xE2\x96\xB2 改地址", UI_INK);
        break;
    }
    }
}

unsigned ui_overlay_wait_frame(ui_wait_kind_t kind, unsigned tick, int *y0, int *y1)
{
    const unsigned bob = (tick / 5u) % 2u;
    switch (kind) {
    case UI_WAIT_WIFI:
        *y0 = 48; *y1 = 170;
        return bob * 3u + (tick / 5u) % 3u;
    case UI_WAIT_SERVER:
        *y0 = 44; *y1 = 98;
        return bob * 16u + bounce(tick / 2u, 14u);
    case UI_WAIT_CHANNEL:
        *y0 = 168; *y1 = 172;
        return bounce(tick, 15u);
    case UI_WAIT_LISTEN:
        // The whole picture area moves (pet, notes, bars, stars), every 0.1 s.
        *y0 = 30; *y1 = 210;
        return tick;
    case UI_WAIT_FAILED:
    default:
        *y0 = 38; *y1 = 90;
        return bob;
    }
}

void ui_overlay_pets(ui_surface_t surface, const ui_menu_t *menu)
{
    if (!menu || menu->pet_count == 0) {
        return;
    }
    const unsigned count = menu->pet_count, at = menu->pet_browse % count;
    const unsigned prev = (at + count - 1u) % count, next = (at + 1u) % count;
    ui_draw_pet(surface, prev, 0, 20, 66, 1);
    ui_text_draw_scaled(surface, 14 + (37 - ui_text_width("\xE2\x96\xB2")) / 2, 94, "\xE2\x96\xB2", 1,
                        UI_AMBER, UI_INK);
    ui_draw_pet(surface, next, 0, 196, 66, 1);
    ui_text_draw_scaled(surface, 190 + (37 - ui_text_width("\xE2\x96\xBC")) / 2, 94, "\xE2\x96\xBC", 1,
                        UI_AMBER, UI_INK);
    ui_draw_pet(surface, at, 1, 84, 38, 3);
    centred(surface, 116, ui_pet_name(at), UI_PAPER);
    centred(surface, 138, ui_pet_kind(at), UI_MUTED);
    if (at == menu->pet_current) {
        const char *used = "\xE2\x97\x8F 使用中";
        ui_text_draw_scaled(surface, (surface.width - ui_text_width(used)) / 2, 164, used, 1,
                            UI_AMBER, UI_INK);
    } else {
        ui_overlay_key_hint(surface, 164, "OK 选它");
    }
    for (unsigned k = 0; k < count && k < 16u; k++) {
        ui_text_fill(surface, 78 + 8 * (int)k, 196, 4, 4, k == at ? UI_AMBER : UI_RULE);
    }
}

void ui_overlay_pet_bottom(ui_surface_t surface, int top)
{
    const char *text = "长按OK 返回";
    ui_overlay_key_line(surface, (surface.width - ui_text_width(text)) / 2, top + 7, text, UI_DEEP);
}

// ---- Setup pages ----

static void setup_row(ui_surface_t surface, int y, const char *label, const char *value,
                      uint16_t color)
{
    ui_text_draw_scaled(surface, 20, y, label, 1, UI_MUTED, UI_INK);
    if (value) {
        char fitted[96];
        (void)ui_text_ellipsize(fitted, sizeof(fitted), value, 226 - 72);
        ui_text_draw_scaled(surface, 72, y, fitted, 1, color, UI_INK);
    }
}

void ui_overlay_setup(ui_surface_t surface, const ui_setup_page_t *page, bool blink_on)
{
    if (!page) {
        return;
    }
    // The bar: page number and title at the left, page squares at the right.
    ui_text_fill(surface, 0, 0, surface.width, 30, UI_DEEP);
    ui_text_fill(surface, 0, 30, surface.width, 180, UI_INK);
    ui_text_fill(surface, 0, 210, surface.width, 30, UI_DEEP);
    // At the panel's own edges, like every other page's top bar.
    const ui_surface_t bar = ui_surface_whole(surface);
    char number[12];
    snprintf(number, sizeof(number), "%u/%u", page->index + 1u, page->count);
    int pen = ui_text_draw_scaled(bar, 20, 7, number, 1, UI_AMBER, UI_DEEP);
    ui_text_draw_scaled(bar, pen + 6, 7, page->title ? page->title : "", 1, UI_PAPER, UI_DEEP);
    // Up to four squares end where they always did; a fifth (the sound page)
    // moves the row left by one step rather than past the edge.
    const int extra = page->count > 4u ? (int)page->count - 4 : 0;
    for (unsigned k = 0; k < page->count && k < 5u; k++) {
        ui_text_fill(bar, bar.width - 44 - 8 * extra + 8 * (int)k, 13, 4, 4, k == page->index ? UI_AMBER : UI_RULE);
    }

    if (!page->status) {
        if (page->qr) {
            // As large as fits the design's 120 px square, centred in it; a
            // version-5 code (the sound page with nearby networks) spills a
            // few pixels over it rather than drop to 2 px modules.
            const int modules = qrcodegen_getSize(page->qr) + 2 * UI_QR_QUIET;
            const int scale = ui_qr_setup_scale(modules);
            const int side = modules * scale;
            ui_qr_draw_colors(surface, 60 + (120 - side) / 2, 38 + (120 - side) / 2, scale, page->qr,
                              UI_PAPER, UI_DEEP);
        }
        ui_draw_pet(surface, page->pet, page->mood, 28, 132, 1);
        centred(surface, 164, page->line1, UI_PAPER);
        centred(surface, 186, page->line2, page->line2_color);
    } else {
        setup_row(surface, 40, "热点", page->ssid, UI_PAPER);
        setup_row(surface, 64, "配网", page->page_url, UI_PAPER);
        setup_row(surface, 88, "服务器", page->server, UI_PAPER);
        setup_row(surface, 112, "设备", page->client_ready ? "已连接设备" : "等待设备连接",
                  page->client_ready ? UI_PAPER : UI_MUTED);
        if (page->client_ready || blink_on) {
            ui_text_fill(surface, 216, 117, 6, 6, UI_AMBER);
        }
        ui_draw_dotted_hline(surface, 16, 223, 140, UI_RULE);
        ui_draw_pet(surface, page->pet, page->client_ready ? 1u : 0u, 22, 150, 2);
        ui_text_draw_scaled(surface, 90, 152, "剩余时间", 1, UI_MUTED, UI_INK);
        char left[8];
        const unsigned s = page->remaining_s > 5999u ? 5999u : page->remaining_s;
        snprintf(left, sizeof(left), "%02u:%02u", s / 60u, s % 60u);
        ui_text_draw_scaled(surface, 90, 176, left, 2, UI_AMBER, UI_INK);
    }
    const char *keys = "\xE2\x96\xB2\xE2\x96\xBC翻页  长按OK退出";
    ui_overlay_key_line(surface, (surface.width - ui_text_width(keys)) / 2, 217, keys, UI_DEEP);
}

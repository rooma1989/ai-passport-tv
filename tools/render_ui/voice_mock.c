// PROTOTYPE, not firmware: mock-ups of the proposed voice control screens
// (docs/superpowers/specs/2026-10-12-voice-control-design.md), drawn with the
// firmware's own primitives and font so they can be judged next to the real
// screens that render_ui.sh draws. Nothing here is linked into the device
// image; when the feature is built, these layouts move into ui_overlay.c and
// this file is deleted. Run through voice_mock.sh.
#include "ui_draw.h"
#include "ui_menu.h"
#include "ui_overlay.h"
#include "ui_status.h"

#include <stdio.h>
#include <string.h>

#ifndef RENDER_W
#define RENDER_W 240
#endif
enum { W = RENDER_W, H = 240, SCALE = 2, COLS = 4, ROWS = 3, GAP = 8 };

static uint8_t screen[W * H * 2];

static ui_surface_t whole(void)
{
    ui_surface_t surface = { screen, W, 0, H, 0, 0 };
    return surface;
}

static ui_surface_t view(void)
{
    return ui_surface_centered(whole());
}

static void picture(void)
{
    ui_surface_t s = whole();
    static const uint16_t colors[] = { 0x4A69, 0x7BCF, 0x2945, 0xC618, 0x8410, 0x3186, 0xA514 };
    for (int y = 30; y < 210; y++) {
        for (int x = 0; x < W; x += 20) {
            ui_text_fill(s, x, y, 20, 1, colors[(x / 20 + y / 30) % 7]);
        }
    }
}

// The top bar with a label of up to three coloured runs.
static void topbar(const char *a, uint16_t ca, const char *b, uint16_t cb)
{
    ui_topbar_label_t label;
    const char *texts[] = { a, b };
    const uint16_t colors[] = { ca, cb };
    ui_topbar_set_label(&label, texts, colors, b ? 2 : 1);
    ui_status_draw(whole(), W, &label, 3, 76, 0, false);
}

static void bottom_hint(const char *text)
{
    ui_text_fill(whole(), 0, 210, W, 30, UI_DEEP);
    ui_overlay_key_line(view(), (240 - ui_text_width(text)) / 2, 217, text, UI_DEEP);
}

// A microphone, 16x16, one bit a dot (the icon set has none yet; it would be
// added to xiaozhi_tv_ui_assets.h as UI_ICON_MIC).
static const char *const MIC[16] = {
    "......####......",
    ".....######.....",
    ".....######.....",
    ".....######.....",
    ".....######.....",
    ".....######.....",
    "..#..######..#..",
    "..#..######..#..",
    "..#...####...#..",
    "...#........#...",
    "....##....##....",
    "......####......",
    ".......##.......",
    ".......##.......",
    ".....######.....",
    "................",
};

static void mic(int x, int y, int scale, uint16_t color)
{
    for (int r = 0; r < 16; r++) {
        for (int c = 0; c < 16; c++) {
            if (MIC[r][c] == '#') ui_text_fill(view(), x + c * scale, y + r * scale, scale, scale, color);
        }
    }
}

// The voice header: rows 36..87 of the list panel. The microphone at the left;
// either the loudness bars (listening) or the words heard and a status line.
#define HEAD_TOP 36
#define RULE_Y 90
#define ROWS_TOP 96
#define ROW_H 24

static void level_bars(const unsigned char *levels, unsigned n)
{
    for (unsigned k = 0; k < n; k++) {
        const int x = 58 + (int)k * 14;
        const unsigned cells = (levels[k] * 6u + 127u) / 255u;
        ui_text_fill(view(), x, 80, 10, 3, UI_RULE);
        for (unsigned c = 0; c < cells && c < 6u; c++) {
            ui_text_fill(view(), x, 80 - (int)c * 6, 10, 4, c + 1u == cells ? UI_VERM : UI_AMBER);
        }
    }
}

static void heard(const char *words, const char *status, uint16_t status_color, uint16_t mic_color)
{
    mic(14, HEAD_TOP + 6, 2, mic_color);
    char quoted[96], fitted[96];
    snprintf(quoted, sizeof(quoted), "\xE3\x80\x8C%s\xE3\x80\x8D", words);   // 「 」
    (void)ui_text_ellipsize(fitted, sizeof(fitted), quoted, 222 - 52);
    ui_text_draw_scaled(view(), 52, HEAD_TOP + 6, fitted, 1, UI_PAPER, UI_INK);
    ui_text_draw_scaled(view(), 52, HEAD_TOP + 28, status, 1, status_color, UI_INK);
}

static void rule(void)
{
    ui_draw_dotted_hline(view(), 16, 223, RULE_Y, UI_RULE);
}

// One row below the header. `lead` is a two-character column (a channel
// number, an episode number) or NULL for an icon.
static void row(unsigned k, bool chosen, const char *lead, ui_icon_t icon, const char *text,
                const char *right, bool muted)
{
    const int top = ROWS_TOP + (int)k * ROW_H;
    if (chosen) ui_draw_notched_rect(view(), 6, top, 222, ROW_H, UI_VERM);
    const uint16_t back = chosen ? UI_VERM : UI_INK;
    const uint16_t fore = chosen ? UI_DEEP : muted ? UI_MUTED : UI_PAPER;
    const int y = top + 4;
    if (lead) ui_text_draw_scaled(view(), 16, y, lead, 1, chosen ? UI_DEEP : UI_MUTED, back);
    else ui_draw_icon(view(), icon, 14, y, 1, chosen ? UI_DEEP : UI_AMBER);
    char fitted[96];
    (void)ui_text_ellipsize(fitted, sizeof(fitted), text, (right ? 180 : 206) - 42);
    ui_text_draw_scaled(view(), 42, y, fitted, 1, fore, back);
    if (right) ui_text_draw_scaled(view(), 222 - ui_text_width(right), y, right, 1,
                                   chosen ? UI_DEEP : UI_MUTED, back);
}

static ui_menu_t sample_menu(void)
{
    static const char groups[][UI_MENU_GROUP_NAME_MAX] = {
        "我的投屏", "UP 主", "央视", "卫视", "港澳台", "国际", "音乐", "动画影视", "其他" };
    static const ui_menu_entry_t entries[] = {
        { "cast", "我的投屏", 0 }, { "up01", "小Lin说", 1 }, { "up02", "老高与小茉", 1 },
        { "ch1", "CCTV-1 综合", 2 }, { "ch5", "CCTV-5 体育", 2 }, { "ch5p", "CCTV-5+ 体育赛事", 2 },
        { "ch7", "CCTV-7 国防军事", 2 }, { "ws1", "湖南卫视", 3 }, { "ws2", "浙江卫视", 3 },
        { "hk1", "凤凰中文", 4 }, { "w1", "CGTN", 5 }, { "m1", "精选音乐", 6 },
        { "sr01", "熊出没", 7 }, { "o1", "广东体育", 8 },
    };
    ui_menu_t menu;
    ui_menu_init(&menu);
    ui_menu_set_groups(&menu, groups, 9);
    ui_menu_load(&menu, entries, sizeof(entries) / sizeof(entries[0]), "ch7");
    return menu;
}

static uint8_t sheet[(COLS * (W * SCALE + GAP)) * (ROWS * (H * SCALE + GAP)) * 3];
static int sheet_w = COLS * (W * SCALE + GAP), sheet_h = ROWS * (H * SCALE + GAP);

static void place(int index)
{
    const int ox = (index % COLS) * (W * SCALE + GAP), oy = (index / COLS) * (H * SCALE + GAP);
    for (int y = 0; y < H * SCALE; y++) {
        for (int x = 0; x < W * SCALE; x++) {
            const uint8_t *p = screen + ((size_t)(y / SCALE) * W + (size_t)(x / SCALE)) * 2u;
            const uint16_t v = (uint16_t)((p[0] << 8) | p[1]);
            uint8_t *q = sheet + ((size_t)(oy + y) * sheet_w + (size_t)(ox + x)) * 3u;
            q[0] = (uint8_t)(((v >> 11) & 0x1F) * 255 / 31);
            q[1] = (uint8_t)(((v >> 5) & 0x3F) * 255 / 63);
            q[2] = (uint8_t)((v & 0x1F) * 255 / 31);
        }
    }
}

static void panel(void)
{
    picture();
    ui_draw_panel(whole(), 30, 210);
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: voice_mock out.ppm\n");
        return 2;
    }
    memset(sheet, 0x80, sizeof(sheet));
    int n = 0;
    static const unsigned char speech[12] = { 40, 90, 160, 220, 180, 120, 200, 240, 170, 110, 150, 90 };

    // 1. OK click: the channel list as today, with one change -- the bottom
    //    bar says how to talk. This is the 2-in-1 panel at rest.
    {
        ui_menu_t menu = sample_menu();
        ui_menu_set_rows(&menu, UI_OVERLAY_LIST_ROWS);
        ui_menu_open(&menu);
        panel();
        topbar("频道分组 3/9", UI_MUTED, NULL, 0);
        ui_overlay_list(view(), &menu);
        bottom_hint("按住OK说话  双击OK返回");
        place(n++);
    }

    // 2. OK held (>= 0.45 s) during playback or in the list: listening.
    panel();
    topbar("\xE2\x97\x8F ", UI_VERM, "正在听…", UI_PAPER);
    mic(14, HEAD_TOP + 6, 2, UI_VERM);
    level_bars(speech, 12);
    rule();
    ui_text_draw_scaled(view(), 16, ROWS_TOP + 4, "可以说：", 1, UI_MUTED, UI_INK);
    row(1, false, NULL, UI_ICON_TV, "湖南卫视 / 中央五套", NULL, true);
    row(2, false, NULL, UI_ICON_FILM, "熊出没 第三集", NULL, true);
    row(3, false, NULL, UI_ICON_VOLUME, "大声一点 / 暂停", NULL, true);
    ui_draw_cells(view(), 200, 4, 3, UI_VERM, UI_RULE);
    bottom_hint("松开OK结束  \xE2\x96\xB2取消");
    place(n++);

    // 3. Released: the server is recognising.
    panel();
    topbar("识别中…", UI_MUTED, NULL, 0);
    mic(14, HEAD_TOP + 6, 2, UI_AMBER);
    ui_text_draw_scaled(view(), 52, HEAD_TOP + 6, "录了 2.6 秒", 1, UI_PAPER, UI_INK);
    ui_text_draw_scaled(view(), 52, HEAD_TOP + 28, "正在识别…", 1, UI_AMBER, UI_INK);
    rule();
    bottom_hint("\xE2\x96\xB2取消");
    place(n++);

    // 4. One clear match: it is carried out at once; the panel shows what it
    //    did for 0.8 s, then the usual channel banner takes over.
    panel();
    topbar("语音", UI_MUTED, NULL, 0);
    heard("我要看湖南卫视", "换台中…", UI_AMBER, UI_AMBER);
    rule();
    row(0, true, "08", UI_ICON_TV, "湖南卫视", "卫视", false);
    bottom_hint("按住OK再说");
    place(n++);

    // 5. Several matches: they are the list; pick with the arrows and OK.
    panel();
    topbar("语音 1/3", UI_MUTED, NULL, 0);
    heard("看体育", "找到 3 个，选一个", UI_AMBER, UI_AMBER);
    rule();
    row(0, true, "05", UI_ICON_TV, "CCTV-5 体育", NULL, false);
    row(1, false, "06", UI_ICON_TV, "CCTV-5+ 体育赛事", NULL, false);
    row(2, false, "14", UI_ICON_TV, "广东体育", NULL, false);
    row(3, false, NULL, UI_ICON_MORE, "都不是，打开频道表", NULL, true);
    bottom_hint("OK选  按住OK重说  双击OK返回");
    place(n++);

    // 6. A series and an episode (the relay's episode list).
    panel();
    topbar("语音", UI_MUTED, NULL, 0);
    heard("熊出没第三集", "从第 3 集开始播", UI_AMBER, UI_AMBER);
    rule();
    row(0, true, "03", UI_ICON_FILM, "熊出没 第3集", "看过", false);
    row(1, false, "04", UI_ICON_FILM, "熊出没 第4集", NULL, true);
    bottom_hint("按住OK再说");
    place(n++);

    // 7. Not understood: the words, and what can be said.
    panel();
    topbar("语音", UI_MUTED, NULL, 0);
    heard("那个那个什么来着", "没听懂，再说一次？", UI_VERM, UI_MUTED);
    rule();
    ui_text_draw_scaled(view(), 16, ROWS_TOP + 4, "可以说：", 1, UI_MUTED, UI_INK);
    row(1, false, NULL, UI_ICON_TV, "频道名，或\"第12台\"", NULL, true);
    row(2, false, NULL, UI_ICON_FILM, "剧名加\"第几集\"", NULL, true);
    row(3, false, NULL, UI_ICON_SUN, "\"亮一点\" \"只听声音\"", NULL, true);
    bottom_hint("按住OK再说  双击OK返回");
    place(n++);

    // 8. A device setting: the panel closes and the usual level box shows,
    //    the words heard in the top bar for 2 s.
    {
        picture();
        topbar("\xE3\x80\x8C大声一点\xE3\x80\x8D", UI_AMBER, NULL, 0);
        ui_text_fill(whole(), 0, 210, W, 30, UI_DEEP);
        ui_menu_t menu = sample_menu();
        ui_menu_show_volume(&menu, 80);
        ui_overlay_level(view(), &menu, 0);
        place(n++);
    }

    // 9. Setup asked for by voice: confirmed first, because it ends playback.
    panel();
    topbar("语音", UI_MUTED, NULL, 0);
    heard("我要配网", "进入配网会停止播放", UI_AMBER, UI_AMBER);
    rule();
    row(0, true, NULL, UI_ICON_WIFI, "进入配网", NULL, false);
    row(1, false, NULL, UI_ICON_CROSS, "算了", NULL, false);
    bottom_hint("OK确定  双击OK返回");
    place(n++);

    // 10. No server connection (waiting screens, or a server without voice).
    panel();
    topbar("语音", UI_MUTED, NULL, 0);
    mic(14, HEAD_TOP + 6, 2, UI_MUTED);
    ui_text_draw_scaled(view(), 52, HEAD_TOP + 6, "语音现在用不了", 1, UI_VERM, UI_INK);
    ui_text_draw_scaled(view(), 52, HEAD_TOP + 28, "需要连上电视服务器", 1, UI_MUTED, UI_INK);
    rule();
#ifdef UI_RENDER_PASSPORT
    row(0, false, NULL, UI_ICON_WIFI, "配网：双击OK 再长按\xE2\x96\xB2", NULL, true);
#else
    row(0, false, NULL, UI_ICON_WIFI, "配网：双击\xE2\x96\xB2 再长按\xE2\x96\xB2", NULL, true);
#endif
    bottom_hint("任意键关闭");
    place(n++);

    // 11. The backlight page. Xingzhi: it now opens with a triple-click of OK
    //     (OK held is taken by voice). Passport: unchanged, the arrows'
    //     double-click steps the backlight as today.
    picture();
    topbar("亮度", UI_MUTED, NULL, 0);
    ui_draw_panel(whole(), 30, 210);
    ui_overlay_brightness(view(), 50);
#ifdef UI_RENDER_PASSPORT
    bottom_hint("播放时双击\xE2\x96\xB2\xE2\x96\xBC调亮度");
#else
    bottom_hint("三击OK打开本页");
#endif
    place(n++);

    // 12. Recognition failed on the server side.
    panel();
    topbar("语音", UI_MUTED, NULL, 0);
    mic(14, HEAD_TOP + 6, 2, UI_MUTED);
    ui_text_draw_scaled(view(), 52, HEAD_TOP + 6, "没有识别出来", 1, UI_VERM, UI_INK);
    ui_text_draw_scaled(view(), 52, HEAD_TOP + 28, "服务器没回应，稍后再试", 1, UI_MUTED, UI_INK);
    rule();
    bottom_hint("按住OK再说  双击OK返回");
    place(n++);

    FILE *out = fopen(argv[1], "wb");
    if (!out) {
        perror(argv[1]);
        return 1;
    }
    fprintf(out, "P6\n%d %d\n255\n", sheet_w, sheet_h);
    fwrite(sheet, 1, sizeof(sheet), out);
    fclose(out);
    return 0;
}

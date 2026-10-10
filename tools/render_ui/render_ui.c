// Host renderer for the device UI: the firmware's own drawing code, the real
// font, a stand-in for the picture, and each screen of the design laid out on
// one sheet at 2x. Run through render_ui.sh.
#include "ui_draw.h"
#include "ui_menu.h"
#include "ui_overlay.h"
#include "ui_status.h"
#include "qrcodegen.h"

#include <stdio.h>
#include <string.h>

enum { W = 240, H = 240, SCALE = 2, COLS = 4, ROWS = 6, GAP = 8 };

static uint8_t screen[W * H * 2];

static ui_surface_t whole(void)
{
    ui_surface_t surface = { screen, W, 0, H };
    return surface;
}

// Bands of colour where the video would be, so overlays are judged against a
// busy picture rather than a flat one.
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

static void bars(const char *number, const char *name, int soc, int charging, unsigned signal)
{
    ui_surface_t s = whole();
    ui_topbar_label_t label;
    if (number) {
        const char *texts[] = { number, name };
        const uint16_t colors[] = { UI_AMBER, UI_PAPER };
        ui_topbar_set_label(&label, texts, colors, 2);
    } else {
        const char *texts[] = { name };
        const uint16_t colors[] = { UI_MUTED };
        ui_topbar_set_label(&label, texts, colors, 1);
    }
    ui_status_draw(s, W, &label, signal, soc, charging, false);
    ui_text_fill(s, 0, 210, W, 30, UI_DEEP);
}

static ui_menu_t sample_menu(void)
{
    static const char groups[][UI_MENU_GROUP_NAME_MAX] = {
        "我的投屏", "UP 主", "央视", "卫视", "港澳台", "国际", "音乐", "动画影视", "其他" };
    static const ui_menu_entry_t entries[] = {
        { "cast", "我的投屏", 0 }, { "up01", "小Lin说", 1 }, { "up02", "老高与小茉", 1 },
        { "ch1", "CCTV-1 综合", 2 }, { "ch2", "CCTV-2 财经", 2 }, { "ch3", "CCTV-3 综艺", 2 },
        { "ch4", "CCTV-4 中文国际", 2 }, { "ch7", "CCTV-7 国防军事", 2 },
        { "ch8", "CCTV-8 电视剧", 2 }, { "ch9", "CCTV-9 纪录", 2 }, { "ch10", "CCTV-10 科教", 2 },
        { "ch11", "CCTV-11 戏曲", 2 }, { "ch13", "CCTV-13 新闻", 2 },
        { "ch14", "CCTV-14 少儿", 2 }, { "ch15", "CCTV-15 音乐", 2 },
        { "ws1", "湖南卫视", 3 }, { "ws2", "浙江卫视", 3 }, { "hk1", "凤凰中文", 4 },
        { "w1", "CGTN", 5 }, { "m1", "精选音乐", 6 }, { "f1", "猫和老鼠", 7 }, { "o1", "其他台", 8 },
    };
    ui_menu_t menu;
    ui_menu_init(&menu);
    ui_menu_set_groups(&menu, groups, 9);
    ui_menu_load(&menu, entries, sizeof(entries) / sizeof(entries[0]), "ch7");
    return menu;
}

// The sheet, filled scene by scene.
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

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: render_ui out.ppm\n");
        return 2;
    }
    memset(sheet, 0x80, sizeof(sheet));
    int n = 0;
    ui_menu_t menu;

    // 4.4 playing, just switched: the banner.
    picture(); bars("07", " CCTV-7 国防军事", 76, 0, 3);
    menu = sample_menu();
    ui_menu_banner(&menu, "CCTV-7 国防军事", NULL);
    ui_menu_banner_place(&menu, "ch7");
    ui_overlay_banner(whole(), &menu);
    place(n++);

    // 4.4 volume from a key, and muted.
    picture(); bars("07", " CCTV-7 国防军事", 76, 0, 4);
    menu = sample_menu();
    ui_menu_show_volume(&menu, 70);
    ui_overlay_level(whole(), &menu, 0);
    place(n++);
    picture(); bars("07", " CCTV-7 国防军事", 76, 0, 4);
    ui_menu_show_volume(&menu, 0);
    ui_overlay_level(whole(), &menu, 0);
    place(n++);

    // 4.4 backlight from the phone.
    picture(); bars("07", " CCTV-7 国防军事", 76, 0, 4);
    ui_menu_show_brightness(&menu);
    ui_overlay_level(whole(), &menu, 50);
    place(n++);

    // 4.5 the seek box: forward 30 s, back 2 min, next.
    static const struct { ui_hint_t hint; int seconds; } hints[] = {
        { UI_HINT_FORWARD, 30 }, { UI_HINT_BACK, -120 }, { UI_HINT_NEXT, 0 },
    };
    for (unsigned i = 0; i < 3; i++) {
        picture(); bars("12", " 老高与小茉 Mr & Mrs Gao", 76, 0, 3);
        menu = sample_menu();
        ui_menu_show_hint(&menu, hints[i].hint, hints[i].seconds);
        ui_overlay_hint(whole(), &menu);
        place(n++);
    }

    // 4.6 paused, with the bottom bar.
    picture(); bars("12", " 老高与小茉 Mr & Mrs Gao", 76, 0, 3);
    ui_overlay_pause(whole(), 84, 156, 0, "为什么古埃及金字塔里没有法老的木乃伊？");
    ui_overlay_pause_hint(whole(), 210, 240);
    place(n++);

    // 4.1 the top bar's states: low, critical, charging, no signal.
    picture(); bars("07", " CCTV-7 国防军事", 15, 0, 2);
    place(n++);
    picture(); bars("07", " CCTV-7 国防军事", 62, 1, 4);
    place(n++);
    picture(); bars(NULL, "小智电视", -1, 0, 0);
    place(n++);
    picture(); bars("07", " CCTV-7 国防军事", 100, 0, 4);
    place(n++);

    // 4.7 the groups, 4.8 a group's channels, scrolled.
    menu = sample_menu();
    ui_menu_set_rows(&menu, UI_OVERLAY_LIST_ROWS);
    ui_menu_open(&menu);
    ui_menu_scroll_by(&menu, 1, UI_OVERLAY_LIST_ROWS);
    picture(); bars(NULL, "频道分组 4/9", 76, 0, 3);
    ui_draw_panel(whole(), 30, 210);
    ui_overlay_list(whole(), &menu);
    ui_overlay_bottom_rates(whole(), 210, 150, 52);
    place(n++);
    ui_menu_scroll_by(&menu, -1, UI_OVERLAY_LIST_ROWS);
    ui_menu_confirm(&menu);
    for (int i = 0; i < 6; i++) ui_menu_scroll_by(&menu, 1, UI_OVERLAY_LIST_ROWS);
    picture(); bars(NULL, "< 央视 11/12", 76, 0, 3);
    ui_draw_panel(whole(), 30, 210);
    ui_overlay_list(whole(), &menu);
    ui_overlay_bottom_rates(whole(), 210, 150, 52);
    place(n++);

    // 4.9 the backlight page.
    picture(); bars(NULL, "亮度", 76, 0, 3);
    ui_draw_panel(whole(), 30, 210);
    ui_overlay_brightness(whole(), 50);
    place(n++);

    // 4.10 the status page with the remote's code.
    static uint8_t qr[qrcodegen_BUFFER_LEN_FOR_VERSION(3)], tmp[qrcodegen_BUFFER_LEN_FOR_VERSION(3)];
    qrcodegen_encodeText("https://iewww.com/tv/r/abcdef1234", tmp, qr, qrcodegen_Ecc_LOW, 1, 3,
                         qrcodegen_Mask_AUTO, true);
    ui_status_page_t page = { "07", "CCTV-7 国防军事", 3, 76, 0, qr, "手机遥控" };
    picture(); bars(NULL, "状态", 76, 0, 3);
    ui_draw_panel(whole(), 30, 210);
    ui_overlay_status(whole(), &page);
    ui_overlay_bottom_text(whole(), 210, "任意键关闭", UI_FAINT);
    place(n++);

    // 4.2 / 4.3 the waiting screens, and 4.15 the pet picker.
    static const struct { ui_wait_kind_t kind; unsigned pet; const char *line; unsigned number;
                          const char *name; const char *detail; unsigned tick; } waits[] = {
        { UI_WAIT_WIFI, 0, "PHP2.4G", 0, "", "", 7 },
        { UI_WAIT_SERVER, 4, "iewww.com:8098", 0, "", "", 13 },
        { UI_WAIT_CHANNEL, 0, "", 7, "CCTV-7 国防军事", "央视 \xC2\xB7 7/17", 4 },
        { UI_WAIT_CHANNEL, 0, "", 0, "", "", 9 },
        { UI_WAIT_FAILED, 0, "203.0.113.10:8098", 0, "", "", 0 },
        { UI_WAIT_FAILED, 2, "203.0.113.10:8098", 0, "", "", 5 },
    };
    for (unsigned i = 0; i < sizeof(waits) / sizeof(waits[0]); i++) {
        ui_wait_page_t wait = { waits[i].kind, waits[i].pet, "", waits[i].number, "", "" };
        snprintf(wait.line, sizeof(wait.line), "%s", waits[i].line);
        snprintf(wait.name, sizeof(wait.name), "%s", waits[i].name);
        snprintf(wait.detail, sizeof(wait.detail), "%s", waits[i].detail);
        picture(); bars(NULL, ui_overlay_wait_label(wait.kind), 76, 0, 3);
        ui_overlay_wait(whole(), &wait, waits[i].tick);
        place(n++);
    }
    menu = sample_menu();
    ui_menu_open_pets(&menu, 0, ui_pet_count());
    for (unsigned i = 0; i < 2; i++) {
        picture(); bars(NULL, i ? "选择宠物 6/11" : "选择宠物 1/11", 76, 0, 3);
        ui_draw_panel(whole(), 30, 210);
        ui_overlay_pets(whole(), &menu);
        ui_text_fill(whole(), 0, 210, W, 30, UI_DEEP);
        ui_overlay_pet_bottom(whole(), 210);
        place(n++);
        for (int k = 0; k < 5; k++) ui_menu_pet_step(&menu, 1);
    }

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

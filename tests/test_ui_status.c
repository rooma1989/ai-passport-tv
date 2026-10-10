// The top bar, tested without a device.
//
// What matters is what a glance reads: the label in its colours and short of
// the icons, how full the battery looks and whether it turns red, how many
// signal bars are lit, and that an unknown reading does not pretend to be a
// number. Positions are HANDOFF.md 4.1's.
#include "ui_status.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

const uint8_t ui_font_cjk_blob_start[] __asm__("_binary_ui_font_cjk_bin_start") = { 0, 0, 0, 0 };

enum { W = 240, H = 30 };
static uint8_t buf[W * H * 2];
static int failures;

static void check(int condition, const char *what)
{
    if (!condition) {
        printf("FAIL: %s\n", what);
        failures++;
    }
}

static uint16_t pixel(int x, int y)
{
    const uint8_t *p = buf + ((size_t)y * W + (size_t)x) * 2u;
    return (uint16_t)((p[0] << 8) | p[1]);
}

static void draw_label(const ui_topbar_label_t *label, unsigned bars, int soc, int charging)
{
    memset(buf, 0, sizeof(buf));
    ui_surface_t surface = { buf, W, 0, H };
    ui_status_draw(surface, W, label, bars, soc, charging, false);
}

static void draw(unsigned bars, int soc, int charging)
{
    draw_label(NULL, bars, soc, charging);
}

// Pixels of `colour` in columns [x0, x1).
static int count_in(int x0, int x1, uint16_t colour)
{
    int n = 0;
    for (int y = 0; y < H; y++)
        for (int x = x0; x < x1; x++)
            n += pixel(x, y) == colour;
    return n;
}

// The rightmost column in [0, x1) holding anything but the bar's colour.
static int last_ink(int x1)
{
    for (int x = x1 - 1; x >= 0; x--)
        for (int y = 0; y < H; y++)
            if (pixel(x, y) != UI_DEEP) return x;
    return -1;
}

static void test_bar_is_deep(void)
{
    draw(4, 50, 0);
    check(pixel(0, 0) == UI_DEEP && pixel(100, 15) == UI_DEEP && pixel(239, 29) == UI_DEEP,
          "the bar is UI_DEEP edge to edge");
}

static void test_fill(void)
{
    const int bx = 174, bx1 = 190;
    draw(4, 0, 0);
    check(count_in(bx, bx1, UI_PAPER) == 0 && count_in(bx, bx1, UI_VERM) == 0, "0% has no fill");
    check(count_in(bx, bx1, UI_FAINT) > 0, "the frame is UI_FAINT");
    draw(4, 50, 0);
    check(count_in(bx, bx1, UI_PAPER) == 5 * 4, "50% lights five columns");
    draw(4, 76, 0);
    check(count_in(bx, bx1, UI_PAPER) == 8 * 4, "76% rounds to eight columns");
    draw(4, 100, 0);
    check(count_in(bx, bx1, UI_PAPER) == 10 * 4, "100% lights all ten");
    draw(4, 150, 0);
    check(count_in(bx, bx1, UI_PAPER) == 10 * 4, "over 100% is clamped");
    draw(4, 15, 0);
    check(count_in(bx, bx1, UI_PAPER) == 0 && count_in(bx, bx1, UI_VERM) == 2 * 4,
          "below 20% turns vermilion");
    check(count_in(194, W, UI_VERM) > 0, "and so does the number");
    draw(4, -1, 0);
    check(count_in(bx, bx1, UI_PAPER) == 0 && count_in(bx, bx1, UI_VERM) == 0,
          "unknown charge draws no fill");
    check(count_in(194, W, UI_MUTED) > 0 && count_in(194, W, UI_PAPER) == 0,
          "unknown charge reads -- in UI_MUTED");
}

static void test_bars(void)
{
    // Bars 1..4 have 9, 18, 27 and 36 dots.
    draw(0, 80, 0);
    check(count_in(154, 170, UI_PAPER) == 0 && count_in(154, 170, UI_RULE) == 90,
          "not connected: no bar lit");
    draw(2, 80, 0);
    check(count_in(154, 170, UI_PAPER) == 27 && count_in(154, 170, UI_RULE) == 63, "two bars lit");
    draw(4, 80, 0);
    check(count_in(154, 170, UI_PAPER) == 90, "four bars lit");
    draw(9, 80, 0);
    check(count_in(154, 170, UI_PAPER) == 90, "more than four is four");
}

static void test_bolt(void)
{
    draw(4, 60, 1);
    check(count_in(156, 172, UI_AMBER) > 0, "charging draws the bolt");
    check(count_in(134, 150, UI_PAPER) == 90, "and moves the signal left");
    draw(4, 60, 0);
    check(count_in(0, W, UI_AMBER) == 0, "not charging draws none");
    draw(4, 60, -1);
    check(count_in(0, W, UI_AMBER) == 0, "unknown charging draws none");
}

static void test_label(void)
{
    ui_topbar_label_t label;
    const char *texts[] = { "07", " CCTV" };
    const uint16_t colors[] = { UI_AMBER, UI_PAPER };
    ui_topbar_set_label(&label, texts, colors, 2);
    draw_label(&label, 0, -1, 0);
    check(count_in(20, 36, UI_AMBER) > 0, "the number is amber, from x=20");
    check(count_in(0, 20, UI_AMBER) == 0 && count_in(0, 20, UI_PAPER) == 0,
          "nothing left of the label's glyphs");
    check(count_in(36, 100, UI_PAPER) > 0, "the name follows in paper");
    // "07" is 16 px from x=20; the name's drawing must not paint over it.
    check(count_in(28, 36, UI_AMBER) > 0, "the second digit survives the name after it");

    const char *long_texts[] = { "07", " ABCDEFGHIJKLMNOPQRSTUVWXYZ" };
    ui_topbar_set_label(&label, long_texts, colors, 2);
    draw_label(&label, 0, -1, 0);
    check(last_ink(154) <= 148, "a long name stops at x=148");
    check(count_in(20, 36, UI_AMBER) > 0, "and the number is not cut");
    draw_label(&label, 0, -1, 1);
    check(last_ink(134) <= 128, "charging: it stops at x=128");

    // More segments than fit are dropped, not overrun.
    const char *many[] = { "a", "b", "c", "d", "e" };
    ui_topbar_set_label(&label, many, NULL, 5);
    check(label.count == UI_TOPBAR_SEGMENTS, "segments are capped");
}

static void test_critical_blink(void)
{
    memset(buf, 0, sizeof(buf));
    ui_surface_t surface = { buf, W, 0, H };
    ui_status_draw(surface, W, NULL, 4, 5, 0, true);
    check(count_in(194, W, UI_VERM) == 0, "below 10% the number blinks off");
    check(count_in(174, 190, UI_VERM) == 4, "but the fill stays");
    ui_status_draw(surface, W, NULL, 4, 15, 0, true);
    check(count_in(194, W, UI_VERM) > 0, "at 15% it does not blink");
}

static void test_stays_off_the_corner(void)
{
    draw(4, 100, 1);
    for (int y = 0; y < H; y++)
        for (int x = 227; x < W; x++)
            if (pixel(x, y) != UI_DEEP) { check(0, "nothing past x=226, in the rounded corner"); return; }
}

int main(void)
{
    test_bar_is_deep();
    test_fill();
    test_bars();
    test_bolt();
    test_label();
    test_critical_blink();
    test_stays_off_the_corner();
    if (failures) {
        printf("%d failure(s)\n", failures);
        return 1;
    }
    printf("test_ui_status: PASS\n");
    return 0;
}

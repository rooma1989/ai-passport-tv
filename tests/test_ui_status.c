// The top-band status group, tested without a device.
//
// What matters is what a glance reads: how full the battery looks, whether it
// turns red, how many signal bars are lit, and that an unknown reading does not
// pretend to be a number.
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

static void draw(unsigned bars, int soc, int charging)
{
    memset(buf, 0, sizeof(buf));
    ui_surface_t surface = { buf, W, 0, H };
    ui_status_draw(surface, W, H, bars, soc, charging);
}

static int count(uint16_t colour)
{
    int n = 0;
    for (int y = 0; y < H; y++)
        for (int x = 0; x < W; x++)
            n += pixel(x, y) == colour;
    return n;
}

// Pixels of `colour` on one row.
static int count_row(int y, uint16_t colour)
{
    int n = 0;
    for (int x = 0; x < W; x++) n += pixel(x, y) == colour;
    return n;
}

// Lit signal pixels: white on the bars' shared bottom row, left of the battery.
static int lit_bar_pixels(void)
{
    const int row = 20;
    int n = 0;
    for (int x = 0; x < W && pixel(x, row) != UI_COLOR_DIM; x++) n += pixel(x, row) == UI_COLOR_TEXT;
    return n;
}

static void test_fill(void)
{
    draw(4, 0, 0);
    check(count(UI_COLOR_GOOD) == 0 && count(UI_COLOR_LOW) == 0, "0% has no fill");
    draw(4, 50, 0);
    check(count_row(15, UI_COLOR_GOOD) == 9, "50% fills half of the 18 px inside");
    draw(4, 100, 0);
    check(count_row(15, UI_COLOR_GOOD) == 18, "100% fills the inside");
    draw(4, 150, 0);
    check(count_row(15, UI_COLOR_GOOD) == 18, "over 100% is clamped");
    draw(4, 15, 0);
    check(count(UI_COLOR_GOOD) == 0 && count_row(15, UI_COLOR_LOW) == 2, "below 20% turns red");
    draw(4, -1, 0);
    check(count(UI_COLOR_GOOD) == 0 && count(UI_COLOR_LOW) == 0, "unknown charge draws no fill");
    check(count(UI_COLOR_DIM) > 0, "the outline is still drawn when unknown");
}

static void test_bars(void)
{
    draw(0, 80, 0);
    check(lit_bar_pixels() == 0, "not connected: no bar lit");
    draw(2, 80, 0);
    check(lit_bar_pixels() == 6, "two bars lit");
    draw(4, 80, 0);
    check(lit_bar_pixels() == 12, "four bars lit");
    draw(9, 80, 0);
    check(lit_bar_pixels() == 12, "more than four is four");
}

static void test_bolt(void)
{
    draw(4, 60, 1);
    check(count(UI_COLOR_BOLT) > 0, "charging draws the bolt");
    draw(4, 60, 0);
    check(count(UI_COLOR_BOLT) == 0, "not charging draws none");
    draw(4, 60, -1);
    check(count(UI_COLOR_BOLT) == 0, "unknown charging draws none");
}

static void test_stays_in_band_and_off_the_edge(void)
{
    draw(4, 100, 1);
    for (int y = 0; y < H; y++)
        for (int x = W - UI_STATUS_EDGE + UI_TEXT_PAD_X; x < W; x++)
            if (pixel(x, y) != 0) { check(0, "nothing in the rounded corner"); return; }
}

int main(void)
{
    test_fill();
    test_bars();
    test_bolt();
    test_stays_in_band_and_off_the_edge();
    if (failures) {
        printf("%d failure(s)\n", failures);
        return 1;
    }
    printf("test_ui_status: PASS\n");
    return 0;
}

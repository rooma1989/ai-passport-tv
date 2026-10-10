// The design's pixel grammar and sprites, tested without a device: icons and
// pets land where asked at the scale asked, transparent dots stay transparent,
// and everything clips to the stripe it is drawn into.
#include "ui_draw.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

const uint8_t ui_font_cjk_blob_start[] __asm__("_binary_ui_font_cjk_bin_start") = { 0, 0, 0, 0 };

enum { W = 240, H = 240 };
static uint8_t buf[W * H * 2];
static int failures;

#define BACK 0x1234u

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

static ui_surface_t clear(void)
{
    ui_surface_t surface = { buf, W, 0, H };
    ui_text_fill(surface, 0, 0, W, H, BACK);
    return surface;
}

static int count(int x0, int y0, int x1, int y1, uint16_t colour)
{
    int n = 0;
    for (int y = y0; y < y1; y++)
        for (int x = x0; x < x1; x++)
            n += pixel(x, y) == colour;
    return n;
}

static void test_icon(void)
{
    ui_surface_t surface = clear();
    ui_draw_icon(surface, UI_ICON_PAUSE, 10, 20, 1, UI_AMBER);
    // Two 4-wide bars, rows 2..13: 0x1E78 per row.
    check(count(10, 20, 26, 36, UI_AMBER) == 8 * 12, "pause icon has 96 dots");
    check(pixel(10 + 3, 20 + 2) == UI_AMBER && pixel(10 + 2, 20 + 2) == BACK, "bit 15 is the left column");
    check(count(0, 0, W, H, UI_AMBER) == 96, "nothing outside the icon");

    surface = clear();
    ui_draw_icon(surface, UI_ICON_PAUSE, 10, 20, 2, UI_AMBER);
    check(count(0, 0, W, H, UI_AMBER) == 96 * 4, "2x has four times the dots");
    check(pixel(10 + 6, 20 + 4) == UI_AMBER && pixel(10 + 7, 20 + 5) == UI_AMBER, "a dot is a 2x2 block");

    surface = clear();
    ui_draw_icon_columns(surface, UI_ICON_BATTERY_FILL, 0, 0, 1, UI_PAPER, 2, 3);
    check(count(0, 0, W, H, UI_PAPER) == 3 * 4, "three fill columns light 12 dots");
    check(count(2, 0, 5, 16, UI_PAPER) == 12, "starting at column 2");

    surface = clear();
    ui_draw_icon(surface, UI_ICON_COUNT, 0, 0, 1, UI_PAPER);
    check(count(0, 0, W, H, UI_PAPER) == 0, "an unknown icon draws nothing");
}

static void test_pet(void)
{
    check(ui_pet_count() == 11, "eleven pets");
    check(ui_pet_name(0) && strcmp(ui_pet_name(0), "电电") == 0, "pet 0 is 电电");
    check(ui_pet_name(11) == NULL, "past the last pet there is no name");
    check(ui_pet_moods(0) == 4 && ui_pet_moods(1) == 3, "only 电电 has a fourth mood");

    ui_surface_t surface = clear();
    ui_draw_pet(surface, 0, 0, 96, 50, 2);
    const int inside = W * H - count(0, 0, W, H, BACK);
    check(inside > 0, "a 2x pet draws");
    check(count(0, 0, 96, H, BACK) == 96 * H && count(144, 0, W, H, BACK) == (W - 144) * H &&
          count(0, 0, W, 50, BACK) == W * 50 && count(0, 98, W, H, BACK) == W * (H - 98),
          "inside its 48x48 square");
    check(count(96, 50, 144, 98, BACK) > 0, "transparent dots leave the background");
    // Every lit 2x block is uniform: the scale is applied to whole dots.
    int uneven = 0;
    for (int y = 50; y < 98; y += 2)
        for (int x = 96; x < 144; x += 2) {
            uint16_t v = pixel(x, y);
            uneven += pixel(x + 1, y) != v || pixel(x, y + 1) != v || pixel(x + 1, y + 1) != v;
        }
    check(uneven == 0, "each dot is a 2x2 block");

    // Mood past the last falls back to the last.
    surface = clear();
    ui_draw_pet(surface, 1, 2, 0, 0, 1);
    uint8_t mood2[sizeof(buf)];
    memcpy(mood2, buf, sizeof(buf));
    surface = clear();
    ui_draw_pet(surface, 1, 3, 0, 0, 1);
    check(memcmp(mood2, buf, sizeof(buf)) == 0, "a missing mood 3 draws mood 2");

    // A 3x pet drawn a stripe at a time matches one drawn whole.
    surface = clear();
    ui_draw_pet(surface, 4, 1, 84, 38, 3);
    static uint8_t whole[sizeof(buf)];
    memcpy(whole, buf, sizeof(buf));
    (void)clear();
    for (int y = 0; y < H; y += 12) {
        ui_surface_t stripe = { buf + (size_t)y * W * 2, W, y, 12 };
        ui_draw_pet(stripe, 4, 1, 84, 38, 3);
    }
    check(memcmp(whole, buf, sizeof(buf)) == 0, "3x by stripes equals 3x whole");

    int top = -1, bottom = -1;
    check(ui_pet_rows(0, 1, &top, &bottom) && top == 0 && bottom == 20, "电电 fills rows 0..20");
    check(ui_pet_rows(6, 1, &top, &bottom) && top == 5 && bottom == 18, "按按 fills rows 5..18");
    check(!ui_pet_rows(99, 0, &top, &bottom), "no rows for an unknown pet");

    surface = clear();
    ui_draw_pet(surface, 99, 0, 0, 0, 2);
    check(count(0, 0, W, H, BACK) == W * H, "an unknown pet draws nothing");
}

static void test_grammar(void)
{
    ui_surface_t surface = clear();
    ui_draw_dotted_hline(surface, 16, 223, 63, UI_RULE);
    check(count(0, 63, W, 64, UI_RULE) == 104, "x=16..223 is 52 on-pairs");
    check(pixel(16, 63) == UI_RULE && pixel(17, 63) == UI_RULE && pixel(18, 63) == BACK &&
          pixel(19, 63) == BACK && pixel(20, 63) == UI_RULE, "two on, two off");
    check(count(0, 0, W, 63, UI_RULE) == 0 && count(0, 64, W, H, UI_RULE) == 0, "one row only");

    surface = clear();
    ui_draw_notched_rect(surface, 60, 92, 120, 56, UI_VERM);
    check(pixel(60, 92) == BACK && pixel(61, 92) == BACK && pixel(62, 92) == UI_VERM, "row 0 inset 2");
    check(pixel(60, 93) == BACK && pixel(61, 93) == UI_VERM, "row 1 inset 1");
    check(pixel(60, 94) == UI_VERM && pixel(179, 94) == UI_VERM, "full width from row 2");
    check(pixel(179, 147) == BACK && pixel(177, 147) == UI_VERM && pixel(178, 146) == UI_VERM &&
          pixel(179, 146) == BACK, "bottom corners mirror the top");
    check(count(0, 0, W, H, UI_VERM) == 120 * 56 - 2 * (2 + 2 + 1 + 1), "six corner dots each side cut");

    surface = clear();
    ui_draw_cells(surface, 62, 6, 7, UI_AMBER, UI_RULE);
    check(count(0, 62, W, 68, UI_AMBER) == 7 * 19 * 6 && count(0, 62, W, 68, UI_RULE) == 3 * 19 * 6,
          "seven lit cells, three dim");
    check(pixel(16, 62) == UI_AMBER && pixel(34, 62) == UI_AMBER && pixel(35, 62) == BACK &&
          pixel(37, 62) == UI_AMBER && pixel(205, 62) == UI_RULE && pixel(224, 62) == BACK,
          "cell k at 16+21k, 19 wide");
    surface = clear();
    ui_draw_cells(surface, 62, 6, 15, UI_AMBER, UI_RULE);
    check(count(0, 62, W, 68, UI_AMBER) == 10 * 19 * 6, "more than ten lights ten");

    surface = clear();
    ui_draw_panel(surface, 30, 210);
    check(count(0, 30, W, 210, UI_INK) == W * 180 && count(0, 0, W, 30, UI_INK) == 0 &&
          count(0, 210, W, H, UI_INK) == 0, "the panel covers y=30..209 only");
}

int main(void)
{
    test_icon();
    test_pet();
    test_grammar();
    if (failures) {
        printf("%d failure(s)\n", failures);
        return 1;
    }
    printf("test_ui_draw: PASS\n");
    return 0;
}

// Pure arithmetic from the Xingzhi board BSP, tested without a device.
//
// The volume curve and the 16-to-32-bit scaling decide whether the speaker is
// silent, clipped or wrapped around; the battery table decides what the status
// view prints. All three are easiest to get wrong at the ends.
#include "bsp_xz_math.h"

#include <stdint.h>
#include <stdio.h>

static int failures;

static void check(int condition, const char *what)
{
    if (!condition) {
        printf("FAIL: %s\n", what);
        failures++;
    }
}

static void test_volume_curve(void)
{
    check(bsp_xz_volume_q16(0) == 0u, "0% is silent");
    check(bsp_xz_volume_q16(100) == 65536u, "100% is unity gain");
    check(bsp_xz_volume_q16(50) == 16384u, "50% is a quarter on the square curve");
    check(bsp_xz_volume_q16(200) == 65536u, "values above 100% are clamped");
}

static void test_scale_extremes_fit_int32(void)
{
    const int16_t in[] = { 32767, -32768, 0, 1 };
    int32_t out[4];

    bsp_xz_scale_s16_to_s32(in, out, 4, 65536u);
    check(out[0] == 2147418112, "full positive scale fills the top 16 bits");
    check(out[1] == INT32_MIN, "full negative scale is exactly INT32_MIN");
    check(out[2] == 0, "silence stays silent");
    check(out[3] == 65536, "one LSB moves to bit 16");

    bsp_xz_scale_s16_to_s32(in, out, 4, 1u << 20);
    check(out[0] == 2147418112, "gain above unity is clamped, no wrap-around");

    bsp_xz_scale_s16_to_s32(in, out, 4, 0u);
    check(out[0] == 0 && out[1] == 0, "zero gain mutes");
}

static void test_battery_table(void)
{
    check(bsp_xz_battery_percent(1500) == 0, "below the table is empty");
    check(bsp_xz_battery_percent(1970) == 0, "first point is 0%");
    check(bsp_xz_battery_percent(2000) == 6, "interpolates inside the first segment");
    check(bsp_xz_battery_percent(2062) == 20, "a table point is exact");
    check(bsp_xz_battery_percent(2108) == 30, "midpoint of a segment");
    check(bsp_xz_battery_percent(2430) == 100, "last point is full");
    check(bsp_xz_battery_percent(4095) == 100, "above the table is full");
}

int main(void)
{
    test_volume_curve();
    test_scale_extremes_fit_int32();
    test_battery_table();
    if (failures) {
        printf("%d failure(s)\n", failures);
        return 1;
    }
    printf("test_bsp_xz_math: PASS\n");
    return 0;
}

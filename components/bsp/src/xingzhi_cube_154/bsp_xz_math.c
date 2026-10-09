// components/bsp/src/xingzhi_cube_154/bsp_xz_math.c
#include "bsp_xz_math.h"

uint32_t bsp_xz_volume_q16(uint8_t percent)
{
    if (percent > 100) percent = 100;
    return (uint32_t)percent * percent * 65536u / 10000u;
}

uint32_t bsp_xz_gain_q16(uint8_t percent, uint32_t max_q16)
{
    if (max_q16 > 65536u) max_q16 = 65536u;
    return (uint32_t)(((uint64_t)bsp_xz_volume_q16(percent) * max_q16) >> 16);
}

void bsp_xz_scale_s16_to_s32(const int16_t *in, int32_t *out, size_t n, uint32_t gain_q16)
{
    if (gain_q16 > 65536u) gain_q16 = 65536u;
    for (size_t i = 0; i < n; i++) {
        // 最大幅度 -32768*65536 恰好是 INT32_MIN,用 64 位乘再收窄不会溢出。
        out[i] = (int32_t)((int64_t)in[i] * (int64_t)gain_q16);
    }
}

// 小智固件 xingzhi-cube-1.54tft power_manager.h 的对照表(ADC2_CH6 原始值)。
static const struct {
    int raw;
    int percent;
} LEVELS[] = {
    {1970, 0}, {2062, 20}, {2154, 40}, {2246, 60}, {2338, 80}, {2430, 100},
};

int bsp_xz_battery_percent(int raw)
{
    const size_t n = sizeof(LEVELS) / sizeof(LEVELS[0]);
    if (raw <= LEVELS[0].raw) return 0;
    if (raw >= LEVELS[n - 1].raw) return 100;
    for (size_t i = 0; i + 1 < n; i++) {
        if (raw < LEVELS[i + 1].raw) {
            int span = LEVELS[i + 1].raw - LEVELS[i].raw;
            int step = LEVELS[i + 1].percent - LEVELS[i].percent;
            return LEVELS[i].percent + (raw - LEVELS[i].raw) * step / span;
        }
    }
    return 100;
}

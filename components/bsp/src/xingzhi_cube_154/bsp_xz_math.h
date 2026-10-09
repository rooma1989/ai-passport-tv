// components/bsp/src/xingzhi_cube_154/bsp_xz_math.h
// 星智板 BSP 里的纯计算:音量曲线、16→32 位样本放大、电池 ADC→百分比。
// 不依赖 ESP-IDF,主机测试 tests/test_bsp_xz_math.c 直接编译它。
#pragma once

#include <stddef.h>
#include <stdint.h>

// 音量百分比 → Q16 增益(0..65536)。平方曲线:线性刻度的上半段几乎听不出差别。
// 与小智固件 NoAudioCodec 的做法一致。超过 100 按 100 算。
uint32_t bsp_xz_volume_q16(uint8_t percent);

// 音量百分比 → 实际增益:音量曲线再乘一个上限(Q16,超过 65536 按 65536 算)。
// 上限存在的理由见 boards/xingzhi_cube_154.h 的 BSP_AUDIO_MAX_GAIN_Q16。
uint32_t bsp_xz_gain_q16(uint8_t percent, uint32_t max_q16);

// 16 位 PCM 乘 Q16 增益,写成 32 位 I2S 样本(功放取高位)。增益超过 65536 按 65536 算。
void bsp_xz_scale_s16_to_s32(const int16_t *in, int32_t *out, size_t n, uint32_t gain_q16);

// 电池 ADC 原始均值(12 位、12 dB 衰减)→ 0..100%,分段线性,两端截断。
int bsp_xz_battery_percent(int raw);

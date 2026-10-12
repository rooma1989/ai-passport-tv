// components/bsp/src/passport/bsp_audio_priv.h
// bsp_audio.c 与 bsp_mic.c 之间的内部接口:麦克风读的是播放用的同一个 I2S 全双工端口。
#pragma once

#include "driver/i2s_std.h"
#include <stdbool.h>
#include <stdint.h>

// 接收通道;codec 没打开或格式不是 hz/16 位/单声道时返回 NULL。
i2s_chan_handle_t bsp_audio_rx_if_open(uint32_t hz);

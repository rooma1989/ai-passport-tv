// components/bsp/include/bsp_mic.h
// 麦克风,给语音按住说话用:16 kHz、16 位、单声道,一块块读。两块板各一份实现:
//   Passport(ES8311):读已经开着的 I2S0 全双工的接收通道(左声道)。播放会话的 audio_task
//     已经以 16 kHz/16 位/单声道打开了 codec,这里不重开 codec,电视声音不会断。没有打开时
//     bsp_mic_start() 返回 ESP_ERR_INVALID_STATE。
//   星智(I2S MEMS 麦克风,WS 4/SCK 5/DIN 6):第一次 start 时建 I2S1 接收通道,stop 时关掉。
//     格式照小智固件的板级配置(标准 I2S、32 位槽、左声道),右移后截成 16 位 ——【未在本板实测】。
// 三个函数都只能在同一个任务里调(语音任务),不能在回调里调。
#pragma once

#include "esp_err.h"
#include <stddef.h>
#include <stdint.h>

esp_err_t bsp_mic_start(uint32_t hz);
// 读 `samples` 个采样到 pcm,最多等 timeout_ms。返回读到的采样数;出错返回负数。
int bsp_mic_read(int16_t *pcm, size_t samples, uint32_t timeout_ms);
void bsp_mic_stop(void);

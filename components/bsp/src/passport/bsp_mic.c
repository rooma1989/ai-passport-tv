// components/bsp/src/passport/bsp_mic.c -- 见 bsp_mic.h。
// ES8311 的 ADC 早就跟着播放一起开着(ESP_CODEC_DEV_WORK_MODE_BOTH、no_dac_ref=true、
// 输入增益 30 dB,见 bsp_audio.c),接收通道也一直在跑,只是没人读。这里只是开始读它。
#include "bsp_mic.h"
#include "bsp_audio_priv.h"
#include "esp_log.h"

static const char *TAG = "bsp_mic";

static i2s_chan_handle_t s_rx;

esp_err_t bsp_mic_start(uint32_t hz) {
    s_rx = bsp_audio_rx_if_open(hz);
    if (!s_rx) {
        ESP_LOGW(TAG, "codec 没有以 %luHz/16bit/mono 打开,不能录音", (unsigned long)hz);
        return ESP_ERR_INVALID_STATE;
    }
    // 没人读的这段时间里,DMA 里存着按下之前的约 90 ms(6 x 240 帧),那是电视的声音。
    // 丢掉,录音从按下这一刻算起。
    int16_t stale[240];
    for (int i = 0; i < 8; i++) {
        size_t got = 0;
        if (i2s_channel_read(s_rx, stale, sizeof(stale), &got, 0) != ESP_OK || !got) break;
    }
    return ESP_OK;
}

int bsp_mic_read(int16_t *pcm, size_t samples, uint32_t timeout_ms) {
    if (!s_rx || !pcm || !samples) return -1;
    size_t got = 0;
    esp_err_t e = i2s_channel_read(s_rx, pcm, samples * sizeof(int16_t), &got, timeout_ms);
    if (e != ESP_OK && e != ESP_ERR_TIMEOUT) return -1;
    return (int)(got / sizeof(int16_t));
}

void bsp_mic_stop(void) {
    // 通道属于播放,不关;只是不再读。
    s_rx = NULL;
}

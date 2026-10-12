// components/bsp/src/xingzhi_cube_154/bsp_mic.c -- 见 bsp_mic.h。
// 星智板的麦克风是单独的 I2S MEMS 麦克风,接在 I2S1(WS 4 / SCK 5 / DIN 6,见板级头文件)。
// 格式照 github.com/78/xiaozhi-esp32 的 NoAudioCodecSimplex(本板在小智固件里的配置):
// 标准(Philips)I2S、32 位数据、单声道、左声道、16 kHz;读出的 32 位样本右移 BSP_MIC_SHIFT
// 后截成 16 位。【这套格式和增益没有在本板上实测过】,上机先看 av_voice 打的电平日志。
#include "bsp_mic.h"
#include "bsp_pins.h"
#include "driver/i2s_std.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include <stdlib.h>

static const char *TAG = "bsp_mic";

// 一次最多换算的样本数;多出来的分几次读。
#define CHUNK 320

static i2s_chan_handle_t s_rx;
static bool              s_enabled;
static uint32_t          s_hz;
static int32_t          *s_raw;

esp_err_t bsp_mic_start(uint32_t hz) {
    esp_err_t e;
    if (!s_rx) {
        i2s_chan_config_t chan = I2S_CHANNEL_DEFAULT_CONFIG(BSP_MIC_I2S_PORT, I2S_ROLE_MASTER);
        chan.dma_desc_num = 6;
        chan.dma_frame_num = 240;
        chan.auto_clear_after_cb = true;
        if ((e = i2s_new_channel(&chan, NULL, &s_rx)) != ESP_OK) {
            ESP_LOGE(TAG, "I2S%d 接收通道建立失败: %s", (int)BSP_MIC_I2S_PORT, esp_err_to_name(e));
            s_rx = NULL;
            return e;
        }
        i2s_std_config_t std = {
            .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(hz),
            .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO),
            .gpio_cfg = {
                .mclk = I2S_GPIO_UNUSED, .bclk = BSP_MIC_SCK, .ws = BSP_MIC_WS,
                .dout = I2S_GPIO_UNUSED, .din = BSP_MIC_DIN,
                .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
            },
        };
        std.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;
        if ((e = i2s_channel_init_std_mode(s_rx, &std)) != ESP_OK) {
            ESP_LOGE(TAG, "I2S%d 接收通道配置失败: %s", (int)BSP_MIC_I2S_PORT, esp_err_to_name(e));
            i2s_del_channel(s_rx);
            s_rx = NULL;
            return e;
        }
        s_hz = hz;
        ESP_LOGI(TAG, "麦克风 I2S%d WS=%d SCK=%d DIN=%d %luHz 32bit 左声道 >>%d(未实测)",
                 (int)BSP_MIC_I2S_PORT, BSP_MIC_WS, BSP_MIC_SCK, BSP_MIC_DIN, (unsigned long)hz,
                 BSP_MIC_SHIFT);
    } else if (hz != s_hz) {
        i2s_std_clk_config_t clk = I2S_STD_CLK_DEFAULT_CONFIG(hz);
        if ((e = i2s_channel_reconfig_std_clock(s_rx, &clk)) != ESP_OK) return e;
        s_hz = hz;
    }
    if (!s_raw) {
        s_raw = heap_caps_malloc(CHUNK * sizeof(int32_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
        if (!s_raw) return ESP_ERR_NO_MEM;
    }
    if (!s_enabled) {
        if ((e = i2s_channel_enable(s_rx)) != ESP_OK) return e;
        s_enabled = true;
    }
    return ESP_OK;
}

int bsp_mic_read(int16_t *pcm, size_t samples, uint32_t timeout_ms) {
    if (!s_enabled || !s_raw || !pcm) return -1;
    size_t done = 0;
    while (done < samples) {
        size_t n = samples - done;
        if (n > CHUNK) n = CHUNK;
        size_t got = 0;
        esp_err_t e = i2s_channel_read(s_rx, s_raw, n * sizeof(int32_t), &got, timeout_ms);
        if (e != ESP_OK && e != ESP_ERR_TIMEOUT) return -1;
        const size_t m = got / sizeof(int32_t);
        for (size_t i = 0; i < m; i++) {
            int32_t v = s_raw[i] >> BSP_MIC_SHIFT;
            pcm[done + i] = (int16_t)(v > INT16_MAX ? INT16_MAX : v < -INT16_MAX ? -INT16_MAX : v);
        }
        done += m;
        if (m < n) break;   // 超时:读到多少给多少
    }
    return (int)done;
}

void bsp_mic_stop(void) {
    if (s_enabled) {
        i2s_channel_disable(s_rx);
        s_enabled = false;
    }
    free(s_raw);
    s_raw = NULL;
}

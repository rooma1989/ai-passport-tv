// components/bsp/src/xingzhi_cube_154/bsp_audio.c
// 星智板:I2S 直连数字功放(无 codec、无 I2C)。只做播放,录音不支持。
// 没有硬件音量:音量/静音在送进 I2S 前用软件乘上去(见 bsp_xz_math.c),
// 样本按小智固件的做法放大成 32 位单声道左声道。
#include "bsp_audio.h"
#include "bsp_pins.h"
#include "bsp_xz_math.h"
#include "driver/i2s_std.h"
#include "esp_log.h"

static const char *TAG = "bsp_audio";

// bsp_audio_write() 没有调用方给的超时,用一个够长的有限值,避免 DMA 卡死时永远阻塞。
#define WRITE_TIMEOUT_MS 1000
// 一次转换的样本数。32 位输出是输入两倍大,分块转换免得为整包再开内存。
#define CHUNK_SAMPLES 240

static i2s_chan_handle_t s_tx;
static bool              s_configured, s_enabled;
static uint32_t          s_hz;
static volatile uint8_t  s_volume = 70;
static volatile bool     s_mute;
static int32_t           s_out[CHUNK_SAMPLES];

esp_err_t bsp_audio_init(void) {
    if (s_tx) return ESP_OK;
    i2s_chan_config_t chan = I2S_CHANNEL_DEFAULT_CONFIG(BSP_I2S_PORT, I2S_ROLE_MASTER);
    chan.dma_desc_num = 6;
    chan.dma_frame_num = 240;
    chan.auto_clear_after_cb = true;
    esp_err_t e = i2s_new_channel(&chan, &s_tx, NULL);
    if (e != ESP_OK) { ESP_LOGE(TAG, "i2s_new_channel 失败: %s", esp_err_to_name(e)); s_tx = NULL; return e; }
    ESP_LOGI(TAG, "I2S 功放就绪 BCLK=%d WS=%d DOUT=%d", BSP_I2S_BCLK, BSP_I2S_WS, BSP_I2S_DOUT);
    return ESP_OK;
}

esp_err_t bsp_audio_set_format(uint32_t hz, uint8_t bits, uint8_t ch) {
    if (!s_tx) return ESP_ERR_INVALID_STATE;
    if (bits != 16 || ch != 1) {
        ESP_LOGE(TAG, "只支持 16bit 单声道,收到 %ubit/%uch", bits, ch);
        return ESP_ERR_NOT_SUPPORTED;
    }
    if (s_enabled && s_hz == hz) return ESP_OK;
    if (s_enabled) { i2s_channel_disable(s_tx); s_enabled = false; }

    esp_err_t e;
    if (!s_configured) {
        i2s_std_config_t std = {
            .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(hz),
            .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO),
            .gpio_cfg = {
                .mclk = I2S_GPIO_UNUSED, .bclk = BSP_I2S_BCLK, .ws = BSP_I2S_WS,
                .dout = BSP_I2S_DOUT, .din = I2S_GPIO_UNUSED,
                .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
            },
        };
        std.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;
        e = i2s_channel_init_std_mode(s_tx, &std);
        if (e == ESP_OK) s_configured = true;
    } else {
        i2s_std_clk_config_t clk = I2S_STD_CLK_DEFAULT_CONFIG(hz);
        e = i2s_channel_reconfig_std_clock(s_tx, &clk);
    }
    if (e == ESP_OK) e = i2s_channel_enable(s_tx);
    if (e != ESP_OK) { ESP_LOGE(TAG, "I2S 配置 %luHz 失败: %s", (unsigned long)hz, esp_err_to_name(e)); return e; }
    s_enabled = true; s_hz = hz;
    ESP_LOGI(TAG, "I2S 打开 %luHz/16bit/mono", (unsigned long)hz);
    return ESP_OK;
}

// written 报告的是【输入】字节数(16 位),与 Passport 的约定一致:提交量,不是已播放量。
static esp_err_t write_scaled(const void *pcm, size_t bytes, size_t *written, uint32_t timeout_ms) {
    const int16_t *in = pcm;
    const size_t samples = bytes / 2;
    const uint32_t gain = s_mute ? 0u : bsp_xz_gain_q16(s_volume, BSP_AUDIO_MAX_GAIN_Q16);
    size_t done = 0;
    while (done < samples) {
        size_t n = samples - done;
        if (n > CHUNK_SAMPLES) n = CHUNK_SAMPLES;
        bsp_xz_scale_s16_to_s32(in + done, s_out, n, gain);
        size_t w = 0;
        esp_err_t e = i2s_channel_write(s_tx, s_out, n * sizeof(int32_t), &w, timeout_ms);
        done += w / sizeof(int32_t);
        *written = done * 2;
        if (e != ESP_OK) return e;
        if (w < n * sizeof(int32_t)) return ESP_ERR_TIMEOUT;
    }
    return ESP_OK;
}

esp_err_t bsp_audio_write(const void *pcm, size_t bytes) {
    if (!s_enabled) return ESP_ERR_INVALID_STATE;
    if (!pcm || bytes % 2) return ESP_ERR_INVALID_ARG;
    size_t written = 0;
    return write_scaled(pcm, bytes, &written, WRITE_TIMEOUT_MS);
}

esp_err_t bsp_audio_write_timeout(const void *pcm, size_t bytes, size_t *written, uint32_t timeout_ms) {
    if (!written) return ESP_ERR_INVALID_ARG;
    *written = 0;
    if (!s_enabled) return ESP_ERR_INVALID_STATE;
    if (!pcm || !bytes || bytes % 2) return ESP_ERR_INVALID_ARG;
    return write_scaled(pcm, bytes, written, timeout_ms);
}

esp_err_t bsp_audio_stream_close(void) {
    if (!s_enabled) return ESP_OK;
    s_enabled = false;
    return i2s_channel_disable(s_tx);
}

esp_err_t bsp_audio_read(void *pcm, size_t bytes) {
    (void)pcm; (void)bytes;
    return ESP_ERR_NOT_SUPPORTED;
}

void bsp_audio_set_mute(bool mute) { s_mute = mute; }

void bsp_audio_set_volume(uint8_t percent) { s_volume = percent > 100 ? 100 : percent; }

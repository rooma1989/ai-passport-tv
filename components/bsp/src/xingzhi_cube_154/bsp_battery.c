// components/bsp/src/xingzhi_cube_154/bsp_battery.c
// 星智板:电池经分压接 ADC2_CH6(GPIO17),按小智固件的对照表换算百分比。
// 没有电量计芯片,百分比只是电压的粗略映射,充电时会偏高。
#include "bsp_battery.h"
#include "bsp_pins.h"
#include "bsp_xz_math.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_log.h"

static const char *TAG = "bsp_batt";

#define SAMPLES 8

static adc_oneshot_unit_handle_t s_adc;
static adc_cali_handle_t         s_cali;

esp_err_t bsp_battery_init(void) {
    if (s_adc) return ESP_OK;
    const adc_oneshot_unit_init_cfg_t ucfg = { .unit_id = BSP_BATT_ADC_UNIT, .ulp_mode = ADC_ULP_MODE_DISABLE };
    esp_err_t e = adc_oneshot_new_unit(&ucfg, &s_adc);
    if (e != ESP_OK) { ESP_LOGE(TAG, "ADC unit 创建失败: %s", esp_err_to_name(e)); s_adc = NULL; return e; }
    const adc_oneshot_chan_cfg_t ccfg = { .atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_12 };
    e = adc_oneshot_config_channel(s_adc, BSP_BATT_ADC_CHANNEL, &ccfg);
    if (e != ESP_OK) {
        ESP_LOGE(TAG, "ADC 通道配置失败: %s", esp_err_to_name(e));
        adc_oneshot_del_unit(s_adc); s_adc = NULL;
        return e;
    }
    const adc_cali_curve_fitting_config_t cal = {
        .unit_id = BSP_BATT_ADC_UNIT, .chan = BSP_BATT_ADC_CHANNEL,
        .atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_12,
    };
    if (adc_cali_create_scheme_curve_fitting(&cal, &s_cali) != ESP_OK) {
        ESP_LOGW(TAG, "ADC 校准不可用,bsp_battery_mv() 将返回 -1");
        s_cali = NULL;
    }
    ESP_LOGI(TAG, "电量 ADC 就绪");
    return ESP_OK;
}

static int read_raw_avg(void) {
    if (!s_adc) return -1;
    int sum = 0, got = 0;
    for (int i = 0; i < SAMPLES; i++) {
        int raw;
        // ADC2 与 Wi-Fi 共用仲裁器,偶尔被占用而读失败,跳过那一次即可。
        if (adc_oneshot_read(s_adc, BSP_BATT_ADC_CHANNEL, &raw) == ESP_OK) { sum += raw; got++; }
    }
    return got ? sum / got : -1;
}

int bsp_battery_soc(void) {
    int raw = read_raw_avg();
    return raw < 0 ? -1 : bsp_xz_battery_percent(raw);
}

int bsp_battery_mv(void) {
    int raw = read_raw_avg(), mv = 0;
    if (raw < 0 || !s_cali) return -1;
    if (adc_cali_raw_to_voltage(s_cali, raw, &mv) != ESP_OK) return -1;
    return mv * BSP_BATT_DIVIDER;
}

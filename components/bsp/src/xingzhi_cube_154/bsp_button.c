// components/bsp/src/xingzhi_cube_154/bsp_button.c
// 星智板:三个独立 GPIO 按键,低电平有效。事件种类与 Passport 的 ADC 三键完全一致。
#include "bsp_button.h"
#include "bsp_pins.h"
#include "iot_button.h"
#include "bsp_button_events.h"
#include "button_gpio.h"
#include "driver/gpio.h"
#include "esp_log.h"

static const char *TAG = "bsp_btn";

static const int BTN_GPIO[BSP_BTN_COUNT] = { BSP_BTN_GPIO_UP, BSP_BTN_GPIO_DOWN, BSP_BTN_GPIO_OK };

static button_handle_t s_btn[BSP_BTN_COUNT];

esp_err_t bsp_button_init(bsp_btn_cb_t cb, void *user) {
    bsp_button_events_init(cb, user);
    for (int i = 0; i < BSP_BTN_COUNT; i++) {
        const button_gpio_config_t gc = { .gpio_num = BTN_GPIO[i], .active_level = 0 };
        const button_config_t bc = { 0 };
        esp_err_t e = iot_button_new_gpio_device(&bc, &gc, &s_btn[i]);
        if (e != ESP_OK || !s_btn[i]) {
            ESP_LOGE(TAG, "按键 %d (GPIO%d) 创建失败: %s", i, BTN_GPIO[i], esp_err_to_name(e));
            return e == ESP_OK ? ESP_FAIL : e;
        }
        // 事件怎么报,两块板一样,见 bsp_button_events.c。
        if ((e = bsp_button_events_attach(s_btn[i], (bsp_btn_t)i)) != ESP_OK) return e;
    }
    ESP_LOGI(TAG, "按键就绪:上=GPIO%d 下=GPIO%d 确定=GPIO%d",
             BSP_BTN_GPIO_UP, BSP_BTN_GPIO_DOWN, BSP_BTN_GPIO_OK);
    return ESP_OK;
}

// 按下接地:低电平是按着。直接读引脚(不经过组件的消抖),给"▲ 和确定一起按住"用。
bool bsp_button_is_down(bsp_btn_t btn) {
    if ((int)btn < 0 || (int)btn >= BSP_BTN_COUNT) return false;
    return gpio_get_level(BTN_GPIO[btn]) == 0;
}

// 没有分压电路,没有电压可读。
int bsp_button_read_mv(void) { return -1; }

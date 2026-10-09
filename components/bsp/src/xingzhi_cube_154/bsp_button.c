// components/bsp/src/xingzhi_cube_154/bsp_button.c
// 星智板:三个独立 GPIO 按键,低电平有效。事件种类与 Passport 的 ADC 三键完全一致。
#include "bsp_button.h"
#include "bsp_pins.h"
#include "iot_button.h"
#include "button_gpio.h"
#include "esp_log.h"

static const char *TAG = "bsp_btn";

static const int BTN_GPIO[BSP_BTN_COUNT] = { BSP_BTN_GPIO_UP, BSP_BTN_GPIO_DOWN, BSP_BTN_GPIO_OK };

static button_handle_t s_btn[BSP_BTN_COUNT];
static bsp_btn_cb_t    s_cb;
static void           *s_user;

static void on_event(void *arg, void *usr_data, bsp_btn_ev_t ev) {
    (void)arg;
    if (!s_cb) return;
    s_cb((bsp_btn_t)(intptr_t)usr_data, ev, s_user);
}
static void cb_press (void *a, void *u) { on_event(a, u, BSP_BTN_PRESS);  }
static void cb_click (void *a, void *u) { on_event(a, u, BSP_BTN_CLICK);  }
static void cb_double(void *a, void *u) { on_event(a, u, BSP_BTN_DOUBLE); }
static void cb_long  (void *a, void *u) { on_event(a, u, BSP_BTN_LONG);   }

esp_err_t bsp_button_init(bsp_btn_cb_t cb, void *user) {
    s_cb = cb; s_user = user;
    for (int i = 0; i < BSP_BTN_COUNT; i++) {
        const button_gpio_config_t gc = { .gpio_num = BTN_GPIO[i], .active_level = 0 };
        const button_config_t bc = { 0 };
        esp_err_t e = iot_button_new_gpio_device(&bc, &gc, &s_btn[i]);
        if (e != ESP_OK || !s_btn[i]) {
            ESP_LOGE(TAG, "按键 %d (GPIO%d) 创建失败: %s", i, BTN_GPIO[i], esp_err_to_name(e));
            return e == ESP_OK ? ESP_FAIL : e;
        }
        void *idx = (void *)(intptr_t)i;
        iot_button_register_cb(s_btn[i], BUTTON_PRESS_DOWN,       NULL, cb_press,  idx);
        iot_button_register_cb(s_btn[i], BUTTON_SINGLE_CLICK,     NULL, cb_click,  idx);
        iot_button_register_cb(s_btn[i], BUTTON_DOUBLE_CLICK,     NULL, cb_double, idx);
        iot_button_register_cb(s_btn[i], BUTTON_LONG_PRESS_START, NULL, cb_long,   idx);
    }
    ESP_LOGI(TAG, "按键就绪:上=GPIO%d 下=GPIO%d 确定=GPIO%d",
             BSP_BTN_GPIO_UP, BSP_BTN_GPIO_DOWN, BSP_BTN_GPIO_OK);
    return ESP_OK;
}

// 没有分压电路,没有电压可读。
int bsp_button_read_mv(void) { return -1; }

// components/bsp/src/bsp_button_events.c -- 见 bsp_button_events.h。
//
// 确定键登记两个"长按开始"回调:BSP_OK_HOLD_MS(报 HOLD)和 1500 ms(报 LONG)。
// 组件按按下时长依次触发它们,条件是该键的 long_press_time 等于较短的那个;
// bsp_button_set_ok_hold(false) 把 long_press_time 设回 1500,较短的那个就永远不会触发,
// 1500 ms 的照常触发 —— 这就是"语音关 = 原来的按键"。
// (组件里 LONG_PRESS_START 的多档时长见 managed_components/espressif__button/iot_button.c
//  button_handler() 的 PRESS_LONG_PRESS_UP_CHECK 分支。)
// 松开不用 LONG_PRESS_UP:那条路径在只登记一个回调时会读出数组末尾,改用 PRESS_UP 加"按住过"标记。
#include "bsp_button_events.h"
#include "sdkconfig.h"
#include "esp_log.h"

static const char *TAG = "bsp_btn";

// 以前确定键的长按时长(组件默认值),语音关时恢复它。
#define LEGACY_LONG_MS CONFIG_BUTTON_LONG_PRESS_TIME_MS

static bsp_btn_cb_t    s_cb;
static void           *s_user;
static button_handle_t s_ok;
// 本次按下是否报过 HOLD(只在按键组件的定时器任务里读写)。
static bool            s_ok_held;

static void emit(void *usr_data, bsp_btn_ev_t ev) {
    if (!s_cb) return;
    s_cb((bsp_btn_t)(intptr_t)usr_data, ev, s_user);
}
static void cb_press (void *a, void *u) { (void)a; emit(u, BSP_BTN_PRESS);  }
static void cb_click (void *a, void *u) { (void)a; emit(u, BSP_BTN_CLICK);  }
static void cb_double(void *a, void *u) { (void)a; emit(u, BSP_BTN_DOUBLE); }
static void cb_long  (void *a, void *u) { (void)a; emit(u, BSP_BTN_LONG);   }
static void cb_triple(void *a, void *u) { (void)a; emit(u, BSP_BTN_TRIPLE); }
static void cb_hold  (void *a, void *u) { (void)a; s_ok_held = true; emit(u, BSP_BTN_HOLD); }
static void cb_up    (void *a, void *u) {
    (void)a;
    if (!s_ok_held) return;
    s_ok_held = false;
    emit(u, BSP_BTN_RELEASE);
}

void bsp_button_events_init(bsp_btn_cb_t cb, void *user) {
    s_cb = cb;
    s_user = user;
}

esp_err_t bsp_button_events_attach(button_handle_t h, bsp_btn_t key) {
    void *idx = (void *)(intptr_t)key;
    esp_err_t e = ESP_OK;
    e |= iot_button_register_cb(h, BUTTON_PRESS_DOWN,   NULL, cb_press,  idx);
    e |= iot_button_register_cb(h, BUTTON_SINGLE_CLICK, NULL, cb_click,  idx);
    e |= iot_button_register_cb(h, BUTTON_DOUBLE_CLICK, NULL, cb_double, idx);
    if (key != BSP_BTN_OK) {
        // 方向键与以前完全相同:一个长按,用组件的默认时长。
        e |= iot_button_register_cb(h, BUTTON_LONG_PRESS_START, NULL, cb_long, idx);
        return e == ESP_OK ? ESP_OK : ESP_FAIL;
    }
    button_event_args_t hold = { .long_press.press_time = BSP_OK_HOLD_MS };
    button_event_args_t legacy = { .long_press.press_time = LEGACY_LONG_MS };
    button_event_args_t triple = { .multiple_clicks.clicks = 3 };
    e |= iot_button_register_cb(h, BUTTON_LONG_PRESS_START, &hold, cb_hold, idx);
    e |= iot_button_register_cb(h, BUTTON_LONG_PRESS_START, &legacy, cb_long, idx);
    e |= iot_button_register_cb(h, BUTTON_MULTIPLE_CLICK, &triple, cb_triple, idx);
    e |= iot_button_register_cb(h, BUTTON_PRESS_UP, NULL, cb_up, idx);
    // 登记较短的那档时组件会把 long_press_time 改成它;先回到以前的值,语音开了再改。
    iot_button_set_param(h, BUTTON_LONG_PRESS_TIME_MS, (void *)(intptr_t)LEGACY_LONG_MS);
    s_ok = h;
    return e == ESP_OK ? ESP_OK : ESP_FAIL;
}

void bsp_button_set_ok_hold(bool on) {
    static bool s_on;
    if (!s_ok || s_on == on) return;
    s_on = on;
    iot_button_set_param(s_ok, BUTTON_LONG_PRESS_TIME_MS,
                         (void *)(intptr_t)(on ? BSP_OK_HOLD_MS : LEGACY_LONG_MS));
    ESP_LOGI(TAG, "确定键按住说话:%s(%d ms)", on ? "开" : "关", on ? BSP_OK_HOLD_MS : LEGACY_LONG_MS);
}

// components/bsp/include/bsp_button.h
// 三个按键共用一个 ADC 引脚,靠分压电阻区分。电压窗口见 bsp_pins.h。
#pragma once

#include "esp_err.h"
#include <stdbool.h>

// 按键索引。数量用 bsp_pins.h 的 BSP_BTN_COUNT(硬件属性,归引脚表管),
// 这里不再定义尾项计数,避免出现 BSP_BTN_COUNT / BSP_BTN_COUNT_ 两个近似名字。
typedef enum {
    BSP_BTN_UP = 0,
    BSP_BTN_DOWN,
    BSP_BTN_OK,
} bsp_btn_t;

typedef enum {
    BSP_BTN_PRESS = 0,   // 按下瞬间(低延迟,适合游戏类即时响应)
    BSP_BTN_CLICK,       // 单击(按下并抬起)
    BSP_BTN_DOUBLE,      // 双击
    BSP_BTN_LONG,        // 长按(1.5 s,CONFIG_BUTTON_LONG_PRESS_TIME_MS)
    // 以下三种是语音按住说话加的(只有确定键报):
    BSP_BTN_HOLD,        // 确定键按住满 BSP_OK_HOLD_MS(只在 bsp_button_set_ok_hold(true) 之后报)
    BSP_BTN_RELEASE,     // 报过 HOLD 之后松开
    BSP_BTN_TRIPLE,      // 三击
    // 恢复登录（main/av_recover.h）加的，也只有确定键报：一串快速连击的次数。
    BSP_BTN_QUAD,        // 四击
    BSP_BTN_PENTA,       // 五击及以上（5~10 次；再多组件不报）
} bsp_btn_ev_t;

// 确定键"按住"的门限。默认取语音的 Kconfig(CONFIG_AV_VOICE_HOLD_MS),没有就是 450 ms。
#if __has_include("sdkconfig.h")
#include "sdkconfig.h"
#endif
#ifndef BSP_OK_HOLD_MS
#ifdef CONFIG_AV_VOICE_HOLD_MS
#define BSP_OK_HOLD_MS CONFIG_AV_VOICE_HOLD_MS
#else
#define BSP_OK_HOLD_MS 450
#endif
#endif

// 按键事件回调。运行于 button 组件的定时器任务,勿在其中阻塞或做重活。
typedef void (*bsp_btn_cb_t)(bsp_btn_t btn, bsp_btn_ev_t ev, void *user);

esp_err_t bsp_button_init(bsp_btn_cb_t cb, void *user);

// 确定键的"按住"开关,默认关。
//   关:和以前一模一样 —— 按 1.5 s 才报 LONG;按 0.5~1.5 s 松开仍是单击。
//   开:按住 BSP_OK_HOLD_MS 报 HOLD,1.5 s 照样报 LONG,松开报 RELEASE;按住过就不再报单击。
// 可在任意任务里调(组件内部有临界区),不阻塞。方向键不受影响。
void bsp_button_set_ok_hold(bool on);

// 这个键现在是不是按着（读引脚电平，不经过消抖）。给"两个键一起按住"用:
// 星智板三个键各有 GPIO,读得准;Passport 三键共用一个 ADC 分压,两个键同时按只读得出一个,
// 而且这个 ADC 由按键组件的定时器任务在读,别处再读会撞车 —— 所以 Passport 上恒为 false。
bool bsp_button_is_down(bsp_btn_t btn);

// 读当前 ADC 原始电压(mV)。松开时约 3300;按住某键时约为该键的分压值。
// ★ 换了分压/上拉阻值后,用它测出自己的三档电压,再改 bsp_pins.h 的 BSP_BTN_MV_TABLE。
// 读取失败返回 -1。
int bsp_button_read_mv(void);

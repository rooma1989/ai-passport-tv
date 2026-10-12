// components/bsp/src/bsp_button_events.h
// 两块板共用的按键事件登记:哪个 iot_button 事件报成哪个 bsp_btn_ev_t。
// 板子文件只管把按键建出来(GPIO 或 ADC 分压),然后对每个键调一次 bsp_button_events_attach()。
#pragma once

#include "bsp_button.h"
#include "iot_button.h"

void bsp_button_events_init(bsp_btn_cb_t cb, void *user);
esp_err_t bsp_button_events_attach(button_handle_t handle, bsp_btn_t key);

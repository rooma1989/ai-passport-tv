// components/bsp/include/bsp_power.h
// 电池供电下的电源保持。有的板子靠 MCU 的一个引脚"拉住"电源,不拉就会在松开开机键后掉电。
#pragma once

#include "esp_err.h"

// app_main 第一行调用。没有电源保持脚的板子上什么都不做,返回 ESP_OK。
esp_err_t bsp_power_init(void);

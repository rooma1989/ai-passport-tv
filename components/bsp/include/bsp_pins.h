// components/bsp/include/bsp_pins.h
// 板级引脚与参数的入口。按 Kconfig 的 AV_BOARD 选入具体板子的引脚表:
//   boards/passport.h          FoloToy AI Passport (ESP32-C3)
//   boards/xingzhi_cube_154.h  星智 Cube 1.54 TFT (ESP32-S3)
// 每个板子文件都是该板的【单一事实来源】;换板/改硬件只改对应那一个文件。
#pragma once

#include "sdkconfig.h"

#if defined(CONFIG_AV_BOARD_XINGZHI_CUBE_154)
#include "boards/xingzhi_cube_154.h"
#else
#include "boards/passport.h"
#endif

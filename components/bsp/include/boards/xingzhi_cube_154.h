// components/bsp/include/boards/xingzhi_cube_154.h
// 星智 Cube 1.54 TFT ML307(ESP32-S3)引脚与参数的【单一事实来源】。由 bsp_pins.h 按 Kconfig AV_BOARD 选入。
// 来源:用户设备实测(esptool 读芯片/Flash ID,逐键 GPIO 探测),并与
// github.com/78/xiaozhi-esp32 的 main/boards/nologo/xingzhi-cube-1.54tft-ml307 核对。
#pragma once

#include "driver/spi_master.h"
#include "hal/adc_types.h"
#include "hal/i2s_types.h"
#include "hal/ledc_types.h"
#include "hal/uart_types.h"

// ============================================================================
// 显示:ST7789 240x240,4-line SPI,走 SPI3
// ============================================================================
#define BSP_LCD_W            240
#define BSP_LCD_H            240
#define BSP_LCD_SPI_HOST     SPI3_HOST
#define BSP_LCD_MOSI         10
#define BSP_LCD_SCLK         9
#define BSP_LCD_CS           14
#define BSP_LCD_DC           8
#define BSP_LCD_RST          18
#define BSP_LCD_BL           13          // 背光,高电平亮,LEDC PWM 调光
#define BSP_LCD_PCLK_HZ      (80 * 1000 * 1000)
// 小智固件对这块屏用 SPI mode 3。
#define BSP_LCD_SPI_MODE     3
#define BSP_LCD_INVERT_COLOR 1
// 通用 ST7789 初始化即可,不发 Passport 那套厂商 gamma 表。
#define BSP_LCD_VENDOR_INIT  0

#define BSP_BL_LEDC_TIMER    LEDC_TIMER_0
#define BSP_BL_LEDC_MODE     LEDC_LOW_SPEED_MODE
#define BSP_BL_LEDC_CHANNEL  LEDC_CHANNEL_0
#define BSP_BL_LEDC_RES      LEDC_TIMER_10_BIT
#define BSP_BL_LEDC_FREQ_HZ  5000

// 画面模式(raw):方屏不用转向,直接 240x240。
#define BSP_RAW_W            240
#define BSP_RAW_H            240
#define BSP_RAW_SWAP_XY      false
#define BSP_RAW_MIRROR_X     false
#define BSP_RAW_MIRROR_Y     false

// ============================================================================
// 按键:三个独立 GPIO,按下接地(低电平有效),用内部上拉。
// 外壳右侧从上到下:GPIO39 / GPIO40 / BOOT / 复位(电源)。最下面那个是硬件键,MCU 读不到。
// 外壳上印的是"音量-、音量+",但实际用起来"靠上的键=上/变大"更顺手(用户反馈),
// 所以按物理位置映射:最上面的键是 UP,第二个是 DOWN。
// ============================================================================
#define BSP_BTN_COUNT        3
// 播放时上/下键:短按调音量、长按换台(这两个键在外壳上就是音量键)。与 Passport 相反。
#define BSP_ARROW_CLICK_IS_VOLUME 1
// 双击:上/下键打开状态页、OK 切换只听声音(与 Passport 不同)。
#define BSP_DOUBLE_OK_IS_STATUS   0
// 双核 S3 不缺 CPU,会话任务保持原来的优先级(与 Passport 不同)。
#define BSP_KEYS_ABOVE_VIDEO      0
#define BSP_BTN_GPIO_UP      39          // 最上面的键(外壳印"音量-")
#define BSP_BTN_GPIO_DOWN    40          // 第二个键(外壳印"音量+")
#define BSP_BTN_GPIO_OK      0           // BOOT 键,也是下载模式的 strapping 脚

// ============================================================================
// 音频:I2S 直连数字功放,无 codec、无 MCLK。麦克风(WS 4/SCK 5/DIN 6)本玩法不用。
// ============================================================================
#define BSP_I2S_PORT         I2S_NUM_0
#define BSP_I2S_BCLK         15
#define BSP_I2S_WS           16
#define BSP_I2S_DOUT         7
// 输出幅度上限(Q16,65536 = 满幅)。曾设为一半:4G 模组反复搜网、Wi-Fi 满功率发射时,
// 测试音一响就欠压复位。两者处理掉之后(见下方模组飞行模式与 sdkconfig 里的发射功率)
// 恢复满幅;若再出现欠压复位(bsp_power 日志"上次复位原因: 9"),先降这里。
#define BSP_AUDIO_MAX_GAIN_Q16 65536u

// ============================================================================
// 电量:电池经分压接 ADC2_CH6(GPIO17)。充电检测接 GPIO38,高电平 = 充电中。
// ============================================================================
#define BSP_BATT_ADC_UNIT    ADC_UNIT_2
#define BSP_BATT_ADC_CHANNEL ADC_CHANNEL_6
#define BSP_BATT_DIVIDER     2           // 按 1:2 分压推测,未实测
#define BSP_BATT_CHARGE_GPIO 38

// ============================================================================
// 电源保持:RTC GPIO21。电池供电时开机必须拉高,否则松开开机键就掉电。
// ============================================================================
#define BSP_POWER_HOLD_GPIO  21

// ============================================================================
// 4G 模组 ML307R:本玩法不用,但它上电即搜网;SIM 不可用时会反复尝试附着,
// 发射脉冲干扰 Wi-Fi 并拉低电源。开机时通过 UART 发 AT+CFUN=0 让它进入飞行模式。
// 引脚按小智固件:ESP 的 TX=GPIO12、RX=GPIO11。小智把模组设成 921600,出厂可能是 115200。
// ============================================================================
#define BSP_MODEM_UART       UART_NUM_1
#define BSP_MODEM_TX         12
#define BSP_MODEM_RX         11

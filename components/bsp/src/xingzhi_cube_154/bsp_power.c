// components/bsp/src/xingzhi_cube_154/bsp_power.c
// 星智板:GPIO21 是 RTC GPIO,拉高才能在电池下保持供电(小智固件同样用 rtc_gpio_* 驱动它,
// 关机时拉低并 hold)。上次关机可能留下 hold,先解除再拉高。
#include "bsp_power.h"
#include "bsp_pins.h"
#include "driver/rtc_io.h"
#include "esp_log.h"

esp_err_t bsp_power_init(void)
{
    rtc_gpio_hold_dis(BSP_POWER_HOLD_GPIO);
    esp_err_t e = rtc_gpio_init(BSP_POWER_HOLD_GPIO);
    if (e == ESP_OK) e = rtc_gpio_set_direction(BSP_POWER_HOLD_GPIO, RTC_GPIO_MODE_OUTPUT_ONLY);
    if (e == ESP_OK) e = rtc_gpio_set_level(BSP_POWER_HOLD_GPIO, 1);
    if (e != ESP_OK) {
        ESP_LOGE("bsp_power", "电源保持 GPIO%d 失败: %s", BSP_POWER_HOLD_GPIO, esp_err_to_name(e));
    }
    return e;
}

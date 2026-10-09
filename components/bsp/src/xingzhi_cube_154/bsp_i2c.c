// components/bsp/src/xingzhi_cube_154/bsp_i2c.c
// 星智板没有 I2C 外设(无 codec、无电量计)。保留同名接口,main.c 就不用按板子分支。
#include "bsp_i2c.h"
#include "esp_log.h"

esp_err_t bsp_i2c_init(void) { return ESP_ERR_NOT_SUPPORTED; }

i2c_master_bus_handle_t bsp_i2c_bus(void) { return NULL; }

esp_err_t bsp_i2c_scan(void)
{
    ESP_LOGI("bsp_i2c", "本板没有 I2C 设备,跳过扫描");
    return ESP_ERR_NOT_SUPPORTED;
}

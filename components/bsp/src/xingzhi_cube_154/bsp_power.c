// components/bsp/src/xingzhi_cube_154/bsp_power.c
// 星智板:GPIO21 是 RTC GPIO,拉高才能在电池下保持供电(小智固件同样用 rtc_gpio_* 驱动它,
// 关机时拉低并 hold)。上次关机可能留下 hold,先解除再拉高。
// 同时让用不到的 4G 模组进入飞行模式(原因见 boards/xingzhi_cube_154.h)。
#include "bsp_power.h"
#include "bsp_pins.h"
#include "driver/rtc_io.h"
#include "driver/uart.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <string.h>

static const char *TAG = "bsp_power";

// 发一条 AT 命令,在 timeout_ms 内等到含 "OK" 的回复即成功。
static bool modem_cmd(const char *cmd, uint32_t timeout_ms)
{
    char buf[128];
    size_t got = 0;
    uart_flush_input(BSP_MODEM_UART);
    uart_write_bytes(BSP_MODEM_UART, cmd, strlen(cmd));
    TickType_t end = xTaskGetTickCount() + pdMS_TO_TICKS(timeout_ms);
    while (xTaskGetTickCount() < end && got < sizeof(buf) - 1) {
        int n = uart_read_bytes(BSP_MODEM_UART, (uint8_t *)buf + got, sizeof(buf) - 1 - got, pdMS_TO_TICKS(50));
        if (n > 0) {
            got += (size_t)n;
            buf[got] = '\0';
            if (strstr(buf, "OK")) return true;
            if (strstr(buf, "ERROR")) return false;
        }
    }
    return false;
}

// 模组上电后要几秒才响应 AT。放在后台任务里,不拖慢开机。
static void modem_quiet_task(void *arg)
{
    (void)arg;
    static const int BAUDS[] = { 921600, 115200 };
    bool done = false;
    const uart_config_t cfg = {
        .baud_rate = BAUDS[0], .data_bits = UART_DATA_8_BITS, .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1, .flow_ctrl = UART_HW_FLOWCTRL_DISABLE, .source_clk = UART_SCLK_DEFAULT,
    };
    if (uart_driver_install(BSP_MODEM_UART, 512, 0, 0, NULL, 0) != ESP_OK ||
        uart_param_config(BSP_MODEM_UART, &cfg) != ESP_OK ||
        uart_set_pin(BSP_MODEM_UART, BSP_MODEM_TX, BSP_MODEM_RX, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE) != ESP_OK) {
        ESP_LOGW(TAG, "4G 模组 UART 初始化失败,模组保持原状");
        vTaskDelete(NULL);
        return;
    }
    for (int attempt = 0; attempt < 20 && !done; attempt++) {
        for (size_t b = 0; b < sizeof(BAUDS) / sizeof(BAUDS[0]) && !done; b++) {
            uart_set_baudrate(BSP_MODEM_UART, BAUDS[b]);
            if (modem_cmd("AT\r\n", 200)) {
                done = modem_cmd("AT+CFUN=0\r\n", 5000);
                ESP_LOGI(TAG, "4G 模组 %d 波特应答;飞行模式(AT+CFUN=0)%s", BAUDS[b], done ? "已开启" : "失败");
                if (!done) attempt = 20;   // 模组在线但拒绝,不再重试
            }
        }
        if (!done && attempt < 20) vTaskDelay(pdMS_TO_TICKS(500));
    }
    if (!done) ESP_LOGW(TAG, "4G 模组没有应答 AT,模组保持原状");
    uart_driver_delete(BSP_MODEM_UART);
    vTaskDelete(NULL);
}

esp_err_t bsp_power_init(void)
{
    rtc_gpio_hold_dis(BSP_POWER_HOLD_GPIO);
    esp_err_t e = rtc_gpio_init(BSP_POWER_HOLD_GPIO);
    if (e == ESP_OK) e = rtc_gpio_set_direction(BSP_POWER_HOLD_GPIO, RTC_GPIO_MODE_OUTPUT_ONLY);
    if (e == ESP_OK) e = rtc_gpio_set_level(BSP_POWER_HOLD_GPIO, 1);
    if (e != ESP_OK) {
        ESP_LOGE(TAG, "电源保持 GPIO%d 失败: %s", BSP_POWER_HOLD_GPIO, esp_err_to_name(e));
    }
    xTaskCreate(modem_quiet_task, "modem_quiet", 3072, NULL, 2, NULL);
    return e;
}

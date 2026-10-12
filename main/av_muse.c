#include "av_muse.h"

#include "sdkconfig.h"

#if CONFIG_AV_MUSE_DUAL

#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "nvs.h"

static const char *TAG = "av_muse";

void av_muse_mark_valid(void)
{
    esp_ota_img_states_t state;
    const esp_partition_t *running = esp_ota_get_running_partition();
    if (esp_ota_get_state_partition(running, &state) == ESP_OK && state == ESP_OTA_IMG_PENDING_VERIFY) {
        esp_err_t err = esp_ota_mark_app_valid_cancel_rollback();
        ESP_LOGI(TAG, "Marked this image valid: %s", esp_err_to_name(err));
    }
}

bool av_muse_wifi(char *ssid, size_t ssid_cap, char *password, size_t password_cap)
{
    nvs_handle_t h;
    if (nvs_open("homehub", NVS_READONLY, &h) != ESP_OK) return false;
    size_t n = ssid_cap, m = password_cap;
    bool ok = nvs_get_str(h, "ssid", ssid, &n) == ESP_OK && ssid[0]
              && nvs_get_str(h, "password", password, &m) == ESP_OK;
    nvs_close(h);
    return ok;
}

void av_muse_volume(unsigned char *percent)
{
    nvs_handle_t h;
    uint8_t v;
    if (nvs_open("avsettings", NVS_READONLY, &h) == ESP_OK) {   /* av_store.c's own */
        bool own = nvs_get_u8(h, "volume", &v) == ESP_OK;
        nvs_close(h);
        if (own) return;
    }
    if (nvs_open("muse", NVS_READONLY, &h) != ESP_OK) return;
    if (nvs_get_u8(h, "volume", &v) == ESP_OK && v <= 100) {
        *percent = v;
        ESP_LOGI(TAG, "Volume %u%%, as Muse's", v);
    }
    nvs_close(h);
}

void av_muse_back(void)
{
    const esp_partition_t *muse =
        esp_partition_find_first(ESP_PARTITION_TYPE_APP, ESP_PARTITION_SUBTYPE_APP_OTA_0, NULL);
    esp_err_t err = muse ? esp_ota_set_boot_partition(muse) : ESP_ERR_NOT_FOUND;
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Can't boot Muse: %s", esp_err_to_name(err));
        return;
    }
    ESP_LOGW(TAG, "Back to Muse");
    esp_restart();
}

#endif

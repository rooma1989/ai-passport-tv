#include "av_store.h"
#include "av_identity.h"
#include "av_server_addr.h"
#include "av_settings.h"

#include <esp_log.h>
#include <nvs.h>
#include <string.h>

static const char *TAG = "av_store";

// Separate from the provisioning component's "wifi" namespace on purpose: it
// owns those keys and reads them while joining a network, and a second writer
// with its own idea of the layout would be a defect waiting to happen.
#define AV_STORE_NAMESPACE "avsettings"
#define AV_STORE_KEY_VOLUME "volume"
#define AV_STORE_KEY_BRIGHT "bright"
#define AV_STORE_KEY_MAGIC  "rev"

// Bumped if the meaning of a stored value ever changes. A record written by a
// different revision is ignored rather than reinterpreted.
#define AV_STORE_REVISION 1u

static bool open_readonly(nvs_handle_t *handle)
{
    esp_err_t e = nvs_open(AV_STORE_NAMESPACE, NVS_READONLY, handle);
    if (e != ESP_OK) {
        // Absent on a device that has never been adjusted, which is not an error.
        ESP_LOGI(TAG, "No stored settings yet (%s)", esp_err_to_name(e));
        return false;
    }
    return true;
}

bool av_store_load(uint8_t *volume_percent, uint8_t *brightness_percent)
{
    if (volume_percent) {
        *volume_percent = (uint8_t)AV_VOLUME_DEFAULT_PERCENT;
    }
    if (brightness_percent) {
        *brightness_percent = (uint8_t)AV_BRIGHTNESS_DEFAULT_PERCENT;
    }

    nvs_handle_t handle;
    if (!open_readonly(&handle)) {
        return true;   // defaults already written out; nothing to report
    }

    uint32_t revision = 0;
    bool usable = nvs_get_u32(handle, AV_STORE_KEY_MAGIC, &revision) == ESP_OK
                  && revision == AV_STORE_REVISION;

    uint8_t volume = 0, bright = 0;
    if (usable) {
        usable = nvs_get_u8(handle, AV_STORE_KEY_VOLUME, &volume) == ESP_OK
                 && nvs_get_u8(handle, AV_STORE_KEY_BRIGHT, &bright) == ESP_OK;
        // Volume is a percentage of the codec's range, so any 0..100 is valid.
        // Brightness is one of a fixed set, so a value that is not one of them
        // means the store was written by a build with different levels, and the
        // nearest level is a better answer than refusing the whole record.
        if (usable && volume > 100u) {
            usable = false;
        }
    }
    nvs_close(handle);

    if (!usable) {
        ESP_LOGW(TAG, "Stored settings unusable; using defaults");
        return true;
    }
    if (volume_percent) {
        *volume_percent = volume;
    }
    if (brightness_percent) {
        uint8_t level = av_brightness_valid_percent(bright)
                        ? bright
                        : av_brightness_percent(
                              av_brightness_nearest_index(bright));
        *brightness_percent = level;
    }
    ESP_LOGI(TAG, "Restored settings: volume %u%%, brightness %u%%",
             volume, brightness_percent ? *brightness_percent : 0u);
    return true;
}

bool av_store_server_addr_load(av_server_addr_t *addr)
{
    if (!addr) {
        return false;
    }

    // The same namespace and key the setup page writes through. Reading it here
    // rather than keeping a second copy means the page and the player can never
    // disagree about which server to use.
    nvs_handle_t handle;
    if (nvs_open("wifi", NVS_READONLY, &handle) != ESP_OK) {
        return false;
    }

    // Matches the ceiling the page enforces on the field, so a value that could
    // be submitted can always be read back. The parser rejects anything longer
    // than it can hold, so an oversized stored value fails rather than truncates.
    char text[256] = {0};
    size_t size = sizeof(text);
    esp_err_t e = nvs_get_str(handle, "ota_url", text, &size);
    nvs_close(handle);
    if (e != ESP_OK) {
        ESP_LOGI(TAG, "No server address stored yet (%s)", esp_err_to_name(e));
        return false;
    }

    av_server_addr_t parsed;
    if (!av_server_addr_parse(text, &parsed) || !av_server_addr_usable(&parsed)) {
        // Said plainly, because the cure is to open the setup page and correct
        // the field -- not something the device can work around on its own.
        ESP_LOGW(TAG, "Stored server address is unusable; open the setup page");
        return false;
    }

    // No success line here. The caller reads this once per session so that a new
    // address takes effect without a restart, and announcing it from inside would
    // print the same line every few seconds. Reporting it is the caller's job.
    *addr = parsed;
    return true;
}

bool av_store_save(uint8_t volume_percent, uint8_t brightness_percent)
{
    nvs_handle_t handle;
    esp_err_t e = nvs_open(AV_STORE_NAMESPACE, NVS_READWRITE, &handle);
    if (e != ESP_OK) {
        ESP_LOGW(TAG, "Cannot open settings store: %s", esp_err_to_name(e));
        return false;
    }
    if (volume_percent > 100u) {
        volume_percent = 100u;
    }
    // Written last, so a record interrupted midway is rejected on the next boot
    // rather than read as a half-valid pair.
    e = nvs_set_u8(handle, AV_STORE_KEY_VOLUME, volume_percent);
    if (e == ESP_OK) {
        e = nvs_set_u8(handle, AV_STORE_KEY_BRIGHT, brightness_percent);
    }
    if (e == ESP_OK) {
        e = nvs_set_u32(handle, AV_STORE_KEY_MAGIC, AV_STORE_REVISION);
    }
    if (e == ESP_OK) {
        e = nvs_commit(handle);
    }
    nvs_close(handle);
    if (e != ESP_OK) {
        ESP_LOGW(TAG, "Settings not saved: %s", esp_err_to_name(e));
        return false;
    }
    return true;
}

// The pet is its own key, outside the volume/brightness record: a device
// written before pets existed simply has none, which reads as the default.
#define AV_STORE_KEY_PET "pet"

uint8_t av_store_load_pet(uint8_t count)
{
    nvs_handle_t handle;
    if (!open_readonly(&handle)) {
        return 0;
    }
    uint8_t pet = 0;
    if (nvs_get_u8(handle, AV_STORE_KEY_PET, &pet) != ESP_OK || pet >= count) {
        pet = 0;
    }
    nvs_close(handle);
    return pet;
}

bool av_store_save_pet(uint8_t pet)
{
    nvs_handle_t handle;
    esp_err_t e = nvs_open(AV_STORE_NAMESPACE, NVS_READWRITE, &handle);
    if (e != ESP_OK) {
        ESP_LOGW(TAG, "Cannot open settings store: %s", esp_err_to_name(e));
        return false;
    }
    e = nvs_set_u8(handle, AV_STORE_KEY_PET, pet);
    if (e == ESP_OK) {
        e = nvs_commit(handle);
    }
    nvs_close(handle);
    if (e != ESP_OK) {
        ESP_LOGW(TAG, "Saving the pet failed: %s", esp_err_to_name(e));
        return false;
    }
    return true;
}

// Audio-only mode, its own key like the pet: absent reads as off.
#define AV_STORE_KEY_AUDIO "audio"

bool av_store_load_audio_only(void)
{
    nvs_handle_t handle;
    if (!open_readonly(&handle)) {
        return false;
    }
    uint8_t on = 0;
    if (nvs_get_u8(handle, AV_STORE_KEY_AUDIO, &on) != ESP_OK) {
        on = 0;
    }
    nvs_close(handle);
    return on != 0;
}

bool av_store_save_audio_only(bool on)
{
    nvs_handle_t handle;
    esp_err_t e = nvs_open(AV_STORE_NAMESPACE, NVS_READWRITE, &handle);
    if (e != ESP_OK) {
        ESP_LOGW(TAG, "Cannot open settings store: %s", esp_err_to_name(e));
        return false;
    }
    e = nvs_set_u8(handle, AV_STORE_KEY_AUDIO, on ? 1u : 0u);
    if (e == ESP_OK) {
        e = nvs_commit(handle);
    }
    nvs_close(handle);
    if (e != ESP_OK) {
        ESP_LOGW(TAG, "Saving audio-only failed: %s", esp_err_to_name(e));
        return false;
    }
    return true;
}

// The device secret, its own key like the pet. Stored as the text that travels
// in HELLO, so what is read back can be checked with the same rule the server
// applies.
#define AV_STORE_KEY_SECRET "dsecret"

bool av_store_load_device_secret(char *out, size_t size)
{
    if (!out || size < AV_DEVICE_SECRET_LEN + 1) {
        return false;
    }
    out[0] = '\0';
    nvs_handle_t handle;
    if (!open_readonly(&handle)) {
        return false;
    }
    size_t length = size;
    esp_err_t e = nvs_get_str(handle, AV_STORE_KEY_SECRET, out, &length);
    nvs_close(handle);
    if (e != ESP_OK || !av_identity_secret_valid(out)) {
        out[0] = '\0';
        return false;
    }
    return true;
}

bool av_store_save_device_secret(const char *secret)
{
    if (!av_identity_secret_valid(secret)) {
        return false;
    }
    nvs_handle_t handle;
    esp_err_t e = nvs_open(AV_STORE_NAMESPACE, NVS_READWRITE, &handle);
    if (e != ESP_OK) {
        ESP_LOGW(TAG, "Cannot open settings store: %s", esp_err_to_name(e));
        return false;
    }
    e = nvs_set_str(handle, AV_STORE_KEY_SECRET, secret);
    if (e == ESP_OK) {
        e = nvs_commit(handle);
    }
    nvs_close(handle);
    if (e != ESP_OK) {
        ESP_LOGW(TAG, "Saving the device secret failed: %s", esp_err_to_name(e));
        return false;
    }
    return true;
}

// ---- Wi-Fi management (av_wifimgr.c) ----

#define AV_STORE_KEY_WIFI_KEY "wkey"
#define AV_STORE_KEY_SERVER_KEY "wsrv"
#define AV_STORE_KEY_SERVER_ADDR "wsrva"
#define AV_STORE_KEY_WIFI_SEQ "wseq"
#define AV_STORE_KEY_SITE "site"

static bool load_blob(const char *key, void *out, size_t size)
{
    nvs_handle_t handle;
    if (!open_readonly(&handle)) {
        return false;
    }
    size_t length = size;
    esp_err_t e = nvs_get_blob(handle, key, out, &length);
    nvs_close(handle);
    return e == ESP_OK && length == size;
}

// Set (or with `erase`, remove) some keys and commit once.
typedef struct { const char *key; const void *blob; size_t size; const char *text; bool u64; uint64_t value; } store_item_t;

static bool store_items(const store_item_t *items, size_t count, const char *what)
{
    nvs_handle_t handle;
    esp_err_t e = nvs_open(AV_STORE_NAMESPACE, NVS_READWRITE, &handle);
    if (e != ESP_OK) {
        ESP_LOGW(TAG, "Cannot open settings store: %s", esp_err_to_name(e));
        return false;
    }
    for (size_t i = 0; i < count && e == ESP_OK; i++) {
        if (items[i].blob) e = nvs_set_blob(handle, items[i].key, items[i].blob, items[i].size);
        else if (items[i].text) e = nvs_set_str(handle, items[i].key, items[i].text);
        else if (items[i].u64) e = nvs_set_u64(handle, items[i].key, items[i].value);
        else {
            e = nvs_erase_key(handle, items[i].key);
            if (e == ESP_ERR_NVS_NOT_FOUND) e = ESP_OK;
        }
    }
    if (e == ESP_OK) {
        e = nvs_commit(handle);
    }
    nvs_close(handle);
    if (e != ESP_OK) {
        ESP_LOGW(TAG, "Saving %s failed: %s", what, esp_err_to_name(e));
        return false;
    }
    return true;
}

bool av_store_load_wifi_key(uint8_t private_key[32], uint8_t public_key[32])
{
    uint8_t both[64];
    if (!load_blob(AV_STORE_KEY_WIFI_KEY, both, sizeof(both))) {
        return false;
    }
    memcpy(private_key, both, 32);
    memcpy(public_key, both + 32, 32);
    memset(both, 0, sizeof(both));
    return true;
}

bool av_store_save_wifi_key(const uint8_t private_key[32], const uint8_t public_key[32])
{
    uint8_t both[64];
    memcpy(both, private_key, 32);
    memcpy(both + 32, public_key, 32);
    const store_item_t item = { .key = AV_STORE_KEY_WIFI_KEY, .blob = both, .size = sizeof(both) };
    const bool ok = store_items(&item, 1, "the Wi-Fi key");
    memset(both, 0, sizeof(both));
    return ok;
}

bool av_store_load_server_key(uint8_t public_key[32], char *addr, size_t addr_size)
{
    if (!load_blob(AV_STORE_KEY_SERVER_KEY, public_key, 32)) {
        return false;
    }
    nvs_handle_t handle;
    if (!open_readonly(&handle)) {
        return false;
    }
    size_t length = addr_size;
    esp_err_t e = nvs_get_str(handle, AV_STORE_KEY_SERVER_ADDR, addr, &length);
    nvs_close(handle);
    return e == ESP_OK;
}

bool av_store_save_server_key(const uint8_t public_key[32], const char *addr)
{
    // A new key starts a new sequence: the seq is the relay's clock, and
    // another relay's clock owes this one nothing.
    const store_item_t items[] = {
        { .key = AV_STORE_KEY_SERVER_KEY, .blob = public_key, .size = 32 },
        { .key = AV_STORE_KEY_SERVER_ADDR, .text = addr ? addr : "" },
        { .key = AV_STORE_KEY_WIFI_SEQ, .u64 = true, .value = 0 },
    };
    return store_items(items, sizeof(items) / sizeof(items[0]), "the server key");
}

void av_store_forget_server_key(void)
{
    const store_item_t items[] = {
        { .key = AV_STORE_KEY_SERVER_KEY }, { .key = AV_STORE_KEY_SERVER_ADDR }, { .key = AV_STORE_KEY_WIFI_SEQ },
    };
    store_items(items, sizeof(items) / sizeof(items[0]), "forgetting the server key");
}

uint64_t av_store_load_wifi_seq(void)
{
    nvs_handle_t handle;
    if (!open_readonly(&handle)) {
        return 0;
    }
    uint64_t value = 0;
    if (nvs_get_u64(handle, AV_STORE_KEY_WIFI_SEQ, &value) != ESP_OK) {
        value = 0;
    }
    nvs_close(handle);
    return value;
}

bool av_store_save_wifi_seq(uint64_t seq)
{
    const store_item_t item = { .key = AV_STORE_KEY_WIFI_SEQ, .u64 = true, .value = seq };
    return store_items(&item, 1, "the Wi-Fi message counter");
}

bool av_store_load_site(char *out, size_t size)
{
    if (!out || size == 0) {
        return false;
    }
    out[0] = '\0';
    nvs_handle_t handle;
    if (!open_readonly(&handle)) {
        return false;
    }
    size_t length = size;
    esp_err_t e = nvs_get_str(handle, AV_STORE_KEY_SITE, out, &length);
    nvs_close(handle);
    if (e != ESP_OK) {
        out[0] = '\0';
        return false;
    }
    return out[0] != '\0';
}

bool av_store_save_site(const char *site)
{
    char current[96];
    if (!site || (av_store_load_site(current, sizeof(current)) && !strcmp(current, site))) {
        return true;
    }
    const store_item_t item = { .key = AV_STORE_KEY_SITE, .text = site };
    return store_items(&item, 1, "the site address");
}

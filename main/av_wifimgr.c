// Saved Wi-Fi networks managed from the phone; see av_wifimgr.h.
#include "av_wifimgr.h"

#include <inttypes.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_random.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "av_provision.h"
#include "av_store.h"
#include "av_wifi_crypto.h"
#include "av_wifi_ops.h"

static const char *TAG = "av_wifimgr";

// The maths (X25519 in mbedTLS's bignum code, GCM, cJSON, NVS writes) wants
// more stack than the callers have: the main task has 3.5 KB and the receive
// task is busy with the stream. A task of its own, made for one message and
// deleted after, costs this much for a second or so.
#define WORKER_STACK 6144
#define WORKER_PRIO 3
// How long a "switch" waits for its ack to leave before the network goes.
#define SWITCH_ACK_WAIT_MS 3000

int av_wifi_crypto_rng(void *ctx, unsigned char *out, size_t length)
{
    (void)ctx;
    esp_fill_random(out, length);
    return 0;
}

static atomic_bool s_ready;
static uint8_t s_public[AV_WIFI_KEY_BYTES];
static char s_public_b64[AV_WIFI_KEY_B64_LEN + 1];
// The relay's key from this session's CONFIG matched the pin (or was pinned).
static atomic_bool s_server_trusted;
// One message at a time.
static atomic_bool s_busy;
// The ack, written by the worker, sent by the receive task.
static char s_ack[AV_WIFIMGR_ACK_MAX];
static atomic_bool s_ack_ready;
static portMUX_TYPE s_ack_lock = portMUX_INITIALIZER_UNLOCKED;

// ---- The key pair ----

typedef struct {
    uint8_t priv[AV_WIFI_KEY_BYTES], pub[AV_WIFI_KEY_BYTES];
    bool ok;
    TaskHandle_t waiter;
} keygen_t;

static void keygen_task(void *arg)
{
    keygen_t *k = arg;
    esp_fill_random(k->priv, sizeof(k->priv));
    k->ok = av_wifi_crypto_public(k->priv, k->pub) && av_store_save_wifi_key(k->priv, k->pub);
    ESP_LOGI(TAG, "key pair %s; stack left=%u", k->ok ? "made and stored" : "NOT made",
             (unsigned)uxTaskGetStackHighWaterMark(NULL));
    xTaskNotifyGive(k->waiter);
    vTaskDelete(NULL);
}

bool av_wifimgr_init(void)
{
    if (atomic_load(&s_ready)) return true;
    uint8_t priv[AV_WIFI_KEY_BYTES];
    bool ok = av_store_load_wifi_key(priv, s_public);
    memset(priv, 0, sizeof(priv));
    if (!ok) {
        // First boot with this feature (or after an NVS erase). Wi-Fi is up by
        // now, so the hardware RNG has its entropy.
        keygen_t *k = calloc(1, sizeof(*k));
        if (!k) return false;
        k->waiter = xTaskGetCurrentTaskHandle();
        if (xTaskCreate(keygen_task, "av_wkey", WORKER_STACK, k, WORKER_PRIO, NULL) == pdPASS &&
            ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(5000)) && k->ok) {
            memcpy(s_public, k->pub, sizeof(s_public));
            ok = true;
        }
        memset(k, 0, sizeof(*k));
        free(k);
        if (!ok) {
            ESP_LOGW(TAG, "no Wi-Fi key; remote Wi-Fi management stays off this boot");
            return false;
        }
    }
    av_b64url_encode(s_public, sizeof(s_public), s_public_b64, sizeof(s_public_b64));
    atomic_store(&s_ready, true);
    return true;
}

// ---- HELLO and CONFIG ----

bool av_wifimgr_hello(cJSON *hello)
{
    if (!atomic_load(&s_ready)) return true;
    char names[AV_WIFI_SAVED_MAX][33];
    char shown[AV_WIFI_SSID_MAX * 3 + 1];
    const unsigned count = av_provision_saved(names, AV_WIFI_SAVED_MAX);
    cJSON *saved = cJSON_CreateArray();
    if (!saved) return false;
    for (unsigned i = 0; i < count; i++) {
        av_wifi_display_name(names[i], shown, sizeof(shown));
        cJSON *item = cJSON_CreateString(shown);
        if (!item) { cJSON_Delete(saved); return false; }
        cJSON_AddItemToArray(saved, item);
    }
    memset(names, 0, sizeof(names));
    if (!cJSON_AddNumberToObject(hello, "wifimgr", 1) || !cJSON_AddStringToObject(hello, "wkey", s_public_b64)) {
        cJSON_Delete(saved);
        return false;
    }
    cJSON_AddItemToObject(hello, "wifi_saved", saved);
    return true;
}

void av_wifimgr_config(const char *key_b64, const char *server)
{
    atomic_store(&s_server_trusted, false);
    uint8_t key[AV_WIFI_KEY_BYTES];
    if (!atomic_load(&s_ready) || !key_b64 || !server ||
        av_b64url_decode(key_b64, key, sizeof(key)) != (int)sizeof(key)) {
        return;
    }
    uint8_t pinned[AV_WIFI_KEY_BYTES];
    char addr[96] = { 0 };
    const bool have = av_store_load_server_key(pinned, addr, sizeof(addr));
    if (have && !strcmp(addr, server)) {
        if (memcmp(pinned, key, sizeof(key)) == 0) {
            atomic_store(&s_server_trusted, true);
        } else {
            // Same server, different key: someone in the middle, or the relay's
            // key file was replaced. Running setup on the device clears the pin.
            ESP_LOGW(TAG, "the server's Wi-Fi key changed; remote Wi-Fi changes refused until setup is run");
        }
        return;
    }
    // First key from this server (or the device was pointed at another one).
    if (av_store_save_server_key(key, server)) {
        ESP_LOGI(TAG, "server Wi-Fi key pinned");
        atomic_store(&s_server_trusted, true);
    }
}

void av_wifimgr_forget_server_key(void)
{
    av_store_forget_server_key();
    atomic_store(&s_server_trusted, false);
}

// ---- One message ----

typedef struct {
    char nonce[24];
    char sealed[AV_WIFI_SEALED_B64_MAX + 1];
    uint8_t plain[AV_WIFI_PLAIN_MAX + 1];
    av_wifi_op_t op;
    char names[AV_WIFI_SAVED_MAX][33];
} job_t;

static void publish_ack(uint64_t seq, av_wifi_err_t err, job_t *job)
{
    const unsigned count = av_provision_saved(job->names, AV_WIFI_SAVED_MAX);
    const char *names[AV_WIFI_SAVED_MAX];
    for (unsigned i = 0; i < count; i++) names[i] = job->names[i];
    char current[33];
    av_provision_network_name(current, sizeof(current));
    char body[AV_WIFIMGR_ACK_MAX];
    if (!av_wifi_ack_json(body, sizeof(body), seq, err, names, count, current)) {
        // Cannot happen within the bounds (10 names of 32 bytes); say so if it does.
        av_wifi_ack_json(body, sizeof(body), seq, AV_WIFI_E_BAD, NULL, 0, "");
    }
    taskENTER_CRITICAL(&s_ack_lock);
    memcpy(s_ack, body, sizeof(s_ack));
    taskEXIT_CRITICAL(&s_ack_lock);
    atomic_store(&s_ack_ready, true);
}

static bool parse(job_t *job)
{
    cJSON *j = cJSON_ParseWithLength((const char *)job->plain, strlen((const char *)job->plain));
    av_wifi_op_t *op = &job->op;
    memset(op, 0, sizeof(*op));
    op->index = -1;
    const cJSON *seq = cJSON_GetObjectItemCaseSensitive(j, "seq");
    const cJSON *kind = cJSON_GetObjectItemCaseSensitive(j, "op");
    const cJSON *index = cJSON_GetObjectItemCaseSensitive(j, "i");
    const cJSON *ssid = cJSON_GetObjectItemCaseSensitive(j, "ssid");
    const cJSON *password = cJSON_GetObjectItemCaseSensitive(j, "password");
    // Whole milliseconds that a double holds exactly.
    bool ok = cJSON_IsObject(j) && cJSON_IsNumber(seq) && seq->valuedouble >= 1 &&
              seq->valuedouble <= 9007199254740991.0 && seq->valuedouble == (double)(uint64_t)seq->valuedouble &&
              cJSON_IsString(kind) && cJSON_IsString(ssid) &&
              strlen(ssid->valuestring) <= AV_WIFI_SSID_MAX * 3;
    if (ok) {
        op->seq = (uint64_t)seq->valuedouble;
        op->kind = av_wifi_op_kind(kind->valuestring);
        // Too long for the field is "bad" rather than silently cut.
        ok = strlen(ssid->valuestring) <= AV_WIFI_SSID_MAX;
        if (ok) strcpy(op->ssid, ssid->valuestring);
        if (cJSON_IsNumber(index) && index->valuedouble >= 0 && index->valuedouble < AV_WIFI_SAVED_MAX) {
            op->index = (int)index->valuedouble;
        }
        if (cJSON_IsString(password)) {
            if (strlen(password->valuestring) > AV_WIFI_PASSWORD_MAX) ok = false;
            else strcpy(op->password, password->valuestring);
        }
    }
    // The password was in cJSON's copy too.
    if (cJSON_IsString(password)) memset(password->valuestring, 0, strlen(password->valuestring));
    cJSON_Delete(j);
    return ok;
}

static void worker_task(void *arg)
{
    job_t *job = arg;
    uint8_t priv[AV_WIFI_KEY_BYTES], pub[AV_WIFI_KEY_BYTES], server[AV_WIFI_KEY_BYTES];
    char addr[96];
    int switch_index = -1;
    const bool keys = atomic_load(&s_server_trusted) && av_store_load_wifi_key(priv, pub) &&
                      av_store_load_server_key(server, addr, sizeof(addr));
    const int n = keys ? av_wifi_crypto_open(priv, server, job->nonce, job->sealed,
                                             job->plain, sizeof(job->plain)) : -1;
    memset(priv, 0, sizeof(priv));
    if (n < 0) {
        // Nothing readable, so no seq to answer: ack 0 means "the last one".
        ESP_LOGW(TAG, "message refused: %s", keys ? "does not decrypt" : "no trusted server key");
        publish_ack(0, AV_WIFI_E_KEY, job);
    } else if (!parse(job)) {
        ESP_LOGW(TAG, "message refused: malformed");
        publish_ack(0, AV_WIFI_E_BAD, job);
    } else {
        const uint64_t last = av_store_load_wifi_seq();
        const unsigned count = av_provision_saved(job->names, AV_WIFI_SAVED_MAX);
        const char *names[AV_WIFI_SAVED_MAX];
        for (unsigned i = 0; i < count; i++) names[i] = job->names[i];
        av_wifi_plan_t plan;
        av_wifi_err_t err = av_wifi_plan(&job->op, last, names, count, &plan);
        // Authentic and newer: never accepted again, whatever happens next.
        if (err != AV_WIFI_E_REPLAY && job->op.seq > last) av_store_save_wifi_seq(job->op.seq);
        if (err == AV_WIFI_OK) {
            bool done = true;
            switch (plan.action) {
            case AV_WIFI_OP_ADD:
                done = av_provision_add_network(job->op.ssid, job->op.password);
                break;
            case AV_WIFI_OP_REMOVE:
                done = av_provision_remove_network((unsigned)plan.index);
                break;
            case AV_WIFI_OP_UP:
                done = av_provision_raise_network((unsigned)plan.index);
                break;
            case AV_WIFI_OP_SWITCH:
                switch_index = plan.index;
                break;
            default:
                break;
            }
            if (!done) err = AV_WIFI_E_STORE;
        }
        ESP_LOGI(TAG, "message %" PRIu64 " op=%d: %s", job->op.seq, (int)job->op.kind, av_wifi_err_code(err));
        publish_ack(job->op.seq, err, job);
    }
    memset(job->op.password, 0, sizeof(job->op.password));
    memset(job->plain, 0, sizeof(job->plain));
    if (switch_index >= 0) {
        // Let the ack leave on the old network first; the session ends with it.
        for (int waited = 0; atomic_load(&s_ack_ready) && waited < SWITCH_ACK_WAIT_MS; waited += 50) {
            vTaskDelay(pdMS_TO_TICKS(50));
        }
        vTaskDelay(pdMS_TO_TICKS(300));
        av_provision_use_network((unsigned)switch_index);
    }
    ESP_LOGI(TAG, "done; stack left=%u heap=%u largest=%u", (unsigned)uxTaskGetStackHighWaterMark(NULL),
             (unsigned)esp_get_free_heap_size(), (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
    free(job);
    atomic_store(&s_busy, false);
    vTaskDelete(NULL);
}

void av_wifimgr_post(const cJSON *envelope)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(envelope, "v");
    const cJSON *nonce = cJSON_GetObjectItemCaseSensitive(envelope, "n");
    const cJSON *sealed = cJSON_GetObjectItemCaseSensitive(envelope, "c");
    if (!atomic_load(&s_ready) || !cJSON_IsNumber(v) || v->valuedouble != 1 ||
        !cJSON_IsString(nonce) || !cJSON_IsString(sealed) ||
        strlen(nonce->valuestring) >= sizeof(((job_t *)0)->nonce) ||
        strlen(sealed->valuestring) > AV_WIFI_SEALED_B64_MAX) {
        ESP_LOGW(TAG, "SWITCH wifi ignored: malformed");
        return;
    }
    if (atomic_exchange(&s_busy, true)) {
        // The phone shows "no answer" and the viewer tries again.
        ESP_LOGW(TAG, "SWITCH wifi ignored: still applying the last one");
        return;
    }
    job_t *job = calloc(1, sizeof(*job));
    if (!job) {
        atomic_store(&s_busy, false);
        ESP_LOGW(TAG, "SWITCH wifi ignored: no memory");
        return;
    }
    strcpy(job->nonce, nonce->valuestring);
    strcpy(job->sealed, sealed->valuestring);
    if (xTaskCreate(worker_task, "av_wifimgr", WORKER_STACK, job, WORKER_PRIO, NULL) != pdPASS) {
        free(job);
        atomic_store(&s_busy, false);
        ESP_LOGW(TAG, "SWITCH wifi ignored: no memory for the worker");
    }
}

// ---- The ack ----

size_t av_wifimgr_ack(char *out, size_t size)
{
    if (!atomic_load(&s_ack_ready) || !out || size == 0) return 0;
    taskENTER_CRITICAL(&s_ack_lock);
    const size_t n = strnlen(s_ack, sizeof(s_ack));
    const bool fits = n < size;
    if (fits) memcpy(out, s_ack, n + 1);
    taskEXIT_CRITICAL(&s_ack_lock);
    return fits ? n : 0;
}

void av_wifimgr_ack_sent(void)
{
    atomic_store(&s_ack_ready, false);
}

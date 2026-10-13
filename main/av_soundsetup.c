// Sound provisioning on the setup screen; see av_soundsetup.h.
#include "av_soundsetup.h"

#if __has_include("av_soundprov.h")
#include "av_soundprov.h"
#define AV_HAVE_SOUNDPROV 1
#else
#define AV_HAVE_SOUNDPROV 0
#endif
#if defined(AV_SOUNDPROV_PRESENT) && !AV_HAVE_SOUNDPROV
#error "AV_SOUNDPROV_PRESENT is set but av_soundprov.h is not found"
#endif

#if AV_HAVE_SOUNDPROV

#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "bsp_mic.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "av_provision.h"
#include "av_wifi_crypto.h"
#include "av_wifimgr.h"

static const char *TAG = "av_soundsetup";

#define RATE ((uint32_t)AV_SOUNDPROV_SAMPLE_RATE)
// 20 ms a read: short enough that stopping is prompt, long enough that the
// decoder is not called for every few samples.
#define CHUNK 320u
// The decoder's own needs are not known here; this leaves it a few kilobytes
// above the read buffer and the logging. Trim to the logged high-water mark.
#define TASK_STACK 6144
#define TASK_PRIO 4
// How long "did not connect" stays on screen before listening again.
#define FAILED_SHOW_US 6000000

// How often the log says how listening is going: progress and the loudest
// sample, which tells "heard nothing" (quiet room, wrong gain, dead
// microphone) from "heard it but could not decode it".
#define LEVEL_LOG_US 2000000

#if CONFIG_AV_SOUNDPROV_DUMP
// The optional console dump (Kconfig AV_SOUNDPROV_DUMP): half a second kept
// in a ring, printed when a message starts, then CONFIG_AV_SOUNDPROV_DUMP_S
// seconds as they arrive. tools/soundprov_dump.py reads it back.
#define DUMP_PREROLL (RATE / 2u)
typedef struct {
    int16_t *ring;
    size_t pos, filled;
    uint32_t left;       // samples still to print; 0 when not dumping
} dump_t;

static void dump_line(const int16_t *pcm, size_t n)
{
    char text[(CHUNK * 2u * 4u + 2u) / 3u + 1u];
    if (av_b64url_encode((const uint8_t *)pcm, n * sizeof(int16_t), text, sizeof(text))) {
        printf("SNDDUMP %s\n", text);
    }
}

static void dump_chunk(dump_t *d, const int16_t *pcm, size_t n, bool started)
{
    if (!d->ring) return;
    if (d->left) {
        const size_t take = n < d->left ? n : d->left;
        dump_line(pcm, take);
        d->left -= (uint32_t)take;
        if (!d->left) printf("SNDDUMP end\n");
        return;
    }
    if (started) {
        printf("SNDDUMP begin rate=%u\n", (unsigned)RATE);
        // The ring, oldest first, in line-sized pieces.
        size_t at = d->filled < DUMP_PREROLL ? 0 : d->pos;
        for (size_t done = 0; done < d->filled;) {
            size_t k = CHUNK;
            if (k > d->filled - done) k = d->filled - done;
            if (k > DUMP_PREROLL - at) k = DUMP_PREROLL - at;
            dump_line(d->ring + at, k);
            done += k;
            at = (at + k) % DUMP_PREROLL;
        }
        d->filled = d->pos = 0;
        d->left = (uint32_t)CONFIG_AV_SOUNDPROV_DUMP_S * RATE;
        dump_chunk(d, pcm, n, false);
        return;
    }
    for (size_t i = 0; i < n; i++) {
        d->ring[d->pos] = pcm[i];
        d->pos = (d->pos + 1u) % DUMP_PREROLL;
        if (d->filled < DUMP_PREROLL) d->filled++;
    }
}
#endif

static atomic_bool s_running;
static atomic_bool s_stop;
static atomic_int s_state = AV_SOUND_OFF;
static atomic_uint s_progress;

static void sound_task(void *arg)
{
    (void)arg;
    av_soundprov_t *decoder = av_soundprov_create();
    const bool mic = decoder && bsp_mic_start(RATE) == ESP_OK;
    if (!mic) {
        ESP_LOGW(TAG, "not listening: %s", decoder ? "no microphone" : "no memory for the decoder");
    } else {
        ESP_LOGI(TAG, "listening; heap=%u largest=%u", (unsigned)esp_get_free_heap_size(),
                 (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
        atomic_store(&s_state, AV_SOUND_LISTENING);
    }
    int16_t pcm[CHUNK];
    int64_t failed_until = 0;
    int64_t next_log = esp_timer_get_time() + LEVEL_LOG_US;
    int peak = 0, last_progress = 0;
#if CONFIG_AV_SOUNDPROV_DUMP
    dump_t dump = { .ring = mic ? malloc(DUMP_PREROLL * sizeof(int16_t)) : NULL };
    if (mic && !dump.ring) ESP_LOGW(TAG, "no memory for the microphone dump");
#endif
    while (mic && !atomic_load(&s_stop)) {
        const int n = bsp_mic_read(pcm, CHUNK, 100);
        if (n < 0) {
            ESP_LOGW(TAG, "microphone read failed; stopping");
            break;
        }
        for (int i = 0; i < n; i++) {
            const int v = pcm[i] < 0 ? -(int)pcm[i] : pcm[i];
            if (v > peak) peak = v;
        }
        if (esp_timer_get_time() >= next_log) {
            ESP_LOGI(TAG, "progress=%d peak=%d state=%d", av_soundprov_progress(decoder), peak,
                     atomic_load(&s_state));
            peak = 0;
            next_log = esp_timer_get_time() + LEVEL_LOG_US;
        }
        if (failed_until && esp_timer_get_time() >= failed_until) {
            failed_until = 0;
            av_soundprov_reset(decoder);
            last_progress = 0;
            atomic_store(&s_state, AV_SOUND_LISTENING);
        }
        if (n == 0 || failed_until) continue;
        const int decoded = av_soundprov_feed(decoder, pcm, (size_t)n);
        const int progress = av_soundprov_progress(decoder);
#if CONFIG_AV_SOUNDPROV_DUMP
        dump_chunk(&dump, pcm, (size_t)n, last_progress == 0 && progress > 0);
#endif
        if (progress > 0 && last_progress == 0) ESP_LOGI(TAG, "a message is arriving");
        last_progress = progress;
        atomic_store(&s_progress, progress < 0 ? 0u : progress > 100 ? 100u : (unsigned)progress);
        if (decoded != 1) {
            atomic_store(&s_state, progress > 0 ? AV_SOUND_RECEIVING : AV_SOUND_LISTENING);
            continue;
        }
        // The result counts when the call did not report an error (negative)
        // and a network name came out of it; that holds whether the decoder
        // answers 0 or 1 for success. Bounded whatever it wrote.
        av_soundprov_result_t result;
        memset(&result, 0, sizeof(result));
        const int got = av_soundprov_result(decoder, &result);
        result.ssid[sizeof(result.ssid) - 1] = '\0';
        result.password[sizeof(result.password) - 1] = '\0';
        result.server[sizeof(result.server) - 1] = '\0';
        if (got < 0 || !result.ssid[0]) {
            memset(&result, 0, sizeof(result));
            av_soundprov_reset(decoder);
            last_progress = 0;
            continue;
        }
        atomic_store(&s_state, AV_SOUND_CONNECTING);
        ESP_LOGI(TAG, "received a network (%u-byte name%s); trying it",
                 (unsigned)strlen(result.ssid), result.server[0] ? ", with a server" : "");
        // The same steps as the hotspot page's form: join, save, server, done.
        const bool joined = av_provision_connect_and_save(result.ssid, result.password, result.server);
        memset(&result, 0, sizeof(result));
        if (joined) {
            atomic_store(&s_state, AV_SOUND_DONE);
            av_wifimgr_forget_server_key();
            // The setup loop sees setup end and restarts into the new network.
            av_provision_finish_setup();
            break;
        }
        ESP_LOGW(TAG, "the received network could not be joined");
        atomic_store(&s_state, AV_SOUND_FAILED);
        failed_until = esp_timer_get_time() + FAILED_SHOW_US;
    }
    if (mic) bsp_mic_stop();
#if CONFIG_AV_SOUNDPROV_DUMP
    if (dump.left) printf("SNDDUMP end\n");
    free(dump.ring);
#endif
    if (decoder) av_soundprov_destroy(decoder);
    ESP_LOGI(TAG, "stopped; stack left=%u", (unsigned)uxTaskGetStackHighWaterMark(NULL));
    if (atomic_load(&s_state) != AV_SOUND_DONE) atomic_store(&s_state, AV_SOUND_OFF);
    atomic_store(&s_running, false);
    vTaskDelete(NULL);
}

bool av_soundsetup_available(void)
{
    return true;
}

bool av_soundsetup_start(void)
{
    if (atomic_load(&s_running)) return true;
    atomic_store(&s_stop, false);
    atomic_store(&s_progress, 0u);
    atomic_store(&s_state, AV_SOUND_OFF);
    atomic_store(&s_running, true);
    if (xTaskCreate(sound_task, "av_sound", TASK_STACK, NULL, TASK_PRIO, NULL) != pdPASS) {
        atomic_store(&s_running, false);
        ESP_LOGW(TAG, "no memory for the sound task");
        return false;
    }
    return true;
}

void av_soundsetup_stop(void)
{
    atomic_store(&s_stop, true);
    // A join in progress takes up to ~25 s; normally this is one read.
    for (int waited = 0; atomic_load(&s_running) && waited < 30000; waited += 20) {
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    if (atomic_load(&s_running)) ESP_LOGW(TAG, "sound task did not stop in time");
}

av_sound_state_t av_soundsetup_state(unsigned *progress)
{
    if (progress) *progress = atomic_load(&s_progress);
    return (av_sound_state_t)atomic_load(&s_state);
}

#else  // no decoder in this tree

bool av_soundsetup_available(void) { return false; }
bool av_soundsetup_start(void) { return false; }
void av_soundsetup_stop(void) {}
av_sound_state_t av_soundsetup_state(unsigned *progress)
{
    if (progress) *progress = 0;
    return AV_SOUND_OFF;
}

#endif

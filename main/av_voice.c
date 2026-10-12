// main/av_voice.c -- see av_voice.h.
#include "av_voice.h"
#include "av_adpcm.h"
#include "av_settings.h"
#include "bsp_mic.h"
#include "cJSON.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"
#include <inttypes.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *TAG = "av_voice";

// 0.96 s of blocks: the press-to-hold pre-roll (about eleven blocks) and room
// for a TCP stall while streaming. 24 x 328 B.
#define RING 24u
// The microphone is read 20 ms at a time.
#define CHUNK 320u
// After release, people are still finishing their last syllable and capture
// runs a little behind real time: three more blocks (120 ms), as Muse does.
#define TAIL_BLOCKS 3u
// Above video (5), beside the receive task and the session owner, below audio
// (7): the capture must not starve while a busy picture is still decoding in
// the first 0.45 s, and I2S DMA holds only 90 ms.
#define VOICE_TASK_PRIO 6
#if CONFIG_IDF_TARGET_ESP32S3
#define VOICE_TASK_STACK 4096
#else
#define VOICE_TASK_STACK 3072
#endif
// 80 Hz one-pole high-pass at 16 kHz, as Muse: a = rc / (rc + dt), Q15.
// rc = 1 / (2 pi 80) = 1.989 ms, dt = 62.5 us, a = 0.96953.
#define HP_A_Q15 31770

typedef struct {
    uint32_t pts;
    uint8_t data[AV_AUDIO_BYTES];
} slot_t;

// Everything an utterance needs, allocated at OK down and freed when it is over.
typedef struct {
    slot_t ring[RING];
    int16_t block[AV_AUDIO_SAMPLES];
    int16_t chunk[CHUNK];
    av_adpcm_state_t enc;
    int32_t hp_x1, hp_y1;
    unsigned fill;
    unsigned peak;      // loudest sample since capture started, for the log
    uint8_t scratch[AV_AUDIO_BYTES];   // where a block nobody has room for is encoded
} cap_t;

typedef enum { FLIGHT_NONE = 0, FLIGHT_CANCEL, FLIGHT_PICK, FLIGHT_BEGIN, FLIGHT_BLOCK, FLIGHT_END } flight_t;

static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
static cap_t *s_cap;
// Guarded by s_lock: the ring indices (absolute counts), what is owed to the
// server, and what the receive task is sending right now.
static uint32_t s_head, s_tail;
static bool s_task_running, s_streaming;
static bool s_begin_pending, s_end_pending, s_cancel_pending, s_pick_pending;
static uint32_t s_utter_id, s_cancel_id, s_pick_id;
static char s_pick_ref[32];
static flight_t s_in_flight;
static uint32_t s_dropped;
// The control message being sent: written by the receive task only.
static char s_ctl[96];
// Set by the owner, read by the voice task.
static atomic_bool s_abort, s_finishing, s_captured;
static atomic_uint s_blocks, s_stop_at;
// The loudness of the latest blocks, for the bars.
static uint8_t s_levels[UI_VOICE_BARS];
static atomic_uint s_level_pos;
static int64_t s_started_us;
// The newest result, handed from the receive task to the owner.
static av_voice_reply_t *s_reply;

static unsigned loudness(const int16_t *pcm, unsigned n, unsigned *peak_out)
{
    unsigned peak = 0;
    for (unsigned i = 0; i < n; i++) {
        const int v = pcm[i];
        const unsigned m = (unsigned)(v < 0 ? -v : v);
        if (m > peak) peak = m;
    }
    if (peak_out && peak > *peak_out) *peak_out = peak;
    // A square-root scale so speech shows over the room, as the listening
    // screen's bars do for the TV's sound.
    unsigned root = 0;
    while ((root + 1u) * (root + 1u) <= peak) root++;
    const unsigned level = root * 255u / 181u;
    return level > 255u ? 255u : level;
}

// One block is full: encode it into the ring.
static void block_done(cap_t *c)
{
    const unsigned index = atomic_load(&s_blocks);
    const unsigned level = loudness(c->block, AV_AUDIO_SAMPLES, &c->peak);
    const unsigned pos = atomic_load(&s_level_pos);
    s_levels[pos % UI_VOICE_BARS] = (uint8_t)level;
    atomic_store(&s_level_pos, pos + 1u);
    bool keep = true;
    taskENTER_CRITICAL(&s_lock);
    if (s_head - s_tail >= RING) {
        if (!s_streaming && s_in_flight == FLIGHT_NONE) {
            s_tail++;            // still the pre-roll: keep the newest
        } else {
            keep = false;        // streaming and stalled: this block is lost
            s_dropped++;
        }
    }
    slot_t *slot = &c->ring[s_head % RING];
    taskEXIT_CRITICAL(&s_lock);
    // The encoder runs on every block, kept or not, so the stream's state stays
    // continuous; each block's header carries it anyway.
    av_adpcm_encode(&c->enc, c->block, AV_AUDIO_SAMPLES, keep ? slot->data : c->scratch);
    if (keep) {
        slot->pts = index * AV_AUDIO_MS;
        taskENTER_CRITICAL(&s_lock);
        s_head++;
        taskEXIT_CRITICAL(&s_lock);
    }
    atomic_store(&s_blocks, index + 1u);
}

static void voice_task(void *arg)
{
    (void)arg;
    cap_t *c = s_cap;
    esp_err_t e = bsp_mic_start(AV_VOICE_RATE);
    if (e != ESP_OK) {
        ESP_LOGW(TAG, "microphone did not start: %s", esp_err_to_name(e));
    } else {
        while (!atomic_load(&s_abort)) {
            const unsigned blocks = atomic_load(&s_blocks);
            if (blocks >= atomic_load(&s_stop_at) || blocks >= AV_VOICE_BLOCKS_MAX) break;
            const int n = bsp_mic_read(c->chunk, CHUNK, 100);
            if (n < 0) {
                ESP_LOGW(TAG, "microphone read failed");
                break;
            }
            for (int i = 0; i < n; i++) {
                // y = a * (y1 + x - x1): DC and rumble out, speech untouched.
                const int32_t x = c->chunk[i];
                const int64_t acc = (int64_t)HP_A_Q15 * (int64_t)(c->hp_y1 + x - c->hp_x1);
                int32_t y = (int32_t)(acc >> 15);
                c->hp_x1 = x;
                c->hp_y1 = y;
                if (y > 32767) y = 32767;
                if (y < -32768) y = -32768;
                c->block[c->fill++] = (int16_t)y;
                if (c->fill == AV_AUDIO_SAMPLES) {
                    c->fill = 0;
                    block_done(c);
                }
            }
        }
        bsp_mic_stop();
    }
    const unsigned blocks = atomic_load(&s_blocks);
    // No words, only the figures that say whether the microphone works: how
    // much was captured, how loud the loudest moment was, what was lost.
    ESP_LOGI(TAG, "capture %s: blocks=%u (%u ms) peak=%u dropped=%" PRIu32 " stack_free=%u heap=%u min_heap=%u",
             atomic_load(&s_abort) ? "dropped" : "done", blocks, blocks * (unsigned)AV_AUDIO_MS, c->peak,
             s_dropped, (unsigned)uxTaskGetStackHighWaterMark(NULL), (unsigned)esp_get_free_heap_size(),
             (unsigned)esp_get_minimum_free_heap_size());
    const bool told = atomic_load(&s_finishing) && !atomic_load(&s_abort);
    const bool capped = blocks >= AV_VOICE_BLOCKS_MAX && !atomic_load(&s_abort);
    taskENTER_CRITICAL(&s_lock);
    s_task_running = false;
    // Ten seconds reached before anyone said finish: the "end" is owed all the same.
    if (capped && (s_begin_pending || s_streaming)) s_end_pending = true;
    taskEXIT_CRITICAL(&s_lock);
    if (told || capped) atomic_store(&s_captured, true);
    vTaskDelete(NULL);
}

bool av_voice_capture_start(void)
{
    taskENTER_CRITICAL(&s_lock);
    const bool busy = s_task_running || s_in_flight != FLIGHT_NONE;
    if (!busy) {
        // A previous utterance whose blocks or "end" are still owed is given up.
        if ((s_streaming || s_end_pending) && !s_cancel_pending) {
            s_cancel_pending = true;
            s_cancel_id = s_utter_id;
        }
        s_begin_pending = s_end_pending = s_streaming = false;
        s_head = s_tail = 0;
        s_dropped = 0;
    }
    taskEXIT_CRITICAL(&s_lock);
    if (busy) {
        ESP_LOGW(TAG, "previous capture still winding down; not starting");
        return false;
    }
    if (!s_cap) {
        s_cap = heap_caps_malloc(sizeof(cap_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
        if (!s_cap) {
            ESP_LOGW(TAG, "no memory for the microphone: wanted %u, heap=%u largest=%u", (unsigned)sizeof(cap_t),
                     (unsigned)esp_get_free_heap_size(),
                     (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
            return false;
        }
    }
    memset(&s_cap->enc, 0, sizeof(s_cap->enc));
    s_cap->hp_x1 = s_cap->hp_y1 = 0;
    s_cap->fill = 0;
    s_cap->peak = 0;
    memset(s_levels, 0, sizeof(s_levels));
    atomic_store(&s_level_pos, 0u);
    atomic_store(&s_blocks, 0u);
    atomic_store(&s_stop_at, UINT32_MAX);
    atomic_store(&s_abort, false);
    atomic_store(&s_finishing, false);
    atomic_store(&s_captured, false);
    s_started_us = esp_timer_get_time();
    taskENTER_CRITICAL(&s_lock);
    s_task_running = true;
    taskEXIT_CRITICAL(&s_lock);
    if (xTaskCreate(voice_task, "av_voice", VOICE_TASK_STACK, NULL, VOICE_TASK_PRIO, NULL) != pdPASS) {
        taskENTER_CRITICAL(&s_lock);
        s_task_running = false;
        taskEXIT_CRITICAL(&s_lock);
        ESP_LOGW(TAG, "no memory for the voice task; heap=%u largest=%u", (unsigned)esp_get_free_heap_size(),
                 (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
        free(s_cap);
        s_cap = NULL;
        return false;
    }
    ESP_LOGI(TAG, "capture started: buffers=%u heap=%u largest=%u", (unsigned)sizeof(cap_t),
             (unsigned)esp_get_free_heap_size(), (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
    return true;
}

void av_voice_capture_begin(uint32_t id)
{
    taskENTER_CRITICAL(&s_lock);
    s_utter_id = id;
    s_begin_pending = true;
    taskEXIT_CRITICAL(&s_lock);
    ESP_LOGI(TAG, "utterance %" PRIu32 " begins, %u ms after OK down (%u blocks of pre-roll)", id,
             (unsigned)((esp_timer_get_time() - s_started_us) / 1000), atomic_load(&s_blocks));
}

void av_voice_capture_finish(void)
{
    atomic_store(&s_finishing, true);
    unsigned stop = atomic_load(&s_blocks) + TAIL_BLOCKS;
    if (stop > AV_VOICE_BLOCKS_MAX) stop = AV_VOICE_BLOCKS_MAX;
    atomic_store(&s_stop_at, stop);
    taskENTER_CRITICAL(&s_lock);
    if (s_begin_pending || s_streaming) s_end_pending = true;
    const bool running = s_task_running;
    taskEXIT_CRITICAL(&s_lock);
    if (!running) atomic_store(&s_captured, true);
}

// Everything not yet sent is dropped. A block on the wire right now finishes.
static void drop_unsent_locked(void)
{
    s_tail = s_head;
    s_streaming = false;
    s_end_pending = false;
}

void av_voice_capture_discard(void)
{
    atomic_store(&s_abort, true);
    atomic_store(&s_captured, false);
    taskENTER_CRITICAL(&s_lock);
    drop_unsent_locked();
    // "begin" not yet sent: the server never heard of it, so nothing is owed.
    s_begin_pending = false;
    taskEXIT_CRITICAL(&s_lock);
}

void av_voice_send_cancel(uint32_t id)
{
    taskENTER_CRITICAL(&s_lock);
    const bool unsent = s_begin_pending && s_utter_id == id;
    s_begin_pending = false;
    drop_unsent_locked();
    if (!unsent) {
        s_cancel_pending = true;
        s_cancel_id = id;
    }
    taskEXIT_CRITICAL(&s_lock);
}

void av_voice_send_pick(uint32_t id, const char *ref)
{
    if (!ref || !ref[0]) return;
    taskENTER_CRITICAL(&s_lock);
    s_pick_pending = true;
    s_pick_id = id;
    strncpy(s_pick_ref, ref, sizeof(s_pick_ref) - 1);
    s_pick_ref[sizeof(s_pick_ref) - 1] = '\0';
    taskEXIT_CRITICAL(&s_lock);
}

bool av_voice_take_captured(void)
{
    return atomic_exchange(&s_captured, false);
}

void av_voice_poll(bool session_up)
{
    if (!session_up) {
        taskENTER_CRITICAL(&s_lock);
        if (s_in_flight == FLIGHT_NONE) {
            // The socket is gone, and so is everything owed on it.
            s_begin_pending = s_end_pending = s_cancel_pending = s_pick_pending = false;
            drop_unsent_locked();
        }
        taskEXIT_CRITICAL(&s_lock);
    }
    if (!s_cap) return;
    taskENTER_CRITICAL(&s_lock);
    const bool done = !s_task_running && s_in_flight == FLIGHT_NONE && s_tail == s_head &&
                      !s_begin_pending && !s_end_pending;
    if (done) s_head = s_tail = 0;
    taskEXIT_CRITICAL(&s_lock);
    if (!done) return;
    free(s_cap);
    s_cap = NULL;
    ESP_LOGI(TAG, "voice buffers freed; heap=%u min_heap=%u largest=%u", (unsigned)esp_get_free_heap_size(),
             (unsigned)esp_get_minimum_free_heap_size(),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
}

void av_voice_levels(uint8_t out[UI_VOICE_BARS])
{
    const unsigned pos = atomic_load(&s_level_pos);
    for (unsigned k = 0; k < UI_VOICE_BARS; k++) {
        const unsigned back = UI_VOICE_BARS - k;
        out[k] = pos >= back ? s_levels[(pos - back) % UI_VOICE_BARS] : 0u;
    }
}

unsigned av_voice_recorded_ds(void)
{
    return atomic_load(&s_blocks) * (unsigned)AV_AUDIO_MS / 100u;
}

void av_voice_session_reset(void)
{
    taskENTER_CRITICAL(&s_lock);
    s_begin_pending = s_end_pending = s_cancel_pending = s_pick_pending = false;
    if (s_in_flight == FLIGHT_NONE) drop_unsent_locked();
    taskEXIT_CRITICAL(&s_lock);
}

// ---- Uplink ----

bool av_voice_uplink_next(av_voice_packet_t *out)
{
    if (!out) return false;
    int n = 0;
    bool got = false;
    taskENTER_CRITICAL(&s_lock);
    if (s_in_flight != FLIGHT_NONE) {
        // The previous one was never confirmed; nothing new until it is.
    } else if (s_cancel_pending) {
        n = snprintf(s_ctl, sizeof(s_ctl), "{\"op\":\"cancel\",\"id\":%" PRIu32 "}", s_cancel_id);
        s_in_flight = FLIGHT_CANCEL;
    } else if (s_pick_pending) {
        n = snprintf(s_ctl, sizeof(s_ctl), "{\"op\":\"pick\",\"id\":%" PRIu32 ",\"ref\":\"%s\"}", s_pick_id,
                     s_pick_ref);
        s_in_flight = FLIGHT_PICK;
    } else if (s_begin_pending) {
        n = snprintf(s_ctl, sizeof(s_ctl),
                     "{\"op\":\"begin\",\"id\":%" PRIu32 ",\"rate\":%u,\"codec\":\"adpcm\"}", s_utter_id,
                     (unsigned)AV_VOICE_RATE);
        s_in_flight = FLIGHT_BEGIN;
    } else if (s_streaming && s_tail != s_head && s_cap) {
        const slot_t *slot = &s_cap->ring[s_tail % RING];
        out->type = AV_VOICE;
        out->pts = slot->pts;
        out->payload = slot->data;
        out->length = AV_AUDIO_BYTES;
        s_in_flight = FLIGHT_BLOCK;
        got = true;
    } else if (s_end_pending && !s_task_running && s_tail == s_head) {
        n = snprintf(s_ctl, sizeof(s_ctl), "{\"op\":\"end\",\"id\":%" PRIu32 "}", s_utter_id);
        s_in_flight = FLIGHT_END;
    }
    if (n > 0 && n < (int)sizeof(s_ctl)) {
        out->type = AV_VOICE_CTL;
        out->pts = 0;
        out->payload = (const uint8_t *)s_ctl;
        out->length = (uint32_t)n;
        got = true;
    } else if (n != 0) {
        s_in_flight = FLIGHT_NONE;   // cannot happen with these sizes; never send a cut message
        s_pick_pending = false;
    }
    taskEXIT_CRITICAL(&s_lock);
    return got;
}

void av_voice_uplink_done(void)
{
    taskENTER_CRITICAL(&s_lock);
    switch (s_in_flight) {
    case FLIGHT_CANCEL: s_cancel_pending = false; break;
    case FLIGHT_PICK:   s_pick_pending = false; break;
    case FLIGHT_BEGIN:  s_begin_pending = false; s_streaming = true; break;
    case FLIGHT_BLOCK:  if (s_tail != s_head) s_tail++; break;
    case FLIGHT_END:    s_end_pending = false; s_streaming = false; break;
    default: break;
    }
    s_in_flight = FLIGHT_NONE;
    taskEXIT_CRITICAL(&s_lock);
}

bool av_voice_uplink_busy(void)
{
    taskENTER_CRITICAL(&s_lock);
    const bool busy = s_cancel_pending || s_pick_pending || s_begin_pending || s_end_pending ||
                      (s_streaming && (s_task_running || s_tail != s_head));
    taskEXIT_CRITICAL(&s_lock);
    return busy;
}

// ---- Results ----

static void copy_text(char *out, size_t size, const cJSON *item)
{
    out[0] = '\0';
    if (cJSON_IsString(item) && item->valuestring) {
        // Cut on a character boundary: the text is UTF-8 and drawn as such.
        const size_t full = strlen(item->valuestring);
        size_t n = full < size - 1 ? full : size - 1;
        while (n > 0 && n < full && ((unsigned char)item->valuestring[n] & 0xC0u) == 0x80u) n--;
        memcpy(out, item->valuestring, n);
        out[n] = '\0';
    }
}

static int number_in(const cJSON *item, double low, double high)
{
    if (cJSON_IsBool(item)) return cJSON_IsTrue(item) ? 1 : 0;
    if (!cJSON_IsNumber(item) || item->valuedouble < low || item->valuedouble > high) return -1;
    return (int)item->valuedouble;
}

static void parse_do(const cJSON *j, av_voice_do_t *act)
{
    memset(act, 0, sizeof(*act));
    act->volume = act->brightness = act->audio_only = act->pet = -1;
    if (!cJSON_IsObject(j)) return;
    act->present = true;
    const cJSON *channel = cJSON_GetObjectItemCaseSensitive(j, "channel");
    if (cJSON_IsString(channel) && channel->valuestring) {
        const size_t len = strlen(channel->valuestring);
        bool printable = len > 0 && len < AV_CHANNEL_ID_MAX;
        for (size_t i = 0; printable && i < len; i++) {
            const char c = channel->valuestring[i];
            printable = c >= '!' && c <= '~';
        }
        if (printable) memcpy(act->channel, channel->valuestring, len + 1);
    }
    act->volume = number_in(cJSON_GetObjectItemCaseSensitive(j, "volume"), 0, 100);
    act->brightness = number_in(cJSON_GetObjectItemCaseSensitive(j, "brightness"), 0,
                                (double)AV_BRIGHTNESS_COUNT - 1.0);
    act->audio_only = number_in(cJSON_GetObjectItemCaseSensitive(j, "audio_only"), 0, 1e9);
    if (act->audio_only > 1) act->audio_only = 1;
    act->pet = number_in(cJSON_GetObjectItemCaseSensitive(j, "pet"), 0, 255);
    copy_text(act->page, sizeof(act->page), cJSON_GetObjectItemCaseSensitive(j, "page"));
    act->setup = number_in(cJSON_GetObjectItemCaseSensitive(j, "setup"), 0, 1e9) > 0;
}

void av_voice_post_result(char *json, size_t n)
{
    // Depth: the result, "choices", a choice, its "do".
    if (!json || !n || n > AV_VOICE_RESULT_MAX || !av_json_depth_safe(json, n, 4)) {
        ESP_LOGW(TAG, "VOICE_RESULT ignored: json depth/parse guard");
        return;
    }
    json[n] = '\0';
    av_voice_reply_t *r = calloc(1, sizeof(*r));
    if (!r) {
        ESP_LOGW(TAG, "VOICE_RESULT ignored: no memory (%u)", (unsigned)sizeof(*r));
        return;
    }
    const char *end = NULL;
    cJSON *j = cJSON_ParseWithLengthOpts(json, n + 1, &end, true);
    const cJSON *id = cJSON_GetObjectItemCaseSensitive(j, "id");
    if (!cJSON_IsObject(j) || !cJSON_IsNumber(id) || id->valuedouble < 0 || id->valuedouble > 4294967295.0) {
        ESP_LOGW(TAG, "VOICE_RESULT ignored: %s, heap=%u largest=%u", j ? "no id" : "parse failed",
                 (unsigned)esp_get_free_heap_size(), (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
        cJSON_Delete(j);
        free(r);
        return;
    }
    r->id = (uint32_t)id->valuedouble;
    const cJSON *state = cJSON_GetObjectItemCaseSensitive(j, "state");
    r->state = AV_VOICE_STATE_FINAL;
    if (cJSON_IsString(state) && state->valuestring) {
        if (!strcmp(state->valuestring, "partial")) r->state = AV_VOICE_STATE_PARTIAL;
        else if (!strcmp(state->valuestring, "error")) r->state = AV_VOICE_STATE_ERROR;
    }
    copy_text(r->heard, sizeof(r->heard), cJSON_GetObjectItemCaseSensitive(j, "heard"));
    copy_text(r->reply, sizeof(r->reply), cJSON_GetObjectItemCaseSensitive(j, "reply"));
    copy_text(r->intent, sizeof(r->intent), cJSON_GetObjectItemCaseSensitive(j, "intent"));
    copy_text(r->label, sizeof(r->label), cJSON_GetObjectItemCaseSensitive(j, "label"));
    copy_text(r->lead, sizeof(r->lead), cJSON_GetObjectItemCaseSensitive(j, "lead"));
    parse_do(cJSON_GetObjectItemCaseSensitive(j, "do"), &r->act);
    const cJSON *choices = cJSON_GetObjectItemCaseSensitive(j, "choices");
    const cJSON *choice = NULL;
    if (cJSON_IsArray(choices)) {
        cJSON_ArrayForEach(choice, choices) {
            if (r->choice_count >= AV_VOICE_CHOICES_MAX) break;
            if (!cJSON_IsObject(choice)) continue;
            av_voice_choice_t *c = &r->choices[r->choice_count];
            copy_text(c->label, sizeof(c->label), cJSON_GetObjectItemCaseSensitive(choice, "label"));
            copy_text(c->lead, sizeof(c->lead), cJSON_GetObjectItemCaseSensitive(choice, "lead"));
            copy_text(c->ref, sizeof(c->ref), cJSON_GetObjectItemCaseSensitive(choice, "ref"));
            parse_do(cJSON_GetObjectItemCaseSensitive(choice, "do"), &c->act);
            // A choice that neither the device nor the server can carry out is
            // not offered; one with no label could not be told apart.
            if (!c->label[0] || (!c->ref[0] && !c->act.present)) continue;
            // A ref goes back to the server inside a JSON string: keep it to
            // characters that need no escaping.
            bool plain = true;
            for (const char *p = c->ref; *p && plain; p++) plain = *p >= ' ' && *p != '"' && *p != '\\';
            if (!plain) continue;
            r->choice_count++;
        }
    }
    cJSON_Delete(j);
    ESP_LOGI(TAG, "VOICE_RESULT id=%" PRIu32 " state=%d intent=%s do=%d choices=%u", r->id, (int)r->state,
             r->intent[0] ? r->intent : "-", (int)r->act.present, r->choice_count);
    taskENTER_CRITICAL(&s_lock);
    av_voice_reply_t *old = s_reply;
    s_reply = r;
    taskEXIT_CRITICAL(&s_lock);
    free(old);
}

av_voice_reply_t *av_voice_take_reply(void)
{
    taskENTER_CRITICAL(&s_lock);
    av_voice_reply_t *r = s_reply;
    s_reply = NULL;
    taskEXIT_CRITICAL(&s_lock);
    return r;
}

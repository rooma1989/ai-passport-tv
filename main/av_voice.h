// Push-to-talk, the device half (docs/superpowers/specs/2026-10-12-voice-control-design.md):
// the microphone task (capture, 80 Hz high-pass, loudness, IMA ADPCM), the uplink
// ring the receive task drains onto the TV socket, and the server's result.
//
// Who calls what:
//   - the session owner (av_player.c, pump_keys) starts, finishes, discards and
//     frees an utterance, as av_voice_policy.c says, and applies results;
//   - the voice task, created at OK press-down and gone when capture stops,
//     fills the ring;
//   - the receive task, which owns the socket, sends what is queued between
//     packets (av_voice_uplink_*), and hands a parsed AV_VOICE_RESULT over
//     (av_voice_post_reply).
//
// Memory is taken at OK press-down and given back when the utterance is over:
// on the C3 nothing is held while nobody talks but this file's statics (well
// under 200 bytes).
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "av_protocol.h"
#include "ui_menu.h"

// ---- The utterance (session owner) ----

// Start the microphone and the ring. False when there is no memory or no
// microphone; the policy then shows the hold as "unavailable".
bool av_voice_capture_start(void);
// The hold: say "begin" for utterance `id` and stream everything captured so
// far (the pre-roll from press-down) and from now on.
void av_voice_capture_begin(uint32_t id);
// Released, or the ten-second cap: capture a short tail, stop, and send "end"
// after the last block.
void av_voice_capture_finish(void);
// Stop at once and drop every block not yet sent.
void av_voice_capture_discard(void);
// "cancel" for utterance `id` (drops what is unsent). "pick" for a candidate
// that the server carries out (`ref`).
void av_voice_send_cancel(uint32_t id);
void av_voice_send_pick(uint32_t id, const char *ref);
// True once, when the microphone has stopped after a finish (the policy's
// av_voice_captured()).
bool av_voice_take_captured(void);
// Give the memory back once the task has gone and the ring is drained (or the
// session is gone). Call every pass of the owner's loop; cheap when idle.
void av_voice_poll(bool session_up);
// Loudness of the last UI_VOICE_BARS blocks, oldest first, 0..255, and how
// long has been recorded, in tenths of a second.
void av_voice_levels(uint8_t out[UI_VOICE_BARS]);
unsigned av_voice_recorded_ds(void);
// A new session: the server has forgotten the old one's utterances.
void av_voice_session_reset(void);

// ---- The uplink (receive task) ----

typedef struct {
    uint8_t type;            // AV_VOICE_CTL or AV_VOICE
    uint32_t pts;
    const uint8_t *payload;
    uint32_t length;
} av_voice_packet_t;

// The next packet to send, if any; the payload stays valid until
// av_voice_uplink_done(). The receive task sends it whole, then calls done.
bool av_voice_uplink_next(av_voice_packet_t *out);
void av_voice_uplink_done(void);
// Whether anything is queued or still being captured, so the receive task
// should not sit in a long blocking read.
bool av_voice_uplink_busy(void);

// ---- The result ----

typedef struct {
    bool present;                     // the result carried a "do" object (maybe empty)
    char channel[AV_CHANNEL_ID_MAX];  // "" for none
    int volume;                       // -1 for none, else 0..100
    int brightness;                   // -1, else a step index
    int audio_only;                   // -1, 0 or 1
    int pet;                          // -1, else a pet index
    char page[12];                    // "", "status", "close" (the server's two); "list", "brightness", "pets"
    bool setup;
} av_voice_do_t;

typedef struct {
    char label[UI_MENU_NAME_MAX];
    char lead[4];
    char ref[32];                     // carried out by the server ("pick") when set
    av_voice_do_t act;
} av_voice_choice_t;

typedef enum { AV_VOICE_STATE_FINAL = 0, AV_VOICE_STATE_PARTIAL, AV_VOICE_STATE_ERROR } av_voice_reply_state_t;

typedef struct {
    uint32_t id;
    av_voice_reply_state_t state;
    char heard[UI_VOICE_HEARD_MAX];
    char reply[UI_VOICE_STATUS_MAX];
    // The kind of decision ("channel", "volume", ...; "none" is not understood,
    // which gets the low earcon) and, for a single match, the row the panel
    // shows: additions the server made to the design's format.
    char intent[16];
    char label[UI_MENU_NAME_MAX];
    char lead[4];
    av_voice_do_t act;
    unsigned choice_count;
    av_voice_choice_t choices[AV_VOICE_CHOICES_MAX];
} av_voice_reply_t;

// Receive task: parse an AV_VOICE_RESULT payload (`json` is writable and has
// room for a terminator at json[n]) and hand it to the owner. Malformed or
// unaffordable results are logged and dropped: it is advice, like AV_SWITCH.
void av_voice_post_result(char *json, size_t n);
// Session owner: the newest result, or NULL. The caller owns it (free()).
av_voice_reply_t *av_voice_take_reply(void);

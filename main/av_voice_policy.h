// Push-to-talk voice control: what each key does in each state, and when an
// utterance starts, ends, times out or is thrown away. Pure logic, no ESP-IDF:
// the player feeds it gestures, ticks and results, and carries out the effects
// it returns (docs/superpowers/specs/2026-10-12-voice-control-design.md, section
// 1 and the "Decisions" section).
//
//   IDLE --OK down--> ARMED --held 0.45 s--> LISTENING --released--> SENDING
//     ^                 |  click/double/triple: thrown away,         | tail captured
//     |                 v  the gesture means what it meant before    v
//     +---------------- IDLE                  RESULT / CHOOSING <-- WAITING
//
// Capture starts at OK down, so the first syllable is kept even when speech
// starts before the hold is recognised; a click throws those milliseconds away.
// "begin" goes to the server only at the hold. Inside the panel OK held talks
// again, OK double-click goes back a level, ▲ cancels while listening or
// recognising.
//
// Voice off (not built, or a server that never offered it) is `enabled` false:
// every gesture is passed through unchanged, which is today's keys bit for bit.
#pragma once
#include <stdbool.h>
#include <stdint.h>

typedef enum {
    AV_VOICE_IDLE = 0,
    AV_VOICE_ARMED,      // OK is down; the microphone runs; the screen has not changed
    AV_VOICE_LISTENING,  // held: the panel listens and blocks stream to the server
    AV_VOICE_SENDING,    // released (or ten seconds): the tail is captured, then "end"
    AV_VOICE_WAITING,    // "end" is out: recognising, up to the result timeout
    AV_VOICE_SHOWING,     // a result is on screen; which one is av_voice_shown_t
    AV_VOICE_CHOOSING,   // several candidates, picked with the arrows and OK
} av_voice_state_t;

typedef enum {
    AV_VOICE_SHOWN_NONE = 0,
    AV_VOICE_SHOWN_DONE,            // carried out: the words heard and what was done
    AV_VOICE_SHOWN_NOT_UNDERSTOOD,  // the words heard, "didn't catch that", examples
    AV_VOICE_SHOWN_ERROR,           // the server failed or did not answer in time
    AV_VOICE_SHOWN_UNAVAILABLE,     // no session to talk over right now
    AV_VOICE_SHOWN_SETUP,           // asks before entering setup (it ends playback)
} av_voice_shown_t;

typedef enum { AV_VOICE_KEY_OK = 0, AV_VOICE_KEY_UP, AV_VOICE_KEY_DOWN } av_voice_key_t;

typedef enum {
    AV_VOICE_G_PRESS = 0,  // key down
    AV_VOICE_G_CLICK,
    AV_VOICE_G_DOUBLE,
    AV_VOICE_G_TRIPLE,
    AV_VOICE_G_HOLD,       // OK held for the hold threshold
    AV_VOICE_G_RELEASE,    // OK let go after a hold
    AV_VOICE_G_LONG,       // the legacy 1.5 s long press (OK still down)
} av_voice_gesture_t;

// What is on screen when the voice panel is not.
typedef enum {
    AV_VOICE_AT_PLAYING = 0,  // the picture (or a waiting screen), no page open
    AV_VOICE_AT_LIST,         // the channel list
    AV_VOICE_AT_PAGE,         // the status page, the backlight page, the pet picker
} av_voice_where_t;

typedef struct {
    bool enabled;    // voice gestures are on (see the header comment)
    bool reachable;  // a session is up, so an utterance can go to the server now
    av_voice_where_t where;
} av_voice_ctx_t;

// Effects, a bit set. The player carries them out in this order: DISCARD and
// CANCEL first, then PASS / PASS_CLICK, then the rest.
enum {
    AV_VOICE_DO_PASS       = 1u << 0,   // the gesture means what it meant before voice
    AV_VOICE_DO_PASS_CLICK = 1u << 1,   // treat it as an OK click (a page's short hold)
    AV_VOICE_DO_CAPTURE    = 1u << 2,   // start the microphone and mute the TV
    AV_VOICE_DO_DISCARD    = 1u << 3,   // stop the microphone, drop its blocks, unmute
    AV_VOICE_DO_BEGIN      = 1u << 4,   // "begin", then stream the blocks
    AV_VOICE_DO_FINISH     = 1u << 5,   // capture the tail; "end" after the last block
    AV_VOICE_DO_CANCEL     = 1u << 6,   // "cancel" (the server had a "begin")
    AV_VOICE_DO_SHOW       = 1u << 7,   // the panel changed: draw it from the state
    AV_VOICE_DO_CLOSE      = 1u << 8,   // close the panel: back to the picture
    AV_VOICE_DO_LIST       = 1u << 9,   // show the channel list in the panel
    AV_VOICE_DO_BACK       = 1u << 10,  // the list goes back a level (closes at the top)
    AV_VOICE_DO_PICK       = 1u << 11,  // carry out candidate `selected`
    AV_VOICE_DO_SETUP      = 1u << 12,  // enter setup (confirmed on screen)
    AV_VOICE_DO_BEEP_END   = 1u << 13,  // the short rising tone: recording ended
    AV_VOICE_DO_BEEP_FAIL  = 1u << 14,  // the low tone: not understood or failed
    AV_VOICE_DO_UNMUTE     = 1u << 15,  // the TV's sound comes back
};

// Results, as the player classified the server's answer.
typedef enum {
    AV_VOICE_RES_PARTIAL = 0,     // words so far; nothing decided
    AV_VOICE_RES_DONE,            // carried out; the panel shows it briefly
    AV_VOICE_RES_DONE_CLOSE,      // carried out; the panel closes at once (volume, a page)
    AV_VOICE_RES_CHOICES,         // candidates
    AV_VOICE_RES_NOT_UNDERSTOOD,
    AV_VOICE_RES_ERROR,
    AV_VOICE_RES_SETUP,           // ask before entering setup
} av_voice_result_t;

// Timing. Defaults from the design; the player may pass its Kconfig values.
#define AV_VOICE_HOLD_MS_DEFAULT 450u
#define AV_VOICE_MAX_MS_DEFAULT 10000u
#define AV_VOICE_RESULT_TIMEOUT_MS 8000u
// An OK press that never became a click, a hold or a multi-click (a fourth
// click, a key lost in a full queue) is thrown away after this long.
#define AV_VOICE_ARMED_GIVE_UP_MS 1200u
// The tail is captured for about 120 ms after release (people let go on their
// last syllable); if the capture side never says it stopped, carry on anyway.
#define AV_VOICE_SENDING_GIVE_UP_MS 1500u
// A carried-out result stays this long before the usual banner takes over.
#define AV_VOICE_DONE_MS 800u
// A result or a list of candidates left alone closes after this long, like the
// channel list.
#define AV_VOICE_IDLE_CLOSE_MS 12000u

typedef struct {
    av_voice_state_t state;
    av_voice_shown_t shown;
    // Where the conversation started: the list (back returns to it) or the picture.
    bool from_list;
    // What ARMED is armed over: the screen a click acts on if the press does
    // not become a hold. IDLE means the list or the picture (see from_list).
    av_voice_state_t armed_over;
    // Whether the microphone runs in this ARMED (false: no session, the hold
    // shows "unavailable").
    bool capturing;
    // "begin" went out for the current utterance, so ending it needs a "cancel".
    bool begun;
    // A page's OK hold, which is a click when let go before the long press.
    bool page_hold, page_long;
    uint32_t id;           // the utterance, counted from 1
    uint32_t armed_ms;     // since the last OK down in ARMED
    uint32_t captured_ms;  // since the microphone started (the ten-second cap)
    uint32_t state_ms;     // in the current state
    unsigned choices;      // candidates, not counting the "open the list" row
    bool list_row;         // the candidates end with "none of these, open the list"
    unsigned selected;     // highlighted row: candidates, then "open the list"
    uint32_t max_ms;       // the utterance cap
} av_voice_policy_t;

void av_voice_policy_init(av_voice_policy_t *p, uint32_t max_ms);

// One gesture. Returns the effects.
unsigned av_voice_on_key(av_voice_policy_t *p, const av_voice_ctx_t *ctx,
                         av_voice_key_t key, av_voice_gesture_t gesture);

// Time passed. Returns the effects (the cap, timeouts, a result closing).
unsigned av_voice_tick(av_voice_policy_t *p, uint32_t elapsed_ms);

// The microphone has stopped after FINISH (or at the cap) and "end" is queued.
unsigned av_voice_captured(av_voice_policy_t *p);

// A result for utterance `id`. Ignored (0) unless it answers the utterance
// being waited for: one arriving after a cancel, or for an older id, is dropped.
// `choices` is the number of candidates for AV_VOICE_RES_CHOICES, and
// `list_row` whether a last row "none of these, open the channel list" follows
// them (channels yes; the setup confirmation's two choices no).
unsigned av_voice_on_result(av_voice_policy_t *p, uint32_t id, av_voice_result_t result,
                            unsigned choices, bool list_row);

// A candidate that needs the server ("ref") was sent: wait for the new result.
unsigned av_voice_picked_remote(av_voice_policy_t *p);
// A candidate was carried out on the device.
unsigned av_voice_picked_done(av_voice_policy_t *p, bool close_now);

// The session went away (or never came): an utterance in flight is lost.
unsigned av_voice_lost(av_voice_policy_t *p);

// Something else took the screen (a page, setup, a remote switch): forget the
// conversation. Returns DISCARD (and CANCEL) when the microphone was running.
unsigned av_voice_reset(av_voice_policy_t *p);

// Whether the voice panel (not the list) is what the panel shows.
bool av_voice_panel_up(const av_voice_policy_t *p);
// Whether the microphone may be running.
bool av_voice_capturing(const av_voice_policy_t *p);

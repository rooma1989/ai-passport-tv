// Account recovery on the device ("恢复登录"): nobody is signed in anywhere,
// and the device itself is the root of trust. A deliberate gesture on the
// status page asks the server, over this device's authenticated TV session,
// for a one-time owner login link and shows it full screen as a QR code for
// five minutes; any phone camera that scans it opens the link in the browser
// and signs that phone in as the device's owner (server/tenancy/recover.py).
// An unbound device has no owner: the gesture shows its bind code instead.
//
// Pure logic, no ESP-IDF, host-tested in tests/test_av_recover.c:
//
// 1. The gesture (av_rg_*), and how it shares OK with the status page.
//    - N quick OK clicks (BSP_RECOVER_CLICKS, 5 on both boards). The button
//      driver already counts a burst of clicks whose gaps are under its
//      short-press time (180 ms) and reports the count once, 180 ms after the
//      last release: 1 click, 2 double, 3 triple, 4, 5 or more. (A second or
//      later press held 180 ms or longer ends the burst with no report at
//      all.) A burst that is not yet N is held back, not acted on: if OK goes
//      down again within AV_RG_WAIT_MS of the burst's report, the next burst's
//      clicks are added to it (so 2 + 3 is 5). Only when OK stays up that long
//      does the page get the held-back clicks: one is the page's OK click
//      (audio-only), two its OK double-click (the Passport's "back to Muse"),
//      three or four nothing; a further press whose burst never reports drops
//      them too. So an OK click on the status page now acts 180 + 400 ms after
//      release instead of 180 ms, and an abandoned 5-click attempt does
//      nothing at all rather than whatever its first burst meant.
//    - ▲ and OK held together (BSP_RECOVER_CHORD, the Xingzhi only: the
//      Passport's three keys share one ADC ladder with ▲ at 0 ohm, so two keys
//      down read as ▲ alone). Both keys report their own 1.5 s long press; the
//      first one to arrive with the other key still down is the gesture,
//      instead of what that long press means on the page (▲: setup, OK: close).
//      The other key's next ending event (its long press, or a click if it is
//      let go sooner) is then swallowed, within AV_RG_LATCH_MS, so it neither
//      closes the code nor opens setup.
//    Neither gesture exists off the status page; the page's other gestures
//    (▲ held: setup, ▼ held: pets, OK held: close, arrows: close) are passed
//    through unchanged, and an arrow or OK's long press drops held-back clicks.
//
// 2. The screen (av_recover_*): asking, the owner's code with its countdown,
//    the bind code, or why not; and when it goes away.
//
// 3. The address the server sends, checked before it is drawn.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "av_protocol.h"

// ---- 1. The gesture ----

typedef enum { AV_RG_KEY_UP = 0, AV_RG_KEY_DOWN, AV_RG_KEY_OK } av_rg_key_t;

typedef enum {
    AV_RG_EV_PRESS = 0,  // key down (only OK's is looked at)
    AV_RG_EV_CLICKS,     // a burst of `count` clicks ended (click, double, triple, 4, 5+)
    AV_RG_EV_LONG,       // the 1.5 s long press
    AV_RG_EV_OTHER,      // anything else (OK's voice hold and release): not part of it
} av_rg_ev_t;

typedef enum {
    AV_RG_NONE = 0,  // tick: nothing to do
    AV_RG_PASS,      // key: not part of the gesture, carry on as before
    AV_RG_TAKEN,     // key: part of the gesture, or held back for it; nothing else to do
    AV_RG_RECOVER,   // the gesture is complete: open the recovery screen
    AV_RG_CLICK,     // tick: the held-back clicks were one OK click on the status page
    AV_RG_DOUBLE,    // tick: they were an OK double-click on the status page
} av_rg_out_t;

// After a burst's report, how long OK may stay up before the held-back clicks
// are handed back to the page.
#define AV_RG_WAIT_MS 400u
// OK went down again after a burst: how long to wait for that burst's report
// (a press that becomes a long press reports that instead).
#define AV_RG_PRESS_MAX_MS 2500u
// How long the chord's other key may take to report its long press or click.
#define AV_RG_LATCH_MS 2000u

typedef struct {
    unsigned clicks_needed;   // 0: the click gesture is off
    bool chord;               // ▲ + OK held is on
    unsigned count;           // OK clicks held back so far
    bool pending;             // clicks are held back
    bool burst;               // OK went down again since: its count is awaited
    uint32_t deadline_ms;     // when they are handed back (or, during a burst, dropped)
    bool latch;               // the chord's other key still has to report
    av_rg_key_t latch_key;
    uint32_t latch_until_ms;
} av_rg_t;

void av_rg_init(av_rg_t *g, unsigned clicks_needed, bool chord);
// One key event. `on_page`: the status page is up. `count`: the clicks in an
// AV_RG_EV_CLICKS burst. `other_down`: for a long press of ▲ or OK, whether
// the other one of the two is down right now (always false without a chord).
av_rg_out_t av_rg_key(av_rg_t *g, bool on_page, av_rg_key_t key, av_rg_ev_t ev, unsigned count,
                      bool other_down, uint32_t now_ms);
// Time passed: held-back clicks handed back (CLICK, DOUBLE) or dropped.
av_rg_out_t av_rg_tick(av_rg_t *g, uint32_t now_ms);
// Forget held-back clicks (the page closed some other way). The latch stays.
void av_rg_drop(av_rg_t *g);
// Whether anything is held back or latched.
bool av_rg_busy(const av_rg_t *g);

// ---- 2. The screen ----

typedef enum {
    AV_RECOVER_NONE = 0,
    AV_RECOVER_ASKING,    // asked the server (or will, when a session is up)
    AV_RECOVER_SHOWING,   // the owner's code and how long it lasts
    AV_RECOVER_BIND,      // an unbound device: its bind code
    AV_RECOVER_FAILED,    // why not
} av_recover_phase_t;

typedef enum {
    AV_RECOVER_ERR_NONE = 0,
    AV_RECOVER_ERR_OFFLINE,  // no session to ask over
    AV_RECOVER_ERR_OFF,      // this server cannot (older, single-user, no site)
    AV_RECOVER_ERR_LIMITED,  // three an hour per device
    AV_RECOVER_ERR_UNBOUND,  // the server knows no owner, and no bind code is known
    AV_RECOVER_ERR_FAILED,   // anything else, or no answer in time
    AV_RECOVER_ERR_EXPIRED,  // the code's five minutes are up
} av_recover_error_t;

// How long to wait for the answer, a session to send the request on included.
#define AV_RECOVER_ASK_MS 12000u
// How long a "why not" stays.
#define AV_RECOVER_FAILED_MS 8000u
// How long the bind code stays.
#define AV_RECOVER_BIND_MS 120000u
// The longest life the device accepts for a code; 0 or more means this.
#define AV_RECOVER_TTL_DEFAULT_S 300u
#define AV_RECOVER_TTL_MAX_S 600u

typedef struct {
    av_recover_phase_t phase;
    av_recover_error_t error;
    uint32_t since_ms;   // when the phase began
    uint32_t until_ms;   // SHOWING: when the code expires
    bool sent;           // ASKING: the request went out on a session
} av_recover_screen_t;

typedef struct {
    bool unbound;      // the last CONFIG said "bound": 0
    bool bind_code;    // and gave a bind address (remote_url) to draw
    bool offered;      // the last CONFIG said "recover": 1
    bool online;       // a session is configured now
} av_recover_ctx_t;

// The gesture happened. Returns true when the server must be asked (the
// screen is ASKING); otherwise the screen is BIND or FAILED already.
bool av_recover_open(av_recover_screen_t *r, const av_recover_ctx_t *ctx, uint32_t now_ms);
// The request went out on a session.
void av_recover_sent(av_recover_screen_t *r);
// The server's answer: a valid address and its life in seconds, or an error.
// Ignored unless ASKING. Returns true when the screen changed.
bool av_recover_answer(av_recover_screen_t *r, const char *url, unsigned ttl_s, av_recover_error_t error,
                       uint32_t now_ms);

typedef enum { AV_RECOVER_TICK_NONE = 0, AV_RECOVER_TICK_CHANGED, AV_RECOVER_TICK_CLOSE } av_recover_tick_t;
av_recover_tick_t av_recover_tick(av_recover_screen_t *r, uint32_t now_ms);
void av_recover_close(av_recover_screen_t *r);
// SHOWING: whole seconds left, rounded up; 0 otherwise.
unsigned av_recover_seconds_left(const av_recover_screen_t *r, uint32_t now_ms);

// What the screen says: two lines (the first may be an error, see `alarm`).
// The bottom bar's countdown is av_recover_seconds_left() (ui_overlay_recover_bottom).
void av_recover_lines(const av_recover_screen_t *r, const char **line1, const char **line2, bool *alarm);

// ---- 3. The address ----

// "HTTP(S)://.../TV/P/<token>" in upper case, digits and URL punctuation only
// (QR alphanumeric mode), at most AV_RECOVER_URL_MAX characters.
bool av_recover_url_ok(const char *url);
// The server's error word ("limited", "unbound", "off", "fail"); anything
// else is AV_RECOVER_ERR_FAILED.
av_recover_error_t av_recover_error_from(const char *word);

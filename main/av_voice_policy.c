// main/av_voice_policy.c -- see av_voice_policy.h.
#include "av_voice_policy.h"

#include <string.h>

void av_voice_policy_init(av_voice_policy_t *p, uint32_t max_ms)
{
    if (!p) {
        return;
    }
    memset(p, 0, sizeof(*p));
    p->max_ms = max_ms ? max_ms : AV_VOICE_MAX_MS_DEFAULT;
}

bool av_voice_panel_up(const av_voice_policy_t *p)
{
    if (!p) {
        return false;
    }
    switch (p->state) {
    case AV_VOICE_IDLE:
        return false;
    case AV_VOICE_ARMED:
        return p->armed_over == AV_VOICE_SHOWING || p->armed_over == AV_VOICE_CHOOSING;
    default:
        return true;
    }
}

bool av_voice_capturing(const av_voice_policy_t *p)
{
    return p && ((p->state == AV_VOICE_ARMED && p->capturing) || p->state == AV_VOICE_LISTENING ||
                 p->state == AV_VOICE_SENDING);
}

// Back to where the conversation started: the list it was opened from, or the
// picture.
static unsigned rest(av_voice_policy_t *p)
{
    p->state = AV_VOICE_IDLE;
    p->shown = AV_VOICE_SHOWN_NONE;
    p->choices = 0;
    p->selected = 0;
    p->state_ms = 0;
    return p->from_list ? (AV_VOICE_DO_LIST | AV_VOICE_DO_SHOW) : AV_VOICE_DO_CLOSE;
}

static unsigned show(av_voice_policy_t *p, av_voice_state_t state, av_voice_shown_t shown)
{
    p->state = state;
    p->shown = shown;
    p->state_ms = 0;
    return AV_VOICE_DO_SHOW;
}

// Stop an utterance that is under way and go back.
static unsigned cancel(av_voice_policy_t *p)
{
    unsigned e = AV_VOICE_DO_DISCARD;
    if (p->begun) {
        e |= AV_VOICE_DO_CANCEL;
        p->begun = false;
    }
    return e | rest(p);
}

static unsigned arm(av_voice_policy_t *p, const av_voice_ctx_t *ctx, av_voice_state_t over)
{
    if (over == AV_VOICE_IDLE) {
        p->from_list = ctx->where == AV_VOICE_AT_LIST;
    }
    p->armed_over = over;
    p->state = AV_VOICE_ARMED;
    p->capturing = ctx->reachable;
    p->armed_ms = 0;
    p->captured_ms = 0;
    p->state_ms = 0;
    return p->capturing ? AV_VOICE_DO_CAPTURE : 0u;
}

// The press did not become a hold: the screen it was armed over takes the
// gesture, and whatever the microphone heard is thrown away.
static unsigned disarm(av_voice_policy_t *p)
{
    const unsigned e = p->capturing ? AV_VOICE_DO_DISCARD : 0u;
    p->capturing = false;
    p->state = p->armed_over;
    p->armed_over = AV_VOICE_IDLE;
    p->state_ms = 0;
    return e;
}

static unsigned hold(av_voice_policy_t *p)
{
    if (!p->capturing) {
        // Nothing to talk to: say so, and how to get going again.
        return show(p, AV_VOICE_SHOWING, AV_VOICE_SHOWN_UNAVAILABLE);
    }
    p->id++;
    p->begun = true;
    p->choices = 0;
    p->selected = 0;
    show(p, AV_VOICE_LISTENING, AV_VOICE_SHOWN_NONE);
    return AV_VOICE_DO_BEGIN | AV_VOICE_DO_SHOW;
}

static unsigned on_idle(av_voice_policy_t *p, const av_voice_ctx_t *ctx, av_voice_key_t key,
                        av_voice_gesture_t g)
{
    const bool ok = key == AV_VOICE_KEY_OK;
    if (ctx->where == AV_VOICE_AT_PAGE) {
        // The pages keep their gestures. With the hold reported at 0.45 s, a
        // press of OK there that is let go before the long press is what the
        // page always called a click; the long press itself is passed on.
        if (!ok) {
            return g == AV_VOICE_G_PRESS ? 0u : AV_VOICE_DO_PASS;
        }
        switch (g) {
        case AV_VOICE_G_PRESS:
            return 0;
        case AV_VOICE_G_HOLD:
            p->page_hold = true;
            p->page_long = false;
            return 0;
        case AV_VOICE_G_LONG:
            p->page_long = true;
            return AV_VOICE_DO_PASS;
        case AV_VOICE_G_RELEASE: {
            const bool click = p->page_hold && !p->page_long;
            p->page_hold = false;
            return click ? AV_VOICE_DO_PASS_CLICK : 0u;
        }
        default:
            return AV_VOICE_DO_PASS;
        }
    }
    if (!ok) {
        return (g == AV_VOICE_G_PRESS || g == AV_VOICE_G_HOLD || g == AV_VOICE_G_RELEASE)
                   ? 0u : AV_VOICE_DO_PASS;
    }
    switch (g) {
    case AV_VOICE_G_PRESS:
        return arm(p, ctx, AV_VOICE_IDLE);
    case AV_VOICE_G_HOLD: {
        // The press itself was lost (a full key queue): start now.
        const unsigned e = arm(p, ctx, AV_VOICE_IDLE);
        return e | hold(p);
    }
    case AV_VOICE_G_RELEASE:
    case AV_VOICE_G_LONG:
        return 0;
    case AV_VOICE_G_DOUBLE:
        // In the list a double-click goes back a level (closing at the top);
        // while playing it keeps its old meaning.
        return ctx->where == AV_VOICE_AT_LIST ? AV_VOICE_DO_BACK : AV_VOICE_DO_PASS;
    default:
        return AV_VOICE_DO_PASS;
    }
}

static unsigned on_result_screen(av_voice_policy_t *p, const av_voice_ctx_t *ctx,
                                 av_voice_key_t key, av_voice_gesture_t g)
{
    const bool ok = key == AV_VOICE_KEY_OK;
    if (ok) {
        switch (g) {
        case AV_VOICE_G_PRESS:
            return arm(p, ctx, AV_VOICE_SHOWING);
        case AV_VOICE_G_HOLD: {
            const unsigned e = arm(p, ctx, AV_VOICE_SHOWING);
            return e | hold(p);
        }
        case AV_VOICE_G_CLICK:
            if (p->shown == AV_VOICE_SHOWN_SETUP && p->selected == 0) {
                p->state = AV_VOICE_IDLE;
                p->shown = AV_VOICE_SHOWN_NONE;
                p->selected = 0;
                return AV_VOICE_DO_SETUP | AV_VOICE_DO_CLOSE;
            }
            return rest(p);
        case AV_VOICE_G_DOUBLE:
        case AV_VOICE_G_TRIPLE:
            return rest(p);
        default:
            return 0;
        }
    }
    if (g == AV_VOICE_G_PRESS || g == AV_VOICE_G_HOLD || g == AV_VOICE_G_RELEASE) {
        return 0;
    }
    if (p->shown == AV_VOICE_SHOWN_SETUP) {
        if (g == AV_VOICE_G_CLICK) {
            p->selected = p->selected ? 0u : 1u;
            p->state_ms = 0;
            return AV_VOICE_DO_SHOW;
        }
        return 0;
    }
    // Any other key puts a message away.
    return rest(p);
}

static unsigned on_choosing(av_voice_policy_t *p, const av_voice_ctx_t *ctx, av_voice_key_t key,
                            av_voice_gesture_t g)
{
    const bool ok = key == AV_VOICE_KEY_OK;
    if (!ok) {
        if (g != AV_VOICE_G_CLICK) {
            return 0;
        }
        const unsigned rows = p->choices + (p->list_row ? 1u : 0u);   // then "open the list"
        p->selected = key == AV_VOICE_KEY_UP ? (p->selected + rows - 1u) % rows
                                             : (p->selected + 1u) % rows;
        p->state_ms = 0;
        return AV_VOICE_DO_SHOW;
    }
    switch (g) {
    case AV_VOICE_G_PRESS:
        return arm(p, ctx, AV_VOICE_CHOOSING);
    case AV_VOICE_G_HOLD: {
        const unsigned e = arm(p, ctx, AV_VOICE_CHOOSING);
        return e | hold(p);
    }
    case AV_VOICE_G_CLICK:
        if (p->list_row && p->selected >= p->choices) {
            // "None of these": the ordinary channel list, in the same panel.
            p->from_list = true;
            return rest(p);
        }
        return AV_VOICE_DO_PICK;
    case AV_VOICE_G_DOUBLE:
        return rest(p);
    default:
        return 0;
    }
}

unsigned av_voice_on_key(av_voice_policy_t *p, const av_voice_ctx_t *ctx,
                         av_voice_key_t key, av_voice_gesture_t g)
{
    if (!p || !ctx || !ctx->enabled) {
        return AV_VOICE_DO_PASS;
    }
    const bool ok = key == AV_VOICE_KEY_OK;
    switch (p->state) {
    case AV_VOICE_IDLE:
        return on_idle(p, ctx, key, g);
    case AV_VOICE_ARMED:
        if (ok && g == AV_VOICE_G_PRESS) {
            // The next press of a double or triple click.
            p->armed_ms = 0;
            return 0;
        }
        if (ok && g == AV_VOICE_G_HOLD) {
            return hold(p);
        }
        if (ok && (g == AV_VOICE_G_RELEASE || g == AV_VOICE_G_LONG)) {
            return 0;
        }
        if (!ok && (g == AV_VOICE_G_PRESS || g == AV_VOICE_G_HOLD || g == AV_VOICE_G_RELEASE)) {
            return 0;
        }
        {
            // A click, a double or triple click, or an arrow: not talking. The
            // screen underneath takes the gesture as it always has.
            const unsigned e = disarm(p);
            return e | av_voice_on_key(p, ctx, key, g);
        }
    case AV_VOICE_LISTENING:
        if (ok && g == AV_VOICE_G_RELEASE) {
            p->state = AV_VOICE_SENDING;
            p->state_ms = 0;
            return AV_VOICE_DO_FINISH | AV_VOICE_DO_SHOW;
        }
        if (key == AV_VOICE_KEY_UP && g == AV_VOICE_G_CLICK) {
            return cancel(p);
        }
        return 0;
    case AV_VOICE_SENDING:
    case AV_VOICE_WAITING:
        if ((key == AV_VOICE_KEY_UP && g == AV_VOICE_G_CLICK) || (ok && g == AV_VOICE_G_DOUBLE)) {
            return cancel(p);
        }
        return 0;
    case AV_VOICE_SHOWING:
        return on_result_screen(p, ctx, key, g);
    case AV_VOICE_CHOOSING:
        return on_choosing(p, ctx, key, g);
    }
    return 0;
}

unsigned av_voice_captured(av_voice_policy_t *p)
{
    if (!p || (p->state != AV_VOICE_SENDING && p->state != AV_VOICE_LISTENING)) {
        return 0;
    }
    show(p, AV_VOICE_WAITING, AV_VOICE_SHOWN_NONE);
    return AV_VOICE_DO_BEEP_END | AV_VOICE_DO_UNMUTE | AV_VOICE_DO_SHOW;
}

unsigned av_voice_tick(av_voice_policy_t *p, uint32_t elapsed_ms)
{
    if (!p || !elapsed_ms) {
        return 0;
    }
    p->state_ms += elapsed_ms;
    switch (p->state) {
    case AV_VOICE_ARMED:
        p->armed_ms += elapsed_ms;
        if (p->capturing) {
            p->captured_ms += elapsed_ms;
        }
        if (p->armed_ms >= AV_VOICE_ARMED_GIVE_UP_MS) {
            return disarm(p);
        }
        return 0;
    case AV_VOICE_LISTENING:
        p->captured_ms += elapsed_ms;
        if (p->captured_ms >= p->max_ms) {
            p->state = AV_VOICE_SENDING;
            p->state_ms = 0;
            return AV_VOICE_DO_FINISH | AV_VOICE_DO_SHOW;
        }
        return 0;
    case AV_VOICE_SENDING:
        if (p->state_ms >= AV_VOICE_SENDING_GIVE_UP_MS) {
            return av_voice_captured(p);
        }
        return 0;
    case AV_VOICE_WAITING:
        if (p->state_ms >= AV_VOICE_RESULT_TIMEOUT_MS) {
            p->begun = false;
            return show(p, AV_VOICE_SHOWING, AV_VOICE_SHOWN_ERROR) | AV_VOICE_DO_BEEP_FAIL;
        }
        return 0;
    case AV_VOICE_SHOWING:
        if (p->shown == AV_VOICE_SHOWN_DONE && p->state_ms >= AV_VOICE_DONE_MS) {
            // Carried out: the picture (and its banner) takes over.
            p->from_list = false;
            return rest(p);
        }
        if (p->state_ms >= AV_VOICE_IDLE_CLOSE_MS) {
            p->from_list = false;
            return rest(p);
        }
        return 0;
    case AV_VOICE_CHOOSING:
        if (p->state_ms >= AV_VOICE_IDLE_CLOSE_MS) {
            p->from_list = false;
            return rest(p);
        }
        return 0;
    default:
        return 0;
    }
}

unsigned av_voice_on_result(av_voice_policy_t *p, uint32_t id, av_voice_result_t result,
                            unsigned choices, bool list_row)
{
    if (!p || id != p->id || (p->state != AV_VOICE_WAITING && p->state != AV_VOICE_SENDING)) {
        return 0;
    }
    // A result before the capture side said it stopped: the sound comes back.
    const unsigned early = p->state == AV_VOICE_SENDING ? AV_VOICE_DO_UNMUTE : 0u;
    if (result == AV_VOICE_RES_PARTIAL) {
        return AV_VOICE_DO_SHOW;
    }
    p->begun = false;
    switch (result) {
    case AV_VOICE_RES_DONE:
        return early | show(p, AV_VOICE_SHOWING, AV_VOICE_SHOWN_DONE);
    case AV_VOICE_RES_DONE_CLOSE:
        p->from_list = false;
        return early | rest(p);
    case AV_VOICE_RES_CHOICES:
        if (choices) {
            p->choices = choices;
            p->list_row = list_row;
            p->selected = 0;
            return early | show(p, AV_VOICE_CHOOSING, AV_VOICE_SHOWN_NONE);
        }
        return early | show(p, AV_VOICE_SHOWING, AV_VOICE_SHOWN_NOT_UNDERSTOOD) | AV_VOICE_DO_BEEP_FAIL;
    case AV_VOICE_RES_NOT_UNDERSTOOD:
        return early | show(p, AV_VOICE_SHOWING, AV_VOICE_SHOWN_NOT_UNDERSTOOD) | AV_VOICE_DO_BEEP_FAIL;
    case AV_VOICE_RES_SETUP:
        p->selected = 0;
        return early | show(p, AV_VOICE_SHOWING, AV_VOICE_SHOWN_SETUP);
    case AV_VOICE_RES_ERROR:
    default:
        return early | show(p, AV_VOICE_SHOWING, AV_VOICE_SHOWN_ERROR) | AV_VOICE_DO_BEEP_FAIL;
    }
}

unsigned av_voice_picked_remote(av_voice_policy_t *p)
{
    if (!p || p->state != AV_VOICE_CHOOSING) {
        return 0;
    }
    return show(p, AV_VOICE_WAITING, AV_VOICE_SHOWN_NONE);
}

unsigned av_voice_picked_done(av_voice_policy_t *p, bool close_now)
{
    if (!p || p->state != AV_VOICE_CHOOSING) {
        return 0;
    }
    if (close_now) {
        p->from_list = false;
        return rest(p);
    }
    return show(p, AV_VOICE_SHOWING, AV_VOICE_SHOWN_DONE);
}

unsigned av_voice_lost(av_voice_policy_t *p)
{
    if (!p) {
        return 0;
    }
    switch (p->state) {
    case AV_VOICE_ARMED:
        if (p->capturing) {
            p->capturing = false;
            return AV_VOICE_DO_DISCARD;
        }
        return 0;
    case AV_VOICE_LISTENING:
    case AV_VOICE_SENDING:
    case AV_VOICE_WAITING:
        p->begun = false;
        return AV_VOICE_DO_DISCARD | show(p, AV_VOICE_SHOWING, AV_VOICE_SHOWN_ERROR);
    default:
        return 0;
    }
}

unsigned av_voice_reset(av_voice_policy_t *p)
{
    if (!p) {
        return 0;
    }
    unsigned e = av_voice_capturing(p) ? AV_VOICE_DO_DISCARD : 0u;
    if (p->begun) {
        e |= AV_VOICE_DO_CANCEL | AV_VOICE_DO_DISCARD;
    }
    const uint32_t id = p->id, max_ms = p->max_ms;
    memset(p, 0, sizeof(*p));
    p->id = id;
    p->max_ms = max_ms;
    return e;
}

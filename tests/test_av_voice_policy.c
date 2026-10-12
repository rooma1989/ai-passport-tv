// Host tests for the push-to-talk state machine (main/av_voice_policy.c): every
// key in every state, click against hold at the threshold, cancel, the
// timeouts, and a result that arrives after a cancel.
#include "av_voice_policy.h"

#include <stdio.h>
#include <string.h>

static int failures;

static void check(int condition, const char *what)
{
    if (!condition) {
        printf("FAIL: %s\n", what);
        failures++;
    }
}

static const av_voice_ctx_t PLAYING = { true, true, AV_VOICE_AT_PLAYING };
static const av_voice_ctx_t LIST = { true, true, AV_VOICE_AT_LIST };
static const av_voice_ctx_t PAGE = { true, true, AV_VOICE_AT_PAGE };
static const av_voice_ctx_t OFFLINE = { true, false, AV_VOICE_AT_PLAYING };
static const av_voice_ctx_t OFF = { false, true, AV_VOICE_AT_PLAYING };

static av_voice_policy_t fresh(void)
{
    av_voice_policy_t p;
    av_voice_policy_init(&p, 0);
    return p;
}

static unsigned ok(av_voice_policy_t *p, const av_voice_ctx_t *c, av_voice_gesture_t g)
{
    return av_voice_on_key(p, c, AV_VOICE_KEY_OK, g);
}

// OK down and held: listening, with "begin" out.
static av_voice_policy_t listening_from(const av_voice_ctx_t *c)
{
    av_voice_policy_t p = fresh();
    ok(&p, c, AV_VOICE_G_PRESS);
    ok(&p, c, AV_VOICE_G_HOLD);
    return p;
}

static av_voice_policy_t waiting_from(const av_voice_ctx_t *c)
{
    av_voice_policy_t p = listening_from(c);
    ok(&p, c, AV_VOICE_G_RELEASE);
    av_voice_captured(&p);
    return p;
}

static void test_voice_off_passes_everything(void)
{
    av_voice_policy_t p = fresh();
    const av_voice_gesture_t all[] = { AV_VOICE_G_PRESS, AV_VOICE_G_CLICK, AV_VOICE_G_DOUBLE,
                                       AV_VOICE_G_TRIPLE, AV_VOICE_G_HOLD, AV_VOICE_G_RELEASE,
                                       AV_VOICE_G_LONG };
    for (unsigned k = 0; k < 3; k++) {
        for (unsigned g = 0; g < sizeof(all) / sizeof(all[0]); g++) {
            check(av_voice_on_key(&p, &OFF, (av_voice_key_t)k, all[g]) == AV_VOICE_DO_PASS,
                  "voice off: every gesture is today's");
        }
    }
    check(p.state == AV_VOICE_IDLE, "voice off: nothing starts");
}

static void test_press_starts_capture_click_discards(void)
{
    av_voice_policy_t p = fresh();
    check(ok(&p, &PLAYING, AV_VOICE_G_PRESS) == AV_VOICE_DO_CAPTURE, "OK down starts the microphone");
    check(p.state == AV_VOICE_ARMED && av_voice_capturing(&p), "armed and capturing");
    check(!av_voice_panel_up(&p), "nothing on screen changes at press-down");
    unsigned e = ok(&p, &PLAYING, AV_VOICE_G_CLICK);
    check(e == (AV_VOICE_DO_DISCARD | AV_VOICE_DO_PASS), "a click throws the capture away and opens the list as before");
    check(p.state == AV_VOICE_IDLE && !av_voice_capturing(&p), "back to idle");
    check(p.id == 0, "no utterance was numbered for a click");
}

static void test_click_in_list_confirms_row(void)
{
    av_voice_policy_t p = fresh();
    ok(&p, &LIST, AV_VOICE_G_PRESS);
    check(p.from_list, "the press came from the list");
    check(ok(&p, &LIST, AV_VOICE_G_CLICK) == (AV_VOICE_DO_DISCARD | AV_VOICE_DO_PASS),
          "in the list a click still confirms the row");
}

static void test_double_in_list_goes_back(void)
{
    av_voice_policy_t p = fresh();
    ok(&p, &LIST, AV_VOICE_G_PRESS);
    ok(&p, &LIST, AV_VOICE_G_PRESS);   // the second press of the double click
    check(p.state == AV_VOICE_ARMED, "still armed between the presses");
    check(ok(&p, &LIST, AV_VOICE_G_DOUBLE) == (AV_VOICE_DO_DISCARD | AV_VOICE_DO_BACK),
          "double-click in the list goes back a level");
    check(ok(&p, &PLAYING, AV_VOICE_G_PRESS) == AV_VOICE_DO_CAPTURE, "re-armed");
    check(ok(&p, &PLAYING, AV_VOICE_G_DOUBLE) == (AV_VOICE_DO_DISCARD | AV_VOICE_DO_PASS),
          "double-click while playing keeps its old meaning");
    ok(&p, &PLAYING, AV_VOICE_G_PRESS);
    check(ok(&p, &PLAYING, AV_VOICE_G_TRIPLE) == (AV_VOICE_DO_DISCARD | AV_VOICE_DO_PASS),
          "triple-click is passed on (the Xingzhi's backlight page)");
}

static void test_hold_begins(void)
{
    av_voice_policy_t p = fresh();
    ok(&p, &PLAYING, AV_VOICE_G_PRESS);
    unsigned e = ok(&p, &PLAYING, AV_VOICE_G_HOLD);
    check(e == (AV_VOICE_DO_BEGIN | AV_VOICE_DO_SHOW), "the hold begins the utterance and shows listening");
    check(p.state == AV_VOICE_LISTENING && p.id == 1 && p.begun, "listening, utterance 1");
    check(av_voice_panel_up(&p), "the panel is up");
    check(ok(&p, &PLAYING, AV_VOICE_G_LONG) == 0, "the 1.5 s long press means nothing while talking");
    check(av_voice_on_key(&p, &PLAYING, AV_VOICE_KEY_DOWN, AV_VOICE_G_CLICK) == 0, "▼ does nothing while listening");
    e = ok(&p, &PLAYING, AV_VOICE_G_RELEASE);
    check(e == (AV_VOICE_DO_FINISH | AV_VOICE_DO_SHOW), "release finishes");
    check(p.state == AV_VOICE_SENDING && av_voice_capturing(&p), "the tail is still captured");
    e = av_voice_captured(&p);
    check(e == (AV_VOICE_DO_BEEP_END | AV_VOICE_DO_UNMUTE | AV_VOICE_DO_SHOW), "captured: beep, sound back");
    check(p.state == AV_VOICE_WAITING && !av_voice_capturing(&p), "waiting for the result");
    check(av_voice_captured(&p) == 0, "captured twice is nothing");
}

static void test_hold_threshold_against_click(void)
{
    // The BSP decides click against hold; the policy must accept either order
    // of the events that can follow a press and never start twice.
    av_voice_policy_t p = fresh();
    ok(&p, &PLAYING, AV_VOICE_G_PRESS);
    av_voice_tick(&p, 449);
    check(p.state == AV_VOICE_ARMED, "just under the hold: still armed");
    check(p.captured_ms == 449, "the capture counts from press-down");
    ok(&p, &PLAYING, AV_VOICE_G_HOLD);
    check(p.state == AV_VOICE_LISTENING, "held");
    av_voice_tick(&p, 100);
    check(p.captured_ms == 549, "the cap counts the pre-roll too");
    check(ok(&p, &PLAYING, AV_VOICE_G_CLICK) == 0, "a click cannot follow a hold");
}

static void test_hold_without_press(void)
{
    av_voice_policy_t p = fresh();
    unsigned e = ok(&p, &PLAYING, AV_VOICE_G_HOLD);
    check(e == (AV_VOICE_DO_CAPTURE | AV_VOICE_DO_BEGIN | AV_VOICE_DO_SHOW), "a lost press still talks");
}

static void test_armed_gives_up(void)
{
    av_voice_policy_t p = fresh();
    ok(&p, &PLAYING, AV_VOICE_G_PRESS);
    check(av_voice_tick(&p, AV_VOICE_ARMED_GIVE_UP_MS - 1) == 0, "not yet");
    ok(&p, &PLAYING, AV_VOICE_G_PRESS);   // a further press restarts the wait
    check(av_voice_tick(&p, AV_VOICE_ARMED_GIVE_UP_MS - 1) == 0, "restarted by the press");
    check(av_voice_tick(&p, 1) == AV_VOICE_DO_DISCARD, "a press that never resolved is thrown away");
    check(p.state == AV_VOICE_IDLE, "idle again");
}

static void test_arrow_while_armed(void)
{
    av_voice_policy_t p = fresh();
    ok(&p, &LIST, AV_VOICE_G_PRESS);
    unsigned e = av_voice_on_key(&p, &LIST, AV_VOICE_KEY_DOWN, AV_VOICE_G_CLICK);
    check(e == (AV_VOICE_DO_DISCARD | AV_VOICE_DO_PASS), "an arrow while armed: not talking, the list scrolls");
}

static void test_cancel(void)
{
    av_voice_policy_t p = listening_from(&PLAYING);
    unsigned e = av_voice_on_key(&p, &PLAYING, AV_VOICE_KEY_UP, AV_VOICE_G_CLICK);
    check(e == (AV_VOICE_DO_DISCARD | AV_VOICE_DO_CANCEL | AV_VOICE_DO_CLOSE), "▲ cancels and closes");
    check(p.state == AV_VOICE_IDLE && !p.begun, "idle");

    p = listening_from(&LIST);
    e = av_voice_on_key(&p, &LIST, AV_VOICE_KEY_UP, AV_VOICE_G_CLICK);
    check(e == (AV_VOICE_DO_DISCARD | AV_VOICE_DO_CANCEL | AV_VOICE_DO_LIST | AV_VOICE_DO_SHOW),
          "from the list, cancel returns to the list");

    p = waiting_from(&PLAYING);
    e = av_voice_on_key(&p, &PLAYING, AV_VOICE_KEY_UP, AV_VOICE_G_CLICK);
    check(e & AV_VOICE_DO_CANCEL, "▲ cancels while recognising");
    check(av_voice_on_result(&p, 1, AV_VOICE_RES_DONE, 0, true) == 0, "a result after cancel is dropped");
    check(p.state == AV_VOICE_IDLE, "and changes nothing");

    p = waiting_from(&PLAYING);
    e = ok(&p, &PLAYING, AV_VOICE_G_DOUBLE);
    check((e & AV_VOICE_DO_CANCEL) && (e & AV_VOICE_DO_CLOSE), "double-click OK while recognising: back");
}

static void test_cap(void)
{
    av_voice_policy_t p = listening_from(&PLAYING);
    check(av_voice_tick(&p, AV_VOICE_MAX_MS_DEFAULT - 1) == 0, "under ten seconds");
    check(av_voice_tick(&p, 1) == (AV_VOICE_DO_FINISH | AV_VOICE_DO_SHOW), "ten seconds ends it");
    check(p.state == AV_VOICE_SENDING, "sending the tail");
    check(ok(&p, &PLAYING, AV_VOICE_G_RELEASE) == 0, "letting go afterwards changes nothing");
    check(av_voice_tick(&p, AV_VOICE_SENDING_GIVE_UP_MS) & AV_VOICE_DO_BEEP_END,
          "if the microphone never reports, carry on");
    check(p.state == AV_VOICE_WAITING, "waiting");
}

static void test_result_timeout(void)
{
    av_voice_policy_t p = waiting_from(&PLAYING);
    check(av_voice_tick(&p, AV_VOICE_RESULT_TIMEOUT_MS - 1) == 0, "within 8 s");
    unsigned e = av_voice_tick(&p, 1);
    check(e == (AV_VOICE_DO_SHOW | AV_VOICE_DO_BEEP_FAIL), "8 s: error with the low tone");
    check(p.state == AV_VOICE_SHOWING && p.shown == AV_VOICE_SHOWN_ERROR, "the error screen");
    check(av_voice_on_result(&p, 1, AV_VOICE_RES_DONE, 0, true) == 0, "a late result is dropped");
    check(av_voice_tick(&p, AV_VOICE_IDLE_CLOSE_MS) == AV_VOICE_DO_CLOSE, "left alone, it closes");
}

static void test_results(void)
{
    av_voice_policy_t p = waiting_from(&PLAYING);
    check(av_voice_on_result(&p, 2, AV_VOICE_RES_DONE, 0, true) == 0, "a result for another id is dropped");
    check(av_voice_on_result(&p, 1, AV_VOICE_RES_PARTIAL, 0, true) == AV_VOICE_DO_SHOW, "partial: words only");
    check(p.state == AV_VOICE_WAITING, "still waiting after a partial");
    check(av_voice_on_result(&p, 1, AV_VOICE_RES_DONE, 0, true) == AV_VOICE_DO_SHOW, "one clear match");
    check(p.state == AV_VOICE_SHOWING && p.shown == AV_VOICE_SHOWN_DONE, "shown as done");
    check(av_voice_tick(&p, AV_VOICE_DONE_MS - 1) == 0, "for 0.8 s");
    check(av_voice_tick(&p, 1) == AV_VOICE_DO_CLOSE, "then the picture and its banner");

    p = waiting_from(&LIST);
    check(av_voice_on_result(&p, 1, AV_VOICE_RES_DONE_CLOSE, 0, true) == AV_VOICE_DO_CLOSE,
          "a device setting closes the panel at once, even from the list");

    p = waiting_from(&PLAYING);
    check(av_voice_on_result(&p, 1, AV_VOICE_RES_NOT_UNDERSTOOD, 0, true) == (AV_VOICE_DO_SHOW | AV_VOICE_DO_BEEP_FAIL),
          "not understood: low tone");
    check(ok(&p, &PLAYING, AV_VOICE_G_CLICK) == AV_VOICE_DO_CLOSE, "OK click puts it away");

    p = waiting_from(&PLAYING);
    check(av_voice_on_result(&p, 1, AV_VOICE_RES_CHOICES, 0, true) & AV_VOICE_DO_BEEP_FAIL,
          "no candidates at all is not understood");

    p = waiting_from(&PLAYING);
    check(av_voice_on_result(&p, 1, AV_VOICE_RES_ERROR, 0, true) == (AV_VOICE_DO_SHOW | AV_VOICE_DO_BEEP_FAIL), "error");
    check(av_voice_on_key(&p, &PLAYING, AV_VOICE_KEY_DOWN, AV_VOICE_G_CLICK) == AV_VOICE_DO_CLOSE,
          "any arrow puts an error away");

    // A result can beat the capture side's "stopped".
    p = listening_from(&PLAYING);
    ok(&p, &PLAYING, AV_VOICE_G_RELEASE);
    check(av_voice_on_result(&p, 1, AV_VOICE_RES_DONE, 0, true) == (AV_VOICE_DO_UNMUTE | AV_VOICE_DO_SHOW),
          "an early result brings the sound back");
}

static void test_choosing(void)
{
    av_voice_policy_t p = waiting_from(&PLAYING);
    av_voice_on_result(&p, 1, AV_VOICE_RES_CHOICES, 3, true);
    check(p.state == AV_VOICE_CHOOSING && p.choices == 3 && p.selected == 0, "three candidates");
    check(av_voice_on_key(&p, &PLAYING, AV_VOICE_KEY_UP, AV_VOICE_G_CLICK) == AV_VOICE_DO_SHOW, "▲ wraps");
    check(p.selected == 3, "to the 'open the list' row");
    av_voice_on_key(&p, &PLAYING, AV_VOICE_KEY_DOWN, AV_VOICE_G_CLICK);
    check(p.selected == 0, "▼ wraps back");
    av_voice_on_key(&p, &PLAYING, AV_VOICE_KEY_DOWN, AV_VOICE_G_CLICK);
    check(av_voice_on_key(&p, &PLAYING, AV_VOICE_KEY_DOWN, AV_VOICE_G_LONG) == 0, "long arrows do nothing here");
    // OK goes down (the microphone starts in case it becomes a hold), then a click picks.
    check(ok(&p, &PLAYING, AV_VOICE_G_PRESS) == AV_VOICE_DO_CAPTURE, "OK down in the panel arms");
    check(av_voice_panel_up(&p), "the candidates stay on screen while armed");
    check(ok(&p, &PLAYING, AV_VOICE_G_CLICK) == (AV_VOICE_DO_DISCARD | AV_VOICE_DO_PICK), "click picks row 1");
    check(p.state == AV_VOICE_CHOOSING && p.selected == 1, "still choosing until carried out");
    check(av_voice_picked_remote(&p) == AV_VOICE_DO_SHOW && p.state == AV_VOICE_WAITING,
          "a relay pick waits for a new result");
    check(av_voice_on_result(&p, 1, AV_VOICE_RES_DONE, 0, true) == AV_VOICE_DO_SHOW, "the same id answers it");

    p = waiting_from(&PLAYING);
    av_voice_on_result(&p, 1, AV_VOICE_RES_CHOICES, 2, true);
    check(av_voice_picked_done(&p, false) == AV_VOICE_DO_SHOW && p.shown == AV_VOICE_SHOWN_DONE,
          "a device pick shows done");

    p = waiting_from(&PLAYING);
    av_voice_on_result(&p, 1, AV_VOICE_RES_CHOICES, 2, true);
    p.selected = 2;
    check(ok(&p, &PLAYING, AV_VOICE_G_CLICK) == (AV_VOICE_DO_LIST | AV_VOICE_DO_SHOW),
          "'none of these' opens the ordinary list");
    check(p.state == AV_VOICE_IDLE && p.from_list, "and the list is where back goes from now");

    // Talk again from the candidates.
    p = waiting_from(&PLAYING);
    av_voice_on_result(&p, 1, AV_VOICE_RES_CHOICES, 2, true);
    ok(&p, &PLAYING, AV_VOICE_G_PRESS);
    check(ok(&p, &PLAYING, AV_VOICE_G_HOLD) == (AV_VOICE_DO_BEGIN | AV_VOICE_DO_SHOW), "hold talks again");
    check(p.id == 2 && p.choices == 0, "a new utterance replaces the rows");

    // Double-click backs out, idle time closes.
    p = waiting_from(&LIST);
    av_voice_on_result(&p, 1, AV_VOICE_RES_CHOICES, 2, true);
    ok(&p, &LIST, AV_VOICE_G_PRESS);
    ok(&p, &LIST, AV_VOICE_G_PRESS);
    check(ok(&p, &LIST, AV_VOICE_G_DOUBLE) == (AV_VOICE_DO_DISCARD | AV_VOICE_DO_LIST | AV_VOICE_DO_SHOW),
          "double-click from candidates opened in the list: back to the list");
    p = waiting_from(&PLAYING);
    av_voice_on_result(&p, 1, AV_VOICE_RES_CHOICES, 2, true);
    check(av_voice_tick(&p, AV_VOICE_IDLE_CLOSE_MS) == AV_VOICE_DO_CLOSE, "left alone, the candidates close");
}

static void test_choices_without_list_row(void)
{
    // The server's setup confirmation: two choices, no "open the list" row.
    av_voice_policy_t p = waiting_from(&PLAYING);
    av_voice_on_result(&p, 1, AV_VOICE_RES_CHOICES, 2, false);
    av_voice_on_key(&p, &PLAYING, AV_VOICE_KEY_DOWN, AV_VOICE_G_CLICK);
    check(p.selected == 1, "▼ to the second choice");
    av_voice_on_key(&p, &PLAYING, AV_VOICE_KEY_DOWN, AV_VOICE_G_CLICK);
    check(p.selected == 0, "two rows wrap without a list row");
    av_voice_on_key(&p, &PLAYING, AV_VOICE_KEY_UP, AV_VOICE_G_CLICK);
    check(ok(&p, &PLAYING, AV_VOICE_G_CLICK) == AV_VOICE_DO_PICK, "the last row is a choice, not the list");
}

static void test_setup_confirm(void)
{
    av_voice_policy_t p = waiting_from(&PLAYING);
    check(av_voice_on_result(&p, 1, AV_VOICE_RES_SETUP, 0, true) == AV_VOICE_DO_SHOW, "setup asks first");
    check(p.shown == AV_VOICE_SHOWN_SETUP && p.selected == 0, "on 'enter setup'");
    av_voice_on_key(&p, &PLAYING, AV_VOICE_KEY_DOWN, AV_VOICE_G_CLICK);
    check(p.selected == 1, "▼ to 'never mind'");
    ok(&p, &PLAYING, AV_VOICE_G_PRESS);
    check(ok(&p, &PLAYING, AV_VOICE_G_CLICK) == (AV_VOICE_DO_DISCARD | AV_VOICE_DO_CLOSE), "never mind closes");

    p = waiting_from(&PLAYING);
    av_voice_on_result(&p, 1, AV_VOICE_RES_SETUP, 0, true);
    ok(&p, &PLAYING, AV_VOICE_G_PRESS);
    check(ok(&p, &PLAYING, AV_VOICE_G_CLICK) == (AV_VOICE_DO_DISCARD | AV_VOICE_DO_SETUP | AV_VOICE_DO_CLOSE),
          "OK on 'enter setup' enters it");
}

static void test_unavailable(void)
{
    av_voice_policy_t p = fresh();
    check(ok(&p, &OFFLINE, AV_VOICE_G_PRESS) == 0, "no session: the microphone stays off");
    check(ok(&p, &OFFLINE, AV_VOICE_G_HOLD) == AV_VOICE_DO_SHOW, "the hold says voice is unavailable");
    check(p.shown == AV_VOICE_SHOWN_UNAVAILABLE && p.id == 0, "no utterance numbered");
    check(ok(&p, &OFFLINE, AV_VOICE_G_RELEASE) == 0, "letting go keeps the message");
    check(av_voice_on_key(&p, &OFFLINE, AV_VOICE_KEY_UP, AV_VOICE_G_CLICK) == AV_VOICE_DO_CLOSE, "any key closes it");

    p = fresh();
    ok(&p, &OFFLINE, AV_VOICE_G_PRESS);
    check(ok(&p, &OFFLINE, AV_VOICE_G_CLICK) == AV_VOICE_DO_PASS, "a click without a session is just a click");
}

static void test_lost_session(void)
{
    av_voice_policy_t p = fresh();
    ok(&p, &PLAYING, AV_VOICE_G_PRESS);
    check(av_voice_lost(&p) == AV_VOICE_DO_DISCARD, "armed: the microphone stops");
    check(ok(&p, &PLAYING, AV_VOICE_G_HOLD) == AV_VOICE_DO_SHOW && p.shown == AV_VOICE_SHOWN_UNAVAILABLE,
          "the hold then says unavailable");
    p = listening_from(&PLAYING);
    check(av_voice_lost(&p) == (AV_VOICE_DO_DISCARD | AV_VOICE_DO_SHOW), "listening: error");
    check(p.shown == AV_VOICE_SHOWN_ERROR, "error screen");
}

static void test_pages_keep_their_gestures(void)
{
    av_voice_policy_t p = fresh();
    check(ok(&p, &PAGE, AV_VOICE_G_PRESS) == 0, "pages do not record");
    check(ok(&p, &PAGE, AV_VOICE_G_CLICK) == AV_VOICE_DO_PASS, "a click is a click");
    check(ok(&p, &PAGE, AV_VOICE_G_HOLD) == 0, "the 0.45 s hold means nothing yet");
    check(ok(&p, &PAGE, AV_VOICE_G_RELEASE) == AV_VOICE_DO_PASS_CLICK,
          "let go before 1.5 s: what the page always called a click");
    ok(&p, &PAGE, AV_VOICE_G_HOLD);
    check(ok(&p, &PAGE, AV_VOICE_G_LONG) == AV_VOICE_DO_PASS, "1.5 s: the page's long press");
    check(ok(&p, &PAGE, AV_VOICE_G_RELEASE) == 0, "and letting go after it is nothing");
    check(av_voice_on_key(&p, &PAGE, AV_VOICE_KEY_UP, AV_VOICE_G_LONG) == AV_VOICE_DO_PASS, "arrows unchanged");
    check(p.state == AV_VOICE_IDLE, "nothing started");
}

static void test_idle_keys(void)
{
    av_voice_policy_t p = fresh();
    check(av_voice_on_key(&p, &PLAYING, AV_VOICE_KEY_UP, AV_VOICE_G_CLICK) == AV_VOICE_DO_PASS, "arrows pass");
    check(av_voice_on_key(&p, &PLAYING, AV_VOICE_KEY_DOWN, AV_VOICE_G_DOUBLE) == AV_VOICE_DO_PASS, "arrow double passes");
    check(ok(&p, &PLAYING, AV_VOICE_G_RELEASE) == 0, "a stray release is nothing");
    check(ok(&p, &PLAYING, AV_VOICE_G_LONG) == 0, "the old long press no longer opens the backlight");
    check(ok(&p, &LIST, AV_VOICE_G_DOUBLE) == AV_VOICE_DO_BACK, "list double (press lost): back");
}

static void test_reset(void)
{
    av_voice_policy_t p = listening_from(&PLAYING);
    unsigned e = av_voice_reset(&p);
    check(e == (AV_VOICE_DO_DISCARD | AV_VOICE_DO_CANCEL), "reset mid-utterance cancels it");
    check(p.state == AV_VOICE_IDLE && p.id == 1, "ids keep counting");
    p = fresh();
    check(av_voice_reset(&p) == 0, "reset at rest is nothing");
}

int main(void)
{
    test_voice_off_passes_everything();
    test_press_starts_capture_click_discards();
    test_click_in_list_confirms_row();
    test_double_in_list_goes_back();
    test_hold_begins();
    test_hold_threshold_against_click();
    test_hold_without_press();
    test_armed_gives_up();
    test_arrow_while_armed();
    test_cancel();
    test_cap();
    test_result_timeout();
    test_results();
    test_choosing();
    test_choices_without_list_row();
    test_setup_confirm();
    test_unavailable();
    test_lost_session();
    test_pages_keep_their_gestures();
    test_idle_keys();
    test_reset();
    if (failures) {
        printf("voice policy: %d failure(s)\n", failures);
        return 1;
    }
    puts("voice policy tests: PASS");
    return 0;
}

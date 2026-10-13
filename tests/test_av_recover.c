// Host tests for account recovery on the device (main/av_recover.c): the
// status page's gestures against a model of the button driver's click
// counting, the chord, the screen's phases and timeouts, and the address check.
#include "av_recover.h"

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

// ---- A model of the button driver (espressif/button 4.2, 5 ms ticks) ----
//
// OK is pressed at press[i] for held[i] ms. The driver reports PRESS at every
// press, and a burst's count once OK has stayed up for SHORT_MS after a
// release; a second or later press held SHORT_MS or longer ends the burst
// with no report. The events are fed to the gesture as they would arrive,
// with av_rg_tick() every 10 ms in between, and what the page would do is
// counted.

#define SHORT_MS 180u

typedef struct {
    unsigned recover, click, dbl, passed_clicks;
} outcome_t;

static void apply(outcome_t *o, av_rg_out_t out)
{
    if (out == AV_RG_RECOVER) o->recover++;
    if (out == AV_RG_CLICK) o->click++;
    if (out == AV_RG_DOUBLE) o->dbl++;
}

typedef struct { uint32_t at; int kind; unsigned count; } ev_t;   // kind 0 press, 1 clicks
static ev_t evs[64];
static unsigned nevs;

static void driver(const uint32_t *press, const uint32_t *held, unsigned n)
{
    nevs = 0;
    unsigned burst = 0;
    bool dead = false;
    for (unsigned i = 0; i < n; i++) {
        evs[nevs++] = (ev_t){ press[i], 0, 0 };
        if (burst && held[i] >= SHORT_MS) dead = true;   // the slow repeat press
        burst++;
        const uint32_t up = press[i] + held[i];
        const bool last = i + 1 == n || press[i + 1] - up > SHORT_MS;
        if (dead) {
            if (last) { burst = 0; dead = false; }
            continue;
        }
        if (last) {
            evs[nevs++] = (ev_t){ up + SHORT_MS, 1, burst };
            burst = 0;
        }
    }
}

static outcome_t run(const uint32_t *press, const uint32_t *held, unsigned n, unsigned needed)
{
    av_rg_t g;
    av_rg_init(&g, needed, true);
    outcome_t o = { 0, 0, 0, 0 };
    driver(press, held, n);
    unsigned k = 0;
    for (uint32_t t = 0; t < 20000u; t += 5u) {
        while (k < nevs && evs[k].at <= t) {
            const av_rg_out_t out = av_rg_key(&g, true, AV_RG_KEY_OK,
                                              evs[k].kind ? AV_RG_EV_CLICKS : AV_RG_EV_PRESS,
                                              evs[k].count, false, t);
            if (out == AV_RG_PASS && evs[k].kind) o.passed_clicks++;
            apply(&o, out);
            k++;
        }
        if (t % 10u == 0) apply(&o, av_rg_tick(&g, t));
    }
    return o;
}

static void test_five_quick_clicks(void)
{
    // Five clicks, 80 ms down and 90 ms up: one burst of five.
    const uint32_t press[] = { 1000, 1170, 1340, 1510, 1680 }, held[] = { 80, 80, 80, 80, 80 };
    outcome_t o = run(press, held, 5, 5);
    check(o.recover == 1 && o.click == 0 && o.dbl == 0, "five quick clicks: recovery, and no audio-only toggle");
    // Six or more count too.
    const uint32_t p6[] = { 1000, 1170, 1340, 1510, 1680, 1850 }, h6[] = { 80, 80, 80, 80, 80, 80 };
    o = run(p6, h6, 6, 5);
    check(o.recover == 1 && o.click == 0, "six clicks: recovery once");
}

static void test_split_bursts_add_up(void)
{
    // 2 + 3: a 250 ms pause after the second click splits the driver's burst.
    const uint32_t press[] = { 1000, 1170, 1500, 1670, 1840 }, held[] = { 80, 80, 80, 80, 80 };
    outcome_t o = run(press, held, 5, 5);
    check(o.recover == 1 && o.click == 0 && o.dbl == 0, "2 + 3 within the wait: recovery, nothing else");
    // 1 + 4 with a 500 ms pause (report at 1260, next press at 1580: 320 ms).
    const uint32_t p2[] = { 1000, 1580, 1750, 1920, 2090 }, h2[] = { 80, 80, 80, 80, 80 };
    o = run(p2, h2, 5, 5);
    check(o.recover == 1 && o.click == 0, "1 + 4 after half a second: still one gesture");
}

static void test_single_and_double_click_are_handed_back(void)
{
    const uint32_t press[] = { 1000 }, held[] = { 90 };
    outcome_t o = run(press, held, 1, 5);
    check(o.click == 1 && o.recover == 0 && o.dbl == 0, "one click is the page's OK click");
    // When: report at 1270, handed back at 1270 + AV_RG_WAIT_MS.
    av_rg_t g;
    av_rg_init(&g, 5, false);
    check(av_rg_key(&g, true, AV_RG_KEY_OK, AV_RG_EV_CLICKS, 1, false, 1270) == AV_RG_TAKEN, "held back");
    check(av_rg_tick(&g, 1270 + AV_RG_WAIT_MS - 10) == AV_RG_NONE, "not before the wait");
    check(av_rg_tick(&g, 1270 + AV_RG_WAIT_MS) == AV_RG_CLICK, "at the end of the wait");
    check(!av_rg_busy(&g), "nothing left");

    const uint32_t p2[] = { 1000, 1170 }, h2[] = { 80, 80 };
    o = run(p2, h2, 2, 5);
    check(o.dbl == 1 && o.click == 0 && o.recover == 0, "a double-click is the page's OK double-click");
    // Two separate clicks, two seconds apart: two clicks.
    const uint32_t p3[] = { 1000, 3000 }, h3[] = { 80, 80 };
    o = run(p3, h3, 2, 5);
    check(o.click == 2 && o.recover == 0, "clicks far apart act one by one");
}

static void test_abandoned_attempts_do_nothing(void)
{
    // 2 + 2: four in all, then OK stays up.
    const uint32_t press[] = { 1000, 1170, 1500, 1670 }, held[] = { 80, 80, 80, 80 };
    outcome_t o = run(press, held, 4, 5);
    check(o.click == 0 && o.dbl == 0 && o.recover == 0, "four clicks: nothing");
    // A triple, then a slow fourth press that never reports.
    const uint32_t p2[] = { 1000, 1170, 1340, 1800, 1970 }, h2[] = { 80, 80, 80, 80, 400 };
    o = run(p2, h2, 5, 5);
    check(o.click == 0 && o.dbl == 0 && o.recover == 0, "a burst that never reports drops what was held");
    // A double, then a slow press: nothing (not the Passport's back-to-Muse).
    const uint32_t p3[] = { 1000, 1170, 1600, 1800 }, h3[] = { 80, 80, 80, 300 };
    o = run(p3, h3, 4, 5);
    check(o.dbl == 0 && o.click == 0, "a double followed by a fumbled burst is not a double-click");
}

static void test_other_keys(void)
{
    av_rg_t g;
    av_rg_init(&g, 5, true);
    check(av_rg_key(&g, true, AV_RG_KEY_OK, AV_RG_EV_CLICKS, 2, false, 100) == AV_RG_TAKEN, "two held");
    check(av_rg_key(&g, true, AV_RG_KEY_DOWN, AV_RG_EV_CLICKS, 1, false, 200) == AV_RG_PASS,
          "an arrow closes the page as before");
    check(!av_rg_busy(&g) && av_rg_tick(&g, 5000) == AV_RG_NONE, "and the clicks are forgotten");
    av_rg_key(&g, true, AV_RG_KEY_OK, AV_RG_EV_CLICKS, 3, false, 100);
    check(av_rg_key(&g, true, AV_RG_KEY_OK, AV_RG_EV_LONG, 0, false, 300) == AV_RG_PASS, "OK held closes the page");
    check(!av_rg_busy(&g), "dropping the clicks");
    // ▲ and ▼ held keep their meaning (setup, pets).
    check(av_rg_key(&g, true, AV_RG_KEY_UP, AV_RG_EV_LONG, 0, false, 400) == AV_RG_PASS, "▲ held alone: setup");
    check(av_rg_key(&g, true, AV_RG_KEY_DOWN, AV_RG_EV_LONG, 0, true, 400) == AV_RG_PASS, "▼ held: pets");
    // The page went away under held-back clicks.
    av_rg_key(&g, true, AV_RG_KEY_OK, AV_RG_EV_CLICKS, 2, false, 500);
    check(av_rg_key(&g, false, AV_RG_KEY_OK, AV_RG_EV_CLICKS, 3, false, 900) == AV_RG_PASS,
          "off the page nothing is counted");
    check(!av_rg_busy(&g), "and nothing is held");
    // Off: clicks pass straight through.
    av_rg_init(&g, 0, false);
    check(av_rg_key(&g, true, AV_RG_KEY_OK, AV_RG_EV_CLICKS, 1, false, 0) == AV_RG_PASS, "gesture off: clicks pass");
    check(av_rg_key(&g, true, AV_RG_KEY_OK, AV_RG_EV_PRESS, 0, false, 0) == AV_RG_PASS, "and presses");
}

static void test_chord(void)
{
    av_rg_t g;
    av_rg_init(&g, 5, true);
    // ▲ first: its long press arrives with OK down.
    check(av_rg_key(&g, true, AV_RG_KEY_UP, AV_RG_EV_LONG, 0, true, 1500) == AV_RG_RECOVER, "▲ + OK: recovery");
    check(av_rg_key(&g, false, AV_RG_KEY_OK, AV_RG_EV_OTHER, 0, false, 1600) == AV_RG_TAKEN, "OK's release is taken");
    check(av_rg_key(&g, false, AV_RG_KEY_OK, AV_RG_EV_LONG, 0, false, 1700) == AV_RG_TAKEN,
          "OK's own long press does not close the code");
    check(av_rg_key(&g, false, AV_RG_KEY_OK, AV_RG_EV_CLICKS, 1, false, 1800) == AV_RG_PASS,
          "after that OK is OK again (any key closes the code)");
    // OK first, ▲ let go early: ▲'s click is swallowed.
    av_rg_init(&g, 5, true);
    av_rg_key(&g, true, AV_RG_KEY_OK, AV_RG_EV_PRESS, 0, false, 0);
    check(av_rg_key(&g, true, AV_RG_KEY_OK, AV_RG_EV_LONG, 0, true, 1500) == AV_RG_RECOVER, "OK + ▲: recovery");
    check(av_rg_key(&g, false, AV_RG_KEY_UP, AV_RG_EV_CLICKS, 1, false, 1700) == AV_RG_TAKEN, "▲'s click swallowed");
    check(av_rg_key(&g, false, AV_RG_KEY_UP, AV_RG_EV_CLICKS, 1, false, 1900) == AV_RG_PASS, "only one");
    // The latch runs out.
    av_rg_init(&g, 5, true);
    av_rg_key(&g, true, AV_RG_KEY_UP, AV_RG_EV_LONG, 0, true, 0);
    check(av_rg_busy(&g), "latched");
    av_rg_tick(&g, AV_RG_LATCH_MS);
    check(!av_rg_busy(&g), "latch expires");
    check(av_rg_key(&g, false, AV_RG_KEY_OK, AV_RG_EV_CLICKS, 1, false, AV_RG_LATCH_MS + 1) == AV_RG_PASS,
          "a later OK closes the code");
    // Off the page the chord is nothing; without the chord (the Passport) a
    // long ▲ is setup even if the ladder reported something odd.
    av_rg_init(&g, 5, true);
    check(av_rg_key(&g, false, AV_RG_KEY_UP, AV_RG_EV_LONG, 0, true, 0) == AV_RG_PASS, "not off the page");
    av_rg_init(&g, 5, false);
    check(av_rg_key(&g, true, AV_RG_KEY_UP, AV_RG_EV_LONG, 0, true, 0) == AV_RG_PASS, "no chord on this board");
    // Clicks held back, then the chord: the chord wins, the clicks are gone.
    av_rg_init(&g, 5, true);
    av_rg_key(&g, true, AV_RG_KEY_OK, AV_RG_EV_CLICKS, 1, false, 0);
    check(av_rg_key(&g, true, AV_RG_KEY_OK, AV_RG_EV_LONG, 0, true, 300) == AV_RG_RECOVER, "chord after a click");
    check(av_rg_tick(&g, 1000) == AV_RG_NONE, "the click is not handed back");
}

// ---- The screen ----

static const char URL[] = "HTTPS://TV.EXAMPLE.COM/TV/P/ABCDEFGHIJKLMNOPQRSTUVWXYZ";

static void test_screen_flow(void)
{
    av_recover_screen_t r;
    const av_recover_ctx_t bound = { false, false, true, true };
    check(av_recover_open(&r, &bound, 1000) && r.phase == AV_RECOVER_ASKING, "asks the server");
    av_recover_sent(&r);
    check(av_recover_answer(&r, URL, 300, AV_RECOVER_ERR_NONE, 1500) && r.phase == AV_RECOVER_SHOWING, "shows");
    check(av_recover_seconds_left(&r, 1500) == 300 && av_recover_seconds_left(&r, 1501) == 300, "five minutes");
    check(av_recover_seconds_left(&r, 1500 + 1000) == 299, "the countdown");
    const char *a, *b;
    bool alarm;
    av_recover_lines(&r, &a, &b, &alarm);
    check(!strcmp(a, "用手机相机扫码") && !strcmp(b, "以主人身份登录") && !alarm, "the two lines");
    check(!av_recover_answer(&r, URL, 300, AV_RECOVER_ERR_NONE, 1600), "a second answer is ignored");
    check(av_recover_tick(&r, 1500 + 299999) == AV_RECOVER_TICK_NONE, "still up");
    check(av_recover_tick(&r, 1500 + 300000) == AV_RECOVER_TICK_CHANGED && r.error == AV_RECOVER_ERR_EXPIRED,
          "expires");
    check(av_recover_seconds_left(&r, 1500 + 300000) == 0, "no countdown after");
    check(av_recover_tick(&r, 1500 + 300000 + AV_RECOVER_FAILED_MS) == AV_RECOVER_TICK_CLOSE, "then closes");
    av_recover_close(&r);
    check(r.phase == AV_RECOVER_NONE, "closed");
    // The server's ttl is clamped.
    av_recover_open(&r, &bound, 0);
    av_recover_answer(&r, URL, 99999, AV_RECOVER_ERR_NONE, 0);
    check(av_recover_seconds_left(&r, 0) == AV_RECOVER_TTL_DEFAULT_S, "an absurd ttl is the default");
}

static void test_screen_refusals(void)
{
    av_recover_screen_t r;
    const av_recover_ctx_t unbound = { true, true, false, true };
    check(!av_recover_open(&r, &unbound, 0) && r.phase == AV_RECOVER_BIND, "unbound: the bind code");
    const char *a, *b;
    av_recover_lines(&r, &a, &b, NULL);
    check(!strcmp(b, "创建账号并绑定这台设备"), "and what it is for");
    check(av_recover_tick(&r, AV_RECOVER_BIND_MS) == AV_RECOVER_TICK_CLOSE, "bind code closes after a while");
    const av_recover_ctx_t unbound_no_code = { true, false, false, true };
    av_recover_open(&r, &unbound_no_code, 0);
    check(r.phase == AV_RECOVER_FAILED && r.error == AV_RECOVER_ERR_UNBOUND, "unbound, no code known");
    const av_recover_ctx_t old_server = { false, false, false, true };
    av_recover_open(&r, &old_server, 0);
    check(r.phase == AV_RECOVER_FAILED && r.error == AV_RECOVER_ERR_OFF, "a server that does not offer it");
    const av_recover_ctx_t offline = { false, false, false, false };
    av_recover_open(&r, &offline, 0);
    check(r.phase == AV_RECOVER_FAILED && r.error == AV_RECOVER_ERR_OFFLINE, "never connected");
    // Asked, never sent (no session came): offline; sent, no answer: failed.
    const av_recover_ctx_t bound = { false, false, true, false };
    av_recover_open(&r, &bound, 0);
    check(av_recover_tick(&r, AV_RECOVER_ASK_MS) == AV_RECOVER_TICK_CHANGED && r.error == AV_RECOVER_ERR_OFFLINE,
          "no session to ask over");
    av_recover_open(&r, &bound, 0);
    av_recover_sent(&r);
    av_recover_tick(&r, AV_RECOVER_ASK_MS);
    check(r.error == AV_RECOVER_ERR_FAILED, "no answer");
    // Errors from the server, and a bad address.
    static const struct { const char *word; av_recover_error_t error; } words[] = {
        { "limited", AV_RECOVER_ERR_LIMITED }, { "unbound", AV_RECOVER_ERR_UNBOUND },
        { "off", AV_RECOVER_ERR_OFF }, { "fail", AV_RECOVER_ERR_FAILED }, { "what", AV_RECOVER_ERR_FAILED },
    };
    for (unsigned i = 0; i < sizeof(words) / sizeof(words[0]); i++) {
        av_recover_open(&r, &bound, 0);
        av_recover_answer(&r, NULL, 0, av_recover_error_from(words[i].word), 10);
        check(r.phase == AV_RECOVER_FAILED && r.error == words[i].error, words[i].word);
        bool alarm = false;
        av_recover_lines(&r, &a, &b, &alarm);
        check(alarm && a[0] && b[0], "a reason on screen");
    }
    av_recover_open(&r, &bound, 0);
    av_recover_answer(&r, "https://tv.example.com/tv/p/abc", 300, AV_RECOVER_ERR_NONE, 10);
    check(r.phase == AV_RECOVER_FAILED && r.error == AV_RECOVER_ERR_FAILED, "a lower-case address is refused");
}

static void test_url_check(void)
{
    check(av_recover_url_ok(URL), "the server's form");
    check(av_recover_url_ok("HTTP://192.168.1.2:8080/TV/P/ABC"), "a LAN address");
    check(!av_recover_url_ok(""), "empty");
    check(!av_recover_url_ok(NULL), "null");
    check(!av_recover_url_ok("HTTPS://TV.EXAMPLE.COM/TV/R/ABC"), "not the login page");
    check(!av_recover_url_ok("HTTPS://TV.EXAMPLE.COM/TV/P/"), "no token");
    check(!av_recover_url_ok("HTTPS://TV.EXAMPLE.COM/TV/P/AB?C"), "outside the alphanumeric set");
    check(!av_recover_url_ok("FTP://X/TV/P/A"), "not http");
    char longest[AV_RECOVER_URL_MAX + 2];
    memset(longest, 'A', sizeof(longest));
    memcpy(longest, "HTTPS://X/TV/P/", 15);
    longest[AV_RECOVER_URL_MAX] = '\0';
    check(av_recover_url_ok(longest), "exactly the limit");
    longest[AV_RECOVER_URL_MAX] = 'A';
    longest[AV_RECOVER_URL_MAX + 1] = '\0';
    check(!av_recover_url_ok(longest), "one over");
}

int main(void)
{
    test_five_quick_clicks();
    test_split_bursts_add_up();
    test_single_and_double_click_are_handed_back();
    test_abandoned_attempts_do_nothing();
    test_other_keys();
    test_chord();
    test_screen_flow();
    test_screen_refusals();
    test_url_check();
    if (failures) {
        printf("recover: %d failure(s)\n", failures);
        return 1;
    }
    puts("recover tests: PASS");
    return 0;
}

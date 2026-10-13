// main/av_recover.c -- see av_recover.h.
#include "av_recover.h"

#include <string.h>

// Wrapping time comparison: `a` is at or after `b`.
static bool reached(uint32_t a, uint32_t b)
{
    return (int32_t)(a - b) >= 0;
}

// ---- 1. The gesture ----

void av_rg_init(av_rg_t *g, unsigned clicks_needed, bool chord)
{
    if (!g) {
        return;
    }
    memset(g, 0, sizeof(*g));
    g->clicks_needed = clicks_needed;
    g->chord = chord;
}

void av_rg_drop(av_rg_t *g)
{
    if (g) {
        g->pending = false;
        g->burst = false;
        g->count = 0;
    }
}

bool av_rg_busy(const av_rg_t *g)
{
    return g && (g->pending || g->latch);
}

av_rg_out_t av_rg_key(av_rg_t *g, bool on_page, av_rg_key_t key, av_rg_ev_t ev, unsigned count,
                      bool other_down, uint32_t now_ms)
{
    if (!g) {
        return AV_RG_PASS;
    }
    // The chord's other key, still to report: its first ending event belongs
    // to the gesture, wherever the screen has gone since.
    if (g->latch) {
        if (reached(now_ms, g->latch_until_ms)) {
            g->latch = false;
        } else if (key == g->latch_key) {
            if (ev == AV_RG_EV_CLICKS || ev == AV_RG_EV_LONG) {
                g->latch = false;
            }
            return AV_RG_TAKEN;
        }
    }
    if (!on_page) {
        // The page went away under held-back clicks: they meant nothing.
        av_rg_drop(g);
        return AV_RG_PASS;
    }
    const bool ok = key == AV_RG_KEY_OK;
    if (g->chord && ev == AV_RG_EV_LONG && (ok || key == AV_RG_KEY_UP) && other_down) {
        av_rg_drop(g);
        g->latch = true;
        g->latch_key = ok ? AV_RG_KEY_UP : AV_RG_KEY_OK;
        g->latch_until_ms = now_ms + AV_RG_LATCH_MS;
        return AV_RG_RECOVER;
    }
    if (!g->clicks_needed) {
        return AV_RG_PASS;
    }
    if (!ok) {
        // An arrow does what it did (closes the page, or its long press) and
        // the clicks before it are forgotten.
        av_rg_drop(g);
        return AV_RG_PASS;
    }
    switch (ev) {
    case AV_RG_EV_PRESS:
        if (!g->pending) {
            return AV_RG_PASS;
        }
        // Another burst begins: wait for its count.
        g->burst = true;
        g->deadline_ms = now_ms + AV_RG_PRESS_MAX_MS;
        return AV_RG_TAKEN;
    case AV_RG_EV_CLICKS:
        g->count += count ? count : 1u;
        if (g->count >= g->clicks_needed) {
            av_rg_drop(g);
            return AV_RG_RECOVER;
        }
        g->pending = true;
        g->burst = false;
        g->deadline_ms = now_ms + AV_RG_WAIT_MS;
        return AV_RG_TAKEN;
    case AV_RG_EV_LONG:
        // OK held closes the page, as before; the clicks before it are dropped.
        av_rg_drop(g);
        return AV_RG_PASS;
    default:
        return AV_RG_PASS;
    }
}

av_rg_out_t av_rg_tick(av_rg_t *g, uint32_t now_ms)
{
    if (!g) {
        return AV_RG_NONE;
    }
    if (g->latch && reached(now_ms, g->latch_until_ms)) {
        g->latch = false;
    }
    if (!g->pending || !reached(now_ms, g->deadline_ms)) {
        return AV_RG_NONE;
    }
    // A burst that began and never reported (a slow press) was not a click:
    // the viewer meant more than the clicks held back, so none of them acts.
    const unsigned n = g->burst ? 0u : g->count;
    av_rg_drop(g);
    return n == 1u ? AV_RG_CLICK : n == 2u ? AV_RG_DOUBLE : AV_RG_NONE;
}

// ---- 2. The screen ----

static void enter(av_recover_screen_t *r, av_recover_phase_t phase, av_recover_error_t error, uint32_t now_ms)
{
    r->phase = phase;
    r->error = error;
    r->since_ms = now_ms;
}

bool av_recover_open(av_recover_screen_t *r, const av_recover_ctx_t *ctx, uint32_t now_ms)
{
    if (!r || !ctx) {
        return false;
    }
    memset(r, 0, sizeof(*r));
    if (ctx->unbound) {
        // Nobody owns it: the way in is to own it.
        if (ctx->bind_code) enter(r, AV_RECOVER_BIND, AV_RECOVER_ERR_NONE, now_ms);
        else enter(r, AV_RECOVER_FAILED, AV_RECOVER_ERR_UNBOUND, now_ms);
        return false;
    }
    if (!ctx->offered) {
        enter(r, AV_RECOVER_FAILED, ctx->online ? AV_RECOVER_ERR_OFF : AV_RECOVER_ERR_OFFLINE, now_ms);
        return false;
    }
    // Offered by the last server that configured a session: ask, now or on the
    // next session (a channel change in between is a second or two).
    enter(r, AV_RECOVER_ASKING, AV_RECOVER_ERR_NONE, now_ms);
    return true;
}

void av_recover_sent(av_recover_screen_t *r)
{
    if (r && r->phase == AV_RECOVER_ASKING) {
        r->sent = true;
    }
}

bool av_recover_answer(av_recover_screen_t *r, const char *url, unsigned ttl_s, av_recover_error_t error,
                       uint32_t now_ms)
{
    if (!r || r->phase != AV_RECOVER_ASKING) {
        return false;
    }
    if (error == AV_RECOVER_ERR_NONE && av_recover_url_ok(url)) {
        if (ttl_s == 0 || ttl_s > AV_RECOVER_TTL_MAX_S) ttl_s = AV_RECOVER_TTL_DEFAULT_S;
        enter(r, AV_RECOVER_SHOWING, AV_RECOVER_ERR_NONE, now_ms);
        r->until_ms = now_ms + ttl_s * 1000u;
        return true;
    }
    enter(r, AV_RECOVER_FAILED, error == AV_RECOVER_ERR_NONE ? AV_RECOVER_ERR_FAILED : error, now_ms);
    return true;
}

av_recover_tick_t av_recover_tick(av_recover_screen_t *r, uint32_t now_ms)
{
    if (!r) {
        return AV_RECOVER_TICK_NONE;
    }
    const uint32_t in_phase = now_ms - r->since_ms;
    switch (r->phase) {
    case AV_RECOVER_ASKING:
        if (in_phase >= AV_RECOVER_ASK_MS) {
            enter(r, AV_RECOVER_FAILED, r->sent ? AV_RECOVER_ERR_FAILED : AV_RECOVER_ERR_OFFLINE, now_ms);
            return AV_RECOVER_TICK_CHANGED;
        }
        return AV_RECOVER_TICK_NONE;
    case AV_RECOVER_SHOWING:
        if (reached(now_ms, r->until_ms)) {
            enter(r, AV_RECOVER_FAILED, AV_RECOVER_ERR_EXPIRED, now_ms);
            return AV_RECOVER_TICK_CHANGED;
        }
        return AV_RECOVER_TICK_NONE;
    case AV_RECOVER_BIND:
        return in_phase >= AV_RECOVER_BIND_MS ? AV_RECOVER_TICK_CLOSE : AV_RECOVER_TICK_NONE;
    case AV_RECOVER_FAILED:
        return in_phase >= AV_RECOVER_FAILED_MS ? AV_RECOVER_TICK_CLOSE : AV_RECOVER_TICK_NONE;
    default:
        return AV_RECOVER_TICK_NONE;
    }
}

void av_recover_close(av_recover_screen_t *r)
{
    if (r) {
        memset(r, 0, sizeof(*r));
    }
}

unsigned av_recover_seconds_left(const av_recover_screen_t *r, uint32_t now_ms)
{
    if (!r || r->phase != AV_RECOVER_SHOWING || reached(now_ms, r->until_ms)) {
        return 0;
    }
    return (r->until_ms - now_ms + 999u) / 1000u;
}

void av_recover_lines(const av_recover_screen_t *r, const char **line1, const char **line2, bool *alarm)
{
    const char *a = "", *b = "";
    bool bad = false;
    if (r) {
        switch (r->phase) {
        case AV_RECOVER_ASKING:
            a = "正在要登录码…";
            b = "稍等几秒";
            break;
        case AV_RECOVER_SHOWING:
            a = "用手机相机扫码";
            b = "以主人身份登录";
            break;
        case AV_RECOVER_BIND:
            a = "用手机相机扫码";
            b = "创建账号并绑定这台设备";
            break;
        case AV_RECOVER_FAILED:
            bad = true;
            switch (r->error) {
            case AV_RECOVER_ERR_OFFLINE: a = "没有连上服务器"; b = "连上网络后再试"; break;
            case AV_RECOVER_ERR_OFF:     a = "服务器不支持恢复登录"; b = "请管理员更新服务器"; break;
            case AV_RECOVER_ERR_LIMITED: a = "一小时内已经要了 3 次"; b = "请过一会儿再试"; break;
            case AV_RECOVER_ERR_UNBOUND: a = "这台设备还没有主人"; b = "连上网络后扫码绑定"; break;
            case AV_RECOVER_ERR_EXPIRED: a = "登录码已过期"; b = "需要的话再要一个"; break;
            default:                     a = "没有要到登录码"; b = "请稍后再试"; break;
            }
            break;
        default:
            break;
        }
    }
    if (line1) *line1 = a;
    if (line2) *line2 = b;
    if (alarm) *alarm = bad;
}

// ---- 3. The address ----

bool av_recover_url_ok(const char *url)
{
    if (!url) {
        return false;
    }
    const size_t n = strnlen(url, AV_RECOVER_URL_MAX + 1u);
    if (n == 0 || n > AV_RECOVER_URL_MAX) {
        return false;
    }
    if (strncmp(url, "HTTPS://", 8) != 0 && strncmp(url, "HTTP://", 7) != 0) {
        return false;
    }
    for (const char *c = url; *c; c++) {
        if (!((*c >= 'A' && *c <= 'Z') || (*c >= '0' && *c <= '9') || strchr(":/.%-", *c))) {
            return false;
        }
    }
    // The landing page, and a token after it.
    const char *page = strstr(url, "/TV/P/");
    return page && page[6] != '\0';
}

av_recover_error_t av_recover_error_from(const char *word)
{
    if (!word) return AV_RECOVER_ERR_FAILED;
    if (!strcmp(word, "limited")) return AV_RECOVER_ERR_LIMITED;
    if (!strcmp(word, "unbound")) return AV_RECOVER_ERR_UNBOUND;
    if (!strcmp(word, "off")) return AV_RECOVER_ERR_OFF;
    return AV_RECOVER_ERR_FAILED;
}

#include "av_wifi_ops.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

// strnlen is POSIX rather than C: glibc hides it from a strict -std=c11 host build
// (see av_channel_policy.c), so the fields are measured by hand.
static size_t bounded_length(const char *s, size_t max)
{
    size_t n = 0;
    while (n < max && s[n] != '\0') {
        n++;
    }
    return n;
}

static const char *const CODES[AV_WIFI_E_COUNT] = {
    "ok", "bad", "replay", "full", "changed", "key", "store", "busy",
};

const char *av_wifi_err_code(av_wifi_err_t err)
{
    return (unsigned)err < AV_WIFI_E_COUNT ? CODES[err] : "bad";
}

av_wifi_op_kind_t av_wifi_op_kind(const char *name)
{
    if (!name) return AV_WIFI_OP_NONE;
    if (!strcmp(name, "add")) return AV_WIFI_OP_ADD;
    if (!strcmp(name, "remove")) return AV_WIFI_OP_REMOVE;
    if (!strcmp(name, "up")) return AV_WIFI_OP_UP;
    if (!strcmp(name, "switch")) return AV_WIFI_OP_SWITCH;
    return AV_WIFI_OP_NONE;
}

// Length of the UTF-8 sequence starting at p, or 0 if it is not a valid one
// (overlong forms, surrogates and code points past U+10FFFF included).
static size_t utf8_length(const unsigned char *p)
{
    const unsigned char c = p[0];
    if (c < 0x80) return 1;
    size_t n;
    unsigned min;
    if ((c & 0xE0) == 0xC0) { n = 2; min = 0x80; }
    else if ((c & 0xF0) == 0xE0) { n = 3; min = 0x800; }
    else if ((c & 0xF8) == 0xF0) { n = 4; min = 0x10000; }
    else return 0;
    unsigned cp = c & (0x7F >> n);
    for (size_t i = 1; i < n; i++) {
        if ((p[i] & 0xC0) != 0x80) return 0;
        cp = (cp << 6) | (p[i] & 0x3F);
    }
    if (cp < min || cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) return 0;
    return n;
}

void av_wifi_display_name(const char *raw, char *out, size_t size)
{
    if (!out || size == 0) return;
    size_t o = 0;
    const unsigned char *p = (const unsigned char *)(raw ? raw : "");
    while (*p) {
        size_t n = utf8_length(p);
        if (n == 0 || (n == 1 && (*p < 0x20 || *p == 0x7F))) {
            if (o + 1 >= size) break;
            out[o++] = '?';
            p++;
            continue;
        }
        if (o + n >= size) break;
        memcpy(out + o, p, n);
        o += n;
        p += n;
    }
    out[o] = '\0';
}

av_wifi_err_t av_wifi_plan(const av_wifi_op_t *op, uint64_t last_seq,
                           const char *const *names, unsigned count, av_wifi_plan_t *plan)
{
    if (!op || !plan || (count && !names) || count > AV_WIFI_SAVED_MAX) return AV_WIFI_E_BAD;
    plan->action = AV_WIFI_OP_NONE;
    plan->index = -1;
    if (op->seq == 0) return AV_WIFI_E_BAD;
    if (op->seq <= last_seq) return AV_WIFI_E_REPLAY;
    if (op->kind == AV_WIFI_OP_ADD) {
        const size_t ssid = bounded_length(op->ssid, sizeof(op->ssid));
        const size_t password = bounded_length(op->password, sizeof(op->password));
        // WPA wants 8..63 characters, or 64 hex digits; empty is an open network.
        if (ssid == 0 || ssid > AV_WIFI_SSID_MAX || password > AV_WIFI_PASSWORD_MAX ||
            (password > 0 && password < 8)) {
            return AV_WIFI_E_BAD;
        }
        bool known = false;
        for (unsigned i = 0; i < count; i++) {
            if (names[i] && !strcmp(names[i], op->ssid)) { known = true; break; }
        }
        // A network already saved gets its new password and keeps its place.
        if (!known && count >= AV_WIFI_SAVED_MAX) return AV_WIFI_E_FULL;
        plan->action = AV_WIFI_OP_ADD;
        return AV_WIFI_OK;
    }
    if (op->kind != AV_WIFI_OP_REMOVE && op->kind != AV_WIFI_OP_UP && op->kind != AV_WIFI_OP_SWITCH) {
        return AV_WIFI_E_BAD;
    }
    if (op->index < 0 || (unsigned)op->index >= count || !names[op->index]) return AV_WIFI_E_CHANGED;
    char shown[AV_WIFI_SSID_MAX * 3 + 1];
    av_wifi_display_name(names[op->index], shown, sizeof(shown));
    if (strcmp(shown, op->ssid)) return AV_WIFI_E_CHANGED;
    plan->index = op->index;
    // Already first: nothing to move, and still a success.
    plan->action = (op->kind == AV_WIFI_OP_UP && op->index == 0) ? AV_WIFI_OP_NONE : op->kind;
    return AV_WIFI_OK;
}

size_t av_wifi_json_string(const char *text, char *out, size_t size)
{
    if (!out || size < 3) return 0;
    size_t o = 0;
    out[o++] = '"';
    for (const unsigned char *p = (const unsigned char *)(text ? text : ""); *p; p++) {
        char esc[7];
        size_t n;
        if (*p == '"' || *p == '\\') { esc[0] = '\\'; esc[1] = (char)*p; n = 2; }
        else if (*p < 0x20) { n = (size_t)snprintf(esc, sizeof(esc), "\\u%04x", *p); }
        else { esc[0] = (char)*p; n = 1; }
        if (o + n + 2 > size) return 0;
        memcpy(out + o, esc, n);
        o += n;
    }
    out[o++] = '"';
    out[o] = '\0';
    return o;
}

size_t av_wifi_ack_json(char *out, size_t size, uint64_t seq, av_wifi_err_t err,
                        const char *const *names, unsigned count, const char *current)
{
    if (!out || size == 0) return 0;
    int n = snprintf(out, size, "{\"wifi\":{\"ack\":%" PRIu64 ",\"ok\":%d,\"err\":\"%s\",\"saved\":[",
                     seq, err == AV_WIFI_OK ? 1 : 0, av_wifi_err_code(err));
    if (n < 0 || (size_t)n >= size) return 0;
    size_t o = (size_t)n;
    char shown[AV_WIFI_SSID_MAX * 3 + 1];
    for (unsigned i = 0; i < count && i < AV_WIFI_SAVED_MAX; i++) {
        if (i) {
            if (o + 1 >= size) return 0;
            out[o++] = ',';
        }
        av_wifi_display_name(names[i], shown, sizeof(shown));
        const size_t m = av_wifi_json_string(shown, out + o, size - o);
        if (!m) return 0;
        o += m;
    }
    av_wifi_display_name(current, shown, sizeof(shown));
    n = snprintf(out + o, size - o, "],\"cur\":");
    if (n < 0 || (size_t)n >= size - o) return 0;
    o += (size_t)n;
    const size_t m = av_wifi_json_string(shown, out + o, size - o);
    if (!m || o + m + 2 >= size) return 0;
    o += m;
    out[o++] = '}';
    out[o++] = '}';
    out[o] = '\0';
    return o;
}

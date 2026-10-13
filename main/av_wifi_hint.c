// The networks the device can see, in the sound page's QR code; see
// av_wifi_hint.h.
#include "av_wifi_hint.h"

#include <string.h>

#include "qrcodegen.h"

void av_wifi_hint_clear(av_wifi_hint_t *hint)
{
    if (hint) memset(hint, 0, sizeof(*hint));
}

unsigned av_wifi_hint_bars(int rssi)
{
    if (rssi >= -55) return 4u;
    if (rssi >= -66) return 3u;
    if (rssi >= -77) return 2u;
    return 1u;
}

// Valid UTF-8 (no overlong forms, no surrogates, at most U+10FFFF) without
// C0 controls or DEL: what a text field can hold and send back unchanged.
static bool showable(const uint8_t *s, size_t n)
{
    size_t i = 0;
    while (i < n) {
        const uint8_t c = s[i];
        if (c < 0x20u || c == 0x7Fu) return false;
        if (c < 0x80u) {
            i++;
            continue;
        }
        size_t more;
        uint32_t cp;
        if (c >= 0xC2u && c <= 0xDFu) {
            more = 1; cp = c & 0x1Fu;
        } else if (c >= 0xE0u && c <= 0xEFu) {
            more = 2; cp = c & 0x0Fu;
        } else if (c >= 0xF0u && c <= 0xF4u) {
            more = 3; cp = c & 0x07u;
        } else {
            return false;
        }
        if (n - i <= more) return false;
        for (size_t k = 1; k <= more; k++) {
            const uint8_t d = s[i + k];
            if ((d & 0xC0u) != 0x80u) return false;
            cp = (cp << 6) | (d & 0x3Fu);
        }
        if ((more == 2 && cp < 0x800u) || (more == 3 && (cp < 0x10000u || cp > 0x10FFFFu)) ||
            (cp >= 0xD800u && cp <= 0xDFFFu)) {
            return false;
        }
        i += more + 1u;
    }
    return true;
}

void av_wifi_hint_offer(av_wifi_hint_t *hint, const uint8_t *ssid, size_t len, int rssi, bool open,
                        bool joinable)
{
    if (!hint || !ssid || !joinable || len == 0 || len > 32 || !showable(ssid, len)) return;
    if (rssi < -128) rssi = -128;
    if (rssi > 0) rssi = 0;
    for (unsigned i = 0; i < hint->count; i++) {
        av_wifi_hint_net_t *net = &hint->net[i];
        if (net->len == len && !memcmp(net->ssid, ssid, len)) {
            if (rssi > net->rssi) {
                net->rssi = (int8_t)rssi;
                net->open = open;
            }
            return;
        }
    }
    unsigned slot = hint->count;
    if (slot >= AV_WIFI_HINT_MAX) {
        // Full: replace the weakest if this one is stronger.
        slot = 0;
        for (unsigned i = 1; i < hint->count; i++) {
            if (hint->net[i].rssi < hint->net[slot].rssi) slot = i;
        }
        if (rssi <= hint->net[slot].rssi) return;
    } else {
        hint->count++;
    }
    av_wifi_hint_net_t *net = &hint->net[slot];
    memset(net, 0, sizeof(*net));
    memcpy(net->ssid, ssid, len);
    net->len = (uint8_t)len;
    net->rssi = (int8_t)rssi;
    net->open = open;
}

static int compare(const av_wifi_hint_net_t *a, const av_wifi_hint_net_t *b)
{
    const unsigned ba = av_wifi_hint_bars(a->rssi), bb = av_wifi_hint_bars(b->rssi);
    if (ba != bb) return ba > bb ? -1 : 1;
    const size_t n = a->len < b->len ? a->len : b->len;
    const int c = memcmp(a->ssid, b->ssid, n);
    if (c) return c;
    return (int)a->len - (int)b->len;
}

void av_wifi_hint_finish(av_wifi_hint_t *hint)
{
    if (!hint) return;
    // Six at most: insertion sort.
    for (unsigned i = 1; i < hint->count; i++) {
        const av_wifi_hint_net_t key = hint->net[i];
        unsigned j = i;
        while (j > 0 && compare(&hint->net[j - 1], &key) > 0) {
            hint->net[j] = hint->net[j - 1];
            j--;
        }
        hint->net[j] = key;
    }
}

bool av_wifi_hint_same(const av_wifi_hint_t *a, const av_wifi_hint_t *b)
{
    if (!a || !b || a->count != b->count) return false;
    for (unsigned i = 0; i < a->count; i++) {
        const av_wifi_hint_net_t *x = &a->net[i], *y = &b->net[i];
        if (x->len != y->len || memcmp(x->ssid, y->ssid, x->len) || x->open != y->open ||
            av_wifi_hint_bars(x->rssi) != av_wifi_hint_bars(y->rssi)) {
            return false;
        }
    }
    return true;
}

size_t av_wifi_hint_fragment(const av_wifi_hint_t *hint, unsigned count, char *out, size_t size)
{
    static const char alphabet[] = "0123456789ABCDEFGHIJKLMNOPQRSTUV";
    if (!hint || !out || count == 0 || count > hint->count) return 0;
    size_t bytes = 0;
    for (unsigned i = 0; i < count; i++) bytes += 1u + hint->net[i].len;
    const size_t chars = 1u + (bytes * 8u + 4u) / 5u;
    if (chars + 1u > size) return 0;
    size_t at = 0;
    out[at++] = '1';
    uint32_t acc = 0;
    unsigned bits = 0;
    for (unsigned i = 0; i < count; i++) {
        const av_wifi_hint_net_t *net = &hint->net[i];
        for (int k = -1; k < (int)net->len; k++) {
            const uint8_t byte = k < 0 ? (uint8_t)((net->len - 1u) | ((av_wifi_hint_bars(net->rssi) - 1u) << 5) |
                                                   (net->open ? 0x80u : 0u))
                                       : net->ssid[k];
            acc = (acc << 8) | byte;
            bits += 8;
            while (bits >= 5) {
                bits -= 5;
                out[at++] = alphabet[(acc >> bits) & 31u];
            }
        }
    }
    if (bits) out[at++] = alphabet[(acc << (5 - bits)) & 31u];
    out[at] = '\0';
    return at;
}

int av_wifi_hint_qr(const char *url, const av_wifi_hint_t *hint, int max_version, uint8_t qrcode[],
                    uint8_t temp[])
{
    if (!url || !qrcode || !temp) return -1;
    if (max_version > 6) max_version = 6;
    // Static: the setup screen runs on the main task's small stack.
    static char fragment[AV_WIFI_HINT_FRAGMENT_MAX];
    static uint8_t url_data[96];
    static uint8_t mark_data[1];
    static uint8_t list_data[(AV_WIFI_HINT_FRAGMENT_MAX * 11u + 15u) / 16u + 8u];
    const size_t url_len = strlen(url);
    const bool url_alnum = qrcodegen_isAlphanumeric(url);
    const size_t url_need =
        qrcodegen_calcSegmentBufferSize(url_alnum ? qrcodegen_Mode_ALPHANUMERIC : qrcodegen_Mode_BYTE, url_len);
    if (hint && max_version >= 1 && url_need <= sizeof(url_data)) {
        for (unsigned count = hint->count; count > 0; count--) {
            const size_t n = av_wifi_hint_fragment(hint, count, fragment, sizeof(fragment));
            if (!n || qrcodegen_calcSegmentBufferSize(qrcodegen_Mode_ALPHANUMERIC, n) > sizeof(list_data)) {
                continue;
            }
            const struct qrcodegen_Segment segs[3] = {
                url_alnum ? qrcodegen_makeAlphanumeric(url, url_data)
                          : qrcodegen_makeBytes((const uint8_t *)url, url_len, url_data),
                qrcodegen_makeBytes((const uint8_t *)"#", 1, mark_data),
                qrcodegen_makeAlphanumeric(fragment, list_data),
            };
            if (qrcodegen_encodeSegmentsAdvanced(segs, 3, qrcodegen_Ecc_LOW, 1, max_version, qrcodegen_Mask_AUTO,
                                                 true, temp, qrcode)) {
                return (int)count;
            }
        }
    }
    // The plain address, exactly as the setup screen encoded it before.
    return qrcodegen_encodeText(url, temp, qrcode, qrcodegen_Ecc_LOW, 1, 6, qrcodegen_Mask_AUTO, true) ? 0 : -1;
}

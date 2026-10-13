// The networks the device can see, in the sound page's QR code.
//
// The phone page that sends a network as sound (server/relay/assets/wifi.html)
// cannot read the phone's own Wi-Fi name -- no web page can -- so the device
// tells it: the setup access point's scan, cut to the strongest few networks
// the device could join, rides in the code's URL as a fragment,
// "<SITE>/TV/W#1<base32>", and the page offers them as a list to tap. A
// fragment is never sent to a server, so the names stay off server logs.
//
// The list: one record per network, strongest bars first,
//   byte 0: bits 0-4 SSID length - 1 (1..32), bits 5-6 signal bars - 1 (1..4),
//           bit 7 open (no password);
//   then the SSID's bytes (UTF-8).
// The fragment is "1" (the format) and the records in base32hex (RFC 4648
// "0-9A-V", no padding), which a QR code holds in alphanumeric mode next to
// the upper-case URL; only the "#" between them is a byte segment.
//
// Pure C over qrcodegen, so tests/test_av_wifi_hint.c runs it without a device.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Most networks offered. The code drops the weakest until it fits.
#define AV_WIFI_HINT_MAX 6u
// Largest code version the sound page shows with a list. The setup page draws
// its code in a 120-px square of the 240-px design, the middle of both panels
// (the Xingzhi's 240 and the AI Passport's 320 columns): version 5 is 41
// modules with the quiet zone, 3 px each, 123 px (ui_qr_setup_scale()).
// Version 6 would be 2 px a module.
#define AV_WIFI_HINT_QR_MAX_VERSION 5
// Fragment text for the most networks: "1" and the base32 of 6 x 33 bytes.
#define AV_WIFI_HINT_FRAGMENT_MAX (1u + (AV_WIFI_HINT_MAX * 33u * 8u + 4u) / 5u + 1u)

typedef struct {
    uint8_t ssid[32];
    uint8_t len;      // 1..32
    int8_t rssi;      // the strongest record of this name, dBm
    bool open;
} av_wifi_hint_net_t;

typedef struct av_wifi_hint_s {
    av_wifi_hint_net_t net[AV_WIFI_HINT_MAX];
    unsigned count;
} av_wifi_hint_t;

void av_wifi_hint_clear(av_wifi_hint_t *hint);

// Signal bars for an RSSI: 4 from -55 dBm, 3 from -66, 2 from -77, else 1.
unsigned av_wifi_hint_bars(int rssi);

// Offer one scan record. Kept when the name is not empty (a hidden network),
// is valid UTF-8 without control characters (the page has to show and send
// it as text), and `joinable` (a mode the setup can join: open or a
// password; not enterprise). A name seen twice (a mesh, two bands of one
// router) is kept once, with its strongest signal; the strongest
// AV_WIFI_HINT_MAX names stay.
void av_wifi_hint_offer(av_wifi_hint_t *hint, const uint8_t *ssid, size_t len, int rssi, bool open,
                        bool joinable);

// After the last offer: order by bars, strongest first, then by name, so a
// signal that wanders inside one bar does not reorder the list (and redraw
// the code).
void av_wifi_hint_finish(av_wifi_hint_t *hint);

// Whether two finished lists encode the same: names, bars, open, order.
bool av_wifi_hint_same(const av_wifi_hint_t *a, const av_wifi_hint_t *b);

// The fragment text (without "#") for the first `count` networks. Returns its
// length, 0 when `count` is 0 or it does not fit `size`.
size_t av_wifi_hint_fragment(const av_wifi_hint_t *hint, unsigned count, char *out, size_t size);

// Encode the sound page's code: `url` and, when `hint` has networks, "#" and
// the fragment of as many of them as fit in a code of at most `max_version`
// (low error correction, raised when it fits). With no network, or none that
// fits, the code is `url` alone exactly as before (version 1..6). Returns how
// many networks are in it, or -1 when even `url` does not encode. `qrcode` and
// `temp` hold qrcodegen_BUFFER_LEN_FOR_VERSION(6) bytes. Not reentrant: one
// caller (the setup screen).
int av_wifi_hint_qr(const char *url, const av_wifi_hint_t *hint, int max_version, uint8_t qrcode[],
                    uint8_t temp[]);

#ifdef __cplusplus
}
#endif

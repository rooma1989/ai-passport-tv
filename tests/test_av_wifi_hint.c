// The networks in the sound page's QR code (main/av_wifi_hint.c), without a
// device: which scan records are kept, the fragment the phone page decodes
// (pinned to a golden string that tests/test_soundprov_page.py feeds the page's
// own parser), and how many networks a code the setup screen can show holds.
//
// `test_av_wifi_hint --print` also prints, for a few site lengths, how many
// typical networks fit and the code's version and module size.
#include "av_wifi_hint.h"
#include "qrcodegen.h"
#include "ui_qr.h"

#include <stdio.h>
#include <string.h>

// ui_text.c reads the non-ASCII glyphs from a blob the device build embeds; the
// host has none, and these tests draw no text.
const uint8_t ui_font_cjk_blob_start[] __asm__("_binary_ui_font_cjk_bin_start") = { 0, 0, 0, 0 };

static int failures;

static void check(int condition, const char *what)
{
    if (!condition) {
        printf("FAIL: %s\n", what);
        failures++;
    }
}

static void offer(av_wifi_hint_t *h, const char *ssid, int rssi, bool open)
{
    av_wifi_hint_offer(h, (const uint8_t *)ssid, strlen(ssid), rssi, open, true);
}

// The same list as GOLDEN in tests/test_soundprov_page.py.
#define GOLDEN "1CL6NILR98PKKTPDGHVJR3CV8MUNUF55HSMCQH927ELIN6T0"

static void test_bars(void)
{
    check(av_wifi_hint_bars(-30) == 4 && av_wifi_hint_bars(-55) == 4, "4 bars from -55");
    check(av_wifi_hint_bars(-56) == 3 && av_wifi_hint_bars(-66) == 3, "3 bars from -66");
    check(av_wifi_hint_bars(-67) == 2 && av_wifi_hint_bars(-77) == 2, "2 bars from -77");
    check(av_wifi_hint_bars(-78) == 1 && av_wifi_hint_bars(-100) == 1, "1 bar below");
}

static void test_golden_fragment(void)
{
    av_wifi_hint_t h;
    av_wifi_hint_clear(&h);
    // Offered weakest first; the list orders them.
    offer(&h, "Guest", -70, true);
    offer(&h, "\xE5\xB0\x8F\xE7\xB1\xB3\xE8\xB7\xAF\xE7\x94\xB1\xE5\x99\xA8", -60, false);  // 小米路由器
    offer(&h, "MyWiFi", -50, false);
    av_wifi_hint_finish(&h);
    check(h.count == 3, "three networks");
    check(h.net[0].len == 6 && !memcmp(h.net[0].ssid, "MyWiFi", 6), "strongest first");
    char text[AV_WIFI_HINT_FRAGMENT_MAX];
    const size_t n = av_wifi_hint_fragment(&h, 3, text, sizeof(text));
    check(n == strlen(GOLDEN) && !strcmp(text, GOLDEN), "the golden fragment");
    if (strcmp(text, GOLDEN)) printf("  got %s\n", text);
    check(av_wifi_hint_fragment(&h, 1, text, sizeof(text)) == 1u + (7u * 8u + 4u) / 5u, "one network");
    check(!strncmp(text, GOLDEN, 10), "a prefix of the list is a prefix of the text up to the cut");
    check(av_wifi_hint_fragment(&h, 0, text, sizeof(text)) == 0, "no network, no fragment");
    check(av_wifi_hint_fragment(&h, 4, text, sizeof(text)) == 0, "more than there are");
    check(av_wifi_hint_fragment(&h, 3, text, 10) == 0, "too small a buffer");
}

static void test_what_is_kept(void)
{
    av_wifi_hint_t h;
    av_wifi_hint_clear(&h);
    av_wifi_hint_offer(&h, (const uint8_t *)"", 0, -40, false, true);
    check(h.count == 0, "a hidden network is skipped");
    av_wifi_hint_offer(&h, (const uint8_t *)"Corp", 4, -40, false, false);
    check(h.count == 0, "an enterprise network is skipped");
    av_wifi_hint_offer(&h, (const uint8_t *)"\xC4\xE3\xBA\xC3", 4, -40, false, true);  // GBK, not UTF-8
    check(h.count == 0, "a name that is not UTF-8 is skipped");
    av_wifi_hint_offer(&h, (const uint8_t *)"a\tb", 3, -40, false, true);
    check(h.count == 0, "a control character is skipped");
    av_wifi_hint_offer(&h, (const uint8_t *)"\xC0\xAF", 2, -40, false, true);
    check(h.count == 0, "an overlong form is skipped");
    av_wifi_hint_offer(&h, (const uint8_t *)"\xED\xA0\x80", 3, -40, false, true);
    check(h.count == 0, "a surrogate is skipped");
    av_wifi_hint_offer(&h, (const uint8_t *)"\xE5\xB0", 2, -40, false, true);
    check(h.count == 0, "a cut-off character is skipped");
    char long_name[34];
    memset(long_name, 'x', 33);
    long_name[33] = '\0';
    av_wifi_hint_offer(&h, (const uint8_t *)long_name, 33, -40, false, true);
    check(h.count == 0, "a name over 32 bytes is skipped");
    av_wifi_hint_offer(&h, (const uint8_t *)long_name, 32, -40, false, true);
    check(h.count == 1 && h.net[0].len == 32, "32 bytes is kept");

    // A mesh: one name from three access points, kept once at its best.
    av_wifi_hint_clear(&h);
    offer(&h, "Home", -80, false);
    offer(&h, "Home", -48, false);
    offer(&h, "Home", -70, false);
    check(h.count == 1 && h.net[0].rssi == -48, "one entry, the strongest signal");

    // More than six: the strongest six stay, whatever the order they come in.
    av_wifi_hint_clear(&h);
    const char *names[] = { "n0", "n1", "n2", "n3", "n4", "n5", "n6", "n7", "n8" };
    const int rssi[] = { -90, -40, -85, -50, -60, -95, -70, -45, -65 };
    for (int i = 0; i < 9; i++) offer(&h, names[i], rssi[i], false);
    av_wifi_hint_finish(&h);
    check(h.count == AV_WIFI_HINT_MAX, "six kept");
    for (unsigned i = 0; i < h.count; i++) {
        check(h.net[i].rssi >= -85, "the weakest are the ones dropped");
    }
    // A weak duplicate of a dropped name does not come back, a strong one does.
    offer(&h, "n5", -96, false);
    check(h.count == 6, "still six");
    offer(&h, "n5", -30, false);
    av_wifi_hint_finish(&h);
    bool back = false;
    for (unsigned i = 0; i < h.count; i++) back |= h.net[i].len == 2 && !memcmp(h.net[i].ssid, "n5", 2);
    check(back && h.count == 6, "a stronger record brings it back");
    check(h.net[0].len == 2 && !memcmp(h.net[0].ssid, "n1", 2) && h.net[2].len == 2 &&
          !memcmp(h.net[2].ssid, "n5", 2), "4 bars each (n1 n3 n5 n7): by name");
}

static void test_order_and_sameness(void)
{
    av_wifi_hint_t a, b;
    av_wifi_hint_clear(&a);
    av_wifi_hint_clear(&b);
    offer(&a, "Beta", -60, false);
    offer(&a, "Alpha", -58, false);
    av_wifi_hint_finish(&a);
    // The same networks, the signals wandering inside their bars and the scan
    // listing them the other way round.
    offer(&b, "Alpha", -64, false);
    offer(&b, "Beta", -57, false);
    av_wifi_hint_finish(&b);
    check(a.net[0].len == 5 && !memcmp(a.net[0].ssid, "Alpha", 5), "same bars: by name");
    check(av_wifi_hint_same(&a, &b), "the same list: the code is not redrawn");
    av_wifi_hint_clear(&b);
    offer(&b, "Alpha", -50, false);
    offer(&b, "Beta", -60, false);
    av_wifi_hint_finish(&b);
    check(!av_wifi_hint_same(&a, &b), "a bar changed: redrawn");
    av_wifi_hint_clear(&b);
    offer(&b, "Alpha", -58, true);
    offer(&b, "Beta", -60, false);
    av_wifi_hint_finish(&b);
    check(!av_wifi_hint_same(&a, &b), "open changed: redrawn");
    av_wifi_hint_t empty;
    av_wifi_hint_clear(&empty);
    check(!av_wifi_hint_same(&a, &empty) && !av_wifi_hint_same(NULL, &a), "a different count");
}

static uint8_t qr[qrcodegen_BUFFER_LEN_FOR_VERSION(6)], tmp[qrcodegen_BUFFER_LEN_FOR_VERSION(6)];

static int version_of(void)
{
    return (qrcodegen_getSize(qr) - 17) / 4;
}

// A plausible neighbourhood: router names as they come out of the box.
static void neighbourhood(av_wifi_hint_t *h)
{
    av_wifi_hint_clear(h);
    offer(h, "TP-LINK_5A3F", -48, false);
    offer(h, "ChinaNet-xQ7d", -59, false);
    offer(h, "\xE5\xB0\x8F\xE7\xB1\xB3\xE8\xB7\xAF\xE7\x94\xB1\xE5\x99\xA8", -63, false);  // 小米路由器
    offer(h, "CMCC-2F9A", -70, false);
    offer(h, "HUAWEI-B3C1", -74, false);
    offer(h, "Guest", -81, true);
    av_wifi_hint_finish(h);
}

static void test_code_fits(void)
{
    const char *url = "HTTPS://EXAMPLE.COM/TV/W";
    uint8_t plain[sizeof(qr)];
    check(qrcodegen_encodeText(url, tmp, plain, qrcodegen_Ecc_LOW, 1, 6, qrcodegen_Mask_AUTO, true),
          "the plain address encodes");

    // No list: exactly the code the screen showed before.
    av_wifi_hint_t h;
    av_wifi_hint_clear(&h);
    check(av_wifi_hint_qr(url, &h, AV_WIFI_HINT_QR_MAX_VERSION, qr, tmp) == 0, "no network: the plain address");
    check(!memcmp(qr, plain, (size_t)qrcodegen_BUFFER_LEN_FOR_VERSION((qrcodegen_getSize(plain) - 17) / 4)),
          "the same code as before");
    check(av_wifi_hint_qr(url, NULL, AV_WIFI_HINT_QR_MAX_VERSION, qr, tmp) == 0, "no list at all");

    neighbourhood(&h);
    const int n = av_wifi_hint_qr(url, &h, AV_WIFI_HINT_QR_MAX_VERSION, qr, tmp);
    check(n == 6, "six typical networks fit a short site");
    check(version_of() <= AV_WIFI_HINT_QR_MAX_VERSION, "within the largest version");
    const int modules = qrcodegen_getSize(qr) + 2 * UI_QR_QUIET;
    const int scale = ui_qr_setup_scale(modules);
    check(scale >= 3, "at least 3 px a module");
    check(modules * scale <= UI_QR_SETUP_BOX_MAX, "inside the setup box");

    // Long names: networks are dropped from the weak end until it fits.
    av_wifi_hint_clear(&h);
    for (int i = 0; i < 6; i++) {
        char name[33];
        snprintf(name, sizeof(name), "Long-network-name-number-%d-xyzw", i);
        offer(&h, name, -40 - 5 * i, false);
    }
    av_wifi_hint_finish(&h);
    const int k = av_wifi_hint_qr(url, &h, AV_WIFI_HINT_QR_MAX_VERSION, qr, tmp);
    check(k >= 1 && k < 6, "some, not all, 32-byte names fit");
    check(version_of() <= AV_WIFI_HINT_QR_MAX_VERSION, "and the code stays small");
    av_wifi_hint_t one = h;
    one.count = (unsigned)k + 1u;
    check(av_wifi_hint_qr(url, &one, AV_WIFI_HINT_QR_MAX_VERSION, qr, tmp) == k, "the first one dropped did not fit");

    // A site so long nothing fits beside it: the plain address.
    const char *huge = "HTTPS://A-VERY-LONG-SUBDOMAIN.OF-A-VERY-LONG-DOMAIN-NAME.EXAMPLE.COM/TV/W";
    av_wifi_hint_clear(&h);
    for (int i = 0; i < 6; i++) {
        char name[33];
        snprintf(name, sizeof(name), "Another-long-network-name-%d-abc", i);
        offer(&h, name, -40, false);
    }
    av_wifi_hint_finish(&h);
    const int none = av_wifi_hint_qr(huge, &h, 2, qr, tmp);
    check(none == 0, "nothing fits version 2: the plain address");
    check(version_of() >= 1, "which still encodes");

    // A lower-case address goes in byte mode and still works.
    neighbourhood(&h);
    check(av_wifi_hint_qr("https://example.com/tv/w", &h, AV_WIFI_HINT_QR_MAX_VERSION, qr, tmp) >= 4,
          "a lower-case address carries a list too");
    check(av_wifi_hint_qr(NULL, &h, 5, qr, tmp) == -1, "no address");
}

static void print_table(void)
{
    const char *sites[] = { "HTTPS://TV.CN/TV/W", "HTTPS://EXAMPLE.COM/TV/W", "HTTPS://TV.EXAMPLE-SITE.COM/TV/W",
                            "HTTPS://REMOTE.A-LONGER-DOMAIN.EXAMPLE.ORG/TV/W" };
    av_wifi_hint_t h;
    neighbourhood(&h);
    for (unsigned i = 0; i < sizeof(sites) / sizeof(sites[0]); i++) {
        const int n = av_wifi_hint_qr(sites[i], &h, AV_WIFI_HINT_QR_MAX_VERSION, qr, tmp);
        const int modules = qrcodegen_getSize(qr) + 2 * UI_QR_QUIET;
        const int scale = ui_qr_setup_scale(modules);
        printf("%-48s %d networks, version %d, %d px/module, %d px\n", sites[i], n, version_of(), scale,
               modules * scale);
    }
}

int main(int argc, char **argv)
{
    test_bars();
    test_golden_fragment();
    test_what_is_kept();
    test_order_and_sameness();
    test_code_fits();
    if (argc > 1 && !strcmp(argv[1], "--print")) print_table();
    if (failures) {
        printf("test_av_wifi_hint: %d failure(s)\n", failures);
        return 1;
    }
    printf("test_av_wifi_hint: all cases passed\n");
    return 0;
}

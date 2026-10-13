// The setup screen's QR codes, tested without a device.
//
// A code that does not scan looks exactly like one that does, so the checks are
// on the things that decide it: the join string's escaping, whether both codes fit
// the panel at a usable size, and whether the drawn pixels have a dark finder
// corner inside a light quiet zone.
#include "ui_qr.h"
#include "qrcodegen.h"

#include <stdint.h>
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

static void test_wifi_text(void)
{
    char out[96];
    check(ui_qr_wifi_text(out, sizeof(out), "FoloToy-04E1"), "a plain SSID fits");
    check(strcmp(out, "WIFI:T:nopass;S:FoloToy-04E1;;") == 0, "open-network join string");
    check(ui_qr_wifi_text(out, sizeof(out), "a;b,c:d\\e\"f"), "special characters fit");
    check(strcmp(out, "WIFI:T:nopass;S:a\\;b\\,c\\:d\\\\e\\\"f;;") == 0,
          "; , : \\ \" are escaped");
    char tiny[10];
    check(!ui_qr_wifi_text(tiny, sizeof(tiny), "FoloToy-04E1"), "too small a buffer is refused");
}

static void test_layout(void)
{
    ui_qr_layout_t l;
    check(ui_qr_layout(240, 6, 25, 25, &l), "two version-2 codes fit 240 wide");
    check(l.scale == 4, "240 wide uses 4 px modules");
    check(l.side_a == (25 + 2 * UI_QR_QUIET) * 4 && l.side_b == l.side_a, "side includes the quiet zone");
    check(l.x_a >= 2 && l.x_b + l.side_b <= 238, "both stay off the panel edges");
    check(l.x_b - (l.x_a + l.side_a) >= l.scale, "a module of gap between them");
    check(l.y == 6, "tops at the requested row");

    check(ui_qr_layout(320, 6, 25, 25, &l), "two version-2 codes fit 320 wide");
    check(l.scale == UI_QR_MAX_SCALE, "320 wide is capped at the largest module size");
    check(l.x_b + l.side_b <= 318, "320 layout stays on the panel");

    check(!ui_qr_layout(240, 6, 57, 57, &l), "two version-10 codes cannot share 240 px");
}

static void test_layout_single(void)
{
    ui_qr_layout_t l;
    check(ui_qr_layout_single(240, 32, 25, 150, &l), "one version-2 code in 150 rows");
    check(l.scale == 5 && l.side_a == 145, "largest scale under the height limit");
    check(l.x_a == (240 - 145) / 2 && l.y == 32, "centred, at the requested row");
    check(l.x_b == 0 && l.side_b == 0, "no second code");
    check(ui_qr_layout_single(240, 32, 21, 80, &l) && l.scale == 3 && l.side_a == 75,
          "a version-1 code in 80 rows is 3 px per module");
    check(ui_qr_layout_single(240, 0, 25, 400, &l) && l.scale == UI_QR_SINGLE_MAX_SCALE &&
          l.side_a <= 236, "capped by the largest scale and the panel width");
    check(!ui_qr_layout_single(240, 0, 25, 40, &l), "too little height is refused");
}

static void test_upper_case_url_is_version_1(void)
{
    // The status page only has room for a version-1 code. Upper case keeps a
    // 21-character URL in alphanumeric mode, which version 1 holds.
    static uint8_t qr[qrcodegen_BUFFER_LEN_FOR_VERSION(6)];
    static uint8_t tmp[qrcodegen_BUFFER_LEN_FOR_VERSION(6)];
    check(qrcodegen_encodeText("HTTPS://IEWWW.COM/TV/", tmp, qr, qrcodegen_Ecc_LOW, 1, 6,
                               qrcodegen_Mask_AUTO, true), "the help URL encodes");
    check(qrcodegen_getSize(qr) == 21, "upper-case help URL is version 1");
    // The phone remote's sign-in address: the site in upper case, /TV/R/, and a
    // 20-character base32 code -- 43 characters, which version 2 holds at low
    // error correction. The status page has room for version 2 at 3 px/module.
    check(qrcodegen_encodeText("HTTPS://IEWWW.COM/TV/R/ABCDEFGHIJKLMNOPQR27", tmp, qr,
                               qrcodegen_Ecc_LOW, 1, 3, qrcodegen_Mask_AUTO, true),
          "remote URL encodes");
    check(qrcodegen_getSize(qr) == 25, "remote URL is version 2");
}

static uint16_t pixel(const uint8_t *buf, int width, int x, int y)
{
    const uint8_t *p = buf + ((size_t)y * (size_t)width + (size_t)x) * 2u;
    return (uint16_t)((p[0] << 8) | p[1]);
}

static void test_draw(void)
{
    static uint8_t qr[qrcodegen_BUFFER_LEN_FOR_VERSION(6)];
    static uint8_t tmp[qrcodegen_BUFFER_LEN_FOR_VERSION(6)];
    check(qrcodegen_encodeText("http://192.168.4.1", tmp, qr, qrcodegen_Ecc_LOW, 1, 6,
                               qrcodegen_Mask_AUTO, true), "the setup URL encodes");
    const int scale = 4, x = 10, y = 10;
    const int side = (qrcodegen_getSize(qr) + 2 * UI_QR_QUIET) * scale;
    enum { W = 160, H = 160 };
    static uint8_t buf[W * H * 2];
    memset(buf, 0x55, sizeof(buf));   // neither black nor white
    ui_surface_t surface = { buf, W, 0, H, 0, 0 };
    ui_qr_draw(surface, x, y, scale, qr);

    const int quiet = UI_QR_QUIET * scale;
    check(pixel(buf, W, x, y) == 0xFFFFu, "quiet zone corner is white");
    check(pixel(buf, W, x + quiet - 1, y + quiet - 1) == 0xFFFFu, "quiet zone inner edge is white");
    check(pixel(buf, W, x + quiet, y + quiet) == 0x0000u, "finder pattern corner is black");
    check(pixel(buf, W, x + side - 1, y + side - 1) == 0xFFFFu, "far quiet corner is white");
    check(pixel(buf, W, x + side, y + side) == 0x5555u, "nothing drawn outside the code");

    // Drawn one stripe at a time it must come out the same.
    static uint8_t striped[W * H * 2];
    memset(striped, 0x55, sizeof(striped));
    for (int row = 0; row < H; row += 12) {
        int rows = H - row < 12 ? H - row : 12;
        ui_surface_t strip = { striped + (size_t)row * W * 2u, W, row, rows, 0, 0 };
        ui_qr_draw(strip, x, y, scale, qr);
    }
    check(memcmp(buf, striped, sizeof(buf)) == 0, "stripe-by-stripe drawing matches");
}

static void test_setup_scale(void)
{
    // Versions 1..6 with the two-module quiet zone each side.
    check(ui_qr_setup_scale(21 + 4) == 4 && ui_qr_setup_scale(25 + 4) == 4, "versions 1-2 at 4 px");
    check(ui_qr_setup_scale(29 + 4) == 3 && ui_qr_setup_scale(33 + 4) == 3, "versions 3-4 at 3 px");
    // Version 5 would be 2 px in 120; it takes 123 px at 3 px instead.
    check(ui_qr_setup_scale(37 + 4) == 3 && (37 + 4) * 3 <= UI_QR_SETUP_BOX_MAX, "version 5 at 3 px");
    check(ui_qr_setup_scale(41 + 4) == 2, "version 6 stays at 2 px");
    check(ui_qr_setup_scale(0) == UI_QR_MIN_SCALE, "nonsense gets the smallest");
}

int main(void)
{
    test_setup_scale();
    test_wifi_text();
    test_layout();
    test_layout_single();
    test_upper_case_url_is_version_1();
    test_draw();
    if (failures) {
        printf("%d failure(s)\n", failures);
        return 1;
    }
    printf("test_ui_qr: PASS\n");
    return 0;
}

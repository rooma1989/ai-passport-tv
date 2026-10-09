// QR codes on the setup screen: the Wi-Fi join string, where two codes go side by
// side, and drawing one into a stripe.
//
// Pure C over ui_text_fill, so tests/test_ui_qr.c runs it without a device. The
// codes themselves come from components/qrcodegen.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "ui_text.h"

// Light border around each code, in modules. The standard asks for four; two is
// what phone cameras read reliably, and it is what lets two codes share a 240 px
// panel at a scannable size.
#define UI_QR_QUIET 2
// Largest module size in pixels. The setup screen puts four text lines under the
// codes in a 240-row panel; at 5 px a version-2 code is 145 rows and they no
// longer fit, at 4 px it is 116 and they do.
#define UI_QR_MAX_SCALE 4
// Smallest module size that still scans from arm's length on this panel.
#define UI_QR_MIN_SCALE 2
// Largest module size when a page shows a single code.
#define UI_QR_SINGLE_MAX_SCALE 6

typedef struct {
    int scale;    // pixels per module, shared by both codes
    int x_a, x_b; // left edge of each code's quiet zone
    int y;        // top edge of both quiet zones
    int side_a;   // full side of code a including its quiet zone, in pixels
    int side_b;
} ui_qr_layout_t;

// "WIFI:T:nopass;S:<ssid>;;" with \ ; , : " in the SSID escaped by a backslash,
// the form phone cameras recognise as "join this network". False when it does
// not fit in `size`.
bool ui_qr_wifi_text(char *out, size_t size, const char *ssid);

// Place two codes of `modules_a` and `modules_b` modules side by side, centred in
// `panel_w`, tops at `y`. Both share one scale: the largest up to UI_QR_MAX_SCALE
// that fits with one module of gap between the codes (each already has its own
// light quiet zone) and 2 px at each panel edge. False when even UI_QR_MIN_SCALE does not fit.
bool ui_qr_layout(int panel_w, int y, int modules_a, int modules_b, ui_qr_layout_t *out);

// Place one code of `modules` modules, centred in `panel_w`, top at `y`, at the
// largest scale up to UI_QR_SINGLE_MAX_SCALE whose side (quiet zone included)
// fits both `max_side` and the panel width less 2 px each side. Fills scale, x_a,
// side_a and y; x_b and side_b are zero. False below UI_QR_MIN_SCALE.
bool ui_qr_layout_single(int panel_w, int y, int modules, int max_side, ui_qr_layout_t *out);

// Paint one encoded code (qrcodegen format) with its quiet zone, top-left at
// (x, y), clipped to the rows `surface` covers.
void ui_qr_draw(ui_surface_t surface, int x, int y, int scale, const uint8_t qrcode[]);

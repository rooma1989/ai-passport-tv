// main/ui_qr.c -- see ui_qr.h.
#include "ui_qr.h"

#include "qrcodegen.h"

#include <string.h>

bool ui_qr_wifi_text(char *out, size_t size, const char *ssid)
{
    static const char head[] = "WIFI:T:nopass;S:";
    static const char tail[] = ";;";
    if (!out || !ssid || size == 0) {
        return false;
    }
    size_t n = 0;
    for (const char *p = head; *p; p++) {
        if (n + 1 >= size) return false;
        out[n++] = *p;
    }
    for (const char *p = ssid; *p; p++) {
        if (strchr("\\;,:\"", *p)) {
            if (n + 1 >= size) return false;
            out[n++] = '\\';
        }
        if (n + 1 >= size) return false;
        out[n++] = *p;
    }
    for (const char *p = tail; *p; p++) {
        if (n + 1 >= size) return false;
        out[n++] = *p;
    }
    out[n] = '\0';
    return true;
}

bool ui_qr_layout(int panel_w, int y, int modules_a, int modules_b, ui_qr_layout_t *out)
{
    if (!out || modules_a <= 0 || modules_b <= 0) {
        return false;
    }
    const int edge = 2;
    for (int scale = UI_QR_MAX_SCALE; scale >= UI_QR_MIN_SCALE; scale--) {
        const int side_a = (modules_a + 2 * UI_QR_QUIET) * scale;
        const int side_b = (modules_b + 2 * UI_QR_QUIET) * scale;
        const int gap = scale;
        const int total = side_a + gap + side_b;
        if (total > panel_w - 2 * edge) {
            continue;
        }
        out->scale = scale;
        out->side_a = side_a;
        out->side_b = side_b;
        out->x_a = (panel_w - total) / 2;
        out->x_b = out->x_a + side_a + gap;
        out->y = y;
        return true;
    }
    return false;
}

void ui_qr_draw(ui_surface_t surface, int x, int y, int scale, const uint8_t qrcode[])
{
    const int size = qrcodegen_getSize(qrcode);
    const int quiet = UI_QR_QUIET * scale;
    const int side = size * scale + 2 * quiet;
    // Rows outside this stripe are skipped before any work: the code is drawn
    // once per stripe, and most stripes see none of it.
    if (y + side <= surface.origin_y || y >= surface.origin_y + surface.rows) {
        return;
    }
    ui_text_fill(surface, x, y, side, side, 0xFFFFu);
    for (int my = 0; my < size; my++) {
        const int py = y + quiet + my * scale;
        if (py + scale <= surface.origin_y || py >= surface.origin_y + surface.rows) {
            continue;
        }
        // Runs of dark modules become one fill each.
        int mx = 0;
        while (mx < size) {
            if (!qrcodegen_getModule(qrcode, mx, my)) {
                mx++;
                continue;
            }
            int start = mx;
            while (mx < size && qrcodegen_getModule(qrcode, mx, my)) {
                mx++;
            }
            ui_text_fill(surface, x + quiet + start * scale, py, (mx - start) * scale, scale, 0x0000u);
        }
    }
}

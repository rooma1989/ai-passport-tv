// Which saved network the station tries first
// (components/esp-wifi-connect/wifi_order.c): the viewer's order, except that a
// network barely in range goes after every usable one.
#include "wifi_order.h"

#include <assert.h>
#include <stdio.h>

static void check(const wifi_order_candidate_t *c, size_t n, const size_t *want)
{
    size_t order[16];
    wifi_order_sort(c, n, order);
    for (size_t i = 0; i < n; i++) {
        if (order[i] != want[i]) {
            fprintf(stderr, "position %zu: got %zu want %zu\n", i, order[i], want[i]);
            assert(0);
        }
    }
}

int main(void)
{
    // Priority beats a stronger signal: the old rule picked index 1.
    const wifi_order_candidate_t a[] = { { 1, -40 }, { 0, -65 } };
    check(a, 2, (const size_t[]){ 1, 0 });

    // A weak first choice goes after a usable second one.
    const wifi_order_candidate_t b[] = { { 0, -85 }, { 1, -60 } };
    check(b, 2, (const size_t[]){ 1, 0 });
    // The boundary itself counts as weak.
    const wifi_order_candidate_t b2[] = { { 0, WIFI_ORDER_WEAK_RSSI }, { 1, WIFI_ORDER_WEAK_RSSI + 1 } };
    check(b2, 2, (const size_t[]){ 1, 0 });

    // Several access points with one name (a mesh): strongest first, still
    // ahead of a lower-priority network.
    const wifi_order_candidate_t c[] = { { 0, -70 }, { 2, -30 }, { 0, -50 }, { 1, -45 } };
    check(c, 4, (const size_t[]){ 2, 0, 3, 1 });

    // All weak: strongest first, priority breaks ties.
    const wifi_order_candidate_t d[] = { { 2, -88 }, { 0, -90 }, { 1, -88 } };
    check(d, 3, (const size_t[]){ 2, 0, 1 });

    // Equal keys keep scan order; empty and single inputs work.
    const wifi_order_candidate_t e[] = { { 3, -50 }, { 3, -50 } };
    check(e, 2, (const size_t[]){ 0, 1 });
    check(e, 1, (const size_t[]){ 0 });
    wifi_order_sort(e, 0, NULL);

    puts("test_wifi_order: PASS");
    return 0;
}

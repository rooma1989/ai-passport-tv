// Which saved network to try first, kept apart from the Wi-Fi driver so it can
// be tested on a host (tests/test_wifi_order.c).
//
// The order used to be signal strength alone: the strongest saved network in a
// scan won, whatever the viewer preferred. The saved list now has an order the
// viewer sets (the setup page's arrow, the phone remote's "move up"), so that
// order decides -- with one exception. A network barely in range is tried after
// every network with a usable signal, because joining it means a picture that
// stutters, and the viewer who put it first did not ask for that.
#pragma once
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// At or below this a link carries too little for video (measured on the
// Xingzhi: under about -80 dBm the stream rate collapses).
#define WIFI_ORDER_WEAK_RSSI (-80)

typedef struct {
    int priority;   // index in the saved list, 0 = first
    int8_t rssi;    // from the scan
} wifi_order_candidate_t;

// Fill `order` with the indices 0..count-1 of `candidates` in the order to try
// them: usable signals first by priority (a stronger access point first among
// several carrying one name), then weak ones strongest first. Stable for equal
// keys, so the result does not depend on the driver's scan order.
void wifi_order_sort(const wifi_order_candidate_t *candidates, size_t count, size_t *order);

#ifdef __cplusplus
}
#endif

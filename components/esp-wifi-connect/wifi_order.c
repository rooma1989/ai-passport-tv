#include "wifi_order.h"

#include <stdbool.h>

// a before b?
static bool before(const wifi_order_candidate_t *a, const wifi_order_candidate_t *b)
{
    const bool a_weak = a->rssi <= WIFI_ORDER_WEAK_RSSI;
    const bool b_weak = b->rssi <= WIFI_ORDER_WEAK_RSSI;
    if (a_weak != b_weak) {
        return !a_weak;
    }
    if (!a_weak && a->priority != b->priority) {
        return a->priority < b->priority;
    }
    if (a->rssi != b->rssi) {
        return a->rssi > b->rssi;
    }
    // Weak and equally strong: the viewer's order breaks the tie.
    return a->priority < b->priority;
}

void wifi_order_sort(const wifi_order_candidate_t *candidates, size_t count, size_t *order)
{
    // Insertion sort: a scan holds a few dozen records at most, and this keeps
    // equal keys in scan order without an allocation.
    for (size_t i = 0; i < count; i++) {
        size_t j = i;
        while (j > 0 && before(&candidates[i], &candidates[order[j - 1]])) {
            order[j] = order[j - 1];
            j--;
        }
        order[j] = i;
    }
}

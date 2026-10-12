#pragma once

// Living beside the Muse gadget firmware (CONFIG_AV_MUSE_DUAL): Muse sits in
// ota_0, this player in ota_1, and each switches to the other by setting the
// boot slot and restarting. Both read the same NVS partition, each its own
// namespaces.

#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

// Tells Muse's bootloader this image works, so it isn't rolled back to Muse at
// the next restart. Call once the display and Wi-Fi stack are up.
void av_muse_mark_valid(void);

// The network Muse joins (its "homehub" namespace), for a first boot that has
// none of its own. False if Muse has none either.
bool av_muse_wifi(char *ssid, size_t ssid_cap, char *password, size_t password_cap);

// Until the player has stored a volume of its own, Muse's speaker volume
// (0..100); otherwise, or if Muse has none, leaves *percent alone.
void av_muse_volume(unsigned char *percent);

// Boots Muse: sets ota_0 as the boot slot and restarts. Returns only if that
// can't be done.
void av_muse_back(void);

#ifdef __cplusplus
}
#endif

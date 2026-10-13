// The decisions around setup mode, kept apart from the WiFi stack so they can be
// tested on a host. Nothing here touches ESP-IDF or the component.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// What the firmware should do when it starts.
typedef enum {
    // A network is known: join it and play.
    AV_BOOT_PLAY = 0,
    // No network is known: raise the setup access point and wait to be told one.
    AV_BOOT_SETUP,
} av_boot_mode_t;

// How long the setup access point stays up with nobody completing it.
//
// It is an open access point, so leaving it up indefinitely means leaving an
// unauthenticated network in the room for as long as the device has power.
// Long enough for someone to find the network, open the page and type a
// password; not long enough to be abandoned without limit.
#define AV_SETUP_WINDOW_MS (15u * 60u * 1000u)

// Which mode to start in.
//
// `has_network` means a network is known from the setup page's store or from the
// credentials compiled into this build. `server_configured` means somewhere to
// stream from is known, from the setup page's store or from this build.
//
// Both are collected by the setup page, so either missing sends the device
// there. A device that can neither join a network nor find a server has nothing
// to show and no other way to be told, which is the state a published build
// starts in.
av_boot_mode_t av_boot_mode(bool has_network, bool server_configured);

// Whether a setup session that began at `started_ms` has run out of time.
// A `limit_ms` of zero means no limit, which is what a caller uses when it wants
// setup to stay until it is finished or cancelled.
bool av_setup_expired(uint32_t started_ms, uint32_t now_ms, uint32_t limit_ms);

// ---- Opening setup on its own, and leaving it again ----

// How long a device that has networks saved waits at boot for any of them
// before it opens setup by itself. Long enough for a router that lost power
// together with the device to come back (a minute or so for most); short
// enough that someone who carried the device somewhere new is not left looking
// at "connecting" with no way forward.
#define AV_BOOT_WIFI_WAIT_MS (60u * 1000u)

// Whether to open setup now, unasked. Only at boot: a device that has been
// online since it started and then loses its network (a router restart in the
// evening) keeps waiting for it instead, because nothing about its settings
// has changed. Once per boot.
bool av_boot_auto_setup(bool has_network, bool ever_connected, bool already_opened,
                        uint32_t waited_ms);

// Whether a setup that opened on its own should close again because a saved
// network is back in range -- unless someone is in the middle of using it: a
// phone on the hotspot, or a sound message being received.
bool av_auto_setup_leave(bool automatic, bool saved_network_visible, bool phone_on_hotspot,
                         bool sound_busy);

// ---- The setup pages ----

typedef enum {
    // Sound: open <site>/tv/wifi on a phone and press "send" near the device.
    AV_SETUP_PAGE_SOUND = 0,
    AV_SETUP_PAGE_JOIN,       // join the device's hotspot
    AV_SETUP_PAGE_OPEN,       // open the hotspot's setup page
    AV_SETUP_PAGE_HELP,       // the help page, when a URL is compiled in
    AV_SETUP_PAGE_STATUS,     // hotspot, page, server, countdown
} av_setup_page_kind_t;

#define AV_SETUP_PAGES_MAX 5u

// The pages in order. Sound comes first when this build can receive it (the
// decoder is linked in); help only when a help URL exists. Returns the count.
unsigned av_setup_pages(bool sound, bool help, av_setup_page_kind_t out[AV_SETUP_PAGES_MAX]);

// The site part of an absolute URL, in lower case: "HTTPS://EXAMPLE.COM/TV/R/X"
// gives "https://example.com". The remote URL in CONFIG is upper case (so its QR
// code fits alphanumeric mode); host names are case-insensitive, the path the
// phone page lives at is not. False when `url` is not http(s)://host...
bool av_setup_site_from_url(const char *url, char *site, size_t size);

// The phone page that plays the sound, for the setup screen's code:
// "<SITE>/TV/W" in upper case ("HTTPS://EXAMPLE.COM/TV/W"), which keeps the
// code in QR alphanumeric mode -- version 1 or 2 for a usual site. The relay
// serves the page at /tv/wifi and at this short path. False if it does not fit.
bool av_setup_wifi_page(const char *site, char *out, size_t size);
// The same address to read off the screen and type: the host without the
// scheme, "example.com/TV/W". False if it does not fit.
bool av_setup_wifi_page_text(const char *site, char *out, size_t size);

// ---- Sound provisioning on the setup screen ----

typedef enum {
    AV_SOUND_OFF = 0,       // not available, or not running
    AV_SOUND_LISTENING,     // microphone on, nothing heard yet
    AV_SOUND_RECEIVING,     // a message is coming in (progress 0..100)
    AV_SOUND_CONNECTING,    // decoded; trying the network like the web form does
    AV_SOUND_FAILED,        // decoded, but the network could not be joined
    AV_SOUND_DONE,          // joined and saved; the device restarts
} av_sound_state_t;

// The line under the code on the sound page for this state.
void av_setup_sound_line(av_sound_state_t state, unsigned progress, char *out, size_t size);
